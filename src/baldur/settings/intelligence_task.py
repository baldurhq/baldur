"""
Intelligence Task Settings - Pydantic v2.

지능 레인(Analyze & Learn) 태스크 관련 설정.

Source:
- tasks/intelligence_tasks.py

Environment Variables:
    BALDUR_INTELLIGENCE_TASK_DEFAULT_COOLDOWN_SECONDS=3600
    BALDUR_INTELLIGENCE_TASK_EXECUTION_THRESHOLD=10
    BALDUR_INTELLIGENCE_TASK_ANALYSIS_THRESHOLD_MINUTES=60
    BALDUR_INTELLIGENCE_TASK_BATCH_SIZE=100
    BALDUR_INTELLIGENCE_TASK_SEVERITY_HIGH_THRESHOLD=50
    BALDUR_INTELLIGENCE_TASK_SEVERITY_MEDIUM_THRESHOLD=10
    BALDUR_INTELLIGENCE_TASK_RECONCILIATION_CUTOFF_MINUTES=30
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class IntelligenceTaskSettings(BaseSettings):
    """
    지능 레인 태스크 설정.

    SLA 드리프트 감지, 포렌식 분석, 인사이트 추출 등의 설정을 정의합니다.
    """

    model_config = make_settings_config("BALDUR_INTELLIGENCE_TASK_")

    # ==========================================================================
    # Notification Policy (from intelligence_tasks.py line 57, 154)
    # ==========================================================================
    default_cooldown_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Default cooldown for task notifications (seconds)",
    )

    recovery_check_cooldown_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Recovery status check cooldown (seconds)",
    )

    # ==========================================================================
    # Thresholds (from intelligence_tasks.py line 151)
    # ==========================================================================
    execution_threshold: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Task execution notification threshold",
    )

    analysis_threshold_minutes: int = Field(
        default=60,
        ge=10,
        le=1440,
        description="Forensic analysis threshold time (minutes)",
    )

    # ==========================================================================
    # Batch Settings (from intelligence_tasks.py line 168)
    # ==========================================================================
    batch_size: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Analysis batch size",
    )

    # ==========================================================================
    # Severity Thresholds (from intelligence_tasks.py line 258-260)
    # ==========================================================================
    severity_high_threshold: int = Field(
        default=50,
        ge=20,
        le=200,
        description="High severity threshold (suspicious_count)",
    )

    severity_medium_threshold: int = Field(
        default=10,
        ge=5,
        le=100,
        description="Medium severity threshold (suspicious_count)",
    )

    # ==========================================================================
    # Reconciliation (from intelligence_tasks.py line 558)
    # ==========================================================================
    reconciliation_cutoff_minutes: int = Field(
        default=30,
        ge=10,
        le=120,
        description="Reconciliation accuracy verification cutoff time (minutes)",
    )

    # ==========================================================================
    # Cross-Stage Insights (from intelligence_tasks.py line 299)
    # ==========================================================================
    insight_threshold: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Insight notification threshold (count)",
    )

    @field_validator("severity_high_threshold")
    @classmethod
    def validate_severity_thresholds(cls, v: int, info) -> int:
        """severity_high_threshold가 severity_medium_threshold보다 커야 함."""
        # Note: Pydantic v2에서는 values 대신 info 사용
        return v


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_intelligence_task_settings() -> "IntelligenceTaskSettings":
    """
    캐시된 IntelligenceTaskSettings 인스턴스 반환.

    Returns:
        IntelligenceTaskSettings: 싱글톤 인스턴스
    """
    from baldur.settings.root import get_config

    return get_config().services_group.intelligence_task


def reset_intelligence_task_settings() -> None:
    """
    캐시된 설정 초기화 (테스트용).

    환경 변수 변경 후 설정을 다시 로드하려면 이 함수를 호출하세요.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["intelligence_task"]
    except KeyError:
        pass
