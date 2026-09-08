"""Unit tests for the LocalFileLeaderElector (665 D5).

The local file-lock elector is the inline scheduler's default elector when
distributed leader election is disabled (the OSS/single-host default). It elects
exactly one process per host via a cross-platform non-blocking file lock so the
framework runs its scheduled jobs out-of-box without Redis or baldur_pro.

Scope:
- Side-effect-free construction (no file opened, no thread spawned).
- State-transition lifecycle (NOT_STARTED -> LEADER / FOLLOWER -> STOPPED).
- Two instances over one lock path: acquire / follower / failover-on-release.
- Idempotent start/stop.
- Per-service namespace resolution for the default lock path.

Cross-platform: the underlying ``audit/checkpoint/file_lock.py`` branches
msvcrt / fcntl, so these run on the Windows dev box and Linux CI alike.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.coordination.base import LeadershipState
from baldur.coordination.local_file_elector import (
    DEFAULT_RETRY_INTERVAL_SECONDS,
    LocalFileLeaderElector,
    _default_lock_path,
    _resolve_namespace,
    _sanitize,
)


@pytest.fixture
def elector_factory(tmp_path):
    """Build electors with a unique lock path, stopping them all in teardown.

    Every elector that ``start()``s must be ``stop()``ed to release the OS lock
    and join the retry thread; the factory tracks them so a test never leaks a
    held lock into the next one.
    """
    created: list[LocalFileLeaderElector] = []
    counter = {"n": 0}

    def _make(**kwargs) -> LocalFileLeaderElector:
        if "lock_path" not in kwargs:
            counter["n"] += 1
            kwargs["lock_path"] = tmp_path / f"elector-{counter['n']}.lock"
        elector = LocalFileLeaderElector(**kwargs)
        created.append(elector)
        return elector

    yield _make

    for elector in created:
        try:
            elector.stop()
        except Exception:
            pass


# =============================================================================
# Side-effect-free construction
# =============================================================================


class TestLocalFileLeaderElectorConstruction:
    """Construction must be inert — mirrors NoOpLeaderElector (665 D5)."""

    def test_construction_opens_no_lock_file(self, tmp_path):
        """Building the elector does not create the lock file."""
        lock_path = tmp_path / "scheduler.lock"

        LocalFileLeaderElector(lock_path=lock_path)

        assert not lock_path.exists()

    def test_construction_spawns_no_retry_thread(self, elector_factory):
        """No retry thread exists until start() is called on a contended lock."""
        elector = elector_factory()

        assert elector._retry_thread is None

    def test_construction_state_is_not_started(self, elector_factory):
        """A freshly built elector reports NOT_STARTED and is not leader."""
        elector = elector_factory()

        assert elector.state == LeadershipState.NOT_STARTED
        assert elector.is_leader() is False

    def test_default_retry_interval_constant(self, tmp_path):
        """The retry interval defaults to the module constant."""
        elector = LocalFileLeaderElector(lock_path=tmp_path / "s.lock")

        assert elector._retry_interval == DEFAULT_RETRY_INTERVAL_SECONDS


# =============================================================================
# State-transition lifecycle (single instance)
# =============================================================================


class TestLocalFileLeaderElectorLifecycle:
    """start() acquires the lock, stop() releases it (665 D5)."""

    def test_start_acquires_leadership(self, elector_factory):
        """An uncontended start() makes this process the leader."""
        elector = elector_factory()

        elector.start()

        assert elector.is_leader() is True
        assert elector.state == LeadershipState.LEADER

    def test_start_creates_the_lock_file(self, tmp_path):
        """The lock file is opened on start() (not before)."""
        lock_path = tmp_path / "scheduler.lock"
        elector = LocalFileLeaderElector(lock_path=lock_path)

        try:
            elector.start()
            assert lock_path.exists()
        finally:
            elector.stop()

    def test_start_bumps_fencing_token(self, elector_factory):
        """Acquiring leadership increments the fencing token to 1."""
        elector = elector_factory()

        elector.start()

        assert elector.get_fencing_token() == 1

    def test_leader_get_leader_returns_self_info(self, elector_factory):
        """get_leader() returns a self-attributed LeaderInfo while leader."""
        elector = elector_factory(resource_name="scheduler")

        elector.start()
        info = elector.get_leader()

        assert info is not None
        assert info.is_self is True
        assert info.fencing_token == 1
        assert "scheduler" in info.node_id

    def test_lease_valid_only_while_leader(self, elector_factory):
        """is_lease_valid() mirrors leadership (the held lock IS the lease)."""
        elector = elector_factory()

        assert elector.is_lease_valid() is False
        elector.start()
        assert elector.is_lease_valid() is True

    def test_stop_releases_leadership(self, elector_factory):
        """stop() releases the lock and transitions to STOPPED."""
        elector = elector_factory()
        elector.start()

        elector.stop()

        assert elector.is_leader() is False
        assert elector.state == LeadershipState.STOPPED

    def test_become_leader_callback_fires_on_acquire(self, elector_factory):
        """A registered on_become_leader callback runs when leadership is won."""
        elector = elector_factory()
        fired = threading.Event()
        elector.on_become_leader(fired.set)

        elector.start()

        assert fired.is_set()

    def test_lose_leader_callback_fires_on_stop(self, elector_factory):
        """on_lose_leader fires when a leader stops."""
        elector = elector_factory()
        lost = threading.Event()
        elector.on_lose_leader(lost.set)
        elector.start()

        elector.stop()

        assert lost.is_set()


# =============================================================================
# Idempotency
# =============================================================================


class TestLocalFileLeaderElectorIdempotency:
    """Double start/stop must be safe (665 D5)."""

    def test_double_start_keeps_single_token(self, elector_factory):
        """A second start() while already leader does not re-bump the token."""
        elector = elector_factory()

        elector.start()
        elector.start()

        assert elector.is_leader() is True
        assert elector.get_fencing_token() == 1

    def test_double_stop_is_safe(self, elector_factory):
        """Calling stop() twice raises nothing and stays released."""
        elector = elector_factory()
        elector.start()

        elector.stop()
        elector.stop()

        assert elector.is_leader() is False


# =============================================================================
# Two instances over one lock path
# =============================================================================


class TestLocalFileLeaderElectorContention:
    """One lock path elects exactly one leader; the other waits (665 D5)."""

    def test_second_instance_is_follower(self, tmp_path, elector_factory):
        """B over A's lock path stays a follower while A holds the lock."""
        lock_path = tmp_path / "shared-scheduler.lock"
        a = elector_factory(lock_path=lock_path)
        b = elector_factory(lock_path=lock_path, retry_interval_seconds=0.05)

        a.start()
        b.start()

        assert a.is_leader() is True
        assert b.is_leader() is False
        assert b.state == LeadershipState.FOLLOWER

    def test_follower_acquires_after_leader_releases(self, tmp_path, elector_factory):
        """When A releases, B's retry thread takes over (automatic failover)."""
        lock_path = tmp_path / "shared-scheduler.lock"
        a = elector_factory(lock_path=lock_path)
        b = elector_factory(lock_path=lock_path, retry_interval_seconds=0.05)

        acquired = threading.Event()
        b.on_become_leader(acquired.set)

        a.start()
        b.start()
        assert b.is_leader() is False

        # When the leader releases the OS lock, the follower's retry thread
        # re-attempts and wins — the callback fires on failover.
        a.stop()

        assert acquired.wait(timeout=5.0), "follower did not acquire after release"
        assert b.is_leader() is True


