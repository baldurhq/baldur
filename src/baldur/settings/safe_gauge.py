"""
SafeGauge Settings - Pydantic v2.

SafeGauge 레이블 조합 관리 설정입니다.
Prometheus 메트릭 카디널리티 폭발을 방지합니다.

Replaces:
- metrics/safe_gauge/core.py:DEFAULT_MAX_LABEL_COMBINATIONS

Environment Variables:
    BALDUR_SAFE_GAUGE_MAX_LABEL_COMBINATIONS=1000

Usage:
    from baldur.settings.safe_gauge import get_safe_gauge_settings
    settings = get_safe_gauge_settings()
    max_combinations = settings.max_label_combinations
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class SafeGaugeSettings(BaseSettings):
    """
    SafeGauge 설정.

    LRU 캐시 기반의 레이블 조합 관리 설정입니다.
    카디널리티 폭발(Cardinality Explosion)을 방지합니다.

    환경별 권장값:
    - 단일 서버: 1000 (기본값)
    - K8s 10 Pods: 500
    - K8s 100+ Pods: 200

    Attributes:
        max_label_combinations: 캐시할 최대 레이블 조합 수
        eviction_warning_threshold: Eviction 경고 임계치 (%)
    """

    model_config = make_settings_config("BALDUR_SAFE_GAUGE_")

    # ==========================================================================
    # Label Combinations - from metrics/safe_gauge/core.py
    # ==========================================================================
    max_label_combinations: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Maximum label combinations to cache. Evicts oldest via LRU when exceeded.",
    )

    # ==========================================================================
    # Eviction Monitoring
    # ==========================================================================
    eviction_warning_threshold: float = Field(
        default=0.8,
        ge=0.5,
        le=1.0,
        description="Eviction warning threshold. Logs warning at 80% capacity.",
    )

    @field_validator("max_label_combinations")
    @classmethod
    def validate_max_label_combinations(cls, v: int) -> int:
        """레이블 조합 수가 적절한지 경고."""
        if v > 5000:
            logger.warning(
                "safe_gauge.max_label_combinations_high",
                setting_value=v,
            )
        return v


def get_safe_gauge_settings() -> "SafeGaugeSettings":
    from baldur.settings.root import get_config

    return get_config().metrics_group.safe_gauge


def reset_safe_gauge_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().metrics_group.__dict__["safe_gauge"]
    except KeyError:
        pass
