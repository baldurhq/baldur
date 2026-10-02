"""What a recovery with no lane leaves behind (809 D1, D2).

A recovery pass for a name with no lane — no map entry and no replay handler of
its own — replays nothing whatever is stored. The only question left is whether
it leaves parked work behind, which the operator must hear about.
``parked_count_for_recovery`` answers that from the shared store, or answers
None when it cannot; every caller reads None as "work may be parked", the loud
direction. ``recovery_is_idle`` is True only for a name with no lane AND a
count of exactly 0 — the one case a pass can end before it starts.

Verification techniques applied:
- Decision table: the five exits of the count (mapped, no domain of its own,
  the read raised, not a count, a count), each pinned by its distinct result
- Boundary: 0 is a count; -1, a bool, a float and a string are not
- Dependency interaction: the strict read is handed the stored domain, and is
  never reached when a cheaper exit has already decided
- Branch-outcome completeness: each lane source alone makes a name non-idle
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from unittest.mock import create_autospec, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.exceptions import DLQError
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationRepository,
)
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.services.replay_service.service import _RecoveryLanes
from baldur.utils.domain_validation import FALLBACK_DOMAIN, resolve_stored_domain

SERVICE = "payment_api"
# A raw name the store files under a different (canonical) spelling.
REPROJECTED = "Payment-API"
# Names with no domain identity of their own: they pool into the fallback bucket.
UNADDRESSABLE = ["3ds-gateway", "a" * 65]


# =============================================================================
# Fixtures / helpers
# =============================================================================


class _Handler(ReplayHandler):
    """A registered replay handler; what it would replay is irrelevant here."""

    def __init__(self, domain: str) -> None:
        self._domain = domain

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "done")


@pytest.fixture
def repository():
    """A repository double carrying the interface's exact method set."""
    return create_autospec(FailedOperationRepository, instance=True)


@pytest.fixture
def service(repository) -> ReplayService:
    return ReplayService(repository=repository)


@pytest.fixture
def register_handler() -> Iterator[Callable[[str], _Handler]]:
    """Register handlers for one test and remove exactly those afterwards."""
    created: list[_Handler] = []

    def _register(domain: str) -> _Handler:
        handler = _Handler(domain)
        register_replay_handler(handler)
        created.append(handler)
        return handler

    yield _register
    for handler in created:
        _replay_handlers.pop(resolve_stored_domain(handler.domain), None)


def _runtime_map(value: dict[str, list[str]]):
    """Stand in for the runtime-configured service→failure_types map."""
    return patch.object(
        ReplayService, "_load_failure_type_map", autospec=True, return_value=value
    )


# =============================================================================
# parked_count_for_recovery — the five exits
# =============================================================================


class TestParkedCountForRecoveryBehavior:
    """The count, or None whenever "nothing is parked" cannot be proven."""

    @pytest.mark.parametrize("stored", [0, 3], ids=["empty", "three_parked"])
    def test_parked_count_unmapped_name_returns_the_strict_count_for_its_stored_domain(
        self, service, repository, stored
    ):
        """The strict read is asked for the stored domain, not the raw name."""
        repository.get_cluster_pending_count_by_domain.return_value = stored

        result = service.parked_count_for_recovery(REPROJECTED, failure_type_map={})

        assert result == stored
        repository.get_cluster_pending_count_by_domain.assert_called_once_with(
            resolve_stored_domain(REPROJECTED)
        )

    def test_parked_count_never_uses_the_substituting_pending_count(
        self, service, repository
    ):
        """The forgiving sibling answers from process memory on a Redis failure."""
        repository.get_cluster_pending_count_by_domain.return_value = 0

        service.parked_count_for_recovery(SERVICE, failure_type_map={})

        repository.get_pending_count_by_domain.assert_not_called()

    def test_parked_count_mapped_name_returns_none_without_reading_the_store(
        self, service, repository
    ):
        """A mapped type selects in every domain, so its own domain bounds nothing."""
        result = service.parked_count_for_recovery(
            SERVICE, failure_type_map={SERVICE: ["TIMEOUT"]}
        )

        assert result is None
        repository.get_cluster_pending_count_by_domain.assert_not_called()

    @pytest.mark.parametrize("name", UNADDRESSABLE, ids=["invalid_chars", "too_long"])
    def test_parked_count_name_without_a_domain_returns_none_without_reading_the_store(
        self, service, repository, name
    ):
        """The pooled fallback bucket cannot be attributed to one breaker."""
        assert resolve_stored_domain(name) == FALLBACK_DOMAIN

        result = service.parked_count_for_recovery(name, failure_type_map={})

        assert result is None
        repository.get_cluster_pending_count_by_domain.assert_not_called()

    def test_parked_count_read_that_raises_returns_none_and_logs_debug(
        self, service, repository
    ):
        """A store that cannot answer from its shared view keeps the loud path."""
        repository.get_cluster_pending_count_by_domain.side_effect = DLQError(
            "pending count unavailable: redis_inactive"
        )

        with capture_logs() as logs:
            result = service.parked_count_for_recovery(SERVICE, failure_type_map={})

        assert result is None
        unavailable = [
            e for e in logs if e["event"] == "replay_service.parked_count_unavailable"
        ]
        assert len(unavailable) == 1
        assert unavailable[0]["log_level"] == "debug"
        assert unavailable[0]["service_name"] == SERVICE
        assert unavailable[0]["healing_domain"] == resolve_stored_domain(SERVICE)
        assert "redis_inactive" in unavailable[0]["error"]

    @pytest.mark.parametrize(
        "returned",
        [-1, True, False, 1.5, "2", None],
        ids=["negative", "bool_true", "bool_false", "float", "string", "none"],
    )
    def test_parked_count_value_that_is_not_a_count_returns_none(
        self, service, repository, returned
    ):
        """A custom or mocked repository's non-count never reads as "nothing"."""
        repository.get_cluster_pending_count_by_domain.return_value = returned

        assert service.parked_count_for_recovery(SERVICE, failure_type_map={}) is None

    def test_parked_count_without_a_map_reads_the_runtime_map(
        self, service, repository
    ):
        """Omitting the map consults runtime configuration, which maps the name."""
        with _runtime_map({SERVICE: ["TIMEOUT"]}) as loader:
            result = service.parked_count_for_recovery(SERVICE)

        assert result is None
        loader.assert_called_once_with(service)
        repository.get_cluster_pending_count_by_domain.assert_not_called()

    def test_parked_count_with_an_empty_map_does_not_read_the_runtime_map(
        self, service, repository
    ):
        """An explicit empty map is the caller's resolved map, not "omitted"."""
        repository.get_cluster_pending_count_by_domain.return_value = 2

        with _runtime_map({SERVICE: ["TIMEOUT"]}) as loader:
            result = service.parked_count_for_recovery(SERVICE, failure_type_map={})

        assert result == 2
        loader.assert_not_called()


