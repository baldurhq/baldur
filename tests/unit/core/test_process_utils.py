"""Unit tests for core/process_utils.py — process-model detection.

Three helpers gate signal handlers and background-thread lifecycle:

- ``is_gunicorn_worker()`` — GUNICORN_WORKER env-var-based, set by
  ``post_worker_init``. Phase-dependent (False in worker pre-post_worker_init).
- ``is_under_gunicorn()`` — SERVER_SOFTWARE-based, set by gunicorn's
  master and inherited by workers via fork(). Phase-independent.
  Use for signal-handler guards.
- ``is_gunicorn_master()`` — composite: ``is_under_gunicorn() and not
  is_gunicorn_worker()``. Use for "skip in master" gating.

The Celery half answers the same question for the other supported pre-forking
server: ``is_celery_worker_process()`` is the argv heuristic, the
``mark_``/``is_celery_worker_main`` pair is the signal-based truth, and the
``mark_``/``is_celery_worker_serving`` pair is the per-serving-process marker.
``is_fork_source_process()`` composes all of them into the single predicate the
background-daemon starters consult — and outside celery it must reduce exactly
to ``is_gunicorn_master()``.

A further helper answers a different question — whether some *other* process is
still alive (``pid_alive``) — and decides whether a PID-stamped WAL file may be
absorbed or reclaimed. ``fork_repaired`` marks the entry points at which a
component re-owns its fork-inherited state.

The last group covers what a fork child inherits from the threads it does not
have: ``fork_safe_lock()`` / ``fork_safe_rlock()`` / ``register_fork_safe_lock()``
register locks for the child step that re-initializes them, and the before-fork
step holds each stream handler's lock so no parent thread is inside a log write
when the child is created. The steps are module functions, so they are driven
directly here without forking; the real-fork compositions live in the
integration suite. ``_at_fork_reinit`` exists only where ``fork()`` does, so the
registration and repair cases are POSIX-only.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import textwrap
import threading
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from baldur.core import process_utils
from baldur.core.process_utils import (
    fork_repaired,
    fork_safe_lock,
    fork_safe_rlock,
    is_celery_worker_main,
    is_celery_worker_process,
    is_celery_worker_serving,
    is_fork_source_process,
    is_gunicorn_master,
    is_gunicorn_worker,
    is_under_gunicorn,
    mark_celery_worker_main,
    mark_celery_worker_serving,
    pid_alive,
    register_fork_safe_lock,
)

_SERVING_ENV_VAR = process_utils._CELERY_WORKER_SERVING_ENV_VAR

_LOCK_TYPE = type(threading.Lock())
_RLOCK_TYPE = type(threading.RLock())

# CPython builds the at-fork re-initializer only where fork() exists, so on
# Windows nothing is registered and there is nothing to repair.
_HAS_AT_FORK_REINIT = hasattr(threading.Lock(), "_at_fork_reinit")
posix_fork_repair = pytest.mark.skipif(
    not _HAS_AT_FORK_REINIT,
    reason="locks have no _at_fork_reinit where fork() does not exist",
)


@pytest.fixture
def celery_main_flag_restored():
    """Restore the process-local celery-worker-main flag around a test.

    The flag is a module global that ``mark_celery_worker_main()`` sets
    permanently — a test that sets it and does not restore it would make every
    later test in the same worker look like a Celery worker main process.
    """
    original = process_utils._celery_worker_main
    try:
        yield
    finally:
        process_utils._celery_worker_main = original


class TestIsGunicornWorkerContract:
    """Contract: detection relies on GUNICORN_WORKER env var set to '1'."""

    def test_returns_true_when_gunicorn_worker_env_is_one(self):
        """GUNICORN_WORKER='1' → True (set by post_worker_init hook)."""
        with patch.dict("os.environ", {"GUNICORN_WORKER": "1"}):
            assert is_gunicorn_worker() is True

    def test_returns_false_when_gunicorn_worker_env_is_absent(self):
        """No GUNICORN_WORKER env → False (default process)."""
        with patch.dict("os.environ", {}, clear=True):
            assert is_gunicorn_worker() is False

    def test_returns_false_when_gunicorn_worker_env_is_zero(self):
        """GUNICORN_WORKER='0' → False (not the contract value)."""
        with patch.dict("os.environ", {"GUNICORN_WORKER": "0"}):
            assert is_gunicorn_worker() is False

    def test_returns_false_when_gunicorn_worker_env_is_true_string(self):
        """GUNICORN_WORKER='true' → False (only '1' is accepted)."""
        with patch.dict("os.environ", {"GUNICORN_WORKER": "true"}):
            assert is_gunicorn_worker() is False

    def test_returns_false_when_gunicorn_worker_env_is_empty(self):
        """GUNICORN_WORKER='' → False."""
        with patch.dict("os.environ", {"GUNICORN_WORKER": ""}):
            assert is_gunicorn_worker() is False


class TestIsGunicornWorkerBehavior:
    """Behavior: idempotent, no side effects."""

    def test_idempotent_returns_same_result_on_repeated_calls(self):
        """Same env → same result for N calls."""
        with patch.dict("os.environ", {"GUNICORN_WORKER": "1"}):
            results = [is_gunicorn_worker() for _ in range(5)]
            assert all(r is True for r in results)

    def test_responds_to_env_change_dynamically(self):
        """Result changes when env var changes between calls."""
        with patch.dict("os.environ", {}, clear=True):
            assert is_gunicorn_worker() is False

        with patch.dict("os.environ", {"GUNICORN_WORKER": "1"}):
            assert is_gunicorn_worker() is True


class TestIsUnderGunicornContract:
    """Contract: detection relies on SERVER_SOFTWARE containing 'gunicorn'.

    Set by gunicorn's master at startup and inherited by workers via fork(),
    so the helper returns True throughout the entire gunicorn lifecycle —
    including the worker pre-post_worker_init window where the env-var-
    based ``is_gunicorn_worker()`` returns False.
    """

    def test_returns_true_when_server_software_contains_gunicorn(self):
        """SERVER_SOFTWARE='gunicorn/21.2.0' → True (typical gunicorn value)."""
        with patch.dict("os.environ", {"SERVER_SOFTWARE": "gunicorn/21.2.0"}):
            assert is_under_gunicorn() is True

    def test_returns_true_for_bare_gunicorn_value(self):
        """SERVER_SOFTWARE='gunicorn' → True."""
        with patch.dict("os.environ", {"SERVER_SOFTWARE": "gunicorn"}):
            assert is_under_gunicorn() is True

    def test_returns_false_when_server_software_absent(self):
        """No SERVER_SOFTWARE env → False (not under gunicorn)."""
        with patch.dict("os.environ", {}, clear=True):
            assert is_under_gunicorn() is False

    def test_returns_false_for_non_gunicorn_server(self):
        """SERVER_SOFTWARE='uwsgi' → False."""
        with patch.dict("os.environ", {"SERVER_SOFTWARE": "uwsgi"}):
            assert is_under_gunicorn() is False

    def test_returns_false_for_empty_server_software(self):
        """SERVER_SOFTWARE='' → False."""
        with patch.dict("os.environ", {"SERVER_SOFTWARE": ""}):
            assert is_under_gunicorn() is False


class TestIsGunicornMasterContract:
    """Contract: master = under gunicorn AND NOT yet identified as a worker.

    Caveat: in worker pre-post_worker_init (between fork() and the moment
    GUNICORN_WORKER=1 is set), this helper returns True even though the
    process IS a worker. Callers must tolerate this race window.
    """

    def test_returns_true_in_master_process(self):
        """SERVER_SOFTWARE=gunicorn AND no GUNICORN_WORKER → True."""
        with patch.dict(
            "os.environ", {"SERVER_SOFTWARE": "gunicorn/21.2.0"}, clear=True
        ):
            assert is_gunicorn_master() is True

    def test_returns_false_in_worker_after_post_worker_init(self):
        """SERVER_SOFTWARE=gunicorn AND GUNICORN_WORKER=1 → False."""
        with patch.dict(
            "os.environ",
            {"SERVER_SOFTWARE": "gunicorn/21.2.0", "GUNICORN_WORKER": "1"},
            clear=True,
        ):
            assert is_gunicorn_master() is False

    def test_returns_false_outside_gunicorn(self):
        """No SERVER_SOFTWARE → False even if GUNICORN_WORKER unset."""
        with patch.dict("os.environ", {}, clear=True):
            assert is_gunicorn_master() is False

    def test_returns_true_in_worker_pre_post_worker_init_race_window(self):
        """SERVER_SOFTWARE=gunicorn AND no GUNICORN_WORKER → True.

        This is the documented race window — the worker process inherits
        SERVER_SOFTWARE via fork() but post_worker_init has not yet set
        GUNICORN_WORKER=1. The helper cannot distinguish this from the
        actual master process; callers using this gate for "skip in
        master" behavior MUST be tolerant of being invoked here.
        """
        with patch.dict(
            "os.environ", {"SERVER_SOFTWARE": "gunicorn/21.2.0"}, clear=True
        ):
            assert is_gunicorn_master() is True


class TestPidAliveContract:
    """``pid_alive`` decides whether a WAL file's owner may still be writing.

    Two rules of its own sit on top of the delegate: non-positive PIDs are
    rejected before probing at all, and an undecidable probe reports *live* so
    callers defer instead of reclaiming.
    """

    @pytest.mark.parametrize(
        "pid", [0, -1, -12345], ids=["zero", "minus_one", "large_negative"]
    )
    def test_non_positive_pid_is_rejected(self, pid):
        """``0`` is a process group on POSIX and Idle on Windows; ``-1`` means
        every process. Neither can be a filename-derived owner.
        """
        assert pid_alive(pid) is False

    @pytest.mark.parametrize("pid", [0, -1], ids=["zero", "minus_one"])
    def test_non_positive_pid_never_reaches_the_probe(self, pid):
        """The rejection is *before* the delegate — on Windows ``pid_exists(0)``
        answers True, so a probe-first ordering would report the Idle process
        as a live WAL owner.
        """
        with patch("psutil.pid_exists") as probe:
            pid_alive(pid)

        probe.assert_not_called()

    def test_own_pid_is_reported_alive(self):
        """The one PID whose liveness is knowable without mocking anything."""
        assert pid_alive(os.getpid()) is True

    def test_result_is_the_probes_answer_for_the_asked_pid(self):
        """Delegation: the queried PID is forwarded unchanged, and a negative
        answer from the delegate is a negative answer here — this is what makes
        a never-allocated PID's file reclaimable.
        """
        with patch("psutil.pid_exists", return_value=False) as probe:
            result = pid_alive(4242)

        assert result is False
        probe.assert_called_once_with(4242)

    def test_truthy_probe_answer_is_normalised_to_a_bool(self):
        """Callers branch on identity (``is True``), so the delegate's return
        value is coerced rather than passed through.
        """
        with patch("psutil.pid_exists", return_value=1):
            assert pid_alive(4242) is True

    def test_probe_failure_reports_live(self):
        """Fail direction: undecidable means defer. Reporting dead would let a
        reclaimer unlink a file whose owner is still appending to it.
        """
        with patch("psutil.pid_exists", side_effect=OSError("probe blew up")):
            assert pid_alive(4242) is True


class TestCeleryWorkerProcessDetectionBehavior:
    """``is_celery_worker_process()`` — the argv heuristic and its fail direction.

    Three conditions are required together: the launcher is celery, the
    subcommand is ``worker``, and celery is actually imported here. The
    subcommand scan has to step past the global options that consume the token
    after them, which is what keeps ``celery -A worker worker`` from reading the
    app name as the subcommand.
    """

    @pytest.fixture(autouse=True)
    def _celery_present(self, monkeypatch):
        """Pin celery into ``sys.modules`` so argv is the only variable."""
        monkeypatch.setitem(
            sys.modules, "celery", sys.modules.get("celery") or object()
        )

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["celery", "-A", "proj", "worker"], True),
            (["/usr/local/bin/celery", "worker", "-l", "info"], True),
            (["celery.exe", "worker"], True),
            (["/venv/lib/site-packages/celery/__main__.py", "worker"], True),
            ([r"C:\venv\Lib\site-packages\celery\__main__.py", "worker"], True),
            (["celery", "-A", "worker", "worker"], True),
            (["celery", "--app=proj", "worker"], True),
            (["celery", "-q", "worker"], True),
            (["celery", "-A", "proj", "beat"], False),
            (["celery", "-A", "worker", "beat"], False),
            (["celery"], False),
            (["python", "manage.py", "runserver"], False),
            (["gunicorn", "proj.wsgi:application"], False),
            (["/usr/bin/celerybeat", "worker"], False),
            ([], False),
        ],
        ids=[
            "console_script_with_app_option",
            "absolute_console_script_path",
            "windows_console_script_exe",
            "python_dash_m_posix_separators",
            "python_dash_m_windows_separators",
            "app_option_value_is_not_the_subcommand",
            "inline_app_option_value",
            "valueless_global_flag_before_subcommand",
            "beat_subcommand",
            "beat_subcommand_behind_app_named_worker",
            "no_subcommand_at_all",
            "django_management_command",
            "gunicorn_launcher",
            "program_name_only_prefixed_by_celery",
            "empty_argv",
        ],
    )
    def test_argv_shape_decides_celery_worker_detection(
        self, monkeypatch, argv, expected
    ):
        """The launcher/subcommand pair decides; everything else is False."""
        monkeypatch.setattr(sys, "argv", argv)

        assert is_celery_worker_process() is expected

    def test_celery_absent_from_sys_modules_returns_false(self, monkeypatch):
        """A non-celery process carrying celery-shaped argv is not a worker.

        The ``sys.modules`` condition is what keeps a script that merely happens
        to be invoked with those arguments out of the answer.
        """
        monkeypatch.setattr(sys, "argv", ["celery", "-A", "proj", "worker"])
        monkeypatch.delitem(sys.modules, "celery", raising=False)

        assert is_celery_worker_process() is False


class TestCeleryServingMarkerBehavior:
    """The serving marker is an env var, so a fork child inherits it unset.

    An env var rather than a module global because billiard's spawn path
    re-imports the app module in the child — a module global would come back
    False there — while the child's ``os.environ`` is its own copy.
    """

    def test_marker_is_unset_before_any_process_marks_itself(self, monkeypatch):
        """Fresh process: nothing has claimed to be serving."""
        monkeypatch.delenv(_SERVING_ENV_VAR, raising=False)

        assert is_celery_worker_serving() is False

    def test_mark_then_read_round_trips_through_the_environment(self, monkeypatch):
        """Marking is observable through ``os.environ``, which fork copies."""
        monkeypatch.delenv(_SERVING_ENV_VAR, raising=False)

        mark_celery_worker_serving()

        assert os.environ[_SERVING_ENV_VAR] == "1"
        assert is_celery_worker_serving() is True

    def test_marking_twice_leaves_the_same_marker(self, monkeypatch):
        """Idempotent: the solo pool marks in both receivers, in one process."""
        monkeypatch.delenv(_SERVING_ENV_VAR, raising=False)

        mark_celery_worker_serving()
        mark_celery_worker_serving()

        assert os.environ[_SERVING_ENV_VAR] == "1"
        assert is_celery_worker_serving() is True

    def test_foreign_marker_value_is_not_accepted_as_serving(self, monkeypatch):
        """Only the value baldur writes counts, mirroring ``GUNICORN_WORKER``."""
        monkeypatch.setenv(_SERVING_ENV_VAR, "yes")

        assert is_celery_worker_serving() is False


class TestCeleryWorkerMainFlagBehavior:
    """The signal-based half of the celery detection, set by ``worker_init``.

    Signal-based truth holds for launcher shapes the argv heuristic cannot
    recognize — a programmatic worker, above all — which is why the flag exists
    alongside the argv predicate rather than instead of it.
    """

    def test_flag_is_false_until_a_worker_init_signal_marks_it(
        self, celery_main_flag_restored
    ):
        """Nothing observed yet → False, which is the status-quo answer."""
        process_utils._celery_worker_main = False

        assert is_celery_worker_main() is False

    def test_marking_records_the_worker_main_process(self, celery_main_flag_restored):
        """``mark_celery_worker_main()`` is what the receiver calls first."""
        process_utils._celery_worker_main = False

        mark_celery_worker_main()

        assert is_celery_worker_main() is True


class TestForkSourcePredicateBehavior:
    """``is_fork_source_process()`` — the single starter-skip predicate.

    Precedence is load-bearing: the gunicorn master answers True before the
    celery half is consulted, and the serving marker answers False before either
    celery signal is. Outside celery the whole thing must reduce to
    ``is_gunicorn_master()`` — that reduction is what keeps the gunicorn
    behavior this predicate replaced unchanged.
    """

    @pytest.fixture
    def clean_process_model(self, monkeypatch, celery_main_flag_restored):
        """Neither server, no signal observed, no serving marker, plain argv."""
        monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)
        monkeypatch.delenv(_SERVING_ENV_VAR, raising=False)
        monkeypatch.setattr(sys, "argv", ["python", "manage.py", "runserver"])
        process_utils._celery_worker_main = False

    def test_gunicorn_master_is_a_fork_source(self, clean_process_model, monkeypatch):
        """The original fork source, unchanged."""
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")

        assert is_fork_source_process() is True

    def test_gunicorn_worker_is_not_a_fork_source(
        self, clean_process_model, monkeypatch
    ):
        """``post_worker_init`` flipped the marker; the worker starts its own."""
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.setenv("GUNICORN_WORKER", "1")

        assert is_fork_source_process() is False

    def test_celery_worker_main_known_by_signal_is_a_fork_source(
        self, clean_process_model
    ):
        """The ``worker_init`` flag alone is enough — no argv shape required."""
        process_utils._celery_worker_main = True

        assert is_fork_source_process() is True

    def test_celery_worker_main_known_by_argv_is_a_fork_source(
        self, clean_process_model, monkeypatch
    ):
        """Before any signal fires, the argv heuristic carries the answer."""
        monkeypatch.setitem(
            sys.modules, "celery", sys.modules.get("celery") or object()
        )
        monkeypatch.setattr(sys, "argv", ["celery", "-A", "proj", "worker"])

        assert is_fork_source_process() is True

    def test_serving_marker_overrides_the_celery_worker_main_flag(
        self, clean_process_model, monkeypatch
    ):
        """A prefork child inherits the flag but marks itself serving first.

        This is the ordering the ``worker_process_init`` receiver depends on:
        with the marker set, the inherited flag must not defer the child's own
        starters.
        """
        process_utils._celery_worker_main = True
        monkeypatch.setenv(_SERVING_ENV_VAR, "1")

        assert is_fork_source_process() is False

    def test_serving_marker_overrides_the_celery_argv_heuristic(
        self, clean_process_model, monkeypatch
    ):
        """Billiard's spawn path restores the parent's argv into the child.

        The child therefore reads as "celery worker main" by argv; the marker,
        set before ``init()``, is what makes it answer False anyway.
        """
        monkeypatch.setitem(
            sys.modules, "celery", sys.modules.get("celery") or object()
        )
        monkeypatch.setattr(sys, "argv", ["celery", "-A", "proj", "worker"])
        monkeypatch.setenv(_SERVING_ENV_VAR, "1")

        assert is_fork_source_process() is False

    def test_serving_marker_does_not_override_the_gunicorn_master(
        self, clean_process_model, monkeypatch
    ):
        """Gunicorn is answered first, so a stale celery marker cannot unskip it."""
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.setenv(_SERVING_ENV_VAR, "1")

        assert is_fork_source_process() is True

    def test_plain_process_is_not_a_fork_source(self, clean_process_model):
        """Neither server: the single-process CLI / runserver shape."""
        assert is_fork_source_process() is False

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({"SERVER_SOFTWARE": "gunicorn/21.2.0"}, True),
            ({"SERVER_SOFTWARE": "gunicorn/21.2.0", "GUNICORN_WORKER": "1"}, False),
            ({}, False),
            ({"SERVER_SOFTWARE": "uwsgi"}, False),
        ],
        ids=["master", "worker", "no_server", "other_server"],
    )
    def test_outside_celery_the_predicate_equals_is_gunicorn_master(
        self, clean_process_model, monkeypatch, env, expected
    ):
        """Regression guard: replacing the starters' skip changed no gunicorn case.

        The starters used to consult ``is_gunicorn_master()`` directly. With no
        celery signal, no celery argv and no serving marker, the composed
        predicate has to return exactly what that helper returns.
        """
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        assert is_fork_source_process() is expected
        assert is_fork_source_process() is is_gunicorn_master()


class TestForkRepairedDecoratorContract:
    """``fork_repaired`` has two forms and exactly one marker.

    The marker is what the repaired classes' introspection gates assert on, so
    both forms must carry it under the same attribute name; the repair must run
    *before* the wrapped body, because the body is what touches the inherited
    state.
    """

    def test_owner_form_runs_the_repair_before_the_wrapped_body(self):
        """The owner's ``_repair_if_forked()`` is looked up on ``self``."""
        # Given — an owner that records the order it is called in
        calls: list[str] = []

        class Subject:
            def _repair_if_forked(self) -> None:
                calls.append("repair")

            @fork_repaired
            def entry(self, value: int) -> int:
                calls.append("body")
                return value

        # When
        result = Subject().entry(7)

        # Then
        assert calls == ["repair", "body"]
        assert result == 7

    def test_owner_form_carries_the_marker(self):
        """Machine-checkable coverage: the wrapper is explicitly marked."""

        class Subject:
            def _repair_if_forked(self) -> None: ...

            @fork_repaired
            def entry(self) -> None: ...

        assert Subject.entry.__fork_repaired__ is True

    def test_module_form_runs_the_named_repair_before_the_wrapped_body(self):
        """A module-level entry point has no owner, so the repair is named."""
        # Given
        calls: list[str] = []

        def _repair() -> None:
            calls.append("repair")

        @fork_repaired(repair=_repair)
        def entry(value: int) -> int:
            calls.append("body")
            return value

        # When
        result = entry(11)

        # Then
        assert calls == ["repair", "body"]
        assert result == 11

    def test_module_form_carries_the_same_marker(self):
        """One marker for both forms — the gates cannot tell them apart."""

        @fork_repaired(repair=lambda: None)
        def entry() -> None: ...

        assert entry.__fork_repaired__ is True

    def test_module_form_forwards_positional_and_keyword_arguments(self):
        """The wrapper is transparent apart from the repair it prepends."""
        seen: dict[str, object] = {}

        @fork_repaired(repair=lambda: None)
        def entry(first: int, *, second: str) -> str:
            seen["first"] = first
            seen["second"] = second
            return f"{first}-{second}"

        assert entry(3, second="x") == "3-x"
        assert seen == {"first": 3, "second": "x"}

    def test_both_forms_preserve_the_wrapped_name(self):
        """``functools.wraps`` keeps introspection pointed at the entry point."""

        class Subject:
            def _repair_if_forked(self) -> None: ...

            @fork_repaired
            def owner_entry(self) -> None: ...

        @fork_repaired(repair=lambda: None)
        def module_entry() -> None: ...

        assert Subject.owner_entry.__name__ == "owner_entry"
        assert module_entry.__name__ == "module_entry"


