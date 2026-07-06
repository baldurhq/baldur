"""Unit tests for core/idempotency_gate.py — AsyncIdempotencyGate (672 D5).

Awaitable sibling of IdempotencyGate. Mirrors the sync gate's decision model
(CONTINUE / SKIP / ABORT + stale/failed retry) over the async cache surface,
plus the two async-frequent invariants the sync gate cannot be cancelled into:

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.8 State transition — CONTINUE/SKIP/ABORT, failed/stale retry, race window.
- §8.1 Boundary analysis — elapsed == execution ttl is NOT yet stale (strict >).
- §8.5 Dependency interaction — explicit ttl forwarded to ``asetnx`` / the
  memory ttl to ``acas_dict_field``; ``mark_*`` never reads via ``aget``.
- §8.9 Concurrency — a concurrent double-acquire on one key yields exactly one
  CONTINUE (the loser ABORTs) — strong consistency, no double-execute.
- Cancellation — a ``CancelledError`` (a ``BaseException``) awaited inside the
  gate propagates uncounted; a cancel-after-acquire-before-mark leaves the key
  acquired so the next same-key acquire ABORTs (TTL is the recovery path).
- §8.10 Fail-closed — a cache I/O error during acquire propagates out of the
  gate (the guard/facade layer decides fail-open vs fail-closed).
- Contract — atomic-override validation, default TTL constant, ctor window split.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from baldur.core.exceptions import ConfigurationError
from baldur.core.idempotency_gate import (
    IDEMPOTENCY_DEFAULT_TTL_SECONDS,
    AsyncIdempotencyGate,
    IdempotencyDecision,
)


class _FakeAsyncAtomicCache:
    """Async mirror of the sync tests' ``_FakeAtomicCache``.

    Defines its own ``asetnx`` / ``acas_dict_field`` (so it passes the gate's
    atomic-override validation by identity), with controllable ``asetnx`` return
    and full call tracking. No ``fakeredis`` needed.
    """

    def __init__(
        self, setnx_return: bool = True, cas_takeover_return: bool = True
    ) -> None:
        self._store: dict[str, Any] = {}
        self._setnx_return = setnx_return
        self._cas_takeover_return = cas_takeover_return
        self._asetnx_calls: list[tuple] = []
        self._aget_calls: list[str] = []
        self._adelete_calls: list[str] = []
        self._acas_calls: list[tuple] = []
        self._acas_takeover_calls: list[tuple] = []

    async def asetnx(self, key: str, value: Any, ttl=None) -> bool:
        self._asetnx_calls.append((key, value, ttl))
        if key not in self._store:
            if self._setnx_return:
                self._store[key] = value
                return True
            return False
        return False

    async def aget(self, key: str) -> Any:
        self._aget_calls.append(key)
        return self._store.get(key)

    async def adelete(self, key: str) -> bool:
        self._adelete_calls.append(key)
        return self._store.pop(key, None) is not None

    async def acas_dict_field(
        self, key: str, field: str, expected: Any, new_value: dict[str, Any], ttl=None
    ) -> bool:
        self._acas_calls.append((key, field, expected, new_value, ttl))
        existing = self._store.get(key)
        if not isinstance(existing, dict):
            return False
        if existing.get(field) != expected:
            return False
        self._store[key] = new_value
        return True

    async def acas_takeover(
        self, key: str, new_record: dict[str, Any], *, stale_before: float, ttl=None
    ) -> bool:
        self._acas_takeover_calls.append((key, new_record, stale_before, ttl))
        if not self._cas_takeover_return:
            return False
        existing = self._store.get(key)
        if not isinstance(existing, dict):
            return False
        status = existing.get("status")
        takeable = status == "failed" or (
            status == "executing" and existing.get("started_at", 0) < stale_before
        )
        if not takeable:
            return False
        self._store[key] = new_record
        return True


def _make_async_cache(setnx_return: bool = True) -> _FakeAsyncAtomicCache:
    return _FakeAsyncAtomicCache(setnx_return=setnx_return)


# =============================================================================
# Contract — ctor window split + atomic-override validation (672 D5)
# =============================================================================


class TestAsyncIdempotencyGateContract:
    """Design contract values + atomic-override fail-closed validation."""

    def test_ctor_execution_default_is_module_constant(self):
        """``execution_ttl_seconds`` defaults to the shared module constant —
        the async gate reuses the sync gate's window, not a fork."""
        gate = AsyncIdempotencyGate()
        assert gate._execution_ttl_seconds == IDEMPOTENCY_DEFAULT_TTL_SECONDS

    def test_ctor_memory_default_is_none_sentinel(self):
        """``memory_ttl_seconds`` defaults to the ``None`` sentinel → per-use
        settings resolution at mark time."""
        gate = AsyncIdempotencyGate()
        assert gate._memory_ttl_seconds is None

    def test_non_atomic_asetnx_raises_configuration_error(self):
        """A subclass inheriting the base (raising) ``asetnx`` is rejected at
        construction — fail-closed before any request runs."""
        from baldur.interfaces.cache_provider import AsyncCacheProviderInterface

        class _NonAtomicSetnx(AsyncCacheProviderInterface):
            async def aget(self, key):
                return None

            async def adelete(self, key):
                return False

            # inherits the base (raising) asetnx AND acas_dict_field

        with pytest.raises(ConfigurationError, match="atomic asetnx"):
            AsyncIdempotencyGate(cache=_NonAtomicSetnx())

    def test_non_atomic_acas_dict_field_raises_configuration_error(self):
        """A subclass with atomic ``asetnx`` but the inherited base
        ``acas_dict_field`` is rejected (the mark path needs an atomic CAS)."""
        from baldur.interfaces.cache_provider import AsyncCacheProviderInterface

        class _AtomicSetnxOnly(AsyncCacheProviderInterface):
            async def aget(self, key):
                return None

            async def adelete(self, key):
                return False

            async def asetnx(self, key, value, ttl=None):
                return True

            # inherits the base (raising) acas_dict_field

        with pytest.raises(ConfigurationError, match="atomic acas_dict_field"):
            AsyncIdempotencyGate(cache=_AtomicSetnxOnly())

    def test_non_atomic_acas_takeover_raises_configuration_error(self):
        """673 D1 (async): a subclass with atomic ``asetnx`` + ``acas_dict_field``
        but the inherited base (raising) ``acas_takeover`` is rejected at
        construction — the takeover validator runs after the setnx/CAS checks."""
        from baldur.interfaces.cache_provider import AsyncCacheProviderInterface

        class _AtomicButNoTakeover(AsyncCacheProviderInterface):
            async def aget(self, key):
                return None

            async def adelete(self, key):
                return False

            async def asetnx(self, key, value, ttl=None):
                return True

            async def acas_dict_field(self, key, field, expected, new_value, ttl=None):
                return True

            # inherits the base (raising) acas_takeover

        with pytest.raises(ConfigurationError, match="atomic acas_takeover"):
            AsyncIdempotencyGate(cache=_AtomicButNoTakeover())

    def test_atomic_override_cache_constructs_without_raising(self):
        """A fully-atomic override (the hand fake) constructs fine and is stored
        verbatim on the gate."""
        cache = _make_async_cache()
        gate = AsyncIdempotencyGate(cache=cache)
        assert gate._cache is cache


