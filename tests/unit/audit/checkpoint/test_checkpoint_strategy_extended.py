"""
CheckpointStorageStrategy 확장 단위 테스트.

305 체크포인트 통합 리팩토링에서 추가된 기능 검증:

Unit Tests:
    A. FileCheckpointStorage 동시성 (20+ threads concurrent save/load)
    B. Prometheus counter increment on save/load failure
    C. Legacy migration (checkpoint.json -> checkpoint.default.json)
    D. Legacy format write-back
    E. get_age_seconds() boundary cases
    F. K8s detection + warning log
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# A. FileCheckpointStorage 동시성 (Thread Safety)
# =============================================================================


class TestFileCheckpointStorageThreadSafetyBehavior:
    """FileCheckpointStorage 멀티스레드 동시 접근 안전성 검증."""

    @pytest.fixture
    def storage(self, tmp_path):
        """FileCheckpointStorage 인스턴스."""
        from baldur.audit.checkpoint import FileCheckpointStorage

        return FileCheckpointStorage(base_path=tmp_path, sync_on_write=False)

    def test_concurrent_save_no_corruption_with_20_threads(self, storage):
        """20개 스레드가 동시에 save해도 데이터 손상 없음."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        errors = []

        def worker(thread_id):
            try:
                data = UnifiedCheckpointData(wal_sequence=thread_id)
                storage.save("concurrent", data)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        # 최종 로드 시 유효한 데이터 반환
        loaded = storage.load("concurrent")
        assert loaded is not None
        assert 0 <= loaded.wal_sequence <= 19

    def test_concurrent_load_returns_consistent_data_with_20_threads(self, storage):
        """20개 스레드가 동시에 load해도 일관된 데이터 반환."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        storage.save("read_test", UnifiedCheckpointData(wal_sequence=9999))

        results = []
        errors = []

        def reader():
            try:
                data = storage.load("read_test")
                results.append(data)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(r is not None and r.wal_sequence == 9999 for r in results)

    def test_concurrent_save_and_load_mixed_with_25_threads(self, storage):
        """25개 스레드 (15 writer + 10 reader) 혼합 동시 접근 시 안정성 유지."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        # 초기 데이터 저장
        storage.save("mixed", UnifiedCheckpointData(wal_sequence=0))

        errors = []

        def writer(seq):
            try:
                storage.save("mixed", UnifiedCheckpointData(wal_sequence=seq))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                data = storage.load("mixed")
                # 로드 결과는 None이 아니어야 함 (초기 데이터 존재)
                assert data is not None
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(15):
            threads.append(threading.Thread(target=writer, args=(i + 1,)))
        for _ in range(10):
            threads.append(threading.Thread(target=reader))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# =============================================================================
# B. Prometheus Counter Increment on Save/Load Failure
# =============================================================================


class TestFileCheckpointStoragePrometheusCounterBehavior:
    """FileCheckpointStorage Prometheus 실패 카운터 검증."""

    def test_save_failure_increments_prometheus_counter(self, tmp_path):
        """save 실패 시 baldur_checkpoint_save_failures_total 카운터 증가."""
        import baldur.audit.checkpoint.strategy as mod
        from baldur.audit.checkpoint import FileCheckpointStorage

        # Given
        mock_counter = MagicMock()
        mock_label = MagicMock()
        mock_counter.labels.return_value = mock_label

        original = mod._CHECKPOINT_SAVE_FAILURES
        mod._CHECKPOINT_SAVE_FAILURES = mock_counter
        try:
            storage = FileCheckpointStorage(base_path=tmp_path, sync_on_write=False)

            # When — save에서 예외 유발 (읽기 전용 파일 생성 대신, json.dump 에러 유발)
            bad_data = MagicMock()
            bad_data.to_dict.side_effect = RuntimeError("serialize error")

            with pytest.raises(Exception):
                storage.save("test", bad_data)

            # Then
            mock_counter.labels.assert_called_once_with(storage_type="file")
            mock_label.inc.assert_called_once()
        finally:
            mod._CHECKPOINT_SAVE_FAILURES = original

    def test_load_failure_increments_prometheus_counter(self, tmp_path):
        """load 실패 시 baldur_checkpoint_load_failures_total 카운터 증가."""
        import baldur.audit.checkpoint.strategy as mod
        from baldur.audit.checkpoint import FileCheckpointStorage

        # Given
        mock_counter = MagicMock()
        mock_label = MagicMock()
        mock_counter.labels.return_value = mock_label

        original = mod._CHECKPOINT_LOAD_FAILURES
        mod._CHECKPOINT_LOAD_FAILURES = mock_counter
        try:
            storage = FileCheckpointStorage(base_path=tmp_path)

            # When — 잘못된 JSON 파일 생성
            bad_file = tmp_path / "checkpoint.broken.json"
            bad_file.write_text("{invalid json!!!", encoding="utf-8")

            result = storage.load("broken")

            # Then
            assert result is None
            mock_counter.labels.assert_called_once_with(storage_type="file")
            mock_label.inc.assert_called_once()
        finally:
            mod._CHECKPOINT_LOAD_FAILURES = original