# =============================================================================
# Fork-safe locks — shared doubles
# =============================================================================


def _is_registered(lock: object) -> bool:
    """Is ``lock`` in the registry the child repair walks?"""
    return any(entry is lock for entry in list(process_utils._fork_safe_locks))


def _free_from_another_thread(lock) -> bool:
    """Return True if a thread holding nothing can take ``lock`` right now."""
    outcome: list[bool] = []

    def probe() -> None:
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        outcome.append(acquired)

    prober = threading.Thread(target=probe)
    prober.start()
    prober.join(timeout=5)
    return outcome == [True]


def _frames() -> list:
    """The calling thread's stack of before-fork frames (empty if none)."""
    return list(getattr(process_utils._fork_log_holds, "stack", None) or [])


class _HeldByAnotherThread:
    """Hold ``lock`` on a helper thread for the duration of a ``with`` block.

    The helper stands in for a parent thread that does not survive ``fork()``.
    Its own release may find the lock already re-initialized under it — the
    state a fork child is in — which raises ``RuntimeError`` and is expected.
    """

    def __init__(self, lock) -> None:
        self._lock = lock
        self._held = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        self._lock.acquire()
        self._held.set()
        self._release.wait(timeout=10)
        try:
            self._lock.release()
        except RuntimeError:
            pass

    def __enter__(self) -> _HeldByAnotherThread:
        self._thread.start()
        assert self._held.wait(timeout=5), "helper thread never took the lock"
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._release.set()
        self._thread.join(timeout=5)


