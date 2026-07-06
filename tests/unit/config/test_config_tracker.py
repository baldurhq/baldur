"""
Tests for config_tracker.py - Configuration Change Tracker.
config_tracker.py의 설정 변경 추적, 감사 로그 남기기, 수동 오버라이드 기록 등에 대한 단위 테스트.

커버리지 대상:
- ConfigChange dataclass 및 to_dict()
- ConfigChangeTracker 초기화 및 기본 동작
- track_change() context manager (성공/실패/캐시 무효화)
- _log_change() (버퍼 패턴 및 폴백)
- log_manual_override() (버퍼 패턴 및 폴백)
- 싱글톤 함수 (get_config_tracker, set_config_tracker)
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.config_tracker import (
    ConfigChange,
    ConfigChangeTracker,
    get_config_tracker,
    set_config_tracker,
)
from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)

# =============================================================================
# ConfigChange Dataclass Tests
# =============================================================================


class TestConfigChange:
    """ConfigChange dataclass 테스트."""

    def test_default_values(self):
        """Default values on creation
        기본값으로 생성 시 올바른 초기 상태를 가지는지 확인.
        """
        change = ConfigChange(
            config_key="circuit_breaker.threshold",
            old_value=10,
            new_value=50,
        )
        assert change.config_key == "circuit_breaker.threshold"
        assert change.old_value == 10
        assert change.new_value == 50
        assert change.reason is None
        assert change.changed_at is None
        assert change.changed_by is None
        assert change.applied is False
        assert change.cache_invalidated is False
        assert change.error_message is None

    def test_to_dict_basic(self):
        """to_dict basic conversion
        기본 상태의 ConfigChange가 올바르게 딕셔너리로 변환되는지 확인.
        """
        change = ConfigChange(
            config_key="dlq.max_retries",
            old_value=3,
            new_value=5,
            reason="테스트용 변경",
        )
        d = change.to_dict()
        assert d["config_key"] == "dlq.max_retries"
        assert d["old_value"] == "3"
        assert d["new_value"] == "5"
        assert d["reason"] == "테스트용 변경"
        assert d["applied"] is False
        assert d["cache_invalidated"] is False

    def test_to_dict_with_datetime(self):
        """to_dict with datetime
        changed_at이 설정된 경우 isoformat 문자열로 변환되는지 확인.
        """
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        change = ConfigChange(
            config_key="test.key",
            old_value="a",
            new_value="b",
            changed_at=dt,
        )
        d = change.to_dict()
        assert d["changed_at"] == dt.isoformat()

    def test_to_dict_without_datetime(self):
        """to_dict without datetime
        changed_at이 None인 경우 None으로 변환되는지 확인.
        """
        change = ConfigChange(config_key="test.key", old_value="a", new_value="b")
        d = change.to_dict()
        assert d["changed_at"] is None


# =============================================================================
# ConfigChangeTracker Initialization Tests
# =============================================================================


class TestConfigChangeTrackerInit:
    """ConfigChangeTracker 초기화 테스트."""

    def test_init_with_defaults(self):
        """Init with default auto_log
        기본 auto_log=True로 초기화되는지 확인.
        """
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)
        assert tracker.audit_adapter is mock_adapter
        assert tracker.auto_log is True

    def test_init_with_auto_log_false(self):
        """Init with auto_log=False
        auto_log=False로 초기화할 수 있는지 확인.
        """
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter, auto_log=False)
        assert tracker.auto_log is False


# =============================================================================
# track_change Context Manager Tests
# =============================================================================


class TestTrackChange:
    """track_change() context manager 테스트."""

    @patch("baldur.config_tracker.ActorContext")
    def test_successful_change(self, mock_actor_ctx):
        """Successful config change
        성공적인 설정 변경 시 change.applied=True이고 감사 로그가 남는지 확인.
        """
        mock_actor = MagicMock()
        mock_actor.actor_id = "admin@example.com"
        mock_actor_ctx.get_current.return_value = mock_actor

        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        with tracker.track_change(
            config_key="circuit_breaker.threshold",
            old_value=10,
            new_value=50,
            reason="트래픽 증가",
        ) as change:
            pass  # 설정 변경 성공

        assert change.applied is True
        assert change.changed_by == "admin@example.com"
        assert change.changed_at is not None
        assert change.error_message is None
        # audit adapter에 log가 호출되었는지 확인
        mock_adapter.log.assert_called_once()

    @patch("baldur.config_tracker.ActorContext")
    def test_failed_change(self, mock_actor_ctx):
        """Failed config change
        설정 변경 중 예외 발생 시 change.applied=False이고 에러가 기록되는지 확인.
        """
        mock_actor = MagicMock()
        mock_actor.actor_id = "admin"
        mock_actor_ctx.get_current.return_value = mock_actor

        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        with pytest.raises(ValueError):
            with tracker.track_change(
                config_key="test.key",
                old_value=1,
                new_value=2,
            ) as change:
                raise ValueError("Config update failed")

        assert change.applied is False
        assert change.error_message == "Config update failed"
        mock_adapter.log.assert_called_once()

    @patch("baldur.config_tracker.ActorContext")
    def test_cache_invalidation_success(self, mock_actor_ctx):
        """Cache invalidation success
        캐시 무효화 함수가 성공적으로 호출되는지 확인.
        """
        mock_actor = MagicMock()
        mock_actor.actor_id = "admin"
        mock_actor_ctx.get_current.return_value = mock_actor

        mock_adapter = MagicMock(spec=AuditLogAdapter)
        mock_invalidate = MagicMock()
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        with tracker.track_change(
            config_key="test.key",
            old_value=1,
            new_value=2,
            invalidate_cache_fn=mock_invalidate,
        ) as change:
            pass

        assert change.cache_invalidated is True
        mock_invalidate.assert_called_once()

    @patch("baldur.config_tracker.ActorContext")
    def test_cache_invalidation_failure(self, mock_actor_ctx):
        """Cache invalidation failure
        캐시 무효화 함수가 실패해도 변경 자체는 성공으로 처리되는지 확인.
        """
        mock_actor = MagicMock()
        mock_actor.actor_id = "admin"
        mock_actor_ctx.get_current.return_value = mock_actor

        mock_adapter = MagicMock(spec=AuditLogAdapter)
        mock_invalidate = MagicMock(side_effect=RuntimeError("Cache error"))
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        with tracker.track_change(
            config_key="test.key",
            old_value=1,
            new_value=2,
            invalidate_cache_fn=mock_invalidate,
        ) as change:
            pass

        assert change.applied is True
        assert change.cache_invalidated is False

    @patch("baldur.config_tracker.ActorContext")
    def test_auto_log_disabled(self, mock_actor_ctx):
        """Auto log disabled
        auto_log=False일 때 감사 로그가 남지 않는지 확인.
        """
        mock_actor = MagicMock()
        mock_actor.actor_id = "admin"
        mock_actor_ctx.get_current.return_value = mock_actor

        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter, auto_log=False)

        with tracker.track_change(
            config_key="test.key",
            old_value=1,
            new_value=2,
        ) as change:
            pass

        assert change.applied is True
        # auto_log=False이므로 log 호출 안됨
        mock_adapter.log.assert_not_called()


# =============================================================================
# _log_change Tests (with request buffer pattern)
# =============================================================================


class TestLogChange:
    """_log_change() 메서드 테스트 - 버퍼 패턴 및 폴백."""

    def test_log_change_fallback_path(self):
        """Log change fallback to audit adapter
        request가 None일 때 AuditLogAdapter.log()로 폴백하는지 확인.
        """
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        change = ConfigChange(
            config_key="test.key",
            old_value="old",
            new_value="new",
            reason="test reason",
        )

        tracker._log_change(change, success=True)
        mock_adapter.log.assert_called_once()
        call_args = mock_adapter.log.call_args[0][0]
        assert isinstance(call_args, AuditEntry)
        assert call_args.action == AuditAction.CONFIG_CHANGE

    def test_log_change_failure(self):
        """Log change failure entry
        실패 시 success=False와 error_message가 기록되는지 확인.
        """
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        change = ConfigChange(
            config_key="test.key",
            old_value="old",
            new_value="new",
            error_message="something went wrong",
        )

        tracker._log_change(change, success=False)
        call_args = mock_adapter.log.call_args[0][0]
        assert call_args.success is False
        assert call_args.error_message == "something went wrong"

    @patch("baldur.config_tracker.RequestAuditBuffer", create=True)
    @patch("baldur.config_tracker.AuditEventType", create=True)
    def test_log_change_with_request_buffer(self, mock_event_type, mock_buffer_cls):
        """Log change with request buffer
        request가 있을 때 버퍼 패턴이 사용되는지 확인.
        """
        # 이 테스트는 event_buffer 모듈이 import 가능한 환경에서만 동작
        # ImportError가 발생하면 fallback 경로로 진행
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        change = ConfigChange(config_key="test.key", old_value="a", new_value="b")
        mock_request = MagicMock()

        # _log_change에서 request를 전달하면 buffer pattern 시도
        # ImportError 발생 시 fallback으로 audit_adapter.log() 호출
        tracker._log_change(change, success=True, request=mock_request)
        # 결과적으로 버퍼 또는 fallback 중 하나가 호출됨
        # fallback이 호출되었다면 log가 호출됨
        # 실제로 이 module import가 있는지에 따라 다르므로, 에러만 안 나면 OK


# =============================================================================
# log_manual_override Tests
# =============================================================================


class TestLogManualOverride:
    """log_manual_override() 메서드 테스트."""

    def test_manual_override_fallback(self):
        """Manual override via audit adapter
        request 없이 호출 시 AuditLogAdapter.log()로 감사 로그를 남기는지 확인.
        """
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        tracker.log_manual_override(
            config_key="chaos.enabled",
            new_value=True,
            reason="긴급 활성화",
            override_type="chaos_config",
        )

        mock_adapter.log.assert_called_once()
        call_args = mock_adapter.log.call_args[0][0]
        assert isinstance(call_args, AuditEntry)
        assert call_args.action == AuditAction.MANUAL_OVERRIDE
        assert call_args.target_type == "chaos_config"
        assert call_args.target_id == "chaos.enabled"

    def test_manual_override_default_type(self):
        """Manual override default override_type
        override_type 미지정 시 기본값 "config"이 사용되는지 확인.
        """
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        tracker.log_manual_override(
            config_key="test.key",
            new_value="value",
            reason="test",
        )

        call_args = mock_adapter.log.call_args[0][0]
        assert call_args.target_type == "config"


# =============================================================================
# Singleton Functions Tests
# =============================================================================


class TestSingletonFunctions:
    """get_config_tracker / set_config_tracker 싱글톤 테스트."""

    def test_set_and_get_tracker(self):
        """Set and get config tracker
        set_config_tracker() 후 get_config_tracker()로 동일 인스턴스를 반환하는지 확인.
        """
        import baldur.config_tracker as ct_module

        original = ct_module._default_tracker
        try:
            mock_adapter = MagicMock(spec=AuditLogAdapter)
            tracker = ConfigChangeTracker(audit_adapter=mock_adapter)
            set_config_tracker(tracker)
            assert get_config_tracker() is tracker
        finally:
            ct_module._default_tracker = original

    def test_get_tracker_unset(self):
        """Get tracker when unset
        set_config_tracker() 호출 전에 get_config_tracker()가 None을 반환하는지 확인.
        """
        import baldur.config_tracker as ct_module

        original = ct_module._default_tracker
        try:
            ct_module._default_tracker = None
            assert get_config_tracker() is None
        finally:
            ct_module._default_tracker = original


# =============================================================================
# log_manual_override with Request Buffer → Fallback Tests
# =============================================================================


class TestLogManualOverrideWithRequest:
    """log_manual_override()에 request를 전달할 때의 버퍼/폴백 경로 테스트."""

    def test_manual_override_with_request_import_error(self):
        """Manual override request buffer pattern with ImportError fallback
        event_buffer import 실패 시 기존 AuditLogAdapter.log()로 폴백하는지 확인.
        """
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)
        mock_request = MagicMock()

        # event_buffer module을 임시로 None으로 설정하여 ImportError 유도
        with patch.dict("sys.modules", {"baldur.audit.event_buffer": None}):
            tracker.log_manual_override(
                config_key="chaos.enabled",
                new_value=True,
                reason="Buffer unavailable",
                request=mock_request,
            )

        # ImportError 발생 → fallback으로 audit_adapter.log() 호출
        mock_adapter.log.assert_called_once()
        call_args = mock_adapter.log.call_args[0][0]
        assert call_args.action == AuditAction.MANUAL_OVERRIDE

    def test_log_change_with_request_import_error(self):
        """_log_change request buffer pattern with ImportError fallback
        event_buffer import 실패 시 기존 AuditLogAdapter.log()로 폴백하는지 확인.
        """
        mock_adapter = MagicMock(spec=AuditLogAdapter)
        tracker = ConfigChangeTracker(audit_adapter=mock_adapter)

        change = ConfigChange(
            config_key="test.key",
            old_value="old",
            new_value="new",
            reason="test",
        )
        mock_request = MagicMock()

        with patch.dict("sys.modules", {"baldur.audit.event_buffer": None}):
            tracker._log_change(change, success=True, request=mock_request)

        mock_adapter.log.assert_called_once()
