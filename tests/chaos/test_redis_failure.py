"""
Redis 장애 시나리오 Chaos 테스트.

Redis 장애 상황에서 시스템 동작 검증:
- 로컬 스토리지로 자동 폴백
- 장애 중 엔트리 degraded 마킹
- Redis 복구 후 재조정(reconciliation)
- 서킷 브레이커 동작

실제 장애 시나리오 시뮬레이션:
- Redis 타임아웃
- Redis 연결 거부
- Redis 간헐적 장애
- 장기 중단 후 Redis 복구

Related code:
    baldur/audit/graceful_degradation.py
    baldur/audit/integrity.py
"""

from __future__ import annotations

import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


@dataclass
class CircuitBreakerConfig:
    """Test configuration for circuit breaker."""

    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_requests: int = 3
    success_threshold: int = 2


from baldur.audit.graceful_degradation import (
    CircuitState,
    DegradationLevel,
    FallbackConfig,
    HashChainCircuitBreaker,
    HashChainDegradationManager,
    HashChainFallbackChain,
)
from baldur.audit.graceful_degradation import (
    HashChainCircuitBreakerConfig as RealCircuitBreakerConfig,
)

# =============================================================================
# Chaos Mock Redis - Simulates Various Failure Modes
# =============================================================================


class ChaosRedisClient:
    """
    Redis client that can simulate various failure modes.

    Supports:
    - Complete failure (connection refused)
    - Timeout (slow responses)
    - Intermittent failures (random failures)
    - Delayed recovery
    """

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

        # Failure modes
        self._failure_mode: str | None = None
        self._failure_probability = 0.0
        self._timeout_seconds = 0.0
        self._call_count = 0
        self._failure_after_calls = None

    # =========================================================================
    # Failure Mode Configuration
    # =========================================================================

    def set_complete_failure(self):
        """Simulate complete Redis unavailability."""
        self._failure_mode = "complete"

    def set_timeout_failure(self, seconds: float = 5.0):
        """Simulate slow Redis (timeout scenario)."""
        self._failure_mode = "timeout"
        self._timeout_seconds = seconds

    def set_intermittent_failure(self, probability: float = 0.5):
        """Simulate random failures with given probability."""
        self._failure_mode = "intermittent"
        self._failure_probability = probability

    def set_failure_after_n_calls(self, n: int):
        """Fail after N successful calls."""
        self._failure_after_calls = n
        self._call_count = 0

    def recover(self):
        """Restore normal operation."""
        self._failure_mode = None
        self._failure_probability = 0.0
        self._timeout_seconds = 0.0
        self._failure_after_calls = None
        self._call_count = 0

    # =========================================================================
    # Failure Check
    # =========================================================================

    def _check_failure(self):
        """Check if operation should fail based on current mode."""
        self._call_count += 1

        # Check call count failure
        if self._failure_after_calls is not None:
            if self._call_count > self._failure_after_calls:
                raise ConnectionError("Redis failed after N calls")

        if self._failure_mode == "complete":
            raise ConnectionError("Redis connection refused")

        elif self._failure_mode == "timeout":
            time.sleep(self._timeout_seconds)
            raise TimeoutError(f"Redis timeout after {self._timeout_seconds}s")

        elif self._failure_mode == "intermittent":
            import random

            if random.random() < self._failure_probability:
                raise ConnectionError("Redis intermittent failure")

    # =========================================================================
    # Redis Operations
    # =========================================================================

    def get(self, key: str) -> bytes | None:
        self._check_failure()
        value = self._data.get(key)
        return str(value).encode() if value is not None else None

    def set(
        self, key: str, value: Any, nx: bool = False, ex: int = None, px: int = None
    ) -> bool:
        """Set key with optional expiration. ex=seconds, px=milliseconds."""
        self._check_failure()
        with self._lock:
            if nx and key in self._data:
                return False
            self._data[key] = value
            return True

    def delete(self, *keys: str) -> int:
        self._check_failure()
        count = 0
        for key in keys:
            if key in self._data:
                del self._data[key]
                count += 1
        return count

    def incr(self, key: str) -> int:
        self._check_failure()
        with self._lock:
            current = int(self._data.get(key, 0))
            new_value = current + 1
            self._data[key] = new_value
            return new_value

    def hget(self, key: str, field: str) -> bytes | None:
        self._check_failure()
        hash_data = self._hashes.get(key, {})
        value = hash_data.get(field)
        return str(value).encode() if value is not None else None

    def hset(self, key: str, mapping: dict[str, Any] = None, **kwargs) -> int:
        self._check_failure()
        if mapping is None:
            mapping = kwargs
        with self._lock:
            if key not in self._hashes:
                self._hashes[key] = {}
            self._hashes[key].update({str(k): str(v) for k, v in mapping.items()})
            return len(mapping)

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        self._check_failure()
        hash_data = self._hashes.get(key, {})
        return {k.encode(): v.encode() for k, v in hash_data.items()}

    def expire(self, key: str, seconds: int) -> int:
        return 1

    def ping(self) -> bool:
        self._check_failure()
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args) -> Any:
        """Mock eval for Lua scripts (used by distributed locks)."""
        self._check_failure()
        # Simple mock: if script contains DEL, delete the key
        if "DEL" in script.upper() or "del" in script:
            if keys_and_args:
                key = keys_and_args[0]
                return self.delete(key)
        return 1

    def pipeline(self, transaction: bool = True) -> MockPipeline:
        return MockPipeline(self)


