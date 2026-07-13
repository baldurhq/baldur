"""G67 — spec-less mock debt MUST shrink monotonically (mock-safety ratchet).

`unittest.mock` does not raise on a mistyped attribute of a spec-less mock, so a
`MagicMock()` whose `.notiffyyy()` call is a typo passes silently — the exact
shape of a green-but-asserts-nothing AI-generated test. The mock-safety
guideline (§6.2) already mandates `autospec=`/`spec=` for new tests, but that
rule was paper-only; this gate mechanizes the mechanizable slice of it as a
G41-style **exact-match ratchet** so the spec-less mass can never grow silently.

Two independent ratchet counters, one budget map each:

1. **Spec-less mock creation (A1)** — a `Mock` / `MagicMock` / `AsyncMock` call
   carrying **none of** {a positional first argument (= `spec`), `spec=`,
   `spec_set=`, `wraps=`}. `MagicMock(Real)` (positional spec) and
   `Mock(wraps=obj)` both raise `AttributeError` on a missing attribute, so they
   are spec-equivalent and exempt. A kwargs-only creation (`Mock(return_value=X)`)
   counts — exempting it would let any kwarg dodge the ratchet.

2. **Spec-less decorator patch (A2)** — a decorator-form `@patch(...)` /
   `@patch.object(...)` (on a `def` / `async def` / `class`) carrying **none of**
   {`autospec=`, `spec=`, `spec_set=`, `new_callable=`, an explicit `new`
   replacement (positional or `new=`)}. An explicit `new` means no auto-mock is
   created at all, so there is nothing to spec. `with`-form patches are NOT
   covered: the singleton-patch exception (§6.5.2) *mandates* non-autospec for
   with-form singleton patches, and a singleton target is a semantic category
   unreachable from the AST (patch targets are dotted strings), so any with-form
   ratchet would count mandated-legitimate sites. The decorator form carries no
   such mandate — §6.5.2 marks decorator+autospec-singleton as the anti-pattern —
   so the decorator-form ratchet is structurally collision-free. With-form
   patches stay judgment-territory (§6.4 mock-boundary clause + `/review`).

**Exact-match ratchet, both directions** (G41 precedent):

- `count > budget` fails ("new spec-less debt — add spec/autospec or justify"):
  new code cannot add a spec-less mock invisibly.
- `count < budget` fails ("lower the budget"): a test edit that removes a
  spec-less mock MUST ratchet its budget down in the same commit, so the freed
  slack can never be silently reclaimed. The `>=` floor is rejected for that
  slack-reclaim leak. "Monotonic" means *no silent growth* — a deliberate,
  justified spec-less creation (§6.2 Exception 1, dynamic-attribute mocks) is a
  visible budget edit with a justification comment, reviewed at the diff.

**Per-root budget (OSS-only-checkout robust).** The budget is a per-root map, not
a single scalar. The scan target is the test tree — the `oss` root (this file's
own `tests/oss` / the public `tests` root, excluding the `factories/` and
`testapp/` support subdirs so the two repo layouts stay semantically
equivalent), plus the sibling `pro` and `dormant` roots. The public repo ships
only the `oss` root; the private `pro` / `dormant` roots are absent there and
skipped (precedent: G20/G21/G38/G39/G41 are all OSS-only-checkout robust). Each
present root is checked against its own budget; each test edit lowers exactly the
budget of the root it touched.

**Callee resolution via a per-file import-alias map.** Both counters resolve the
callee through the file's own imports so alias evasion is closed: bare names
(`MagicMock()` after `from unittest.mock import MagicMock`), renamed imports
(`from unittest.mock import MagicMock as MM` → `MM()`), and attribute-leaf forms
on a module binding (`mock.Mock()` after `from unittest import mock`;
`um.Mock()` after `import unittest.mock as um`) all resolve. A
`from unittest.mock import *` wildcard binds every class + `patch`.

**Out of scope** (near-zero prevalence; each needs runtime / whole-program
resolution the AST cannot do): imperative `mocker.*` (pytest-mock) patches and
`mocker.MagicMock()` creations, `mock_add_spec` post-hoc spec injection,
`create_autospec` / `PropertyMock` (different names/semantics),
`patch.dict` / `patch.multiple`, and custom `@patch`-wrapping decorators.

**Known limitations** (review-caught, G41-style). Presence-check only:
`autospec=False` / `spec=None` pass the detector (perverse, review-caught). A
helper factory that builds a mock counts at the helper's own creation site.
Dynamically-built patch targets are invisible.

Rule registry:
``ARCHITECTURE.md#g67-mock-spec-ratchet``
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.architecture.conftest import OSS_TESTS_ROOT, parse_ast, walk_src

# The three mock classes whose spec-less creation this gate ratchets. Deliberately
# excludes NonCallable*/PropertyMock/create_autospec (different safety semantics).
_MOCK_CLASSES = frozenset({"Mock", "MagicMock", "AsyncMock"})

# Keyword arguments that make a mock creation spec-safe (a positional first
# argument counts separately as the spec).
_CREATION_SPEC_KWARGS = frozenset({"spec", "spec_set", "wraps"})

# Keyword arguments that make a decorator `@patch` spec-safe. `new` (explicit
# replacement object) is included because it suppresses auto-mock creation
# entirely; its positional form is handled separately per patch kind.
_PATCH_SPEC_KWARGS = frozenset({"autospec", "spec", "spec_set", "new_callable", "new"})

# Support subdirs excluded from the `oss` root: fixture/support code, not test
# suites. Present as immediate children of the public `tests/` root only; a no-op
# under the private `tests/oss/` root (which does not contain them). Excluding
# them keeps the private and public `oss` roots semantically equivalent.
_OSS_ROOT_EXCLUDE = frozenset({"factories", "testapp"})

# Inline monotonic-DECREASING per-root budgets, measured at landing. Two maps,
# one per counter. Lower the relevant entry (never raise it) whenever a test edit
# removes a spec-less mock from that root — the exact-match assertion enforces it.
# A deliberate, justified new spec-less creation (§6.2 Exception 1) is a budget
# +1 with a justification comment, reviewed at the diff.
#
# The `oss` budget is this repo's test tree. The `pro` / `dormant` roots do not
# exist in this repo and are skipped; their entries are inert.
_MOCK_CREATION_BUDGETS: dict[str, int] = {
    "oss": 4394,
    "pro": 1817,
    "dormant": 401,
}
_DECORATOR_PATCH_BUDGETS: dict[str, int] = {
    "oss": 719,
    "pro": 434,
    "dormant": 144,
}


def _root_paths() -> dict[str, tuple[Path, frozenset[str]]]:
    """Resolve each budgeted root to its ``(path, excluded-top-level-dirs)``.

    Layout-robust: resolved from ``OSS_TESTS_ROOT`` (``tests/oss`` in the private
    repo, ``tests`` in the public repo). The ``pro`` / ``dormant`` roots are
    siblings of the ``oss`` root; they are absent on the public checkout and the
    counter skips them.
    """
    return {
        "oss": (OSS_TESTS_ROOT, _OSS_ROOT_EXCLUDE),
        "pro": (OSS_TESTS_ROOT.parent / "pro", frozenset()),
        "dormant": (OSS_TESTS_ROOT.parent / "dormant", frozenset()),
    }


def _iter_test_files(root: Path, exclude_top: frozenset[str]) -> Iterator[Path]:
    """Yield `.py` files under ``root``, skipping the ``exclude_top`` subtrees.

    ``exclude_top`` names immediate child directories of ``root`` whose whole
    subtree is skipped (matched on the first path component relative to ``root``).
    """
    for path in walk_src((root,)):
        if exclude_top:
            try:
                first = path.relative_to(root).parts[0]
            except (ValueError, IndexError):
                first = ""
            if first in exclude_top:
                continue
        yield path


def _import_alias_maps(
    tree: ast.Module,
) -> tuple[dict[str, str], set[str], set[str]]:
    """Return ``(class_aliases, module_aliases, patch_aliases)`` for one file.

    * ``class_aliases`` — local name → canonical mock class, from
      ``from unittest.mock import MagicMock [as X]``.
    * ``module_aliases`` — local names bound to the ``unittest.mock`` module
      (``from unittest import mock`` → ``{"mock"}``; ``import unittest.mock as um``
      → ``{"um"}``), used to resolve attribute-leaf callees (``mock.Mock(...)``).
    * ``patch_aliases`` — local names bound to ``patch``, from
      ``from unittest.mock import patch [as X]``.

    A ``from unittest.mock import *`` wildcard binds every mock class + ``patch``.
    """
    class_aliases: dict[str, str] = {}
    module_aliases: set[str] = set()
    patch_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "unittest.mock":
                for alias in node.names:
                    if alias.name == "*":
                        for cls in _MOCK_CLASSES:
                            class_aliases.setdefault(cls, cls)
                        patch_aliases.add("patch")
                        continue
                    local = alias.asname or alias.name
                    if alias.name in _MOCK_CLASSES:
                        class_aliases[local] = alias.name
                    elif alias.name == "patch":
                        patch_aliases.add(local)
            elif node.module == "unittest":
                for alias in node.names:
                    if alias.name == "mock":
                        module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "unittest.mock" and alias.asname:
                    module_aliases.add(alias.asname)
    return class_aliases, module_aliases, patch_aliases


def _is_mock_creation(
    call: ast.Call, class_aliases: dict[str, str], module_aliases: set[str]
) -> bool:
    """True when ``call`` constructs a ``Mock`` / ``MagicMock`` / ``AsyncMock``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in class_aliases
    if isinstance(func, ast.Attribute):
        return (
            func.attr in _MOCK_CLASSES
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        )
    return False


