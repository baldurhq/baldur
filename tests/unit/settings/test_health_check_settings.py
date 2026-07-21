"""
HealthCheckSettings unit tests.

Test classification (UNIT_TEST_GUIDELINES §0):
- Contract: designed default/constraint contracts, asserted as hardcoded values
- Behavior: env-var override, singleton pair, boundary behavior

Source under test:
- settings/health_check.py (HealthCheckSettings)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.health_check import (
    HealthCheckSettings,
    get_health_check_settings,
    reset_health_check_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset the settings singleton around every test."""
    reset_health_check_settings()
    yield
    reset_health_check_settings()


# =============================================================================
# Contract Tests — designed contract values
# =============================================================================


class TestHealthCheckSettingsDefaultContract:
    """HealthCheckSettings default-value design contract."""

    def test_checker_cache_ttl_seconds_default(self):
        """TTLCacheStrategy default cache TTL: 5.0s."""
        assert HealthCheckSettings().checker_cache_ttl_seconds == 5.0

    def test_tcp_info_timeout_seconds_default(self):
        """LinuxTCPInfoStrategy timeout: 0.1s."""
        assert HealthCheckSettings().tcp_info_timeout_seconds == 0.1

    def test_socket_timeout_seconds_default(self):
        """SimpleSocketStrategy timeout: 1.0s."""
        assert HealthCheckSettings().socket_timeout_seconds == 1.0

    def test_probe_cb_open_threshold_default(self):
        """Circuit Breaker OPEN DEGRADED threshold: 3."""
        assert HealthCheckSettings().probe_cb_open_threshold == 3

    def test_probe_active_recoveries_threshold_default(self):
        """Active-recoveries DEGRADED threshold: 10."""
        assert HealthCheckSettings().probe_active_recoveries_threshold == 10

    def test_probe_memory_usage_threshold_default(self):
        """Redis memory DEGRADED threshold: 0.8."""
        assert HealthCheckSettings().probe_memory_usage_threshold == pytest.approx(0.8)

    def test_probe_worker_join_timeout_default(self):
        """Worker thread join timeout: 2.0s."""
        assert HealthCheckSettings().probe_worker_join_timeout == 2.0

    def test_readiness_probe_timeout_seconds_default(self):
        """Readiness probe round budget: 0.5s.

        Half the kubelet ``timeoutSeconds`` default (1s), so Baldur always
        answers before the probe itself times out and the depool decision stays
        Baldur's rather than the orchestrator's.
        """
        assert HealthCheckSettings().readiness_probe_timeout_seconds == 0.5

    def test_readiness_cache_ttl_seconds_default(self):
        """Readiness verdict cache TTL: 5.0s — under the 10s probe period."""
        assert HealthCheckSettings().readiness_cache_ttl_seconds == 5.0

    def test_readiness_timeout_fail_direction_default(self):
        """Timeout fail direction: depool honestly and fast."""
        assert HealthCheckSettings().readiness_timeout_fail_direction == "not_ready"

    def test_field_count(self):
        """HealthCheckSettings has exactly 10 fields."""
        assert len(HealthCheckSettings.model_fields) == 10

    def test_env_prefix(self):
        """Env-var prefix: BALDUR_HEALTH_CHECK_."""
        assert (
            HealthCheckSettings.model_config.get("env_prefix") == "BALDUR_HEALTH_CHECK_"
        )


# =============================================================================
# Boundary Tests — field constraint boundaries (§8.1)
# =============================================================================


