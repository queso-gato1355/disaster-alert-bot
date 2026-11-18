"""공공 API 클라이언트.

인증키는 이미 URL 인코딩된 값을 쓰므로 aiohttp 가 다시 인코딩하지 않도록
URL 문자열을 직접 만들고 yarl.URL(encoded=True) 로 넘긴다.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from urllib.parse import quote

import aiohttp
from yarl import URL

from . import timeutil
from .models import DisasterMsg, EqkReport

log = logging.getLogger(__name__)

KMA_URL = "https://apis.data.go.kr/1360000/EqkInfoService/getEqkMsg"
SAFETY_URL = "https://www.safetydata.go.kr/V2/api/DSSP-IF-00247"

# 이 코드들은 재시도해도 소용없음 → 길게 쉬어야 함
FATAL_CODES = {"12", "20", "21", "30", "31", "32", "33"}
LIMIT_CODE = "22"
NODATA_CODE = "03"


class ApiError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES

    @property
    def limit_exceeded(self) -> bool:
        return self.code == LIMIT_CODE


def _build(base: str, key: str, params: dict) -> URL:
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return URL(f"{base}?serviceKey={key}&{qs}", encoded=True)


async def _get_json(session: aiohttp.ClientSession, url: URL) -> dict:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        text = await resp.text()
        if resp.status >= 500:
            raise ApiError("HTTP", f"HTTP {resp.status}")
    try:
        return json.loads(text)
    except ValueError:
        # data.go.kr 게이트웨이 오류는 JSON 요청이어도 XML 로 옴
        code = re.search(r"<returnReasonCode>(\d+)</returnReasonCode>", text) or \
            re.search(r"<resultCode>(\d+)</resultCode>", text)
        msg = re.search(r"<returnAuthMsg>([^<]+)</returnAuthMsg>", text) or \
            re.search(r"<resultMsg>([^<]+)</resultMsg>", text)
        raise ApiError(code.group(1) if code else "PARSE",
                       msg.group(1) if msg else text[:200]) from None


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 기상청 지진
async def fetch_eqk(session: aiohttp.ClientSession, key: str, rows: int = 100,
                    days: int = 3) -> list[EqkReport]:
    """최근 days 일(기상청은 최대 3일 제공)의 지진통보. 최신순."""
    today = timeutil.now()
    url = _build(KMA_URL, key, {
        "pageNo": 1,
        "numOfRows": rows,
        "dataType": "JSON",
        "fromTmFc": timeutil.ymd(today - timedelta(days=days - 1)),
        "toTmFc": timeutil.ymd(today),
    })
    data = await _get_json(session, url)
    header = data.get("response", {}).get("header", {})
    code = str(header.get("resultCode", ""))
    if code == NODATA_CODE:
        return []
    if code != "00":
        raise ApiError(code, header.get("resultMsg", ""))

    items = (data["response"].get("body") or {}).get("items") or {}
    items = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(items, dict):
        items = [items]

    out = []
    for it in items:
        raw_eqk = str(it.get("tmEqk", ""))
        out.append(EqkReport(
            fc_tp=int(it.get("fcTp", 0) or 0),
            tm_fc=timeutil.parse(it.get("tmFc")),
            tm_seq=int(it.get("tmSeq", 0) or 0),
            tm_eqk=timeutil.parse(raw_eqk[:14]),
            tm_eqk_raw=raw_eqk,
            lat=_f(it.get("lat")),
            lon=_f(it.get("lon")),
            loc=str(it.get("loc") or ""),
            mt=_f(it.get("mt")),
            dep=_f(it.get("dep")),
            intensity=str(it.get("inT") or ""),
            img=str(it.get("img") or ""),
            rem=str(it.get("rem") or ""),
        ))
    return out


# ---------------------------------------------------------------- 행안부 재난문자
# 실제 응답 (2026-11-18 확인):
# {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE", "errorMsg": null},
#  "numOfRows": 3, "pageNo": 1, "totalCount": 15,
#  "body": [{"SN": 270101, "CRT_DT": "2026/11/18 12:00:20", "REG_YMD": "2026/11/18 12:10:10.000000000",
#            "MSG_CN": "...", "RCPTN_RGN_NM": "강원특별자치도  ,경기도  ,...", "EMRG_STEP_NM": "안전안내",
#            "DST_SE_NM": "한파", ...}]}
# - crtDt 는 '그 날짜 이후' 조건 (전날을 주면 오늘 문자도 포함)
# - 정렬 순서가 일정하지 않음 (SN/시각 순 아님) → 전체를 읽어 SN 으로 중복 제거
# - REG_YMD(API 반영 시각)가 CRT_DT(발송 시각)보다 약 10분 늦음
# - 데이터 없음: resultCode 00 + totalCount 0 / 키 오류: resultCode 30
def _safety_items(data: dict) -> tuple[list[dict], int]:
    header = data.get("header") or {}
    code = str(header.get("resultCode", "00"))
    if code == NODATA_CODE:
        return [], 0
    if code not in ("00", "0"):
        raise ApiError(code, header.get("errorMsg") or header.get("resultMsg", ""))
    body = data.get("body") or []
    if isinstance(body, dict):   # 혹시 {"items": [...]} 형태일 경우
        body = body.get("items") or body.get("item") or []
    total = int(data.get("totalCount") or (header.get("totalCount") or 0) or len(body))
    return list(body), total


def _regions(raw) -> str:
    """'강원특별자치도  ,경기도  ,충청남도 천안시 동남구 ' → '강원특별자치도, 경기도, 충청남도 천안시 동남구'"""
    parts = (" ".join(p.split()) for p in str(raw or "").split(","))
    return ", ".join(dict.fromkeys(p for p in parts if p))


def _to_msg(it: dict) -> DisasterMsg:
    return DisasterMsg(
        sn=str(it.get("SN") or it.get("sn") or ""),
        created=timeutil.parse(it.get("CRT_DT") or it.get("crtDt")),
        content=str(it.get("MSG_CN") or it.get("msgCn") or "").strip(),
        regions=_regions(it.get("RCPTN_RGN_NM") or it.get("rcptnRgnNm")),
        step=str(it.get("EMRG_STEP_NM") or it.get("emrgStepNm") or "").strip(),
        category=str(it.get("DST_SE_NM") or it.get("dstSeNm") or "").strip(),
        registered=timeutil.parse(str(it.get("REG_YMD") or "").split(".")[0] or None),
    )


async def fetch_disaster_page(session: aiohttp.ClientSession, key: str, crt_dt: str,
                              page: int, rows: int) -> tuple[list[DisasterMsg], int]:
    url = _build(SAFETY_URL, key, {
        "pageNo": page, "numOfRows": rows, "returnType": "json", "crtDt": crt_dt,
    })
    data = await _get_json(session, url)
    items, total = _safety_items(data)
    return [_to_msg(i) for i in items if (i.get("SN") or i.get("sn"))], total
