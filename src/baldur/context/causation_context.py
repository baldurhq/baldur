"""
Causation Context - causality tracking context.

Safely propagates causality information across async boundaries
(Celery/Kafka).

Features:
- contextvars-based thread/async-safe context management
- Causality propagation between Celery tasks
- cascade_id, parent_event_id, chain_depth tracking

Usage:
    # Start a new cascade
    with CausationContext.start_cascade(namespace="seoul") as ctx:
        print(f"Cascade ID: {ctx.cascade_id}")
        do_work()

    # When calling a Celery task
    my_task.apply_async(
        args=[...],
        headers=get_causation_for_celery(),
    )

    # Restoring inside a Celery task
    @shared_task(bind=True)
    def my_task(self, ...):
        with restore_causation_from_celery(self.request.headers or {}):
            do_work()

Reference:
    context/actor_context.py — the ContextVar pattern this module follows.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# X-Test causation ID prefix constant and helpers
# =============================================================================


XTEST_CAUSATION_PREFIX = "XTC-"
"""X-Test-Mode causation ID prefix. Identifies test requests in logs."""


def _get_xtest_id_prefix() -> str:
    """
    Return the ID prefix according to X-Test-Mode.

    Returns 'XTC-' when TestModeContext.is_synthetic() is True, otherwise an
    empty string.

    Returns:
        'XTC-' (X-Test-Mode) or '' (production mode)
    """
    try:
        from baldur.core.test_mode_context import TestModeContext

        if TestModeContext.is_synthetic():
            return XTEST_CAUSATION_PREFIX
    except ImportError:
        pass
    return ""


def is_xtest_id(causation_id: str) -> bool:
    """
    Check whether the given ID is an X-Test causation ID.

    Args:
        causation_id: Causation ID to check (cascade_id or event_id)

    Returns:
        True if the ID starts with the XTC- prefix

    Examples:
        >>> is_xtest_id("XTC-cascade-a1b2c3d4e5f6")
        True
        >>> is_xtest_id("cascade-a1b2c3d4e5f6")
        False
    """
    return causation_id.startswith(XTEST_CAUSATION_PREFIX)


def normalize_causation_id(causation_id: str) -> str:
    """
    Strip the XTC- prefix from a causation ID.

    Used by existing ID parsing logic for backward compatibility.
    An ID without the prefix is returned unchanged.

    Args:
        causation_id: Causation ID to normalize

    Returns:
        The bare ID with the XTC- prefix removed

    Examples:
        >>> normalize_causation_id("XTC-cascade-a1b2c3d4e5f6")
        'cascade-a1b2c3d4e5f6'
        >>> normalize_causation_id("cascade-a1b2c3d4e5f6")
        'cascade-a1b2c3d4e5f6'
    """
    if is_xtest_id(causation_id):
        return causation_id[len(XTEST_CAUSATION_PREFIX) :]
    return causation_id


# =============================================================================
# Celery header constants
# =============================================================================


CELERY_HEADER_CASCADE_ID = "x-baldur-cascade-id"
"""Celery message header: cascade ID."""

CELERY_HEADER_PARENT_EVENT = "x-baldur-parent-event"
"""Celery message header: parent event ID."""

CELERY_HEADER_CHAIN_DEPTH = "x-baldur-chain-depth"
"""Celery message header: chain depth."""

CELERY_HEADER_NAMESPACE = "x-baldur-namespace"
"""Celery message header: namespace."""


# Kafka headers (same structure)
KAFKA_HEADER_PREFIX = "baldur."
"""Kafka message header prefix."""


# =============================================================================
# CausationInfo
# =============================================================================


@dataclass
class CausationInfo(SerializableMixin):
    """
    Causality tracking information.

    Uses contextvars to guarantee thread/async safety.

    Attributes:
        cascade_id: Current cascade event ID
        parent_event_id: Parent event ID (causality chain)
        chain_depth: Current chain depth (guards against cycles)
        namespace: Namespace
        metadata: Additional metadata

    Code reference:
        context/actor_context.py (the _current_actor ContextVar pattern)
    """

    cascade_id: str
    """Current cascade event ID."""

    parent_event_id: str
    """Parent event ID (causality chain)."""

    chain_depth: int = 0
    """Current chain depth (guards against cycles)."""

    namespace: str = "global"
    """Namespace."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CausationInfo:
        """Deserialize (for restoration on the receiving side)."""
        return cls(
            cascade_id=data.get("cascade_id", ""),
            parent_event_id=data.get("parent_event_id", ""),
            chain_depth=data.get("chain_depth", 0),
            namespace=data.get("namespace", "global"),
            metadata=data.get("metadata", {}),
        )


# =============================================================================
# ContextVar declaration
# =============================================================================


_current_causation: ContextVar[CausationInfo | None] = ContextVar(
    "current_causation", default=None
)
"""Current causality context (follows the actor_context.py pattern)."""


# =============================================================================
# CausationContext
# =============================================================================


