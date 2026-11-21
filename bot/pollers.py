"""공용 수집기 DB 를 id 커서로 짧게 주기 조회해 새 항목을 알림.

- DB 조회는 공짜이므로 한도/백오프 없이 고정 주기 (재난문자·지진 5초, PEWS 2초).
- 커서(마지막으로 알린 id)는 봇 자신의 DB(cursors 테이블)에 둔다. 처음엔 MAX(id) 에서 시작.
- 알림을 보낸 뒤에 커서를 올린다. 처리 중 예외가 나면 다음 주기에 다시 시도하고,
  같은 행이 MAX_TRIES 번 실패하면 관리자에게 알리고 건너뛴다.
- DB 가 없거나 잠겨 있으면(sqlite3.Error) 이번 회차만 건너뛴다.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import timedelta

from . import safetydb, timeutil
from .dispatcher import Dispatcher
from .storage import Storage

log = logging.getLogger(__name__)

MAX_TRIES = 3


class Poller:
    name = "poller"
    table = ""
    interval = 5.0

    def __init__(self, storage: Storage, dispatcher: Dispatcher, stale_minutes: int):
        self.storage = storage
        self.dispatcher = dispatcher
        self.stale = timedelta(minutes=stale_minutes)
        self.cursor: int | None = None
        # 상태 표시용
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self._fail_id: int | None = None
        self._fail_n = 0

    # ---------------------------------------------------------- 하위 클래스
    def fetch(self, last_id: int) -> list[dict]:
        raise NotImplementedError

    async def handle(self, row: dict) -> None:
        raise NotImplementedError

    # ---------------------------------------------------------- loop
    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                if self.last_error:
                    log.info("%s: 수집기 DB 조회 정상화", self.name)
                self.last_ok, self.last_error = time.time(), None
            except sqlite3.Error as e:          # DB 없음/잠김/권한 → 다음 주기에 다시
                if self.last_error != str(e):
                    log.warning("%s: 수집기 DB 조회 실패 (다음 주기에 재시도): %s", self.name, e)
                self.last_error = str(e)
            except Exception as e:              # noqa: BLE001 - 루프는 죽지 않아야 함
                log.exception("%s: 예상치 못한 오류", self.name)
                self.last_error = str(e)
            await asyncio.sleep(self.interval)

    async def _load_cursor(self) -> int:
        saved = await self.storage.cursor_get(self.table)
        if saved is None:
            saved = await asyncio.to_thread(safetydb.max_id, self.table)
            await self.storage.cursor_set(self.table, saved)
            log.info("%s: 커서 시작값 %d (MAX(id), 지난 항목은 알리지 않음)", self.name, saved)
        return saved

    async def tick(self) -> None:
        if self.cursor is None:
            self.cursor = await self._load_cursor()
        rows = await asyncio.to_thread(self.fetch, self.cursor)
        if not rows:
            # 수집기 DB 를 새로 만들어 id 가 처음부터 다시 시작한 경우
            top = await asyncio.to_thread(safetydb.max_id, self.table)
            if top < self.cursor:
                log.warning("%s: 수집기 DB 의 MAX(id)=%d 가 커서 %d 보다 작음 → 커서 재설정",
                            self.name, top, self.cursor)
                await self._advance(top)
            return
        for row in rows:
            try:
                await self.handle(row)
            except Exception as e:              # noqa: BLE001
                if not await self._handle_failed(row, e):
                    return                      # 다음 주기에 이 행부터 다시
            await self._advance(row["id"])

    async def _advance(self, new_id: int) -> None:
        self.cursor = new_id
        await self.storage.cursor_set(self.table, new_id)

    async def _handle_failed(self, row: dict, e: Exception) -> bool:
        """True = 이 행을 건너뜀."""
        if row["id"] == self._fail_id:
            self._fail_n += 1
        else:
            self._fail_id, self._fail_n = row["id"], 1
        log.exception("%s: id=%s 처리 실패 (%d/%d)", self.name, row["id"], self._fail_n, MAX_TRIES)
        if self._fail_n < MAX_TRIES:
            return False
        await self.dispatcher.admin(f"{self.name}: id={row['id']} 처리가 {MAX_TRIES}회 실패해 건너뜀 — {e}")
        return True


class DisasterPoller(Poller):
    name = "재난문자(행안부)"
    table = "cbs"

    def fetch(self, last_id: int) -> list[dict]:
        return safetydb.new_cbs(last_id)

    async def handle(self, row: dict) -> None:
        m = safetydb.to_msg(row)
        # 새 문자 판별은 id 커서로 하고, SN 은 혹시 모를 중복(이전 API 방식 기록 포함)만 막는다
        if m.sn and await self.storage.is_seen("disaster", m.sn):
            return
        t = m.available_at
        if t and timeutil.now() - t > self.stale:
            log.info("오래된 재난문자 건너뜀 SN=%s (%s)", m.sn, timeutil.fmt(t))
        else:
            log.info("새 재난문자 SN=%s [%s/%s] %s", m.sn, m.step, m.category, m.regions[:40])
            await self.dispatcher.handle_disaster(m)
        if m.sn:
            await self.storage.mark_seen("disaster", m.sn)


class EqkPoller(Poller):
    name = "지진정보(기상청)"
    table = "eqk_reports"

    def fetch(self, last_id: int) -> list[dict]:
        return safetydb.new_quake_reports(last_id)

    async def handle(self, row: dict) -> None:
        r = safetydb.to_report(row)
        if r.tm_fc and timeutil.now() - r.tm_fc > self.stale * 2:
            log.info("오래된 지진통보 건너뜀 %s %s", r.kind, timeutil.fmt(r.tm_fc))
            return
        log.info("새 지진통보: %s %s M%s", r.kind, r.loc, r.mt)
        await self.dispatcher.handle_eqk(r)


class PewsPoller(Poller):
    name = "PEWS(기상청 실시간)"
    table = "pews_events"
    interval = 2.0

    def fetch(self, last_id: int) -> list[dict]:
        return safetydb.new_pews(last_id)

    async def handle(self, row: dict) -> None:
        r = safetydb.pews_to_report(row)
        t = r.tm_eqk or r.tm_fc
        if t and timeutil.now() - t > self.stale:
            log.info("오래된 PEWS 감지 건너뜀 %s", timeutil.fmt(t))
            return
        log.info("PEWS 감지: phase=%s %s M%s %s", row.get("phase"), r.loc, r.mt, r.intensity)
        await self.dispatcher.handle_eqk(r)


class CollectorWatch:
    """수집기 heartbeat 감시. 멈춤/복구를 관리자 채널에 한 번씩만 알림."""
    CHECK = 30
    LAG_MAX = 300          # heartbeat 가 이보다 오래되면 멈춤
    NONE_MAX = 600         # heartbeat 를 못 읽는 상태가 이만큼 이어지면 멈춤

    def __init__(self, dispatcher: Dispatcher):
        self.dispatcher = dispatcher
        self.lag: float | None = None
        self.error: str | None = None
        self.alerted = False
        self._none_since: float | None = None

    @property
    def healthy(self) -> bool:
        return self.lag is not None and self.lag <= self.LAG_MAX

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:                   # noqa: BLE001
                log.exception("수집기 감시 오류")
            await asyncio.sleep(self.CHECK)

    async def check(self) -> None:
        try:
            self.lag = await asyncio.to_thread(safetydb.collector_lag_seconds)
            self.error = None
        except sqlite3.Error as e:
            self.lag, self.error = None, str(e)
        now = time.time()
        if self.lag is None:
            self._none_since = self._none_since or now
            bad = now - self._none_since > self.NONE_MAX
        else:
            self._none_since = None
            bad = self.lag > self.LAG_MAX
        if bad and not self.alerted:
            self.alerted = True
            why = (f"heartbeat {self.lag / 60:.0f}분 전" if self.lag is not None
                   else f"heartbeat 를 읽을 수 없음 ({self.error or '값 없음'})")
            await self.dispatcher.admin(f"⚠️ 수집기(safety-collector) 멈춤 — {why}. 새 재난문자·지진 알림이 오지 않습니다.")
        elif self.alerted and self.healthy:
            self.alerted = False
            await self.dispatcher.admin("✅ 수집기(safety-collector) 복구됨")
