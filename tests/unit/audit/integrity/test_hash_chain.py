"""
Hash chain and checkpoint unit tests (Phase 3).

Tests:
- Checkpoint creation and lookup
- Checkpoint-based integrity verification
- Querying events after a given timestamp
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from baldur.audit.cascade_auditor import (
    CascadeEventAuditor,
    reset_cascade_auditor,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def memory_backend():
    """Memory backend fixture."""
    from baldur.core.state_backend import MemoryStateBackend

    return MemoryStateBackend()


@pytest.fixture
def cascade_auditor(memory_backend):
    """CascadeEventAuditor fixture with memory backend."""
    reset_cascade_auditor()

    auditor = CascadeEventAuditor()
    auditor._get_backend = MagicMock(return_value=memory_backend)

    return auditor


@pytest.fixture
def populated_auditor(cascade_auditor):
    """
    Auditor fixture pre-populated with several cascade events.

    Records 5 events to build up the chain.
    """
    for i in range(5):
        cascade_auditor.record(
            trigger_type=f"TEST_TRIGGER_{i}",
            trigger_details={"index": i},
            effects=[
                {"action_type": f"ACTION_{i}", "success": True},
            ],
            namespace="test",
            triggered_by="test",
        )

    return cascade_auditor


# =============================================================================
# Checkpoint Creation Tests
# =============================================================================


class TestCreateCheckpoint:
    """Checkpoint creation tests."""

    def test_create_checkpoint_empty_namespace(self, cascade_auditor):
        """Create a checkpoint for an empty namespace."""
        checkpoint = cascade_auditor.create_checkpoint("empty")

        assert checkpoint is not None
        assert checkpoint["namespace"] == "empty"
        assert checkpoint["event_count"] == 0
        assert checkpoint["last_hash"] is None
        assert "verified_at" in checkpoint

    def test_create_checkpoint_with_events(self, populated_auditor):
        """Create a checkpoint for a namespace that has events."""
        checkpoint = populated_auditor.create_checkpoint("test")

        assert checkpoint is not None
        assert checkpoint["namespace"] == "test"
        assert checkpoint["event_count"] == 5
        assert checkpoint["last_hash"] is not None
        assert len(checkpoint["last_hash"]) == 64  # SHA-256 hex
        assert "verified_at" in checkpoint
        assert checkpoint["version"] == "1.0"

    def test_create_checkpoint_updates_existing(self, populated_auditor):
        """A later checkpoint reflects newly recorded events."""
        # First checkpoint
        checkpoint1 = populated_auditor.create_checkpoint("test")

        # Add a new event
        populated_auditor.record(
            trigger_type="NEW_EVENT",
            trigger_details={},
            effects=[],
            namespace="test",
        )

        # Second checkpoint
        checkpoint2 = populated_auditor.create_checkpoint("test")

        assert checkpoint2["event_count"] == 6
        assert checkpoint2["last_hash"] != checkpoint1["last_hash"]
        assert checkpoint2["verified_at"] >= checkpoint1["verified_at"]


# =============================================================================
# Get Checkpoint Tests
# =============================================================================


class TestGetCheckpoint:
    """Checkpoint lookup tests."""

    def test_get_checkpoint_not_exists(self, cascade_auditor):
        """Look up a checkpoint that does not exist."""
        result = cascade_auditor.get_checkpoint("nonexistent")
        assert result is None

    def test_get_checkpoint_exists(self, populated_auditor):
        """Look up an existing checkpoint."""
        # Create the checkpoint
        created = populated_auditor.create_checkpoint("test")

        # Retrieve
        retrieved = populated_auditor.get_checkpoint("test")

        assert retrieved is not None
        assert retrieved["last_hash"] == created["last_hash"]
        assert retrieved["event_count"] == created["event_count"]
        assert retrieved["verified_at"] == created["verified_at"]


# =============================================================================
# Verify Chain Integrity From Checkpoint Tests
# =============================================================================


class TestVerifyChainIntegrityFromCheckpoint:
    """Checkpoint-based integrity verification tests."""

    def test_verify_no_checkpoint_falls_back(self, populated_auditor):
        """Fall back to full verification when no checkpoint exists."""
        result = populated_auditor.verify_chain_integrity_from_checkpoint("test")

        assert result["valid"] is True
        assert result["checked"] == 5
        # from_checkpoint is absent or None

    def test_verify_with_checkpoint_no_new_events(self, populated_auditor):
        """No new events after the checkpoint."""
        # Create the checkpoint
        populated_auditor.create_checkpoint("test")

        # Verify (no new events)
        result = populated_auditor.verify_chain_integrity_from_checkpoint("test")

        assert result["valid"] is True
        assert result["checked"] == 0
        assert "from_checkpoint" in result

    def test_verify_with_checkpoint_new_events(self, populated_auditor):
        """Verify only the events recorded after the checkpoint."""
        # Create the checkpoint
        populated_auditor.create_checkpoint("test")

        # Add new events
        populated_auditor.record(
            trigger_type="NEW_EVENT_1",
            trigger_details={},
            effects=[{"action_type": "ACTION", "success": True}],
            namespace="test",
        )
        populated_auditor.record(
            trigger_type="NEW_EVENT_2",
            trigger_details={},
            effects=[],
            namespace="test",
        )

        # Verify (only the 2 new events)
        result = populated_auditor.verify_chain_integrity_from_checkpoint("test")

        assert result["valid"] is True
        assert result["checked"] == 2
        assert "from_checkpoint" in result

    def test_verify_empty_namespace(self, cascade_auditor):
        """Verify an empty namespace."""
        result = cascade_auditor.verify_chain_integrity_from_checkpoint("empty")

        assert result["valid"] is True
        assert result["checked"] == 0


# =============================================================================
# Hash Chain Integrity Tests
# =============================================================================


class TestHashChainIntegrity:
    """Hash chain integrity tests."""

    def test_chain_integrity_valid(self, populated_auditor):
        """Verify a valid chain."""
        result = populated_auditor.verify_chain_integrity("test")

        assert result["valid"] is True
        assert result["checked"] == 5
        assert result["errors"] == []

    def test_hash_values_unique(self, populated_auditor):
        """Each event's hash is unique."""
        events = populated_auditor.get_recent_events("test")
        hashes = [e.current_hash for e in events]

        assert len(hashes) == len(set(hashes))  # all unique

    def test_previous_hash_chain(self, populated_auditor):
        """Each event's previous_hash links back through the chain."""
        events = populated_auditor.get_recent_events("test")

        # Sorted newest-first, so events[0] is the most recent
        for i in range(len(events) - 1):
            newer = events[i]
            older = events[i + 1]

            assert newer.previous_hash == older.current_hash


