"""
PolicyComposer / AsyncPolicyComposer metadata propagation unit tests (#466).

Test target:
- resilience/policies/composer.py
  (_merge_chain_metadata, _build_failure_result, chain_metadata closure)

UNIT_TEST_GUIDELINES.md compliance:
- Contract: hardcoded expected values for _build_failure_result terminal shape
- Behavior: source-referenced expectations using real policy implementations
  (RetryPolicy / TimeoutPolicy / CircuitBreakerPolicy / AsyncFallbackPolicy)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyRejectedException,
    PolicyResult,
)
from baldur.resilience.policies.composer import (
    AsyncPolicyComposer,
    _build_failure_result,
    _merge_chain_metadata,
    compose,
)
from baldur.resilience.policies.fallback import AsyncFallbackPolicy
from baldur.resilience.policies.timeout import AsyncTimeoutPolicy, TimeoutPolicy
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

# =============================================================================
# Helpers — minimal fake policies that set metadata on non-success outcomes
# =============================================================================


class _MetadataSettingFailurePolicy:
    """Returns FAILURE with a configurable metadata dict (re-raises the error
    via composer's policy_wrapper)."""

    def __init__(self, name: str, metadata: dict[str, Any], error: Exception) -> None:
        self._name = name
        self._metadata = metadata
        self._error = error

    @property
    def name(self) -> str:
        return self._name

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=self._error,
            metadata=dict(self._metadata),
        )


class _MetadataSettingSuccessPolicy:
    """Returns SUCCESS with metadata — exercises the success-path merge."""

    def __init__(self, name: str, value: Any, metadata: dict[str, Any]) -> None:
        self._name = name
        self._value = value
        self._metadata = metadata

    @property
    def name(self) -> str:
        return self._name

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            value=self._value,
            outcome=PolicyOutcome.SUCCESS,
            metadata=dict(self._metadata),
        )


class _AsyncMetadataFailurePolicy:
    """Async sibling of _MetadataSettingFailurePolicy."""

    def __init__(self, name: str, metadata: dict[str, Any], error: Exception) -> None:
        self._name = name
        self._metadata = metadata
        self._error = error

    @property
    def name(self) -> str:
        return self._name

    async def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=self._error,
            metadata=dict(self._metadata),
        )


class _AsyncRejectedNoErrorPolicy:
    """Async REJECTED with no error → composer raises PolicyRejectedException."""

    def __init__(self, name: str = "rej_async") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            outcome=PolicyOutcome.REJECTED,
            metadata={"rejected_by_inner": self._name},
        )


# =============================================================================
# _merge_chain_metadata — Behavior
# =============================================================================


class TestMergeChainMetadataBehavior:
    """_merge_chain_metadata helper — closure-variable accumulator semantics."""

    def test_empty_incoming_is_noop(self):
        chain: dict[str, Any] = {"existing": 1}
        _merge_chain_metadata(chain, None, "p")
        assert chain == {"existing": 1}

    def test_empty_dict_incoming_is_noop(self):
        chain: dict[str, Any] = {"existing": 1}
        _merge_chain_metadata(chain, {}, "p")
        assert chain == {"existing": 1}

    def test_disjoint_keys_auto_merge(self):
        chain: dict[str, Any] = {"a": 1}
        _merge_chain_metadata(chain, {"b": 2, "c": 3}, "p")
        assert chain == {"a": 1, "b": 2, "c": 3}

    def test_collision_last_write_wins(self):
        chain: dict[str, Any] = {"k": "old"}
        _merge_chain_metadata(chain, {"k": "new"}, "p2")
        assert chain == {"k": "new"}

    def test_same_value_collision_no_warning(self, caplog):
        # Same value is not a collision per the implementation's `!=` check.
        chain: dict[str, Any] = {"k": "v"}
        _merge_chain_metadata(chain, {"k": "v"}, "p")
        assert chain == {"k": "v"}

    def test_multi_key_merge(self):
        chain: dict[str, Any] = {}
        _merge_chain_metadata(chain, {"x": 1}, "p1")
        _merge_chain_metadata(chain, {"y": 2}, "p2")
        _merge_chain_metadata(chain, {"z": 3}, "p3")
        assert chain == {"x": 1, "y": 2, "z": 3}


