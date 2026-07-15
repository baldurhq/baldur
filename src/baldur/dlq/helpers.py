"""OSS-side DLQ store + postmortem helpers — stable import target.

Provides a single, stable import target for OSS callsites that need to store
operations in the DLQ, compress DLQ entries, or record postmortem incidents.

``store_to_dlq`` / ``dlq_backing_available`` resolve the DLQ capture backing
through one chain — the PRO ``DLQService`` (registered under ACTIVE entitlement)
when present, otherwise the OSS ``DLQCaptureService``. So a pure ``pip install
baldur`` install captures failures into the DLQ store (no ``baldur_pro``
required); the backing always resolves on a functional install.

``compress_entries`` and the ``postmortem.store`` helpers stay PRO-only:
compression and postmortem incident storage remain PRO-tier, so each wrapper
delegates to the corresponding PRO function when installed and no-ops otherwise
(``None`` for writers, ``[]`` / ``0`` for read helpers).

Test isolation
--------------
Tests that swap PRO presence MUST reset the compression / postmortem module
caches + the OSS capture singleton via the ``reset_dlq_helpers`` fixture in
``tests/conftest.py``. The DLQ-store backing resolves through the provider
registry (not a module import cache), so "PRO absent" is simulated by leaving
the ``dlq_service`` slot empty — no import games.
"""

from __future__ import annotations

from typing import Any

_pro_dlq_compression: Any = None
_pro_postmortem_store: Any = None
_resolved_dlq_compression: bool = False
_resolved_postmortem_store: bool = False


def _get_pro_dlq_compression() -> Any:
    """Return cached :mod:`baldur_pro.services.dlq.compression` or ``None``."""
    global _pro_dlq_compression, _resolved_dlq_compression
    if not _resolved_dlq_compression:
        try:
            import baldur_pro.services.dlq.compression as _m

            _pro_dlq_compression = _m
        except ImportError:
            _pro_dlq_compression = None
        _resolved_dlq_compression = True
    return _pro_dlq_compression


def _get_pro_postmortem_store() -> Any:
    """Return cached :mod:`baldur_pro.services.postmortem.store` or ``None``."""
    global _pro_postmortem_store, _resolved_postmortem_store
    if not _resolved_postmortem_store:
        try:
            import baldur_pro.services.postmortem.store as _m

            _pro_postmortem_store = _m
        except ImportError:
            _pro_postmortem_store = None
        _resolved_postmortem_store = True
    return _pro_postmortem_store


# ============================================================
# DLQ store + compression
# ============================================================


def store_to_dlq(*args: Any, **kwargs: Any) -> Any:
    """Store a failure in the DLQ via the resolved capture backing.

    Signature: ``store_to_dlq(domain, failure_type, ..., request=None,
    mode=None) -> DLQEntryResult``. Resolves the PRO ``DLQService`` (ACTIVE
    entitlement) when present, otherwise the OSS ``DLQCaptureService``.
    """
    from baldur.services.dlq_capture import resolve_dlq_backing

    return resolve_dlq_backing().store_failure(*args, **kwargs)


def dlq_backing_available() -> bool:
    """Return ``True`` iff a real DLQ store backs :func:`store_to_dlq`.

    Resolves the same chain :func:`store_to_dlq` uses, so callers asking "will a
    ``dlq=True`` failure actually persist?" get the same verdict the store
    would. The OSS ``DLQCaptureService`` always resolves on a functional install
    (construction is I/O-free), so this returns ``True`` whenever the DLQ store
    can be reached — ``False`` only if resolution itself raises (a broken
    install). Truthful, documented probe; kept in lockstep with the store path.
    """
    from baldur.services.dlq_capture import resolve_dlq_backing

    try:
        return resolve_dlq_backing() is not None
    except Exception:
        return False


def compress_entries(*args: Any, **kwargs: Any) -> Any | None:
    """PRO: compress_entries(entries) -> CompressResult."""
    if (p := _get_pro_dlq_compression()) is None:
        return None
    return p.compress_entries(*args, **kwargs)


# ============================================================
# Postmortem incident store
# ============================================================


def add_healing_incident(*args: Any, **kwargs: Any) -> None:
    """PRO: add_healing_incident(incident)."""
    if (p := _get_pro_postmortem_store()) is None:
        return None
    return p.add_healing_incident(*args, **kwargs)


def get_healing_incidents(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """PRO: get_healing_incidents(limit=10, start_date=None, end_date=None, service=None, min_duration=None, offset=0, use_db=True) -> list[dict]."""
    if (p := _get_pro_postmortem_store()) is None:
        return []
    return p.get_healing_incidents(*args, **kwargs)


def get_healing_incidents_count(*args: Any, **kwargs: Any) -> int:
    """PRO: get_healing_incidents_count(start_date=None, end_date=None, service=None, min_duration=None, use_db=True) -> int."""
    if (p := _get_pro_postmortem_store()) is None:
        return 0
    return p.get_healing_incidents_count(*args, **kwargs)


__all__ = [
    "add_healing_incident",
    "compress_entries",
    "dlq_backing_available",
    "get_healing_incidents",
    "get_healing_incidents_count",
    "store_to_dlq",
]
