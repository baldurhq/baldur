# baldur.interfaces — Config, Runtime & Domain State Stores

The configuration-provider interface and its defaults, the database-health and
session-invalidation providers, the runtime-config manager, and the per-domain
state-store contracts (canary / chaos / config history / cross-cluster).

## Configuration provider

::: baldur.interfaces.ConfigProviderInterface

::: baldur.interfaces.DictConfigProvider

::: baldur.interfaces.EnvConfigProvider

## Database & session

::: baldur.interfaces.DatabaseConnectionInfo

::: baldur.interfaces.DatabaseHealthProvider

::: baldur.interfaces.SessionInvalidationProvider

## Runtime config

::: baldur.interfaces.RuntimeConfigManager

## Domain state stores

::: baldur.interfaces.CanaryRolloutStore

::: baldur.interfaces.ChaosExperimentStore

::: baldur.interfaces.ConfigHistoryStore

::: baldur.interfaces.CrossClusterStore