class _LockLike:
    """Answers ``_at_fork_reinit`` like a lock but is not a C lock.

    Weak-referenceable, so only the registration's type check — not the
    attribute probe, not the weak-set insert — can keep it out.
    """

    def __init__(self) -> None:
        self.reinitialized = False

    def _at_fork_reinit(self) -> None:
        self.reinitialized = True


class _RaisingReinit:
    """A registry entry whose re-initializer raises."""

    def __init__(self) -> None:
        self.touched = False

    def _at_fork_reinit(self) -> None:
        self.touched = True
        raise RuntimeError("re-initializer failed")


class _RaisingOwnershipQuery:
    """A registry entry whose ``_is_owned`` raises before any repair."""

    def __init__(self) -> None:
        self.touched = False

    def _is_owned(self) -> bool:
        self.touched = True
        raise RuntimeError("ownership query failed")

    def _at_fork_reinit(self) -> None:
        raise AssertionError("the repair must not continue past a failed query")


class _UnreadableRegistry:
    """A registry that raises when the repair tries to snapshot it."""

    def __init__(self) -> None:
        self.touched = False

    def __iter__(self):
        self.touched = True
        raise RuntimeError("registry unreadable")


class _WaitSignallingLock:
    """Delegates to a lock and signals once a caller is actually waiting on it.

    The signal is raised only for a blocking acquire that found the lock
    taken, so a caller that tried once without waiting never raises it.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._inner.acquire(blocking=False):
            return True
        if not blocking:
            return False
        self.waiting.set()
        return self._inner.acquire(True, timeout)

    def release(self) -> None:
        self._inner.release()

    # logging takes its module lock with ``with`` from 3.13 on.
    def __enter__(self) -> _WaitSignallingLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class _FakeMonotonicClock:
    """The only clock the before-fork step reads, advanced by the lock doubles."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _TimedOutLock:
    """A handler lock whose write never finishes: each wait runs out its timeout."""

    def __init__(self, clock: _FakeMonotonicClock) -> None:
        self._clock = clock
        self.timeouts: list[float] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.timeouts.append(timeout)
        self._clock.now += timeout
        return False

    def release(self) -> None:
        raise AssertionError("a lock that was never acquired was released")


