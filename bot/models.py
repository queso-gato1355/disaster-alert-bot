from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# 기상청 통보종류(fcTp)
FC_TP = {
    2: "국외 지진정보",
    3: "국내 지진정보",
    5: "국내 지진정보(재통보)",
    11: "국내 지진조기경보",
    12: "국외 지진조기경보",
    13: "조기경보 정밀분석",
    14: "지진속보(조기분석)",
}
DOMESTIC_FC = {3, 5, 11, 13, 14}
DETAILED_FC = {2, 3, 5, 13}      # 수동분석/정밀분석 → 더 이상 기다릴 필요 없음
PRELIMINARY_FC = {11, 12, 14}    # 자동추정 → 상세정보가 뒤따름

# 서버별 지진 공지 등급: 값 → (표시 이름, 최소 국내 진도 / None = 진도 무관)
# 국외 지진도 국내에서 관측된 진도(inT)로 판단한다.
EQK_LEVELS: dict[int, tuple[str, int | None]] = {
    1: ("진도 Ⅴ 이상", 5),
    2: ("진도 Ⅲ 이상", 3),
    3: ("모든 지진", None),
    0: ("끄기", None),
}
EQK_LEVEL_DEFAULT = 2

# 진도(inT) 표기: "최대진도 Ⅳ(경북,충북),Ⅲ(강원,세종,전북)" 처럼 로마숫자. ASCII/아라비아 숫자도 허용
_ROMAN_UNI = {chr(0x2160 + i): i + 1 for i in range(12)}          # Ⅰ..Ⅻ
_ROMAN_ASCII = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
                "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
_INT_RE = re.compile(r"[Ⅰ-Ⅻ]|(?<![A-Za-z])(?:XII|XI|IX|X|VIII|VII|VI|IV|V|III|II|I)(?![A-Za-z])"
                     r"|(?<![\d.])(?:1[0-2]|[1-9])(?![\d.])")


def parse_max_intensity(text: str) -> int | None:
    """진도 문자열에서 가장 큰 진도(1~12)를 뽑는다. 진도 정보가 없으면 None."""
    best = None
    for m in _INT_RE.finditer(text or ""):
        tok = m.group(0)
        v = _ROMAN_UNI.get(tok) or _ROMAN_ASCII.get(tok) or int(tok)
        best = v if best is None else max(best, v)
    return best


@dataclass
class DisasterMsg:
    sn: str
    created: datetime | None
    content: str
    regions: str
    step: str          # 위급재난 / 긴급재난 / 안전안내
    category: str      # 지진, 호우, ...
    registered: datetime | None = None   # API 에 반영된 시각 (발송보다 약 10분 늦음)

    @property
    def available_at(self) -> datetime | None:
        return self.registered or self.created

    @property
    def is_earthquake(self) -> bool:
        return "지진" in self.category or "지진" in self.content[:40]

    @property
    def is_missing_person(self) -> bool:
        """경찰청 실종자 찾기 문자 (예: '[경찰청] … 실종된 ○○씨를 찾습니다')."""
        if "실종" in self.category:
            return True
        c = self.content
        return ("실종" in c or "배회" in c) and ("찾습니다" in c or "경찰" in c or "182" in c)


@dataclass
class EqkReport:
    fc_tp: int
    tm_fc: datetime | None
    tm_seq: int
    tm_eqk: datetime | None
    tm_eqk_raw: str
    lat: float | None
    lon: float | None
    loc: str
    mt: float | None
    dep: float | None
    intensity: str
    img: str
    rem: str

    @property
    def key(self) -> str:
        return f"{self.tm_eqk_raw}-{self.fc_tp}-{self.tm_seq}"

    @property
    def domestic(self) -> bool:
        return self.fc_tp in DOMESTIC_FC

    @property
    def max_intensity(self) -> int | None:
        """국내에서 관측(국외 지진은 국내 영향)된 최대 진도. 없으면 None."""
        return parse_max_intensity(self.intensity)

    @property
    def kind(self) -> str:
        return FC_TP.get(self.fc_tp, f"통보종류 {self.fc_tp}")


def group_quakes(reports: list[EqkReport], window_s: int = 90) -> list["EqkEvent"]:
    """같은 지진(발생시각 ±window_s)의 속보/정보를 하나로 묶어 최신 지진 순으로 반환."""
    events: list[EqkEvent] = []
    for r in sorted(reports, key=lambda x: x.tm_eqk.timestamp() if x.tm_eqk else 0, reverse=True):
        for ev in events:
            if ev.tm_eqk and r.tm_eqk and abs((ev.tm_eqk - r.tm_eqk).total_seconds()) <= window_s:
                ev.reports.append(r)
                break
        else:
            events.append(EqkEvent(event_id=len(events) + 1, tm_eqk=r.tm_eqk, reports=[r]))
    return events


@dataclass
class EqkEvent:
    """한 번의 지진에 대한 모든 정보(기상청 통보 + 재난문자)를 묶음."""
    event_id: int
    tm_eqk: datetime | None = None
    reports: list[EqkReport] = field(default_factory=list)
    messages: list[DisasterMsg] = field(default_factory=list)
    created_at: datetime | None = None
    # guild_id -> (channel_id, message_id)
    posts: dict[int, tuple[int, int]] = field(default_factory=dict)

    @property
    def best(self) -> EqkReport | None:
        """상세(수동분석) 통보 우선, 그 중 가장 최근 발표."""
        if not self.reports:
            return None
        return max(self.reports, key=lambda r: (r.fc_tp in DETAILED_FC, r.tm_fc.timestamp() if r.tm_fc else 0))

    @property
    def has_detail(self) -> bool:
        return any(r.fc_tp in DETAILED_FC for r in self.reports)
