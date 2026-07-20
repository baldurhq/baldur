"""Result-predicate retry tests (704 D1).

Target: ``services/retry_handler/policy.py`` + ``services/retry_handler/models.py``.

``retry_on_result`` retries a call that *returns* a soft-error value (200 + error
payload, ``None``, partial response) which never raises. Coverage:
- the success-path predicate check (accept / reject / ``None`` rejection),
- result-exhaustion synthesizing a ``MaxRetriesExceededError`` that carries
  ``last_result`` + ``result_rejected`` (the ``is_result_exhaustion``
  discriminator, correct even when the rejected value is ``None``),
- fail-open on a broken predicate (accept the result, log, never retry),
- ``async def`` predicate rejected at construction,
- the ``protect()``-through-composer path that must map the synthesized
  ``FAILURE(value=last_result, error=synthesized)`` to a raised
  ``MaxRetriesExceededError`` rather than the ``error=None`` REJECTED edge.

The retry loop runs with a zero-delay backoff and a no-op sleeper so no
wall-clock time passes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.retry_handler.models import (
    MaxRetriesExceededError,
    RetryPolicyConfig,
)
from baldur.services.retry_handler.policy import RetryPolicy


async def _async_predicate(result: object) -> bool:
    """An ``async def`` predicate — must be rejected at policy construction."""
    return True


def _raising_predicate(result: object) -> bool:
    """A broken predicate that always raises (drives the fail-open path)."""
    raise RuntimeError("predicate broken")


def _predicate_policy(
    predicate,
    *,
    max_attempts: int = 3,
    domain: str = "test_result_predicate",
) -> RetryPolicy:
    """RetryPolicy on the real retry loop with a zero-delay, no-wait backoff."""
    return RetryPolicy(
        config=RetryPolicyConfig(
            max_attempts=max_attempts,
            retry_on_result=predicate,
            domain=domain,
        ),
        backoff=ConstantBackoff(delay=0.0),
        sleeper=lambda _: None,
    )


# =============================================================================
# Behavior — the success-path result predicate drives retry / accept / exhaust
# =============================================================================


class TestRetryResultPredicateBehavior:
    """``retry_on_result`` retries soft-error results and synthesizes exhaustion."""

    def test_matching_result_retried_until_a_non_matching_result_succeeds(self):
        """A rejected result retries; the first accepted result returns SUCCESS."""
        # Given — attempt 1 returns a soft error, attempt 2 a good value
        values = iter(["soft-error", "ok"])
        policy = _predicate_policy(lambda r: r == "soft-error", max_attempts=3)

        # When
        result = policy.execute(lambda: next(values))

        # Then — the loop retried once and accepted the second result
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.total_attempts == 2

    def test_always_matching_result_exhausts_with_synthesized_error(self):
        """A result the predicate always rejects exhausts into a synthesized
        MaxRetriesExceededError carrying the last value on both slots."""
        # Given
        payload = {"status": "error"}
        policy = _predicate_policy(lambda r: True, max_attempts=3)

        # When
        result = policy.execute(lambda: payload)

        # Then — FAILURE, real synthesized exception, last value preserved
        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, MaxRetriesExceededError)
        assert result.error.is_result_exhaustion is True
        assert result.error.last_result == payload
        assert result.value == payload  # survives on PolicyResult.value too
        assert result.total_attempts == 3

    def test_none_result_rejection_reports_result_exhaustion(self):
        """A predicate that rejects ``None`` still reports is_result_exhaustion —
        the discriminator must not be inferred from ``last_result is not None``."""
        # Given
        policy = _predicate_policy(lambda r: r is None, max_attempts=2)

        # When
        result = policy.execute(lambda: None)

        # Then
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error.last_result is None
        assert result.error.is_result_exhaustion is True

    def test_non_matching_result_returns_success_on_first_attempt(self):
        """A predicate that accepts the first result returns immediately, no retry."""
        # Given
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        policy = _predicate_policy(lambda r: r == "bad", max_attempts=3)

        # When
        result = policy.execute(fn)

        # Then — one call, no retry
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.total_attempts == 1
        assert len(calls) == 1

    def test_result_rejection_records_result_type_in_history_without_payload(self):
        """Each rejection appends a history entry naming the result *type* only —
        never the payload (audit/DLQ safety)."""
        # Given — a payload with a secret that must never be serialized
        secret_payload = {"password": "hunter2"}
        policy = _predicate_policy(lambda r: True, max_attempts=2)

        # When
        result = policy.execute(lambda: secret_payload)

        # Then — every rejection entry carries result_type, not the value
        history = result.metadata["retry_history"]
        rejected = [h for h in history if h.get("result_rejected")]
        assert len(rejected) == 2
        for entry in rejected:
            assert entry["result_type"] == "dict"
        assert "hunter2" not in str(history)


class TestRetryResultPredicateFailOpen:
    """A broken result predicate fails open — accept the result, never retry."""

    def test_predicate_exception_accepts_result_without_retry(self):
        """A predicate that raises is treated as *not rejected*: one attempt,
        the result returned as success (re-execution would amplify side effects)."""
        # Given
        calls = []

        def fn():
            calls.append(1)
            return "value"

        policy = _predicate_policy(_raising_predicate, max_attempts=3)

        # When
        result = policy.execute(fn)

        # Then — the raising predicate did not cause a retry
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "value"
        assert result.total_attempts == 1
        assert len(calls) == 1

    def test_predicate_exception_logs_result_predicate_failed_warning(self):
        """The fail-open path logs retry.result_predicate_failed exactly once."""
        # Given
        policy = _predicate_policy(_raising_predicate, max_attempts=3)

        # When
        with capture_logs() as logs:
            policy.execute(lambda: "value")

        # Then
        events = [e for e in logs if e["event"] == "retry.result_predicate_failed"]
        assert len(events) == 1


class TestRetryResultPredicateDisabled:
    """A disabled retry stage never evaluates the result predicate."""

    def test_globally_disabled_retry_skips_predicate_evaluation(self):
        """With retry globally disabled the single-attempt path returns the
        result verbatim even when the predicate would reject it."""
        # Given — retry globally off at construction
        with patch(
            "baldur.settings.retry.get_retry_settings",
            return_value=SimpleNamespace(enabled=False),
        ):
            policy = RetryPolicy(
                config=RetryPolicyConfig(
                    max_attempts=3,
                    retry_on_result=lambda r: True,  # would reject everything
                    domain="test_result_predicate",
                )
            )

        # When
        result = policy.execute(lambda: "would-match")

        # Then — one attempt, the "rejected" value returned as success
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "would-match"
        assert result.total_attempts == 1


# =============================================================================
# Behavior — coroutine predicates are rejected at construction
# =============================================================================


class TestRetryPredicateConstruction:
    """``async def`` predicates are rejected; sync predicates are accepted."""

    def test_sync_policy_rejects_async_predicate_with_type_error(self):
        """A coroutine-function predicate raises TypeError on RetryPolicy build."""
        with pytest.raises(TypeError):
            RetryPolicy(config=RetryPolicyConfig(retry_on_result=_async_predicate))

    def test_async_policy_rejects_async_predicate_with_type_error(self):
        """AsyncRetryPolicy rejects a coroutine-function predicate identically."""
        from baldur.resilience.policies.async_retry import AsyncRetryPolicy

        with pytest.raises(TypeError):
            AsyncRetryPolicy(retry_on_result=_async_predicate)

    def test_sync_predicate_is_stored_at_construction(self):
        """A plain synchronous predicate is accepted and retained."""
        predicate = lambda r: r is None  # noqa: E731
        policy = RetryPolicy(config=RetryPolicyConfig(retry_on_result=predicate))
        assert policy._retry_on_result is predicate


# =============================================================================
# Contract — MaxRetriesExceededError result-exhaustion slots
# =============================================================================


class TestMaxRetriesExceededErrorContract:
    """The synthesized exhaustion error's discriminator and context keys."""

    def test_is_result_exhaustion_tracks_result_rejected_flag(self):
        """is_result_exhaustion is True iff result_rejected was set."""
        rejected = MaxRetriesExceededError(
            "msg",
            retry_count=3,
            max_retries=3,
            last_result={"e": 1},
            result_rejected=True,
        )
        assert rejected.is_result_exhaustion is True

    def test_is_result_exhaustion_false_for_exception_exhaustion(self):
        """Exception-driven exhaustion is not a result exhaustion."""
        err = MaxRetriesExceededError(
            "msg", retry_count=3, max_retries=3, last_error=ConnectionError("x")
        )
        assert err.is_result_exhaustion is False

    def test_is_result_exhaustion_true_even_when_last_result_is_none(self):
        """The flag — not ``last_result is not None`` — is the discriminator."""
        err = MaxRetriesExceededError(
            "msg",
            retry_count=2,
            max_retries=2,
            last_result=None,
            result_rejected=True,
        )
        assert err.last_result is None
        assert err.is_result_exhaustion is True

    def test_extra_context_carries_result_rejected_marker_only(self):
        """extra_context() adds ``result_rejected=True`` but never the payload."""
        err = MaxRetriesExceededError(
            "msg",
            retry_count=2,
            max_retries=2,
            last_result={"secret": "hunter2"},
            result_rejected=True,
        )
        ctx = err.extra_context()
        assert ctx["result_rejected"] is True
        assert "hunter2" not in str(ctx)

    def test_extra_context_omits_result_rejected_for_exception_exhaustion(self):
        """No ``result_rejected`` key for an exception exhaustion; last_error kept."""
        err = MaxRetriesExceededError(
            "msg", retry_count=2, max_retries=2, last_error=ValueError("boom")
        )
        ctx = err.extra_context()
        assert "result_rejected" not in ctx
        assert ctx["last_error"] == "boom"


