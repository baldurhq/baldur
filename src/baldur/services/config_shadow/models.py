"""
Config Shadow Evaluator Data Models.

Shadow Evaluation entities and comparison-report models.
Simulates the effect of a config change up front, independently of the canary
service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baldur.interfaces.event_journal import JournalEntry  # noqa: F401


class EvaluationStatus(str, Enum):
    """Shadow Evaluation status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class EvaluatorResult:
    """Comparison result of a single evaluator."""

    evaluator_name: str
    passed: bool
    confidence_score: float

    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    candidate_metrics: dict[str, Any] = field(default_factory=dict)
    delta: dict[str, Any] = field(default_factory=dict)

    details: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class EvaluationReport:
    """Simulation comparison report."""

    events_analyzed: int
    time_range_start: datetime
    time_range_end: datetime

    evaluator_results: list[EvaluatorResult] = field(default_factory=list)

    passed: bool = False
    confidence_score: float = 0.0
    summary: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class ShadowEvaluation:
    """A single Shadow Evaluation run."""

    evaluation_id: str
    rollout_id: str | None
    status: EvaluationStatus
    created_at: datetime
    completed_at: datetime | None = None

    config_type: str = ""
    baseline_config: dict[str, Any] = field(default_factory=dict)
    candidate_config: dict[str, Any] = field(default_factory=dict)
    service_name: str = ""
    time_window_hours: int = 336
    region: str = ""

    report: EvaluationReport | None = None
    error_message: str = ""


@dataclass
class SimulationResult:
    """Aggregated CB simulation result."""

    open_count: int = 0
    total_open_seconds: float = 0.0
    avg_recovery_seconds: float = 0.0


@dataclass
class BudgetSimulationResult:
    """Aggregated Error Budget simulation result."""

    total_drain_percent: float = 0.0
    critical_episodes: int = 0
    max_burn_rate_1h: float = 0.0


@dataclass
class EvaluationContext:
    """Unified evaluation context passed to an evaluator.

    The Shadow evaluator uses events; the Live evaluator uses
    time_window_seconds + labels.
    """

    baseline_config: dict[str, Any]
    candidate_config: dict[str, Any]

    # For Shadow (past-event replay)
    events: list[JournalEntry] = field(default_factory=list)

    # For Live (real-time metric queries)
    time_window_seconds: int = 300
    baseline_labels: dict[str, str] = field(default_factory=dict)
    candidate_labels: dict[str, str] = field(default_factory=dict)

    # Common
    service_name: str = ""
