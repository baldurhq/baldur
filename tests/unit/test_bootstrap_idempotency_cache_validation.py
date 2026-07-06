"""532 D7 — ``_validate_idempotency_cache_in_production`` init-time validation.

Defense-in-depth on top of ``_wire_redis_registry``'s cache row enforcement:
catches the residual case where the cache default ends up as ``None`` or
``"memory"`` after ``_wire_registry_defaults`` (e.g. a customized wiring
skips the cache row).

Trigger matrix (3 dims):
- ``BaldurRuntime.is_test_mode`` → no-op when True.
- ``BaldurRuntime.is_production`` → no-op when False.
- ``IdempotencySettings.allow_inmemory_fallback`` → no-op when True
  (operator explicitly accepted in-process-only semantics).
- ``ProviderRegistry.cache.get_default_name()`` ∈
  ``{None, "memory", "redis"}`` → raise on ``None`` / ``"memory"``,
  succeed on ``"redis"``.

This file lives at ``tests/unit/test_bootstrap_idempotency_cache_validation.py``
following the existing ``tests/unit/test_bootstrap_*.py`` convention
(``baldur.bootstrap`` is a top-level module without a parent package).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur import bootstrap
from baldur.core.exceptions import ConfigurationError

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_bootstrap_and_runtime():
    """Each test starts and ends with clean bootstrap + runtime state."""
    bootstrap.reset_init_state()
    yield
    bootstrap.reset_init_state()


@pytest.fixture(autouse=True)
def _reset_idempotency_settings():
    """Each test starts and ends with cleared idempotency settings cache."""
    from baldur.settings.idempotency import reset_idempotency_settings

    reset_idempotency_settings()
    yield
    reset_idempotency_settings()


@pytest.fixture
def _isolated_cache_default():
    """Snapshot the cache registry's default + instances around the test."""
    from baldur.factory.registry import ProviderRegistry

    with ProviderRegistry.cache.snapshot():
        yield


def _seed_env(monkeypatch, *, in_production: bool, escape_hatch: bool) -> None:
    if in_production:
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
    else:
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
    monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
    monkeypatch.setenv(
        "BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK",
        "true" if escape_hatch else "false",
    )
    bootstrap.reset_init_state()  # rebuild runtime with new env


# =============================================================================
# TestIdempotencyCacheValidation — 3-dim matrix
# =============================================================================


class TestIdempotencyCacheValidation:
    """D7 trigger matrix: (in_production × allow_inmemory_fallback × default_name)."""

    @pytest.mark.parametrize(
        "default_name",
        [None, "memory"],
        ids=["default_none", "default_memory"],
    )
    def test_prod_no_escape_with_undistributed_default_raises(
        self, monkeypatch, _isolated_cache_default, default_name
    ):
        """prod + escape off + default ∈ {None, "memory"} → ConfigurationError."""
        from baldur.factory.registry import ProviderRegistry

        # Given — production env + escape hatch off + undistributed cache.
        _seed_env(monkeypatch, in_production=True, escape_hatch=False)
        ProviderRegistry.cache._default = default_name  # bypass set_default validation

        # When + Then — raises with operator-actionable message.
        with pytest.raises(ConfigurationError) as exc_info:
            bootstrap._validate_idempotency_cache_in_production()

        message = str(exc_info.value)
        assert "BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK" in message
        assert "BALDUR_REDIS_URL" in message
        assert "production" in message.lower()
        assert repr(default_name) in message

    def test_prod_no_escape_with_redis_default_passes(
        self, monkeypatch, _isolated_cache_default
    ):
        """prod + escape off + default = "redis" → no raise (distributed)."""
        from baldur.factory.registry import ProviderRegistry

        _seed_env(monkeypatch, in_production=True, escape_hatch=False)
        ProviderRegistry.cache.set_default("redis")

        # No raise — distributed default satisfies the invariant.
        bootstrap._validate_idempotency_cache_in_production()

    @pytest.mark.parametrize(
        "default_name",
        [None, "memory", "redis"],
        ids=["default_none", "default_memory", "default_redis"],
    )
    def test_prod_with_escape_hatch_on_skips_all_defaults(
        self, monkeypatch, _isolated_cache_default, default_name
    ):
        """prod + escape on → operator opted into in-process semantics.

        ``allow_inmemory_fallback=true`` is the explicit acknowledgement that
        per-worker dedup is acceptable, so the validator must not block.
        """
        from baldur.factory.registry import ProviderRegistry

        _seed_env(monkeypatch, in_production=True, escape_hatch=True)
        if default_name is None:
            ProviderRegistry.cache._default = None
        else:
            ProviderRegistry.cache.set_default(default_name)

        # No raise regardless of cache default.
        bootstrap._validate_idempotency_cache_in_production()

    @pytest.mark.parametrize(
        "default_name",
        [None, "memory", "redis"],
        ids=["default_none", "default_memory", "default_redis"],
    )
    def test_skips_in_non_production(
        self, monkeypatch, _isolated_cache_default, default_name
    ):
        """Non-production runtime → validator is a no-op.

        Dev laptops + CI tolerate in-memory cache by design — the loud
        signal would be noise outside production.
        """
        from baldur.factory.registry import ProviderRegistry

        _seed_env(monkeypatch, in_production=False, escape_hatch=False)
        if default_name is None:
            ProviderRegistry.cache._default = None
        else:
            ProviderRegistry.cache.set_default(default_name)

        # No raise.
        bootstrap._validate_idempotency_cache_in_production()

    def test_skips_in_test_mode(self, monkeypatch, _isolated_cache_default):
        """``BALDUR_TEST_MODE=true`` → validator is a no-op even in prod env.

        Test mode is the deliberate escape route for test-suite isolation
        and matches the test-mode early return used by every other
        startup-config check.
        """
        from baldur.factory.registry import ProviderRegistry

        # Test mode set before bootstrap.reset_init_state — runtime eager-reads it.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        bootstrap.reset_init_state()
        ProviderRegistry.cache._default = "memory"

        # No raise — test mode wins.
        bootstrap._validate_idempotency_cache_in_production()


