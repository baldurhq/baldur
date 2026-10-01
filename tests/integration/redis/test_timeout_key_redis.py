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
from baldur.resilience.policies.idempotency import _ensure_policy_gate

pytestmark = pytest.mark.requires_redis

# A sync timeout that fires while the work is already running: an idle
# shared-executor worker picks it up long before.
_SYNC_TIMEOUT_FIRES_S = 1.0
_HOLD_S = 5.0
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

    @pytest.mark.parametrize("hop", ["direct", "to_thread"])
    def test_async_key_held_past_loop_close_then_released_by_sync_gate(
        self, redis_ledger, hop
    ):
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
