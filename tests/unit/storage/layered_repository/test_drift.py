"""
Drift reconciliation tests.
"""

from datetime import UTC, datetime, timedelta

import pytest


class TestDriftReconcilerHalfOpenXorBehavior:
    """476 D7: full XOR resolution matrix.

    Pre-476 the priority rule (OPEN > HALF_OPEN > CLOSED) governed
    every drift. Post-476 the L2-Lua atomic OPEN→HALF_OPEN transition
    must survive Worker B's drift reconciliation on L2 recovery, so
    *exactly one side HALF_OPEN* drifts now resolve by timestamp
    instead of priority. This test class is the regression guard for
    the four (l1_state, l2_state) combinations × the two timestamp
    orderings, plus the no-timestamp fallback that prefers HALF_OPEN.
    """

    def setup_method(self):
        from baldur.adapters.memory.circuit_breaker import get_drift_reconciler

        self.reconciler = get_drift_reconciler()
        self.reconciler.clear_history()

    @pytest.mark.parametrize(
        ("l1_state", "l2_state", "l1_newer", "expected_state", "expected_result_attr"),
        [
            # XOR matrix: HALF_OPEN newer (own side wins).
            ("half_open", "open", True, "half_open", "TIMESTAMP_HALF_OPEN_L1"),
            ("half_open", "closed", True, "half_open", "TIMESTAMP_HALF_OPEN_L1"),
            ("open", "half_open", False, "half_open", "TIMESTAMP_HALF_OPEN_L2"),
            ("closed", "half_open", False, "half_open", "TIMESTAMP_HALF_OPEN_L2"),
            # XOR matrix: non-HALF_OPEN side newer (still uses XOR enum, but the
            # newer non-HALF_OPEN state wins by timestamp — Most Restrictive
            # Wins is bypassed for any XOR drift).
            ("half_open", "open", False, "open", "TIMESTAMP_HALF_OPEN_L2"),
            ("half_open", "closed", False, "closed", "TIMESTAMP_HALF_OPEN_L2"),
            ("open", "half_open", True, "open", "TIMESTAMP_HALF_OPEN_L1"),
            ("closed", "half_open", True, "closed", "TIMESTAMP_HALF_OPEN_L1"),
        ],
    )
    def test_xor_matrix_resolves_by_timestamp(
        self, l1_state, l2_state, l1_newer, expected_state, expected_result_attr
    ):
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()
        anchor = datetime.now(UTC)
        l1_time = anchor if l1_newer else anchor - timedelta(seconds=10)
        l2_time = anchor - timedelta(seconds=10) if l1_newer else anchor

        winner_state, result = reconciler.reconcile(
            service_name="svc",
            l1_state=l1_state,
            l2_state=l2_state,
            l1_updated_at=l1_time,
            l2_updated_at=l2_time,
        )

        assert winner_state == expected_state
        assert result == getattr(DriftReconciliationResult, expected_result_attr)

    def test_xor_no_timestamps_prefers_half_open_l2(self):
        """No timestamps + L2 is HALF_OPEN → TIMESTAMP_HALF_OPEN_L2.

        The fallback deliberately favors the HALF_OPEN side so that an
        L2-Lua-driven transition isn't lost when both sides forgot to
        stamp ``updated_at`` (e.g., test fixtures, legacy data).
        """
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        winner_state, result = reconciler.reconcile(
            service_name="svc",
            l1_state="open",
            l2_state="half_open",
        )

        assert winner_state == "half_open"
        assert result == DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L2

    def test_open_vs_closed_still_uses_most_restrictive_wins(self):
        """Non-XOR drifts (no HALF_OPEN involved) keep the priority rule.

        D7 only added an exception for the XOR case. OPEN vs CLOSED is the
        original ``L1_WINS`` / ``L2_WINS`` partition-protection branch and
        must remain unchanged — relaxing it would let a stale L2 less-
        restrictive state win during a partition.
        """
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        winner_state, result = reconciler.reconcile(
            service_name="svc",
            l1_state="closed",
            l2_state="open",
        )

        assert winner_state == "open"
        assert result == DriftReconciliationResult.L2_WINS


