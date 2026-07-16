"""
CellHealthAggregator 단위 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 가중치, 정규화 기준, 저트래픽 보호 임계값 계약 검증
- Behavior: 건강도 산출, EWMA 스무딩, 2-Tier 소스 전환, 스냅샷 동작 검증

참조 소스:
- services/cell_topology/health.py (CellHealthAggregator, CellHealthSnapshot)
- core/time_series.py (EWMAForecaster)
- settings/cell_topology.py (CellTopologySettings)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.cell_topology.health import (
    CellHealthAggregator,
    CellHealthSnapshot,
    get_cell_health_aggregator,
    reset_cell_health_aggregator,
)
from baldur.settings.cell_topology import (
    CellTopologySettings,
    reset_cell_topology_settings,
)

# Settings-sourced constants (previously module-level in health.py)
_default_settings = CellTopologySettings(enabled=True, metrics_enabled=False)
_PROMETHEUS_TIMEOUT = _default_settings.prometheus_timeout_seconds
_PROMETHEUS_MAX_CONSECUTIVE_FAILURES = (
    _default_settings.prometheus_max_consecutive_failures
)
_PROMETHEUS_RETRY_AFTER_SECONDS = _default_settings.prometheus_retry_after_seconds


@pytest.fixture(autouse=True)
def _reset_singletons():
    """각 테스트 전후 싱글톤 리셋."""
    reset_cell_health_aggregator()
    reset_cell_topology_settings()
    yield
    reset_cell_health_aggregator()
    reset_cell_topology_settings()


@pytest.fixture
def settings() -> CellTopologySettings:
    """테스트용 CellTopologySettings (메트릭 비활성화)."""
    return CellTopologySettings(
        enabled=True,
        metrics_enabled=False,
        health_check_interval_seconds=10,
    )


@pytest.fixture
def aggregator(settings: CellTopologySettings) -> CellHealthAggregator:
    """Prometheus 실패로 폴백 강제 사용하는 CellHealthAggregator."""
    agg = CellHealthAggregator(settings=settings)
    # Prometheus 연속 실패 임계값을 채워 항상 폴백 모드 사용
    agg._prometheus_consecutive_failures = _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
    # half-open probe 억제: "방금 실패"로 설정하여 _PROMETHEUS_RETRY_AFTER_SECONDS 대기 강제
    # 미설정 시 _last_prometheus_failure_time=0.0이므로 elapsed가 항상 > 60s가 되어
    # half-open probe가 실제 HTTP 호출(localhost:9090)을 시도하고 ~4.3s 타임아웃 발생
    agg._last_prometheus_failure_time = time.monotonic()
    return agg


# =============================================================================
# 계약 검증 (Contract)
# =============================================================================


class TestCellHealthAggregatorWeightContract:
    """건강도 산출 가중치 설계 계약값 검증 (CellTopologySettings 기반)."""

    def test_weight_error_rate(self):
        """에러율 가중치 기본값은 0.35이어야 한다."""
        assert _default_settings.health_weight_error_rate == 0.35

    def test_weight_latency(self):
        """레이턴시 가중치 기본값은 0.25이어야 한다."""
        assert _default_settings.health_weight_latency == 0.25

    def test_weight_bulkhead(self):
        """Bulkhead 가중치 기본값은 0.20이어야 한다."""
        assert _default_settings.health_weight_bulkhead == 0.20

    def test_weight_cb_open(self):
        """CB Open 가중치 기본값은 0.20이어야 한다."""
        assert _default_settings.health_weight_cb_open == 0.20

    def test_weights_sum_to_one(self):
        """모든 가중치의 합은 1.0이어야 한다."""
        total = (
            _default_settings.health_weight_error_rate
            + _default_settings.health_weight_latency
            + _default_settings.health_weight_bulkhead
            + _default_settings.health_weight_cb_open
        )
        assert total == pytest.approx(1.0)


class TestCellHealthAggregatorNormalizationContract:
    """정규화 기준 설계 계약값 검증 (CellTopologySettings 기반)."""

    def test_max_error_rate(self):
        """에러율 정규화 기준 기본값은 0.5 (50%)이어야 한다."""
        assert _default_settings.health_max_error_rate == 0.5

    def test_max_latency_p99(self):
        """P99 레이턴시 정규화 기준 기본값은 5.0초이어야 한다."""
        assert _default_settings.health_max_latency_p99 == 5.0

    def test_min_samples_for_penalty(self):
        """저트래픽 보호 최소 표본 수 기본값은 10이어야 한다."""
        assert _default_settings.health_min_samples_for_penalty == 10


class TestPrometheusConfigContract:
    """Prometheus API 설정 계약값 검증."""

    def test_prometheus_timeout(self):
        """Prometheus API 타임아웃 기본값은 3.0초이어야 한다."""
        default = CellTopologySettings.model_fields[
            "prometheus_timeout_seconds"
        ].default
        assert default == 3.0

    def test_prometheus_max_consecutive_failures(self):
        """Prometheus 연속 실패 임계값은 3이어야 한다."""
        assert _PROMETHEUS_MAX_CONSECUTIVE_FAILURES == 3

    def test_prometheus_retry_after_seconds(self):
        """Prometheus half-open probe 재시도 대기는 60초이어야 한다."""
        assert _PROMETHEUS_RETRY_AFTER_SECONDS == 60.0


class TestCellHealthSnapshotContract:
    """CellHealthSnapshot 기본값 계약 검증."""

    def test_default_source(self):
        """기본 데이터 소스는 'prometheus'이어야 한다."""
        snapshot = CellHealthSnapshot(cell_id="cell-0", health_score=1.0)
        assert snapshot.source == "prometheus"

    def test_default_raw_health_score(self):
        """기본 raw_health_score는 0.0이어야 한다."""
        snapshot = CellHealthSnapshot(cell_id="cell-0", health_score=1.0)
        assert snapshot.raw_health_score == 0.0

    def test_default_error_rate(self):
        """기본 error_rate는 0.0이어야 한다."""
        snapshot = CellHealthSnapshot(cell_id="cell-0", health_score=1.0)
        assert snapshot.error_rate == 0.0

    def test_timestamp_auto_generated(self):
        """timestamp는 자동 생성되어야 한다."""
        before = time.time()
        snapshot = CellHealthSnapshot(cell_id="cell-0", health_score=1.0)
        after = time.time()
        assert before <= snapshot.timestamp <= after


# =============================================================================
# 동작 검증 (Behavior)
# =============================================================================


class TestRecordRequestBehavior:
    """record_request() 동작 검증."""

    def test_creates_ewma_on_first_request(self, aggregator: CellHealthAggregator):
        """첫 요청 시 EWMA 인스턴스가 생성되어야 한다."""
        aggregator.record_request("cell-0", success=True, latency=0.1)

        assert "cell-0" in aggregator._error_rate_ewma
        assert "cell-0" in aggregator._latency_ewma

    def test_increments_local_request_count(self, aggregator: CellHealthAggregator):
        """요청 기록 시 로컬 카운트가 증가해야 한다."""
        aggregator.record_request("cell-0", success=True, latency=0.1)
        aggregator.record_request("cell-0", success=False, latency=0.2)

        assert aggregator._local_request_counts["cell-0"] == 2

    def test_error_updates_ewma_with_one(self, aggregator: CellHealthAggregator):
        """실패 요청 시 에러율 EWMA에 1.0이 입력되어야 한다."""
        aggregator.record_request("cell-0", success=False, latency=0.1)

        smoothed = aggregator._error_rate_ewma["cell-0"].get_smoothed()
        assert smoothed == 1.0  # 첫 값이므로 EWMA = 입력값

    def test_success_updates_ewma_with_zero(self, aggregator: CellHealthAggregator):
        """성공 요청 시 에러율 EWMA에 0.0이 입력되어야 한다."""
        aggregator.record_request("cell-0", success=True, latency=0.1)

        smoothed = aggregator._error_rate_ewma["cell-0"].get_smoothed()
        assert smoothed == 0.0

    def test_latency_tracked_via_ewma(self, aggregator: CellHealthAggregator):
        """레이턴시가 EWMA로 추적되어야 한다."""
        aggregator.record_request("cell-0", success=True, latency=0.5)

        smoothed = aggregator._latency_ewma["cell-0"].get_smoothed()
        assert smoothed == 0.5  # 첫 값이므로 EWMA = 입력값

    def test_multiple_cells_tracked_independently(
        self, aggregator: CellHealthAggregator
    ):
        """서로 다른 Cell의 요청이 독립적으로 추적되어야 한다."""
        aggregator.record_request("cell-0", success=True, latency=0.1)
        aggregator.record_request("cell-1", success=False, latency=0.5)

        assert aggregator._local_request_counts["cell-0"] == 1
        assert aggregator._local_request_counts["cell-1"] == 1
        assert aggregator._error_rate_ewma["cell-0"].get_smoothed() == 0.0
        assert aggregator._error_rate_ewma["cell-1"].get_smoothed() == 1.0


class TestComputeHealthBehavior:
    """compute_health() 건강도 산출 동작 검증."""

    def test_perfect_health_with_no_requests(self, aggregator: CellHealthAggregator):
        """요청이 없으면 건강도는 1.0이어야 한다."""
        score = aggregator.compute_health("cell-0")
        assert score == pytest.approx(1.0)

    def test_min_samples_guard_exempts_error_penalty(
        self, aggregator: CellHealthAggregator
    ):
        """최소 표본 수 미달 시 에러율 페널티가 면제되어야 한다."""
        # MIN_SAMPLES_FOR_PENALTY 미만의 실패 요청 기록
        for _ in range(_default_settings.health_min_samples_for_penalty - 1):
            aggregator.record_request("cell-0", success=False, latency=0.01)

        score = aggregator.compute_health("cell-0")
        # 에러율 면제이므로 레이턴시 페널티만 존재 (매우 작음)
        assert score > 0.9

    def test_high_error_rate_degrades_health(self, aggregator: CellHealthAggregator):
        """높은 에러율은 건강도를 낮춰야 한다."""
        # MIN_SAMPLES_FOR_PENALTY 이상 요청, 모두 실패
        for _ in range(_default_settings.health_min_samples_for_penalty + 5):
            aggregator.record_request("cell-0", success=False, latency=0.01)

        score = aggregator.compute_health("cell-0")
        assert score < 0.7

    def test_high_latency_degrades_health(self, aggregator: CellHealthAggregator):
        """높은 레이턴시는 건강도를 낮춰야 한다."""
        # MIN_SAMPLES_FOR_PENALTY 이상, 성공이지만 5초 레이턴시
        for _ in range(_default_settings.health_min_samples_for_penalty + 5):
            aggregator.record_request(
                "cell-0",
                success=True,
                latency=_default_settings.health_max_latency_p99,
            )

        score = aggregator.compute_health("cell-0")
        # 레이턴시 정규화 = 1.0, 가중치 0.25 적용
        assert score < 0.8

    def test_health_score_range(self, aggregator: CellHealthAggregator):
        """건강도는 항상 0.0~1.0 범위이어야 한다."""
        # 모든 시그널이 최악
        for _ in range(20):
            aggregator.record_request("cell-0", success=False, latency=10.0)

        score = aggregator.compute_health("cell-0")
        assert 0.0 <= score <= 1.0

    def test_ewma_smoothing_applied(self, aggregator: CellHealthAggregator):
        """EWMA 스무딩이 적용되어 raw와 smoothed가 다를 수 있다."""
        # 1차: 모두 성공 → 건강
        for _ in range(15):
            aggregator.record_request("cell-0", success=True, latency=0.01)
        aggregator.compute_health("cell-0")

        # 2차: 모두 실패로 전환 → 급격한 변화
        for _ in range(15):
            aggregator.record_request("cell-0", success=False, latency=5.0)
        aggregator.compute_health("cell-0")

        snapshot = aggregator.get_snapshot("cell-0")
        assert snapshot is not None
        # raw는 smoothed보다 더 극단적이어야 한다 (EWMA 감쇠 효과)
        assert snapshot.raw_health_score <= snapshot.health_score


class TestComputeHealthFormulaVerificationBehavior:
    """건강도 공식 세부 동작 검증."""

    def test_error_rate_normalization_capped_at_one(
        self, aggregator: CellHealthAggregator
    ):
        """에러율 정규화는 1.0에서 캡된다 (100% 에러율 = 1.0)."""
        # 100% 에러율 (MAX_ERROR_RATE 이상)
        for _ in range(15):
            aggregator.record_request("cell-0", success=False, latency=0.01)

        aggregator.compute_health("cell-0")
        snapshot = aggregator.get_snapshot("cell-0")
        assert snapshot is not None
        # error_rate이 MAX_ERROR_RATE를 넘어도 정규화 = 1.0
        error_norm = min(
            snapshot.error_rate / _default_settings.health_max_error_rate, 1.0
        )
        assert error_norm <= 1.0

    def test_latency_normalization_capped_at_one(
        self, aggregator: CellHealthAggregator
    ):
        """레이턴시 정규화는 1.0에서 캡된다 (5초 이상 = 1.0)."""
        for _ in range(15):
            aggregator.record_request("cell-0", success=True, latency=10.0)

        aggregator.compute_health("cell-0")
        snapshot = aggregator.get_snapshot("cell-0")
        assert snapshot is not None
        latency_norm = min(
            snapshot.latency_p99 / _default_settings.health_max_latency_p99, 1.0
        )
        assert latency_norm <= 1.0


class TestCBOpenRatioBehavior:
    """CB Open Ratio 동작 검증."""

    def test_no_cb_events_returns_zero(self, aggregator: CellHealthAggregator):
        """CB 이벤트가 없으면 비율은 0.0이어야 한다."""
        ratio = aggregator._get_cb_open_ratio("cell-0")
        assert ratio == 0.0

    def test_cb_open_increments_counters(self, aggregator: CellHealthAggregator):
        """CB OPEN 상태 CB가 있으면 비율이 올바르게 계산되어야 한다."""
        from baldur.core.cb_namespace import (
            make_cell_scoped_cb_name,
        )

        mock_service = MagicMock()
        mock_service.get_all_states.return_value = [
            {
                "service_name": make_cell_scoped_cb_name("svc-a", "cell-0"),
                "state": "open",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-b", "cell-0"),
                "state": "open",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-c", "cell-0"),
                "state": "closed",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-d", "cell-0"),
                "state": "closed",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-e", "cell-0"),
                "state": "closed",
            },
        ]

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            ratio = aggregator._get_cb_open_ratio("cell-0")
        assert ratio == pytest.approx(2 / 5)

    def test_cb_closed_decrements_open_count(self, aggregator: CellHealthAggregator):
        """CB CLOSED 상태 변경 후 open 비율이 감소해야 한다."""
        from baldur.core.cb_namespace import (
            make_cell_scoped_cb_name,
        )

        # 초기 상태: 3 OPEN / 5 총
        mock_service = MagicMock()
        mock_service.get_all_states.return_value = [
            {
                "service_name": make_cell_scoped_cb_name("svc-a", "cell-0"),
                "state": "open",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-b", "cell-0"),
                "state": "open",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-c", "cell-0"),
                "state": "closed",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-d", "cell-0"),
                "state": "closed",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-e", "cell-0"),
                "state": "closed",
            },
        ]

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            ratio = aggregator._get_cb_open_ratio("cell-0")
        assert ratio == pytest.approx(2 / 5)


class TestBulkheadUtilizationBehavior:
    """Bulkhead utilization behavior (core registry)."""

    def test_no_bulkhead_returns_zero(self, aggregator: CellHealthAggregator):
        """BulkheadRegistry에 데이터 없으면 0.0이어야 한다."""
        with patch(
            "baldur.services.bulkhead.registry.get_bulkhead_registry"
        ) as mock_get:
            mock_registry = MagicMock()
            mock_registry.get_all_states.return_value = {}
            mock_get.return_value = mock_registry

            util = aggregator._get_bulkhead_utilization("cell-0")
            assert util == 0.0

    def test_bulkhead_utilization_calculated(self, aggregator: CellHealthAggregator):
        """Bulkhead 사용률이 active/max 비율로 계산되어야 한다."""

        @dataclass
        class MockBulkheadState:
            max_concurrent: int = 100
            active_count: int = 50

        with patch(
            "baldur.services.bulkhead.registry.get_bulkhead_registry"
        ) as mock_get:
            mock_registry = MagicMock()
            mock_registry.get_all_states.return_value = {
                "cell-0": MockBulkheadState(max_concurrent=100, active_count=50)
            }
            mock_get.return_value = mock_registry

            util = aggregator._get_bulkhead_utilization("cell-0")
            assert util == pytest.approx(0.5)

    def test_bulkhead_utilization_capped_at_one(self, aggregator: CellHealthAggregator):
        """Bulkhead 사용률은 1.0에서 캡되어야 한다."""

        @dataclass
        class MockBulkheadState:
            max_concurrent: int = 10
            active_count: int = 20  # 초과 상태

        with patch(
            "baldur.services.bulkhead.registry.get_bulkhead_registry"
        ) as mock_get:
            mock_registry = MagicMock()
            mock_registry.get_all_states.return_value = {
                "cell-0": MockBulkheadState(max_concurrent=10, active_count=20)
            }
            mock_get.return_value = mock_registry

            util = aggregator._get_bulkhead_utilization("cell-0")
            assert util == 1.0

    def test_bulkhead_import_failure_returns_zero(
        self, aggregator: CellHealthAggregator
    ):
        """BulkheadRegistry import 실패 시 0.0이어야 한다."""
        with patch(
            "baldur.services.bulkhead.registry.get_bulkhead_registry",
            side_effect=ImportError("not installed"),
        ):
            util = aggregator._get_bulkhead_utilization("cell-0")
            assert util == 0.0


class TestPrometheusFallbackBehavior:
    """Prometheus → EWMA 폴백 전환 동작 검증."""

    def test_initial_prometheus_failures_counted(self, settings: CellTopologySettings):
        """Prometheus 실패 시 연속 실패 카운트가 증가해야 한다."""
        agg = CellHealthAggregator(
            settings=settings, prometheus_url="http://localhost:9999"
        )

        with patch(
            "baldur.services.cell_topology.health.safe_urlopen",
            side_effect=ConnectionError("mocked"),
        ):
            result = agg._fetch_prometheus_metrics("cell-0")
        assert result is None
        assert agg._prometheus_consecutive_failures == 1

    def test_fallback_mode_after_max_consecutive_failures(
        self, settings: CellTopologySettings
    ):
        """연속 실패 임계값 도달 시 대기 시간 내에는 API 호출 없이 None 반환해야 한다."""
        agg = CellHealthAggregator(settings=settings)
        agg._prometheus_consecutive_failures = _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
        # half-open probe 억제: 방금 실패한 것처럼 시간 설정
        agg._last_prometheus_failure_time = time.monotonic()

        # 실패 카운트가 이미 임계치 + 대기 미경과 → 즉시 None (HTTP 호출 없이)
        result = agg._fetch_prometheus_metrics("cell-0")
        assert result is None
        # 카운트가 더 증가하지 않아야 한다 (API 호출 자체를 건너뛰므로)
        assert agg._prometheus_consecutive_failures == (
            _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
        )

    def test_compute_health_uses_fallback_source(
        self, aggregator: CellHealthAggregator
    ):
        """Prometheus 불가용 시 폴백 소스를 사용해야 한다."""
        aggregator.record_request("cell-0", success=True, latency=0.1)
        aggregator.compute_health("cell-0")

        snapshot = aggregator.get_snapshot("cell-0")
        assert snapshot is not None
        assert snapshot.source == "ewma_fallback"

    def test_half_open_probe_after_retry_interval(self, settings: CellTopologySettings):
        """half-open: 대기 시간 경과 후 Prometheus probe를 재시도해야 한다."""
        agg = CellHealthAggregator(
            settings=settings, prometheus_url="http://invalid:9999"
        )
        # 임계값 도달 + 불가능한 URL로 반드시 실패하는 상태
        agg._prometheus_consecutive_failures = _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
        # 대기 시간 미경과 → 즉시 None
        agg._last_prometheus_failure_time = time.monotonic()
        result = agg._fetch_prometheus_metrics("cell-0")
        assert result is None
        # 카운트 증가 없음 (HTTP 호출 자체를 건너뜀)
        assert agg._prometheus_consecutive_failures == (
            _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
        )

    def test_half_open_probe_attempts_after_elapsed(
        self, settings: CellTopologySettings
    ):
        """half-open: 대기 시간 경과 시 HTTP 호출을 시도해야 한다."""
        agg = CellHealthAggregator(
            settings=settings, prometheus_url="http://localhost:9999"
        )
        agg._prometheus_consecutive_failures = _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
        # 초과된 시간 설정 (현재보다 retry_after 이상 과거)
        agg._last_prometheus_failure_time = (
            time.monotonic() - _PROMETHEUS_RETRY_AFTER_SECONDS - 1.0
        )
        # safe_urlopen mock: fail immediately without a real network call
        with patch(
            "baldur.services.cell_topology.health.safe_urlopen",
            side_effect=ConnectionError("mocked"),
        ):
            # probe attempt → fails but the count must increment (HTTP actually tried)
            result = agg._fetch_prometheus_metrics("cell-0")
        assert result is None
        assert agg._prometheus_consecutive_failures == (
            _PROMETHEUS_MAX_CONSECUTIVE_FAILURES + 1
        )


class TestParsePrometheusScalarBehavior:
    """_parse_prometheus_scalar() 동작 검증."""

    def test_successful_response(self):
        """A scalar value must be extracted from a normal Prometheus body."""
        data = {
            "status": "success",
            "data": {"result": [{"value": [1234567890, "0.42"]}]},
        }

        result = CellHealthAggregator._parse_prometheus_scalar(data)
        assert result == pytest.approx(0.42)

    def test_empty_result_returns_zero(self):
        """An empty result set must return 0.0."""
        data = {
            "status": "success",
            "data": {"result": []},
        }

        result = CellHealthAggregator._parse_prometheus_scalar(data)
        assert result == 0.0

    def test_error_status_returns_zero(self):
        """An error-status body must return 0.0."""
        data = {"status": "error", "error": "bad query"}

        result = CellHealthAggregator._parse_prometheus_scalar(data)
        assert result == 0.0


class TestSnapshotBehavior:
    """스냅샷 조회 동작 검증."""

    def test_get_snapshot_returns_none_for_unknown_cell(
        self, aggregator: CellHealthAggregator
    ):
        """미등록 Cell의 스냅샷은 None이어야 한다."""
        assert aggregator.get_snapshot("cell-99") is None

    def test_get_snapshot_after_compute(self, aggregator: CellHealthAggregator):
        """compute_health() 후 스냅샷이 저장되어야 한다."""
        aggregator.record_request("cell-0", success=True, latency=0.1)
        aggregator.compute_health("cell-0")

        snapshot = aggregator.get_snapshot("cell-0")
        assert snapshot is not None
        assert snapshot.cell_id == "cell-0"
        assert 0.0 <= snapshot.health_score <= 1.0

    def test_get_all_snapshots_empty(self, aggregator: CellHealthAggregator):
        """초기 상태에서 스냅샷 딕셔너리가 비어있어야 한다."""
        snapshots = aggregator.get_all_snapshots()
        assert snapshots == {}

    def test_get_all_snapshots_returns_copy(self, aggregator: CellHealthAggregator):
        """get_all_snapshots()는 원본이 아닌 복사본을 반환해야 한다."""
        aggregator.compute_health("cell-0")

        snapshots = aggregator.get_all_snapshots()
        snapshots["cell-99"] = CellHealthSnapshot(cell_id="cell-99", health_score=0.5)

        # 원본에는 영향 없어야 한다
        assert aggregator.get_snapshot("cell-99") is None

    def test_snapshot_contains_all_signals(self, aggregator: CellHealthAggregator):
        """스냅샷이 모든 시그널(error_rate, latency_p99 등)을 포함해야 한다."""
        aggregator.record_request("cell-0", success=True, latency=0.2)
        aggregator.compute_health("cell-0")

        snapshot = aggregator.get_snapshot("cell-0")
        assert snapshot is not None
        assert hasattr(snapshot, "error_rate")
        assert hasattr(snapshot, "latency_p99")
        assert hasattr(snapshot, "bulkhead_utilization")
        assert hasattr(snapshot, "cb_open_ratio")
        assert hasattr(snapshot, "raw_health_score")
        assert hasattr(snapshot, "health_score")
        assert hasattr(snapshot, "source")


class TestLeaderHandoffBehavior:
    """리더 전환 동작 검증."""

    def test_on_become_leader_records_timestamp(self, aggregator: CellHealthAggregator):
        """리더 전환 시 시작 시각이 기록되어야 한다."""
        assert aggregator._leader_since is None
        aggregator.on_become_leader()
        assert aggregator._leader_since is not None

    def test_on_lose_leader_clears_timestamp(self, aggregator: CellHealthAggregator):
        """리더십 상실 시 시작 시각이 초기화되어야 한다."""
        aggregator.on_become_leader()
        assert aggregator._leader_since is not None
        aggregator.on_lose_leader()
        assert aggregator._leader_since is None


class TestAggregateAllBehavior:
    """aggregate_all() 동작 검증."""

    def test_aggregate_all_updates_registry(self, aggregator: CellHealthAggregator):
        """aggregate_all()이 CellRegistry.update_health_score()를 호출해야 한다."""
        mock_registry = MagicMock()
        mock_registry.get_all_cells.return_value = {
            "cell-0": MagicMock(),
            "cell-1": MagicMock(),
        }

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=mock_registry,
        ):
            aggregator.aggregate_all()

        assert mock_registry.update_health_score.call_count == 2

    def test_aggregate_all_logs_warmup(self, aggregator: CellHealthAggregator):
        """리더 warmup 기간 중 aggregate_all()이 정상 실행되어야 한다."""
        aggregator.on_become_leader()

        mock_registry = MagicMock()
        mock_registry.get_all_cells.return_value = {"cell-0": MagicMock()}

        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            return_value=mock_registry,
        ):
            # 예외 없이 실행되어야 한다
            aggregator.aggregate_all()

        mock_registry.update_health_score.assert_called_once()

    def test_aggregate_all_handles_exception(self, aggregator: CellHealthAggregator):
        """aggregate_all() 내부 예외가 전파되지 않아야 한다."""
        with patch(
            "baldur.services.cell_topology.get_cell_registry",
            side_effect=RuntimeError("test error"),
        ):
            # 예외 없이 처리되어야 한다
            aggregator.aggregate_all()

    def test_aggregate_all_calls_evacuation_policy(self):
        """evacuation_enabled=True이면 건강도 갱신 후 대피 정책을 평가한다."""
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=True,
            metrics_enabled=False,
        )
        agg = CellHealthAggregator(settings=settings)
        agg._prometheus_consecutive_failures = _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
        agg._last_prometheus_failure_time = time.monotonic()

        mock_cell = MagicMock()
        mock_cell.health_score = 0.5
        mock_registry = MagicMock()
        mock_registry.get_all_cells.return_value = {"cell-0": mock_cell}

        mock_policy = MagicMock()

        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=mock_registry,
            ),
            patch(
                "baldur.services.cell_topology.policy.get_cell_evacuation_policy",
                return_value=mock_policy,
            ),
        ):
            agg.aggregate_all()

        mock_policy.evaluate.assert_called_once_with("cell-0", mock_cell.health_score)

    def test_aggregate_all_skips_evacuation_when_disabled(self):
        """evacuation_enabled=False이면 대피 정책 평가를 건너뛴다."""
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=False,
            metrics_enabled=False,
        )
        agg = CellHealthAggregator(settings=settings)
        agg._prometheus_consecutive_failures = _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
        agg._last_prometheus_failure_time = time.monotonic()

        mock_registry = MagicMock()
        mock_registry.get_all_cells.return_value = {"cell-0": MagicMock()}

        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=mock_registry,
            ),
            patch(
                "baldur.services.cell_topology.policy.get_cell_evacuation_policy",
            ) as mock_get_policy,
        ):
            agg.aggregate_all()

        mock_get_policy.assert_not_called()

    def test_aggregate_all_evacuation_failure_does_not_affect_health_collection(self):
        """대피 정책 평가 실패가 건강도 수집 루프에 영향을 주지 않는다."""
        settings = CellTopologySettings(
            enabled=True,
            evacuation_enabled=True,
            metrics_enabled=False,
        )
        agg = CellHealthAggregator(settings=settings)
        agg._prometheus_consecutive_failures = _PROMETHEUS_MAX_CONSECUTIVE_FAILURES
        agg._last_prometheus_failure_time = time.monotonic()

        mock_registry = MagicMock()
        mock_registry.get_all_cells.return_value = {
            "cell-0": MagicMock(health_score=0.5),
        }

        with (
            patch(
                "baldur.services.cell_topology.get_cell_registry",
                return_value=mock_registry,
            ),
            patch(
                "baldur.services.cell_topology.policy.get_cell_evacuation_policy",
                side_effect=RuntimeError("policy error"),
            ),
        ):
            # 예외 없이 처리되어야 한다
            agg.aggregate_all()

        # 건강도 갱신은 정상적으로 호출되어야 한다
        mock_registry.update_health_score.assert_called_once()


class TestSingletonBehavior:
    """싱글톤 동작 검증."""

    def test_get_returns_same_instance(self):
        """get_cell_health_aggregator()는 동일 인스턴스를 반환해야 한다."""
        a1 = get_cell_health_aggregator()
        a2 = get_cell_health_aggregator()
        assert a1 is a2

    def test_reset_creates_new_instance(self):
        """reset 후 새 인스턴스가 생성되어야 한다."""
        # metrics_enabled=False로 설정하여 Prometheus 중복 등록 방지
        with patch(
            "baldur.settings.cell_topology.get_cell_topology_settings",
            return_value=CellTopologySettings(enabled=True, metrics_enabled=False),
        ):
            a1 = get_cell_health_aggregator()
            reset_cell_health_aggregator()
            a2 = get_cell_health_aggregator()
            assert a1 is not a2


class TestMetricsInitBehavior:
    """Prometheus 메트릭 초기화 동작 검증."""

    def test_metrics_disabled_returns_none(self, settings: CellTopologySettings):
        """metrics_enabled=False이면 메트릭이 None이어야 한다."""
        settings_off = CellTopologySettings(enabled=True, metrics_enabled=False)
        agg = CellHealthAggregator(settings=settings_off)
        assert agg._metrics is None

    def test_prometheus_import_failure_returns_none(self):
        """prometheus_client 미설치 시 메트릭이 None이어야 한다."""
        settings_on = CellTopologySettings(enabled=True, metrics_enabled=True)
        with patch.dict("sys.modules", {"prometheus_client": None}):
            agg = CellHealthAggregator(settings=settings_on)
            assert agg._metrics is None


class TestRecordRequestWithMetricsBehavior:
    """Prometheus 메트릭 활성화 시 record_request() 동작 검증."""

    def test_record_request_increments_counter(self):
        """메트릭 활성화 시 Counter가 increment 되어야 한다."""
        mock_counter = MagicMock()
        mock_gauge = MagicMock()
        mock_histogram = MagicMock()

        settings = CellTopologySettings(enabled=True, metrics_enabled=False)
        agg = CellHealthAggregator(settings=settings)
        agg._metrics = {
            "request_total": mock_counter,
            "request_duration": mock_histogram,
            "health_score": mock_gauge,
            "health_score_raw": mock_gauge,
            "bulkhead_utilization": mock_gauge,
            "health_data_source": mock_gauge,
        }

        agg.record_request("cell-0", success=True, latency=0.1)

        mock_counter.labels.assert_called_with(cell_id="cell-0", status="success")
        mock_counter.labels().inc.assert_called_once()
        mock_histogram.labels.assert_called_with(cell_id="cell-0")
        mock_histogram.labels().observe.assert_called_once_with(0.1)
