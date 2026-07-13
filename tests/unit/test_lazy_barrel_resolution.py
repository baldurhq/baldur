"""Lazy-barrel resolution suite.

The five hot-path barrels (``baldur.core`` / ``baldur.utils`` /
``baldur.settings`` / ``baldur.interfaces`` / ``baldur.metrics``) were converted
from eager re-export barrels to the PEP 562 lazy ``__getattr__`` pattern. This
suite is the static integrity guard for that conversion — the
``__all__``-declaration gate and the first-party-import-target gate both skip
``__getattr__`` modules by design, so nothing else catches a typo'd
``_LAZY_IMPORTS`` entry or a drift between the dict, the ``__all__`` surface, and
the ``TYPE_CHECKING`` mirror until first attribute access.

Per converted barrel:
    (a) every ``__all__`` name getattr-resolves;
    (b) ``_LAZY_IMPORTS`` keys == the public surface (``__all__``) plus the
        recorded non-public stragglers;
    (c) the ``if TYPE_CHECKING:`` mirror (AST-parsed — the block is
        runtime-invisible) binds exactly the ``_LAZY_IMPORTS`` keys;
    (d) an unknown attribute raises AttributeError naming the module;
    (e) ``__dir__()`` returns ``list(__all__)``;
    (f) a broken lazy target propagates the underlying ImportError — the
        ``__getattr__`` must not mask an import failure as AttributeError;
    (g) the barrel reflects the *live* source-submodule attribute (no
        ``globals()`` memoization) — a value patched onto the submodule is seen
        through the barrel and is not shadowed by a value cached from a prior
        access. This is what keeps ``mock.patch`` from leaking across tests.

The ``TYPE_CHECKING`` mirror is additionally load-bearing for the published
reference: griffe resolves per-symbol ``::: baldur.interfaces.<X>`` directives
through it statically, so a missing mirror line breaks a rendered docs page
(guarded by check (c)).
"""

import ast
import importlib
import re
from pathlib import Path
from unittest.mock import patch

import pytest

BARRELS = [
    "baldur.core",
    "baldur.utils",
    "baldur.settings",
    "baldur.interfaces",
    "baldur.metrics",
]

# Non-public names carried in ``_LAZY_IMPORTS`` beyond ``__all__``: real
# barrel-path consumers exist, so they stay resolvable, but they are not
# advertised in ``__all__`` / ``__dir__``. Hardcoded spec constants — a new
# straggler must be a conscious edit here (ratchet).
EXPECTED_STRAGGLERS = {
    "baldur.core": frozenset(),
    "baldur.utils": frozenset(),
    "baldur.settings": frozenset(
        {"FallbackPolicy", "get_security_thresholds", "get_sla_thresholds"}
    ),
    "baldur.interfaces": frozenset(),
    "baldur.metrics": frozenset(),
}