# =============================================================================
# Behavior — cache=None no-op mode
# =============================================================================


class TestAsyncIdempotencyGateNoCacheBehavior:
    """When cache is None the async gate is a no-op (always CONTINUE)."""

    @pytest.mark.asyncio
    async def test_no_cache_always_returns_continue(self):
        gate = AsyncIdempotencyGate(cache=None)
        result = await gate.check_and_acquire("any-key")
        assert result.decision == IdempotencyDecision.CONTINUE

    @pytest.mark.asyncio
    async def test_no_cache_mark_completed_is_noop(self):
        gate = AsyncIdempotencyGate(cache=None)
        await gate.mark_completed("key", {"result": "ok"})  # must not raise

    @pytest.mark.asyncio
    async def test_no_cache_mark_failed_is_noop(self):
        gate = AsyncIdempotencyGate(cache=None)
        await gate.mark_failed("key", "error")  # must not raise

    @pytest.mark.asyncio
    async def test_no_cache_release_is_noop(self):
        gate = AsyncIdempotencyGate(cache=None)
        await gate.release("key")  # must not raise


# =============================================================================
# Behavior — check_and_acquire state transitions (parity with the sync gate)
# =============================================================================


class TestAsyncIdempotencyGateCheckAndAcquireBehavior:
    """State-transition parity with IdempotencyGate, awaited."""

    @pytest.mark.asyncio
    async def test_first_check_returns_continue_via_fast_path(self):
        """asetnx wins → CONTINUE with no aget (single-round-trip fast path)."""
        cache = _make_async_cache(setnx_return=True)
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-1")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert cache._aget_calls == []
        assert len(cache._asetnx_calls) == 1

    @pytest.mark.asyncio
    async def test_completed_status_returns_skip_with_cached_result(self):
        cache = _make_async_cache(setnx_return=False)
        cache._store["key-done"] = {
            "status": "completed",
            "result": {"output": "success"},
            "retry_count": 1,
        }
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-done")

        assert result.decision == IdempotencyDecision.SKIP
        assert result.cached_result == {"output": "success"}
        assert result.retry_count == 1

    @pytest.mark.asyncio
    async def test_executing_within_ttl_returns_abort_without_takeover(self):
        """A fresh in-TTL claim ABORTs and is left untouched (no adelete) so a
        double execution cannot slip through."""
        import time

        cache = _make_async_cache(setnx_return=True)
        cache._store["key-run"] = {
            "status": "executing",
            "started_at": time.time(),
            "retry_count": 0,
        }
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-run")

        assert result.decision == IdempotencyDecision.ABORT
        assert "key-run" not in cache._adelete_calls

    @pytest.mark.asyncio
    async def test_failed_status_returns_continue_via_cas_takeover(self):
        """failed → atomic acas_takeover wins → CONTINUE, retry_count incremented,
        the takeover claims the SAME key as a fresh executing record."""
        cache = _make_async_cache(setnx_return=True)
        cache._store["key-failed"] = {"status": "failed", "retry_count": 2}
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-failed")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert result.retry_count == 3
        # Single atomic takeover — no adelete+asetnx two-step.
        assert cache._adelete_calls == []
        assert len(cache._acas_takeover_calls) == 1
        call = cache._acas_takeover_calls[-1]
        assert call[0] == "key-failed"
        assert call[1]["status"] == "executing"
        assert call[1]["retry_count"] == 3

    @pytest.mark.asyncio
    async def test_failed_status_cas_takeover_race_returns_abort(self):
        """failed → another process wins the atomic takeover → ABORT."""
        cache = _FakeAsyncAtomicCache(cas_takeover_return=False)
        cache._store["key-failed"] = {"status": "failed", "retry_count": 1}
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-failed")

        assert result.decision == IdempotencyDecision.ABORT

    @pytest.mark.asyncio
    async def test_stale_executing_returns_continue_via_cas_takeover(self):
        """executing past TTL → atomic acas_takeover wins → CONTINUE, retry incremented."""
        import time

        cache = _make_async_cache(setnx_return=True)
        cache._store["key-stale"] = {
            "status": "executing",
            "started_at": time.time() - 7200,  # 2h ago, TTL 1800s
            "retry_count": 1,
        }
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-stale")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert result.retry_count == 2
        assert cache._adelete_calls == []
        assert len(cache._acas_takeover_calls) == 1
        assert cache._acas_takeover_calls[-1][0] == "key-stale"

    @pytest.mark.asyncio
    async def test_unknown_status_returns_abort(self):
        cache = _make_async_cache(setnx_return=False)
        cache._store["key-weird"] = {"status": "unknown_state"}
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-weird")
        assert result.decision == IdempotencyDecision.ABORT

    @pytest.mark.asyncio
    async def test_non_dict_existing_value_returns_abort(self):
        cache = _make_async_cache(setnx_return=False)
        cache._store["key-bad"] = "not-a-dict"
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-bad")
        assert result.decision == IdempotencyDecision.ABORT

    @pytest.mark.asyncio
    async def test_race_existing_expired_between_setnx_and_get_reacquires(self):
        """Initial asetnx loses (key held) but aget returns None (expired in the
        gap) → a single re-asetnx re-acquires → CONTINUE, not a spurious ABORT."""

        class _RaceCache(_FakeAsyncAtomicCache):
            async def asetnx(self, key, value, ttl=None):
                self._asetnx_calls.append((key, value, ttl))
                if len(self._asetnx_calls) == 1:
                    return False  # lose the initial race, store nothing
                self._store[key] = value
                return True

        cache = _RaceCache()
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-race")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert cache._aget_calls == ["key-race"]
        assert len(cache._asetnx_calls) == 2

    @pytest.mark.asyncio
    async def test_race_existing_none_retry_lost_aborts(self):
        """Initial asetnx loses, aget None, re-asetnx ALSO loses → ABORT."""
        cache = _make_async_cache(setnx_return=False)
        gate = AsyncIdempotencyGate(cache=cache)

        result = await gate.check_and_acquire("key-race-lost")

        assert result.decision == IdempotencyDecision.ABORT
        assert cache._aget_calls == ["key-race-lost"]
        assert len(cache._asetnx_calls) == 2

    @pytest.mark.asyncio
    async def test_stale_executing_at_exact_ttl_is_not_yet_stale(self):
        """Boundary: elapsed == ttl exactly is NOT stale (strict ``>``) → ABORT,
        no takeover. Pins a ``>`` → ``>=`` drift that would take over a tick early."""
        cache = _make_async_cache(setnx_return=True)
        cache._store["key-bnd"] = {
            "status": "executing",
            "started_at": 1000.0,
            "retry_count": 0,
        }
        gate = AsyncIdempotencyGate(cache=cache, execution_ttl_seconds=1800)

        with patch(
            "baldur.core.idempotency_gate.time.time", return_value=1000.0 + 1800
        ):
            result = await gate.check_and_acquire("key-bnd")

        assert result.decision == IdempotencyDecision.ABORT
        assert "key-bnd" not in cache._adelete_calls

    @pytest.mark.asyncio
    async def test_explicit_ttl_is_forwarded_to_acquire_setnx(self):
        """A per-call execution ttl must reach the acquiring asetnx verbatim."""
        cache = _make_async_cache(setnx_return=True)
        gate = AsyncIdempotencyGate(cache=cache)
        custom = timedelta(seconds=42)

        await gate.check_and_acquire("key-customttl", ttl=custom)

        assert cache._asetnx_calls[0][2] == custom


