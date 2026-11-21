"""수신한 재난문자/지진통보를 이벤트로 묶고, 서버별 필터를 적용해 Discord 로 발송."""
from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import timedelta
import discord

from . import render, timeutil
from .models import EQK_LEVELS, DisasterMsg, EqkEvent, EqkReport
from .storage import GuildConfig, Storage

log = logging.getLogger(__name__)

STEP_ORDER = ["위급재난", "긴급재난", "안전안내"]
EVENT_TTL = timedelta(hours=2)
# 재난문자 생성시각 기준, 이 범위 안에 발생한 지진이면 같은 지진으로 본다
MATCH_BEFORE = timedelta(minutes=30)
MATCH_AFTER = timedelta(minutes=2)
SAME_QUAKE = timedelta(seconds=90)


# 사용자가 줄임말로 입력해도 재난문자의 정식 지역명과 맞도록
REGION_ALIASES = {
    "경기": ["경기도"], "강원": ["강원도", "강원특별자치도"],
    "충북": ["충청북도"], "충남": ["충청남도"],
    "전북": ["전라북도", "전북특별자치도"], "전남": ["전라남도", "전남광주통합특별시"],
    "경북": ["경상북도"], "경남": ["경상남도"],
    "제주": ["제주특별자치도", "제주도"], "세종": ["세종특별자치시"],
}
_REVERSE = {full: short for short, fulls in REGION_ALIASES.items() for full in fulls}


def _variants(kw: str) -> list[str]:
    out = [kw]
    for full, short in _REVERSE.items():
        if kw.startswith(full):
            out.append(short + kw[len(full):])
            for alt in REGION_ALIASES[short]:
                out.append(alt + kw[len(full):])
    for short, fulls in REGION_ALIASES.items():
        if kw.startswith(short) and not any(kw.startswith(f) for f in fulls):
            out += [f + kw[len(short):] for f in fulls]
    return out


def region_matches(regions: str, keywords: list[str]) -> bool:
    """수신지역 문자열이 키워드 지역을 포함하는지.
    - '경북 상주시' → '경상북도 상주시 ' 처럼 키워드가 수신지역 안에 있거나
    - 수신지역이 상위 지역('충청남도')이라 키워드('충남 천안시')를 덮는 경우 모두 True.
    전국 문자는 '전국'이 아니라 시·도 16~17개가 나열되어 오므로 두 번째 경우가 필요함."""
    if "전국" in regions:
        return True
    parts = [p for p in regions.split(", ") if p]
    for kw in keywords:
        for v in _variants(kw):
            if v in regions or any(v.startswith(p + " ") for p in parts):
                return True
    return False


def msg_passes(g: GuildConfig, m: DisasterMsg) -> bool:
    if m.is_missing_person and not g.include_missing:
        return False
    if m.step and m.step not in g.steps:
        return False
    return not g.regions or region_matches(m.regions, g.regions)


def eqk_passes(g: GuildConfig, r: EqkReport | None) -> bool:
    """재난문자 없이 지진정보만으로 공지할지 (서버 지진 등급 기준).
    등급 1·2 는 국내 관측 최대진도로 판단 — 국외 지진이라도 국내 진도가 기준 이상이면 공지.
    진도가 아직 없는 속보는 상세정보(진도 포함)가 들어오면 그때 공지된다."""
    if r is None or g.eqk_level not in EQK_LEVELS or g.eqk_level == 0:
        return False
    _, min_int = EQK_LEVELS[g.eqk_level]
    if min_int is None:          # 모든 지진
        return True
    inten = r.max_intensity
    return inten is not None and inten >= min_int


