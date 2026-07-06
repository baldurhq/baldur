"""
StartupHashChainSync 테스트.
"""

import json
from datetime import UTC, datetime


class TestStartupHashChainSync:
    """StartupHashChainSync 테스트."""

    def test_sync_fresh_start(self, mock_redis, temp_log_dir):
        """빈 상태에서 시작 테스트."""
        from baldur.audit.integrity import StartupHashChainSync

        sync = StartupHashChainSync(mock_redis, temp_log_dir, key_prefix="test:")

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "fresh_start"
        assert result["file_sequence"] == 0
        assert result["redis_sequence"] == 0

    def test_sync_redis_ahead(self, mock_redis, temp_log_dir):
        """Redis가 파일보다 앞선 경우 (정상 상태)."""
        from baldur.audit.integrity import StartupHashChainSync

        # Redis에 상태 설정
        mock_redis.set("test:audit:hash_chain:seq", 10)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "previous_hash": "redis_hash",
                "sequence": "10",
            },
        )

        sync = StartupHashChainSync(mock_redis, temp_log_dir, key_prefix="test:")

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "redis_ahead_ok"
        assert result["redis_sequence"] == 10
        assert result["file_sequence"] == 0

    def test_sync_file_ahead(self, mock_redis, temp_log_dir):
        """파일이 Redis보다 앞선 경우 (Redis 복구 필요)."""
        from baldur.audit.integrity import StartupHashChainSync, compute_hash

        # 파일에 엔트리 작성
        log_file = temp_log_dir / "audit_2026-01-18.jsonl"
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = {
                "event": f"event_{i}",
                "integrity": {
                    "sequence": i + 1,
                    "previous_hash": prev_hash,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            }
            current_hash = compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash
            entries.append(entry)
            prev_hash = current_hash

        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        # Redis는 비어있음 (시퀀스 0)
        sync = StartupHashChainSync(mock_redis, temp_log_dir, key_prefix="test:")

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "synced_redis_to_file"
        assert result["file_sequence"] == 5
        assert int(mock_redis.get("test:audit:hash_chain:seq")) == 5

    def test_sync_in_sync(self, mock_redis, temp_log_dir):
        """이미 동기화된 상태 테스트."""
        from baldur.audit.integrity import StartupHashChainSync

        # 파일에 엔트리 작성
        log_file = temp_log_dir / "audit_2026-01-18.jsonl"
        entry = {
            "event": "test",
            "integrity": {
                "sequence": 5,
                "previous_hash": "prev",
                "current_hash": "curr",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        }
        with open(log_file, "w") as f:
            f.write(json.dumps(entry) + "\n")

        # Redis도 동일 시퀀스
        mock_redis.set("test:audit:hash_chain:seq", 5)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "previous_hash": "curr",
                "sequence": "5",
            },
        )

        sync = StartupHashChainSync(mock_redis, temp_log_dir, key_prefix="test:")

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "in_sync"

    def test_sync_cleanup_pending(self, mock_redis, temp_log_dir):
        """PENDING 시퀀스 정리 테스트."""
        from baldur.audit.integrity import StartupHashChainSync

        # PENDING 상태 설정
        mock_redis.set("test:audit:hash_chain:pending:10", "hash10")
        mock_redis.set("test:audit:hash_chain:pending:11", "hash11")

        sync = StartupHashChainSync(mock_redis, temp_log_dir, key_prefix="test:")

        result = sync.sync()

        assert result["pending_cleaned"] == 2
        assert mock_redis.get("test:audit:hash_chain:pending:10") is None
        assert mock_redis.get("test:audit:hash_chain:orphaned:10") is not None

    def test_sync_idempotent(self, mock_redis, temp_log_dir):
        """동기화 멱등성 테스트 (한 번만 실행)."""
        from baldur.audit.integrity import StartupHashChainSync

        sync = StartupHashChainSync(mock_redis, temp_log_dir, key_prefix="test:")

        # 첫 실행
        result1 = sync.sync()
        assert result1["status"] == "success"

        # 재실행
        result2 = sync.sync()
        assert result2["status"] == "already_synced"
