"""
standard_pipeline / ha_pipeline / minimal_pipeline / adaptive_pipeline preset unit tests.

Test targets:
- resilience/policies/presets.py

Complies with UNIT_TEST_GUIDELINES.md:
- Behavior: source references (PolicyComposer, Guard/Hook/Sink types)
- conftest.py placement: fixtures used by a single file → inside the file (§5.1)

Note:
  presets.py lazily imports RetryPolicy, BulkheadPolicy, HedgingPolicy, etc.,
  so tests run only in environments where those dependencies are available.
  If a dependency is not installed, tests are skipped via ImportError.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, DLQEntryResult
from baldur.resilience.policies.composer import PolicyComposer
from baldur.resilience.policies.fallback import FallbackPolicy
from baldur.resilience.policies.guards.error_budget import ErrorBudgetGuard
from baldur.resilience.policies.hooks.audit import AuditHook
from baldur.resilience.policies.hooks.metrics import MetricsHook
from baldur.resilience.policies.presets import (
    _build_fallback_policy,
    ha_pipeline,
    standard_pipeline,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

# =============================================================================
# Behavior — standard_pipeline
# =============================================================================


class TestStandardPipelineBehavior:
    """standard_pipeline() behavior."""

    def test_returns_policy_composer(self):
        """standard_pipeline() returns a PolicyComposer instance."""
        pipeline = standard_pipeline("test_service")
        assert isinstance(pipeline, PolicyComposer)

    def test_has_retry_and_cb_policies(self):
        """RetryPolicy and CircuitBreakerPolicy are included in _policies."""
        pipeline = standard_pipeline("test_service", max_retries=2)
        assert len(pipeline._policies) == 2
        assert pipeline._policies[0].name == "retry"
        assert pipeline._policies[1].name == "circuit_breaker"

    def test_has_no_kill_switch_guard(self):
        """No kill-switch guard: the switch steps Baldur aside, never refuses the call."""
        pipeline = standard_pipeline("test_service")
        assert "kill_switch" not in [g.name for g in pipeline._guards]

    def test_has_error_budget_guard(self):
        """ErrorBudgetGuard is included in _guards."""
        pipeline = standard_pipeline("test_service")
        guard_types = [type(g) for g in pipeline._guards]
        assert ErrorBudgetGuard in guard_types

    def test_has_audit_hook(self):
        """AuditHook is included in _hooks."""
        pipeline = standard_pipeline("test_service")
        hook_types = [type(h) for h in pipeline._hooks]
        assert AuditHook in hook_types

    def test_has_dlq_sink(self):
        """DLQSink is included in _sinks."""
        from baldur.services.retry_handler.sinks import DLQSink

        pipeline = standard_pipeline("test_service")
        sink_types = [type(s) for s in pipeline._sinks]
        assert DLQSink in sink_types

    def test_custom_max_retries(self):
        """The max_retries parameter is passed to RetryPolicy."""
        pipeline = standard_pipeline("test_service", max_retries=5)
        retry_policy = pipeline._policies[0]
        assert retry_policy._config.max_attempts == 5

    def test_custom_domain(self):
        """The domain parameter is passed to the RetryPolicy config."""
        pipeline = standard_pipeline("test_service", domain="payment")
        retry_policy = pipeline._policies[0]
        assert retry_policy._config.domain == "payment"


# =============================================================================
# Behavior — standard_pipeline CB inclusion (#418 P0-2)
# =============================================================================


class TestStandardPipelineCBInclusionP0_2Behavior:
    """standard_pipeline() CB inclusion and ordering (#418 P0-2)."""

    def test_standard_pipeline_includes_cb(self):
        """standard_pipeline contains CircuitBreakerPolicy by default."""
        from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy

        pipeline = standard_pipeline("test_service")
        policy_types = [type(p) for p in pipeline._policies]
        assert CircuitBreakerPolicy in policy_types

    def test_standard_pipeline_policy_order_without_fallback(self):
        """Without fallback: order = [Retry, CB] (outermost→innermost)."""
        pipeline = standard_pipeline("test_service")
        names = [p.name for p in pipeline._policies]
        assert names == ["retry", "circuit_breaker"]

    def test_standard_pipeline_policy_order_with_fallback(self):
        """With fallback: order = [Fallback, Retry, CB] (outermost→innermost)."""
        pipeline = standard_pipeline(
            "test_service", fallback_default={"status": "degraded"}
        )
        names = [p.name for p in pipeline._policies]
        assert names == ["fallback", "retry", "circuit_breaker"]

    def test_standard_pipeline_cb_disabled(self):
        """cb_enabled=False excludes CircuitBreakerPolicy."""
        pipeline = standard_pipeline("test_service", cb_enabled=False)
        names = [p.name for p in pipeline._policies]
        assert "circuit_breaker" not in names
        assert names == ["retry"]

    def test_standard_pipeline_cb_disabled_with_fallback(self):
        """cb_enabled=False with fallback: order = [Fallback, Retry]."""
        pipeline = standard_pipeline(
            "test_service",
            cb_enabled=False,
            fallback_default="default",
        )
        names = [p.name for p in pipeline._policies]
        assert names == ["fallback", "retry"]


# =============================================================================
# Behavior — standard_pipeline parks every failed call under service_name
# =============================================================================

_STORE = "baldur.services.retry_handler.sinks.store_to_dlq"


@pytest.fixture
def store() -> Iterator[MagicMock]:
    """The DLQ store the preset's sink calls — the capture seam."""
    with patch(
        _STORE, autospec=True, return_value=DLQEntryResult.created("dlq-1")
    ) as mock_store:
        yield mock_store


@pytest.fixture
def shared_breaker_opening_on_first_failure() -> Iterator[CircuitBreakerService]:
    """The process-shared breaker service the preset's own breaker records
    on, swapped for an in-memory one that opens on a single failure."""
    service = CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            failure_threshold=1,
            minimum_calls=1,
            failure_rate_threshold=0,
            recovery_timeout=60,
        ),
        repository=InMemoryCircuitBreakerStateRepository(),
    )
    with patch(
        "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service",
        return_value=service,
    ):
        yield service


