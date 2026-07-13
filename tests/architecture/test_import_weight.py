"""G68 — ``import baldur`` stays lightweight: the hot-path barrels stay lazy.

Obtaining the marquee core types (``CircuitState`` / ``FailedOperationData``)
must not detonate the whole first-party import graph. The package ships a PEP 562
lazy ``__getattr__`` in every hot-path barrel (``core`` / ``utils`` / ``settings``
/ ``interfaces`` / ``metrics``); a single eager ``from baldur.X import ...`` at
the top of any of them silently re-defeats the lazy path and drags the full blob
back in — with zero signal from any other gate (the eager copy is locally clean,
acyclic, and correctly tiered).

The keep-eager marquee line in ``baldur/__init__.py`` binds two lightweight types
at import time, so EVERY ``baldur.*`` import inherits an 8-module stdlib-pure
chain as its floor (Python runs the parent ``baldur/__init__.py`` first).
``baldur.utils.time`` routes ``utc_now()`` through ``baldur.core.time_provider``,
so time_provider is part of that floor.

This gate imports each entrypoint in a FRESH subprocess (``sys.modules`` is
process-global, so an in-process assertion is vacuously green after any earlier
import in the session) and asserts the resulting ``baldur.*`` closure equals a
per-entrypoint EXACT allowlist. The assertion is set-membership, not wall-time:
an allowlist is strictly sharper than a count ceiling (a ceiling silently
tolerates partial eager re-introduction) and is deterministic where a ~450ms
timing bound is flaky. Do not "upgrade" it to a benchmark. Intentional eager
additions require a conscious edit to ``_ALLOWLISTS`` — designed ratchet behavior.

Rule registry:
``ARCHITECTURE.md#g68-import-weight``
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# The 8-module stdlib-pure floor inherited by every ``baldur.*`` import: the
# keep-eager marquee closure of ``baldur/__init__.py``.
_MARQUEE_CHAIN = frozenset(
    {
        "baldur",
        "baldur.interfaces",
        "baldur.interfaces.repositories",
        "baldur.core",
        "baldur.core.serializable",
        "baldur.core.time_provider",
        "baldur.utils",
        "baldur.utils.time",
    }
)

# Per-entrypoint EXACT allowed ``baldur.*`` closure (not a ceiling).
_ALLOWLISTS = {
    "baldur": _MARQUEE_CHAIN,
    # ``baldur.utils.time`` is in the chain; its floor is the same 8 modules —
    # in particular NO baldur.utils.async_logger, NO baldur.settings.*.
    "baldur.utils.time": _MARQUEE_CHAIN,
    # The base exception module (imported by dozens of internal files) adds only
    # itself on top of the unavoidable ``baldur/__init__.py`` floor.
    "baldur.core.exceptions": _MARQUEE_CHAIN | {"baldur.core.exceptions"},
}

# Import the target in a clean interpreter, then print the sorted first-party
# closure. ASCII-only prints avoid the Windows cp949 text-mode capture trap. The
# membership predicate is ``m == "baldur" or m.startswith("baldur.")`` — precise:
# bare ``startswith("baldur")`` would also capture baldur_pro / baldur_dormant.
_PROBE = """
import importlib
import sys

target = sys.argv[1]
importlib.import_module(target)
mods = sorted(m for m in sys.modules if m == "baldur" or m.startswith("baldur."))
print("MODS:" + ",".join(mods))
"""


@pytest.mark.parametrize("entrypoint", sorted(_ALLOWLISTS))
def test_import_stays_lightweight(entrypoint: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, entrypoint],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"import-weight probe crashed for {entrypoint!r} "
        f"(exit {result.returncode}):\n{result.stderr}"
    )

    out = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    assert out.startswith("MODS:"), (
        f"unexpected probe output for {entrypoint!r}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    loaded = frozenset(m for m in out[len("MODS:") :].split(",") if m)
    allowed = _ALLOWLISTS[entrypoint]
    assert loaded == allowed, (
        f"import-weight regression for {entrypoint!r}: the baldur.* import "
        f"closure drifted from the {len(allowed)}-module allowlist.\n"
        f"  unexpectedly loaded (eager barrel re-introduced?): "
        f"{sorted(loaded - allowed)}\n"
        f"  missing (allowlist stale?): {sorted(allowed - loaded)}\n"
        "Intentional additions require a conscious edit to _ALLOWLISTS."
    )
