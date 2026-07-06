"""
PartitionReconciliationService 단위 테스트.

네트워크 고립 복구 시 상태 조정 서비스 테스트.
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.models.emergency import EmergencyLevel, EmergencyScope
from baldur.services.regional_emergency.partition_reconciliation import (
    PartitionReconciliationService,
    PartitionStatus,
    ReconciliationAction,
    ReconciliationResult,
    get_partition_reconciliation_service,
    reset_partition_reconciliation_service,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """테스트 간 싱글톤 초기화."""
    reset_partition_reconciliation_service()
    yield
    reset_partition_reconciliation_service()


@pytest.fixture
def mock_tracker():
    """Mock NamespacedEmergencyTracker."""
    return MagicMock()


@pytest.fixture
def mock_tiered_redis():
    """Mock TieredRedisProvider."""
    mock = MagicMock()
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock.get_redis.return_value = mock_client
    return mock


def create_mock_state(
    namespace: str = "seoul",
    scope: EmergencyScope = EmergencyScope.REGIONAL,
    governance_mode: str = "NORMAL",
    emergency_level: EmergencyLevel = EmergencyLevel.NORMAL,
) -> MagicMock:
    """ScopedEmergencyState Mock 생성."""
    mock = MagicMock()
    mock.namespace = namespace
    mock.scope = scope
    mock.governance_mode = governance_mode
    mock.emergency_level = emergency_level
    mock.is_active.return_value = emergency_level != EmergencyLevel.NORMAL
    return mock


class TestPartitionStatus:
    """PartitionStatus 데이터 클래스 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        status = PartitionStatus()

        assert status.is_partitioned is False
        assert status.last_heartbeat_at is None
        assert status.partition_duration_seconds == 0.0
        assert status.error_message is None

    def test_to_dict(self):
        """딕셔너리 변환."""
        status = PartitionStatus(
            is_partitioned=True,
            last_heartbeat_at="2026-01-22T10:00:00+00:00",
            partition_duration_seconds=60.0,
            error_message="Connection refused",
        )

        result = status.to_dict()

        assert result["is_partitioned"] is True
        assert result["last_heartbeat_at"] == "2026-01-22T10:00:00+00:00"
        assert result["partition_duration_seconds"] == 60.0
        assert result["error_message"] == "Connection refused"


class TestReconciliationAction:
    """ReconciliationAction 데이터 클래스 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        action = ReconciliationAction()

        assert action.action_type == ""
        assert action.message == ""
        assert action.success is True

    def test_to_dict(self):
        """딕셔너리 변환."""
        action = ReconciliationAction(
            action_type="NOTIFICATION",
            message="Test message",
            namespace="seoul",
            success=True,
        )

        result = action.to_dict()

        assert result["action_type"] == "NOTIFICATION"
        assert result["message"] == "Test message"
        assert result["namespace"] == "seoul"
        assert "executed_at" in result


class TestReconciliationResult:
    """ReconciliationResult 데이터 클래스 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        result = ReconciliationResult()

        assert result.reconciled is False
        assert result.reason is None
        assert result.actions == []

    def test_to_dict_with_actions(self):
        """액션 포함 딕셔너리 변환."""
        action = ReconciliationAction(
            action_type="MANUAL_REVIEW",
            message="Review needed",
        )
        result = ReconciliationResult(
            reconciled=True,
            actions=[action],
            global_state_mode="NORMAL",
            regional_state_mode="STRICT",
        )

        data = result.to_dict()

        assert data["reconciled"] is True
        assert len(data["actions"]) == 1
        assert data["actions"][0]["action_type"] == "MANUAL_REVIEW"
        assert data["global_state_mode"] == "NORMAL"
        assert data["regional_state_mode"] == "STRICT"


