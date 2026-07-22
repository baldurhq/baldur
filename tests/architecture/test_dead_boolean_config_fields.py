"""722 D1/D4/D5 — G72 dead-boolean-config-field gate (OSS half).

A boolean config field that no production path ever reads is a claim with no
backing: the operator sets it, nothing consumes it, and the constraint it
advertises is silently absent. That is worst for *restrictive* flags — the
default happens to match shipped behavior, so nothing looks wrong until someone
relies on the restriction.

G32 covers only the enable-shaped names on the settings registry. G72 widens the
population to EVERY boolean config field — config dataclasses, serializable
configs, and settings classes outside the settings package all included — and
narrows the verdict to zero production read (echo counts as a read here: on a
name-agnostic population a boolean echoed into a report is weak evidence of
deadness, unlike an *enable* claim).

The population is AST-discovered from the OSS source root; reads are counted
across every installed tier, because a field read only by a higher tier is live,
not dead. When a tier is absent the live analysis skips — deadness is unknowable
without the full consumer set — and the run in the repo that has every tier is
the authoritative one. The synthetic fixture suites below run regardless, so the
gate can never pass vacuously.

Baseline: ``dead_boolean_config_fields:`` in ``baseline.yaml``, seeded with the
known dead set. Unlike the shared regression-only mechanism, this rule ratchets
in BOTH directions — a baselined field that gets wired or deleted must leave the
baseline in the same commit — and bans whole-file waivers, which would make the
stale check undecidable.

Rule registry:
``ARCHITECTURE.md#g72-dead-boolean-config-fields``
"""

from __future__ import annotations

import pytest

from tests.architecture._helpers import (
    OBSERVED_STATE_SUFFIXES,
    collect_bool_field_reads,
    consumer_src_roots,
    discover_bool_config_fields,
    is_excluded_bool_config_field,
    is_observed_state_class,
    load_baseline,
    oss_src_root,
    zero_read_bool_fields,
)
from tests.architecture.conftest import PROJECT_ROOT, collect_violations

_RULE_KEY = "dead_boolean_config_fields"
_RULE_ANCHOR = "#g72-dead-boolean-config-fields"

# Source root used by the live consumer-set scan. On a checkout that carries
# only the OSS tier the cross-tier consumer set is incomplete and field deadness
# is unknowable — the live analysis then skips.
_SRC = PROJECT_ROOT / "src"
_GATED_TIERS = ("baldur_pro", "baldur_dormant")

_TIER_ABSENT_SKIP = (
    "G72's consumer-set scan needs every tier present; a tier is absent "
    "(OSS-only checkout) so cross-tier field deadness is unknowable. The "
    "full-tier run is the authoritative G72 gate."
)

_FIX_HINT = (
    "Wire a consumer, or delete the field and its env var. If the owning class "
    "is serialized wholesale (`SerializableMixin.to_dict` / pydantic "
    "`model_dump` / `dataclasses.asdict`), check the serialization reach first "
    "— a field consumed only that way carries no name token any static scan can "
    "see; baseline it with a `serialized-live` reason instead of deleting it."
)


def _live_zero_read_symbols() -> set[tuple[str, str]]:
    """The live ``(file, symbol)`` set of zero-read OSS boolean config fields."""
    fields = discover_bool_config_fields((oss_src_root(),))
    reads = collect_bool_field_reads(fields, consumer_src_roots())
    return {(f.file, f.symbol) for f in zero_read_bool_fields(fields, reads)}


def stale_baseline_entries(
    baseline: dict[tuple[str, str | None], int],
    live: set[tuple[str, str]],
) -> list[str]:
    """Baselined entries that no longer match a live zero-read field (722 D5).

    The shared mechanism silently accepts these; for this rule they are a
    failure, so a wired-or-deleted field cannot leave its entry behind forever.
    """
    return sorted(
        f"{file}::{symbol}"
        for (file, symbol) in baseline
        if symbol is not None and (file, symbol) not in live
    )


def whole_file_waiver_entries(
    baseline: dict[tuple[str, str | None], int],
) -> list[str]:
    """Baselined entries with no ``symbol:`` — banned for this rule (722 D5).

    A whole-file waiver suppresses per-field matching, which would make the
    stale-entry check above undecidable for every field in that file.
    """
    return sorted(file for (file, symbol) in baseline if symbol is None)


