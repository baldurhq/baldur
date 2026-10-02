"""
Tests for IdempotencyGuard and IdempotencyHook (395 A3, extended by #567).

Covers:
- IdempotencyGuard.check() — context None, SKIP, CONTINUE, ABORT (#567 D1),
  block-path WARN (#567 D6), cache-error fail direction (#567 D9 / D5 WARN)
- IdempotencyHook.on_success/on_failure — key presence/absence, gate calls,
  fail-open WARN (#567 D5)
- Guard↔decorator block-event-name parity (#567 D8)
- AntiFlapping settings wiring + reset
- 595 D4 window threading — ``execution_ttl`` → ``check_and_acquire(ttl=)``,
  memory ``ttl`` → ``context.extra["_idempotency_ttl"]`` → hook ``mark_*``;
  fail-open cache error stores neither threading key
- 799 D1 fallback-answer mark rule, shared by the sync and async hooks —
  completed on plain success, failed (re-claimable) on a fallback answer with
  no work left running; a mark fault on the release branch logs
  ``idempotency.mark_failed_failed``
- 805 D10 abandoned-work rule: while recorded work runs the claim stays
  ``executing`` (a repeat reads ABORT); when it ends the key is completed only
  for a timeout whose own work returned, else failed — claim-scoped, carrying
  the record read at close, run in a copy of the caller's context; the async
  hook marks through the sync gate on the shared ledger and on its own loop on
  the in-process one; ``idempotency.mark_deferred`` /
  ``idempotency.deferred_mark_failed`` logs
"""

import asyncio
import contextvars
import threading
import time
from concurrent.futures import Future
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.abandoned_work import (
    WorkSummary,
    close_work_scope,
    current_work_scope,
    record_abandoned,
)
from baldur.core.exceptions import TimeoutPolicyError
from baldur.core.idempotency_gate import IdempotencyDecision, IdempotencyGate
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.resilience.policies.idempotency import (
    AsyncIdempotencyGuard,
    AsyncIdempotencyHook,
    IdempotencyGuard,
    IdempotencyHook,
    _ensure_async_policy_gate,
    _ensure_policy_gate,
    _read_keyed_call,
    _reset_policy_gate,
    _settle_call_ended_from_outside,
    _write_keyed_call,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def make_context():
    """Create a PolicyContext with extra dict."""

    def _make(extra=None):
        from baldur.interfaces.resilience_policy import PolicyContext

        return PolicyContext(
            domain="test_domain",
            extra=extra if extra is not None else {},
        )

    return _make


@pytest.fixture
def key_fn():
    """Simple key generator that returns a fixed key."""
    return lambda ctx: f"test_key_{ctx.domain}"


# =============================================================================
# IdempotencyGuard — Behavior (§8.2 Edge Cases, §8.5 Dependency Interaction)
# =============================================================================


class TestIdempotencyGuardBehavior:
    """IdempotencyGuard.check() behavior verification."""

    def test_name_is_idempotency(self, key_fn):
        """guard name is 'idempotency'."""
        guard = IdempotencyGuard(key_generator=key_fn)
        assert guard.name == "idempotency"

    def test_context_none_returns_allowed(self, key_fn):
        """context=None returns allowed=True (fail-open)."""
        guard = IdempotencyGuard(key_generator=key_fn)
        result = guard.check(context=None)
        assert result.allowed is True

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_continue_decision_returns_allowed_and_stores_key(
        self, mock_ensure_gate, key_fn, make_context
    ):
        """CONTINUE decision returns allowed=True and stores the key in context.extra."""
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        mock_gate = MagicMock()
        mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
            decision=IdempotencyDecision.CONTINUE,
            retry_count=2,
        )
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context()
        guard = IdempotencyGuard(key_generator=key_fn)
        result = guard.check(context=ctx)

        assert result.allowed is True
        assert ctx.extra["_idempotency_key"] == "test_key_test_domain"
        assert ctx.extra["_idempotency_retry_count"] == 2

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_skip_decision_returns_not_allowed(
        self, mock_ensure_gate, key_fn, make_context
    ):
        """SKIP decision returns allowed=False."""
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        mock_gate = MagicMock()
        mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
            decision=IdempotencyDecision.SKIP
        )
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context()
        guard = IdempotencyGuard(key_generator=key_fn)
        result = guard.check(context=ctx)

        assert result.allowed is False
        assert "Already processed" in (result.reason or "")

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_skip_decision_populates_decision_key_and_cached_result_metadata(
        self, mock_ensure_gate, key_fn, make_context
    ):
        """#567 D1: a SKIP block carries decision name + key + cached_result in
        ``GuardResult.metadata`` so the facade can build a precise exception."""
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        mock_gate = MagicMock()
        mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
            decision=IdempotencyDecision.SKIP,
            cached_result={"prior": "value"},
        )
        mock_ensure_gate.return_value = mock_gate

        guard = IdempotencyGuard(key_generator=key_fn)
        result = guard.check(context=make_context())

        assert result.allowed is False
        assert result.metadata["idempotency_decision"] == "SKIP"
        assert result.metadata["idempotency_key"] == "test_key_test_domain"
        assert result.metadata["cached_result"] == {"prior": "value"}

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_abort_decision_returns_not_allowed_with_abort_metadata(
        self, mock_ensure_gate, key_fn, make_context
    ):
        """#567 D1: ABORT (a concurrent in-flight duplicate) is blocked
        (``allowed=False``) just like SKIP — previously it fell through to
        ``allowed=True`` and the side effect ran more than once."""
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        mock_gate = MagicMock()
        mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
            decision=IdempotencyDecision.ABORT
        )
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context()
        guard = IdempotencyGuard(key_generator=key_fn)
        result = guard.check(context=ctx)

        assert result.allowed is False
        assert result.metadata["idempotency_decision"] == "ABORT"
        assert result.metadata["idempotency_key"] == "test_key_test_domain"
        assert "Another process is executing" in (result.reason or "")
        # The loser never owns the key, so it must NOT be stored for the hook.
        assert "_idempotency_key" not in ctx.extra

    def test_gate_failure_returns_not_allowed_fail_closed_by_default(
        self, make_context
    ):
        """D9: a cache/key error fails CLOSED by default (allowed=False) and
        marks the result unavailable, so a transient blip cannot let a duplicate
        side effect through."""

        def failing_key_fn(ctx):
            raise RuntimeError("key generation failed")

        ctx = make_context()
        guard = IdempotencyGuard(key_generator=failing_key_fn)
        result = guard.check(context=ctx)
        assert result.allowed is False
        assert result.metadata.get("idempotency_unavailable") is True


# =============================================================================
# IdempotencyGuard — cache-error fail direction (#567 D9, §8.1 Boundary)
# =============================================================================


