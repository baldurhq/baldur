"""Unit tests for the ``structlog_config`` settings module.

Under test:
- configure_structlog(): selects the renderer according to ``structured_json``;
  on the zero-config path (root logger without a handler) installs baldur's
  own root handler and root level with ``logging.basicConfig`` semantics and
  leaves a configured root alone; sets the level of baldur's own loggers from
  ``BALDUR_LOG_LEVEL`` when it is set.
- _HostReadableEventDict: a baldur event that reaches a host handler renders
  as ``event key=value ...`` and never as a dict repr.
- _inject_otel_trace_context(): injects trace_id/span_id into the event_dict
  when an OTEL context is active, and returns the event_dict unchanged when
  there is none or the import fails.
- route_structlog_to_stdlib(): before the settings are readable, structlog
  routes through stdlib with the processors that read nothing, so a line
  raised while ``configure_structlog()`` builds the settings is a stdlib
  record and never structlog's default printer's.

pytest adds its capture handlers to the root logger at the start of the call
phase — after fixture setup — so ``basicConfig`` semantics see every
in-process test as a configured host. The two host shapes are therefore
installed by context managers inside the test body: the zero-config shape
clears the root for the call, the configured shape replaces it with one
sentinel handler, and both put the original list object back so the capture
plugin's own teardown still finds its handlers.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import structlog

from baldur.observability.structlog_config import (
    _COMPONENT_LOGGER_MAP,
    _DEFAULT_LOG_LEVEL_NAME,
    POSTURE_LOGGER_NAME,
    _BaldurStreamHandler,
    _HostReadableEventDict,
    _inject_otel_trace_context,
    _wrap_for_host_and_formatter,
    configure_structlog,
    reset_structlog_config,
    route_structlog_to_stdlib,
)

# The level an unset or unrecognised BALDUR_LOG_LEVEL falls back to.
_DEFAULT_LEVEL = getattr(logging, _DEFAULT_LOG_LEVEL_NAME)

# The six BALDUR_LOG_LEVEL shapes the contract names: unset, three valid
# names, a lower-case name, and an unrecognised value.
_LOG_LEVEL_CASES: list[tuple[str | None, int]] = [
    (None, _DEFAULT_LEVEL),
    ("DEBUG", logging.DEBUG),
    ("INFO", logging.INFO),
    ("WARNING", logging.WARNING),
    ("error", logging.ERROR),
    ("INVALID_LEVEL", _DEFAULT_LEVEL),
]

# =============================================================================
# Shared fixtures and host shapes
# =============================================================================


@pytest.fixture(autouse=True)
def reset_logging_settings():
    """Isolate every case: reset the LoggingSettings singleton and the
    structlog configuration before and after."""
    from baldur.settings.logging_settings import reset_logging_settings

    reset_logging_settings()
    reset_structlog_config()
    yield
    reset_logging_settings()
    reset_structlog_config()


@pytest.fixture
def inject_otel_fn():
    """The processor under test."""
    from baldur.observability.structlog_config import _inject_otel_trace_context

    return _inject_otel_trace_context


@pytest.fixture
def operator_log_level(monkeypatch):
    """Drive ``BALDUR_LOG_LEVEL`` and let the production branch run.

    ``BALDUR_TEST_LOG_LEVEL`` (set session-wide by the root conftest) selects
    the NullHandler branch, so it is cleared for the case.
    """

    def _set(level: str | None) -> None:
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
        if level is None:
            monkeypatch.delenv("BALDUR_LOG_LEVEL", raising=False)
        else:
            monkeypatch.setenv("BALDUR_LOG_LEVEL", level)

    return _set


@contextmanager
def zero_config_root() -> Iterator[logging.Logger]:
    """A root logger with no handler — the zero-config host shape."""
    root = logging.getLogger()
    saved_handlers = root.handlers
    saved_level = root.level
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


@contextmanager
def configured_root() -> Iterator[logging.StreamHandler]:
    """A root logger an application configured: one sentinel handler with a
    plain ``%(message)s`` formatter, at INFO.

    Yields the sentinel; its stream holds whatever reached the host.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers
    saved_level = root.level
    sentinel = logging.StreamHandler(io.StringIO())
    sentinel.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.handlers = [sentinel]
    root.setLevel(logging.INFO)
    try:
        yield sentinel
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def _baldur_handlers() -> list[_BaldurStreamHandler]:
    return [
        h for h in logging.getLogger().handlers if isinstance(h, _BaldurStreamHandler)
    ]


