"""749 D12 — the keyspace scan finds what the repository wrote, on real Redis.

Every write resolves its physical key through the backend's key seam, which
applies the *dynamic* namespace prefix when namespacing is enabled. The three
enumeration sites built their SCAN pattern from the *static* configured prefix
instead, so a namespaced deployment scanned a prefix nothing had ever been
written under — and reported an empty keyspace, indistinguishable from a store
with nothing in it. The full-state scan is what hydrates the layered
repository, so an operator's pinned row was among the rows that went missing.

The unit test's fake reproduces ``_get_full_key`` by construction, which is
exactly what makes it insufficient: it proves the fake agrees with itself. Only
a live round-trip shows that the pattern the adapter builds matches the keys
Redis actually holds.

Requires a running Redis instance (auto-skip via ``requires_redis``).
"""

from __future__ import annotations

import pytest

from baldur.adapters.redis.circuit_breaker import RedisCircuitBreakerStateRepository
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.settings.namespace import (
    get_effective_key_prefix,
    reset_namespace_settings,
)
from baldur.settings.resilient_storage import ResilientStorageSettings

pytestmark = pytest.mark.requires_redis

SERVICE = "payment-api"
STATIC_PREFIX = "test:baldur:"


@pytest.fixture
def namespaced_repo(redis_client, redis_url, monkeypatch):
    """A repository on a namespaced deployment — the shape that broke.

    ``use_dynamic_prefix=True`` is production's default; the fixture only adds
    the namespace that makes the dynamic prefix differ from the static one.
    Without that difference the pre-fix pattern would coincide with the correct
    one and nothing here could fail.
    """
    monkeypatch.setenv("BALDUR_NAMESPACE_NAMESPACE_ENABLED", "true")
    monkeypatch.setenv("BALDUR_NAMESPACE_NAMESPACE", "seoul")
    reset_namespace_settings()
    assert get_effective_key_prefix() != STATIC_PREFIX

    settings = ResilientStorageSettings(
        redis_url=redis_url,
        key_prefix=STATIC_PREFIX,
        use_dynamic_prefix=True,
        allow_memory_only=True,
    )
    backend = ResilientStorageBackend(settings=settings)
    repo = RedisCircuitBreakerStateRepository(backend=backend)
    # Force the lazy connect so the scan paths take their Redis branch.
    assert backend.ensure_redis() is True

    yield repo

    for key in redis_client.keys(f"{get_effective_key_prefix()}*"):
        redis_client.delete(key)
    reset_namespace_settings()


class TestNamespacedEnumerationRoundTrip:
    """What the repository wrote under a namespace, it can enumerate back."""

    def test_enumeration_prefix_returns_the_namespaced_row(
        self, namespaced_repo, redis_client
    ):
        """
        Purpose:
            A row written through the repository is returned by the full-state
            scan on a namespaced deployment.
        Expected:
            - the row comes back, and its service name round-trips intact
            - the physical key really does carry the namespace (so the test is
              not passing because namespacing quietly did nothing)
        """
        namespaced_repo.get_or_create(SERVICE)

        states = namespaced_repo.get_all_states()

        assert [s.service_name for s in states] == [SERVICE]
        physical = f"{get_effective_key_prefix()}cb:{SERVICE}"
        assert redis_client.exists(physical) == 1

    def test_the_pre_fix_pattern_matches_nothing_in_the_same_keyspace(
        self, namespaced_repo, redis_client
    ):
        """The negative half, run against the very keys the test just wrote.

        This is the assertion that makes the row above non-vacuous: the pattern
        the adapter used before — static configured prefix plus the component
        prefix — selects zero keys from a keyspace that demonstrably holds one.
        """
        namespaced_repo.get_or_create(SERVICE)

        # Built from the seam writes use, not from the adapter's own helper —
        # the property under test is that the two agree, so deriving both
        # sides from the same helper would assert nothing.
        seam_pattern = f"{namespaced_repo._backend._get_full_key('cb:')}*"
        pre_fix_pattern = f"{STATIC_PREFIX}cb:*"

        assert redis_client.keys(pre_fix_pattern) == []
        assert len(redis_client.keys(seam_pattern)) == 1

    def test_an_open_row_is_found_by_the_scan_based_site(self, namespaced_repo):
        """``get_open_states`` uses SCAN rather than KEYS — same pattern bug."""
        namespaced_repo.get_or_create(SERVICE)
        namespaced_repo.update_state(SERVICE, state="open")

        open_states = namespaced_repo.get_open_states()

        assert [s.service_name for s in open_states] == [SERVICE]

    def test_a_pinned_row_survives_the_round_trip_through_enumeration(
        self, namespaced_repo
    ):
        """The row that mattered: an operator's block, seen by the scan.

        This is the payload the layered repository's construction-time load
        hydrates from; if the scan misses it, a fresh process starts unaware
        that the service is blocked.
        """
        namespaced_repo.atomic_force_open(SERVICE, reason="incident", ttl_minutes=90)

        (state,) = namespaced_repo.get_all_states()

        assert state.service_name == SERVICE
        assert state.state == "open"
        assert state.manually_controlled is True
        assert state.manual_override_expires_at is not None
