"""DaemonWorkerHandle unit tests (impl 489 D4).

Test targets:
    - baldur.meta.daemon_worker.DaemonWorkerHandle dataclass
    - heartbeat() / observe_iteration() / record_crash() methods
    - __post_init__ validation (tick_interval OR threshold required)

Test Categories:
    A. Contract — defaults declared in impl 489 D4
    B. Behavior — heartbeat / observe_iteration / record_crash side effects
    C. Behavior — __post_init__ derivation + validation
    D. Contract — reset_after_fork drops inherited observations, keeps wiring
"""

from __future__ import annotations

import threading

import pytest

from baldur.meta.daemon_worker import DaemonWorkerHandle


def _dummy_thread() -> threading.Thread:
    """Construct a Thread that has never been started (is_alive=False)."""
    return threading.Thread(target=lambda: None, daemon=True)


# =============================================================================
# A. Contract — default field values from impl 489 D4
# =============================================================================


class TestDaemonWorkerHandleContract:
    """Default field values declared in impl 489 D4."""

    def test_restart_count_defaults_to_zero(self):
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle.restart_count == 0

    def test_restart_callback_defaults_to_none(self):
        """Non-respawnable by default (D4 + D7 — opt-in)."""
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle.restart_callback is None

    def test_processing_delay_provider_defaults_to_none(self):
        """Buffer-only metric — pure pollers leave this None."""
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle.processing_delay_provider is None

    def test_is_stopping_defaults_to_false(self):
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle.is_stopping is False

    def test_last_healthy_observed_at_defaults_to_none(self):
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle.last_healthy_observed_at is None

    def test_last_respawn_attempt_at_defaults_to_none(self):
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle.last_respawn_attempt_at is None

    def test_last_crash_reason_defaults_to_none(self):
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle.last_crash_reason is None

    def test_iteration_observer_defaults_to_none(self):
        """No observer wired until register_daemon_worker injects one."""
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle._iteration_duration_observer is None


# =============================================================================
# B. Behavior — __post_init__ staleness derivation + validation
# =============================================================================


class TestDaemonWorkerHandlePostInitBehavior:
    """``__post_init__`` derives or accepts ``staleness_threshold_seconds``."""

    def test_explicit_threshold_preserved(self):
        """When caller passes staleness explicitly, it is not derived from tick."""
        handle = DaemonWorkerHandle(
            thread=_dummy_thread(),
            tick_interval_seconds=1.0,
            staleness_threshold_seconds=42.0,
        )
        assert handle.staleness_threshold_seconds == 42.0

    def test_threshold_derived_from_tick_interval_via_settings(self):
        """When only tick_interval provided, threshold = tick * default_multiplier."""
        from baldur.settings.daemon_worker import get_daemon_worker_settings

        multiplier = get_daemon_worker_settings().default_staleness_multiplier
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=5.0)
        assert handle.staleness_threshold_seconds == 5.0 * multiplier

    def test_neither_field_raises_value_error(self):
        """``__post_init__`` rejects construction when both are None."""
        with pytest.raises(ValueError, match="tick_interval_seconds or"):
            DaemonWorkerHandle(thread=_dummy_thread())

    def test_threshold_only_construction_succeeds(self):
        """tick_interval is optional when threshold is given directly."""
        handle = DaemonWorkerHandle(
            thread=_dummy_thread(), staleness_threshold_seconds=10.0
        )
        assert handle.tick_interval_seconds is None
        assert handle.staleness_threshold_seconds == 10.0


# =============================================================================
# C. Behavior — heartbeat / observe_iteration / record_crash
# =============================================================================


