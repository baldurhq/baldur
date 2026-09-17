"""A Celery worker's own logging setup owns the root logger.

The Celery adapter used to connect a no-op ``setup_logging`` receiver so that
Celery's worker boot would configure nothing and baldur's stdout JSON handler
would survive — which also meant ``-l INFO`` was ignored, ``celery.task`` sat
at WARNING and no Celery handler existed. That receiver is gone. The boot
order is ``worker_init`` (baldur's ``init()``, which installs its handler on
an empty root) and then Celery's ``setup_logging_subsystem``, which under the
default ``worker_hijack_root_logger=True`` replaces baldur's boot-time handler
with its own stderr handler at the ``-l`` level. baldur's events then reach
Celery's handler by propagation, and the posture line's INFO floor passes
because Celery's handler carries no level of its own.

Driven in-process: the real ``configure_structlog()`` against a cleared root,
then the real ``setup_logging_subsystem`` on a fresh ``Celery()``. Celery
keeps its "already set up" latch on the class (``Logging._setup``), so the
sandbox resets it between cases and restores every logger it touches.
"""

from __future__ import annotations

import logging
import os
import weakref
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

pytest.importorskip("celery")

from celery import Celery  # noqa: E402
from celery.app.log import Logging  # noqa: E402
from celery.signals import setup_logging  # noqa: E402
from celery.utils.log import get_multiprocessing_logger  # noqa: E402

from baldur.observability.structlog_config import (  # noqa: E402
    POSTURE_LOGGER_NAME,
    _BaldurStreamHandler,
    configure_structlog,
    reset_structlog_config,
)

_CELERY_LOGGER_NAMES = ("celery", "celery.task", "celery.redirected")
_MP_FORK_ENV_KEYS = ("_MP_FORK_LOGLEVEL_", "_MP_FORK_LOGFILE_", "_MP_FORK_LOGFORMAT_")


@pytest.fixture
def celery_logging_sandbox(monkeypatch) -> Iterator[None]:
    """Undo everything Celery's setup touches besides the root: its own
    loggers, the multiprocessing logger, the class-level latch and the
    fork-environment variables.

    ``BALDUR_TEST_LOG_LEVEL`` is cleared so ``configure_structlog()`` takes
    its production branch.
    """
    monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
    monkeypatch.delenv("BALDUR_LOG_LEVEL", raising=False)
    for key in _MP_FORK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    saved_loggers = {
        name: (lg.handlers, lg.level, lg.propagate)
        for name, lg in (
            (name, logging.getLogger(name)) for name in _CELERY_LOGGER_NAMES
        )
    }
    mp_logger = get_multiprocessing_logger()
    saved_mp = (mp_logger.handlers, mp_logger.level)
    saved_latch = Logging._setup
    Logging._setup = False
    reset_structlog_config()
    try:
        yield
    finally:
        reset_structlog_config()
        Logging._setup = saved_latch
        for name, (handlers, level, propagate) in saved_loggers.items():
            lg = logging.getLogger(name)
            lg.handlers = handlers
            lg.setLevel(level)
            lg.propagate = propagate
        mp_logger.handlers, level = saved_mp
        mp_logger.setLevel(level)
        for key in _MP_FORK_ENV_KEYS:
            os.environ.pop(key, None)


@contextmanager
def empty_root() -> Iterator[logging.Logger]:
    """The root as a worker main process finds it at ``worker_init``: no
    handler. Entered inside the test body, because pytest adds its capture
    handlers to the root at the start of the call phase and would otherwise
    make ``basicConfig`` semantics see a configured host; the original list
    object is put back so the capture plugin's teardown still finds them.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers
    saved_level = root.level
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def _baldur_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if isinstance(h, _BaldurStreamHandler)]


class TestCeleryWorkerOwnsLogging:
    """Boot order: baldur's ``init()`` first (empty root), Celery's setup second."""

    def test_celerys_setup_replaces_baldurs_boot_time_handler(
        self, celery_logging_sandbox
    ):
        """Default ``worker_hijack_root_logger=True``: the root carries
        Celery's handler only, at the ``-l`` level."""
        with empty_root() as root:
            configure_structlog()
            assert len(_baldur_handlers(root)) == 1, "init() installs on an empty root"

            app = Celery("baldur-worker-owns-logging")
            app.log.setup_logging_subsystem(loglevel=logging.INFO)

            assert _baldur_handlers(root) == []
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0], logging.StreamHandler)
            assert root.level == logging.INFO

    def test_the_posture_line_passes_celerys_handler(self, celery_logging_sandbox):
        """Celery's handler has no level of its own, and the posture logger's
        INFO floor is a write to baldur's own logger, so the one startup line
        still reaches the worker's log."""
        with empty_root() as root:
            configure_structlog()

            app = Celery("baldur-worker-owns-logging")
            app.log.setup_logging_subsystem(loglevel=logging.WARNING)

            posture = logging.getLogger(POSTURE_LOGGER_NAME)
            assert posture.isEnabledFor(logging.INFO) is True
            assert root.handlers[0].level == logging.NOTSET

    def test_hijack_off_keeps_baldurs_handler_at_the_worker_level(
        self, celery_logging_sandbox
    ):
        """``worker_hijack_root_logger=False``: Celery adds no root handler
        of its own (it sees one already) and only sets the level — the
        documented way to keep JSON on a worker."""
        with empty_root() as root:
            configure_structlog()
            boot_handler = _baldur_handlers(root)[0]

            app = Celery("baldur-worker-keeps-json")
            app.conf.worker_hijack_root_logger = False
            app.log.setup_logging_subsystem(loglevel=logging.INFO)

            assert root.handlers == [boot_handler]
            assert root.level == logging.INFO

    def test_task_loggers_get_celerys_own_handler(self, celery_logging_sandbox):
        """``-l`` is Celery's contract: ``celery.task`` gets Celery's stderr
        handler at the worker level and stops propagating, as on a worker
        without baldur."""
        with empty_root():
            configure_structlog()

            app = Celery("baldur-worker-owns-logging")
            app.log.setup_logging_subsystem(loglevel=logging.INFO)

            task_logger = logging.getLogger("celery.task")
            stream_handlers = [
                h
                for h in task_logger.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.NullHandler)
            ]
            assert len(stream_handlers) == 1
            assert task_logger.level == logging.INFO
            assert task_logger.propagate == 0


class TestNoBaldurSetupLoggingReceiver:
    """The receiver that blocked Celery's setup is gone from the tree."""

    def test_setup_logging_has_no_baldur_receiver(self):
        """Celery stores a weak reference unless connected with ``weak=False``;
        both shapes resolve to the function whose module is checked."""
        receivers = [
            receiver() if isinstance(receiver, weakref.ReferenceType) else receiver
            for _, receiver in setup_logging.receivers
        ]
        modules = [getattr(r, "__module__", "") or "" for r in receivers]

        assert not [m for m in modules if m.startswith("baldur")], modules

    def test_the_adapter_ships_no_signal_handlers_module(self):
        import importlib.util

        assert (
            importlib.util.find_spec("baldur.adapters.celery.signal_handlers") is None
        )
