"""
Stress Test Settings - Pydantic v2.

Pool 스트레스 테스트 관련 설정.
테스트 환경에서 DB Connection Pool 고갈 시뮬레이션에 사용됩니다.

Source:
- services/stress_test_service.py

Environment Variables:
    BALDUR_STRESS_TEST_DEFAULT_LOCK_TIMEOUT_MS=1
    BALDUR_STRESS_TEST_MAX_BURST_DURATION_SECONDS=30
    BALDUR_STRESS_TEST_MAX_CONCURRENT_LOCKS=100
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class StressTestSettings(BaseSettings):
    """
    스트레스 테스트 설정.

    Pool 고갈, Lock Contention 등 스트레스 테스트에 사용되는 설정값입니다.
    테스트 전용이며, 프로덕션에서는 사용되지 않습니다.
    """

    model_config = make_settings_config("BALDUR_STRESS_TEST_")

    # ==========================================================================
    # Lock Timeout Settings (from stress_test_service.py line 565)
    # ==========================================================================
    default_lock_timeout_ms: int = Field(
        default=1,
        ge=1,
        le=10000,
        description="Default lock acquisition timeout (ms). Minimum 1ms.",
    )

    # ==========================================================================
    # Burst Failure Settings (from stress_test_service.py lines 566-567)
    # ==========================================================================
    max_burst_duration_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Controlled Burst Failure maximum duration (seconds)",
    )
    max_concurrent_locks: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum concurrent lock attempts",
    )

    # ==========================================================================
    # Connection Leak Simulation (from stress_test_service.py)
    # ==========================================================================
    default_leak_hold_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Connection hold time during connection leak simulation (seconds)",
    )

    # ==========================================================================
    # Sleep Intervals
    # ==========================================================================
    inter_request_sleep_ms: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Inter-request wait time during burst test (ms)",
    )

    # ==========================================================================
    # Advisory Lock Defaults (from stress_views.py)
    # ==========================================================================
    default_lock_id: int = Field(
        default=12345,
        ge=1,
        le=1000000,
        description="Default advisory lock ID",
    )
    default_lock_hold_seconds: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Default advisory lock hold time (seconds)",
    )
    max_lock_hold_seconds: int = Field(
        default=60,
        ge=1,
        le=300,
        description="Maximum advisory lock hold time (seconds)",
    )
    default_lock_hold_ms: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Lock hold time per lock during lock contention (ms)",
    )
    default_contention_duration_seconds: int = Field(
        default=5,
        ge=1,
        le=30,
        description="Default lock contention duration (seconds)",
    )
    contention_lock_id: int = Field(
        default=99999,
        ge=1,
        le=1000000,
        description="Default lock contention lock ID",
    )
    burst_lock_id: int = Field(
        default=777,
        ge=1,
        le=1000000,
        description="Default burst failure lock ID",
    )
    default_burst_duration_seconds: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Default burst failure duration (seconds)",
    )
    default_concurrent_locks: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Default concurrent lock attempts for burst failure",
    )

    # ==========================================================================
    # Pool Exhaustion Settings (from stress_views.py)
    # ==========================================================================
    default_connections_to_hold: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Default connections to hold during pool exhaustion",
    )
    default_pool_hold_seconds: int = Field(
        default=30,
        ge=1,
        le=60,
        description="Connection hold time during pool exhaustion (seconds)",
    )

    # ==========================================================================
    # Heavy Query Settings (368: Django Settings Decoupling)
    # ==========================================================================
    table: str = Field(
        default="baldur_failedoperation",
        description="Target table for heavy query stress test",
    )

    @field_validator("max_burst_duration_seconds")
    @classmethod
    def validate_burst_duration(cls, v: int) -> int:
        """Burst duration이 너무 길면 경고."""
        if v > 60:
            logger.warning(
                "stress_test_settings.high_consider_using_safety",
                setting_value=v,
            )
        return v


def get_stress_test_settings() -> "StressTestSettings":
    from baldur.settings.root import get_config

    return get_config().testing.stress_test


def reset_stress_test_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().testing.__dict__["stress_test"]
    except KeyError:
        pass
