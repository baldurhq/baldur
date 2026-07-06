"""Experiment Domain Value Types.

OSS-tier value types for chaos-experiment lifecycle states. Pure enum
with no runtime dependency on PRO modules.
"""

from __future__ import annotations

from enum import Enum


class ExperimentStatus(str, Enum):
    """Experiment lifecycle status."""

    PENDING = "pending"
    """Scheduled but not yet started."""

    AWAITING_APPROVAL = "awaiting_approval"
    """High-risk experiment awaiting manual approval."""

    RUNNING = "running"
    """Currently active."""

    COMPLETED = "completed"
    """Finished successfully."""

    FAILED = "failed"
    """Encountered an error."""

    ABORTED = "aborted"
    """Stopped manually or by safety guard (e.g., Kill Switch)."""

    SKIPPED = "skipped"
    """Skipped (e.g., low error budget)."""

    ROLLED_BACK = "rolled_back"
    """Rolled back due to detected issues."""

    RECOVERY_MONITORING = "recovery_monitoring"
    """Completed but monitoring downstream recovery."""


__all__ = ["ExperimentStatus"]
