"""
reconcile_cb_cell_mapping() 단위 테스트.

테스트 대상: services/circuit_breaker/service.py reconcile_cb_cell_mapping()
- 고아 CB 감지 (Hash Ring 변경 후 Cell 불일치)
- 고아 CB 삭제 (상태 전이 없음, Poison Pill 방지)
- 레거시 키 스킵
- 정상 CB 유지
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.core.cb_namespace import (
    make_cell_scoped_cb_name,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService


def _make_service_with_repo(repo=None):
    """InMemory repo를 사용하는 CircuitBreakerService 생성."""
    if repo is None:
        repo = InMemoryCircuitBreakerStateRepository()
    return (
        CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True),
            repository=repo,
        ),
        repo,
    )


class TestReconcileCbCellMappingBehavior:
    """reconcile_cb_cell_mapping 동작 검증."""

    def test_orphan_cb_is_archived_and_deleted(self):
        """Hash Ring 변경 후 불일치 CB는 삭제된다."""
        service, repo = _make_service_with_repo()

        # cell-3에 할당된 CB 생성
        orphan_name = make_cell_scoped_cb_name("svc-a", "cell-3")
        repo.get_or_create(orphan_name)
        repo.update_state(orphan_name, "open")

        # Hash Ring이 변경되어 svc-a가 이제 cell-9에 할당
        mock_registry = MagicMock()
        mock_registry.get_cell_for_key.return_value = "cell-9"

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=mock_registry,
        ):
            result = service.reconcile_cb_cell_mapping()

        assert orphan_name in result["archived"]
        assert repo.get_by_service_name(orphan_name) is None
        assert result["errors"] == []

    def test_matching_cb_is_preserved(self):
        """현재 Hash Ring과 일치하는 CB는 삭제되지 않는다."""
        service, repo = _make_service_with_repo()

        valid_name = make_cell_scoped_cb_name("svc-a", "cell-5")
        repo.get_or_create(valid_name)

        mock_registry = MagicMock()
        mock_registry.get_cell_for_key.return_value = "cell-5"

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=mock_registry,
        ):
            result = service.reconcile_cb_cell_mapping()

        assert result["archived"] == []
        assert repo.get_by_service_name(valid_name) is not None

    def test_legacy_keys_are_skipped(self):
        """레거시 키(cell_id 없음)는 reconciliation에서 건너뛴다."""
        service, repo = _make_service_with_repo()

        repo.get_or_create("legacy_svc")

        mock_registry = MagicMock()

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=mock_registry,
        ):
            result = service.reconcile_cb_cell_mapping()

        assert result["archived"] == []
        assert repo.get_by_service_name("legacy_svc") is not None
        # 레거시 키에 대해 get_cell_for_key가 호출되지 않아야 함
        mock_registry.get_cell_for_key.assert_not_called()

    def test_multiple_orphans_all_deleted(self):
        """여러 고아 CB가 모두 삭제된다."""
        service, repo = _make_service_with_repo()

        orphan1 = make_cell_scoped_cb_name("svc-a", "cell-1")
        orphan2 = make_cell_scoped_cb_name("svc-b", "cell-2")
        repo.get_or_create(orphan1)
        repo.get_or_create(orphan2)

        mock_registry = MagicMock()
        mock_registry.get_cell_for_key.side_effect = lambda key: "cell-99"

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=mock_registry,
        ):
            result = service.reconcile_cb_cell_mapping()

        assert set(result["archived"]) == {orphan1, orphan2}
        assert repo.get_by_service_name(orphan1) is None
        assert repo.get_by_service_name(orphan2) is None

    def test_open_cb_not_migrated_to_new_cell(self):
        """OPEN 상태의 고아 CB는 삭제되며 새 Cell로 상태가 전파되지 않는다."""
        service, repo = _make_service_with_repo()

        orphan_name = make_cell_scoped_cb_name("svc-a", "cell-3")
        repo.get_or_create(orphan_name)
        repo.update_state(orphan_name, "open", failure_count=10)

        new_name = make_cell_scoped_cb_name("svc-a", "cell-7")

        mock_registry = MagicMock()
        mock_registry.get_cell_for_key.return_value = "cell-7"

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=mock_registry,
        ):
            result = service.reconcile_cb_cell_mapping()

        # 고아 삭제 확인
        assert orphan_name in result["archived"]
        # 새 Cell CB가 자동 생성되지 않음 (Lazy Init — 트래픽 발생 시에만 생성)
        assert repo.get_by_service_name(new_name) is None

    def test_returns_errors_on_delete_failure(self):
        """삭제 실패 시 errors에 기록된다."""
        service, repo = _make_service_with_repo()

        orphan_name = make_cell_scoped_cb_name("svc-a", "cell-3")
        repo.get_or_create(orphan_name)

        mock_registry = MagicMock()
        mock_registry.get_cell_for_key.return_value = "cell-9"

        # delete_state를 실패하도록 패치
        original_delete = repo.delete_state
        repo.delete_state = MagicMock(side_effect=RuntimeError("delete failed"))

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=mock_registry,
        ):
            result = service.reconcile_cb_cell_mapping()

        assert len(result["errors"]) > 0
        assert result["errors"][0]["service_name"] == orphan_name
        repo.delete_state = original_delete
