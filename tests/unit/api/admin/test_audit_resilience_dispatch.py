"""Admin-transport dispatch guard for the degraded-mode force route — 635.

Verification target (the G1/G2 param-name-divergence class):
- The admin registry resolves ``POST /resilience/degraded-mode/{action}`` for
  both ``enter`` and ``exit`` and populates the ``action`` path param, so the
  shared handler ``degraded_mode_force`` reaches its action branch instead of
  falling through to ``400 Unknown action: None``.

Unlike ``test_registry_phase2b.py`` (registration *shape* only), this drives the
full chain ``resolve`` → ``RequestContext`` → ``route.handler`` and asserts the
manager call at argument level — mirroring the Django precedent
``test_enter_action_calls_force_degraded`` — so the ``body → handler → manager``
``reason`` plumbing is proven to survive the admin transport intact.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.api.admin.registry import get_admin_registry, reset_admin_registry
from baldur.interfaces.web_framework import RequestContext


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_admin_registry()
    yield
    reset_admin_registry()


class TestDegradedModeForceAdminDispatchBehavior:
    """The admin route must reach the handler's action branch, not 400."""

    def test_enter_dispatch_populates_action_and_calls_force_degraded(self):
        """POST /resilience/degraded-mode/enter → force_degraded(reason)."""
        reg = get_admin_registry()
        resolved = reg.resolve("POST", "/resilience/degraded-mode/enter")
        assert resolved is not None
        route, params = resolved
        assert params == {"action": "enter"}

        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": True}
        ctx = RequestContext(
            method=route.method,
            path="/resilience/degraded-mode/enter",
            path_params=params,
            json_body={"reason": "test reason"},
        )

        with patch(
            "baldur.audit.get_degraded_mode_manager",
            return_value=mock_manager,
        ):
            response = route.handler(ctx)

        assert response.status_code == 200
        mock_manager.force_degraded.assert_called_once_with("test reason")
        mock_manager.force_normal.assert_not_called()

    def test_exit_dispatch_populates_action_and_calls_force_normal(self):
        """POST /resilience/degraded-mode/exit → force_normal()."""
        reg = get_admin_registry()
        resolved = reg.resolve("POST", "/resilience/degraded-mode/exit")
        assert resolved is not None
        route, params = resolved
        assert params == {"action": "exit"}

        mock_manager = MagicMock()
        mock_manager.get_status.return_value = {"degraded": False}
        ctx = RequestContext(
            method=route.method,
            path="/resilience/degraded-mode/exit",
            path_params=params,
        )

        with patch(
            "baldur.audit.get_degraded_mode_manager",
            return_value=mock_manager,
        ):
            response = route.handler(ctx)

        assert response.status_code == 200
        mock_manager.force_normal.assert_called_once()
        mock_manager.force_degraded.assert_not_called()
