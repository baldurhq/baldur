"""G87 — a body that already exists twice cannot be added a third time silently.

Two copies of a function body are the correct, reversible state; the third is
the extraction trigger (the rule of three). That rule was paper-only: every
copy is locally clean — ruff, import-cycle and tier gates all stay green — so
the class is invisible to every other fitness function, and the count it
depends on came from recall. Recall found the founding family at forty-two
copies, not three. This gate supplies the count.

**Population.** Every ``def`` / ``async def`` under a source root whose body
normalizes to at least ``CLONE_TOKEN_FLOOR`` tokens (identifiers alpha-renamed,
string constants collapsed to their type, docstring skipped — see the scanner
module). Bodies with identical streams form a cluster; a cluster of
``RECURRENCE_THRESHOLD`` or more members is an *at-threshold family*.

**Budget unit = total members across at-threshold families, per root.** A
cluster *count* cannot see the 43rd copy joining a 42-member family (the count
stays 1); the member total moves on both events — a new family reaching three
(+3) and any growth of an existing one (+1).

**Exact-match ratchet, both directions** (G41 / G67 precedent):

- ``total > budget`` fails: a body that existed twice was added a third time,
  or an at-threshold family grew. Extract the shape, or raise the budget with a
  justification comment reviewed at the diff. Every cluster counts — a new
  service shipping the mandated ``get_*()`` / ``reset_*()`` singleton pair trips
  the gate and is a visible ``+1`` with a comment, never a silent landing. A
  name-keyed exemption was rejected: a name does not guarantee a role, so a real
  duplicate named ``get_...`` would be permanently invisible.
- ``total < budget`` fails: a refactor that removed members MUST lower the
  budget in the same commit, so freed slack cannot be silently reclaimed.

**Fail-closed.** A file the parser cannot read and a directory the walk cannot
list both fail the gate outright. Under an exact-match ratchet a silently
dropped file lowers the total, and the gate would then instruct the developer
to lower the budget — baking the loss in.

**Per-root budget (OSS-only-checkout robust).** The budget is a per-root map;
this file carries the ``src/baldur`` entry and is skipped where that root is
not in the checkout (the repo that ships the source runs it). The private
trees have their own half with its own map. Clusters form over the roots one
checkout holds.

**Known limitations.**

- *Net change, not each event.* A scalar sees the commit's net movement: a
  commit that deletes ``k`` members from at-threshold families and adds ``k``
  elsewhere passes without a budget edit. A family shrinking below three
  contributes 0, not its remainder, so the arithmetic is narrower than it
  looks; every additions-only commit is fully caught, and the mixed commit is
  the ``refactor`` + ``feat`` shape the commit convention already splits.
- *Cross-checkout families.* A shape with two copies in the public tree and
  one in a private tree reaches three in neither checkout. Measured at
  landing: 74 members sit in families that reach three only when both
  checkouts are seen together.
- *Keyword order.* ``Thread(name=..., daemon=True)`` and
  ``Thread(daemon=True, name=...)`` are different streams; the thread-spawner
  family therefore shows as two clusters, not one.
- *Symlinked directories.* The walk does not follow a directory symlink (a
  cycle guard), and ``os.walk`` reports nothing for one; files below it are
  neither counted nor named. No source tree holds one.

Rule registry:
``ARCHITECTURE.md#g87-clone-recurrence-ratchet``
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from tests.architecture._clone_scan import (
    CloneMember,
    CloneScan,
    format_family,
    normalized_body_tokens,
    scan_roots,
)
from tests.architecture.conftest import PROJECT_ROOT, parse_ast

_SRC = PROJECT_ROOT / "src"
_RULE_ANCHOR = "#g87-clone-recurrence-ratchet"

# Inline exact-match per-root budget: the total member count across
# at-threshold families under the root, measured at landing. Lower the entry
# whenever a refactor removes members; raise it only with a justification
# comment reviewed at the diff (a mandated singleton pair, a deliberate copy).
_ROOT_BUDGETS: dict[str, int] = {
    # Landing measurement: 106 families, floor 40 tokens; the two largest are
    # the 33-member service-singleton getter and the crash-capture wrapper.
    # Re-measured when the encoding gained field-boundary tokens (floor 56):
    # 105 families — the one 3-member ``__str__`` family that sat at exactly
    # the old floor fell below the new one; no family split.
    "baldur": 575,
}

# The budget half needs the OSS source on disk; the fixture half below is pure
# tmp-tree analysis and runs anywhere, so the skip is per class.
_needs_oss_source = pytest.mark.skipif(
    not (_SRC / "baldur").is_dir(),
    reason="OSS-source scan runs in the public repo; baldur is a pip sibling here",
)


def fail_closed_reasons(scan: CloneScan) -> list[str]:
    """Every file or directory the scan could not count — each one fails the gate."""
    reasons = [
        f"unparsed file (cannot count what cannot be read): {p}" for p in scan.unparsed
    ]
    reasons.extend(
        f"unlistable directory (its files never reached the parser): {d}"
        for d in scan.unlistable
    )
    return reasons


def clone_budget_verdict(
    actual: int,
    budget: int,
    *,
    root: str,
    families: tuple[tuple[CloneMember, ...], ...],
) -> str | None:
    """Return a failure reason when ``actual`` does not exactly match ``budget``.

    ``None`` means in-budget. Above budget lists every at-threshold family
    touching ``root``, smallest first — the scalar cannot tell which family
    moved, but the developer knows what they just added and finds it by name.
    Below budget is unratcheted slack: lower the budget so it cannot be
    silently reclaimed.
    """
    if actual > budget:
        listing = "\n  ".join(
            format_family(family)
            for family in families
            if any(member.root == root for member in family)
        )
        return (
            f"{root}: clone mass grew ({actual} > budget {budget}) — a body that "
            "already existed twice was added a third time, or an at-threshold "
            "family grew. Extract the shape (rule of three) or raise the budget "
            "with a justification comment. At-threshold families in this root, "
            f"smallest first:\n  {listing}"
        )
    if actual < budget:
        return (
            f"{root}: clone mass shrank ({actual} < budget {budget}) — "
            f"lower the budget to {actual}"
        )
    return None


def budget_mismatches(
    budgets: dict[str, int], roots: dict[str, Path], relative_to: Path
) -> tuple[list[str], list[str]]:
    """Return ``(fail_closed_reasons, budget_verdicts)`` for the given roots."""
    scan = scan_roots(roots, relative_to=relative_to)
    blocked = fail_closed_reasons(scan)
    totals = scan.members_per_root()
    verdicts = []
    for name in roots:
        verdict = clone_budget_verdict(
            totals.get(name, 0), budgets[name], root=name, families=scan.families
        )
        if verdict is not None:
            verdicts.append(verdict)
    return blocked, verdicts


@_needs_oss_source
class TestCloneRecurrenceRatchet:
    """G87 — at-threshold clone mass under ``src/baldur`` is exact-match-ratcheted."""

    def test_clone_member_budget_exact_match(self):
        roots = {name: _SRC / name for name in _ROOT_BUDGETS}
        blocked, verdicts = budget_mismatches(_ROOT_BUDGETS, roots, PROJECT_ROOT)
        assert not blocked, (
            "G87: the scan could not read part of the tree, so no count is "
            "trustworthy (fail-closed). Registry: ARCHITECTURE.md"
            f"{_RULE_ANCHOR}\n" + "\n".join(blocked)
        )
        assert not verdicts, (
            "G87: clone-recurrence budget mismatch. The budget is an exact-match "
            "ratchet over the member total of at-threshold families; it moves "
            "only with a justified edit in the same commit. Registry: "
            f"ARCHITECTURE.md{_RULE_ANCHOR}\n" + "\n".join(verdicts)
        )


# -- Non-vacuity fixtures ---------------------------------------------------------
#
# The crash-capture wrapper that founded the rule (107 normalized tokens). Each
# copy renames the method, the delegated call and the handle attribute — the
# differences alpha-renaming must absorb.
_CRASH_CAPTURE = """\
class Worker:
    def _{name}_loop_with_crash_capture(self) -> None:
        try:
            self._{name}_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._{handle} is not None:
                self._{handle}.record_crash(e)
            raise
