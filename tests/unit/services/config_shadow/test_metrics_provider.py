"""
Unit tests for Time Series Metrics Provider.

검증 항목:
- TimeSeriesMetricsProvider Protocol 적합성
- MockTimeSeriesProvider: 데이터 주입 및 시간 필터링
- MockTimeSeriesProvider: 빈 데이터, 미등록 키
- MockTimeSeriesProvider: 스칼라 집계 메서드 (error_rate_agg, request_count, latency)
- get_metrics_provider / reset_metrics_provider 싱글톤 라이프사이클

테스트 대상: baldur.services.config_shadow.metrics_provider
"""

from datetime import UTC, datetime

import pytest

from baldur.services.config_shadow.metrics_provider import (
    MockTimeSeriesProvider,
    TimeSeriesMetricsProvider,
    get_metrics_provider,
    is_metrics_provider_registered,
    reset_metrics_provider,
    set_metrics_provider,
)


class TestTimeSeriesMetricsProviderContract:
    """TimeSeriesMetricsProvider Protocol 계약 검증."""

    def test_mock_provider_is_protocol_instance(self):
        """MockTimeSeriesProvider는 TimeSeriesMetricsProvider Protocol을 만족한다."""
        provider = MockTimeSeriesProvider()
        assert isinstance(provider, TimeSeriesMetricsProvider)


class TestMockTimeSeriesProviderBehavior:
    """MockTimeSeriesProvider 동작 검증."""

    def test_query_error_rate_returns_matching_data(self):
        """시간 범위 내 error_rate 데이터를 반환한다."""
        t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 1, 1, 10, 1, tzinfo=UTC)
        t3 = datetime(2026, 1, 1, 10, 2, tzinfo=UTC)

        provider = MockTimeSeriesProvider(
            data={"svc:error_rate": [(t1, 0.01), (t2, 0.05), (t3, 0.1)]}
        )

        result = provider.query_error_rate("svc", start=t1, end=t2)
        assert len(result) == 1
        assert result[0] == (t1, 0.01)

    def test_query_error_rate_empty_for_no_matching_key(self):
        """미등록 키: 빈 리스트 반환."""
        provider = MockTimeSeriesProvider()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        result = provider.query_error_rate("unknown", start=t, end=t)
        assert result == []

    def test_query_request_rate_returns_matching_data(self):
        """시간 범위 내 request_rate 데이터를 반환한다."""
        t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 1, 1, 10, 1, tzinfo=UTC)

        provider = MockTimeSeriesProvider(
            data={"svc:request_rate": [(t1, 100.0), (t2, 200.0)]}
        )

        result = provider.query_request_rate("svc", start=t1, end=t2)
        assert len(result) == 1
        assert result[0] == (t1, 100.0)

    def test_query_filters_by_start_end_exclusive(self):
        """end는 exclusive로 필터링된다 (start <= ts < end)."""
        t1 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 1, 1, 10, 1, tzinfo=UTC)

        provider = MockTimeSeriesProvider(
            data={"svc:error_rate": [(t1, 0.01), (t2, 0.05)]}
        )

        # end=t2이면 t2는 제외
        result = provider.query_error_rate("svc", start=t1, end=t2)
        assert len(result) == 1

    def test_empty_provider_returns_empty(self):
        """데이터 없는 provider: 빈 리스트."""
        provider = MockTimeSeriesProvider()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        assert provider.query_error_rate("any", t, t) == []
        assert provider.query_request_rate("any", t, t) == []


class TestMockTimeSeriesProviderScalarBehavior:
    """MockTimeSeriesProvider 스칼라 집계 메서드 동작 검증."""

    def test_query_error_rate_aggregated_returns_injected_value(self):
        """주입된 error_rate_agg 스칼라를 반환한다."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {"svc:error_rate_agg": 0.035}
        t = datetime(2026, 1, 1, tzinfo=UTC)

        result = provider.query_error_rate_aggregated("svc", start=t, end=t)
        assert result == pytest.approx(0.035)

    def test_query_error_rate_aggregated_defaults_to_zero(self):
        """미등록 키: 0.0 반환."""
        provider = MockTimeSeriesProvider()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        assert provider.query_error_rate_aggregated("unknown", t, t) == 0.0

    def test_query_request_count_returns_injected_value(self):
        """주입된 request_count 스칼라를 정수로 반환한다."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {"svc:request_count": 500.0}
        t = datetime(2026, 1, 1, tzinfo=UTC)

        result = provider.query_request_count("svc", start=t, end=t)
        assert result == 500
        assert isinstance(result, int)

    def test_query_request_count_defaults_to_zero(self):
        """미등록 키: 0 반환."""
        provider = MockTimeSeriesProvider()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        assert provider.query_request_count("unknown", t, t) == 0

    def test_query_latency_aggregated_p95_returns_correct_key(self):
        """percentile=0.95 시 'latency_p95' 키에서 값을 조회한다."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {"svc:latency_p95": 42.5}
        t = datetime(2026, 1, 1, tzinfo=UTC)

        result = provider.query_latency_aggregated(
            "svc", start=t, end=t, percentile=0.95
        )
        assert result == pytest.approx(42.5)

    def test_query_latency_aggregated_p99_returns_correct_key(self):
        """percentile=0.99 시 'latency_p99' 키에서 값을 조회한다."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {"svc:latency_p99": 150.0}
        t = datetime(2026, 1, 1, tzinfo=UTC)

        result = provider.query_latency_aggregated(
            "svc", start=t, end=t, percentile=0.99
        )
        assert result == pytest.approx(150.0)

    def test_query_latency_aggregated_defaults_to_zero(self):
        """미등록 latency 키: 0.0 반환."""
        provider = MockTimeSeriesProvider()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        assert provider.query_latency_aggregated("unknown", t, t) == 0.0