# =============================================================================
# Namespace resolution for the default lock path
# =============================================================================


class TestLocalFileLeaderElectorNamespace:
    """_resolve_namespace / _default_lock_path keep distinct services apart."""

    def test_otel_service_name_used_when_set(self):
        """A non-default OTEL service_name becomes the (sanitized) namespace."""
        fake_settings = MagicMock()
        fake_settings.service_name = "payments-api"

        with patch(
            "baldur.settings.otel.get_otel_settings", return_value=fake_settings
        ):
            assert _resolve_namespace() == "payments-api"

    def test_default_service_name_falls_back_to_token(self):
        """The default service_name ('baldur') falls back to a stable token."""
        fake_settings = MagicMock()
        fake_settings.service_name = "baldur"

        with patch(
            "baldur.settings.otel.get_otel_settings", return_value=fake_settings
        ):
            ns = _resolve_namespace()

        # Not the literal default name — a 12-char entrypoint/cwd-derived hash.
        assert ns != "baldur"
        assert len(ns) == 12

    def test_namespace_falls_back_when_settings_unavailable(self):
        """A settings failure falls back to the derived token, never raising."""
        with patch(
            "baldur.settings.otel.get_otel_settings",
            side_effect=RuntimeError("settings down"),
        ):
            ns = _resolve_namespace()

        assert len(ns) == 12

    def test_default_lock_path_includes_resource_and_namespace(self):
        """The default lock path carries the resource name and namespace."""
        with patch(
            "baldur.coordination.local_file_elector._resolve_namespace",
            return_value="svc1",
        ):
            path = _default_lock_path("scheduler")

        assert path.name == "baldur-scheduler-svc1.lock"

    def test_sanitize_strips_unsafe_characters(self):
        """_sanitize keeps alnum/-/_ and replaces the rest."""
        assert _sanitize("a/b c:d") == "a_b_c_d"


