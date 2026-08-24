"""771 — the layered repository's reject-path convergence lane.

A healthy L2 that rejects with ``closed`` against a non-closed L1 row is
answering a contradiction. Before this lane the acquire discarded that answer
on every request, so one service stayed rejected on that worker until the
breaker next tripped cluster-wide, the process restarted, or an operator
resynced — visible only as an ordinary "open" block.

Covers the four seams the lane is built from:

- detection on the request path (``_maybe_schedule_reject_convergence``) —
  the shape guard, the ordering that keeps the L1 lock read off the
  per-request path, and the isolation that stops a scheduling failure from
  flipping an L2-authoritative rejection into an L1 admission;
- pacing (``_should_schedule_reject_convergence``);
- the in-flight permit (``_submit_reject_convergence``) — released from one
  done-callback, so a cancelled or failed submit cannot leak it;
- resolution (``_run_reject_path_convergence``) — the direction its own fresh
  L2 read decides, and the outcome each branch reports.

Both producers are reproduced: a shared row lost in place (repair) and a
worker whose L1 hydrated ``half_open`` at boot while the cluster closed
(converge).
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
    LayeredCircuitBreakerStateRepository,
)
from baldur.adapters.memory.layered_repository import repository_operations
from baldur.core.rate_limiting import CooldownGate
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)

CLOSED = CircuitBreakerStateEnum.CLOSED.value
OPEN = CircuitBreakerStateEnum.OPEN.value
HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN.value

# The lane's spec values, hardcoded once here (contract assertions below).
COOLDOWN_SECONDS = 30.0
MAX_IN_FLIGHT = 2
APPLIED_EVENT = "layered_repo.reject_path_convergence_applied"
NOOP_EVENT = "layered_repo.reject_path_convergence_noop"
OUTCOMES = frozenset(
    {"converged", "repaired", "repair_failed", "skipped_pinned", "skipped", "noop"}
)
APPLIED_OUTCOMES = frozenset({"converged", "repaired"})


class _InlineExecutor:
    """Executor stub running submitted callables inline on a real Future.

    A real ``Future`` matters here: the lane releases its in-flight permit from
    a done-callback, so a stub returning a bare mock would make every permit
    assertion vacuous.
    """

    def __init__(self, raise_for=None):
        self.calls: list[tuple[object, Future]] = []
        self._raise_for = raise_for

    def submit(self, fn, *args, **kwargs):
        if self._raise_for is not None and fn == self._raise_for:
            raise RuntimeError("cannot schedule new futures after shutdown")
        future: Future = Future()
        self.calls.append((fn, future))
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - mirrored onto the future
            future.set_exception(exc)
        return future

    def result_for(self, fn):
        """The result of the (single) submit of ``fn``."""
        matches = [future for submitted, future in self.calls if submitted == fn]
        assert len(matches) == 1, f"expected exactly one submit of {fn}, got {matches}"
        return matches[0].result()


class _DegradingBackend:
    """Resilient-backend stand-in that reports degraded after N reads.

    Models the silent fold: a failed read is answered from the process-local
    fallback and the backend switches itself to degraded *before* returning,
    so the flip is only observable to a probe taken after the read.
    """

    def __init__(self, healthy_reads: int = 0):
        self.reads = 0
        self._healthy_reads = healthy_reads

    @property
    def is_degraded(self) -> bool:
        self.reads += 1
        return self.reads > self._healthy_reads


class _FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _isolated_in_flight_permits():
    """Give each test its own copy of the process-wide in-flight semaphore.

    The bound is deliberately process-wide in production; leaking a permit
    between tests would silently disable the lane for every later test in the
    worker — the exact failure mode the done-callback exists to prevent.
    """
    import threading

    original = repository_operations._reject_convergence_slots
    repository_operations._reject_convergence_slots = threading.BoundedSemaphore(
        repository_operations._REJECT_CONVERGENCE_MAX_IN_FLIGHT
    )
    yield
    repository_operations._reject_convergence_slots = original


def _available_permits() -> int:
    """Count the free permits without consuming them."""
    semaphore = repository_operations._reject_convergence_slots
    taken = 0
    while semaphore.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        semaphore.release()
    return taken


def _layered(l2) -> LayeredCircuitBreakerStateRepository:
    repo = LayeredCircuitBreakerStateRepository(l2_repo=l2, adapter_type="redis")
    repo._l2_healthy = True
    repo._l2_consecutive_failures = 0
    return repo


def _l1_state(repo, service: str, state: str) -> None:
    repo._l1.get_or_create(service)
    repo._l1.update_state(service_name=service, state=state)


@pytest.fixture
def repo(mock_l2_repo):
    """Layered repo over a mock L2, for the seams that drive L2's answers."""
    r = _layered(mock_l2_repo)
    mock_l2_repo.reset_mock()
    return r


