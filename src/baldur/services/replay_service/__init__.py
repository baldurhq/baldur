"""
DLQ Replay Service Package.

Provides DLQ replay functionality. Supports manual replay, batch replay,
and conditional replay triggered by circuit-breaker recovery events.

Modules:
    - models: ReplayResult / BatchReplayResult dataclasses
    - handlers: ReplayHandler ABC, DefaultReplayHandler, handler registry
    - service: ReplayService class, singleton, convenience functions

Usage:
    from baldur.services.replay_service import (
        ReplayService,
        ReplayResult,
        BatchReplayResult,
        get_replay_service,
        replay_failed_operation,
        batch_replay_by_failure_type,
        register_replay_handler,
        get_replay_handler,
    )

.. versionadded:: 7.0.0
    Migrated from a flat ``replay_service.py`` file to a ``replay_service/`` package layout.

Status: Public
"""

# ---------------------------------------------------------------------------
# Dynamic attribute forwarding – expose ALL sub-module attributes at package
# level so that ``from baldur.services.replay_service import <name>``
# keeps working for every symbol, including imports pulled into sub-modules
# (e.g. check_all_governance, log_dlq_replay_audit, GovernanceCheckResult).
#
# This is CRITICAL for test patches such as:
#   @patch("baldur.services.replay_service.check_all_governance")
#   @patch("baldur.services.replay_service.log_dlq_replay_audit")
# ---------------------------------------------------------------------------
import importlib as _importlib
import sys as _sys
import types as _types
from typing import Any as _Any

from baldur.services.replay_service.arming import (
    ArmingStatus,
    get_on_recovery_arming_status,
)
from baldur.services.replay_service.handlers import (
    DefaultReplayHandler,
    ReplayHandler,
    _replay_handlers,
    get_replay_handler,
    register_replay_handler,
)
from baldur.services.replay_service.models import (
    BatchReplayResult,
    ReplayResult,
)
from baldur.services.replay_service.service import (
    ReplayService,
    _replay_service,
    batch_replay_by_failure_type,
    get_replay_service,
    logger,
    replay_failed_operation,
    reset_replay_service,
)

_SUB_MODULES = ("models", "handlers", "service", "arming")

from baldur.services.replay_service import (
    arming as _arming_mod,
)
from baldur.services.replay_service import (
    handlers as _handlers_mod,
)
from baldur.services.replay_service import (
    models as _models_mod,
)
from baldur.services.replay_service import (
    service as _service_mod,
)

_pkg = _sys.modules[__name__]
for _mod in (
    _models_mod,
    _handlers_mod,
    _service_mod,
    _arming_mod,
):
    for _name in dir(_mod):
        if not _name.startswith("__") and not hasattr(_pkg, _name):
            setattr(_pkg, _name, getattr(_mod, _name))
del _name, _mod, _pkg


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


__all__ = [
    # Models
    "ReplayResult",
    "BatchReplayResult",
    # Handlers
    "ReplayHandler",
    "DefaultReplayHandler",
    "register_replay_handler",
    "get_replay_handler",
    # Service
    "ReplayService",
    "get_replay_service",
    "reset_replay_service",
    "replay_failed_operation",
    "batch_replay_by_failure_type",
    "logger",
    # Arming probe
    "ArmingStatus",
    "get_on_recovery_arming_status",
]

# Install the forwarding proxy AFTER ``__all__`` so the proxy snapshot at
# ``_proxy.__dict__.update(_current.__dict__)`` includes the runtime
# ``__all__`` attribute. Pre-D9 ordering set the proxy first, leaving
# ``replay_service.__all__`` invisible at runtime even though the AST
# audit could see it.
_current = _sys.modules[__name__]
_proxy = _ForwardingModule(__name__)
_proxy.__dict__.update(_current.__dict__)
_proxy.__path__ = _current.__path__  # type: ignore[attr-defined]
_proxy.__package__ = _current.__package__
_proxy.__spec__ = _current.__spec__
_proxy.__file__ = _current.__file__  # type: ignore[attr-defined]
_proxy.__loader__ = getattr(_current, "__loader__", None)
_sys.modules[__name__] = _proxy
