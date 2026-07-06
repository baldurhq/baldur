"""
HashChainGracefulDegradationManager 및 통합 테스트.

통합 액세스 인터페이스 및 장애 복구 시나리오 테스트:
- 단일 진입점을 통한 모든 graceful degradation 컴포넌트 접근
- 전체 장애 → 복구 사이클 테스트
- CircuitBreaker + FallbackChain + WALRecovery 연동
"""


class TestHashChainGracefulDegradationManager:
    """Tests for HashChainGracefulDegradationManager."""

    def setup_method(self):
        """Reset singleton before each test."""
        from baldur.audit.graceful_degradation import HashChainDegradationManager

        HashChainDegradationManager.reset_instance()

    def teardown_method(self):
        """Reset singleton after each test."""
        from baldur.audit.graceful_degradation import HashChainDegradationManager

        HashChainDegradationManager.reset_instance()

    def test_initialization(self, mock_redis, temp_dir):
        """Test manager initialization."""
        from baldur.audit.graceful_degradation import (
            HashChainGracefulDegradationManager,
        )

        manager = HashChainGracefulDegradationManager(
            redis_client=mock_redis,
            wal_dir=temp_dir,
        )

        assert manager._initialized is False

        manager.initialize()

        assert manager._initialized is True
        assert manager._fallback_chain is not None
        assert manager._degraded_marker is not None
        assert manager._wal_recovery is not None
        assert manager._circuit_breaker is not None

    def test_recover_on_startup(self, mock_redis, temp_dir):
        """Test startup recovery."""
        from baldur.audit.graceful_degradation import (
            HashChainGracefulDegradationManager,
        )

        manager = HashChainGracefulDegradationManager(
            redis_client=mock_redis,
            wal_dir=temp_dir,
        )

        result = manager.recover_on_startup()

        assert result["status"] == "success"
        assert "wal_recovery" in result

    def test_add_integrity_with_fallback(self, mock_redis, temp_dir, sample_entry):
        """Test adding integrity with fallback."""
        from baldur.audit.graceful_degradation import (
            HashChainGracefulDegradationManager,
        )

        manager = HashChainGracefulDegradationManager(
            redis_client=mock_redis,
            wal_dir=temp_dir,
        )
        manager.initialize()

        result = manager.add_integrity_with_fallback(sample_entry)

        assert "integrity" in result
        assert "current_hash" in result["integrity"]

    def test_degradation_level_property(self, mock_redis, temp_dir):
        """Test degradation level property."""
        from baldur.audit.graceful_degradation import (
            DegradationLevel,
            HashChainGracefulDegradationManager,
        )

        manager = HashChainGracefulDegradationManager(
            redis_client=mock_redis,
            wal_dir=temp_dir,
        )
        manager.initialize()

        assert manager.degradation_level == DegradationLevel.NORMAL
        assert manager.is_degraded is False

    def test_get_status(self, mock_redis, temp_dir):
        """Test status retrieval."""
        from baldur.audit.graceful_degradation import (
            HashChainGracefulDegradationManager,
        )

        manager = HashChainGracefulDegradationManager(
            redis_client=mock_redis,
            wal_dir=temp_dir,
        )
        manager.initialize()

        status = manager.get_status()

        assert status["initialized"] is True
        assert "circuit_breaker" in status
        assert "fallback_chain" in status
        assert "degraded_marker" in status


