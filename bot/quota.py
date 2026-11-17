"""일일 호출 한도 관리.

- 호출 수는 SQLite 에 KST 날짜별로 저장 → 재시작해도 초기화되지 않음.
- 다음 호출 간격 = 오늘 남은 시간 / (남은 호출 수 - 예비분)
  → 하루 동안 고르게 분배되고 한도 초과가 구조적으로 불가능.
- 예비분은 '집중 호출(burst)' 에만 사용. 하드 바닥(hard_floor)은 절대 쓰지 않음.
"""
from __future__ import annotations

from . import timeutil
from .storage import Storage


class Quota:
    def __init__(self, storage: Storage, api: str, daily_limit: int, reserve: int,
                 min_interval: int, max_interval: int, hard_floor: int = 5):
        self.storage = storage
        self.api = api
        self.limit = daily_limit
        self.reserve = reserve
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.hard_floor = hard_floor
        self.exhausted_until_midnight = False   # API 가 22(한도초과)를 반환한 경우

    async def used(self) -> int:
        return await self.storage.quota_get(self.api, timeutil.ymd())

    async def acquire(self, burst: bool = False) -> bool:
        """호출 1회 사용을 시도. 가능하면 카운트를 올리고 True."""
        if self.exhausted_until_midnight:
            return False
        used = await self.used()
        ceiling = self.limit - self.hard_floor if burst else self.limit - self.reserve
        if used >= ceiling:
            return False
        await self.storage.quota_incr(self.api, timeutil.ymd())
        return True

    async def next_interval(self) -> float:
        used = await self.used()
        remaining = self.limit - self.reserve - used
        secs = timeutil.seconds_until_midnight()
        if remaining <= 0:
            return secs + 1          # 오늘은 끝. 자정 이후 재개
        return max(self.min_interval, min(self.max_interval, secs / remaining))

    def reset_day(self) -> None:
        self.exhausted_until_midnight = False