@pytest.fixture
def real_l2() -> InMemoryCircuitBreakerStateRepository:
    """A real in-process L2 — the reads and writes are the ones under test."""
    return InMemoryCircuitBreakerStateRepository()


# =============================================================================
# Detection — the request-path tuple compare
# =============================================================================


class TestRejectConvergenceDetectionBehavior:
    """``_maybe_schedule_reject_convergence`` — shape, ordering, isolation."""

    @pytest.mark.parametrize(
        ("allowed", "new_state"),
        [
            (True, CLOSED),
            (False, HALF_OPEN),
            (False, OPEN),
        ],
        ids=[
            "negative_allowed_acquire",
            "negative_rejected_with_half_open_answer",
            "negative_rejected_with_open_answer",
        ],
    )
    def test_negative_shapes_schedule_nothing(self, repo, allowed, new_state):
        """Only a rejected ``closed`` answer can be a contradiction; every
        other tuple shape is an ordinary decision and stops before the lane.

        The ``half_open`` rejection in particular is a trial already in
        progress, which resolves itself when the trial closes or re-opens.
        """
        _l1_state(repo, "svc", OPEN)

        with patch.object(repo, "_submit_reject_convergence") as mock_submit:
            repo._maybe_schedule_reject_convergence("svc", allowed, new_state)

        mock_submit.assert_not_called()

    @pytest.mark.parametrize(
        "l1_state",
        [None, CLOSED],
        ids=["negative_no_local_row", "negative_layers_agree_on_closed"],
    )
    def test_l1_negative_confirm_submits_nothing_and_returns_the_permit(
        self, repo, l1_state
    ):
        """The store-side half of detection runs behind the permit: a row
        that is absent or already closed is an ordinary decision, so nothing
        is submitted, the permit taken to check comes back, and the cooldown
        stays unconsumed.
        """
        if l1_state is not None:
            _l1_state(repo, "svc", l1_state)
        executor = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=executor):
            repo._maybe_schedule_reject_convergence("svc", False, CLOSED)

        assert executor.calls == []
        assert _available_permits() == MAX_IN_FLIGHT
        assert repo._should_schedule_reject_convergence("svc") is True

    def test_contradiction_hands_off_to_the_lane(self, repo):
        """The positive shape: rejected ``closed`` over a local ``half_open``."""
        _l1_state(repo, "svc", HALF_OPEN)

        with patch.object(repo, "_submit_reject_convergence") as mock_submit:
            repo._maybe_schedule_reject_convergence("svc", False, CLOSED)

        mock_submit.assert_called_once_with("svc")

    def test_suppressed_service_is_not_re_read_from_the_local_store(self, repo):
        """Ordering: the cooldown gate runs before the lock-taking L1 read.

        A rejected service at several hundred requests per second would
        otherwise pay an in-memory-store lock acquisition on every single
        request instead of once per cooldown window.
        """
        _l1_state(repo, "svc", HALF_OPEN)
        repo._reject_convergence_cooldown.try_reserve("svc", COOLDOWN_SECONDS)

        with patch.object(
            repo._l1, "get_by_service_name", wraps=repo._l1.get_by_service_name
        ) as wrapped_read:
            repo._maybe_schedule_reject_convergence("svc", False, CLOSED)

        wrapped_read.assert_not_called()

    def test_cap_full_detection_is_dropped_before_the_l1_read(self, repo):
        """Ordering: the in-flight permit gate also runs before the L1 read.

        A dropped detection leaves the cooldown unconsumed by design, so
        while the lane is at its bound every rejected request for a further
        stuck service re-enters the handler; without the permit-first
        ordering each of those requests would pay the store's lock for a
        detection that cannot schedule anyway.
        """
        _l1_state(repo, "svc", HALF_OPEN)
        for _ in range(MAX_IN_FLIGHT):
            assert repository_operations._reject_convergence_slots.acquire(
                blocking=False
            )

        with patch.object(
            repo._l1, "get_by_service_name", wraps=repo._l1.get_by_service_name
        ) as wrapped_read:
            repo._maybe_schedule_reject_convergence("svc", False, CLOSED)

        wrapped_read.assert_not_called()
        assert repo._should_schedule_reject_convergence("svc") is True

    def test_scheduling_failure_is_swallowed_and_logged_at_debug(self, repo):
        """Nothing in the handler may raise into the acquire."""
        _l1_state(repo, "svc", HALF_OPEN)

        with (
            patch.object(
                repo,
                "_submit_reject_convergence",
                side_effect=RuntimeError("executor is gone"),
            ),
            patch.object(repository_operations, "logger") as mock_logger,
        ):
            repo._maybe_schedule_reject_convergence("svc", False, CLOSED)

        assert any(
            call.args
            and call.args[0] == "layered_repo.reject_path_convergence_schedule_failed"
            for call in mock_logger.debug.call_args_list
        )

    def test_isolation_of_a_failed_schedule_preserves_the_l2_rejection(self, real_l2):
        """A scheduling failure must not become an admission.

        Placed bare inside the acquire's ``try``, a submit-time ``RuntimeError``
        would be caught by the L2 error handler, fall through to the L1
        fallback — which transitions a timed-out OPEN row into a trial and
        admits — and tick the consecutive-failure count toward a quarantine L2
        never earned.
        """
        # Given — the cluster is closed, this worker still holds open
        real_l2.get_or_create("svc")
        repo = _layered(real_l2)
        _l1_state(repo, "svc", OPEN)
        executor = _InlineExecutor(raise_for=repo._run_reject_path_convergence)

        # When
        with (
            patch.object(repo, "_get_executor", return_value=executor),
            patch.object(repo, "_record_half_open_degraded_mode") as mock_degraded,
        ):
            decision = repo.try_acquire_half_open_slot(
                service_name="svc", limit=3, stuck_timeout_seconds=60
            )

        # Then — the answer is still L2's rejection, not a local admission
        assert decision == (False, CLOSED, CLOSED)
        mock_degraded.assert_not_called()
        assert repo._l2_consecutive_failures == 0
        assert repo._l2_healthy is True


