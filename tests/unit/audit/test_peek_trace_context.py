"""Unit tests for the 679 origin-trace helpers in ``baldur.audit.trace``.

Covers:
- ``peek_trace_context()`` — the non-generating capture-side snapshot. Unlike
  ``get_trace_id()`` it must NEVER generate-and-set a fresh id, and it layers
  OTEL span -> context variable -> thread-local (display id only off the last
  two, the full W3C trio only off an active OTEL span).
- ``extract_origin_trace()`` — the read-side companion. Reads the three origin
  keys via plain ``.get()``, guarding only the non-dict / None shape, and is
  deliberately marker-tolerant (a truncation marker that carries origin keys
  still yields them).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.audit.trace import (
    _thread_local,
    _trace_id_var,
    clear_trace_id,
    extract_origin_trace,
    peek_trace_context,
)

# Well-formed W3C ids used across the OTEL-source cases.
_FULL_TRACE_ID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
_SPAN_ID = "1234567890abcdef"
_DISPLAY_ID = f"req-{_FULL_TRACE_ID[:8]}"


class TestPeekTraceContextBehavior:
    """State-source enumeration + the non-generating guarantee."""

    def setup_method(self):
        clear_trace_id()

    def teardown_method(self):
        clear_trace_id()

    def test_peek_returns_all_none_when_no_trace_active(self):
        """No OTEL span, no context var, no thread-local -> all-None trio."""
        with patch("baldur.audit.trace._get_trace_id_from_otel", return_value=None):
            result = peek_trace_context()

        assert result == {
            "trace_id": None,
            "trace_id_full": None,
            "span_id": None,
        }

    def test_peek_does_not_generate_or_set_a_fresh_id_when_absent(self):
        """The core non-generating guarantee: peek must not mutate trace state.

        ``get_trace_id()`` generates-and-sets on a miss; ``peek_trace_context``
        must not — otherwise a no-trace capture would fabricate an origin id.
        """
        with patch("baldur.audit.trace._get_trace_id_from_otel", return_value=None):
            peek_trace_context()

        # No fresh id was written to either backing store.
        assert _trace_id_var.get() is None
        assert getattr(_thread_local, "trace_id", None) is None

    def test_peek_reads_context_var_display_id_only(self):
        """A context-var trace surfaces as the display id with no full/span."""
        _trace_id_var.set("req-ctxvar")

        with patch("baldur.audit.trace._get_trace_id_from_otel", return_value=None):
            result = peek_trace_context()

        assert result["trace_id"] == "req-ctxvar"
        assert result["trace_id_full"] is None
        assert result["span_id"] is None

    def test_peek_falls_back_to_thread_local_when_context_var_empty(self):
        """Thread-local is read when the context var is empty (display only)."""
        _trace_id_var.set(None)
        _thread_local.trace_id = "req-threadlocal"

        with patch("baldur.audit.trace._get_trace_id_from_otel", return_value=None):
            result = peek_trace_context()

        assert result["trace_id"] == "req-threadlocal"
        assert result["trace_id_full"] is None
        assert result["span_id"] is None

    def test_peek_prefers_otel_and_includes_full_trio(self):
        """An active OTEL span yields the display id + full W3C id + span id.

        OTEL is first in precedence and is the only source that can supply the
        full 32-hex trace id and 16-hex span id needed to rebuild a
        ``SpanContext`` for a downstream span link.
        """
        # A context-var value is present too — OTEL must still win.
        _trace_id_var.set("req-ctxvar-should-be-shadowed")

        with (
            patch(
                "baldur.audit.trace._get_trace_id_from_otel",
                return_value=_DISPLAY_ID,
            ),
            patch("baldur.observability.is_otel_enabled", return_value=True),
            patch(
                "baldur.observability.get_current_trace_id_from_otel",
                return_value=_FULL_TRACE_ID,
            ),
            patch(
                "baldur.observability.get_current_span_id_from_otel",
                return_value=_SPAN_ID,
            ),
        ):
            result = peek_trace_context()

        assert result["trace_id"] == _DISPLAY_ID
        assert result["trace_id_full"] == _FULL_TRACE_ID
        assert result["span_id"] == _SPAN_ID

    def test_peek_otel_source_survives_observability_import_error(self):
        """If the observability full-id lookup fails, the display id still rides.

        The OTEL branch guards the full/span lookup in try/except — a failure
        there degrades to the display id alone, never crashes the capture.
        """
        with (
            patch(
                "baldur.audit.trace._get_trace_id_from_otel",
                return_value=_DISPLAY_ID,
            ),
            patch(
                "baldur.observability.is_otel_enabled",
                side_effect=RuntimeError("otel probe broke"),
            ),
        ):
            result = peek_trace_context()

        assert result["trace_id"] == _DISPLAY_ID
        assert result["trace_id_full"] is None
        assert result["span_id"] is None


class TestExtractOriginTraceContract:
    """Equivalence partitioning over stored ``metadata`` shapes.

    Hardcoded key names / all-None shape are the design contract from D1, so
    this is a Contract class (fixed literals, not source-referenced).
    """

    _FULL_META = {
        "origin_trace_id": _DISPLAY_ID,
        "origin_trace_id_full": _FULL_TRACE_ID,
        "origin_span_id": _SPAN_ID,
    }
    _DISPLAY_ONLY_META = {"origin_trace_id": "req-ctxvar"}
    _MARKER_NO_KEYS = {
        "_truncated": True,
        "original_size": 5000,
        "preview": "{'big': 'xxx...'}",
    }
    _MARKER_WITH_KEYS = {
        "_truncated": True,
        "original_size": 5000,
        "preview": "{'big': 'xxx...'}",
        "origin_trace_id": _DISPLAY_ID,
        "origin_trace_id_full": _FULL_TRACE_ID,
        "origin_span_id": _SPAN_ID,
    }

    def test_full_trio_metadata_returns_all_three(self):
        assert extract_origin_trace(self._FULL_META) == {
            "origin_trace_id": _DISPLAY_ID,
            "origin_trace_id_full": _FULL_TRACE_ID,
            "origin_span_id": _SPAN_ID,
        }

    def test_display_only_metadata_returns_display_and_none_rest(self):
        assert extract_origin_trace(self._DISPLAY_ONLY_META) == {
            "origin_trace_id": "req-ctxvar",
            "origin_trace_id_full": None,
            "origin_span_id": None,
        }

    @pytest.mark.parametrize(
        "metadata",
        [None, "a-string", 123, ["list"], {}],
    )
    def test_none_non_dict_or_empty_metadata_returns_all_none(self, metadata):
        assert extract_origin_trace(metadata) == {
            "origin_trace_id": None,
            "origin_trace_id_full": None,
            "origin_span_id": None,
        }

    def test_truncation_marker_without_origin_keys_returns_all_none(self):
        """A marker with no origin keys yields all-None via the ``.get()`` miss.

        The marker's fixed keys cannot collide with ``origin_*`` names, so a
        pre-679 / no-trace truncated entry naturally skips linkage.
        """
        assert extract_origin_trace(self._MARKER_NO_KEYS) == {
            "origin_trace_id": None,
            "origin_trace_id_full": None,
            "origin_span_id": None,
        }

    def test_truncation_marker_with_origin_keys_still_restores_trio(self):
        """Regression guard (D1/D2): markers are NOT actively rejected.

        Capture injects origin keys AFTER truncation, so a truncated-but-traced
        entry carries them on the marker and must still link.
        """
        assert extract_origin_trace(self._MARKER_WITH_KEYS) == {
            "origin_trace_id": _DISPLAY_ID,
            "origin_trace_id_full": _FULL_TRACE_ID,
            "origin_span_id": _SPAN_ID,
        }
