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
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.resilience_policy import PolicyResult
from baldur.resilience.policies.idempotency import (
    IdempotencyGuard,
    IdempotencyHook,
    _ensure_policy_gate,
    _reset_policy_gate,
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
            "my_key", retry_count=2, ttl=None
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
            "my_key", error="test error", retry_count=3, ttl=None
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
            "my_key", retry_count=1, ttl=mem_ttl
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
            "my_key", error="boom", retry_count=0, ttl=mem_ttl
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
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        self._reset_for_env()

        # Registered adapter present → resolution returns it, no raise.
        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            return_value=InMemoryCacheAdapter(key_prefix="present:"),
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