# =============================================================================
# Pacing
# =============================================================================


class TestRejectConvergenceThrottleBehavior:
    """``_should_schedule_reject_convergence`` — the lock-free pre-check."""

    @pytest.fixture
    def clock(self) -> _FakeClock:
        return _FakeClock()

    @pytest.fixture
    def paced_repo(self, repo, clock):
        repo._reject_convergence_cooldown = CooldownGate(clock=clock)
        return repo

    def test_first_detection_is_not_suppressed(self, paced_repo):
        """Nothing is paced until something has fired."""
        assert paced_repo._should_schedule_reject_convergence("svc") is True

    @pytest.mark.parametrize(
        ("elapsed", "expected"),
        [
            (0.0, False),
            (COOLDOWN_SECONDS - 0.001, False),
            (COOLDOWN_SECONDS, True),
            (COOLDOWN_SECONDS + 1.0, True),
        ],
        ids=["immediately_after", "just_inside", "at_the_edge", "past_the_edge"],
    )
    def test_retry_is_paced_until_the_cooldown_edge(
        self, paced_repo, clock, elapsed, expected
    ):
        """Boundary: suppression ends *at* the cooldown, not after it."""
        paced_repo._reject_convergence_cooldown.try_reserve("svc", COOLDOWN_SECONDS)

        clock.now += elapsed

        assert paced_repo._should_schedule_reject_convergence("svc") is expected

    def test_pacing_is_per_service(self, paced_repo):
        """One stuck service must not stop another from converging."""
        paced_repo._reject_convergence_cooldown.try_reserve("svc-a", COOLDOWN_SECONDS)

        assert paced_repo._should_schedule_reject_convergence("svc-a") is False
        assert paced_repo._should_schedule_reject_convergence("svc-b") is True


