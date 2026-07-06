"""Unit tests for the 679 OTEL span-link helper in ``baldur.observability``.

Covers:
- ``_build_span_link()`` — builds an OTEL ``Link`` to the origin
  ``(trace_id, span_id)`` SpanContext, returning None for any malformed /
  absent id (a link cannot be fabricated from a display id like ``req-xxx``).
- ``span_with_link()`` — a fail-open context manager that starts a
  ``dlq.replay``-style span linked to the origin SpanContext when OTEL is on
  and both ids are well-formed, and otherwise yields None WITHOUT creating a
  span (the caller's block runs unchanged).

The exported-span assertion drives a real ``TracerProvider`` +
``InMemorySpanExporter`` and asserts on the *finished* span (its ``links`` and
attributes), not merely that ``start_as_current_span`` was called — this guards
the actual W3C SpanContext export shape.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("opentelemetry")

from baldur.observability import (  # noqa: E402
    _build_span_link,
    reset_opentelemetry,
    span_with_link,
)

# Well-formed W3C ids: 32-hex trace, 16-hex span, both non-zero.
_FULL_TRACE_ID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
_SPAN_ID = "1234567890abcdef"


def _in_memory_tracer():
    """A tracer backed by an ``InMemorySpanExporter`` for export assertions.

    Returns (tracer, exporter). A fresh ``TracerProvider`` is used per call so
    the global provider (set-once per process) is never touched; the caller
    patches ``opentelemetry.trace.get_tracer`` to hand this tracer to
    ``span_with_link``.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("baldur"), exporter


class TestBuildSpanLinkBehavior:
    """Malformed-id boundary for the link builder (no OTEL enable check here)."""

    def test_well_formed_ids_build_a_link_to_the_origin_span_context(self):
        link = _build_span_link(_FULL_TRACE_ID, _SPAN_ID)

        assert link is not None
        ctx = link.context
        # The origin ids round-trip through the SpanContext as W3C hex.
        assert format(ctx.trace_id, "032x") == _FULL_TRACE_ID
        assert format(ctx.span_id, "016x") == _SPAN_ID

    @pytest.mark.parametrize(
        ("trace_id", "span_id"),
        [
            (None, _SPAN_ID),  # absent trace id
            (_FULL_TRACE_ID, None),  # absent span id
            (None, None),  # both absent
            ("", ""),  # empty strings
        ],
    )
    def test_absent_ids_return_none(self, trace_id, span_id):
        assert _build_span_link(trace_id, span_id) is None

    @pytest.mark.parametrize(
        ("trace_id", "span_id"),
        [
            (_FULL_TRACE_ID[:16], _SPAN_ID),  # short trace id
            (_FULL_TRACE_ID + "ff", _SPAN_ID),  # long trace id
            (_FULL_TRACE_ID, _SPAN_ID[:8]),  # short span id
            ("req-a1b2c3d4", _SPAN_ID),  # a display id, not a W3C hex
        ],
    )
    def test_wrong_length_ids_return_none(self, trace_id, span_id):
        assert _build_span_link(trace_id, span_id) is None

    def test_non_hex_ids_return_none(self):
        # Right length, but not parseable as base-16.
        assert _build_span_link("z" * 32, "z" * 16) is None

    @pytest.mark.parametrize(
        ("trace_id", "span_id"),
        [
            ("0" * 32, _SPAN_ID),  # zero trace id is invalid per W3C
            (_FULL_TRACE_ID, "0" * 16),  # zero span id is invalid per W3C
        ],
    )
    def test_zero_valued_ids_return_none(self, trace_id, span_id):
        assert _build_span_link(trace_id, span_id) is None


class TestSpanWithLinkBehavior:
    """OTEL enabled/disabled state transition + exported-span link shape."""

    def setup_method(self):
        reset_opentelemetry()

    def teardown_method(self):
        reset_opentelemetry()

    def test_yields_none_and_creates_no_span_when_otel_disabled(self):
        """OTEL off -> yield None without resolving a tracer (no side effect)."""
        with (
            patch("baldur.observability.is_otel_enabled", return_value=False),
            patch("opentelemetry.trace.get_tracer") as mock_get_tracer,
        ):
            with span_with_link("dlq.replay", _FULL_TRACE_ID, _SPAN_ID) as span:
                assert span is None

        mock_get_tracer.assert_not_called()

    def test_yields_none_when_ids_malformed_even_though_otel_enabled(self):
        """OTEL on but a display-only id (no full W3C id) -> no span, yield None."""
        with (
            patch("baldur.observability.is_otel_enabled", return_value=True),
            patch("opentelemetry.trace.get_tracer") as mock_get_tracer,
        ):
            with span_with_link("dlq.replay", "req-a1b2c3d4", None) as span:
                assert span is None

        mock_get_tracer.assert_not_called()

    def test_exports_span_with_single_origin_link_and_search_attribute(self):
        """OTEL on + well-formed ids -> a finished span carrying exactly one
        link to the origin SpanContext plus the searchable attribute."""
        tracer, exporter = _in_memory_tracer()

        with (
            patch("baldur.observability.is_otel_enabled", return_value=True),
            patch("opentelemetry.trace.get_tracer", return_value=tracer),
        ):
            with span_with_link(
                "dlq.replay",
                _FULL_TRACE_ID,
                _SPAN_ID,
                attributes={"baldur.dlq.origin_trace_id": "req-a1b2c3d4"},
            ) as span:
                assert span is not None

        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        exported = finished[0]
        assert exported.name == "dlq.replay"

        # Exactly one link, and it points at the origin SpanContext.
        assert len(exported.links) == 1
        link_ctx = exported.links[0].context
        assert format(link_ctx.trace_id, "032x") == _FULL_TRACE_ID
        assert format(link_ctx.span_id, "016x") == _SPAN_ID

        # The searchable attribute rides the span (trace ids stay off metrics).
        assert exported.attributes["baldur.dlq.origin_trace_id"] == "req-a1b2c3d4"

    def test_body_exception_propagates_and_is_not_swallowed(self):
        """Span creation is fail-open; the WRAPPED body is not — a body
        exception propagates (recorded on the span, never swallowed here)."""
        tracer, exporter = _in_memory_tracer()

        with (
            patch("baldur.observability.is_otel_enabled", return_value=True),
            patch("opentelemetry.trace.get_tracer", return_value=tracer),
        ):
            with pytest.raises(ValueError, match="boom"):
                with span_with_link("dlq.replay", _FULL_TRACE_ID, _SPAN_ID):
                    raise ValueError("boom")

        # The span still finished (and recorded the error) despite the raise.
        assert len(exporter.get_finished_spans()) == 1

    def test_yields_none_when_tracer_resolution_raises(self):
        """A tracer-resolution failure degrades to an unlinked run (fail-open)."""
        with (
            patch("baldur.observability.is_otel_enabled", return_value=True),
            patch(
                "opentelemetry.trace.get_tracer",
                side_effect=RuntimeError("no provider"),
            ),
        ):
            with span_with_link("dlq.replay", _FULL_TRACE_ID, _SPAN_ID) as span:
                assert span is None
