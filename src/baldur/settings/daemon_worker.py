"""DaemonWorkerSettings — observability and respawn dials for daemon workers.

Cross-shape settings for the ~40 daemon-thread worker singletons covered by
impl 489. Operators tune liveness staleness tolerance and the global
respawn behavior here; per-worker overrides live on each
``DaemonWorkerHandle`` (e.g. ``staleness_threshold_seconds`` for slow-tick
workers that should not be flagged at the global multiplier).

Environment Variables:
    BALDUR_DAEMON_WORKER_DEFAULT_STALENESS_MULTIPLIER=2.0
    BALDUR_DAEMON_WORKER_RESPAWN_ENABLED=false
    BALDUR_DAEMON_WORKER_RESPAWN_MAX_ATTEMPTS=3
    BALDUR_DAEMON_WORKER_RESPAWN_BACKOFF_BASE_SECONDS=1.0
    BALDUR_DAEMON_WORKER_RESPAWN_BACKOFF_MAX_SECONDS=60.0
    BALDUR_DAEMON_WORKER_RESPAWN_COUNT_RESET_SECONDS=3600.0

Per impl 489 D11.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class DaemonWorkerSettings(BaseSettings):
    """Settings for the cross-shape daemon worker observability layer (impl 489)."""

    model_config = make_settings_config("BALDUR_DAEMON_WORKER_")

    default_staleness_multiplier: float = Field(
        default=2.0,
        ge=1.0,
        le=100.0,
        description=(
            "Multiplier applied to a handle's tick_interval_seconds when no "
            "explicit staleness_threshold_seconds was supplied. A value of "
            "2.0 flags a worker as UNHEALTHY when the heartbeat is older "
            "than two tick intervals."
        ),
    )
    respawn_enabled: bool = Field(
        default=False,
        description=(
            "Global kill-switch for auto-respawn of dead daemon worker "
            "threads. Default False — operators opt in after dashboard "
            "verification. Per-worker opt-in is also required: a worker "
            "must register with restart_callback != None to be eligible."
        ),
    )
    respawn_max_attempts: int = Field(
        default=3,
        ge=1,
        le=100,
        description=(
            "Maximum number of restart attempts per handle before the "
            "respawn coordinator gives up. The handle's restart_count "
            "field is the gate counter and is reset to 0 after the worker "
            "has been observed HEALTHY for respawn_count_reset_seconds."
        ),
    )
    respawn_backoff_base_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=300.0,
        description=(
            "Base delay for ExponentialBackoff between respawn attempts. "
            "Reuses core.backoff.ExponentialBackoff with jitter=True so "
            "concurrent dead workers do not converge on the same retry "
            "timestamp."
        ),
    )
    respawn_backoff_max_seconds: float = Field(
        default=60.0,
        ge=0.1,
        le=3600.0,
        description="Cap on the per-attempt backoff delay.",
    )
    respawn_count_reset_seconds: float = Field(
        default=3600.0,
        ge=60.0,
        le=86_400.0,
        description=(
            "Sustained-health window: when a worker has been observed "
            "HEALTHY for this many seconds, the handle's restart_count "
            "gate counter resets to 0. The lifetime Prometheus Counter "
            "(baldur_daemon_worker_restarts_total) is unaffected — "
            "operators detect borderline flakiness via PromQL rate()."
        ),
    )


def get_daemon_worker_settings() -> DaemonWorkerSettings:
    from baldur.settings.root import get_config

    return get_config().meta.daemon_worker


def reset_daemon_worker_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["daemon_worker"]
    except KeyError:
        pass
