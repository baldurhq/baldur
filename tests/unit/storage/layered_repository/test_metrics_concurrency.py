"""Concurrency correctness for LayeredRepository ``_metrics`` counters (577).

The local metric counters are mutated read-modify-write (``+=``) from up to 16
concurrent ``l2_sync`` executor threads. ``dict[k] += n`` is non-atomic under
the GIL, so unsynchronized increments lose updates. These tests drive
``_incr_metrics`` from many real threads and assert:

- N threads x M increments == N*M (zero lost updates), and
- a concurrent reader never observes a torn ``(latency_total_ms,
  latency_count)`` pair from the multi-field success-path helper.

Pure-OSS: imports only ``baldur`` code, no ``baldur_pro`` — stays in
``tests/`` with no ``requires_pro`` marker.
"""

from __future__ import annotations

import threading

import pytest

from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
)


@pytest.fixture
def repo(mock_l2_repo):
    """Layered repository with a mock L2 (warmup/drift suppressed by conftest).

    ``reset_metrics()`` zeroes the latency sample the construction-time initial
    load contributes, so the per-counter assertions below start from a clean
    ``_default_metrics()`` baseline.
    """
    r = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2_repo)
    r.reset_metrics()
    return r


def _run_threads(target, n_threads):
    """Start ``n_threads`` running ``target`` together, then join them all."""
    threads = [threading.Thread(target=target) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class TestMetricsIncrementConcurrency:
    @pytest.mark.parametrize(
        ("n_threads", "m_increments"),
        [(4, 10_000), (16, 5_000)],
    )
    def test_single_key_increments_lose_no_updates(self, repo, n_threads, m_increments):
        """N threads x M single-key increments must total exactly N*M."""
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()  # release together to maximize write-write contention
            for _ in range(m_increments):
                repo._incr_metrics(l2_timeout_count=1)

        _run_threads(worker, n_threads)

        assert repo.get_metrics()["l2_timeout_count"] == n_threads * m_increments

    def test_multi_field_increments_lose_no_updates(self, repo):
        """The 3-field success-path helper applies all deltas atomically."""
        n_threads, m_increments = 8, 5_000
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(m_increments):
                repo._incr_metrics(
                    l2_sync_success_count=1,
                    l2_latency_total_ms=1.0,
                    l2_latency_count=1,
                )

        _run_threads(worker, n_threads)

        total = n_threads * m_increments
        metrics = repo.get_metrics()
        assert metrics["l2_sync_success_count"] == total
        assert metrics["l2_latency_count"] == total
        assert metrics["l2_latency_total_ms"] == pytest.approx(float(total))


class TestMetricsSnapshotConsistency:
    def test_reader_never_observes_torn_latency_pair(self, repo):
        """A concurrent reader must always see ``total_ms == count``.

        Each success increment adds ``elapsed=1.0`` ms and ``count=1`` together,
        so any consistent snapshot satisfies ``latency_total_ms ==
        latency_count``. A torn read (one field updated, the other not) would
        break the invariant and corrupt the derived average in
        ``get_storage_info``. The multi-field helper holds the metrics lock
        across both writes, and ``get_metrics`` snapshots under the same lock,
        so the invariant must hold on every observation.
        """
        n_writers, m_increments = 8, 5_000
        stop = threading.Event()
        torn: list[tuple[float, int]] = []

        def writer():
            for _ in range(m_increments):
                repo._incr_metrics(
                    l2_sync_success_count=1,
                    l2_latency_total_ms=1.0,
                    l2_latency_count=1,
                )

        def reader():
            while not stop.is_set():
                snapshot = repo.get_metrics()
                total_ms = snapshot["l2_latency_total_ms"]
                count = snapshot["l2_latency_count"]
                if total_ms != float(count):
                    torn.append((total_ms, count))
                    return

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        _run_threads(writer, n_writers)
        stop.set()
        reader_thread.join()

        assert not torn, f"reader observed torn latency pair(s): {torn}"
        final = repo.get_metrics()
        assert final["l2_latency_total_ms"] == pytest.approx(
            float(final["l2_latency_count"])
        )
