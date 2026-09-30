"""A peer process driven by a cross-process integration test.

The control-state reach tests run a real second process (a "peer") that shares
the state store with the test process. The peer prints one JSON object per
line, each prefixed ``PEER ``; ``PeerProcess`` reads those lines on a daemon
thread, lets the test wait for the next line of a given event with matching
fields, and stops the peer through a stop file it polls.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PEER_LINE_PREFIX = "PEER "


class PeerProcess:
    """A running peer script: its protocol lines, and a stop file it polls."""

    def __init__(
        self,
        script: Path,
        env: dict[str, str],
        stop_file: Path,
        *,
        shutdown_timeout: float,
    ) -> None:
        self._stop_file = stop_file
        self._shutdown_timeout = shutdown_timeout
        self._lines: queue.Queue[dict[str, Any]] = queue.Queue()
        # -P: the script's own directory stays off sys.path. The integration
        # test directories hold a test package named ``redis``, which would
        # otherwise shadow redis-py in the peer.
        self.proc = subprocess.Popen(
            [sys.executable, "-P", str(script)],
            env={**env, "PYTHONUNBUFFERED": "1", "PEER_STOP_FILE": str(stop_file)},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            if line.startswith(PEER_LINE_PREFIX):
                self._lines.put(json.loads(line.removeprefix(PEER_LINE_PREFIX)))

    def next_event(self, name: str, timeout: float, **match: Any) -> dict[str, Any]:
        """The next ``name`` line whose fields include ``match``."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"peer did not report {name} {match} in time")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as e:
                raise AssertionError(f"peer did not report {name} {match}") from e
            if line["event"] == name and all(
                line.get(k) == v for k, v in match.items()
            ):
                return line

    def finish(self) -> dict[str, Any]:
        """Stop serving and return the peer's ``done`` line."""
        self._stop_file.touch()
        done = self.next_event("done", timeout=self._shutdown_timeout)
        self.proc.wait(timeout=self._shutdown_timeout)
        return done

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=self._shutdown_timeout)


__all__ = ["PEER_LINE_PREFIX", "PeerProcess"]
