"""
HealthCheckService unit tests.

Health Check business logic service tests.
"""

from unittest.mock import MagicMock, patch

from baldur.interfaces.database_health import DatabaseConnectionInfo
from baldur.services.health_check import (
    DatabaseCheck,
    HealthCheckService,
    PoolHealthSummary,
    PoolInfo,
    ReadinessStatus,
    SystemHealthSummary,
    get_health_check_service,
)

_REGISTRY_PATH = "baldur.factory.registry.ProviderRegistry"


def _make_mock_db_provider(vendor="postgresql", is_usable=True, aliases=None):
    """Create a mock DatabaseHealthProvider."""
    provider = MagicMock()
    provider.check_connection.return_value = DatabaseConnectionInfo(
        alias="default",
        vendor=vendor,
        is_usable=is_usable,
    )
    provider.list_aliases.return_value = aliases or ["default"]
    return provider


class TestHealthCheckService:
    """HealthCheckService unit tests."""

    def setup_method(self):
        """Create service instance before each test."""
        self.service = HealthCheckService()

    # =========================================================================
    # check_database Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_database_success(self, mock_db_health):
        """Healthy DB: is_usable=True flows through to is_connected=True."""
        mock_db_health.get.return_value = _make_mock_db_provider(is_usable=True)

        result = self.service.check_database("default")

        assert isinstance(result, DatabaseCheck)
        assert result.alias == "default"
        assert result.vendor == "postgresql"
        assert result.is_connected is True
        assert result.is_usable is True
        assert result.error is None
        assert result.latency_ms is not None
        assert result.latency_ms >= 0

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_database_unusable_returns_disconnected(self, mock_db_health):
        """473 D2: provider returns is_usable=False without raising →
        is_connected=False (single source of truth)."""
        mock_db_health.get.return_value = _make_mock_db_provider(is_usable=False)

        result = self.service.check_database("default")

        assert isinstance(result, DatabaseCheck)
        assert result.is_connected is False
        assert result.is_usable is False
        assert result.error is None  # no exception raised

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_database_connection_failure(self, mock_db_health):
        """check_connection raising an exception → is_connected=False + error."""
        provider = _make_mock_db_provider()
        provider.check_connection.side_effect = Exception("Connection refused")
        mock_db_health.get.return_value = provider

        result = self.service.check_database("default")

        assert isinstance(result, DatabaseCheck)
        assert result.alias == "default"
        assert result.is_connected is False
        assert result.is_usable is False
        assert result.error == "Connection refused"

    # =========================================================================
    # check_all_databases Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_all_databases(self, mock_db_health):
        """Iterates list_aliases and returns one DatabaseCheck per alias."""
        mock_db_health.get.return_value = _make_mock_db_provider(
            aliases=["default", "replica"],
        )

        results = self.service.check_all_databases()

        assert len(results) == 2
        assert all(isinstance(r, DatabaseCheck) for r in results)

    # =========================================================================
    # check_connection_pool Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_connection_pool_healthy(self, mock_db_health):
        """Healthy pool."""
        mock_db_health.get.return_value = _make_mock_db_provider()

        result = self.service.check_connection_pool("default")

        assert isinstance(result, PoolInfo)
        assert result.alias == "default"
        assert result.vendor == "postgresql"
        assert result.is_usable is True
        assert result.status == "healthy"
        assert result.error is None

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_connection_pool_degraded(self, mock_db_health):
        """Pool reporting is_usable=False → status='degraded'."""
        mock_db_health.get.return_value = _make_mock_db_provider(is_usable=False)

        result = self.service.check_connection_pool("default")

        assert isinstance(result, PoolInfo)
        assert result.is_usable is False
        assert result.status == "degraded"

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_check_connection_pool_error(self, mock_db_health):
        """Pool provider raising → status='error'."""
        mock_db_health.get.side_effect = Exception("Pool error")

        result = self.service.check_connection_pool("default")

        assert isinstance(result, PoolInfo)
        assert result.is_usable is False
        assert result.status == "error"
        assert result.error == "Pool error"

    # =========================================================================
    # get_pool_health Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_pool_health_healthy(self, mock_db_health):
        """Healthy pool summary."""
        mock_db_health.get.return_value = _make_mock_db_provider()

        result = self.service.get_pool_health()

        assert isinstance(result, PoolHealthSummary)
        assert result.status == "healthy"
        assert result.error is None

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_pool_health_error(self, mock_db_health):
        """Pool error summary."""
        mock_db_health.get.side_effect = Exception("Pool error")

        result = self.service.get_pool_health()

        assert isinstance(result, PoolHealthSummary)
        assert result.status == "error"
        assert result.error == "Pool error"

    # =========================================================================
    # get_readiness Tests
    # =========================================================================

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_readiness_ready(self, mock_db_health):
        """All DBs healthy → ready."""
        mock_db_health.get.return_value = _make_mock_db_provider(aliases=["default"])

        result = self.service.get_readiness()

        assert isinstance(result, ReadinessStatus)
        assert result.status == "ready"
        assert result.is_ready is True
        assert result.checks["database_default"] == "ready"

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_readiness_not_ready(self, mock_db_health):
        """check_connection raises → not_ready."""
        provider = _make_mock_db_provider(aliases=["default"])
        provider.check_connection.side_effect = Exception("Connection refused")
        mock_db_health.get.return_value = provider

        result = self.service.get_readiness()

        assert isinstance(result, ReadinessStatus)
        assert result.status == "not_ready"
        assert result.is_ready is False
        assert result.checks["database_default"] == "not_ready"

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_get_readiness_not_ready_when_db_unusable(self, mock_db_health):
        """473 D2 collateral: provider returns is_usable=False without raising
        → readiness reports not_ready (no false-positive ready emission)."""
        mock_db_health.get.return_value = _make_mock_db_provider(
            is_usable=False, aliases=["default"]
        )

        result = self.service.get_readiness()

        assert result.status == "not_ready"
        assert result.is_ready is False
        assert result.checks["database_default"] == "not_ready"

    # =========================================================================
    # get_overall_health Tests
    # =========================================================================

    @patch("baldur.utils.time.utc_now")
    @patch.object(HealthCheckService, "_get_circuit_breaker_count")
    @patch.object(HealthCheckService, "check_database")
    def test_get_overall_health_healthy(self, mock_check_db, mock_get_count, mock_now):
        """Healthy DB → status='healthy'."""
        mock_check_db.return_value = DatabaseCheck(
            alias="default",
            vendor="postgresql",
            is_connected=True,
            is_usable=True,
        )
        mock_get_count.return_value = 5
        mock_now.return_value.isoformat.return_value = "2025-12-19T00:00:00Z"

        result = self.service.get_overall_health()

        assert isinstance(result, SystemHealthSummary)
        assert result.status == "healthy"
        assert result.checks["database"] == "healthy"
        assert result.checks["circuit_breaker"] == "enabled"
        assert result.services_count == 5

    @patch("baldur.utils.time.utc_now")
    @patch.object(HealthCheckService, "check_database")
    def test_get_overall_health_unhealthy_when_db_unusable(
        self, mock_check_db, mock_now
    ):
        """473 D7 axis 1 (b): is_usable=False → status='unhealthy'."""
        mock_check_db.return_value = DatabaseCheck(
            alias="default",
            is_connected=False,
            is_usable=False,
            error="Connection refused",
        )
        mock_now.return_value.isoformat.return_value = "2025-12-19T00:00:00Z"

        with patch("baldur.services.health_check.set_health_status") as mock_set_status:
            result = self.service.get_overall_health()

        assert isinstance(result, SystemHealthSummary)
        assert result.status == "unhealthy"
        assert result.checks["database"] == "unhealthy"
        assert result.services_count == 0
        # 473 D7 mock-call assertion: set_health_status called with "unhealthy"
        # so the metric layer (_STATUS_MAP) translates to numeric 2.
        mock_set_status.assert_called_with("overall", "unhealthy")

    # =========================================================================
    # Liveness/Readiness Helper Tests
    # =========================================================================

    def test_is_alive_always_true(self):
        """is_alive is always True."""
        assert self.service.is_alive() is True

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_is_ready_true(self, mock_db_health):
        """Healthy DB → is_ready True."""
        mock_db_health.get.return_value = _make_mock_db_provider(aliases=["default"])

        assert self.service.is_ready() is True

    @patch(f"{_REGISTRY_PATH}.database_health")
    def test_is_ready_false(self, mock_db_health):
        """check_connection raises → is_ready False."""
        provider = _make_mock_db_provider(aliases=["default"])
        provider.check_connection.side_effect = Exception("Connection refused")
        mock_db_health.get.return_value = provider

        assert self.service.is_ready() is False


