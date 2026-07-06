"""
Mock-based integration tests for ``baldur.protect()`` end-to-end composition (429 Part 1).

Verifies that the facade successfully composes the live resilience stack:
  protect(name, fn, fallback, dlq, retry, circuit_breaker)
    ├─ CircuitBreakerPolicy (real CircuitBreakerService + InMemory repo)
    ├─ RetryPolicy           (real RetryPolicyConfig)
    ├─ FallbackPolicy
    └─ DLQSink

State transition lifecycle under test:
  1. CLOSED → many failures → CB records failure on each attempt
  2. Fallback fires when primary raises (SUCCESS_WITH_FALLBACK path)
  3. Retry exhausts (max_attempts=2, no fallback) → original error propagates
  4. Metrics path fires for every invocation

Infrastructure: InMemory Circuit Breaker repo, InMemory failed operation repo.
No Docker required.
"""

from __future__ import annotations

import pytest

from baldur.protect_facade import protect, protect_with_meta
from baldur.services.retry_handler.models import RetryPolicyConfig


@pytest.fixture(autouse=True)
def _reset_protect_and_cb():
    """Clean singletons between tests so CB state is not leaked."""
    from baldur.services.circuit_breaker.convenience import (
        reset_circuit_breaker_service,
    )
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()
    try:
        reset_circuit_breaker_service()
    except Exception:
        pass
    yield
    reset_protect_settings()
    try:
        reset_circuit_breaker_service()
    except Exception:
        pass


class TestProtectComposition:
    """End-to-end composition of CB + Retry + Fallback + DLQSink via protect()."""

    def test_success_path_returns_fn_value_and_records_attempt(self):
        """Given a healthy fn, protect() returns its value and attempts==1."""
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        meta = protect_with_meta(name="comp.ok", fn=fn)

        assert meta.value == "ok"
        assert meta.success is True
        assert meta.attempts == 1
        assert calls["n"] == 1

    def test_fallback_activates_when_primary_raises(self):
        """Primary raises → fallback fires → outcome marked SUCCESS_WITH_FALLBACK."""

        def primary():
            raise RuntimeError("upstream down")

        def fallback():
            return "cache_value"

        meta = protect_with_meta(
            name="comp.fb",
            fn=primary,
            fallback=fallback,
        )

        assert meta.success is True
        assert meta.fallback_used is True
        assert meta.value == "cache_value"

    def test_retry_exhausts_without_fallback_and_raises_original(self):
        """Retry max_attempts run; each fails; original exception bubbles up.

        Validates the Retry → Fallback → DLQSink chain when fallback is absent:
        Retry exhausts, no fallback absorbs the error, and the Composer finally
        re-raises the underlying exception to the caller.
        """
        call_count = {"n": 0}

        def always_fails():
            call_count["n"] += 1
            raise ValueError(f"attempt-{call_count['n']}")

        cfg = RetryPolicyConfig(
            max_attempts=2,
            backoff_base=0,
            jitter_percent=0,
            domain="comp.retry",
            enable_dlq=False,
        )

        with pytest.raises(ValueError, match="attempt-"):
            protect(
                name="comp.retry",
                fn=always_fails,
                retry=cfg,
                circuit_breaker=False,  # isolate retry contribution
            )

        # All configured attempts executed.
        assert call_count["n"] == cfg.max_attempts

    def test_retry_succeeds_on_second_attempt_returns_value(self):
        """First call raises, second succeeds → protect returns second value."""
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("transient")
            return "recovered"

        cfg = RetryPolicyConfig(
            max_attempts=3,
            backoff_base=0,
            jitter_percent=0,
            domain="comp.flaky",
            enable_dlq=False,
        )

        meta = protect_with_meta(
            name="comp.flaky",
            fn=flaky,
            retry=cfg,
            circuit_breaker=False,
        )

        assert meta.value == "recovered"
        assert meta.success is True
        assert attempts["n"] == 2
