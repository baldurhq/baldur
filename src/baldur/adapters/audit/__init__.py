"""
Default Audit Log Adapter Implementations (OSS tier).

Provides non-invasive audit logging implementations:
- FileAuditLogAdapter: Log to files (default for production)
- StdoutAuditLogAdapter: Log to stdout (good for containers)
- NullAuditLogAdapter: No-op (for testing or opt-out)
- HashChainFileAuditLogAdapter: tamper-evident hash chain (D6/D22/D23)

Non-invasive principle:
- Never reach into customer DBs directly
- Default: FileAuditLogAdapter (local JSONL)
- External shipping is done via the sidecar pattern or the Export CLI

Dormant tier (relocated to ``baldur_dormant.adapters.audit`` per doc 528
D10-v2 / D16):
- KafkaAuditAdapter, kafka_consumer (KafkaConsumerConfig, BaseAuditConsumer,
  IdempotentAuditConsumer, RebalanceAwareConsumer, PostgreSQLSinkConfig,
  PostgreSQLSinkConsumer)
- WORM adapters (S3ObjectLockAdapter, LokiAdapter, HTTPWebhookAdapter,
  SidecarFileWatcher, create_worm_adapter)

OSS callers route through ``ProviderRegistry.audit_kafka_adapter`` /
``ProviderRegistry.audit_worm_adapter`` (NoOp defaults registered at OSS
bootstrap; concrete adapters self-register via ``baldur_dormant.
register_dormant_services()``). Direct import paths after the relocation:
``from baldur_dormant.adapters.audit.kafka_adapter import KafkaAuditAdapter``.
"""

from .file_adapter import FileAuditLogAdapter
from .hashchain_adapter import HashChainFileAuditLogAdapter
from .null_adapter import NullAuditLogAdapter
from .singleton import (
    configure_audit_adapter,
    get_audit_adapter,
    reset_audit_adapter,
)
from .stdout_adapter import StdoutAuditLogAdapter

__all__ = [
    # Default Adapters (Non-invasive, OSS tier)
    "FileAuditLogAdapter",
    "HashChainFileAuditLogAdapter",
    "StdoutAuditLogAdapter",
    "NullAuditLogAdapter",
    # Singleton Management
    "get_audit_adapter",
    "configure_audit_adapter",
    "reset_audit_adapter",
    # Django Adapter (optional - requires Django, lazy via __getattr__)
    "DjangoAuditLogAdapter",
    "get_django_audit_adapter",
]


# Lazy import for Django adapter (avoid import errors when Django is absent).
def __getattr__(name: str):
    """Lazy import for optional adapters."""
    if name in ("DjangoAuditLogAdapter", "get_django_audit_adapter"):
        from .django_adapter import DjangoAuditLogAdapter, get_django_audit_adapter

        if name == "DjangoAuditLogAdapter":
            return DjangoAuditLogAdapter
        return get_django_audit_adapter

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
