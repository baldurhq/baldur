"""
Tenacity bridge package — wraps ``tenacity.Retrying`` into Baldur's
``ResiliencePolicy[T]`` Protocol.

Public API:
    TenacityBridgePolicy — explicit Policy with constructor-injected guards.
    instrument_tenacity  — Level-1 monkey-patch for global observation.

Import safety:
    ``tenacity`` is an optional dependency (``pip install baldur-framework[tenacity]``).
    The module always imports successfully so IDE auto-import / symbol
    discovery works without the extra. ``TenacityBridgePolicy.__init__()`` and
    ``instrument_tenacity()`` raise (or skip) at first use when tenacity is
    not installed.

Reference:
    451
"""

from __future__ import annotations

try:
    import tenacity as _tenacity_module  # noqa: F401

    _TENACITY_AVAILABLE = True
except ImportError:
    _tenacity_module = None  # type: ignore[assignment]
    _TENACITY_AVAILABLE = False


from baldur.bridges.tenacity.instrument import (
    instrument_tenacity,
    is_instrumented,
)
from baldur.bridges.tenacity.policy import (
    AsyncTenacityBridgePolicy,
    TenacityBridgePolicy,
)

__all__ = [
    "TenacityBridgePolicy",
    "AsyncTenacityBridgePolicy",
    "instrument_tenacity",
    "is_instrumented",
]
