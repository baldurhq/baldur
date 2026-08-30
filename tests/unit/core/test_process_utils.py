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
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from baldur.core import process_utils
from baldur.core.process_utils import (
    fork_repaired,
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
)

_SERVING_ENV_VAR = process_utils._CELERY_WORKER_SERVING_ENV_VAR


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
