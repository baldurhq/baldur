"""
IPC 통신 관련 예외 클래스.

Unix Domain Socket 및 gRPC 사이드카 통신에서 발생하는
예외 상황을 정의합니다.

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
        # Fail-open: 연결 실패 시 허용
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
    """IPC 연결 실패 예외."""

    def __init__(self, message: str = "Failed to connect to IPC server"):
        super().__init__(message, jsonrpc_code=-32003)


class IPCTimeoutError(IPCError):
    """IPC 요청 타임아웃 예외."""

    def __init__(
        self, message: str = "IPC request timed out", timeout: float | None = None
    ):
        super().__init__(message, jsonrpc_code=-32003)
        self.timeout = timeout


class IPCAuthenticationError(IPCError):
    """IPC 인증 실패 예외."""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, jsonrpc_code=-32001)


class IPCAuthorizationError(IPCError):
    """IPC 권한 부족 예외."""

    def __init__(self, message: str = "Authorization denied"):
        super().__init__(message, jsonrpc_code=-32002)


class IPCMethodNotFoundError(IPCError):
    """IPC 메서드를 찾을 수 없음."""

    def __init__(self, method: str):
        message = f"Method not found: {method}"
        super().__init__(message, jsonrpc_code=-32601)
        self.method = method


class IPCInvalidParamsError(IPCError):
    """IPC 잘못된 파라미터."""

    def __init__(self, message: str = "Invalid params", param_name: str | None = None):
        super().__init__(message, jsonrpc_code=-32602)
        self.param_name = param_name


class IPCParseError(IPCError):
    """IPC 메시지 파싱 실패."""

    def __init__(self, message: str = "Failed to parse message"):
        super().__init__(message, jsonrpc_code=-32700)


class IPCInternalError(IPCError):
    """IPC 내부 서버 오류."""

    def __init__(
        self, message: str = "Internal server error", cause: Exception | None = None
    ):
        super().__init__(message, jsonrpc_code=-32603)
        self.cause = cause


class IPCRateLimitedError(IPCError):
    """IPC 요청 제한 초과."""

    def __init__(
        self, message: str = "Rate limit exceeded", retry_after: float | None = None
    ):
        super().__init__(message, jsonrpc_code=-32004)
        self.retry_after = retry_after


class IPCCircuitBreakerOpenError(IPCError):
    """IPC 서킷 브레이커 열림 상태."""

    def __init__(self, service_name: str, message: str | None = None):
        msg = message or f"Circuit breaker is open for service: {service_name}"
        super().__init__(msg, jsonrpc_code=-32005)
        self.service_name = service_name


class IPCServiceUnavailableError(IPCError):
    """IPC 서비스 이용 불가."""

    def __init__(self, service_name: str | None = None, message: str | None = None):
        msg = message or "Service unavailable"
        if service_name:
            msg = f"Service unavailable: {service_name}"
        super().__init__(msg, jsonrpc_code=-32003)
        self.service_name = service_name