# =============================================================================
# Behavior — retry_on_result sourcing on the config dataclass
# =============================================================================


class TestRetryConfigMappingBehavior:
    """``retry_on_result`` is never set by env — it is constructor-only."""

    def test_from_settings_leaves_retry_on_result_none(self):
        """A callable cannot round-trip through settings, so from_settings never
        sets ``retry_on_result`` (constructor/decorator-only field)."""
        cfg = RetryPolicyConfig.from_settings("default")
        assert cfg.retry_on_result is None


# =============================================================================
# Integration (mock-based) — protect() maps result-exhaustion through composer
# =============================================================================


class TestProtectResultPredicateComposition:
    """``protect(retry=RetryPolicyConfig(retry_on_result=...))`` maps the
    synthesized FAILURE through PolicyComposer as a raised
    MaxRetriesExceededError — NOT the ``error=None`` REJECTED misclassification.
    """

    @pytest.fixture(autouse=True)
    def _reset_protect_state(self):
        from baldur.protect_facade import reset_protect_caches

        reset_protect_caches()
        yield
        reset_protect_caches()

    def _cfg(self, predicate, *, max_attempts: int = 2) -> RetryPolicyConfig:
        return RetryPolicyConfig(
            max_attempts=max_attempts,
            backoff_base=0,
            backoff_max=0,
            jitter_percent=0,
            retry_on_result=predicate,
            domain="test_protect_softretry",
        )

    def test_result_exhaustion_propagates_max_retries_error_through_composer(self):
        """Soft-error exhaustion under protect() raises MaxRetriesExceededError
        with the last value preserved (composer classification is correct)."""
        from baldur.protect_facade import protect

        def soft_error():
            return {"status": "error"}

        with pytest.raises(MaxRetriesExceededError) as exc_info:
            protect(
                "test.softretry",
                soft_error,
                retry=self._cfg(lambda r: r.get("status") == "error"),
                circuit_breaker=False,
                timeout=None,
            )

        assert exc_info.value.is_result_exhaustion is True
        assert exc_info.value.last_result == {"status": "error"}

    def test_recovering_soft_error_returns_value_through_composer(self):
        """A soft error that recovers on retry returns the good value via protect()."""
        from baldur.protect_facade import protect

        values = iter([{"status": "error"}, {"status": "ok"}])

        out = protect(
            "test.softrecover",
            lambda: next(values),
            retry=self._cfg(lambda r: r.get("status") == "error"),
            circuit_breaker=False,
            timeout=None,
        )

        assert out == {"status": "ok"}
