"""
Unit tests for ResilientStorageBackend lazy degraded-mode recovery
loop (#470 D1+D2+D3+D4+D5).

Phase 1 of #470: ``_ensure_redis()`` gains a 3-way branch
(REDIS / DEGRADED / uninitialized) so a worker that reverted to
DEGRADED after a Redis blip will dispatch a background recovery
thread the next time the hot path calls in. The dispatch is
cooldown-gated by ``recovery_probe_interval`` (5s default), serialized
by a non-reentrant ``_recovery_lock``, and the daemon thread runs
``check_and_recover()`` (ping pre-check + jitter + ``_do_recovery()``).

Coverage:
- ``_ensure_redis`` 3-way branch (state x cooldown matrix)
- ``_maybe_dispatch_recovery`` lock try-acquire concurrency, cooldown
  gating, ``auto_recovery=False`` kill switch, daemon-thread spawning
- ``_run_recovery_payload`` success path resets ``_degraded_critical_logged``,
  failure path keeps mode rolled back, lock release in ``finally``
- ``_do_recovery`` rollback on exception, ``mode="runtime"`` flows to WAL
- Drive-by event renames (G9): no ``watchdog.recovery_failed`` emission
"""

from __future__ import annotations

import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_redis_negative_cache():
    """Clear the runtime Redis negative cache so ``_ensure_redis()``
    proceeds to the connection branches under test.
    """
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    prev = (state.unavailable, state.fail_time)
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable, state.fail_time = prev


@pytest.fixture
def temp_wal_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def settings(temp_wal_dir):
    """Memory-only settings — keeps the backend out of real Redis."""
    from baldur.settings.resilient_storage import ResilientStorageSettings

    return ResilientStorageSettings(
        wal_dir=temp_wal_dir,
        allow_memory_only=True,
    )


@pytest.fixture
def backend(settings):
    """Backend in DEGRADED mode (no Redis call yet)."""
    from baldur.adapters.resilient.backend import ResilientStorageBackend

    b = ResilientStorageBackend(settings)
    yield b
    b.close()


# =============================================================================
# D1: _ensure_redis() 3-way branch
# =============================================================================


class TestEnsureRedisThreeWayBranchBehavior:
    """``_ensure_redis()`` selects exactly one of three branches based
    on (``_redis_initialized``, ``_mode``).

    | _redis_initialized | _mode    | Branch                        |
    | True               | REDIS    | fast-path return True         |
    | True               | DEGRADED | dispatch recovery, return False|
    | False              | DEGRADED | first-init lazy probe         |
    """

    def test_redis_mode_fast_path_returns_true(self, backend):
        """Fast path: already-connected backend returns True
        immediately, never dispatches recovery.
        """
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.REDIS

        with patch.object(backend, "_maybe_dispatch_recovery") as mock_disp:
            assert backend._ensure_redis() is True

        mock_disp.assert_not_called()

    def test_degraded_branch_dispatches_recovery_and_returns_false(self, backend):
        """Degraded re-entry path: ``_ensure_redis()`` calls
        ``_maybe_dispatch_recovery()`` and returns False so the caller
        falls through to the degraded read path.
        """
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.DEGRADED

        with patch.object(backend, "_maybe_dispatch_recovery") as mock_disp:
            assert backend._ensure_redis() is False

        mock_disp.assert_called_once()

    def test_uninitialized_branch_runs_first_init_probe(self, backend):
        """Uninitialized path: still probes Redis (first-init lazy
        connect from #437) and never dispatches degraded recovery —
        recovery only applies to once-connected backends.
        """
        with patch.object(backend, "_maybe_dispatch_recovery") as mock_disp:
            with patch("baldur.adapters.cache.RedisCacheAdapter") as Mock:
                mock_instance = MagicMock()
                mock_instance._redis.ping.return_value = True
                Mock.return_value = mock_instance

                result = backend._ensure_redis()

        assert result is True
        mock_disp.assert_not_called()

    def test_recovering_mode_returns_false_without_redispatch(self, backend):
        """Implicit fourth state per design doc: when ``_do_recovery``
        is mid-flight (mode=RECOVERING), the branch is neither REDIS
        fast-path nor DEGRADED dispatch. The first-init path is taken,
        but the negative cache + cooldown discard it without I/O.
        """
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.RECOVERING
        # Cooldown set so first-init branch returns False fast.
        backend._next_redis_probe = time.monotonic() + 999.0

        with patch.object(backend, "_maybe_dispatch_recovery") as mock_disp:
            assert backend._ensure_redis() is False

        mock_disp.assert_not_called()