# =============================================================================
# Failover retry-thread crash visibility
# =============================================================================


class TestLocalFileLeaderElectorRetryCrashVisibility:
    """A crash of the failover retry thread is logged before the thread dies."""

    def test_retry_thread_target_is_the_crash_capturing_wrapper(
        self, tmp_path, elector_factory
    ):
        """The spawned thread runs the wrapper, not the bare loop.

        Spawning the bare loop is what made a crash invisible: the exception
        would reach threading's default hook with no elector context.
        """
        holder = elector_factory(lock_path=tmp_path / "shared.lock")
        holder.start()
        assert holder.is_leader() is True

        follower = elector_factory(lock_path=tmp_path / "shared.lock")
        with patch.object(follower, "_retry_loop_with_crash_capture") as wrapper:
            follower.start()
            assert follower._retry_thread is not None
            follower._retry_thread.join(timeout=2.0)

        wrapper.assert_called_once_with()

    def test_crash_is_logged_with_resource_and_lock_path(self, elector_factory):
        """An exception escaping the loop is logged at ERROR, then re-raised."""
        elector = elector_factory(resource_name="sched-a")
        boom = RuntimeError("lock backend exploded")

        with (
            patch.object(elector, "_retry_loop", side_effect=boom),
            patch(
                "baldur.coordination.local_file_elector.logger.exception"
            ) as log_exception,
            pytest.raises(RuntimeError, match="lock backend exploded"),
        ):
            elector._retry_loop_with_crash_capture()

        log_exception.assert_called_once()
        event, kwargs = log_exception.call_args[0][0], log_exception.call_args[1]
        assert event == "local_file_elector.retry_loop_error"
        assert kwargs["resource"] == "sched-a"
        assert kwargs["error"] is boom
        assert "lock_path" in kwargs

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
    def test_interpreter_shutdown_signals_are_not_logged_as_crashes(
        self, exc_type, elector_factory
    ):
        """KeyboardInterrupt / SystemExit propagate untouched.

        They mean the interpreter is going down, not that failover broke.
        """
        elector = elector_factory()

        with (
            patch.object(elector, "_retry_loop", side_effect=exc_type()),
            patch(
                "baldur.coordination.local_file_elector.logger.exception"
            ) as log_exception,
            pytest.raises(exc_type),
        ):
            elector._retry_loop_with_crash_capture()

        log_exception.assert_not_called()

    def test_clean_completion_logs_nothing(self, elector_factory):
        """Winning the lock ends the loop normally — no crash record.

        The loop terminating is the success path here, unlike every registered
        daemon worker, which is why this thread is not in the worker registry.
        """
        elector = elector_factory()

        with (
            patch.object(elector, "_retry_loop", return_value=None),
            patch(
                "baldur.coordination.local_file_elector.logger.exception"
            ) as log_exception,
        ):
            elector._retry_loop_with_crash_capture()

        log_exception.assert_not_called()