def _processor_formatter_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if isinstance(
            getattr(h, "formatter", None), structlog.stdlib.ProcessorFormatter
        )
    ]


def _baldur_formatter() -> structlog.stdlib.ProcessorFormatter:
    handlers = _baldur_handlers()
    assert len(handlers) == 1, handlers
    formatter = handlers[0].formatter
    assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)
    return formatter


@contextmanager
def recording_structlog_configure() -> Iterator[list[list[Any]]]:
    """Record the processor list of every ``structlog.configure`` call while
    still applying it. ``configure_structlog()`` makes two calls — the
    pre-settings stdlib routing, then the full pipeline — so a pin on the
    final pipeline reads the last entry."""
    calls: list[list[Any]] = []
    original_configure = structlog.configure

    def capture_configure(**kwargs: Any) -> None:
        calls.append(list(kwargs.get("processors", [])))
        original_configure(**kwargs)

    with patch("structlog.configure", side_effect=capture_configure):
        yield calls


def _host_processor_formatter_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[structlog.processors.KeyValueRenderer()]
        )
    )
    return handler


# =============================================================================
# Contract: the module's public surface
# =============================================================================


class TestStructlogConfigContract:
    """Design contract of the structlog_config module's interface."""

    def test_configure_structlog_is_callable(self):
        from baldur.observability import structlog_config

        assert callable(structlog_config.configure_structlog)

    def test_inject_otel_trace_context_is_callable(self):
        from baldur.observability.structlog_config import (
            _inject_otel_trace_context,
        )

        assert callable(_inject_otel_trace_context)

    def test_inject_otel_trace_context_accepts_three_positional_args(
        self, inject_otel_fn
    ):
        """structlog processor signature: (logger, method_name, event_dict) -> event_dict."""
        result = inject_otel_fn(None, "info", {"event": "test"})
        assert isinstance(result, dict)

    def test_shared_processor_count_in_configure_structlog(self, monkeypatch):
        """Ten shared processors, in this order:

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

        followed by the host-readable wrapper as the last processor.
        """
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")

        with recording_structlog_configure() as calls:
            configure_structlog()

        shared_count = len(calls[-1]) - 1
        assert shared_count == 10

    def test_last_processor_is_the_host_readable_wrapper(self, monkeypatch):
        """The event dict reaches stdlib through ``_wrap_for_host_and_formatter``,
        never through structlog's own ``wrap_for_formatter`` (a plain dict)."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")

        with recording_structlog_configure() as calls:
            configure_structlog()

        assert calls[-1][-1] is _wrap_for_host_and_formatter

    def test_event_name_validator_positioned_after_add_logger_name(self, monkeypatch):
        """Pipeline position contract: add_logger_name -> event_name_validator
        -> rate_limit_processor."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")

        with recording_structlog_configure() as calls:
            configure_structlog()

        shared = calls[-1][:-1]

        from baldur.observability.log_processors import event_name_validator

        assert event_name_validator in shared

        add_logger_name_idx = shared.index(structlog.stdlib.add_logger_name)
        validator_idx = shared.index(event_name_validator)
        assert validator_idx == add_logger_name_idx + 1

    def test_the_pre_settings_pipeline_carries_no_settings_reader(self):
        """The routing installed before the settings are readable: stdlib
        factory, no caching, the host-readable wrapper last, and none of
        the processors that read settings — the validator, rate limiter
        and sampler read ``LoggingSettings``; the OTEL injector's first
        call initialises OTEL from the observability settings."""
        from baldur.observability.log_processors import (
            event_name_validator,
            rate_limit_processor,
            sampling_processor,
        )

        route_structlog_to_stdlib()
        config = structlog.get_config()

        assert isinstance(config["logger_factory"], structlog.stdlib.LoggerFactory)
        assert config["wrapper_class"] is structlog.stdlib.BoundLogger
        assert config["cache_logger_on_first_use"] is False
        processors = config["processors"]
        assert processors[-1] is _wrap_for_host_and_formatter
        assert structlog.stdlib.add_log_level in processors
        for reader in (
            event_name_validator,
            rate_limit_processor,
            sampling_processor,
            _inject_otel_trace_context,
        ):
            assert reader not in processors


