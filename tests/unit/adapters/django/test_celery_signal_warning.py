"""Unit tests for BaldurConfig._warn_if_celery_signals_missing (320).

Tests celery signal registration detection and warning log emission.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# =========================================================================
# Behavior Tests
# =========================================================================


class TestWarnIfCelerySignalsMissingBehavior:
    """_warn_if_celery_signals_missing Celery 시그널 미등록 감지 동작 검증."""

    @pytest.fixture(autouse=True)
    def _reset_auto_config(self):
        """각 테스트 후 AutoConfigSettings 싱글톤 캐시를 리셋."""
        from baldur.settings.auto_config import reset_auto_config_settings

        reset_auto_config_settings()
        yield
        reset_auto_config_settings()

    # structlog BoundLogger uses dynamic __getattr__ for log methods,
    # so autospec=True cannot detect .warning — spec omitted intentionally.
    @patch("baldur.adapters.django.apps.logger")
    @patch("celery.signals.task_failure", autospec=True)
    def test_no_receivers_emits_warning(self, mock_task_failure, mock_logger):
        """Celery 시그널 수신자가 없으면 경고 로그를 발생시킨다."""
        mock_task_failure.receivers = []

        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig._warn_if_celery_signals_missing()

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "baldur.celery_signals_not_registered"

    @patch("baldur.adapters.django.apps.logger")
    @patch("celery.signals.task_failure", autospec=True)
    def test_with_receivers_no_warning(self, mock_task_failure, mock_logger):
        """Celery 시그널 수신자가 있으면 경고를 발생시키지 않는다."""
        mock_task_failure.receivers = [("handler", MagicMock())]

        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig._warn_if_celery_signals_missing()

        mock_logger.warning.assert_not_called()

    @patch("baldur.adapters.django.apps.logger")
    def test_celery_not_installed_silently_passes(self, mock_logger):
        """Celery가 설치되지 않은 환경에서는 조용히 넘어간다."""
        # sys.modules에 None을 넣으면 CPython이 ImportError를 발생시킨다.
        # _warn_if_celery_signals_missing()은 매 호출마다 local import하므로
        # 이미 캐싱된 모듈 참조 문제 없이 정확히 동작한다.
        with patch.dict("sys.modules", {"celery.signals": None}):
            from baldur.adapters.django.apps import BaldurConfig

            BaldurConfig._warn_if_celery_signals_missing()

        mock_logger.warning.assert_not_called()

    @patch("baldur.adapters.django.apps.logger")
    @patch("celery.signals.task_failure", autospec=True)
    def test_disabled_via_settings_skips_check(self, mock_task_failure, mock_logger):
        """celery_signal_warning=False이면 검사를 건너뛴다."""
        mock_task_failure.receivers = []

        with patch.dict(
            "os.environ", {"BALDUR_AUTO_CONFIG_CELERY_SIGNAL_WARNING": "false"}
        ):
            from baldur.adapters.django.apps import BaldurConfig

            BaldurConfig._warn_if_celery_signals_missing()

        mock_logger.warning.assert_not_called()
