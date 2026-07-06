"""
Actor Context Handler — inject actor context headers into outbound Celery messages.

Mirrors CausationHandler pattern. When an ActorContext is active during task
publication, this handler automatically injects the actor information into
message headers so it can be restored on the worker side.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from baldur.adapters.celery.signal_config import SignalHooksSettings

__all__ = ["ActorContextHandler"]

logger = structlog.get_logger()


class ActorContextHandler:
    """Inject actor context headers before Celery task publication."""

    def __init__(self, config: SignalHooksSettings) -> None:
        self._config = config

    def handle(
        self,
        sender: Any = None,
        body: Any = None,
        exchange: Any = None,
        routing_key: Any = None,
        headers: dict | None = None,
        properties: Any = None,
        declare: Any = None,
        retry_policy: Any = None,
        **kw: Any,
    ) -> None:
        """
        Inject ActorContext headers into Celery message headers.

        If an ActorContext is active, automatically adds actor_id, actor_type,
        source, ip_address, session_id, and roles to the message headers.
        This enables automatic actor tracking across async tasks.

        Priority resolution happens in task_prerun:
        - kwargs["actor_info"] (explicit override) takes precedence
        - headers (auto-propagated) are secondary
        - SYSTEM_ACTOR fallback if neither is present
        """
        if not self._config.enabled:
            return

        try:
            from baldur.context.actor_context import (
                CELERY_HEADER_ACTOR_ID,
                CELERY_HEADER_ACTOR_IP,
                CELERY_HEADER_ACTOR_ROLES,
                CELERY_HEADER_ACTOR_SESSION,
                CELERY_HEADER_ACTOR_SOURCE,
                CELERY_HEADER_ACTOR_TYPE,
                ActorContext,
            )

            if not ActorContext.is_set():
                return

            actor = ActorContext.get_current_or_none()
            if not actor:
                return

            # headers dict unavailable — cannot inject
            if headers is None:
                logger.debug("celery_signal.actor_headers_unavailable")
                return

            # Do not overwrite explicit actor headers
            if headers.get(CELERY_HEADER_ACTOR_ID):
                logger.debug("celery_signal.actor_headers_present")
                return

            # Inject actor headers
            headers[CELERY_HEADER_ACTOR_ID] = actor.actor_id
            headers[CELERY_HEADER_ACTOR_TYPE] = actor.actor_type
            headers[CELERY_HEADER_ACTOR_SOURCE] = f"celery_from_{actor.source}"
            if actor.ip_address:
                headers[CELERY_HEADER_ACTOR_IP] = actor.ip_address
            if actor.session_id:
                headers[CELERY_HEADER_ACTOR_SESSION] = actor.session_id
            headers[CELERY_HEADER_ACTOR_ROLES] = json.dumps(actor.roles)

            logger.debug(
                "celery_signal.actor_headers_injected",
                actor_id=actor.actor_id,
                actor_type=actor.actor_type,
            )

        except ImportError:
            # actor_context module not available — skip
            pass
        except Exception as e:
            # Never let signal handler crash affect task publishing
            logger.debug(
                "baldur_signal.actor_inject_failed",
                error=e,
            )
