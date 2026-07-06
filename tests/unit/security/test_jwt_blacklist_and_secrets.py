"""JWT blacklist hook registration and secret-validation tests.

Covers ``apps.py._register_jwt_blacklist_hook()`` and the centralized secret
gate ``baldur.bootstrap._validate_critical_secrets()``. Per 632 D7 the secret
gate was lifted out of the Django-only ``apps.py._validate_secrets`` method
into ``baldur.init()`` so it fires on every framework adapter; the
secret-validation assertions below therefore target the bootstrap function.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.django.apps import BaldurConfig
from baldur.bootstrap import _validate_critical_secrets
from baldur.services.security.hooks import (
    get_session_invalidation_hooks,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app_config():
    """BaldurConfig 인스턴스."""
    return BaldurConfig("baldur.adapters.django", __import__("baldur"))


# =============================================================================
# JWT Blacklist Hook Registration Tests
# =============================================================================


class TestJWTBlacklistHookRegistrationBehavior:
    """JWT 블랙리스트 훅 등록 동작 검증."""

    def test_hook_registered_when_token_blacklist_installed(self, app_config):
        """token_blacklist 앱 설치 시 훅이 등록되는지 확인."""
        with patch("django.apps.apps.is_installed", return_value=True):
            app_config._register_jwt_blacklist_hook()

        hooks = get_session_invalidation_hooks()
        assert len(hooks) == 1

    def test_hook_not_registered_when_token_blacklist_missing(self, app_config):
        """token_blacklist 미설치 시 훅이 등록되지 않는지 확인."""
        with patch("django.apps.apps.is_installed", return_value=False):
            app_config._register_jwt_blacklist_hook()

        hooks = get_session_invalidation_hooks()
        assert len(hooks) == 0

    def test_hook_skipped_on_import_error(self, app_config):
        """ImportError 발생 시 훅 등록이 건너뛰어지는지 확인."""
        with patch(
            "django.apps.apps.is_installed",
            side_effect=ImportError("No module found"),
        ):
            # 예외가 전파되지 않아야 함
            app_config._register_jwt_blacklist_hook()

        hooks = get_session_invalidation_hooks()
        assert len(hooks) == 0

    def test_registered_hook_blacklists_user_tokens(self, app_config):
        """등록된 훅이 사용자의 OutstandingToken을 블랙리스트에 추가하는지 확인."""
        with patch("django.apps.apps.is_installed", return_value=True):
            app_config._register_jwt_blacklist_hook()

        hooks = get_session_invalidation_hooks()
        assert len(hooks) == 1

        # 훅 호출 시 OutstandingToken/BlacklistedToken 사용 확인
        mock_outstanding = MagicMock()
        mock_token_1 = MagicMock()
        mock_token_2 = MagicMock()
        mock_outstanding.objects.filter.return_value = [mock_token_1, mock_token_2]

        mock_blacklisted = MagicMock()
        mock_blacklisted.objects.get_or_create.return_value = (MagicMock(), True)

        with patch.dict(
            "sys.modules",
            {
                "rest_framework_simplejwt": MagicMock(),
                "rest_framework_simplejwt.token_blacklist": MagicMock(),
                "rest_framework_simplejwt.token_blacklist.models": MagicMock(
                    OutstandingToken=mock_outstanding,
                    BlacklistedToken=mock_blacklisted,
                ),
            },
        ):
            result = hooks[0](42)

        mock_outstanding.objects.filter.assert_called_once_with(user_id=42)
        assert mock_blacklisted.objects.get_or_create.call_count == 2
        assert result == "jwt_blacklisted(2)"

    def test_registered_hook_returns_empty_when_no_tokens(self, app_config):
        """블랙리스트할 토큰이 없을 때 빈 문자열을 반환하는지 확인."""
        with patch("django.apps.apps.is_installed", return_value=True):
            app_config._register_jwt_blacklist_hook()

        hooks = get_session_invalidation_hooks()

        mock_outstanding = MagicMock()
        mock_outstanding.objects.filter.return_value = []

        with patch.dict(
            "sys.modules",
            {
                "rest_framework_simplejwt": MagicMock(),
                "rest_framework_simplejwt.token_blacklist": MagicMock(),
                "rest_framework_simplejwt.token_blacklist.models": MagicMock(
                    OutstandingToken=mock_outstanding,
                    BlacklistedToken=MagicMock(),
                ),
            },
        ):
            result = hooks[0](42)

        assert result == ""


# =============================================================================
# Secrets Validation Tests (632 D7 — centralized bootstrap gate)
# =============================================================================


class TestValidateCriticalSecretsBehavior:
    """Centralized secret-gate behavior (``bootstrap._validate_critical_secrets``).

    Migrated from the removed ``apps.py._validate_secrets`` method. The wrapper
    keeps the same log-event names and the same best-effort-vs-fail-loud
    contract, only the home moved from the Django adapter to ``baldur.init()``.
    The inner ``validate_required_secrets`` is imported lazily inside the
    function, so patching it at its source module still intercepts the call.
    """

    def test_validate_secrets_called_successfully(self):
        """validate_required_secrets() is invoked by the gate."""
        with patch(
            "baldur.settings.secrets.validate_required_secrets",
            return_value={"critical": [], "warning": [], "info": []},
        ) as mock_validate:
            _validate_critical_secrets()

            mock_validate.assert_called_once()

    def test_validate_secrets_logs_critical_count(self):
        """Missing CRITICAL secret -> error log."""
        with patch(
            "baldur.settings.secrets.validate_required_secrets",
            return_value={"critical": ["encryption_key"], "warning": [], "info": []},
        ):
            with patch("baldur.bootstrap.logger") as mock_logger:
                _validate_critical_secrets()

                mock_logger.error.assert_called_once()
                assert (
                    mock_logger.error.call_args[0][0]
                    == "baldur.critical_secrets_configured_check"
                )

    def test_validate_secrets_logs_warning_count(self):
        """Missing IMPORTANT secret -> warning log."""
        with patch(
            "baldur.settings.secrets.validate_required_secrets",
            return_value={"critical": [], "warning": ["database_password"], "info": []},
        ):
            with patch("baldur.bootstrap.logger") as mock_logger:
                _validate_critical_secrets()

                mock_logger.warning.assert_called_once()
                assert (
                    mock_logger.warning.call_args[0][0]
                    == "baldur.important_secrets_configured_check"
                )

    def test_validate_secrets_logs_success(self):
        """All secrets present -> success info log."""
        with patch(
            "baldur.settings.secrets.validate_required_secrets",
            return_value={"critical": [], "warning": [], "info": []},
        ):
            with patch("baldur.bootstrap.logger") as mock_logger:
                _validate_critical_secrets()

                mock_logger.info.assert_called_once()
                assert (
                    mock_logger.info.call_args[0][0]
                    == "baldur.all_secrets_validated_successfully"
                )

    def test_validate_secrets_critical_failure_blocks_startup(self):
        """Production CRITICAL secret missing -> RuntimeError re-raised (abort boot)."""
        with patch(
            "baldur.settings.secrets.validate_required_secrets",
            side_effect=RuntimeError("CRITICAL secrets not configured in production"),
        ):
            with pytest.raises(RuntimeError, match="CRITICAL secrets not configured"):
                _validate_critical_secrets()

    def test_validate_secrets_critical_failure_logs_resolution_guide(self):
        """RuntimeError path logs a critical resolution line before re-raising."""
        with patch(
            "baldur.settings.secrets.validate_required_secrets",
            side_effect=RuntimeError("CRITICAL secrets not configured"),
        ):
            with patch("baldur.bootstrap.logger") as mock_logger:
                with pytest.raises(RuntimeError):
                    _validate_critical_secrets()

                mock_logger.critical.assert_called_once()
                log_message = mock_logger.critical.call_args[0][0]
                assert log_message == "baldur.secrets_validation_failed_resolution"
                # The RuntimeError is forwarded on the error kwarg.
                assert "error" in mock_logger.critical.call_args[1]

    def test_validate_secrets_other_error_continues(self):
        """Non-RuntimeError failures are best-effort (swallowed)."""
        with patch(
            "baldur.settings.secrets.validate_required_secrets",
            side_effect=Exception("Unexpected error"),
        ):
            # Must not propagate.
            _validate_critical_secrets()

    def test_validate_secrets_import_error_continues(self):
        """A failing inner read degrades to best-effort, not a boot abort."""
        with patch(
            "baldur.settings.secrets.validate_required_secrets",
            side_effect=ImportError("No module named 'baldur.settings.secrets'"),
        ):
            # ImportError -> handled by the except-Exception arm, not propagated.
            _validate_critical_secrets()
