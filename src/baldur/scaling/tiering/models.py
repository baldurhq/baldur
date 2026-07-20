"""
Tiering System Data Models.

Data classes for tier definitions, mappings, overrides, and results.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import ip_address, ip_network
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

from .enums import OverrideIdentifierType, TierFallbackReason, TierMatchType


@dataclass
class TierResult:
    """Tier resolution result with fallback tracking."""

    tier_id: str
    multiplier: float
    is_fallback: bool
    fallback_reason: TierFallbackReason
    latency_ms: float


@dataclass
class TierDefinition(SerializableMixin):
    """
    Tier definition with emergency mode behavior.

    Attributes:
        id: Unique identifier (e.g., "critical")
        name: Display name (e.g., "Mission Critical")
        multiplier: Emergency mode rate multiplier (0.0 ~ 1.0)
        priority: Priority level (higher = more important)
        description: Description of the tier
        color: UI display color
    """

    id: str
    name: str
    multiplier: float  # 0.0 = blocked, 1.0 = full access
    priority: int = 0
    description: str = ""
    color: str = "#000000"

    def __post_init__(self):
        """Validate tier definition."""
        if self.multiplier < 0 or self.multiplier > 1:
            raise ValueError(
                f"Multiplier must be between 0 and 1, got {self.multiplier}"
            )
        if not self.id:
            raise ValueError("Tier ID is required")


@dataclass
class TierMapping(SerializableMixin):
    """
    API path to tier mapping.

    Attributes:
        pattern: Path pattern (exact, wildcard, or regex)
        tier_id: Target tier ID
        pattern_type: Type of pattern matching
        priority: Mapping priority (higher = matched first)
        description: Description of the mapping
        methods: HTTP methods this mapping applies to (None = all methods)
    """

    pattern: str
    tier_id: str
    pattern_type: TierMatchType = TierMatchType.EXACT
    priority: int = 0
    description: str = ""
    methods: frozenset[str] | None = None

    # Compiled regex cache
    _compiled_pattern: re.Pattern | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self):
        """Compile regex pattern and normalize methods to uppercase frozenset."""
        if self.pattern_type == TierMatchType.REGEX:
            try:
                self._compiled_pattern = re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern '{self.pattern}': {e}") from e

        # Normalize methods: list/tuple/set → frozenset, case → UPPER
        if self.methods is not None:
            if not isinstance(self.methods, frozenset):
                self.methods = frozenset(m.upper() for m in self.methods)
            else:
                normalized = frozenset(m.upper() for m in self.methods)
                if normalized != self.methods:
                    self.methods = normalized

    def matches(self, path: str, method: str | None = None) -> bool:
        """
        Check if the path (and optionally method) matches this mapping.

        Args:
            path: API path to check
            method: HTTP method (GET, POST, etc.) — None ignores the method

        Returns:
            True if path matches and method is compatible
        """
        # Method filter: if the mapping declares methods and the request
        # method is not in that set, it does not match
        if (
            self.methods is not None
            and method is not None
            and method.upper() not in self.methods
        ):
            return False

        # Path matching logic
        if self.pattern_type == TierMatchType.EXACT:
            return path == self.pattern
        if self.pattern_type == TierMatchType.WILDCARD:
            return fnmatch.fnmatch(path, self.pattern)
        if self.pattern_type == TierMatchType.REGEX:
            if self._compiled_pattern is None:
                self._compiled_pattern = re.compile(self.pattern)
            return bool(self._compiled_pattern.match(path))
        return False

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        # _compiled_pattern is a private cache field — exclude from output
        data.pop("_compiled_pattern", None)
        if self.methods is not None:
            data["methods"] = sorted(self.methods)
        else:
            data.pop("methods", None)
        return super()._post_serialize(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TierMapping:
        """Create from dictionary."""
        methods = None
        if "methods" in data and data["methods"] is not None:
            methods = frozenset(data["methods"])

        return cls(
            pattern=data["pattern"],
            tier_id=data["tier_id"],
            pattern_type=TierMatchType(data.get("pattern_type", "exact")),
            priority=data.get("priority", 0),
            description=data.get("description", ""),
            methods=methods,
        )


@dataclass
class TierOverride(SerializableMixin):
    """
    Per-client tier override.

    Attributes:
        identifier: IP, user ID, or API key
        identifier_type: Type of identifier
        tier_id: Tier to apply
        reason: Reason for override
        expires_at: Optional expiration time
    """

    identifier: str
    identifier_type: OverrideIdentifierType
    tier_id: str
    reason: str = ""
    expires_at: datetime | None = None

    def is_expired(self) -> bool:
        """Check if override has expired."""
        if self.expires_at is None:
            return False
        return utc_now() > self.expires_at

    def matches_ip(self, client_ip: str) -> bool:
        """
        Check if the client IP matches this override.

        Supports both exact IP and CIDR notation.
        """
        if self.identifier_type != OverrideIdentifierType.IP:
            return False

        try:
            client = ip_address(client_ip)
            if "/" in self.identifier:
                # CIDR notation
                network = ip_network(self.identifier, strict=False)
                return client in network
            # Exact IP
            return client == ip_address(self.identifier)
        except ValueError:
            return False

    def matches(self, identifier: str, identifier_type: OverrideIdentifierType) -> bool:
        """Check if identifier matches this override."""
        if self.identifier_type != identifier_type:
            return False

        if self.is_expired():
            return False

        if identifier_type == OverrideIdentifierType.IP:
            return self.matches_ip(identifier)

        return self.identifier == identifier

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TierOverride:
        """Create from dictionary."""
        expires_at = None
        if data.get("expires_at"):
            expires_at = datetime.fromisoformat(data["expires_at"])

        return cls(
            identifier=data["identifier"],
            identifier_type=OverrideIdentifierType(data["identifier_type"]),
            tier_id=data["tier_id"],
            reason=data.get("reason", ""),
            expires_at=expires_at,
        )
