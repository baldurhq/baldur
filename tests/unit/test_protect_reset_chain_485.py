"""#485 D7 — ``reset_protect_caches()`` reset-chain extension regression tests.

Round 1 (#480 DEC-3) established a single-call reset surface that
flushed the per-name CB policy cache + the protect-scope recorder
sticky state + TimeoutPolicy's executor. Round 2 (#485 D1a, D1b, D1d,
D4) adds four new pieces of process-local state behind the protect()
hot path:

- ``baldur.metrics.recorders.circuit_breaker._cb_recorder`` +
  ``_cb_recorder_init_failed`` (D1a / G2)
- ``baldur.services.retry_handler.sinks._baldur_pro_dlq_resolver`` +
  ``_baldur_pro_dlq_unavailable`` (D1b / G4)
- ``baldur.metrics.event_handlers._metrics_init_failed`` +
  surrounding caches (D1d / G7)
- ``baldur.services.dlq_capture.overflow._overflow_check_counter`` +
  ``_overflow_last_ratio`` (overflow check relocated to OSS)

D7 wires each piece into ``reset_protect_caches()`` so a single reset
call cascades to every new sticky / counter / cached-ref. This test
locks in that invariant — adding new sticky state without extending
the chain would silently leak across test boundaries, the failure mode
this round was designed to prevent.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.protect_facade import reset_protect_caches


@pytest.fixture(autouse=True)
def _restore_module_state():
    """After each test, ensure module state is clean for the next test."""
    yield
    reset_protect_caches()


class TestResetProtectCachesD7ChainContract:
    """Each piece of #485 process-local state is invalidated by a single
    ``reset_protect_caches()`` call."""

    def test_chain_calls_reset_blocked_recorder(self):
        """D1a — CB blocked recorder reset is in the chain."""
        with patch(
            "baldur.metrics.recorders.circuit_breaker.reset_blocked_recorder",
            autospec=True,
        ) as mock_reset:
            reset_protect_caches()

        mock_reset.assert_called_once()

    # test_chain_calls_reset_baldur_pro_dlq_resolver: removed by 518 batch (a).
    # The DLQSink sticky-flag resolver (D1b / G4) was retired when
    # ``baldur.dlq.helpers.store_to_dlq`` took over fail-open responsibility;
    # there is no longer a ``_reset_baldur_pro_dlq_resolver`` to wire into the
    # reset chain.

    def test_chain_calls_reset_event_handler_cache(self):
        """D1d — DLQ event-handler cache reset is in the chain."""
        with patch(
            "baldur.metrics.event_handlers.reset_event_handler_cache",
            autospec=True,
        ) as mock_reset:
            reset_protect_caches()

        mock_reset.assert_called_once()

    def test_chain_calls_reset_overflow_state(self):
        """The overflow periodic-N counter reset is in the chain.

        The overflow check + its ``reset_overflow_state`` now live in the OSS
        ``baldur.services.dlq_capture`` package (unconditional import — no
        PRO gating), so the chain resolves it there.
        """
        with patch(
            "baldur.services.dlq_capture.reset_overflow_state",
            autospec=True,
        ) as mock_reset:
            reset_protect_caches()

        mock_reset.assert_called_once()

    def test_chain_calls_reset_dlq_capture_service(self):
        """The OSS DLQ capture singleton reset is in the chain."""
        with patch(
            "baldur.services.dlq_capture.reset_dlq_capture_service",
            autospec=True,
        ) as mock_reset:
            reset_protect_caches()

        mock_reset.assert_called_once()

    # test_chain_clears_all_485_sticky_flags: removed by 518 batch (a).
    # The DLQSink sticky flag (``sinks_module._baldur_pro_dlq_unavailable``)
    # was retired alongside ``_reset_baldur_pro_dlq_resolver`` — see the note
    # above test_chain_calls_reset_event_handler_cache. The remaining
    # individual chain tests (CB recorder, event-handler cache, overflow
    # state) still verify the reset-cascade invariant for the surviving
    # sticky flags one at a time.
