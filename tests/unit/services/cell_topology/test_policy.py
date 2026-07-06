"""
CellEvacuationPolicy 단위 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 대피 정책 설정 계약값 (임계치, 카운터 수, 비율)
- Behavior: 상태 머신 전이, 히스테리시스, 전역 제한, 수동 복구 동작 검증

참조 소스:
- services/cell_topology/policy.py (CellEvacuationPolicy, EvacuationRecord)
- services/cell_topology/models.py (CellState, CellInfo)
- services/cell_topology/registry.py (CellRegistry)
- settings/cell_topology.py (CellTopologySettings)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.cell_topology.models import CellInfo, CellState
from baldur.services.cell_topology.policy import (
    CellEvacuationPolicy,
    EvacuationRecord,
    get_cell_evacuation_policy,
    reset_cell_evacuation_policy,
)
from baldur.services.cell_topology.registry import (
    CellRegistry,
    reset_cell_registry,
)
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
def evacuation_settings() -> CellTopologySettings:
    """대피 기능이 활성화된 설정."""
    return CellTopologySettings(
        enabled=True,
        evacuation_enabled=True,
        bulkhead_isolation_enabled=False,
        cell_count=8,
        cell_prefix="cell",
    )


@pytest.fixture
def registry(evacuation_settings: CellTopologySettings) -> CellRegistry:
    """대피 기능이 활성화된 CellRegistry."""
    return CellRegistry(settings=evacuation_settings)


@pytest.fixture
def policy(evacuation_settings: CellTopologySettings) -> CellEvacuationPolicy:
    """대피 기능이 활성화된 CellEvacuationPolicy.

    Fire-and-forget 통보 메서드는 no-op으로 대체하여
    실제 Redis/Celery 연결 시도를 방지한다.
    """
    pol = CellEvacuationPolicy(settings=evacuation_settings)
    pol._notify_isolation_gate = lambda *a, **kw: None  # type: ignore[assignment]
    pol._notify_blast_radius = lambda *a, **kw: None  # type: ignore[assignment]
    pol._notify_restore_region = lambda *a, **kw: None  # type: ignore[assignment]
    return pol


# =============================================================================
# 계약 검증 (Contract Tests)
# =============================================================================


class TestEvacuationSettingsContract:
    """대피 정책 설정 계약값 검증."""

    def test_evacuation_health_threshold_contract(self):
        """대피 건강도 임계치 설계 계약값: 0.3."""
        settings = CellTopologySettings()
        assert settings.evacuation_health_threshold == 0.3

    def test_recovery_health_threshold_contract(self):
        """복구 건강도 임계치 설계 계약값: 0.7."""
        settings = CellTopologySettings()
        assert settings.recovery_health_threshold == 0.7

    def test_evacuation_consecutive_count_contract(self):
        """대피 연속 횟수 설계 계약값: 3."""
        settings = CellTopologySettings()
        assert settings.evacuation_consecutive_count == 3

    def test_recovery_consecutive_count_contract(self):
        """복구 연속 횟수 설계 계약값: 5."""
        settings = CellTopologySettings()
        assert settings.recovery_consecutive_count == 5

    def test_evacuation_traffic_drain_seconds_contract(self):
        """드레인 시간 설계 계약값: 30."""
        settings = CellTopologySettings()
        assert settings.evacuation_traffic_drain_seconds == 30

    def test_evacuation_drain_grace_seconds_contract(self):
        """NTP Drift Grace Buffer 설계 계약값: 2.0."""
        settings = CellTopologySettings()
        assert settings.evacuation_drain_grace_seconds == 2.0

    def test_max_evacuated_ratio_contract(self):
        """전역 격리 비율 하드 리미트 설계 계약값: 0.25."""
        settings = CellTopologySettings()
        assert settings.max_evacuated_ratio == 0.25

    def test_asymmetric_hysteresis_contract(self):
        """비대칭 히스테리시스: 대피(3) < 복구(5)."""
        settings = CellTopologySettings()
        assert (
            settings.evacuation_consecutive_count < settings.recovery_consecutive_count
        )


class TestEvacuationRecordContract:
    """EvacuationRecord 필드 계약 검증."""

    def test_record_has_required_fields(self):
        """EvacuationRecord는 cell_id, trigger_health_score 필수 필드를 갖는다."""
        record = EvacuationRecord(
            cell_id="cell-0",
            trigger_health_score=0.2,
        )
        assert record.cell_id == "cell-0"
        assert record.trigger_health_score == 0.2
        assert record.completed_at is None
        assert record.reason == ""
        assert record.affected_services == []


# =============================================================================
# 동작 검증 (Behavior Tests)
# =============================================================================


class TestEvacuationToggleBehavior:
    """대피 정책 토글 동작 검증."""

    def test_disabled_master_switch_returns_false(self, registry: CellRegistry):
        """마스터 스위치 비활성 시 evaluate()는 항상 False."""
        settings = CellTopologySettings(
            enabled=False,
            evacuation_enabled=True,
        )
        pol = CellEvacuationPolicy(settings=settings)
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            assert pol.evaluate("cell-0", 0.1) is False

    def test_disabled_evacuation_switch_returns_false(self, registry: CellRegistry):
        """대피 스위치 비활성 시 evaluate()는 항상 False."""
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=False,
        )
        pol = CellEvacuationPolicy(settings=settings)
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            assert pol.evaluate("cell-0", 0.1) is False

    def test_nonexistent_cell_returns_false(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """존재하지 않는 Cell ID에 대해 evaluate() False 반환."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            assert policy.evaluate("nonexistent-cell", 0.1) is False

    def test_warmup_cell_is_skipped(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """WARMUP 상태의 Cell은 대피 평가를 건너뛴다."""
        registry.set_cell_state("cell-0", CellState.WARMUP, "test")
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            assert policy.evaluate("cell-0", 0.05) is False


class TestActiveToDrawingHysteresisBehavior:
    """ACTIVE → DRAINING 히스테리시스 동작 검증."""

    def test_single_tick_below_threshold_does_not_transition(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """1회 임계치 이하로는 상태 전이가 발생하지 않는다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            result = policy.evaluate("cell-0", 0.2)
            assert result is False
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.ACTIVE

    def test_consecutive_below_count_increments(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """연속 임계치 이하 시 below_count가 증가한다."""
        threshold = policy._settings.evacuation_health_threshold
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            policy.evaluate("cell-0", threshold - 0.1)
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.metadata["evacuation_below_count"] == 1

            policy.evaluate("cell-0", threshold - 0.1)
            assert cell.metadata["evacuation_below_count"] == 2

    def test_above_threshold_resets_below_count(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """임계치를 초과하면 below_count가 0으로 리셋된다."""
        threshold = policy._settings.evacuation_health_threshold
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # 카운터 2까지 증가
            policy.evaluate("cell-0", threshold - 0.1)
            policy.evaluate("cell-0", threshold - 0.1)
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.metadata["evacuation_below_count"] == 2

            # 임계치 초과 → 리셋
            policy.evaluate("cell-0", threshold + 0.1)
            assert cell.metadata["evacuation_below_count"] == 0

    def test_consecutive_count_triggers_draining(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """연속 횟수 도달 시 DRAINING으로 전이한다."""
        threshold = policy._settings.evacuation_health_threshold
        required_count = policy._settings.evacuation_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            for _i in range(required_count - 1):
                result = policy.evaluate("cell-0", threshold - 0.1)
                assert result is False

            # 마지막 tick에서 전이 발생
            result = policy.evaluate("cell-0", threshold - 0.1)
            assert result is True

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.DRAINING

    def test_transition_resets_both_counters(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """ACTIVE → DRAINING 전이 시 양방향 카운터가 모두 리셋된다."""
        threshold = policy._settings.evacuation_health_threshold
        required_count = policy._settings.evacuation_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            for _ in range(required_count):
                policy.evaluate("cell-0", threshold - 0.1)

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.metadata["evacuation_below_count"] == 0
            assert cell.metadata["recovery_above_count"] == 0

    def test_evacuation_records_history(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """대피 발생 시 이력이 기록된다."""
        threshold = policy._settings.evacuation_health_threshold
        required_count = policy._settings.evacuation_consecutive_count
        low_score = threshold - 0.1

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            for _ in range(required_count):
                policy.evaluate("cell-0", low_score)

        history = policy.get_evacuation_history()
        assert len(history) == 1
        assert history[0].cell_id == "cell-0"
        assert history[0].trigger_health_score == low_score
        assert history[0].completed_at is None

    def test_boundary_at_threshold_triggers_below_count(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """health_score == threshold 일 때 below_count가 증가한다 (<= 조건)."""
        threshold = policy._settings.evacuation_health_threshold
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            policy.evaluate("cell-0", threshold)
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.metadata["evacuation_below_count"] == 1

    def test_interrupted_consecutive_does_not_trigger(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """연속 카운터 중간에 정상 tick이 끼면 카운터가 리셋되어 전이하지 않는다."""
        threshold = policy._settings.evacuation_health_threshold
        required_count = policy._settings.evacuation_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # 카운터 (required - 1)까지 증가
            for _ in range(required_count - 1):
                policy.evaluate("cell-0", threshold - 0.1)

            # 정상 tick → 리셋
            policy.evaluate("cell-0", threshold + 0.1)

            # 다시 (required - 1)회 → 전이 안 됨
            for _ in range(required_count - 1):
                result = policy.evaluate("cell-0", threshold - 0.1)
                assert result is False

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.ACTIVE


class TestGlobalEvacuationLimitBehavior:
    """전역 격리 비율 하드 리미트 동작 검증."""

    def test_refuses_evacuation_when_limit_reached(self, registry: CellRegistry):
        """max_evacuated_ratio 초과 시 추가 대피를 거부한다."""
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=True,
            bulkhead_isolation_enabled=False,
            cell_count=8,
            cell_prefix="cell",
            max_evacuated_ratio=0.25,
            evacuation_consecutive_count=1,
        )
        reg = CellRegistry(settings=settings)
        pol = CellEvacuationPolicy(settings=settings)

        # 2개 Cell을 ISOLATED로 변경 (8개 중 2개 = 25%)
        reg.set_cell_state("cell-0", CellState.ISOLATED, "test")
        reg.set_cell_state("cell-1", CellState.ISOLATED, "test")

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=reg,
        ):
            # 3번째 대피 시도는 거부되어야 함
            result = pol.evaluate("cell-2", 0.1)
            assert result is False
            cell = reg.get_cell_info("cell-2")
            assert cell is not None
            assert cell.state == CellState.ACTIVE

    def test_allows_evacuation_under_limit(self, registry: CellRegistry):
        """격리 비율이 제한 내이면 대피를 허용한다."""
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=True,
            bulkhead_isolation_enabled=False,
            cell_count=8,
            cell_prefix="cell",
            max_evacuated_ratio=0.25,
            evacuation_consecutive_count=1,
        )
        reg = CellRegistry(settings=settings)
        pol = CellEvacuationPolicy(settings=settings)

        # 1개만 ISOLATED (8개 중 1개 = 12.5% < 25%)
        reg.set_cell_state("cell-0", CellState.ISOLATED, "test")

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=reg,
        ):
            result = pol.evaluate("cell-1", 0.1)
            assert result is True
            cell = reg.get_cell_info("cell-1")
            assert cell is not None
            assert cell.state == CellState.DRAINING


class TestDrainingToIsolatedBehavior:
    """DRAINING → ISOLATED 드레인 시간 기반 전이 동작 검증."""

    def _setup_draining_cell(
        self,
        registry: CellRegistry,
        cell_id: str = "cell-0",
    ) -> CellInfo:
        """Cell을 DRAINING 상태로 설정하고 metadata를 올바르게 초기화."""
        registry.set_cell_state(cell_id, CellState.DRAINING, "test drain")
        cell = registry.get_cell_info(cell_id)
        assert cell is not None
        return cell

    def test_first_tick_records_time(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """DRAINING 첫 tick에서 시간이 기록되고 전이하지 않는다."""
        cell = self._setup_draining_cell(registry)

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            result = policy.evaluate("cell-0", 0.2)
            assert result is False
            assert "last_state_change_time" in cell.metadata

    def test_insufficient_time_does_not_transition(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """드레인 시간이 충분하지 않으면 전이하지 않는다."""
        cell = self._setup_draining_cell(registry)
        # 시간을 5초 전으로 설정 (30+2=32초 필요)
        cell.metadata["last_state_change_time"] = time.time() - 5

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            result = policy.evaluate("cell-0", 0.2)
            assert result is False
            assert cell.state == CellState.DRAINING

    def test_sufficient_time_transitions_to_isolated(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """드레인 시간이 충분하면 ISOLATED로 전이한다."""
        cell = self._setup_draining_cell(registry)
        drain_required = (
            policy._settings.evacuation_traffic_drain_seconds
            + policy._settings.evacuation_drain_grace_seconds
        )
        cell.metadata["last_state_change_time"] = time.time() - (drain_required + 1)

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            result = policy.evaluate("cell-0", 0.2)
            assert result is True
            assert cell.state == CellState.ISOLATED

    def test_draining_transition_resets_counters(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """DRAINING → ISOLATED 전이 시 양방향 카운터가 리셋된다."""
        cell = self._setup_draining_cell(registry)
        drain_required = (
            policy._settings.evacuation_traffic_drain_seconds
            + policy._settings.evacuation_drain_grace_seconds
        )
        cell.metadata["last_state_change_time"] = time.time() - (drain_required + 1)
        cell.metadata["evacuation_below_count"] = 5
        cell.metadata["recovery_above_count"] = 3

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            policy.evaluate("cell-0", 0.2)
            assert cell.metadata["evacuation_below_count"] == 0
            assert cell.metadata["recovery_above_count"] == 0

    def test_metadata_mismatch_skips_transition(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """metadata의 last_state_change.to가 불일치하면 전이를 건너뛴다."""
        registry.set_cell_state("cell-0", CellState.DRAINING, "test")
        cell = registry.get_cell_info("cell-0")
        assert cell is not None
        # metadata를 의도적으로 불일치하게 설정
        cell.metadata["last_state_change"] = {"to": "active", "from": "draining"}

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            result = policy.evaluate("cell-0", 0.2)
            assert result is False

    def test_fire_and_forget_notifications_called(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """DRAINING → ISOLATED 전이 시 Fire-and-forget 통보가 호출된다."""
        cell = self._setup_draining_cell(registry)
        drain_required = (
            policy._settings.evacuation_traffic_drain_seconds
            + policy._settings.evacuation_drain_grace_seconds
        )
        cell.metadata["last_state_change_time"] = time.time() - (drain_required + 1)

        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=registry,
            ),
            patch.object(policy, "_notify_isolation_gate") as mock_gate,
            patch.object(policy, "_notify_blast_radius") as mock_blast,
        ):
            policy.evaluate("cell-0", 0.2)
            mock_gate.assert_called_once()
            mock_blast.assert_called_once()


class TestIsolatedToActiveHysteresisBehavior:
    """ISOLATED → ACTIVE 히스테리시스 복구 동작 검증."""

    def _setup_isolated_cell(
        self,
        registry: CellRegistry,
        cell_id: str = "cell-0",
    ) -> CellInfo:
        """Cell을 ISOLATED 상태로 설정."""
        registry.set_cell_state(cell_id, CellState.ISOLATED, "test isolated")
        cell = registry.get_cell_info(cell_id)
        assert cell is not None
        return cell

    def test_single_tick_above_threshold_does_not_recover(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """1회 임계치 이상으로는 복구되지 않는다."""
        self._setup_isolated_cell(registry)

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            result = policy.evaluate("cell-0", 0.9)
            assert result is False
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.ISOLATED

    def test_consecutive_above_count_increments(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """연속 임계치 이상 시 above_count가 증가한다."""
        self._setup_isolated_cell(registry)
        recovery_threshold = policy._settings.recovery_health_threshold

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            policy.evaluate("cell-0", recovery_threshold + 0.1)
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.metadata["recovery_above_count"] == 1

            policy.evaluate("cell-0", recovery_threshold + 0.1)
            assert cell.metadata["recovery_above_count"] == 2

    def test_below_recovery_threshold_resets_above_count(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """복구 임계치 미달 시 above_count가 0으로 리셋된다."""
        self._setup_isolated_cell(registry)
        recovery_threshold = policy._settings.recovery_health_threshold

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # 카운터 2까지 증가
            policy.evaluate("cell-0", recovery_threshold + 0.1)
            policy.evaluate("cell-0", recovery_threshold + 0.1)
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.metadata["recovery_above_count"] == 2

            # 미달 → 리셋
            policy.evaluate("cell-0", recovery_threshold - 0.1)
            assert cell.metadata["recovery_above_count"] == 0

    def test_consecutive_count_triggers_recovery(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """연속 횟수 도달 시 ACTIVE로 복구한다."""
        self._setup_isolated_cell(registry)
        recovery_threshold = policy._settings.recovery_health_threshold
        required_count = policy._settings.recovery_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            for _i in range(required_count - 1):
                result = policy.evaluate("cell-0", recovery_threshold + 0.1)
                assert result is False

            # 마지막 tick에서 복구 발생
            result = policy.evaluate("cell-0", recovery_threshold + 0.1)
            assert result is True

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.ACTIVE

    def test_recovery_resets_both_counters(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """ISOLATED → ACTIVE 전이 시 양방향 카운터가 리셋된다."""
        self._setup_isolated_cell(registry)
        recovery_threshold = policy._settings.recovery_health_threshold
        required_count = policy._settings.recovery_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            for _ in range(required_count):
                policy.evaluate("cell-0", recovery_threshold + 0.1)

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.metadata["evacuation_below_count"] == 0
            assert cell.metadata["recovery_above_count"] == 0

    def test_recovery_marks_history_complete(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """복구 시 대피 이력의 completed_at이 기록된다."""
        threshold = policy._settings.evacuation_health_threshold
        evac_count = policy._settings.evacuation_consecutive_count
        recovery_threshold = policy._settings.recovery_health_threshold
        recovery_count = policy._settings.recovery_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # ACTIVE → DRAINING
            for _ in range(evac_count):
                policy.evaluate("cell-0", threshold - 0.1)

            # DRAINING → ISOLATED (시간 경과 시뮬레이션)
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            drain_required = (
                policy._settings.evacuation_traffic_drain_seconds
                + policy._settings.evacuation_drain_grace_seconds
            )
            cell.metadata["last_state_change_time"] = time.time() - (drain_required + 1)
            policy.evaluate("cell-0", 0.1)

            assert cell.state == CellState.ISOLATED

            # ISOLATED → ACTIVE
            for _ in range(recovery_count):
                policy.evaluate("cell-0", recovery_threshold + 0.1)

        history = policy.get_evacuation_history()
        assert len(history) == 1
        assert history[0].completed_at is not None

    def test_boundary_at_recovery_threshold_triggers_above_count(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """health_score == recovery_threshold 일 때 above_count가 증가한다 (>= 조건)."""
        self._setup_isolated_cell(registry)
        recovery_threshold = policy._settings.recovery_health_threshold

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            policy.evaluate("cell-0", recovery_threshold)
            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.metadata["recovery_above_count"] == 1

    def test_restore_notification_called_on_recovery(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """ISOLATED → ACTIVE 복구 시 복구 통보가 호출된다."""
        self._setup_isolated_cell(registry)
        recovery_threshold = policy._settings.recovery_health_threshold
        required_count = policy._settings.recovery_consecutive_count

        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=registry,
            ),
            patch.object(policy, "_notify_restore_region") as mock_restore,
        ):
            for _ in range(required_count):
                policy.evaluate("cell-0", recovery_threshold + 0.1)

            mock_restore.assert_called_once_with("cell-0")


class TestManualRestoreBehavior:
    """수동 복구 동작 검증."""

    def test_manual_restore_from_isolated(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """ISOLATED 상태의 Cell을 수동으로 ACTIVE로 복구한다."""
        registry.set_cell_state("cell-0", CellState.ISOLATED, "test")

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            result = policy.restore_cell("cell-0")
            assert result is True

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.ACTIVE

    def test_manual_restore_from_draining(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """DRAINING 상태의 Cell을 수동으로 ACTIVE로 복구한다."""
        registry.set_cell_state("cell-0", CellState.DRAINING, "test")

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            result = policy.restore_cell("cell-0")
            assert result is True

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.ACTIVE

    def test_manual_restore_resets_counters(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """수동 복구 시 양방향 카운터가 리셋된다."""
        registry.set_cell_state("cell-0", CellState.ISOLATED, "test")
        cell = registry.get_cell_info("cell-0")
        assert cell is not None
        cell.metadata["evacuation_below_count"] = 5
        cell.metadata["recovery_above_count"] = 3

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            policy.restore_cell("cell-0")
            assert cell.metadata["evacuation_below_count"] == 0
            assert cell.metadata["recovery_above_count"] == 0

    def test_manual_restore_nonexistent_cell(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """존재하지 않는 Cell 수동 복구는 False를 반환한다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            assert policy.restore_cell("nonexistent") is False

    def test_manual_restore_disabled_returns_false(self):
        """마스터 스위치 비활성 시 수동 복구도 False."""
        settings = CellTopologySettings(enabled=False)
        pol = CellEvacuationPolicy(settings=settings)
        assert pol.restore_cell("cell-0") is False

    def test_restore_notification_called(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """수동 복구 시 복구 통보가 호출된다."""
        registry.set_cell_state("cell-0", CellState.ISOLATED, "test")

        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=registry,
            ),
            patch.object(policy, "_notify_restore_region") as mock_restore,
        ):
            policy.restore_cell("cell-0")
            mock_restore.assert_called_once_with("cell-0")


class TestFireAndForgetNotificationBehavior:
    """Fire-and-forget 통보 동작 검증.

    이 클래스의 테스트는 실제 통보 메서드 동작을 검증하므로
    lambda 오버라이드가 없는 원본 policy를 사용한다.
    """

    @pytest.fixture
    def raw_policy(
        self, evacuation_settings: CellTopologySettings
    ) -> CellEvacuationPolicy:
        """통보 메서드가 원본인 CellEvacuationPolicy."""
        return CellEvacuationPolicy(settings=evacuation_settings)

    def test_isolation_gate_async_preferred(self, raw_policy: CellEvacuationPolicy):
        """Celery 환경에서 apply_async가 호출된다."""
        mock_task = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "baldur.adapters.celery.tasks": MagicMock(
                    notify_cell_isolation=mock_task
                ),
            },
        ):
            raw_policy._notify_isolation_gate("cell-0", "test reason")
            mock_task.apply_async.assert_called_once()

    def test_isolation_gate_sync_fallback_on_import_error(
        self, raw_policy: CellEvacuationPolicy
    ):
        """ImportError 시 동기 폴백이 호출된다."""
        with patch.object(raw_policy, "_notify_isolation_gate_sync") as mock_sync:
            with patch.dict("sys.modules", {"baldur.adapters.celery.tasks": None}):
                raw_policy._notify_isolation_gate("cell-0", "test reason")
                mock_sync.assert_called_once_with(
                    "cell-0",
                    "test reason",
                    duration_seconds=raw_policy._settings.isolation_notification_duration_seconds,
                )

    def test_blast_radius_sync_fallback_on_import_error(
        self, raw_policy: CellEvacuationPolicy
    ):
        """ImportError 시 blast radius 동기 폴백이 호출된다."""
        with patch.object(raw_policy, "_notify_blast_radius_sync") as mock_sync:
            with patch.dict("sys.modules", {"baldur.adapters.celery.tasks": None}):
                raw_policy._notify_blast_radius("cell-0", ["svc-1"])
                mock_sync.assert_called_once_with("cell-0", ["svc-1"])

    def test_restore_sync_fallback_on_import_error(
        self, raw_policy: CellEvacuationPolicy
    ):
        """ImportError 시 restore 동기 폴백이 호출된다."""
        with patch.object(raw_policy, "_notify_restore_region_sync") as mock_sync:
            with patch.dict("sys.modules", {"baldur.adapters.celery.tasks": None}):
                raw_policy._notify_restore_region("cell-0")
                mock_sync.assert_called_once_with("cell-0")


