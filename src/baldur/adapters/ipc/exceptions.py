"""
Exception classes for IPC communication.

Defines the failure cases that arise in Unix domain socket and gRPC sidecar
communication.

Usage:
    from baldur.adapters.ipc.exceptions import (
        IPCError,
        IPCConnectionError,
        IPCTimeoutError,
        IPCAuthenticationError,
        IPCMethodNotFoundError,
        IPCInvalidParamsError,
    )

    try:
        client.should_allow("service_name")
    except IPCConnectionError:
        # Fail-open: allow when the connection fails
        return True
"""

from __future__ import annotations

from typing import Any

from baldur.core.exceptions import AdapterError


class IPCError(AdapterError):
    """IPC communication base exception."""

    def __init__(self, message: str, jsonrpc_code: int | None = None):
        """
        Initialize IPC exception.

        Args:
            message: Error message
            jsonrpc_code: JSON-RPC error code (optional)
        """
        super().__init__(message)
        self.message = message
        self.jsonrpc_code: int | None = jsonrpc_code

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        if self.jsonrpc_code is not None:
            ctx["jsonrpc_code"] = self.jsonrpc_code
        return ctx


class IPCConnectionError(IPCError):
    """Raised when an IPC connection fails."""

    def __init__(self, message: str = "Failed to connect to IPC server"):
        super().__init__(message, jsonrpc_code=-32003)


class IPCTimeoutError(IPCError):
    """Raised when an IPC request times out."""

    def __init__(
        self, message: str = "IPC request timed out", timeout: float | None = None
    ):
        super().__init__(message, jsonrpc_code=-32003)
        self.timeout = timeout


class IPCAuthenticationError(IPCError):
    """Raised when IPC authentication fails."""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, jsonrpc_code=-32001)


class IPCAuthorizationError(IPCError):
    """Raised when the IPC caller lacks permission."""

    def __init__(self, message: str = "Authorization denied"):
        super().__init__(message, jsonrpc_code=-32002)


class IPCMethodNotFoundError(IPCError):
    """Raised when the IPC method cannot be found."""

    def __init__(self, method: str):
        message = f"Method not found: {method}"
        super().__init__(message, jsonrpc_code=-32601)
        self.method = method


class IPCInvalidParamsError(IPCError):
    """Raised when IPC parameters are invalid."""

    def __init__(self, message: str = "Invalid params", param_name: str | None = None):
        super().__init__(message, jsonrpc_code=-32602)
        self.param_name = param_name


class IPCParseError(IPCError):
    """Raised when an IPC message cannot be parsed."""

    def __init__(self, message: str = "Failed to parse message"):
        super().__init__(message, jsonrpc_code=-32700)


class IPCInternalError(IPCError):
    """Raised on an internal IPC server error."""

    def __init__(
        self, message: str = "Internal server error", cause: Exception | None = None
    ):
        super().__init__(message, jsonrpc_code=-32603)
        self.cause = cause


class IPCRateLimitedError(IPCError):
    """Raised when the IPC request rate limit is exceeded."""

    def __init__(
        self, message: str = "Rate limit exceeded", retry_after: float | None = None
    ):
        super().__init__(message, jsonrpc_code=-32004)
        self.retry_after = retry_after


class IPCCircuitBreakerOpenError(IPCError):
    """Raised when the IPC circuit breaker is open."""

    def __init__(self, service_name: str, message: str | None = None):
        msg = message or f"Circuit breaker is open for service: {service_name}"
        super().__init__(msg, jsonrpc_code=-32005)
        self.service_name = service_name


class IPCServiceUnavailableError(IPCError):
    """Raised when the IPC service is unavailable."""

    def __init__(self, service_name: str | None = None, message: str | None = None):
        msg = message or "Service unavailable"
        if service_name:
            msg = f"Service unavailable: {service_name}"
        super().__init__(msg, jsonrpc_code=-32003)
        self.service_name = service_name
