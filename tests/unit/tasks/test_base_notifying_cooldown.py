"""Concurrency safety of the BaseNotifyingTask alert cooldown.

Regression coverage for the unlocked check-then-act race: ``_last_alert_times``
is a ClassVar dict shared across task instances and worker threads, and the
old flow checked the cooldown, sent, then recorded — so two threads could both
pass the check and both page for one event. The immediate-send path now
reserves the slot atomically under a class-level lock before sending, rolls
the reservation back on a failed send, and lazily evicts expired keys on every
reserve so the shared dict stays bounded.

Test targets:
    - tasks.base.BaseNotifyingTask._on_post_execute: at-most-one send per
      cooldown window per key under concurrency; rollback-on-failure.
    - _reserve_alert_slot / _rollback_alert_slot / _evict_expired_locked:
      reservation state transitions and lazy TTL eviction.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from baldur.tasks.base import BaseNotifyingTask
from baldur.tasks.notification_policy import NotificationPolicy, NotificationTiming
from baldur.utils.time import utc_now

_COOLDOWN_SECONDS = 300


class _RecordingTask(BaseNotifyingTask):
    """Immediate-send task whose notification sink records instead of paging.

    ``send_results`` scripts the outcome of successive sends (True = delivered,
    False = failed); it repeats its last value once exhausted.
    """

    name = "cooldown_probe_task"
    notification_policy = NotificationPolicy(
        timing=NotificationTiming.AFTER,
        aggregate=False,
        cooldown_seconds=_COOLDOWN_SECONDS,
        escalate_on_emergency=False,
    )

    def __init__(self, send_results: list[bool] | None = None):
        self.sent: list[dict] = []
        self.audited: list[dict] = []
        self._send_results = list(send_results or [True])
        self._send_lock = threading.Lock()

    def run(self, *args, **kwargs):
        return {"count": 1}

    def _send_notification(self, result):
        with self._send_lock:
            self.sent.append(result)
            if len(self._send_results) > 1:
                return self._send_results.pop(0)
            return self._send_results[0]

    def _record_audit_trail(self, result):
        self.audited.append(result)


@pytest.fixture(autouse=True)
def _isolate_cooldown_state():
    """Reset the shared ClassVar cooldown dict around each test (xdist isolation)."""
    BaseNotifyingTask._last_alert_times.clear()
    yield
    BaseNotifyingTask._last_alert_times.clear()


def _meaningful_result() -> dict:
    # count > 0 passes _has_meaningful_result, so only the cooldown gates the send.
    return {"count": 5}


class TestBaseNotifyingCooldownBehavior:
    """Reserve-under-lock closes the duplicate-page window; rollback reopens it."""

    def test_concurrent_post_execute_sends_at_most_one_alert(self):
        """N racing threads produce exactly one send for one alert key."""
        task = _RecordingTask()
        barrier = threading.Barrier(8)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait(timeout=5.0)
                task._on_post_execute(_meaningful_result())
            except Exception as e:  # pragma: no cover - failure diagnostics
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert errors == []
        assert len(task.sent) == 1

    def test_second_send_within_cooldown_is_suppressed(self):
        """A successful send starts the cooldown; the next event is suppressed."""
        task = _RecordingTask()

        task._on_post_execute(_meaningful_result())
        task._on_post_execute(_meaningful_result())

        assert len(task.sent) == 1

    def test_failed_send_rolls_back_so_the_retry_can_send(self):
        """A failed send must not start a cooldown that suppresses the retry."""
        task = _RecordingTask(send_results=[False, True])

        task._on_post_execute(_meaningful_result())  # send fails, slot rolled back
        task._on_post_execute(_meaningful_result())  # retry must go through

        assert len(task.sent) == 2
        # Only the delivered send records an audit entry.
        assert len(task.audited) == 1

    def test_rollback_restores_the_previous_expired_timestamp(self):
        """Rollback puts back the pre-reservation value, not an empty slot."""
        task = _RecordingTask(send_results=[False])
        alert_key = f"{task.name}:default"
        expired = utc_now() - timedelta(seconds=_COOLDOWN_SECONDS * 2)
        BaseNotifyingTask._last_alert_times[alert_key] = expired

        reserved, previous = task._reserve_alert_slot(alert_key)
        assert reserved is True
        task._rollback_alert_slot(alert_key, previous)

        # The reservation is undone; an expired previous no longer blocks a send.
        assert task._can_send_alert(alert_key) is True

    def test_expired_keys_are_evicted_on_reserve(self):
        """Reserving any key lazily drops other keys past their cooldown window."""
        task = _RecordingTask()
        stale_key = "other_task:stale"
        expired = utc_now() - timedelta(seconds=_COOLDOWN_SECONDS * 2)
        BaseNotifyingTask._last_alert_times[stale_key] = expired

        task._on_post_execute(_meaningful_result())

        assert stale_key not in BaseNotifyingTask._last_alert_times

    def test_fresh_keys_survive_eviction(self):
        """Eviction only drops entries past the window; fresh entries stay."""
        task = _RecordingTask()
        fresh_key = "other_task:fresh"
        BaseNotifyingTask._last_alert_times[fresh_key] = utc_now()

        task._on_post_execute(_meaningful_result())

        assert fresh_key in BaseNotifyingTask._last_alert_times

    def test_aggregated_path_does_not_consume_the_cooldown(self):
        """Aggregation must not reserve — every result lands in the daily report."""

        class _AggregatedTask(_RecordingTask):
            name = "cooldown_probe_aggregated_task"
            notification_policy = NotificationPolicy(
                timing=NotificationTiming.AGGREGATED,
                aggregate=True,
                cooldown_seconds=_COOLDOWN_SECONDS,
                escalate_on_emergency=False,
            )

            def __init__(self):
                super().__init__()
                self.reported: list[dict] = []

            def _add_to_daily_report(self, result):
                self.reported.append(result)

        task = _AggregatedTask()

        task._on_post_execute(_meaningful_result())
        task._on_post_execute(_meaningful_result())

        assert len(task.reported) == 2
        assert task.sent == []
        assert f"{task.name}:default" not in BaseNotifyingTask._last_alert_times
