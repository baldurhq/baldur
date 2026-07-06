"""
BaldurMiddleware Settings - Pydantic v2.

Configures CB trigger status codes and rate limit handling
for BaldurMiddleware.

Environment Variables:
    BALDUR_MIDDLEWARE_CB_STATUS_CODES=[500,502,503,504]
    BALDUR_MIDDLEWARE_RATE_LIMIT_CODES=[429]
    BALDUR_MIDDLEWARE_RETRY_AFTER_MAX=300
"""

from __future__ import annotations

import warnings

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "BaldurMiddlewareSettings",
    "get_middleware_settings",
    "reset_middleware_settings",
]


class BaldurMiddlewareSettings(BaseSettings):
    """
    Settings for BaldurMiddleware CB trigger behavior.

    Controls which HTTP status codes trigger CB failure recording,
    rate limit cascade detection, and Retry-After header clamping.
    """

    model_config = make_settings_config("BALDUR_MIDDLEWARE_")

    cb_status_codes: list[int] = Field(
        default=[500, 502, 503, 504],
        description="HTTP status codes to record as CB failures",
    )

    rate_limit_codes: list[int] = Field(
        default=[429],
        description="HTTP status codes to treat as rate limit responses",
    )

    retry_after_max: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Maximum Retry-After wait time in seconds",
    )

    @model_validator(mode="after")
    def _warn_status_code_overlap(self) -> BaldurMiddlewareSettings:
        """Warn if cb_status_codes and rate_limit_codes overlap.

        Overlapping codes are dispatched to the CB failure branch (if/elif
        priority), silently bypassing rate limit cascade detection.
        """
        overlap = set(self.cb_status_codes) & set(self.rate_limit_codes)
        if overlap:
            warnings.warn(
                f"cb_status_codes and rate_limit_codes overlap on {overlap}. "
                f"Overlapping codes will only trigger CB failure recording; "
                f"rate limit cascade detection will be bypassed for those codes.",
                UserWarning,
                stacklevel=2,
            )
        return self


def get_middleware_settings() -> BaldurMiddlewareSettings:
    from baldur.settings.root import get_config

    return get_config().adapters.middleware


def reset_middleware_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["middleware"]
    except KeyError:
        pass