# =============================================================================
# Get Events After Timestamp Tests
# =============================================================================


class TestGetEventsAfterTimestamp:
    """Tests for querying events after a given timestamp."""

    def test_get_events_after_past_timestamp(self, populated_auditor):
        """A past timestamp returns every event."""
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

        events = populated_auditor.get_events_after_timestamp("test", past)

        assert len(events) == 5

    def test_get_events_after_future_timestamp(self, populated_auditor):
        """A future timestamp returns no events."""
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()

        events = populated_auditor.get_events_after_timestamp("test", future)

        assert len(events) == 0

    def test_get_events_invalid_timestamp(self, populated_auditor):
        """An invalid timestamp format returns everything."""
        events = populated_auditor.get_events_after_timestamp("test", "invalid")

        assert len(events) == 5


# =============================================================================
# Checkpoint Key Pattern Tests
# =============================================================================


class TestCheckpointKeyPattern:
    """Checkpoint key pattern tests."""

    def test_checkpoint_key_format(self):
        """Checkpoint key format."""
        auditor = CascadeEventAuditor()

        key = auditor.CHECKPOINT_KEY.format(namespace="test")

        assert key == "baldur:test:audit:cascade_checkpoint"

    def test_checkpoint_key_with_namespace(self):
        """Key format across different namespaces."""
        auditor = CascadeEventAuditor()

        assert (
            auditor.CHECKPOINT_KEY.format(namespace="seoul")
            == "baldur:seoul:audit:cascade_checkpoint"
        )
        assert (
            auditor.CHECKPOINT_KEY.format(namespace="global")
            == "baldur:global:audit:cascade_checkpoint"
        )
