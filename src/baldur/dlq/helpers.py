"""OSS-side thin wrappers for PRO DLQ + postmortem store helpers (518 D4).

Provides a single, stable import target for OSS callsites that need to store
operations in the DLQ, compress DLQ entries, or record postmortem incidents.
When PRO modules are installed, each wrapper delegates to the corresponding
PRO function. When PRO is not installed, each wrapper silently no-ops and
returns ``None`` (or an empty result for read helpers).

Each wrapper accepts ``*args, **kwargs`` and forwards them verbatim — the
caller's exact argument shape is preserved into the PRO call. Consult
``src/baldur_pro/services/dlq/`` and ``src/baldur_pro/services/postmortem/``
for parameter types and defaults.

Scope (batch a)
---------------
Function-style helpers only. Singleton getters (``get_dlq_service``,
``get_dlq_repository``, etc.) belong to the (c) singletons batch; the
``DLQService`` class import for typing belongs to the (d) types batch.

Re-folded from the dissolved (e) repositories batch: the three
``postmortem.store`` functions (``add_healing_incident``,
``get_healing_incidents``, ``get_healing_incidents_count``) ship here
because they are function-style callsites, not repository abstractions.

Test isolation
--------------
Tests that swap PRO presence or pop the relevant PRO modules from
``sys.modules`` MUST reset the module-level cache via the
``reset_dlq_helpers`` fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

from typing import Any

_pro_dlq: Any = None
_pro_dlq_compression: Any = None
_pro_postmortem_store: Any = None
_resolved_dlq: bool = False
_resolved_dlq_compression: bool = False
_resolved_postmortem_store: bool = False


def _get_pro_dlq() -> Any:
    """Return cached :mod:`baldur_pro.services.dlq` or ``None``."""
    global _pro_dlq, _resolved_dlq
    if not _resolved_dlq:
        try:
            import baldur_pro.services.dlq as _m

            _pro_dlq = _m
        except ImportError:
            _pro_dlq = None
        _resolved_dlq = True
    return _pro_dlq


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


def store_to_dlq(*args: Any, **kwargs: Any) -> Any | None:
    """PRO: store_to_dlq(domain, failure_type, ..., request=None, mode=None) -> DLQEntryResult."""
    if (p := _get_pro_dlq()) is None:
        return None
    return p.store_to_dlq(*args, **kwargs)


def dlq_backing_available() -> bool:
    """Return ``True`` iff a real DLQ store backs :func:`store_to_dlq`.

    Reuses the exact :func:`_get_pro_dlq` resolution that the store path itself
    consults, so callers asking "will a ``dlq=True`` failure actually persist?"
    get the same verdict the store would. ``False`` means the store silently
    no-ops (the caller still sees the failure raised, but nothing is durably
    captured for replay). The single source of truth for DLQ-store availability:
    if an OSS-tier store backing is ever added to :func:`store_to_dlq`, update
    this predicate in lockstep so it never drifts from real store behavior.
    """
    return _get_pro_dlq() is not None


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
