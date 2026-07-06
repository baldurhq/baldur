"""Mock-based integration tests for ``baldur.init()`` fail-loud wiring (#463).

Verifies the framework-agnostic init() wiring flow end-to-end through
:func:`baldur.init`, exercising the composition of:

- :class:`BaldurRuntime` env eager-read (BALDUR_TEST_MODE / BALDUR_ENVIRONMENT)
- ``ProviderRegistry.cache`` default-name selection
- :class:`ResilientStorageBackend` singleton install via
  :func:`configure_storage_backend`
- WAL filesystem (``tmp_path``-backed)
- ``reset_init_state`` lifecycle (D11 / D16) — repeated init() under
  xdist must not leak Redis sockets

No Docker required: the cache adapter and WAL are mocked / driven by
``tmp_path``. The full ``init()`` orchestrator runs (10 steps) so
inter-step ordering and state propagation are exercised.

Reference:
- 463 (D3 trigger matrix)
- the integration test guidelines (mock-based subtype)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.core.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def _isolated_init_state():
    """Each test starts and ends with a clean bootstrap + cache snapshot."""
    from baldur import bootstrap
    from baldur.factory.registry import ProviderRegistry

    bootstrap.reset_init_state()
    with ProviderRegistry.cache.snapshot():
        yield
    bootstrap.reset_init_state()


@pytest.fixture
def patched_eager_backend(tmp_path, monkeypatch):
    """Replace ``ResilientStorageBackend`` + ``configure_storage_backend``.

    The mock backend reports a writable ``wal_dir`` (``tmp_path``) and a
    successful ``_wal_initialized=True`` so the production WAL fail-fast
    path does NOT trip in the trigger-matrix happy paths. WAL-failure
    scenarios override this fixture inline.
    """
    backend = MagicMock()
    backend._wal_initialized = True
    backend.config = MagicMock(wal_dir=str(tmp_path))

    backend_cls = MagicMock(return_value=backend)
    configure_fn = MagicMock()

    with patch.multiple(
        "baldur.adapters.resilient.backend",
        ResilientStorageBackend=backend_cls,
        configure_storage_backend=configure_fn,
    ):
        # Stub get_redis_settings so wiring step doesn't depend on env var.
        settings_stub = MagicMock(url="redis://stub:6379/0")
        monkeypatch.setattr(
            "baldur.settings.redis.get_redis_settings",
            lambda: settings_stub,
        )
        yield {
            "backend": backend,
            "configure_fn": configure_fn,
            "backend_cls": backend_cls,
        }


def _scaffold_init_subdeps():
    """Patch every other init() sub-step except _wire_registry_defaults.

    Returns a context manager that, when entered, isolates the wiring step
    from event-bus / shutdown-handler / scheduler / admin server side
    effects so the integration test sees a clean signal.
    """
    from baldur import bootstrap

    return patch.multiple(
        bootstrap,
        _validate_startup_config=MagicMock(),
        _register_default_event_handlers=MagicMock(),
        _init_bridge_instrumentation=MagicMock(),
        _register_shutdown_handlers=MagicMock(),
        _run_pro_extensions=MagicMock(return_value=bootstrap.ExtensionResult()),
        _apply_audit_default_provider=MagicMock(),
        _start_audit_pipeline_if_enabled=MagicMock(),
        _record_env_snapshot=MagicMock(),
        _start_default_scheduler=MagicMock(),
        _register_sql_statistics_if_available=MagicMock(),
        _start_admin_server_if_enabled=MagicMock(),
    )


# =============================================================================
# D3 trigger matrix — full init() exercise
# =============================================================================


class TestInitTriggerMatrixIntegration:
    """Five rows of the D3 trigger matrix exercised via ``baldur.init()``."""

    def test_init_in_test_mode_does_not_flip_default_to_redis(
        self, monkeypatch, patched_eager_backend
    ):
        """Row 1: BALDUR_TEST_MODE=true → wiring step early-returns silently."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)

        with _scaffold_init_subdeps():
            bootstrap.init()

        assert ProviderRegistry.cache.get_default_name() == "memory"
        # Eager backend NOT constructed in test mode.
        patched_eager_backend["configure_fn"].assert_not_called()

    def test_init_in_production_with_url_unset_blocks_startup(self, monkeypatch):
        """Row 2: prod + URL unset → init() raises ConfigurationError."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)

        with _scaffold_init_subdeps():
            with pytest.raises(ConfigurationError, match="BALDUR_REDIS_URL"):
                bootstrap.init()

    def test_init_in_production_with_url_set_wires_redis_and_backend(
        self, monkeypatch, patched_eager_backend
    ):
        """Row 3: prod + URL set → cache=redis + backend installed via init()."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        # 464 — production also requires a SQL/Django signal so Group B
        # of the wiring step does not raise.
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")

        with _scaffold_init_subdeps():
            bootstrap.init()

        assert ProviderRegistry.cache.get_default_name() == "redis"
        patched_eager_backend["configure_fn"].assert_called_once_with(
            patched_eager_backend["backend"]
        )

    def test_init_non_production_with_url_unset_warns_and_falls_back(
        self, monkeypatch, caplog
    ):
        """Row 4: non-prod + URL unset → WARNING + memory default."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)

        with _scaffold_init_subdeps():
            with caplog.at_level("WARNING"):
                bootstrap.init()

        assert ProviderRegistry.cache.get_default_name() == "memory"
        assert any("registry_memory_fallback" in r.message for r in caplog.records)

    def test_init_non_production_with_url_set_wires_redis_default(
        self, monkeypatch, patched_eager_backend
    ):
        """Row 5: non-prod + URL set → redis default + backend installed."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")

        with _scaffold_init_subdeps():
            bootstrap.init()

        assert ProviderRegistry.cache.get_default_name() == "redis"
        patched_eager_backend["configure_fn"].assert_called_once()


