"""
X-Test Integration Scenario 기본 클래스 및 공통 모델.

모든 통합 테스트 시나리오의 기반이 되는 클래스와 데이터 모델을 정의합니다.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog
from django.utils import timezone

logger = structlog.get_logger()


# =============================================================================
# 시나리오 상태 및 결과 모델
# =============================================================================


class ScenarioStatus(str, Enum):
    """시나리오 실행 상태."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class ScenarioStep:
    """시나리오 개별 단계 결과."""

    step: int
    action: str
    component: str
    expected: str
    actual: str | None = None
    success: bool = False
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: str | None = None


@dataclass
class TimelineEvent:
    """시나리오 이벤트 타임라인 항목."""

    timestamp: str
    step: int
    action: str
    component: str
    result: str
    duration_ms: float


@dataclass
class ScenarioResult:
    """시나리오 실행 결과."""

    scenario_id: str
    scenario: str
    service_name: str
    status: ScenarioStatus
    started_at: str
    completed_at: str | None = None
    steps: list[ScenarioStep] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    snapshot: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    config: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """결과를 딕셔너리로 변환."""
        return {
            "scenario_id": self.scenario_id,
            "scenario": self.scenario,
            "service_name": self.service_name,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "steps": [
                {
                    "step": s.step,
                    "action": s.action,
                    "component": s.component,
                    "expected": s.expected,
                    "actual": s.actual,
                    "success": s.success,
                    "error": s.error,
                    "duration_ms": s.duration_ms,
                    "timestamp": s.timestamp,
                }
                for s in self.steps
            ],
            "timeline": [
                {
                    "timestamp": t.timestamp,
                    "step": t.step,
                    "action": t.action,
                    "component": t.component,
                    "result": t.result,
                    "duration_ms": t.duration_ms,
                }
                for t in self.timeline
            ],
            "snapshot": self.snapshot,
            "errors": self.errors,
            "config": self.config,
        }


# =============================================================================
# In-Memory 시나리오 저장소
# =============================================================================


_scenario_results: dict[str, ScenarioResult] = {}
_max_results = 100


def store_scenario_result(result: ScenarioResult) -> None:
    """시나리오 결과 저장."""
    global _scenario_results
    _scenario_results[result.scenario_id] = result

    # 오래된 결과 삭제
    if len(_scenario_results) > _max_results:
        sorted_ids = sorted(
            _scenario_results.keys(),
            key=lambda k: _scenario_results[k].started_at,
        )
        for old_id in sorted_ids[: len(sorted_ids) - _max_results]:
            del _scenario_results[old_id]


def get_scenario_result(scenario_id: str) -> ScenarioResult | None:
    """시나리오 결과 조회."""
    return _scenario_results.get(scenario_id)


def clear_scenario_results() -> int:
    """모든 시나리오 결과 삭제 (테스트용)."""
    global _scenario_results
    count = len(_scenario_results)
    _scenario_results = {}
    return count


# =============================================================================
# 기본 시나리오 클래스
# =============================================================================


class IntegrationScenario(ABC):
    """
    통합 테스트 시나리오 기본 클래스.

    각 시나리오는 여러 단계를 순차적으로 실행하고 결과를 수집합니다.
    단계별 타임라인과 스냅샷을 포함한 상세 결과를 제공합니다.
    """

    scenario_name: str = "base"
    max_timeout_seconds: int = 60

    def __init__(self, service_name: str, config: dict[str, Any] | None = None):
        self.service_name = service_name
        self.config = config or {}
        self.scenario_id = str(uuid.uuid4())
        self.result: ScenarioResult | None = None

    def _create_result(self) -> ScenarioResult:
        """시나리오 결과 객체 생성."""
        return ScenarioResult(
            scenario_id=self.scenario_id,
            scenario=self.scenario_name,
            service_name=self.service_name,
            status=ScenarioStatus.PENDING,
            started_at=timezone.now().isoformat(),
            config=self.config,
        )

    def _add_step(
        self,
        step_num: int,
        action: str,
        component: str,
        expected: str,
        actual: str,
        success: bool,
        error: str | None = None,
        duration_ms: float = 0.0,
    ) -> ScenarioStep:
        """단계 결과 기록."""
        timestamp = timezone.now().isoformat()
        step = ScenarioStep(
            step=step_num,
            action=action,
            component=component,
            expected=expected,
            actual=actual,
            success=success,
            error=error,
            duration_ms=duration_ms,
            timestamp=timestamp,
        )
        if self.result:
            self.result.steps.append(step)
            self.result.timeline.append(
                TimelineEvent(
                    timestamp=timestamp,
                    step=step_num,
                    action=action,
                    component=component,
                    result=actual if success else f"ERROR: {error}",
                    duration_ms=duration_ms,
                )
            )
        return step

    def _execute_step(
        self,
        step_num: int,
        action: str,
        component: str,
        expected: str,
        execute_fn: Callable[[], str],
    ) -> bool:
        """단계 실행 헬퍼. 예외 발생 시 자동으로 에러 기록."""
        start = time.perf_counter()
        try:
            actual = execute_fn()
            duration_ms = (time.perf_counter() - start) * 1000
            self._add_step(
                step_num, action, component, expected, actual, True, None, duration_ms
            )
            return True
        except Exception as e:
            duration_ms = (time.perf_counter() - start) * 1000
            error = str(e)
            self._add_step(
                step_num, action, component, expected, "", False, error, duration_ms
            )
            if self.result:
                self.result.errors.append(f"Step {step_num}: {error}")
            return False

    @abstractmethod
    def execute(self) -> None:
        """Run the scenario's step sequence.

        Subclasses mutate ``self.result`` (populated by ``run()`` beforehand)
        via ``_add_step`` / ``_execute_step``; the return value is unused.
        Early-exit on step failure with a bare ``return``.
        """
        pass

    def run(self) -> ScenarioResult:
        """시나리오 실행 및 결과 저장."""
        self.result = self._create_result()
        self.result.status = ScenarioStatus.RUNNING

        try:
            self.execute()

            # 모든 단계 성공 여부 확인
            all_success = all(s.success for s in self.result.steps)
            self.result.status = (
                ScenarioStatus.COMPLETED if all_success else ScenarioStatus.FAILED
            )
        except Exception as e:
            logger.exception(
                "test.integration_scenario_failed",
                _self=self.scenario_name,
                error=e,
            )
            self.result.status = ScenarioStatus.FAILED
            self.result.errors.append(str(e))
        finally:
            self.result.completed_at = timezone.now().isoformat()
            self._collect_snapshot()
            store_scenario_result(self.result)

        return self.result

    def _collect_snapshot(self) -> None:
        """시스템 스냅샷 수집."""
        if not self.result:
            return
        try:
            from ..base import collect_system_snapshot

            self.result.snapshot = collect_system_snapshot()
        except Exception as e:
            logger.warning(
                "test.integration_snapshot_collection",
                error=e,
            )
            self.result.snapshot = {"error": str(e)}
