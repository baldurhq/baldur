"""
Baldur Decorators Package.

Public decorator API for Baldur self-healing primitives.

Decorators:
- @domain_tag: Auto-tag domain on errors raised inside the wrapped scope
- DomainContext: with-statement domain context
- @dlq_protect: PRO-aliased preset of @protected (dlq+retry+CB pinned on)
- @idempotent: Atomic do-not-run-twice gate over IdempotencyGate
- @rate_limit: Function-level sliding-window rate limiter

Exceptions re-exported for convenience:
- IdempotencyDuplicateError: raised by @idempotent on SKIP/ABORT
- RateLimitExceeded: raised by @rate_limit when limiter rejects

Reference:
    458

Status: Public
"""

from baldur.core.exceptions import (
    DomainValidationError,
    IdempotencyDuplicateError,
    RateLimitExceeded,
)
from baldur.decorators.dlq_protect import dlq_protect
from baldur.decorators.domain_tag import (
    DomainContext,
    clear_domain_context,
    domain_tag,
    get_current_domain,
)
from baldur.decorators.idempotent import idempotent
from baldur.decorators.rate_limit import rate_limit

__all__ = [
    "domain_tag",
    "DomainContext",
    "get_current_domain",
    "clear_domain_context",
    "dlq_protect",
    "idempotent",
    "rate_limit",
    "IdempotencyDuplicateError",
    "RateLimitExceeded",
    "DomainValidationError",
]