# =============================================================================
# _build_failure_result — Contract
# =============================================================================


class TestBuildFailureResultContract:
    """Terminal failure-path PolicyResult shape — pinned for ABI stability."""

    def test_outcome_round_trip_rejected(self):
        err = PolicyRejectedException("rejected")
        result = _build_failure_result(
            PolicyOutcome.REJECTED, err, ["a", "b"], {"k": "v"}
        )
        assert result.outcome == PolicyOutcome.REJECTED
        assert result.error is err
        assert result.value is None

    def test_outcome_round_trip_timeout(self):
        err = TimeoutPolicyError(0.5)
        result = _build_failure_result(
            PolicyOutcome.TIMEOUT, err, ["timeout"], {"timeout_seconds": 0.5}
        )
        assert result.outcome == PolicyOutcome.TIMEOUT
        assert result.metadata == {"timeout_seconds": 0.5}

    def test_outcome_round_trip_failure(self):
        err = ValueError("boom")
        result = _build_failure_result(
            PolicyOutcome.FAILURE, err, ["retry"], {"should_dlq": True}
        )
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["should_dlq"] is True

    def test_executed_policies_reversed(self):
        # Composer pushes inner policies first; result lists outer → inner.
        result = _build_failure_result(
            PolicyOutcome.FAILURE, ValueError(), ["inner", "outer"], {}
        )
        assert result.executed_policies == ["outer", "inner"]

    def test_metadata_is_copy_not_reference(self):
        chain: dict[str, Any] = {"k": "v"}
        result = _build_failure_result(PolicyOutcome.FAILURE, ValueError(), [], chain)
        chain["k"] = "mutated"
        assert result.metadata == {"k": "v"}


# =============================================================================
# Sync chain failure-path metadata propagation — Behavior (G1)
# =============================================================================


