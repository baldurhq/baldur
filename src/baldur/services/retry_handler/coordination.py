"""
Outbound 429 coordination — key identity and admission, shared by both retry stages.

The synchronous ``RetryPolicy`` and the asynchronous ``AsyncRetryPolicy`` both
coordinate outbound 429s through the shared ``RateLimitCoordinator`` by
default. The rules that decide *whether* a call coordinates and *under which
key* live here, once, so the two stages cannot disagree:

- :func:`coordination_key` — the identity rule (``rate_limit_key`` or,
  failing that, ``domain``).
- :func:`coordination_admitted` — the resolution order: per-policy opt-out,
  then the deployment kill switch, then the identity gate. The gate is the
  only conjunct that logs, which is why it runs last: an operator who turned
  coordination off must not be told to configure the thing they disabled.
- :func:`resolve_coordinator_sync` — the synchronous resolution the sync stage
  uses (injection wins over every lever; any fault degrades to ``None``).

The once-per-key WARNING behind the identity gate is deduplicated by a
process-wide set. It lives here rather than in either policy module so that
the two stages share one dedup record — two copies would warn once per key
*per module*, breaking the "one WARNING line per key per process" contract.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog

from .rate_limit_detection import UNIDENTIFIED_COORDINATION_KEY

if TYPE_CHECKING:
    from baldur.services.rate_limit_coordinator import RateLimitCoordinator

__all__ = [
    "coordination_admitted",
    "coordination_key",
    "resolve_coordinator_sync",
]

logger = structlog.get_logger()

# Coordination keys already warned about, so the unidentified-domain diagnostic
# costs one WARNING line per key per process rather than one per call.
_unidentified_key_warned: set[str] = set()
_unidentified_key_warned_lock = threading.Lock()


def coordination_key(rate_limit_key: str | None, domain: str) -> str:
    """The key a policy's outbound 429 cooldowns are shared under.

    Single expression for the identity gate, the coordinator calls, and the
    deferral error alike, so no two of them can disagree about what counts as
    an override. ``or`` rather than ``is not None``: an override set to the
    empty string is not an identity, and treating it as one would let the gate
    pass while the key fell back to the placeholder — coordinating unrelated
    downstreams on one record, silently.
    """
    return rate_limit_key or domain


def coordination_admitted(
    *,
    rate_limit_aware: bool,
    rate_limit_key: str | None,
    domain: str,
) -> bool:
    """Whether a policy with these fields coordinates outbound 429s by default.

    Three conjuncts, in a load-bearing order:

    1. ``rate_limit_aware`` (per-policy / per-domain opt-out), then
    2. the deployment kill switch
       (``BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED``), then
    3. the coordination key is *identified* (not the placeholder domain).

    Both levers are checked before the identity gate because the identity
    gate is the only conjunct that logs. The settings read is not wrapped
    here: the caller owns the fail-open wrap around the whole resolution.
    """
    if not rate_limit_aware:
        return False

    from baldur.settings.rate_limit_backoff import get_rate_limit_backoff_settings

    if not get_rate_limit_backoff_settings().coordination_enabled:
        return False

    key = coordination_key(rate_limit_key, domain)
    if key == UNIDENTIFIED_COORDINATION_KEY:
        _warn_unidentified_coordination_key(key)
        return False

    return True


def resolve_coordinator_sync(
    *,
    injected: RateLimitCoordinator | None,
    rate_limit_aware: bool,
    rate_limit_key: str | None,
    domain: str,
) -> RateLimitCoordinator | None:
    """Resolve the coordinator a synchronous call should coordinate through.

    An explicitly injected coordinator always wins — it bypasses both opt-out
    levers, because a caller who constructed one asked for it. Otherwise the
    process-wide singleton is resolved when :func:`coordination_admitted`
    holds.

    Fail-open: any resolution fault (settings read, storage auto-detect,
    Redis connect) degrades to ``None`` — no coordination — never to a
    failed business call.
    """
    if injected is not None:
        return injected

    try:
        if not coordination_admitted(
            rate_limit_aware=rate_limit_aware,
            rate_limit_key=rate_limit_key,
            domain=domain,
        ):
            return None

        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        return RateLimitCoordinator.get_instance()
    except Exception as resolution_error:
        logger.warning(
            "retry.rate_limit_coordinator_resolution_failed",
            error=str(resolution_error),
            domain=domain,
        )
        return None


def _warn_unidentified_coordination_key(key: str) -> None:
    """Warn once per key that outbound 429 coordination is inert here.

    WARNING rather than DEBUG on purpose: this is the only runtime signal
    that a default-on protection is not actually protecting this call site,
    and DEBUG is off under any production log configuration — the operator
    who most needs the line would never see it. The once-per-key dedup is
    what makes that level affordable.
    """
    with _unidentified_key_warned_lock:
        if key in _unidentified_key_warned:
            return
        _unidentified_key_warned.add(key)

    logger.warning(
        "retry.rate_limit_coordination_skipped",
        reason="unidentified_domain",
        domain=key,
        remedy=(
            "pass an explicit domain (or a RetryPolicyConfig.rate_limit_key) "
            "so outbound 429 cooldowns are shared per downstream"
        ),
    )
