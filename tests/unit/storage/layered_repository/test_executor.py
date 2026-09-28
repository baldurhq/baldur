"""478 D3 — LayeredRepositoryBase ThreadPoolExecutor sizing.

Pre-D3: hardcoded `max_workers=4`. Under N-instance × M-thread burst the
class-level pool saturated and `try_acquire_half_open_slot` futures timed
out, breaking the cluster-wide HALF_OPEN cap.

Post-D3: pool size driven by `BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS` env
(default 16, ge=1, le=64). Startup-only — `ThreadPoolExecutor` is fixed-
size at construction. Tests use env-var fixtures + reset_l2_storage_settings
+ reset_layered_repository_executor to recreate the pool.

The pool is also fork-aware: a ``ThreadPoolExecutor`` inherited across
``fork()`` has no live threads, so each pool records the pid that built it and
a process that did not build it drops it and builds its own. The exits of
``_get_executor()`` are driven here by stamping a pool with another pid rather
than by forking; the real-fork composition lives in the integration suite.
The package conftest shuts the class-level pool down around every test.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.layered_repository import base as base_module
from baldur.adapters.memory.layered_repository.base import (
    _POOL_OWNER_PID_ATTR,
    LayeredRepositoryBase,
)


class TestExecutorMaxWorkers:
    """LayeredRepositoryBase._executor pool size — env-driven (478 D3)."""

    def test_default_max_workers_is_16(self, monkeypatch):
        """Env unset → default 16."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.adapters.memory.layered_repository import (
            reset_layered_repository_executor,
        )
        from baldur.settings.l2_storage import reset_l2_storage_settings

        monkeypatch.delenv("BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS", raising=False)
        reset_l2_storage_settings()
        reset_layered_repository_executor()

        try:
            LayeredCircuitBreakerStateRepository(l2_repo=None)
            executor = LayeredRepositoryBase._get_executor()
            assert executor._max_workers == 16
        finally:
            reset_layered_repository_executor()
            reset_l2_storage_settings()

    def test_env_override_max_workers(self, monkeypatch):
        """Env set → override applied."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.adapters.memory.layered_repository import (
            reset_layered_repository_executor,
        )
        from baldur.settings.l2_storage import reset_l2_storage_settings

        monkeypatch.setenv("BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS", "32")
        reset_l2_storage_settings()
        reset_layered_repository_executor()

        try:
            LayeredCircuitBreakerStateRepository(l2_repo=None)
            executor = LayeredRepositoryBase._get_executor()
            assert executor._max_workers == 32
        finally:
            monkeypatch.delenv("BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS", raising=False)
            reset_layered_repository_executor()
            reset_l2_storage_settings()


def _stamp(pool: ThreadPoolExecutor, pid: object) -> ThreadPoolExecutor:
    """Record ``pid`` as the pool's builder, the way ``_get_executor`` does."""
    vars(pool)[_POOL_OWNER_PID_ATTR] = pid
    return pool


def _another_pid() -> int:
    return os.getpid() + 1


class _PeerBuildsWhileUnlocked:
    """``_executor_lock`` stand-in: a peer thread publishes its own pool in the
    gap between the caller's drop and the caller's build.

    ``_get_executor`` releases the lock after dropping a foreign pool and takes
    it again to build; the first release is where a real peer can slip in.
    """

    def __init__(self, inner, peer_pool: ThreadPoolExecutor) -> None:
        self._inner = inner
        self._peer_pool = peer_pool
        self.releases = 0

    def __enter__(self) -> _PeerBuildsWhileUnlocked:
        self._inner.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._inner.release()
        self.releases += 1
        if self.releases == 1:
            LayeredRepositoryBase._executor = self._peer_pool


