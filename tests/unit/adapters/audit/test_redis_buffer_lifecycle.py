"""RedisAuditBuffer drain-side lifecycle tests (600 D1/D3/D4).

Covers:
- D1 accessor: process-lifetime singleton identity, negative-cache TTL,
  reset, and hooks-off construction (zero atexit/signal hooks from the
  drain path).
- D3 writer-footgun: one-shot ``drain_disabled`` WARNING + reset re-arm.
- D4 fix-356 mirror: ``_graceful_shutdown`` restores ``logging.raiseExceptions``.
"""

from __future__ import annotations

import logging
import signal
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.audit.redis_buffer import (
    RedisAuditBuffer,
    _reset_drain_disabled_warning,
    get_redis_audit_buffer,
    reset_redis_audit_buffer,
)
from baldur.settings.audit import override_audit_settings

_CREATE = "baldur.adapters.audit.redis_buffer.create_redis_audit_buffer"


def _drain_warns(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e.get("event") == "redis_audit_buffer.drain_disabled"]


@pytest.fixture(autouse=True)
def _isolate_lifecycle_state():
    """Drop runtime-scoped buffer state + re-arm the one-shot warning, and
    restore ``logging.raiseExceptions`` around every test (xdist-safe)."""
    reset_redis_audit_buffer()
    _reset_drain_disabled_warning()
    original_raise = logging.raiseExceptions
    yield
    reset_redis_audit_buffer()
    _reset_drain_disabled_warning()
    logging.raiseExceptions = original_raise


# =============================================================================
# D1 — accessor singleton identity / negative cache / reset
# =============================================================================


class TestDrainBufferAccessor:
    def test_accessor_returns_cached_singleton(self):
        """Repeated calls construct at most one buffer."""
        sentinel = MagicMock()
        with patch(_CREATE, return_value=sentinel) as mock_create:
            first = get_redis_audit_buffer()
            second = get_redis_audit_buffer()

        assert first is sentinel
        assert second is sentinel
        assert mock_create.call_count == 1

    def test_accessor_builds_with_graceful_shutdown_disabled(self):
        """The drain buffer is built hooks-off (G3 eliminated at the root)."""
        with patch(_CREATE, return_value=MagicMock()) as mock_create:
            get_redis_audit_buffer()

        _, kwargs = mock_create.call_args
        assert kwargs.get("enable_graceful_shutdown") is False

    def test_negative_cache_suppresses_retry(self):
        """A None build activates the negative cache: no immediate rebuild."""
        with patch(_CREATE, return_value=None) as mock_create:
            assert get_redis_audit_buffer() is None
            assert get_redis_audit_buffer() is None

        # Second call short-circuited on the negative cache.
        assert mock_create.call_count == 1

    def test_negative_cache_ttl_expiry_allows_retry(self, monkeypatch):
        """After the retry interval, a fresh build is attempted."""
        import baldur.adapters.audit.redis_buffer as rb

        clock = {"t": 1000.0}
        monkeypatch.setattr(rb.time, "monotonic", lambda: clock["t"])

        with patch(_CREATE, return_value=None) as mock_create:
            assert get_redis_audit_buffer() is None  # build #1, fail at t=1000
            clock["t"] = 1000.0 + rb._REDIS_AUDIT_BUFFER_RETRY_INTERVAL + 1
            assert get_redis_audit_buffer() is None  # TTL expired -> build #2

        assert mock_create.call_count == 2

    def test_reset_drops_cached_buffer(self):
        """reset() forces the next access to rebuild."""
        with patch(_CREATE, return_value=MagicMock()) as mock_create:
            get_redis_audit_buffer()
            reset_redis_audit_buffer()
            get_redis_audit_buffer()

        assert mock_create.call_count == 2

    def test_reset_closes_underlying_client(self):
        """reset() best-effort closes the cached buffer's Redis client."""
        buffer = MagicMock()
        with patch(_CREATE, return_value=buffer):
            get_redis_audit_buffer()
        reset_redis_audit_buffer()

        buffer._redis.close.assert_called_once()


# =============================================================================
# D1/G3 — hooks-off construction registers nothing
# =============================================================================


