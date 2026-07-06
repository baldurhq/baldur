"""
L2 Storage API Common Utilities.

Shared utility functions for L2 storage API views.
"""

import structlog

logger = structlog.get_logger()


def get_layered_repository():
    """Get LayeredCircuitBreakerStateRepository if available."""
    try:
        from baldur.factory import ProviderRegistry

        repo = ProviderRegistry.get_circuit_breaker_repo(name="layered")

        # Check if it's a LayeredRepository
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        if isinstance(repo, LayeredCircuitBreakerStateRepository):
            return repo
        return None
    except Exception as e:
        logger.warning(
            "l2_storage_api.get_layered_repository_failed",
            error=e,
        )
        return None


def get_shadow_logger():
    """Get ShadowLogger instance."""
    try:
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        return get_shadow_logger()
    except Exception as e:
        logger.warning(
            "l2_storage_api.get_shadow_logger_failed",
            error=e,
        )
        return None
