"""
Config-invalidation target registry.

A runtime configuration write reaches storage; whether a *running* process
picks it up is a separate fact, and one that has historically been asserted
rather than wired. This module is the single place that fact lives: a domain
becomes runtime-reloadable by registering an invalidation target for its
``config_type``, and every statement about runtime pickup is derived from what
is registered here rather than authored per domain.

Two axes, deliberately separate:

- **Registration** — the consuming service registered a callable that refreshes
  it. This says the domain *can* be refreshed in this process.
- **Armed** — the delivery mechanism that decides *when* to call those targets
  is running in this process. Registration without delivery still means running
  processes keep the old value.

Both are per process, so a deployment where the delivery never started reports
itself honestly instead of over-claiming.

Usage:
    from baldur.core.config_invalidation import (
        register_config_invalidation_target,
        invoke_config_invalidation_targets,
    )

    register_config_invalidation_target(
        "circuit_breaker", invalidate_circuit_breaker_config
    )
    ...
    invoke_config_invalidation_targets("circuit_breaker")
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import structlog

logger = structlog.get_logger()

__all__ = [
    "RuntimeApplyMode",
    "RuntimeApplyDeclaration",
    "register_config_invalidation_target",
    "get_config_invalidation_targets",
    "registered_config_invalidation_types",
    "invoke_config_invalidation_targets",
    "set_config_delivery_armed",
    "config_delivery_armed",
    "config_delivery_convergence_seconds",
    "describe_config_runtime_apply",
    "reset_config_invalidation_targets",
]


InvalidationTarget = Callable[[], object]

_targets: dict[str, list[InvalidationTarget]] = {}
_armed: dict[str, int | None] = {}
_registry_lock = threading.Lock()


# =============================================================================
# Registration
# =============================================================================


def register_config_invalidation_target(
    config_type: str, target: InvalidationTarget
) -> None:
    """Register a callable that refreshes ``config_type``'s consumers.

    The callable takes no arguments and is expected to rebuild whatever the
    domain's services read. Registration is membership-deduplicated, so a
    starter that runs twice (a framework's own init plus a worker post-fork
    hook) registers once.
    """
    with _registry_lock:
        targets = _targets.setdefault(config_type, [])
        if target in targets:
            return
        targets.append(target)

    logger.debug(
        "config_invalidation.target_registered",
        config_type=config_type,
    )


def get_config_invalidation_targets(config_type: str) -> list[InvalidationTarget]:
    """Return the targets registered for ``config_type`` (a copy, possibly empty)."""
    with _registry_lock:
        return list(_targets.get(config_type, ()))


def registered_config_invalidation_types() -> set[str]:
    """Return every ``config_type`` that has at least one registered target."""
    with _registry_lock:
        return {ct for ct, targets in _targets.items() if targets}


def invoke_config_invalidation_targets(config_type: str) -> int:
    """Run every target registered for ``config_type``; return how many succeeded.

    Failures are isolated: one raising target does not suppress the rest, and
    none of them propagates to the caller — which is a dispatcher thread or a
    background tick with no useful handler for a domain's refresh failure. The
    success count is the caller's signal that anything actually ran; ``0`` means
    either nothing is registered or every target failed, and the failures are
    logged individually.
    """
    succeeded = 0
    for target in get_config_invalidation_targets(config_type):
        try:
            target()
            succeeded += 1
        except Exception as e:
            logger.warning(
                "config_invalidation.target_failed",
                config_type=config_type,
                error=str(e),
            )
    return succeeded


# =============================================================================
# Delivery-armed marker
# =============================================================================


def set_config_delivery_armed(
    config_type: str,
    armed: bool,
    *,
    converges_within_seconds: int | None = None,
) -> None:
    """Record whether this process's delivery for ``config_type`` is running.

    Args:
        config_type: The configuration domain.
        armed: Whether delivery is running in this process.
        converges_within_seconds: The delivery mechanism's own stated upper
            bound on how long a stored change takes to reach this process's
            consumers. Reported verbatim; the marker never invents one.
    """
    with _registry_lock:
        if armed:
            _armed[config_type] = converges_within_seconds
        else:
            _armed.pop(config_type, None)


def config_delivery_armed(config_type: str) -> bool:
    """Whether delivery for ``config_type`` is running in this process."""
    with _registry_lock:
        return config_type in _armed


def config_delivery_convergence_seconds(config_type: str) -> int | None:
    """The armed delivery's stated convergence bound, if it declared one."""
    with _registry_lock:
        return _armed.get(config_type)


# =============================================================================
# Runtime-apply declaration
# =============================================================================


class RuntimeApplyMode(str, Enum):
    """What a stored configuration change does to already-running processes."""

    LIVE = "live"
    STORED_ONLY = "stored_only"
    UNVERIFIED = "unverified"


_MODE_DETAIL: dict[RuntimeApplyMode, str] = {
    RuntimeApplyMode.LIVE: (
        "Stored on write and delivered to this process's consumers."
    ),
    RuntimeApplyMode.STORED_ONLY: (
        "Stored on write; applies to new processes. Running processes keep the "
        "old value because delivery is not running here."
    ),
    RuntimeApplyMode.UNVERIFIED: (
        "Stored on write; runtime pickup by the consuming services is not "
        "verified for this domain. Running processes may keep the old value "
        "until they restart."
    ),
}


@dataclass(frozen=True)
class RuntimeApplyDeclaration:
    """What an operator is told about a configuration change taking effect."""

    mode: RuntimeApplyMode
    converges_within_seconds: int | None
    detail: str

    def to_dict(self) -> dict[str, object]:
        """Render for an API response."""
        return {
            "mode": self.mode.value,
            "converges_within_seconds": self.converges_within_seconds,
            "detail": self.detail,
        }


def describe_config_runtime_apply(config_type: str) -> RuntimeApplyDeclaration:
    """Derive ``config_type``'s runtime-apply declaration from the wiring.

    The declaration is never authored per domain — it reads the registry and
    the armed marker, so it cannot drift from what the process actually does
    and it degrades honestly on a deployment where the delivery starter never
    ran.
    """
    if not get_config_invalidation_targets(config_type):
        mode = RuntimeApplyMode.UNVERIFIED
        converges = None
    elif not config_delivery_armed(config_type):
        mode = RuntimeApplyMode.STORED_ONLY
        converges = None
    else:
        mode = RuntimeApplyMode.LIVE
        converges = config_delivery_convergence_seconds(config_type)

    return RuntimeApplyDeclaration(
        mode=mode,
        converges_within_seconds=converges,
        detail=_MODE_DETAIL[mode],
    )


def reset_config_invalidation_targets() -> None:
    """Drop every registered target and armed marker — test isolation only."""
    with _registry_lock:
        _targets.clear()
        _armed.clear()
