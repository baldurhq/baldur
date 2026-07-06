"""
Policy Hooks — 실행 이벤트 관찰 모듈.

PolicyComposer 파이프라인의 성공/실패/거부 이벤트를 관찰하는
Hook 구현체를 제공한다. 모든 Hook은 Fail-Open 원칙을 따른다.

- AuditHook: 감사 로깅
- SampledAuditHook: 샘플링 기반 감사 로깅
- MetricsHook: Prometheus 메트릭 수집
- EventBusHook: EventBus 이벤트 발행
"""

from baldur.resilience.policies.hooks.audit import AuditHook
from baldur.resilience.policies.hooks.event_bus import EventBusHook
from baldur.resilience.policies.hooks.metrics import MetricsHook
from baldur.resilience.policies.hooks.sampled_audit import SampledAuditHook

__all__ = [
    "AuditHook",
    "EventBusHook",
    "MetricsHook",
    "SampledAuditHook",
]
