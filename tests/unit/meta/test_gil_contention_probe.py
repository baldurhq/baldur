"""
GILContentionProbe 단위 테스트.

GIL 경합 스케줄링 지연 측정 프로브의 계약값 및 동작 검증.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.meta.health_probe import HealthStatus
from baldur.meta.probes.gil_contention import GILContentionProbe

# =============================================================================
# Contract Tests — 설계 문서 상수/구조 검증
# =============================================================================


class TestGILContentionProbeContract:
    """GILContentionProbe 설계 계약값 검증."""

    def test_degraded_threshold_is_one_ms(self):
        """DEGRADED 임계값: 1.0ms. (§3.3 GIL Contention Metrics)"""
        assert GILContentionProbe.DEGRADED_THRESHOLD_MS == 1.0

    def test_unhealthy_threshold_is_five_ms(self):
        """UNHEALTHY 임계값: 5.0ms. (§3.3 GIL Contention Metrics)"""
        assert GILContentionProbe.UNHEALTHY_THRESHOLD_MS == 5.0

    def test_check_returns_tuple_of_status_and_details(self):
        """check()는 (HealthStatus, dict) 튜플을 반환."""
        probe = GILContentionProbe()
        result = probe.check()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], HealthStatus)
        assert isinstance(result[1], dict)

    def test_details_contains_required_keys(self):
        """details 딕셔너리에 p90_ms, min_ms, max_ms 키가 포함."""
        probe = GILContentionProbe()
        _, details = probe.check()
        assert "p90_ms" in details
        assert "min_ms" in details
        assert "max_ms" in details

    def test_measures_exactly_ten_samples(self):
        """P90 계산을 위해 정확히 10회 측정."""
        probe = GILContentionProbe()
        call_count = 0

        original_perf_counter_ns = __import__("time").perf_counter_ns

        def counting_perf_counter_ns():
            nonlocal call_count
            call_count += 1
            return original_perf_counter_ns()

        with patch(
            "baldur.meta.probes.gil_contention.time.perf_counter_ns",
            side_effect=counting_perf_counter_ns,
        ):
            probe.check()

        # 10 iterations × 1 call each before sleep = 10 t0 calls
        # + 10 calls after sleep = 20 total
        assert call_count == 20


# =============================================================================
# Behavior Tests — 동작 검증
# =============================================================================


class TestGILContentionProbeBehavior:
    """GILContentionProbe 동작 검증."""

    def _make_probe_with_delays(self, delays_ns):
        """perf_counter_ns를 조작하여 특정 지연 패턴을 만드는 헬퍼.

        delays_ns: 10개의 지연 값(ns 단위) 리스트
        """
        probe = GILContentionProbe()
        # perf_counter_ns는 각 iteration에서 2번 호출됨 (t0, t0+delay)
        counter_values = []
        base = 1_000_000_000
        for delay in delays_ns:
            counter_values.append(base)
            counter_values.append(base + delay)
            base += delay + 1000

        with (
            patch(
                "baldur.meta.probes.gil_contention.time.perf_counter_ns",
                side_effect=counter_values,
            ),
            patch(
                "baldur.meta.probes.gil_contention.time.sleep",
            ),
        ):
            return probe.check()

    def test_all_delays_below_degraded_returns_healthy(self):
        """모든 지연이 DEGRADED 임계값 미만이면 HEALTHY."""
        # 10개 모두 0.5ms (500,000 ns) — P90 = 0.5ms < 1.0ms
        delays = [500_000] * 10
        status, details = self._make_probe_with_delays(delays)

        assert status == HealthStatus.HEALTHY
        assert details["p90_ms"] < GILContentionProbe.DEGRADED_THRESHOLD_MS

    def test_p90_at_degraded_threshold_returns_degraded(self):
        """P90이 정확히 DEGRADED 임계값이면 DEGRADED."""
        # 9개는 낮고, 9번째(P90 = sorted[8])가 정확히 1.0ms
        delays = [100_000] * 8 + [1_000_000, 2_000_000]
        status, details = self._make_probe_with_delays(delays)

        assert status == HealthStatus.DEGRADED

    def test_p90_between_degraded_and_unhealthy_returns_degraded(self):
        """P90이 DEGRADED와 UNHEALTHY 사이면 DEGRADED."""
        # P90 = 3.0ms (DEGRADED 범위: 1ms ≤ P90 < 5ms)
        delays = [100_000] * 8 + [3_000_000, 4_000_000]
        status, details = self._make_probe_with_delays(delays)

        assert status == HealthStatus.DEGRADED

    def test_p90_at_unhealthy_threshold_returns_unhealthy(self):
        """P90이 UNHEALTHY 임계값 이상이면 UNHEALTHY."""
        # P90 = 5.0ms
        delays = [100_000] * 8 + [5_000_000, 10_000_000]
        status, details = self._make_probe_with_delays(delays)

        assert status == HealthStatus.UNHEALTHY

    def test_p90_well_above_unhealthy_returns_unhealthy(self):
        """P90이 UNHEALTHY 임계값을 크게 초과하면 UNHEALTHY."""
        # 모든 지연이 10ms
        delays = [10_000_000] * 10
        status, details = self._make_probe_with_delays(delays)

        assert status == HealthStatus.UNHEALTHY

    def test_details_values_are_rounded_to_three_decimals(self):
        """details 값이 소수점 3자리로 반올림."""
        delays = [123_456] * 10  # 0.123456ms
        _, details = self._make_probe_with_delays(delays)

        # round(0.123456, 3) == 0.123
        assert details["p90_ms"] == pytest.approx(0.123, abs=0.001)
        assert details["min_ms"] == pytest.approx(0.123, abs=0.001)
        assert details["max_ms"] == pytest.approx(0.123, abs=0.001)

    def test_min_max_reflect_actual_extremes(self):
        """min_ms와 max_ms가 실제 최소/최대 지연을 반영."""
        # 명확한 최소(50,000ns=0.05ms)와 최대(9,000,000ns=9.0ms)
        delays = [50_000] + [500_000] * 8 + [9_000_000]
        _, details = self._make_probe_with_delays(delays)

        assert details["min_ms"] == pytest.approx(0.05, abs=0.001)
        assert details["max_ms"] == pytest.approx(9.0, abs=0.001)