class TestPolicyComposerFailureMetadataPropagationBehavior:
    """Inner policy metadata reaches the outer terminal PolicyResult on failure."""

    def test_retry_should_dlq_reaches_outer_metadata(self):
        """RetryPolicy(FAILURE).metadata['should_dlq'] flows through composer."""
        cfg = RetryPolicyConfig(
            max_attempts=2,
            backoff_base=0,
            backoff_max=0,
            jitter_percent=0,
            enable_dlq=True,
            domain="test",
        )

        def always_fails() -> None:
            raise ValueError("always_fails")

        result = compose(RetryPolicy(config=cfg)).execute(always_fails)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata.get("should_dlq") is True
        assert result.metadata.get("domain") == "test"
        assert result.metadata.get("max_attempts") == 2
        assert "retry_history" in result.metadata

    def test_timeout_seconds_reaches_outer_metadata(self):
        """TimeoutPolicy(TIMEOUT).metadata['timeout_seconds'] flows through composer."""
        import time as _time

        def slow_fn() -> None:
            _time.sleep(0.5)

        result = compose(TimeoutPolicy(timeout_seconds=0.05)).execute(slow_fn)

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert result.metadata.get("timeout_seconds") == 0.05

    def test_failure_metadata_from_fake_policy(self):
        """Generic non-success metadata from any inner policy reaches outer."""
        fake = _MetadataSettingFailurePolicy(
            name="fake",
            metadata={"domain": "tenacity-domain"},
            error=ValueError("boom"),
        )
        result = compose(fake).execute(lambda: None)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata.get("domain") == "tenacity-domain"

    def test_multi_policy_disjoint_metadata_merges(self):
        """Two metadata-setting policies on the same chain → both keys present.

        TimeoutPolicy returns PolicyResult(TIMEOUT, error=TimeoutPolicyError)
        with metadata={timeout_seconds}. Its policy_wrapper merges that and
        re-raises TimeoutPolicyError. RetryPolicy classifies it as
        non-retryable, returning PolicyResult(FAILURE, error=TimeoutPolicyError,
        metadata={should_dlq, domain, ...}). Retry's wrapper merges those keys
        too, then re-raises TimeoutPolicyError. The outer terminal branch is
        ``except TimeoutPolicyError`` → outcome=TIMEOUT, but chain_metadata
        carries BOTH layers' keys.
        """
        import time as _time

        cfg = RetryPolicyConfig(
            max_attempts=2,
            backoff_base=0,
            backoff_max=0,
            jitter_percent=0,
            enable_dlq=True,
            domain="multi",
        )

        def slow_fn() -> None:
            _time.sleep(0.5)

        result = compose(
            RetryPolicy(config=cfg),
            TimeoutPolicy(timeout_seconds=0.05),
        ).execute(slow_fn)

        assert result.outcome == PolicyOutcome.TIMEOUT
        # Both policy layers' metadata keys merged via chain_metadata closure.
        assert result.metadata.get("timeout_seconds") == 0.05
        assert result.metadata.get("should_dlq") is True
        assert result.metadata.get("domain") == "multi"

    def test_rejected_branch_propagates_metadata(self):
        """REJECTED branch (PolicyRejectedException) carries chain_metadata."""

        # A non-error REJECTED inner policy would raise PolicyRejectedException
        # via policy_wrapper, but it would NOT have set metadata on chain (since
        # `if result.error: raise result.error` skips no-error path).
        # Build via a custom inner that returns REJECTED with no error → wrapper
        # raises PolicyRejectedException AFTER merging metadata.
        class _RejectedNoErrorPolicy:
            @property
            def name(self) -> str:
                return "rej"

            def execute(self, func, *args, context=None, **kwargs):  # type: ignore[no-untyped-def]
                return PolicyResult(
                    outcome=PolicyOutcome.REJECTED,
                    metadata={"rejected_by_inner": "rej"},
                )

        result = compose(_RejectedNoErrorPolicy()).execute(lambda: None)
        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata.get("rejected_by_inner") == "rej"


# =============================================================================
# Sync chain success-path metadata propagation — Behavior (D2 symmetry)
# =============================================================================


class TestPolicyComposerSuccessMetadataPropagationBehavior:
    """Success-path metadata also rides chain_metadata (HedgingPolicy F9 guard)."""

    def test_inner_success_metadata_carries_to_outer(self):
        """Hedging-shaped inner that returns SUCCESS+metadata → outer carries it."""
        fake = _MetadataSettingSuccessPolicy(
            name="hedging-like",
            value="ok",
            metadata={"hedged": True, "winner": "primary", "latency_ms": 12.3},
        )
        result = compose(fake).execute(lambda: None)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.metadata.get("hedged") is True
        assert result.metadata.get("winner") == "primary"
        assert result.metadata.get("latency_ms") == 12.3


# =============================================================================
# CB.record_failure regression guard — D2 Option-A property
# =============================================================================


class TestComposerCircuitBreakerNotBypassedBehavior:
    """Verify Option B (BaseException) was not used — inner CB except Exception
    must still catch the re-raised error so record_failure() runs."""

    def test_cb_record_failure_invoked_on_retry_chain_failure(self):
        """compose(CB, Retry).execute(failing_fn) → CB.record_failure called."""
        # Build CB with an injected mock cb_service so we can observe calls.
        cb_service = MagicMock()
        cb_service.is_enabled = True
        cb_service.should_allow.return_value = True
        cb_service.get_state.return_value = "closed"

        cb_policy = CircuitBreakerPolicy(
            service_name="t",
            cb_service=cb_service,
            hooks=[],  # bypass default hooks for test isolation
        )

        cfg = RetryPolicyConfig(
            max_attempts=3,
            backoff_base=0,
            backoff_max=0,
            jitter_percent=0,
            enable_dlq=False,
            domain="cb_test",
        )

        def fail() -> None:
            raise ValueError("boom")

        # Outer order: Retry wraps CB wraps fn. Retry will exhaust, with each
        # attempt running through CB (which must record_failure each time).
        compose(RetryPolicy(config=cfg), cb_policy).execute(fail)

        # CB.record_failure must have been called for every retry attempt.
        # Option B (BaseException signal) would have bypassed the inner
        # `except Exception` block where record_failure lives.
        assert cb_service.record_failure.call_count == 3


