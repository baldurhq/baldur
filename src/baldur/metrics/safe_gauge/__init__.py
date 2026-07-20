"""
SafeGauge - Thread-safe Prometheus Gauge Wrapper.

Prevents negative gauge values after server restarts.

Usage:
    >>> from baldur.metrics.safe_gauge import SafeGauge
    >>> from prometheus_client import Gauge
    >>>
    >>> raw = Gauge("dlq_pending", "Pending DLQ items", ["domain"])
    >>> safe = SafeGauge(raw)
    >>> safe.labels(domain="payment").inc()
    >>> safe.labels(domain="payment").dec()  # Won't go below 0

Module Structure:
    - core.py: SafeGauge, SafeGaugeChild (core wrappers)
    - sync.py: SyncStatus, SyncInfo (sync status tracking)
    - clamping.py: clamp_non_negative, clamp_percentage, safe_set_gauge
      (utilities)
    - noop.py: NoOpGaugeChild (no-op implementation)
"""

from .clamping import clamp_non_negative, clamp_percentage, safe_set_gauge
from .core import SafeGauge, SafeGaugeChild
from .noop import NoOpGaugeChild
from .sync import SyncInfo, SyncStatus

__all__ = [
    # Core
    "SafeGauge",
    "SafeGaugeChild",
    # Sync
    "SyncStatus",
    "SyncInfo",
    # Clamping
    "clamp_non_negative",
    "clamp_percentage",
    "safe_set_gauge",
    # NoOp
    "NoOpGaugeChild",
]