class TestHooksOffConstruction:
    def test_graceful_shutdown_false_registers_no_hooks(self):
        """No atexit hook, no signal-disposition change when hooks are off."""
        sigterm_before = signal.getsignal(signal.SIGTERM)

        buffer = RedisAuditBuffer(
            redis_client=MagicMock(), enable_graceful_shutdown=False
        )

        assert buffer._shutdown_registered is False
        assert signal.getsignal(signal.SIGTERM) is sigterm_before

    def test_graceful_shutdown_false_registers_no_atexit(self, monkeypatch):
        """atexit.register is never called from the hooks-off path."""
        import baldur.adapters.audit.redis_buffer as rb

        registered: list = []
        monkeypatch.setattr(
            rb.atexit, "register", lambda fn, *a, **k: registered.append(fn)
        )

        RedisAuditBuffer(redis_client=MagicMock(), enable_graceful_shutdown=False)

        assert registered == []

    def test_repeated_task_runs_construct_one_buffer(self):
        """SC1: N flush-task runs build at most one buffer; SIGTERM unchanged."""
        pytest.importorskip("baldur_pro")
        from baldur.celery_tasks import audit_flush_tasks

        sigterm_before = signal.getsignal(signal.SIGTERM)

        mock_buffer = MagicMock()
        mock_buffer.flush_to_external_safe.return_value = 0

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True

        with (
            override_audit_settings(enabled=True, buffer_redis_enabled=True),
            patch(_CREATE, return_value=mock_buffer) as mock_create,
            patch(
                "baldur_pro.services.coordination.distributed_recovery_lock.DistributedRecoveryLock",
                MagicMock(return_value=mock_lock),
            ),
            patch(
                "baldur.factory.ProviderRegistry.get_audit_adapter",
                return_value=MagicMock(),
            ),
        ):
            for i in range(5):
                audit_flush_tasks.flush_redis_audit_buffer.apply(
                    kwargs={"batch_size": 10}, task_id=f"t{i}"
                ).get()

        assert mock_create.call_count == 1
        assert signal.getsignal(signal.SIGTERM) is sigterm_before


# =============================================================================
# D3 — one-shot drain-disabled warning
# =============================================================================


class TestDrainDisabledWarning:
    def test_warning_emitted_once_when_gate_off(self):
        import structlog

        with (
            override_audit_settings(enabled=False, buffer_redis_enabled=False),
            structlog.testing.capture_logs() as logs,
        ):
            RedisAuditBuffer(redis_client=MagicMock(), enable_graceful_shutdown=False)
            RedisAuditBuffer(redis_client=MagicMock(), enable_graceful_shutdown=False)

        assert len(_drain_warns(logs)) == 1  # one-shot

    def test_no_warning_when_gate_on(self):
        import structlog

        with (
            override_audit_settings(enabled=True, buffer_redis_enabled=True),
            structlog.testing.capture_logs() as logs,
        ):
            RedisAuditBuffer(redis_client=MagicMock(), enable_graceful_shutdown=False)

        assert _drain_warns(logs) == []

    def test_reset_rearms_warning(self):
        import structlog

        with override_audit_settings(enabled=False, buffer_redis_enabled=False):
            with structlog.testing.capture_logs() as logs1:
                RedisAuditBuffer(
                    redis_client=MagicMock(), enable_graceful_shutdown=False
                )
            assert len(_drain_warns(logs1)) == 1

            _reset_drain_disabled_warning()

            with structlog.testing.capture_logs() as logs2:
                RedisAuditBuffer(
                    redis_client=MagicMock(), enable_graceful_shutdown=False
                )
            assert len(_drain_warns(logs2)) == 1


# =============================================================================
# D4 — logging.raiseExceptions save/restore (fix-356 mirror)
# =============================================================================


class TestRaiseExceptionsRestore:
    def test_graceful_shutdown_restores_raise_exceptions_true(self):
        buffer = RedisAuditBuffer(
            redis_client=MagicMock(), enable_graceful_shutdown=False
        )
        logging.raiseExceptions = True

        buffer._graceful_shutdown()

        assert logging.raiseExceptions is True

    def test_graceful_shutdown_restores_raise_exceptions_false(self):
        buffer = RedisAuditBuffer(
            redis_client=MagicMock(), enable_graceful_shutdown=False
        )
        logging.raiseExceptions = False

        buffer._graceful_shutdown()

        assert logging.raiseExceptions is False