def _typechecking_mirror_names(module) -> set[str]:
    """Names bound by ImportFrom inside the module's ``if TYPE_CHECKING:`` block.

    ``TYPE_CHECKING`` is False at runtime, so the mirror is invisible to
    introspection and must be read from source via AST.
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and (
            isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        ):
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.ImportFrom):
                    for alias in stmt.names:
                        names.add(alias.asname or alias.name)
    return names


@pytest.mark.parametrize("barrel_name", BARRELS)
class TestLazyBarrelContract:
    """_LAZY_IMPORTS structure, surface coverage, and mirror consistency."""

    def test_lazy_imports_cover_public_surface_plus_stragglers(self, barrel_name):
        """(b) dict keys == __all__ ∪ recorded stragglers (no gap, no surprise)."""
        module = importlib.import_module(barrel_name)
        keys = set(module._LAZY_IMPORTS)
        public = set(module.__all__)
        stragglers = EXPECTED_STRAGGLERS[barrel_name]

        missing = public - keys
        assert not missing, (
            f"{barrel_name}: __all__ names absent from _LAZY_IMPORTS: {sorted(missing)}"
        )
        unexpected = keys - public - stragglers
        assert not unexpected, (
            f"{barrel_name}: undeclared straggler(s) in _LAZY_IMPORTS: {sorted(unexpected)} "
            "— add to EXPECTED_STRAGGLERS only if a real barrel-path consumer needs it"
        )

    def test_lazy_import_values_are_module_attr_tuples(self, barrel_name):
        """Each value is a (baldur module path, attr name) 2-tuple of strings."""
        module = importlib.import_module(barrel_name)
        for name, value in module._LAZY_IMPORTS.items():
            assert isinstance(value, tuple), f"{name}: {value!r}"
            assert len(value) == 2, f"{name}: {value!r}"
            module_path, attr_name = value
            assert isinstance(module_path, str)
            assert module_path.startswith("baldur.")
            assert isinstance(attr_name, str)
            assert attr_name

    def test_typechecking_mirror_matches_lazy_imports(self, barrel_name):
        """(c) AST-parsed TYPE_CHECKING mirror binds exactly the dict keys."""
        module = importlib.import_module(barrel_name)
        mirror = _typechecking_mirror_names(module)
        keys = set(module._LAZY_IMPORTS)
        assert mirror == keys, (
            f"{barrel_name}: TYPE_CHECKING mirror drifted from _LAZY_IMPORTS.\n"
            f"  only in mirror: {sorted(mirror - keys)}\n"
            f"  only in dict:   {sorted(keys - mirror)}"
        )


@pytest.mark.parametrize("barrel_name", BARRELS)
class TestLazyBarrelBehavior:
    """__getattr__ resolution, __dir__, and error propagation."""

    def test_every_public_name_resolves(self, barrel_name):
        """(a) getattr resolves every __all__ name without raising."""
        module = importlib.import_module(barrel_name)
        failures = []
        for name in module.__all__:
            try:
                getattr(module, name)
            except Exception as exc:  # noqa: BLE001 - report every failure together
                failures.append(f"{name}: {exc!r}")
        assert not failures, f"{barrel_name}: unresolved names:\n" + "\n".join(failures)

    def test_dir_returns_all(self, barrel_name):
        """(e) __dir__() == list(__all__) (stragglers stay unadvertised)."""
        module = importlib.import_module(barrel_name)
        assert module.__dir__() == list(module.__all__)

    def test_unknown_name_raises_attribute_error_naming_module(self, barrel_name):
        """(d) an unknown attribute raises AttributeError naming the module."""
        module = importlib.import_module(barrel_name)
        with pytest.raises(AttributeError, match=re.escape(barrel_name)):
            module.__getattr__("_definitely_not_a_real_symbol_xyz")

    def test_broken_target_propagates_import_error(self, barrel_name):
        """(f) a broken lazy target raises ImportError, not AttributeError.

        Guards the verbatim ``__getattr__`` contract against a future "fix" that
        swallows import failures and re-raises AttributeError.
        """
        module = importlib.import_module(barrel_name)
        probe = "_broken_lazy_probe"
        module._LAZY_IMPORTS[probe] = ("baldur._nonexistent_barrel_module_xyz", "Foo")
        try:
            with pytest.raises(ImportError):
                module.__getattr__(probe)
        finally:
            module._LAZY_IMPORTS.pop(probe, None)
            module.__dict__.pop(probe, None)

    def test_reflects_live_submodule_no_memoization(self, barrel_name):
        """(g) The barrel reflects the live submodule attr — no globals() cache.

        A memoizing __getattr__ would cache the first-resolved value in the
        module dict and shadow later submodule patches, leaking mock.patch across
        tests. Warm the name first (which would populate any such cache), then
        patch the source submodule and require the barrel to see the new value.
        """
        module = importlib.import_module(barrel_name)
        name = next(iter(module._LAZY_IMPORTS))
        module_path, attr_name = module._LAZY_IMPORTS[name]
        submodule = importlib.import_module(module_path)

        getattr(module, name)  # warm: a globals() cache, if any, is now populated
        sentinel = object()
        with patch.object(submodule, attr_name, sentinel):
            assert getattr(module, name) is sentinel
        assert getattr(module, name) is not sentinel
