"""The traffic-aware lane picks its domains before it selects any entry.

The gate was per *task argument* and the selection was not: the shipped Beat
entry passes no domain, so ``replay_batch(domain=None)`` drained everything
pending with the circuit check skipped entirely. Gating on a domain nobody
passes gates nothing.

Three pieces make the per-entry-domain check possible, and each has a way to be
silently wrong:

- circuits are keyed by the raw ``protect()`` name while DLQ entries are stored
  under the normalized domain, and that projection is many-to-one — it has no
  inverse — so the map has to be built forward, from circuit name to domain;
- a worker's L1 circuit view holds what it hydrated at boot plus what that
  process itself touched, so a projection read without a whole-store restore
  is missing every circuit the web process created;
- a domain no circuit projects onto is *unknown*, not healthy, and it looks
  exactly like a domain with nothing to drain unless the drop is logged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.services.circuit_breaker import CircuitBreakerService
from baldur.services.replay_service import ReplayService
from baldur.tasks.traffic_aware_replay import (
    DROP_REASON_CIRCUIT_OPEN,
    DROP_REASON_NO_CIRCUIT_PROJECTS,
    DROP_REASON_NO_REPLAY_HANDLER,
    CircuitProjection,
    TrafficAwareReplayTask,
    build_circuit_projection,
)

PAYMENT = "payment_api"


def _cb_service(states, *, repository=None):
    cb = MagicMock(spec=CircuitBreakerService)
    cb.repository = repository if repository is not None else MagicMock(spec=[])
    cb.get_all_states.return_value = states
    return cb


def _facets(*domains):
    service = MagicMock(spec=ReplayService)
    service.repository.get_facet_counts.return_value = {
        "by_domain": dict.fromkeys(domains, 1)
    }
    return service


# =============================================================================
# CircuitProjection / build_circuit_projection
# =============================================================================


class TestCircuitProjectionBehavior:
    """Forward projection, and what "unknown" means."""

    def test_a_reprojected_circuit_name_makes_its_stored_domain_drainable(self):
        """``protect("Payment-API")`` files entries under ``payment_api``; a
        fixture that named circuits in already-normalized form could not see
        this defect at all."""
        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service(
                [{"service_name": "Payment-API", "state": "closed"}]
            ),
        ):
            projection = build_circuit_projection()

        assert projection.drop_reason(PAYMENT) is None

    def test_one_open_peer_under_another_spelling_blocks_the_whole_domain(self):
        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service(
                [
                    {"service_name": "Payment-API", "state": "closed"},
                    {"service_name": "payment_api", "state": "open"},
                ]
            ),
        ):
            projection = build_circuit_projection()

        assert projection.drop_reason(PAYMENT) == DROP_REASON_CIRCUIT_OPEN

    def test_a_half_open_circuit_is_not_closed(self):
        """It is still probing; driving a backlog through it re-trips it."""
        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service([{"service_name": PAYMENT, "state": "half_open"}]),
        ):
            projection = build_circuit_projection()

        assert projection.drop_reason(PAYMENT) == DROP_REASON_CIRCUIT_OPEN

    def test_a_domain_no_circuit_projects_onto_is_unknown_not_healthy(self):
        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service(
                [{"service_name": "point_api", "state": "closed"}]
            ),
        ):
            projection = build_circuit_projection()

        assert projection.drop_reason(PAYMENT) == DROP_REASON_NO_CIRCUIT_PROJECTS

    def test_the_store_is_restored_before_it_is_read(self):
        """``get_all_states()`` on the layered repository returns L1 only, and
        a worker that never served HTTP holds none of the middleware circuits."""
        repository = MagicMock(spec=["force_sync_from_l2", "get_l2_health"])
        cb = _cb_service([], repository=repository)
        cb.get_all_states.return_value = []

        def _sync():
            cb.get_all_states.return_value = [
                {"service_name": PAYMENT, "state": "closed"}
            ]
            return True

        repository.force_sync_from_l2.side_effect = _sync

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ):
            projection = build_circuit_projection()

        assert projection.drop_reason(PAYMENT) is None
        assert projection.store_refreshed is True

    def test_a_configured_l2_that_could_not_be_read_marks_the_snapshot_stale(self):
        repository = MagicMock(spec=["force_sync_from_l2", "get_l2_health"])
        repository.force_sync_from_l2.return_value = False
        repository.get_l2_health.return_value = {"adapter_type": "redis"}

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service([], repository=repository),
        ):
            projection = build_circuit_projection()

        assert projection.store_refreshed is False

    def test_a_single_layer_store_is_not_treated_as_a_failed_restore(self):
        """False from the restore means "no L2 configured" there — L1 IS the
        store — and only the other meaning is a reason to stop."""
        repository = MagicMock(spec=["force_sync_from_l2", "get_l2_health"])
        repository.force_sync_from_l2.return_value = False
        repository.get_l2_health.return_value = {"adapter_type": None}

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service([], repository=repository),
        ):
            projection = build_circuit_projection()

        assert projection.store_refreshed is True

    def test_a_store_with_no_restore_seam_is_read_as_is(self):
        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=_cb_service([{"service_name": PAYMENT, "state": "closed"}]),
        ):
            projection = build_circuit_projection()

        assert projection.store_refreshed is True
        assert projection.drop_reason(PAYMENT) is None

    def test_an_empty_projection_reports_unknown_for_every_domain(self):
        assert (
            CircuitProjection().drop_reason(PAYMENT) == DROP_REASON_NO_CIRCUIT_PROJECTS
        )


# =============================================================================
# _select_drainable_domains
# =============================================================================


class TestDrainableDomainSelectionBehavior:
    """Which pending domains this pass is allowed to touch, and why not."""

    def _projection(self, **states):
        return CircuitProjection(
            by_domain={domain: [(domain, state)] for domain, state in states.items()}
        )

    def test_only_domains_with_closed_circuits_are_selected(self):
        service = _facets(PAYMENT, "point_api")
        projection = self._projection(payment_api="closed", point_api="open")

        with patch(
            "baldur.tasks.traffic_aware_replay.has_replay_handler", return_value=True
        ):
            drainable = TrafficAwareReplayTask._select_drainable_domains(
                service, projection
            )

        assert drainable == [PAYMENT]

    def test_a_domain_with_no_registered_handler_is_dropped(self):
        """An unregistered domain still gets a handler — one whose replay
        always fails — so replaying it would burn a retry per entry every
        minute and walk the domain to requires_review."""
        service = _facets(PAYMENT)
        projection = self._projection(payment_api="closed")

        with patch(
            "baldur.tasks.traffic_aware_replay.has_replay_handler", return_value=False
        ):
            drainable = TrafficAwareReplayTask._select_drainable_domains(
                service, projection
            )

        assert drainable == []

    @pytest.mark.parametrize(
        ("states", "handler", "expected_reason"),
        [
            ({"payment_api": "open"}, True, DROP_REASON_CIRCUIT_OPEN),
            ({"point_api": "closed"}, True, DROP_REASON_NO_CIRCUIT_PROJECTS),
            ({"payment_api": "closed"}, False, DROP_REASON_NO_REPLAY_HANDLER),
        ],
    )
    def test_every_drop_is_logged_with_its_domain_and_its_reason(
        self, states, handler, expected_reason
    ):
        """Without it, the permanent exclusion is indistinguishable from a
        domain with nothing to drain."""
        service = _facets(PAYMENT)
        projection = self._projection(**states)

        with (
            patch(
                "baldur.tasks.traffic_aware_replay.has_replay_handler",
                return_value=handler,
            ),
            capture_logs() as logs,
        ):
            TrafficAwareReplayTask._select_drainable_domains(service, projection)

        skipped = [
            e for e in logs if e["event"] == "traffic_aware_replay.domain_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["healing_domain"] == PAYMENT
        assert skipped[0]["reason"] == expected_reason

    def test_no_snapshot_means_no_domain_is_drainable(self):
        """The check cannot be made, so the pass replays nothing rather than
        draining against state it never read."""
        assert (
            TrafficAwareReplayTask._select_drainable_domains(_facets(PAYMENT), None)
            == []
        )

    def test_an_empty_facet_answer_yields_no_domains(self):
        service = MagicMock(spec=ReplayService)
        service.repository.get_facet_counts.return_value = None
        projection = self._projection(payment_api="closed")

        assert (
            TrafficAwareReplayTask._select_drainable_domains(service, projection) == []
        )

    def test_selection_order_is_stable_across_passes(self):
        """The rotation that follows keys on position, so an unordered
        enumeration would rotate randomly instead of fairly."""
        service = _facets("zeta", "alpha", "mid")
        projection = self._projection(zeta="closed", alpha="closed", mid="closed")

        with patch(
            "baldur.tasks.traffic_aware_replay.has_replay_handler", return_value=True
        ):
            drainable = TrafficAwareReplayTask._select_drainable_domains(
                service, projection
            )

        assert drainable == ["alpha", "mid", "zeta"]


# =============================================================================
# _split_across_domains
# =============================================================================


def _split(domains, max_items, *, minute):
    with patch(
        "baldur.tasks.traffic_aware_replay.time.time", return_value=minute * 60.0
    ):
        return TrafficAwareReplayTask._split_across_domains(domains, max_items)


class TestDomainQuotaSplitBehavior:
    """The same divmod fairness rule the circuit-close sweep uses for lanes."""

    def test_budget_is_split_by_divmod_with_the_remainder_on_the_head(self):
        split = _split(["a", "b", "c"], 10, minute=0)

        assert split == [("a", 4), ("b", 3), ("c", 3)]

    def test_the_whole_budget_is_handed_out(self):
        split = _split(["a", "b", "c", "d"], 37, minute=0)

        assert sum(quota for _, quota in split) == 37

    def test_a_single_domain_takes_the_whole_budget(self):
        assert _split(["a"], 50, minute=0) == [("a", 50)]

    def test_more_domains_than_items_drops_the_tail_for_this_pass_only(self):
        """The base share is 0, so a domain outside the head gets nothing —
        which is why the head has to move."""
        split = _split(["a", "b", "c", "d"], 2, minute=0)

        assert split == [("a", 1), ("b", 1)]

    def test_the_head_rotates_with_the_wall_clock_minute(self):
        """Keyed on the clock rather than a counter because the lane runs on
        whichever worker picks the message up, so process-local state would
        restart per worker and re-create the starvation it removes."""
        heads = [_split(["a", "b", "c"], 3, minute=m)[0][0] for m in range(3)]

        assert heads == ["a", "b", "c"]

    def test_every_domain_leads_within_one_full_rotation(self):
        domains = ["a", "b", "c", "d"]

        leaders = {_split(domains, 2, minute=m)[0][0] for m in range(len(domains))}

        assert leaders == set(domains)

    def test_a_starved_domain_is_served_on_a_later_pass(self):
        """The tail is dropped this pass, not permanently."""
        domains = ["a", "b", "c", "d"]

        served = {
            name
            for m in range(len(domains))
            for name, _ in _split(domains, 2, minute=m)
        }

        assert served == set(domains)
