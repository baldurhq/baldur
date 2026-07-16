"""
BulkheadPolicy / AsyncBulkheadPolicy unit tests.

Targets:
- services/bulkhead/policy.py (BulkheadPolicy, AsyncBulkheadPolicy,
  bulkhead_policy, async_bulkhead_policy)
- services/bulkhead/__init__.py (export verification)

Worker-pool-backed policy cases live in the private tree (the pool
implementation ships in the licensed tier).

UNIT_TEST_GUIDELINES.md compliance:
- Contract verification: hardcoded expected values (name, outcome, executed_policies)
- Behavior verification: source references (BulkheadFullError, BulkheadTimeoutError, etc.)
- conftest.py placement: single-file fixtures live inside the file (§5.1)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.interfaces.resilience_policy import (
    AsyncResiliencePolicy,
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)
from baldur.services.bulkhead.async_semaphore import AsyncSemaphoreBulkhead
from baldur.services.bulkhead.base import BulkheadState, BulkheadType
from baldur.services.bulkhead.exceptions import (
    BulkheadFullError,
)
from baldur.services.bulkhead.policy import (
    AsyncBulkheadPolicy,
    BulkheadPolicy,
    async_bulkhead_policy,
    bulkhead_policy,
)
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead

# =============================================================================
# Fixtures — single-file scope, so placed inside the file (§5.1)
# =============================================================================


@pytest.fixture(autouse=True)
def _empty_provider_slot(monkeypatch):
    """Pin the resolution chain to its fallback leg for this module.

    These tests exercise the base registry/decorator semantics; a populated
    provider slot (e.g. a registry overlay registered by another test's
    environment) would leak saturated compartments across tests because
    reset_bulkhead_registry() clears only the fallback leg.
    """
    from baldur.factory.registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry.bulkhead_registry, "safe_get", lambda name=None: None
    )


@pytest.fixture
def semaphore_bulkhead():
    """A real SemaphoreBulkhead instance (max_concurrent=2)."""
    return SemaphoreBulkhead("test_semaphore", max_concurrent=2)


@pytest.fixture
def semaphore_policy(semaphore_bulkhead):
    """A SemaphoreBulkhead-backed BulkheadPolicy instance."""
    return BulkheadPolicy(bulkhead=semaphore_bulkhead)


@pytest.fixture
def async_bulkhead():
    """A real AsyncSemaphoreBulkhead instance (max_concurrent=2)."""
    return AsyncSemaphoreBulkhead("test_async", max_concurrent=2)


@pytest.fixture
def async_policy(async_bulkhead):
    """An AsyncBulkheadPolicy instance."""
    return AsyncBulkheadPolicy(async_bulkhead=async_bulkhead)


def _mock_bulkhead_state(
    name: str = "test",
    active_count: int = 0,
    max_concurrent: int = 10,
) -> BulkheadState:
    """BulkheadState helper."""
    return BulkheadState(
        name=name,
        bulkhead_type=BulkheadType.SEMAPHORE,
        max_concurrent=max_concurrent,
        active_count=active_count,
        waiting_count=0,
        rejected_count=0,
    )


# =============================================================================
# Contract verification — BulkheadPolicy
# =============================================================================


class TestBulkheadPolicyContract:
    """BulkheadPolicy fixed-identifier and result-structure contract."""

    def test_name_is_bulkhead(self, semaphore_policy):
        """The name property is 'bulkhead'."""
        assert semaphore_policy.name == "bulkhead"

    def test_bulkhead_name_matches_inner_bulkhead(
        self, semaphore_policy, semaphore_bulkhead
    ):
        """bulkhead_name matches the inner Bulkhead.name."""
        assert semaphore_policy.bulkhead_name == semaphore_bulkhead.name

    def test_success_result_has_bulkhead_in_executed_policies(self, semaphore_policy):
        """A success result has 'bulkhead' in executed_policies."""
        result = semaphore_policy.execute(lambda: "ok")
        assert "bulkhead" in result.executed_policies

    def test_rejected_result_has_bulkhead_in_executed_policies(self):
        """A rejected result has 'bulkhead' in executed_policies."""
        bh = SemaphoreBulkhead("full_test", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        # Occupy the only slot.
        bh.try_acquire()
        try:
            result = policy.execute(lambda: "should_not_run")
            assert "bulkhead" in result.executed_policies
        finally:
            bh.release()

    def test_success_outcome_is_success(self, semaphore_policy):
        """On success the outcome is PolicyOutcome.SUCCESS."""
        result = semaphore_policy.execute(lambda: 42)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_rejected_outcome_is_rejected(self):
        """On rejection the outcome is PolicyOutcome.REJECTED."""
        bh = SemaphoreBulkhead("reject_test", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        bh.try_acquire()
        try:
            result = policy.execute(lambda: "nope")
            assert result.outcome == PolicyOutcome.REJECTED
        finally:
            bh.release()

    def test_success_result_is_policy_result_instance(self, semaphore_policy):
        """The return type is PolicyResult."""
        result = semaphore_policy.execute(lambda: "ok")
        assert isinstance(result, PolicyResult)

    def test_default_timeout_is_none(self, semaphore_bulkhead):
        """The default timeout is None (Fast Fail)."""
        policy = BulkheadPolicy(bulkhead=semaphore_bulkhead)
        assert policy._timeout is None

    def test_explicit_timeout_stored(self, semaphore_bulkhead):
        """An explicit timeout is stored."""
        policy = BulkheadPolicy(bulkhead=semaphore_bulkhead, timeout=3.5)
        assert policy._timeout == 3.5


# =============================================================================
# BulkheadPolicy — Protocol-compatibility contract
# =============================================================================


class TestBulkheadPolicyProtocolContract:
    """ResiliencePolicy Protocol compatibility verification."""

    def test_bulkhead_policy_is_resilience_policy(self, semaphore_policy):
        """BulkheadPolicy is isinstance-compatible with ResiliencePolicy."""
        assert isinstance(semaphore_policy, ResiliencePolicy)


# =============================================================================
# BulkheadPolicy success-path behavior
# =============================================================================


class TestBulkheadPolicySemaphoreSuccessBehavior:
    """SemaphoreBulkhead success-path behavior."""

    def test_success_returns_function_value(self, semaphore_policy):
        """On success func's return value is carried in result.value."""
        result = semaphore_policy.execute(lambda: "success_value")
        assert result.value == "success_value"

    def test_success_passes_args(self, semaphore_policy):
        """args are passed to the function correctly."""

        def add(a, b):
            return a + b

        result = semaphore_policy.execute(add, 3, 7)
        assert result.value == 10

    def test_success_passes_kwargs(self, semaphore_policy):
        """kwargs are passed to the function correctly."""

        def greet(name, prefix="Hello"):
            return f"{prefix}, {name}"

        result = semaphore_policy.execute(greet, "world", prefix="Hi")
        assert result.value == "Hi, world"

    def test_success_result_property_true(self, semaphore_policy):
        """A success result's .success property is True."""
        result = semaphore_policy.execute(lambda: "ok")
        assert result.success is True

    def test_success_result_rejected_property_false(self, semaphore_policy):
        """A success result's .rejected property is False."""
        result = semaphore_policy.execute(lambda: "ok")
        assert result.rejected is False

    def test_success_result_error_is_none(self, semaphore_policy):
        """On success the error is None."""
        result = semaphore_policy.execute(lambda: "ok")
        assert result.error is None

    def test_success_metadata_contains_bulkhead_name(
        self, semaphore_policy, semaphore_bulkhead
    ):
        """Success metadata contains bulkhead_name."""
        result = semaphore_policy.execute(lambda: "ok")
        assert result.metadata["bulkhead_name"] == semaphore_bulkhead.name

    def test_success_metadata_contains_state(self, semaphore_policy):
        """Success metadata contains the state dict."""
        result = semaphore_policy.execute(lambda: "ok")
        state = result.metadata["state"]
        assert "active_count" in state
        assert "max_concurrent" in state
        assert "available_permits" in state
        assert "utilization_percent" in state


