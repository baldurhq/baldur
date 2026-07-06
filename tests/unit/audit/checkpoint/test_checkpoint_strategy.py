"""통합 체크포인트 저장 전략 테스트."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


class TestUnifiedCheckpointData:
    """UnifiedCheckpointData 테스트."""

    def test_to_dict_and_from_dict(self):
        """직렬화/역직렬화 테스트."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        data = UnifiedCheckpointData(
            wal_sequence=1234,
            kafka_topic="test.topic",
            kafka_partition=3,
            kafka_offset=56789,
            checksum="abc123",
        )

        dict_data = data.to_dict()
        restored = UnifiedCheckpointData.from_dict(dict_data)

        assert restored.wal_sequence == 1234
        assert restored.kafka_topic == "test.topic"
        assert restored.kafka_partition == 3
        assert restored.kafka_offset == 56789
        assert restored.checksum == "abc123"

    def test_from_legacy_checkpoint_data(self):
        """레거시 CheckpointData 변환 테스트."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        legacy = {
            "last_sequence": 999,
            "timestamp": 1700000000.0,
            "version": 1,
        }

        data = UnifiedCheckpointData.from_legacy_checkpoint_data(legacy)

        assert data.wal_sequence == 999
        assert data.kafka_topic is None  # 레거시에는 없음
        assert "2023" in data.timestamp  # ISO 8601 형식으로 변환

    def test_from_dict_with_legacy_last_sequence(self):
        """from_dict가 last_sequence도 지원하는지 테스트."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        legacy = {
            "last_sequence": 500,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": 1,
        }

        data = UnifiedCheckpointData.from_dict(legacy)
        assert data.wal_sequence == 500

    def test_default_timestamp(self):
        """timestamp 기본값 테스트."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        data = UnifiedCheckpointData(wal_sequence=1)
        assert data.timestamp is not None
        assert "T" in data.timestamp  # ISO 8601 형식


class TestFileCheckpointStorage:
    """FileCheckpointStorage 테스트."""

    @pytest.fixture
    def storage(self, tmp_path):
        """FileCheckpointStorage 인스턴스."""
        from baldur.audit.checkpoint import FileCheckpointStorage

        return FileCheckpointStorage(base_path=tmp_path)

    def test_save_and_load(self, storage):
        """저장 및 로드 테스트."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        data = UnifiedCheckpointData(wal_sequence=1234)
        storage.save("test", data)

        loaded = storage.load("test")
        assert loaded is not None
        assert loaded.wal_sequence == 1234

    def test_load_nonexistent(self, storage):
        """존재하지 않는 체크포인트 로드."""
        loaded = storage.load("nonexistent")
        assert loaded is None

    def test_delete(self, storage):
        """삭제 테스트."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        data = UnifiedCheckpointData(wal_sequence=1234)
        storage.save("test", data)

        assert storage.exists("test")
        assert storage.delete("test")
        assert not storage.exists("test")

    def test_get_wal_sequence(self, storage):
        """WAL 시퀀스 조회 편의 메서드."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        # 없을 때
        assert storage.get_wal_sequence("test") == 0

        # 있을 때
        data = UnifiedCheckpointData(wal_sequence=5678)
        storage.save("test", data)
        assert storage.get_wal_sequence("test") == 5678

    def test_multiple_namespaces(self, storage):
        """여러 네임스페이스 테스트."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        storage.save("ns1", UnifiedCheckpointData(wal_sequence=100))
        storage.save("ns2", UnifiedCheckpointData(wal_sequence=200))

        assert storage.get_wal_sequence("ns1") == 100
        assert storage.get_wal_sequence("ns2") == 200

    def test_overwrite(self, storage):
        """덮어쓰기 테스트."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        storage.save("test", UnifiedCheckpointData(wal_sequence=1))
        storage.save("test", UnifiedCheckpointData(wal_sequence=2))

        assert storage.get_wal_sequence("test") == 2

    def test_commit_is_noop(self, storage):
        """commit()이 no-op인지 테스트."""
        # 예외 발생하지 않아야 함
        storage.commit("test")

    def test_load_legacy_format(self, storage, tmp_path):
        """레거시 형식 로드 테스트."""

        # 레거시 형식으로 직접 파일 생성
        file_path = tmp_path / "checkpoint.legacy.json"
        legacy_data = {
            "last_sequence": 999,
            "timestamp": 1700000000.0,
            "version": 1,
        }
        with open(file_path, "w") as f:
            json.dump(legacy_data, f)

        loaded = storage.load("legacy")
        assert loaded is not None
        assert loaded.wal_sequence == 999


