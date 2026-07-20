"""
structlog global configuration — stdlib logging compatibility mode.

Configures structlog as a wrapper around stdlib logging so the existing
infrastructure is preserved as-is:
- OTEL LoggingInstrumentor: intercepts the stdlib LogRecord beneath structlog
  and ships it to Loki
- IncidentLogHandler: a stdlib logging.Handler subclass, so it works unchanged
- LoggingSettings: stdlib logger level configuration preserved as-is
- Django/Celery internal logging: passes through the structlog pipeline via
  foreign_pre_chain

Renderer per environment:
- structured_json=True  (production):  JSONRenderer  -> Loki/Datadog parse the
  JSON automatically
- structured_json=False (development): ConsoleRenderer -> terminal readability

Shared processor pipeline order:
  1. merge_contextvars  — merges values bound to contextvars automatically
  2. add_log_level      — injects the level field automatically
  3. add_logger_name    — injects the logger field automatically (from
     __name__)
  4. _rate_limit_processor — de-dups a repeating event (10s / 100 events)
  5. _sampling_processor   — probabilistically samples hot path logs
  6. TimeStamper(iso)   — injects the timestamp in ISO-8601 format
  7. _inject_otel_trace_context — injects trace_id and span_id automatically
     (when OTEL is active)
  8. StackInfoRenderer  — renders stack information
  9. format_exc_info    — renders exception information
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, cast

import structlog

from baldur.observability.log_processors import (
    event_name_validator,
    rate_limit_processor,
    sampling_processor,
)

# Thread-local flag preventing re-entry into the OTEL trace context injection
# processor. Stops a log emitted inside an observability initialization
# function from calling the processor again and recursing infinitely.
_otel_injection_in_progress = threading.local()

# =============================================================================
# LoggingSettings -> stdlib logger level mapping.
# Applies the 8 per-component log levels from LoggingSettings to the actual
# stdlib loggers. structlog.get_logger() uses the stdlib LoggerFactory
# internally, so the module path (__name__) becomes the logger name.
# =============================================================================
_COMPONENT_LOGGER_MAP: dict[str, list[str]] = {
    "dlq_log_level": [
        "baldur_pro.services.dlq",
        "baldur_pro.services.dlq.base",
        "baldur_pro.services.dlq.models",
    ],
    "circuit_breaker_log_level": [
        "baldur.services.circuit_breaker",
        "baldur.services.circuit_breaker.service",
    ],
    "replay_log_level": [
        "baldur.services.replay_service",
        "baldur.services.adaptive_replay",
        "baldur_pro.services.dlq.replay_operations",
    ],
    "sla_log_level": [
        "baldur_pro.services.throttle.sla_notification",
    ],
    "forensic_log_level": [
        "baldur.audit.forensic_recorder",
    ],
    "emergency_log_level": [
        "baldur_pro.services.emergency_mode",
        "baldur.services.namespace_emergency",
    ],
    "chaos_log_level": [
        "baldur_pro.services.chaos",
    ],
    "l2_storage_log_level": [
        "baldur.adapters.memory.layered_repository",
        "baldur.services.precomputed_cache.l2_cache",
    ],
}


_configure_lock = threading.Lock()


class _StructlogState:
    """Runtime-scoped structlog configuration guard (450 Phase 4)."""

    __slots__ = ("configured",)

    def __init__(self) -> None:
        self.configured: bool = False


def _structlog_state() -> _StructlogState:
    from baldur.runtime import get_runtime

    state: _StructlogState = get_runtime().get_singleton(
        "structlog_state", _StructlogState
    )
    return state


def configure_structlog() -> None:
    """Initialize the global structlog configuration.

    Idempotent — a duplicate call returns immediately.
    Selects the renderer according to the `structured_json` setting.

    Thread-safe: double-checked locking makes concurrent calls safe.
    """
    state = _structlog_state()
    if state.configured:
        return
    with _configure_lock:
        if state.configured:
            return
        from baldur.settings.logging_settings import get_logging_settings

        settings = get_logging_settings()

        renderer: structlog.types.Processor
        if settings.structured_json:
            renderer = structlog.processors.JSONRenderer()
        else:
            renderer = structlog.dev.ConsoleRenderer()

        # structlog declares Processor as MutableMapping in / Mapping|str|bytes
        # out; our processors are typed dict[str, Any] in/out, which is a
        # narrower-input/narrower-output pair. cast() at assembly is the
        # standard structlog idiom — runtime semantics unchanged.
        shared_processors: list[structlog.types.Processor] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            cast(structlog.types.Processor, event_name_validator),
            cast(structlog.types.Processor, rate_limit_processor),
            cast(structlog.types.Processor, sampling_processor),
            structlog.processors.TimeStamper(fmt="iso"),
            cast(structlog.types.Processor, _inject_otel_trace_context),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
        ]

        structlog.configure(
            processors=[
                *shared_processors,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        # Apply the structlog ProcessorFormatter to the stdlib logging handler.
        # Prepend ExtraAdder() to the foreign_pre_chain so stdlib logging's
        # extra={...} fields are lifted into the event_dict. ExtraAdder only
        # acts on foreign records (those carrying event_dict["_record"]), so the
        # native chain is left untouched and it is added only here. Placing it
        # ahead of the structural processors (add_log_level, etc.) ensures that
        # when an extra= key collides with a canonical field
        # (level/logger/timestamp), the downstream structural processor
        # overwrites it so the canonical value always wins.
        foreign_pre_chain: list[structlog.types.Processor] = [
            structlog.stdlib.ExtraAdder(),
            *shared_processors,
        ]
        formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            foreign_pre_chain=foreign_pre_chain,
        )

        root_logger = logging.getLogger()
        # Avoid duplicate handlers: replace only handlers carrying the
        # structlog formatter
        root_logger.handlers = [
            h
            for h in root_logger.handlers
            if not isinstance(
                getattr(h, "formatter", None), structlog.stdlib.ProcessorFormatter
            )
        ]

        # In the test environment, NullHandler blocks console output entirely.
        # StreamHandler(sys.stdout) grabs the original stdout reference at
        # pytest_configure time and so bypasses pytest capture — NullHandler is
        # the only workable answer under test. pytest's caplog uses its own
        # LogCaptureHandler, so it is unaffected.
        _test_level_name = os.environ.get("BALDUR_TEST_LOG_LEVEL")
        handler: logging.Handler
        if _test_level_name:
            handler = logging.NullHandler()
            _effective_level = getattr(
                logging, _test_level_name.upper(), logging.WARNING
            )
            root_logger.setLevel(_effective_level)
        else:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(formatter)
            _log_level_name = os.environ.get("BALDUR_LOG_LEVEL", "WARNING").upper()
            _log_level = getattr(logging, _log_level_name, None)
            if _log_level is None:
                _log_level = logging.WARNING
            root_logger.setLevel(_log_level)
        root_logger.addHandler(handler)

        # =====================================================================
        # Apply the per-component log levels (see _apply_component_log_levels).
        # Applies the 8 level values from LoggingSettings to the actual stdlib
        # loggers via setLevel(), making them controllable by environment
        # variable alone:
        #   BALDUR_LOGGING_SETTINGS_CIRCUIT_BREAKER_LOG_LEVEL=WARNING
        # =====================================================================
        _apply_component_log_levels(settings)
        state.configured = True


def reset_structlog_config() -> None:
    """Reset the structlog configuration in tests.

    Clears the configured flag and also removes the ProcessorFormatter handlers
    registered on the root logger, so the next configure_structlog() call
    rebuilds everything from the new configuration values.
    """
    _structlog_state().configured = False

    root = logging.getLogger()
    root.handlers = [
        h
        for h in root.handlers
        if not isinstance(
            getattr(h, "formatter", None), structlog.stdlib.ProcessorFormatter
        )
    ]


def _apply_component_log_levels(settings: Any) -> None:
    """Apply the per-component log levels from LoggingSettings to stdlib
    loggers.

    Following the mapping defined in _COMPONENT_LOGGER_MAP, applies each
    component's environment variable value via
    logging.getLogger(name).setLevel().

    Without this function the level values defined on LoggingSettings would
    never take effect — dead configuration.
    """
    for setting_name, logger_names in _COMPONENT_LOGGER_MAP.items():
        level_str = getattr(settings, setting_name, "INFO")
        level = getattr(logging, level_str.upper(), logging.INFO)
        for logger_name in logger_names:
            logging.getLogger(logger_name).setLevel(level)


def _inject_otel_trace_context(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Processor injecting the active OTEL span's trace_id and span_id into
    the event_dict.

    Returns the event_dict unchanged when OTEL is not installed or there is no
    active span.

    Re-entry guard: a thread-local flag blocks a log emitted inside OTEL
    initialization from calling this processor again and recursing infinitely.
    """
    if getattr(_otel_injection_in_progress, "active", False):
        return event_dict

    _otel_injection_in_progress.active = True
    try:
        from baldur.observability import (
            get_current_span_id_from_otel,
            get_current_trace_id_from_otel,
        )

        trace_id = get_current_trace_id_from_otel()
        span_id = get_current_span_id_from_otel()

        if trace_id:
            event_dict["trace_id"] = trace_id
        if span_id:
            event_dict["span_id"] = span_id
    except ImportError:
        pass
    finally:
        _otel_injection_in_progress.active = False

    return event_dict