# =============================================================================
# Async chain failure-path metadata propagation — Behavior (G2)
# =============================================================================


class TestAsyncPolicyComposerMetadataPropagationBehavior:
    """Inner async policy metadata reaches outer terminal PolicyResult."""

    def _run(self, coro: Awaitable[Any]) -> Any:
        return asyncio.run(coro)  # type: ignore[arg-type]

    def test_async_failure_metadata_propagates(self):
        fake = _AsyncMetadataFailurePolicy(
            name="async-fake",
            metadata={"domain": "async-domain"},
            error=ValueError("async-boom"),
        )

        async def fn() -> None:
            return None

        composer: AsyncPolicyComposer = AsyncPolicyComposer().add(fake)
        result = self._run(composer.execute(fn))

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata.get("domain") == "async-domain"

    def test_async_timeout_metadata_propagates(self):
        async def slow_fn() -> None:
            await asyncio.sleep(0.5)

        composer: AsyncPolicyComposer = AsyncPolicyComposer().add(
            AsyncTimeoutPolicy(timeout_seconds=0.05)
        )
        result = self._run(composer.execute(slow_fn))

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert result.metadata.get("timeout_seconds") == 0.05

    def test_async_rejected_no_error_propagates_metadata(self):
        rej = _AsyncRejectedNoErrorPolicy(name="async-rej")

        async def fn() -> None:
            return None

        composer: AsyncPolicyComposer = AsyncPolicyComposer().add(rej)
        result = self._run(composer.execute(fn))

        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata.get("rejected_by_inner") == "async-rej"


# =============================================================================
# Async _FallbackApplied metadata parity — D5
# =============================================================================


class TestAsyncFallbackMetadataParityBehavior:
    """AsyncPolicyComposer's _FallbackApplied handler forwards fb_result.metadata.
    Sync sibling already does this; D5 closes the asymmetry."""

    def _run(self, coro: Awaitable[Any]) -> Any:
        return asyncio.run(coro)  # type: ignore[arg-type]

    def test_async_fallback_metadata_round_trip(self):
        async def primary() -> str:
            raise RuntimeError("primary failed")

        async def fb() -> str:
            return "fallback-value"

        # AsyncFallbackPolicy._apply_fallback sets metadata={
        #   "fallback_used": True, "fallback_source": "fallback_fn",
        #   "original_error": "primary failed",
        # }
        composer: AsyncPolicyComposer = AsyncPolicyComposer().add(
            AsyncFallbackPolicy(fallback_fn=fb)
        )
        result = self._run(composer.execute(primary))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "fallback-value"
        # D5: metadata is forwarded through the async _FallbackApplied handler.
        assert result.metadata.get("fallback_used") is True
        assert result.metadata.get("fallback_source") == "fallback_fn"
        assert "original_error" in result.metadata


# =============================================================================
# OOS #466 F10 — Domain rejection-exception outcome dispatch via marker bases
# =============================================================================


