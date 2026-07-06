"""
Security Action Policies.

Defines immediate response policies for security violations
and their priority ordering.
"""

from __future__ import annotations

from enum import Enum

from baldur.services.security.types import ViolationType


class ActionPolicy(str, Enum):
    """
    Immediate response policies (highest priority first).

    Guarantees atomicity of protective actions - the "strongest policy first" principle.
    """

    # Priority 1: system-wide protection
    EMERGENCY_LEVEL_3 = "emergency_level_3"
    """System-wide protection mode."""

    EMERGENCY_LEVEL_2 = "emergency_level_2"
    """Partial system protection mode."""

    EMERGENCY_LEVEL_1 = "emergency_level_1"
    """Warning mode."""

    # Priority 2: user/session isolation
    ACCOUNT_FREEZE = "account_freeze"
    """Account freeze."""

    SESSION_INVALIDATE = "session_invalidate"
    """Invalidate all sessions."""

    # Priority 3: network blocking
    IP_PERMANENT_BAN = "ip_permanent_ban"
    """Permanent IP ban."""

    IP_TEMPORARY_BAN = "ip_temporary_ban"
    """Temporary IP ban."""

    # Priority 4: logging
    BLOCK_AND_LOG = "block_and_log"
    """Block and log."""


# Policy priority (lower number = higher priority)
ACTION_POLICY_PRIORITY: dict[ActionPolicy, int] = {
    ActionPolicy.EMERGENCY_LEVEL_3: 1,
    ActionPolicy.EMERGENCY_LEVEL_2: 2,
    ActionPolicy.EMERGENCY_LEVEL_1: 3,
    ActionPolicy.ACCOUNT_FREEZE: 4,
    ActionPolicy.SESSION_INVALIDATE: 5,
    ActionPolicy.IP_PERMANENT_BAN: 6,
    ActionPolicy.IP_TEMPORARY_BAN: 7,
    ActionPolicy.BLOCK_AND_LOG: 8,
}


# ViolationType → ActionPolicy mapping
ACTION_POLICY_BY_VIOLATION_TYPE: dict[ViolationType, list[ActionPolicy]] = {
    # Baldur loop detection - the most severe violation
    ViolationType.RECOVERY_LOOP_DETECTED: [
        ActionPolicy.EMERGENCY_LEVEL_3,  # full protection!
    ],
    # High-severity security violations
    ViolationType.TOKEN_FORGED: [
        ActionPolicy.SESSION_INVALIDATE,
        ActionPolicy.IP_TEMPORARY_BAN,
    ],
    ViolationType.SIGNATURE_INVALID: [
        ActionPolicy.BLOCK_AND_LOG,
        ActionPolicy.IP_TEMPORARY_BAN,
    ],
    ViolationType.REPLAY_ATTACK: [
        ActionPolicy.BLOCK_AND_LOG,
        ActionPolicy.IP_TEMPORARY_BAN,
    ],
    ViolationType.DATA_TAMPERED: [
        ActionPolicy.SESSION_INVALIDATE,
        ActionPolicy.IP_PERMANENT_BAN,
    ],
    ViolationType.UNAUTHORIZED_ACCESS: [
        ActionPolicy.BLOCK_AND_LOG,
    ],
    ViolationType.INJECTION_ATTEMPT: [
        ActionPolicy.IP_TEMPORARY_BAN,
        ActionPolicy.BLOCK_AND_LOG,
    ],
    # Default policy
    ViolationType.RATE_LIMIT_ABUSE: [
        ActionPolicy.IP_TEMPORARY_BAN,
    ],
    ViolationType.SUSPICIOUS_ACTIVITY: [
        ActionPolicy.BLOCK_AND_LOG,
    ],
    # Baldur-related
    ViolationType.FLAPPING_DETECTED: [
        ActionPolicy.BLOCK_AND_LOG,
    ],
    ViolationType.CONFLICTING_ADJUSTMENT: [
        ActionPolicy.BLOCK_AND_LOG,
    ],
    ViolationType.HEALING_TIMEOUT: [
        ActionPolicy.BLOCK_AND_LOG,
    ],
    # ═══════════════════════════════════════════════════════════════════════════
    # New ViolationType ActionPolicy mapping (priority 2 - v2.3.0)
    # ═══════════════════════════════════════════════════════════════════════════
    # Governance violations - most severe (multiple policies)
    ViolationType.PRIVILEGE_ESCALATION: [
        ActionPolicy.EMERGENCY_LEVEL_2,
        ActionPolicy.SESSION_INVALIDATE,
        ActionPolicy.ACCOUNT_FREEZE,
    ],
    ViolationType.GOVERNANCE_BYPASS_ATTEMPT: [
        ActionPolicy.EMERGENCY_LEVEL_2,
        ActionPolicy.SESSION_INVALIDATE,
    ],
    # Audit integrity - severe (permanent IP ban)
    ViolationType.AUDIT_TAMPERING: [
        ActionPolicy.EMERGENCY_LEVEL_2,
        ActionPolicy.IP_PERMANENT_BAN,
    ],
    ViolationType.HASH_CHAIN_BROKEN: [
        ActionPolicy.EMERGENCY_LEVEL_2,
        ActionPolicy.BLOCK_AND_LOG,
    ],
    ViolationType.WAL_CORRUPTION: [
        ActionPolicy.EMERGENCY_LEVEL_1,
        ActionPolicy.BLOCK_AND_LOG,
    ],
    # CorruptionShield / anomaly detection
    ViolationType.ANOMALY_STATISTICAL: [
        ActionPolicy.BLOCK_AND_LOG,
    ],
    ViolationType.ANOMALY_BEHAVIORAL: [
        ActionPolicy.BLOCK_AND_LOG,
        ActionPolicy.IP_TEMPORARY_BAN,
    ],
    ViolationType.UNAUTHORIZED_OVERRIDE: [
        ActionPolicy.SESSION_INVALIDATE,
        ActionPolicy.BLOCK_AND_LOG,
    ],
    ViolationType.BUSINESS_RULE_VIOLATION: [
        ActionPolicy.BLOCK_AND_LOG,
    ],
    ViolationType.SCHEMA_VIOLATION: [
        ActionPolicy.BLOCK_AND_LOG,
    ],
}
