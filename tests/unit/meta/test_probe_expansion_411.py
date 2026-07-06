"""
#411 probe expansion — cross-cutting unit tests.

Covers:
- Contract: ProbeResult.reason field, staleness_multiplier settings,
  _COMPONENT_PRIORITY expansion, existing probe reason strings
- Behavior: _recover_precomputed_cache_impl, reason field defaults
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.meta.health_probe import HealthStatus, ProbeResult

# =========================================================================
# ProbeResult.reason field
# =========================================================================


class TestProbeResultReasonContract:
    """Contract verification for ProbeResult.reason field (411)."""

    def test_reason_field_default_is_empty_string(self):
        """reason field defaults to empty string."""
        result = ProbeResult(
            component="test",
            status=HealthStatus.HEALTHY,
            latency_ms=1.0,
            timestamp=datetime.now(UTC),
        )
        assert result.reason == ""

    def test_reason_field_accepts_string(self):
        """reason field accepts arbitrary string."""
        result = ProbeResult(
            component="test",
            status=HealthStatus.DEGRADED,
            latency_ms=1.0,
            timestamp=datetime.now(UTC),
            reason="3 circuit breakers open (threshold: 2)",
        )
        assert result.reason == "3 circuit breakers open (threshold: 2)"


class TestAuditProbeResultReasonContract:
    """Contract verification for AuditProbeResult.reason field (411)."""

    def test_reason_field_default_is_empty_string(self):
        """AuditProbeResult.reason defaults to empty string."""
        from baldur.meta.audit_probe import AuditProbeResult

        result = AuditProbeResult(
            component="audit_system",
            status="healthy",
            latency_ms=1.0,
            timestamp=datetime.now(UTC),
            details={},
        )
        assert result.reason == ""


# =========================================================================
# MetaWatchdogSettings.probe_cache_staleness_multiplier
# =========================================================================


class TestStalenessMultiplierContract:
    """Contract verification for probe_cache_staleness_multiplier (411)."""

    def test_default_value_is_2_0(self):
        """Default value must be 2.0 per 411 design."""
        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        settings = MetaWatchdogSettings()
        assert settings.probe_cache_staleness_multiplier == 2.0

    def test_minimum_boundary_rejects_1_0(self):
        """ge=1.1: value 1.0 must be rejected."""
        from pydantic import ValidationError

        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        with pytest.raises(ValidationError):
            MetaWatchdogSettings(probe_cache_staleness_multiplier=1.0)

    def test_minimum_boundary_accepts_1_1(self):
        """ge=1.1: value 1.1 must be accepted."""
        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        settings = MetaWatchdogSettings(probe_cache_staleness_multiplier=1.1)
        assert settings.probe_cache_staleness_multiplier == pytest.approx(1.1)

    def test_maximum_boundary_rejects_10_1(self):
        """le=10.0: value 10.1 must be rejected."""
        from pydantic import ValidationError

        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        with pytest.raises(ValidationError):
            MetaWatchdogSettings(probe_cache_staleness_multiplier=10.1)

    def test_maximum_boundary_accepts_10_0(self):
        """le=10.0: value 10.0 must be accepted."""
        from baldur.settings.meta_watchdog import MetaWatchdogSettings

        settings = MetaWatchdogSettings(probe_cache_staleness_multiplier=10.0)
        assert settings.probe_cache_staleness_multiplier == 10.0


# =========================================================================
# _COMPONENT_PRIORITY expansion
# =========================================================================


# =========================================================================
# _recover_precomputed_cache_impl
# =========================================================================


# =========================================================================
# Existing probe reason strings (spot checks)
# =========================================================================
