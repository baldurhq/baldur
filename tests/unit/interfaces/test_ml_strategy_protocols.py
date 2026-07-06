"""
Tests for ML Strategy Interfaces — @runtime_checkable Protocol 검증.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: Protocol 메서드 시그니처, isinstance 검증 (하드코딩)
- Behavior: Protocol 동작 (isinstance 분기, 전략 교체) 검증

참조 소스:
- interfaces/ml_strategy.py (AnomalyDetectionStrategy, ForecastStrategy,
    ClassificationStrategy, BatchDetectable, StrategyLifecycle)
"""

from __future__ import annotations

from typing import Any

from baldur.interfaces.ml_strategy import (
    AnomalyDetectionStrategy,
    BatchDetectable,
    ClassificationStrategy,
    ForecastStrategy,
    StrategyLifecycle,
)

# =============================================================================
# Protocol 구현 Stub (테스트 전용)
# =============================================================================


class StubAnomalyDetector:
    """AnomalyDetectionStrategy Protocol 구현 스텁."""

    def detect(
        self, value: float, context: dict[str, Any] | None = None
    ) -> tuple[bool, float]:
        return (value > 100.0, abs(value))

    def update(self, value: float, context: dict[str, Any] | None = None) -> None:
        pass

    def reset(self) -> None:
        pass

    def get_feature_schema(self) -> dict[str, str] | None:
        return None


class StubForecaster:
    """ForecastStrategy Protocol 구현 스텁."""

    def update(self, value: float) -> float:
        return value

    def predict(self, steps_ahead: int = 1) -> float | None:
        return 42.0

    def get_confidence(self) -> float:
        return 0.85


class StubClassifier:
    """ClassificationStrategy Protocol 구현 스텁."""

    def classify(
        self,
        features: dict[str, float],
        context: dict[str, Any] | None = None,
    ) -> tuple[str, float]:
        return ("spike", 0.9)


class StubBatchDetector:
    """BatchDetectable Protocol 구현 스텁."""

    def detect_batch(
        self,
        values: list[float],
        contexts: list[dict[str, Any]] | None = None,
    ) -> list[tuple[bool, float]]:
        return [(v > 100.0, abs(v)) for v in values]

    def update_batch(self, values: list[float]) -> None:
        pass


class StubLifecycleStrategy:
    """StrategyLifecycle Protocol 구현 스텁."""

    def __init__(self) -> None:
        self._ready = False
        self._initialized = False
        self._warmed_up = False
        self._torn_down = False

    def initialize(self) -> None:
        self._initialized = True

    def warmup(self) -> None:
        self._warmed_up = True
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    def teardown(self) -> None:
        self._ready = False
        self._torn_down = True


class IncompleteDetector:
    """Protocol 미구현 객체 — detect() 누락."""

    def update(self, value: float) -> None:
        pass


class PartialBatchDetector:
    """BatchDetectable 부분 구현 — update_batch() 누락."""

    def detect_batch(
        self,
        values: list[float],
        contexts: list[dict[str, Any]] | None = None,
    ) -> list[tuple[bool, float]]:
        return [(False, 0.0)] * len(values)


# =============================================================================
# Contract Tests — Protocol isinstance 검증
# =============================================================================


class TestAnomalyDetectionStrategyContract:
    """AnomalyDetectionStrategy Protocol 계약 검증."""

    def test_isinstance_passes_for_compliant_class(self):
        """모든 필수 메서드를 구현한 객체는 isinstance 통과."""
        detector = StubAnomalyDetector()
        assert isinstance(detector, AnomalyDetectionStrategy)

    def test_isinstance_fails_for_incomplete_class(self):
        """필수 메서드 누락 시 isinstance 실패."""
        incomplete = IncompleteDetector()
        assert not isinstance(incomplete, AnomalyDetectionStrategy)

    def test_isinstance_fails_for_plain_object(self):
        """일반 객체는 isinstance 실패."""
        assert not isinstance(object(), AnomalyDetectionStrategy)

    def test_protocol_is_runtime_checkable(self):
        """@runtime_checkable 데코레이터 적용 확인."""
        assert hasattr(AnomalyDetectionStrategy, "__protocol_attrs__") or hasattr(
            AnomalyDetectionStrategy, "_is_runtime_protocol"
        )


class TestForecastStrategyContract:
    """ForecastStrategy Protocol 계약 검증."""

    def test_isinstance_passes_for_compliant_class(self):
        forecaster = StubForecaster()
        assert isinstance(forecaster, ForecastStrategy)

    def test_isinstance_fails_for_plain_object(self):
        assert not isinstance(object(), ForecastStrategy)


