"""
DegradedEntryMarker 테스트.

Marking entries recorded during failures 테스트.
"""


class TestDegradedEntryMarker:
    """Tests for DegradedEntryMarker."""

    def test_mark_degraded(self, mock_redis, sample_entry):
        """Test marking entry as degraded."""
        from baldur.audit.graceful_degradation import DegradedEntryMarker

        marker = DegradedEntryMarker(redis_client=mock_redis)
        sample_entry["integrity"] = {"sequence": 1}

        result = marker.mark_degraded(sample_entry, "test_reason", "test_tier")

        assert result["integrity"]["degraded"] is True
        assert result["integrity"]["degraded_reason"] == "test_reason"
        assert result["integrity"]["degraded_tier"] == "test_tier"
        assert "degraded_at" in result["integrity"]

    def test_mark_reconciled(self, mock_redis, sample_entry):
        """Test marking entry as reconciled."""
        from baldur.audit.graceful_degradation import DegradedEntryMarker

        marker = DegradedEntryMarker(redis_client=mock_redis)
        sample_entry["integrity"] = {"sequence": 1}

        marker.mark_degraded(sample_entry, "test", "test")
        result = marker.mark_reconciled(1, 100)

        assert result is True

        unreconciled = marker.get_unreconciled_entries()
        assert len(unreconciled) == 0

    def test_get_unreconciled_entries(self, sample_entry):
        """Test getting unreconciled entries."""
        from baldur.audit.graceful_degradation import DegradedEntryMarker

        marker = DegradedEntryMarker()

        for i in range(3):
            entry = {"integrity": {"sequence": i}}
            marker.mark_degraded(entry, "test", "test")

        # Reconcile one
        marker.mark_reconciled(1, 101)

        unreconciled = marker.get_unreconciled_entries()
        assert len(unreconciled) == 2

    def test_clear_reconciled(self, sample_entry):
        """Test clearing reconciled entries."""
        from baldur.audit.graceful_degradation import DegradedEntryMarker

        marker = DegradedEntryMarker()

        for i in range(3):
            entry = {"integrity": {"sequence": i}}
            marker.mark_degraded(entry, "test", "test")

        marker.mark_reconciled(0, 100)
        marker.mark_reconciled(1, 101)

        cleared = marker.clear_reconciled()
        assert cleared == 2

        # Only one unreconciled should remain
        unreconciled = marker.get_unreconciled_entries()
        assert len(unreconciled) == 1

    def test_stats(self):
        """Test statistics tracking."""
        from baldur.audit.graceful_degradation import DegradedEntryMarker

        marker = DegradedEntryMarker()

        marker.mark_degraded({"integrity": {"sequence": 1}}, "test", "test")
        marker.mark_reconciled(1, 101)

        stats = marker.get_stats()
        assert stats["marked_count"] == 1
        assert stats["reconciled_count"] == 1
        assert stats["unreconciled_count"] == 0