class TestDeadBooleanConfigFields:
    """722 D4 — no boolean config field is read nowhere in production."""

    def test_no_dead_boolean_config_field(self):
        """Every OSS boolean config field has at least one production read.

        Burn-down: the known dead set is baselined under
        ``dead_boolean_config_fields:``, each entry reviewed at seeding to
        confirm it is not a live field. A NEW dead field regresses on its first
        occurrence.
        """
        if any(not (_SRC / tier).is_dir() for tier in _GATED_TIERS):
            pytest.skip(_TIER_ABSENT_SKIP)

        fields = discover_bool_config_fields((oss_src_root(),))
        reads = collect_bool_field_reads(fields, consumer_src_roots())
        raw = [
            (
                PROJECT_ROOT / field.file,
                field.lineno,
                field.symbol,
                f"boolean config field with no production read "
                f"(default={field.default}) — {_FIX_HINT}",
            )
            for field in zero_read_bool_fields(fields, reads)
        ]
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"{len(violations)} boolean config field(s) with zero production "
            f"read — an advertised knob nothing consumes (claim-wiring bug "
            f"class). {_FIX_HINT} If the deadness is tracked rather than fixed, "
            f"baseline under `{_RULE_KEY}` with a reason and a ticket:\n"
            + "\n".join(violations)
        )

    def test_baseline_holds_no_stale_entry(self):
        """722 D5 — a wired or deleted field must leave the baseline (ratchet)."""
        if any(not (_SRC / tier).is_dir() for tier in _GATED_TIERS):
            pytest.skip(_TIER_ABSENT_SKIP)

        stale = stale_baseline_entries(
            load_baseline(_RULE_KEY), _live_zero_read_symbols()
        )
        assert not stale, (
            f"G72: {len(stale)} baselined field(s) no longer read as dead — they "
            f"were wired or deleted. Drop them from `{_RULE_KEY}` in the same "
            f"commit so the baseline cannot outlive the defect it documents:\n"
            + "\n".join(f"  {entry}" for entry in stale)
        )

    def test_baseline_carries_no_whole_file_waiver(self):
        """722 D5 — a `symbol:`-less entry is banned under this rule key."""
        waivers = whole_file_waiver_entries(load_baseline(_RULE_KEY))
        assert not waivers, (
            f"G72: {len(waivers)} whole-file waiver(s) under `{_RULE_KEY}`. This "
            "rule keys every entry on `symbol: Class.field` — a file-level entry "
            "suppresses per-field matching and makes the stale-entry ratchet "
            "undecidable:\n" + "\n".join(f"  {file}" for file in waivers)
        )


def _write(tmp_path, source: str):
    """Write a synthetic module into a fixture tree and return its root."""
    root = tmp_path / "src" / "baldur"
    root.mkdir(parents=True, exist_ok=True)
    (root / "fixture_module.py").write_text(source, encoding="utf-8")
    return root


def _discover_names(tmp_path, source: str) -> set[str]:
    """The ``Class.field`` symbols discovered in a synthetic source."""
    return {f.symbol for f in discover_bool_config_fields((_write(tmp_path, source),))}