# =============================================================================
# Behavior: the pre-settings window routes through stdlib
# =============================================================================


class TestRouteStructlogToStdlibBehavior:
    """A line emitted before the settings are readable is a stdlib record —
    dropped by the root level or written by the host's handler — never a
    line from structlog's default printer."""

    @contextmanager
    def _default_printer(self) -> Iterator[io.StringIO]:
        """structlog's pre-configuration shape, pointed at a sink: a
        PrintLogger that writes every level, no filtering."""
        sink = io.StringIO()
        structlog.configure(
            processors=[structlog.dev.ConsoleRenderer(colors=False)],
            logger_factory=structlog.PrintLoggerFactory(file=sink),
            wrapper_class=structlog.BoundLogger,
            cache_logger_on_first_use=False,
        )
        yield sink

    def test_an_emission_before_configuration_reaches_the_hosts_handler(
        self, operator_log_level
    ):
        operator_log_level(None)

        with self._default_printer() as printer, configured_root() as sentinel:
            route_structlog_to_stdlib()
            structlog.get_logger("baldur.probe").warning(
                "probe.early_event_emitted", key="v"
            )
            host_output = sentinel.stream.getvalue()

        assert "WARNING baldur.probe probe.early_event_emitted key='v'" in host_output
        assert "probe.early_event_emitted" not in printer.getvalue()

    def test_configure_structlog_routes_before_it_reads_the_settings(
        self, operator_log_level
    ):
        """A settings cross-validation warning is raised inside the settings
        constructor, which ``configure_structlog()`` runs before the full
        pipeline exists; it must land on the host's handler, not on the
        default printer."""
        from baldur.settings import logging_settings as settings_module

        operator_log_level(None)
        real_read = settings_module.get_logging_settings

        def emitting_read() -> Any:
            structlog.get_logger("baldur.probe").warning(
                "probe.settings_window_emitted", key="v"
            )
            return real_read()

        with (
            self._default_printer() as printer,
            configured_root() as sentinel,
            patch.object(settings_module, "get_logging_settings", emitting_read),
        ):
            configure_structlog()
            host_output = sentinel.stream.getvalue()

        assert (
            "WARNING baldur.probe probe.settings_window_emitted key='v'" in host_output
        )
        assert "probe.settings_window_emitted" not in printer.getvalue()

    def test_is_a_no_op_once_configured(self, operator_log_level):
        """A caller that routes after the full pipeline is in place (the CLI
        callback in a process something else initialised) changes nothing."""
        operator_log_level(None)
        with zero_config_root():
            configure_structlog()
        full_pipeline = structlog.get_config()["processors"]

        route_structlog_to_stdlib()

        assert structlog.get_config()["processors"] is full_pipeline
        assert structlog.get_config()["cache_logger_on_first_use"] is True


# =============================================================================
# Behavior: _inject_otel_trace_context
# =============================================================================


class TestInjectOtelTraceContextBehavior:
    """Behavior of the _inject_otel_trace_context processor."""

    def test_returns_event_dict_unchanged_when_otel_not_available(self, inject_otel_fn):
        """An ImportError on baldur.observability returns the event_dict as is."""
        event_dict = {"event": "some.event", "key": "value"}

        with patch.dict("sys.modules", {"baldur.observability": None}):
            result = inject_otel_fn(None, "info", event_dict)

        assert result == {"event": "some.event", "key": "value"}
        assert "trace_id" not in result
        assert "span_id" not in result

    def test_injects_trace_id_and_span_id_when_otel_active(self, inject_otel_fn):
        """With an active OTEL span, trace_id and span_id are injected."""
        event_dict: dict[str, Any] = {"event": "circuit_breaker.state_changed"}

        mock_observability = MagicMock()
        mock_observability.get_current_trace_id_from_otel.return_value = "abc123trace"
        mock_observability.get_current_span_id_from_otel.return_value = "def456span"

        with patch.dict("sys.modules", {"baldur.observability": mock_observability}):
            result = inject_otel_fn(None, "info", event_dict)

        assert result["trace_id"] == "abc123trace"
        assert result["span_id"] == "def456span"

    def test_skips_trace_id_when_none(self, inject_otel_fn):
        """A None trace_id adds no trace_id key."""
        event_dict: dict[str, Any] = {"event": "watchdog.recovery_failed"}

        mock_observability = MagicMock()
        mock_observability.get_current_trace_id_from_otel.return_value = None
        mock_observability.get_current_span_id_from_otel.return_value = "span999"

        with patch.dict("sys.modules", {"baldur.observability": mock_observability}):
            result = inject_otel_fn(None, "error", event_dict)

        assert "trace_id" not in result
        assert result["span_id"] == "span999"

    def test_skips_span_id_when_none(self, inject_otel_fn):
        """A None span_id adds no span_id key."""
        event_dict: dict[str, Any] = {"event": "cell_registry.state_changed"}

        mock_observability = MagicMock()
        mock_observability.get_current_trace_id_from_otel.return_value = "trace_abc"
        mock_observability.get_current_span_id_from_otel.return_value = None

        with patch.dict("sys.modules", {"baldur.observability": mock_observability}):
            result = inject_otel_fn(None, "info", event_dict)

        assert result["trace_id"] == "trace_abc"
        assert "span_id" not in result

    def test_both_none_leaves_event_dict_without_trace_fields(self, inject_otel_fn):
        """With neither id available no trace field is added."""
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
        """Injection leaves the other event_dict fields intact."""
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
# Behavior: configure_structlog — renderer selection and baldur's own handler
# =============================================================================


