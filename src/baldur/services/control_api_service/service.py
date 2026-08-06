"""
Control API Service - Service

Defines the ControlAPIService class, the singleton instance, and the get_control_api_service() function.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog

from baldur.core.constants import (
    ControlAPIActions,
    ControlAPIEnvironments,
)
from baldur.settings.circuit_breaker import MAX_MANUAL_OVERRIDE_TTL_MINUTES
from baldur.utils.time import utc_now

from .models import ControlRequest, ControlResponse
from .risk import assess_risk_level, classify_reason

logger = structlog.get_logger()

# Degradation order used when two raw circuit-breaker names project onto one
# canonical row key. The merged row reports the worst of them, so a merge can
# never hide an open breaker behind a closed one.
_CB_STATE_SEVERITY: dict[str, int] = {"closed": 0, "half_open": 1, "open": 2}


# =============================================================================
# Null Object — CircuitBreakerService (D1, doc 426)
# =============================================================================


class _NullCBResult:
    """Null result for circuit breaker operations when CB is unavailable."""

    def __init__(self, service_name: str = ""):
        self.success = False
        self.service_name = service_name
        self.previous_state = ""
        self.new_state = ""
        self.message = "Circuit breaker service unavailable"
        self.error = "Circuit breaker service unavailable"


class _NullCBState:
    """Null state for circuit breaker queries when CB is unavailable."""

    failure_count: int = 0
    success_count: int = 0
    last_failure_at = None
    state: str = "closed"
    manually_controlled: bool = False
    control_reason: str = ""


class NullCircuitBreakerService:
    """Null Object for CircuitBreakerService when CB module is unavailable.

    Follows existing Null Object pattern (adapters/audit/null_adapter.py,
    services/chaos/resilience_validator.py:NoOpCircuitBreakerStatusProvider).
    All operations return safe no-op values — callers require zero changes.
    """

    def force_close(self, service_name, reason="", trigger_replay=False, **kwargs):
        return _NullCBResult(service_name)

    def force_open(self, service_name, reason="", **kwargs):
        return _NullCBResult(service_name)

    # Named for the method the real service actually has. The previous name
    # (`reset_to_default`) existed ONLY here, so this Null object was the sole
    # implementer of the method its caller called — which is how the reset path
    # went to its AttributeError fallback on every real install.
    def reset(self, service_name, reason="", **kwargs):
        return _NullCBResult(service_name)

    def record_failure(self, service_name, **kwargs):
        pass

    def record_success(self, service_name, **kwargs):
        pass

    def get_or_create_state(self, service_name):
        return _NullCBState()

    def get_all_states(self):
        return []


# =============================================================================
# Control API Service
# =============================================================================


class ControlAPIService:
    """
    Baldur Control API Service.

    Provides a unified, auditable, reversible, and governed control surface
    to manage reliability behaviors across testing, chaos experimentation,
    and real production operations.

    Usage:
        service = ControlAPIService()

        # Execute control action
        response = service.execute(ControlRequest(
            service_name="payment",
            action="allow",
            reason="PG recovered",
            environment="ops"
        ))

        # Get current status
        status = service.get_status(environment="ops")

        # Get audit logs
        logs = service.get_audit_logs(service_name="payment")
    """

    def __init__(self):
        """Initialize the Control API Service."""
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            self.circuit_breaker = get_circuit_breaker_service()
        except ImportError:
            logger.debug("control_api.circuit_breaker_unavailable")
            self.circuit_breaker = NullCircuitBreakerService()
        except Exception as exc:
            logger.warning("control_api.circuit_breaker_init_failed", error=str(exc))
            self.circuit_breaker = NullCircuitBreakerService()

        # replay_service: 0 call sites in ControlAPIService methods (dead reference)
        try:
            from baldur.services.replay_service import ReplayService

            self.replay_service = ReplayService()
        except ImportError:
            logger.debug("control_api.replay_service_unavailable")
            self.replay_service = None
        except Exception as exc:
            logger.warning("control_api.replay_service_init_failed", error=str(exc))
            self.replay_service = None

        # Failure injection state (in-memory for chaos/test)
        self._failure_injections: dict[str, dict] = {}

    # =========================================================================
    # Main Execution
    # =========================================================================

    def execute(self, request: ControlRequest) -> ControlResponse:
        """
        Execute a control API action.

        Args:
            request: Control request

        Returns:
            ControlResponse with outcome
        """
        # 1. Pre-execution validation
        validation_error = self._validate_request(request)
        if validation_error:
            return validation_error

        # 2. Assess risk
        risk_level = assess_risk_level(request.action, request.environment)

        # 3. Execute action
        try:
            if request.action == ControlAPIActions.ALLOW:
                response = self._execute_allow(request)
            elif request.action == ControlAPIActions.BLOCK:
                response = self._execute_block(request)
            elif request.action == ControlAPIActions.OVERRIDE:
                response = self._execute_override(request)
            elif request.action == ControlAPIActions.RESET:
                response = self._execute_reset(request)
            elif request.action == ControlAPIActions.INJECT_FAILURE:
                response = self._execute_inject_failure(request)
            elif request.action == ControlAPIActions.INJECT_SUCCESS:
                response = self._execute_inject_success(request)
            else:
                response = ControlResponse(
                    status="error",
                    action_applied=request.action,
                    error_code="UNKNOWN_ACTION",
                    error_message=f"Unknown action: {request.action}",
                )
        except Exception as e:
            logger.exception(
                "control_api.error_executing_action",
                error=e,
            )
            response = ControlResponse(
                status="error",
                action_applied=request.action,
                error_code="EXECUTION_ERROR",
                error_message=str(e),
            )

        # 4. Add metadata
        response.reason_classification = classify_reason(request.reason)
        response.risk_level = risk_level
        response.correlation_id = request.request_id

        # 5. Record audit (best-effort)
        self._record_audit(request, response)

        return response

    # =========================================================================
    # Action Implementations
    # =========================================================================

    def _execute_allow(self, request: ControlRequest) -> ControlResponse:
        """
        Execute allow action - enable service operations.

        Maps to: Circuit Breaker → CLOSED state
        """
        result = self.circuit_breaker.force_close(
            service_name=request.service_name,
            reason=request.reason,
            trigger_replay=request.metadata.get("trigger_replay", False),
            ttl_minutes=request.ttl_minutes,
        )

        if result.success:
            return ControlResponse(
                status="success",
                action_applied="allow",
                system_state="allow",
                effective_until=(
                    result.expires_at.isoformat() if result.expires_at else None
                ),
                evidence=self._gather_evidence(request.service_name),
            )
        return ControlResponse(
            status="error",
            action_applied="allow",
            error_code="CIRCUIT_BREAKER_ERROR",
            error_message=result.error or "Failed to close circuit breaker",
        )

    def _execute_block(self, request: ControlRequest) -> ControlResponse:
        """
        Execute block action - disable service operations.

        Maps to: Circuit Breaker → OPEN state
        """
        result = self.circuit_breaker.force_open(
            service_name=request.service_name,
            reason=request.reason,
            ttl_minutes=request.ttl_minutes,
        )

        if result.success:
            return ControlResponse(
                status="success",
                action_applied="block",
                system_state="block",
                # Read back from storage, not recomputed: the reported lift
                # time is the one actually stored, so the response cannot
                # promise an expiry the breaker does not have.
                effective_until=(
                    result.expires_at.isoformat() if result.expires_at else None
                ),
                evidence=self._gather_evidence(request.service_name),
            )
        return ControlResponse(
            status="error",
            action_applied="block",
            error_code="CIRCUIT_BREAKER_ERROR",
            error_message=result.error or "Failed to open circuit breaker",
        )

    def _execute_override(self, request: ControlRequest) -> ControlResponse:
        """
        Execute override action - temporarily bypass rules.

        Allows operations even when normal rules would block them.
        """
        # For override, we force close (allow) with a TTL
        result = self.circuit_breaker.force_close(
            service_name=request.service_name,
            reason=f"OVERRIDE: {request.reason}",
            ttl_minutes=request.ttl_minutes,
        )

        if result.success:
            return ControlResponse(
                status="success",
                action_applied="override",
                system_state="allow",
                effective_until=(
                    result.expires_at.isoformat() if result.expires_at else None
                ),
                evidence=self._gather_evidence(request.service_name),
            )
        return ControlResponse(
            status="error",
            action_applied="override",
            error_code="OVERRIDE_ERROR",
            error_message=result.error or "Failed to apply override",
        )

    def _execute_reset(self, request: ControlRequest) -> ControlResponse:
        """
        Execute reset action - revert to default configuration.

        Clears all manual overrides and returns to policy defaults, which is
        what ``reset()`` does: ``atomic_reset`` closes the circuit, zeroes the
        counters and clears ``manually_controlled``.

        This called ``reset_to_default()`` until 2026-08-03 — a name that only
        ever existed on ``NullCircuitBreakerService``. Every real service raised
        ``AttributeError`` and the ``except`` branch below it "fell back" to
        ``force_close``, so the operator's recovery action pinned a permanent
        manual override instead of clearing one, and that breaker could never
        open again. The fallback is gone with it: a reset that cannot reset must
        report failure, never quietly do the opposite.
        """
        result = self.circuit_breaker.reset(
            service_name=request.service_name,
            reason=request.reason,
        )

        if getattr(result, "success", True):
            return ControlResponse(
                status="success",
                action_applied="reset",
                system_state="allow",  # Default state is allow
                evidence=self._gather_evidence(request.service_name),
            )
        return ControlResponse(
            status="error",
            action_applied="reset",
            error_code="CIRCUIT_BREAKER_ERROR",
            error_message=getattr(result, "error", None)
            or "Failed to reset circuit breaker",
        )

    def _execute_inject_failure(self, request: ControlRequest) -> ControlResponse:
        """
        Execute inject_failure action - simulate failures.

        Only allowed in test and chaos environments.

        Supports two modes:
        1. Configuration mode: Sets up failure injection config for future requests
        2. Trigger CB mode: Immediately records N failures to trigger Circuit Breaker
           - Use metadata: {"trigger_cb_failures": 5} to record 5 failures immediately
           - This will naturally open the CB without setting manually_controlled=True
        """
        # Check for immediate CB trigger mode
        trigger_cb_failures = request.metadata.get("trigger_cb_failures", 0)

        if trigger_cb_failures > 0:
            # Record failures to naturally trigger CB OPEN
            for _i in range(trigger_cb_failures):
                self.circuit_breaker.record_failure(request.service_name)

            state = self.circuit_breaker.get_or_create_state(request.service_name)

            logger.info(
                "control_api.triggered_failures",
                trigger_cb_failures=trigger_cb_failures,
                request_service_name=request.service_name,
                circuit_breaker_state=state.state,
                failure_count=state.failure_count,
            )

            return ControlResponse(
                status="success",
                action_applied="inject_failure",
                system_state="block" if state.state == "open" else "allow",
                evidence={
                    "failures_triggered": trigger_cb_failures,
                    "cb_state": state.state,
                    "failure_count": state.failure_count,
                    "manually_controlled": state.manually_controlled,
                },
            )

        # Original configuration mode - store failure injection config
        failure_config = {
            "enabled": True,
            "failure_rate": request.metadata.get("failure_rate", 1.0),
            "simulate_latency_ms": request.metadata.get("simulate_latency_ms", 0),
            "failure_type": request.metadata.get("failure_type", "exception"),
            "expires_at": None,
        }

        if request.ttl_minutes:
            failure_config["expires_at"] = utc_now() + timedelta(
                minutes=request.ttl_minutes
            )

        self._failure_injections[request.service_name] = failure_config

        logger.info(
            "control_api.failure_injection_enabled",
            request_service_name=request.service_name,
            failure_config=failure_config["failure_rate"],
            failure_type=failure_config["failure_type"],
        )

        effective_until = None
        if failure_config["expires_at"]:
            effective_until = failure_config["expires_at"].isoformat()

        return ControlResponse(
            status="success",
            action_applied="inject_failure",
            system_state="block",  # Failures being injected
            effective_until=effective_until,
            evidence={
                "failure_rate": failure_config["failure_rate"],
                "failure_type": failure_config["failure_type"],
            },
        )

    def _execute_inject_success(self, request: ControlRequest) -> ControlResponse:
        """
        Execute inject_success action - simulate successful requests.

        Only allowed in test and chaos environments.
        Used to help Circuit Breaker recover from HALF_OPEN to CLOSED state.

        Supports:
        - metadata: {"success_count": N} to record N successes
        """
        success_count = request.metadata.get("success_count", 1)

        # Record successes to help CB recover
        for _i in range(success_count):
            self.circuit_breaker.record_success(request.service_name)

        state = self.circuit_breaker.get_or_create_state(request.service_name)

        logger.info(
            "control_api.recorded_successes",
            success_count=success_count,
            request_service_name=request.service_name,
            circuit_breaker_state=state.state,
            state_success_count=state.success_count,
        )

        return ControlResponse(
            status="success",
            action_applied="inject_success",
            system_state="allow" if state.state == "closed" else "half_open",
            evidence={
                "successes_recorded": success_count,
                "cb_state": state.state,
                "success_count": state.success_count,
                "manually_controlled": state.manually_controlled,
            },
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _validate_request(self, request: ControlRequest) -> ControlResponse | None:
        """
        Validate the request before execution.

        Returns ControlResponse with error if validation fails, None if valid.
        """
        # inject_failure forbidden in ops
        if (
            request.action == ControlAPIActions.INJECT_FAILURE
            and request.environment == ControlAPIEnvironments.OPS
        ):
            return ControlResponse(
                status="rejected",
                action_applied=request.action,
                error_code="ACTION_FORBIDDEN_IN_ENVIRONMENT",
                error_message="inject_failure is forbidden in ops environment",
            )

        # inject_success forbidden in ops
        if (
            request.action == ControlAPIActions.INJECT_SUCCESS
            and request.environment == ControlAPIEnvironments.OPS
        ):
            return ControlResponse(
                status="rejected",
                action_applied=request.action,
                error_code="ACTION_FORBIDDEN_IN_ENVIRONMENT",
                error_message="inject_success is forbidden in ops environment",
            )

        # TTL range, for every action that pins a manual override.
        #
        # Guarded before it compares: this method runs outside execute()'s
        # try/except, and the quick-action routes put the raw JSON value into
        # ControlRequest without a type check — so a non-int reaching a
        # comparison here would surface as a 500 instead of a rejection.
        if (
            request.action
            in (
                ControlAPIActions.ALLOW,
                ControlAPIActions.BLOCK,
                ControlAPIActions.OVERRIDE,
            )
            and request.ttl_minutes is not None
        ):
            ttl = request.ttl_minutes
            if (
                not isinstance(ttl, int)
                or isinstance(ttl, bool)
                or ttl < 1
                or ttl > MAX_MANUAL_OVERRIDE_TTL_MINUTES
            ):
                return ControlResponse(
                    status="rejected",
                    action_applied=request.action,
                    error_code="TTL_OUT_OF_RANGE",
                    error_message=(
                        f"ttl_minutes must be an integer between 1 and "
                        f"{MAX_MANUAL_OVERRIDE_TTL_MINUTES} (got: {ttl!r})"
                    ),
                )

        # override in ops requires TTL (max 60)
        if (
            request.action == ControlAPIActions.OVERRIDE
            and request.environment == ControlAPIEnvironments.OPS
        ):
            if not request.ttl_minutes:
                return ControlResponse(
                    status="rejected",
                    action_applied=request.action,
                    error_code="TTL_REQUIRED_FOR_OPS_OVERRIDE",
                    error_message="TTL is required for override action in ops environment",
                )
            if request.ttl_minutes > 60:
                return ControlResponse(
                    status="rejected",
                    action_applied=request.action,
                    error_code="TTL_EXCEEDS_OPS_LIMIT",
                    error_message=f"TTL cannot exceed 60 minutes in ops (got: {request.ttl_minutes})",
                )

        return None

    def _gather_evidence(self, service_name: str) -> dict:
        """
        Gather evidence metrics for a service.

        Returns dict with recent metrics.
        """
        try:
            state = self.circuit_breaker.get_or_create_state(service_name)
            return {
                "failure_count": state.failure_count,
                "success_count": state.success_count,
                "last_failure_at": (
                    state.last_failure_at.isoformat() if state.last_failure_at else None
                ),
            }
        except Exception as e:
            logger.warning(
                "control_api.gather_evidence_failed",
                error=e,
            )
            return {}

    def _record_audit(self, request: ControlRequest, response: ControlResponse):
        """
        Record the action in audit log.

        Best-effort - never blocks the response.
        """
        try:
            logger.info(
                "control_api.audit",
                request_action=request.action,
                service_name=request.service_name,
                environment=request.environment,
                response_status=response.status,
                actor_id=request.actor,
                risk_level=response.risk_level,
                reason=request.reason,
            )
        except Exception as e:
            logger.warning(
                "control_api.record_audit_failed",
                error=e,
            )

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_status(self, environment: str = "ops") -> dict:
        """
        Get the current status of all services.

        Args:
            environment: Current environment context

        Returns:
            Status dictionary with all service states
        """
        states = self.circuit_breaker.get_all_states()

        return {
            "services": states,
            "environment": environment,
            "timestamp": utc_now().isoformat(),
        }

    def get_service_status(self, service_name: str) -> dict:
        """
        Get the status of a specific service.

        Args:
            service_name: Service to check

        Returns:
            Service state dictionary
        """
        state = self.circuit_breaker.get_or_create_state(service_name)

        return {
            "service_name": service_name,
            "state": state.state,
            "failure_count": state.failure_count,
            "success_count": state.success_count,
            "last_failure_at": state.last_failure_at,
            "manually_controlled": state.manually_controlled,
            "control_reason": state.control_reason,
        }

    def is_failure_injection_active(self, service_name: str) -> bool:
        """
        Check if failure injection is active for a service.

        Args:
            service_name: Service to check

        Returns:
            True if failures should be injected
        """
        config = self._failure_injections.get(service_name)
        if not config or not config.get("enabled"):
            return False

        # Check expiration
        if config.get("expires_at") and utc_now() > config["expires_at"]:
            del self._failure_injections[service_name]
            return False

        return True

    def get_failure_injection_config(self, service_name: str) -> dict | None:
        """
        Get failure injection configuration for a service.

        Args:
            service_name: Service to check

        Returns:
            Configuration dict or None
        """
        if not self.is_failure_injection_active(service_name):
            return None
        return self._failure_injections.get(service_name)

    @staticmethod
    def _resolve_circuit_breaker_repo() -> Any:
        """Return the repository instance the circuit breakers actually write to.

        Every ``CircuitBreakerPolicy`` builds its service against the
        separately-registered ``"layered"`` repository, while the registry's
        module-load default is the Redis one. Asking for the default here would
        resolve nothing on any deployment whose L2 is absent or degraded, so the
        payload's ``circuit_state`` would be empty for every service precisely
        when breakers are open. Requesting the name reaches the same per-name
        singleton the policies obtained, and its ``get_all_states()`` reads the
        in-process L1 tier, so a deployment with no reachable Redis still yields
        real states.

        ``"layered"`` is only registered inside the redis-client-import guard,
        so on a redis-client-absent install the name is unregistered — hence the
        same fall-back shape the policy uses for its own lookup. When both fail
        the caller sees ``None`` and renders the state as unknown, never as
        "closed".
        """
        from baldur.factory import ProviderRegistry

        try:
            return ProviderRegistry.get_circuit_breaker_repo(name="layered")
        except Exception:
            logger.debug("control_api.layered_cb_repo_unavailable")
        try:
            return ProviderRegistry.circuit_breaker_repo.safe_get()
        except Exception:
            return None

    @staticmethod
    def _canonical_lookup_views(
        cb_states: dict[str, Any], dlq_pending: dict[str, int]
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Re-key the per-row lookup sources into one join vocabulary.

        Four key sources meet on one ``service_name`` column and three of them
        speak different dialects: registered domains and the outcome window are
        canonical, circuit breaker states carry the raw name the application
        protected, and the DLQ breakdown carries that store's own validated
        form. Joining a canonical row against a raw map misses every name
        holding a dot, a hyphen, a space or an uppercase letter — rendering "no
        breaker state" for a breaker the repository knows, and "0 in DLQ" for a
        service with real entries.

        Only the LOOKUP side is re-keyed. The emitted breakdown and the counting
        fields keep the raw keys they ship today: canonicalizing those would move
        three numeric fields for reasons unrelated to the join.

        Two raw names that merge onto one key resolve to the **most degraded**
        state and to the **sum** of the pending counts, so a merged row can never
        under-report an open breaker or an existing backlog.
        """
        from baldur.metrics.registry import canonicalize_domain_label

        states_by_key: dict[str, Any] = {}
        for raw_name, state in cb_states.items():
            key = canonicalize_domain_label(raw_name)
            previous = states_by_key.get(key)
            if previous is None or _CB_STATE_SEVERITY.get(
                state, 0
            ) > _CB_STATE_SEVERITY.get(previous, 0):
                states_by_key[key] = state

        pending_by_key: dict[str, int] = {}
        for raw_name, pending in dlq_pending.items():
            key = canonicalize_domain_label(raw_name)
            pending_by_key[key] = pending_by_key.get(key, 0) + pending

        return states_by_key, pending_by_key

    def get_metrics(self) -> dict:
        """
        Collect comprehensive baldur metrics for trend analysis.

        Returns operational metrics for dashboards, AI agents, and monitoring.
        Unlike status (point-in-time snapshot), metrics provide trend data.

        **Consumers:**
        - Admin UI: Dashboard visualization
        - AI Agent: Automated decision making
        - Prometheus/Grafana: Metrics scraping
        - External Monitoring: Alerting integration

        **What the failure-rate and circuit-state fields mean:**

        All three per-service fields describe **this worker process**: the
        serving worker's own call-outcome window and its own breaker view.
        Responses therefore vary across workers on a multi-worker deployment;
        fleet-level aggregation is the Prometheus surface's job.

        The denominator is calls this worker's circuit breakers **admitted and
        counted** over the last five minutes — not calls that executed. An
        inner stage that exhausts its own budget (a timeout whose task never
        started, for instance) is a failed call from the caller's perspective,
        and the window counts it exactly as the breaker does. Conversely, an
        admitted call whose exception the breaker was configured to ignore is in
        neither the numerator nor the denominator. Under the default
        composition the breaker is outermost, so one admission is one protected
        call; under a retry-over-breaker composition the breaker sits inside the
        retry loop and one admission is one attempt.

        Absence is reported as ``null``, never as zero: unprotected traffic, a
        disabled breaker, an observe-only mode and retry-only compositions are
        all invisible to this producer, and a service with no observed
        admissions renders ``failure_rate_5m: null``. Test-mode traffic is not
        segregated, matching the breaker's own trip evidence. Two service names
        differing only in characters a metric label cannot carry share one row,
        whose rate matches neither; the producer warns when that first happens.
        Beyond the process's domain cap a newly-seen service is **absent from
        the list** rather than folded into another row, and appears once a read
        frees a slot.

        Returns:
            Dictionary with comprehensive metrics data
        """
        import time

        start_time = time.time()

        from baldur.metrics.registry import get_registered_domains
        from baldur.services.circuit_breaker.time_outcome_window import (
            get_call_outcome_window,
        )
        from baldur.services.metrics.updaters import (
            _resolve_pending_total,
            update_dlq_pending_gauges,
            update_retry_success_rates,
        )
        from baldur.utils.time import utc_now as get_now

        current_time = get_now()

        # Every shared source that feeds more than one field is read exactly
        # once and reused from a local. Re-reading either of these produces a
        # response that contradicts itself under concurrent traffic: a domain
        # registered between two registry reads makes the row list longer than
        # the count beside it, and an outcome recorded between two window reads
        # makes the aggregate disagree with the rows it summarises.
        registered_domains = get_registered_domains()
        outcome_window = get_call_outcome_window().snapshot()

        # One DLQ repository snapshot per poll: both gauge updaters and the
        # headline pending total read it, so the response describes a single
        # instant instead of three.
        dlq_stats: dict[str, Any] = {}
        try:
            from baldur.factory import ProviderRegistry

            failed_op_repo = ProviderRegistry.failed_op_repo.safe_get()
            if failed_op_repo:
                dlq_stats = failed_op_repo.get_statistics() or {}
        except Exception:
            pass

        # Per-domain pending breakdown — diagnostic-grade. `or {}` because the
        # updater returns None (holding the previously exported gauge values)
        # when the breakdown is unavailable, rather than zero-filling.
        dlq_pending = update_dlq_pending_gauges(stats=dlq_stats) or {}
        # The headline total reads the O(1) status-pending quantity — the same
        # one the bundled DLQ alerts page on — never the sum of the breakdown:
        # summing renders 0 on exactly the collection failure the adapter omits
        # the breakdown for, disagreeing with the alert that is firing.
        total_dlq_pending = _resolve_pending_total(dlq_stats) or 0

        # Retry success rates — empty while no adapter produces them, so the
        # per-service field below renders null instead of a fabricated 100%.
        retry_rates = update_retry_success_rates(stats=dlq_stats) or {}

        # Circuit breaker states, read from the instance the breakers write to.
        cb_states: dict[str, Any] = {}
        try:
            cb_repo = self._resolve_circuit_breaker_repo()
            if cb_repo:
                all_states = cb_repo.get_all_states()
                for cb in all_states:
                    cb_states[cb.service_name] = cb.state
        except Exception:
            pass

        cb_states_by_key, dlq_by_key = self._canonical_lookup_views(
            cb_states, dlq_pending
        )

        # Aggregate service counts — deliberately still over the raw maps: a
        # canonicalized recount would move three numeric fields for reasons
        # unrelated to the failure rate. The window's keys join the union so the
        # response can never carry more rows than this counts.
        total_services = len(
            set(dlq_pending)
            | set(cb_states)
            | set(registered_domains)
            | set(outcome_window)
        )
        healthy_services = sum(1 for s in cb_states.values() if s == "closed")
        degraded_services = sum(
            1 for s in cb_states.values() if s in ("open", "half_open")
        )

        # Five-minute aggregate, summed from the SAME window snapshot the rows
        # are built from — not a second read of the producer. A call landing
        # between two reads would make the aggregate contradict the rows beside
        # it in one response. Null when nothing was observed; the request count
        # stays 0, which is the honest count for "no observed admissions".
        window_failures = sum(failures for failures, _ in outcome_window.values())
        window_total = sum(total for _, total in outcome_window.values())
        last_5m_failure_rate = window_failures / window_total if window_total else None
        last_5m_request_count = window_total

        # DLQ resolution time — a separate, honestly-sourced measurement that
        # happens to ride the same stats blob.
        avg_time_to_recovery = dlq_stats.get("avg_resolution_time_seconds")

        # auto_allowed/auto_blocked: not yet implemented — counts require audit log query
        # (deferred until governance event volume justifies the query cost).
        # When implementing: use ProviderRegistry.get_audit_adapter().query(
        #     action=AuditAction.GOVERNANCE_BLOCKED, ...
        # )
        auto_allowed = 0
        auto_blocked = 0

        # Build per-service metrics. Rows come from the registered domains
        # UNION the window's keys: the default circuit-breaker-only protect()
        # deliberately never registers a domain, so a service failing every call
        # would otherwise have no row at all — the very case this payload exists
        # to answer. Both sources are canonical and both are members of the
        # total_services union above, so len(services) <= total_services holds
        # by construction.
        services_metrics = []
        for domain in sorted(set(registered_domains) | set(outcome_window)):
            observed = outcome_window.get(domain)
            service_metric = {
                "service_name": domain,
                # Null, never 0.0, when this worker observed no admission for
                # the service: a fabricated zero reads as "healthy" to an
                # operator during an incident.
                "failure_rate_5m": (
                    observed[0] / observed[1] if observed and observed[1] else None
                ),
                # None while no producer computes per-domain success rates —
                # the field models "not measured", never a fabricated 100%.
                "retry_success_rate": retry_rates.get(domain),
                "dlq_count": dlq_by_key.get(domain, 0),
                # Null when the repository view holds no evidence for this
                # service. Absence is "unknown", not "closed" — defaulting to
                # closed asserts that a breaker is fine about breakers this
                # process cannot see.
                "circuit_state": cb_states_by_key.get(domain),
                "avg_recovery_time_seconds": None,
            }
            services_metrics.append(service_metric)

        collection_duration_ms = int((time.time() - start_time) * 1000)

        return {
            "total_services": total_services,
            "healthy_services": healthy_services,
            "degraded_services": degraded_services,
            "last_5m_failure_rate": last_5m_failure_rate,
            "last_5m_request_count": last_5m_request_count,
            "avg_time_to_recovery": avg_time_to_recovery,
            "auto_allowed_count_24h": auto_allowed,
            "auto_blocked_count_24h": auto_blocked,
            "total_dlq_pending": total_dlq_pending,
            "dlq_by_service": dlq_pending,
            "services": services_metrics,
            "timestamp": current_time,
            "collection_duration_ms": collection_duration_ms,
        }


# =============================================================================
# Singleton instance
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_control_api_service, configure_control_api_service, reset_control_api_service = (
    make_singleton_factory("control_api_service", ControlAPIService)
)
