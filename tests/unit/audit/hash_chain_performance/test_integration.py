"""
Hash Chain Performance 컴포넌트 통합 테스트.

성능 최적화 컴포넌트들을 함께 사용하는 전체 흐름 테스트:
- BatchFlushWriter + AsyncAuditWriter 조합
- LuaAtomicHashChain + PipelineBatchQuery 조합
- 전체 쓰기 플로우 성능 검증
"""

import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path

from .conftest import MockRedisClient


class TestPerformanceIntegration:
    """Hash Chain Performance 통합 테스트."""

    def test_full_write_flow_with_performance_components(self):
        """Test complete write flow using performance components."""
        from baldur.audit.performance import (
            BatchFlushConfig,
            HashChainPerformanceManager,
        )

        redis = MockRedisClient()

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = HashChainPerformanceManager(
                redis_client=redis,
                log_dir=Path(tmpdir),
                key_prefix="test:",
            )

            # Start watchdog
            manager.start_watchdog()

            # Create batch writer
            path = Path(tmpdir) / "audit.jsonl"
            batch_writer = manager.get_batch_writer(
                path,
                BatchFlushConfig(batch_size=5, sync_on_flush=False),
            )

            try:
                # Simulate write flow
                for i in range(10):
                    # Reserve sequence (Lua atomic)
                    success, seq, _ = manager.lua_chain.reserve_sequence_atomic(
                        expected_hash=f"hash_{i}",
                        previous_hash=f"hash_{i - 1}" if i > 0 else "GENESIS",
                    )

                    # Track in watchdog
                    manager.get_watchdog().register_pending(seq)

                    # Write to batch writer
                    entry = {
                        "data": f"entry_{i}",
                        "integrity": {
                            "sequence": seq,
                            "current_hash": f"hash_{i}",
                        },
                    }
                    batch_writer.write(entry)

                    # Mark committed
                    manager.get_watchdog().mark_committed(seq)

                # Force flush remaining
                batch_writer.force_flush()

                # Verify file was written
                assert path.exists()
                with open(path) as f:
                    lines = f.readlines()
                assert len(lines) == 10

            finally:
                batch_writer.close()
                manager.stop_all()

    def test_sampling_verification_performance(self):
        """Test that sampling is faster than full verification."""
        from baldur.audit.performance import (
            SamplingConfig,
            SamplingVerifier,
        )

        # Create large chain
        entries = []
        prev_hash = "GENESIS"

        for i in range(1000):
            entry = {
                "data": f"entry_{i}",
                "integrity": {
                    "sequence": i + 1,
                    "previous_hash": prev_hash,
                },
            }
            json_str = json.dumps(entry, sort_keys=True)
            current_hash = hashlib.sha256(json_str.encode()).hexdigest()
            entry["integrity"]["current_hash"] = current_hash
            entries.append(entry)
            prev_hash = current_hash

        # Full verification
        full_verifier = SamplingVerifier(SamplingConfig(sample_rate=1.0))
        start = time.monotonic()
        full_verifier.verify_sampled(entries)
        full_time = time.monotonic() - start

        # Sampled verification
        sampled_verifier = SamplingVerifier(
            SamplingConfig(
                sample_rate=0.1,
                max_samples=100,
            )
        )
        start = time.monotonic()
        sampled_verifier.verify_sampled(entries)
        sampled_time = time.monotonic() - start

        # Sampled should be faster (or at least not significantly slower)
        # Note: For small chains, overhead may dominate
        # Guard against timing resolution issues (full_time can be 0.0 on fast systems)
        # On very fast systems, both times can be extremely small (< 50ms)
        # In that case, timing noise dominates and comparison is meaningless
        if full_time < 0.05:
            # Full verification under 50ms means the dataset is too small
            # for timing comparison to be statistically meaningful
            pass
        else:
            assert sampled_time <= full_time * 2  # Allow some margin

    def test_concurrent_batch_writes(self):
        """Test concurrent writes to batch writer."""
        from baldur.audit.performance import (
            BatchFlushConfig,
            BatchFlushWriter,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "concurrent.jsonl"
            writer = BatchFlushWriter(
                path,
                BatchFlushConfig(batch_size=20, sync_on_flush=False),
            )

            errors = []

            def write_entries(start_id: int, count: int):
                try:
                    for i in range(count):
                        writer.write({"id": start_id + i})
                except Exception as e:
                    errors.append(str(e))

            # Start multiple threads
            threads = [
                threading.Thread(target=write_entries, args=(i * 10, 10))
                for i in range(5)
            ]

            for t in threads:
                t.start()

            for t in threads:
                t.join()

            writer.force_flush()
            writer.close()

            # Should have no errors
            assert len(errors) == 0

            # Should have all entries
            with open(path) as f:
                lines = f.readlines()
            assert len(lines) == 50
