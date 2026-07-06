"""Unit tests for RedisAuditBuffer signal-hook chaining.

The buffer's SIGTERM/SIGINT hooks CHAIN the previously installed
disposition instead of replacing it: the fallback buffer is flushed
first, then the prior handler runs, and the closure carries the
``_baldur_chained_original`` marker consumed by the coordinator's
disposition chain-walk.
"""
# Replace → chain conversion coverage: 597 D7 (pattern precedent:
# audit/persistence/disk_buffer_shutdown).

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

from baldur.adapters.audit.redis_buffer import RedisAuditBuffer


def _bare_buffer() -> RedisAuditBuffer:
    """Build a buffer without Redis: the hook surface only touches
    ``_graceful_shutdown`` and ``_shutdown_registered``."""
    buffer = RedisAuditBuffer.__new__(RedisAuditBuffer)
    buffer._shutdown_registered = False
    buffer._graceful_shutdown = MagicMock()
    return buffer


class TestRedisBufferChainedHandlerBehavior:
    """_make_chained_signal_handler / _register_shutdown_hooks chaining (597 D7)."""

    def test_chained_handler_flushes_before_invoking_original(self):
        """Chain order: fallback-buffer flush runs BEFORE the prior handler."""
        # Given
        order: list[str] = []
        buffer = RedisAuditBuffer.__new__(RedisAuditBuffer)
        buffer._graceful_shutdown = lambda: order.append("flush")

        def _original(signum, frame):
            order.append("original")

        handler = buffer._make_chained_signal_handler(_original)

        # When
        handler(signal.SIGTERM, None)

        # Then
        assert order == ["flush", "original"]

    def test_chained_handler_forwards_signum_and_frame_to_original(self):
        """The captured handler receives the exact (signum, frame) delivery."""
        # Given
        received: list[tuple] = []
        buffer = _bare_buffer()

        def _original(signum, frame):
            received.append((signum, frame))

        handler = buffer._make_chained_signal_handler(_original)
        sentinel_frame = object()

        # When
        handler(signal.SIGINT, sentinel_frame)

        # Then
        assert received == [(signal.SIGINT, sentinel_frame)]

    def test_chained_handler_with_non_callable_original_only_flushes(self):
        """A SIG_DFL original is not invoked — the handler flushes and returns."""
        buffer = _bare_buffer()
        handler = buffer._make_chained_signal_handler(signal.SIG_DFL)

        handler(signal.SIGTERM, None)

        buffer._graceful_shutdown.assert_called_once_with()

    def test_chained_handler_carries_chain_walk_marker(self):
        """The closure exposes the captured original via the chain-walk marker."""
        buffer = _bare_buffer()
        original = signal.default_int_handler

        handler = buffer._make_chained_signal_handler(original)

        assert handler._baldur_chained_original is original

    def test_register_shutdown_hooks_chains_captured_disposition_per_signal(self):
        """Registration captures and chains each signal's own prior disposition."""

        # Given — distinct pre-installed dispositions per signal
        def _prior_sigterm(signum, frame):
            pass

        def _prior_sigint(signum, frame):
            pass

        originals = {signal.SIGTERM: _prior_sigterm, signal.SIGINT: _prior_sigint}
        buffer = _bare_buffer()

        # When — outside gunicorn, with no real OS handler installed
        with (
            patch(
                "baldur.core.process_utils.is_under_gunicorn",
                return_value=False,
            ),
            patch("atexit.register"),
            patch("signal.getsignal", side_effect=lambda sig: originals[sig]),
            patch("signal.signal") as mock_signal,
        ):
            buffer._register_shutdown_hooks()

        # Then — a chained handler per signal, each carrying its own original
        installed = {args[0][0]: args[0][1] for args in mock_signal.call_args_list}
        assert set(installed) == {signal.SIGTERM, signal.SIGINT}
        for sig, handler in installed.items():
            assert handler._baldur_chained_original is originals[sig]
        assert buffer._shutdown_registered is True

    def test_register_shutdown_hooks_second_call_is_noop(self):
        """The once-guard prevents stacking a second chain link per process."""
        buffer = _bare_buffer()
        buffer._shutdown_registered = True

        with (
            patch("atexit.register") as mock_atexit,
            patch("signal.signal") as mock_signal,
        ):
            buffer._register_shutdown_hooks()

        mock_atexit.assert_not_called()
        mock_signal.assert_not_called()
