"""BaldurEventBusDispatchShutdownHandler unit tests (487 D4).

Test targets:
- BaldurEventBusDispatchShutdownHandler — ShutdownHandler ABC contract
- ``on_drain_complete()`` drains the dispatch executor (wait=True)
- ``on_force_shutdown()`` cancels in-flight without waiting (wait=False)
- ``integrate_dispatch_with_shutdown_coordinator()`` factory returns
  the handler unconditionally
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clean_dispatch_executor():
    from baldur.services.event_bus.bus.event_bus import BaldurEventBus

    BaldurEventBus.shutdown_dispatch_executor()
    yield
    BaldurEventBus.shutdown_dispatch_executor()


# =============================================================================
# Contract — ShutdownHandler ABC implementation
# =============================================================================


class TestDispatchShutdownHandlerContract:
    """487 D4: handler implements all ``ShutdownHandler`` abstract methods."""

    def test_implements_shutdown_handler_interface(self):
        """Subclass of the ABC ``ShutdownHandler``."""
        from baldur.core.shutdown_coordinator import ShutdownHandler
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBusDispatchShutdownHandler,
        )

        handler = BaldurEventBusDispatchShutdownHandler()
        assert isinstance(handler, ShutdownHandler)

    def test_on_shutdown_start_is_noop(self):
        """``on_shutdown_start`` is a pass — does not raise (D4 contract)."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBusDispatchShutdownHandler,
        )

        BaldurEventBusDispatchShutdownHandler().on_shutdown_start()

    def test_is_drain_complete_returns_true_default(self):
        """``is_drain_complete`` inherits ABC default ``True`` (D4)."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBusDispatchShutdownHandler,
        )

        handler = BaldurEventBusDispatchShutdownHandler()
        assert handler.is_drain_complete() is True


# =============================================================================
# Behavior — drain + force-shutdown
# =============================================================================


class TestDispatchShutdownHandlerDrainBehavior:
    """487 D4: drain semantics on ``on_drain_complete``."""

    def test_on_drain_complete_calls_shutdown_dispatch_executor(self):
        """on_drain_complete() invokes ``BaldurEventBus.shutdown_dispatch_executor``."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBus,
            BaldurEventBusDispatchShutdownHandler,
        )

        with patch.object(
            BaldurEventBus, "shutdown_dispatch_executor", autospec=True
        ) as spy:
            BaldurEventBusDispatchShutdownHandler().on_drain_complete()

        spy.assert_called_once()

    def test_on_drain_complete_clears_classvar(self):
        """End-to-end: real drain clears ``BaldurEventBus._executor``."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBus,
            BaldurEventBusDispatchShutdownHandler,
        )

        BaldurEventBus._get_executor()
        assert BaldurEventBus._executor is not None

        BaldurEventBusDispatchShutdownHandler().on_drain_complete()
        assert BaldurEventBus._executor is None

    def test_on_drain_complete_drain_failure_does_not_raise(self):
        """``on_drain_complete`` swallows drain exceptions (fail-open)."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBus,
            BaldurEventBusDispatchShutdownHandler,
        )

        with patch.object(
            BaldurEventBus,
            "shutdown_dispatch_executor",
            side_effect=RuntimeError("simulated drain failure"),
        ):
            # Must not propagate the RuntimeError to the coordinator.
            BaldurEventBusDispatchShutdownHandler().on_drain_complete()


