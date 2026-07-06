"""``CleanupService`` lifecycle hygiene tests (484 D5 / D7).

Covers two cleanup-service additions from 484:

D5 — ``CleanupService.cleanup_stale_cb_keys()``
    Thin caller: defaults ``retention_days`` from settings, delegates to the
    CB repo via ``ProviderRegistry.get_circuit_breaker_repo()``, wraps the
    deleted count in a ``CleanupResult``, and emits
    ``log_system_control_audit(action="cleanup_stale_cb_keys")``. On any
    exception inside the try-block the method returns a failed
    ``CleanupResult`` rather than propagating.

D7 — ``CleanupService.cleanup_memory_cache_expired()``
    Iterates ``InMemoryCacheAdapter._instances`` (a ``WeakSet``) and calls
    each instance's lock-acquiring ``cleanup_expired()``. Per-instance
    failures are logged and skipped — a single misbehaving adapter must
    not abort the sweep across the rest. The total count is the sum across
    every successful instance.

References:
- ``484`` D5 / D7
- ``src/baldur/services/cleanup_service.py``
- ``src/baldur/adapters/cache/memory_adapter.py``
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.services.cleanup_service import CleanupResult, CleanupService


@pytest.fixture
def service() -> CleanupService:
    """A fresh CleanupService — the class is stateless, so this is cheap."""
    return CleanupService()


@pytest.fixture
def isolated_memory_registry():
    """Snapshot ``InMemoryCacheAdapter._instances`` and restore on teardown.

    The class-level WeakSet is shared global state; without isolation, an
    earlier test's adapter (held alive by a fixture) would leak into the
    fan-out assertions below.
    """
    saved = list(InMemoryCacheAdapter._instances)
    InMemoryCacheAdapter.clear_all_instances()
    try:
        yield
    finally:
        InMemoryCacheAdapter.clear_all_instances()
        for instance in saved:
            InMemoryCacheAdapter._instances.add(instance)


# =============================================================================
# D5: CleanupService.cleanup_stale_cb_keys
# =============================================================================


class TestCleanupStaleCBKeysBehavior:
    """484 D5: ``cleanup_stale_cb_keys`` orchestration + audit + fail-safe."""

    def test_delegates_to_cb_repo_with_explicit_retention(self, service):
        """Explicit ``retention_days`` is forwarded verbatim to the repo."""
        repo = MagicMock()
        repo.cleanup_stale_keys.return_value = 4

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                return_value=repo,
            ),
            patch("baldur.services.cleanup_service.log_system_control_audit"),
        ):
            result = service.cleanup_stale_cb_keys(retention_days=14)

        repo.cleanup_stale_keys.assert_called_once_with(retention_days=14)
        assert isinstance(result, CleanupResult)
        assert result.success is True
        assert result.operation == "deleted"
        assert result.count == 4
        assert result.details == {"retention_days": 14}

    def test_default_retention_loaded_from_settings(self, service):
        """``retention_days=None`` falls back to ``CleanupSettings.cb_stale_key_retention_days``."""
        repo = MagicMock()
        repo.cleanup_stale_keys.return_value = 0

        fake_settings = MagicMock()
        fake_settings.cb_stale_key_retention_days = 45

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                return_value=repo,
            ),
            patch(
                "baldur.settings.cleanup.get_cleanup_settings",
                return_value=fake_settings,
            ),
            patch("baldur.services.cleanup_service.log_system_control_audit"),
        ):
            result = service.cleanup_stale_cb_keys(retention_days=None)

        repo.cleanup_stale_keys.assert_called_once_with(retention_days=45)
        assert result.success is True
        assert result.details == {"retention_days": 45}

    def test_emits_system_control_audit_with_old_and_new_state(self, service):
        """Audit log records action, actor, deleted count delta, reason."""
        repo = MagicMock()
        repo.cleanup_stale_keys.return_value = 7

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                return_value=repo,
            ),
            patch(
                "baldur.services.cleanup_service.log_system_control_audit"
            ) as mock_audit,
        ):
            service.cleanup_stale_cb_keys(retention_days=30)

        mock_audit.assert_called_once()
        kwargs = mock_audit.call_args.kwargs
        assert kwargs["action"] == "cleanup_stale_cb_keys"
        assert kwargs["actor"] == "system"
        assert kwargs["old_state"] == {"deleted_count": 0}
        assert kwargs["new_state"] == {"deleted_count": 7}
        assert "30" in kwargs["reason"]

    def test_repo_exception_returns_failed_result(self, service):
        """Repo failure is swallowed → ``CleanupResult.success=False``."""
        repo = MagicMock()
        repo.cleanup_stale_keys.side_effect = ConnectionError("redis down")

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                return_value=repo,
            ),
            patch("baldur.services.cleanup_service.log_system_control_audit"),
        ):
            result = service.cleanup_stale_cb_keys(retention_days=30)

        assert isinstance(result, CleanupResult)
        assert result.success is False
        assert result.operation == "deleted"
        assert result.count == 0
        assert "redis down" in result.error
        assert result.details == {"retention_days": 30}

    def test_failed_path_does_not_emit_audit(self, service):
        """No audit event is emitted on the failure path."""
        repo = MagicMock()
        repo.cleanup_stale_keys.side_effect = RuntimeError("oops")

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                return_value=repo,
            ),
            patch(
                "baldur.services.cleanup_service.log_system_control_audit"
            ) as mock_audit,
        ):
            service.cleanup_stale_cb_keys(retention_days=30)

        mock_audit.assert_not_called()

    def test_zero_deleted_still_succeeds(self, service):
        """Empty cleanup is a normal success outcome (idempotent run)."""
        repo = MagicMock()
        repo.cleanup_stale_keys.return_value = 0

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                return_value=repo,
            ),
            patch("baldur.services.cleanup_service.log_system_control_audit"),
        ):
            result = service.cleanup_stale_cb_keys(retention_days=30)

        assert result.success is True
        assert result.count == 0


# =============================================================================
# D7: CleanupService.cleanup_memory_cache_expired
# =============================================================================


class TestCleanupMemoryCacheExpiredBehavior:
    """484 D7: fan-out across the ``InMemoryCacheAdapter._instances`` WeakSet."""

    def _make_adapter_with_expired_entries(
        self,
        prefix: str,
        expired_count: int,
        fresh_count: int,
    ) -> InMemoryCacheAdapter:
        """Build an adapter pre-populated with N expired + M fresh entries.

        Bypasses the public ``set()`` API so we can write past expiry
        timestamps directly — ``set()`` only accepts forward TTLs.
        """
        from baldur.adapters.cache.memory_adapter import CacheEntry

        adapter = InMemoryCacheAdapter(key_prefix=prefix)
        now = time.time()

        for i in range(expired_count):
            adapter._store[f"{prefix}stale_{i}"] = CacheEntry(
                value=f"v{i}",
                expires_at=now - 60,  # 1 minute ago
            )
        for i in range(fresh_count):
            # Use the public set() with a generous TTL for fresh entries.
            adapter.set(f"fresh_{i}", f"v{i}", ttl=timedelta(minutes=30))

        return adapter

    def test_no_live_instances_returns_zero(self, service, isolated_memory_registry):
        """Empty registry → success, count=0, instance_count=0."""
        result = service.cleanup_memory_cache_expired()

        assert result.success is True
        assert result.operation == "removed"
        assert result.count == 0
        assert result.details == {"instance_count": 0}

    def test_fan_out_sums_counts_across_instances(
        self, service, isolated_memory_registry
    ):
        """Total count = sum of removals across every live adapter."""
        a = self._make_adapter_with_expired_entries(
            "a:", expired_count=3, fresh_count=2
        )
        b = self._make_adapter_with_expired_entries(
            "b:", expired_count=5, fresh_count=4
        )

        result = service.cleanup_memory_cache_expired()

        assert result.success is True
        assert result.count == 3 + 5  # only expired
        assert result.details == {"instance_count": 2}

        # Fresh entries survive in both adapters.
        assert a.get_store_size() == 2
        assert b.get_store_size() == 4

    def test_fresh_entries_are_not_removed(self, service, isolated_memory_registry):
        """Adapters with no expired entries contribute zero to the total."""
        adapter = self._make_adapter_with_expired_entries(
            "fresh:", expired_count=0, fresh_count=5
        )

        result = service.cleanup_memory_cache_expired()

        assert result.success is True
        assert result.count == 0
        assert adapter.get_store_size() == 5

    def test_per_instance_exception_does_not_abort_sweep(
        self, service, isolated_memory_registry
    ):
        """A failing adapter is logged-and-skipped; others still cleaned up."""
        bad = InMemoryCacheAdapter(key_prefix="bad:")
        good = self._make_adapter_with_expired_entries(
            "good:", expired_count=4, fresh_count=1
        )

        with patch.object(
            bad,
            "cleanup_expired",
            side_effect=RuntimeError("lock acquire failed"),
        ):
            result = service.cleanup_memory_cache_expired()

        # Good adapter's removals still counted; bad adapter contributes 0.
        assert result.success is True
        assert result.count == 4
        assert result.details == {"instance_count": 2}
        assert good.get_store_size() == 1

    def test_outer_exception_returns_failed_result(
        self, service, isolated_memory_registry
    ):
        """If the WeakSet snapshot itself raises, return a failed result."""

        class ExplodingProxy:
            def __iter__(self):
                raise RuntimeError("registry corrupt")

        with patch(
            "baldur.adapters.cache.memory_adapter.InMemoryCacheAdapter._instances",
            new=ExplodingProxy(),
        ):
            result = service.cleanup_memory_cache_expired()

        assert result.success is False
        assert result.operation == "removed"
        assert "registry corrupt" in result.error

    def test_calls_lock_acquiring_public_method_not_underscore_helper(
        self, service, isolated_memory_registry
    ):
        """Service must invoke ``cleanup_expired()`` (locks), not ``_cleanup_expired()``.

        ``self._lock`` is a non-reentrant ``threading.Lock``; calling the
        underscore helper from outside a held-lock context would deadlock
        on the next public-method use.
        """
        adapter = InMemoryCacheAdapter(key_prefix="lock:")

        with (
            patch.object(adapter, "cleanup_expired", return_value=2) as mock_public,
            patch.object(adapter, "_cleanup_expired", return_value=999) as mock_private,
        ):
            result = service.cleanup_memory_cache_expired()

        mock_public.assert_called_once_with()
        mock_private.assert_not_called()
        assert result.count == 2

    def test_idempotent_second_call_is_zero(self, service, isolated_memory_registry):
        """Running cleanup twice in a row removes everything once and stays at 0."""
        # Bind the adapter so the WeakSet entry survives until the second call.
        adapter = self._make_adapter_with_expired_entries(
            "idem:", expired_count=6, fresh_count=0
        )

        first = service.cleanup_memory_cache_expired()
        second = service.cleanup_memory_cache_expired()

        assert first.count == 6
        assert second.count == 0
        assert second.success is True
        assert adapter.get_store_size() == 0
