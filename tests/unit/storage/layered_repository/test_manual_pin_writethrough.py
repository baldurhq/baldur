"""Pins reach L2 through the manual-control ops, and no repair lane erases one.

Two halves of the same contract:

- **Write-through (D3).** The generic state mirror carries four state fields,
  so a pin written through it alone never reaches the durable row. The five
  manual-control operations therefore drive a second, explicit L2 write. It is
  synchronous because the operator's response is read back once the op returns:
  the expiry reported and the expiry stored must be the same instant.
- **Pin neutrality (D11).** Every whole-row L1→L2 repair lane re-reads the row
  and skips a pinned one. The mirror opens with L2 ``get_or_create``, which on
  a missing key writes the default payload — "not manually controlled"
  included — so repairing a pinned service after a keyspace loss used to erase
  the operator's decision from the shared store. And the reconciler wrote from
  a pass-entry snapshot, so a Block placed mid-pass was overwritten by it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
)
from baldur.utils.time import utc_now

SERVICE = "payment-api"


class RecordingL2(InMemoryCircuitBreakerStateRepository):
    """A real in-memory store that also keeps an ordered log of its writes.

    A MagicMock would satisfy every assertion here without ever holding a row,
    so the fake is a real repository first and a recorder second.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    def get_or_create(self, service_name):
        self.calls.append(("get_or_create", {"service_name": service_name}))
        return super().get_or_create(service_name)

    def update_state(self, service_name, state, **kwargs):
        self.calls.append(
            ("update_state", {"service_name": service_name, "state": state, **kwargs})
        )
        return super().update_state(service_name, state, **kwargs)

    def set_manual_control(self, service_name, state, *args, **kwargs):
        self.calls.append(
            (
                "set_manual_control",
                {"service_name": service_name, "state": state, **kwargs},
            )
        )
        return super().set_manual_control(service_name, state, *args, **kwargs)

    def clear_manual_control(self, service_name, preserve_reason=False):
        self.calls.append(
            ("clear_manual_control", {"service_name": service_name}),
        )
        return super().clear_manual_control(service_name, preserve_reason)

    def atomic_reset(self, service_name, reason="", controlled_by_id=None):
        self.calls.append(("atomic_reset", {"service_name": service_name}))
        return super().atomic_reset(service_name, reason, controlled_by_id)

    @property
    def written_names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def l2() -> RecordingL2:
    return RecordingL2()


@pytest.fixture
def repo(l2) -> LayeredCircuitBreakerStateRepository:
    layered = LayeredCircuitBreakerStateRepository(l2_repo=l2)
    l2.calls.clear()  # construction-time load is not under test here
    return layered


# The five operations that write an operator's manual-control decision.
# ``clear_manual_control`` and ``atomic_reset`` write the *absence* of a pin,
# which must reach L2 for the same reason a pin must.
PIN_OPS = [
    pytest.param(
        lambda r: r.atomic_force_open(SERVICE, reason="incident", ttl_minutes=90),
        True,
        id="atomic_force_open",
    ),
    pytest.param(
        lambda r: r.atomic_force_close(SERVICE, reason="recovered", ttl_minutes=30),
        True,
        id="atomic_force_close",
    ),
    pytest.param(
        lambda r: r.set_manual_control(
            SERVICE,
            state="open",
            reason="incident",
            expires_at=utc_now() + timedelta(minutes=45),
        ),
        True,
        id="set_manual_control",
    ),
    pytest.param(
        lambda r: r.atomic_reset(SERVICE, reason="done"), False, id="atomic_reset"
    ),
    pytest.param(
        lambda r: r.clear_manual_control(SERVICE), False, id="clear_manual_control"
    ),
]


def _seed_existing_pin(repo, l2) -> None:
    """Put a live pin on the row before the op under test runs.

    The two clearing ops are no-ops on an absent or unpinned row — they would
    return False and never reach the write-through at all, so the parametrized
    cases would pass while exercising nothing.
    """
    repo.set_manual_control(
        SERVICE,
        state="open",
        reason="pre-existing",
        expires_at=utc_now() + timedelta(minutes=10),
    )
    l2.calls.clear()


# =============================================================================
# D3 — the five manual-control ops write their pin through, synchronously
# =============================================================================


