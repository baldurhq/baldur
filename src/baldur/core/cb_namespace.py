"""
Cell-scoped Circuit Breaker Composite Key 관리.

CB의 service_name을 ``{base_name}::{cell_id}`` 형태로 네임스페이스화하여
Cell별로 물리적으로 분리된 CB 인스턴스를 생성한다.

기존 CB는 ``service_name=payment_api`` 형태로 유지되며,
``parse_composite_cb_name()`` 이 구분자 없는 키를 ``("payment_api", "")`` 로
처리하므로 레거시 코드에 영향 없음.
"""

from __future__ import annotations

COMPOSITE_KEY_SEPARATOR = "::"


def make_cell_scoped_cb_name(service_name: str, cell_id: str) -> str:
    """
    Cell-scoped CB Composite Key 생성.

    Args:
        service_name: 기본 서비스 이름 (예: ``"payment_api"``)
        cell_id: Cell 식별자 (예: ``"cell-3"``)

    Returns:
        Composite Key (예: ``"payment_api::cell-3"``)
    """
    return f"{service_name}{COMPOSITE_KEY_SEPARATOR}{cell_id}"


def parse_composite_cb_name(composite_name: str) -> tuple[str, str]:
    """
    Composite Key에서 ``(service_name, cell_id)`` 분리.

    레거시 호환: 구분자가 없으면 ``cell_id=""`` 반환.

    Args:
        composite_name: CB 식별자

    Returns:
        ``(base_service_name, cell_id)``
    """
    if COMPOSITE_KEY_SEPARATOR in composite_name:
        parts = composite_name.split(COMPOSITE_KEY_SEPARATOR, 1)
        return parts[0], parts[1]
    return composite_name, ""  # 레거시 단일 키 호환
