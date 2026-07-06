"""
ApplyStrategy Settings - Pydantic v2.

설정 적용 전략별 delay 및 grace_timeout 설정.
config 타입별 기본 지연 시간을 환경변수로 설정 가능.

Environment Variables:
    # 설정 타입별 지연 시간
    BALDUR_APPLY_STRATEGY_SLA_DELAY=0
    BALDUR_APPLY_STRATEGY_METRICS_DELAY=0
    BALDUR_APPLY_STRATEGY_NOTIFICATION_DELAY=0
    BALDUR_APPLY_STRATEGY_FORENSIC_DELAY=0
    BALDUR_APPLY_STRATEGY_RATE_LIMIT_DELAY=0
    BALDUR_APPLY_STRATEGY_RETRY_DELAY=10
    BALDUR_APPLY_STRATEGY_DLQ_DELAY=10
    BALDUR_APPLY_STRATEGY_CIRCUIT_BREAKER_DELAY=30
    BALDUR_APPLY_STRATEGY_IDEMPOTENCY_DELAY=30
    BALDUR_APPLY_STRATEGY_SECURITY_DELAY=60
    BALDUR_APPLY_STRATEGY_ERROR_BUDGET_DELAY=30
    BALDUR_APPLY_STRATEGY_DEFAULT_GRACE_TIMEOUT=60

    # Celery Task 재시도 설정
    BALDUR_APPLY_STRATEGY_PENDING_MAX_RETRIES=3
    BALDUR_APPLY_STRATEGY_PENDING_RETRY_DELAY=10
    BALDUR_APPLY_STRATEGY_GRACEFUL_MAX_RETRIES=10
    BALDUR_APPLY_STRATEGY_GRACEFUL_RETRY_DELAY=5
    BALDUR_APPLY_STRATEGY_CLEANUP_MAX_AGE_HOURS=24
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ApplyStrategySettings(BaseSettings):
    """
    ApplyStrategy 설정.

    설정 변경 적용 시 타입별 지연 시간 및 grace timeout 설정.
    """

    model_config = make_settings_config("BALDUR_APPLY_STRATEGY_")

    # ==========================================================================
    # 즉시 적용 (Safe Immediate) - delay_seconds
    # ==========================================================================
    sla_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="SLA config apply delay (seconds)",
    )
    metrics_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Metrics config apply delay (seconds)",
    )
    notification_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Notification config apply delay (seconds)",
    )
    forensic_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Forensic config apply delay (seconds)",
    )

    # ==========================================================================
    # 트래픽 제어 - 즉시지만 주의 필요
    # ==========================================================================
    rate_limit_delay: int = Field(
        default=0,
        ge=0,
        le=300,
        description="Rate limit config apply delay (seconds)",
    )

    # ==========================================================================
    # 처리 관련 - 지연 적용
    # ==========================================================================
    retry_delay: int = Field(
        default=10,
        ge=0,
        le=300,
        description="Retry config apply delay (seconds)",
    )
    dlq_delay: int = Field(
        default=10,
        ge=0,
        le=300,
        description="DLQ config apply delay (seconds)",
    )

    # ==========================================================================
    # 핵심 보호 - 긴 지연
    # ==========================================================================
    circuit_breaker_delay: int = Field(
        default=30,
        ge=0,
        le=600,
        description="Circuit breaker config apply delay (seconds)",
    )
    idempotency_delay: int = Field(
        default=30,
        ge=0,
        le=600,
        description="Idempotency config apply delay (seconds)",
    )
    security_delay: int = Field(
        default=60,
        ge=0,
        le=600,
        description="Security config apply delay (seconds)",
    )
    error_budget_delay: int = Field(
        default=30,
        ge=0,
        le=600,
        description="Error budget config apply delay (seconds)",
    )

    # ==========================================================================
    # 공통 설정
    # ==========================================================================
    default_grace_timeout: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Default maximum wait time for GRACEFUL strategy (seconds)",
    )

    # ==========================================================================
    # Celery Task 재시도 설정 (apply_pending_config_changes)
    # ==========================================================================
    pending_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retries for pending config apply task",
    )
    pending_retry_delay: int = Field(
        default=10,
        ge=1,
        le=300,
        description="Retry delay for pending config apply task (seconds)",
    )

    # ==========================================================================
    # Celery Task 재시도 설정 (apply_graceful_config_change)
    # 진행 중인 작업 완료 대기가 필요하므로 재시도 횟수가 많음
    # ==========================================================================
    graceful_max_retries: int = Field(
        default=10,
        ge=0,
        le=20,
        description="Maximum retries for graceful config apply task",
    )
    graceful_retry_delay: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Retry delay for graceful config apply task (seconds)",
    )

    # ==========================================================================
    # 만료 설정 정리 (cleanup_expired_config_changes)
    # ==========================================================================
    cleanup_max_age_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Maximum age for expired config change cleanup (hours)",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_apply_strategy_settings() -> "ApplyStrategySettings":
    """
    캐시된 ApplyStrategySettings 인스턴스 반환.

    Returns:
        ApplyStrategySettings: 싱글톤 인스턴스
    """
    from baldur.settings.root import get_config

    return get_config().services_group.apply_strategy


def reset_apply_strategy_settings() -> None:
    """
    캐시된 설정 초기화 (테스트용).
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["apply_strategy"]
    except KeyError:
        pass
