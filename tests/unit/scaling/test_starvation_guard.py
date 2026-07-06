"""
Starvation Guard 단위 테스트 — 최소 트래픽 보장 + Per-Tier Counter.

테스트 항목:
- 계약: ServiceConfig.min_traffic_percentage 기본값 5.0
- 동작: Per-Tier Dropped Counter 증가 및 격리
- 동작: Per-Tier Processed Counter 증가 및 격리
- 동작: RateControllerState에 tier별 카운터 포함
- 동작: Starvation Relief + RecoveryGate 안전 차단
- 계약: min_traffic_percentage → LoadShedding 최소 보장

NOTE:
- BACKPRESSURE_TIER_RULES 계약 검증 → tests/unit/api/test_tiering_middleware_merge.py
- Degraded tier forced deadline 검증 → tests/unit/api/test_degraded_tier_deadline.py
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.scaling.config import (
    BackpressureSettings,
    BackpressureStrategy,
    reset_backpressure_settings,
)
from baldur.scaling.rate_controller import (
    STARVATION_RELIEF_SECONDS,
    STARVATION_RELIEF_WATERMARK,
    RateController,
    reset_rate_controller,
)
from baldur.services.circuit_breaker.models import ServiceConfig

# =============================================================================
# 작업 B: ServiceConfig.min_traffic_percentage 기본값 계약 검증
# =============================================================================


class TestMinTrafficPercentageContract:
    """ServiceConfig.min_traffic_percentage 기본값 계약 검증."""

    def test_default_value_5_percent(self):
        """기본값은 5.0%."""
        config = ServiceConfig(service_id="test-api", criticality="low")
        assert config.min_traffic_percentage == 5.0

    def test_explicit_zero_overrides_default(self):
        """명시적 0.0은 기본값 5.0을 덮어쓴다."""
        config = ServiceConfig(
            service_id="batch-worker",
            criticality="low",
            min_traffic_percentage=0.0,
        )
        assert config.min_traffic_percentage == 0.0


class TestMinTrafficPercentageBehavior:
    """min_traffic_percentage와 LoadShedding 연동 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_shedding_manager(self):
        from baldur.services.circuit_breaker.load_shedding.manager import (
            LoadSheddingManager,
        )

        LoadSheddingManager.reset_instance()
        yield
        LoadSheddingManager.reset_instance()

    def test_evaluate_shedding_minimum_guarantee(self):
        """traffic_limit=0 + min_traffic=5.0 → evaluate_shedding ≥ 5.0."""
        from baldur.services.circuit_breaker.load_shedding.manager import (
            LoadSheddingManager,
        )

        manager = LoadSheddingManager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                shed_priority=0,
            )
        )
        manager.register_service(
            ServiceConfig(
                service_id="dashboard-api",
                criticality="low",
                shed_priority=10,
                # 기본값 min_traffic_percentage=5.0
            )
        )

        # Level 3 유발 (75% 에러)
        manager.set_error_rate("payment-api", 75.0)

        # traffic_limit=0% 이지만 min_traffic_percentage=5.0이 보장
        allowed = manager.evaluate_shedding("dashboard-api")
        assert allowed >= 5.0

    def test_critical_services_unaffected(self):
        """critical 서비스는 min_traffic_percentage 무관하게 100% 유지."""
        from baldur.services.circuit_breaker.load_shedding.manager import (
            LoadSheddingManager,
        )

        manager = LoadSheddingManager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                shed_priority=0,
            )
        )

        manager.set_error_rate("payment-api", 80.0)
        assert manager.evaluate_shedding("payment-api") == 100.0


# =============================================================================
# 작업 C: Per-Tier Dropped Counter 동작 검증
# =============================================================================


