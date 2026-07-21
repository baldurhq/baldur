"""Unit tests for core/process_utils.py — gunicorn process detection.

Three helpers gate signal handlers and background-thread lifecycle:

- ``is_gunicorn_worker()`` — GUNICORN_WORKER env-var-based, set by
  ``post_worker_init``. Phase-dependent (False in worker pre-post_worker_init).
- ``is_under_gunicorn()`` — SERVER_SOFTWARE-based, set by gunicorn's
  master and inherited by workers via fork(). Phase-independent.
  Use for signal-handler guards.
- ``is_gunicorn_master()`` — composite: ``is_under_gunicorn() and not
  is_gunicorn_worker()``. Use for "skip in master" gating.
"""

from __future__ import annotations

from unittest.mock import patch

from baldur.core.process_utils import (
    is_gunicorn_master,
    is_gunicorn_worker,
    is_under_gunicorn,
)


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
