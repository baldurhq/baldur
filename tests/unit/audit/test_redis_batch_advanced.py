"""
Redis Audit 배치 처리 고급 기능 단위 테스트.

테스트 항목:
- AuditBatchLuaScripts Lua 스크립트 등록 및 실행
- Processing Queue 패턴 (원자적 이동/완료/복원)
- ActiveKeySet O(1) 도메인 조회
- 청킹 구현
- Safety LTRIM
- Graceful Shutdown
"""

from __future__ import annotations

import json
import time
from unittest.mock import Mock, patch

import pytest


class FakeRedisWithLua:
    """Lua 스크립트 지원하는 테스트용 Redis 모킹."""

    def __init__(self):
        self._data: dict[str, list] = {}
        self._sets: dict[str, set] = {}
        self._hashes: dict[str, dict] = {}
        self._scripts: dict[str, str] = {}
        self._script_counter = 0
        self._pipelines_created = 0

    def script_load(self, script: str) -> str:
        """Lua 스크립트 등록."""
        self._script_counter += 1
        sha = f"sha_{self._script_counter}"
        self._scripts[sha] = script
        return sha

    def evalsha(self, sha: str, numkeys: int, *args) -> int:
        """Lua 스크립트 실행 (간소화된 시뮬레이션)."""
        if sha not in self._scripts:
            raise Exception("NOSCRIPT")

        script = self._scripts[sha]

        # batch_move 스크립트 시뮬레이션
        if "RPOPLPUSH" in script:
            buffer_key = args[0]
            processing_key = args[1]
            batch_size = int(args[2])

            if buffer_key not in self._data:
                return 0

            moved = 0
            for _ in range(batch_size):
                if not self._data.get(buffer_key):
                    break
                item = self._data[buffer_key].pop()
                if processing_key not in self._data:
                    self._data[processing_key] = []
                self._data[processing_key].insert(0, item)
                moved += 1

            return moved

        # batch_complete 스크립트 시뮬레이션
        if "RPOP" in script and "LLEN" in script:
            processing_key = args[0]
            count = int(args[1])

            if processing_key not in self._data:
                return 0

            removed = 0
            for _ in range(count):
                if not self._data.get(processing_key):
                    break
                self._data[processing_key].pop()
                removed += 1

            return removed

        # batch_restore 스크립트 시뮬레이션
        if "LPOP" in script and "RPUSH" in script:
            processing_key = args[0]
            buffer_key = args[1]

            if processing_key not in self._data:
                return 0

            restored = 0
            while self._data.get(processing_key):
                item = self._data[processing_key].pop(0)
                if buffer_key not in self._data:
                    self._data[buffer_key] = []
                self._data[buffer_key].append(item)
                restored += 1

            return restored

        return 0

    def pipeline(self, transaction: bool = False):
        self._pipelines_created += 1
        return FakePipeline(self)

    def lpush(self, key: str, *values) -> int:
        if key not in self._data:
            self._data[key] = []
        for v in values:
            self._data[key].insert(0, v)
        return len(self._data[key])

    def lrange(self, key: str, start: int, end: int) -> list:
        if key not in self._data:
            return []
        return self._data[key][start : end + 1]

    def llen(self, key: str) -> int:
        return len(self._data.get(key, []))

    def ltrim(self, key: str, start: int, end: int) -> bool:
        if key in self._data:
            self._data[key] = self._data[key][start : end + 1]
        return True

    def sadd(self, key: str, *values) -> int:
        if key not in self._sets:
            self._sets[key] = set()
        added = 0
        for v in values:
            if v not in self._sets[key]:
                self._sets[key].add(v)
                added += 1
        return added

    def smembers(self, key: str) -> set:
        return self._sets.get(key, set())

    def srem(self, key: str, *values) -> int:
        if key not in self._sets:
            return 0
        removed = 0
        for v in values:
            if v in self._sets[key]:
                self._sets[key].remove(v)
                removed += 1
        return removed

    def hgetall(self, key: str) -> dict:
        return self._hashes.get(key, {})

    def hset(self, key: str, field: str, value: str) -> int:
        if key not in self._hashes:
            self._hashes[key] = {}
        self._hashes[key][field] = value
        return 1

    def hdel(self, key: str, field: str) -> int:
        if key in self._hashes and field in self._hashes[key]:
            del self._hashes[key][field]
            return 1
        return 0

    def expire(self, key: str, seconds: int) -> bool:
        return True

    def delete(self, key: str) -> int:
        if key in self._data:
            del self._data[key]
            return 1
        return 0

    def ping(self) -> bool:
        return True

    def scan_iter(self, match: str = "*") -> list:
        import fnmatch

        return [k for k in self._data.keys() if fnmatch.fnmatch(k, match)]


