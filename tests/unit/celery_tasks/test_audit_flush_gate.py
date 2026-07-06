"""Audit-buffer drain gate + null-guard + canonical-path tests (600 D2/D3).

Covers:
- Effective drain gate matrix (``enabled`` x ``buffer_redis_enabled``): the
  OFF combos early-exit with ``{"status": "disabled"}`` and zero Redis work
  (the accessor is never invoked).
- Default-settings deployment is drain-silent (no beat entries injected).
- Null-target guard: a ``NullAuditLogAdapter`` registry default blocks the
  flush (entries retained, WARNING emitted) instead of silently discarding.
- Canonical flush path: a real registered ``FileAuditLogAdapter`` receives
  the entries through ``flush_to_external_safe`` — no mocked adapter seam.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.audit.redis_buffer import reset_redis_audit_buffer
from baldur.settings.audit import override_audit_settings

# Real Lua-simulating fake Redis (reused — already proves flush_to_external_safe
# at the buffer level in test_redis_batch_advanced.py).
from tests.unit.audit.test_redis_batch_advanced import FakeRedisWithLua

_LOCK_PATH = (
    "baldur_pro.services.coordination.distributed_recovery_lock.DistributedRecoveryLock"
)
_ACCESSOR = "baldur.celery_tasks.audit_flush_tasks.get_redis_audit_buffer"
_ADAPTER = "baldur.factory.ProviderRegistry.get_audit_adapter"


@pytest.fixture(autouse=True)
def _isolate_buffer_state():
    reset_redis_audit_buffer()
    yield
    reset_redis_audit_buffer()


def _acquired_lock() -> MagicMock:
    lock = MagicMock()
    lock.acquire.return_value = True
    return lock


# =============================================================================
# D3 — effective drain gate: OFF combos early-exit, zero Redis
# =============================================================================


class TestDrainGateEarlyExit:
    @pytest.mark.parametrize(
        ("enabled", "buffer_enabled"),
        [
            (False, False),
            (False, True),
            (True, False),
        ],
    )
    def test_flush_disabled_when_gate_off(self, enabled, buffer_enabled):
        """Each OFF combo early-exits; the buffer accessor is never called."""
        from baldur.celery_tasks import audit_flush_tasks

        with (
            override_audit_settings(
                enabled=enabled, buffer_redis_enabled=buffer_enabled
            ),
            patch(_ACCESSOR) as mock_accessor,
        ):
            result = audit_flush_tasks.flush_redis_audit_buffer.apply(task_id="t").get()

        assert result["status"] == "disabled"
        # Zero Redis work: the drain buffer is never acquired.
        mock_accessor.assert_not_called()

    def test_recover_disabled_when_gate_off(self):
        from baldur.celery_tasks import audit_flush_tasks

        with (
            override_audit_settings(enabled=True, buffer_redis_enabled=False),
            patch(_ACCESSOR) as mock_accessor,
        ):
            result = audit_flush_tasks.recover_orphaned_processing_queues.apply(
                task_id="t"
            ).get()

        assert result["status"] == "disabled"
        mock_accessor.assert_not_called()

    def test_safety_ltrim_disabled_when_gate_off(self):
        from baldur.celery_tasks import audit_flush_tasks

        with (
            override_audit_settings(enabled=False, buffer_redis_enabled=True),
            patch(_ACCESSOR) as mock_accessor,
        ):
            result = audit_flush_tasks.apply_audit_buffer_safety_ltrim.apply(
                task_id="t"
            ).get()

        assert result["status"] == "disabled"
        mock_accessor.assert_not_called()

    def test_default_settings_inject_no_audit_flush_entries(self):
        """Default deployment (gate OFF) injects no audit-flush beat entries."""
        from baldur.adapters.celery.beat_schedule import get_baldur_beat_schedule

        with override_audit_settings(enabled=False, buffer_redis_enabled=False):
            schedule = get_baldur_beat_schedule(
                include_cleanup=False,
                include_intelligence=False,
                include_compliance=False,
                include_traffic_aware=False,
                include_canary_watchdog=False,
                include_governance=False,
                include_xtest_cleanup=False,
                include_saga=False,
                include_chaos_scheduler=False,
                include_postmortem=False,
                include_dlq_maintenance=False,
                include_config_apply=False,
                include_legacy=False,
                # include_audit_flush=None resolves from the gate (OFF)
            )

        assert schedule == {}


# =============================================================================
# D2 — null-target guard
# =============================================================================


class TestNullTargetGuard:
    def test_null_adapter_blocks_flush_and_retains_entries(self):
        """Gate ON + null registry default -> flush blocked, WARNING, retained."""
        pytest.importorskip("baldur_pro")
        import structlog

        from baldur.adapters.audit.null_adapter import NullAuditLogAdapter
        from baldur.celery_tasks import audit_flush_tasks

        spy_buffer = MagicMock()

        with (
            override_audit_settings(enabled=True, buffer_redis_enabled=True),
            patch(_LOCK_PATH, MagicMock(return_value=_acquired_lock())),
            patch(_ACCESSOR, return_value=spy_buffer),
            patch(_ADAPTER, return_value=NullAuditLogAdapter()),
            structlog.testing.capture_logs() as logs,
        ):
            result = audit_flush_tasks.flush_redis_audit_buffer.apply(task_id="t").get()

        assert result["status"] == "blocked"
        assert result["reason"] == "null_target_adapter"
        # Entries retained: no flush attempted against the null sink.
        spy_buffer.flush_to_external_safe.assert_not_called()
        blocked = [e for e in logs if e.get("event") == "audit_flush.flush_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["reason"] == "null_target_adapter"


# =============================================================================
# D2 — canonical flush path (real registered FileAuditLogAdapter)
# =============================================================================


class TestCanonicalFlushPath:
    def test_canonical_flush_through_real_file_adapter(self, tmp_path):
        """A populated buffer drains into a real registered file adapter."""
        pytest.importorskip("baldur_pro")
        from baldur.adapters.audit.file_adapter import FileAuditLogAdapter
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer
        from baldur.celery_tasks import audit_flush_tasks
        from baldur.factory import ProviderRegistry

        # Populated fake-Redis buffer (2 entries in one active domain).
        fake = FakeRedisWithLua()
        payloads = [
            json.dumps(
                {
                    "entry": {"event": "e1"},
                    "timestamp": "2026-01-08T00:00:00Z",
                    "instance_id": "x",
                }
            ),
            json.dumps(
                {
                    "entry": {"event": "e2"},
                    "timestamp": "2026-01-08T00:00:01Z",
                    "instance_id": "x",
                }
            ),
        ]
        fake._data["audit:{payment}:buffer"] = list(payloads)
        fake._sets["audit:active_domains"] = {"payment"}

        buffer = RedisAuditBuffer(redis_client=fake, enable_graceful_shutdown=False)

        # Real file adapter registered as the registry default (no mocked seam).
        audit_file = tmp_path / "audit.jsonl"
        adapter = FileAuditLogAdapter(file_path=audit_file)

        snapshot = ProviderRegistry.audit.save_state()
        try:
            ProviderRegistry.audit.set_instance("file_canonical_600", adapter)
            ProviderRegistry.audit.set_default("file_canonical_600")

            with (
                override_audit_settings(enabled=True, buffer_redis_enabled=True),
                patch(_LOCK_PATH, MagicMock(return_value=_acquired_lock())),
                patch(_ACCESSOR, return_value=buffer),
            ):
                result = audit_flush_tasks.flush_redis_audit_buffer.apply(
                    kwargs={"batch_size": 100}, task_id="t"
                ).get()
        finally:
            ProviderRegistry.audit.restore_state(snapshot)

        assert result["status"] == "success"
        assert result["flushed_count"] == 2

        # The real file adapter received both entries.
        lines = audit_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert any("e1" in line for line in lines)
        assert any("e2" in line for line in lines)
