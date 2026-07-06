"""
Django tiering package — re-exports the framework-free tiering core from its
new home (``baldur.scaling.tiering``) plus the Django-coupled
``TieringMiddleware`` which stays here.

The tiering core (enums/models/defaults/circuit_breaker/validator/registry)
was relocated to ``baldur.scaling.tiering`` so a Django-less Flask/FastAPI app
can import it without triggering the ``api/django`` DRF/Django import chain.
This package keeps the historical ``baldur.api.django.tiering`` import surface
working by re-exporting from the new home.

Tier Hierarchy:
- Tier 1 (Critical): Baldur actions, payment APIs - 50% allowed in emergency
- Tier 2 (Standard): Config changes, DLQ replay - 10% allowed in emergency
- Tier 3 (Non-Essential): Dashboard, metrics - Blocked in emergency
"""

from __future__ import annotations

# Framework-free tiering core (relocated to baldur.scaling.tiering).
from baldur.scaling.tiering import (
    DEFAULT_TIER_DEFINITIONS,
    DEFAULT_TIER_MAPPINGS,
    DEFAULT_TIER_OVERRIDES,
    STATIC_CRITICAL_PATHS,
    STATIC_CRITICAL_PREFIXES,
    OverrideIdentifierType,
    TierConfigValidator,
    TierDefinition,
    TierFallbackReason,
    TieringCircuitBreaker,
    TierMapping,
    TierMatchType,
    TierOverride,
    TierRegistry,
    TierResult,
    TierValidationResult,
    get_tier_registry,
    get_tiering_circuit_breaker,
)

# Django-coupled middleware stays here.
from .middleware import TieringMiddleware

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
    # Circuit Breaker
    "TieringCircuitBreaker",
    "get_tiering_circuit_breaker",
    # Validator
    "TierValidationResult",
    "TierConfigValidator",
    # Registry
    "TierRegistry",
    "get_tier_registry",
    # Middleware
    "TieringMiddleware",
]