def _down() -> str:
    raise ConnectionError("upstream down")


def _parked(store: MagicMock) -> list[tuple[str, str]]:
    return [
        (c.kwargs["domain"], c.kwargs["failure_type"]) for c in store.call_args_list
    ]


class TestStandardPipelineDlqCaptureBehavior:
    """The preset arms its composer under ``service_name``: a call its open
    breaker refuses is parked as an open-circuit entry, and a failed call's
    placeholder-domain verdict is filed under the service name."""

    def test_open_breaker_refusal_is_parked_once_as_open_circuit_under_service_name(
        self, store, shared_breaker_opening_on_first_failure
    ):
        # Given a single-attempt preset whose breaker opens on its first failure
        pipeline = standard_pipeline("svc.preset_outage", max_retries=1)

        # When one call fails and the next is refused by the open breaker
        first = pipeline.execute(_down)
        second = pipeline.execute(_down)

        # Then each is parked once under the service name — the refusal as an
        # open-circuit entry, never as a retry exhaustion of the breaker error
        assert first.outcome == PolicyOutcome.FAILURE
        assert second.outcome == PolicyOutcome.REJECTED
        assert isinstance(second.error, CircuitBreakerOpenError)
        assert _parked(store) == [
            ("svc.preset_outage", "MAX_RETRIES_CONNECTIONERROR"),
            ("svc.preset_outage", OPEN_CIRCUIT_FAILURE_TYPE),
        ]

    def test_failed_call_is_parked_under_service_name_not_the_placeholder(self, store):
        """The preset's own retry config names the placeholder domain."""
        pipeline = standard_pipeline("svc.preset_down", max_retries=1, cb_enabled=False)

        pipeline.execute(_down)

        assert _parked(store) == [("svc.preset_down", "MAX_RETRIES_CONNECTIONERROR")]
        assert store.call_args.kwargs["domain"] != "default"

    def test_caller_retry_stage_exhaustion_is_parked_under_service_name(self, store):
        """A ``retry_policy`` built without a domain keeps its own attempt count;
        only its placeholder domain is replaced."""
        pipeline = standard_pipeline(
            "svc.preset_retried",
            cb_enabled=False,
            retry_policy=RetryPolicy(
                config=RetryPolicyConfig(max_attempts=2), sleeper=lambda _: None
            ),
        )

        pipeline.execute(_down)

        assert _parked(store) == [("svc.preset_retried", "MAX_RETRIES_CONNECTIONERROR")]
        assert store.call_args.kwargs["metadata"]["max_attempts"] == 2


