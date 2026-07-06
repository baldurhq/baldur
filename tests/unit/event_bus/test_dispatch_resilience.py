"""BaldurEventBus async_pool dispatch resilience tests (487 D6/G6/G7).

Test targets (async_pool mode only — sync / thread_per_emit are
covered by ``test_dispatch_modes.py``):

- ContextVar propagation across the executor submit boundary (D5)
- Handler-exception path → ``event_bus.handler_failed`` log (G6)
- Per-handler timeout enforcement → ``event_bus.handler_timeout`` log
  + ``_timeout_count`` increment + ``future.cancel()`` (G7)
- Shutdown-window race — pre-shutdown executor → ``RuntimeError`` →
  inline fallback, no log noise (G7 / D6)
- Zombie-handler regression — saturated pool with timed-out handlers
  must not block subsequent emit's timeout path
- ``bus.publish().return == handlers_called`` contract preservation

Compliance:
- UNIT_TEST_GUIDELINES.md §8 (exception/timeout/concurrency techniques)
- All assertions exercise async_pool branch (default ``BALDUR_EVENT_BUS_DISPATCH_MODE``).
"""

from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.services.event_bus.bus.models import BaldurEvent

# =============================================================================
# Fixtures + isolation
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_event_bus_state():
    """Each test starts with cleared executor + settings cache."""
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
        source="dispatch_resilience_test",
        priority=EventPriority.NORMAL,
    )


# =============================================================================
# ContextVar propagation across submit boundary (D5)
# =============================================================================


class TestAsyncPoolContextPropagationBehavior:
    """487 D5: ``contextvars.copy_context()`` → handler observes caller binding."""

    def test_contextvar_propagated_to_worker_thread(self):
        """A ContextVar set in the caller is visible in the executor worker."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        var: contextvars.ContextVar[str] = contextvars.ContextVar("dispatch_test_var")
        var.set("caller-binding")

        bus = BaldurEventBus()
        observed: list[str] = []

        def handler(event: BaldurEvent) -> None:
            observed.append(var.get("MISSING"))

        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        bus.publish(_make_event())

        assert observed == ["caller-binding"]


# =============================================================================
# Handler exception path → publish() catches via outer try/except (G6)
# =============================================================================


class TestAsyncPoolHandlerExceptionBehavior:
    """487 G6: handler exception re-raised through ``future.result()``."""

    def test_raising_handler_does_not_count_as_called(self):
        """publish() return value excludes handlers that raised."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()

        def raising(event: BaldurEvent) -> None:
            raise ValueError("boom")

        bus.subscribe(EventType.CONFIG_UPDATED, raising)
        # Outer try/except catches the exception; return is 0.
        assert bus.publish(_make_event()) == 0
        # Timeout counter is NOT incremented for an exception path.
        assert bus._timeout_count == 0

    # 525 D4: xdist mock_leak — caplog capture races with sibling tests
    # under -n 6 (project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="mock_leak"
    )
    def test_handler_failed_log_emitted_on_exception(self, caplog):
        """``event_bus.handler_failed`` log line fires (D6 contract preservation)."""
        import logging

        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()

        def raising(event: BaldurEvent) -> None:
            raise ValueError("expected-test-error")

        bus.subscribe(EventType.CONFIG_UPDATED, raising)

        with caplog.at_level(logging.ERROR):
            bus.publish(_make_event())

        # structlog event keys are stringified into the message — match
        # on the literal event name from ``publish()``.
        assert any("event_bus.handler_failed" in rec.message for rec in caplog.records)

    def test_other_handlers_still_run_after_one_raises(self):
        """A raising handler does not stop sibling handlers."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        seen: list[str] = []

        def raising(event: BaldurEvent) -> None:
            raise RuntimeError("first")

        def good(event: BaldurEvent) -> None:
            seen.append("good")

        bus.subscribe(EventType.CONFIG_UPDATED, raising, priority=EventPriority.HIGH)
        bus.subscribe(EventType.CONFIG_UPDATED, good, priority=EventPriority.LOW)

        # Only ``good`` is counted (raising returns 0).
        assert bus.publish(_make_event()) == 1
        assert seen == ["good"]


# =============================================================================
# Timeout enforcement (G7)
# =============================================================================


class TestAsyncPoolTimeoutBehavior:
    """487 G7: ``future.result(timeout=)`` enforces per-handler timeout."""

    # 525 D4: xdist mock_leak — caplog capture races with sibling tests
    # under -n 6 (project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="mock_leak"
    )
    def test_timeout_increments_counter_and_logs(self, caplog, monkeypatch):
        """Timing-out handler → ``_timeout_count++`` and ``event_bus.handler_timeout`` log."""
        import logging

        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", "0.05")
        reset_event_bus_settings()

        bus = BaldurEventBus()

        def slow(event: BaldurEvent) -> None:
            time.sleep(0.5)

        bus.subscribe(EventType.CONFIG_UPDATED, slow)

        with caplog.at_level(logging.WARNING):
            assert bus.publish(_make_event()) == 0

        assert bus._timeout_count == 1
        assert any("event_bus.handler_timeout" in rec.message for rec in caplog.records)

    def test_future_cancel_called_on_timeout(self, monkeypatch):
        """``future.cancel()`` is invoked on the timed-out future."""
        from concurrent.futures import Future

        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", "0.05")
        reset_event_bus_settings()

        bus = BaldurEventBus()
        cancel_calls: list[Future] = []

        original_submit = ThreadPoolExecutor.submit

        def patched_submit(self, fn, *args, **kwargs):
            future = original_submit(self, fn, *args, **kwargs)
            real_cancel = future.cancel

            def _cancel():
                cancel_calls.append(future)
                return real_cancel()

            future.cancel = _cancel  # type: ignore[method-assign]
            return future

        def slow(event: BaldurEvent) -> None:
            time.sleep(0.5)

        bus.subscribe(EventType.CONFIG_UPDATED, slow)

        monkeypatch.setattr(ThreadPoolExecutor, "submit", patched_submit)
        bus.publish(_make_event())

        assert len(cancel_calls) == 1


# =============================================================================
# Shutdown-window race — RuntimeError → inline fallback (G7 / D6)
# =============================================================================


class TestAsyncPoolShutdownWindowBehavior:
    """487 G7: pre-shutdown executor raises ``RuntimeError`` on submit → inline fallback."""

    def test_shutdown_executor_runtimeerror_falls_back_inline(self):
        """When ``submit`` raises ``RuntimeError`` the handler runs inline + returns True."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        # Construct an executor and shut it down so subsequent
        # ``submit()`` calls raise ``RuntimeError``.
        pre_shutdown_executor = ThreadPoolExecutor(max_workers=1)
        pre_shutdown_executor.shutdown(wait=True)

        bus = BaldurEventBus()
        observed: list[int] = []

        def handler(event: BaldurEvent) -> None:
            observed.append(threading.get_ident())

        bus.subscribe(EventType.CONFIG_UPDATED, handler)

        # Force the dispatch path to use the already-shut-down executor.
        try:
            BaldurEventBus._executor = pre_shutdown_executor  # type: ignore[assignment]
            caller = threading.get_ident()
            assert bus.publish(_make_event()) == 1
            # Inline execution → handler ran on the caller thread.
            assert observed == [caller]
        finally:
            BaldurEventBus._executor = None

    def test_shutdown_window_does_not_log_handler_failed(self, caplog):
        """RuntimeError fallback path does NOT emit ``event_bus.handler_failed``."""
        import logging

        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        pre_shutdown_executor = ThreadPoolExecutor(max_workers=1)
        pre_shutdown_executor.shutdown(wait=True)

        bus = BaldurEventBus()
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)

        try:
            BaldurEventBus._executor = pre_shutdown_executor  # type: ignore[assignment]
            with caplog.at_level(logging.ERROR):
                bus.publish(_make_event())

            assert not any(
                "event_bus.handler_failed" in rec.message for rec in caplog.records
            )
        finally:
            BaldurEventBus._executor = None