# =============================================================================
# BulkheadPolicy — REJECTED behavior
# =============================================================================


class TestBulkheadPolicyRejectedBehavior:
    """BulkheadFullError → REJECTED behavior."""

    def test_semaphore_full_returns_rejected(self):
        """A full SemaphoreBulkhead returns REJECTED."""
        bh = SemaphoreBulkhead("full_test", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        # Occupy the slot.
        bh.try_acquire()
        try:
            result = policy.execute(lambda: "should_not_run")
            assert result.outcome == PolicyOutcome.REJECTED
            assert result.rejected is True
        finally:
            bh.release()

    def test_rejected_error_is_bulkhead_full_error(self):
        """On rejection the error is a BulkheadFullError instance."""
        bh = SemaphoreBulkhead("error_test", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        bh.try_acquire()
        try:
            result = policy.execute(lambda: "nope")
            assert isinstance(result.error, BulkheadFullError)
        finally:
            bh.release()

    def test_rejected_error_has_bulkhead_name(self):
        """On rejection error.bulkhead_name matches the Bulkhead's name."""
        bh = SemaphoreBulkhead("name_check", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        bh.try_acquire()
        try:
            result = policy.execute(lambda: "nope")
            assert result.error.bulkhead_name == bh.name
        finally:
            bh.release()

    def test_rejected_error_has_max_concurrent(self):
        """On rejection error.max_concurrent matches the configured value."""
        max_conc = 3
        bh = SemaphoreBulkhead("max_check", max_concurrent=max_conc)
        policy = BulkheadPolicy(bulkhead=bh)
        # Occupy every slot.
        acquired = [bh.try_acquire() for _ in range(max_conc)]
        try:
            result = policy.execute(lambda: "nope")
            assert result.error.max_concurrent == max_conc
        finally:
            for _ in filter(None, acquired):
                bh.release()

    def test_rejected_value_is_none(self):
        """On rejection the value is None."""
        bh = SemaphoreBulkhead("val_none", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        bh.try_acquire()
        try:
            result = policy.execute(lambda: "nope")
            assert result.value is None
        finally:
            bh.release()

    def test_rejected_does_not_execute_function(self):
        """On rejection func is not executed."""
        bh = SemaphoreBulkhead("no_exec", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        bh.try_acquire()
        func = MagicMock(spec=lambda: None)
        try:
            policy.execute(func)
            func.assert_not_called()
        finally:
            bh.release()

    def test_rejected_metadata_contains_bulkhead_name(self):
        """Rejection metadata contains bulkhead_name."""
        bh = SemaphoreBulkhead("meta_test", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        bh.try_acquire()
        try:
            result = policy.execute(lambda: "nope")
            assert result.metadata["bulkhead_name"] == bh.name
        finally:
            bh.release()

    def test_rejected_metadata_contains_state(self):
        """Rejection metadata contains the state dict."""
        bh = SemaphoreBulkhead("state_meta", max_concurrent=1)
        policy = BulkheadPolicy(bulkhead=bh)
        bh.try_acquire()
        try:
            result = policy.execute(lambda: "nope")
            state = result.metadata["state"]
            assert "active_count" in state
            assert "max_concurrent" in state
        finally:
            bh.release()


# =============================================================================
# BulkheadPolicy — exception re-propagation behavior
# =============================================================================


class TestBulkheadPolicyExceptionPropagationBehavior:
    """Business-exception re-propagation behavior."""

    def test_business_exception_reraises(self, semaphore_policy):
        """A business exception (e.g. ValueError) is re-propagated, not caught."""
        with pytest.raises(ValueError, match="business error"):
            semaphore_policy.execute(self._raise_value_error)

    def test_runtime_error_reraises(self, semaphore_policy):
        """A RuntimeError is also re-propagated."""
        with pytest.raises(RuntimeError, match="runtime fail"):
            semaphore_policy.execute(self._raise_runtime_error)

    @staticmethod
    def _raise_value_error():
        raise ValueError("business error")

    @staticmethod
    def _raise_runtime_error():
        raise RuntimeError("runtime fail")


# =============================================================================
# BulkheadPolicy — _get_state_dict behavior
# =============================================================================


class TestBulkheadPolicyStateDictBehavior:
    """_get_state_dict() return-value verification."""

    def test_state_dict_has_four_keys(self, semaphore_policy):
        """_get_state_dict() contains four fields."""
        state_dict = semaphore_policy._get_state_dict()
        expected_keys = {
            "active_count",
            "max_concurrent",
            "available_permits",
            "utilization_percent",
        }
        assert set(state_dict.keys()) == expected_keys

    def test_state_dict_matches_bulkhead_state(
        self, semaphore_policy, semaphore_bulkhead
    ):
        """_get_state_dict() values match bulkhead.get_state()."""
        state = semaphore_bulkhead.get_state()
        state_dict = semaphore_policy._get_state_dict()
        assert state_dict["active_count"] == state.active_count
        assert state_dict["max_concurrent"] == state.max_concurrent
        assert state_dict["available_permits"] == state.available_permits
        assert state_dict["utilization_percent"] == state.utilization_percent

    def test_state_dict_reflects_active_count(self):
        """After occupying a slot, state_dict's active_count reflects it."""
        bh = SemaphoreBulkhead("state_active", max_concurrent=3)
        policy = BulkheadPolicy(bulkhead=bh)
        bh.try_acquire()
        try:
            state_dict = policy._get_state_dict()
            assert state_dict["active_count"] == 1
            assert state_dict["available_permits"] == 2
        finally:
            bh.release()


# =============================================================================
# BulkheadPolicy — PolicyContext passthrough behavior
# =============================================================================


class TestBulkheadPolicyContextBehavior:
    """PolicyContext passthrough behavior."""

    def test_execute_accepts_context_parameter(self, semaphore_policy):
        """execute() accepts a context parameter."""
        ctx = PolicyContext(order_id="order-123", trace_id="trace-abc")
        result = semaphore_policy.execute(lambda: "with_context", context=ctx)
        assert result.value == "with_context"
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_execute_works_without_context(self, semaphore_policy):
        """execute() works with the default context=None."""
        result = semaphore_policy.execute(lambda: "no_context")
        assert result.value == "no_context"
        assert result.outcome == PolicyOutcome.SUCCESS


# =============================================================================
# Contract verification — AsyncBulkheadPolicy
# =============================================================================


class TestAsyncBulkheadPolicyContract:
    """AsyncBulkheadPolicy fixed-identifier and result-structure contract."""

    def test_name_is_bulkhead(self, async_policy):
        """The name property is 'bulkhead'."""
        assert async_policy.name == "bulkhead"

    def test_bulkhead_name_matches_inner_bulkhead(self, async_policy, async_bulkhead):
        """bulkhead_name matches the inner AsyncSemaphoreBulkhead.name."""
        assert async_policy.bulkhead_name == async_bulkhead.name

    def test_default_timeout_is_none(self, async_bulkhead):
        """The default timeout is None."""
        policy = AsyncBulkheadPolicy(async_bulkhead=async_bulkhead)
        assert policy._timeout is None

    def test_explicit_timeout_stored(self, async_bulkhead):
        """An explicit timeout is stored."""
        policy = AsyncBulkheadPolicy(async_bulkhead=async_bulkhead, timeout=2.0)
        assert policy._timeout == 2.0


# =============================================================================
# AsyncBulkheadPolicy — Protocol-compatibility contract
# =============================================================================


class TestAsyncBulkheadPolicyProtocolContract:
    """AsyncResiliencePolicy Protocol compatibility verification."""

    def test_async_bulkhead_policy_is_async_resilience_policy(self, async_policy):
        """AsyncBulkheadPolicy is isinstance-compatible with AsyncResiliencePolicy."""
        assert isinstance(async_policy, AsyncResiliencePolicy)


# =============================================================================
# AsyncBulkheadPolicy — success-path behavior
# =============================================================================


class TestAsyncBulkheadPolicySuccessBehavior:
    """Async success-path behavior."""

    @pytest.mark.asyncio
    async def test_success_returns_function_value(self, async_policy):
        """On success func's return value is carried in result.value."""

        async def async_func():
            return "async_success"

        result = await async_policy.execute(async_func)
        assert result.value == "async_success"

    @pytest.mark.asyncio
    async def test_success_outcome_is_success(self, async_policy):
        """On success the outcome is PolicyOutcome.SUCCESS."""

        async def async_func():
            return 42

        result = await async_policy.execute(async_func)
        assert result.outcome == PolicyOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_success_has_bulkhead_in_executed_policies(self, async_policy):
        """A success result has 'bulkhead' in executed_policies."""

        async def async_func():
            return "ok"

        result = await async_policy.execute(async_func)
        assert "bulkhead" in result.executed_policies

    @pytest.mark.asyncio
    async def test_success_passes_args(self, async_policy):
        """args are passed to the async function correctly."""

        async def add(a, b):
            return a + b

        result = await async_policy.execute(add, 5, 8)
        assert result.value == 13

    @pytest.mark.asyncio
    async def test_success_passes_kwargs(self, async_policy):
        """kwargs are passed to the async function correctly."""

        async def greet(name, prefix="Hello"):
            return f"{prefix}, {name}"

        result = await async_policy.execute(greet, "world", prefix="Async")
        assert result.value == "Async, world"

    @pytest.mark.asyncio
    async def test_success_result_property_true(self, async_policy):
        """A success result's .success property is True."""

        async def async_func():
            return "ok"

        result = await async_policy.execute(async_func)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_success_metadata_contains_bulkhead_name(
        self, async_policy, async_bulkhead
    ):
        """Success metadata contains bulkhead_name."""

        async def async_func():
            return "ok"

        result = await async_policy.execute(async_func)
        assert result.metadata["bulkhead_name"] == async_bulkhead.name

    @pytest.mark.asyncio
    async def test_success_metadata_contains_state(self, async_policy):
        """Success metadata contains the state dict."""

        async def async_func():
            return "ok"

        result = await async_policy.execute(async_func)
        state = result.metadata["state"]
        assert "active_count" in state
        assert "max_concurrent" in state
        assert "available_permits" in state
        assert "utilization_percent" in state

    @pytest.mark.asyncio
    async def test_success_error_is_none(self, async_policy):
        """On success the error is None."""

        async def async_func():
            return "ok"

        result = await async_policy.execute(async_func)
        assert result.error is None


# =============================================================================
# AsyncBulkheadPolicy — REJECTED behavior
# =============================================================================


class TestAsyncBulkheadPolicyRejectedBehavior:
    """Async BulkheadFullError → REJECTED behavior."""

    @pytest.mark.asyncio
    async def test_async_full_returns_rejected(self):
        """A full async bulkhead returns REJECTED."""
        bh = AsyncSemaphoreBulkhead("async_full", max_concurrent=1)
        policy = AsyncBulkheadPolicy(async_bulkhead=bh)
        # Occupy the slot.
        await bh.try_acquire()
        try:

            async def should_not_run():
                return "nope"

            result = await policy.execute(should_not_run)
            assert result.outcome == PolicyOutcome.REJECTED
            assert result.rejected is True
        finally:
            await bh.release()

    @pytest.mark.asyncio
    async def test_rejected_error_is_bulkhead_full_error(self):
        """On async rejection the error is a BulkheadFullError instance."""
        bh = AsyncSemaphoreBulkhead("async_err", max_concurrent=1)
        policy = AsyncBulkheadPolicy(async_bulkhead=bh)
        await bh.try_acquire()
        try:

            async def noop():
                return "nope"

            result = await policy.execute(noop)
            assert isinstance(result.error, BulkheadFullError)
        finally:
            await bh.release()

    @pytest.mark.asyncio
    async def test_rejected_value_is_none(self):
        """On async rejection the value is None."""
        bh = AsyncSemaphoreBulkhead("async_val_none", max_concurrent=1)
        policy = AsyncBulkheadPolicy(async_bulkhead=bh)
        await bh.try_acquire()
        try:

            async def noop():
                return "nope"

            result = await policy.execute(noop)
            assert result.value is None
        finally:
            await bh.release()

    @pytest.mark.asyncio
    async def test_rejected_has_bulkhead_in_executed_policies(self):
        """An async rejection result has 'bulkhead' in executed_policies."""
        bh = AsyncSemaphoreBulkhead("async_ep", max_concurrent=1)
        policy = AsyncBulkheadPolicy(async_bulkhead=bh)
        await bh.try_acquire()
        try:

            async def noop():
                return "nope"

            result = await policy.execute(noop)
            assert "bulkhead" in result.executed_policies
        finally:
            await bh.release()

    @pytest.mark.asyncio
    async def test_rejected_metadata_contains_bulkhead_name(self):
        """Async rejection metadata contains bulkhead_name."""
        bh = AsyncSemaphoreBulkhead("async_meta_name", max_concurrent=1)
        policy = AsyncBulkheadPolicy(async_bulkhead=bh)
        await bh.try_acquire()
        try:

            async def noop():
                return "nope"

            result = await policy.execute(noop)
            assert result.metadata["bulkhead_name"] == bh.name
        finally:
            await bh.release()


# =============================================================================
# AsyncBulkheadPolicy — exception re-propagation behavior
# =============================================================================


class TestAsyncBulkheadPolicyExceptionPropagationBehavior:
    """Async business-exception re-propagation behavior."""

    @pytest.mark.asyncio
    async def test_async_business_exception_reraises(self, async_policy):
        """An async business exception (ValueError) is re-propagated, not caught."""

        async def raise_error():
            raise ValueError("async business error")

        with pytest.raises(ValueError, match="async business error"):
            await async_policy.execute(raise_error)

    @pytest.mark.asyncio
    async def test_async_runtime_error_reraises(self, async_policy):
        """An async RuntimeError is also re-propagated."""

        async def raise_error():
            raise RuntimeError("async runtime fail")

        with pytest.raises(RuntimeError, match="async runtime fail"):
            await async_policy.execute(raise_error)


# =============================================================================
# AsyncBulkheadPolicy — _get_state_dict behavior
# =============================================================================


class TestAsyncBulkheadPolicyStateDictBehavior:
    """AsyncBulkheadPolicy._get_state_dict() return-value verification."""

    def test_async_state_dict_has_four_keys(self, async_policy):
        """_get_state_dict() contains four fields."""
        state_dict = async_policy._get_state_dict()
        expected_keys = {
            "active_count",
            "max_concurrent",
            "available_permits",
            "utilization_percent",
        }
        assert set(state_dict.keys()) == expected_keys

    def test_async_state_dict_matches_bulkhead_state(
        self, async_policy, async_bulkhead
    ):
        """_get_state_dict() values match async_bulkhead.get_state()."""
        state = async_bulkhead.get_state()
        state_dict = async_policy._get_state_dict()
        assert state_dict["active_count"] == state.active_count
        assert state_dict["max_concurrent"] == state.max_concurrent
        assert state_dict["available_permits"] == state.available_permits
        assert state_dict["utilization_percent"] == state.utilization_percent


# =============================================================================
# AsyncBulkheadPolicy — PolicyContext passthrough behavior
# =============================================================================


class TestAsyncBulkheadPolicyContextBehavior:
    """AsyncBulkheadPolicy PolicyContext passthrough behavior."""

    @pytest.mark.asyncio
    async def test_execute_accepts_context_parameter(self, async_policy):
        """execute() accepts a context parameter."""
        ctx = PolicyContext(order_id="order-async", trace_id="trace-async")

        async def async_func():
            return "with_context"

        result = await async_policy.execute(async_func, context=ctx)
        assert result.value == "with_context"
        assert result.outcome == PolicyOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_execute_works_without_context(self, async_policy):
        """execute() works with the default context=None."""

        async def async_func():
            return "no_context"

        result = await async_policy.execute(async_func)
        assert result.value == "no_context"


# =============================================================================
# bulkhead_policy() factory behavior
# =============================================================================


class TestBulkheadPolicyFactoryBehavior:
    """bulkhead_policy() factory-function behavior."""

    def test_factory_returns_bulkhead_policy_instance(self):
        """The factory return type is BulkheadPolicy."""
        policy = bulkhead_policy("factory_test", max_concurrent=5)
        assert isinstance(policy, BulkheadPolicy)

    def test_factory_sets_bulkhead_name(self):
        """The created policy's bulkhead_name matches the argument."""
        policy = bulkhead_policy("my_domain", max_concurrent=5)
        assert policy.bulkhead_name == "my_domain"

    def test_factory_sets_timeout(self):
        """The factory's timeout argument is passed to the policy."""
        policy = bulkhead_policy("timeout_test", max_concurrent=5, timeout=3.0)
        assert policy._timeout == 3.0

    def test_factory_default_timeout_is_none(self):
        """The factory's default timeout is None."""
        policy = bulkhead_policy("default_timeout", max_concurrent=5)
        assert policy._timeout is None

    def test_factory_semaphore_type_default(self):
        """The default bulkhead_type is 'semaphore'."""
        policy = bulkhead_policy("sem_default", max_concurrent=5)
        # Confirm the inner bulkhead is a SemaphoreBulkhead.
        assert isinstance(policy._bulkhead, SemaphoreBulkhead)

    def test_factory_registry_singleton(self):
        """Two calls with the same name reuse the same Bulkhead instance."""
        p1 = bulkhead_policy("singleton_test", max_concurrent=5)
        p2 = bulkhead_policy("singleton_test", max_concurrent=5)
        assert p1._bulkhead is p2._bulkhead

    def test_factory_execute_works(self):
        """A factory-created policy executes normally."""
        policy = bulkhead_policy("exec_test", max_concurrent=5)
        result = policy.execute(lambda: "factory_ok")
        assert result.value == "factory_ok"
        assert result.outcome == PolicyOutcome.SUCCESS


# =============================================================================
# async_bulkhead_policy() factory behavior
# =============================================================================


class TestAsyncBulkheadPolicyFactoryBehavior:
    """async_bulkhead_policy() factory-function behavior."""

    def test_factory_returns_async_bulkhead_policy_instance(self):
        """The factory return type is AsyncBulkheadPolicy."""
        policy = async_bulkhead_policy("async_factory_test", max_concurrent=5)
        assert isinstance(policy, AsyncBulkheadPolicy)

    def test_factory_sets_bulkhead_name(self):
        """The created async policy's bulkhead_name matches the argument."""
        policy = async_bulkhead_policy("async_domain", max_concurrent=5)
        assert policy.bulkhead_name == "async_domain"

    def test_factory_sets_timeout(self):
        """The factory's timeout argument is passed to the async policy."""
        policy = async_bulkhead_policy("async_timeout", max_concurrent=5, timeout=2.0)
        assert policy._timeout == 2.0

    def test_factory_default_timeout_is_none(self):
        """The factory's default timeout is None."""
        policy = async_bulkhead_policy("async_default", max_concurrent=5)
        assert policy._timeout is None

    def test_factory_inner_is_async_semaphore_bulkhead(self):
        """The factory's inner instance is an AsyncSemaphoreBulkhead."""
        policy = async_bulkhead_policy("async_inner", max_concurrent=5)
        assert isinstance(policy._async_bulkhead, AsyncSemaphoreBulkhead)

    def test_factory_leaves_domain_visible_in_registry(self):
        """async_bulkhead_policy provisions the sync twin → domain is registry-visible.

        The sync-first provisioning (D5) makes the domain appear in
        list_names(), so it reaches the admin API, metrics, and shutdown
        iteration — even when max_concurrent is None.
        """
        from baldur.services.bulkhead.registry import (
            get_bulkhead_registry,
            reset_bulkhead_registry,
        )

        # This file has no autouse registry reset; isolate explicitly.
        reset_bulkhead_registry()
        try:
            async_bulkhead_policy("visible_domain", max_concurrent=None)
            assert "visible_domain" in get_bulkhead_registry().list_names()
        finally:
            reset_bulkhead_registry()

    @pytest.mark.asyncio
    async def test_factory_execute_works(self):
        """A factory-created async policy executes normally."""
        policy = async_bulkhead_policy("async_exec", max_concurrent=5)

        async def async_func():
            return "async_factory_ok"

        result = await policy.execute(async_func)
        assert result.value == "async_factory_ok"
        assert result.outcome == PolicyOutcome.SUCCESS


# =============================================================================
# __init__.py export contract
# =============================================================================


class TestBulkheadPackageExportContract:
    """services/bulkhead/__init__.py export verification."""

    def test_bulkhead_policy_exported(self):
        """BulkheadPolicy is exported from __init__.py."""
        from baldur.services.bulkhead import BulkheadPolicy as Exported

        assert Exported is BulkheadPolicy

    def test_async_bulkhead_policy_exported(self):
        """AsyncBulkheadPolicy is exported from __init__.py."""
        from baldur.services.bulkhead import AsyncBulkheadPolicy as Exported

        assert Exported is AsyncBulkheadPolicy

    def test_bulkhead_policy_factory_exported(self):
        """The bulkhead_policy factory is exported from __init__.py."""
        from baldur.services.bulkhead import bulkhead_policy as exported_factory

        assert exported_factory is bulkhead_policy

    def test_async_bulkhead_policy_factory_exported(self):
        """The async_bulkhead_policy factory is exported from __init__.py."""
        from baldur.services.bulkhead import (
            async_bulkhead_policy as exported_factory,
        )

        assert exported_factory is async_bulkhead_policy

    def test_policy_classes_in_all(self):
        """Policy classes/factories are present in __all__."""
        import baldur.services.bulkhead as pkg

        assert "BulkheadPolicy" in pkg.__all__
        assert "AsyncBulkheadPolicy" in pkg.__all__
        assert "bulkhead_policy" in pkg.__all__
        assert "async_bulkhead_policy" in pkg.__all__
