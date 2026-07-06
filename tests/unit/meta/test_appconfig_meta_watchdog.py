"""Meta-Watchdog framework-agnostic start-helper tests.

The watchdog start logic lives in the framework-agnostic bootstrap helper
``baldur.bootstrap._start_meta_watchdog_if_enabled`` (started for every adapter
via ``start_background_workers()``). These tests assert that the moved wiring
(enable gate, ProviderRegistry resolution, ``watchdog.start()``, fail-soft
handling, gunicorn-master skip) lives in that helper.
"""

from __future__ import annotations

import inspect


def _bootstrap_helper_source() -> str:
    """Source of just the framework-agnostic watchdog start helper."""
    from baldur.bootstrap import _start_meta_watchdog_if_enabled

    return inspect.getsource(_start_meta_watchdog_if_enabled)


class TestBootstrapHelperWiring:
    """The watchdog start wiring lives in _start_meta_watchdog_if_enabled."""

    def test_helper_resolves_selfhealer_watchdog_via_registry(self):
        """selfhealer_watchdog is resolved via the OSS->PRO boundary slot."""
        source = _bootstrap_helper_source()
        assert "selfhealer_watchdog" in source
        assert "ProviderRegistry" in source

    def test_helper_uses_canonical_enable_gate(self):
        """The canonical Pydantic enable gate replaces the old env/Django checks."""
        assert "get_meta_watchdog_settings" in _bootstrap_helper_source()

    def test_helper_calls_watchdog_start(self):
        """The helper starts the watchdog."""
        assert "watchdog.start()" in _bootstrap_helper_source()

    def test_helper_skips_gunicorn_master(self):
        """The helper skips the start in the gunicorn master (fork-safety)."""
        assert "is_gunicorn_master" in _bootstrap_helper_source()

    def test_helper_is_fail_soft(self):
        """The helper swallows ImportError and generic Exception (fail-soft)."""
        source = _bootstrap_helper_source()
        assert "ImportError" in source
        assert "except Exception" in source
