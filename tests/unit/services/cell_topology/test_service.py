"""
CellTopologyService 오케스트레이터 테스트 (doc 388).

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Behavior: start/stop 시퀀스, anti-entropy thread, health delegation,
            singleton lifecycle, disabled/duplicate-start guard

참조 소스:
- services/cell_topology/service.py (CellTopologyService, get_/reset_)
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.cell_topology.service import (
    CellTopologyService,
    get_cell_topology_service,
    reset_cell_topology_service,
)

# Patch paths — lazy imports resolve to their defining modules
_SETTINGS = "baldur.settings.cell_topology.get_cell_topology_settings"
_REGISTRY = "baldur.services.cell_topology.registry.get_cell_registry"
_POLICY = "baldur.services.cell_topology.policy.get_cell_evacuation_policy"
_REGISTER = "baldur.services.cell_topology.registry.register_cell_handlers"
_UNREGISTER = "baldur.services.cell_topology.registry.unregister_cell_handlers"
_HEALTH = "baldur.services.cell_topology.health.setup_cell_health_scheduler"


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Ensure clean singleton state for each test."""
    reset_cell_topology_service()
    yield
    reset_cell_topology_service()


def _make_enabled_settings(reconciliation_interval: float = 999) -> MagicMock:
    """Helper to create enabled settings mock."""
    settings = MagicMock()
    settings.enabled = True
    settings.reconciliation_interval_seconds = reconciliation_interval
    return settings


class TestCellTopologyServiceStartBehavior:
    """start() 시퀀스 동작 검증."""

    @patch(_HEALTH)
    @patch(_REGISTER, autospec=True)
    @patch(_POLICY)
    @patch(_REGISTRY)
    @patch(_SETTINGS)
    def test_start_registers_handlers_and_hydrates(
        self,
        mock_settings,
        mock_get_reg,
        mock_get_policy,
        mock_register,
        mock_health,
    ):
        """start()가 handler 등록 → hydration → anti-entropy → health 순서로 실행한다."""
        mock_settings.return_value = _make_enabled_settings()
        mock_registry = MagicMock()
        mock_registry._load_all_states_from_redis.return_value = 3
        mock_get_reg.return_value = mock_registry

        service = CellTopologyService()
        service.start()

        mock_register.assert_called_once_with(mock_registry)
        # hydration call + potential anti-entropy loop iteration
        assert mock_registry._load_all_states_from_redis.call_count >= 1
        mock_health.assert_called_once()
        assert service.active is True

        service.stop()

    @patch(_SETTINGS)
    def test_start_disabled_returns_immediately(self, mock_settings):
        """enabled=False이면 start()가 즉시 반환한다."""
        settings = MagicMock()
        settings.enabled = False
        mock_settings.return_value = settings

        service = CellTopologyService()
        service.start()
        assert service.active is False

    @patch(_HEALTH)
    @patch(_REGISTER, autospec=True)
    @patch(_POLICY)
    @patch(_REGISTRY)
    @patch(_SETTINGS)
    def test_start_duplicate_returns_immediately(
        self,
        mock_settings,
        mock_get_reg,
        mock_get_policy,
        mock_register,
        mock_health,
    ):
        """이미 active인 경우 start()가 즉시 반환한다."""
        mock_settings.return_value = _make_enabled_settings()
        mock_get_reg.return_value = MagicMock()

        service = CellTopologyService()
        service.start()
        initial_call_count = mock_register.call_count

        service.start()  # duplicate
        assert mock_register.call_count == initial_call_count

        service.stop()


