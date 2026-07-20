"""
Safety Bounds - autonomous tuning safety limits.

Guards autonomous tuning against drifting into a dangerous range.

Key features:
- Per-parameter min/max range validation
- Maximum change ratio allowed in a single cycle
- Runtime bound updates (admin only)

Values are overridable via SafetyBoundsSettings environment variables:
- BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MIN / MAX / MAX_CHANGE
- BALDUR_SAFETY_BOUNDS_RETRY_COUNT_MIN / MAX / MAX_CHANGE
- Other parameters...
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

import structlog

from baldur.settings.safety_bounds import get_safety_bounds_settings

logger = structlog.get_logger()


@dataclass
class ParameterBound:
    """Parameter bound."""

    min_value: float
    max_value: float
    max_change_per_cycle: float  # Max change ratio per cycle (0.3 = 30%)

    def validate(self) -> bool:
        """Validate the bound configuration."""
        if self.min_value > self.max_value:
            return False
        return not (self.max_change_per_cycle <= 0 or self.max_change_per_cycle > 1)


class SafetyBounds:
    """
    Safety bound management.

    Protects system stability by constraining the range of autonomous tuning.

    Protection mechanisms:
    1. Absolute range: min_value ~ max_value
    2. Change limit: within max_change_per_cycle % per cycle
    3. Rejection of unknown parameters
    """

    @classmethod
    def _get_default_bounds(cls) -> dict[str, ParameterBound]:
        """
        Load the default bounds from SafetyBoundsSettings.

        Returns the per-parameter bounds, overridable via environment
        variables.
        """
        settings = get_safety_bounds_settings()
        return {
            "timeout_ms": ParameterBound(
                min_value=settings.timeout_ms_min,
                max_value=settings.timeout_ms_max,
                max_change_per_cycle=settings.timeout_ms_max_change,
            ),
            "retry_count": ParameterBound(
                min_value=settings.retry_count_min,
                max_value=settings.retry_count_max,
                max_change_per_cycle=settings.retry_count_max_change,
            ),
            "circuit_breaker_threshold": ParameterBound(
                min_value=settings.circuit_breaker_threshold_min,
                max_value=settings.circuit_breaker_threshold_max,
                max_change_per_cycle=settings.circuit_breaker_threshold_max_change,
            ),
            "jitter_range": ParameterBound(
                min_value=settings.jitter_range_min,
                max_value=settings.jitter_range_max,
                max_change_per_cycle=settings.jitter_range_max_change,
            ),
            "rate_limit_rps": ParameterBound(
                min_value=settings.rate_limit_rps_min,
                max_value=settings.rate_limit_rps_max,
                max_change_per_cycle=settings.rate_limit_rps_max_change,
            ),
            "throttle_sla_warning_ms": ParameterBound(
                min_value=settings.throttle_sla_warning_ms_min,
                max_value=settings.throttle_sla_warning_ms_max,
                max_change_per_cycle=settings.throttle_sla_warning_ms_max_change,
            ),
            "throttle_sla_critical_ms": ParameterBound(
                min_value=settings.throttle_sla_critical_ms_min,
                max_value=settings.throttle_sla_critical_ms_max,
                max_change_per_cycle=settings.throttle_sla_critical_ms_max_change,
            ),
            "backoff_base_ms": ParameterBound(
                min_value=settings.backoff_base_ms_min,
                max_value=settings.backoff_base_ms_max,
                max_change_per_cycle=settings.backoff_base_ms_max_change,
            ),
            "backoff_max_ms": ParameterBound(
                min_value=settings.backoff_max_ms_min,
                max_value=settings.backoff_max_ms_max,
                max_change_per_cycle=settings.backoff_max_ms_max_change,
            ),
            "connection_pool_size": ParameterBound(
                min_value=settings.connection_pool_size_min,
                max_value=settings.connection_pool_size_max,
                max_change_per_cycle=settings.connection_pool_size_max_change,
            ),
        }

    def __init__(
        self,
        custom_bounds: dict[str, dict[str, float]] | None = None,
        strict_mode: bool = True,
    ):
        """
        Args:
            custom_bounds: Custom bound configuration
            strict_mode: True rejects unknown parameters
        """
        self._lock = RLock()
        self.strict_mode = strict_mode

        # Copy the defaults (loaded from settings)
        default_bounds = self._get_default_bounds()
        self.bounds: dict[str, ParameterBound] = {
            k: ParameterBound(
                min_value=v.min_value,
                max_value=v.max_value,
                max_change_per_cycle=v.max_change_per_cycle,
            )
            for k, v in default_bounds.items()
        }

        # Apply custom bounds
        if custom_bounds:
            for param, config in custom_bounds.items():
                self.update_bounds(param, config)

        logger.info(
            "safety_bounds.initialized_parameters",
            bounds_count=len(self.bounds),
        )

    def is_within_bounds(
        self,
        parameter: str,
        new_value: float,
        current_value: float | None = None,
    ) -> bool:
        """
        Check whether a value is within the safety bounds.

        Args:
            parameter: Parameter name
            new_value: New value
            current_value: Current value (for change-ratio validation)

        Returns:
            True if within the safety bounds
        """
        with self._lock:
            bound = self.bounds.get(parameter)

            if bound is None:
                if self.strict_mode:
                    logger.warning(
                        "safety_bounds.unknown_parameter_rejected",
                        safety_parameter=parameter,
                    )
                    return False
                logger.debug(
                    "safety_bounds.unknown_parameter_allowed_non",
                    safety_parameter=parameter,
                )
                return True

            # Range validation
            if new_value < bound.min_value:
                logger.warning(
                    "safety_bounds.below_minimum",
                    safety_parameter=parameter,
                    new_value=new_value,
                    bound=bound.min_value,
                )
                return False

            if new_value > bound.max_value:
                logger.warning(
                    "safety_bounds.above_maximum",
                    safety_parameter=parameter,
                    new_value=new_value,
                    bound=bound.max_value,
                )
                return False

            # Change-ratio validation
            if current_value is not None and current_value > 0:
                change_ratio = abs(new_value - current_value) / current_value
                if change_ratio > bound.max_change_per_cycle:
                    logger.warning(
                        "safety_bounds.change_ratio_exceeds_limit",
                        safety_parameter=parameter,
                        change_ratio=change_ratio,
                        bound=bound.max_change_per_cycle,
                    )
                    return False

            return True

    def clamp_to_bounds(
        self,
        parameter: str,
        value: float,
        current_value: float | None = None,
    ) -> float:
        """
        Clamp a value into the safety bounds.

        Args:
            parameter: Parameter name
            value: Desired value
            current_value: Current value (for change-ratio limiting)

        Returns:
            The value adjusted into the safety bounds
        """
        with self._lock:
            bound = self.bounds.get(parameter)

            if bound is None:
                return value

            # Apply the absolute range
            clamped = max(bound.min_value, min(value, bound.max_value))

            # Apply the change-ratio limit
            if current_value is not None and current_value > 0:
                max_change = current_value * bound.max_change_per_cycle
                if abs(clamped - current_value) > max_change:
                    # Keep the direction of change, cap only the magnitude
                    if clamped > current_value:
                        clamped = current_value + max_change
                    else:
                        clamped = current_value - max_change

            return clamped

    def update_bounds(
        self,
        parameter: str,
        config: dict[str, float],
    ) -> bool:
        """
        Update a bound at runtime (admin only).

        Args:
            parameter: Parameter name
            config: {"min_value": x, "max_value": y, "max_change_per_cycle": z}

        Returns:
            True on success
        """
        with self._lock:
            try:
                new_bound = ParameterBound(
                    min_value=config.get("min_value", 0),
                    max_value=config.get("max_value", float("inf")),
                    max_change_per_cycle=config.get("max_change_per_cycle", 0.3),
                )

                if not new_bound.validate():
                    logger.error(
                        "safety_bounds.invalid_bound_config",
                        safety_parameter=parameter,
                    )
                    return False

                self.bounds[parameter] = new_bound
                logger.info(
                    "safety_bounds.updated_bounds",
                    safety_parameter=parameter,
                    new_bound=new_bound.min_value,
                    max_value=new_bound.max_value,
                    max_change_per_cycle=new_bound.max_change_per_cycle,
                )
                return True
            except Exception as e:
                logger.exception(
                    "safety_bounds.update_bounds_failed",
                    error=e,
                )
                return False

    def remove_bounds(self, parameter: str) -> bool:
        """Remove a bound."""
        with self._lock:
            if parameter in self.bounds:
                del self.bounds[parameter]
                logger.info(
                    "safety_bounds.removed_bounds",
                    safety_parameter=parameter,
                )
                return True
            return False

    def get_bounds(self, parameter: str) -> dict[str, float] | None:
        """Get the bound for a specific parameter."""
        with self._lock:
            bound = self.bounds.get(parameter)
            if bound is None:
                return None
            return {
                "min_value": bound.min_value,
                "max_value": bound.max_value,
                "max_change_per_cycle": bound.max_change_per_cycle,
            }

    def get_all_bounds(self) -> dict[str, dict[str, float]]:
        """Get all bounds."""
        with self._lock:
            return {
                param: {
                    "min_value": bound.min_value,
                    "max_value": bound.max_value,
                    "max_change_per_cycle": bound.max_change_per_cycle,
                }
                for param, bound in self.bounds.items()
            }

    def check_all(
        self,
        values: dict[str, float],
        current_values: dict[str, float] | None = None,
    ) -> dict[str, bool]:
        """Validate several values at once."""
        results = {}
        for param, value in values.items():
            current = current_values.get(param) if current_values else None
            results[param] = self.is_within_bounds(param, value, current)
        return results

    def reset_to_defaults(self) -> None:
        """Reset to the default bounds (reloaded from settings)."""
        with self._lock:
            default_bounds = self._get_default_bounds()
            self.bounds = {
                k: ParameterBound(
                    min_value=v.min_value,
                    max_value=v.max_value,
                    max_change_per_cycle=v.max_change_per_cycle,
                )
                for k, v in default_bounds.items()
            }
            logger.info("safety_bounds.reset_defaults")


__all__ = [
    "SafetyBounds",
    "ParameterBound",
]
