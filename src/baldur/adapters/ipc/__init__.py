"""
IPC adapters — library-mode components.

Dual-use components retained after Sidecar → Library transition:
    - CBStateCache: TTL-based CB state cache (fewer Redis hits)
    - CBStateSnapshot: shared-memory CB state sharing (sub-10μs)
    - RequestHandler: service routing abstraction
    - IPC exceptions: error code scheme

Sidecar-only code (UDS/gRPC server, auth, metrics, probe, protocol) was
marked for removal by 316 Gunicorn Preload Optimization.

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
