"""Canary Domain Value Types.

OSS-tier value types for Canary rollout state and per-stage promotion
criteria. Runtime-instantiated DTOs/enums that must be available on
OSS-only installs (e.g., LiveCanaryEvaluator constructs PassCriteria;
runbook/scenario handlers compare CanaryState values).

The orchestrator class (CanaryRolloutService) and per-rollout instance
(CanaryRollout) remain PRO-tier — OSS callers reach them via the
Protocols in :mod:`baldur.interfaces.canary`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CanaryState(str, Enum):
    """Canary rollout state machine.

    State transitions:
    - CREATED: initial, not yet started
    - CANARY: applied to a subset of clusters
    - PROMOTING: advancing to the next stage
    - PAUSED: temporarily halted (manual or automatic)
    - COMPLETED: fully rolled out to all clusters
    - ROLLED_BACK: rollback finished
    - FAILED: failure state
    - CANCELLED: cancelled by operator
    """

    CREATED = "created"
    CANARY = "canary"
    PROMOTING = "promoting"
    PAUSED = "paused"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LockRenewalOutcome(str, Enum):
    """Outcome of one watchdog-hosted config-lock renewal pass for a rollout.

    Lives beside :class:`CanaryState` so the OSS watchdog and the PRO service
    both import it without a private-boundary crossing.

    Values:
    - RENEWED: the owner still held the lock and its TTL was extended.
    - REACQUIRED: the lock had lapsed under a live, started rollout and was
      re-acquired (closes the post-outage duplicate-create window).
    - CONFLICT: a different rollout already holds the lock — the single-active
      invariant is violated and an alert is warranted.
    - FAILED: renewal could not complete (store error, a re-acquire lost to a
      concurrent create, or the rollout went terminal during the pass).
    - SKIPPED: no store is wired, so there is nothing to renew (degradation).
    """

    RENEWED = "renewed"
    REACQUIRED = "reacquired"
    CONFLICT = "conflict"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PassCriteria:
    """Promotion thresholds for canary stage evaluation.

    Pure threshold DTO consumed by LiveCanaryEvaluator. The evaluator
    owns the comparison logic; this class holds the values only.
    """

    error_rate_absolute_max: float = 0.05
    error_rate_increase_max: float = 0.01

    latency_p95_delta_ms: float = 50.0
    latency_p99_delta_pct: float = 0.2

    error_budget_drain_rate_max: float = 1.2
    error_budget_remaining_min: float = 0.1

    min_requests_required: int = 100
    evaluation_window_seconds: int = 300

    @classmethod
    def for_tier(cls, tier_id: str) -> PassCriteria:
        """Return tier-default PassCriteria for ``critical``/``standard``/``non_essential``."""
        _TIER_DEFAULTS: dict[str, dict] = {
            "critical": {
                "error_budget_drain_rate_max": 0.8,
                "error_budget_remaining_min": 0.15,
                "error_rate_absolute_max": 0.03,
            },
            "standard": {
                "error_budget_drain_rate_max": 1.2,
                "error_budget_remaining_min": 0.10,
                "error_rate_absolute_max": 0.05,
            },
            "non_essential": {
                "error_budget_drain_rate_max": 2.0,
                "error_budget_remaining_min": 0.05,
                "error_rate_absolute_max": 0.10,
            },
        }
        overrides = _TIER_DEFAULTS.get(tier_id, {})
        return cls(**overrides)


@dataclass
class CanaryStage:
    """One stage in a multi-stage canary rollout plan.

    A rollout is composed of stages; each stage applies the new
    configuration to a subset of clusters and waits for the stage
    duration plus pass-criteria evaluation before promoting.
    """

    name: str
    clusters: list[str]
    percentage: float
    duration_minutes: int = 5

    auto_promote: bool = True
    pass_criteria: PassCriteria = field(default_factory=PassCriteria)

    # Legacy fields (kept for backward compatibility; prefer pass_criteria).
    error_rate_threshold: float = 0.05
    latency_increase_threshold: float = 0.5


__all__ = ["CanaryStage", "CanaryState", "LockRenewalOutcome", "PassCriteria"]
