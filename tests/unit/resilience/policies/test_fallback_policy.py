"""
FallbackPolicy / AsyncFallbackPolicy / partition_aware_chain 단위 테스트 (#229).

테스트 대상:
- resilience/policies/fallback.py (FallbackPolicy, AsyncFallbackPolicy,
  partition_aware_chain, _FALLBACK_MODE_TO_OUTCOME)
- resilience/policies/__init__.py (export 검증)

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): 하드코딩 기대값 (name, outcome, executed_policies, 매핑 테이블)
- 동작 검증(Behavior): 소스 참조 (PolicyOutcome, _FALLBACK_MODE_TO_OUTCOME 등)
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부 (§5.1)
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from baldur.core.exceptions import TimeoutPolicyError
from baldur.core.fallback_strategy import (
    FallbackMode,
    FallbackResult,
    SimpleFallback,
)
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyRejectedException,
    PolicyResult,
)
from baldur.resilience.policies import (
    AsyncFallbackPolicy,
    FallbackPolicy,
    partition_aware_chain,
)
from baldur.resilience.policies.fallback import (
    _FALLBACK_ARITY_CACHE_SIZE,
    _FALLBACK_MODE_TO_OUTCOME,
    _fallback_accepts_error,
)

# =============================================================================
# Fixtures — 1개 파일 전용이므로 파일 내부 배치 (§5.1)
# =============================================================================


@pytest.fixture
def basic_policy():
    """fallback_fn만 가진 기본 FallbackPolicy."""
    return FallbackPolicy(fallback_fn=lambda: "fallback_value")


@pytest.fixture
def chain_policy():
    """fallback_chain + default_value를 가진 FallbackPolicy."""
    return FallbackPolicy(
        fallback_chain=[
            lambda: "chain_0",
            lambda: "chain_1",
        ],
        default_value="default",
    )


@pytest.fixture
def full_policy():
    """fallback_chain + fallback_fn + default_value 모두 설정된 FallbackPolicy."""
    return FallbackPolicy(
        fallback_chain=[lambda: "chain_result"],
        fallback_fn=lambda: "fn_result",
        default_value="default_result",
    )


@pytest.fixture
def strategy_policy():
    """SimpleFallback strategy shim 기반 FallbackPolicy."""
    return FallbackPolicy(strategy=SimpleFallback())


@pytest.fixture
def async_basic_policy():
    """fallback_fn만 가진 기본 AsyncFallbackPolicy."""

    async def async_fallback():
        return "async_fallback_value"

    return AsyncFallbackPolicy(fallback_fn=async_fallback)


@pytest.fixture
def async_chain_policy():
    """fallback_chain + default_value를 가진 AsyncFallbackPolicy."""

    async def chain_0():
        return "async_chain_0"

    async def chain_1():
        return "async_chain_1"

    return AsyncFallbackPolicy(
        fallback_chain=[chain_0, chain_1],
        default_value="async_default",
    )


# =============================================================================
# 계약 검증 (Contract) — FallbackPolicy 고정 식별자 및 결과 구조
# =============================================================================


class TestFallbackPolicyContract:
    """FallbackPolicy 고정 식별자 및 결과 구조 계약 검증."""

    def test_name_is_fallback(self, basic_policy):
        """name property는 'fallback'이다."""
        assert basic_policy.name == "fallback"

    def test_success_result_has_fallback_in_executed_policies(self, basic_policy):
        """성공 결과의 executed_policies에 'fallback'가 포함된다."""
        result = basic_policy.execute(lambda: "ok")
        assert "fallback" in result.executed_policies

    def test_fallback_result_has_fallback_in_executed_policies(self, basic_policy):
        """Fallback 결과의 executed_policies에 'fallback'가 포함된다."""
        result = basic_policy.execute(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert "fallback" in result.executed_policies

    def test_success_outcome_is_success(self, basic_policy):
        """func 성공 시 outcome은 PolicyOutcome.SUCCESS이다."""
        result = basic_policy.execute(lambda: 42)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_success_metadata_fallback_used_false(self, basic_policy):
        """func 성공 시 metadata['fallback_used']는 False이다."""
        result = basic_policy.execute(lambda: 42)
        assert result.metadata["fallback_used"] is False

    def test_fallback_fn_outcome_is_success_with_fallback(self, basic_policy):
        """fallback_fn 사용 시 outcome은 PolicyOutcome.SUCCESS_WITH_FALLBACK이다."""

        def failing():
            raise ValueError("fail")

        result = basic_policy.execute(failing)
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_fallback_fn_metadata_fallback_used_true(self, basic_policy):
        """fallback_fn 사용 시 metadata['fallback_used']는 True이다."""

        def failing():
            raise ValueError("fail")

        result = basic_policy.execute(failing)
        assert result.metadata["fallback_used"] is True

    def test_fallback_fn_metadata_fallback_source(self, basic_policy):
        """fallback_fn 사용 시 metadata['fallback_source']는 'fallback_fn'이다."""

        def failing():
            raise ValueError("fail")

        result = basic_policy.execute(failing)
        assert result.metadata["fallback_source"] == "fallback_fn"

    def test_chain_metadata_fallback_index(self, chain_policy):
        """fallback_chain 사용 시 metadata['fallback_index']가 설정된다."""

        def failing():
            raise ValueError("fail")

        result = chain_policy.execute(failing)
        assert result.metadata["fallback_index"] == 0

    def test_default_value_metadata_fallback_source(self):
        """default_value 사용 시 metadata['fallback_source']는 'default_value'이다."""
        policy = FallbackPolicy(default_value="default")

        def failing():
            raise ValueError("fail")

        result = policy.execute(failing)
        assert result.metadata["fallback_source"] == "default_value"

    def test_all_exhausted_metadata(self):
        """모든 fallback 소진 시 metadata['all_fallbacks_exhausted']는 True이다."""
        policy = FallbackPolicy()

        def failing():
            raise ValueError("fail")

        result = policy.execute(failing)
        assert result.metadata["all_fallbacks_exhausted"] is True

    def test_all_exhausted_outcome_is_failure(self):
        """모든 fallback 소진 시 outcome은 PolicyOutcome.FAILURE이다."""
        policy = FallbackPolicy()

        def failing():
            raise ValueError("fail")

        result = policy.execute(failing)
        assert result.outcome == PolicyOutcome.FAILURE

    def test_result_is_policy_result_instance(self, basic_policy):
        """반환 타입은 PolicyResult이다."""
        result = basic_policy.execute(lambda: "ok")
        assert isinstance(result, PolicyResult)

    def test_original_error_in_metadata(self, basic_policy):
        """fallback 사용 시 metadata['original_error']에 원본 에러 문자열 포함."""

        def failing():
            raise ValueError("test_error_message")

        result = basic_policy.execute(failing)
        assert "test_error_message" in result.metadata["original_error"]


# =============================================================================
# 계약 검증 (Contract) — _FALLBACK_MODE_TO_OUTCOME 매핑 테이블
# =============================================================================


class TestFallbackModeToOutcomeMappingContract:
    """_FALLBACK_MODE_TO_OUTCOME 매핑 테이블 계약 검증."""

    def test_fail_fast_maps_to_failure(self):
        """fail_fast → PolicyOutcome.FAILURE."""
        assert _FALLBACK_MODE_TO_OUTCOME["fail_fast"] == PolicyOutcome.FAILURE

    def test_use_cache_maps_to_success_with_fallback(self):
        """use_cache → PolicyOutcome.SUCCESS_WITH_FALLBACK."""
        assert (
            _FALLBACK_MODE_TO_OUTCOME["use_cache"]
            == PolicyOutcome.SUCCESS_WITH_FALLBACK
        )

    def test_use_default_maps_to_success_with_fallback(self):
        """use_default → PolicyOutcome.SUCCESS_WITH_FALLBACK."""
        assert (
            _FALLBACK_MODE_TO_OUTCOME["use_default"]
            == PolicyOutcome.SUCCESS_WITH_FALLBACK
        )

    def test_degrade_maps_to_success_with_fallback(self):
        """degrade → PolicyOutcome.SUCCESS_WITH_FALLBACK."""
        assert (
            _FALLBACK_MODE_TO_OUTCOME["degrade"] == PolicyOutcome.SUCCESS_WITH_FALLBACK
        )

    def test_retry_alt_maps_to_success_with_fallback(self):
        """retry_alt → PolicyOutcome.SUCCESS_WITH_FALLBACK."""
        assert (
            _FALLBACK_MODE_TO_OUTCOME["retry_alt"]
            == PolicyOutcome.SUCCESS_WITH_FALLBACK
        )

    def test_hedge_maps_to_success_with_fallback(self):
        """hedge → PolicyOutcome.SUCCESS_WITH_FALLBACK."""
        assert _FALLBACK_MODE_TO_OUTCOME["hedge"] == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_mapping_covers_all_fallback_modes(self):
        """매핑 테이블은 FallbackMode의 모든 멤버를 포함한다."""
        all_mode_values = {mode.value for mode in FallbackMode}
        mapped_keys = set(_FALLBACK_MODE_TO_OUTCOME.keys())
        assert mapped_keys == all_mode_values

    def test_mapping_has_exactly_6_entries(self):
        """매핑 테이블은 정확히 6개 항목을 가진다."""
        assert len(_FALLBACK_MODE_TO_OUTCOME) == 6


# =============================================================================
# 계약 검증 (Contract) — AsyncFallbackPolicy 고정 식별자
# =============================================================================


class TestAsyncFallbackPolicyContract:
    """AsyncFallbackPolicy 고정 식별자 및 결과 구조 계약 검증."""

    def test_name_is_fallback(self, async_basic_policy):
        """name property는 'fallback'이다."""
        assert async_basic_policy.name == "fallback"

    @pytest.mark.asyncio
    async def test_success_result_has_fallback_in_executed_policies(
        self, async_basic_policy
    ):
        """성공 결과의 executed_policies에 'fallback'가 포함된다."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert "fallback" in result.executed_policies

    @pytest.mark.asyncio
    async def test_success_outcome_is_success(self, async_basic_policy):
        """func 성공 시 outcome은 PolicyOutcome.SUCCESS이다."""

        async def ok():
            return 42

        result = await async_basic_policy.execute(ok)
        assert result.outcome == PolicyOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_fallback_fn_outcome(self, async_basic_policy):
        """fallback_fn 사용 시 outcome은 SUCCESS_WITH_FALLBACK이다."""

        async def failing():
            raise ValueError("fail")

        result = await async_basic_policy.execute(failing)
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    @pytest.mark.asyncio
    async def test_result_is_policy_result_instance(self, async_basic_policy):
        """반환 타입은 PolicyResult이다."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert isinstance(result, PolicyResult)


# =============================================================================
# 계약 검증 (Contract) — 패키지 Export
# =============================================================================


class TestPoliciesPackageExportContract:
    """resilience/policies/__init__.py export 계약 검증."""

    def test_fallback_policy_exported(self):
        """FallbackPolicy가 패키지에서 export된다."""
        from baldur.resilience.policies import FallbackPolicy as Exported

        assert Exported is FallbackPolicy

    def test_async_fallback_policy_exported(self):
        """AsyncFallbackPolicy가 패키지에서 export된다."""
        from baldur.resilience.policies import AsyncFallbackPolicy as Exported

        assert Exported is AsyncFallbackPolicy

    def test_partition_aware_chain_exported(self):
        """partition_aware_chain이 패키지에서 export된다."""
        from baldur.resilience.policies import partition_aware_chain as Exported

        assert Exported is partition_aware_chain

    def test_all_contains_exact_count(self):
        """__all__ contains exactly 35 entries (PRO-backed names not advertised)."""
        import baldur.resilience.policies as pkg

        assert len(pkg.__all__) == 35

    def test_all_contains_expected_names(self):
        """__all__에 FallbackPolicy, AsyncFallbackPolicy, partition_aware_chain이 포함된다."""
        import baldur.resilience.policies as pkg

        assert "FallbackPolicy" in pkg.__all__
        assert "AsyncFallbackPolicy" in pkg.__all__
        assert "partition_aware_chain" in pkg.__all__


# =============================================================================
# 동작 검증 (Behavior) — FallbackPolicy execute() 성공 경로
# =============================================================================


class TestFallbackPolicyExecuteSuccessBehavior:
    """FallbackPolicy.execute() 성공 경로 동작 검증."""

    def test_func_return_value_preserved(self, basic_policy):
        """func의 반환값이 PolicyResult.value에 보존된다."""
        result = basic_policy.execute(lambda: {"key": "value"})
        assert result.value == {"key": "value"}

    def test_func_with_args(self, basic_policy):
        """func에 *args가 전달된다."""
        result = basic_policy.execute(lambda x, y: x + y, 3, 7)
        assert result.value == 10

    def test_func_with_kwargs(self, basic_policy):
        """func에 **kwargs가 전달된다."""
        result = basic_policy.execute(lambda x=0: x * 2, x=5)
        assert result.value == 10

    def test_func_returning_none_is_success(self, basic_policy):
        """func이 None을 반환해도 SUCCESS이다."""
        result = basic_policy.execute(lambda: None)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value is None

    def test_success_property_true_on_success(self, basic_policy):
        """성공 시 PolicyResult.success property는 True이다."""
        result = basic_policy.execute(lambda: "ok")
        assert result.success is True


# =============================================================================
# 동작 검증 (Behavior) — FallbackPolicy execute() 실패 → fallback 경로
# =============================================================================


class TestFallbackPolicyExecuteFailureBehavior:
    """FallbackPolicy.execute() 실패 경로 동작 검증."""

    def test_fallback_fn_called_on_exception(self, basic_policy):
        """func 예외 시 fallback_fn이 호출된다."""

        def failing():
            raise RuntimeError("primary failed")

        result = basic_policy.execute(failing)
        assert result.value == "fallback_value"

    def test_fallback_chain_first_success(self, chain_policy):
        """fallback_chain[0]이 성공하면 즉시 반환된다."""

        def failing():
            raise RuntimeError("fail")

        result = chain_policy.execute(failing)
        assert result.value == "chain_0"

    def test_fallback_chain_skips_to_next_on_failure(self):
        """chain[0] 실패 시 chain[1]이 시도된다."""

        def failing_chain_0():
            raise RuntimeError("chain_0 failed")

        policy = FallbackPolicy(
            fallback_chain=[failing_chain_0, lambda: "chain_1_ok"],
        )

        def failing():
            raise RuntimeError("primary failed")

        result = policy.execute(failing)
        assert result.value == "chain_1_ok"
        assert result.metadata["fallback_index"] == 1

    def test_chain_exhausted_then_fallback_fn(self):
        """chain 모두 실패 시 fallback_fn이 시도된다."""

        def failing_chain():
            raise RuntimeError("chain failed")

        policy = FallbackPolicy(
            fallback_chain=[failing_chain],
            fallback_fn=lambda: "fn_result",
        )

        def failing():
            raise RuntimeError("primary failed")

        result = policy.execute(failing)
        assert result.value == "fn_result"
        assert result.metadata["fallback_source"] == "fallback_fn"

    def test_chain_and_fn_exhausted_then_default(self):
        """chain과 fallback_fn 모두 실패 시 default_value가 반환된다."""

        def failing():
            raise RuntimeError("fail")

        def failing_chain():
            raise RuntimeError("chain fail")

        def failing_fn():
            raise RuntimeError("fn fail")

        policy = FallbackPolicy(
            fallback_chain=[failing_chain],
            fallback_fn=failing_fn,
            default_value="default_val",
        )

        result = policy.execute(failing)
        assert result.value == "default_val"
        assert result.metadata["fallback_source"] == "default_value"

    def test_all_exhausted_returns_failure_with_original_error(self):
        """모든 fallback 소진 시 원본 예외가 error에 저장된다."""
        policy = FallbackPolicy()

        error = ValueError("original_fail")

        def failing():
            raise error

        result = policy.execute(failing)
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is error

    def test_success_property_true_on_fallback(self, basic_policy):
        """fallback 성공 시 PolicyResult.success property는 True이다."""

        def failing():
            raise RuntimeError("fail")

        result = basic_policy.execute(failing)
        assert result.success is True

    def test_success_property_false_on_all_exhausted(self):
        """모든 fallback 소진 시 PolicyResult.success는 False이다."""
        policy = FallbackPolicy()

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.success is False

    def test_default_value_none_not_treated_as_default(self):
        """default_value가 None이면 default_value 경로를 사용하지 않는다."""
        policy = FallbackPolicy(default_value=None)

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.outcome == PolicyOutcome.FAILURE

    def test_execution_order_chain_before_fn_before_default(self):
        """실행 순서: fallback_chain → fallback_fn → default_value."""
        call_order = []

        def chain_fn():
            call_order.append("chain")
            raise RuntimeError("chain fail")

        def fb_fn():
            call_order.append("fn")
            raise RuntimeError("fn fail")

        policy = FallbackPolicy(
            fallback_chain=[chain_fn],
            fallback_fn=fb_fn,
            default_value="default",
        )

        def failing():
            raise RuntimeError("primary fail")

        result = policy.execute(failing)
        assert call_order == ["chain", "fn"]
        assert result.value == "default"


# =============================================================================
# 동작 검증 (Behavior) — FallbackPolicy._apply_fallback() Composer 전용
# =============================================================================


class TestFallbackPolicyApplyFallbackBehavior:
    """FallbackPolicy._apply_fallback() Composer 전용 경로 동작 검증."""

    def test_apply_fallback_uses_chain(self, chain_policy):
        """_apply_fallback()은 fallback_chain을 시도한다."""
        result = chain_policy._apply_fallback(original_error=RuntimeError("fail"))
        assert result.value == "chain_0"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_apply_fallback_uses_fn(self, basic_policy):
        """_apply_fallback()은 fallback_fn을 시도한다."""
        result = basic_policy._apply_fallback(original_error=RuntimeError("fail"))
        assert result.value == "fallback_value"
        assert result.metadata["fallback_source"] == "fallback_fn"

    def test_apply_fallback_uses_default(self):
        """_apply_fallback()은 default_value를 반환한다."""
        policy = FallbackPolicy(default_value="default_only")
        result = policy._apply_fallback(original_error=RuntimeError("fail"))
        assert result.value == "default_only"
        assert result.metadata["fallback_source"] == "default_value"

    def test_apply_fallback_all_exhausted(self):
        """_apply_fallback()에서 모든 fallback 소진 시 FAILURE 반환."""
        policy = FallbackPolicy()
        error = RuntimeError("original")
        result = policy._apply_fallback(original_error=error)
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is error

    def test_apply_fallback_does_not_execute_func(self):
        """_apply_fallback()은 func를 실행하지 않는다 (Composer 중복 실행 방지)."""
        call_tracker = MagicMock()
        policy = FallbackPolicy(default_value="safe")

        # _apply_fallback은 func를 인자로 받지 않으므로 func 재실행 불가능
        result = policy._apply_fallback(original_error=RuntimeError("fail"))
        call_tracker.assert_not_called()
        assert result.value == "safe"

    def test_apply_fallback_with_context(self, basic_policy):
        """_apply_fallback()은 context를 받아들인다."""
        ctx = PolicyContext(order_id="test-123")
        result = basic_policy._apply_fallback(
            original_error=RuntimeError("fail"),
            context=ctx,
        )
        assert result.value == "fallback_value"


# =============================================================================
# 동작 검증 (Behavior) — FallbackPolicy predicate 커스터마이징
# =============================================================================


class TestFallbackPolicyPredicateBehavior:
    """FallbackPolicy predicate 동작 검증."""

    def test_default_predicate_activates_on_failure(self):
        """기본 predicate는 SUCCESS가 아닌 모든 outcome에서 활성화된다."""
        policy = FallbackPolicy()
        failure_result = PolicyResult(outcome=PolicyOutcome.FAILURE)
        assert policy._predicate(failure_result) is True

    def test_default_predicate_not_activates_on_success(self):
        """기본 predicate는 SUCCESS이면 비활성화된다."""
        policy = FallbackPolicy()
        success_result = PolicyResult(outcome=PolicyOutcome.SUCCESS)
        assert policy._predicate(success_result) is False

    def test_default_predicate_activates_on_rejected(self):
        """기본 predicate는 REJECTED에서 활성화된다."""
        policy = FallbackPolicy()
        rejected_result = PolicyResult(outcome=PolicyOutcome.REJECTED)
        assert policy._predicate(rejected_result) is True

    def test_default_predicate_activates_on_timeout(self):
        """기본 predicate는 TIMEOUT에서 활성화된다."""
        policy = FallbackPolicy()
        timeout_result = PolicyResult(outcome=PolicyOutcome.TIMEOUT)
        assert policy._predicate(timeout_result) is True

    def test_default_predicate_activates_on_success_with_fallback(self):
        """기본 predicate는 SUCCESS_WITH_FALLBACK에서 활성화된다 (SUCCESS만 비활성화)."""
        policy = FallbackPolicy()
        fallback_result = PolicyResult(outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK)
        assert policy._predicate(fallback_result) is True

    def test_custom_predicate_only_rejected(self):
        """커스텀 predicate: REJECTED일 때만 활성화."""
        policy = FallbackPolicy(
            fallback_fn=lambda: "fallback",
            predicate=lambda r: r.outcome == PolicyOutcome.REJECTED,
        )
        rejected = PolicyResult(outcome=PolicyOutcome.REJECTED)
        failure = PolicyResult(outcome=PolicyOutcome.FAILURE)
        assert policy._predicate(rejected) is True
        assert policy._predicate(failure) is False

    def test_custom_predicate_multiple_outcomes(self):
        """커스텀 predicate: FAILURE 및 REJECTED 모두 활성화."""
        policy = FallbackPolicy(
            predicate=lambda r: (
                r.outcome in (PolicyOutcome.FAILURE, PolicyOutcome.REJECTED)
            ),
        )
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.FAILURE)) is True
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.REJECTED)) is True
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.TIMEOUT)) is False


# =============================================================================
# 동작 검증 (Behavior) — FallbackPolicy strategy Shim (과도기)
# =============================================================================


class TestFallbackPolicyStrategyShimBehavior:
    """FallbackPolicy strategy Shim 과도기 동작 검증."""

    def test_strategy_shim_simple_fallback_with_fallback_fn(self):
        """SimpleFallback strategy shim: fallback_fn 경로 동작."""
        strategy = SimpleFallback()
        policy = FallbackPolicy(strategy=strategy)

        # SimpleFallback.execute는 primary 실패 시 fallback_fn이 없으면 FAIL_FAST
        # strategy shim은 FAIL_FAST 시 네이티브 경로로 fall-through
        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        # SimpleFallback에 fallback_fn, default_value 없으므로 FAIL_FAST → 네이티브 FAILURE
        assert result.outcome == PolicyOutcome.FAILURE

    def test_strategy_shim_success_is_passed_through(self, strategy_policy):
        """strategy shim: func 성공 시 strategy 호출 없이 SUCCESS."""
        result = strategy_policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"

    def test_strategy_shim_with_native_fallback(self):
        """strategy FAIL_FAST → 네이티브 fallback_fn으로 fall-through."""
        strategy = SimpleFallback()
        policy = FallbackPolicy(
            strategy=strategy,
            fallback_fn=lambda: "native_fallback",
        )

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.value == "native_fallback"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_convert_fallback_result_mode_preserved(self):
        """_convert_fallback_result: FallbackMode가 metadata에 보존된다."""
        fb_result = FallbackResult(
            value="cached",
            used_fallback=True,
            fallback_mode=FallbackMode.USE_CACHE,
            original_error="some error",
        )
        policy_result = FallbackPolicy._convert_fallback_result(
            fb_result, RuntimeError("test")
        )
        assert policy_result.metadata["fallback_mode"] == FallbackMode.USE_CACHE.value

    def test_convert_fallback_result_fail_fast_maps_to_failure(self):
        """_convert_fallback_result: FAIL_FAST → FAILURE outcome."""
        fb_result = FallbackResult(
            value=None,
            used_fallback=True,
            fallback_mode=FallbackMode.FAIL_FAST,
            original_error="error",
        )
        policy_result = FallbackPolicy._convert_fallback_result(
            fb_result, RuntimeError("test")
        )
        assert policy_result.outcome == PolicyOutcome.FAILURE

    def test_convert_fallback_result_use_default_maps_to_success_with_fallback(self):
        """_convert_fallback_result: USE_DEFAULT → SUCCESS_WITH_FALLBACK."""
        fb_result = FallbackResult(
            value="default",
            used_fallback=True,
            fallback_mode=FallbackMode.USE_DEFAULT,
            original_error="error",
        )
        policy_result = FallbackPolicy._convert_fallback_result(
            fb_result, RuntimeError("test")
        )
        assert policy_result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_convert_fallback_result_strategy_shim_flag(self):
        """_convert_fallback_result: metadata['strategy_shim']은 True이다."""
        fb_result = FallbackResult(
            value="val",
            used_fallback=True,
            fallback_mode=FallbackMode.USE_CACHE,
        )
        policy_result = FallbackPolicy._convert_fallback_result(
            fb_result, RuntimeError("test")
        )
        assert policy_result.metadata["strategy_shim"] is True

    def test_convert_fallback_result_not_used_fallback_is_success(self):
        """_convert_fallback_result: used_fallback=False → SUCCESS."""
        fb_result = FallbackResult(
            value="primary_val",
            used_fallback=False,
        )
        policy_result = FallbackPolicy._convert_fallback_result(
            fb_result, RuntimeError("test")
        )
        assert policy_result.outcome == PolicyOutcome.SUCCESS

    def test_convert_fallback_result_original_error_preserved(self):
        """_convert_fallback_result: original_error가 metadata에 보존된다."""
        fb_result = FallbackResult(
            value="val",
            used_fallback=True,
            fallback_mode=FallbackMode.USE_CACHE,
            original_error="preserved_error",
        )
        policy_result = FallbackPolicy._convert_fallback_result(
            fb_result, RuntimeError("test")
        )
        assert policy_result.metadata["original_error"] == "preserved_error"

    def test_convert_fallback_result_failure_has_error(self):
        """_convert_fallback_result: FAILURE outcome 시 error가 설정된다."""
        original = RuntimeError("the_error")
        fb_result = FallbackResult(
            value=None,
            used_fallback=True,
            fallback_mode=FallbackMode.FAIL_FAST,
            original_error="err",
        )
        policy_result = FallbackPolicy._convert_fallback_result(fb_result, original)
        assert policy_result.error is original

    def test_convert_fallback_result_success_has_no_error(self):
        """_convert_fallback_result: SUCCESS outcome 시 error는 None이다."""
        fb_result = FallbackResult(
            value="val",
            used_fallback=True,
            fallback_mode=FallbackMode.USE_CACHE,
        )
        policy_result = FallbackPolicy._convert_fallback_result(
            fb_result, RuntimeError("test")
        )
        assert policy_result.error is None


# =============================================================================
# 동작 검증 (Behavior) — AsyncFallbackPolicy execute()
# =============================================================================


class TestAsyncFallbackPolicyExecuteBehavior:
    """AsyncFallbackPolicy.execute() 동작 검증."""

    @pytest.mark.asyncio
    async def test_func_return_value_preserved(self, async_basic_policy):
        """async func의 반환값이 보존된다."""

        async def ok():
            return {"key": "async_value"}

        result = await async_basic_policy.execute(ok)
        assert result.value == {"key": "async_value"}

    @pytest.mark.asyncio
    async def test_fallback_fn_on_exception(self, async_basic_policy):
        """func 예외 시 async fallback_fn이 호출된다."""

        async def failing():
            raise RuntimeError("async fail")

        result = await async_basic_policy.execute(failing)
        assert result.value == "async_fallback_value"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    @pytest.mark.asyncio
    async def test_fallback_chain_first_success(self, async_chain_policy):
        """async fallback_chain[0] 성공 시 즉시 반환."""

        async def failing():
            raise RuntimeError("fail")

        result = await async_chain_policy.execute(failing)
        assert result.value == "async_chain_0"
        assert result.metadata["fallback_index"] == 0

    @pytest.mark.asyncio
    async def test_fallback_chain_skips_to_next(self):
        """async chain[0] 실패 시 chain[1] 시도."""

        async def failing_chain_0():
            raise RuntimeError("chain_0 fail")

        async def chain_1():
            return "chain_1_ok"

        policy = AsyncFallbackPolicy(
            fallback_chain=[failing_chain_0, chain_1],
        )

        async def failing():
            raise RuntimeError("primary fail")

        result = await policy.execute(failing)
        assert result.value == "chain_1_ok"
        assert result.metadata["fallback_index"] == 1

    @pytest.mark.asyncio
    async def test_default_value_on_all_failure(self, async_chain_policy):
        """async chain 모두 실패 시 default_value 반환."""

        async def failing_chain_0():
            raise RuntimeError("fail")

        async def failing_chain_1():
            raise RuntimeError("fail")

        policy = AsyncFallbackPolicy(
            fallback_chain=[failing_chain_0, failing_chain_1],
            default_value="async_default",
        )

        async def failing():
            raise RuntimeError("primary fail")

        result = await policy.execute(failing)
        assert result.value == "async_default"

    @pytest.mark.asyncio
    async def test_all_exhausted_returns_failure(self):
        """async 모든 fallback 소진 시 FAILURE 반환."""
        policy = AsyncFallbackPolicy()

        async def failing():
            raise ValueError("original")

        result = await policy.execute(failing)
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["all_fallbacks_exhausted"] is True

    @pytest.mark.asyncio
    async def test_func_with_args(self, async_basic_policy):
        """async func에 *args가 전달된다."""

        async def add(x, y):
            return x + y

        result = await async_basic_policy.execute(add, 3, 7)
        assert result.value == 10

    @pytest.mark.asyncio
    async def test_func_with_kwargs(self, async_basic_policy):
        """async func에 **kwargs가 전달된다."""

        async def mul(x=0):
            return x * 2

        result = await async_basic_policy.execute(mul, x=5)
        assert result.value == 10

    @pytest.mark.asyncio
    async def test_success_metadata_fallback_used_false(self, async_basic_policy):
        """async 성공 시 metadata['fallback_used']는 False이다."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert result.metadata["fallback_used"] is False


