"""두 API 를 각자의 호출 한도 안에서 주기적으로 조회."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

import aiohttp

from . import apis, timeutil
from .dispatcher import Dispatcher
from .models import DisasterMsg, EqkEvent, EqkReport
from .quota import Quota
from .storage import Storage

log = logging.getLogger(__name__)

ALERT_AFTER_FAILS = 5


class Poller:
    name = "poller"
    source = ""

    def __init__(self, session: aiohttp.ClientSession, storage: Storage, quota: Quota,
                 dispatcher: Dispatcher, key: str, stale_minutes: int):
        self.session = session
        self.storage = storage
        self.quota = quota
        self.dispatcher = dispatcher
        self.key = key
        self.stale = timedelta(minutes=stale_minutes)
        self._wake = asyncio.Event()
        self._last_poll = 0.0
        self._backoff_until = 0.0
        self._burst_until = 0.0
        self._burst_interval = 0.0
        self._burst_done = lambda: False
        self._day = timeutil.ymd()
        # 상태 표시용
        self.fails = 0
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self._alerted = False

    # ---------------------------------------------------------- burst
    def burst(self, seconds: float, interval: float, done=lambda: False) -> None:
        self._burst_until = max(self._burst_until, time.time() + seconds)
        self._burst_interval = interval
        self._burst_done = done
        self._wake.set()

    def _in_burst(self) -> bool:
        return time.time() < self._burst_until and not self._burst_done()

    # ---------------------------------------------------------- loop
    async def _next_wait(self) -> float:
        now = time.time()
        if now < self._backoff_until:
            return self._backoff_until - now
        interval = await self.quota.next_interval()
        if self._in_burst():
            interval = min(interval, self._burst_interval)
        return max(0.0, self._last_poll + interval - now)

    async def run(self) -> None:
        if not self.key:
            log.error("%s: API 키가 없어 비활성화됨", self.name)
            self.last_error = "API 키 없음"
            return
        while True:
            if timeutil.ymd() != self._day:
                self._day = timeutil.ymd()
                self.quota.reset_day()
                await self.storage.prune()
            wait = await self._next_wait()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
                self._wake.clear()
                continue        # 깨어났으면 대기시간 재계산
            except asyncio.TimeoutError:
                pass
            if not await self.quota.acquire(burst=self._in_burst()):
                self._last_poll = time.time()
                continue
            self._last_poll = time.time()
            try:
                await self.poll()
                await self._ok()
            except apis.ApiError as e:
                await self._fail(e, e.limit_exceeded, e.fatal)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                await self._fail(e, False, False)
            except Exception as e:      # noqa: BLE001 - 루프는 죽지 않아야 함
                log.exception("%s: 예상치 못한 오류", self.name)
                await self._fail(e, False, False)

    async def _ok(self) -> None:
        if self._alerted:
            await self.dispatcher.admin(f"{self.name}: 정상 복구됨")
        self.fails = 0
        self._alerted = False
        self.last_ok = time.time()
        self.last_error = None

    async def _fail(self, e: Exception, limit: bool, fatal: bool) -> None:
        self.fails += 1
        self.last_error = str(e)
        log.warning("%s 실패(%d회 연속): %s", self.name, self.fails, e)
        if limit:
            self.quota.exhausted_until_midnight = True
            self._backoff_until = time.time() + timeutil.seconds_until_midnight() + 5
        elif fatal:
            self._backoff_until = time.time() + 3600
        else:
            self._backoff_until = time.time() + min(600, 30 * 2 ** (self.fails - 1))
        if (fatal or limit or self.fails >= ALERT_AFTER_FAILS) and not self._alerted:
            self._alerted = True
            await self.dispatcher.admin(f"{self.name}: API 오류 — {e}")

    async def _is_new(self, key: str) -> bool:
        if await self.storage.is_seen(self.source, key):
            return False
        await self.storage.mark_seen(self.source, key)
        return True

    async def poll(self) -> None:
        raise NotImplementedError


class EqkPoller(Poller):
    name = "지진정보(기상청)"
    source = "eqk"

    # 수동 조회 명령이 이 값보다 최근 캐시가 있으면 API 를 호출하지 않음
    CACHE_TTL = 120

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.cache: list[EqkReport] = []
        self.cache_at: float = 0.0
        self._fetch_lock = asyncio.Lock()

    async def _fetch(self) -> list[EqkReport]:
        reports = await apis.fetch_eqk(self.session, self.key)
        self.cache, self.cache_at = reports, time.time()
        return reports

    async def recent_reports(self) -> tuple[list[EqkReport], float, bool]:
        """수동 조회용. (통보 목록, 조회 시각, 최신 여부).
        캐시가 오래됐으면 예비분 안에서 1회 호출하고, 실패/한도 부족 시 캐시를 반환."""
        async with self._fetch_lock:
            if time.time() - self.cache_at <= self.CACHE_TTL:
                return self.cache, self.cache_at, True
            if self.key and await self.quota.acquire(burst=True):
                try:
                    await self._fetch()
                    return self.cache, self.cache_at, True
                except Exception as e:      # noqa: BLE001
                    log.warning("수동 지진정보 조회 실패: %s", e)
            return self.cache, self.cache_at, False

    def burst_for(self, ev: EqkEvent) -> None:
        """상세정보가 들어올 때까지 20초 간격, 최대 10분."""
        self.burst(600, 20, done=lambda: ev.has_detail)

    async def poll(self) -> None:
        async with self._fetch_lock:
            reports = list(await self._fetch())
        reports.sort(key=lambda r: (r.tm_fc.timestamp() if r.tm_fc else 0, r.tm_seq))
        now = timeutil.now()
        for r in reports:
            if not await self._is_new(r.key):
                continue
            if r.tm_fc and now - r.tm_fc > self.stale * 2:
                continue        # 재시작 등으로 놓친 오래된 통보는 공지하지 않음
            log.info("새 지진통보: %s %s M%s", r.kind, r.loc, r.mt)
            await self.dispatcher.handle_eqk(r)


class DisasterPoller(Poller):
    name = "재난문자(행안부)"
    source = "disaster"
    ROWS = 50

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._ascending: bool | None = None    # API 정렬 순서 (첫 응답에서 감지)
        self._prev_day_checked: str | None = None

    def burst_short(self) -> None:
        """국내 유감지진 발생 시 재난문자를 빨리 받기 위해 30초 간격 4분."""
        self.burst(240, 30)

    async def poll(self) -> None:
        today = timeutil.ymd()
        msgs = await self._fetch_latest(today)
        now = timeutil.now()
        # 자정 직후 1회: 전날 23시대 마지막 문자 누락 방지
        if now.hour == 0 and self._prev_day_checked != today and \
                await self.quota.acquire(burst=True):
            self._prev_day_checked = today
            msgs += await self._fetch_latest(timeutil.ymd(now - timedelta(days=1)))
        await self._process(msgs)

    async def _fetch_latest(self, crt_dt: str) -> list[DisasterMsg]:
        msgs, total = await apis.fetch_disaster_page(self.session, self.key, crt_dt, 1, self.ROWS)
        if self._ascending is None and len(msgs) >= 2:
            self._ascending = _sn(msgs[0]) < _sn(msgs[-1])
            log.info("재난문자 API 정렬: %s", "오래된순" if self._ascending else "최신순")
        if self._ascending and total > self.ROWS:
            # 오래된순이면 마지막 페이지에 최신 문자가 있다 (추가 호출 1회)
            last = (total + self.ROWS - 1) // self.ROWS
            if await self.quota.acquire(burst=True):
                more, _ = await apis.fetch_disaster_page(
                    self.session, self.key, crt_dt, last, self.ROWS)
                msgs = more
        return msgs

    async def _process(self, msgs: list[DisasterMsg]) -> None:
        msgs.sort(key=_sn)
        now = timeutil.now()
        for m in msgs:
            if not await self._is_new(m.sn):
                continue
            if m.created and now - m.created > self.stale:
                continue
            log.info("새 재난문자 SN=%s [%s/%s] %s", m.sn, m.step, m.category, m.regions[:40])
            await self.dispatcher.handle_disaster(m)


def _sn(m: DisasterMsg):
    try:
        return (0, int(m.sn))
    except ValueError:
        return (1, m.sn)
