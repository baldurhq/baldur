"""
standard_pipeline / ha_pipeline / minimal_pipeline / adaptive_pipeline 프리셋 단위 테스트.

테스트 대상:
- resilience/policies/presets.py

UNIT_TEST_GUIDELINES.md 준수:
- 동작 검증(Behavior): 소스 참조 (PolicyComposer, Guard/Hook/Sink 타입)
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부 (§5.1)

Note:
  presets.py 내부에서 RetryPolicy, BulkheadPolicy, HedgingPolicy 등을
  lazy import하므로, 해당 의존성이 사용 가능한 환경에서만 테스트한다.
  의존성 미설치 시 ImportError로 테스트 skip.
"""

from __future__ import annotations

import pytest

from baldur.resilience.policies.composer import PolicyComposer
from baldur.resilience.policies.fallback import FallbackPolicy
from baldur.resilience.policies.guards.error_budget import ErrorBudgetGuard
from baldur.resilience.policies.guards.kill_switch import KillSwitchGuard
from baldur.resilience.policies.hooks.audit import AuditHook
from baldur.resilience.policies.hooks.metrics import MetricsHook
from baldur.resilience.policies.presets import (
    _build_fallback_policy,
    ha_pipeline,
    standard_pipeline,
)

# =============================================================================
# 동작 검증 — standard_pipeline
# =============================================================================


class TestStandardPipelineBehavior:
    """standard_pipeline() 동작 검증."""

    def test_returns_policy_composer(self):
        """standard_pipeline()은 PolicyComposer 인스턴스를 반환한다."""
        pipeline = standard_pipeline("test_service")
        assert isinstance(pipeline, PolicyComposer)

    def test_has_retry_and_cb_policies(self):
        """RetryPolicy and CircuitBreakerPolicy are included in _policies."""
        pipeline = standard_pipeline("test_service", max_retries=2)
        assert len(pipeline._policies) == 2
        assert pipeline._policies[0].name == "retry"
        assert pipeline._policies[1].name == "circuit_breaker"

    def test_has_kill_switch_guard(self):
        """KillSwitchGuard가 _guards에 포함되어 있다."""
        pipeline = standard_pipeline("test_service")
        guard_types = [type(g) for g in pipeline._guards]
        assert KillSwitchGuard in guard_types

    def test_has_error_budget_guard(self):
        """ErrorBudgetGuard가 _guards에 포함되어 있다."""
        pipeline = standard_pipeline("test_service")
        guard_types = [type(g) for g in pipeline._guards]
        assert ErrorBudgetGuard in guard_types

    def test_has_audit_hook(self):
        """AuditHook이 _hooks에 포함되어 있다."""
        pipeline = standard_pipeline("test_service")
        hook_types = [type(h) for h in pipeline._hooks]
        assert AuditHook in hook_types

    def test_has_dlq_sink(self):
        """DLQSink가 _sinks에 포함되어 있다."""
        from baldur.services.retry_handler.sinks import DLQSink

        pipeline = standard_pipeline("test_service")
        sink_types = [type(s) for s in pipeline._sinks]
        assert DLQSink in sink_types

    def test_custom_max_retries(self):
        """max_retries 파라미터가 RetryPolicy에 전달된다."""
        pipeline = standard_pipeline("test_service", max_retries=5)
        retry_policy = pipeline._policies[0]
        assert retry_policy._config.max_attempts == 5

    def test_custom_domain(self):
        """domain 파라미터가 RetryPolicy config에 전달된다."""
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
# 동작 검증 — ha_pipeline
# =============================================================================