# =============================================================================
# In-flight permit accounting
# =============================================================================


class TestRejectConvergencePermitBehavior:
    """``_submit_reject_convergence`` — a permit per task, released once."""

    def test_submit_takes_a_permit_and_the_done_callback_returns_it(self, repo):
        _l1_state(repo, "svc", HALF_OPEN)
        executor = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=executor):
            repo._submit_reject_convergence("svc")

        assert len(executor.calls) == 1
        assert _available_permits() == MAX_IN_FLIGHT

    def test_cancelled_task_returns_its_permit(self, repo):
        """Executor shutdown cancels queued futures; the callback still fires.

        Without a release on cancellation the permit is gone for the life of
        the process, with no metric, log, or retry to show for it — two of
        those and the lane is silently off.
        """
        # Given — a contradiction and a submit whose future is never run
        _l1_state(repo, "svc", HALF_OPEN)
        pending: Future = Future()
        executor = MagicMock(spec=ThreadPoolExecutor)
        executor.submit.return_value = pending

        with patch.object(repo, "_get_executor", return_value=executor):
            repo._submit_reject_convergence("svc")
        assert _available_permits() == MAX_IN_FLIGHT - 1

        # When — the queued task is cancelled at shutdown
        assert pending.cancel() is True

        # Then
        assert _available_permits() == MAX_IN_FLIGHT

    def test_permit_is_restored_when_the_submit_itself_raises(self, repo):
        _l1_state(repo, "svc", HALF_OPEN)
        executor = MagicMock(spec=ThreadPoolExecutor)
        executor.submit.side_effect = RuntimeError(
            "cannot schedule new futures after shutdown"
        )

        with (
            patch.object(repo, "_get_executor", return_value=executor),
            pytest.raises(RuntimeError),
        ):
            repo._submit_reject_convergence("svc")

        assert _available_permits() == MAX_IN_FLIGHT

    def test_two_further_detections_still_schedule_after_a_leak_would_have(self, repo):
        """The permit-accounting regression, stated as the lane staying alive.

        A cancelled task and a raising submit are exactly the two paths that
        never reach a running task body; if either leaked, these two later
        detections would find the semaphore empty and drop silently.
        """
        # Given — one cancelled task and one raising submit
        for service in ("svc-a", "svc-b", "svc-c", "svc-d"):
            _l1_state(repo, service, HALF_OPEN)
        pending: Future = Future()
        cancelling_executor = MagicMock(spec=ThreadPoolExecutor)
        cancelling_executor.submit.return_value = pending
        raising_executor = MagicMock(spec=ThreadPoolExecutor)
        raising_executor.submit.side_effect = RuntimeError("shut down")

        with patch.object(repo, "_get_executor", return_value=cancelling_executor):
            repo._submit_reject_convergence("svc-a")
        pending.cancel()
        with (
            patch.object(repo, "_get_executor", return_value=raising_executor),
            pytest.raises(RuntimeError),
        ):
            repo._submit_reject_convergence("svc-b")

        # When
        executor = _InlineExecutor()
        with patch.object(repo, "_get_executor", return_value=executor):
            repo._submit_reject_convergence("svc-c")
            repo._submit_reject_convergence("svc-d")

        # Then
        assert len(executor.calls) == 2

    def test_cap_full_drops_the_detection_without_consuming_the_cooldown(self, repo):
        """A deferred detection must be retried by the next rejected request,
        not made to wait out a window it never used.
        """
        # Given — every permit is held
        for _ in range(MAX_IN_FLIGHT):
            assert repository_operations._reject_convergence_slots.acquire(
                blocking=False
            )
        executor = _InlineExecutor()

        # When
        with patch.object(repo, "_get_executor", return_value=executor):
            repo._submit_reject_convergence("svc")

        # Then
        assert executor.calls == []
        assert repo._should_schedule_reject_convergence("svc") is True

    def test_submit_consumes_the_cooldown_so_concurrent_detections_collapse(self, repo):
        """The reservation is what makes two detections resolve to one task."""
        _l1_state(repo, "svc", HALF_OPEN)
        executor = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=executor):
            repo._submit_reject_convergence("svc")
            repo._submit_reject_convergence("svc")

        assert len(executor.calls) == 1
        assert repo._should_schedule_reject_convergence("svc") is False

    def test_reservation_survives_a_submit_failure(self, repo):
        """Deliberate: an executor that rejects a submit keeps rejecting, and
        the cooldown is what stops a rejected service retrying once a request.
        """
        _l1_state(repo, "svc", HALF_OPEN)
        executor = MagicMock(spec=ThreadPoolExecutor)
        executor.submit.side_effect = RuntimeError("shut down")

        with (
            patch.object(repo, "_get_executor", return_value=executor),
            pytest.raises(RuntimeError),
        ):
            repo._submit_reject_convergence("svc")

        assert repo._should_schedule_reject_convergence("svc") is False