class TestSingletonBehavior:
    """싱글톤 동작 검증."""

    def test_get_returns_same_instance(self):
        """get_cell_evacuation_policy()는 동일 인스턴스를 반환한다."""
        p1 = get_cell_evacuation_policy()
        p2 = get_cell_evacuation_policy()
        assert p1 is p2

    def test_reset_clears_singleton(self):
        """reset 후 새로운 인스턴스가 생성된다."""
        p1 = get_cell_evacuation_policy()
        reset_cell_evacuation_policy()
        p2 = get_cell_evacuation_policy()
        assert p1 is not p2


class TestFullLifecycleBehavior:
    """전체 대피 라이프사이클 동작 검증."""

    def test_active_to_draining_to_isolated_to_active(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """ACTIVE → DRAINING → ISOLATED → ACTIVE 전체 흐름."""
        threshold = policy._settings.evacuation_health_threshold
        evac_count = policy._settings.evacuation_consecutive_count
        recovery_threshold = policy._settings.recovery_health_threshold
        recovery_count = policy._settings.recovery_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # Step 1: ACTIVE → DRAINING
            for _ in range(evac_count):
                policy.evaluate("cell-0", threshold - 0.1)

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.DRAINING

            # Step 2: DRAINING → ISOLATED
            drain_required = (
                policy._settings.evacuation_traffic_drain_seconds
                + policy._settings.evacuation_drain_grace_seconds
            )
            cell.metadata["last_state_change_time"] = time.time() - (drain_required + 1)
            policy.evaluate("cell-0", 0.1)
            assert cell.state == CellState.ISOLATED

            # Step 3: ISOLATED → ACTIVE
            for _ in range(recovery_count):
                policy.evaluate("cell-0", recovery_threshold + 0.1)

            assert cell.state == CellState.ACTIVE

    def test_multiple_cells_independent_evaluation(
        self, policy: CellEvacuationPolicy, registry: CellRegistry
    ):
        """각 Cell의 대피 평가는 독립적이다."""
        threshold = policy._settings.evacuation_health_threshold
        evac_count = policy._settings.evacuation_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            # cell-0만 카운터 올리기
            for _ in range(evac_count):
                policy.evaluate("cell-0", threshold - 0.1)

            cell_0 = registry.get_cell_info("cell-0")
            cell_1 = registry.get_cell_info("cell-1")
            assert cell_0 is not None
            assert cell_1 is not None
            assert cell_0.state == CellState.DRAINING
            assert cell_1.state == CellState.ACTIVE


# =============================================================================
# 리팩토링 검증 — 설정 계약값 + 동작 검증
# =============================================================================


class TestRefactoredSettingsContract:
    """리팩토링으로 추가된 설정 필드 계약값 검증."""

    def test_isolation_notification_duration_seconds_contract(self):
        """격리 통보 지속 시간 설계 계약값: 3600."""
        settings = CellTopologySettings()
        assert settings.isolation_notification_duration_seconds == 3600

    def test_evacuation_history_max_size_contract(self):
        """대피 이력 최대 보관 건수 설계 계약값: 1000."""
        settings = CellTopologySettings()
        assert settings.evacuation_history_max_size == 1000


class TestDrainingTimestampRecordingBehavior:
    """ACTIVE → DRAINING 전환 시 last_state_change_time 즉시 기록 검증."""

    def test_draining_transition_records_timestamp_immediately(
        self,
        policy: CellEvacuationPolicy,
        registry: CellRegistry,
    ):
        """DRAINING 전환 시 last_state_change_time이 즉시 기록된다."""
        threshold = policy._settings.evacuation_health_threshold
        required_count = policy._settings.evacuation_consecutive_count

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            before = time.time()
            for _ in range(required_count):
                policy.evaluate("cell-0", threshold - 0.1)
            after = time.time()

            cell = registry.get_cell_info("cell-0")
            assert cell is not None
            assert cell.state == CellState.DRAINING
            recorded = cell.metadata.get("last_state_change_time")
            assert recorded is not None
            assert before <= recorded <= after

    def test_draining_uses_prerecorded_timestamp_for_elapsed(
        self,
        policy: CellEvacuationPolicy,
        registry: CellRegistry,
    ):
        """DRAINING 전환 시 기록된 타임스탬프가 드레인 경과 계산에 사용된다.

        _tick_draining()의 첫 tick에서 시간을 새로 기록하는 것이 아니라
        _tick_active()에서 이미 기록된 시간이 그대로 유지된다.
        """
        threshold = policy._settings.evacuation_health_threshold
        required_count = policy._settings.evacuation_consecutive_count
        drain_required = (
            policy._settings.evacuation_traffic_drain_seconds
            + policy._settings.evacuation_drain_grace_seconds
        )

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=registry,
        ):
            for _ in range(required_count):
                policy.evaluate("cell-0", threshold - 0.1)

            cell = registry.get_cell_info("cell-0")
            assert cell is not None

            # 시간을 충분히 경과한 것으로 직접 설정
            fake_past_time = time.time() - (drain_required + 1)
            cell.metadata["last_state_change_time"] = fake_past_time

            result = policy.evaluate("cell-0", 0.2)
            assert result is True
            assert cell.state == CellState.ISOLATED

            # ISOLATED 전이 시 새 타임스탬프가 기록됨을 확인
            assert cell.metadata.get("last_state_change_time") != fake_past_time


