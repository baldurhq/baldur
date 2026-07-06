"""
CBStateSnapshot 단위 테스트.

테스트 항목:
- Shared Memory 초기화
- CB 상태 읽기/쓰기
- 스냅샷 업데이트
- 플랫폼별 동작
"""

from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import patch

import pytest

from baldur.adapters.ipc.cb_state_snapshot import (
    CBState,
    CBStateEntry,
    CBStateSnapshot,
    get_cb_state_snapshot,
    reset_cb_state_snapshot,
)


class TestCBStateEntry:
    """CBStateEntry 테스트."""

    def test_create_entry(self):
        """엔트리 생성."""
        entry = CBStateEntry(
            cb_id="test_cb",
            state=CBState.CLOSED,
            failure_count=0,
            success_count=100,
            last_failure_ts=0.0,
            last_success_ts=time.time(),
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )

        assert entry.cb_id == "test_cb"
        assert entry.state == CBState.CLOSED

    def test_is_open(self):
        """Open 상태 확인."""
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.OPEN,
            failure_count=5,
            success_count=0,
            last_failure_ts=time.time(),
            last_success_ts=0.0,
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )

        assert entry.is_open
        assert not entry.is_closed
        assert not entry.is_half_open

    def test_is_closed(self):
        """Closed 상태 확인."""
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.CLOSED,
            failure_count=0,
            success_count=100,
            last_failure_ts=0.0,
            last_success_ts=time.time(),
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )

        assert entry.is_closed
        assert not entry.is_open

    def test_is_half_open(self):
        """Half-Open 상태 확인."""
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.HALF_OPEN,
            failure_count=5,
            success_count=0,
            last_failure_ts=time.time() - 31,  # 31초 전
            last_success_ts=0.0,
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )

        assert entry.is_half_open

    def test_should_allow_closed(self):
        """Closed 상태에서 허용."""
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.CLOSED,
            failure_count=0,
            success_count=100,
            last_failure_ts=0.0,
            last_success_ts=time.time(),
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )

        assert entry.should_allow() is True

    def test_should_allow_open_before_timeout(self):
        """Open 상태, 타임아웃 전에는 거부."""
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.OPEN,
            failure_count=5,
            success_count=0,
            last_failure_ts=time.time(),  # 방금 실패
            last_success_ts=0.0,
            failure_threshold=5,
            recovery_timeout_ms=30000.0,  # 30초
        )

        assert entry.should_allow() is False

    def test_should_allow_open_after_timeout(self):
        """Open 상태, 타임아웃 후에는 허용."""
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.OPEN,
            failure_count=5,
            success_count=0,
            last_failure_ts=time.time() - 31,  # 31초 전
            last_success_ts=0.0,
            failure_threshold=5,
            recovery_timeout_ms=30000.0,  # 30초
        )

        assert entry.should_allow() is True

    def test_should_allow_half_open(self):
        """Half-Open 상태에서 허용."""
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.HALF_OPEN,
            failure_count=5,
            success_count=0,
            last_failure_ts=time.time() - 31,
            last_success_ts=0.0,
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )

        assert entry.should_allow() is True

    def test_to_dict(self):
        """딕셔너리 변환."""
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.CLOSED,
            failure_count=0,
            success_count=100,
            last_failure_ts=0.0,
            last_success_ts=time.time(),
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )

        result = entry.to_dict()

        assert result["cb_id"] == "test"
        assert result["state"] == "CLOSED"
        assert result["failure_count"] == 0


