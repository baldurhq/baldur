# baldur.services — Service Access

The re-export facade for service getters and the core service classes. Resolve
the circuit-breaker and replay services here; the SLA-threshold helper exposes
the configured breach thresholds.

## Service getters

::: baldur.services.get_circuit_breaker_service

::: baldur.services.get_replay_service

::: baldur.services.get_sla_thresholds

## Core service classes

::: baldur.services.CircuitBreakerService

::: baldur.services.ReplayService

::: baldur.services.BatchReplayResult

::: baldur.services.ReplayResult
