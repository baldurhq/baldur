"""
Resilience Testing Module.

Production-grade resilience verification engine for continuous
fault injection and baldur validation.

This module provides:
- Bypass hooks for stress/load testing
- Chaos monkey integration
- Circuit breaker test utilities

Note: All hooks are conditionally registered based on environment settings.
Production environment blocks all extreme testing modes.

Status: Internal
"""

from baldur.resilience.bypass_hooks import register_resilience_hooks

# Auto-register hooks on module import (if enabled)
register_resilience_hooks()

__all__ = [
    "register_resilience_hooks",
]
