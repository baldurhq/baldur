"""
Tests for GovernanceSettings and ChaosSettings.
"""

import pytest
from pydantic import ValidationError


class TestGovernanceSettings:
    """Tests for GovernanceSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.governance import reset_governance_settings

        reset_governance_settings()
        yield
        reset_governance_settings()

    def test_default_values(self):
        """기본값이 core/config.py:GovernanceConfig와 일치하는지 검증."""
        from baldur.settings.governance import GovernanceSettings

        settings = GovernanceSettings()

        assert settings.threshold_operator == 0.15
        assert settings.threshold_admin == 0.30
        assert settings.emergency_expiry_hours == 8
        assert settings.default_mode == "NORMAL"

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.governance import GovernanceSettings

        monkeypatch.setenv("BALDUR_GOVERNANCE_EMERGENCY_EXPIRY_HOURS", "12")

        settings = GovernanceSettings()

        assert settings.emergency_expiry_hours == 12

    def test_validation_mode(self):
        """default_mode 유효값 검증."""
        from baldur.settings.governance import GovernanceSettings

        # Valid modes
        for mode in ["NORMAL", "STRICT"]:
            settings = GovernanceSettings(default_mode=mode)
            assert settings.default_mode == mode

        # Invalid mode
        with pytest.raises(ValidationError):
            GovernanceSettings(default_mode="INVALID")

    def test_validation_threshold_range(self):
        """threshold_operator 범위 (0.01-1.0) 검증."""
        from baldur.settings.governance import GovernanceSettings

        with pytest.raises(ValidationError):
            GovernanceSettings(threshold_operator=0.0)

        with pytest.raises(ValidationError):
            GovernanceSettings(threshold_operator=1.1)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.governance import get_governance_settings

        settings1 = get_governance_settings()
        settings2 = get_governance_settings()

        assert settings1 is settings2


class TestChaosSettings:
    """Tests for ChaosSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.chaos import reset_chaos_settings

        reset_chaos_settings()
        yield
        reset_chaos_settings()

    def test_default_values(self):
        """기본값이 core/config.py:ChaosConfig와 일치하는지 검증."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings()

        assert settings.enabled is False  # Safety: disabled by default
        assert settings.max_blast_radius == 0.10
        assert settings.max_failure_rate == 0.20
        assert settings.auto_rollback_enabled is True
        assert settings.dry_run_default is True

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.chaos import ChaosSettings

        monkeypatch.setenv("BALDUR_CHAOS_MAX_BLAST_RADIUS", "0.20")

        settings = ChaosSettings()

        assert settings.max_blast_radius == 0.20

    def test_validation_blast_radius_range(self):
        """max_blast_radius 범위 (0.0-0.5) 검증."""
        from baldur.settings.chaos import ChaosSettings

        with pytest.raises(ValidationError):
            ChaosSettings(max_blast_radius=-0.1)

        with pytest.raises(ValidationError):
            ChaosSettings(max_blast_radius=0.6)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.chaos import get_chaos_settings

        settings1 = get_chaos_settings()
        settings2 = get_chaos_settings()

        assert settings1 is settings2


class TestChaosSettingsCrossProcessContract:
    """Cross-process zombie detection (390) settings contract verification."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        from baldur.settings.chaos import reset_chaos_settings

        reset_chaos_settings()
        yield
        reset_chaos_settings()

    def test_cross_process_detection_enabled_default_false(self):
        """cross_process_detection_enabled defaults to False (fail-safe)."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings()
        assert settings.cross_process_detection_enabled is False

    def test_worker_heartbeat_interval_default_40(self):
        """worker_heartbeat_interval_seconds defaults to 40 (TTL/3 ratio)."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings()
        assert settings.worker_heartbeat_interval_seconds == 40

    def test_worker_heartbeat_ttl_default_120(self):
        """worker_heartbeat_ttl_seconds defaults to 120 (2x hunt interval)."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings()
        assert settings.worker_heartbeat_ttl_seconds == 120

    def test_heartbeat_interval_minimum_boundary(self):
        """worker_heartbeat_interval_seconds boundary: ge=10."""
        from baldur.settings.chaos import ChaosSettings

        with pytest.raises(ValidationError):
            ChaosSettings(worker_heartbeat_interval_seconds=9)
        settings = ChaosSettings(worker_heartbeat_interval_seconds=10)
        assert settings.worker_heartbeat_interval_seconds == 10

    def test_heartbeat_interval_maximum_boundary(self):
        """worker_heartbeat_interval_seconds boundary: le=300."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings(worker_heartbeat_interval_seconds=300)
        assert settings.worker_heartbeat_interval_seconds == 300
        with pytest.raises(ValidationError):
            ChaosSettings(worker_heartbeat_interval_seconds=301)

    def test_heartbeat_ttl_minimum_boundary(self):
        """worker_heartbeat_ttl_seconds boundary: ge=30."""
        from baldur.settings.chaos import ChaosSettings

        with pytest.raises(ValidationError):
            ChaosSettings(worker_heartbeat_ttl_seconds=29)
        settings = ChaosSettings(worker_heartbeat_ttl_seconds=30)
        assert settings.worker_heartbeat_ttl_seconds == 30

    def test_heartbeat_ttl_maximum_boundary(self):
        """worker_heartbeat_ttl_seconds boundary: le=600."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings(worker_heartbeat_ttl_seconds=600)
        assert settings.worker_heartbeat_ttl_seconds == 600
        with pytest.raises(ValidationError):
            ChaosSettings(worker_heartbeat_ttl_seconds=601)

    def test_cross_process_env_override(self, monkeypatch):
        """Environment variable override for cross_process_detection_enabled."""
        from baldur.settings.chaos import ChaosSettings

        monkeypatch.setenv("BALDUR_CHAOS_CROSS_PROCESS_DETECTION_ENABLED", "true")
        settings = ChaosSettings()
        assert settings.cross_process_detection_enabled is True
