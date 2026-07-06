# baldur.interfaces — Core State Repositories

The two highest-traffic repository interfaces: failed-operation persistence and
circuit-breaker state. Adapter authors implement these to back Baldur on a new
storage layer. The shared enums and DTOs live on the data-model page.

::: baldur.interfaces.FailedOperationRepository

::: baldur.interfaces.CircuitBreakerStateRepository
