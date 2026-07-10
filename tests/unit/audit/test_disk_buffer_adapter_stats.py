"""Drop-accounting truth for DiskBufferAdapter.get_stats().

Regression coverage for the stats-shadowing bug: the adapter built its
protocol keys and then splatted the wrapped buffer's stats dict AFTER them, so
the storage layer's hardcoded ``total_dropped: 0`` permanently overwrote the
adapter's real drop counter (a false health signal in an audit-durability
component) and LMDB internals leaked across the protocol boundary. The adapter
now owns every top-level protocol key and nests the wrapped buffer's stats
under ``"backend"``.

Test targets:
    - audit.persistence.disk_buffer_adapter.DiskBufferAdapter.add / get_stats:
      real drop counting, protocol-key authority, backend namespacing.
"""

from __future__ import annotations

from baldur.audit.persistence.disk_buffer_adapter import DiskBufferAdapter


class _StubDiskBuffer:
    """Minimal DiskPersistentBuffer stand-in.

    ``put`` returns a key while ``accept_remaining > 0`` and ``None`` (drop)
    afterwards, so a test can force adapter-layer drops deterministically. Its
    ``get_stats`` payload deliberately carries a ``total_dropped: 0`` and an
    implementation-internal key to pin that neither can shadow or leak into the
    adapter's top-level protocol keys.
    """

    def __init__(self, accept_remaining: int = 0):
        self.accept_remaining = accept_remaining
        self.stored: list[dict] = []

    def put(self, entry: dict):
        if self.accept_remaining > 0:
            self.accept_remaining -= 1
            self.stored.append(entry)
            return len(self.stored)
        return None

    def count(self) -> int:
        return len(self.stored)

    def get_stats(self) -> dict:
        return {
            "count": len(self.stored),
            "total_added": len(self.stored),
            "total_dropped": 0,  # the storage layer never drops; the adapter does
            "db_size_bytes": 4096,
        }


class TestDiskBufferAdapterStatsBehavior:
    """Adapter-owned protocol keys report true drop accounting."""

    def test_dropped_entries_increment_total_dropped(self):
        """Every rejected put is counted in the adapter's total_dropped."""
        adapter = DiskBufferAdapter(disk_buffer=_StubDiskBuffer(accept_remaining=1))

        assert adapter.add({"event": "kept"}) is True
        assert adapter.add({"event": "dropped-1"}) is False
        assert adapter.add({"event": "dropped-2"}) is False

        stats = adapter.get_stats()
        assert stats["total_dropped"] == 2
        assert stats["total_added"] == 1

    def test_backend_hardcoded_zero_cannot_shadow_the_real_drop_counter(self):
        """The wrapped buffer's total_dropped: 0 no longer overwrites real drops."""
        stub = _StubDiskBuffer(accept_remaining=0)
        adapter = DiskBufferAdapter(disk_buffer=stub)

        adapter.add({"event": "dropped"})

        stats = adapter.get_stats()
        # The backend payload still says 0 — the adapter's counter must win.
        assert stats["backend"]["total_dropped"] == 0
        assert stats["total_dropped"] == 1

    def test_backend_stats_are_nested_under_the_backend_key(self):
        """The wrapped buffer's own stats surface intact under 'backend'."""
        stub = _StubDiskBuffer(accept_remaining=2)
        adapter = DiskBufferAdapter(disk_buffer=stub)
        adapter.add({"event": "a"})
        adapter.add({"event": "b"})

        stats = adapter.get_stats()

        assert stats["backend"] == stub.get_stats()

    def test_backend_internals_do_not_leak_to_top_level(self):
        """Implementation-specific backend keys stay out of the protocol surface."""
        adapter = DiskBufferAdapter(disk_buffer=_StubDiskBuffer(accept_remaining=1))
        adapter.add({"event": "a"})

        stats = adapter.get_stats()

        assert "db_size_bytes" not in set(stats) - {"backend"}
        assert "db_size_bytes" in stats["backend"]

    def test_top_level_count_is_computed_by_the_adapter(self):
        """count reflects the adapter's live count() call, not a stale splat."""
        stub = _StubDiskBuffer(accept_remaining=3)
        adapter = DiskBufferAdapter(disk_buffer=stub)
        adapter.add({"event": "a"})
        adapter.add({"event": "b"})

        stats = adapter.get_stats()

        assert stats["count"] == stub.count() == 2

    def test_protocol_keys_present_in_stats(self):
        """The AuditBufferProtocol key set is always emitted at top level."""
        adapter = DiskBufferAdapter(disk_buffer=_StubDiskBuffer())

        stats = adapter.get_stats()

        assert {
            "count",
            "total_added",
            "total_dropped",
            "capacity",
            "usage_percent",
        } <= set(stats)
