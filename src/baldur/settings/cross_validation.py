"""
Cross-Settings Validation Infrastructure.

Detects dangerous setting combinations at configuration load time.
Severity-based response: HIGH/MEDIUM/LOW log warnings, allowing startup.

Design doc: 420

Conflict 1 (Emergency LEVEL_3 + Chaos enabled) is handled at runtime
in the Chaos service — see #421.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from baldur.settings.root import BaldurSettings

__all__ = ["check_all"]

logger = structlog.get_logger()


def check_all(settings: BaldurSettings) -> None:
    """
    Run all cross-settings conflict checks.

    Called from BaldurSettings._run_cross_validation model_validator.
    Each check logs a warning with actionable remedy if conflict detected.

    Args:
        settings: Fully constructed BaldurSettings instance.
    """
    _check_error_budget_governance(settings)  # HIGH
    _check_backoff_cb_timeout(settings)  # MEDIUM
    _check_retry_cb_timeout(settings)  # MEDIUM
    _check_throttle_admission_starvation(settings)  # MEDIUM
    _check_sla_slo_hierarchy(settings)  # MEDIUM
    _check_dlq_replay_ratio(settings)  # LOW


# =============================================================================
# Conflict 2: Error Budget = 0% + Governance gate loose
# =============================================================================


def _check_error_budget_governance(settings: BaldurSettings) -> None:
    """
    Check: Error budget exhausted but governance gate still allows risky actions.

    Trigger: critical_threshold_percent == 0.0 AND default_mode == "NORMAL"
    Severity: HIGH
    """
    threshold = settings.services_group.error_budget_gate.critical_threshold_percent
    mode = settings.services_group.governance.default_mode

    if threshold == 0.0 and mode == "NORMAL":
        logger.warning(
            "settings.conflict_detected",
            conflict="error_budget_governance",
            severity="HIGH",
            critical_threshold_percent=threshold,
            governance_mode=mode,
            remedy=(
                "Set BALDUR_ERROR_BUDGET_GATE_CRITICAL_THRESHOLD_PERCENT > 0 "
                "or BALDUR_GOVERNANCE_DEFAULT_MODE=STRICT"
            ),
            runbook_url="/docs/runbooks/settings/error-budget-governance.md",
        )


# =============================================================================
# Conflict 3a: Backoff max_delay > CB recovery_timeout × 5
# =============================================================================


def _check_backoff_cb_timeout(settings: BaldurSettings) -> None:
    """
    Check: Standalone backoff max_delay outlasts CB recovery window.

    Trigger: backoff.exponential_max_delay > circuit_breaker.recovery_timeout * 5
    Severity: MEDIUM
    """
    backoff_max = settings.core.backoff.exponential_max_delay
    cb_recovery = settings.core.circuit_breaker.recovery_timeout
    threshold = cb_recovery * 5

    if backoff_max > threshold:
        logger.warning(
            "settings.conflict_detected",
            conflict="backoff_cb_timeout",
            severity="MEDIUM",
            backoff_max_delay_s=backoff_max,
            cb_recovery_timeout_s=cb_recovery,
            threshold_s=threshold,
            remedy=f"Set BALDUR_BACKOFF_EXPONENTIAL_MAX_DELAY <= {threshold}",
            runbook_url="/docs/runbooks/settings/backoff-cb-timeout.md",
        )


# =============================================================================
# Conflict 3b: Retry max_delay > CB recovery_timeout × 5
# =============================================================================


def _check_retry_cb_timeout(settings: BaldurSettings) -> None:
    """
    Check: Retry handler backoff max_delay outlasts CB recovery window.

    Trigger: retry.max_delay > circuit_breaker.recovery_timeout * 5
    Severity: MEDIUM
    """
    retry_max = settings.core.retry.max_delay
    cb_recovery = settings.core.circuit_breaker.recovery_timeout
    threshold = cb_recovery * 5

    if retry_max > threshold:
        logger.warning(
            "settings.conflict_detected",
            conflict="retry_cb_timeout",
            severity="MEDIUM",
            retry_max_delay_s=retry_max,
            cb_recovery_timeout_s=cb_recovery,
            threshold_s=threshold,
            remedy=f"Set BALDUR_RETRY_MAX_DELAY <= {threshold}",
            runbook_url="/docs/runbooks/settings/retry-cb-timeout.md",
        )


# =============================================================================
# Conflict 4: Throttle + Admission Control both tight
# =============================================================================


def _check_throttle_admission_starvation(settings: BaldurSettings) -> None:
    """
    Check: Both throttle and admission control at tight thresholds.

    Trigger: throttle.min_limit <= 10 AND admission_control.tier_non_essential_max_concurrent <= 10
    Severity: MEDIUM

    Note: These are different dimensions (rate vs concurrency). Direct comparison
    is dimensionally invalid. "Both tight" detects genuine double-constraint starvation.
    """
    throttle_min = settings.scaling.throttle.min_limit
    admission_max = settings.core.admission_control.tier_non_essential_max_concurrent

    if throttle_min <= 10 and admission_max <= 10:
        logger.warning(
            "settings.conflict_detected",
            conflict="throttle_admission_starvation",
            severity="MEDIUM",
            throttle_min_limit=throttle_min,
            admission_non_essential_max_concurrent=admission_max,
            remedy=(
                "Increase BALDUR_THROTTLE_MIN_LIMIT or "
                "BALDUR_ADMISSION_CONTROL_TIER_NON_ESSENTIAL_MAX_CONCURRENT "
                "to avoid request starvation under double constraint"
            ),
            runbook_url="/docs/runbooks/settings/throttle-admission.md",
        )


# =============================================================================
# Conflict 5: SLA hours vs SLO window vs Error Budget window
# =============================================================================


def _check_sla_slo_hierarchy(settings: BaldurSettings) -> None:
    """
    Check: SLA default hours exceeds SLO window.

    Trigger: sla.default_hours > slo.default_window_days * 24
    Severity: MEDIUM

    SLA response time should be shorter than SLO measurement window.
    """
    sla_hours = settings.slo_group.sla.default_hours
    slo_days = settings.slo_group.slo.default_window_days
    slo_hours = slo_days * 24

    if sla_hours > slo_hours:
        logger.warning(
            "settings.conflict_detected",
            conflict="sla_slo_hierarchy",
            severity="MEDIUM",
            sla_default_hours=sla_hours,
            slo_window_days=slo_days,
            slo_window_hours=slo_hours,
            remedy=(
                f"Set BALDUR_SLA_DEFAULT_HOURS <= {slo_hours} "
                f"(= BALDUR_SLO_DEFAULT_WINDOW_DAYS × 24)"
            ),
            runbook_url="/docs/runbooks/settings/sla-slo-hierarchy.md",
        )


# =============================================================================
# Conflict 6: DLQ overflow_evict_batch_size vs replay max_items
# =============================================================================


def _check_dlq_replay_ratio(settings: BaldurSettings) -> None:
    """
    Check: DLQ eviction batch size greatly exceeds replay batch size.

    Trigger: dlq.overflow_evict_batch_size / replay.on_recovery_max_items > 10
    Severity: LOW

    Large ratio indicates inefficient evict/replay balance (e.g., 1000 vs 50 = 20x).
    """
    evict_batch = settings.services_group.dlq.overflow_evict_batch_size
    replay_items = settings.services_group.replay_automation.on_recovery_max_items

    # Avoid division by zero (on_recovery_max_items has ge=1 constraint)
    if replay_items > 0:
        ratio = evict_batch / replay_items
        if ratio > 10:
            logger.warning(
                "settings.conflict_detected",
                conflict="dlq_replay_ratio",
                severity="LOW",
                dlq_evict_batch_size=evict_batch,
                replay_on_recovery_max_items=replay_items,
                ratio=ratio,
                remedy=(
                    "Reduce BALDUR_DLQ_OVERFLOW_EVICT_BATCH_SIZE or "
                    "increase BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_MAX_ITEMS (ratio > 10 = inefficient)"
                ),
                runbook_url="/docs/runbooks/settings/dlq-replay-ratio.md",
            )