# =============================================================================
# Behavior — mark_completed / mark_failed transitions
# =============================================================================


class TestAsyncIdempotencyGateMarkBehavior:
    """awaited mark_completed / mark_failed transition behavior."""

    @pytest.mark.asyncio
    async def test_mark_completed_sets_completed_status(self):
        cache = _make_async_cache()
        cache._store["key-1"] = {"status": "executing", "retry_count": 1}
        gate = AsyncIdempotencyGate(cache=cache)

        await gate.mark_completed("key-1", result={"data": "ok"}, retry_count=1)

        saved = cache._store["key-1"]
        assert saved["status"] == "completed"
        assert saved["result"] == {"data": "ok"}
        assert saved["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_mark_failed_sets_failed_status(self):
        cache = _make_async_cache()
        cache._store["key-1"] = {"status": "executing", "retry_count": 0}
        gate = AsyncIdempotencyGate(cache=cache)

        await gate.mark_failed("key-1", error="step crashed")

        saved = cache._store["key-1"]
        assert saved["status"] == "failed"
        assert saved["error"] == "step crashed"

    @pytest.mark.asyncio
    async def test_mark_completed_does_not_read_via_get(self):
        """mark_completed transitions via acas_dict_field, never aget."""
        cache = _make_async_cache()
        cache._store["key-1"] = {"status": "executing", "retry_count": 0}
        gate = AsyncIdempotencyGate(cache=cache)

        await gate.mark_completed("key-1", result={"data": "ok"})

        assert "key-1" not in cache._aget_calls
        assert any(call[0] == "key-1" for call in cache._acas_calls)

    @pytest.mark.asyncio
    async def test_mark_completed_cas_conflict_skips_write(self):
        """Non-executing status → CAS conflict → the record is not overwritten."""
        cache = _make_async_cache()
        cache._store["key-1"] = {"status": "completed", "retry_count": 0}
        gate = AsyncIdempotencyGate(cache=cache)

        await gate.mark_completed("key-1", result={"data": "ok"})

        saved = cache._store["key-1"]
        assert saved["status"] == "completed"
        assert saved.get("result") != {"data": "ok"}


class TestAsyncIdempotencyGateMarkTtlBehavior:
    """The memory ttl forwards to acas_dict_field: explicit verbatim, ``None`` →
    the settings-driven memory default per use (async parity with the sync gate)."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        yield
        reset_idempotency_settings()

    @staticmethod
    def _gate_with_executing_record(key: str):
        cache = _make_async_cache()
        cache._store[key] = {"status": "executing", "retry_count": 0}
        return AsyncIdempotencyGate(cache=cache), cache

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mark", ["mark_completed", "mark_failed"], ids=["completed", "failed"]
    )
    async def test_explicit_ttl_forwarded_to_acas_dict_field(self, mark):
        gate, cache = self._gate_with_executing_record("key-ttl")
        explicit = timedelta(hours=2)

        await getattr(gate, mark)("key-ttl", ttl=explicit)

        assert cache._acas_calls[0][4] is explicit

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mark", ["mark_completed", "mark_failed"], ids=["completed", "failed"]
    )
    async def test_none_ttl_resolves_to_settings_memory_default(self, mark):
        from baldur.settings.idempotency import get_idempotency_settings

        gate, cache = self._gate_with_executing_record("key-default")

        await getattr(gate, mark)("key-default")

        expected = timedelta(seconds=get_idempotency_settings().gate_memory_ttl_seconds)
        assert cache._acas_calls[0][4] == expected


# =============================================================================
# Behavior — release re-arms a future acquisition
# =============================================================================


class TestAsyncIdempotencyGateReleaseBehavior:
    """``release`` deletes the record so the same logical key is re-acquirable."""

    @pytest.mark.asyncio
    async def test_release_makes_completed_key_reacquirable(self):
        cache = _make_async_cache(setnx_return=True)
        gate = AsyncIdempotencyGate(cache=cache)

        await gate.check_and_acquire("key-rearm")
        await gate.mark_completed("key-rearm", result={"ok": True})
        assert (
            await gate.check_and_acquire("key-rearm")
        ).decision == IdempotencyDecision.SKIP

        await gate.release("key-rearm")

        assert (
            await gate.check_and_acquire("key-rearm")
        ).decision == IdempotencyDecision.CONTINUE

    @pytest.mark.asyncio
    async def test_release_swallows_cache_error(self):
        """A cache adelete failure is swallowed (best-effort)."""

        class _RaisingDeleteCache(_FakeAsyncAtomicCache):
            async def adelete(self, key):
                raise RuntimeError("cache down")

        gate = AsyncIdempotencyGate(cache=_RaisingDeleteCache())
        await gate.release("key-boom")  # must not raise


# =============================================================================
# Behavior — strong consistency: concurrent acquire + cancellation
# =============================================================================


class TestAsyncIdempotencyGateStrongConsistencyBehavior:
    """The acquire is awaited inline (never fire-and-forget), so two concurrent
    requests cannot both proceed, and cancellation propagates without corrupting
    the acquired state."""

    @pytest.mark.asyncio
    async def test_concurrent_double_acquire_yields_exactly_one_continue(self):
        """asyncio.gather of two acquires on one key → exactly one CONTINUE, one
        ABORT (loop-atomic asetnx elects a single winner) — no double-execute."""
        from baldur.adapters.cache.async_memory_adapter import (
            AsyncInMemoryCacheAdapter,
        )

        cache = AsyncInMemoryCacheAdapter(key_prefix="dbl:")
        gate = AsyncIdempotencyGate(cache=cache)

        r1, r2 = await asyncio.gather(
            gate.check_and_acquire("dup-key"),
            gate.check_and_acquire("dup-key"),
        )

        decisions = sorted([r1.decision, r2.decision], key=lambda d: d.value)
        assert decisions == [IdempotencyDecision.ABORT, IdempotencyDecision.CONTINUE]

    @pytest.mark.asyncio
    async def test_cancellation_mid_acquire_propagates(self):
        """A ``CancelledError`` raised while awaiting the acquire escapes the gate
        uncounted (the gate wraps no ``except`` around the cache await)."""
        started = asyncio.Event()
        release = asyncio.Event()

        class _BlockingAsyncCache:
            async def asetnx(self, key, value, ttl=None):
                started.set()
                await release.wait()  # blocks until released; we cancel instead
                return True

            async def acas_dict_field(self, key, field, expected, new_value, ttl=None):
                return True

            async def acas_takeover(self, key, new_record, *, stale_before, ttl=None):
                return True

            async def aget(self, key):
                return None

            async def adelete(self, key):
                return False

        gate = AsyncIdempotencyGate(cache=_BlockingAsyncCache())
        task = asyncio.create_task(gate.check_and_acquire("cancel-key"))
        await started.wait()  # ensure we are suspended inside asetnx

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_cancel_after_acquire_before_mark_leaves_key_acquired(self):
        """The acquire↔mark crash/cancel window: an acquired-but-unmarked key
        keeps blocking duplicates (fail-closed-safe) so a cancelled operation
        cannot double-execute; TTL is the recovery path."""
        from baldur.adapters.cache.async_memory_adapter import (
            AsyncInMemoryCacheAdapter,
        )

        cache = AsyncInMemoryCacheAdapter(key_prefix="cx:")
        gate = AsyncIdempotencyGate(cache=cache, execution_ttl_seconds=1800)

        r1 = await gate.check_and_acquire("op-key")
        assert r1.decision == IdempotencyDecision.CONTINUE
        # Operation cancelled here — mark_completed / mark_failed never runs.

        r2 = await gate.check_and_acquire("op-key")
        assert r2.decision == IdempotencyDecision.ABORT


# =============================================================================
# Behavior — fail-closed: cache I/O error propagates out of the gate
# =============================================================================


class TestAsyncIdempotencyGateFailClosedBehavior:
    """A cache error during acquire propagates — the gate does not swallow it,
    so the guard/facade can fail closed by default (671 D1 + 672 D10 parity)."""

    @pytest.mark.asyncio
    async def test_cache_error_during_acquire_propagates(self):
        class _RaisingAsyncCache(_FakeAsyncAtomicCache):
            async def asetnx(self, key, value, ttl=None):
                raise RuntimeError("redis down")

        gate = AsyncIdempotencyGate(cache=_RaisingAsyncCache())

        with pytest.raises(RuntimeError, match="redis down"):
            await gate.check_and_acquire("key-boom")


# =============================================================================
# Behavior — decision metric recorded on the real-cache path (566 D9 parity)
# =============================================================================


class TestAsyncIdempotencyGateDecisionMetricBehavior:
    """The async gate records the decision through the same choke point as the
    sync gate (``IdempotencyGate._record_gate_decision``)."""

    @pytest.mark.asyncio
    async def test_real_cache_continue_records_continue_decision(self):
        cache = _make_async_cache(setnx_return=True)
        gate = AsyncIdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = await gate.check_and_acquire("key-1")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.return_value.idempotency.record_gate_decision.assert_called_once_with(
            "continue"
        )

    @pytest.mark.asyncio
    async def test_no_cache_noop_does_not_record(self):
        gate = AsyncIdempotencyGate(cache=None)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = await gate.check_and_acquire("any-key")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.assert_not_called()


# =============================================================================
# Behavior — clock-skew-conservative stale_before (673 D1/1a, sync parity)
# =============================================================================


class TestAsyncIdempotencyGateStaleBeforeToleranceBehavior:
    """673 1a (async parity): ``_stale_before`` subtracts
    ``clock_skew_tolerance_seconds`` so a clock-ahead peer cannot take over a
    still-running claim early; ``tolerance=0.0`` disables the margin."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        yield
        reset_idempotency_settings()

    _TOL_ENV = "BALDUR_IDEMPOTENCY_CLOCK_SKEW_TOLERANCE_SECONDS"
    _TIME = "baldur.core.idempotency_gate.time.time"

    def _executing_gate(self, started_at: float):
        cache = _make_async_cache(setnx_return=True)
        cache._store["k"] = {
            "status": "executing",
            "started_at": started_at,
            "retry_count": 0,
        }
        return AsyncIdempotencyGate(cache=cache, execution_ttl_seconds=1800), cache

    def test_stale_before_subtracts_ttl_and_tolerance(self):
        # 686 D3/D5: tolerance read through the cached layered seam.
        from baldur.settings.idempotency import IdempotencySettings

        gate = AsyncIdempotencyGate(cache=_make_async_cache())

        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(clock_skew_tolerance_seconds=7.5),
            ),
            patch(self._TIME, return_value=10_000.0),
        ):
            value = gate._stale_before(timedelta(seconds=1800))

        assert value == 10_000.0 - 1800 - 7.5

    @pytest.mark.asyncio
    async def test_claim_within_tolerance_margin_is_not_taken_over(self, monkeypatch):
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv(self._TOL_ENV, "5.0")
        reset_idempotency_settings()
        gate, cache = self._executing_gate(started_at=1000.0)

        with patch(self._TIME, return_value=1000.0 + 1802):
            result = await gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.ABORT
        assert cache._acas_takeover_calls == []

    @pytest.mark.asyncio
    async def test_claim_beyond_tolerance_margin_is_taken_over(self, monkeypatch):
        from baldur.settings.idempotency import reset_idempotency_settings

        monkeypatch.setenv(self._TOL_ENV, "5.0")
        reset_idempotency_settings()
        gate, cache = self._executing_gate(started_at=1000.0)

        with patch(self._TIME, return_value=1000.0 + 1806):
            result = await gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert len(cache._acas_takeover_calls) == 1

    @pytest.mark.asyncio
    async def test_tolerance_zero_disables_the_margin(self):
        # 686 D3/D5: tolerance read through the cached layered seam.
        from baldur.settings.idempotency import IdempotencySettings

        gate, cache = self._executing_gate(started_at=1000.0)

        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(clock_skew_tolerance_seconds=0.0),
            ),
            patch(self._TIME, return_value=1000.0 + 1801),
        ):
            result = await gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.CONTINUE
        assert len(cache._acas_takeover_calls) == 1


