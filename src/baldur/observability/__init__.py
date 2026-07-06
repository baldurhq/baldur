"""
OpenTelemetry SDK Initialization.

Provides TracerProvider setup, OTLP exporter configuration,
and compatibility layer with existing trace_id system.

450 Phase 4: every mutable OTEL flag (initialization status, provider/meter
handles, instrumentation guards) is now stored on a runtime-scoped
``_OtelState`` object accessed via :func:`_otel_state`. Resetting the active
``BaldurRuntime`` (or the ``reset_opentelemetry`` helper below) drops the
whole bundle atomically, eliminating the cross-test contamination that the
~11 module-level globals used to cause.

Status: Internal
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

logger = structlog.get_logger()


class _OtelState:
    """Runtime-scoped OpenTelemetry init/instrumentation state."""

    __slots__ = (
        "celery_instrumented",
        "django_instrumented",
        "initialized",
        "logger_provider",
        "logging_instrumented",
        "meter",
        "meter_provider",
        "prometheus_metric_reader",
        "requests_instrumented",
        "tracer",
        "tracer_provider",
    )

    def __init__(self) -> None:
        self.initialized: bool = False
        self.tracer_provider: Any = None
        self.tracer: Any = None
        self.logger_provider: Any = None
        self.meter_provider: Any = None
        self.meter: Any = None
        self.prometheus_metric_reader: Any = None
        self.logging_instrumented: bool = False
        self.requests_instrumented: bool = False
        self.celery_instrumented: bool = False
        self.django_instrumented: bool = False


def _otel_state() -> _OtelState:
    from baldur.runtime import get_runtime

    return get_runtime().get_singleton("otel_state", _OtelState)


def _is_otel_available() -> bool:
    """Check if OpenTelemetry SDK packages are installed."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401

        return True
    except ImportError:
        return False


def _is_otel_meter_available() -> bool:
    """Check if the OpenTelemetry metric SDK + Prometheus bridge are installed.

    Parallel to :func:`_is_otel_available` for the metric path: probes the
    PrometheusMetricReader bridge that exposes OTEL Meter instruments through
    prometheus_client. Used by AUTO profile resolution and by the metrics
    backend derivation to avoid selecting a dead OTEL meter.
    """
    try:
        import opentelemetry.exporter.prometheus  # noqa: F401
        import opentelemetry.sdk.metrics  # noqa: F401

        return True
    except ImportError:
        return False


def _select_sampler(settings: Any) -> Any:
    """Resolve the OTEL trace sampler from settings, strategy-first.

    The explicit ``traces_sampler`` strategy is honored before the adaptive
    flag. The absolute strategies (``always_on`` / ``always_off`` /
    ``parentbased_always_*``) express an unambiguous OTEL-standard user
    contract and win over ``adaptive_sampling_enabled``; emergency-level
    escalation only wraps the ratio strategies, where a normal base rate that
    escalates during incidents is meaningful.

    Returns a duck-typed Sampler — an OTEL public building block
    (``ALWAYS_ON`` / ``ALWAYS_OFF`` / ``DEFAULT_ON`` / ``DEFAULT_OFF`` /
    ``TraceIdRatioBased`` / ``ParentBasedTraceIdRatio``) or
    ``EmergencyLevelAdaptiveSampler``.
    """
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        DEFAULT_OFF,
        DEFAULT_ON,
        ParentBasedTraceIdRatio,
        TraceIdRatioBased,
    )

    strategy = settings.traces_sampler
    arg = settings.traces_sampler_arg
    adaptive = settings.adaptive_sampling_enabled

    # Absolute strategies express an unambiguous contract and override the
    # adaptive flag. When adaptive is enabled but an absolute strategy wins the
    # adaptive sampler is inert — surface the precedence to operators (DEBUG,
    # not an error: this is a precedence situation, not a misconfiguration).
    absolute = {
        "always_on": ALWAYS_ON,
        "always_off": ALWAYS_OFF,
        "parentbased_always_on": DEFAULT_ON,
        "parentbased_always_off": DEFAULT_OFF,
    }
    if strategy in absolute:
        if adaptive:
            logger.debug(
                "otel.adaptive_sampling_bypassed",
                traces_sampler=strategy,
            )
        return absolute[strategy]

    # Ratio strategies: emergency-level escalation wraps the base ratio when
    # adaptive sampling is enabled (the default), otherwise the plain OTEL
    # ratio sampler (parent-based variant preserved).
    if adaptive:
        from baldur.observability.sampler import EmergencyLevelAdaptiveSampler

        return EmergencyLevelAdaptiveSampler(base_ratio=arg)

    if strategy == "parentbased_traceidratio":
        return ParentBasedTraceIdRatio(arg)
    return TraceIdRatioBased(arg)