class TestRedisCheckpointStorage:
    """RedisCheckpointStorage 테스트 (Mock)."""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis 클라이언트."""
        return MagicMock()

    @pytest.fixture
    def storage(self, mock_redis):
        """RedisCheckpointStorage 인스턴스."""
        from baldur.audit.checkpoint import RedisCheckpointStorage

        return RedisCheckpointStorage(
            redis_client=mock_redis,
            use_distributed_lock=False,  # 테스트에서 락 비활성화
            enable_notification=False,
        )

    def test_save_calls_redis_set(self, storage, mock_redis):
        """save()가 Redis set을 호출하는지 확인."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        data = UnifiedCheckpointData(wal_sequence=1234)
        storage.save("test", data)

        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert "baldur:checkpoint:test" in call_args[0]

    def test_save_with_ttl(self, mock_redis):
        """TTL이 설정된 경우 setex 호출."""
        from baldur.audit.checkpoint import (
            RedisCheckpointStorage,
            UnifiedCheckpointData,
        )

        storage = RedisCheckpointStorage(
            redis_client=mock_redis,
            ttl_seconds=3600,
            use_distributed_lock=False,
            enable_notification=False,
        )
        data = UnifiedCheckpointData(wal_sequence=1234)
        storage.save("test", data)

        mock_redis.setex.assert_called_once()

    def test_load_returns_data(self, storage, mock_redis):
        """load()가 데이터를 올바르게 반환하는지 확인."""

        mock_redis.get.return_value = json.dumps(
            {
                "wal_sequence": 5678,
                "timestamp": "2024-01-01T00:00:00Z",
                "version": 1,
            }
        )

        data = storage.load("test")
        assert data is not None
        assert data.wal_sequence == 5678

    def test_load_returns_none_when_missing(self, storage, mock_redis):
        """키가 없을 때 None 반환."""
        mock_redis.get.return_value = None
        assert storage.load("missing") is None

    def test_delete(self, storage, mock_redis):
        """delete() 테스트."""
        mock_redis.delete.return_value = 1
        assert storage.delete("test")
        mock_redis.delete.assert_called_once()

    def test_exists(self, storage, mock_redis):
        """exists() 테스트."""
        mock_redis.exists.return_value = 1
        assert storage.exists("test")

        mock_redis.exists.return_value = 0
        assert not storage.exists("test2")


