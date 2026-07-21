"""
Circuit Breaker Evaluator.

Replays recorded circuit-breaker events through the live trip predicate to
simulate the effect of a config change.

The predicate and the config defaults both come from the circuit breaker
service, so a simulated trip and a real trip cannot disagree. Events recorded
before the journal carried window evidence replay approximately — one failure
per event — which the evaluator's confidence warnings already account for.
"""

from __future__ import annotations

from collections import deque
from dataclasses import fields
from typing import Any

from baldur.interfaces.event_journal import JournalEntry
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.outcome_window import (
    FAILURE_OUTCOME,
    SUCCESS_OUTCOME,
    evaluate_trip,
)
from baldur.services.config_shadow.models import (
    EvaluationContext,
    EvaluatorResult,
    SimulationResult,
)


class CircuitBreakerEvaluator:
    """Simulator for the effect of a CB config change."""

    @property
    def name(self) -> str:
        return "circuit_breaker"

    @property
    def event_types(self) -> list[str]:
        return ["circuit_breaker_opened", "circuit_breaker_closed"]

    def evaluate(self, context: EvaluationContext) -> EvaluatorResult:
        events = context.events
        baseline_config = self._resolve_config(context.baseline_config)
        candidate_config = self._resolve_config(context.candidate_config)

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

    def _resolve_config(self, config: dict[str, Any]) -> CircuitBreakerConfig:
        """Overlay the supplied keys onto the live circuit-breaker defaults.

        ``baseline_config`` and ``candidate_config`` are arbitrary dicts, so an
        operator shadow-testing a single field supplies only that field. Filling
        the rest from ``CircuitBreakerConfig`` means the simulated baseline is
        the configuration actually running, not a set of evaluator-local
        literals that would make the reported delta meaningless.
        """
        known = {field.name for field in fields(CircuitBreakerConfig)}
        return CircuitBreakerConfig(
            **{key: value for key, value in config.items() if key in known}
        )

    def _simulate(
        self,
        events: list[JournalEntry],
        config: CircuitBreakerConfig,
    ) -> SimulationResult:
        """Drive a virtual CB state machine over the event stream."""
        state = "closed"
        outcome_window: deque[int] = deque(maxlen=config.sliding_window_size)
        consecutive_failures = 0
        opened_at = None
        open_count = 0
        total_open_seconds = 0.0
        recovery_durations: list[float] = []

        for event in events:
            if state == "open" and (
                opened_at
                and (event.timestamp - opened_at).total_seconds()
                >= config.recovery_timeout
            ):
                state = "half_open"

            if event.event_type == "circuit_breaker_opened":
                consecutive_failures = self._replay_open_event(
                    event, outcome_window, consecutive_failures
                )

                if state == "closed" and (
                    evaluate_trip(
                        consecutive_failures=consecutive_failures,
                        window_failures=sum(outcome_window),
                        window_total=len(outcome_window),
                        config=config,
                    )
                    is not None
                ):
                    state = "open"
                    opened_at = event.timestamp
                    open_count += 1

            elif event.event_type == "circuit_breaker_closed":
                if state in ("open", "half_open") and opened_at:
                    duration = (event.timestamp - opened_at).total_seconds()
                    total_open_seconds += duration
                    recovery_durations.append(duration)
                state = "closed"
                outcome_window.clear()
                consecutive_failures = 0
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

    def _replay_open_event(
        self,
        event: JournalEntry,
        outcome_window: deque[int],
        consecutive_failures: int,
    ) -> int:
        """Restore the window the live breaker saw when it opened.

        An enriched event reports the exact denominators, so the window is
        rebuilt from them. A legacy event carries none, so it contributes a
        single failure — approximate, and reflected in the confidence score.

        Returns:
            The consecutive-failure count after this event.
        """
        window_failures = event.context.get("window_failure_count")
        window_total = event.context.get("window_total_calls")

        if window_failures is None or window_total is None:
            outcome_window.append(FAILURE_OUTCOME)
            return consecutive_failures + 1

        capacity = outcome_window.maxlen or 0
        failures = max(min(window_failures, capacity), 0)
        successes = max(min(window_total, capacity) - failures, 0)

        outcome_window.clear()
        outcome_window.extend([SUCCESS_OUTCOME] * successes)
        outcome_window.extend([FAILURE_OUTCOME] * failures)

        reported_consecutive = event.context.get("consecutive_failure_count")
        return reported_consecutive if reported_consecutive is not None else failures

    def _check_pass_criteria(
        self,
        baseline: SimulationResult,
        candidate: SimulationResult,
    ) -> bool:
        """Decide whether the candidate config is no worse than the baseline."""
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
        baseline_config: CircuitBreakerConfig,
        candidate_config: CircuitBreakerConfig,
    ) -> tuple[float, list[str]]:
        """Compute confidence from event sufficiency plus change direction."""
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

        baseline_threshold = baseline_config.failure_threshold
        candidate_threshold = candidate_config.failure_threshold

        if candidate_threshold > baseline_threshold:
            ratio = baseline_threshold / candidate_threshold
            base_confidence *= ratio
            warnings.append(
                f"threshold_increase: threshold raised ({baseline_threshold}->"
                f"{candidate_threshold}), simulation accuracy limited due to "
                f"missing raw traffic data after CB open (confidence x{ratio:.2f})"
            )

        return min(base_confidence, 0.95), warnings
