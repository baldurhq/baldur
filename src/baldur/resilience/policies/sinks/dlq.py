"""
DLQ Sink — store the terminal failure in the DLQ (Dead Letter Queue).

Re-exports the existing DLQSink from services/retry_handler/sinks.py.
Used as PolicyComposer's FailureSink.
"""

from baldur.services.retry_handler.sinks import DLQSink

__all__ = ["DLQSink"]