class TestCBStateSnapshot:
    """CBStateSnapshot 테스트."""

    @pytest.fixture
    def temp_shm_path(self):
        """임시 SHM 파일 경로."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".shm") as f:
            path = f.name
        yield path
        # 정리
        try:
            os.unlink(path)
        except OSError:
            pass

    @pytest.fixture
    def writer_snapshot(self, temp_shm_path):
        """Writer 모드 스냅샷 (sync 비활성화로 CB 서비스 오염 방지)."""
        snapshot = CBStateSnapshot(shm_path=temp_shm_path, is_writer=True)
        # _sync_from_registry가 CB 서비스 싱글톤에서 stale 데이터를
        # 가져오는 것을 방지하여 테스트 격리 보장
        snapshot._sync_from_registry = lambda: None
        snapshot.start()
        yield snapshot
        snapshot.stop()

    def test_init(self, temp_shm_path):
        """초기화 테스트."""
        snapshot = CBStateSnapshot(shm_path=temp_shm_path)

        assert snapshot.shm_path == temp_shm_path
        assert not snapshot._running

    def test_start_stop_writer(self, temp_shm_path):
        """Writer 시작/중지."""
        snapshot = CBStateSnapshot(shm_path=temp_shm_path, is_writer=True)

        snapshot.start()
        assert snapshot._running is True

        snapshot.stop()
        assert snapshot._running is False

    def test_update_and_get_state(self, writer_snapshot, temp_shm_path):
        """상태 업데이트 및 조회."""
        entry = CBStateEntry(
            cb_id="payment_service",
            state=CBState.CLOSED,
            failure_count=0,
            success_count=100,
            last_failure_ts=0.0,
            last_success_ts=time.time(),
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )

        result = writer_snapshot.update_state(entry)
        assert result is True

        # 조회
        retrieved = writer_snapshot.get_state("payment_service")

        assert retrieved is not None
        assert retrieved.cb_id == "payment_service"
        assert retrieved.state == CBState.CLOSED

    def test_get_state_nonexistent(self, writer_snapshot):
        """존재하지 않는 CB 조회."""
        result = writer_snapshot.get_state("nonexistent_cb")

        assert result is None

    def test_update_existing_state(self, writer_snapshot):
        """기존 상태 업데이트."""
        # 초기 상태 설정
        entry1 = CBStateEntry(
            cb_id="test_cb",
            state=CBState.CLOSED,
            failure_count=0,
            success_count=100,
            last_failure_ts=0.0,
            last_success_ts=time.time(),
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )
        writer_snapshot.update_state(entry1)

        # 상태 변경
        entry2 = CBStateEntry(
            cb_id="test_cb",
            state=CBState.OPEN,
            failure_count=5,
            success_count=100,
            last_failure_ts=time.time(),
            last_success_ts=0.0,
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )
        writer_snapshot.update_state(entry2)

        # 조회
        retrieved = writer_snapshot.get_state("test_cb")

        assert retrieved.state == CBState.OPEN
        assert retrieved.failure_count == 5

    def test_get_all_states(self, writer_snapshot):
        """모든 상태 조회."""
        # 여러 CB 상태 설정
        for i in range(3):
            entry = CBStateEntry(
                cb_id=f"cb_{i}",
                state=CBState.CLOSED,
                failure_count=0,
                success_count=i * 10,
                last_failure_ts=0.0,
                last_success_ts=time.time(),
                failure_threshold=5,
                recovery_timeout_ms=30000.0,
            )
            writer_snapshot.update_state(entry)

        all_states = writer_snapshot.get_all_states()

        assert len(all_states) == 3
        cb_ids = [s.cb_id for s in all_states]
        assert "cb_0" in cb_ids
        assert "cb_1" in cb_ids
        assert "cb_2" in cb_ids

    def test_get_stats(self, writer_snapshot):
        """통계 조회."""
        # 몇 가지 작업 수행
        entry = CBStateEntry(
            cb_id="test",
            state=CBState.CLOSED,
            failure_count=0,
            success_count=0,
            last_failure_ts=0.0,
            last_success_ts=0.0,
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )
        writer_snapshot.update_state(entry)
        writer_snapshot.get_state("test")

        stats = writer_snapshot.get_stats()

        assert "read_count" in stats
        assert "write_count" in stats
        assert stats["is_running"] is True
        assert stats["is_writer"] is True

    def test_reader_mode(self, writer_snapshot, temp_shm_path):
        """Reader 모드."""
        # Writer가 상태 설정
        entry = CBStateEntry(
            cb_id="shared_cb",
            state=CBState.OPEN,
            failure_count=5,
            success_count=0,
            last_failure_ts=time.time(),
            last_success_ts=0.0,
            failure_threshold=5,
            recovery_timeout_ms=30000.0,
        )
        writer_snapshot.update_state(entry)

        # Reader가 읽기
        reader = CBStateSnapshot(shm_path=temp_shm_path, is_writer=False)
        reader.start()

        try:
            retrieved = reader.get_state("shared_cb")

            assert retrieved is not None
            assert retrieved.cb_id == "shared_cb"
            assert retrieved.state == CBState.OPEN
        finally:
            reader.stop()


class TestCBStateSnapshotSingleton:
    """싱글톤 패턴 테스트."""

    def test_get_cb_state_snapshot(self):
        """싱글톤 인스턴스 반환."""
        reset_cb_state_snapshot()

        snapshot1 = get_cb_state_snapshot()
        snapshot2 = get_cb_state_snapshot()

        assert snapshot1 is snapshot2

        # 정리
        reset_cb_state_snapshot()

    def test_reset_cb_state_snapshot(self):
        """싱글톤 리셋."""
        reset_cb_state_snapshot()

        snapshot1 = get_cb_state_snapshot()
        reset_cb_state_snapshot()
        snapshot2 = get_cb_state_snapshot()

        assert snapshot1 is not snapshot2

        # 정리
        reset_cb_state_snapshot()

    def test_reset_clears_singleton_even_if_stop_fails(self):
        """Singleton is cleared even when stop() raises. (L1 fork-safety)"""
        reset_cb_state_snapshot()
        instance = get_cb_state_snapshot()
        assert instance is not None

        with patch.object(instance, "stop", side_effect=RuntimeError("dead thread")):
            reset_cb_state_snapshot()

        new_instance = get_cb_state_snapshot()
        assert new_instance is not instance


class TestCBState:
    """CBState Enum 테스트."""

    def test_state_values(self):
        """상태 값."""
        assert CBState.CLOSED.value == 0
        assert CBState.OPEN.value == 1
        assert CBState.HALF_OPEN.value == 2

    def test_state_from_int(self):
        """정수에서 상태 변환."""
        assert CBState(0) == CBState.CLOSED
        assert CBState(1) == CBState.OPEN
        assert CBState(2) == CBState.HALF_OPEN
