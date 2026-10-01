"""Claim ids on idempotency records — a mark lands only on the claim it names.

Every record ``check_and_acquire`` writes carries a fresh ``claim_id`` (a first
acquire, the retry acquire after an expiry race, and both takeovers), returned
in the CONTINUE result. ``mark_completed`` / ``mark_failed`` given that id
compare it before writing — the record they write carries none, so a second
mark by the same claim finds nothing — and without one they compare
``status == "executing"`` as before. The sync and async gates share the rule.

Verification techniques applied:
- Contract: the claim id's shape, where it is written and returned.
- State transition: setnx / failed takeover / stale takeover each issue a new
  claim; a stale claim's late mark leaves the later claim untouched.
- Idempotency: a second mark by the same claim is a no-op.

Runs over the real in-process adapters (sync and async), so each effect is the
stored record and the next acquire's decision.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Generator
from datetime import timedelta
from typing import Any

import pytest

from baldur.adapters.cache.async_memory_adapter import AsyncInMemoryCacheAdapter
from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.core.idempotency_gate import (
    AsyncIdempotencyGate,
    IdempotencyCheckResult,
    IdempotencyDecision,
    IdempotencyGate,
)

_KEY = "order:claim"
_CLAIM_ID_SHAPE = re.compile(r"^[0-9a-f]{32}$")


class _ExpiryRaceCache(InMemoryCacheAdapter):
    """The record expires between the first setnx and the read that follows."""

    def __init__(self) -> None:
        super().__init__(key_prefix="race:")
        self._raced = False

    def setnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        if not self._raced:
            self._raced = True
            return False
        return super().setnx(key, value, ttl=ttl)


class _AsyncExpiryRaceCache(AsyncInMemoryCacheAdapter):
    """Async twin of :class:`_ExpiryRaceCache`."""

    def __init__(self) -> None:
        super().__init__(key_prefix="race:")
        self._raced = False

    async def asetnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        if not self._raced:
            self._raced = True
            return False
        return await super().asetnx(key, value, ttl=ttl)


class _GateDriver:
    """One synchronous surface over the sync gate or the async gate."""

    def __init__(self, kind: str, race: bool = False) -> None:
        self.kind = kind
        self._loop = asyncio.new_event_loop() if kind == "async" else None
        if kind == "sync":
            self.cache: Any = _ExpiryRaceCache() if race else InMemoryCacheAdapter()
            self.gate: Any = IdempotencyGate(cache=self.cache)
        else:
            self.cache = (
                _AsyncExpiryRaceCache() if race else AsyncInMemoryCacheAdapter()
            )
            self.gate = AsyncIdempotencyGate(cache=self.cache)

    def _run(self, value: Any) -> Any:
        if self._loop is None:
            return value
        return self._loop.run_until_complete(value)

    def acquire(self, key: str = _KEY) -> IdempotencyCheckResult:
        return self._run(self.gate.check_and_acquire(key))

    def mark_completed(self, key: str = _KEY, **kwargs: Any) -> None:
        self._run(self.gate.mark_completed(key, **kwargs))

    def mark_failed(self, key: str = _KEY, **kwargs: Any) -> None:
        self._run(self.gate.mark_failed(key, error="declined", **kwargs))

    def record(self, key: str = _KEY) -> dict[str, Any] | None:
        if self.kind == "sync":
            return self.cache.get(key)
        return self._run(self.cache.aget(key))

    def make_stale(self, key: str = _KEY) -> None:
        """Age the claim past every stale threshold (the adapters return the
        stored record itself)."""
        record = self.record(key)
        assert record is not None
        record["started_at"] = 0

    def close(self) -> None:
        if self._loop is not None:
            self._loop.close()


@pytest.fixture(params=["sync", "async"])
def driver(request) -> Generator[_GateDriver, None, None]:
    gate_driver = _GateDriver(request.param)
    yield gate_driver
    gate_driver.close()


class TestIdempotencyGateClaimContract:
    """Claim ids are issued on every claim and scope every mark that names one."""

    def test_first_acquire_returns_claim_id_written_on_record(self, driver):
        result = driver.acquire()

        assert result.decision == IdempotencyDecision.CONTINUE
        assert _CLAIM_ID_SHAPE.match(result.claim_id)
        assert driver.record()["claim_id"] == result.claim_id

    @pytest.mark.parametrize("kind", ["sync", "async"])
    def test_retry_acquire_after_expiry_race_returns_written_claim_id(self, kind):
        race_driver = _GateDriver(kind, race=True)
        try:
            result = race_driver.acquire()

            assert result.decision == IdempotencyDecision.CONTINUE
            assert _CLAIM_ID_SHAPE.match(result.claim_id)
            assert race_driver.record()["claim_id"] == result.claim_id
        finally:
            race_driver.close()

    def test_failed_takeover_issues_new_claim_id(self, driver):
        first = driver.acquire()
        driver.mark_failed(claim_id=first.claim_id)

        takeover = driver.acquire()

        assert takeover.decision == IdempotencyDecision.CONTINUE
        assert takeover.retry_count == 1
        assert takeover.claim_id != first.claim_id
        assert driver.record()["claim_id"] == takeover.claim_id

    def test_stale_takeover_issues_new_claim_id(self, driver):
        first = driver.acquire()
        driver.make_stale()

        takeover = driver.acquire()

        assert takeover.decision == IdempotencyDecision.CONTINUE
        assert takeover.claim_id != first.claim_id
        assert driver.record()["claim_id"] == takeover.claim_id

    @pytest.mark.parametrize("mark", ["completed", "failed"])
    def test_stale_claim_late_mark_leaves_later_claim_executing(self, driver, mark):
        """A late mark by a claim that was taken over writes nothing."""
        # Given — the first claim was taken over by a later one.
        first = driver.acquire()
        driver.make_stale()
        later = driver.acquire()

        # When — the first claim's work ends and marks.
        getattr(driver, f"mark_{mark}")(claim_id=first.claim_id)

        # Then — the later claim still holds the key.
        record = driver.record()
        assert record["status"] == "executing"
        assert record["claim_id"] == later.claim_id
        assert driver.acquire().decision == IdempotencyDecision.ABORT

    @pytest.mark.parametrize(
        ("mark", "expected_status"),
        [("completed", "completed"), ("failed", "failed")],
        ids=["completed", "failed"],
    )
    def test_mark_by_current_claim_lands_and_drops_claim_id(
        self, driver, mark, expected_status
    ):
        claim = driver.acquire()

        getattr(driver, f"mark_{mark}")(claim_id=claim.claim_id)

        record = driver.record()
        assert record["status"] == expected_status
        assert "claim_id" not in record

    def test_second_mark_by_same_claim_is_noop(self, driver):
        claim = driver.acquire()
        driver.mark_failed(claim_id=claim.claim_id)

        driver.mark_completed(claim_id=claim.claim_id)

        assert driver.record()["status"] == "failed"

    def test_mark_without_claim_id_compares_executing_status(self, driver):
        driver.acquire()

        driver.mark_completed()

        assert driver.record()["status"] == "completed"
        assert driver.acquire().decision == IdempotencyDecision.SKIP

    def test_mark_without_claim_id_skips_settled_record(self, driver):
        claim = driver.acquire()
        driver.mark_failed(claim_id=claim.claim_id)

        driver.mark_completed()

        assert driver.record()["status"] == "failed"

    @pytest.mark.parametrize(
        ("settle_first", "expected"),
        [(True, IdempotencyDecision.SKIP), (False, IdempotencyDecision.ABORT)],
        ids=["skip", "abort"],
    )
    def test_refusing_decision_carries_no_claim_id(
        self, driver, settle_first, expected
    ):
        claim = driver.acquire()
        if settle_first:
            driver.mark_completed(claim_id=claim.claim_id)

        repeat = driver.acquire()

        assert repeat.decision == expected
        assert repeat.claim_id is None

    def test_unconfigured_gate_continue_carries_no_claim_id(self):
        assert IdempotencyGate(cache=None).check_and_acquire(_KEY).claim_id is None