# =============================================================================
# D7 production WAL fail-fast — full init() exercise
# =============================================================================


class TestInitProductionWalFailFastIntegration:
    """D7: production WAL init failure is observable through ``init()``."""

    def test_init_raises_when_production_wal_init_fails(self, monkeypatch, tmp_path):
        """Production + WAL init fails → init() raises ConfigurationError."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")

        # Backend reports WAL init failure.
        backend = MagicMock()
        backend._wal_initialized = False
        backend.config = MagicMock(wal_dir="/nonexistent/baldur-wal")

        settings_stub = MagicMock(url="redis://prod:6379/0")
        monkeypatch.setattr(
            "baldur.settings.redis.get_redis_settings",
            lambda: settings_stub,
        )

        with (
            patch(
                "baldur.adapters.resilient.backend.ResilientStorageBackend",
                return_value=backend,
            ),
            patch("baldur.adapters.resilient.backend.configure_storage_backend"),
            _scaffold_init_subdeps(),
        ):
            with pytest.raises(ConfigurationError, match="WAL initialization failed"):
                bootstrap.init()


# =============================================================================
# D15 legacy alias rejection — full init() exercise
# =============================================================================


class TestInitLegacyAliasRejectionIntegration:
    """D15 legacy alias hard-fails are visible through ``init()``."""

    @pytest.mark.parametrize(
        "alias",
        ["prod", "live", "release", "stable"],
        ids=["prod", "live", "release", "stable"],
    )
    def test_init_raises_on_known_legacy_alias(self, monkeypatch, alias):
        """init() raises ConfigurationError on each known legacy alias."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", alias)
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://x:6379/0")

        with _scaffold_init_subdeps():
            with pytest.raises(ConfigurationError, match="legacy alias"):
                bootstrap.init()


# =============================================================================
# Reset chain — D11 / D16 lifecycle integration
# =============================================================================


# =============================================================================
# 464 — Group A/B integration coverage (representative rows beyond cache)
# =============================================================================


class TestInitGroupAIntegration:
    """464 — at least one Group A row beyond cache exercised through ``init()``.

    ``config_history_store`` is the chosen representative: like cache it
    is a Redis-backed registry with no Django ORM fallback, so the D3
    matrix applies directly. Cache and the other Group A rows share the
    helper, so a single representative covers the wiring contract; the
    full Group A × matrix is in the unit tests.
    """

    def test_production_with_redis_unset_raises_naming_config_history_store_or_cache(
        self, monkeypatch
    ):
        """prod + Redis unset → init() raises naming the offending Group A row.

        Cache is row 1 of ``_REGISTRIES_TO_WIRE`` so it is the first to
        fail, but the message must mention ``BALDUR_REDIS_URL`` either
        way. The point of this row is to confirm the fail-loud path
        propagates through the full ``init()`` orchestrator.
        """
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)

        with _scaffold_init_subdeps():
            with pytest.raises(ConfigurationError, match="BALDUR_REDIS_URL"):
                bootstrap.init()

    def test_init_in_production_wires_all_group_a_rows_to_redis(
        self, monkeypatch, patched_eager_backend
    ):
        """prod + URL set → every Group A registry flips to ``"redis"`` via init()."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")

        with _scaffold_init_subdeps():
            bootstrap.init()

        # Spot-check one row beyond cache so the integration confirms the
        # end-to-end wiring is not cache-only.
        assert ProviderRegistry.cache.get_default_name() == "redis"
        assert ProviderRegistry.config_history_store.get_default_name() == "redis"
        assert ProviderRegistry.cross_cluster_store.get_default_name() == "redis"


class TestInitGroupBIntegration:
    """464 — at least one Group B row exercised through ``init()``.

    ``cascade_event_repo`` is the chosen representative: SQL/Django-backed,
    no special fallback (cf. ``rate_limit_storage``), so the D6 matrix
    applies directly.
    """

    def test_production_with_redis_set_but_sql_django_unset_raises(
        self, monkeypatch, patched_eager_backend
    ):
        """prod + Redis set + neither SQL/Django → ConfigurationError on Group B.

        Group A and Group B verdicts run independently; satisfying one
        does not exempt the other.
        """
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with _scaffold_init_subdeps():
            with pytest.raises(ConfigurationError) as exc_info:
                bootstrap.init()

        message = str(exc_info.value)
        assert "BALDUR_SQL_DSN" in message
        assert "Django DATABASES" in message

    def test_production_with_sql_dsn_wires_group_b_rows_to_sql(
        self, monkeypatch, patched_eager_backend
    ):
        """prod + DSN set → all Group B registries flip to ``"sql"`` via init()."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://prod-db/baldur")

        with _scaffold_init_subdeps():
            bootstrap.init()

        assert ProviderRegistry.cascade_event_repo.get_default_name() == "sql"
        assert ProviderRegistry.recovery_session_repo.get_default_name() == "sql"
        assert ProviderRegistry.security_repo.get_default_name() == "sql"


