"""Unit tests for BaldurConfig._warn_if_admission_middleware_missing (591 D8).

Django reads ``settings.MIDDLEWARE`` once at startup, so an operator who never
adds ``AdmissionControlMiddleware`` silently gets no admission control. The
init-time warning fires only for the actual gap: admission enabled AND the
per-tier Bulkhead registry present (PRO) AND the middleware path absent.

These tests mock the PRO Bulkhead registry rather than importing ``baldur_pro``,
so the file stays OSS-classified and needs no ``requires_pro`` marker.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

_MIDDLEWARE_PATH = "baldur.api.django.admission_control.AdmissionControlMiddleware"
_WARNING_EVENT = "baldur.admission_control_middleware_not_registered"


class TestWarnIfAdmissionMiddlewareMissingBehavior:
    """Init-time signal for the PRO-admission-enabled-but-unregistered gap."""

    @patch("baldur.adapters.django.apps.logger")
    @patch("django.conf.settings")
    @patch("baldur.factory.registry.ProviderRegistry")
    @patch("baldur.settings.admission_control.get_admission_control_settings")
    def test_warn_if_admission_pro_enabled_middleware_absent_emits_warning(
        self, mock_get_settings, mock_registry, mock_django_settings, mock_logger
    ):
        """Enabled + PRO registry present + middleware absent -> one warning."""
        mock_get_settings.return_value = MagicMock(enabled=True)
        mock_registry.bulkhead_registry.safe_get.return_value = MagicMock()
        mock_django_settings.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]

        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig._warn_if_admission_middleware_missing()

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == _WARNING_EVENT

    @patch("baldur.adapters.django.apps.logger")
    @patch("django.conf.settings")
    @patch("baldur.factory.registry.ProviderRegistry")
    @patch("baldur.settings.admission_control.get_admission_control_settings")
    def test_warn_if_admission_middleware_present_no_warning(
        self, mock_get_settings, mock_registry, mock_django_settings, mock_logger
    ):
        """Registered middleware -> the gap does not exist, so no signal."""
        mock_get_settings.return_value = MagicMock(enabled=True)
        mock_registry.bulkhead_registry.safe_get.return_value = MagicMock()
        mock_django_settings.MIDDLEWARE = [_MIDDLEWARE_PATH]

        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig._warn_if_admission_middleware_missing()

        mock_logger.warning.assert_not_called()

    @patch("baldur.adapters.django.apps.logger")
    @patch("django.conf.settings")
    @patch("baldur.factory.registry.ProviderRegistry")
    @patch("baldur.settings.admission_control.get_admission_control_settings")
    def test_warn_if_admission_oss_no_bulkhead_registry_no_warning(
        self, mock_get_settings, mock_registry, mock_django_settings, mock_logger
    ):
        """OSS (registry absent) -> admission is a clean no-op, so no false alarm."""
        mock_get_settings.return_value = MagicMock(enabled=True)
        mock_registry.bulkhead_registry.safe_get.return_value = None
        mock_django_settings.MIDDLEWARE = []

        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig._warn_if_admission_middleware_missing()

        mock_logger.warning.assert_not_called()

    @patch("baldur.adapters.django.apps.logger")
    @patch("baldur.factory.registry.ProviderRegistry")
    @patch("baldur.settings.admission_control.get_admission_control_settings")
    def test_warn_if_admission_disabled_skips_check_entirely(
        self, mock_get_settings, mock_registry, mock_logger
    ):
        """enabled=False short-circuits before the registry / settings probe."""
        mock_get_settings.return_value = MagicMock(enabled=False)

        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig._warn_if_admission_middleware_missing()

        mock_registry.bulkhead_registry.safe_get.assert_not_called()
        mock_logger.warning.assert_not_called()

    @patch("baldur.adapters.django.apps.logger")
    @patch("baldur.factory.registry.ProviderRegistry")
    @patch("baldur.settings.admission_control.get_admission_control_settings")
    def test_warn_if_admission_unexpected_error_logs_check_failed_not_raise(
        self, mock_get_settings, mock_registry, mock_logger
    ):
        """A probe failure is swallowed into a check-failed warning (fail-soft)."""
        mock_get_settings.return_value = MagicMock(enabled=True)
        mock_registry.bulkhead_registry.safe_get.side_effect = RuntimeError("boom")

        from baldur.adapters.django.apps import BaldurConfig

        BaldurConfig._warn_if_admission_middleware_missing()

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == (
            "baldur.admission_middleware_check_failed"
        )
