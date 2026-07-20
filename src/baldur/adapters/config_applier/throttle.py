"""
ConfigApplier dedicated to AdaptiveThrottle SLA settings.

Implementation of the ConfigApplier Protocol.
Adjusts only whitelisted SLA parameters (sla_warning_ms, sla_critical_ms) and
guarantees thread-safety via a model_copy() atomic swap.

Non-whitelisted parameters (rate_limit_rps, etc.) stay a no-op plus a log line
to preserve backward compatibility.
"""

from typing import Any, cast

import structlog

from baldur.factory.registry import ProviderRegistry

logger = structlog.get_logger()


class ThrottleConfigApplier:
    """
    ConfigApplier dedicated to AdaptiveThrottle SLA settings.

    Adjusts only whitelisted parameters and guarantees thread-safety via a
    model_copy() atomic swap.

    Non-whitelisted parameters (rate_limit_rps, etc.) stay a no-op plus a log
    line to preserve backward compatibility.
    """

    # Adjustable parameter -> config attribute name mapping
    PARAM_TO_CONFIG: dict[str, str] = {
        "throttle_sla_warning_ms": "sla_warning_ms",
        "throttle_sla_critical_ms": "sla_critical_ms",
    }

    # Legacy parameters treated as a no-op (backward compatibility)
    LEGACY_NOOP_PARAMS: set[str] = {"rate_limit_rps"}

    def get_current(self, parameter: str) -> float:
        """Read the current value."""
        # No-op legacy parameter
        if parameter in self.LEGACY_NOOP_PARAMS:
            return 0.0

        config_attr = self.PARAM_TO_CONFIG.get(parameter)
        if config_attr is None:
            raise ValueError(
                f"Parameter '{parameter}' not supported by ThrottleConfigApplier. "
                f"Allowed: {set(self.PARAM_TO_CONFIG.keys())}"
            )

        throttle = ProviderRegistry.adaptive_throttle.safe_get()
        if throttle is None:
            raise RuntimeError(
                "ThrottleConfigApplier requires baldur_pro AdaptiveThrottle"
            )
        # PRO impl exposes `.config`; OSS Protocol intentionally omits it
        # (impl-specific introspection used by the config applier).
        return float(getattr(cast(Any, throttle).config, config_attr))

    def apply(self, parameter: str, value: float) -> bool:
        """
        Apply a setting — atomic swap.

        Builds a new config object with Pydantic v2 model_copy(update=...) and
        replaces the throttle.config reference in one step.
        Reference assignment is atomic under the Python GIL, so this is safe
        even while _maybe_adjust_limit() is running.
        """
        # No-op legacy parameter — report success (backward compatibility)
        if parameter in self.LEGACY_NOOP_PARAMS:
            logger.info(
                "throttle_config_applier.deprecated_no_op_use",
                config_parameter=parameter,
            )
            return True

        config_attr = self.PARAM_TO_CONFIG.get(parameter)
        if config_attr is None:
            return False

        throttle = ProviderRegistry.adaptive_throttle.safe_get()
        if throttle is None:
            raise RuntimeError(
                "ThrottleConfigApplier requires baldur_pro AdaptiveThrottle"
            )
        # PRO impl exposes `.config`; OSS Protocol omits it intentionally.
        throttle_any = cast(Any, throttle)
        old_config = throttle_any.config

        # Atomic swap: build a new object with model_copy(), then swap the ref
        new_config = old_config.model_copy(update={config_attr: int(value)})
        throttle_any.config = new_config  # GIL atomic reference swap

        logger.info(
            "throttle_config_applier.applied_config_swap",
            config_parameter=parameter,
            getattr=getattr(old_config, config_attr),
            int=int(value),
        )
        return True

    def rollback(self, parameter: str, value: float) -> bool:
        """Apply a rollback — same logic as apply()."""
        return self.apply(parameter, value)
