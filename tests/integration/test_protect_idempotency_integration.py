"""Mock-based integration tests for ``protect(idempotency_key=…)`` (#564).

Exercises the full idempotency bracket end-to-end through the public
``protect()`` / ``aprotect()`` facade against a real in-process, cache-backed
``IdempotencyGate``:

    IdempotencyGuard.check (Phase 1: check_and_acquire)
      → PolicyComposer chain (fn)
        → IdempotencyHook.on_success/on_failure (Phase 2: mark_completed/failed)

The guard, the hook, and the gate share one ``_POLICY_FALLBACK_CACHE`` resolved
via ``_ensure_policy_gate()``, so the acquire → mark state-transition lifecycle
spans a transaction boundary across two components plus the cache — a
composition a single-function unit test cannot drive end-to-end.

Infrastructure: in-process ``InMemoryCacheAdapter`` fallback (no Docker).
``ProviderRegistry.get_cache`` is patched to raise ``AdapterNotFoundError`` so
resolution lands on the fallback. The cross-worker (Redis) ``requires_redis``
variant is deferred — see #564 Test Assessment.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest

from baldur.core.exceptions import AdapterNotFoundError, IdempotencyDuplicateError
from baldur.interfaces.resilience_policy import PolicyContext, PolicyOutcome
from baldur.protect_facade import (
    aprotect_with_meta,
    protect,
    protect_with_meta,
    reset_protect_caches,
)


@pytest.fixture(autouse=True)
def _isolate_in_process_idempotency():
    """Reset protect/idempotency singletons and force the in-process fallback
    cache so each test starts from a clean dedup state."""
    from baldur.runtime import reset_runtime
    from baldur.settings.idempotency import reset_idempotency_settings
    from baldur.settings.protect import reset_protect_settings

    def _reset() -> None:
        reset_protect_settings()
        reset_idempotency_settings()
        reset_runtime()
        reset_protect_caches()

    _reset()
    with patch(
        "baldur.factory.registry.ProviderRegistry.get_cache",
        side_effect=AdapterNotFoundError(adapter_type="cache"),
    ):
        yield
    _reset()


class TestProtectIdempotencyLifecycle:
    """Acquire → mark lifecycle across guard + hook + shared gate + composer."""

    def test_success_then_duplicate_is_blocked(self):
        # Given — a side-effecting fn protected with an idempotency key.
        calls = {"n": 0}

        def charge():
            calls["n"] += 1
            return "charged"

        # When — the operation runs once and is completed (Phase 2 mark).
        first = protect_with_meta(
            "payment.charge",
            charge,
            idempotency_key="order_id",
            context=PolicyContext(order_id="ord-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        # Then — a duplicate carrying the same key is dedup-blocked, not re-run.
        second = protect_with_meta(
            "payment.charge",
            charge,
            idempotency_key="order_id",
            context=PolicyContext(order_id="ord-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert first.success is True
        assert first.value == "charged"
        assert second.outcome == PolicyOutcome.REJECTED
        assert second.success is False
        assert calls["n"] == 1

    def test_distinct_keys_both_execute(self):
        calls = {"n": 0}

        def charge():
            calls["n"] += 1
            return calls["n"]

        for order_id in ("ord-a", "ord-b"):
            protect_with_meta(
                "payment.charge",
                charge,
                idempotency_key="order_id",
                context=PolicyContext(order_id=order_id),
                circuit_breaker=False,
                retry=False,
                dlq=False,
            )

        assert calls["n"] == 2

    def test_failure_marks_failed_and_allows_subsequent_retry(self):
        # A FAILED operation must NOT dedup-block a later attempt: the hook
        # marks it failed (not completed), so the next acquire CONTINUEs.
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return "recovered"

        first = protect_with_meta(
            "payment.flaky",
            flaky,
            idempotency_key="order_id",
            context=PolicyContext(order_id="ord-retry"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        second = protect_with_meta(
            "payment.flaky",
            flaky,
            idempotency_key="order_id",
            context=PolicyContext(order_id="ord-retry"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert first.success is False  # first attempt failed
        assert second.success is True  # retry allowed (not dedup-blocked)
        assert second.value == "recovered"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_async_success_then_duplicate_is_blocked(self):
        calls = {"n": 0}

        async def submit():
            calls["n"] += 1
            return "submitted"

        first = await aprotect_with_meta(
            "webhook.submit",
            submit,
            idempotency_key="order_id",
            context=PolicyContext(order_id="evt-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        second = await aprotect_with_meta(
            "webhook.submit",
            submit,
            idempotency_key="order_id",
            context=PolicyContext(order_id="evt-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert first.success is True
        assert second.outcome == PolicyOutcome.REJECTED
        assert calls["n"] == 1


class TestProtectIdempotencyConcurrency:
    """#567 D1/G1: N concurrent ``protect(idempotency_key=)`` calls on ONE key
    run the side effect exactly once — exactly one wins ``CONTINUE`` and the
    rest get ``ABORT`` (``IdempotencyDuplicateError``). Exercises the
    guard → ``IdempotencyGate.check_and_acquire`` (atomic setnx on one shared
    cache record) → composer reject → facade ``_finalize_value`` lifecycle under
    real thread contention — a state dependency a single mocked-gate unit cannot
    prove."""

    def test_concurrent_duplicates_run_side_effect_exactly_once(self):
        n_callers = 8
        side_effect_runs = {"n": 0}
        runs_lock = threading.Lock()
        # The winner blocks inside fn until released, so it holds the gate record
        # in ``executing`` while the losers attempt their acquire — making them
        # deterministically observe ABORT (in-flight), not a stale/completed key.
        # Deterministic synchronization over time.sleep (UNIT_GUIDELINES §6.5.6).
        release = threading.Event()

        def fn():
            with runs_lock:
                side_effect_runs["n"] += 1
            release.wait(timeout=5.0)
            return "charged"

        def task():
            try:
                value = protect(
                    "payment.concurrent",
                    fn,
                    idempotency_key="order_id",
                    context=PolicyContext(order_id="same-key"),
                    circuit_breaker=False,
                    retry=False,
                    dlq=False,
                )
                return ("ok", value)
            except IdempotencyDuplicateError as exc:
                return ("dup", exc.decision)

        outcomes: list[tuple[str, object]] = []
        try:
            with ThreadPoolExecutor(max_workers=n_callers) as executor:
                futures = [executor.submit(task) for _ in range(n_callers)]
                for future in as_completed(futures, timeout=15):
                    outcomes.append(future.result())
                    # Once every loser has rejected, release the blocked winner.
                    if sum(1 for o in outcomes if o[0] == "dup") >= n_callers - 1:
                        release.set()
        finally:
            release.set()  # never leave the winner blocked

        oks = [o for o in outcomes if o[0] == "ok"]
        dups = [o for o in outcomes if o[0] == "dup"]

        # Exactly one caller ran the side effect; the rest were dedup-blocked.
        assert len(oks) == 1
        assert oks[0][1] == "charged"
        assert len(dups) == n_callers - 1
        assert all(decision == "ABORT" for _, decision in dups)
        assert side_effect_runs["n"] == 1
