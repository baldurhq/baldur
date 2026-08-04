"""Django startup modules unit tests.

Tests RBACInitializer startup behaviors.

Note:
    EnvironmentAuditor.audit() tests were removed in 416 D21 — env_var snapshot
    logging was relocated from EnvironmentAuditor to baldur.bootstrap.init().
    Coverage now lives in tests/unit/audit/test_env_snapshot.py and
    tests/unit/test_bootstrap.py.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

# ── Django setup ──────────────────────────────────────────

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django  # noqa: E402

django.setup()

from baldur.adapters.django.startup.rbac_initializer import (  # noqa: E402
    RBACInitializer,
    create_baldur_groups,
)

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
