"""The auto-registered pg-admin providers carry a vendor-aware probe.

``PgAdmin.is_available()`` gained an injectable probe; this module covers the
half that decides whether a real deployment benefits from it — what the two
factories in ``discover_pg_admin_adapters`` actually inject, and what the
resolved instance answers on the posture the defect was found on: a Django
install whose default alias is not a PostgreSQL.

Neither probe is a reachability test. The Django one reads the alias's vendor
string, which Django exposes without opening a connection; the DSN one reads
the resolved dialect. Both are configuration facts, so a deployment with an
unreachable-but-configured postgres still answers True and still surfaces its
outage through the compute's own error path.

The environment precondition is derived rather than enumerated. Every
``BALDUR_*`` variable has to be gone for these assertions to be about the
zero-config posture — ``BALDUR_PG_ADMIN_PROVIDER`` alone reroutes the whole
provider chain, and ``BALDUR_TEST_MODE`` short-circuits the pool-status
compute before it consults anything. An authored list drifts, and
``monkeypatch.delenv`` defaults to raising on a machine that does not have
the variable at all.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.postgres.admin import PgAdmin
from baldur.factory import ProviderRegistry
from baldur.factory.adapters import (
    _configured_sql_dialect_is_postgres,
    _django_default_alias_is_postgres,
    discover_pg_admin_adapters,
)
from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
    DatabaseHealthProvider,
)
from baldur.interfaces.pool_info import PoolInfoProvider
from baldur.services.precomputed_cache.compute_functions import compute_pool_status

POOL_STATUS_COMPUTE_FAILED = "precomputed_cache.pool_status_compute_failed"

_A_SQLITE_DSN = "sqlite:///./baldur-753.db"


@pytest.fixture
def zero_config_env(monkeypatch):
    """No ``BALDUR_*`` variable survives, and the settings singletons agree.

    The strip is derived from the live environment: an authored list cannot
    stay complete, and the two settings objects the DSN probe reads are
    cached, so clearing the variables without resetting them would leave the
    probe answering from a pre-strip snapshot.
    """
    from baldur.settings.postgres import reset_postgres_settings
    from baldur.settings.sql import reset_sql_settings

    for key in [name for name in os.environ if name.startswith("BALDUR_")]:
        monkeypatch.delenv(key, raising=False)
    reset_sql_settings()
    reset_postgres_settings()
    yield
    reset_sql_settings()
    reset_postgres_settings()


class TestPgAdminResolvedProviderBehavior:
    """A Django + sqlite deployment stops claiming it can answer PG admin SQL."""

    def test_the_django_probe_reports_the_default_alias_is_not_a_postgres(
        self, zero_config_env
    ):
        """The test project runs on in-memory sqlite — the quickstart posture
        the four log lines were measured on."""
        assert _django_default_alias_is_postgres() is False

    def test_the_registry_resolved_django_provider_declines(self, zero_config_env):
        """What a framework boot resolves, not a hand-built instance: the
        probe has to survive the factory to reach production."""
        discover_pg_admin_adapters()

        provider = ProviderRegistry.pg_admin.get("django")

        assert isinstance(provider, PgAdmin)
        assert provider.is_available() is False

    def test_the_pool_status_compute_omits_pg_stats_when_the_provider_declines(
        self, zero_config_env
    ):
        """The ERROR-with-traceback this removes: an ungated
        ``pg_stat_activity`` against sqlite raised once per refresh pass.

        The pool and connection providers are held constant so the asserted
        outcome turns on the pg-admin flag and nothing else; the pg-admin
        instance itself is the real registry-resolved one.
        """
        discover_pg_admin_adapters()
        pg_admin = ProviderRegistry.pg_admin.get("django")

        pool_info = MagicMock(spec=PoolInfoProvider)
        pool_info.get_pool_info.return_value = {"pool_type": "django_default"}
        db_health = MagicMock(spec=DatabaseHealthProvider)
        db_health.check_connection.return_value = DatabaseConnectionInfo(
            alias="default", vendor="sqlite", is_usable=True
        )

        with (
            patch.object(ProviderRegistry.pool_info, "get", return_value=pool_info),
            patch.object(
                ProviderRegistry.database_health, "get", return_value=db_health
            ),
            patch.object(ProviderRegistry.pg_admin, "get", return_value=pg_admin),
            capture_logs() as logs,
        ):
            response = compute_pool_status()

        assert "pg_stats" not in response
        assert response["status"] == "healthy"
        assert response["connection_usable"] is True
        assert [
            entry for entry in logs if entry.get("event") == POOL_STATUS_COMPUTE_FAILED
        ] == []

    def test_a_postgres_backed_provider_still_carries_the_pg_stats_block(
        self, zero_config_env
    ):
        """The other half of the flag: the guard is a gate, not a removal.

        A False-only pair of assertions would pass a change that dropped the
        PG block outright.
        """
        from baldur.interfaces.pg_admin import ConnectionStats, PgAdminProvider

        pool_info = MagicMock(spec=PoolInfoProvider)
        pool_info.get_pool_info.return_value = {}
        db_health = MagicMock(spec=DatabaseHealthProvider)
        db_health.check_connection.return_value = DatabaseConnectionInfo(
            alias="default", vendor="postgresql", is_usable=True
        )
        pg_admin = MagicMock(spec=PgAdminProvider)
        pg_admin.is_available.return_value = True
        pg_admin.get_connection_stats.return_value = ConnectionStats(
            total_connections=7, active=2, idle=4, idle_in_transaction=1
        )

        with (
            patch.object(ProviderRegistry.pool_info, "get", return_value=pool_info),
            patch.object(
                ProviderRegistry.database_health, "get", return_value=db_health
            ),
            patch.object(ProviderRegistry.pg_admin, "get", return_value=pg_admin),
        ):
            response = compute_pool_status()

        assert response["pg_stats"]["total_connections"] == 7
        assert response["pg_stats"]["idle_in_transaction"] == 1


class TestConfiguredSqlDialectProbeBehavior:
    """The DSN-backed provider's probe, including where it cannot bite.

    The sql instance exists only where a DSN or a ``BALDUR_POSTGRES_*``
    component was set, or where an operator forced the provider by name. With
    no DSN, the components fall back to their defaults and the resolved DSN
    is synthesised as ``postgresql://…`` — so the dialect term is True by
    construction there, and only an explicit non-postgres DSN (or dialect
    override) makes it decline. That asymmetry is the documented semantics,
    not an oversight, and pinning it keeps a future "just add a dialect test
    to the chain" from being written against a term that cannot be False.
    """

    def test_a_component_derived_dsn_resolves_to_postgres(self, zero_config_env):
        assert _configured_sql_dialect_is_postgres() is True

    def test_an_explicit_sqlite_dsn_makes_the_probe_decline(
        self, monkeypatch, zero_config_env
    ):
        from baldur.settings.sql import reset_sql_settings

        monkeypatch.setenv("BALDUR_SQL_DSN", _A_SQLITE_DSN)
        reset_sql_settings()

        assert _configured_sql_dialect_is_postgres() is False
