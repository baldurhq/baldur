"""
Policy Sinks — terminal failure handling module.

Provides Sink implementations that handle the terminal failure once every
Policy is exhausted.

- DLQSink: store the terminal failure in the DLQ (Dead Letter Queue)
"""

from baldur.resilience.policies.sinks.dlq import DLQSink

__all__ = [
    "DLQSink",
]
