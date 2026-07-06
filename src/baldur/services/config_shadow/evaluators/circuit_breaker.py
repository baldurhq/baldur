"""
Circuit Breaker Evaluator.

CB 서비스의 _should_open() 로직을 가상 상태에 대해 재실행하여
설정 변경 효과를 시뮬레이션한다.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from baldur.interfaces.event_journal import JournalEntry
from baldur.services.config_shadow.models import (
    EvaluationContext,
    EvaluatorResult,
    SimulationResult,
)


class CircuitBreakerEvaluator:
    """CB 설정 변경 효과 시뮬레이터."""

    @property
    def name(self) -> str:
        return "circuit_breaker"

    @property
    def event_types(self) -> list[str]:
        return ["circuit_breaker_opened", "circuit_breaker_closed"]

    def evaluate(self, context: EvaluationContext) -> EvaluatorResult:
        events = context.events
        baseline_config = context.baseline_config
        candidate_config = context.candidate_config

        baseline_opens = self._simulate(events, baseline_config)
        candidate_opens = self._simulate(events, candidate_config)

        delta_opens = candidate_opens.open_count - baseline_opens.open_count
        delta_pct = (
            (delta_opens / baseline_opens.open_count * 100)
            if baseline_opens.open_count > 0
            else 0.0
        )

        passed = self._check_pass_criteria(baseline_opens, candidate_opens)
        confidence, conf_warnings = self._calculate_confidence(
            events,
            baseline_config,
            candidate_config,
        )

        return EvaluatorResult(
            evaluator_name=self.name,
            passed=passed,
            confidence_score=confidence,
            baseline_metrics={
                "open_count": baseline_opens.open_count,
                "total_open_duration_seconds": baseline_opens.total_open_seconds,
                "avg_recovery_time_seconds": baseline_opens.avg_recovery_seconds,
            },
            candidate_metrics={
                "open_count": candidate_opens.open_count,
                "total_open_duration_seconds": candidate_opens.total_open_seconds,
                "avg_recovery_time_seconds": candidate_opens.avg_recovery_seconds,
            },
            delta={
                "open_count_delta": delta_opens,
                "open_count_change_percent": delta_pct,
            },
            details=(
                f"CB open {baseline_opens.open_count} -> "
                f"{candidate_opens.open_count} ({delta_pct:+.1f}%)"
            ),
            warnings=conf_warnings,
        )

    def _simulate(  # noqa: C901, PLR0912
        self,
        events: list[JournalEntry],
        config: dict[str, Any],
    ) -> SimulationResult:
        """이벤트 스트림에 대해 가상 CB 상태 머신을 구동한다."""
        failure_threshold = config.get("failure_threshold", 5)
        recovery_timeout = config.get("recovery_timeout", 30)
        minimum_calls = config.get("minimum_calls", 5)
        failure_rate_threshold = config.get("failure_rate_threshold", 0)
        sliding_window_size = config.get("sliding_window_size", 100)

        state = "closed"
        failure_window: deque[bool] = deque(maxlen=sliding_window_size)
        opened_at = None
        open_count = 0
        total_open_seconds = 0.0
        recovery_durations: list[float] = []

        for event in events:
            if state == "open" and (
                opened_at
                and (event.timestamp - opened_at).total_seconds() >= recovery_timeout
            ):
                state = "half_open"

            if event.event_type == "circuit_breaker_opened":
                # failure_count in context is optional; defaults to 1 per event.
                # When present (e.g., via enriched journal), seeds the window
                # with the reported count. Safe because close events clear the window.
                event_failures = event.context.get("failure_count", 1)
                for _ in range(min(event_failures, sliding_window_size)):
                    failure_window.append(True)

                if state == "closed":
                    total_calls = len(failure_window)
                    failure_count = sum(1 for x in failure_window if x)

                    if total_calls < minimum_calls:
                        continue

                    should_open = False

                    if failure_rate_threshold > 0:
                        rate = (
                            (failure_count / total_calls * 100)
                            if total_calls > 0
                            else 0
                        )
                        if rate >= failure_rate_threshold:
                            should_open = True

                    if failure_count >= failure_threshold:
                        should_open = True

                    if should_open:
                        state = "open"
                        opened_at = event.timestamp
                        open_count += 1

            elif event.event_type == "circuit_breaker_closed":
                if state in ("open", "half_open") and opened_at:
                    duration = (event.timestamp - opened_at).total_seconds()
                    total_open_seconds += duration
                    recovery_durations.append(duration)
                state = "closed"
                failure_window.clear()
                opened_at = None

        avg_recovery = (
            sum(recovery_durations) / len(recovery_durations)
            if recovery_durations
            else 0.0
        )

        return SimulationResult(
            open_count=open_count,
            total_open_seconds=total_open_seconds,
            avg_recovery_seconds=avg_recovery,
        )

    def _check_pass_criteria(
        self,
        baseline: SimulationResult,
        candidate: SimulationResult,
    ) -> bool:
        """후보 설정이 기존보다 나쁘지 않은지 판정한다."""
        if baseline.open_count > 0:
            increase_ratio = candidate.open_count / baseline.open_count
            if increase_ratio > 2.0:
                return False

        if baseline.avg_recovery_seconds > 0:
            recovery_ratio = (
                candidate.avg_recovery_seconds / baseline.avg_recovery_seconds
            )
            if recovery_ratio > 3.0:
                return False

        return True

    def _calculate_confidence(
        self,
        events: list[JournalEntry],
        baseline_config: dict[str, Any],
        candidate_config: dict[str, Any],
    ) -> tuple[float, list[str]]:
        """이벤트 충분성 + 방향성 기반 신뢰도 계산."""
        cb_events = [e for e in events if e.event_type.startswith("circuit_breaker_")]
        warnings: list[str] = []

        if len(cb_events) < 5:
            base_confidence = 0.2
        elif len(cb_events) < 20:
            base_confidence = 0.5
        elif len(cb_events) < 50:
            base_confidence = 0.8
        else:
            base_confidence = 0.95

        baseline_threshold = baseline_config.get("failure_threshold", 5)
        candidate_threshold = candidate_config.get("failure_threshold", 5)

        if candidate_threshold > baseline_threshold:
            ratio = baseline_threshold / candidate_threshold
            base_confidence *= ratio
            warnings.append(
                f"threshold_increase: threshold raised ({baseline_threshold}->"
                f"{candidate_threshold}), simulation accuracy limited due to "
                f"missing raw traffic data after CB open (confidence x{ratio:.2f})"
            )

        return min(base_confidence, 0.95), warnings
