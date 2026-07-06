"""
L3 Cell Topology EventType Publisher + Logging Fix Tests.

테스트 대상: services/cell_topology/registry.py, services/cell_topology/policy.py
검증 기법: 부수효과 (emit 호출), 로깅 표준 준수, 데드 코드 제거 확인

Test Categories:
    A. CellRegistry — CELL_STATE_CHANGED emission
    B. CellEvacuationPolicy — CELL_EVACUATION_STARTED / COMPLETED / CELL_RESTORED
    B2. CellEvacuationPolicy — CELL_EVACUATION_CANCELLED (contract + behavior)
    C. Logging standard compliance — 15 non-constant format strings fixed
    D. Dead code removal — _subscribe_state_changes / _on_cell_state_event removed
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.cell_topology.models import CellState
from baldur.services.cell_topology.policy import (
    CellEvacuationPolicy,
    reset_cell_evacuation_policy,
)
from baldur.services.cell_topology.registry import (
    CellRegistry,
    reset_cell_registry,
)
from baldur.services.event_bus.bus.event_types import EventType
from baldur.settings.cell_topology import (
    CellTopologySettings,
    reset_cell_topology_settings,
)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """각 테스트 전후 싱글톤 리셋."""
    reset_cell_evacuation_policy()
    reset_cell_registry()
    reset_cell_topology_settings()
    yield
    reset_cell_evacuation_policy()
    reset_cell_registry()
    reset_cell_topology_settings()


@pytest.fixture
def settings() -> CellTopologySettings:
    """대피 기능 활성화 설정."""
    return CellTopologySettings(
        enabled=True,
        evacuation_enabled=True,
        bulkhead_isolation_enabled=False,
        cell_count=4,
        cell_prefix="cell",
    )


@pytest.fixture
def registry(settings: CellTopologySettings) -> CellRegistry:
    """CellRegistry (emit mock)."""
    reg = CellRegistry(settings=settings)
    reg._emit_event = MagicMock()
    return reg


@pytest.fixture
def policy(settings: CellTopologySettings) -> CellEvacuationPolicy:
    """CellEvacuationPolicy (emit + fire-and-forget mock)."""
    pol = CellEvacuationPolicy(settings=settings)
    pol._emit_event = MagicMock()
    pol._notify_isolation_gate = lambda *a, **kw: None  # type: ignore[assignment]
    pol._notify_blast_radius = lambda *a, **kw: None  # type: ignore[assignment]
    pol._notify_restore_region = lambda *a, **kw: None  # type: ignore[assignment]
    return pol


# =============================================================================
# A. CellRegistry — CELL_STATE_CHANGED emission
# =============================================================================


class TestCellRegistryStateChangedEmissionBehavior:
    """CellRegistry.set_cell_state()에서 CELL_STATE_CHANGED emit 검증."""

    def test_set_cell_state_emits_cell_state_changed(self, registry):
        """상태 변경 시 CELL_STATE_CHANGED를 emit한다."""
        registry.set_cell_state("cell-0", CellState.DRAINING, "test reason")

        registry._emit_event.assert_called_once()
        call_args = registry._emit_event.call_args
        assert call_args[0][0] == EventType.CELL_STATE_CHANGED
        payload = call_args[0][1]
        assert payload["cell_id"] == "cell-0"
        assert payload["old_state"] == CellState.ACTIVE.value
        assert payload["new_state"] == CellState.DRAINING.value
        assert payload["reason"] == "test reason"

    def test_set_cell_state_unknown_cell_does_not_emit(self, registry):
        """존재하지 않는 Cell은 emit하지 않는다."""
        result = registry.set_cell_state("nonexistent", CellState.DRAINING)
        assert result is False
        registry._emit_event.assert_not_called()


# =============================================================================
# B. CellEvacuationPolicy — 4 EventType emissions
# =============================================================================


class TestCellEvacuationPolicyEvacuationStartedBehavior:
    """ACTIVE → DRAINING 전환 시 CELL_EVACUATION_STARTED emit."""

    def test_consecutive_threshold_reached_emits_evacuation_started(
        self,
        policy,
        registry,
        settings,
    ):
        """연속 카운터 도달 시 CELL_EVACUATION_STARTED를 emit한다."""
        # Given — registry를 get_cell_registry()로 반환하도록 patch
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            registry.get_cell_info("cell-0")
            consecutive_count = settings.evacuation_consecutive_count
            threshold = settings.evacuation_health_threshold

            # When — 연속 카운터 충족
            for _ in range(consecutive_count):
                policy.evaluate("cell-0", threshold - 0.1)

        # Then
        calls = [
            c
            for c in policy._emit_event.call_args_list
            if c[0][0] == EventType.CELL_EVACUATION_STARTED
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["cell_id"] == "cell-0"
        assert "reason" in calls[0][0][1]


class TestCellEvacuationPolicyEvacuationCompletedBehavior:
    """DRAINING → ISOLATED 전환 시 CELL_EVACUATION_COMPLETED emit."""

    def test_drain_complete_emits_evacuation_completed(
        self,
        policy,
        registry,
        settings,
    ):
        """드레인 완료 시 CELL_EVACUATION_COMPLETED를 emit한다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # Given — Cell을 DRAINING 상태로 설정
            registry.set_cell_state("cell-0", CellState.DRAINING, "test")
            cell = registry.get_cell_info("cell-0")
            cell.metadata["last_state_change_time"] = time.time() - 1000

            # When
            policy.evaluate("cell-0", 0.1)

        # Then
        calls = [
            c
            for c in policy._emit_event.call_args_list
            if c[0][0] == EventType.CELL_EVACUATION_COMPLETED
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["cell_id"] == "cell-0"


class TestCellEvacuationPolicyRestoredBehavior:
    """ISOLATED → ACTIVE 전환 시 CELL_RESTORED emit."""

    def test_auto_restore_emits_cell_restored_with_trigger_auto(
        self,
        policy,
        registry,
        settings,
    ):
        """자동 복구 시 trigger='auto' 페이로드로 CELL_RESTORED를 emit한다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # Given — Cell을 ISOLATED 상태로 설정
            registry.set_cell_state("cell-0", CellState.ISOLATED, "test")
            recovery_count = settings.recovery_consecutive_count
            recovery_threshold = settings.recovery_health_threshold

            # When — 연속 카운터 충족
            for _ in range(recovery_count):
                policy.evaluate("cell-0", recovery_threshold + 0.1)

        # Then
        calls = [
            c
            for c in policy._emit_event.call_args_list
            if c[0][0] == EventType.CELL_RESTORED
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["cell_id"] == "cell-0"
        assert calls[0][0][1]["trigger"] == "auto"

    def test_manual_restore_emits_cell_restored_with_trigger_manual(
        self,
        policy,
        registry,
    ):
        """수동 복구 시 trigger='manual' 페이로드로 CELL_RESTORED를 emit한다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            registry.set_cell_state("cell-0", CellState.ISOLATED, "test")

            policy.restore_cell("cell-0")

        calls = [
            c
            for c in policy._emit_event.call_args_list
            if c[0][0] == EventType.CELL_RESTORED
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["trigger"] == "manual"


class TestCellEvacuationCancelledContract:
    """CELL_EVACUATION_CANCELLED EventType 계약 검증."""

    def test_cell_evacuation_cancelled_value(self):
        """CELL_EVACUATION_CANCELLED = 'cell_evacuation_cancelled'."""
        assert EventType.CELL_EVACUATION_CANCELLED == "cell_evacuation_cancelled"


class TestCellEvacuationPolicyCancelledBehavior:
    """DRAINING 중 취소 시 CELL_EVACUATION_CANCELLED emit."""

    def test_metadata_mismatch_emits_evacuation_cancelled(
        self,
        policy,
        registry,
    ):
        """_tick_draining() metadata 불일치 시 CELL_EVACUATION_CANCELLED를 emit한다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # Given — Cell을 DRAINING으로 설정 후 metadata 불일치
            registry.set_cell_state("cell-0", CellState.DRAINING, "test")
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            cell.metadata["last_state_change"] = {"to": "active", "from": "draining"}

            # When
            result = policy.evaluate("cell-0", 0.2)

        # Then — 전이 없음 (False) + 취소 이벤트 발행
        assert result is False
        calls = [
            c
            for c in policy._emit_event.call_args_list
            if c[0][0] == EventType.CELL_EVACUATION_CANCELLED
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["cell_id"] == "cell-0"
        assert calls[0][0][1]["reason"] == "metadata_mismatch"

    def test_manual_restore_from_draining_emits_evacuation_cancelled(
        self,
        policy,
        registry,
    ):
        """DRAINING 중 수동 복구 시 CELL_EVACUATION_CANCELLED를 emit한다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # Given — DRAINING 상태
            registry.set_cell_state("cell-0", CellState.DRAINING, "test")

            # When
            policy.restore_cell("cell-0")

        # Then
        calls = [
            c
            for c in policy._emit_event.call_args_list
            if c[0][0] == EventType.CELL_EVACUATION_CANCELLED
        ]
        assert len(calls) == 1
        assert calls[0][0][1]["cell_id"] == "cell-0"
        assert calls[0][0][1]["reason"] == "manual_restore"

    def test_manual_restore_from_draining_emits_both_cancelled_and_restored(
        self,
        policy,
        registry,
    ):
        """DRAINING 중 수동 복구 시 CANCELLED + RESTORED 두 이벤트를 모두 emit한다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # Given
            registry.set_cell_state("cell-0", CellState.DRAINING, "test")

            # When
            policy.restore_cell("cell-0")

        # Then — CANCELLED 먼저, RESTORED 나중
        emitted_types = [c[0][0] for c in policy._emit_event.call_args_list]
        assert EventType.CELL_EVACUATION_CANCELLED in emitted_types
        assert EventType.CELL_RESTORED in emitted_types

    def test_manual_restore_from_isolated_does_not_emit_cancelled(
        self,
        policy,
        registry,
    ):
        """ISOLATED 상태에서 수동 복구 시 CELL_EVACUATION_CANCELLED를 emit하지 않는다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # Given — ISOLATED 상태 (정상 대피 완료 후)
            registry.set_cell_state("cell-0", CellState.ISOLATED, "test")

            # When
            policy.restore_cell("cell-0")

        # Then — RESTORED만, CANCELLED 없음
        calls = [
            c
            for c in policy._emit_event.call_args_list
            if c[0][0] == EventType.CELL_EVACUATION_CANCELLED
        ]
        assert len(calls) == 0


# =============================================================================
# C. Logging standard compliance
# =============================================================================


class TestCellEvacuationPolicyLoggingComplianceBehavior:
    """policy.py 로깅 표준 준수 확인 (string literal event names)."""

    def test_draining_started_uses_structured_event_name(
        self, policy, registry, settings
    ):
        """DRAINING 전환 시 'cell_evacuation.draining_started' 이벤트명을 사용한다."""
        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=registry,
            ),
            patch("baldur.services.cell_topology.policy.logger") as mock_log,
        ):
            consecutive_count = settings.evacuation_consecutive_count
            threshold = settings.evacuation_health_threshold

            for _ in range(consecutive_count):
                policy.evaluate("cell-0", threshold - 0.1)

        warning_calls = mock_log.warning.call_args_list
        event_names = [c[0][0] for c in warning_calls]
        assert "cell_evacuation.draining_started" in event_names

    def test_global_limit_uses_structured_event_name(self, registry, settings):
        """전역 대피 제한 시 'cell_evacuation.global_limit_reached' 이벤트명을 사용한다."""
        policy = CellEvacuationPolicy(
            settings=CellTopologySettings(
                enabled=True,
                evacuation_enabled=True,
                bulkhead_isolation_enabled=False,
                cell_count=4,
                cell_prefix="cell",
                max_evacuated_ratio=0.0,
            )
        )
        policy._emit_event = MagicMock()
        policy._notify_isolation_gate = lambda *a, **kw: None  # type: ignore[assignment]
        policy._notify_blast_radius = lambda *a, **kw: None  # type: ignore[assignment]
        policy._notify_restore_region = lambda *a, **kw: None  # type: ignore[assignment]

        reg = CellRegistry(
            settings=CellTopologySettings(
                enabled=True,
                evacuation_enabled=True,
                bulkhead_isolation_enabled=False,
                cell_count=4,
                cell_prefix="cell",
                max_evacuated_ratio=0.0,
            )
        )
        reg._emit_event = MagicMock()

        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=reg,
            ),
            patch("baldur.services.cell_topology.policy.logger") as mock_log,
        ):
            cell = reg.get_cell_info("cell-0")
            cell.metadata["evacuation_below_count"] = 99

            policy.evaluate("cell-0", 0.1)

        critical_calls = mock_log.critical.call_args_list
        event_names = [c[0][0] for c in critical_calls]
        assert "cell_evacuation.global_limit_reached" in event_names


# =============================================================================
# D. Dead code removal verification
# =============================================================================


class TestCellRegistryDeadCodeRemovalBehavior:
    """_subscribe_state_changes 제거 + _on_cell_state_event 재구현(388) 확인."""

    def test_subscribe_state_changes_method_does_not_exist(self):
        """CellRegistry._subscribe_state_changes는 제거되었다."""
        assert not hasattr(CellRegistry, "_subscribe_state_changes")

    def test_on_cell_state_event_method_exists(self):
        """CellRegistry._on_cell_state_event는 388에서 올바르게 재구현되었다."""
        assert hasattr(CellRegistry, "_on_cell_state_event")
