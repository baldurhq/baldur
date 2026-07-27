"""Architectural fitness function — a Prometheus collector must never be
patched with ``side_effect`` (G76).

A test that writes::

    with patch("...definitions.some_metric_total", side_effect=RuntimeError):
        do_the_thing()          # "should not raise"

installs no fault at all. Production never *calls* a collector — it calls
``.labels(...)`` on it — so the mock's ``side_effect`` never fires, the
``except`` arm the test exists for never runs, and the test passes for the same
reason an empty test passes. The shape is invisible to review (it reads exactly
like a correct fail-open test) and invisible to the §9 test-value auditor, whose
§9.3 carve-out exempts assertion-free fail-open tests from removal by design.

Three of these shipped undetected, one of them from the initial commit, each
leaving a fail-open arm with zero real coverage. The correct shape raises from
the attribute the SUT actually reaches::

    class _RaisingCollector:
        def __init__(self): self.touched = False
        def labels(self, **kw): self.touched = True; raise RuntimeError(...)

and then asserts the fault happened — ``assert collector.touched``, or the
WARNING the fail-open arm logs. That turns "does not raise" from an implicit
claim into a checkable one.

Precision: a collector is a *value*, never a callable in production use, so
``side_effect`` on one is inert unconditionally. There is no legitimate
instance to exempt, which is why this is enforced-empty rather than baselined.
Patching a metrics *function* (``record_http_request``, ``get_metrics``) with
``side_effect`` is correct and stays unflagged — those are called.

Scope: the published OSS test tree (``OSS_TESTS_ROOT`` — ``tests/oss`` in the
private repo, ``tests`` in the public one). ``tests/pro`` / ``tests/dormant``
are outside the two-copy gate, matching the placement precedent for
published-surface rules; the sweep that produced this rule found no instance
there.

Rule registry:
``ARCHITECTURE.md#g76-metric-collector-patch-inertness``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import DEFAULT_SRC_ROOTS, OSS_TESTS_ROOT
from tests.architecture.conftest import (
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "metric_collector_patch_inertness"
_RULE_ANCHOR = "#g76-metric-collector-patch-inertness"

# The factories and constructors whose return value is a Prometheus collector.
_COLLECTOR_FACTORIES = frozenset(
    {
        "get_or_create_counter",
        "get_or_create_gauge",
        "get_or_create_histogram",
        "get_or_create_summary",
        "Counter",
        "Gauge",
        "Histogram",
        "Summary",
    }
)


def _collector_names(roots: tuple[Path, ...] = DEFAULT_SRC_ROOTS) -> frozenset[str]:
    """Every name in the source tree bound to a Prometheus collector.

    Only the bare attribute name is kept, not the defining module: a test may
    legitimately patch a collector at an importing module's namespace rather
    than where it was defined, and metric names are distinctive enough that the
    last dotted segment is an unambiguous key.
    """
    names: set[str] = set()
    for path in walk_src(roots):
        tree = parse_ast(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            func = value.func
            factory = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if factory not in _COLLECTOR_FACTORIES:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
    return frozenset(names)


def _patched_target(node: ast.Call) -> str | None:
    """The string target of a ``patch(...)`` / ``patch.object(...)`` call.

    Returns the last dotted segment, or None when this is not a patch call, the
    target is not a literal string, or no ``side_effect`` is installed.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr == "object":
            base = func.value
            base_name = (
                base.attr
                if isinstance(base, ast.Attribute)
                else getattr(base, "id", "")
            )
            if base_name != "patch":
                return None
            # patch.object(module, "name", ...) — the name is the second arg.
            args = node.args[1:2]
        elif func.attr == "patch":
            args = node.args[:1]
        else:
            return None
    elif getattr(func, "id", "") == "patch":
        args = node.args[:1]
    else:
        return None

    if not any(kw.arg == "side_effect" for kw in node.keywords):
        return None
    if not args or not isinstance(args[0], ast.Constant):
        return None
    target = args[0].value
    if not isinstance(target, str):
        return None
    return target.rsplit(".", 1)[-1]


def _scan(path: Path, collectors: frozenset[str]) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _patched_target(node)
        if name is None or name not in collectors:
            continue
        violations.append(
            (
                path,
                node.lineno,
                symbol_of(tree, node),
                f"side_effect on the collector `{name}` never fires — "
                "production calls .labels() on it, not the collector itself",
            )
        )
    return violations


class TestMetricCollectorPatchInertness:
    """G76 — a fail-open test whose fault cannot fire proves nothing."""

    def test_no_side_effect_patch_on_a_collector(self):
        collectors = _collector_names()
        assert collectors, (
            "no Prometheus collectors found in the source tree — the scan below "
            "would pass vacuously"
        )

        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in OSS_TESTS_ROOT.rglob("test_*.py"):
            raw.extend(_scan(path, collectors))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"inert fail-open test(s) ({len(violations)}). A Prometheus "
            "collector is never called in production — `.labels(...)` is — so a "
            "`side_effect` on the collector never fires and the except arm under "
            "test never runs. Replace the mock with a double that raises from "
            "`.labels(...)` and assert the fault actually happened (the double's "
            "own touch flag, or the WARNING the fail-open arm logs).\n"
            + "\n".join(violations)
        )

    def test_matcher_detects_the_known_inert_shape(self):
        """Guard-of-the-guard: the canonical bad shape must still be flagged, so
        the enforced-empty pass above can never go vacuous."""
        bad = ast.parse(
            'patch("baldur.services.metrics.definitions.rate_limit_429_total",'
            ' side_effect=RuntimeError("down"))'
        )
        calls = [n for n in ast.walk(bad) if isinstance(n, ast.Call)]
        assert any(_patched_target(c) == "rate_limit_429_total" for c in calls), (
            "matcher no longer detects the canonical inert-patch shape — the "
            "enforced-empty assertion would pass vacuously"
        )

    def test_matcher_exempts_a_collector_patch_without_side_effect(self):
        """Patching a collector to *observe* it is the normal, correct idiom —
        only the inert fault install is banned."""
        good = ast.parse(
            'patch("baldur.services.metrics.definitions.rate_limit_429_total",'
            " autospec=True)"
        )
        calls = [n for n in ast.walk(good) if isinstance(n, ast.Call)]
        assert all(_patched_target(c) is None for c in calls), (
            "matcher false-positives on an observation-only collector patch"
        )

    def test_collector_index_excludes_metrics_functions(self):
        """A `side_effect` on a metrics *function* is correct — it is called."""
        collectors = _collector_names()
        for callable_name in ("record_http_request", "get_metrics", "record_retry"):
            assert callable_name not in collectors, (
                f"`{callable_name}` is a function, not a collector — indexing it "
                "would flag correct fail-open tests"
            )
