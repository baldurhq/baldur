"""
Correlation Engine Orchestrator Settings — engine ON/OFF, analysis interval, integration flags.

Co-occurrence Tracker, DAG Builder etc. sub-module settings are
managed in ``settings.correlation.CorrelationSettings``.
This file defines orchestrator (service-level) settings only.

Environment Variables:
    BALDUR_CORRELATION_ENGINE_ENABLED=true
    BALDUR_CORRELATION_ENGINE_ANALYSIS_INTERVAL_SECONDS=60.0
    BALDUR_CORRELATION_ENGINE_WEIGHT_TOPOLOGY=0.35
    BALDUR_CORRELATION_ENGINE_WEIGHT_TEMPORAL=0.25
    BALDUR_CORRELATION_ENGINE_WEIGHT_BLAST_RADIUS=0.25
    BALDUR_CORRELATION_ENGINE_WEIGHT_HISTORICAL=0.15
    BALDUR_CORRELATION_ENGINE_LEARNING_INTEGRATION_ENABLED=true
    BALDUR_CORRELATION_ENGINE_POSTMORTEM_INTEGRATION_ENABLED=true
    BALDUR_CORRELATION_ENGINE_STATE_PERSISTENCE_ENABLED=true
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import Probability


class CorrelationEngineSettings(BaseSettings):
    """Metric Correlation Engine orchestrator settings.

    Env prefix: BALDUR_CORRELATION_ENGINE_
    e.g. BALDUR_CORRELATION_ENGINE_ENABLED=true
    """

    model_config = make_settings_config("BALDUR_CORRELATION_ENGINE_")

    # -- Global ON/OFF --
    enabled: bool = Field(
        default=False,
        description="Correlation Engine activation toggle",
    )

    # -- Periodic analysis tick --
    analysis_interval_seconds: float = Field(
        default=60.0,
        ge=10.0,
        le=600.0,
        description="Periodic analysis tick interval (seconds)",
    )

    # -- Root Cause Ranker weights --
    weight_topology: Probability = Field(default=0.35)
    weight_temporal: Probability = Field(default=0.25)
    weight_blast_radius: Probability = Field(default=0.25)
    weight_historical: Probability = Field(default=0.15)

    # -- Integration flags --
    learning_integration_enabled: bool = Field(
        default=False,
        description="LearningService pattern accumulation integration",
    )
    postmortem_integration_enabled: bool = Field(
        default=False,
        description="Postmortem automatic timeline injection",
    )
    state_persistence_enabled: bool = Field(
        default=False,
        description="StateBackend persistence (cold start prevention)",
    )


def get_correlation_engine_settings() -> "CorrelationEngineSettings":
    from baldur.settings.root import get_config

    return get_config().obs.correlation_engine


def reset_correlation_engine_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().obs.__dict__["correlation_engine"]
    except KeyError:
        pass
