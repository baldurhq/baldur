"""
Unit tests for :class:`baldur.adapters.postgres.noop_admin.NoopPgAdmin` (515 D7).

Source: ``src/baldur/adapters/postgres/noop_admin.py``

The Noop provider must satisfy the full ``PgAdminProvider`` ABC with safe
defaults: ``is_available()`` returns False, every read returns an empty /
zero structure, every write is a no-op. Only ``create_cursor`` is allowed
to raise — callers that try to escape the registry-resolved provider into
a real cursor must surface the misconfig.

Verification technique mix (per UNIT_TEST_GUIDELINES §8):
- §8.7 Boundary analysis — exhaustive enumeration of every abstract method.
- §8.2 Exception/edge cases — ``create_cursor`` raises ``RuntimeError``
  with an actionable hint message.
- §8.3 Idempotency — repeated calls return the same safe defaults.
"""

from __future__ import annotations

import pytest

from baldur.adapters.postgres.noop_admin import NoopPgAdmin
from baldur.interfaces.pg_admin import ConnectionStats, PgAdminProvider


@pytest.fixture
def noop() -> NoopPgAdmin:
    return NoopPgAdmin()


class TestNoopPgAdminContract:
    """Pinned safe defaults for every abstract method on ``PgAdminProvider``."""

    def test_is_pg_admin_provider_subclass(self, noop):
        """Concrete impl must satisfy the ABC for registry resolution."""
        assert isinstance(noop, PgAdminProvider)

    def test_is_available_returns_false(self, noop):
        """515 D7: ``is_available()`` False so consumers omit pg-only keys."""
        assert noop.is_available() is False

    def test_ping_returns_false(self, noop):
        assert noop.ping() is False

    def test_get_connection_stats_returns_all_zeros(self, noop):
        """Safe default: zero-counted ConnectionStats so dashboards render."""
        stats = noop.get_connection_stats()
        assert stats == ConnectionStats(
            total_connections=0,
            active=0,
            idle=0,
            idle_in_transaction=0,
        )

    def test_get_active_connection_count_returns_zero(self, noop):
        assert noop.get_active_connection_count() == 0

    def test_pg_sleep_returns_none(self, noop):
        assert noop.pg_sleep(1.5) is None

    def test_execute_slow_query_returns_none(self, noop):
        assert noop.execute_slow_query(seconds=5) is None

    def test_get_backend_pid_with_delay_returns_zero(self, noop):
        assert noop.get_backend_pid_with_delay() == 0

    @pytest.mark.parametrize("wait", [True, False])
    def test_acquire_advisory_lock_returns_false(self, noop, wait):
        assert noop.acquire_advisory_lock(lock_id=42, wait=wait) is False

    @pytest.mark.parametrize("wait", [True, False])
    def test_acquire_advisory_lock_shared_returns_false(self, noop, wait):
        assert noop.acquire_advisory_lock_shared(lock_id=42, wait=wait) is False

    def test_release_advisory_lock_returns_false(self, noop):
        assert noop.release_advisory_lock(lock_id=42) is False

    def test_release_advisory_lock_shared_returns_false(self, noop):
        assert noop.release_advisory_lock_shared(lock_id=42) is False

    def test_try_advisory_lock_returns_false(self, noop):
        assert noop.try_advisory_lock(lock_id=42) is False

    def test_set_lock_timeout_returns_none(self, noop):
        assert noop.set_lock_timeout(timeout_ms=1000) is None

    def test_set_statement_timeout_returns_none(self, noop):
        assert noop.set_statement_timeout(timeout_ms=1000) is None

    def test_reset_timeouts_returns_none(self, noop):
        assert noop.reset_timeouts() is None

    def test_execute_aggregate_query_returns_zero_tuple(self, noop):
        """Safe default: ``(0, 0.0, 0.0, 0.0)`` so callers see no rows."""
        assert noop.execute_aggregate_query("any_table") == (0, 0.0, 0.0, 0.0)

    def test_execute_nonexistent_table_query_returns_none(self, noop):
        assert noop.execute_nonexistent_table_query() is None

    def test_execute_timeout_query_returns_none(self, noop):
        assert noop.execute_timeout_query() is None

    @pytest.mark.parametrize(
        ("exclusive", "wait"),
        [(True, True), (True, False), (False, True), (False, False)],
    )
    def test_advisory_lock_context_yields_false_in_all_modes(
        self, noop, exclusive, wait
    ):
        """Context manager always yields ``False`` (lock never acquired)."""
        with noop.advisory_lock_context(
            lock_id=99, exclusive=exclusive, wait=wait
        ) as acquired:
            assert acquired is False

    def test_timeout_context_is_safe_no_op(self, noop):
        """Both timeout settings unused; context exits cleanly."""
        with noop.timeout_context(lock_timeout_ms=500, statement_timeout_ms=5000):
            pass  # body runs without error

    def test_create_cursor_raises_runtime_error_with_hint(self, noop):
        """Escape hatch: ``create_cursor`` must NOT silently fail.

        Callers reaching ``create_cursor()`` on the noop have escaped the
        ``is_available()`` gate. Surfacing the misconfig as a RuntimeError
        with an actionable hint matches the documented contract.
        """
        with pytest.raises(RuntimeError) as exc_info:
            noop.create_cursor()
        message = str(exc_info.value)
        assert "BALDUR_SQL_DSN" in message
        assert "Django" in message

    def test_execute_with_cursor_returns_none(self, noop):
        """``execute_with_cursor`` is a no-op even on a non-None cursor argument."""
        result = noop.execute_with_cursor(cursor=object(), query="SELECT 1")
        assert result is None


class TestNoopPgAdminIdempotency:
    """Repeated calls keep returning the same safe defaults — no hidden state."""

    def test_repeated_get_connection_stats_returns_identical_value(self, noop):
        first = noop.get_connection_stats()
        second = noop.get_connection_stats()
        assert first == second

    def test_repeated_acquire_release_sequence_remains_safe(self, noop):
        """A real PgAdmin would acquire → release; noop returns False both times."""
        assert noop.acquire_advisory_lock(7) is False
        assert noop.release_advisory_lock(7) is False
        assert noop.acquire_advisory_lock(7) is False
