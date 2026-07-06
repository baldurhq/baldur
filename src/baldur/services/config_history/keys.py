"""
Configuration History - Redis Key Helpers & Legacy Constants.

Redis Key Helpers (Multi-Cluster Support)
Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

from baldur.settings.audit import get_audit_settings

# =============================================================================
# Redis Key Helpers (Multi-Cluster Support)
# Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
# =============================================================================


def _get_key_prefix() -> str:
    """
    Get namespace-aware key prefix.

    Returns:
        Key prefix like "baldur:seoul:" or "baldur:"
    """
    from baldur.settings.namespace import get_namespace_settings

    return get_namespace_settings().get_key_prefix()


def _get_config_history_key(config_type: str) -> str:
    """Get config history key with namespace support."""
    return f"{_get_key_prefix()}config:history:{config_type}"


def _get_config_version_key(config_type: str) -> str:
    """Get config version counter key with namespace support."""
    return f"{_get_key_prefix()}config:version:{config_type}"


def _get_config_current_key(config_type: str) -> str:
    """Get current config key with namespace support."""
    return f"{_get_key_prefix()}config:current:{config_type}"


# Legacy constants (for backward compatibility with imports)
# These still work but use the dynamic functions internally
CONFIG_HISTORY_KEY = "baldur:config:history:{config_type}"
CONFIG_VERSION_COUNTER_KEY = "baldur:config:version:{config_type}"
CONFIG_CURRENT_KEY = "baldur:config:current:{config_type}"


def _get_max_history_entries() -> int:
    """Get max history entries from AuditSettings."""
    return get_audit_settings().config_history_entries
