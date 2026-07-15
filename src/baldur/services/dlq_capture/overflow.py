"""DLQ overflow — synchronous check + OSS strategy-faithful enforcement.

Home of the size-based overflow *check* (periodic-N sampling + emergency
override) shared by every tier, plus the OSS-tier *enforcement* seam. The PRO
tier re-exports the check and overlays its own background (lazy) eviction; the
OSS tier enforces synchronously in the store path via
:func:`enforce_overflow_eviction`.

Design:
- ``store_failure()`` path: O(1) ZCARD check only (periodic-N below the
  emergency threshold, every-store above it).
- ``reject`` strategy: synchronous rejection at store time.
- ``drop_oldest`` (shared default): OSS evicts the oldest synchronously, PRO
  defers to a background worker.
- ``compress_oldest``: PRO summarizes + evicts in the background; OSS degrades
  to ``drop_oldest`` (warn-once).
"""

from __future__ import annotations

import itertools
from enum import Enum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from baldur.interfaces.repositories import FailedOperationRepository
    from baldur.settings.dlq import DLQSettings

logger = structlog.get_logger()

__all__ = [
    "DLQOverflowStrategy",
    "OverflowResult",
    "enforce_overflow_eviction",
    "handle_overflow",
    "reset_overflow_state",
]

# =============================================================================
# Periodic-N + emergency override state.
#
# ``store_failure`` previously paid an unconditional ``count_all + count_by_domain``
# RLock acquire on every call — pure overhead at DLQ size << max_size (the
# steady-state common case). The algorithm samples at intervals of
# ``settings.overflow_check_interval`` below the emergency threshold, and falls
# back to full-check on every call once the last-observed ratio crosses
# ``emergency_purge_threshold``.
#
# Both module-level mutables are GIL-atomic (``itertools.count.__next__`` /
# single store/load on a float). ``reset_overflow_state()`` is wired into
# ``baldur.protect_facade.reset_protect_caches`` so test fixtures get a
# deterministic ``n=0`` start.
# =============================================================================

_overflow_check_counter: itertools.count = itertools.count()
_overflow_last_ratio: float = 0.0

# Warn-once flags for the OSS enforcement seam. A sustained at-cap condition or
# a misconfigured non-default strategy must not flood logs, so the soft-cap
# (all-candidates-protected) WARNING and the compress→drop degrade WARNING each
# fire at most once per process (reset with the periodic-N state for test
# isolation).
_compress_degrade_warned: bool = False
_soft_cap_warned: bool = False

# Inline eviction floor — evict at least one entry when an overflow is detected.
# Domain-invariant guard, not an operational tunable.
_OVERFLOW_INLINE_EVICT_MIN = 1


def reset_overflow_state() -> None:
    """Reset the periodic-N counter, last-ratio cache, and warn-once flags."""
    global _overflow_check_counter, _overflow_last_ratio
    global _compress_degrade_warned, _soft_cap_warned
    _overflow_check_counter = itertools.count()
    _overflow_last_ratio = 0.0
    _compress_degrade_warned = False
    _soft_cap_warned = False


class DLQOverflowStrategy(str, Enum):
    """DLQ overflow strategy."""

    DROP_OLDEST = "drop_oldest"
    REJECT = "reject"
    COMPRESS_OLDEST = "compress_oldest"


class OverflowResult:
    """Overflow check result.

    ``overflow_scope`` records which cap triggered a detected overflow
    (``"domain"`` or ``"global"``) so synchronous enforcement can evict from
    the right bucket. Empty when no overflow was detected.
    """

    def __init__(
        self,
        *,
        accepted: bool,
        overflow_detected: bool = False,
        evicted_count: int = 0,
        reason: str = "",
        overflow_scope: str = "",
    ):
        self.accepted = accepted
        self.overflow_detected = overflow_detected
        self.evicted_count = evicted_count
        self.reason = reason
        self.overflow_scope = overflow_scope


