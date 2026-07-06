"""Conformance tests for ``StateBackend.compare_and_set`` (666 D1).

The version-aware optimistic-concurrency primitive shared by every state
backend. A parametrized conformance suite asserts the in-process backends
(Memory + File) honor the identical contract; the Redis backing — whose
``WATCH``/``MULTI``/``EXEC`` transactional atomicity these lock-based backends
cannot represent — is exercised by the ``requires_redis`` integration test
(``tests/integration/redis/test_state_backend_cas_redis.py``).

Contract (D1):
  - set-on-match: the stored version equals ``expected_version`` → store
    ``new_value`` (already stamped by the caller) and return ``True``;
  - false-on-mismatch: a version mismatch returns ``False`` and leaves the
    stored value untouched (a concurrent writer won the race);
  - an absent key, or a value lacking the version field, is treated as
    version ``0``;
  - a backend error propagates (it is NOT swallowed into ``False`` / a blind
    set — the write fails closed exactly as :meth:`set` does).

Techniques (§8): boundary analysis (match / mismatch / absent-key=v0),
state transition (first writer wins, stale second writer is rejected),
exception propagation (injected backend error).
"""

from __future__ import annotations

import pytest

from baldur.core.state_backend import FileStateBackend, MemoryStateBackend

OCC = "__occ_version__"


@pytest.fixture(params=["memory", "file"])
def cas_backend(request, tmp_path):
    """A constructed StateBackend per param — the conformance SUT.

    File needs a directory (provided by ``tmp_path``); both back the same
    abstract CAS contract under test.
    """
    if request.param == "memory":
        return MemoryStateBackend()
    return FileStateBackend(tmp_path / "state")


def _stamped(value: dict, version: int) -> dict:
    """Mimic the production caller: stamp ``new_value`` with ``expected + 1``.

    ``compare_and_set`` stores ``new_value`` verbatim; the version bump is the
    caller's responsibility, so the conformance test stamps it explicitly.
    """
    blob = dict(value)
    blob[OCC] = version
    return blob


# =============================================================================
# compare_and_set — Contract conformance (Memory + File parametrized)
# =============================================================================


class TestCompareAndSetConformance:
    """All in-process backends honor the D1 CAS contract identically.

    The boundary cases (set-on-match / false-on-mismatch / absent-key=v0 /
    lacking-field=v0) are table-driven and run for every backend via the
    parametrized ``cas_backend`` fixture (backend × case cross product). This is
    the function the ``compare_and_set`` ``# verified-by:`` link points to.
    """

    @pytest.mark.parametrize(
        ("seed", "expected_version", "new_fields", "want_ok", "want_stored"),
        [
            # absent key is version 0 → a v0 CAS sets
            (None, 0, {"a": 1}, True, {"a": 1, OCC: 1}),
            # absent key is version 0 → a non-zero expected mismatches
            (None, 3, {"a": 1}, False, None),
            # matching stored version → set + persist the caller-stamped bump
            ({"a": 1, OCC: 2}, 2, {"a": 9}, True, {"a": 9, OCC: 3}),
            # version mismatch → False, stored value untouched
            ({"a": 1, OCC: 5}, 3, {"a": 9}, False, {"a": 1, OCC: 5}),
            # a legacy blob lacking the version field is version 0
            ({"a": 1}, 0, {"a": 2}, True, {"a": 2, OCC: 1}),
        ],
    )
    def test_compare_and_set_conformance(
        self, cas_backend, seed, expected_version, new_fields, want_ok, want_stored
    ):
        # Given an optional seeded blob
        if seed is not None:
            cas_backend.set("cfg", seed)

        # When a writer CASes against expected_version (stamping expected + 1)
        ok = cas_backend.compare_and_set(
            "cfg",
            expected_version=expected_version,
            new_value=_stamped(new_fields, expected_version + 1),
        )

        # Then the return + the stored value match the contract
        assert ok is want_ok
        assert cas_backend.get("cfg") == want_stored

    def test_custom_version_field_is_honored(self, cas_backend):
        cas_backend.set("cfg", {"a": 1, "ver": 4})

        ok = cas_backend.compare_and_set(
            "cfg",
            expected_version=4,
            new_value={"a": 2, "ver": 5},
            version_field="ver",
        )

        assert ok is True
        assert cas_backend.get("cfg")["ver"] == 5

    def test_first_writer_wins_stale_second_writer_rejected(self, cas_backend):
        """State transition: once a writer wins (0→1), a second writer still
        holding the stale expected version 0 loses — the lost-update guard."""
        assert (
            cas_backend.compare_and_set(
                "cfg", expected_version=0, new_value=_stamped({"v": "A"}, 1)
            )
            is True
        )

        # Second writer's view is stale (still expects 0) → rejected.
        assert (
            cas_backend.compare_and_set(
                "cfg", expected_version=0, new_value=_stamped({"v": "B"}, 1)
            )
            is False
        )
        assert cas_backend.get("cfg")["v"] == "A"  # first write preserved


# =============================================================================
# compare_and_set — backend error propagates (fails closed, not False)
# =============================================================================


class TestCompareAndSetErrorPropagation:
    """A backend error propagates rather than degrading to ``False`` / a blind
    set — the write fails closed exactly as :meth:`set` does, never silently
    falling back to an overwrite that would reintroduce the lost-update clobber
    (D1 contract; Risk: CAS backend error semantics)."""

    def test_memory_backend_read_error_propagates(self, monkeypatch):
        backend = MemoryStateBackend()

        class _Boom:
            def get(self, *args, **kwargs):
                raise RuntimeError("store unavailable")

        # Replace the store so the in-lock read raises mid-CAS.
        monkeypatch.setattr(backend, "_store", _Boom())

        with pytest.raises(RuntimeError):
            backend.compare_and_set("cfg", expected_version=0, new_value={OCC: 1})

    def test_file_backend_read_error_propagates(self, tmp_path, monkeypatch):
        backend = FileStateBackend(tmp_path / "state")
        backend.set("cfg", {"a": 1, OCC: 0})

        def _boom(_key):
            raise OSError("disk read failure")

        # The File CAS reads via _read_raw (errors surface, unlike get()).
        monkeypatch.setattr(backend, "_read_raw", _boom)

        with pytest.raises(OSError):
            backend.compare_and_set("cfg", expected_version=0, new_value={OCC: 1})
