"""
Cascade Audit exception classes.

Defines the exceptions that can be raised while processing Cascade Events.

Exception Hierarchy:
    CascadeAuditError (base)
    ├── CascadeChainDepthExceeded - chain depth exceeded
    └── CascadeCycleDetected - cycle detected
"""

from __future__ import annotations

from baldur.core.exceptions import AuditError


class CascadeAuditError(AuditError):
    """Cascade Audit base exception."""

    pass


class CascadeChainDepthExceeded(CascadeAuditError):
    """
    Chain depth exceeded exception.

    Raised when a Cascade chain exceeds the configured maximum depth.
    This usually indicates an excessive chain reaction between automated
    systems.

    Attributes:
        depth: Current chain depth
        max_depth: Maximum allowed depth
        cascade_id: Cascade Event ID
    """

    def __init__(
        self,
        depth: int,
        max_depth: int,
        cascade_id: str,
        message: str | None = None,
    ):
        self.depth = depth
        self.max_depth = max_depth
        self.cascade_id = cascade_id

        default_message = (
            f"Cascade chain depth {depth} exceeds max {max_depth} "
            f"for cascade {cascade_id}"
        )
        super().__init__(message or default_message)

    def extra_context(self) -> dict:
        ctx = super().extra_context()
        ctx["depth"] = self.depth
        ctx["max_depth"] = self.max_depth
        ctx["cascade_id"] = self.cascade_id
        return ctx

    def to_dict(self) -> dict:
        """Convert the exception details to a dictionary."""
        return {
            "error_type": "CascadeChainDepthExceeded",
            "depth": self.depth,
            "max_depth": self.max_depth,
            "cascade_id": self.cascade_id,
            "message": str(self),
        }


class CascadeCycleDetected(CascadeAuditError):
    """
    Cycle detected exception.

    Raised when a cycle (A → B → A) is detected in a Cascade chain.
    This indicates an infinite loop between automated systems.

    Attributes:
        cycle_path: Cycle path (list of event IDs)
        cascade_id: Cascade Event ID
    """

    def __init__(
        self,
        cycle_path: list[str],
        cascade_id: str,
        message: str | None = None,
    ):
        self.cycle_path = cycle_path
        self.cascade_id = cascade_id

        default_message = (
            f"Cascade cycle detected: {' -> '.join(cycle_path)} in cascade {cascade_id}"
        )
        super().__init__(message or default_message)

    def extra_context(self) -> dict:
        ctx = super().extra_context()
        ctx["cycle_path"] = self.cycle_path
        ctx["cascade_id"] = self.cascade_id
        return ctx

    def to_dict(self) -> dict:
        """Convert the exception details to a dictionary."""
        return {
            "error_type": "CascadeCycleDetected",
            "cycle_path": self.cycle_path,
            "cascade_id": self.cascade_id,
            "message": str(self),
        }


class CascadeEventNotFound(CascadeAuditError):
    """
    Cascade Event not found exception.

    Raised when the requested Cascade Event cannot be found.
    """

    def __init__(
        self,
        cascade_id: str,
        namespace: str,
        message: str | None = None,
    ):
        self.cascade_id = cascade_id
        self.namespace = namespace

        default_message = (
            f"Cascade event '{cascade_id}' not found in namespace '{namespace}'"
        )
        super().__init__(message or default_message)

    def extra_context(self) -> dict:
        ctx = super().extra_context()
        ctx["cascade_id"] = self.cascade_id
        ctx["namespace"] = self.namespace
        return ctx


class CascadeIntegrityError(CascadeAuditError):
    """
    Cascade integrity error exception.

    Raised when hash chain integrity verification fails.
    """

    def __init__(
        self,
        cascade_id: str,
        error_type: str,
        details: dict | None = None,
        message: str | None = None,
    ):
        self.cascade_id = cascade_id
        self.error_type = error_type
        self.details = details or {}

        default_message = f"Cascade integrity error for '{cascade_id}': {error_type}"
        super().__init__(message or default_message)

    def extra_context(self) -> dict:
        ctx = super().extra_context()
        ctx["cascade_id"] = self.cascade_id
        ctx["integrity_error_type"] = self.error_type
        ctx["details"] = self.details
        return ctx

    def to_dict(self) -> dict:
        """Convert the exception details to a dictionary."""
        return {
            "error_type": "CascadeIntegrityError",
            "cascade_id": self.cascade_id,
            "integrity_error_type": self.error_type,
            "details": self.details,
            "message": str(self),
        }