# =============================================================================
# Resolution — direction, outcomes, and the guards that decline to act
# =============================================================================


class TestRejectPathConvergenceOutcomeBehavior:
    """``_run_reject_path_convergence`` — one outcome per branch."""

    def test_absent_l2_reports_skipped(self, repo):
        repo._l2 = None

        assert repo._run_reject_path_convergence("svc") == "skipped"

    def test_quarantined_l2_reports_skipped_without_reading(self, repo, mock_l2_repo):
        """A degraded-mode local trial must not be clobbered by a stale view."""
        repo._l2_healthy = False

        assert repo._run_reject_path_convergence("svc") == "skipped"
        mock_l2_repo.get_by_service_name.assert_not_called()

    def test_backend_already_degraded_reports_skipped_without_reading(self, real_l2):
        real_l2._backend = _DegradingBackend(healthy_reads=0)
        repo = _layered(real_l2)

        with patch.object(repo, "_repair_row_to_l2_inline") as mock_repair:
            assert repo._run_reject_path_convergence("svc") == "skipped"

        mock_repair.assert_not_called()

    def test_backend_that_degrades_during_the_read_reports_skipped(self, real_l2):
        """The lying read, caught deterministically rather than acted on.

        A failed read is answered from the process-local fallback — usually
        empty, so the row "looks" missing — and the backend flips to degraded
        before returning. Repairing against that false absence would write a
        fabricated default row into the write-ahead log, whose replay can
        erase a peer's pin once the store comes back.
        """
        # Given — healthy at the pre-check, degraded by the post-read probe
        real_l2._backend = _DegradingBackend(healthy_reads=1)
        repo = _layered(real_l2)
        _l1_state(repo, "svc", HALF_OPEN)

        # When
        with patch.object(repo, "_repair_row_to_l2_inline") as mock_repair:
            outcome = repo._run_reject_path_convergence("svc")

        # Then
        assert outcome == "skipped"
        mock_repair.assert_not_called()
        assert real_l2.get_by_service_name("svc") is None

    def test_read_failure_reports_skipped_and_counts_toward_quarantine(
        self, repo, mock_l2_repo
    ):
        """A store that raises is a real L2 failure and should be accounted."""
        mock_l2_repo.get_by_service_name.side_effect = ConnectionError("redis down")

        with patch.object(repo, "_handle_l2_error") as mock_error:
            outcome = repo._run_reject_path_convergence("svc")

        assert outcome == "skipped"
        mock_error.assert_called_once()
        assert mock_error.call_args.args[0] == "reject_path_convergence"

    @pytest.mark.parametrize(
        ("repair_result", "expected"),
        [
            (True, "repaired"),
            (False, "repair_failed"),
            (None, "skipped_pinned"),
        ],
        ids=["repaired", "repair_failed", "skipped_pinned"],
    )
    def test_missing_remote_row_reports_the_repair_tri_state(
        self, repo, mock_l2_repo, repair_result, expected
    ):
        """A skip is not a failure and must not be reported as one."""
        mock_l2_repo.get_by_service_name.return_value = None

        with patch.object(
            repo, "_repair_row_to_l2_inline", return_value=repair_result
        ) as mock_repair:
            outcome = repo._run_reject_path_convergence("svc")

        assert outcome == expected
        mock_repair.assert_called_once_with("svc")

    @pytest.mark.parametrize(
        ("remote_state", "expected"),
        [
            (CLOSED, "converged"),
            (OPEN, "noop"),
            (HALF_OPEN, "noop"),
        ],
        ids=["closed_converges", "open_is_noop", "half_open_is_noop"],
    )
    def test_present_remote_row_converges_only_on_a_closed_cluster(
        self, real_l2, remote_state, expected
    ):
        """Trust is extended to a healthy ``closed`` answer and nothing else.

        Any other remote state means the two layers no longer disagree in the
        way the acquire reported, so the lane leaves both alone.
        """
        repo = _layered(real_l2)
        _l1_state(repo, "svc", HALF_OPEN)
        real_l2.get_or_create("svc")
        real_l2.update_state(service_name="svc", state=remote_state)

        outcome = repo._run_reject_path_convergence("svc")

        assert outcome == expected
        expected_l1 = CLOSED if expected == "converged" else HALF_OPEN
        assert repo._l1.get_by_service_name("svc").state == expected_l1

    def test_pinned_remote_row_is_delivered_whole_rather_than_state_only(self, real_l2):
        """Copying the state alone would leave this worker unpinned and free to
        record outcomes, re-trip, and mirror an OPEN over the operator's
        still-active decision.
        """
        # Given — this worker has no pin of its own; the operator's Allow was
        # placed elsewhere after this worker booted (so the remote pin must
        # arrive through the lane, not through the construction-time load)
        repo = _layered(real_l2)
        _l1_state(repo, "svc", HALF_OPEN)
        real_l2.set_manual_control(
            service_name="svc",
            state=CLOSED,
            controlled_by_id=99,
            reason="remote operator allow",
        )

        # When
        outcome = repo._run_reject_path_convergence("svc")

        # Then
        assert outcome == "converged"
        l1_row = repo._l1.get_by_service_name("svc")
        assert l1_row.state == CLOSED
        assert l1_row.manually_controlled is True
        assert l1_row.controlled_by_id == 99
        assert l1_row.control_reason == "remote operator allow"

    @pytest.mark.parametrize(
        "remote_pinned",
        [False, True],
        ids=["unpinned_remote_row", "pinned_remote_row"],
    )
    def test_active_local_pin_makes_both_write_branches_skip(
        self, real_l2, remote_pinned
    ):
        """Whichever branch the remote row routes to, a local override wins.

        The local operator op already wrote its pin through to the shared
        store synchronously, so the task's remote read predates it.
        """
        repo = _layered(real_l2)
        repo._l1.set_manual_control(
            service_name="svc", state=OPEN, reason="local operator block"
        )
        if remote_pinned:
            real_l2.set_manual_control(
                service_name="svc", state=CLOSED, reason="remote allow"
            )
        else:
            real_l2.get_or_create("svc")

        outcome = repo._run_reject_path_convergence("svc")

        assert outcome == "skipped_pinned"
        l1_row = repo._l1.get_by_service_name("svc")
        assert l1_row.state == OPEN
        assert l1_row.control_reason == "local operator block"

    def test_local_write_failure_reports_skipped(self, real_l2):
        """A raising L1 write is logged and swallowed, never re-raised."""
        real_l2.get_or_create("svc")
        repo = _layered(real_l2)
        _l1_state(repo, "svc", HALF_OPEN)

        with (
            patch.object(
                repo._l1,
                "converge_to_closed_unless_pinned",
                side_effect=MemoryError("simulated store failure"),
            ),
            patch.object(repository_operations, "logger") as mock_logger,
        ):
            outcome = repo._run_reject_path_convergence("svc")

        assert outcome == "skipped"
        assert any(
            call.args and call.args[0] == "layered_repo.reject_path_convergence_failed"
            for call in mock_logger.warning.call_args_list
        )