class TestClassificationStrategyContract:
    """ClassificationStrategy Protocol 계약 검증."""

    def test_isinstance_passes_for_compliant_class(self):
        classifier = StubClassifier()
        assert isinstance(classifier, ClassificationStrategy)

    def test_isinstance_fails_for_plain_object(self):
        assert not isinstance(object(), ClassificationStrategy)


class TestBatchDetectableContract:
    """BatchDetectable Protocol 계약 검증."""

    def test_isinstance_passes_for_compliant_class(self):
        batch = StubBatchDetector()
        assert isinstance(batch, BatchDetectable)

    def test_isinstance_fails_for_non_batch_detector(self):
        """일반 AnomalyDetector는 BatchDetectable이 아님."""
        detector = StubAnomalyDetector()
        assert not isinstance(detector, BatchDetectable)

    def test_isinstance_fails_for_plain_object(self):
        assert not isinstance(object(), BatchDetectable)


class TestStrategyLifecycleContract:
    """StrategyLifecycle Protocol 계약 검증."""

    def test_isinstance_passes_for_compliant_class(self):
        lifecycle = StubLifecycleStrategy()
        assert isinstance(lifecycle, StrategyLifecycle)

    def test_isinstance_fails_for_plain_object(self):
        assert not isinstance(object(), StrategyLifecycle)

    def test_isinstance_fails_for_non_lifecycle_detector(self):
        """일반 AnomalyDetector는 StrategyLifecycle이 아님."""
        detector = StubAnomalyDetector()
        assert not isinstance(detector, StrategyLifecycle)


# =============================================================================
# Behavior Tests — Protocol 동작 검증
# =============================================================================


class TestAnomalyDetectionStrategyBehavior:
    """AnomalyDetectionStrategy 동작 검증."""

    def test_detect_returns_tuple(self):
        """detect()는 (bool, float) 튜플을 반환."""
        detector = StubAnomalyDetector()
        result = detector.detect(150.0)
        assert isinstance(result, tuple)
        assert len(result) == 2
        is_anomaly, score = result
        assert isinstance(is_anomaly, bool)
        assert isinstance(score, float)

    def test_detect_with_context(self):
        """detect()에 context를 전달해도 정상 동작."""
        detector = StubAnomalyDetector()
        result = detector.detect(50.0, context={"service": "payment"})
        assert isinstance(result, tuple)

    def test_get_feature_schema_returns_none_for_statistics(self):
        """통계 기반 전략은 feature_schema가 None."""
        detector = StubAnomalyDetector()
        assert detector.get_feature_schema() is None

    def test_reset_clears_state(self):
        """reset()은 예외 없이 실행."""
        detector = StubAnomalyDetector()
        detector.update(100.0)
        detector.reset()  # 예외 발생 안 함


class TestBatchDetectableBehavior:
    """BatchDetectable 분기 동작 검증."""

    def test_detect_batch_returns_list(self):
        """detect_batch()는 입력과 동일 길이의 리스트 반환."""
        batch = StubBatchDetector()
        values = [10.0, 200.0, 50.0]
        results = batch.detect_batch(values)
        assert len(results) == len(values)

    def test_isinstance_branch_batch_vs_single(self):
        """BatchDetectable isinstance 분기로 배치/단건 처리 결정."""
        batch_detector = StubBatchDetector()
        single_detector = StubAnomalyDetector()

        # BatchDetectable 분기
        assert isinstance(batch_detector, BatchDetectable)
        assert not isinstance(single_detector, BatchDetectable)


class TestStrategyLifecycleBehavior:
    """StrategyLifecycle 동작 검증."""

    def test_lifecycle_sequence(self):
        """initialize → warmup → is_ready 순서대로 상태 전이."""
        lifecycle = StubLifecycleStrategy()

        assert not lifecycle.is_ready()

        lifecycle.initialize()
        assert lifecycle._initialized

        lifecycle.warmup()
        assert lifecycle._warmed_up
        assert lifecycle.is_ready()

    def test_teardown_releases_readiness(self):
        """teardown() 후 is_ready()가 False."""
        lifecycle = StubLifecycleStrategy()
        lifecycle.initialize()
        lifecycle.warmup()
        assert lifecycle.is_ready()

        lifecycle.teardown()
        assert not lifecycle.is_ready()
        assert lifecycle._torn_down
