"""The operator-facing pool status keeps the half its backend can answer.

``StressTestService.get_pool_status`` assembles the same five fields as the
precomputed-cache twin, from the same providers — but read the PG section
unconditionally. On a backend that cannot run ``pg_stat_activity`` the read
raised, the method's ``except`` logged an ERROR with a traceback, and the
*whole* payload collapsed to ``status="error"``: the operator lost the
SQLAlchemy pool section and the connection-usable flag too, not just the PG
numbers they were never going to get.

Gating the PG block on the provider's availability flag is the guard the twin
already applied, and it needs no new response vocabulary — ``pg_stats``
already defaults to an empty dict and the omit-the-PG-keys contract is what
the no-op provider's docstring settles.

Both halves are asserted. A test that only drove the flag False would pass a
change that deleted the PG block outright, which is a different bug wearing
the same green.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.factory import ProviderRegistry
from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
    DatabaseHealthProvider,
)
from baldur.interfaces.pg_admin import ConnectionStats, PgAdminProvider
from baldur.interfaces.pool_info import PoolInfoProvider
from baldur.services.stress_test_service.service import StressTestService

STRESS_FAILED = "stress_test_service.failed"

_A_POOL_INFO = {"pool_type": "django_default", "pool_exhausted": False}


@pytest.fixture
def held_constant():
    """Pin the two providers the availability flag does not decide.

    ``get_pool_status`` reads three registry slots; only the pg-admin one is
    under test, so the other two answer the same way in every case and the
    asserted outcome turns on the flag alone.
    """
    pool_info = MagicMock(spec=PoolInfoProvider)
    pool_info.get_pool_info.return_value = dict(_A_POOL_INFO)
    db_health = MagicMock(spec=DatabaseHealthProvider)
    db_health.check_connection.return_value = DatabaseConnectionInfo(
        alias="default", vendor="sqlite", is_usable=True
    )

    with (
        patch.object(ProviderRegistry.pool_info, "get", return_value=pool_info),
        patch.object(ProviderRegistry.database_health, "get", return_value=db_health),
    ):
        yield


def _repository(*, available: bool) -> MagicMock:
    """A pg-admin double that answers the flag and reacts accordingly.

    When it declines, ``get_connection_stats`` raises the way a real
    ``pg_stat_activity`` does against a non-PostgreSQL backend — so a guard
    that failed to fire could not produce a clean payload by accident.
    """
    repo = MagicMock(spec=PgAdminProvider)
    repo.is_available.return_value = available
    if available:
        repo.get_connection_stats.return_value = ConnectionStats(
            total_connections=11, active=3, idle=7, idle_in_transaction=1
        )
    else:
        repo.get_connection_stats.side_effect = RuntimeError(
            'relation "pg_stat_activity" does not exist'
        )
    return repo


class TestGetPoolStatusAvailabilityBehavior:
    """The payload survives a backend that cannot answer the PG half."""

    def test_a_declining_provider_still_yields_the_pool_payload(self, held_constant):
        service = StressTestService(repository=_repository(available=False))

        result = service.get_pool_status()

        assert result.status == "healthy"
        assert result.sqlalchemy_pool == _A_POOL_INFO
        assert result.connection_usable is True
        assert result.pg_stats == {}

    def test_a_declining_provider_is_never_asked_for_connection_stats(
        self, held_constant
    ):
        """The flag is consulted before the read, not after it raises."""
        repo = _repository(available=False)

        StressTestService(repository=repo).get_pool_status()

        repo.is_available.assert_called_once_with()
        repo.get_connection_stats.assert_not_called()

    def test_a_declining_provider_produces_no_failure_record(self, held_constant):
        """The ERROR-with-traceback per operator request that this removes."""
        service = StressTestService(repository=_repository(available=False))

        with capture_logs() as logs:
            service.get_pool_status()

        assert [entry for entry in logs if entry.get("event") == STRESS_FAILED] == []

    def test_an_available_provider_still_carries_every_pg_stat(self, held_constant):
        """The postgres half is unchanged — the guard gates, never removes."""
        service = StressTestService(repository=_repository(available=True))

        result = service.get_pool_status()

        assert result.pg_stats == {
            "total_connections": 11,
            "active": 3,
            "idle": 7,
            "idle_in_transaction": 1,
        }
        assert result.status == "healthy"

    def test_a_genuine_failure_still_collapses_to_the_error_payload(
        self, held_constant
    ):
        """The except arm the guard must not silence: a provider that claims
        it can answer and then cannot is a real failure, and still says so.
        """
        repo = MagicMock(spec=PgAdminProvider)
        repo.is_available.return_value = True
        repo.get_connection_stats.side_effect = RuntimeError("connection refused")
        service = StressTestService(repository=repo)

        with capture_logs() as logs:
            result = service.get_pool_status()

        assert result.status == "error"
        assert "connection refused" in result.error
        assert [
            entry
            for entry in logs
            if entry.get("event") == STRESS_FAILED and entry.get("log_level") == "error"
        ]
