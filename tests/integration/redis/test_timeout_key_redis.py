"""An async keyed call's key follows its abandoned sync work past the loop's end.

``aprotect(idempotency_key=...)`` inside ``asyncio.run``, on the shared (Redis)
ledger: the coroutine's nested sync ``protect(timeout=...)`` times out while
its work keeps running — called directly in the coroutine, or inside
``asyncio.to_thread``. ``asyncio.run`` returns and closes its loop; the claim
stays ``executing`` while the work runs, and when the work ends the late mark
goes through the sync policy gate over the same Redis keys, releasing the key.

Composition under test: the async guard's claim (async Redis adapter), the work
scope the timeout worker records into, and the late mark the finishing worker
writes through the sync gate (sync Redis adapter) — observable only against a
real Redis, since the in-process async ledger marks on its own loop.

Test Categories:
    A. Async key held past the loop's close (Redis ledger):
        - nested sync timed work called directly in the coroutine
        - nested sync timed work run inside ``asyncio.to_thread``
    B. A keyed call cancelled, or whose wait was cut, on the shared ledger:
        - ``aprotect`` cancelled while Redis answers its claim (the server holds
          writes under ``CLIENT PAUSE WRITE``): the granted claim is released
        - a sync wait cut short by a soft time limit or a gevent timeout holds
          the key for every worker until the charge returns, then completes it

Requires a running Redis instance (auto-skipped via ``requires_redis``).
"""

from __future__ import annotations

import asyncio
import functools
import threading
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.core.exceptions import TimeoutPolicyError
from baldur.core.idempotency_gate import IdempotencyDecision, IdempotencyGate
from baldur.interfaces.resilience_policy import PolicyContext
from baldur.protect_facade import aprotect, protect, reset_protect_caches
from baldur.resilience.policies.idempotency import (
    _ensure_async_policy_gate,
    _ensure_policy_gate,
)
from tests.factories.interruptions import (
    GeventTimeout,
    SoftTimeLimitExceeded,
    interrupted_timeout_wait,
)

pytestmark = pytest.mark.requires_redis

# A sync timeout that fires while the work is already running: an idle
# shared-executor worker picks it up long before.
_SYNC_TIMEOUT_FIRES_S = 1.0
_HOLD_S = 5.0
# Longest the server holds writes if the test cannot unpause it (auto-expiry).
_PAUSE_MS = 5000
_BARE: dict[str, Any] = {"circuit_breaker": False, "retry": False, "dlq": False}


def _reset() -> None:
    from baldur.runtime import reset_runtime
    from baldur.settings.idempotency import reset_idempotency_settings
    from baldur.settings.protect import reset_protect_settings

    reset_protect_settings()
    reset_idempotency_settings()
    reset_runtime()
    reset_protect_caches()


@pytest.fixture
def redis_ledger(redis_url, monkeypatch):
    """The registry's cache is Redis, so both policy gates share its keys."""
    monkeypatch.setenv("BALDUR_REDIS_URL", redis_url)
    _reset()
    adapter = RedisCacheAdapter(url=redis_url)
    with patch(
        "baldur.factory.registry.ProviderRegistry.get_cache", return_value=adapter
    ):
        yield adapter
    _reset()


class _LateMarkSignal:
    """Observes the marks the sync policy gate makes."""

    def __init__(self) -> None:
        self.gate = _ensure_policy_gate()
        self.marked = threading.Event()
        for method in ("mark_completed", "mark_failed"):
            real = getattr(self.gate, method)

            def _signalling(key: str, _real: Any = real, **kwargs: Any) -> None:
                try:
                    _real(key, **kwargs)
                finally:
                    self.marked.set()

            setattr(self.gate, method, _signalling)


class TestAprotectTimeoutKeyRedisIntegration:
    """SC5 (Redis ledger): held after the loop closed, released when the work ends."""

    @pytest.mark.parametrize(
        "hop", ["direct", "to_thread"], ids=["direct_call", "asyncio_to_thread"]
    )
    def test_async_key_held_past_loop_close_then_released_by_sync_gate(
        self, redis_ledger, hop
    ):
        """
        Purpose:
            Verify that an async keyed call's claim on the shared Redis ledger
            stays held after ``asyncio.run`` closed its loop while nested sync
            timed work still runs, and that the work's end releases it through
            the sync policy gate.
        Expected:
            - while the work runs, the record is ``executing`` with a claim id
              and a same-key repeat reads ``ABORT``
            - once the work ends, the late mark writes ``failed`` (the call
              raised) and an immediate retry reads ``CONTINUE``
        """
        # Given — a nested sync timed call whose work keeps running.
        signal = _LateMarkSignal()
        order_id = uuid4().hex
        key = f"svc.redis-outer:{order_id}"
        entered, release = threading.Event(), threading.Event()

        def work() -> str:
            entered.set()
            release.wait(_HOLD_S)
            return "inner"

        async def charge() -> str:
            timed = functools.partial(
                protect, "svc.redis-inner", work, timeout=_SYNC_TIMEOUT_FIRES_S, **_BARE
            )
            if hop == "direct":
                timed()
            else:
                await asyncio.to_thread(timed)
            return "charged"

        async def call() -> Any:
            return await aprotect(
                "svc.redis-outer",
                charge,
                idempotency_key="order_id",
                context=PolicyContext(order_id=order_id),
                **_BARE,
            )

        try:
            # When — asyncio.run ends (its loop closed) with the work running.
            with pytest.raises(TimeoutPolicyError):
                asyncio.run(call())
            assert entered.is_set()
            held = redis_ledger.get(key)
            repeat_while_held = IdempotencyGate(cache=redis_ledger).check_and_acquire(
                key
            )
        finally:
            release.set()

        # Then — held while it ran, then released through the sync gate.
        assert held["status"] == "executing"
        assert held["claim_id"]
        assert repeat_while_held.decision == IdempotencyDecision.ABORT
        assert signal.marked.wait(_HOLD_S)
        assert redis_ledger.get(key)["status"] == "failed"
        retry = IdempotencyGate(cache=redis_ledger).check_and_acquire(key)
        assert retry.decision == IdempotencyDecision.CONTINUE


