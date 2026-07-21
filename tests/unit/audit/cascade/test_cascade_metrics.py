"""
Cascade Event Metrics 단위 테스트.

Phase 9: Prometheus 메트릭 테스트.

Tests:
- record_cascade_event: Cascade Event 기록 메트릭
- record_effect: 연쇄 효과 메트릭
- record_chain_depth: 체인 깊이 메트릭
- record_integrity_check: 무결성 검증 결과 메트릭
- record_load_shedding_drop: Load Shedding 드랍 메트릭
- to_prometheus_format: Prometheus 형식 출력
"""

from __future__ import annotations

# =============================================================================
# CascadeMetrics Record Tests
# =============================================================================


class TestCascadeMetricsRecord:
    """CascadeMetrics 기록 메서드 테스트."""

    def setup_method(self):
        """각 테스트 전 싱글턴 초기화."""
        from baldur.audit.cascade_metrics import CascadeMetrics

        CascadeMetrics.reset_instance()

    def test_record_cascade_event(self):
        """Cascade Event 기록 메트릭."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_cascade_event("seoul", "EMERGENCY_LEVEL_CHANGED")
        metrics.record_cascade_event("seoul", "EMERGENCY_LEVEL_CHANGED")
        metrics.record_cascade_event("seoul", "MANUAL_ACTIVATION")
        metrics.record_cascade_event("tokyo", "EMERGENCY_LEVEL_CHANGED")

        result = metrics.get_cascade_events_total()

        assert result["seoul"]["EMERGENCY_LEVEL_CHANGED"] == 2
        assert result["seoul"]["MANUAL_ACTIVATION"] == 1
        assert result["tokyo"]["EMERGENCY_LEVEL_CHANGED"] == 1

    def test_record_effect(self):
        """연쇄 효과 기록 메트릭."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_effect("seoul", "governance_strict", success=True)
        metrics.record_effect("seoul", "governance_strict", success=True)
        metrics.record_effect("seoul", "governance_strict", success=False)
        metrics.record_effect("seoul", "canary_rollback", success=True)

        result = metrics.get_effects_total()

        assert result["seoul"]["governance_strict"]["success"] == 2
        assert result["seoul"]["governance_strict"]["failure"] == 1
        assert result["seoul"]["canary_rollback"]["success"] == 1

    def test_record_chain_depth_max_update(self):
        """체인 깊이 최대값 갱신 테스트."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_chain_depth("seoul", 3)
        metrics.record_chain_depth("seoul", 5)
        metrics.record_chain_depth("seoul", 2)  # 최대값보다 작음

        result = metrics.get_chain_depth_max()

        assert result["seoul"] == 5  # 최대값 유지

    def test_record_integrity_check(self):
        """무결성 검증 결과 메트릭."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_integrity_check("seoul", is_valid=True)
        metrics.record_integrity_check("tokyo", is_valid=False)

        result = metrics.get_integrity_status()

        assert result["seoul"] == 1
        assert result["tokyo"] == 0

    def test_record_load_shedding_drop(self):
        """Load Shedding 드랍 메트릭."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_load_shedding_drop("LOW")
        metrics.record_load_shedding_drop("LOW")
        metrics.record_load_shedding_drop("MEDIUM")

        result = metrics.get_load_shedding_dropped()

        assert result["LOW"] == 2
        assert result["MEDIUM"] == 1

    def test_record_fallback_write(self):
        """로컬 폴백 저장 메트릭."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_fallback_write()
        metrics.record_fallback_write()
        metrics.record_fallback_write()

        assert metrics.get_fallback_writes_total() == 3


# =============================================================================
# CascadeMetrics Query Tests
# =============================================================================


class TestCascadeMetricsQuery:
    """CascadeMetrics 조회 메서드 테스트."""

    def setup_method(self):
        """각 테스트 전 싱글턴 초기화."""
        from baldur.audit.cascade_metrics import CascadeMetrics

        CascadeMetrics.reset_instance()

    def test_get_all_metrics(self):
        """전체 메트릭 조회."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_cascade_event("seoul", "TEST")
        metrics.record_effect("seoul", "test_action", success=True)
        metrics.record_chain_depth("seoul", 3)
        metrics.record_integrity_check("seoul", is_valid=True)
        metrics.record_load_shedding_drop("LOW")
        metrics.record_fallback_write()

        result = metrics.get_all_metrics()

        assert "cascade_events_total" in result
        assert "cascade_effects_total" in result
        assert "chain_depth_max" in result
        assert "integrity_valid" in result
        assert "load_shedding_dropped_total" in result
        assert "fallback_writes_total" in result
        assert "last_updated" in result

    def test_singleton_instance(self):
        """싱글턴 패턴 테스트."""
        from baldur.audit.cascade_metrics import (
            CascadeMetrics,
            get_cascade_metrics,
        )

        metrics1 = get_cascade_metrics()
        metrics2 = get_cascade_metrics()
        metrics3 = CascadeMetrics.get_instance()

        assert metrics1 is metrics2
        assert metrics2 is metrics3


# =============================================================================
# CascadeMetrics Prometheus Format Tests
# =============================================================================


class TestCascadeMetricsPrometheusFormat:
    """Prometheus 형식 출력 테스트."""

    def setup_method(self):
        """각 테스트 전 싱글턴 초기화."""
        from baldur.audit.cascade_metrics import CascadeMetrics

        CascadeMetrics.reset_instance()

    def test_to_prometheus_format_empty(self):
        """빈 메트릭 Prometheus 형식."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()
        output = metrics.to_prometheus_format()

        assert "# HELP baldur_cascade_events_total" in output
        assert "# TYPE baldur_cascade_events_total counter" in output
        assert "baldur_cascade_fallback_writes_total 0" in output

    def test_to_prometheus_format_with_data(self):
        """데이터 있는 Prometheus 형식."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_cascade_event("seoul", "EMERGENCY_LEVEL_CHANGED")
        metrics.record_effect("seoul", "governance_strict", success=True)
        metrics.record_chain_depth("seoul", 5)
        metrics.record_integrity_check("seoul", is_valid=True)
        metrics.record_load_shedding_drop("LOW")
        metrics.record_fallback_write()

        output = metrics.to_prometheus_format()

        # Cascade Events
        assert (
            'baldur_cascade_events_total{namespace="seoul",trigger_type="EMERGENCY_LEVEL_CHANGED"} 1'
            in output
        )

        # Effects
        assert (
            'baldur_cascade_effects_total{namespace="seoul",action_type="governance_strict",status="success"} 1'
            in output
        )

        # Chain Depth
        assert 'baldur_cascade_chain_depth_max{namespace="seoul"} 5' in output

        # Integrity
        assert 'baldur_cascade_integrity_valid{namespace="seoul"} 1' in output

        # Load Shedding
        assert 'baldur_cascade_load_shedding_dropped_total{priority="LOW"} 1' in output

        # Fallback
        assert "baldur_cascade_fallback_writes_total 1" in output

    def test_prometheus_format_multiple_namespaces(self):
        """여러 네임스페이스 Prometheus 형식."""
        from baldur.audit.cascade_metrics import get_cascade_metrics

        metrics = get_cascade_metrics()

        metrics.record_cascade_event("seoul", "TEST")
        metrics.record_cascade_event("tokyo", "TEST")

        output = metrics.to_prometheus_format()

        assert 'namespace="seoul"' in output
        assert 'namespace="tokyo"' in output