def _creation_is_specless(call: ast.Call) -> bool:
    """True when a mock-creation ``call`` supplies no spec-equivalent argument.

    A positional first argument (the ``spec`` slot), or a ``spec=`` / ``spec_set=``
    / ``wraps=`` keyword, makes it spec-safe; anything else (bare, or kwargs-only
    like ``return_value=X``) is spec-less.
    """
    if call.args:
        return False
    return not any(kw.arg in _CREATION_SPEC_KWARGS for kw in call.keywords)


def _patch_decorator_kind(
    deco: ast.expr, patch_aliases: set[str], module_aliases: set[str]
) -> str | None:
    """Return ``"patch"`` / ``"patch.object"`` for a patch decorator call, else None.

    Resolves the bare-name form (``@patch(...)``), the attribute-leaf form on a
    module binding (``@mock.patch(...)``), and both ``.object`` variants.
    """
    if not isinstance(deco, ast.Call):
        return None
    func = deco.func
    if isinstance(func, ast.Name):
        return "patch" if func.id in patch_aliases else None
    if isinstance(func, ast.Attribute):
        if func.attr == "object":
            inner = func.value
            if isinstance(inner, ast.Name) and inner.id in patch_aliases:
                return "patch.object"
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "patch"
                and isinstance(inner.value, ast.Name)
                and inner.value.id in module_aliases
            ):
                return "patch.object"
            return None
        if (
            func.attr == "patch"
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        ):
            return "patch"
    return None


