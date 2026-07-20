"""
Execution Services Package

Converted from the flat ``execution_services.py`` file to the
``execution_services/`` package, keeping existing
``from baldur.services.execution_services import X`` imports working.
"""

from __future__ import annotations

# Chaos Service
from .chaos_service import (
    ChaosExecutionService,
    _chaos_execution_service_instance,
    get_chaos_execution_service,
)

# Config Apply Service
from .config_apply_service import (
    ConfigApplyService,
    _config_apply_service_instance,
    get_config_apply_service,
)

# === Explicit re-exports ===
# Models
from .models import (
    ApprovalCleanupResult,
    DailyReportResult,
    ExperimentExecutionResult,
    PendingApprovalCheckResult,
)

__all__ = [
    # Chaos
    "ChaosExecutionService",
    "ExperimentExecutionResult",
    "DailyReportResult",
    "ApprovalCleanupResult",
    "PendingApprovalCheckResult",
    "get_chaos_execution_service",
    # Config Apply
    "ConfigApplyService",
    "get_config_apply_service",
]


# === Dynamic forwarding (event_bus setattr pattern) ===
# Ensures `baldur.services.execution_services.X` resolves to the actual
# object in whichever sub-module defines it, so mock.patch targets keep working.

import importlib as _importlib  # noqa: E402
import types as _types  # noqa: E402

_SUB_MODULES = ("models", "chaos_service", "config_apply_service")


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
