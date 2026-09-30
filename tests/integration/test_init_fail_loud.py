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

import importlib.metadata
from unittest.mock import MagicMock, patch

import pytest

# Bound at import, before the suite-wide fixture swaps the module attribute for
# a fixed ACTIVE verdict, so a case can put the real cached validator back.
from baldur.core.entitlement import (
    get_entitlement_status as _real_get_entitlement_status,
)
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

    The mock backend reports a writable ``wal_dir`` (``tmp_path``) and a WAL
    running on it, so the production WAL fail-fast path does NOT trip in the
    trigger-matrix happy paths. WAL-failure scenarios override this fixture
    inline. The attributes the boot gate reads are set explicitly rather than
    left to MagicMock's always-truthy auto-resolution.
    """
    backend = MagicMock()
    backend._wal_initialized = True
    backend._wal_on_fallback_dir = False
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


def _scaffold_init_subdeps(*, run_pro_extensions: bool = False):
    """Patch every other init() sub-step except _wire_registry_defaults.

    Returns a context manager that, when entered, isolates the wiring step
    from event-bus / shutdown-handler / scheduler / admin server side
    effects so the integration test sees a clean signal.

    ``run_pro_extensions`` keeps the real hook discovery, for cases that
    install their own ``baldur.bootstrap_hooks`` entry point.
    """
    from baldur import bootstrap

    stubs = {
        "_validate_startup_config": MagicMock(),
        "_register_default_event_handlers": MagicMock(),
        "_init_bridge_instrumentation": MagicMock(),
        "_register_shutdown_handlers": MagicMock(),
        "_apply_audit_default_provider": MagicMock(),
        "_start_audit_pipeline_if_enabled": MagicMock(),
        "_record_env_snapshot": MagicMock(),
        "_start_default_scheduler": MagicMock(),
        "_register_sql_statistics_if_available": MagicMock(),
        "_start_admin_server_if_enabled": MagicMock(),
    }
    if not run_pro_extensions:
        stubs["_run_pro_extensions"] = MagicMock(
            return_value=bootstrap.ExtensionResult()
        )
    return patch.multiple(bootstrap, **stubs)


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

    def test_init_non_production_with_url_unset_announces_and_falls_back(
        self, monkeypatch, caplog
    ):
        """Row 4: non-prod + URL unset → memory default, announced at INFO.

        The announcement is deliberately below WARNING: a development boot
        with no Redis configured is the expected posture, not a fault, and
        the startup posture line states the same fact once. What this row
        pins is that the fallback stays *visible* — the original defect was
        it being silent — and that it is not upgraded back to an alarm.

        ``init()`` now configures logging as its first step, which sets the
        root level from the environment. ``caplog.at_level`` alone is
        therefore not enough: it is applied before ``init()`` and overwritten
        by it. Driving the level through the environment instead means this
        asserts what an operator running at INFO would actually see, rather
        than what a capture handler was told to keep.
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry
        from baldur.observability.structlog_config import reset_structlog_config

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.setenv("BALDUR_TEST_LOG_LEVEL", "INFO")
        reset_structlog_config()

        try:
            with _scaffold_init_subdeps(), caplog.at_level("INFO"):
                bootstrap.init()
        finally:
            reset_structlog_config()

        assert ProviderRegistry.cache.get_default_name() == "memory"
        fallback_records = [
            record
            for record in caplog.records
            if "registry_memory_fallback" in record.getMessage()
        ]
        assert fallback_records
        assert {record.levelname for record in fallback_records} == {"INFO"}

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

        # Backend reports WAL init failure. The gate's attributes are set
        # explicitly — an auto-resolved MagicMock attribute is always truthy
        # and the gate would never fire.
        backend = MagicMock()
        backend._wal_initialized = False
        backend._wal_on_fallback_dir = False
        backend._wal = None
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

    ``recovery_session_repo`` is the chosen representative: SQL/Django-backed,
    no special fallback (cf. ``rate_limit_storage``), so the D6 matrix
    applies directly.
    """

    @staticmethod
    def _seed_production_without_sql_or_django(monkeypatch, *, entitled: bool):
        """prod + Redis set + neither SQL nor Django, with a pinned verdict.

        The suite-wide fixture reports an ACTIVE entitlement wherever PRO is
        installed, and the public CI runs with PRO absent, so the verdict the
        post-hook requirement reads is pinned here. The signing key is set so
        the key check — which runs first — passes.
        """
        from baldur.settings.secrets import reset_secrets_settings

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_SECRETS_AUDIT_SIGNING_KEY", "audit-signing-key")
        monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
        monkeypatch.setattr(
            "baldur.core.entitlement.is_entitlement_active", lambda: entitled
        )
        reset_secrets_settings()

    def test_production_entitled_without_sql_or_django_raises(
        self, monkeypatch, patched_eager_backend
    ):
        """
        Purpose:
            An active PRO entitlement writes postmortems and security
            incidents to the SQL/Django store, so production requires one;
            the requirement is checked after the PRO hook, not by wiring
            (801 D2).
        Expected:
            - prod + entitled + neither SQL nor Django: init() raises
              ConfigurationError naming both signals and the entitlement
        """
        from baldur import bootstrap

        self._seed_production_without_sql_or_django(monkeypatch, entitled=True)

        with _scaffold_init_subdeps():
            with pytest.raises(ConfigurationError) as exc_info:
                bootstrap.init()

        message = str(exc_info.value)
        assert "Neither BALDUR_SQL_DSN nor Django DATABASES" in message
        assert "PRO entitlement" in message

    def test_production_not_entitled_without_sql_or_django_boots_on_memory(
        self, monkeypatch, patched_eager_backend
    ):
        """
        Purpose:
            On OSS nothing writes these stores automatically, so the
            published two-variable production block boots (801 D2).
        Expected:
            - prod + not entitled + neither SQL nor Django: init() returns
            - the three Group B stores are wired to memory
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        self._seed_production_without_sql_or_django(monkeypatch, entitled=False)

        with _scaffold_init_subdeps():
            bootstrap.init()

        for attr in ("recovery_session_repo", "security_repo", "postmortem_repo"):
            assert getattr(ProviderRegistry, attr).get_default_name() == "memory"

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

        assert ProviderRegistry.recovery_session_repo.get_default_name() == "sql"
        assert ProviderRegistry.recovery_session_repo.get_default_name() == "sql"
        assert ProviderRegistry.security_repo.get_default_name() == "sql"


