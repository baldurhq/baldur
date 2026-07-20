"""
Cell-scoped Circuit Breaker composite key management.

Namespaces a CB's service_name into the ``{base_name}::{cell_id}`` form so that
each Cell gets a physically separate CB instance.

Existing CBs keep the ``service_name=payment_api`` form: since
``parse_composite_cb_name()`` maps a key without the separator to
``("payment_api", "")``, legacy code is unaffected.
"""

from __future__ import annotations

COMPOSITE_KEY_SEPARATOR = "::"


def make_cell_scoped_cb_name(service_name: str, cell_id: str) -> str:
    """
    Build a cell-scoped CB composite key.

    Args:
        service_name: Base service name (e.g. ``"payment_api"``)
        cell_id: Cell identifier (e.g. ``"cell-3"``)

    Returns:
        Composite key (e.g. ``"payment_api::cell-3"``)
    """
    return f"{service_name}{COMPOSITE_KEY_SEPARATOR}{cell_id}"


def parse_composite_cb_name(composite_name: str) -> tuple[str, str]:
    """
    Split a composite key into ``(service_name, cell_id)``.

    Legacy compatibility: returns ``cell_id=""`` when the separator is absent.

    Args:
        composite_name: CB identifier

    Returns:
        ``(base_service_name, cell_id)``
    """
    if COMPOSITE_KEY_SEPARATOR in composite_name:
        parts = composite_name.split(COMPOSITE_KEY_SEPARATOR, 1)
        return parts[0], parts[1]
    return composite_name, ""  # Legacy single-key compatibility
