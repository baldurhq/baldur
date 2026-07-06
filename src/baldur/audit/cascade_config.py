"""
Cascade Audit 설정.

Cascade Event 처리와 관련된 설정값들을 정의합니다.

Settings:
- CascadeChainConfig: 체인 깊이 제한 설정
- CascadeRetentionConfig: 데이터 보관 정책 (Phase 4에서 구현)
- AuditBackpressureConfig: 배압 설정 (Phase 5에서 구현)

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
    docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 3 [16])
"""

from __future__ import annotations

from dataclasses import dataclass

from baldur.settings import CascadeRetentionSettings, get_layered_settings

# =============================================================================
# CascadeChainConfig
# =============================================================================


@dataclass
class CascadeChainConfig:
    """
    Cascade 체인 깊이 설정.

    자동화 시스템 간의 과도한 연쇄 반응을 방지하기 위해
    체인 깊이를 제한합니다.

    Attributes:
        max_chain_depth: 최대 체인 깊이 (초과 시 경고/차단)
        warn_at_depth: 경고를 발생시킬 깊이
        block_on_exceed: 깊이 초과 시 차단 여부
        detect_cycles: 순환 참조 감지 활성화

    Code reference:
        services/error_budget/propagation.py#L78-84 (max_hops 패턴)
    """

    max_chain_depth: int = 10
    """
    최대 체인 깊이.

    이 값을 초과하면 경고 발생 또는 차단.
    기본값 10은 대부분의 정상 케이스를 커버합니다.
    """

    warn_at_depth: int = 7
    """
    경고를 발생시킬 깊이.

    이 깊이에 도달하면 로그 경고를 발생시킵니다.
    """

    block_on_exceed: bool = True
    """
    깊이 초과 시 차단 여부.

    True: CascadeChainDepthExceeded 예외 발생
    False: 경고만 발생하고 계속 진행
    """

    detect_cycles: bool = True
    """
    순환 참조 감지 활성화.

    True: 순환 참조 감지 시 CascadeCycleDetected 예외 발생
    False: 순환 참조 감지 비활성화
    """

    def __post_init__(self) -> None:
        """설정값 검증."""
        if self.warn_at_depth >= self.max_chain_depth:
            # warn_at_depth는 max_chain_depth보다 작아야 함
            self.warn_at_depth = max(1, self.max_chain_depth - 3)


# =============================================================================
# Default Configurations
# =============================================================================


DEFAULT_CASCADE_CHAIN_CONFIG = CascadeChainConfig()
"""기본 Cascade 체인 설정."""


def get_cascade_chain_config() -> CascadeChainConfig:
    """
    Cascade 체인 설정 반환.

    CascadeSettings (Pydantic) 경유로 설정을 로드합니다.
    """
    try:
        from baldur.settings.cascade import get_cascade_settings

        s = get_cascade_settings()
        return CascadeChainConfig(
            max_chain_depth=s.max_depth,
            warn_at_depth=s.warn_depth,
            block_on_exceed=s.block_on_exceed,
            detect_cycles=s.detect_cycles,
        )
    except Exception:
        return DEFAULT_CASCADE_CHAIN_CONFIG


# =============================================================================
# CascadeRetentionConfig (Phase 4)
# =============================================================================


@dataclass
class CascadeRetentionConfig:
    """
    Cascade 데이터 보관 정책.

    Hot/Warm/Cold 계층별 보관 기간을 정의합니다.

    Tiered Storage:
    - Hot (Redis): 실시간 조회용, 짧은 보관
    - Warm (PostgreSQL): 복잡한 쿼리, 중간 보관
    - Cold (Archive): 법적 요구사항, 장기 보관

    Attributes:
        hot_retention_days: Redis 내 보관 기간
        hot_max_count: Redis 내 최대 개수
        warm_retention_days: PostgreSQL 내 보관 기간
        cold_retention_days: 아카이브 보관 기간
        index_retention_days: 인덱스 키 보관 기간
        anchor_retention_days: 체크포인트 보관 기간

    Code reference:
        tasks/cleanup_tasks.py (archive_old_dlq_entries 패턴)
        audit/integrity/anchor.py#L46 (DEFAULT_RETENTION_DAYS)
    """

    # Hot 데이터 (Redis)
    hot_retention_days: int = 7
    """Redis 내 보관 기간 (빠른 조회용)."""

    hot_max_count: int = 10000
    """Redis 내 최대 개수 (메모리 제한)."""

    # Warm 데이터 (PostgreSQL)
    warm_retention_days: int = 90
    """PostgreSQL 내 보관 기간 (Audit 대응용)."""

    # Cold 데이터 (Archive)
    cold_retention_days: int = 365
    """아카이브 보관 기간 (법적 요구사항)."""

    # Index 보관
    index_retention_days: int = 30
    """인덱스 키 보관 기간."""

    # Hash Chain Anchor
    anchor_retention_days: int = 90
    """체크포인트 보관 기간 (anchor.py 패턴)."""


