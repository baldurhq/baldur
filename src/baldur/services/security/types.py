"""
Security Violation Types and Severity Enums.

Contains domain-neutral security violation type definitions
and severity level mappings.
"""

from __future__ import annotations

from enum import Enum


class ViolationType(str, Enum):
    """Types of security violations that never self-heal (domain-neutral)."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Existing ViolationType
    # ═══════════════════════════════════════════════════════════════════════════
    SIGNATURE_INVALID = "signature_invalid"
    DATA_TAMPERED = "data_tampered"
    TOKEN_FORGED = "token_forged"
    UNAUTHORIZED_ACCESS = "unauthorized_access"
    RATE_LIMIT_ABUSE = "rate_limit_abuse"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    REPLAY_ATTACK = "replay_attack"
    INJECTION_ATTEMPT = "injection_attempt"

    # ═══════════════════════════════════════════════════════════════════════════
    # Baldur loop detection
    # Recovery infinite loops, conflicting adjustments, timeouts, flapping detection
    # ═══════════════════════════════════════════════════════════════════════════
    RECOVERY_LOOP_DETECTED = "recovery_loop_detected"
    """Recovery/adjustment infinite-loop detected - the most severe."""

    CONFLICTING_ADJUSTMENT = "conflicting_adjustment"
    """Conflicting autonomous adjustment detected (e.g., A→B→A repetition)."""

    HEALING_TIMEOUT = "healing_timeout"
    """Baldur operation timed out."""

    FLAPPING_DETECTED = "flapping_detected"
    """Parameter flapping detected (repeated micro-adjustments)."""

    # ═══════════════════════════════════════════════════════════════════════════
    # CorruptionShield / anomaly detection
    # Statistical anomalies, behavioral anomalies, schema violations, business-rule violations
    # ═══════════════════════════════════════════════════════════════════════════
    ANOMALY_STATISTICAL = "anomaly_statistical"
    """L3 statistical anomaly detected (Z-score based)."""

    ANOMALY_BEHAVIORAL = "anomaly_behavioral"
    """Behavioral anomaly detected (sequence-pattern deviation)."""

    SCHEMA_VIOLATION = "schema_violation"
    """L1 schema violation (missing required field, type mismatch)."""

    BUSINESS_RULE_VIOLATION = "business_rule_violation"
    """L2 business-rule violation."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Audit integrity
    # Detection of audit-log tampering attempts and hash-chain integrity violations
    # ═══════════════════════════════════════════════════════════════════════════
    AUDIT_TAMPERING = "audit_tampering"
    """Audit-log tampering attempt detected."""

    HASH_CHAIN_BROKEN = "hash_chain_broken"
    """ContinuousAuditRecorder hash-chain integrity violation."""

    WAL_CORRUPTION = "wal_corruption"
    """WAL CRC32 checksum mismatch."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Governance violations (priority 1 - v2.3.0)
    # ═══════════════════════════════════════════════════════════════════════════
    UNAUTHORIZED_OVERRIDE = "unauthorized_override"
    """Unauthorized configuration-change attempt."""

    GOVERNANCE_BYPASS_ATTEMPT = "governance_bypass_attempt"
    """Kill Switch/Emergency Mode bypass attempt."""

    PRIVILEGE_ESCALATION = "privilege_escalation"
    """Privilege-escalation attempt."""


class Severity(str, Enum):
    """Severity levels for security incidents."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"


# Severity mapping for each violation type
SEVERITY_BY_VIOLATION_TYPE: dict[str, Severity] = {
    # Existing ViolationType
    ViolationType.SIGNATURE_INVALID: Severity.CRITICAL,
    ViolationType.DATA_TAMPERED: Severity.CRITICAL,
    ViolationType.TOKEN_FORGED: Severity.CRITICAL,
    ViolationType.REPLAY_ATTACK: Severity.CRITICAL,
    ViolationType.UNAUTHORIZED_ACCESS: Severity.HIGH,
    ViolationType.INJECTION_ATTEMPT: Severity.HIGH,
    ViolationType.RATE_LIMIT_ABUSE: Severity.MEDIUM,
    ViolationType.SUSPICIOUS_ACTIVITY: Severity.MEDIUM,
    # Baldur loop detection (v2.0.0 - priority 0.5)
    ViolationType.RECOVERY_LOOP_DETECTED: Severity.CRITICAL,
    ViolationType.CONFLICTING_ADJUSTMENT: Severity.HIGH,
    ViolationType.HEALING_TIMEOUT: Severity.MEDIUM,
    ViolationType.FLAPPING_DETECTED: Severity.HIGH,
    # ═══════════════════════════════════════════════════════════════════════════
    # New ViolationType Severity (priority 2 - v2.3.0)
    # ═══════════════════════════════════════════════════════════════════════════
    # Audit integrity - most severe (block immediately)
    ViolationType.AUDIT_TAMPERING: Severity.CRITICAL,
    ViolationType.HASH_CHAIN_BROKEN: Severity.CRITICAL,
    ViolationType.WAL_CORRUPTION: Severity.CRITICAL,
    # Governance violations - severe (block immediately)
    ViolationType.GOVERNANCE_BYPASS_ATTEMPT: Severity.CRITICAL,
    ViolationType.PRIVILEGE_ESCALATION: Severity.CRITICAL,
    # CorruptionShield / anomaly detection - HIGH (block, store in DLQ)
    ViolationType.ANOMALY_STATISTICAL: Severity.HIGH,
    ViolationType.ANOMALY_BEHAVIORAL: Severity.HIGH,
    ViolationType.UNAUTHORIZED_OVERRIDE: Severity.HIGH,
    ViolationType.BUSINESS_RULE_VIOLATION: Severity.HIGH,
    # Schema violation - MEDIUM (logging, monitoring)
    ViolationType.SCHEMA_VIOLATION: Severity.MEDIUM,
}
