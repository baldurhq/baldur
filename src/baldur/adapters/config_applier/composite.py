"""
Composite ConfigApplier that combines the per-module ConfigAppliers.

Walks the appliers list in order and lets the first applier that can handle
the request serve it. Returns False when no applier can handle it.
"""

from typing import Protocol

import structlog

logger = structlog.get_logger()


class ConfigApplierProtocol(Protocol):
    """ConfigApplier Protocol (identical to the runtime-feedback definition)."""

    def get_current(self, parameter: str) -> float: ...
    def apply(self, parameter: str, value: float) -> bool: ...
    def rollback(self, parameter: str, value: float) -> bool: ...


class CompositeConfigApplier:
    """
    Composite that combines the per-module ConfigAppliers.

    Walks the appliers list in order and lets the first applier that can
    handle the request serve it. Returns False when no applier can handle it.
    """

    def __init__(self, appliers: list[ConfigApplierProtocol]):
        if not appliers:
            raise ValueError("CompositeConfigApplier requires at least one applier")
        self._appliers = appliers

    def get_current(self, parameter: str) -> float:
        """Read the value from the first applier that can handle it."""
        last_error: Exception | None = None
        for applier in self._appliers:
            try:
                return applier.get_current(parameter)
            except (ValueError, KeyError) as e:
                last_error = e
                continue
        # Every applier failed -> propagate the last error
        raise ValueError(
            f"No applier can handle parameter '{parameter}'"
        ) from last_error

    def apply(self, parameter: str, value: float) -> bool:
        """Delegate to the first applier that returns True."""
        for applier in self._appliers:
            if applier.apply(parameter, value):
                return True
        logger.warning(
            "composite_config_applier.no_applier_handled",
            config_parameter=parameter,
            config_value=value,
        )
        return False

    def rollback(self, parameter: str, value: float) -> bool:
        """Roll back using the same routing logic as apply()."""
        return any(applier.rollback(parameter, value) for applier in self._appliers)