def _patch_new_positional_index(kind: str) -> int:
    """The positional index of the ``new`` argument for a patch ``kind``.

    ``patch(target, new, ...)`` → index 1; ``patch.object(target, attribute,
    new, ...)`` → index 2. A positional beyond this index is an explicit ``new``
    replacement, which suppresses auto-mock creation and is spec-safe.
    """
    return 2 if kind == "patch.object" else 1


def _patch_is_specless(deco: ast.Call, kind: str) -> bool:
    """True when a patch decorator supplies no spec-safe / explicit-object form."""
    if any(kw.arg in _PATCH_SPEC_KWARGS for kw in deco.keywords):
        return False
    return len(deco.args) <= _patch_new_positional_index(kind)


def _count_specless_creations_in_tree(tree: ast.Module) -> int:
    class_aliases, module_aliases, _ = _import_alias_maps(tree)
    if not class_aliases and not module_aliases:
        return 0
    total = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _is_mock_creation(node, class_aliases, module_aliases)
            and _creation_is_specless(node)
        ):
            total += 1
    return total


def _count_specless_patches_in_tree(tree: ast.Module) -> int:
    _, module_aliases, patch_aliases = _import_alias_maps(tree)
    if not patch_aliases and not module_aliases:
        return 0
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for deco in node.decorator_list:
            kind = _patch_decorator_kind(deco, patch_aliases, module_aliases)
            if kind is not None and _patch_is_specless(deco, kind):
                total += 1
    return total


