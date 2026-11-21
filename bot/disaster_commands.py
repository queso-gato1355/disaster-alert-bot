"""/재난문자 — 관리자가 최근 재난문자를 채널에 공개 공지 (공용 수집기 DB 에서 최근 24시간 조회)."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import render, safetydb, timeutil
from .dispatcher import region_matches
from .models import DisasterMsg

if TYPE_CHECKING:
    from .client import AlertBot

log = logging.getLogger(__name__)


def _region_match(m: DisasterMsg, keyword: str | None) -> bool:
    if not keyword:
        return True
    return region_matches(m.regions, [k.strip() for k in keyword.split(",") if k.strip()])


def build_disaster_commands(bot: "AlertBot") -> app_commands.Group:
    grp = app_commands.Group(
        name="재난문자", description="최근 행정안전부 재난문자를 이 채널에 공지합니다",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    async def load(inter: discord.Interaction, 지역: str | None):
        """(문자 목록, 하단 문구) 반환. 사용할 수 없으면 안내 후 None."""
        if not bot.cfg.disaster_enabled:
            await inter.response.send_message("⏸️ 재난문자 기능이 꺼져 있습니다.", ephemeral=True)
            return None
        try:
            recent = await asyncio.to_thread(safetydb.recent_cbs, 24)
            polled = (await asyncio.to_thread(safetydb.meta)).get("last_poll:cbs")
        except sqlite3.Error as e:
            log.warning("수동 재난문자 조회 실패: %s", e)
            await inter.response.send_message(
                "⏳ 지금은 재난문자를 불러올 수 없습니다. 잠시 후 다시 시도해주세요.", ephemeral=True)
            return None
        await inter.response.defer(thinking=True)          # 공개(ephemeral 아님)
        g = await bot.storage.guild(inter.guild_id)
        msgs = [m for m in recent if _region_match(m, 지역)
                and (g.include_missing or not m.is_missing_person)]
        footer = f"행정안전부 재난문자 · {inter.user.display_name} 님 요청"
        if polled:
            age = max(0, int(timeutil.now().timestamp() - polled.timestamp()))
            footer += f" · {age // 60}분 {age % 60}초 전 조회"
        if not bot.watch.healthy:
            footer += " (최신 조회 실패 — 이전 데이터)"
        return msgs, footer

    async def post(inter: discord.Interaction, embeds: list[discord.Embed], title: str) -> None:
        msg = await inter.followup.send(embeds=embeds, wait=True,
                                        allowed_mentions=discord.AllowedMentions.none())
        await bot.storage.ensure_guild(inter.guild_id, inter.guild.name)
        await bot.storage.log_delivery(inter.guild_id, msg.channel.id, msg.id, "manual", title)

    @grp.command(name="최신", description="가장 최근 재난문자 1건")
    @app_commands.describe(지역="지역 필터 (쉼표 구분, 예: 서울,경북 상주시)")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    async def latest(inter: discord.Interaction, 지역: str | None = None):
        res = await load(inter, 지역)
        if not res:
            return
        msgs, footer = res
        if not msgs:
            await inter.followup.send(embed=discord.Embed(
                title="📢 최신 재난문자", color=0x9E9E9E,
                description="최근 24시간 동안 발송된 재난문자가 없습니다."))
            return
        m = msgs[0]
        e = render.disaster_embed(m)
        e.set_footer(text=f"{footer} · 발송 {timeutil.fmt(m.created)}")
        await post(inter, [e], f"[수동] 최신 재난문자 {m.category}: {m.content[:40]}")

    @grp.command(name="오늘", description="오늘 발송된 재난문자 목록")
    @app_commands.describe(지역="지역 필터 (쉼표 구분, 예: 서울,경북 상주시)")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    async def today(inter: discord.Interaction, 지역: str | None = None):
        res = await load(inter, 지역)
        if not res:
            return
        msgs, footer = res
        day = timeutil.now().date()
        msgs = [m for m in msgs if m.created and m.created.date() == day]
        title = f"📋 오늘의 재난문자 ({day:%Y-%m-%d}{', ' + 지역 if 지역 else ''})"
        e = render.disaster_list_embed(msgs, title[:256], "오늘 발송된 재난문자가 없습니다.")
        e.set_footer(text=footer)
        await post(inter, [e], f"[수동] {title}")

    @grp.error
    async def on_error(inter: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await inter.response.send_message(
                f"⏳ 잠시 후 다시 사용해주세요 ({error.retry_after:.0f}초).", ephemeral=True)
            return
        log.exception("재난문자 명령 오류", exc_info=error)
        send = inter.followup.send if inter.response.is_done() else inter.response.send_message
        await send("⚠️ 처리 중 오류가 발생했습니다.", ephemeral=True)

    return grp
