"""
Test Factories for Baldur Tests.

This package provides Factory-pattern helpers that build Mock objects and data
for tests.

Main components:
- TestDataFactory: builds test data (FailedOperationData, CircuitBreakerStateData, etc.)
- MockRedisClient: unified Redis mock client
- InMemoryCircuitBreakerRepository: in-memory repository for CB state
- InMemoryDLQRepository: in-memory repository for DLQ entries
- Constants: test constants (Domains, Services, FailureTypes, Status, CircuitState)
- repo_root / src_root: location-robust repo-root / src-root resolution

Usage:
    from tests.factories import TestDataFactory, MockRedisClient, Domains, Status
    from tests.factories.repositories import InMemoryCircuitBreakerRepository

    # Build test data (using constants)
    cb_state = TestDataFactory.circuit_breaker_state(service_name="payment-api")
    failed_op = TestDataFactory.failed_operation(domain=Domains.ORDER, status=Status.PENDING)

    # Use the Redis mock
    redis = MockRedisClient()
    redis.set("key", "value")

    # Use a repository
    repo = InMemoryCircuitBreakerRepository()
    state = repo.get_or_create("test_service")
"""

# Constants (constants.py)
# Builders (builders.py)
from tests.factories.builders import (
    CanaryRolloutBuilder,
    CanaryStageBuilder,
    ChaosExperimentBuilder,
    CircuitBreakerStateBuilder,
    FailedOperationBuilder,
    MockRequestBuilder,
    MockServiceBuilder,
    WatchdogConfigBuilder,
)
from tests.factories.constants import (
    CanaryCluster,
    CanaryPercentage,
    CeleryTestConfig,
    ChaosIntensity,
    CircuitState,
    DatabaseTestConfig,
    DefaultValues,
    Domains,
    FailureTypes,
    RBACRole,
    RedisTestConfig,
    Services,
    Status,
    TestConstants,
)
from tests.factories.data_factory import (
    MockCanaryRolloutData,
    MockCircuitBreakerStateData,
    MockFailedOperationData,
    TestDataFactory,
)
from tests.factories.execution_mode_helpers import dry_run_active

# Integration (integration.py)
from tests.factories.integration import (
    CeleryTaskRunner,
    IntegrationTestContext,
    RealDatabaseFactory,
    RealRedisClientFactory,
)
from tests.factories.paths import repo_root, src_root
from tests.factories.redis import (
    FakeRawRedis,
    FakeRedisAdapter,
    MockDistributedLock,
    MockPipeline,
    MockRedisClient,
)
from tests.factories.repositories import (
    InMemoryCircuitBreakerRepository,
    InMemoryDLQRepository,
    InMemoryRateLimitTracker,
    MockDLQEntry,
)
from tests.factories.time_helpers import (
    MockSleep,
    freeze_time,
    get_fixed_datetime,
    make_datetime_range,
    mock_sleep,
)

__all__ = [
    # Constants
    "DefaultValues",
    "Domains",
    "Services",
    "FailureTypes",
    "Status",
    "CircuitState",
    "TestConstants",
    "CeleryTestConfig",
    "RedisTestConfig",
    "DatabaseTestConfig",
    "CanaryCluster",
    "CanaryPercentage",
    "ChaosIntensity",
    "RBACRole",
    # Data Factory
    "TestDataFactory",
    "MockCircuitBreakerStateData",
    "MockFailedOperationData",
    "MockCanaryRolloutData",
    # Redis
    "MockRedisClient",
    "MockPipeline",
    "MockDistributedLock",
    "FakeRawRedis",
    "FakeRedisAdapter",
    # Repositories
    "InMemoryCircuitBreakerRepository",
    "InMemoryRateLimitTracker",
    "InMemoryDLQRepository",
    "MockDLQEntry",
    # Execution Mode / Dry-run Helpers
    "dry_run_active",
    # Time Helpers
    "freeze_time",
    "mock_sleep",
    "MockSleep",
    "get_fixed_datetime",
    "make_datetime_range",
    # Builders
    "CircuitBreakerStateBuilder",
    "FailedOperationBuilder",
    "CanaryRolloutBuilder",
    "MockServiceBuilder",
    "MockRequestBuilder",
    "CanaryStageBuilder",
    "ChaosExperimentBuilder",
    "WatchdogConfigBuilder",
    # Integration
    "RealRedisClientFactory",
    "RealDatabaseFactory",
    "CeleryTaskRunner",
    "IntegrationTestContext",
    # Paths
    "repo_root",
    "src_root",
]