# =============================================================================
# 동작 검증 (Behavior) — AsyncFallbackPolicy._apply_fallback()
# =============================================================================


class TestAsyncFallbackPolicyApplyFallbackBehavior:
    """AsyncFallbackPolicy._apply_fallback() 동작 검증."""

    @pytest.mark.asyncio
    async def test_apply_fallback_uses_chain(self, async_chain_policy):
        """async _apply_fallback은 chain을 시도한다."""
        result = await async_chain_policy._apply_fallback(
            original_error=RuntimeError("fail")
        )
        assert result.value == "async_chain_0"

    @pytest.mark.asyncio
    async def test_apply_fallback_uses_fn(self, async_basic_policy):
        """async _apply_fallback은 fallback_fn을 시도한다."""
        result = await async_basic_policy._apply_fallback(
            original_error=RuntimeError("fail")
        )
        assert result.value == "async_fallback_value"

    @pytest.mark.asyncio
    async def test_apply_fallback_uses_default(self):
        """async _apply_fallback은 default_value를 반환한다."""
        policy = AsyncFallbackPolicy(default_value="default_only")
        result = await policy._apply_fallback(original_error=RuntimeError("fail"))
        assert result.value == "default_only"

    @pytest.mark.asyncio
    async def test_apply_fallback_all_exhausted(self):
        """async _apply_fallback 모든 fallback 소진 시 FAILURE."""
        policy = AsyncFallbackPolicy()
        error = RuntimeError("original")
        result = await policy._apply_fallback(original_error=error)
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is error


