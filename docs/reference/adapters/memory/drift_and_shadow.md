# baldur.adapters.memory — Drift Reconciliation & Shadow Logging

The layered (L1+L2) consistency machinery: drift reconciliation between the
in-memory tier and its backing store, and the shadow logger that records L2
sync failures for later replay.

## Drift reconciliation

::: baldur.adapters.memory.DriftReconciler

::: baldur.adapters.memory.DriftReconciliationResult

::: baldur.adapters.memory.DriftReconciliationRecord

::: baldur.adapters.memory.get_drift_reconciler

## Shadow logging

::: baldur.adapters.memory.ShadowLogger

::: baldur.adapters.memory.L2SyncFailureRecord

::: baldur.adapters.memory.get_shadow_logger
