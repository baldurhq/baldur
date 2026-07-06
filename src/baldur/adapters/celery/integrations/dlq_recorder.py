"""
DLQ Recorder — store failed operations and classify failures.

Wraps lazy imports to baldur_pro.services.dlq so the signal handler
layer never crashes due to missing optional dependencies.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.dlq.helpers import store_to_dlq
from baldur.utils.time import utc_now

__all__ = ["DLQRecorder"]

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Failure Classification Patterns
# ---------------------------------------------------------------------------

# Exception type name patterns -> failure type
_EXCEPTION_TYPE_PATTERNS: dict[str, list[str]] = {
    "NETWORK_ERROR": ["connection", "timeout", "network", "socket"],
}

# Exception message patterns -> failure type (order matters, first match wins)
_EXCEPTION_MESSAGE_PATTERNS: list[tuple[list[str], str]] = [
    (["rate limit", "too many requests", "429"], "RATE_LIMITED"),
    (["auth", "unauthorized", "401", "403"], "AUTH_ERROR"),
    (["validation", "invalid", "400"], "VALIDATION_ERROR"),
    (["502", "503", "504", "bad gateway"], "EXTERNAL_SERVICE_ERROR"),
    (["gateway", "provider", "external"], "GATEWAY_ERROR"),
    (["timeout"], "TIMEOUT"),
    (["connection"], "CONNECTION_ERROR"),
]

# Recommended action lookup
_RECOMMENDED_ACTIONS: dict[str, str] = {
    "NETWORK_ERROR": "Wait for network recovery, then auto-replay",
    "TIMEOUT": "Increase timeout or retry with backoff",
    "CONNECTION_ERROR": "Check external service availability",
    "RATE_LIMITED": "Wait for rate limit window, then retry",
    "AUTH_ERROR": "Check credentials and permissions",
    "VALIDATION_ERROR": "Manual review required - data may be invalid",
    "EXTERNAL_SERVICE_ERROR": "Wait for external service recovery",
    "GATEWAY_ERROR": "Check gateway status, manual review may be needed",
    "UNKNOWN_ERROR": "Manual review recommended",
}

# Entity ID inference patterns
_ID_PRIORITY: list[tuple[str, str]] = [
    ("order_id", "order"),
    ("payment_id", "payment"),
    ("transaction_id", "transaction"),
    ("subscription_id", "subscription"),
    ("user_id", "user"),
    ("product_id", "product"),
    ("cart_id", "cart"),
    ("shipment_id", "shipment"),
]


class DLQRecorder:
    """Store failed operations in the Dead Letter Queue."""

    def store(
        self,
        domain: str,
        task_name: str,
        task_id: str,
        exception: Exception,
        args: tuple | None,
        kwargs: dict | None,
        einfo: Any,
    ) -> None:
        """Store a failed operation to DLQ."""
        try:
            failure_type = self.classify_failure_type(exception)

            snapshot_data = {
                "task_name": task_name,
                "task_id": task_id,
                "args": list(args) if args else [],
                "kwargs": kwargs or {},
                "exception_type": type(exception).__name__,
                "exception_message": str(exception),
                "timestamp": utc_now().isoformat(),
            }

            request_data = {
                "task_name": task_name,
                "task_id": task_id,
                "args": list(args) if args else [],
                "kwargs": kwargs or {},
            }

            entity_refs = self.extract_entity_refs(kwargs)
            entity_type = entity_refs.get("entity_type", "")
            entity_id = entity_refs.get("entity_id", "")

            result = store_to_dlq(
                domain=domain,
                failure_type=failure_type,
                error_message=str(exception),
                error_code=type(exception).__name__,
                snapshot_data=snapshot_data,
                request_data=request_data,
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id else "",
                user_id=entity_refs.get("user_id"),
                metadata={
                    "source": "celery_signal_hook",
                    "task_name": task_name,
                    "traceback": str(einfo) if einfo else None,
                },
                recommended_action=self.get_recommended_action(failure_type),
            )

            if result is None:
                # PRO DLQ store not loaded — OSS path is a no-op recorder.
                return

            logger.info(
                "baldur_dlq.entry_stored",
                healing_domain=domain,
                failure_type=failure_type,
                dlq_id=result.dlq_id,
            )

            # NOTE: the dlq_items_total metric is recorded inside store_to_dlq
            # (PRO store path -> DLQMetricEventHandler.on_item_created) for ALL
            # store paths, exactly as the HTTP @dlq_protect path relies on. Do
            # NOT record it again here, or dlq_items_total (declared EXACT)
            # double-counts for celery-task-originated DLQ entries.

        except ImportError as e:
            logger.debug(
                "baldur_dlq.service_unavailable",
                error=e,
            )
        except Exception as e:
            logger.exception(
                "baldur_dlq.store_failed",
                error=e,
            )

    # ------------------------------------------------------------------
    # Classification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def classify_failure_type(exception: Exception) -> str:
        """Classify exception into failure type using pattern matching."""
        exc_type = type(exception).__name__.lower()
        exc_str = str(exception).lower()

        # Check exception type name patterns
        for failure_type, keywords in _EXCEPTION_TYPE_PATTERNS.items():
            if any(keyword in exc_type for keyword in keywords):
                return failure_type

        # Check exception message patterns
        for keywords, failure_type in _EXCEPTION_MESSAGE_PATTERNS:
            if any(keyword in exc_str for keyword in keywords):
                return failure_type

        return "UNKNOWN_ERROR"

    @staticmethod
    def extract_entity_refs(kwargs: dict | None) -> dict[str, str]:
        """
        Extract entity references from task kwargs.

        Returns generic entity_type and entity_id for DLQ storage.
        """
        if not kwargs:
            return {}

        entity_refs: dict[str, str] = {}

        # Check for explicit entity_type/entity_id first
        if "entity_type" in kwargs and "entity_id" in kwargs:
            entity_refs["entity_type"] = str(kwargs["entity_type"])
            entity_refs["entity_id"] = str(kwargs["entity_id"])
            if "user_id" in kwargs:
                entity_refs["user_id"] = kwargs["user_id"]
            return entity_refs

        # Fallback: infer from common ID patterns
        for key, entity_type in _ID_PRIORITY:
            if key in kwargs and kwargs[key] is not None:
                entity_refs["entity_type"] = entity_type
                entity_refs["entity_id"] = str(kwargs[key])
                break

        # Always include user_id if present
        if "user_id" in kwargs and kwargs["user_id"] is not None:
            entity_refs["user_id"] = kwargs["user_id"]

        return entity_refs

    @staticmethod
    def get_recommended_action(failure_type: str) -> str:
        """Get recommended action based on failure type."""
        return _RECOMMENDED_ACTIONS.get(failure_type, "Review and retry manually")
