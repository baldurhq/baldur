"""
Baldur Utilities.

Provides utility functions for the baldur system.

Status: Internal
"""

# Lazy barrel — register names in `_LAZY_IMPORTS`; never add an eager
# top-level `from baldur.X import ...` here (defeats the lazy import path
# and is caught by the import-weight gate).

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.utils.async_logger import (
        AsyncHealingLogger,
        EventSeverity,
    )
    from baldur.utils.event_filters import should_handle_emergency_event
    from baldur.utils.jitter import (
        JitterConfig,
        async_sleep_with_jitter,
        calculate_jitter,
        sleep_with_jitter,
        with_jitter,
    )
    from baldur.utils.network import extract_client_ip
    from baldur.utils.template import SafeFormatDict
    from baldur.utils.time import (
        add_seconds,
        elapsed_seconds,
        ensure_aware,
        format_duration,
        from_iso_string,
        is_expired,
        to_iso_string,
        utc_now,
    )

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AsyncHealingLogger": ("baldur.utils.async_logger", "AsyncHealingLogger"),
    "EventSeverity": ("baldur.utils.async_logger", "EventSeverity"),
    "should_handle_emergency_event": (
        "baldur.utils.event_filters",
        "should_handle_emergency_event",
    ),
    "JitterConfig": ("baldur.utils.jitter", "JitterConfig"),
    "async_sleep_with_jitter": ("baldur.utils.jitter", "async_sleep_with_jitter"),
    "calculate_jitter": ("baldur.utils.jitter", "calculate_jitter"),
    "sleep_with_jitter": ("baldur.utils.jitter", "sleep_with_jitter"),
    "with_jitter": ("baldur.utils.jitter", "with_jitter"),
    "extract_client_ip": ("baldur.utils.network", "extract_client_ip"),
    "SafeFormatDict": ("baldur.utils.template", "SafeFormatDict"),
    "add_seconds": ("baldur.utils.time", "add_seconds"),
    "elapsed_seconds": ("baldur.utils.time", "elapsed_seconds"),
    "ensure_aware": ("baldur.utils.time", "ensure_aware"),
    "format_duration": ("baldur.utils.time", "format_duration"),
    "from_iso_string": ("baldur.utils.time", "from_iso_string"),
    "is_expired": ("baldur.utils.time", "is_expired"),
    "to_iso_string": ("baldur.utils.time", "to_iso_string"),
    "utc_now": ("baldur.utils.time", "utc_now"),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        # Resolve live on each access (no globals() memoization) so the barrel
        # transparently reflects the current submodule attribute — a test that
        # patches `<this package>.<submodule>.<name>` must not be shadowed by a
        # value cached from an earlier patch. importlib already caches the module
        # import, so the cost is a dict lookup.
        return getattr(importlib.import_module(module_path), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)


__all__ = [
    # Event filtering utilities (namespace-aware event handling)
    "should_handle_emergency_event",
    # Network utilities (canonical IP extraction)
    "extract_client_ip",
    "utc_now",
    "ensure_aware",
    "to_iso_string",
    "from_iso_string",
    "elapsed_seconds",
    "is_expired",
    "add_seconds",
    "format_duration",
    # Platinum SLA Optimization
    "AsyncHealingLogger",
    "EventSeverity",
    # Jitter utilities (Thundering Herd prevention)
    "with_jitter",
    "calculate_jitter",
    "sleep_with_jitter",
    "async_sleep_with_jitter",
    "JitterConfig",
    # Template utilities (safe format_map)
    "SafeFormatDict",
]
