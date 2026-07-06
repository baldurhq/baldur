"""
API View Settings 단위 테스트 - Postmortem 관련 설정.

settings/api_view.py의 postmortem 관련 설정 테스트.
"""

import os
from unittest.mock import patch


class TestApiViewSettingsPostmortem:
    """Postmortem 관련 Settings 테스트."""

    def test_new_postmortem_settings_fields_exist(self):
        """새 postmortem 설정 필드가 존재하는지 확인."""
        from baldur.settings.api_view import (
            get_api_view_settings,
            reset_api_view_settings,
        )

        reset_api_view_settings()
        settings = get_api_view_settings()

        # 새 필드명 확인
        assert hasattr(settings, "auto_postmortem_min_duration")
        assert hasattr(settings, "postmortem_history_limit")

    def test_default_values(self):
        """기본값이 올바른지 확인."""
        from baldur.settings.api_view import (
            get_api_view_settings,
            reset_api_view_settings,
        )

        reset_api_view_settings()
        settings = get_api_view_settings()

        assert settings.auto_postmortem_min_duration == 30
        assert settings.postmortem_history_limit == 100

    def test_postmortem_notification_settings(self):
        """Post-mortem 알림 설정이 존재하는지 확인."""
        from baldur.settings.api_view import (
            get_api_view_settings,
            reset_api_view_settings,
        )

        reset_api_view_settings()
        settings = get_api_view_settings()

        assert hasattr(settings, "postmortem_notification_min_duration")
        assert settings.postmortem_notification_min_duration == 60


class TestApiViewSettingsEnvVariables:
    """환경 변수 테스트."""

    def test_auto_postmortem_min_duration_from_env(self):
        """환경 변수로 auto_postmortem_min_duration 설정 가능한지 확인."""
        from baldur.settings.api_view import (
            ApiViewSettings,
            reset_api_view_settings,
        )

        reset_api_view_settings()

        with patch.dict(
            os.environ, {"BALDUR_API_VIEW_AUTO_POSTMORTEM_MIN_DURATION": "120"}
        ):
            settings = ApiViewSettings()
            assert settings.auto_postmortem_min_duration == 120
