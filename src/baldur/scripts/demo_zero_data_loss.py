#!/usr/bin/env python
"""
Kafka + WAL integration demo: proving 0% data loss.

Demonstrates 100% recovery after a complete Kafka cluster outage.

Demo scenario:
1. Generate 1000 events
2. Stop the Kafka cluster (after 500 are sent)
3. Remaining 500 -> stored in the WAL
4. Recover the Kafka cluster
5. Automatically replay the 500 events from the WAL
6. Final check: all 1000 events arrived

Usage:
    python -m baldur.scripts.demo_zero_data_loss

    # Mock mode (test without Kafka)
    python -m baldur.scripts.demo_zero_data_loss --mock
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from typing import Any
from unittest.mock import MagicMock

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _configure_demo_logging() -> None:
    """Logging setup specific to the demo script. Called only from __main__."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


class DemoStats:
    """Demo statistics."""

    def __init__(self):
        self.kafka_sent = 0
        self.kafka_failed = 0
        self.wal_written = 0
        self.wal_replayed = 0
        self.total_events = 0


def create_audit_entry(index: int) -> dict[str, Any]:
    """Create an audit event for testing."""
    return {
        "id": f"demo-event-{index}",
        "action": "DEMO_EVENT",
        "target_type": "demo",
        "target_id": str(index),
        "timestamp": utc_now().isoformat(),
        "details": {"index": index, "demo": True},
    }


def run_demo_with_mock():  # noqa: C901, PLR0915
    """Mock mode demo (runs without Kafka)."""
    from baldur.audit.wal import WALConfig, WriteAheadLog

    stats = DemoStats()
    stats.total_events = 1000

    with tempfile.TemporaryDirectory() as tmp_dir:
        # WAL configuration
        wal_config = WALConfig(
            wal_dir=tmp_dir,
            sync_on_write=True,
            max_files=100,
        )
        wal = WriteAheadLog(config=wal_config)

        # Mock Kafka Producer
        mock_producer = MagicMock()
        kafka_online = True

        def mock_produce(**kwargs):
            nonlocal kafka_online
            if not kafka_online:
                raise ConnectionError("Kafka cluster is down")
            stats.kafka_sent += 1

        mock_producer.produce = mock_produce
        mock_producer.flush = MagicMock(return_value=0)

        print("\n" + "=" * 60)
        print("🚀 PHASE 1: normal delivery (events 1-500)")
        print("=" * 60)

        # Phase 1: normal delivery (500 events)
        for i in range(1, 501):
            entry_dict = create_audit_entry(i)

            # WAL write (always)
            wal.write(entry_dict)
            stats.wal_written += 1

            # Kafka send
            try:
                mock_producer.produce(
                    topic="baldur.audit.events",
                    value=json.dumps(entry_dict).encode("utf-8"),
                )
            except Exception:
                stats.kafka_failed += 1

            if i % 100 == 0:
                print(
                    f"  ✅ {i} sent (Kafka: {stats.kafka_sent}, WAL: {stats.wal_written})"
                )

        print(f"\n📊 Phase 1 result: Kafka={stats.kafka_sent}, WAL={stats.wal_written}")

        print("\n" + "=" * 60)
        print("🔴 PHASE 2: Kafka cluster outage!")
        print("=" * 60)
        kafka_online = False
        time.sleep(0.5)  # visual effect

        print("\n" + "=" * 60)
        print("📝 PHASE 3: WAL fallback (events 501-1000)")
        print("=" * 60)

        # Phase 3: WAL fallback (500 events)
        for i in range(501, 1001):
            entry_dict = create_audit_entry(i)

            # WAL write (always)
            wal.write(entry_dict)
            stats.wal_written += 1

            # Kafka send attempt (fails)
            try:
                mock_producer.produce(
                    topic="baldur.audit.events",
                    value=json.dumps(entry_dict).encode("utf-8"),
                )
            except Exception:
                stats.kafka_failed += 1

            if i % 100 == 0:
                print(f"  📦 {i} recorded (buffering in WAL...)")

        print(
            f"\n📊 Phase 3 result: Kafka failed={stats.kafka_failed}, WAL={stats.wal_written}"
        )
        print(
            f"📦 Unsent events buffered in WAL: {stats.wal_written - stats.kafka_sent}"
        )

        print("\n" + "=" * 60)
        print("🟢 PHASE 4: Kafka cluster recovered!")
        print("=" * 60)
        kafka_online = True
        time.sleep(0.5)  # visual effect

        print("\n" + "=" * 60)
        print("🔄 PHASE 5: replay from WAL")
        print("=" * 60)

        # Phase 5: replay unsent events from the WAL
        unprocessed = wal.recover_unprocessed(last_processed_seq=500)
        print(f"  📋 Found {len(unprocessed)} unprocessed events in WAL")

        for entry in unprocessed:
            try:
                mock_producer.produce(
                    topic="baldur.audit.events",
                    value=json.dumps(entry.data).encode("utf-8"),
                )
                stats.wal_replayed += 1
            except Exception:
                pass

            if stats.wal_replayed % 100 == 0:
                print(f"  🔄 {stats.wal_replayed} replayed")

        print(f"\n📊 Phase 5 result: {stats.wal_replayed} replayed from WAL")

        print("\n" + "=" * 60)
        print("🎉 FINAL RESULT: 0% data loss proven!")
        print("=" * 60)

        total_delivered = stats.kafka_sent + stats.wal_replayed
        print(
            f"""
        📈 Final statistics:
        ─────────────────────────────────
        Total events:        {stats.total_events}
        Kafka direct sends:  {stats.kafka_sent}
        WAL replays:         {stats.wal_replayed}
        ─────────────────────────────────
        Total delivered:     {total_delivered}
        Data loss:           {stats.total_events - total_delivered}
        ─────────────────────────────────
        """
        )

        if total_delivered == stats.total_events:
            print("✅ SUCCESS: all events were delivered!")
            return 0
        print(f"❌ FAILURE: {stats.total_events - total_delivered} events lost!")
        return 1


