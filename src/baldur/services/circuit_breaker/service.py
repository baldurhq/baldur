"""
Circuit Breaker Service

Provides toggle-based circuit breaker management for external service protection.
Supports manual force open/close controls and conditional replay on recovery.

Features:
- Toggle-based circuit breaker (not automatic failure counting)
- Manual force open/close by operators
- Conditional replay trigger when circuit breaker closes
- Admin integration for operational control
- Rate limit cascade detection (auto-open CB on 429 storm)
- Self-DDoS protection (prevent retry amplification)
- Minimum calls check (prevents false positives with low traffic)
- Fallback strategies (cache, DLQ, default response)
- Error Budget burn rate integration
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import log_cb_state_change_audit
from baldur.core.timezone import now
from baldur.dlq.helpers import store_to_dlq
from baldur.metrics.recorders.circuit_breaker import record_blocked
from baldur.services.event_bus.emitter import EventEmitterMixin

from .config import (
    CircuitBreakerConfig,
    CircuitBreakerDecision,
    CircuitBreakerFallbackResult,
    CircuitBreakerResult,
    CircuitState,
)
from .manual_control import ManualControlMixin
from .protection import ProtectionMixin

if TYPE_CHECKING:
    from baldur.interfaces.repositories import (
        CircuitBreakerStateData,
        CircuitBreakerStateRepository,
    )

logger = structlog.get_logger()


class CircuitBreakerService(EventEmitterMixin, ProtectionMixin, ManualControlMixin):
    """
    Circuit Breaker Service.

    Provides management operations for circuit breaker states.
    Designed for manual (toggle-based) control by operators.

    Usage:
        service = CircuitBreakerService()

        # Force open (block requests)
        result = service.force_open(
            service_name="external_api",
            reason="External service maintenance",
            controlled_by=admin_user
        )

        # Force close (allow requests)
        result = service.force_close(
            service_name="external_api",
            reason="Service recovered",
            controlled_by=admin_user,
            trigger_replay=True
        )

        # Check if requests should be allowed
        if service.should_allow("external_api"):
            # proceed with request

    For testing with mock repository:
        mock_repo = Mock(spec=CircuitBreakerStateRepository)
        service = CircuitBreakerService(repository=mock_repo)
    """

    _event_source = "circuit_breaker_service"

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        repository: CircuitBreakerStateRepository | None = None,
    ):
        """
        Initialize the circuit breaker service.

        Args:
            config: Optional configuration, loads from settings if None
            repository: Optional repository for DI, uses Django adapter if None
        """
        self.config = config or CircuitBreakerConfig.from_settings()
        self._repository = repository

        # MeshCoordinator integration: list of downstream-state pre-check functions
        # checker(service_name) → True means downstream healthy, False triggers preemptive Fallback
        self._downstream_checkers: list[Callable[[str], bool]] = []

        # MeshCoordinator integration: per-service threshold-override map
        self._threshold_overrides: dict[str, Any] = {}

    @property
    def repository(self) -> CircuitBreakerStateRepository:  # type: ignore[override]
        """Get the repository using ProviderRegistry with fallback policy."""
        if self._repository is None:
            from baldur.adapters.memory import (
                InMemoryCircuitBreakerStateRepository,
            )
            from baldur.core.di_fallback import resolve_with_fallback
            from baldur.factory import ProviderRegistry

            self._repository = resolve_with_fallback(
                registry_method=lambda: ProviderRegistry.get_circuit_breaker_repo(),
                fallback_class=InMemoryCircuitBreakerStateRepository,
                service_name=self.__class__.__name__,
            )
        return self._repository

    @property
    def is_enabled(self) -> bool:  # type: ignore[override]
        """Check if circuit breaker is enabled."""
        return self.config.enabled

    # =========================================================================
    # Mesh Coordinator Extension Points
    # =========================================================================

    def register_downstream_checker(
        self,
        checker: Callable[[str], bool],
    ) -> None:
        """
        Register a should_allow() pre-check hook.

        checker(service_name) → False triggers a preemptive Fallback.
        checker MUST only perform local in-memory lookups (no external I/O).
        """
        self._downstream_checkers.append(checker)

    def apply_threshold_override(self, service_name: str, override: Any) -> None:
        """
        Apply a threshold override set by the mesh coordinator.

        While the override is active, the service's failure_threshold and
        recovery_timeout use the override values.
        """
        self._threshold_overrides[service_name] = override

    def remove_threshold_override(self, service_name: str) -> None:
        """Remove the threshold override and revert to the original config."""
        self._threshold_overrides.pop(service_name, None)

    @staticmethod
    def _record_preemptive_fallback_metric() -> None:
        """Record the preemptive Fallback metric (graceful degradation)."""
        try:
            from baldur.metrics.prometheus import get_metrics

            metrics = get_metrics()
            if getattr(metrics, "_initialized", False) and hasattr(
                metrics, "mesh_preemptive_fallback_total"
            ):
                metrics.mesh_preemptive_fallback_total.inc()
        except Exception:
            pass

    def get_effective_config(self, service_name: str) -> CircuitBreakerConfig:
        """
        Return the effective config with overrides applied.

        Lookup happens in the L1 local cache, so there is no external I/O.
        Without an override, returns the base config; otherwise replaces only the overridden fields.
        """
        if service_name not in self._threshold_overrides:
            return self.config

        override = self._threshold_overrides[service_name]
        if now() > override.expires_at:
            self._threshold_overrides.pop(service_name)
            return self.config

        return CircuitBreakerConfig(
            **{
                **vars(self.config),
                "failure_threshold": override.adjusted_failure_threshold,
                "recovery_timeout": override.adjusted_recovery_timeout,
            }
        )

    # =========================================================================
    # State Query Operations
    # =========================================================================

    def get_or_create_state(self, service_name: str) -> CircuitBreakerStateData:
        """
        Get or create a circuit breaker state for a service.

        Args:
            service_name: Name of the external service

        Returns:
            CircuitBreakerStateData instance
        """
        return self.repository.get_or_create(service_name)

    def get_state(self, service_name: str) -> str:
        """
        Get the current state of a circuit breaker.

        Args:
            service_name: Name of the external service

        Returns:
            Current state (closed, open, half_open)
        """
        state = self.get_or_create_state(service_name)
        return state.state

    def should_allow(self, service_name: str) -> bool:
        """
        Check if requests should be allowed through the circuit breaker.

        Post-476: HALF_OPEN slot acquisition is delegated to the repository's
        atomic ``try_acquire_half_open_slot`` so the per-service counter is
        cluster-wide accurate (Redis Lua) instead of per-process best-effort.

        Args:
            service_name: Name of the external service

        Returns:
            True if requests should be allowed, False if blocked
        """
        if not self.is_enabled:
            return True
        return self._evaluate_admission(service_name).allowed

    def should_allow_with_state(self, service_name: str) -> CircuitBreakerDecision:
        """Companion API to ``should_allow`` that returns the admission decision
        and the resolved state in a single call.

        Closes the redundant ``get_or_create_state`` lookup that
        ``CircuitBreakerPolicy.execute()`` previously incurred on the reject
        path: the policy can now read ``decision.allowed`` for branching and
        ``decision.state.state`` for the rejection metadata without a second
        repository RLock acquire.

        For ``is_enabled=False`` callers we return ``CircuitBreakerDecision``
        with ``allowed=True`` and a freshly-fetched state — direct callers of
        the companion API contract receive a non-None state regardless of
        feature-flag posture. ``CircuitBreakerPolicy`` short-circuits on
        ``is_enabled`` before invoking this method, so the disabled-CB fetch
        is off the hot path.
        """
        if not self.is_enabled:
            return CircuitBreakerDecision(
                allowed=True,
                state=self.get_or_create_state(service_name),
            )
        return self._evaluate_admission(service_name)

    def _evaluate_admission(self, service_name: str) -> CircuitBreakerDecision:  # noqa: C901
        """Shared admission-decision body for ``should_allow`` /
        ``should_allow_with_state``.

        Caller MUST have already verified ``self.is_enabled`` — this method
        does not re-check it. Returns a ``CircuitBreakerDecision`` whose
        ``state`` reflects any post-atomic-acquire transition that happened
        during the call.
        """
        # Downstream-state pre-check (MeshCoordinator integration)
        # Performs only O(1) in-memory lookups, no external I/O
        for checker in self._downstream_checkers:
            try:
                if not checker(service_name):
                    logger.info(
                        "circuit_breaker.downstream_preemptive_fallback",
                        service=service_name,
                    )
                    self._record_preemptive_fallback_metric()
                    return CircuitBreakerDecision(
                        allowed=False,
                        state=self.get_or_create_state(service_name),
                    )
            except Exception as e:
                logger.warning(
                    "circuit_breaker.downstream_checker_failed",
                    service=service_name,
                    error=str(e),
                )

        state = self.get_or_create_state(service_name)

        if state.state == CircuitState.CLOSED:
            return CircuitBreakerDecision(allowed=True, state=state)

        effective_config = self.get_effective_config(service_name)

        # OPEN with recovery_timeout NOT yet elapsed: short-circuit reject.
        if state.state == CircuitState.OPEN:
            elapsed = (
                (now() - state.opened_at).total_seconds()
                if state.opened_at is not None
                else 0.0
            )
            if state.opened_at is None or elapsed < effective_config.recovery_timeout:
                self._record_blocked_metric(service_name, "open")
                return CircuitBreakerDecision(allowed=False, state=state)

        # OPEN with elapsed timeout, OR already HALF_OPEN — atomic acquire.
        # The repository's Lua / RLock primitive owns the state-machine
        # decision (OPEN→HALF_OPEN combo / HALF_OPEN increment / rejected /
        # stuck-recovery auto-reset). Single-winner semantics close G3/G4.
        from baldur.settings.circuit_breaker import get_circuit_breaker_settings

        settings = get_circuit_breaker_settings()
        allowed, prev_state, new_state = self.repository.try_acquire_half_open_slot(
            service_name=service_name,
            limit=effective_config.half_open_max_calls,
            stuck_timeout_seconds=settings.half_open_stuck_timeout_seconds,
        )

        # Emit transition event/audit iff this thread caused the OPEN→HALF_OPEN
        # transition (single-winner from the atomic primitive — closes R1).
        if (
            prev_state == CircuitState.OPEN.value
            and new_state == CircuitState.HALF_OPEN.value
        ):
            log_cb_state_change_audit(
                cb_name=service_name,
                old_state=CircuitState.OPEN,
                new_state=CircuitState.HALF_OPEN,
                reason=(
                    f"auto_recovery: recovery_timeout "
                    f"({effective_config.recovery_timeout}s) elapsed"
                ),
            )

            from baldur.services.event_bus import EventType

            self._emit_event(
                EventType.CIRCUIT_BREAKER_HALF_OPENED,
                data={
                    "service_name": service_name,
                    "previous_state": "open",
                    "timestamp": now().isoformat(),
                    "trigger": "auto",
                },
            )

        if not allowed:
            reason = (
                "half_open_full"
                if new_state == CircuitState.HALF_OPEN.value
                else "open"
            )
            self._record_blocked_metric(service_name, reason)
            logger.debug(
                "circuit_breaker.half_open_limit_reached"
                if reason == "half_open_full"
                else "circuit_breaker.open_blocked",
                service=service_name,
                limit=effective_config.half_open_max_calls,
            )

        # Reflect any post-acquire transition (OPEN→HALF_OPEN, HALF_OPEN reset)
        # in the returned state without an extra repository fetch. Production
        # repos return ``CircuitBreakerStateData`` (a regular dataclass) so
        # ``replace`` rebinds the state field cleanly; tests sometimes mock
        # the repo to return a Mock instance — guard the replace path so the
        # mocked attribute stays as the test set it up.
        if state.state != new_state:
            from dataclasses import is_dataclass, replace

            if is_dataclass(state) and not isinstance(state, type):
                state = replace(state, state=new_state)
        return CircuitBreakerDecision(allowed=allowed, state=state)

    @staticmethod
    def _record_blocked_metric(service_name: str, reason: str) -> None:
        """Record a CB-blocked request.

        ``record_blocked`` is hoisted to module top. The recorder
        lookup inside it uses a sticky failure flag so a missing
        ``prometheus_client`` no longer pays the import cost on every reject.
        """
        try:
            record_blocked(service_name, reason)
        except Exception as e:
            logger.debug(
                "circuit_breaker.blocked_metric_failed",
                error=e,
            )

    def should_allow_with_fallback(
        self,
        service_name: str,
        cache_key: str | None = None,
        default_response: Any | None = None,
        request_data: dict[str, Any] | None = None,
    ) -> CircuitBreakerFallbackResult:
        """
        Check if requests should be allowed with fallback strategy support.

        .. deprecated::
            This method is deprecated.
            Use a CircuitBreakerPolicy + FallbackPolicy combination instead.

        When CB is open, instead of simply blocking, this method can:
        1. Return cached (stale) data
        2. Queue the request to DLQ for later retry
        3. Return a default/static response

        Args:
            service_name: Name of the external service
            cache_key: Optional Redis key for cached data lookup
            default_response: Optional default response to return
            request_data: Optional request data for DLQ queueing

        Returns:
            CircuitBreakerFallbackResult with decision and optional fallback data
        """
        import warnings

        warnings.warn(
            "should_allow_with_fallback() is deprecated. "
            "Use CircuitBreakerPolicy + FallbackPolicy combination instead.",
            DeprecationWarning,
            stacklevel=2,
        )

        if not self.is_enabled:
            return CircuitBreakerFallbackResult.allow()

        state = self.get_or_create_state(service_name)

        if state.state == CircuitState.CLOSED:
            return CircuitBreakerFallbackResult.allow()

        if state.state == CircuitState.HALF_OPEN:
            # Allow limited requests for testing
            return CircuitBreakerFallbackResult.allow()

        # CB is OPEN - apply fallback strategy
        strategy = self.config.fallback_strategy

        if strategy == "cache" and cache_key:
            # Try to get cached data
            cached_data = self._get_cached_data(cache_key)
            if cached_data is not None:
                logger.info(
                    "circuit_breaker.stale_cache_served",
                    service_name=service_name,
                    cache_key=cache_key,
                )
                return CircuitBreakerFallbackResult.from_cache(
                    data=cached_data,
                    message=f"Circuit open for {service_name}, serving cached data",
                )

        if strategy == "dlq" and request_data:
            # Queue to DLQ for later retry
            success = self._enqueue_to_dlq(service_name, request_data)
            if success:
                logger.info(
                    "circuit_breaker.request_queued_to_dlq",
                    service_name=service_name,
                )
                return CircuitBreakerFallbackResult.to_dlq(
                    message=f"Circuit open for {service_name}, request queued for retry"
                )

        if strategy == "default_response" and default_response is not None:
            logger.info(
                "circuit_breaker.default_response_returned",
                service_name=service_name,
            )
            return CircuitBreakerFallbackResult.default_response(
                data=default_response,
                message=f"Circuit open for {service_name}, using default response",
            )

        # Default: block
        return CircuitBreakerFallbackResult.block(
            message=f"Circuit breaker open for {service_name}"
        )

    def _get_cached_data(self, cache_key: str) -> Any | None:
        """
        Get cached data from cache provider.

        Args:
            cache_key: Cache key for the data

        Returns:
            Cached data or None if not found/expired
        """
        try:
            from baldur.factory import ProviderRegistry

            cache = ProviderRegistry.get_cache()
            return cache.get(cache_key)
        except Exception as e:
            logger.debug(
                "circuit_breaker.cache_lookup_failed",
                error=e,
            )
            return None

    def _enqueue_to_dlq(
        self,
        service_name: str,
        request_data: dict[str, Any],
    ) -> bool:
        """
        Enqueue a failed request to DLQ for later retry.

        Args:
            service_name: Name of the service
            request_data: Request data to queue

        Returns:
            True if successfully queued
        """
        try:
            store_to_dlq(
                domain="circuit_breaker",
                failure_type=f"cb_fallback_{service_name}",
                request_data=request_data,
                error_message=f"Circuit breaker open for {service_name}",
                snapshot_data={"service_name": service_name, "fallback_type": "dlq"},
            )
            return True
        except Exception as e:
            logger.exception(
                "circuit_breaker.enqueue_dlq_failed",
                error=e,
            )
            return False

    def get_total_calls(self, service_name: str) -> int:
        """
        Get total call count for a service (success + failure).

        Used for minimum_calls check to prevent false positives.

        Args:
            service_name: Name of the external service

        Returns:
            Total number of calls tracked
        """
        state = self.get_or_create_state(service_name)
        # Total calls = failure_count + success_count
        return state.failure_count + state.success_count

    def get_all_states(self) -> list[dict[str, Any]]:
        """
        Get all circuit breaker states.

        Returns:
            List of state dictionaries
        """
        states = self.repository.get_all_states()
        return [
            {
                "service_name": s.service_name,
                "state": s.state,
                "failure_count": s.failure_count,
                "success_count": s.success_count,
                "last_failure_at": (
                    s.last_failure_at.isoformat() if s.last_failure_at else None
                ),
                "opened_at": s.opened_at.isoformat() if s.opened_at else None,
                "manually_controlled": s.manually_controlled,
                "controlled_by_id": s.controlled_by_id,
                "control_reason": s.control_reason,
                "metadata": s.metadata,
            }
            for s in states
        ]

    def get_open_states(
        self, limit: int | None = None
    ) -> list[CircuitBreakerStateData]:
        """Get circuit breaker states currently in OPEN state.

        More efficient than get_all_states() for watchdog recovery which
        only needs OPEN states. Delegates to repository.get_open_states()
        which uses SCAN instead of KEYS in Redis.

        Args:
            limit: Maximum number of results. None means no limit.

        Returns:
            List of CircuitBreakerStateData with state == OPEN,
            ordered by opened_at ascending (oldest first).
        """
        return self.repository.get_open_states(limit)

    def get_aggregate_failure_rate(self) -> float:
        """Return the system-wide circuit-breaker failure fraction (0.0-1.0).

        Aggregates over every tracked circuit-breaker state as
        ``sum(failure_count) / sum(failure_count + success_count)``, returning
        ``0.0`` when no calls have been recorded across any circuit.

        This is a system-wide **mean** error fraction, not a fixed-time-window
        or per-service rate: a single failing service among many healthy ones
        is averaged below threshold, while a broad multi-service failure raises
        the mean. That makes it suited to a system-wide stability gate, where
        each individual service is still protected by its own circuit breaker.

        Returns:
            Failure fraction in the range 0.0-1.0.
        """
        total_failures = 0
        total_calls = 0
        for state in self.repository.get_all_states():
            total_failures += state.failure_count
            total_calls += state.failure_count + state.success_count
        if total_calls == 0:
            return 0.0
        return total_failures / total_calls

    # =========================================================================
    # Failure/Success Recording (for automatic mode)
    # =========================================================================

    def record_failure(
        self,
        service_name: str,
        error_context: dict[str, Any] | None = None,
        hint_state: CircuitBreakerStateData | None = None,
    ) -> None:
        """
        Record a failure for a service.

        This is used for automatic circuit breaker mode.
        If the threshold is exceeded AND minimum_calls is met, the circuit opens automatically.

        Args:
            service_name: Name of the external service
            error_context: Optional context about the failure (for snapshot)
            hint_state: Optional pre-fetched state — when the caller
                already loaded the state via ``should_allow_with_state``,
                passing it here skips a redundant repository lookup. The hint
                is used only when its ``service_name`` matches; otherwise we
                fall through to a fresh fetch. Failures always increment
                counters, so the hint cannot fully skip the repository write.
        """
        if not self.is_enabled:
            return

        if hint_state is not None and hint_state.service_name == service_name:
            state = hint_state
        else:
            state = self.get_or_create_state(service_name)

        # Skip if manually controlled
        if state.manually_controlled:
            logger.debug(
                "circuit_breaker.skipping_failure_recording_manually",
                service_name=service_name,
            )
            return

        # HALF_OPEN failure → atomically revert to OPEN (656 D7).
        if state.state == CircuitState.HALF_OPEN:
            # The atomic primitive performs the OPEN state write under the
            # repository's cluster-single-winner guarantee (InMemory RLock /
            # Redis Lua / SQL row-lock / Layered L2-authoritative routing).
            # `did_open` is the single-fire gate for the entire side-effect
            # block, mirroring the close-side `circuit_closed` check — only the
            # caller that performed the HALF_OPEN->OPEN transition emits the
            # CIRCUIT_BREAKER_OPENED event + audit + metrics, eliminating the
            # multi-worker duplicate-emit race (#498 F11). Gating only the EB
            # emit would leave the metrics/audit residue 498 deferred.
            attempt = self.repository.record_failure_with_open_check(service_name)

            if attempt.did_open:
                logger.warning(
                    "circuit_breaker.half_open_test_failed",
                    service_name=service_name,
                )

                log_cb_state_change_audit(
                    cb_name=service_name,
                    old_state=CircuitState.HALF_OPEN,
                    new_state=CircuitState.OPEN,
                    reason="half_open_test_failure: request failed during recovery testing",
                )

                try:
                    from baldur.metrics.event_handlers import (
                        CircuitBreakerEventHandler,
                    )

                    CircuitBreakerEventHandler.on_state_changed(
                        service=service_name,
                        from_state="half_open",
                        to_state="open",
                    )
                except ImportError:
                    pass

                from baldur.services.event_bus import EventType

                self._emit_event(
                    EventType.CIRCUIT_BREAKER_OPENED,
                    data={
                        "service_name": service_name,
                        "previous_state": "half_open",
                        "timestamp": now().isoformat(),
                        "trigger": "half_open_failure",
                    },
                )
            return

        # Use repository to record failure (handles atomic update)
        updated_state = self.repository.record_failure(service_name)

        # Check if threshold exceeded and circuit should open
        effective_config = self.get_effective_config(service_name)
        should_open = self._should_open_circuit(updated_state, effective_config)

        if should_open and updated_state.state == "closed":
            # Collect snapshot before opening
            snapshot = self._collect_failure_snapshot(
                service_name, updated_state, error_context
            )

            # Open the circuit
            self.repository.update_state(
                service_name=service_name,
                state="open",
                opened_at=now(),
            )

            # Log with snapshot
            logger.warning(
                "circuit_breaker.circuit_auto_opened_failures",
                service_name=service_name,
                updated_state=updated_state.failure_count,
                total_calls=self.get_total_calls(service_name),
            )

            # Save audit log with snapshot
            self._log_circuit_open_audit(service_name, snapshot)

            # Apply burn rate multiplier to Error Budget
            self._apply_burn_rate_multiplier(service_name)

            # Push event - record the CB state-change metric
            try:
                from baldur.metrics.event_handlers import (
                    CircuitBreakerEventHandler,
                )

                CircuitBreakerEventHandler.on_state_changed(
                    service=service_name,
                    from_state="closed",
                    to_state="open",
                )
            except ImportError:
                pass  # Metrics not available

            # EventBus emission — Auto OPEN (outside _apply_burn_rate_multiplier
            # try-except to guarantee emission regardless of EB consumer failures)
            from baldur.services.event_bus import EventType

            self._emit_event(
                EventType.CIRCUIT_BREAKER_OPENED,
                data={
                    "service_name": service_name,
                    "previous_state": "closed",
                    "timestamp": now().isoformat(),
                    "trigger": "auto",
                },
            )

    def _should_open_circuit(
        self,
        state: CircuitBreakerStateData,
        effective_config: CircuitBreakerConfig | None = None,
    ) -> bool:
        """
        Determine if circuit should be opened based on failure threshold and minimum calls.

        Implements both count-based and rate-based thresholds with minimum_calls protection.
        Rate-based threshold uses sliding_window_size to bound the calculation,
        ensuring that cumulative-count pollution is prevented even when the
        repository does not provide a window-based count.

        Args:
            state: Current circuit breaker state
            effective_config: Optional overridden config from MeshCoordinator

        Returns:
            True if circuit should open
        """
        cfg = effective_config or self.config
        total_calls = state.failure_count + state.success_count

        # Sliding Window: cap total_calls at the window size (§9)
        # InMemoryRepo (ring buffer) already returns a window-based count, so this is a no-op.
        # For non-windowed repos such as RedisRepo, this prevents cumulative-count pollution.
        window_size = cfg.sliding_window_size
        if window_size > 0 and total_calls > window_size:
            logger.debug(
                "circuit_breaker.capping",
                target_service_name=state.service_name,
                total_calls=total_calls,
                window_size=window_size,
            )
            # For rate calculation, use only the count within the window range
            # count-based threshold uses the original failure_count (§9.4)
            total_calls = window_size

        # Check minimum_calls - prevent false positives with low traffic
        if total_calls < cfg.minimum_calls:
            logger.debug(
                "circuit_breaker.opening_skipped",
                target_service_name=state.service_name,
                total_calls=total_calls,
                minimum_calls=cfg.minimum_calls,
            )
            return False

        # Check rate-based threshold if configured
        if cfg.failure_rate_threshold > 0:
            failure_rate = (
                (state.failure_count / total_calls * 100) if total_calls > 0 else 0
            )
            if failure_rate >= cfg.failure_rate_threshold:
                logger.info(
                    "circuit_breaker.rate_threshold_exceeded",
                    target_service_name=state.service_name,
                    failure_rate=failure_rate,
                    failure_rate_threshold=cfg.failure_rate_threshold,
                    window_size=window_size,
                )
                return True

        # Check count-based threshold
        return state.failure_count >= cfg.failure_threshold

    def _collect_failure_snapshot(
        self,
        service_name: str,
        state: CircuitBreakerStateData,
        error_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Collect a snapshot of system state when circuit opens.

        This data is valuable for post-mortem analysis and ML training.

        Args:
            service_name: Name of the service
            state: Current circuit breaker state
            error_context: Optional error context

        Returns:
            Snapshot dictionary with failure details
        """
        snapshot = {
            "service_name": service_name,
            "timestamp": now().isoformat(),
            "circuit_breaker": {
                "failure_count": state.failure_count,
                "success_count": state.success_count,
                "total_calls": state.failure_count + state.success_count,
                "failure_rate_percent": (
                    state.failure_count
                    / (state.failure_count + state.success_count)
                    * 100
                    if (state.failure_count + state.success_count) > 0
                    else 0
                ),
                "threshold_config": {
                    "failure_threshold": self.config.failure_threshold,
                    "minimum_calls": self.config.minimum_calls,
                    "failure_rate_threshold": self.config.failure_rate_threshold,
                },
            },
            "trigger_reason": "auto_threshold_exceeded",
        }

        # Add system metrics if available
        try:
            from baldur.services.system_metrics_cache import (
                get_system_metrics_cache,
            )

            cache = get_system_metrics_cache()
            if cache.is_running():
                snapshot["system_metrics"] = {
                    "cpu_percent": cache.get_cpu_percent(),
                    "memory_percent": cache.get_memory_percent(),
                }
            else:
                import psutil

                snapshot["system_metrics"] = {
                    "cpu_percent": psutil.cpu_percent(interval=None),
                    "memory_percent": psutil.virtual_memory().percent,
                }
        except Exception:
            pass  # system_metrics_cache or psutil not available

        # Add error context if provided
        if error_context:
            snapshot["error_context"] = error_context

        # Add latency metrics if available
        try:
            from baldur.metrics.reliability_manager import get_reliability_manager

            manager = get_reliability_manager()
            latency_info = manager.get_effective_value("latency", service_name)
            if latency_info:
                snapshot["latency"] = {
                    "value": latency_info[0],
                    "source": latency_info[1],
                }
        except Exception:
            pass  # Metrics not available

        return snapshot

    def _log_circuit_open_audit(
        self, service_name: str, snapshot: dict[str, Any]
    ) -> None:
        """
        Log circuit open event to audit log with snapshot.

        Uses unified audit_helpers.log_cb_state_change_audit for:
        - WAL-based zero-loss guarantee
        - Hash chain integrity connection
        - Consistent CB audit format

        Args:
            service_name: Name of the service
            snapshot: Failure snapshot data
        """
        try:
            # Read values from the snapshot (supports both flat and nested structures)
            cb_data = snapshot.get("circuit_breaker", {})
            failure_count = cb_data.get("failure_count") or snapshot.get(
                "failure_count", "N/A"
            )
            threshold_data = cb_data.get("threshold_config", {})
            threshold_value = threshold_data.get("failure_threshold") or snapshot.get(
                "threshold", "N/A"
            )
            reason = (
                f"auto_trigger|failures={failure_count}|threshold={threshold_value}"
            )

            log_cb_state_change_audit(
                cb_name=service_name,
                old_state="closed",
                new_state="open",
                reason=reason,
                request=None,  # System-triggered, no HTTP context
            )

            # Log detailed snapshot separately for debugging
            logger.info(
                "circuit_breaker.audit_logged",
                service_name=service_name,
                snapshot=snapshot,
            )
        except Exception as e:
            logger.debug(
                "circuit_breaker.audit_log_failed",
                error=e,
            )

    def _apply_burn_rate_multiplier(self, service_name: str) -> None:
        """
        Apply burn rate multiplier to Error Budget when CB opens.

        Directly consumes error budget via AtomicBudgetConsumer.
        EventBus emission is handled by the caller (record_failure).

        Args:
            service_name: Name of the service
        """
        try:
            from baldur_pro.services.error_budget.atomic_consumer import (
                get_atomic_budget_consumer,
            )

            consumer = get_atomic_budget_consumer()
            result = consumer.consume_atomic(
                namespace=service_name,
                raw_minutes=self.config.cb_open_base_consumption_minutes,
                multiplier=self.config.cb_open_burn_rate_multiplier,
                budget_key=f"baldur:{service_name}:error_budget",
            )

            logger.info(
                "circuit_breaker.error_budget_consumed",
                success=result.success,
                consumed_minutes=result.consumed_minutes,
                degraded_mode=result.degraded_mode,
                service_name=service_name,
            )

        except ImportError:
            logger.debug("circuit_breaker.error_budget_consumer_unavailable")
        except Exception as e:
            logger.debug(
                "circuit_breaker.burn_rate_multiplier_failed",
                error=e,
            )

    def record_success(
        self,
        service_name: str,
        hint_state: CircuitBreakerStateData | None = None,
    ) -> None:
        """
        Record a success for a service.

        This is used for automatic circuit breaker mode.
        In half-open state, enough successes will close the circuit.

        Args:
            service_name: Name of the external service
            hint_state: Optional pre-fetched state — when the caller
                already loaded the state via ``should_allow_with_state``,
                passing it here unlocks two optimizations:
                  1. Fast path: when the hint indicates steady-state CLOSED
                     (manually_controlled=False, failure_count=0), the call
                     returns immediately without touching the repository — the
                     eventual ``update_state(failure_count=0)`` is a no-op.
                  2. Slow path: skips the redundant ``get_or_create_state``
                     repository read when the hint's ``service_name`` matches.
                Stale-hint cases are observably equivalent to the no-hint path:
                a missed reset is corrected by the next call's slow path.
        """
        if not self.is_enabled:
            return

        # 490 D4 fast path: steady-state CLOSED + zero failures means
        # update_state(failure_count=0) would be a no-op. Skip all repository
        # I/O. Stale hints (state has drifted to OPEN/HALF_OPEN since the
        # hint was taken) fall through; a missed reset is corrected by the
        # next record_*'s slow path.
        if (
            hint_state is not None
            and hint_state.service_name == service_name
            and hint_state.state == CircuitState.CLOSED
            and not hint_state.manually_controlled
            and hint_state.failure_count == 0
        ):
            return

        if hint_state is not None and hint_state.service_name == service_name:
            state = hint_state
        else:
            state = self.get_or_create_state(service_name)

        # Skip if manually controlled
        if state.manually_controlled:
            logger.debug(
                "circuit_breaker.skipping_success_recording_manually",
                service_name=service_name,
            )
            return

        circuit_closed = False

        if state.state == "half_open":
            # 497 D1/D2: atomic record-success + threshold-check + close in
            # one repository call. The `did_close` flag is the single-fire
            # emit gate — only the caller that crossed the threshold under
            # the repository lock sees True, eliminating the multi-fire race
            # where N stale-view callers would each pass an unlocked
            # `state.state == half_open` check.
            attempt = self.repository.record_success_with_close_check(
                service_name, self.config.success_threshold
            )
            circuit_closed = attempt.did_close

        elif state.state == "closed":
            # Reset failure count on success in closed state
            self.repository.update_state(
                service_name=service_name,
                state="closed",
                failure_count=0,
            )

        if circuit_closed:
            logger.info(
                "circuit_breaker.circuit_auto_closed_successes",
                service_name=service_name,
                success_threshold=self.config.success_threshold,
            )

            # Audit - auto-recovery complete (HALF_OPEN → CLOSED)
            log_cb_state_change_audit(
                cb_name=service_name,
                old_state="half_open",
                new_state="closed",
                reason=f"auto_recovery: success_threshold ({self.config.success_threshold}) reached",
            )
            # Push event - record the CB state-change metric
            try:
                from baldur.metrics.event_handlers import (
                    CircuitBreakerEventHandler,
                )

                CircuitBreakerEventHandler.on_state_changed(
                    service=service_name,
                    from_state="half_open",
                    to_state="closed",
                )
            except ImportError:
                pass  # Metrics not available

            # EventBus emission — Auto CLOSE.
            # The EventBus handler ``_on_circuit_breaker_closed`` is the
            # single dispatch path to ``conditional_replay_on_circuit_close``.
            from baldur.services.event_bus import EventType

            self._emit_event(
                EventType.CIRCUIT_BREAKER_CLOSED,
                data={
                    "service_name": service_name,
                    "previous_state": "half_open",
                    "timestamp": now().isoformat(),
                    "trigger": "auto",
                    "trigger_replay": True,
                },
            )

    # =========================================================================
    # Recovery Transition Check (for periodic task)
    # =========================================================================

    def check_recovery_transitions(self) -> dict:
        """
        Check for circuit breakers that should transition from OPEN to HALF_OPEN.

        This method should be called periodically (e.g., every minute) to check
        if any OPEN circuits have exceeded the recovery timeout and should
        transition to HALF_OPEN for testing.

        Returns:
            Dictionary with transitioned service names and count
        """
        if not self.is_enabled:
            return {"success": True, "message": "Circuit breaker disabled", "count": 0}

        transitioned = []

        try:
            # Get all states and filter for OPEN, non-manually-controlled ones
            all_states = self.repository.get_all_states()
            open_states = [
                s
                for s in all_states
                if s.state == CircuitState.OPEN and not s.manually_controlled
            ]

            for state in open_states:
                if state.opened_at is None:
                    continue

                elapsed = (now() - state.opened_at).total_seconds()

                effective_cfg = self.get_effective_config(state.service_name)
                if elapsed >= effective_cfg.recovery_timeout:
                    # Transition to half-open
                    self.repository.update_state(
                        service_name=state.service_name,
                        state=CircuitState.HALF_OPEN,
                        success_count=0,
                    )
                    transitioned.append(state.service_name)
                    logger.info(
                        "circuit_breaker.transitioned_open_after",
                        target_service_name=state.service_name,
                        elapsed=elapsed,
                    )

            return {
                "success": True,
                "transitioned": transitioned,
                "count": len(transitioned),
            }

        except Exception as e:
            logger.exception(
                "circuit_breaker.error_checking_recovery_transitions",
                error=e,
            )
            return {
                "success": False,
                "error": str(e),
                "transitioned": transitioned,
                "count": len(transitioned),
            }

    def manual_control(
        self,
        service_name: str,
        action: str,
        reason: str = "",
        controlled_by: Any = None,
    ) -> CircuitBreakerResult:
        """
        Manually control a circuit breaker state.

        Args:
            service_name: Name of the service
            action: 'open', 'close', or 'auto'
            reason: Reason for the control action
            controlled_by: User who initiated the action

        Returns:
            CircuitBreakerResult with operation details
        """
        state = self.get_or_create_state(service_name)
        previous_state = state.state

        # ActorContext is the source of truth for "who triggered this" —
        # force_open/force_close read it via ActorContext.get_current();
        # we fold the explicit controlled_by argument into the reason so it
        # still surfaces in the audit trail even if the caller did not set
        # an ActorContext (e.g., test/CLI flows).
        if controlled_by is not None and controlled_by:
            reason = (
                f"{reason} (controlled_by={controlled_by})"
                if reason
                else f"controlled_by={controlled_by}"
            )
        if action == "open":
            return self.force_open(
                service_name=service_name,
                reason=reason,
            )
        if action == "close":
            return self.force_close(
                service_name=service_name,
                reason=reason,
            )
        # auto
        # Release manual control — keep state/counters, clear only the manual-control flag
        self.repository.clear_manual_control(service_name, preserve_reason=True)
        logger.info(
            "circuit_breaker.switched_auto_mode",
            service_name=service_name,
        )
        return CircuitBreakerResult(
            success=True,
            service_name=service_name,
            previous_state=previous_state,
            new_state=state.state,
            message=f"Circuit breaker for '{service_name}' switched to auto mode",
        )

    # =========================================================================
    # Ring Resize Reconciliation — orphan CB cleanup
    # =========================================================================

    def reconcile_cb_cell_mapping(self) -> dict[str, Any]:
        """
        Reconcile CB-to-Cell mapping consistency after a Ring Resize.

        1. Iterate over all CBs and extract cell_id from the Composite Key
        2. Compare against the correct cell_id per the current Hash Ring
        3. On mismatch: archive + delete the orphan CB (no state transition)
        4. CBs for new Cells are lazily created by get_or_create()

        Returns:
            ``{"archived": [...], "errors": [...]}``
        """
        from baldur.core.cb_namespace import (
            parse_composite_cb_name,
        )
        from baldur.services.cell_topology import get_cell_registry

        registry = get_cell_registry()
        result: dict[str, Any] = {"archived": [], "errors": []}

        try:
            all_states = self.repository.get_all_states()

            for state in all_states:
                base_name, old_cell_id = parse_composite_cb_name(state.service_name)
                if not old_cell_id:
                    continue  # legacy single key — skip

                # The correct Cell per the current Hash Ring
                current_cell_id = registry.get_cell_for_key(base_name)

                if old_cell_id != current_cell_id:
                    # Orphan CB — delete (no state copy allowed)
                    try:
                        self.repository.delete_state(state.service_name)
                        result["archived"].append(state.service_name)
                    except Exception as e:
                        result["errors"].append(
                            {
                                "service_name": state.service_name,
                                "error": str(e),
                            }
                        )

        except Exception as e:
            logger.exception(
                "circuit_breaker.reconciliation_failed",
                error=e,
            )
            result["errors"].append({"error": str(e)})

        return result
