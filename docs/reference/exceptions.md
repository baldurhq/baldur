# baldur.core.exceptions — Exception Hierarchy

Top-level re-export selection rule: a name is exported by
`baldur/__init__.py` iff it is **(a)** a domain base class, or **(b)** a
leaf class raised by code reachable from a top-level public surface.
Everything else stays in `baldur.core.exceptions` and is reachable via
the nested import path.

## When-to-catch (13 top-level exceptions)

| Exception | When raised | Recovery hint |
|-----------|-------------|---------------|
| `BaldurError` | Catch-all base | Last-resort logging + alert |
| `AdapterError` | Adapter base (Redis/SQL misuse) | Reconnect; fall back to in-memory |
| `AdapterNotFoundError` | `ProviderRegistry.resolve(...)` finds no match | Wire the missing adapter |
| `CircuitBreakerError` | CB-base — domain-specific subclasses | Do NOT retry — non-retryable |
| `DLQError` | DLQ-base | Inspect DLQ entry; manual replay if needed |
| `DLQReplayError` | `ReplayService.replay(...)` could not complete | Mark for manual review |
| `ResilienceError` | Resilience-pattern base | Domain-specific fallback |
| `RetryExhaustedError` | All retry attempts failed | Send to DLQ; alert on SLA breach |
| `TimeoutPolicyError` | `protect(timeout=...)` exceeded | Cancel downstream; degrade |
| `RateLimitExceeded` | `@rate_limit` rejected the call | Surface 429; honor `reset_at` |
| `IdempotencyDuplicateError` | `@idempotent` detected a duplicate | Return cached result; do NOT retry |
| `DomainValidationError` | Domain-tag validation rejected the call | Caller bug — fix the domain identifier |
| `ConfigurationError` | Settings or wiring misconfiguration | Crash fast — operator must fix env |

## Nested-only classes

::: baldur.core.exceptions