class TestDispatchShutdownHandlerForceBehavior:
    """487 D4: ``on_force_shutdown`` calls ``shutdown(wait=False)``."""

    def test_on_force_shutdown_clears_classvar(self):
        """End-to-end: ``on_force_shutdown`` nulls ``_executor``."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBus,
            BaldurEventBusDispatchShutdownHandler,
        )

        BaldurEventBus._get_executor()
        assert BaldurEventBus._executor is not None

        BaldurEventBusDispatchShutdownHandler().on_force_shutdown(pending_requests=[])
        assert BaldurEventBus._executor is None

    def test_on_force_shutdown_calls_shutdown_with_wait_false(self):
        """``on_force_shutdown`` calls ``executor.shutdown(wait=False)``."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBus,
            BaldurEventBusDispatchShutdownHandler,
        )

        # Wire in a mock executor we can inspect.
        mock_executor = ThreadPoolExecutor(max_workers=1)
        try:
            BaldurEventBus._executor = mock_executor

            with patch.object(mock_executor, "shutdown", autospec=True) as spy:
                BaldurEventBusDispatchShutdownHandler().on_force_shutdown(
                    pending_requests=[]
                )

            # ``shutdown`` was invoked with wait=False (positional or kw).
            assert spy.call_count == 1
            args, kwargs = spy.call_args
            assert kwargs.get("wait") is False or False in args
        finally:
            mock_executor.shutdown(wait=False)

    def test_on_force_shutdown_failure_does_not_raise(self):
        """Force-shutdown failures are swallowed (fail-open)."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBus,
            BaldurEventBusDispatchShutdownHandler,
        )

        mock_executor = ThreadPoolExecutor(max_workers=1)
        try:
            BaldurEventBus._executor = mock_executor

            with patch.object(
                mock_executor,
                "shutdown",
                side_effect=RuntimeError("simulated force shutdown failure"),
            ):
                # Must not propagate.
                BaldurEventBusDispatchShutdownHandler().on_force_shutdown(
                    pending_requests=[]
                )
        finally:
            BaldurEventBus._executor = None
            mock_executor.shutdown(wait=False)


# =============================================================================
# Factory — integrate_dispatch_with_shutdown_coordinator
# =============================================================================


class TestDispatchShutdownIntegrationFactoryBehavior:
    """487 D4: factory returns the handler unconditionally."""

    def test_factory_returns_handler_instance(self):
        """``integrate_dispatch_with_shutdown_coordinator()`` returns the handler."""
        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBusDispatchShutdownHandler,
            integrate_dispatch_with_shutdown_coordinator,
        )

        handler = integrate_dispatch_with_shutdown_coordinator()
        assert isinstance(handler, BaldurEventBusDispatchShutdownHandler)

    def test_factory_returns_independent_instances(self):
        """Each call returns a fresh handler (no implicit singleton)."""
        from baldur.services.event_bus.bus.event_bus import (
            integrate_dispatch_with_shutdown_coordinator,
        )

        a = integrate_dispatch_with_shutdown_coordinator()
        b = integrate_dispatch_with_shutdown_coordinator()
        assert a is not b


# =============================================================================
# Re-export — module-level shim for protect.py / bootstrap.py
# =============================================================================


class TestModuleLevelShimExportsBehavior:
    """487 D10: bus/__init__.py re-exports the handler + factory + shutdown alias."""

    def test_shutdown_dispatch_executor_re_exported(self):
        """``baldur.services.event_bus.bus.shutdown_dispatch_executor`` callable."""
        from baldur.services.event_bus.bus import shutdown_dispatch_executor

        assert callable(shutdown_dispatch_executor)

    def test_module_level_alias_drains_classvar(self):
        """Calling the module alias drains the executor."""
        from baldur.services.event_bus.bus import shutdown_dispatch_executor
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()
        assert BaldurEventBus._executor is not None
        shutdown_dispatch_executor()
        assert BaldurEventBus._executor is None

    def test_integrate_factory_re_exported(self):
        """``integrate_dispatch_with_shutdown_coordinator`` re-exported."""
        from baldur.services.event_bus.bus import (
            BaldurEventBusDispatchShutdownHandler,
            integrate_dispatch_with_shutdown_coordinator,
        )

        handler = integrate_dispatch_with_shutdown_coordinator()
        assert isinstance(handler, BaldurEventBusDispatchShutdownHandler)


# =============================================================================
# Concurrency safety — drain while emit is in-flight
# =============================================================================


class TestDispatchShutdownConcurrencyBehavior:
    """487 D4: drain blocks until in-flight handler tasks complete."""

    def test_drain_waits_for_in_flight_handler(self):
        """``on_drain_complete`` (wait=True) returns only after handlers finish."""
        import time

        from baldur.services.event_bus.bus.event_bus import (
            BaldurEventBus,
            BaldurEventBusDispatchShutdownHandler,
        )

        executor = BaldurEventBus._get_executor()
        completed = threading.Event()
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(0.1)
            completed.set()

        executor.submit(slow)
        assert started.wait(timeout=5.0)

        BaldurEventBusDispatchShutdownHandler().on_drain_complete()
        # ``wait=True`` guarantees the slow task ran to completion.
        assert completed.is_set()
        assert BaldurEventBus._executor is None