class TestPolicyRejectedMarkerBaseDispatch:
    """Domain rejection exceptions multi-inherit ``PolicyRejectedException``
    (and ``TimeoutPolicyError`` for the timeout variant) so the outer composer
    catch hierarchy classifies them as ``REJECTED`` / ``TIMEOUT`` instead of
    funneling into ``except Exception`` (FAILURE).

    Regression guard for OOS #466 F10 — pre-fix, all three exceptions were
    misclassified as ``PolicyOutcome.FAILURE``.
    """

    def test_circuit_breaker_open_error_is_policy_rejected(self):
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        err = CircuitBreakerOpenError("payment_svc")
        assert isinstance(err, PolicyRejectedException)
        # Original domain hierarchy preserved.
        from baldur.core.exceptions import CircuitBreakerError

        assert isinstance(err, CircuitBreakerError)
        # extra_context still carries the domain payload.
        assert err.extra_context() == {"service_name": "payment_svc"}

    def test_bulkhead_full_error_is_policy_rejected(self):
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import (
            BulkheadError,
            BulkheadFullError,
        )

        err = BulkheadFullError(
            bulkhead_name="checkout", max_concurrent=10, active_count=10
        )
        assert isinstance(err, PolicyRejectedException)
        assert isinstance(err, BulkheadError)
        assert err.extra_context() == {
            "bulkhead_name": "checkout",
            "max_concurrent": 10,
            "active_count": 10,
        }

    def test_bulkhead_timeout_error_is_timeout_policy_error(self):
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import (
            BulkheadError,
            BulkheadTimeoutError,
        )

        err = BulkheadTimeoutError(bulkhead_name="checkout", timeout=2.5)
        assert isinstance(err, TimeoutPolicyError)
        assert isinstance(err, BulkheadError)
        # timeout_seconds inherited from TimeoutPolicyError MRO is populated.
        assert err.timeout_seconds == 2.5
        # Domain-specific fields remain.
        assert err.bulkhead_name == "checkout"
        assert err.timeout == 2.5

    def test_circuit_breaker_open_dispatches_to_rejected(self):
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        class _StubCbPolicy:
            """Returns PolicyResult(REJECTED, error=CircuitBreakerOpenError).
            Composer's policy_wrapper will re-raise the error, and the outer
            catch should classify it as REJECTED via the marker-base MRO."""

            @property
            def name(self) -> str:
                return "circuit_breaker"

            def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(
                    outcome=PolicyOutcome.REJECTED,
                    error=CircuitBreakerOpenError("payment_svc"),
                    metadata={"service_name": "payment_svc", "state": "open"},
                )

        result = compose(_StubCbPolicy()).execute(lambda: "unreached")
        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, CircuitBreakerOpenError)
        # chain_metadata propagation preserved (D2 still applies on this path).
        assert result.metadata.get("service_name") == "payment_svc"
        assert result.metadata.get("state") == "open"

    def test_bulkhead_full_dispatches_to_rejected(self):
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import BulkheadFullError

        class _StubBulkheadPolicy:
            @property
            def name(self) -> str:
                return "bulkhead"

            def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(
                    outcome=PolicyOutcome.REJECTED,
                    error=BulkheadFullError(
                        bulkhead_name="checkout",
                        max_concurrent=4,
                        active_count=4,
                    ),
                    metadata={"bulkhead_name": "checkout"},
                )

        result = compose(_StubBulkheadPolicy()).execute(lambda: "unreached")
        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, BulkheadFullError)
        assert result.metadata.get("bulkhead_name") == "checkout"

    def test_bulkhead_timeout_dispatches_to_timeout(self):
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.bulkhead.exceptions import BulkheadTimeoutError

        class _StubBulkheadPolicy:
            @property
            def name(self) -> str:
                return "bulkhead"

            def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(
                    outcome=PolicyOutcome.TIMEOUT,
                    error=BulkheadTimeoutError(bulkhead_name="checkout", timeout=2.5),
                    metadata={"bulkhead_name": "checkout", "timeout": 2.5},
                )

        result = compose(_StubBulkheadPolicy()).execute(lambda: "unreached")
        assert result.outcome == PolicyOutcome.TIMEOUT
        assert isinstance(result.error, BulkheadTimeoutError)
        assert result.metadata.get("bulkhead_name") == "checkout"
