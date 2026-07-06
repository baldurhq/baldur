"""Unit tests for bootstrap admin server autostart (429 PR3-runtime).

Scope of `_start_admin_server_if_enabled`:
- Respects `AdminServerSettings.enabled` feature flag
- Respects `BALDUR_ADMIN_AUTOSTART=0` escape hatch
- `AdminAuthRequiredError` is re-raised (silently refusing is a security bug)
- Other exceptions are caught and logged; init() must continue

Does NOT test the HTTP server itself — covered by tests/unit/api/admin/test_server.py.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from baldur.bootstrap import _start_admin_server_if_enabled


@pytest.fixture(autouse=True)
def _reset_admin_state():
    """Clear the AdminServer + settings singletons between tests."""
    from baldur.api.admin import reset_admin_server
    from baldur.api.admin.registry import reset_admin_registry
    from baldur.settings.admin import reset_admin_server_settings

    reset_admin_server()
    reset_admin_server_settings()
    reset_admin_registry()
    yield
    reset_admin_server()
    reset_admin_server_settings()
    reset_admin_registry()


# =============================================================================
# Behavior — gating
# =============================================================================


class TestStartAdminServerIfEnabledBehavior:
    """autostart_gating behavior."""

    def test_disabled_feature_flag_skips_start(self, monkeypatch):
        """BALDUR_ADMIN_ENABLED=0 → start_admin_server is not invoked."""
        monkeypatch.setenv("BALDUR_ADMIN_ENABLED", "0")
        monkeypatch.setenv("BALDUR_ADMIN_AUTOSTART", "1")

        with patch("baldur.api.admin.start_admin_server", autospec=True) as mock_start:
            _start_admin_server_if_enabled()

        mock_start.assert_not_called()

    def test_disabled_autostart_env_skips_start(self, monkeypatch):
        """BALDUR_ADMIN_AUTOSTART=0 → start_admin_server is not invoked."""
        monkeypatch.setenv("BALDUR_ADMIN_ENABLED", "1")
        monkeypatch.setenv("BALDUR_ADMIN_AUTOSTART", "0")

        with patch("baldur.api.admin.start_admin_server", autospec=True) as mock_start:
            _start_admin_server_if_enabled()

        mock_start.assert_not_called()

    def test_enabled_and_autostart_invokes_start(self, monkeypatch):
        """Both flags on → start_admin_server called exactly once."""
        monkeypatch.setenv("BALDUR_ADMIN_ENABLED", "1")
        monkeypatch.setenv("BALDUR_ADMIN_AUTOSTART", "1")

        with patch("baldur.api.admin.start_admin_server", autospec=True) as mock_start:
            _start_admin_server_if_enabled()

        mock_start.assert_called_once()


# =============================================================================
# Behavior — error handling
# =============================================================================


class TestStartAdminServerErrorHandlingBehavior:
    """start failures: AdminAuthRequiredError re-raised, others swallowed."""

    def test_admin_auth_required_error_is_reraised(self, monkeypatch):
        """Non-localhost without key must fail loud — init() must see it."""
        from baldur.api.admin.auth import AdminAuthRequiredError

        monkeypatch.setenv("BALDUR_ADMIN_ENABLED", "1")
        monkeypatch.setenv("BALDUR_ADMIN_AUTOSTART", "1")

        with patch(
            "baldur.api.admin.start_admin_server",
            autospec=True,
            side_effect=AdminAuthRequiredError("non-localhost, no key"),
        ):
            with pytest.raises(AdminAuthRequiredError):
                _start_admin_server_if_enabled()

    # 525 D4: xdist mock_leak — caplog WARNING capture races with sibling
    # tests under -n 6 (project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="mock_leak"
    )
    def test_unexpected_exception_is_logged_and_swallowed(self, monkeypatch, caplog):
        """Any other exception is logged at WARNING; init() continues.

        Rationale: a broken admin server must not block framework startup
        (~30 other startup steps depend on init() completing).
        """
        monkeypatch.setenv("BALDUR_ADMIN_ENABLED", "1")
        monkeypatch.setenv("BALDUR_ADMIN_AUTOSTART", "1")

        with patch(
            "baldur.api.admin.start_admin_server",
            autospec=True,
            side_effect=OSError("port 9090 already in use"),
        ):
            with caplog.at_level(logging.WARNING):
                _start_admin_server_if_enabled()  # must not raise

        # Some form of failure log is emitted.
        assert any(
            "admin.autostart_failed" in str(record.msg)
            or "autostart_failed" in record.getMessage()
            for record in caplog.records
        ) or any("autostart_failed" in str(r) for r in caplog.records)
