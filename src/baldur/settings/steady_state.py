"""
Steady State Settings - Pydantic v2.

Chaos 실험의 정상 상태(Steady State) 가설 검증 설정입니다.

Replaces:
- services/chaos/base/models.py:SteadyStateHypothesis 기본값

Environment Variables:
    BALDUR_STEADY_STATE_P50_LATENCY_MAX_MS=100.0
    BALDUR_STEADY_STATE_P99_LATENCY_MAX_MS=500.0
    BALDUR_STEADY_STATE_ERROR_RATE_MAX_PERCENT=0.1
    BALDUR_STEADY_STATE_THROUGHPUT_MIN_RPS=100.0
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class SteadyStateSettings(BaseSettings):
    """
    Steady State 가설 설정.

    Chaos 실험 전후 시스템의 '정상' 상태를 정의합니다.
    SteadyStateHypothesis.validate()에서 이 설정과 비교하여 검증합니다.

    Attributes:
        p50_latency_max_ms: P50 레이턴시 최대 허용값 (ms)
        p99_latency_max_ms: P99 레이턴시 최대 허용값 (ms)
        error_rate_max_percent: 에러율 최대 허용값 (%)
        throughput_min_rps: 최소 처리량 (requests per second)
    """

    model_config = make_settings_config("BALDUR_STEADY_STATE_")

    # ==========================================================================
    # Latency Thresholds (from services/chaos/base/models.py SteadyStateHypothesis)
    # ==========================================================================
    p50_latency_max_ms: float = Field(
        default=100.0,
        ge=1.0,
        le=10000.0,
        description="P50 latency maximum threshold (ms). 50% of requests must respond within this time",
    )

    p99_latency_max_ms: float = Field(
        default=500.0,
        ge=10.0,
        le=60000.0,
        description="P99 latency maximum threshold (ms). 99% of requests must respond within this time",
    )

    # ==========================================================================
    # Error Rate Threshold
    # ==========================================================================
    error_rate_max_percent: float = Field(
        default=0.1,
        ge=0.0,
        le=100.0,
        description="Maximum allowed error rate (%). 0.1 = 0.1% error tolerance",
    )

    # ==========================================================================
    # Throughput Threshold
    # ==========================================================================
    throughput_min_rps: float = Field(
        default=100.0,
        ge=0.0,
        le=1000000.0,
        description="Minimum throughput (requests per second). Below this value indicates performance degradation",
    )

    @field_validator("p99_latency_max_ms")
    @classmethod
    def validate_p99_latency(cls, v: float, info) -> float:
        """P99이 P50보다 커야 함."""
        # Note: cross-field validation은 model_validator에서 처리
        if v < 100.0:
            logger.warning(
                "safe_default.very_tight_ms_cause",
                setting_value=v,
            )
        return v

    @field_validator("error_rate_max_percent")
    @classmethod
    def validate_error_rate(cls, v: float) -> float:
        """에러율 경고."""
        if v > 5.0:
            logger.warning(
                "safe_default.high_miss_real_issues",
                setting_value=v,
            )
        return v

    @field_validator("throughput_min_rps")
    @classmethod
    def validate_throughput(cls, v: float) -> float:
        """처리량 경고."""
        if v == 0.0:
            logger.warning("safe_default.throughput_check_effectively_disabled")
        return v


def get_steady_state_settings() -> "SteadyStateSettings":
    from baldur.settings.root import get_config

    return get_config().slo_group.steady_state


def reset_steady_state_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().slo_group.__dict__["steady_state"]
    except KeyError:
        pass