class TestConfigureStructlogBehavior:
    """Renderer selection and the handler baldur installs on the zero-config
    path."""

    def test_json_renderer_selected_when_structured_json_true(
        self, monkeypatch, operator_log_level
    ):
        """structured_json=True: baldur's handler renders JSON."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        operator_log_level(None)

        with zero_config_root():
            configure_structlog()

            renderer = _baldur_formatter().processors[-1]
            assert isinstance(renderer, structlog.processors.JSONRenderer)

    def test_console_renderer_selected_when_structured_json_false(
        self, monkeypatch, operator_log_level
    ):
        """structured_json=False: baldur's handler renders console lines."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "false")
        operator_log_level(None)

        with zero_config_root():
            configure_structlog()

            renderer = _baldur_formatter().processors[-1]
            assert isinstance(renderer, structlog.dev.ConsoleRenderer)

    def test_repeat_calls_install_exactly_one_handler(
        self, monkeypatch, operator_log_level
    ):
        """Repeat calls are no-ops: one baldur handler, never two."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        operator_log_level(None)

        with zero_config_root() as root:
            configure_structlog()
            configure_structlog()
            configure_structlog()

            assert len(_baldur_handlers()) == 1
            assert len(root.handlers) == 1

    def test_null_handler_used_when_test_log_level_set(self, monkeypatch):
        """Under BALDUR_TEST_LOG_LEVEL a NullHandler blocks console output."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        monkeypatch.setenv("BALDUR_TEST_LOG_LEVEL", "WARNING")

        configure_structlog()

        root = logging.getLogger()
        assert _baldur_handlers() == []
        null_handlers = [h for h in root.handlers if isinstance(h, logging.NullHandler)]
        assert len(null_handlers) >= 1

    def test_structlog_wrapper_class_is_bound_logger_after_configure(self, monkeypatch):
        """After configure_structlog() the wrapper_class is BoundLogger.

        With cache_logger_on_first_use=True, get_logger() returns a
        BoundLoggerLazyProxy until first use, so the configuration is read
        directly.
        """
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")

        configure_structlog()

        config = structlog.get_config()
        assert config["wrapper_class"] is structlog.stdlib.BoundLogger

    def test_root_logger_level_respects_test_log_level_override(self, monkeypatch):
        """Under BALDUR_TEST_LOG_LEVEL the root level is that variable's."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        monkeypatch.setenv("BALDUR_TEST_LOG_LEVEL", "WARNING")
        root = logging.getLogger()
        saved_level = root.level

        try:
            configure_structlog()
            assert root.level == logging.WARNING
        finally:
            root.setLevel(saved_level)

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
        self, monkeypatch, operator_log_level, case, extra
    ):
        """Foreign stdlib ``extra={...}`` fields must survive the ProcessorFormatter render.

        Regression guard for the ExtraAdder wiring: without
        ``structlog.stdlib.ExtraAdder`` in the foreign pre-chain a foreign
        record's ``extra=`` attributes are silently dropped at render time.

        Captures *rendered* output through the installed ProcessorFormatter —
        NOT pytest's record-level capture, which holds the raw LogRecord before
        formatting and so would pass even with ExtraAdder removed.

        Cases:
          - benign: a plain extra field is lifted to the JSON top level.
          - collision: an extra key colliding with a canonical structural field
            (``level``) loses to the downstream structural processor — the
            prepend ordering guarantee.
          - empty: an empty ``extra={}`` adds no spurious keys and does not crash.
          - nonserializable: a non-serializable value does not crash the render
            (JSONRenderer ``repr()`` fallback — a renderer-level contract).
        """
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        operator_log_level(None)

        with zero_config_root():
            configure_structlog()
            formatter = _baldur_formatter()

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

    def test_the_foreign_chain_carries_no_stateful_processor(
        self, monkeypatch, operator_log_level
    ):
        """A host record through baldur's handler is written once: the
        processors that can drop or reject an event (the rate limiter, the
        sampler, the event-name validator) are not in the foreign chain."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        operator_log_level(None)

        with zero_config_root():
            configure_structlog()
            foreign_pre_chain = list(_baldur_formatter().foreign_pre_chain or [])

        from baldur.observability.log_processors import (
            event_name_validator,
            rate_limit_processor,
            sampling_processor,
        )

        assert rate_limit_processor not in foreign_pre_chain
        assert sampling_processor not in foreign_pre_chain
        assert event_name_validator not in foreign_pre_chain
        assert structlog.stdlib.add_log_level in foreign_pre_chain
        assert isinstance(foreign_pre_chain[0], structlog.stdlib.ExtraAdder)


