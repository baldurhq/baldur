"""
Traffic Gate - integration of RateController + CascadeLoadShedding + Bulkhead.

Pipelines RateController, CascadeLoadShedding, and Bulkhead together to control
traffic.

Processing order:
1. Bulkhead (per-domain isolation) - reject only the affected domain when one
   floods.
2. CascadeLoadShedding (priority filtering) - reject low-priority requests.
3. RateController (process-local rate gate) - bound this instance's throughput.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from baldur.scaling.config import (
    BackpressureLevel,
)
from baldur.scaling.rate_controller import (
    RateController,
    get_rate_controller,
)

logger = structlog.get_logger()


# Thresholds used to convert TrafficGate priority ints into RateController
# priority tier strings.
# TrafficGate convention: lower means higher priority.
# AdmissionControlMiddleware passes critical=0, standard=50, non_essential=100.
_PRIORITY_TIER_THRESHOLDS: list[tuple[int, str]] = [
    (25, "critical"),  # priority <= 25 → critical
    (75, "standard"),  # priority <= 75 → standard
]
_PRIORITY_TIER_DEFAULT = "non_essential"  # priority > 75


def _map_priority_int_to_tier(priority: int) -> str:
    """Convert a priority int to a tier string.

    Args:
        priority: request priority (lower means higher priority)

    Returns:
        "critical" | "standard" | "non_essential"
    """
    for threshold, tier in _PRIORITY_TIER_THRESHOLDS:
        if priority <= threshold:
            return tier
    return _PRIORITY_TIER_DEFAULT


@dataclass
class TrafficDecision:
    """Traffic Gate decision result."""

    allowed: bool
    """Whether processing is allowed."""

    reason: str
    """Reason for the decision."""

    level: BackpressureLevel
    """Current backpressure level."""

    gate: str
    """Name of the deciding gate."""

    metadata: dict[str, Any] | None = None
    """Additional metadata."""

    bulkhead_acquired: bool = False
    """Whether a bulkhead resource was acquired (release required when True)."""

    bulkhead_name: str | None = None
    """Name of the acquired bulkhead."""


class TrafficGate:
    """
    Traffic Gate - integrated traffic control.

    Processing order:
    1. Bulkhead.try_acquire() - per-domain resource isolation (new)
    2. CascadeLoadShedding.should_accept() - priority-based filtering
    3. RateController.should_process() - rate-limit-based throttling

    Benefits of bulkhead integration:
    - When the database domain floods, only the database bulkhead rejects;
      cache/external_api keep flowing
    - Avoids exhausting the process-local rate budget
    - Per-domain bottlenecks are easy to identify

    Usage:
        gate = TrafficGate()

        # Basic usage (no bulkhead)
        decision = gate.should_allow(priority=5)

        # Usage with a bulkhead
        decision = gate.should_allow(priority=5, bulkhead_name="database")
        if decision.allowed:
            try:
                process_item()
            finally:
                # Note: when the bulkhead is acquired, release it
                if decision.bulkhead_acquired:
                    gate.release_bulkhead("database")
    """

    def __init__(
        self,
        rate_controller: RateController | None = None,
        load_shedding: Any | None = None,
    ):
        """
        Args:
            rate_controller: RateController instance
            load_shedding: optional CascadeLoadShedding instance
        """
        self._rate_controller = rate_controller or get_rate_controller()
        self._load_shedding = load_shedding

    def _check_bulkhead(
        self,
        bulkhead_name: str,
        current_level: BackpressureLevel,
        metadata: dict[str, Any] | None,
        timeout: float | None = None,
    ) -> tuple[bool, TrafficDecision | None]:
        """Check the bulkhead. Returns acquisition status and a rejection decision.

        Fail-open on every failure class (the request proceeds ungated), but the
        log severity distinguishes expected unavailability from unexpected errors:
        unknown-name skips are routine; an unexpected error means the bulkhead
        contract was violated and is logged loudly so the next contract-class
        bug surfaces instead of hiding. The resolution chain always yields a
        registry, so there is no registry-absent branch.

        Args:
            bulkhead_name: bulkhead name
            current_level: current backpressure level
            metadata: additional metadata
            timeout: upper bound (seconds) on how long the bulkhead may wait for
                capacity — not a guarantee of waiting. None fails fast. Semaphore
                compartments honor it; thread-pool compartments return an
                immediate verdict regardless (their bounded queue absorbs bursts).
        """
        try:
            from baldur.services.bulkhead.registry import get_bulkhead_registry

            bulkhead = get_bulkhead_registry().get(bulkhead_name)

            if not bulkhead.try_acquire(timeout=timeout):
                return False, TrafficDecision(
                    allowed=False,
                    reason=f"Bulkhead '{bulkhead_name}' is full",
                    level=current_level,
                    gate="Bulkhead",
                    metadata=metadata,
                    bulkhead_acquired=False,
                    bulkhead_name=bulkhead_name,
                )
            return True, None
        except KeyError:
            logger.debug(
                "traffic_gate.bulkhead_found_skipping",
                bulkhead_name=bulkhead_name,
            )
            return False, None
        except Exception as e:
            logger.exception(
                "traffic_gate.bulkhead_error",
                bulkhead_name=bulkhead_name,
                error=e,
            )
            return False, None

    def _check_load_shedding(
        self,
        priority: int,
        current_level: BackpressureLevel,
        metadata: dict[str, Any] | None,
    ) -> TrafficDecision | None:
        """Check load shedding. Returns a rejection decision, or None on accept."""
        if self._load_shedding is None:
            return None

        try:
            if hasattr(self._load_shedding, "should_accept"):
                result = self._load_shedding.should_accept(priority=priority)
                if isinstance(result, dict) and not result.get("accepted", True):
                    return TrafficDecision(
                        allowed=False,
                        reason=f"Load shedding rejected priority={priority}",
                        level=current_level,
                        gate="CascadeLoadShedding",
                        metadata=metadata,
                    )
        except Exception as e:
            logger.warning(
                "traffic_gate.loadshedding_failed",
                error=e,
            )
        return None

    def should_allow(
        self,
        priority: int = 0,
        bulkhead_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        bulkhead_timeout: float | None = None,
    ) -> TrafficDecision:
        """
        Decide whether to allow traffic.

        Processing order:
        1. Bulkhead (per-domain isolation) - new
        2. CascadeLoadShedding (priority filtering)
        3. RateController (process-local rate gate)

        Args:
            priority: request priority (lower means higher priority)
            bulkhead_name: bulkhead name (ConnectionType.value or custom)
            metadata: additional metadata used in the decision
            bulkhead_timeout: upper bound (seconds) on how long the bulkhead may
                wait for capacity — not a guarantee of waiting. None fails fast
                (immediate verdict). Semaphore-backed compartments wait up to
                this bound; ThreadPool-backed compartments return an immediate
                verdict regardless, since their bounded queue already absorbs
                bursts.

        Returns:
            TrafficDecision

        Note:
            When bulkhead_name is set and the result is allowed=True with
            bulkhead_acquired=True, the caller must invoke release_bulkhead()
            after the operation completes.
        """
        current_level = self._rate_controller.get_state().level
        bulkhead_acquired = False

        # Step 0: Deadline expiry + Dynamic Fast-Fail
        try:
            from baldur.scaling.deadline_context import (
                get_estimated_processing_ms,
                is_expired,
                should_fast_fail,
            )

            if is_expired():
                return TrafficDecision(
                    allowed=False,
                    reason="Deadline expired",
                    level=current_level,
                    gate="DeadlineContext",
                    metadata=metadata,
                )

            # Dynamic Fast-Fail: estimated RTT-based processing time vs remaining time
            # Pull tier_id from metadata to look up the per-tier GradientCalculator
            tier_id = (metadata or {}).get("tier_id", "standard")
            calculator_name = f"admission_control:{tier_id}"
            estimated = get_estimated_processing_ms(
                calculator_name=calculator_name,
                tier_id=tier_id,
            )
            if should_fast_fail(estimated):
                return TrafficDecision(
                    allowed=False,
                    reason=(
                        f"Deadline Fast-Fail: estimated={estimated:.0f}ms "
                        f"exceeds remaining time"
                    ),
                    level=current_level,
                    gate="DeadlineContext",
                    metadata={
                        **(metadata or {}),
                        "estimated_ms": estimated,
                        "fast_fail": True,
                    },
                )
        except ImportError:
            pass

        # Step 1: Bulkhead check (per-domain isolation)
        if bulkhead_name is not None:
            acquired, decision = self._check_bulkhead(
                bulkhead_name,
                current_level,
                metadata,
                timeout=bulkhead_timeout,
            )
            if decision is not None:
                return decision
            bulkhead_acquired = acquired

        # Step 2: CascadeLoadShedding check
        load_shedding_decision = self._check_load_shedding(
            priority, current_level, metadata
        )
        if load_shedding_decision is not None:
            if bulkhead_acquired and bulkhead_name:
                self._release_bulkhead_internal(bulkhead_name)
            return load_shedding_decision

        # Step 3: RateController check (priority-based watermark)
        tier_str = _map_priority_int_to_tier(priority)
        if not self._rate_controller.should_process(priority=tier_str):
            if bulkhead_acquired and bulkhead_name:
                self._release_bulkhead_internal(bulkhead_name)
            return TrafficDecision(
                allowed=False,
                reason=(
                    f"Rate limit exceeded: priority={tier_str}, "
                    f"level={current_level.value}"
                ),
                level=current_level,
                gate="RateController",
                metadata={**(metadata or {}), "priority": tier_str},
            )

        return TrafficDecision(
            allowed=True,
            reason="Allowed",
            level=current_level,
            gate="TrafficGate",
            metadata=metadata,
            bulkhead_acquired=bulkhead_acquired,
            bulkhead_name=bulkhead_name if bulkhead_acquired else None,
        )

    def _release_bulkhead_internal(self, bulkhead_name: str) -> None:
        """Internal bulkhead release."""
        try:
            from baldur.services.bulkhead.registry import get_bulkhead_registry

            bulkhead = get_bulkhead_registry().get(bulkhead_name)
            bulkhead.release()
        except Exception as e:
            logger.warning(
                "traffic_gate.release_bulkhead_failed",
                error=e,
            )

    def release_bulkhead(self, bulkhead_name: str) -> None:
        """
        Release the bulkhead resource.

        Must be invoked after the operation completes when should_allow()
        returned bulkhead_acquired=True.

        Args:
            bulkhead_name: name of the bulkhead to release
        """
        self._release_bulkhead_internal(bulkhead_name)

    def get_level(self) -> BackpressureLevel:
        """Return the current backpressure level."""
        return self._rate_controller.get_state().level


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_traffic_gate, configure_traffic_gate, reset_traffic_gate = make_singleton_factory(
    "traffic_gate", TrafficGate
)


def create_traffic_gate_with_cascade_load_shedding(
    buffer_size_provider: Any = None,
    buffer_capacity: int = 10000,
) -> TrafficGate:
    """
    Create a TrafficGate wired to CascadeLoadShedding.

    Configures CascadeLoadShedding automatically and attaches it to the
    TrafficGate.

    Args:
        buffer_size_provider: function or object providing the buffer size
            - Callable[[], int]: returns the buffer size
            - RingBuffer: has __len__
            - None: defaults to 0
        buffer_capacity: maximum buffer capacity

    Returns:
        TrafficGate wired to CascadeLoadShedding

    Usage:
        from baldur.audit.ring_buffer import RingBuffer

        buffer = RingBuffer(capacity=10000)
        gate = create_traffic_gate_with_cascade_load_shedding(
            buffer_size_provider=buffer,
            buffer_capacity=10000,
        )

        decision = gate.should_allow(priority=5)
    """
    try:
        from baldur.audit.cascade_load_shedding import CascadeLoadShedding
    except ImportError:
        logger.warning("traffic_gate.cascade_load_shedding_unavailable")
        return TrafficGate()

    # Build the buffer size provider
    def get_buffer_size() -> int:
        if buffer_size_provider is None:
            return 0
        if callable(buffer_size_provider):
            return buffer_size_provider()
        if hasattr(buffer_size_provider, "__len__"):
            return len(buffer_size_provider)
        return 0

    # CascadeLoadShedding wrapper class
    class LoadSheddingAdapter:
        """CascadeLoadShedding adapter."""

        def __init__(self):
            self._shedding = CascadeLoadShedding()
            self._buffer_capacity = buffer_capacity

        def should_accept(self, priority: int = 0, **kwargs: Any) -> dict:
            """
            should_accept wrapper.

            CascadeLoadShedding.should_accept() needs trigger_type, buffer_size,
            and buffer_capacity.
            """
            return self._shedding.should_accept(
                trigger_type="traffic_gate",
                buffer_size=get_buffer_size(),
                buffer_capacity=self._buffer_capacity,
                priority=None,  # priority is inferred from trigger_type
            )

    adapter = LoadSheddingAdapter()

    return TrafficGate(load_shedding=adapter)


# Global instance (for convenient access)
traffic_gate = get_traffic_gate()
