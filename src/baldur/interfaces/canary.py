"""Canary Rollout Service Interface (519 PR 2 / D-c1).

OSS-side Protocol for the PRO CanaryRolloutService singleton. PRO ships
the realized backend behind ``baldur_pro.services.canary``; OSS callers
resolve via ``ProviderRegistry.canary_rollout_service.safe_get()`` and
use the returned instance with a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared. Distinct from
:class:`baldur.interfaces.canary_rollout_store.CanaryRolloutStore` —
this Protocol is the orchestration singleton; that Interface is the
persistence layer.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["CanaryRollout", "CanaryRolloutService"]


@runtime_checkable
class CanaryRolloutService(Protocol):
    """Protocol for the PRO canary rollout orchestration singleton."""

    def collect_metrics(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_active_rollouts(self) -> list[Any]: ...

    def create_rollout(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_rollout(self, rollout_id: str) -> Any: ...

    def renew_config_lock(self, rollout: Any) -> Any: ...


# OSS consumer of this Protocol: tasks/canary_watchdog.py.
@runtime_checkable
class CanaryRollout(Protocol):
    """Protocol for an individual PRO canary rollout instance.

    Used as a TYPE_CHECKING-only annotation by OSS task wiring that
    holds a rollout reference but does not construct one — construction
    stays inside PRO via ``CanaryRolloutService``.
    """

    id: str
    state: Any
    """Underlying ``CanaryState`` value — exposed as ``Any`` so OSS
    callers can compare with :class:`baldur.models.canary.CanaryState`
    without coupling to the PRO concrete attribute type."""

    config_type: str
    created_by: str
    created_at: Any
    """``datetime`` at PRO impl; ``Any`` here to keep this OSS Protocol
    free of a datetime import constraint."""

    paused_at: Any
    """``datetime | None`` at PRO impl — when the rollout entered PAUSED.
    Watchdog anchor for the PAUSED stall clock (created_at fallback)."""

    stage_started_at: Any
    """``datetime | None`` at PRO impl — when the current stage's config
    was applied. Watchdog anchor for the per-stage observation window and
    the CANARY stall clock (created_at fallback for legacy rollouts)."""

    @property
    def current_stage(self) -> Any: ...

    """PRO ``CanaryStage | None``; ``Any`` so OSS callers can read
    ``.duration_minutes`` without importing the PRO stage type."""

    @property
    def is_terminal(self) -> bool: ...

    @property
    def affected_clusters(self) -> list[str]: ...