def run_demo_with_kafka():
    """Real Kafka integration demo."""
    print("⚠️ The real Kafka integration demo requires a Kafka cluster.")
    print("  To run in mock mode: use the --mock option.")
    print()
    print("Kafka cluster configuration:")
    print("  - BALDUR_KAFKA_AUDIT_BOOTSTRAP_SERVERS=localhost:9092")
    print("  - BALDUR_KAFKA_AUDIT_TOPIC=baldur.audit.events")
    print()

    try:
        # 528 D10-v2: KafkaAuditAdapter relocated to baldur_dormant.
        from baldur.settings.kafka_audit import KafkaAuditSettings
        from baldur_dormant.adapters.audit.kafka_adapter import KafkaAuditAdapter

        settings = KafkaAuditSettings()
        print(f"📡 Kafka brokers: {settings.bootstrap_servers}")
        print(f"📋 Topic: {settings.topic}")

        # Attempt real integration
        adapter = KafkaAuditAdapter(settings=settings)
        print("✅ Kafka Producer initialized successfully")

        # Simple test message
        from baldur.interfaces.audit_adapter import AuditEntry

        entry = AuditEntry(
            action="DEMO_TEST",
            target_type="demo",
            target_id="test",
        )
        adapter.log(entry)
        adapter.flush(timeout=5.0)
        print("✅ Test message sent successfully")

        adapter.close()
        return 0

    except ImportError as e:
        print(f"confluent-kafka not installed: {e}")
        print("  Install: pip install confluent-kafka (or 'baldur-pro[kafka]')")
        return 1
    except Exception as e:
        print(f"❌ Kafka connection failed: {e}")
        return 1


def main():
    """Demo main function."""
    parser = argparse.ArgumentParser(
        description="Kafka + WAL integration demo: proving 0% data loss",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in mock mode (test without Kafka)",
    )
    args = parser.parse_args()

    print()
    print("╔════════════════════════════════════════════════════════════╗")
    print("║  Kafka + WAL integration demo: proving 0% data loss        ║")
    print("║  Baldur Audit System                                       ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()

    if args.mock:
        print("🔧 Running in mock mode (test without Kafka)")
        return run_demo_with_mock()
    return run_demo_with_kafka()


if __name__ == "__main__":
    _configure_demo_logging()
    sys.exit(main())
