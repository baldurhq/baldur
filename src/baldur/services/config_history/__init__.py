"""
Configuration History & Rollback Service.

Stores config change history in Redis and provides rollback.

Features:
- Automatic version save on change
- Retention of the most recent N versions
- Rollback to a specific version
- Graceful degradation on Redis failure

Usage:
    from baldur.services.config_history import get_config_history_service

    service = get_config_history_service()

    # Save a version
    version = service.save_version(
        config_type="circuit_breaker",
        values={"failure_threshold": 10},
        changed_by="admin",
        reason="Increase threshold for high load",
    )

    # Read history
    history = service.get_history("circuit_breaker", limit=10)

    # Rollback
    rolled_back = service.rollback(
        config_type="circuit_breaker",
        target_version=1,
        rolled_back_by="admin",
    )

Audit:
- save_version: log_config_apply_audit(status="applied")
- rollback: log_rollback_audit(state="completed")

See also:
    AuditSettings for the audit configuration used by these calls.
"""

# === Explicit re-exports ===
from .keys import (
    CONFIG_CURRENT_KEY,
    CONFIG_HISTORY_KEY,
    CONFIG_VERSION_COUNTER_KEY,
    _get_config_current_key,
    _get_config_history_key,
    _get_config_version_key,
    _get_key_prefix,
    _get_max_history_entries,
)
from .models import ConfigVersion
from .service import (
    ConfigHistoryService,
    _config_history_service,
    get_config_history_service,
    logger,
    reset_config_history_service,
)

__all__ = [
    # keys
    "_get_key_prefix",
    "_get_config_history_key",
    "_get_config_version_key",
    "_get_config_current_key",
    "_get_max_history_entries",
    "CONFIG_HISTORY_KEY",
    "CONFIG_VERSION_COUNTER_KEY",
    "CONFIG_CURRENT_KEY",
    # models
    "ConfigVersion",
    # service
    "ConfigHistoryService",
    "get_config_history_service",
    "reset_config_history_service",
    "_config_history_service",
    "logger",
]


# === Dynamic forwarding for test patch compatibility ===
# Ensures `baldur.services.config_history.X` resolves to the actual
# object in whichever sub-module defines it, so mock.patch targets keep working.

import importlib as _importlib  # noqa: E402
import types as _types  # noqa: E402

_SUB_MODULES = ("keys", "models", "service")


def __getattr__(name: str):
    """Dynamic attribute forwarding from all sub-modules."""
    for _sub in _SUB_MODULES:
        _mod = _importlib.import_module(f".{_sub}", __name__)
        try:
            _val = getattr(_mod, name)
            # Cache on package for future access
            setattr(_types.ModuleType(__name__), name, _val)
            globals()[name] = _val
            return _val
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
