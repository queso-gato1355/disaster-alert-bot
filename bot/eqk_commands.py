"""/지진정보 — 관리자가 최근 지진정보를 채널에 공개 공지."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import render, safetydb, timeutil
from .models import group_quakes

if TYPE_CHECKING:
    from .client import AlertBot

log = logging.getLogger(__name__)

SCOPES = [app_commands.Choice(name="국내", value="국내"),
          app_commands.Choice(name="국내+국외", value="전체")]


def build_eqk_commands(bot: "AlertBot") -> app_commands.Group:
    grp = app_commands.Group(
        name="지진정보", description="기상청 최근 지진정보를 이 채널에 공지합니다",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    async def load(inter: discord.Interaction, scope: str):
        """공개 응답으로 defer 후 (지진 목록, 하단 문구) 반환. 실패 시 None."""
        await inter.response.defer(thinking=True)          # 공개(ephemeral 아님)
        try:
            reports = await asyncio.to_thread(safetydb.recent_quake_reports, 3)
            polled = (await asyncio.to_thread(safetydb.meta)).get("last_poll:eqk")
        except sqlite3.Error as e:
            log.warning("수동 지진정보 조회 실패: %s", e)
            await inter.followup.send("⚠️ 기상청 지진정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.")
            return None
        if scope == "국내":
            reports = [r for r in reports if r.domestic]
        events = group_quakes(reports)
        footer = f"기상청 지진정보 · {inter.user.display_name} 님 요청"
        if polled:
            age = max(0, int(timeutil.now().timestamp() - polled.timestamp()))
            footer += f" · {age // 60}분 {age % 60}초 전 조회"
        if not bot.watch.healthy:
            footer += " (최신 조회 실패 — 이전 데이터)"
        return events, footer

    async def post(inter: discord.Interaction, embed: discord.Embed, title: str) -> None:
        msg = await inter.followup.send(embed=embed, wait=True,
                                        allowed_mentions=discord.AllowedMentions.none())
        await bot.storage.ensure_guild(inter.guild_id, inter.guild.name)
        await bot.storage.log_delivery(inter.guild_id, msg.channel.id, msg.id, "manual", title)

    @grp.command(name="최신", description="가장 최근 지진 1건의 상세정보 (진앙 지도 포함)")
    @app_commands.choices(범위=SCOPES)
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    async def latest(inter: discord.Interaction, 범위: app_commands.Choice[str] | None = None):
        scope = 범위.value if 범위 else "국내"
        res = await load(inter, scope)
        if not res:
            return
        events, footer = res
        if not events:
            await inter.followup.send(embed=discord.Embed(
                title="🌏 최신 지진정보", color=0x9E9E9E,
                description=f"최근 3일간 발표된 {'국내 ' if scope == '국내' else ''}지진정보가 없습니다."))
            return
        r = events[0].best
        e = render.eqk_embed(r)
        e.set_footer(text=f"{footer} · 발표 {timeutil.fmt(r.tm_fc)}")
        await post(inter, e, f"[수동] 최신 지진 {r.loc} M{r.mt}")

    async def list_cmd(inter: discord.Interaction, scope: str, days: int):
        res = await load(inter, scope)
        if not res:
            return
        events, footer = res
        today = timeutil.now().date()
        start = today - timedelta(days=days - 1)
        events = [ev for ev in events if ev.tm_eqk and ev.tm_eqk.date() >= start]
        label = "국내" if scope == "국내" else "국내·국외"
        if days == 1:
            title = f"📋 오늘의 지진정보 ({today:%Y-%m-%d}, {label})"
        else:
            title = f"📋 최근 {days}일 지진정보 ({start:%m/%d}~{today:%m/%d}, {label})"
        e = render.eqk_list_embed(events, title, "해당 기간에 발표된 지진정보가 없습니다.")
        e.set_footer(text=footer)
        await post(inter, e, f"[수동] {title}")

    @grp.command(name="오늘", description="오늘 발생한 지진 목록")
    @app_commands.choices(범위=SCOPES)
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    async def today(inter: discord.Interaction, 범위: app_commands.Choice[str] | None = None):
        await list_cmd(inter, 범위.value if 범위 else "전체", 1)

    @grp.command(name="최근", description="최근 3일간 발생한 지진 목록 (기상청 제공 범위)")
    @app_commands.choices(범위=SCOPES)
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    async def recent(inter: discord.Interaction, 범위: app_commands.Choice[str] | None = None):
        await list_cmd(inter, 범위.value if 범위 else "전체", 3)

    @grp.error
    async def on_error(inter: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await inter.response.send_message(
                f"⏳ 잠시 후 다시 사용해주세요 ({error.retry_after:.0f}초).", ephemeral=True)
            return
        log.exception("지진정보 명령 오류", exc_info=error)
        send = inter.followup.send if inter.response.is_done() else inter.response.send_message
        await send("⚠️ 처리 중 오류가 발생했습니다.", ephemeral=True)

    return grp
