"""
Replay Handlers - Domain-specific replay handler registry.

Provides the ReplayHandler ABC, DefaultReplayHandler, and the handler registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .models import ReplayResult

if TYPE_CHECKING:
    from baldur.interfaces.repositories import FailedOperationData


# =============================================================================
# Replay Handlers (Domain-specific)
# =============================================================================


class ReplayHandler(ABC):
    """
    Abstract base class for domain-specific replay handlers.

    Each domain (payment, point, inventory, etc.) should implement
    its own replay logic by subclassing this.

    Note: Handlers should work with FailedOperationData (a simple dataclass)
    not Django models. The handler receives operation data and should
    use injected services for actual operations.
    """

    @property
    @abstractmethod
    def domain(self) -> str:
        """Return the domain this handler handles."""
        pass

    @abstractmethod
    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        """
        Execute replay for a single failed operation.

        Args:
            failed_op: The FailedOperationData to replay

        Returns:
            ReplayResult indicating success or failure
        """
        pass

    @abstractmethod
    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        """
        Check if the operation can be replayed.

        Args:
            failed_op: The FailedOperationData to check

        Returns:
            Tuple of (can_replay: bool, reason: str)
        """
        pass


class DefaultReplayHandler(ReplayHandler):
    """
    Default replay handler that returns an error.

    This handler is used when no specific handler is registered for a domain.
    Users should register their own handlers for each domain they need.
    """

    def __init__(self, domain_name: str):
        self._domain = domain_name

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return False, f"No replay handler registered for domain '{self._domain}'"

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        return ReplayResult.failed(
            failed_op.id,
            f"No replay handler registered for domain '{self._domain}'. "
            "Please register a handler using register_replay_handler().",
        )


# =============================================================================
# Domain-Specific Handlers (MOVED TO ADAPTER LAYER)
# =============================================================================
# NOTE: Domain-specific handlers have been moved to the adapter layer.
#
# For Django projects, create your own handlers:
#   from myapp.services.baldur.replay_handlers import (
#       OrderReplayHandler,
#       NotificationReplayHandler,
#   )
#
# Or register your own handlers:
#   from baldur.services.replay_service import register_replay_handler, ReplayHandler
#
#   class MyDomainHandler(ReplayHandler):
#       @property
#       def domain(self) -> str:
#           return "my_domain"
#
#       def can_replay(self, failed_op) -> tuple[bool, str]:
#           # Your validation logic
#           return True, ""
#
#       def replay(self, failed_op) -> ReplayResult:
#           # Your replay logic
#           return ReplayResult.succeeded(failed_op.id, "Done")
#
#   register_replay_handler(MyDomainHandler())
# =============================================================================


# =============================================================================
# Replay Handler Registry
# =============================================================================


_replay_handlers: dict[str, ReplayHandler] = {}


def register_replay_handler(handler: ReplayHandler) -> None:
    """Register a replay handler for a domain."""
    _replay_handlers[handler.domain] = handler


def get_replay_handler(domain: str) -> ReplayHandler:
    """
    Get the replay handler for a domain.

    Args:
        domain: The domain name

    Returns:
        ReplayHandler instance for the domain
    """
    if domain in _replay_handlers:
        return _replay_handlers[domain]

    # Return default handler if no specific handler exists
    return DefaultReplayHandler(domain)


# =============================================================================
# Replay Safety Gates (#502 D7)
# =============================================================================


def _truncate_gate(failed_op: FailedOperationData) -> tuple[bool, str]:
    """Decide whether a DLQ entry with truncated request_data may replay.

    Returns ``(allowed, reason)``. When ``request_data`` was capped by the
    write-side forensic size limit (#502 D7) the original payload is gone,
    so silent replay would corrupt downstream state — this gate blocks
    replay by default. Operators that handle ``_truncated`` themselves can
    opt out via ``BALDUR_DLQ_TRUNCATE_BLOCKS_REPLAY=False``.

    Pure function: customer ``ReplayHandler.can_replay`` ABC contract is
    untouched; this is a framework-side gate invoked at the two replay
    sites (``replay_service/service.py`` and the baldur_pro replay
    operations mixin).
    """
    request_data = failed_op.request_data or {}
    if not isinstance(request_data, dict) or not request_data.get("_truncated"):
        return True, ""

    try:
        from baldur.settings.dlq import get_dlq_settings

        if not get_dlq_settings().truncate_blocks_replay:
            return True, ""
    except Exception:
        pass  # Default-deny on settings failure (conservative).

    return False, "request_data_truncated"


# NOTE: No default handlers registered in core package.
# Domain-specific handlers should be registered by the adapter layer.
# Example (in your adapter):
#   from baldur.services.replay_service import register_replay_handler
#   from myapp.services.replay_handlers import OrderReplayHandler
#   register_replay_handler(OrderReplayHandler())
