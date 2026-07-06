"""
Baldur Control API Service

Provides the core business logic for the Baldur Control API.

Provides the logic for service blocking/allowing, fault injection, risk
assessment, and control-request handling.

Converted from the flat ``control_api_service.py`` file to the
``control_api_service/`` package.
Existing ``from baldur.services.control_api_service import X`` imports remain compatible.
"""

from __future__ import annotations

# === Explicit re-exports ===
# Models
from .models import (
    ControlRequest,
    ControlResponse,
    ReasonClassification,
)

# Risk
from .risk import (
    assess_risk_level,
    classify_reason,
)

# Service
from .service import (
    ControlAPIService,
    configure_control_api_service,
    get_control_api_service,
    logger,
    reset_control_api_service,
)

__all__ = [
    # models
    "ReasonClassification",
    "ControlRequest",
    "ControlResponse",
    # risk
    "classify_reason",
    "assess_risk_level",
    # service
    "ControlAPIService",
    "get_control_api_service",
    "configure_control_api_service",
    "reset_control_api_service",
    "logger",
]


# === Dynamic forwarding (event_bus setattr pattern) ===
# Ensures `baldur.services.control_api_service.X` resolves to the actual
# object in whichever sub-module defines it, so mock.patch targets keep working.

import importlib as _importlib  # noqa: E402
import sys as _sys  # noqa: E402
import types as _types  # noqa: E402
from typing import Any as _Any  # noqa: E402

_SUB_MODULES = ("models", "risk", "service")

# Eager copy of all sub-module attributes to package level
from . import models as _models_mod  # noqa: E402
from . import risk as _risk_mod
from . import service as _service_mod

_pkg = _sys.modules[__name__]
for _mod in (_models_mod, _risk_mod, _service_mod):
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
