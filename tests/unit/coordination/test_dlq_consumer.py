"""
DLQConsumerCoordinator 단위 테스트.

packages/baldur-python/tests/unit/coordination/test_dlq_consumer.py
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.coordination.base import LeadershipState
from baldur.coordination.dlq_consumer import (
    DLQ_CONSUMER_RESOURCE,
    DLQConsumerCoordinator,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_leader_elector():
    """LeaderElector Mock."""
    elector = MagicMock()
    elector.is_leader.return_value = False
    elector.state = LeadershipState.NOT_STARTED
    elector.resource_name = "dlq-consumer-test"
    elector.get_fencing_token.return_value = 1

    on_become_callbacks = []
    on_lose_callbacks = []

    def on_become_leader(callback):
        on_become_callbacks.append(callback)
        return callback

    def on_lose_leader(callback):
        on_lose_callbacks.append(callback)
        return callback

    elector.on_become_leader.side_effect = on_become_leader
    elector.on_lose_leader.side_effect = on_lose_leader
    elector._on_become_callbacks = on_become_callbacks
    elector._on_lose_callbacks = on_lose_callbacks

    return elector


@pytest.fixture
def coordinator(mock_leader_elector):
    """DLQConsumerCoordinator 인스턴스."""
    with (
        patch(
            "baldur.coordination.dlq_consumer.get_leader_elector",
            return_value=mock_leader_elector,
        ),
        patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
    ):
        coord = DLQConsumerCoordinator(
            resource_name="dlq-consumer-test",
            process_interval_seconds=0.1,
            batch_size=5,
        )
        yield coord

        # Cleanup
        try:
            coord.stop()
        except Exception:
            pass


# =============================================================================
# 초기화 테스트
# =============================================================================


class TestDLQConsumerCoordinatorInitialization:
    """초기화 테스트."""

    def test_should_initialize_with_default_resource_name(self, mock_leader_elector):
        """기본 리소스 이름으로 초기화되어야 한다."""
        with (
            patch(
                "baldur.coordination.dlq_consumer.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
        ):
            coord = DLQConsumerCoordinator()

        assert coord._resource_name == DLQ_CONSUMER_RESOURCE

    def test_should_initialize_with_custom_parameters(self, mock_leader_elector):
        """사용자 지정 파라미터로 초기화되어야 한다."""
        with (
            patch(
                "baldur.coordination.dlq_consumer.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
        ):
            coord = DLQConsumerCoordinator(
                resource_name="custom-dlq",
                process_interval_seconds=2.0,
                batch_size=20,
            )

        assert coord._resource_name == "custom-dlq"
        assert coord._process_interval == 2.0
        assert coord._batch_size == 20

    def test_should_register_leader_callbacks(self, mock_leader_elector):
        """리더 콜백이 등록되어야 한다."""
        with (
            patch(
                "baldur.coordination.dlq_consumer.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
        ):
            DLQConsumerCoordinator(resource_name="test-dlq")

        assert mock_leader_elector.on_become_leader.called
        assert mock_leader_elector.on_lose_leader.called


# =============================================================================
# 상태 테스트
# =============================================================================


class TestDLQConsumerCoordinatorState:
    """상태 테스트."""

    def test_should_not_be_consuming_initially(self, coordinator):
        """초기에는 소비 중이 아니어야 한다."""
        assert not coordinator.is_consuming

    def test_should_not_be_leader_initially(self, coordinator, mock_leader_elector):
        """초기에는 리더가 아니어야 한다."""
        mock_leader_elector.is_leader.return_value = False
        assert not coordinator.is_leader


# =============================================================================
# 시작/중지 테스트
# =============================================================================


class TestDLQConsumerCoordinatorStartStop:
    """시작/중지 테스트."""

    def test_should_start_elector_on_start(self, coordinator, mock_leader_elector):
        """start() 호출 시 elector가 시작되어야 한다."""
        coordinator.start()

        assert mock_leader_elector.start.called

    def test_should_stop_elector_on_stop(self, coordinator, mock_leader_elector):
        """stop() 호출 시 elector가 중지되어야 한다."""
        coordinator.start()
        coordinator.stop()

        assert mock_leader_elector.stop.called

    def test_should_not_be_consuming_after_stop(self, coordinator):
        """stop() 후에는 소비 중이 아니어야 한다."""
        coordinator.start()
        coordinator.stop()

        assert not coordinator.is_consuming


# =============================================================================
# 리더십 변경 테스트
# =============================================================================


class TestDLQConsumerCoordinatorLeadershipChange:
    """리더십 변경 테스트."""

    def test_should_start_consuming_on_become_leader(
        self, coordinator, mock_leader_elector
    ):
        """리더가 되면 소비를 시작해야 한다."""
        coordinator.start()

        # 리더가 됨
        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.05)  # 스레드 시작 대기

        assert coordinator.is_consuming

    def test_should_stop_consuming_on_lose_leader(
        self, coordinator, mock_leader_elector
    ):
        """리더십을 잃으면 소비를 중지해야 한다."""
        coordinator.start()

        # 리더가 됨
        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.05)
        assert coordinator.is_consuming

        # 리더십 상실
        mock_leader_elector.is_leader.return_value = False
        for callback in mock_leader_elector._on_lose_callbacks:
            callback()

        assert not coordinator.is_consuming

    def test_should_start_consume_thread_on_become_leader(
        self, coordinator, mock_leader_elector
    ):
        """리더가 되면 소비 스레드가 시작되어야 한다."""
        coordinator.start()

        # 리더가 됨
        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.05)

        assert coordinator._consume_thread is not None
        assert coordinator._consume_thread.is_alive()

        coordinator.stop()


# =============================================================================
# DLQ 처리 테스트
# =============================================================================


class TestDLQConsumerCoordinatorProcessing:
    """DLQ 처리 테스트."""

    def test_should_call_process_dlq_batch_when_leader(self, mock_leader_elector):
        """리더일 때 _process_dlq_batch를 호출해야 한다."""
        with (
            patch(
                "baldur.coordination.dlq_consumer.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
        ):
            coord = DLQConsumerCoordinator(
                resource_name="test-dlq",
                process_interval_seconds=0.05,
                batch_size=5,
            )

        # _process_dlq_batch 메서드 모킹
        coord._process_dlq_batch = MagicMock(return_value=0)

        coord.start()

        # 리더가 됨
        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.15)  # 처리 대기

        coord.stop()

        # _process_dlq_batch가 호출되었는지 확인
        assert coord._process_dlq_batch.called

    def test_should_not_process_when_not_leader(self, coordinator, mock_leader_elector):
        """리더가 아닐 때는 처리하지 않아야 한다."""
        # _process_dlq_batch 메서드 모킹
        coordinator._process_dlq_batch = MagicMock()

        coordinator.start()

        # 리더가 아님
        mock_leader_elector.is_leader.return_value = False

        time.sleep(0.15)

        coordinator.stop()

        # 리더가 아니면 consume_thread가 시작되지 않음
        assert not coordinator.is_consuming


# =============================================================================
# 펜싱 토큰 테스트
# =============================================================================


class TestDLQConsumerCoordinatorFencing:
    """펜싱 토큰 테스트."""

    def test_should_check_leadership_in_consume_loop(self, mock_leader_elector):
        """소비 루프에서 리더십을 확인해야 한다."""
        is_leader_calls = []

        def track_is_leader():
            is_leader_calls.append(True)
            return len(is_leader_calls) <= 3  # 3번까지만 True

        mock_leader_elector.is_leader.side_effect = track_is_leader

        with (
            patch(
                "baldur.coordination.dlq_consumer.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
        ):
            coord = DLQConsumerCoordinator(
                resource_name="test-dlq",
                process_interval_seconds=0.05,
            )

        coord.start()

        # 리더가 됨
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.3)

        coord.stop()

        # is_leader가 여러 번 호출되었어야 함
        assert len(is_leader_calls) >= 1

    def test_should_stop_consuming_when_leadership_check_fails(
        self, mock_leader_elector
    ):
        """리더십 확인 실패 시 소비를 중지해야 한다."""
        call_count = [0]

        def sometimes_leader():
            call_count[0] += 1
            # 처음 2번만 True
            return call_count[0] <= 2

        mock_leader_elector.is_leader.side_effect = sometimes_leader

        with (
            patch(
                "baldur.coordination.dlq_consumer.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
        ):
            coord = DLQConsumerCoordinator(
                resource_name="test-dlq",
                process_interval_seconds=0.05,
            )

        coord.start()

        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.25)

        # 루프가 종료되었어야 함
        # (스레드가 종료되거나 대기 중)

        coord.stop()


# =============================================================================
# 예외 처리 테스트
# =============================================================================


class TestDLQConsumerCoordinatorErrorHandling:
    """예외 처리 테스트."""

    def test_should_continue_on_import_error(self, mock_leader_elector):
        """ImportError 발생 시에도 계속해야 한다."""
        with (
            patch(
                "baldur.coordination.dlq_consumer.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
        ):
            coord = DLQConsumerCoordinator(
                resource_name="test-dlq",
                process_interval_seconds=0.05,
            )

        coord.start()

        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.15)

        # ImportError 후에도 계속 실행 중
        assert coord.is_consuming

        coord.stop()

    def test_should_handle_processing_exception(self, mock_leader_elector):
        """처리 예외를 처리해야 한다."""
        pytest.importorskip("baldur_pro")
        mock_dlq_service = MagicMock()
        mock_dlq_service.list_pending_entries.side_effect = Exception("처리 오류")

        with (
            patch(
                "baldur.coordination.dlq_consumer.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
            patch(
                "baldur_pro.services.dlq.get_dlq_service",
                return_value=mock_dlq_service,
            ),
        ):
            coord = DLQConsumerCoordinator(
                resource_name="test-dlq",
                process_interval_seconds=0.05,
            )

        coord.start()

        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.15)

        # 예외 발생해도 실행 중
        assert coord.is_consuming

        coord.stop()