class TestDriftReconciliation:
    """Drift reconciliation tests."""

    def setup_method(self):
        """Reset the DriftReconciler before each test."""
        from baldur.adapters.memory.circuit_breaker import get_drift_reconciler

        self.reconciler = get_drift_reconciler()
        self.reconciler.clear_history()

    def test_most_restrictive_wins_open_vs_closed(self):
        """OPEN > CLOSED priority."""
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        # When: OPEN vs CLOSED
        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="open",
            l2_state="closed",
        )

        # Then: OPEN wins (more restrictive)
        assert winner_state == "open"
        assert result == DriftReconciliationResult.L1_WINS

    def test_half_open_xor_closed_resolves_by_timestamp(self):
        """476 D7: HALF_OPEN-XOR resolves by timestamp, not priority.

        Pre-476 this was Most Restrictive Wins (HALF_OPEN > CLOSED → L1
        wins by priority). Post-476, exactly-one-side-HALF_OPEN drifts
        resolve by timestamp because L2 owns OPEN→HALF_OPEN transitions
        atomically and the priority rule would otherwise reverse them.
        """
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        # When: HALF_OPEN vs CLOSED, no timestamps — fall back prefers
        # the HALF_OPEN side (deliberate permission window).
        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="half_open",
            l2_state="closed",
        )

        assert winner_state == "half_open"
        assert result == DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L1

    def test_half_open_xor_open_resolves_by_timestamp(self):
        """476 D7: OPEN vs HALF_OPEN XOR — newer wins, not priority.

        Pre-476 OPEN beat HALF_OPEN via L2_WINS. Post-476, the L2-Lua
        atomic OPEN→HALF_OPEN decision must be preserved when newer.
        """
        from datetime import datetime, timedelta

        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()
        now = datetime.now(UTC)

        # L2 just transitioned to HALF_OPEN via Lua; L1 is still stale OPEN.
        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="open",
            l2_state="half_open",
            l1_updated_at=now - timedelta(seconds=10),
            l2_updated_at=now,
        )

        assert winner_state == "half_open"
        assert result == DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L2

    def test_timestamp_tiebreaker_same_state(self):
        """Same state resolves by timestamp."""
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        now = datetime.now(UTC)
        l1_time = now - timedelta(seconds=5)
        l2_time = now  # L2 is newer

        # When: same state, L2 newer
        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="open",
            l2_state="open",
            l1_updated_at=l1_time,
            l2_updated_at=l2_time,
        )

        # Then: same state means no drift
        assert result == DriftReconciliationResult.NO_DRIFT

    def test_timestamp_tiebreaker_different_priority_same_level(self):
        """Same priority level resolves by timestamp."""
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        now = datetime.now(UTC)
        l1_time = now
        l2_time = now - timedelta(seconds=5)

        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="closed",
            l2_state="closed",
            l1_updated_at=l1_time,
            l2_updated_at=l2_time,
        )

        # Same state yields NO_DRIFT
        assert result == DriftReconciliationResult.NO_DRIFT

    def test_jitter_distribution(self):
        """Jitter is uniformly distributed."""
        from baldur.adapters.memory.circuit_breaker import DriftReconciler

        reconciler = DriftReconciler(
            min_jitter_seconds=0.0,
            max_jitter_seconds=5.0,
        )

        jitters = [reconciler.get_jitter() for _ in range(1000)]

        # Mean lands near 2.5 seconds
        avg = sum(jitters) / len(jitters)
        assert 2.0 < avg < 3.0, f"Jitter mean outside the expected range: {avg}"

        # Min/max stay within the range
        assert min(jitters) >= 0.0
        assert max(jitters) <= 5.0

    def test_thundering_herd_prevention(self):
        """Thundering herd prevention."""
        from baldur.adapters.memory.circuit_breaker import DriftReconciler

        # Given: 100 reconcilers (one per simulated pod)
        jitters = []
        for _ in range(100):
            reconciler = DriftReconciler(
                min_jitter_seconds=0.0,
                max_jitter_seconds=5.0,
            )
            jitters.append(reconciler.get_jitter())

        # Then: many unique values are produced
        unique_jitters = {round(j, 2) for j in jitters}
        assert len(unique_jitters) > 50, (
            f"Insufficient jitter spread: {len(unique_jitters)} unique"
        )

        # Spread across the window (threshold relaxed for statistical variance)
        in_first_second = sum(1 for j in jitters if j < 1.0)
        in_last_second = sum(1 for j in jitters if j >= 4.0)
        # A uniform 0-5s distribution expects ~20 per one-second bucket (20%)
        # Relaxed to >= 5 for statistical variance (lowered from 10)
        assert in_first_second >= 5, (
            f"Not enough jitter spread in the first second: {in_first_second}"
        )
        assert in_last_second >= 5, (
            f"Not enough jitter spread in the last second: {in_last_second}"
        )

    def test_reconciler_history_tracking(self):
        """Reconciliation history tracking."""
        from baldur.adapters.memory.circuit_breaker import DriftReconciler

        reconciler = DriftReconciler()
        reconciler.clear_history()

        # When: several reconciliations run
        reconciler.reconcile("svc-1", "open", "closed")
        reconciler.reconcile("svc-2", "closed", "open")
        reconciler.reconcile("svc-3", "half_open", "half_open")

        # Then: history is recorded
        history = reconciler.get_history()
        assert len(history) == 3
        assert history[0].service_name == "svc-1"
        assert history[1].service_name == "svc-2"
        assert history[2].service_name == "svc-3"

    def test_reconciler_stats(self):
        """Reconciliation statistics."""
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
        )

        reconciler = DriftReconciler()
        reconciler.clear_history()

        # When: several reconciliations run
        reconciler.reconcile("svc-1", "open", "closed")  # L1 wins
        reconciler.reconcile("svc-2", "closed", "open")  # L2 wins
        reconciler.reconcile("svc-3", "closed", "closed")  # No drift

        # Then: stats reflect them
        stats = reconciler.get_stats()
        assert stats["total_reconciliations"] == 3
        assert stats["by_result"]["l1_wins"] == 1
        assert stats["by_result"]["l2_wins"] == 1
        assert stats["by_result"]["no_drift"] == 1
        assert len(stats["affected_services"]) == 3