def _effective_base_sampling_ratio(settings: Any) -> float:
    """Effective head-sampling base ratio (0.0-1.0) for the resolved sampler.

    Mirrors the strategy precedence in ``_select_sampler`` so the rate logged at
    init reflects the *installed* sampler rather than the configured
    ``traces_sampler_arg``: the absolute strategies pin the rate to 1.0/0.0
    regardless of the arg, while the ratio strategies — and the adaptive wrapper,
    whose NORMAL-level base is the arg — use ``traces_sampler_arg``. Keep in sync
    with ``_select_sampler``.
    """
    pinned = {
        "always_on": 1.0,
        "parentbased_always_on": 1.0,
        "always_off": 0.0,
        "parentbased_always_off": 0.0,
    }
    return pinned.get(settings.traces_sampler, settings.traces_sampler_arg)


def initialize_opentelemetry() -> bool:
    """
    Initialize OpenTelemetry SDK with TracerProvider and OTLP Exporter.

    Uses settings from OpenTelemetrySettings.
    Safe to call multiple times (idempotent).

    Returns:
        bool: True if initialization succeeded, False if disabled or failed
    """
    state = _otel_state()

    if state.initialized:
        return state.tracer_provider is not None

    # Check if OTEL is available
    if not _is_otel_available():
        logger.debug("otel.sdk_not_available")
        state.initialized = True
        return False

    # Profile gate — the single effective_otel_enabled signal replaces the
    # removed OpenTelemetrySettings.enabled field.
    from baldur.settings.observability import get_observability_settings

    if not get_observability_settings().effective_otel_enabled:
        logger.debug("otel.sdk_disabled")
        state.initialized = True
        return False

    # Import settings
    from baldur.settings.otel import get_otel_settings

    settings = get_otel_settings()

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        # Build resource attributes
        resource_attrs = {
            "service.name": settings.service_name,
        }
        resource_attrs.update(settings.get_resource_attributes_dict())
        resource = Resource(attributes=resource_attrs)

        # Resolve the sampler strategy-first (all six `traces_sampler` values;
        # absolute strategies override the adaptive flag). The result is a
        # duck-typed Sampler — `EmergencyLevelAdaptiveSampler` implements
        # should_sample/get_description without inheriting Sampler — so the
        # local annotation widens to Any.
        sampler: Any = _select_sampler(settings)

        # Create TracerProvider
        state.tracer_provider = TracerProvider(
            resource=resource,
            sampler=sampler,
        )

        # Configure the span exporter per OTEL_TRACES_EXPORTER. The provider,
        # sampler, and composite propagator are installed in every branch — only
        # the export path differs.
        #   - "otlp"    : batch to the OTLP collector (the fork-safe
        #                 register_at_fork path applies here and only here)
        #   - "console" : synchronous stdout for dev visibility (no collector)
        #   - "none"    : no processor — spans are created but never exported,
        #                 keeping trace_id-in-logs correlation while silencing
        #                 no-collector failure noise
        if settings.traces_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            otlp_exporter = OTLPSpanExporter(
                endpoint=settings.exporter_otlp_endpoint,
                timeout=settings.exporter_otlp_timeout_ms / 1000,  # to seconds
            )
            state.tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        elif settings.traces_exporter == "console":
            from opentelemetry.sdk.trace.export import (
                ConsoleSpanExporter,
                SimpleSpanProcessor,
            )

            state.tracer_provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter())
            )
        # "none": intentionally no span processor added.

        # Set as global TracerProvider
        trace.set_tracer_provider(state.tracer_provider)

        # Create default tracer
        state.tracer = trace.get_tracer(settings.service_name)

        state.initialized = True
        logger.info(
            "otel.initialized",
            service_name=settings.service_name,
            traces_exporter=settings.traces_exporter,
            exporter_endpoint=settings.exporter_otlp_endpoint,
            traces_sampler=settings.traces_sampler,
            sampling_ratio_percent=_effective_base_sampling_ratio(settings) * 100,
        )

        # Baggage Propagator 등록 — traceparent + baggage 헤더 자동 전파
        from baldur.observability.baggage import setup_baggage_propagation

        setup_baggage_propagation()

        return True

    except Exception as e:
        logger.warning(
            "otel.opentelemetry_init_failed",
            error=e,
        )
        state.initialized = True
        return False


