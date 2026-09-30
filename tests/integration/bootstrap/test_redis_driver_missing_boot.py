"""``baldur.init()`` with ``BALDUR_REDIS_URL`` set and no Redis driver installed.

Before the driver check, production died deep inside startup with a bare
``ModuleNotFoundError`` from the first Redis adapter built — a message naming no
fix, and an exception class the Celery ``worker_init`` receiver does not turn
into ``SystemExit``, so a worker could come up half-initialised. Now the Group A
phase checks the driver once: production refuses with ``ConfigurationError``
naming ``BALDUR_REDIS_URL`` and the extra to install; elsewhere the Redis-backed
stores run on memory and the whole boot completes.

The whole ``init()`` runs in a child process with ``sys.modules["redis"] =
None`` set before Baldur is imported, which makes every ``import redis`` fail
the way a missing package does (a meta-path blocker would also break
``find_spec``). The child sees no parent ``BALDUR_*`` variable and no ``.env``
(it runs in ``tmp_path`` beside an empty one, with ``PYTHON_DOTENV_DISABLED``).
No Redis server is needed.

Test Categories:
    A. Production: the refusal's class and text.
    B. Development: the boot completes with the Redis-backed stores on memory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_CHILD_TIMEOUT_SECONDS = 120
_RESULT_MARKER = "REDIS_DRIVER_BOOT_RESULT="
_STRIPPED_PREFIXES = ("BALDUR_", "DJANGO_")
_STRIPPED_NAMES = frozenset({"FALLBACK_POLICY"})

_CHILD = f"""
import json, sys
sys.modules["redis"] = None
import baldur
from baldur.factory.registry import ProviderRegistry

outcome = {{"raised": None, "message": None}}
try:
    baldur.init()
except BaseException as exc:
    outcome["raised"] = f"{{type(exc).__module__}}.{{type(exc).__qualname__}}"
    outcome["message"] = str(exc)
outcome["defaults"] = {{
    attr: getattr(ProviderRegistry, attr).get_default_name()
    for attr in ("cache", "event_journal_repo", "failed_op_repo")
}}
print({_RESULT_MARKER!r} + json.dumps(outcome))
"""


def _boot_without_the_driver(environment: str, tmp_path: Path) -> dict:
    """Run ``init()`` in an isolated child with no Redis driver; return its report."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_STRIPPED_PREFIXES) and name not in _STRIPPED_NAMES
    }
    env.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "BALDUR_ENVIRONMENT": environment,
            "BALDUR_REDIS_URL": "redis://127.0.0.1:1/0",
        }
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith(_RESULT_MARKER)
    ]
    assert len(lines) == 1, (
        f"no report line (exit {completed.returncode})\n"
        f"stdout tail:\n{completed.stdout[-2000:]}\n"
        f"stderr tail:\n{completed.stderr[-3000:]}"
    )
    return json.loads(lines[0][len(_RESULT_MARKER) :])


# =============================================================================
# A. Production
# =============================================================================


class TestRedisDriverMissingProductionBootIntegration:
    """Production refuses with the class every adapter's startup aborts on."""

    def test_production_boot_refuses_with_a_configuration_error_naming_the_fix(
        self, tmp_path
    ):
        """
        Purpose:
            A production boot with the URL set and no driver fails as a
            ConfigurationError, not a ModuleNotFoundError (SC7).
        Expected:
            - the raised class is baldur's ConfigurationError
            - the message names BALDUR_REDIS_URL and the redis extra
        """
        report = _boot_without_the_driver("production", tmp_path)

        assert report["raised"] == "baldur.core.exceptions.ConfigurationError"
        assert "BALDUR_REDIS_URL" in report["message"]
        assert "pip install baldur-framework[redis]" in report["message"]


# =============================================================================
# B. Development
# =============================================================================


class TestRedisDriverMissingDevelopmentBootIntegration:
    """Outside production the whole boot completes on memory."""

    def test_development_boot_completes_with_the_redis_stores_on_memory(self, tmp_path):
        """
        Purpose:
            No later step of init() reaches for the missing driver: the cache,
            the event journal and the dead-letter chain all avoid Redis.
        Expected:
            - init() returns
            - cache, event journal and dead-letter store are memory
        """
        report = _boot_without_the_driver("development", tmp_path)

        assert report["raised"] is None, report["message"]
        assert report["defaults"] == {
            "cache": "memory",
            "event_journal_repo": "memory",
            "failed_op_repo": "memory",
        }
