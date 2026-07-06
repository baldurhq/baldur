"""
WAL 복구 시 중복 제거 테스트.

테스트 범위:
1. IdempotencyKey.for_wal_recovery() 사용
2. 중복 엔트리 스킵
3. 복구 성공 시 멱등성 키 등록
4. IdempotencyService 미사용 환경에서 안전하게 진행
5. 복구 통계에 idempotency_skipped 포함
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.graceful_degradation.wal_recovery import HashChainWALRecovery


class TestWALRecoveryDeduplicationBehavior:
    """WAL 복구 중복 제거 테스트."""

    @pytest.fixture
    def wal_dir(self, tmp_path):
        """임시 WAL 디렉토리."""
        return tmp_path / "wal"

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis 클라이언트."""
        redis = MagicMock()
        redis.get.return_value = None  # 처리되지 않은 상태
        redis.pipeline.return_value = MagicMock()
        return redis

    @pytest.fixture
    def recovery(self, wal_dir, mock_redis):
        """HashChainWALRecovery 인스턴스."""
        return HashChainWALRecovery(
            wal_dir=wal_dir,
            redis_client=mock_redis,
            key_prefix="test:",
        )

    def _create_wal_file(self, wal_dir: Path, entries: list) -> Path:
        """테스트용 WAL 파일 생성."""
        wal_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        wal_file = wal_dir / f"hash_chain_wal_{date_str}.jsonl"

        with open(wal_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        return wal_file

    def test_idempotency_key_for_wal_recovery_format(self):
        """IdempotencyKey.for_wal_recovery() 키 형식 확인."""
        from baldur.services.idempotency import (
            IdempotencyDomain,
            IdempotencyKey,
        )

        key = IdempotencyKey.for_wal_recovery(
            wal_entry_id="123",
            operation="redis_replay",
        )

        assert key.domain == IdempotencyDomain.WAL_RECOVERY
        assert key.key == "wal:123:redis_replay"
        assert key.components["wal_entry_id"] == "123"
        assert key.components["operation"] == "redis_replay"
        assert "wal_recovery" in key.cache_key

    def test_recover_skips_duplicate_via_idempotency(self, recovery, wal_dir):
        """멱등성 배치 체크로 중복 엔트리 스킵."""
        # WAL 파일 생성 (2개 엔트리)
        entries = [
            {
                "wal_sequence": 1,
                "operation": "add_integrity",
                "entry_data": {"integrity": {"sequence": 1, "current_hash": "hash1"}},
                "timestamp": datetime.now(UTC).isoformat(),
                "pod_id": "pod-1",
            },
            {
                "wal_sequence": 2,
                "operation": "add_integrity",
                "entry_data": {"integrity": {"sequence": 2, "current_hash": "hash2"}},
                "timestamp": datetime.now(UTC).isoformat(),
                "pod_id": "pod-1",
            },
        ]
        self._create_wal_file(wal_dir, entries)

        # 첫 번째 엔트리는 이미 처리된 것으로 설정 (배치 체크)
        with patch.object(recovery, "_batch_check_idempotency", return_value={1}):
            with patch.object(
                recovery, "_replay_entry", return_value=True
            ) as mock_replay:
                with patch.object(recovery, "_batch_mark_processed"):
                    result = recovery.recover_on_startup()

        assert result["entries_found"] == 2
        assert result["idempotency_skipped"] == 1
        assert result["entries_recovered"] == 1
        assert mock_replay.call_count == 1  # 두 번째만 replay

    def test_recover_marks_as_processed_after_success(self, recovery, wal_dir):
        """복구 성공 후 배치 멱등성 마킹."""
        entries = [
            {
                "wal_sequence": 1,
                "operation": "add_integrity",
                "entry_data": {"integrity": {"sequence": 1, "current_hash": "hash1"}},
                "timestamp": datetime.now(UTC).isoformat(),
                "pod_id": "pod-1",
            },
        ]
        self._create_wal_file(wal_dir, entries)

        with patch.object(recovery, "_batch_check_idempotency", return_value=set()):
            with patch.object(recovery, "_replay_entry", return_value=True):
                with patch.object(recovery, "_batch_mark_processed") as mock_mark:
                    result = recovery.recover_on_startup()

        assert result["entries_recovered"] == 1
        mock_mark.assert_called_once_with([1], "redis_replay")

    def test_recover_does_not_mark_on_failure(self, recovery, wal_dir):
        """복구 실패 시 배치 멱등성 마킹 미호출."""
        entries = [
            {
                "wal_sequence": 1,
                "operation": "add_integrity",
                "entry_data": {"integrity": {"sequence": 1, "current_hash": "hash1"}},
                "timestamp": datetime.now(UTC).isoformat(),
                "pod_id": "pod-1",
            },
        ]
        self._create_wal_file(wal_dir, entries)

        with patch.object(recovery, "_batch_check_idempotency", return_value=set()):
            with patch.object(recovery, "_replay_entry", return_value=False):  # 실패
                with patch.object(recovery, "_batch_mark_processed") as mock_mark:
                    result = recovery.recover_on_startup()

        assert result["entries_failed"] == 1
        mock_mark.assert_not_called()

    def test_idempotency_check_graceful_on_import_error(self, recovery, wal_dir):
        """IdempotencyService import 실패 시 안전하게 진행."""
        entries = [
            {
                "wal_sequence": 1,
                "operation": "add_integrity",
                "entry_data": {"integrity": {"sequence": 1, "current_hash": "hash1"}},
                "timestamp": datetime.now(UTC).isoformat(),
                "pod_id": "pod-1",
            },
        ]
        self._create_wal_file(wal_dir, entries)

        # ImportError 시뮬레이션
        with patch.dict("sys.modules", {"baldur.services.idempotency": None}):
            with patch.object(recovery, "_replay_entry", return_value=True):
                # 실제 메서드 호출
                is_dup = recovery._is_duplicate_via_idempotency(1, "redis_replay")

        assert is_dup is False  # 중복 아님으로 처리 (안전하게 진행)

    def test_idempotency_check_graceful_on_service_error(self, recovery):
        """IdempotencyService 에러 시 안전하게 진행."""
        with patch("baldur.services.idempotency.IdempotencyKey") as mock_key:
            mock_key.for_wal_recovery.side_effect = RuntimeError("Redis unavailable")

            is_dup = recovery._is_duplicate_via_idempotency(1, "redis_replay")

        assert is_dup is False  # 에러 시 중복 아님으로 처리

    def test_result_includes_idempotency_skipped_count(self, recovery, wal_dir):
        """복구 결과에 idempotency_skipped 카운트 포함."""
        # 빈 WAL (엔트리 없음)
        self._create_wal_file(wal_dir, [])

        result = recovery.recover_on_startup()

        assert "idempotency_skipped" in result
        assert result["idempotency_skipped"] == 0

    def test_committed_entries_still_skipped(self, recovery, wal_dir):
        """COMMIT된 엔트리는 idempotency 체크 없이 스킵."""
        entries = [
            {
                "wal_sequence": 1,
                "operation": "add_integrity",
                "entry_data": {"integrity": {"sequence": 1}},
                "timestamp": datetime.now(UTC).isoformat(),
                "pod_id": "pod-1",
            },
            {
                "wal_sequence": 1,
                "operation": "COMMIT",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        ]
        self._create_wal_file(wal_dir, entries)

        with patch.object(recovery, "_is_duplicate_via_idempotency") as mock_check:
            result = recovery.recover_on_startup()

        assert result["entries_already_committed"] == 1
        assert result["entries_recovered"] == 0
        mock_check.assert_not_called()  # COMMIT된 것은 idempotency 체크 안함


class TestIdempotencyKeyBehavior:
    """IdempotencyKey 통합 테스트."""

    def test_for_wal_recovery_creates_valid_key(self):
        """for_wal_recovery가 유효한 키 생성."""
        from baldur.services.idempotency import IdempotencyKey

        key = IdempotencyKey.for_wal_recovery(
            wal_entry_id="seq_12345",
            operation="pg_insert",
        )

        # cache_key는 Redis/캐시에 사용 가능한 형식
        assert key.cache_key.startswith("idempotency:wal_recovery:")
        assert "seq_12345" in key.cache_key
        assert "pg_insert" in key.cache_key

    def test_for_wal_recovery_different_operations_different_keys(self):
        """다른 operation은 다른 키 생성."""
        from baldur.services.idempotency import IdempotencyKey

        key1 = IdempotencyKey.for_wal_recovery("1", "redis_replay")
        key2 = IdempotencyKey.for_wal_recovery("1", "pg_insert")

        assert key1.cache_key != key2.cache_key
        assert key1.key != key2.key

    def test_for_wal_recovery_same_inputs_same_key(self):
        """동일 입력은 동일 키 생성 (멱등성 보장)."""
        from baldur.services.idempotency import IdempotencyKey

        key1 = IdempotencyKey.for_wal_recovery("123", "redis_replay")
        key2 = IdempotencyKey.for_wal_recovery("123", "redis_replay")

        assert key1.cache_key == key2.cache_key
        assert key1.key == key2.key
        assert key1.hash == key2.hash