class TestDaemonWorkerHandleBehavior:
    """``heartbeat`` / ``observe_iteration`` / ``record_crash`` side effects."""

    def test_heartbeat_advances_last_heartbeat_at(self, monkeypatch):
        """``heartbeat()`` writes the current ``time.monotonic()`` value."""
        # Pass last_heartbeat_at explicitly to bypass the default_factory
        # (which captures the real time.monotonic at dataclass construction
        # and is not affected by monkeypatching the module attribute).
        handle = DaemonWorkerHandle(
            thread=_dummy_thread(),
            tick_interval_seconds=1.0,
            last_heartbeat_at=1000.0,
        )
        assert handle.last_heartbeat_at == 1000.0

        # heartbeat() reads time.monotonic via the daemon_worker module's
        # attribute lookup, which IS patchable.
        monkeypatch.setattr("baldur.meta.daemon_worker.time.monotonic", lambda: 1005.5)
        handle.heartbeat()
        assert handle.last_heartbeat_at == 1005.5

    def test_observe_iteration_no_op_without_observer(self):
        """``observe_iteration(d)`` is a no-op when observer is unset."""
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle._iteration_duration_observer is None
        # Must not raise.
        handle.observe_iteration(0.5)

    def test_observe_iteration_forwards_to_injected_observer(self):
        """When observer is wired, ``observe_iteration(d)`` forwards exactly d."""
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        seen: list[float] = []
        handle._iteration_duration_observer = seen.append

        handle.observe_iteration(0.05)
        handle.observe_iteration(2.5)

        assert seen == [0.05, 2.5]

    def test_record_crash_populates_last_crash_reason_with_typename_and_msg(self):
        """``record_crash(exc)`` formats as ``f"{type(exc).__name__}: {exc}"``."""
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)

        handle.record_crash(ValueError("bad input"))

        assert handle.last_crash_reason == "ValueError: bad input"

    def test_record_crash_overwrites_previous_reason(self):
        """A subsequent crash record replaces the prior one."""
        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        handle.record_crash(RuntimeError("first"))
        handle.record_crash(KeyError("second"))

        assert handle.last_crash_reason == "KeyError: 'second'"


# =============================================================================
# D. Contract — reset_after_fork drops inherited observations
# =============================================================================


class TestDaemonWorkerHandleForkResetContract:
    """``reset_after_fork()`` drops the parent's observations, keeps identity.

    A forked child inherits this object through the component that owns it.
    The observations on it describe a run in another process: left as-is the
    child publishes the parent's respawn count and crash reason, and computes
    heartbeat age from a heartbeat no thread here ever made — which reads as a
    dead worker.
    """

    # Every field the reset must clear, with the value it must clear to.
    # ``last_heartbeat_at`` is excluded: it is re-stamped to "now", not zeroed.
    CLEARED_FIELDS = {
        "restart_count": 0,
        "last_healthy_observed_at": None,
        "last_respawn_attempt_at": None,
        "last_crash_reason": None,
        "is_stopping": False,
    }

    @staticmethod
    def _inherited_handle() -> DaemonWorkerHandle:
        """A handle carrying a full set of another process's observations."""
        handle = DaemonWorkerHandle(
            thread=_dummy_thread(),
            tick_interval_seconds=1.0,
            last_heartbeat_at=1000.0,
        )
        handle.restart_count = 4
        handle.last_healthy_observed_at = 111.0
        handle.last_respawn_attempt_at = 222.0
        handle.record_crash(RuntimeError("parent crash"))
        handle.is_stopping = True
        return handle

    def test_every_inherited_observation_field_is_cleared(self):
        handle = self._inherited_handle()

        handle.reset_after_fork()

        for name, expected in self.CLEARED_FIELDS.items():
            assert getattr(handle, name) == expected, name

    def test_heartbeat_is_restamped_to_now_not_zeroed(self, monkeypatch):
        """A zeroed heartbeat would make the staleness probe read the child's
        brand-new worker as maximally stale.
        """
        handle = self._inherited_handle()
        monkeypatch.setattr("baldur.meta.daemon_worker.time.monotonic", lambda: 5150.0)

        handle.reset_after_fork()

        assert handle.last_heartbeat_at == 5150.0

    def test_wiring_fields_survive_the_reset(self):
        """Identity is deliberately preserved: the registry entry and the
        respawn callback keep pointing at this object, so a respawn racing a
        post-fork start converges instead of chasing a stale entry.
        """
        thread = _dummy_thread()

        def callback() -> None:
            """Stand-in respawn callback — identity is all that is asserted."""

        def observer(duration_seconds: float) -> None:
            """Stand-in iteration observer."""

        handle = DaemonWorkerHandle(
            thread=thread,
            tick_interval_seconds=1.0,
            restart_callback=callback,
            staleness_threshold_seconds=42.0,
        )
        handle._iteration_duration_observer = observer

        handle.reset_after_fork()

        assert handle.thread is thread
        assert handle.restart_callback is callback
        assert handle._iteration_duration_observer is observer
        assert handle.tick_interval_seconds == 1.0
        assert handle.staleness_threshold_seconds == 42.0

    def test_reset_matches_a_freshly_constructed_handle(self):
        """Derived cross-check: after the reset every cleared field equals the
        construction default, so a field added to the dataclass without being
        added to the reset shows up here rather than as a stale value in a
        worker's published stats.
        """
        fresh = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        handle = self._inherited_handle()

        handle.reset_after_fork()

        for name in self.CLEARED_FIELDS:
            assert getattr(handle, name) == getattr(fresh, name), name
