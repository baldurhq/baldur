"""741 D6 — the manual-override expiry column, against real Redis.

Redis was the adapter that carried the stale-expiry bug: ``atomic_force_close``
left ``manual_override_expires_at`` out of its update map entirely, so a
previous force-open's expiry stayed on the hash and governed the force-close
pin — the sweep would clear an Allow at a timestamp that belonged to a Block.
The unit tests assert the payload the adapter builds; only a real round-trip
shows what the hash ends up holding after two successive writes.

Requires a running Redis instance (auto-skip via ``requires_redis``).
"""

from __future__ import annotations

import pytest

from baldur.utils.time import utc_now

pytestmark = pytest.mark.requires_redis

SERVICE = "payment-api"


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset the runtime-scoped Redis negative cache before each test."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


class TestRedisManualOverrideExpiryRoundTrip:
    """What the hash holds after a manual override is written."""

    def test_force_open_stores_the_requested_lifetime(
        self, redis_circuit_breaker_repository
    ):
        repo = redis_circuit_breaker_repository
        before = utc_now()

        repo.atomic_force_open(SERVICE, reason="incident", ttl_minutes=30)

        state = repo.get_by_service_name(SERVICE)
        assert state.manually_controlled is True
        assert state.manual_override_expires_at > before

    def test_force_open_with_a_non_positive_ttl_stores_no_expiry(
        self, redis_circuit_breaker_repository
    ):
        """It used to store ``now + ttl`` unguarded — a past timestamp.

        The same request stored a permanent pin on SQL and memory, so the
        operator's action meant opposite things depending on the backend.
        """
        repo = redis_circuit_breaker_repository

        repo.atomic_force_open(SERVICE, reason="incident", ttl_minutes=0)

        state = repo.get_by_service_name(SERVICE)
        assert state.manually_controlled is True
        assert state.manual_override_expires_at is None

    def test_force_close_does_not_inherit_a_previous_blocks_expiry(
        self, redis_circuit_breaker_repository
    ):
        """The latent bug: the column survived the force-close untouched."""
        # Given: a block with a long lifetime already on the hash.
        repo = redis_circuit_breaker_repository
        repo.atomic_force_open(SERVICE, reason="block", ttl_minutes=1440)
        blocked_expiry = repo.get_by_service_name(SERVICE).manual_override_expires_at
        assert blocked_expiry is not None

        # When: the operator allows the service again for five minutes.
        repo.atomic_force_close(SERVICE, reason="allow", ttl_minutes=5)

        # Then: the stored expiry belongs to the allow, not to the block.
        state = repo.get_by_service_name(SERVICE)
        assert state.state == "closed"
        assert state.manual_override_expires_at < blocked_expiry

    def test_force_close_without_a_ttl_clears_the_expiry_column(
        self, redis_circuit_breaker_repository
    ):
        repo = redis_circuit_breaker_repository
        repo.atomic_force_open(SERVICE, reason="block", ttl_minutes=1440)

        repo.atomic_force_close(SERVICE, reason="allow", ttl_minutes=None)

        assert repo.get_by_service_name(SERVICE).manual_override_expires_at is None
