"""
Idempotency Package

Provides idempotency key management for safe retry operations.
A duplicate arrival of an already-processed key is rejected with an
explicit error instead of executing again; the original result is not
replayed.

Canonical location: ``baldur.services.idempotency``

Usage:
    from baldur.services.idempotency import (
        IdempotencyService,
        IdempotencyKey,
        IdempotencyDomain,
        get_idempotency_service,
    )

Idempotency-key management keeps retried operations from repeating
their side effects.
"""

from .anti_flapping import AntiFlappingWindow, get_anti_flapping_window
from .models import IdempotencyDomain, IdempotencyKey, IdempotencyResult, T
from .service import (
    IdempotencyService,
    get_idempotency_service,
    reset_idempotency_service,
)

__all__ = [
    # Models
    "IdempotencyDomain",
    "IdempotencyKey",
    "IdempotencyResult",
    "T",
    # Service
    "IdempotencyService",
    "get_idempotency_service",
    "reset_idempotency_service",
    # Anti-Flapping
    "AntiFlappingWindow",
    "get_anti_flapping_window",
]


# =============================================================================
# Dynamic forwarding (event_bus setattr pattern)
# =============================================================================
# Eagerly copy all sub-module attributes to package level.
# This is critical for backward compatibility with tests that do:
#   import baldur.services.idempotency as mod; mod.time
#   @patch("baldur.services.idempotency.X")

import importlib as _importlib  # noqa: E402
import sys as _sys  # noqa: E402
import types as _types  # noqa: E402
from typing import Any as _Any  # noqa: E402

_SUB_MODULES = ("models", "service", "anti_flapping")

from . import (  # noqa: E402
    anti_flapping as _anti_flapping_mod,
)
from . import (
    models as _models_mod,
)
from . import (
    service as _service_mod,
)

_pkg = _sys.modules[__name__]
for _mod in (_models_mod, _service_mod, _anti_flapping_mod):
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