# =============================================================================
# D2 + D3: _maybe_dispatch_recovery (cooldown + lock + thread spawn)
# =============================================================================


class TestRecoveryDispatchBehavior:
    """``_maybe_dispatch_recovery`` is the choke point. It must:

    - Honor the ``auto_recovery=False`` kill switch.
    - Skip when ``recovery_probe_interval`` cooldown is still active.
    - Try-acquire ``_recovery_lock`` non-blocking; only the winner
      spawns a thread.
    - Set the next-probe deadline BEFORE spawning so concurrent
      callers see "cooldown active" instead of contending.
    """

    def test_auto_recovery_false_short_circuits(self, settings):
        """Operator kill switch: ``auto_recovery=False`` skips
        dispatch entirely — no thread, no lock acquisition.
        """
        from baldur.adapters.resilient.backend import ResilientStorageBackend
        from baldur.settings.resilient_storage import ResilientStorageSettings

        kill_switch_settings = ResilientStorageSettings(
            wal_dir=settings.wal_dir,
            allow_memory_only=True,
            auto_recovery=False,
        )
        backend = ResilientStorageBackend(kill_switch_settings)
        try:
            with patch(
                "baldur.adapters.resilient.backend.threading.Thread"
            ) as mock_thread:
                backend._maybe_dispatch_recovery()
            mock_thread.assert_not_called()
            # Lock must remain available — kill switch returned before
            # try-acquire.
            assert backend._recovery_lock.acquire(blocking=False) is True
            backend._recovery_lock.release()
        finally:
            backend.close()

    def test_cooldown_skips_dispatch(self, backend):
        """Within ``recovery_probe_interval`` of the previous dispatch,
        no thread is spawned.
        """
        backend._next_redis_probe = time.monotonic() + 999.0

        with patch("baldur.adapters.resilient.backend.threading.Thread") as mock_thread:
            backend._maybe_dispatch_recovery()

        mock_thread.assert_not_called()
        # Lock untouched.
        assert backend._recovery_lock.acquire(blocking=False) is True
        backend._recovery_lock.release()

    def test_lock_contention_skips_thread_spawn(self, backend):
        """Lock try-acquire serializes dispatch — when an in-flight
        recovery already holds ``_recovery_lock``, the dispatcher
        observes ``recovery_skipped`` and returns without spawning.

        Holding the lock from the test thread simulates the in-flight
        case more reliably than racing real threads. We patch only the
        production-side ``threading.Thread`` reference so the test's
        own threading APIs are unaffected.
        """
        backend._next_redis_probe = 0.0
        backend._recovery_lock.acquire()

        try:
            with patch(
                "baldur.adapters.resilient.backend.threading.Thread"
            ) as mock_thread_cls:
                backend._maybe_dispatch_recovery()
                assert mock_thread_cls.call_count == 0
        finally:
            backend._recovery_lock.release()

    def test_lock_available_dispatches_exactly_one_thread(self, backend):
        """Inverse of the contention case: when the lock is free, a
        single dispatch call constructs exactly one Thread.
        """
        backend._next_redis_probe = 0.0

        with patch(
            "baldur.adapters.resilient.backend.threading.Thread"
        ) as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()
            backend._maybe_dispatch_recovery()

        assert mock_thread_cls.call_count == 1
        # Release the lock the dispatcher acquired — in production the
        # daemon thread's finally would do this.
        backend._recovery_lock.release()

    def test_dispatch_sets_cooldown_before_spawn(self, backend):
        """The next-probe deadline is set BEFORE the thread spawns so
        the next caller sees ``recovery_cooldown_active`` rather than
        attempting another lock try-acquire (race-window closure).
        """
        backend._next_redis_probe = 0.0

        captured = {}

        def fake_thread(*args, **kwargs):
            captured["next_probe_at_spawn"] = backend._next_redis_probe
            mock_t = MagicMock()
            mock_t.start = lambda: None
            return mock_t

        with patch(
            "baldur.adapters.resilient.backend.threading.Thread",
            side_effect=fake_thread,
        ):
            before = time.monotonic()
            backend._maybe_dispatch_recovery()

        # Cooldown was set BEFORE Thread() was constructed — hence
        # captured value is already > before + ~5s.
        assert "next_probe_at_spawn" in captured
        assert captured["next_probe_at_spawn"] >= before + 4.0

    def test_thread_is_daemon_with_pid_in_name(self, backend):
        """py-spy / log traceability: thread name embeds the PID, and
        ``daemon=True`` so process exit is not blocked.
        """
        import os

        backend._next_redis_probe = 0.0

        captured = {}

        def fake_thread_class(*args, **kwargs):
            captured["name"] = kwargs.get("name", "")
            captured["daemon"] = kwargs.get("daemon", False)
            mock_t = MagicMock()
            mock_t.start = lambda: None
            return mock_t

        with patch(
            "baldur.adapters.resilient.backend.threading.Thread",
            side_effect=fake_thread_class,
        ):
            backend._maybe_dispatch_recovery()
            # Release lock for fixture cleanup.
            backend._recovery_lock.release()

        assert captured["daemon"] is True
        assert str(os.getpid()) in captured["name"]
        assert "baldur-resilient-recovery" in captured["name"]