class TestDriftRepairRoutingContract:
    """Which reconciliation verdicts send the local row up to the shared store.

    The resolver decides the winner; this predicate decides where that
    winner has to be written. Its whole reason for existing is the
    HALF_OPEN-XOR verdict that names the local row: without a route to the
    repair, the caller's else branch copies the losing shared state back
    over the row that just won. It yields to operator pins, though — the
    repair's own skip only looks at the local flag, so a pinned shared row
    keeps the copy-down behavior.
    """

    # Every verdict -> (routes when the shared row is unpinned,
    #                  routes when it is pinned).
    _ROUTING_TABLE = {
        "l1_wins": (True, True),
        "l2_wins": (False, False),
        "timestamp_l1": (True, True),
        "timestamp_l2": (False, False),
        "timestamp_half_open_l1": (True, False),
        "timestamp_half_open_l2": (False, False),
        "no_drift": (False, False),
        "skipped": (False, False),
    }

    def test_decision_table_covers_every_reconciliation_result(self):
        """A new verdict must be given a route explicitly, not defaulted."""
        from baldur.adapters.memory.circuit_breaker import DriftReconciliationResult

        assert {r.value for r in DriftReconciliationResult} == set(self._ROUTING_TABLE)

    @pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
    @pytest.mark.parametrize("result_value", sorted(_ROUTING_TABLE))
    def test_routing_decision_matches_the_table(self, result_value, pinned):
        from baldur.adapters.memory.circuit_breaker import DriftReconciliationResult
        from baldur.adapters.memory.layered_repository.drift_operations import (
            DriftOperationsMixin,
        )
        from baldur.interfaces.repositories import CircuitBreakerStateData

        result = DriftReconciliationResult(result_value)
        l2_state = CircuitBreakerStateData(
            service_name="svc",
            state="open",
            manually_controlled=pinned,
        )
        expected = self._ROUTING_TABLE[result_value][1 if pinned else 0]

        assert DriftOperationsMixin._routes_to_l1_repair(result, l2_state) is expected


class TestDriftHalfOpenWinnerRoutingBehavior:
    """End-to-end: the XOR winner reaches L2 instead of being overwritten.

    Drives the public ``reconcile_single_service`` entry point so the
    routing predicate is exercised where it actually runs, and the reported
    action names the direction the row was written.
    """

    @pytest.fixture
    def repo(self, mock_l2_repo):
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(
            l2_repo=mock_l2_repo,
            adapter_type="redis",
        )
        # The default 50 ms L2 budget is tight enough to flake on a loaded
        # xdist worker; the mock resolves instantly either way.
        repo._get_timeout_seconds = lambda: 5.0
        return repo

    def _seed_newer_l1_half_open(self, repo, mock_l2_repo, *, l2_pinned):
        """L1 HALF_OPEN (newer) drifting against an older L2 OPEN row."""
        from baldur.interfaces.repositories import CircuitBreakerStateData
        from baldur.utils.time import utc_now

        repo._l1.get_or_create("svc")
        repo._l1.update_state(service_name="svc", state="half_open")
        l1_state = repo._l1.get_by_service_name("svc")

        mock_l2_repo.get_by_service_name.return_value = CircuitBreakerStateData(
            service_name="svc",
            state="open",
            manually_controlled=l2_pinned,
            opened_at=utc_now() - timedelta(seconds=30),
            updated_at=l1_state.updated_at - timedelta(seconds=10),
        )

    def test_unpinned_l2_receives_the_half_open_winner(self, repo, mock_l2_repo):
        """The local HALF_OPEN win is repaired up, and survives locally."""
        self._seed_newer_l1_half_open(repo, mock_l2_repo, l2_pinned=False)

        outcome = repo.reconcile_single_service("svc")

        assert outcome["action"] == "l1_to_l2"
        assert outcome["winner_state"] == "half_open"
        assert repo._l1.get_by_service_name("svc").state == "half_open"
        mock_l2_repo.update_state.assert_called_once_with(
            service_name="svc",
            state="half_open",
            failure_count=0,
            success_count=0,
            opened_at=None,
            clear_opened_at=True,
            skip_if_pinned=True,
        )

    def test_pinned_l2_keeps_its_state_and_is_copied_down(self, repo, mock_l2_repo):
        """An operator pin outranks an automatic timestamp win."""
        self._seed_newer_l1_half_open(repo, mock_l2_repo, l2_pinned=True)

        outcome = repo.reconcile_single_service("svc")

        assert outcome["action"] == "l2_to_l1"
        assert repo._l1.get_by_service_name("svc").state == "open"
        mock_l2_repo.update_state.assert_not_called()
