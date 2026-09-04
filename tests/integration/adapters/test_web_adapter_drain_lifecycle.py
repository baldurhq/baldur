"""Web-adapter in-flight drain lifecycle integration tests (784).

Covers the seam the unit tests cannot reach: the adapter producer writes the
request tracker from the request thread while ``GracefulShutdownCoordinator``
reads ``get_pending_count()`` from its own drain thread and gates the
DRAINING -> TERMINATED transition — and therefore every registered handler's
``on_drain_complete()`` — on it. The unit tests observe the count only from
inside the request; nothing there proves the drain loop actually waits.

Test Categories:
    A. Flask (WSGI request hooks):
        - a request held open keeps the coordinator in DRAINING and
          on_drain_complete() unfired until it returns
        - negative half: with no hooks installed the same sequence reaches
          TERMINATED while the request is still executing
    B. FastAPI (ASGI middleware):
        - the same two halves through BaldurMiddleware

Note: no infrastructure — a real coordinator, a real tracker, a real thread
      and a bounded wait. No ``requires_*`` marker.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from baldur.adapters.fastapi.middleware import BaldurMiddleware
from baldur.adapters.flask.middleware import install_baldur_request_hooks
from baldur.core.shutdown_coordinator import (
    GracefulShutdownCoordinator,
    RequestTracker,
    ShutdownHandler,
    ShutdownPhase,
    TrackedRequest,
    configure_shutdown_coordinator,
    reset_shutdown_coordinator,
)

# =============================================================================
# Fixtures + helpers
# =============================================================================

# Well above the drain the tests actually exercise: no test may reach the
# force-shutdown branch, so a stalled request fails as a hang-free assertion
# rather than as a timeout.
DRAIN_TIMEOUT_SECONDS = 10.0
# Fast enough that "the drain did not complete" is decided by the predicate
# rather than by the poll cadence.
CHECK_INTERVAL_SECONDS = 0.02
# Upper bound for every deterministic wait (Event / thread join / drain join).
JOIN_TIMEOUT_SECONDS = 10.0
# Window granted to the drain loop to (wrongly) terminate while a request is
# still open — many poll intervals, and CPU contention only lengthens the
# request, never shortens the loop, so a slow host cannot flip the verdict.
DRAIN_SETTLE_SECONDS = 0.3


class _RecordingHandler(ShutdownHandler):
    """Drain participant that records which shutdown callbacks fired."""

    def __init__(self) -> None:
        self.started = 0
        self.drain_complete = 0
        self.force_shutdown = 0

    def on_shutdown_start(self) -> None:
        self.started += 1

    def on_drain_complete(self) -> None:
        self.drain_complete += 1

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        self.force_shutdown += 1


@pytest.fixture
def drain():
    """Real coordinator + real tracker installed as the process singleton.

    The adapters resolve the tracker through ``get_shutdown_coordinator()``, so
    the coordinator under test has to BE the singleton — not an injected double.
    """
    reset_shutdown_coordinator()
    tracker = RequestTracker()
    coordinator = GracefulShutdownCoordinator(
        request_tracker=tracker,
        drain_timeout=DRAIN_TIMEOUT_SECONDS,
        check_interval=CHECK_INTERVAL_SECONDS,
    )
    configure_shutdown_coordinator(coordinator)
    handler = _RecordingHandler()
    coordinator.register_handler(handler)

    yield SimpleNamespace(coordinator=coordinator, tracker=tracker, handler=handler)

    # Join the drain thread before the next test reuses the singleton slot.
    coordinator.wait_for_shutdown(timeout=JOIN_TIMEOUT_SECONDS)
    reset_shutdown_coordinator()


class _Held:
    """A request the test opens, holds, and releases on demand."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_until_in_flight(self) -> None:
        assert self.entered.wait(timeout=JOIN_TIMEOUT_SECONDS), (
            "the held request never reached the application"
        )

    def hold(self) -> None:
        self.entered.set()
        assert self.release.wait(timeout=JOIN_TIMEOUT_SECONDS), (
            "the held request was never released"
        )


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _http_scope() -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/slow",
        "headers": [],
        "query_string": b"",
        "client": ("203.0.113.5", 54321),
    }


class _AsgiRecorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


# =============================================================================
# A. Flask (WSGI request hooks)
# =============================================================================


