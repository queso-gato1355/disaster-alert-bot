"""Discord Embed 생성."""
from __future__ import annotations

import discord

from . import timeutil
from .models import DisasterMsg, EqkEvent, EqkReport, PRELIMINARY_FC

STEP_STYLE = {
    "위급재난": (0xD32F2F, "🚨"),
    "긴급재난": (0xF57C00, "⚠️"),
    "안전안내": (0xFBC02D, "📢"),
}
KMA_EQK_PAGE = "https://www.weather.go.kr/w/eqk-vol/search/korea.do"


def disaster_embed(m: DisasterMsg) -> discord.Embed:
    color, icon = STEP_STYLE.get(m.step, (0x607D8B, "📢"))
    title = f"{icon} [{m.step or '재난문자'}] {m.category or '재난'}"
    e = discord.Embed(title=title, description=m.content[:4000], color=color,
                      timestamp=m.created)
    if m.regions:
        e.add_field(name="수신지역", value=_clip(region_summary(m.regions), 1024), inline=False)
    e.set_footer(text="행정안전부 재난문자")
    return e


def _mag_color(r: EqkReport) -> int:
    mt = r.mt or 0
    if mt >= 5.0:
        return 0xB71C1C
    if mt >= 4.0:
        return 0xE53935
    if mt >= 3.0:
        return 0xFB8C00
    return 0x43A047


def eqk_embed(r: EqkReport) -> discord.Embed:
    title = f"🌏 {r.kind} — {r.loc}"
    desc = None
    if r.fc_tp in PRELIMINARY_FC:
        desc = "⏳ 자동 추정 정보입니다. 기상청 상세 분석 결과가 나오면 이 메시지가 갱신됩니다."
    e = discord.Embed(title=title[:256], description=desc, color=_mag_color(r),
                      url=KMA_EQK_PAGE, timestamp=r.tm_eqk)
    e.add_field(name="발생시각", value=timeutil.fmt(r.tm_eqk), inline=True)
    e.add_field(name="규모", value=f"**M {r.mt:.1f}**" if r.mt is not None else "-", inline=True)
    e.add_field(name="깊이", value=f"{r.dep:g} km" if r.dep else "-", inline=True)
    if r.lat is not None and r.lon is not None:
        ns = "N" if r.lat >= 0 else "S"
        ew = "E" if r.lon >= 0 else "W"
        e.add_field(name="진앙",
                    value=f"{abs(r.lat):.2f}°{ns}, {abs(r.lon):.2f}°{ew}", inline=True)
    if r.intensity:
        e.add_field(name="진도", value=_clip(r.intensity, 1024), inline=False)
    if r.rem:
        e.add_field(name="참고사항", value=_clip(r.rem, 1024), inline=False)
    if r.img:
        e.set_image(url=r.img)
    e.set_footer(text=f"기상청 · 발표 {timeutil.fmt(r.tm_fc)}")
    return e


def event_embeds(ev: EqkEvent, include_msgs: list[DisasterMsg], include_eqk: bool) -> list[discord.Embed]:
    embeds = [disaster_embed(m) for m in include_msgs[-8:]]
    if include_eqk and ev.best:
        embeds.append(eqk_embed(ev.best))
    elif include_msgs and not ev.best:
        embeds.append(discord.Embed(
            description="🔎 기상청 지진 상세정보를 조회 중입니다… (확인되면 자동으로 추가됩니다)",
            color=0x9E9E9E))
    return embeds


def region_summary(regions: str) -> str:
    """전국 대상 문자는 시·도 16~17개가 나열되므로 '전국'으로 줄임."""
    parts = [p for p in regions.split(", ") if p]
    if len(parts) >= 15 and all(" " not in p for p in parts):
        return f"전국 ({len(parts)}개 시·도)"
    return regions


def disaster_list_embed(msgs: list[DisasterMsg], title: str, empty_text: str) -> discord.Embed:
    e = discord.Embed(title=title, color=0x546E7A)
    if not msgs:
        e.description = empty_text
        return e
    top = next((s for s in STEP_STYLE if any(m.step == s for m in msgs)), None)
    if top:
        e.colour = STEP_STYLE[top][0]
    text, shown = "", 0
    for m in msgs:
        icon = STEP_STYLE.get(m.step, (0, "📢"))[1]
        when = m.created.strftime("%H:%M") if m.created else "--:--"
        body = " ".join(m.content.split())
        ln = (f"{icon} `{when}` **{m.category or '재난'}** · {_clip(region_summary(m.regions), 40)}\n"
              f"　{_clip(body, 110)}\n")
        if len(text) + len(ln) > 3900:
            break
        text += ln
        shown += 1
    if shown < len(msgs):
        text += f"… 외 {len(msgs) - shown}건"
    e.description = text
    counts = {s: sum(1 for m in msgs if m.step == s) for s in STEP_STYLE}
    e.add_field(name="합계", value=" · ".join(f"{s} {n}건" for s, n in counts.items() if n)
                or f"{len(msgs)}건", inline=False)
    return e


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _intensity_short(r: EqkReport) -> str:
    # "최대진도 Ⅳ(경북,충북),Ⅲ(...)" → "최대진도 Ⅳ"
    s = r.intensity.split("(")[0].split(",")[0].strip()
    return f" · {s}" if s else ""


def eqk_list_embed(events: list[EqkEvent], title: str, empty_text: str) -> discord.Embed:
    e = discord.Embed(title=title, url=KMA_EQK_PAGE, color=0x1E88E5)
    lines = []
    for ev in events:
        r = ev.best
        flag = "🇰🇷" if r.domestic else "🌐"
        when = r.tm_eqk.strftime("%m/%d %H:%M") if r.tm_eqk else "--"
        mt = f"M{r.mt:.1f}" if r.mt is not None else "M?"
        lines.append(f"`{when}` {flag} **{mt}** {r.loc}{_intensity_short(r)}")
    if lines:
        text, shown = "", 0
        for ln in lines:
            if len(text) + len(ln) + 1 > 3900:
                break
            text += ln + "\n"
            shown += 1
        if shown < len(lines):
            text += f"… 외 {len(lines) - shown}건"
        e.description = text
        e.colour = _mag_color(max((ev.best for ev in events), key=lambda r: r.mt or 0))
        dom = sum(1 for ev in events if ev.best.domestic)
        e.add_field(name="합계", value=f"국내 {dom}건 · 국외 {len(events) - dom}건", inline=True)
        strongest = max((ev.best for ev in events), key=lambda r: r.mt or 0)
        mt = f"M{strongest.mt:.1f}" if strongest.mt is not None else "M?"
        e.add_field(name="최대 규모", value=f"{mt} — {strongest.loc}"[:1024], inline=True)
        # 가장 최근 지진의 진앙 지도를 큰 이미지로 (썸네일은 너무 작게 표시됨)
        latest = next((ev.best for ev in events if ev.best.img), None)
        if latest:
            when = latest.tm_eqk.strftime("%m/%d %H:%M") if latest.tm_eqk else ""
            lmt = f"M{latest.mt:.1f}" if latest.mt is not None else "M?"
            e.add_field(name="🗺️ 아래 지도", value=f"가장 최근 지진 · `{when}` {lmt} {latest.loc}"[:1024],
                        inline=False)
            e.set_image(url=latest.img)
    else:
        e.description = empty_text
    return e
