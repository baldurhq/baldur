"""
Test Constants.

테스트에서 사용하는 상수들을 중앙에서 관리합니다.
하드코딩 제거 및 일관성 유지를 위해 사용합니다.

Usage:
    from tests.factories.constants import DefaultValues, Domains, Services

    # 클래스 속성으로 접근
    domain = DefaultValues.DOMAIN_PAYMENT
    service = DefaultValues.SERVICE_TEST

    # 또는 개별 클래스 사용
    domain = Domains.PAYMENT
    service = Services.TEST
"""


class Domains:
    """도메인 상수."""

    ORDER = "order"
    PAYMENT = "payment"
    NOTIFICATION = "notification"
    EXTERNAL = "external_service"
    POINT = "point"
    SHIPPING = "shipping"
    WEBHOOK = "webhook"


class Services:
    """서비스 이름 상수."""

    PAYMENT_API = "payment-api"
    EXTERNAL_GATEWAY = "external-gateway"
    ORDER_SERVICE = "order-service"
    TEST = "test_service"
    TOSS_PAYMENTS = "toss-payments"
    NOTIFICATION = "notification-service"


class FailureTypes:
    """실패 유형 상수."""

    NETWORK = "network"
    TIMEOUT = "timeout"
    PG_TIMEOUT = "PG_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    REDIS_ERROR = "REDIS_ERROR"


from baldur.interfaces.repositories import CircuitBreakerStateEnum


class Status:
    """상태 상수."""

    PENDING = "pending"
    RESOLVED = "resolved"
    ARCHIVED = "archived"
    FAILED = "failed"
    SUCCESS = "success"
    RUNNING = "running"
    COMPLETED = "completed"


class CircuitState:
    """Circuit Breaker 상태 상수 (문자열 기반)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CanaryCluster:
    """Canary cluster name constants."""

    SEOUL_CANARY = "seoul-canary"
    SEOUL_MAIN = "seoul-main"
    TOKYO_MAIN = "tokyo-main"
    SINGAPORE_MAIN = "singapore-main"

    CANARY_ONLY = [SEOUL_CANARY]
    REGIONAL = [SEOUL_MAIN, TOKYO_MAIN]
    GLOBAL = [SEOUL_MAIN, TOKYO_MAIN, SINGAPORE_MAIN]


class CanaryPercentage:
    """Canary traffic percentage constants."""

    INITIAL = 10.0
    HALF = 50.0
    FULL = 100.0


class ChaosIntensity:
    """Chaos experiment intensity constants."""

    LOW_RATE = 0.001
    MEDIUM_RATE = 0.01
    HIGH_RATE = 0.05
    EXTREME_RATE = 0.1

    SHORT_DURATION = 60
    MEDIUM_DURATION = 300
    LONG_DURATION = 600
    EXTENDED_DURATION = 1800


class RBACRole:
    """RBAC role constants for Baldur API access control."""

    VIEWER = "baldur_viewer"
    OPERATOR = "baldur_operator"
    ADMIN = "baldur_admin"

    ALL_ROLES = [VIEWER, OPERATOR, ADMIN]
    ELEVATED_ROLES = [OPERATOR, ADMIN]


class DefaultValues:
    """
    테스트에서 사용하는 기본값 상수 (통합).

    개별 클래스(Domains, Services 등)를 사용하거나,
    이 클래스에서 모든 상수에 접근할 수 있습니다.
    """

    # 도메인 관련 (Domains 클래스와 동일)
    DOMAIN_ORDER = Domains.ORDER
    DOMAIN_PAYMENT = Domains.PAYMENT
    DOMAIN_NOTIFICATION = Domains.NOTIFICATION
    DOMAIN_EXTERNAL = Domains.EXTERNAL

    # 서비스 관련 (Services 클래스와 동일)
    SERVICE_PAYMENT_API = Services.PAYMENT_API
    SERVICE_EXTERNAL_GATEWAY = Services.EXTERNAL_GATEWAY
    SERVICE_ORDER_SERVICE = Services.ORDER_SERVICE
    SERVICE_TEST = Services.TEST

    # 실패 타입 (FailureTypes 클래스와 동일)
    FAILURE_NETWORK = FailureTypes.NETWORK
    FAILURE_TIMEOUT = FailureTypes.TIMEOUT
    FAILURE_PG_TIMEOUT = FailureTypes.PG_TIMEOUT

    # 상태 (Status 클래스와 동일)
    STATUS_PENDING = Status.PENDING
    STATUS_RESOLVED = Status.RESOLVED
    STATUS_ARCHIVED = Status.ARCHIVED

    # Circuit Breaker (CircuitBreakerStateEnum 정규 소스 사용)
    CB_STATE_CLOSED = CircuitBreakerStateEnum.CLOSED
    CB_STATE_OPEN = CircuitBreakerStateEnum.OPEN
    CB_STATE_HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN

    # 기본 설정값
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_FAILURE_THRESHOLD = 5
    DEFAULT_RECOVERY_TIMEOUT = 60
    DEFAULT_SUCCESS_THRESHOLD = 2


# =============================================================================
# Infrastructure Test Config
# =============================================================================

from dataclasses import dataclass


@dataclass(frozen=True)
class TestConstants:
    """Test default value constants."""

    IDEMPOTENCY_KEY_TTL: int = 60
    WEBHOOK_EVENT_TTL: int = 60
    CIRCUIT_BREAKER_TTL: int = 300
    DEFAULT_PASSWORD: str = "testpass123"


@dataclass(frozen=True)
class CeleryTestConfig:
    """Celery test configuration."""

    TASK_ALWAYS_EAGER: bool = True
    TASK_EAGER_PROPAGATES: bool = True

    DEFAULT_QUEUE: str = "default"
    EXTERNAL_API_QUEUE: str = "external_api"
    NOTIFICATIONS_QUEUE: str = "notifications"

    DEFAULT_MAX_RETRIES: int = 3
    DEFAULT_RETRY_DELAY: int = 60


@dataclass(frozen=True)
class RedisTestConfig:
    """Redis test configuration."""

    DEFAULT_HOST: str = "localhost"
    DEFAULT_PORT: int = 6379
    TEST_PORT: int = 16379

    KEY_PREFIX: str = "test:"
    CB_KEY_PREFIX: str = "test:baldur:cb:"
    DLQ_KEY_PREFIX: str = "test:baldur:dlq:"

    DEFAULT_DB: int = 0
    TEST_DB: int = 15

    @property
    def redis_url(self) -> str:
        return f"redis://{self.DEFAULT_HOST}:{self.DEFAULT_PORT}/{self.DEFAULT_DB}"

    @property
    def test_redis_url(self) -> str:
        return f"redis://{self.DEFAULT_HOST}:{self.TEST_PORT}/{self.TEST_DB}"


@dataclass(frozen=True)
class DatabaseTestConfig:
    """Database test configuration."""

    DEFAULT_HOST: str = "localhost"
    DEFAULT_PORT: int = 5432
    DEFAULT_DB: str = "baldur_test_db"
    DEFAULT_USER: str = "baldur_user"
    DEFAULT_PASSWORD: str = "baldur_pass"

    @property
    def database_url(self) -> str:
        return (
            f"postgres://{self.DEFAULT_USER}:{self.DEFAULT_PASSWORD}"
            f"@{self.DEFAULT_HOST}:{self.DEFAULT_PORT}/{self.DEFAULT_DB}"
        )


TEST_CONSTANTS = TestConstants()
CELERY_CONFIG = CeleryTestConfig()
REDIS_CONFIG = RedisTestConfig()
DB_CONFIG = DatabaseTestConfig()
