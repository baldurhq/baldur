"""Nothing baldur emits goes through structlog's unconfigured default.

Until ``configure_structlog()`` has run, structlog's default configuration
prints every level to stdout regardless of ``BALDUR_LOG_LEVEL`` — so a line
emitted at import time, or from a public entry point an application reaches
before ``baldur.init()``, would print unfiltered on every install. The rule
(the logging standard's library/host boundary) is that no module emits at
import and every such entry point configures logging first. This measures
it: ``import baldur``, every adapter package, and the Celery connector a
quickstart's app module calls before any worker signal, each in a child
interpreter with the ``BALDUR_*`` environment stripped, must write nothing
to either stream.

Mock-based: no infrastructure, no ``init()`` — the whole point is what
happens before it.
"""

from __future__ import annotations

import os
import pkgutil
import subprocess
import sys

import pytest

_CHILD_TIMEOUT_SECONDS = 120


def _adapter_packages() -> list[str]:
    import baldur.adapters

    return sorted(
        f"baldur.adapters.{module.name}"
        for module in pkgutil.iter_modules(baldur.adapters.__path__)
        if module.ispkg
    )


def _child_environment() -> dict[str, str]:
    """A bare install's environment: every ``BALDUR_*`` variable removed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BALDUR_")}
    env.pop("REDIS_URL", None)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_child(script: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        env=_child_environment(),
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    # Decode explicitly: text=True picks the console codepage on Windows and
    # can silently yield an empty stream on a decode failure.
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    assert completed.returncode == 0, output
    return output


# One child imports the whole set: the packages that cannot import bare (a
# missing extra, a framework that needs settings) are skipped inside the
# child, so a skip is reported by name rather than hidden as an empty run.
_IMPORT_ALL = (
    "import importlib\n"
    "skipped = []\n"
    "for name in {names!r}:\n"
    "    try:\n"
    "        importlib.import_module(name)\n"
    "    except ImportError as exc:\n"
    "        skipped.append((name, type(exc).__name__))\n"
    "import sys\n"
    "sys.stderr.write('')\n"
)


class TestImportEmitsNothing:
    def test_import_baldur_writes_nothing(self):
        assert _run_child("import baldur\n") == ""

    def test_every_adapter_package_imports_silently(self):
        packages = _adapter_packages()
        assert packages, "the adapter package set must not collapse to nothing"

        output = _run_child(_IMPORT_ALL.format(names=packages))

        assert output == "", output

    @pytest.mark.parametrize("package", _adapter_packages())
    def test_each_adapter_package_alone_imports_silently(self, package):
        """One package per child: an emission is attributed to the package
        whose import triggered it, not to whichever came first in a shared
        interpreter."""
        script = (
            "import importlib\n"
            "try:\n"
            f"    importlib.import_module({package!r})\n"
            "except ImportError:\n"
            "    pass\n"
        )

        assert _run_child(script) == ""


class TestCeleryConnectorConfiguresFirst:
    """``setup_baldur_signals(app)`` runs at the app module's import, before
    any worker signal reaches ``init()``; its own INFO line must not print
    through the unconfigured default."""

    def test_setup_baldur_signals_writes_nothing(self):
        pytest.importorskip("celery")
        script = (
            "from celery import Celery\n"
            "from baldur.adapters.celery import setup_baldur_signals\n"
            "setup_baldur_signals(Celery('probe'))\n"
        )

        assert _run_child(script) == ""

    def test_configure_baldur_celery_writes_nothing(self):
        pytest.importorskip("celery")
        script = (
            "from celery import Celery\n"
            "from baldur.adapters.celery.beat_schedule import configure_baldur_celery\n"
            "configure_baldur_celery(Celery('probe'))\n"
        )

        assert _run_child(script) == ""