# =============================================================================
# 801 D1/D2 — the production requirements read what the PRO hook settled
# =============================================================================


class _HookEntryPoint:
    """A ``baldur.bootstrap_hooks`` entry point standing in for PRO's."""

    name = "fake_pro_hook"

    def __init__(self, hook):
        self._hook = hook

    def load(self):
        return self._hook


def _bootstrap_hooks(*hooks):
    """``entry_points`` answering the bootstrap-hook group with ``hooks`` only.

    Any other group is answered by the real function, so the installed PRO
    distribution's own hook never runs in these cases.
    """
    real_entry_points = importlib.metadata.entry_points

    def _entry_points(**kwargs):
        if kwargs.get("group") == "baldur.bootstrap_hooks":
            return [_HookEntryPoint(hook) for hook in hooks]
        return real_entry_points(**kwargs)

    return patch("importlib.metadata.entry_points", _entry_points)


def _revalidate_entitlement():
    """What PRO's hook does first: re-validate the licence with ``force=True``."""
    from baldur.core.entitlement import get_entitlement_status

    get_entitlement_status(force=True)


def _turn_audit_on():
    """What PRO's hook does for an entitled process whose operator left audit
    unset: switch the audit trail on."""
    from baldur.settings.audit import set_audit_settings

    set_audit_settings(enabled=True)


def _active_entitlement():
    from baldur.core.entitlement import (
        EntitlementClaims,
        EntitlementResult,
        EntitlementStatus,
    )

    return EntitlementResult(
        status=EntitlementStatus.ACTIVE,
        claims=EntitlementClaims(
            customer_id="cust_test",
            org="test-org",
            tier="PRO",
            plan="monthly",
            issued_at="2020-01-01",
            expires="2999-12-31",
        ),
    )