def count_specless_mock_creations(
    root: Path, exclude_top: frozenset[str] = frozenset()
) -> int:
    """Count spec-less ``Mock``/``MagicMock``/``AsyncMock`` creations under ``root``.

    Pure over its ``root`` argument, so the non-vacuity tests inject a synthetic
    ``tmp_path`` tree instead of scanning the real test tree.
    """
    total = 0
    for path in _iter_test_files(root, exclude_top):
        tree = parse_ast(path)
        if tree is not None:
            total += _count_specless_creations_in_tree(tree)
    return total


def count_specless_decorator_patches(
    root: Path, exclude_top: frozenset[str] = frozenset()
) -> int:
    """Count spec-less decorator ``@patch``/``@patch.object`` under ``root`` (pure)."""
    total = 0
    for path in _iter_test_files(root, exclude_top):
        tree = parse_ast(path)
        if tree is not None:
            total += _count_specless_patches_in_tree(tree)
    return total


def ratchet_verdict(actual: int, budget: int, unit: str) -> str | None:
    """Return a failure reason when ``actual`` does not exactly match ``budget``.

    ``None`` means in-budget. Above budget is new debt (add spec/autospec or
    justify); below budget is unratcheted slack (lower the budget so it cannot be
    silently reclaimed).
    """
    if actual > budget:
        return (
            f"new spec-less {unit} debt ({actual} > budget {budget}) — "
            "add spec/autospec or justify (§6.2 Exception 1)"
        )
    if actual < budget:
        return (
            f"you reduced {unit} debt ({actual} < budget {budget}) — "
            f"lower the budget to {actual}"
        )
    return None