class TestInitRateLimitFallbackIntegration:
    """464 D11 — ``rate_limit_storage`` cross-backend fallback through ``init()``."""

    def test_non_production_redis_unset_django_set_lands_on_database(self, monkeypatch):
        """non-prod + Redis unset + Django configured →
        ``rate_limit_storage`` lands on ``"database"``; sibling Group A
        rows fall back to memory."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        # The pytest-django plugin already exports DJANGO_SETTINGS_MODULE
        # via pytest.ini, so the Django+DATABASES signal is set in this
        # test environment. Re-assert it explicitly for clarity.
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

        with _scaffold_init_subdeps():
            bootstrap.init()

        assert ProviderRegistry.rate_limit_storage.get_default_name() == "database"
        # Sibling Group A rows without ``fallback_target`` stay at memory.
        assert ProviderRegistry.config_history_store.get_default_name() == "memory"


# =============================================================================
# 464 — Reset chain wired-registry cleanup (D13)
# =============================================================================


class TestInitWiredRegistryResetIntegration:
    """464 D13 Step 3.5 exercised through the full ``init() → reset → init()``."""

    def test_reset_after_init_clears_group_a_and_b_defaults(
        self, monkeypatch, patched_eager_backend
    ):
        """After ``init()`` flips Group A/B to non-memory, ``reset_init_state``
        restores every wired registry to memory baseline (D13)."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://prod-db/baldur")

        with _scaffold_init_subdeps():
            bootstrap.init()

            # Sanity: Group A → redis, Group B → sql.
            assert ProviderRegistry.config_history_store.get_default_name() == "redis"
            assert ProviderRegistry.cascade_event_repo.get_default_name() == "sql"

            bootstrap.reset_init_state()

        # Every wired registry is back at its declared reset baseline — most
        # rows restore to "memory", but probe-surface PRIORITY_CHAIN rows (e.g.
        # database_health) restore to "noop", their only safe default.
        for wiring in bootstrap._REGISTRIES_TO_WIRE:
            registry = getattr(ProviderRegistry, wiring.registry_attr)
            assert registry.get_default_name() == wiring.reset_baseline, (
                f"{wiring.registry_attr} not reset to {wiring.reset_baseline!r} "
                "after reset_init_state"
            )


class TestInitResetCycleIntegration:
    """Repeated ``init() → reset_init_state() → init()`` is leak-free."""

    def test_reset_chain_drains_storage_backend_and_cache_pool(
        self, monkeypatch, patched_eager_backend
    ):
        """Chain order: reset_storage_backend(cleanup=True) → cache close →
        cache default reset → reset_runtime."""
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")

        with _scaffold_init_subdeps():
            bootstrap.init()

        # Inject a stub redis cache instance so the reset chain's adapter
        # close path is exercised.
        stub_cache = MagicMock()
        ProviderRegistry.cache.set_instance("redis", stub_cache)

        with patch(
            "baldur.adapters.resilient.backend.reset_storage_backend"
        ) as m_reset_storage:
            bootstrap.reset_init_state()

        m_reset_storage.assert_called_once_with(cleanup=True)
        stub_cache.close.assert_called_once_with()
        # Default re-asserted after reset.
        assert ProviderRegistry.cache.get_default_name() == "memory"

    def test_repeated_init_reset_cycle_does_not_raise(
        self, monkeypatch, patched_eager_backend
    ):
        """init → reset → init → reset → init: no leak symptom (no exception).

        Models the xdist re-entry pattern documented in
        UNIT_TEST_GUIDELINES §6.5.5.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://dev:6379/0")

        with _scaffold_init_subdeps():
            for _ in range(3):
                bootstrap.init()
                # Sanity: each init lands the redis default.
                assert ProviderRegistry.cache.get_default_name() == "redis"
                bootstrap.reset_init_state()
                # Sanity: reset flips back to memory.
                assert ProviderRegistry.cache.get_default_name() == "memory"