# =============================================================================
# TestValidatorCallSiteOrdering — invoked from init() between two precise steps
# =============================================================================


class TestValidatorCallSiteOrdering:
    """D7 call-site: ``init()`` runs the validator AFTER
    ``_wire_registry_defaults`` and BEFORE ``_emit_tier_setting_warnings``.

    The ordering is load-bearing — the validator reads
    ``ProviderRegistry.cache.get_default_name()`` which must reflect the
    post-wiring state. 566 D1 inserts ``_install_idempotency_gate`` between the
    validator and tier-warnings: prod-bad aborts at the validator (its nicer
    message wins) before install runs, and prod-good has a guaranteed
    distributed cache for the resolver to return.
    """

    def test_init_invokes_validator_between_wiring_and_tier_warnings(self):
        """``init()`` calls the validator in the documented relative position.

        566 D1 extends the chain: ``wire < validate_idem < install_idem <
        tier_warnings``.
        """
        call_order: list[str] = []

        def _track(name):
            def _impl(*_a, **_kw):
                call_order.append(name)

            return _impl

        with (
            patch.object(bootstrap, "_validate_startup_config", _track("validate_cfg")),
            patch.object(
                bootstrap, "_validate_critical_secrets", _track("validate_secrets")
            ),
            patch.object(
                bootstrap,
                "_register_default_event_handlers",
                _track("event_handlers"),
            ),
            patch.object(
                bootstrap, "_init_bridge_instrumentation", _track("bridge_instr")
            ),
            patch.object(
                bootstrap, "_register_shutdown_handlers", _track("shutdown_handlers")
            ),
            patch.object(bootstrap, "_wire_registry_defaults", _track("wire")),
            patch.object(
                bootstrap,
                "_validate_idempotency_cache_in_production",
                _track("validate_idem"),
            ),
            patch.object(
                bootstrap, "_install_idempotency_gate", _track("install_idem")
            ),
            patch.object(
                bootstrap, "_emit_tier_setting_warnings", _track("tier_warnings")
            ),
            patch.object(
                bootstrap, "_run_pro_extensions", MagicMock(return_value=MagicMock())
            ),
            patch.object(bootstrap, "_apply_audit_default_provider", _track("audit")),
            patch.object(
                bootstrap, "_start_audit_pipeline_if_enabled", _track("audit_pipe")
            ),
            patch.object(
                bootstrap, "_start_dlq_outbox_if_enabled", _track("dlq_outbox")
            ),
            patch.object(bootstrap, "_record_env_snapshot", _track("env_snap")),
            patch.object(bootstrap, "_start_default_scheduler", _track("scheduler")),
            patch.object(
                bootstrap, "_register_sql_statistics_if_available", _track("sql_stats")
            ),
            patch.object(bootstrap, "_start_admin_server_if_enabled", _track("admin")),
            patch.object(
                bootstrap, "_start_capacity_reservation_if_enabled", _track("cap_res")
            ),
            patch.object(
                bootstrap, "_start_cell_topology_if_enabled", _track("cell_topo")
            ),
            # _start_circuit_mesh_if_enabled removed (599 D12 - start moved
            # to baldur_pro.register_pro_services).
            patch.object(
                bootstrap, "_build_startup_report", MagicMock(return_value={})
            ),
        ):
            bootstrap.init()

        # Then — validator sits between wiring and the gate install, which in
        # turn precedes tier-warnings (566 D1).
        wire_idx = call_order.index("wire")
        validate_idx = call_order.index("validate_idem")
        install_idx = call_order.index("install_idem")
        tier_idx = call_order.index("tier_warnings")
        assert wire_idx < validate_idx < install_idx < tier_idx, (
            "Expected wire → validate_idem → install_idem → tier_warnings, "
            f"got: {call_order}"
        )

    def test_validator_raise_propagates_from_init(
        self, monkeypatch, _isolated_cache_default
    ):
        """A validator raise must propagate out of ``init()`` so the process
        crashes loudly — defense-in-depth has no value if init() swallows it."""
        from baldur.factory.registry import ProviderRegistry

        # Force prod + escape off + memory default → validator raises.
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        bootstrap.reset_init_state()

        # Sidestep heavy upstream steps so the test reaches the validator.
        # _validate_critical_secrets (632 D7) is one of those upstream steps:
        # under production it would raise on the (here-ambient/unset) CRITICAL
        # secrets and short-circuit init() before the idempotency validator, so
        # it must be stubbed like every other pre-validator step.
        with (
            patch.object(bootstrap, "_validate_startup_config"),
            patch.object(bootstrap, "_validate_critical_secrets"),
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_init_bridge_instrumentation"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_wire_registry_defaults"),
        ):
            # Wiring stubbed → cache default stays "memory" (the registry baseline).
            ProviderRegistry.cache._default = "memory"
            with pytest.raises(ConfigurationError, match="Idempotency requires"):
                bootstrap.init()
