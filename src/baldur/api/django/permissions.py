"""
RBAC Permission Classes for Baldur Control API.

Provides role-based access control for the Baldur system:
- Viewer: Read-only access (dashboard, status, audit logs)
- Operator: Operational tasks (DLQ replay, archive)
- Admin: Full access (CB control, system enable/disable, config changes)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog
from rest_framework.permissions import BasePermission

from baldur.interfaces.web_framework import PermissionLevel

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView

logger = structlog.get_logger()


def _is_auth_disabled() -> bool:
    """Disable Baldur authentication in non-production environments only.

    Fail-Secure policy:
    - Production deploys can never bypass auth, even with
      ``DISABLE_BALDUR_AUTH=true``.
    - Production is detected via :func:`baldur.runtime.is_production` —
      the single canonical signal (``BALDUR_ENVIRONMENT == "production"``,
      strict equality). Legacy aliases (``prod``/``live``/``release``/
      ``stable``) and the ``DJANGO_SETTINGS_MODULE`` substring fallback
      are no longer honored; D15 hard-fails the known aliases at startup.
    - Production bypass attempts emit an ERROR log.
    """
    from baldur.runtime import is_production

    if is_production():
        if os.environ.get("DISABLE_BALDUR_AUTH", "").lower() in (
            "true",
            "1",
            "yes",
        ):
            logger.error("security.set_production_environment_auth")
        return False

    return os.environ.get("DISABLE_BALDUR_AUTH", "").lower() in (
        "true",
        "1",
        "yes",
    )


class IsBaldurAuthenticated(BasePermission):
    """
    Allow access to authenticated users only (test-environment bypass supported).

    When the DISABLE_BALDUR_AUTH=true environment variable is set,
    access is allowed without authentication.
    """

    message = "Authentication required."

    def has_permission(self, request: Request, view: APIView) -> bool:
        # Auth bypass in test environments
        if _is_auth_disabled():
            return True

        return bool(request.user and request.user.is_authenticated)


class IsViewer(BasePermission):
    """
    Read-only permission (Viewer role).

    Allowed operations:
    - GET /status, GET /dashboard
    - GET /audit (query audit logs)
    - GET /dlq/list, GET /dlq/<pk> (query DLQ)
    - GET /system/status (query system status)

    Conditions:
    - Authenticated user
    - staff or a member of the 'baldur_viewer' group
    """

    message = "Baldur viewer permission required. Must be a member of the baldur_viewer group."

    def has_permission(self, request: Request, view: APIView) -> bool:
        """
        Request-level permission check.

        Args:
            request: HTTP request object
            view: View object

        Returns:
            bool: Whether permission is granted
        """
        # Auth bypass in test environments
        if _is_auth_disabled():
            return True

        if not request.user or not request.user.is_authenticated:
            return False

        # Admin/Staff is always allowed
        if request.user.is_staff:
            return True

        # Check baldur_viewer, operator, admin group membership
        # (higher permissions include lower ones)
        return bool(
            request.user.groups.filter(
                name__in=["baldur_viewer", "baldur_operator", "baldur_admin"]
            ).exists()
        )


class IsOperator(BasePermission):
    """
    Operator permission (Operator role).

    Allowed operations:
    - All Viewer permissions
    - POST /dlq/replay (DLQ replay)
    - POST /dlq/cleanup/archive (DLQ archive)
    - POST /dlq/<pk>/retry (retry an individual entry)
    - POST /dlq/<pk>/resolve (resolve an individual entry)

    Conditions:
    - Authenticated user
    - superuser or a member of the 'baldur_operator' or 'baldur_admin' group
    """

    message = "Baldur operator permission required. Must be a member of the baldur_operator group."

    def has_permission(self, request: Request, view: APIView) -> bool:
        """
        Request-level permission check.

        Args:
            request: HTTP request object
            view: View object

        Returns:
            bool: Whether permission is granted
        """
        # Auth bypass in test environments
        if _is_auth_disabled():
            return True

        if not request.user or not request.user.is_authenticated:
            return False

        # Admin is always allowed
        if request.user.is_staff and request.user.is_superuser:
            return True

        # Check baldur_operator or baldur_admin group membership
        return bool(
            request.user.groups.filter(
                name__in=["baldur_operator", "baldur_admin"]
            ).exists()
        )


class IsBaldurAdmin(BasePermission):
    """
    Administrator permission (Admin role).

    Allowed operations:
    - All Operator permissions
    - POST /control/ (manual CB control: allow/block)
    - POST /system/enable, /system/disable (kill switch)
    - PUT /config/* (config changes)
    - POST /dlq/cleanup/purge (permanent DLQ deletion)
    - Chaos Engineering config changes

    Conditions:
    - Authenticated user
    - Django superuser or a member of the 'baldur_admin' group

    Security:
    - Fail-Secure: deny when the permission check fails
    """

    message = (
        "Baldur admin permission required. Must be a member of the baldur_admin group."
    )

    def has_permission(self, request: Request, view: APIView) -> bool:
        """
        Request-level permission check.

        Args:
            request: HTTP request object
            view: View object

        Returns:
            bool: Whether permission is granted

        Note:
            Fail-Secure: deny on exception
        """
        try:
            # Auth bypass in test environments
            if _is_auth_disabled():
                return True

            if not request.user or not request.user.is_authenticated:
                return False

            # Django superuser
            if request.user.is_superuser:
                return True

            # baldur_admin group
            return bool(request.user.groups.filter(name="baldur_admin").exists())

        except Exception as e:
            # Fail-Secure: deny on error
            logger.warning(
                "rbac.permission_check_failed_deny",
                error=e,
            )
            return False


class HasChaosTestPermission(BasePermission):
    """
    X-Test/Chaos experiment API permission (dual security layer - first-layer Django RBAC).

    Django RBAC-based permission class for the X-Test-Mode API.
    Combined with header validation (XTestModeMixin.check_chaos_permission)
    to form a dual security layer.

    Allow conditions (OR):
    - Test bypass: DISABLE_BALDUR_AUTH=true
    - Django superuser
    - baldur_admin group member
    - baldur_chaos_tester group member

    Block conditions (unconditional):
    - ENVIRONMENT == production (Fail-Secure)

    Logging:
    - Permission denied: WARNING (user, reason)
    - Production block: ERROR
    - Permission granted: DEBUG

    Security:
    - Fail-Secure: all exceptions are treated as denial
    """

    message = "X-Test/Chaos experiment permission denied. Must be a member of baldur_admin or baldur_chaos_tester group."

    def has_permission(self, request: Request, view: APIView) -> bool:
        """
        X-Test/Chaos API access permission check.

        Args:
            request: HTTP request object
            view: View object

        Returns:
            bool: Whether permission is granted

        Note:
            Fail-Secure: deny on any exception
        """
        try:
            # 1. Test-environment bypass (DISABLE_BALDUR_AUTH=true)
            if _is_auth_disabled():
                logger.debug(
                    "rbac.test_permission_bypassed_auth",
                    getattr=getattr(request, "path", "unknown"),
                )
                return True

            # 2. Production environments are unconditionally blocked (Fail-Secure).
            from baldur.runtime import is_production

            if is_production():
                logger.error(
                    "rbac.test_access_denied_production",
                    request_user=request.user,
                    getattr=getattr(request, "path", "unknown"),
                    client_ip=self._get_client_ip(request),
                )
                self.message = "X-Test/Chaos API is not available in production. Access blocked by security policy."
                return False

            # 3. Authentication required
            if not request.user or not request.user.is_authenticated:
                logger.warning(
                    "rbac.test_permission_denied_authenticated",
                    getattr=getattr(request, "path", "unknown"),
                )
                self.message = "Authentication required to access X-Test/Chaos API."
                return False

            # 4. Django superuser auto-allow
            if request.user.is_superuser:
                logger.debug(
                    "rbac.test_permission_granted_superuser",
                    request_user=request.user,
                )
                return True

            # 5. Group-based permission check (baldur_admin or baldur_chaos_tester)
            allowed_groups = ["baldur_admin", "baldur_chaos_tester"]
            if request.user.groups.filter(name__in=allowed_groups).exists():
                user_groups = list(
                    request.user.groups.filter(name__in=allowed_groups).values_list(
                        "name", flat=True
                    )
                )
                logger.debug(
                    "rbac.test_permission_granted_group",
                    request_user=request.user,
                    user_groups=user_groups,
                )
                return True

            # 6. No permission - deny
            logger.warning(
                "rbac.test_permission_denied_no",
                request_user=request.user,
                allowed_groups=allowed_groups,
            )
            return False

        except Exception as e:
            # Fail-Secure: deny on exception
            logger.exception(
                "rbac.test_permission_check_failed",
                error=e,
                getattr=getattr(request, "user", "unknown"),
            )
            self.message = "Permission check failed. Access denied."
            return False

    def _get_client_ip(self, request: Request) -> str | None:
        """Extract the client IP."""
        from baldur.utils.network import extract_client_ip

        return extract_client_ip(request)


# =============================================================================
# PermissionLevel → DRF Permission mapping
# =============================================================================


def get_permission_instances(
    level: PermissionLevel,
) -> list[BasePermission]:
    """Convert a PermissionLevel enum to DRF permission class instances.

    Args:
        level: Framework-independent permission level

    Returns:
        List of DRF BasePermission instances for the given level
    """
    _PERMISSION_MAP: dict[PermissionLevel, list[type[BasePermission]]] = {
        PermissionLevel.PUBLIC: [],
        PermissionLevel.AUTHENTICATED: [IsBaldurAuthenticated],
        PermissionLevel.VIEWER: [IsViewer],
        PermissionLevel.OPERATOR: [IsOperator],
        PermissionLevel.ADMIN: [IsBaldurAdmin],
    }
    classes = _PERMISSION_MAP.get(level, [IsBaldurAuthenticated])
    return [cls() for cls in classes]


__all__ = [
    "IsBaldurAuthenticated",
    "IsViewer",
    "IsOperator",
    "IsBaldurAdmin",
    "HasChaosTestPermission",
    "get_permission_instances",
]
