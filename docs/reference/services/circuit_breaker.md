# baldur.services — Circuit Breaker & Rate Limiting

Circuit-breaker configuration and result types, the manual open/close controls,
the breaker state enum, and the rate-limit tracker.

## Configuration & results

::: baldur.services.CircuitBreakerConfig

::: baldur.services.CircuitBreakerResult

::: baldur.services.CircuitState

## Manual controls

::: baldur.services.should_allow_request

::: baldur.services.force_open_circuit

::: baldur.services.force_close_circuit

## Rate limiting

::: baldur.services.RateLimitTracker

::: baldur.services.get_rate_limit_tracker