class TestHaPipelineBehavior:
    """ha_pipeline() 동작 검증."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # ha_pipeline is fail-closed PRO-absent (Bulkhead + Hedging are PRO).
        pytest.importorskip("baldur_pro")

    def test_returns_policy_composer(self):
        """ha_pipeline()은 PolicyComposer 인스턴스를 반환한다."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        assert isinstance(pipeline, PolicyComposer)

    def test_has_three_policies(self):
        """RetryPolicy + BulkheadPolicy + HedgingPolicy 총 3개 Policy가 포함된다."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        assert len(pipeline._policies) == 3

    def test_policy_order(self):
        """Policy 순서: Retry(바깥) → Bulkhead → Hedging(안쪽)."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        policy_names = [p.name for p in pipeline._policies]
        assert policy_names[0] == "retry"
        assert policy_names[1] == "bulkhead"
        assert policy_names[2] == "hedging"

    def test_has_both_guards(self):
        """KillSwitchGuard, ErrorBudgetGuard가 모두 포함된다."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        guard_types = [type(g) for g in pipeline._guards]
        assert KillSwitchGuard in guard_types
        assert ErrorBudgetGuard in guard_types

    def test_has_audit_and_metrics_hooks(self):
        """AuditHook과 MetricsHook이 포함된다."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        hook_types = [type(h) for h in pipeline._hooks]
        assert AuditHook in hook_types
        assert MetricsHook in hook_types

    def test_has_dlq_sink(self):
        """DLQSink가 포함된다."""
        from baldur.services.retry_handler.sinks import DLQSink

        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        sink_types = [type(s) for s in pipeline._sinks]
        assert DLQSink in sink_types

    def test_custom_max_retries(self):
        """max_retries 파라미터가 RetryPolicy에 전달된다."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            max_retries=1,
        )
        retry_policy = pipeline._policies[0]
        assert retry_policy._config.max_attempts == 1


# =============================================================================
# 동작 검증 — _build_fallback_policy (#234)
# =============================================================================


class TestBuildFallbackPolicyBehavior:
    """_build_fallback_policy() 동작 검증."""

    def test_all_none_returns_none(self):
        """모든 파라미터가 None이면 None을 반환한다."""
        result = _build_fallback_policy(
            fallback_chain=None,
            fallback_fn=None,
            fallback_default=None,
        )
        assert result is None

    def test_fallback_default_only(self):
        """fallback_default만 전달하면 FallbackPolicy를 반환한다."""
        result = _build_fallback_policy(
            fallback_chain=None,
            fallback_fn=None,
            fallback_default={"status": "degraded"},
        )
        assert isinstance(result, FallbackPolicy)
        assert result._default_value == {"status": "degraded"}

    def test_fallback_fn_only(self):
        """fallback_fn만 전달하면 FallbackPolicy를 반환한다."""

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
        """fallback_chain만 전달하면 FallbackPolicy를 반환한다."""
        chain = [lambda: "first", lambda: "second"]
        result = _build_fallback_policy(
            fallback_chain=chain,
            fallback_fn=None,
            fallback_default=None,
        )
        assert isinstance(result, FallbackPolicy)
        assert result._fallback_chain == chain

    def test_all_three_params(self):
        """3단계 파라미터 모두 전달 시 FallbackPolicy에 모두 적용된다."""
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
        """반환된 FallbackPolicy의 name은 'fallback'이다."""
        result = _build_fallback_policy(
            fallback_chain=None,
            fallback_fn=None,
            fallback_default="default",
        )
        assert result.name == "fallback"


# =============================================================================
# 동작 검증 — standard_pipeline Fallback 통합 (#234)
# =============================================================================


class TestStandardPipelineFallbackBehavior:
    """standard_pipeline() Fallback 파라미터 동작 검증 (#234)."""

    def test_no_fallback_params_excludes_fallback_policy(self):
        """Fallback 파라미터 없으면 FallbackPolicy가 _policies에 포함되지 않는다."""
        pipeline = standard_pipeline("test_service")
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" not in policy_names

    def test_fallback_default_adds_fallback_policy(self):
        """fallback_default 전달 시 FallbackPolicy가 _policies에 추가된다."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" in policy_names

    def test_fallback_fn_adds_fallback_policy(self):
        """fallback_fn 전달 시 FallbackPolicy가 _policies에 추가된다."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_fn=lambda: "backup",
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" in policy_names

    def test_fallback_chain_adds_fallback_policy(self):
        """fallback_chain 전달 시 FallbackPolicy가 _policies에 추가된다."""
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
        """Fallback 추가 시 Guard(KillSwitch, ErrorBudget)는 유지된다."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        guard_types = [type(g) for g in pipeline._guards]
        assert KillSwitchGuard in guard_types
        assert ErrorBudgetGuard in guard_types

    def test_policy_count_with_fallback(self):
        """With Fallback, _policies count is 3 (Fallback + Retry + CB)."""
        pipeline = standard_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        assert len(pipeline._policies) == 3


# =============================================================================
# 동작 검증 — ha_pipeline Fallback 통합 (#234)
# =============================================================================


class TestHaPipelineFallbackBehavior:
    """ha_pipeline() Fallback 파라미터 동작 검증 (#234)."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # ha_pipeline is fail-closed PRO-absent (Bulkhead + Hedging are PRO).
        pytest.importorskip("baldur_pro")

    def test_no_fallback_params_excludes_fallback_policy(self):
        """Fallback 파라미터 없으면 FallbackPolicy가 _policies에 포함되지 않는다."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" not in policy_names

    def test_fallback_default_adds_fallback_policy(self):
        """fallback_default 전달 시 FallbackPolicy가 _policies에 추가된다."""
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
        """Fallback 추가 시 _policies 개수는 4 (Retry + Bulkhead + Hedging + Fallback)이다."""
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
        """Fallback 추가 시 Guard(KillSwitch, ErrorBudget)는 유지된다."""
        pipeline = ha_pipeline(
            "test_service",
            candidates=[lambda: "alt"],
            fallback_default={"status": "degraded"},
        )
        guard_types = [type(g) for g in pipeline._guards]
        assert KillSwitchGuard in guard_types
        assert ErrorBudgetGuard in guard_types


# =============================================================================
# 동작 검증 — minimal_pipeline
# =============================================================================


class TestMinimalPipelineBehavior:
    """minimal_pipeline() 동작 검증."""

    def test_returns_policy_composer(self):
        """minimal_pipeline()은 PolicyComposer 인스턴스를 반환한다."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert isinstance(pipeline, PolicyComposer)

    def test_has_circuit_breaker_policy(self):
        """CircuitBreakerPolicy가 _policies에 포함되어 있다."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert len(pipeline._policies) == 1
        assert pipeline._policies[0].name == "circuit_breaker"

    def test_no_guards(self):
        """Guard가 포함되지 않는다 (ErrorBudget Redis 호출 절약)."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert len(pipeline._guards) == 0

    def test_no_sinks(self):
        """Sink가 포함되지 않는다 (DLQ 미사용)."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert len(pipeline._sinks) == 0

    def test_default_audit_rate_uses_audit_hook(self):
        """audit_sampling_rate 기본값(1.0)이면 AuditHook이 사용된다."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service")
        assert len(pipeline._hooks) == 1
        assert type(pipeline._hooks[0]) is AuditHook

    def test_sampled_rate_uses_sampled_audit_hook(self):
        """audit_sampling_rate < 1.0이면 SampledAuditHook이 사용된다."""
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
        """audit_sampling_rate=0.0이면 Hook이 없다."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("test_service", audit_sampling_rate=0.0)
        assert len(pipeline._hooks) == 0

    def test_service_name_passed_to_cb(self):
        """service_name이 CircuitBreakerPolicy에 전달된다."""
        from baldur.resilience.policies.presets import minimal_pipeline

        pipeline = minimal_pipeline("my_read_api")
        cb = pipeline._policies[0]
        assert cb.service_name == "my_read_api"


# =============================================================================
# 동작 검증 — adaptive_pipeline
# =============================================================================


class TestAdaptivePipelineBehavior:
    """adaptive_pipeline() 동작 검증."""

    def _reset_settings(self):
        from baldur.settings.pipeline import reset_pipeline_settings

        reset_pipeline_settings()

    def test_disabled_returns_standard_pipeline(self):
        """adaptive_enabled=False이면 standard_pipeline을 반환한다."""
        from baldur.resilience.policies.presets import adaptive_pipeline

        self._reset_settings()
        pipeline = adaptive_pipeline("test_service")
        # standard_pipeline은 KillSwitchGuard + ErrorBudgetGuard를 가짐
        guard_types = [type(g) for g in pipeline._guards]
        assert KillSwitchGuard in guard_types
        assert ErrorBudgetGuard in guard_types

    def test_enabled_hot_tier_returns_minimal(self):
        """adaptive_enabled=True + hot tier → minimal_pipeline 반환."""
        from unittest.mock import patch

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
        # minimal은 Guard가 없다
        assert len(pipeline._guards) == 0
        assert pipeline._policies[0].name == "circuit_breaker"

    def test_enabled_non_hot_tier_returns_standard(self):
        """adaptive_enabled=True + non-hot tier → standard_pipeline 반환."""
        from unittest.mock import patch

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
        assert KillSwitchGuard in guard_types

    def test_enabled_no_tier_returns_standard(self):
        """adaptive_enabled=True + tier_id=None → standard_pipeline 반환."""
        from unittest.mock import patch

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
        assert KillSwitchGuard in guard_types

    def test_degradation_active_returns_minimal(self):
        """GracefulDegradation이 full_guards를 비활성화하면 minimal 반환."""
        from unittest.mock import MagicMock, patch

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
        # minimal → Guard 없음
        assert len(pipeline._guards) == 0
        mock_degradation.is_enabled.assert_called_once_with("full_guards")

    def test_audit_sampling_rate_propagated_to_minimal(self):
        """adaptive_pipeline의 audit_sampling_rate가 minimal에 전달된다."""
        from unittest.mock import patch

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
        """adaptive_pipeline의 fallback 파라미터가 standard_pipeline에 전달된다."""
        from baldur.resilience.policies.presets import adaptive_pipeline

        self._reset_settings()
        pipeline = adaptive_pipeline(
            "test_service",
            fallback_default={"status": "degraded"},
        )
        policy_names = [p.name for p in pipeline._policies]
        assert "fallback" in policy_names
