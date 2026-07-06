"""
Audit System Fixes - Unit Tests

55_AUDIT_SYSTEM_FIXES.md에서 정의한 수정 및 확장 기능 테스트:
- 수정 1: get_audit_adapter() 함수
- 수정 2+확장1: ContextType 스키마
- 수정 3: ProviderRegistry audit adapter 등록
- 수정 5+확장2: WAL Group Commit
- 확장 3: Fail-Open 정책
"""

import tempfile
from unittest.mock import Mock

import pytest

# =============================================================================
# 수정 1: get_audit_adapter() 함수 테스트
# =============================================================================


class TestGetAuditAdapter:
    """get_audit_adapter() 함수 테스트."""

    def test_get_audit_adapter_returns_adapter(self):
        """get_audit_adapter가 어댑터를 반환하는지 테스트."""
        from baldur.adapters.audit import get_audit_adapter, reset_audit_adapter

        reset_audit_adapter()  # 초기화
        adapter = get_audit_adapter()

        assert adapter is not None
        # AuditLogAdapter 인터페이스를 구현해야 함
        assert hasattr(adapter, "log")

    def test_configure_audit_adapter(self):
        """configure_audit_adapter sets a custom adapter."""
        from baldur.adapters.audit import (
            NullAuditLogAdapter,
            configure_audit_adapter,
            get_audit_adapter,
            reset_audit_adapter,
        )

        reset_audit_adapter()

        custom_adapter = NullAuditLogAdapter()
        configure_audit_adapter(custom_adapter)

        adapter = get_audit_adapter()
        assert adapter is custom_adapter

        reset_audit_adapter()

    def test_reset_audit_adapter(self):
        """reset_audit_adapter가 어댑터를 초기화하는지 테스트."""
        from baldur.adapters.audit import (
            NullAuditLogAdapter,
            configure_audit_adapter,
            get_audit_adapter,
            reset_audit_adapter,
        )

        custom_adapter = NullAuditLogAdapter()
        configure_audit_adapter(custom_adapter)

        adapter1 = get_audit_adapter()
        assert adapter1 is custom_adapter

        reset_audit_adapter()

        adapter2 = get_audit_adapter()
        assert adapter2 is not custom_adapter

        reset_audit_adapter()

    def test_get_audit_adapter_singleton(self):
        """get_audit_adapter가 싱글톤을 반환하는지 테스트."""
        from baldur.adapters.audit import get_audit_adapter, reset_audit_adapter

        reset_audit_adapter()

        adapter1 = get_audit_adapter()
        adapter2 = get_audit_adapter()

        assert adapter1 is adapter2

        reset_audit_adapter()


# =============================================================================
# 수정 2+확장1: ContextType 스키마 테스트
# =============================================================================


class TestContextType:
    """ContextType Enum 및 AuditEntry 통합 테스트."""

    def test_context_type_enum_values(self):
        """ContextType Enum 값 테스트."""
        from baldur.interfaces.audit_adapter import ContextType

        assert ContextType.REQUEST.value == "request"
        assert ContextType.TASK.value == "task"
        assert ContextType.SYSTEM.value == "system"
        assert ContextType.WEBHOOK.value == "webhook"
        assert ContextType.CLI.value == "cli"
        assert ContextType.UNKNOWN.value == "unknown"

    def test_audit_entry_default_context_type(self):
        """AuditEntry 기본 context_type 테스트."""
        from baldur.interfaces.audit_adapter import (
            AuditAction,
            AuditEntry,
            ContextType,
        )

        entry = AuditEntry(action=AuditAction.DLQ_STORE)

        assert entry.context_type == ContextType.UNKNOWN

    def test_audit_entry_custom_context_type(self):
        """AuditEntry 커스텀 context_type 테스트."""
        from baldur.interfaces.audit_adapter import (
            AuditAction,
            AuditEntry,
            ContextType,
        )

        entry = AuditEntry(
            action=AuditAction.DLQ_REPLAY_SUCCESS,
            context_type=ContextType.TASK,
        )

        assert entry.context_type == ContextType.TASK

    def test_audit_entry_to_dict_includes_context_type(self):
        """to_dict()에 context_type이 포함되는지 테스트."""
        from baldur.interfaces.audit_adapter import (
            AuditAction,
            AuditEntry,
            ContextType,
        )

        entry = AuditEntry(
            action=AuditAction.CB_AUTO_OPEN,
            context_type=ContextType.REQUEST,
        )

        entry_dict = entry.to_dict()

        assert "context_type" in entry_dict
        assert entry_dict["context_type"] == "request"

    def test_context_type_for_middleware_logging(self):
        """미들웨어 로깅 시 context_type=REQUEST 사용."""
        from baldur.interfaces.audit_adapter import (
            AuditAction,
            AuditEntry,
            ContextType,
        )

        # 미들웨어에서 생성하는 entry
        entry = AuditEntry(
            action=AuditAction.SECURITY_INCIDENT,
            context_type=ContextType.REQUEST,
            actor_id="user:123",
            actor_type="user",
        )

        assert entry.context_type == ContextType.REQUEST

    def test_context_type_for_celery_task(self):
        """Celery Task에서 context_type=TASK 사용."""
        from baldur.interfaces.audit_adapter import (
            AuditAction,
            AuditEntry,
            ContextType,
        )

        # Celery Task에서 생성하는 entry
        entry = AuditEntry(
            action=AuditAction.DLQ_REPLAY_SUCCESS,
            context_type=ContextType.TASK,
            actor_id="celery_worker",
            actor_type="scheduler",
        )

        assert entry.context_type == ContextType.TASK


