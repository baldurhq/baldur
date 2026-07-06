"""Settings Recommendation Service — orchestrates the full recommendation pipeline.

Responsibilities:
- Metrics collection (I/O)
- ML context preparation (I/O)
- Pipeline execution (pure computation delegation)
- Plan validation (ConstraintEngine, Shadow evaluation)
- Plan deployment (Governance, Canary/direct apply)
- Feedback recording
- Leader election + LeaderScheduler integration
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from baldur.services.settings_recommendation.metrics_collector import (
    ClusterMetricsCollector,
)
from baldur.services.settings_recommendation.models import (
    PlanStore,
    RecommendationPlan,
    RecommendationStatus,
)
from baldur.services.settings_recommendation.pipeline import (
    RecommendationPipeline,
)
from baldur.services.settings_recommendation.presets import (
    WORKLOAD_PRESETS,
    WorkloadProfile,
)
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.coordination.scheduler import LeaderScheduler
    from baldur.core.constraint_engine import ConstraintEngine
    from baldur.core.decision_engine import DecisionEngine
    from baldur.core.settings_dependency import SettingsDependencyGraph
    from baldur.interfaces.canary import CanaryRolloutService
    from baldur.interfaces.learning import LearningServiceProtocol
    from baldur.services.config_shadow.service import ShadowEvaluatorService
    from baldur.services.event_bus.bus.models import BaldurEvent

logger = logging.getLogger(__name__)

__all__ = [
    "SettingsRecommendationService",
    "get_settings_recommendation_service",
    "configure_settings_recommendation_service",
    "reset_settings_recommendation_service",
]

STAGE_NAMES = {0: "canary", 1: "staging", 2: "full"}


class SettingsRecommendationService:
    """Orchestrates the full recommendation pipeline.

    Singleton: get_settings_recommendation_service() / reset_settings_recommendation_service()
    """

    def __init__(
        self,
        decision_engine: DecisionEngine | None = None,
        dependency_graph: SettingsDependencyGraph | None = None,
        constraint_engine: ConstraintEngine | None = None,
        shadow_service: ShadowEvaluatorService | None = None,
        canary_service: CanaryRolloutService | None = None,
        learning_service: LearningServiceProtocol | None = None,
        metrics_collector: ClusterMetricsCollector | None = None,
    ) -> None:
        self._decision_engine = decision_engine
        self._dependency_graph = dependency_graph
        self._constraint_engine = constraint_engine
        self._shadow_service = shadow_service
        self._canary_service = canary_service
        self._learning_service = learning_service
        self._metrics_collector = metrics_collector or ClusterMetricsCollector()
        self._settings = self._load_settings()

        # Pipeline (pure computation)
        self._pipeline = RecommendationPipeline(
            decision_engine=decision_engine,
            dependency_graph=dependency_graph,
            mode=self._settings.mode,
            min_confidence=self._settings.min_confidence,
            max_changes_per_cycle=self._settings.max_changes_per_cycle,
        )

        # Plan storage
        self._plan_store = PlanStore(max_plans=self._settings.max_plans)

        # Scheduler (set by register_with_scheduler)
        self._scheduler: LeaderScheduler | None = None
        self._rollback_subscribed = False

        # Adjustment recorder (lazily resolved)
        self._adjustment_recorder = None

        # Paused state
        self._paused = False
        self._pause_reason = ""

    @staticmethod
    def _load_settings() -> Any:
        try:
            from baldur.settings.settings_recommendation import (
                get_settings_recommendation_settings,
            )

            return get_settings_recommendation_settings()
        except Exception:
            # Return a minimal default object
            from types import SimpleNamespace

            return SimpleNamespace(
                enabled=False,
                mode="rule_based",
                auto_apply=False,
                min_confidence=0.7,
                max_changes_per_cycle=5,
                schedule_seconds=3600,
                cooldown_seconds=7200,
                shadow_required=True,
                canary_required=True,
                ml_min_data_points=100,
                ml_objective_metrics=["error_rate", "p99_latency_ms"],
                fallback_to_rules=True,
                max_plans=200,
                pipeline_timeout_seconds=30.0,
                state_save_interval_seconds=600,
                history_grouping_window_seconds=30,
                prediction_steps=3,
                canary_stages=[],
            )

    # === Core API ===

    def generate_recommendations(
        self,
        metrics: dict[str, float] | None = None,
        *,
        force_rules_only: bool = False,
    ) -> RecommendationPlan:
        """Generate a recommendation plan from current metrics."""
        if metrics is None:
            metrics = self._collect_metrics()

        # Prepare contexts
        prediction_context = self._collect_prediction_context()
        ml_context = self._prepare_ml_context()
        ml_context["current_values"] = self._get_current_param_values(ml_context)
        ml_context["prediction_steps"] = self._settings.prediction_steps

        # Override mode if force_rules_only
        if force_rules_only:
            self._pipeline._mode = "rule_based"

        try:
            items, anomaly_context = self._pipeline.run(
                metrics, ml_context, prediction_context
            )
        finally:
            if force_rules_only:
                self._pipeline._mode = self._settings.mode

        # Build plan
        plan = RecommendationPlan(
            plan_id=str(uuid.uuid4()),
            items=items,
            status=RecommendationStatus.GENERATED,
            overall_confidence=(
                sum(i.confidence for i in items) / len(items) if items else 0.0
            ),
            feedback={"anomalies": anomaly_context, "metrics_snapshot": metrics},
        )

        self._plan_store.save(plan, self._settings.cooldown_seconds)

        logger.debug(
            "recommendation.plan_generated",
            extra={
                "plan_id": plan.plan_id,
                "items": plan.parameter_count,
                "confidence": round(plan.overall_confidence, 3),
            },
        )
        self._record_metrics("generated", plan)
        return plan

    def validate_plan(
        self,
        plan: RecommendationPlan,
        *,
        skip_shadow: bool = False,
    ) -> RecommendationPlan:
        """Validate a recommendation plan."""
        if not plan.items:
            plan.status = RecommendationStatus.REJECTED
            self._plan_store.save(plan, self._settings.cooldown_seconds)
            return plan

        # Step 1: ConstraintEngine validation
        if self._constraint_engine:
            # ConstraintEngine expects changes[param] = (current, proposed) tuples.
            changes = {
                item.parameter: (item.current_value, item.recommended_value)
                for item in plan.items
            }
            current_values = {item.parameter: item.current_value for item in plan.items}
            try:
                result = self._constraint_engine.validate(
                    changes=changes,
                    current_values=current_values,
                    module="settings_recommendation",
                )
                plan.constraint_result = result
                if not result.passed:
                    plan.status = RecommendationStatus.REJECTED
                    self._plan_store.save(plan, self._settings.cooldown_seconds)
                    logger.warning(
                        "recommendation.plan_rejected",
                        extra={
                            "plan_id": plan.plan_id,
                            "violations": len(result.violations),
                        },
                    )
                    return plan
            except Exception:
                logger.warning(
                    "recommendation.constraint_violated",
                    exc_info=True,
                )

        # Step 2: Shadow evaluation (async)
        if self._settings.shadow_required and not skip_shadow and self._shadow_service:
            try:
                evaluation_id = self._shadow_evaluate(plan)
                plan.shadow_evaluation_id = evaluation_id
                plan.status = RecommendationStatus.VALIDATING
                self._plan_store.save(plan, self._settings.cooldown_seconds)
                logger.debug(
                    "recommendation.shadow_submitted",
                    extra={
                        "plan_id": plan.plan_id,
                        "evaluation_id": evaluation_id,
                    },
                )
                return plan
            except Exception:
                logger.warning("recommendation.shadow_submit_failed", exc_info=True)

        # No shadow required or unavailable → immediately validated
        plan.status = RecommendationStatus.VALIDATED
        self._plan_store.save(plan, self._settings.cooldown_seconds)
        logger.debug(
            "recommendation.plan_validated",
            extra={"plan_id": plan.plan_id},
        )
        return plan

    def apply_plan(
        self,
        plan: RecommendationPlan,
        *,
        use_canary: bool | None = None,
    ) -> RecommendationPlan:
        """Apply a validated recommendation plan."""
        if plan.status != RecommendationStatus.VALIDATED:
            return plan

        # Governance check
        try:
            from baldur.factory.registry import ProviderRegistry

            gov_result = ProviderRegistry.governance.get().check_all_governance(
                operation_name="recommendation_apply",
                service_name="settings_recommendation",
            )
            if not gov_result.allowed:
                plan.status = RecommendationStatus.REJECTED
                self._plan_store.save(plan, self._settings.cooldown_seconds)
                logger.warning(
                    "recommendation.governance_blocked",
                    extra={
                        "plan_id": plan.plan_id,
                        "reason": gov_result.block_reason,
                    },
                )
                return plan
        except Exception:
            pass

        should_canary = (
            use_canary if use_canary is not None else self._settings.canary_required
        )

        if should_canary and self._canary_service:
            try:
                rollout_id = self._canary_deploy(plan)
                plan.canary_rollout_id = rollout_id
                plan.status = RecommendationStatus.DEPLOYING
                self._plan_store.save(plan, self._settings.cooldown_seconds)
                logger.info(
                    "recommendation.canary_started",
                    extra={
                        "plan_id": plan.plan_id,
                        "rollout_id": rollout_id,
                    },
                )
                return plan
            except Exception:
                logger.warning("recommendation.canary_start_failed", exc_info=True)

        # Direct apply via ConfigApplier
        self._direct_apply(plan)
        plan.status = RecommendationStatus.APPLIED
        plan.applied_at = utc_now()
        self._plan_store.save(plan, self._settings.cooldown_seconds)

        logger.info(
            "recommendation.plan_applied",
            extra={"plan_id": plan.plan_id, "items": plan.parameter_count},
        )
        self._record_metrics("applied", plan)
        return plan

    def run_full_pipeline(
        self,
        metrics: dict[str, float] | None = None,
    ) -> RecommendationPlan | None:
        """Execute full 2-Phase pipeline: generate → validate → apply."""
        if self._paused:
            return None

        settings = self._settings

        # Phase 0: Check DEPLOYING plans for canary completion
        self._check_deployed_plans()

        # Phase 2: Check existing VALIDATING plans
        ready_plans = self.check_pending_plans()
        for plan in ready_plans:
            if plan.status == RecommendationStatus.VALIDATED and settings.auto_apply:
                self.apply_plan(plan)
                return plan

        # Phase 1: Generate new plan (only if no pending plans)
        if not self._has_validating_plans():
            plan = self.generate_recommendations(metrics)
            plan = self.validate_plan(plan)

            if plan.status == RecommendationStatus.VALIDATED and settings.auto_apply:
                self.apply_plan(plan)
            return plan

        return None

    # === Query API ===

    def get_plan(self, plan_id: str) -> RecommendationPlan | None:
        return self._plan_store.get(plan_id)

    def get_recent_plans(
        self,
        limit: int = 20,
        status: RecommendationStatus | None = None,
    ) -> list[RecommendationPlan]:
        return self._plan_store.get_recent_plans(limit=limit, status=status)

    def get_recommendation_history(
        self,
        parameter: str | None = None,
        start_date: Any = None,
        end_date: Any = None,
    ) -> list[RecommendationPlan]:
        plans = self._plan_store.get_recent_plans(limit=200)
        if parameter:
            plans = [p for p in plans if any(i.parameter == parameter for i in p.items)]
        if start_date:
            plans = [p for p in plans if p.created_at >= start_date]
        if end_date:
            plans = [p for p in plans if p.created_at <= end_date]
        return plans

    # === Feedback API ===

    def record_feedback(
        self,
        plan_id: str,
        metrics_before: dict[str, float],
        metrics_after: dict[str, float],
    ) -> None:
        """Record post-deployment metrics for ML model improvement."""
        plan = self._plan_store.get(plan_id)
        if not plan:
            return

        plan.feedback["metrics_before"] = metrics_before
        plan.feedback["metrics_after"] = metrics_after
        self._plan_store.save(plan, self._settings.cooldown_seconds)

        # Update ML models with outcome
        try:
            from baldur.factory.strategies import get_best_optimizer

            optimizer = get_best_optimizer()
            if optimizer:
                params = {i.parameter: i.recommended_value for i in plan.items}
                optimizer.update_observation(params, metrics_after)
        except Exception:
            pass

        logger.debug(
            "recommendation.feedback_recorded",
            extra={"plan_id": plan_id},
        )

    # === Profile API ===

    def recommend_profile(
        self,
        workload_metrics: dict[str, float] | None = None,
    ) -> tuple[str, dict[str, Any], float]:
        """Recommend a workload profile based on metrics."""
        if workload_metrics is None:
            workload_metrics = self._collect_metrics()

        best_profile = WorkloadProfile.MICROSERVICE
        best_confidence = 0.5

        # Simple heuristic: classify based on dominant metric patterns
        rps = workload_metrics.get("rate_limit_rps", 1000)
        latency = workload_metrics.get("p99_latency_ms", 100)

        if rps > 5000 and latency < 200:
            best_profile = WorkloadProfile.API_GATEWAY
            best_confidence = 0.7
        elif rps < 200 and latency > 5000:
            best_profile = WorkloadProfile.DATA_PIPELINE
            best_confidence = 0.7
        elif latency < 50:
            best_profile = WorkloadProfile.REAL_TIME
            best_confidence = 0.6

        settings = WORKLOAD_PRESETS.get(best_profile, {})

        logger.info(
            "recommendation.profile_recommended",
            extra={
                "profile": best_profile.value,
                "confidence": best_confidence,
            },
        )
        return best_profile.value, settings, best_confidence

    # === Lifecycle ===

    def register_with_scheduler(self) -> None:
        """Register with LeaderScheduler for periodic execution."""
        try:
            from baldur.coordination.scheduler import get_leader_scheduler

            settings = self._settings
            self._scheduler = get_leader_scheduler("settings-recommendation")

            self._scheduler.register_leader_callbacks(
                on_become=self._on_become_leader,
                on_lose=self._on_lose_leader,
            )

            @self._scheduler.job(
                interval_seconds=settings.schedule_seconds,
                name="recommendation-pipeline",
            )
            def _cycle() -> None:
                self.run_full_pipeline()

            self._scheduler.start()
        except Exception:
            logger.warning(
                "recommendation.scheduler_registration_failed", exc_info=True
            )

    def unregister_scheduler(self) -> None:
        if self._scheduler:
            self._scheduler.stop()

    def pause(self, reason: str = "manual") -> None:
        self._paused = True
        self._pause_reason = reason

    def resume(self) -> None:
        self._paused = False
        self._pause_reason = ""

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self._settings.enabled,
            "mode": self._settings.mode,
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "auto_apply": self._settings.auto_apply,
            "recent_plans": len(self._plan_store.get_recent_plans(limit=10)),
        }

    # === Leader callbacks ===

    def _on_become_leader(self) -> None:
        """Leader acquired: restore ML states + subscribe to rollback events."""
        self._load_ml_states()
        self._load_pending_plans()
        if not self._rollback_subscribed:
            try:
                from baldur.services.event_bus import (
                    EventPriority,
                    EventType,
                    get_event_bus,
                )

                get_event_bus().subscribe(
                    EventType.RECOMMENDATION_ROLLBACK,
                    self._on_external_rollback,
                    EventPriority.NORMAL,
                )
                self._rollback_subscribed = True
            except Exception:
                pass
        logger.info("recommendation.became_leader")

    def _on_lose_leader(self) -> None:
        """Leader lost: save states + cleanup."""
        self._save_all_ml_states()
        self._persist_all_plans()
        self._teardown_ml_models()
        logger.info("recommendation.lost_leader")

    def close(self) -> None:
        """Unsubscribe all EventBus handlers."""
        if not self._rollback_subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.unsubscribe(
                EventType.RECOMMENDATION_ROLLBACK,
                self._on_external_rollback,
            )
            self._rollback_subscribed = False
        except ImportError:
            pass
        except Exception:
            pass

    def _on_external_rollback(self, event: BaldurEvent) -> None:
        """Handle rollback notification from AutoRollbackGuard."""
        rolled_back_params = event.data.get("parameters", [])
        plan = self._find_active_plan(rolled_back_params)
        if plan:
            plan.status = RecommendationStatus.ROLLED_BACK
            self._plan_store.save(plan, self._settings.cooldown_seconds)
            logger.warning(
                "recommendation.plan_rolled_back",
                extra={
                    "plan_id": plan.plan_id,
                    "reason": event.data.get("reason"),
                },
            )

    # === Shadow / Canary helpers ===

    def _shadow_evaluate(self, plan: RecommendationPlan) -> str:
        """Submit recommendation plan for shadow evaluation."""
        assert (
            self._shadow_service is not None
        )  # caller guards via shadow_required + truthy check
        baseline = {item.parameter: item.current_value for item in plan.items}
        candidate = {item.parameter: item.recommended_value for item in plan.items}

        evaluation = self._shadow_service.submit_evaluation(
            config_type="auto_tuning",
            baseline_config=baseline,
            candidate_config=candidate,
            service_name="settings_recommendation",
        )
        return evaluation.evaluation_id

    def _canary_deploy(self, plan: RecommendationPlan) -> str:
        """Create canary rollout for recommendation plan."""
        from baldur.models.canary import CanaryStage

        assert (
            self._canary_service is not None
        )  # caller guards via should_canary + truthy check

        stage_configs = self._settings.canary_stages
        stages = [
            CanaryStage(
                name=STAGE_NAMES.get(i, f"stage-{i}"),
                clusters=[],
                percentage=sc.percentage,
                duration_minutes=sc.duration_minutes,
            )
            for i, sc in enumerate(stage_configs)
        ]
        rollout = self._canary_service.create_rollout(
            config_type="auto_tuning",
            new_values={i.parameter: i.recommended_value for i in plan.items},
            stages=stages,
            created_by="settings_recommendation",
            reason=f"recommendation-{plan.plan_id[:8]}",
        )
        return rollout.id

    def _direct_apply(self, plan: RecommendationPlan) -> None:
        """Apply changes directly via ConfigApplier (no canary)."""
        try:
            from baldur.core.runtime_feedback import ConfigApplier  # noqa: F401

            # Try to get config_applier from decision_engine
            if self._decision_engine and hasattr(
                self._decision_engine, "config_provider"
            ):
                provider = self._decision_engine.config_provider
                if hasattr(provider, "apply"):
                    for item in plan.items:
                        try:
                            provider.apply(item.parameter, item.recommended_value)
                        except Exception:
                            logger.warning(
                                "recommendation.direct_apply_failed",
                                extra={"parameter": item.parameter},
                            )
        except ImportError:
            pass

    def check_pending_plans(self) -> list[RecommendationPlan]:
        """Check shadow evaluation status for VALIDATING plans."""
        if not self._shadow_service:
            return []

        updated: list[RecommendationPlan] = []
        for plan in self._get_validating_plans():
            if not plan.shadow_evaluation_id:
                continue
            try:
                from baldur.services.config_shadow.models import EvaluationStatus

                evaluation = self._shadow_service.get_evaluation(
                    plan.shadow_evaluation_id
                )
                if evaluation and evaluation.status == EvaluationStatus.COMPLETED:
                    plan.status = (
                        RecommendationStatus.VALIDATED
                        if evaluation.report and evaluation.report.passed
                        else RecommendationStatus.REJECTED
                    )
                    self._plan_store.save(plan, self._settings.cooldown_seconds)
                    updated.append(plan)
                elif evaluation and evaluation.status == EvaluationStatus.FAILED:
                    plan.status = RecommendationStatus.REJECTED
                    self._plan_store.save(plan, self._settings.cooldown_seconds)
                    updated.append(plan)
            except Exception:
                pass
        return updated

    def _check_deployed_plans(self) -> None:
        """Check canary completion for DEPLOYING plans and record feedback."""
        if not self._canary_service:
            return

        try:
            from baldur.models.canary import CanaryState  # noqa: F401
            from baldur.settings.canary import get_canary_settings

            max_age = timedelta(days=get_canary_settings().rollout_ttl_days)
        except Exception:
            max_age = timedelta(days=7)

        for plan in self._get_deploying_plans():
            # Zombie prevention: expire old DEPLOYING plans
            if utc_now() - plan.created_at > max_age:
                plan.status = RecommendationStatus.EXPIRED
                self._plan_store.save(plan, self._settings.cooldown_seconds)
                logger.warning(
                    "recommendation.plan_expired",
                    extra={"plan_id": plan.plan_id, "reason": "rollout_ttl_exceeded"},
                )
                continue

            if not plan.canary_rollout_id:
                continue

            try:
                rollout = self._canary_service.get_rollout(plan.canary_rollout_id)
                if rollout and rollout.state == CanaryState.COMPLETED:
                    plan.status = RecommendationStatus.APPLIED
                    plan.applied_at = utc_now()
                    self._plan_store.save(plan, self._settings.cooldown_seconds)
                    self.record_feedback(
                        plan.plan_id,
                        metrics_before=plan.feedback.get("metrics_snapshot", {}),
                        metrics_after=self._collect_metrics(),
                    )
            except Exception:
                pass

    # === Internal helpers ===

    def _collect_metrics(self) -> dict[str, float]:
        return self._metrics_collector.collect()

    def _collect_prediction_context(self) -> dict[str, Any] | None:
        """Get prediction context from PredictiveForecaster (if available).

        599 D7 — the forecaster lives in the private distribution; resolve
        via the registry slot populated by register_dormant_services().
        Empty slot (OSS-only install) -> None, same as before relocation.
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            forecaster = ProviderRegistry.predictive_forecaster_service.safe_get()
            if forecaster:
                return {
                    "trend_slope": getattr(forecaster, "trend_slope", 0.0),
                    "prediction_confidence": getattr(forecaster, "confidence", 0.0),
                }
        except Exception:
            pass
        return None

    def _prepare_ml_context(self) -> dict[str, Any]:
        """Prepare ML context data (I/O)."""
        try:
            from baldur.core.safety_bounds import SafetyBounds

            sb = SafetyBounds()
            all_bounds = sb.get_all_bounds()
        except Exception:
            all_bounds = {}

        bounds = {p: (b["min_value"], b["max_value"]) for p, b in all_bounds.items()}
        tunable_params = sorted(bounds.keys())

        # Build history from AdjustmentRecorder
        history = self._build_ml_history(tunable_params)

        return {
            "bounds": bounds,
            "history": history,
            "objective_metrics": self._settings.ml_objective_metrics,
            "tunable_parameters": tunable_params,
        }

    def _build_ml_history(self, tunable_params: list[str]) -> list[dict[str, Any]]:  # noqa: C901, PLR0912
        """Build ML history with timestamp window grouping."""
        recorder = self._get_adjustment_recorder()
        if not recorder:
            return []

        try:
            records = recorder.get_records(limit=200)
        except Exception:
            return []

        if not records:
            return []

        # Sort by timestamp and group by window
        window_seconds = self._settings.history_grouping_window_seconds
        groups: list[list[Any]] = []
        current_group: list[Any] = []

        for r in sorted(records, key=lambda x: x.timestamp):
            if (
                current_group
                and (r.timestamp - current_group[0].timestamp).total_seconds()
                > window_seconds
            ):
                groups.append(current_group)
                current_group = [r]
            else:
                current_group.append(r)
        if current_group:
            groups.append(current_group)

        # Convert groups to multi-param observations
        history: list[dict[str, Any]] = []
        for group in groups:
            obs: dict[str, Any] = {}
            for r in group:
                obs[r.parameter] = r.new_value
            # Fill missing params from old_value within group
            for r in group:
                for p in tunable_params:
                    if p not in obs and r.parameter == p:
                        obs.setdefault(p, r.old_value)
            # Add metrics snapshot from first record
            if group[0].metrics_snapshot:
                obs.update(group[0].metrics_snapshot)
            history.append(obs)

        return history

    def _get_adjustment_recorder(self) -> Any:
        if self._adjustment_recorder is not None:
            return self._adjustment_recorder
        try:
            pass

            # Try to get from existing service
        except Exception:
            pass
        return None

    def _get_current_param_values(self, ml_context: dict[str, Any]) -> dict[str, float]:
        """Get current parameter values."""
        current: dict[str, float] = {}
        if self._decision_engine and hasattr(self._decision_engine, "config_provider"):
            provider = self._decision_engine.config_provider
            for param in ml_context.get("tunable_parameters", []):
                try:
                    val = provider.get(param)
                    if val is not None:
                        current[param] = float(val)
                except Exception:
                    pass
        return current

    def _get_validating_plans(self) -> list[RecommendationPlan]:
        return self._plan_store.get_recent_plans(
            limit=50, status=RecommendationStatus.VALIDATING
        )

    def _get_deploying_plans(self) -> list[RecommendationPlan]:
        return self._plan_store.get_recent_plans(
            limit=50, status=RecommendationStatus.DEPLOYING
        )

    def _has_validating_plans(self) -> bool:
        return len(self._get_validating_plans()) > 0

    def _find_active_plan(
        self, rolled_back_params: list[str]
    ) -> RecommendationPlan | None:
        """Find most recent APPLIED/DEPLOYING plan matching rolled back params."""
        candidates = [
            p
            for p in self._plan_store.get_recent_plans(limit=50)
            if p.status
            in (RecommendationStatus.APPLIED, RecommendationStatus.DEPLOYING)
        ]
        rolled_set = set(rolled_back_params)
        for plan in candidates:
            plan_params = {item.parameter for item in plan.items}
            if plan_params & rolled_set:
                return plan
        return None

    # === ML State Management ===

    def _load_ml_states(self) -> None:
        """Restore ML model states from StateBackend.

        load_state/save_state are persistence concerns on the concrete ML
        model implementations (BaseModel and subclasses); the Strategy
        Protocols intentionally do not declare them. Use duck-typed access.
        """
        try:
            from baldur.factory.strategies import (
                get_best_anomaly_detector,
                get_best_forecaster,
                get_best_optimizer,
            )

            for getter in (
                get_best_optimizer,
                get_best_anomaly_detector,
                get_best_forecaster,
            ):
                strategy = getter()
                load_state = getattr(strategy, "load_state", None)
                if load_state is not None:
                    load_state("recommendation")
        except Exception:
            logger.debug("recommendation.ml_state_load_failed", exc_info=True)

    def _save_all_ml_states(self) -> None:
        """Save all ML model states to StateBackend.

        Same getattr pattern as _load_ml_states — persistence is a
        concrete-impl concern, not part of the Strategy Protocol contract.
        """
        try:
            from baldur.factory.strategies import (
                get_best_anomaly_detector,
                get_best_forecaster,
                get_best_optimizer,
            )

            for getter in (
                get_best_optimizer,
                get_best_anomaly_detector,
                get_best_forecaster,
            ):
                strategy = getter()
                save_state = getattr(strategy, "save_state", None)
                if save_state is not None:
                    save_state("recommendation")
        except Exception:
            logger.debug("recommendation.ml_state_save_failed", exc_info=True)

    def _load_pending_plans(self) -> None:
        """Load VALIDATING plans from Redis after leader transition."""
        restored = self._plan_store.load_all_pending()
        if restored:
            logger.info(
                "recommendation.pending_plans_loaded",
                extra={"count": len(restored)},
            )

    def _persist_all_plans(self) -> None:
        """Save all plan states to Redis before losing leadership."""
        for plan in self._plan_store.get_recent_plans(limit=50):
            self._plan_store.save(plan, self._settings.cooldown_seconds)

    def _teardown_ml_models(self) -> None:
        """Release ML model memory."""
        pass  # Models are managed by ProviderRegistry singletons

    def _record_metrics(self, event: str, plan: RecommendationPlan) -> None:
        """Record Prometheus metrics."""
        try:
            from baldur.metrics.prometheus import get_metrics

            metrics = get_metrics()
            if hasattr(metrics, "recommendation"):
                recorder = metrics.recommendation
                if event == "generated":
                    for item in plan.items:
                        recorder.record_generated(
                            self._settings.mode, item.source.value
                        )
                        recorder.observe_confidence(item.source.value, item.confidence)
                elif event == "applied":
                    recorder.record_applied(self._settings.mode, "success")
        except Exception:
            pass


from baldur.utils.singleton import CLEANUP_CLOSE, make_singleton_factory

(
    get_settings_recommendation_service,
    configure_settings_recommendation_service,
    reset_settings_recommendation_service,
) = make_singleton_factory(
    "settings_recommendation_service",
    SettingsRecommendationService,
    cleanup_fn=CLEANUP_CLOSE,
)