class _ImmediatelyFreeLock:
    """A handler lock no thread holds: acquired at once, in no clock time."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.releases = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.timeouts.append(timeout)
        return True

    def release(self) -> None:
        self.releases += 1


class _CountingLock:
    """Delegates to a lock and counts every acquire attempt, from any thread."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.acquire_calls = 0

    def acquire(self, *args, **kwargs) -> bool:
        self.acquire_calls += 1
        return self._inner.acquire(*args, **kwargs)

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> _CountingLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class _OrderRecordingLock:
    """Delegates to a lock and records the test thread's acquires and releases.

    Other threads go through untouched, so a stray log call elsewhere in the
    process cannot enter the record.
    """

    def __init__(self, inner, name: str, events: list, thread_id: int) -> None:
        self._inner = inner
        self._name = name
        self._events = events
        self._thread_id = thread_id

    def acquire(self, *args, **kwargs) -> bool:
        acquired = self._inner.acquire(*args, **kwargs)
        if acquired and threading.get_ident() == self._thread_id:
            self._events.append((self._name, "acquire"))
        return acquired

    def release(self) -> None:
        if threading.get_ident() == self._thread_id:
            self._events.append((self._name, "release"))
        self._inner.release()

    def __enter__(self) -> _OrderRecordingLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@pytest.fixture