# =============================================================================
# C. Legacy Migration (checkpoint.json -> checkpoint.default.json)
# =============================================================================


class TestFileCheckpointStorageLegacyMigrationBehavior:
    """FileCheckpointStorage 레거시 파일 마이그레이션 검증."""

    def test_legacy_checkpoint_json_migrated_to_default_namespace(self, tmp_path):
        """load('default') 시 checkpoint.json -> checkpoint.default.json 마이그레이션."""
        from baldur.audit.checkpoint import FileCheckpointStorage

        # Given — 레거시 checkpoint.json 파일 생성
        legacy_path = tmp_path / "checkpoint.json"
        legacy_data = {
            "wal_sequence": 777,
            "timestamp": "2024-06-01T00:00:00+00:00",
            "version": 1,
        }
        with open(legacy_path, "w") as f:
            json.dump(legacy_data, f)

        storage = FileCheckpointStorage(base_path=tmp_path, sync_on_write=False)

        # When
        loaded = storage.load("default")

        # Then
        assert loaded is not None
        assert loaded.wal_sequence == 777

        # 레거시 파일이 새 이름으로 이동됨
        new_path = tmp_path / "checkpoint.default.json"
        assert new_path.exists()
        assert not legacy_path.exists()

    def test_legacy_migration_skipped_for_non_default_namespace(self, tmp_path):
        """default 외 namespace에서는 마이그레이션이 수행되지 않음."""
        from baldur.audit.checkpoint import FileCheckpointStorage

        # Given
        legacy_path = tmp_path / "checkpoint.json"
        legacy_path.write_text('{"wal_sequence": 100}', encoding="utf-8")

        storage = FileCheckpointStorage(base_path=tmp_path, sync_on_write=False)

        # When
        loaded = storage.load("other")

        # Then — 레거시 파일 그대로 존재
        assert loaded is None
        assert legacy_path.exists()

    def test_legacy_migration_skipped_when_target_already_exists(self, tmp_path):
        """target(checkpoint.default.json)이 이미 존재하면 마이그레이션 스킵."""
        from baldur.audit.checkpoint import (
            FileCheckpointStorage,
            UnifiedCheckpointData,
        )

        # Given — 두 파일 모두 존재
        legacy_path = tmp_path / "checkpoint.json"
        legacy_path.write_text('{"wal_sequence": 111}', encoding="utf-8")

        storage = FileCheckpointStorage(base_path=tmp_path, sync_on_write=False)
        storage.save("default", UnifiedCheckpointData(wal_sequence=222))

        # When
        loaded = storage.load("default")

        # Then — 새 파일의 값이 반환됨
        assert loaded is not None
        assert loaded.wal_sequence == 222
        # 레거시 파일도 그대로 존재 (마이그레이션 스킵)
        assert legacy_path.exists()


# =============================================================================
# D. Legacy Format Write-Back
# =============================================================================


class TestFileCheckpointStorageLegacyWriteBackBehavior:
    """레거시 형식(last_sequence) 로드 시 통합 형식으로 write-back 검증."""

    def test_legacy_format_converted_and_written_back_as_unified(self, tmp_path):
        """last_sequence 형식의 파일 로드 시 wal_sequence 형식으로 write-back."""
        from baldur.audit.checkpoint import FileCheckpointStorage

        # Given — 레거시 형식 파일
        file_path = tmp_path / "checkpoint.legacy_ns.json"
        legacy_data = {
            "last_sequence": 555,
            "timestamp": 1700000000.0,
            "version": 1,
        }
        with open(file_path, "w") as f:
            json.dump(legacy_data, f)

        storage = FileCheckpointStorage(base_path=tmp_path, sync_on_write=False)

        # When
        loaded = storage.load("legacy_ns")

        # Then — 데이터 올바르게 로드
        assert loaded is not None
        assert loaded.wal_sequence == 555
        assert "2023" in loaded.timestamp  # 1700000000.0 = 2023-11-14

        # write-back 확인: 파일을 다시 읽으면 통합 형식
        with open(file_path) as f:
            written = json.load(f)
        assert "wal_sequence" in written
        assert written["wal_sequence"] == 555

    def test_legacy_format_without_wal_sequence_triggers_writeback(self, tmp_path):
        """wal_sequence 필드가 없고 last_sequence만 있는 경우 write-back 트리거."""
        from baldur.audit.checkpoint import FileCheckpointStorage

        file_path = tmp_path / "checkpoint.wb.json"
        with open(file_path, "w") as f:
            json.dump(
                {"last_sequence": 300, "timestamp": 1600000000.0, "version": 1}, f
            )

        storage = FileCheckpointStorage(base_path=tmp_path, sync_on_write=False)

        # When — 첫 로드로 write-back
        storage.load("wb")

        # Then — 두 번째 로드 시 통합 형식으로 이미 변환됨
        with open(file_path) as f:
            data = json.load(f)
        assert "wal_sequence" in data
        assert "last_sequence" not in data or "wal_sequence" in data


