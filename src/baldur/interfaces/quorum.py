"""
Quorum Witness Protocol.

Distributed lock interface for preventing multi-region split-brain.
Can be implemented with various backends such as DynamoDB, Redis, Kubernetes, etc.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["QuorumLease", "QuorumWitnessProtocol"]


@dataclass
class QuorumLease:
    """
    Quorum lease information.

    The region holding the lease acts as the Primary for the duration of the lease.

    Attributes:
        region: Region that holds the lease
        acquired_at: Acquisition time (Unix timestamp)
        expires_at: Expiry time (Unix timestamp)
        lease_id: Lease ID (used for verification on re-acquisition)
    """

    region: str
    """Region that holds the lease."""

    acquired_at: float
    """Acquisition time (Unix timestamp)."""

    expires_at: float
    """Expiry time (Unix timestamp)."""

    lease_id: str
    """Lease ID (used for verification on re-acquisition)."""

    def is_valid(self) -> bool:
        """Check whether the lease is still valid."""
        return time.time() < self.expires_at


@runtime_checkable
class QuorumWitnessProtocol(Protocol):
    """
    Quorum Witness protocol.

    Prevents split-brain during Primary election in a multi-region environment.
    All implementations must provide the following guarantees:

    1. At most one region can be Primary at any given time (mutual exclusion)
    2. Leases expire automatically based on TTL (liveness)
    3. Only the lease holder can renew or release the lease (fencing)
    """

    def try_acquire_primary(self) -> bool:
        """
        Attempt to acquire the Primary lock.

        If this region is already Primary, renews the lease and returns True (idempotent).
        On renewal failure, resets local state and attempts a new CAS operation.

        Returns:
            True if acquisition succeeded (this region is Primary)
        """
        ...

    def renew_lease(self) -> bool:
        """
        Renew the lease.

        Returns:
            True if renewal succeeded
        """
        ...

    def release_lease(self) -> None:
        """Release the lease."""
        ...

    def get_current_primary(self) -> str | None:
        """
        Query the current Primary region.

        Returns:
            Name of the Primary region, or None
        """
        ...

    def is_primary(self) -> bool:
        """
        Check whether the current region is Primary (local check, hot path).

        Applies safety_margin to return False earlier than the actual expiry.
        Use is_primary_verified() for critical decisions.

        Returns:
            True if Primary
        """
        ...

    def is_primary_verified(self) -> bool:
        """
        Primary check with server-side cross-validation (cold path).

        Queries the backend server to verify that the lease still exists.
        Involves a network call, so do not use on the hot path.

        Returns:
            True if the lease is also valid on the server
        """
        ...

    def get_lease(self) -> QuorumLease | None:
        """Return the current lease."""
        ...

    def start_auto_renew(self) -> None:
        """Start automatic renewal."""
        ...

    def stop_auto_renew(self) -> None:
        """Stop automatic renewal."""
        ...