class TestCellTopologyServiceStopBehavior:
    """stop() 동작 검증 (Q10)."""

    @patch(_HEALTH)
    @patch(_UNREGISTER, autospec=True)
    @patch(_REGISTER, autospec=True)
    @patch(_POLICY)
    @patch(_REGISTRY)
    @patch(_SETTINGS)
    def test_stop_unregisters_handlers_and_marks_inactive(
        self,
        mock_settings,
        mock_get_reg,
        mock_get_policy,
        mock_register,
        mock_unregister,
        mock_health,
    ):
        """stop()이 handler를 해제하고 active=False로 설정한다."""
        mock_settings.return_value = _make_enabled_settings()
        mock_registry = MagicMock()
        mock_get_reg.return_value = mock_registry

        service = CellTopologyService()
        service.start()
        service.stop()

        mock_unregister.assert_called_once_with(mock_registry)
        assert service.active is False

    def test_stop_when_not_active_is_noop(self):
        """active가 아닐 때 stop()은 아무것도 하지 않는다."""
        with patch(_SETTINGS) as mock_settings:
            mock_settings.return_value = MagicMock(enabled=False)
            service = CellTopologyService()
            service.stop()
            assert service.active is False


class TestCellTopologyServiceHealthFaultIsolationBehavior:
    """Health scheduling fault isolation 동작 검증 (Q4)."""

    @patch(_HEALTH, side_effect=RuntimeError("scheduler init failed"))
    @patch(_REGISTER, autospec=True)
    @patch(_POLICY)
    @patch(_REGISTRY)
    @patch(_SETTINGS)
    def test_health_failure_does_not_prevent_start(
        self,
        mock_settings,
        mock_get_reg,
        mock_get_policy,
        mock_register,
        mock_health,
    ):
        """Health scheduler 실패가 서비스 시작을 막지 않는다."""
        mock_settings.return_value = _make_enabled_settings()
        mock_get_reg.return_value = MagicMock()

        service = CellTopologyService()
        service.start()

        assert service.active is True
        service.stop()


class TestCellTopologyServiceAntiEntropyBehavior:
    """Anti-entropy daemon thread 동작 검증 (Q8)."""

    @patch(_HEALTH)
    @patch(_REGISTER, autospec=True)
    @patch(_POLICY)
    @patch(_REGISTRY)
    @patch(_SETTINGS)
    def test_anti_entropy_thread_is_daemon(
        self,
        mock_settings,
        mock_get_reg,
        mock_get_policy,
        mock_register,
        mock_health,
    ):
        """Anti-entropy thread는 daemon이어야 한다."""
        mock_settings.return_value = _make_enabled_settings()
        mock_get_reg.return_value = MagicMock()

        service = CellTopologyService()
        service.start()

        assert service._anti_entropy_thread is not None
        assert service._anti_entropy_thread.daemon is True
        assert service._anti_entropy_thread.name == "cell-topology-anti-entropy"

        service.stop()

    @patch(_HEALTH)
    @patch(_REGISTER, autospec=True)
    @patch(_POLICY)
    @patch(_REGISTRY)
    @patch(_SETTINGS)
    def test_stop_terminates_anti_entropy_thread(
        self,
        mock_settings,
        mock_get_reg,
        mock_get_policy,
        mock_register,
        mock_health,
    ):
        """stop() 후 anti-entropy thread가 종료된다."""
        mock_settings.return_value = _make_enabled_settings(
            reconciliation_interval=0.01
        )
        mock_get_reg.return_value = MagicMock()

        service = CellTopologyService()
        service.start()
        thread = service._anti_entropy_thread
        assert thread is not None

        service.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert service._anti_entropy_thread is None


class TestCellTopologyServiceSingletonBehavior:
    """get_cell_topology_service / reset_cell_topology_service 싱글톤 검증."""

    def test_get_returns_same_instance(self):
        """get_cell_topology_service()는 동일 인스턴스를 반환한다."""
        s1 = get_cell_topology_service()
        s2 = get_cell_topology_service()
        assert s1 is s2

    def test_reset_creates_new_instance(self):
        """reset 후 새 인스턴스가 생성된다."""
        s1 = get_cell_topology_service()
        reset_cell_topology_service()
        s2 = get_cell_topology_service()
        assert s1 is not s2

    def test_concurrent_get_returns_same_instance(self):
        """멀티스레드에서 get_cell_topology_service()가 동일 인스턴스를 반환한다."""
        results = []

        def worker():
            results.append(get_cell_topology_service())

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is results[0] for r in results)
