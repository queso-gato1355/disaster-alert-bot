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
    disaster_off_reason: str   # 꺼져 있을 때 그 이유 (켜져 있으면 빈 문자열)
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


def _disaster_state(safety_key: str) -> tuple[bool, str]:
    """(켜짐 여부, 꺼진 이유)."""
    flag = _env("DISASTER_ENABLED", "true").lower()
    if not safety_key:
        # 'CBS_SERVICE_KEY = 값' 처럼 = 앞에 공백이 있으면 Docker 는 변수 이름에 공백을 포함시킨다
        near = [k for k in os.environ
                if k != "CBS_SERVICE_KEY" and k.strip().upper() == "CBS_SERVICE_KEY"]
        if near:
            return False, (f"CBS_SERVICE_KEY 이름이 잘못됨 ({near[0]!r}) — "
                           "'=' 앞뒤 공백을 지우고 컨테이너를 다시 만드세요")
        if "CBS_SERVICE_KEY" in os.environ:
            return False, "CBS_SERVICE_KEY 값이 비어 있음"
        return False, "CBS_SERVICE_KEY 가 컨테이너 환경변수에 없음"
    if flag not in ("1", "true", "yes", "on"):
        return False, f"DISASTER_ENABLED={flag} 로 꺼져 있음 — 줄을 지우거나 true 로 바꾸세요"
    return True, ""


def load() -> Config:
    admin = _env("ADMIN_CHANNEL_ID")
    safety_key = _env("CBS_SERVICE_KEY")
    enabled, off_reason = _disaster_state(safety_key)
    return Config(
        discord_token=_env("DISCORD_TOKEN"),
        data_go_kr_key=_env("DATA_GO_KR_KEY"),
        safety_key=safety_key,
        disaster_enabled=enabled,
        disaster_off_reason=off_reason,
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
