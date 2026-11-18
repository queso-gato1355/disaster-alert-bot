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


@dataclass(frozen=True)
class Config:
    discord_token: str
    data_go_kr_key: str        # 기상청 지진정보 (URL 인코딩된 키)
    safety_key: str            # 행안부 재난문자 (safetydata.go.kr)
    disaster_enabled: bool     # 재난문자 기능 on/off (기본: 키가 있으면 켜짐)
    db_path: str
    admin_channel_id: int | None

    # 일일 한도 / 예비분
    disaster_daily_limit: int
    disaster_reserve: int
    eqk_daily_limit: int
    eqk_reserve: int

    # 폴링 간격 하한/상한(초)
    min_interval: int
    max_interval: int

    # 이 시간보다 오래된 항목은 공지하지 않고 '본 것'으로만 기록(재시작 시 폭주 방지)
    stale_minutes: int


def load() -> Config:
    admin = _env("ADMIN_CHANNEL_ID")
    safety_key = _env("CBS_SERVICE_KEY")
    return Config(
        discord_token=_env("DISCORD_TOKEN"),
        data_go_kr_key=_env("DATA_GO_KR_KEY"),
        safety_key=safety_key,
        disaster_enabled=bool(safety_key) and
        _env("DISASTER_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        db_path=_env("DB_PATH", "data/bot.db"),
        admin_channel_id=int(admin) if admin else None,
        disaster_daily_limit=_int("DISASTER_DAILY_LIMIT", 1000),
        disaster_reserve=_int("DISASTER_RESERVE", 130),
        eqk_daily_limit=_int("EQK_DAILY_LIMIT", 1000),
        eqk_reserve=_int("EQK_RESERVE", 280),
        min_interval=_int("MIN_INTERVAL", 20),
        max_interval=_int("MAX_INTERVAL", 600),
        stale_minutes=_int("STALE_MINUTES", 30),
    )
