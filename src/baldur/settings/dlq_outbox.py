"""
DLQ Outbox Settings - Pydantic v2.

Configuration for the non-blocking RingBuffer-backed outbox that lifts the
DLQ store path off ``protect()``'s failure hot path (impl doc 486).

Environment Variables:
    BALDUR_DLQ_OUTBOX_ENABLED=true
    BALDUR_DLQ_OUTBOX_CAPACITY=10000
    BALDUR_DLQ_OUTBOX_BATCH_SIZE=50
    BALDUR_DLQ_OUTBOX_FLUSH_INTERVAL_SECONDS=0.1
    BALDUR_DLQ_OUTBOX_DROP_RATE_THRESHOLD=0.01
    BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS=5.0
    BALDUR_DLQ_OUTBOX_DURABLE=false
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class DLQOutboxSettings(BaseSettings):
    """Settings for the DLQ outbox (impl doc 486 D3).

    The outbox is a RingBuffer-backed queue that decouples the DLQ store
    path from the request thread. ``capacity`` caps entry count (NOT total
    bytes — see Behavioral Notes in 486). Producer hot-path latency is the
    RingBuffer ``put`` cost (~50-100 ns lock-bounded); the worker thread
    pays the downstream DLQ DB cost.
    """

    model_config = make_settings_config("BALDUR_DLQ_OUTBOX_")

    enabled: bool = Field(
        default=True,
        description=(
            "Async-default flip per plan 2026-05-08. When True, the DLQSink "
            "and other auto-benefit callers route through the outbox. "
            "Sync-required surfaces opt back in via mode='sync' kwarg."
        ),
    )
    capacity: int = Field(
        default=10_000,
        ge=100,
        le=1_000_000,
        description=(
            "Max RingBuffer entry count (NOT total bytes). DLQ payloads "
            "typically run 5-50 KB; tune downward if memory is constrained."
        ),
    )
    batch_size: int = Field(
        default=50,
        ge=1,
        le=10_000,
        description="Entries per worker drain iteration.",
    )
    flush_interval_seconds: float = Field(
        default=0.1,
        ge=0.01,
        le=60.0,
        description="Max time the worker blocks waiting for new entries before flushing the partial batch.",
    )
    drop_rate_threshold: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        description="RingBuffer drop-rate fraction that triggers the on_drop_threshold callback (default 1%).",
    )
    join_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description="Worker thread join timeout during graceful shutdown / flush_and_wait.",
    )
    durable: bool = Field(
        default=False,
        description=(
            "PRO opt-in: route worker drain through DiskPersistentBuffer "
            "(LMDB) before dispatching to the DLQ DB. Producer hot path is "
            "unaffected (RingBuffer-only)."
        ),
    )


def get_dlq_outbox_settings() -> DLQOutboxSettings:
    from baldur.settings.root import get_config

    return get_config().services_group.dlq_outbox


def reset_dlq_outbox_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["dlq_outbox"]
    except KeyError:
        pass
