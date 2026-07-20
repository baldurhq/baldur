"""
Post-Recovery Integrity Gate.

Verifies WAL <-> hash-chain integrity before replay when the CB goes CLOSED.
On failure it blocks the replay and alerts the operator.

EventBus handler priority:
    CRITICAL: this gate (runs before replay)
    NORMAL: _on_circuit_breaker_closed (replay)
    LOW: _on_circuit_breaker_closed_postmortem
"""

from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger()

# Event-data flag keys (imported and referenced by the bus module)
INTEGRITY_GATE_KEY = "integrity_gate_result"
INTEGRITY_FAILED_KEY = "integrity_failed"


# =============================================================================
# EventBus Handler
# =============================================================================


def on_circuit_breaker_closed_integrity_gate(event: Any) -> None:
    """
    WAL integrity gate on CB recovery.

    Registered at CRITICAL priority so it runs before the replay handler.

    Behavior:
        1. Collect WAL entries from the window where the circuit was Open
        2. Verify hash-chain integrity over that window
        3. On failure, set integrity_failed=True on event.data
        4. The downstream _on_circuit_breaker_closed checks this flag
    """
    service_name = event.data.get("service_name", "unknown")
    start = time.time()

    # Load Fail-Open/Secure mode from settings
    try:
        from baldur.settings.audit_integrity import get_audit_integrity_settings

        fail_open = get_audit_integrity_settings().integrity_gate_fail_open
    except Exception:
        fail_open = True  # Safe default when settings loading fails

    logger.info(
        "integrity_gate.checking_wal_integrity_before",
        service_name=service_name,
        fail_open=fail_open,
    )

    try:
        result = _verify_recovery_window_integrity(service_name, event)
        duration_ms = (time.time() - start) * 1000

        event.data[INTEGRITY_GATE_KEY] = {
            "valid": result["valid"],
            "checked": result.get("checked", 0),
            "duration_ms": duration_ms,
            "strategy": result.get("strategy", "full_chain"),
        }

        if not result["valid"]:
            event.data[INTEGRITY_FAILED_KEY] = True
            logger.critical(
                "integrity_gate.integrity_violation_replay_blocked",
                service_name=service_name,
                errors=result.get("errors", []),
            )
            _send_integrity_violation_alert(service_name, result, duration_ms)
        else:
            event.data[INTEGRITY_FAILED_KEY] = False
            logger.info(
                "integrity_gate.integrity_ok_entries_ms",
                service_name=service_name,
                checked=result.get("checked", 0),
                duration_ms=duration_ms,
            )

        _update_health_score(result, duration_ms)

    except Exception as e:
        # Branch on the Fail-Open/Secure setting
        if fail_open:
            logger.warning(
                "integrity_gate.gate_check_failed_proceeding",
                service_name=service_name,
                error=e,
            )
            event.data[INTEGRITY_FAILED_KEY] = False
        else:
            logger.critical(
                "integrity_gate.gate_check_failed_fail",
                service_name=service_name,
                error=e,
            )
            event.data[INTEGRITY_FAILED_KEY] = True

        event.data[INTEGRITY_GATE_KEY] = {
            "valid": None,
            "error": str(e),
            "policy": "fail_open" if fail_open else "fail_secure",
        }


# =============================================================================
# Internal Helpers
# =============================================================================


def _verify_recovery_window_integrity(
    service_name: str,
    event: Any,
) -> dict[str, Any]:
    """
    Verify the hash chain of WAL data accumulated while the circuit was Open.

    Returns:
        {"valid": bool, "checked": int, "errors": list, "strategy": str}
    """
    from baldur.audit.integrity import HashChainVerifier

    verifier = HashChainVerifier()

    # Collect unsynced entries from the WAL
    wal_entries = _get_unsynced_wal_entries(service_name)

    if not wal_entries:
        return {"valid": True, "checked": 0, "errors": [], "strategy": "no_entries"}

    # Verify the hash chain
    is_valid, error_msg = verifier.verify_chain(wal_entries)
    issues = verifier.find_tampering(wal_entries) if not is_valid else []

    return {
        "valid": is_valid,
        "checked": len(wal_entries),
        "errors": [i["message"] for i in issues]
        if issues
        else ([error_msg] if error_msg else []),
        "strategy": "wal_chain_verify",
    }


def _get_unsynced_wal_entries(service_name: str) -> list[dict]:
    """
    Fetch entries from the WAL that have not been synced yet.

    Uses wal.recover_unprocessed() to collect unprocessed entries and returns
    their WALEntry.data fields (dicts).
    """
    try:
        from baldur_pro.services.audit.base import _get_wal

        wal = _get_wal()
        if wal is None:
            return []

        # last_processed_seq=0 -> collect every unprocessed entry
        wal_entries = wal.recover_unprocessed(last_processed_seq=0)
        # Extract the WALEntry.data field (dict[str, Any])
        return [e.data for e in wal_entries if hasattr(e, "data")]

    except Exception as e:
        logger.warning(
            "integrity_gate.wal_read_failed",
            error=e,
        )
        return []


def _send_integrity_violation_alert(
    service_name: str,
    result: dict,
    duration_ms: float,
) -> None:
    """Send an integrity-violation alert and record it in the audit trail."""
    try:
        from baldur_pro.services.audit.base import _write_to_wal

        _write_to_wal(
            event_type="INTEGRITY_VIOLATION",
            source="PostRecoveryIntegrityGate",
            details={
                "service_name": service_name,
                "checked": result.get("checked", 0),
                "errors": result.get("errors", []),
                "duration_ms": duration_ms,
            },
            success=False,
            error_message="Hash chain integrity violation detected during post-recovery check",
        )
    except Exception as e:
        logger.exception(
            "integrity_gate.audit_write_failed",
            error=e,
        )


def _update_health_score(result: dict, duration_ms: float) -> None:
    """Update the IntegrityHealthScore."""
    try:
        from baldur.audit.integrity import get_integrity_health_score

        health = get_integrity_health_score()
        if result["valid"]:
            health.record_recovery(
                event_type="post_recovery_gate_ok",
                sequences_affected=result.get("checked", 0),
                recovery_time_ms=duration_ms,
            )
        else:
            health.record_chain_break()
    except Exception as e:
        logger.debug(
            "integrity_gate.health_score_update_failed",
            error=e,
        )