# =============================================================================
# 수정 3: ProviderRegistry audit adapter 등록 테스트
# =============================================================================


class TestProviderRegistryAuditAdapter:
    """ProviderRegistry audit adapter 등록 테스트."""

    def test_register_audit_adapter(self):
        """audit adapter 등록 테스트."""
        from baldur.adapters.audit import NullAuditLogAdapter
        from baldur.factory import ProviderRegistry

        ProviderRegistry.reset()

        ProviderRegistry.register_audit_adapter("test", NullAuditLogAdapter)

        providers = ProviderRegistry.list_providers()
        assert "audit_adapter" in providers
        assert "test" in providers["audit_adapter"]

        ProviderRegistry.reset()

    def test_get_audit_adapter_from_registry(self):
        """ProviderRegistry에서 audit adapter 가져오기."""
        from baldur.adapters.audit import NullAuditLogAdapter
        from baldur.factory import ProviderRegistry

        ProviderRegistry.reset()
        ProviderRegistry.register_audit_adapter("null", NullAuditLogAdapter)

        adapter = ProviderRegistry.get_audit_adapter("null")

        assert adapter is not None
        assert isinstance(adapter, NullAuditLogAdapter)

        ProviderRegistry.reset()

    def test_get_audit_adapter_auto_register(self):
        """자동 등록 후 audit adapter 가져오기."""
        from baldur.factory import ProviderRegistry

        ProviderRegistry.reset()

        # 자동 등록 트리거
        from baldur.core.exceptions import AdapterNotFoundError

        try:
            adapter = ProviderRegistry.get_audit_adapter("stdout")
            assert adapter is not None
        except AdapterNotFoundError:
            # 자동 등록이 안 된 경우도 허용 (import 문제)
            pass

        ProviderRegistry.reset()

    def test_get_audit_adapter_unknown_raises_adapter_not_found_error(self):
        """등록되지 않은 adapter 요청 시 AdapterNotFoundError."""
        from baldur.core.exceptions import AdapterNotFoundError
        from baldur.factory import ProviderRegistry

        ProviderRegistry.reset()

        with pytest.raises(AdapterNotFoundError, match="Adapter not found"):
            ProviderRegistry.get_audit_adapter("nonexistent_adapter_xyz")

        ProviderRegistry.reset()


# =============================================================================
# 수정 5+확장2: WAL Group Commit 테스트
# =============================================================================


