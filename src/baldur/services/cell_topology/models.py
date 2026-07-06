"""
Cell 토폴로지 모델.

Cell 상태(CellState)와 Cell 정보(CellInfo)를 정의합니다.
Cell은 논리적 트래픽 격벽으로, DB/Redis/캐시를 공유하며
물리적 데이터 파티셔닝이 아닙니다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar

import structlog

from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class CellState(str, Enum):
    """Cell 상태."""

    ACTIVE = "active"
    """정상 동작 — 트래픽 100% 수신 중."""

    WARMUP = "warmup"
    """예열 중 — 트래픽 점진적 투입 (percentage 기반)."""

    DRAINING = "draining"
    """대피 중 — 신규 트래픽 차단, 기존 요청 완료 대기."""

    ISOLATED = "isolated"
    """격리됨 — 모든 트래픽 차단."""


# Cell 상태 우선순위 (Most Restrictive Wins)
# ISOLATED(3) > DRAINING(2) > WARMUP(1) > ACTIVE(0)
CELL_STATE_PRIORITY: dict[CellState, int] = {
    CellState.ACTIVE: 0,
    CellState.WARMUP: 1,
    CellState.DRAINING: 2,
    CellState.ISOLATED: 3,
}


@dataclass
class CellInfo:
    """Cell 정보."""

    # L1↔L2 sync contract (CLAUDE.md pattern)
    _L2_SYNCED_FIELDS: ClassVar[tuple[str, ...]] = (
        "state",
        "health_score",
        "warmup_percentage",
    )
    """Fields synced to Redis L2 hash."""

    _L2_SYNCED_METADATA: ClassVar[tuple[str, ...]] = (
        "last_state_change",
        "last_state_change_time",
    )
    """Metadata fields synced to Redis L2 hash (meta: prefix)."""

    cell_id: str
    """Cell 식별자. 예: 'cell-0', 'cell-3'."""

    state: CellState = CellState.ACTIVE
    """현재 상태."""

    assigned_services: set[str] = field(default_factory=set)
    """할당된 서비스 목록."""

    health_score: float = 1.0
    """건강도 (0.0~1.0). CellHealthAggregator가 갱신."""

    warmup_percentage: float = 0.0
    """WARMUP 상태일 때 트래픽 투입 비율 (0.0~100.0). ACTIVE일 때는 무시."""

    created_at: datetime = field(default_factory=lambda: utc_now())
    """생성 시각."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """추가 메타데이터."""

    updated_at: float = field(default_factory=time.time)
    """L2 sync timestamp for LWW comparison (time.time())."""

    def to_l2_dict(self) -> dict[str, str]:
        """Serialize synced fields to Redis Hash mapping.

        Returns:
            Dict suitable for redis.hset(mapping=...).
        """
        data: dict[str, str] = {
            "state": self.state.value,
            "health_score": str(self.health_score),
            "warmup_percentage": str(self.warmup_percentage),
            "updated_at": str(self.updated_at),
        }

        # Metadata fields with meta: prefix
        last_change = self.metadata.get("last_state_change")
        if last_change is not None:
            data["meta:last_state_change"] = fast_dumps_str(last_change)

        last_change_time = self.metadata.get("last_state_change_time")
        if last_change_time is not None:
            data["meta:last_state_change_time"] = str(last_change_time)

        return data

    def apply_l2_dict(self, data: dict[str | bytes, str | bytes]) -> bool:  # noqa: C901
        """Apply L2 data to this CellInfo using LWW+MRW hybrid comparison.

        Comparison (Q19):
        1. l2_updated_at > l1_updated_at → LWW wins (accept, enables recovery)
        2. l2_updated_at == l1_updated_at → Most Restrictive Wins (tie-break)
        3. l2_updated_at < l1_updated_at → reject (stale)

        Args:
            data: Redis hgetall() result (may contain str or bytes keys/values).

        Returns:
            True if L1 state was updated, False otherwise.
        """
        # Parse updated_at from L2
        l2_updated_raw = data.get("updated_at") or data.get(b"updated_at")
        if l2_updated_raw is None:
            # Legacy data without updated_at — fall back to MRW only
            return self._apply_mrw_only(data)

        if isinstance(l2_updated_raw, bytes):
            l2_updated_raw = l2_updated_raw.decode()
        try:
            l2_updated_at = float(l2_updated_raw)
        except (ValueError, TypeError):
            return False

        # LWW+MRW hybrid decision
        if l2_updated_at < self.updated_at:
            # Stale L2 data — reject
            return False

        state_str = data.get("state") or data.get(b"state")
        if not state_str:
            return False

        if isinstance(state_str, bytes):
            state_str = state_str.decode()
        new_state = CellState(state_str)

        # Tie — Most Restrictive Wins
        if l2_updated_at == self.updated_at and CELL_STATE_PRIORITY.get(
            new_state, 0
        ) < CELL_STATE_PRIORITY.get(self.state, 0):
            return False

        # Accept L2 data
        self.state = new_state
        self.updated_at = l2_updated_at

        # Sync scalar fields
        health_str = data.get("health_score") or data.get(b"health_score")
        if health_str:
            if isinstance(health_str, bytes):
                health_str = health_str.decode()
            self.health_score = max(0.0, min(1.0, float(health_str)))

        warmup_str = data.get("warmup_percentage") or data.get(b"warmup_percentage")
        if warmup_str:
            if isinstance(warmup_str, bytes):
                warmup_str = warmup_str.decode()
            self.warmup_percentage = max(0.0, min(100.0, float(warmup_str)))

        # Sync metadata fields with safe defaults (Q21)
        self._apply_l2_metadata(data)

        return True

    def _apply_mrw_only(self, data: dict[str | bytes, str | bytes]) -> bool:
        """Fallback for legacy L2 data without updated_at — MRW only."""
        state_str = data.get("state") or data.get(b"state")
        if not state_str:
            return False

        if isinstance(state_str, bytes):
            state_str = state_str.decode()
        new_state = CellState(state_str)

        if CELL_STATE_PRIORITY.get(new_state, 0) < CELL_STATE_PRIORITY.get(
            self.state, 0
        ):
            return False

        self.state = new_state

        health_str = data.get("health_score") or data.get(b"health_score")
        if health_str:
            if isinstance(health_str, bytes):
                health_str = health_str.decode()
            self.health_score = max(0.0, min(1.0, float(health_str)))

        warmup_str = data.get("warmup_percentage") or data.get(b"warmup_percentage")
        if warmup_str:
            if isinstance(warmup_str, bytes):
                warmup_str = warmup_str.decode()
            self.warmup_percentage = max(0.0, min(100.0, float(warmup_str)))

        self._apply_l2_metadata(data)
        return True

    def _apply_l2_metadata(self, data: dict[str | bytes, str | bytes]) -> None:
        """Deserialize metadata fields from L2 with safe defaults (Q21)."""
        # last_state_change — JSON dict
        meta_raw = data.get("meta:last_state_change") or data.get(
            b"meta:last_state_change"
        )
        if meta_raw:
            if isinstance(meta_raw, bytes):
                meta_raw = meta_raw.decode()
            try:
                self.metadata["last_state_change"] = fast_loads(meta_raw)
            except (ValueError, TypeError):
                logger.warning(
                    "cell_info.metadata_deserialize_failed",
                    cell_id=self.cell_id,
                    field="last_state_change",
                )
                self.metadata["last_state_change"] = {}
                self.metadata["last_state_change_time"] = None

        # last_state_change_time — float timestamp
        time_raw = data.get("meta:last_state_change_time") or data.get(
            b"meta:last_state_change_time"
        )
        if time_raw:
            if isinstance(time_raw, bytes):
                time_raw = time_raw.decode()
            try:
                self.metadata["last_state_change_time"] = float(time_raw)
            except (ValueError, TypeError):
                logger.warning(
                    "cell_info.metadata_deserialize_failed",
                    cell_id=self.cell_id,
                    field="last_state_change_time",
                )
                self.metadata["last_state_change_time"] = None