class TestPerTierDroppedCounterBehavior:
    """Per-tier dropped counter 증가 및 격리 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_dropped_by_tier_increments(self):
        """non_essential 거부 시 해당 tier 카운터가 증가한다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰을 소진하여 non_essential watermark 미만으로 만듦
        for _ in range(9):
            controller._token_bucket.consume()

        before = controller.get_state().dropped_by_tier["non_essential"]
        controller.should_process(priority="non_essential")
        after = controller.get_state().dropped_by_tier["non_essential"]

        assert after == before + 1

    def test_dropped_by_tier_isolation(self):
        """critical 거부 시 non_essential 카운터는 변하지 않는다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰 소진
        controller._token_bucket.consume()

        before_ne = controller.get_state().dropped_by_tier["non_essential"]
        controller.should_process(priority="critical")
        after_ne = controller.get_state().dropped_by_tier["non_essential"]

        assert after_ne == before_ne

    def test_get_state_includes_tier_counts(self):
        """RateControllerState에 dropped_by_tier가 포함된다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)
        state = controller.get_state()

        assert state.dropped_by_tier is not None
        assert set(state.dropped_by_tier.keys()) == {
            "critical",
            "standard",
            "non_essential",
        }

    def test_counter_thread_safety(self):
        """멀티스레드 동시 접근 시 카운터가 정확하다."""
        import threading

        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰을 거의 소진
        for _ in range(9999):
            controller._token_bucket.consume()

        threads = []
        for _ in range(10):
            t = threading.Thread(
                target=lambda: [
                    controller.should_process(priority="non_essential")
                    for _ in range(100)
                ]
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        state = controller.get_state()
        # dropped + processed == 1000 (10 threads × 100 calls)
        total = state.dropped_by_tier["non_essential"] + (
            state.processed_by_tier.get("non_essential", 0)
            if state.processed_by_tier
            else 0
        )
        assert total == 1000


# =============================================================================
# 작업 E: Per-Tier Processed Counter 동작 검증
# =============================================================================


class TestPerTierProcessedCounterBehavior:
    """Per-tier processed counter 증가 및 격리 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_processed_by_tier_increments(self):
        """허용 시 해당 tier processed 카운터가 증가한다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=100.0,
        )
        controller = RateController(settings=settings)

        before = controller.get_state().processed_by_tier["standard"]
        controller.should_process(priority="standard")
        after = controller.get_state().processed_by_tier["standard"]

        assert after == before + 1

    def test_processed_by_tier_isolation(self):
        """critical 허용 시 non_essential processed 카운터는 변하지 않는다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=100.0,
        )
        controller = RateController(settings=settings)

        before_ne = controller.get_state().processed_by_tier["non_essential"]
        controller.should_process(priority="critical")
        after_ne = controller.get_state().processed_by_tier["non_essential"]

        assert after_ne == before_ne

    def test_get_state_includes_processed_by_tier(self):
        """RateControllerState에 processed_by_tier가 포함된다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)
        state = controller.get_state()

        assert state.processed_by_tier is not None
        assert set(state.processed_by_tier.keys()) == {
            "critical",
            "standard",
            "non_essential",
        }

    def test_initial_processed_counts_are_zero(self):
        """초기 tier별 processed 카운터는 모두 0이다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)
        state = controller.get_state()

        for tier, count in state.processed_by_tier.items():
            assert count == 0, f"{tier} tier initial processed count should be 0"


# =============================================================================
# Starvation Relief + RecoveryGate 안전 차단 동작 검증
# =============================================================================


class TestStarvationReliefSafetyBehavior:
    """Starvation Relief가 RecoveryGate 안정성 확인 후에만 활성화되는 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_relief_blocked_on_high_cpu(self):
        """CPU > 80% → Starvation Relief 차단 (거부 유지)."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # non_essential 마지막 허용 시각을 STARVATION_RELIEF_SECONDS + 1초 전으로 설정
        controller._tier_last_allowed["non_essential"] = (
            time.monotonic() - STARVATION_RELIEF_SECONDS - 1.0
        )

        # 토큰을 거의 소진 (watermark 미만)
        for _ in range(9):
            controller._token_bucket.consume()

        # RecoveryGate가 CPU > 80%로 Relief 차단
        with patch.object(
            controller,
            "_check_starvation_relief_allowed",
            return_value=False,
        ):
            result = controller.should_process(priority="non_essential")

        assert result is False

    def test_relief_blocked_on_high_error_rate(self):
        """error_rate > 5% → Starvation Relief 차단."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        controller._tier_last_allowed["non_essential"] = (
            time.monotonic() - STARVATION_RELIEF_SECONDS - 1.0
        )

        for _ in range(9):
            controller._token_bucket.consume()

        with patch.object(
            controller,
            "_check_starvation_relief_allowed",
            return_value=False,
        ):
            result = controller.should_process(priority="non_essential")

        assert result is False

    def test_relief_allowed_on_stable_system(self):
        """CPU < 80% + error_rate < 5% → Relief 허용 (watermark 완화)."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        controller._tier_last_allowed["non_essential"] = (
            time.monotonic() - STARVATION_RELIEF_SECONDS - 1.0
        )

        # 토큰 비율을 relief watermark(0.3)와 기본 watermark(0.6) 사이로 유지
        # token_ratio ~= 0.4 → 기본 watermark(0.6) 미만이지만 relief watermark(0.3) 이상
        for _ in range(6):
            controller._token_bucket.consume()

        with patch.object(
            controller,
            "_check_starvation_relief_allowed",
            return_value=True,
        ):
            result = controller.should_process(priority="non_essential")

        # watermark가 0.6 → 0.3으로 완화되어 토큰 비율 ~0.4로 통과
        assert result is True

    def test_relief_constants(self):
        """Starvation Relief 상수 계약값 확인."""
        assert STARVATION_RELIEF_SECONDS == 300.0
        assert STARVATION_RELIEF_WATERMARK == 0.3

    def test_check_starvation_relief_calls_recovery_gate(self):
        """_check_starvation_relief_allowed()는 RecoveryGate를 호출한다."""
        pytest.importorskip("baldur_pro")
        settings = BackpressureSettings()
        controller = RateController(settings=settings)

        mock_gate = MagicMock()
        mock_gate.check_recovery_allowed.return_value = (
            True,
            "All metrics within thresholds",
        )

        with patch(
            "baldur_pro.services.emergency_mode.recovery_gate.RecoveryGate",
            return_value=mock_gate,
        ):
            result = controller._check_starvation_relief_allowed()

        assert result is True
        mock_gate.check_recovery_allowed.assert_called_once()

    def test_check_starvation_relief_returns_false_on_gate_denial(self):
        """RecoveryGate가 차단하면 False를 반환한다."""
        pytest.importorskip("baldur_pro")
        settings = BackpressureSettings()
        controller = RateController(settings=settings)

        mock_gate = MagicMock()
        mock_gate.check_recovery_allowed.return_value = (
            False,
            "CPU usage too high: 85.0%",
        )

        with patch(
            "baldur_pro.services.emergency_mode.recovery_gate.RecoveryGate",
            return_value=mock_gate,
        ):
            result = controller._check_starvation_relief_allowed()

        assert result is False

    def test_check_starvation_relief_returns_false_on_exception(self):
        """RecoveryGate import 실패 시 안전하게 False를 반환한다."""
        pytest.importorskip("baldur_pro")
        settings = BackpressureSettings()
        controller = RateController(settings=settings)

        with patch(
            "baldur_pro.services.emergency_mode.recovery_gate.RecoveryGate",
            side_effect=RuntimeError("import failed"),
        ):
            result = controller._check_starvation_relief_allowed()

        assert result is False


# =============================================================================
# Prometheus 메트릭 테스트
# =============================================================================


class TestProcessedByTierMetricBehavior:
    """processed_by_tier_total Prometheus Counter 동작 검증."""

    @pytest.fixture(autouse=True)
    def _metrics(self):
        """prometheus_client 중복 등록 방지를 위해 전용 Registry 사용."""
        import prometheus_client

        from baldur.scaling.metrics import BackpressureMetrics

        registry = prometheus_client.CollectorRegistry()
        settings = BackpressureSettings(
            metrics_enabled=True,
            metrics_prefix="test_sg_",
        )
        # BackpressureMetrics builds its metrics via baldur.metrics.registry
        # get_or_create_* helpers, which register on prometheus_client.REGISTRY.
        # Patching REGISTRY to a test-local CollectorRegistry is what isolates
        # the metrics between tests.
        with patch.object(prometheus_client, "REGISTRY", registry):
            self.metrics = BackpressureMetrics(settings=settings)
            yield

    def test_processed_by_tier_total_metric_exists(self):
        """BackpressureMetrics에 processed_by_tier_total Counter가 존재한다."""
        assert hasattr(self.metrics, "processed_by_tier_total")

    def test_inc_processed_by_tier_method_exists(self):
        """BackpressureMetrics에 inc_processed_by_tier() 메서드가 존재한다."""
        assert hasattr(self.metrics, "inc_processed_by_tier")
        assert callable(self.metrics.inc_processed_by_tier)

    def test_inc_processed_by_tier_increments_counter(self):
        """inc_processed_by_tier() 호출 시 카운터가 증가한다."""
        before = self.metrics.processed_by_tier_total.labels(
            tier="standard"
        )._value.get()
        self.metrics.inc_processed_by_tier("standard")
        after = self.metrics.processed_by_tier_total.labels(
            tier="standard"
        )._value.get()
        assert after == before + 1.0
