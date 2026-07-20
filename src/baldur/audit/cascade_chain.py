"""
Cascade Chain validation logic.

Provides chain-depth checking and cycle detection.

Features:
- check_chain_depth: chain depth check
- detect_cycle: cycle detection
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.audit.cascade_config import (
    DEFAULT_CASCADE_CHAIN_CONFIG,
    CascadeChainConfig,
)
from baldur.audit.cascade_exceptions import (
    CascadeChainDepthExceeded,
    CascadeCycleDetected,
)

if TYPE_CHECKING:
    from baldur.audit.cascade_event import CascadeEffect

logger = structlog.get_logger()


# =============================================================================
# Chain depth check
# =============================================================================


def check_chain_depth(
    current_depth: int,
    cascade_id: str,
    namespace: str,
    trigger_type: str,
    config: CascadeChainConfig | None = None,
) -> None:
    """
    Chain depth check.

    Checks whether the current chain depth exceeds the configured threshold.

    Args:
        current_depth: Current chain depth
        cascade_id: Cascade ID
        namespace: Namespace
        trigger_type: Trigger type
        config: Chain config (defaults are used when None)

    Raises:
        CascadeChainDepthExceeded: On depth overflow (block_on_exceed=True)
    """
    if config is None:
        config = DEFAULT_CASCADE_CHAIN_CONFIG

    # Warning threshold check
    if current_depth >= config.warn_at_depth:
        logger.warning(
            "cascade_chain.depth_warning",
            current_depth=current_depth,
            warn_at_depth=config.warn_at_depth,
            cascade_id=cascade_id,
            namespace=namespace,
            trigger_type=trigger_type,
        )

    # Max depth check
    if current_depth >= config.max_chain_depth:
        logger.error(
            "cascade_chain.depth_exceeded",
            current_depth=current_depth,
            max_chain_depth=config.max_chain_depth,
            cascade_id=cascade_id,
        )

        # Record the metric (when available)
        _increment_depth_exceeded_metric(namespace, trigger_type)

        if config.block_on_exceed:
            raise CascadeChainDepthExceeded(
                depth=current_depth,
                max_depth=config.max_chain_depth,
                cascade_id=cascade_id,
            )
        logger.error(
            "cascade_chain.depth_exceeded_blocking",
            current_depth=current_depth,
            max_chain_depth=config.max_chain_depth,
        )


# Metric cache (prevents duplicate registration)
_CASCADE_CHAIN_DEPTH_EXCEEDED = None
_CASCADE_CYCLE_DETECTED = None


def _get_depth_exceeded_counter():
    """Return the chain-depth-exceeded Counter singleton."""
    global _CASCADE_CHAIN_DEPTH_EXCEEDED
    if _CASCADE_CHAIN_DEPTH_EXCEEDED is None:
        try:
            from baldur.metrics.registry import get_or_create_counter

            _CASCADE_CHAIN_DEPTH_EXCEEDED = get_or_create_counter(
                "baldur_cascade_chain_depth_exceeded_total",
                "Number of times cascade chain depth was exceeded",
                ["namespace", "trigger_type"],
            )
        except ImportError:
            pass
    return _CASCADE_CHAIN_DEPTH_EXCEEDED


def _get_cycle_detected_counter():
    """Return the cycle-detected Counter singleton."""
    global _CASCADE_CYCLE_DETECTED
    if _CASCADE_CYCLE_DETECTED is None:
        try:
            from baldur.metrics.registry import get_or_create_counter

            _CASCADE_CYCLE_DETECTED = get_or_create_counter(
                "baldur_cascade_cycle_detected_total",
                "Number of times a cascade cycle was detected",
                ["namespace"],
            )
        except ImportError:
            pass
    return _CASCADE_CYCLE_DETECTED


def _increment_depth_exceeded_metric(namespace: str, trigger_type: str) -> None:
    """Increment the chain-depth-exceeded metric (optional)."""
    counter = _get_depth_exceeded_counter()
    if counter:
        counter.labels(
            namespace=namespace,
            trigger_type=trigger_type,
        ).inc()


# =============================================================================
# Cycle detection
# =============================================================================


def detect_cycle(
    effects: list[CascadeEffect],
    trigger_event_id: str,
) -> list[str] | None:
    """
    Cycle detection.

    Checks the effect list for a cycle (A → B → A).

    Args:
        effects: Effect list
        trigger_event_id: Trigger event ID

    Returns:
        The cycle path (list of event IDs), or None if there is none

    Example:
        >>> effects = [
        ...     CascadeEffect(event_id="A", caused_by="trigger", ...),
        ...     CascadeEffect(event_id="B", caused_by="A", ...),
        ...     CascadeEffect(event_id="C", caused_by="B", ...),
        ...     CascadeEffect(event_id="A", caused_by="C", ...),  # cycle!
        ... ]
        >>> cycle = detect_cycle(effects, "trigger")
        >>> print(cycle)  # ["A", "B", "C", "A"]
    """
    if not effects:
        return None

    # Build the graph: event_id -> caused_by
    graph: dict[str, str | None] = {trigger_event_id: None}
    for effect in effects:
        graph[effect.event_id] = effect.caused_by

    # Map each effect to the effects it causes
    children: dict[str, list[str]] = {}
    for effect in effects:
        caused_by = effect.caused_by
        if caused_by not in children:
            children[caused_by] = []
        children[caused_by].append(effect.event_id)

    # Detect cycles via DFS
    visited: set[str] = set()
    path: list[str] = []

    def dfs(node: str) -> list[str] | None:
        if node in path:
            # Cycle found
            cycle_start = path.index(node)
            return path[cycle_start:] + [node]

        if node in visited:
            return None

        visited.add(node)
        path.append(node)

        # Walk the effects caused by this node
        for child in children.get(node, []):
            cycle = dfs(child)
            if cycle:
                return cycle

        path.pop()
        return None

    return dfs(trigger_event_id)


def check_and_raise_cycle(
    effects: list[CascadeEffect],
    trigger_event_id: str,
    cascade_id: str,
    namespace: str,
    config: CascadeChainConfig | None = None,
) -> None:
    """
    Check for a cycle and raise.

    Args:
        effects: Effect list
        trigger_event_id: Trigger event ID
        cascade_id: Cascade ID
        namespace: Namespace
        config: Chain config (defaults are used when None)

    Raises:
        CascadeCycleDetected: When a cycle is detected
    """
    if config is None:
        config = DEFAULT_CASCADE_CHAIN_CONFIG

    if not config.detect_cycles:
        return

    cycle_path = detect_cycle(effects, trigger_event_id)

    if cycle_path:
        logger.error(
            "cascade_chain.cycle_detected",
            cycle_path=cycle_path,
            cascade_id=cascade_id,
            namespace=namespace,
        )

        # Record the metric (when available)
        _increment_cycle_detected_metric(namespace)

        raise CascadeCycleDetected(
            cycle_path=cycle_path,
            cascade_id=cascade_id,
        )


def _increment_cycle_detected_metric(namespace: str) -> None:
    """Increment the cycle-detected metric (optional)."""
    counter = _get_cycle_detected_counter()
    if counter:
        counter.labels(namespace=namespace).inc()


# =============================================================================
# Combined validation function
# =============================================================================


def validate_cascade_chain(
    effects: list[CascadeEffect],
    trigger_event_id: str,
    cascade_id: str,
    namespace: str,
    current_depth: int,
    trigger_type: str,
    config: CascadeChainConfig | None = None,
) -> None:
    """
    Full Cascade chain validation.

    Performs both the depth check and cycle detection.

    Args:
        effects: Effect list
        trigger_event_id: Trigger event ID
        cascade_id: Cascade ID
        namespace: Namespace
        current_depth: Current chain depth
        trigger_type: Trigger type
        config: Chain config (defaults are used when None)

    Raises:
        CascadeChainDepthExceeded: On depth overflow
        CascadeCycleDetected: When a cycle is detected
    """
    if config is None:
        config = DEFAULT_CASCADE_CHAIN_CONFIG

    # 1. Depth check
    check_chain_depth(
        current_depth=current_depth,
        cascade_id=cascade_id,
        namespace=namespace,
        trigger_type=trigger_type,
        config=config,
    )

    # 2. Cycle detection
    check_and_raise_cycle(
        effects=effects,
        trigger_event_id=trigger_event_id,
        cascade_id=cascade_id,
        namespace=namespace,
        config=config,
    )
