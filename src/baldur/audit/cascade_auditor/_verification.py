"""
Cascade Auditor - integrity verification module.

Owns hash chain verification and checkpoint responsibilities.
Consolidates the duplicated hash verification logic into
_verify_event_chain().
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.audit.cascade_event import CascadeEvent

logger = structlog.get_logger()


def _verify_event_chain(events: list[CascadeEvent]) -> list[dict[str, Any]]:
    """
    Shared logic verifying the hash chain integrity of an event list.

    Consolidates the verification code that was repeated identically in
    verify_chain_integrity/verify_chain_integrity_from_checkpoint.

    Args:
        events: CascadeEvent list (sorted newest first)

    Returns:
        Error list
    """
    errors = []

    for i, event in enumerate(events):
        # 1. Recompute the hash
        recalculated_hash = event.calculate_hash()
        if recalculated_hash != event.current_hash:
            errors.append(
                {
                    "cascade_id": event.id,
                    "error": "hash_mismatch",
                    "expected": event.current_hash,
                    "actual": recalculated_hash,
                }
            )

        # 2. Check the chain link (excluding the last one)
        # Sorted newest first, so i=0 is newest and i+1 is the older event
        if i < len(events) - 1:
            older_event = events[i + 1]
            if event.previous_hash != older_event.current_hash:
                errors.append(
                    {
                        "cascade_id": event.id,
                        "error": "chain_broken",
                        "expected_previous": older_event.current_hash,
                        "actual_previous": event.previous_hash,
                    }
                )

    return errors


class VerificationMixin:
    """Hash Chain integrity verification and checkpoint methods."""

    # Checkpoint Redis key pattern
    CHECKPOINT_KEY = "baldur:{namespace}:audit:cascade_checkpoint"

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided by CascadeEventAuditor
        # and sibling mixins (QueryingMixin).
        CASCADE_INDEX_KEY: str

        def _get_backend(self) -> Any: ...
        def _get_last_hash(self, namespace: str) -> str | None: ...
        def get_recent_events(
            self, namespace: str, limit: int = 100
        ) -> list[CascadeEvent]: ...

    def verify_chain_integrity(
        self,
        namespace: str,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """
        Hash Chain integrity verification.

        Args:
            namespace: Namespace
            limit: Maximum number of events to verify

        Returns:
            Verification result dictionary:
            - valid: Whether integrity holds
            - checked: Number of events verified
            - errors: Error list
        """
        events = self.get_recent_events(namespace, limit)

        if not events:
            return {"valid": True, "checked": 0, "errors": []}

        errors = _verify_event_chain(events)

        return {
            "valid": len(errors) == 0,
            "checked": len(events),
            "errors": errors,
        }

    def create_checkpoint(self, namespace: str) -> dict[str, Any]:
        """
        Save the current state as a checkpoint.

        A checkpoint records the Hash Chain state at a point in time so that
        later integrity verification can verify only what follows the
        checkpoint instead of starting from the beginning.

        Invoked from the daily Celery Beat.

        Args:
            namespace: Namespace

        Returns:
            The created checkpoint info
        """

        from baldur.audit.cascade_auditor._helpers import get_index_ids

        backend = self._get_backend()

        # Look up the newest event's hash
        last_hash = self._get_last_hash(namespace)

        # Count the events
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        event_count = len(get_index_ids(backend, index_key))

        checkpoint = {
            "last_hash": last_hash,
            "verified_at": utc_now().isoformat(),
            "event_count": event_count,
            "namespace": namespace,
            "version": "1.0",
        }

        key = self.CHECKPOINT_KEY.format(namespace=namespace)
        backend.set(key, checkpoint)

        logger.info(
            "cascade_audit.checkpoint_created",
            namespace=namespace,
            event_count=event_count,
            last_hash=last_hash[:16] if last_hash else "None",
        )

        return checkpoint

    def get_checkpoint(self, namespace: str) -> dict[str, Any] | None:
        """
        Look up a checkpoint.

        Args:
            namespace: Namespace

        Returns:
            Checkpoint info, or None
        """
        backend = self._get_backend()
        key = self.CHECKPOINT_KEY.format(namespace=namespace)
        return backend.get(key)

    def verify_chain_integrity_from_checkpoint(
        self,
        namespace: str,
    ) -> dict[str, Any]:
        """
        Verify only what follows the checkpoint (efficient).

        verify_chain_integrity() verifies from the beginning, while this
        method verifies only what follows the last checkpoint.

        Args:
            namespace: Namespace

        Returns:
            Verification result dictionary
        """
        # 1. Look up the checkpoint
        checkpoint = self.get_checkpoint(namespace)

        if not checkpoint or not checkpoint.get("last_hash"):
            # No checkpoint — verify everything
            return self.verify_chain_integrity(namespace)

        # 2. Look up all events (newest first)
        events = self.get_recent_events(namespace, limit=10000)

        if not events:
            return {
                "valid": True,
                "checked": 0,
                "from_checkpoint": checkpoint.get("verified_at"),
                "errors": [],
            }

        # 3. Filter to the events after the checkpoint
        checkpoint_hash = checkpoint.get("last_hash")
        events_after_checkpoint = []
        checkpoint_found = False

        for event in events:
            if event.current_hash == checkpoint_hash:
                checkpoint_found = True
                break
            events_after_checkpoint.append(event)

        if not checkpoint_found:
            logger.warning(
                "cascade_audit.checkpoint_hash_found_falling",
                namespace=namespace,
            )
            return self.verify_chain_integrity(namespace)

        if not events_after_checkpoint:
            return {
                "valid": True,
                "checked": 0,
                "from_checkpoint": checkpoint.get("verified_at"),
                "errors": [],
            }

        # 4. Verify only the events after the checkpoint
        errors = []

        # Check that the first event (right after the checkpoint) has a
        # previous_hash linking back to the checkpoint
        first_event = events_after_checkpoint[-1]  # the oldest one
        if first_event.previous_hash != checkpoint_hash:
            errors.append(
                {
                    "cascade_id": first_event.id,
                    "error": "checkpoint_mismatch",
                    "expected_previous": checkpoint_hash,
                    "actual_previous": first_event.previous_hash,
                }
            )

        # Verify the rest of the chain (via the shared function)
        errors.extend(_verify_event_chain(events_after_checkpoint))

        return {
            "valid": len(errors) == 0,
            "checked": len(events_after_checkpoint),
            "from_checkpoint": checkpoint.get("verified_at"),
            "errors": errors,
        }