def isolated_handlers(monkeypatch):
    """Point the before-fork walk at the handlers a test built, and only those.

    ``logging._handlerList`` holds a weak reference to every handler the
    process ever created, pytest's own included; a lock some other handler
    holds would otherwise spend the shared deadline before the walk reached
    the test's handler.
    """

    def install(*handlers: logging.Handler) -> None:
        monkeypatch.setattr(
            logging, "_handlerList", [weakref.ref(handler) for handler in handlers]
        )

    return install


@pytest.fixture
def clean_fork_log_stack():
    """Drop any before-fork frame a failing test left on this thread."""
    yield
    stack = getattr(process_utils._fork_log_holds, "stack", None)
    if stack:
        stack.clear()


# =============================================================================
# Fork-safe locks — the factories and the registration
# =============================================================================


class TestForkSafeLockFactoryContract:
    """The factories hand back the plain C lock, registered for the child repair.

    No wrapper: acquiring the lock must cost what acquiring any lock costs, and
    code that checks the lock's type keeps working.
    """

    @pytest.mark.parametrize(
        ("factory", "expected_type"),
        [(fork_safe_lock, _LOCK_TYPE), (fork_safe_rlock, _RLOCK_TYPE)],
        ids=["lock", "rlock"],
    )
    def test_factory_returns_the_raw_c_lock(self, factory, expected_type):
        """The exact stdlib type — not a subclass, not a proxy."""
        assert type(factory()) is expected_type

    @posix_fork_repair
    @pytest.mark.parametrize(
        "factory", [fork_safe_lock, fork_safe_rlock], ids=["lock", "rlock"]
    )
    def test_factory_registers_the_lock_for_the_child_repair(self, factory):
        """What the factory returns is what the repair re-initializes."""
        lock = factory()

        assert _is_registered(lock)

    @pytest.mark.skipif(
        _HAS_AT_FORK_REINIT, reason="covers interpreters built without fork()"
    )
    @pytest.mark.parametrize(
        "factory", [fork_safe_lock, fork_safe_rlock], ids=["lock", "rlock"]
    )
    def test_factory_leaves_the_lock_unregistered_where_fork_does_not_exist(
        self, factory
    ):
        """Windows: no ``fork()``, no re-initializer, nothing to repair after."""
        lock = factory()

        assert not _is_registered(lock)


class TestRegisterForkSafeLockContract:
    """``register_fork_safe_lock()`` admits C locks by type and ignores the rest.

    It exists for locks a library builds on Baldur's behalf (a redis-py pool's),
    so its input is whatever a ``getattr`` returned — possibly ``None`` after a
    rename, possibly a test double — and it must never raise.
    """

    @posix_fork_repair
    @pytest.mark.parametrize(
        "make_lock", [threading.Lock, threading.RLock], ids=["lock", "rlock"]
    )
    def test_register_accepts_a_c_lock_built_elsewhere(self, make_lock):
        """A lock this code did not construct joins the repair."""
        lock = make_lock()

        register_fork_safe_lock(lock)

        assert _is_registered(lock)

    @posix_fork_repair
    def test_registering_the_same_lock_twice_keeps_one_entry(self):
        """Idempotent: two components registering one pool's lock repair it once."""
        lock = threading.Lock()

        register_fork_safe_lock(lock)
        register_fork_safe_lock(lock)

        entries = [e for e in list(process_utils._fork_safe_locks) if e is lock]
        assert len(entries) == 1

    @pytest.mark.parametrize(
        "candidate",
        [None, _LockLike(), threading.Semaphore(), threading.Event()],
        ids=["none", "lock_like_double", "semaphore", "event"],
    )
    def test_register_ignores_anything_that_is_not_a_c_lock(self, candidate):
        """Admission is by type, not by answering ``_at_fork_reinit``.

        The lock-like double answers the attribute and is weak-referenceable,
        so the type check is the only thing that keeps it out.
        """
        register_fork_safe_lock(candidate)

        assert not _is_registered(candidate)


# =============================================================================
# Fork-safe locks — the child repair
# =============================================================================


