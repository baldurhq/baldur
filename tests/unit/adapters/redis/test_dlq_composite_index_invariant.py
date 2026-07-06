"""Unit tests for the DLQ composite-index write-path invariant (544 D6).

Across any sequence of write entry points (create, _update status
transition, delete), the composite ``(status, domain)`` ZSET membership
and the ``dlq:domains`` registry membership must match the entries'
actual ``(status, domain)`` -- zero drift.

The four write entry points:

  - ``create``                -- adds (PENDING, domain) to composite, adds
                                 domain to registry on first observation.
  - ``_update`` (status txn)  -- zrem (old_status, domain), zadd
                                 (new_status, domain).
  - ``delete``                -- zrem (status, domain) from composite.
  - ``_try_acquire_atomic``   -- zrem (PENDING, domain), zadd (REPLAYING,
                                 domain). Tested separately in
                                 test_dlq_lifecycle_atomic_acquire; this
                                 invariant test focuses on the three
                                 ``RedisDLQRepository``-owned write paths.

Test classes:
    TestCompositeIndexInvariantBehavior -- randomized sequences (seeded)
        of create / update / delete operations applied to a fake backend
        produce composite + registry state matching the blob-derived
        ground truth.
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.interfaces.repositories import FailedOperationStatus

_PENDING = FailedOperationStatus.PENDING.value
_RESOLVED = FailedOperationStatus.RESOLVED.value
_REPLAYING = FailedOperationStatus.REPLAYING.value
_REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value


# =============================================================================
# Fake backend -- minimal ResilientStorageBackend stand-in
# =============================================================================


class _FakeBackend:
    """Records every batch_write_ops call into in-memory blob + ZSET state.

    Replays the ops in order, applying ``set_blob`` / ``zadd`` / ``zrem`` /
    ``delete`` to the same in-memory structures the production replay path
    would touch. Ground truth for the invariant is the blob store; the
    composite + registry ZSETs are the "indexes under test"."""

    def __init__(self):
        self.config = MagicMock(key_prefix="")
        self.blobs: dict[str, bytes] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.is_degraded = False

    def _get_full_key(self, key):
        return key

    def set_blob(self, key, value):
        self.blobs[key] = value

    def get_blob(self, key):
        return self.blobs.get(key)

    def delete(self, key):
        existed = key in self.blobs
        self.blobs.pop(key, None)
        return existed

    def batch_write_ops(self, ops):
        for op_name, key, value in ops:
            if op_name == "set_blob":
                self.blobs[key] = value
            elif op_name == "delete":
                self.blobs.pop(key, None)
                self.zsets.pop(key, None)
            elif op_name == "zadd":
                self.zsets.setdefault(key, {}).update(value)
            elif op_name == "zrem":
                members = [value] if isinstance(value, str) else list(value)
                zset = self.zsets.get(key)
                if zset:
                    for m in members:
                        zset.pop(m, None)
        return True

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zrem(self, key, *members):
        zset = self.zsets.get(key, {})
        return sum(1 for m in members if zset.pop(m, None) is not None)


def _make_repo() -> tuple[RedisDLQRepository, _FakeBackend]:
    backend = _FakeBackend()
    repo = RedisDLQRepository(backend, pod_id="pod-a", pid=100, run_nonce="nonce0")
    repo._compression_enabled = MagicMock(return_value=False)
    return repo, backend


# =============================================================================
# Invariant verifier
# =============================================================================


def _blob_derived_ground_truth(
    backend: _FakeBackend, repo: RedisDLQRepository
) -> tuple[dict[tuple[str, str], set[str]], set[str]]:
    """Reconstruct what the composite + registry SHOULD contain from the
    blob store alone.

    Returns ``(composite_map, domains)`` where:
      - ``composite_map[(status, domain)] = {entry_id, ...}``
      - ``domains = {domain, ...}`` (every distinct domain ever created)
    """
    composite: dict[tuple[str, str], set[str]] = {}
    domains: set[str] = set()
    for key, blob in backend.blobs.items():
        if not key.startswith("dlq:entry:"):
            continue
        data = repo._decode_entry(blob)
        if not data:
            continue
        status = data.get("status", "")
        domain = data.get("domain", "")
        eid = data.get("id", "")
        if not (status and domain and eid):
            continue
        composite.setdefault((status, domain), set()).add(str(eid))
        domains.add(domain)
    return composite, domains


def _composite_state(backend: _FakeBackend) -> dict[tuple[str, str], set[str]]:
    """Project the backend's ZSET state onto a (status, domain) -> {ids} map."""
    result: dict[tuple[str, str], set[str]] = {}
    prefix = "dlq:status_domain:"
    for key, members in backend.zsets.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        # rest is "status:domain" -- split on the FIRST colon (status names
        # carry no colons today; domain may carry colons in principle but
        # the test fixture's domains do not).
        status, _, domain = rest.partition(":")
        result[(status, domain)] = set(members.keys())
    return result


