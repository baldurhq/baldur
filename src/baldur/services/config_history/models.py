"""
Configuration History - Data Models.
"""

from dataclasses import dataclass
from typing import Any

from baldur.core.serializable import SerializableMixin


@dataclass
class ConfigVersion(SerializableMixin):
    """Config version information."""

    version: int
    timestamp: float
    config_type: str
    values: dict[str, Any]
    changed_by: str
    reason: str
    hash: str