# =============================================================================
# Behavior — ha_pipeline
# =============================================================================


class TestHaPipelineBehavior:
    """ha_pipeline() behavior."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # ha_pipeline is fail-closed PRO-absent (Bulkhead + Hedging are PRO).
        pytest.importorskip("baldur_pro")

    def test_returns_policy_composer(self):
        """ha_pipeline() returns a PolicyComposer instance."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        assert isinstance(pipeline, PolicyComposer)

    def test_has_three_policies(self):
        """A total of 3 policies are included: RetryPolicy + BulkheadPolicy + HedgingPolicy."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        assert len(pipeline._policies) == 3

    def test_policy_order(self):
        """Policy order: Retry (outermost) → Bulkhead → Hedging (innermost)."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        policy_names = [p.name for p in pipeline._policies]
        assert policy_names[0] == "retry"
        assert policy_names[1] == "bulkhead"
        assert policy_names[2] == "hedging"

    def test_has_error_budget_guard_only(self):
        """ErrorBudgetGuard is the only guard (no kill-switch guard)."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        guard_types = [type(g) for g in pipeline._guards]
        assert guard_types == [ErrorBudgetGuard]

    def test_has_audit_and_metrics_hooks(self):
        """AuditHook and MetricsHook are included."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        hook_types = [type(h) for h in pipeline._hooks]
        assert AuditHook in hook_types
        assert MetricsHook in hook_types

    def test_has_dlq_sink(self):
        """DLQSink is included."""
        from baldur.services.retry_handler.sinks import DLQSink

        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        sink_types = [type(s) for s in pipeline._sinks]
        assert DLQSink in sink_types

    def test_custom_max_retries(self):
        """The max_retries parameter is passed to RetryPolicy."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            max_retries=1,
        )
        retry_policy = pipeline._policies[0]
        assert retry_policy._config.max_attempts == 1


# =============================================================================
# Behavior — _build_fallback_policy (#234)
# =============================================================================


class TestBuildFallbackPolicyBehavior:
    """_build_fallback_policy() behavior."""

    def test_all_none_returns_none(self):
        """Returns None if all parameters are None."""
        result = _build_fallback_policy(
            fallback_chain=None,
            fallback_fn=None,
            fallback_default=None,
        )
        assert result is None

    def test_fallback_default_only(self):
        """Returns a FallbackPolicy if only fallback_default is passed."""
        result = _build_fallback_policy(
            fallback_chain=None,
            fallback_fn=None,
            fallback_default={"status": "degraded"},
        )
        assert isinstance(result, FallbackPolicy)
        assert result._default_value == {"status": "degraded"}

    def test_fallback_fn_only(self):
        """Returns a FallbackPolicy if only fallback_fn is passed."""

        def fn():
            return "fallback_value"

        result = _build_fallback_policy(
            fallback_chain=None,
            fallback_fn=fn,
            fallback_default=None,
        )
        assert isinstance(result, FallbackPolicy)
        assert result._fallback_fn is fn

    def test_fallback_chain_only(self):
        """Returns a FallbackPolicy if only fallback_chain is passed."""
        chain = [lambda: "first", lambda: "second"]
        result = _build_fallback_policy(
            fallback_chain=chain,
            fallback_fn=None,
            fallback_default=None,
        )
        assert isinstance(result, FallbackPolicy)
        assert result._fallback_chain == chain

    def test_all_three_params(self):
        """When all 3 tier parameters are passed, all are applied to the FallbackPolicy."""
        chain = [lambda: "chain"]

        def fn():
            return "fn"

        default = {"status": "degraded"}

        result = _build_fallback_policy(
            fallback_chain=chain,
            fallback_fn=fn,
            fallback_default=default,
        )
        assert isinstance(result, FallbackPolicy)
        assert result._fallback_chain == chain
        assert result._fallback_fn is fn
        assert result._default_value == default

    def test_returned_policy_name_is_fallback(self):
        """The returned FallbackPolicy has the name 'fallback'."""
        result = _build_fallback_policy(
            fallback_chain=None,
            fallback_fn=None,
            fallback_default="default",
        )
        assert result.name == "fallback"


# =============================================================================
# Behavior — standard_pipeline Fallback integration (#234)
# =============================================================================


class TestStandardPipelineFallbackBehavior:
    """standard_pipeline() Fallback parameter behavior (#234)."""

    def test_no_fallback_params_excludes_fallback_policy(self):
        """Without Fallback parameters, no FallbackPolicy is included in _policies."""
        pipeline = standard_pipeline("test_service")
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" not in policy_names

    def test_fallback_default_adds_fallback_policy(self):
        """Passing fallback_default adds a FallbackPolicy to _policies."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" in policy_names

    def test_fallback_fn_adds_fallback_policy(self):
        """Passing fallback_fn adds a FallbackPolicy to _policies."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_fn=lambda: "backup",
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" in policy_names

    def test_fallback_chain_adds_fallback_policy(self):
        """Passing fallback_chain adds a FallbackPolicy to _policies."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_chain=[lambda: "first", lambda: "second"],
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" in policy_names

    def test_fallback_policy_is_first_in_policies(self):
        """FallbackPolicy is placed first (outermost) in _policies."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        first_policy = pipeline._policies[0]
        assert first_policy.name == "fallback"

    def test_fallback_preserves_retry_policy(self):
        """With Fallback, RetryPolicy is the second Policy."""
        pipeline = standard_pipeline(
            "test_service",
            max_retries=3,
            fallback_default={"status": "degraded"},
        )
        assert pipeline._policies[1].name == "retry"

    def test_fallback_default_value_propagated(self):
        """fallback_default value is propagated to FallbackPolicy._default_value."""
        expected_default = {"status": "degraded"}
        pipeline = standard_pipeline(
            "test_service",
            fallback_default=expected_default,
        )
        fallback = pipeline._policies[0]
        assert fallback._default_value == expected_default

    def test_fallback_with_all_three_params(self):
        """All 3-tier Fallback params are reflected in FallbackPolicy."""
        chain = [lambda: "chain_value"]

        def fn():
            return "fn_value"

        default = {"status": "degraded"}

        pipeline = standard_pipeline(
            "test_service",
            fallback_chain=chain,
            fallback_fn=fn,
            fallback_default=default,
        )
        fallback = pipeline._policies[0]
        assert fallback._fallback_chain == chain
        assert fallback._fallback_fn is fn
        assert fallback._default_value == default

    def test_guards_preserved_with_fallback(self):
        """Adding a fallback keeps the ErrorBudget guard."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        guard_types = [type(g) for g in pipeline._guards]
        assert guard_types == [ErrorBudgetGuard]

    def test_policy_count_with_fallback(self):
        """With Fallback, _policies count is 3 (Fallback + Retry + CB)."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        assert len(pipeline._policies) == 3


# =============================================================================
# Behavior — ha_pipeline Fallback integration (#234)
# =============================================================================


class TestHaPipelineFallbackBehavior:
    """ha_pipeline() Fallback parameter behavior (#234)."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # ha_pipeline is fail-closed PRO-absent (Bulkhead + Hedging are PRO).
        pytest.importorskip("baldur_pro")

    def test_no_fallback_params_excludes_fallback_policy(self):
        """Without Fallback parameters, no FallbackPolicy is included in _policies."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" not in policy_names

    def test_fallback_default_adds_fallback_policy(self):
        """Passing fallback_default adds a FallbackPolicy to _policies."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            fallback_default={"status": "degraded"},
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" in policy_names

    def test_fallback_policy_is_first_in_policies(self):
        """FallbackPolicy is placed first (outermost) in _policies."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            fallback_default={"status": "degraded"},
        )
        first_policy = pipeline._policies[0]
        assert first_policy.name == "fallback"

    def test_original_three_policies_preserved_with_fallback(self):
        """With Fallback, original 3 policies (Retry, Bulkhead, Hedging) order preserved."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            fallback_default={"status": "degraded"},
        )
        policy_names = [p.name for p in pipeline._policies]
        assert policy_names[0] == "fallback"
        assert policy_names[1] == "retry"
        assert policy_names[2] == "bulkhead"
        assert policy_names[3] == "hedging"

    def test_policy_count_with_fallback(self):
        """With Fallback added, _policies has 4 entries (Retry + Bulkhead + Hedging + Fallback)."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            fallback_default={"status": "degraded"},
        )
        assert len(pipeline._policies) == 4

    def test_fallback_chain_propagated(self):
        """fallback_chain is propagated to FallbackPolicy._fallback_chain."""
        chain = [lambda: "first", lambda: "second"]
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            fallback_chain=chain,
        )
        fallback = pipeline._policies[0]
        assert fallback._fallback_chain == chain

    def test_guards_preserved_with_fallback(self):
        """Adding a fallback keeps the ErrorBudget guard."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            fallback_default={"status": "degraded"},
        )
        guard_types = [type(g) for g in pipeline._guards]
        assert guard_types == [ErrorBudgetGuard]


# =============================================================================
# Behavior — minimal_pipeline
# =============================================================================


class TestMinimalPipelineBehavior:
    """minimal_pipeline() behavior."""

    def test_returns_policy_composer(self):
        """minimal_pipeline() returns a PolicyComposer instance."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert isinstance(pipeline, PolicyComposer)

    def test_has_circuit_breaker_policy(self):
        """CircuitBreakerPolicy is included in _policies."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert len(pipeline._policies) == 1
        assert pipeline._policies[0].name == "circuit_breaker"

    def test_no_guards(self):
        """No Guard is included (saves ErrorBudget Redis calls)."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert len(pipeline._guards) == 0

    def test_no_sinks(self):
        """No Sink is included (DLQ not used)."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert len(pipeline._sinks) == 0

    def test_default_audit_rate_uses_audit_hook(self):
        """With the default audit_sampling_rate (1.0), AuditHook is used."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert len(pipeline._hooks) == 1
        assert type(pipeline._hooks[0]) is AuditHook

    def test_sampled_rate_uses_sampled_audit_hook(self):
        """With audit_sampling_rate < 1.0, SampledAuditHook is used."""
        from baldur.resilience.policies.hooks.sampled_audit import (
            SampledAuditHook,
        )
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service", audit_sampling_rate=0.5)
        assert len(pipeline._hooks) == 1
        hook = pipeline._hooks[0]
        assert isinstance(hook, SampledAuditHook)
        assert hook.sample_rate == 0.5

    def test_zero_rate_no_hooks(self):
        """With audit_sampling_rate=0.0, there are no Hooks."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service", audit_sampling_rate=0.0)
        assert len(pipeline._hooks) == 0

    def test_service_name_passed_to_cb(self):
        """service_name is passed to CircuitBreakerPolicy."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("my_read_api")
        cb = pipeline._policies[0]
        assert cb.service_name == "my_read_api"