class TestInitPostHookRequirementsIntegration:
    """801 D1/D2 — ``init()`` checks the key and the store after the PRO hook.

    The hook re-validates the licence with ``force=True`` and may turn audit
    on. A verdict read earlier can be a MISSING cached before the token reached
    the environment (an import-time read, then a dotenv or Django-settings
    token). These cases run the real hook discovery with a stand-in hook and
    the real cached validator, so the requirement is shown to read the verdict
    and the audit switch the hook left behind — not what was cached before it.
    """

    @pytest.fixture
    def production_without_a_store(self, monkeypatch):
        """prod + Redis set + no SQL, no Django, no licence source, audit unset."""
        from baldur.core.entitlement import reset_entitlement_status
        from baldur.settings.audit import reset_audit_settings
        from baldur.settings.license import reset_entitlement_settings
        from baldur.settings.secrets import reset_secrets_settings

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        for name in (
            "BALDUR_SQL_DSN",
            "DJANGO_SETTINGS_MODULE",
            "BALDUR_AUDIT_ENABLED",
            "BALDUR_LICENSE_KEY",
            "BALDUR_LICENSE_FILE",
            "BALDUR_SECRETS_AUDIT_SIGNING_KEY",
        ):
            monkeypatch.delenv(name, raising=False)
        # The requirement must be decided by the validator's cache.
        monkeypatch.setattr(
            "baldur.core.entitlement.get_entitlement_status",
            _real_get_entitlement_status,
        )
        reset_entitlement_status()
        reset_entitlement_settings()
        reset_secrets_settings()
        reset_audit_settings()
        yield
        reset_entitlement_status()
        reset_entitlement_settings()
        reset_audit_settings()

    @staticmethod
    def _prime_a_stale_missing_verdict():
        """An import-time read taken before any licence reached the process."""
        from baldur.core.entitlement import EntitlementStatus, is_entitlement_active

        assert _real_get_entitlement_status().status is EntitlementStatus.MISSING
        assert is_entitlement_active() is False

    @staticmethod
    def _as_if_pro_were_installed(monkeypatch):
        """The public CI runs with PRO absent, and the entitlement predicate
        answers "not entitled" from that alone, before the verdict. The cases
        that refuse stop right after the hook, so nothing later reads it."""
        monkeypatch.setattr("baldur.utils.tier.is_pro_installed", lambda: True)

    def test_a_verdict_the_hook_turned_active_requires_the_signing_key(
        self, production_without_a_store, patched_eager_backend, monkeypatch
    ):
        """
        Purpose:
            A MISSING cached before the hook must not waive the key once the
            hook's forced read entitles the process (SC11).
        Expected:
            - init() raises ConfigurationError naming the signing-key variable
        """
        from baldur import bootstrap
        from baldur.core import entitlement

        self._as_if_pro_were_installed(monkeypatch)
        self._prime_a_stale_missing_verdict()

        with (
            _scaffold_init_subdeps(run_pro_extensions=True),
            _bootstrap_hooks(_revalidate_entitlement),
            patch.object(
                entitlement._EntitlementValidator,
                "_do_validate",
                autospec=True,
                return_value=_active_entitlement(),
            ),
            pytest.raises(ConfigurationError) as exc_info,
        ):
            bootstrap.init()

        assert "BALDUR_SECRETS_AUDIT_SIGNING_KEY" in str(exc_info.value)

    def test_with_the_key_set_the_same_verdict_requires_a_store(
        self, production_without_a_store, patched_eager_backend, monkeypatch
    ):
        """
        Purpose:
            With the key present, the hook's verdict still requires a SQL or
            Django store rather than leaving the memory wiring in place (SC11).
        Expected:
            - init() raises ConfigurationError naming BALDUR_SQL_DSN
        """
        from baldur import bootstrap
        from baldur.core import entitlement
        from baldur.settings.secrets import reset_secrets_settings

        monkeypatch.setenv("BALDUR_SECRETS_AUDIT_SIGNING_KEY", "audit-signing-key")
        reset_secrets_settings()
        self._as_if_pro_were_installed(monkeypatch)
        self._prime_a_stale_missing_verdict()

        with (
            _scaffold_init_subdeps(run_pro_extensions=True),
            _bootstrap_hooks(_revalidate_entitlement),
            patch.object(
                entitlement._EntitlementValidator,
                "_do_validate",
                autospec=True,
                return_value=_active_entitlement(),
            ),
            pytest.raises(ConfigurationError) as exc_info,
        ):
            bootstrap.init()

        assert "Neither BALDUR_SQL_DSN nor Django DATABASES" in str(exc_info.value)

    def test_without_a_forced_read_the_stale_verdict_boots_on_memory(
        self, production_without_a_store, patched_eager_backend
    ):
        """
        Purpose:
            Control for the two cases above: with no hook re-validating, the
            cached MISSING stands, nothing requires the key or a store, and the
            published OSS block boots.
        Expected:
            - init() returns; the incident stores run on memory
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        self._prime_a_stale_missing_verdict()

        with _scaffold_init_subdeps(run_pro_extensions=True), _bootstrap_hooks():
            bootstrap.init()

        for attr in ("recovery_session_repo", "security_repo", "postmortem_repo"):
            assert getattr(ProviderRegistry, attr).get_default_name() == "memory"

    def test_an_audit_switch_the_hook_turned_on_requires_the_signing_key(
        self, production_without_a_store, patched_eager_backend, monkeypatch
    ):
        """
        Purpose:
            The key's condition is read after the hook, so audit turned on by
            the hook counts, entitlement aside.
        Expected:
            - init() raises ConfigurationError naming the signing-key variable
        """
        from baldur import bootstrap

        monkeypatch.setattr(
            "baldur.core.entitlement.is_entitlement_active", lambda: False
        )

        with (
            _scaffold_init_subdeps(run_pro_extensions=True),
            _bootstrap_hooks(_turn_audit_on),
            pytest.raises(ConfigurationError) as exc_info,
        ):
            bootstrap.init()

        assert "BALDUR_SECRETS_AUDIT_SIGNING_KEY" in str(exc_info.value)


# =============================================================================
# 801 D6 — a provider the operator selected must construct, whatever its name
# =============================================================================


class TestInitOperatorSelectedBackendIntegration:
    """801 D6 (re-queue Q9) — boot accepts a probe's ``redis`` / ``django``
    unbuilt, but a name chosen through an override is always constructed.

    A host may register its own dead-letter provider under ``django`` and
    select it with ``BALDUR_DLQ_BACKEND``; skipping construction by name would
    let an unbuildable one boot and then capture into memory.
    """

    @pytest.fixture
    def unbuildable_host_provider(self, monkeypatch):
        """A host provider registered as ``django`` on the dead-letter registry
        that cannot be built, plus a production environment that needs
        nothing else."""
        from baldur.factory.registry import ProviderRegistry
        from baldur.settings.audit import reset_audit_settings

        built: list[int] = []

        def _host_provider():
            built.append(1)
            raise ImportError("the host's dead-letter store is not installed")

        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://prod:6379/0")
        monkeypatch.setenv("BALDUR_AUDIT_ENABLED", "false")
        for name in ("BALDUR_SQL_DSN", "DJANGO_SETTINGS_MODULE", "BALDUR_DLQ_BACKEND"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(
            "baldur.core.entitlement.is_entitlement_active", lambda: False
        )
        reset_audit_settings()

        with ProviderRegistry.failed_op_repo.snapshot():
            ProviderRegistry.failed_op_repo.register("django", _host_provider)
            ProviderRegistry.failed_op_repo.clear_instances()
            yield built
        reset_audit_settings()

    def test_an_override_selecting_the_unbuildable_provider_refuses_boot(
        self, unbuildable_host_provider, patched_eager_backend, monkeypatch
    ):
        """
        Purpose:
            ``BALDUR_DLQ_BACKEND=django`` names the host's provider, so boot
            builds it and refuses when it cannot be built (SC5).
        Expected:
            - init() raises ConfigurationError naming the row, the provider
              and the override
        """
        from baldur import bootstrap

        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "django")

        with _scaffold_init_subdeps(), pytest.raises(ConfigurationError) as exc_info:
            bootstrap.init()

        message = str(exc_info.value)
        assert "ProviderRegistry.failed_op_repo selected backend 'django'" in message
        assert "BALDUR_DLQ_BACKEND" in message
        assert unbuildable_host_provider == [1]

    def test_without_the_override_the_chain_never_reaches_the_provider(
        self, unbuildable_host_provider, patched_eager_backend
    ):
        """
        Purpose:
            The dead-letter chain is ``redis > sql > memory``; it never probes
            ``django``, so the unbuildable provider is unreachable (SC5).
        Expected:
            - init() returns; the dead-letter store is redis; nothing was built
        """
        from baldur import bootstrap
        from baldur.factory.registry import ProviderRegistry

        with _scaffold_init_subdeps():
            bootstrap.init()

        assert ProviderRegistry.failed_op_repo.get_default_name() == "redis"
        assert unbuildable_host_provider == []


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
            assert ProviderRegistry.recovery_session_repo.get_default_name() == "sql"

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
