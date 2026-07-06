"""
Default Tiering Configuration Templates.

Best practice defaults for tier definitions, mappings, and overrides.
Also includes per-Backpressure-Level tier multiplier rules.
"""

from __future__ import annotations

from baldur.settings.backpressure import BackpressureLevel

from .enums import OverrideIdentifierType, TierMatchType
from .models import TierDefinition, TierMapping, TierOverride

# =============================================================================
# L1: Static Critical Paths (Defense-in-Depth - Last Line of Defense)
# =============================================================================

# Immutable set - requires code deployment to change
STATIC_CRITICAL_PATHS = frozenset(
    [
        "/api/baldur/control/",
        "/api/baldur/emergency/",
        "/api/auth/token/",
    ]
)

# Prefix matching optimization (tuple for startswith)
STATIC_CRITICAL_PREFIXES = (
    "/api/baldur/control/",
    "/api/baldur/emergency/",
)


# =============================================================================
# Default Templates (Best Practices)
# =============================================================================


DEFAULT_TIER_DEFINITIONS: list[TierDefinition] = [
    TierDefinition(
        id="critical",
        name="Mission Critical",
        multiplier=0.5,  # 50% allowed in emergency
        priority=100,
        description="Core APIs that must keep working even during an outage",
        color="#FF0000",
    ),
    TierDefinition(
        id="standard",
        name="Operational",
        multiplier=0.1,  # 10% allowed in emergency
        priority=50,
        description="General operational APIs",
        color="#FFA500",
    ),
    TierDefinition(
        id="non_essential",
        name="Non-Essential",
        multiplier=0.0,  # Blocked in emergency
        priority=10,
        description="Non-essential APIs (Load Shedding target)",
        color="#808080",
    ),
]


DEFAULT_TIER_MAPPINGS: list[TierMapping] = [
    # =========================================================================
    # Method-Specific Mappings (method+path combos take priority over path-only)
    # =========================================================================
    # POST/PUT/PATCH/DELETE /config/* → critical (config changes are healing actions)
    TierMapping(
        pattern="/api/baldur/config/*",
        tier_id="critical",
        pattern_type=TierMatchType.WILDCARD,
        priority=70,
        description="Config-change API (write)",
        methods=frozenset({"POST", "PUT", "PATCH", "DELETE"}),
    ),
    # POST/PUT/DELETE /dlq/* → critical (DLQ reprocessing is a healing action)
    TierMapping(
        pattern="/api/baldur/dlq/*",
        tier_id="critical",
        pattern_type=TierMatchType.WILDCARD,
        priority=70,
        description="DLQ reprocessing (write)",
        methods=frozenset({"POST", "PUT", "DELETE"}),
    ),
    # GET/HEAD /config/* → non_essential (config reads are non-essential)
    TierMapping(
        pattern="/api/baldur/config/*",
        tier_id="non_essential",
        pattern_type=TierMatchType.WILDCARD,
        priority=55,
        description="Config-read API (read)",
        methods=frozenset({"GET", "HEAD"}),
    ),
    # =========================================================================
    # Path-Only Mappings (apply to all HTTP methods)
    # =========================================================================
    # Critical (Tier 1) - Baldur control actions
    TierMapping(
        pattern="/api/baldur/control/",
        tier_id="critical",
        pattern_type=TierMatchType.EXACT,
        priority=100,
        description="Self-healing control actions",
    ),
    TierMapping(
        pattern="/api/baldur/allow/*",
        tier_id="critical",
        pattern_type=TierMatchType.WILDCARD,
        priority=100,
        description="Self-healing allow actions",
    ),
    TierMapping(
        pattern="/api/baldur/block/*",
        tier_id="critical",
        pattern_type=TierMatchType.WILDCARD,
        priority=100,
        description="Self-healing block actions",
    ),
    TierMapping(
        pattern="/api/baldur/system/*",
        tier_id="critical",
        pattern_type=TierMatchType.WILDCARD,
        priority=95,
        description="System control such as kill switch",
    ),
    # Standard (Tier 2) - Operational tasks
    TierMapping(
        pattern="/api/baldur/config/*",
        tier_id="standard",
        pattern_type=TierMatchType.WILDCARD,
        priority=50,
        description="Config-change API",
    ),
    TierMapping(
        pattern="/api/baldur/dlq/*",
        tier_id="standard",
        pattern_type=TierMatchType.WILDCARD,
        priority=50,
        description="DLQ-related API",
    ),
    TierMapping(
        pattern="/api/baldur/audit/",
        tier_id="standard",
        pattern_type=TierMatchType.EXACT,
        priority=50,
        description="Audit log query",
    ),
    TierMapping(
        pattern="/api/baldur/status/*",
        tier_id="standard",
        pattern_type=TierMatchType.WILDCARD,
        priority=50,
        description="Status query",
    ),
    # Non-Essential (Tier 3) - Dashboard, metrics
    TierMapping(
        pattern="/api/baldur/dashboard/*",
        tier_id="non_essential",
        pattern_type=TierMatchType.WILDCARD,
        priority=10,
        description="Dashboard API",
    ),
    TierMapping(
        pattern="/api/baldur/metrics/",
        tier_id="non_essential",
        pattern_type=TierMatchType.EXACT,
        priority=10,
        description="Metrics query API",
    ),
    TierMapping(
        pattern=r"/api/baldur/chaos/reports/.*",
        tier_id="non_essential",
        pattern_type=TierMatchType.REGEX,
        priority=10,
        description="Chaos report API",
    ),
]


DEFAULT_TIER_OVERRIDES: list[TierOverride] = [
    TierOverride(
        identifier="10.0.0.0/8",
        identifier_type=OverrideIdentifierType.IP,
        tier_id="critical",
        reason="Internal monitoring system",
    ),
    TierOverride(
        identifier="172.16.0.0/12",
        identifier_type=OverrideIdentifierType.IP,
        tier_id="critical",
        reason="Internal network",
    ),
    TierOverride(
        identifier="192.168.0.0/16",
        identifier_type=OverrideIdentifierType.IP,
        tier_id="critical",
        reason="Internal network",
    ),
]


# =============================================================================
# Per-Backpressure-Level Tier Traffic Multiplier Rules
# =============================================================================
# Same pattern as Emergency Mode's EMERGENCY_LEVEL_RULES
# (services/emergency_mode/enums.py). Used by TieringMiddleware for the
# Most Restrictive Wins merge. Higher values allow more traffic
# (1.0 = allow all, 0.0 = block all).

BACKPRESSURE_TIER_RULES: dict[BackpressureLevel, dict[str, float]] = {
    BackpressureLevel.NONE: {"critical": 1.0, "standard": 1.0, "non_essential": 1.0},
    BackpressureLevel.LOW: {"critical": 1.0, "standard": 1.0, "non_essential": 0.5},
    BackpressureLevel.MEDIUM: {"critical": 1.0, "standard": 0.8, "non_essential": 0.2},
    BackpressureLevel.HIGH: {"critical": 1.0, "standard": 0.5, "non_essential": 0.05},
    BackpressureLevel.CRITICAL: {
        "critical": 0.8,
        "standard": 0.1,
        "non_essential": 0.02,
    },
}
