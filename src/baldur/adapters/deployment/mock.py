"""
Mock Deployment Adapter.

테스트 및 개발 환경용 Mock 어댑터입니다.
정적 데이터를 반환하여 Kubernetes 없이도 DeploymentCorrelator를 테스트할 수 있습니다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import structlog

from baldur.utils.time import utc_now

from .base import (
    DeploymentConfigChange,
    DeploymentEvent,
    DeploymentSource,
    DeploymentType,
)

logger = structlog.get_logger()


class MockDeploymentAdapter:
    """
    테스트용 Mock 배포 어댑터.

    정적 데이터를 반환하여 실제 Kubernetes 연동 없이
    DeploymentCorrelator를 테스트할 수 있습니다.

    설정:
        DEPLOYMENT_ADAPTER=mock

    Example:
        >>> adapter = MockDeploymentAdapter()
        >>> deployments = adapter.get_deployments_in_range(
        ...     service_name="payment-service",
        ...     start_time=datetime(2025, 1, 1, 10, 0),
        ...     end_time=datetime(2025, 1, 1, 12, 0)
        ... )
    """

    def __init__(
        self,
        mock_deployments: list[DeploymentEvent] | None = None,
        mock_config_changes: list[DeploymentConfigChange] | None = None,
    ):
        """
        Mock 어댑터 초기화.

        Args:
            mock_deployments: 테스트용 배포 이벤트 목록 (None이면 기본 데이터 생성)
            mock_config_changes: 테스트용 설정 변경 이벤트 목록
        """
        self._mock_deployments = mock_deployments or []
        self._mock_config_changes = mock_config_changes or []
        self._is_available = True
        logger.debug("mock_deployment_adapter.initialized_mock_data")

    def set_mock_deployments(self, deployments: list[DeploymentEvent]) -> None:
        """테스트용 배포 데이터 설정."""
        self._mock_deployments = deployments

    def set_mock_config_changes(self, changes: list[DeploymentConfigChange]) -> None:
        """테스트용 설정 변경 데이터 설정."""
        self._mock_config_changes = changes

    def set_availability(self, available: bool) -> None:
        """어댑터 가용성 설정 (Fallback 테스트용)."""
        self._is_available = available

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
        if not self._is_available:
            logger.warning("mock_deployment_adapter.adapter_available")
            return []

        result = []
        for deploy in self._mock_deployments:
            # 서비스 필터
            if deploy.service_name != service_name:
                continue
            # 네임스페이스 필터
            if deploy.namespace != namespace:
                continue
            # 시간 범위 필터
            try:
                deploy_time = datetime.fromisoformat(
                    deploy.deployed_at.replace("Z", "+00:00")
                )
                if start_time <= deploy_time <= end_time:
                    result.append(deploy)
            except (ValueError, TypeError):
                continue

        # 시간순 정렬
        result.sort(key=lambda d: d.deployed_at)

        logger.debug(
            "mock_deployment_adapter.found_deployments_range",
            result_count=len(result),
            service_name=service_name,
        )
        return result

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
        if not self._is_available:
            return None

        for deploy in self._mock_deployments:
            if (
                deploy.service_name == service_name
                and deploy.version_to == version
                and deploy.namespace == namespace
            ):
                return deploy

        return None

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
        if not self._is_available:
            return None

        # 가장 최근 배포의 version_to 반환
        matching = [
            d
            for d in self._mock_deployments
            if d.service_name == service_name and d.namespace == namespace
        ]

        if not matching:
            return None

        # 시간순 정렬 후 마지막 항목
        matching.sort(key=lambda d: d.deployed_at)
        return matching[-1].version_to

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
        if not self._is_available:
            return []

        rollbacks = [
            d
            for d in self._mock_deployments
            if (
                d.service_name == service_name
                and d.namespace == namespace
                and d.is_rollback
            )
        ]

        # 최신순 정렬
        rollbacks.sort(key=lambda d: d.deployed_at, reverse=True)

        return rollbacks[:limit]

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
        if not self._is_available:
            return []

        result = []
        for change in self._mock_config_changes:
            # 서비스 필터
            if change.service_name and change.service_name != service_name:
                continue
            # 네임스페이스 필터
            if change.namespace != namespace:
                continue
            # 시간 범위 필터
            try:
                change_time = datetime.fromisoformat(
                    change.changed_at.replace("Z", "+00:00")
                )
                if start_time <= change_time <= end_time:
                    result.append(change)
            except (ValueError, TypeError):
                continue

        # 시간순 정렬
        result.sort(key=lambda c: c.changed_at)

        logger.debug(
            "mock_deployment_adapter.found_config_changes_range",
            result_count=len(result),
            service_name=service_name,
        )
        return result

    def is_available(self) -> bool:
        """
        어댑터가 사용 가능한지 확인합니다.

        Returns:
            사용 가능 여부
        """
        return self._is_available


def create_sample_deployment(
    service_name: str,
    version_from: str,
    version_to: str,
    minutes_ago: int,
    is_rollback: bool = False,
    namespace: str = "default",
) -> DeploymentEvent:
    """
    테스트용 샘플 배포 이벤트 생성.

    Args:
        service_name: 서비스 이름
        version_from: 이전 버전
        version_to: 새 버전
        minutes_ago: 몇 분 전 배포인지
        is_rollback: 롤백 여부
        namespace: 네임스페이스

    Returns:
        DeploymentEvent 인스턴스
    """
    deployed_at = utc_now() - timedelta(minutes=minutes_ago)

    return DeploymentEvent(
        deployment_id=f"deploy-{uuid4().hex[:8]}",
        service_name=service_name,
        version_from=version_from,
        version_to=version_to,
        deployed_at=deployed_at.isoformat(),
        deployed_by="ci/cd-pipeline",
        deployment_type=DeploymentType.ROLLING,
        source=DeploymentSource.MOCK,
        namespace=namespace,
        is_rollback=is_rollback,
    )


def create_sample_config_change(
    config_key: str,
    old_value: str,
    new_value: str,
    minutes_ago: int,
    service_name: str = "",
    namespace: str = "default",
) -> DeploymentConfigChange:
    """
    테스트용 샘플 설정 변경 이벤트 생성.

    Args:
        config_key: 설정 키
        old_value: 이전 값
        new_value: 새 값
        minutes_ago: 몇 분 전 변경인지
        service_name: 서비스 이름
        namespace: 네임스페이스

    Returns:
        DeploymentConfigChange 인스턴스
    """
    changed_at = utc_now() - timedelta(minutes=minutes_ago)

    return DeploymentConfigChange(
        change_id=f"config-{uuid4().hex[:8]}",
        config_key=config_key,
        old_value=old_value,
        new_value=new_value,
        changed_at=changed_at.isoformat(),
        changed_by="admin",
        service_name=service_name,
        namespace=namespace,
    )