class TestManualControlWriteThroughBehavior:
    """Pre-fix red run: every op fired ``_sync_to_l2_async`` only, so the L2
    row carried the new state with ``manually_controlled`` untouched — and for
    a fire-and-forget write, not even reliably by the time the operator's
    response was built."""

    @pytest.mark.parametrize(("op", "expect_pinned"), PIN_OPS)
    def test_the_pin_state_reaches_l2(self, repo, l2, op, expect_pinned):
        """The durable row agrees with L1 on whether the service is pinned."""
        _seed_existing_pin(repo, l2)

        op(repo)

        assert l2.get_by_service_name(SERVICE).manually_controlled is expect_pinned
        assert repo.get_by_service_name(SERVICE).manually_controlled is expect_pinned

    @pytest.mark.parametrize(("op", "_expect_pinned"), PIN_OPS)
    def test_the_state_mirror_is_written_before_the_pin(
        self, repo, l2, op, _expect_pinned
    ):
        """Order matters, and the safe direction is state-then-pin.

        A reader racing between the two writes sees the new state without the
        pin — an ordinary OPEN. The reverse order would expose a pin attached
        to the state it replaced.
        """
        _seed_existing_pin(repo, l2)

        op(repo)

        names = l2.written_names
        mirror_at = names.index("update_state")
        pin_at = min(
            names.index(n)
            for n in ("set_manual_control", "clear_manual_control", "atomic_reset")
            if n in names
        )
        assert mirror_at < pin_at

    def test_expiry_writethrough_equality_between_response_and_l2(self, repo, l2):
        """String equality, because that is the form the operator is shown.

        The pin write passes ``expires_at`` explicitly from the row L1 just
        produced rather than recomputing it from the TTL, which would resolve
        to a different instant than the one the response reports.
        """
        repo.atomic_force_open(SERVICE, reason="incident", ttl_minutes=90)

        reported = repo.get_by_service_name(SERVICE).manual_override_expires_at
        stored = l2.get_by_service_name(SERVICE).manual_override_expires_at

        assert reported.isoformat() == stored.isoformat()

    def test_a_failing_l2_write_still_leaves_the_operation_successful(self, repo, l2):
        """Fail-open on the durability side-effect — enforcement is the L1 row.

        The fault must actually fire, so the test asserts the L2 call was
        attempted and the failure counter moved; "no exception" alone would
        pass against a write that never ran.
        """
        attempted: list[str] = []

        def _explode(*args, **kwargs):
            attempted.append("set_manual_control")
            raise ConnectionError("redis gone")

        l2.set_manual_control = _explode
        before = repo._metrics.get("l2_sync_failure_count", 0)

        result = repo.atomic_force_open(SERVICE, reason="incident", ttl_minutes=90)

        assert result[0] is True
        assert attempted == ["set_manual_control"]
        assert repo._metrics["l2_sync_failure_count"] > before
        # Enforcement is unaffected: L1 carries the pin regardless.
        assert repo.get_by_service_name(SERVICE).manually_controlled is True


# =============================================================================
# D11 — repair lanes are pin-neutral
# =============================================================================


class TestRepairSkipsPinnedRowBehavior:
    """No repair lane creates, erases, or contradicts an operator's pin."""

    def test_repair_skips_pinned_row_and_writes_nothing_to_l2(self, repo, l2):
        """The keyspace-loss case: L2 lost the row, L1 still holds the Block.

        Negative assertion — nothing at all is written. ``get_or_create`` is
        the erasing call: on a missing key it writes the default payload,
        ``manually_controlled=False`` included.
        """
        repo._l1.set_manual_control(
            SERVICE,
            state="open",
            reason="incident",
            expires_at=utc_now() + timedelta(minutes=90),
        )
        l2.delete_state(SERVICE)
        l2.calls.clear()

        outcome = repo._repair_row_to_l2(SERVICE)

        assert outcome is None
        assert l2.calls == []
        assert l2.get_by_service_name(SERVICE) is None

    def test_repair_skips_pinned_row_but_still_mirrors_an_unpinned_one(self, repo, l2):
        """Positive control: the lane is skipped for pins, not broken outright.

        Without this the negative assertion above could pass against a helper
        that never mirrors anything.
        """
        repo._l1.get_or_create(SERVICE)
        repo._l1.update_state(SERVICE, state="open", failure_count=5)
        l2.delete_state(SERVICE)
        l2.calls.clear()

        outcome = repo._repair_row_to_l2(SERVICE)

        assert outcome is True
        assert l2.get_by_service_name(SERVICE).state == "open"

    def test_a_missing_row_is_a_skip_not_a_failure(self, repo, l2):
        """Tri-state return: ``None`` (nothing attempted) ≠ ``False`` (failed).

        Collapsing the two would make ``force_sync_to_l2`` report success=False
        for a keyspace that simply moved on.
        """
        assert repo._repair_row_to_l2("never-seen") is None

    def test_repair_skips_pinned_row_taken_mid_reconciliation_pass(self, repo, l2):
        """The stale-snapshot interleaving, reproduced without timing.

        ``_reconcile_all_drift`` takes an L1 snapshot at pass entry and then
        reads L2 per service. The fake places the Block during that per-service
        read, so by the time the repair runs the L1 row is pinned while the
        pass's own snapshot says otherwise — exactly the race, deterministically.
        """
        # Given: an unpinned row present in L1 and absent from L2, so the pass
        # takes its L1-wins repair branch.
        repo._l1.get_or_create(SERVICE)
        repo._l1.update_state(SERVICE, state="closed", failure_count=2)
        l2.delete_state(SERVICE)

        def _pin_during_the_l2_read(service_name):
            repo._l1.set_manual_control(
                service_name,
                state="open",
                reason="operator blocks mid-pass",
                expires_at=utc_now() + timedelta(minutes=90),
            )
            return None

        l2.get_by_service_name = _pin_during_the_l2_read
        l2.calls.clear()

        # When
        repo._reconcile_all_drift()

        # Then: the pass wrote nothing over the operator's decision.
        assert l2.calls == []
        row = repo._l1.get_by_service_name(SERVICE)
        assert row.manually_controlled is True
        assert row.state == "open"

    def test_repair_skips_pinned_row_in_the_admin_force_resync(self, repo, l2):
        """The admin force-resync reports the skip rather than a failure."""
        repo._l1.get_or_create("unpinned-service")
        repo._l1.update_state("unpinned-service", state="open")
        repo._l1.set_manual_control(
            SERVICE,
            state="open",
            reason="incident",
            expires_at=utc_now() + timedelta(minutes=90),
        )
        l2.delete_state(SERVICE)
        l2.calls.clear()

        report = repo.force_sync_to_l2()

        assert report["skipped"] == 1
        assert report["synced"] == 1
        assert report["failed"] == 0
        assert report["total"] == 2
        assert report["success"] is True
        # And the pinned service was never written.
        assert all(payload["service_name"] != SERVICE for _name, payload in l2.calls)