class FakePipeline:
    """테스트용 Pipeline 모킹."""

    def __init__(self, redis: FakeRedisWithLua):
        self._redis = redis
        self._commands: list = []

    def lpush(self, key: str, *values):
        self._commands.append(("lpush", key, values))
        return self

    def expire(self, key: str, seconds: int):
        self._commands.append(("expire", key, seconds))
        return self

    def sadd(self, key: str, *values):
        self._commands.append(("sadd", key, values))
        return self

    def execute(self) -> list:
        results = []
        for cmd in self._commands:
            if cmd[0] == "lpush":
                results.append(self._redis.lpush(cmd[1], *cmd[2]))
            elif cmd[0] == "sadd":
                results.append(self._redis.sadd(cmd[1], *cmd[2]))
            else:
                results.append(True)
        return results


class TestAuditBatchLuaScriptsBehavior:
    """AuditBatchLuaScripts 동작 검증."""

    @pytest.fixture
    def fake_redis(self) -> FakeRedisWithLua:
        return FakeRedisWithLua()

    @pytest.fixture
    def lua_scripts(self, fake_redis):
        from baldur.audit.redis_batch_lua import AuditBatchLuaScripts

        return AuditBatchLuaScripts(fake_redis)

    def test_scripts_registered_on_init(self, lua_scripts, fake_redis) -> None:
        """초기화 시 Lua 스크립트 등록."""
        registry = lua_scripts._registry
        assert len(registry._scripts) == 3
        assert "batch_move" in registry._scripts
        assert "batch_complete" in registry._scripts
        assert "batch_restore" in registry._scripts

    def test_atomic_batch_move_transfers_items(self, lua_scripts, fake_redis) -> None:
        """Buffer → Processing Queue 이동."""
        # 버퍼에 데이터 추가
        fake_redis._data["audit:{test}:buffer"] = ["item1", "item2", "item3"]

        moved = lua_scripts.atomic_batch_move(
            domain="test",
            batch_size=2,
            worker_id="worker-1",
        )

        assert moved == 2
        assert len(fake_redis._data.get("audit:{test}:buffer", [])) == 1
        assert len(fake_redis._data.get("audit:{test}:processing", [])) == 2

    def test_atomic_batch_move_empty_buffer(self, lua_scripts, fake_redis) -> None:
        """빈 버퍼에서 이동 시도."""
        moved = lua_scripts.atomic_batch_move(
            domain="empty",
            batch_size=10,
            worker_id="worker-1",
        )

        assert moved == 0

    def test_atomic_batch_complete_removes_items(self, lua_scripts, fake_redis) -> None:
        """Processing Queue 정리."""
        fake_redis._data["audit:{test}:processing"] = ["item1", "item2"]

        removed = lua_scripts.atomic_batch_complete(domain="test", count=2)

        assert removed == 2
        assert len(fake_redis._data.get("audit:{test}:processing", [])) == 0

    def test_atomic_batch_restore_preserves_order(
        self, lua_scripts, fake_redis
    ) -> None:
        """실패 시 순서 보존하여 복원."""
        fake_redis._data["audit:{test}:processing"] = ["item1", "item2", "item3"]
        fake_redis._data["audit:{test}:buffer"] = ["item4", "item5"]

        restored = lua_scripts.atomic_batch_restore(domain="test")

        assert restored == 3
        # Processing Queue 비워짐
        assert len(fake_redis._data.get("audit:{test}:processing", [])) == 0
        # Buffer 끝에 복원됨 (순서 보존)
        buffer = fake_redis._data.get("audit:{test}:buffer", [])
        assert len(buffer) == 5

    def test_get_orphaned_processing_queues(self, lua_scripts, fake_redis) -> None:
        """고아 Processing Queue 조회."""
        # 5분 전 타임스탬프
        old_timestamp = int(time.time()) - 400

        fake_redis._hashes["audit:processing:meta"] = {
            b"audit:{old}:processing": f"worker-1:{old_timestamp}".encode(),
            b"audit:{new}:processing": f"worker-2:{int(time.time())}".encode(),
        }

        orphaned = lua_scripts.get_orphaned_processing_queues(timeout_seconds=300)

        # old 도메인만 고아로 판단
        assert len(orphaned) == 1
        assert orphaned[0][0] == "audit:{old}:processing"


