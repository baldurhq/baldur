"""Concurrency safety of the BaseNotifyingTask alert cooldown.

Regression coverage for the unlocked check-then-act race: the shared
``CooldownGate`` ClassVar is shared across task instances and worker threads,
and the immediate-send path reserves the slot atomically before sending,
releases the reservation on a failed send, and evicts each entry by its own
stored window so keys with different cooldowns never shorten one another.

Test targets:
    - tasks.base.BaseNotifyingTask._on_post_execute: at-most-one send per
      cooldown window per key under concurrency; release-on-failure.
    - the shared CooldownGate reserve / release / per-window eviction driving
      that path.
"""

from __future__ import annotations

import threading

import pytest

from baldur.core.rate_limiting import CooldownGate
from baldur.tasks.base import BaseNotifyingTask
from baldur.tasks.notification_policy import NotificationPolicy, NotificationTiming

_COOLDOWN_SECONDS = 300


class _FakeClock:
    """Deterministic injectable clock; ``advance`` steps time forward."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


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
def cooldown_gate():
    """Swap a fresh fake-clock gate into the shared ClassVar (xdist isolation)."""
    saved = BaseNotifyingTask._alert_gate
    clock = _FakeClock()
    BaseNotifyingTask._alert_gate = CooldownGate(clock=clock)
    yield clock
    BaseNotifyingTask._alert_gate = saved


def _meaningful_result() -> dict:
    # count > 0 passes _has_meaningful_result, so only the cooldown gates the send.
    return {"count": 5}


class TestBaseNotifyingCooldownBehavior:
    """Reserve-under-lock closes the duplicate-page window; release reopens it."""

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

    def test_failed_send_releases_so_the_retry_can_send(self):
        """A failed send must not start a cooldown that suppresses the retry."""
        task = _RecordingTask(send_results=[False, True])

        task._on_post_execute(_meaningful_result())  # send fails, slot released
        task._on_post_execute(_meaningful_result())  # retry must go through

        assert len(task.sent) == 2
        # Only the delivered send records an audit entry.
        assert len(task.audited) == 1

    def test_release_is_token_conditional(self, cooldown_gate):
        """A stale release must not clobber a successor's live reservation."""
        gate = BaseNotifyingTask._alert_gate
        _, first_token = gate.try_reserve("k", _COOLDOWN_SECONDS)
        cooldown_gate.advance(_COOLDOWN_SECONDS + 1)  # first reservation expires
        _, second_token = gate.try_reserve("k", _COOLDOWN_SECONDS)  # successor

        assert gate.release("k", first_token) is False  # stale -> no-op
        assert gate.is_suppressed("k", _COOLDOWN_SECONDS) is True  # successor live
        assert gate.release("k", second_token) is True

    def test_expired_keys_are_evicted_on_reserve(self, cooldown_gate):
        """Reserving any key lazily drops other keys past their own window."""
        gate = BaseNotifyingTask._alert_gate
        gate.try_reserve("other_task:stale", _COOLDOWN_SECONDS)
        cooldown_gate.advance(_COOLDOWN_SECONDS + 1)  # stale now past its window

        task = _RecordingTask()
        task._on_post_execute(_meaningful_result())  # reserves, evicting stale

        assert "other_task:stale" not in gate.keys()

    def test_fresh_keys_survive_eviction(self):
        """Eviction only drops entries past the window; fresh entries stay."""
        gate = BaseNotifyingTask._alert_gate
        gate.try_reserve("other_task:fresh", _COOLDOWN_SECONDS)

        task = _RecordingTask()
        task._on_post_execute(_meaningful_result())

        assert "other_task:fresh" in gate.keys()

    def test_short_cooldown_reserve_preserves_long_cooldown_entry(self, cooldown_gate):
        """G6 regression: a short-cooldown reserve on the shared gate must not
        strip a different key's still-in-window long-cooldown entry."""
        gate = BaseNotifyingTask._alert_gate
        gate.try_reserve("long:key", 300.0)
        cooldown_gate.advance(10.0)  # long entry still well within its window

        gate.try_reserve("short:key", 5.0)  # eviction is per-entry, not per-call

        assert "long:key" in gate.keys()
        assert gate.is_suppressed("long:key", 300.0) is True

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
        assert f"{task.name}:default" not in BaseNotifyingTask._alert_gate.keys()
