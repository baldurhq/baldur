"""G52 — Framework independence: framework-free imports MUST NOT load the Django API.

CLAUDE.md § Pattern Compliance — framework independence + no import-time side
effects. ``baldur/api/__init__.py`` historically eager-imported
``baldur.api.django``, which pulls DRF serializers AND — via the module-level
``pool_circuit_breaker = PoolCircuitBreaker()`` singleton — spawned a background
refresh thread at import time. Because importing the framework-free
``baldur.api.middleware`` runs its parent package ``baldur.api.__init__`` first,
every FastAPI / Flask process (whose adapter imports ``baldur.api.middleware``)
transitively loaded the entire Django API and started a thread that, when Django
was installed-but-unconfigured, logged ``ImproperlyConfigured`` /
``background_refresh_failing_consecutively`` every tick.

The fix makes ``baldur.api.__init__`` lazy-import ``django`` via PEP 562
``__getattr__`` and moves the thread start out of import time. This gate locks
it: in a fresh subprocess (so no prior in-process import pollutes
``sys.modules``), importing each framework-free entrypoint MUST leave
``baldur.api.django`` — and every ``baldur.api.django.*`` submodule — absent.

Subprocess isolation is required: any earlier test in the session that imported
``baldur.api.django`` would make an in-process check pass vacuously.

Rule registry:
``ARCHITECTURE.md#g52-framework-independence``
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Probe: import the target module in a clean interpreter, then report any
# ``baldur.api.django`` module that ended up in sys.modules. ImportError of the
# target itself (optional framework extra absent) is reported as SKIP so the
# gate is meaningful in an OSS-only checkout that lacks FastAPI / Flask.
_PROBE = """
import importlib
import sys

target = sys.argv[1]
try:
    importlib.import_module(target)
except ImportError as exc:
    print("SKIP:" + str(exc))
    raise SystemExit(0)

leaked = sorted(
    name
    for name in sys.modules
    if name == "baldur.api.django" or name.startswith("baldur.api.django.")
)
print("LEAKED:" + ",".join(leaked))
"""

# Framework-free entrypoints that must never transitively load the Django API.
# - ``baldur.api.middleware`` is the direct framework-free surface and needs no
#   optional extra, so it is the always-on assertion (red before the fix).
# - The FastAPI / Flask adapters are the real user-facing path; they SKIP when
#   their extra is not installed.
_ENTRYPOINTS = [
    "baldur.api.middleware",
    "baldur.adapters.fastapi",
    "baldur.adapters.flask",
]


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_framework_free_import_does_not_load_django_api(entrypoint: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, entrypoint],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"G52 probe subprocess crashed for {entrypoint!r} "
        f"(exit {result.returncode}):\n{result.stderr}"
    )

    out = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if out.startswith("SKIP:"):
        pytest.skip(f"{entrypoint} not importable in this env: {out[len('SKIP:') :]}")

    assert out.startswith("LEAKED:"), (
        f"G52: unexpected probe output for {entrypoint!r}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    leaked = [name for name in out[len("LEAKED:") :].split(",") if name]
    assert not leaked, (
        f"G52: importing {entrypoint!r} transitively loaded the Django API "
        f"(framework-independence leak): {leaked}. "
        "baldur.api.__init__ must lazy-import django (PEP 562 __getattr__), and "
        "baldur.api.django must not be pulled in by any framework-free module."
    )
