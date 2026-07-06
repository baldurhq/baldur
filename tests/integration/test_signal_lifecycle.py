"""Standalone signal lifecycle integration tests.

Real-OS-signal subprocess tests for the disposition-sensitive signal
chaining and the post-drain exit trampoline — the exit path spans two
threads plus kernel signal delivery (main-thread handler TAS → drain
thread → self-kill trampoline → main-thread restore + re-raise →
default action) and is not representable with mocks.

Test Categories:
    A. Plain-Python defer-exit path:
        - SIGTERM on a SIG_DFL-disposition child with one tracked
          in-flight request exits by true signal death after the drain
        - SIGINT on a default_int_handler child chains
          KeyboardInterrupt and exits after the drain instead of
          swallowing the signal
    B. uvicorn coexistence (chain mode):
        - SIGTERM triggers BOTH Baldur's drain and uvicorn's own
          shutdown; the Baldur lifespan teardown observes TERMINATED

POSIX-only: Windows has no SIGTERM delivery and the defer-exit
re-raise path is unreachable on win32. On local Windows verify via
the Docker command in 597
(SC5); the file also runs in Linux CI.

No Docker service markers: children are plain ``sys.executable -c``
subprocesses. ``BALDUR_SCHEDULER_AUTOSTART`` is restored to its
production default (ON) in the children — the default config is
exactly the clobber-cascade regression this file guards.

Flake hygiene: children signal readiness via file markers / TCP
accept; every verdict polls (``process.wait`` with a timeout); there
are no fixed sleeps in the parent.
"""
# Lifecycle coverage: 597 SC5 (D2/D3/D4/D9/D10 end to end).

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: Windows has no SIGTERM delivery",
)

_READY_POLL_INTERVAL = 0.05
_READY_TIMEOUT = 60.0


def _drain_window_seconds() -> float:
    """Exit-verdict window: default drain timeout + generous slack."""
    from baldur.settings.recovery_shutdown import get_recovery_shutdown_settings

    return get_recovery_shutdown_settings().default_drain_timeout_seconds + 15.0


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    # Default-config regression guard: scheduler autostart stays at its
    # production default (ON) so init() reaches
    # register_for_graceful_shutdown — the path that used to clobber.
    env.pop("BALDUR_SCHEDULER_AUTOSTART", None)
    # Children must not bind real ports for the admin server.
    env["BALDUR_ADMIN_AUTOSTART"] = "0"
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return env


