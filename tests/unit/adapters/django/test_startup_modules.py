"""Django startup modules unit tests.

Tests MetricHydrator and RBACInitializer startup behaviors including
idempotency and graceful degradation.

Note:
    EnvironmentAuditor.audit() tests were removed in 416 D21 — env_var snapshot
    logging was relocated from EnvironmentAuditor to baldur.bootstrap.init().
    Coverage now lives in tests/unit/audit/test_env_snapshot.py and
    tests/unit/test_bootstrap.py.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# ── Django setup ──────────────────────────────────────────

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django  # noqa: E402

django.setup()

from baldur.adapters.django.startup.metric_hydrator import (  # noqa: E402
    MetricHydrator,
)
from baldur.adapters.django.startup.rbac_initializer import (  # noqa: E402
    RBACInitializer,
    create_baldur_groups,
)

# =============================================================================
# Behavior: MetricHydrator.hydrate() idempotency
# =============================================================================


class TestMetricHydratorBehavior:
    """MetricHydrator.hydrate() duplicate guard and idempotency tests."""

    @pytest.fixture(autouse=True)
    def _reset_hydrator(self):
        """Reset MetricHydrator state before and after each test."""
        MetricHydrator.reset_state()
        yield
        MetricHydrator.reset_state()

    @patch(
        "baldur.adapters.django.startup.metric_hydrator.threading.Timer",
        autospec=True,
    )
    def test_hydrate_schedules_timer(self, mock_timer_cls):
        """hydrate() creates and starts a daemon Timer."""
        mock_timer = MagicMock()
        mock_timer_cls.return_value = mock_timer

        MetricHydrator.hydrate()

        mock_timer_cls.assert_called_once()
        mock_timer.start.assert_called_once()

    @patch(
        "baldur.adapters.django.startup.metric_hydrator.threading.Timer",
        autospec=True,
    )
    def test_hydrate_twice_only_schedules_once(self, mock_timer_cls):
        """Calling hydrate() twice only creates one Timer (duplicate guard)."""
        mock_timer = MagicMock()
        mock_timer_cls.return_value = mock_timer

        MetricHydrator.hydrate()
        MetricHydrator.hydrate()

        mock_timer_cls.assert_called_once()

    @patch(
        "baldur.adapters.django.startup.metric_hydrator.threading.Timer",
        autospec=True,
    )
    def test_reset_state_allows_re_hydration(self, mock_timer_cls):
        """After reset_state(), hydrate() can schedule again."""
        mock_timer = MagicMock()
        mock_timer_cls.return_value = mock_timer

        MetricHydrator.hydrate()
        MetricHydrator.reset_state()
        MetricHydrator.hydrate()

        assert mock_timer_cls.call_count == 2

    def test_reset_state_resets_hydration_guard(self):
        """reset_state() clears _hydration_done flag."""
        MetricHydrator._hydration_done = True

        MetricHydrator.reset_state()

        assert MetricHydrator._hydration_done is False


# =============================================================================
# Behavior: RBACInitializer.connect_post_migrate()
# =============================================================================


class TestRBACInitializerBehavior:
    """RBACInitializer.connect_post_migrate() signal connection tests."""

    @patch(
        "baldur.adapters.django.startup.rbac_initializer.post_migrate",
        autospec=True,
    )
    def test_connect_post_migrate_connects_signal(self, mock_signal):
        """connect_post_migrate() connects signal with correct dispatch_uid."""
        mock_app_config = MagicMock()

        RBACInitializer.connect_post_migrate(mock_app_config)

        mock_signal.connect.assert_called_once_with(
            create_baldur_groups,
            sender=mock_app_config,
            dispatch_uid="baldur_create_rbac_groups",
        )

    @patch(
        "baldur.adapters.django.startup.rbac_initializer.post_migrate",
        autospec=True,
    )
    def test_connect_post_migrate_passes_app_config_as_sender(self, mock_signal):
        """connect_post_migrate() uses provided app_config as sender."""
        mock_app_config = MagicMock()
        mock_app_config.name = "baldur"

        RBACInitializer.connect_post_migrate(mock_app_config)

        call_kwargs = mock_signal.connect.call_args
        assert call_kwargs[1]["sender"] is mock_app_config


# =============================================================================
# Behavior: All startup modules handle ImportError gracefully
# =============================================================================


class TestStartupModulesGracefulDegradationBehavior:
    """All startup modules handle missing dependencies gracefully."""

    @patch(
        "baldur.adapters.django.startup.metric_hydrator.threading.Timer",
        autospec=True,
    )
    def test_metric_hydrator_hydrate_gauges_handles_import_error(self, mock_timer):
        """MetricHydrator._hydrate_gauges() handles ImportError."""
        with patch.dict("sys.modules", {"baldur.metrics.reconciler": None}):
            # Should not raise
            MetricHydrator._hydrate_gauges()
