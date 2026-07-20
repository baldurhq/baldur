"""
Environment Variable Snapshot for Audit Trail.

Records a snapshot of Baldur related environment variables
at system startup for audit and forensic analysis.

Features:
- Automatic masking of sensitive values (SECRET, PASSWORD, TOKEN, KEY, CREDENTIAL)
- SHA256 hash for change detection
- Integration with AuditLogger via log_config_change
- L1 Local Fallback: Critical log + JSON file if DB fails
- Prometheus metrics for observability

Defense-in-Depth Strategy:
1. Primary: Log to AuditService (DB)
2. Fallback: Log to local file (logs/env_snapshot_fallback.jsonl)
3. Always: logger.critical with hash for stdout/syslog
4. Metrics: Prometheus gauge for monitoring

Usage:
    This module is called automatically by BaldurConfig.ready()
    on every server start.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import structlog

from baldur.utils.serialization import fast_dumps_str
from baldur.utils.time import utc_now

logger = structlog.get_logger()

# Baldur-related environment variable prefixes
TRACKED_PREFIXES: list[str] = [
    "BALDUR_",
    "CIRCUIT_BREAKER_",
    "DLQ_",
    "SLA_",
    "CHAOS_",
]

# Sensitive keywords (masking targets)
SENSITIVE_KEYWORDS: list[str] = [
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "KEY",
    "CREDENTIAL",
    "API_KEY",
    "PRIVATE",
]

# Fallback log file path
FALLBACK_LOG_PATH = "logs/env_snapshot_fallback.jsonl"

# Global state: whether the snapshot was recorded successfully
_snapshot_recorded: bool = False
_last_snapshot_hash: str | None = None


def _get_metrics():
    """Prometheus metrics (lazy import to avoid circular deps)."""
    try:
        from baldur.metrics.registry import get_or_create_gauge

        # Create metrics via the singleton pattern
        if not hasattr(_get_metrics, "_env_snapshot_recorded"):
            _get_metrics._env_snapshot_recorded = get_or_create_gauge(
                "baldur_env_snapshot_recorded",
                "Whether environment snapshot was successfully recorded (1=yes, 0=no)",
                [],
            )
            _get_metrics._env_snapshot_variable_count = get_or_create_gauge(
                "baldur_env_snapshot_variable_count",
                "Number of tracked environment variables",
                [],
            )
        return (
            _get_metrics._env_snapshot_recorded,
            _get_metrics._env_snapshot_variable_count,
        )
    except ImportError:
        return None, None


def collect_env_snapshot() -> dict[str, Any]:
    """
    Collect Baldur related environment variables snapshot.

    Sensitive values are automatically masked for security.

    Returns:
        dict: {
            "variables": {"BALDUR_DLQ_ENABLED": "true", ...},
            "hash": "sha256:abc123...",
            "count": 15
        }

    Example:
        >>> snapshot = collect_env_snapshot()
        >>> print(snapshot["count"])
        12
        >>> print(snapshot["hash"])
        sha256:a1b2c3d4e5f6...
    """
    variables: dict[str, str] = {}

    for key, value in os.environ.items():
        # Prefix match
        if any(key.startswith(prefix) for prefix in TRACKED_PREFIXES):
            # Mask sensitive values
            if any(kw in key.upper() for kw in SENSITIVE_KEYWORDS):
                variables[key] = "***MASKED***"
            else:
                variables[key] = value

    # Change-detection hash (computed over the masked values)
    sorted_items = sorted(variables.items())
    hash_input = str(sorted_items).encode("utf-8")
    config_hash = hashlib.sha256(hash_input).hexdigest()[:16]

    return {
        "variables": variables,
        "hash": f"sha256:{config_hash}",
        "count": len(variables),
    }


def log_env_snapshot_to_audit() -> bool:
    """
    Log environment variable snapshot to AuditService with fallback.

    Called from BaldurConfig.ready() on every server start.

    Defense-in-Depth:
    1. Try primary: AuditService (DB)
    2. On failure: L1 fallback (local file + critical log)
    3. Always: Update Prometheus metrics

    Returns:
        bool: True if successfully logged (primary or fallback), False otherwise.
    """
    global _snapshot_recorded, _last_snapshot_hash

    snapshot = collect_env_snapshot()
    _last_snapshot_hash = snapshot["hash"]

    # Get Prometheus metrics
    metric_recorded, metric_count = _get_metrics()

    if snapshot["count"] == 0:
        logger.debug("env_audit.no_tracked_environment_variables")
        _snapshot_recorded = True
        if metric_recorded:
            metric_recorded.set(1)
            metric_count.set(0)
        return True

    # Try primary: AuditService
    primary_success = _log_to_audit_service(snapshot)

    if primary_success:
        _snapshot_recorded = True
        if metric_recorded:
            metric_recorded.set(1)
            metric_count.set(snapshot["count"])
        logger.info(
            "env_audit.snapshot_recorded",
            snapshot=snapshot["count"],
            snapshot_hash=snapshot["hash"],
        )
        return True

    # Primary failed - activate L1 fallback
    logger.warning("env_audit.primary_logging_failed_activating")
    fallback_success = _log_to_fallback(snapshot)

    # Always emit critical log with hash (for syslog/stdout capture)
    _emit_critical_log(
        snapshot, primary_success=False, fallback_success=fallback_success
    )

    # Update metrics
    if metric_recorded:
        metric_recorded.set(1 if fallback_success else 0)
        metric_count.set(snapshot["count"])

    _snapshot_recorded = fallback_success
    return fallback_success


def _log_to_audit_service(snapshot: dict[str, Any]) -> bool:
    """
    Try to log snapshot to primary AuditService.

    D7: When ``AuditSettings.enabled`` is False, treat as
    "intentionally silenced — primary success" and skip fallback. This
    avoids the false-failure path that would otherwise leak through
    ``_log_to_fallback()`` when the resolved adapter is
    ``NullAuditLogAdapter`` (returns None → bool(None) == False).

    Returns:
        bool: True if successful (or intentionally silenced)
    """
    try:
        from baldur.settings.audit import get_audit_settings

        if not get_audit_settings().enabled:
            logger.debug("audit.env_snapshot_silenced")
            return True
    except Exception:
        # Settings unavailable — fall through and try the primary path.
        pass

    try:
        from baldur.audit import log_config_change

        success = log_config_change(
            config_type="environment_variables",
            config_key="startup_snapshot",
            old_value=None,
            new_value=snapshot["variables"],
            user="system_startup",
            reason="Application startup - environment snapshot",
            metadata={
                "hash": snapshot["hash"],
                "variable_count": snapshot["count"],
                "source": "ready",
            },
        )
        return bool(success)
    except ImportError as e:
        logger.debug(
            "env_audit.audit_module_available",
            error=e,
        )
        return False
    except Exception as e:
        logger.warning(
            "env_audit.primary_audit_failed",
            error=e,
        )
        return False


def _log_to_fallback(snapshot: dict[str, Any]) -> bool:
    """
    L1 Fallback: Log to local JSON file.

    D7 defense-in-depth: when ``AuditSettings.enabled`` is False, this
    function returns False immediately without touching the filesystem.
    This is redundant with ``_log_to_audit_service()``'s early-return for
    the same flag, but the layered guarantee makes it impossible for the
    fallback file to be created in OSS mode.

    File format: JSON Lines (.jsonl) for easy parsing.

    Returns:
        bool: True if successful
    """
    try:
        from baldur.settings.audit import get_audit_settings

        if not get_audit_settings().enabled:
            logger.debug("audit.env_snapshot_fallback_blocked")
            return False
    except Exception:
        pass

    try:
        fallback_path = Path(FALLBACK_LOG_PATH)
        fallback_path.parent.mkdir(parents=True, exist_ok=True)

        fallback_entry = {
            "timestamp": utc_now().isoformat(),
            "event": "env_snapshot_fallback",
            "hash": snapshot["hash"],
            "variable_count": snapshot["count"],
            "variables": snapshot["variables"],
            "reason": "Primary AuditService unavailable",
        }

        with open(fallback_path, "a", encoding="utf-8") as f:
            f.write(fast_dumps_str(fallback_entry) + "\n")

        logger.warning(
            "env_audit.fallback_recorded",
            fallback_path=fallback_path,
            snapshot=snapshot["hash"],
        )
        return True
    except Exception as e:
        logger.exception(
            "env_audit.fallback_logging_also_failed",
            error=e,
        )
        return False


def _emit_critical_log(
    snapshot: dict[str, Any],
    primary_success: bool,
    fallback_success: bool,
) -> None:
    """
    Emit critical log for syslog/stdout capture.

    This is the last line of defense - even if everything else fails,
    this should appear in container logs / syslog for forensic analysis.
    """
    status = "FALLBACK" if fallback_success else "FAILED"
    logger.critical(
        "env_audit.snapshot",
        snapshot_status=status,
        snapshot=snapshot["hash"],
        snapshot_count=snapshot["count"],
        primary_success=primary_success,
        fallback_success=fallback_success,
    )


def get_env_snapshot_summary() -> dict[str, Any]:
    """
    Get a summary of the current environment snapshot.

    Useful for debugging and status endpoints.

    Returns:
        dict: Snapshot summary with hash and count.
    """
    snapshot = collect_env_snapshot()
    return {
        "hash": snapshot["hash"],
        "count": snapshot["count"],
        "tracked_prefixes": TRACKED_PREFIXES,
    }
