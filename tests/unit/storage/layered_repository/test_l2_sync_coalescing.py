"""Record-path mirror: fresh read, per-service coalescing, and pin neutrality.

The mirror used to carry a snapshot taken at submit time, so one trip was six
concurrent snapshots — five ``closed/N`` from the failure records plus one
``open`` from the trip — and whichever finished last decided the durable row.
A genuine trip could therefore settle as ``closed``.

Fresh-reading at execution is not enough on its own: the mirror submitted by
the trip-triggering failure record runs *concurrently with* the trip and would
still read a pre-trip row. So at most one task per service is in flight, a
submit arriving while one runs sets a dirty flag instead of queueing behind
it, and the trip nudges the lane after its own L1 writeback.

Covered here:

- the ``{in_flight, dirty}`` state machine, including the two ways it could
  strand a service as permanently busy;
- pin neutrality on both sides — the local freshness gate reads the row
  through the expiry-aware predicate, and the write itself carries the
  store-side directive;
- the ordering regression the whole design exists for: a stale mirror holding
  a pre-trip row cannot leave the store non-OPEN once the executor drains.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.utils.time import utc_now

SVC = "payment.charge"


class _InlineExecutor:
    """Executor stub that runs submitted callables synchronously inline.

    Makes the fire-and-forget mirror deterministic and exposes ``submit_count``
    so the coalescing invariant — a submit arriving while a task runs must not
    queue a second one — is directly assertable.
    """

    def __init__(self):
        self.submit_count = 0

    def submit(self, fn, *args, **kwargs):
        self.submit_count += 1
        fn(*args, **kwargs)
        return MagicMock(spec=Future)


@pytest.fixture
def repo(mock_l2_repo):
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    r = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2_repo, adapter_type="redis")
    r._l1.get_or_create(SVC)
    mock_l2_repo.reset_mock()
    r._l2_healthy = True
    r._l2_consecutive_failures = 0
    return r


@pytest.fixture
def real_l2_repo():
    """A real InMemory repository standing in for the durable store.

    The pin-skip and ordering claims are about what the store row ends up
    holding, which a mock cannot answer.
    """
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )

    return InMemoryCircuitBreakerStateRepository()


# =============================================================================
# Behavior — the {in_flight, dirty} coalescing state machine
# =============================================================================


class TestMirrorCoalescingBehavior:
    """One task per service in flight; a dirty flag stands in for the queue."""

    def test_submit_while_a_task_runs_sets_dirty_instead_of_queueing(self, repo):
        # Given: a mirror task that issues three more submits mid-write, the
        # way three record_failure calls landing during one mirror would.
        inline = _InlineExecutor()
        reads: list[int] = []

        def _pass(service_name):
            reads.append(repo._l1.get_by_service_name(service_name).failure_count)
            if len(reads) == 1:
                for _ in range(3):
                    repo._l1.record_failure(service_name)
                    repo._sync_to_l2_async(service_name)

        with (
            patch.object(repo, "_get_executor", return_value=inline),
            patch.object(repo, "_repair_row_to_l2_inline", side_effect=_pass),
        ):
            repo._sync_to_l2_async(SVC)

        # Then: one submit total, and exactly one re-run — whose read
        # postdates the last of the three writes rather than replaying each.
        assert inline.submit_count == 1
        assert reads == [0, 3]

    def test_task_exits_when_no_write_arrived_during_it(self, repo):
        inline = _InlineExecutor()
        passes: list[str] = []

        with (
            patch.object(repo, "_get_executor", return_value=inline),
            patch.object(repo, "_repair_row_to_l2_inline", side_effect=passes.append),
        ):
            repo._sync_to_l2_async(SVC)

        assert passes == [SVC]
        # The service is released, so the next write mirrors immediately.
        assert SVC not in repo._mirror_in_flight
        assert SVC not in repo._mirror_dirty

    def test_dirty_is_cleared_before_the_read_not_after_the_write(self, repo):
        # A write landing *during* the mirror must re-arm the flag. Clearing
        # after the write would swallow it and leave the store stale.
        inline = _InlineExecutor()
        seen_dirty: list[bool] = []

        def _pass(service_name):
            seen_dirty.append(service_name in repo._mirror_dirty)
            if len(seen_dirty) == 1:
                repo._sync_to_l2_async(service_name)

        with (
            patch.object(repo, "_get_executor", return_value=inline),
            patch.object(repo, "_repair_row_to_l2_inline", side_effect=_pass),
        ):
            repo._sync_to_l2_async(SVC)

        # Pass 1 starts clean; pass 2 starts clean again because the flag the
        # mid-write submit set was consumed by the re-run decision.
        assert seen_dirty == [False, False]

    def test_a_submit_that_raises_releases_the_service(self, repo):
        # An executor rejecting the task after the flag was set would suppress
        # this service's mirroring for the rest of the process.
        failing_executor = MagicMock(spec=ThreadPoolExecutor)
        failing_executor.submit.side_effect = RuntimeError("pool shut down")

        with capture_logs() as caplog:
            with patch.object(repo, "_get_executor", return_value=failing_executor):
                repo._sync_to_l2_async(SVC)

        assert SVC not in repo._mirror_in_flight
        assert any(
            entry.get("event") == "layered_repo.submit_sync_task_failed"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )

        # And the next submit is attempted rather than silently dropped.
        inline = _InlineExecutor()
        with (
            patch.object(repo, "_get_executor", return_value=inline),
            patch.object(repo, "_repair_row_to_l2_inline"),
        ):
            repo._sync_to_l2_async(SVC)
        assert inline.submit_count == 1

    def test_a_task_that_dies_releases_the_service(self, repo):
        # The mirror body routes its own failures to the quarantine handlers,
        # but the task's own guard has to hold for anything it cannot: a task
        # that dies holding the flag would mark the service busy forever.
        # Driven directly rather than through a submit, so the release under
        # test is the task's ``finally`` and not the submit's handler.
        repo._mirror_in_flight.add(SVC)

        with patch.object(
            repo, "_repair_row_to_l2_inline", side_effect=RuntimeError("task died")
        ):
            with pytest.raises(RuntimeError, match="task died"):
                repo._run_mirror_task(SVC)

        assert SVC not in repo._mirror_in_flight

    def test_quarantined_l2_neither_submits_nor_marks_the_service(self, repo):
        repo._l2_healthy = False
        executor = MagicMock(spec=ThreadPoolExecutor)

        with patch.object(repo, "_get_executor", return_value=executor):
            repo._sync_to_l2_async(SVC)

        executor.submit.assert_not_called()
        assert SVC not in repo._mirror_in_flight
        assert SVC not in repo._mirror_dirty

    def test_coalescing_is_per_service(self, repo):
        # A busy service must not stall an unrelated one.
        repo._l1.get_or_create("other")
        inline = _InlineExecutor()
        mirrored: list[str] = []

        def _pass(service_name):
            mirrored.append(service_name)
            if len(mirrored) == 1:
                repo._sync_to_l2_async("other")

        with (
            patch.object(repo, "_get_executor", return_value=inline),
            patch.object(repo, "_repair_row_to_l2_inline", side_effect=_pass),
        ):
            repo._sync_to_l2_async(SVC)

        assert mirrored == [SVC, "other"]
        assert inline.submit_count == 2


# =============================================================================
# Behavior — pin neutrality, local gate and store-side directive
# =============================================================================


class TestRepairRowPinSkipBehavior:
    """The freshness gate reads the expiry-aware rule, never the raw flag."""

    def test_active_pin_skips_the_mirror(self, repo):
        repo._l1.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.OPEN.value,
            reason="operator block",
            expires_at=utc_now() + timedelta(minutes=10),
        )

        with capture_logs() as caplog:
            row = repo._resolve_repair_row(SVC)

        assert row is None
        assert any(
            entry.get("event") == "layered_repo.repair_skipped_manually_controlled"
            for entry in caplog
        )

    def test_lapsed_pin_is_mirrored(self, repo):
        # The negative twin of the skip. A raw-flag gate would stop L2
        # delivery permanently for any service this worker once hydrated
        # pinned — no automatic transition clears the flag, and only one
        # process per host runs the sweep that does.
        repo._l1.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.OPEN.value,
            reason="expired block",
            expires_at=utc_now() - timedelta(seconds=1),
        )

        row = repo._resolve_repair_row(SVC)

        assert row is not None
        assert row.service_name == SVC

    def test_missing_row_is_skipped_rather_than_resurrected(self, repo):
        row = repo._resolve_repair_row("never-created")

        assert row is None

    def test_inline_repair_passes_the_store_side_pin_guard(self, repo, mock_l2_repo):
        # The local gate cannot see an override a peer placed and this worker
        # never hydrated, so the test rides in the write itself.
        repo._repair_row_to_l2_inline(SVC)

        assert mock_l2_repo.update_state.call_args.kwargs["skip_if_pinned"] is True

    def test_timeout_bounded_repair_passes_the_store_side_pin_guard(
        self, repo, mock_l2_repo
    ):
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            repo._repair_row_to_l2(SVC)

        assert mock_l2_repo.update_state.call_args.kwargs["skip_if_pinned"] is True

    def test_closed_row_mirrors_an_explicit_opened_at_clear(self, repo, mock_l2_repo):
        # A CLOSED snapshot carries no OPEN-era timestamp, and None means
        # "keep" at the storage boundary — so the durable row would go on
        # reporting the instant it opened.
        repo._repair_row_to_l2_inline(SVC)

        kwargs = mock_l2_repo.update_state.call_args.kwargs
        assert kwargs["opened_at"] is None
        assert kwargs["clear_opened_at"] is True

    def test_open_row_does_not_clear_its_own_timestamp(self, repo, mock_l2_repo):
        repo._l1.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )

        repo._repair_row_to_l2_inline(SVC)

        kwargs = mock_l2_repo.update_state.call_args.kwargs
        assert kwargs["opened_at"] is not None
        assert kwargs["clear_opened_at"] is False

    def test_actively_pinned_store_row_survives_the_record_path_mirror(
        self, real_l2_repo
    ):
        # The composed hazard: this worker never hydrated the peer's override,
        # so it has nothing local to skip on and would otherwise write plain
        # state straight over the operator's decision.
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        real_l2_repo.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.CLOSED.value,
            controlled_by_id=7,
            reason="peer allow",
            expires_at=utc_now() + timedelta(minutes=10),
        )
        before = real_l2_repo.get_by_service_name(SVC)

        repo = LayeredCircuitBreakerStateRepository(
            l2_repo=real_l2_repo, adapter_type="redis"
        )
        repo._l1.get_or_create(SVC)
        repo._l1.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )
        repo._l2_healthy = True
        repo._l2_consecutive_failures = 0
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            repo._sync_to_l2_async(SVC)

        after = real_l2_repo.get_by_service_name(SVC)
        assert after.state == before.state
        assert after.manually_controlled is True
        assert after.updated_at == before.updated_at
        # A declined write is a healthy answer, not a failure: the skip must
        # not push L2 toward quarantine.
        assert repo._l2_consecutive_failures == 0

    def test_lapsed_store_pin_does_not_block_the_record_path_mirror(self, real_l2_repo):
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        real_l2_repo.set_manual_control(
            SVC,
            CircuitBreakerStateEnum.CLOSED.value,
            reason="expired allow",
            expires_at=utc_now() - timedelta(seconds=1),
        )

        repo = LayeredCircuitBreakerStateRepository(
            l2_repo=real_l2_repo, adapter_type="redis"
        )
        repo._l1.get_or_create(SVC)
        repo._l1.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )
        repo._l2_healthy = True
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            repo._sync_to_l2_async(SVC)

        assert (
            real_l2_repo.get_by_service_name(SVC).state
            == CircuitBreakerStateEnum.OPEN.value
        )


# =============================================================================
# Behavior — the ordering regression (SC1)
# =============================================================================


class TestTripSurvivesConcurrentMirrorBehavior:
    """A mirror holding a pre-trip row cannot leave the store non-OPEN."""

    def test_stale_mirror_write_is_corrected_before_the_executor_drains(
        self, real_l2_repo
    ):
        # Given: L1 CLOSED with the failure burst that is about to trip, and a
        # mirror task already resident on the pool holding that pre-trip row.
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(
            l2_repo=real_l2_repo, adapter_type="redis"
        )
        repo._l1.get_or_create(SVC)
        for _ in range(5):
            repo._l1.record_failure(SVC)
        repo._l2_healthy = True

        original_inline = repo._sync_to_l2_inline
        reached_write = threading.Event()
        release_write = threading.Event()
        written_states: list[str] = []
        state_lock = threading.Lock()

        def _held_inline(service_name, state, skip_if_pinned=False):
            """Hold the first mirror write between its read and its write.

            That is the exact interleaving the fix targets: the row was
            resolved before the trip, the write lands after it.
            """
            if not reached_write.is_set():
                reached_write.set()
                assert release_write.wait(timeout=10)
            with state_lock:
                written_states.append(state.state)
            return original_inline(service_name, state, skip_if_pinned=skip_if_pinned)

        executor = ThreadPoolExecutor(max_workers=2)
        try:
            with (
                patch.object(repo, "_get_executor", return_value=executor),
                patch.object(repo, "_sync_to_l2_inline", side_effect=_held_inline),
            ):
                # When: the record path's mirror is in flight holding CLOSED...
                repo._sync_to_l2_async(SVC)
                assert reached_write.wait(timeout=10)

                # ...and the trip lands in the store while it waits.
                attempt = repo.trip_to_open(SVC, 5)
                assert attempt.did_open is True
                assert (
                    real_l2_repo.get_by_service_name(SVC).state
                    == CircuitBreakerStateEnum.OPEN.value
                )

                # Then: releasing the stale write must not be the last word.
                # The drain happens inside the patch so the task's dirty
                # re-run is recorded too.
                release_write.set()
                executor.shutdown(wait=True)
        finally:
            executor.shutdown(wait=True)

        # The stale CLOSED really did land — without this the test would pass
        # for the wrong reason, never having reproduced the race at all.
        assert written_states[0] == CircuitBreakerStateEnum.CLOSED.value
        assert written_states[-1] == CircuitBreakerStateEnum.OPEN.value
        assert (
            real_l2_repo.get_by_service_name(SVC).state
            == CircuitBreakerStateEnum.OPEN.value
        )
        assert real_l2_repo.get_by_service_name(SVC).opened_at is not None
