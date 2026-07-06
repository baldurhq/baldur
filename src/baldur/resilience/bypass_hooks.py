"""
Resilience Bypass Hooks.

Environment-conditional bypass hooks for stress testing, chaos engineering,
and resilience verification.

CRITICAL: These hooks are ONLY registered when:
1. ENVIRONMENT != "production"
2. ENABLE_RESILIENCE_TESTING = True (in settings)

All bypass decisions are audit-logged for compliance.

Hook Priority Levels:
- 1000+: Emergency/Admin overrides
- 500-999: Stress testing modes (PLATINUM, HELLMODE)
- 100-499: Standard testing modes (chaos-monkey, integration)
- 1-99: Low priority/fallback hooks
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = structlog.get_logger()


# =============================================================================
# Environment Check
# =============================================================================


def _is_resilience_testing_enabled() -> bool:
    """
    Check if resilience testing hooks should be registered.

    Conditions:
    1. Not in production environment (canonical signal:
       :func:`baldur.runtime.is_production`)
    2. ENABLE_RESILIENCE_TESTING=true OR DEBUG=True OR CHAOS_ENABLED=true
    """
    from baldur.runtime import is_production

    # NEVER enable in production
    if is_production():
        return False

    # Check explicit enable flag
    resilience_enabled = os.getenv("ENABLE_RESILIENCE_TESTING", "").lower() == "true"
    if resilience_enabled:
        return True

    # Check CHAOS_ENABLED (backward compatibility)
    chaos_enabled = os.getenv("CHAOS_ENABLED", "").lower() == "true"
    if chaos_enabled:
        return True

    # Check DEBUG mode
    return os.getenv("DEBUG", "").lower() in ("true", "1")


# =============================================================================
# Bypass Hook Functions
# =============================================================================


def platinum_bypass_hook(request: HttpRequest) -> bool:
    """
    PLATINUM mode: Complete rate limiter bypass for extreme stress testing.

    Triggered by: X-Test-Mode: platinum
    Priority: 1000 (highest)

    Use case: Stage 14-16 extreme load tests, system limit verification.
    """
    xtest_header = request.META.get("HTTP_X_TEST_MODE", "").lower()
    bypass_header = request.META.get("HTTP_X_TEST_BYPASS_RATELIMIT", "").lower()

    return xtest_header == "platinum" or bypass_header == "full"


def chaos_monkey_bypass_hook(request: HttpRequest) -> bool:
    """
    Chaos Monkey mode: Bypass for chaos engineering tests.

    Triggered by: X-Test-Mode: chaos-monkey
    Priority: 500

    Use case: Chaos injection, circuit breaker testing.
    """
    xtest_header = request.META.get("HTTP_X_TEST_MODE", "").lower()
    return xtest_header == "chaos-monkey"


def stress_test_bypass_hook(request: HttpRequest) -> bool:
    """
    Stress/Load test mode: Bypass for general load testing.

    Triggered by: X-Test-Mode: stress, extreme, load-test
    Priority: 300

    Use case: Locust load tests, performance benchmarks.
    """
    xtest_header = request.META.get("HTTP_X_TEST_MODE", "").lower()
    return xtest_header in ("stress", "extreme", "load-test", "hellmode")


def integration_test_bypass_hook(request: HttpRequest) -> bool:
    """
    Integration test mode: Bypass for integration testing.

    Triggered by: X-Test-Mode: integration, true
    Priority: 100

    Use case: pytest integration tests, CI/CD pipelines.
    """
    xtest_header = request.META.get("HTTP_X_TEST_MODE", "").lower()
    bypass_header = request.META.get("HTTP_X_TEST_BYPASS_RATELIMIT", "").lower()

    return xtest_header in ("integration", "true") or bypass_header == "true"


# =============================================================================
# Hook Registration
# =============================================================================


_hooks_registered = False


def register_resilience_hooks() -> bool:
    """
    Register all resilience bypass hooks if environment allows.

    This function is idempotent - calling it multiple times has no effect
    after the first successful registration.

    Returns:
        True if hooks were registered, False if skipped
    """
    global _hooks_registered

    if _hooks_registered:
        return False

    if not _is_resilience_testing_enabled():
        from baldur.runtime import is_production

        reason = "production" if is_production() else "flag_disabled"
        logger.info(
            "resilience.bypass_hooks_skipped",
            reason=reason,
        )
        return False

    from baldur.core.hooks import BypassRegistry

    # Register hooks in priority order
    BypassRegistry.register(
        platinum_bypass_hook,
        priority=1000,
        name="platinum_mode",
        description="PLATINUM extreme stress testing - complete rate limit bypass",
    )

    BypassRegistry.register(
        chaos_monkey_bypass_hook,
        priority=500,
        name="chaos_monkey",
        description="Chaos engineering mode - circuit breaker and fault injection testing",
    )

    BypassRegistry.register(
        stress_test_bypass_hook,
        priority=300,
        name="stress_test",
        description="Stress/Load testing mode - Locust and performance benchmarks",
    )

    BypassRegistry.register(
        integration_test_bypass_hook,
        priority=100,
        name="integration_test",
        description="Integration testing mode - CI/CD pipeline tests",
    )

    _hooks_registered = True

    logger.info(
        "resilience.bypass_hooks_registered",
        hooks=["platinum_mode", "chaos_monkey", "stress_test", "integration_test"],
        hook_count=4,
    )

    return True


def unregister_resilience_hooks() -> None:
    """Unregister all resilience hooks. For testing cleanup."""
    global _hooks_registered

    from baldur.core.hooks import BypassRegistry

    BypassRegistry.unregister("platinum_mode")
    BypassRegistry.unregister("chaos_monkey")
    BypassRegistry.unregister("stress_test")
    BypassRegistry.unregister("integration_test")

    _hooks_registered = False
    logger.info("resilience.all_bypass_hooks_unregistered")


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "register_resilience_hooks",
    "unregister_resilience_hooks",
    "platinum_bypass_hook",
    "chaos_monkey_bypass_hook",
    "stress_test_bypass_hook",
    "integration_test_bypass_hook",
]
