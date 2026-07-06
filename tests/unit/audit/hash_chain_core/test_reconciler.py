"""
HashChainReconciler 테스트.
"""

import json
from datetime import UTC, datetime


class TestHashChainReconciler:
    """HashChainReconciler 테스트."""

    def test_reconcile_no_degraded(self, mock_redis, temp_log_dir):
        """degraded 엔트리 없을 때 테스트."""
        from baldur.audit.integrity import HashChainReconciler

        reconciler = HashChainReconciler(mock_redis, temp_log_dir, key_prefix="test:")

        result = reconciler.reconcile()

        assert result["status"] == "no_degraded_entries"
        assert result["degraded_entries_found"] == 0

    def test_reconcile_with_degraded_entries(self, mock_redis, temp_log_dir):
        """degraded 엔트리 병합 테스트."""
        from baldur.audit.integrity import HashChainReconciler

        # 파일에 degraded 엔트리 작성
        log_file = temp_log_dir / "audit_2026-01-18.jsonl"

        degraded_entries = []
        for i in range(3):
            entry = {
                "event": f"degraded_event_{i}",
                "integrity": {
                    "sequence": -1,
                    "previous_hash": "DEGRADED",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "degraded": True,
                    "current_hash": f"hash_{i}",
                },
            }
            degraded_entries.append(entry)

        with open(log_file, "w") as f:
            for entry in degraded_entries:
                f.write(json.dumps(entry) + "\n")

        # Redis 초기 상태
        mock_redis.set("test:audit:hash_chain:seq", 0)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "previous_hash": "GENESIS",
                "sequence": "0",
            },
        )

        reconciler = HashChainReconciler(mock_redis, temp_log_dir, key_prefix="test:")

        result = reconciler.reconcile()

        assert result["status"] == "success"
        assert result["degraded_entries_found"] == 3
        assert result["entries_merged"] == 3
        assert result["new_sequence_start"] == 1
        assert result["new_sequence_end"] == 3

        assert int(mock_redis.get("test:audit:hash_chain:seq")) == 3

    def test_reconcile_skips_already_reconciled(self, mock_redis, temp_log_dir):
        """이미 reconciled된 엔트리는 건너뛰기 테스트."""
        from baldur.audit.integrity import HashChainReconciler

        log_file = temp_log_dir / "audit_2026-01-18.jsonl"

        entries = [
            {
                "event": "already_reconciled",
                "integrity": {
                    "sequence": 1,
                    "previous_hash": "GENESIS",
                    "degraded": True,
                    "reconciled": True,
                    "current_hash": "hash1",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            },
            {
                "event": "needs_reconciliation",
                "integrity": {
                    "sequence": -1,
                    "previous_hash": "DEGRADED",
                    "degraded": True,
                    "current_hash": "hash2",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            },
        ]

        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        reconciler = HashChainReconciler(mock_redis, temp_log_dir, key_prefix="test:")

        result = reconciler.reconcile()

        assert result["degraded_entries_found"] == 1
        assert result["entries_merged"] == 1

    def test_reconcile_preserves_chain_continuity(self, mock_redis, temp_log_dir):
        """병합 후 체인 연속성 보장 테스트."""
        from baldur.audit.integrity import HashChainReconciler

        # 기존 체인 상태
        existing_hash = "existing_chain_hash"
        mock_redis.set("test:audit:hash_chain:seq", 5)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "previous_hash": existing_hash,
                "sequence": "5",
            },
        )

        # degraded 엔트리
        log_file = temp_log_dir / "audit_2026-01-18.jsonl"
        entry = {
            "event": "degraded_event",
            "integrity": {
                "sequence": -1,
                "previous_hash": "DEGRADED",
                "timestamp": datetime.now(UTC).isoformat(),
                "degraded": True,
                "current_hash": "temp_hash",
            },
        }
        with open(log_file, "w") as f:
            f.write(json.dumps(entry) + "\n")

        reconciler = HashChainReconciler(mock_redis, temp_log_dir, key_prefix="test:")

        result = reconciler.reconcile()

        # 새 시퀀스는 기존 체인 이어서 시작
        assert result["new_sequence_start"] == 6
        assert result["new_sequence_end"] == 6

    def test_get_stats(self, mock_redis, temp_log_dir):
        """통계 조회 테스트."""
        from baldur.audit.integrity import HashChainReconciler

        reconciler = HashChainReconciler(mock_redis, temp_log_dir, key_prefix="test:")

        # 초기 상태
        stats = reconciler.get_stats()
        assert stats["last_reconciliation"] is None
        assert str(temp_log_dir) in stats["log_dir"]

        # reconcile 후
        reconciler.reconcile()
        stats = reconciler.get_stats()
