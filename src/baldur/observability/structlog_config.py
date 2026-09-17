"""
structlog global configuration — stdlib logging compatibility mode.

Configures structlog as a wrapper around stdlib logging so the existing
infrastructure is preserved as-is:
- OTEL LoggingInstrumentor: intercepts the stdlib LogRecord beneath structlog
  and ships it to Loki
- LoggingSettings: stdlib logger level configuration preserved as-is
- Host records on the zero-config path: a stdlib record that reaches baldur's
  own root handler is rendered through foreign_pre_chain

Library/host boundary: baldur writes to the process root logger only with
``logging.basicConfig`` semantics — the handler and the root level are
installed when the root has no handler at configure time, and an application
that configured its own logging (``basicConfig``, a ``dictConfig`` / framework
``LOGGING`` with a ``root`` entry) keeps it. ``BALDUR_LOG_LEVEL`` governs
baldur's own loggers (``baldur``, ``baldur_pro``) when set; when unset they
inherit the application's level. A baldur event that reaches a host handler
renders as ``event key=value ...`` (see ``_HostReadableEventDict``). Nothing
baldur emits goes through structlog's unconfigured default printer: the
structural half of the pipeline is routed to stdlib before the settings are
read (``route_structlog_to_stdlib``), so a line raised while the settings
object is being built becomes a stdlib record instead of an unfiltered
stdout line.

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
from structlog.processors import KeyValueRenderer

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

# The startup posture announcement's own logger name. It needs one because
# the root level defaults to WARNING and _COMPONENT_LOGGER_MAP carries no
# bootstrap namespace, so a one-line INFO summary of what this process is
# actually running on would never reach a handler — the same reason
# ``baldur.startup_report`` is invisible on a default run today.
POSTURE_LOGGER_NAME = "baldur.posture"
_POSTURE_FLOOR_LEVEL = logging.INFO

# The level BALDUR_LOG_LEVEL falls back to when unset or unrecognised.
_DEFAULT_LOG_LEVEL_NAME = "WARNING"

# The loggers BALDUR_LOG_LEVEL governs when it is set. Everything baldur emits
# lives under one of these; the eight component families in
# _COMPONENT_LOGGER_MAP are pinned afterwards and keep precedence.
_BALDUR_LOGGER_NAMESPACES: tuple[str, ...] = ("baldur", "baldur_pro")

# Fields a host formatter prints from the LogRecord itself, so the readable
# rendering of a baldur event leaves them out rather than printing them twice.
_HOST_FORMATTER_OWNED_FIELDS = frozenset({"level", "logger", "timestamp"})

_HOST_KEY_VALUE_RENDERER = KeyValueRenderer()


class _BaldurStreamHandler(logging.StreamHandler):
    """The root handler baldur installs on the zero-config path.

    A marker subclass with no behaviour of its own: ``reset_structlog_config``
    and tests identify baldur's handler by ``isinstance`` rather than by the
    formatter it carries, so a host's own ``ProcessorFormatter`` handler is
    never mistaken for it.
    """


class _HostReadableEventDict(dict):
    """The event dict handed to stdlib as ``LogRecord.msg``.

    baldur's own ``ProcessorFormatter`` still receives the Mapping (it copies
    ``record.msg`` and renders JSON or console from it, byte-identical to a
    plain dict). A host handler with a plain ``logging.Formatter`` prints
    ``str(record.msg)`` through ``LogRecord.getMessage``, and for a plain dict
    that is a Python dict repr. This subclass renders ``event key=value ...``
    instead, with the fields the host formatter prints itself dropped and any
    rendered stack or exception appended on new lines.

    The rendered exception is left out when the record carries ``exc_info``
    itself — structlog proxies ``.exception()`` to ``Logger.exception``, which
    attaches it — because the host formatter appends that traceback on its
    own and would otherwise print it twice. Other handlers on the root
    (Sentry, OTEL) keep reading ``exc_info`` from the record as before.

    A flat ``extra`` mapping is not an option: baldur's log calls use
    ``name=``, ``message=``, ``args=`` and other ``LogRecord`` attribute names
    as fields, and stdlib raises ``KeyError`` for an ``extra`` key that
    collides with one.
    """

    __slots__ = ("_traceback_on_record",)

    def __init__(
        self, event_dict: dict[str, Any], *, traceback_on_record: bool = False
    ) -> None:
        super().__init__(event_dict)
        self._traceback_on_record = traceback_on_record

    def __str__(self) -> str:
        fields = {
            key: value
            for key, value in self.items()
            if key not in _HOST_FORMATTER_OWNED_FIELDS
        }
        event = fields.pop("event", "")
        exception = fields.pop("exception", None)
        stack = fields.pop("stack", None)
        rendered = str(event)
        if fields:
            pairs = _HOST_KEY_VALUE_RENDERER(None, "", fields)
            rendered = f"{rendered} {pairs}"
        if stack:
            rendered = f"{rendered}\n{stack}"
        if exception and not self._traceback_on_record:
            rendered = f"{rendered}\n{exception}"
        return rendered


# The structlog method structlog.stdlib.BoundLogger proxies to
# logging.Logger.exception, which attaches exc_info to the record itself.
_STDLIB_EXCEPTION_METHOD = "exception"


def _wrap_for_host_and_formatter(
    logger: logging.Logger, name: str, event_dict: dict[str, Any]
) -> tuple[tuple[_HostReadableEventDict], dict[str, dict[str, Any]]]:
    """Last processor: ``ProcessorFormatter.wrap_for_formatter`` with a
    host-readable event dict.

    Same contract as structlog's own — the event dict becomes the record's
    ``msg`` and the logger / method name travel in ``extra`` — except that the
    ``msg`` renders readably through any host formatter.
    """
    readable = _HostReadableEventDict(
        event_dict, traceback_on_record=name == _STDLIB_EXCEPTION_METHOD
    )
    return (readable,), {"extra": {"_logger": logger, "_name": name}}


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


def route_structlog_to_stdlib() -> None:
    """Route structlog through stdlib logging before the settings are read.

    Until ``configure_structlog()`` has run, structlog's default configuration
    prints every level to stdout regardless of any level setting. This
    installs the processors that read nothing — not the validator, rate
    limiter or sampler (``LoggingSettings``), and not the OTEL trace-context
    injector (its first call initialises OTEL from the observability
    settings) — plus the host-readable wrapper, so it can run before, and
    while, the settings object is being built without constructing a second
    one; and with no logger caching, so a record emitted in that window (a
    settings cross-validation warning raised inside the settings constructor,
    the CLI's own config-resolution lines) becomes a stdlib record: dropped
    by the root level, or written once by the host's handler or
    ``logging.lastResort``. ``configure_structlog()`` replaces it with the
    full pipeline on first entry; once that has happened this is a no-op, so
    a caller can never downgrade a configured process.
    """
    if _structlog_state().configured:
        return
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            cast(structlog.types.Processor, _wrap_for_host_and_formatter),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


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
        # Reading the settings can itself emit (cross-validation warnings run
        # inside the settings constructor); route those through stdlib first
        # so they never reach structlog's default printer.
        route_structlog_to_stdlib()

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
                cast(structlog.types.Processor, _wrap_for_host_and_formatter),
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
        #
        # Structural processors only: a host record that reaches baldur's own
        # handler on the zero-config path is written once, so the stateful
        # processors that can drop or reject an event (the event-name
        # validator, the rate limiter, the sampler) run for baldur's own
        # records only — they already ran once, in BoundLogger.
        foreign_pre_chain: list[structlog.types.Processor] = [
            structlog.stdlib.ExtraAdder(),
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            cast(structlog.types.Processor, _inject_otel_trace_context),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
        ]
        formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            foreign_pre_chain=foreign_pre_chain,
        )

        # BALDUR_LOG_LEVEL: presence is the operator's explicit level intent;
        # an unrecognised value falls back to the default, as it always has.
        _level_name = os.environ.get("BALDUR_LOG_LEVEL")
        _explicit_level = _level_name is not None
        _log_level = getattr(
            logging, (_level_name or _DEFAULT_LOG_LEVEL_NAME).upper(), None
        )
        if not isinstance(_log_level, int):
            _log_level = getattr(logging, _DEFAULT_LOG_LEVEL_NAME)

        # In the test environment, NullHandler blocks console output entirely.
        # StreamHandler(sys.stdout) grabs the original stdout reference at
        # pytest_configure time and so bypasses pytest capture — NullHandler is
        # the only workable answer under test. pytest's caplog uses its own
        # LogCaptureHandler, so it is unaffected.
        _test_level_name = os.environ.get("BALDUR_TEST_LOG_LEVEL")
        if _test_level_name:
            root_logger = logging.getLogger()
            _effective_level = getattr(
                logging, _test_level_name.upper(), logging.WARNING
            )
            root_logger.setLevel(_effective_level)
            root_logger.addHandler(logging.NullHandler())
        else:
            # The zero-config path IS logging.basicConfig with a JSON handler:
            # the handler and the root level are installed only when the root
            # has no handler, and the call is a no-op for an application that
            # configured its own logging — its level, handlers, format and
            # stream stay, and baldur's events reach them by propagation.
            handler = _BaldurStreamHandler(sys.stdout)
            handler.setFormatter(formatter)
            logging.basicConfig(handlers=[handler], level=_log_level)

        # A set BALDUR_LOG_LEVEL is the level of baldur's own loggers in both
        # cases — the documented diagnostic switch keeps working inside a
        # configured host. Unset, they stay NOTSET and inherit the host's root
        # level, as any library's loggers do.
        if _explicit_level:
            for namespace in _BALDUR_LOGGER_NAMESPACES:
                logging.getLogger(namespace).setLevel(_log_level)

        # =====================================================================
        # Apply the per-component log levels (see _apply_component_log_levels).
        # Applies the 8 level values from LoggingSettings to the actual stdlib
        # loggers via setLevel(), making them controllable by environment
        # variable alone:
        #   BALDUR_LOGGING_SETTINGS_CIRCUIT_BREAKER_LOG_LEVEL=WARNING
        # Runs after the namespace write above, so the families keep
        # precedence over BALDUR_LOG_LEVEL.
        # =====================================================================
        _apply_component_log_levels(settings)

        # Floor the posture logger at INFO so the one-line startup summary
        # survives a WARNING root level. Keyed on the operator not having
        # expressed a level intent — the same explicit-set convention the
        # Redis posture predicate uses — so BALDUR_LOG_LEVEL=ERROR silences
        # this line like anything else, and an operator who names the logger
        # in their own config wins by writing it later. A write to baldur's
        # own logger, so it applies inside a configured host as well.
        if not _explicit_level:
            logging.getLogger(POSTURE_LOGGER_NAME).setLevel(_POSTURE_FLOOR_LEVEL)

        state.configured = True


def reset_structlog_config() -> None:
    """Reset the structlog configuration in tests.

    Clears the configured flag and removes the handler baldur installed on the
    root logger (identified by its class, never by its formatter — a host's own
    ``ProcessorFormatter`` handler is not baldur's), so the next
    configure_structlog() call rebuilds everything from the new configuration
    values. Restores baldur's own loggers and the posture logger to NOTSET so
    neither a ``BALDUR_LOG_LEVEL`` write nor the INFO floor leaks across tests.
    """
    _structlog_state().configured = False
    logging.getLogger(POSTURE_LOGGER_NAME).setLevel(logging.NOTSET)
    for namespace in _BALDUR_LOGGER_NAMESPACES:
        logging.getLogger(namespace).setLevel(logging.NOTSET)

    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers if not isinstance(h, _BaldurStreamHandler)
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
