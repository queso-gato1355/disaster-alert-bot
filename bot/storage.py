"""SQLite 영속 저장소 (다중 서버 지원).

테이블
- guilds          서버별 설정 1행 (채널, 단계, 지진 기준 …)
- guild_regions   서버별 수신지역 키워드 (없으면 전국 수신)
- guild_mentions  서버별 멘션 역할 (kind: 위급재난/긴급재난/안전안내/지진)
- deliveries      서버별 발송 기록 (대시보드 '최근 공지', 통계)
- seen            API 항목 중복 제거 (전 서버 공통)
- quota           API 일일 호출 수 (전 서버 공통 - API 호출은 서버 수와 무관하게 1회)

스키마 버전은 PRAGMA user_version 으로 관리하고 MIGRATIONS 를 순서대로 적용한다.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import aiosqlite

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS seen (
        source   TEXT NOT NULL,              -- 'disaster' | 'eqk'
        item_key TEXT NOT NULL,
        seen_at  REAL NOT NULL,
        PRIMARY KEY (source, item_key)
    );
    CREATE TABLE IF NOT EXISTS quota (
        api   TEXT NOT NULL,
        ymd   TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (api, ymd)
    );
    CREATE TABLE IF NOT EXISTS guilds (
        guild_id         INTEGER PRIMARY KEY,
        name             TEXT NOT NULL DEFAULT '',
        channel_id       INTEGER,            -- NULL = 공지 꺼짐
        step_critical    INTEGER NOT NULL DEFAULT 1,   -- 위급재난
        step_emergency   INTEGER NOT NULL DEFAULT 1,   -- 긴급재난
        step_safety      INTEGER NOT NULL DEFAULT 0,   -- 안전안내
        include_missing  INTEGER NOT NULL DEFAULT 0,   -- 실종알림
        eqk_domestic_min REAL NOT NULL DEFAULT 2.0,
        eqk_overseas_min REAL NOT NULL DEFAULT 6.0,
        created_at       REAL NOT NULL,
        updated_at       REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS guild_regions (
        guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
        region   TEXT NOT NULL,
        PRIMARY KEY (guild_id, region)
    );
    CREATE TABLE IF NOT EXISTS guild_mentions (
        guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
        kind     TEXT NOT NULL,
        role_id  INTEGER NOT NULL,
        PRIMARY KEY (guild_id, kind)
    );
    CREATE TABLE IF NOT EXISTS deliveries (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        kind       TEXT NOT NULL,            -- 'disaster' | 'eqk'
        title      TEXT NOT NULL,
        sent_at    REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_deliveries_guild ON deliveries(guild_id, sent_at DESC);
    """,
]

STEP_COLUMNS = {"위급재난": "step_critical", "긴급재난": "step_emergency", "안전안내": "step_safety"}
_GUILD_COLS = ("guild_id, name, channel_id, step_critical, step_emergency, step_safety, "
               "include_missing, eqk_domestic_min, eqk_overseas_min")


@dataclass
class GuildConfig:
    guild_id: int
    name: str = ""
    channel_id: int | None = None
    steps: list[str] = field(default_factory=lambda: ["위급재난", "긴급재난"])
    include_missing: bool = False
    eqk_domestic_min: float = 2.0
    eqk_overseas_min: float = 6.0
    regions: list[str] = field(default_factory=list)      # 비어 있으면 전국
    mentions: dict[str, int] = field(default_factory=dict)


