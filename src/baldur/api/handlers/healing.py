"""
Framework-agnostic healing-summary handler.

Serves the console's healing counters and per-row latency tokens straight from
the in-process metrics registry. The process answering the request IS the
process that recorded the numbers, so the payload's ``since`` stamp — this
module's import time, i.e. process start — describes them exactly.

Read-only and uncached by design: a cached body would be rebuilt by whichever
process happened to miss the cache, which would falsify that ``since`` label.

Every field is independently fail-open. A field whose source is unavailable is
**omitted** rather than reported as zero, so no rendered number can be a
fabrication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from prometheus_client.samples import Sample

logger = structlog.get_logger()

__all__ = ["healing_summary"]

# Counter epoch. Bound at module import, which the admin server reaches during
# route registration — strictly before it starts serving.
_COUNTER_EPOCH = utc_now()

_REPLAY_OUTCOMES = "baldur_replay_outcomes_total"
_WATCHDOG_ESCALATION = "baldur_watchdog_escalation_total"
_HTTP_DURATION = "baldur_http_request_duration_seconds"
_DLQ_REPLAY_DURATION = "baldur_dlq_replay_duration_seconds"
_DLQ_STORE_DURATION = "baldur_dlq_store_duration_seconds"

_FAMILIES = (
    _REPLAY_OUTCOMES,
    _WATCHDOG_ESCALATION,
    _HTTP_DURATION,
    _DLQ_REPLAY_DURATION,
    _DLQ_STORE_DURATION,
)

# Successful replays only, and only real ones — synthetic traffic entered
# through the test-mode context is another surface's number, not healing.
_HEALED_LABELS = {"outcome": "success", "is_synthetic": "false"}

# The escalation writer stamps this result only when a channel that leaves the
# host accepted the page. Every other result value means nobody was reached.
_PAGE_DELIVERED_LABELS = {"result": "sent"}

# Latency payload key -> histogram family.
_LATENCY_FAMILIES = (
    ("http_p95_seconds", _HTTP_DURATION),
    ("dlq_replay_p95_seconds", _DLQ_REPLAY_DURATION),
    ("dlq_store_p95_seconds", _DLQ_STORE_DURATION),
)


def healing_summary(ctx: RequestContext) -> ResponseContext:
    """Report what this process healed, and how fast, since it started."""
    from baldur.adapters.prometheus_adapter import p95_from_buckets, sum_counter
    from baldur.utils.tier import is_pro_installed

    families = _collect_metric_families()

    counters: dict[str, int] = {}
    healed = families.get(_REPLAY_OUTCOMES)
    if healed is not None:
        counters["replayed"] = sum_counter(healed, _HEALED_LABELS)

    # PRO-only field: the OSS tree has no escalation writer at all, so an
    # OSS-visible count could only ever be a permanently-zero claim about a
    # capability this tier does not have.
    if is_pro_installed():
        paged = families.get(_WATCHDOG_ESCALATION)
        if paged is not None:
            counters["humans_paged"] = sum_counter(paged, _PAGE_DELIVERED_LABELS)

    latency: dict[str, float] = {}
    for key, family in _LATENCY_FAMILIES:
        samples = families.get(family)
        if samples is None:
            continue
        p95 = p95_from_buckets(samples)
        if p95 is not None:
            latency[key] = p95

    payload: dict[str, Any] = {"since": _COUNTER_EPOCH.isoformat()}
    if counters:
        payload["counters"] = counters
    if latency:
        payload["latency"] = latency

    return ResponseContext.json(payload)


def _collect_metric_families() -> dict[str, list[Sample]]:
    """Read every family this handler needs in a single registry walk.

    Returns an empty mapping when there is no metrics backend to read (the
    Prometheus client is absent, or metric initialization failed) — every
    counter and latency field is then omitted from the payload.
    """
    try:
        from baldur.adapters.prometheus_adapter import get_prometheus_adapter
        from baldur.metrics.prometheus import get_metrics

        # Recorders construct their families on first access, so this call is
        # what makes the families exist at all in a process that has not
        # recorded anything yet.
        if get_metrics() is None:
            return {}

        adapter = get_prometheus_adapter()
        if adapter is None:
            return {}

        return adapter.collect_families(_FAMILIES)
    except Exception as e:
        logger.debug("healing.metric_collection_failed", error=str(e))
        return {}
