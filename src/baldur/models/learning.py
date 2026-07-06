"""
Learning shared models.

OSS-chassis surface of the Self-Learning DNA feature: the pattern-type
enum consumed by the framework-agnostic API handlers and by private-tier
consumers (chaos impact prediction, runbook pattern matching, error-budget
shadow calculation). The learning service itself lives in the private
distribution and imports this enum from here (dependency-inversion
direction: private -> baldur.models).
# Extracted from services/learning/models.py per 599 D8.
"""

from __future__ import annotations

from enum import Enum


class PatternType(str, Enum):
    """Learned pattern categories."""

    FAILURE = "failure"
    RECOVERY = "recovery"
    PERFORMANCE = "performance"
    ANOMALY = "anomaly"
    OPTIMIZATION = "optimization"


__all__ = [
    "PatternType",
]
