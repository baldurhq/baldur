"""656 D2 — LayeredCircuitBreakerStateRepository.apply_peer_cb_state.

The peer-side L1 updater for cluster-wide OPEN/CLOSED propagation. Updates
L1 ONLY (never ``_sync_to_l2_async`` — the emitting worker already owns the
authoritative L2 write). Idempotent by construction: applying a state L1
already holds is a no-op. Returns ``True`` iff L1 actually transitioned, so
the listener records the peer-propagation metric (``applied`` vs ``noop``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.utils.time import utc_now


@pytest.fixture
def repo():
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    return LayeredCircuitBreakerStateRepository(l2_repo=None)


def _set_l1_state(repo, service_name, state):
    repo._l1.get_or_create(service_name)
    repo._l1.update_state(service_name=service_name, state=state)


# =============================================================================
# Behavior — state-transition table + idempotency
# =============================================================================


class TestApplyPeerCbStateBehavior:
    """``new_state`` x ``L1 prior`` transition / idempotency table (656 D2)."""

    def test_missing_l1_plus_open_transitions_and_returns_true(self, repo):
        # Given: no L1 entry (resolves to the CLOSED default).
        changed = repo.apply_peer_cb_state("svc", "open")

        # Then: L1 transitions to OPEN, returns True.
        assert changed is True
        assert repo._l1.get_by_service_name("svc").state == "open"

    def test_missing_l1_plus_closed_is_noop_returns_false(self, repo):
        # Given: no L1 entry — already resolves to closed; applying closed is
        # an idempotent no-op.
        changed = repo.apply_peer_cb_state("svc", "closed")

        assert changed is False

    def test_closed_l1_plus_open_transitions_returns_true(self, repo):
        _set_l1_state(repo, "svc", CircuitBreakerStateEnum.CLOSED.value)

        changed = repo.apply_peer_cb_state("svc", "open")

        assert changed is True
        assert repo._l1.get_by_service_name("svc").state == "open"

    def test_open_l1_plus_open_is_noop_returns_false(self, repo):
        _set_l1_state(repo, "svc", CircuitBreakerStateEnum.OPEN.value)

        changed = repo.apply_peer_cb_state("svc", "open")

        assert changed is False
        assert repo._l1.get_by_service_name("svc").state == "open"

    def test_open_l1_plus_closed_transitions_returns_true(self, repo):
        _set_l1_state(repo, "svc", CircuitBreakerStateEnum.OPEN.value)

        changed = repo.apply_peer_cb_state("svc", "closed")

        assert changed is True
        assert repo._l1.get_by_service_name("svc").state == "closed"

    def test_half_open_l1_plus_open_abandons_trial_returns_true(self, repo):
        # HALF_OPEN handling: a local trial slot is abandoned when a peer
        # reports OPEN (the safe response to a peer detecting failure).
        _set_l1_state(repo, "svc", CircuitBreakerStateEnum.HALF_OPEN.value)

        changed = repo.apply_peer_cb_state("svc", "open")

        assert changed is True
        assert repo._l1.get_by_service_name("svc").state == "open"

    def test_open_apply_carries_opened_at(self, repo):
        opened = utc_now()

        repo.apply_peer_cb_state("svc", "open", opened)

        assert repo._l1.get_by_service_name("svc").opened_at == opened

    def test_closed_apply_resets_l1_window(self, repo):
        # Given: L1 OPEN with a primed failure window.
        _set_l1_state(repo, "svc", CircuitBreakerStateEnum.OPEN.value)
        for _ in range(3):
            repo._l1.record_failure("svc")
        assert len(repo._l1._call_windows["svc"]) == 3

        repo.apply_peer_cb_state("svc", "closed")

        # The CLOSED apply resets the L1 sliding window via reset_counts.
        assert len(repo._l1._call_windows["svc"]) == 0
        assert repo._l1.get_by_service_name("svc").state == "closed"


# =============================================================================
# Behavior — L1-only invariant (never syncs to L2)
# =============================================================================


class TestApplyPeerCbStateL1Only:
    """The peer apply never writes L2 (the emitter already owns L2) (656 D2)."""

    def test_open_apply_does_not_sync_to_l2(self, repo):
        with patch.object(repo, "_sync_to_l2_async") as mock_sync:
            repo.apply_peer_cb_state("svc", "open")

        mock_sync.assert_not_called()

    def test_closed_apply_does_not_sync_to_l2(self, repo):
        _set_l1_state(repo, "svc", CircuitBreakerStateEnum.OPEN.value)

        with patch.object(repo, "_sync_to_l2_async") as mock_sync:
            repo.apply_peer_cb_state("svc", "closed")

        mock_sync.assert_not_called()
