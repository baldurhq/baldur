"""
Leader Election interface.

Abstract interface for electing a single leader in distributed
environments. Provides leadership state management, callback
registration, and leader-info queries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

import structlog

logger = structlog.get_logger()


class LeadershipState(str, Enum):
    """Leadership state."""

    NOT_STARTED = "not_started"
    """Before the election process starts."""

    FOLLOWER = "follower"
    """Follower state (not the leader)."""

    LEADER = "leader"
    """Leader state."""

    STOPPING = "stopping"
    """Shutdown in progress."""

    STOPPED = "stopped"
    """Shutdown complete."""


@dataclass
class LeaderInfo:
    """Current leader information."""

    node_id: str
    """Leader node ID."""

    elected_at: datetime
    """Election timestamp."""

    lease_expires_at: datetime
    """Lease expiry timestamp."""

    fencing_token: int = 0
    """Fencing token (monotonically increasing, split-brain prevention)."""

    region_priority: int = 100
    """Region priority."""

    is_self: bool = False
    """Whether this node is the leader."""


class LeaderCallback(Protocol):
    """Leadership change callback protocol."""

    def on_become_leader(self) -> None:
        """Called when this node becomes the leader."""
        ...

    def on_lose_leader(self) -> None:
        """Called when this node loses leadership."""
        ...


class LeaderElector(ABC):
    """
    Leader Elector abstract interface.

    Base interface for leader election and leadership maintenance.

    Usage:
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
    """

    @property
    @abstractmethod
    def resource_name(self) -> str:
        """Resource name (leadership target identifier)."""
        pass

    @property
    @abstractmethod
    def state(self) -> LeadershipState:
        """Current leadership state."""
        pass

    @abstractmethod
    def is_leader(self) -> bool:
        """Check whether this node is currently the leader."""
        pass

    @abstractmethod
    def get_leader(self) -> LeaderInfo | None:
        """Query the current leader info."""
        pass

    @abstractmethod
    def get_fencing_token(self) -> int:
        """
        Return the current fencing token.

        Pass this token along when writing to external systems to
        prevent stale-leader writes.
        """
        pass

    @abstractmethod
    def is_lease_valid(self) -> bool:
        """
        Check whether the current lease is valid (self-fencing).

        Call in the middle of long-running work to detect a stale leader.
        """
        pass

    @abstractmethod
    def start(self) -> None:
        """Start the leader-election process."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop the leader-election process (release leadership)."""
        pass

    @abstractmethod
    def on_become_leader(self, callback: Callable[[], None]) -> Callable[[], None]:
        """
        Register a callback fired on becoming leader.

        Usable as a decorator:
            @elector.on_become_leader
            def handle_become_leader():
                pass
        """
        pass

    @abstractmethod
    def on_lose_leader(self, callback: Callable[[], None]) -> Callable[[], None]:
        """
        Register a callback fired on losing leadership.

        Usable as a decorator:
            @elector.on_lose_leader
            def handle_lose_leader():
                pass
        """
        pass
