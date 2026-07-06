"""
RedisHashChainManager 단위 테스트.

분산 해시 체인의 핵심 기능 테스트:
1. Redis 정상 모드에서 무결성 추가
2. Redis 장애 시 fallback 동작
3. 동시 쓰기 시 시퀀스 원자성
4. 해시 체인 연속성 검증
5. 상태 조회 및 통계

Refactored to use Factory Pattern (Phase 4):
- MockRedisClient → factories.MockRedisClient
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from baldur.audit.integrity import (
    HashChainManager,
    HashChainManagerProtocol,
    HashChainVerifier,
    RedisHashChainManager,
    create_hash_chain_manager,
)

# Factory Pattern imports
from tests.factories import MockRedisClient

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_redis():
    """Create mock Redis client."""
    return MockRedisClient()


@pytest.fixture
def failing_redis():
    """Create failing mock Redis client."""
    return MockRedisClient(should_fail=True)


@pytest.fixture
def temp_state_file(tmp_path):
    """Create temporary state file path."""
    return tmp_path / ".hash_chain_state.json"


@pytest.fixture
def local_hash_chain_manager(temp_state_file):
    """Create local HashChainManager."""
    return HashChainManager(state_file=temp_state_file)


@pytest.fixture
def redis_hash_chain_manager(mock_redis, local_hash_chain_manager):
    """Create RedisHashChainManager with mock Redis."""
    return RedisHashChainManager(
        redis_client=mock_redis,
        key_prefix="test:",
        fallback_manager=local_hash_chain_manager,
    )


# =============================================================================
# Test: RedisHashChainManager Basic Functionality
# =============================================================================


class TestRedisHashChainManagerBasic:
    """RedisHashChainManager 기본 기능 테스트."""

    def test_add_integrity_adds_fields(self, redis_hash_chain_manager):
        """무결성 필드가 올바르게 추가되는지 확인."""
        entry = {"event": "test_event", "data": "test_data"}

        result = redis_hash_chain_manager.add_integrity(entry)

        assert "integrity" in result
        assert result["integrity"]["sequence"] == 1
        assert result["integrity"]["previous_hash"] == "GENESIS"
        assert "current_hash" in result["integrity"]
        assert "timestamp" in result["integrity"]
        assert "pod_id" in result["integrity"]

    def test_add_integrity_increments_sequence(self, redis_hash_chain_manager):
        """시퀀스가 순차적으로 증가하는지 확인."""
        entries = [
            {"event": "test_1"},
            {"event": "test_2"},
            {"event": "test_3"},
        ]

        results = [redis_hash_chain_manager.add_integrity(e) for e in entries]

        sequences = [r["integrity"]["sequence"] for r in results]
        assert sequences == [1, 2, 3]

    def test_add_integrity_chains_hashes(self, redis_hash_chain_manager):
        """해시가 체인으로 연결되는지 확인."""
        entry1 = redis_hash_chain_manager.add_integrity({"event": "first"})
        entry2 = redis_hash_chain_manager.add_integrity({"event": "second"})

        # Second entry's previous_hash should be first entry's current_hash
        assert (
            entry2["integrity"]["previous_hash"] == entry1["integrity"]["current_hash"]
        )

    def test_genesis_hash(self, redis_hash_chain_manager):
        """첫 번째 엔트리의 previous_hash가 GENESIS인지 확인."""
        entry = redis_hash_chain_manager.add_integrity({"event": "first"})

        assert entry["integrity"]["previous_hash"] == "GENESIS"

    def test_current_hash_is_valid(self, redis_hash_chain_manager):
        """current_hash가 유효한 SHA-256 형식인지 확인."""
        entry = redis_hash_chain_manager.add_integrity({"event": "test"})

        current_hash = entry["integrity"]["current_hash"]

        # SHA-256 is 64 hex characters
        assert len(current_hash) == 64
        assert all(c in "0123456789abcdef" for c in current_hash)


# =============================================================================
# Test: Fallback Behavior
# =============================================================================


class TestRedisHashChainManagerFallback:
    """Redis 장애 시 fallback 동작 테스트."""

    def test_fallback_on_redis_failure(self, failing_redis, local_hash_chain_manager):
        """Redis 장애 시 로컬 fallback 사용."""
        manager = RedisHashChainManager(
            redis_client=failing_redis,
            key_prefix="test:",
            fallback_manager=local_hash_chain_manager,
        )

        entry = manager.add_integrity({"event": "test"})

        assert "integrity" in entry
        assert entry["integrity"]["sequence"] > 0
        # Fallback should mark as degraded
        assert entry["integrity"].get("degraded") is True

    def test_fallback_without_manager(self, failing_redis):
        """Fallback 매니저 없이 Redis 장애 시 최소 무결성 정보 추가."""
        manager = RedisHashChainManager(
            redis_client=failing_redis,
            key_prefix="test:",
            fallback_manager=None,
        )

        entry = manager.add_integrity({"event": "test"})

        assert "integrity" in entry
        assert entry["integrity"]["sequence"] == -1  # Indicates degraded
        assert entry["integrity"]["previous_hash"] == "DEGRADED"
        assert entry["integrity"]["degraded"] is True

    def test_fallback_increments_stats(self, failing_redis, local_hash_chain_manager):
        """Fallback 사용 시 통계가 증가하는지 확인."""
        manager = RedisHashChainManager(
            redis_client=failing_redis,
            key_prefix="test:",
            fallback_manager=local_hash_chain_manager,
        )

        manager.add_integrity({"event": "test1"})
        manager.add_integrity({"event": "test2"})

        stats = manager.get_stats()
        assert stats["fallback_writes"] == 2
        assert stats["redis_writes"] == 0


# =============================================================================
# Test: Concurrent Writes
# =============================================================================


class TestRedisHashChainManagerConcurrency:
    """동시 쓰기 테스트."""

    def test_concurrent_writes_unique_sequences(self, mock_redis):
        """동시 쓰기 시 모든 시퀀스가 고유한지 확인."""
        manager = RedisHashChainManager(
            redis_client=mock_redis,
            key_prefix="test:",
        )

        num_entries = 50
        entries = [{"event": f"concurrent_{i}"} for i in range(num_entries)]

        # Use ThreadPoolExecutor for concurrent writes
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(manager.add_integrity, entries))

        # All sequences should be unique
        sequences = [r["integrity"]["sequence"] for r in results]
        assert len(sequences) == len(set(sequences)), "Duplicate sequences found!"

        # All sequences should be present (1 to num_entries)
        assert sorted(sequences) == list(range(1, num_entries + 1))

    def test_concurrent_writes_chain_integrity(self, mock_redis):
        """동시 쓰기 후 체인 무결성 확인."""
        manager = RedisHashChainManager(
            redis_client=mock_redis,
            key_prefix="test:",
        )

        num_entries = 20
        entries = [{"event": f"test_{i}"} for i in range(num_entries)]

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(manager.add_integrity, entries))

        # Sort by sequence for verification
        sorted_results = sorted(results, key=lambda r: r["integrity"]["sequence"])

        # Verify chain is valid
        verifier = HashChainVerifier()
        is_valid, error_msg = verifier.verify_chain(sorted_results)

        assert is_valid, f"Chain verification failed: {error_msg}"


# =============================================================================
# Test: State Management
# =============================================================================


class TestRedisHashChainManagerState:
    """상태 관리 테스트."""

    def test_get_state(self, redis_hash_chain_manager):
        """상태 조회 테스트."""
        # Add some entries
        redis_hash_chain_manager.add_integrity({"event": "test1"})
        redis_hash_chain_manager.add_integrity({"event": "test2"})

        state = redis_hash_chain_manager.get_state()

        assert state["sequence"] == 2
        assert state["source"] == "redis"
        assert "previous_hash" in state

    def test_get_state_fallback(self, failing_redis, local_hash_chain_manager):
        """Redis 장애 시 fallback 상태 조회."""
        # First add entry to local manager
        local_hash_chain_manager.add_integrity({"event": "local"})

        manager = RedisHashChainManager(
            redis_client=failing_redis,
            key_prefix="test:",
            fallback_manager=local_hash_chain_manager,
        )

        state = manager.get_state()

        assert state["source"] == "fallback"
        assert state["sequence"] == 1

    def test_get_stats(self, redis_hash_chain_manager):
        """통계 조회 테스트."""
        redis_hash_chain_manager.add_integrity({"event": "test"})

        stats = redis_hash_chain_manager.get_stats()

        assert "redis_writes" in stats
        assert "fallback_writes" in stats
        assert "lock_failures" in stats
        assert "state" in stats
        assert stats["redis_writes"] == 1


# =============================================================================
# Test: Verification
# =============================================================================


class TestRedisHashChainManagerVerification:
    """검증 기능 테스트."""

    def test_verify_continuity_valid_chain(self, redis_hash_chain_manager):
        """유효한 체인 검증."""
        entries = []
        for i in range(5):
            entry = redis_hash_chain_manager.add_integrity({"event": f"test_{i}"})
            entries.append(entry)

        is_valid, error_msg = redis_hash_chain_manager.verify_continuity(entries)

        assert is_valid
        assert error_msg is None

    def test_verify_continuity_detects_tampering(self, redis_hash_chain_manager):
        """위변조 감지 테스트."""
        entries = []
        for i in range(3):
            entry = redis_hash_chain_manager.add_integrity({"event": f"test_{i}"})
            entries.append(entry)

        # Tamper with middle entry
        entries[1]["event"] = "TAMPERED"

        is_valid, error_msg = redis_hash_chain_manager.verify_continuity(entries)

        assert not is_valid
        assert "hash mismatch" in error_msg.lower()

    def test_verify_continuity_detects_missing_entry(self, redis_hash_chain_manager):
        """누락된 엔트리 감지 테스트."""
        entries = []
        for i in range(5):
            entry = redis_hash_chain_manager.add_integrity({"event": f"test_{i}"})
            entries.append(entry)

        # Remove middle entry
        del entries[2]

        is_valid, error_msg = redis_hash_chain_manager.verify_continuity(entries)

        assert not is_valid
        assert "missing" in error_msg.lower() or "sequence" in error_msg.lower()


# =============================================================================
# Test: Reset Functionality
# =============================================================================


class TestRedisHashChainManagerReset:
    """리셋 기능 테스트."""

    def test_reset_clears_state(self, mock_redis, local_hash_chain_manager):
        """리셋이 상태를 초기화하는지 확인."""
        manager = RedisHashChainManager(
            redis_client=mock_redis,
            key_prefix="test:",
            fallback_manager=local_hash_chain_manager,
        )

        # Add entries
        manager.add_integrity({"event": "test1"})
        manager.add_integrity({"event": "test2"})

        # Reset
        manager.reset()

        # State should be reset
        state = manager.get_state()
        assert state["sequence"] == 0
        assert state["previous_hash"] == "GENESIS"


# =============================================================================
# Test: Factory Function
# =============================================================================


class TestCreateHashChainManager:
    """create_hash_chain_manager 팩토리 함수 테스트."""

    def test_create_local_manager(self, temp_state_file):
        """로컬 매니저 생성 테스트."""
        manager = create_hash_chain_manager(
            distributed=False,
            state_file=temp_state_file,
        )

        assert isinstance(manager, HashChainManager)

    def test_create_distributed_manager(self, mock_redis, temp_state_file):
        """분산 매니저 생성 테스트."""
        manager = create_hash_chain_manager(
            distributed=True,
            redis_client=mock_redis,
            key_prefix="test:",
            state_file=temp_state_file,
        )

        assert isinstance(manager, RedisHashChainManager)

    def test_create_distributed_without_redis_falls_back(self, temp_state_file):
        """Redis 없이 분산 모드 요청 시 로컬로 fallback."""
        manager = create_hash_chain_manager(
            distributed=True,
            redis_client=None,  # No Redis
            state_file=temp_state_file,
        )

        # Should fall back to local manager
        assert isinstance(manager, HashChainManager)


# =============================================================================
# Test: Protocol Compliance
# =============================================================================


class TestHashChainManagerProtocol:
    """HashChainManagerProtocol 준수 테스트."""

    def test_local_manager_implements_protocol(self, local_hash_chain_manager):
        """HashChainManager가 Protocol을 구현하는지 확인."""
        assert isinstance(local_hash_chain_manager, HashChainManagerProtocol)

    def test_redis_manager_implements_protocol(self, redis_hash_chain_manager):
        """RedisHashChainManager가 Protocol을 구현하는지 확인."""
        assert isinstance(redis_hash_chain_manager, HashChainManagerProtocol)


# =============================================================================
# Test: Edge Cases
# =============================================================================


class TestEdgeCases:
    """엣지 케이스 테스트."""

    def test_empty_entry(self, redis_hash_chain_manager):
        """빈 엔트리 처리."""
        entry = {}
        result = redis_hash_chain_manager.add_integrity(entry)

        assert "integrity" in result
        assert result["integrity"]["sequence"] == 1

    def test_nested_entry(self, redis_hash_chain_manager):
        """중첩된 데이터 구조 처리."""
        entry = {"event": "complex", "nested": {"level1": {"level2": ["a", "b", "c"]}}}
        result = redis_hash_chain_manager.add_integrity(entry)

        assert "integrity" in result
        assert result["nested"]["level1"]["level2"] == ["a", "b", "c"]

    def test_special_characters(self, redis_hash_chain_manager):
        """특수 문자 처리."""
        entry = {
            "event": "special",
            "data": "한글 테스트 🎉 <script>alert('xss')</script>",
        }
        result = redis_hash_chain_manager.add_integrity(entry)

        assert "integrity" in result
        assert "current_hash" in result["integrity"]

    def test_large_entry(self, redis_hash_chain_manager):
        """대용량 엔트리 처리."""
        large_data = "x" * 100000  # 100KB
        entry = {"event": "large", "data": large_data}

        result = redis_hash_chain_manager.add_integrity(entry)

        assert "integrity" in result
        assert len(result["data"]) == 100000