class TestIdempotencyGuardFailDirectionBehavior:
    """#567 D9: a cache I/O error during the check fails CLOSED by default
    (``allowed=False`` + ``idempotency_unavailable`` marker); opt into fail-open
    via the per-call ``fail_open`` flag or
    ``IdempotencySettings.fail_open_on_cache_error``. An explicit per-call flag
    overrides the global setting."""

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    @staticmethod
    def _failing_key_fn(ctx):
        # A raising key generator hits the same ``except`` branch a cache I/O
        # fault would, deterministically and without patching a core method.
        raise RuntimeError("cache I/O error during check")

    @staticmethod
    def _reset_for_env():
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        reset_runtime()
        _reset_policy_gate()

    def test_per_call_fail_open_true_allows_through(self, make_context):
        guard = IdempotencyGuard(key_generator=self._failing_key_fn, fail_open=True)
        result = guard.check(context=make_context())
        assert result.allowed is True

    def test_per_call_fail_open_false_fails_closed_with_marker(self, make_context):
        guard = IdempotencyGuard(key_generator=self._failing_key_fn, fail_open=False)
        result = guard.check(context=make_context())
        assert result.allowed is False
        assert result.metadata.get("idempotency_unavailable") is True

    def test_settings_fail_open_consulted_when_flag_is_none(self, make_context):
        # Global posture = fail-open; per-call flag None → consult the setting.
        # 686: ``fail_open_on_cache_error`` resolves via the cached layered seam
        # at construction; patch the seam (not the env base) — a present PRO
        # RuntimeConfigManager pins a full-blob default over the env var.
        from baldur.settings.idempotency import IdempotencySettings

        with patch(
            "baldur.settings.layered_provider.get_layered_settings_cached",
            return_value=IdempotencySettings(fail_open_on_cache_error=True),
        ):
            guard = IdempotencyGuard(key_generator=self._failing_key_fn, fail_open=None)
        result = guard.check(context=make_context())
        assert result.allowed is True

    def test_per_call_false_overrides_fail_open_setting(
        self, make_context, monkeypatch
    ):
        # Global posture = fail-open, but an explicit per-call False wins.
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_FAIL_OPEN_ON_CACHE_ERROR", "true")
        self._reset_for_env()
        guard = IdempotencyGuard(key_generator=self._failing_key_fn, fail_open=False)
        result = guard.check(context=make_context())
        assert result.allowed is False
        assert result.metadata.get("idempotency_unavailable") is True


# =============================================================================
# IdempotencyGuard — 673 G2 cache-outage routing via a real gate that RAISES
# =============================================================================


class _OutageCache:
    """Duck-typed cache that passes the gate's atomic validators but raises
    ``AdapterConnectionError`` on the acquire op — models a Redis outage after
    673 un-swallowed ``setnx`` / ``cas_takeover``. Pre-673 the gate would have
    returned ABORT (misreported as a duplicate); now it RAISES, so the guard's
    fail-open / ``idempotency_unavailable`` path is reachable."""

    def setnx(self, key, value, ttl=None):
        from baldur.core.exceptions import AdapterConnectionError

        raise AdapterConnectionError("redis down")

    def cas_dict_field(self, key, field, expected, new_value, ttl=None):
        return False

    def cas_takeover(self, key, new_record, *, stale_before, ttl=None):
        from baldur.core.exceptions import AdapterConnectionError

        raise AdapterConnectionError("redis down")

    def get(self, key):
        return None

    def set(self, key, value, ttl=None):
        return True

    def delete(self, key):
        return False

    def exists(self, key):
        return False


def _outage_gate():
    from baldur.core.idempotency_gate import IdempotencyGate

    return IdempotencyGate(cache=_OutageCache())


class TestIdempotencyGuardCacheOutageRoutingBehavior:
    """673 G2 wiring: a real gate over an outage cache RAISES from
    ``check_and_acquire``; the guard routes that raise to fail-open (allowed) or
    fail-closed (``idempotency_unavailable``), NOT to a duplicate/ABORT verdict.
    This proves the un-swallow → surface → fail-open chain end-to-end (the
    existing raising-key_fn tests only exercise the guard's ``except`` arm)."""

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_outage_fail_open_true_allows_through(
        self, mock_ensure_gate, key_fn, make_context
    ):
        mock_ensure_gate.return_value = _outage_gate()
        guard = IdempotencyGuard(key_generator=key_fn, fail_open=True)

        result = guard.check(context=make_context())

        assert result.allowed is True

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_outage_fail_open_false_fails_closed_as_unavailable_not_duplicate(
        self, mock_ensure_gate, key_fn, make_context
    ):
        mock_ensure_gate.return_value = _outage_gate()
        guard = IdempotencyGuard(key_generator=key_fn, fail_open=False)

        result = guard.check(context=make_context())

        assert result.allowed is False
        # An outage is UNAVAILABLE, not a duplicate — the misreport 673 G2 fixes.
        assert result.metadata.get("idempotency_unavailable") is True
        assert "idempotency_decision" not in result.metadata


class _AsyncOutageCache:
    """Async twin of ``_OutageCache`` — ``asetnx`` / ``acas_takeover`` raise."""

    async def asetnx(self, key, value, ttl=None):
        from baldur.core.exceptions import AdapterConnectionError

        raise AdapterConnectionError("redis down")

    async def acas_dict_field(self, key, field, expected, new_value, ttl=None):
        return False

    async def acas_takeover(self, key, new_record, *, stale_before, ttl=None):
        from baldur.core.exceptions import AdapterConnectionError

        raise AdapterConnectionError("redis down")

    async def aget(self, key):
        return None

    async def adelete(self, key):
        return False


def _async_outage_gate():
    from baldur.core.idempotency_gate import AsyncIdempotencyGate

    return AsyncIdempotencyGate(cache=_AsyncOutageCache())


class TestAsyncIdempotencyGuardCacheOutageRoutingBehavior:
    """673 G2 wiring (async parity): the async guard routes a gate raise to
    fail-open (allowed) / fail-closed (``idempotency_unavailable``)."""

    @pytest.mark.asyncio
    @patch("baldur.resilience.policies.idempotency._ensure_async_policy_gate")
    async def test_outage_fail_open_true_allows_through(
        self, mock_ensure_gate, key_fn, make_context
    ):
        from baldur.resilience.policies.idempotency import AsyncIdempotencyGuard

        mock_ensure_gate.return_value = _async_outage_gate()
        guard = AsyncIdempotencyGuard(key_generator=key_fn, fail_open=True)

        result = await guard.check(context=make_context())

        assert result.allowed is True

    @pytest.mark.asyncio
    @patch("baldur.resilience.policies.idempotency._ensure_async_policy_gate")
    async def test_outage_fail_open_false_fails_closed_as_unavailable(
        self, mock_ensure_gate, key_fn, make_context
    ):
        from baldur.resilience.policies.idempotency import AsyncIdempotencyGuard

        mock_ensure_gate.return_value = _async_outage_gate()
        guard = AsyncIdempotencyGuard(key_generator=key_fn, fail_open=False)

        result = await guard.check(context=make_context())

        assert result.allowed is False
        assert result.metadata.get("idempotency_unavailable") is True
        assert "idempotency_decision" not in result.metadata


# =============================================================================
# IdempotencyGuard — block-path + fail-open WARN logging (#567 D5/D6, §8.4)
# =============================================================================


class TestIdempotencyGuardLogBehavior:
    """#567 D5/D6: the guard logs at WARNING on the legitimate block path (D6)
    and on the cache-error fail path (D5) — a facade-blocked duplicate and a
    silent cache degradation are both observable."""

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    @pytest.mark.parametrize(
        ("decision_name", "event_name"),
        [
            ("SKIP", "idempotency.duplicate_blocked"),
            ("ABORT", "idempotency.execution_blocked"),
        ],
        ids=["skip", "abort"],
    )
    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_block_path_emits_blocked_warning_with_decision(
        self, mock_ensure_gate, decision_name, event_name, key_fn, make_context
    ):
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        mock_gate = MagicMock()
        mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
            decision=IdempotencyDecision[decision_name]
        )
        mock_ensure_gate.return_value = mock_gate

        guard = IdempotencyGuard(key_generator=key_fn)
        with capture_logs() as cap_logs:
            result = guard.check(context=make_context())

        assert result.allowed is False
        events = [e for e in cap_logs if e["event"] == event_name]
        assert len(events) == 1
        assert events[0]["decision"] == decision_name
        assert events[0]["key"] == "test_key_test_domain"

    def test_cache_error_fail_open_emits_guard_check_failed_warning(self, make_context):
        def failing(ctx):
            raise RuntimeError("redis down")

        guard = IdempotencyGuard(key_generator=failing)
        with capture_logs() as cap_logs:
            result = guard.check(context=make_context())

        assert result.allowed is False
        events = [e for e in cap_logs if e["event"] == "idempotency.guard_check_failed"]
        assert len(events) == 1
        # Fail-closed by default → the logged fail_open posture is False.
        assert events[0]["fail_open"] is False