class TestEvacuationHistoryBoundedBehavior:
    """대피 이력 deque maxlen 경계 동작 검증."""

    def test_history_bounded_by_max_size(self):
        """대피 이력은 evacuation_history_max_size를 초과하지 않는다."""
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=True,
            evacuation_history_max_size=10,
        )
        pol = CellEvacuationPolicy(settings=settings)

        for i in range(15):
            pol._evacuation_history.append(
                EvacuationRecord(
                    cell_id=f"cell-{i}",
                    trigger_health_score=0.2,
                    reason="test",
                ),
            )

        assert len(pol.get_evacuation_history()) == settings.evacuation_history_max_size

    def test_oldest_records_evicted_first(self):
        """maxlen 초과 시 가장 오래된 레코드가 먼저 제거된다."""
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=True,
            evacuation_history_max_size=10,
        )
        pol = CellEvacuationPolicy(settings=settings)

        for i in range(15):
            pol._evacuation_history.append(
                EvacuationRecord(
                    cell_id=f"cell-{i}",
                    trigger_health_score=0.2,
                    reason="test",
                ),
            )

        history = pol.get_evacuation_history()
        assert len(history) == 10
        # 가장 오래된 5개(cell-0~cell-4)가 제거되고 cell-5부터 남아야 한다
        assert history[0].cell_id == "cell-5"
        assert history[-1].cell_id == "cell-14"


