"""GitHub API quota monitoring and automatic twin-mode gating."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from .models import QuotaSnapshot


class QuotaGuard:
    """Tracks upstream GitHub API quota from local state and optional OneDev budget file."""

    def __init__(
        self,
        *,
        budget_file: Path | None = None,
        reserve: int = 50,
        limit: int = 5000,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._budget_file = budget_file
        self._reserve = reserve
        self._limit = limit
        self._clock = clock or (lambda: int(time.time()))
        self._manual: QuotaSnapshot | None = None

    def now(self) -> int:
        return int(self._clock())

    def set_quota(
        self,
        *,
        remaining: int,
        reset_at: int = 0,
        blocked: bool = False,
        source: str = "manual",
    ) -> QuotaSnapshot:
        self._manual = QuotaSnapshot(
            remaining=max(0, int(remaining)),
            limit=self._limit,
            reset_at=max(0, int(reset_at)),
            blocked=bool(blocked),
            source=source,
            reserve=self._reserve,
        )
        return self._manual

    def record_upstream_headers(self, remaining: int | None, reset_at: int | None) -> QuotaSnapshot:
        rem = self._limit if remaining is None else max(0, int(remaining))
        rst = 0 if reset_at is None else max(0, int(reset_at))
        blocked = rem <= self._reserve and (rst == 0 or self.now() < rst)
        return self.set_quota(
            remaining=rem,
            reset_at=rst,
            blocked=blocked,
            source="upstream-headers",
        )

    def current(self) -> QuotaSnapshot:
        now = self.now()
        if self._manual is not None:
            return self._normalize_expired(self._manual, now)
        from_file = self._read_budget_file(now)
        if from_file is not None:
            return from_file
        return QuotaSnapshot(
            remaining=self._limit,
            limit=self._limit,
            reset_at=0,
            blocked=False,
            source="default-open",
            reserve=self._reserve,
        )

    def _normalize_expired(self, snap: QuotaSnapshot, now: int) -> QuotaSnapshot:
        if snap.reset_at > 0 and now >= snap.reset_at and not snap.blocked:
            return QuotaSnapshot(
                remaining=max(snap.remaining, self._limit),
                limit=snap.limit,
                reset_at=snap.reset_at,
                blocked=False,
                source=f"{snap.source}:reset-elapsed",
                reserve=snap.reserve,
            )
        return snap

    def _read_budget_file(self, now: int) -> QuotaSnapshot | None:
        if self._budget_file is None or not self._budget_file.is_file():
            return None
        try:
            payload = json.loads(self._budget_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        remaining = payload.get("remaining")
        reset_at = payload.get("reset_at")
        if not isinstance(remaining, int):
            return None
        rst = int(reset_at) if isinstance(reset_at, int) else 0
        snap = QuotaSnapshot(
            remaining=max(0, remaining),
            limit=self._limit,
            reset_at=rst,
            blocked=False,
            source=f"budget-file:{self._budget_file.name}",
            reserve=self._reserve,
        )
        return self._normalize_expired(snap, now)