# =============================================================================
# IdempotencyGuard ↔ @idempotent decorator block-event parity (#567 D8)
# =============================================================================


class TestIdempotencyEventNameParityContract:
    """#567 D8: the policy guard emits the SAME block-event-name literals as the
    ``@idempotent`` decorator. The two surfaces never run for the same call, so
    sharing the literal is the cross-surface parity (one log query catches a
    block on either surface), not a collision."""

    BLOCK_EVENTS = {
        "idempotency.duplicate_blocked",
        "idempotency.execution_blocked",
    }

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    def test_guard_block_events_match_decorator_block_events(
        self, key_fn, make_context, caplog
    ):
        import logging

        from baldur.core.exceptions import IdempotencyDuplicateError
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )
        from baldur.decorators.idempotent import _reset_fallback_cache, idempotent

        # --- Guard side: capture both block events via a patched gate. ---
        guard_events: set[str] = set()
        for decision in (IdempotencyDecision.SKIP, IdempotencyDecision.ABORT):
            mock_gate = MagicMock()
            mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
                decision=decision
            )
            with patch(
                "baldur.resilience.policies.idempotency._ensure_policy_gate",
                return_value=mock_gate,
            ):
                guard = IdempotencyGuard(key_generator=key_fn)
                with capture_logs() as cap_logs:
                    guard.check(context=make_context())
            guard_events.update(
                e["event"]
                for e in cap_logs
                if str(e["event"]).startswith("idempotency.")
            )

        # --- Decorator side: SKIP (real fallback) + ABORT (patched gate). ---
        _reset_fallback_cache()

        @idempotent(key_args=["order_id"])
        def op(order_id: str) -> str:
            return "ok"

        op("parity-oid")  # first call CONTINUEs + marks completed
        with caplog.at_level(logging.WARNING, logger="baldur.decorators.idempotent"):
            with pytest.raises(IdempotencyDuplicateError):
                op("parity-oid")  # SKIP → duplicate_blocked
            with patch(
                "baldur.core.idempotency_gate.IdempotencyGate.check_and_acquire",
                return_value=IdempotencyCheckResult(decision=IdempotencyDecision.ABORT),
            ):
                with pytest.raises(IdempotencyDuplicateError):
                    op("parity-oid-2")  # ABORT → execution_blocked
        decorator_events = {
            r.message
            for r in caplog.records
            if str(r.message).startswith("idempotency.")
        }

        # The guard emits exactly the documented block literals, and the
        # decorator emits the same ones — a rename on either side breaks this.
        assert guard_events == self.BLOCK_EVENTS
        assert guard_events == (guard_events & decorator_events)


# =============================================================================
# IdempotencyGuard — 595 D4 window threading (§8.5 Dependency Interaction)
# =============================================================================


class TestIdempotencyGuardTtlThreadingBehavior:
    """595 D4: the guard is the single window source — ``execution_ttl``
    threads to ``check_and_acquire(ttl=)``; on CONTINUE the memory ``ttl`` is
    stored in ``context.extra["_idempotency_ttl"]`` for the hook's ``mark_*``;
    a fail-open cache error stores neither key, so the hook no-ops and both
    windows go unused."""

    _MEM_TTL = timedelta(hours=2)
    _EXEC_TTL = timedelta(minutes=5)

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_execution_ttl_reaches_check_and_acquire(
        self, mock_ensure_gate, key_fn, make_context
    ):
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        mock_gate = MagicMock()
        mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
            decision=IdempotencyDecision.CONTINUE
        )
        mock_ensure_gate.return_value = mock_gate

        guard = IdempotencyGuard(
            key_generator=key_fn, ttl=self._MEM_TTL, execution_ttl=self._EXEC_TTL
        )
        guard.check(context=make_context())

        mock_gate.check_and_acquire.assert_called_once_with(
            "test_key_test_domain", ttl=self._EXEC_TTL
        )

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_continue_stores_memory_ttl_in_context_extra(
        self, mock_ensure_gate, key_fn, make_context
    ):
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        mock_gate = MagicMock()
        mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
            decision=IdempotencyDecision.CONTINUE
        )
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context()
        guard = IdempotencyGuard(
            key_generator=key_fn, ttl=self._MEM_TTL, execution_ttl=self._EXEC_TTL
        )
        result = guard.check(context=ctx)

        assert result.allowed is True
        assert ctx.extra["_idempotency_ttl"] is self._MEM_TTL

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_none_windows_defer_to_gate_defaults(
        self, mock_ensure_gate, key_fn, make_context
    ):
        """No guard windows → ttl=None to the gate on both phases."""
        from baldur.core.idempotency_gate import (
            IdempotencyCheckResult,
            IdempotencyDecision,
        )

        mock_gate = MagicMock()
        mock_gate.check_and_acquire.return_value = IdempotencyCheckResult(
            decision=IdempotencyDecision.CONTINUE
        )
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context()
        guard = IdempotencyGuard(key_generator=key_fn)
        guard.check(context=ctx)

        mock_gate.check_and_acquire.assert_called_once_with(
            "test_key_test_domain", ttl=None
        )
        assert ctx.extra["_idempotency_ttl"] is None

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_fail_open_cache_error_stores_neither_key_nor_ttl(
        self, mock_ensure_gate, make_context
    ):
        """595 Testability Notes fail-open × TTL case: the guard CONTINUEs
        without storing ``_idempotency_key``/``_idempotency_ttl``, so the
        hook no-ops and both TTL kwargs are unused; no exception escapes."""

        def failing_key_fn(ctx):
            raise RuntimeError("cache I/O error during check")

        mock_gate = MagicMock()
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context()
        guard = IdempotencyGuard(
            key_generator=failing_key_fn,
            fail_open=True,
            ttl=self._MEM_TTL,
            execution_ttl=self._EXEC_TTL,
        )
        result = guard.check(context=ctx)

        assert result.allowed is True
        assert "_idempotency_key" not in ctx.extra
        assert "_idempotency_ttl" not in ctx.extra

        # The hook consequently no-ops — the gate is never marked.
        hook = IdempotencyHook()
        hook.on_success("composer", PolicyResult(value=1), context=ctx)
        mock_gate.mark_completed.assert_not_called()


# =============================================================================
# IdempotencyHook — Behavior (§8.5 Dependency Interaction)
# =============================================================================


