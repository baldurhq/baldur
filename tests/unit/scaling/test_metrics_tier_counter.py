"""
BackpressureMetrics Tier별 Drop 카운터 단위 테스트.

테스트 항목:
- 동작: inc_dropped_by_tier() 카운터 증가
- 동작: dropped_by_tier_total에 tier 라벨 포함
- 동작: 메트릭 비활성화 시 no-op
"""

import pytest

from baldur.scaling.config import BackpressureSettings, reset_backpressure_settings
from baldur.scaling.metrics import HAS_PROMETHEUS, BackpressureMetrics

if HAS_PROMETHEUS:
    from prometheus_client import REGISTRY


def _clean_prometheus_registry():
    """Prometheus 레지스트리에서 baldur_ 메트릭 정리."""
    if not HAS_PROMETHEUS:
        return
    collectors_to_remove = []
    for collector in list(REGISTRY._collector_to_names.keys()):
        names = REGISTRY._collector_to_names.get(collector, [])
        if any(name.startswith("baldur_") for name in names):
            collectors_to_remove.append(collector)
    for collector in collectors_to_remove:
        try:
            REGISTRY.unregister(collector)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def reset_prometheus_registry():
    """Prometheus 레지스트리 리셋."""
    _clean_prometheus_registry()
    yield
    _clean_prometheus_registry()


class TestIncDroppedByTierBehavior:
    """inc_dropped_by_tier() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, reset_prometheus_registry):
        reset_backpressure_settings()
        import baldur.scaling.metrics as m

        m._metrics = None
        yield
        reset_backpressure_settings()
        m._metrics = None

    @pytest.mark.skipif(not HAS_PROMETHEUS, reason="prometheus_client not installed")
    def test_increments_without_error(self):
        """3개 tier 카운터가 예외 없이 증가한다."""
        settings = BackpressureSettings(metrics_enabled=True)
        metrics = BackpressureMetrics(settings=settings)

        metrics.inc_dropped_by_tier("critical")
        metrics.inc_dropped_by_tier("standard")
        metrics.inc_dropped_by_tier("non_essential")

    @pytest.mark.skipif(not HAS_PROMETHEUS, reason="prometheus_client not installed")
    def test_counter_has_tier_label(self):
        """dropped_by_tier_total 카운터에 tier 라벨이 포함된다."""
        settings = BackpressureSettings(metrics_enabled=True)
        metrics = BackpressureMetrics(settings=settings)

        assert "tier" in metrics.dropped_by_tier_total._labelnames

    def test_noop_when_disabled(self):
        """메트릭 비활성화 시 예외 없이 no-op으로 동작한다."""
        settings = BackpressureSettings(metrics_enabled=False)
        metrics = BackpressureMetrics(settings=settings)

        metrics.inc_dropped_by_tier("critical")
        metrics.inc_dropped_by_tier("standard")
        metrics.inc_dropped_by_tier("non_essential")