@posix_fork_repair
class TestForkLockRepairBehavior:
    """``_reinit_fork_safe_locks()`` frees what dead threads held.

    Driven against a patched registry: re-initializing every lock the test
    process registered would free locks live threads of this process hold.
    """

    @pytest.mark.parametrize(
        "factory", [fork_safe_lock, fork_safe_rlock], ids=["lock", "rlock"]
    )
    def test_lock_held_by_another_thread_is_free_after_the_repair(
        self, monkeypatch, factory
    ):
        """The fork case: the holder does not exist in the child."""
        # Given
        lock = factory()
        monkeypatch.setattr(process_utils, "_fork_safe_locks", [lock])

        with _HeldByAnotherThread(lock):
            assert not _free_from_another_thread(lock)

            # When
            process_utils._reinit_fork_safe_locks()

            # Then
            assert _free_from_another_thread(lock)

    def test_rlock_owned_by_the_calling_thread_is_left_owned(self, monkeypatch):
        """The forking thread survives into the child and releases it itself."""
        # Given
        lock = fork_safe_rlock()
        monkeypatch.setattr(process_utils, "_fork_safe_locks", [lock])
        lock.acquire()

        try:
            # When
            process_utils._reinit_fork_safe_locks()

            # Then
            assert not _free_from_another_thread(lock)
        finally:
            lock.release()  # raises if the repair had re-initialized it

        assert _free_from_another_thread(lock)

    def test_plain_lock_held_by_the_calling_thread_is_freed(self, monkeypatch):
        """The factory docstring's one constraint on callers.

        A plain lock records no owner, so the repair cannot tell the forking
        thread's hold from a dead thread's and frees it; the forking thread's
        later ``release()`` then raises.
        """
        lock = fork_safe_lock()
        monkeypatch.setattr(process_utils, "_fork_safe_locks", [lock])
        lock.acquire()

        process_utils._reinit_fork_safe_locks()

        assert _free_from_another_thread(lock)
        with pytest.raises(RuntimeError):
            lock.release()

    def test_a_failing_entry_does_not_stop_the_repair_of_the_rest(self, monkeypatch):
        """Each lock is repaired on its own: CPython reports a raising at-fork
        callback and moves on, which would leave every later lock held.
        """
        # Given — two entries that fail in different places, then a held lock
        failing_reinit = _RaisingReinit()
        failing_query = _RaisingOwnershipQuery()
        lock = fork_safe_lock()
        monkeypatch.setattr(
            process_utils, "_fork_safe_locks", [failing_reinit, failing_query, lock]
        )

        with _HeldByAnotherThread(lock):
            # When
            process_utils._reinit_fork_safe_locks()

            # Then
            assert failing_reinit.touched
            assert failing_query.touched
            assert _free_from_another_thread(lock)

    def test_repair_returns_quietly_when_the_registry_cannot_be_read(self, monkeypatch):
        """Fail-open: the child keeps its locks as inherited — today's state."""
        registry = _UnreadableRegistry()
        monkeypatch.setattr(process_utils, "_fork_safe_locks", registry)

        process_utils._reinit_fork_safe_locks()

        assert registry.touched


# =============================================================================
# Stream-handler hold across the fork
# =============================================================================


