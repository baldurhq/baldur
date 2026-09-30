"""
FallbackPolicy / AsyncFallbackPolicy / partition_aware_chain unit tests (#229).

Targets:
- resilience/policies/fallback.py (FallbackPolicy, AsyncFallbackPolicy,
  partition_aware_chain, _FALLBACK_MODE_TO_OUTCOME)
- resilience/policies/__init__.py (export checks)

Follows UNIT_TEST_GUIDELINES.md:
- Contract: hardcoded expectations (name, outcome, executed_policies, mapping table)
- Behavior: source references (PolicyOutcome, _FALLBACK_MODE_TO_OUTCOME, ...)
- conftest.py placement: fixtures used by one file stay in the file (§5.1)
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
# Fixtures — used by this file only, so they live here (§5.1)
# =============================================================================


@pytest.fixture
def basic_policy():
    """A basic FallbackPolicy with only fallback_fn."""
    return FallbackPolicy(fallback_fn=lambda: "fallback_value")


@pytest.fixture
def chain_policy():
    """A FallbackPolicy with fallback_chain + default_value."""
    return FallbackPolicy(
        fallback_chain=[
            lambda: "chain_0",
            lambda: "chain_1",
        ],
        default_value="default",
    )


@pytest.fixture
def full_policy():
    """A FallbackPolicy with fallback_chain, fallback_fn and default_value all set."""
    return FallbackPolicy(
        fallback_chain=[lambda: "chain_result"],
        fallback_fn=lambda: "fn_result",
        default_value="default_result",
    )


@pytest.fixture
def strategy_policy():
    """A FallbackPolicy built on the SimpleFallback strategy shim."""
    return FallbackPolicy(strategy=SimpleFallback())


@pytest.fixture
def async_basic_policy():
    """A basic AsyncFallbackPolicy with only fallback_fn."""

    async def async_fallback():
        return "async_fallback_value"

    return AsyncFallbackPolicy(fallback_fn=async_fallback)


@pytest.fixture
def async_chain_policy():
    """An AsyncFallbackPolicy with fallback_chain + default_value."""

    async def chain_0():
        return "async_chain_0"

    async def chain_1():
        return "async_chain_1"

    return AsyncFallbackPolicy(
        fallback_chain=[chain_0, chain_1],
        default_value="async_default",
    )


# =============================================================================
# Contract — FallbackPolicy fixed identifiers and result structure
# =============================================================================


class TestFallbackPolicyContract:
    """FallbackPolicy fixed identifiers and result structure contract."""

    def test_name_is_fallback(self, basic_policy):
        """The name property is 'fallback'."""
        assert basic_policy.name == "fallback"

    def test_success_result_has_fallback_in_executed_policies(self, basic_policy):
        """A success result's executed_policies contains 'fallback'."""
        result = basic_policy.execute(lambda: "ok")
        assert "fallback" in result.executed_policies

    def test_fallback_result_has_fallback_in_executed_policies(self, basic_policy):
        """A fallback result's executed_policies contains 'fallback'."""
        result = basic_policy.execute(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert "fallback" in result.executed_policies

    def test_success_outcome_is_success(self, basic_policy):
        """When func succeeds the outcome is PolicyOutcome.SUCCESS."""
        result = basic_policy.execute(lambda: 42)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_success_metadata_fallback_used_false(self, basic_policy):
        """When func succeeds metadata['fallback_used'] is False."""
        result = basic_policy.execute(lambda: 42)
        assert result.metadata["fallback_used"] is False

    def test_fallback_fn_outcome_is_success_with_fallback(self, basic_policy):
        """When fallback_fn is used the outcome is PolicyOutcome.SUCCESS_WITH_FALLBACK."""

        def failing():
            raise ValueError("fail")

        result = basic_policy.execute(failing)
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_fallback_fn_metadata_fallback_used_true(self, basic_policy):
        """When fallback_fn is used metadata['fallback_used'] is True."""

        def failing():
            raise ValueError("fail")

        result = basic_policy.execute(failing)
        assert result.metadata["fallback_used"] is True

    def test_fallback_fn_metadata_fallback_source(self, basic_policy):
        """When fallback_fn is used metadata['fallback_source'] is 'fallback_fn'."""

        def failing():
            raise ValueError("fail")

        result = basic_policy.execute(failing)
        assert result.metadata["fallback_source"] == "fallback_fn"

    def test_chain_metadata_fallback_index(self, chain_policy):
        """When fallback_chain is used metadata['fallback_index'] is set."""

        def failing():
            raise ValueError("fail")

        result = chain_policy.execute(failing)
        assert result.metadata["fallback_index"] == 0

    def test_default_value_metadata_fallback_source(self):
        """When default_value is used metadata['fallback_source'] is 'default_value'."""
        policy = FallbackPolicy(default_value="default")

        def failing():
            raise ValueError("fail")

        result = policy.execute(failing)
        assert result.metadata["fallback_source"] == "default_value"

    def test_all_exhausted_metadata(self):
        """When every fallback is exhausted metadata['all_fallbacks_exhausted'] is True."""
        policy = FallbackPolicy()

        def failing():
            raise ValueError("fail")

        result = policy.execute(failing)
        assert result.metadata["all_fallbacks_exhausted"] is True

    def test_all_exhausted_outcome_is_failure(self):
        """When every fallback is exhausted the outcome is PolicyOutcome.FAILURE."""
        policy = FallbackPolicy()

        def failing():
            raise ValueError("fail")

        result = policy.execute(failing)
        assert result.outcome == PolicyOutcome.FAILURE

    def test_result_is_policy_result_instance(self, basic_policy):
        """The return type is PolicyResult."""
        result = basic_policy.execute(lambda: "ok")
        assert isinstance(result, PolicyResult)

    def test_original_error_in_metadata(self, basic_policy):
        """When a fallback is used metadata['original_error'] carries the original error string."""

        def failing():
            raise ValueError("test_error_message")

        result = basic_policy.execute(failing)
        assert "test_error_message" in result.metadata["original_error"]


# =============================================================================
# Contract — the _FALLBACK_MODE_TO_OUTCOME mapping table
# =============================================================================


class TestFallbackModeToOutcomeMappingContract:
    """_FALLBACK_MODE_TO_OUTCOME mapping table contract."""

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
        """The mapping table covers every FallbackMode member."""
        all_mode_values = {mode.value for mode in FallbackMode}
        mapped_keys = set(_FALLBACK_MODE_TO_OUTCOME.keys())
        assert mapped_keys == all_mode_values

    def test_mapping_has_exactly_6_entries(self):
        """The mapping table has exactly 6 entries."""
        assert len(_FALLBACK_MODE_TO_OUTCOME) == 6


# =============================================================================
# Contract — AsyncFallbackPolicy fixed identifiers
# =============================================================================


class TestAsyncFallbackPolicyContract:
    """AsyncFallbackPolicy fixed identifiers and result structure contract."""

    def test_name_is_fallback(self, async_basic_policy):
        """The name property is 'fallback'."""
        assert async_basic_policy.name == "fallback"

    @pytest.mark.asyncio
    async def test_success_result_has_fallback_in_executed_policies(
        self, async_basic_policy
    ):
        """A success result's executed_policies contains 'fallback'."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert "fallback" in result.executed_policies

    @pytest.mark.asyncio
    async def test_success_outcome_is_success(self, async_basic_policy):
        """When func succeeds the outcome is PolicyOutcome.SUCCESS."""

        async def ok():
            return 42

        result = await async_basic_policy.execute(ok)
        assert result.outcome == PolicyOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_fallback_fn_outcome(self, async_basic_policy):
        """When fallback_fn is used the outcome is SUCCESS_WITH_FALLBACK."""

        async def failing():
            raise ValueError("fail")

        result = await async_basic_policy.execute(failing)
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    @pytest.mark.asyncio
    async def test_result_is_policy_result_instance(self, async_basic_policy):
        """The return type is PolicyResult."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert isinstance(result, PolicyResult)


# =============================================================================
# Contract — package exports
# =============================================================================


class TestPoliciesPackageExportContract:
    """resilience/policies/__init__.py export contract."""

    def test_fallback_policy_exported(self):
        """FallbackPolicy is exported from the package."""
        from baldur.resilience.policies import FallbackPolicy as Exported

        assert Exported is FallbackPolicy

    def test_async_fallback_policy_exported(self):
        """AsyncFallbackPolicy is exported from the package."""
        from baldur.resilience.policies import AsyncFallbackPolicy as Exported

        assert Exported is AsyncFallbackPolicy

    def test_partition_aware_chain_exported(self):
        """partition_aware_chain is exported from the package."""
        from baldur.resilience.policies import partition_aware_chain as Exported

        assert Exported is partition_aware_chain

    def test_all_contains_exact_count(self):
        """__all__ contains exactly 34 entries (PRO-backed names not advertised).

        ``KillSwitchGuard`` left the package with 802 D1: a pulled kill switch
        steps Baldur aside through the execution-mode resolver instead.
        """
        import baldur.resilience.policies as pkg

        assert len(pkg.__all__) == 34
        assert "KillSwitchGuard" not in pkg.__all__

    def test_all_contains_expected_names(self):
        """__all__ contains FallbackPolicy, AsyncFallbackPolicy and partition_aware_chain."""
        import baldur.resilience.policies as pkg

        assert "FallbackPolicy" in pkg.__all__
        assert "AsyncFallbackPolicy" in pkg.__all__
        assert "partition_aware_chain" in pkg.__all__


# =============================================================================
# Behavior — FallbackPolicy execute() success path
# =============================================================================


class TestFallbackPolicyExecuteSuccessBehavior:
    """FallbackPolicy.execute() success path behavior."""

    def test_func_return_value_preserved(self, basic_policy):
        """func's return value is kept in PolicyResult.value."""
        result = basic_policy.execute(lambda: {"key": "value"})
        assert result.value == {"key": "value"}

    def test_func_with_args(self, basic_policy):
        """*args reach func."""
        result = basic_policy.execute(lambda x, y: x + y, 3, 7)
        assert result.value == 10

    def test_func_with_kwargs(self, basic_policy):
        """**kwargs reach func."""
        result = basic_policy.execute(lambda x=0: x * 2, x=5)
        assert result.value == 10

    def test_func_returning_none_is_success(self, basic_policy):
        """func returning None is still SUCCESS."""
        result = basic_policy.execute(lambda: None)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value is None

    def test_success_property_true_on_success(self, basic_policy):
        """On success PolicyResult.success is True."""
        result = basic_policy.execute(lambda: "ok")
        assert result.success is True


# =============================================================================
# Behavior — FallbackPolicy execute() failure → fallback path
# =============================================================================


class TestFallbackPolicyExecuteFailureBehavior:
    """FallbackPolicy.execute() failure path behavior."""

    def test_fallback_fn_called_on_exception(self, basic_policy):
        """A func exception calls fallback_fn."""

        def failing():
            raise RuntimeError("primary failed")

        result = basic_policy.execute(failing)
        assert result.value == "fallback_value"

    def test_fallback_chain_first_success(self, chain_policy):
        """When fallback_chain[0] succeeds it returns at once."""

        def failing():
            raise RuntimeError("fail")

        result = chain_policy.execute(failing)
        assert result.value == "chain_0"

    def test_fallback_chain_skips_to_next_on_failure(self):
        """When chain[0] fails chain[1] is tried."""

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
        """When the whole chain fails fallback_fn is tried."""

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
        """When the chain and fallback_fn all fail default_value is returned."""

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
        """When every fallback is exhausted the original exception is kept in error."""
        policy = FallbackPolicy()

        error = ValueError("original_fail")

        def failing():
            raise error

        result = policy.execute(failing)
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is error

    def test_success_property_true_on_fallback(self, basic_policy):
        """When a fallback succeeds PolicyResult.success is True."""

        def failing():
            raise RuntimeError("fail")

        result = basic_policy.execute(failing)
        assert result.success is True

    def test_success_property_false_on_all_exhausted(self):
        """When every fallback is exhausted PolicyResult.success is False."""
        policy = FallbackPolicy()

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.success is False

    def test_default_value_none_not_treated_as_default(self):
        """A default_value of None does not take the default_value path."""
        policy = FallbackPolicy(default_value=None)

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.outcome == PolicyOutcome.FAILURE

    def test_execution_order_chain_before_fn_before_default(self):
        """Order: fallback_chain → fallback_fn → default_value."""
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
# Behavior — FallbackPolicy._apply_fallback() (Composer only)
# =============================================================================


class TestFallbackPolicyApplyFallbackBehavior:
    """FallbackPolicy._apply_fallback() Composer-only path behavior."""

    def test_apply_fallback_uses_chain(self, chain_policy):
        """_apply_fallback() tries fallback_chain."""
        result = chain_policy._apply_fallback(original_error=RuntimeError("fail"))
        assert result.value == "chain_0"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_apply_fallback_uses_fn(self, basic_policy):
        """_apply_fallback() tries fallback_fn."""
        result = basic_policy._apply_fallback(original_error=RuntimeError("fail"))
        assert result.value == "fallback_value"
        assert result.metadata["fallback_source"] == "fallback_fn"

    def test_apply_fallback_uses_default(self):
        """_apply_fallback() returns default_value."""
        policy = FallbackPolicy(default_value="default_only")
        result = policy._apply_fallback(original_error=RuntimeError("fail"))
        assert result.value == "default_only"
        assert result.metadata["fallback_source"] == "default_value"

    def test_apply_fallback_all_exhausted(self):
        """_apply_fallback() returns FAILURE when every fallback is exhausted."""
        policy = FallbackPolicy()
        error = RuntimeError("original")
        result = policy._apply_fallback(original_error=error)
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is error

    def test_apply_fallback_does_not_execute_func(self):
        """_apply_fallback() does not run func (no duplicate run under the Composer)."""
        call_tracker = MagicMock()
        policy = FallbackPolicy(default_value="safe")

        # _apply_fallback takes no func argument, so it cannot re-run func
        result = policy._apply_fallback(original_error=RuntimeError("fail"))
        call_tracker.assert_not_called()
        assert result.value == "safe"

    def test_apply_fallback_with_context(self, basic_policy):
        """_apply_fallback() accepts context."""
        ctx = PolicyContext(order_id="test-123")
        result = basic_policy._apply_fallback(
            original_error=RuntimeError("fail"),
            context=ctx,
        )
        assert result.value == "fallback_value"


# =============================================================================
# Behavior — FallbackPolicy predicate customization
# =============================================================================


class TestFallbackPolicyPredicateBehavior:
    """FallbackPolicy predicate behavior."""

    def test_default_predicate_activates_on_failure(self):
        """The default predicate activates on every outcome except SUCCESS."""
        policy = FallbackPolicy()
        failure_result = PolicyResult(outcome=PolicyOutcome.FAILURE)
        assert policy._predicate(failure_result) is True

    def test_default_predicate_not_activates_on_success(self):
        """The default predicate is inactive on SUCCESS."""
        policy = FallbackPolicy()
        success_result = PolicyResult(outcome=PolicyOutcome.SUCCESS)
        assert policy._predicate(success_result) is False

    def test_default_predicate_activates_on_rejected(self):
        """The default predicate activates on REJECTED."""
        policy = FallbackPolicy()
        rejected_result = PolicyResult(outcome=PolicyOutcome.REJECTED)
        assert policy._predicate(rejected_result) is True

    def test_default_predicate_activates_on_timeout(self):
        """The default predicate activates on TIMEOUT."""
        policy = FallbackPolicy()
        timeout_result = PolicyResult(outcome=PolicyOutcome.TIMEOUT)
        assert policy._predicate(timeout_result) is True

    def test_default_predicate_activates_on_success_with_fallback(self):
        """The default predicate activates on SUCCESS_WITH_FALLBACK (only SUCCESS is inactive)."""
        policy = FallbackPolicy()
        fallback_result = PolicyResult(outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK)
        assert policy._predicate(fallback_result) is True

    def test_custom_predicate_only_rejected(self):
        """Custom predicate: active only on REJECTED."""
        policy = FallbackPolicy(
            fallback_fn=lambda: "fallback",
            predicate=lambda r: r.outcome == PolicyOutcome.REJECTED,
        )
        rejected = PolicyResult(outcome=PolicyOutcome.REJECTED)
        failure = PolicyResult(outcome=PolicyOutcome.FAILURE)
        assert policy._predicate(rejected) is True
        assert policy._predicate(failure) is False

    def test_custom_predicate_multiple_outcomes(self):
        """Custom predicate: active on both FAILURE and REJECTED."""
        policy = FallbackPolicy(
            predicate=lambda r: (
                r.outcome in (PolicyOutcome.FAILURE, PolicyOutcome.REJECTED)
            ),
        )
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.FAILURE)) is True
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.REJECTED)) is True
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.TIMEOUT)) is False