class TestIdempotencyHookBehavior:
    """IdempotencyHook on_success/on_failure behavior verification."""

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_on_success_marks_completed_when_key_present(
        self, mock_ensure_gate, make_context
    ):
        """on_success() calls mark_completed() when the key is present in context."""
        mock_gate = MagicMock()
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context(
            extra={"_idempotency_key": "my_key", "_idempotency_retry_count": 2}
        )
        hook = IdempotencyHook()
        hook.on_success(
            "composer",
            PolicyResult(value=42),
            context=ctx,
        )

        # ttl=None → gate memory default (the guard threaded no per-call ttl).
        mock_gate.mark_completed.assert_called_once_with(
            "my_key", retry_count=2, ttl=None, claim_id=None
        )

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_on_failure_marks_failed_when_key_present(
        self, mock_ensure_gate, make_context
    ):
        """on_failure() calls mark_failed() when the key is present in context."""
        mock_gate = MagicMock()
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context(
            extra={"_idempotency_key": "my_key", "_idempotency_retry_count": 3}
        )
        error = ValueError("test error")
        hook = IdempotencyHook()
        hook.on_failure("composer", error, 1, context=ctx)

        # ttl=None → gate memory default (the guard threaded no per-call ttl).
        mock_gate.mark_failed.assert_called_once_with(
            "my_key", error="test error", retry_count=3, ttl=None, claim_id=None
        )

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_on_success_forwards_threaded_memory_ttl(
        self, mock_ensure_gate, make_context
    ):
        """595 D4: the guard-threaded ``_idempotency_ttl`` reaches mark_completed."""
        mock_gate = MagicMock()
        mock_ensure_gate.return_value = mock_gate
        mem_ttl = timedelta(hours=2)

        ctx = make_context(
            extra={
                "_idempotency_key": "my_key",
                "_idempotency_retry_count": 1,
                "_idempotency_ttl": mem_ttl,
            }
        )
        hook = IdempotencyHook()
        hook.on_success("composer", PolicyResult(value=42), context=ctx)

        mock_gate.mark_completed.assert_called_once_with(
            "my_key", retry_count=1, ttl=mem_ttl, claim_id=None
        )

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_on_failure_forwards_threaded_memory_ttl(
        self, mock_ensure_gate, make_context
    ):
        """595 D4: the guard-threaded ``_idempotency_ttl`` reaches mark_failed."""
        mock_gate = MagicMock()
        mock_ensure_gate.return_value = mock_gate
        mem_ttl = timedelta(hours=2)

        ctx = make_context(
            extra={
                "_idempotency_key": "my_key",
                "_idempotency_retry_count": 0,
                "_idempotency_ttl": mem_ttl,
            }
        )
        hook = IdempotencyHook()
        hook.on_failure("composer", ValueError("boom"), 1, context=ctx)

        mock_gate.mark_failed.assert_called_once_with(
            "my_key", error="boom", retry_count=0, ttl=mem_ttl, claim_id=None
        )

    def test_on_success_noop_when_context_none(self):
        """on_success() is a no-op when context=None."""
        hook = IdempotencyHook()
        # Should not raise
        hook.on_success("composer", PolicyResult(value=42), context=None)

    def test_on_success_noop_when_no_key_in_extra(self, make_context):
        """on_success() is a no-op when no key is in context.extra."""
        ctx = make_context(extra={})
        hook = IdempotencyHook()
        # Should not raise
        hook.on_success("composer", PolicyResult(value=42), context=ctx)

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_on_success_fail_open_on_gate_error(self, mock_ensure_gate, make_context):
        """on_success() does not propagate gate failures (fail-open)."""
        mock_ensure_gate.side_effect = Exception("Redis down")
        ctx = make_context(extra={"_idempotency_key": "k"})
        hook = IdempotencyHook()
        # Should not raise
        hook.on_success("composer", PolicyResult(value=42), context=ctx)

    def test_on_execute_is_noop(self, make_context):
        """on_execute() is a no-op."""
        hook = IdempotencyHook()
        hook.on_execute("composer", 1, context=make_context())

    def test_on_retry_is_noop(self, make_context):
        """on_retry() is a no-op."""
        hook = IdempotencyHook()
        hook.on_retry("composer", 1, 0.5, context=make_context())

    def test_on_reject_is_noop(self, make_context):
        """on_reject() is a no-op."""
        hook = IdempotencyHook()
        hook.on_reject("composer", "reason", context=make_context())


# =============================================================================
# IdempotencyHook — fail-open WARN logging (#567 D5, §8.4 Side Effects)
# =============================================================================


class TestIdempotencyHookLogBehavior:
    """#567 D5: the hook's fail-open marks log at WARNING before swallowing —
    the call already succeeded/failed so a mark fault must never raise, but the
    silent degradation must be observable."""

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_on_success_mark_failure_warns_and_fails_open(
        self, mock_ensure_gate, make_context
    ):
        mock_gate = MagicMock()
        mock_gate.mark_completed.side_effect = RuntimeError("redis down")
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context(
            extra={"_idempotency_key": "k", "_idempotency_retry_count": 0}
        )
        hook = IdempotencyHook()
        with capture_logs() as cap_logs:
            # Must not raise — the protected call already succeeded.
            hook.on_success("composer", PolicyResult(value=1), context=ctx)

        events = [
            e for e in cap_logs if e["event"] == "idempotency.mark_completed_failed"
        ]
        assert len(events) == 1
        assert events[0]["fail_open"] is True
        assert events[0]["key"] == "k"

    @patch("baldur.resilience.policies.idempotency._ensure_policy_gate")
    def test_on_failure_mark_failure_warns_and_fails_open(
        self, mock_ensure_gate, make_context
    ):
        mock_gate = MagicMock()
        mock_gate.mark_failed.side_effect = RuntimeError("redis down")
        mock_ensure_gate.return_value = mock_gate

        ctx = make_context(
            extra={"_idempotency_key": "k", "_idempotency_retry_count": 0}
        )
        hook = IdempotencyHook()
        with capture_logs() as cap_logs:
            # Must not raise — the original error has already propagated.
            hook.on_failure("composer", ValueError("boom"), 1, context=ctx)

        events = [e for e in cap_logs if e["event"] == "idempotency.mark_failed_failed"]
        assert len(events) == 1
        assert events[0]["fail_open"] is True
        assert events[0]["key"] == "k"


# =============================================================================
# IdempotencyHook / AsyncIdempotencyHook — 799 D1 fallback-answer mark rule
# (§8.8 State transition, §8.12 Branch outcome, §8.5 Dependency interaction)
# =============================================================================


def _fallback_answer_result(outcome, trigger):
    """A pipeline result as the composer hands it to the hook's success path.

    ``trigger=None`` leaves ``fallback_trigger`` out of the metadata — the
    composer never produces that for a fallback answer, but the hook must still
    treat it as "the function did not complete".
    """
    metadata = {}
    if outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK:
        metadata = {"fallback_used": True, "original_error": "charge declined"}
        if trigger is not None:
            metadata["fallback_trigger"] = trigger.value
    return PolicyResult(value="answer", outcome=outcome, metadata=metadata)


# Rows: (outcome, fallback_trigger, expected record status after on_success).
_FALLBACK_ANSWER_ROWS = [
    (PolicyOutcome.SUCCESS, None, "completed"),
    (PolicyOutcome.SUCCESS_WITH_FALLBACK, PolicyOutcome.FAILURE, "failed"),
    (PolicyOutcome.SUCCESS_WITH_FALLBACK, PolicyOutcome.REJECTED, "failed"),
    (PolicyOutcome.SUCCESS_WITH_FALLBACK, PolicyOutcome.TIMEOUT, "failed"),
    (PolicyOutcome.SUCCESS_WITH_FALLBACK, None, "failed"),
]
_FALLBACK_ANSWER_IDS = [
    "plain_success_completes",
    "failure_answer_releases",
    "rejected_answer_releases",
    "timeout_answer_without_own_work_releases",
    "no_trigger_answer_releases",
]


