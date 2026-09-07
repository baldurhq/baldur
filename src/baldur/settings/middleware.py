"""
Baldur HTTP status-code vocabulary - Pydantic v2.

Configures which response statuses count as a circuit-breaker failure and
which count as a rate-limit answer. Read by BaldurMiddleware and the
framework-free middleware helpers on inbound responses, and by the outbound
circuit-breaker stage when a protected call *returns* a response instead of
raising - one operator answer covers both directions.

Environment Variables:
    BALDUR_MIDDLEWARE_CB_STATUS_CODES=[500,502,503,504]
    BALDUR_MIDDLEWARE_RATE_LIMIT_CODES=[429]
    BALDUR_MIDDLEWARE_RETRY_AFTER_MAX=300
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "BaldurMiddlewareSettings",
    "get_middleware_settings",
    "reset_middleware_settings",
]


class BaldurMiddlewareSettings(BaseSettings):
    """
    Settings for Baldur's HTTP status-code classification.

    Controls which HTTP status codes trigger CB failure recording,
    rate limit cascade detection, and Retry-After header clamping.

    The two status sets are read on both sides of a call: inbound by
    BaldurMiddleware and the framework-free helpers, outbound by the circuit
    breaker stage classifying a returned response. Membership is
    non-exclusive - a status listed in both sets records a failure *and* feeds
    the rate-limit cascade.
    """

    model_config = make_settings_config("BALDUR_MIDDLEWARE_")

    cb_status_codes: list[int] = Field(
        default=[500, 502, 503, 504],
        description=(
            "HTTP status codes to record as CB failures, inbound and outbound"
        ),
    )

    rate_limit_codes: list[int] = Field(
        default=[429],
        description=(
            "HTTP status codes to treat as rate limit responses, inbound and outbound"
        ),
    )

    retry_after_max: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Maximum Retry-After wait time in seconds",
    )


def get_middleware_settings() -> BaldurMiddlewareSettings:
    from baldur.settings.root import get_config

    return get_config().adapters.middleware


def reset_middleware_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["middleware"]
    except KeyError:
        pass
