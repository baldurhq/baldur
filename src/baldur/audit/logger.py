"""
Audit Logger - Thin shim over ProviderRegistry.audit (416 Part 6).

After the H1/H2 unification (416), ``AuditLogger`` no longer owns its
own ``AuditBackend``. ``log_change()`` masks fields, builds an
``AuditEntry`` (H1 schema), and delegates to
``ProviderRegistry.get_audit_adapter().log()``. This is the single
audit interface for both resilience events (Pipeline A) and
config-change events (Pipeline B).

The ``log_config_change()`` convenience function and the
``AuditConfigChangeEvent`` dataclass remain stable so the ~14 existing
callers do not need to change.

D8: ``query()``, ``verify_integrity()`` and ``get_backend_health()``
were deleted — their use cases are now served directly by
``ProviderRegistry`` and ``HashChainFileAuditLogAdapter``.

D17: ``log_change()`` returns ``True`` after a successful
``adapter.log()`` (which returns ``None`` per the H1 ABC). The H1 log
contract is "raises on failure", so any non-exception path is success.
This avoids the ``bool(None) == False`` regression that would
otherwise leak through ``audit/env_snapshot.py:_log_to_fallback()``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

import structlog

from baldur.audit.masking import (
    extract_ip_from_request,
    mask_ip,
    mask_sensitive_fields,
)
from baldur.audit.trace import get_trace_id, get_trace_id_full
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry

logger = structlog.get_logger()

__all__ = [
    "AuditLogger",
    "AuditConfigChangeEvent",
    "ConfigAuditAction",
    "ConfigChangeEvent",
    "get_audit_logger",
    "log_config_change",
]


class ConfigAuditAction(str, Enum):
    """Types of audit actions."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    READ = "read"  # For sensitive config reads
    APPLY = "apply"  # For config application
    ROLLBACK = "rollback"
    VERIFY = "verify"  # Integrity verification
    EXPORT = "export"  # Bulk export


@dataclass
class AuditConfigChangeEvent:
    """Configuration change event — input to ``AuditLogger.log_change()``."""

    config_type: str
    config_key: str
    action: ConfigAuditAction | str
    old_value: Any = None
    new_value: Any = None
    reason: str | None = None
    user: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    source: str = "api"  # api, cli, system, scheduler
    apply_strategy: str | None = None
    apply_delay_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result = asdict(self)
        if isinstance(result["action"], ConfigAuditAction):
            result["action"] = result["action"].value
        return result


