"""
Tiering System Enums.

Enumerations for tier matching, override types, and fallback reasons.
"""

from __future__ import annotations

from enum import Enum


class TierFallbackReason(str, Enum):
    """Fallback reason for Shadow Audit tracking."""

    NONE = "none"
    CONFIG_MISSING = "config_missing"
    ENGINE_ERROR = "engine_error"
    ENGINE_TIMEOUT = "engine_timeout"
    CIRCUIT_OPEN = "circuit_open"
    STATIC_PATH_MATCH = "static_path_match"


class TierMatchType(str, Enum):
    """Pattern matching type for tier mappings."""

    EXACT = "exact"
    WILDCARD = "wildcard"
    REGEX = "regex"


class OverrideIdentifierType(str, Enum):
    """Type of override identifier."""

    IP = "ip"
    USER_ID = "user_id"
    API_KEY = "api_key"


# PatternType은 TierMatchType의 별칭 (tiering views에서 사용)
PatternType = TierMatchType