def get_tracer():
    """
    Get the configured OpenTelemetry tracer.

    Returns:
        Tracer instance if OTEL is initialized, None otherwise
    """
    state = _otel_state()
    if not state.initialized:
        initialize_opentelemetry()
    return state.tracer


def get_tracer_provider():
    """
    Get the configured TracerProvider.

    Returns:
        TracerProvider instance if OTEL is initialized, None otherwise
    """
    state = _otel_state()
    if not state.initialized:
        initialize_opentelemetry()
    return state.tracer_provider


def is_otel_enabled() -> bool:
    """
    Check if OpenTelemetry is enabled and initialized.

    Returns:
        bool: True if OTEL is active, False otherwise
    """
    state = _otel_state()
    if not state.initialized:
        initialize_opentelemetry()
    return state.tracer_provider is not None


def get_current_span():
    """
    Get the current active span from OpenTelemetry context.

    Returns:
        Current Span if OTEL is enabled, None otherwise
    """
    if not is_otel_enabled():
        return None

    try:
        from opentelemetry import trace

        return trace.get_current_span()
    except Exception:
        return None


def get_current_trace_id_from_otel() -> str | None:
    """
    Extract trace_id from the current OpenTelemetry span context.

    Returns:
        32-character hex trace_id if available, None otherwise
    """
    span = get_current_span()
    if span is None:
        return None

    try:
        span_context = span.get_span_context()
        if span_context and span_context.is_valid:
            # Format as 32-character hex string (W3C standard)
            return format(span_context.trace_id, "032x")
    except Exception:
        pass

    return None


def get_current_span_id_from_otel() -> str | None:
    """
    Extract span_id from the current OpenTelemetry span context.

    Returns:
        16-character hex span_id if available, None otherwise
    """
    span = get_current_span()
    if span is None:
        return None

    try:
        span_context = span.get_span_context()
        if span_context and span_context.is_valid:
            # Format as 16-character hex string
            return format(span_context.span_id, "016x")
    except Exception:
        pass

    return None