class MockPipeline:
    """Mock Redis pipeline for chaos testing."""

    def __init__(self, redis: ChaosRedisClient):
        self._redis = redis
        self._commands: list[tuple] = []

    def set(self, key: str, value: Any) -> MockPipeline:
        self._commands.append(("set", key, value))
        return self

    def hset(self, key: str, mapping: dict = None, **kwargs) -> MockPipeline:
        self._commands.append(("hset", key, mapping or kwargs))
        return self

    def execute(self) -> list[Any]:
        results = []
        for cmd in self._commands:
            if cmd[0] == "set":
                self._redis.set(cmd[1], cmd[2])
                results.append(True)
            elif cmd[0] == "hset":
                self._redis.hset(cmd[1], cmd[2])
                results.append(1)
        return results


class MockDistributedLock:
    """Mock distributed lock for testing."""

    def __init__(self, *args, **kwargs):
        pass

    def acquire(self, blocking: bool = True) -> bool:
        return True

    def release(self) -> None:
        pass


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def chaos_redis():
    """Create a chaos Redis client."""
    return ChaosRedisClient()


@pytest.fixture
def temp_fallback_dir():
    """Create temp directory for fallback files."""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    # Close disk buffer before temp dir is removed (Windows file locking)
    try:
        from baldur.audit.persistence.disk_buffer import reset_disk_buffer

        reset_disk_buffer()
    except Exception:
        pass
    # Force GC to close file handles held by fallback chain objects (Windows)
    import gc

    gc.collect()
    # Retry cleanup to handle lingering file locks on Windows
    import shutil
    import time as _time

    for attempt in range(3):
        try:
            shutil.rmtree(tmpdir, ignore_errors=False)
            break
        except PermissionError:
            gc.collect()
            _time.sleep(0.1 * (attempt + 1))
    else:
        shutil.rmtree(tmpdir, ignore_errors=True)


# =============================================================================
# Test: Complete Redis Failure
# =============================================================================


