"""
IPC 어댑터 — Library-mode 컴포넌트.

Dual-use components retained after Sidecar → Library transition:
    - CBStateCache: TTL 기반 CB 상태 캐시 (Redis hit 감소)
    - CBStateSnapshot: Shared Memory 기반 CB 상태 공유 (sub-10μs)
    - RequestHandler: 서비스 라우팅 추상화
    - IPC exceptions: 에러 코드 체계

Sidecar-only 코드 (UDS/gRPC 서버, auth, metrics, probe, protocol)는
316 Gunicorn Preload Optimization에서 제거 대상으로 분류됨.

Usage:
    from baldur.adapters.ipc import (
        CBStateSnapshot,
        get_cb_state_snapshot,
        reset_cb_state_snapshot,
    )

    snapshot = get_cb_state_snapshot()
    state = snapshot.get_state("payment_service")
"""

from baldur.adapters.ipc.cb_state_cache import (
    CBStateCache,
    IPCStateCache,
    get_cb_state_cache,
    reset_cb_state_cache,
)
from baldur.adapters.ipc.cb_state_snapshot import (
    CBStateSnapshot,
    configure_cb_state_snapshot,
    get_cb_state_snapshot,
    reset_cb_state_snapshot,
)
from baldur.adapters.ipc.exceptions import (
    IPCAuthenticationError,
    IPCAuthorizationError,
    IPCCircuitBreakerOpenError,
    IPCConnectionError,
    IPCError,
    IPCInternalError,
    IPCInvalidParamsError,
    IPCMethodNotFoundError,
    IPCParseError,
    IPCRateLimitedError,
    IPCServiceUnavailableError,
    IPCTimeoutError,
)
from baldur.adapters.ipc.request_handler import (
    RequestHandler,
    get_request_handler,
    reset_request_handler,
)

__all__ = [
    # Cache
    "IPCStateCache",
    "CBStateCache",
    "get_cb_state_cache",
    "reset_cb_state_cache",
    # CB State Snapshot
    "CBStateSnapshot",
    "configure_cb_state_snapshot",
    "get_cb_state_snapshot",
    "reset_cb_state_snapshot",
    # Request Handler
    "RequestHandler",
    "get_request_handler",
    "reset_request_handler",
    # Exceptions
    "IPCError",
    "IPCConnectionError",
    "IPCTimeoutError",
    "IPCAuthenticationError",
    "IPCAuthorizationError",
    "IPCMethodNotFoundError",
    "IPCInvalidParamsError",
    "IPCParseError",
    "IPCInternalError",
    "IPCRateLimitedError",
    "IPCCircuitBreakerOpenError",
    "IPCServiceUnavailableError",
]
