"""G78 — bundled alert expressions MUST stay capable of firing.

Two ways an ``expr`` in ``examples/monitoring/prometheus-alerts.yml`` can be
wrong while parsing cleanly, loading into Prometheus without complaint, and
simply never firing:

* **a framework-specific scrape-job selector.** ``up{job="django"} == 0``
  shipped as the only critical scrape-liveness rule and matched an empty vector
  on every Flask / FastAPI / plain-Python deployment — a structural inability to
  fire, not a mis-tuned threshold. Baldur ships no scrape config, so there is no
  job-name convention any rule may assume. All four matcher operators are
  covered against all three PromQL string-literal delimiters (double quote,
  apostrophe, backquoted raw string) — a delimiter the matcher misses is a
  selector that ships.
* **a value read of the ``baldur_up`` liveness marker.** The marker is a
  constant 1 whose *presence* carries the whole signal. An unlabelled
  ``prometheus_client`` Gauge initializes to ``0.0``, so any path that registers
  it but does not set it — a lost ``.set(1)``, or a name collision whose
  swallowed ``.set`` leaves a foreign collector's value — exports
  ``baldur_up 0``. A predicate like ``max_over_time(baldur_up[6h]) == 1`` would
  then silently switch scrape-down detection off, while the absence backstop
  also stayed quiet because the series does exist. Both liveness rules must
  therefore test *existence* (``present_over_time`` / ``absent_over_time``).

Both regressions are behaviorally invisible: nothing in the file, in Prometheus,
or in a metric-name check notices. G48 sits one axis over on this same file — it
asks whether every ``baldur_``-prefixed token *resolves in the registry*, a
name-existence question. This guard asks whether the expression can fire.

Both checks above are negative, and a third way to ship a file that cannot fire
is to carry no liveness rule at all — deleting the pair satisfies every negative
assertion, and no gate over this file asserts a rule exists. So the presence of
each existence predicate is pinned here too.

Comment lines are out of scope by construction: the file is parsed as YAML and
only ``expr`` values are inspected, so the deliberate ``job=`` specialization
example and the prose about the marker's value semantics are never read.

Enforced-empty — no baseline. Neither shape has a legitimate instance: the job
selector was a single grep-verified outlier, and a value read of a constant-1
marker is the failure this rule exists to prevent.

Rule registry:
``ARCHITECTURE.md#g78-prometheus-alert-expression-shape``
"""

from __future__ import annotations

import re

import pytest

from tests.architecture.conftest import PROJECT_ROOT

yaml = pytest.importorskip("yaml")

_ALERTS_YAML = PROJECT_ROOT / "examples" / "monitoring" / "prometheus-alerts.yml"

# The exporter liveness marker. Its value is never the signal — its presence is.
_UP_MARKER = "baldur_up"

# Functions that read a series' *existence* over a window. Only these may take
# the liveness marker as an argument.
_EXISTENCE_FUNCTIONS = ("present_over_time", "absent_over_time")

# A label matcher on `job` inside an expr, in any of PromQL's four matcher
# forms (`=`, `!=`, `=~`, `!~`) against any of its three string-literal
# delimiters (double quote, apostrophe, backquoted raw string). Vector-matching
# clauses that merely *name* the label — `on (job, instance)` — carry no matcher
# operator and are not matched.
_JOB_SELECTOR_RE = re.compile(r"\bjob\s*(?:=~|!~|!=|=)\s*[\"'`]")

# An occurrence of the marker is legitimate only when it opens the argument
# list of an existence function.
_MARKER_OCCURRENCE_RE = re.compile(rf"\b{_UP_MARKER}\b")
_EXISTENCE_CALL_RE = re.compile(
    rf"(?:{'|'.join(_EXISTENCE_FUNCTIONS)})\(\s*{_UP_MARKER}\b"
)


def _iter_alert_exprs() -> list[tuple[str, str, str]]:
    """Collect ``(group_name, alert_name, expr)`` for every rule with an expr."""
    data = yaml.safe_load(_ALERTS_YAML.read_text(encoding="utf-8"))
    cases: list[tuple[str, str, str]] = []
    for group in data.get("groups", []):
        group_name = group.get("name", "<unnamed-group>")
        for rule in group.get("rules", []):
            expr = rule.get("expr")
            alert_name = rule.get("alert", rule.get("record", "<unnamed-rule>"))
            if expr:
                cases.append((group_name, alert_name, expr))
    return cases


_ALERT_CASES = _iter_alert_exprs()


def _job_selectors(expr: str) -> list[str]:
    """Framework-specific scrape-job matchers in ``expr``."""
    return [m.group(0) for m in _JOB_SELECTOR_RE.finditer(expr)]


def _value_reads_of_marker(expr: str) -> int:
    """Occurrences of the liveness marker outside an existence-function call."""
    return len(_MARKER_OCCURRENCE_RE.findall(expr)) - len(
        _EXISTENCE_CALL_RE.findall(expr)
    )