class TestMockSpecRatchet:
    """G67 — spec-less mock creation + decorator-patch debt is exact-match-ratcheted."""

    def test_mock_creation_budget_exact_match(self):
        mismatches: list[str] = []
        for name, (root, exclude) in _root_paths().items():
            if not root.exists():
                continue  # absent private root on an OSS-only checkout
            verdict = ratchet_verdict(
                count_specless_mock_creations(root, exclude),
                _MOCK_CREATION_BUDGETS[name],
                "mock-creation",
            )
            if verdict is not None:
                mismatches.append(f"{name}: {verdict}")
        assert not mismatches, (
            "G67: spec-less mock-creation budget mismatch. The budget is an "
            "exact-match ratchet — it only ever moves down, in lockstep with test "
            "edits that add a spec/autospec. Registry: "
            "ARCHITECTURE.md#g67-mock-spec-ratchet\n" + "\n".join(mismatches)
        )

    def test_decorator_patch_budget_exact_match(self):
        mismatches: list[str] = []
        for name, (root, exclude) in _root_paths().items():
            if not root.exists():
                continue
            verdict = ratchet_verdict(
                count_specless_decorator_patches(root, exclude),
                _DECORATOR_PATCH_BUDGETS[name],
                "decorator-patch",
            )
            if verdict is not None:
                mismatches.append(f"{name}: {verdict}")
        assert not mismatches, (
            "G67: spec-less decorator-patch budget mismatch. The budget is an "
            "exact-match ratchet — it only ever moves down, in lockstep with test "
            "edits that add autospec/spec/new_callable. Registry: "
            "ARCHITECTURE.md#g67-mock-spec-ratchet\n" + "\n".join(mismatches)
        )

    # -- Non-vacuity: both directions, per counter ------------------------------

    def _write(self, tmp_path: Path, src: str) -> Path:
        (tmp_path / "m.py").write_text(src, encoding="utf-8")
        return tmp_path

    def test_creation_over_budget_input_fails(self, tmp_path):
        """A new spec-less creation is counted and flagged as new debt."""
        root = self._write(
            tmp_path,
            "from unittest.mock import MagicMock\nx = MagicMock()\n",
        )
        count = count_specless_mock_creations(root)
        assert count == 1
        assert ratchet_verdict(count, 0, "mock-creation") is not None

    def test_creation_under_budget_input_fails(self):
        assert ratchet_verdict(0, 5, "mock-creation") is not None
        assert ratchet_verdict(3, 5, "mock-creation") is not None

    def test_creation_exact_match_passes(self):
        assert ratchet_verdict(5, 5, "mock-creation") is None

    def test_decorator_patch_over_budget_input_fails(self, tmp_path):
        """A new spec-less decorator patch is counted and flagged as new debt."""
        root = self._write(
            tmp_path,
            "from unittest.mock import patch\n"
            '@patch("mod.Target")\n'
            "def test_x(m):\n    pass\n",
        )
        count = count_specless_decorator_patches(root)
        assert count == 1
        assert ratchet_verdict(count, 0, "decorator-patch") is not None

    def test_decorator_patch_under_budget_input_fails(self):
        assert ratchet_verdict(0, 5, "decorator-patch") is not None
        assert ratchet_verdict(3, 5, "decorator-patch") is not None

    def test_decorator_patch_exact_match_passes(self):
        assert ratchet_verdict(5, 5, "decorator-patch") is None

    # -- Detection-semantics matrices (one per counter) -------------------------

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            # Spec-less → counted.
            ("from unittest.mock import MagicMock\nx = MagicMock()\n", 1),
            ("from unittest.mock import Mock\nx = Mock(return_value=5)\n", 1),
            # Spec-equivalent → exempt.
            ("from unittest.mock import MagicMock\nx = MagicMock(spec=Foo)\n", 0),
            (
                "from unittest.mock import MagicMock\nx = MagicMock(spec_set=Foo)\n",
                0,
            ),
            ("from unittest.mock import Mock\nx = Mock(wraps=obj)\n", 0),
            ("from unittest.mock import MagicMock\nx = MagicMock(Foo)\n", 0),
            # Alias / attribute-leaf / wildcard callee resolution → counted.
            (
                "from unittest.mock import MagicMock as MM\nx = MM()\n",
                1,
            ),
            ("from unittest import mock\nx = mock.Mock()\n", 1),
            ("import unittest.mock as um\nx = um.AsyncMock()\n", 1),
            ("from unittest.mock import *\nx = MagicMock()\n", 1),
            # Non-mock callee, and an unresolved attribute leaf → not counted.
            ("x = some_factory()\n", 0),
            ("x = self.MagicMock()\n", 0),
        ],
    )
    def test_creation_detection_semantics(self, tmp_path, body, expected):
        root = self._write(tmp_path, body)
        assert count_specless_mock_creations(root) == expected

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            # Spec-less decorator patch → counted.
            (
                'from unittest.mock import patch\n@patch("m.T")\ndef test_x(m):\n    pass\n',
                1,
            ),
            (
                "from unittest.mock import patch\n"
                '@patch.object(C, "meth")\ndef test_x(m):\n    pass\n',
                1,
            ),
            (
                "from unittest import mock\n"
                '@mock.patch("m.T")\ndef test_x(m):\n    pass\n',
                1,
            ),
            (
                'from unittest.mock import patch\n@patch("m.T")\nclass TestX:\n    pass\n',
                1,
            ),
            # Spec-safe / explicit-object forms → exempt.
            (
                "from unittest.mock import patch\n"
                '@patch("m.T", autospec=True)\ndef test_x(m):\n    pass\n',
                0,
            ),
            (
                "from unittest.mock import patch\n"
                '@patch("m.T", spec=Foo)\ndef test_x(m):\n    pass\n',
                0,
            ),
            (
                "from unittest.mock import patch\n"
                '@patch("m.T", new_callable=PropertyMock)\ndef test_x(m):\n    pass\n',
                0,
            ),
            (
                "from unittest.mock import patch\n"
                '@patch("m.T", sentinel.X)\ndef test_x():\n    pass\n',
                0,
            ),
            (
                "from unittest.mock import patch\n"
                '@patch("m.T", new=sentinel.X)\ndef test_x():\n    pass\n',
                0,
            ),
            (
                "from unittest.mock import patch\n"
                '@patch.object(C, "meth", sentinel.X)\ndef test_x():\n    pass\n',
                0,
            ),
            # Out of scope: with-form patch (not a decorator) and patch.dict.
            (
                'from unittest.mock import patch\nwith patch("m.T"):\n    pass\n',
                0,
            ),
            (
                "from unittest.mock import patch\n"
                '@patch.dict("m.D", {})\ndef test_x():\n    pass\n',
                0,
            ),
        ],
    )
    def test_decorator_patch_detection_semantics(self, tmp_path, body, expected):
        root = self._write(tmp_path, body)
        assert count_specless_decorator_patches(root) == expected
