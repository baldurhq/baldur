"""
X-Test Resource Guard Settings - Pydantic v2.

X-Test 요청 시 시스템 CPU/메모리 과부하 상태를 체크하여
운영 시스템에 추가 부담을 방지하기 위한 설정.

Environment Variables:
    BALDUR_RESOURCE_GUARD_CPU_THRESHOLD=80           # CPU 임계값 (%)
    BALDUR_RESOURCE_GUARD_MEMORY_THRESHOLD=85        # 메모리 임계값 (%)
    BALDUR_RESOURCE_GUARD_RESOURCE_CHECK_ENABLED=true  # 리소스 체크 활성화 여부
    BALDUR_RESOURCE_GUARD_RETRY_AFTER_SECONDS=30     # 429 응답 시 권장 대기 시간
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ResourceGuardSettings(BaseSettings):
    """
    X-Test 리소스 가드 설정.

    시스템 CPU/메모리가 과부하 상태일 때 X-Test 요청을 차단하여
    운영 시스템 안정성을 보호합니다.

    RecoveryGate의 cpu_threshold_percent (80%)와 일관성 유지.
    """

    model_config = make_settings_config("BALDUR_RESOURCE_GUARD_")

    # ==========================================================================
    # CPU 임계값
    # ==========================================================================
    cpu_threshold: float = Field(
        default=80.0,
        ge=50.0,
        le=99.0,
        description="CPU usage threshold (%). Blocks X-Test when exceeded. Aligned with RecoveryGate at 80%.",
    )

    # ==========================================================================
    # 메모리 임계값
    # ==========================================================================
    memory_threshold: float = Field(
        default=85.0,
        ge=50.0,
        le=99.0,
        description="Memory usage threshold (%). Blocks X-Test when exceeded.",
    )

    # ==========================================================================
    # 리소스 체크 활성화 여부
    # ==========================================================================
    resource_check_enabled: bool = Field(
        default=True,
        description="Enable resource check. Skips check when false.",
    )

    # ==========================================================================
    # 429 응답 시 권장 대기 시간
    # ==========================================================================
    retry_after_seconds: int = Field(
        default=30,
        ge=10,
        le=300,
        description="Retry-After header value (seconds) for 429 responses.",
    )


def get_resource_guard_settings() -> "ResourceGuardSettings":
    from baldur.settings.root import get_config

    return get_config().meta.resource_guard


def reset_resource_guard_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["resource_guard"]
    except KeyError:
        pass
