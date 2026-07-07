# baldur.adapters.memory — Layered, Incident & Archive Repositories

The layered (L1+L2) circuit-breaker variant — Memory + Redis/DB for
high-throughput distributed access — plus the in-memory security-incident,
archive, and event-journal repositories.

::: baldur.adapters.memory.LayeredCircuitBreakerStateRepository

::: baldur.adapters.memory.InMemorySecurityIncidentRepository

::: baldur.adapters.memory.InMemoryCascadeEventArchiveRepository

::: baldur.adapters.memory.InMemoryRecoverySessionArchiveRepository

::: baldur.adapters.memory.InMemoryEventJournalRepository
