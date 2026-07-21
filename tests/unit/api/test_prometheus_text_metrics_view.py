"""Unit tests for PrometheusTextMetricsView and IPC __init__ sidecar cleanup.

PrometheusTextMetricsView tests require Django settings (DRF dependency).
IPC __init__ cleanup tests are pure unit tests.
"""

from __future__ import annotations

import pytest


class TestPrometheusTextMetricsViewContract:
    """Contract: view configuration per design spec."""

    @pytest.fixture(autouse=True)
    def _check_django(self):
        """Skip if Django settings not configured."""
        try:
            import django.conf

            django.conf.settings.REST_FRAMEWORK  # noqa: B018
        except Exception:
            pytest.skip("Django settings not configured")

    def test_permission_level_is_public(self):
        """Prometheus scraping endpoint requires no authentication.

        429 Phase 2a: HandlerAPIView subclasses declare framework-
        independent ``permission_level`` — PUBLIC maps to an empty
        permission list at dispatch time (equivalent to AllowAny).
        """
        from baldur.api.django.views.health import PrometheusTextMetricsView
        from baldur.interfaces.web_framework import PermissionLevel

        assert PrometheusTextMetricsView.permission_level == PermissionLevel.PUBLIC

    def test_permissions_resolve_to_empty_list(self):
        """PUBLIC level resolves to zero permission instances at dispatch."""
        from baldur.api.django.permissions import get_permission_instances
        from baldur.api.django.views.health import PrometheusTextMetricsView

        instances = get_permission_instances(PrometheusTextMetricsView.permission_level)
        assert instances == []


class TestIPCInitSidecarCleanupContract:
    """Contract: IPC __init__ no longer exports sidecar-only symbols."""

    def test_removed_sidecar_exports(self):
        """Sidecar-only symbols must NOT be in __all__."""
        from baldur.adapters.ipc import __all__ as ipc_all

        sidecar_only = [
            "UDSServer",
            "UDSClient",
            "FailOpenUDSClient",
            "SidecarGRPCServer",
            "SidecarAuthenticator",
            "EventStreamProxy",
            "SidecarIPCProbe",
            "sidecar_metrics",
            "record_ipc_request",
        ]
        for name in sidecar_only:
            assert name not in ipc_all, f"{name} should have been removed"

    def test_retained_library_mode_exports(self):
        """Library-mode symbols must still be in __all__."""
        from baldur.adapters.ipc import __all__ as ipc_all

        library_mode = [
            "IPCStateCache",
            "CBStateCache",
            "CBStateSnapshot",
            "get_cb_state_snapshot",
            "reset_cb_state_snapshot",
            "RequestHandler",
            "IPCError",
        ]
        for name in library_mode:
            assert name in ipc_all, f"{name} should be retained"

    def test_reset_cb_state_snapshot_is_importable(self):
        """reset_cb_state_snapshot must be importable (used in post_fork)."""
        from baldur.adapters.ipc import reset_cb_state_snapshot

        assert callable(reset_cb_state_snapshot)
