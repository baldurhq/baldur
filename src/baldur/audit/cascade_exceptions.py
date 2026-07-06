"""
Cascade Audit 예외 클래스.

Cascade Event 처리 중 발생할 수 있는 예외들을 정의합니다.

Exception Hierarchy:
    CascadeAuditError (base)
    ├── CascadeChainDepthExceeded - 체인 깊이 초과
    └── CascadeCycleDetected - 순환 참조 감지

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
"""

from __future__ import annotations

from baldur.core.exceptions import AuditError


class CascadeAuditError(AuditError):
    """Cascade Audit base exception."""

    pass


class CascadeChainDepthExceeded(CascadeAuditError):
    """
    체인 깊이 초과 예외.

    Cascade 체인이 설정된 최대 깊이를 초과했을 때 발생합니다.
    이는 보통 자동화 시스템 간의 과도한 연쇄 반응을 의미합니다.

    Attributes:
        depth: 현재 체인 깊이
        max_depth: 최대 허용 깊이
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
        """예외 정보를 딕셔너리로 변환."""
        return {
            "error_type": "CascadeChainDepthExceeded",
            "depth": self.depth,
            "max_depth": self.max_depth,
            "cascade_id": self.cascade_id,
            "message": str(self),
        }


class CascadeCycleDetected(CascadeAuditError):
    """
    순환 참조 감지 예외.

    Cascade 체인에서 순환 참조(A → B → A)가 감지되었을 때 발생합니다.
    이는 자동화 시스템 간의 무한 루프를 의미합니다.

    Attributes:
        cycle_path: 순환 경로 (이벤트 ID 목록)
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
        """예외 정보를 딕셔너리로 변환."""
        return {
            "error_type": "CascadeCycleDetected",
            "cycle_path": self.cycle_path,
            "cascade_id": self.cascade_id,
            "message": str(self),
        }


class CascadeEventNotFound(CascadeAuditError):
    """
    Cascade Event 미발견 예외.

    요청한 Cascade Event를 찾을 수 없을 때 발생합니다.
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
    Cascade 무결성 오류 예외.

    해시 체인 무결성 검증에 실패했을 때 발생합니다.
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
        """예외 정보를 딕셔너리로 변환."""
        return {
            "error_type": "CascadeIntegrityError",
            "cascade_id": self.cascade_id,
            "integrity_error_type": self.error_type,
            "details": self.details,
            "message": str(self),
        }