class TestIdempotencyHookFallbackAnswerBehavior:
    """799 D1 / 805 D10: both hooks mark the key by one rule. Only the
    function's own return marks it completed at once; a fallback answer marks
    it by the work the call abandoned — with no running or own work recorded
    (the timed-out work never started, or nothing was abandoned) it is failed,
    so the next call on the key re-claims it. A timeout whose own work still
    runs is covered by the facade-level timeout-key tests.

    Runs over the real in-process gate so the effect is the stored record and
    the next acquire, not a mocked call."""

    _KEY = "fallback-answer-key"

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    @pytest.mark.parametrize(
        ("outcome", "trigger", "expected_status"),
        _FALLBACK_ANSWER_ROWS,
        ids=_FALLBACK_ANSWER_IDS,
    )
    def test_on_success_fallback_answer_marks_key_by_trigger(
        self, outcome, trigger, expected_status, make_context
    ):
        from baldur.core.idempotency_gate import IdempotencyDecision

        # Given — this call holds the key (record ``executing``).
        gate = _ensure_policy_gate()
        assert (
            gate.check_and_acquire(self._KEY).decision == IdempotencyDecision.CONTINUE
        )
        ctx = make_context(extra={"_idempotency_key": self._KEY})

        # When
        IdempotencyHook().on_success(
            "composer", _fallback_answer_result(outcome, trigger), context=ctx
        )

        # Then — the record, and what the next call on the key gets.
        assert gate._cache.get(self._KEY)["status"] == expected_status
        repeat = gate.check_and_acquire(self._KEY)
        if expected_status == "failed":
            assert repeat.decision == IdempotencyDecision.CONTINUE
            assert repeat.retry_count == 1
        else:
            assert repeat.decision == IdempotencyDecision.SKIP

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("outcome", "trigger", "expected_status"),
        _FALLBACK_ANSWER_ROWS,
        ids=_FALLBACK_ANSWER_IDS,
    )
    async def test_async_on_success_fallback_answer_marks_key_by_trigger(
        self, outcome, trigger, expected_status, make_context
    ):
        from baldur.core.idempotency_gate import IdempotencyDecision

        # Given — this call holds the key on the async gate.
        gate = _ensure_async_policy_gate()
        acquired = await gate.check_and_acquire(self._KEY)
        assert acquired.decision == IdempotencyDecision.CONTINUE
        ctx = make_context(extra={"_idempotency_key": self._KEY})

        # When
        await AsyncIdempotencyHook().on_success(
            "composer", _fallback_answer_result(outcome, trigger), context=ctx
        )

        # Then
        record = await gate._cache.aget(self._KEY)
        assert record["status"] == expected_status
        repeat = await gate.check_and_acquire(self._KEY)
        if expected_status == "failed":
            assert repeat.decision == IdempotencyDecision.CONTINUE
            assert repeat.retry_count == 1
        else:
            assert repeat.decision == IdempotencyDecision.SKIP

    def test_fallback_answer_release_forwards_error_retry_count_and_ttl(
        self, make_context
    ):
        """The release marks with the answered error and the guard-threaded
        retry count and memory window — the same arguments ``on_failure``
        forwards — and never marks the key completed."""
        from baldur.core.idempotency_gate import IdempotencyGate

        gate = MagicMock(spec=IdempotencyGate)
        mem_ttl = timedelta(hours=2)
        ctx = make_context(
            extra={
                "_idempotency_key": "my_key",
                "_idempotency_retry_count": 2,
                "_idempotency_ttl": mem_ttl,
            }
        )
        result = _fallback_answer_result(
            PolicyOutcome.SUCCESS_WITH_FALLBACK, PolicyOutcome.FAILURE
        )

        with patch(
            "baldur.resilience.policies.idempotency._ensure_policy_gate",
            return_value=gate,
        ):
            IdempotencyHook().on_success("composer", result, context=ctx)

        gate.mark_failed.assert_called_once_with(
            "my_key",
            error="charge declined",
            retry_count=2,
            ttl=mem_ttl,
            claim_id=None,
        )
        gate.mark_completed.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_fallback_answer_release_forwards_error_retry_count_and_ttl(
        self, make_context
    ):
        """Async twin of the forwarded-arguments check."""
        from baldur.core.idempotency_gate import AsyncIdempotencyGate

        gate = MagicMock(spec=AsyncIdempotencyGate)
        mem_ttl = timedelta(hours=2)
        ctx = make_context(
            extra={
                "_idempotency_key": "my_key",
                "_idempotency_retry_count": 2,
                "_idempotency_ttl": mem_ttl,
            }
        )
        result = _fallback_answer_result(
            PolicyOutcome.SUCCESS_WITH_FALLBACK, PolicyOutcome.REJECTED
        )

        with patch(
            "baldur.resilience.policies.idempotency._ensure_async_policy_gate",
            return_value=gate,
        ):
            await AsyncIdempotencyHook().on_success("composer", result, context=ctx)

        gate.mark_failed.assert_awaited_once_with(
            "my_key",
            error="charge declined",
            retry_count=2,
            ttl=mem_ttl,
            claim_id=None,
        )
        gate.mark_completed.assert_not_called()


class TestIdempotencyHookFallbackAnswerLogBehavior:
    """799 D1: a mark fault on the release branch is swallowed (the fallback's
    answer is already served) and logged under the op that was attempted —
    ``idempotency.mark_failed_failed``, never ``mark_completed_failed``."""

    @staticmethod
    def _release_result():
        return _fallback_answer_result(
            PolicyOutcome.SUCCESS_WITH_FALLBACK, PolicyOutcome.FAILURE
        )

    def test_fallback_answer_mark_fault_logs_mark_failed_failed(self, make_context):
        from baldur.core.idempotency_gate import IdempotencyGate

        gate = MagicMock(spec=IdempotencyGate)
        gate.mark_failed.side_effect = RuntimeError("redis down")
        ctx = make_context(extra={"_idempotency_key": "k"})

        with (
            patch(
                "baldur.resilience.policies.idempotency._ensure_policy_gate",
                return_value=gate,
            ),
            capture_logs() as cap_logs,
        ):
            # Must not raise — the fallback's answer has already been served.
            IdempotencyHook().on_success(
                "composer", self._release_result(), context=ctx
            )

        gate.mark_failed.assert_called_once()  # the fault fired
        events = [e["event"] for e in cap_logs]
        assert events.count("idempotency.mark_failed_failed") == 1
        assert "idempotency.mark_completed_failed" not in events

    @pytest.mark.asyncio
    async def test_async_fallback_answer_mark_fault_logs_mark_failed_failed(
        self, make_context
    ):
        from baldur.core.idempotency_gate import AsyncIdempotencyGate

        gate = MagicMock(spec=AsyncIdempotencyGate)
        gate.mark_failed.side_effect = RuntimeError("redis down")
        ctx = make_context(extra={"_idempotency_key": "k"})

        with (
            patch(
                "baldur.resilience.policies.idempotency._ensure_async_policy_gate",
                return_value=gate,
            ),
            capture_logs() as cap_logs,
        ):
            await AsyncIdempotencyHook().on_success(
                "composer", self._release_result(), context=ctx
            )

        gate.mark_failed.assert_awaited_once()  # the fault fired
        events = [e["event"] for e in cap_logs]
        assert events.count("idempotency.mark_failed_failed") == 1
        assert "idempotency.mark_completed_failed" not in events


# =============================================================================
# AntiFlapping — Singleton & Settings Wiring (§8.10)
# =============================================================================


