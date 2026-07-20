"""
Namespaced Emergency Tracker.

Manages Emergency state independently per region.
Global state takes precedence over every region.

Key features:
- Independent per-namespace state management (get_state, set_state)
- Global/Regional precedence resolution (get_effective_state)
- AtomicStateQuery integration (atomic lookup)
- EscalationAuditTrail integration (decision recording)

Redis key layout:
- baldur:governance:emergency_state (Global)
- baldur:{namespace}:governance:emergency_state (Regional)

Code reference:
    governance.py (existing EmergencyModeTracker pattern)
    core/state_backend.py (StateBackend interface)
    regional_emergency/atomic_query.py (AtomicStateQuery)
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any

import structlog

from baldur.models.emergency import EmergencyLevel, EmergencyScope, ScopedEmergencyState
from baldur.services.event_bus.emitter import EventEmitterMixin
from baldur.utils.jitter import calculate_jitter
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

GLOBAL_NAMESPACE = "global"
"""Global namespace identifier."""


def _get_emergency_expiry_hours() -> int:
    """Load the Emergency expiry duration from Settings."""
    try:
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        return get_regional_emergency_settings().expiry_hours
    except ImportError:
        return 8  # default


def _get_cache_ttl_seconds() -> float:
    """Load the local cache TTL from Settings."""
    try:
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        return get_regional_emergency_settings().cache_ttl_seconds
    except ImportError:
        return 30.0  # default


# Backward-compatibility constants (not recommended)
DEFAULT_EMERGENCY_EXPIRY_HOURS = 8
"""Default Emergency state expiry (8 hours). Prefer _get_emergency_expiry_hours()."""

CACHE_TTL_SECONDS = 30.0
"""Local cache TTL (30 seconds). Prefer _get_cache_ttl_seconds()."""


class NamespacedEmergencyTracker(EventEmitterMixin):
    """
    Namespace-aware Emergency tracker with cross-pod sync.

    Manages Emergency state independently per region.
    Global state takes precedence over every region.

    Key features:
    - get_state(namespace): read the state of a specific namespace
    - set_state(namespace, state): persist state
    - get_effective_state(namespace): read the effective state after precedence
    - activate_emergency(namespace, level): activate Emergency
    - deactivate_emergency(namespace): deactivate Emergency
    - get_all_active_namespaces(): list active namespaces

    Precedence (Safety-Max):
    1. Admin Override (ADMIN_OVERRIDE, KILL_SWITCH): explicit regional override
    2. Global STRICT: a global emergency puts every region in STRICT
    3. Regional: local state

    Cross-pod sync:
    - EventBus subscription for cache invalidation on external events
    - Event emission on state changes for cross-pod propagation

    Usage:
        tracker = NamespacedEmergencyTracker()

        # Activate Emergency for the Seoul region
        tracker.activate_emergency(
            namespace="seoul",
            level=EmergencyLevel.LEVEL_3,
            activated_by="admin@company.com",
            reason="DB failure detected",
        )

        # Read the effective state (Global precedence applied)
        state = tracker.get_effective_state("seoul")
        if state.governance_mode == "STRICT":
            # Handle STRICT mode
            ...
    """

    # Redis key pattern
    STATE_KEY_PATTERN = "governance:emergency_state"

    # EventEmitterMixin: event source identifier
    _event_source = "namespaced_tracker"

    def __init__(
        self,
        backend: Any | None = None,
        atomic_query: Any | None = None,
        audit_trail: Any | None = None,
    ):
        """
        Initialize NamespacedEmergencyTracker.

        Args:
            backend: StateBackend instance (resolved automatically if None)
            atomic_query: AtomicStateQuery instance (resolved automatically if None)
            audit_trail: EscalationAuditTrail instance (resolved automatically if None)
        """
        self._backend = backend
        self._atomic_query = atomic_query
        self._audit_trail = audit_trail
        self._lock = threading.RLock()
        self._subscribed = False

        # Local cache (reduces network round trips)
        self._local_cache: dict[str, ScopedEmergencyState] = {}
        self._cache_timestamps: dict[str, float] = {}

        # EventBus subscription for cross-pod cache invalidation
        self._register_event_handlers()

    # =========================================================================
    # EventBus Integration (Cross-pod Sync)
    # =========================================================================

    def _register_event_handlers(self) -> None:
        """EventBus subscription for cache invalidation."""
        if self._subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.subscribe(
                EventType.EMERGENCY_LEVEL_CHANGED,
                self._on_external_emergency_changed,
            )
            self._subscribed = True
        except Exception as e:
            logger.debug(
                "namespaced_tracker.event_bus_registration_skipped",
                error=str(e),
            )

    def close(self) -> None:
        """Unsubscribe all EventBus handlers."""
        if not self._subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.unsubscribe(
                EventType.EMERGENCY_LEVEL_CHANGED,
                self._on_external_emergency_changed,
            )
            self._subscribed = False
        except ImportError:
            pass
        except Exception:
            pass

    def _on_external_emergency_changed(self, event: Any) -> None:
        """Invalidate cache on external event."""
        if event.source == self._event_source:
            return  # Skip self-originated events

        namespace = event.data.get("namespace") if hasattr(event, "data") else None
        # Global events should invalidate ALL caches, not just "global" namespace
        if namespace == "global":
            namespace = None
        self.invalidate_cache(namespace)
        logger.debug(
            "namespaced_tracker.cache_invalidated_external",
            namespace=namespace,
        )

    def _emit_state_change(
        self,
        namespace: str,
        state: ScopedEmergencyState,
        previous_level: EmergencyLevel,
    ) -> None:
        """Emit state change event for cross-pod propagation."""
        from baldur.services.event_bus import EventType

        self._emit_event(
            EventType.EMERGENCY_LEVEL_CHANGED,
            data={
                "namespace": namespace,
                "scope": state.scope.value,
                "level": state.emergency_level.value,
                "previous_level": previous_level.value,
                "reason": state.reason,
                "activated_by": state.activated_by,
                "is_active": state.emergency_level != EmergencyLevel.NORMAL,
                "is_escalation": state.emergency_level.severity
                > previous_level.severity,
            },
        )

    def _get_cache_ttl_with_jitter(self) -> float:
        """Cache TTL with jitter to prevent thundering herd.

        Jitter is proportional to base TTL (±10%) to ensure:
        - Large TTL (30s): ±3s jitter spreads Redis queries
        - Small TTL (1s): ±0.1s jitter preserves test determinism
        """
        base_ttl = _get_cache_ttl_seconds()
        # Proportional jitter: ±10% of base TTL
        jitter_range = base_ttl * 0.1
        jitter = calculate_jitter(
            max_delay_seconds=jitter_range, min_delay_seconds=-jitter_range
        )
        return max(base_ttl + jitter, 0.1)  # Ensure minimum 100ms TTL

    # =========================================================================
    # Backend Access
    # =========================================================================

    def _get_backend(self) -> Any:
        """Obtain the StateBackend instance."""
        if self._backend is None:
            from baldur.core.state_backend import get_state_backend

            self._backend = get_state_backend()
        return self._backend

    def _get_atomic_query(self) -> Any:
        """Obtain the AtomicStateQuery instance."""
        if self._atomic_query is None:
            try:
                from baldur.services.regional_emergency.atomic_query import (
                    get_atomic_state_query,
                )

                self._atomic_query = get_atomic_state_query()
            except Exception:
                # Stay None in environments without Redis
                pass
        return self._atomic_query

    def _get_audit_trail(self) -> Any:
        """Obtain the EscalationAuditTrail instance."""
        if self._audit_trail is None:
            from baldur.services.regional_emergency.escalation_audit import (
                get_escalation_audit_trail,
            )

            self._audit_trail = get_escalation_audit_trail()
        return self._audit_trail

    def _get_current_namespace(self) -> str:
        """
        Obtain the namespace of the current instance.

        Identifies the current region from ClusterIdentity.region.
        """
        try:
            from baldur.core.cluster_identity import get_cluster_identity

            identity = get_cluster_identity()
            return identity.region or GLOBAL_NAMESPACE
        except Exception:
            return GLOBAL_NAMESPACE

    def _get_state_key(self, namespace: str) -> str:
        """
        Build the Redis key for a namespace.

        Args:
            namespace: target namespace

        Returns:
            Redis key (e.g. "baldur:seoul:governance:emergency_state")
        """
        if namespace == GLOBAL_NAMESPACE:
            return f"baldur:{self.STATE_KEY_PATTERN}"
        return f"baldur:{namespace}:{self.STATE_KEY_PATTERN}"

    # =========================================================================
    # State CRUD
    # =========================================================================

    def get_state(self, namespace: str | None = None) -> ScopedEmergencyState:
        """
        Read the state of a specific namespace (cache-backed).

        Args:
            namespace: target namespace (current instance if None)

        Returns:
            ScopedEmergencyState (default value if absent)
        """
        ns = namespace or self._get_current_namespace()
        return self._load_state(ns)

    def set_state(
        self,
        state: ScopedEmergencyState,
        namespace: str | None = None,
    ) -> None:
        """
        Persist state.

        Args:
            state: state to persist
            namespace: target namespace (uses state.namespace if None)
        """
        ns = namespace or state.namespace
        self._save_state(ns, state)

    def get_effective_state(
        self,
        namespace: str | None = None,
        precedence: str | None = None,
    ) -> ScopedEmergencyState:
        """
        Read the effective Emergency state (precedence applied).

        Uses AtomicStateQuery to read Global+Regional atomically and determines
        the effective state according to precedence.

        Precedence:
        1. Admin Override (precedence >= ADMIN_OVERRIDE): region wins
        2. Global STRICT: a global emergency puts every region in STRICT
        3. Regional: local state

        Args:
            namespace: namespace to query (current instance if None)
            precedence: command precedence ("AUTO", "ADMIN_OVERRIDE", etc.)

        Returns:
            The effective ScopedEmergencyState
        """
        ns = namespace or self._get_current_namespace()

        # Try AtomicStateQuery first
        atomic_query = self._get_atomic_query()
        if atomic_query is not None:
            try:
                state_dict, decision_type, reason = atomic_query.query_effective_state(
                    namespace=ns,
                    precedence=precedence,
                )

                # Audit record (significant decisions only)
                if decision_type in ("GLOBAL_OVERRIDE", "ADMIN_OVERRIDE"):
                    audit = self._get_audit_trail()
                    audit.log_decision(
                        decision_type=decision_type,
                        decision_reason=reason,
                        namespace=ns,
                        effective_state=state_dict,
                        triggered_by="AtomicStateQuery",
                        precedence=precedence,
                    )

                return ScopedEmergencyState.from_dict(state_dict)

            except Exception as e:
                logger.warning(
                    "namespaced_tracker.atomicstatequery_failed_falling_back",
                    error=e,
                )

        # Fallback: manual lookup (2 Redis calls)
        return self._get_effective_state_manual(ns, precedence)

    def _get_effective_state_manual(
        self,
        namespace: str,
        precedence: str | None = None,
    ) -> ScopedEmergencyState:
        """
        Manual effective-state lookup (AtomicStateQuery fallback).

        Two Redis calls (Global + Regional).
        """
        with self._lock:
            global_state = self._load_state(GLOBAL_NAMESPACE)
            regional_state = self._load_state(namespace)

            # Precedence check (Regional wins at ADMIN_OVERRIDE or above)
            if precedence in ("ADMIN_OVERRIDE", "KILL_SWITCH"):
                return regional_state

            # Safety-Max: the stricter of the two states. Only the Global side
            # needs testing here -- when Global is not STRICT the Regional state
            # is returned as-is, which is already the answer for both a STRICT
            # and a non-STRICT Regional.
            global_is_strict = (
                global_state.is_active() and global_state.governance_mode == "STRICT"
            )

            if global_is_strict:
                # Global STRICT overrides Regional
                return ScopedEmergencyState(
                    namespace=namespace,
                    emergency_level=global_state.emergency_level,
                    governance_mode="STRICT",
                    scope=EmergencyScope.GLOBAL,  # Mark that it came from Global
                    activated_at=global_state.activated_at,
                    activated_by=global_state.activated_by,
                    reason=f"Global override: {global_state.reason or 'N/A'}",
                )

            return regional_state

    # =========================================================================
    # Emergency Lifecycle
    # =========================================================================

    def activate_emergency(
        self,
        level: EmergencyLevel,
        activated_by: str,
        reason: str,
        namespace: str | None = None,
        scope: EmergencyScope = EmergencyScope.REGIONAL,
        expiry_hours: int | None = None,
    ) -> ScopedEmergencyState:
        """
        Activate Emergency mode.

        Args:
            level: Emergency level (LEVEL_1, LEVEL_2, LEVEL_3)
            activated_by: actor that activated it (user ID or "system")
            reason: activation reason
            namespace: target namespace (current instance if None)
            scope: application scope (REGIONAL or GLOBAL)
            expiry_hours: expiry duration (defaults to 8 hours if None)

        Returns:
            The activated ScopedEmergencyState
        """
        target_ns = namespace or self._get_current_namespace()

        # GLOBAL scope is stored under the global namespace
        if scope == EmergencyScope.GLOBAL:
            target_ns = GLOBAL_NAMESPACE

        # Compute expiry (loaded from Settings)
        hours = expiry_hours or _get_emergency_expiry_hours()
        expires_at = utc_now() + timedelta(hours=hours)

        # Determine governance mode (STRICT at LEVEL_2 or above)
        governance_mode = "STRICT" if level >= EmergencyLevel.LEVEL_2 else "NORMAL"

        with self._lock:
            # Load previous state for event emission
            previous = self._load_state(target_ns)
            previous_level = previous.emergency_level

            state = ScopedEmergencyState(
                namespace=target_ns,
                emergency_level=level,
                governance_mode=governance_mode,
                scope=scope,
                activated_at=utc_now(),
                activated_by=activated_by,
                reason=reason,
                expires_at=expires_at,
            )

            self._save_state(target_ns, state)

            # Cross-pod event emission
            self._emit_state_change(target_ns, state, previous_level)

            logger.warning(
                "namespaced_tracker.emergency_activated",
                target_ns=target_ns,
                emergency_level_name=level.value,
                governance_mode=governance_mode,
                activated_by=activated_by,
            )

            return state

    def deactivate_emergency(
        self,
        deactivated_by: str,
        namespace: str | None = None,
        scope: EmergencyScope = EmergencyScope.REGIONAL,
    ) -> ScopedEmergencyState:
        """
        Deactivate Emergency mode.

        Args:
            deactivated_by: actor that deactivated it
            namespace: target namespace (current instance if None)
            scope: application scope

        Returns:
            The deactivated ScopedEmergencyState
        """
        target_ns = namespace or self._get_current_namespace()

        if scope == EmergencyScope.GLOBAL:
            target_ns = GLOBAL_NAMESPACE

        with self._lock:
            # Load previous state for event emission
            previous = self._load_state(target_ns)
            previous_level = previous.emergency_level

            state = ScopedEmergencyState(
                namespace=target_ns,
                emergency_level=EmergencyLevel.NORMAL,
                governance_mode="NORMAL",
                scope=scope,
                activated_at=None,
                activated_by=None,
                reason=f"Deactivated by {deactivated_by}",
                expires_at=None,
            )

            self._save_state(target_ns, state)

            # Cross-pod event emission
            self._emit_state_change(target_ns, state, previous_level)

            logger.info(
                "namespaced_tracker.emergency_deactivated",
                target_ns=target_ns,
                deactivated_by=deactivated_by,
            )

            return state

    def get_all_active_namespaces(self) -> list[str]:
        """
        List every namespace with an active Emergency.

        Returns:
            List of namespaces with an active Emergency
        """
        active = []
        backend = self._get_backend()

        try:
            # Pattern-match every emergency_state key
            all_states = backend.get_all(f"*{self.STATE_KEY_PATTERN}")

            for key, data in all_states.items():
                if data and data.get("emergency_level", "normal") != "normal":
                    # Extract the namespace from the key
                    # baldur:seoul:governance:emergency_state -> seoul
                    parts = key.split(":")
                    if len(parts) >= 2:
                        ns = parts[1] if parts[0] == "baldur" else parts[0]
                        active.append(ns)
        except Exception as e:
            logger.warning(
                "namespaced_tracker.scan_namespaces_failed",
                error=e,
            )

        return active

    # =========================================================================
    # Cache Management
    # =========================================================================

    def invalidate_cache(self, namespace: str | None = None) -> None:
        """
        Invalidate the cache.

        Args:
            namespace: namespace to invalidate (all namespaces if None)
        """
        with self._lock:
            if namespace:
                cache_key = f"state:{namespace}"
                self._local_cache.pop(cache_key, None)
                self._cache_timestamps.pop(cache_key, None)
            else:
                self._local_cache.clear()
                self._cache_timestamps.clear()

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _load_state(self, namespace: str) -> ScopedEmergencyState:
        """Load state from backend with local cache and expiry guard."""
        cache_key = f"state:{namespace}"
        now = time.time()

        # Check local cache (TTL with jitter for thundering herd prevention)
        cache_ttl = self._get_cache_ttl_with_jitter()
        with self._lock:
            if cache_key in self._local_cache:
                cache_time = self._cache_timestamps.get(cache_key, 0)
                if now - cache_time < cache_ttl:
                    return self._local_cache[cache_key]

        # Backend lookup
        backend = self._get_backend()
        key = self._get_state_key(namespace)
        data = backend.get(key)

        state = None
        if data:
            loaded = ScopedEmergencyState.from_dict(data)
            if not loaded.is_expired():
                state = loaded

        if state is None:
            state = ScopedEmergencyState(
                namespace=namespace,
                emergency_level=EmergencyLevel.NORMAL,
                governance_mode="NORMAL",
                scope=(
                    EmergencyScope.REGIONAL
                    if namespace != GLOBAL_NAMESPACE
                    else EmergencyScope.GLOBAL
                ),
            )

        # Store in local cache
        with self._lock:
            self._local_cache[cache_key] = state
            self._cache_timestamps[cache_key] = now

        return state

    def _save_state(self, namespace: str, state: ScopedEmergencyState) -> None:
        """Save state to backend with TTL propagation."""
        backend = self._get_backend()
        key = self._get_state_key(namespace)
        ttl_seconds: int | None = None

        if state.is_active() and state.expires_at is not None:
            remaining = (state.expires_at - utc_now()).total_seconds()
            if remaining < 1:
                state = ScopedEmergencyState(
                    namespace=namespace,
                    emergency_level=EmergencyLevel.NORMAL,
                    governance_mode="NORMAL",
                    scope=state.scope,
                )
                logger.warning(
                    "namespaced_tracker.emergency_state_expired_on_save",
                    namespace=namespace,
                )
            else:
                ttl_seconds = int(remaining)

        backend.set(key, state.to_dict(), ttl_seconds=ttl_seconds)

        # Invalidate local cache
        self.invalidate_cache(namespace)


# =============================================================================
# Singleton
# =============================================================================

_namespaced_tracker: NamespacedEmergencyTracker | None = None
_tracker_lock = threading.Lock()


def get_namespaced_emergency_tracker() -> NamespacedEmergencyTracker:
    """Return the NamespacedEmergencyTracker singleton."""
    global _namespaced_tracker

    if _namespaced_tracker is None:
        with _tracker_lock:
            if _namespaced_tracker is None:
                _namespaced_tracker = NamespacedEmergencyTracker()

    return _namespaced_tracker


def reset_namespaced_emergency_tracker() -> None:
    """Reset the singleton (for tests)."""
    global _namespaced_tracker
    with _tracker_lock:
        if _namespaced_tracker is not None:
            _namespaced_tracker.close()
        _namespaced_tracker = None
