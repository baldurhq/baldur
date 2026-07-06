"""
Namespace Settings - Multi-Cluster Support.

환경변수로 클러스터/리전/테넌트 등의 네임스페이스를 설정합니다.

Usage:
    # 방법 1: 통합 네임스페이스
    BALDUR_NAMESPACE_NAMESPACE=seoul

    # 방법 2: 개별 설정 (우선순위: NAMESPACE > REGION > TENANT > ENV)
    BALDUR_NAMESPACE_REGION=seoul
    BALDUR_NAMESPACE_TENANT=customer123
    BALDUR_NAMESPACE_ENV=production

동적 Namespace (X-Test-Mode 지원):
    - 운영 요청: baldur:*
    - 합성 요청: xtest:baldur:* (TestModeContext 활성화 시)
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.core.test_mode_context import TestModeContext
from baldur.settings.base import make_settings_config


class NamespaceSettings(BaseSettings):
    """
    네임스페이스 설정.

    다중 클러스터/리전/테넌트 환경에서 Redis 키를 분리합니다.
    """

    model_config = make_settings_config("BALDUR_NAMESPACE_")

    # 통합 네임스페이스 (최우선)
    namespace: str | None = Field(
        default=None,
        description="Unified namespace (highest priority)",
    )

    # 개별 설정 (우선순위 순)
    region: str | None = Field(
        default=None,
        description="Region identifier (e.g., seoul, tokyo)",
    )
    tenant: str | None = Field(
        default=None,
        description="Tenant identifier for SaaS multi-tenancy",
    )
    env: str | None = Field(
        default=None,
        description="Environment (dev, staging, production)",
    )

    # 기본값 (아무것도 설정 안 된 경우)
    default_namespace: str = Field(
        default="default",
        description="Fallback namespace when nothing is set",
    )

    # 네임스페이스 활성화 여부
    namespace_enabled: bool = Field(
        default=False,
        description="Enable namespace-based key prefixing",
    )

    def get_effective_namespace(self) -> str:
        """
        유효 네임스페이스 반환.

        우선순위: namespace > region > tenant > env > default

        Returns:
            유효한 네임스페이스 문자열. 비활성화 시 빈 문자열.
        """
        if not self.namespace_enabled:
            return ""  # 비활성화 시 빈 문자열 (기존 동작 유지)

        return (
            self.namespace
            or self.region
            or self.tenant
            or self.env
            or self.default_namespace
        )

    def get_key_prefix(self, base_prefix: str = "baldur") -> str:
        """
        Redis 키 프리픽스 생성.

        Args:
            base_prefix: 기본 프리픽스

        Returns:
            완전한 키 프리픽스 (예: "baldur:seoul:" 또는 "baldur:")
        """
        ns = self.get_effective_namespace()
        if ns:
            return f"{base_prefix}:{ns}:"
        return f"{base_prefix}:"


# =============================================================================
# Synthetic Mode Key Prefix (X-Test-Mode 지원)
# =============================================================================

SYNTHETIC_KEY_PREFIX = "xtest"


def get_effective_key_prefix(base_prefix: str = "baldur") -> str:
    """
    현재 컨텍스트 기반 동적 키 프리픽스 반환.

    TestModeContext가 활성화된 경우 xtest: 프리픽스가 자동 추가되어
    운영 데이터와 테스트 데이터가 분리됩니다.

    Args:
        base_prefix: 기본 프리픽스

    Returns:
        동적 키 프리픽스:
        - 운영 모드: "baldur:*" 또는 "baldur:seoul:*"
        - 합성 모드: "xtest:baldur:*" 또는 "xtest:baldur:seoul:*"

    Example:
        # 운영 요청
        prefix = get_effective_key_prefix()  # "baldur:"

        # X-Test-Mode 요청
        with TestModeContext.start():
            prefix = get_effective_key_prefix()  # "xtest:baldur:"
    """
    settings = get_namespace_settings()
    standard_prefix = settings.get_key_prefix(base_prefix)

    if TestModeContext.is_synthetic():
        return f"{SYNTHETIC_KEY_PREFIX}:{standard_prefix}"

    return standard_prefix


def get_namespace_settings() -> "NamespaceSettings":
    """NamespaceSettings 싱글톤 반환."""
    from baldur.settings.root import get_config

    return get_config().multi_region.namespace


def reset_namespace_settings() -> None:
    """테스트용 싱글톤 리셋."""
    from baldur.settings.root import get_config

    try:
        del get_config().multi_region.__dict__["namespace"]
    except KeyError:
        pass


def get_key_prefix(base_prefix: str = "baldur") -> str:
    """
    현재 네임스페이스 기반 키 프리픽스 반환.

    편의 함수로, 어디서든 호출 가능.

    Args:
        base_prefix: 기본 프리픽스

    Returns:
        완전한 키 프리픽스
    """
    return get_namespace_settings().get_key_prefix(base_prefix)
