"""
Configuration History & Rollback Service.

Redis에 설정 변경 이력을 저장하고 롤백 기능 제공.

Features:
- 변경 시 자동 버전 저장
- 최근 N개 버전 유지
- 특정 버전으로 롤백
- Redis 장애 시 Graceful Degradation

Usage:
    from baldur.services.config_history import get_config_history_service

    service = get_config_history_service()

    # 버전 저장
    version = service.save_version(
        config_type="circuit_breaker",
        values={"failure_threshold": 10},
        changed_by="admin",
        reason="Increase threshold for high load",
    )

    # 이력 조회
    history = service.get_history("circuit_breaker", limit=10)

    # 롤백
    rolled_back = service.rollback(
        config_type="circuit_breaker",
        target_version=1,
        rolled_back_by="admin",
    )

Audit:
- save_version: log_config_apply_audit(status="applied")
- rollback: log_rollback_audit(state="completed")

Reference:
    92_CONFIG_IMPLEMENTATION_GUIDE.md Week 4 [20] AuditSettings 참조.
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