# =============================================================================
# D4 + D5: _run_recovery_payload (success/failure paths)
# =============================================================================


class TestRecoveryPayloadBehavior:
    """``_run_recovery_payload`` wraps ``check_and_recover()`` with the
    success/failure post-processing. The function is the daemon
    thread's entry point — running it synchronously isolates the
    behavior from thread-scheduling flake.
    """

    def test_success_resets_degraded_critical_logged_flag(self, backend):
        """D5: ``_degraded_critical_logged`` resets on success so
        recurring flapping logs CRITICAL again — operator visibility
        stays intact across multiple Redis blips.
        """
        backend._degraded_critical_logged = True
        backend._recovery_lock.acquire()  # Simulate dispatch precondition.

        with patch.object(backend, "check_and_recover", return_value=True):
            backend._run_recovery_payload()

        assert backend._degraded_critical_logged is False

    def test_failure_keeps_flag_unchanged(self, backend):
        """When ``check_and_recover()`` returns False (Redis still
        down), the flag must NOT reset — there was no successful
        recovery edge.
        """
        backend._degraded_critical_logged = True
        backend._recovery_lock.acquire()

        with patch.object(backend, "check_and_recover", return_value=False):
            backend._run_recovery_payload()

        assert backend._degraded_critical_logged is True

    def test_lock_released_on_success(self, backend):
        """``_recovery_lock`` is released in ``finally`` — next probe
        window can dispatch a fresh recovery.
        """
        backend._recovery_lock.acquire()

        with patch.object(backend, "check_and_recover", return_value=True):
            backend._run_recovery_payload()

        # Verify by attempting non-blocking acquire — must succeed
        # because the payload released it.
        assert backend._recovery_lock.acquire(blocking=False) is True
        backend._recovery_lock.release()

    def test_lock_released_on_exception(self, backend):
        """Exception escape path must still release the lock —
        otherwise a single crash strands the recovery channel.
        """
        backend._recovery_lock.acquire()

        with patch.object(
            backend,
            "check_and_recover",
            side_effect=RuntimeError("boom"),
        ):
            # Exception is swallowed by the payload's bare except.
            backend._run_recovery_payload()

        assert backend._recovery_lock.acquire(blocking=False) is True
        backend._recovery_lock.release()

    def test_success_logs_recovery_succeeded_event(self, backend):
        """LOGGING_STANDARDS §Suffix mapping: ``_succeeded`` is INFO
        per the dual standard (state transition).
        """
        backend._recovery_lock.acquire()

        with patch.object(backend, "check_and_recover", return_value=True):
            with patch("baldur.adapters.resilient.backend.logger") as mock_logger:
                backend._run_recovery_payload()

        info_events = [
            call.args[0] for call in mock_logger.info.call_args_list if call.args
        ]
        assert "resilient_storage.recovery_succeeded" in info_events

    def test_failure_logs_recovery_failed_event_at_warning(self, backend):
        """LOGGING_STANDARDS §Suffix: ``_failed`` is WARNING (not
        ERROR) — recoverable; next probe will retry.
        """
        backend._recovery_lock.acquire()

        with patch.object(backend, "check_and_recover", return_value=False):
            with patch("baldur.adapters.resilient.backend.logger") as mock_logger:
                backend._run_recovery_payload()

        warning_events = [
            call.args[0] for call in mock_logger.warning.call_args_list if call.args
        ]
        assert "resilient_storage.recovery_failed" in warning_events