class TestExecutorForkOwnershipBehavior:
    """``_get_executor()`` keeps a pool this process built and replaces one it
    inherited — exits E1-E5 of the fork-ownership check.
    """

    def test_built_pool_is_stamped_and_returned_again(self):
        """E4 then E1: the first call builds and stamps; the next returns it."""
        first = LayeredRepositoryBase._get_executor()
        second = LayeredRepositoryBase._get_executor()

        assert second is first
        assert vars(first)[_POOL_OWNER_PID_ATTR] == os.getpid()

    def test_pool_is_stamped_before_it_is_published(self, monkeypatch):
        """A fork between publication and stamping would leave an unstamped
        dead pool that no child ever drops — so the stamp's pid is read while
        the class attribute is still empty.
        """
        # Given — the stamp's pid read records what other threads can see
        seen_at_stamp: list[object] = []
        real_getpid = os.getpid

        def getpid() -> int:
            seen_at_stamp.append(LayeredRepositoryBase._executor)
            return real_getpid()

        monkeypatch.setattr(base_module, "os", SimpleNamespace(getpid=getpid))

        # When
        pool = LayeredRepositoryBase._get_executor()

        # Then
        assert seen_at_stamp == [None]
        assert vars(pool)[_POOL_OWNER_PID_ATTR] == real_getpid()

    @pytest.mark.parametrize(
        "make_pool",
        [
            lambda: ThreadPoolExecutor(max_workers=1),
            lambda: _stamp(ThreadPoolExecutor(max_workers=1), str(_another_pid())),
            lambda: MagicMock(spec=ThreadPoolExecutor),
        ],
        ids=["assigned_pool", "non_int_owner", "mock_pool"],
    )
    def test_pool_without_an_owner_pid_is_returned_as_is(self, make_pool):
        """E2: a pool assigned from outside (tests do) records no owner, and a
        double that answers every attribute must not read as foreign.
        """
        injected = make_pool()
        LayeredRepositoryBase._executor = injected

        assert LayeredRepositoryBase._get_executor() is injected

    def test_pool_built_by_another_process_is_replaced_by_a_working_one(self):
        """E3: the inherited pool is dropped, never shut down, and the new one
        runs what is submitted to it.
        """
        # Given — a pool a fork parent built
        inherited = _stamp(ThreadPoolExecutor(max_workers=1), _another_pid())
        LayeredRepositoryBase._executor = inherited

        try:
            # When
            replacement = LayeredRepositoryBase._get_executor()

            # Then
            assert replacement is not inherited
            assert LayeredRepositoryBase._executor is replacement
            assert vars(replacement)[_POOL_OWNER_PID_ATTR] == os.getpid()
            assert replacement.submit(lambda: 42).result(timeout=5) == 42
            # Its shutdown lock and wake-up sentinel belong to dead threads.
            assert inherited._shutdown is False
        finally:
            inherited.shutdown(wait=False)

    def test_losing_the_build_race_returns_the_peers_pool(self, monkeypatch):
        """E3 with a peer that rebuilds first: the caller returns the peer's
        pool — never ``None``, never a second pool of its own.
        """
        # Given
        inherited = _stamp(ThreadPoolExecutor(max_workers=1), _another_pid())
        peer_pool = _stamp(ThreadPoolExecutor(max_workers=1), os.getpid())
        LayeredRepositoryBase._executor = inherited
        lock = _PeerBuildsWhileUnlocked(LayeredRepositoryBase._executor_lock, peer_pool)
        monkeypatch.setattr(LayeredRepositoryBase, "_executor_lock", lock)

        try:
            with patch.object(
                base_module, "ThreadPoolExecutor", wraps=ThreadPoolExecutor
            ) as constructor:
                # When
                result = LayeredRepositoryBase._get_executor()

            # Then
            assert result is peer_pool
            assert lock.releases == 2
            constructor.assert_not_called()
        finally:
            inherited.shutdown(wait=False)

    def test_unreadable_settings_fall_back_to_the_settings_default_pool_size(self):
        """E5: a settings failure still yields a pool, sized like the default."""
        from baldur.settings.l2_storage import L2StorageSettings

        default_size = L2StorageSettings.model_fields["executor_max_workers"].default

        with patch(
            "baldur.settings.l2_storage.get_l2_storage_settings",
            side_effect=RuntimeError("settings unavailable"),
        ):
            pool = LayeredRepositoryBase._get_executor()

        assert pool._max_workers == default_size