# =============================================================================
# Behavior: the zero-config root — basicConfig semantics
# =============================================================================


class TestZeroConfigRootBehavior:
    """A root logger with no handler gets baldur's stdout handler and the
    BALDUR_LOG_LEVEL root level, as today."""

    @pytest.mark.parametrize(("level_name", "expected"), _LOG_LEVEL_CASES)
    def test_root_level_follows_baldur_log_level(
        self, operator_log_level, level_name, expected
    ):
        operator_log_level(level_name)

        with zero_config_root() as root:
            configure_structlog()

            assert root.level == expected

    @pytest.mark.parametrize(("level_name", "expected"), _LOG_LEVEL_CASES)
    def test_exactly_one_baldur_stream_handler_is_installed(
        self, operator_log_level, level_name, expected
    ):
        operator_log_level(level_name)

        with zero_config_root() as root:
            configure_structlog()

            assert [type(h) for h in root.handlers] == [_BaldurStreamHandler]

    @pytest.mark.parametrize(("level_name", "expected"), _LOG_LEVEL_CASES)
    def test_baldur_loggers_take_the_level_only_when_it_is_set(
        self, operator_log_level, level_name, expected
    ):
        """Set: ``baldur`` and ``baldur_pro`` carry the level. Unset: they stay
        NOTSET and inherit the root, as any library's loggers do."""
        operator_log_level(level_name)

        with zero_config_root():
            configure_structlog()

        expected_namespace = logging.NOTSET if level_name is None else expected
        assert logging.getLogger("baldur").level == expected_namespace
        assert logging.getLogger("baldur_pro").level == expected_namespace

    def test_the_component_families_keep_precedence(self, operator_log_level):
        """BALDUR_LOG_LEVEL=ERROR does not silence a family whose documented
        level is below it: the family write runs after the namespace write."""
        from baldur.settings.logging_settings import get_logging_settings

        operator_log_level("ERROR")
        family_name = _COMPONENT_LOGGER_MAP["circuit_breaker_log_level"][0]
        family_level = getattr(
            logging, get_logging_settings().circuit_breaker_log_level.upper()
        )
        assert family_level < logging.ERROR, "the case needs a family below ERROR"

        with zero_config_root():
            configure_structlog()

        assert logging.getLogger("baldur").level == logging.ERROR
        family = logging.getLogger(family_name)
        assert family.getEffectiveLevel() == family_level
        assert family.isEnabledFor(family_level) is True


# =============================================================================
# Behavior: a configured root is left alone
# =============================================================================


