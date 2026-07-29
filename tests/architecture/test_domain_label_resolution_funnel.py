"""Architectural fitness function — every ``.labels(domain=…)`` write MUST
pass its value through ``resolve_domain_label``.

The domain registry's cardinality cap is enforced by ``resolve_domain_label``,
not by the metric definitions: a writer that reaches a metric definition
directly and passes a raw string both exempts that family from the cap and puts
one logical domain on two label values (canonical everywhere else, raw here).

This is an AST rule rather than a textual sweep on purpose. Inserting the
resolution call pushes several of the guarded call sites past the line limit,
so the formatter wraps ``.labels(`` and ``domain=`` onto separate lines — a
regex over source text would go false-green on exactly the sites the rule
exists to guard.

Accepted forms for the ``domain=`` keyword value:

- a direct ``resolve_domain_label(...)`` call;
- a name bound earlier in the same function by
  ``domain = resolve_domain_label(...)`` (the established idiom in the
  recorder funnel);
- a string literal — a compile-time-fixed label is bounded by construction and
  has no registry admission to fail.

Owner exemption: ``metrics/recorders/**`` IS the funnel's implementation layer;
its callers resolve before calling in. Channels that cannot be routed yet are
carried in ``baseline.yaml`` with their tracking reference, not exempted here.

Rule registry:
``ARCHITECTURE.md#g79-domain-label-resolution-funnel``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import PROJECT_ROOT, oss_src_root, repo_relative
from tests.architecture.conftest import (
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "domain_label_resolution_funnel"
_RULE_ANCHOR = "#g79-domain-label-resolution-funnel"

_RESOLVER_NAME = "resolve_domain_label"
_OWNER_PREFIX = "src/baldur/metrics/recorders/"


def _src_roots() -> tuple[Path, ...]:
    """OSS (wherever it is installed from) plus the private PRO tier."""
    return (oss_src_root(), PROJECT_ROOT / "src" / "baldur_pro")


def _is_resolver_call(node: ast.AST) -> bool:
    """True for ``resolve_domain_label(...)`` — bare or dotted."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == _RESOLVER_NAME
    if isinstance(func, ast.Attribute):
        return func.attr == _RESOLVER_NAME
    return False


def _resolved_names(scope: ast.AST) -> set[str]:
    """Names bound to a resolver call anywhere inside ``scope``.

    Flow-insensitive by design: the guarded idiom rebinds the parameter
    (``domain = resolve_domain_label(domain)``) at the top of the function and
    every write below reads it, so binding-existence is the property worth
    checking. A name assigned from the resolver and later overwritten with a
    raw value is not a shape this codebase uses.
    """
    bound: set[str] = set()
    for node in ast.walk(scope):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        value = node.value
        if not _is_resolver_call(value):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bound.add(target.id)
    return bound


def _labels_domain_keyword(node: ast.Call) -> ast.keyword | None:
    """Return the ``domain=`` keyword of a ``….labels(...)`` call, if any."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "labels":
        return None
    for keyword in node.keywords:
        if keyword.arg == "domain":
            return keyword
    return None


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []

    # Per-scope resolver bindings: a function that rebinds ``domain`` from the
    # resolver satisfies the rule for every write inside it.
    scope_bindings: dict[int, set[str]] = {}
    for scope in ast.walk(tree):
        if isinstance(
            scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module | ast.ClassDef
        ):
            scope_bindings[id(scope)] = _resolved_names(scope)
    module_bindings = scope_bindings.get(id(tree), set())

    violations: list[tuple[Path, int, str, str]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        allowed = scope_bindings.get(id(scope), set()) | module_bindings
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            keyword = _labels_domain_keyword(node)
            if keyword is None:
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                continue
            if _is_resolver_call(value):
                continue
            if isinstance(value, ast.Name) and value.id in allowed:
                continue
            violations.append(
                (
                    path,
                    node.lineno,
                    symbol_of(tree, node),
                    f"labels(domain=…) not routed through {_RESOLVER_NAME}()",
                )
            )
    return violations


def _is_owner(path: Path) -> bool:
    return repo_relative(path).startswith(_OWNER_PREFIX)


class TestDomainLabelResolutionFunnelArchitecture:
    """Domain-label cardinality invariant — the resolution funnel is the only
    way a ``domain`` label value reaches a metric."""

    def test_no_unresolved_domain_label_write(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(_src_roots()):
            if _is_owner(path):
                continue
            for offender_path, line, symbol, extra in _scan(path):
                # Normalize to the repo-relative ``src/<pkg>/...`` form BEFORE
                # baselining: OSS is an installed sibling here, so its absolute
                # path lies outside PROJECT_ROOT and would key the baseline (and
                # the reported location) on this machine's checkout layout.
                raw.append(
                    (PROJECT_ROOT / repo_relative(offender_path), line, symbol, extra)
                )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"Domain-label funnel breach ({len(violations)}). "
            f"Route the value through `{_RESOLVER_NAME}()` from "
            "`baldur.metrics.registry`, or add a baseline entry under "
            f"`{_RULE_KEY}:` with reason+ticket.\n" + "\n".join(violations)
        )

    def test_guarded_sites_are_actually_scanned(self):
        """Belt-and-suspenders: the two families this rule was written for must
        still be reachable by the scan, so a rename cannot make it vacuous."""
        guarded = (
            oss_src_root() / "services" / "backoff_calculator" / "calculator.py",
            oss_src_root() / "services" / "dlq_outbox" / "outbox.py",
        )
        for path in guarded:
            assert path.is_file(), f"guarded site moved or renamed: {path}"
            tree = parse_ast(path)
            assert tree is not None
            found = any(
                _labels_domain_keyword(node) is not None
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            )
            assert found, (
                f"{repo_relative(path)} no longer writes a `domain=` label. "
                "If the write moved, update this fitness function's anchor."
            )
