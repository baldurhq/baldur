"""
Configuration History - Data Models.
"""

from dataclasses import dataclass
from typing import Any

from baldur.core.serializable import SerializableMixin


@dataclass
class ConfigVersion(SerializableMixin):
    """설정 버전 정보."""

    version: int
    timestamp: float
    config_type: str
    values: dict[str, Any]
    changed_by: str
    reason: str
    hash: str