# =============================================================================
# Behavior — FallbackPolicy strategy shim (transitional)
# =============================================================================


class TestFallbackPolicyStrategyShimBehavior:
    """FallbackPolicy strategy shim transitional behavior."""

    def test_strategy_shim_simple_fallback_with_fallback_fn(self):
        """SimpleFallback strategy shim: the fallback_fn path."""
        strategy = SimpleFallback()
        policy = FallbackPolicy(strategy=strategy)

        # SimpleFallback.execute is FAIL_FAST when the primary fails and there is no fallback_fn
        # the strategy shim falls through to the native path on FAIL_FAST
        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        # SimpleFallback has no fallback_fn and no default_value, so FAIL_FAST → native FAILURE
        assert result.outcome == PolicyOutcome.FAILURE

    def test_strategy_shim_success_is_passed_through(self, strategy_policy):
        """Strategy shim: when func succeeds, SUCCESS without calling the strategy."""
        result = strategy_policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"

    def test_strategy_shim_with_native_fallback(self):
        """Strategy FAIL_FAST → falls through to the native fallback_fn."""
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
        """_convert_fallback_result: the FallbackMode is kept in metadata."""
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
        """_convert_fallback_result: metadata['strategy_shim'] is True."""
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
        """_convert_fallback_result: original_error is kept in metadata."""
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
        """_convert_fallback_result: a FAILURE outcome sets error."""
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
        """_convert_fallback_result: a SUCCESS outcome leaves error None."""
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
# Behavior — AsyncFallbackPolicy execute()
# =============================================================================