class TestCompleteRedisFailure:
    """Tests for complete Redis unavailability."""

    def test_fallback_to_local_on_redis_failure(self, chaos_redis, temp_fallback_dir):
        """
        When Redis fails completely, should fallback to local storage.

        Expected: Entries written to local with degraded=True
        """
        config = FallbackConfig(
            local_file_path=temp_fallback_dir / "fallback.jsonl",
        )

        fallback = HashChainFallbackChain(
            redis_primary=chaos_redis,
            config=config,
        )

        # Start Redis failure
        chaos_redis.set_complete_failure()

        # Write entry - should fallback to local
        entry = {"event": "test", "data": {"value": 1}}

        with patch(
            "baldur.adapters.cache.redis_adapter.RedisDistributedLock",
            MockDistributedLock,
        ):
            result = fallback.add_integrity(entry)

        # Verify fallback occurred
        assert result["integrity"]["degraded"] is True
        assert result["integrity"]["tier"] in ["local", "memory"]
        assert fallback.current_tier in ["local", "memory"]

        stats = fallback.get_stats()
        assert stats["fallback_events"] >= 1

    def test_multiple_entries_during_failure(self, chaos_redis, temp_fallback_dir):
        """
        Multiple entries during Redis failure maintain local chain.
        """
        config = FallbackConfig(
            local_file_path=temp_fallback_dir / "fallback.jsonl",
        )

        fallback = HashChainFallbackChain(config=config)  # No Redis

        entries = []
        for i in range(5):
            entry = {"event": f"test_{i}", "data": {"index": i}}
            result = fallback.add_integrity(entry)
            entries.append(result)

        # Verify local chain continuity
        for i, entry in enumerate(entries):
            assert entry["integrity"]["sequence"] == i + 1
            assert entry["integrity"]["degraded"] is True

            if i > 0:
                # Each entry should link to previous
                prev_hash = entries[i - 1]["integrity"]["current_hash"]
                assert entry["integrity"]["previous_hash"] == prev_hash

    def test_local_tier_when_redis_unavailable(self, chaos_redis):
        """
        When Redis is unavailable, fallback to local storage.

        Actual behavior: Local file tier is used when Redis fails.
        Memory tier is only used when local file path is also inaccessible.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            config = FallbackConfig(
                local_file_path=Path(tmpdir) / "fallback.jsonl",
                memory_max_entries=100,
            )

            fallback = HashChainFallbackChain(config=config)  # No Redis

            try:
                entry = {"event": "test", "data": {}}
                result = fallback.add_integrity(entry)

                # With valid local path, writes go to local tier
                assert result["integrity"]["tier"] == "local"
                assert result["integrity"]["degraded"] is True
            finally:
                # Close file handle to allow cleanup on Windows
                fallback.close()


# =============================================================================
# Test: Redis Recovery
# =============================================================================


class TestRedisRecovery:
    """Tests for behavior when Redis recovers."""

    def test_resumes_normal_operation_after_recovery(
        self, chaos_redis, temp_fallback_dir
    ):
        """
        After Redis recovers, should resume normal operation.

        Note: The fallback chain detects Redis availability and routes
        writes appropriately. Recovery testing verifies tier transitions.
        """
        config = FallbackConfig(
            local_file_path=temp_fallback_dir / "fallback.jsonl",
        )

        # Start without Redis (degraded mode)
        fallback = HashChainFallbackChain(config=config)

        try:
            # Write during "outage" - should go to local
            entry1 = fallback.add_integrity({"event": "degraded"})
            assert entry1["integrity"]["degraded"] is True
            assert entry1["integrity"]["tier"] == "local"
        finally:
            # Close file handle to allow cleanup on Windows
            fallback.close()

        # Simulate "recovery" by creating new fallback with Redis
        chaos_redis.set("test:audit:hash_chain:seq", 10)
        chaos_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "previous_hash": "recovered_hash",
                "sequence": "10",
            },
        )

        # Create new fallback chain with Redis
        fallback_recovered = HashChainFallbackChain(
            redis_primary=chaos_redis,
            config=config,
        )

        try:
            # Verify Redis is being used for primary writes
            entry2 = fallback_recovered.add_integrity({"event": "recovered"})
            # With functioning Redis, should use primary tier
            assert entry2["integrity"]["tier"] == "redis_primary"
            assert entry2["integrity"].get("degraded", False) is False
        finally:
            fallback_recovered.close()

    def test_degraded_entries_tracked_in_memory_buffer(
        self, chaos_redis, temp_fallback_dir
    ):
        """
        Memory tier entries are tracked in buffer for reconciliation.

        Note: get_degraded_entries() only returns entries from memory buffer,
        NOT from local file tier. Local file entries must be read from file.
        To reach memory tier, local must also fail.
        """
        from unittest.mock import patch

        config = FallbackConfig(
            local_file_path=None,
            memory_max_entries=100,
        )

        fallback = HashChainFallbackChain(config=config)  # No Redis

        # Mock local tier to fail, forcing memory tier
        with patch.object(
            fallback,
            "_add_integrity_local",
            side_effect=RuntimeError("Local storage unavailable"),
        ):
            # Also mock disk buffer to fail so we reach the volatile memory tier
            with patch(
                "baldur.audit.persistence.disk_buffer.get_disk_buffer",
                side_effect=Exception("DiskBuffer unavailable"),
            ):
                # Write entries during "outage" - goes to memory
                for i in range(5):
                    fallback.add_integrity({"event": f"degraded_{i}"})

        # Get degraded entries from memory buffer
        degraded = fallback.get_degraded_entries()

        assert len(degraded) == 5
        for entry in degraded:
            assert entry["integrity"]["degraded"] is True
            assert entry["integrity"]["tier"] == "memory"


# =============================================================================
# Test: Circuit Breaker Behavior
# =============================================================================


class TestCircuitBreakerChaos:
    """Tests for circuit breaker under chaos conditions."""

    def test_circuit_opens_after_threshold_failures(self, chaos_redis):
        """
        Circuit breaker should open after consecutive failures.
        """
        config = RealCircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout_seconds=0.1,
        )

        # Create manager for this test
        HashChainDegradationManager.reset_instance()
        manager = HashChainDegradationManager(redis_client=None)

        circuit = HashChainCircuitBreaker(
            config=config,
            degradation_manager=manager,
        )

        assert circuit.state == CircuitState.CLOSED

        # Record failures
        for _ in range(3):
            circuit.record_failure()

        # Circuit should open
        assert circuit.state == CircuitState.OPEN

        # Cleanup
        HashChainDegradationManager.reset_instance()

    def test_circuit_prevents_cascade_failures(self, chaos_redis):
        """
        Open circuit should prevent requests during recovery.
        """
        config = RealCircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=1.0,
            half_open_requests=1,
        )

        HashChainDegradationManager.reset_instance()
        manager = HashChainDegradationManager(redis_client=None)

        circuit = HashChainCircuitBreaker(
            config=config,
            degradation_manager=manager,
        )

        # Open the circuit
        circuit.record_failure()
        circuit.record_failure()

        assert circuit.state == CircuitState.OPEN

        # Circuit should reject requests
        assert not circuit.can_execute()

        # Cleanup
        HashChainDegradationManager.reset_instance()

    def test_circuit_half_open_recovery(self, chaos_redis):
        """
        Circuit should transition to HALF_OPEN after timeout.
        """
        config = RealCircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.1,  # Short timeout for testing
            half_open_requests=1,
        )

        HashChainDegradationManager.reset_instance()
        manager = HashChainDegradationManager(redis_client=None)

        circuit = HashChainCircuitBreaker(
            config=config,
            degradation_manager=manager,
        )

        # Open circuit
        circuit.record_failure()
        circuit.record_failure()
        assert circuit.state == CircuitState.OPEN

        # Wait for recovery timeout
        time.sleep(0.15)

        # Should transition to HALF_OPEN
        assert circuit.state == CircuitState.HALF_OPEN

        # One request allowed
        assert circuit.can_execute()

        # Cleanup
        HashChainDegradationManager.reset_instance()

    def test_circuit_closes_on_success(self, chaos_redis):
        """
        Successful request in HALF_OPEN should close circuit.
        """
        config = RealCircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.1,
            half_open_requests=1,
            success_threshold=1,
        )

        HashChainDegradationManager.reset_instance()
        manager = HashChainDegradationManager(redis_client=None)

        circuit = HashChainCircuitBreaker(
            config=config,
            degradation_manager=manager,
        )

        # Open and wait for HALF_OPEN
        circuit.record_failure()
        circuit.record_failure()
        time.sleep(0.15)

        assert circuit.state == CircuitState.HALF_OPEN
        circuit.can_execute()  # Allow one request

        # Record success
        circuit.record_success()

        # Should close
        assert circuit.state == CircuitState.CLOSED

        # Cleanup
        HashChainDegradationManager.reset_instance()


# =============================================================================
# Test: Degradation Level Transitions
# =============================================================================


class TestDegradationLevelTransitions:
    """Tests for degradation level transitions during chaos."""

    def test_normal_to_degraded_on_failure(self, chaos_redis):
        """
        Should transition from NORMAL to DEGRADED on Redis failure.
        """
        # Create fresh manager for this test
        HashChainDegradationManager.reset_instance()
        manager = HashChainDegradationManager(redis_client=chaos_redis)

        assert manager.level == DegradationLevel.NORMAL

        # Trigger failure
        manager.on_redis_failure(Exception("Redis failed"))

        assert manager.level == DegradationLevel.DEGRADED

        # Cleanup
        HashChainDegradationManager.reset_instance()

    def test_degraded_to_emergency_on_repeated_failures(self, chaos_redis):
        """
        Repeated failures should escalate to EMERGENCY.
        """
        HashChainDegradationManager.reset_instance()
        manager = HashChainDegradationManager(redis_client=chaos_redis)

        # Multiple failures (need >10 to trigger emergency)
        for _ in range(15):
            manager.on_redis_failure(Exception("Redis failed"))

        assert manager.level == DegradationLevel.EMERGENCY

        # Cleanup
        HashChainDegradationManager.reset_instance()

    def test_recovery_resets_to_normal(self, chaos_redis):
        """
        Redis recovery should reset level to NORMAL.
        """
        HashChainDegradationManager.reset_instance()
        manager = HashChainDegradationManager(redis_client=chaos_redis)

        # Set to degraded first
        manager.on_redis_failure(Exception("Redis failed"))
        assert manager.level == DegradationLevel.DEGRADED

        # Trigger recovery
        manager.on_redis_recovery()

        assert manager.level == DegradationLevel.NORMAL

        # Cleanup
        HashChainDegradationManager.reset_instance()


# =============================================================================
# Test: Intermittent Failures
# =============================================================================


class TestIntermittentFailures:
    """Tests for intermittent Redis failures."""

    def test_handles_intermittent_failures_gracefully(
        self, chaos_redis, temp_fallback_dir
    ):
        """
        System should handle intermittent failures without data loss.
        """
        config = FallbackConfig(
            local_file_path=temp_fallback_dir / "fallback.jsonl",
        )

        fallback = HashChainFallbackChain(
            redis_primary=chaos_redis,
            config=config,
        )

        # Set intermittent failure (50% chance)
        chaos_redis.set_intermittent_failure(0.5)

        entries = []
        with patch(
            "baldur.adapters.cache.redis_adapter.RedisDistributedLock",
            MockDistributedLock,
        ):
            for i in range(10):
                entry = {"event": f"test_{i}"}
                result = fallback.add_integrity(entry)
                entries.append(result)

        # All entries should have been written (to some tier)
        assert len(entries) == 10

        # Each entry should have integrity fields
        for entry in entries:
            assert "integrity" in entry
            assert "sequence" in entry["integrity"]
            assert "current_hash" in entry["integrity"]

        stats = fallback.get_stats()
        # Some writes should have fallen back
        total_writes = (
            stats["primary_writes"] + stats["local_writes"] + stats["memory_writes"]
        )
        assert total_writes == 10


# =============================================================================
# Test: Memory Buffer Overflow
# =============================================================================


class TestMemoryBufferOverflow:
    """Tests for memory buffer behavior under prolonged outage."""

    def test_memory_buffer_drops_oldest_entries(self, chaos_redis):
        """
        When memory buffer is full, oldest entries are dropped.

        Note: To reach memory tier, both Redis and local must fail.
        Since local tier always succeeds (even with None path - it just
        doesn't write to disk), we need to mock the local method to fail.
        """
        from unittest.mock import patch

        config = FallbackConfig(
            memory_max_entries=5,  # Small buffer for testing
        )

        fallback = HashChainFallbackChain(config=config)  # No Redis

        # Mock local tier to fail, forcing memory tier
        with patch.object(
            fallback,
            "_add_integrity_local",
            side_effect=RuntimeError("Local storage unavailable"),
        ):
            # Write more entries than buffer can hold
            for i in range(10):
                fallback.add_integrity({"event": f"test_{i}"})

        # Buffer should only have last 5 entries
        stats = fallback.get_stats()
        assert stats["memory_buffer_size"] == 5

        degraded = fallback.get_degraded_entries()
        # Should have entries 5-9 (oldest were dropped)
        events = [e["event"] for e in degraded]
        assert "test_9" in events  # Latest should be there


# =============================================================================
# Test: Data Integrity During Chaos
# =============================================================================


class TestDataIntegrityDuringChaos:
    """Tests ensuring data integrity is maintained during failures."""

    def test_hash_chain_valid_during_failure(self, chaos_redis, temp_fallback_dir):
        """
        Hash chain should remain valid even during failures.
        """
        config = FallbackConfig(
            local_file_path=temp_fallback_dir / "fallback.jsonl",
        )

        fallback = HashChainFallbackChain(config=config)

        entries = []
        for i in range(5):
            result = fallback.add_integrity({"event": f"test_{i}"})
            entries.append(result)

        # Verify chain integrity
        for i in range(1, len(entries)):
            current = entries[i]
            previous = entries[i - 1]

            # Previous hash should link correctly
            assert (
                current["integrity"]["previous_hash"]
                == previous["integrity"]["current_hash"]
            )

    def test_sequence_continuity_during_failure(self, chaos_redis, temp_fallback_dir):
        """
        Sequence numbers should be continuous during failure.
        """
        config = FallbackConfig(
            local_file_path=temp_fallback_dir / "fallback.jsonl",
        )

        fallback = HashChainFallbackChain(config=config)

        entries = []
        for i in range(10):
            result = fallback.add_integrity({"event": f"test_{i}"})
            entries.append(result)

        # Verify sequence continuity
        sequences = [e["integrity"]["sequence"] for e in entries]
        for i in range(1, len(sequences)):
            assert sequences[i] == sequences[i - 1] + 1
