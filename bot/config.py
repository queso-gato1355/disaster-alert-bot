"""환경변수 기반 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None) -> str:
    # .env 가 CRLF 로 저장된 경우를 대비해 공백/개행/따옴표 제거
    return (os.getenv(name, default) or "").strip().strip('"').strip("'")


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    discord_token: str
    safety_db: str             # 공용 수집기(safety-collector) SQLite DB — 읽기 전용
    disaster_enabled: bool     # 재난문자 기능 on/off
    pews_enabled: bool         # PEWS 실시간 감지(비공식)로 지진을 공식 발표보다 먼저 공지
    db_path: str               # 봇 자신의 DB (서버 설정, 발송기록, 커서)
    admin_channel_id: int | None

    # 이 시간보다 오래된 항목은 공지하지 않고 건너뜀 (봇이 오래 멈췄다 켜졌을 때 폭주 방지)
    stale_minutes: int


def load() -> Config:
    admin = _env("ADMIN_CHANNEL_ID")
    return Config(
        discord_token=_env("DISCORD_TOKEN"),
        safety_db=_env("SAFETY_DB", "/data/safety.db"),
        disaster_enabled=_bool("DISASTER_ENABLED", True),
        pews_enabled=_bool("PEWS_ENABLED", False),
        db_path=_env("DB_PATH", "data/bot.db"),
        admin_channel_id=int(admin) if admin else None,
        stale_minutes=_int("STALE_MINUTES", 30),
    )
