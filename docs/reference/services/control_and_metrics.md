# baldur.services — Control API & Metrics

The control-API request/response surface for runtime operations, and the
metrics helpers. The metrics symbols resolve lazily and require the
`[prometheus]` extra at runtime.

## Control API

::: baldur.services.ControlAPIService

::: baldur.services.ControlRequest

::: baldur.services.ControlResponse

## Metrics

::: baldur.services.record_sla_breach

::: baldur.services.collect_all_metrics
