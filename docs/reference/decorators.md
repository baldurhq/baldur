# baldur.decorators — Resilience Decorators

Opinionated `@protected` presets and orthogonal gates. The primitive
`@protected` / `@aprotected` lives at the top level; this module hosts
preset compositions (`@dlq_protect`) and orthogonal call-site gates
(`@idempotent`, `@rate_limit`, `@domain_tag`).

!!! note "See also"
    [Django quickstart](../getting-started/django.md) — end-to-end decorator
    wiring on a real example app.

## Decorators

::: baldur.decorators.dlq_protect.dlq_protect

::: baldur.decorators.idempotent.idempotent

::: baldur.decorators.rate_limit.rate_limit

::: baldur.decorators.domain_tag.domain_tag

## Context manager

::: baldur.decorators.DomainContext

## Helpers

::: baldur.decorators.get_current_domain

::: baldur.decorators.clear_domain_context

## Exceptions raised by the decorators above

::: baldur.decorators.IdempotencyDuplicateError

::: baldur.decorators.RateLimitExceeded

::: baldur.decorators.DomainValidationError
