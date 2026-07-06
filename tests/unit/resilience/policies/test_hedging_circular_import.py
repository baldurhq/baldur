"""``baldur.resilience.policies.hedging`` cold-start module-load contract (#503).

Pre-#503 the OSS module top-imported four submodules of
``baldur_pro.services.hedging``, each of which transitively triggers
``baldur_pro.services.hedging.__init__`` — which in turn re-imports
``AsyncHedgingPolicy / HedgingPolicy / HedgingConfigUpdateHook`` from the
*still-loading* ``baldur.resilience.policies.hedging`` module. The result was
``ImportError: cannot import name 'AsyncHedgingPolicy' from partially
initialized module ...`` on any cold-start entry point that didn't pre-load
the PRO hedging package.

The standard pytest-django invocation masked the cycle because
``tests/testapp/settings.py`` → ``AppConfig.ready()`` → ``baldur.init()`` →
``baldur.bootstrap`` pre-loads ``baldur_pro.services.hedging.shutdown`` before
any OSS-side hedging import runs. Outside that path — a slim ``python -c``
import, ``pytest -p no:django``, or any non-Django entry point — the cycle
fires immediately.

#503 relocated the four offending imports to AFTER all class definitions in
``hedging.py`` so the re-export in ``baldur_pro.services.hedging.__init__``
resolves against a fully-classes-bound module. This file is the regression
gate: subprocesses run with a clean ``sys.modules`` (no Django bootstrap, no
prior ``baldur_pro.services.hedging`` import) and assert each entry point
loads cleanly.

Implementation note:
    Subprocess isolation is mandatory here. Asserting on a "fresh" import in
    the same interpreter requires ``del sys.modules[...]`` which leaks across
    pytest-xdist workers (UNIT_TEST_GUIDELINES.md §6.5.7). Pattern source:
    ``tests/unit/adapters/celery/test_lazy_import.py`` (impl #466 DBF3).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


def _run_subprocess(snippet: str) -> subprocess.CompletedProcess:
    """Run a Python snippet in a clean subprocess.

    Use a subprocess so sys.modules mutations don't leak into the
    pytest-xdist worker's module cache.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        timeout=30,
    )


# Stderr patterns that indicate a load-time failure OR a Python interpreter
# shutdown-sequence GC failure (NoneType-attribute errors against partially
# torn-down modules). Exit code 0 alone misses the shutdown-sequence path.
_FAILURE_PATTERNS = (
    "Traceback",
    "Exception ignored in",
    "ImportError",
    "ModuleNotFoundError",
    "Error:",
)


def _assert_clean(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, (
        f"subprocess exited with code {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    for pattern in _FAILURE_PATTERNS:
        assert pattern not in result.stderr, (
            f"stderr contains forbidden pattern {pattern!r}:\n{result.stderr}"
        )
    assert "LOAD-OK" in result.stdout


# =============================================================================
# Contract — cold-start module load must not trigger the OSS<->PRO cycle
# =============================================================================


class TestHedgingModuleLoadContract:
    """Three entry paths exercise the cold-start load surface end-to-end."""

    @pytest.mark.parametrize(
        "snippet",
        [
            pytest.param(
                """
                from baldur.resilience.policies.hedging import HedgingPolicy
                assert HedgingPolicy is not None
                print("LOAD-OK")
                """,
                id="direct-hedging-import",
            ),
            pytest.param(
                """
                from baldur.resilience.policies.presets import ha_pipeline
                assert ha_pipeline is not None
                print("LOAD-OK")
                """,
                id="presets-ha-pipeline-import",
            ),
            pytest.param(
                """
                import baldur.resilience.policies.hedging as _hedging_mod
                assert _hedging_mod.HedgingPolicy is not None
                print("LOAD-OK")
                """,
                id="bare-package-attribute-access",
            ),
        ],
    )
    def test_cold_start_load_does_not_trigger_circular_import(self, snippet):
        """No prior ``baldur_pro.services.hedging`` load — must succeed cleanly."""
        result = _run_subprocess(snippet)
        _assert_clean(result)
