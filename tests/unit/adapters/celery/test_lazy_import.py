"""``baldur.adapters.celery`` PEP-562 lazy-import surface tests (#466 DBF3).

Pre-DBF3 the package eagerly imported ``beat_schedule``, which transitively
``import``-ed ``kombu``. Any environment with baldur installed without celery
extras (e.g., the testbed django_app) hit ``ModuleNotFoundError: kombu``
the moment a runbook handler touched ``baldur.adapters.celery``.

Post-DBF3 the package uses ``__getattr__``: ``kombu`` is required only when
celery internals are actually accessed.

Implementation note:
    Tests that assert on ``sys.modules`` membership at import time MUST run
    inside a subprocess. In-process ``del sys.modules[...]`` followed by a
    re-import pollutes the test runner's sys.modules and causes flakiness
    under ``pytest-xdist`` parallel execution (other tests in the same
    worker pick up stale module references — see
    UNIT_TEST_GUIDELINES.md §6.5.7). Pattern source:
    ``tests/unit/metrics/test_registry_no_prometheus.py`` (impl #460).
"""

from __future__ import annotations

import importlib
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


# =============================================================================
# Contract — module load does not pull heavy submodules into sys.modules
# =============================================================================


class TestCeleryAdapterLazyImportContract:
    """Bare ``import baldur.adapters.celery`` must NOT eager-load ``kombu``."""

    def test_bare_import_does_not_load_kombu_or_beat_schedule(self):
        """Pre-DBF3 regression: importing the package pulled in kombu."""
        result = _run_subprocess(
            """
            import sys
            import baldur.adapters.celery  # noqa: F401

            assert "kombu" not in sys.modules, (
                "baldur.adapters.celery must not transitively import kombu at module load"
            )
            assert "baldur.adapters.celery.beat_schedule" not in sys.modules
            print("OK")
            """
        )
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout

    def test_attribute_access_loads_submodule_on_demand(self):
        """Accessing a beat_schedule symbol triggers the lazy import."""
        result = _run_subprocess(
            """
            import sys
            import baldur.adapters.celery as celery_pkg

            assert "baldur.adapters.celery.beat_schedule" not in sys.modules

            # Touching a beat_schedule-backed symbol resolves the submodule.
            _ = celery_pkg.BALDUR_QUEUE_CONFIG
            assert "baldur.adapters.celery.beat_schedule" in sys.modules
            print("OK")
            """
        )
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout

    def test_lazy_import_caches_resolved_attribute(self):
        """After first access the symbol is cached in ``globals()``."""
        result = _run_subprocess(
            """
            import baldur.adapters.celery as celery_pkg

            # First access — flows through __getattr__.
            first = celery_pkg.baldur_task
            # Second access — comes from globals(), must be the same object.
            second = celery_pkg.baldur_task
            assert first is second
            print("OK")
            """
        )
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout

    def test_unknown_attribute_raises_attribute_error(self):
        """``__getattr__`` rejects names not in the lazy-import map."""
        celery_pkg = importlib.import_module("baldur.adapters.celery")
        with pytest.raises(AttributeError):
            _ = celery_pkg.does_not_exist  # type: ignore[attr-defined]

    def test_all_names_resolvable_via_lazy_getattr(self):
        """Every symbol in ``__all__`` resolves through ``__getattr__``."""
        celery_pkg = importlib.import_module("baldur.adapters.celery")
        unresolved: list[str] = []
        for name in celery_pkg.__all__:
            try:
                getattr(celery_pkg, name)
            except AttributeError:
                unresolved.append(name)
        assert unresolved == []
