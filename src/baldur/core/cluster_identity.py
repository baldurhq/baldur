"""
Cluster Identity - Multi-Cluster SSOT.

Single source of truth through which each Pod knows its own cluster.

Background: the Redis manager only derived a pod ID from the ``HOSTNAME``
environment variable — it had no notion of cluster or region.

Usage:
    from baldur.core.cluster_identity import get_cluster_identity

    identity = get_cluster_identity()
    print(identity.cluster_id)    # "seoul-prod-01"
    print(identity.region)        # "seoul"
    print(identity.full_prefix)   # "baldur:seoul:"
"""

from __future__ import annotations

import structlog
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger()


class ClusterIdentity(BaseSettings):
    """
    Cluster identification (immutable BaseSettings).

    Automatic environment-variable parsing replaces manual os.environ.get()
    parsing (202 paradigm unification).

    Attributes:
        cluster_id: Unique cluster ID (required)
        region: Region identifier (e.g. seoul, tokyo)
        environment: Environment (dev, staging, prod)
        tenant: SaaS tenant ID (optional)
        pod_id: Current Pod ID
    """

    model_config = SettingsConfigDict(
        frozen=True,
        extra="ignore",
        validate_default=True,
        populate_by_name=True,
    )

    cluster_id: str = Field(
        default="default",
        validation_alias=AliasChoices(
            "BALDUR_CLUSTER_ID",
            "cluster_id",
        ),
    )
    region: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BALDUR_NAMESPACE_REGION",
            "region",
        ),
    )
    environment: str = Field(
        default="production",
        validation_alias=AliasChoices(
            "BALDUR_NAMESPACE_ENV",
            "environment",
        ),
    )
    tenant: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BALDUR_NAMESPACE_TENANT",
            "tenant",
        ),
    )
    pod_id: str = Field(
        default="unknown",
        validation_alias=AliasChoices(
            "HOSTNAME",
            "pod_id",
        ),
    )

    @property
    def namespace(self) -> str:
        """Return the Redis key namespace."""
        # Precedence: region > tenant > environment
        return self.region or self.tenant or self.environment

    @property
    def full_prefix(self) -> str:
        """Return the full Redis key prefix."""
        return f"baldur:{self.namespace}:"

    @property
    def trace_id_prefix(self) -> str:
        """Cluster prefix for trace IDs."""
        # Kept short: first 3 chars of region + first char of environment
        region_short = (self.region or "unk")[:3]
        env_short = self.environment[0] if self.environment else "u"
        return f"{region_short}{env_short}"

    def validate(self, fail_fast: bool = True) -> bool:  # type: ignore[override]
        """
        Validate cluster_id and region.

        Fail-Fast guarantees:
        - BALDUR_CLUSTER_ID missing → optional immediate process abort
        - BALDUR_NAMESPACE_REGION missing → required (Phase 1)
        - Prevents touching the wrong namespace at the source

        Args:
            fail_fast: True → sys.exit(1) on failure, False → return False.
                       Caller controls the policy explicitly (453 D3) — the
                       BALDUR_FAIL_FAST env-read previously inlined here is
                       now a bootstrap-side decision.

        Returns:
            True if valid; False if invalid AND fail_fast=False.

        Raises:
            SystemExit: fail_fast=True and validation failed.
        """
        import sys

        errors = []

        # 1. cluster_id validation
        if not self.cluster_id or self.cluster_id in ("unknown", "default"):
            errors.append(f"BALDUR_CLUSTER_ID not set or invalid: '{self.cluster_id}'")

        # 2. region validation (added in Phase 1 - required!)
        if not self.region:
            errors.append(
                "BALDUR_NAMESPACE_REGION not set. "
                "Cannot determine namespace - refusing to start."
            )

        # Handle validation failure
        if errors:
            error_msg = (
                "[FATAL] ClusterIdentity validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
                + "\n\nRefusing to start to prevent namespace collision."
            )

            if fail_fast:
                logger.critical(error_msg)
                sys.exit(1)  # Fail-Fast: terminate immediately
            else:
                logger.error(
                    "running.quarantine_mode",
                    error_msg=error_msg,
                )
                return False

        logger.info(
            "cluster_identity.validated",
            cluster_id=self.cluster_id,
            region=self.region,
            environment=self.environment,
            pod_id=self.pod_id,
        )
        return True


# =============================================================================
# Factory & Singleton
# =============================================================================


class _QuarantineState:
    """Runtime-scoped quarantine flag (450 Phase 4 / 453 D2).

    Promoted to its own keyed singleton triple in 453 D2 so the autodiscovered
    ``reset_quarantine_state()`` clears the flag between test modules
    independent of whether ``cluster_identity`` was instantiated. This closes
    the leak vector where ``set_quarantine_mode(True)`` was called without
    ever creating the ``cluster_identity`` singleton — previously the cleanup
    hook only fired when an instance existed.
    """

    __slots__ = ("enabled",)

    def __init__(self) -> None:
        self.enabled: bool = False


from baldur.utils.singleton import make_singleton_factory

_quarantine_state, _configure_quarantine_state, reset_quarantine_state = (
    make_singleton_factory("quarantine_state", _QuarantineState)
)


def _create_cluster_identity() -> ClusterIdentity:
    """Construct a ClusterIdentity (453 D5).

    Pure construction — no env reads, no global state mutation. Validation
    and quarantine-flip moved to ``baldur.bootstrap._validate_startup_config``
    where the caller controls timing (test_mode skip) and fail-policy.
    """
    return ClusterIdentity()


_get_identity, configure_cluster_identity, reset_cluster_identity = (
    make_singleton_factory("cluster_identity", _create_cluster_identity)
)


def get_cluster_identity(skip_validation: bool = False) -> ClusterIdentity:
    """Return the ClusterIdentity singleton."""
    if skip_validation is not False:
        import warnings

        warnings.warn(
            "skip_validation is deprecated and ignored. "
            "Set BALDUR_TEST_MODE=true to skip validation.",
            DeprecationWarning,
            stacklevel=2,
        )
    return _get_identity()


def is_quarantine_mode() -> bool:
    """Check if system is in Quarantine Mode."""
    return _quarantine_state().enabled


def set_quarantine_mode(enabled: bool) -> None:
    """Manually set Quarantine Mode (admin use)."""
    _quarantine_state().enabled = enabled
    if enabled:
        logger.warning("quarantine_mode.manually_enabled_administrator")
    else:
        logger.info("quarantine_mode.disabled_administrator")
