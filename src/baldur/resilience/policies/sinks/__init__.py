"""
Policy Sinks — 최종 실패 처리 모듈.

모든 Policy가 소진된 후 최종 실패를 처리하는
Sink 구현체를 제공한다.

- DLQSink: DLQ(Dead Letter Queue)에 최종 실패 저장
"""

from baldur.resilience.policies.sinks.dlq import DLQSink

__all__ = [
    "DLQSink",
]