class AuditLogger:
    """Audit logger for configuration changes (post-416 thin shim).

    Holds masking configuration as instance state but does NOT own a
    backend. Every call delegates to
    ``ProviderRegistry.get_audit_adapter().log()`` so OSS / PRO / null
    routing happens in one place.

    Preferred entry points for new code:

    - For config-change events with masking::

        from baldur.audit import log_config_change
        log_config_change(config_type=..., config_key=..., ...)

    - For arbitrary audit entries (resilience events, custom domains)::

        from baldur.factory import ProviderRegistry
        from baldur.interfaces.audit_adapter import AuditEntry
        ProviderRegistry.get_audit_adapter().log(AuditEntry(...))

    ``AuditLogger.get_instance()`` is retained for the two existing
    consumers (``services/metric_sync_service.py``,
    ``services/governance/api_service.py``) but is **not recommended**
    for new code.
    """

    _instance: AuditLogger | None = None

    def __init__(
        self,
        mask_ip_addresses: bool = True,
        sensitive_fields: list[str] | None = None,
        enable_console_log: bool = True,
    ):
        self._mask_ip = mask_ip_addresses
        self._sensitive_fields = sensitive_fields or [
            "password",
            "secret",
            "token",
            "api_key",
            "private_key",
            "credit_card",
        ]
        self._enable_console = enable_console_log

    @classmethod
    def get_instance(cls) -> AuditLogger:
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        cls._instance = None

    @classmethod
    def configure(cls, **kwargs: Any) -> AuditLogger:
        """Configure the singleton instance.

        416: the ``backend`` parameter is no longer accepted — backend
        selection happens via ``ProviderRegistry.audit.set_default()``.
        """
        cls._instance = cls(**kwargs)
        return cls._instance

    def log_change(
        self,
        event: AuditConfigChangeEvent | dict[str, Any],
        request: Any = None,
    ) -> bool:
        """Log a configuration change event.

        D17: returns ``True`` whenever the underlying ``adapter.log()``
        does not raise (the H1 contract is "raises on failure"). Returns
        ``False`` only on exception. This is what allows
        ``audit/env_snapshot.py`` to distinguish silenced-Null from real
        failure.
        """
        try:
            if isinstance(event, AuditConfigChangeEvent):
                event_dict = event.to_dict()
            else:
                event_dict = dict(event)

            # Extract IP from request if available.
            if request and not event_dict.get("ip_address"):
                event_dict["ip_address"] = extract_ip_from_request(request)
                if hasattr(request, "META"):
                    event_dict["user_agent"] = request.META.get("HTTP_USER_AGENT", "")

            entry = self._build_h1_entry(event_dict)

            # D8: route through the unified ProviderRegistry adapter.
            from baldur.factory import ProviderRegistry

            ProviderRegistry.get_audit_adapter().log(entry)

            if self._enable_console:
                self._log_to_console(entry)

            return True
        except Exception as e:
            logger.exception(
                "audit_logger.log_change_failed",
                error=e,
            )
            return False

    def log_config_update(
        self,
        config_type: str,
        config_key: str,
        old_value: Any,
        new_value: Any,
        user: str | None = None,
        ip_address: str | None = None,
        reason: str | None = None,
        request: Any = None,
        **kwargs: Any,
    ) -> bool:
        """Convenience method for logging config updates."""
        event = AuditConfigChangeEvent(
            config_type=config_type,
            config_key=config_key,
            action=ConfigAuditAction.UPDATE,
            old_value=old_value,
            new_value=new_value,
            user=user,
            ip_address=ip_address,
            reason=reason,
            metadata=kwargs,
        )
        return self.log_change(event, request=request)

    def log_batch_update(
        self,
        config_type: str,
        changes: list[dict[str, Any]],
        user: str | None = None,
        request: Any = None,
    ) -> bool:
        """Log multiple configuration changes as a batch."""
        success = True
        batch_id = get_trace_id() or self._generate_batch_id()

        for change in changes:
            event = AuditConfigChangeEvent(
                config_type=config_type,
                config_key=change.get("key", ""),
                action=ConfigAuditAction.UPDATE,
                old_value=change.get("old_value"),
                new_value=change.get("new_value"),
                user=user,
                metadata={"batch_id": batch_id, "batch_size": len(changes)},
            )
            if not self.log_change(event, request=request):
                success = False

        return success

    def _build_h1_entry(self, event_dict: dict[str, Any]) -> AuditEntry:
        """Build an H1 ``AuditEntry`` from the legacy event dict.

        Applies the GDPR/CCPA IP masking pipeline and W3C trace
        correlation. The resulting ``AuditEntry.details`` carries every
        config-change-specific field so the
        ``HashChainFileAuditLogAdapter`` can re-emit the H2 dict schema
        unchanged (D6).
        """
        if self._mask_ip and event_dict.get("ip_address"):
            event_dict["ip_address"] = mask_ip(event_dict["ip_address"])

        if event_dict.get("old_value") is not None:
            event_dict["old_value"] = mask_sensitive_fields(
                event_dict["old_value"],
                self._sensitive_fields,
            )
        if event_dict.get("new_value") is not None:
            event_dict["new_value"] = mask_sensitive_fields(
                event_dict["new_value"],
                self._sensitive_fields,
            )

        details: dict[str, Any] = {
            "old_value": event_dict.get("old_value"),
            "new_value": event_dict.get("new_value"),
            "source": event_dict.get("source", "api"),
            "ip_address": event_dict.get("ip_address"),
            "user_agent": event_dict.get("user_agent"),
            "apply_strategy": event_dict.get("apply_strategy"),
            "apply_delay_seconds": event_dict.get("apply_delay_seconds"),
            "trace_id": get_trace_id(),
            "trace_id_full": get_trace_id_full(),
        }
        # Merge caller-supplied metadata last so it can carry batch_id, etc.
        details.update(event_dict.get("metadata") or {})

        return AuditEntry(
            action=AuditAction.CONFIG_CHANGE,
            target_type=event_dict.get("config_type"),
            target_id=event_dict.get("config_key"),
            actor_id=event_dict.get("user"),
            reason=event_dict.get("reason"),
            details=details,
        )

    def _log_to_console(self, entry: AuditEntry) -> None:
        """Log the entry to the structured logger for stdout/syslog capture."""
        details = entry.details or {}
        logger.info(
            "audit.event",
            change=(
                entry.action.value
                if isinstance(entry.action, AuditAction)
                else entry.action
            ),
            config_type=entry.target_type or "",
            config_key=entry.target_id or "",
            actor_id=entry.actor_id or "system",
            ip_address=details.get("ip_address", "unknown"),
        )

    def _generate_batch_id(self) -> str:
        """Generate a batch ID for grouped changes."""
        import uuid

        return f"batch-{uuid.uuid4().hex[:12]}"

    def close(self) -> None:
        """Close the audit logger.

        416: the underlying adapter is owned by ``ProviderRegistry``,
        not by this class. ``close()`` is intentionally a no-op so the
        existing call sites continue to work without leaking the
        registry-managed adapter lifecycle.
        """
        return


# =============================================================================
# Convenience functions
# =============================================================================


def log_config_change(
    config_type: str,
    config_key: str,
    old_value: Any,
    new_value: Any,
    user: str | None = None,
    request: Any = None,
    **kwargs: Any,
) -> bool:
    """Log a configuration change using the global logger.

    Signature unchanged across 416 — the ~14 existing call sites need
    no source change.
    """
    return AuditLogger.get_instance().log_config_update(
        config_type=config_type,
        config_key=config_key,
        old_value=old_value,
        new_value=new_value,
        user=user,
        request=request,
        **kwargs,
    )


def get_audit_logger() -> AuditLogger:
    """Get the global audit logger instance."""
    return AuditLogger.get_instance()


# Backward-compat alias retained for callers that imported the old name.
ConfigChangeEvent = AuditConfigChangeEvent
