"""aprotect() async CB + retry + retry-DLQ integration tests (670 D1/D3/D4).

Target:
- protect_facade.py (aprotect / aprotect_with_meta / _build_async_composer)

These are mock-based composition tests (no Docker): ``aprotect`` assembles
AsyncCircuitBreakerPolicy + AsyncRetryPolicy + DLQSink through AsyncPolicyComposer
and shares one CircuitBreakerService per name with the sync path. They assert
cross-component interactions the SCs pin:

- an async fn drives a shared breaker OPEN (no silent bypass),
- the SAME breaker is visible to the sync ``protect(name=...)`` path,
- async retry re-executes an ``async def`` and reports the attempt count,
- an exhausted async retry with ``dlq=True`` fires the DLQ sink (``should_dlq``
  armed).

INTEGRATION_TEST_GUIDELINES.md: Baldur-service composition only → InMemory /
mock-based, no infra markers. Backoff sleeps are patched so exhaustion tests do
not wait real wall-clock time (§6.3).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from baldur import protect_facade
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.core.execution_mode import clear_execution_mode_override
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.protect_facade import (
    _build_async_composer,
    aprotect,
    aprotect_with_meta,
    protect_with_meta,
    reset_protect_caches,
)
from baldur.services.circuit_breaker.config import (
    CircuitBreakerConfig,
    CircuitState,
)
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.retry_handler.models import RetryPolicyConfig

# The async retry stage sleeps between attempts via this symbol — patch it so
# exhaustion tests run instantly and deterministically.
_ASYNC_SLEEP = "baldur.resilience.policies.async_retry.asyncio.sleep"


@pytest.fixture
def clean_caches():
    """Isolate process-local protect() caches + execution mode around each test.

    The shared-breaker / cb-open tests seed ``_cb_policy_cache`` directly; the
    reset both before and after guarantees no per-name breaker (or a prior
    test's shadow mode) leaks across tests.
    """
    clear_execution_mode_override()
    reset_protect_caches()
    yield
    reset_protect_caches()
    clear_execution_mode_override()


def _seed_low_threshold_breaker(
    name: str, *, failure_threshold: int = 2
) -> CircuitBreakerService:
    """Inject a real, low-threshold breaker into the per-name cache for ``name``.

    Both ``aprotect(name=…)`` and ``protect(name=…)`` resolve the breaker via
    ``_get_or_build_cb_policy(name)``, which reads this cache — so seeding it
    makes the two call styles share one CircuitBreakerService (D3). ``minimum_calls=1``
    + ``failure_rate_threshold=0`` make the breaker open deterministically after
    exactly ``failure_threshold`` count-based failures.
    """
    cb_config = CircuitBreakerConfig(
        enabled=True,
        failure_threshold=failure_threshold,
        minimum_calls=1,
        failure_rate_threshold=0,
        recovery_timeout=60,
    )
    cb_service = CircuitBreakerService(
        config=cb_config,
        repository=InMemoryCircuitBreakerStateRepository(),
    )
    policy = CircuitBreakerPolicy(service_name=name, cb_service=cb_service, hooks=[])
    protect_facade._cb_policy_cache[name] = policy
    return cb_service


# =============================================================================
# Behavior — async circuit breaker opens (no silent bypass)
# =============================================================================


class TestAprotectAsyncCircuitBreakerBehavior:
    """The async path applies a real breaker that opens on accumulated failure."""

    def test_cb_opens_async_after_threshold_failures(self, clean_caches):
        """Repeated failing async calls open the shared breaker → CircuitBreakerOpenError.

        Proves the async fn is actually awaited and its failures recorded (no
        silent bypass): the breaker only opens because real awaited outcomes
        reached ``record_failure``.
        """
        name = "async.cb_opens_async"
        cb_service = _seed_low_threshold_breaker(name, failure_threshold=2)

        calls = {"n": 0}

        async def boom():
            calls["n"] += 1
            raise RuntimeError("dependency down")

        open_error_seen = False
        for _ in range(5):
            try:
                asyncio.run(
                    aprotect(
                        name=name,
                        fn=boom,
                        circuit_breaker=True,
                        retry=False,
                        dlq=False,
                        timeout=None,
                    )
                )
            except CircuitBreakerOpenError:
                open_error_seen = True
            except RuntimeError:
                pass

        assert open_error_seen is True
        assert cb_service.get_or_create_state(name).state == CircuitState.OPEN
        # The async fn was really awaited at least until the breaker tripped.
        assert calls["n"] >= 2

    def test_shared_breaker_open_visible_across_sync_protect(self, clean_caches):
        """A breaker opened via ``aprotect`` is OPEN for the sync ``protect`` (same name).

        Opens the breaker on the async path, then a sync ``protect_with_meta``
        against the same name is REJECTED without ever running its function —
        the two paths share one CircuitBreakerService (D3).
        """
        name = "async.shared_breaker"
        _seed_low_threshold_breaker(name, failure_threshold=2)

        async def boom():
            raise RuntimeError("dependency down")

        # Drive the async breaker OPEN.
        for _ in range(4):
            try:
                asyncio.run(
                    aprotect(
                        name=name,
                        fn=boom,
                        circuit_breaker=True,
                        retry=False,
                        dlq=False,
                        timeout=None,
                    )
                )
            except (CircuitBreakerOpenError, RuntimeError):
                pass

        # The sync path sees the SAME open breaker and fast-fails.
        sync_ran = {"n": 0}

        def sync_fn():
            sync_ran["n"] += 1
            return "ok"

        meta = protect_with_meta(
            name,
            sync_fn,
            circuit_breaker=True,
            retry=False,
            dlq=False,
            timeout=None,
        )

        assert meta.outcome == PolicyOutcome.REJECTED
        assert isinstance(meta.error, CircuitBreakerOpenError)
        assert sync_ran["n"] == 0  # sync fn never ran — breaker rejected it


# =============================================================================
# Behavior — async retry re-executes an async def
# =============================================================================


class TestAprotectAsyncRetryBehavior:
    """``aprotect(retry=…)`` retries an ``async def`` through the composer."""

    def test_retry_async_succeeds_after_transient_failures(self, clean_caches):
        """Transient failures below the cap retry, then aprotect returns the value."""
        name = "async.retry_async_ok"

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("transient")
            return "recovered"

        cfg = RetryPolicyConfig(max_attempts=5, domain=name)
        with patch(_ASYNC_SLEEP, new_callable=AsyncMock):
            result = asyncio.run(
                aprotect(
                    name=name,
                    fn=flaky,
                    retry=cfg,
                    circuit_breaker=False,
                    dlq=False,
                    timeout=None,
                )
            )

        assert result == "recovered"
        assert calls["n"] == 3  # succeeds on the 3rd attempt

    def test_retry_async_exhaustion_raises_last_error(self, clean_caches):
        """Exhausting the async retry stage raises the last error and runs max_attempts."""
        name = "async.retry_async_exhaust"

        calls = {"n": 0}

        async def always_fail():
            calls["n"] += 1
            raise ConnectionError("down")

        cfg = RetryPolicyConfig(max_attempts=3, domain=name)
        with patch(_ASYNC_SLEEP, new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="down"):
                asyncio.run(
                    aprotect(
                        name=name,
                        fn=always_fail,
                        retry=cfg,
                        circuit_breaker=False,
                        dlq=False,
                        timeout=None,
                    )
                )

        assert calls["n"] == 3  # max_attempts attempts, no fallback


# =============================================================================
# Behavior — async retry + DLQ sink fires on exhaustion
# =============================================================================


class TestAprotectAsyncRetryDLQBehavior:
    """``aprotect(retry=True, dlq=True)`` routes an exhausted async failure to the sink."""

    def test_retry_dlq_async_routes_exhausted_failure_to_sink(self, clean_caches):
        """The async retry stage arms ``should_dlq`` so the DLQ sink stores on exhaustion.

        Without D1's ``should_dlq`` metadata the sink would return None and the
        failure would be silently dropped. Assert the store helper fired with the
        right domain and the sink id surfaced in the result metadata.
        """
        name = "async.retry_dlq_async"

        async def always_fail():
            raise ConnectionError("permanent")

        store_result = MagicMock(success=True, dlq_id="dlq-async-1")
        with (
            patch(_ASYNC_SLEEP, new_callable=AsyncMock),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                autospec=True,
            ) as mock_store,
        ):
            mock_store.return_value = store_result
            meta = asyncio.run(
                aprotect_with_meta(
                    name=name,
                    fn=always_fail,
                    retry=True,
                    dlq=True,
                    circuit_breaker=False,
                    timeout=None,
                )
            )

        assert meta.success is False
        assert meta.outcome == PolicyOutcome.FAILURE
        # Sink actually fired — should_dlq armed by the async retry stage.
        mock_store.assert_called_once()
        assert mock_store.call_args.kwargs["domain"] == name
        assert meta.metadata["sink_id"] == "dlq-async-1"

    def test_retry_dlq_async_skips_sink_when_dlq_disabled(self, clean_caches):
        """With ``dlq=False`` the sink is not wired — the store helper never fires."""
        name = "async.retry_dlq_async_off"

        async def always_fail():
            raise ConnectionError("permanent")

        with (
            patch(_ASYNC_SLEEP, new_callable=AsyncMock),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                autospec=True,
            ) as mock_store,
        ):
            meta = asyncio.run(
                aprotect_with_meta(
                    name=name,
                    fn=always_fail,
                    retry=True,
                    dlq=False,
                    circuit_breaker=False,
                    timeout=None,
                )
            )

        assert meta.success is False
        mock_store.assert_not_called()
        assert "sink_id" not in meta.metadata


# =============================================================================
# Behavior — _build_async_composer sync-mirrored chain order (670 D1)
# =============================================================================


class TestBuildAsyncComposerChainBehavior:
    """The async composer nests policies in the sync-mirrored add-order.

    Order is load-bearing: CB (outermost) wraps Retry so one exhausted
    retry-sequence counts as a single CB failure, and Timeout wraps Retry so a
    single global timeout bounds the whole sequence (not per-attempt).
    """

    async def _fallback(self):
        return "fb"

    def test_full_chain_order_is_cb_timeout_retry_fallback(self, clean_caches):
        """All four stages present → add-order CB → Timeout → Retry → Fallback."""
        composer = _build_async_composer(
            name="async.chain",
            fallback=self._fallback,
            dlq=False,
            retry_cfg=RetryPolicyConfig(max_attempts=2, domain="async.chain"),
            circuit_breaker=True,
            timeout_seconds=1.0,
        )

        assert [p.name for p in composer._policies] == [
            "circuit_breaker",
            "timeout",
            "retry",
            "fallback",
        ]

    def test_cb_precedes_retry_when_timeout_absent(self, clean_caches):
        """Even without a timeout, CB stays outside Retry (CB → Retry)."""
        composer = _build_async_composer(
            name="async.chain2",
            fallback=None,
            dlq=False,
            retry_cfg=RetryPolicyConfig(max_attempts=2, domain="async.chain2"),
            circuit_breaker=True,
            timeout_seconds=None,
        )

        names = [p.name for p in composer._policies]
        assert names == ["circuit_breaker", "retry"]
        assert names.index("circuit_breaker") < names.index("retry")

    def test_omitted_stages_are_absent(self, clean_caches):
        """Disabled CB + no retry/fallback → only the timeout stage is present."""
        composer = _build_async_composer(
            name="async.chain3",
            fallback=None,
            dlq=False,
            retry_cfg=None,
            circuit_breaker=False,
            timeout_seconds=2.0,
        )

        assert [p.name for p in composer._policies] == ["timeout"]
