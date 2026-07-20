"""Outbound 429-cooldown lifecycle — RetryPolicy x RateLimitCoordinator x storage.

Mock-based integration (real ``InMemoryRateLimitStorage`` adapter, no infra).

Unit tests pre-seed the cooldown and test each component in isolation. These
tests exercise the *composition* the unit level cannot: a real 429 flows through
the retry loop into ``on_rate_limited`` (writing ``cooldown_until`` to shared
storage), and a later attempt's ``wait_if_needed`` reads that freshly-installed
value back to decide serve-vs-defer. The read-after-write across attempts, and
across two independent policies sharing one storage, is the integration contract.

Test Categories:
    A. Cooldown install -> read-back within one call:
        - A 429 on attempt 1 installs a cooldown that attempt 2 defers on
        - A cooldown that fits the budget is served, and the retried call succeeds
        - A recovery success resets the consecutive-429 counter in storage
    B. Cross-worker shared state:
        - A cooldown installed by one worker defers an independent worker that
          never saw a 429 of its own
    C. The same lifecycle reached through the *default* wiring, with no
       coordinator handed to the policy
    D. Cross-instance sharing over real Redis — the one claim the in-memory
       adapter cannot make, since its state lives in the instance

Note: Categories A-C use the in-memory rate-limit adapter - no infra dependency.
      This enables parallel test execution with pytest-xdist. Category D is
      marked ``requires_redis`` and auto-skips without it.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.rate_limit_coordinator import (
    RateLimitCoordinator,
    RateLimitDeferredError,
)
from baldur.services.rate_limit_coordinator.models import RateLimitCoordinatorConfig
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from tests.factories.time_helpers import mock_sleep

# A 429-detectable message (baldur.services.retry_handler.rate_limit_detection).
_RATE_LIMIT_MESSAGE = "429 too many requests"


@pytest.fixture(autouse=True)
def _no_cluster_broadcast():
    """Neutralize the out-of-scope cluster 429 broadcast for every test here.

    ``on_rate_limited`` fires ``_broadcast_to_cluster``, a fail-open seam wrapping
    the Dormant-tier Kafka channel that eagerly attempts a broker connection — an
    explicit NON-GOAL of this feature. Left live it makes the test slow (~2s
    connect timeout per 429) and infra-dependent. Patching the coordinator's own
    broadcast seam guarantees no channel is fetched or built during these tests,
    keeping the cooldown lifecycle the sole subject (UNIT_TEST_GUIDELINES §6.4).
    """
    with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
        yield


def _coordinator(storage, *, default_retry_after: float) -> RateLimitCoordinator:
    """Deterministic coordinator (jitter off) over a given storage."""
    config = RateLimitCoordinatorConfig(
        jitter_percent=0.0,
        debounce_window_seconds=0.0,
        default_retry_after=default_retry_after,
    )
    return RateLimitCoordinator(storage=storage, config=config)


def _policy(coordinator, *, domain: str, max_elapsed: float) -> RetryPolicy:
    """Retry loop wired to the coordinator, zero backoff so budget = the cooldown wait."""
    return RetryPolicy(
        config=RetryPolicyConfig(
            max_attempts=3, domain=domain, max_elapsed=max_elapsed
        ),
        rate_limit_coordinator=coordinator,
        backoff=ConstantBackoff(delay=0.0),
        sleeper=lambda _: None,
    )


class TestOutboundCooldownLifecycle:
    """The 429 -> cooldown -> next-attempt-reads-it chain through all three components.

    Validates:
    - A 429 raised inside the retry loop reaches storage as a real cooldown
    - The next attempt reads that cooldown back and decides serve-vs-defer on it
    - A shared storage propagates one worker's cooldown to another worker
    """

    def test_429_installs_cooldown_and_next_attempt_defers(self):
        """
        Purpose:
            A real 429 installs a cooldown that the next attempt reads back and
            defers on. The cooldown is NOT pre-seeded — attempt 1's 429 drives
            on_rate_limited, and attempt 2's wait_if_needed reads the just-written
            value.
        Expected:
            - Outcome is FAILURE with reason "rate_limit_deferred" and not_before set
            - The error is the real prior 429, not a synthesized deferral error
            - Only attempt 1 is recorded in retry_history
            - Storage holds an active cooldown with consecutive_429s == 1
        """
        storage = InMemoryRateLimitStorage()
        coord = _coordinator(storage, default_retry_after=10.0)
        policy = _policy(coord, domain="payment", max_elapsed=2.0)

        def func():
            raise Exception(_RATE_LIMIT_MESSAGE)

        with mock_sleep():
            result = policy.execute(func)

        # Deferred exit, carrying the real prior 429 (not a synthesized error).
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert result.metadata["not_before"] is not None
        assert _RATE_LIMIT_MESSAGE in str(result.error)
        assert len(result.metadata["retry_history"]) == 1  # only attempt 1 ran

        # The coordinator actually wrote the cooldown to shared storage.
        state = storage.get_state("payment")
        assert state.is_in_cooldown is True
        assert state.consecutive_429s == 1

    def test_429_then_fitting_cooldown_is_served_and_call_succeeds(self):
        """
        Purpose:
            A cooldown that fits the caller's budget is waited out in full, then
            the retried call succeeds.
        Expected:
            - Outcome is SUCCESS carrying the second attempt's value
            - Attempt 2 slept the installed cooldown (~0.5s) before running
        """
        storage = InMemoryRateLimitStorage()
        coord = _coordinator(storage, default_retry_after=0.5)
        policy = _policy(coord, domain="payment", max_elapsed=30.0)
        calls = {"n": 0}

        def func():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        with mock_sleep() as sleep_mock:
            result = policy.execute(func)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        # Attempt 2 waited out the installed cooldown (~0.5s) before running.
        assert any(0.4 <= slept <= 0.6 for slept in sleep_mock.calls)

    def test_recovery_success_resets_the_consecutive_counter(self):
        """
        Purpose:
            After the retried call succeeds, on_success clears the consecutive-429
            counter on the shared store.
        Expected:
            - storage.get_state(domain).consecutive_429s == 0 after the call
        """
        storage = InMemoryRateLimitStorage()
        coord = _coordinator(storage, default_retry_after=0.5)
        policy = _policy(coord, domain="payment", max_elapsed=30.0)
        calls = {"n": 0}

        def func():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        with mock_sleep():
            policy.execute(func)

        # The success drove on_success -> reset_consecutive_429s on the shared store.
        assert storage.get_state("payment").consecutive_429s == 0

    def test_shared_cooldown_defers_a_second_worker_without_its_own_429(self):
        """
        Purpose:
            One worker's 429 cooldown, held in shared storage, defers an
            independent worker. Worker B never raises a 429 of its own — it defers
            purely on the cooldown worker A installed, proving the cross-worker
            shared-state contract.
        Expected:
            - B exits with reason "rate_limit_deferred"
            - B's wrapped call never ran and B slept nothing
            - B's error is a synthesized RateLimitDeferredError (no prior error)
        """
        storage = InMemoryRateLimitStorage()

        # Worker A hits a 429 and installs a long cooldown into the shared store.
        coord_a = _coordinator(storage, default_retry_after=10.0)
        policy_a = _policy(coord_a, domain="payment", max_elapsed=2.0)
        with mock_sleep():
            policy_a.execute(
                lambda: (_ for _ in ()).throw(Exception(_RATE_LIMIT_MESSAGE))
            )
        assert storage.get_state("payment").is_in_cooldown is True

        # Worker B is a separate coordinator + policy over the SAME storage.
        coord_b = _coordinator(storage, default_retry_after=10.0)
        policy_b = _policy(coord_b, domain="payment", max_elapsed=2.0)
        b_calls = {"n": 0}

        def b_func():
            b_calls["n"] += 1
            return "ok"

        with mock_sleep() as sleep_mock:
            result = policy_b.execute(b_func)

        assert result.metadata["reason"] == "rate_limit_deferred"
        assert b_calls["n"] == 0  # B's call never ran
        assert sleep_mock.call_count == 0  # and it slept nothing
        # B had no prior error of its own, so the deferral error is synthesized.
        assert type(result.error) is RateLimitDeferredError


class TestDefaultWiredCooldownLifecycle:
    """The same lifecycle with nothing injected — the shape production runs.

    Every case above hands the policy a coordinator. Production does not: a
    settings-derived ``RetryPolicy`` is built with none and resolves the shared
    one at use time. That resolution is the composition seam this feature added,
    and it is the one a caller never exercises explicitly, so the whole chain is
    re-driven through it here.
    """

    def test_a_default_wired_policy_installs_and_honors_its_own_cooldown(self):
        """
        Purpose:
            A policy that was handed no coordinator still writes a 429 cooldown
            to shared storage and reads it back on the next attempt.
        Expected:
            - The retried call succeeds after waiting out the installed cooldown
            - Storage carries the cooldown the loop itself installed
            - The recovery success clears the consecutive-429 counter
        """
        storage = InMemoryRateLimitStorage()
        calls = {"n": 0}

        def func():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        # Stand the shared coordinator up over in-memory storage. Patching
        # get_instance rather than the storage auto-detect keeps the resolution
        # path itself under test — only the backend choice is pinned.
        policy = RetryPolicy(
            config=RetryPolicyConfig(
                max_attempts=3, domain="payment", max_elapsed=30.0
            ),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )
        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_coordinator(storage, default_retry_after=0.5),
        ):
            with mock_sleep() as sleep_mock:
                result = policy.execute(func)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        # Attempt 2 waited out the cooldown attempt 1's 429 installed.
        assert any(0.4 <= slept <= 0.6 for slept in sleep_mock.calls)
        # And the recovery reset the ladder for the next 429.
        assert storage.get_state("payment").consecutive_429s == 0

    def test_an_unidentified_default_wired_policy_writes_nothing(self):
        """
        Purpose:
            The paired negative at composition level: without a domain identity
            the default wiring must not reach storage at all.
        Expected:
            - The 429 exhausts retries normally (no coordination side effects)
            - Storage holds no cooldown and no counter for the placeholder key
        """
        storage = InMemoryRateLimitStorage()
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2, max_elapsed=30.0),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )

        with patch.object(
            RateLimitCoordinator,
            "get_instance",
            autospec=True,
            return_value=_coordinator(storage, default_retry_after=10.0),
        ):
            with mock_sleep():
                result = policy.execute(
                    lambda: (_ for _ in ()).throw(Exception(_RATE_LIMIT_MESSAGE))
                )

        assert result.outcome == PolicyOutcome.FAILURE
        state = storage.get_state("default")
        assert state.is_in_cooldown is False
        assert state.consecutive_429s == 0


@pytest.mark.requires_redis
class TestCrossInstanceCooldownSharingOverRedis:
    """Two independently constructed storages share one cooldown record.

    The in-memory case above shares a storage *object*, so it proves the
    coordinator reads what it wrote — not that two workers in different
    processes converge. In-memory state lives in the instance, so it cannot make
    that claim by construction. Redis is the backend the shared-cooldown promise
    actually rests on, and this is the only test that puts weight on it.
    """

    def test_a_cooldown_written_by_one_instance_defers_another(self, redis_client):
        """
        Purpose:
            Worker A's 429, written through its own RedisRateLimitStorage,
            defers worker B holding a separate storage instance over the same
            Redis — the cross-process contract.
        Expected:
            - B defers without ever running its call or raising a 429 of its own
        """
        from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage

        key = f"lifecycle-test-{uuid.uuid4().hex}"
        storage_a = RedisRateLimitStorage(redis_client)
        storage_b = RedisRateLimitStorage(redis_client)

        policy_a = _policy(
            _coordinator(storage_a, default_retry_after=30.0),
            domain=key,
            max_elapsed=2.0,
        )
        policy_b = _policy(
            _coordinator(storage_b, default_retry_after=30.0),
            domain=key,
            max_elapsed=2.0,
        )
        b_calls = {"n": 0}

        def b_func():
            b_calls["n"] += 1
            return "ok"

        try:
            with mock_sleep():
                policy_a.execute(
                    lambda: (_ for _ in ()).throw(Exception(_RATE_LIMIT_MESSAGE))
                )
                result = policy_b.execute(b_func)

            assert result.metadata["reason"] == "rate_limit_deferred"
            assert b_calls["n"] == 0  # B never ran, on a cooldown it never wrote
        finally:
            redis_client.delete(
                f"ratelimit:{key}:cooldown_until",
                f"ratelimit:{key}:consecutive_429s",
                f"ratelimit:{key}:last_updated",
            )
