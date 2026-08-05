"""``GET /healing/summary`` — the console's healing counters and latency tokens.

The console's healing-ledger footer is "the strip whose whole purpose is trust",
so this handler's contract is about what it *refuses* to say as much as what it
reports:

- A field whose source is unavailable is **omitted**, never reported as ``0``.
  Absent and zero mean different things to the renderer: absent is "no source",
  zero is "a live source that has nothing to report".
- ``humans_paged`` exists only where an escalation writer exists. The OSS tree
  has none, so an OSS-visible count could only ever be a permanently-zero claim
  about a capability the tier does not have.
- ``humans_paged`` counts ``result="sent"`` alone. Every other result value of
  that family — ``logged`` (the logging fallback or dry-run accepted it),
  ``fallback``, ``suppressed`` — means nobody was reached.
- A histogram with no observations yields no latency key at all, rather than a
  fabricated ``p95 0``.

The payload also self-describes its window: ``since`` is the counter epoch, and
it is present on **every** branch including total metric-backend absence — a
caption stating "this process, since <clock>" is what lets the console tell a
live-but-quiet source from no source at all.

Collaborators resolve through singleton getters, so the tests monkeypatch those
seams and hand the handler controlled family samples. The end-to-end claim (a
real replay moving the real family, read back through this handler) is the
integration case in ``tests/integration/dlq/``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client.samples import Sample

from baldur.adapters.prometheus_adapter import PrometheusAdapter
from baldur.api.admin.registry import AdminRegistry
from baldur.api.admin.routes.healing import _register_healing_routes
from baldur.api.handlers.healing import healing_summary
from baldur.interfaces.web_framework import (
    HttpMethod,
    PermissionLevel,
    RequestContext,
)

# ``healing_summary`` uses the metrics backend as a presence signal only: it
# calls ``get_metrics()`` so the recorders' families exist, then reads the
# registry through the adapter. A sentinel is a faithful stand-in for that,
# and a stricter one than a mock — nothing can be called on it by accident.
_METRICS_BACKEND_PRESENT = object()

_REPLAY_OUTCOMES = "baldur_replay_outcomes_total"
_WATCHDOG_ESCALATION = "baldur_watchdog_escalation_total"
_HTTP_DURATION = "baldur_http_request_duration_seconds"
_DLQ_REPLAY_DURATION = "baldur_dlq_replay_duration_seconds"
_DLQ_STORE_DURATION = "baldur_dlq_store_duration_seconds"


def _ctx(query: dict[str, str] | None = None) -> RequestContext:
    return RequestContext(
        method=HttpMethod.GET,
        path="/healing/summary",
        query_params=query or {},
        path_params={},
    )


def _outcome_samples(**by_outcome: float) -> list[Sample]:
    """Replay-outcome samples, all non-synthetic, one label set per outcome."""
    return [
        Sample(
            _REPLAY_OUTCOMES,
            {"domain": "payments", "outcome": outcome, "is_synthetic": "false"},
            value,
        )
        for outcome, value in by_outcome.items()
    ]


def _escalation_samples(**by_result: float) -> list[Sample]:
    return [
        Sample(
            _WATCHDOG_ESCALATION,
            {"component": "redis", "result": result},
            value,
        )
        for result, value in by_result.items()
    ]


def _bucket_samples(family: str, cumulative: dict[str, float]) -> list[Sample]:
    return [
        Sample(f"{family}_bucket", {"le": le}, count)
        for le, count in cumulative.items()
    ]


@pytest.fixture
def summary():
    """Invoke the handler with a controlled family mapping and tier verdict.

    Patches the three singleton seams the handler resolves at call time:
    ``get_metrics`` (backend presence), ``get_prometheus_adapter`` (the reader)
    and ``is_pro_installed`` (the tier gate).
    """

    def _call(
        families: dict[str, list[Sample]] | None = None,
        *,
        pro: bool = False,
        metrics_backend: bool = True,
        adapter_error: Exception | None = None,
        query: dict[str, str] | None = None,
    ) -> dict:
        adapter = MagicMock(spec=PrometheusAdapter)
        if adapter_error is not None:
            adapter.collect_families.side_effect = adapter_error
        else:
            adapter.collect_families.return_value = dict(families or {})

        with (
            patch(
                "baldur.metrics.prometheus.get_metrics",
                return_value=_METRICS_BACKEND_PRESENT if metrics_backend else None,
            ),
            patch(
                "baldur.adapters.prometheus_adapter.get_prometheus_adapter",
                return_value=adapter,
            ),
            patch("baldur.utils.tier.is_pro_installed", return_value=pro),
        ):
            response = healing_summary(_ctx(query))

        assert response.status_code == 200
        return response.body

    return _call


class TestHealingSummaryBehavior:
    """Payload tri-state per field, and the tier gate on ``humans_paged``."""

    # -- since ---------------------------------------------------------------

    def test_since_is_the_module_counter_epoch_on_a_populated_payload(self, summary):
        from baldur.api.handlers.healing import _COUNTER_EPOCH

        body = summary({_REPLAY_OUTCOMES: _outcome_samples(success=3.0)})

        assert body["since"] == _COUNTER_EPOCH.isoformat()
        # Parseable by the console's Date(): the caption renders it as a clock.
        assert datetime.fromisoformat(body["since"]) == _COUNTER_EPOCH

    def test_since_is_present_even_when_no_metrics_backend_exists(self, summary):
        # "No caption" must mean "no source", so the caption's own datum has to
        # survive the branch where every counter is dropped.
        body = summary(metrics_backend=False)

        assert "since" in body
        assert "counters" not in body
        assert "latency" not in body

    # -- replayed ------------------------------------------------------------

    def test_replayed_counts_successful_non_synthetic_outcomes(self, summary):
        body = summary({_REPLAY_OUTCOMES: _outcome_samples(success=7.0, failure=4.0)})

        assert body["counters"]["replayed"] == 7

    def test_replayed_excludes_synthetic_traffic(self, summary):
        # Synthetic replays entered through the test-mode context are another
        # surface's number, not healing.
        samples = [
            *_outcome_samples(success=7.0),
            Sample(
                _REPLAY_OUTCOMES,
                {"domain": "payments", "outcome": "success", "is_synthetic": "true"},
                99.0,
            ),
        ]

        body = summary({_REPLAY_OUTCOMES: samples})

        assert body["counters"]["replayed"] == 7

    def test_replayed_excludes_blocked_and_batch_outcome_values(self, summary):
        # The same family also carries governance blocks and batch summaries.
        body = summary(
            {
                _REPLAY_OUTCOMES: _outcome_samples(
                    success=2.0, blocked=5.0, batch_completed=3.0
                )
            }
        )

        assert body["counters"]["replayed"] == 2

    def test_replayed_sums_across_domains(self, summary):
        samples = [
            Sample(
                _REPLAY_OUTCOMES,
                {"domain": domain, "outcome": "success", "is_synthetic": "false"},
                value,
            )
            for domain, value in (("payments", 4.0), ("orders", 3.0))
        ]

        body = summary({_REPLAY_OUTCOMES: samples})

        assert body["counters"]["replayed"] == 7

    def test_replayed_is_zero_when_the_family_is_registered_but_unwritten(
        self, summary
    ):
        # Present-with-no-samples is the steady state of a fresh process: the
        # recorders construct their families eagerly. The payload reports that
        # honestly as 0 — refusing to RENDER a process-local zero is the
        # console's job, not this handler's.
        body = summary({_REPLAY_OUTCOMES: []})

        assert body["counters"]["replayed"] == 0

    def test_replayed_is_absent_when_the_family_is_not_registered(self, summary):
        # Absent from the collected mapping ⇒ absent from the payload. The
        # console can then tell "no source" from "source, nothing to report".
        body = summary({})

        assert "counters" not in body

    # -- humans_paged (tier) -------------------------------------------------

    def test_humans_paged_is_absent_without_the_pro_distribution(self, summary):
        # Even with the family populated: the OSS tree has no escalation
        # writer, so an OSS-visible count is a claim about a capability the
        # tier does not have.
        body = summary(
            {
                _REPLAY_OUTCOMES: _outcome_samples(success=1.0),
                _WATCHDOG_ESCALATION: _escalation_samples(sent=3.0),
            },
            pro=False,
        )

        assert "humans_paged" not in body["counters"]
        assert body["counters"] == {"replayed": 1}

    def test_humans_paged_counts_delivered_pages_when_pro_is_installed(self, summary):
        body = summary({_WATCHDOG_ESCALATION: _escalation_samples(sent=3.0)}, pro=True)

        assert body["counters"]["humans_paged"] == 3

    def test_humans_paged_excludes_every_result_value_but_sent(self, summary):
        # `logged` is the log-only or dry-run delivery, `fallback` a genuine
        # channel failure, `suppressed` a policy rejection. None reached a
        # person, so none may back a counter labelled "humans paged".
        body = summary(
            {
                _WATCHDOG_ESCALATION: _escalation_samples(
                    sent=2.0, logged=11.0, fallback=5.0, suppressed=8.0
                )
            },
            pro=True,
        )

        assert body["counters"]["humans_paged"] == 2

    def test_humans_paged_is_zero_on_a_pro_install_that_never_paged(self, summary):
        # PRO installed but inactive (expired licence, daemon off) — a true
        # statement the console then declines to render.
        body = summary({_WATCHDOG_ESCALATION: []}, pro=True)

        assert body["counters"]["humans_paged"] == 0

    def test_humans_paged_is_absent_when_pro_is_installed_but_the_family_is_not(
        self, summary
    ):
        body = summary({_REPLAY_OUTCOMES: _outcome_samples(success=1.0)}, pro=True)

        assert "humans_paged" not in body["counters"]

    # -- latency -------------------------------------------------------------

    @pytest.mark.parametrize(
        ("family", "key"),
        [
            (_HTTP_DURATION, "http_p95_seconds"),
            (_DLQ_REPLAY_DURATION, "dlq_replay_p95_seconds"),
            (_DLQ_STORE_DURATION, "dlq_store_p95_seconds"),
        ],
    )
    def test_each_latency_family_lands_on_its_own_payload_key(
        self, summary, family, key
    ):
        # 10 observations in (1, 2]: rank 9.5 interpolates to 1.95.
        body = summary(
            {family: _bucket_samples(family, {"1.0": 0.0, "2.0": 10.0, "+Inf": 10.0})}
        )

        assert body["latency"] == {key: pytest.approx(1.95)}

    def test_latency_key_is_absent_for_a_histogram_with_no_observations(self, summary):
        # A fabricated `p95 0 ms` on the trust strip is exactly the failure the
        # None return exists to prevent.
        body = summary(
            {_HTTP_DURATION: _bucket_samples(_HTTP_DURATION, {"1.0": 0.0, "+Inf": 0.0})}
        )

        assert "latency" not in body

    def test_latency_reports_only_the_families_that_answered(self, summary):
        body = summary(
            {
                _HTTP_DURATION: _bucket_samples(
                    _HTTP_DURATION, {"0.1": 0.0, "0.2": 10.0, "+Inf": 10.0}
                ),
                _DLQ_STORE_DURATION: _bucket_samples(
                    _DLQ_STORE_DURATION, {"0.005": 10.0, "+Inf": 10.0}
                ),
            }
        )

        # dlq_replay is not registered at all → no key, not a null.
        assert set(body["latency"]) == {"http_p95_seconds", "dlq_store_p95_seconds"}

    # -- fail-open + shape ---------------------------------------------------

    def test_collection_failure_degrades_to_the_epoch_alone(self, summary):
        # Fail-open: a metrics-backend fault must not 500 the console's ledger.
        body = summary(adapter_error=RuntimeError("registry corrupted"))

        assert set(body) == {"since"}

    def test_response_is_json_with_a_two_hundred_status(self):
        # No adapter at all (prometheus_client absent) — still a well-formed
        # 200 carrying the epoch, so the console's chain never rejects.
        with (
            patch(
                "baldur.metrics.prometheus.get_metrics",
                return_value=_METRICS_BACKEND_PRESENT,
            ),
            patch(
                "baldur.adapters.prometheus_adapter.get_prometheus_adapter",
                return_value=None,
            ),
            patch("baldur.utils.tier.is_pro_installed", return_value=False),
        ):
            response = healing_summary(_ctx())

        assert response.status_code == 200
        assert response.content_type == "application/json"
        assert set(response.body) == {"since"}

    def test_query_parameters_do_not_change_the_payload(self, summary):
        # NON-GOAL guard: fixed shape, no query surface. This is the console's
        # own data source, not a metrics-query API.
        families = {_REPLAY_OUTCOMES: _outcome_samples(success=5.0)}

        plain = summary(families)
        with_params = summary(
            families,
            query={"outcome": "failure", "domain": "orders", "window": "7d"},
        )

        assert with_params == plain

    def test_every_needed_family_is_read_in_one_collect_call(self):
        # One registry walk per request, not one per field: the walk costs
        # O(total registered label sets), which grows with label cardinality.
        adapter = MagicMock(spec=PrometheusAdapter)
        adapter.collect_families.return_value = {}

        with (
            patch(
                "baldur.metrics.prometheus.get_metrics",
                return_value=_METRICS_BACKEND_PRESENT,
            ),
            patch(
                "baldur.adapters.prometheus_adapter.get_prometheus_adapter",
                return_value=adapter,
            ),
            patch("baldur.utils.tier.is_pro_installed", return_value=True),
        ):
            healing_summary(_ctx())

        adapter.collect_families.assert_called_once()
        requested = set(adapter.collect_families.call_args.args[0])
        assert requested == {
            _REPLAY_OUTCOMES,
            _WATCHDOG_ESCALATION,
            _HTTP_DURATION,
            _DLQ_REPLAY_DURATION,
            _DLQ_STORE_DURATION,
        }


class TestHealingRouteContract:
    """The route table entry: read-only GET at VIEWER, on its own literal path."""

    def test_registers_one_get_route_at_viewer_permission(self):
        registry = AdminRegistry()

        _register_healing_routes(registry)

        routes = registry.all_routes()
        assert len(routes) == 1
        assert routes[0].method is HttpMethod.GET
        assert routes[0].path == "/healing/summary"
        assert routes[0].permission_level is PermissionLevel.VIEWER

    def test_registers_the_healing_summary_handler(self):
        registry = AdminRegistry()

        _register_healing_routes(registry)

        assert registry.all_routes()[0].handler is healing_summary

    def test_route_resolves_with_no_path_parameters(self):
        # Fixed shape: a literal path, so nothing is captured out of the URL.
        registry = AdminRegistry()
        _register_healing_routes(registry)

        resolved = registry.resolve("GET", "/healing/summary")

        assert resolved is not None
        route, params = resolved
        assert route.handler is healing_summary
        assert params == {}

    @pytest.mark.parametrize(
        "method",
        ["POST", "PUT", "PATCH", "DELETE"],
    )
    def test_route_is_read_only(self, method):
        registry = AdminRegistry()
        _register_healing_routes(registry)

        assert registry.resolve(method, "/healing/summary") is None

    def test_route_does_not_answer_a_neighbouring_path(self):
        registry = AdminRegistry()
        _register_healing_routes(registry)

        assert registry.resolve("GET", "/healing") is None
        assert registry.resolve("GET", "/healing/summary/extra") is None

    def test_resolves_to_the_healing_handler_on_the_full_admin_surface(self):
        # Neither shadowed nor shadowing: `resolve` returns the FIRST match, so
        # this only holds if no earlier-registered route's pattern also covers
        # `/healing/summary`.
        from baldur.api.admin.routes import register_all_routes

        registry = AdminRegistry()
        register_all_routes(registry)

        resolved = registry.resolve("GET", "/healing/summary")

        assert resolved is not None
        assert resolved[0].handler is healing_summary

    def test_the_console_root_route_is_not_shadowed_by_the_healing_route(self):
        # The reverse direction of the same claim.
        from baldur.api.admin.routes import register_all_routes

        registry = AdminRegistry()
        register_all_routes(registry)

        resolved = registry.resolve("GET", "/")

        assert resolved is not None
        assert resolved[0].handler is not healing_summary
