"""
Coordination package — Global Leader Election.

Module for single-leader election in distributed environments.

Components:
- LeaderElector: Leader election interface
- NoOpLeaderElector: OSS never-leader default
- LeaderElectionSettings: Configuration
- DLQConsumerCoordinator: DLQ processing coordination

Concrete elector implementations live in the private distribution:
the Redis implementation moved to ``baldur_pro.coordination.redis_elector``
per doc 599 D2/D4, and the K8s Lease implementation lives in
``baldur_dormant.coordination.kubernetes_elector`` per doc 528 D10-v2 /
D16. Both are resolved through ``ProviderRegistry.leader_elector`` by
``get_leader_elector()``; with no provider registered the factory degrades
to ``NoOpLeaderElector`` (never-leader).

Usage::

    from baldur.coordination import get_leader_elector

    elector = get_leader_elector("dlq-consumer")

    @elector.on_become_leader
    def start_processing():
        print("Became leader!")

    @elector.on_lose_leader
    def stop_processing():
        print("Lost leadership")

    elector.start()
    # ...
    elector.stop()

Status: Internal
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baldur.coordination.base import (
    LeaderCallback,
    LeaderElector,
    LeaderInfo,
    LeadershipState,
)
from baldur.coordination.config import (
    LeaderElectionSettings,
    get_leader_election_settings,
    reset_leader_election_settings,
)
from baldur.coordination.dlq_consumer import DLQConsumerCoordinator
from baldur.coordination.factory import (
    get_leader_elector,
    reset_leader_electors,
)
from baldur.coordination.local_file_elector import LocalFileLeaderElector
from baldur.coordination.noop_elector import NoOpLeaderElector
from baldur.coordination.shutdown_integration import (
    integrate_with_shutdown_coordinator,
    register_for_graceful_shutdown,
    unregister_from_graceful_shutdown,
)

if TYPE_CHECKING:
    from baldur.coordination.scheduler import (
        LeaderScheduler as LeaderScheduler,
    )
    from baldur.coordination.scheduler import (
        ScheduledJob as ScheduledJob,
    )

_DORMANT_SCHEDULER = {"LeaderScheduler", "ScheduledJob"}


def __getattr__(name: str):
    """Lazy import for Dormant-tier coordination modules (PEP 562)."""
    if name in _DORMANT_SCHEDULER:
        try:
            from baldur.coordination import scheduler as _mod

            return getattr(_mod, name)
        except ImportError as e:
            raise AttributeError(f"Cannot import {name} (Dormant tier): {e}") from e
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Base interfaces
    "LeaderElector",
    "LeaderCallback",
    "LeaderInfo",
    "LeadershipState",
    # Config
    "LeaderElectionSettings",
    "get_leader_election_settings",
    "reset_leader_election_settings",
    # Factory
    "get_leader_elector",
    "reset_leader_electors",
    # OSS NoOp default (528 D10-v2 — registered as ProviderRegistry.leader_elector default)
    "NoOpLeaderElector",
    # OSS single-host file-lock elector (inline scheduler default)
    "LocalFileLeaderElector",
    # DLQ Consumer
    "DLQConsumerCoordinator",
    # Scheduler (Dormant — lazy via __getattr__)
    "LeaderScheduler",
    "ScheduledJob",
    # Graceful Shutdown
    "register_for_graceful_shutdown",
    "unregister_from_graceful_shutdown",
    "integrate_with_shutdown_coordinator",
]
