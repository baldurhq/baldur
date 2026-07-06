"""
Retry Handler Package

Provides retry mechanisms with:
- RetryPolicy: Pure retry policy (recommended, PolicyComposer compatible)
- Guards: KillSwitchGuard, ErrorBudgetGuard
- Sinks: DLQSink

.. versionadded:: 2.1.0
    ``retry_handler.py`` flat file to ``retry_handler/`` package.

.. versionchanged:: 2.3.0
    RetryHandler removed. Use RetryPolicy + PolicyComposer instead.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import types as _types
from typing import Any as _Any

# Guards
from .guards import ErrorBudgetGuard, KillSwitchGuard

# === Explicit re-exports ===
# Models
from .models import (
    MaxRetriesExceededError,
    RetryAction,
    RetryConfig,
    RetryPolicyConfig,
    RetryResult,
    T,
)

# Policy
from .policy import RetryPolicy

# Rate Limit Detection
from .rate_limit_detection import detect_rate_limit

# Sinks
from .sinks import DLQSink

__all__ = [
    # models
    "RetryAction",
    "MaxRetriesExceededError",
    "RetryConfig",
    "RetryPolicyConfig",
    "RetryResult",
    "T",
    # policy
    "RetryPolicy",
    # guards
    "KillSwitchGuard",
    "ErrorBudgetGuard",
    # sinks
    "DLQSink",
    # rate limit
    "detect_rate_limit",
]

# =============================================================================
# Dynamic forwarding (event_bus setattr pattern)
# =============================================================================
# Eagerly copy all sub-module attributes to package level.

_SUB_MODULES = (
    "models",
    "policy",
    "guards",
    "sinks",
    "rate_limit_detection",
)

from . import guards as _guards_mod  # noqa: E402
from . import models as _models_mod  # noqa: E402
from . import policy as _policy_mod  # noqa: E402
from . import rate_limit_detection as _rate_limit_mod  # noqa: E402
from . import sinks as _sinks_mod  # noqa: E402

_pkg = _sys.modules[__name__]
for _mod in (
    _models_mod,
    _policy_mod,
    _guards_mod,
    _sinks_mod,
    _rate_limit_mod,
):
    for _name in dir(_mod):
        if not _name.startswith("__") and not hasattr(_pkg, _name):
            setattr(_pkg, _name, getattr(_mod, _name))
del _name, _mod


def __getattr__(name: str) -> _Any:
    """Dynamic attribute forwarding from all sub-modules."""
    for _sub in _SUB_MODULES:
        try:
            _mod = _importlib.import_module(f".{_sub}", __name__)
            if hasattr(_mod, name):
                _val = getattr(_mod, name)
                globals()[name] = _val
                return _val
        except ImportError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# =============================================================================
# Module proxy for mock.patch support (setattr forwarding)
# =============================================================================


class _ForwardingModule(_types.ModuleType):
    """Module proxy: forwards __setattr__ to owning sub-module."""

    def __getattr__(self, name: str) -> _Any:
        for _sub in _SUB_MODULES:
            try:
                _mod = _importlib.import_module(f".{_sub}", self.__name__)
                if hasattr(_mod, name):
                    _val = getattr(_mod, name)
                    object.__setattr__(self, name, _val)
                    return _val
            except ImportError:
                continue
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value: _Any) -> None:
        if not name.startswith("__"):
            for _sub in _SUB_MODULES:
                try:
                    _mod = _importlib.import_module(f".{_sub}", self.__name__)
                    if name in _mod.__dict__:
                        _mod.__dict__[name] = value
                        break
                except (ImportError, AttributeError):
                    continue
        object.__setattr__(self, name, value)


_current = _sys.modules[__name__]
_proxy = _ForwardingModule(__name__)
_proxy.__dict__.update(_current.__dict__)
_proxy.__path__ = _current.__path__  # type: ignore[attr-defined]
_proxy.__package__ = _current.__package__
_proxy.__spec__ = _current.__spec__
_proxy.__file__ = _current.__file__  # type: ignore[attr-defined]
_proxy.__loader__ = getattr(_current, "__loader__", None)
_sys.modules[__name__] = _proxy
