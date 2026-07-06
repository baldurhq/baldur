"""GIL contention probe for Meta-Watchdog.

Measures GIL contention indirectly via scheduling delay: calls
time.sleep(0) to yield the GIL, then measures how long it takes
to reacquire it. Under normal conditions P90 < 0.1ms; under heavy
GIL contention it can reach several milliseconds.
"""

import time

from baldur.meta.health_probe import HealthStatus


class GILContentionProbe:
    """GIL contention probe using scheduling delay measurement.

    Thresholds:
    - HEALTHY: P90 < 1ms
    - DEGRADED: 1ms <= P90 < 5ms
    - UNHEALTHY: P90 >= 5ms
    """

    DEGRADED_THRESHOLD_MS = 1.0
    UNHEALTHY_THRESHOLD_MS = 5.0

    def check(self) -> tuple[HealthStatus, dict]:
        delays_ns = []
        for _ in range(10):
            t0 = time.perf_counter_ns()
            time.sleep(0)
            delays_ns.append(time.perf_counter_ns() - t0)

        delays_ns.sort()
        p90_ms = delays_ns[8] / 1_000_000

        details = {
            "p90_ms": round(p90_ms, 3),
            "min_ms": round(delays_ns[0] / 1_000_000, 3),
            "max_ms": round(delays_ns[-1] / 1_000_000, 3),
        }

        if p90_ms >= self.UNHEALTHY_THRESHOLD_MS:
            return HealthStatus.UNHEALTHY, details
        if p90_ms >= self.DEGRADED_THRESHOLD_MS:
            return HealthStatus.DEGRADED, details
        return HealthStatus.HEALTHY, details
