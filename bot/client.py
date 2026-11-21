"""Discord 클라이언트 + 슬래시 명령어."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

import discord
from discord import app_commands

from . import __version__, render, safetydb, timeutil
from .config import Config
from .dispatcher import Dispatcher
from .disaster_commands import build_disaster_commands
from .eqk_commands import build_eqk_commands
from .models import EQK_LEVELS, DisasterMsg, EqkReport
from .pollers import CollectorWatch, DisasterPoller, EqkPoller, PewsPoller
from .storage import Storage

log = logging.getLogger(__name__)

STEPS = ["위급재난", "긴급재난", "안전안내"]


class AlertBot(discord.Client):
    def __init__(self, cfg: Config):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(cfg.db_path)
        safetydb.DB = cfg.safety_db
        self.dispatcher = Dispatcher(self, self.storage, cfg.admin_channel_id)
        self.eqk_poller = EqkPoller(self.storage, self.dispatcher, cfg.stale_minutes)
        self.disaster_poller = DisasterPoller(self.storage, self.dispatcher, cfg.stale_minutes)
        self.pews_poller = PewsPoller(self.storage, self.dispatcher, cfg.stale_minutes)
        self.watch = CollectorWatch(self.dispatcher)
        self._tasks: list[asyncio.Task] = []
        self.tree.add_command(build_commands(self))
        self.tree.add_command(build_eqk_commands(self))
        self.tree.add_command(build_disaster_commands(self))

    async def setup_hook(self) -> None:
        await self.storage.open()
        log.info("공용 수집기 DB(읽기 전용): %s", self.cfg.safety_db)
        if not cfg_on(self):
            log.warning("재난문자 기능 비활성화: DISASTER_ENABLED=false — 지진정보만 동작")
        if self.cfg.pews_enabled:
            log.info("PEWS 실시간 감지 사용 (공식 발표 전 자동 분석값으로 먼저 공지)")
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("로그인: %s (서버 %d개, v%s, 재난문자 %s)", self.user, len(self.guilds), __version__,
                 "켜짐" if cfg_on(self) else "꺼짐")
        for g in self.guilds:
            await self.storage.ensure_guild(g.id, g.name)
        if not self._tasks:   # 재연결 시 중복 실행 방지
            self._tasks = [asyncio.create_task(self.eqk_poller.run(), name="eqk"),
                           asyncio.create_task(self.watch.run(), name="collector-watch"),
                           asyncio.create_task(self._maintenance(), name="maintenance")]
            if cfg_on(self):
                self._tasks.append(asyncio.create_task(self.disaster_poller.run(), name="disaster"))
            if self.cfg.pews_enabled:
                self._tasks.append(asyncio.create_task(self.pews_poller.run(), name="pews"))

    async def _maintenance(self) -> None:
        while True:
            try:
                await self.storage.prune()
            except Exception:       # noqa: BLE001
                log.exception("오래된 기록 정리 실패")
            await asyncio.sleep(86400)

    # ---- 서버 수명주기 ----
    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("서버 참가: %s (%s)", guild.name, guild.id)
        await self.storage.ensure_guild(guild.id, guild.name)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("서버 탈퇴: %s (%s) — 설정 삭제", guild.name, guild.id)
        await self.storage.delete_guild(guild.id)

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.name != after.name:
            await self.storage.ensure_guild(after.id, after.name)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        gid = await self.storage.clear_channel(channel.id)
        if gid:
            log.warning("서버 %s 의 공지 채널이 삭제되어 공지를 중지함", gid)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.storage.close()
        await super().close()


def cfg_on(bot: "AlertBot") -> bool:
    return bot.cfg.disaster_enabled


def _ago(ts: float | None) -> str:
    if not ts:
        return "없음"
    s = int(time.time() - ts)
    if s < 120:
        return f"{s}초 전"
    if s < 7200:
        return f"{s // 60}분 전"
    return f"{s // 3600}시간 전"


def _onoff(v: bool) -> str:
    return "✅" if v else "❌"


EQK_LEVEL_DESC = {
    1: "국내 진도 Ⅴ 이상 (국외 지진으로 국내 진도 Ⅴ 이상 포함)",
    2: "국내 진도 Ⅲ 이상 (국외 지진으로 국내 진도 Ⅲ 이상 포함)",
    3: "기상청이 발표하는 모든 지진 (국내·국외)",
    0: "꺼짐 (지진 재난문자에 붙는 상세정보만)",
}


def _eqk_level_text(level: int) -> str:
    return EQK_LEVEL_DESC.get(level, f"알 수 없는 등급 {level}")


async def dashboard_embed(bot: AlertBot, guild: discord.Guild) -> discord.Embed:
    g = await bot.storage.guild(guild.id)
    active = g.channel_id is not None
    e = discord.Embed(
        title=f"📊 재난알림 대시보드 — {guild.name}",
        color=0x2E7D32 if active else 0x757575,
        description="🟢 공지 중" if active else "⚪ 공지 꺼짐 — `/재난알림 채널`로 시작하세요",
        timestamp=timeutil.now(),
    )

    # 채널 + 권한 점검
    if active:
        ch = guild.get_channel(g.channel_id)
        if ch is None:
            ch_text = f"<#{g.channel_id}> ⚠️ 채널을 찾을 수 없음"
        else:
            p = ch.permissions_for(guild.me)
            issues = [n for n, ok in (("메시지 보내기", p.send_messages),
                                      ("링크 첨부", p.embed_links)) if not ok]
            if getattr(ch, "is_news", lambda: False)() and not p.manage_messages:
                issues.append("메시지 관리(공지 게시용)")
            ch_text = ch.mention + (" · 📣 공지 채널" if getattr(ch, "is_news", lambda: False)() else "")
            ch_text += f"\n⚠️ 권한 부족: {', '.join(issues)}" if issues else "\n권한 정상"
    else:
        ch_text = "미설정"
    e.add_field(name="공지 채널", value=ch_text, inline=False)

    e.add_field(name="수신 지역",
                value=", ".join(g.regions) if g.regions else "🇰🇷 전국 (필터 없음)", inline=True)
    e.add_field(name="재난문자 단계",
                value="\n".join(f"{_onoff(s in g.steps)} {s}" for s in STEPS)
                + f"\n{_onoff(g.include_missing)} 실종알림", inline=True)
    e.add_field(name="지진정보",
                value=f"**{EQK_LEVELS.get(g.eqk_level, ('?',))[0]}**\n{_eqk_level_text(g.eqk_level)}"
                      + "\n(지진 재난문자엔 항상 상세정보 첨부)", inline=True)
    mentions = "\n".join(f"{k} → <@&{v}>" for k, v in g.mentions.items()) or "없음"
    e.add_field(name="멘션", value=mentions, inline=False)

    recent = await bot.storage.recent_deliveries(guild.id, 5)
    today_start = timeutil.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today_n = await bot.storage.delivery_count_since(guild.id, today_start)
    if recent:
        lines = [f"[{d.title[:50]}](https://discord.com/channels/{guild.id}/{d.channel_id}/{d.message_id})"
                 f" · {_ago(d.sent_at)}" for d in recent]
        e.add_field(name=f"최근 공지 (오늘 {today_n}건)", value="\n".join(lines)[:1024], inline=False)
    else:
        e.add_field(name="최근 공지", value="아직 없음", inline=False)

    # 봇 공통 상태
    lines = []
    try:
        meta = await asyncio.to_thread(safetydb.meta)
    except sqlite3.Error:
        meta = {}
    w = bot.watch
    hb = meta.get("heartbeat")
    w_state = "🟢" if w.healthy else ("🔴" if w.alerted or w.lag is not None else "⚪")
    line = f"{w_state} **수집기** heartbeat {_ago(hb.timestamp() if hb else None)}"
    if w.error:
        line += f"\n　└ {w.error[:100]}"
    lines.append(line)
    if not cfg_on(bot):
        lines.append("⏸️ **재난문자(행안부)** 비활성화 — 지진정보만 공지")
    pollers = [(bot.disaster_poller, "last_poll:cbs")] if cfg_on(bot) else []
    pollers.append((bot.eqk_poller, "last_poll:eqk"))
    if bot.cfg.pews_enabled:
        pollers.append((bot.pews_poller, "pews_heartbeat"))
    for p, key in pollers:
        state = "🟢" if p.last_ok and not p.last_error else ("🔴" if p.last_error else "⚪")
        polled = meta.get(key)
        line = (f"{state} **{p.name}** 수집 {_ago(polled.timestamp() if polled else None)} · "
                f"봇 확인 {_ago(p.last_ok)} · 커서 #{p.cursor if p.cursor is not None else '-'}")
        if p.last_error:
            line += f"\n　└ {p.last_error[:100]}"
        lines.append(line)
    total, active_n = await bot.storage.count_guilds()
    lines.append(f"참여 서버 {total}곳 · 공지 중 {active_n}곳")
    e.add_field(name="봇 상태 (전체 공통)", value="\n".join(lines), inline=False)
    e.set_footer(text="설정 변경: /재난알림 채널 · 지역 · 단계 · 멘션 · 지진")
    return e


class DashboardView(discord.ui.View):
    def __init__(self, bot: AlertBot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="새로고침", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, inter: discord.Interaction, _: discord.ui.Button):
        await inter.response.edit_message(embed=await dashboard_embed(self.bot, inter.guild),
                                          view=self)


def build_commands(bot: AlertBot) -> app_commands.Group:
    grp = app_commands.Group(
        name="재난알림", description="재난알림봇 설정",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    async def reply(inter: discord.Interaction, text: str) -> None:
        await inter.response.send_message(text, ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())

    @grp.command(name="대시보드", description="이 서버의 설정과 봇 상태를 한눈에 봅니다")
    async def dashboard(inter: discord.Interaction):
        await inter.response.send_message(embed=await dashboard_embed(bot, inter.guild),
                                          view=DashboardView(bot), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())

    @grp.command(name="채널", description="재난 공지를 올릴 채널을 지정합니다")
    async def set_channel(inter: discord.Interaction, 채널: discord.TextChannel):
        perms = 채널.permissions_for(inter.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await reply(inter, f"{채널.mention} 에 메시지 보내기/링크 첨부 권한이 필요합니다.")
            return
        g = await bot.storage.guild(inter.guild_id)
        g.name, g.channel_id = inter.guild.name, 채널.id
        await bot.storage.save_guild(g)
        note = " (공지 채널이라 자동 게시(publish)도 됩니다)" if 채널.is_news() else ""
        await reply(inter, f"✅ 공지 채널: {채널.mention}{note}")

    @grp.command(name="해제", description="이 서버의 재난 공지를 중지합니다 (설정은 유지)")
    async def disable(inter: discord.Interaction):
        g = await bot.storage.guild(inter.guild_id)
        g.channel_id = None
        await bot.storage.save_guild(g)
        await reply(inter, "⏹️ 재난 공지를 중지했습니다. `/재난알림 채널`로 다시 켤 수 있습니다.")

    @grp.command(name="지역",
                 description="수신 지역 필터 (쉼표 구분, 예: 서울,경북 상주시 / '전국'으로 해제)")
    async def set_regions(inter: discord.Interaction, 지역: str):
        g = await bot.storage.guild(inter.guild_id)
        g.regions = [] if 지역.strip() in ("전체", "전국", "") else \
            sorted({x.strip() for x in 지역.split(",") if x.strip()})[:25]
        await bot.storage.save_guild(g)
        await reply(inter, f"✅ 수신 지역: {', '.join(g.regions) or '전국 (필터 없음)'}\n"
                           "※ '전국' 대상 문자와 지진 등급 기준을 넘는 지진정보는 필터와 관계없이 공지됩니다.")

    @grp.command(name="단계", description="공지할 재난문자 종류 (지정하지 않은 항목은 그대로 유지)")
    async def set_steps(inter: discord.Interaction, 위급재난: bool | None = None,
                        긴급재난: bool | None = None, 안전안내: bool | None = None,
                        실종알림: bool | None = None):
        g = await bot.storage.guild(inter.guild_id)
        for name, val in zip(STEPS, (위급재난, 긴급재난, 안전안내)):
            if val is True and name not in g.steps:
                g.steps.append(name)
            elif val is False and name in g.steps:
                g.steps.remove(name)
        if 실종알림 is not None:
            g.include_missing = 실종알림
        await bot.storage.save_guild(g)
        await reply(inter, "✅ " + " · ".join(f"{_onoff(s in g.steps)} {s}" for s in STEPS)
                    + f" · {_onoff(g.include_missing)} 실종알림")

    @grp.command(name="멘션", description="종류별로 멘션할 역할 지정 (역할 비우면 해제)")
    @app_commands.choices(종류=[app_commands.Choice(name=s, value=s) for s in STEPS + ["지진"]])
    async def set_mention(inter: discord.Interaction, 종류: app_commands.Choice[str],
                          역할: discord.Role | None = None):
        g = await bot.storage.guild(inter.guild_id)
        if 역할:
            g.mentions[종류.value] = 역할.id
        else:
            g.mentions.pop(종류.value, None)
        await bot.storage.save_guild(g)
        await reply(inter, f"✅ {종류.value} → {역할.mention if 역할 else '멘션 없음'}")

    @grp.command(name="지진", description="재난문자와 별도로 공지할 지진 등급 (국내 진도 기준)")
    @app_commands.choices(등급=[
        app_commands.Choice(name=f"{name} — {EQK_LEVEL_DESC[v]}"[:100], value=v)
        for v, (name, _) in EQK_LEVELS.items()])
    async def set_eqk(inter: discord.Interaction, 등급: app_commands.Choice[int]):
        g = await bot.storage.guild(inter.guild_id)
        g.eqk_level = 등급.value
        await bot.storage.save_guild(g)
        await reply(inter, f"✅ 지진 공지 등급: **{EQK_LEVELS[등급.value][0]}** — "
                           f"{_eqk_level_text(등급.value)}")

    @grp.command(name="미리보기", description="공지 형식 미리보기 (예시 데이터, 나에게만 보임)")
    async def preview(inter: discord.Interaction):
        now = timeutil.now()
        m = DisasterMsg(sn="0", created=now, step="긴급재난", category="지진",
                        regions="경상북도 상주시",
                        content="[기상청] 11-17 23:10 경북 상주시 북서쪽 22km 지역 규모3.7 지진발생"
                                "/낙하물로부터 몸 보호, 진동 멈춘 후 야외 대피하며 여진 주의 (예시)")
        r = EqkReport(fc_tp=3, tm_fc=now, tm_seq=0, tm_eqk=now, tm_eqk_raw="",
                      lat=36.52, lon=127.96, loc="경북 상주시 북서쪽 22km 지역 (예시)", mt=3.6,
                      dep=20, intensity="최대진도 Ⅳ(경북,충북),Ⅲ(강원,세종,전북)",
                      img="http://www.weather.go.kr/w/repositary/image/eqk/img/eqk_img_3_20261117231017.png",
                      rem="예시 데이터입니다.")
        await inter.response.send_message(
            embeds=[render.disaster_embed(m), render.eqk_embed(r)], ephemeral=True)

    return grp