DEFAULT_CASCADE_RETENTION_CONFIG = CascadeRetentionConfig()
"""기본 Cascade 보관 정책."""


def get_cascade_retention_config() -> CascadeRetentionConfig:
    """
    Cascade 보관 정책 반환.

    LayeredSettings를 통해 4계층 설정을 병합하여 반환합니다.

    92_CONFIG_IMPLEMENTATION_GUIDE.md Week 3 [16] CascadeRetentionSettings 참조.
    """
    settings = get_layered_settings(CascadeRetentionSettings, "cascade_retention")

    return CascadeRetentionConfig(
        hot_retention_days=settings.hot_retention_days,
        hot_max_count=settings.hot_max_count,
        warm_retention_days=settings.warm_retention_days,
        cold_retention_days=settings.cold_retention_days,
        index_retention_days=settings.index_retention_days,
        anchor_retention_days=settings.anchor_retention_days,
    )


# =============================================================================
# AuditBackpressureConfig (Phase 5)
# =============================================================================


@dataclass
class AuditBackpressureConfig:
    """
    Audit Backpressure 설정.

    고부하 상황에서 Audit 시스템이 시스템 전체 장애로 번지지 않도록
    Load Shedding을 적용합니다.

    Attributes:
        load_shedding_enabled: Load Shedding 활성화 여부
        buffer_warning_threshold: 버퍼 경고 임계치 (기본값 0.7 = 70%)
        buffer_critical_threshold: 버퍼 임계치 (기본값 0.9 = 90%)
        max_events_per_second: 초당 최대 이벤트 처리량
        fallback_enabled: 로컬 폴백 활성화 여부
        metrics_enabled: 메트릭 기록 활성화 여부

    Code reference:
        test_lazy_import.py#L105-117 (get_load_shedding_manager 패턴)
    """

    load_shedding_enabled: bool = True
    """Load Shedding 활성화 여부."""

    buffer_warning_threshold: float = 0.7
    """
    버퍼 경고 임계치 (0.0 ~ 1.0).

    이 비율을 초과하면 LOW 우선순위 이벤트 드롭 시작.
    """

    buffer_critical_threshold: float = 0.9
    """
    버퍼 임계치 (0.0 ~ 1.0).

    이 비율을 초과하면 MEDIUM 우선순위 이벤트도 드롭.
    """

    max_events_per_second: int = 1000
    """
    초당 최대 이벤트 처리량.

    이를 초과하면 Load Shedding 적용.
    """

    fallback_enabled: bool = True
    """로컬 폴백 활성화 여부."""

    metrics_enabled: bool = True
    """메트릭 기록 활성화 여부."""

    def __post_init__(self) -> None:
        """설정값 검증."""
        if not 0.0 <= self.buffer_warning_threshold <= 1.0:
            self.buffer_warning_threshold = 0.7
        if not 0.0 <= self.buffer_critical_threshold <= 1.0:
            self.buffer_critical_threshold = 0.9
        if self.buffer_warning_threshold >= self.buffer_critical_threshold:
            self.buffer_warning_threshold = self.buffer_critical_threshold - 0.2


DEFAULT_BACKPRESSURE_CONFIG = AuditBackpressureConfig()
"""기본 Backpressure 설정."""


def get_audit_backpressure_config() -> AuditBackpressureConfig:
    """
    Audit Backpressure 설정 반환.

    AuditSettings (Pydantic) 경유로 설정을 로드합니다.
    """
    try:
        from baldur.settings.audit import get_audit_settings

        s = get_audit_settings()
        return AuditBackpressureConfig(
            load_shedding_enabled=s.load_shedding_enabled,
            buffer_warning_threshold=s.buffer_warning_threshold,
            buffer_critical_threshold=s.buffer_critical_threshold,
            max_events_per_second=s.max_events_per_second,
            fallback_enabled=s.fallback_enabled,
            metrics_enabled=s.metrics_enabled,
        )
    except Exception:
        return DEFAULT_BACKPRESSURE_CONFIG
