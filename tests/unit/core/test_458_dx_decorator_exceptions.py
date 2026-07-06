"""Unit tests for #458 exception additions in core/exceptions.py.

Covers ``RateLimitExceeded`` (new ResilienceError subclass),
``IdempotencyDuplicateError`` (new direct BaldurError subclass), and
``IdempotencyUnavailableError`` (#567 D9 — direct BaldurError subclass).

Verification techniques applied:
- Contract: inheritance hierarchy (RateLimitExceeded → ResilienceError →
  BaldurError; IdempotencyDuplicateError / IdempotencyUnavailableError →
  BaldurError directly).
- Contract: ``extra_context()`` payload shape and conditional emptiness.
- Behavior: default vs explicit ``message`` and per-field defaults; ``from``
  exception chaining.
"""

from __future__ import annotations

import pytest

from baldur.core.exceptions import (
    BaldurError,
    IdempotencyDuplicateError,
    IdempotencyUnavailableError,
    RateLimitExceeded,
    ResilienceError,
)

# =============================================================================
# Inheritance contract
# =============================================================================


class TestExceptionInheritanceContract:
    def test_rate_limit_exceeded_inherits_resilience_error(self):
        assert issubclass(RateLimitExceeded, ResilienceError)
        assert issubclass(RateLimitExceeded, BaldurError)

    def test_idempotency_duplicate_error_inherits_baldur_error_directly(self):
        assert issubclass(IdempotencyDuplicateError, BaldurError)
        # Per #458 §G4: IdempotencyDuplicateError is a correctness contract,
        # NOT a resilience stage.
        assert not issubclass(IdempotencyDuplicateError, ResilienceError)

    def test_idempotency_unavailable_error_inherits_baldur_error_directly(self):
        # #567 D9: a sibling of IdempotencyDuplicateError — also a correctness
        # contract (the verdict is unknown), not a resilience stage.
        assert issubclass(IdempotencyUnavailableError, BaldurError)
        assert not issubclass(IdempotencyUnavailableError, ResilienceError)

    def test_unavailable_and_duplicate_are_distinct_types(self):
        # Neither is a subtype of the other: "couldn't verify" must never be
        # caught as "deduped (safe)" or vice versa.
        assert not issubclass(IdempotencyUnavailableError, IdempotencyDuplicateError)
        assert not issubclass(IdempotencyDuplicateError, IdempotencyUnavailableError)


# =============================================================================
# RateLimitExceeded — message formatting and extra_context
# =============================================================================


class TestRateLimitExceededContract:
    def test_default_message_uses_key_limit_and_window(self):
        err = RateLimitExceeded(key="orders.charge", limit=10, window_seconds=60)
        assert "orders.charge" in str(err)
        assert "10/60s" in str(err)

    def test_explicit_message_overrides_default(self):
        err = RateLimitExceeded("custom message", key="x", limit=1, window_seconds=1)
        assert str(err) == "custom message"

    def test_extra_context_includes_all_fields_when_key_set(self):
        err = RateLimitExceeded(
            key="orders.charge", limit=10, window_seconds=60, reset_at=12345
        )
        ctx = err.extra_context()
        assert ctx == {
            "key": "orders.charge",
            "limit": 10,
            "window_seconds": 60,
            "reset_at": 12345,
        }

    def test_extra_context_empty_when_key_unset(self):
        # Per source: extra_context() only emits fields when key is set.
        err = RateLimitExceeded("anonymous failure")
        assert err.extra_context() == {}

    def test_field_defaults(self):
        err = RateLimitExceeded()
        assert err.key == ""
        assert err.limit == 0
        assert err.window_seconds == 0
        assert err.reset_at == 0


# =============================================================================
# IdempotencyDuplicateError — message formatting and extra_context
# =============================================================================


class TestIdempotencyDuplicateErrorContract:
    def test_default_message_uses_key_and_decision(self):
        err = IdempotencyDuplicateError(
            key="idempotency:custom:abc", domain="custom", decision="SKIP"
        )
        assert "idempotency:custom:abc" in str(err)
        assert "SKIP" in str(err)

    def test_explicit_message_overrides_default(self):
        err = IdempotencyDuplicateError(
            "another process is executing",
            key="x",
            domain="custom",
            decision="ABORT",
        )
        assert str(err) == "another process is executing"

    def test_extra_context_includes_all_fields_when_key_set(self):
        err = IdempotencyDuplicateError(
            key="idempotency:custom:abc",
            domain="custom",
            decision="SKIP",
        )
        assert err.extra_context() == {
            "key": "idempotency:custom:abc",
            "domain": "custom",
            "decision": "SKIP",
        }

    def test_extra_context_empty_when_key_unset(self):
        err = IdempotencyDuplicateError("opaque")
        assert err.extra_context() == {}

    def test_field_defaults(self):
        err = IdempotencyDuplicateError()
        assert err.key == ""
        assert err.domain == ""
        assert err.decision == ""


# =============================================================================
# IdempotencyUnavailableError — message formatting, extra_context, chaining
# =============================================================================


class TestIdempotencyUnavailableErrorContract:
    """#567 D9: signals an idempotency check could not complete (cache I/O
    fault) — the verdict is *unknown*, distinct from a *successful* dedup
    verdict (``IdempotencyDuplicateError``)."""

    def test_default_message_uses_key_and_error(self):
        err = IdempotencyUnavailableError(
            key="payment.charge:o-1", error="Redis ConnectionError"
        )
        assert "payment.charge:o-1" in str(err)
        assert "Redis ConnectionError" in str(err)

    def test_explicit_message_overrides_default(self):
        err = IdempotencyUnavailableError("cache unreachable", key="k", error="boom")
        assert str(err) == "cache unreachable"

    def test_extra_context_includes_set_fields(self):
        err = IdempotencyUnavailableError(key="k", error="boom")
        assert err.extra_context() == {"key": "k", "error": "boom"}

    def test_extra_context_empty_when_key_unset(self):
        # ``key`` gates ``key`` emission; ``error`` is emitted independently
        # only when set — an all-default instance yields an empty context.
        err = IdempotencyUnavailableError()
        assert err.extra_context() == {}

    def test_field_defaults(self):
        err = IdempotencyUnavailableError()
        assert err.key == ""
        assert err.error == ""

    def test_supports_from_chaining(self):
        # D9: the raw cache exception is wrapped (raised ``from`` it) so a
        # backend-specific error never leaks across the boundary but the cause
        # chain is preserved for debugging.
        cause = ConnectionError("redis down")
        with pytest.raises(IdempotencyUnavailableError) as exc_info:
            try:
                raise cause
            except ConnectionError as exc:
                raise IdempotencyUnavailableError(key="k", error=str(exc)) from exc
        assert exc_info.value.__cause__ is cause
