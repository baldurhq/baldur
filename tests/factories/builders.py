"""
Builder Pattern for Test Data.

복잡한 테스트 객체 생성을 위한 Builder 클래스들입니다.
Fluent API 스타일로 체이닝하여 사용합니다.

Usage:
    # Circuit Breaker State Builder
    state = (CircuitBreakerStateBuilder()
        .with_service("payment-api")
        .opened()
        .with_failure_count(5)
        .controlled_by(user_id=1, reason="Maintenance")
        .build())

    # Failed Operation Builder
    entry = (FailedOperationBuilder()
        .payment_domain()
        .pg_timeout()
        .pending()
        .with_retries(2, max_retries=5)
        .build())
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, Mock

from tests.factories.constants import (
    CircuitState,
    Domains,
    FailureTypes,
    Services,
    Status,
)
from tests.factories.data_factory import (
    MockCanaryRolloutData,
    MockCircuitBreakerStateData,
    MockFailedOperationData,
)


class CircuitBreakerStateBuilder:
    """
    Circuit Breaker State Builder.

    체이닝 방식으로 CB 상태 객체를 생성합니다.

    Usage:
        state = (CircuitBreakerStateBuilder()
            .with_service("payment-api")
            .opened()
            .with_failure_count(5)
            .build())
    """

    def __init__(self):
        self._service_name = Services.TEST
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: datetime | None = None
        self._opened_by_id: int | None = None
        self._opened_reason = ""
        self._manually_controlled = False
        self._controlled_by_id: int | None = None
        self._control_reason = ""
        self._half_open_request_count = 0
        self._manual_override_expires_at: datetime | None = None

    def with_service(self, name: str) -> "CircuitBreakerStateBuilder":
        """서비스 이름 설정."""
        self._service_name = name
        return self

    def payment_service(self) -> "CircuitBreakerStateBuilder":
        """결제 서비스로 설정."""
        return self.with_service(Services.PAYMENT_API)

    def external_gateway(self) -> "CircuitBreakerStateBuilder":
        """외부 게이트웨이로 설정."""
        return self.with_service(Services.EXTERNAL_GATEWAY)

    def toss_payments(self) -> "CircuitBreakerStateBuilder":
        """토스페이먼츠로 설정."""
        return self.with_service(Services.TOSS_PAYMENTS)

    def closed(self) -> "CircuitBreakerStateBuilder":
        """Closed 상태로 설정."""
        self._state = CircuitState.CLOSED
        self._opened_at = None
        return self

    def opened(self, at: datetime | None = None) -> "CircuitBreakerStateBuilder":
        """Open 상태로 설정."""
        self._state = CircuitState.OPEN
        self._opened_at = at or datetime.now(UTC)
        return self

    def half_open(self, request_count: int = 0) -> "CircuitBreakerStateBuilder":
        """Half-Open 상태로 설정."""
        self._state = CircuitState.HALF_OPEN
        self._half_open_request_count = request_count
        return self

    def with_failure_count(self, count: int) -> "CircuitBreakerStateBuilder":
        """실패 횟수 설정."""
        self._failure_count = count
        return self

    def with_success_count(self, count: int) -> "CircuitBreakerStateBuilder":
        """성공 횟수 설정."""
        self._success_count = count
        return self

    def controlled_by(
        self,
        user_id: int,
        reason: str = "Manual control",
        expires_in_minutes: int | None = None,
    ) -> "CircuitBreakerStateBuilder":
        """수동 제어 상태로 설정."""
        self._manually_controlled = True
        self._controlled_by_id = user_id
        self._opened_by_id = user_id
        self._control_reason = reason
        self._opened_reason = reason

        if expires_in_minutes:
            self._manual_override_expires_at = datetime.now(UTC) + timedelta(
                minutes=expires_in_minutes
            )

        return self

    def build(self) -> MockCircuitBreakerStateData:
        """객체 생성."""
        return MockCircuitBreakerStateData(
            service_name=self._service_name,
            state=self._state,
            failure_count=self._failure_count,
            success_count=self._success_count,
            opened_at=self._opened_at,
            opened_by_id=self._opened_by_id,
            opened_reason=self._opened_reason,
            half_open_request_count=self._half_open_request_count,
            manually_controlled=self._manually_controlled,
            controlled_by_id=self._controlled_by_id,
            control_reason=self._control_reason,
            manual_override_expires_at=self._manual_override_expires_at,
        )


class FailedOperationBuilder:
    """
    Failed Operation (DLQ) Builder.

    체이닝 방식으로 DLQ 엔트리 객체를 생성합니다.
    """

    def __init__(self):
        self._id = 1
        self._domain = Domains.PAYMENT
        self._failure_type = FailureTypes.PG_TIMEOUT
        self._status = Status.PENDING
        self._error_message = "Test error"
        self._retry_count = 0
        self._max_retries = 3
        self._context: dict[str, Any] = {}
        self._created_at: datetime | None = None

    def with_id(self, id: int) -> "FailedOperationBuilder":
        """ID 설정."""
        self._id = id
        return self

    def payment_domain(self) -> "FailedOperationBuilder":
        """결제 도메인으로 설정."""
        self._domain = Domains.PAYMENT
        return self

    def order_domain(self) -> "FailedOperationBuilder":
        """주문 도메인으로 설정."""
        self._domain = Domains.ORDER
        return self

    def notification_domain(self) -> "FailedOperationBuilder":
        """알림 도메인으로 설정."""
        self._domain = Domains.NOTIFICATION
        return self

    def webhook_domain(self) -> "FailedOperationBuilder":
        """웹훅 도메인으로 설정."""
        self._domain = Domains.WEBHOOK
        return self

    def with_domain(self, domain: str) -> "FailedOperationBuilder":
        """도메인 설정."""
        self._domain = domain
        return self

    def pg_timeout(self) -> "FailedOperationBuilder":
        """PG 타임아웃 오류로 설정."""
        self._failure_type = FailureTypes.PG_TIMEOUT
        self._error_message = "PG API timeout exceeded"
        return self

    def network_error(self) -> "FailedOperationBuilder":
        """네트워크 오류로 설정."""
        self._failure_type = FailureTypes.NETWORK_ERROR
        self._error_message = "Network connection failed"
        return self

    def amount_mismatch(self) -> "FailedOperationBuilder":
        """금액 불일치 오류로 설정."""
        self._failure_type = FailureTypes.AMOUNT_MISMATCH
        self._error_message = "Amount mismatch detected"
        return self

    def with_failure_type(
        self, failure_type: str, message: str = "Test error"
    ) -> "FailedOperationBuilder":
        """실패 유형 설정."""
        self._failure_type = failure_type
        self._error_message = message
        return self

    def pending(self) -> "FailedOperationBuilder":
        """대기 상태로 설정."""
        self._status = Status.PENDING
        return self

    def resolved(self) -> "FailedOperationBuilder":
        """해결됨 상태로 설정."""
        self._status = Status.RESOLVED
        return self

    def failed(self) -> "FailedOperationBuilder":
        """실패 상태로 설정."""
        self._status = Status.FAILED
        return self

    def archived(self) -> "FailedOperationBuilder":
        """보관됨 상태로 설정."""
        self._status = Status.ARCHIVED
        return self

    def with_retries(
        self, count: int, max_retries: int = 3
    ) -> "FailedOperationBuilder":
        """재시도 횟수 설정."""
        self._retry_count = count
        self._max_retries = max_retries
        return self

    def with_context(self, **kwargs) -> "FailedOperationBuilder":
        """컨텍스트 정보 추가."""
        self._context.update(kwargs)
        return self

    def created_at(self, dt: datetime) -> "FailedOperationBuilder":
        """생성 시각 설정."""
        self._created_at = dt
        return self

    def created_days_ago(self, days: int) -> "FailedOperationBuilder":
        """N일 전에 생성된 것으로 설정."""
        self._created_at = datetime.now(UTC) - timedelta(days=days)
        return self

    def build(self) -> MockFailedOperationData:
        """객체 생성."""
        return MockFailedOperationData(
            id=self._id,
            domain=self._domain,
            failure_type=self._failure_type,
            status=self._status,
            error_message=self._error_message,
            retry_count=self._retry_count,
            max_retries=self._max_retries,
            context=self._context,
            created_at=self._created_at,
        )


class CanaryRolloutBuilder:
    """
    Canary Rollout Builder.

    체이닝 방식으로 Canary 롤아웃 객체를 생성합니다.
    """

    def __init__(self):
        self._id = "rollout-test-001"
        self._config_type = "circuit_breaker"
        self._state = "created"
        self._current_stage_index = 0
        self._new_values: dict[str, Any] = {}
        self._created_by = "test@example.com"
        self._reason = "Test rollout"
        self._stages: list[dict[str, Any]] = []

    def with_id(self, id: str) -> "CanaryRolloutBuilder":
        """ID 설정."""
        self._id = id
        return self

    def circuit_breaker_config(self) -> "CanaryRolloutBuilder":
        """CB 설정 타입으로 지정."""
        self._config_type = "circuit_breaker"
        return self

    def dlq_config(self) -> "CanaryRolloutBuilder":
        """DLQ 설정 타입으로 지정."""
        self._config_type = "dlq"
        return self

    def with_new_values(self, **kwargs) -> "CanaryRolloutBuilder":
        """새 설정 값 지정."""
        self._new_values.update(kwargs)
        return self

    def created(self) -> "CanaryRolloutBuilder":
        """생성됨 상태로 설정."""
        self._state = "created"
        return self

    def running(self, stage_index: int = 0) -> "CanaryRolloutBuilder":
        """실행 중 상태로 설정."""
        self._state = "running"
        self._current_stage_index = stage_index
        return self

    def completed(self) -> "CanaryRolloutBuilder":
        """완료 상태로 설정."""
        self._state = "completed"
        return self

    def failed(self) -> "CanaryRolloutBuilder":
        """실패 상태로 설정."""
        self._state = "failed"
        return self

    def created_by(self, email: str) -> "CanaryRolloutBuilder":
        """생성자 설정."""
        self._created_by = email
        return self

    def with_reason(self, reason: str) -> "CanaryRolloutBuilder":
        """사유 설정."""
        self._reason = reason
        return self

    def with_default_stages(self) -> "CanaryRolloutBuilder":
        """기본 스테이지 설정 (canary → regional → global)."""
        self._stages = [
            {
                "name": "canary",
                "clusters": ["seoul-canary"],
                "percentage": 10.0,
                "duration_minutes": 5,
                "auto_promote": True,
            },
            {
                "name": "regional",
                "clusters": ["seoul-main", "tokyo-main"],
                "percentage": 50.0,
                "duration_minutes": 10,
            },
            {
                "name": "global",
                "clusters": ["seoul-main", "tokyo-main", "singapore-main"],
                "percentage": 100.0,
                "duration_minutes": 0,
            },
        ]
        return self

    def build(self) -> MockCanaryRolloutData:
        """객체 생성."""
        return MockCanaryRolloutData(
            id=self._id,
            config_type=self._config_type,
            state=self._state,
            current_stage_index=self._current_stage_index,
            new_values=self._new_values,
            created_by=self._created_by,
            reason=self._reason,
        )


class MockServiceBuilder:
    """
    Mock Service Builder.

    테스트용 Mock 서비스 객체를 생성합니다.
    """

    def __init__(self):
        self._mock: MagicMock = MagicMock()

    def with_method(self, name: str, return_value: Any = None) -> "MockServiceBuilder":
        """메서드와 반환값 설정."""
        getattr(self._mock, name).return_value = return_value
        return self

    def with_async_method(
        self, name: str, return_value: Any = None
    ) -> "MockServiceBuilder":
        """비동기 메서드와 반환값 설정."""

        async def async_return():
            return return_value

        getattr(self._mock, name).return_value = async_return()
        return self

    def with_side_effect(self, name: str, side_effect: Any) -> "MockServiceBuilder":
        """메서드에 side_effect 설정."""
        getattr(self._mock, name).side_effect = side_effect
        return self

    def with_property(self, name: str, value: Any) -> "MockServiceBuilder":
        """프로퍼티 값 설정."""
        setattr(self._mock, name, value)
        return self

    def build(self) -> MagicMock:
        """Mock 객체 반환."""
        return self._mock


# =============================================================================
# Phase 3: 추가 Builder 클래스
# =============================================================================


class MockRequestBuilder:
    """
    Django/DRF API 테스트용 Mock Request Builder.

    체이닝 방식으로 HTTP 요청 Mock 객체를 생성합니다.
    Permission 테스트, View 테스트에서 반복되는 request 생성을 간소화합니다.

    Usage:
        # 관리자 요청
        request = (MockRequestBuilder()
            .authenticated()
            .as_superuser()
            .with_data({"threshold": 10})
            .build())

        # 특정 그룹 사용자
        request = (MockRequestBuilder()
            .authenticated()
            .in_group("baldur_operator")
            .with_ip("192.168.1.100")
            .build())
    """

    def __init__(self):
        self._mock: Mock = Mock()
        self._user = Mock()
        self._is_authenticated = False
        self._is_staff = False
        self._is_superuser = False
        self._groups: list[str] = []
        self._group_check_result = False
        self._data: dict[str, Any] = {}
        self._method = "GET"
        self._ip_address = "127.0.0.1"
        self._headers: dict[str, str] = {}
        self._user_str = "test_user"

    def authenticated(self) -> "MockRequestBuilder":
        """인증된 사용자로 설정."""
        self._is_authenticated = True
        return self

    def anonymous(self) -> "MockRequestBuilder":
        """익명(비인증) 사용자로 설정."""
        self._is_authenticated = False
        return self

    def as_staff(self) -> "MockRequestBuilder":
        """스태프 권한으로 설정."""
        self._is_staff = True
        return self

    def as_superuser(self) -> "MockRequestBuilder":
        """슈퍼유저(관리자) 권한으로 설정."""
        self._is_superuser = True
        self._is_staff = True  # 슈퍼유저는 스태프 권한 포함
        return self

    def in_group(self, *group_names: str) -> "MockRequestBuilder":
        """
        특정 그룹에 소속된 사용자로 설정.

        Args:
            *group_names: 그룹 이름들 (예: "baldur_operator", "baldur_admin")
        """
        self._groups.extend(group_names)
        self._group_check_result = True
        return self

    def as_viewer(self) -> "MockRequestBuilder":
        """Baldur Viewer 그룹으로 설정."""
        return self.in_group("baldur_viewer")

    def as_operator(self) -> "MockRequestBuilder":
        """Baldur Operator 그룹으로 설정."""
        return self.in_group("baldur_operator")

    def as_admin(self) -> "MockRequestBuilder":
        """Baldur Admin 그룹으로 설정."""
        return self.in_group("baldur_admin")

    def with_data(self, data: dict[str, Any]) -> "MockRequestBuilder":
        """요청 바디 데이터 설정."""
        self._data.update(data)
        return self

    def with_method(self, method: str) -> "MockRequestBuilder":
        """HTTP 메서드 설정 (GET, POST, PUT, PATCH, DELETE)."""
        self._method = method.upper()
        return self

    def get(self) -> "MockRequestBuilder":
        """GET 요청으로 설정."""
        return self.with_method("GET")

    def post(self) -> "MockRequestBuilder":
        """POST 요청으로 설정."""
        return self.with_method("POST")

    def put(self) -> "MockRequestBuilder":
        """PUT 요청으로 설정."""
        return self.with_method("PUT")

    def patch(self) -> "MockRequestBuilder":
        """PATCH 요청으로 설정."""
        return self.with_method("PATCH")

    def delete(self) -> "MockRequestBuilder":
        """DELETE 요청으로 설정."""
        return self.with_method("DELETE")

    def with_ip(self, ip_address: str) -> "MockRequestBuilder":
        """클라이언트 IP 주소 설정."""
        self._ip_address = ip_address
        return self

    def with_headers(self, headers: dict[str, str]) -> "MockRequestBuilder":
        """HTTP 헤더 설정."""
        self._headers.update(headers)
        return self

    def with_user_str(self, user_str: str) -> "MockRequestBuilder":
        """사용자 문자열 표현 설정 (로깅용)."""
        self._user_str = user_str
        return self

    def build(self) -> Mock:
        """Mock Request 객체 생성."""
        # 사용자 설정
        if not self._is_authenticated:
            self._mock.user = None
            # 또는 익명 사용자 Mock
            self._mock.user = self._user
            self._user.is_authenticated = False
        else:
            self._mock.user = self._user
            self._user.is_authenticated = True
            self._user.is_staff = self._is_staff
            self._user.is_superuser = self._is_superuser

            # 그룹 체크 설정
            self._user.groups.filter.return_value.exists.return_value = (
                self._group_check_result
            )

            # 사용자 문자열 표현
            self._user.__str__ = Mock(return_value=self._user_str)

        # 요청 데이터
        self._mock.data = self._data
        self._mock.method = self._method

        # META 설정 (IP, 헤더 등)
        meta = {"REMOTE_ADDR": self._ip_address}
        for key, value in self._headers.items():
            # HTTP_ 접두사 추가 (Django 표준)
            http_key = f"HTTP_{key.upper().replace('-', '_')}"
            meta[http_key] = value
        self._mock.META = meta

        return self._mock


class CanaryStageBuilder:
    """
    Canary Stage Builder.

    체이닝 방식으로 Canary 롤아웃 단계 객체를 생성합니다.
    CanaryStage dataclass와 호환되는 딕셔너리를 반환합니다.

    Usage:
        # 카나리 단계 (10%)
        stage = (CanaryStageBuilder()
            .canary_stage()
            .with_percentage(10.0)
            .with_duration(5)
            .auto_promote()
            .build())

        # 리전 확장 단계 (50%)
        stage = (CanaryStageBuilder()
            .regional_stage()
            .build())
    """

    def __init__(self):
        from tests.factories.constants import CanaryCluster, CanaryPercentage

        self._name = "canary"
        self._clusters = CanaryCluster.CANARY_ONLY.copy()
        self._percentage = CanaryPercentage.INITIAL
        self._duration_minutes = 5
        self._auto_promote = True
        self._error_rate_threshold = 0.05
        self._latency_increase_threshold = 0.5

    def canary_stage(self) -> "CanaryStageBuilder":
        """카나리 단계 (10% 트래픽, seoul-canary 클러스터)."""
        from tests.factories.constants import CanaryCluster, CanaryPercentage

        self._name = "canary"
        self._clusters = CanaryCluster.CANARY_ONLY.copy()
        self._percentage = CanaryPercentage.INITIAL
        self._duration_minutes = 5
        self._auto_promote = True
        return self

    def regional_stage(self) -> "CanaryStageBuilder":
        """리전 확장 단계 (50% 트래픽, seoul + tokyo 메인)."""
        from tests.factories.constants import CanaryCluster, CanaryPercentage

        self._name = "regional"
        self._clusters = CanaryCluster.REGIONAL.copy()
        self._percentage = CanaryPercentage.HALF
        self._duration_minutes = 10
        self._auto_promote = False
        return self

    def global_stage(self) -> "CanaryStageBuilder":
        """글로벌 단계 (100% 트래픽, 전체 클러스터)."""
        from tests.factories.constants import CanaryCluster, CanaryPercentage

        self._name = "global"
        self._clusters = CanaryCluster.GLOBAL.copy()
        self._percentage = CanaryPercentage.FULL
        self._duration_minutes = 0  # 최종 단계
        self._auto_promote = False
        return self

    def with_name(self, name: str) -> "CanaryStageBuilder":
        """단계 이름 설정."""
        self._name = name
        return self

    def with_clusters(self, clusters: list[str]) -> "CanaryStageBuilder":
        """클러스터 목록 설정."""
        self._clusters = clusters.copy()
        return self

    def with_percentage(self, percentage: float) -> "CanaryStageBuilder":
        """트래픽 비율 설정 (0.0 ~ 100.0)."""
        self._percentage = percentage
        return self

    def with_duration(self, minutes: int) -> "CanaryStageBuilder":
        """단계 유지 시간 설정 (분)."""
        self._duration_minutes = minutes
        return self

    def auto_promote(self, enabled: bool = True) -> "CanaryStageBuilder":
        """자동 프로모션 활성화/비활성화."""
        self._auto_promote = enabled
        return self

    def with_error_threshold(self, threshold: float) -> "CanaryStageBuilder":
        """에러율 임계값 설정 (0.0 ~ 1.0)."""
        self._error_rate_threshold = threshold
        return self

    def with_latency_threshold(self, threshold: float) -> "CanaryStageBuilder":
        """레이턴시 증가 임계값 설정 (0.0 ~ 1.0)."""
        self._latency_increase_threshold = threshold
        return self

    def build(self) -> dict[str, Any]:
        """
        Canary Stage 딕셔너리 생성.

        Returns:
            CanaryStage 생성자와 호환되는 딕셔너리
        """
        return {
            "name": self._name,
            "clusters": self._clusters,
            "percentage": self._percentage,
            "duration_minutes": self._duration_minutes,
            "auto_promote": self._auto_promote,
            "error_rate_threshold": self._error_rate_threshold,
            "latency_increase_threshold": self._latency_increase_threshold,
        }

    @classmethod
    def default_three_stage_rollout(cls) -> list[dict[str, Any]]:
        """
        기본 3단계 롤아웃 구성 생성 (canary → regional → global).

        Returns:
            [canary(10%), regional(50%), global(100%)] 단계 리스트
        """
        return [
            cls().canary_stage().build(),
            cls().regional_stage().build(),
            cls().global_stage().build(),
        ]


class ChaosExperimentBuilder:
    """
    Chaos Experiment Builder.

    체이닝 방식으로 Chaos 실험 설정을 생성합니다.
    ExperimentConfig dataclass와 호환되는 딕셔너리를 반환합니다.

    Usage:
        # 레이턴시 주입 실험
        config = (ChaosExperimentBuilder()
            .latency_injection()
            .target_service("payment-api")
            .medium_intensity()
            .build())

        # CB 강제 Open 실험
        config = (ChaosExperimentBuilder()
            .circuit_breaker_open()
            .target_service("external-gateway")
            .with_duration(60)
            .build())
    """

    def __init__(self):
        from tests.factories.constants import ChaosIntensity

        self._experiment_type = "latency_injection"
        self._target_service = "test-service"
        self._target_domain = ""
        self._target_instances: list[str] = []
        self._injection_rate = ChaosIntensity.LOW_RATE
        self._duration_seconds = ChaosIntensity.MEDIUM_DURATION
        self._traffic_type = "synthetic"
        self._auto_rollback = True
        self._sla_threshold = 1.0  # 1%
        self._parameters: dict[str, Any] = {}
        self._dry_run = False

    # --- 실험 타입 설정 ---

    def latency_injection(self, latency_ms: int = 500) -> "ChaosExperimentBuilder":
        """레이턴시 주입 실험."""
        self._experiment_type = "latency_injection"
        self._parameters["latency_ms"] = latency_ms
        return self

    def error_5xx(self, status_code: int = 500) -> "ChaosExperimentBuilder":
        """5XX 에러 주입 실험."""
        self._experiment_type = "error_5xx"
        self._parameters["status_code"] = status_code
        return self

    def timeout(self, timeout_seconds: int = 30) -> "ChaosExperimentBuilder":
        """타임아웃 주입 실험."""
        self._experiment_type = "timeout"
        self._parameters["timeout_seconds"] = timeout_seconds
        return self

    def circuit_breaker_open(self) -> "ChaosExperimentBuilder":
        """Circuit Breaker 강제 Open 실험."""
        self._experiment_type = "circuit_breaker_open"
        return self

    def rate_limit(self, requests_per_second: int = 10) -> "ChaosExperimentBuilder":
        """Rate Limit 실험."""
        self._experiment_type = "rate_limit"
        self._parameters["requests_per_second"] = requests_per_second
        return self

    def resource_exhaustion(self) -> "ChaosExperimentBuilder":
        """리소스 고갈 실험."""
        self._experiment_type = "resource_exhaustion"
        return self

    def packet_loss(self, loss_rate: float = 0.1) -> "ChaosExperimentBuilder":
        """패킷 손실 실험."""
        self._experiment_type = "packet_loss"
        self._parameters["loss_rate"] = loss_rate
        return self

    def connection_reset(self) -> "ChaosExperimentBuilder":
        """커넥션 리셋 실험."""
        self._experiment_type = "connection_reset"
        return self

    def partial_failure(self, failure_rate: float = 0.3) -> "ChaosExperimentBuilder":
        """부분 실패 실험."""
        self._experiment_type = "partial_failure"
        self._parameters["failure_rate"] = failure_rate
        return self

    def cascading_failure(self) -> "ChaosExperimentBuilder":
        """연쇄 장애 실험."""
        self._experiment_type = "cascading_failure"
        return self

    # --- 대상 설정 ---

    def target_service(self, service_name: str) -> "ChaosExperimentBuilder":
        """대상 서비스 설정."""
        self._target_service = service_name
        return self

    def target_domain(self, domain: str) -> "ChaosExperimentBuilder":
        """대상 도메인 설정."""
        self._target_domain = domain
        return self

    def target_instances(self, instances: list[str]) -> "ChaosExperimentBuilder":
        """대상 인스턴스 목록 설정."""
        self._target_instances = instances.copy()
        return self

    # --- 강도 설정 ---

    def low_intensity(self) -> "ChaosExperimentBuilder":
        """낮은 강도 (0.1%, 1분)."""
        from tests.factories.constants import ChaosIntensity

        self._injection_rate = ChaosIntensity.LOW_RATE
        self._duration_seconds = ChaosIntensity.SHORT_DURATION
        return self

    def medium_intensity(self) -> "ChaosExperimentBuilder":
        """중간 강도 (1%, 5분)."""
        from tests.factories.constants import ChaosIntensity

        self._injection_rate = ChaosIntensity.MEDIUM_RATE
        self._duration_seconds = ChaosIntensity.MEDIUM_DURATION
        return self

    def high_intensity(self) -> "ChaosExperimentBuilder":
        """높은 강도 (5%, 10분)."""
        from tests.factories.constants import ChaosIntensity

        self._injection_rate = ChaosIntensity.HIGH_RATE
        self._duration_seconds = ChaosIntensity.LONG_DURATION
        return self

    def extreme_intensity(self) -> "ChaosExperimentBuilder":
        """극한 강도 (10%, 30분)."""
        from tests.factories.constants import ChaosIntensity

        self._injection_rate = ChaosIntensity.EXTREME_RATE
        self._duration_seconds = ChaosIntensity.EXTENDED_DURATION
        return self

    def with_injection_rate(self, rate: float) -> "ChaosExperimentBuilder":
        """주입 비율 직접 설정 (0.0 ~ 1.0)."""
        self._injection_rate = rate
        return self

    def with_duration(self, seconds: int) -> "ChaosExperimentBuilder":
        """지속 시간 직접 설정 (초)."""
        self._duration_seconds = seconds
        return self

    # --- 기타 설정 ---

    def dry_run(self, enabled: bool = True) -> "ChaosExperimentBuilder":
        """Dry Run 모드 설정 (실제 장애 주입 없음)."""
        self._dry_run = enabled
        return self

    def auto_rollback(self, enabled: bool = True) -> "ChaosExperimentBuilder":
        """SLA 위반 시 자동 롤백 설정."""
        self._auto_rollback = enabled
        return self

    def with_sla_threshold(self, percent: float) -> "ChaosExperimentBuilder":
        """SLA 위반 임계값 설정 (%)."""
        self._sla_threshold = percent
        return self

    def with_traffic_type(self, traffic_type: str) -> "ChaosExperimentBuilder":
        """트래픽 타입 설정 (synthetic, production)."""
        self._traffic_type = traffic_type
        return self

    def with_parameter(self, key: str, value: Any) -> "ChaosExperimentBuilder":
        """추가 파라미터 설정."""
        self._parameters[key] = value
        return self

    def build(self) -> dict[str, Any]:
        """
        Chaos Experiment 설정 딕셔너리 생성.

        Returns:
            ExperimentConfig 생성자와 호환되는 딕셔너리
        """
        return {
            "experiment_type": self._experiment_type,
            "target_service": self._target_service,
            "target_domain": self._target_domain,
            "target_instances": self._target_instances,
            "injection_rate": self._injection_rate,
            "duration_seconds": self._duration_seconds,
            "traffic_type": self._traffic_type,
            "auto_rollback_on_sla_breach": self._auto_rollback,
            "sla_breach_threshold_percent": self._sla_threshold,
            "parameters": self._parameters,
            "dry_run": self._dry_run,
        }


class WatchdogConfigBuilder:
    """
    Watchdog Config Builder.

    Canary Rollout Watchdog 설정을 생성합니다.
    zombie 롤아웃 감지 및 자동 롤백/프로모션 설정을 포함합니다.

    Usage:
        # 기본 설정
        config = WatchdogConfigBuilder().default().build()

        # 공격적 설정 (빠른 감지)
        config = WatchdogConfigBuilder().aggressive().build()

        # 보수적 설정 (느린 감지)
        config = WatchdogConfigBuilder().conservative().build()
    """

    def __init__(self):
        self._zombie_threshold_minutes = 30
        self._auto_rollback_after_minutes = 60
        self._max_stage_duration_minutes = 15
        self._enable_auto_promote = True
        self._enable_auto_rollback = True
        self._notification_enabled = True
        self._slack_channel = "#baldur-alerts"

    def default(self) -> "WatchdogConfigBuilder":
        """기본 Watchdog 설정."""
        self._zombie_threshold_minutes = 30
        self._auto_rollback_after_minutes = 60
        self._max_stage_duration_minutes = 15
        self._enable_auto_promote = True
        self._enable_auto_rollback = True
        return self

    def aggressive(self) -> "WatchdogConfigBuilder":
        """
        공격적 설정 (빠른 감지/대응).

        짧은 임계값으로 빠르게 zombie 감지 및 롤백합니다.
        """
        self._zombie_threshold_minutes = 10
        self._auto_rollback_after_minutes = 20
        self._max_stage_duration_minutes = 5
        self._enable_auto_promote = True
        self._enable_auto_rollback = True
        return self

    def conservative(self) -> "WatchdogConfigBuilder":
        """
        보수적 설정 (느린 감지/대응).

        긴 임계값으로 충분한 관찰 시간을 부여합니다.
        """
        self._zombie_threshold_minutes = 60
        self._auto_rollback_after_minutes = 120
        self._max_stage_duration_minutes = 30
        self._enable_auto_promote = False
        self._enable_auto_rollback = False
        return self

    def with_zombie_threshold(self, minutes: int) -> "WatchdogConfigBuilder":
        """Zombie 감지 임계값 설정 (분)."""
        self._zombie_threshold_minutes = minutes
        return self

    def with_auto_rollback_after(self, minutes: int) -> "WatchdogConfigBuilder":
        """자동 롤백까지 대기 시간 설정 (분)."""
        self._auto_rollback_after_minutes = minutes
        return self

    def with_max_stage_duration(self, minutes: int) -> "WatchdogConfigBuilder":
        """단계별 최대 체류 시간 설정 (분)."""
        self._max_stage_duration_minutes = minutes
        return self

    def enable_auto_promote(self, enabled: bool = True) -> "WatchdogConfigBuilder":
        """자동 프로모션 활성화/비활성화."""
        self._enable_auto_promote = enabled
        return self

    def enable_auto_rollback(self, enabled: bool = True) -> "WatchdogConfigBuilder":
        """자동 롤백 활성화/비활성화."""
        self._enable_auto_rollback = enabled
        return self

    def with_notification(
        self, enabled: bool = True, channel: str = "#baldur-alerts"
    ) -> "WatchdogConfigBuilder":
        """알림 설정."""
        self._notification_enabled = enabled
        self._slack_channel = channel
        return self

    def build(self) -> dict[str, Any]:
        """
        Watchdog 설정 딕셔너리 생성.

        Returns:
            WatchdogConfig 생성자와 호환되는 딕셔너리
        """
        return {
            "zombie_threshold_minutes": self._zombie_threshold_minutes,
            "auto_rollback_after_minutes": self._auto_rollback_after_minutes,
            "max_stage_duration_minutes": self._max_stage_duration_minutes,
            "enable_auto_promote": self._enable_auto_promote,
            "enable_auto_rollback": self._enable_auto_rollback,
            "notification_enabled": self._notification_enabled,
            "slack_channel": self._slack_channel,
        }