class TestConfiguredRootBehavior:
    """An application that configured its root logger keeps its level,
    handlers and format; baldur's events reach them by propagation."""

    @pytest.mark.parametrize(("level_name", "expected"), _LOG_LEVEL_CASES)
    def test_root_level_and_handler_list_are_unchanged(
        self, operator_log_level, level_name, expected
    ):
        operator_log_level(level_name)
        root = logging.getLogger()

        with configured_root() as sentinel:
            configure_structlog()

            assert root.handlers == [sentinel]
            assert root.level == logging.INFO

    @pytest.mark.parametrize(("level_name", "expected"), _LOG_LEVEL_CASES)
    def test_no_baldur_handler_and_no_processor_formatter_on_root(
        self, operator_log_level, level_name, expected
    ):
        operator_log_level(level_name)

        with configured_root():
            configure_structlog()

            assert _baldur_handlers() == []
            assert _processor_formatter_handlers() == []

    @pytest.mark.parametrize(("level_name", "expected"), _LOG_LEVEL_CASES)
    def test_baldur_loggers_take_the_level_only_when_it_is_set(
        self, operator_log_level, level_name, expected
    ):
        """The documented diagnostic switch keeps working inside a configured
        host; unset, baldur's loggers inherit the host's level."""
        operator_log_level(level_name)

        with configured_root():
            configure_structlog()

        expected_namespace = logging.NOTSET if level_name is None else expected
        assert logging.getLogger("baldur").level == expected_namespace
        assert logging.getLogger("baldur_pro").level == expected_namespace

    def test_a_host_processor_formatter_handler_is_kept(self, operator_log_level):
        """A host's own ProcessorFormatter handler is the host's, not baldur's:
        it is neither removed nor joined by a second one."""
        operator_log_level(None)
        root = logging.getLogger()
        host_handler = _host_processor_formatter_handler()

        with zero_config_root():
            root.addHandler(host_handler)
            configure_structlog()

            assert root.handlers == [host_handler]

    def test_a_baldur_event_reaches_the_host_handler_in_its_format(
        self, operator_log_level
    ):
        """Propagation, not a second handler: the host's ``%(message)s``
        formatter prints the event readably."""
        operator_log_level(None)

        with configured_root() as sentinel:
            configure_structlog()
            structlog.get_logger("baldur.probe").warning(
                "probe.host_event_emitted", key="v", n=1
            )
            output = sentinel.stream.getvalue()

        assert "WARNING baldur.probe probe.host_event_emitted key='v' n=1" in output
        assert "{'event'" not in output


# =============================================================================
# Behavior: the host-readable event dict
# =============================================================================