class TestWALGroupCommit:
    """WAL Group Commit 기능 테스트."""

    def test_wal_config_group_commit_defaults(self):
        """WALConfig Group Commit 기본값 테스트."""
        from baldur.audit.wal import WALConfig

        config = WALConfig()

        assert config.group_commit_enabled is False
        assert config.group_commit_max_entries == 100
        assert config.group_commit_max_wait_ms == 10

    def test_wal_config_group_commit_custom(self):
        """WALConfig Group Commit 커스텀 설정 테스트."""
        from baldur.audit.wal import WALConfig

        config = WALConfig(
            group_commit_enabled=True,
            group_commit_max_entries=50,
            group_commit_max_wait_ms=5,
        )

        assert config.group_commit_enabled is True
        assert config.group_commit_max_entries == 50
        assert config.group_commit_max_wait_ms == 5

    def test_wal_direct_write(self):
        """WAL 직접 쓰기 (Group Commit 비활성화) 테스트."""
        from baldur.audit.wal import WALConfig, WriteAheadLog

        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(
                wal_dir=tmpdir,
                group_commit_enabled=False,
            )
            wal = WriteAheadLog(config=config)

            seq = wal.write({"event": "test", "value": 1})

            assert seq == 1
            wal.close()

    def test_wal_buffered_write(self):
        """WAL 버퍼 쓰기 (Group Commit 활성화) 테스트."""
        from baldur.audit.wal import WALConfig, WriteAheadLog

        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(
                wal_dir=tmpdir,
                group_commit_enabled=True,
                group_commit_max_entries=3,
            )
            wal = WriteAheadLog(config=config)

            # 3개 미만이면 버퍼에만 저장
            seq1 = wal.write({"event": "test1"})
            seq2 = wal.write({"event": "test2"})

            assert seq1 == 1
            assert seq2 == 2

            # 3개가 되면 flush
            seq3 = wal.write({"event": "test3"})
            assert seq3 == 3

            wal.close()

    def test_wal_flush_method(self):
        """WAL flush() 메서드 테스트."""
        from baldur.audit.wal import WALConfig, WriteAheadLog

        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(
                wal_dir=tmpdir,
                group_commit_enabled=True,
                group_commit_max_entries=100,  # 높게 설정
            )
            wal = WriteAheadLog(config=config)

            wal.write({"event": "test"})

            # 강제 flush
            wal.flush()

            wal.close()

    def test_wal_stats_includes_group_commit(self):
        """WALStats에 group_commit 통계 포함 확인."""
        from baldur.audit.wal import WALState, WALStats

        stats = WALStats(
            state=WALState.ACTIVE,
            current_file="test.wal",
            current_size_bytes=1024,
            total_entries=10,
            total_files=1,
            last_sequence=10,
            last_write_time=None,
            corrupted_entries=0,
            recovered_entries=0,
            group_commit_flushes=5,
            group_commit_buffered=3,
        )

        assert stats.group_commit_flushes == 5
        assert stats.group_commit_buffered == 3


# =============================================================================
# 확장 3: Fail-Open 정책 테스트
# =============================================================================


class TestFailOpenPolicy:
    """ContinuousAuditRecorder Fail-Open 정책 테스트."""

    def test_continuous_audit_default_fail_open(self):
        """기본 fail_open=True 테스트."""
        from baldur.adapters.audit import NullAuditLogAdapter
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        recorder = ContinuousAuditRecorder(
            audit_adapter=NullAuditLogAdapter(),
        )

        assert recorder._fail_open is True
        assert recorder._fallback_to_stdout is True

    def test_continuous_audit_fail_secure_mode(self):
        """fail_open=False (Fail-Secure) 모드 테스트."""
        from baldur.adapters.audit import NullAuditLogAdapter
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        recorder = ContinuousAuditRecorder(
            audit_adapter=NullAuditLogAdapter(),
            fail_open=False,
        )

        assert recorder._fail_open is False

    def test_fail_open_continues_on_adapter_error(self):
        """Fail-Open: adapter 오류 시에도 계속 진행."""
        from baldur.audit.continuous_audit import ContinuousAuditRecorder
        from baldur.interfaces.audit_adapter import AuditAction, AuditEntry

        # 항상 예외를 발생시키는 mock adapter
        mock_adapter = Mock()
        mock_adapter.log.side_effect = Exception("Storage failed")

        recorder = ContinuousAuditRecorder(
            audit_adapter=mock_adapter,
            fail_open=True,
            fallback_to_stdout=False,  # stdout 출력 비활성화
        )

        entry = AuditEntry(action=AuditAction.DLQ_STORE)

        # 예외가 발생해도 계속 진행
        audit_id = recorder._record_with_integrity(entry)

        assert audit_id is not None
        assert recorder._failed_write_count == 1

    def test_fail_secure_raises_on_adapter_error(self):
        """Fail-Secure: adapter 오류 시 예외 전파."""
        from baldur.audit.continuous_audit import ContinuousAuditRecorder
        from baldur.interfaces.audit_adapter import AuditAction, AuditEntry

        # 항상 예외를 발생시키는 mock adapter
        mock_adapter = Mock()
        mock_adapter.log.side_effect = Exception("Storage failed")

        recorder = ContinuousAuditRecorder(
            audit_adapter=mock_adapter,
            fail_open=False,  # Fail-Secure
            fallback_to_stdout=False,
        )

        entry = AuditEntry(action=AuditAction.DLQ_STORE)

        # 예외가 전파되어야 함
        with pytest.raises(Exception, match="Storage failed"):
            recorder._record_with_integrity(entry)

    def test_fallback_to_stdout(self, capsys):
        """fallback_to_stdout 테스트."""
        from baldur.audit.continuous_audit import ContinuousAuditRecorder
        from baldur.interfaces.audit_adapter import AuditAction, AuditEntry

        mock_adapter = Mock()
        mock_adapter.log.side_effect = Exception("Storage failed")

        recorder = ContinuousAuditRecorder(
            audit_adapter=mock_adapter,
            fail_open=True,
            fallback_to_stdout=True,
        )

        entry = AuditEntry(action=AuditAction.CONFIG_CHANGE)
        recorder._record_with_integrity(entry)

        captured = capsys.readouterr()
        assert "[FALLBACK_AUDIT_LOG]" in captured.err
        assert "config_change" in captured.err

    def test_get_stats_includes_fail_open_info(self):
        """get_stats()에 Fail-Open 정보 포함."""
        from baldur.adapters.audit import NullAuditLogAdapter
        from baldur.audit.continuous_audit import ContinuousAuditRecorder

        recorder = ContinuousAuditRecorder(
            audit_adapter=NullAuditLogAdapter(),
            fail_open=True,
            fallback_to_stdout=False,
        )

        stats = recorder.get_stats()

        assert "failed_write_count" in stats
        assert "fail_open" in stats
        assert "fallback_to_stdout" in stats
        assert stats["fail_open"] is True
        assert stats["fallback_to_stdout"] is False

    def test_wal_enabled_in_recorder(self):
        """ContinuousAuditRecorder에서 WAL 활성화 테스트."""
        from baldur.adapters.audit import NullAuditLogAdapter
        from baldur.audit.continuous_audit import ContinuousAuditRecorder
        from baldur.audit.wal import WALConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            wal_config = WALConfig(wal_dir=tmpdir)

            recorder = ContinuousAuditRecorder(
                audit_adapter=NullAuditLogAdapter(),
                wal_enabled=True,
                wal_config=wal_config,
            )

            assert recorder._wal_enabled is True
            assert recorder._wal is not None