class TestMockTimeSeriesProviderLabelAwareBehavior:
    """MockTimeSeriesProvider label-aware 키 분리 동작 검증."""

    def test_labeled_scalar_returns_label_specific_value(self):
        """label 키가 등록되면 해당 label에 맞는 값을 반환한다."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg:track=stable": 0.01,
            "svc:error_rate_agg:track=canary": 0.05,
        }
        t = datetime(2026, 1, 1, tzinfo=UTC)

        stable = provider.query_error_rate_aggregated(
            "svc", t, t, labels={"track": "stable"}
        )
        canary = provider.query_error_rate_aggregated(
            "svc", t, t, labels={"track": "canary"}
        )
        assert stable == pytest.approx(0.01)
        assert canary == pytest.approx(0.05)

    def test_unlabeled_fallback_when_no_labeled_key(self):
        """label 키가 없으면 label 없는 기본 키로 폴백한다."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {"svc:error_rate_agg": 0.03}
        t = datetime(2026, 1, 1, tzinfo=UTC)

        result = provider.query_error_rate_aggregated(
            "svc", t, t, labels={"track": "canary"}
        )
        assert result == pytest.approx(0.03)

    def test_labeled_key_takes_priority_over_unlabeled(self):
        """label 키와 unlabeled 키가 모두 있으면 label 키가 우선한다."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:latency_p99": 100.0,
            "svc:latency_p99:track=canary": 250.0,
        }
        t = datetime(2026, 1, 1, tzinfo=UTC)

        result = provider.query_latency_aggregated(
            "svc", t, t, percentile=0.99, labels={"track": "canary"}
        )
        assert result == pytest.approx(250.0)

    def test_multi_label_key_sorted(self):
        """복수 레이블은 키 이름순으로 정렬되어 키가 구성된다."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:request_count:namespace=prod,track=canary": 1000,
        }
        t = datetime(2026, 1, 1, tzinfo=UTC)

        result = provider.query_request_count(
            "svc", t, t, labels={"track": "canary", "namespace": "prod"}
        )
        assert result == 1000

    def test_none_labels_uses_unlabeled_key(self):
        """labels=None 시 기존 unlabeled 키를 사용한다 (하위 호환)."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {"svc:error_rate_agg": 0.02}
        t = datetime(2026, 1, 1, tzinfo=UTC)

        result = provider.query_error_rate_aggregated("svc", t, t, labels=None)
        assert result == pytest.approx(0.02)


class TestMetricsProviderSingletonBehavior:
    """get_metrics_provider / set / reset 싱글톤 라이프사이클 검증."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """각 테스트 전후에 싱글톤을 리셋한다."""
        reset_metrics_provider()
        yield
        reset_metrics_provider()

    def test_get_returns_same_instance(self):
        """get_metrics_provider()는 동일 인스턴스를 반환한다."""
        first = get_metrics_provider()
        second = get_metrics_provider()
        assert first is second

    def test_get_returns_mock_by_default(self):
        """기본 provider는 MockTimeSeriesProvider이다."""
        provider = get_metrics_provider()
        assert isinstance(provider, MockTimeSeriesProvider)

    def test_reset_clears_cached_instance(self):
        """reset 후 새 인스턴스가 생성된다."""
        first = get_metrics_provider()
        reset_metrics_provider()
        second = get_metrics_provider()
        assert first is not second

    def test_set_overrides_provider(self):
        """set_metrics_provider()로 커스텀 provider를 등록할 수 있다."""
        custom = MockTimeSeriesProvider()
        set_metrics_provider(custom)
        assert get_metrics_provider() is custom


class TestMetricsProviderRegistrationBehavior:
    """is_metrics_provider_registered() explicit-registration tracking."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        reset_metrics_provider()
        yield
        reset_metrics_provider()

    def test_not_registered_by_default(self):
        """No registration mark before any set_metrics_provider() call."""
        assert is_metrics_provider_registered() is False

    def test_lazy_mock_default_does_not_count_as_registered(self):
        """get_metrics_provider()'s lazy Mock fallback is not a registration."""
        get_metrics_provider()

        assert is_metrics_provider_registered() is False

    def test_set_marks_registered(self):
        """set_metrics_provider() marks the provider registered."""
        set_metrics_provider(MockTimeSeriesProvider())

        assert is_metrics_provider_registered() is True

    def test_deliberately_registered_mock_counts(self):
        """An explicitly registered Mock counts (tests/staging registration)."""
        set_metrics_provider(MockTimeSeriesProvider())

        assert is_metrics_provider_registered() is True
        assert isinstance(get_metrics_provider(), MockTimeSeriesProvider)

    def test_reset_clears_registration_mark(self):
        """reset_metrics_provider() clears the registration mark."""
        set_metrics_provider(MockTimeSeriesProvider())
        reset_metrics_provider()

        assert is_metrics_provider_registered() is False