class TestAsyncFallbackPolicyExecuteBehavior:
    """AsyncFallbackPolicy.execute() behavior."""

    @pytest.mark.asyncio
    async def test_func_return_value_preserved(self, async_basic_policy):
        """The async func's return value is kept."""

        async def ok():
            return {"key": "async_value"}

        result = await async_basic_policy.execute(ok)
        assert result.value == {"key": "async_value"}

    @pytest.mark.asyncio
    async def test_fallback_fn_on_exception(self, async_basic_policy):
        """A func exception calls the async fallback_fn."""

        async def failing():
            raise RuntimeError("async fail")

        result = await async_basic_policy.execute(failing)
        assert result.value == "async_fallback_value"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    @pytest.mark.asyncio
    async def test_fallback_chain_first_success(self, async_chain_policy):
        """When async fallback_chain[0] succeeds it returns at once."""

        async def failing():
            raise RuntimeError("fail")

        result = await async_chain_policy.execute(failing)
        assert result.value == "async_chain_0"
        assert result.metadata["fallback_index"] == 0

    @pytest.mark.asyncio
    async def test_fallback_chain_skips_to_next(self):
        """When async chain[0] fails chain[1] is tried."""

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
        """When the whole async chain fails default_value is returned."""

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
        """When every async fallback is exhausted FAILURE is returned."""
        policy = AsyncFallbackPolicy()

        async def failing():
            raise ValueError("original")

        result = await policy.execute(failing)
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["all_fallbacks_exhausted"] is True

    @pytest.mark.asyncio
    async def test_func_with_args(self, async_basic_policy):
        """*args reach the async func."""

        async def add(x, y):
            return x + y

        result = await async_basic_policy.execute(add, 3, 7)
        assert result.value == 10

    @pytest.mark.asyncio
    async def test_func_with_kwargs(self, async_basic_policy):
        """**kwargs reach the async func."""

        async def mul(x=0):
            return x * 2

        result = await async_basic_policy.execute(mul, x=5)
        assert result.value == 10

    @pytest.mark.asyncio
    async def test_success_metadata_fallback_used_false(self, async_basic_policy):
        """On async success metadata['fallback_used'] is False."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert result.metadata["fallback_used"] is False


# =============================================================================
# Behavior — AsyncFallbackPolicy._apply_fallback()
# =============================================================================


class TestAsyncFallbackPolicyApplyFallbackBehavior:
    """AsyncFallbackPolicy._apply_fallback() behavior."""

    @pytest.mark.asyncio
    async def test_apply_fallback_uses_chain(self, async_chain_policy):
        """Async _apply_fallback tries the chain."""
        result = await async_chain_policy._apply_fallback(
            original_error=RuntimeError("fail")
        )
        assert result.value == "async_chain_0"

    @pytest.mark.asyncio
    async def test_apply_fallback_uses_fn(self, async_basic_policy):
        """Async _apply_fallback tries fallback_fn."""
        result = await async_basic_policy._apply_fallback(
            original_error=RuntimeError("fail")
        )
        assert result.value == "async_fallback_value"

    @pytest.mark.asyncio
    async def test_apply_fallback_uses_default(self):
        """Async _apply_fallback returns default_value."""
        policy = AsyncFallbackPolicy(default_value="default_only")
        result = await policy._apply_fallback(original_error=RuntimeError("fail"))
        assert result.value == "default_only"

    @pytest.mark.asyncio
    async def test_apply_fallback_all_exhausted(self):
        """Async _apply_fallback returns FAILURE when every fallback is exhausted."""
        policy = AsyncFallbackPolicy()
        error = RuntimeError("original")
        result = await policy._apply_fallback(original_error=error)
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is error


# =============================================================================
# Behavior — AsyncFallbackPolicy predicate
# =============================================================================


class TestAsyncFallbackPolicyPredicateBehavior:
    """AsyncFallbackPolicy predicate behavior."""

    def test_default_predicate_activates_on_failure(self):
        """The async default predicate activates on FAILURE."""
        policy = AsyncFallbackPolicy()
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.FAILURE)) is True

    def test_default_predicate_not_activates_on_success(self):
        """The async default predicate is inactive on SUCCESS."""
        policy = AsyncFallbackPolicy()
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.SUCCESS)) is False

    def test_custom_predicate_applied(self):
        """An async custom predicate is applied."""
        policy = AsyncFallbackPolicy(
            predicate=lambda r: r.outcome == PolicyOutcome.TIMEOUT,
        )
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.TIMEOUT)) is True
        assert policy._predicate(PolicyResult(outcome=PolicyOutcome.FAILURE)) is False


# =============================================================================
# Behavior — partition_aware_chain
# =============================================================================


@dataclass
class MockPartitionState:
    """PartitionState-compatible double; reflects the latest state at call time."""

    db_available: bool = True
    cache_available: bool = True
    external_apis: dict[str, bool] = field(default_factory=dict)


class TestPartitionAwareChainBehavior:
    """partition_aware_chain helper behavior."""

    def test_returns_two_callables_when_both_fns(self):
        """With both cache_fn and db_fn, two callables are returned."""
        state = MockPartitionState()
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cache",
            db_fn=lambda: "db",
        )
        assert len(chain) == 2

    def test_returns_one_callable_cache_only(self):
        """With cache_fn only, one callable is returned."""
        state = MockPartitionState()
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cache",
        )
        assert len(chain) == 1

    def test_returns_one_callable_db_only(self):
        """With db_fn only, one callable is returned."""
        state = MockPartitionState()
        chain = partition_aware_chain(
            state_provider=lambda: state,
            db_fn=lambda: "db",
        )
        assert len(chain) == 1

    def test_returns_empty_when_no_fns(self):
        """With neither cache_fn nor db_fn, an empty list is returned."""
        state = MockPartitionState()
        chain = partition_aware_chain(state_provider=lambda: state)
        assert chain == []

    def test_cache_fn_called_when_available(self):
        """cache_available=True calls cache_fn."""
        state = MockPartitionState(cache_available=True)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cached_data",
        )
        assert chain[0]() == "cached_data"

    def test_cache_fn_raises_when_unavailable(self):
        """cache_available=False raises RuntimeError."""
        state = MockPartitionState(cache_available=False)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cached_data",
        )
        with pytest.raises(RuntimeError, match="Cache unavailable"):
            chain[0]()

    def test_db_fn_called_when_available(self):
        """db_available=True calls db_fn."""
        state = MockPartitionState(db_available=True)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            db_fn=lambda: "db_data",
        )
        # db_fn is index 0 when there is no cache_fn
        assert chain[0]() == "db_data"

    def test_db_fn_raises_when_unavailable(self):
        """db_available=False raises RuntimeError."""
        state = MockPartitionState(db_available=False)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            db_fn=lambda: "db_data",
        )
        with pytest.raises(RuntimeError, match="DB unavailable"):
            chain[0]()

    def test_state_provider_called_at_execution_time(self):
        """state_provider is called when the chain function runs (no stale state)."""
        state = MockPartitionState(cache_available=True)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cached",
        )
        # State changes after the chain is built
        state.cache_available = False
        with pytest.raises(RuntimeError, match="Cache unavailable"):
            chain[0]()

    def test_state_provider_dynamic_recovery(self):
        """Once the state recovers the fallback function succeeds too."""
        state = MockPartitionState(cache_available=False)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "recovered",
        )
        # Confirm the failure
        with pytest.raises(RuntimeError):
            chain[0]()
        # Recover the state
        state.cache_available = True
        assert chain[0]() == "recovered"

    def test_chain_order_cache_before_db(self):
        """Chain order: cache comes before db."""
        state = MockPartitionState(cache_available=True, db_available=True)
        chain = partition_aware_chain(
            state_provider=lambda: state,
            cache_fn=lambda: "cache_result",
            db_fn=lambda: "db_result",
        )
        assert chain[0]() == "cache_result"
        assert chain[1]() == "db_result"

    def test_integration_with_fallback_policy(self):
        """partition_aware_chain used together with FallbackPolicy."""
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
        # cache unavailable → db used
        assert result.value == "db"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_integration_all_unavailable_falls_to_default(self):
        """With cache and db both unavailable, falls back to default_value."""
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
# Behavior — FallbackPolicy exception-handling contract
# =============================================================================


class TestFallbackPolicyExceptionHandlingBehavior:
    """FallbackPolicy exception absorption behavior."""

    def test_execute_never_raises(self, basic_policy):
        """execute() absorbs every exception and returns a PolicyResult."""

        def failing():
            raise RuntimeError("should be absorbed")

        result = basic_policy.execute(failing)
        # The exception is absorbed and a PolicyResult is returned
        assert isinstance(result, PolicyResult)

    def test_execute_absorbs_various_exceptions(self):
        """execute() absorbs many exception types."""
        policy = FallbackPolicy(default_value="safe")
        exceptions = [ValueError, TypeError, IOError, KeyError, AttributeError]

        for exc_type in exceptions:

            def failing(e=exc_type):
                raise e("test")

            result = policy.execute(failing)
            assert result.success is True
            assert result.value == "safe"

    def test_apply_fallback_never_raises(self):
        """_apply_fallback() never raises."""
        policy = FallbackPolicy()
        result = policy._apply_fallback(original_error=RuntimeError("test"))
        assert isinstance(result, PolicyResult)


# =============================================================================
# Behavior — FallbackPolicy edge cases
# =============================================================================


class TestFallbackPolicyEdgeCaseBehavior:
    """FallbackPolicy edge-case behavior."""

    def test_empty_fallback_chain(self):
        """An empty fallback_chain is skipped."""
        policy = FallbackPolicy(
            fallback_chain=[],
            fallback_fn=lambda: "fn_result",
        )

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.value == "fn_result"

    def test_none_strategy_uses_native_path(self):
        """strategy=None uses the native path."""
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
        """execute() accepts the context parameter."""
        ctx = PolicyContext(order_id="ctx-123")
        result = basic_policy.execute(lambda: "ok", context=ctx)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_default_value_zero_is_valid(self):
        """default_value=0 is a valid default_value (not None)."""
        policy = FallbackPolicy(default_value=0)

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.value == 0
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_default_value_empty_string_is_valid(self):
        """default_value='' is a valid default_value."""
        policy = FallbackPolicy(default_value="")

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        assert result.value == ""
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_default_value_false_is_valid(self):
        """default_value=False is a valid default_value."""
        policy = FallbackPolicy(default_value=False)

        def failing():
            raise RuntimeError("fail")

        result = policy.execute(failing)
        # default_value is not None (False != None), so the default path is used
        # because the code checks `if self._default_value is not None`, False passes
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