# =============================================================================
# Producer reproductions
# =============================================================================


class TestRejectPathConvergenceProducerBehavior:
    """The two reachable ways a worker ends up rejecting on a closed cluster."""

    def test_producer2_hydrated_half_open_converges_against_a_closed_cluster(
        self, real_l2
    ):
        """A worker whose L1 hydrated ``half_open`` at boot holds no trial slot
        of its own, so it records no outcomes; once a peer closes the cluster
        every acquire here is answered ``closed`` and rejected.

        Driven through the real acquire so the detection, the schedule and the
        task all run — the reject the lane must notice is the one the store
        actually produced, not one the test asserted into place.
        """
        # Given — the cluster is closed, this worker still holds a trial state
        real_l2.get_or_create("svc")
        repo = _layered(real_l2)
        repo._l1.hydrate_snapshot(
            CircuitBreakerStateData(service_name="svc", state=HALF_OPEN)
        )
        executor = _InlineExecutor()

        # When
        with patch.object(repo, "_get_executor", return_value=executor):
            decision = repo.try_acquire_half_open_slot(
                service_name="svc", limit=3, stuck_timeout_seconds=60
            )

        # Then — the rejection stands, and the contradiction is resolved
        assert decision == (False, CLOSED, CLOSED)
        assert executor.result_for(repo._run_reject_path_convergence) == "converged"
        assert repo._l1.get_by_service_name("svc").state == CLOSED

    def test_producer1_lost_remote_row_is_repaired_from_local_state(self, real_l2):
        """The shared row was lost in place — a flush, an eviction — while the
        connection stayed healthy, so the atomic acquire folded the missing
        hash into ``closed``. The task's own read tells that apart, and this
        worker's state is mirrored back rather than treated as a cluster close.
        """
        # Given — no remote row, a local trial state
        repo = _layered(real_l2)
        _l1_state(repo, "svc", HALF_OPEN)

        # When
        outcome = repo._run_reject_path_convergence("svc")

        # Then
        assert outcome == "repaired"
        restored = real_l2.get_by_service_name("svc")
        assert restored is not None
        assert restored.state == HALF_OPEN

    def test_producer1_with_a_pinned_local_row_writes_nothing(self, real_l2):
        """The repair opens with ``get_or_create``, whose default payload says
        "not manually controlled" — mirroring a pinned row after the durable
        one was lost would erase the operator's decision from the store.
        """
        repo = _layered(real_l2)
        repo._l1.set_manual_control(
            service_name="svc", state=OPEN, reason="operator block"
        )

        outcome = repo._run_reject_path_convergence("svc")

        assert outcome == "skipped_pinned"
        assert real_l2.get_by_service_name("svc") is None


