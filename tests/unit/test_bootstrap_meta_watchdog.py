"""558 D4 — bootstrap._start_meta_watchdog_if_enabled unit tests.

Framework-independent start path for the Slice-A Meta-Watchdog, called from
``baldur.init()`` so Flask / FastAPI / plain-Python CLI get the same wiring
Django gets. Branch coverage:

- ``BALDUR_META_WATCHDOG_AUTOSTART`` in {0,false,no} → skip (test escape hatch).
- Gunicorn master (``SERVER_SOFTWARE`` contains "gunicorn", no ``GUNICORN_WORKER``)
  → skip (threads die after fork(); ``init()`` is not re-run in workers).
- ``meta_watchdog.enabled=False`` → skip.
- ``selfhealer_watchdog.safe_get()`` is None (OSS / PRO absent) → no-op.
- Otherwise → ``watchdog.start()`` (idempotent).
- ImportError / runtime Exception → swallowed (init() must continue).

These tests patch every collaborator; they do NOT require ``baldur_pro`` — the
helper itself is OSS surface and no-ops without the PRO watchdog.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.bootstrap import _start_meta_watchdog_if_enabled
from baldur.factory import ProviderRegistry


@pytest.fixture
def non_gunicorn_autostart(monkeypatch):
    """Pass the autostart + non-master gates so the body runs.

    The unit-test process sets ``BALDUR_META_WATCHDOG_AUTOSTART=0`` globally
    (tests/conftest.py); re-enable it here and strip any gunicorn env so
    ``is_gunicorn_master()`` returns False.
    """
    monkeypatch.setenv("BALDUR_META_WATCHDOG_AUTOSTART", "1")
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


class TestStartMetaWatchdogIfEnabled:
    """558 D4: framework-independent watchdog start gating."""

    @pytest.mark.parametrize("disabled_value", ["0", "false", "no"])
    def test_autostart_disabled_returns_before_master_check(
        self, monkeypatch, disabled_value
    ):
        """An autostart escape-hatch value returns before any further work."""
        monkeypatch.setenv("BALDUR_META_WATCHDOG_AUTOSTART", disabled_value)

        with patch(
            "baldur.core.process_utils.is_gunicorn_master", autospec=True
        ) as is_master:
            _start_meta_watchdog_if_enabled()

        is_master.assert_not_called()

    def test_gunicorn_master_skips_start(self, monkeypatch):
        """In the Gunicorn master the watchdog start is skipped (fork-safety)."""
        monkeypatch.setenv("BALDUR_META_WATCHDOG_AUTOSTART", "1")
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with patch(
            "baldur.settings.meta_watchdog.get_meta_watchdog_settings", autospec=True
        ) as get_settings:
            _start_meta_watchdog_if_enabled()

        # Master-skip happens before the settings lookup.
        get_settings.assert_not_called()

    def test_disabled_settings_skips_watchdog_resolution(self, non_gunicorn_autostart):
        """meta_watchdog.enabled=False stops before resolving the watchdog."""
        registry = MagicMock()
        with (
            patch(
                "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                return_value=MagicMock(enabled=False),
            ),
            patch.object(ProviderRegistry, "selfhealer_watchdog", registry),
        ):
            _start_meta_watchdog_if_enabled()

        registry.safe_get.assert_not_called()

    def test_watchdog_unavailable_noops(self, non_gunicorn_autostart):
        """safe_get()=None (OSS / PRO absent) is a clean no-op, not an error."""
        registry = MagicMock()
        registry.safe_get.return_value = None
        with (
            patch(
                "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                return_value=MagicMock(enabled=True),
            ),
            patch.object(ProviderRegistry, "selfhealer_watchdog", registry),
        ):
            _start_meta_watchdog_if_enabled()  # must not raise

        registry.safe_get.assert_called_once()

    def test_enabled_starts_watchdog(self, non_gunicorn_autostart):
        """enabled + available watchdog → watchdog.start() is invoked."""
        watchdog = MagicMock()
        registry = MagicMock()
        registry.safe_get.return_value = watchdog
        with (
            patch(
                "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                return_value=MagicMock(enabled=True),
            ),
            patch.object(ProviderRegistry, "selfhealer_watchdog", registry),
        ):
            _start_meta_watchdog_if_enabled()

        watchdog.start.assert_called_once()

    def test_import_error_swallowed(self, non_gunicorn_autostart):
        """An ImportError inside the body is swallowed (init() continues)."""
        with patch(
            "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
            side_effect=ImportError("baldur_pro missing"),
        ):
            _start_meta_watchdog_if_enabled()  # must not raise

    def test_runtime_error_swallowed(self, non_gunicorn_autostart):
        """A watchdog.start() crash is swallowed (init() continues)."""
        watchdog = MagicMock()
        watchdog.start.side_effect = RuntimeError("thread spawn boom")
        registry = MagicMock()
        registry.safe_get.return_value = watchdog
        with (
            patch(
                "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                return_value=MagicMock(enabled=True),
            ),
            patch.object(ProviderRegistry, "selfhealer_watchdog", registry),
        ):
            _start_meta_watchdog_if_enabled()  # must not raise
