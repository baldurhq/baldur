"""
DegradationLevel Enum 테스트.
"""


class TestDegradationLevel:
    """Tests for DegradationLevel enum."""

    def test_levels_exist(self):
        """Test all degradation levels are defined."""
        from baldur.audit.graceful_degradation import DegradationLevel

        assert DegradationLevel.NORMAL == "normal"
        assert DegradationLevel.DEGRADED == "degraded"
        assert DegradationLevel.EMERGENCY == "emergency"
        assert DegradationLevel.READONLY == "readonly"

    def test_level_comparison(self):
        """Test level string comparison."""
        from baldur.audit.graceful_degradation import DegradationLevel

        assert DegradationLevel.NORMAL.value == "normal"
        assert DegradationLevel.DEGRADED.value == "degraded"