# =============================================================================
# E. get_age_seconds() Boundary Cases
# =============================================================================


class TestFileCheckpointStorageGetAgeSecondsBehavior:
    """FileCheckpointStorage.get_age_seconds() 경계값 검증."""

    @pytest.fixture
    def storage(self, tmp_path):
        from baldur.audit.checkpoint import FileCheckpointStorage

        return FileCheckpointStorage(base_path=tmp_path, sync_on_write=False)

    def test_get_age_seconds_returns_none_when_no_checkpoint(self, storage):
        """체크포인트가 없으면 None 반환."""
        result = storage.get_age_seconds("nonexistent")
        assert result is None

    def test_get_age_seconds_returns_positive_for_recent_checkpoint(self, storage):
        """방금 저장한 체크포인트의 age는 0 이상의 작은 값."""
        from baldur.audit.checkpoint import UnifiedCheckpointData

        storage.save("recent", UnifiedCheckpointData(wal_sequence=1))
        age = storage.get_age_seconds("recent")

        assert age is not None
        assert 0 <= age < 5  # 5초 이내

    def test_get_age_seconds_returns_none_for_invalid_timestamp(
        self, storage, tmp_path
    ):
        """유효하지 않은 timestamp 형식이면 None 반환."""
        # Given — 잘못된 timestamp 파일
        file_path = tmp_path / "checkpoint.bad_ts.json"
        with open(file_path, "w") as f:
            json.dump({"wal_sequence": 1, "timestamp": "not-a-date", "version": 1}, f)

        # When
        age = storage.get_age_seconds("bad_ts")

        # Then
        assert age is None

    def test_get_age_seconds_with_far_past_timestamp(self, storage, tmp_path):
        """과거 시점의 timestamp에 대해 큰 양수 반환."""
        file_path = tmp_path / "checkpoint.old.json"
        past_ts = "2020-01-01T00:00:00+00:00"
        with open(file_path, "w") as f:
            json.dump({"wal_sequence": 1, "timestamp": past_ts, "version": 1}, f)

        age = storage.get_age_seconds("old")

        assert age is not None
        assert age > 365 * 24 * 3600  # 최소 1년 이상

    def test_get_age_seconds_with_future_timestamp_returns_negative(
        self, storage, tmp_path
    ):
        """미래 시점의 timestamp에 대해 음수 반환."""
        file_path = tmp_path / "checkpoint.future.json"
        future_ts = "2099-01-01T00:00:00+00:00"
        with open(file_path, "w") as f:
            json.dump({"wal_sequence": 1, "timestamp": future_ts, "version": 1}, f)

        age = storage.get_age_seconds("future")

        assert age is not None
        assert age < 0


# =============================================================================
# F. K8s Detection + Warning Log
# =============================================================================


class TestK8sDetectionBehavior:
    """K8s 환경 감지 및 파일 저장소 경고 검증."""

    def test_is_k8s_environment_true_when_service_host_set(self, monkeypatch):
        """KUBERNETES_SERVICE_HOST 환경변수 설정 시 K8s 감지."""
        from baldur.audit.checkpoint import _is_k8s_environment

        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        assert _is_k8s_environment() is True

    def test_is_k8s_environment_true_when_port_set(self, monkeypatch):
        """KUBERNETES_PORT 환경변수 설정 시 K8s 감지."""
        from baldur.audit.checkpoint import _is_k8s_environment

        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.setenv("KUBERNETES_PORT", "tcp://10.0.0.1:443")
        assert _is_k8s_environment() is True

    def test_is_k8s_environment_false_when_no_indicators(self, monkeypatch):
        """K8s 관련 환경변수/경로 모두 없으면 False."""
        from baldur.audit.checkpoint import _is_k8s_environment

        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.delenv("KUBERNETES_PORT", raising=False)

        with patch.object(Path, "exists", return_value=False):
            assert _is_k8s_environment() is False

    def test_file_strategy_in_k8s_logs_warning(self, monkeypatch, tmp_path):
        """K8s 환경에서 file 전략 선택 시 경고 로그 출력."""
        from baldur.audit.checkpoint import get_checkpoint_strategy

        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

        with patch("baldur.audit.checkpoint.logger") as mock_logger:
            get_checkpoint_strategy(storage_type="file", base_path=tmp_path)

            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert "file_storage_in_k8s" in call_args[0][0]