@pytest.mark.usefixtures("clean_fork_log_stack")
class TestForkLogHoldBehavior:
    """The before-fork step waits out stream writes; the after-steps undo it.

    A buffered stream's internal lock is not re-initialized in a fork child, so
    a parent thread inside ``StreamHandler.emit`` at the fork instant leaves the
    child's first log line blocked forever. Holding each stream handler's lock
    across the fork keeps every parent thread out of such a write. The steps
    are called directly from the test thread, which plays the forking thread.
    """

    def test_before_step_waits_for_a_stream_handler_write_in_progress(
        self, isolated_handlers
    ):
        """A write that finishes within the deadline is waited for, then held."""
        # Given — another thread is inside a write and finishes it only once
        # the before-step is actually waiting for it
        handler = logging.StreamHandler(io.StringIO())
        inner = handler.lock
        handler.lock = waiting_lock = _WaitSignallingLock(inner)
        isolated_handlers(handler)
        writing = threading.Event()

        def writer() -> None:
            inner.acquire()
            writing.set()
            waiting_lock.waiting.wait(timeout=5)
            inner.release()

        writer_thread = threading.Thread(target=writer, daemon=True)
        writer_thread.start()
        assert writing.wait(timeout=5)

        # When
        process_utils._hold_stream_handler_locks()
        try:
            held = list(_frames()[-1].handler_locks)
        finally:
            process_utils._release_stream_handler_locks()
        writer_thread.join(timeout=5)

        # Then
        assert held == [waiting_lock]
        assert _free_from_another_thread(inner)

    def test_before_step_gives_up_on_a_write_that_outlasts_the_deadline(
        self, monkeypatch, isolated_handlers
    ):
        """A blocked stream (a full pipe) costs the fork the deadline, then is
        left as it would be without the step — the fork is never held hostage.
        """
        monkeypatch.setattr(process_utils, "_FORK_LOG_HANDLER_WAIT_SECONDS", 0.05)
        handler = logging.StreamHandler(io.StringIO())
        isolated_handlers(handler)

        with _HeldByAnotherThread(handler.lock):
            process_utils._hold_stream_handler_locks()
            try:
                held = list(_frames()[-1].handler_locks)
            finally:
                process_utils._release_stream_handler_locks()

        assert held == []

    def test_a_timed_out_handler_spends_the_shared_budget_so_later_ones_are_skipped(
        self, monkeypatch, isolated_handlers
    ):
        """One deadline for the whole walk, not one per handler."""
        # Given — the first handler's write never finishes, the second is free
        clock = _FakeMonotonicClock()
        monkeypatch.setattr(process_utils, "time", clock)
        blocked = logging.StreamHandler(io.StringIO())
        free = logging.StreamHandler(io.StringIO())
        blocked.lock = blocked_lock = _TimedOutLock(clock)
        free.lock = free_lock = _ImmediatelyFreeLock()
        isolated_handlers(blocked, free)

        # When
        process_utils._hold_stream_handler_locks()
        try:
            held = list(_frames()[-1].handler_locks)
        finally:
            process_utils._release_stream_handler_locks()

        # Then
        budget = process_utils._FORK_LOG_HANDLER_WAIT_SECONDS
        assert blocked_lock.timeouts == [budget]
        assert free_lock.timeouts == []
        assert held == []

    def test_handlers_reached_before_the_budget_runs_out_are_held(
        self, monkeypatch, isolated_handlers
    ):
        """Each wait gets what is left of the one budget; a free lock costs none."""
        # Given
        clock = _FakeMonotonicClock()
        monkeypatch.setattr(process_utils, "time", clock)
        free = logging.StreamHandler(io.StringIO())
        blocked = logging.StreamHandler(io.StringIO())
        free.lock = free_lock = _ImmediatelyFreeLock()
        blocked.lock = blocked_lock = _TimedOutLock(clock)
        isolated_handlers(free, blocked)

        # When
        process_utils._hold_stream_handler_locks()
        try:
            held = list(_frames()[-1].handler_locks)
        finally:
            process_utils._release_stream_handler_locks()

        # Then
        budget = process_utils._FORK_LOG_HANDLER_WAIT_SECONDS
        assert held == [free_lock]
        assert free_lock.timeouts == [budget]
        assert blocked_lock.timeouts == [budget]
        assert free_lock.releases == 1

    @pytest.mark.parametrize(
        "make_handler",
        [
            lambda path: logging.StreamHandler(io.StringIO()),
            lambda path: logging.FileHandler(path, delay=True),
        ],
        ids=["stream_handler", "file_handler_subclass"],
    )
    def test_before_step_holds_every_stream_handler_subclass(
        self, tmp_path, isolated_handlers, make_handler
    ):
        """``FileHandler`` and the rotating handlers write through a stream too."""
        handler = make_handler(tmp_path / "fork.log")
        isolated_handlers(handler)

        process_utils._hold_stream_handler_locks()
        try:
            held = list(_frames()[-1].handler_locks)
        finally:
            process_utils._release_stream_handler_locks()

        assert held == [handler.lock]

    @pytest.mark.parametrize(
        "make_forwarder",
        [
            lambda target: logging.handlers.MemoryHandler(capacity=1, target=target),
            lambda target: logging.handlers.QueueHandler(queue.SimpleQueue()),
        ],
        ids=["memory_handler", "queue_handler"],
    )
    def test_before_step_never_takes_a_forwarding_handler_lock(
        self, isolated_handlers, make_forwarder
    ):
        """Holding a handler that forwards to another deadlocks against a thread
        inside the forwarding (the stdlib's own reason for dropping the
        hold-every-handler approach) — only the stream leaves are held.
        """
        # Given
        stream = logging.StreamHandler(io.StringIO())
        forwarder = make_forwarder(stream)
        forwarder.lock = counting_lock = _CountingLock(forwarder.lock)
        isolated_handlers(forwarder, stream)

        # When
        process_utils._hold_stream_handler_locks()
        try:
            held = list(_frames()[-1].handler_locks)
        finally:
            process_utils._release_stream_handler_locks()

        # Then
        assert counting_lock.acquire_calls == 0
        assert held == [stream.lock]

    def test_module_lock_is_taken_before_and_released_after_the_handler_locks(
        self, monkeypatch, isolated_handlers
    ):
        """``logging.config`` takes the module lock and then each handler's, so
        the hold must take them in that order or deadlock against it.
        """
        # Given — handler built before the module lock is instrumented, so its
        # own registration does not enter the record
        events: list[tuple[str, str]] = []
        test_thread = threading.get_ident()
        handler = logging.StreamHandler(io.StringIO())
        handler.lock = _OrderRecordingLock(handler.lock, "handler", events, test_thread)
        isolated_handlers(handler)
        monkeypatch.setattr(
            logging,
            "_lock",
            _OrderRecordingLock(logging._lock, "module", events, test_thread),
        )

        # When
        process_utils._hold_stream_handler_locks()
        process_utils._release_stream_handler_locks()

        # Then
        assert events == [
            ("module", "acquire"),
            ("handler", "acquire"),
            ("handler", "release"),
            ("module", "release"),
        ]

    def test_two_threads_running_the_fork_steps_concurrently_leave_no_lock_held(
        self, isolated_handlers
    ):
        """Nothing serializes forks from two threads of one process: a second
        thread's before-step can start while the first thread's fork is in
        progress, and each after-step must undo only its own thread's hold.

        The interleaving is forced rather than hoped for — a real fork releases
        the GIL between the two steps, a unit loop does not — and the module
        lock is a private stand-in, so a failure cannot leave logging's own
        lock held by a finished thread.
        """
        # Given
        handler = logging.StreamHandler(io.StringIO())
        isolated_handlers(handler)
        real_module_lock = logging._lock
        module_lock = _WaitSignallingLock(threading.RLock())
        first_holds = threading.Event()
        errors: list[BaseException] = []

        def first_forker() -> None:
            try:
                process_utils._hold_stream_handler_locks()
                first_holds.set()
                # The second thread's before-step now waits behind this hold.
                module_lock.waiting.wait(timeout=5)
                process_utils._release_stream_handler_locks()
            except BaseException as e:  # pragma: no cover - reported below
                errors.append(e)

        def second_forker() -> None:
            try:
                first_holds.wait(timeout=5)
                process_utils._hold_stream_handler_locks()
                process_utils._release_stream_handler_locks()
            except BaseException as e:  # pragma: no cover - reported below
                errors.append(e)

        forkers = [
            threading.Thread(target=first_forker, daemon=True),
            threading.Thread(target=second_forker, daemon=True),
        ]

        logging._lock = module_lock
        try:
            # When
            for thread in forkers:
                thread.start()
            for thread in forkers:
                thread.join(timeout=5)

            # Then
            assert module_lock.waiting.is_set(), "the forks never overlapped"
            assert not any(thread.is_alive() for thread in forkers)
            assert errors == []
            assert _free_from_another_thread(module_lock)
            assert _free_from_another_thread(handler.lock)
        finally:
            # Restored here rather than by monkeypatch: pytest's own logging
            # teardown takes the module lock before fixtures are undone, and on
            # a regression a finished thread still owns the stand-in — and the
            # handler's lock, which logging.shutdown() at exit would wait on.
            logging._lock = real_module_lock
            handler.lock = None

    def test_nested_fork_steps_in_one_thread_unwind_to_no_lock_held(
        self, isolated_handlers
    ):
        """A fork started by a signal handler inside the before-step pushes its
        own frame instead of overwriting the outer fork's record.
        """
        handler = logging.StreamHandler(io.StringIO())
        isolated_handlers(handler)

        process_utils._hold_stream_handler_locks()
        process_utils._hold_stream_handler_locks()
        process_utils._release_stream_handler_locks()
        process_utils._release_stream_handler_locks()

        assert _frames() == []
        assert _free_from_another_thread(handler.lock)
        assert _free_from_another_thread(logging._lock)

    def test_missing_handler_list_holds_the_module_lock_only(self, monkeypatch):
        """A future ``logging`` without ``_handlerList`` degrades to today's
        behaviour: no handler is held, and the module-lock hold still pairs.
        """
        monkeypatch.delattr(logging, "_handlerList")

        process_utils._hold_stream_handler_locks()
        try:
            frame = _frames()[-1]
            held, module_lock = list(frame.handler_locks), frame.module_lock
        finally:
            process_utils._release_stream_handler_locks()

        assert held == []
        assert module_lock is logging._lock
        assert _free_from_another_thread(logging._lock)

    def test_parent_step_without_a_before_step_releases_nothing(self):
        """A thread with no frame of its own must not pop — or raise."""
        errors: list[BaseException] = []

        def parent_step_on_a_fresh_thread() -> None:
            try:
                process_utils._release_stream_handler_locks()
            except BaseException as e:  # pragma: no cover - reported below
                errors.append(e)

        thread = threading.Thread(target=parent_step_on_a_fresh_thread)
        thread.start()
        thread.join(timeout=5)

        assert errors == []

    @posix_fork_repair
    def test_child_step_frees_the_held_locks_and_keeps_the_module_hold(
        self, monkeypatch, isolated_handlers
    ):
        """In the child, logging's own hook has already re-initialized the module
        lock, so releasing it here would raise; the handler locks and every
        registered lock are re-initialized instead.
        """
        # Given — a registered lock a dead parent thread holds, and a handler
        # the before-step took on the forking (test) thread
        registered = fork_safe_lock()
        monkeypatch.setattr(process_utils, "_fork_safe_locks", [registered])
        handler = logging.StreamHandler(io.StringIO())
        isolated_handlers(handler)

        with _HeldByAnotherThread(registered):
            process_utils._hold_stream_handler_locks()
            try:
                # When
                process_utils._repair_after_fork_in_child()
                module_still_held = logging._lock._is_owned()
                frames_after = _frames()
                registered_free = _free_from_another_thread(registered)
            finally:
                # Here the test thread, not logging's child hook, ends the hold.
                logging._lock.release()

        # Then
        assert module_still_held is True
        assert frames_after == []
        assert registered_free
        assert _free_from_another_thread(handler.lock)

    def test_logging_internals_the_hold_reads_exist_on_this_interpreter(self):
        """Tripwire for the two private ``logging`` attributes the step reads.

        The module lock must be re-entrant: logging's own before-fork hook takes
        it again on the same thread after this step has taken it.
        """
        assert isinstance(logging._lock, _RLOCK_TYPE)
        assert isinstance(logging._handlerList, list)


