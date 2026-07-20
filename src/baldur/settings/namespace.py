"""
Namespace Settings - Multi-Cluster Support.

Configures cluster/region/tenant namespaces from environment variables.

Usage:
    # Option 1: unified namespace
    BALDUR_NAMESPACE_NAMESPACE=seoul

    # Option 2: individual parts (priority: NAMESPACE > REGION > TENANT > ENV)
    BALDUR_NAMESPACE_REGION=seoul
    BALDUR_NAMESPACE_TENANT=customer123
    BALDUR_NAMESPACE_ENV=production

Dynamic namespace (X-Test-Mode support):
    - Production requests: baldur:*
    - Synthetic requests: xtest:baldur:* (when TestModeContext is active)
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.core.test_mode_context import TestModeContext
from baldur.settings.base import make_settings_config


class NamespaceSettings(BaseSettings):
    """
    Namespace settings.

    Separates Redis keys across multi-cluster/region/tenant deployments.
    """

    model_config = make_settings_config("BALDUR_NAMESPACE_")

    # Unified namespace (highest priority)
    namespace: str | None = Field(
        default=None,
        description="Unified namespace (highest priority)",
    )

    # Individual parts, in priority order
    region: str | None = Field(
        default=None,
        description="Region identifier (e.g., seoul, tokyo)",
    )
    tenant: str | None = Field(
        default=None,
        description="Tenant identifier for SaaS multi-tenancy",
    )
    env: str | None = Field(
        default=None,
        description="Environment (dev, staging, production)",
    )

    # Fallback when nothing is configured
    default_namespace: str = Field(
        default="default",
        description="Fallback namespace when nothing is set",
    )

    # Whether namespacing is active at all
    namespace_enabled: bool = Field(
        default=False,
        description="Enable namespace-based key prefixing",
    )

    def get_effective_namespace(self) -> str:
        """
        Return the effective namespace.

        Priority: namespace > region > tenant > env > default

        Returns:
            The effective namespace string. Empty string when disabled.
        """
        if not self.namespace_enabled:
            return ""  # Disabled: empty string (preserves existing behavior)

        return (
            self.namespace
            or self.region
            or self.tenant
            or self.env
            or self.default_namespace
        )

    def get_key_prefix(self, base_prefix: str = "baldur") -> str:
        """
        Build the Redis key prefix.

        Args:
            base_prefix: Base prefix.

        Returns:
            The full key prefix (e.g., "baldur:seoul:" or "baldur:").
        """
        ns = self.get_effective_namespace()
        if ns:
            return f"{base_prefix}:{ns}:"
        return f"{base_prefix}:"


# =============================================================================
# Synthetic Mode Key Prefix (X-Test-Mode support)
# =============================================================================

SYNTHETIC_KEY_PREFIX = "xtest"


def get_effective_key_prefix(base_prefix: str = "baldur") -> str:
    """
    Return the dynamic key prefix for the current context.

    When TestModeContext is active, an ``xtest:`` prefix is prepended so that
    synthetic data stays separated from production data.

    Args:
        base_prefix: Base prefix.

    Returns:
        The dynamic key prefix:
        - Production mode: "baldur:*" or "baldur:seoul:*"
        - Synthetic mode: "xtest:baldur:*" or "xtest:baldur:seoul:*"

    Example:
        # Production request
        prefix = get_effective_key_prefix()  # "baldur:"

        # X-Test-Mode request
        with TestModeContext.start():
            prefix = get_effective_key_prefix()  # "xtest:baldur:"
    """
    settings = get_namespace_settings()
    standard_prefix = settings.get_key_prefix(base_prefix)

    if TestModeContext.is_synthetic():
        return f"{SYNTHETIC_KEY_PREFIX}:{standard_prefix}"

    return standard_prefix


def get_namespace_settings() -> "NamespaceSettings":
    """Return the NamespaceSettings singleton."""
    from baldur.settings.root import get_config

    return get_config().multi_region.namespace


def reset_namespace_settings() -> None:
    """Reset the singleton (test helper)."""
    from baldur.settings.root import get_config

    try:
        del get_config().multi_region.__dict__["namespace"]
    except KeyError:
        pass


def get_key_prefix(base_prefix: str = "baldur") -> str:
    """
    Return the key prefix for the current namespace.

    Convenience wrapper, callable from anywhere.

    Args:
        base_prefix: Base prefix.

    Returns:
        The full key prefix.
    """
    return get_namespace_settings().get_key_prefix(base_prefix)