# =============================================================================
# Observability
# =============================================================================


class TestRejectPathConvergenceObservabilityBehavior:
    """The counter and the log events the operator sees."""

    def test_applied_outcome_logs_at_info_with_the_outcome(self, real_l2):
        real_l2.get_or_create("svc")
        repo = _layered(real_l2)
        _l1_state(repo, "svc", HALF_OPEN)

        with patch.object(repository_operations, "logger") as mock_logger:
            repo._run_reject_path_convergence("svc")

        info_calls = [
            call
            for call in mock_logger.info.call_args_list
            if call.args and call.args[0] == APPLIED_EVENT
        ]
        assert len(info_calls) == 1
        assert info_calls[0].kwargs["outcome"] == "converged"

    def test_outcome_is_forwarded_to_the_convergence_counter(self, real_l2):
        real_l2.get_or_create("svc")
        repo = _layered(real_l2)
        _l1_state(repo, "svc", HALF_OPEN)

        with patch(
            "baldur.metrics.recorders.circuit_breaker.record_reject_path_convergence"
        ) as mock_record:
            repo._run_reject_path_convergence("svc")

        mock_record.assert_called_once_with("svc", "converged")

    def test_unavailable_metrics_module_does_not_break_the_task(self, real_l2):
        """The counter is best-effort; the convergence is not."""
        real_l2.get_or_create("svc")
        repo = _layered(real_l2)
        _l1_state(repo, "svc", HALF_OPEN)

        with patch.dict(
            "sys.modules", {"baldur.metrics.recorders.circuit_breaker": None}
        ):
            outcome = repo._run_reject_path_convergence("svc")

        assert outcome == "converged"
        assert repo._l1.get_by_service_name("svc").state == CLOSED