class TestForkHookInstallContract:
    """``_install_fork_hook()`` registers the three steps in one call."""

    def test_install_registers_the_three_fork_steps(self, monkeypatch):
        """``register_at_fork`` takes keyword arguments only; one call, three steps."""
        calls: list[dict] = []
        monkeypatch.setattr(
            process_utils,
            "os",
            SimpleNamespace(register_at_fork=lambda **steps: calls.append(steps)),
        )

        process_utils._install_fork_hook()

        assert calls == [
            {
                "before": process_utils._hold_stream_handler_locks,
                "after_in_parent": process_utils._release_stream_handler_locks,
                "after_in_child": process_utils._repair_after_fork_in_child,
            }
        ]

    def test_install_without_register_at_fork_is_a_noop(self, monkeypatch):
        """Windows has no ``os.register_at_fork``: nothing to register, no error."""
        monkeypatch.setattr(process_utils, "os", SimpleNamespace())

        process_utils._install_fork_hook()


# =============================================================================
# No lock on the steady-state protected-call path
# =============================================================================

# Runs in a fresh interpreter: ``baldur.init()`` and several hundred protected
# calls would leave threads and singletons behind in the test process. The spy
# replaces ``process_utils``' own view of ``threading``, which the factories
# read at call time, so it sees every factory construction however a module
# bound the factory name. Only constructions on the calling thread count: the
# protected call runs there, while init's background threads build their own
# state on their own schedule.
_STEADY_STATE_SCRIPT = textwrap.dedent(
    """
    import threading

    import baldur.core.process_utils as process_utils

    calling_thread = threading.get_ident()
    constructions = {"count": 0}


    class _CountingThreading:
        def __getattr__(self, name):
            return getattr(threading, name)

        def Lock(self):
            if threading.get_ident() == calling_thread:
                constructions["count"] += 1
            return threading.Lock()

        def RLock(self):
            if threading.get_ident() == calling_thread:
                constructions["count"] += 1
            return threading.RLock()


    process_utils.threading = _CountingThreading()

    import baldur

    baldur.init()
    attempts = {"count": 0}


    @baldur.protected("steady_state_success")
    def succeed():
        return 1


    @baldur.protected("steady_state_retry", retry=True)
    def fail_every_seventh_attempt():
        attempts["count"] += 1
        if attempts["count"] % 7 == 0:
            raise ConnectionError("transient")
        return 1


    @baldur.protected("steady_state_trip")
    def always_fail():
        raise RuntimeError("dependency down")


    def run(rounds):
        for _ in range(rounds):
            succeed()
            for call in (fail_every_seventh_attempt, always_fail):
                try:
                    call()
                except Exception:
                    pass


    run(50)
    warmed_up = constructions["count"]
    run(134)
    print(f"WARMUP_CONSTRUCTIONS={warmed_up}")
    print(f"STEADY_CONSTRUCTIONS={constructions['count'] - warmed_up}")
    """
)


def _reported(stdout: str, key: str) -> int:
    for line in stdout.splitlines():
        if line.startswith(f"{key}="):
            return int(line.split("=", 1)[1])
    raise AssertionError(f"{key} missing from the script output:\n{stdout}")


class TestSteadyStateLockConstructionContract:
    """Registration is paid at construction only: after warm-up, protected calls
    construct no lock — success, retry and breaker-trip paths alike.
    """

    def test_steady_state_protected_calls_construct_no_fork_safe_lock(self, tmp_path):
        """402 calls after 150 warm-up calls: 0 factory constructions."""
        # Given — a clean environment: the session's BALDUR_* overrides would
        # otherwise decide the child's posture
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("BALDUR_")},
            "BALDUR_ADMIN_ENABLED": "false",
            "BALDUR_SCHEDULER_AUTOSTART": "0",
            "BALDUR_RETRY_MAX_ATTEMPTS": "2",
            "BALDUR_RETRY_BASE_DELAY": "0.1",
            "BALDUR_LOG_LEVEL": "WARNING",
            "PYTHONIOENCODING": "utf-8",
        }

        # When
        result = subprocess.run(
            [sys.executable, "-c", _STEADY_STATE_SCRIPT],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
            cwd=tmp_path,
        )

        # Then — the warm-up count proves the spy saw the factories at all
        assert result.returncode == 0, result.stderr[-2000:]
        assert _reported(result.stdout, "WARMUP_CONSTRUCTIONS") > 0
        assert _reported(result.stdout, "STEADY_CONSTRUCTIONS") == 0
