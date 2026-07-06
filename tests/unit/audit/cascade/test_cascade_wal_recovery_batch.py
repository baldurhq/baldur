"""
Cascade Auditor WAL Recovery — Batch Idempotency & Index 단위 테스트.

최신 커밋 8baccbf에서 추가된 배치 최적화 메서드를 검증합니다.

Test Categories:
    A. _batch_check_cascade_idempotency (Behavior):
        - 정상 반환 (중복 인덱스 집합)
        - ImportError graceful (빈 집합 반환)
        - RuntimeError graceful (빈 집합 반환)
    B. _batch_mark_cascade_processed (Behavior):
        - 정상 호출 (batch_mark_as_processed 호출 확인)
        - ImportError graceful (예외 없이 종료)
        - RuntimeError graceful (예외 없이 종료)
    C. _batch_add_to_index (Behavior):
        - 역순 삽입으로 최신순 유지
        - max_index_size 트리밍
        - 빈 인덱스에 추가
    D. recover_from_local_wal 배치 경로 (Behavior):
        - 멱등성 스킵 카운트 정확성
        - 빈 입력 처리
        - 전체 중복 시 스킵 처리
        - 부분 중복 시 선별 복구
    E. recover_from_local_wal WAL 정리 조건 (Behavior):
        - failed==0 && (recovered>0 || skipped>0) 시 정리
        - failed>0 시 정리 안 함
    F. recover_from_local_wal 반환값 (Contract):
        - idempotency_skipped 키 존재 (모든 경로)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.cascade_auditor import (
    CascadeEventAuditor,
    reset_cascade_auditor,
)
from baldur.audit.cascade_auditor._wal_recovery import (
    _BATCH_SIZE,
    _IDEMPOTENCY_TTL,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def memory_backend():
    """Memory backend fixture for cascade auditor."""
    from baldur.core.state_backend import MemoryStateBackend

    return MemoryStateBackend()


@pytest.fixture
def auditor(memory_backend):
    """CascadeEventAuditor with memory backend and fixed max_index_size."""
    reset_cascade_auditor()
    a = CascadeEventAuditor(max_index_size=100)
    a._get_backend = MagicMock(return_value=memory_backend)
    return a


def _make_wal_entry(entry_id: str, namespace: str = "global") -> dict:
    """WAL 엔트리 딕셔너리를 생성하는 헬퍼."""
    return {
        "id": entry_id,
        "namespace": namespace,
        "type": "cascade",
        "trigger": {
            "trigger_type": "EMERGENCY_LEVEL_CHANGED",
            "event_id": f"evt-{entry_id}",
            "details": {"old_level": "NORMAL", "new_level": "LEVEL_3"},
            "triggered_by": "system",
        },
        "effects": [
            {
                "event_id": f"eff-{entry_id}",
                "action_type": "GOVERNANCE_STRICT",
                "caused_by": f"evt-{entry_id}",
                "success": True,
                "details": {"mode": "STRICT"},
            }
        ],
        "timestamp": "2026-03-13T10:00:00Z",
    }


# =============================================================================
# A. _batch_check_cascade_idempotency Tests
# =============================================================================


class TestBatchCheckCascadeIdempotencyBehavior:
    """_batch_check_cascade_idempotency 동작 검증."""

    def test_normal_returns_duplicate_indices(self, auditor):
        """IdempotencyService가 정상 작동할 때 중복 인덱스 집합을 반환한다."""
        entries = [_make_wal_entry(f"id-{i}") for i in range(5)]

        # Given — batch_check가 [dup, new, dup, new, new] 반환
        mock_result_dup = MagicMock()
        mock_result_dup.is_duplicate = True
        mock_result_new = MagicMock()
        mock_result_new.is_duplicate = False

        mock_service = MagicMock()
        mock_service.batch_check.return_value = [
            mock_result_dup,
            mock_result_new,
            mock_result_dup,
            mock_result_new,
            mock_result_new,
        ]

        with (
            patch(
                "baldur.services.idempotency.IdempotencyService",
                return_value=mock_service,
            ),
            patch(
                "baldur.services.idempotency.IdempotencyKey",
            ) as mock_key_cls,
        ):
            mock_key_cls.for_wal_recovery.return_value = MagicMock()

            # When
            result = auditor._batch_check_cascade_idempotency(entries)

        # Then
        assert result == {0, 2}

    def test_import_error_returns_empty_set(self, auditor):
        """IdempotencyService 미설치 시 빈 집합 반환 (graceful degradation)."""
        entries = [_make_wal_entry("id-1")]

        with patch.dict(
            "sys.modules",
            {"baldur.services.idempotency": None},
        ):
            result = auditor._batch_check_cascade_idempotency(entries)

        assert result == set()

    def test_runtime_error_returns_empty_set(self, auditor):
        """IdempotencyService 런타임 에러 시 빈 집합 반환 (graceful degradation)."""
        entries = [_make_wal_entry("id-1")]

        with patch(
            "baldur.services.idempotency.IdempotencyService",
            side_effect=RuntimeError("Redis unavailable"),
        ):
            result = auditor._batch_check_cascade_idempotency(entries)

        assert result == set()


# =============================================================================
# B. _batch_mark_cascade_processed Tests
# =============================================================================


class TestBatchMarkCascadeProcessedBehavior:
    """_batch_mark_cascade_processed 동작 검증."""

    def test_normal_calls_batch_mark(self, auditor):
        """정상 시 batch_mark_as_processed를 올바른 TTL로 호출한다."""
        entries = [_make_wal_entry(f"id-{i}") for i in range(3)]

        mock_service = MagicMock()

        with (
            patch(
                "baldur.services.idempotency.IdempotencyService",
                return_value=mock_service,
            ),
            patch(
                "baldur.services.idempotency.IdempotencyKey",
            ) as mock_key_cls,
        ):
            mock_key_cls.for_wal_recovery.return_value = MagicMock()

            # When
            auditor._batch_mark_cascade_processed(entries)

        # Then
        mock_service.batch_mark_as_processed.assert_called_once()
        call_kwargs = mock_service.batch_mark_as_processed.call_args
        assert call_kwargs[1]["ttl"] == _IDEMPOTENCY_TTL

    def test_import_error_graceful(self, auditor):
        """IdempotencyService 미설치 시 예외 없이 종료."""
        entries = [_make_wal_entry("id-1")]

        with patch.dict(
            "sys.modules",
            {"baldur.services.idempotency": None},
        ):
            # Should not raise
            auditor._batch_mark_cascade_processed(entries)

    def test_runtime_error_graceful(self, auditor):
        """IdempotencyService 런타임 에러 시 예외 없이 종료."""
        entries = [_make_wal_entry("id-1")]

        with patch(
            "baldur.services.idempotency.IdempotencyService",
            side_effect=RuntimeError("Redis unavailable"),
        ):
            # Should not raise
            auditor._batch_mark_cascade_processed(entries)


# =============================================================================
# C. _batch_add_to_index Tests
# =============================================================================


class TestBatchAddToIndexBehavior:
    """_batch_add_to_index 동작 검증."""

    def test_reverse_insertion_order(self, auditor, memory_backend):
        """역순 삽입으로 기존 _add_to_index N회 호출과 동일한 최신순 유지."""
        # Given — 기존 인덱스에 old-1, old-2 존재
        namespace = "global"
        key = auditor.CASCADE_INDEX_KEY.format(namespace=namespace)
        memory_backend.set(key, {"ids": ["old-1", "old-2"]})

        # When — [A, B, C]를 배치 추가
        # 기존 _add_to_index 호출 시: insert(0,A) → insert(0,B) → insert(0,C)
        # 결과: [C, B, A, old-1, old-2]
        auditor._batch_add_to_index(namespace, ["A", "B", "C"])

        # Then — 역순 prepend로 동일 순서 유지
        result = memory_backend.get(key)
        assert result["ids"] == ["C", "B", "A", "old-1", "old-2"]

    def test_max_index_size_trimming(self, auditor, memory_backend):
        """max_index_size를 초과하면 오래된 항목이 제거된다."""
        # Given — max_index_size=100, 기존 95개 + 신규 10개 = 105개 → 100으로 트림
        namespace = "global"
        key = auditor.CASCADE_INDEX_KEY.format(namespace=namespace)
        existing = [f"old-{i}" for i in range(95)]
        memory_backend.set(key, {"ids": existing})

        new_ids = [f"new-{i}" for i in range(10)]

        # When
        auditor._batch_add_to_index(namespace, new_ids)

        # Then
        result = memory_backend.get(key)
        assert len(result["ids"]) == auditor._max_index_size
        # 신규 10개가 모두 앞에 존재
        for nid in reversed(new_ids):
            assert nid in result["ids"]

    def test_add_to_empty_index(self, auditor, memory_backend):
        """빈 인덱스에 추가 시 정상 동작."""
        namespace = "global"

        # When
        auditor._batch_add_to_index(namespace, ["A", "B"])

        # Then
        key = auditor.CASCADE_INDEX_KEY.format(namespace=namespace)
        result = memory_backend.get(key)
        assert result["ids"] == ["B", "A"]


# =============================================================================
# D. recover_from_local_wal — 배치 경로 Tests
# =============================================================================


class TestRecoverFromLocalWalBatchPathBehavior:
    """recover_from_local_wal 배치 멱등성 경로 동작 검증."""

    def _setup_wal_entries(self, tmp_path, entries, auditor):
        """WAL 파일 준비 및 경로 패치 헬퍼."""
        import json

        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        with open(wal_path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return wal_path

    def test_idempotency_skip_count_accurate(self, auditor, memory_backend, tmp_path):
        """멱등성 스킵 카운트가 정확하다."""
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(5)]
        wal_path = self._setup_wal_entries(tmp_path, entries, auditor)

        # 2개를 중복으로 마킹
        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch.object(
                auditor,
                "_batch_check_cascade_idempotency",
                return_value={0, 3},
            ),
            patch.object(
                auditor,
                "_save_cascade_event",
            ),
            patch.object(
                auditor,
                "_batch_mark_cascade_processed",
            ),
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ),
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        assert result["idempotency_skipped"] == 2
        assert result["recovered"] == 3

    def test_empty_entries_returns_no_wal_data(self, auditor, tmp_path):
        """WAL에 해당 네임스페이스 엔트리가 없으면 no_wal_data 반환."""
        import json

        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        # 다른 네임스페이스의 엔트리만 존재
        entry = _make_wal_entry("id-1", namespace="other-ns")
        with open(wal_path, "w") as f:
            f.write(json.dumps(entry) + "\n")

        with patch(
            "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
            str(wal_path),
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        assert result["status"] == "no_wal_data"
        assert result["idempotency_skipped"] == 0

    def test_all_duplicates_skips_all(self, auditor, memory_backend, tmp_path):
        """전체 엔트리가 중복이면 recovered=0, skipped=N, WAL 정리 실행."""
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(3)]
        wal_path = self._setup_wal_entries(tmp_path, entries, auditor)

        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch.object(
                auditor,
                "_batch_check_cascade_idempotency",
                return_value={0, 1, 2},
            ),
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ) as mock_remove,
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        assert result["recovered"] == 0
        assert result["idempotency_skipped"] == 3
        assert result["failed"] == 0
        # failed==0 && skipped>0 → WAL 정리 실행
        mock_remove.assert_called_once_with("global")

    def test_partial_duplicates_recovers_non_duplicates(
        self, auditor, memory_backend, tmp_path
    ):
        """부분 중복 시 중복이 아닌 엔트리만 복구한다."""
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(4)]
        wal_path = self._setup_wal_entries(tmp_path, entries, auditor)

        # 인덱스 1, 3이 중복
        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch.object(
                auditor,
                "_batch_check_cascade_idempotency",
                return_value={1, 3},
            ),
            patch.object(
                auditor,
                "_save_cascade_event",
            ) as mock_save,
            patch.object(
                auditor,
                "_batch_add_to_index",
            ),
            patch.object(
                auditor,
                "_batch_mark_cascade_processed",
            ),
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ),
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        assert result["recovered"] == 2
        assert result["idempotency_skipped"] == 2
        assert mock_save.call_count == 2


# =============================================================================
# E. recover_from_local_wal — WAL 정리 조건 Tests
# =============================================================================


class TestRecoverFromLocalWalCleanupBehavior:
    """recover_from_local_wal WAL 정리 조건 동작 검증."""

    def _setup_wal_file(self, tmp_path, count=3, namespace="global"):
        """WAL 파일 생성 헬퍼."""
        import json

        entries = [
            _make_wal_entry(f"cascade-{i}", namespace=namespace) for i in range(count)
        ]
        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        with open(wal_path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return wal_path

    def test_cleanup_when_all_recovered_no_failures(
        self, auditor, memory_backend, tmp_path
    ):
        """recovered>0 && failed==0 시 WAL 정리 실행."""
        wal_path = self._setup_wal_file(tmp_path, count=2)

        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch.object(
                auditor,
                "_batch_check_cascade_idempotency",
                return_value=set(),
            ),
            patch.object(
                auditor,
                "_save_cascade_event",
            ),
            patch.object(
                auditor,
                "_batch_add_to_index",
            ),
            patch.object(
                auditor,
                "_batch_mark_cascade_processed",
            ),
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ) as mock_remove,
        ):
            auditor.recover_from_local_wal(namespace="global")

        mock_remove.assert_called_once_with("global")

    def test_cleanup_when_all_skipped_no_failures(
        self, auditor, memory_backend, tmp_path
    ):
        """skipped>0 && failed==0 시 WAL 정리 실행."""
        wal_path = self._setup_wal_file(tmp_path, count=2)

        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch.object(
                auditor,
                "_batch_check_cascade_idempotency",
                return_value={0, 1},
            ),
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ) as mock_remove,
        ):
            auditor.recover_from_local_wal(namespace="global")

        mock_remove.assert_called_once_with("global")

    def test_no_cleanup_when_failures_exist(self, auditor, memory_backend, tmp_path):
        """failed>0 시 WAL 정리 안 함."""
        wal_path = self._setup_wal_file(tmp_path, count=2)

        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch.object(
                auditor,
                "_batch_check_cascade_idempotency",
                return_value=set(),
            ),
            patch.object(
                auditor,
                "_save_cascade_event",
                side_effect=Exception("save failed"),
            ),
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ) as mock_remove,
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        assert result["failed"] > 0
        mock_remove.assert_not_called()


# =============================================================================
# F. recover_from_local_wal — 반환값 Contract Tests
# =============================================================================


class TestRecoverFromLocalWalReturnContract:
    """recover_from_local_wal 반환값에 idempotency_skipped 키가 항상 존재하는지 계약 검증."""

    def test_no_wal_file_has_idempotency_skipped_key(self, auditor, tmp_path):
        """WAL 파일 미존재 시 반환값에 idempotency_skipped 키 존재."""
        with patch(
            "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
            str(tmp_path / "nonexistent.jsonl"),
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        assert "idempotency_skipped" in result
        assert result["idempotency_skipped"] == 0

    def test_dry_run_has_idempotency_skipped_key(self, auditor, tmp_path):
        """dry_run 시 반환값에 idempotency_skipped 키 존재."""
        import json

        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        entry = _make_wal_entry("cascade-1")
        with open(wal_path, "w") as f:
            f.write(json.dumps(entry) + "\n")

        with patch(
            "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
            str(wal_path),
        ):
            result = auditor.recover_from_local_wal(namespace="global", dry_run=True)

        assert "idempotency_skipped" in result
        assert result["idempotency_skipped"] == 0

    def test_empty_entries_has_idempotency_skipped_key(self, auditor, tmp_path):
        """빈 WAL 파일 시 반환값에 idempotency_skipped 키 존재."""
        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        wal_path.write_text("")

        with patch(
            "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
            str(wal_path),
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        assert "idempotency_skipped" in result

    def test_completed_has_idempotency_skipped_key(
        self, auditor, memory_backend, tmp_path
    ):
        """정상 완료 시 반환값에 idempotency_skipped 키 존재."""
        import json

        entries = [_make_wal_entry(f"cascade-{i}") for i in range(2)]
        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        with open(wal_path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch.object(
                auditor,
                "_batch_check_cascade_idempotency",
                return_value={0},
            ),
            patch.object(
                auditor,
                "_save_cascade_event",
            ),
            patch.object(
                auditor,
                "_batch_add_to_index",
            ),
            patch.object(
                auditor,
                "_batch_mark_cascade_processed",
            ),
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ),
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        assert "idempotency_skipped" in result
        assert result["idempotency_skipped"] == 1
        assert result["status"] == "completed"


# =============================================================================
# G. Contract — 배치 상수 검증
# =============================================================================


class TestCascadeWalRecoveryBatchContract:
    """배치 복구 상수 계약 검증."""

    def test_batch_size_is_1000(self):
        """_BATCH_SIZE 계약값: 1000."""
        assert _BATCH_SIZE == 1000

    def test_idempotency_ttl_is_3600(self):
        """_IDEMPOTENCY_TTL 계약값: 3600초 (1시간)."""
        assert _IDEMPOTENCY_TTL == 3600