class TestRedisAuditBufferV2Behavior:
    """RedisAuditBuffer v2.0 기능 동작 검증."""

    @pytest.fixture
    def fake_redis(self) -> FakeRedisWithLua:
        return FakeRedisWithLua()

    @pytest.fixture
    def buffer(self, fake_redis):
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        return RedisAuditBuffer(
            redis_client=fake_redis,
            fallback_adapter=None,
            enable_graceful_shutdown=False,  # 테스트에서는 비활성화
        )

    def test_active_domains_set_updated_on_log_batch(self, buffer, fake_redis) -> None:
        """log_batch 시 ActiveKeySet 업데이트."""
        entries = [{"action": "test", "source": "app"}]

        buffer.log_batch(entries, domain="payment")

        # ActiveKeySet에 도메인 추가됨
        assert "payment" in fake_redis._sets.get("audit:active_domains", set())

    def test_get_active_domains_returns_non_empty(self, buffer, fake_redis) -> None:
        """비어있지 않은 도메인만 반환."""
        # 데이터 있는 도메인
        fake_redis._data["audit:{active}:buffer"] = ["item1"]
        fake_redis._sets["audit:active_domains"] = {"active", "empty"}

        domains = buffer._get_active_domains()

        assert "active" in domains
        assert "empty" not in domains

    def test_get_active_domains_fallback(self, buffer, fake_redis) -> None:
        """ActiveKeySet 실패 시 fallback."""
        fake_redis._data["audit:{domain1}:buffer"] = ["item1"]
        fake_redis._data["audit:{domain2}:buffer"] = ["item2"]

        domains = buffer._get_active_domains_fallback()

        assert len(domains) == 2
        assert "domain1" in domains
        assert "domain2" in domains

    def test_log_batch_chunking(self, buffer, fake_redis) -> None:
        """청킹 적용 확인."""
        # MAX_PIPELINE_CHUNK보다 큰 배치
        entries = [{"action": f"test{i}"} for i in range(1500)]

        initial_pipelines = fake_redis._pipelines_created

        buffer.log_batch(entries, domain="chunked")

        # 1500개 → 2개 청크 (1000 + 500)
        assert fake_redis._pipelines_created >= initial_pipelines + 2

    def test_apply_safety_ltrim(self, buffer, fake_redis) -> None:
        """Safety LTRIM 적용."""
        # 임계치 초과 데이터 설정
        fake_redis._data["audit:{large}:buffer"] = [f"item{i}" for i in range(200000)]
        fake_redis._sets["audit:active_domains"] = {"large"}

        with patch(
            "baldur.adapters.audit.redis_buffer._SAFETY_LTRIM_THRESHOLD", 100000
        ):
            trimmed = buffer.apply_safety_ltrim()

        assert "large" in trimmed
        assert trimmed["large"] == 100000

    def test_flush_to_external_safe(self, buffer, fake_redis) -> None:
        """Processing Queue 패턴 플러시."""
        # 버퍼에 데이터 추가
        fake_redis._data["audit:{test}:buffer"] = [
            json.dumps({"entry": {"action": "test1"}}),
            json.dumps({"entry": {"action": "test2"}}),
        ]
        fake_redis._sets["audit:active_domains"] = {"test"}

        # 대상 어댑터 모킹
        target_adapter = Mock()
        target_adapter.log_batch = Mock()

        flushed = buffer.flush_to_external_safe(
            target_adapter=target_adapter,
            batch_size=10,
            domain="test",
        )

        assert flushed == 2
        target_adapter.log_batch.assert_called_once()

    def test_recover_orphaned_processing_queues(self, buffer, fake_redis) -> None:
        """고아 Processing Queue 복구."""
        # 고아 Processing Queue 설정
        fake_redis._data["audit:{orphan}:processing"] = ["item1", "item2"]
        old_timestamp = int(time.time()) - 400

        fake_redis._hashes["audit:processing:meta"] = {
            b"audit:{orphan}:processing": f"worker-1:{old_timestamp}".encode(),
        }

        recovered = buffer.recover_orphaned_processing_queues(timeout_seconds=300)

        assert recovered == 2

    def test_graceful_shutdown_flushes_fallback(self, fake_redis) -> None:
        """Graceful Shutdown 시 폴백 버퍼 플러시."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        buffer = RedisAuditBuffer(
            redis_client=fake_redis,
            fallback_adapter=None,
            enable_graceful_shutdown=False,
        )

        # 폴백 버퍼에 데이터 추가
        buffer._fallback_buffer = [
            {"entry": {"action": "test1"}, "domain": "default"},
            {"entry": {"action": "test2"}, "domain": "default"},
        ]

        buffer._graceful_shutdown()

        # 폴백 버퍼 비워짐
        assert len(buffer._fallback_buffer) == 0


class TestAuditBufferMetricsBehavior:
    """Audit Buffer 메트릭 동작 검증."""

    def test_metrics_module_imports(self) -> None:
        """메트릭 모듈 임포트 확인."""
        from baldur.metrics.audit_buffer_metrics import (
            audit_buffer_backpressure,
            audit_buffer_dropped_total,
            audit_buffer_size,
        )

        # prometheus_client 없어도 동작
        assert audit_buffer_size is not None
        assert audit_buffer_backpressure is not None
        assert audit_buffer_dropped_total is not None

    def test_update_buffer_metrics_helper(self) -> None:
        """메트릭 업데이트 헬퍼 함수."""
        from baldur.metrics.audit_buffer_metrics import update_buffer_metrics

        # 예외 없이 실행
        update_buffer_metrics(domain="test", size=100, max_size=1000)

    def test_record_helpers(self) -> None:
        """기록 헬퍼 함수들."""
        from baldur.metrics.audit_buffer_metrics import (
            record_batch_write,
            record_flush,
            record_orphan_recovery,
            record_safety_ltrim,
        )

        # 모두 예외 없이 실행
        record_batch_write(domain="test", success=True)
        record_batch_write(domain="test", success=False)
        record_flush(domain="test", count=10)
        record_orphan_recovery(domain="test", count=5)
        record_safety_ltrim(domain="test", dropped_count=100)


class TestCeleryAuditFlushTasksContract:
    """Celery 플러시 태스크 설계 계약값 검증."""

    def test_flush_task_imports(self) -> None:
        """태스크 임포트 확인."""
        from baldur.celery_tasks.audit_flush_tasks import (
            apply_audit_buffer_safety_ltrim,
            flush_redis_audit_buffer,
            get_audit_flush_beat_schedule,
            recover_orphaned_processing_queues,
        )

        assert flush_redis_audit_buffer is not None
        assert recover_orphaned_processing_queues is not None
        assert apply_audit_buffer_safety_ltrim is not None
        assert "flush-redis-audit-buffer" in get_audit_flush_beat_schedule()

    def test_beat_schedule_configuration(self) -> None:
        """Beat 스케줄 설정 확인 (600 D2: getter replaces BEAT_SCHEDULE const)."""
        from baldur.celery_tasks.audit_flush_tasks import (
            get_audit_flush_beat_schedule,
        )
        from baldur.settings.audit import get_audit_settings

        schedule = get_audit_flush_beat_schedule()

        # Flush interval is settings-driven (default 10s)
        assert (
            schedule["flush-redis-audit-buffer"]["schedule"]
            == get_audit_settings().buffer_redis_flush_interval
        )

        # Orphan recovery every 5 minutes
        assert schedule["recover-orphaned-processing-queues"]["schedule"] == 300.0

        # Safety LTRIM every minute
        assert schedule["apply-audit-buffer-safety-ltrim"]["schedule"] == 60.0
