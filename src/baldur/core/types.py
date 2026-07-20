"""
Common type definitions for the baldur system.

History: this module previously contained early-design types (FailureType,
OperationStatus, RetryContext, MetricsSnapshot) that were superseded by:
- interfaces/repositories.py: FailedOperationStatus, FailedOperationDomain
- services/retry_handler/models.py: RetryPolicyConfig, RetryResult
- interfaces/statistics.py: StatusCounts, CircuitBreakerSummary
- services/metrics/definitions.py: Prometheus label-based domain metrics

All dead types were removed per 194_DEAD_CODE_REMOVAL_PLAN.md. Per
``504_DLQ_PROTECT_CONTEXT_CAPTURE.md`` D8, this module now hosts the shared
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

__all__ = ["ALLOWED_PRIMITIVE_TYPES", "is_primitive_annotation"]

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
