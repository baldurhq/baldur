"""
Baldur Utilities.

Provides utility functions for the baldur system.

Status: Internal
"""

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
