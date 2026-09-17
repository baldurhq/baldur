"""752 D0b — the posture line survives the default WARNING root level.

The root logger defaults to WARNING and the component-level map carries no
bootstrap namespace, so a one-line INFO summary of what a process is running
on would never reach a handler — the same reason ``baldur.startup_report``
is invisible on a default run. The announcement therefore has its own logger
name, floored at INFO by ``configure_structlog()``.

Visibility is asserted as an **effective level**, never as a captured
record: the session fixture owns the root level and ``caplog.set_level``
overrides it, so a record count proves the call happened and never that a
user would see it. That is exactly how an earlier design's invisible posture
line passed its own tests.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from baldur.observability.structlog_config import (
    _POSTURE_FLOOR_LEVEL,
    POSTURE_LOGGER_NAME,
    configure_structlog,
    reset_structlog_config,
)


@pytest.fixture(autouse=True)
def isolated_structlog_configuration():
    """Each case runs the real ``configure_structlog()`` from a clean slate."""
    from baldur.settings.logging_settings import reset_logging_settings

    reset_logging_settings()
    reset_structlog_config()
    root_level = logging.getLogger().level
    yield
    reset_logging_settings()
    reset_structlog_config()
    logging.getLogger().setLevel(root_level)


@pytest.fixture
def operator_log_level(monkeypatch):
    """Drive ``BALDUR_LOG_LEVEL`` and let the production branch run.

    ``BALDUR_TEST_LOG_LEVEL`` is cleared so the root level comes from the
    operator's own variable rather than the suite's, which is what makes the
    "an operator who set a level wins" half falsifiable.
    """

    def _set(level: str | None) -> None:
        monkeypatch.delenv("BALDUR_TEST_LOG_LEVEL", raising=False)
        if level is None:
            monkeypatch.delenv("BALDUR_LOG_LEVEL", raising=False)
        else:
            monkeypatch.setenv("BALDUR_LOG_LEVEL", level)

    return _set


@contextmanager
def host_configured_root(level: int) -> Iterator[logging.Logger]:
    """A root logger an application configured: one handler of its own at
    ``level``. Installed inside the test body because pytest adds its own
    capture handlers at the start of the call phase."""
    root = logging.getLogger()
    saved_handlers = root.handlers
    saved_level = root.level
    root.handlers = [logging.StreamHandler(io.StringIO())]
    root.setLevel(level)
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


class TestPostureLoggerFloorContract:
    """The floor, the operator override, and the reset that undoes both."""

    def test_the_posture_logger_name_is_the_documented_one(self):
        assert POSTURE_LOGGER_NAME == "baldur.posture"

    def test_no_operator_level_floors_the_posture_logger_at_info(
        self, operator_log_level
    ):
        """The zero-config run: the line is actually visible."""
        operator_log_level(None)

        configure_structlog()

        logger = logging.getLogger(POSTURE_LOGGER_NAME)
        assert logger.getEffectiveLevel() <= logging.INFO
        assert logger.isEnabledFor(logging.INFO) is True

    def test_the_floor_does_not_lift_the_root_level(self, operator_log_level):
        """Only the posture logger is floored — the flood stays filtered."""
        operator_log_level(None)

        configure_structlog()

        assert logging.getLogger().getEffectiveLevel() > logging.INFO

    def test_an_operator_chosen_level_silences_the_posture_line(
        self, operator_log_level
    ):
        """``BALDUR_LOG_LEVEL=ERROR`` means ERROR, posture line included."""
        operator_log_level("ERROR")

        configure_structlog()

        logger = logging.getLogger(POSTURE_LOGGER_NAME)
        assert logger.level == logging.NOTSET, "the floor must not be applied"
        assert logger.isEnabledFor(logging.INFO) is False

    def test_an_operator_chosen_debug_level_is_not_raised_to_info(
        self, operator_log_level
    ):
        """The floor is a floor, not an override — DEBUG still reaches here."""
        operator_log_level("DEBUG")

        configure_structlog()

        logger = logging.getLogger(POSTURE_LOGGER_NAME)
        assert logger.level == logging.NOTSET
        assert logger.isEnabledFor(logging.DEBUG) is True

    def test_reset_restores_the_posture_logger_to_notset(self, operator_log_level):
        """Otherwise the floor leaks into every later test in the worker."""
        operator_log_level(None)
        configure_structlog()
        assert logging.getLogger(POSTURE_LOGGER_NAME).level == logging.INFO

        reset_structlog_config()

        assert logging.getLogger(POSTURE_LOGGER_NAME).level == logging.NOTSET


class TestPostureFloorInsideAConfiguredHostBehavior:
    """The floor is a write to baldur's own logger, so a host that configured
    its logging at WARNING still gets the one posture line — and its root
    level is never touched, floor or no floor."""

    def test_the_floor_applies_and_the_host_root_level_is_untouched(
        self, operator_log_level
    ):
        operator_log_level(None)

        with host_configured_root(logging.ERROR) as root:
            configure_structlog()

            assert root.level == logging.ERROR
            posture = logging.getLogger(POSTURE_LOGGER_NAME)
            assert posture.level == _POSTURE_FLOOR_LEVEL
            assert posture.isEnabledFor(_POSTURE_FLOOR_LEVEL) is True

    def test_an_operator_level_skips_the_floor_and_leaves_the_host_root_alone(
        self, operator_log_level
    ):
        operator_log_level("ERROR")

        with host_configured_root(logging.INFO) as root:
            configure_structlog()

            assert root.level == logging.INFO
            posture = logging.getLogger(POSTURE_LOGGER_NAME)
            assert posture.level == logging.NOTSET, "the floor must not be applied"
            assert posture.isEnabledFor(logging.INFO) is False
