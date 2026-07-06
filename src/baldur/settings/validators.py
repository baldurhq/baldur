"""
Reusable validator factories for settings classes.

Provides factory functions that generate validator callables
for common validation patterns (threshold warnings, enum checks).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import structlog

logger = structlog.get_logger()

# TypeVar so the field type (int or float) flows through the validator
# unchanged — a settings field declared `int` still gets back `int`, not
# `int | float`. Constrained to int|float so str/etc don't sneak in.
_NumberT = TypeVar("_NumberT", int, float)


def warn_above(
    threshold: float | int,
    event: str,
    *,
    extra_fields: dict[str, Any] | None = None,
) -> Callable[[_NumberT], _NumberT]:
    """Return a validator function that logs a warning when value exceeds threshold.

    Usage in a Settings class:

        @field_validator("failure_threshold")
        @classmethod
        def _warn_failure_threshold(cls, v):
            return warn_above(50, "safe_default.high_consider_using_safety")(v)

    Args:
        threshold: Value above which a warning is logged.
        event: Structured log event name.
        extra_fields: Additional fields to include in the log.
    """

    def _check(v: _NumberT) -> _NumberT:
        if v > threshold:
            fields = {"setting_value": v}
            if extra_fields:
                fields.update(extra_fields)
            logger.warning(event, **fields)
        return v

    return _check


def warn_below(
    threshold: float | int,
    event: str,
    *,
    extra_fields: dict[str, Any] | None = None,
) -> Callable[[_NumberT], _NumberT]:
    """Return a validator function that logs a warning when value is below threshold.

    Args:
        threshold: Value below which a warning is logged.
        event: Structured log event name.
        extra_fields: Additional fields to include in the log.
    """

    def _check(v: _NumberT) -> _NumberT:
        if v < threshold:
            fields = {"setting_value": v}
            if extra_fields:
                fields.update(extra_fields)
            logger.warning(event, **fields)
        return v

    return _check


__all__ = [
    "warn_above",
    "warn_below",
]
