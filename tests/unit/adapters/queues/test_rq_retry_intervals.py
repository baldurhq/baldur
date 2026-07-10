"""Behavior tests for RQTaskAdapter._get_retry_intervals (D3 site 4).

The method composes the canonical ExponentialBackoff and now honors
``TaskOptions.retry_jitter`` instead of documenting it as ignored. ``_get_retry_intervals``
is independent of ``self``, so it is exercised as an unbound call (no rq/redis needed).
"""

from __future__ import annotations

from baldur.adapters.queues.rq_adapter import RQTaskAdapter
from baldur.interfaces.task_queue import TaskOptions


def _intervals(options: TaskOptions) -> list[int]:
    return RQTaskAdapter._get_retry_intervals(None, options)


class TestRQRetryIntervals:
    def test_jitterless_curve_matches_celery_doubling(self):
        opts = TaskOptions(
            retry_backoff=True, max_retries=5, retry_backoff_max=600, retry_jitter=False
        )
        assert _intervals(opts) == [1, 2, 4, 8, 16]

    def test_cap_respected_jitterless(self):
        opts = TaskOptions(
            retry_backoff=True, max_retries=6, retry_backoff_max=10, retry_jitter=False
        )
        assert _intervals(opts) == [1, 2, 4, 8, 10, 10]

    def test_jitter_honored_stays_within_hard_cap(self):
        opts = TaskOptions(
            retry_backoff=True, max_retries=8, retry_backoff_max=20, retry_jitter=True
        )
        intervals = _intervals(opts)
        assert len(intervals) == 8
        assert all(isinstance(i, int) for i in intervals)
        # Canonical jitters after capping, so each interval is re-clamped to the cap.
        assert all(0 <= i <= 20 for i in intervals)

    def test_jitter_desynchronizes_across_enqueues(self):
        opts = TaskOptions(
            retry_backoff=True, max_retries=8, retry_backoff_max=600, retry_jitter=True
        )
        runs = {tuple(_intervals(opts)) for _ in range(20)}
        # Each enqueue draws its own jittered list -> the sequences differ.
        assert len(runs) > 1

    def test_fixed_interval_branch(self):
        opts = TaskOptions(retry_backoff=False, max_retries=3)
        assert _intervals(opts) == [1, 1, 1]