# =============================================================================
# 동작 검증 (Behavior) — AsyncFallbackPolicy predicate
# =============================================================================


class TestAsyncFallbackPolicyPredicateBehavior:
    """AsyncFallbackPolicy predicate 동작 검증."""

    def test_default_predicate_activates_on_failure(self):
        """비동기 기본 predicate는 FAILURE에서 활성화된다."""
        policy = AsyncFallbackPolicy()
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.FAILURE)) is True

    def test_default_predicate_not_activates_on_success(self):
        """비동기 기본 predicate는 SUCCESS에서 비활성화된다."""
        policy = AsyncFallbackPolicy()
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.SUCCESS)) is False

    def test_custom_predicate_applied(self):
        """비동기 커스텀 predicate가 적용된다."""
        policy = AsyncFallbackPolicy(
            predicate=lambda r: r.outcome == PolicyOutcome.TIMEOUT,
        )
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.TIMEOUT)) is True
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.FAILURE)) is False


# =============================================================================
# 동작 검증 (Behavior) — partition_aware_chain
# =============================================================================


@dataclass
class MockPartitionState:
    """PartitionState 호환 mock. 실행 시 최신 상태 반영."""

    db_available: bool = True
    cache_available: bool = True
    external_apis: dict[str, bool] = field(default_factory=dict)


