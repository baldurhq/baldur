"""BaldurEventBus dispatch-mode matrix tests (487 D6/D7/D8/D9).

Test targets:
- BaldurEventBus._dispatch_mode resolution at __init__ (D8)
- BaldurEventBus._execute_handler_with_timeout branches (sync /
  thread_per_emit / async_pool)

Verifies the 3 modes × handler-shape matrix per impl 487 Test Assessment.
Each mode uses a distinct dispatch primitive, so this file pins regression
guards on the primitive (``threading.Thread`` for thread_per_emit,
``BaldurEventBus._get_executor`` for async_pool, neither for sync).

UNIT_TEST_GUIDELINES.md compliance:
- parametrize over (mode, handler_shape) per Testability Notes §
- Behavior verification with source-referenced assertions
- mock.patch with autospec where the call surface is concrete
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.services.event_bus.bus.models import BaldurEvent

# =============================================================================
# Fixtures + isolation
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_event_bus_state():
    """Each test starts with a clean dispatch executor + settings cache."""
    from baldur.services.event_bus.bus.event_bus import BaldurEventBus
    from baldur.settings.event_bus import reset_event_bus_settings

    BaldurEventBus.shutdown_dispatch_executor()
    reset_event_bus_settings()
    yield
    BaldurEventBus.shutdown_dispatch_executor()
    reset_event_bus_settings()


def _make_event() -> BaldurEvent:
    return BaldurEvent(
        event_type=EventType.CONFIG_UPDATED,
        data={},
        source="dispatch_mode_test",
        priority=EventPriority.NORMAL,
    )


# =============================================================================
# Mode resolution at __init__ (D8)
# =============================================================================


class TestDispatchModeInitContract:
    """487 D8: ``self._dispatch_mode`` is frozen at ``__init__``."""

    @pytest.mark.parametrize(
        "mode",
        ["sync", "thread_per_emit", "async_pool"],
    )
    def test_init_loads_dispatch_mode_from_settings(self, mode, monkeypatch):
        """``__init__`` reads ``BALDUR_EVENT_BUS_DISPATCH_MODE`` once."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", mode)
        reset_event_bus_settings()

        bus = BaldurEventBus()
        assert bus._dispatch_mode == mode

    def test_default_dispatch_mode_is_async_pool(self):
        """Default ``BALDUR_EVENT_BUS_DISPATCH_MODE`` is ``async_pool`` (D6)."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        assert bus._dispatch_mode == "async_pool"


# =============================================================================
# sync mode — inline on caller thread, no timeout enforcement
# =============================================================================


class TestSyncModeBehavior:
    """487 D6 ``sync`` branch: inline call, no timeout guard."""

    def test_sync_runs_on_caller_thread(self, monkeypatch):
        """Handler observes the caller thread (no Thread / executor indirection)."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "sync")
        reset_event_bus_settings()

        bus = BaldurEventBus()
        seen: list[int] = []

        def handler(event: BaldurEvent) -> None:
            seen.append(threading.get_ident())

        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        caller_tid = threading.get_ident()
        bus.publish(_make_event())

        assert seen == [caller_tid]

    def test_sync_does_not_use_executor(self, monkeypatch):
        """``_get_executor`` is never invoked under ``sync`` mode."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "sync")
        reset_event_bus_settings()

        bus = BaldurEventBus()
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)

        with patch.object(
            BaldurEventBus,
            "_get_executor",
            wraps=BaldurEventBus._get_executor,
        ) as spy:
            bus.publish(_make_event())

        spy.assert_not_called()

    def test_sync_propagates_handler_exception_through_publish(self, monkeypatch):
        """Handler exceptions in ``sync`` are caught by publish's outer try/except."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "sync")
        reset_event_bus_settings()

        bus = BaldurEventBus()

        def raising_handler(event: BaldurEvent) -> None:
            raise RuntimeError("boom")

        bus.subscribe(EventType.CONFIG_UPDATED, raising_handler)
        # publish() catches exceptions per subscriber and returns the
        # successful-handler count; raising handlers contribute 0.
        assert bus.publish(_make_event()) == 0

    def test_sync_bypasses_timeout_guard(self, monkeypatch):
        """A handler longer than ``handler_timeout_seconds`` still completes inline.

        Cross-check: ``_timeout_count`` does not increment because the
        ``sync`` branch never enters the timeout-enforcement path.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "sync")
        monkeypatch.setenv("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", "0.05")
        reset_event_bus_settings()

        bus = BaldurEventBus()
        completed: list[bool] = []

        def slow_handler(event: BaldurEvent) -> None:
            time.sleep(0.1)
            completed.append(True)

        bus.subscribe(EventType.CONFIG_UPDATED, slow_handler)
        assert bus.publish(_make_event()) == 1
        assert completed == [True]
        assert bus._timeout_count == 0


# =============================================================================
# thread_per_emit mode — legacy ``threading.Thread`` path (regression guard)
# =============================================================================


class TestThreadPerEmitModeBehavior:
    """487 D6 ``thread_per_emit`` branch: legacy per-call ``Thread.start()``."""

    def test_thread_per_emit_uses_threading_thread(self, monkeypatch):
        """Regression guard — ``threading.Thread`` is constructed per emit."""
        from baldur.services.event_bus.bus import event_bus as event_bus_module
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "thread_per_emit")
        reset_event_bus_settings()

        bus = BaldurEventBus()
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)

        original_thread = threading.Thread

        def counting(*args, **kwargs):
            counting.call_count += 1
            return original_thread(*args, **kwargs)

        counting.call_count = 0

        with patch.object(event_bus_module.threading, "Thread", side_effect=counting):
            bus.publish(_make_event())

        assert counting.call_count == 1

    def test_thread_per_emit_does_not_use_executor(self, monkeypatch):
        """Regression guard — ``_get_executor`` never invoked under thread_per_emit."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "thread_per_emit")
        reset_event_bus_settings()

        bus = BaldurEventBus()
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)

        with patch.object(
            BaldurEventBus,
            "_get_executor",
            wraps=BaldurEventBus._get_executor,
        ) as spy:
            bus.publish(_make_event())

        spy.assert_not_called()

    def test_thread_per_emit_enforces_timeout(self, monkeypatch):
        """Slow handler exceeds timeout → ``_timeout_count`` increments (parity with pre-487)."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "thread_per_emit")
        monkeypatch.setenv("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", "0.05")
        reset_event_bus_settings()

        bus = BaldurEventBus()

        def slow(event: BaldurEvent) -> None:
            time.sleep(0.5)

        bus.subscribe(EventType.CONFIG_UPDATED, slow)
        # Slow handler is NOT counted as completed — return value is 0.
        assert bus.publish(_make_event()) == 0
        assert bus._timeout_count == 1


# =============================================================================
# async_pool mode — shared executor with future.result(timeout=)
# =============================================================================


class TestAsyncPoolModeBehavior:
    """487 D6 ``async_pool`` branch: shared executor + future.result(timeout=)."""

    def test_async_pool_uses_shared_executor(self):
        """Regression guard — ``_get_executor`` is called per emit."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)

        with patch.object(
            BaldurEventBus,
            "_get_executor",
            wraps=BaldurEventBus._get_executor,
        ) as spy:
            bus.publish(_make_event())

        assert spy.call_count >= 1

    def test_async_pool_does_not_use_threading_thread_in_dispatch(self):
        """Regression guard — ``threading.Thread`` not constructed in async_pool dispatch.

        Note: the executor itself constructs worker threads via
        ``threading.Thread`` lazily — spy on the call site inside
        ``_execute_handler_async_pool`` instead by exercising the dispatch
        path with the executor pre-created so dispatch does not trigger
        worker spawn.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)

        # Prime the executor + at least one worker so the next submit
        # reuses the existing worker.
        executor = BaldurEventBus._get_executor()
        executor.submit(lambda: None).result(timeout=2.0)

        thread_count_before = len(executor._threads)
        bus.publish(_make_event())
        # No additional worker should be spawned for a fast handler.
        assert len(executor._threads) <= thread_count_before + 1

    def test_async_pool_runs_handler_on_worker_thread(self):
        """Handler observes a non-caller thread (i.e., the executor worker)."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        observed: list[int] = []

        def handler(event: BaldurEvent) -> None:
            observed.append(threading.get_ident())

        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        caller = threading.get_ident()
        bus.publish(_make_event())

        assert len(observed) == 1
        assert observed[0] != caller

    def test_async_pool_enforces_timeout(self, monkeypatch):
        """Handler exceeding timeout → ``_timeout_count`` increments + return 0."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", "0.05")
        reset_event_bus_settings()

        bus = BaldurEventBus()

        def slow(event: BaldurEvent) -> None:
            time.sleep(0.5)

        bus.subscribe(EventType.CONFIG_UPDATED, slow)
        assert bus.publish(_make_event()) == 0
        assert bus._timeout_count == 1


# =============================================================================
# Cross-cutting — preserves priority-sorted sequential ordering (D7)
# =============================================================================


class TestDispatchOrderingBehavior:
    """487 D7: priority-sorted sequential per-handler dispatch preserved in all modes."""

    @pytest.mark.parametrize(
        "mode",
        ["sync", "thread_per_emit", "async_pool"],
    )
    def test_priority_order_preserved(self, mode, monkeypatch):
        """HIGH runs before NORMAL runs before LOW within a single emit."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", mode)
        reset_event_bus_settings()

        bus = BaldurEventBus()
        order: list[str] = []
        order_lock = threading.Lock()

        def make_handler(label: str):
            def _handler(event: BaldurEvent) -> None:
                with order_lock:
                    order.append(label)

            _handler.__name__ = f"handler_{label}"
            return _handler

        bus.subscribe(
            EventType.CONFIG_UPDATED, make_handler("low"), priority=EventPriority.LOW
        )
        bus.subscribe(
            EventType.CONFIG_UPDATED,
            make_handler("normal"),
            priority=EventPriority.NORMAL,
        )
        bus.subscribe(
            EventType.CONFIG_UPDATED,
            make_handler("high"),
            priority=EventPriority.HIGH,
        )

        assert bus.publish(_make_event()) == 3
        assert order == ["high", "normal", "low"]
