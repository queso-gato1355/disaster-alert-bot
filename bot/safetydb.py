"""공용 수집기(safety-collector) SQLite DB 읽기 전용 조회.

공공 API(재난문자 DSSP-IF-00247, 기상청 EqkInfoService)는 같은 NAS 의 수집기가 혼자 호출해
safety.db(WAL)에 쓴다. 봇은 이 DB 를 읽기만 한다.

규칙
- 항상 `file:...?mode=ro` 로 열고, 쓰기/PRAGMA/VACUUM 은 하지 않는다.
- 매 조회마다 연결을 열고 닫는다 (오래 열린 연결은 WAL 체크포인트를 막음).
- 새 항목은 `id`(DB 에 들어온 순서) 커서로 고른다. 커서는 봇 자신의 DB 에 저장한다.
- 함수는 모두 동기 함수다. 이벤트 루프에서는 asyncio.to_thread 로 부른다 (잠금 대기 최대 10초).
- DB 가 없거나 잠겨 있으면 sqlite3.OperationalError 가 그대로 올라간다 → 호출부가 이번 회차만 건너뜀.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from . import timeutil
from .models import DisasterMsg, EqkReport

DB = "/data/safety.db"      # client 가 설정값(SAFETY_DB)으로 바꿈
TABLES = ("cbs", "eqk_reports", "pews_events")


def _ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def query(sql: str, args: tuple = ()) -> list[dict]:
    # `with con:` 은 트랜잭션만 끝내고 연결을 닫지 않으므로 closing 으로 확실히 닫는다
    with closing(_ro()) as con:
        return [dict(r) for r in con.execute(sql, args)]


def _iso(dt: datetime) -> str:
    """DB 시각 형식 'YYYY-MM-DDTHH:MM:SS+09:00' (문자열 비교 = 시간 비교)."""
    return dt.astimezone(timeutil.KST).replace(microsecond=0).isoformat()


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return timeutil.parse(v)
    return d.astimezone(timeutil.KST) if d.tzinfo else d.replace(tzinfo=timeutil.KST)


# ---------------------------------------------------------------- 커서
def max_id(table: str) -> int:
    """처음 배포할 때 커서 시작값 (지난 항목을 다시 알리지 않도록)."""
    assert table in TABLES
    return query(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}")[0]["m"]


# ---------------------------------------------------------------- 재난문자
_CBS_COLS = "id, sn, sent_at, reg_at, level, category, regions, msg"


def new_cbs(last_id: int) -> list[dict]:
    return query(f"SELECT {_CBS_COLS} FROM cbs WHERE id > ? ORDER BY id", (last_id,))


def recent_cbs(hours: int = 24) -> list[DisasterMsg]:
    """수동 조회용. 발송 시각 최신순."""
    since = _iso(timeutil.now() - timedelta(hours=hours))
    rows = query(f"SELECT {_CBS_COLS} FROM cbs WHERE sent_at >= ? ORDER BY sent_at DESC, id DESC",
                 (since,))
    return [to_msg(r) for r in rows]


def _regions(raw) -> str:
    """'강원특별자치도 ,경기도' → '강원특별자치도, 경기도' (필터/요약 코드가 ', ' 구분을 기대함)"""
    parts = (" ".join(p.split()) for p in str(raw or "").split(","))
    return ", ".join(dict.fromkeys(p for p in parts if p))


def to_msg(r: dict) -> DisasterMsg:
    return DisasterMsg(
        sn=str(r.get("sn") or ""),
        created=_dt(r.get("sent_at")),
        content=str(r.get("msg") or "").strip(),
        regions=_regions(r.get("regions")),
        step=str(r.get("level") or "").strip(),
        category=str(r.get("category") or "").strip(),
        registered=_dt(r.get("reg_at")),
    )


# ---------------------------------------------------------------- 기상청 지진 발표문
# 기존 봇과 같은 출처(data.go.kr getEqkMsg)만 사용
_EQK_COLS = ("id, kind, issued_at, origin_at, lat, lon, depth, mag, place, max_int, remark, url, "
             "domestic, quake_id")
# 수집기의 kind → 기상청 통보종류 코드(fcTp). 알림/렌더링 코드가 fcTp 로 동작하므로 되돌린다.
_KIND_FC = {"조기경보": 11, "속보": 14, "정보": 3, "재통보": 5, "국외": 2}


def new_quake_reports(last_id: int) -> list[dict]:
    return query(f"SELECT {_EQK_COLS} FROM eqk_reports WHERE id > ? AND src = 'data.go.kr' "
                 "ORDER BY id", (last_id,))


def recent_quake_reports(days: int = 3) -> list[EqkReport]:
    """수동 조회용. 최근 days 일 동안 발표된 통보."""
    start = timeutil.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    rows = query(f"SELECT {_EQK_COLS} FROM eqk_reports WHERE src = 'data.go.kr' AND issued_at >= ? "
                 "ORDER BY id", (_iso(start),))
    return [to_report(r) for r in rows]


def _fc_tp(kind: str, domestic: bool) -> int:
    fc = _KIND_FC.get(kind)
    if fc is None:                       # '기타' (예: 조기경보 정밀분석)
        return 13 if domestic else 2
    if fc == 11 and not domestic:
        return 12                        # 국외 조기경보
    return fc


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_report(r: dict) -> EqkReport:
    origin = _dt(r.get("origin_at"))
    return EqkReport(
        fc_tp=_fc_tp(str(r.get("kind") or ""), bool(r.get("domestic"))),
        tm_fc=_dt(r.get("issued_at")),
        tm_seq=int(r.get("id") or 0),
        tm_eqk=origin,
        tm_eqk_raw=origin.strftime("%Y%m%d%H%M%S") if origin else str(r.get("quake_id") or ""),
        lat=_f(r.get("lat")),
        lon=_f(r.get("lon")),
        loc=str(r.get("place") or ""),
        mt=_f(r.get("mag")),
        dep=_f(r.get("depth")),
        intensity=str(r.get("max_int") or ""),
        img=str(r.get("url") or ""),
        rem=str(r.get("remark") or ""),
    )


# ---------------------------------------------------------------- PEWS (비공식, 공식 발표보다 빠름)
_ROMAN = {i + 1: chr(0x2160 + i) for i in range(12)}     # 1 → Ⅰ


def new_pews(last_id: int) -> list[dict]:
    return query(
        "SELECT id, eqk_no, phase, lat, lon, mag, depth, origin_at, max_int, max_area, place, seen_at "
        "FROM pews_events WHERE id > ? ORDER BY id", (last_id,))


def pews_to_report(r: dict) -> EqkReport:
    """PEWS 행 → 자동추정(속보) 통보. 공식 발표가 오면 그쪽이 우선 표시된다 (EqkEvent.best)."""
    origin = _dt(r.get("origin_at"))
    phase = int(r.get("phase") or 2)
    area = " ".join(str(r.get("max_area") or "").split())
    inten = ""
    try:
        n = int(r.get("max_int"))
        if n in _ROMAN:
            inten = f"{'최대 예상진도' if phase == 2 else '최대진도'} {_ROMAN[n]}" + (f"({area})" if area else "")
    except (TypeError, ValueError):
        pass
    return EqkReport(
        fc_tp=14,                                   # 자동추정 → '갱신 예정' 안내가 붙음
        tm_fc=_dt(r.get("seen_at")) or timeutil.now(),
        tm_seq=int(r.get("id") or 0),
        tm_eqk=origin,
        tm_eqk_raw=origin.strftime("%Y%m%d%H%M%S") if origin else str(r.get("eqk_no") or ""),
        lat=_f(r.get("lat")),
        lon=_f(r.get("lon")),
        loc=str(r.get("place") or "") or area,
        mt=_f(r.get("mag")),
        dep=_f(r.get("depth")),
        intensity=inten,
        img="",
        rem="",
        official=False,
        label=f"기상청 PEWS 자동 분석({'속보' if phase == 2 else '지진정보'})",
    )


# ---------------------------------------------------------------- 지진 ↔ 재난문자
def cbs_of_quake(quake_id) -> list[dict]:
    return query(
        "SELECT c.id, c.sn, c.sent_at, c.level, c.regions, c.msg, l.aftershock, l.score "
        "FROM quake_cbs l JOIN cbs c USING(sn) WHERE l.quake_id = ? ORDER BY c.sent_at",
        (quake_id,))


# ---------------------------------------------------------------- 수집기 상태
def meta() -> dict[str, datetime | None]:
    """heartbeat, pews_heartbeat, last_poll:cbs, last_poll:eqk ... → 시각."""
    return {r["k"]: _dt(r["v"]) for r in query("SELECT k, v FROM meta")}


def collector_lag_seconds() -> float | None:
    """수집기 heartbeat 이후 지난 초. 값이 없으면 None. DB 오류는 그대로 올림."""
    rows = query("SELECT v FROM meta WHERE k = 'heartbeat'")
    hb = _dt(rows[0]["v"]) if rows else None
    if hb is None:
        return None
    return (timeutil.now() - hb).total_seconds()
