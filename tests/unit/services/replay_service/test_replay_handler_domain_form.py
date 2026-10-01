"""Replay handlers are filed under the stored form of their domain.

A dead letter is stored under the stored form of the name its call was
protected under (``Payment-API`` -> ``payment_api``), and every replay path
looks its handler up by that stored form. A handler declared with the raw call
name must therefore be found under the stored form, or its entries replay
through the default handler, fail, and are escalated to review.

Reference:
    src/baldur/services/replay_service/handlers.py — ``_registry_key``
"""

from __future__ import annotations

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.services.replay_service import (
    ReplayResult,
    ReplayService,
    _replay_handlers,
    get_replay_handler,
    register_replay_handler,
)
from baldur.services.replay_service.handlers import (
    DefaultReplayHandler,
    ReplayHandler,
    has_replay_handler,
)
from baldur.utils.domain_validation import FALLBACK_DOMAIN, resolve_stored_domain


class _NamedHandler(ReplayHandler):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def domain(self) -> str:
        return self._name

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "reprocessed")


@pytest.fixture(autouse=True)
def _empty_registry():
    """The registry is process-global; each test starts and ends empty."""
    _replay_handlers.clear()
    yield
    _replay_handlers.clear()


class TestHandlerDomainFormContract:
    """Contract: the key a declared domain is filed and found under."""

    @pytest.mark.parametrize(
        ("declared", "stored"),
        [
            ("Payment-API", "payment_api"),
            ("Payment", "payment"),
            ("pay api", "pay_api"),
            ("payment", "payment"),
            ("payment.api", "payment.api"),
        ],
    )
    def test_declared_name_is_found_under_its_stored_form(self, declared, stored):
        handler = _NamedHandler(declared)
        register_replay_handler(handler)

        assert has_replay_handler(stored) is True
        assert get_replay_handler(stored) is handler

    def test_raw_name_lookup_finds_the_same_handler(self):
        handler = _NamedHandler("Payment-API")
        register_replay_handler(handler)

        assert get_replay_handler("Payment-API") is handler
        assert has_replay_handler("Payment-API") is True

    def test_unaddressable_name_is_not_filed_in_the_shared_bucket(self):
        """A name with no domain identity keeps its raw key, never OTHER_DOMAIN."""
        name = "x" * 80
        assert resolve_stored_domain(name) == FALLBACK_DOMAIN
        register_replay_handler(_NamedHandler(name))

        assert has_replay_handler(FALLBACK_DOMAIN) is False
        assert isinstance(get_replay_handler(FALLBACK_DOMAIN), DefaultReplayHandler)

    def test_handler_declared_as_the_shared_bucket_keeps_that_key(self):
        handler = _NamedHandler(FALLBACK_DOMAIN)
        register_replay_handler(handler)

        assert get_replay_handler(FALLBACK_DOMAIN) is handler


class TestHandlerDomainFormBehavior:
    """Behavior: an entry a raw-named call parked replays through its handler."""

    def test_entry_stored_under_the_stored_form_replays_through_raw_named_handler(
        self,
    ):
        repo = InMemoryFailedOperationRepository()
        entry = repo.create(
            domain=resolve_stored_domain("Payment-API"),
            failure_type="MAX_RETRIES_VALUEERROR",
        )
        register_replay_handler(_NamedHandler("Payment-API"))

        result = ReplayService(repository=repo)._execute_replay(
            entry.id, replay_type="batch"
        )

        assert result.success is True