class Dispatcher:
    def __init__(self, client: discord.Client, storage: Storage, admin_channel_id: int | None):
        self.client = client
        self.storage = storage
        self.admin_channel_id = admin_channel_id
        self.events: list[EqkEvent] = []
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(10)

    # ------------------------------------------------------------ 입력
    async def handle_disaster(self, m: DisasterMsg) -> None:
        async with self._lock:
            if m.is_earthquake:
                ev = self._event_for_msg(m)
                ev.messages.append(m)
                await self._publish_event(ev)
                return
            targets = [g for g in await self.storage.active_guilds() if msg_passes(g, m)]
            title = f"[{m.step}] {m.category}: {m.content[:60]}"
            await self._fanout(targets, lambda g: self._send(
                g, [render.disaster_embed(m)], self._mention(g, [m], None), "disaster", title))

    async def handle_eqk(self, r: EqkReport) -> None:
        async with self._lock:
            ev = self._event_for_report(r)
            ev.reports.append(r)
            if ev.tm_eqk is None:
                ev.tm_eqk = r.tm_eqk
            await self._publish_event(ev)

    # ------------------------------------------------------------ 이벤트 매칭
    def _gc(self) -> None:
        cutoff = timeutil.now() - EVENT_TTL
        self.events = [e for e in self.events if e.created_at and e.created_at > cutoff]

    def _new_event(self) -> EqkEvent:
        ev = EqkEvent(event_id=next(self._ids), created_at=timeutil.now())
        self.events.append(ev)
        return ev

    def _event_for_msg(self, m: DisasterMsg) -> EqkEvent:
        self._gc()
        t = m.created or timeutil.now()
        for ev in reversed(self.events):
            if ev.tm_eqk and t - MATCH_BEFORE <= ev.tm_eqk <= t + MATCH_AFTER:
                return ev
            # 아직 기상청 정보 없이 지진 문자만 있는 이벤트 (여러 지역 문자)
            if not ev.tm_eqk and ev.messages and ev.messages[-1].created and \
                    abs(t - ev.messages[-1].created) <= timedelta(minutes=10):
                return ev
        return self._new_event()

    def _event_for_report(self, r: EqkReport) -> EqkEvent:
        self._gc()
        for ev in reversed(self.events):
            if ev.tm_eqk and r.tm_eqk and abs(ev.tm_eqk - r.tm_eqk) <= SAME_QUAKE:
                return ev
        if r.domestic and r.tm_eqk:
            for ev in reversed(self.events):
                if not ev.tm_eqk and ev.messages and any(
                    m.created and r.tm_eqk - MATCH_AFTER <= m.created and
                    m.created - MATCH_BEFORE <= r.tm_eqk for m in ev.messages
                ):
                    return ev
        return self._new_event()

    # ------------------------------------------------------------ 발송
    async def _fanout(self, guilds: list[GuildConfig], job) -> None:
        """여러 서버에 동시에 발송 (동시 실행 수 제한)."""
        async def one(g):
            async with self._sem:
                await job(g)
        await asyncio.gather(*(one(g) for g in guilds))

    async def _publish_event(self, ev: EqkEvent) -> None:
        async def deliver(g: GuildConfig):
            msgs = [m for m in ev.messages if msg_passes(g, m)]
            include_eqk = eqk_passes(g, ev.best) or (bool(msgs) and ev.best is not None)
            if not msgs and not include_eqk:
                return
            embeds = render.event_embeds(ev, msgs, include_eqk)
            if g.guild_id in ev.posts:
                await self._edit(ev.posts[g.guild_id], embeds)
                return
            r = ev.best
            title = f"지진 {r.loc} M{r.mt}" if r else f"지진 재난문자: {msgs[0].content[:60]}"
            posted = await self._send(g, embeds, self._mention(g, msgs, r), "eqk", title)
            if posted:
                ev.posts[g.guild_id] = posted
        await self._fanout(await self.storage.active_guilds(), deliver)

    def _mention(self, g: GuildConfig, msgs: list[DisasterMsg], r: EqkReport | None) -> str | None:
        role = None
        steps = {m.step for m in msgs}
        for s in STEP_ORDER:
            if s in steps and s in g.mentions:
                role = g.mentions[s]
                break
        if role is None and r is not None and "지진" in g.mentions:
            role = g.mentions["지진"]
        return f"<@&{role}>" if role else None

    async def _channel(self, channel_id: int):
        ch = self.client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.client.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch

    async def _send(self, g: GuildConfig, embeds: list[discord.Embed], content: str | None,
                    kind: str, title: str) -> tuple[int, int] | None:
        ch = await self._channel(g.channel_id)
        if ch is None:
            log.warning("guild %s: 채널 %s 를 찾을 수 없음", g.guild_id, g.channel_id)
            return None
        try:
            msg = await ch.send(content=content, embeds=embeds,
                                allowed_mentions=discord.AllowedMentions(roles=True))
            if getattr(ch, "is_news", lambda: False)():
                try:
                    await msg.publish()
                except discord.HTTPException as e:
                    log.info("공지 게시(publish) 실패: %s", e)
            await self.storage.log_delivery(g.guild_id, ch.id, msg.id, kind, title)
            return ch.id, msg.id
        except discord.HTTPException as e:
            log.warning("guild %s 발송 실패: %s", g.guild_id, e)
            return None

    async def _edit(self, post: tuple[int, int], embeds: list[discord.Embed]) -> None:
        ch = await self._channel(post[0])
        if ch is None:
            return
        try:
            await ch.get_partial_message(post[1]).edit(embeds=embeds)
        except discord.HTTPException as e:
            log.warning("메시지 수정 실패: %s", e)

    async def admin(self, text: str) -> None:
        log.warning("ADMIN: %s", text)
        if not self.admin_channel_id:
            return
        ch = await self._channel(self.admin_channel_id)
        if ch:
            try:
                await ch.send(f"🛠️ {text}")
            except discord.HTTPException:
                pass
