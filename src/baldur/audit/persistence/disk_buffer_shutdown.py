"""Graceful shutdown support for DiskPersistentBuffer.

Registers atexit and signal handlers to ensure the group-commit
buffer is flushed and LMDB is synced before process termination.

Two-step lifecycle: the signal-time step is a non-destructive
flush + sync (``_flush_disk_buffer``) so audit writes during the
graceful-shutdown drain window keep persisting; the destructive
close + instance null (``_shutdown_disk_buffer``) is drain-positioned
— it runs as the final step of ``graceful_shutdown_audit_system``
and via atexit.
"""

from __future__ import annotations

import atexit
import logging
import signal
import sys
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

__all__ = [
    "_register_signal_handlers",
    "_shutdown_disk_buffer",
    "register_disk_buffer_shutdown",
]

logger = structlog.get_logger()

_disk_buffer_instance: DiskPersistentBuffer | None = None


def register_disk_buffer_shutdown(buffer: DiskPersistentBuffer) -> None:
    """Register shutdown handlers for a DiskPersistentBuffer.

    Hooks into SIGTERM, SIGINT, and atexit to safely flush and close
    the buffer on process termination.

    Args:
        buffer: The buffer instance to shut down.
    """
    global _disk_buffer_instance
    _disk_buffer_instance = buffer

    # atexit handler
    atexit.register(_shutdown_disk_buffer)

    # Signal handlers (chain existing handlers)
    _register_signal_handlers()

    logger.debug("disk_buffer.shutdown_handlers_registered")


def _register_signal_handlers() -> None:
    """Register SIGTERM / SIGINT handlers.

    Skipped under gunicorn (master OR worker) — gunicorn manages
    signal lifecycle, and disk-buffer cleanup runs via the
    ``worker_exit`` hook in ``baldur.adapters.gunicorn.hooks``
    (atexit also fires there). ``is_under_gunicorn()`` is used
    instead of ``is_gunicorn_worker()`` because the latter relies on
    ``GUNICORN_WORKER=1``, which is set by ``post_worker_init`` AFTER
    ``baldur.init()`` runs — in worker pre-post_worker_init, the
    chained SIGTERM handler would still install (chaining preserves
    gunicorn's drain) but emit confusing logs and reduce signal-
    delivery determinism.
    """
    if sys.platform == "win32":
        # Windows does not support SIGTERM; rely on atexit only
        return

    from baldur.core.process_utils import is_under_gunicorn

    if is_under_gunicorn():
        return

    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)

    def _sigterm_handler(signum: int, frame: Any) -> None:
        """SIGTERM handler."""
        logger.info("disk_buffer.sigterm_received_initiating_shutdown")
        _flush_disk_buffer()
        # Chain original handler
        if callable(original_sigterm):
            original_sigterm(signum, frame)

    def _sigint_handler(signum: int, frame: Any) -> None:
        """SIGINT handler."""
        logger.info("disk_buffer.sigint_received_initiating_shutdown")
        _flush_disk_buffer()
        # Chain original handler
        if callable(original_sigint):
            original_sigint(signum, frame)

    # Markers consumed by the coordinator's disposition chain-walk (597 D2/D8):
    # classification follows these to the effective tail, so a buffer handler
    # registered before the coordinator cannot flip the chain/defer verdict.
    _sigterm_handler._baldur_chained_original = original_sigterm  # type: ignore[attr-defined]
    _sigint_handler._baldur_chained_original = original_sigint  # type: ignore[attr-defined]

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigint_handler)


def _flush_disk_buffer() -> int:
    """Non-destructive flush: group-commit flush + LMDB fsync.

    Signal-time step — leaves the buffer open and the module instance
    set, so audit writes arriving during the graceful-shutdown drain
    window keep persisting. The destructive close is drain-positioned
    (see ``_shutdown_disk_buffer``). Exceptions are swallowed (signal
    context).

    Returns:
        Number of pending group-commit entries flushed; 0 on no-op,
        swallowed failure, or group-commit-disabled config.
    """
    if _disk_buffer_instance is None:
        return 0

    started = time.monotonic()
    flushed_count = 0
    try:
        # 1. Force flush group commit buffer
        if _disk_buffer_instance._settings.group_commit_enabled:
            if _disk_buffer_instance._group_writer is not None:
                flushed_count = _disk_buffer_instance._group_writer.pending_count
            _disk_buffer_instance.flush_group_commit()

        # 2. LMDB fsync (data safety guarantee)
        if _disk_buffer_instance._env:
            _disk_buffer_instance._env.sync()
    except Exception:
        return 0

    if flushed_count > 0:
        # Drain-window forensic record; silent at 0 to avoid log spam
        # on empty buffers.
        logger.info(
            "disk_buffer.pending_flushed",
            flushed_count=flushed_count,
            duration_ms=(time.monotonic() - started) * 1000,
        )
    return flushed_count


def _shutdown_disk_buffer() -> bool:
    """Full teardown sequence.

    Steps:
    1. Flush the group-commit buffer + LMDB fsync (signal-path flush).
    2. Close resources.
    3. Null the module instance.

    Runs as the final step of ``graceful_shutdown_audit_system``
    (drain-positioned) and via atexit.

    Returns:
        ``True`` on success (or no-op), ``False`` when the close
        failed — observable by the lifecycle step without changing
        atexit's swallow semantics.
    """
    global _disk_buffer_instance

    if _disk_buffer_instance is None:
        return True

    # At atexit the logging stream may already be closed; suppress
    # "--- Logging error ---" output.
    original_raise = logging.raiseExceptions
    success = True
    try:
        logging.raiseExceptions = False

        try:
            # 1. Flush group commit buffer + fsync (swallows internally)
            _flush_disk_buffer()

            # 2. Close buffer
            _disk_buffer_instance.close()

        except Exception:
            success = False

        finally:
            _disk_buffer_instance = None
    finally:
        logging.raiseExceptions = original_raise
    return success
