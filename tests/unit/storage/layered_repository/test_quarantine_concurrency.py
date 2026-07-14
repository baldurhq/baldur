"""Concurrency correctness for the LayeredRepository L2 quarantine state (579).

The four quarantine fields — ``_l2_consecutive_failures``, ``_l2_healthy``,
``_l2_was_unhealthy``, ``_l2_last_error_time`` — are mutated from up to 16
``l2_sync`` executor threads. 579 atomizes the failure-count read-modify-write
and the healthy<->quarantined transition decision under ``self._lock`` and fires
the WARNING + audit + notification trio exactly once per transition without
holding the lock across any I/O. These tests assert, with real
``threading.Thread`` fan-out + mocks:

- **lost-increment (G1/D2)** — N x M concurrent failures total exactly N*M; a
  lockless regression undercounts under write-write contention.
- **quarantine-edge one-shot (G2/D3/D4)** — the *behavior change* the doc owns:
  ``_log_l2_failure_audit`` / ``_send_l2_failure_notification`` now fire exactly
  once on the healthy->quarantined edge instead of per-failure-past-threshold,
  both sequentially and under N concurrent threads, and the audit call carries
  the **captured edge value** (3), not a post-release re-read.
- **recovery-edge one-shot (G3/D3)** — the recovery audit / notification / drift
  trio fires once on the quarantined->healthy edge under concurrency.
- **consistent admin snapshot (G4/D6)** — ``get_l2_health`` / ``get_storage_info``
  never expose ``l2_healthy=True`` together with ``consecutive_failures >= 3``,
  including while ``reset_l2_health`` races the failure handlers.

The quarantine-entry **WARNING** one-shot (``layered_repo.l2_quarantined``) is
already covered by ``test_l2_sync_quarantine.py::TestL2QuarantineEntryWarning``
via ``capture_logs``. This file deliberately proves the audit/notification
one-shot (the actual D4 change) through ``patch.object`` spies instead: spies are
scheduling-deterministic and do not depend on ``structlog.testing.capture_logs``,
which has a known module-``logger`` freeze residual under ``-n6`` (UNIT_TEST_
GUIDELINES §6.5.9). Re-adding a per-file ``structlog`` guard would also violate
the G34 fitness function.

Pure-OSS: imports only ``baldur`` code, no ``baldur_pro`` — the notification
path's ``baldur_pro`` import lives inside the spied/patched
``_send_l2_*_notification``, so this test never imports it. Stays in
``tests/`` with no ``requires_pro`` marker.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
)

# The quarantine threshold is the contract under test (hardcoded once here;
# mirrors error_handling.py ``_l2_consecutive_failures >= 3``).
QUARANTINE_THRESHOLD = 3


@pytest.fixture
def repo():
    """L1-only layered repo (redis timeout profile).

    ``l2_repo=None``: the quarantine handlers operate on the four state fields,
    the metric counters, and the (here patched-out) audit/notification
    side-effects only — no L2 store, executor, or initial load is needed to
    exercise the transitions. Warmup/drift are suppressed by the autouse package
    conftest fixture regardless.
    """
    return LayeredCircuitBreakerStateRepository(l2_repo=None, adapter_type="redis")


def _join_all(threads, *, budget=20.0):
    """Join every thread against one shared wall-clock budget; return live ones.

    A ``for t in threads: t.join(timeout=budget)`` loop has a worst case of
    ``len(threads) * budget`` — six writers at a 20s budget is 120s, exactly the
    pytest-timeout — so a *starved* (not deadlocked) finish on a loaded CI runner
    reads as a hard timeout instead of a fast, bounded assertion. Joining against
    a single shared deadline caps the total wait at one ``budget`` window
    regardless of thread count.
    """
    deadline = time.monotonic() + budget
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    return [t for t in threads if t.is_alive()]


def _run_concurrently(worker, n_threads, *, join_timeout=20.0):
    """Start ``n_threads`` running ``worker(barrier)`` together, join bounded.

    The ``Barrier`` releases every thread at once to maximize contention on the
    failure-count RMW and the transition decision. Joins share one wall-clock
    budget (see ``_join_all``) so a lock-ordering mistake surfaces as a fast,
    bounded CI failure instead of a hang (house convention — UNIT_TEST_
    GUIDELINES, test_composite_storage_thread_safety.py et al.).
    """
    barrier = threading.Barrier(n_threads, timeout=join_timeout)
    threads = [
        threading.Thread(target=worker, args=(barrier,)) for _ in range(n_threads)
    ]
    for t in threads:
        t.start()
    alive = _join_all(threads, budget=join_timeout)
    assert not alive, (
        f"{len(alive)}/{n_threads} thread(s) did not finish within "
        f"{join_timeout}s — possible deadlock on self._lock"
    )


class TestQuarantineLostIncrement:
    """G1/D2 — the failure-count RMW loses no increments under contention."""

    @pytest.mark.parametrize(
        ("n_threads", "m_failures"),
        [(4, 5_000), (8, 2_500)],
    )
    def test_concurrent_failures_lose_no_increments(self, repo, n_threads, m_failures):
        """N threads x M failures must total exactly N*M.

        ``_l2_consecutive_failures += 1`` runs unconditionally before the ``>= 3``
        check, so the counter grows monotonically past the threshold; under the
        lock the final value is exactly N*M. A lockless regression interleaves
        the read-modify-write and undercounts.
        """
        # Side-effects fire once on the edge but do real audit/notification I/O;
        # patch them to no-ops so the stress loop measures only the RMW.
        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True),
            patch.object(repo, "_send_l2_failure_notification", autospec=True),
        ):

            def worker(barrier):
                barrier.wait()
                for _ in range(m_failures):
                    repo._handle_l2_error("sync", "svc", Exception("boom"))

            _run_concurrently(worker, n_threads)

        assert repo._l2_consecutive_failures == n_threads * m_failures

    def test_state_invariant_holds_after_concurrent_quarantine(self, repo):
        """After concurrent failures past the threshold, ``healthy`` is False and
        the ``healthy=True ==> consecutive < 3`` invariant (D2) holds."""
        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True),
            patch.object(repo, "_send_l2_failure_notification", autospec=True),
        ):

            def worker(barrier):
                barrier.wait()
                for _ in range(1_000):
                    repo._handle_l2_error("sync", "svc", Exception("boom"))

            _run_concurrently(worker, 4)

        assert repo._l2_healthy is False
        # Invariant: healthy implies sub-threshold count (here healthy is False).
        assert not (
            repo._l2_healthy and repo._l2_consecutive_failures >= QUARANTINE_THRESHOLD
        )


class TestQuarantineEntrySingleFire:
    """G2/D3/D4 — failure-side audit + notification fire once on the edge."""

    def test_sequential_failures_past_threshold_fire_side_effects_once(self, repo):
        """The D4 behavior change: 5 failures fire audit + notification once each.

        Before 579 these sat one indent out of the ``was_healthy`` one-shot and
        fired on *every* failure >= 3 (operator-notification spam). After 579
        both sit under the ``should_fire`` guard, so they fire exactly once on
        the healthy->quarantined edge.
        """
        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True) as audit_spy,
            patch.object(
                repo, "_send_l2_failure_notification", autospec=True
            ) as notif_spy,
        ):
            for _ in range(5):
                repo._handle_l2_error("sync", "svc", Exception("boom"))

        assert audit_spy.call_count == 1
        assert notif_spy.call_count == 1
        assert repo._l2_healthy is False
        assert repo._l2_consecutive_failures == 5

    def test_failure_audit_reports_threshold_edge_value(self, repo):
        """The audit's ``consecutive_failures`` is the threshold edge value (3).

        Sequentially this does not yet distinguish a captured local from a
        post-release re-read (the audit fires synchronously on the 3rd call,
        when the counter is exactly 3 either way); that distinction is proven
        under concurrency by
        ``test_concurrent_failures_fire_side_effects_once``. This case pins the
        reported value to the edge and guards against an off-by-one or a 0/None.
        """
        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True) as audit_spy,
            patch.object(repo, "_send_l2_failure_notification", autospec=True),
        ):
            for _ in range(5):
                repo._handle_l2_error("sync", "svc", Exception("boom"))

        audit_spy.assert_called_once()
        assert (
            audit_spy.call_args.kwargs["consecutive_failures"] == QUARANTINE_THRESHOLD
        )

    def test_fourth_failure_does_not_refire_side_effects(self, repo):
        """Boundary — the 4th failure (already quarantined) re-fires nothing."""
        for _ in range(QUARANTINE_THRESHOLD):
            with (
                patch.object(repo, "_log_l2_failure_audit", autospec=True),
                patch.object(repo, "_send_l2_failure_notification", autospec=True),
            ):
                repo._handle_l2_error("sync", "svc", Exception("boom"))
        assert repo._l2_healthy is False

        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True) as audit_spy,
            patch.object(
                repo, "_send_l2_failure_notification", autospec=True
            ) as notif_spy,
        ):
            repo._handle_l2_error("sync", "svc", Exception("boom"))  # 4th

        audit_spy.assert_not_called()
        notif_spy.assert_not_called()

    def test_timeout_handler_fires_side_effects_once(self, repo):
        """The timeout handler shares the one-shot — fires audit + notification
        once on the edge, with the captured edge value."""
        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True) as audit_spy,
            patch.object(
                repo, "_send_l2_failure_notification", autospec=True
            ) as notif_spy,
        ):
            for _ in range(5):
                repo._handle_l2_timeout("sync", "svc")

        assert audit_spy.call_count == 1
        assert notif_spy.call_count == 1
        assert (
            audit_spy.call_args.kwargs["consecutive_failures"] == QUARANTINE_THRESHOLD
        )

    def test_concurrent_failures_fire_side_effects_once(self, repo):
        """G2 + D3 under concurrency — N threads driving the count past the
        threshold fire audit + notification exactly once, carrying the captured
        edge value (3) not a re-read.

        Single-fire is deterministic regardless of scheduling: ``should_fire`` is
        captured under the lock, so exactly the thread that increments the count
        to 3 observes ``_l2_healthy=True`` on the edge. That same thread captures
        ``consecutive=3``; by the time the audit actually fires (after release)
        the counter has climbed to ``n_threads * m_failures``. A post-release
        re-read regression would therefore pass that larger value — so asserting
        the audit's ``consecutive_failures`` is exactly the threshold proves the
        captured local is threaded through (D3).
        """
        n_threads, m_failures = 8, 50
        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True) as audit_spy,
            patch.object(
                repo, "_send_l2_failure_notification", autospec=True
            ) as notif_spy,
        ):

            def worker(barrier):
                barrier.wait()
                for _ in range(m_failures):
                    repo._handle_l2_error("sync", "svc", Exception("boom"))

            _run_concurrently(worker, n_threads)

        assert audit_spy.call_count == 1
        assert notif_spy.call_count == 1
        # D3: captured edge value, not the (much larger) post-release counter.
        assert (
            audit_spy.call_args.kwargs["consecutive_failures"] == QUARANTINE_THRESHOLD
        )
        assert (
            notif_spy.call_args.kwargs["consecutive_failures"] == QUARANTINE_THRESHOLD
        )
        assert repo._l2_consecutive_failures == n_threads * m_failures
        assert repo._l2_healthy is False


class TestQuarantineRecoverySingleFire:
    """G3/D3 — recovery side-effects fire once on the quarantined->healthy edge."""

    @staticmethod
    def _drive_into_quarantine(repo):
        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True),
            patch.object(repo, "_send_l2_failure_notification", autospec=True),
        ):
            for _ in range(QUARANTINE_THRESHOLD):
                repo._handle_l2_error("sync", "svc", Exception("boom"))
        assert repo._l2_healthy is False
        assert repo._l2_was_unhealthy is True

    def test_sequential_recovery_fires_side_effects_once(self, repo):
        """Several successes after quarantine fire the recovery trio once."""
        self._drive_into_quarantine(repo)

        # The autouse package conftest already replaces
        # _schedule_drift_reconciliation with a no-op MagicMock (drift
        # suppression); reuse it as the drift spy rather than re-patching it
        # (autospec over an already-mocked attr raises InvalidSpecError).
        drift_spy = repo._schedule_drift_reconciliation
        drift_spy.reset_mock()
        with (
            patch.object(repo, "_log_l2_recovery_audit", autospec=True) as audit_spy,
            patch.object(
                repo, "_send_l2_recovery_notification", autospec=True
            ) as notif_spy,
        ):
            for _ in range(5):
                repo._handle_l2_success(1.0)

        assert audit_spy.call_count == 1
        assert notif_spy.call_count == 1
        assert drift_spy.call_count == 1
        assert repo._l2_healthy is True
        assert repo._l2_was_unhealthy is False
        assert repo._l2_consecutive_failures == 0

    def test_concurrent_recovery_fires_side_effects_once(self, repo):
        """N concurrent successes after quarantine fire the recovery trio once.

        ``was_unhealthy`` is captured and ``_l2_was_unhealthy`` cleared in the
        same critical section, so only the first thread sees the edge.
        """
        self._drive_into_quarantine(repo)

        # Reuse the conftest drift-suppression mock as the spy (see the
        # sequential test for why it is not re-patched here).
        drift_spy = repo._schedule_drift_reconciliation
        drift_spy.reset_mock()
        n_threads = 8
        with (
            patch.object(repo, "_log_l2_recovery_audit", autospec=True) as audit_spy,
            patch.object(
                repo, "_send_l2_recovery_notification", autospec=True
            ) as notif_spy,
        ):

            def worker(barrier):
                barrier.wait()
                repo._handle_l2_success(1.0)

            _run_concurrently(worker, n_threads)

        assert audit_spy.call_count == 1
        assert notif_spy.call_count == 1
        assert drift_spy.call_count == 1
        assert repo._l2_healthy is True
        assert repo._l2_was_unhealthy is False


class TestQuarantineAdminReadSnapshot:
    """G4/D6 — multi-field admin reads observe a consistent four-field snapshot."""

    def test_get_l2_health_never_observes_healthy_with_quarantine_count(self, repo):
        """A reader looping on ``get_l2_health`` while failures run must never see
        ``healthy=True`` together with ``consecutive_failures >= 3``.

        The transition write sets ``_l2_healthy=False`` only inside
        ``if consecutive >= 3`` under the lock, and the read snapshots all four
        fields under the same lock — so ``(True, >=3)`` is unobservable.
        """
        # Stress knobs, not spec values: the healthy->quarantined transition is
        # one-shot (no reset here), so the torn-read window is the first few
        # failures — the reader loops thousands of times across it at this count.
        # Bounded so writers finish fast even under CI CPU starvation.
        n_writers, m_failures = 6, 500
        stop = threading.Event()
        violations: list[tuple[bool, int]] = []

        def writer():
            for _ in range(m_failures):
                repo._handle_l2_error("sync", "svc", Exception("boom"))

        def reader():
            while not stop.is_set():
                snap = repo.get_l2_health()
                if (
                    snap["healthy"]
                    and snap["consecutive_failures"] >= QUARANTINE_THRESHOLD
                ):
                    violations.append((snap["healthy"], snap["consecutive_failures"]))
                    return

        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True),
            patch.object(repo, "_send_l2_failure_notification", autospec=True),
        ):
            reader_thread = threading.Thread(target=reader)
            reader_thread.start()
            writers = [threading.Thread(target=writer) for _ in range(n_writers)]
            for t in writers:
                t.start()
            alive = _join_all(writers, budget=20.0)
            stop.set()
            alive += _join_all([reader_thread], budget=5.0)

        assert not alive, (
            f"thread(s) did not finish (possible lock starvation): {alive}"
        )
        assert not violations, (
            f"reader observed inconsistent (healthy, consecutive): {violations}"
        )

    def test_reset_racing_failures_preserves_health_count_invariant(self, repo):
        """D6 — ``reset_l2_health`` flips ``healthy=True`` AND ``consecutive=0``
        atomically, so a reset racing the failure handlers never lets a reader
        observe ``healthy=True`` with a stale ``consecutive >= 3``.

        Checks both admin reads (``get_l2_health`` exposes ``healthy`` /
        ``consecutive_failures``; ``get_storage_info`` exposes ``l2_healthy`` /
        ``l2_consecutive_failures``).
        """
        # Stress knob, not a spec value: reset<->fail races flip the edge many
        # times, so hundreds of iterations already exercise the snapshot torn
        # read; bounded so the reader-starved writers finish under CI load.
        m = 1_000
        stop = threading.Event()
        violations: list[str] = []

        def fail_writer():
            for _ in range(m):
                repo._handle_l2_error("sync", "svc", Exception("boom"))

        def reset_writer():
            for _ in range(m):
                repo.reset_l2_health()

        def reader():
            while not stop.is_set():
                health = repo.get_l2_health()
                if (
                    health["healthy"]
                    and health["consecutive_failures"] >= QUARANTINE_THRESHOLD
                ):
                    violations.append(
                        f"get_l2_health {health['healthy'], health['consecutive_failures']}"
                    )
                    return
                info = repo.get_storage_info()
                if (
                    info["l2_healthy"]
                    and info["l2_consecutive_failures"] >= QUARANTINE_THRESHOLD
                ):
                    violations.append(
                        f"get_storage_info {info['l2_healthy'], info['l2_consecutive_failures']}"
                    )
                    return

        with (
            patch.object(repo, "_log_l2_failure_audit", autospec=True),
            patch.object(repo, "_send_l2_failure_notification", autospec=True),
        ):
            reader_thread = threading.Thread(target=reader)
            reader_thread.start()
            writers = [
                threading.Thread(target=fail_writer),
                threading.Thread(target=fail_writer),
                threading.Thread(target=reset_writer),
                threading.Thread(target=reset_writer),
            ]
            for t in writers:
                t.start()
            alive = _join_all(writers, budget=20.0)
            stop.set()
            alive += _join_all([reader_thread], budget=5.0)

        assert not alive, (
            f"thread(s) did not finish (possible lock starvation): {alive}"
        )
        assert not violations, f"reader observed inconsistent snapshot(s): {violations}"


class TestAuditHelperContract:
    """D3 — ``_log_l2_failure_audit`` forwards the captured edge value unchanged."""

    def test_log_l2_failure_audit_forwards_consecutive_failures(self, repo):
        """The ``consecutive_failures`` parameter (no longer a self-attribute
        re-read) reaches ``log_storage_failure_audit`` verbatim alongside the
        fixed ``storage_type='l2'`` / ``adapter_type`` arguments."""
        with patch(
            "baldur.adapters.memory.layered_repository.audit_helpers."
            "log_storage_failure_audit",
            autospec=True,
        ) as mock_audit:
            repo._log_l2_failure_audit(
                operation="sync",
                service_name="svc",
                error_type="TimeoutError",
                error_message="L2 timeout after 3 consecutive failures",
                consecutive_failures=3,
            )

        mock_audit.assert_called_once_with(
            storage_type="l2",
            adapter_type="redis",
            operation="sync",
            service_name="svc",
            error_type="TimeoutError",
            error_message="L2 timeout after 3 consecutive failures",
            consecutive_failures=3,
        )
