"""Shared PromQL metric-name tokenizer for the metric-drift fitness functions.

Both G43 (``test_grafana_dashboard_metric_drift.py`` — dashboard panels) and G48
(``test_prometheus_alert_metric_drift.py`` — alert rules) extract the metric-name
tokens from a PromQL expression and assert each resolves in the live registry.
The tokenizer is a regex approximation (strip label clauses / matchers /
durations / strings, then subtract the reserved PromQL function+keyword set),
not a full parser — sufficient for the hand-authored sample exprs the guards
police, and shared here so the two guards cannot drift apart.

This is a non-test helper (no ``test_`` prefix → pytest does not collect it).
"""

from __future__ import annotations

import re

__all__ = [
    "metric_tokens",
    "unregistered_tokens",
    "PROMQL_RESERVED",
]

# PromQL identifier tokens that are NOT metric names — aggregation operators,
# functions, and vector-matching keywords. Any identifier left after stripping
# label clauses / matchers / durations and subtracting this set is treated as a
# metric name and must resolve in the registry.
PROMQL_RESERVED = frozenset(
    {
        # aggregation operators
        "sum",
        "min",
        "max",
        "avg",
        "group",
        "stddev",
        "stdvar",
        "count",
        "count_values",
        "bottomk",
        "topk",
        "quantile",
        "limitk",
        "limit_ratio",
        # aggregation / vector-matching keywords + binary set ops
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "offset",
        "bool",
        "and",
        "or",
        "unless",
        "atan2",
        "start",
        "end",
        "le",
        # counter / range functions
        "rate",
        "irate",
        "increase",
        "resets",
        "changes",
        "delta",
        "idelta",
        "deriv",
        "predict_linear",
        "holt_winters",
        "double_exponential_smoothing",
        # histogram functions
        "histogram_quantile",
        "histogram_count",
        "histogram_sum",
        "histogram_avg",
        "histogram_fraction",
        "histogram_stddev",
        "histogram_stdvar",
        # math / misc functions
        "abs",
        "absent",
        "absent_over_time",
        "ceil",
        "clamp",
        "clamp_max",
        "clamp_min",
        "exp",
        "floor",
        "ln",
        "log2",
        "log10",
        "round",
        "scalar",
        "sgn",
        "sqrt",
        "vector",
        "sort",
        "sort_desc",
        "sort_by_label",
        "sort_by_label_desc",
        # label functions
        "label_join",
        "label_replace",
        # time functions
        "time",
        "timestamp",
        "day_of_month",
        "day_of_week",
        "day_of_year",
        "days_in_month",
        "hour",
        "minute",
        "month",
        "year",
        # *_over_time
        "avg_over_time",
        "min_over_time",
        "max_over_time",
        "sum_over_time",
        "count_over_time",
        "quantile_over_time",
        "stddev_over_time",
        "stdvar_over_time",
        "last_over_time",
        "present_over_time",
        "mad_over_time",
    }
)

# Label-list clauses whose contents are label names, never metric names.
_LABEL_CLAUSE_RE = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)"
)
_LABEL_MATCHER_RE = re.compile(r"\{[^}]*\}")
_DURATION_RE = re.compile(r"\[[^\]]*\]")
_STRING_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
_IDENT_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


def metric_tokens(expr: str) -> set[str]:
    """Extract candidate metric-name tokens from a PromQL expression.

    Strips label-list clauses (``by (...)`` etc.), label matchers (``{...}``),
    range/offset durations (``[5m]``), and string literals, then returns the
    identifier tokens that are not reserved PromQL functions/keywords.
    """
    stripped = _LABEL_CLAUSE_RE.sub(" ", expr)
    stripped = _LABEL_MATCHER_RE.sub(" ", stripped)
    stripped = _DURATION_RE.sub(" ", stripped)
    stripped = _STRING_RE.sub(" ", stripped)
    return {tok for tok in _IDENT_RE.findall(stripped) if tok not in PROMQL_RESERVED}


def unregistered_tokens(expr: str, registered: frozenset[str]) -> set[str]:
    """Return the metric-name tokens in ``expr`` absent from the registry."""
    return metric_tokens(expr) - registered