class TestPartitionDetection:
    """네트워크 고립 감지 테스트."""

    def test_not_partitioned_when_ping_succeeds(self, mock_tiered_redis):
        """ping 성공 시 고립 아님."""
        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
        )

        status = service.check_partition_status()

        assert status.is_partitioned is False
        assert status.last_heartbeat_at is not None
        assert status.partition_duration_seconds == 0.0

    def test_partitioned_when_ping_fails(self, mock_tiered_redis):
        """ping 실패 시 고립."""
        mock_tiered_redis.get_redis.return_value.ping.side_effect = Exception(
            "Connection refused"
        )

        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
            partition_threshold=0,  # 즉시 고립 판단
        )

        status = service.check_partition_status()

        assert status.is_partitioned is True
        assert status.error_message is not None

    def test_partition_detection_threshold(self, mock_tiered_redis):
        """고립 감지 임계값."""
        # 첫 번째 ping 성공
        mock_tiered_redis.get_redis.return_value.ping.return_value = True
        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
            partition_threshold=1,  # 1초 임계값
        )
        service.check_partition_status()  # heartbeat 기록

        # 두 번째 ping 실패 (임계값 이내)
        mock_tiered_redis.get_redis.return_value.ping.side_effect = Exception("Error")
        status = service.check_partition_status()

        # 아직 임계값 이내면 고립 아님
        assert status.partition_duration_seconds >= 0

    def test_is_partitioned_method(self, mock_tiered_redis):
        """is_partitioned() 간편 메서드."""
        mock_tiered_redis.get_redis.return_value.ping.side_effect = Exception("Error")

        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
            partition_threshold=0,
        )
        service.check_partition_status()

        assert service.is_partitioned() is True


class TestReconciliation:
    """상태 조정 테스트."""

    def test_reconcile_skipped_when_partitioned(self, mock_tracker, mock_tiered_redis):
        """고립 상태에서 조정 스킵."""
        mock_tiered_redis.get_redis.return_value.ping.side_effect = Exception("Error")

        service = PartitionReconciliationService(
            tracker=mock_tracker,
            tiered_redis=mock_tiered_redis,
            partition_threshold=0,
        )
        service.check_partition_status()  # 고립 상태로 만들기

        result = service.reconcile_after_recovery()

        assert result.reconciled is False
        assert "partitioned" in result.reason.lower()

    def test_reconcile_no_action_when_states_match(
        self, mock_tracker, mock_tiered_redis
    ):
        """상태 일치 시 액션 없음."""
        mock_tracker.get_state.side_effect = [
            create_mock_state(
                namespace="global",
                scope=EmergencyScope.GLOBAL,
                governance_mode="NORMAL",
                emergency_level=EmergencyLevel.NORMAL,
            ),
            create_mock_state(
                namespace="seoul",
                scope=EmergencyScope.REGIONAL,
                governance_mode="NORMAL",
                emergency_level=EmergencyLevel.NORMAL,
            ),
        ]

        service = PartitionReconciliationService(
            tracker=mock_tracker,
            tiered_redis=mock_tiered_redis,
        )
        service.check_partition_status()  # 정상 상태 확인

        result = service.reconcile_after_recovery()

        assert result.reconciled is True
        assert len(result.actions) == 0

    def test_reconcile_action_when_regional_strict_global_normal(
        self, mock_tracker, mock_tiered_redis
    ):
        """Global NORMAL, Regional STRICT 불일치 - 레벨 불일치 알림."""
        mock_tracker.get_state.side_effect = [
            create_mock_state(
                namespace="global",
                scope=EmergencyScope.GLOBAL,
                governance_mode="NORMAL",
                emergency_level=EmergencyLevel.NORMAL,
            ),
            create_mock_state(
                namespace="seoul",
                scope=EmergencyScope.REGIONAL,
                governance_mode="STRICT",
                emergency_level=EmergencyLevel.LEVEL_3,
            ),
        ]

        service = PartitionReconciliationService(
            tracker=mock_tracker,
            tiered_redis=mock_tiered_redis,
        )
        service.check_partition_status()

        result = service.reconcile_after_recovery()

        assert result.reconciled is True
        assert len(result.actions) >= 1
        # 둘 다 활성화 상태이므로 레벨 불일치 알림
        assert result.actions[0].action_type in ["MANUAL_REVIEW", "NOTIFICATION"]

    def test_reconcile_action_when_global_strict_regional_normal(
        self, mock_tracker, mock_tiered_redis
    ):
        """Global STRICT, Regional NORMAL 불일치."""
        mock_tracker.get_state.side_effect = [
            create_mock_state(
                namespace="global",
                scope=EmergencyScope.GLOBAL,
                governance_mode="STRICT",
                emergency_level=EmergencyLevel.LEVEL_3,
            ),
            create_mock_state(
                namespace="seoul",
                scope=EmergencyScope.REGIONAL,
                governance_mode="NORMAL",
                emergency_level=EmergencyLevel.NORMAL,
            ),
        ]

        service = PartitionReconciliationService(
            tracker=mock_tracker,
            tiered_redis=mock_tiered_redis,
        )
        service.check_partition_status()

        result = service.reconcile_after_recovery()

        assert result.reconciled is True
        assert len(result.actions) == 1
        assert result.actions[0].action_type == "NOTIFICATION"

    def test_reconcile_action_level_mismatch(self, mock_tracker, mock_tiered_redis):
        """둘 다 활성화, 레벨 불일치."""
        mock_tracker.get_state.side_effect = [
            create_mock_state(
                namespace="global",
                scope=EmergencyScope.GLOBAL,
                governance_mode="STRICT",
                emergency_level=EmergencyLevel.LEVEL_3,
            ),
            create_mock_state(
                namespace="seoul",
                scope=EmergencyScope.REGIONAL,
                governance_mode="STRICT",
                emergency_level=EmergencyLevel.LEVEL_2,  # 레벨 다름
            ),
        ]

        service = PartitionReconciliationService(
            tracker=mock_tracker,
            tiered_redis=mock_tiered_redis,
        )
        service.check_partition_status()

        result = service.reconcile_after_recovery()

        assert result.reconciled is True
        assert len(result.actions) == 1
        assert "mismatch" in result.actions[0].message.lower()


