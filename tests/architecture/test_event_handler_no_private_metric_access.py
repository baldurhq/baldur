"""Architectural fitness function — no consumer may reach into a backend-private
metric recorder attribute (645 D6 / G45).

``metrics/event_handlers.py`` historically recorded domain metrics by reaching
into prometheus_client-private recorder internals — the
``metrics.<recorder>._<private>.labels(...).set()/.inc()/.observe()`` shape. That
shape assumes the Prometheus backend's recorder layout; under the OTel metrics
backend the per-domain recorders expose a different internal shape, so every such
access raised ``AttributeError`` (swallowed by the handler's fail-open envelope)
and the metric was silently dropped. The defect was behaviorally invisible — a
dead recording path changes no test output — so only a live smoke caught it.

This rule locks the invariant: outside the metric OWNERS (the recorder classes
and the two backend facades, which legitimately touch their own ``self._x``
instruments), no module may chain ``*.{dlq|retry|circuit_breaker|replay|infra|
throttle}._<private>.(labels|set|inc|observe|add)``. Consumers must route through
the recorder public methods (``record_state_change`` / ``record_trip`` / ...),
which both backends implement.

The trailing recording-call anchor (``.labels|set|inc|observe|add``) is
load-bearing: a bare private-attribute reference with no recording call — e.g.
``SafeGauge(metrics.dlq._pending_gauge)`` in ``event_handlers.py`` — is an
intentional fast-path wrapper and stays exempt. Do NOT tighten this rule to flag
bare ``_<private>`` references.

Rule registry:
``ARCHITECTURE.md#g45-event-handler-no-private-metric-access``
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import DEFAULT_SRC_ROOTS, PROJECT_ROOT
from tests.architecture.conftest import (
    collect_violations,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "event_handler_no_private_metric_access"
_RULE_ANCHOR = "#g45-event-handler-no-private-metric-access"

# The recorder attribute names whose private internals are backend-coupled.
_RECORDER_NAMES = frozenset(
    {"dlq", "retry", "circuit_breaker", "replay", "infra", "throttle"}
)
# The recording-call methods that signal "this is a metric write, not a bare ref".
_RECORDING_CALLS = frozenset({"labels", "set", "inc", "observe", "add"})

# Metric OWNERS — the only modules allowed to touch recorder private internals.
# The recorder classes reach their OWN instruments via ``self._x`` (which does
# NOT match this rule's ``<recorder>._x`` chain anyway), and the two backend
# facades construct/delegate to them.
_OWNER_FILES = frozenset(
    {
        "src/baldur/metrics/prometheus.py",
        "src/baldur/metrics/otel_backend.py",
    }
)
_OWNER_DIR_PREFIX = "src/baldur/metrics/recorders/"


def _rel_posix(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_owner(path: Path) -> bool:
    rel = _rel_posix(path)
    return rel in _OWNER_FILES or rel.startswith(_OWNER_DIR_PREFIX)


def _is_private_recorder_access(node: ast.Call) -> bool:
    """Match ``*.{recorder}._<private>.(labels|set|inc|observe|add)(...)``.

    AST shape for ``metrics.circuit_breaker._state.labels(...)``::

        Call(func=Attribute(attr="labels",
                             value=Attribute(attr="_state",
                                             value=Attribute(attr="circuit_breaker"))))

    A recorder's own ``self._state.labels(...)`` does NOT match — its innermost
    value is a ``Name`` (``self``), not a recorder ``Attribute``.
    """
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _RECORDING_CALLS:
        return False
    private = func.value
    if not isinstance(private, ast.Attribute) or not private.attr.startswith("_"):
        return False
    recorder = private.value
    if not isinstance(recorder, ast.Attribute):
        return False
    return recorder.attr in _RECORDER_NAMES


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_private_recorder_access(node):
            continue
        recorder_attr = node.func.value.value.attr  # type: ignore[attr-defined]
        private_attr = node.func.value.attr  # type: ignore[attr-defined]
        violations.append(
            (
                path,
                node.lineno,
                symbol_of(tree, node),
                f"{recorder_attr}.{private_attr}.{node.func.attr}(...) "
                "private recorder access",
            )
        )
    return violations


class TestEventHandlerNoPrivateMetricAccess:
    """645 D6 / G45 — recorder private internals are not a cross-backend contract;
    consumers must route through recorder public methods."""

    def test_no_private_recorder_access_outside_owners(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in walk_src(DEFAULT_SRC_ROOTS):
            if _is_owner(path):
                continue
            for offender_path, line, symbol, extra in _scan(path):
                raw.append((offender_path, line, symbol, extra))

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"backend-private metric recorder access ({len(violations)}). "
            "Route through the recorder public method "
            "(record_state_change / record_trip / record_failure / "
            "record_attempt / record_retry / record_recovery_duration / "
            "record_sla_breach / record_started / record_replay / "
            "record_store_duration) — both metrics backends implement them. "
            "A backend-private `<recorder>._x.<call>(...)` access raises "
            "AttributeError under the OTel backend and silently drops the "
            "metric.\n" + "\n".join(violations)
        )

    def test_matcher_detects_known_violation_shape(self):
        """Guard-of-the-guard: the matcher must flag the canonical bad shape, so
        the enforced-empty pass above can never go vacuous."""
        bad = ast.parse(
            "metrics.circuit_breaker._state.labels(service='s', cell_id='').set(1)"
        )
        calls = [n for n in ast.walk(bad) if isinstance(n, ast.Call)]
        assert any(_is_private_recorder_access(c) for c in calls), (
            "matcher no longer detects the canonical private-recorder access "
            "shape — the enforced-empty assertion would pass vacuously"
        )

    def test_matcher_exempts_bare_reference_and_self_access(self):
        """The trailing-call anchor must keep a bare ``_x`` reference and a
        recorder's own ``self._x.<call>`` exempt."""
        bare = ast.parse("wrap(metrics.dlq._pending_gauge)")
        self_access = ast.parse("self._items_total.add(1, {'domain': d})")
        for tree in (bare, self_access):
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
            assert not any(_is_private_recorder_access(c) for c in calls), (
                "matcher false-positives on an exempt access shape "
                "(bare reference or recorder self-access)"
            )
