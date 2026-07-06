"""
generate_lock_owner_id() unit tests (311 — Phase 4b).

Verifies the standardized lock owner ID format, uniqueness,
and thread-safety.
"""

from __future__ import annotations

import os
import re
import socket
import threading

from baldur.interfaces.cache_provider import generate_lock_owner_id


class TestGenerateLockOwnerIdContract:
    """Contract verification for lock owner ID format."""

    def test_format_has_four_colon_separated_parts(self):
        """Lock owner ID format: {hostname}:{pid}:{thread_id}:{uuid8}."""
        owner_id = generate_lock_owner_id()
        parts = owner_id.split(":")
        assert len(parts) == 4, f"Expected 4 parts, got {len(parts)}: {owner_id}"

    def test_first_part_is_hostname(self):
        """First part is the machine hostname."""
        owner_id = generate_lock_owner_id()
        hostname = owner_id.split(":")[0]
        assert hostname == socket.gethostname()

    def test_second_part_is_pid(self):
        """Second part is the current process ID."""
        owner_id = generate_lock_owner_id()
        pid_str = owner_id.split(":")[1]
        assert int(pid_str) == os.getpid()

    def test_third_part_is_thread_ident(self):
        """Third part is the current thread identifier."""
        owner_id = generate_lock_owner_id()
        tid_str = owner_id.split(":")[2]
        assert int(tid_str) == threading.get_ident()

    def test_fourth_part_is_8_char_hex(self):
        """Fourth part is an 8-character hex string (uuid4 prefix)."""
        owner_id = generate_lock_owner_id()
        suffix = owner_id.split(":")[3]
        assert re.fullmatch(r"[0-9a-f]{8}", suffix), (
            f"Expected 8-char hex, got: {suffix}"
        )


class TestGenerateLockOwnerIdBehavior:
    """Behavior verification for lock owner ID generation."""

    def test_successive_calls_produce_unique_ids(self):
        """Each call returns a unique owner ID (uuid suffix differs)."""
        ids = {generate_lock_owner_id() for _ in range(100)}
        assert len(ids) == 100

    def test_ids_from_different_threads_are_unique(self):
        """Owner IDs generated in different threads are unique."""
        results: list[str] = []

        def worker():
            results.append(generate_lock_owner_id())

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == 20

    def test_ids_from_different_threads_have_different_tid(self):
        """Thread identifiers differ across threads."""
        tids: list[str] = []
        # Rendezvous so every worker is provably alive before any generates
        # its id. Without it a worker can finish and the OS can recycle its
        # thread id before the next starts, yielding fewer distinct tids than
        # threads — a flaky failure under load / on fast interpreters.
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            owner_id = generate_lock_owner_id()
            tids.append(owner_id.split(":")[2])  # list.append is atomic (GIL)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(tids)) == 5

    def test_return_type_is_string(self):
        """Return type is str."""
        assert isinstance(generate_lock_owner_id(), str)
