"""
Framework-free API Tiering core for Criticality-Based Load Shedding.

Provides tier-based classification where different APIs get different
treatment during emergency / backpressure mode. Critical APIs get priority
access while non-essential APIs are shed first.

Tier Hierarchy:
- Tier 1 (Critical): Baldur actions, payment APIs - 50% allowed in emergency
- Tier 2 (Standard): Config changes, DLQ replay - 10% allowed in emergency
- Tier 3 (Non-Essential): Dashboard, metrics - Blocked in emergency

This package holds the Django-free tiering core (relocated out of
``api/django/tiering/`` so a Django-less Flask/FastAPI app can import it):
- enums.py: TierFallbackReason, PatternType, OverrideIdentifierType
- models.py: TierResult, TierDefinition, TierMapping, TierOverride
- defaults.py: Default tier templates, static critical paths, backpressure rules
- circuit_breaker.py: TieringCircuitBreaker
- validator.py: TierValidationResult, TierConfigValidator
- registry.py: TierRegistry

The Django-coupled ``TieringMiddleware`` stays in
``baldur.api.django.tiering.middleware`` and imports this core.
"""

from __future__ import annotations

# Circuit Breaker
from .circuit_breaker import (
    TieringCircuitBreaker,
    get_tiering_circuit_breaker,
)

# Defaults
from .defaults import (
    BACKPRESSURE_TIER_RULES,
    DEFAULT_TIER_DEFINITIONS,
    DEFAULT_TIER_MAPPINGS,
    DEFAULT_TIER_OVERRIDES,
    STATIC_CRITICAL_PATHS,
    STATIC_CRITICAL_PREFIXES,
)

# Enums
from .enums import (
    OverrideIdentifierType,
    TierFallbackReason,
    TierMatchType,
)

# Models
from .models import (
    TierDefinition,
    TierMapping,
    TierOverride,
    TierResult,
)

# Registry
from .registry import (
    TierRegistry,
    get_tier_registry,
)

# Validator
from .validator import (
    TierConfigValidator,
    TierValidationResult,
)

__all__ = [
    # Enums
    "TierFallbackReason",
    "TierMatchType",
    "OverrideIdentifierType",
    # Models
    "TierResult",
    "TierDefinition",
    "TierMapping",
    "TierOverride",
    # Defaults
    "STATIC_CRITICAL_PATHS",
    "STATIC_CRITICAL_PREFIXES",
    "DEFAULT_TIER_DEFINITIONS",
    "DEFAULT_TIER_MAPPINGS",
    "DEFAULT_TIER_OVERRIDES",
    "BACKPRESSURE_TIER_RULES",
    # Circuit Breaker
    "TieringCircuitBreaker",
    "get_tiering_circuit_breaker",
    # Validator
    "TierValidationResult",
    "TierConfigValidator",
    # Registry
    "TierRegistry",
    "get_tier_registry",
]
