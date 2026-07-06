"""G48 — shipped Prometheus alert rules MUST name live registry metrics.

The sample alert file ``examples/monitoring/prometheus-alerts.yml`` ships with
the ``baldur-framework[prometheus]`` extra alongside the two Grafana dashboards. Every
alert ``expr`` references framework metric series by name. Like the dashboard
panels G43 guards, those names were once authored against a guessed metric set
and silently rotted — rules dividing by the never-existent ``baldur_requests_total``
or the typo'd ``baldur_retry_total`` can never fire, with nothing in CI to catch
it. G43 only scans the two dashboard JSONs, so the alert surface was unguarded.

This guard closes that hole on the alert surface. It parses every ``expr`` in
the YAML, extracts the metric-name tokens, **filters to ``baldur_``-prefixed
tokens** (the alert file legitimately mixes namespaces — ``up`` from the
scrape target and OTel-native ``http_server_*`` series from DjangoInstrumentor
are framework-foreign and not ours to validate), and asserts each token is a
registered series name. A typo or a renamed metric turns the build red.

Scope boundary — name existence, NOT population (the same boundary G43
documents). A registered-but-never-populated name passes; population is a
runtime property a static content gate cannot assert without driving traffic.
The manual-render / live-scrape success criteria cover population.

Snapshot construction — union of TWO registration sources (NOT ``get_metrics()``,
which under an ``otel_collector`` observability profile returns the OTel backend
exposing only a ~7-recorder subset via ``PrometheusMetricReader``, yielding false
"unregistered" verdicts for prometheus-only series):

* importing ``baldur.services.metrics.definitions`` + ``...recorders`` registers
  the **module-level** series at import (e.g. ``baldur_canary_governance_bypass_total``,
  which is NOT in any per-domain recorder — dropping this import false-positives
  the untouched ``canary_alerts`` rule); and
* instantiating ``BaldurMetrics()`` directly registers the **per-domain recorder**
  series (``baldur_circuit_breaker_state``, ``baldur_retry_outcomes_total``,
  ``baldur_http_requests_total``, ``baldur_dlq_pending_count``, ...).

Both imports are load-bearing. The only difference from G43's snapshot is the
final ``get_metrics()`` -> ``BaldurMetrics()`` substitution, which makes the
snapshot independent of the ambient observability profile.

The PromQL metric-name tokenizer is shared with G43 via ``_promql.py``.

Rule registry:
``ARCHITECTURE.md#g48-prometheus-alert-metric-drift``
"""

from __future__ import annotations

import pytest

from tests.architecture._promql import metric_tokens, unregistered_tokens
from tests.architecture.conftest import PROJECT_ROOT

# prometheus_client backs the registry snapshot; PyYAML parses the alert file.
# Skip cleanly on a stripped checkout rather than erroring at import.
pytest.importorskip("prometheus_client")
yaml = pytest.importorskip("yaml")

_ALERTS_YAML = PROJECT_ROOT / "examples" / "monitoring" / "prometheus-alerts.yml"

# Only ``baldur_``-prefixed tokens are framework-owned. Foreign series the alert
# file legitimately references — ``up`` (scrape liveness) and the OTel-native
# ``http_server_request_duration_seconds_*`` histogram from DjangoInstrumentor —
# are not registered in BaldurMetrics and are out of this guard's scope.
_FRAMEWORK_PREFIX = "baldur_"


def _baldur_tokens(expr: str) -> set[str]:
    """Metric tokens in ``expr`` restricted to the ``baldur_`` namespace."""
    return {tok for tok in metric_tokens(expr) if tok.startswith(_FRAMEWORK_PREFIX)}


def _iter_alert_exprs():
    """Yield ``(group_name, alert_name, expr)`` for every rule with an expr."""
    data = yaml.safe_load(_ALERTS_YAML.read_text(encoding="utf-8"))
    for group in data.get("groups", []):
        group_name = group.get("name", "<unnamed-group>")
        for rule in group.get("rules", []):
            expr = rule.get("expr")
            alert_name = rule.get("alert", rule.get("record", "<unnamed-rule>"))
            if expr:
                yield group_name, alert_name, expr


