"""
Common type definitions for the baldur system.

History: this module previously contained early-design types (FailureType,
OperationStatus, RetryContext, MetricsSnapshot) that were superseded by:
- interfaces/repositories.py: FailedOperationStatus, FailedOperationDomain
- services/retry_handler/models.py: RetryPolicyConfig, RetryResult
- interfaces/statistics.py: StatusCounts, CircuitBreakerSummary
- services/metrics/definitions.py: Prometheus label-based domain metrics

All dead types were removed. Per 504 D8, this module now hosts the shared
primitive whitelist consumed by ``@idempotent`` (cache key fold-in) and
``@protected`` / ``@dlq_protect`` (context auto-extract for DLQ visibility).
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

__all__ = [
    "ALLOWED_PRIMITIVE_TYPES",
    "is_primitive_annotation",
    "primitive_json_default",
]

# Primitive types that the decorator layer is willing to fold into cache keys
# (``@idempotent``) or context snapshots (``@protected`` / ``@dlq_protect``)
# without coercion. All entries are immutable types, so captured values are
# safe to retain across retries — no ``copy.deepcopy()`` is needed.
#
# Container types (dict, list, tuple, set) are deliberately absent: capturing
# a structured payload should go through the explicit ``context_from=Callable``
# escape hatch so the user takes responsibility for redaction shape.
ALLOWED_PRIMITIVE_TYPES: tuple[type, ...] = (
    int,
    str,
    bool,
    float,
    Decimal,
    bytes,
    UUID,
    Enum,
    type(None),
    datetime,
    date,
    dtime,
    timedelta,
)


def is_primitive_annotation(annotation: Any) -> bool:
    """Return True iff ``annotation`` is a known-safe primitive type.

    Conservative: unknown / generic / forward-reference annotations return
    False so the runtime fallback (``isinstance(value, ALLOWED_PRIMITIVE_TYPES)``)
    is the final gate.
    """
    if annotation is inspect.Parameter.empty:
        return False
    if isinstance(annotation, type):
        return issubclass(annotation, ALLOWED_PRIMITIVE_TYPES)
    return False


def primitive_json_default(value: Any) -> Any:
    """JSON ``default=`` hook for the whitelisted primitives JSON cannot hold.

    A context snapshot keeps every ``ALLOWED_PRIMITIVE_TYPES`` value as is, so
    a store that encodes the snapshot as JSON must accept all of them, or the
    entry carrying one is refused. Enum members encode as their value and
    temporal values as ISO-8601, the forms orjson writes natively, so a value
    reads back the same from every store; ``Decimal``, ``UUID``, ``bytes`` and
    ``timedelta`` encode as ``str(value)``. Any other type still raises
    ``TypeError``.

    Usable with both ``json.dumps(default=...)`` and ``orjson.dumps(default=...)``.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID, bytes, timedelta)):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
