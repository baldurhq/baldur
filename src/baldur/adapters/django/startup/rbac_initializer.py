"""
RBAC group auto-creation via post_migrate signal.

Creates the three Baldur RBAC groups idempotently using
Django's ``Group.objects.get_or_create()`` pattern.
"""

from __future__ import annotations

import structlog
from django.db.models.signals import post_migrate

__all__ = [
    "BALDUR_GROUPS",
    "RBACInitializer",
    "create_baldur_groups",
]

logger = structlog.get_logger()

# RBAC group definitions
BALDUR_GROUPS: list[str] = [
    "baldur_viewer",
    "baldur_operator",
    "baldur_admin",
]


def create_baldur_groups(sender, **kwargs):
    """
    Create RBAC groups for Baldur system.

    Called via post_migrate signal - runs only after migrations complete.
    Uses get_or_create for idempotency.

    Note: Environment variable snapshot is logged in ready() instead,
    because env vars can change on every restart (not just migrations).
    """
    try:
        from django.contrib.auth.models import Group

        created_groups = []
        existing_groups = []

        for group_name in BALDUR_GROUPS:
            group, created = Group.objects.get_or_create(name=group_name)
            if created:
                created_groups.append(group_name)
            else:
                existing_groups.append(group_name)

        if created_groups:
            logger.info(
                "baldur.rbac_groups_created",
                created_groups=created_groups,
            )

        if existing_groups and created_groups:
            logger.debug(
                "baldur.rbac_groups_already_existed",
                existing_groups=existing_groups,
            )

    except Exception as e:
        # Best-effort: system continues even if this fails
        logger.warning(
            "baldur.create_rbac_groups_failed",
            error=e,
        )


class RBACInitializer:
    """RBAC group auto-creation via post_migrate signal."""

    @staticmethod
    def connect_post_migrate(app_config) -> None:
        """Connect post_migrate signal for RBAC group creation.

        Args:
            app_config: The Django AppConfig instance (sender).
        """
        post_migrate.connect(
            create_baldur_groups,
            sender=app_config,
            dispatch_uid="baldur_create_rbac_groups",
        )

    @staticmethod
    def create_groups(sender, **kwargs) -> None:
        """Create RBAC groups (idempotent via get_or_create).

        Thin delegate kept for backward-compatibility with any code
        that references ``RBACInitializer.create_groups`` directly.
        """
        create_baldur_groups(sender, **kwargs)