# =============================================================================
# Behavior — takeover metric (673 D9/3a, sync parity)
# =============================================================================


class TestAsyncIdempotencyGateTakeoverMetricBehavior:
    """673 3a (async parity): a won failed / stale takeover increments
    ``record_takeover`` with the correct reason via the shared choke point
    (``IdempotencyGate._record_takeover``); a fresh acquire does NOT."""

    @pytest.mark.asyncio
    async def test_failed_takeover_records_reason_failed(self):
        cache = _make_async_cache(setnx_return=True)
        cache._store["k"] = {"status": "failed", "retry_count": 0}
        gate = AsyncIdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = await gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.return_value.idempotency.record_takeover.assert_called_once_with(
            "failed"
        )

    @pytest.mark.asyncio
    async def test_stale_takeover_records_reason_stale(self):
        import time

        cache = _make_async_cache(setnx_return=True)
        cache._store["k"] = {
            "status": "executing",
            "started_at": time.time() - 7200,
            "retry_count": 0,
        }
        gate = AsyncIdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = await gate.check_and_acquire("k")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.return_value.idempotency.record_takeover.assert_called_once_with(
            "stale"
        )

    @pytest.mark.asyncio
    async def test_fresh_acquire_does_not_record_takeover(self):
        cache = _make_async_cache(setnx_return=True)
        gate = AsyncIdempotencyGate(cache=cache)

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            result = await gate.check_and_acquire("fresh")

        assert result.decision == IdempotencyDecision.CONTINUE
        mock_get.return_value.idempotency.record_takeover.assert_not_called()