class TestDeadBooleanConfigFieldsDiscoveryFixtures:
    """722 D2 — every in-shape declaration is discovered, every out-of-shape one is not."""

    @pytest.mark.parametrize(
        ("shape", "declaration"),
        [
            ("literal", "    flag: bool = True"),
            ("literal_false", "    flag: bool = False"),
            ("field_default_kw", "    flag: bool = Field(default=True)"),
            ("field_positional", "    flag: bool = Field(True)"),
            ("dataclass_field_kw", "    flag: bool = field(default=False)"),
            ("annotated", "    flag: Annotated[bool, Field(default=True)] = True"),
            ("final", "    flag: Final[bool] = True"),
        ],
    )
    def test_in_shape_declaration_is_discovered(self, tmp_path, shape, declaration):
        source = f"class SomeConfig:\n{declaration}\n"
        assert _discover_names(tmp_path, source) == {"SomeConfig.flag"}

    @pytest.mark.parametrize(
        ("shape", "declaration"),
        [
            ("optional_union", "    flag: bool | None = True"),
            ("optional_typing", "    flag: Optional[bool] = True"),
            ("classvar", "    flag: ClassVar[bool] = True"),
            ("string_annotation", '    flag: "bool" = True'),
            ("unannotated", "    flag = True"),
            ("non_bool_annotation", "    flag: int = 1"),
            ("no_default", "    flag: bool"),
            ("default_factory", "    flag: bool = field(default_factory=bool)"),
            ("non_constant_default", "    flag: bool = _compute()"),
        ],
    )
    def test_out_of_shape_declaration_is_not_discovered(
        self, tmp_path, shape, declaration
    ):
        source = f"class SomeConfig:\n{declaration}\n"
        assert _discover_names(tmp_path, source) == set()

    def test_nested_class_field_is_discovered(self, tmp_path):
        source = "class Outer:\n    class Inner:\n        flag: bool = True\n"
        assert _discover_names(tmp_path, source) == {"Inner.flag"}

    def test_module_level_annotation_is_not_a_config_field(self, tmp_path):
        """Only class-body declarations are config fields."""
        assert _discover_names(tmp_path, "flag: bool = True\n") == set()

    def test_discovered_field_carries_default_and_line(self, tmp_path):
        root = _write(tmp_path, "class SomeConfig:\n\n    flag: bool = False\n")
        (discovered,) = discover_bool_config_fields((root,))
        assert (discovered.cls, discovered.field, discovered.default) == (
            "SomeConfig",
            "flag",
            False,
        )
        assert discovered.lineno == 3


class TestDeadBooleanConfigFieldsExclusions:
    """722 D3 — the two population exclusions, each with a live counter-case."""

    def test_bare_enabled_is_excluded(self, tmp_path):
        source = (
            "class SomeConfig:\n    enabled: bool = True\n    strict: bool = True\n"
        )
        assert _discover_names(tmp_path, source) == {"SomeConfig.strict"}
        assert is_excluded_bool_config_field("SomeConfig", "enabled")
        assert not is_excluded_bool_config_field("SomeConfig", "strict")

    @pytest.mark.parametrize("suffix", OBSERVED_STATE_SUFFIXES)
    def test_observed_state_suffix_is_excluded(self, tmp_path, suffix):
        source = f"class Some{suffix}:\n    flag: bool = True\n"
        assert _discover_names(tmp_path, source) == set()
        assert is_observed_state_class(f"Some{suffix}")
        assert is_excluded_bool_config_field(f"Some{suffix}", "flag")

    def test_config_class_name_is_not_excluded(self):
        assert not is_observed_state_class("RetryPolicyConfig")
        assert not is_excluded_bool_config_field("RetryPolicyConfig", "strict")

    def test_suffix_set_is_not_a_prefix_match(self):
        """`StatefulPolicy` starts with a suffix token but does not end with one."""
        assert not is_observed_state_class("StatefulPolicy")


class TestDeadBooleanConfigFieldsVerdict:
    """722 D4 — zero read is dead; any single read shape clears the field."""

    _SOURCES = {
        "static_attr": "def f(cfg):\n    return cfg.strict\n",
        "gate": "def f(cfg):\n    if cfg.strict:\n        return 1\n",
        "getattr_string": 'def f(cfg):\n    return getattr(cfg, "strict", True)\n',
        "subscript": 'def f(cfg):\n    return cfg["strict"]\n',
        "dict_get": 'def f(cfg):\n    return cfg.get("strict", True)\n',
        "echo": "def f(cfg, d, k):\n    d[k] = cfg.strict\n",
    }

    def _fields(self, tmp_path):
        root = _write(tmp_path, "class SomeConfig:\n    strict: bool = True\n")
        return discover_bool_config_fields((root,)), root

    def test_no_read_anywhere_is_dead(self, tmp_path):
        fields, root = self._fields(tmp_path)
        (root / "consumer.py").write_text(
            "def f(cfg):\n    return cfg.other\n", encoding="utf-8"
        )
        reads = collect_bool_field_reads(fields, (root,))
        assert [f.symbol for f in zero_read_bool_fields(fields, reads)] == [
            "SomeConfig.strict"
        ]

    @pytest.mark.parametrize("shape", list(_SOURCES), ids=list(_SOURCES))
    def test_any_read_shape_clears_the_field(self, tmp_path, shape):
        fields, root = self._fields(tmp_path)
        (root / "consumer.py").write_text(self._SOURCES[shape], encoding="utf-8")
        reads = collect_bool_field_reads(fields, (root,))
        assert zero_read_bool_fields(fields, reads) == []


