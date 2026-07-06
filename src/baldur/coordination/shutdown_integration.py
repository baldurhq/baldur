"""
Leader Elector graceful-shutdown integration.

Releases leadership safely when the process shuts down. Coverage map:

- ``init()`` deployments: the bootstrap-registered
  ``LeaderElectorShutdownHandler`` (see
  ``integrate_with_shutdown_coordinator``) stops all electors when the
  coordinator drain starts.
- Polite exits (normal return, ``sys.exit``): the atexit hook registered
  on first elector registration.
- Ownerless death (SIGKILL, OOM, scripts that never call ``init()``):
  bounded by lease TTL expiry — the elector's designed handover path.

This module installs NO OS signal handlers. A non-chaining handler here
was the last registrant in the default init order, so it swallowed
SIGTERM/SIGINT for the whole process — suppressing both the coordinator
drain and the host server's own shutdown.
"""
# Signal-handler removal rationale: 597 D6 (G5 clobber cascade).

from __future__ import annotations

import atexit
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from baldur.coordination.base import LeaderElector
    from baldur.core.shutdown_coordinator import ShutdownHandler

logger = structlog.get_logger()


class _ShutdownIntegrationState:
    """Runtime-scoped graceful-shutdown registry (450 Phase 4).

    Owns the list of registered LeaderElectors plus the once-guard for the
    process-wide atexit registration. Lives on the active
    ``BaldurRuntime`` so resetting the runtime drops the registry atomically.
    """

    __slots__ = ("atexit_registered", "registered_electors")

    def __init__(self) -> None:
        self.registered_electors: list[LeaderElector] = []
        self.atexit_registered: bool = False


def _shutdown_state() -> _ShutdownIntegrationState:
    from baldur.runtime import get_runtime

    return get_runtime().get_singleton(
        "shutdown_integration_state", _ShutdownIntegrationState
    )


def reset_registered_electors() -> None:
    """Reset registered electors for test isolation."""
    state = _shutdown_state()
    state.registered_electors.clear()
    state.atexit_registered = False


def register_for_graceful_shutdown(elector: LeaderElector) -> None:
    """
    Register a LeaderElector for shutdown-time stop coverage.

    Adds the elector to the runtime-scoped registry and (once per
    runtime) registers an atexit hook that stops every registered
    elector at interpreter exit. Installs NO OS signal handler — in
    ``init()`` deployments the coordinator's bootstrap-wired shutdown
    handler stops electors at drain start, and a process killed before
    atexit can run is bounded by lease TTL expiry.

    Args:
        elector: LeaderElector instance to register.

    Usage:
        elector = get_leader_elector("dlq-consumer")
        register_for_graceful_shutdown(elector)
        elector.start()
    """
    state = _shutdown_state()

    if elector not in state.registered_electors:
        state.registered_electors.append(elector)
        logger.debug(
            "leader_elector.graceful_shutdown_registered",
            elector=elector.resource_name,
        )

    # Once per runtime: atexit only. The former OS signal handler here was
    # deleted per 597 D6 — it replaced (did not chain) SIGTERM/SIGINT and,
    # being the last registrant in the default init order, swallowed the
    # signal for the whole process.
    if not state.atexit_registered:
        atexit.register(shutdown_all_electors)
        state.atexit_registered = True


def unregister_from_graceful_shutdown(elector: LeaderElector) -> None:
    """
    Unregister a LeaderElector from graceful shutdown.

    Args:
        elector: LeaderElector instance to unregister.
    """
    state = _shutdown_state()

    if elector in state.registered_electors:
        state.registered_electors.remove(elector)
        logger.debug(
            "leader_elector.graceful_shutdown_unregistered",
            elector=elector.resource_name,
        )


def shutdown_all_electors() -> None:
    """Stop all registered electors."""
    state = _shutdown_state()

    for elector in list(state.registered_electors):
        try:
            logger.info(
                "leader_elector.stopping",
                elector=elector.resource_name,
            )
            elector.stop()
            logger.info(
                "leader_elector.stopped",
                elector=elector.resource_name,
            )
        except Exception as e:
            logger.exception(
                "leader_elector.stop_failed",
                elector=elector.resource_name,
                error=e,
            )

    state.registered_electors.clear()


def integrate_with_shutdown_coordinator() -> ShutdownHandler | None:
    """
    Integrate with GracefulShutdownCoordinator.

    Reuses the existing graceful-shutdown infrastructure: bootstrap
    registers the returned handler with the coordinator, which stops all
    electors when the drain starts.
    """
    try:
        from baldur.core.shutdown_coordinator import (
            ShutdownHandler,
        )

        class LeaderElectorShutdownHandler(ShutdownHandler):
            """Shutdown handler that releases leadership."""

            def on_shutdown_start(self) -> None:
                """Release leadership when the shutdown starts."""
                logger.info("leader_elector.shutdown_started_releasing_leadership")
                shutdown_all_electors()

            def on_drain_complete(self) -> None:
                """Nothing further to do once the drain completes."""
                pass

            def on_force_shutdown(self, pending_requests) -> None:
                """Release leadership on forced shutdown."""
                shutdown_all_electors()

        logger.info("leader_elector.graceful_shutdown_coordinator_integration_ready")
        return LeaderElectorShutdownHandler()

    except ImportError:
        logger.debug("leader_elector.graceful_shutdown_coordinator_not_found")
        return None
