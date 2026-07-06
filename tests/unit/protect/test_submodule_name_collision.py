"""Regression gate: the inline ``baldur.protect(name, fn)`` idiom must resolve
to the **function** regardless of import order (impl doc 555 D3).

Before the ``protect.py`` -> ``protect_facade.py`` rename, the top-level name
``protect`` collided with a submodule **named** ``protect``. Touching any
protect-family attribute first (``@baldur.protected``, ``baldur.aprotect``, ...)
imported that submodule, and Python's import machinery bound it as a parent
attribute -- ``globals()["protect"] = <module>`` -- permanently shadowing the
lazily-resolved function. The documented inline form then raised
``TypeError: 'module' object is not callable``.

Subprocess isolation is **mandatory**: the shadow is process-global, so a
fresh interpreter is the only way to deterministically reproduce the
"decorator-first, then inline-call" order regardless of in-suite import
pollution from other tests (in-suite ``del sys.modules[...]`` also leaks across
pytest-xdist workers per UNIT_TEST_GUIDELINES.md §6.5.7). Pattern source:
``tests/unit/resilience/policies/test_hedging_circular_import.py``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# The four top-level marquee names (508 D3) that must stay symmetric: each
# resolves to a function, never a shadowing submodule. Only ``protect`` ever
# collided (no ``aprotect.py`` / ``protected.py`` / ``aprotected.py`` submodule
# exists), so the other three are a forward guard — if a future sibling
# submodule reintroduced the same collision class for any of them, this gate
# goes red.
_MARQUEE_NAMES = ("protect", "aprotect", "protected", "aprotected")


def _run_subprocess(snippet: str) -> subprocess.CompletedProcess:
    """Run a Python snippet in a clean subprocess (isolated ``sys.modules``)."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestProtectSubmoduleNameCollisionBehavior:
    """The inline idiom survives a decorator-first import order."""

    def test_inline_protect_resolves_after_decorator_use(self):
        """``baldur.protect(...)`` returns its value after ``@baldur.protected``.

        The decorator access loads the protect-family implementation module;
        the inline call that follows must resolve to the function (returning
        ``2``), not the shadowing submodule (which would raise ``TypeError``).
        """
        result = _run_subprocess(
            """
            import baldur

            # Decorator-first: reproduces the pre-555 shadow window.
            assert baldur.protected("a")(lambda: 1)() == 1

            # The documented inline idiom must still call the FUNCTION.
            value = baldur.protect("b", lambda: 2)
            assert value == 2, value
            print("INLINE-OK", value)
            """
        )
        assert result.returncode == 0, (
            f"subprocess exited with code {result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "TypeError" not in result.stderr, result.stderr
        assert "INLINE-OK 2" in result.stdout, (
            f"expected inline call to return 2\nstdout={result.stdout!r}\n"
            f"stderr={result.stderr!r}"
        )

    @pytest.mark.parametrize("name", _MARQUEE_NAMES)
    def test_marquee_name_resolves_to_non_module_callable(self, name):
        """Each of the four marquee names is a function after decorator-first use.

        The D3 test above proves the ``protect`` *call* works end-to-end. This
        locks the broader symmetry invariant (508 D3): after the protect-family
        submodule has loaded, ``baldur.<name>`` must be a callable that is not a
        ``types.ModuleType`` for every marquee name — the type-level property
        neither D3 (``protect`` only) nor the AGENTS-derived drift guard
        (inline-call names only) covers for ``aprotect``/``protected``/
        ``aprotected``. Subprocess-isolated because the failure mode it guards
        against (a name bound to a submodule) is process-global.
        """
        result = _run_subprocess(
            f"""
            import types
            import baldur

            # Decorator-first: load the protect-family submodule, reproducing
            # the window in which a same-named submodule could shadow the name.
            _ = baldur.protected

            attr = getattr(baldur, {name!r})
            assert callable(attr), ({name!r}, type(attr).__name__)
            assert not isinstance(attr, types.ModuleType), (
                {name!r}, type(attr).__name__
            )
            print("RESOLVED", {name!r}, type(attr).__name__)
            """
        )
        assert result.returncode == 0, (
            f"{name!r} did not resolve to a non-module callable\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert f"RESOLVED {name} " in result.stdout, (
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