class TestActionHistory:
    """액션 히스토리 테스트."""

    def test_get_recent_actions_empty(self, mock_tiered_redis):
        """히스토리 비어있을 때."""
        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
        )

        actions = service.get_recent_actions()

        assert actions == []

    def test_get_recent_actions_after_reconciliation(
        self, mock_tracker, mock_tiered_redis
    ):
        """조정 후 히스토리."""
        mock_tracker.get_state.side_effect = [
            create_mock_state(
                namespace="global",
                scope=EmergencyScope.GLOBAL,
                governance_mode="NORMAL",
                emergency_level=EmergencyLevel.NORMAL,
            ),
            create_mock_state(
                namespace="seoul",
                scope=EmergencyScope.REGIONAL,
                governance_mode="STRICT",
                emergency_level=EmergencyLevel.LEVEL_3,
            ),
        ]

        service = PartitionReconciliationService(
            tracker=mock_tracker,
            tiered_redis=mock_tiered_redis,
        )
        service.check_partition_status()
        service.reconcile_after_recovery()

        actions = service.get_recent_actions()

        assert len(actions) >= 1
        assert actions[0]["action_type"] in ["MANUAL_REVIEW", "NOTIFICATION"]

    def test_get_recent_actions_respects_limit(self, mock_tiered_redis):
        """limit 파라미터 동작."""
        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
        )

        # 직접 액션 추가 (테스트용)
        for i in range(10):
            action = ReconciliationAction(
                action_type="TEST",
                message=f"Action {i}",
            )
            service._action_history.append(action)

        actions = service.get_recent_actions(limit=5)

        assert len(actions) == 5


class TestHeartbeatLoop:
    """Heartbeat 루프 테스트."""

    def test_start_heartbeat_loop(self, mock_tiered_redis):
        """Heartbeat 루프 시작."""
        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
            heartbeat_interval=1,
        )

        service.start_heartbeat_loop()
        assert service._heartbeat_running is True

        # 정리
        service.stop_heartbeat_loop()

    def test_stop_heartbeat_loop(self, mock_tiered_redis):
        """Heartbeat 루프 중지."""
        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
            heartbeat_interval=1,
        )

        service.start_heartbeat_loop()
        service.stop_heartbeat_loop()

        assert service._heartbeat_running is False

    def test_double_start_warning(self, mock_tiered_redis):
        """중복 시작 경고."""
        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
            heartbeat_interval=1,
        )

        service.start_heartbeat_loop()
        service.start_heartbeat_loop()  # 두 번째 시작

        # 정리
        service.stop_heartbeat_loop()