# =============================================================================
# Behavior — adaptive_pipeline
# =============================================================================


class TestAdaptivePipelineBehavior:
    """adaptive_pipeline() behavior."""

    def _reset_settings(self):
        from baldur.settings.pipeline import reset_pipeline_settings

        reset_pipeline_settings()

    def test_disabled_returns_standard_pipeline(self):
        """With adaptive_enabled=False, returns standard_pipeline."""
        from baldur.resilience.policies.presets import adaptive_pipeline

        self._reset_settings()
        pipeline = adaptive_pipeline("test_service")
        # standard_pipeline carries the ErrorBudgetGuard (and no kill-switch guard)
        guard_types = [type(g) for g in pipeline._guards]
        assert guard_types == [ErrorBudgetGuard]

    def test_enabled_hot_tier_returns_minimal(self):
        """adaptive_enabled=True + hot tier → returns minimal_pipeline."""

        from baldur.resilience.policies.presets import adaptive_pipeline
        from baldur.settings.pipeline import PipelineSettings

        self._reset_settings()
        mock_settings = PipelineSettings(
            adaptive_enabled=True,
            hot_path_tiers=["non_essential"],
            audit_sampling_rate=1.0,
        )
        with patch(
            "baldur.settings.pipeline.get_pipeline_settings",
            return_value=mock_settings,
        ):
            pipeline = adaptive_pipeline("test_service", tier_id="non_essential")
        # minimal has no Guard
        assert len(pipeline._guards) == 0
        assert pipeline._policies[0].name == "circuit_breaker"

    def test_enabled_non_hot_tier_returns_standard(self):
        """adaptive_enabled=True + non-hot tier → returns standard_pipeline."""

        from baldur.resilience.policies.presets import adaptive_pipeline
        from baldur.settings.pipeline import PipelineSettings

        self._reset_settings()
        mock_settings = PipelineSettings(
            adaptive_enabled=True,
            hot_path_tiers=["non_essential"],
            audit_sampling_rate=1.0,
        )
        with patch(
            "baldur.settings.pipeline.get_pipeline_settings",
            return_value=mock_settings,
        ):
            pipeline = adaptive_pipeline("test_service", tier_id="critical")
        guard_types = [type(g) for g in pipeline._guards]
        assert guard_types == [ErrorBudgetGuard]

    def test_enabled_no_tier_returns_standard(self):
        """adaptive_enabled=True + tier_id=None → returns standard_pipeline."""

        from baldur.resilience.policies.presets import adaptive_pipeline
        from baldur.settings.pipeline import PipelineSettings

        self._reset_settings()
        mock_settings = PipelineSettings(
            adaptive_enabled=True,
            hot_path_tiers=["non_essential"],
            audit_sampling_rate=1.0,
        )
        with patch(
            "baldur.settings.pipeline.get_pipeline_settings",
            return_value=mock_settings,
        ):
            pipeline = adaptive_pipeline("test_service", tier_id=None)
        guard_types = [type(g) for g in pipeline._guards]
        assert guard_types == [ErrorBudgetGuard]

    def test_degradation_active_returns_minimal(self):
        """Returns minimal when GracefulDegradation disables full_guards."""

        from baldur.resilience.policies.presets import adaptive_pipeline
        from baldur.settings.pipeline import PipelineSettings

        self._reset_settings()
        mock_settings = PipelineSettings(
            adaptive_enabled=True,
            hot_path_tiers=[],
            audit_sampling_rate=1.0,
        )
        mock_degradation = MagicMock()
        mock_degradation.is_enabled.return_value = False

        with (
            patch(
                "baldur.settings.pipeline.get_pipeline_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.scaling.graceful_degradation.get_graceful_degradation",
                return_value=mock_degradation,
            ),
        ):
            pipeline = adaptive_pipeline("test_service", tier_id="standard")
        # minimal → no Guard
        assert len(pipeline._guards) == 0
        mock_degradation.is_enabled.assert_called_once_with("full_guards")

    def test_audit_sampling_rate_propagated_to_minimal(self):
        """adaptive_pipeline's audit_sampling_rate is passed to minimal."""

        from baldur.resilience.policies.hooks.sampled_audit import (
            SampledAuditHook,
        )
        from baldur.resilience.policies.presets import adaptive_pipeline
        from baldur.settings.pipeline import PipelineSettings

        self._reset_settings()
        mock_settings = PipelineSettings(
            adaptive_enabled=True,
            hot_path_tiers=["non_essential"],
            audit_sampling_rate=0.05,
        )
        with patch(
            "baldur.settings.pipeline.get_pipeline_settings",
            return_value=mock_settings,
        ):
            pipeline = adaptive_pipeline("test_service", tier_id="non_essential")
        assert len(pipeline._hooks) == 1
        hook = pipeline._hooks[0]
        assert isinstance(hook, SampledAuditHook)
        assert hook.sample_rate == 0.05

    def test_fallback_params_forwarded_to_standard(self):
        """adaptive_pipeline's fallback parameters are passed to standard_pipeline."""
        from baldur.resilience.policies.presets import adaptive_pipeline

        self._reset_settings()
        pipeline = adaptive_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" in policy_names
