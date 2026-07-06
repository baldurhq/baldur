"""
Deployment Adapter Base Interface and Data Models.

외부 배포 시스템과 연동하기 위한 추상 인터페이스와 데이터 모델을 정의합니다.

Data Models:
- DeploymentEvent: 배포 이벤트 정보
- DeploymentConfigChange: 설정 변경 이벤트 정보

Interfaces:
- ExternalDeploymentAdapter: 외부 배포 시스템 어댑터 프로토콜
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from baldur.core.serializable import SerializableMixin


class DeploymentType(str, Enum):
    """배포 방식 유형."""

    ROLLING = "rolling"
    """롤링 업데이트 배포."""

    CANARY = "canary"
    """카나리 배포."""

    BLUE_GREEN = "blue-green"
    """블루-그린 배포."""

    RECREATE = "recreate"
    """재생성 배포."""

    UNKNOWN = "unknown"
    """알 수 없는 배포 방식."""


class DeploymentSource(str, Enum):
    """배포 정보 소스."""

    KUBERNETES = "kubernetes"
    """Kubernetes API에서 수집."""

    ARGOCD = "argocd"
    """ArgoCD에서 수집."""

    HELM = "helm"
    """Helm Release에서 수집."""

    MANUAL = "manual"
    """수동 입력."""

    MOCK = "mock"
    """테스트용 Mock 데이터."""


@dataclass
class DeploymentEvent(SerializableMixin):
    """
    배포 이벤트 정보.

    인시던트 발생 전후의 배포 이력을 추적하기 위한 데이터 모델입니다.

    Attributes:
        deployment_id: 배포 고유 ID
        service_name: 대상 서비스 이름
        version_from: 이전 버전
        version_to: 새 버전
        deployed_at: 배포 시각 (ISO 8601)
        deployed_by: 배포자 (사용자 또는 시스템)
        deployment_type: 배포 방식
        source: 배포 정보 소스
        namespace: 네임스페이스 (Kubernetes)
        is_rollback: 롤백 배포 여부
        metadata: 추가 메타데이터
    """

    deployment_id: str
    """배포 고유 ID."""

    service_name: str
    """대상 서비스 이름."""

    version_from: str
    """이전 버전."""

    version_to: str
    """새 버전."""

    deployed_at: str
    """배포 시각 (ISO 8601 형식)."""

    deployed_by: str = "system"
    """배포자 (사용자 또는 시스템)."""

    deployment_type: DeploymentType = DeploymentType.ROLLING
    """배포 방식."""

    source: DeploymentSource = DeploymentSource.KUBERNETES
    """배포 정보 소스."""

    namespace: str = "default"
    """네임스페이스."""

    is_rollback: bool = False
    """롤백 배포 여부."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """추가 메타데이터."""

    def to_timeline_event(self) -> dict[str, Any]:
        """타임라인 이벤트 형식으로 변환."""
        event_type = "ROLLBACK" if self.is_rollback else "DEPLOY"
        return {
            "timestamp": self.deployed_at,
            "event_type": f"[{event_type}] {self.version_from} → {self.version_to}",
            "details": {
                "deployment_id": self.deployment_id,
                "service_name": self.service_name,
                "deployed_by": self.deployed_by,
                "deployment_type": self.deployment_type.value,
                "source": self.source.value,
            },
        }


@dataclass
class DeploymentConfigChange(SerializableMixin):
    """
    설정 변경 이벤트 정보.

    인시던트 발생 전후의 설정 변경 이력을 추적하기 위한 데이터 모델입니다.

    Attributes:
        change_id: 변경 고유 ID
        config_key: 변경된 설정 키
        old_value: 이전 값 (민감 정보 마스킹됨)
        new_value: 새 값 (민감 정보 마스킹됨)
        changed_at: 변경 시각 (ISO 8601)
        changed_by: 변경자
        service_name: 대상 서비스 이름
        namespace: 네임스페이스
    """

    change_id: str
    """변경 고유 ID."""

    config_key: str
    """변경된 설정 키."""

    old_value: str
    """이전 값 (민감 정보 마스킹됨)."""

    new_value: str
    """새 값 (민감 정보 마스킹됨)."""

    changed_at: str
    """변경 시각 (ISO 8601 형식)."""

    changed_by: str = "system"
    """변경자."""

    service_name: str = ""
    """대상 서비스 이름."""

    namespace: str = "default"
    """네임스페이스."""

    def to_timeline_event(self) -> dict[str, Any]:
        """타임라인 이벤트 형식으로 변환."""
        return {
            "timestamp": self.changed_at,
            "event_type": f"[CONFIG] {self.config_key}: {self.old_value} → {self.new_value}",
            "details": {
                "change_id": self.change_id,
                "service_name": self.service_name,
                "changed_by": self.changed_by,
            },
        }


@runtime_checkable
class ExternalDeploymentAdapter(Protocol):
    """
    외부 배포 시스템 어댑터 인터페이스.

    Kubernetes, ArgoCD, Helm 등 외부 배포 시스템과 연동하여
    배포 이력을 수집하는 어댑터의 프로토콜을 정의합니다.

    구현체:
    - MockDeploymentAdapter: 테스트용 정적 데이터 반환
    - KubernetesDeploymentAdapter: Kubernetes API 연동

    Example:
        >>> adapter = MockDeploymentAdapter()
        >>> deployments = adapter.get_deployments_in_range(
        ...     service_name="payment-service",
        ...     start_time=datetime(2025, 1, 1, 10, 0),
        ...     end_time=datetime(2025, 1, 1, 12, 0)
        ... )
        >>> for deploy in deployments:
        ...     print(f"{deploy.version_from} -> {deploy.version_to}")
    """

    def get_deployments_in_range(
        self,
        service_name: str,
        start_time: datetime,
        end_time: datetime,
        namespace: str = "default",
    ) -> list[DeploymentEvent]:
        """
        지정된 시간 범위 내의 배포 이력을 조회합니다.

        Args:
            service_name: 서비스 이름
            start_time: 조회 시작 시각
            end_time: 조회 종료 시각
            namespace: 네임스페이스

        Returns:
            배포 이벤트 목록 (시간순 정렬)
        """
        ...

    def get_deployment_by_version(
        self,
        service_name: str,
        version: str,
        namespace: str = "default",
    ) -> DeploymentEvent | None:
        """
        특정 버전의 배포 상세 정보를 조회합니다.

        Args:
            service_name: 서비스 이름
            version: 배포 버전
            namespace: 네임스페이스

        Returns:
            배포 이벤트 또는 None
        """
        ...

    def get_current_version(
        self,
        service_name: str,
        namespace: str = "default",
    ) -> str | None:
        """
        서비스의 현재 배포 버전을 조회합니다.

        Args:
            service_name: 서비스 이름
            namespace: 네임스페이스

        Returns:
            현재 버전 문자열 또는 None
        """
        ...

    def get_rollback_history(
        self,
        service_name: str,
        namespace: str = "default",
        limit: int = 10,
    ) -> list[DeploymentEvent]:
        """
        롤백 이력을 조회합니다.

        Args:
            service_name: 서비스 이름
            namespace: 네임스페이스
            limit: 최대 조회 개수

        Returns:
            롤백 이벤트 목록 (최신순 정렬)
        """
        ...

    def get_config_changes_in_range(
        self,
        service_name: str,
        start_time: datetime,
        end_time: datetime,
        namespace: str = "default",
    ) -> list[DeploymentConfigChange]:
        """
        지정된 시간 범위 내의 설정 변경 이력을 조회합니다.

        Args:
            service_name: 서비스 이름
            start_time: 조회 시작 시각
            end_time: 조회 종료 시각
            namespace: 네임스페이스

        Returns:
            설정 변경 이벤트 목록 (시간순 정렬)
        """
        ...

    def is_available(self) -> bool:
        """
        어댑터가 사용 가능한지 확인합니다.

        Returns:
            사용 가능 여부
        """
        ...