class TestGracefulDegradationIntegration:
    """Graceful Degradation 통합 테스트 - 장애 → 복구 전체 흐름."""

    def setup_method(self):
        """Reset singleton before each test."""
        from baldur.audit.graceful_degradation import HashChainDegradationManager

        HashChainDegradationManager.reset_instance()

    def teardown_method(self):
        """Reset singleton after each test."""
        from baldur.audit.graceful_degradation import HashChainDegradationManager

        HashChainDegradationManager.reset_instance()

    def test_full_failure_recovery_cycle(self, temp_dir):
        """Test complete failure and recovery cycle."""
        from baldur.audit.graceful_degradation import (
            DegradationLevel,
            HashChainGracefulDegradationManager,
        )

        from .conftest import MockRedisClient

        mock_redis = MockRedisClient()

        manager = HashChainGracefulDegradationManager(
            redis_client=mock_redis,
            wal_dir=temp_dir,
            local_fallback_path=temp_dir / "fallback.jsonl",
        )
        manager.initialize()

        # 1. Normal operation
        entry1 = {"event": "test1"}
        manager.add_integrity_with_fallback(entry1)
        assert manager.degradation_level == DegradationLevel.NORMAL

        # 2. Simulate Redis failure
        mock_redis.set_should_fail(True)

        # Trigger circuit breaker
        for _ in range(5):
            manager._circuit_breaker.record_failure()

        # 3. Operation during failure (uses fallback)
        entry2 = {"event": "test2_during_failure"}
        result2 = manager.add_integrity_with_fallback(entry2)

        assert result2.get("integrity", {}).get("degraded") is True

        # 4. Redis recovers
        mock_redis.set_should_fail(False)
        manager._circuit_breaker.force_closed()

        # 5. Normal operation resumed
        entry3 = {"event": "test3_after_recovery"}
        manager.add_integrity_with_fallback(entry3)

        assert manager.degradation_level == DegradationLevel.NORMAL

        manager.close()

    def test_wal_protects_against_crash(self, temp_dir):
        """Test WAL provides crash protection."""
        from baldur.audit.graceful_degradation import HashChainWALRecovery

        from .conftest import MockRedisClient

        mock_redis = MockRedisClient()

        # Session 1: Write entry, crash before commit
        recovery1 = HashChainWALRecovery(
            wal_dir=temp_dir,
            redis_client=mock_redis,
        )

        entry = {"integrity": {"sequence": 10, "current_hash": "abc123"}}
        recovery1.write_wal_entry("add_integrity", entry)
        # Simulate crash - no commit
        recovery1.close()

        # Session 2: Recover from WAL
        recovery2 = HashChainWALRecovery(
            wal_dir=temp_dir,
            redis_client=mock_redis,
        )

        result = recovery2.recover_on_startup()

        assert result["entries_recovered"] == 1

        # Verify Redis was updated
        seq = mock_redis._data.get("baldur:audit:hash_chain:seq")
        assert seq == 10

        recovery2.close()

    def test_circuit_breaker_prevents_cascade(self, temp_dir):
        """Test circuit breaker prevents cascading failures."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            HashChainGracefulDegradationManager,
        )

        from .conftest import MockRedisClient

        mock_redis = MockRedisClient()

        manager = HashChainGracefulDegradationManager(
            redis_client=mock_redis,
            wal_dir=temp_dir,
        )
        manager.initialize()

        # Make Redis fail
        mock_redis.set_should_fail(True)

        # Trigger failures
        for _ in range(5):
            manager._circuit_breaker.record_failure()

        assert manager._circuit_breaker.state == CircuitState.OPEN

        # Operations should use fallback without hitting Redis
        entry = {"event": "test"}
        result = manager.add_integrity_with_fallback(entry)

        # Entry should be processed via fallback
        assert result.get("integrity", {}).get("degraded") is True

        manager.close()

    def test_degraded_entries_tracked_for_reconciliation(self, temp_dir):
        """Test degraded entries are tracked for later reconciliation."""
        from baldur.audit.graceful_degradation import (
            HashChainGracefulDegradationManager,
        )

        manager = HashChainGracefulDegradationManager(
            redis_client=None,  # No Redis = always degraded
            wal_dir=temp_dir,
        )
        manager.initialize()

        # Add entries in degraded mode
        for i in range(3):
            entry = {"event": f"test_{i}"}
            manager.add_integrity_with_fallback(entry)

        # Check tracking
        marker = manager._degraded_marker
        unreconciled = marker.get_unreconciled_count()

        assert unreconciled == 3

        manager.close()