class TestPartitionDuration:
    """고립 지속 시간 테스트."""

    def test_get_partition_duration_when_not_partitioned(self, mock_tiered_redis):
        """고립 아닐 때 None 반환."""
        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
        )
        service.check_partition_status()  # 정상 연결

        duration = service.get_partition_duration()

        assert duration is None

    def test_get_partition_duration_when_partitioned(self, mock_tiered_redis):
        """고립 시 지속 시간 반환."""
        mock_tiered_redis.get_redis.return_value.ping.side_effect = Exception("Error")

        service = PartitionReconciliationService(
            tiered_redis=mock_tiered_redis,
            partition_threshold=0,
        )
        service.check_partition_status()

        duration = service.get_partition_duration()

        assert duration is not None
        assert duration >= 0


class TestSingleton:
    """싱글톤 테스트."""

    def test_singleton_returns_same_instance(self):
        """싱글톤 동일 인스턴스 반환."""
        instance1 = get_partition_reconciliation_service()
        instance2 = get_partition_reconciliation_service()

        assert instance1 is instance2

    def test_reset_clears_singleton(self):
        """싱글톤 초기화."""
        instance1 = get_partition_reconciliation_service()
        reset_partition_reconciliation_service()
        instance2 = get_partition_reconciliation_service()

        assert instance1 is not instance2


class TestFallbackWithoutTieredRedis:
    """TieredRedisProvider 없이 동작 테스트."""

    def test_fallback_to_state_backend_success(self, mock_tracker):
        """TieredRedisProvider 없으면 StateBackend로 폴백 - 성공 케이스."""
        with patch.object(
            PartitionReconciliationService, "_get_tiered_redis", return_value=None
        ):
            service = PartitionReconciliationService(tracker=mock_tracker)

            # _ping_global_redis 내부에서 get_state_backend를 로컬 import하므로
            # 실제 모듈을 패치
            with patch(
                "baldur.core.state_backend.get_state_backend"
            ) as mock_get_backend:
                mock_backend = MagicMock(spec=[])  # spec=[]로 ping 메서드 없음을 명시
                mock_backend.get = MagicMock(return_value=None)  # get만 있음
                mock_get_backend.return_value = mock_backend

                # _ping_global_redis를 직접 호출하여 fallback 로직 테스트
                result = service._ping_global_redis()

                # StateBackend.get() 호출됨
                mock_backend.get.assert_called_once_with("__ping_test__")
                assert result is True

    def test_fallback_to_state_backend_with_ping_method(self, mock_tracker):
        """StateBackend에 ping 메서드가 있으면 사용."""
        with patch.object(
            PartitionReconciliationService, "_get_tiered_redis", return_value=None
        ):
            service = PartitionReconciliationService(tracker=mock_tracker)

            with patch(
                "baldur.core.state_backend.get_state_backend"
            ) as mock_get_backend:
                mock_backend = MagicMock()
                mock_backend.ping.return_value = True
                mock_get_backend.return_value = mock_backend

                result = service._ping_global_redis()

                # ping 메서드 호출됨
                mock_backend.ping.assert_called_once()
                assert result is True

    def test_fallback_to_state_backend_failure(self, mock_tracker):
        """StateBackend도 실패하면 partitioned로 감지."""
        with patch.object(
            PartitionReconciliationService, "_get_tiered_redis", return_value=None
        ):
            service = PartitionReconciliationService(tracker=mock_tracker)

            with patch(
                "baldur.core.state_backend.get_state_backend"
            ) as mock_get_backend:
                mock_backend = MagicMock()
                mock_backend.ping.side_effect = Exception("Connection refused")
                mock_get_backend.return_value = mock_backend

                result = service._ping_global_redis()

                # 실패 시 False 반환
                assert result is False

    def test_partition_status_when_fallback_fails(self, mock_tracker):
        """Fallback 실패 시 check_partition_status가 partitioned 반환."""
        with patch.object(
            PartitionReconciliationService, "_get_tiered_redis", return_value=None
        ):
            with patch(
                "baldur.core.state_backend.get_state_backend"
            ) as mock_get_backend:
                mock_backend = MagicMock()
                mock_backend.ping.side_effect = Exception("No backend")
                mock_get_backend.return_value = mock_backend

                service = PartitionReconciliationService(tracker=mock_tracker)
                status = service.check_partition_status()

                # 연결 실패로 partitioned
                assert status.is_partitioned is True
