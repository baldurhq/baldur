"""
Compliance shared models.

OSS-chassis surface of the compliance feature: the enum of supported
standards and the typed execution context. These are consumed by the OSS
settings validators and framework-agnostic API handlers; the compliance
check engine itself lives in the private distribution and imports these
models from here (dependency-inversion direction: private -> baldur.models).
# Extracted from services/compliance/models.py per 599 D8.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ComplianceStandard(str, Enum):
    """Supported compliance standards."""

    DORA_2025 = "DORA_2025"
    SOC2 = "SOC2"
    PCI_DSS = "PCI_DSS"
    HIPAA = "HIPAA"
    GDPR = "GDPR"
    ISO27001 = "ISO27001"
    CUSTOM = "CUSTOM"


@dataclass
class ComplianceContext:
    """Typed context for compliance check execution.

    Follows project convention of using @dataclass for structured contexts
    (consistent with context/actor_context.py:Actor pattern).
    """

    service_name: str = "baldur"  # Target service (maps to audit service_name)
    domain: str | None = None  # Domain partition (optional)
    triggered_by: str = "celery_beat"  # "celery_beat" | "api_manual" | "on_demand"
    request: Any = None  # Django HttpRequest (for audit buffer integration)


__all__ = [
    "ComplianceContext",
    "ComplianceStandard",
]
