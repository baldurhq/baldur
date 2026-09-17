"""Replay execution as the read-side declaration site for the metric label.

The metric registry is per process, while a dead letter's domain was declared
by the process that captured it. A process that replays before it has made a
single protected call in that domain (a cron job that replays first, then
sweeps) would otherwise emit its first ``replay.started`` under the fallback
label and only switch to the real one once the handler's own protected call
happened to register it.

Reference:
    src/baldur/services/replay_service/service.py — ``_execute_replay``
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.metrics.registry import (
    _registered_domains,
    reset_registered_domains,
    resolve_domain_label,
)
from baldur.services.replay_service import ReplayResult, ReplayService, _replay_handlers
from baldur.services.replay_service.handlers import ReplayHandler
from baldur.utils.domain_validation import FALLBACK_DOMAIN

# Not one of the registry's shipped defaults, so a fresh registry does not
# know it — the situation of a replaying process that has not called
# ``protect()`` for this domain yet.
STORED_DOMAIN = "reddit"


class StoredDomainHandler(ReplayHandler):
    @property
    def domain(self) -> str:
        return STORED_DOMAIN

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "OK")


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is process-global; start from the shipped defaults only."""
    original = _registered_domains.copy()
    reset_registered_domains()
    yield
    reset_registered_domains()
    _registered_domains.clear()
    _registered_domains.update(original)


@pytest.fixture(autouse=True)
def _handler():
    _replay_handlers.clear()
    _replay_handlers[STORED_DOMAIN] = StoredDomainHandler()
    yield
    _replay_handlers.clear()


@pytest.fixture
def repo():
    """Real in-process double — the entry is a stored row, as in production."""
    return InMemoryFailedOperationRepository()


@pytest.fixture
def stored_id(repo):
    return repo.create(
        domain=STORED_DOMAIN, failure_type="MAX_RETRIES_HTTPSTATUSERROR"
    ).id


@pytest.fixture
def replay_service(repo):
    return ReplayService(repository=repo)


class TestReplayStartedLabelBehavior:
    """Behavior: the label the started event resolves to, in a fresh process."""

    def test_stored_domain_is_registered_before_replay_started_fires(
        self, replay_service, stored_id
    ):
        """The first started event of a domain resolves to that domain, not the fallback."""
        assert resolve_domain_label(STORED_DOMAIN) == FALLBACK_DOMAIN  # fresh process

        seen_at_call: list[str] = []
        with patch(
            "baldur.metrics.event_handlers.ReplayEventHandler.on_replay_started",
            side_effect=lambda domain, replay_type: seen_at_call.append(
                resolve_domain_label(domain)
            ),
        ):
            result = replay_service._execute_replay(stored_id, replay_type="batch")

        assert result.success is True
        assert seen_at_call == [STORED_DOMAIN]
