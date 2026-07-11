"""Concurrency + reserve/rollback tests for the AsyncHealingLogger flush-error alert
cooldown (D4).

The cooldown moved from an unlocked global scalar to a per-alert-key map guarded by
the class lock, with the slot reserved *under the lock* before the (unlocked) send and
rolled back if the send fails. These tests pin the race-safety and the fail-open
rollback behavior.
"""

from __future__ import annotations

import threading
import time

import pytest

from baldur.utils.async_logger import AsyncHealingLogger, FlushErrorAlertConfig


@pytest.fixture
def alert_state():
    """Isolate the class-level alert cooldown state (xdist-safe teardown)."""
    cls = AsyncHealingLogger
    saved_last = dict(cls._last_alert_time)
    saved_config = cls._alert_config
    saved_ts = list(cls._error_timestamps)

    cls._last_alert_time = {}
    cls._error_timestamps.clear()
    yield cls

    cls._last_alert_time = saved_last
    cls._alert_config = saved_config
    cls._error_timestamps.clear()
    cls._error_timestamps.extend(saved_ts)


def _flood_recent_errors(cls, n: int) -> None:
    now = time.time()
    for _ in range(n):
        cls._error_timestamps.append(now)


class TestAlertCooldownConcurrencyBehavior:
    def test_single_send_per_window_under_threads(self, alert_state, monkeypatch):
        cls = alert_state
        cls._alert_config = FlushErrorAlertConfig(
            threshold_count=1, window_seconds=60.0, cooldown_seconds=300.0
        )
        _flood_recent_errors(cls, 5)

        sends: list[int] = []
        send_lock = threading.Lock()

        def _fake_send(inner_cls, error_count):
            with send_lock:
                sends.append(error_count)
            return True

        monkeypatch.setattr(cls, "_send_flush_error_alert", classmethod(_fake_send))

        threads = [
            threading.Thread(target=cls._check_and_send_alert) for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Reserve-under-lock: exactly one send despite 20 concurrent callers.
        assert len(sends) == 1

    def test_successful_send_starts_cooldown(self, alert_state, monkeypatch):
        cls = alert_state
        cls._alert_config = FlushErrorAlertConfig(
            threshold_count=1, window_seconds=60.0, cooldown_seconds=300.0
        )
        _flood_recent_errors(cls, 3)

        calls: list[int] = []
        monkeypatch.setattr(
            cls,
            "_send_flush_error_alert",
            classmethod(lambda inner_cls, c: calls.append(c) or True),
        )

        cls._check_and_send_alert()  # sends
        cls._check_and_send_alert()  # within cooldown -> suppressed
        assert len(calls) == 1

    def test_failed_send_rolls_back_cooldown_for_reattempt(
        self, alert_state, monkeypatch
    ):
        cls = alert_state
        cls._alert_config = FlushErrorAlertConfig(
            threshold_count=1, window_seconds=60.0, cooldown_seconds=300.0
        )
        _flood_recent_errors(cls, 3)

        calls: list[int] = []

        def _failing_send(inner_cls, c):
            calls.append(c)
            return False  # send failed -> reservation must roll back

        monkeypatch.setattr(cls, "_send_flush_error_alert", classmethod(_failing_send))

        cls._check_and_send_alert()  # reserves, send fails, rolls back
        cls._check_and_send_alert()  # cooldown was rolled back -> re-attempts
        assert len(calls) == 2

    def test_below_threshold_does_not_send(self, alert_state, monkeypatch):
        cls = alert_state
        cls._alert_config = FlushErrorAlertConfig(
            threshold_count=10, window_seconds=60.0, cooldown_seconds=300.0
        )
        _flood_recent_errors(cls, 3)  # below threshold_count=10

        calls: list[int] = []
        monkeypatch.setattr(
            cls,
            "_send_flush_error_alert",
            classmethod(lambda inner_cls, c: calls.append(c) or True),
        )

        cls._check_and_send_alert()
        assert calls == []