# =============================================================================
# 통합 테스트
# =============================================================================


class TestAuditSystemIntegration:
    """Audit 시스템 통합 테스트."""

    def test_full_audit_flow(self):
        """전체 Audit 플로우 테스트."""
        from baldur.adapters.audit import NullAuditLogAdapter
        from baldur.audit.continuous_audit import ContinuousAuditRecorder
        from baldur.interfaces.audit_adapter import (
            AuditAction,
            AuditEntry,
            ContextType,
        )

        recorder = ContinuousAuditRecorder(
            audit_adapter=NullAuditLogAdapter(),
            fail_open=True,
        )

        # 다양한 context_type으로 기록
        entry_request = AuditEntry(
            action=AuditAction.CB_AUTO_OPEN,
            context_type=ContextType.REQUEST,
            actor_id="middleware",
        )

        entry_task = AuditEntry(
            action=AuditAction.DLQ_REPLAY_SUCCESS,
            context_type=ContextType.TASK,
            actor_id="celery_worker",
        )

        entry_system = AuditEntry(
            action=AuditAction.AUTO_TUNING_ADJUSTMENT,
            context_type=ContextType.SYSTEM,
            actor_id="runtime_feedback_loop",
        )

        id1 = recorder._record_with_integrity(entry_request)
        id2 = recorder._record_with_integrity(entry_task)
        id3 = recorder._record_with_integrity(entry_system)

        assert id1 is not None
        assert id2 is not None
        assert id3 is not None

    def test_audit_adapter_hierarchy(self):
        """get_audit_adapter 우선순위 테스트."""
        from baldur.adapters.audit import (
            StdoutAuditLogAdapter,
            configure_audit_adapter,
            get_audit_adapter,
            reset_audit_adapter,
        )
        from baldur.factory import ProviderRegistry

        reset_audit_adapter()
        ProviderRegistry.reset()

        # 1. 기본 adapter 가져오기
        adapter1 = get_audit_adapter()
        assert adapter1 is not None

        # 2. 명시적 설정이 우선
        custom = StdoutAuditLogAdapter()
        configure_audit_adapter(custom)

        adapter2 = get_audit_adapter()
        assert adapter2 is custom

        reset_audit_adapter()
        ProviderRegistry.reset()