# =============================================================================
# D4: _do_recovery rollback + mode="runtime" propagation
# =============================================================================


class TestDoRecoveryBehavior:
    """``_do_recovery`` is the substantive recovery payload (WAL
    replay + memory sync + cleanup). Phase 1 changes: mode rollback
    on exception, ``mode="runtime"`` propagation to WAL.
    """

    def test_runtime_mode_passed_to_wal_recover_unprocessed(self, backend):
        """G3/G4 surface: ``_do_recovery()`` must call
        ``recover_unprocessed`` with ``mode="runtime"`` so peer
        workers' WAL files are not over-replayed.

        Two-phase recovery (#539 D1) calls ``recover_unprocessed`` TWICE —
        a lock-free bulk replay and a locked delta-replay finalize — both
        with ``mode="runtime"``.
        """
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._mode = ResilientStorageMode.DEGRADED
        backend._wal = MagicMock()
        backend._wal.recover_unprocessed.return_value = []
        backend._wal_initialized = True

        with patch.object(backend, "_sync_memory_to_redis"):
            backend._do_recovery()

        assert backend._wal.recover_unprocessed.call_count == 2
        for call in backend._wal.recover_unprocessed.call_args_list:
            assert call.kwargs.get("mode") == "runtime"

    def test_runtime_mode_passed_to_wal_cleanup_processed(self, backend):
        """G3 (data loss): ``cleanup_processed`` also receives
        ``mode="runtime"`` so a peer's still-active WAL file isn't
        deleted by this worker's recovery thread.
        """
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._mode = ResilientStorageMode.DEGRADED
        backend._wal = MagicMock()
        backend._wal.recover_unprocessed.return_value = []
        backend._wal_initialized = True

        with patch.object(backend, "_sync_memory_to_redis"):
            backend._do_recovery()

        backend._wal.cleanup_processed.assert_called_once()
        call_args = backend._wal.cleanup_processed.call_args
        # Either positional or keyword — both shapes accepted by impl.
        assert call_args.kwargs.get("mode") == "runtime"

    def test_exception_rolls_mode_back_to_degraded(self, backend):
        """If ``_replay_wal_entry`` (or anything else under the try)
        raises an unhandled exception, mode must roll back to DEGRADED
        — RECOVERING leaks would leave the backend permanently stuck.
        """
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._mode = ResilientStorageMode.DEGRADED
        backend._wal = MagicMock()
        backend._wal.recover_unprocessed.side_effect = RuntimeError("io")
        backend._wal_initialized = True

        result = backend._do_recovery()

        assert result is False
        assert backend._mode == ResilientStorageMode.DEGRADED

    def test_success_transitions_mode_to_redis(self, backend):
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._mode = ResilientStorageMode.DEGRADED
        backend._wal = MagicMock()
        backend._wal.recover_unprocessed.return_value = []
        backend._wal_initialized = True

        with patch.object(backend, "_sync_memory_to_redis"):
            result = backend._do_recovery()

        assert result is True
        assert backend._mode == ResilientStorageMode.REDIS

    def test_two_phase_recovery_surfaces_write_in_rotated_wal_file(
        self, backend, monkeypatch
    ):
        """#539 D1 cross-file delta: a degraded write landing post-bulk in a
        freshly rotated WAL file is still surfaced by the locked delta-replay.

        ``recover_unprocessed`` globs every this-PID file and merges by
        sequence, so the cross-file boundary is absorbed (audit/wal/_reader.py).
        Uses a real temp-dir WAL so an actual rotation creates a new file.
        """
        import os

        from baldur.adapters.resilient.backend import ResilientStorageMode

        # Keep the hot path off a real Redis connection so degraded writes
        # actually hit the WAL.
        backend._next_redis_probe = time.monotonic() + 9999.0

        # WAL filenames embed a second-resolution timestamp, so a rotate +
        # write within the same second would reopen the SAME file. Force unique
        # names per rotation so the post-bulk write lands in a genuinely new
        # file (the cross-file boundary under test). The name still matches the
        # runtime glob ``<prefix>_*_<pid>.wal``.
        counter = {"n": 0}

        def unique_name():
            counter["n"] += 1
            return (
                f"{backend._wal._config.file_prefix}_"
                f"{counter['n']:04d}_{os.getpid()}.wal"
            )

        monkeypatch.setattr(backend._wal, "_get_current_wal_filename", unique_name)

        # Pre-window degraded write -> first WAL file.
        backend.set("before", "v0")

        # Wire a MagicMock redis (identity _serialize) as the recovered client.
        raw = MagicMock()
        redis = MagicMock()
        redis._redis = raw
        redis.raw_client = raw
        redis._serialize = MagicMock(side_effect=lambda value: value)
        backend._redis = redis
        backend._redis_initialized = True

        # During the lock-free bulk phase (via the _sync_memory_to_redis seam):
        # rotate the WAL, then issue a degraded write that lands in the NEW file.
        def rotate_then_write():
            backend._wal._rotate_file()
            backend.set("after", "v1")

        with patch.object(
            backend, "_sync_memory_to_redis", side_effect=rotate_then_write
        ):
            assert backend._do_recovery() is True

        assert backend._mode == ResilientStorageMode.REDIS
        # The cross-file post-bulk write was replayed to Redis by the locked
        # delta — not stranded in the rotated file until restart.
        expected_key = backend._get_full_key("after")
        raw.set.assert_any_call(expected_key, "v1")


# =============================================================================
# D6: Event prefix consistency (drive-by G9 fix)
# =============================================================================


class TestRecoveryEventNamingContract:
    """G9 drive-by: file-local ``watchdog.recovery_failed`` was renamed
    to ``resilient_storage.recovery_failed`` so the file's 14 other
    events stay in the same prefix family. The 7 callers in OTHER
    files that legitimately belong to ``watchdog.*`` are untouched
    (verified at design time).
    """

    def test_do_recovery_failure_emits_resilient_storage_prefix(self, backend):
        """Exception path emits ``resilient_storage.recovery_failed``,
        NOT the old ``watchdog.recovery_failed`` — file-local rename.
        """
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._mode = ResilientStorageMode.DEGRADED
        backend._wal = MagicMock()
        backend._wal.recover_unprocessed.side_effect = RuntimeError("io")
        backend._wal_initialized = True

        with patch("baldur.adapters.resilient.backend.logger") as mock_logger:
            backend._do_recovery()

        emitted_events = []
        for call in mock_logger.exception.call_args_list:
            if call.args:
                emitted_events.append(call.args[0])
        assert "resilient_storage.recovery_failed" in emitted_events
        assert "watchdog.recovery_failed" not in emitted_events
