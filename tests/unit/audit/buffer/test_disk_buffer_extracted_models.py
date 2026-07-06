"""Disk buffer domain models and checksum utility unit tests.

Tests contract values for BufferState, DiskBufferError, and
behavior for BufferEntry.sequence and compute_checksum().
"""

from __future__ import annotations

import json
from enum import Enum

import pytest

from baldur.audit.persistence.checksum import compute_checksum
from baldur.audit.persistence.disk_buffer_models import (
    BufferEntry,
    BufferState,
    DiskBufferError,
)

# =============================================================================
# Contract: BufferState enum values
# =============================================================================


class TestBufferStateContract:
    """BufferState enum design contract verification."""

    def test_buffer_state_has_five_states(self):
        """BufferState enum has exactly 5 states."""
        assert len(BufferState) == 5

    def test_buffer_state_uninitialized_value(self):
        """UNINITIALIZED state value is 'uninitialized'."""
        assert BufferState.UNINITIALIZED == "uninitialized"

    def test_buffer_state_active_value(self):
        """ACTIVE state value is 'active'."""
        assert BufferState.ACTIVE == "active"

    def test_buffer_state_disk_full_failopen_value(self):
        """DISK_FULL_FAILOPEN state value is 'disk_full_failopen'."""
        assert BufferState.DISK_FULL_FAILOPEN == "disk_full_failopen"

    def test_buffer_state_corrupted_value(self):
        """CORRUPTED state value is 'corrupted'."""
        assert BufferState.CORRUPTED == "corrupted"

    def test_buffer_state_closed_value(self):
        """CLOSED state value is 'closed'."""
        assert BufferState.CLOSED == "closed"

    def test_buffer_state_inherits_str_enum(self):
        """BufferState inherits from (str, Enum) for JSON serialization."""
        assert issubclass(BufferState, str)
        assert issubclass(BufferState, Enum)

    def test_buffer_state_json_serializable(self):
        """BufferState values are JSON-serializable strings."""
        for state in BufferState:
            serialized = json.dumps(state)
            assert isinstance(serialized, str)
            # Round-trip: JSON string should deserialize back to a string
            assert json.loads(serialized) == state.value


# =============================================================================
# Contract: DiskBufferError exception hierarchy
# =============================================================================


class TestDiskBufferErrorContract:
    """DiskBufferError exception hierarchy contract."""

    def test_disk_buffer_error_is_exception_subclass(self):
        """DiskBufferError inherits from Exception."""
        assert issubclass(DiskBufferError, Exception)

    def test_disk_buffer_error_can_be_raised_and_caught(self):
        """DiskBufferError can be raised and caught as Exception."""
        with pytest.raises(Exception):
            raise DiskBufferError("test error")


# =============================================================================
# Behavior: BufferEntry.sequence property
# =============================================================================


class TestBufferEntrySequenceBehavior:
    """BufferEntry.sequence property extraction tests."""

    def test_sequence_extracts_from_well_formed_key(self):
        """Extracts sequence number from b'1234.567890:00000042'."""
        entry = BufferEntry(
            key=b"1234.567890:00000042",
            data={"event": "test"},
            timestamp=1234.567890,
            checksum=0,
        )
        assert entry.sequence == 42

    def test_sequence_extracts_large_number(self):
        """Extracts large sequence number correctly."""
        entry = BufferEntry(
            key=b"9999.999999:99999999",
            data={},
            timestamp=9999.999999,
            checksum=0,
        )
        assert entry.sequence == 99999999

    def test_sequence_returns_zero_for_malformed_key_no_colon(self):
        """Returns 0 when key has no colon separator."""
        entry = BufferEntry(
            key=b"no_colon_here",
            data={},
            timestamp=0.0,
            checksum=0,
        )
        assert entry.sequence == 0

    def test_sequence_returns_zero_for_non_numeric_part(self):
        """Returns 0 when the part after colon is not numeric."""
        entry = BufferEntry(
            key=b"1234.567890:not_a_number",
            data={},
            timestamp=0.0,
            checksum=0,
        )
        assert entry.sequence == 0

    def test_sequence_returns_zero_for_empty_key(self):
        """Returns 0 for an empty key."""
        entry = BufferEntry(
            key=b"",
            data={},
            timestamp=0.0,
            checksum=0,
        )
        assert entry.sequence == 0

    def test_sequence_returns_zero_for_colon_only_key(self):
        """Returns 0 when key is just a colon with no parts."""
        entry = BufferEntry(
            key=b":",
            data={},
            timestamp=0.0,
            checksum=0,
        )
        assert entry.sequence == 0


# =============================================================================
# Behavior: compute_checksum()
# =============================================================================


class TestComputeChecksumBehavior:
    """compute_checksum() CRC32 function behavior tests."""

    def test_compute_checksum_deterministic(self):
        """Same input always produces same checksum."""
        data = b"deterministic test data"
        result1 = compute_checksum(data)
        result2 = compute_checksum(data)
        assert result1 == result2

    def test_compute_checksum_different_input_different_output(self):
        """Different inputs produce different checksums."""
        result1 = compute_checksum(b"input_a")
        result2 = compute_checksum(b"input_b")
        assert result1 != result2

    def test_compute_checksum_returns_unsigned_32bit(self):
        """Checksum is an unsigned 32-bit integer (0 to 0xFFFFFFFF)."""
        for test_data in [b"", b"hello", b"\xff" * 1024, b"a" * 10000]:
            result = compute_checksum(test_data)
            assert isinstance(result, int)
            assert 0 <= result <= 0xFFFFFFFF

    def test_compute_checksum_empty_bytes(self):
        """Empty bytes produces a valid checksum."""
        result = compute_checksum(b"")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFFFFFF
