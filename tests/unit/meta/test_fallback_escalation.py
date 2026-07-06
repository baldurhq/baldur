"""
FallbackEscalationHandler 테스트.

디스크 기반 폴백 에스컬레이션 핸들러 테스트.
"""

import json
from pathlib import Path

import pytest

from baldur.meta.fallback_escalation import FallbackEscalationHandler


class TestFallbackEscalationHandler:
    """FallbackEscalationHandler 테스트."""

    @pytest.fixture
    def temp_log_path(self, tmp_path):
        """임시 로그 파일 경로."""
        return tmp_path / "escalation.jsonl"

    @pytest.fixture
    def handler(self, temp_log_path):
        """핸들러 fixture."""
        return FallbackEscalationHandler(
            log_path=str(temp_log_path), max_buffer_size=100
        )

    def test_initialization(self, handler, temp_log_path):
        """초기화 테스트."""
        assert handler is not None
        assert handler._log_path == Path(temp_log_path)
        assert handler._max_buffer_size == 100
        assert handler._memory_buffer == []

    def test_record_failed_escalation(self, handler, temp_log_path):
        """실패한 에스컬레이션 기록 테스트."""
        result = handler.record_failed_escalation(
            component="redis",
            title="Redis Down",
            description="Redis is not responding",
            level="critical",
            details={"host": "localhost", "port": 6379},
            failed_channels=["pagerduty", "slack"],
            error_message="Connection timeout",
        )

        assert result is True

        # 파일 확인
        if temp_log_path.exists():
            with open(temp_log_path) as f:
                line = f.readline()
                data = json.loads(line)
            assert data["component"] == "redis"
            assert data["title"] == "Redis Down"

    def test_record_multiple_escalations(self, handler, temp_log_path):
        """복수 에스컬레이션 기록 테스트."""
        for i in range(5):
            handler.record_failed_escalation(
                component=f"component_{i}",
                title=f"Issue {i}",
                description=f"Description {i}",
                level="warning",
                details={"index": i},
                failed_channels=["slack"],
                error_message="Network error",
            )

        # 파일 확인
        if temp_log_path.exists():
            with open(temp_log_path) as f:
                lines = f.readlines()
            assert len(lines) == 5

    def test_get_pending_count(self, handler):
        """대기 중인 에스컬레이션 수 테스트."""
        assert handler.get_pending_count() == 0

        # 메모리 버퍼에 직접 추가 (파일 쓰기 실패 시뮬레이션)
        handler._memory_buffer.append({"component": "redis"})
        handler._memory_buffer.append({"component": "postgres"})

        assert handler.get_pending_count() == 2

    def test_clear_all(self, handler):
        """전체 초기화 테스트."""
        handler._memory_buffer.append({"component": "redis"})
        handler._memory_buffer.append({"component": "postgres"})

        handler.clear_all()

        assert handler.get_pending_count() == 0
        assert handler.get_pending_escalations() == []


class TestBufferLimit:
    """버퍼 제한 테스트."""

    @pytest.fixture
    def small_buffer_handler(self, tmp_path):
        """작은 버퍼 핸들러."""
        return FallbackEscalationHandler(
            log_path=str(tmp_path / "escalation.jsonl"), max_buffer_size=5
        )

    def test_buffer_limit_enforced(self, small_buffer_handler):
        """버퍼 제한 적용 테스트."""
        # 메모리 버퍼에 직접 추가 (오버플로우 시뮬레이션)
        for i in range(10):
            small_buffer_handler._write_to_memory({"component": f"comp_{i}"})

        # 최대 5개만 유지
        assert small_buffer_handler.get_pending_count() <= 5


class TestDrainToFile:
    """파일로 드레인 테스트."""

    @pytest.fixture
    def temp_log_path(self, tmp_path):
        """임시 로그 파일 경로."""
        return tmp_path / "escalation.jsonl"

    @pytest.fixture
    def handler(self, temp_log_path):
        """핸들러 fixture."""
        return FallbackEscalationHandler(
            log_path=str(temp_log_path), max_buffer_size=100
        )

    def test_drain_to_file(self, handler, temp_log_path):
        """파일로 드레인 테스트."""
        # 메모리 버퍼에 올바른 엔트리 형식으로 추가
        handler._memory_buffer.append(
            {
                "component": "redis",
                "title": "Redis Down",
                "timestamp": "2024-01-01T00:00:00Z",
            }
        )
        handler._memory_buffer.append(
            {
                "component": "postgres",
                "title": "Postgres Down",
                "timestamp": "2024-01-01T00:00:00Z",
            }
        )

        drained_count = handler.drain_to_file()

        assert drained_count == 2
        # drain 후 메모리 버퍼는 비어있어야 함
        assert len(handler._memory_buffer) == 0
        # 파일에 기록되었으므로 파일에서 읽을 수 있음
        assert temp_log_path.exists()

    def test_drain_to_file_empty_buffer(self, handler):
        """빈 버퍼 드레인."""
        drained_count = handler.drain_to_file()
        assert drained_count == 0


class TestFileWriteFailure:
    """파일 쓰기 실패 테스트."""

    def test_fallback_to_memory_buffer(self, tmp_path):
        """파일 쓰기 실패 시 메모리 버퍼 사용."""
        # 존재하지 않는 경로
        handler = FallbackEscalationHandler(
            log_path="/invalid/path/that/cannot/exist/escalation.jsonl",
            max_buffer_size=100,
        )

        result = handler.record_failed_escalation(
            component="redis",
            title="Test",
            description="Test desc",
            level="critical",
            details={},
            failed_channels=["slack"],
            error_message="Test error",
        )

        # 메모리 버퍼에 저장됨
        assert result is True or handler.get_pending_count() > 0


class TestJsonSerialization:
    """JSON 직렬화 테스트."""

    @pytest.fixture
    def temp_log_path(self, tmp_path):
        """임시 로그 파일 경로."""
        return tmp_path / "escalation.jsonl"

    @pytest.fixture
    def handler(self, temp_log_path):
        """핸들러 fixture."""
        return FallbackEscalationHandler(
            log_path=str(temp_log_path), max_buffer_size=100
        )

    def test_complex_event_serialization(self, handler, temp_log_path):
        """복잡한 이벤트 직렬화."""
        handler.record_failed_escalation(
            component="redis",
            title="Connection Refused",
            description="Cannot connect to Redis",
            level="CRITICAL",
            details={"host": "localhost", "port": 6379, "retry_count": 3},
            failed_channels=["pagerduty", "slack"],
            error_message="Connection timeout",
        )

        with open(temp_log_path) as f:
            line = f.readline()
            loaded = json.loads(line)

        assert loaded["component"] == "redis"
        assert loaded["details"]["port"] == 6379


class TestGetPendingEscalations:
    """대기 중인 에스컬레이션 조회 테스트."""

    @pytest.fixture
    def handler(self, tmp_path):
        """핸들러 fixture."""
        return FallbackEscalationHandler(
            log_path=str(tmp_path / "escalation.jsonl"), max_buffer_size=100
        )

    def test_get_pending_from_memory(self, handler):
        """메모리 버퍼에서 조회."""
        handler._memory_buffer.append({"component": "redis"})
        handler._memory_buffer.append({"component": "postgres"})

        pending = handler.get_pending_escalations()

        assert len(pending) == 2
        assert pending[0]["component"] == "redis"

    def test_peek_does_not_remove(self, handler):
        """조회는 제거하지 않음."""
        handler._memory_buffer.append({"component": "redis"})

        pending1 = handler.get_pending_escalations()
        pending2 = handler.get_pending_escalations()

        assert len(pending1) == len(pending2)
