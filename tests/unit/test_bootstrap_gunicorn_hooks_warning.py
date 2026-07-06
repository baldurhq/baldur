"""Bootstrap deferred-Timer WARNING tests (impl 471 D2, D5, D6, D7).

Coverage:
- Under gunicorn (SERVER_SOFTWARE) AND hooks-module absent → ONE
  ``baldur.gunicorn_hooks_not_installed`` WARNING is emitted
- Under gunicorn AND hooks-module already imported → silent
- NOT under gunicorn → silent (returns early)

Timer mechanics: ``threading.Timer(delay, _check)`` is replaced by a
synchronous-callback FakeTimer (``UNIT_TEST_GUIDELINES.md`` §6.5.5–6.5.7)
so the test does not sleep ``delay`` seconds and is xdist-safe.
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

import baldur.bootstrap as bootstrap_module

# =============================================================================
# FakeTimer — synchronous callback, identical surface to threading.Timer
# =============================================================================


class _FakeTimer:
    """threading.Timer stub that runs the callback inside ``.start()``.

    Matches the real Timer's constructor signature (``interval, function``)
    and the ``daemon`` attribute that bootstrap sets immediately after
    construction.
    """

    def __init__(self, interval, function, args=None, kwargs=None):
        self._interval = interval
        self._function = function
        self._args = args or ()
        self._kwargs = kwargs or {}
        self.daemon = False
        self.started = False

    def start(self):
        self.started = True
        # Synchronous: run immediately. Tests assert post-start state.
        self._function(*self._args, **self._kwargs)


@pytest.fixture
def fake_timer(monkeypatch):
    """Patch ``threading.Timer`` (looked up via ``import threading``) inside
    bootstrap to the synchronous FakeTimer."""
    monkeypatch.setattr(threading, "Timer", _FakeTimer)
    return


# =============================================================================
# Detection-signal matrix
# =============================================================================


class TestGunicornHooksMissingWarningBehavior:
    """``_schedule_gunicorn_hooks_check`` Timer callback decision matrix."""

    def test_warning_emitted_when_under_gunicorn_and_hooks_absent(
        self, fake_timer, monkeypatch
    ):
        """Under gunicorn + hooks-module not in sys.modules → WARNING."""
        monkeypatch.delitem(
            sys.modules, "baldur.adapters.gunicorn.hooks", raising=False
        )

        with (
            patch(
                "baldur.core.process_utils.is_under_gunicorn",
                return_value=True,
            ),
            patch.object(bootstrap_module, "logger") as mock_logger,
        ):
            bootstrap_module._schedule_gunicorn_hooks_check()

        warn_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "baldur.gunicorn_hooks_not_installed"
        ]
        assert len(warn_calls) == 1, (
            f"expected exactly one hooks-not-installed WARNING, got "
            f"{mock_logger.warning.call_args_list}"
        )

    def test_silent_when_under_gunicorn_but_hooks_present(
        self, fake_timer, monkeypatch
    ):
        """Hooks module already in sys.modules → no WARNING."""
        stub = MagicMock()
        monkeypatch.setitem(sys.modules, "baldur.adapters.gunicorn.hooks", stub)

        with (
            patch(
                "baldur.core.process_utils.is_under_gunicorn",
                return_value=True,
            ),
            patch.object(bootstrap_module, "logger") as mock_logger,
        ):
            bootstrap_module._schedule_gunicorn_hooks_check()

        warn_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "baldur.gunicorn_hooks_not_installed"
        ]
        assert warn_calls == []

    def test_silent_when_not_under_gunicorn(self, fake_timer, monkeypatch):
        """Not under gunicorn → no WARNING regardless of sys.modules state."""
        # Even if the hooks module is absent, non-gunicorn deployments must
        # not produce the warning.
        monkeypatch.delitem(
            sys.modules, "baldur.adapters.gunicorn.hooks", raising=False
        )

        with (
            patch(
                "baldur.core.process_utils.is_under_gunicorn",
                return_value=False,
            ),
            patch.object(bootstrap_module, "logger") as mock_logger,
        ):
            bootstrap_module._schedule_gunicorn_hooks_check()

        warn_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and call.args[0] == "baldur.gunicorn_hooks_not_installed"
        ]
        assert warn_calls == []

    def test_timer_is_daemon_so_it_does_not_block_interpreter_exit(self, monkeypatch):
        """The scheduled Timer must be daemon=True (471 D7 mandate).

        Captures the Timer instance instead of running its callback so we can
        assert post-construction attributes without firing the WARNING.
        """
        captured: list[_FakeTimer] = []

        class _CapturingTimer(_FakeTimer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured.append(self)

            def start(self):
                # Skip running the callback so we can assert daemon=True
                # after bootstrap finished setting attributes.
                self.started = True

        monkeypatch.setattr(threading, "Timer", _CapturingTimer)

        with patch(
            "baldur.core.process_utils.is_under_gunicorn",
            return_value=False,
        ):
            bootstrap_module._schedule_gunicorn_hooks_check()

        assert len(captured) == 1
        assert captured[0].daemon is True
        assert captured[0].started is True