class TestPartitionAwareChainBehavior:
    """partition_aware_chain 헬퍼 동작 검증."""

    def test_returns_two_callables_when_both_fns(self):
        """cache_fn과 db_fn 모두 제공 시 2개 callable 반환."""
        state = MockPartitionState()
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cache",
            db_fn=lambda: "db",
        )
        assert len(chain) == 2

    def test_returns_one_callable_cache_only(self):
        """cache_fn만 제공 시 1개 callable 반환."""
        state = MockPartitionState()
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cache",
        )
        assert len(chain) == 1

    def test_returns_one_callable_db_only(self):
        """db_fn만 제공 시 1개 callable 반환."""
        state = MockPartitionState()
        chain = partition_aware_chain(
            state_provider=lambda: state,
            db_fn=lambda: "db",
        )
        assert len(chain) == 1

    def test_returns_empty_when_no_fns(self):
        """cache_fn, db_fn 모두 미제공 시 빈 리스트 반환."""
        state = MockPartitionState()
        chain = partition_aware_chain(state_provider=lambda: state)
        assert chain == []

    def test_cache_fn_called_when_available(self):
        """cache_available=True이면 cache_fn이 호출된다."""
        state = MockPartitionState(cache_available=True)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cached_data",
        )
        assert chain[0]() == "cached_data"

    def test_cache_fn_raises_when_unavailable(self):
        """cache_available=False이면 RuntimeError가 발생한다."""
        state = MockPartitionState(cache_available=False)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cached_data",
        )
        with pytest.raises(RuntimeError, match="Cache unavailable"):
            chain[0]()

    def test_db_fn_called_when_available(self):
        """db_available=True이면 db_fn이 호출된다."""
        state = MockPartitionState(db_available=True)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            db_fn=lambda: "db_data",
        )
        # db_fn은 cache_fn이 없을 때 index 0
        assert chain[0]() == "db_data"

    def test_db_fn_raises_when_unavailable(self):
        """db_available=False이면 RuntimeError가 발생한다."""
        state = MockPartitionState(db_available=False)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            db_fn=lambda: "db_data",
        )
        with pytest.raises(RuntimeError, match="DB unavailable"):
            chain[0]()

    def test_state_provider_called_at_execution_time(self):
        """state_provider는 chain 함수 실행 시점에 호출된다 (Stale State 방지)."""
        state = MockPartitionState(cache_available=True)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cached",
        )
        # chain 생성 후 상태 변경
        state.cache_available = False
        with pytest.raises(RuntimeError, match="Cache unavailable"):
            chain[0]()

    def test_state_provider_dynamic_recovery(self):
        """상태가 복구되면 fallback 함수도 성공한다."""
        state = MockPartitionState(cache_available=False)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "recovered",
        )
        # 실패 확인
        with pytest.raises(RuntimeError):
            chain[0]()
        # 상태 복구
        state.cache_available = True
        assert chain[0]() == "recovered"

    def test_chain_order_cache_before_db(self):
        """chain 순서: cache가 db 앞에 온다."""
        state = MockPartitionState(cache_available=True, db_available=True)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cache_result",
            db_fn=lambda: "db_result",
        )
        assert chain[0]() == "cache_result"
        assert chain[1]() == "db_result"

    def test_integration_with_fallback_policy(self):
        """partition_aware_chain을 FallbackPolicy와 통합 사용."""
        state = MockPartitionState(cache_available=False, db_available=True)
        policy = FallbackPolicy(
            fallback_chain=partition_aware_chain(
                state_provider=lambda: state,
                cache_fn=lambda: "cache",
                db_fn=lambda: "db",
            ),
            default_value="degraded",
        )

        def failing():
            raise RuntimeError("primary fail")

        result = policy.execute(failing)
        # cache 불가 → db 사용
        assert result.value == "db"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_integration_all_unavailable_falls_to_default(self):
        """cache/db 모두 불가 시 default_value로 fallback."""
        state = MockPartitionState(cache_available=False, db_available=False)
        policy = FallbackPolicy(
            fallback_chain=partition_aware_chain(
                state_provider=lambda: state,
                cache_fn=lambda: "cache",
                db_fn=lambda: "db",
            ),
            default_value="degraded",
        )

        def failing():
            raise RuntimeError("primary fail")

        result = policy.execute(failing)
        assert result.value == "degraded"


