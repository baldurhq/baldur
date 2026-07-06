"""
Singleton Audit Adapter Management.

Usage:
    >>> from baldur.adapters.audit.singleton import get_audit_adapter
    >>> adapter = get_audit_adapter()
    >>> adapter.log(entry)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from baldur.utils.singleton import make_singleton_factory

if TYPE_CHECKING:
    from baldur.interfaces.audit_adapter import AuditLogAdapter

logger = structlog.get_logger()


def _create_audit_adapter() -> AuditLogAdapter:
    # 1. ProviderRegistry
    try:
        from baldur.factory import ProviderRegistry

        adapter = ProviderRegistry.get_audit_adapter()
        if adapter is not None:
            logger.debug("audit_adapter.using_adapter_providerregistry")
            return adapter
    except (ImportError, ValueError, AttributeError):
        pass

    # 2. FileAuditLogAdapter
    try:
        from .file_adapter import FileAuditLogAdapter

        log_path = os.getenv("AUDIT_LOG_PATH", "logs/audit.jsonl")
        logger.debug("audit_adapter.using_fileauditlogadapter", log_path=log_path)
        return FileAuditLogAdapter(log_path)
    except Exception as e:
        logger.warning("audit_adapter.fileauditlogadapter_failed", error=e)

    # 3. NullAuditLogAdapter fallback
    from .null_adapter import NullAuditLogAdapter

    logger.warning("audit_adapter.using_nullauditlogadapter_fallback")
    return NullAuditLogAdapter()


get_audit_adapter, configure_audit_adapter, reset_audit_adapter = (
    make_singleton_factory("audit_adapter", _create_audit_adapter)
)
