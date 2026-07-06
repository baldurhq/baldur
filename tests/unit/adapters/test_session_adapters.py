"""
Session Invalidation Provider unit tests.

Tests for NoopSessionAdapter and DjangoSessionAdapter.

Test Categories:
    A. Contract: Noop adapter safe defaults
    B. Behavior: Django adapter delegation via mock Session model
"""

import sys
from unittest.mock import MagicMock, patch

from baldur.adapters.session.noop_adapter import NoopSessionAdapter

# =============================================================================
# A. Contract Tests
# =============================================================================


class TestNoopSessionAdapterContract:
    """NoopSessionAdapter returns safe defaults."""

    def test_invalidate_returns_empty_list(self):
        """Noop adapter returns empty list for invalidation."""
        adapter = NoopSessionAdapter()
        assert adapter.invalidate_user_sessions(1) == []

    def test_invalidate_returns_empty_for_string_user_id(self):
        """Noop adapter handles string user_id."""
        adapter = NoopSessionAdapter()
        assert adapter.invalidate_user_sessions("user-abc") == []

    def test_get_active_session_count_returns_zero(self):
        """Noop adapter returns 0 active sessions."""
        adapter = NoopSessionAdapter()
        assert adapter.get_active_session_count(1) == 0


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestDjangoSessionAdapterBehavior:
    """DjangoSessionAdapter delegates to Django Session model."""

    def _setup_django_mock(self):
        """Set up mock Django Session module to avoid AppRegistryNotReady."""
        mock_session_module = MagicMock()
        self._mock_session_model = mock_session_module.Session
        sys.modules.setdefault("django.contrib.sessions", MagicMock())
        sys.modules["django.contrib.sessions.models"] = mock_session_module
        return self._mock_session_model

    def _teardown_django_mock(self):
        """Remove mock Django modules."""
        sys.modules.pop("django.contrib.sessions.models", None)

    @patch("baldur.utils.time.utc_now", autospec=True)
    def test_invalidate_deletes_matching_sessions(self, mock_utc_now):
        """invalidate_user_sessions deletes sessions matching user_id."""
        mock_session_model = self._setup_django_mock()
        try:
            from baldur.adapters.django.session_adapter import DjangoSessionAdapter

            mock_utc_now.return_value = MagicMock()

            session_match = MagicMock()
            session_match.get_decoded.return_value = {"_auth_user_id": "42"}
            session_nomatch = MagicMock()
            session_nomatch.get_decoded.return_value = {"_auth_user_id": "99"}

            mock_session_model.objects.filter.return_value = [
                session_match,
                session_nomatch,
            ]

            adapter = DjangoSessionAdapter()
            result = adapter.invalidate_user_sessions(42)

            assert result == ["django_db_sessions(1)"]
            session_match.delete.assert_called_once()
            session_nomatch.delete.assert_not_called()
        finally:
            self._teardown_django_mock()

    @patch("baldur.utils.time.utc_now", autospec=True)
    def test_get_active_session_count_counts_matching(self, mock_utc_now):
        """get_active_session_count counts sessions matching user_id."""
        mock_session_model = self._setup_django_mock()
        try:
            from baldur.adapters.django.session_adapter import DjangoSessionAdapter

            mock_utc_now.return_value = MagicMock()

            session1 = MagicMock()
            session1.get_decoded.return_value = {"_auth_user_id": "5"}
            session2 = MagicMock()
            session2.get_decoded.return_value = {"_auth_user_id": "5"}
            session3 = MagicMock()
            session3.get_decoded.return_value = {"_auth_user_id": "10"}

            mock_session_model.objects.filter.return_value = [
                session1,
                session2,
                session3,
            ]

            adapter = DjangoSessionAdapter()
            count = adapter.get_active_session_count(5)

            assert count == 2
        finally:
            self._teardown_django_mock()

    @patch("baldur.utils.time.utc_now", autospec=True)
    def test_invalidate_no_sessions_returns_zero_count(self, mock_utc_now):
        """invalidate_user_sessions with no matching sessions returns count 0."""
        mock_session_model = self._setup_django_mock()
        try:
            from baldur.adapters.django.session_adapter import DjangoSessionAdapter

            mock_utc_now.return_value = MagicMock()
            mock_session_model.objects.filter.return_value = []

            adapter = DjangoSessionAdapter()
            result = adapter.invalidate_user_sessions(999)

            assert result == ["django_db_sessions(0)"]
        finally:
            self._teardown_django_mock()
