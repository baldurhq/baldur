"""
BaldurMiddleware unit tests.

Covers:
- BaldurMiddlewareSettings defaults, boundaries, env var override, singleton lifecycle
- _is_internal_429(): decision table (4 header combinations)
- _parse_retry_after(): seconds / HTTP-date / clamp / fail-open contracts
- __call__(): status code routing (500/502/external-429/internal-429/2xx/4xx)
- _handle_external_429(): CB + EventBus + Retry-After + audit side effects
- _lazy_init(): settings load failure fallback to safe defaults
- CB_DATABASE_DOMAIN constant contract (D7)
- _record_cb_failure(): service_name routing + pool CB gating (D3, D6)
- _record_cb_success(): dual recording for non-database domains (D4)
- _is_cb_open(): database CB + domain CB + pool CB checks (D5)
- __call__(): 5xx domain routing, DB exception domain, success domain (D1, D2)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock, patch

import pytest
from pydantic import ValidationError

from baldur.api.django.middleware.baldur import BaldurMiddleware
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.settings.middleware import (
    BaldurMiddlewareSettings,
    get_middleware_settings,
    reset_middleware_settings,
)

# =============================================================================
# Test helpers
# =============================================================================


class FakeResponse:
    """Minimal HttpResponse stub with dict-like header access."""

    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self._headers: dict = headers or {}

    def get(self, key: str, default=None):
        return self._headers.get(key, default)

    def __setitem__(self, key: str, value: str) -> None:
        self._headers[key] = value

    def __getitem__(self, key: str) -> str:
        return self._headers[key]


class FakeRequest:
    """Minimal HttpRequest stub."""

    def __init__(self, path: str = "/api/test/", method: str = "GET"):
        self.path = path
        self.method = method
        self.body = b""
        self.META: dict = {}


def _make_middleware(
    cb_service=None,
    retry_after_max: int = 300,
    cb_status_codes=None,
    rate_limit_codes=None,
) -> BaldurMiddleware:
    """Factory: pre-initialized middleware with injected dependencies."""
    mw = BaldurMiddleware(get_response=lambda r: None)
    mw._initialized = True
    mw._audit_logger = None
    mw._cb_service = cb_service
    mw._retry_after_max = retry_after_max
    mw._cb_status_codes = frozenset(cb_status_codes or {500, 502, 503, 504})
    mw._rate_limit_codes = frozenset(rate_limit_codes or {429})
    return mw


@pytest.fixture(autouse=True)
def _reset_class_state():
    """Reset BaldurMiddleware class-level path cache between tests."""
    BaldurMiddleware._paths_loaded = False
    yield
    BaldurMiddleware._paths_loaded = False


# =============================================================================
# BaldurMiddlewareSettings — Contract
# =============================================================================


class TestBaldurMiddlewareSettingsContract:
    """BaldurMiddlewareSettings 설계 계약값 검증."""

    def test_cb_status_codes_default_contains_500(self):
        """500은 설계 계약상 기본 CB 트리거에 포함되어야 한다 (D1)."""
        s = BaldurMiddlewareSettings()
        assert 500 in s.cb_status_codes

    def test_cb_status_codes_default_contains_502_503_504(self):
        """502/503/504는 기존 CB 트리거에 계속 포함되어야 한다."""
        s = BaldurMiddlewareSettings()
        assert {502, 503, 504}.issubset(set(s.cb_status_codes))

    def test_cb_status_codes_default_has_exactly_four_entries(self):
        """기본 cb_status_codes는 정확히 [500, 502, 503, 504] 4개이다."""
        s = BaldurMiddlewareSettings()
        assert sorted(s.cb_status_codes) == [500, 502, 503, 504]

    def test_rate_limit_codes_default_is_429_only(self):
        """기본 rate_limit_codes는 [429] 하나만 포함해야 한다."""
        s = BaldurMiddlewareSettings()
        assert s.rate_limit_codes == [429]

    def test_retry_after_max_default_is_300(self):
        """Retry-After 최대 대기 시간 기본값은 300초 (5분)이다."""
        s = BaldurMiddlewareSettings()
        assert s.retry_after_max == 300

    def test_retry_after_max_minimum_boundary_1_is_accepted(self):
        """retry_after_max 최솟값 경계 1은 허용된다 (ge=1)."""
        s = BaldurMiddlewareSettings(retry_after_max=1)
        assert s.retry_after_max == 1

    def test_retry_after_max_zero_raises_validation_error(self):
        """retry_after_max=0은 ge=1 위반으로 ValidationError를 발생시켜야 한다."""
        with pytest.raises(ValidationError):
            BaldurMiddlewareSettings(retry_after_max=0)

    def test_retry_after_max_maximum_boundary_3600_is_accepted(self):
        """retry_after_max 최댓값 경계 3600은 허용된다 (le=3600)."""
        s = BaldurMiddlewareSettings(retry_after_max=3600)
        assert s.retry_after_max == 3600

    def test_retry_after_max_above_3600_raises_validation_error(self):
        """retry_after_max=3601은 le=3600 위반으로 ValidationError를 발생시켜야 한다."""
        with pytest.raises(ValidationError):
            BaldurMiddlewareSettings(retry_after_max=3601)

    def test_env_prefix_is_baldur_middleware(self):
        """Pydantic model_config의 env_prefix가 'BALDUR_MIDDLEWARE_'이어야 한다."""
        config = BaldurMiddlewareSettings.model_config
        assert config.get("env_prefix") == "BALDUR_MIDDLEWARE_"

    def test_cb_status_codes_overridable_via_env(self, monkeypatch):
        """BALDUR_MIDDLEWARE_CB_STATUS_CODES env var로 오버라이드 가능해야 한다."""
        monkeypatch.setenv("BALDUR_MIDDLEWARE_CB_STATUS_CODES", "[400,404,500]")
        s = BaldurMiddlewareSettings()
        assert set(s.cb_status_codes) == {400, 404, 500}

    def test_rate_limit_codes_overridable_via_env(self, monkeypatch):
        """BALDUR_MIDDLEWARE_RATE_LIMIT_CODES env var로 오버라이드 가능해야 한다."""
        monkeypatch.setenv("BALDUR_MIDDLEWARE_RATE_LIMIT_CODES", "[429,503]")
        with pytest.warns(UserWarning, match="overlap"):
            s = BaldurMiddlewareSettings()
        assert set(s.rate_limit_codes) == {429, 503}

    def test_retry_after_max_overridable_via_env(self, monkeypatch):
        """BALDUR_MIDDLEWARE_RETRY_AFTER_MAX env var로 오버라이드 가능해야 한다."""
        monkeypatch.setenv("BALDUR_MIDDLEWARE_RETRY_AFTER_MAX", "120")
        s = BaldurMiddlewareSettings()
        assert s.retry_after_max == 120

    def test_no_overlap_does_not_warn(self):
        """Default config (no overlap) must not emit UserWarning."""
        import warnings as _w

        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            BaldurMiddlewareSettings()
        user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
        assert len(user_warnings) == 0

    def test_overlap_emits_user_warning(self):
        """Overlapping status codes in cb_status_codes and rate_limit_codes must warn."""
        import warnings as _w

        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            BaldurMiddlewareSettings(
                cb_status_codes=[429, 500, 502], rate_limit_codes=[429]
            )
        user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
        assert len(user_warnings) == 1
        assert "429" in str(user_warnings[0].message)


# =============================================================================
# BaldurMiddlewareSettings — Singleton lifecycle
# =============================================================================


class TestBaldurMiddlewareSettingsSingletonBehavior:
    """get_middleware_settings() / reset_middleware_settings() 싱글톤 수명 주기 검증."""

    def setup_method(self):
        reset_middleware_settings()

    def teardown_method(self):
        reset_middleware_settings()

    def test_get_returns_same_instance_on_repeated_calls(self):
        """두 번 호출 시 동일 인스턴스를 반환해야 한다 (cached_property 캐시)."""
        a = get_middleware_settings()
        b = get_middleware_settings()
        assert a is b

    def test_reset_invalidates_cache_and_returns_new_instance(self):
        """reset 후 get을 호출하면 새 인스턴스를 반환해야 한다."""
        a = get_middleware_settings()
        reset_middleware_settings()
        b = get_middleware_settings()
        assert a is not b

    def test_reset_when_not_cached_does_not_raise(self):
        """캐시가 없는 상태에서 reset을 호출해도 예외가 발생하지 않아야 한다."""
        reset_middleware_settings()  # already cleared in setup
        reset_middleware_settings()  # second call: should be a no-op


# =============================================================================
# _is_internal_429 — Contract (decision table)
# =============================================================================


class TestBaldurMiddlewareIsInternal429Contract:
    """_is_internal_429() 헤더 기반 내부/외부 판별 계약 검증."""

    def setup_method(self):
        self.mw = _make_middleware()

    def test_x_ratelimit_mode_header_present_identifies_internal(self):
        """X-RateLimit-Mode 헤더가 있으면 내부 429로 판별해야 한다 (HybridRateLimitMiddleware)."""
        response = FakeResponse(429, {"X-RateLimit-Mode": "normal"})
        assert self.mw._is_internal_429(response) is True

    def test_x_ratelimit_limit_header_present_identifies_internal(self):
        """X-RateLimit-Limit 헤더가 있으면 내부 429로 판별해야 한다."""
        response = FakeResponse(429, {"X-RateLimit-Limit": "100"})
        assert self.mw._is_internal_429(response) is True

    def test_no_ratelimit_headers_identifies_external(self):
        """RateLimit 관련 헤더가 없으면 외부 429로 판별해야 한다."""
        response = FakeResponse(429, {})
        assert self.mw._is_internal_429(response) is False

    def test_empty_x_ratelimit_mode_value_identifies_external(self):
        """X-RateLimit-Mode가 빈 문자열이면 falsy이므로 외부 429로 판별해야 한다."""
        response = FakeResponse(429, {"X-RateLimit-Mode": ""})
        assert self.mw._is_internal_429(response) is False

    def test_unrelated_headers_do_not_influence_result(self):
        """Content-Type, Retry-After 등 무관 헤더는 판별에 영향을 주지 않아야 한다."""
        response = FakeResponse(
            429, {"Content-Type": "application/json", "Retry-After": "60"}
        )
        assert self.mw._is_internal_429(response) is False


# =============================================================================
# _parse_retry_after — Contract
# =============================================================================


class TestBaldurMiddlewareParseRetryAfterContract:
    """_parse_retry_after() 파싱 계약값·경계값 검증."""

    def test_seconds_string_returns_correct_float(self):
        """숫자 초 형식 '120'은 120.0을 반환해야 한다."""
        mw = _make_middleware(retry_after_max=300)
        result = mw._parse_retry_after(FakeResponse(429, {"Retry-After": "120"}))
        assert result == pytest.approx(120.0)

    def test_seconds_exceeding_max_are_clamped_to_max(self):
        """max(300)을 초과하는 600초는 300.0으로 클램핑되어야 한다."""
        mw = _make_middleware(retry_after_max=300)
        result = mw._parse_retry_after(FakeResponse(429, {"Retry-After": "600"}))
        assert result == pytest.approx(300.0)

    def test_seconds_at_exact_max_are_not_clamped(self):
        """max(300)과 정확히 같은 300초는 그대로 300.0을 반환해야 한다."""
        mw = _make_middleware(retry_after_max=300)
        result = mw._parse_retry_after(FakeResponse(429, {"Retry-After": "300"}))
        assert result == pytest.approx(300.0)

    def test_missing_retry_after_header_returns_none(self):
        """Retry-After 헤더가 없으면 None을 반환해야 한다 (fail-open)."""
        mw = _make_middleware()
        result = mw._parse_retry_after(FakeResponse(429, {}))
        assert result is None

    def test_empty_retry_after_header_returns_none(self):
        """Retry-After 헤더가 빈 문자열이면 None을 반환해야 한다."""
        mw = _make_middleware()
        result = mw._parse_retry_after(FakeResponse(429, {"Retry-After": ""}))
        assert result is None

    def test_unparseable_string_returns_none(self):
        """파싱 불가능한 문자열 'garbage'은 None을 반환해야 한다 (fail-open)."""
        mw = _make_middleware()
        result = mw._parse_retry_after(FakeResponse(429, {"Retry-After": "not-a-date"}))
        assert result is None

    def test_past_http_date_returns_none(self):
        """이미 지난 HTTP-date는 seconds < 0이 되어 None을 반환해야 한다."""
        mw = _make_middleware(retry_after_max=9999)
        fixed_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

        with patch(
            "baldur.api.django.middleware.baldur.utc_now",
            return_value=fixed_now,
        ):
            result = mw._parse_retry_after(
                FakeResponse(429, {"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"})
            )

        assert result is None

    def test_future_http_date_returns_positive_seconds(self):
        """미래 HTTP-date는 양수 초를 반환해야 한다."""
        mw = _make_middleware(retry_after_max=999_999)
        fixed_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

        with patch(
            "baldur.api.django.middleware.baldur.utc_now",
            return_value=fixed_now,
        ):
            # 2026-07-01 12:00:00 - 2026-06-01 12:00:00 = 30 days
            result = mw._parse_retry_after(
                FakeResponse(429, {"Retry-After": "Wed, 01 Jul 2026 12:00:00 GMT"})
            )

        assert result is not None
        assert result > 0

    def test_future_http_date_clamped_by_max(self):
        """미래 HTTP-date도 max 초과 시 클램핑되어야 한다."""
        mw = _make_middleware(retry_after_max=60)
        fixed_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

        with patch(
            "baldur.api.django.middleware.baldur.utc_now",
            return_value=fixed_now,
        ):
            result = mw._parse_retry_after(
                FakeResponse(429, {"Retry-After": "Wed, 01 Jul 2026 12:00:00 GMT"})
            )

        assert result == pytest.approx(60.0)


# =============================================================================
# __call__ status code routing — Behavior
# =============================================================================


class TestBaldurMiddlewareCallRoutingBehavior:
    """__call__() 상태 코드별 CB 라우팅 동작 검증."""

    def _mw_returning(self, response: FakeResponse) -> BaldurMiddleware:
        """Builds a pre-initialized middleware that always returns the given response."""
        mw = BaldurMiddleware(get_response=lambda r: response)
        mw._initialized = True
        mw._audit_logger = None
        mw._cb_service = None
        mw._cb_status_codes = frozenset({500, 502, 503, 504})
        mw._rate_limit_codes = frozenset({429})
        mw._retry_after_max = 300
        return mw

    def test_500_response_triggers_cb_failure(self):
        """500 응답은 _record_cb_failure를 호출해야 한다 (D1: 500 신규 추가)."""
        # Given
        mw = self._mw_returning(FakeResponse(500))
        mw._record_cb_failure = Mock()
        mw._record_cb_success = Mock()

        # When
        mw(FakeRequest())

        # Then
        mw._record_cb_failure.assert_called_once()
        mw._record_cb_success.assert_not_called()

    def test_502_response_triggers_cb_failure(self):
        """502 응답은 _record_cb_failure를 호출해야 한다 (기존 동작 유지)."""
        mw = self._mw_returning(FakeResponse(502))
        mw._record_cb_failure = Mock()

        mw(FakeRequest())

        mw._record_cb_failure.assert_called_once()

    def test_503_response_triggers_cb_failure(self):
        """503 응답은 _record_cb_failure를 호출해야 한다 (기존 동작 유지)."""
        mw = self._mw_returning(FakeResponse(503))
        mw._record_cb_failure = Mock()

        mw(FakeRequest())

        mw._record_cb_failure.assert_called_once()

    def test_external_429_calls_handle_external_429(self):
        """RateLimit 헤더가 없는 429는 _handle_external_429를 호출해야 한다."""
        # Given
        response = FakeResponse(429, {})
        mw = self._mw_returning(response)
        mw._handle_external_429 = Mock()

        # When
        mw(FakeRequest())

        # Then
        mw._handle_external_429.assert_called_once()
        _, called_response = mw._handle_external_429.call_args[0]
        assert called_response is response

    def test_internal_429_with_x_ratelimit_mode_skips_cascade_detection(self):
        """429 with X-RateLimit-Mode header must not call _handle_external_429."""
        response = FakeResponse(429, {"X-RateLimit-Mode": "normal"})
        mw = self._mw_returning(response)
        mw._handle_external_429 = Mock()

        mw(FakeRequest())

        mw._handle_external_429.assert_not_called()

    def test_200_response_records_cb_success(self):
        """200 응답은 _record_cb_success를 호출해야 한다."""
        mw = self._mw_returning(FakeResponse(200))
        mw._record_cb_success = Mock()
        mw._record_cb_failure = Mock()

        mw(FakeRequest())

        mw._record_cb_success.assert_called_once()
        mw._record_cb_failure.assert_not_called()

    def test_4xx_non_429_response_triggers_no_cb_operation(self):
        """400 응답은 CB success도 CB failure도 기록하지 않아야 한다."""
        mw = self._mw_returning(FakeResponse(400))
        mw._record_cb_success = Mock()
        mw._record_cb_failure = Mock()

        mw(FakeRequest())

        mw._record_cb_success.assert_not_called()
        mw._record_cb_failure.assert_not_called()

    def test_cb_status_codes_from_settings_used_not_hardcoded(self):
        """cb_status_codes가 설정값에서 로드된 인스턴스 변수를 사용해야 한다."""
        # Override to include 503 only
        response = FakeResponse(500)
        mw = self._mw_returning(response)
        mw._cb_status_codes = frozenset({503})  # 500 NOT in override
        mw._record_cb_failure = Mock()
        mw._record_cb_success = Mock()

        mw(FakeRequest())

        # 500 is not in {503} — should NOT trigger CB failure
        mw._record_cb_failure.assert_not_called()


# =============================================================================
# _handle_external_429 — Behavior
# =============================================================================


class TestBaldurMiddlewareHandleExternal429Behavior:
    """_handle_external_429() CB + EventBus + Retry-After + audit 부수효과 검증."""

    def test_calls_record_rate_limit_response_with_inferred_domain(self):
        """CB 서비스가 있으면 record_rate_limit_response(domain)을 호출해야 한다."""
        # Given
        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.record_rate_limit_response.return_value = None
        mw = _make_middleware(cb_service=mock_cb)
        mw.DOMAIN_MAPPING = {"/payments/": "payment"}
        mw._log_audit_event = Mock()

        # When
        mw._handle_external_429(
            FakeRequest(path="/api/payments/123/"), FakeResponse(429)
        )

        # Then
        mock_cb.record_rate_limit_response.assert_called_once_with("payment")

    def test_no_cb_service_does_not_raise(self):
        """CB 서비스가 None이어도 예외 없이 동작해야 한다 (fail-open)."""
        mw = _make_middleware(cb_service=None)
        mw._log_audit_event = Mock()

        mw._handle_external_429(FakeRequest(), FakeResponse(429))  # must not raise

    def test_cb_service_exception_does_not_propagate(self):
        """CB 호출이 예외를 던져도 메서드가 중단되지 않아야 한다 (fail-open)."""
        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.record_rate_limit_response.side_effect = RuntimeError("redis down")
        mw = _make_middleware(cb_service=mock_cb)
        mw._log_audit_event = Mock()

        mw._handle_external_429(FakeRequest(), FakeResponse(429))  # must not raise

        mw._log_audit_event.assert_called_once()  # audit still runs

    def test_parsed_retry_after_set_on_response(self):
        """Retry-After가 파싱되면 응답 헤더에 정수 초 문자열로 설정해야 한다."""
        mw = _make_middleware(retry_after_max=300)
        mw._log_audit_event = Mock()
        response = FakeResponse(429, {"Retry-After": "120"})

        mw._handle_external_429(FakeRequest(), response)

        assert response.get("Retry-After") == "120"

    def test_clamped_retry_after_set_on_response(self):
        """600초가 max(300)으로 클램핑되어 응답 헤더에 '300'으로 설정되어야 한다."""
        mw = _make_middleware(retry_after_max=300)
        mw._log_audit_event = Mock()
        response = FakeResponse(429, {"Retry-After": "600"})

        mw._handle_external_429(FakeRequest(), response)

        assert response.get("Retry-After") == "300"

    def test_missing_retry_after_not_added_to_response(self):
        """Retry-After 헤더가 없으면 응답에 추가하지 않아야 한다."""
        mw = _make_middleware()
        mw._log_audit_event = Mock()
        response = FakeResponse(429, {})

        mw._handle_external_429(FakeRequest(), response)

        assert response.get("Retry-After") is None

    def test_log_audit_event_called_with_rate_limit_detected(self):
        """_log_audit_event가 'rate_limit_detected' 이벤트로 호출되어야 한다."""
        mw = _make_middleware()
        mw._log_audit_event = Mock()

        mw._handle_external_429(FakeRequest(path="/api/orders/1/"), FakeResponse(429))

        event_type = mw._log_audit_event.call_args[0][0]
        assert event_type == "rate_limit_detected"

    def test_log_audit_event_data_contains_path_and_domain(self):
        """audit 데이터에 path와 domain 키가 포함되어야 한다."""
        mw = _make_middleware()
        mw._log_audit_event = Mock()
        mw.DOMAIN_MAPPING = {"/orders/": "order"}

        mw._handle_external_429(FakeRequest(path="/api/orders/99/"), FakeResponse(429))

        audit_data = mw._log_audit_event.call_args[0][1]
        assert "path" in audit_data
        assert "domain" in audit_data

    def test_emit_rate_limit_event_called_with_rate_limit_429(self):
        """RATE_LIMIT_429 이벤트가 EventBus 헬퍼를 통해 발행되어야 한다."""
        mw = _make_middleware()
        mw._log_audit_event = Mock()

        with patch(
            "baldur.services.rate_limit_coordinator.helpers._emit_rate_limit_event",
        ) as mock_emit:
            mw._handle_external_429(FakeRequest(path="/api/test/"), FakeResponse(429))

        mock_emit.assert_called_once()
        event_type_arg = mock_emit.call_args[0][0]
        assert event_type_arg == "RATE_LIMIT_429"

    def test_emit_rate_limit_event_data_includes_service_name_and_path(self):
        """발행 데이터에 service_name과 path 키가 포함되어야 한다."""
        mw = _make_middleware()
        mw._log_audit_event = Mock()

        with patch(
            "baldur.services.rate_limit_coordinator.helpers._emit_rate_limit_event",
        ) as mock_emit:
            mw._handle_external_429(FakeRequest(path="/api/test/"), FakeResponse(429))

        event_data = mock_emit.call_args[0][1]
        assert "service_name" in event_data
        assert "path" in event_data


# =============================================================================
# _lazy_init settings load failure — Behavior
# =============================================================================


class TestBaldurMiddlewareLazyInitFallbackBehavior:
    """_lazy_init()에서 설정 로드 실패 시 안전 기본값 유지 검증."""

    def test_settings_load_failure_preserves_safe_defaults(self):
        """설정 로드가 RuntimeError를 발생시켜도 안전 기본값이 유지되어야 한다."""
        # Given
        mw = BaldurMiddleware(get_response=lambda r: None)

        # When — force settings load to fail
        with patch(
            "baldur.settings.middleware.get_middleware_settings",
            side_effect=RuntimeError("settings unavailable"),
        ):
            with patch(
                "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service",
                side_effect=Exception("cb unavailable"),
            ):
                with patch(
                    "baldur.audit.get_audit_logger",
                    side_effect=Exception("audit unavailable"),
                ):
                    mw._lazy_init()

        # Then — __init__ safe defaults are intact
        assert 500 in mw._cb_status_codes
        assert 429 in mw._rate_limit_codes
        assert mw._retry_after_max == 300

    def test_initialized_flag_set_even_when_settings_fail(self):
        """설정 로드 실패 후에도 _initialized=True로 설정되어 재초기화를 방지해야 한다."""
        mw = BaldurMiddleware(get_response=lambda r: None)

        with patch(
            "baldur.settings.middleware.get_middleware_settings",
            side_effect=RuntimeError("fail"),
        ):
            with patch(
                "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service",
                side_effect=Exception("fail"),
            ):
                with patch(
                    "baldur.audit.get_audit_logger",
                    side_effect=Exception("fail"),
                ):
                    mw._lazy_init()

        assert mw._initialized is True


# =============================================================================
# CB_DATABASE_DOMAIN constant — Contract (D7)
# =============================================================================


class TestBaldurMiddlewareCbDomainContract:
    """CB_DATABASE_DOMAIN design contract: must equal 'database'."""

    def test_cb_database_domain_constant_is_database(self):
        """CB_DATABASE_DOMAIN must be 'database' (D7 rename from CB_SERVICE_NAME)."""
        assert BaldurMiddleware.CB_DATABASE_DOMAIN == "database"


# =============================================================================
# _record_cb_failure() — Behavior (D3, D6)
# =============================================================================

_POOL_CB_PATH = "baldur.api.django.pool_circuit_breaker.pool_circuit_breaker"


class TestBaldurMiddlewareRecordCbFailureBehavior:
    """_record_cb_failure(service_name, ...) domain routing and pool CB gating."""

    def test_service_name_forwarded_to_cb_service_record_failure(self):
        """service_name is passed directly to cb_service.record_failure() (D3)."""
        # Given
        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.is_enabled = True
        mw = _make_middleware(cb_service=mock_cb)
        mw._log_audit_event = Mock()
        error_ctx = {"error_type": "HTTP_502"}

        # When
        with patch(_POOL_CB_PATH):
            mw._record_cb_failure("payment", error_ctx)

        # Then
        mock_cb.record_failure.assert_called_once_with(
            "payment", error_context=error_ctx
        )

    def test_database_domain_triggers_pool_cb_record_failure(self):
        """pool_circuit_breaker.record_failure() called when service_name == CB_DATABASE_DOMAIN (D6)."""
        # Given
        mw = _make_middleware()
        mw._log_audit_event = Mock()

        # When
        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool._failure_count = 0
            mock_pool.state = "closed"
            mw._record_cb_failure(BaldurMiddleware.CB_DATABASE_DOMAIN, {})

        # Then
        mock_pool.record_failure.assert_called_once()

    def test_non_database_domain_skips_pool_cb_record_failure(self):
        """pool_circuit_breaker.record_failure() NOT called for non-database domains (D6)."""
        # Given
        mw = _make_middleware()
        mw._log_audit_event = Mock()

        # When
        with patch(_POOL_CB_PATH) as mock_pool:
            mw._record_cb_failure("payment", {})

        # Then
        mock_pool.record_failure.assert_not_called()

    def test_http_fallback_domain_skips_pool_cb_record_failure(self):
        """'http' fallback domain (unmapped path) also skips pool CB (D6)."""
        mw = _make_middleware()
        mw._log_audit_event = Mock()

        with patch(_POOL_CB_PATH) as mock_pool:
            mw._record_cb_failure("http", {"error_type": "HTTP_503"})

        mock_pool.record_failure.assert_not_called()

    def test_no_cb_service_does_not_raise(self):
        """_record_cb_failure with cb_service=None must not raise (fail-open)."""
        mw = _make_middleware(cb_service=None)
        mw._log_audit_event = Mock()

        with patch(_POOL_CB_PATH):
            mw._record_cb_failure("payment", {})  # must not raise


# =============================================================================
# _record_cb_success() — Behavior (D4)
# =============================================================================


class TestBaldurMiddlewareRecordCbSuccessBehavior:
    """_record_cb_success(service_name) dual recording for non-database domains."""

    def test_non_database_domain_records_success_for_domain_and_database(self):
        """Non-database domain triggers record_success for both the domain and 'database' (D4)."""
        # Given
        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.is_enabled = True
        mw = _make_middleware(cb_service=mock_cb)

        # When
        mw._record_cb_success("payment")

        # Then — two record_success calls
        assert mock_cb.record_success.call_count == 2
        called_names = {call[0][0] for call in mock_cb.record_success.call_args_list}
        assert "payment" in called_names
        assert BaldurMiddleware.CB_DATABASE_DOMAIN in called_names

    def test_http_fallback_domain_also_dual_records(self):
        """'http' fallback domain (unmapped path) also triggers dual recording (D4)."""
        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.is_enabled = True
        mw = _make_middleware(cb_service=mock_cb)

        mw._record_cb_success("http")

        called_names = {call[0][0] for call in mock_cb.record_success.call_args_list}
        assert "http" in called_names
        assert BaldurMiddleware.CB_DATABASE_DOMAIN in called_names

    def test_database_domain_records_success_exactly_once(self):
        """service_name == CB_DATABASE_DOMAIN: record_success called once, no double-recording (D4)."""
        # Given
        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.is_enabled = True
        mw = _make_middleware(cb_service=mock_cb)

        # When
        mw._record_cb_success(BaldurMiddleware.CB_DATABASE_DOMAIN)

        # Then — exactly one call
        mock_cb.record_success.assert_called_once_with(
            BaldurMiddleware.CB_DATABASE_DOMAIN
        )

    def test_no_cb_service_does_not_raise(self):
        """_record_cb_success with cb_service=None must not raise (fail-open)."""
        mw = _make_middleware(cb_service=None)
        mw._record_cb_success("payment")  # must not raise


# =============================================================================
# _is_cb_open() domain awareness — Behavior (D5)
# =============================================================================


class TestBaldurMiddlewareIsCbOpenBehavior:
    """_is_cb_open(request) checks database CB (shared), domain CB, and pool CB."""

    def _make_cb_service(self, state_map: dict[str, str]) -> MagicMock:
        """Build a CircuitBreakerService mock with per-domain state."""
        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.is_enabled = True
        mock_cb.get_state.side_effect = lambda name: state_map.get(name, "closed")
        return mock_cb

    def test_database_cb_open_returns_true_regardless_of_request_path(self):
        """Database CB OPEN → True for any request path (shared resource, D5)."""
        # Given
        mock_cb = self._make_cb_service({"database": "open"})
        mw = _make_middleware(cb_service=mock_cb)
        mw.DOMAIN_MAPPING = {"/payments/": "payment"}

        # When
        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool.state = "closed"
            result = mw._is_cb_open(FakeRequest(path="/api/payments/"))

        # Then
        assert result is True

    def test_database_cb_half_open_also_blocks(self):
        """Database CB HALF_OPEN → True (half-open is also a blocking state, D5)."""
        mock_cb = self._make_cb_service({"database": "half_open"})
        mw = _make_middleware(cb_service=mock_cb)

        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool.state = "closed"
            result = mw._is_cb_open(FakeRequest())

        assert result is True

    def test_domain_cb_open_returns_true_for_matching_request(self):
        """Domain CB OPEN → True only for requests on that domain (D5)."""
        # Given — database CB closed, payment CB open
        mock_cb = self._make_cb_service({"database": "closed", "payment": "open"})
        mw = _make_middleware(cb_service=mock_cb)
        mw.DOMAIN_MAPPING = {"/payments/": "payment"}

        # When
        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool.state = "closed"
            result = mw._is_cb_open(FakeRequest(path="/api/payments/charge/"))

        # Then
        assert result is True

    def test_domain_cb_open_with_no_request_skips_domain_check(self):
        """Domain CB OPEN but request=None → False (domain check requires request, D5)."""
        # Given — database CB closed, payment CB open
        mock_cb = self._make_cb_service({"database": "closed", "payment": "open"})
        mw = _make_middleware(cb_service=mock_cb)
        mw.DOMAIN_MAPPING = {"/payments/": "payment"}

        # When
        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool.state = "closed"
            result = mw._is_cb_open(request=None)

        # Then — domain CB skipped because request is None
        assert result is False

    def test_domain_equal_to_database_domain_not_double_checked(self):
        """Path that infers to 'database' domain skips the redundant second get_state call (D5)."""
        # Given — DOMAIN_MAPPING maps path to 'database'
        mock_cb = self._make_cb_service({"database": "closed"})
        mw = _make_middleware(cb_service=mock_cb)
        mw.DOMAIN_MAPPING = {"/internal/db/": "database"}

        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool.state = "closed"
            result = mw._is_cb_open(FakeRequest(path="/internal/db/query/"))

        # get_state should have been called exactly once (for database, not again for database)
        assert mock_cb.get_state.call_count == 1
        assert result is False

    def test_all_cbs_closed_returns_false(self):
        """All CBs closed → False (normal operating state)."""
        mock_cb = self._make_cb_service({"database": "closed", "payment": "closed"})
        mw = _make_middleware(cb_service=mock_cb)
        mw.DOMAIN_MAPPING = {"/payments/": "payment"}

        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool.state = "closed"
            result = mw._is_cb_open(FakeRequest(path="/api/payments/"))

        assert result is False

    @pytest.mark.parametrize("pool_state", ["open", "half_open"])
    def test_pool_cb_blocking_state_returns_true(self, pool_state):
        """Pool CB open/half_open → True even when all service CBs are closed."""
        mock_cb = self._make_cb_service({"database": "closed"})
        mw = _make_middleware(cb_service=mock_cb)

        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool.state = pool_state
            result = mw._is_cb_open(FakeRequest())

        assert result is True

    def test_pool_cb_uppercase_state_not_canonical(self):
        """An UPPERCASE pool state no longer matches — live states are lowercase."""
        mock_cb = self._make_cb_service({"database": "closed"})
        mw = _make_middleware(cb_service=mock_cb)

        with patch(_POOL_CB_PATH) as mock_pool:
            mock_pool.state = "OPEN"
            result = mw._is_cb_open(FakeRequest())

        assert result is False

    def test_cb_service_exception_returns_false_not_raises(self):
        """Exception in CB state check → False (fail-open, D5)."""
        mock_cb = MagicMock(spec=CircuitBreakerService)
        mock_cb.is_enabled = True
        mock_cb.get_state.side_effect = RuntimeError("redis down")
        mw = _make_middleware(cb_service=mock_cb)

        result = mw._is_cb_open(FakeRequest())

        assert result is False


# =============================================================================
# __call__() CB domain argument routing — Behavior (D1, D2)
# =============================================================================


class TestBaldurMiddlewareCallCbDomainRoutingBehavior:
    """__call__() passes correct domain to _record_cb_failure / _record_cb_success."""

    def _mw_returning(self, response: FakeResponse) -> BaldurMiddleware:
        """Pre-initialized middleware that always returns the given response."""
        mw = BaldurMiddleware(get_response=lambda r: response)
        mw._initialized = True
        mw._audit_logger = None
        mw._cb_service = None
        mw._cb_status_codes = frozenset({500, 502, 503, 504})
        mw._rate_limit_codes = frozenset({429})
        mw._retry_after_max = 300
        return mw

    def test_5xx_response_passes_inferred_domain_to_record_cb_failure(self):
        """5xx response uses _infer_domain(request.path) as service_name, not 'database' (D1)."""
        # Given
        mw = self._mw_returning(FakeResponse(502))
        mw.DOMAIN_MAPPING = {"/payments/": "payment"}
        mw._record_cb_failure = Mock()

        # When
        mw(FakeRequest(path="/api/payments/charge/"))

        # Then — first positional arg is the inferred domain
        first_arg = mw._record_cb_failure.call_args[0][0]
        assert first_arg == "payment"

    def test_5xx_response_on_unmapped_path_passes_http_fallback_domain(self):
        """5xx on a path with no DOMAIN_MAPPING entry uses 'http' fallback domain (D1)."""
        # Given
        mw = self._mw_returning(FakeResponse(503))
        mw.DOMAIN_MAPPING = {}  # no mappings
        mw._record_cb_failure = Mock()

        # When
        mw(FakeRequest(path="/api/unknown/resource/"))

        # Then
        first_arg = mw._record_cb_failure.call_args[0][0]
        assert first_arg == "http"

    def test_db_exception_passes_database_domain_to_record_cb_failure(self):
        """DB exception always passes CB_DATABASE_DOMAIN to _record_cb_failure (D2)."""

        # Given — get_response raises an OperationalError (matched by class name)
        class OperationalError(Exception):
            pass

        def get_response_raising(r):
            raise OperationalError("connection refused")

        mw = BaldurMiddleware(get_response=get_response_raising)
        mw._initialized = True
        mw._audit_logger = None
        mw._cb_service = None
        mw._cb_status_codes = frozenset({500, 502, 503, 504})
        mw._rate_limit_codes = frozenset({429})
        mw._retry_after_max = 300
        mw._record_cb_failure = Mock()

        # When
        mw(FakeRequest())

        # Then — DB exception path always uses CB_DATABASE_DOMAIN
        first_arg = mw._record_cb_failure.call_args[0][0]
        assert first_arg == BaldurMiddleware.CB_DATABASE_DOMAIN

    def test_200_response_passes_inferred_domain_to_record_cb_success(self):
        """200 response passes _infer_domain(request.path) to _record_cb_success."""
        # Given
        mw = self._mw_returning(FakeResponse(200))
        mw.DOMAIN_MAPPING = {"/orders/": "order"}
        mw._record_cb_success = Mock()

        # When
        mw(FakeRequest(path="/api/orders/42/"))

        # Then
        first_arg = mw._record_cb_success.call_args[0][0]
        assert first_arg == "order"

    def test_preemptive_dlq_check_passes_request_to_is_cb_open(self):
        """Preemptive DLQ path calls _is_cb_open with the current request (D5)."""
        # Given
        mw = self._mw_returning(FakeResponse(200))
        mw._is_cb_open = Mock(return_value=False)

        request = FakeRequest(path="/api/orders/", method="POST")

        # When
        mw(request)

        # Then
        mw._is_cb_open.assert_called_once_with(request)


# =============================================================================
# Pool CB state casing — Contract
# =============================================================================


class TestPoolCircuitBreakerStateCasingContract:
    """Pool CB state constants carry the canonical lowercase enum values."""

    @pytest.mark.parametrize(
        ("constant_name", "canonical_value"),
        [("CLOSED", "closed"), ("OPEN", "open"), ("HALF_OPEN", "half_open")],
    )
    def test_state_constants_are_canonical_lowercase(
        self, constant_name, canonical_value
    ):
        from baldur.api.django.pool_circuit_breaker import PoolCircuitBreaker

        assert getattr(PoolCircuitBreaker, constant_name) == canonical_value