class TestHostReadableEventDictBehavior:
    """``str(record.msg)`` — what ``LogRecord.getMessage`` hands a host
    formatter — is ``event key=value ...``, never a dict repr."""

    def test_renders_event_then_key_value_pairs(self):
        rendered = str(
            _HostReadableEventDict(
                {
                    "event": "sql.default_factory_no_pool",
                    "dsn": "sqlite:///x",
                    "pool": "none",
                }
            )
        )

        assert rendered == "sql.default_factory_no_pool dsn='sqlite:///x' pool='none'"

    def test_drops_the_fields_the_host_formatter_prints_itself(self):
        rendered = str(
            _HostReadableEventDict(
                {
                    "event": "probe.host_event_emitted",
                    "level": "warning",
                    "logger": "baldur.probe",
                    "timestamp": "2026-09-17T00:00:00Z",
                    "key": "v",
                }
            )
        )

        assert rendered == "probe.host_event_emitted key='v'"

    def test_an_event_without_fields_renders_as_the_event_alone(self):
        assert str(_HostReadableEventDict({"event": "baldur.runtime_posture"})) == (
            "baldur.runtime_posture"
        )

    def test_a_rendered_exception_is_appended_on_a_new_line(self):
        rendered = str(
            _HostReadableEventDict(
                {
                    "event": "sql.rollback_failed",
                    "exception": "Traceback ...\nValueError",
                }
            )
        )

        assert rendered == "sql.rollback_failed\nTraceback ...\nValueError"

    def test_the_exception_is_left_to_the_record_when_it_carries_exc_info(self):
        """``.exception()`` proxies to ``Logger.exception``, which attaches
        ``exc_info``; the host formatter prints that traceback itself."""
        rendered = str(
            _HostReadableEventDict(
                {"event": "sql.rollback_failed", "exception": "Traceback ..."},
                traceback_on_record=True,
            )
        )

        assert rendered == "sql.rollback_failed"

    def test_a_rendered_stack_is_appended_on_a_new_line(self):
        rendered = str(
            _HostReadableEventDict(
                {"event": "probe.stack_captured", "stack": "Stack ..."}
            )
        )

        assert rendered == "probe.stack_captured\nStack ..."

    def test_copy_is_a_plain_dict_for_the_processor_formatter(self):
        """ProcessorFormatter copies ``record.msg``; the copy is a plain dict,
        so baldur's own JSON line is byte-identical to today's."""
        copied = _HostReadableEventDict({"event": "a.b_c", "k": 1}).copy()

        assert type(copied) is dict
        assert copied == {"event": "a.b_c", "k": 1}

    @pytest.mark.parametrize(
        ("method_name", "traceback_on_record"),
        [("exception", True), ("error", False), ("warning", False)],
        ids=["exception", "error", "warning"],
    )
    def test_the_wrapper_marks_only_the_exception_method(
        self, method_name, traceback_on_record
    ):
        """Only ``.exception()`` — the method stdlib attaches ``exc_info``
        for — leaves the rendered traceback to the record."""
        probe_logger = logging.getLogger("x")

        args, kwargs = _wrap_for_host_and_formatter(
            probe_logger, method_name, {"event": "a.b_c"}
        )

        assert kwargs == {"extra": {"_logger": probe_logger, "_name": method_name}}
        assert isinstance(args[0], _HostReadableEventDict)
        assert args[0]._traceback_on_record is traceback_on_record

    @pytest.mark.parametrize(
        ("method_name", "kwargs", "expected_line"),
        [
            ("exception", {}, "ERROR baldur.probe probe.call_failed key='v'"),
            (
                "warning",
                {"exc_info": True},
                "WARNING baldur.probe probe.call_failed key='v'",
            ),
        ],
        ids=["exception_method", "exc_info_kwarg"],
    )
    def test_a_logged_exception_prints_its_traceback_once_through_a_host_handler(
        self, operator_log_level, method_name, kwargs, expected_line
    ):
        """Through a plain ``logging.Formatter``, each shape prints its
        traceback exactly once: ``.exception()`` leaves it to the record,
        ``exc_info=True`` (consumed by structlog) has it appended."""
        operator_log_level(None)

        with configured_root() as sentinel:
            configure_structlog()
            probe = structlog.get_logger("baldur.probe")
            try:
                raise ValueError("boom")
            except ValueError:
                getattr(probe, method_name)("probe.call_failed", key="v", **kwargs)
            output = sentinel.stream.getvalue()

        assert expected_line in output
        assert output.count("Traceback (most recent call last)") == 1
        assert output.count("ValueError: boom") == 1

    def test_baldur_json_line_is_unchanged_by_the_readable_msg(
        self, monkeypatch, operator_log_level
    ):
        """baldur's own handler still renders the Mapping as JSON."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_STRUCTURED_JSON", "true")
        operator_log_level(None)

        with zero_config_root():
            configure_structlog()
            formatter = _baldur_formatter()

        record = logging.LogRecord(
            name="baldur.probe",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=_HostReadableEventDict(
                {"event": "probe.host_event_emitted", "level": "warning", "key": "v"}
            ),
            args=(),
            exc_info=None,
        )
        record._logger = logging.getLogger("baldur.probe")
        record._name = "warning"

        rendered = json.loads(formatter.format(record))

        assert rendered["event"] == "probe.host_event_emitted"
        assert rendered["key"] == "v"


# =============================================================================
# Behavior: reset_structlog_config
# =============================================================================


class TestResetStructlogConfigBehavior:
    """The reset removes baldur's handler only, and restores the levels
    configure_structlog() wrote."""

    def test_removes_baldur_handler_and_keeps_the_hosts(self, operator_log_level):
        operator_log_level(None)
        host_handler = _host_processor_formatter_handler()

        with zero_config_root() as root:
            configure_structlog()
            root.addHandler(host_handler)
            assert len(_baldur_handlers()) == 1

            reset_structlog_config()

            assert root.handlers == [host_handler]

    def test_restores_baldur_loggers_and_the_posture_floor_to_notset(
        self, operator_log_level
    ):
        operator_log_level("DEBUG")
        with configured_root():
            configure_structlog()
        assert logging.getLogger("baldur").level == logging.DEBUG

        reset_structlog_config()

        assert logging.getLogger("baldur").level == logging.NOTSET
        assert logging.getLogger("baldur_pro").level == logging.NOTSET
        assert logging.getLogger(POSTURE_LOGGER_NAME).level == logging.NOTSET
