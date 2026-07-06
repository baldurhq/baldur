"""
Integration test fixtures for baldur framework.

Provides real infrastructure connections (Redis, PostgreSQL, Celery)
for Docker-based integration testing.

Usage:
    docker-compose -f docker-compose.test.yml up -d
    pytest tests/integration/ -v -m "not requires_db"  # Redis only
    pytest tests/integration/ -v                         # All
"""

import os

import pytest

# =============================================================================
# Infrastructure Configuration
# =============================================================================


class RedisTestConfig:
    """Redis test connection configuration."""

    DEFAULT_HOST = "localhost"
    DEFAULT_PORT = 6379
    TEST_PORT = 16379
    DEFAULT_DB = 0
    TEST_DB = 1

    @property
    def test_redis_url(self) -> str:
        return f"redis://{self.DEFAULT_HOST}:{self.TEST_PORT}/{self.TEST_DB}"


class DatabaseTestConfig:
    """PostgreSQL test connection configuration."""

    DEFAULT_HOST = "localhost"
    DEFAULT_PORT = 15432
    DEFAULT_DB = "baldur_test"
    DEFAULT_USER = "postgres"
    DEFAULT_PASSWORD = "postgres"


# =============================================================================
# Auto-skip logic for infrastructure-dependent tests
# =============================================================================


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests with requires_db, requires_redis, requires_kafka, or requires_otel markers."""
    db_available = os.environ.get("TEST_DB_AVAILABLE", "").lower() == "true"
    redis_available = os.environ.get("TEST_REDIS_AVAILABLE", "").lower() == "true"
    kafka_available = os.environ.get("TEST_KAFKA_AVAILABLE", "").lower() == "true"
    otel_available = os.environ.get("TEST_OTEL_AVAILABLE", "").lower() == "true"

    if not db_available and not os.environ.get("TEST_DB_AVAILABLE"):
        db_available = _check_db_connection()

    if not redis_available and not os.environ.get("TEST_REDIS_AVAILABLE"):
        redis_available = _check_redis_connection()

    if not kafka_available and not os.environ.get("TEST_KAFKA_AVAILABLE"):
        kafka_available = _check_kafka_connection()

    if not otel_available and not os.environ.get("TEST_OTEL_AVAILABLE"):
        otel_available = _check_otel_connection()

    skip_db = pytest.mark.skip(
        reason="Database not available (set TEST_DB_AVAILABLE=true)"
    )
    skip_redis = pytest.mark.skip(
        reason="Redis not available (set TEST_REDIS_AVAILABLE=true)"
    )
    skip_kafka = pytest.mark.skip(
        reason="Kafka not available (set TEST_KAFKA_AVAILABLE=true)"
    )
    skip_otel = pytest.mark.skip(
        reason="OTEL Collector not available (set TEST_OTEL_AVAILABLE=true)"
    )

    for item in items:
        markers = [m.name for m in item.iter_markers()]
        if not db_available and "requires_db" in markers:
            item.add_marker(skip_db)
        if not redis_available and "requires_redis" in markers:
            item.add_marker(skip_redis)
        if not kafka_available and "requires_kafka" in markers:
            item.add_marker(skip_kafka)
        if not otel_available and "requires_otel" in markers:
            item.add_marker(skip_otel)


def _pg_connect(**extra):
    """Open a psycopg2 connection to the test database.

    Honors a ``BALDUR_SQL_DSN`` / ``DATABASE_URL`` env DSN first (so the suite
    reaches PostgreSQL by service name when run inside the compose network),
    falling back to the localhost-exposed test config for host-side runs.
    """
    import psycopg2

    dsn = os.environ.get("BALDUR_SQL_DSN") or os.environ.get("DATABASE_URL")
    if dsn:
        return psycopg2.connect(dsn, **extra)

    config = DatabaseTestConfig()
    return psycopg2.connect(
        host=config.DEFAULT_HOST,
        port=config.DEFAULT_PORT,
        database=config.DEFAULT_DB,
        user=config.DEFAULT_USER,
        password=config.DEFAULT_PASSWORD,
        **extra,
    )


def _check_db_connection() -> bool:
    """Check PostgreSQL connection."""
    try:
        conn = _pg_connect(connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


def _check_kafka_connection() -> bool:
    """Check Kafka broker connection."""
    try:
        from confluent_kafka.admin import AdminClient

        bootstrap_servers = os.environ.get(
            "BALDUR_KAFKA_BOOTSTRAP_SERVERS", "localhost:19092"
        )
        admin = AdminClient({"bootstrap.servers": bootstrap_servers})
        metadata = admin.list_topics(timeout=5)
        return metadata is not None
    except Exception:
        return False


def _check_otel_connection() -> bool:
    """Check OTEL Collector health endpoint."""
    try:
        import requests

        endpoint = os.environ.get("COLLECTOR_HEALTH_ENDPOINT", "http://localhost:13133")
        response = requests.get(endpoint, timeout=2)
        return response.status_code == 200
    except Exception:
        return False


def _check_redis_connection() -> bool:
    """Check Redis connection (REDIS_URL env first, then test port 16379, then 6379)."""
    try:
        import redis

        # Honor REDIS_URL so the suite reaches Redis by service name when run
        # inside the compose network (matches the test fixtures). Falls back to
        # the localhost-exposed test ports for host-side runs.
        env_url = os.environ.get("REDIS_URL")
        if env_url:
            client = redis.from_url(env_url, socket_connect_timeout=2, socket_timeout=2)
            client.ping()
            client.close()
            return True

        config = RedisTestConfig()

        try:
            client = redis.Redis(
                host=config.DEFAULT_HOST,
                port=config.TEST_PORT,
                db=config.TEST_DB,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            client.close()
            return True
        except Exception:
            pass

        client = redis.Redis(
            host=config.DEFAULT_HOST,
            port=config.DEFAULT_PORT,
            db=config.DEFAULT_DB,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        client.close()
        return True
    except Exception:
        return False


# =============================================================================
# Redis Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def redis_client():
    """Real Redis client for integration tests (port 16379)."""
    import redis

    config = RedisTestConfig()
    redis_url = os.environ.get("REDIS_URL", config.test_redis_url)
    client = redis.from_url(redis_url, decode_responses=True)

    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip(
            "Redis not available. Run: docker-compose -f docker-compose.test.yml up -d"
        )

    yield client
    client.flushdb()


@pytest.fixture
def redis_circuit_breaker_repository(redis_client):
    """Real Redis-based Circuit Breaker Repository."""
    from baldur.adapters.redis.circuit_breaker import (
        RedisCircuitBreakerStateRepository,
    )
    from baldur.adapters.resilient.backend import ResilientStorageBackend
    from baldur.settings.resilient_storage import ResilientStorageSettings

    config_redis = RedisTestConfig()
    redis_url = os.environ.get("REDIS_URL", config_redis.test_redis_url)
    settings = ResilientStorageSettings(
        redis_url=redis_url,
        key_prefix="test:baldur:",
        use_dynamic_prefix=False,
        allow_memory_only=True,
    )
    backend = ResilientStorageBackend(settings=settings)

    yield RedisCircuitBreakerStateRepository(backend=backend)

    for key in redis_client.keys("test:baldur:*"):
        redis_client.delete(key)


@pytest.fixture
def redis_dlq_repository(redis_client):
    """Real Redis-based DLQ Repository."""
    from baldur.adapters.redis.dlq import RedisDLQRepository
    from baldur.adapters.resilient.backend import ResilientStorageBackend
    from baldur.settings.resilient_storage import ResilientStorageSettings

    config_redis = RedisTestConfig()
    redis_url = os.environ.get("REDIS_URL", config_redis.test_redis_url)
    settings = ResilientStorageSettings(
        redis_url=redis_url,
        key_prefix="test:baldur:dlq:",
        use_dynamic_prefix=False,
        allow_memory_only=True,
    )
    backend = ResilientStorageBackend(settings=settings)

    yield RedisDLQRepository(backend=backend)

    for key in redis_client.keys("test:baldur:dlq:*"):
        redis_client.delete(key)


@pytest.fixture(scope="session")
def docker_redis_client():
    """Docker Compose Redis client (port 6379)."""
    import redis

    config = RedisTestConfig()
    client = redis.Redis(
        host=config.DEFAULT_HOST,
        port=config.DEFAULT_PORT,
        db=config.TEST_DB,
        decode_responses=True,
    )

    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("Docker Redis not available. Run: docker-compose up -d")

    yield client
    client.flushdb()


@pytest.fixture
def clean_redis(docker_redis_client):
    """Clean Redis before/after each test."""
    docker_redis_client.flushdb()
    yield docker_redis_client
    docker_redis_client.flushdb()


# =============================================================================
# PostgreSQL Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def docker_db_connection():
    """Docker Compose PostgreSQL connection."""
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        pytest.skip("psycopg2 not installed")

    try:
        conn = _pg_connect(connect_timeout=3)
        yield conn
        conn.close()
    except Exception:
        pytest.skip("Docker PostgreSQL not available. Run: docker-compose up -d")


# =============================================================================
# Celery Fixtures
# =============================================================================


@pytest.fixture
def celery_eager_mode(settings):
    """Enable Celery eager mode (synchronous execution)."""
    original_always_eager = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
    original_eager_propagates = getattr(settings, "CELERY_TASK_EAGER_PROPAGATES", False)

    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True

    yield

    settings.CELERY_TASK_ALWAYS_EAGER = original_always_eager
    settings.CELERY_TASK_EAGER_PROPAGATES = original_eager_propagates


@pytest.fixture
def celery_async_mode(settings):
    """Disable Celery eager mode (requires running worker)."""
    original_always_eager = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)

    settings.CELERY_TASK_ALWAYS_EAGER = False

    yield

    settings.CELERY_TASK_ALWAYS_EAGER = original_always_eager