def _do_full_overflow_check(
    repository: FailedOperationRepository,
    settings: DLQSettings,
    domain: str,
) -> tuple[OverflowResult, int]:
    """Run the full ZCARD-based overflow check and return ``(result, total_count)``.

    ``total_count`` is returned alongside the result so the periodic-N
    fast-path can update ``_overflow_last_ratio`` without paying a second
    ``count_all`` call.
    """
    strategy = DLQOverflowStrategy(settings.overflow_strategy)

    # 1. Global size check — ZCARD O(1)
    total_count = repository.count_all()
    if total_count < settings.max_size:
        # 2. Per-domain size check — ZCARD O(1)
        domain_count = repository.count_by_domain(domain)
        if domain_count < settings.max_size_per_domain:
            return OverflowResult(accepted=True), total_count

        # Domain limit exceeded
        logger.warning(
            "dlq.domain_overflow_triggered",
            domain=domain,
            domain_count=domain_count,
            max_size_per_domain=settings.max_size_per_domain,
        )
        if strategy == DLQOverflowStrategy.REJECT:
            return (
                OverflowResult(accepted=False, reason="domain_capacity_exceeded"),
                total_count,
            )
        # drop_oldest / compress_oldest → accept; enforcement evicts the domain
        return (
            OverflowResult(
                accepted=True, overflow_detected=True, overflow_scope="domain"
            ),
            total_count,
        )

    # Global limit exceeded
    logger.warning(
        "dlq.global_overflow_triggered",
        total_count=total_count,
        max_size=settings.max_size,
    )
    if strategy == DLQOverflowStrategy.REJECT:
        return (
            OverflowResult(accepted=False, reason="dlq_capacity_exceeded"),
            total_count,
        )

    # drop_oldest / compress_oldest → accept; enforcement evicts globally
    return (
        OverflowResult(accepted=True, overflow_detected=True, overflow_scope="global"),
        total_count,
    )


def handle_overflow(
    repository: FailedOperationRepository,
    settings: DLQSettings,
    domain: str,
) -> OverflowResult:
    """
    DLQ overflow check (synchronous, O(1)) with periodic-N skip and
    emergency override.

    Below ``settings.emergency_purge_threshold`` ratio every Nth store
    performs the full ZCARD check (where N=``settings.overflow_check_interval``);
    intermediate stores fast-path with ``accepted=True``. Above the threshold
    every store performs the full check — drift = 0 in the danger zone.
    Last-observed ratio is cached in module-level ``_overflow_last_ratio``
    to gate the threshold check without an extra ``count_all`` per skipped call.
    """
    global _overflow_last_ratio

    # Emergency window: bypass periodic-N to keep drift = 0 near capacity.
    if _overflow_last_ratio > settings.emergency_purge_threshold:
        result, total_count = _do_full_overflow_check(repository, settings, domain)
        if settings.max_size > 0:
            _overflow_last_ratio = total_count / settings.max_size
        return result

    # Steady-state regime: skip the full check until the next interval boundary.
    n = next(_overflow_check_counter)
    if n % settings.overflow_check_interval != 0:
        return OverflowResult(accepted=True)

    result, total_count = _do_full_overflow_check(repository, settings, domain)
    if settings.max_size > 0:
        _overflow_last_ratio = total_count / settings.max_size
    return result


def enforce_overflow_eviction(
    repository: FailedOperationRepository,
    settings: DLQSettings,
    domain: str,
    overflow_result: OverflowResult,
) -> None:
    """Synchronously evict the oldest entries to bound the queue (OSS tier).

    Called from the store orchestrator when the check reports
    ``overflow_detected`` under a non-reject strategy:

    - ``drop_oldest`` → evict the oldest entries from the overflowing bucket
      (REPLAYING/REVIEWING protected by ``evict_oldest``), then accept.
    - ``compress_oldest`` → compression is a PRO-tier capability; degrade to
      ``drop_oldest`` semantics with a warn-once.
    - all candidates protected (evicted == 0) → accept over the soft cap with a
      warn-once; stale REPLAYING entries are released back to PENDING within
      ``stale_replaying_timeout_minutes``.

    Fail-open: the caller wraps this so any exception degrades to accept.
    """
    global _compress_degrade_warned, _soft_cap_warned

    if (
        settings.overflow_strategy == DLQOverflowStrategy.COMPRESS_OLDEST.value
        and not _compress_degrade_warned
    ):
        _compress_degrade_warned = True
        logger.warning(
            "dlq.overflow_strategy_degraded",
            requested=settings.overflow_strategy,
            applied=DLQOverflowStrategy.DROP_OLDEST.value,
            reason="compression is a PRO-tier capability; degrading to drop_oldest",
        )

    # Evict from the bucket that overflowed. Batch = the periodic-N interval so
    # the eviction covers the sampling drift (up to N stores can slip past
    # between checks below the emergency threshold).
    evict_domain = domain if overflow_result.overflow_scope == "domain" else None
    batch = max(_OVERFLOW_INLINE_EVICT_MIN, settings.overflow_check_interval)
    evicted = repository.evict_oldest(batch, domain=evict_domain)

    if evicted == 0 and not _soft_cap_warned:
        _soft_cap_warned = True
        logger.warning(
            "dlq.overflow_soft_cap_all_protected",
            domain=domain,
            scope=overflow_result.overflow_scope,
            hint=(
                "all overflow eviction candidates are REPLAYING/REVIEWING; "
                "accepting over the soft cap until stale entries are released"
            ),
        )