# =============================================================================
# 동작 검증 (Behavior) — FallbackPolicy 예외 처리 컨트랙트
# =============================================================================


class TestFallbackPolicyExceptionHandlingBehavior:
    """FallbackPolicy 예외 흡수 동작 검증."""

    def test_execute_never_raises(self, basic_policy):
        """execute()는 모든 예외를 흡수하여 PolicyResult로 반환한다."""

        def failing():
            raise RuntimeError("should be absorbed")

        result = basic_policy.execute(failing)
        # 예외가 흡수되고 PolicyResult가 반환됨
        assert isinstance(result, PolicyResult)

    def test_execute_absorbs_various_exceptions(self):
        """execute()는 다양한 예외 타입을 흡수한다."""
        policy = FallbackPolicy(default_value="safe")
        exceptions = [ValueError, TypeError, IOError, KeyError, AttributeError]

        for exc_type in exceptions:

            def failing(e=exc_type):
                raise e("test")

            result = policy.execute(failing)
            assert result.success is True
            assert result.value == "safe"

    def test_apply_fallback_never_raises(self):
        """_apply_fallback()은 예외를 던지지 않는다."""
        policy = FallbackPolicy()
        result = policy._apply_fallback(original_error=RuntimeError("test"))
        assert isinstance(result, PolicyResult)