class CausationContext:
    """
    Causality context manager.

    Uses Python's contextvars to track causality information safely in
    threaded and async environments.

    Usage:
        # Start a new cascade
        with CausationContext.start_cascade(namespace="seoul") as ctx:
            print(f"Cascade: {ctx.cascade_id}")
            # ctx.cascade_id is available here
            do_work()

        # Continue an existing cascade (restore across an async boundary)
        with CausationContext.continue_cascade(causation_info):
            do_work()

        # Read the current context
        info = CausationContext.get_current()
        if info:
            print(f"Current cascade: {info.cascade_id}")

    Code reference:
        context/actor_context.py (the ActorContext pattern)
    """

    @classmethod
    @contextmanager
    def start_cascade(
        cls,
        namespace: str = "global",
        trigger_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Generator[CausationInfo, None, None]:
        """
        Start a new cascade.

        In X-Test-Mode the XTC- prefix is added to every ID automatically.

        Args:
            namespace: Namespace
            trigger_event_id: Trigger event ID (generated when omitted)
            metadata: Additional metadata

        Yields:
            CausationInfo instance
        """
        prefix = _get_xtest_id_prefix()
        cascade_id = f"{prefix}cascade-{uuid.uuid4().hex[:12]}"
        event_id = trigger_event_id or f"{prefix}evt-{uuid.uuid4().hex[:8]}"

        info = CausationInfo(
            cascade_id=cascade_id,
            parent_event_id=event_id,
            chain_depth=0,
            namespace=namespace,
            metadata=metadata or {},
        )

        token = _current_causation.set(info)
        try:
            logger.debug(
                "causation_context.started_cascade",
                cascade_id=cascade_id,
                namespace=namespace,
            )
            yield info
        finally:
            _current_causation.reset(token)
            logger.debug(
                "causation_context.ended_cascade",
                cascade_id=cascade_id,
            )

    @classmethod
    @contextmanager
    def start_system_cascade(
        cls,
        source: str,
        namespace: str = "global",
        metadata: dict[str, Any] | None = None,
    ) -> Generator[CausationInfo, None, None]:
        """
        Start a cascade for a system trigger (Celery Beat / management command).

        Used to track causality for automated system work rather than API
        requests. The trigger_event_id is generated in the form
        SYSTEM_ROOT_{source}_{uuid}. In X-Test-Mode the XTC- prefix is added
        automatically.

        Args:
            source: Trigger source (celery_beat, management_cmd, cron,
                scheduler)
            namespace: Namespace
            metadata: Additional metadata

        Yields:
            CausationInfo instance

        Examples:
            with CausationContext.start_system_cascade(source="celery_beat") as ctx:
                process_scheduled_task()
                # ctx.parent_event_id = "SYSTEM_ROOT_celery_beat_{uuid}"
        """
        prefix = _get_xtest_id_prefix()
        system_event_id = f"{prefix}SYSTEM_ROOT_{source}_{uuid.uuid4().hex[:8]}"

        with cls.start_cascade(
            namespace=namespace,
            trigger_event_id=system_event_id,
            metadata={**(metadata or {}), "system_source": source},
        ) as ctx:
            logger.debug(
                "causation_context.started_system_cascade",
                source=source,
                system_event_id=system_event_id,
            )
            yield ctx

    @classmethod
    @contextmanager
    def continue_cascade(
        cls,
        info: CausationInfo,
        increment_depth: bool = True,
    ) -> Generator[CausationInfo, None, None]:
        """
        Continue an existing cascade (restore across an async boundary).

        Args:
            info: CausationInfo to restore
            increment_depth: Whether to increment the chain depth

        Yields:
            CausationInfo instance (with the depth incremented)
        """
        new_depth = info.chain_depth + 1 if increment_depth else info.chain_depth

        continued_info = CausationInfo(
            cascade_id=info.cascade_id,
            parent_event_id=info.parent_event_id,
            chain_depth=new_depth,
            namespace=info.namespace,
            metadata=dict(info.metadata),  # copy
        )

        token = _current_causation.set(continued_info)
        try:
            logger.debug(
                "causation_context.continued_cascade",
                cascade_info_id=info.cascade_id,
                new_depth=new_depth,
            )
            yield continued_info
        finally:
            _current_causation.reset(token)

    @classmethod
    def get_current(cls) -> CausationInfo | None:
        """
        Read the current context.

        Returns:
            The current CausationInfo, or None
        """
        return _current_causation.get()

    @classmethod
    def is_set(cls) -> bool:
        """
        Check whether a context is set.

        Returns:
            True if a context is set
        """
        return _current_causation.get() is not None

    @classmethod
    def get_current_cascade_id(cls) -> str | None:
        """
        Read the current cascade ID.

        Returns:
            The current cascade_id, or None
        """
        info = cls.get_current()
        return info.cascade_id if info else None

    @classmethod
    def get_current_depth(cls) -> int:
        """
        Read the current chain depth.

        Returns:
            The current chain_depth (0 when no context is set)
        """
        info = cls.get_current()
        return info.chain_depth if info else 0

    @classmethod
    @contextmanager
    def set_parent_event(
        cls,
        new_event_id: str,
    ) -> Generator[CausationInfo, None, None]:
        """
        Change the parent event ID.

        Used when recording a new effect within the current context.

        Args:
            new_event_id: New parent event ID

        Yields:
            The updated CausationInfo
        """
        current = cls.get_current()
        if not current:
            raise RuntimeError("No causation context set")

        updated_info = CausationInfo(
            cascade_id=current.cascade_id,
            parent_event_id=new_event_id,
            chain_depth=current.chain_depth,
            namespace=current.namespace,
            metadata=dict(current.metadata),
        )

        token = _current_causation.set(updated_info)
        try:
            yield updated_info
        finally:
            _current_causation.reset(token)


# =============================================================================
# Celery propagation functions
# =============================================================================


def get_causation_for_celery() -> dict[str, str]:
    """
    Build the causation headers to pass when calling a Celery task.

    Usage:
        my_task.apply_async(
            args=[...],
            headers=get_causation_for_celery(),
        )

    Returns:
        Celery message header dict

    Code reference:
        context/actor_context.py (the get_actor_for_celery pattern)
    """
    info = CausationContext.get_current()
    if not info:
        return {}

    return {
        CELERY_HEADER_CASCADE_ID: info.cascade_id,
        CELERY_HEADER_PARENT_EVENT: info.parent_event_id,
        CELERY_HEADER_CHAIN_DEPTH: str(info.chain_depth),
        CELERY_HEADER_NAMESPACE: info.namespace,
    }


@contextmanager
def restore_causation_from_celery(
    headers: dict[str, str],
) -> Generator[CausationInfo | None, None, None]:
    """
    Restore causation inside a Celery task.

    Usage:
        @shared_task(bind=True)
        def my_task(self, ...):
            with restore_causation_from_celery(self.request.headers or {}):
                do_work()

    Args:
        headers: Celery request headers

    Yields:
        The restored CausationInfo, or None

    Code reference:
        context/actor_context.py (the restore_actor_from_celery pattern)
    """
    cascade_id = headers.get(CELERY_HEADER_CASCADE_ID)

    if not cascade_id:
        yield None
        return

    info = CausationInfo(
        cascade_id=cascade_id,
        parent_event_id=headers.get(CELERY_HEADER_PARENT_EVENT, ""),
        chain_depth=int(headers.get(CELERY_HEADER_CHAIN_DEPTH, "0")),
        namespace=headers.get(CELERY_HEADER_NAMESPACE, "global"),
        metadata={
            "restored_from": "celery",
            "restored_at": utc_now().isoformat(),
        },
    )

    with CausationContext.continue_cascade(info) as ctx:
        yield ctx


def get_causation_for_kafka() -> dict[str, bytes]:
    """
    Build the causation headers to pass when sending a Kafka message.

    Usage:
        producer.send(
            topic="my-topic",
            value=message,
            headers=list(get_causation_for_kafka().items()),
        )

    Returns:
        Kafka message header dict (bytes values)
    """
    info = CausationContext.get_current()
    if not info:
        return {}

    return {
        f"{KAFKA_HEADER_PREFIX}cascade_id": info.cascade_id.encode("utf-8"),
        f"{KAFKA_HEADER_PREFIX}parent_event": info.parent_event_id.encode("utf-8"),
        f"{KAFKA_HEADER_PREFIX}chain_depth": str(info.chain_depth).encode("utf-8"),
        f"{KAFKA_HEADER_PREFIX}namespace": info.namespace.encode("utf-8"),
    }


@contextmanager
def restore_causation_from_kafka(
    headers: list | None = None,
) -> Generator[CausationInfo | None, None, None]:
    """
    Restore causation in a Kafka consumer.

    Usage:
        for message in consumer:
            with restore_causation_from_kafka(message.headers):
                process_message(message)

    Args:
        headers: Kafka message header list [(key, value), ...]

    Yields:
        The restored CausationInfo, or None
    """
    if not headers:
        yield None
        return

    # Convert the headers into a dict
    header_dict = {}
    for key, value in headers:
        if key.startswith(KAFKA_HEADER_PREFIX):
            short_key = key[len(KAFKA_HEADER_PREFIX) :]
            header_dict[short_key] = (
                value.decode("utf-8") if isinstance(value, bytes) else value
            )

    cascade_id = header_dict.get("cascade_id")
    if not cascade_id:
        yield None
        return

    info = CausationInfo(
        cascade_id=cascade_id,
        parent_event_id=header_dict.get("parent_event", ""),
        chain_depth=int(header_dict.get("chain_depth", "0")),
        namespace=header_dict.get("namespace", "global"),
        metadata={
            "restored_from": "kafka",
            "restored_at": utc_now().isoformat(),
        },
    )

    with CausationContext.continue_cascade(info) as ctx:
        yield ctx
