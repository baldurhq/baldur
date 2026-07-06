"""
Django 세션 시그널 핸들러 동작 검증.

adapters/django/signal_hooks.py의 on_user_login_register_session /
on_user_logout_unregister_session이 UserSessionRegistry를 올바르게 호출하는지
검증한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.adapters.django.signal_hooks import (
    on_user_login_register_session,
    on_user_logout_unregister_session,
)


class TestLoginSignalHandlerBehavior:
    """user_logged_in 시그널 핸들러 동작 검증."""

    @patch("baldur.adapters.django.signal_hooks.get_user_session_registry")
    def test_registers_session_on_login(self, mock_get_registry):
        """로그인 시 registry.register() 호출."""
        mock_registry = MagicMock()
        mock_get_registry.return_value = mock_registry

        request = MagicMock()
        request.session.session_key = "test_session_key"
        user = MagicMock()
        user.pk = 42

        on_user_login_register_session(sender=None, request=request, user=user)

        mock_registry.register.assert_called_once_with(42, "test_session_key")

    @patch("baldur.adapters.django.signal_hooks.get_user_session_registry")
    def test_creates_session_key_if_missing(self, mock_get_registry):
        """session_key가 None일 때 request.session.save() 호출."""
        mock_registry = MagicMock()
        mock_get_registry.return_value = mock_registry

        request = MagicMock()
        request.session.session_key = None
        request.session.save.side_effect = lambda: setattr(
            request.session, "session_key", "new_key"
        )
        user = MagicMock()
        user.pk = 1

        on_user_login_register_session(sender=None, request=request, user=user)

        request.session.save.assert_called_once()

    def test_skips_when_user_is_none(self):
        """user가 None일 때 registry 호출 안 함 (예외 없이 종료)."""
        request = MagicMock()
        request.session.session_key = "test_key"

        on_user_login_register_session(sender=None, request=request, user=None)

    def test_skips_when_user_pk_is_none(self):
        """user.pk가 None일 때 registry 호출 안 함."""
        request = MagicMock()
        request.session.session_key = "test_key"
        user = MagicMock()
        user.pk = None

        on_user_login_register_session(sender=None, request=request, user=user)


class TestLogoutSignalHandlerBehavior:
    """user_logged_out 시그널 핸들러 동작 검증."""

    @patch("baldur.adapters.django.signal_hooks.get_user_session_registry")
    def test_unregisters_session_on_logout(self, mock_get_registry):
        """로그아웃 시 registry.unregister() 호출."""
        mock_registry = MagicMock()
        mock_get_registry.return_value = mock_registry

        request = MagicMock()
        request.session.session_key = "logout_session"
        user = MagicMock()
        user.pk = 42

        on_user_logout_unregister_session(sender=None, request=request, user=user)

        mock_registry.unregister.assert_called_once_with(42, "logout_session")

    def test_skips_when_no_session_key(self):
        """session_key가 None일 때 registry 호출 안 함."""
        request = MagicMock()
        request.session.session_key = None
        user = MagicMock()
        user.pk = 1

        on_user_logout_unregister_session(sender=None, request=request, user=user)

    def test_skips_when_user_is_none(self):
        """user가 None일 때 registry 호출 안 함."""
        request = MagicMock()
        request.session.session_key = "test_key"

        on_user_logout_unregister_session(sender=None, request=request, user=None)
