"""
CellRegistry 동작 검증 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: Hash Ring 구성 계약 (vnode 수, cell 수)
- Behavior: Consistent Hash 할당, 상태 변경, 동적 스케일링 동작 검증

참조 소스:
- services/cell_topology/registry.py (CellRegistry, VNODES_PER_CELL)
- services/cell_topology/models.py (CellState, CellInfo, CELL_STATE_PRIORITY)
- settings/cell_topology.py (CellTopologySettings)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.services.cell_topology.models import CellState
from baldur.services.cell_topology.registry import (
    VNODES_PER_CELL,
    CellRegistry,
    get_cell_registry,
    reset_cell_registry,
)
from baldur.settings.cell_topology import (
    CellTopologySettings,
    reset_cell_topology_settings,
)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """각 테스트 전후 싱글톤 리셋."""
    reset_cell_registry()
    reset_cell_topology_settings()
    yield
    reset_cell_registry()
    reset_cell_topology_settings()


@pytest.fixture
def enabled_settings() -> CellTopologySettings:
    """enabled=True인 CellTopologySettings."""
    return CellTopologySettings(
        enabled=True,
        bulkhead_isolation_enabled=False,
        cell_count=8,
        cell_prefix="cell",
    )


@pytest.fixture
def registry(enabled_settings: CellTopologySettings) -> CellRegistry:
    """enabled=True인 CellRegistry."""
    return CellRegistry(settings=enabled_settings)


@pytest.fixture
def small_registry() -> CellRegistry:
    """Cell 수가 적은 CellRegistry (테스트 편의)."""
    settings = CellTopologySettings(
        enabled=True,
        bulkhead_isolation_enabled=False,
        cell_count=3,
        cell_prefix="cell",
    )
    return CellRegistry(settings=settings)


class TestCellRegistryInitContract:
    """CellRegistry 초기화 계약 검증."""

    def test_default_cell_count(self, registry: CellRegistry):
        """기본 8개 Cell이 생성되어야 한다."""
        assert len(registry.get_all_cells()) == 8

    def test_cell_naming_convention(self, registry: CellRegistry):
        """Cell 이름이 '{prefix}-{index}' 형식이어야 한다."""
        cells = registry.get_all_cells()
        for i in range(8):
            assert f"cell-{i}" in cells

    def test_all_cells_start_active(self, registry: CellRegistry):
        """초기화 시 모든 Cell이 ACTIVE 상태여야 한다."""
        for cell_info in registry.get_all_cells().values():
            assert cell_info.state == CellState.ACTIVE

    def test_hash_ring_size(self, registry: CellRegistry):
        """Hash Ring 크기: cell_count × VNODES_PER_CELL."""
        expected = 8 * VNODES_PER_CELL
        assert len(registry._hash_ring) == expected

    def test_hash_ring_is_sorted(self, registry: CellRegistry):
        """Hash Ring은 해시값 기준으로 정렬되어야 한다."""
        ring = registry._hash_ring
        for i in range(len(ring) - 1):
            assert ring[i][0] <= ring[i + 1][0]

    def test_vnodes_per_cell_contract_150(self):
        """Cell당 가상 노드 수: 150."""
        assert VNODES_PER_CELL == 150


class TestCellRegistryHashBehavior:
    """Consistent Hash 할당 동작 검증."""

    def test_same_key_returns_same_cell(self, registry: CellRegistry):
        """동일한 키는 항상 동일한 Cell에 할당되어야 한다 (결정적)."""
        cell1 = registry.get_cell_for_key("user-12345")
        cell2 = registry.get_cell_for_key("user-12345")
        assert cell1 == cell2

    def test_different_keys_can_differ(self, registry: CellRegistry):
        """다른 키는 다른 Cell에 할당될 수 있다."""
        cells = set()
        for i in range(100):
            cells.add(registry.get_cell_for_key(f"service-{i}"))
        # 100개 키를 8개 Cell에 할당하면 최소 2개 이상 Cell 사용
        assert len(cells) >= 2

    def test_disabled_returns_default_cell(self):
        """비활성 시 기본 Cell(prefix-0)을 반환해야 한다."""
        settings = CellTopologySettings(enabled=False, cell_prefix="cell")
        reg = CellRegistry(settings=settings)
        assert reg.get_cell_for_key("any-key") == "cell-0"

    def test_returns_valid_cell_id(self, registry: CellRegistry):
        """반환된 cell_id가 유효한 Cell이어야 한다."""
        cell_id = registry.get_cell_for_key("test-service")
        assert registry.get_cell_info(cell_id) is not None

    def test_draining_cell_is_skipped(self, small_registry: CellRegistry):
        """DRAINING Cell은 건너뛰고 다음 ACTIVE Cell을 반환해야 한다."""
        # 모든 key에 대해 DRAINING Cell이 반환되지 않는지 확인
        reg = small_registry
        # cell-1을 DRAINING으로 변경
        reg.set_cell_state("cell-1", CellState.DRAINING, reason="test")

        # 여러 키를 시도하여 DRAINING Cell 반환 여부 확인
        for i in range(50):
            cell_id = reg.get_cell_for_key(f"key-{i}")
            assert cell_id != "cell-1", f"DRAINING cell-1이 key-{i}에 할당됨"

    def test_isolated_cell_is_skipped(self, small_registry: CellRegistry):
        """ISOLATED Cell은 건너뛰고 다음 ACTIVE Cell을 반환해야 한다."""
        reg = small_registry
        reg.set_cell_state("cell-0", CellState.ISOLATED, reason="test")

        for i in range(50):
            cell_id = reg.get_cell_for_key(f"key-{i}")
            assert cell_id != "cell-0", f"ISOLATED cell-0이 key-{i}에 할당됨"

    def test_all_cells_draining_returns_fallback(self, small_registry: CellRegistry):
        """모든 Cell이 비활성이면 최후의 수단으로 어떤 Cell이든 반환해야 한다."""
        reg = small_registry
        for cell_id in reg.get_all_cells():
            reg.set_cell_state(cell_id, CellState.DRAINING, reason="test")

        # 에러 없이 반환되어야 함
        result = reg.get_cell_for_key("any-key")
        assert result.startswith("cell-")


class TestCellRegistryWarmupBehavior:
    """WARMUP Cell 확률적 라우팅 동작 검증."""

    def test_warmup_zero_percent_is_never_selected(self, small_registry: CellRegistry):
        """warmup_percentage=0인 WARMUP Cell은 선택되지 않아야 한다."""
        reg = small_registry
        # 나머지 Cell을 DRAINING으로 변경하되, cell-0만 WARMUP(0%)
        reg.set_cell_state("cell-0", CellState.WARMUP, reason="test")
        cell_info = reg.get_cell_info("cell-0")
        assert cell_info is not None
        cell_info.warmup_percentage = 0.0

        # cell-1, cell-2는 ACTIVE로 유지 → WARMUP(0%)인 cell-0 대신 선택
        results = set()
        for i in range(50):
            results.add(reg.get_cell_for_key(f"test-{i}"))
        # warmup 0%인 cell-0은 선택되지 않아야 함
        assert "cell-0" not in results

    def test_warmup_100_percent_behaves_like_subset_active(
        self, small_registry: CellRegistry
    ):
        """warmup_percentage=100인 WARMUP Cell은 항상 선택 가능해야 한다."""
        reg = small_registry
        reg.set_cell_state("cell-2", CellState.WARMUP, reason="test")
        cell_info = reg.get_cell_info("cell-2")
        assert cell_info is not None
        cell_info.warmup_percentage = 100.0

        # cell-2가 적어도 한 번은 선택되어야 함 (100% 투입)
        selected_cells = set()
        for i in range(200):
            selected_cells.add(reg.get_cell_for_key(f"warmup-key-{i}"))
        assert "cell-2" in selected_cells


class TestCellRegistryStateBehavior:
    """Cell 상태 관리 동작 검증."""

    def test_set_cell_state_changes_state(self, registry: CellRegistry):
        """set_cell_state로 상태를 변경할 수 있어야 한다."""
        result = registry.set_cell_state(
            "cell-0", CellState.DRAINING, reason="maintenance"
        )
        assert result is True
        info = registry.get_cell_info("cell-0")
        assert info is not None
        assert info.state == CellState.DRAINING

    def test_set_cell_state_records_metadata(self, registry: CellRegistry):
        """상태 변경 시 metadata에 변경 이력이 기록되어야 한다."""
        registry.set_cell_state("cell-3", CellState.ISOLATED, reason="incident")
        info = registry.get_cell_info("cell-3")
        assert info is not None
        change = info.metadata.get("last_state_change")
        assert change is not None
        assert change["from"] == "active"
        assert change["to"] == "isolated"
        assert change["reason"] == "incident"

    def test_set_cell_state_nonexistent_returns_false(self, registry: CellRegistry):
        """존재하지 않는 Cell에 대해 False를 반환해야 한다."""
        result = registry.set_cell_state("cell-999", CellState.ACTIVE, reason="test")
        assert result is False

    def test_get_active_cells_excludes_non_active(self, registry: CellRegistry):
        """get_active_cells는 ACTIVE 상태만 반환해야 한다."""
        total_cells = len(registry.get_all_cells())
        registry.set_cell_state("cell-0", CellState.DRAINING, reason="test")
        registry.set_cell_state("cell-1", CellState.ISOLATED, reason="test")
        excluded_count = 2
        active = registry.get_active_cells()
        assert "cell-0" not in active
        assert "cell-1" not in active
        assert len(active) == total_cells - excluded_count

    def test_update_health_score_clamps_0_to_1(self, registry: CellRegistry):
        """건강도는 0.0~1.0 범위로 클램프되어야 한다."""
        registry.update_health_score("cell-0", 1.5)
        info = registry.get_cell_info("cell-0")
        assert info is not None
        assert info.health_score == 1.0

        registry.update_health_score("cell-0", -0.3)
        assert info.health_score == 0.0

    def test_update_health_score_stores_value(self, registry: CellRegistry):
        """건강도가 정확히 저장되어야 한다."""
        registry.update_health_score("cell-5", 0.75)
        info = registry.get_cell_info("cell-5")
        assert info is not None
        assert info.health_score == pytest.approx(0.75)

    def test_get_cell_info_nonexistent(self, registry: CellRegistry):
        """존재하지 않는 Cell 조회 시 None을 반환해야 한다."""
        assert registry.get_cell_info("cell-999") is None


class TestCellRegistryServiceAssignBehavior:
    """서비스 할당 동작 검증."""

    def test_assign_service_adds_to_assigned_services(self, registry: CellRegistry):
        """assign_service로 서비스가 할당되어야 한다."""
        with patch(
            "baldur.services.cell_topology.registry.CellRegistry"
            "._record_service_heartbeat"
        ):
            cell_id = registry.assign_service("user-service")
            info = registry.get_cell_info(cell_id)
            assert info is not None
            assert "user-service" in info.assigned_services

    def test_assign_service_returns_consistent_cell(self, registry: CellRegistry):
        """같은 서비스명은 항상 같은 Cell에 할당되어야 한다."""
        with patch(
            "baldur.services.cell_topology.registry.CellRegistry"
            "._record_service_heartbeat"
        ):
            cell1 = registry.assign_service("payment-service")
            cell2 = registry.assign_service("payment-service")
            assert cell1 == cell2

    def test_assign_service_idempotent(self, registry: CellRegistry):
        """같은 서비스를 반복 할당해도 한 번만 등록되어야 한다."""
        with patch(
            "baldur.services.cell_topology.registry.CellRegistry"
            "._record_service_heartbeat"
        ):
            cell_id = registry.assign_service("order-service")
            registry.assign_service("order-service")
            info = registry.get_cell_info(cell_id)
            assert info is not None
            assert (
                info.assigned_services.count("order-service")
                if hasattr(info.assigned_services, "count")
                else list(info.assigned_services).count("order-service") == 1
            )


class TestCellRegistryDynamicScalingBehavior:
    """동적 스케일링 동작 검증."""

    def test_add_cells_creates_warmup_cells(self, registry: CellRegistry):
        """add_cells로 WARMUP 상태의 새 Cell이 추가되어야 한다."""
        with patch.object(registry, "_sync_state_to_redis"):
            added = registry.add_cells(2)
        assert len(added) == 2
        assert "cell-8" in added
        assert "cell-9" in added

        for cell_id in added:
            info = registry.get_cell_info(cell_id)
            assert info is not None
            assert info.state == CellState.WARMUP

    def test_add_cells_sets_initial_warmup_percentage(self, registry: CellRegistry):
        """새 Cell의 warmup_percentage가 설정값과 일치해야 한다."""
        with patch.object(registry, "_sync_state_to_redis"):
            added = registry.add_cells(1)
        info = registry.get_cell_info(added[0])
        assert info is not None
        expected = registry._settings.warmup_initial_percentage
        assert info.warmup_percentage == pytest.approx(expected)

    def test_add_cells_rebuilds_hash_ring(self, registry: CellRegistry):
        """Cell 추가 후 Hash Ring이 리빌딩되어야 한다."""
        old_ring_size = len(registry._hash_ring)
        with patch.object(registry, "_sync_state_to_redis"):
            registry.add_cells(1)
        new_ring_size = len(registry._hash_ring)
        assert new_ring_size == old_ring_size + VNODES_PER_CELL

    def test_add_cells_increases_total_count(self, registry: CellRegistry):
        """Cell 추가 후 전체 Cell 수가 증가해야 한다."""
        initial_count = len(registry.get_all_cells())
        with patch.object(registry, "_sync_state_to_redis"):
            registry.add_cells(3)
        assert len(registry.get_all_cells()) == initial_count + 3

    def test_remove_cells_transitions_to_draining(self, registry: CellRegistry):
        """remove_cells로 Cell이 DRAINING 상태로 전환되어야 한다."""
        with patch.object(registry, "_sync_state_to_redis"):
            drained = registry.remove_cells(["cell-6", "cell-7"])
        assert "cell-6" in drained
        assert "cell-7" in drained

        info6 = registry.get_cell_info("cell-6")
        info7 = registry.get_cell_info("cell-7")
        assert info6 is not None
        assert info6.state == CellState.DRAINING
        assert info7 is not None
        assert info7.state == CellState.DRAINING

    def test_remove_nonexistent_cell_returns_empty(self, registry: CellRegistry):
        """존재하지 않는 Cell 제거 시 빈 리스트를 반환해야 한다."""
        with patch.object(registry, "_sync_state_to_redis"):
            drained = registry.remove_cells(["cell-999"])
        assert drained == []


class TestCellRegistrySingletonBehavior:
    """싱글톤 동작 검증."""

    def test_singleton_returns_same_instance(self):
        """get_cell_registry는 동일 인스턴스를 반환해야 한다."""
        r1 = get_cell_registry()
        r2 = get_cell_registry()
        assert r1 is r2

    def test_reset_clears_singleton(self):
        """reset 후 새 인스턴스가 생성되어야 한다."""
        r1 = get_cell_registry()
        reset_cell_registry()
        r2 = get_cell_registry()
        assert r1 is not r2


class TestCellRegistryHashDistributionBehavior:
    """Hash 분배 균일성 동작 검증."""

    def test_hash_is_deterministic(self):
        """동일 키의 해시값은 항상 동일해야 한다."""
        h1 = CellRegistry._hash("test-key")
        h2 = CellRegistry._hash("test-key")
        assert h1 == h2

    def test_hash_is_positive_integer(self):
        """해시값은 양의 정수여야 한다."""
        h = CellRegistry._hash("any-key")
        assert isinstance(h, int)
        assert h >= 0

    def test_distribution_across_cells(self, registry: CellRegistry):
        """1000개 키가 모든 Cell에 분배되어야 한다 (균등 분배 검증)."""
        cell_counts: dict[str, int] = {}
        for i in range(1000):
            cell_id = registry.get_cell_for_key(f"user-{i}")
            cell_counts[cell_id] = cell_counts.get(cell_id, 0) + 1

        # 8개 Cell에 1000개 키 → 평균 125개
        # 최소 50개 이상이면 합리적인 분배
        all_cells = registry.get_all_cells()
        for cell_id in all_cells:
            count = cell_counts.get(cell_id, 0)
            assert count >= 50, f"{cell_id}에 {count}개만 할당됨 (50 미만)"


# =============================================================================
# 388: L2 Sync + EventBus Handler Tests
# =============================================================================


class TestCellRegistrySetCellStateL2SyncBehavior:
    """set_cell_state()의 L1→L2→emit 순서 동작 검증 (Q7)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        reset_cell_topology_settings()
        reset_cell_registry()
        yield
        reset_cell_registry()
        reset_cell_topology_settings()

    def test_set_cell_state_calls_sync_to_redis(self):
        """set_cell_state()가 _sync_state_to_redis()를 호출한다."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))
        with patch.object(registry, "_sync_state_to_redis") as mock_sync:
            registry.set_cell_state("cell-0", CellState.DRAINING, reason="test")
            mock_sync.assert_called_once_with("cell-0")

    def test_set_cell_state_updates_updated_at(self):
        """set_cell_state()가 cell.updated_at 타임스탬프를 갱신한다."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))
        cell = registry.get_cell_info("cell-0")
        assert cell is not None
        # Force a known old timestamp
        cell.updated_at = 1000.0

        with patch.object(registry, "_sync_state_to_redis"):
            registry.set_cell_state("cell-0", CellState.DRAINING, reason="test")

        assert cell.updated_at > 1000.0

    def test_set_cell_state_records_last_state_change_time_in_metadata(self):
        """set_cell_state()가 metadata에 last_state_change_time을 기록한다."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))
        with patch.object(registry, "_sync_state_to_redis"):
            registry.set_cell_state("cell-0", CellState.ISOLATED, reason="test")

        cell = registry.get_cell_info("cell-0")
        assert cell is not None
        assert "last_state_change_time" in cell.metadata
        assert isinstance(cell.metadata["last_state_change_time"], float)


class TestCellRegistrySyncToRedisBehavior:
    """_sync_state_to_redis() 리팩터 동작 검증 (Q2, Q6)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        reset_cell_topology_settings()
        reset_cell_registry()
        yield
        reset_cell_registry()
        reset_cell_topology_settings()

    def test_sync_uses_to_l2_dict(self):
        """_sync_state_to_redis()가 cell.to_l2_dict()을 사용한다."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))
        cell = registry.get_cell_info("cell-0")
        assert cell is not None

        with patch("baldur.adapters.redis.get_redis_client") as mock_get_redis:
            mock_redis = mock_get_redis.return_value
            with patch.object(
                cell, "to_l2_dict", return_value={"state": "active"}
            ) as mock_to_l2:
                registry._sync_state_to_redis("cell-0")
                mock_to_l2.assert_called_once()
                mock_redis.hset.assert_called_once()

    def test_sync_does_not_call_redis_publish(self):
        """_sync_state_to_redis()가 raw redis.publish()를 호출하지 않는다 (Q2)."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))
        with patch("baldur.adapters.redis.get_redis_client") as mock_get_redis:
            mock_redis = mock_get_redis.return_value
            registry._sync_state_to_redis("cell-0")
            mock_redis.publish.assert_not_called()


