"""
Namespaced Emergency Tracker.

리전별 독립적인 Emergency 상태를 관리합니다.
Global 상태는 모든 리전보다 우선합니다.

주요 기능:
- 네임스페이스별 독립 상태 관리 (get_state, set_state)
- Global/Regional 우선순위 적용 (get_effective_state)
- AtomicStateQuery 통합 (원자적 조회)
- EscalationAuditTrail 연동 (의사결정 기록)

Redis 키 구조:
- baldur:governance:emergency_state (Global)
- baldur:{namespace}:governance:emergency_state (Regional)

Code reference:
    governance.py (EmergencyModeTracker 기존 패턴)
    core/state_backend.py (StateBackend 인터페이스)
    regional_emergency/atomic_query.py (AtomicStateQuery)

Reference:
    docs/baldur/middleware_system/73_NAMESPACE_AWARE_EMERGENCY.md
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
"""Global 네임스페이스 식별자."""


def _get_emergency_expiry_hours() -> int:
    """Settings에서 Emergency 만료 시간 로드."""
    try:
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        return get_regional_emergency_settings().expiry_hours
    except ImportError:
        return 8  # 기본값


def _get_cache_ttl_seconds() -> float:
    """Settings에서 로컬 캐시 TTL 로드."""
    try:
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        return get_regional_emergency_settings().cache_ttl_seconds
    except ImportError:
        return 30.0  # 기본값


# 하위 호환성을 위한 상수 (권장하지 않음)
DEFAULT_EMERGENCY_EXPIRY_HOURS = 8
"""Emergency 상태 기본 만료 시간 (8시간). 권장: _get_emergency_expiry_hours() 사용."""

CACHE_TTL_SECONDS = 30.0
"""로컬 캐시 TTL (30초). 권장: _get_cache_ttl_seconds() 사용."""


class NamespacedEmergencyTracker(EventEmitterMixin):
    """
    네임스페이스 인지형 Emergency 추적기 with cross-pod sync.

    리전별 독립적인 Emergency 상태를 관리합니다.
    Global 상태는 모든 리전보다 우선합니다.

    주요 기능:
    - get_state(namespace): 특정 네임스페이스 상태 조회
    - set_state(namespace, state): 상태 저장
    - get_effective_state(namespace): 우선순위 적용된 유효 상태 조회
    - activate_emergency(namespace, level): Emergency 활성화
    - deactivate_emergency(namespace): Emergency 비활성화
    - get_all_active_namespaces(): 활성 네임스페이스 목록

    우선순위 (Safety-Max):
    1. Admin Override (ADMIN_OVERRIDE, KILL_SWITCH): 명시적 지역 오버라이드
    2. Global STRICT: 전역 비상시 모든 리전 STRICT
    3. Regional: 로컬 상태

    Cross-pod sync:
    - EventBus subscription for cache invalidation on external events
    - Event emission on state changes for cross-pod propagation

    Usage:
        tracker = NamespacedEmergencyTracker()

        # Seoul 리전 Emergency 활성화
        tracker.activate_emergency(
            namespace="seoul",
            level=EmergencyLevel.LEVEL_3,
            activated_by="admin@company.com",
            reason="DB 장애 감지",
        )

        # 유효 상태 조회 (Global 우선순위 적용)
        state = tracker.get_effective_state("seoul")
        if state.governance_mode == "STRICT":
            # STRICT 모드 처리
            ...
    """

    # Redis 키 패턴
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
        NamespacedEmergencyTracker 초기화.

        Args:
            backend: StateBackend 인스턴스 (None이면 자동 획득)
            atomic_query: AtomicStateQuery 인스턴스 (None이면 자동 획득)
            audit_trail: EscalationAuditTrail 인스턴스 (None이면 자동 획득)
        """
        self._backend = backend
        self._atomic_query = atomic_query
        self._audit_trail = audit_trail
        self._lock = threading.RLock()
        self._subscribed = False

        # 로컬 캐시 (네트워크 왕복 감소)
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
        """StateBackend 인스턴스 획득."""
        if self._backend is None:
            from baldur.core.state_backend import get_state_backend

            self._backend = get_state_backend()
        return self._backend

    def _get_atomic_query(self) -> Any:
        """AtomicStateQuery 인스턴스 획득."""
        if self._atomic_query is None:
            try:
                from baldur.services.regional_emergency.atomic_query import (
                    get_atomic_state_query,
                )

                self._atomic_query = get_atomic_state_query()
            except Exception:
                # Redis 미사용 환경에서는 None 유지
                pass
        return self._atomic_query

    def _get_audit_trail(self) -> Any:
        """EscalationAuditTrail 인스턴스 획득."""
        if self._audit_trail is None:
            from baldur.services.regional_emergency.escalation_audit import (
                get_escalation_audit_trail,
            )

            self._audit_trail = get_escalation_audit_trail()
        return self._audit_trail

    def _get_current_namespace(self) -> str:
        """
        현재 인스턴스의 네임스페이스 획득.

        ClusterIdentity.region을 참조하여 현재 리전 식별.
        """
        try:
            from baldur.core.cluster_identity import get_cluster_identity

            identity = get_cluster_identity()
            return identity.region or GLOBAL_NAMESPACE
        except Exception:
            return GLOBAL_NAMESPACE

    def _get_state_key(self, namespace: str) -> str:
        """
        네임스페이스용 Redis 키 생성.

        Args:
            namespace: 대상 네임스페이스

        Returns:
            Redis 키 (예: "baldur:seoul:governance:emergency_state")
        """
        if namespace == GLOBAL_NAMESPACE:
            return f"baldur:{self.STATE_KEY_PATTERN}"
        return f"baldur:{namespace}:{self.STATE_KEY_PATTERN}"

    # =========================================================================
    # State CRUD
    # =========================================================================

    def get_state(self, namespace: str | None = None) -> ScopedEmergencyState:
        """
        특정 네임스페이스의 상태 조회 (캐시 활용).

        Args:
            namespace: 대상 네임스페이스 (None이면 현재 인스턴스)

        Returns:
            ScopedEmergencyState (없으면 기본값)
        """
        ns = namespace or self._get_current_namespace()
        return self._load_state(ns)

    def set_state(
        self,
        state: ScopedEmergencyState,
        namespace: str | None = None,
    ) -> None:
        """
        상태 저장.

        Args:
            state: 저장할 상태
            namespace: 대상 네임스페이스 (None이면 state.namespace 사용)
        """
        ns = namespace or state.namespace
        self._save_state(ns, state)

    def get_effective_state(
        self,
        namespace: str | None = None,
        precedence: str | None = None,
    ) -> ScopedEmergencyState:
        """
        유효한 Emergency 상태 조회 (우선순위 적용).

        AtomicStateQuery를 사용하여 Global+Regional을 원자적으로 조회하고
        우선순위에 따라 유효한 상태를 결정합니다.

        우선순위:
        1. Admin Override (precedence >= ADMIN_OVERRIDE): 리전 우선
        2. Global STRICT: 전역 비상시 모든 리전 STRICT
        3. Regional: 로컬 상태

        Args:
            namespace: 조회할 네임스페이스 (None이면 현재 인스턴스)
            precedence: 명령 우선순위 ("AUTO", "ADMIN_OVERRIDE" 등)

        Returns:
            유효한 ScopedEmergencyState
        """
        ns = namespace or self._get_current_namespace()

        # AtomicStateQuery 사용 시도
        atomic_query = self._get_atomic_query()
        if atomic_query is not None:
            try:
                state_dict, decision_type, reason = atomic_query.query_effective_state(
                    namespace=ns,
                    precedence=precedence,
                )

                # Audit 기록 (중요 결정만)
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

        # 폴백: 수동 조회 (2회 Redis 호출)
        return self._get_effective_state_manual(ns, precedence)

    def _get_effective_state_manual(
        self,
        namespace: str,
        precedence: str | None = None,
    ) -> ScopedEmergencyState:
        """
        수동 유효 상태 조회 (AtomicStateQuery 폴백).

        Redis 2회 호출 (Global + Regional).
        """
        with self._lock:
            global_state = self._load_state(GLOBAL_NAMESPACE)
            regional_state = self._load_state(namespace)

            # 우선순위 확인 (ADMIN_OVERRIDE 이상이면 Regional 우선)
            if precedence in ("ADMIN_OVERRIDE", "KILL_SWITCH"):
                return regional_state

            # Safety-Max: 둘 중 더 엄격한 상태
            global_is_strict = (
                global_state.is_active() and global_state.governance_mode == "STRICT"
            )
            (regional_state.is_active() and regional_state.governance_mode == "STRICT")

            if global_is_strict:
                # Global STRICT가 Regional을 오버라이드
                return ScopedEmergencyState(
                    namespace=namespace,
                    emergency_level=global_state.emergency_level,
                    governance_mode="STRICT",
                    scope=EmergencyScope.GLOBAL,  # Global에서 왔음을 표시
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
        Emergency 모드 활성화.

        Args:
            level: Emergency 레벨 (LEVEL_1, LEVEL_2, LEVEL_3)
            activated_by: 활성화한 주체 (user ID 또는 "system")
            reason: 활성화 사유
            namespace: 대상 네임스페이스 (None이면 현재 인스턴스)
            scope: 적용 범위 (REGIONAL 또는 GLOBAL)
            expiry_hours: 만료 시간 (None이면 기본값 8시간)

        Returns:
            활성화된 ScopedEmergencyState
        """
        target_ns = namespace or self._get_current_namespace()

        # GLOBAL scope면 global 네임스페이스에 저장
        if scope == EmergencyScope.GLOBAL:
            target_ns = GLOBAL_NAMESPACE

        # 만료 시간 계산 (Settings에서 로드)
        hours = expiry_hours or _get_emergency_expiry_hours()
        expires_at = utc_now() + timedelta(hours=hours)

        # Governance 모드 결정 (LEVEL_2 이상이면 STRICT)
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
        Emergency 모드 비활성화.

        Args:
            deactivated_by: 비활성화한 주체
            namespace: 대상 네임스페이스 (None이면 현재 인스턴스)
            scope: 적용 범위

        Returns:
            비활성화된 ScopedEmergencyState
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
        활성화된 모든 네임스페이스 목록 조회.

        Returns:
            활성 Emergency 네임스페이스 목록
        """
        active = []
        backend = self._get_backend()

        try:
            # 패턴 매칭으로 모든 emergency_state 키 조회
            all_states = backend.get_all(f"*{self.STATE_KEY_PATTERN}")

            for key, data in all_states.items():
                if data and data.get("emergency_level", "normal") != "normal":
                    # 키에서 네임스페이스 추출
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
        캐시 무효화.

        Args:
            namespace: 무효화할 네임스페이스 (None이면 전체)
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
    """NamespacedEmergencyTracker 싱글톤 반환."""
    global _namespaced_tracker

    if _namespaced_tracker is None:
        with _tracker_lock:
            if _namespaced_tracker is None:
                _namespaced_tracker = NamespacedEmergencyTracker()

    return _namespaced_tracker


def reset_namespaced_emergency_tracker() -> None:
    """싱글톤 초기화 (테스트용)."""
    global _namespaced_tracker
    with _tracker_lock:
        if _namespaced_tracker is not None:
            _namespaced_tracker.close()
        _namespaced_tracker = None
