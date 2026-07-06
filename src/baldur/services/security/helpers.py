"""
Security Violation Helper Functions.

Module-level helper functions for accessing security violation services.
"""

from __future__ import annotations

from typing import Any

from baldur.services.security.models import SecurityViolationResult
from baldur.services.security.service import SecurityViolationService
from baldur.services.security.types import ViolationType
from baldur.utils.singleton import make_singleton_factory

(
    get_security_violation_service,
    configure_security_violation_service,
    reset_security_violation_service,
) = make_singleton_factory("security_violation_service", SecurityViolationService)


def handle_security_violation(
    violation_type: str | ViolationType,
    request_info: dict[str, Any] | None = None,
    user_id: int | None = None,
    description: str = "",
    **kwargs: Any,
) -> SecurityViolationResult:
    """
    Convenience function to handle a security violation.

    This is the main entry point for security violation handling.

    Args:
        violation_type: Type of security violation
        request_info: Request info dict with 'ip', 'user_agent' keys
        user_id: Associated user ID
        description: Description of the violation
        **kwargs: Additional arguments passed to handle_violation

    Returns:
        SecurityViolationResult
    """
    service = get_security_violation_service()
    return service.handle_violation(
        violation_type=violation_type,
        request_info=request_info,
        user_id=user_id,
        description=description,
        **kwargs,
    )