class TestCellRegistryLoadSingleStateBehavior:
    """_load_single_state_from_redis() 동작 검증 (Q16, Q20)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        reset_cell_topology_settings()
        reset_cell_registry()
        yield
        reset_cell_registry()
        reset_cell_topology_settings()

    def test_load_single_applies_l2_data(self):
        """Redis에서 가져온 데이터가 apply_l2_dict로 적용된다."""
        import time

        registry = CellRegistry(CellTopologySettings(cell_count=2))
        future_ts = str(time.time() + 1000)
        l2_data = {
            "state": "draining",
            "updated_at": future_ts,
            "health_score": "0.5",
            "warmup_percentage": "0.0",
        }
        with patch("baldur.adapters.redis.get_redis_client") as mock_get_redis:
            mock_get_redis.return_value.hgetall.return_value = l2_data
            result = registry._load_single_state_from_redis("cell-0")

        assert result is True
        cell = registry.get_cell_info("cell-0")
        assert cell is not None
        assert cell.state == CellState.DRAINING

    def test_load_single_returns_false_for_removed_cell(self):
        """삭제된 Cell에 대해 defensive .get()으로 False를 반환한다 (Q20)."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))
        with patch("baldur.adapters.redis.get_redis_client") as mock_get_redis:
            mock_get_redis.return_value.hgetall.return_value = {
                "state": "active",
                "updated_at": "1.0",
            }
            result = registry._load_single_state_from_redis("cell-nonexistent")
        assert result is False

    def test_load_single_returns_false_when_redis_unavailable(self):
        """Redis 미사용 시 False를 반환한다."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))
        with patch(
            "baldur.adapters.redis.get_redis_client",
            return_value=None,
        ):
            result = registry._load_single_state_from_redis("cell-0")
        assert result is False


class TestCellRegistryOnCellStateEventBehavior:
    """_on_cell_state_event() handler 동작 검증 (Q1, Q11)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        reset_cell_topology_settings()
        reset_cell_registry()
        yield
        reset_cell_registry()
        reset_cell_topology_settings()

    def test_handler_delegates_to_load_single(self):
        """handler가 _load_single_state_from_redis에 위임한다."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))

        class MockEvent:
            data = {"cell_id": "cell-0", "old_state": "active", "new_state": "draining"}

        with patch.object(registry, "_load_single_state_from_redis") as mock_load:
            registry._on_cell_state_event(MockEvent())
            mock_load.assert_called_once_with("cell-0")

    def test_handler_ignores_empty_cell_id(self):
        """cell_id가 비어있으면 load를 호출하지 않는다."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))

        class MockEvent:
            data = {"cell_id": ""}

        with patch.object(registry, "_load_single_state_from_redis") as mock_load:
            registry._on_cell_state_event(MockEvent())
            mock_load.assert_not_called()

    def test_handler_ignores_event_without_data(self):
        """data 속성이 없는 이벤트를 안전하게 무시한다."""
        registry = CellRegistry(CellTopologySettings(cell_count=2))

        class MockEvent:
            pass

        with patch.object(registry, "_load_single_state_from_redis") as mock_load:
            registry._on_cell_state_event(MockEvent())
            mock_load.assert_not_called()