def _registry_state(backend: _FakeBackend) -> set[str]:
    return set(backend.zsets.get("dlq:domains", {}).keys())


def _assert_invariant(repo, backend):
    """Assert composite + registry state == blob-derived ground truth."""
    expected_composite, expected_domains = _blob_derived_ground_truth(backend, repo)
    actual_composite = _composite_state(backend)
    actual_registry = _registry_state(backend)

    # Composite parity: every (s,d) pair in the blob ground truth lives in
    # the composite ZSETs with the same member set.
    for key, expected_members in expected_composite.items():
        assert actual_composite.get(key) == expected_members, (
            f"composite drift at {key}: "
            f"expected {expected_members}, got {actual_composite.get(key)}"
        )

    # The composite ZSETs hold no stale (s,d) pairs whose member sets are
    # non-empty -- a zrem may leave an empty dict but no orphan members.
    for key, members in actual_composite.items():
        if key not in expected_composite:
            assert members == set(), f"orphan composite members at {key}: {members}"

    # Registry parity: every distinct domain observed must live in the
    # registry. The reverse direction (registry has domains that no entry
    # references) is allowed -- the registry is sticky once added; that
    # matches Redis production behavior.
    assert expected_domains.issubset(actual_registry), (
        f"registry missing domains: expected {expected_domains}, got {actual_registry}"
    )


# =============================================================================
# Randomized sequence invariant
# =============================================================================


class TestCompositeIndexInvariantBehavior:
    """After any sequence of create / update / delete, composite + registry
    state matches the blob-derived ground truth (zero drift)."""

    @pytest.mark.parametrize("seed", [1, 2, 3, 7, 42])
    def test_random_sequence_preserves_composite_invariant(self, seed):
        rng = random.Random(seed)
        repo, backend = _make_repo()

        domains = ["payment", "auth", "inventory"]
        statuses = [_RESOLVED, _REPLAYING, _REQUIRES_REVIEW]
        active_ids: list[str] = []

        for _ in range(40):
            roll = rng.random()
            if roll < 0.5 or not active_ids:
                # create
                domain = rng.choice(domains)
                entry = repo.create(domain=domain, failure_type="t")
                active_ids.append(entry.id)
            elif roll < 0.85:
                # update status to a non-PENDING indexed status
                target = rng.choice(active_ids)
                repo._update(entry_id=target, status=rng.choice(statuses))
            else:
                # delete
                target = rng.choice(active_ids)
                repo.delete(target)
                active_ids.remove(target)

        _assert_invariant(repo, backend)

    def test_double_delete_is_idempotent_and_preserves_invariant(self):
        """Second delete on the same id returns False without raising and
        leaves the composite + registry state unchanged."""
        repo, backend = _make_repo()
        entry = repo.create(domain="payment", failure_type="t")

        assert repo.delete(entry.id) is True
        assert repo.delete(entry.id) is False
        _assert_invariant(repo, backend)

    def test_update_then_delete_clears_composite_for_new_status(self):
        """When an entry transitions PENDING -> RESOLVED and is then
        deleted, the composite (RESOLVED, domain) ZSET no longer
        carries the entry -- the delete path zrems against the CURRENT
        status, not the original PENDING."""
        repo, backend = _make_repo()
        entry = repo.create(domain="payment", failure_type="t")
        repo._update(entry_id=entry.id, status=_RESOLVED)
        repo.delete(entry.id)

        composite = _composite_state(backend)
        # Both PENDING and RESOLVED composites must end up empty for this id.
        pending_members = composite.get((_PENDING, "payment"), set())
        resolved_members = composite.get((_RESOLVED, "payment"), set())
        assert entry.id not in pending_members
        assert entry.id not in resolved_members

    def test_two_domains_have_independent_composites(self):
        """Two creates in different domains write to disjoint composite
        ZSETs and the registry carries both."""
        repo, backend = _make_repo()
        a = repo.create(domain="payment", failure_type="t")
        b = repo.create(domain="auth", failure_type="t")

        composite = _composite_state(backend)
        assert composite.get((_PENDING, "payment")) == {a.id}
        assert composite.get((_PENDING, "auth")) == {b.id}
        assert "payment" in _registry_state(backend)
        assert "auth" in _registry_state(backend)

    def test_status_transition_moves_id_between_composites(self):
        """A status transition removes the id from (old_status, domain)
        and adds it to (new_status, domain) atomically (one batch)."""
        repo, backend = _make_repo()
        entry = repo.create(domain="payment", failure_type="t")
        repo._update(entry_id=entry.id, status=_RESOLVED)

        composite = _composite_state(backend)
        assert entry.id not in composite.get((_PENDING, "payment"), set())
        assert composite.get((_RESOLVED, "payment")) == {entry.id}
