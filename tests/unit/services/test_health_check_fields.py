"""
Tests for SystemHealthSummary new fields (395 A5/A6).

Covers:
- emergency_level field existence and default
- baldur_enabled field existence and default
"""

from baldur.services.health_check import SystemHealthSummary

# =============================================================================
# Contract — Field Existence (§2.3 메트릭/필드 존재 여부)
# =============================================================================


class TestSystemHealthSummaryFieldsContract:
    """SystemHealthSummary 신규 필드 존재 계약 검증."""

    def test_emergency_level_field_exists(self):
        """emergency_level 필드가 존재한다."""
        assert "emergency_level" in SystemHealthSummary.__dataclass_fields__

    def test_baldur_enabled_field_exists(self):
        """baldur_enabled 필드가 존재한다."""
        assert "baldur_enabled" in SystemHealthSummary.__dataclass_fields__

    def test_emergency_level_default_is_none(self):
        """emergency_level 기본값은 None이다."""
        summary = SystemHealthSummary(status="healthy")
        assert summary.emergency_level is None

    def test_baldur_enabled_default_is_none(self):
        """baldur_enabled 기본값은 None이다."""
        summary = SystemHealthSummary(status="healthy")
        assert summary.baldur_enabled is None

    def test_fields_accept_values(self):
        """필드에 값을 설정할 수 있다."""
        summary = SystemHealthSummary(
            status="degraded",
            emergency_level="level_2",
            baldur_enabled=True,
        )
        assert summary.emergency_level == "level_2"
        assert summary.baldur_enabled is True
