"""
Cascade WAL Recovery Batch Idempotency 통합 테스트.

_batch_check_cascade_idempotency → IdempotencyService.batch_check → cache.get_many
체인이 2+ 서비스 조합이므로 mock-based 통합 테스트로 검증합니다.

Test Categories:
    A. Batch Check → IdempotencyService → Cache Chain:
        - 정상 경로: WAL 엔트리 → IdempotencyKey 생성 → cache.get_many → 중복 판정
        - 부분 중복: 일부 엔트리만 캐시에 존재
        - 캐시 전체 미스: 모든 엔트리가 신규
    B. Batch Mark → IdempotencyService → Cache Chain:
        - 정상 경로: WAL 엔트리 → IdempotencyKey 생성 → cache.set_many 호출
        - TTL 전파: _IDEMPOTENCY_TTL이 cache.set_many의 timeout으로 전달
    C. End-to-End Recovery Flow:
        - recover_from_local_wal → 배치 체크 → 선별 복구 → 배치 마킹 → 인덱스 업데이트

Note: All tests use in-memory mock cache - no Redis/DB dependency.
      This enables parallel test execution with pytest-xdist.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.cascade_auditor import (
    CascadeEventAuditor,
    reset_cascade_auditor,
)
from baldur.audit.cascade_auditor._wal_recovery import (
    _IDEMPOTENCY_TTL,
)
from baldur.services.idempotency import IdempotencyKey, IdempotencyService
from baldur.services.idempotency.models import IdempotencyDomain

# =============================================================================
# Test Helpers
# =============================================================================


class InMemoryCache:
    """
    In-memory cache simulating CacheProviderInterface for integration tests.

    Supports both Django cache interface (get_many/set_many) and
    CacheProviderInterface (mget/mset) used by IdempotencyService batch methods.
    """

    def __init__(self):
        self._store: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def get_many(self, keys: list[str]) -> dict[str, Any]:
        return {k: self._store[k] for k in keys if k in self._store}

    def mget(self, keys: list[str]) -> dict[str, Any]:
        return {k: self._store[k] for k in keys if k in self._store}

    def set(self, key: str, value: Any, timeout: int | None = None, **kwargs) -> None:
        self._store[key] = value

    def set_many(self, mapping: dict[str, Any], timeout: int | None = None) -> None:
        self._store.update(mapping)

    def mset(self, mapping: dict[str, Any], **kwargs) -> bool:
        self._store.update(mapping)
        return True

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


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


def _write_wal_file(path: Path, entries: list[dict]) -> None:
    """WAL JSONL 파일을 작성하는 헬퍼."""
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def in_memory_cache():
    """InMemoryCache fixture for IdempotencyService."""
    return InMemoryCache()


@pytest.fixture
def idempotency_service(in_memory_cache):
    """IdempotencyService with in-memory cache."""
    service = IdempotencyService()
    service._cache = in_memory_cache
    return service


@pytest.fixture
def memory_backend():
    """Memory state backend fixture."""
    from baldur.core.state_backend import MemoryStateBackend

    return MemoryStateBackend()


@pytest.fixture
def auditor(memory_backend):
    """CascadeEventAuditor with memory backend."""
    reset_cascade_auditor()
    a = CascadeEventAuditor(max_index_size=100)
    a._get_backend = MagicMock(return_value=memory_backend)
    return a


# =============================================================================
# A. Batch Check → IdempotencyService → Cache Chain
# =============================================================================


class TestBatchCheckIdempotencyChain:
    """
    _batch_check_cascade_idempotency → IdempotencyService.batch_check → cache chain.

    Validates:
    - IdempotencyKey.for_wal_recovery로 올바른 키 생성
    - cache.get_many로 배치 조회 수행
    - 중복/신규 판정이 캐시 상태에 따라 정확
    """

    def test_full_chain_detects_duplicates_from_cache(
        self, auditor, idempotency_service, in_memory_cache
    ):
        """
        Purpose:
            WAL 엔트리의 ID로 생성된 IdempotencyKey가 캐시에 존재하면
            중복으로 판정되는 전체 체인을 검증.
        Expected:
            - 캐시에 존재하는 엔트리의 인덱스가 중복 집합에 포함
            - 캐시에 없는 엔트리의 인덱스는 포함되지 않음
        """
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(5)]

        # Given — 인덱스 0, 2의 엔트리를 캐시에 미리 등록
        for idx in [0, 2]:
            key = IdempotencyKey.for_wal_recovery(
                wal_entry_id=entries[idx]["id"],
                operation="cascade_recovery",
            )
            in_memory_cache.set(key.cache_key, True)

        # When — 실제 IdempotencyService를 통해 배치 체크
        with patch(
            "baldur.services.idempotency.IdempotencyService",
            return_value=idempotency_service,
        ):
            result = auditor._batch_check_cascade_idempotency(entries)

        # Then
        assert result == {0, 2}

    def test_full_chain_all_new_returns_empty_set(
        self, auditor, idempotency_service, in_memory_cache
    ):
        """
        Purpose:
            캐시가 비어있을 때 모든 엔트리가 신규로 판정되는지 검증.
        Expected:
            - 빈 집합 반환 (중복 없음)
        """
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(3)]

        # Given — 캐시 비어있음

        # When
        with patch(
            "baldur.services.idempotency.IdempotencyService",
            return_value=idempotency_service,
        ):
            result = auditor._batch_check_cascade_idempotency(entries)

        # Then
        assert result == set()

    def test_full_chain_all_duplicates(
        self, auditor, idempotency_service, in_memory_cache
    ):
        """
        Purpose:
            모든 엔트리가 캐시에 존재할 때 전체 중복 판정 검증.
        Expected:
            - 모든 인덱스가 중복 집합에 포함
        """
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(3)]

        # Given — 모든 엔트리를 캐시에 등록
        for entry in entries:
            key = IdempotencyKey.for_wal_recovery(
                wal_entry_id=entry["id"],
                operation="cascade_recovery",
            )
            in_memory_cache.set(key.cache_key, True)

        # When
        with patch(
            "baldur.services.idempotency.IdempotencyService",
            return_value=idempotency_service,
        ):
            result = auditor._batch_check_cascade_idempotency(entries)

        # Then
        assert result == {0, 1, 2}

    def test_idempotency_key_uses_wal_recovery_domain(self):
        """
        Purpose:
            for_wal_recovery로 생성된 키가 WAL_RECOVERY 도메인을 사용하는지 검증.
        Expected:
            - domain이 IdempotencyDomain.WAL_RECOVERY
            - cache_key 접두사가 "idempotency:wal_recovery:"
        """
        key = IdempotencyKey.for_wal_recovery(
            wal_entry_id="cascade-42",
            operation="cascade_recovery",
        )
        assert key.domain == IdempotencyDomain.WAL_RECOVERY
        assert key.cache_key.startswith("idempotency:wal_recovery:")


# =============================================================================
# B. Batch Mark → IdempotencyService → Cache Chain
# =============================================================================


class TestBatchMarkIdempotencyChain:
    """
    _batch_mark_cascade_processed → IdempotencyService.batch_mark_as_processed → cache chain.

    Validates:
    - cache.set_many로 배치 마킹 수행
    - TTL이 올바르게 전파
    """

    def test_full_chain_marks_entries_in_cache(
        self, auditor, idempotency_service, in_memory_cache
    ):
        """
        Purpose:
            _batch_mark_cascade_processed 후 캐시에 엔트리가 존재하는지 검증.
        Expected:
            - 마킹된 엔트리의 cache_key가 캐시에 True로 존재
        """
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(3)]

        # When — 실제 IdempotencyService를 통해 배치 마킹
        with patch(
            "baldur.services.idempotency.IdempotencyService",
            return_value=idempotency_service,
        ):
            auditor._batch_mark_cascade_processed(entries)

        # Then — 모든 엔트리가 캐시에 존재
        for entry in entries:
            key = IdempotencyKey.for_wal_recovery(
                wal_entry_id=entry["id"],
                operation="cascade_recovery",
            )
            assert in_memory_cache.get(key.cache_key) is True

    def test_ttl_propagated_to_cache(self, auditor):
        """
        Purpose:
            _IDEMPOTENCY_TTL이 cache.mset의 ttl 파라미터로 전달되는지 검증.
        Expected:
            - mset의 ttl 인자가 _IDEMPOTENCY_TTL과 동일
        """
        entries = [_make_wal_entry("cascade-1")]
        mock_cache = MagicMock()
        mock_cache.mget.return_value = {}

        service = IdempotencyService()
        service._cache = mock_cache

        # When
        with patch(
            "baldur.services.idempotency.IdempotencyService",
            return_value=service,
        ):
            auditor._batch_mark_cascade_processed(entries)

        # Then
        mock_cache.mset.assert_called_once()
        call_kwargs = mock_cache.mset.call_args
        assert call_kwargs[1]["ttl"] == timedelta(seconds=_IDEMPOTENCY_TTL)


# =============================================================================
# C. End-to-End Recovery Flow
# =============================================================================


class TestEndToEndRecoveryFlow:
    """
    recover_from_local_wal 전체 흐름 통합 검증.

    Validates:
    - WAL 읽기 → 배치 체크 → 선별 복구 → 배치 마킹 → 인덱스 업데이트 전체 흐름
    - 중복 엔트리가 체크/마킹 체인을 통해 올바르게 스킵
    """

    def test_recovery_skips_already_processed_entries(
        self, auditor, memory_backend, in_memory_cache, tmp_path
    ):
        """
        Purpose:
            이미 처리된 엔트리가 캐시에 존재할 때 recover_from_local_wal이
            해당 엔트리를 스킵하고 나머지만 복구하는 전체 흐름을 검증.
        Expected:
            - idempotency_skipped == 캐시에 존재하던 엔트리 수
            - recovered == 신규 엔트리 수
            - 신규 엔트리만 _save_cascade_event로 저장
            - 복구 후 신규 엔트리가 캐시에 마킹
        """
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(5)]
        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        _write_wal_file(wal_path, entries)

        # Given — 인덱스 1, 3을 이미 처리된 것으로 캐시에 등록
        for idx in [1, 3]:
            key = IdempotencyKey.for_wal_recovery(
                wal_entry_id=entries[idx]["id"],
                operation="cascade_recovery",
            )
            in_memory_cache.set(key.cache_key, True)

        service = IdempotencyService()
        service._cache = in_memory_cache

        # When
        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch(
                "baldur.services.idempotency.IdempotencyService",
                return_value=service,
            ),
            patch.object(
                auditor,
                "_save_cascade_event",
            ) as mock_save,
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ),
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        # Then — 2개 스킵, 3개 복구
        assert result["idempotency_skipped"] == 2
        assert result["recovered"] == 3
        assert result["failed"] == 0
        assert mock_save.call_count == 3

        # Then — 복구된 엔트리도 캐시에 마킹됨
        for idx in [0, 2, 4]:
            key = IdempotencyKey.for_wal_recovery(
                wal_entry_id=entries[idx]["id"],
                operation="cascade_recovery",
            )
            assert in_memory_cache.get(key.cache_key) is True

    def test_recovery_updates_index_only_for_recovered(
        self, auditor, memory_backend, in_memory_cache, tmp_path
    ):
        """
        Purpose:
            복구된 이벤트의 ID만 인덱스에 추가되는지 검증.
        Expected:
            - 스킵된 엔트리의 ID는 인덱스에 없음
            - 복구된 엔트리의 ID만 인덱스에 존재
        """
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(3)]
        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        _write_wal_file(wal_path, entries)

        # Given — 인덱스 1을 이미 처리된 것으로 등록
        dup_key = IdempotencyKey.for_wal_recovery(
            wal_entry_id=entries[1]["id"],
            operation="cascade_recovery",
        )
        in_memory_cache.set(dup_key.cache_key, True)

        service = IdempotencyService()
        service._cache = in_memory_cache

        # When
        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch(
                "baldur.services.idempotency.IdempotencyService",
                return_value=service,
            ),
            patch.object(
                auditor,
                "_save_cascade_event",
            ),
            patch.object(
                auditor,
                "_remove_namespace_from_wal",
            ),
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        # Then
        assert result["recovered"] == 2
        assert result["idempotency_skipped"] == 1

        # 인덱스에 복구된 ID만 존재
        index_key = auditor.CASCADE_INDEX_KEY.format(namespace="global")
        index_data = memory_backend.get(index_key)
        assert index_data is not None
        stored_ids = index_data["ids"]
        assert entries[1]["id"] not in stored_ids
        assert entries[0]["id"] in stored_ids
        assert entries[2]["id"] in stored_ids

    def test_index_failure_does_not_crash_recovery(
        self, auditor, memory_backend, in_memory_cache, tmp_path
    ):
        """
        Purpose:
            _batch_add_to_index가 예외를 던져도 recover_from_local_wal이
            정상 종료하고 멱등성 마킹이 수행되는지 검증.
        Expected:
            - 예외가 전파되지 않음
            - result["index_failed"] == True
            - 멱등성 마킹은 수행됨 (다음 복구에서 데이터 중복 저장 방지)
        """
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(3)]
        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        _write_wal_file(wal_path, entries)

        service = IdempotencyService()
        service._cache = in_memory_cache

        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch(
                "baldur.services.idempotency.IdempotencyService",
                return_value=service,
            ),
            patch.object(auditor, "_save_cascade_event"),
            patch.object(
                auditor,
                "_batch_add_to_index",
                side_effect=ConnectionError("Redis down"),
            ),
            patch.object(auditor, "_remove_namespace_from_wal") as mock_cleanup,
        ):
            result = auditor.recover_from_local_wal(namespace="global")

        # Then — 예외 없이 완료, index_failed 플래그 설정
        assert result["recovered"] == 3
        assert result["index_failed"] is True

        # Then — WAL 정리되지 않음 (인덱스 재시도 필요)
        mock_cleanup.assert_not_called()

        # Then — 멱등성 마킹은 수행됨
        for entry in entries:
            key = IdempotencyKey.for_wal_recovery(
                wal_entry_id=entry["id"],
                operation="cascade_recovery",
            )
            assert in_memory_cache.get(key.cache_key) is True

    def test_index_failure_wal_retained_for_retry(
        self, auditor, memory_backend, in_memory_cache, tmp_path
    ):
        """
        Purpose:
            인덱스 실패 후 재시도 시, 멱등성 체크가 데이터 중복 저장을 방지하고
            인덱스 업데이트만 재시도하는지 검증.
        Expected:
            - 2차 시도에서 idempotency_skipped == 3 (모두 중복)
            - 2차 시도에서 recovered == 0 (재저장 없음)
        """
        entries = [_make_wal_entry(f"cascade-{i}") for i in range(3)]
        wal_path = tmp_path / "cascade_audit_wal.jsonl"
        _write_wal_file(wal_path, entries)

        service = IdempotencyService()
        service._cache = in_memory_cache

        # 1차 시도 — 인덱스 실패
        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch(
                "baldur.services.idempotency.IdempotencyService",
                return_value=service,
            ),
            patch.object(auditor, "_save_cascade_event"),
            patch.object(
                auditor,
                "_batch_add_to_index",
                side_effect=ConnectionError("Redis down"),
            ),
            patch.object(auditor, "_remove_namespace_from_wal"),
        ):
            result1 = auditor.recover_from_local_wal(namespace="global")

        assert result1["recovered"] == 3
        assert result1["index_failed"] is True

        # 2차 시도 — 인덱스 정상
        with (
            patch(
                "baldur.audit.cascade_auditor._wal_recovery.LOCAL_CASCADE_WAL_PATH",
                str(wal_path),
            ),
            patch(
                "baldur.services.idempotency.IdempotencyService",
                return_value=service,
            ),
            patch.object(auditor, "_save_cascade_event") as mock_save,
            patch.object(auditor, "_remove_namespace_from_wal"),
        ):
            result2 = auditor.recover_from_local_wal(namespace="global")

        # Then — 모두 중복으로 스킵, 재저장 없음
        assert result2["idempotency_skipped"] == 3
        assert result2["recovered"] == 0
        assert result2["index_failed"] is False
        mock_save.assert_not_called()