class TestIsolationNotificationDurationBehavior:
    """격리 통보 duration_seconds 설정 참조 동작 검증."""

    def test_isolation_gate_receives_configured_duration(self, registry: CellRegistry):
        """격리 통보 시 설정의 isolation_notification_duration_seconds가 전달된다."""
        custom_duration = 7200
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=True,
            isolation_notification_duration_seconds=custom_duration,
        )
        pol = CellEvacuationPolicy(settings=settings)

        mock_task = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "baldur.adapters.celery.tasks": MagicMock(
                    notify_cell_isolation=mock_task,
                ),
            },
        ):
            pol._notify_isolation_gate(
                "cell-0",
                "test reason",
                duration_seconds=custom_duration,
            )
            call_kwargs = mock_task.apply_async.call_args[1]["kwargs"]
            assert call_kwargs["duration_seconds"] == custom_duration

    def test_draining_to_isolated_passes_settings_duration(
        self, registry: CellRegistry
    ):
        """DRAINING → ISOLATED 전환 시 설정의 duration이 통보에 전달된다."""
        custom_duration = 1800
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=True,
            bulkhead_isolation_enabled=False,
            cell_count=8,
            cell_prefix="cell",
            isolation_notification_duration_seconds=custom_duration,
        )
        reg = CellRegistry(settings=settings)
        pol = CellEvacuationPolicy(settings=settings)

        # DRAINING 상태로 사전 설정
        reg.set_cell_state("cell-0", CellState.DRAINING, "test drain")
        cell = reg.get_cell_info("cell-0")
        assert cell is not None
        drain_required = (
            settings.evacuation_traffic_drain_seconds
            + settings.evacuation_drain_grace_seconds
        )
        cell.metadata["last_state_change_time"] = time.time() - (drain_required + 1)

        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=reg,
            ),
            patch.object(pol, "_notify_isolation_gate") as mock_gate,
            patch.object(pol, "_notify_blast_radius"),
        ):
            pol.evaluate("cell-0", 0.2)
            mock_gate.assert_called_once()
            call_kwargs = mock_gate.call_args[1]
            assert call_kwargs["duration_seconds"] == custom_duration
