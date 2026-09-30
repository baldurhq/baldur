"""A writer process for the file state store's cross-process tests.

Run as a script by ``test_file_state_backend_cross_process.py``; never collected
as a test. It opens the shared store directory, waits for the common start
instant, then writes one key through ``compare_and_set`` and prints one summary
line prefixed ``WRITER `` (JSON).

Modes (``WRITER_MODE``):

- ``interleave``: ``WRITER_COUNT`` read-then-CAS attempts, racing the peer; the
  summary counts the attempts that committed.
- ``rounds``: round ``r`` makes one CAS expecting ``WRITER_BASE + r`` — the
  version both writers share for that round — and reports each round's answer.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


def _wait_until(epoch_seconds: float) -> None:
    pause = threading.Event()
    while time.time() < epoch_seconds:
        pause.wait(min(0.005, max(epoch_seconds - time.time(), 0.0)))


def main() -> int:
    from baldur.core.state_backend import OCC_VERSION_FIELD, FileStateBackend

    directory = os.environ["WRITER_DIR"]
    key = os.environ["WRITER_KEY"]
    mode = os.environ["WRITER_MODE"]
    count = int(os.environ["WRITER_COUNT"])
    base = int(os.environ.get("WRITER_BASE", "0"))
    start_at = float(os.environ["WRITER_START_AT"])
    name = os.environ["WRITER_NAME"]

    backend = FileStateBackend(directory)
    _wait_until(start_at)

    committed = 0
    errors: list[str] = []
    rounds: list[bool] = []
    for i in range(count):
        try:
            if mode == "interleave":
                stored = backend.get_strict(key) or {}
                version = int(stored.get(OCC_VERSION_FIELD, 0))
                value = {"writer": name, "i": i, OCC_VERSION_FIELD: version + 1}
                if backend.compare_and_set(key, version, value):
                    committed += 1
            else:
                expected = base + i
                value = {"writer": name, "round": i, OCC_VERSION_FIELD: expected + 1}
                won = backend.compare_and_set(key, expected, value)
                rounds.append(won)
                committed += int(won)
        except Exception as e:  # reported, never swallowed silently
            errors.append(f"{type(e).__name__}: {e}")
            if mode == "rounds":
                rounds.append(False)

    print(
        "WRITER "
        + json.dumps(
            {"name": name, "committed": committed, "rounds": rounds, "errors": errors}
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