class TestGetHealthCheckService:
    """get_health_check_service factory tests."""

    def test_returns_singleton(self):
        """Singleton instance returned across calls."""
        import baldur.services.health_check as module

        module._health_check_service = None

        service1 = get_health_check_service()
        service2 = get_health_check_service()

        assert service1 is service2
        assert isinstance(service1, HealthCheckService)


class TestDataClasses:
    """Data class tests."""

    def test_database_check_to_dict(self):
        """DatabaseCheck.to_dict()."""
        check = DatabaseCheck(
            alias="default",
            vendor="postgresql",
            is_connected=True,
            is_usable=True,
            latency_ms=1.5,
        )

        result = check.to_dict()

        assert result["alias"] == "default"
        assert result["vendor"] == "postgresql"
        assert result["is_connected"] is True
        assert result["latency_ms"] == 1.5

    def test_health_status_to_dict(self):
        """SystemHealthSummary.to_dict()."""
        status = SystemHealthSummary(
            status="healthy",
            checks={"database": "healthy"},
            services_count=5,
            timestamp="2025-12-19T00:00:00Z",
        )

        result = status.to_dict()

        assert result["status"] == "healthy"
        assert result["checks"]["database"] == "healthy"
        assert result["services_count"] == 5

    def test_readiness_status_to_dict(self):
        """ReadinessStatus.to_dict()."""
        status = ReadinessStatus(
            status="ready",
            checks={"database_default": "ready"},
            is_ready=True,
        )

        result = status.to_dict()

        assert result["status"] == "ready"
        assert result["is_ready"] is True

    def test_pool_health_summary_to_dict(self):
        """PoolHealthSummary.to_dict()."""
        status = PoolHealthSummary(
            status="healthy",
            pool_info={"alias": "default"},
        )

        result = status.to_dict()

        assert result["status"] == "healthy"
        assert result["pool_info"]["alias"] == "default"