@dataclass
class Delivery:
    channel_id: int
    message_id: int
    kind: str
    title: str
    sent_at: float


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA journal_mode = WAL")
        async with self.db.execute("PRAGMA user_version") as cur:
            version = (await cur.fetchone())[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.db.executescript(sql)
            await self.db.execute(f"PRAGMA user_version = {i}")
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # ------------------------------------------------------------ seen
    async def is_seen(self, source: str, key: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM seen WHERE source=? AND item_key=?", (source, key)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_seen(self, source: str, key: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO seen(source, item_key, seen_at) VALUES (?,?,?)",
            (source, key, time.time()),
        )
        await self.db.commit()

    async def prune(self, days: int = 7, delivery_days: int = 30) -> None:
        now = time.time()
        await self.db.execute("DELETE FROM seen WHERE seen_at < ?", (now - days * 86400,))
        await self.db.execute("DELETE FROM deliveries WHERE sent_at < ?",
                              (now - delivery_days * 86400,))
        await self.db.execute(
            "DELETE FROM quota WHERE ymd < strftime('%Y%m%d', 'now', ?)", (f"-{days} days",))
        await self.db.commit()

    # ------------------------------------------------------------ quota
    async def quota_get(self, api: str, ymd: str) -> int:
        async with self.db.execute(
            "SELECT count FROM quota WHERE api=? AND ymd=?", (api, ymd)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def quota_incr(self, api: str, ymd: str) -> int:
        await self.db.execute(
            "INSERT INTO quota(api, ymd, count) VALUES (?,?,1) "
            "ON CONFLICT(api, ymd) DO UPDATE SET count = count + 1",
            (api, ymd),
        )
        await self.db.commit()
        return await self.quota_get(api, ymd)

    # ------------------------------------------------------------ guilds
    async def ensure_guild(self, guild_id: int, name: str = "") -> None:
        now = time.time()
        await self.db.execute(
            "INSERT INTO guilds(guild_id, name, created_at, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET name = CASE WHEN excluded.name != '' "
            "THEN excluded.name ELSE guilds.name END",
            (guild_id, name, now, now),
        )
        await self.db.commit()

    async def delete_guild(self, guild_id: int) -> None:
        await self.db.execute("DELETE FROM guilds WHERE guild_id=?", (guild_id,))
        await self.db.commit()

    async def active_guilds(self) -> list[GuildConfig]:
        """공지 채널이 설정된 서버 전체 (지역/멘션 포함)."""
        async with self.db.execute(
            f"SELECT {_GUILD_COLS} FROM guilds WHERE channel_id IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        guilds = {r[0]: self._row(r) for r in rows}
        if not guilds:
            return []
        async with self.db.execute("SELECT guild_id, region FROM guild_regions") as cur:
            for gid, region in await cur.fetchall():
                if gid in guilds:
                    guilds[gid].regions.append(region)
        async with self.db.execute("SELECT guild_id, kind, role_id FROM guild_mentions") as cur:
            for gid, kind, role in await cur.fetchall():
                if gid in guilds:
                    guilds[gid].mentions[kind] = role
        return list(guilds.values())

    async def guild(self, guild_id: int) -> GuildConfig:
        await self.ensure_guild(guild_id)
        async with self.db.execute(
            f"SELECT {_GUILD_COLS} FROM guilds WHERE guild_id=?", (guild_id,)
        ) as cur:
            g = self._row(await cur.fetchone())
        async with self.db.execute(
            "SELECT region FROM guild_regions WHERE guild_id=? ORDER BY region", (guild_id,)
        ) as cur:
            g.regions = [r[0] for r in await cur.fetchall()]
        async with self.db.execute(
            "SELECT kind, role_id FROM guild_mentions WHERE guild_id=?", (guild_id,)
        ) as cur:
            g.mentions = {k: v for k, v in await cur.fetchall()}
        return g

    async def save_guild(self, g: GuildConfig) -> None:
        """설정 전체를 한 트랜잭션으로 저장."""
        await self.ensure_guild(g.guild_id, g.name)
        await self.db.execute(
            "UPDATE guilds SET channel_id=?, step_critical=?, step_emergency=?, step_safety=?, "
            "include_missing=?, eqk_domestic_min=?, eqk_overseas_min=?, updated_at=? "
            "WHERE guild_id=?",
            (g.channel_id, *(int(s in g.steps) for s in STEP_COLUMNS),
             int(g.include_missing), g.eqk_domestic_min, g.eqk_overseas_min, time.time(),
             g.guild_id),
        )
        await self.db.execute("DELETE FROM guild_regions WHERE guild_id=?", (g.guild_id,))
        await self.db.executemany(
            "INSERT OR IGNORE INTO guild_regions(guild_id, region) VALUES (?,?)",
            [(g.guild_id, r) for r in g.regions])
        await self.db.execute("DELETE FROM guild_mentions WHERE guild_id=?", (g.guild_id,))
        await self.db.executemany(
            "INSERT INTO guild_mentions(guild_id, kind, role_id) VALUES (?,?,?)",
            [(g.guild_id, k, v) for k, v in g.mentions.items()])
        await self.db.commit()

    async def clear_channel(self, channel_id: int) -> int | None:
        """채널이 삭제됐을 때 해당 채널을 쓰던 서버의 공지를 끈다."""
        async with self.db.execute(
            "SELECT guild_id FROM guilds WHERE channel_id=?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await self.db.execute(
                "UPDATE guilds SET channel_id=NULL, updated_at=? WHERE guild_id=?",
                (time.time(), row[0]))
            await self.db.commit()
        return row[0] if row else None

    async def count_guilds(self) -> tuple[int, int]:
        async with self.db.execute(
            "SELECT COUNT(*), COUNT(channel_id) FROM guilds"
        ) as cur:
            total, active = await cur.fetchone()
        return total, active

    # ------------------------------------------------------------ deliveries
    async def log_delivery(self, guild_id: int, channel_id: int, message_id: int,
                           kind: str, title: str) -> None:
        await self.db.execute(
            "INSERT INTO deliveries(guild_id, channel_id, message_id, kind, title, sent_at) "
            "VALUES (?,?,?,?,?,?)",
            (guild_id, channel_id, message_id, kind, title[:200], time.time()))
        await self.db.commit()

    async def recent_deliveries(self, guild_id: int, limit: int = 5) -> list[Delivery]:
        async with self.db.execute(
            "SELECT channel_id, message_id, kind, title, sent_at FROM deliveries "
            "WHERE guild_id=? ORDER BY sent_at DESC LIMIT ?", (guild_id, limit)
        ) as cur:
            return [Delivery(*r) for r in await cur.fetchall()]

    async def delivery_count_since(self, guild_id: int, since: float) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE guild_id=? AND sent_at>=?", (guild_id, since)
        ) as cur:
            return (await cur.fetchone())[0]

    @staticmethod
    def _row(r) -> GuildConfig:
        steps = [s for s, flag in zip(STEP_COLUMNS, r[3:6]) if flag]
        return GuildConfig(guild_id=r[0], name=r[1], channel_id=r[2], steps=steps,
                           include_missing=bool(r[6]), eqk_domestic_min=r[7],
                           eqk_overseas_min=r[8])
