"""
Stuck Detector - Zero-variance 기반 Stuck 감지.

메트릭의 분산이 0에 가깝고 에러율이 높으면 시스템이
논리적으로 멈춘(Stuck) 것으로 판단합니다.

Zero-variance Stuck 조건:
- 특정 메트릭 X가 일정 시간 동안 σ²(X) ≈ 0 (변화량 없음)
- 동시에 에러율이 임계치 이상

예시:
- DLQ pending_count가 계속 1000에서 변하지 않고 처리 실패 중
- Circuit Breaker가 OPEN 상태에서 계속 고정
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class MetricSample:
    """메트릭 샘플."""

    value: float
    """메트릭 값."""

    timestamp: float
    """수집 시각 (Unix timestamp)."""

    error: bool = False
    """이 샘플이 에러 상태인지."""


@dataclass
class MetricWindow:
    """
    슬라이딩 윈도우 메트릭 컨테이너.

    최근 N개의 샘플을 유지하며 분산과 에러율을 계산합니다.
    """

    samples: deque[MetricSample] = field(default_factory=deque)
    """샘플 큐."""

    max_size: int = 20
    """최대 샘플 수."""

    def add(self, value: float, error: bool = False) -> None:
        """
        샘플 추가.

        Args:
            value: 메트릭 값
            error: 에러 상태 여부
        """
        self.samples.append(
            MetricSample(value=value, timestamp=time.time(), error=error)
        )
        while len(self.samples) > self.max_size:
            self.samples.popleft()

    def variance(self) -> float:
        """
        분산 계산.

        Returns:
            샘플들의 분산 (샘플 부족 시 무한대)
        """
        if len(self.samples) < 2:
            return float("inf")  # 샘플 부족 시 무한대 반환

        values = [s.value for s in self.samples]
        n = len(values)
        mean = sum(values) / n
        return sum((x - mean) ** 2 for x in values) / n

    def error_rate(self) -> float:
        """
        에러율 계산.

        Returns:
            에러 샘플 비율 (0.0 ~ 1.0)
        """
        if not self.samples:
            return 0.0
        error_count = sum(1 for s in self.samples if s.error)
        return error_count / len(self.samples)

    def mean(self) -> float:
        """
        평균 계산.

        Returns:
            샘플들의 평균 (샘플 없으면 0)
        """
        if not self.samples:
            return 0.0
        return sum(s.value for s in self.samples) / len(self.samples)

    def is_stuck(
        self,
        variance_threshold: float = 0.001,
        error_rate_threshold: float = 0.5,
    ) -> bool:
        """
        Stuck 여부 판단.

        조건: 분산 ≈ 0 AND 에러율 > 임계치

        Args:
            variance_threshold: 분산 임계치 (기본 0.001)
            error_rate_threshold: 에러율 임계치 (기본 50%)

        Returns:
            Stuck 여부
        """
        if len(self.samples) < 5:
            return False  # 최소 샘플 필요

        var = self.variance()
        err_rate = self.error_rate()

        # 분산이 매우 낮고 에러율이 높으면 Stuck
        return var < variance_threshold and err_rate > error_rate_threshold

    def clear(self) -> None:
        """샘플 초기화."""
        self.samples.clear()


@dataclass
class StuckDetectionResult:
    """Stuck 감지 결과."""

    component: str
    """컴포넌트 이름."""

    is_stuck: bool
    """Stuck 여부."""

    variance: float
    """현재 분산."""

    error_rate: float
    """현재 에러율."""

    sample_count: int
    """샘플 수."""

    duration_seconds: float
    """첫 샘플 이후 경과 시간."""

    mean_value: float = 0.0
    """평균값."""

    details: dict[str, Any] = field(default_factory=dict)
    """추가 상세 정보."""


class StuckDetector:
    """
    Stuck 감지기.

    각 컴포넌트의 메트릭을 추적하고 Zero-variance 상태를 감지합니다.

    사용 예시:
        detector = StuckDetector()

        # 메트릭 기록 (주기적으로)
        detector.record("dlq", pending_count=100, error=False)
        detector.record("dlq", pending_count=100, error=True)

        # Stuck 확인
        result = detector.check("dlq")
        if result.is_stuck:
            trigger_recovery()
    """

    def __init__(
        self,
        window_size: int = 20,
        variance_threshold: float = 0.001,
        error_rate_threshold: float = 0.5,
    ):
        """
        초기화.

        Args:
            window_size: 샘플 윈도우 크기
            variance_threshold: Stuck 판단 분산 임계치
            error_rate_threshold: Stuck 판단 에러율 임계치
        """
        self._window_size = window_size
        self._variance_threshold = variance_threshold
        self._error_rate_threshold = error_rate_threshold

        self._windows: dict[str, MetricWindow] = {}
        self._first_sample_time: dict[str, float] = {}
        self._lock = threading.RLock()

    def record(
        self,
        component: str,
        value: float,
        error: bool = False,
    ) -> None:
        """
        메트릭 기록.

        Args:
            component: 컴포넌트 이름
            value: 메트릭 값 (예: pending_count, queue_size)
            error: 이 샘플이 에러 상태인지
        """
        with self._lock:
            if component not in self._windows:
                self._windows[component] = MetricWindow(
                    samples=deque(maxlen=self._window_size),
                    max_size=self._window_size,
                )
                self._first_sample_time[component] = time.time()

            self._windows[component].add(value=value, error=error)

    def check(self, component: str) -> StuckDetectionResult:
        """
        Stuck 여부 확인.

        Args:
            component: 컴포넌트 이름

        Returns:
            StuckDetectionResult
        """
        with self._lock:
            if component not in self._windows:
                return StuckDetectionResult(
                    component=component,
                    is_stuck=False,
                    variance=float("inf"),
                    error_rate=0.0,
                    sample_count=0,
                    duration_seconds=0.0,
                    mean_value=0.0,
                )

            window = self._windows[component]
            first_time = self._first_sample_time.get(component, time.time())
            duration = time.time() - first_time

            variance_stuck = window.is_stuck(
                variance_threshold=self._variance_threshold,
                error_rate_threshold=self._error_rate_threshold,
            )

            # Time-based stuck detection: component stuck if duration exceeds
            # stuck_threshold_seconds regardless of variance
            time_stuck = False
            try:
                from baldur.meta.config import get_meta_watchdog_settings

                threshold = get_meta_watchdog_settings().stuck_threshold_seconds
                time_stuck = (
                    duration >= threshold
                    and window.error_rate() >= self._error_rate_threshold
                )
            except Exception:
                pass

            is_stuck = variance_stuck or time_stuck

            return StuckDetectionResult(
                component=component,
                is_stuck=is_stuck,
                variance=window.variance(),
                error_rate=window.error_rate(),
                sample_count=len(window.samples),
                duration_seconds=duration,
                mean_value=window.mean(),
                details={
                    "variance_threshold": self._variance_threshold,
                    "error_rate_threshold": self._error_rate_threshold,
                },
            )

    def check_all(self) -> dict[str, StuckDetectionResult]:
        """
        모든 컴포넌트 Stuck 확인.

        Returns:
            컴포넌트별 Stuck 감지 결과
        """
        with self._lock:
            return {comp: self.check(comp) for comp in self._windows}

    def get_stuck_components(self) -> list[str]:
        """
        Stuck 상태인 컴포넌트 목록 반환.

        Returns:
            Stuck 컴포넌트 이름 목록
        """
        results = self.check_all()
        return [comp for comp, result in results.items() if result.is_stuck]

    def clear(self, component: str | None = None) -> None:
        """
        메트릭 초기화.

        Args:
            component: 특정 컴포넌트만 초기화 (None이면 전체)
        """
        with self._lock:
            if component:
                self._windows.pop(component, None)
                self._first_sample_time.pop(component, None)
            else:
                self._windows.clear()
                self._first_sample_time.clear()

    def get_component_names(self) -> list[str]:
        """
        등록된 컴포넌트 이름 목록 반환.

        Returns:
            컴포넌트 이름 목록
        """
        with self._lock:
            return list(self._windows.keys())


# =============================================================================
# 싱글톤
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_stuck_detector, configure_stuck_detector, reset_stuck_detector = (
    make_singleton_factory("stuck_detector", StuckDetector)
)
