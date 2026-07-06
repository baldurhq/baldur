"""
Resilient Recorder Settings - Pydantic v2.

Single Source of Truth for resilient continuous audit recorder configuration.

Replaces:
- audit/resilient_recorder.py:ResilientRecorderConfig

Environment Variables:
    BALDUR_RESILIENT_RECORDER_BUFFER_CAPACITY=10000
    BALDUR_RESILIENT_RECORDER_FLUSH_INTERVAL=1.0
    BALDUR_RESILIENT_RECORDER_FLUSH_BATCH_SIZE=100
    ... etc

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_BATCH_SIZE,
    STANDARD_TIMEOUT_SECONDS,
    HugeCount,
    MediumCount,
    MediumDuration,
    ShortDuration,
)
from baldur.settings.validators import warn_below


class ResilientRecorderSettings(BaseSettings):
    """
    Resilient Continuous Audit Recorder configuration with validation.

    장애 허용 기능을 가진 연속 감사 기록기 설정입니다.

    Features:
    - RingBuffer: 비침투 Shadow Logging
    - CircuitBreaker: resilience.py 재사용
    - Self-Audit: 자체 상태 기록
    - SyslogFallback: 최후의 수단

    All defaults match:
    - audit/resilient_recorder.py:ResilientRecorderConfig
    """

    model_config = make_settings_config("BALDUR_RESILIENT_RECORDER_")

    # ==========================================================================
    # Buffer Settings
    # ==========================================================================
    buffer_capacity: int = Field(
        default=10000,
        ge=100,
        le=1000000,
        description="RingBuffer capacity (number of events)",
    )
    backpressure_strategy: str = Field(
        default="DROP_OLDEST",
        description="Backpressure strategy on buffer overflow: DROP_OLDEST, DROP_NEWEST, BLOCK",
    )

    # ==========================================================================
    # Background Worker Settings
    # ==========================================================================
    enable_background_flush: bool = Field(
        default=True,
        description="Enable background flush",
    )
    flush_interval_seconds: ShortDuration = Field(
        default=1.0,
        description="Flush interval (seconds)",
    )
    flush_batch_size: HugeCount = Field(
        default=STANDARD_BATCH_SIZE,
        description="Flush batch size",
    )

    # ==========================================================================
    # Circuit Breaker Settings
    # ==========================================================================
    circuit_failure_threshold: MediumCount = Field(
        default=3,
        description="Circuit Breaker failure threshold",
    )
    circuit_success_threshold: MediumCount = Field(
        default=2,
        description="Circuit Breaker success threshold (half-open to closed)",
    )
    circuit_timeout_seconds: MediumDuration = Field(
        default=STANDARD_TIMEOUT_SECONDS,
        description="Circuit Breaker timeout (seconds)",
    )
    circuit_call_timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=60.0,
        description="Individual call timeout (seconds). Prevents external backend hangs.",
    )

    # ==========================================================================
    # Fallback Settings
    # ==========================================================================
    fallback_file_path: str | None = Field(
        default=None,
        description="Fallback file path (None uses default location)",
    )
    enable_syslog_fallback: bool = Field(
        default=True,
        description="Enable syslog fallback",
    )

    @field_validator("backpressure_strategy")
    @classmethod
    def validate_backpressure_strategy(cls, v: str) -> str:
        """Validate backpressure strategy is valid."""
        valid_strategies = {"DROP_OLDEST", "DROP_NEWEST", "BLOCK"}
        if v not in valid_strategies:
            raise ValueError(
                f"Invalid backpressure_strategy: {v}. Valid options: {valid_strategies}"
            )
        return v

    @field_validator("circuit_failure_threshold")
    @classmethod
    def _warn_circuit_failure_threshold(cls, v: int) -> int:
        """Warn if circuit failure threshold is very low."""
        return warn_below(2, "resilient_recorder.circuit_failure_threshold_low")(v)

    # ==========================================================================
    # In-Memory Audit Buffer - from audit/resilience/buffer.py
    # ==========================================================================
    memory_buffer_max_entries: int = Field(
        default=10000,
        ge=100,
        le=100000,
        description="Maximum memory buffer entries on WAL failure. Prevents memory exhaustion.",
    )

    memory_buffer_flush_interval: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Memory buffer flush attempt interval (seconds). Default 30s.",
    )


def get_resilient_recorder_settings() -> "ResilientRecorderSettings":
    from baldur.settings.root import get_config

    return get_config().resilience.resilient_recorder


def reset_resilient_recorder_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().resilience.__dict__["resilient_recorder"]
    except KeyError:
        pass
