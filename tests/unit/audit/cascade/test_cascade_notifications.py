"""
Cascade Event notification template unit tests.

Phase 9: notification message generation tests.

Tests:
- cascade_integrity_alert: Hash Chain integrity violation alert
- cascade_depth_alert: chain depth exceeded alert
- cascade_load_shedding_alert: Load Shedding activation/deactivation alert
- cascade_summary: daily summary alert
- cascade_fallback_recovery_alert: local fallback recovery alert
"""

from __future__ import annotations

# =============================================================================
# Integrity Alert Tests
# =============================================================================


class TestCascadeIntegrityAlert:
    """Hash Chain integrity violation alert tests."""

    def test_integrity_alert_basic(self):
        """Build a basic integrity violation alert."""
        from baldur.audit.cascade_notifications import cascade_integrity_alert

        errors = [
            {
                "cascade_id": "cascade-xyz",
                "error": "hash_mismatch",
                "expected": "abc",
                "actual": "def",
            }
        ]

        alert = cascade_integrity_alert(
            namespace="seoul",
            errors=errors,
            verified_count=100,
        )

        assert alert["severity"] == "critical"
        assert "seoul" in alert["title"]
        assert "integrity violation" in alert["message"]
        assert alert["details"]["namespace"] == "seoul"
        assert alert["details"]["verified_count"] == 100
        assert alert["details"]["error_count"] == 1

    def test_integrity_alert_limits_errors_to_5(self):
        """The error list is capped at 5 entries."""
        from baldur.audit.cascade_notifications import cascade_integrity_alert

        errors = [{"cascade_id": f"cascade-{i}"} for i in range(10)]

        alert = cascade_integrity_alert(
            namespace="seoul",
            errors=errors,
            verified_count=100,
        )

        # details includes at most 5
        assert len(alert["details"]["errors"]) == 5

    def test_integrity_alert_has_actions(self):
        """The alert includes action links."""
        from baldur.audit.cascade_notifications import cascade_integrity_alert

        alert = cascade_integrity_alert(
            namespace="seoul",
            errors=[{"cascade_id": "test"}],
            verified_count=100,
        )

        assert len(alert["actions"]) == 2
        assert "Investigate details" in alert["actions"][0]["label"]
        assert "Restore checkpoint" in alert["actions"][1]["label"]


# =============================================================================
# Depth Alert Tests
# =============================================================================


class TestCascadeDepthAlert:
    """Chain depth exceeded alert tests."""

    def test_depth_alert_critical_at_max(self):
        """Critical alert when the maximum depth is reached."""
        from baldur.audit.cascade_notifications import cascade_depth_alert

        alert = cascade_depth_alert(
            namespace="seoul",
            cascade_id="cascade-abc123",
            current_depth=10,
            max_depth=10,
        )

        assert alert["severity"] == "critical"
        assert "🔴" in alert["title"]
        assert alert["details"]["current_depth"] == 10
        assert alert["details"]["max_depth"] == 10

    def test_depth_alert_warning_before_max(self):
        """Warning alert below the maximum depth."""
        from baldur.audit.cascade_notifications import cascade_depth_alert

        alert = cascade_depth_alert(
            namespace="seoul",
            cascade_id="cascade-abc123",
            current_depth=8,
            max_depth=10,
        )

        assert alert["severity"] == "warning"
        assert "🟡" in alert["title"]

    def test_depth_alert_includes_cascade_link(self):
        """Includes the Cascade details link."""
        from baldur.audit.cascade_notifications import cascade_depth_alert

        alert = cascade_depth_alert(
            namespace="seoul",
            cascade_id="cascade-abc123",
            current_depth=10,
            max_depth=10,
        )

        assert len(alert["actions"]) == 1
        assert "cascade-abc123" in alert["actions"][0]["url"]


# =============================================================================
# Load Shedding Alert Tests
# =============================================================================


class TestCascadeLoadSheddingAlert:
    """Load Shedding alert tests."""

    def test_load_shedding_enabled_alert(self):
        """Load Shedding activation alert."""
        from baldur.audit.cascade_notifications import cascade_load_shedding_alert

        alert = cascade_load_shedding_alert(
            enabled=True,
            current_load=0.85,
            threshold=0.7,
            dropped_count=100,
        )

        assert alert["severity"] == "warning"
        assert "activated" in alert["title"]
        assert "85.0%" in alert["message"]
        assert alert["details"]["enabled"] is True

    def test_load_shedding_disabled_alert(self):
        """Load Shedding deactivation alert."""
        from baldur.audit.cascade_notifications import cascade_load_shedding_alert

        alert = cascade_load_shedding_alert(
            enabled=False,
            current_load=0.45,
            threshold=0.7,
            dropped_count=150,
        )

        assert alert["severity"] == "info"
        assert "deactivated" in alert["title"]
        assert "returned to normal" in alert["message"]
        assert alert["details"]["enabled"] is False


# =============================================================================
# Daily Summary Tests
# =============================================================================


class TestCascadeSummary:
    """Daily summary alert tests."""

    def test_summary_basic(self):
        """Build a basic daily summary alert."""
        from baldur.audit.cascade_notifications import cascade_summary

        alert = cascade_summary(
            namespace="seoul",
            date="2026-01-23",
            total_events=150,
            events_by_trigger={
                "EMERGENCY_LEVEL_CHANGED": 100,
                "MANUAL_ACTIVATION": 50,
            },
            effects_by_action={
                "governance_strict": {"success": 95, "failure": 5},
                "canary_rollback": {"success": 48, "failure": 2},
            },
            integrity_valid=True,
            max_chain_depth=5,
        )

        assert alert["severity"] == "info"
        assert "daily summary" in alert["title"]
        assert "2026-01-23" in alert["title"]
        assert "✅ Valid" in alert["message"]
        assert alert["details"]["total_events"] == 150
        assert alert["details"]["integrity_valid"] is True

    def test_summary_integrity_invalid(self):
        """Summary when integrity is violated."""
        from baldur.audit.cascade_notifications import cascade_summary

        alert = cascade_summary(
            namespace="seoul",
            date="2026-01-23",
            total_events=100,
            events_by_trigger={},
            effects_by_action={},
            integrity_valid=False,
            max_chain_depth=3,
        )

        assert "❌ Violated" in alert["message"]
        assert alert["details"]["integrity_valid"] is False


# =============================================================================
# Fallback Recovery Alert Tests
# =============================================================================


class TestCascadeFallbackRecoveryAlert:
    """Local fallback recovery alert tests."""

    def test_recovery_success(self, tmp_path):
        """Recovery success alert."""
        from baldur.audit.cascade_notifications import (
            cascade_fallback_recovery_alert,
        )

        alert = cascade_fallback_recovery_alert(
            recovered_count=50,
            failed_count=0,
            fallback_path=str(tmp_path / "cascade_fallback.jsonl"),
        )

        assert alert["severity"] == "info"
        assert "success" in alert["title"]
        assert alert["details"]["recovered_count"] == 50
        assert alert["details"]["failed_count"] == 0

    def test_recovery_partial_success(self, tmp_path):
        """Partial success alert."""
        from baldur.audit.cascade_notifications import (
            cascade_fallback_recovery_alert,
        )

        alert = cascade_fallback_recovery_alert(
            recovered_count=45,
            failed_count=5,
            fallback_path=str(tmp_path / "cascade_fallback.jsonl"),
        )

        assert alert["severity"] == "warning"
        assert "partial success" in alert["title"]
        assert alert["details"]["failed_count"] == 5
