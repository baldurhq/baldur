"""
설정 모듈 structlog_config 단위 테스트.

검증 대상:
- configure_structlog(): structured_json 설정에 따라 올바른 렌더러를 선택하고
  stdlib root logger에 ProcessorFormatter를 등록한다.
- _inject_otel_trace_context(): OTEL 컨텍스트가 있을 때 trace_id/span_id를
  event_dict에 주입하고, 없거나 ImportError 시 event_dict를 그대로 반환한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import structlog

# =============================================================================
# 공통 픽스처
# =============================================================================


@pytest.fixture(autouse=True)
def reset_logging_settings():
    """각 테스트 전후로 LoggingSettings 싱글톤 및 structlog 설정을 초기화해 환경 격리."""
    from baldur.observability.structlog_config import reset_structlog_config
    from baldur.settings.logging_settings import reset_logging_settings

    reset_logging_settings()
    reset_structlog_config()
    yield
    reset_logging_settings()
    reset_structlog_config()


@pytest.fixture
def inject_otel_fn():
    """테스트 대상 함수를 반환하는 픽스처."""
    from baldur.observability.structlog_config import _inject_otel_trace_context

    return _inject_otel_trace_context


# =============================================================================
# 계약 검증: 모듈 공개 인터페이스 확인
# =============================================================================


class TestStructlogConfigContract:
    """structlog_config 모듈 공개 인터페이스 설계 계약 검증."""

    def test_configure_structlog_is_callable(self):
        """configure_structlog 함수가 모듈에 공개되어 있어야 한다."""
        from baldur.observability import structlog_config

        assert callable(structlog_config.configure_structlog)

    def test_inject_otel_trace_context_is_callable(self):
        """_inject_otel_trace_context 함수가 모듈에 존재해야 한다."""
        from baldur.observability.structlog_config import (
            _inject_otel_trace_context,
        )

        assert callable(_inject_otel_trace_context)

    def test_inject_otel_trace_context_accepts_three_positional_args(
        self, inject_otel_fn
    ):
        """structlog 프로세서 시그니처: (logger, method_name, event_dict) → event_dict."""
        result = inject_otel_fn(None, "info", {"event": "test"})
        assert isinstance(result, dict)

    def test_shared_processor_count_in_configure_structlog(self, monkeypatch):
        """공유 프로세서가 10개여야 한다.

        순서:
          1. merge_contextvars
          2. add_log_level
          3. add_logger_name
          4. event_name_validator
          5. rate_limit_processor
          6. sampling_processor
          7. TimeStamper
          8. _inject_otel_trace_context
          9. StackInfoRenderer
          10. format_exc_info
        """
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")

        captured: list[Any] = []

        original_configure = structlog.configure

        def capture_configure(**kwargs: Any) -> None:
            # ProcessorFormatter.wrap_for_formatter가 추가되므로 shared = 전체 - 1
            captured.extend(kwargs.get("processors", []))
            original_configure(**kwargs)

        with patch("structlog.configure", side_effect=capture_configure):
            from baldur.observability.structlog_config import configure_structlog

            configure_structlog()

        # wrap_for_formatter를 제외한 공유 프로세서 수 = 10
        shared_count = len(captured) - 1  # 마지막 wrap_for_formatter 제외
        assert shared_count == 10

    def test_event_name_validator_positioned_after_add_logger_name(self, monkeypatch):
        """event_name_validator는 add_logger_name 직후에 위치해야 한다.

        Pipeline position contract: add_logger_name → event_name_validator → rate_limit_processor.
        """
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")

        captured: list[Any] = []
        original_configure = structlog.configure

        def capture_configure(**kwargs: Any) -> None:
            captured.extend(kwargs.get("processors", []))
            original_configure(**kwargs)

        with patch("structlog.configure", side_effect=capture_configure):
            from baldur.observability.structlog_config import configure_structlog

            configure_structlog()

        # shared_processors = captured[:-1] (마지막 wrap_for_formatter 제외)
        shared = captured[:-1]

        from baldur.observability.log_processors import event_name_validator

        # event_name_validator가 shared_processors에 존재해야 한다
        assert event_name_validator in shared

        # add_logger_name 직후에 위치해야 한다
        add_logger_name_idx = shared.index(structlog.stdlib.add_logger_name)
        validator_idx = shared.index(event_name_validator)
        assert validator_idx == add_logger_name_idx + 1


# =============================================================================
# 동작 검증: _inject_otel_trace_context
# =============================================================================


class TestInjectOtelTraceContextBehavior:
    """_inject_otel_trace_context 프로세서 동작 검증."""

    def test_returns_event_dict_unchanged_when_otel_not_available(self, inject_otel_fn):
        """baldur.observability ImportError 시 event_dict를 그대로 반환해야 한다."""
        event_dict = {"event": "some.event", "key": "value"}

        with patch.dict("sys.modules", {"baldur.observability": None}):
            result = inject_otel_fn(None, "info", event_dict)

        assert result == {"event": "some.event", "key": "value"}
        assert "trace_id" not in result
        assert "span_id" not in result

    def test_injects_trace_id_and_span_id_when_otel_active(self, inject_otel_fn):
        """활성 OTEL 스팬이 있을 때 trace_id와 span_id가 event_dict에 주입되어야 한다."""
        event_dict: dict[str, Any] = {"event": "circuit_breaker.state_changed"}

        mock_observability = MagicMock()
        mock_observability.get_current_trace_id_from_otel.return_value = "abc123trace"
        mock_observability.get_current_span_id_from_otel.return_value = "def456span"

        with patch.dict("sys.modules", {"baldur.observability": mock_observability}):
            result = inject_otel_fn(None, "info", event_dict)

        assert result["trace_id"] == "abc123trace"
        assert result["span_id"] == "def456span"

    def test_skips_trace_id_when_none(self, inject_otel_fn):
        """trace_id가 None이면 event_dict에 trace_id 키를 추가하지 않아야 한다."""
        event_dict: dict[str, Any] = {"event": "watchdog.recovery_failed"}

        mock_observability = MagicMock()
        mock_observability.get_current_trace_id_from_otel.return_value = None
        mock_observability.get_current_span_id_from_otel.return_value = "span999"

        with patch.dict("sys.modules", {"baldur.observability": mock_observability}):
            result = inject_otel_fn(None, "error", event_dict)

        assert "trace_id" not in result
        assert result["span_id"] == "span999"

    def test_skips_span_id_when_none(self, inject_otel_fn):
        """span_id가 None이면 event_dict에 span_id 키를 추가하지 않아야 한다."""
        event_dict: dict[str, Any] = {"event": "cell_registry.state_changed"}

        mock_observability = MagicMock()
        mock_observability.get_current_trace_id_from_otel.return_value = "trace_abc"
        mock_observability.get_current_span_id_from_otel.return_value = None

        with patch.dict("sys.modules", {"baldur.observability": mock_observability}):
            result = inject_otel_fn(None, "info", event_dict)

        assert result["trace_id"] == "trace_abc"
        assert "span_id" not in result

    def test_both_none_leaves_event_dict_without_trace_fields(self, inject_otel_fn):
        """trace_id, span_id 모두 None이면 event_dict에 아무 trace 필드도 추가되지 않아야 한다."""
        event_dict: dict[str, Any] = {
            "event": "resilient_storage.degraded_mode_entered"
        }

        mock_observability = MagicMock()
        mock_observability.get_current_trace_id_from_otel.return_value = None
        mock_observability.get_current_span_id_from_otel.return_value = None

        with patch.dict("sys.modules", {"baldur.observability": mock_observability}):
            result = inject_otel_fn(None, "critical", event_dict)

        assert "trace_id" not in result
        assert "span_id" not in result
        assert result["event"] == "resilient_storage.degraded_mode_entered"

    def test_existing_event_dict_fields_are_preserved(self, inject_otel_fn):
        """주입 과정에서 기존 event_dict의 다른 필드는 손상되지 않아야 한다."""
        event_dict: dict[str, Any] = {
            "event": "adaptive_throttle.governance_blocked",
            "component": "adaptive_throttle",
            "cell_id": "cell-ap-1",
        }

        mock_observability = MagicMock()
        mock_observability.get_current_trace_id_from_otel.return_value = "tid"
        mock_observability.get_current_span_id_from_otel.return_value = "sid"

        with patch.dict("sys.modules", {"baldur.observability": mock_observability}):
            result = inject_otel_fn(None, "warning", event_dict)

        assert result["event"] == "adaptive_throttle.governance_blocked"
        assert result["component"] == "adaptive_throttle"
        assert result["cell_id"] == "cell-ap-1"
        assert result["trace_id"] == "tid"
        assert result["span_id"] == "sid"


# =============================================================================
# 동작 검증: configure_structlog — 렌더러 선택
# =============================================================================


class TestConfigureStructlogBehavior:
    """configure_structlog() 렌더러 선택 및 핸들러 등록 동작 검증."""

    def _get_structlog_formatter(self) -> structlog.stdlib.ProcessorFormatter | None:
        """root logger에 등록된 ProcessorFormatter를 반환한다."""
        root = logging.getLogger()
        for handler in root.handlers:
            fmt = getattr(handler, "formatter", None)
            if isinstance(fmt, structlog.stdlib.ProcessorFormatter):
                return fmt
        return None

    def test_json_renderer_selected_when_structured_json_true(self, monkeypatch):
        """structured_json=True이면 root logger에 JSONRenderer가 적용되어야 한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        formatter = self._get_structlog_formatter()
        assert formatter is not None

        # ProcessorFormatter.processors의 마지막 렌더러가 JSONRenderer인지 확인
        renderer = formatter.processors[-1]
        assert isinstance(renderer, structlog.processors.JSONRenderer)

    def test_console_renderer_selected_when_structured_json_false(self, monkeypatch):
        """structured_json=False이면 root logger에 ConsoleRenderer가 적용되어야 한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "false")
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        formatter = self._get_structlog_formatter()
        assert formatter is not None

        renderer = formatter.processors[-1]
        assert isinstance(renderer, structlog.dev.ConsoleRenderer)

    def test_duplicate_calls_do_not_add_multiple_processor_formatters(
        self, monkeypatch
    ):
        """프로덕션 모드에서 configure_structlog()을 여러 번 호출해도 ProcessorFormatter 핸들러가 중복 등록되지 않아야 한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()
        configure_structlog()
        configure_structlog()

        root = logging.getLogger()
        processor_formatter_count = sum(
            1
            for h in root.handlers
            if isinstance(
                getattr(h, "formatter", None), structlog.stdlib.ProcessorFormatter
            )
        )
        assert processor_formatter_count == 1

    def test_null_handler_used_when_test_log_level_set(self, monkeypatch):
        """테스트 환경(BALDUR_TEST_LOG_LEVEL 설정)에서는 NullHandler로 콘솔 출력을 차단해야 한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        monkeypatch.setenv("BALDUR_TEST_LOG_LEVEL", "WARNING")

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        root = logging.getLogger()
        # ProcessorFormatter 핸들러가 없어야 한다 (NullHandler에는 formatter 없음)
        assert self._get_structlog_formatter() is None
        # NullHandler가 추가되어 있어야 한다
        null_handlers = [h for h in root.handlers if isinstance(h, logging.NullHandler)]
        assert len(null_handlers) >= 1

    def test_structlog_wrapper_class_is_bound_logger_after_configure(self, monkeypatch):
        """configure_structlog() 후 structlog 설정의 wrapper_class가 BoundLogger여야 한다.

        cache_logger_on_first_use=True이면 get_logger()는 첫 호출 전까지
        BoundLoggerLazyProxy를 반환하므로, 설정값으로 직접 검증한다.
        """
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        config = structlog.get_config()
        assert config["wrapper_class"] is structlog.stdlib.BoundLogger

    def test_root_logger_level_respects_test_log_level_override(self, monkeypatch):
        """configure_structlog() 후 root logger 레벨이 BALDUR_TEST_LOG_LEVEL 환경변수를 존중해야 한다.

        테스트 환경(BALDUR_TEST_LOG_LEVEL 설정됨): 해당 레벨 적용.
        프로덕션 환경(설정 없음): DEBUG(10)로 설정.
        """
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")

        from baldur.observability.structlog_config import configure_structlog

        # 테스트 환경: BALDUR_TEST_LOG_LEVEL=WARNING이면 WARNING(30)
        monkeypatch.setenv("BALDUR_TEST_LOG_LEVEL", "WARNING")
        configure_structlog()
        root = logging.getLogger()
        assert root.level == logging.WARNING

        # 프로덕션 환경: 환경변수 미설정이면 WARNING(30)
        from baldur.observability.structlog_config import reset_structlog_config

        reset_structlog_config()
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
        configure_structlog()
        assert root.level == logging.WARNING

    @pytest.mark.parametrize(
        ("case", "extra"),
        [
            ("benign", {"key": "v"}),
            ("collision", {"level": "BOGUS"}),
            ("empty", {}),
            ("nonserializable", {"obj": object()}),
        ],
    )
    def test_foreign_stdlib_extra_fields_render_at_json_top_level(
        self, monkeypatch, case, extra
    ):
        """Foreign stdlib ``extra={...}`` fields must survive the ProcessorFormatter render.

        G2 regression guard for the ExtraAdder wiring (D2/D4): without
        ``structlog.stdlib.ExtraAdder`` in the foreign pre-chain a foreign
        record's ``extra=`` attributes are silently dropped at render time.

        Captures *rendered* output through the installed ProcessorFormatter —
        NOT pytest's record-level capture, which holds the raw LogRecord before
        formatting and so would pass even with ExtraAdder removed.

        Cases:
          - benign: a plain extra field is lifted to the JSON top level.
          - collision: an extra key colliding with a canonical structural field
            (``level``) loses to the downstream structural processor — the D2
            prepend ordering guarantee.
          - empty: an empty ``extra={}`` adds no spurious keys and does not crash.
          - nonserializable: a non-serializable value does not crash the render
            (JSONRenderer ``repr()`` fallback — a renderer-level contract, not a
            572 guarantee).
        """
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        formatter = self._get_structlog_formatter()
        assert formatter is not None

        # A foreign record is a plain stdlib LogRecord (no structlog meta).
        # `extra={...}` surfaces as non-standard attributes on the record, so
        # set them directly to simulate the stdlib `logger.warning(event, extra=)` path.
        record = logging.LogRecord(
            name="test.logger",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="test.extra_rendered",
            args=(),
            exc_info=None,
        )
        for attr_name, attr_value in extra.items():
            setattr(record, attr_name, attr_value)

        rendered = json.loads(formatter.format(record))

        # The event name always survives regardless of the extra payload.
        assert rendered["event"] == "test.extra_rendered"

        if case == "benign":
            assert rendered["key"] == "v"
        elif case == "collision":
            # add_log_level (downstream of the prepended ExtraAdder) overwrites
            # the colliding `extra={"level": ...}` — canonical value wins.
            assert rendered["level"] == "warning"
        elif case == "empty":
            assert "key" not in rendered
        elif case == "nonserializable":
            # Render did not crash and the object became a repr-shaped string.
            assert "object object at" in rendered["obj"]


# =============================================================================
# 계약 검증: BALDUR_LOG_LEVEL env var (D6, D7)
# =============================================================================


class TestBaldurLogLevelContract:
    """BALDUR_LOG_LEVEL 환경변수 설계 계약 검증."""

    def test_default_root_log_level_is_warning(self, monkeypatch):
        """D7: 환경변수 미설정 시 root log level은 WARNING."""
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
        monkeypatch.delenv("BALDUR_LOG_LEVEL", raising=False)

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        root = logging.getLogger()
        assert root.level == logging.WARNING


# =============================================================================
# 동작 검증: BALDUR_LOG_LEVEL env var
# =============================================================================


class TestBaldurLogLevelBehavior:
    """BALDUR_LOG_LEVEL 환경변수 동작 검증."""

    def test_baldur_log_level_debug_overrides_default(self, monkeypatch):
        """BALDUR_LOG_LEVEL=DEBUG 설정 시 root logger가 DEBUG로 설정된다."""
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
        monkeypatch.setenv("BALDUR_LOG_LEVEL", "DEBUG")

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_baldur_log_level_info_overrides_default(self, monkeypatch):
        """BALDUR_LOG_LEVEL=INFO 설정 시 root logger가 INFO로 설정된다."""
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
        monkeypatch.setenv("BALDUR_LOG_LEVEL", "INFO")

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_baldur_log_level_invalid_falls_back_to_warning(self, monkeypatch):
        """D7: 잘못된 BALDUR_LOG_LEVEL 값은 WARNING으로 폴백한다."""
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
        monkeypatch.setenv("BALDUR_LOG_LEVEL", "INVALID_LEVEL")

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_baldur_log_level_case_insensitive(self, monkeypatch):
        """BALDUR_LOG_LEVEL 값은 대소문자를 구분하지 않는다."""
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
        monkeypatch.setenv("BALDUR_LOG_LEVEL", "error")

        from baldur.observability.structlog_config import configure_structlog

        configure_structlog()

        root = logging.getLogger()
        assert root.level == logging.ERROR