# =============================================================================
# 동작 검증 (Behavior) — FallbackPolicy 엣지 케이스
# =============================================================================


class TestFallbackPolicyEdgeCaseBehavior:
    """FallbackPolicy 엣지 케이스 동작 검증."""

    def test_empty_fallback_chain(self):
        """빈 fallback_chain은 건너뛴다."""
        policy = FallbackPolicy(
            fallback_chain=[],
            fallback_fn=lambda: "fn_result",
        )

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.value == "fn_result"

    def test_none_strategy_uses_native_path(self):
        """strategy=None이면 네이티브 경로를 사용한다."""
        policy = FallbackPolicy(
            strategy=None,
            fallback_fn=lambda: "native",
        )

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.value == "native"
        assert "strategy_shim" not in result.metadata

    def test_context_parameter_accepted(self, basic_policy):
        """execute()는 context 파라미터를 받아들인다."""
        ctx = PolicyContext(order_id="ctx-123")
        result = basic_policy.execute(lambda: "ok", context=ctx)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_default_value_zero_is_valid(self):
        """default_value=0은 유효한 default_value이다 (None이 아님)."""
        policy = FallbackPolicy(default_value=0)

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.value == 0
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_default_value_empty_string_is_valid(self):
        """default_value=''은 유효한 default_value이다."""
        policy = FallbackPolicy(default_value="")

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.value == ""
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_default_value_false_is_valid(self):
        """default_value=False는 유효한 default_value이다."""
        policy = FallbackPolicy(default_value=False)

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        # default_value가 None이 아니므로 (False != None) default 경로 사용
        # 그러나 코드가 `if self._default_value is not None`를 체크하므로 False는 통과
        assert result.value is False
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK


