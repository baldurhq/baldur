"""Cat 4.8 — apps.py ``ready()`` exception-propagation contract.

Plan row: the perf-scenario plan

Verification criterion: ``apps.py:ready() must NOT swallow init exceptions``.

``BaldurConfig.ready()`` is the Django ``AppConfig`` hook that runs at
every server start. Its contract is to **fail loud** on critical
bootstrap errors (so Django CrashLoops and an operator sees the problem)
while remaining **best-effort** on optional sub-systems that legitimately
fail in some deployments (Celery missing, JWT blacklist not installed,
etc.).

This file pins the propagation contract for ``ready()``:

Propagating call sites (must NOT be silently swallowed):
    1. ``baldur.init()`` — ``ConfigurationError`` raised by
       ``_wire_registry_defaults`` in production when the Redis URL is
       not configured (#463 fail-loud, ``bootstrap.py``
       ``_wire_redis_registry``).
    2. ``baldur.init()`` — ``AdminAuthRequiredError`` raised by
       ``_start_admin_server_if_enabled`` for a non-localhost bind
       without an API key (``bootstrap.py`` explicit re-raise).
    3. ``baldur.init()`` — ``RuntimeError`` raised by the centralized secret
       gate (``bootstrap._validate_critical_secrets``) when CRITICAL secrets
       (``encryption_key``, ``audit_signing_key``) are unset in production.
       632 D7 lifted this out of the Django-only ``apps.py._validate_secrets``
       method so it fires on every framework adapter via ``init()``.

Best-effort call sites (intentionally swallow, do NOT propagate):
    - ``_connect_session_signals`` (any ``Exception`` → WARNING log)
    - ``_autodiscover_celery_tasks`` (any ``Exception`` → WARNING log)
    - ``_initialize_orphan_services`` (any ``Exception`` → WARNING log)
    - ``_register_jwt_blacklist_hook`` (any ``Exception`` → WARNING log)

Verification techniques (UNIT_TEST_GUIDELINES §8):
    - §8.2 Exception/edge cases — each call site exercised under
      deliberate failure.
    - §8.5 Dependency interaction — patch chain isolates the propagation
      path under test from unrelated subsystems.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.adapters.django.apps import BaldurConfig
from baldur.api.admin.auth import AdminAuthRequiredError
from baldur.core.exceptions import ConfigurationError


@pytest.fixture
def app_config():
    """Fully constructed ``BaldurConfig`` instance for ``ready()`` tests."""
    return BaldurConfig("baldur.adapters.django", __import__("baldur"))


@pytest.fixture
def silent_inputs(monkeypatch):
    """Stub the **external inputs** that ``ready()`` calls into.

    Replaces the side-effect-laden boundaries (RBAC signal connect, hash
    chain sync, ``baldur.init()``, background-thread spawning) with
    no-ops, leaving every ``BaldurConfig._*`` private method on the
    instance UNTOUCHED so each one's real ``try/except`` wrapper still
    executes.

    Tests that need a propagating call site to raise (e.g. ``baldur.init``
    surfacing the production secret-gate ``RuntimeError``) re-apply
    :func:`monkeypatch.setattr` on top.

    .. note::

       ``baldur.init`` is patched on the top-level ``baldur`` module,
       not on ``baldur.bootstrap``. The PEP 562 lazy-import dispatcher in
       ``baldur/__init__.py`` caches ``init`` as a module-level attribute
       on first access, so patching the source in ``baldur.bootstrap``
       has no effect once the cache is warm. ``raising=False`` makes the
       patch idempotent regardless of whether prior tests have already
       primed the cache.
    """
    monkeypatch.setattr(
        "baldur.adapters.django.startup.RBACInitializer.connect_post_migrate",
        staticmethod(lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        "baldur.adapters.django.startup.EnvironmentAuditor.sync_hash_chain_on_startup",
        staticmethod(lambda *args, **kwargs: None),
    )
    monkeypatch.setattr("baldur.init", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(
        BaldurConfig,
        "_should_start_background_threads",
        lambda self: False,
    )


# =============================================================================
# Contract — propagating call sites
# =============================================================================


class TestReadyInitPropagationContract:
    """``baldur.init()`` exceptions MUST propagate out of ``ready()``.

    The ``import baldur; baldur.init(...)`` call site in
    ``BaldurConfig.ready()`` is intentionally NOT wrapped in
    ``try/except`` — config wiring failures and admin-server auth
    misconfig are too consequential for the system to silently start in a
    half-working state.
    """

    def test_ready_propagates_configuration_error_from_init(
        self, app_config, silent_inputs, monkeypatch
    ):
        """``ConfigurationError`` raised by ``baldur.init()`` propagates.

        Reproduces the #463 production fail-loud path: in production
        ``_wire_registry_defaults`` raises ``ConfigurationError`` when
        ``BALDUR_REDIS_URL`` is unset. ``ready()`` must surface this
        error so the Django boot CrashLoops instead of silently running
        with memory-only storage.
        """

        def _raise_config_error(**kwargs):
            raise ConfigurationError(
                "BALDUR_REDIS_URL is not set in production for ProviderRegistry.cache."
            )

        monkeypatch.setattr("baldur.init", _raise_config_error, raising=False)

        with pytest.raises(ConfigurationError, match="BALDUR_REDIS_URL"):
            app_config.ready()

    def test_ready_propagates_admin_auth_required_error_from_init(
        self, app_config, silent_inputs, monkeypatch
    ):
        """``AdminAuthRequiredError`` propagates through ``ready()``.

        ``_start_admin_server_if_enabled`` explicitly re-raises this
        ``ConfigurationError`` subclass because silently refusing to
        start an insecurely-configured admin server would mask a serious
        misconfiguration. ``ready()`` must not swallow it either.
        """

        def _raise_admin_auth(**kwargs):
            raise AdminAuthRequiredError(
                "Admin server bound to 0.0.0.0 without an API key.",
                bind="0.0.0.0:8090",
            )

        monkeypatch.setattr("baldur.init", _raise_admin_auth, raising=False)

        with pytest.raises(AdminAuthRequiredError):
            app_config.ready()

    def test_ready_passes_quarantine_callback_to_init(
        self, app_config, silent_inputs, monkeypatch
    ):
        """``ready()`` forwards ``_activate_quarantine_mode`` to ``init()``.

        The framework-agnostic init() / 416 contract: Django's ``ready()`` must hand its
        own quarantine callback to ``baldur.init()`` so a fatal config
        error can flip the system into Quarantine Mode (LEVEL_3) instead
        of crash-looping. Without this wire-up the propagation contract
        verified above is moot — ``init()`` would raise even on
        recoverable config errors.
        """
        from unittest.mock import MagicMock

        mock_init = MagicMock()
        monkeypatch.setattr("baldur.init", mock_init, raising=False)

        app_config.ready()

        assert mock_init.call_count == 1
        kwargs = mock_init.call_args.kwargs
        assert "quarantine_callback" in kwargs
        callback = kwargs["quarantine_callback"]
        assert callback is not None
        # Bound method of the BaldurConfig instance — ``__func__`` is the
        # underlying ``_activate_quarantine_mode`` defined on the class.
        assert callback.__func__ is BaldurConfig._activate_quarantine_mode


class TestReadySecretsPropagationContract:
    """Production CRITICAL-secret ``RuntimeError`` MUST propagate through ``ready()``.

    Since 632 D7 the secret gate lives inside ``baldur.init()``
    (``bootstrap._validate_critical_secrets``), so a missing ``encryption_key``
    / ``audit_signing_key`` in production surfaces as a ``RuntimeError`` from
    the ``baldur.init()`` call site in ``ready()``. ``ready()`` must not swallow
    it — best-effort silent recovery is unacceptable for security-critical
    secrets. (Same fail-loud contract previously held by the removed
    ``apps.py._validate_secrets`` step.)
    """

    def test_ready_propagates_runtime_error_from_secret_gate(
        self, app_config, silent_inputs, monkeypatch
    ):
        """A secret-gate ``RuntimeError`` raised by ``baldur.init()`` propagates.

        The gate moved into ``baldur.init()`` (632 D7), so this reproduces the
        production path where ``_validate_critical_secrets`` re-raises the
        ``validate_required_secrets`` ``RuntimeError`` and ``init()`` lets it
        out — ``ready()`` must surface it so the Django boot CrashLoops.
        """

        def _raise_secret_error(**kwargs):
            raise RuntimeError(
                "[Security] CRITICAL secrets not configured in production: "
                "encryption_key, audit_signing_key."
            )

        monkeypatch.setattr("baldur.init", _raise_secret_error, raising=False)

        with pytest.raises(RuntimeError, match="CRITICAL secrets"):
            app_config.ready()


# =============================================================================
# Behavior — best-effort call sites swallow exceptions
# =============================================================================


class TestReadyBestEffortBehavior:
    """Best-effort sub-systems must NOT bring down ``ready()``.

    Each call site below wraps its body in ``try/except Exception`` and
    logs a WARNING. The contract is asymmetric on purpose: critical
    bootstrap (``init``, secrets) fails loud; optional sub-systems
    (Celery, session signals, JWT, orphan services) degrade quietly so a
    deployment without those extras still boots.

    Each test patches the INNER dependency (not the wrapper method) so
    the real ``try/except`` body in ``apps.py`` executes and we verify
    its production behavior — not a stubbed substitute.
    """

    def test_ready_swallows_session_signals_failure(self, app_config, silent_inputs):
        """``_connect_session_signals`` raising does NOT abort ``ready()``."""
        with patch(
            "baldur.adapters.django.signal_hooks.connect_session_signals",
            side_effect=RuntimeError("session signal wiring crashed"),
        ):
            # Absence of exception is the contract.
            app_config.ready()

    def test_ready_swallows_autodiscover_celery_failure(
        self, app_config, silent_inputs
    ):
        """``_autodiscover_celery_tasks`` raising does NOT abort ``ready()``."""
        from celery import current_app

        with patch.object(
            current_app,
            "autodiscover_tasks",
            side_effect=RuntimeError("celery broker unreachable"),
        ):
            app_config.ready()

    def test_ready_swallows_orphan_services_failure(self, app_config, silent_inputs):
        """``_initialize_orphan_services`` raising does NOT abort ``ready()``.

        Forces the dependency-graph construction to fail so the wrapper's
        ``except Exception`` arm fires (``apps.py:_initialize_orphan_services``).
        """
        with patch(
            "baldur.core.dependency_graph.ServiceDependencyGraph",
            side_effect=RuntimeError("dependency graph unavailable"),
        ):
            app_config.ready()

    def test_ready_swallows_jwt_blacklist_hook_failure(self, app_config, silent_inputs):
        """``_register_jwt_blacklist_hook`` raising does NOT abort ``ready()``.

        Forces ``django.apps.apps.is_installed`` to raise so the JWT-hook
        wrapper's ``except Exception`` arm fires.
        """
        with patch(
            "django.apps.apps.is_installed",
            side_effect=RuntimeError("apps registry corrupt"),
        ):
            app_config.ready()