class TestAntiFlappingSingletonBehavior:
    """AntiFlapping singleton and settings wiring behavior verification."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from baldur.services.idempotency.anti_flapping import (
            reset_anti_flapping_window,
        )

        reset_anti_flapping_window()
        yield
        reset_anti_flapping_window()

    def test_get_returns_same_instance(self):
        """get_anti_flapping_window() returns the same instance."""
        from baldur.services.idempotency.anti_flapping import (
            get_anti_flapping_window,
        )

        first = get_anti_flapping_window()
        second = get_anti_flapping_window()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """A new instance is created after reset."""
        from baldur.services.idempotency.anti_flapping import (
            get_anti_flapping_window,
            reset_anti_flapping_window,
        )

        first = get_anti_flapping_window()
        reset_anti_flapping_window()
        second = get_anti_flapping_window()
        assert first is not second

    def test_window_reads_settings(self):
        """The singleton reads values from AntiFlappingSettings."""
        from baldur.services.idempotency.anti_flapping import (
            get_anti_flapping_window,
        )
        from baldur.settings.anti_flapping import get_anti_flapping_settings

        settings = get_anti_flapping_settings()
        window = get_anti_flapping_window()

        assert window.window_seconds == settings.window_seconds
        assert window.similarity_threshold == settings.similarity_threshold
        assert window.max_similar_changes == settings.max_similar_changes


# =============================================================================
# Policy gate resolution (#564) — memoization, construction-time fail-closed,
# and real-cache dedup. These exercise the cache-backed gate that replaced the
# bare ``cache=None`` singleton, so they reset the memoized gate and force the
# in-process fallback cache for determinism.
# =============================================================================


@pytest.fixture
def policy_gate_isolation():
    """Reset the memoized policy gate + idempotency settings/runtime and force
    the in-process fallback cache (no registered adapter)."""
    from baldur.core.exceptions import AdapterNotFoundError
    from baldur.runtime import reset_runtime
    from baldur.settings.idempotency import reset_idempotency_settings

    reset_idempotency_settings()
    reset_runtime()
    _reset_policy_gate()
    with patch(
        "baldur.factory.registry.ProviderRegistry.get_cache",
        side_effect=AdapterNotFoundError(adapter_type="cache"),
    ):
        yield
    _reset_policy_gate()
    reset_idempotency_settings()
    reset_runtime()


class TestPolicyGateMemoizationBehavior:
    """``_ensure_policy_gate`` memoizes one cache-backed gate; ``_reset_policy_gate``
    rebuilds it and discards prior dedup state."""

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    def test_ensure_policy_gate_returns_same_instance_when_memoized(self):
        first = _ensure_policy_gate()
        second = _ensure_policy_gate()
        assert first is second

    def test_reset_policy_gate_builds_a_fresh_instance(self):
        first = _ensure_policy_gate()
        _reset_policy_gate()
        second = _ensure_policy_gate()
        assert first is not second

    def test_reset_policy_gate_clears_prior_dedup_state(self):
        from baldur.core.idempotency_gate import IdempotencyDecision

        # Given — a key acquired + completed against the first gate's cache.
        gate = _ensure_policy_gate()
        assert (
            gate.check_and_acquire("leak-key").decision == IdempotencyDecision.CONTINUE
        )
        gate.mark_completed("leak-key")
        assert gate.check_and_acquire("leak-key").decision == IdempotencyDecision.SKIP

        # When — reset replaces the fallback cache as well as the gate.
        _reset_policy_gate()

        # Then — the fresh gate has no record of the key (CONTINUE again).
        fresh = _ensure_policy_gate()
        assert (
            fresh.check_and_acquire("leak-key").decision == IdempotencyDecision.CONTINUE
        )


class TestGuardConstructionResolveContract:
    """D5: ``IdempotencyGuard.__init__`` resolves the cache-backed gate eagerly
    so a prod misconfiguration fails closed at construction — gated on
    ``enabled`` so a globally-disabled feature never raises."""

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    @staticmethod
    def _reset_for_env():
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        reset_runtime()
        _reset_policy_gate()

    def test_prod_no_adapter_no_escape_raises_at_construction(self, monkeypatch):
        from baldur.core.exceptions import ConfigurationError

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        self._reset_for_env()

        with pytest.raises(ConfigurationError):
            IdempotencyGuard(key_generator=lambda c: "k")

    def test_prod_no_adapter_escape_on_does_not_raise(self, monkeypatch):
        # 686: env drives ``is_production()``; the escape hatch resolves via the
        # cached layered seam at construction, patched here so a present PRO
        # RuntimeConfigManager cannot pin the full-blob default over it.
        from baldur.settings.idempotency import IdempotencySettings

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        self._reset_for_env()

        with patch(
            "baldur.settings.layered_provider.get_layered_settings_cached",
            return_value=IdempotencySettings(allow_inmemory_fallback=True),
        ):
            guard = IdempotencyGuard(key_generator=lambda c: "k")
        assert guard.name == "idempotency"

    def test_development_no_adapter_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        self._reset_for_env()

        guard = IdempotencyGuard(key_generator=lambda c: "k")
        assert guard.name == "idempotency"

    def test_disabled_skips_resolve_and_never_raises(self, monkeypatch):
        # enabled=False → __init__ skips _ensure_policy_gate even in the
        # prod + no-adapter + escape-off combination that otherwise raises.
        # 686: ``enabled`` resolves via the cached layered seam at construction;
        # patch the seam (not the env base) — a present PRO RuntimeConfigManager
        # pins a full-blob default over the env var.
        from baldur.settings.idempotency import IdempotencySettings

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        self._reset_for_env()

        with patch(
            "baldur.settings.layered_provider.get_layered_settings_cached",
            return_value=IdempotencySettings(enabled=False),
        ):
            guard = IdempotencyGuard(key_generator=lambda c: "k")
        assert guard._globally_enabled is False

    def test_prod_with_registered_adapter_does_not_raise(self, monkeypatch):
        from tests.factories.cache_doubles import DistributedCacheStandIn

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        self._reset_for_env()

        # Distributed adapter registered → resolution returns it, no raise.
        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            return_value=DistributedCacheStandIn(key_prefix="present:"),
        ):
            guard = IdempotencyGuard(key_generator=lambda c: "k")
        assert guard.name == "idempotency"


class TestGuardRealCacheDedupBehavior:
    """The guard + hook share one cache-backed gate, so the same key twice
    against the in-process fallback yields CONTINUE then SKIP (real dedup, not
    a mocked gate) — the change that fixed the ``cache=None`` singleton no-op."""

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    def test_guard_dedup_continue_then_skip_against_in_process_cache(
        self, make_context
    ):
        guard = IdempotencyGuard(key_generator=lambda c: "dedup-key")
        hook = IdempotencyHook()

        # Phase 1 — first acquire CONTINUEs and stores the key for the hook.
        ctx1 = make_context()
        first = guard.check(context=ctx1)
        assert first.allowed is True
        assert ctx1.extra["_idempotency_key"] == "dedup-key"

        # Phase 2 — mark the operation completed via the same shared gate.
        hook.on_success("composer", PolicyResult(value=1), context=ctx1)

        # Re-check with a fresh context → SKIP (already processed).
        second = guard.check(context=make_context())
        assert second.allowed is False
        assert "Already processed" in (second.reason or "")


# =============================================================================
# 805 D10 — the key follows the work the call abandoned
# =============================================================================

# Upper bound on any wait the test expects to end.
_WAIT_S = 5.0
_ABANDONED_KEY = "abandoned-work-key"
_CALLER_VAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hook_test_caller", default="unset"
)


def _eventually(predicate, timeout: float = _WAIT_S) -> bool:
    """Poll a mark another thread or loop writes."""
    poll = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        poll.wait(0.002)
    return bool(predicate())


def _claim(key: str = _ABANDONED_KEY) -> PolicyContext:
    """A keyed call that passed the guard: the record is in ``context.extra``
    and its work scope is current."""
    context = PolicyContext(domain="abandoned", extra={})
    assert IdempotencyGuard(key_generator=lambda c: key).check(context).allowed
    return context


def _finish(piece: Future, outcome: str) -> None:
    if outcome == "returned":
        piece.set_result("charged")
    else:
        piece.set_exception(ConnectionError("gateway reset"))


def _end_call(context: PolicyContext, ended_by: str) -> None:
    """End the keyed call the way the composer does for ``ended_by``."""
    hook = IdempotencyHook()
    if ended_by == "timeout_error":
        hook.on_failure("composer", TimeoutPolicyError(1.0), 1, context=context)
    elif ended_by == "timeout_fallback":
        hook.on_success(
            "composer",
            _fallback_answer_result(
                PolicyOutcome.SUCCESS_WITH_FALLBACK, PolicyOutcome.TIMEOUT
            ),
            context=context,
        )
    elif ended_by == "other_error":
        hook.on_failure("composer", ValueError("declined"), 1, context=context)
    else:
        hook.on_success(
            "composer",
            _fallback_answer_result(
                PolicyOutcome.SUCCESS_WITH_FALLBACK, PolicyOutcome.FAILURE
            ),
            context=context,
        )


def _status(key: str = _ABANDONED_KEY):
    record = _ensure_policy_gate()._cache.get(key)
    return None if record is None else record["status"]


class _RecordingGate:
    """Delegates marks to the real policy gate, recording where each ran."""

    def __init__(self, real, raise_on_mark: bool = False) -> None:
        self._real = real
        self._raise = raise_on_mark
        self.marks: list[dict] = []
        self.marked = threading.Event()

    def _record(self, kind: str, key: str, kwargs: dict) -> None:
        self.marks.append(
            {
                "kind": kind,
                "key": key,
                "claim_id": kwargs.get("claim_id"),
                "caller_var": _CALLER_VAR.get(),
                "thread": threading.current_thread().name,
            }
        )
        self.marked.set()
        if self._raise:
            raise ConnectionError("ledger unreachable")

    def mark_completed(self, key, **kwargs):
        self._record("completed", key, kwargs)
        self._real.mark_completed(key, **kwargs)

    def mark_failed(self, key, **kwargs):
        self._record("failed", key, kwargs)
        self._real.mark_failed(key, **kwargs)


# (how the call ended, how its own running piece ended) -> status once it ends
_DEFERRED_ROWS = [
    ("timeout_error", "returned", "completed"),
    ("timeout_error", "raised", "failed"),
    ("timeout_fallback", "returned", "completed"),
    ("timeout_fallback", "raised", "failed"),
    ("other_error", "returned", "completed"),
    ("failure_fallback", "returned", "completed"),
]
_DEFERRED_IDS = [
    "timeout_own_returned_completes",
    "timeout_own_raised_releases",
    "timeout_answer_own_returned_completes",
    "timeout_answer_own_raised_releases",
    "raise_own_returned_completes",
    "failure_answer_own_returned_completes",
]


class TestIdempotencyHookAbandonedWorkBehavior:
    """805 D10 / 810 D2 decision table over the real in-process gate: a call
    that did not return keeps its claim ``executing`` while recorded work runs,
    then marks completed only when its own cut-off work returned, whatever
    ended the call."""

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    @pytest.mark.parametrize(
        ("ended_by", "outcome", "expected"), _DEFERRED_ROWS, ids=_DEFERRED_IDS
    )
    def test_running_own_piece_holds_key_then_marks_by_rule(
        self, ended_by, outcome, expected
    ):
        # Given — a keyed call whose own timed-out work is still running.
        context = _claim()
        piece = Future()
        record_abandoned(piece, origin=context)

        # When — the call ends; a repeat arrives; then the work ends.
        _end_call(context, ended_by)
        status_while_running = _status()
        repeat = _ensure_policy_gate().check_and_acquire(_ABANDONED_KEY)
        _finish(piece, outcome)

        # Then
        assert status_while_running == "executing"
        assert repeat.decision == IdempotencyDecision.ABORT
        assert _status() == expected

    @pytest.mark.parametrize(
        ("ended_by", "outcome", "expected"),
        [
            ("timeout_error", "returned", "completed"),
            ("timeout_error", "raised", "failed"),
            ("timeout_error", None, "failed"),
            ("other_error", "returned", "completed"),
        ],
        ids=[
            "timeout_own_done_returned",
            "timeout_own_done_raised",
            "timeout_nothing_recorded",
            "raise_own_done_returned",
        ],
    )
    def test_nothing_running_marks_at_once(self, ended_by, outcome, expected):
        """Own work that ended before the close, or none at all, marks now."""
        context = _claim()
        if outcome is not None:
            piece = Future()
            record_abandoned(piece, origin=context)
            _finish(piece, outcome)

        _end_call(context, ended_by)

        assert _status() == expected

    def test_return_marks_completed_while_other_work_runs(self):
        context = _claim()
        piece = Future()
        record_abandoned(piece, origin=None)

        IdempotencyHook().on_success("composer", PolicyResult(value=1), context=context)

        assert _status() == "completed"
        piece.set_result("done")
        assert _status() == "completed"

    def test_late_mark_runs_on_finishing_thread_in_copy_of_caller_context(self):
        # Given — the caller set a context variable before the call ended.
        real = _ensure_policy_gate()
        recording = _RecordingGate(real)
        context = _claim()
        piece = Future()
        record_abandoned(piece, origin=context)
        caller_token = _CALLER_VAR.set("caller")
        try:
            with patch(
                "baldur.resilience.policies.idempotency._ensure_policy_gate",
                return_value=recording,
            ):
                _end_call(context, "timeout_error")
                _CALLER_VAR.set("changed-after-close")

                # When — another thread ends the work.
                finisher = threading.Thread(
                    target=piece.set_result, args=("charged",), name="finisher"
                )
                finisher.start()
                finisher.join(_WAIT_S)
        finally:
            _CALLER_VAR.reset(caller_token)

        # Then — one mark, on that thread, seeing the caller's value at close.
        assert len(recording.marks) == 1
        mark = recording.marks[0]
        assert mark["kind"] == "completed"
        assert mark["thread"] == "finisher"
        assert mark["caller_var"] == "caller"
        assert _CALLER_VAR.get() == "unset"

    @pytest.mark.parametrize(
        "outcome", ["returned", "raised"], ids=["late_completed", "late_failed"]
    )
    def test_late_mark_is_claim_scoped_against_later_claim(self, outcome):
        """A takeover while the work ran keeps its claim through either late mark."""
        # Given — the call deferred; its record went stale and was taken over.
        context = _claim()
        piece = Future()
        record_abandoned(piece, origin=context)
        _end_call(context, "timeout_error")
        gate = _ensure_policy_gate()
        gate._cache.get(_ABANDONED_KEY)["started_at"] = 0
        later = gate.check_and_acquire(_ABANDONED_KEY)
        assert later.decision == IdempotencyDecision.CONTINUE

        # When — the abandoned work ends, so the late mark is completed or failed.
        _finish(piece, outcome)

        # Then
        record = gate._cache.get(_ABANDONED_KEY)
        assert record["status"] == "executing"
        assert record["claim_id"] == later.claim_id

    def test_late_mark_uses_record_read_at_close_not_reused_context(self):
        """A later keyed call on the same PolicyContext cannot redirect the mark."""
        # Given — call A deferred; call B then reused A's context object.
        context = _claim("key-a")
        piece = Future()
        record_abandoned(piece, origin=context)
        _end_call(context, "timeout_error")
        assert IdempotencyGuard(key_generator=lambda c: "key-b").check(context).allowed
        IdempotencyHook().on_success("composer", PolicyResult(value=1), context=context)

        # When — A's work ends and fails.
        piece.set_exception(ConnectionError("gateway reset"))

        # Then — A's key is released; B's key keeps B's outcome.
        assert _status("key-a") == "failed"
        assert _status("key-b") == "completed"

    def test_deferral_logs_mark_deferred_with_running_pieces(self):
        context = _claim()
        pieces = [Future(), Future()]
        record_abandoned(pieces[0], origin=context)
        record_abandoned(pieces[1], origin=None)

        with capture_logs() as logs:
            _end_call(context, "timeout_error")

        deferred = [log for log in logs if log["event"] == "idempotency.mark_deferred"]
        assert len(deferred) == 1
        assert deferred[0]["log_level"] == "info"
        assert deferred[0]["key"] == _ABANDONED_KEY
        assert deferred[0]["pieces"] == 2
        for piece in pieces:
            piece.set_result("done")

    def test_late_mark_failure_logs_warning_and_does_not_raise(self):
        recording = _RecordingGate(_ensure_policy_gate(), raise_on_mark=True)
        context = _claim()
        piece = Future()
        record_abandoned(piece, origin=context)

        with (
            patch(
                "baldur.resilience.policies.idempotency._ensure_policy_gate",
                return_value=recording,
            ),
            capture_logs() as logs,
        ):
            _end_call(context, "timeout_error")
            piece.set_result("charged")

        failed = [
            log for log in logs if log["event"] == "idempotency.deferred_mark_failed"
        ]
        assert recording.marked.is_set()
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"
        assert failed[0]["key"] == _ABANDONED_KEY
        assert failed[0]["error_type"] == "ConnectionError"


class TestAsyncIdempotencyHookAbandonedWorkBehavior:
    """805 D10 on the async hook: the late mark goes through the sync gate on
    the shared ledger, and is scheduled on the hook's loop on the in-process
    one."""

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    @staticmethod
    async def _claim_async(key: str = _ABANDONED_KEY) -> PolicyContext:
        context = PolicyContext(domain="abandoned", extra={})
        guard = AsyncIdempotencyGuard(key_generator=lambda c: key)
        assert (await guard.check(context)).allowed
        return context

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [("returned", "completed"), ("raised", "failed")],
        ids=["own_returned", "own_raised"],
    )
    async def test_in_process_ledger_late_mark_runs_on_hook_loop(
        self, outcome, expected
    ):
        # Given — an async keyed call whose own sync timed work still runs.
        gate = _ensure_async_policy_gate()
        context = await self._claim_async()
        piece = Future()
        record_abandoned(piece, origin=context)

        # When — the call ends on the timeout, then another thread ends the work.
        await AsyncIdempotencyHook().on_failure(
            "composer", TimeoutPolicyError(1.0), 1, context=context
        )
        held = (await gate._cache.aget(_ABANDONED_KEY))["status"]
        finisher = threading.Thread(target=_finish, args=(piece, outcome))
        finisher.start()
        finisher.join(_WAIT_S)

        # Then — the mark lands once the loop runs the scheduled task.
        for _ in range(500):
            if (await gate._cache.aget(_ABANDONED_KEY))["status"] != "executing":
                break
            await asyncio.sleep(0.002)
        assert held == "executing"
        assert (await gate._cache.aget(_ABANDONED_KEY))["status"] == expected

    @pytest.mark.asyncio
    async def test_shared_ledger_late_mark_goes_through_sync_gate(self):
        """With a Redis async ledger the late mark goes through the sync gate."""
        from baldur.adapters.cache.async_redis_adapter import AsyncRedisCacheAdapter
        from baldur.core.idempotency_gate import AsyncIdempotencyGate

        # Given — the claim was taken; when the call ends the async ledger is
        # the shared Redis one (its client is never reached: the mark goes to
        # the sync gate).
        context = await self._claim_async()
        claim_id = context.extra["_idempotency_claim_id"]
        piece = Future()
        record_abandoned(piece, origin=context)
        sync_gate = MagicMock(spec=IdempotencyGate)
        redis_async_gate = AsyncIdempotencyGate(
            cache=AsyncRedisCacheAdapter(client=object(), key_prefix="")
        )

        with (
            patch(
                "baldur.resilience.policies.idempotency._ensure_async_policy_gate",
                return_value=redis_async_gate,
            ),
            patch(
                "baldur.resilience.policies.idempotency._ensure_policy_gate",
                return_value=sync_gate,
            ),
        ):
            await AsyncIdempotencyHook().on_failure(
                "composer", TimeoutPolicyError(1.0), 1, context=context
            )
            # When — the work ends on another thread.
            finisher = threading.Thread(target=piece.set_result, args=("charged",))
            finisher.start()
            finisher.join(_WAIT_S)

        # Then — marked on the finishing thread through the sync gate, claim-scoped.
        sync_gate.mark_completed.assert_called_once_with(
            _ABANDONED_KEY, retry_count=0, ttl=None, claim_id=claim_id
        )
        sync_gate.mark_failed.assert_not_called()

    def test_in_process_ledger_late_mark_on_closed_loop_logs_warning(self):
        """The hook's loop is gone when the work ends: logged, claim left to the window."""
        # Given — the call ended on a loop that is then closed.
        loop = asyncio.new_event_loop()
        piece = Future()

        async def _call_ends() -> None:
            context = await self._claim_async()
            record_abandoned(piece, origin=context)
            await AsyncIdempotencyHook().on_failure(
                "composer", TimeoutPolicyError(1.0), 1, context=context
            )

        loop.run_until_complete(_call_ends())
        loop.close()

        # When
        with capture_logs() as logs:
            piece.set_result("charged")

        # Then
        failed = [
            log for log in logs if log["event"] == "idempotency.deferred_mark_failed"
        ]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"
        assert failed[0]["error_type"] == "RuntimeError"