# =============================================================================
# Contract — arity cache size (705 D3, execution-note leak-avoidance bound)
# =============================================================================


class TestFallbackArityCacheSizeContract:
    """The arity resolver is memoized with a bounded, named cache size so a
    per-call lambda fallback cannot grow the cache without bound."""

    def test_arity_cache_size_is_1024(self):
        """Contract: the named bound is 1024."""
        assert _FALLBACK_ARITY_CACHE_SIZE == 1024

    def test_arity_helper_lru_cache_uses_the_named_size(self):
        """The lru_cache maxsize is wired from the named constant (not unbounded)."""
        assert (
            _fallback_accepts_error.cache_info().maxsize == _FALLBACK_ARITY_CACHE_SIZE
        )


# =============================================================================
# Behavior — arity detection (705 D3): call-shape → error-accepting?
# =============================================================================


def _two_required(a, b):
    """A 2-required-positional callable — rejected as a fallback (fail-loud)."""
    return (a, b)


class _BoundMethodHost:
    """Host for bound-method arity cases (``self`` is already bound off)."""

    def zero_extra(self):
        return "zero"

    def one_extra(self, error):
        return error


class _VarArgsCallable:
    """A callable object whose ``__call__`` is ``(*args, **kwargs)`` — the
    inspectable var-args shape (also what a bare ``Mock``/``MagicMock`` reports),
    which the arity resolver classifies as error-accepting."""

    def __call__(self, *args, **kwargs):
        return "var-args"


# (callable, expected accepts_error) — see _fallback_accepts_error docstring.
_ARITY_SHAPE_CASES = [
    ("zero_arg_lambda", lambda: "x", False),
    ("one_required_lambda", lambda error: error, True),
    ("var_positional", lambda *args: args, True),
    ("var_keyword_only", lambda **kwargs: kwargs, True),
    ("partial_leaves_one_required", functools.partial(_two_required, 1), True),
    ("partial_leaves_zero_required", functools.partial(_two_required, 1, 2), False),
    ("bound_method_zero_extra", _BoundMethodHost().zero_extra, False),
    ("bound_method_one_extra", _BoundMethodHost().one_extra, True),
    ("uninspectable_builtin_type", str, False),
    ("var_args_call_object", _VarArgsCallable(), True),
]


class TestFallbackArityDetection:
    """``_fallback_accepts_error`` resolves the fallback call shape once per
    callable identity: zero required positional (no ``*args``) → legacy zero-arg;
    one required, ``*args``, or ``**kwargs``-only → error-accepting; an
    uninspectable callable degrades safely to zero-arg; >= 2 required → ValueError."""

    @pytest.mark.parametrize(
        ("callable_obj", "expected"),
        [(c, e) for _id, c, e in _ARITY_SHAPE_CASES],
        ids=[_id for _id, _c, _e in _ARITY_SHAPE_CASES],
    )
    def test_arity_shape_maps_to_error_accepting_flag(self, callable_obj, expected):
        """Each call shape resolves to the documented error-accepting flag."""
        assert _fallback_accepts_error(callable_obj) is expected

    def test_arity_uninspectable_builtin_degrades_to_zero_arg(self):
        """A signature-uninspectable callable (builtin type) degrades to zero-arg
        (the ``except (ValueError, TypeError)`` guard), never raising."""
        # ``str`` raises ValueError from inspect.signature; the guard returns False.
        assert _fallback_accepts_error(str) is False

    def test_arity_two_required_positional_raises_valueerror_at_construction(self):
        """>= 2 required positional → fail loud at construction (ValueError), not
        a runtime TypeError mid-incident."""
        with pytest.raises(ValueError, match="required positional"):
            FallbackPolicy(fallback_fn=_two_required)

    def test_arity_two_required_in_chain_raises_valueerror(self):
        """A ≥2-required chain entry also fails loud at construction."""
        with pytest.raises(ValueError, match="required positional"):
            FallbackPolicy(fallback_chain=[_two_required])

    def test_async_arity_two_required_positional_raises_valueerror(self):
        """The async twin fails loud identically on a ≥2-required fallback."""
        with pytest.raises(ValueError, match="required positional"):
            AsyncFallbackPolicy(fallback_fn=_two_required)


# =============================================================================
# Behavior — error-aware fallback invocation (705 D3)
# =============================================================================


