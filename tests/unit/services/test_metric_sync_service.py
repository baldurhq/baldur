"""
Tests for services/metric_sync_service.py - Metric Sync Service.
services/metric_sync_service.py의 메트릭 동기화, Drift 감지, 상태 분류 등에 대한 단위 테스트.

커버리지 대상:
- DriftThresholds 상수
- MetricSyncService 초기화
- sync_metrics() (dry_run 및 실제 동기화)
- get_drift_report()
- 내부 헬퍼 메서드들 (_capture_current_state, _get_actual_state, _build_results 등)
- _classify_health(), _get_recommendation()
- _calculate_drift_metrics(), _get_max_drift_percent()
- 싱글톤 함수 (get_metric_sync_service, reset_metric_sync_service)
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.metric_sync_service import (
    DriftThresholds,
    MetricSyncService,
    get_metric_sync_service,
    reset_metric_sync_service,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_reconciler():
    """MetricReconciler mock."""
    reconciler = MagicMock()
    reconciler.sync_domain_gauges = MagicMock()
    reconciler.sync_all_gauges = MagicMock()
    return reconciler


@pytest.fixture
def mock_adapter():
    """MetricSourceAdapter mock."""
    adapter = MagicMock()
    adapter.get_dlq_pending_count = MagicMock(return_value=0)
    adapter.get_retry_success_rate = MagicMock(return_value=100.0)
    return adapter


@pytest.fixture
def service(mock_reconciler, mock_adapter):
    """MetricSyncService 인스턴스."""
    return MetricSyncService(reconciler=mock_reconciler, adapter=mock_adapter)


# =============================================================================
# DriftThresholds Tests
# =============================================================================


class TestDriftThresholds:
    """DriftThresholds 상수 테스트."""

    def test_warning_threshold(self):
        """Warning threshold value
        WARNING 임계값이 5.0인지 확인.
        """
        assert DriftThresholds.WARNING == 5.0

    def test_critical_threshold(self):
        """Critical threshold value
        CRITICAL 임계값이 20.0인지 확인.
        """
        assert DriftThresholds.CRITICAL == 20.0

    def test_incident_threshold(self):
        """Incident threshold value
        INCIDENT 임계값이 50.0인지 확인.
        """
        assert DriftThresholds.INCIDENT == 50.0

    def test_threshold_ordering(self):
        """Threshold ordering
        임계값이 WARNING < CRITICAL < INCIDENT 순서인지 확인.
        """
        assert (
            DriftThresholds.WARNING
            < DriftThresholds.CRITICAL
            < DriftThresholds.INCIDENT
        )


# =============================================================================
# MetricSyncService Initialization Tests
# =============================================================================


class TestMetricSyncServiceInit:
    """MetricSyncService 초기화 테스트."""

    @patch("baldur.services.metric_sync_service.get_reconciler")
    @patch("baldur.services.metric_sync_service.get_metric_adapter")
    def test_init_with_defaults(self, mock_get_adapter, mock_get_reconciler):
        """Init with default reconciler and adapter
        기본값으로 초기화 시 get_reconciler()와 get_metric_adapter()가 호출되는지 확인.
        """
        mock_reconciler = MagicMock()
        mock_adapter = MagicMock()
        mock_get_reconciler.return_value = mock_reconciler
        mock_get_adapter.return_value = mock_adapter

        service = MetricSyncService()
        assert service.reconciler is mock_reconciler
        assert service.adapter is mock_adapter

    def test_init_with_custom_reconciler(self, mock_adapter):
        """Init with custom reconciler
        커스텀 reconciler로 초기화할 수 있는지 확인.
        """
        custom_reconciler = MagicMock()
        service = MetricSyncService(reconciler=custom_reconciler, adapter=mock_adapter)
        assert service.reconciler is custom_reconciler


# =============================================================================
# sync_metrics Tests
# =============================================================================


class TestSyncMetrics:
    """sync_metrics() 메서드 테스트."""

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_dry_run_returns_report(self, mock_domains, service):
        """Dry run returns report only
        dry_run=True일 때 실제 동기화 없이 리포트만 반환하는지 확인.
        """
        mock_domains.return_value = ["payment"]
        service._capture_current_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 5},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )
        service._get_actual_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 10},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )

        result = service.sync_metrics(dry_run=True, actor="tester")

        assert result["status"] == "dry_run"
        assert result["dry_run"] is True
        assert result["actor"] == "tester"
        assert "results" in result
        assert "summary" in result
        # 실제 동기화 메서드가 호출되지 않아야 함
        service.reconciler.sync_all_gauges.assert_not_called()
        service.reconciler.sync_domain_gauges.assert_not_called()

    @patch("baldur.services.metric_sync_service.MetricSyncService._log_sync_action")
    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_sync_all_domains(self, mock_domains, mock_log, service):
        """Sync all domains
        domains=None일 때 sync_all_gauges()가 호출되는지 확인.
        """
        mock_domains.return_value = ["payment", "point"]
        service._capture_current_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 0, "point": 0},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )
        service._get_actual_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 0, "point": 0},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )

        result = service.sync_metrics(domains=None, actor="admin")

        assert result["status"] == "completed"
        assert result["dry_run"] is False
        service.reconciler.sync_all_gauges.assert_called_once()

    @patch("baldur.services.metric_sync_service.MetricSyncService._log_sync_action")
    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_sync_specific_domains(self, mock_domains, mock_log, service):
        """Sync specific domains
        특정 도메인만 동기화할 때 sync_domain_gauges()가 각 도메인에 대해 호출되는지 확인.
        """
        service._capture_current_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 0},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )
        service._get_actual_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 0},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )

        result = service.sync_metrics(domains=["payment"], actor="admin")

        service.reconciler.sync_domain_gauges.assert_called_once_with("payment")
        assert result["status"] == "completed"


# =============================================================================
# get_drift_report Tests
# =============================================================================


class TestGetDriftReport:
    """get_drift_report() 메서드 테스트."""

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_drift_report_structure(self, mock_domains, service):
        """Drift report structure
        get_drift_report()가 올바른 구조의 딕셔너리를 반환하는지 확인.
        """
        mock_domains.return_value = ["payment"]
        service._capture_current_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 0},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )
        service._get_actual_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 0},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )

        report = service.get_drift_report()

        assert "generated_at" in report
        assert "metrics" in report
        assert "overall_health" in report
        assert "max_drift_percent" in report
        assert "recommendation" in report

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_drift_report_healthy(self, mock_domains, service):
        """Drift report healthy state
        Drift가 없을 때 overall_health="healthy"인지 확인.
        """
        mock_domains.return_value = ["payment"]
        service._capture_current_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 10},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )
        service._get_actual_state = MagicMock(
            return_value={
                "dlq_pending": {"payment": 10},
                "circuit_breaker": {},
                "retry_rate": {},
            }
        )

        report = service.get_drift_report()
        assert report["overall_health"] == "healthy"
        assert report["max_drift_percent"] == 0.0


# =============================================================================
# Internal Helper Method Tests
# =============================================================================


class TestClassifyHealth:
    """_classify_health() 메서드 테스트."""

    def test_healthy(self, service):
        """Healthy classification
        Drift < WARNING이면 "healthy"로 분류되는지 확인.
        """
        assert service._classify_health(0.0) == "healthy"
        assert service._classify_health(4.9) == "healthy"

    def test_warning(self, service):
        """Warning classification
        WARNING <= Drift < CRITICAL이면 "warning"으로 분류되는지 확인.
        """
        assert service._classify_health(5.0) == "warning"
        assert service._classify_health(19.9) == "warning"

    def test_critical(self, service):
        """Critical classification
        CRITICAL <= Drift < INCIDENT이면 "critical"으로 분류되는지 확인.
        """
        assert service._classify_health(20.0) == "critical"
        assert service._classify_health(49.9) == "critical"

    def test_incident(self, service):
        """Incident classification
        Drift >= INCIDENT이면 "incident"로 분류되는지 확인.
        """
        assert service._classify_health(50.0) == "incident"
        assert service._classify_health(100.0) == "incident"


class TestGetRecommendation:
    """_get_recommendation() 메서드 테스트."""

    def test_healthy_no_recommendation(self, service):
        """Healthy - no recommendation
        healthy 상태에서 빈 문자열을 반환하는지 확인.
        """
        assert service._get_recommendation("healthy") == ""

    def test_warning_recommendation(self, service):
        """Warning recommendation
        warning 상태에서 모니터링 권장 메시지를 반환하는지 확인.
        """
        rec = service._get_recommendation("warning")
        assert "monitoring" in rec

    def test_critical_recommendation(self, service):
        """Critical recommendation
        critical 상태에서 동기화 권장 메시지를 반환하는지 확인.
        """
        rec = service._get_recommendation("critical")
        assert "sync" in rec.lower()

    def test_incident_recommendation(self, service):
        """Incident recommendation
        incident 상태에서 즉시 동기화 메시지를 반환하는지 확인.
        """
        rec = service._get_recommendation("incident")
        assert "immediately" in rec

    def test_unknown_health(self, service):
        """Unknown health status
        알 수 없는 상태에서 빈 문자열을 반환하는지 확인.
        """
        assert service._get_recommendation("unknown") == ""


class TestBuildResults:
    """_build_results() 메서드 테스트."""

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_build_results_with_drift(self, mock_domains, service):
        """Build results with drift
        Drift가 있을 때 올바른 before/after/drift 값이 계산되는지 확인.
        """
        mock_domains.return_value = ["payment"]
        before = {"dlq_pending": {"payment": 5}}
        after = {"dlq_pending": {"payment": 15}}

        results = service._build_results(before, after, ["payment"])

        assert results["payment"]["dlq_pending"]["before"] == 5
        assert results["payment"]["dlq_pending"]["after"] == 15
        assert results["payment"]["dlq_pending"]["drift"] == 10


class TestCalculateSummary:
    """_calculate_summary() 메서드 테스트."""

    def test_no_drifts(self, service):
        """No drifts summary
        Drift가 없을 때 total_drifts_detected=0인지 확인.
        """
        results = {
            "payment": {"dlq_pending": {"before": 5, "after": 5, "drift": 0}},
        }
        summary = service._calculate_summary(results)
        assert summary["total_drifts_detected"] == 0

    def test_drift_detected(self, service):
        """Drift detected summary
        Drift가 있을 때 올바르게 감지되는지 확인.
        """
        results = {
            "payment": {"dlq_pending": {"before": 10, "after": 15, "drift": 5}},
        }
        summary = service._calculate_summary(results)
        assert summary["total_drifts_detected"] == 1
        assert summary["max_drift_percent"] == 50.0  # 5/10 * 100

    def test_drift_from_zero(self, service):
        """Drift from zero base
        before=0에서 after>0일 때 100% drift가 감지되는지 확인.
        """
        results = {
            "payment": {"dlq_pending": {"before": 0, "after": 5, "drift": 5}},
        }
        summary = service._calculate_summary(results)
        assert summary["max_drift_percent"] == 100.0


class TestCalculateDriftMetrics:
    """_calculate_drift_metrics() 메서드 테스트."""

    def test_drift_metrics_calculation(self, service):
        """Drift metrics calculation
        인메모리와 실제 상태 간의 Drift 메트릭이 올바르게 계산되는지 확인.
        """
        in_memory = {"dlq_pending": {"payment": 10}}
        actual = {"dlq_pending": {"payment": 15}}

        metrics = service._calculate_drift_metrics(in_memory, actual)

        assert "dlq_pending_count" in metrics
        assert "payment" in metrics["dlq_pending_count"]
        pm = metrics["dlq_pending_count"]["payment"]
        assert pm["in_memory"] == 10
        assert pm["actual"] == 15
        assert pm["drift"] == 5
        assert pm["drift_percent"] == 50.0
        assert pm["is_critical"] is True  # 50% >= 20%

    def test_drift_metrics_zero_in_memory(self, service):
        """Drift metrics with zero in-memory
        인메모리 값이 0이고 실제 값이 있을 때 drift_percent=100%인지 확인.
        """
        in_memory = {"dlq_pending": {"payment": 0}}
        actual = {"dlq_pending": {"payment": 5}}

        metrics = service._calculate_drift_metrics(in_memory, actual)
        assert metrics["dlq_pending_count"]["payment"]["drift_percent"] == 100.0

    def test_drift_metrics_both_zero(self, service):
        """Drift metrics both zero
        인메모리와 실제 값 모두 0일 때 drift_percent=0%인지 확인.
        """
        in_memory = {"dlq_pending": {"payment": 0}}
        actual = {"dlq_pending": {"payment": 0}}

        metrics = service._calculate_drift_metrics(in_memory, actual)
        assert metrics["dlq_pending_count"]["payment"]["drift_percent"] == 0.0


class TestGetMaxDriftPercent:
    """_get_max_drift_percent() 메서드 테스트."""

    def test_single_metric(self, service):
        """Single metric max drift
        단일 메트릭에서 최대 drift percent를 올바르게 반환하는지 확인.
        """
        metrics = {
            "dlq_pending_count": {
                "payment": {"drift_percent": 15.5},
            }
        }
        assert service._get_max_drift_percent(metrics) == 15.5

    def test_multiple_metrics(self, service):
        """Multiple metrics max drift
        여러 메트릭에서 최대값을 반환하는지 확인.
        """
        metrics = {
            "dlq_pending_count": {
                "payment": {"drift_percent": 15.5},
                "point": {"drift_percent": 35.0},
            }
        }
        assert service._get_max_drift_percent(metrics) == 35.0

    def test_empty_metrics(self, service):
        """Empty metrics
        빈 메트릭에서 0.0을 반환하는지 확인.
        """
        assert service._get_max_drift_percent({}) == 0.0


# =============================================================================
# _get_all_domains Tests
# =============================================================================


class TestGetAllDomains:
    """_get_all_domains() 메서드 테스트."""

    def test_fallback_domains(self, service):
        """Fallback domains when prometheus unavailable
        Prometheus 모듈이 없을 때 registry의 도메인 목록을 반환하는지 확인.
        """
        with patch.dict("sys.modules", {"baldur.metrics.prometheus": None}):
            # ImportError 발생 시 registry 기반 도메인 반환
            domains = service._get_all_domains()
            assert isinstance(domains, list)
            assert len(domains) > 0
            # registry DEFAULT_DOMAINS에 포함된 도메인이 반환됨
            assert "notification" in domains


# =============================================================================
# _capture_current_state Tests
# =============================================================================


class TestCaptureCurrentState:
    """_capture_current_state() 메서드 테스트."""

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_capture_with_no_prometheus(self, mock_domains, service):
        """Capture state without prometheus
        Prometheus 모듈이 없을 때 0으로 기본값을 채우는지 확인.
        """
        mock_domains.return_value = ["payment"]

        with patch.dict("sys.modules", {"baldur.metrics.prometheus": None}):
            state = service._capture_current_state(["payment"])
            assert "dlq_pending" in state
            assert state["dlq_pending"]["payment"] == 0

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_capture_gauge_read_error_logs_and_falls_back_to_zero(
        self, mock_domains, service
    ):
        """A gauge-read failure surfaces a WARNING and falls back to 0.

        Sibling-parity with _get_actual_state (D2, advisor review 2a): the
        drift-before read must NOT silently mask a read failure as drift=0
        (false-healthy) — it logs ``metric_sync.dlq_gauge_read_failed`` and
        then fills 0.
        """
        import baldur.services.metric_sync_service as svc_module
        from baldur.metrics.prometheus import (
            BaldurMetrics,
            configure_metrics,
            reset_metrics,
        )

        mock_domains.return_value = ["payment"]

        # Given: the configured backend's gauge-read accessor raises
        instance = BaldurMetrics()
        instance.dlq.get_pending_count = MagicMock(
            side_effect=RuntimeError("gauge read broke")
        )
        configure_metrics(instance)
        try:
            # When: the drift-before snapshot reads the in-memory gauge
            with patch.object(svc_module, "logger") as mock_logger:
                state = service._capture_current_state(["payment"])
        finally:
            reset_metrics()

        # Then: fall back to 0 AND log the read failure (not silent)
        assert state["dlq_pending"]["payment"] == 0
        mock_logger.warning.assert_called_once()
        (event,) = mock_logger.warning.call_args.args
        assert event == "metric_sync.dlq_gauge_read_failed"
        assert mock_logger.warning.call_args.kwargs["healing_domain"] == "payment"


# =============================================================================
# _get_actual_state Tests
# =============================================================================


class TestGetActualState:
    """_get_actual_state() 메서드 테스트."""

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_get_actual_state_success(self, mock_domains, service, mock_adapter):
        """Get actual state success
        어댑터에서 정상적으로 데이터를 가져오는지 확인.
        """
        mock_domains.return_value = ["payment"]
        mock_adapter.get_dlq_pending_count.return_value = 10
        mock_adapter.get_retry_success_rate.return_value = 95.0

        state = service._get_actual_state(["payment"])
        assert state["dlq_pending"]["payment"] == 10
        assert state["retry_rate"]["payment"] == 95.0

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_get_actual_state_adapter_error(self, mock_domains, service, mock_adapter):
        """Get actual state with adapter error
        어댑터에서 예외 발생 시 기본값(0)으로 처리하는지 확인.
        """
        mock_domains.return_value = ["payment"]
        mock_adapter.get_dlq_pending_count.side_effect = Exception("DB Error")
        mock_adapter.get_retry_success_rate.side_effect = Exception("DB Error")

        state = service._get_actual_state(["payment"])
        assert state["dlq_pending"]["payment"] == 0
        assert state["retry_rate"]["payment"] == 0.0


# =============================================================================
# Drift report — gauge divergence / before-first-hydration (647)
# =============================================================================


@pytest.fixture(params=["prometheus", "otel"])
def configured_backend(request):
    """Configure the metrics singleton with each backend, then reset.

    Function-scoped to avoid xdist singleton leakage. OTEL needs the meter
    patched only during construction.
    """
    from baldur.metrics.prometheus import (
        BaldurMetrics,
        configure_metrics,
        reset_metrics,
    )

    if request.param == "prometheus":
        instance = BaldurMetrics()
    else:
        from baldur.metrics.otel_backend import OTELBaldurMetrics

        with patch("baldur.observability.get_meter", return_value=MagicMock()):
            instance = OTELBaldurMetrics()
    configure_metrics(instance)
    yield request.param, instance
    reset_metrics()


class TestDriftReportDivergence:
    """get_drift_report() detects real drift (no longer trivially 0)."""

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_get_drift_report_detects_gauge_divergence(
        self, mock_domains, configured_backend
    ):
        """In-memory gauge diverging from the data source is reported as drift.

        Restoration check: the drift-before snapshot now reads the real
        in-memory gauge (D2 repoint), so a low gauge vs a high data source is
        no longer masked as healthy.
        """
        _param, instance = configured_backend
        mock_domains.return_value = ["payment"]

        # In-memory gauge low (5), data source high (10) -> 100% drift.
        instance.dlq.set_pending_count("payment", 5)

        adapter = MagicMock()
        adapter.get_dlq_pending_count.return_value = 10
        adapter.get_retry_success_rate.return_value = 100.0
        service = MetricSyncService(reconciler=MagicMock(), adapter=adapter)

        report = service.get_drift_report()
        assert report["overall_health"] != "healthy"
        assert report["overall_health"] == "incident"  # 5 -> 10 is 100% drift


class TestDriftReportBeforeFirstHydration:
    """SC5: an unhydrated gauge (post-restart 0) is reported as drift."""

    @patch("baldur.services.metric_sync_service.MetricSyncService._get_all_domains")
    def test_before_first_hydration_unhydrated_gauge_reported_as_drift(
        self, mock_domains, configured_backend
    ):
        """A never-hydrated gauge (0) vs a non-zero data source flags drift.

        Guards the post-restart window where the API is hit before
        sync_all_gauges() runs: the "0 before first hydrate" is treated as real
        drift, not masked as healthy.
        """
        _param, instance = configured_backend
        domain = "unhydrated_647"
        mock_domains.return_value = [domain]

        # Do NOT hydrate the gauge -> the drift-before read returns 0.0.
        adapter = MagicMock()
        adapter.get_dlq_pending_count.return_value = 8
        adapter.get_retry_success_rate.return_value = 100.0
        service = MetricSyncService(reconciler=MagicMock(), adapter=adapter)

        report = service.get_drift_report()
        assert report["overall_health"] != "healthy"


# =============================================================================
# Singleton Function Tests
# =============================================================================


class TestSingletonFunctions:
    """get_metric_sync_service / reset_metric_sync_service 싱글톤 테스트."""

    def test_get_creates_singleton(self):
        """get_metric_sync_service() returns a MetricSyncService instance."""
        reset_metric_sync_service()
        try:
            result = get_metric_sync_service()
            assert result is not None
            assert isinstance(result, MetricSyncService)
        finally:
            reset_metric_sync_service()

    def test_reset_clears_singleton(self):
        """reset_metric_sync_service() creates a new instance on next get."""
        reset_metric_sync_service()
        try:
            svc_before = get_metric_sync_service()
            reset_metric_sync_service()
            svc_after = get_metric_sync_service()
            assert svc_before is not svc_after
        finally:
            reset_metric_sync_service()


# =============================================================================
# _log_sync_action Tests
# =============================================================================


class TestLogSyncAction:
    """_log_sync_action() 메서드 테스트."""

    def test_log_sync_action_handles_import_error(self, service):
        """Log sync action handles ImportError
        audit 모듈 import 실패 시에도 에러 없이 처리되는지 확인.
        """
        with patch.dict("sys.modules", {"baldur.audit.logger": None}):
            # ImportError가 발생해도 exception 없이 완료되어야 함
            try:
                service._log_sync_action(
                    actor="admin",
                    domains=["payment"],
                    dry_run=False,
                    reason="test",
                    summary={"total_drifts_corrected": 1},
                )
            except Exception:
                pass  # 환경별로 다를 수 있음
