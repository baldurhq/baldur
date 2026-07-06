"""Settings Recommendation data models.

RecommendationItem and RecommendationPlan are the core value objects
for the recommendation pipeline. Both inherit SerializableMixin for
Redis persistence via PlanStore.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.core.constraint_engine import ConstraintResult
    from baldur.interfaces.cache_provider import CacheProviderInterface

__all__ = [
    "CanaryStageConfig",
    "PlanStore",
    "RecommendationItem",
    "RecommendationPlan",
    "RecommendationSource",
    "RecommendationStatus",
]


class RecommendationSource(str, Enum):
    """Where the recommendation originated."""

    RULE_BASED = "rule_based"
    ML_ANOMALY = "ml_anomaly"
    ML_FORECAST = "ml_forecast"
    ML_OPTIMIZATION = "ml_optimization"
    DEPENDENCY_CASCADE = "dependency_cascade"
    PROFILE_PRESET = "profile_preset"


class RecommendationStatus(str, Enum):
    """Lifecycle status of a recommendation plan."""

    GENERATED = "generated"
    VALIDATING = "validating"
    VALIDATED = "validated"
    REJECTED = "rejected"
    DEPLOYING = "deploying"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


@dataclass
class CanaryStageConfig:
    """Canary rollout stage configuration.

    Used as element type in SettingsRecommendationSettings.canary_stages.
    @model_validator enforces percentage ascending, last=100, duration>=0.
    """

    percentage: int  # 1~100, ascending order required
    duration_minutes: int  # >= 0, last stage typically 0


@dataclass
class RecommendationItem(SerializableMixin):
    """Single parameter change recommendation."""

    parameter: str
    current_value: float
    recommended_value: float
    source: RecommendationSource
    confidence: float  # 0.0 ~ 1.0
    expected_improvement: float  # Percentage
    reason: str
    priority: Any  # AdjustmentPriority (avoid circular import)
    metric_evidence: dict[str, Any] = field(default_factory=dict)
    is_cascade: bool = False  # True if from DependencyGraph propagation


@dataclass
class RecommendationPlan(SerializableMixin):
    """Aggregate of recommendations to apply together.

    Nested RecommendationItem list is recursively serialized.
    ConstraintResult is omitted when None (exclude_none=True).
    """

    plan_id: str  # UUID
    items: list[RecommendationItem]
    status: RecommendationStatus = RecommendationStatus.GENERATED
    created_at: datetime = field(default_factory=utc_now)
    overall_confidence: float = 0.0
    constraint_result: ConstraintResult | None = None
    shadow_evaluation_id: str | None = None
    canary_rollout_id: str | None = None
    applied_at: datetime | None = None
    feedback: dict[str, Any] = field(default_factory=dict)

    exclude_none: ClassVar[bool] = True

    @property
    def parameter_count(self) -> int:
        return len(self.items)

    @property
    def has_ml_items(self) -> bool:
        return any(
            item.source
            in (
                RecommendationSource.ML_ANOMALY,
                RecommendationSource.ML_FORECAST,
                RecommendationSource.ML_OPTIMIZATION,
            )
            for item in self.items
        )


class PlanStore:
    """Leader-aware plan storage with Redis persistence."""

    def __init__(
        self,
        cache_provider: CacheProviderInterface | None = None,
        max_plans: int = 200,
    ):
        self._memory: dict[str, RecommendationPlan] = {}
        self._order: deque[str] = deque(maxlen=max_plans)
        self._cache = cache_provider
        self._max_plans = max_plans

    def save(self, plan: RecommendationPlan, cooldown_seconds: int = 7200) -> None:
        self._memory[plan.plan_id] = plan
        if plan.plan_id not in self._order:
            self._order.append(plan.plan_id)
        # Evict oldest if over capacity
        while len(self._memory) > self._max_plans and self._order:
            oldest = self._order.popleft()
            self._memory.pop(oldest, None)
        if self._cache:
            try:
                self._cache.set(
                    f"recommendation:plan:{plan.plan_id}",
                    plan.to_dict(),
                    ttl=timedelta(seconds=cooldown_seconds * 2),
                )
            except Exception:
                pass  # fail-open: cache write failure is non-fatal

    def get(self, plan_id: str) -> RecommendationPlan | None:
        if plan_id in self._memory:
            return self._memory[plan_id]
        if self._cache:
            try:
                data = self._cache.get(f"recommendation:plan:{plan_id}")
                if data:
                    plan = RecommendationPlan.from_dict(data)
                    self._memory[plan_id] = plan
                    return plan
            except Exception:
                pass
        return None

    def get_recent_plans(
        self,
        limit: int = 20,
        status: RecommendationStatus | None = None,
    ) -> list[RecommendationPlan]:
        """Get recent plans, newest first."""
        plans = list(reversed(list(self._memory.values())))
        if status is not None:
            plans = [p for p in plans if p.status == status]
        return plans[:limit]

    def load_all_pending(self) -> list[RecommendationPlan]:
        """Restore VALIDATING plans from Redis after leader transition."""
        if not self._cache:
            return []
        restored: list[RecommendationPlan] = []
        for plan_id in list(self._order):
            plan = self.get(plan_id)
            if plan and plan.status == RecommendationStatus.VALIDATING:
                restored.append(plan)
        return restored