def _all_alert_cases() -> list[tuple[str, str, str]]:
    """Collect ``(group_name, alert_name, expr)`` across the alert file."""
    return list(_iter_alert_exprs())


_ALERT_CASES = _all_alert_cases()


@pytest.fixture(scope="module")
def registered_metric_names() -> frozenset[str]:
    """Snapshot the live Prometheus registry's exposed series names.

    Union of two registration sources (see module docstring): import
    ``definitions`` + ``recorders`` for the module-level series, and instantiate
    ``BaldurMetrics()`` directly for the per-domain recorder series. The direct
    instantiation (vs G43's ``get_metrics()``) makes the snapshot independent of
    the ambient observability profile. ``_names_to_collectors`` already carries
    the synthetic per-series suffixes (``_total`` / ``_bucket`` / ``_count`` /
    ``_sum``), so alert tokens match by direct membership with no suffix logic.
    """
    from prometheus_client import REGISTRY

    import baldur.services.metrics.definitions  # noqa: F401
    import baldur.services.metrics.recorders  # noqa: F401
    from baldur.metrics.prometheus import BaldurMetrics

    BaldurMetrics()
    return frozenset(REGISTRY._names_to_collectors.keys())


def test_alert_file_exists() -> None:
    """The shipped alert sample is present (guards against a stale rename)."""
    assert _ALERTS_YAML.is_file(), f"missing alert sample: {_ALERTS_YAML}"


def test_alert_cases_collected() -> None:
    """The collector found alert exprs — an empty list would vacuously pass."""
    assert _ALERT_CASES, "no alert exprs collected from prometheus-alerts.yml"


@pytest.mark.parametrize(
    ("group_name", "alert_name", "expr"),
    _ALERT_CASES,
    ids=[f"{g}::{a}" for g, a, _ in _ALERT_CASES],
)
def test_alert_query_metrics_are_registered(
    group_name: str,
    alert_name: str,
    expr: str,
    registered_metric_names: frozenset[str],
) -> None:
    """Every ``baldur_`` metric an alert queries resolves in the registry (G48)."""
    missing = _baldur_tokens(expr) - registered_metric_names
    assert not missing, (
        f"alert {alert_name!r} (group {group_name!r}) queries unregistered "
        f"baldur_ metric(s) {sorted(missing)}; expr={expr!r}. Re-point the rule "
        f"at a live series in metrics/recorders/* or services/metrics/*."
    )


def test_guard_rejects_a_bogus_metric_name(
    registered_metric_names: frozenset[str],
) -> None:
    """Guard-of-the-guard: an unregistered ``baldur_`` name turns the checker red.

    Injects a token known to be absent into a real expr and asserts the checker
    flags exactly it — proving the positive test above is not vacuously green.
    """
    bogus = "baldur_nonexistent_alert_total"
    assert bogus not in registered_metric_names
    poisoned = f"increase({bogus}[15m]) > 3"
    assert _baldur_tokens(poisoned) - registered_metric_names == {bogus}


def test_foreign_namespace_tokens_are_ignored(
    registered_metric_names: frozenset[str],
) -> None:
    """Non-``baldur_`` series (``up``, OTel-native) are out of the guard's scope.

    The latency rules query ``http_server_request_duration_seconds_bucket`` and
    the system rule queries ``up`` — both framework-foreign and unregistered in
    BaldurMetrics. The prefix filter must drop them so the guard never demands
    they resolve. Sanity-check that the raw (unfiltered) token set does contain a
    foreign token the filter then removes — otherwise the filter is vacuous.
    """
    foreign_expr = "histogram_quantile(0.95, sum(rate(http_server_request_duration_seconds_bucket[5m])) by (le)) * 1000 > 500"
    raw = metric_tokens(foreign_expr)
    assert "http_server_request_duration_seconds_bucket" in raw
    assert _baldur_tokens(foreign_expr) == set()
    # And `unregistered_tokens` (the shared helper) would otherwise flag it.
    assert "http_server_request_duration_seconds_bucket" in unregistered_tokens(
        foreign_expr, registered_metric_names
    )