def test_alert_cases_collected() -> None:
    """The collector found alert exprs — an empty list would vacuously pass."""
    assert _ALERT_CASES, "no alert exprs collected from prometheus-alerts.yml"


@pytest.mark.parametrize(
    ("group_name", "alert_name", "expr"),
    _ALERT_CASES,
    ids=[f"{g}::{a}" for g, a, _ in _ALERT_CASES],
)
def test_no_framework_specific_job_selector(
    group_name: str, alert_name: str, expr: str
) -> None:
    """No rule pins itself to one framework's scrape job (G78)."""
    found = _job_selectors(expr)
    assert not found, (
        f"alert {alert_name!r} (group {group_name!r}) selects on a scrape job "
        f"{found}; expr={expr!r}. Baldur ships no scrape config, so a job name "
        f"is a deployment detail: the rule matches an empty vector — and can "
        f"never fire — wherever the operator named the job differently. Join on "
        f"the {_UP_MARKER} marker instead."
    )


@pytest.mark.parametrize(
    ("group_name", "alert_name", "expr"),
    _ALERT_CASES,
    ids=[f"{g}::{a}" for g, a, _ in _ALERT_CASES],
)
def test_liveness_marker_is_read_by_existence_only(
    group_name: str, alert_name: str, expr: str
) -> None:
    """No rule reads the liveness marker's value (G78)."""
    assert _value_reads_of_marker(expr) == 0, (
        f"alert {alert_name!r} (group {group_name!r}) reads {_UP_MARKER} outside "
        f"{list(_EXISTENCE_FUNCTIONS)}; expr={expr!r}. The marker is a constant-1 "
        f"presence signal: an unlabelled Gauge that is registered but never set "
        f"exports 0, so a value test silently disables the rule. Test existence."
    )


def test_both_liveness_rules_ship_and_key_on_the_marker() -> None:
    """The file still ships a scrape-liveness pair keyed on the marker (G78).

    The two checks above are negative: deleting both liveness rules satisfies
    them trivially, and no other gate over this file asserts a rule exists —
    G48 only asks whether the tokens that *are* present resolve. Dropping the
    rules restores the original defect (an operator importing the file gets no
    scrape-liveness alarm at all) with every gate green, so the presence of
    each existence predicate is pinned here rather than left to a landing grep.
    """
    for function in _EXISTENCE_FUNCTIONS:
        call = re.compile(rf"{function}\(\s*{_UP_MARKER}\b")
        matched = [name for _, name, expr in _ALERT_CASES if call.search(expr)]
        assert matched, (
            f"no bundled alert calls {function}({_UP_MARKER}...). The pair "
            f"`present_over_time` (per-target death, joined on the marker) and "
            f"`absent_over_time` (nothing reporting at all) is the whole "
            f"framework-agnostic scrape-liveness signal — without both, a "
            f"deployment that imports these rules has no liveness alarm."
        )


def test_guard_rejects_a_job_selector() -> None:
    """Guard-of-the-guard: the job-selector check must reject the shape it bans.

    Covers all four matcher forms plus the vector-matching clause that names
    ``job`` without selecting on it — the false positive that would make the
    new liveness rule itself unshippable.
    """
    assert _job_selectors('up{job="django"} == 0')
    assert _job_selectors('up{job!="django"} == 0')
    assert _job_selectors('up{job=~"baldur-.*"} == 0')
    assert _job_selectors('up{job !~ "django"} == 0')
    # PromQL accepts three string-literal delimiters, not just the double
    # quote: an apostrophe and a backquoted raw string select identically.
    assert _job_selectors("up{job='django'} == 0")
    assert _job_selectors("up{job=`django`} == 0"), (
        "a backquoted raw string is a PromQL string literal like any other"
    )
    assert not _job_selectors(
        "up == 0 and on (job, instance) present_over_time(baldur_up[6h])"
    )
    assert not _job_selectors("absent_over_time(baldur_up[5m])")


def test_guard_rejects_a_value_read_of_the_marker() -> None:
    """Guard-of-the-guard: the existence-only check must reject a value test."""
    assert _value_reads_of_marker("max_over_time(baldur_up[5m]) == 1")
    assert _value_reads_of_marker("baldur_up == 0")
    assert _value_reads_of_marker("up == 0 and on (job, instance) baldur_up"), (
        "a bare marker reference is a value read"
    )
    assert not _value_reads_of_marker(
        "up == 0 and on (job, instance) present_over_time(baldur_up[6h])"
    )
    assert not _value_reads_of_marker("absent_over_time(baldur_up[5m])")
    # A rule naming a different baldur_ series is untouched by this check.
    assert not _value_reads_of_marker("sum(baldur_dlq_pending_count) > 50")
