# baldur.interfaces — Health, Pools & Quorum

Connection-pool and PostgreSQL-admin providers (stats, leak reports, health
status) and the quorum-witness protocol that prevents multi-region split-brain.

## PostgreSQL admin

::: baldur.interfaces.PgAdminProvider

::: baldur.interfaces.ConnectionStats

::: baldur.interfaces.AdvisoryLockResult

## Pool monitoring

::: baldur.interfaces.PoolInfoProvider

::: baldur.interfaces.ConnectionInfo

::: baldur.interfaces.LeakReport

::: baldur.interfaces.NoOpPoolStatsProvider

::: baldur.interfaces.PoolHealthStatus

::: baldur.interfaces.PoolStats

::: baldur.interfaces.PoolStatsProvider

::: baldur.interfaces.ConnectionPoolMonitor

## Quorum

::: baldur.interfaces.QuorumLease

::: baldur.interfaces.QuorumWitnessProtocol
