"""
Meta-watchdog unit test isolation (autouse fixtures).

Rationale:
    HealthProbeManager starts a daemon worker thread that runs `probe_all()`
    in a loop; `probe_all()` creates a short-lived ThreadPoolExecutor per
    probe. When a test constructs a manager (directly or indirectly via
    SelfHealerWatchdog) and calls `start()` without a matching `stop()`, the
    worker keeps running. During interpreter shutdown it races the
    ThreadPoolExecutor and logs `cannot schedule new futures after
    interpreter shutdown`. More importantly, the leaked worker may still
    hold references to module-level probes that other tests are mocking,
    producing order-dependent flakes.

    Instance tracking via `weakref.WeakSet` on `__init__` lets the teardown
    fixture call `stop()` on every alive manager without keeping them alive.

Reference:
    the unit test guidelines §6.5 (xdist parallel isolation).
"""

from __future__ import annotations

import weakref
from typing import Any

import pytest

_LIVE_MANAGERS: weakref.WeakSet[Any] = weakref.WeakSet()


@pytest.fixture(autouse=True)
def _track_and_stop_health_probe_managers(monkeypatch):
    """Track every HealthProbeManager instance and stop it after the test.

    The fixture monkeypatches `__init__` so the tracking wrapper registers
    each new instance in a module-level WeakSet. On teardown, any instance
    that is still running has `stop()` invoked so its worker thread exits
    deterministically before the next test runs.
    """
    from baldur.meta.health_probe import HealthProbeManager

    original_init = HealthProbeManager.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        _LIVE_MANAGERS.add(self)

    monkeypatch.setattr(HealthProbeManager, "__init__", tracking_init)

    yield

    # Snapshot before iterating — stop() mutates attributes that may affect GC.
    for manager in list(_LIVE_MANAGERS):
        try:
            if manager.is_running():
                manager.stop()
        except Exception:  # noqa: BLE001
            # Teardown must never fail — worst case the worker dies via
            # daemon-thread cleanup. Swallowing here is intentional.
            pass


@pytest.fixture(autouse=True)
def _reset_stuck_detector():
    """Reset the StuckDetector singleton before and after every meta test.

    DLQProbe.probe() records each tick's pending_count into the
    get_stuck_detector() singleton, whose per-component sliding window
    accumulates samples across ticks. The root-conftest singleton reset runs
    only at module scope — too coarse to isolate within-file accumulation
    tests that drive >=5 probe() cycles — and the function-scope
    auto_reset_all_state does not cover this detector. A per-function reset is
    safe: the detector has no other production consumer, so resetting it can
    never perturb an unrelated test (the standalone test_stuck_detector.py
    already resets explicitly, so the extra reset is idempotent there).
    """
    from baldur.meta.stuck_detector import reset_stuck_detector

    reset_stuck_detector()
    yield
    reset_stuck_detector()
