"""566 D1/D3/D4/D8 — ``_install_idempotency_gate`` init-time singleton install.

Installs the unified idempotency-gate singleton with a registry-resolved cache
at ``baldur.init()`` so its direct consumers (``replay_service``, PRO governance
``IdempotencyCheck``) perform real dedup instead of the ``cache=None`` silent
no-op.

Behavior matrix:
- development (non-test) runtime + cache wired → singleton becomes cache-backed
  (D1/D2).
- test-mode runtime → no-op skip, singleton stays ``cache=None`` (D3).
- re-install after ``reset_init_state()`` → fresh cache-backed singleton (D4).
- success path emits the ``idempotency_gate.installed`` breadcrumb (D8); the
  test-mode skip path emits nothing.

Lives at ``tests/unit/test_bootstrap_idempotency_gate_install.py`` following
the ``tests/unit/test_bootstrap_*.py`` convention (``baldur.bootstrap`` is a
top-level module without a parent package).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur import bootstrap
from baldur.core.idempotency_gate import (
    get_idempotency_gate,
    reset_idempotency_gate,
)

_INSTALLED_EVENT = "idempotency_gate.installed"

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_bootstrap_and_runtime():
    """Each test starts and ends with clean bootstrap + runtime + gate state."""
    bootstrap.reset_init_state()
    reset_idempotency_gate()
    yield
    reset_idempotency_gate()
    bootstrap.reset_init_state()


def _seed_runtime(monkeypatch, *, test_mode: bool) -> None:
    """Rebuild the runtime as development (non-prod), test-mode on/off.

    Mirrors ``test_bootstrap_idempotency_cache_validation._seed_env``: the
    runtime eager-reads ``BALDUR_ENVIRONMENT`` / ``BALDUR_TEST_MODE`` at
    construction, so the env must be set *before* ``reset_init_state()``.
    """
    monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
    if test_mode:
        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
    else:
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
    bootstrap.reset_init_state()  # rebuild runtime with the new env


# =============================================================================
# TestInstallIdempotencyGateBehavior
# =============================================================================


class TestInstallIdempotencyGateBehavior:
    """566 D1/D3/D4/D8 — observable side-effect of ``_install_idempotency_gate``."""

    def test_development_runtime_installs_cache_backed_singleton(self, monkeypatch):
        """Development runtime → singleton is configured with a real cache (D1/D2)."""
        # Given — a development (non-test) runtime; bare singleton is a no-op.
        _seed_runtime(monkeypatch, test_mode=False)
        assert get_idempotency_gate()._cache is None

        # When
        bootstrap._install_idempotency_gate()

        # Then — singleton now performs real dedup (cache-backed).
        assert get_idempotency_gate()._cache is not None

    def test_test_mode_runtime_skips_install(self, monkeypatch):
        """Test-mode runtime → install is a no-op, singleton stays ``cache=None`` (D3)."""
        # Given — a test-mode runtime (the suite default).
        _seed_runtime(monkeypatch, test_mode=True)

        # When
        bootstrap._install_idempotency_gate()

        # Then — singleton untouched.
        assert get_idempotency_gate()._cache is None

    def test_reinstall_after_reset_yields_fresh_cache_backed_gate(self, monkeypatch):
        """After ``reset_init_state()`` a re-install rebuilds a cache-backed gate (D4)."""
        # Given — an installed cache-backed singleton.
        _seed_runtime(monkeypatch, test_mode=False)
        bootstrap._install_idempotency_gate()
        first = get_idempotency_gate()
        assert first._cache is not None

        # When — reset drops the runtime-scoped singleton store, then re-install.
        bootstrap.reset_init_state()
        bootstrap._install_idempotency_gate()
        second = get_idempotency_gate()

        # Then — a fresh, distinct, still cache-backed instance.
        assert second is not first
        assert second._cache is not None

    def test_success_path_emits_installed_breadcrumb(self, monkeypatch):
        """Success path logs ``idempotency_gate.installed`` with the adapter name (D8).

        Spies the module logger directly rather than ``structlog.testing.
        capture_logs`` — the module-level ``bootstrap.logger`` is cached on
        first use, so a global-config capture is a known xdist flake source
        (per ``project_xdist_capture_logs_flake``). A direct call-args spy is
        deterministic regardless of worker ordering.
        """
        _seed_runtime(monkeypatch, test_mode=False)

        with patch.object(bootstrap, "logger") as mock_logger:
            bootstrap._install_idempotency_gate()

        installed = [
            c
            for c in mock_logger.debug.call_args_list
            if c.args and c.args[0] == _INSTALLED_EVENT
        ]
        assert len(installed) == 1
        # adapter type name is recorded for the install breadcrumb.
        assert installed[0].kwargs.get("adapter")

    def test_test_mode_skip_does_not_emit_breadcrumb(self, monkeypatch):
        """The test-mode early-return path emits no breadcrumb (D3/D8)."""
        _seed_runtime(monkeypatch, test_mode=True)

        with patch.object(bootstrap, "logger") as mock_logger:
            bootstrap._install_idempotency_gate()

        installed = [
            c
            for c in mock_logger.debug.call_args_list
            if c.args and c.args[0] == _INSTALLED_EVENT
        ]
        assert installed == []
