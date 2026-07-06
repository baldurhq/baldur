"""Unit tests for the keyed audit hash chain (632 — AUDIT_HASHCHAIN_HMAC_SIGNING).

Covers the new forge-resistance coverage owned by ``/test``:

- ``models.compute_hash`` keyed (HMAC-SHA256) vs keyless (plain SHA-256)
  dispatch driven purely by ``audit_signing_key`` presence.
- ``models._audit_signing_key`` key resolution (set / unset / empty-string),
  read fresh on every call.
- ``HashChainVerifier`` forge rejection and no-silent-downgrade — a chain
  whose ``current_hash`` was (re)computed without the key fails under a keyed
  verifier; a keyless chain still verifies when no key is configured.
- The verify-side ``current_hash`` comparison goes through
  ``hmac.compare_digest`` (timing-safe, D8).
- ``sampling._compute_hash`` / ``fallback._compute_hash`` delegate to the
  single keyed-aware source (D2), so no chain site re-implements the algorithm.

Key-state control (Testability Notes / memory ``reference_runtime_settings_cache_flaky``):
``tests/testapp/settings.py`` ambiently sets ``BALDUR_SECRETS_AUDIT_SIGNING_KEY``
at import, so every test MUST control the key explicitly via
``monkeypatch.setenv``/``delenv`` + ``reset_secrets_settings()`` and never rely
on ambient state. The autouse fixture resets the secrets cache around each test
so the restored (ambient) env is re-read for the next test.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from baldur.audit.integrity import HashChainManager, HashChainVerifier
from baldur.audit.integrity.models import _audit_signing_key, compute_hash
from baldur.settings.secrets import reset_secrets_settings
from baldur.utils.serialization import fast_canonical_dumps

_KEY_ENV = "BALDUR_SECRETS_AUDIT_SIGNING_KEY"


# =============================================================================
# Helpers — explicit key-state control (never rely on ambient test-value key)
# =============================================================================


def _set_key(monkeypatch, value: str) -> None:
    """Configure ``audit_signing_key`` and force a fresh secrets read."""
    monkeypatch.setenv(_KEY_ENV, value)
    reset_secrets_settings()


def _unset_key(monkeypatch) -> None:
    """Remove ``audit_signing_key`` (keyless mode) and force a fresh read."""
    monkeypatch.delenv(_KEY_ENV, raising=False)
    reset_secrets_settings()


def _expected_keyed(data: dict, key: str) -> str:
    """Reference HMAC-SHA256 the way ``compute_hash`` computes it."""
    payload = fast_canonical_dumps(data, default=str)
    return hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()


def _expected_keyless(data: dict) -> str:
    """Reference keyless SHA-256 the way ``compute_hash`` computes it."""
    payload = fast_canonical_dumps(data, default=str)
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture(autouse=True)
def _reset_secrets_cache():
    """Isolate the secrets cache so per-test key state never leaks."""
    reset_secrets_settings()
    yield
    reset_secrets_settings()


# =============================================================================
# compute_hash — keyed vs keyless dispatch
# =============================================================================


class TestComputeHashKeyedMode:
    """``compute_hash`` selects HMAC vs keyless SHA-256 by key presence."""

    def test_compute_hash_with_key_set_produces_hmac(self, monkeypatch):
        # Given a configured signing key
        _set_key(monkeypatch, "signing-key-A")
        data = {"event": "payment_failed", "amount": 42}

        # When hashing
        result = compute_hash(data)

        # Then it equals the keyed HMAC-SHA256 of the canonical payload
        assert result == _expected_keyed(data, "signing-key-A")

    def test_compute_hash_with_key_unset_produces_plain_sha256(self, monkeypatch):
        # Given no signing key
        _unset_key(monkeypatch)
        data = {"event": "payment_failed", "amount": 42}

        # When hashing
        result = compute_hash(data)

        # Then it equals the keyless SHA-256 of the canonical payload
        assert result == _expected_keyless(data)

    def test_compute_hash_with_empty_key_uses_keyless_mode(self, monkeypatch):
        # Given an empty-string key (load-bearing: empty == unset, D4)
        _set_key(monkeypatch, "")
        data = {"event": "payment_failed", "amount": 42}

        # When hashing, an empty key must NOT silently select HMAC mode
        result = compute_hash(data)

        # Then it equals the keyless hash, not an HMAC under a zero-length key
        assert result == _expected_keyless(data)

    def test_compute_hash_keyed_differs_from_keyless_for_same_entry(self, monkeypatch):
        # Given the same entry hashed in each mode
        data = {"event": "payment_failed", "amount": 42}
        _set_key(monkeypatch, "signing-key-A")
        keyed = compute_hash(data)
        _unset_key(monkeypatch)
        keyless = compute_hash(data)

        # Then the keyed and keyless hashes differ (the key materially enters)
        assert keyed != keyless

    def test_compute_hash_is_idempotent_in_keyed_mode(self, monkeypatch):
        # Given a configured key
        _set_key(monkeypatch, "signing-key-A")
        data = {"event": "payment_failed", "amount": 42}

        # When hashing the same entry repeatedly, the digest is stable
        digests = {compute_hash(data) for _ in range(5)}

        assert len(digests) == 1

    @pytest.mark.parametrize(
        "key_value",
        [None, "param-signing-key"],
        ids=["keyless", "keyed"],
    )
    def test_compute_hash_deterministic_in_both_modes(self, monkeypatch, key_value):
        # Given either mode
        if key_value is None:
            _unset_key(monkeypatch)
        else:
            _set_key(monkeypatch, key_value)
        data = {"a": 1, "b": [2, 3], "c": "x"}

        # Then the same input yields the same hash within a mode
        assert compute_hash(data) == compute_hash(data)


class TestComputeHashKeyDistinguishes:
    """The signing key materially enters the hash (SC #1)."""

    def test_same_entry_under_two_keys_yields_different_hashes(self, monkeypatch):
        # Given the same entry hashed under two distinct keys
        data = {"event": "audit", "seq": 1}
        _set_key(monkeypatch, "key-one")
        hash_one = compute_hash(data)
        _set_key(monkeypatch, "key-two")
        hash_two = compute_hash(data)

        # Then the digests differ — the key is part of the MAC, not decoration
        assert hash_one != hash_two

    def test_same_key_same_entry_yields_same_hash(self, monkeypatch):
        # Given one key and one entry computed twice (cache reset between)
        data = {"event": "audit", "seq": 1}
        _set_key(monkeypatch, "key-one")
        hash_first = compute_hash(data)
        _set_key(monkeypatch, "key-one")
        hash_second = compute_hash(data)

        # Then re-resolving the same key reproduces the same digest
        assert hash_first == hash_second


class TestComputeHashContract:
    """Output-shape invariants that hold under HMAC-SHA256 (Contract)."""

    def test_keyed_hash_is_64_char_lowercase_hex(self, monkeypatch):
        _set_key(monkeypatch, "signing-key-A")

        result = compute_hash({"test": "data"})

        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_keyed_hash_is_key_order_independent(self, monkeypatch):
        # Canonical serialization sorts keys, so order must not change the MAC
        _set_key(monkeypatch, "signing-key-A")

        assert compute_hash({"a": 1, "b": 2, "c": 3}) == compute_hash(
            {"c": 3, "a": 1, "b": 2}
        )


# =============================================================================
# _audit_signing_key — key resolution
# =============================================================================


class TestAuditSigningKeyResolution:
    """``_audit_signing_key`` returns bytes / None by key presence (SC #5)."""

    def test_returns_key_bytes_when_set(self, monkeypatch):
        _set_key(monkeypatch, "signing-key-A")

        assert _audit_signing_key() == b"signing-key-A"

    def test_returns_none_when_unset(self, monkeypatch):
        _unset_key(monkeypatch)

        assert _audit_signing_key() is None

    def test_returns_none_when_empty_string(self, monkeypatch):
        # Empty string is treated as unset (falsy), matching the boot gate.
        _set_key(monkeypatch, "")

        assert _audit_signing_key() is None

    def test_reads_key_fresh_on_each_call(self, monkeypatch):
        # Given an initial key
        _set_key(monkeypatch, "key-before")
        assert _audit_signing_key() == b"key-before"

        # When the key rotates (no module-level caching of the key)
        _set_key(monkeypatch, "key-after")

        # Then the next call reflects the new key — never a stale cached value
        assert _audit_signing_key() == b"key-after"


# =============================================================================
# HashChainVerifier — forge rejection / no silent downgrade (keyed)
# =============================================================================


class TestVerifierForgeRejection:
    """A chain forged without the key fails under a keyed verifier (SC #2)."""

    def test_forge_without_key_fails(self, monkeypatch):
        # Given an attacker who rewrites content and recomputes the WHOLE chain
        # without the signing key — i.e. keyless SHA-256 over forged entries,
        # keeping sequence/previous_hash linkage internally consistent.
        _unset_key(monkeypatch)
        forger = HashChainManager()
        forged_chain = [
            forger.add_integrity({"event": "FORGED_TRANSFER", "amount": 1_000_000}),
            forger.add_integrity({"event": "e1"}),
            forger.add_integrity({"event": "e2"}),
        ]

        # The keyless-forged chain is internally consistent: a keyless verifier
        # (no key configured) cannot tell it apart from a legitimate chain.
        assert HashChainVerifier().verify_chain(forged_chain)[0] is True

        # When the real deployment has the signing key configured
        _set_key(monkeypatch, "real-signing-key")

        # Then the keyed verifier rejects the forged chain — it recomputes the
        # HMAC, which the keyless current_hash cannot match.
        is_valid, error = HashChainVerifier().verify_chain(forged_chain)
        assert is_valid is False
        assert "hash mismatch" in error.lower()

    def test_keyed_chain_with_forged_content_fails(self, monkeypatch):
        # Given a legitimately keyed chain
        _set_key(monkeypatch, "real-signing-key")
        manager = HashChainManager()
        entries = [
            manager.add_integrity({"event": "e0"}),
            manager.add_integrity({"event": "e1"}),
            manager.add_integrity({"event": "e2"}),
        ]
        assert HashChainVerifier().verify_chain(entries)[0] is True

        # When an attacker edits content but cannot re-sign (key absent to them)
        entries[1]["event"] = "TAMPERED"

        # Then verification fails at the tampered entry
        is_valid, error = HashChainVerifier().verify_chain(entries)
        assert is_valid is False
        assert "hash mismatch" in error.lower()


class TestVerifierNoDowngrade:
    """Key-presence verify semantics — no silent keyless downgrade (SC #4)."""

    def test_keyless_chain_verifies_when_no_key_configured(self, monkeypatch):
        # Given a keyless chain verified with no key — the pre-632 behavior
        _unset_key(monkeypatch)
        manager = HashChainManager()
        entries = [manager.add_integrity({"event": f"e{i}"}) for i in range(4)]

        # Then it still verifies (no regression for keyless deployments)
        is_valid, error = HashChainVerifier().verify_chain(entries)
        assert is_valid is True
        assert error is None

    def test_keyed_chain_verifies_under_matching_key(self, monkeypatch):
        # Given a keyed chain verified with the same key
        _set_key(monkeypatch, "matching-key")
        manager = HashChainManager()
        entries = [manager.add_integrity({"event": f"e{i}"}) for i in range(4)]

        # Then it verifies cleanly
        is_valid, error = HashChainVerifier().verify_chain(entries)
        assert is_valid is True
        assert error is None

    def test_unsigned_keyless_chain_does_not_pass_under_keyed_verifier(
        self, monkeypatch
    ):
        # Given an unsigned (keyless) chain, content untouched
        _unset_key(monkeypatch)
        manager = HashChainManager()
        entries = [manager.add_integrity({"event": f"e{i}"}) for i in range(4)]

        # When the verifier runs with a key configured (keyed deployment)
        _set_key(monkeypatch, "deployment-key")

        # Then the keyless chain is rejected — the verifier never downgrades to
        # keyless when a key is present.
        is_valid, error = HashChainVerifier().verify_chain(entries)
        assert is_valid is False
        assert "hash mismatch" in error.lower()


class TestVerifierConstantTimeCompare:
    """Verify-side current_hash comparison is timing-safe (D8, SC #6)."""

    def test_verify_chain_compares_via_compare_digest(self, monkeypatch):
        from unittest.mock import patch

        _set_key(monkeypatch, "signing-key-A")
        manager = HashChainManager()
        entries = [manager.add_integrity({"event": f"e{i}"}) for i in range(3)]
        verifier = HashChainVerifier()

        with patch(
            "baldur.audit.integrity.verifier.hmac.compare_digest",
            wraps=hmac.compare_digest,
        ) as spy:
            is_valid, _ = verifier.verify_chain(entries)

        assert is_valid is True
        # One MAC compare per entry — the equality test never used a plain !=.
        assert spy.call_count == len(entries)

    def test_find_tampering_compares_via_compare_digest(self, monkeypatch):
        from unittest.mock import patch

        _set_key(monkeypatch, "signing-key-A")
        manager = HashChainManager()
        entries = [manager.add_integrity({"event": f"e{i}"}) for i in range(3)]
        verifier = HashChainVerifier()

        with patch(
            "baldur.audit.integrity.verifier.hmac.compare_digest",
            wraps=hmac.compare_digest,
        ) as spy:
            issues = verifier.find_tampering(entries)

        assert issues == []
        assert spy.call_count == len(entries)


# =============================================================================
# Fanned-out re-implementations delegate to the single source (D2, SC #3)
# =============================================================================


class TestChainHashDelegation:
    """``sampling`` / ``fallback`` route through ``models.compute_hash``."""

    def test_sampling_compute_hash_equals_models_compute_hash_keyed(self, monkeypatch):
        from baldur.audit.performance import SamplingVerifier

        _set_key(monkeypatch, "signing-key-A")
        data = {
            "event": "x",
            "integrity": {"sequence": 1, "previous_hash": "GENESIS", "timestamp": "t"},
        }

        assert SamplingVerifier()._compute_hash(data) == compute_hash(data)

    def test_sampling_compute_hash_equals_models_compute_hash_keyless(
        self, monkeypatch
    ):
        from baldur.audit.performance import SamplingVerifier

        _unset_key(monkeypatch)
        data = {
            "event": "x",
            "integrity": {"sequence": 1, "previous_hash": "GENESIS", "timestamp": "t"},
        }

        assert SamplingVerifier()._compute_hash(data) == compute_hash(data)

    def test_sampling_delegation_reflects_key_presence(self, monkeypatch):
        # Proves the delegate is the live keyed source, not a stale keyless copy.
        from baldur.audit.performance import SamplingVerifier

        data = {"event": "x", "integrity": {"sequence": 1, "previous_hash": "GENESIS"}}
        _set_key(monkeypatch, "signing-key-A")
        keyed = SamplingVerifier()._compute_hash(data)
        _unset_key(monkeypatch)
        keyless = SamplingVerifier()._compute_hash(data)

        assert keyed != keyless

    def test_fallback_compute_hash_equals_models_compute_hash_of_stripped_entry(
        self, monkeypatch
    ):
        from baldur.audit.graceful_degradation.fallback import HashChainFallbackChain

        _set_key(monkeypatch, "signing-key-A")
        # fallback._compute_hash strips current_hash before hashing.
        entry = {
            "event": "x",
            "integrity": {
                "sequence": 1,
                "previous_hash": "GENESIS",
                "timestamp": "t",
                "current_hash": "stale-hash-to-be-stripped",
            },
        }
        stripped = {
            "event": "x",
            "integrity": {"sequence": 1, "previous_hash": "GENESIS", "timestamp": "t"},
        }

        assert HashChainFallbackChain()._compute_hash(entry) == compute_hash(stripped)

    def test_fallback_delegation_reflects_key_presence(self, monkeypatch):
        from baldur.audit.graceful_degradation.fallback import HashChainFallbackChain

        entry = {
            "event": "x",
            "integrity": {"sequence": 1, "previous_hash": "GENESIS", "timestamp": "t"},
        }
        _set_key(monkeypatch, "signing-key-A")
        keyed = HashChainFallbackChain()._compute_hash(entry)
        _unset_key(monkeypatch)
        keyless = HashChainFallbackChain()._compute_hash(entry)

        assert keyed != keyless
