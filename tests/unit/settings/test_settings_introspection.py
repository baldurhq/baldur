"""Unit tests for ``baldur.settings.introspection`` (576 D1/D3).

Production home for the settings-class reflection helpers that the startup
unknown-env-var scan and the G15/G30 documentation gates share. The public
surface is a set of pure functions over a passed prefix index plus a
module-level direct-read registry:

- ``resolve_env_var(var, index)`` — Pydantic field-level resolution (longest
  prefix, nested-``__``, case-insensitive).
- ``is_known_env_var(var, index)`` — field-level OR direct-read registry.
- ``build_prefix_index()`` — force-load every settings submodule, then reflect.
- ``register_direct_read_env_vars()`` / ``_known_direct_read_vars()`` — the
  Channel-2 pro/plugin/computed-read seam.
- ``known_env_var_names(index)`` — union of every known name for difflib hints.
- ``KNOWN_DIRECT_READ_ENV_VARS`` — Channel-1 committed constant.

Verification techniques (per UNIT_TEST_GUIDELINES §8):

- §8.1 Boundary analysis (bare-prefix / triple-delimiter / longest-prefix edges).
- §8.6 Equivalence partitioning (resolvable / typo / unregistered / direct-read).
- §8.10 Idempotency (registry additive-union, force-load repeat).

These are Behavior tests (compute expectations from the source algorithm) except
``TestKnownDirectReadConstantContract`` which pins design-required members.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from baldur.settings import introspection
from baldur.settings.introspection import (
    KNOWN_DIRECT_READ_ENV_VARS,
    _known_direct_read_vars,
    build_prefix_index,
    is_known_env_var,
    known_env_var_names,
    register_direct_read_env_vars,
    resolve_env_var,
)

# =============================================================================
# Fixtures + synthetic index
# =============================================================================

# A controlled prefix index so the resolver algorithm (longest-prefix, nested
# ``__``, case-folding, degenerate remainders) can be exercised independent of
# the live settings tree. ``BALDUR_DLQ_OUTBOX_`` deliberately shadows the
# shorter ``BALDUR_DLQ_`` so longest-prefix selection is observable.
SYNTHETIC_INDEX: dict[str, set[str]] = {
    "BALDUR_DLQ_": {"max_size", "enabled"},
    "BALDUR_DLQ_OUTBOX_": {"enabled", "batch_size"},
    "BALDUR_NESTED_": {"sub"},
}


@pytest.fixture
def isolated_extension_registry() -> Iterator[None]:
    """Snapshot/restore the module-level Channel-2 extension set.

    ``register_direct_read_env_vars`` mutates ``_EXTENSION_DIRECT_READ_VARS``
    (populated at import by ``degraded_mode_handler``). Tests that register
    must not leak names into sibling tests, so we restore the snapshot.
    """
    saved = set(introspection._EXTENSION_DIRECT_READ_VARS)
    try:
        yield
    finally:
        introspection._EXTENSION_DIRECT_READ_VARS.clear()
        introspection._EXTENSION_DIRECT_READ_VARS.update(saved)


# =============================================================================
# resolve_env_var — Behavior
# =============================================================================


class TestResolveEnvVarBehavior:
    """Pydantic field-level resolution over a passed index."""

    @pytest.mark.parametrize(
        ("var", "expected"),
        [
            # Resolvable field present under its prefix.
            ("BALDUR_DLQ_MAX_SIZE", True),
            # In-namespace typo: prefix matches, field does not.
            ("BALDUR_DLQ_MAX_SIE", False),
            # Unregistered prefix → no match.
            ("BALDUR_NOPE_FOO", False),
            # Mixed-case var: upper-cased before matching (case_sensitive=False).
            ("BALDUR_Dlq_Max_Size", True),
            ("baldur_dlq_max_size", True),
        ],
    )
    def test_resolve_partitions_known_vs_unknown(self, var: str, expected: bool):
        assert resolve_env_var(var, SYNTHETIC_INDEX) is expected

    def test_resolve_picks_longest_prefix_for_shadowed_field(self):
        # ``outbox_enabled`` is NOT a field of ``BALDUR_DLQ_``; ``enabled`` IS a
        # field of the longer ``BALDUR_DLQ_OUTBOX_``. A True result proves the
        # resolver selected the longest matching prefix.
        assert resolve_env_var("BALDUR_DLQ_OUTBOX_ENABLED", SYNTHETIC_INDEX) is True

    def test_resolve_does_not_fall_back_to_shorter_prefix(self):
        # ``bogus`` is not a field of the longest prefix ``BALDUR_DLQ_OUTBOX_``.
        # The resolver must NOT then retry against the shorter ``BALDUR_DLQ_`` —
        # longest-prefix is authoritative, so this is unknown.
        assert resolve_env_var("BALDUR_DLQ_OUTBOX_BOGUS", SYNTHETIC_INDEX) is False

    def test_resolve_nested_delimiter_checks_first_segment(self):
        # With env_nested_delimiter="__" a var resolves on its first ``__``
        # segment (the sub-config attribute name): ``sub`` is a field.
        assert resolve_env_var("BALDUR_NESTED_SUB__CHILD", SYNTHETIC_INDEX) is True

    def test_resolve_nested_delimiter_unknown_first_segment_is_false(self):
        assert resolve_env_var("BALDUR_NESTED_BOGUS__CHILD", SYNTHETIC_INDEX) is False

    def test_resolve_bare_prefix_no_remainder_is_false_without_raising(self):
        # Boundary: var == prefix → empty remainder must short-circuit to False,
        # never IndexError on the ``[0]`` field split.
        assert resolve_env_var("BALDUR_DLQ_", SYNTHETIC_INDEX) is False

    def test_resolve_triple_delimiter_empty_segment_is_false_without_raising(self):
        # Boundary: ``BALDUR_DLQ___MAX_SIZE`` → remainder ``__max_size`` whose
        # first ``__`` segment is empty → unknown, no crash.
        assert resolve_env_var("BALDUR_DLQ___MAX_SIZE", SYNTHETIC_INDEX) is False

    def test_resolve_empty_index_always_false(self):
        assert resolve_env_var("BALDUR_DLQ_MAX_SIZE", {}) is False


# =============================================================================
# is_known_env_var — Behavior
# =============================================================================


class TestIsKnownEnvVarBehavior:
    """Known = Pydantic field-level OR direct-read registry membership."""

    @pytest.fixture(autouse=True)
    def _ensure_channel2_registered(self):
        # Importing the handler triggers its import-time Channel-2 registration
        # of the degraded-mode knobs (``f"BALDUR_{k}"`` over ``_defaults``).
        import baldur.core.degraded_mode_handler  # noqa: F401

    def test_pydantic_resolvable_var_is_known(self):
        index = build_prefix_index()
        assert is_known_env_var("BALDUR_DLQ_MAX_SIZE", index) is True

    def test_direct_read_registry_var_is_known(self):
        # ``BALDUR_LOG_LEVEL`` has no backing Pydantic field — it is known only
        # via the Channel-1 direct-read constant.
        index = build_prefix_index()
        assert is_known_env_var("BALDUR_LOG_LEVEL", index) is True

    def test_unmatched_var_is_unknown(self):
        index = build_prefix_index()
        assert is_known_env_var("BALDUR_TOTALLY_UNKNOWN_XYZ", index) is False

    def test_mixed_case_resolvable_var_is_known(self):
        # Pydantic arm upper-cases before matching.
        index = build_prefix_index()
        assert is_known_env_var("BALDUR_Dlq_Max_Size", index) is True

    def test_mixed_case_direct_read_var_is_known(self):
        # Direct-read arm upper-cases before membership test.
        index = build_prefix_index()
        assert is_known_env_var("BALDUR_Log_Level", index) is True

    def test_channel2_degraded_knob_is_known_but_not_in_channel1(self):
        # ``BALDUR_DEFAULT_TIMEOUT_MS`` does not resolve field-level and is NOT
        # in the Channel-1 literal constant — it is known ONLY because
        # ``degraded_mode_handler`` registered it via Channel-2. This pins that
        # the seam, not the constant, covers the computed degraded-mode knobs.
        index = build_prefix_index()
        assert "BALDUR_DEFAULT_TIMEOUT_MS" not in KNOWN_DIRECT_READ_ENV_VARS
        assert is_known_env_var("BALDUR_DEFAULT_TIMEOUT_MS", index) is True


# =============================================================================
# build_prefix_index — Behavior (force-load contract)
# =============================================================================


class TestBuildPrefixIndexBehavior:
    """Force-load every settings submodule before reflecting."""

    def test_index_is_dict_of_prefix_to_field_sets(self):
        index = build_prefix_index()
        assert isinstance(index, dict)
        assert index, "expected a non-empty settings surface"
        for prefix, fields in index.items():
            assert isinstance(prefix, str)
            assert isinstance(fields, set)
            assert all(isinstance(f, str) for f in fields)

    def test_force_load_registers_lazily_loaded_dlq_outbox_prefix(self):
        # ``dlq_outbox.py`` is NOT eager-loaded by ``import baldur.settings``;
        # only the force-load inside build_prefix_index() pulls it in. Its
        # presence pins the force-load contract (mirrors G30's
        # ``test_force_load_registers_nested_dlq_outbox_prefix``).
        index = build_prefix_index()
        assert "BALDUR_DLQ_OUTBOX_" in index
        assert "enabled" in index["BALDUR_DLQ_OUTBOX_"]

    def test_lazily_loaded_module_var_resolves_after_force_load(self):
        # SC: resolve_env_var("BALDUR_DLQ_OUTBOX_ENABLED", build_prefix_index())
        # returns True — proving build_prefix_index force-loads before reflect.
        assert resolve_env_var("BALDUR_DLQ_OUTBOX_ENABLED", build_prefix_index())

    def test_force_load_is_idempotent_across_repeat_builds(self):
        # force_load_settings_modules() is idempotent — a second build yields the
        # same prefix surface (module import cache makes re-import a no-op).
        first = build_prefix_index()
        second = build_prefix_index()
        assert set(first) == set(second)


# =============================================================================
# register_direct_read_env_vars / _known_direct_read_vars — Behavior
# =============================================================================


class TestDirectReadRegistryBehavior:
    """Channel-2 seam: additive, idempotent, case-normalizing union."""

    def test_registration_makes_var_known_in_union(
        self, isolated_extension_registry: None
    ):
        register_direct_read_env_vars("BALDUR_TEST_PLUGIN_FOO")
        assert "BALDUR_TEST_PLUGIN_FOO" in _known_direct_read_vars()

    def test_registration_is_idempotent(self, isolated_extension_registry: None):
        register_direct_read_env_vars("BALDUR_TEST_PLUGIN_FOO")
        before = len(_known_direct_read_vars())
        register_direct_read_env_vars("BALDUR_TEST_PLUGIN_FOO")
        assert len(_known_direct_read_vars()) == before

    def test_registration_upper_cases_names(self, isolated_extension_registry: None):
        # Names registered in mixed/lower case are stored upper-cased so the
        # membership test matches resolve_env_var's case-folding.
        register_direct_read_env_vars("baldur_test_plugin_bar")
        union = _known_direct_read_vars()
        assert "BALDUR_TEST_PLUGIN_BAR" in union
        assert "baldur_test_plugin_bar" not in union

    def test_union_always_superset_of_committed_constant(
        self, isolated_extension_registry: None
    ):
        register_direct_read_env_vars("BALDUR_TEST_PLUGIN_BAZ")
        union = _known_direct_read_vars()
        assert KNOWN_DIRECT_READ_ENV_VARS <= union
        assert isinstance(union, frozenset)

    def test_multiple_names_registered_additively(
        self, isolated_extension_registry: None
    ):
        register_direct_read_env_vars("BALDUR_TEST_A", "BALDUR_TEST_B")
        union = _known_direct_read_vars()
        assert {"BALDUR_TEST_A", "BALDUR_TEST_B"} <= union


# =============================================================================
# known_env_var_names — Behavior (union completeness)
# =============================================================================


class TestKnownEnvVarNamesBehavior:
    """Every Pydantic PREFIX+FIELD name unioned with direct-read names."""

    def test_includes_pydantic_prefix_plus_field_names(self):
        names = known_env_var_names(SYNTHETIC_INDEX)
        # prefix + field.upper() — the canonical env-var spelling.
        assert "BALDUR_DLQ_MAX_SIZE" in names
        assert "BALDUR_NESTED_SUB" in names

    def test_includes_direct_read_registry_names(self):
        names = known_env_var_names(SYNTHETIC_INDEX)
        assert "BALDUR_LOG_LEVEL" in names

    def test_includes_channel2_registered_names(
        self, isolated_extension_registry: None
    ):
        register_direct_read_env_vars("BALDUR_TEST_PLUGIN_HINT")
        names = known_env_var_names(SYNTHETIC_INDEX)
        assert "BALDUR_TEST_PLUGIN_HINT" in names


# =============================================================================
# KNOWN_DIRECT_READ_ENV_VARS — Contract
# =============================================================================


class TestKnownDirectReadConstantContract:
    """Design-required members of the Channel-1 committed constant."""

    def test_is_frozenset(self):
        assert isinstance(KNOWN_DIRECT_READ_ENV_VARS, frozenset)

    @pytest.mark.parametrize(
        "var",
        [
            # The scan reads these suppress flags itself — they MUST self-register
            # so the scan never flags its own helper vars.
            "BALDUR_SUPPRESS_UNKNOWN_ENV_WARNING",
            "BALDUR_SUPPRESS_TIER_WARNING",
            # Bootstrap-phase identity vars (read before settings are wired).
            "BALDUR_TEST_MODE",
            "BALDUR_FAIL_FAST",
            # Logging direct-read.
            "BALDUR_LOG_LEVEL",
        ],
    )
    def test_required_direct_read_vars_present(self, var: str):
        assert var in KNOWN_DIRECT_READ_ENV_VARS
