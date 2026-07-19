"""
Baldur Models.

Provides data models for the baldur system.

Django-dependent models use PEP 562 lazy imports to avoid
AppRegistryNotReady errors when the package is imported before
Django setup.

Status: Internal
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baldur.models.blast_radius import BlastRadiusLevel
from baldur.models.compliance import ComplianceContext, ComplianceStandard
from baldur.models.drift_config import DriftThresholdConfig
from baldur.models.emergency import EmergencyLevel
from baldur.models.governance import BlockReason, GovernanceCheckResult
from baldur.models.learning import PatternType
from baldur.models.recovery_session import (
    RecoverySessionData,
    RecoveryStatus,
    RecoveryStepData,
    TriggerLevel,
)

if TYPE_CHECKING:
    from baldur.models.recovery_session_archive import (
        AbstractRecoverySessionArchive,
    )

__all__ = [
    "BlastRadiusLevel",
    "BlockReason",
    # Compliance shared models (599 D8)
    "ComplianceContext",
    "ComplianceStandard",
    "DriftThresholdConfig",
    "EmergencyLevel",
    "GovernanceCheckResult",
    # Learning shared models (599 D8)
    "PatternType",
    # Domain models (366)
    "RecoverySessionData",
    "RecoveryStepData",
    "RecoveryStatus",
    "TriggerLevel",
    # Django Abstract Models (legacy — used by adapters)
    "AbstractRecoverySessionArchive",
]

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AbstractRecoverySessionArchive": (
        "baldur.models.recovery_session_archive",
        "AbstractRecoverySessionArchive",
    ),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib

        module = importlib.import_module(module_path)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
