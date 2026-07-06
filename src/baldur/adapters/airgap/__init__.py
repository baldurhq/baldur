"""
Air-Gap Storage Adapters.

Provides an abstraction layer between Baldur engine and business DB.
The engine reads metrics from Air-Gap storage (Redis) instead of directly
accessing the business database.

Design Philosophy:
- Baldur engine NEVER touches business DB directly
- Business layer writes summaries to Air-Gap storage
- Engine reads from Air-Gap storage only

Usage:
    >>> from baldur.adapters.airgap import get_airgap_adapter
    >>> adapter = get_airgap_adapter()
    >>>
    >>> # Business layer writes
    >>> adapter.write_summary("dlq:payment:pending", 5)
    >>>
    >>> # Baldur engine reads
    >>> count = adapter.read_summary("dlq:payment:pending")
"""

from baldur.adapters.airgap.base import (
    AirGapStorageAdapter,
    BaseAirGapAdapter,
)
from baldur.adapters.airgap.factory import (
    configure_airgap_adapter,
    get_airgap_adapter,
    reset_airgap_adapter,
)
from baldur.adapters.airgap.null_adapter import NullAirGapAdapter

__all__ = [
    "AirGapStorageAdapter",
    "BaseAirGapAdapter",
    "NullAirGapAdapter",
    "get_airgap_adapter",
    "configure_airgap_adapter",
    "reset_airgap_adapter",
]
