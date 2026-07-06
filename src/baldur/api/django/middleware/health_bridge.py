"""
Health Bridge Middleware (Stage 50: Worker Saturation 방지)

DB-independent health endpoint를 제공하여 DB 장애 시에도
CircuitBreaker 상태를 외부에서 관찰할 수 있도록 합니다.

Usage in settings.py:
    MIDDLEWARE = [
        "baldur.api.django.middleware.HealthBridgeMiddleware",  # 최상단!
        "django.middleware.security.SecurityMiddleware",
        ...
    ]
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from _thread import LockType

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = structlog.get_logger()


class HealthBridgeMiddleware:
    """
    DB-independent Health Endpoint Middleware.

    문제 상황:
    - DB 죽음 → 모든 Django Worker가 DB 연결 대기
    - /health/l3도 Worker를 사용하므로 타임아웃
    - CB 상태를 외부에서 관찰 불가

    해결책:
    - Middleware에서 DB 엔진 로드 전에 즉시 반환
    - CB 스냅샷을 메모리에 저장 (매 요청마다 갱신)
    - Prometheus 메트릭은 기존 인프라 활용

    CRITICAL: 이 Middleware는 MIDDLEWARE 리스트 최상단에 위치해야 함!
    """

    # 클래스 변수: CB 스냅샷 저장 (모든 인스턴스가 공유)
    _cb_snapshot: dict[str, Any] = {
        "states": {},
        "last_updated": None,
        "update_count": 0,
    }
    # ``threading.Lock`` returns an opaque LockType — initialised in __init__.
    _snapshot_lock: ClassVar[LockType | None] = None

    # Health Bridge 대상 경로
    BRIDGE_PATHS = [
        "/api/baldur/health/l3/",
        "/api/baldur/health/bridge/",
    ]

    def __init__(self, get_response: Callable):
        """Initialize middleware."""
        self.get_response = get_response

        # Lazy init lock (import threading here to avoid circular import)
        import threading

        if HealthBridgeMiddleware._snapshot_lock is None:
            HealthBridgeMiddleware._snapshot_lock = threading.Lock()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Process request/response."""

        # === Early Return for Bridge Paths ===
        if request.path in self.BRIDGE_PATHS:
            return self._serve_bridge_response(request)

        # === Normal Request Processing ===
        response: HttpResponse = self.get_response(request)

        # === Update CB Snapshot (best-effort) ===
        # Non-blocking: 실패해도 요청은 정상 처리
        self._try_update_snapshot()

        return response

    def _serve_bridge_response(self, request: HttpRequest) -> HttpResponse:
        """
        Serve health bridge response without touching DB.

        Returns CB snapshot from memory - instant response even during DB blackout.

        While the GracefulShutdownCoordinator is in the DRAINING phase, this
        endpoint returns 503 + ``Retry-After`` + ``Connection: close`` and a
        ``status: draining`` payload so k8s readiness probes flip the pod's
        endpoint slice to NotReady — the LB stops routing new connections
        immediately. Liveness probes hit different paths
        (``/api/baldur/health/live/``, ``/api/baldur/health/ping/``) and stay
        200 throughout drain (drain is a normal lifecycle phase, not a
        liveness failure).
        """
        from django.http import JsonResponse

        snapshot = self._get_snapshot()

        drain_state = self._get_drain_state()
        if drain_state is not None:
            response_data = {
                "status": "draining",
                "timestamp": utc_now().isoformat(),
                "circuit_breakers": snapshot.get("states", {}),
                "snapshot": {
                    "last_updated": snapshot.get("last_updated"),
                    "update_count": snapshot.get("update_count", 0),
                    "age_seconds": self._calculate_snapshot_age(snapshot),
                },
                "note": "DB-independent health endpoint (Stage 50)",
                "shutdown": drain_state,
            }
            response = JsonResponse(response_data, status=503)
            response["Retry-After"] = str(drain_state["retry_after_seconds"])
            response["Connection"] = "close"
            return response

        response_data = {
            "status": "bridge_active",
            "timestamp": utc_now().isoformat(),
            "circuit_breakers": snapshot.get("states", {}),
            "snapshot": {
                "last_updated": snapshot.get("last_updated"),
                "update_count": snapshot.get("update_count", 0),
                "age_seconds": self._calculate_snapshot_age(snapshot),
            },
            "note": "DB-independent health endpoint (Stage 50)",
        }

        return JsonResponse(response_data)

    @staticmethod
    def _get_drain_state() -> dict[str, Any] | None:
        """Return drain metadata when the coordinator is DRAINING, else None.

        Lookup is best-effort — any failure (coordinator unavailable, settings
        missing) returns None and the caller falls through to the normal
        ``bridge_active`` response. The bridge endpoint is the only readiness
        signal during DB blackout, so a coordinator probe failure must never
        flip it to 503.
        """
        try:
            from baldur.core.shutdown_coordinator import (
                ShutdownPhase,
                get_shutdown_coordinator,
            )

            coordinator = get_shutdown_coordinator()
            if coordinator.phase != ShutdownPhase.DRAINING:
                return None

            from baldur.settings.recovery_shutdown import (
                get_recovery_shutdown_settings,
            )

            stats = coordinator.get_stats()
            remaining = stats.remaining_drain_time
            if remaining is None:
                remaining = (
                    get_recovery_shutdown_settings().drain_default_retry_after_seconds
                )
            retry_after = max(1, int(remaining))
            return {
                "phase": coordinator.phase.value,
                "retry_after_seconds": retry_after,
                "in_flight_count": stats.in_flight_count,
            }
        except Exception as exc:
            logger.warning("health_bridge.drain_probe_failed", error=exc)
            return None

    def _try_update_snapshot(self) -> None:
        """
        Try to update CB snapshot from in-memory state.

        Non-blocking: If CB service is unavailable, skip silently.
        """
        try:
            # Import here to avoid circular imports and keep DB-independence
            from baldur.services.circuit_breaker.convenience import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            if cb_service is None:
                return

            # get_all_states returns list of dicts from repository
            all_states = cb_service.get_all_states()

            states = {}
            for s in all_states:
                states[s["service_name"]] = {
                    "state": s["state"],
                    "failure_count": s.get("failure_count", 0),
                    "success_count": s.get("success_count", 0),
                    "last_failure_at": s.get("last_failure_at"),
                    "manually_controlled": s.get("manually_controlled", False),
                }

            assert self._snapshot_lock is not None  # initialised in __init__
            with self._snapshot_lock:
                HealthBridgeMiddleware._cb_snapshot = {
                    "states": states,
                    "last_updated": utc_now().isoformat(),
                    "update_count": self._cb_snapshot.get("update_count", 0) + 1,
                }

        except Exception as e:
            # Log at warning level temporarily for debugging
            logger.warning(
                "cb.snapshot_update_failed",
                error=e,
            )

    def _get_snapshot(self) -> dict[str, Any]:
        """Thread-safe snapshot read."""
        assert self._snapshot_lock is not None  # initialised in __init__
        with self._snapshot_lock:
            return dict(self._cb_snapshot)

    def _calculate_snapshot_age(self, snapshot: dict[str, Any]) -> float | None:
        """Calculate age of snapshot in seconds."""
        last_updated = snapshot.get("last_updated")
        if not last_updated:
            return None

        try:
            last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            now = utc_now()
            return round((now - last_dt).total_seconds(), 2)
        except Exception:
            return None
