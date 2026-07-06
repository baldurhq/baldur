"""
DLQ Metric Event Handlers.

Provides event-driven metric updates without DB queries.

Key Features:
- SafeGauge: negative-value guard so the gauge stays >= 0 even after restart
- Dynamic Logging: log level can be adjusted at runtime via the API layer
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.metrics.registry import resolve_domain_label
from baldur.services.daily_report import get_daily_report_collector

if TYPE_CHECKING:
    from baldur.metrics.safe_gauge import SafeGauge

logger = structlog.get_logger()

# Lazy imports to avoid circular dependencies. ``_metrics_init_failed`` is
# the #485 D1d/G7 sticky flag: once an ImportError has been observed, every
# subsequent call short-circuits to None instead of re-running the failing
# import. Reset is wired into ``baldur.protect_facade.reset_protect_caches`` via
# ``reset_event_handler_cache()``.
_metrics_instance = None
_metrics_init_failed: bool = False
_safe_gauge_cache: dict[str, SafeGauge] = {}
_logging_config = None


def _get_metrics():
    """Get the metrics instance lazily."""
    global _metrics_instance, _metrics_init_failed
    if _metrics_instance is not None:
        return _metrics_instance
    if _metrics_init_failed:
        return None
    try:
        from baldur.metrics.prometheus import get_metrics

        _metrics_instance = get_metrics()
    except ImportError:
        _metrics_init_failed = True
        logger.warning("event_handler.metrics_unavailable_sticky")
        return None
    return _metrics_instance


def _get_logging_config():
    """Get the logging config instance lazily."""
    global _logging_config
    if _logging_config is None:
        try:
            from baldur.settings.event_logging import get_event_logging_config

            _logging_config = get_event_logging_config()
        except ImportError:
            return None
    return _logging_config


def _log_event(level_getter: str, event: str, **kw) -> None:
    """
    Log an event with dynamic log level from EventLoggingConfig.

    Args:
        level_getter: Method name on EventLoggingConfig (e.g., 'get_dlq_log_level')
        event: Structured log event name
        **kw: Structured logging keyword arguments
    """
    config = _get_logging_config()
    if config is None:
        # Fallback to INFO if config not available
        logger.info(event, **kw)
        return

    try:
        level_name = getattr(config, level_getter)()
        level = config.get_log_level_int(level_name)
        logger.log(level, event, **kw)
    except Exception:
        logger.info(event, **kw)


def _get_safe_pending_gauge() -> SafeGauge | None:
    """
    Get or create SafeGauge wrapper for dlq_pending_gauge.

    Returns SafeGauge instance that prevents negative values.
    """
    global _safe_gauge_cache

    if "dlq_pending" in _safe_gauge_cache:
        return _safe_gauge_cache["dlq_pending"]

    metrics = _get_metrics()
    if metrics is None:
        return None

    try:
        from baldur.metrics.safe_gauge import SafeGauge

        if hasattr(metrics, "dlq") and hasattr(metrics.dlq, "_pending_gauge"):
            safe_gauge = SafeGauge(metrics.dlq._pending_gauge)
            _safe_gauge_cache["dlq_pending"] = safe_gauge
            return safe_gauge
    except ImportError:
        logger.warning("event_handler.safegauge_unavailable_raw_fallback")

    return None


class DLQMetricEventHandler:
    """
    Handler that updates metrics in response to DLQ events.

    Pure in-memory counter manipulation — no DB queries. Callers invoke
    this handler from business logic whenever DLQ state changes.

    Design:
    - Counter: cumulative, push-only (100% accurate)
    - Histogram: observation on event, push-only (100% accurate)
    - Gauge: wrapped with SafeGauge to prevent negative values
      (~99% accurate, reconciled at restart)

    SafeGauge pattern:
        If a "resolved" event arrives right after a restart (Gauge == 0),
        the pending count would otherwise underflow to -1. SafeGauge
        clamps at 0 to avoid surfacing apparent inconsistency in
        technical DD / dashboards.

    Example:
        >>> handler = DLQMetricEventHandler()
        >>> # On DLQ entry creation
        >>> handler.on_item_created("payment", "PG_TIMEOUT")
        >>> # On DLQ resolution
        >>> handler.on_item_resolved("payment", "auto_replay")
    """

    @staticmethod
    def on_item_created(
        domain: str,
        failure_type: str,
        duration_seconds: float | None = None,
    ) -> None:
        """
        Called on DLQ item creation.

        Args:
            domain: domain name (payment, point, inventory, etc.)
            failure_type: failure type (PG_TIMEOUT, INSUFFICIENT_STOCK, etc.)
            duration_seconds: store_failure() wall time (for histogram)
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        try:
            domain = resolve_domain_label(domain)

            # Counter: cumulative count (100% accurate)
            metrics.record_dlq_item_created(domain, failure_type)

            # Histogram: store duration (D8/446)
            if duration_seconds is not None and hasattr(metrics, "dlq"):
                metrics.dlq.record_store_duration(domain, duration_seconds)

            # Gauge: safe increment via SafeGauge wrapper
            safe_gauge = _get_safe_pending_gauge()
            if safe_gauge:
                safe_gauge.labels(domain=domain).inc()

            _log_event(
                "get_dlq_log_level",
                f"[EventHandler] DLQ created: domain={domain}, type={failure_type}",
                event_type="dlq.created",
                domain=domain,
                failure_type=failure_type,
                duration_seconds=duration_seconds,
            )

            # Push to daily report collector with domain/failure_type context.
            # Extra keys are ignored by _update_counts_from_entry field_mapping —
            # no counter impact, available in entry detail for per-domain breakdown.
            try:
                get_daily_report_collector().add_result(
                    task_name="dlq_item_created",
                    result={
                        "dlq_new_entries_count": 1,
                        "domain": domain,
                        "failure_type": failure_type,
                    },
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning(
                "event_handler.record_dlq_creation_failed",
                error=e,
            )

    @staticmethod
    def on_item_resolved(
        domain: str,
        resolution_type: str,
        duration_seconds: float | None = None,
    ) -> None:
        """
        Called on DLQ item resolution.

        Uses SafeGauge to prevent negative values:
        - Resolved events arriving before the Gauge is primed after a
          restart won't underflow below 0.
        - Shadow counter tracks the current value and clamps at 0.
        - Lazy Sync (Reconciler) periodically reconciles against the DB.

        Args:
            domain: domain name
            resolution_type: resolution type (auto_replay, manual, expired, ...)
            duration_seconds: wall time from failure to resolution
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        try:
            domain = resolve_domain_label(domain)

            # Gauge: safe decrement via SafeGauge wrapper (prevents negative)
            safe_gauge = _get_safe_pending_gauge()
            if safe_gauge:
                safe_gauge.labels(domain=domain).dec()

            # Histogram: recovery duration (100% accurate)
            if duration_seconds is not None and hasattr(metrics, "retry"):
                metrics.retry.record_recovery_duration(
                    domain, resolution_type, duration_seconds
                )

            # Counter: success outcome increment
            if hasattr(metrics, "retry"):
                metrics.retry.record_retry(domain, True)

            _log_event(
                "get_dlq_log_level",
                f"[EventHandler] DLQ resolved: domain={domain}, "
                f"resolution={resolution_type}, duration={duration_seconds}s",
                event_type="dlq.resolved",
                domain=domain,
                resolution_type=resolution_type,
                duration_seconds=duration_seconds,
            )

            # Push to daily report collector with resolution type and domain context
            try:
                result_data: dict[str, Any] = {
                    "dlq_resolved_count": 1,
                    "domain": domain,
                }
                if resolution_type == "manual":
                    result_data["dlq_manual_resolutions"] = 1
                elif resolution_type == "ttl_expired":
                    result_data["dlq_ttl_expired"] = 1
                elif resolution_type == "max_retries_exhausted":
                    result_data["dlq_max_retries_exhausted"] = 1

                get_daily_report_collector().add_result(
                    task_name="dlq_item_resolved",
                    result=result_data,
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning(
                "event_handler.record_dlq_resolution_failed",
                error=e,
            )

    @staticmethod
    def on_item_failed(
        domain: str,
        failure_type: str,
        attempt_count: int = 1,
    ) -> None:
        """
        Called on DLQ retry failure (pending count is preserved).

        Args:
            domain: domain name
            failure_type: failure type
            attempt_count: number of attempts so far
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        try:
            domain = resolve_domain_label(domain)

            # Counter + Histogram: failure outcome and attempt count.
            # record_attempt observes the attempts histogram AND increments
            # _outcomes_total{outcome=failure} in one call.
            if hasattr(metrics, "retry"):
                metrics.retry.record_attempt(domain, attempt_count, "failure")

            _log_event(
                "get_dlq_log_level",
                f"[EventHandler] DLQ retry failed: domain={domain}, "
                f"type={failure_type}, attempts={attempt_count}",
                event_type="dlq.retry_failed",
                domain=domain,
                failure_type=failure_type,
                attempt_count=attempt_count,
            )
        except Exception as e:
            logger.warning(
                "event_handler.record_dlq_failure_failed",
                error=e,
            )

    @staticmethod
    def on_domain_rejected(
        site: str,
        reason: Any,
        original_domain: object,
    ) -> None:
        """Record a domain input validation rejection at a chokepoint.

        Emits a low-cardinality counter (labelled by ``site`` only) plus a
        WARNING structlog entry carrying ``reason`` and a 32-char sanitized
        preview of the original input.

        Args:
            site: One of ``"domain_context"``, ``"set_domain_context"``,
                ``"store_failure"``. Bounded to keep metric series flat.
            reason: ``DomainRejectReason`` enum value (``.value`` is emitted
                into the log payload).
            original_domain: Raw rejected input (any type — boundary callers
                may pass non-strings).
        """
        from baldur.metrics.registry import sanitize_label_value

        reason_value = getattr(reason, "value", reason)
        preview = sanitize_label_value(str(original_domain), max_length=32)

        metrics = _get_metrics()
        if metrics is not None:
            try:
                if hasattr(metrics, "record_dlq_domain_input_rejected"):
                    metrics.record_dlq_domain_input_rejected(site)
            except Exception as e:
                logger.warning(
                    "event_handler.record_domain_rejected_failed",
                    error=e,
                )

        logger.warning(
            "domain.input_rejected",
            site=site,
            reason=reason_value,
            original_preview=preview,
        )

    @staticmethod
    def on_overflow_rejected(domain: str) -> None:
        """
        Called when DLQ overflow rejects an item.

        Args:
            domain: rejected domain
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        try:
            domain = resolve_domain_label(domain)

            if hasattr(metrics, "record_dlq_rejected"):
                metrics.record_dlq_rejected(domain)
            if hasattr(metrics, "record_dlq_overflow"):
                metrics.record_dlq_overflow(domain, "reject")
        except Exception as e:
            logger.warning(
                "event_handler.record_overflow_rejected_failed",
                error=e,
            )

    @staticmethod
    def on_overflow_evicted(evicted_count: int, level: str) -> None:
        """
        Called after background eviction completes.

        Args:
            evicted_count: total evicted items
            level: eviction level (normal, aggressive, emergency)
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        try:
            if hasattr(metrics, "record_dlq_evicted"):
                metrics.record_dlq_evicted(
                    count=evicted_count,
                    strategy="drop_oldest",
                )
            if level == "emergency" and hasattr(metrics, "record_dlq_emergency_purge"):
                metrics.record_dlq_emergency_purge()
        except Exception as e:
            logger.warning(
                "event_handler.record_overflow_evicted_failed",
                error=e,
            )

    @staticmethod
    def on_sla_breach(domain: str) -> None:
        """
        Called when an SLA breach occurs.

        SLA breach is significant for system governance, so it is
        logged at WARNING level by default.

        Args:
            domain: domain name
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        try:
            domain = resolve_domain_label(domain)

            if hasattr(metrics, "retry"):
                metrics.retry.record_sla_breach(domain)

            _log_event(
                "get_sla_log_level",
                f"[EventHandler] SLA breach: domain={domain}",
                event_type="sla.breach",
                domain=domain,
            )
        except Exception as e:
            logger.warning(
                "event_handler.record_sla_breach_failed",
                error=e,
            )


class CircuitBreakerEventHandler:
    """
    Circuit Breaker event handler.

    Updates metrics on CB state changes. CB state changes indicate
    a governance risk, so they are logged at WARNING level by default.
    """

    @staticmethod
    def on_state_changed(
        service: str,
        from_state: str,
        to_state: str,
    ) -> None:
        """
        Called on Circuit Breaker state change.

        CB state changes indicate system instability and are logged at
        WARNING level. Composite keys (``service::cell_id``) are split
        so metric labels carry the base service and cell id separately.

        Args:
            service: service name (may include composite key)
            from_state: previous state
            to_state: new state
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        # Composite key split — performed only at the metric emit boundary
        from baldur.core.cb_namespace import (
            parse_composite_cb_name,
        )

        base_service, cell_id = parse_composite_cb_name(service)

        try:
            # Gauge + transitions counter: record_state_change sets the state
            # gauge, increments the transitions counter, and computes
            # is_synthetic internally — one call for both operations.
            if hasattr(metrics, "circuit_breaker"):
                metrics.circuit_breaker.record_state_change(
                    base_service, from_state, to_state, cell_id=cell_id
                )

            # Counter: trip count on transition to "open"
            if to_state == "open" and hasattr(metrics, "circuit_breaker"):
                metrics.circuit_breaker.record_trip(base_service)

            _log_event(
                "get_cb_log_level",
                f"[EventHandler] CB state changed: service={base_service}, "
                f"cell_id={cell_id}, {from_state} -> {to_state}",
                event_type="circuit_breaker.state_changed",
                service=base_service,
                cell_id=cell_id,
                from_state=from_state,
                to_state=to_state,
            )

            # Push CB data to daily report collector with service context.
            # base_service enables per-service breakdown in detail API response.
            try:
                cb_result_data: dict[str, Any] = {
                    "circuit_transitions": 1,
                    "service": base_service,
                }
                if to_state == "open":
                    cb_result_data["circuits_opened"] = 1
                elif to_state == "closed":
                    cb_result_data["circuits_closed"] = 1

                get_daily_report_collector().add_result(
                    task_name="circuit_breaker_state_changed",
                    result=cb_result_data,
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning(
                "event_handler.record_cb_state_failed",
                error=e,
            )

    @staticmethod
    def on_failure(service: str) -> None:
        """
        Record a Circuit Breaker failure.

        Args:
            service: service name
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        # Composite key split — keep the failures series' service label
        # consistent with the other CB series (record_state_change / record_trip
        # also emit on base_service). The failures counter is service-only, like
        # trips, so cell_id is carried in the log context, not the metric label.
        from baldur.core.cb_namespace import (
            parse_composite_cb_name,
        )

        base_service, cell_id = parse_composite_cb_name(service)

        try:
            if hasattr(metrics, "circuit_breaker"):
                metrics.circuit_breaker.record_failure(base_service)

            _log_event(
                "get_cb_log_level",
                f"[EventHandler] CB failure recorded: service={base_service}",
                event_type="circuit_breaker.failure",
                service=base_service,
                cell_id=cell_id,
            )
        except Exception as e:
            logger.warning(
                "event_handler.record_cb_failure_failed",
                error=e,
            )


class ReplayEventHandler:
    """
    Replay event handler.

    Updates metrics related to replay operations.
    """

    @staticmethod
    def on_replay_started(domain: str, replay_type: str) -> None:
        """
        Called on replay start.

        Args:
            domain: domain name
            replay_type: replay type (auto, manual, ...)
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        try:
            domain = resolve_domain_label(domain)

            if hasattr(metrics, "replay"):
                metrics.replay.record_started(domain, replay_type)

            _log_event(
                "get_replay_log_level",
                f"[EventHandler] Replay started: domain={domain}, type={replay_type}",
                event_type="replay.started",
                domain=domain,
                replay_type=replay_type,
            )
        except Exception as e:
            logger.warning(
                "event_handler.record_replay_start_failed",
                error=e,
            )

    @staticmethod
    def on_replay_completed(
        domain: str,
        success: bool,
        duration_seconds: float,
    ) -> None:
        """
        Called on replay completion.

        Args:
            domain: domain name
            success: whether the replay succeeded
            duration_seconds: elapsed time
        """
        metrics = _get_metrics()
        if metrics is None:
            return

        try:
            domain = resolve_domain_label(domain)

            outcome = "success" if success else "failure"
            # record_replay bumps _outcomes_total{outcome} and observes
            # _duration_seconds in one call.
            if hasattr(metrics, "replay"):
                metrics.replay.record_replay(domain, outcome, duration_seconds)

            _log_event(
                "get_replay_log_level",
                f"[EventHandler] Replay completed: domain={domain}, "
                f"success={success}, duration={duration_seconds}s",
                event_type="replay.completed",
                domain=domain,
                success=success,
                duration_seconds=duration_seconds,
            )
        except Exception as e:
            logger.warning(
                "event_handler.record_replay_completion_failed",
                error=e,
            )

    @staticmethod
    def on_replay_blocked(domain: str, reason: str) -> None:
        """Record a governance-blocked replay."""
        try:
            metrics = _get_metrics()
            if metrics is None:
                return
            domain = resolve_domain_label(domain)
            if hasattr(metrics, "replay"):
                metrics.replay.record_replay(domain, "blocked")
            logger.warning(
                "metrics.replay_blocked",
                healing_domain=domain,
                block_reason=reason,
            )
        except Exception as e:
            logger.warning("metrics.record_replay_metric_failed", error=e)

    @staticmethod
    def on_batch_completed(
        domain: str,
        total: int,
        success_count: int,
        failed_count: int,
        duration_seconds: float,
    ) -> None:
        """Record batch replay summary metrics."""
        try:
            metrics = _get_metrics()
            if metrics is None:
                return
            domain = resolve_domain_label(domain)
            if hasattr(metrics, "replay"):
                metrics.replay.record_replay(
                    domain, "batch_completed", duration_seconds
                )
            logger.info(
                "metrics.replay_batch_completed",
                healing_domain=domain,
                total=total,
                success_count=success_count,
                failed_count=failed_count,
                duration_seconds=round(duration_seconds, 3),
            )
        except Exception as e:
            logger.warning("metrics.record_replay_metric_failed", error=e)


def reset_event_handler_cache() -> None:
    """
    Reset cached instances (for testing).

    Clears the global caches for metrics, safe gauge, and logging config.
    Also resets the #485 D1d/G7 ``_metrics_init_failed`` sticky flag so
    settings/recorder resets restore the lazy-init contract.
    """
    global _metrics_instance, _metrics_init_failed, _safe_gauge_cache, _logging_config
    _metrics_instance = None
    _metrics_init_failed = False
    _safe_gauge_cache = {}
    _logging_config = None


__all__ = [
    "DLQMetricEventHandler",
    "CircuitBreakerEventHandler",
    "ReplayEventHandler",
    "reset_event_handler_cache",
]
