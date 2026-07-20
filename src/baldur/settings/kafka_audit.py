"""
Kafka Audit Settings.

Configuration for streaming high-throughput audit events to Kafka.
Provides confluent-kafka (librdkafka-based) Producer settings.

Key capabilities:
- Idempotent Producer (prevents duplicate delivery)
- Batched delivery (linger_ms, batch_size)
- Compression support (snappy, lz4, zstd)
- Hot partition prevention (salted partitioning)
- TLS/SASL authentication

Environment variable prefix: BALDUR_KAFKA_AUDIT_

Usage:
    from baldur.settings.kafka_audit import KafkaAuditSettings

    settings = KafkaAuditSettings()
    print(settings.bootstrap_servers)
    print(settings.topic)
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class SerializationFormat(str, Enum):
    """Serialization format."""

    JSON = "json"
    AVRO = "avro"
    PROTOBUF = "protobuf"


class KafkaAuditSettings(BaseSettings):
    """
    Kafka audit log configuration.

    Environment variable examples:
        BALDUR_KAFKA_AUDIT_BOOTSTRAP_SERVERS=kafka1:9092,kafka2:9092
        BALDUR_KAFKA_AUDIT_TOPIC=baldur.audit.events
        BALDUR_KAFKA_AUDIT_ENABLE_IDEMPOTENCE=true
    """

    model_config = make_settings_config("BALDUR_KAFKA_AUDIT_")

    # ==========================================================================
    # Connection
    # ==========================================================================
    bootstrap_servers: list[str] = Field(
        default=["localhost:9092"],
        description="List of Kafka broker addresses",
    )
    topic: str = Field(
        default="baldur.audit.events",
        description="Audit event topic name",
    )
    dead_letter_topic: str = Field(
        default="baldur.audit.events.dlt",
        description="Dead Letter Topic (stores serialization-failed events)",
    )

    # ==========================================================================
    # Idempotent Producer (Exactly-once)
    # ==========================================================================
    enable_idempotence: bool = Field(
        default=True,
        description="Enable Idempotent Producer (prevents duplicate delivery)",
    )

    # ==========================================================================
    # Batching
    # ==========================================================================
    batch_size_bytes: int = Field(
        default=16384,  # 16KB
        ge=1024,
        le=1048576,  # 1MB
        description="Batch size (bytes)",
    )
    linger_ms: int = Field(
        default=10,  # 10ms
        ge=0,
        le=1000,
        description="Batch linger time (ms). Expected P50 ~20ms",
    )
    latency_budget_p99_ms: int = Field(
        default=50,
        description="Producer latency budget P99 (linger + ack + network)",
    )
    latency_alert_threshold_ms: int = Field(
        default=100,
        description="Latency alert threshold. Warns when P99 > 100ms",
    )

    # ==========================================================================
    # Hot partition prevention
    # ==========================================================================
    partition_salt_enabled: bool = Field(
        default=True,
        description="Enable salt for Hot Partition prevention",
    )
    partition_salt_range: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Salt range (degree of partition distribution)",
    )

    # ==========================================================================
    # Compression
    # ==========================================================================
    compression_type: str = Field(
        default="zstd",
        description="Compression algorithm: zstd (recommended), snappy, lz4, gzip, none",
    )
    compression_level: int = Field(
        default=3,
        ge=1,
        le=22,
        description="Zstd compression level (1-22, higher = better ratio, more CPU)",
    )

    # ==========================================================================
    # Reliability
    # ==========================================================================
    acks: str = Field(
        default="all",
        description="ACK level: 0 (none), 1 (leader), all (all replicas)",
    )
    retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Number of retries",
    )
    retry_backoff_ms: int = Field(
        default=100,
        ge=10,
        le=5000,
        description="Retry backoff time (ms)",
    )
    message_timeout_ms: int = Field(
        default=30000,  # 30s
        ge=1000,
        le=120000,
        description="Message delivery timeout (ms)",
    )

    # ==========================================================================
    # Buffer (for Producer Lag monitoring)
    # ==========================================================================
    buffer_memory: int = Field(
        default=33554432,  # 32MB
        ge=1048576,  # 1MB
        le=1073741824,  # 1GB
        description="Producer buffer memory (bytes)",
    )
    max_queue_messages: int = Field(
        default=100000,
        ge=1000,
        le=10000000,
        description="Maximum number of messages in producer queue",
    )

    # ==========================================================================
    # Security: TLS/SSL
    # ==========================================================================
    security_protocol: str = Field(
        default="PLAINTEXT",
        description="Security protocol: PLAINTEXT, SSL, SASL_PLAINTEXT, SASL_SSL",
    )
    ssl_cafile: str | None = Field(
        default=None,
        description="CA certificate file path",
    )
    ssl_certfile: str | None = Field(
        default=None,
        description="Client certificate file path",
    )
    ssl_keyfile: str | None = Field(
        default=None,
        description="Client key file path",
    )

    # ==========================================================================
    # Security: SASL (production authentication)
    # ==========================================================================
    sasl_mechanism: str = Field(
        default="SCRAM-SHA-512",
        description="SASL mechanism: PLAIN, SCRAM-SHA-256, SCRAM-SHA-512",
    )
    sasl_username: str | None = Field(
        default=None,
        description="SASL username",
    )
    sasl_password: str | None = Field(
        default=None,
        description="SASL password (secret management recommended)",
    )

    # ==========================================================================
    # Serialization
    # ==========================================================================
    serialization_format: SerializationFormat = Field(
        default=SerializationFormat.JSON,
        description="Serialization format",
    )
    avro_schema_path: str | None = Field(
        default=None,
        description="Avro schema file path (.avsc)",
    )

    # ==========================================================================
    # Schema Registry
    # ==========================================================================
    schema_registry_url: str | None = Field(
        default=None,
        description="Confluent Schema Registry URL",
    )
    schema_compatibility: str = Field(
        default="BACKWARD",
        description="Schema compatibility policy: BACKWARD, FORWARD, FULL",
    )

    @property
    def is_tls_enabled(self) -> bool:
        """Check if TLS is enabled based on security protocol."""
        return self.security_protocol in ("SSL", "SASL_SSL")

    def get_producer_config(self) -> dict:
        """
        Return the confluent-kafka Producer configuration dictionary.

        Returns:
            Configuration dictionary needed to construct a Producer
        """
        import os

        config = {
            # Connection
            "bootstrap.servers": ",".join(self.bootstrap_servers),
            "client.id": f"baldur-audit-{os.getpid()}",
            # Idempotent Producer (exactly-once guarantee)
            "enable.idempotence": self.enable_idempotence,
            "acks": "all" if self.enable_idempotence else self.acks,
            "max.in.flight.requests.per.connection": 5,
            # Batching (performance tuning)
            "batch.size": self.batch_size_bytes,
            "linger.ms": self.linger_ms,
            # Compression
            "compression.type": self.compression_type,
            # Reliability
            "retries": self.retries,
            "retry.backoff.ms": self.retry_backoff_ms,
            # Buffer (for Producer Lag monitoring)
            "queue.buffering.max.messages": self.max_queue_messages,
            "queue.buffering.max.kbytes": self.buffer_memory // 1024,
            # Message delivery timeout
            "message.timeout.ms": self.message_timeout_ms,
        }

        # TLS/SASL authentication (for production)
        if self.security_protocol != "PLAINTEXT":
            config["security.protocol"] = self.security_protocol

            if "SSL" in self.security_protocol:
                if self.ssl_cafile:
                    config["ssl.ca.location"] = self.ssl_cafile
                if self.ssl_certfile:
                    config["ssl.certificate.location"] = self.ssl_certfile
                if self.ssl_keyfile:
                    config["ssl.key.location"] = self.ssl_keyfile

            if "SASL" in self.security_protocol:
                config["sasl.mechanism"] = self.sasl_mechanism
                if self.sasl_username:
                    config["sasl.username"] = self.sasl_username
                if self.sasl_password:
                    config["sasl.password"] = self.sasl_password

        return config


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def get_kafka_audit_settings() -> KafkaAuditSettings:
    """Return singleton KafkaAuditSettings instance via root config."""
    from baldur.settings.root import get_config

    return get_config().adapters.kafka_audit


def reset_kafka_audit_settings() -> None:
    """Invalidate cached KafkaAuditSettings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["kafka_audit"]
    except KeyError:
        pass


__all__ = [
    "KafkaAuditSettings",
    "SerializationFormat",
    "get_kafka_audit_settings",
    "reset_kafka_audit_settings",
]