# =============================================================================
# Zombie-handler pool exhaustion regression
# =============================================================================


class TestAsyncPoolZombieHandlerRegressionBehavior:
    """487 G7: saturate the pool with timed-out handlers; next emit still hits timeout path."""

    def test_saturated_pool_does_not_block_subsequent_emit(self, monkeypatch):
        """``dispatch_workers`` saturated handlers → next emit hits timeout within bound.

        CPython cannot force-kill a worker thread, so a runaway handler
        holds its pool slot indefinitely. With ``dispatch_workers=2``
        and 2 zombie handlers, the next emit still takes the timeout
        path (and the timeout fires within ``handler_timeout_seconds``)
        because ``executor.submit`` queues the new task and
        ``future.result(timeout=)`` enforces the bound on the queue
        wait, NOT on actual execution.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_WORKERS", "2")
        monkeypatch.setenv("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", "0.05")
        reset_event_bus_settings()

        bus = BaldurEventBus()

        zombie_release = threading.Event()

        def zombie(event: BaldurEvent) -> None:
            zombie_release.wait(timeout=5.0)

        def fast(event: BaldurEvent) -> None:
            pass

        bus.subscribe(EventType.CONFIG_UPDATED, zombie, priority=EventPriority.HIGH)
        # Saturate the 2-worker pool with two zombie emissions.
        zombie_t1 = threading.Thread(target=bus.publish, args=(_make_event(),))
        zombie_t2 = threading.Thread(target=bus.publish, args=(_make_event(),))
        zombie_t1.start()
        zombie_t2.start()

        # Give both zombies time to be picked up by the worker threads.
        time.sleep(0.2)

        bus.unsubscribe(EventType.CONFIG_UPDATED, zombie)
        bus.subscribe(EventType.CONFIG_UPDATED, fast)

        # Subsequent emit must still hit the timeout path within
        # handler_timeout_seconds — NOT block waiting forever for a free
        # worker.
        start = time.monotonic()
        result = bus.publish(_make_event())
        elapsed = time.monotonic() - start

        try:
            assert result == 0
            assert bus._timeout_count >= 1
            # Allow generous slack for CI/Windows scheduler — the key is
            # that we did NOT wait minutes.
            assert elapsed < 2.0
        finally:
            zombie_release.set()
            zombie_t1.join(timeout=5.0)
            zombie_t2.join(timeout=5.0)


# =============================================================================
# Return-value contract — publish() returns successful handler count
# =============================================================================


class TestAsyncPoolReturnValueContract:
    """487 D9: ``bus.publish()`` returns successful-handler count (pre-487 contract)."""

    def test_publish_returns_handler_count(self):
        """N successful handlers → ``publish()`` returns N."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()

        def make_handler(label: str):
            def _h(event: BaldurEvent) -> None:
                pass

            _h.__name__ = f"handler_{label}"
            return _h

        bus.subscribe(EventType.CONFIG_UPDATED, make_handler("a"))
        bus.subscribe(EventType.CONFIG_UPDATED, make_handler("b"))
        bus.subscribe(EventType.CONFIG_UPDATED, make_handler("c"))

        assert bus.publish(_make_event()) == 3

    def test_publish_returns_zero_when_no_subscribers(self):
        """No subscribers → ``publish()`` returns 0."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        assert bus.publish(_make_event()) == 0
