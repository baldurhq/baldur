"""State-store doubles for control-state tests.

``ScriptedStateBackend`` is the in-memory store with the failure shapes the
shipped backends really have, switchable per test: a strict read that fails or
blocks, a conditional write that loses to a peer, raises before landing, or
lands and then raises (a lost reply). The kill switch, the emergency level and
the versioned-write primitive are all tested against it, so the outcomes a
writer has to classify are produced by one double rather than re-invented per
file.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from baldur.core.state_backend import MemoryStateBackend

#: Conditional-write steps ``ScriptedStateBackend.cas_script`` accepts.
CAS_PASS = "pass"
CAS_LOSE = "lose"
CAS_PEER = "peer"
CAS_RAISE = "raise"
CAS_LAND_THEN_RAISE = "land_then_raise"


class ScriptedStateBackend(MemoryStateBackend):
    """Memory store whose strict reads and writes fail, block or race on demand.

    Standing switches (hold until cleared):

    - ``fail_reads``: raised by every strict read.
    - ``fail_writes``: raised by every conditional and blind write, before
      anything is written (a full disk, a Redis ``OOM``, a read-only mount).

    One-shot scripts (consumed in order, then normal behaviour):

    - ``read_errors``: one entry per strict read — an exception to raise, or
      ``None`` to read normally.
    - ``cas_script``: one step per conditional write — ``"pass"``; ``"lose"``
      (answer ``False``, write nothing); ``"peer"`` (``peer_value`` commits
      first, then this write loses); ``"raise"`` (raise before writing);
      ``"land_then_raise"`` (write, then raise — a lost reply).
    - ``before_next_read``: called once, inside the next strict read, after
      the stored value was taken and before it is returned — a local write
      racing a read that already began.

    ``read_gate``: when set to an ``Event``, every strict read takes the stored
    value and then waits on it (``read_gate_timeout`` seconds) — a hung store.

    Counters: ``strict_reads`` and ``cas_calls`` (``(expected, value)`` pairs).
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_reads: BaseException | None = None
        self.fail_writes: BaseException | None = None
        self.read_errors: list[BaseException | None] = []
        self.cas_script: list[str] = []
        self.peer_value: dict[str, Any] | None = None
        self.before_next_read: Callable[[], None] | None = None
        self.read_gate: threading.Event | None = None
        self.read_gate_timeout: float = 5.0
        self.strict_reads = 0
        self.cas_calls: list[tuple[int, dict[str, Any]]] = []

    def get_strict(self, key: str) -> dict[str, Any] | None:
        self.strict_reads += 1
        if self.fail_reads is not None:
            raise self.fail_reads
        if self.read_errors:
            error = self.read_errors.pop(0)
            if error is not None:
                raise error
        value = super().get_strict(key)
        hook, self.before_next_read = self.before_next_read, None
        if hook is not None:
            hook()
        if self.read_gate is not None:
            self.read_gate.wait(self.read_gate_timeout)
        return value

    def set(
        self, key: str, value: dict[str, Any], *, ttl_seconds: int | None = None
    ) -> None:
        if self.fail_writes is not None:
            raise self.fail_writes
        super().set(key, value, ttl_seconds=ttl_seconds)

    def compare_and_set(self, key, expected_version, new_value, **kwargs) -> bool:
        self.cas_calls.append((expected_version, dict(new_value)))
        if self.fail_writes is not None:
            raise self.fail_writes
        step = self.cas_script.pop(0) if self.cas_script else CAS_PASS
        if step == CAS_LOSE:
            return False
        if step == CAS_PEER:
            if self.peer_value is None:
                raise AssertionError("a 'peer' step needs peer_value")
            super().set(key, self.peer_value)
            return False
        if step == CAS_RAISE:
            raise ConnectionError("reply lost before the write")
        if step == CAS_LAND_THEN_RAISE:
            super().compare_and_set(key, expected_version, new_value, **kwargs)
            raise ConnectionError("reply lost after the write")
        return super().compare_and_set(key, expected_version, new_value, **kwargs)


__all__ = [
    "CAS_LAND_THEN_RAISE",
    "CAS_LOSE",
    "CAS_PASS",
    "CAS_PEER",
    "CAS_RAISE",
    "ScriptedStateBackend",
]
