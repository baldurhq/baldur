#!/usr/bin/env python3
"""Test-value audit — flag low-value test candidates by AST signal (per
the unit test guidelines §9).

Single, repeatable sweep behind the §9 "what earns a test's place" lens. It is
FLAG-ONLY: it never edits or deletes a test. A human disposes of each candidate.

Categories (mapped 1:1 to §9 subsections):

  §9.1  empty-ish       a ``test_*`` body with no assertion signal AND no SUT
                        call — asserts nothing, exercises nothing. Remove.

  §9.2  self-asserted   builds a value from literals, then asserts on that same
                        literal, never calling the SUT — a tautology. Remove
                        (or make it call the SUT).

  §9.3  smoke (KEEP)    no explicit assert but a real SUT call — "does not
                        raise" IS the contract (fail-open / no-op / graceful
                        degradation). Reported for transparency, NOT a removal
                        candidate.

  §9.4  duplicate       bodies that are structurally identical once literals are
                        normalized away → one ``parametrize``, not N copies.
                        Reported as clusters.

  §9.5  weak delegation the sole ``assert call() == <lit>`` re-asserts a mock's
                        own configured return value → verifies pass-through but
                        not that arguments were forwarded. Strengthen with an
                        ``assert_called_*`` on the forwarded args.

Exit code: 1 if any §9.1 / §9.2 candidate is found (those should be zero — the
"remove" cats are gate-able), else 0. §9.3 / §9.4 / §9.5 are advisory and never
affect the exit code.

Usage:
    python scripts/audit_test_value.py [tests/]            # full tree (default)
    python scripts/audit_test_value.py tests/unit      # one subtree
    python scripts/audit_test_value.py --report out.md     # write full markdown
    python scripts/audit_test_value.py --json              # machine-readable
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = "tests"

# --- assertion-signal vocabulary -------------------------------------------
MOCK_ASSERT_ATTRS = {
    "assert_called",
    "assert_called_once",
    "assert_called_with",
    "assert_called_once_with",
    "assert_not_called",
    "assert_has_calls",
    "assert_any_call",
    "assert_awaited",
    "assert_awaited_once",
    "assert_awaited_with",
    "assert_awaited_once_with",
    "assert_not_awaited",
}
RAISES_CTX = {
    "raises",
    "assertRaises",
    "assertRaisesRegex",
    "warns",
    "assertWarns",
    "assertWarnsRegex",
    "assertLogs",
    "assertRaisesMessage",
    "deprecated_call",
}
# A call to one of these (by name or trailing attr) counts as an assertion so a
# test that asserts through a helper is NOT mis-flagged as assertion-free.
HELPER_PREFIXES = ("assert", "verify", "expect", "ensure", "fail")

# Builtins that may be called inside a self-asserted-literal test without it
# counting as a SUT call (they operate on the test's own local literals).
SAFE_CALL_NAMES = {
    "isinstance",
    "issubclass",
    "len",
    "str",
    "int",
    "float",
    "bool",
    "dict",
    "list",
    "tuple",
    "set",
    "frozenset",
    "bytes",
    "type",
    "hasattr",
    "getattr",
    "repr",
    "abs",
    "sorted",
    "any",
    "all",
    "sum",
    "min",
    "max",
    "round",
}
# Names that may appear (loaded) in an assert of a self-asserted-literal test
# without disqualifying it — builtin types/predicates.
SAFE_LOAD_NAMES = SAFE_CALL_NAMES | {"None", "True", "False"}


def _norm_dump(node: ast.AST) -> str:
    """``ast.dump`` with every Constant collapsed to one token (so tests that
    differ only in literals hash identically) — the §9.4 cluster key."""

    class _Strip(ast.NodeTransformer):
        def visit_Constant(self, n: ast.Constant) -> ast.AST:  # noqa: N802
            return ast.copy_location(ast.Constant(value="<C>"), n)

    stripped = _Strip().visit(ast.parse(ast.unparse(node)))
    return ast.dump(stripped, annotate_fields=False)


def _is_assertion_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in MOCK_ASSERT_ATTRS:
            return True
        if func.attr.lower().startswith(HELPER_PREFIXES):
            return True
    return isinstance(func, ast.Name) and func.id.lower().startswith(HELPER_PREFIXES)


def _ctx_is_raises(item: ast.withitem) -> bool:
    ce = item.context_expr
    if isinstance(ce, ast.Call):
        f = ce.func
        if isinstance(f, ast.Attribute) and f.attr in RAISES_CTX:
            return True
        if isinstance(f, ast.Name) and f.id in RAISES_CTX:
            return True
    return False


def count_assertions(fn: ast.AST) -> tuple[int, bool]:
    """Return ``(assertion_signal_count, has_any_non_assert_call)``."""
    asserts = 0
    has_call = False
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            asserts += 1
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            if any(_ctx_is_raises(it) for it in n.items):
                asserts += 1
        elif isinstance(n, ast.Call):
            if _is_assertion_call(n):
                asserts += 1
            else:
                has_call = True
    return asserts, has_call


# --- §9.2 self-asserted literal --------------------------------------------
def _is_literal_value(node: ast.AST, literal_names: set[str]) -> bool:
    """True if ``node`` is composed solely of constants / collections of
    constants / names already known to be literal-bound."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal_value(e, literal_names) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (k is None or _is_literal_value(k, literal_names))
            and _is_literal_value(v, literal_names)
            for k, v in zip(node.keys, node.values, strict=False)
        )
    if isinstance(node, ast.UnaryOp):
        return _is_literal_value(node.operand, literal_names)
    if isinstance(node, ast.Name):
        return node.id in literal_names
    return False


