"""
Blast Radius Domain Types.

Shared blast radius level enum used by circuit breaker integration,
chaos engineering, and blast radius analysis services.

Keeping these in models/ breaks the OSS → PRO tier dependency
(same pattern as models/emergency.py, models/governance.py).
"""

from __future__ import annotations

from enum import Enum


class BlastRadiusLevel(str, Enum):
    """Blast radius impact level."""

    MINIMAL = "minimal"
    """Single instance/pod. Lowest risk."""

    CONTAINED = "contained"
    """Single service. Isolated impact."""

    MODERATE = "moderate"
    """2-3 services affected. Caution required."""

    EXTENSIVE = "extensive"
    """4+ services affected. Approval required."""

    CRITICAL = "critical"
    """Critical service included. Senior approval required."""