class TestDeadBooleanConfigFieldsBaselineRatchet:
    """722 D5 — both-directions stale check and the whole-file-waiver ban."""

    def test_stale_entry_is_flagged(self):
        baseline = {("src/baldur/a.py", "SomeConfig.strict"): 1}
        assert stale_baseline_entries(baseline, set()) == [
            "src/baldur/a.py::SomeConfig.strict"
        ]

    def test_matching_entry_is_not_stale(self):
        entry = ("src/baldur/a.py", "SomeConfig.strict")
        assert stale_baseline_entries({entry: 1}, {entry}) == []

    def test_whole_file_waiver_is_flagged(self):
        baseline = {("src/baldur/a.py", None): 1}
        assert whole_file_waiver_entries(baseline) == ["src/baldur/a.py"]
        # A whole-file waiver carries no symbol, so it is never "stale".
        assert stale_baseline_entries(baseline, set()) == []

    def test_symbol_entries_are_not_waivers(self):
        baseline = {("src/baldur/a.py", "SomeConfig.strict"): 1}
        assert whole_file_waiver_entries(baseline) == []


class TestDeadBooleanConfigFieldsFalsePositiveGuard:
    """722 SC — the measured OSS false-positive shapes stay out of the population."""

    def test_report_dto_fields_are_not_in_the_population(self):
        """A settings-recommendation report item is state, not a config claim."""
        population = {f.symbol for f in discover_bool_config_fields((oss_src_root(),))}
        assert "RecommendationItem.is_cascade" not in population
        assert not any(
            symbol.split(".", 1)[0].endswith(OBSERVED_STATE_SUFFIXES)
            for symbol in population
        )

    def test_population_is_non_vacuous(self):
        """Anti-vacuous-pass: the live OSS scan must find a real population."""
        assert len(discover_bool_config_fields((oss_src_root(),))) >= 100


class TestG72TierAbsentSkip:
    """722 D6 — the live scan skips when a tier is absent; fixtures still run."""

    _MOD = "tests.architecture.test_dead_boolean_config_fields"

    def test_skips_when_pro_tier_absent(self, monkeypatch, tmp_path):
        (tmp_path / "baldur").mkdir()
        (tmp_path / "baldur_dormant").mkdir()
        monkeypatch.setattr(f"{self._MOD}._SRC", tmp_path)
        with pytest.raises(pytest.skip.Exception):
            TestDeadBooleanConfigFields().test_no_dead_boolean_config_field()

    def test_skips_when_dormant_tier_absent(self, monkeypatch, tmp_path):
        (tmp_path / "baldur").mkdir()
        (tmp_path / "baldur_pro").mkdir()
        monkeypatch.setattr(f"{self._MOD}._SRC", tmp_path)
        with pytest.raises(pytest.skip.Exception):
            TestDeadBooleanConfigFields().test_no_dead_boolean_config_field()

    def test_runs_when_every_tier_present(self, monkeypatch, tmp_path):
        # Both guards pass -> the body runs. The heavy scan is stubbed to an
        # empty population so this isolates the guard's pass-through branch:
        # running the real analysis with a tier absent would false-positive
        # every cross-tier-read field as dead.
        for tier in ("baldur", "baldur_pro", "baldur_dormant"):
            (tmp_path / tier).mkdir()
        monkeypatch.setattr(f"{self._MOD}._SRC", tmp_path)
        monkeypatch.setattr(
            f"{self._MOD}.discover_bool_config_fields", lambda roots: []
        )
        monkeypatch.setattr(
            f"{self._MOD}.collect_bool_field_reads", lambda fields, roots: {}
        )
        # Must not raise Skipped: the guard passed and the stubbed body is clean.
        TestDeadBooleanConfigFields().test_no_dead_boolean_config_field()
