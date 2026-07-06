"""
Tests for ForensicSettings.
"""

import pytest
from pydantic import ValidationError


class TestForensicSettings:
    """Tests for ForensicSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.forensic import reset_forensic_settings

        reset_forensic_settings()
        yield
        reset_forensic_settings()

    def test_default_values(self):
        """기본값이 core/config.py:ForensicConfig와 일치하는지 검증."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()

        assert settings.error_message_max_length == 500
        assert settings.response_body_max_length == 5000
        assert settings.user_agent_max_length == 500
        assert settings.max_stack_frames == 50
        assert settings.max_context_size_bytes == 65536
        assert settings.include_local_variables is False
        assert settings.sanitize_sensitive_data is True

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.forensic import ForensicSettings

        monkeypatch.setenv("BALDUR_FORENSIC_MAX_STACK_FRAMES", "100")

        settings = ForensicSettings()

        assert settings.max_stack_frames == 100

    def test_validation_max_stack_frames_range(self):
        """max_stack_frames 범위 (10-200) 검증."""
        from baldur.settings.forensic import ForensicSettings

        with pytest.raises(ValidationError):
            ForensicSettings(max_stack_frames=5)

        with pytest.raises(ValidationError):
            ForensicSettings(max_stack_frames=201)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.forensic import get_forensic_settings

        settings1 = get_forensic_settings()
        settings2 = get_forensic_settings()

        assert settings1 is settings2