def _build_span_link(trace_id_hex: str | None, span_id_hex: str | None) -> Any:
    """Build an OTEL ``Link`` to the origin ``(trace_id, span_id)`` SpanContext.

    Returns None when either id is absent or not a well-formed W3C hex value
    (32-hex trace, 16-hex span, both non-zero) — a link cannot be fabricated
    from a display id like ``req-xxx``. Fail-open: any construction error
    yields None so the caller degrades to an unlinked span.
    """
    if not trace_id_hex or not span_id_hex:
        return None
    if len(trace_id_hex) != 32 or len(span_id_hex) != 16:
        return None
    try:
        trace_id_int = int(trace_id_hex, 16)
        span_id_int = int(span_id_hex, 16)
    except (ValueError, TypeError):
        return None
    if trace_id_int == 0 or span_id_int == 0:
        return None
    try:
        from opentelemetry.trace import Link, SpanContext, TraceFlags

        span_context = SpanContext(
            trace_id=trace_id_int,
            span_id=span_id_int,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        return Link(span_context)
    except Exception:
        return None


@contextmanager
def span_with_link(
    name: str,
    trace_id_hex: str | None,
    span_id_hex: str | None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Start a span linked to a prior SpanContext, fail-open.

    Yields a span linked to the origin ``(trace_id_hex, span_id_hex)``
    SpanContext when OTEL is enabled and both ids are well-formed W3C hex.
    Otherwise yields None WITHOUT creating a span — the caller's block runs
    unchanged. The link is attached at span creation (works on every OTEL SDK
    version; avoids the newer mutate-current-span ``add_link`` API).

    Span creation is fail-open (a tracing failure never affects the wrapped
    operation); the wrapped body is NOT — a body exception propagates to the
    caller and is recorded on the span, never swallowed here.

    Args:
        name: Span name (e.g. ``"dlq.replay"``).
        trace_id_hex: Origin 32-hex W3C trace id, or None.
        span_id_hex: Origin 16-hex span id, or None.
        attributes: Optional searchable span attributes.
    """
    if not is_otel_enabled():
        yield None
        return

    link = _build_span_link(trace_id_hex, span_id_hex)
    if link is None:
        yield None
        return

    span_cm = None
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("baldur")
        span_cm = tracer.start_as_current_span(
            name, links=[link], attributes=attributes or {}
        )
    except Exception:
        span_cm = None

    if span_cm is None:
        yield None
        return

    with span_cm as span:
        yield span


def initialize_meter_provider() -> bool:
    """
    Initialize OpenTelemetry MeterProvider with PrometheusMetricReader.

    PrometheusMetricReader bridges OTEL instruments to prometheus_client
    REGISTRY, enabling /metrics text exposition while using OTEL Meter API.
    This resolves Prometheus multiprocess metrics fragmentation (Section 5.4).

    Returns:
        bool: True if initialization succeeded, False if unavailable
    """
    state = _otel_state()

    if state.meter_provider is not None:
        return True

    # Profile gate — only build the MeterProvider when OTel export is enabled.
    from baldur.settings.observability import get_observability_settings

    if not get_observability_settings().effective_otel_enabled:
        return False

    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource

        from baldur.settings.otel import get_otel_settings

        settings = get_otel_settings()

        resource_attrs = {
            "service.name": settings.service_name,
        }
        resource_attrs.update(settings.get_resource_attributes_dict())
        resource = Resource(attributes=resource_attrs)

        state.prometheus_metric_reader = PrometheusMetricReader()
        state.meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[state.prometheus_metric_reader],
        )
        state.meter = state.meter_provider.get_meter(
            "baldur",
            version="1.0.0",
        )

        logger.info(
            "otel.meter_provider_initialized",
            service_name=settings.service_name,
        )
        return True

    except ImportError:
        # Under an OTel-enabled profile the bridge being absent means metrics
        # fell back to local Prometheus (effective_backend derives prometheus),
        # so surface it loudly rather than at DEBUG — it is a misconfig signal,
        # not silent metric loss.
        logger.warning("otel.prometheus_metric_reader_not_installed")
        return False
    except Exception as e:
        logger.warning(
            "otel.meter_provider_initialization_failed",
            error=e,
        )
        return False


def get_meter():
    """
    Get the configured OpenTelemetry Meter.

    Returns:
        Meter instance if MeterProvider is initialized, None otherwise
    """
    state = _otel_state()
    if state.meter is None:
        initialize_meter_provider()
    return state.meter


def get_meter_provider():
    """
    Get the configured MeterProvider.

    Returns:
        MeterProvider instance if initialized, None otherwise
    """
    state = _otel_state()
    if state.meter_provider is None:
        initialize_meter_provider()
    return state.meter_provider


def shutdown_opentelemetry() -> None:
    """
    Gracefully shutdown OpenTelemetry SDK.

    Flushes pending spans, log records, and metrics, releases resources.
    """
    state = _otel_state()

    if state.logger_provider is not None:
        try:
            state.logger_provider.shutdown()
            logger.debug("otel.logger_provider_shutdown")
        except Exception as e:
            logger.warning(
                "error.during_loggerprovider_shutdown",
                error=e,
            )
        state.logger_provider = None

    if state.tracer_provider is not None:
        try:
            state.tracer_provider.shutdown()
            logger.debug("otel.tracer_provider_shutdown")
        except Exception as e:
            logger.warning(
                "error.during_tracerprovider_shutdown",
                error=e,
            )

    if state.meter_provider is not None:
        try:
            state.meter_provider.shutdown()
            logger.debug("otel.meter_provider_shutdown")
        except Exception as e:
            logger.warning(
                "otel.meter_provider_shutdown_failed",
                error=e,
            )

    state.tracer_provider = None
    state.tracer = None
    state.meter_provider = None
    state.meter = None
    state.prometheus_metric_reader = None
    state.initialized = False


def reset_opentelemetry() -> None:
    """
    Reset OpenTelemetry state for testing or post-fork reinitialization.

    This forces re-initialization on next use.
    """
    state = _otel_state()
    state.initialized = False
    state.tracer_provider = None
    state.tracer = None
    state.logger_provider = None
    state.meter_provider = None
    state.meter = None
    state.prometheus_metric_reader = None
    state.requests_instrumented = False
    state.celery_instrumented = False
    state.logging_instrumented = False
    state.django_instrumented = False


def instrument_requests() -> bool:
    """
    Enable automatic instrumentation for requests library.

    Adds automatic span creation and traceparent header injection
    for all outgoing HTTP requests made via the requests library.

    Returns:
        bool: True if instrumentation was successful, False otherwise
    """
    state = _otel_state()

    if state.requests_instrumented:
        return True

    if not is_otel_enabled():
        return False

    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
        state.requests_instrumented = True
        logger.info("otel.requests_instrumentation_enabled")
        return True

    except ImportError:
        logger.debug("otel.requests_instrumentation_installed")
        return False
    except Exception as e:
        logger.warning(
            "otel.requests_instrument_failed",
            error=e,
        )
        return False


def uninstrument_requests() -> None:
    """
    Disable automatic instrumentation for requests library.

    Used primarily for testing to ensure clean state.
    """
    state = _otel_state()

    if not state.requests_instrumented:
        return

    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().uninstrument()
        state.requests_instrumented = False
        logger.debug("otel.requests_instrumentation_disabled")
    except Exception:
        pass


def instrument_celery() -> bool:
    """
    Enable automatic instrumentation for Celery tasks.

    Adds automatic span creation for Celery task execution
    and propagates trace context between task producers and consumers.

    Returns:
        bool: True if instrumentation was successful, False otherwise
    """
    state = _otel_state()

    if state.celery_instrumented:
        return True

    if not is_otel_enabled():
        return False

    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
        state.celery_instrumented = True
        logger.info("otel.celery_instrumentation_enabled")
        return True

    except ImportError:
        logger.debug("otel.celery_instrumentation_installed")
        return False
    except Exception as e:
        logger.warning(
            "otel.celery_instrument_failed",
            error=e,
        )
        return False


def uninstrument_celery() -> None:
    """
    Disable automatic instrumentation for Celery.

    Used primarily for testing to ensure clean state.
    """
    state = _otel_state()

    if not state.celery_instrumented:
        return

    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().uninstrument()
        state.celery_instrumented = False
        logger.debug("otel.celery_instrumentation_disabled")
    except Exception:
        pass


def is_requests_instrumented() -> bool:
    """Check if requests library is instrumented."""
    return _otel_state().requests_instrumented


def is_celery_instrumented() -> bool:
    """Check if Celery is instrumented."""
    return _otel_state().celery_instrumented


def is_django_instrumented() -> bool:
    """Check if Django is instrumented."""
    return _otel_state().django_instrumented


def instrument_django() -> bool:
    """
    Enable automatic instrumentation for Django.

    WSGI 레벨에서 traceparent + baggage 헤더를 자동 추출하고,
    Django 요청에 대한 span을 자동 생성한다.

    DjangoInstrumentor는 내부적으로 MIDDLEWARE 최상단에
    _DjangoMiddleware를 자동 삽입한다.
    따라서 BaggageSyncMiddleware보다 반드시 먼저 실행된다.

    excluded_urls: /health, /metrics 등 불필요한 span/baggage 파싱 제외.

    Returns:
        bool: True if instrumentation was successful, False otherwise
    """
    state = _otel_state()

    if state.django_instrumented:
        return True

    if not is_otel_enabled():
        return False

    try:
        import os

        from opentelemetry.instrumentation.django import DjangoInstrumentor

        from baldur.settings.otel import get_otel_settings

        settings = get_otel_settings()

        if not settings.django_instrument_enabled:
            logger.debug("otel.django_instrumentation_disabled_config")
            return False

        # excluded_urls 설정 적용 — 환경변수 OTEL_PYTHON_DJANGO_EXCLUDED_URLS 사용
        excluded = ",".join(settings.get_excluded_urls_list())
        if excluded:
            os.environ.setdefault("OTEL_PYTHON_DJANGO_EXCLUDED_URLS", excluded)

        DjangoInstrumentor().instrument()
        state.django_instrumented = True
        logger.info(
            "otel.django_instrumentation_enabled",
            excluded_urls=excluded or "none",
        )
        return True

    except ImportError:
        logger.debug("otel.django_instrumentation_installed")
        return False
    except Exception as e:
        logger.warning(
            "otel.django_instrument_failed",
            error=e,
        )
        return False


def uninstrument_django() -> None:
    """
    Disable automatic instrumentation for Django.

    Used primarily for testing to ensure clean state.
    """
    state = _otel_state()

    if not state.django_instrumented:
        return

    try:
        from opentelemetry.instrumentation.django import DjangoInstrumentor

        DjangoInstrumentor().uninstrument()
        state.django_instrumented = False
        logger.debug("otel.django_instrumentation_disabled")
    except Exception:
        pass


def is_logging_instrumented() -> bool:
    """Check if Python logging is instrumented."""
    return _otel_state().logging_instrumented


def _is_otel_logging_available() -> bool:
    """Check if OpenTelemetry logging SDK packages are installed."""
    try:
        import opentelemetry.exporter.otlp.proto.grpc._log_exporter  # noqa: F401
        import opentelemetry.sdk._logs  # noqa: F401

        return True
    except ImportError:
        return False


def initialize_logger_provider() -> bool:
    """
    Initialize OpenTelemetry LoggerProvider with OTLP Log Exporter.

    Enables sending Python logs to OTEL Collector for storage in Loki.
    Automatically includes trace_id and span_id in log records.

    Returns:
        bool: True if initialization succeeded, False if disabled or failed
    """
    state = _otel_state()

    if state.logger_provider is not None:
        return True

    if not is_otel_enabled():
        return False

    if not _is_otel_logging_available():
        logger.debug("otel.logging_sdk_installed")
        return False

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource

        # Import settings
        from baldur.settings.otel import get_otel_settings

        settings = get_otel_settings()

        # Build resource attributes (same as TracerProvider)
        resource_attrs = {
            "service.name": settings.service_name,
        }
        resource_attrs.update(settings.get_resource_attributes_dict())
        resource = Resource(attributes=resource_attrs)

        # Create LoggerProvider
        state.logger_provider = LoggerProvider(resource=resource)

        # Configure OTLP Log Exporter
        otlp_log_exporter = OTLPLogExporter(
            endpoint=settings.exporter_otlp_endpoint,
            timeout=settings.exporter_otlp_timeout_ms / 1000,
        )

        # Add BatchLogRecordProcessor for efficient export
        log_processor = BatchLogRecordProcessor(otlp_log_exporter)
        state.logger_provider.add_log_record_processor(log_processor)

        # Set as global LoggerProvider
        set_logger_provider(state.logger_provider)

        logger.info(
            "otel.logger_provider_initialized",
            service_name=settings.service_name,
            exporter_endpoint=settings.exporter_otlp_endpoint,
        )
        return True

    except Exception as e:
        logger.warning(
            "otel.loggerprovider_init_failed",
            error=e,
        )
        return False


def get_logger_provider():
    """
    Get the configured LoggerProvider.

    Returns:
        LoggerProvider instance if OTEL logging is initialized, None otherwise
    """
    state = _otel_state()
    if state.logger_provider is None:
        initialize_logger_provider()
    return state.logger_provider


def instrument_logging() -> bool:
    """
    Enable automatic instrumentation for Python logging.

    Adds OpenTelemetry handler to Python logging that:
    - Sends log records to OTEL Collector via LoggerProvider
    - Automatically includes trace_id and span_id from current span context
    - Enables log-trace correlation in Grafana

    Returns:
        bool: True if instrumentation was successful, False otherwise
    """
    state = _otel_state()

    if state.logging_instrumented:
        return True

    if not is_otel_enabled():
        return False

    if not initialize_logger_provider():
        return False

    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        # Instrument logging to add trace context to all log records
        LoggingInstrumentor().instrument(
            set_logging_format=True,
            log_level=logging.INFO,
        )

        state.logging_instrumented = True
        logger.info("otel.logging_instrumentation_enabled")
        return True

    except ImportError:
        logger.debug("otel.logging_instrumentation_installed")
        return False
    except Exception as e:
        logger.warning(
            "otel.logging_instrument_failed",
            error=e,
        )
        return False


def uninstrument_logging() -> None:
    """
    Disable automatic instrumentation for Python logging.

    Used primarily for testing to ensure clean state.
    """
    state = _otel_state()

    if not state.logging_instrumented:
        return

    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().uninstrument()
        state.logging_instrumented = False
        logger.debug("otel.logging_instrumentation_disabled")
    except Exception:
        pass


def shutdown_logger_provider() -> None:
    """
    Gracefully shutdown OpenTelemetry LoggerProvider.

    Flushes pending log records and releases resources.
    """
    state = _otel_state()

    if state.logger_provider is not None:
        try:
            state.logger_provider.shutdown()
            logger.debug("otel.logger_provider_shutdown")
        except Exception as e:
            logger.warning(
                "error.during_loggerprovider_shutdown",
                error=e,
            )

    state.logger_provider = None
