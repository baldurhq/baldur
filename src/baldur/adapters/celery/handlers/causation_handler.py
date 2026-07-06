"""
Causation Handler — inject causation context headers into outbound Celery messages.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.adapters.celery.signal_config import SignalHooksSettings

__all__ = ["CausationHandler"]

logger = structlog.get_logger()


class CausationHandler:
    """Inject causation context headers before Celery task publication."""

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
        Inject CausationContext headers into Celery message headers.

        If a CausationContext is active, automatically adds cascade_id,
        parent_event_id, chain_depth, and namespace to the message headers.
        This enables automatic causation chain tracking across async tasks.
        """
        if not self._config.enabled:
            return

        try:
            from baldur.context.causation_context import (
                CELERY_HEADER_CASCADE_ID,
                CELERY_HEADER_CHAIN_DEPTH,
                CELERY_HEADER_NAMESPACE,
                CELERY_HEADER_PARENT_EVENT,
                CausationContext,
            )

            if not CausationContext.is_set():
                return

            info = CausationContext.get_current()
            if not info:
                return

            # headers dict unavailable — cannot inject
            if headers is None:
                logger.debug("celery_signal.headers_unavailable")
                return

            # Do not overwrite explicit causation headers
            if headers.get(CELERY_HEADER_CASCADE_ID):
                logger.debug("celery_signal.causation_headers_present")
                return

            # Inject causation headers
            headers[CELERY_HEADER_CASCADE_ID] = info.cascade_id
            headers[CELERY_HEADER_PARENT_EVENT] = info.parent_event_id
            headers[CELERY_HEADER_CHAIN_DEPTH] = str(info.chain_depth)
            headers[CELERY_HEADER_NAMESPACE] = info.namespace

            logger.debug(
                "celery_signal.causation_headers_injected",
                cascade_id=info.cascade_id,
                chain_depth=info.chain_depth,
            )

        except ImportError:
            # causation_context module not available — skip
            pass
        except Exception as e:
            # Never let signal handler crash affect task publishing
            logger.debug(
                "baldur_signal.causation_inject_failed",
                error=e,
            )
