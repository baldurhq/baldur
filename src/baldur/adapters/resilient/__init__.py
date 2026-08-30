# verified-by: test_wal_survives_memory_clear
"""
Resilient Storage Adapters.

Provides Redis-First + Graceful Degradation + WAL architecture
for zero data loss guarantees.
"""

from baldur.adapters.resilient.backend import (
    ResilientStorageBackend,
    ResilientStorageMode,
    probing_unconfigured_default,
)

__all__ = [
    "ResilientStorageBackend",
    "ResilientStorageMode",
    "probing_unconfigured_default",
]
