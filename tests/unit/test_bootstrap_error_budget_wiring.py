"""Unit tests for bootstrap Error Budget flip-to-activate wiring (622 D1).

``_configure_error_budget_if_enabled()`` is the bootstrap step that makes Error
Budget a genuine flip-to-activate Deferred feature: gated by
``ErrorBudgetSettings.enabled`` (BALDUR_ERROR_BUDGET_ENABLED). When OFF (the v1.0
default) the service stays unwired and its consumers honestly skip; when ON it
wires the DLQ windowed-inflow stats source so consumers read real data. A
pre-init operator ``configure_error_budget_service`` is never clobbered, and a
DLQ retention shorter than the SLO window emits a disclosure WARNING.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import structlog

from baldur.bootstrap import _configure_error_budget_if_enabled
from baldur_pro.services.error_budget import (
    configure_error_budget_service,
    get_error_budget_service,
    is_error_budget_service_wired,
    reset_error_budget_service,
)

_SETTINGS = "baldur.settings.error_budget.get_error_budget_settings"
_DLQ_SETTINGS = "baldur.settings.dlq.get_dlq_settings"


def _events(logs) -> list[str]:
    return [entry["event"] for entry in logs]


class TestErrorBudgetBootstrapWiring:
    """_configure_error_budget_if_enabled() flag/precedence/disclosure behavior."""

    def setup_method(self):
        reset_error_budget_service()

    def teardown_method(self):
        reset_error_budget_service()

    def test_skips_wiring_when_feature_disabled(self):
        """Flag OFF (v1.0 default) → no wiring, honest startup_skipped log."""
        # Given the feature flag is off.
        with patch(_SETTINGS, return_value=SimpleNamespace(enabled=False)):
            with structlog.testing.capture_logs() as logs:
                _configure_error_budget_if_enabled()

        # Then the service stays unwired and the skip is logged with its reason.
        assert is_error_budget_service_wired() is False
        assert "error_budget.startup_skipped" in _events(logs)

    def test_wires_dlq_stats_when_feature_enabled(self):
        """Flag ON → the DLQ stats source is wired into the service."""
        # Given the feature flag is on and retention matches the SLO window.
        with (
            patch(_SETTINGS, return_value=SimpleNamespace(enabled=True)),
            patch(_DLQ_SETTINGS, return_value=SimpleNamespace(retention_days=30)),
        ):
            with structlog.testing.capture_logs() as logs:
                _configure_error_budget_if_enabled()

        # Then the service is wired with real stats and emits the wired event.
        assert is_error_budget_service_wired() is True
        assert "error_budget.stats_wired" in _events(logs)

    def test_skips_when_already_wired_operator_precedence(self):
        """A pre-init operator configure_ is never clobbered by auto-wiring."""

        # Given an operator already wired a custom stats source pre-init.
        def _operator_stats(start_time, end_time, **kwargs):
            return {"total_errors": 1, "source": "dlq"}

        configure_error_budget_service(get_failed_operation_stats=_operator_stats)
        operator_service = get_error_budget_service()

        # When bootstrap runs with the flag on.
        with (
            patch(_SETTINGS, return_value=SimpleNamespace(enabled=True)),
            patch(_DLQ_SETTINGS, return_value=SimpleNamespace(retention_days=30)),
        ):
            with structlog.testing.capture_logs() as logs:
                _configure_error_budget_if_enabled()

        # Then the operator's singleton is preserved (not reconstructed).
        assert get_error_budget_service() is operator_service
        assert is_error_budget_service_wired() is True
        assert "error_budget.startup_skipped" in _events(logs)

    def test_warns_when_retention_shorter_than_slo_window(self):
        """Operator-shortened DLQ retention emits a window-mismatch WARNING."""
        with (
            patch(_SETTINGS, return_value=SimpleNamespace(enabled=True)),
            patch(_DLQ_SETTINGS, return_value=SimpleNamespace(retention_days=7)),
        ):
            with structlog.testing.capture_logs() as logs:
                _configure_error_budget_if_enabled()

        # The undercount-risk disclosure fires, and wiring still proceeds.
        assert "error_budget.retention_window_mismatch" in _events(logs)
        assert is_error_budget_service_wired() is True

    def test_no_retention_warning_when_retention_matches_window(self):
        """Aligned retention (== SLO window) does not warn."""
        with (
            patch(_SETTINGS, return_value=SimpleNamespace(enabled=True)),
            patch(_DLQ_SETTINGS, return_value=SimpleNamespace(retention_days=30)),
        ):
            with structlog.testing.capture_logs() as logs:
                _configure_error_budget_if_enabled()

        assert "error_budget.retention_window_mismatch" not in _events(logs)
