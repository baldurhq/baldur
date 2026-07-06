# baldur.interfaces — Policy Composition & ML Strategy

The resilience-policy composition protocols (guards, hooks, failure sinks, the
policy result/context DTOs) and the AI/ML strategy interfaces that back
anomaly detection, forecasting, and classification.

## Resilience policy

::: baldur.interfaces.PolicyOutcome

::: baldur.interfaces.PolicyResult

::: baldur.interfaces.PolicyContext

::: baldur.interfaces.GuardResult

::: baldur.interfaces.ResiliencePolicy

::: baldur.interfaces.AsyncResiliencePolicy

::: baldur.interfaces.PolicyGuard

::: baldur.interfaces.PolicyHook

::: baldur.interfaces.FailureSink

::: baldur.interfaces.AsyncPolicyGuard

::: baldur.interfaces.AsyncPolicyHook

::: baldur.interfaces.AsyncFailureSink

## ML strategy

::: baldur.interfaces.AnomalyDetectionStrategy

::: baldur.interfaces.ForecastStrategy

::: baldur.interfaces.ClassificationStrategy

::: baldur.interfaces.BatchDetectable

::: baldur.interfaces.BatchClassifiable

::: baldur.interfaces.OptimizationStrategy

::: baldur.interfaces.StrategyLifecycle
