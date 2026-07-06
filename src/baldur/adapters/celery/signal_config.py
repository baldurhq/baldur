"""
Signal Hooks Settings — Pydantic v2.

Configuration for Celery signal hooks: circuit breaker, DLQ, metrics,
forensics toggles, task-domain mapping, and domain resolution helpers.

Environment Variables:
    BALDUR_SIGNAL_HOOKS_ENABLED=true
    BALDUR_SIGNAL_HOOKS_CB_ENABLED=true
    BALDUR_SIGNAL_HOOKS_DLQ_ENABLED=true
    BALDUR_SIGNAL_HOOKS_METRICS_ENABLED=true
    BALDUR_SIGNAL_HOOKS_FORENSICS_ENABLED=true
    BALDUR_SIGNAL_HOOKS_CB_FAILURE_THRESHOLD=5
    BALDUR_SIGNAL_HOOKS_CB_RECOVERY_TIMEOUT=60
    BALDUR_SIGNAL_HOOKS_CB_SUCCESS_THRESHOLD=2
    BALDUR_SIGNAL_HOOKS_TASK_DOMAIN_MAPPING='{"task_name": "domain"}'
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "SignalHooksSettings",
    "extract_domain_from_task_name",
    "extract_service_name",
    "get_signal_hooks_settings",
    "reset_signal_hooks_settings",
]


class SignalHooksSettings(BaseSettings):
    """Configuration for Celery signal hooks."""

    model_config = make_settings_config("BALDUR_SIGNAL_HOOKS_")

    # Master switch
    enabled: bool = True

    # Feature toggles
    cb_enabled: bool = True
    dlq_enabled: bool = True
    metrics_enabled: bool = True
    forensics_enabled: bool = True

    # Circuit breaker thresholds
    cb_failure_threshold: int = Field(default=5, ge=1)
    cb_recovery_timeout: int = Field(default=60, ge=1)
    cb_success_threshold: int = Field(default=2, ge=1)

    # Task domain mapping
    task_domain_mapping: dict[str, str] = Field(default_factory=dict)

    # Domain patterns for task name matching (overrides defaults when set)
    domain_patterns: dict[str, list[str]] | None = None

    # Excluded tasks (never process these)
    excluded_tasks: set[str] = Field(
        default_factory=lambda: {
            "celery.backend_cleanup",
            "celery.chord_unlock",
            "baldur.celery_tasks.check_circuit_breaker_recovery",
            "baldur.celery_tasks.expire_manual_overrides",
            "baldur.adapters.celery.tasks.collect_baldur_metrics",
            "baldur.adapters.celery.tasks.cleanup_resolved_dlq_entries",
        }
    )


# ---------------------------------------------------------------------------
# Domain Resolution
# ---------------------------------------------------------------------------

# Default domain keyword patterns
_DEFAULT_DOMAIN_PATTERNS: dict[str, list[str]] = {
    "payment": ["payment", "pay", "checkout", "billing"],
    "order": ["order", "purchase", "buy"],
    "inventory": ["inventory", "stock", "warehouse"],
    "notification": ["notification", "email", "sms", "push", "notify"],
    "user": ["user", "auth", "login", "register"],
    "cart": ["cart", "basket"],
    "shipping": ["shipping", "delivery", "shipment"],
    "refund": ["refund", "return", "cancel"],
}


def extract_domain_from_task_name(task_name: str, config: SignalHooksSettings) -> str:
    """
    Extract domain from task name.

    Priority:
    1. Explicit mapping in task_domain_mapping
    2. Task name pattern matching (e.g., 'myapp.tasks.process_order' -> 'order')
    3. First segment of task name
    """
    # Check explicit mapping first
    if task_name in config.task_domain_mapping:
        return config.task_domain_mapping[task_name]

    # Pattern matching for common task names
    name_lower = task_name.lower()
    domain_patterns = config.domain_patterns or _DEFAULT_DOMAIN_PATTERNS

    for domain, patterns in domain_patterns.items():
        if any(pattern in name_lower for pattern in patterns):
            return domain

    # Fallback: use first meaningful segment
    parts = task_name.split(".")
    if len(parts) >= 2:
        for part in parts:
            if part not in ("tasks", "celery", "app"):
                return part

    return "unknown"


def extract_service_name(
    task_name: str,
    config: SignalHooksSettings,
    exception: Exception | None = None,
) -> str:
    """
    Extract service name for circuit breaker tracking.

    Attempts to identify the external service that failed.
    """
    if exception:
        exc_str = str(exception).lower()

        service_patterns = {
            "redis": ["redis"],
            "external_timeout": ["timeout"],
            "external_connection": ["connection"],
            "payment_gateway": ["pg", "payment", "gateway"],
        }

        for service_name, keywords in service_patterns.items():
            if any(keyword in exc_str for keyword in keywords):
                return service_name

    # Use domain as service name
    return extract_domain_from_task_name(task_name, config)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_settings_instance: SignalHooksSettings | None = None


def get_signal_hooks_settings() -> SignalHooksSettings:
    """Return singleton SignalHooksSettings instance."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = SignalHooksSettings()
    return _settings_instance


def reset_signal_hooks_settings() -> None:
    """Reset singleton (for testing)."""
    global _settings_instance
    _settings_instance = None
