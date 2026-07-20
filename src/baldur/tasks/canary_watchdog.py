"""
Canary Rollout Watchdog Tasks.

Renews config-type locks for live rollouts, detects and cleans up zombie
(stalled) rollouts, and handles automatic promotion / rollback.

Tasks:
    - scan_zombie_rollouts: renew live rollouts' config locks, detect stalled
      rollouts, notify, auto-rollback
    - auto_promote_eligible: promote rollouts that meet auto-promote conditions
    - collect_canary_metrics: collect metrics and health for active rollouts

Thin Task, Fat Service principle:
- The functions in this file are simple delegators.
- All business logic lives in the RolloutWatchdog class.

Celery Beat:
    The composed schedule injects entries for these tasks, but the functions
    below carry no task registration, so a worker cannot resolve the names and
    the lane does not currently run. Registering them would start automatic
    canary promotion and rollback on installs that have never had it, so the
    activation is a deliberate decision rather than a wiring detail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.interfaces.canary import CanaryRollout
    from baldur.settings.canary_watchdog import CanaryWatchdogSettings

logger = structlog.get_logger()


# =============================================================================
# Watchdog Configuration
# =============================================================================


@dataclass
class CanaryWatchdogConfig:
    """
    Watchdog configuration.

    Attributes:
        zombie_threshold_minutes: Time after which a rollout counts as stalled (default 30)
        auto_rollback_after_minutes: Wait time before automatic rollback (default 60)
        max_stage_duration_minutes: Maximum dwell time per stage (default 15)
        enable_auto_promote: Enable automatic promotion
        enable_auto_rollback: Enable automatic rollback for zombies
        notification_enabled: Enable Slack notifications
        slack_channel: Notification Slack channel
    """

    zombie_threshold_minutes: int = 30
    auto_rollback_after_minutes: int = 60
    max_stage_duration_minutes: int = 15
    enable_auto_promote: bool = True
    enable_auto_rollback: bool = True
    notification_enabled: bool = True
    slack_channel: str = "#baldur-alerts"

    @classmethod
    def from_settings(
        cls,
        settings: CanaryWatchdogSettings | None = None,
        **overrides,
    ) -> CanaryWatchdogConfig:
        """
        Build a CanaryWatchdogConfig instance from Settings.

        Args:
            settings: CanaryWatchdogSettings instance (singleton when None)
            **overrides: Per-field overrides

        Returns:
            CanaryWatchdogConfig: Settings-backed instance
        """
        from baldur.settings.canary_watchdog import get_canary_watchdog_settings

        s = settings or get_canary_watchdog_settings()
        return cls(
            zombie_threshold_minutes=overrides.get(
                "zombie_threshold_minutes", s.zombie_threshold_minutes
            ),
            auto_rollback_after_minutes=overrides.get(
                "auto_rollback_after_minutes", s.auto_rollback_after_minutes
            ),
            max_stage_duration_minutes=overrides.get(
                "max_stage_duration_minutes", s.max_stage_duration_minutes
            ),
            enable_auto_promote=overrides.get(
                "enable_auto_promote", s.enable_auto_promote
            ),
            enable_auto_rollback=overrides.get(
                "enable_auto_rollback", s.enable_auto_rollback
            ),
            notification_enabled=overrides.get(
                "notification_enabled", s.notification_enabled
            ),
            slack_channel=overrides.get("slack_channel", s.slack_channel),
        )


@dataclass
class ZombieRollout:
    """
    Zombie rollout information.

    A rollout whose stall time exceeded the threshold.
    """

    rollout_id: str
    config_type: str
    state: str
    stuck_since: datetime
    stuck_minutes: float
    created_by: str
    affected_clusters: list[str]
    reason: str = ""
    action_taken: str = ""  # "notified", "auto_rolled_back", "none"


@dataclass
class WatchdogResult:
    """
    Watchdog execution result.

    Attributes:
        success: Whether the run succeeded
        scanned_count: Number of rollouts scanned
        zombie_count: Number of zombies detected
        rollback_count: Number of automatic rollbacks
        promote_count: Number of automatic promotions
        renewed_count: Number of config locks renewed (incl. re-acquired)
        renewal_failed_count: Number of lock renewals that failed or conflicted
        governance_blocked: Blocked by governance
        governance_block_reason: Block reason
        zombies: List of zombie rollouts
        errors: List of errors
    """

    success: bool = True
    scanned_count: int = 0
    zombie_count: int = 0
    rollback_count: int = 0
    promote_count: int = 0
    renewed_count: int = 0
    renewal_failed_count: int = 0
    governance_blocked: bool = False
    governance_block_reason: str = ""
    zombies: list[ZombieRollout] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "success": self.success,
            "scanned_count": self.scanned_count,
            "zombie_count": self.zombie_count,
            "rollback_count": self.rollback_count,
            "promote_count": self.promote_count,
            "renewed_count": self.renewed_count,
            "renewal_failed_count": self.renewal_failed_count,
            "governance_blocked": self.governance_blocked,
            "governance_block_reason": self.governance_block_reason,
            "zombies": [
                {
                    "rollout_id": z.rollout_id,
                    "config_type": z.config_type,
                    "state": z.state,
                    "stuck_minutes": z.stuck_minutes,
                    "action_taken": z.action_taken,
                }
                for z in self.zombies
            ],
            "errors": self.errors,
        }


# =============================================================================
# RolloutWatchdog Service
# =============================================================================


class RolloutWatchdog:
    """
    Canary Rollout Watchdog.

    Detects stalled (zombie) rollouts and performs automatic actions.

    Zombie criteria:
    - CANARY state for more than 2x the stage's duration_minutes
    - PAUSED state for more than zombie_threshold_minutes
    - PROMOTING state for more than 5 minutes (failed transition)

    Automatic actions:
    1. zombie_threshold reached: Slack notification
    2. auto_rollback_after reached: automatic rollback + notification

    Example:
        watchdog = RolloutWatchdog()
        result = watchdog.scan_and_handle()

        if result.zombie_count > 0:
            print(f"Found {result.zombie_count} zombie rollouts")
    """

    def __init__(self, config: CanaryWatchdogConfig | None = None):
        """
        Initialize RolloutWatchdog.

        Args:
            config: Watchdog configuration (None for defaults)
        """
        self.config = config or CanaryWatchdogConfig()
        self._service = None

    @property
    def service(self):
        """CanaryRolloutService (lazy loading)."""
        if self._service is None:
            from baldur.factory.registry import ProviderRegistry

            self._service = ProviderRegistry.canary_rollout_service.safe_get()
            if self._service is None:
                raise RuntimeError(
                    "Canary watchdog requires baldur_pro CanaryRolloutService"
                )
        return self._service

    def scan_and_handle(self) -> WatchdogResult:
        """
        Renew config locks and handle zombie rollouts.

        Each active rollout is visited once: first its config-type lock is
        renewed (keeping the single-active-rollout invariant alive past the
        lock TTL — see ``_renew_lock``), then it is checked for the zombie /
        stall condition. Renewal runs before the zombie check so a zombie's
        lock cannot lapse mid-handling; the auto-rollback path then releases
        it explicitly. Renewal is NOT governance-gated (keeping an existing
        lock alive is invariant maintenance, not a new mutation).

        Returns:
            WatchdogResult: Scan and handling result
        """
        result = WatchdogResult()

        try:
            active_rollouts = self.service.get_active_rollouts()
            result.scanned_count = len(active_rollouts)

            now = utc_now()

            # Renew every active rollout's config lock first (D1) so a zombie's
            # lock cannot lapse before _handle_zombie's explicit release on the
            # auto-rollback path. Renewing the whole set ahead of zombie
            # handling is a strictly stronger ordering than the prior
            # per-rollout interleave for the same invariant.
            for rollout in active_rollouts:
                self._renew_lock(rollout, result)

            # Stall detection delegates to the shared side-effect-free path
            # (D5) — the same source of truth the meta-watchdog canary_rollout
            # probe reads, so the two definitions cannot drift. The fetched
            # rollout list is passed through to avoid a second store round-trip.
            rollout_by_id = {rollout.id: rollout for rollout in active_rollouts}
            for zombie in self.detect_stalled_rollouts(active_rollouts, now):
                result.zombies.append(zombie)
                result.zombie_count += 1

                rollout = rollout_by_id.get(zombie.rollout_id)
                if rollout is None:
                    continue

                # Perform automatic action
                action = self._handle_zombie(zombie, rollout)
                zombie.action_taken = action

                if action == "auto_rolled_back":
                    result.rollback_count += 1

            logger.info(
                "watchdog.scan_complete",
                scanned_count=result.scanned_count,
                zombie_count=result.zombie_count,
                rollback_count=result.rollback_count,
                renewed_count=result.renewed_count,
                renewal_failed_count=result.renewal_failed_count,
            )

        except Exception as e:
            logger.exception("watchdog.scan_and_handle_failed")
            result.success = False
            result.errors.append(str(e))

        return result

    def detect_stalled_rollouts(
        self,
        active_rollouts: list[CanaryRollout] | None = None,
        now: datetime | None = None,
    ) -> list[ZombieRollout]:
        """Side-effect-free detection of stalled (zombie) rollouts (D5).

        The single source of truth for the "stuck canary" definition: applies
        the zombie/stall check (:meth:`_check_zombie`) to each active rollout
        and returns the stalled set with NO lock-renewal, notification,
        auto-rollback, or metric side effect (read-only). Shared by the Celery
        ``scan_and_handle()`` maintenance path (which passes its already-fetched
        rollout list) and the meta-watchdog ``canary_rollout`` semantic-stuck
        probe (which calls with no arguments) so the two cannot drift.

        Args:
            active_rollouts: Pre-fetched active rollouts; fetched via the
                service when ``None`` (the probe's entry path).
            now: Reference time for the stall clock; ``utc_now()`` when ``None``.

        Returns:
            list[ZombieRollout]: stalled rollouts (empty when none).
        """
        if active_rollouts is None:
            active_rollouts = self.service.get_active_rollouts()
        if now is None:
            now = utc_now()

        stalled: list[ZombieRollout] = []
        for rollout in active_rollouts:
            zombie = self._check_zombie(rollout, now)
            if zombie is not None:
                stalled.append(zombie)
        return stalled

    def _renew_lock(self, rollout: CanaryRollout, result: WatchdogResult) -> None:
        """Renew one active rollout's config-type lock (D1/D2/D4).

        Eligibility: renew every active rollout EXCEPT CREATED (a never-started
        rollout has no liveness signal — renewing it would convert a bounded
        TTL freeze into an unbounded silent one) and terminal (defensive).
        Renewal failures are isolated per rollout — each renewal is guarded by
        its own try/except — so one bad rollout cannot abort the zombie scan.

        On a CONFLICT outcome (a foreign rollout already holds the lock) a
        Slack alert is raised when notifications are enabled; the alert is
        debounced by a config-type-scoped dedup_key through the notification
        manager's distributed cooldown, so a persistent conflict does not
        re-alert on every scan.
        """
        from baldur.models.canary import CanaryState, LockRenewalOutcome

        # D2 eligibility: skip never-started and terminal rollouts.
        if rollout.state == CanaryState.CREATED or rollout.is_terminal:
            return

        try:
            outcome = self.service.renew_config_lock(rollout)
        except Exception as e:
            logger.warning(
                "watchdog.lock_renewal_failed",
                rollout_id=rollout.id,
                error=e,
            )
            result.renewal_failed_count += 1
            return

        if outcome in (LockRenewalOutcome.RENEWED, LockRenewalOutcome.REACQUIRED):
            result.renewed_count += 1
        elif outcome == LockRenewalOutcome.CONFLICT:
            result.renewal_failed_count += 1
            if self.config.notification_enabled:
                self._notify_lock_conflict(rollout)
        elif outcome == LockRenewalOutcome.FAILED:
            result.renewal_failed_count += 1
        # SKIPPED (no store wired): nothing to count.

    def _notify_lock_conflict(self, rollout: CanaryRollout) -> None:
        """Raise a Slack alert for a live config-lock conflict (D4).

        Built directly here rather than via ``_send_notification`` (which is
        ZombieRollout-shaped and closed to its two event types). HIGH priority,
        CHAOS category for watchdog parity, with an explicit config-type-scoped
        ``dedup_key`` so distinct config-type conflicts are not collapsed and
        zombie alerts (which default to ``canary_watchdog:chaos``) do not
        debounce against conflict alerts. The conflicting owner stays in the
        service-layer ``canary_rollout.config_lock_conflict`` log.
        """
        try:
            from baldur.models.notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
            )
            from baldur_pro.services.unified_notification import (
                get_unified_notification_manager,
            )

            service = get_unified_notification_manager()
            service.notify(
                NotificationPayload(
                    title="Canary Watchdog: Config Lock Conflict",
                    message=(
                        f"🔒 [Canary Watchdog] Config Lock Conflict Detected\n"
                        f"• Victim Rollout ID: `{rollout.id}`\n"
                        f"• Config: {rollout.config_type}\n"
                        f"• A different rollout now holds this config type's lock\n"
                        f"• Action: see logs (canary_rollout.config_lock_conflict) "
                        f"for the conflicting owner"
                    ),
                    priority=NotificationPriority.HIGH,
                    category=NotificationCategory.CHAOS,
                    source="canary_watchdog",
                    channels=[self.config.slack_channel],
                    dedup_key=f"canary_lock_conflict:{rollout.config_type}",
                )
            )
        except Exception as e:
            logger.warning(
                "watchdog.lock_conflict_notification_failed",
                error=e,
            )

    @staticmethod
    def _stall_anchor(rollout: CanaryRollout) -> datetime:
        """Anchor for the rollout's current-state stall clock.

        stage_started_at / paused_at are the precise per-state anchors;
        created_at is the fallback for rollouts persisted before those
        fields existed.
        """
        from baldur.models.canary import CanaryState

        if rollout.state == CanaryState.PAUSED:
            return getattr(rollout, "paused_at", None) or rollout.created_at
        return getattr(rollout, "stage_started_at", None) or rollout.created_at

    def _check_zombie(
        self,
        rollout: CanaryRollout,
        now: datetime,
    ) -> ZombieRollout | None:
        """
        Check whether a rollout is a zombie.

        Args:
            rollout: Rollout to check
            now: Current time

        Returns:
            ZombieRollout or None
        """
        from baldur.models.canary import CanaryState

        try:
            from baldur_pro.services.canary.models import ZOMBIE_EXEMPT_TRIGGERS
        except ImportError:
            ZOMBIE_EXEMPT_TRIGGERS = None  # type: ignore[assignment,misc]

        stage = rollout.current_stage
        stage_duration = stage.duration_minutes if stage else 5

        # Time the rollout has been sitting in its current state.
        stuck_since = self._stall_anchor(rollout)
        stuck_minutes = (now - stuck_since).total_seconds() / 60

        # Zombie determination
        is_zombie = False
        reason = ""

        if rollout.state == CanaryState.CANARY:
            # CANARY state: more than 2x the stage duration
            threshold = stage_duration * 2
            if stuck_minutes > threshold:
                is_zombie = True
                reason = f"Stuck in CANARY for {stuck_minutes:.1f} min (threshold: {threshold})"

        elif rollout.state == CanaryState.PAUSED:
            # PAUSED caused by Error Budget / Governance is a legitimate wait
            # state, not a zombie.
            triggered_by = getattr(rollout, "pause_triggered_by", None)

            if triggered_by in ZOMBIE_EXEMPT_TRIGGERS:
                # Legitimate wait state — not a zombie
                logger.debug(
                    "watchdog.rollout_excluded_zombie_check",
                    rollout_id=rollout.id,
                    triggered_by=triggered_by,
                )
                return None

            # Other PAUSED rollouts follow the threshold rule
            if stuck_minutes > self.config.zombie_threshold_minutes:
                is_zombie = True
                reason = f"Paused for {stuck_minutes:.1f} min (threshold: {self.config.zombie_threshold_minutes})"

        elif rollout.state == CanaryState.PROMOTING:
            # PROMOTING state: more than 5 minutes (failed transition)
            if stuck_minutes > 5:
                is_zombie = True
                reason = "Stuck in PROMOTING state"

        if not is_zombie:
            return None

        return ZombieRollout(
            rollout_id=rollout.id,
            config_type=rollout.config_type,
            state=rollout.state.value,
            stuck_since=stuck_since,
            stuck_minutes=stuck_minutes,
            created_by=rollout.created_by,
            affected_clusters=rollout.affected_clusters,
            reason=reason,
        )

    def _handle_zombie(
        self,
        zombie: ZombieRollout,
        rollout: CanaryRollout,
    ) -> str:
        """
        Handle a zombie rollout.

        Args:
            zombie: Zombie information
            rollout: Rollout object

        Returns:
            Action taken ("notified", "auto_rolled_back", "none")
        """
        # Auto-rollback condition: stuck beyond auto_rollback_after_minutes
        if (
            self.config.enable_auto_rollback
            and zombie.stuck_minutes > self.config.auto_rollback_after_minutes
        ):
            try:
                success = self.service.rollback(
                    rollout.id,
                    reason=f"[Watchdog] Auto rollback - {zombie.reason}",
                    bypass_governance=True,
                    bypass_reason=f"[AUTO-WATCHDOG] Zombie rollout cleanup after {zombie.stuck_minutes}m: {zombie.reason}",
                    requested_by="system:watchdog",
                )
                if success:
                    self._send_notification(zombie, "auto_rolled_back")
                    logger.warning(
                        "watchdog.auto_rolled_back",
                        rollout_id=rollout.id,
                        zombie=zombie.reason,
                    )
                    return "auto_rolled_back"
            except Exception as e:
                logger.exception(
                    "watchdog.auto_rollback_failed",
                    error=e,
                )
                return "rollback_failed"

        # Notification only
        if self.config.notification_enabled:
            self._send_notification(zombie, "zombie_detected")
            return "notified"

        return "none"

    def _send_notification(self, zombie: ZombieRollout, event_type: str) -> None:
        """
        Send a Slack notification.

        Args:
            zombie: Zombie information
            event_type: Event type ("zombie_detected", "auto_rolled_back")
        """
        try:
            from baldur.models.notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
            )
            from baldur_pro.services.unified_notification import (
                get_unified_notification_manager,
            )

            service = get_unified_notification_manager()

            if event_type == "zombie_detected":
                title = "Canary Watchdog: Zombie Rollout Detected"
                message = (
                    f"⚠️ [Canary Watchdog] Zombie Rollout Detected\n"
                    f"• Rollout ID: `{zombie.rollout_id}`\n"
                    f"• Config: {zombie.config_type}\n"
                    f"• State: {zombie.state}\n"
                    f"• Stuck: {zombie.stuck_minutes:.0f} min\n"
                    f"• Reason: {zombie.reason}\n"
                    f"• Created by: {zombie.created_by}\n"
                    f"• Action: Manual intervention required"
                )
            elif event_type == "auto_rolled_back":
                title = "Canary Watchdog: Auto Rollback Executed"
                message = (
                    f"🔄 [Canary Watchdog] Auto Rollback Executed\n"
                    f"• Rollout ID: `{zombie.rollout_id}`\n"
                    f"• Config: {zombie.config_type}\n"
                    f"• Stuck: {zombie.stuck_minutes:.0f} min\n"
                    f"• Reason: {zombie.reason}\n"
                    f"• Affected Clusters: {', '.join(zombie.affected_clusters)}\n"
                    f"• Action: Previous config restored"
                )
            else:
                return

            service.notify(
                NotificationPayload(
                    title=title,
                    message=message,
                    priority=NotificationPriority.HIGH,
                    category=NotificationCategory.CHAOS,
                    source="canary_watchdog",
                    channels=[self.config.slack_channel],
                )
            )

        except Exception as e:
            logger.warning(
                "watchdog.notification_failed",
                error=e,
            )

    def auto_promote_eligible(self) -> WatchdogResult:  # noqa: C901
        """
        Promote rollouts that meet the auto-promotion conditions.

        Conditions:
        - Stage has auto_promote=True
        - duration_minutes elapsed (measured from stage entry)
        - Metric validation passes
        - Global error budget check

        Returns:
            WatchdogResult: Promotion result
        """
        result = WatchdogResult()

        if not self.config.enable_auto_promote:
            return result

        # Global error budget check (fail-closed policy)
        try:
            from baldur.factory.registry import ProviderRegistry
            from baldur.settings.canary_governance import (
                get_canary_governance_settings,
            )

            gov_settings = get_canary_governance_settings()
            governance = ProviderRegistry.governance.get().check_all_governance(
                check_kill_switch=True,
                check_emergency=True,
                emergency_min_level=gov_settings.promote_emergency_min_level,
                check_error_budget=True,
                operation_name="auto_promote_canary",
                service_name="RolloutWatchdog",
                domain="canary",
                audit_on_block=True,
            )

            if not governance.allowed:
                logger.warning(
                    "watchdog.auto_promotion_blocked_governance",
                    governance=governance.block_message,
                )

                # Record Prometheus metrics
                self._record_governance_blocked_metrics(governance)

                result.governance_blocked = True
                result.governance_block_reason = governance.block_message
                return result

        except ImportError:
            logger.warning("watchdog.governance_checks_unavailable")
            # Fail-closed: block on import failure (conservative policy)
            result.governance_blocked = True
            result.governance_block_reason = "GovernanceChecks module not available"
            return result
        except Exception as e:
            logger.warning(
                "watchdog.governance_check_failed",
                error=e,
            )
            # Fail-closed: block on error (mutating operations stay conservative)
            result.governance_blocked = True
            result.governance_block_reason = f"Governance check error: {e}"
            return result

        try:
            from baldur.models.canary import CanaryState

            active_rollouts = self.service.get_active_rollouts()
            result.scanned_count = len(active_rollouts)
            now = utc_now()

            for rollout in active_rollouts:
                if rollout.state != CanaryState.CANARY:
                    continue

                stage = rollout.current_stage
                if not stage or not stage.auto_promote:
                    continue

                # Has the stage's observation window elapsed? Measured from
                # stage entry (stage_started_at); created_at is the fallback
                # for rollouts persisted before that field existed.
                stage_anchor = (
                    getattr(rollout, "stage_started_at", None) or rollout.created_at
                )
                elapsed = (now - stage_anchor).total_seconds() / 60
                if elapsed < stage.duration_minutes:
                    continue

                # Promote after metric validation
                try:
                    success = self.service.promote(rollout.id, force=False)
                    if success:
                        result.promote_count += 1
                        logger.info(
                            "watchdog.auto_promoted_stage",
                            rollout_id=rollout.id,
                            stage=stage.name,
                        )
                except Exception as e:
                    result.errors.append(f"{rollout.id}: {e}")

        except Exception as e:
            logger.exception("watchdog.auto_promote_failed")
            result.success = False
            result.errors.append(str(e))

        return result

    def _record_governance_blocked_metrics(self, governance) -> None:
        """Record Prometheus metrics when governance blocks promotion."""
        try:
            from baldur.services.metrics.definitions import (
                canary_governance_blocked_total,
                canary_pending_promotion_gauge,
            )

            block_reason = (
                governance.block_reason.value if governance.block_reason else "unknown"
            )
            canary_governance_blocked_total.labels(block_reason=block_reason).inc()

            pending_count = len(self.service.get_active_rollouts())
            canary_pending_promotion_gauge.labels(reason=block_reason).set(
                pending_count
            )
        except Exception as e:
            logger.debug(
                "watchdog.metrics_recording_failed",
                error=e,
            )


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_rollout_watchdog, configure_rollout_watchdog, reset_rollout_watchdog = (
    make_singleton_factory("rollout_watchdog", RolloutWatchdog)
)

# Backward-compatible alias (old name had no "rollout_" prefix)
reset_watchdog = reset_rollout_watchdog


# =============================================================================
# Thin Task Wrappers (Celery Tasks)
# =============================================================================


def scan_zombie_rollouts() -> dict[str, Any]:
    """
    Renew config locks and handle zombie rollouts.

    For each active rollout: renews its config-type lock (keeping the
    single-active-rollout invariant alive past the lock TTL), then detects
    stalled canary rollouts and:
    1. Sends a Slack notification
    2. Rolls back automatically past the threshold

    Recommended Celery Beat schedule: every 5 minutes

    Returns:
        dict: {
            "success": bool,
            "scanned_count": int,
            "zombie_count": int,
            "rollback_count": int,
            "renewed_count": int,
            "renewal_failed_count": int,
            "zombies": [...],
        }
    """
    try:
        watchdog = get_rollout_watchdog()
        result = watchdog.scan_and_handle()
        return result.to_dict()

    except Exception as e:
        logger.exception(
            "canary_watchdog.failed",
            error=e,
        )
        raise


def auto_promote_eligible() -> dict[str, Any]:
    """
    Promote rollouts that meet the auto-promotion conditions.

    - Stage has auto_promote=True
    - duration_minutes elapsed
    - Metric validation passes

    Recommended Celery Beat schedule: every minute

    Returns:
        dict: {
            "success": bool,
            "scanned_count": int,
            "promote_count": int,
        }
    """
    try:
        watchdog = get_rollout_watchdog()
        result = watchdog.auto_promote_eligible()
        return result.to_dict()

    except Exception as e:
        logger.exception(
            "canary_watchdog.failed",
            error=e,
        )
        raise


def collect_canary_metrics() -> dict[str, Any]:
    """
    Collect metrics for active rollouts.

    TODO: implement full integration when the Prometheus/metrics system lands

    Returns:
        dict: {
            "success": bool,
            "rollout_count": int,
            "metrics_collected": int,
        }
    """
    try:
        from baldur.factory.registry import ProviderRegistry

        service = ProviderRegistry.canary_rollout_service.safe_get()
        if service is None:
            raise RuntimeError("baldur_pro CanaryRolloutService not registered")
        active_rollouts = service.get_active_rollouts()

        metrics_count = 0
        for rollout in active_rollouts:
            metrics = service.collect_metrics(rollout.id)
            metrics_count += len(metrics)

        return {
            "success": True,
            "rollout_count": len(active_rollouts),
            "metrics_collected": metrics_count,
        }

    except Exception as e:
        logger.exception(
            "canary_watchdog.failed",
            error=e,
        )
        raise


# =============================================================================
# Celery Beat Schedule
# =============================================================================


def get_canary_watchdog_beat_schedule() -> dict[str, Any]:
    """
    Canary Watchdog Celery Beat schedule configuration.

    The entries this returns name tasks that are not registered, so they do not
    resolve on a worker yet — see the module docstring.

    Returns:
        Dict[str, Any]: Celery Beat schedule configuration

    Usage:
        # Normally unnecessary: configure_baldur_celery(app) merges this lane.
        from baldur.tasks.canary_watchdog import get_canary_watchdog_beat_schedule

        CELERY_BEAT_SCHEDULE.update(get_canary_watchdog_beat_schedule())
    """
    from celery.schedules import crontab

    return {
        # Zombie rollout scan (every 5 minutes)
        "canary-scan-zombie-rollouts": {
            "task": "baldur.tasks.canary_watchdog.scan_zombie_rollouts",
            "schedule": crontab(minute="*/5"),
            "options": {
                "queue": "maintenance",
                "expires": 240,  # expire if not handled within 4 minutes
            },
        },
        # Auto-promotion check (every minute)
        "canary-auto-promote-eligible": {
            "task": "baldur.tasks.canary_watchdog.auto_promote_eligible",
            "schedule": crontab(minute="*/1"),
            "options": {
                "queue": "realtime",
                "expires": 50,  # expire if not handled within 50 seconds
            },
        },
        # Metrics collection (every 2 minutes)
        "canary-collect-metrics": {
            "task": "baldur.tasks.canary_watchdog.collect_canary_metrics",
            "schedule": crontab(minute="*/2"),
            "options": {
                "queue": "metrics",
                "expires": 90,
            },
        },
    }