class TestKafkaRedisCheckpointStorage:
    """KafkaRedisCheckpointStorage 테스트 (Mock)."""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis 클라이언트."""
        return MagicMock()

    @pytest.fixture
    def storage(self, mock_redis, tmp_path):
        """KafkaRedisCheckpointStorage 인스턴스."""
        from baldur.audit.checkpoint import KafkaRedisCheckpointStorage

        return KafkaRedisCheckpointStorage(
            redis_client=mock_redis,
            file_backup_path=tmp_path / "backup",
            enable_file_backup=True,
            enable_notification=False,
        )

    def test_save_to_both_redis_and_file(self, storage, mock_redis, tmp_path):
        """Redis와 File 둘 다 저장되는지 확인."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        data = UnifiedCheckpointData(wal_sequence=1234)
        storage.save("test", data)

        # Redis 호출 확인
        mock_redis.set.assert_called_once()

        # File 백업 존재 확인
        backup_path = tmp_path / "backup" / "checkpoint.test.json"
        assert backup_path.exists()

    def test_fallback_to_file_when_redis_fails(self, storage, mock_redis, tmp_path):
        """Redis 실패 시 File로 폴백."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        mock_redis.set.side_effect = Exception("Redis connection failed")

        data = UnifiedCheckpointData(wal_sequence=1234)
        # 예외가 발생하지 않아야 함 (File 백업 성공)
        storage.save("test", data)

        # File 백업 존재 확인
        backup_path = tmp_path / "backup" / "checkpoint.test.json"
        assert backup_path.exists()

    def test_load_from_redis_first(self, storage, mock_redis):
        """load()가 Redis를 먼저 시도하는지 확인."""
        mock_redis.get.return_value = json.dumps(
            {
                "wal_sequence": 5678,
                "kafka_topic": "test.topic",
                "kafka_partition": 0,
                "kafka_offset": 100,
                "timestamp": "2024-01-01T00:00:00Z",
                "version": 1,
            }
        )

        data = storage.load("test")
        assert data is not None
        assert data.wal_sequence == 5678
        assert data.kafka_topic == "test.topic"

    def test_load_fallback_to_file(self, storage, mock_redis, tmp_path):
        """Redis 실패 시 File에서 로드."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        mock_redis.get.return_value = None

        # File에 직접 데이터 저장
        backup_path = tmp_path / "backup" / "checkpoint.test.json"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_path, "w") as f:
            json.dump(UnifiedCheckpointData(wal_sequence=9999).to_dict(), f)

        data = storage.load("test")
        assert data is not None
        assert data.wal_sequence == 9999

    def test_save_with_kafka_offset(self, storage, mock_redis):
        """save_with_kafka_offset() 테스트."""
        storage.save_with_kafka_offset(
            namespace="test",
            wal_sequence=1234,
            kafka_topic="my.topic",
            kafka_partition=2,
            kafka_offset=5678,
            checksum="abc",
        )

        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        saved_data = json.loads(call_args[0][1])
        assert saved_data["wal_sequence"] == 1234
        assert saved_data["kafka_topic"] == "my.topic"
        assert saved_data["kafka_partition"] == 2
        assert saved_data["kafka_offset"] == 5678

    def test_delete_from_both(self, storage, mock_redis, tmp_path):
        """Redis와 File 둘 다에서 삭제."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        # 먼저 저장
        data = UnifiedCheckpointData(wal_sequence=1234)
        storage.save("test", data)

        mock_redis.delete.return_value = 1

        # 삭제
        assert storage.delete("test")
        mock_redis.delete.assert_called_once()


class TestCompositeCheckpointStorage:
    """CompositeCheckpointStorage 테스트."""

    @pytest.fixture
    def primary(self, tmp_path):
        """Primary 저장소 (File)."""
        from baldur.audit.checkpoint import FileCheckpointStorage

        return FileCheckpointStorage(base_path=tmp_path / "primary")

    @pytest.fixture
    def secondary(self, tmp_path):
        """Secondary 저장소 (File)."""
        from baldur.audit.checkpoint import FileCheckpointStorage

        return FileCheckpointStorage(base_path=tmp_path / "secondary")

    def test_save_to_primary(self, primary, secondary):
        """정상적인 경우 Primary에 저장."""
        from baldur.audit.checkpoint import (
            CompositeCheckpointStorage,
            UnifiedCheckpointData,
        )

        composite = CompositeCheckpointStorage(primary=primary, secondary=secondary)

        data = UnifiedCheckpointData(wal_sequence=1234)
        composite.save("test", data)

        assert primary.exists("test")
        stats = composite.get_stats()
        assert stats["primary_writes"] == 1
        assert stats["current_tier"] == "primary"

    def test_fallback_to_secondary(self, secondary):
        """Primary 실패 시 Secondary로 폴백."""
        from baldur.audit.checkpoint import (
            CompositeCheckpointStorage,
            UnifiedCheckpointData,
        )

        # 실패하는 Mock Primary
        mock_primary = MagicMock()
        mock_primary.save.side_effect = Exception("Primary failed")

        composite = CompositeCheckpointStorage(
            primary=mock_primary,
            secondary=secondary,
        )

        data = UnifiedCheckpointData(wal_sequence=1234)
        composite.save("test", data)

        assert secondary.exists("test")
        stats = composite.get_stats()
        assert stats["secondary_writes"] == 1
        assert stats["fallback_events"] == 1
        assert stats["current_tier"] == "secondary"

    def test_fallback_to_memory(self):
        """모든 저장소 실패 시 Memory로 폴백."""
        from baldur.audit.checkpoint import (
            CompositeCheckpointStorage,
            UnifiedCheckpointData,
        )

        mock_primary = MagicMock()
        mock_primary.save.side_effect = Exception("Primary failed")

        mock_secondary = MagicMock()
        mock_secondary.save.side_effect = Exception("Secondary failed")

        composite = CompositeCheckpointStorage(
            primary=mock_primary,
            secondary=mock_secondary,
            enable_memory_fallback=True,
        )

        data = UnifiedCheckpointData(wal_sequence=1234)
        composite.save("test", data)

        stats = composite.get_stats()
        assert stats["memory_writes"] == 1
        assert stats["current_tier"] == "memory"

        # Memory에서 로드
        mock_primary.load.return_value = None
        mock_secondary.load.return_value = None
        loaded = composite.load("test")
        assert loaded is not None
        assert loaded.wal_sequence == 1234

    def test_load_tiered(self, primary, secondary):
        """Tiered Load 테스트."""
        from baldur.audit.checkpoint import (
            CompositeCheckpointStorage,
            UnifiedCheckpointData,
        )

        composite = CompositeCheckpointStorage(primary=primary, secondary=secondary)

        # Secondary에만 데이터 저장
        secondary.save("test", UnifiedCheckpointData(wal_sequence=9999))

        # Primary에는 없으므로 Secondary에서 로드
        data = composite.load("test")
        assert data is not None
        assert data.wal_sequence == 9999

    def test_exists_checks_all_tiers(self, primary, secondary):
        """exists()가 모든 Tier를 체크하는지 확인."""
        from baldur.audit.checkpoint import (
            CompositeCheckpointStorage,
            UnifiedCheckpointData,
        )

        composite = CompositeCheckpointStorage(primary=primary, secondary=secondary)

        assert not composite.exists("test")

        secondary.save("test", UnifiedCheckpointData(wal_sequence=1))
        assert composite.exists("test")


class TestGetCheckpointStrategy:
    """get_checkpoint_strategy 팩토리 테스트."""

    def test_file_strategy_default(self):
        """기본값은 FileCheckpointStorage."""
        from baldur.audit.checkpoint import (
            FileCheckpointStorage,
            get_checkpoint_strategy,
        )

        strategy = get_checkpoint_strategy(storage_type="file")
        assert isinstance(strategy, FileCheckpointStorage)

    def test_redis_requires_client(self):
        """Redis 전략은 클라이언트 필수."""
        from baldur.audit.checkpoint import get_checkpoint_strategy

        with pytest.raises(ValueError, match="redis_client is required"):
            get_checkpoint_strategy(storage_type="redis")

    def test_kafka_redis_requires_client(self):
        """Kafka+Redis 전략은 클라이언트 필수."""
        from baldur.audit.checkpoint import get_checkpoint_strategy

        with pytest.raises(ValueError, match="redis_client is required"):
            get_checkpoint_strategy(storage_type="kafka_redis")

    def test_unknown_storage_type(self):
        """알 수 없는 저장소 유형."""
        from baldur.audit.checkpoint import get_checkpoint_strategy

        with pytest.raises(ValueError, match="Unknown storage_type"):
            get_checkpoint_strategy(storage_type="unknown")

    def test_redis_strategy_with_client(self):
        """Redis 전략 생성."""
        from baldur.audit.checkpoint import (
            RedisCheckpointStorage,
            get_checkpoint_strategy,
        )

        mock_redis = MagicMock()
        strategy = get_checkpoint_strategy(
            storage_type="redis",
            redis_client=mock_redis,
        )
        assert isinstance(strategy, RedisCheckpointStorage)

    def test_composite_strategy(self):
        """Composite 전략 생성."""
        from baldur.audit.checkpoint import (
            CompositeCheckpointStorage,
            get_checkpoint_strategy,
        )

        mock_redis = MagicMock()
        strategy = get_checkpoint_strategy(
            storage_type="composite",
            redis_client=mock_redis,
            primary_type="redis",
            secondary_type="file",
        )
        assert isinstance(strategy, CompositeCheckpointStorage)


class TestCheckpointStrategyRegistry:
    """CheckpointStrategyRegistry 테스트."""

    def teardown_method(self):
        """각 테스트 후 레지스트리 초기화."""
        from baldur.audit.checkpoint import CheckpointStrategyRegistry

        CheckpointStrategyRegistry.clear()

    def test_register_and_get(self):
        """등록 및 조회 테스트."""
        from baldur.audit.checkpoint import (
            CheckpointStrategyRegistry,
            FileCheckpointStorage,
        )

        CheckpointStrategyRegistry.register("custom_file", FileCheckpointStorage)

        strategy = CheckpointStrategyRegistry.get("custom_file")
        assert isinstance(strategy, FileCheckpointStorage)

    def test_auto_register(self):
        """자동 등록 테스트."""
        from baldur.audit.checkpoint import CheckpointStrategyRegistry

        strategies = CheckpointStrategyRegistry.list_strategies()
        assert "file" in strategies
        assert "redis" in strategies
        assert "kafka_redis" in strategies
        assert "composite" in strategies

    def test_set_default(self):
        """기본값 설정 테스트."""
        from baldur.audit.checkpoint import (
            CheckpointStrategyRegistry,
            FileCheckpointStorage,
        )

        CheckpointStrategyRegistry.set_default("file")
        strategy = CheckpointStrategyRegistry.get()  # name=None
        assert isinstance(strategy, FileCheckpointStorage)


class TestCheckpointErrors:
    """에러 클래스 테스트."""

    def test_checkpoint_error(self):
        """CheckpointError 테스트."""
        from baldur.audit.checkpoint import CheckpointError

        error = CheckpointError("Test error")
        assert str(error) == "Test error"

    def test_checkpoint_corrupted_error(self):
        """CheckpointCorruptedError 테스트."""
        from baldur.audit.checkpoint import CheckpointCorruptedError

        error = CheckpointCorruptedError(
            message="Checksum mismatch",
            expected="abc123",
            computed="xyz789",
        )
        assert "Checksum mismatch" in str(error)
        assert error.expected == "abc123"
        assert error.computed == "xyz789"


class TestSingleton:
    """싱글톤 테스트."""

    def teardown_method(self):
        """각 테스트 후 싱글톤 초기화."""
        from baldur.audit.checkpoint import (
            reset_default_checkpoint_strategy,
        )

        reset_default_checkpoint_strategy()

    def test_get_default_checkpoint_strategy(self, monkeypatch):
        """get_default_checkpoint_strategy 테스트."""
        from baldur.audit.checkpoint import (
            FileCheckpointStorage,
            get_default_checkpoint_strategy,
        )

        monkeypatch.setenv("BALDUR_CHECKPOINT_STORAGE", "file")

        strategy = get_default_checkpoint_strategy()
        assert isinstance(strategy, FileCheckpointStorage)

        # 같은 인스턴스 반환
        strategy2 = get_default_checkpoint_strategy()
        assert strategy is strategy2

    def test_reset_default_checkpoint_strategy(self, monkeypatch):
        """reset_default_checkpoint_strategy 테스트."""
        from baldur.audit.checkpoint import (
            get_default_checkpoint_strategy,
            reset_default_checkpoint_strategy,
        )

        monkeypatch.setenv("BALDUR_CHECKPOINT_STORAGE", "file")

        strategy1 = get_default_checkpoint_strategy()
        reset_default_checkpoint_strategy()
        strategy2 = get_default_checkpoint_strategy()

        # 리셋 후 새 인스턴스
        assert strategy1 is not strategy2


class TestContinuousAuditRecorderBackPressure:
    """ContinuousAuditRecorder Back-pressure 테스트."""

    def _make_test_record(self, recorder):
        """테스트용 레코드 기록 헬퍼."""
        return recorder.record_auto_tuning(
            parameter="test_param",
            old_value=1,
            new_value=2,
            reason="test",
            confidence=0.9,
            metrics_snapshot={"cpu": 50},
            safety_check={"passed": True},
        )

    def _inject_mock_wal(self, recorder, start_seq=1):
        """Mock WAL 주입 헬퍼. wal_seq가 순차 증가하도록 설정."""
        from unittest.mock import MagicMock

        mock_wal = MagicMock()
        # write() 호출 시 순차 증가하는 시퀀스 반환
        mock_wal.write.side_effect = lambda _: next(mock_wal._seq_counter)
        mock_wal._seq_counter = iter(range(start_seq, start_seq + 1000))
        mock_wal.mark_processed.return_value = None

        recorder._wal = mock_wal
        recorder._wal_enabled = True
        return mock_wal

    def test_backpressure_counter_increases_with_mock_wal(self, tmp_path):
        """Mock WAL 주입 시 records_since_checkpoint가 증가하는지 확인."""
        from unittest.mock import MagicMock

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        mock_adapter = MagicMock()
        mock_adapter.log.return_value = None

        strategy = FileCheckpointStorage(base_path=tmp_path)

        recorder = ContinuousAuditRecorder(
            audit_adapter=mock_adapter,
            checkpoint_strategy=strategy,
            checkpoint_save_interval=10,  # 10개마다 저장 (임계값 도달 전)
            checkpoint_save_max_seconds=60.0,
        )

        # Mock WAL 주입
        mock_wal = self._inject_mock_wal(recorder, start_seq=1)

        # 초기값 확인
        assert recorder.get_stats()["records_since_checkpoint"] == 0

        # 2개 기록
        self._make_test_record(recorder)
        assert recorder.get_stats()["records_since_checkpoint"] == 1

        self._make_test_record(recorder)
        assert recorder.get_stats()["records_since_checkpoint"] == 2

        # WAL.write() 호출 확인
        assert mock_wal.write.call_count == 2

    def test_backpressure_saves_checkpoint_at_threshold(self, tmp_path):
        """임계값 도달 시 체크포인트가 저장되고 카운터가 리셋되는지 확인."""
        from unittest.mock import MagicMock

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        mock_adapter = MagicMock()
        mock_adapter.log.return_value = None

        strategy = FileCheckpointStorage(base_path=tmp_path)

        recorder = ContinuousAuditRecorder(
            audit_adapter=mock_adapter,
            checkpoint_strategy=strategy,
            checkpoint_save_interval=3,  # 3개마다 저장
            checkpoint_save_max_seconds=60.0,
        )

        # Mock WAL 주입
        self._inject_mock_wal(recorder, start_seq=100)

        # 3개 기록 (임계값 = 3)
        for _i in range(3):
            self._make_test_record(recorder)

        # 카운터가 리셋되었는지 확인 (저장 후 0)
        assert recorder.get_stats()["records_since_checkpoint"] == 0

        # 체크포인트가 실제로 저장되었는지 확인
        loaded = strategy.load("default")
        assert loaded is not None
        assert loaded.wal_sequence == 102  # 100, 101, 102 중 마지막

    def test_backpressure_saves_checkpoint_at_time_threshold(self, tmp_path):
        """시간 임계값 도달 시 체크포인트가 저장되는지 확인."""
        import time
        from unittest.mock import MagicMock

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        mock_adapter = MagicMock()
        mock_adapter.log.return_value = None

        strategy = FileCheckpointStorage(base_path=tmp_path)

        recorder = ContinuousAuditRecorder(
            audit_adapter=mock_adapter,
            checkpoint_strategy=strategy,
            checkpoint_save_interval=1000,  # 높게 설정 (개수 임계값 도달 안함)
            checkpoint_save_max_seconds=0.1,  # 0.1초 후 저장
        )

        # Mock WAL 주입
        self._inject_mock_wal(recorder, start_seq=200)

        # Setup 소요 시간이 checkpoint_save_max_seconds(0.1s)를 초과할 수 있으므로
        # 타이머 리셋하여 시간 기반 트리거가 setup 지연에 영향받지 않도록 함
        recorder._last_checkpoint_time = time.time()

        # 1개 기록
        self._make_test_record(recorder)
        assert recorder.get_stats()["records_since_checkpoint"] == 1

        # 0.15초 대기
        time.sleep(0.15)

        # 다음 레코드가 시간 임계값 초과로 저장 트리거
        self._make_test_record(recorder)

        # 카운터가 리셋되었는지 확인
        assert recorder.get_stats()["records_since_checkpoint"] == 0

        # 체크포인트가 저장되었는지 확인
        loaded = strategy.load("default")
        assert loaded is not None
        assert loaded.wal_sequence == 201

    def test_force_save_checkpoint(self, tmp_path):
        """force_save_checkpoint 테스트."""
        from unittest.mock import MagicMock

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        mock_adapter = MagicMock()

        strategy = FileCheckpointStorage(base_path=tmp_path)

        recorder = ContinuousAuditRecorder(
            audit_adapter=mock_adapter,
            checkpoint_strategy=strategy,
            checkpoint_save_interval=1000,  # 높게 설정
        )

        # 강제 저장 (시퀀스 지정)
        recorder.force_save_checkpoint(wal_seq=999)

        loaded = strategy.load("default")
        assert loaded is not None
        assert loaded.wal_sequence == 999

    def test_stats_include_backpressure_info(self, tmp_path):
        """get_stats()가 Back-pressure 정보 포함."""
        from unittest.mock import MagicMock

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        mock_adapter = MagicMock()
        mock_adapter.log.return_value = None

        strategy = FileCheckpointStorage(base_path=tmp_path)

        recorder = ContinuousAuditRecorder(
            audit_adapter=mock_adapter,
            checkpoint_strategy=strategy,
            checkpoint_save_interval=100,
        )

        # Mock WAL 주입
        self._inject_mock_wal(recorder, start_seq=1)

        self._make_test_record(recorder)

        stats = recorder.get_stats()
        assert "records_since_checkpoint" in stats
        assert stats["records_since_checkpoint"] == 1

    def test_backpressure_time_threshold_deterministic(self, tmp_path):
        """time.time mock으로 시간 기반 체크포인트 결정론적 검증 (flaky 방지)."""
        from unittest.mock import MagicMock, patch

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        mock_adapter = MagicMock()
        mock_adapter.log.return_value = None

        strategy = FileCheckpointStorage(base_path=tmp_path)

        # Given: deterministic time control
        current_time = [1000.0]

        def controlled_time():
            return current_time[0]

        with patch("time.time", controlled_time):
            recorder = ContinuousAuditRecorder(
                audit_adapter=mock_adapter,
                checkpoint_strategy=strategy,
                checkpoint_save_interval=1000,  # count threshold unreachable
                checkpoint_save_max_seconds=0.1,  # 100ms time threshold
            )

            self._inject_mock_wal(recorder, start_seq=300)

            # When: no time elapsed (0ms < 100ms threshold)
            self._make_test_record(recorder)

            # Then: no checkpoint triggered, counter incremented
            assert recorder.get_stats()["records_since_checkpoint"] == 1

            # When: advance time past threshold (150ms >= 100ms)
            current_time[0] = 1000.15
            self._make_test_record(recorder)

            # Then: checkpoint triggered, counter reset
            assert recorder.get_stats()["records_since_checkpoint"] == 0

            loaded = strategy.load("default")
            assert loaded is not None
            assert loaded.wal_sequence == 301

    def test_backpressure_setup_delay_triggers_on_first_record(self, tmp_path):
        """생성자 이후 setup 지연이 시간 임계값을 초과하면 첫 레코드에서 체크포인트 트리거됨."""
        from unittest.mock import MagicMock, patch

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        mock_adapter = MagicMock()
        mock_adapter.log.return_value = None

        strategy = FileCheckpointStorage(base_path=tmp_path)

        # Given: time advances 500ms between constructor and first record
        current_time = [1000.0]

        def controlled_time():
            return current_time[0]

        with patch("time.time", controlled_time):
            recorder = ContinuousAuditRecorder(
                audit_adapter=mock_adapter,
                checkpoint_strategy=strategy,
                checkpoint_save_interval=1000,
                checkpoint_save_max_seconds=0.1,
            )

            self._inject_mock_wal(recorder, start_seq=400)

            # When: 500ms setup delay (exceeds 100ms threshold)
            current_time[0] = 1000.5
            self._make_test_record(recorder)

            # Then: time threshold exceeded → checkpoint saved, counter reset to 0
            assert recorder.get_stats()["records_since_checkpoint"] == 0

            loaded = strategy.load("default")
            assert loaded is not None
            assert loaded.wal_sequence == 400

    def test_backpressure_setup_delay_mitigated_by_timer_reset(self, tmp_path):
        """타이머 리셋으로 setup 지연에 의한 premature trigger 방지 패턴 검증."""
        from unittest.mock import MagicMock, patch

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        mock_adapter = MagicMock()
        mock_adapter.log.return_value = None

        strategy = FileCheckpointStorage(base_path=tmp_path)

        # Given: time advances 500ms between constructor and first record
        current_time = [1000.0]

        def controlled_time():
            return current_time[0]

        with patch("time.time", controlled_time):
            recorder = ContinuousAuditRecorder(
                audit_adapter=mock_adapter,
                checkpoint_strategy=strategy,
                checkpoint_save_interval=1000,
                checkpoint_save_max_seconds=0.1,
            )

            self._inject_mock_wal(recorder, start_seq=500)

            # When: 500ms setup delay, but timer reset before first record
            current_time[0] = 1000.5
            recorder._last_checkpoint_time = current_time[0]

            self._make_test_record(recorder)

            # Then: timer reset prevents premature trigger
            assert recorder.get_stats()["records_since_checkpoint"] == 1

            # And: no checkpoint saved
            loaded = strategy.load("default")
            assert loaded is None


class TestAuditSyncWorkerCheckpointStrategyMigration:
    """AuditSyncWorker CheckpointStorageStrategy 마이그레이션 테스트."""

    def test_set_checkpoint_strategy(self, tmp_path):
        """set_checkpoint_strategy 테스트."""
        from unittest.mock import MagicMock

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.sync_worker import AuditSyncWorker

        mock_wal = MagicMock()
        mock_adapter = MagicMock()

        worker = AuditSyncWorker(wal=mock_wal, central_adapter=mock_adapter)
        strategy = FileCheckpointStorage(base_path=tmp_path)

        worker.set_checkpoint_strategy(strategy)

        # 내부 필드 확인
        assert worker._checkpoint_strategy is strategy

    def test_save_checkpoint_uses_strategy(self, tmp_path):
        """_save_checkpoint가 주입된 Strategy 사용."""
        from unittest.mock import MagicMock

        from baldur.audit.checkpoint import FileCheckpointStorage
        from baldur.audit.sync_worker import AuditSyncWorker

        mock_wal = MagicMock()
        mock_adapter = MagicMock()

        worker = AuditSyncWorker(wal=mock_wal, central_adapter=mock_adapter)
        strategy = FileCheckpointStorage(base_path=tmp_path)
        worker.set_checkpoint_strategy(strategy)

        # 시퀀스 설정
        worker._last_processed_seq = 12345

        # 체크포인트 저장
        worker._save_checkpoint()

        # 저장되었는지 확인
        loaded = strategy.load("sync_worker")
        assert loaded is not None
        assert loaded.wal_sequence == 12345

    def test_save_checkpoint_logs_warning_when_no_strategy(self, monkeypatch):
        """Strategy 없으면 경고 로그 후 워커 계속 실행."""
        from unittest.mock import MagicMock

        from baldur.audit.sync_worker import AuditSyncWorker

        mock_wal = MagicMock()
        mock_adapter = MagicMock()

        worker = AuditSyncWorker(wal=mock_wal, central_adapter=mock_adapter)
        worker._last_processed_seq = 999

        # Strategy 없이 저장 시도 — 예외 없이 경고 로그만 출력
        worker._save_checkpoint()