class TestFallbackErrorAwareInvocation:
    """A one-arg fallback receives the caught error positionally; a zero-arg
    fallback is called with no args (legacy shape unchanged)."""

    def test_error_aware_fallback_fn_receives_original_error(self):
        """``fb(error)`` gets the exact caught exception object."""
        received: dict[str, BaseException] = {}

        def fb(error):
            received["error"] = error
            return "served"

        policy = FallbackPolicy(fallback_fn=fb)
        err = RuntimeError("boom")

        result = policy._apply_fallback(original_error=err)

        assert result.value == "served"
        assert received["error"] is err

    def test_zero_arg_fallback_fn_called_without_error(self):
        """A zero-arg fallback runs with no positional argument (legacy shape)."""
        calls = {"n": 0}

        def fb():
            calls["n"] += 1
            return "served"

        policy = FallbackPolicy(fallback_fn=fb)

        result = policy._apply_fallback(original_error=ValueError("x"))

        assert result.value == "served"
        assert calls["n"] == 1

    def test_error_aware_chain_entry_receives_error(self):
        """A one-arg fallback_chain entry also receives the caught error."""
        seen: list[BaseException] = []

        policy = FallbackPolicy(
            fallback_chain=[lambda error: seen.append(error) or "chain"],
        )
        err = KeyError("k")

        result = policy._apply_fallback(original_error=err)

        assert result.value == "chain"
        assert seen == [err]

    def test_error_aware_fallback_branches_on_error_type(self):
        """The SC error-type branch: serve on TimeoutPolicyError, re-raise (→
        decline) on any other error type."""

        def fb(error):
            if isinstance(error, TimeoutPolicyError):
                return "stale-on-timeout"
            raise error

        policy = FallbackPolicy(fallback_fn=fb)

        # TimeoutPolicyError → the fallback serves its degraded value.
        served = policy._apply_fallback(original_error=TimeoutPolicyError(1.0))
        assert served.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert served.value == "stale-on-timeout"

        # Any other error → the fallback re-raises → no value → FAILURE.
        declined = policy._apply_fallback(original_error=RuntimeError("boom"))
        assert declined.outcome == PolicyOutcome.FAILURE

    @pytest.mark.asyncio
    async def test_async_error_aware_fallback_fn_receives_error(self):
        """The async twin passes the caught error to a one-arg async fallback."""
        received: dict[str, BaseException] = {}

        async def fb(error):
            received["error"] = error
            return "served"

        policy = AsyncFallbackPolicy(fallback_fn=fb)
        err = RuntimeError("boom")

        result = await policy._apply_fallback(original_error=err)

        assert result.value == "served"
        assert received["error"] is err

    @pytest.mark.asyncio
    async def test_async_zero_arg_fallback_fn_called_without_error(self):
        """A zero-arg async fallback is awaited with no positional argument."""
        calls = {"n": 0}

        async def fb():
            calls["n"] += 1
            return "served"

        policy = AsyncFallbackPolicy(fallback_fn=fb)

        result = await policy._apply_fallback(original_error=ValueError("x"))

        assert result.value == "served"
        assert calls["n"] == 1


# =============================================================================
# Behavior — standalone execute() honors the predicate (705 D5/D6)
# =============================================================================


class TestFallbackStandalonePredicate:
    """``FallbackPolicy.execute`` / ``AsyncFallbackPolicy.execute`` consult the
    predicate against the D6-classified outcome before applying the fallback;
    a declining predicate re-surfaces the original error as FAILURE."""

    def test_execute_default_predicate_still_serves_on_failure(self):
        """Default-predicate users see no behavior change — any non-SUCCESS
        activates the fallback exactly as before."""
        policy = FallbackPolicy(fallback_fn=lambda: "fb")

        result = policy.execute(lambda: (_ for _ in ()).throw(RuntimeError("x")))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "fb"

    def test_execute_declining_predicate_returns_failure_without_fallback(self):
        """A predicate that declines the classified outcome makes execute()
        return FAILURE with the original error and never runs the fallback."""
        calls = {"n": 0}

        def fb():
            calls["n"] += 1
            return "fb"

        # TIMEOUT-only predicate; a plain RuntimeError classifies as FAILURE.
        policy = FallbackPolicy(
            fallback_fn=fb,
            predicate=lambda r: r.outcome == PolicyOutcome.TIMEOUT,
        )
        err = RuntimeError("boom")

        result = policy.execute(lambda: (_ for _ in ()).throw(err))

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is err
        assert result.metadata["fallback_used"] is False
        assert calls["n"] == 0  # predicate declined → fallback never ran

    @pytest.mark.parametrize(
        ("raised_exc", "expect_served"),
        [
            (TimeoutPolicyError(1.0), True),
            (PolicyRejectedException("rejected"), False),
            (RuntimeError("boom"), False),
        ],
        ids=["timeout_served", "rejected_declined", "failure_declined"],
    )
    def test_execute_timeout_only_predicate_distinguishes_outcomes(
        self, raised_exc, expect_served
    ):
        """A TIMEOUT-only predicate distinguishes TIMEOUT from REJECTED/FAILURE —
        proving execute() feeds the classified outcome, not always-FAILURE."""
        policy = FallbackPolicy(
            default_value="degraded",
            predicate=lambda r: r.outcome == PolicyOutcome.TIMEOUT,
        )

        result = policy.execute(lambda: (_ for _ in ()).throw(raised_exc))

        if expect_served:
            assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        else:
            assert result.outcome == PolicyOutcome.FAILURE

    def test_execute_rejected_only_predicate_serves_rejected_declines_timeout(self):
        """A REJECTED-only predicate serves on a rejection but declines a timeout
        — the mirror direction, confirming REJECTED is distinguishable."""
        policy = FallbackPolicy(
            default_value="degraded",
            predicate=lambda r: r.outcome == PolicyOutcome.REJECTED,
        )

        served = policy.execute(
            lambda: (_ for _ in ()).throw(PolicyRejectedException("r"))
        )
        assert served.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

        declined = policy.execute(
            lambda: (_ for _ in ()).throw(TimeoutPolicyError(1.0))
        )
        assert declined.outcome == PolicyOutcome.FAILURE

    @pytest.mark.asyncio
    async def test_async_execute_declining_predicate_returns_failure(self):
        """The async twin honors a declining predicate identically."""
        calls = {"n": 0}

        async def fb():
            calls["n"] += 1
            return "fb"

        policy = AsyncFallbackPolicy(
            fallback_fn=fb,
            predicate=lambda r: r.outcome == PolicyOutcome.TIMEOUT,
        )

        async def bad():
            raise RuntimeError("boom")

        result = await policy.execute(bad)

        assert result.outcome == PolicyOutcome.FAILURE
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_async_execute_timeout_predicate_serves_on_timeout(self):
        """The async twin serves the fallback when the classified TIMEOUT matches
        a TIMEOUT-only predicate."""
        policy = AsyncFallbackPolicy(
            default_value="degraded",
            predicate=lambda r: r.outcome == PolicyOutcome.TIMEOUT,
        )

        async def timed_out():
            raise TimeoutPolicyError(1.0)

        result = await policy.execute(timed_out)

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