def _only_safe_builtin_calls(fn: ast.AST) -> bool:
    """True if every call in the body is to a safe builtin (no possible SUT)."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if not (isinstance(f, ast.Name) and f.id in SAFE_CALL_NAMES):
                return False
    return True


def _exercises_sut(fn: ast.AST) -> bool:
    """True if the body uses a raises/warns context (a real SUT exercise)."""
    return any(
        isinstance(n, (ast.With, ast.AsyncWith))
        and any(_ctx_is_raises(it) for it in n.items)
        for n in ast.walk(fn)
    )


def _literal_bound_names(fn: ast.AST) -> set[str]:
    """Names assigned (in walk order) only to literal expressions."""
    names: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            tgt = n.targets[0]
            if isinstance(tgt, ast.Name):
                if _is_literal_value(n.value, names):
                    names.add(tgt.id)
                else:
                    names.discard(tgt.id)
    return names


def is_self_asserted_literal(fn: ast.AST) -> bool:
    """A test that asserts on values it built from literals, never calling the
    SUT (only safe builtins) — verifies only that assignment works (§9.2)."""
    asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
    if not asserts or _exercises_sut(fn) or not _only_safe_builtin_calls(fn):
        return False
    literal_names = _literal_bound_names(fn)
    if not literal_names:
        return False

    # Every name loaded inside an assert must be literal-bound or a safe builtin,
    # and at least one literal-bound name must actually be asserted upon.
    touched_literal = False
    for a in asserts:
        for sub in ast.walk(a):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id in literal_names:
                    touched_literal = True
                elif sub.id not in SAFE_LOAD_NAMES:
                    return False
    return touched_literal


# --- §9.5 weak delegation (mock-tautology) ---------------------------------
def _mock_return_literals(fn: ast.AST) -> set:
    """Literals fed to a mock as its return value, as ``(type, value)`` pairs.

    Keying by type as well as value keeps ``bool`` distinct from ``int`` — in
    Python ``1 == True`` and ``0 == False``, so a value-only set would match an
    ``assert len(x) == 1`` against a ``return_value=True`` mock (false §9.5)."""
    lits: set = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == "return_value"
                    and isinstance(n.value, ast.Constant)
                ):
                    lits.add((type(n.value.value), n.value.value))
        if isinstance(n, ast.Call):
            for kw in n.keywords:
                if kw.arg == "return_value" and isinstance(kw.value, ast.Constant):
                    lits.add((type(kw.value.value), kw.value.value))
    return lits


def is_weak_delegation(fn: ast.AST) -> bool:
    """Sole ``assert <call> == <lit>`` (or reversed) where ``<lit>`` is a
    configured mock return value in the same test, and there is no
    ``assert_called_*`` pinning the forwarded args (§9.5)."""
    assert_nodes = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
    if len(assert_nodes) != 1:
        return False
    # If the test already pins the call, it is not weak.
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in MOCK_ASSERT_ATTRS
        ):
            return False
    test = assert_nodes[0].test
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
    ):
        return False
    left, right = test.left, test.comparators[0]
    lits = _mock_return_literals(fn)
    if not lits:
        return False

    def is_lit(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and (type(node.value), node.value) in lits

    def has_call(node: ast.AST) -> bool:
        return any(isinstance(d, ast.Call) for d in ast.walk(node))

    return (is_lit(right) and has_call(left)) or (is_lit(left) and has_call(right))


def iter_test_funcs(tree: ast.AST):
    """Yield ``(qualname, node)`` for every ``test_*`` function."""
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test_"):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(
                    sub, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and sub.name.startswith("test_"):
                    yield f"{node.name}::{sub.name}", sub


def audit(root: Path) -> dict:
    files = sorted(root.rglob("test_*.py"))
    total = 0
    empty: list[str] = []
    smoke: list[str] = []
    self_lit: list[str] = []
    weak: list[str] = []
    cluster_map: dict[str, list[str]] = defaultdict(list)
    parse_errors: list[str] = []

    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            parse_errors.append(f"{f.as_posix()}: {exc}")
            continue
        rel = f.as_posix()
        for qual, fn in iter_test_funcs(tree):
            total += 1
            loc = f"{rel}:{fn.lineno}  {qual}"
            n_assert, has_call = count_assertions(fn)
            if n_assert == 0:
                (smoke if has_call else empty).append(loc)
            elif is_self_asserted_literal(fn):
                self_lit.append(loc)
            if is_weak_delegation(fn):
                weak.append(loc)
            key = _norm_dump(fn)
            if len(key) >= 220:
                cluster_map[key].append(loc)

    clusters = sorted(
        (locs for locs in cluster_map.values() if len(locs) >= 2),
        key=len,
        reverse=True,
    )
    return {
        "root": root.as_posix(),
        "total": total,
        "empty": empty,
        "smoke": smoke,
        "self_lit": self_lit,
        "weak": weak,
        "clusters": clusters,
        "parse_errors": parse_errors,
    }


def render_markdown(r: dict) -> str:
    dup_total = sum(len(c) for c in r["clusters"])
    out = [f"# Test-value audit (§9) — `{r['root']}`\n"]
    out.append(f"- Test functions scanned: **{r['total']}**")
    out.append(f"- §9.1 empty-ish (remove): **{len(r['empty'])}**")
    out.append(f"- §9.2 self-asserted literal (remove): **{len(r['self_lit'])}**")
    out.append(f"- §9.3 smoke / does-not-raise (KEEP): **{len(r['smoke'])}**")
    out.append(
        f"- §9.4 duplicate-cluster members (consolidate): "
        f"**{dup_total}** in **{len(r['clusters'])}** clusters"
    )
    out.append(f"- §9.5 weak delegation (strengthen): **{len(r['weak'])}**")
    removal = len(r["empty"]) + len(r["self_lit"])
    out.append(f"\n- **Removal candidates (§9.1+§9.2): {removal}**")

    def section(title, items, limit=200):
        out.append(f"\n## {title} ({len(items)})\n")
        for it in items[:limit]:
            out.append(f"- {it}")
        if len(items) > limit:
            out.append(f"- … +{len(items) - limit} more")

    section("§9.1 — empty-ish (strongest delete candidates)", r["empty"])
    section("§9.2 — self-asserted literal (delete or call the SUT)", r["self_lit"])
    section("§9.5 — weak delegation (add assert_called_*)", r["weak"])
    section("§9.3 — smoke / does-not-raise (KEEP — informational)", r["smoke"], 80)

    out.append(f"\n## §9.4 — duplicate clusters ({len(r['clusters'])})\n")
    for c in r["clusters"]:
        out.append(f"\n**cluster size {len(c)}** → one parametrize:")
        for loc in c[:10]:
            out.append(f"  - {loc}")
        if len(c) > 10:
            out.append(f"  - … +{len(c) - 10} more in cluster")
    if r["parse_errors"]:
        out.append(f"\n## parse skips ({len(r['parse_errors'])})\n")
        out.extend(f"- {e}" for e in r["parse_errors"])
    return "\n".join(out)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Audit test value (§9)")
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT, help="Test root")
    parser.add_argument("--report", help="Write full markdown report to this path")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    if not root.exists():
        print(f"ERROR: {root} does not exist", file=sys.stderr)
        return 2

    r = audit(root)

    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        md = render_markdown(r)
        if args.report:
            Path(args.report).write_text(md, encoding="utf-8")
            print(md.split("\n## ", 1)[0])
            print(f"\nFull report: {args.report}")
        else:
            print(md)

    return 1 if (r["empty"] or r["self_lit"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
