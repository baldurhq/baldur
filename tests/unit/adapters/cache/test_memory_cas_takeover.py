"""Unit tests for ``InMemoryCacheAdapter.cas_takeover`` (673 D1 / G1).

The atomic failed / stale-executing takeover primitive that replaced the gate's
non-atomic ``delete()+setnx()`` two-step. Replaces the whole record IFF the
existing value is a dict whose ``status == "failed"`` OR (``status ==
"executing"`` AND ``started_at < stale_before``). The lock-wrapped memory adapter
is loop/thread-atomic, so — unlike the pre-fix two-step — a fresh post-takeover
claim (``started_at`` not ``< stale_before``) is ineligible for a second taker.

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.8 State transition — the takeable-predicate matrix (failed / stale /
  fresh-executing / completed / non-dict / missing / unknown).
- §8.3 Idempotency / single-winner — a second takeover after a fresh claim loses.
- §8.5 Dependency interaction — the replacement record + ttl are written verbatim.
- §8.7 Time dependency — ttl expiry via the adapter's own ``ttl()`` readout.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter


@pytest.fixture
def cache() -> InMemoryCacheAdapter:
    return InMemoryCacheAdapter(key_prefix="cast:")


_FRESH = {"status": "executing", "started_at": 100.0, "retry_count": 0}


class TestMemoryCasTakeoverPredicateBehavior:
    """The takeable predicate: failed OR (executing AND started_at < stale_before)."""

    def test_failed_record_is_taken_over_regardless_of_stale_before(self, cache):
        """A ``failed`` record is always takeable — ``stale_before`` is irrelevant."""
        cache.set("k", {"status": "failed", "retry_count": 2})

        # stale_before far in the past — a stale check would fail, but failed wins.
        taken = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=0.0
        )

        assert taken is True
        assert cache.get("k") == {"status": "executing", "started_at": 1000.0}

    def test_stale_executing_record_is_taken_over(self, cache):
        """``executing`` with ``started_at < stale_before`` → takeable."""
        cache.set("k", dict(_FRESH))  # started_at == 100.0

        taken = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 500.0}, stale_before=200.0
        )

        assert taken is True
        assert cache.get("k")["started_at"] == 500.0

    def test_fresh_executing_record_is_not_taken_over(self, cache):
        """``executing`` with ``started_at >= stale_before`` → NOT takeable, no write.

        This is the single-winner crux: a claim younger than the staleness
        threshold must survive so a concurrent taker cannot double-execute it.
        """
        cache.set("k", dict(_FRESH))  # started_at == 100.0

        taken = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 999.0}, stale_before=50.0
        )

        assert taken is False
        assert cache.get("k") == _FRESH  # unchanged

    def test_started_at_equal_to_stale_before_is_not_taken_over(self, cache):
        """Boundary: ``started_at == stale_before`` is NOT stale (strict ``<``).

        Pins a ``<`` → ``<=`` drift that would take over a claim a tick too early.
        """
        cache.set("k", {"status": "executing", "started_at": 200.0})

        taken = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 999.0}, stale_before=200.0
        )

        assert taken is False
        assert cache.get("k")["started_at"] == 200.0

    def test_completed_record_is_not_taken_over(self, cache):
        """A ``completed`` record is never takeable (the op already finished)."""
        cache.set("k", {"status": "completed", "result": {"ok": True}})

        taken = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=1e12
        )

        assert taken is False
        assert cache.get("k")["status"] == "completed"

    def test_unknown_status_record_is_not_taken_over(self, cache):
        """A dict with an unrecognized status is not takeable (defensive)."""
        cache.set("k", {"status": "quiescent"})

        taken = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=1e12
        )

        assert taken is False

    def test_non_dict_record_is_not_taken_over(self, cache):
        """A non-dict stored value cannot be a dedup record → not takeable."""
        cache.set("k", "not-a-dict")

        taken = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=1e12
        )

        assert taken is False
        assert cache.get("k") == "not-a-dict"

    def test_missing_key_is_not_taken_over(self, cache):
        """A missing key returns False without writing (nothing to take over)."""
        taken = cache.cas_takeover(
            "absent", {"status": "executing", "started_at": 1000.0}, stale_before=1e12
        )

        assert taken is False
        assert cache.get("absent") is None

    def test_missing_started_at_defaults_to_zero_and_is_takeable(self, cache):
        """An ``executing`` record with no ``started_at`` defaults to 0, so any
        positive ``stale_before`` makes it takeable (never a stuck claim)."""
        cache.set("k", {"status": "executing"})  # no started_at

        taken = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 1.0}, stale_before=0.5
        )

        assert taken is True


class TestMemoryCasTakeoverSingleWinnerBehavior:
    """After one takeover the record is a fresh claim, so a second taker loses."""

    def test_second_takeover_after_fresh_claim_loses(self, cache):
        """Two sequential takeovers on a seeded failed record: the first wins and
        rewrites to a fresh ``executing`` claim; the second sees a fresh claim
        (``started_at`` not ``< stale_before``) and loses — single-winner."""
        cache.set("k", {"status": "failed", "retry_count": 0})

        first = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=500.0
        )
        second = cache.cas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=500.0
        )

        assert first is True
        assert second is False
        # The winner's claim is intact — the loser did not overwrite it.
        assert cache.get("k")["started_at"] == 1000.0


class TestMemoryCasTakeoverTtlBehavior:
    """The replacement record honors the supplied ttl."""

    def test_takeover_applies_ttl_to_the_new_record(self, cache):
        """A takeover with ttl bounds the fresh claim's execution window."""
        cache.set("k", {"status": "failed"})

        cache.cas_takeover(
            "k",
            {"status": "executing", "started_at": 1.0},
            stale_before=0.0,
            ttl=timedelta(seconds=30),
        )

        remaining = cache.ttl("k")
        assert remaining is not None
        assert 0 < remaining <= 30

    def test_takeover_without_ttl_leaves_record_non_expiring(self, cache):
        """No ttl → the replacement record has no expiration (ttl() → None)."""
        cache.set("k", {"status": "failed"})

        cache.cas_takeover(
            "k", {"status": "executing", "started_at": 1.0}, stale_before=0.0
        )

        assert cache.ttl("k") is None