"""

# The mandated service-singleton pair (getter 75 tokens; the 13-token reset
# body sits below the floor). Counted like every other cluster — no exemption.
_SINGLETON_PAIR = """\
import threading

_{name}: object | None = None
_{name}_lock = threading.Lock()


def get_{name}() -> object:
    global _{name}
    if _{name} is None:
        with _{name}_lock:
            if _{name} is None:
                _{name} = object()
    return _{name}


def reset_{name}() -> None:
    global _{name}
    _{name} = None
"""


def _write_pkg(tmp_path: Path, files: dict[str, str]) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for name, source in files.items():
        target = pkg / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return pkg


def _scan_pkg(tmp_path: Path) -> CloneScan:
    return scan_roots({"pkg": tmp_path / "pkg"}, relative_to=tmp_path)


class TestCloneRecurrenceRatchetFixtures:
    """Non-vacuity: the shapes that shipped are the shapes this fires on."""

    def test_third_copy_fails_and_names_both_existing_sites(self, tmp_path):
        _write_pkg(
            tmp_path,
            {
                "a.py": _CRASH_CAPTURE.format(name="update", handle="handle"),
                "b.py": _CRASH_CAPTURE.format(name="refresh", handle="sender_handle"),
                "c.py": _CRASH_CAPTURE.format(name="writer", handle="health_handle"),
            },
        )
        scan = _scan_pkg(tmp_path)
        assert scan.members_per_root() == {"pkg": 3}
        verdict = clone_budget_verdict(3, 0, root="pkg", families=scan.families)
        assert verdict is not None
        assert "pkg/a.py:2 Worker._update_loop_with_crash_capture" in verdict
        assert "pkg/b.py:2 Worker._refresh_loop_with_crash_capture" in verdict
        assert "pkg/c.py:2 Worker._writer_loop_with_crash_capture" in verdict

    def test_second_copy_is_not_a_family(self, tmp_path):
        """Two copies is the correct state — the gate counts nothing below three."""
        _write_pkg(
            tmp_path,
            {
                "a.py": _CRASH_CAPTURE.format(name="update", handle="handle"),
                "b.py": _CRASH_CAPTURE.format(name="refresh", handle="sender_handle"),
            },
        )
        scan = _scan_pkg(tmp_path)
        assert scan.families == ()
        assert clone_budget_verdict(0, 0, root="pkg", families=scan.families) is None

    def test_mandated_pair_still_counts(self, tmp_path):
        """A new service's ``get_*``/``reset_*`` pair trips the gate — no exemptions."""
        _write_pkg(
            tmp_path,
            {
                "alpha.py": _SINGLETON_PAIR.format(name="alpha_service"),
                "beta.py": _SINGLETON_PAIR.format(name="beta_service"),
                "gamma.py": _SINGLETON_PAIR.format(name="gamma_service"),
            },
        )
        scan = _scan_pkg(tmp_path)
        # The getter clusters; the reset body is below the floor and does not.
        assert scan.members_per_root() == {"pkg": 3}
        verdict = clone_budget_verdict(3, 0, root="pkg", families=scan.families)
        assert verdict is not None
        assert "get_alpha_service" in verdict
        assert "get_beta_service" in verdict
        assert "get_gamma_service" in verdict
        assert "reset_" not in verdict

    def test_unratcheted_slack_fails_with_lower_the_budget(self):
        verdict = clone_budget_verdict(3, 5, root="pkg", families=())
        assert verdict is not None
        assert "lower the budget to 3" in verdict
        assert clone_budget_verdict(5, 5, root="pkg", families=()) is None

    def test_unreadable_file_fails_closed(self, tmp_path):
        _write_pkg(
            tmp_path,
            {
                "a.py": _CRASH_CAPTURE.format(name="update", handle="handle"),
                "broken.py": "def not_python(:\n    pass\n",
            },
        )
        scan = _scan_pkg(tmp_path)
        assert scan.unparsed == ("pkg/broken.py",)
        reasons = fail_closed_reasons(scan)
        assert len(reasons) == 1
        assert "pkg/broken.py" in reasons[0]

    def test_unlistable_dir_fails_closed(self, tmp_path, monkeypatch):
        """A directory the walk cannot list is reported, not skipped.

        ``Path.rglob`` swallows the ``PermissionError`` of an unlistable
        directory and yields only its listable siblings; the scan must surface
        it. A real ACL fixture is not portable (``chmod 000`` is inert on
        Windows), so ``os.scandir`` is patched for one subdirectory.
        """
        _write_pkg(
            tmp_path,
            {
                "open/a.py": _CRASH_CAPTURE.format(name="update", handle="handle"),
                "locked/b.py": _CRASH_CAPTURE.format(name="refresh", handle="handle"),
            },
        )
        real_scandir = os.scandir

        def denying_scandir(path=".", *args):
            if Path(path).name == "locked":
                raise PermissionError(13, "listing denied", str(path))
            return real_scandir(path, *args)

        monkeypatch.setattr(os, "scandir", denying_scandir)
        scan = _scan_pkg(tmp_path)
        assert [Path(d) for d in scan.unlistable] == [tmp_path / "pkg" / "locked"]
        reasons = fail_closed_reasons(scan)
        assert len(reasons) == 1
        assert "locked" in reasons[0]

    def test_normalizer_is_read_only(self, tmp_path):
        """Normalizing twice yields the same stream and leaves the cached tree untouched."""
        pkg = _write_pkg(
            tmp_path,
            {
                "m.py": (
                    '"""Module docstring."""\n'
                    "class Outer:\n"
                    '    """Class docstring."""\n'
                    "    def method(self, x: int) -> str:\n"
                    '        """Method docstring."""\n'
                    "        def inner(y):\n"
                    '            """Inner docstring."""\n'
                    '            return f"{y!r}-{x}"\n'
                    "        try:\n"
                    "            return inner(x) + self.name\n"
                    "        except (KeyError, ValueError) as e:\n"
                    '            self.log.warning("outer.method_failed", error=e)\n'
                    "            raise\n"
                )
            },
        )
        tree = parse_ast(pkg / "m.py")
        assert tree is not None
        before_dump = ast.dump(tree, include_attributes=True)
        before_attrs = [sorted(vars(node)) for node in ast.walk(tree)]
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        first = [normalized_body_tokens(f) for f in funcs]
        second = [normalized_body_tokens(f) for f in funcs]
        assert first == second
        assert ast.dump(tree, include_attributes=True) == before_dump
        assert [sorted(vars(node)) for node in ast.walk(tree)] == before_attrs
        assert all(ast.get_docstring(f) is not None for f in funcs)
        assert parse_ast(pkg / "m.py") is tree

    def test_string_constant_collapses_but_number_does_not(self):
        """The level decision: a log-event string is not a difference; a number is."""

        def body(event: str, timeout: float) -> ast.FunctionDef:
            module = ast.parse(
                f"def f(self):\n"
                f"    self.log.warning({event!r}, timeout={timeout!r})\n"
                f"    return self.handle.wait({timeout!r})\n"
            )
            return module.body[0]  # type: ignore[return-value]

        assert normalized_body_tokens(
            body("a.b_failed", 5.0)
        ) == normalized_body_tokens(body("c.d_timeout", 5.0))
        assert normalized_body_tokens(
            body("a.b_failed", 5.0)
        ) != normalized_body_tokens(body("a.b_failed", 30.0))
