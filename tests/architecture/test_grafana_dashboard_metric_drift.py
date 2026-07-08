"""G43 — shipped Grafana dashboard queries MUST name live registry metrics.

The two sample dashboards under ``examples/monitoring/`` (the OSS overview and
the PRO operations board) ship with the ``baldur-framework[prometheus]`` extra. Every
Prometheus panel query in them references framework metric series by name. Those
names were once authored by guessing — before the recorder layer existed — and
silently rotted (``baldur_retry_total`` / ``baldur_requests_total`` never
existed), rendering empty panels with nothing in CI to catch it.

This guard closes that hole. It snapshots the LIVE Prometheus registry after
importing the metric modules, extracts the metric-name tokens from every panel
``target.expr`` in both dashboards, and asserts each token is a registered
series name. A typo or a renamed metric turns the build red on the next commit.

Scope boundary — name existence, NOT population. A registered-but-dead name
(e.g. the bare ``circuit_breaker_state`` duplicate) would pass: population is a
runtime property a static content gate cannot assert without driving traffic.
The panels are hand-authored against empirically-verified populated names, and
the manual render success criteria cover population. Registration is
PRO-independent — every recorder registers its ``baldur_``-prefixed series at
construction gated only on ``prometheus_client``, so the PRO operations-board
series (DLQ, throttle, canary, emergency, notification, watchdog, bulkhead) all
resolve on an OSS-only checkout; this guard does NOT skip PRO tokens and needs
no PRO-name allowlist.

The PromQL metric-name tokenizer is shared with G48
(``test_prometheus_alert_metric_drift.py``) via ``_promql.py`` so the two
metric-drift guards cannot drift apart.

Rule registry:
``ARCHITECTURE.md#g43-grafana-dashboard-metric-drift``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.architecture._promql import unregistered_tokens
from tests.architecture.conftest import PROJECT_ROOT

# prometheus_client backs the registry snapshot; the private repo installs it,
# but skip cleanly on a stripped checkout rather than erroring at import.
pytest.importorskip("prometheus_client")

_MONITORING_DIR = PROJECT_ROOT / "examples" / "monitoring"
_OVERVIEW_JSON = _MONITORING_DIR / "baldur-overview.json"
_OPERATIONS_JSON = _MONITORING_DIR / "baldur-operations.json"
_DASHBOARDS = (_OVERVIEW_JSON, _OPERATIONS_JSON)

# PRO-only metric families in the v1.0 launch set. They live only on the
# operations board so an OSS-only stack renders no mystery-empty panels (the
# physical tiering, D3). Every prefix here MUST have a present panel on the
# operations board (asserted by test_operations_holds_pro_series); deferred
# (v1.1) families live in _DEFERRED_PREFIXES instead.
_PRO_PREFIXES = (
    "baldur_dlq_",
    "baldur_throttle_",
    "baldur_canary_",
    "baldur_emergency_mode_",
    "baldur_notification_",
    "baldur_watchdog_",
    "baldur_bulkhead_",
)

# Deferred-tier (v1.1) families: NOT in the v1.0 launch set, so they must appear
# on NO shipped board. The mirror of _PRO_PREFIXES — on graduation (#527) a
# prefix moves from here into _PRO_PREFIXES (one-line swap) and its panel is
# re-added, and the gate catches a half-graduation in either direction.
_DEFERRED_PREFIXES = (
    "baldur_error_budget_",
    "baldur_saga_",
)


def _iter_panel_exprs(dashboard_path: Path):
    """Yield ``(panel_title, expr)`` for every Prometheus target with an expr.

    Tempo search targets (``queryType: search``, no ``expr``) are skipped — they
    have no metric name to validate.
    """
    data = json.loads(dashboard_path.read_text(encoding="utf-8"))
    for panel in data.get("panels", []):
        title = panel.get("title", "<untitled>")
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if expr:
                yield title, expr


def _all_panel_cases() -> list[tuple[str, str, str]]:
    """Collect ``(file_name, panel_title, expr)`` across both dashboards."""
    cases: list[tuple[str, str, str]] = []
    for path in _DASHBOARDS:
        for title, expr in _iter_panel_exprs(path):
            cases.append((path.name, title, expr))
    return cases


_PANEL_CASES = _all_panel_cases()


@pytest.fixture(scope="module")
def registered_metric_names() -> frozenset[str]:
    """Snapshot the live Prometheus registry's exposed series names.

    Ordering matters (the one non-obvious setup dependency): import
    ``definitions`` + ``recorders`` (their module-level series register at
    import) AND call ``get_metrics()`` (the per-domain recorders register their
    ``baldur_``-prefixed series in ``BaldurMetrics.__init__``) BEFORE
    snapshotting — otherwise the prefixed recorder names are absent.
    ``_names_to_collectors`` already contains the synthetic per-series suffixes
    (``_bucket`` / ``_count`` / ``_sum`` for histograms, ``_total`` for
    counters), so panel tokens match by direct membership with no suffix logic.
    """
    from prometheus_client import REGISTRY

    import baldur.services.metrics.definitions  # noqa: F401
    import baldur.services.metrics.recorders  # noqa: F401
    from baldur.metrics.prometheus import get_metrics

    get_metrics()
    return frozenset(REGISTRY._names_to_collectors.keys())


def test_dashboard_files_exist() -> None:
    """Both shipped dashboards are present (guards against a stale rename)."""
    for path in _DASHBOARDS:
        assert path.is_file(), f"missing dashboard sample: {path}"


def test_panel_cases_collected() -> None:
    """The collector found Prometheus panels — an empty list would vacuously pass."""
    assert _PANEL_CASES, "no Prometheus panel exprs collected from the dashboards"


@pytest.mark.parametrize(
    ("file_name", "panel_title", "expr"),
    _PANEL_CASES,
    ids=[f"{f}::{t}" for f, t, _ in _PANEL_CASES],
)
def test_panel_query_metrics_are_registered(
    file_name: str,
    panel_title: str,
    expr: str,
    registered_metric_names: frozenset[str],
) -> None:
    """Every metric name a panel queries resolves in the live registry (G43)."""
    missing = unregistered_tokens(expr, registered_metric_names)
    assert not missing, (
        f"{file_name} panel {panel_title!r} queries unregistered metric(s) "
        f"{sorted(missing)}; expr={expr!r}. Re-point the panel at a live "
        f"series in metrics/recorders/* or services/metrics/*."
    )


def test_guard_rejects_a_bogus_metric_name(
    registered_metric_names: frozenset[str],
) -> None:
    """Guard-of-the-guard (SC2): an unregistered name turns the checker red.

    Injects a token known to be absent into a real expr and asserts the checker
    flags exactly it — proving the positive test above is not vacuously green.
    """
    bogus = "baldur_nonexistent_total"
    assert bogus not in registered_metric_names
    poisoned = f"sum(rate({bogus}[5m])) by (domain)"
    assert unregistered_tokens(poisoned, registered_metric_names) == {bogus}


@pytest.mark.parametrize(
    "pro_prefix",
    _PRO_PREFIXES,
    ids=list(_PRO_PREFIXES),
)
def test_overview_excludes_pro_series(pro_prefix: str) -> None:
    """OSS overview holds no PRO-only series — physical tiering (SC3)."""
    overview_text = _OVERVIEW_JSON.read_text(encoding="utf-8")
    assert pro_prefix not in overview_text, (
        f"PRO series prefix {pro_prefix!r} leaked into the OSS overview "
        f"dashboard; it belongs only in baldur-operations.json."
    )


@pytest.mark.parametrize(
    "pro_prefix",
    _PRO_PREFIXES,
    ids=list(_PRO_PREFIXES),
)
def test_operations_holds_pro_series(pro_prefix: str) -> None:
    """Each PRO-only family appears on the PRO operations dashboard (SC3)."""
    operations_text = _OPERATIONS_JSON.read_text(encoding="utf-8")
    assert pro_prefix in operations_text, (
        f"PRO series prefix {pro_prefix!r} missing from the PRO operations dashboard."
    )


@pytest.mark.parametrize(
    "deferred_prefix",
    _DEFERRED_PREFIXES,
    ids=list(_DEFERRED_PREFIXES),
)
def test_deferred_series_on_no_board(deferred_prefix: str) -> None:
    """Deferred-tier families ship on no dashboard — tier-honesty invariant."""
    for path in _DASHBOARDS:
        assert deferred_prefix not in path.read_text(encoding="utf-8"), (
            f"deferred-tier prefix {deferred_prefix!r} leaked onto {path.name}; "
            f"deferred (v1.1) features must not advertise panels on a v1.0 board."
        )
