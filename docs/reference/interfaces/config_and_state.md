# baldur.interfaces — Config & Domain State Stores

The configuration-provider interface and its defaults, the database-health and
session-invalidation providers, and the per-domain state-store contracts
(canary / config history / cross-cluster).

## Configuration provider

::: baldur.interfaces.ConfigProviderInterface

::: baldur.interfaces.DictConfigProvider

::: baldur.interfaces.EnvConfigProvider

## Database & session

::: baldur.interfaces.DatabaseConnectionInfo

::: baldur.interfaces.DatabaseHealthProvider

::: baldur.interfaces.SessionInvalidationProvider

## Domain state stores

::: baldur.interfaces.CanaryRolloutStore

::: baldur.interfaces.ConfigHistoryStore

::: baldur.interfaces.CrossClusterStore
