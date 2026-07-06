"""
Baldur bridges - adapter package for third-party resilience libraries.

Each subpackage wraps an external library so its primitives flow through
Baldur's ResiliencePolicy / PolicyComposer pipeline without losing the
library's native ergonomics:

- ``baldur.bridges.tenacity`` — bridges ``tenacity.Retrying`` into
  ``ResiliencePolicy[T]`` so existing ``@tenacity.retry`` users get
  Baldur observability (metrics, audit, RETRY_EXHAUSTED events) and
  Self-DDoS guards (AdaptiveRetryBudget, RateLimitCoordinator) without
  rewriting their retry config.

Reference:
    451
    memory/bridge-adapter-strategy.md

Status: Internal
"""

__all__: list[str] = []