# =============================================================================
# B. A keyed call cancelled, or whose wait was cut, on the shared ledger
# =============================================================================


def _status_becomes(ledger: RedisCacheAdapter, key: str, expected: str) -> bool:
    """Poll the shared record until a mark another thread or task writes lands."""
    poll = threading.Event()
    for _ in range(int(_HOLD_S / 0.01)):
        record = ledger.get(key)
        if record is not None and record["status"] == expected:
            return True
        poll.wait(0.01)
    return False


class TestKeyedCallCutShortRedisIntegration:
    """810 D1-D3 on the shared Redis ledger."""

    def test_cancel_while_redis_answers_claim_releases_it_once_answered(
        self, redis_ledger, redis_url
    ):
        """
        Purpose:
            Verify that an ``aprotect`` call cancelled while Redis is still
            answering its claim (the server holds writes under
            ``CLIENT PAUSE WRITE``) propagates the cancel at once, and that the
            claim the server grants afterwards is released (claim-scoped
            ``failed``) because the call never ran.
        Expected:
            - the cancel reaches the caller while the claim is unanswered
            - no record exists before the server answers
            - once it answers, the record ends ``failed`` and a retry runs the
              charge (the cancelled call never did)
        """
        import redis as redis_lib

        admin = redis_lib.from_url(redis_url)
        order_id = uuid4().hex
        key = f"svc.redis-claim:{order_id}"
        ran = {"n": 0}

        async def charge() -> str:
            ran["n"] += 1
            return "charged"

        def call() -> Any:
            return aprotect(
                "svc.redis-claim",
                charge,
                idempotency_key="order_id",
                context=PolicyContext(order_id=order_id),
                **_BARE,
            )

        async def scenario() -> tuple[dict | None, bool, Any]:
            gate = _ensure_async_policy_gate()
            real_claim = gate.check_and_acquire
            claiming = asyncio.Event()

            async def _claim(claim_key: str, ttl: Any = None) -> Any:
                claiming.set()
                return await real_claim(claim_key, ttl=ttl)

            admin.execute_command("CLIENT", "PAUSE", str(_PAUSE_MS), "WRITE")
            try:
                with patch.object(gate, "check_and_acquire", _claim):
                    task = asyncio.ensure_future(call())
                    await asyncio.wait_for(claiming.wait(), _HOLD_S)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, _HOLD_S)
                    before_answer = redis_ledger.get(key)
            finally:
                admin.execute_command("CLIENT", "UNPAUSE")
            for _ in range(int(_HOLD_S / 0.01)):
                record = redis_ledger.get(key)
                if record is not None and record["status"] == "failed":
                    break
                await asyncio.sleep(0.01)
            released = (redis_ledger.get(key) or {}).get("status") == "failed"
            return before_answer, released, await call()

        try:
            before_answer, released, retried = asyncio.run(scenario())
        finally:
            admin.close()

        assert before_answer is None
        assert released
        assert retried == "charged"
        assert ran["n"] == 1

    @pytest.mark.parametrize(
        "interruption",
        [SoftTimeLimitExceeded, GeventTimeout],
        ids=["soft_time_limit", "gevent_timeout"],
    )
    def test_interrupted_wait_holds_shared_key_until_own_work_returns(
        self, redis_ledger, interruption
    ):
        """
        Purpose:
            Verify that a sync keyed call whose wait on its running charge was
            cut short holds the shared key for every worker while the charge
            runs, and that the charge's return completes the key through the
            sync gate on the worker thread that finished it.
        Expected:
            - while the charge runs the record is ``executing`` and another
              worker's gate reads ``ABORT``
            - once it returns the record is ``completed`` and another worker's
              gate reads ``SKIP``
        """
        signal = _LateMarkSignal()
        order_id = uuid4().hex
        key = f"svc.redis-cut:{order_id}"
        entered, release = threading.Event(), threading.Event()

        def charge() -> str:
            entered.set()
            release.wait(_HOLD_S)
            return "charged"

        try:
            # When — the wait is cut while the charge runs.
            with interrupted_timeout_wait(interruption(), entered=entered):
                with pytest.raises(interruption):
                    protect(
                        "svc.redis-cut",
                        charge,
                        timeout=_HOLD_S,
                        idempotency_key="order_id",
                        context=PolicyContext(order_id=order_id),
                        **_BARE,
                    )
            held = redis_ledger.get(key)
            other_worker = IdempotencyGate(cache=redis_ledger).check_and_acquire(key)
        finally:
            release.set()

        # Then — held for every worker, then completed by its own work.
        assert held["status"] == "executing"
        assert other_worker.decision == IdempotencyDecision.ABORT
        assert signal.marked.wait(_HOLD_S)
        assert _status_becomes(redis_ledger, key, "completed")
        after = IdempotencyGate(cache=redis_ledger).check_and_acquire(key)
        assert after.decision == IdempotencyDecision.SKIP