class TestFlaskAdapterDrainLifecycle:
    """The Flask hooks make the drain loop wait for the request they opened.

    Validates:
    - the coordinator stays in DRAINING while a Flask request is executing
    - on_drain_complete() fires only after that request returns
    - without the producer the same sequence terminates mid-request
    """

    @staticmethod
    def _app(held: _Held, *, install_hooks: bool) -> Flask:
        app = Flask(__name__)

        @app.route("/slow")
        def _slow():
            held.hold()
            return {"ok": True}

        if install_hooks:
            install_baldur_request_hooks(app)
        return app

    def test_drain_waits_for_the_in_flight_flask_request(self, drain):
        """
        Purpose:
            A SIGTERM-equivalent while a Flask request is executing must not
            complete the drain until that request returns.
        Expected:
            - the phase stays DRAINING for the whole time the request is open
            - the handler's on_drain_complete() has not fired at that point
            - after the request returns: TERMINATED, on_drain_complete() once,
              and the tracker back to zero pending
        """
        # Given: a Flask request parked inside the view
        held = _Held()
        client = self._app(held, install_hooks=True).test_client()
        responses: list = []
        caller = threading.Thread(
            target=lambda: responses.append(client.get("/slow")), daemon=True
        )
        caller.start()
        try:
            held.wait_until_in_flight()

            # When: the drain begins with that request still open
            drain.coordinator.initiate_shutdown()

            # Then: it cannot finish
            assert (
                drain.coordinator.wait_for_shutdown(timeout=DRAIN_SETTLE_SECONDS)
                is False
            )
            assert drain.coordinator.get_stats().phase is ShutdownPhase.DRAINING
            assert drain.tracker.get_pending_count() == 1
            assert drain.handler.drain_complete == 0
        finally:
            held.release.set()
            caller.join(timeout=JOIN_TIMEOUT_SECONDS)

        # And: releasing the request lets the same drain finish
        assert drain.coordinator.wait_for_shutdown(timeout=JOIN_TIMEOUT_SECONDS) is True
        assert drain.coordinator.get_stats().phase is ShutdownPhase.TERMINATED
        assert drain.handler.drain_complete == 1
        assert drain.handler.force_shutdown == 0
        assert drain.tracker.get_pending_count() == 0
        assert responses[0].status_code == 200

    def test_drain_completes_mid_request_without_the_flask_producer(self, drain):
        """
        Purpose:
            Negative half — pin that the wait above is caused by the producer,
            not by the coordinator or the registered handler.
        Expected:
            - with no hooks installed the drain reaches TERMINATED while the
              request is still parked in the view
            - on_drain_complete() fires at that point, with nothing tracked
        """
        held = _Held()
        client = self._app(held, install_hooks=False).test_client()
        caller = threading.Thread(target=lambda: client.get("/slow"), daemon=True)
        caller.start()
        try:
            held.wait_until_in_flight()
            drain.coordinator.initiate_shutdown()
            terminated = drain.coordinator.wait_for_shutdown(
                timeout=JOIN_TIMEOUT_SECONDS
            )
            drain_complete_while_open = drain.handler.drain_complete
        finally:
            held.release.set()
            caller.join(timeout=JOIN_TIMEOUT_SECONDS)

        assert terminated is True
        assert drain_complete_while_open == 1
        assert drain.tracker.get_pending_count() == 0


# =============================================================================
# B. FastAPI (ASGI middleware)
# =============================================================================


class TestFastapiAdapterDrainLifecycle:
    """``BaldurMiddleware`` makes the drain loop wait for the request it opened.

    Validates:
    - the coordinator stays in DRAINING while the downstream await is pending
    - on_drain_complete() fires only after the ASGI call returns
    - without the middleware the same sequence terminates mid-request
    """

    @staticmethod
    def _call_in_thread(app, recorder: _AsgiRecorder) -> threading.Thread:
        """Drive one ASGI request to completion on its own event loop."""

        def _drive() -> None:
            asyncio.run(app(_http_scope(), _receive, recorder))

        thread = threading.Thread(target=_drive, daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _held_app(held: _Held):
        async def _app(scope, receive, send) -> None:
            # Blocking this loop is safe: it serves exactly this one request.
            held.hold()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        return _app

    def test_drain_waits_for_the_in_flight_asgi_request(self, drain):
        """
        Purpose:
            A drain started while the downstream ASGI await is pending must not
            complete until that await returns.
        Expected:
            - the phase stays DRAINING while the downstream app is parked
            - on_drain_complete() has not fired at that point
            - after the call returns: TERMINATED, on_drain_complete() once,
              200 emitted, tracker back to zero pending
        """
        # Given: an ASGI request parked inside the downstream app
        held = _Held()
        recorder = _AsgiRecorder()
        caller = self._call_in_thread(BaldurMiddleware(self._held_app(held)), recorder)
        try:
            held.wait_until_in_flight()

            # When
            drain.coordinator.initiate_shutdown()

            # Then
            assert (
                drain.coordinator.wait_for_shutdown(timeout=DRAIN_SETTLE_SECONDS)
                is False
            )
            assert drain.coordinator.get_stats().phase is ShutdownPhase.DRAINING
            assert drain.tracker.get_pending_count() == 1
            assert drain.handler.drain_complete == 0
        finally:
            held.release.set()
            caller.join(timeout=JOIN_TIMEOUT_SECONDS)

        assert drain.coordinator.wait_for_shutdown(timeout=JOIN_TIMEOUT_SECONDS) is True
        assert drain.coordinator.get_stats().phase is ShutdownPhase.TERMINATED
        assert drain.handler.drain_complete == 1
        assert drain.handler.force_shutdown == 0
        assert drain.tracker.get_pending_count() == 0
        assert recorder.messages[0]["status"] == 200

    def test_drain_completes_mid_request_without_the_asgi_producer(self, drain):
        """
        Purpose:
            Negative half — the same downstream app called without
            BaldurMiddleware leaves the drain predicate empty.
        Expected:
            - the drain reaches TERMINATED while the downstream app is parked
            - on_drain_complete() fires at that point, with nothing tracked
        """
        held = _Held()
        recorder = _AsgiRecorder()
        caller = self._call_in_thread(self._held_app(held), recorder)
        try:
            held.wait_until_in_flight()
            drain.coordinator.initiate_shutdown()
            terminated = drain.coordinator.wait_for_shutdown(
                timeout=JOIN_TIMEOUT_SECONDS
            )
            drain_complete_while_open = drain.handler.drain_complete
        finally:
            held.release.set()
            caller.join(timeout=JOIN_TIMEOUT_SECONDS)

        assert terminated is True
        assert drain_complete_while_open == 1
        assert drain.tracker.get_pending_count() == 0