class TestCellHandlerRegistrationBehavior:
    """register_cell_handlers/unregister_cell_handlers 동작 검증 (Q14)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        reset_cell_topology_settings()
        reset_cell_registry()
        yield
        reset_cell_registry()
        reset_cell_topology_settings()

    def test_register_subscribes_to_cell_state_changed(self):
        """register_cell_handlers가 CELL_STATE_CHANGED에 구독한다."""
        from baldur.services.cell_topology.registry import register_cell_handlers
        from baldur.services.event_bus.bus.event_types import EventType

        registry = CellRegistry(CellTopologySettings(cell_count=2))
        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_bus = mock_get_bus.return_value
            register_cell_handlers(registry)
            mock_bus.subscribe.assert_called_once_with(
                EventType.CELL_STATE_CHANGED,
                registry._on_cell_state_event,
            )

    def test_unregister_unsubscribes_from_cell_state_changed(self):
        """unregister_cell_handlers가 CELL_STATE_CHANGED 구독을 해제한다."""
        from baldur.services.cell_topology.registry import unregister_cell_handlers
        from baldur.services.event_bus.bus.event_types import EventType

        registry = CellRegistry(CellTopologySettings(cell_count=2))
        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_bus = mock_get_bus.return_value
            unregister_cell_handlers(registry)
            mock_bus.unsubscribe.assert_called_once_with(
                EventType.CELL_STATE_CHANGED,
                registry._on_cell_state_event,
            )
