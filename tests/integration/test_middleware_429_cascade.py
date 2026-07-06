"""
BaldurMiddleware × CircuitBreakerService cascade integration test.

Verifies that repeated external 429 responses flowing through BaldurMiddleware
trigger the cascade detection logic in CircuitBreakerService and auto-open the CB
for the rate-limited domain. Uses InMemoryCircuitBreakerStateRepository — no Docker.

Interaction chain under test:
  BaldurMiddleware._handle_external_429(request, response)
  → CircuitBreakerService.record_rate_limit_response(domain)
  → RateLimitTracker.record_rate_limit(domain)        [shared singleton state]
  → (when count >= threshold) CircuitBreakerService.force_open(domain)
  → InMemoryCircuitBreakerStateRepository.atomic_force_open(domain)

Test Categories:
    A. Cascade threshold workflow:
        - Below threshold: CB stays closed
        - At threshold: CB auto-opens for domain
        - Above threshold: CB stays open
    B. Domain isolation:
        - Cascade opens only targeted domain CB
        - Separate cascades open separate domain CBs
    C. Internal vs external filtering:
        - Internal 429 does not contribute to cascade count
        - Mixed internal and external: only external counts

Note: All tests use InMemory repository + in-process tracker — no Docker required.
      This enables parallel test execution with pytest-xdist.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.api.django.middleware.baldur import BaldurMiddleware
from baldur.services.circuit_breaker import (
    rate_limit_tracker as _rl_tracker_module,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService

# Low threshold keeps tests fast (default is 10)
_CASCADE_THRESHOLD = 3
_PAYMENT_DOMAIN = "payment"
_ORDER_DOMAIN = "order"


# =============================================================================
# Helpers
# =============================================================================


class _FakeResponse:
    """Minimal HttpResponse stub with dict-like header access."""

    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self._headers: dict = headers or {}

    def get(self, key: str, default=None):
        return self._headers.get(key, default)

    def __setitem__(self, key: str, value: str) -> None:
        self._headers[key] = value


class _FakeRequest:
    """Minimal HttpRequest stub."""

    def __init__(self, path: str = "/api/payments/1/", method: str = "GET"):
        self.path = path
        self.method = method
        self.body = b""
        self.META: dict = {}


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_rate_limit_tracker():
    """
    Reset the global RateLimitTracker singleton between tests.

    RateLimitTracker is a process-level singleton. Resetting its module-level
    reference forces a fresh instance for each test, preventing count leakage.
    """
    _rl_tracker_module._rate_limit_tracker = None
    yield
    _rl_tracker_module._rate_limit_tracker = None


@pytest.fixture
def cb_config() -> CircuitBreakerConfig:
    """Low-threshold config that cascades after only 3 external 429s.

    The cascade condition is hybrid (absolute count AND minimum sample AND
    rate), so the minimum-calls sample gate must also be lowered to the
    threshold — otherwise 3 external 429s never reach the default minimum of
    20 requests and the CB never opens.
    """
    return CircuitBreakerConfig(
        enabled=True,
        rate_limit_cascade_threshold=_CASCADE_THRESHOLD,
        rate_limit_cascade_minimum_calls=_CASCADE_THRESHOLD,
        rate_limit_cascade_window_seconds=60,
    )


@pytest.fixture
def cb_repo() -> InMemoryCircuitBreakerStateRepository:
    """Isolated in-memory repository per test — no Redis needed."""
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def cb_service(
    cb_config: CircuitBreakerConfig,
    cb_repo: InMemoryCircuitBreakerStateRepository,
) -> CircuitBreakerService:
    """CircuitBreakerService wired to isolated in-memory repo."""
    return CircuitBreakerService(config=cb_config, repository=cb_repo)


@pytest.fixture
def middleware(cb_service: CircuitBreakerService) -> BaldurMiddleware:
    """Pre-initialized middleware with the isolated CB service injected."""
    mw = BaldurMiddleware(get_response=lambda r: None)
    mw._initialized = True
    mw._audit_logger = None
    mw._cb_service = cb_service
    mw._cb_status_codes = frozenset({500, 502, 503, 504})
    mw._rate_limit_codes = frozenset({429})
    mw._retry_after_max = 300
    mw.DOMAIN_MAPPING = {
        "/payments/": _PAYMENT_DOMAIN,
        "/orders/": _ORDER_DOMAIN,
    }
    mw._log_audit_event = Mock()
    return mw


def _deliver_external_429(
    mw: BaldurMiddleware,
    path: str = "/api/payments/1/",
) -> None:
    """Simulate one external 429 response flowing through _handle_external_429."""
    response = _FakeResponse(429, {})  # No X-RateLimit-Mode → external
    mw._handle_external_429(_FakeRequest(path=path), response)


# =============================================================================
# Integration tests
# =============================================================================


class TestMiddlewareCascadeThresholdWorkflow:
    """
    Cascade detection lifecycle: below threshold / at threshold / above threshold.

    Verifies that the shared state between BaldurMiddleware and
    CircuitBreakerService (via RateLimitTracker) produces the correct CB
    state transitions.
    """

    def test_below_threshold_cb_remains_closed(
        self,
        middleware: BaldurMiddleware,
        cb_service: CircuitBreakerService,
    ):
        """
        Purpose:
            threshold 미만의 외부 429가 CB를 열지 않는지 검증.
        Expected:
            - threshold - 1 개의 외부 429 후 CB가 closed 상태를 유지
        """
        # Given / When
        for _ in range(_CASCADE_THRESHOLD - 1):
            _deliver_external_429(middleware)

        # Then — CB must still be closed
        state = cb_service.get_state(_PAYMENT_DOMAIN)
        assert state.lower() in ("closed", "half_open"), (
            f"CB should remain closed below threshold={_CASCADE_THRESHOLD}, got: {state!r}"
        )

    def test_at_threshold_cb_auto_opens_for_domain(
        self,
        middleware: BaldurMiddleware,
        cb_service: CircuitBreakerService,
    ):
        """
        Purpose:
            정확히 threshold 개의 외부 429가 해당 도메인 CB를 자동으로 여는지 검증.
        Expected:
            - cascade threshold 도달 시 CB가 open 상태로 전환
        """
        # Given / When
        for _ in range(_CASCADE_THRESHOLD):
            _deliver_external_429(middleware)

        # Then — CB must be open
        state = cb_service.get_state(_PAYMENT_DOMAIN)
        assert state.lower() == "open", (
            f"CB should auto-open at cascade threshold={_CASCADE_THRESHOLD}, got: {state!r}"
        )

    def test_above_threshold_cb_stays_open(
        self,
        middleware: BaldurMiddleware,
        cb_service: CircuitBreakerService,
    ):
        """
        Purpose:
            threshold 초과 후 추가 429에도 CB가 open 상태를 유지하는지 검증.
        Expected:
            - threshold * 3 개의 429 후에도 CB가 open 상태 유지
        """
        for _ in range(_CASCADE_THRESHOLD * 3):
            _deliver_external_429(middleware)

        state = cb_service.get_state(_PAYMENT_DOMAIN)
        assert state.lower() == "open"


class TestMiddlewareCascadeDomainIsolationWorkflow:
    """
    Cascade isolation: 429s for domain A must not affect domain B.

    Verifies that RateLimitTracker tracks events per service_name, so
    cascade detection is scoped to the domain inferred from the request path.
    """

    def test_cascade_opens_only_targeted_domain_cb(
        self,
        middleware: BaldurMiddleware,
        cb_service: CircuitBreakerService,
    ):
        """
        Purpose:
            특정 도메인의 429 cascade가 다른 도메인 CB에 영향을 주지 않는지 검증.
        Expected:
            - payment 도메인 CB는 open
            - order 도메인 CB는 closed 유지
        """
        # Given / When — cascade on payment path only
        for _ in range(_CASCADE_THRESHOLD):
            _deliver_external_429(middleware, path="/api/payments/1/")

        # Then — payment CB opened
        payment_state = cb_service.get_state(_PAYMENT_DOMAIN)
        assert payment_state.lower() == "open"

        # Then — order CB unaffected
        order_state = cb_service.get_state(_ORDER_DOMAIN)
        assert order_state.lower() in ("closed", "half_open"), (
            f"order CB should be unaffected by payment cascade, got: {order_state!r}"
        )

    def test_separate_cascades_open_separate_domain_cbs(
        self,
        middleware: BaldurMiddleware,
        cb_service: CircuitBreakerService,
    ):
        """
        Purpose:
            두 도메인이 독립적으로 threshold에 도달할 때 각각의 CB가 열리는지 검증.
        Expected:
            - payment CB open
            - order CB open
        """
        # Given / When — cascade on both domains independently
        for _ in range(_CASCADE_THRESHOLD):
            _deliver_external_429(middleware, path="/api/payments/1/")
        for _ in range(_CASCADE_THRESHOLD):
            _deliver_external_429(middleware, path="/api/orders/99/")

        # Then — both CBs open
        assert cb_service.get_state(_PAYMENT_DOMAIN).lower() == "open"
        assert cb_service.get_state(_ORDER_DOMAIN).lower() == "open"


class TestMiddlewareInternalVsExternalFilteringWorkflow:
    """
    Internal 429 filtering: self-generated rate limit responses must not
    accumulate in RateLimitTracker or trigger cascade detection.
    """

    def test_internal_429_does_not_contribute_to_cascade_count(
        self,
        middleware: BaldurMiddleware,
        cb_service: CircuitBreakerService,
    ):
        """
        Purpose:
            내부 429 (X-RateLimit-Mode 헤더)가 cascade 카운트에 포함되지 않는지 검증.
        Expected:
            - _is_internal_429이 True를 반환하여 _handle_external_429을 건너뜀
            - threshold 개의 내부 429 후에도 CB가 closed 유지
        """
        # Given — simulate __call__ seeing internal 429 (X-RateLimit-Mode header present)
        # We verify via _is_internal_429() gate, then confirm CB stays closed
        internal_response = _FakeResponse(429, {"X-RateLimit-Mode": "normal"})
        request = _FakeRequest(path="/api/payments/1/")

        # _is_internal_429 must classify this as internal — gate fires
        assert middleware._is_internal_429(internal_response) is True

        # Simulate threshold-many "calls" that would have triggered cascade IF external
        for _ in range(_CASCADE_THRESHOLD):
            # _is_internal_429 is True, so _handle_external_429 is NOT called
            if not middleware._is_internal_429(internal_response):
                middleware._handle_external_429(request, internal_response)

        # CB must remain closed — none of the 429s reached the tracker
        state = cb_service.get_state(_PAYMENT_DOMAIN)
        assert state.lower() in ("closed", "half_open"), (
            f"CB should be unaffected by internal 429s, got: {state!r}"
        )

    def test_mixed_internal_and_external_only_external_counts(
        self,
        middleware: BaldurMiddleware,
        cb_service: CircuitBreakerService,
    ):
        """
        Purpose:
            내부와 외부 429가 혼합될 때 외부 429만 cascade에 카운트되는지 검증.
        Expected:
            - 내부 429 N개는 무시
            - 외부 429 threshold개 도달 시 CB가 open
        """
        # Given — send some internal 429s first (should not count)
        internal = _FakeResponse(429, {"X-RateLimit-Mode": "emergency"})
        for _ in range(_CASCADE_THRESHOLD * 2):
            if not middleware._is_internal_429(internal):
                middleware._handle_external_429(_FakeRequest(), internal)

        # When — now send real external 429s up to the threshold
        for _ in range(_CASCADE_THRESHOLD):
            _deliver_external_429(middleware, path="/api/payments/1/")

        # Then — CB opened based on external 429s only
        state = cb_service.get_state(_PAYMENT_DOMAIN)
        assert state.lower() == "open", (
            f"CB should open after {_CASCADE_THRESHOLD} external 429s, got: {state!r}"
        )
