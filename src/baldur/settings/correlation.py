"""
Correlation Engine Settings — DAG construction and co-occurrence analysis.

Controls the time window relative to the trigger event (lookback/lookahead),
debounce cooldown, DAG size limits, Co-occurrence Tracker settings, and more
through environment variables.

Environment Variables:
    BALDUR_CORRELATION_LOOKBACK_SECONDS=300
    BALDUR_CORRELATION_LOOKAHEAD_SECONDS=60
    BALDUR_CORRELATION_COOLDOWN_SECONDS=60.0
    BALDUR_CORRELATION_MAX_EVENTS_PER_DAG=200
    BALDUR_CORRELATION_MIN_CONFIDENCE=0.4
    BALDUR_CORRELATION_MAX_GRAPH_DEPTH=3
    BALDUR_CORRELATION_WINDOW_SECONDS=300
    BALDUR_CORRELATION_ZSCORE_THRESHOLD=2.5
    BALDUR_CORRELATION_MIN_CO_OCCURRENCES=3
    BALDUR_CORRELATION_MAX_TRACKED_PAIRS=1000
    BALDUR_CORRELATION_ANALYSIS_INTERVAL=60
    BALDUR_CORRELATION_MAX_EVENT_BUFFER=500
    BALDUR_CORRELATION_COUNT_HISTORY_SIZE=100
    BALDUR_CORRELATION_SIMULTANEOUS_THRESHOLD_SECONDS=0.001
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class CorrelationSettings(BaseSettings):
    """Correlation Engine DAG construction and co-occurrence analysis settings."""

    model_config = make_settings_config("BALDUR_CORRELATION_")

    # ── DAG construction settings ──

    lookback_seconds: int = Field(
        default=300,
        ge=30,
        le=900,
        description="Past capture range relative to trigger event (seconds)",
    )

    lookahead_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Future capture range relative to trigger event (seconds)",
    )

    cooldown_seconds: float = Field(
        default=60.0,
        ge=10.0,
        le=600.0,
        description="Cooldown to prevent DAG regeneration in same namespace (seconds)",
    )

    max_events_per_dag: int = Field(
        default=200,
        ge=10,
        le=1000,
        description="Maximum events per single DAG",
    )

    min_confidence: float = Field(
        default=0.4,
        ge=0.1,
        le=1.0,
        description="Edges below this confidence value are removed from DAG",
    )

    max_graph_depth: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum DAG graph depth (prevents OOM)",
    )

    # ── Co-occurrence Tracker settings ──

    window_seconds: float = Field(
        default=300.0,
        ge=30.0,
        le=3600.0,
        description="Co-occurrence detection time window (seconds)",
    )

    zscore_threshold: float = Field(
        default=2.5,
        ge=1.0,
        le=5.0,
        description="Z-score outlier threshold",
    )

    min_co_occurrences: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Minimum co-occurrence count (below this is ignored)",
    )

    max_tracked_pairs: int = Field(
        default=1000,
        ge=10,
        le=10000,
        description="Maximum number of tracked event pairs",
    )

    analysis_interval: float = Field(
        default=60.0,
        ge=10.0,
        le=600.0,
        description="Analysis tick interval (seconds)",
    )

    max_event_buffer: int = Field(
        default=500,
        ge=50,
        le=10000,
        description="Maximum timestamp buffer per event type",
    )

    count_history_size: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Count history size per pair",
    )

    simultaneous_threshold_seconds: float = Field(
        default=0.001,
        ge=0.0,
        le=1.0,
        description="Simultaneous arrival threshold (seconds). Time gaps below this are excluded from directionality analysis",
    )


def get_correlation_settings() -> "CorrelationSettings":
    from baldur.settings.root import get_config

    return get_config().obs.correlation


def reset_correlation_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().obs.__dict__["correlation"]
    except KeyError:
        pass
