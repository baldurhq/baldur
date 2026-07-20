"""
Kafka Producer Settings — Pydantic v2.

Kafka producer timeout and connection settings.
Makes the previously hardcoded request_timeout_ms and send/flush/close timeout
values controllable through environment variables.

Environment Variables:
    BALDUR_KAFKA_PRODUCER_REQUEST_TIMEOUT_MS=10000
    BALDUR_KAFKA_PRODUCER_SEND_TIMEOUT=10.0
    BALDUR_KAFKA_PRODUCER_SHUTDOWN_TIMEOUT=5.0
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class KafkaProducerSettings(BaseSettings):
    """Kafka producer timeout settings."""

    model_config = make_settings_config("BALDUR_KAFKA_PRODUCER_")

    request_timeout_ms: int = Field(
        default=10000,
        ge=1000,
        le=120000,
        description="Kafka protocol-level request timeout (ms)",
    )

    send_timeout: float = Field(
        default=10.0,
        ge=1.0,
        le=120.0,
        description="Timeout for producer.send().get() — waits for async send completion",
    )

    shutdown_timeout: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="Timeout for producer.flush() and producer.close()",
    )


def get_kafka_producer_settings() -> "KafkaProducerSettings":
    """Single entry point via the root settings (SSOT)."""
    from baldur.settings.root import get_config

    return get_config().adapters.kafka_producer


def reset_kafka_producer_settings() -> None:
    """Delegate to the root reset (for tests)."""
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["kafka_producer"]
    except KeyError:
        pass
