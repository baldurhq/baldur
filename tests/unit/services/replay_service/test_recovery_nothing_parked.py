"""What a recovery with no lane leaves behind (809 D1, D2).

A recovery pass for a name with no lane — no replay handler for its stored
domain, or no domain of its own — replays nothing whatever is stored (every
automatic lane, a mapped one included, needs the domain's handler). The only
question left is whether it leaves parked work behind, which the operator must
hear about. ``parked_count_for_recovery`` answers that from the shared store, or
answers None when it cannot; every caller reads None as "work may be parked",
the loud direction. ``recovery_is_idle`` is True only for a name with no lane
AND a count of exactly 0 — the one case a pass can end before it starts.

Verification techniques applied:
- Decision table: the four exits of the count (no domain of its own, the read
  raised, not a count, a count), each pinned by its distinct result
- Boundary: 0 is a count; -1, a bool, a float and a string are not
- Dependency interaction: the strict read is handed the stored domain, and is
  never reached when a cheaper exit has already decided
- Branch-outcome completeness: a handler makes a name non-idle; a mapping
  without one does not
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from unittest.mock import PropertyMock, create_autospec, patch

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
from baldur.services.replay_service.service import recovery_lanes
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
    def test_parked_count_returns_the_strict_count_for_its_stored_domain(
        self, service, repository, stored
    ):
        """The strict read is asked for the stored domain, not the raw name."""
        repository.get_cluster_pending_count_by_domain.return_value = stored

        result = service.parked_count_for_recovery(REPROJECTED)

        assert result == stored
        repository.get_cluster_pending_count_by_domain.assert_called_once_with(
            resolve_stored_domain(REPROJECTED)
        )

    def test_parked_count_never_uses_the_substituting_pending_count(
        self, service, repository
    ):
        """The forgiving sibling answers from process memory on a Redis failure."""
        repository.get_cluster_pending_count_by_domain.return_value = 0

        service.parked_count_for_recovery(SERVICE)

        repository.get_pending_count_by_domain.assert_not_called()

    def test_parked_count_mapped_name_is_counted_in_its_own_domain(
        self, service, repository
    ):
        """A mapped type selects only under the mapped service's own domain, so
        that domain's count bounds what a recovery could replay."""
        repository.get_cluster_pending_count_by_domain.return_value = 4

        with _runtime_map({SERVICE: ["TIMEOUT"]}):
            result = service.parked_count_for_recovery(SERVICE)

        assert result == 4
        repository.get_cluster_pending_count_by_domain.assert_called_once_with(
            resolve_stored_domain(SERVICE)
        )

    @pytest.mark.parametrize("name", UNADDRESSABLE, ids=["invalid_chars", "too_long"])
    def test_parked_count_name_without_a_domain_returns_none_without_reading_the_store(
        self, service, repository, name
    ):
        """The pooled fallback bucket cannot be attributed to one breaker."""
        assert resolve_stored_domain(name) == FALLBACK_DOMAIN

        result = service.parked_count_for_recovery(name)

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
            result = service.parked_count_for_recovery(SERVICE)

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

        assert service.parked_count_for_recovery(SERVICE) is None

    def test_parked_count_does_not_read_the_runtime_map(self, service, repository):
        """The count depends on the domain alone, never on the operator map."""
        repository.get_cluster_pending_count_by_domain.return_value = 2

        with _runtime_map({SERVICE: ["TIMEOUT"]}) as loader:
            result = service.parked_count_for_recovery(SERVICE)

        assert result == 2
        loader.assert_not_called()


# =============================================================================
# recovery_is_idle — no lane AND a count of exactly 0
# =============================================================================


class TestRecoveryIsIdleBehavior:
    """Idle only when a pass could replay nothing and nothing is left behind."""

    @pytest.mark.parametrize(
        ("handler", "failure_type_map", "has_lane"),
        [
            (False, {}, False),
            (False, {SERVICE: ["TIMEOUT"]}, False),
            (True, {}, True),
            (True, {SERVICE: ["TIMEOUT"]}, True),
        ],
        ids=["nothing", "mapped_only", "handler_only", "handler_and_mapped"],
    )
    def test_recovery_lanes_need_the_domain_s_replay_handler(
        self, register_handler, handler, failure_type_map, has_lane
    ):
        """A handler alone gives the pass something it could replay; a mapping
        without a handler gives nothing (its lane would replay through the
        default handler, which always fails)."""
        if handler:
            register_handler(SERVICE)

        assert bool(recovery_lanes(SERVICE, failure_type_map)) is has_lane

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

    def test_recovery_is_idle_name_with_a_lane_is_never_idle_and_reads_no_store(
        self, service, repository, register_handler
    ):
        """Skipping a laned pass would race entries that become pending
        between a count and the selection, so the count is not even read."""
        repository.get_cluster_pending_count_by_domain.return_value = 0
        register_handler(SERVICE)

        with _runtime_map({}):
            result = service.recovery_is_idle(SERVICE)

        assert result is False
        repository.get_cluster_pending_count_by_domain.assert_not_called()

    def test_recovery_is_idle_mapped_name_without_a_handler_follows_the_count(
        self, service, repository
    ):
        """A mapping alone gives no lane, so nothing parked is an idle pass."""
        repository.get_cluster_pending_count_by_domain.return_value = 0

        with _runtime_map({SERVICE: ["TIMEOUT"]}):
            result = service.recovery_is_idle(SERVICE)

        assert result is True

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

    def test_recovery_is_idle_failure_while_deciding_answers_not_idle(
        self, service, repository, register_handler
    ):
        """A handler whose declared types break lane resolution: the check
        answers False instead of raising, so the pass runs as it would have."""
        handler = register_handler(SERVICE)
        repository.get_cluster_pending_count_by_domain.return_value = 0

        with (
            patch.object(
                type(handler),
                "auto_replay_failure_types",
                new_callable=PropertyMock,
                return_value=(["MAX_RETRIES_TIMEOUTERROR"],),
            ),
            _runtime_map({}),
            capture_logs() as logs,
        ):
            result = service.recovery_is_idle(SERVICE)

        assert result is False
        unavailable = [
            e for e in logs if e["event"] == "replay_service.recovery_idle_unavailable"
        ]
        assert len(unavailable) == 1
        assert unavailable[0]["log_level"] == "debug"
        assert unavailable[0]["service_name"] == SERVICE
