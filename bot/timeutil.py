from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now() -> datetime:
    return datetime.now(KST)


def ymd(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y%m%d")


_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
    "%Y-%m-%dT%H:%M:%S",
)


def parse(value) -> datetime | None:
    """API 들이 쓰는 여러 시각 포맷을 KST datetime 으로."""
    if value is None:
        return None
    s = str(value).strip()
    if s.isdigit():
        # 숫자만 있는 경우 길이로 포맷을 고정 (strptime 은 한 자리 분/초도 받아들여
        # '202611172314' 를 23:01:04 로 잘못 해석할 수 있음)
        fmt = {14: "%Y%m%d%H%M%S", 12: "%Y%m%d%H%M", 8: "%Y%m%d"}.get(len(s))
        if not fmt:
            return None
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST)
        except ValueError:
            return None
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"