# =============================================================================
# recovery_is_idle — no lane AND a count of exactly 0
# =============================================================================


class TestRecoveryIsIdleBehavior:
    """Idle only when a pass could replay nothing and nothing is left behind."""

    @pytest.mark.parametrize(
        ("lanes", "expected"),
        [
            (_RecoveryLanes([], None, []), True),
            (_RecoveryLanes(["TIMEOUT"], None, []), False),
            (_RecoveryLanes([], SERVICE, []), False),
            (_RecoveryLanes([], None, [("TIMEOUT", SERVICE, None)]), False),
        ],
        ids=["no_lane", "mapped_only", "open_circuit_only", "declared_only"],
    )
    def test_recovery_lanes_any_single_source_is_a_lane(self, lanes, expected):
        """Each source alone gives the pass something it could replay."""
        assert lanes.is_empty is expected

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, True),
            (1, False),
            (DLQError("redis_inactive"), False),
        ],
        ids=["nothing_parked", "one_parked", "count_unavailable"],
    )
    def test_recovery_is_idle_name_without_a_lane_follows_the_count(
        self, service, repository, count, expected
    ):
        """Only a proven 0 ends the pass early; a parked entry or an unknown
        count leaves it to the sweep, which reports what it cannot replay."""
        if isinstance(count, Exception):
            repository.get_cluster_pending_count_by_domain.side_effect = count
        else:
            repository.get_cluster_pending_count_by_domain.return_value = count

        with _runtime_map({}):
            result = service.recovery_is_idle(SERVICE)

        assert result is expected
        repository.get_cluster_pending_count_by_domain.assert_called_once_with(
            resolve_stored_domain(SERVICE)
        )

    @pytest.mark.parametrize("lane", ["mapped", "handler"])
    def test_recovery_is_idle_name_with_a_lane_is_never_idle_and_reads_no_store(
        self, service, repository, register_handler, lane
    ):
        """Skipping a laned pass would race entries that become pending
        between a count and the selection, so the count is not even read."""
        repository.get_cluster_pending_count_by_domain.return_value = 0
        runtime_map: dict[str, list[str]] = {}
        if lane == "mapped":
            runtime_map = {SERVICE: ["TIMEOUT"]}
        else:
            register_handler(SERVICE)

        with _runtime_map(runtime_map):
            result = service.recovery_is_idle(SERVICE)

        assert result is False
        repository.get_cluster_pending_count_by_domain.assert_not_called()

    @pytest.mark.parametrize("name", UNADDRESSABLE, ids=["invalid_chars", "too_long"])
    def test_recovery_is_idle_name_without_a_domain_is_never_idle(
        self, service, repository, name
    ):
        """No lane, but its pooled bucket cannot be counted — keep the sweep."""
        repository.get_cluster_pending_count_by_domain.return_value = 0

        with _runtime_map({}):
            assert service.recovery_is_idle(name) is False

    def test_recovery_is_idle_reads_the_runtime_map_once(self, service, repository):
        """The lane check and the count decide from one snapshot of the map."""
        repository.get_cluster_pending_count_by_domain.return_value = 0

        with _runtime_map({}) as loader:
            service.recovery_is_idle(SERVICE)

        assert loader.call_count == 1