# =============================================================================
# Contract — the lane's spec values
# =============================================================================


class TestRejectPathConvergenceContract:
    """Hardcoded spec values: constants, outcome vocabulary, event names."""

    def test_cooldown_and_in_flight_constants(self):
        assert repository_operations._REJECT_CONVERGENCE_COOLDOWN_SECONDS == 30.0
        assert repository_operations._REJECT_CONVERGENCE_MAX_IN_FLIGHT == 2

    def test_in_flight_semaphore_is_bounded_by_the_constant(self):
        assert _available_permits() == MAX_IN_FLIGHT

    @pytest.mark.parametrize("outcome", sorted(OUTCOMES))
    def test_outcome_vocabulary_routes_to_its_documented_log_level(self, repo, outcome):
        """State transitions announce themselves; everything else stays at DEBUG.

        ``repair_failed``'s underlying cause is already reported at WARNING by
        the L2 error handlers, so the lane does not restate it.
        """
        with (
            patch.object(
                repo, "_resolve_reject_path_convergence", return_value=outcome
            ),
            patch.object(repository_operations, "logger") as mock_logger,
        ):
            assert repo._run_reject_path_convergence("svc") == outcome

        applied = [
            call
            for call in mock_logger.info.call_args_list
            if call.args and call.args[0] == APPLIED_EVENT
        ]
        noop = [
            call
            for call in mock_logger.debug.call_args_list
            if call.args and call.args[0] == NOOP_EVENT
        ]
        if outcome in APPLIED_OUTCOMES:
            assert (len(applied), len(noop)) == (1, 0)
        else:
            assert (len(applied), len(noop)) == (0, 1)

    @pytest.mark.parametrize("outcome", sorted(OUTCOMES))
    def test_every_outcome_reaches_the_counter(self, repo, outcome):
        with (
            patch.object(
                repo, "_resolve_reject_path_convergence", return_value=outcome
            ),
            patch(
                "baldur.metrics.recorders.circuit_breaker."
                "record_reject_path_convergence"
            ) as mock_record,
        ):
            repo._run_reject_path_convergence("svc")

        mock_record.assert_called_once_with("svc", outcome)
