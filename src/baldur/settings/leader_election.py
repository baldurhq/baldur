"""
Leader Election Settings - Pydantic v2.

Leader election settings for cluster coordination.
Supports Redis/K8s based distributed leader election.

Moved from: coordination/config.py (location consolidation)
"""

from __future__ import annotations

import os
import socket
from typing import Literal

import structlog
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import JitterFactor

logger = structlog.get_logger()


class LeaderElectionSettings(BaseSettings):
    """
    Leader Election settings.

    Env prefix: BALDUR_LEADER_ELECTION_
    """

    model_config = make_settings_config("BALDUR_LEADER_ELECTION_")

    # Activation
    enabled: bool = Field(
        default=False,
        description="Leader Election activation toggle",
    )

    # Backend
    backend: Literal["redis", "kubernetes"] = Field(
        default="redis",
        description="Leader election backend (redis or kubernetes)",
    )

    # Node identifier
    node_id: str = Field(
        default="",
        description="Node unique ID (uses hostname if empty)",
    )

    # Lease settings
    lease_ttl_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Leadership Lease TTL (seconds)",
    )

    renew_interval_seconds: float | None = Field(
        default=None,
        ge=1.0,
        description="Lease renewal interval (seconds). None for auto-calculation",
    )

    # Safe Margin
    lease_safety_margin_ratio: float = Field(
        default=0.1,
        ge=0.05,
        le=0.3,
        description="Lease renewal safety margin (ratio of TTL)",
    )

    # Retry settings
    retry_interval_seconds: float = Field(
        default=5.0,
        ge=1.0,
        description="Retry interval on election failure (seconds)",
    )

    retry_jitter_factor: JitterFactor = Field(
        default=0.5,
        description="Retry interval jitter ratio (Thundering Herd prevention)",
    )

    max_retry_attempts: int = Field(
        default=3,
        ge=0,
        description="Max consecutive failure count (0=unlimited)",
    )

    # Self-Fencing
    self_fencing_enabled: bool = Field(
        default=False,
        description="Immediately relinquish leadership on lease renewal failure",
    )

    # Region priority
    region_priority: int = Field(
        default=100,
        ge=0,
        le=1000,
        description="Region priority (lower = higher priority)",
    )

    # Redis settings
    redis_url: str = Field(
        default="",
        description="Redis URL. Empty falls back to BALDUR_REDIS_URL (RedisSettings.url).",
    )

    redis_key_prefix: str = Field(
        default="baldur:leader:",
        description="Redis key prefix",
    )

    # K8s settings
    k8s_namespace: str = Field(
        default="default",
        description="K8s Lease resource namespace",
    )

    k8s_in_cluster: bool = Field(
        default=True,
        description="Use K8s in-cluster config (False uses kubeconfig)",
    )

    @model_validator(mode="after")
    def _fallback_redis_url(self) -> LeaderElectionSettings:
        """Resolve redis_url with BALDUR_REDIS_URL fallback.

        Resolution order (highest first):
          1. BALDUR_LEADER_ELECTION_REDIS_URL (per-feature override) — wins if set.
          2. BALDUR_REDIS_URL via RedisSettings.url (project-wide convention).
          3. RedisSettings default (redis://localhost:6379/0).

        Emits a single INFO log so operators see which env var resolved
        the elector's host without case-by-case asymmetry.

        Pattern source: settings/system_control.py::_fallback_redis_url.
        """
        if "redis_url" in self.model_fields_set:
            source = "BALDUR_LEADER_ELECTION_REDIS_URL"
        else:
            try:
                from baldur.settings.redis import get_redis_settings

                self.redis_url = get_redis_settings().url
            except Exception:
                pass
            source = (
                "BALDUR_REDIS_URL" if os.environ.get("BALDUR_REDIS_URL") else "default"
            )

        logger.info(
            "leader_election.redis_url_resolved",
            source=source,
            redis_url=self.redis_url,
        )
        return self

    @model_validator(mode="after")
    def validate_timing_constraints(self) -> LeaderElectionSettings:
        """Validate timing constraints."""
        effective_interval = self.get_effective_renew_interval()

        # renew_interval must be < lease_ttl/2 (ensure at least 2 renewal opportunities)
        max_allowed = self.lease_ttl_seconds / 2
        if effective_interval >= max_allowed:
            raise ValueError(
                f"renew_interval ({effective_interval}s) must be < "
                f"lease_ttl/2 ({max_allowed}s) for safe renewal"
            )

        # Check recommended range (lease_ttl/4 ~ lease_ttl/3)
        recommended_min = self.lease_ttl_seconds / 4
        recommended_max = self.lease_ttl_seconds / 3
        if not (recommended_min <= effective_interval <= recommended_max):
            logger.warning(
                "outside.recommended_range",
                effective_interval=effective_interval,
                recommended_min=recommended_min,
                recommended_max=recommended_max,
            )

        return self

    def get_node_id(self) -> str:
        """Return node ID (configured value or hostname)."""
        if self.node_id:
            return self.node_id

        # Kubernetes Pod name first
        pod_name = os.environ.get("HOSTNAME", "")
        if pod_name:
            return pod_name

        return socket.gethostname()

    def get_effective_renew_interval(self) -> float:
        """
        Return the effective renewal interval.

        Uses user-specified value if set, otherwise auto-calculates:
        - Default: lease_ttl/3 - safety_margin
        """
        if self.renew_interval_seconds is not None:
            return self.renew_interval_seconds

        # Auto-calculate: TTL/3 minus safety margin
        base = self.lease_ttl_seconds / 3
        margin = self.lease_ttl_seconds * self.lease_safety_margin_ratio
        return max(base - margin, 1.0)


def get_leader_election_settings() -> LeaderElectionSettings:
    from baldur.settings.root import get_config

    return get_config().coordination.leader_election


def reset_leader_election_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().coordination.__dict__["leader_election"]
    except KeyError:
        pass
