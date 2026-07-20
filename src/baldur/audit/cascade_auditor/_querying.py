"""
Cascade Auditor - querying module.

Responsible for Cascade Event lookup and causation tracing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.cascade_auditor._helpers import get_index_ids
from baldur.audit.cascade_event import CascadeEvent

logger = structlog.get_logger()


class QueryingMixin:
    """Cascade Event querying methods."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided by CascadeEventAuditor.
        CASCADE_KEY: str
        CASCADE_INDEX_KEY: str

        def _get_backend(self) -> Any: ...

    def get_cascade_event(
        self,
        cascade_id: str,
        namespace: str,
    ) -> CascadeEvent | None:
        """
        Look up a Cascade Event.

        Args:
            cascade_id: Cascade Event ID
            namespace: Namespace

        Returns:
            CascadeEvent, or None
        """
        backend = self._get_backend()
        key = self.CASCADE_KEY.format(namespace=namespace, cascade_id=cascade_id)
        data = backend.get(key)

        if data:
            return CascadeEvent.from_dict(data)
        return None

    def get_recent_events(
        self,
        namespace: str,
        limit: int = 100,
    ) -> list[CascadeEvent]:
        """
        List recent Cascade Events.

        Args:
            namespace: Namespace
            limit: Maximum count

        Returns:
            List of CascadeEvent (newest first)
        """
        backend = self._get_backend()
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)

        cascade_ids = get_index_ids(backend, index_key)
        if not cascade_ids:
            return []

        cascade_ids = cascade_ids[:limit]

        events = []
        for cascade_id in cascade_ids:
            event = self.get_cascade_event(cascade_id, namespace)
            if event:
                events.append(event)

        return events

    def get_event_count(self, namespace: str) -> int:
        """
        Total Cascade Event count for a namespace.

        Args:
            namespace: Namespace

        Returns:
            Event count
        """
        backend = self._get_backend()
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        return len(get_index_ids(backend, index_key))

    def find_by_trigger_event(
        self,
        trigger_event_id: str,
        namespace: str,
    ) -> CascadeEvent | None:
        """
        Look up a Cascade Event by trigger event ID.

        Args:
            trigger_event_id: Trigger event ID
            namespace: Namespace

        Returns:
            CascadeEvent, or None
        """
        events = self.get_recent_events(namespace, limit=1000)

        for event in events:
            if event.trigger.event_id == trigger_event_id:
                return event

        return None

    def get_causation_trace(
        self,
        effect_event_id: str,
        namespace: str,
    ) -> list[dict[str, Any]]:
        """
        Trace the causation of an effect event.

        Traces back why a specific effect occurred.

        Args:
            effect_event_id: Effect event ID
            namespace: Namespace

        Returns:
            Causation trace result (traced back to the trigger)
        """
        events = self.get_recent_events(namespace, limit=1000)

        for cascade in events:
            for effect in cascade.effects:
                if effect.event_id == effect_event_id:
                    # Trace causation backwards
                    trace: list[dict[str, Any]] = []
                    current_id = effect_event_id

                    while True:
                        # Find the effect matching the current ID
                        found = False
                        for e in cascade.effects:
                            if e.event_id == current_id:
                                trace.append(
                                    {
                                        "event_id": e.event_id,
                                        "action_type": e.action_type,
                                        "caused_by": e.caused_by,
                                    }
                                )
                                current_id = e.caused_by
                                found = True
                                break

                        if not found:
                            # Reached the trigger
                            if current_id == cascade.trigger.event_id:
                                trace.append(
                                    {
                                        "event_id": cascade.trigger.event_id,
                                        "action_type": cascade.trigger.trigger_type,
                                        "caused_by": None,
                                    }
                                )
                            break

                    return list(reversed(trace))

        return []

    def get_events_after_timestamp(
        self,
        namespace: str,
        after_timestamp: str,
        limit: int = 1000,
    ) -> list[CascadeEvent]:
        """
        Look up events after a specific point in time.

        Args:
            namespace: Namespace
            after_timestamp: Only events after this time (ISO format)
            limit: Maximum count

        Returns:
            List of CascadeEvent (newest first)
        """
        from datetime import datetime

        all_events = self.get_recent_events(namespace, limit=limit)

        try:
            cutoff = datetime.fromisoformat(after_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return all_events

        filtered = []
        for event in all_events:
            try:
                event_time = datetime.fromisoformat(
                    event.timestamp.replace("Z", "+00:00")
                )
                if event_time > cutoff:
                    filtered.append(event)
            except ValueError:
                # Include on parse failure
                filtered.append(event)

        return filtered
