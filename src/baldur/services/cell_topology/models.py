"""
Cell topology models.

Defines cell state (CellState) and cell information (CellInfo).
A cell is a logical traffic bulkhead that shares DB/Redis/cache — it is not
physical data partitioning.
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
    """Cell state."""

    ACTIVE = "active"
    """Operating normally — receiving 100% of traffic."""

    WARMUP = "warmup"
    """Warming up — traffic ramped in gradually (percentage-based)."""

    DRAINING = "draining"
    """Draining — new traffic blocked, waiting for in-flight requests."""

    ISOLATED = "isolated"
    """Isolated — all traffic blocked."""


# Cell state priority (Most Restrictive Wins)
# ISOLATED(3) > DRAINING(2) > WARMUP(1) > ACTIVE(0)
CELL_STATE_PRIORITY: dict[CellState, int] = {
    CellState.ACTIVE: 0,
    CellState.WARMUP: 1,
    CellState.DRAINING: 2,
    CellState.ISOLATED: 3,
}


@dataclass
class CellInfo:
    """Cell information."""

    # L1<->L2 sync contract (project pattern)
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
    """Cell identifier. e.g. 'cell-0', 'cell-3'."""

    state: CellState = CellState.ACTIVE
    """Current state."""

    assigned_services: set[str] = field(default_factory=set)
    """Assigned services."""

    health_score: float = 1.0
    """Health score (0.0~1.0). Updated by CellHealthAggregator."""

    warmup_percentage: float = 0.0
    """Traffic ramp-in ratio while WARMUP (0.0~100.0). Ignored when ACTIVE."""

    created_at: datetime = field(default_factory=lambda: utc_now())
    """Creation time."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""

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
