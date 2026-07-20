"""
Actor Context - automatically tracks who performed an action

Problem:
- actor_id has to be filled into AuditEntry by hand
- Forget it and there is no way to trace who changed a setting
- A cache issue leaving a setting unchanged becomes a major incident

Solution:
- ActorContext tracks the current user thread-locally
- Set automatically by Django middleware
- Covers both the admin pages and API calls

Usage:
    # Set automatically from Django middleware
    class ActorMiddleware:
        def __call__(self, request):
            with ActorContext.set_actor(
                actor_id=request.user.email,
                actor_type="user",
                source="web"
            ):
                return self.get_response(request)

    # Read the current actor from anywhere
    actor = ActorContext.get_current()
    print(f"Current user: {actor.actor_id}")

    # Explicit setting inside a Celery task
    @task
    def my_task(actor_id: str):
        with ActorContext.set_actor(actor_id=actor_id, actor_type="scheduler"):
            do_work()
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

logger = structlog.get_logger()

# =============================================================================
# Celery Header Constants for ActorContext Propagation
# =============================================================================

CELERY_HEADER_ACTOR_ID = "baldur_actor_id"
CELERY_HEADER_ACTOR_TYPE = "baldur_actor_type"
CELERY_HEADER_ACTOR_SOURCE = "baldur_actor_source"
CELERY_HEADER_ACTOR_IP = "baldur_actor_ip"
CELERY_HEADER_ACTOR_SESSION = "baldur_actor_session"
CELERY_HEADER_ACTOR_ROLES = "baldur_actor_roles"  # json.dumps(list)

# Context variable for thread-safe actor tracking
_current_actor: contextvars.ContextVar[Actor | None] = contextvars.ContextVar(
    "current_actor", default=None
)


# RBAC role priority constants
RBAC_ROLE_PRIORITY: dict[str, int] = {
    "baldur_admin": 3,
    "baldur_operator": 2,
    "baldur_viewer": 1,
}


@dataclass
class Actor(SerializableMixin):
    """
    Information about the principal performing the current action.

    Attributes:
        actor_id: User identifier (email, username, user_id, etc.)
        actor_type: Principal type (user, system, scheduler, api_client, etc.)
        source: Request origin (web, api, celery, management_command, etc.)
        ip_address: Request IP (for security auditing)
        session_id: Session ID (links actions within the same session)
        set_at: When the actor was set
        metadata: Extra information (user-agent, request_id, etc.)
        roles: RBAC role list (RBAC-audit integration)
    """

    actor_id: str
    actor_type: str = "user"
    source: str = "unknown"
    ip_address: str | None = None
    session_id: str | None = None
    set_at: datetime = field(default_factory=lambda: utc_now())
    metadata: dict[str, Any] = field(default_factory=dict)
    roles: list[str] = field(default_factory=list)

    @property
    def highest_role(self) -> str:
        """Return the highest-privilege RBAC role."""
        if not self.roles:
            return self.actor_type  # fallback to actor_type
        return max(
            self.roles,
            key=lambda r: RBAC_ROLE_PRIORITY.get(r, 0),
            default=self.actor_type,
        )


# Sentinel for anonymous/system actor
SYSTEM_ACTOR = Actor(
    actor_id="system",
    actor_type="system",
    source="internal",
    roles=[],
)

ANONYMOUS_ACTOR = Actor(
    actor_id="anonymous",
    actor_type="anonymous",
    source="unknown",
    roles=[],
)


class ActorContext:
    """
    Thread-safe context for tracking who is performing an action.

    Uses Python's contextvars for async/thread safety.
    Works with Django, Celery, asyncio, and plain threads.
    """

    @classmethod
    @contextmanager
    def set_actor(
        cls,
        actor_id: str,
        actor_type: str = "user",
        source: str = "unknown",
        ip_address: str | None = None,
        session_id: str | None = None,
        roles: list[str] | None = None,
        **metadata: Any,
    ) -> Generator[Actor, None, None]:
        """
        Set the current actor for this context.

        Usage:
            with ActorContext.set_actor(actor_id="admin@example.com"):
                # All audit logs in this block will have this actor
                do_something()
        """
        actor = Actor(
            actor_id=actor_id,
            actor_type=actor_type,
            source=source,
            ip_address=ip_address,
            session_id=session_id,
            metadata=metadata,
            roles=roles or [],
        )
        token = _current_actor.set(actor)
        try:
            logger.debug(
                "actor_context.set_actor",
                actor_id=actor_id,
                actor_type=actor_type,
                source=source,
                actor_roles=actor.roles,
            )
            yield actor
        finally:
            _current_actor.reset(token)
            logger.debug(
                "actor_context.cleared_actor",
                actor_id=actor_id,
            )

    @classmethod
    def set_actor_from_django_request(
        cls, request: Any
    ) -> contextlib.AbstractContextManager[Actor]:
        """
        Set actor from Django request object.

        Extracts user info, IP address, session ID, and RBAC roles automatically.

        RBAC: roles are extracted too, and actor_type is set to the highest
        privilege among them.
        """
        # Extract user info
        if hasattr(request, "user") and request.user.is_authenticated:
            actor_id = getattr(request.user, "email", None) or str(request.user.pk)

            # Extract RBAC roles
            roles = cls._extract_baldur_roles(request.user)

            # Set actor_type to the highest RBAC role (when one exists)
            actor_type = cls._get_highest_role(roles) if roles else "user"
        else:
            actor_id = "anonymous"
            actor_type = "anonymous"
            roles = []

        # Extract IP address
        ip_address = cls._get_client_ip(request)

        # Extract session ID
        session_id = None
        if hasattr(request, "session") and request.session.session_key:
            session_id = request.session.session_key

        # Determine source
        source = "api" if "/api/" in request.path else "web"

        return cls.set_actor(
            actor_id=actor_id,
            actor_type=actor_type,
            source=source,
            ip_address=ip_address,
            session_id=session_id,
            roles=roles,
            path=request.path,
            method=request.method,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

    @classmethod
    def _extract_baldur_roles(cls, user: Any) -> list[str]:
        """
        Extract the user's baldur RBAC groups.

        Filters the Django User's groups down to those with the baldur_ prefix.

        Args:
            user: Django User object

        Returns:
            List of group names carrying the baldur_ prefix
        """
        try:
            if hasattr(user, "groups"):
                return list(
                    user.groups.filter(name__startswith="baldur_").values_list(
                        "name", flat=True
                    )
                )
        except Exception:
            logger.debug(
                "actor_context.extract_rbac_roles_failed",
                user=user,
            )
        return []

    @classmethod
    def _get_highest_role(cls, roles: list[str]) -> str:
        """
        Return the highest-privilege RBAC role.

        Ordering: baldur_admin > baldur_operator > baldur_viewer.

        Args:
            roles: RBAC role list

        Returns:
            Name of the highest-privilege role, or 'user' when there is none
        """
        if not roles:
            return "user"
        return max(
            roles,
            key=lambda r: RBAC_ROLE_PRIORITY.get(r, 0),
            default="user",
        )

    @classmethod
    def _get_client_ip(cls, request: Any) -> str | None:
        """Extract client IP from Django request.

        Fail-open: on extraction failure it returns None so Actor creation still
        proceeds. Actor.ip_address is Optional[str], so None is a safe default.
        """
        try:
            from baldur.utils.network import extract_client_ip

            return extract_client_ip(request)
        except Exception as e:
            logger.warning(
                "actor_context.extract_client_ip_failed",
                error=e,
            )
            return None

    @classmethod
    def get_current(cls) -> Actor:
        """
        Get the current actor.

        Returns SYSTEM_ACTOR if no actor is set (background jobs, etc.)
        """
        actor = _current_actor.get()
        if actor is None:
            return SYSTEM_ACTOR
        return actor

    @classmethod
    def get_current_or_none(cls) -> Actor | None:
        """Get the current actor, or None if not set."""
        return _current_actor.get()

    @classmethod
    def is_set(cls) -> bool:
        """Check if an actor is currently set."""
        return _current_actor.get() is not None

    @classmethod
    def require_actor(cls) -> Actor:
        """
        Get the current actor, raising if not set.

        Use this when an action MUST have an actor (security-critical operations).
        """
        actor = _current_actor.get()
        if actor is None:
            raise RuntimeError(
                "ActorContext not set. Security-critical operations require an actor. "
                "Use ActorContext.set_actor() or ensure middleware is configured."
            )
        return actor

    @classmethod
    def is_anonymous_or_system(cls) -> bool:
        """
        Check if current actor is anonymous or system (potentially untracked).

        Returns True if:
        - No actor is set (will default to SYSTEM_ACTOR)
        - Actor is anonymous
        - Actor is system

        Use this to detect potentially untracked operations.
        """
        actor = _current_actor.get()
        if actor is None:
            return True
        return actor.actor_type in ("system", "anonymous")


class ActorTrackingWarning(UserWarning):
    """Warning for untracked sensitive operations."""

    pass


def warn_if_untracked(operation: str) -> None:
    """
    Emit warning if current operation is not properly tracked.

    Use this in sensitive operations to alert about missing actor context.

    Usage:
        def force_open_circuit_breaker(service_name: str):
            warn_if_untracked("force_open_circuit_breaker")
            # ... do the operation
    """
    import warnings

    if ActorContext.is_anonymous_or_system():
        warnings.warn(
            f"Sensitive operation '{operation}' performed without actor tracking. "
            f"Current actor: {ActorContext.get_current().actor_id}. "
            f"Consider using ActorContext.set_actor() for audit trail.",
            ActorTrackingWarning,
            stacklevel=2,
        )
        logger.warning(
            "actor_context.event",
            operation=operation,
            ActorContext=ActorContext.get_current().actor_id,
        )


def require_actor_for_action(action_name: str) -> Actor:
    """
    Require an actor for a specific action, with detailed error message.

    Use for operations that MUST be tracked (config changes, manual overrides, etc.)

    Usage:
        def change_critical_config(key, value):
            actor = require_actor_for_action("change_critical_config")
            # actor is guaranteed to be a real user, not system/anonymous
    """
    actor = ActorContext.get_current()

    if actor.actor_type in ("system", "anonymous"):
        raise RuntimeError(
            f"Action '{action_name}' requires a tracked actor. "
            f"Current actor '{actor.actor_id}' ({actor.actor_type}) is not sufficient. "
            f"This action must be performed by a logged-in user. "
            f"If this is a background job, use ActorContext.set_actor() to specify who initiated it."
        )

    return actor


def get_audit_actor_info() -> dict[str, Any]:
    """
    Get actor info formatted for AuditEntry.

    Returns dict with actor_id, actor_type, actor_roles that can be unpacked into AuditEntry.

    Includes actor_roles as well, to support RBAC-audit integration.

    Usage:
        entry = AuditEntry(
            action=AuditAction.CONFIG_CHANGE,
            **get_audit_actor_info(),  # Adds actor_id, actor_type, actor_roles
            ...
        )
    """
    actor = ActorContext.get_current()
    return {
        "actor_id": actor.actor_id,
        "actor_type": actor.actor_type,
        "actor_roles": actor.roles,
    }


# =============================================================================
# Celery task support
# =============================================================================


def get_actor_for_celery() -> dict[str, Any]:
    """
    Get current actor info for passing to Celery task.

    Passes the roles along as well, so RBAC roles survive into the Celery task.

    Usage (in view/api):
        from baldur.context import get_actor_for_celery

        # Pass actor info to Celery task
        my_task.delay(
            order_id=123,
            actor_info=get_actor_for_celery(),
        )

    Usage (in task):
        @app.task
        def my_task(order_id: int, actor_info: dict):
            with restore_actor_from_celery(actor_info):
                do_work()  # ActorContext is now set
    """
    actor = ActorContext.get_current()
    return {
        "actor_id": actor.actor_id,
        "actor_type": actor.actor_type,
        "source": f"celery_from_{actor.source}",
        "ip_address": actor.ip_address,
        "session_id": actor.session_id,
        "original_set_at": actor.set_at.isoformat(),
        "roles": actor.roles,  # propagate RBAC roles
    }


@contextmanager
def restore_actor_from_celery(
    actor_info: dict[str, Any],
) -> Generator[Actor, None, None]:
    """
    Restore actor context in Celery task from passed info.

    Restores the roles as well, so RBAC roles are preserved.

    Usage:
        @app.task
        def my_task(order_id: int, actor_info: dict):
            with restore_actor_from_celery(actor_info):
                # ActorContext is now set with original user info
                entry = AuditEntry(action=AuditAction.DLQ_REPLAY_START)
                # entry.actor_id will be the original user, not "system"
    """
    if not actor_info:
        # No actor info passed, log warning
        logger.warning("actor_context.celery_task_started_without")
        yield SYSTEM_ACTOR
        return

    with ActorContext.set_actor(
        actor_id=actor_info.get("actor_id", "unknown"),
        actor_type=actor_info.get("actor_type", "celery"),
        source=actor_info.get("source", "celery"),
        ip_address=actor_info.get("ip_address"),
        session_id=actor_info.get("session_id"),
        roles=actor_info.get("roles", []),  # restore RBAC roles
        original_request_time=actor_info.get("original_set_at"),
    ) as actor:
        yield actor


# =============================================================================
# Management command support
# =============================================================================


@contextmanager
def set_management_command_actor(
    command_name: str,
    run_by: str | None = None,
) -> Generator[Actor, None, None]:
    """
    Set actor context for Django management command.

    Usage:
        class Command(BaseCommand):
            def handle(self, *args, **options):
                with set_management_command_actor("cleanup_dlq", run_by="cron"):
                    do_cleanup()
    """
    import getpass
    import socket

    actor_id = run_by or f"{getpass.getuser()}@{socket.gethostname()}"

    with ActorContext.set_actor(
        actor_id=actor_id,
        actor_type="management_command",
        source=f"manage.py:{command_name}",
    ) as actor:
        logger.info(
            "actor_context.management_command_started",
            command_name=command_name,
            actor_id=actor_id,
        )
        yield actor