# =============================================================================
# The facade's raise-exit settles a scope left open, never one the hook closed
# =============================================================================


class TestCallEndedFromOutsideScopeBehavior:
    """A call that skipped its hook closes its scope; a hook-closed one is kept."""

    @pytest.fixture(autouse=True)
    def _iso(self, policy_gate_isolation):
        return

    def test_scope_left_open_is_closed_and_leaves_the_context(self):
        # Given — the guard opened the call's scope and no hook ran.
        context = PolicyContext(order_id="o-1")
        before = current_work_scope()
        _write_keyed_call(context, "k-open", 0, None, "claim-1")
        opened = current_work_scope()

        # When
        _settle_call_ended_from_outside(context, KeyboardInterrupt())

        # Then
        assert opened is not before
        assert opened.closed
        assert current_work_scope() is before

    def test_scope_the_hook_closed_keeps_its_late_settle(self):
        """Closing it again would drop the late mark the hook handed over."""
        # Given — the hook closed the scope while a piece of its own work runs.
        context = PolicyContext(order_id="o-1")
        _write_keyed_call(context, "k-held", 0, None, "claim-1")
        piece: Future = Future()
        record_abandoned(piece, origin=context)
        call = _read_keyed_call(context)
        settled: list[WorkSummary] = []
        assert close_work_scope(call.scope, call.token, settled.append) is None

        # When — the raise-exit runs, then the piece ends.
        _settle_call_ended_from_outside(context, KeyboardInterrupt())
        piece.set_result("done")

        # Then — the late settle ran once, with the piece's outcome.
        assert settled == [WorkSummary(own_finished=True, own_failed=False)]

    def test_context_without_a_keyed_call_is_untouched(self):
        before = current_work_scope()

        _settle_call_ended_from_outside(
            PolicyContext(order_id="o-1"), KeyboardInterrupt()
        )
        _settle_call_ended_from_outside(None, KeyboardInterrupt())

        assert current_work_scope() is before
