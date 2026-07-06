"""
Framework-free middleware helpers for Baldur.

This module provides pure functions that implement the decision layer of
Baldur's middleware (rate limiting, backpressure, circuit-breaker pre-flight,
post-response observation) without any framework coupling.

Three responsibility classes:

1. **Reject decision** — returns ``ResponseContext | None``. ``None`` means
   "allow request to proceed", a ``ResponseContext`` means "reject with this
   response". Reuses the existing ``ResponseContext`` DTO at
   ``interfaces/web_framework.py:168`` to preserve Retry-After / X-RateLimit-*
   / X-Baldur-Backpressure-Level headers on the rejection response.

2. **Success-side header injection** — mutates a ``dict[str, str]`` of
   response headers, returns ``None``. Required because the reject pattern
   only handles failure paths; the existing Django middleware also adds
   X-RateLimit-Remaining / X-Baldur-Backpressure-Level to successful
   responses.

3. **Post-response observation** — returns ``None``, pure side-effect.
   Records 5xx as CB failure, 2xx/3xx as CB success.

Framework adapters (Django, FastAPI, Flask) compose these helpers into
framework-native middleware classes / hooks. The helpers themselves know
nothing about Django's HttpRequest, FastAPI's Request, or Flask's request
proxy — they accept ``RequestContext`` (the same DTO used by ``HandlerFunc``).

Reference: ``429`` Part 3 D9 and
Convergence Note Pass 7.
"""

from __future__ import annotations

from baldur.api.middleware.admission import (
    AdmissionDecision,
    check_admission,
)
from baldur.api.middleware.backpressure import (
    apply_backpressure_headers,
    check_backpressure,
)
from baldur.api.middleware.circuit_breaker import (
    check_cb_open,
    record_cb_observation,
)
from baldur.api.middleware.deadline import (
    check_deadline,
    record_rtt_sample,
)
from baldur.api.middleware.http_metrics import (
    record_http_red,
)
from baldur.api.middleware.rate_limit import (
    apply_rate_limit_headers,
    check_rate_limit,
)

__all__ = [
    "check_rate_limit",
    "apply_rate_limit_headers",
    "check_backpressure",
    "apply_backpressure_headers",
    "check_cb_open",
    "record_cb_observation",
    "check_deadline",
    "record_rtt_sample",
    "record_http_red",
    "check_admission",
    "AdmissionDecision",
]
