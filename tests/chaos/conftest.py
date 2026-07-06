"""
Chaos Engineering Test Fixtures for baldur package.

Provides specialized fixtures for chaos engineering tests including
failure injectors, latency simulators, and resource trackers.
"""

import random
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import pytest

# =============================================================================
# Failure Injection Framework
# =============================================================================


@dataclass
class FailureInjector:
    """
    Configurable failure injection for chaos testing.

    Tracks injection statistics for validation.
    """

    failure_rate: float = 0.3
    total_calls: int = 0
    failed_calls: int = 0
    success_calls: int = 0
    call_history: list = field(default_factory=list)

    def should_fail(self) -> bool:
        """Determine if the current call should fail based on rate."""
        self.total_calls += 1
        should_fail = random.random() < self.failure_rate
        if should_fail:
            self.failed_calls += 1
        else:
            self.success_calls += 1
        self.call_history.append(
            {
                "timestamp": datetime.now(),
                "failed": should_fail,
            }
        )
        return should_fail

    def reset(self) -> None:
        """Reset all counters."""
        self.total_calls = 0
        self.failed_calls = 0
        self.success_calls = 0
        self.call_history.clear()

    @property
    def actual_failure_rate(self) -> float:
        """Calculate actual observed failure rate."""
        if self.total_calls == 0:
            return 0.0
        return self.failed_calls / self.total_calls

    def get_stats(self) -> dict:
        """Get injection statistics."""
        return {
            "total_calls": self.total_calls,
            "failed_calls": self.failed_calls,
            "success_calls": self.success_calls,
            "actual_failure_rate": self.actual_failure_rate,
            "configured_rate": self.failure_rate,
        }


@dataclass
class BurstFailureInjector:
    """
    Burst failure pattern injector.

    Injects consecutive failures in bursts.
    """

    burst_size: int = 10
    burst_interval: int = 50  # Calls between bursts
    current_burst_count: int = 0
    calls_since_burst: int = 0
    in_burst: bool = False
    total_calls: int = 0
    failed_calls: int = 0

    def should_fail(self) -> bool:
        """Determine if call should fail based on burst pattern."""
        self.total_calls += 1

        if self.in_burst:
            self.current_burst_count += 1
            if self.current_burst_count >= self.burst_size:
                self.in_burst = False
                self.current_burst_count = 0
                self.calls_since_burst = 0
            self.failed_calls += 1
            return True
        else:
            self.calls_since_burst += 1
            if self.calls_since_burst >= self.burst_interval:
                self.in_burst = True
            return False

    def reset(self) -> None:
        """Reset burst state."""
        self.current_burst_count = 0
        self.calls_since_burst = 0
        self.in_burst = False
        self.total_calls = 0
        self.failed_calls = 0


@dataclass
class LatencyInjector:
    """
    Latency injection for slow degradation testing.

    Can simulate gradual latency increase over time.
    """

    min_latency_ms: int = 100
    max_latency_ms: int = 2000
    degradation_rate: float = 0.0  # ms per call increase
    current_base_latency: float = 0
    total_calls: int = 0
    total_latency_ms: float = 0
    latency_history: list = field(default_factory=list)

    def inject_latency(self) -> int:
        """
        Inject latency delay.

        Returns the actual latency applied in milliseconds.
        """
        self.total_calls += 1

        # Calculate base latency with degradation
        base = self.min_latency_ms + self.current_base_latency
        jitter = random.randint(0, min(500, self.max_latency_ms - int(base)))
        latency = min(int(base + jitter), self.max_latency_ms)

        # Apply degradation for next call
        self.current_base_latency += self.degradation_rate

        # Track statistics
        self.total_latency_ms += latency
        self.latency_history.append(
            {
                "timestamp": datetime.now(),
                "latency_ms": latency,
            }
        )

        return latency

    @property
    def average_latency(self) -> float:
        """Get average latency in ms."""
        if self.total_calls == 0:
            return 0.0
        return self.total_latency_ms / self.total_calls

    def reset(self) -> None:
        """Reset latency state."""
        self.current_base_latency = 0
        self.total_calls = 0
        self.total_latency_ms = 0
        self.latency_history.clear()


@dataclass
class ResourceExhaustionSimulator:
    """
    Simulates resource exhaustion scenarios.

    Tracks resource usage for connection pool, memory, etc.
    """

    max_connections: int = 100
    current_connections: int = 0
    connection_history: list = field(default_factory=list)
    exhaustion_events: list = field(default_factory=list)

    def acquire_connection(self) -> bool:
        """
        Attempt to acquire a connection.

        Returns True if successful, False if pool exhausted.
        """
        if self.current_connections >= self.max_connections:
            self.exhaustion_events.append(
                {
                    "timestamp": datetime.now(),
                    "current": self.current_connections,
                    "max": self.max_connections,
                }
            )
            return False
        self.current_connections += 1
        self.connection_history.append(
            {
                "action": "acquire",
                "timestamp": datetime.now(),
                "current": self.current_connections,
            }
        )
        return True

    def release_connection(self) -> None:
        """Release a connection back to the pool."""
        if self.current_connections > 0:
            self.current_connections -= 1
            self.connection_history.append(
                {
                    "action": "release",
                    "timestamp": datetime.now(),
                    "current": self.current_connections,
                }
            )

    @property
    def is_exhausted(self) -> bool:
        """Check if resource pool is exhausted."""
        return self.current_connections >= self.max_connections

    def reset(self) -> None:
        """Reset all connections."""
        self.current_connections = 0
        self.connection_history.clear()
        self.exhaustion_events.clear()


# =============================================================================
# Context Manager for Chaos Injection
# =============================================================================


@contextmanager
def chaos_injection_context(
    failure_rate: float = 0.3,
    latency_ms: int = 0,
) -> Generator[dict, None, None]:
    """
    Context manager for scoped chaos injection.

    Provides clean setup and teardown for chaos tests.
    """
    context = {
        "failure_injector": FailureInjector(failure_rate=failure_rate),
        "latency_injector": LatencyInjector(min_latency_ms=latency_ms),
        "start_time": datetime.now(),
        "events": [],
    }
    try:
        yield context
    finally:
        context["end_time"] = datetime.now()
        context["duration"] = (
            context["end_time"] - context["start_time"]
        ).total_seconds()


# =============================================================================
# Pytest Fixtures
# =============================================================================


@pytest.fixture
def failure_injector():
    """Provides a configurable failure injector."""
    return FailureInjector(failure_rate=0.3)


@pytest.fixture
def burst_failure_injector():
    """Provides a burst failure pattern injector."""
    return BurstFailureInjector(burst_size=10, burst_interval=50)


@pytest.fixture
def latency_injector():
    """Provides a latency injector for slow degradation tests."""
    return LatencyInjector(
        min_latency_ms=100,
        max_latency_ms=30000,  # 30 seconds max
        degradation_rate=100,  # 100ms increase per call
    )


@pytest.fixture
def resource_simulator():
    """Provides a resource exhaustion simulator."""
    return ResourceExhaustionSimulator(max_connections=100)


@pytest.fixture
def chaos_context():
    """
    Provides a context for tracking chaos test state.

    Useful for complex chaos scenarios.
    """
    return {
        "start_time": datetime.now(),
        "events": [],
        "failures": [],
        "recoveries": [],
        "metrics": {},
    }