class TestHealthCheckSettingsBoundaryContract:
    """HealthCheckSettings field boundary contracts."""

    def test_checker_cache_ttl_below_minimum_rejected(self):
        """checker_cache_ttl_seconds: below ge=0.5 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(checker_cache_ttl_seconds=0.4)

    def test_checker_cache_ttl_at_minimum_accepted(self):
        """checker_cache_ttl_seconds: ge=0.5 boundary → accepted."""
        s = HealthCheckSettings(checker_cache_ttl_seconds=0.5)
        assert s.checker_cache_ttl_seconds == 0.5

    def test_checker_cache_ttl_above_maximum_rejected(self):
        """checker_cache_ttl_seconds: above le=60.0 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(checker_cache_ttl_seconds=60.1)

    def test_probe_cb_open_threshold_below_minimum_rejected(self):
        """probe_cb_open_threshold: below ge=1 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(probe_cb_open_threshold=0)

    def test_probe_memory_usage_threshold_above_maximum_rejected(self):
        """probe_memory_usage_threshold: above le=1.0 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(probe_memory_usage_threshold=1.1)

    def test_probe_memory_usage_threshold_at_minimum_accepted(self):
        """probe_memory_usage_threshold: ge=0.1 boundary → accepted."""
        s = HealthCheckSettings(probe_memory_usage_threshold=0.1)
        assert s.probe_memory_usage_threshold == pytest.approx(0.1)

    def test_readiness_probe_timeout_below_minimum_rejected(self):
        """readiness_probe_timeout_seconds: below ge=0.05 → ValidationError.

        The floor keeps the budget above the cost of spawning the probe round
        itself, where every alias would report a timeout it never had.
        """
        with pytest.raises(ValidationError):
            HealthCheckSettings(readiness_probe_timeout_seconds=0.04)

    def test_readiness_probe_timeout_at_minimum_accepted(self):
        """readiness_probe_timeout_seconds: ge=0.05 boundary → accepted."""
        s = HealthCheckSettings(readiness_probe_timeout_seconds=0.05)
        assert s.readiness_probe_timeout_seconds == pytest.approx(0.05)

    def test_readiness_probe_timeout_at_maximum_accepted(self):
        """readiness_probe_timeout_seconds: le=30.0 boundary → accepted."""
        s = HealthCheckSettings(readiness_probe_timeout_seconds=30.0)
        assert s.readiness_probe_timeout_seconds == 30.0

    def test_readiness_probe_timeout_above_maximum_rejected(self):
        """readiness_probe_timeout_seconds: above le=30.0 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(readiness_probe_timeout_seconds=30.1)

    def test_readiness_cache_ttl_at_zero_accepted(self):
        """readiness_cache_ttl_seconds: ge=0.0 boundary → accepted (0 disables)."""
        s = HealthCheckSettings(readiness_cache_ttl_seconds=0.0)
        assert s.readiness_cache_ttl_seconds == 0.0

    def test_readiness_cache_ttl_below_zero_rejected(self):
        """readiness_cache_ttl_seconds: below ge=0.0 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(readiness_cache_ttl_seconds=-0.1)

    def test_readiness_cache_ttl_above_maximum_rejected(self):
        """readiness_cache_ttl_seconds: above le=60.0 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(readiness_cache_ttl_seconds=60.1)

    @pytest.mark.parametrize("direction", ["not_ready", "ready"])
    def test_readiness_timeout_fail_direction_accepts_both_verdicts(self, direction):
        """readiness_timeout_fail_direction: the Literal domain is exactly these."""
        s = HealthCheckSettings(readiness_timeout_fail_direction=direction)
        assert s.readiness_timeout_fail_direction == direction

    def test_readiness_timeout_fail_direction_rejects_unknown_verdict(self):
        """A value outside the Literal domain is rejected, not silently coerced.

        The knob decides whether a stalled dependency depools the pod, so a
        typo must fail loudly at startup rather than pick a direction.
        """
        with pytest.raises(ValidationError):
            HealthCheckSettings(readiness_timeout_fail_direction="maybe")


# =============================================================================
# Behavior Tests — env-var override, singleton
# =============================================================================


class TestHealthCheckSettingsEnvOverrideBehavior:
    """Env-var override behavior."""

    def test_env_override_checker_cache_ttl(self, monkeypatch):
        """Override via BALDUR_HEALTH_CHECK_CHECKER_CACHE_TTL_SECONDS."""
        monkeypatch.setenv("BALDUR_HEALTH_CHECK_CHECKER_CACHE_TTL_SECONDS", "10.0")
        s = HealthCheckSettings()
        assert s.checker_cache_ttl_seconds == 10.0

    def test_env_override_probe_cb_open_threshold(self, monkeypatch):
        """Override via BALDUR_HEALTH_CHECK_PROBE_CB_OPEN_THRESHOLD."""
        monkeypatch.setenv("BALDUR_HEALTH_CHECK_PROBE_CB_OPEN_THRESHOLD", "5")
        s = HealthCheckSettings()
        assert s.probe_cb_open_threshold == 5

    def test_env_override_readiness_probe_timeout(self, monkeypatch):
        """Override via BALDUR_HEALTH_CHECK_READINESS_PROBE_TIMEOUT_SECONDS."""
        monkeypatch.setenv("BALDUR_HEALTH_CHECK_READINESS_PROBE_TIMEOUT_SECONDS", "1.5")
        s = HealthCheckSettings()
        assert s.readiness_probe_timeout_seconds == 1.5

    def test_env_override_readiness_timeout_fail_direction(self, monkeypatch):
        """Override via BALDUR_HEALTH_CHECK_READINESS_TIMEOUT_FAIL_DIRECTION.

        This is the operator-facing knob of the three, so its env path is the
        one that has to work.
        """
        monkeypatch.setenv(
            "BALDUR_HEALTH_CHECK_READINESS_TIMEOUT_FAIL_DIRECTION", "ready"
        )
        s = HealthCheckSettings()
        assert s.readiness_timeout_fail_direction == "ready"


class TestHealthCheckSettingsSingletonBehavior:
    """HealthCheckSettings singleton pair behavior."""

    def test_get_returns_same_instance(self):
        """get_health_check_settings() returns the same instance."""
        s1 = get_health_check_settings()
        s2 = get_health_check_settings()
        assert s1 is s2

    def test_reset_clears_cached_instance(self):
        """A new instance is built after reset."""
        s1 = get_health_check_settings()
        reset_health_check_settings()
        s2 = get_health_check_settings()
        assert s1 is not s2