def _spawn_child(script: str, args: list[str], log_path) -> subprocess.Popen:
    log_fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — Popen owns it
    try:
        return subprocess.Popen(
            [sys.executable, "-c", script, *args],
            env=_child_env(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_fh.close()


def _await_readiness(predicate, proc: subprocess.Popen, log_path, timeout: float):
    """Poll a readiness predicate, failing fast if the child dies early."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if proc.poll() is not None:
            pytest.fail(
                f"child exited early (rc={proc.returncode}):\n"
                f"{log_path.read_text(encoding='utf-8')}"
            )
        time.sleep(_READY_POLL_INTERVAL)
    proc.kill()
    proc.wait(timeout=10)
    pytest.fail(
        f"child not ready within {timeout}s:\n{log_path.read_text(encoding='utf-8')}"
    )


def _terminate_leftover(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Plain-Python child: default-config init() with one tracked in-flight
# request that completes shortly after the drain starts. Readiness is
# signalled by writing sys.argv[1]. The main loop differs per scenario.
_PLAIN_CHILD_TEMPLATE = """
import sys
import threading
import time

import baldur

baldur.init()

from baldur.core.shutdown_coordinator import get_shutdown_coordinator

coordinator = get_shutdown_coordinator()
tracker = coordinator._tracker
tracker.start_request("in-flight-1")


def _finish_in_flight_after_drain_starts():
    while not coordinator.is_shutting_down():
        time.sleep(0.05)
    # Hold the request briefly so the drain visibly waits on it.
    time.sleep(0.2)
    tracker.end_request("in-flight-1")


threading.Thread(target=_finish_in_flight_after_drain_starts, daemon=True).start()

with open(sys.argv[1], "w") as fh:
    fh.write("ready")

{main_loop}
"""

# SIGTERM scenario: park forever — only the trampoline can end the process.
_PARK_FOREVER = """
while True:
    time.sleep(0.5)
"""

# SIGINT scenario: the chained default_int_handler raises
# KeyboardInterrupt; the child then waits for the drain and reports the
# verdict via its exit code (0 = drain reached TERMINATED).
_PARK_UNTIL_KEYBOARD_INTERRUPT = """
try:
    while True:
        time.sleep(0.5)
except KeyboardInterrupt:
    completed = coordinator.wait_for_shutdown(timeout=60)
    sys.exit(0 if completed else 3)
"""

# uvicorn child: Baldur's fastapi lifespan chained under uvicorn's
# handle_exit. After the inner lifespan teardown (drain wait) completes,
# the wrapper records the coordinator phase to sys.argv[1].
_UVICORN_CHILD_SCRIPT = """
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from baldur.adapters.fastapi.lifespan import fastapi_lifespan


@asynccontextmanager
async def lifespan(app):
    async with fastapi_lifespan(app):
        yield {}
    # Reached only after Baldur's teardown drain-wait returned.
    from baldur.core.shutdown_coordinator import get_shutdown_coordinator

    with open(sys.argv[1], "w") as fh:
        fh.write(get_shutdown_coordinator().phase.value)


app = FastAPI(lifespan=lifespan)

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
"""


class TestPlainPythonSignalLifecycle:
    """Defer-exit lifecycle: SIG_DFL / default_int_handler dispositions."""

    def test_sigterm_plain_python_child_exits_by_signal_after_drain(self, tmp_path):
        """
        Purpose:
            A default-config plain-Python child (SIGTERM disposition
            SIG_DFL → defer-exit mode) sent SIGTERM while one tracked
            request is in flight must drain and then terminate via the
            two-hop trampoline re-raise.
        Expected:
            - child exits within the drain window + slack
            - returncode == -signal.SIGTERM (true signal death, not a
              swallowed signal and not the deadman's exit-code death)
        """
        ready_marker = tmp_path / "ready.txt"
        log_path = tmp_path / "child.log"
        script = _PLAIN_CHILD_TEMPLATE.format(main_loop=_PARK_FOREVER)

        proc = _spawn_child(script, [str(ready_marker)], log_path)
        try:
            _await_readiness(
                lambda: ready_marker.exists(), proc, log_path, _READY_TIMEOUT
            )

            proc.send_signal(signal.SIGTERM)
            returncode = proc.wait(timeout=_drain_window_seconds())
        finally:
            _terminate_leftover(proc)

        assert returncode == -signal.SIGTERM, (
            f"expected signal death {-signal.SIGTERM}, got {returncode}:\n"
            f"{log_path.read_text(encoding='utf-8')}"
        )

    def test_sigint_plain_python_child_exits_after_drain_instead_of_ignoring(
        self, tmp_path
    ):
        """
        Purpose:
            SIGINT on a plain-Python child (disposition
            default_int_handler → chain mode) must initiate the drain
            and chain KeyboardInterrupt — the CLI/dev-server semantics
            — instead of swallowing the signal forever.
        Expected:
            - the child's KeyboardInterrupt path observes a completed
              drain (wait_for_shutdown True → exit code 0)
            - child exits within the drain window + slack
        """
        ready_marker = tmp_path / "ready.txt"
        log_path = tmp_path / "child.log"
        script = _PLAIN_CHILD_TEMPLATE.format(main_loop=_PARK_UNTIL_KEYBOARD_INTERRUPT)

        proc = _spawn_child(script, [str(ready_marker)], log_path)
        try:
            _await_readiness(
                lambda: ready_marker.exists(), proc, log_path, _READY_TIMEOUT
            )

            proc.send_signal(signal.SIGINT)
            returncode = proc.wait(timeout=_drain_window_seconds())
        finally:
            _terminate_leftover(proc)

        assert returncode == 0, (
            f"expected drained-then-exit (0), got {returncode}:\n"
            f"{log_path.read_text(encoding='utf-8')}"
        )


class TestUvicornCoexistence:
    """Chain mode against a real host server owning process exit."""

    def test_uvicorn_child_sigterm_triggers_baldur_drain_and_uvicorn_shutdown(
        self, tmp_path
    ):
        """
        Purpose:
            With uvicorn's handle_exit installed BEFORE baldur.init()
            (chain mode), SIGTERM must trigger BOTH Baldur's drain and
            uvicorn's own graceful shutdown — lifespan shutdown
            executes and the process exits with uvicorn's signal-death
            semantics.
        Expected:
            - the lifespan teardown completes and records the
              coordinator phase as "terminated"
            - the process exits within the drain window + slack with
              signal death (uvicorn restores + LIFO-re-raises SIGTERM)
        """
        pytest.importorskip("uvicorn")
        pytest.importorskip("fastapi")

        shutdown_marker = tmp_path / "lifespan_shutdown.txt"
        log_path = tmp_path / "child.log"
        port = _free_port()

        proc = _spawn_child(
            _UVICORN_CHILD_SCRIPT, [str(shutdown_marker), str(port)], log_path
        )
        try:

            def _accepting() -> bool:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                        return True
                except OSError:
                    return False

            _await_readiness(_accepting, proc, log_path, _READY_TIMEOUT)

            proc.send_signal(signal.SIGTERM)
            returncode = proc.wait(timeout=_drain_window_seconds())
        finally:
            _terminate_leftover(proc)

        assert shutdown_marker.exists(), (
            "lifespan shutdown never completed:\n"
            f"{log_path.read_text(encoding='utf-8')}"
        )
        assert shutdown_marker.read_text(encoding="utf-8") == "terminated"
        assert returncode == -signal.SIGTERM, (
            f"expected uvicorn signal-death {-signal.SIGTERM}, got {returncode}:\n"
            f"{log_path.read_text(encoding='utf-8')}"
        )
