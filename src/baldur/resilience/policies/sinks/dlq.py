"""
DLQ Sink — 최종 실패를 DLQ(Dead Letter Queue)에 저장.

기존 services/retry_handler/sinks.py의 DLQSink를 re-export한다.
PolicyComposer의 FailureSink로 사용한다.
"""

from baldur.services.retry_handler.sinks import DLQSink

__all__ = ["DLQSink"]
