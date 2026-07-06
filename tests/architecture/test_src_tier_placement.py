"""G39 — Deferred/Dormant-tier implementations MUST NOT live under ``src/baldur``.

Impl doc 599 D13. Both public surfaces ship ``src/baldur`` wholesale — the PyPI
wheel (``pyproject.toml`` wheel target) and the planned public GitHub mirror
(release checklist 7.5G-1) — so a Deferred/Dormant-tier implementation that
lands under ``src/baldur`` becomes public at first publish, and graduating a
Deferred feature to a paid PRO feature afterwards gives the implementation
away. Doc 599 physically relocated the ten mover subtrees to
``baldur_pro`` / ``baldur_dormant``; this gate keeps the boundary from
silently regressing. Two checks:

1. **Relocated-set enforced-empty** — none of the ten relocated path prefixes
   (pinned in ``scripts/verify_oss_wheel.py:RELOCATED_PATH_PREFIXES``, the
   single source of truth shared with the publish-time artifact guard) may
   exist as a file or package under ``src/``. Zero false positives by
   construction: the list is pinned, not derived.

2. **Manifest-tier placement map** — every ``V1_LAUNCH_MANIFEST.yaml`` row
   with tier ∈ {Deferred, Dormant} must map to an entry in ``PLACEMENT_MAP``
   declaring where the implementation lives: ``baldur_pro``,
   ``baldur_dormant``, or ``oss-exempt`` with a mandatory rationale string. A
   new Deferred/Dormant manifest row without a map entry FAILS — placement is
   decided at feature-introduction time, not discovered at publish time. For
   private homes the map also pins the **OSS chassis allowlist**: the exact
   feature-named paths (settings/handlers/views/urls/recorders/interfaces/
   models/adapters) allowed to stay OSS per the D7 ProviderRegistry-chassis
   pattern. Any feature-named path under ``src/baldur`` outside that allowlist
   FAILS, and a stale allowlist entry (covering nothing) FAILS — the map can
   drift in neither direction.

**Graduation hook**: when a Deferred feature graduates (manifest tier flips to
v1.0), its ``PLACEMENT_MAP`` key goes stale and the no-stale check fires —
forcing the placement entry to be re-triaged at exactly the manifest-flip
moment (the re-tier re-audit point).

Known limitations (documented per D13):
- A feature with no enable flag escapes the manifest trigger — but a
  Deferred/Dormant feature without a flag already violates 527's defaults-OFF
  discipline; out of gate scope.
- The chassis scan keys on *feature-named* path segments (``<stem>.py`` or a
  directory named ``<stem>``); an implementation hidden under an unrelated
  name escapes it. The pinned relocated set (check 1) and review remain the
  backstop for that class.

OSS-only-checkout robust: both checks assert about ``src/baldur`` only; the
``impl_path``-existence staleness check skips entries whose private root is
absent from the checkout (G37 skip precedent).

Baseline: ENFORCED-EMPTY — a violation is relocated or consciously mapped
(``oss-exempt`` + rationale), never baselined.

Rule registry: ``ARCHITECTURE.md#g39-src-tier-placement``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from scripts.verify_oss_wheel import RELOCATED_PATH_PREFIXES
from tests.architecture.conftest import PROJECT_ROOT, walk_src

_SRC = PROJECT_ROOT / "src"
_BALDUR_ROOT = _SRC / "baldur"
_MANIFEST_PATH = _BALDUR_ROOT / "_data" / "V1_LAUNCH_MANIFEST.yaml"
_PRIVATE_TIERS = frozenset({"Deferred", "Dormant"})
_PRIVATE_HOMES = frozenset({"baldur_pro", "baldur_dormant"})
_OSS_EXEMPT = "oss-exempt"
_RULE_ANCHOR = "ARCHITECTURE.md#g39-src-tier-placement"


@dataclass(frozen=True)
class PlacementEntry:
    """Placement decision for one Deferred/Dormant settings module.

    ``impl_home`` is ``baldur_pro`` / ``baldur_dormant`` (implementation lives
    in the private distribution) or ``oss-exempt`` (implementation deliberately
    stays OSS; ``rationale`` is then mandatory). ``impl_path`` is the
    implementation subtree relative to the private home root. ``oss_chassis``
    pins the exact feature-named OSS paths allowed to remain (the settings
    module ``settings/<stem>.py`` is implicitly allowed per D6 — the whole PRO
    launch set keeps settings OSS). ``pinned_dir`` optionally pins a shared
    OSS directory to an exact module set (split-home case: the elector moved,
    the chassis directory stays).
    """

    impl_home: str
    impl_path: str | None = None
    oss_chassis: tuple[str, ...] = ()
    rationale: str | None = None
    pinned_dir: tuple[str, frozenset[str]] | None = None


# Placement decisions for every Deferred/Dormant settings module in
# V1_LAUNCH_MANIFEST.yaml (599 D13). Keys are manifest `module` filenames.
# Adding a Deferred/Dormant manifest row REQUIRES adding a row here — that
# edit is the conscious placement decision this gate exists to force.
PLACEMENT_MAP: dict[str, PlacementEntry] = {
    # ── Deferred → baldur_pro ──
    "auto_tuning.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/auto_tuning",
        oss_chassis=(
            "api/django/urls/auto_tuning.py",
            "api/django/views/auto_tuning.py",
            "api/handlers/auto_tuning.py",
            "metrics/recorders/auto_tuning.py",
        ),
    ),
    "circuit_breaker_advanced.py": PlacementEntry(
        impl_home=_OSS_EXEMPT,
        rationale=(
            "Advanced-CB knobs are woven into the OSS core circuit breaker "
            "(core/config.py aggregation) — no dedicated subtree to relocate; "
            "runtime exposure neutralized by 527 defaults-OFF."
        ),
    ),
    "backpressure.py": PlacementEntry(
        impl_home=_OSS_EXEMPT,
        rationale=(
            "Implementation is woven into the OSS scaling chassis (scaling/, "
            "api middleware, resilience policy guards) rather than a "
            "relocatable package; not part of 599's mover set; runtime "
            "exposure neutralized by 527 defaults-OFF."
        ),
    ),
    "circuit_mesh.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/circuit_mesh",
    ),
    "corruption_shield.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/corruption_shield",
        oss_chassis=("metrics/recorders/corruption_shield.py",),
    ),
    "error_budget.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/error_budget",
        oss_chassis=(
            "api/admin/routes/error_budget.py",
            "api/django/urls/error_budget.py",
            "api/django/views/error_budget",
            "api/django/views/xtest/error_budget.py",
            "interfaces/error_budget.py",
            "resilience/policies/guards/error_budget.py",
            "services/config_shadow/evaluators/error_budget.py",
        ),
    ),
    "error_budget_gate.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/error_budget_gate",
        oss_chassis=("api/handlers/error_budget_gate.py",),
    ),
    "error_budget_propagation.py": PlacementEntry(
        # Hosted inside the error_budget package (propagation.py), not a
        # sibling subtree.
        impl_home="baldur_pro",
        impl_path="services/error_budget",
    ),
    "finops.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/finops",
        oss_chassis=(
            "api/django/views/finops.py",
            "api/handlers/finops.py",
        ),
    ),
    "leader_election.py": PlacementEntry(
        # Split-home (D4): only the elector moved to baldur_pro/coordination;
        # the coordination chassis (scheduler, dlq_consumer, factory, ...)
        # stays OSS. The pinned_dir set IS the allowlist for that directory —
        # a new elector implementation added there fails the gate.
        impl_home="baldur_pro",
        impl_path="coordination",
        pinned_dir=(
            "coordination",
            frozenset(
                {
                    "__init__.py",
                    "base.py",
                    "config.py",
                    "dlq_consumer.py",
                    "factory.py",
                    "local_file_elector.py",
                    "metrics.py",
                    "noop_elector.py",
                    "scheduler.py",
                    "shutdown_integration.py",
                }
            ),
        ),
    ),
    "meta_watchdog.py": PlacementEntry(
        # The OSS self-monitoring chassis lives at baldur/meta/ (stem-invisible
        # to the chassis scan; v1.0-tier rows enabled/escalation_enabled own it
        # — OSS observes). The Deferred rows (recovery, self-CB) gate
        # baldur_pro/services/meta_watchdog.
        impl_home="baldur_pro",
        impl_path="services/meta_watchdog",
        oss_chassis=(
            "api/django/urls/meta_watchdog.py",
            "api/django/views/meta_watchdog.py",
            "api/handlers/meta_watchdog.py",
            "interfaces/meta_watchdog.py",
        ),
    ),
    "pool_monitor.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/pool_monitor",
        oss_chassis=(
            "interfaces/pool_monitor.py",
            "metrics/recorders/pool_monitor.py",
        ),
    ),
    "postmortem.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/postmortem",
        oss_chassis=(
            "adapters/celery/tasks/postmortem.py",
            "adapters/django/repositories/postmortem.py",
            "adapters/memory/postmortem.py",
            "adapters/sql/postmortem.py",
            "api/django/urls/postmortem.py",
            "api/django/views/postmortem.py",
            "api/handlers/postmortem.py",
            "metrics/recorders/postmortem.py",
        ),
    ),
    "runbook.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/runbook",
    ),
    "saga.py": PlacementEntry(
        impl_home="baldur_pro",
        impl_path="services/saga",
        oss_chassis=("models/saga.py",),
    ),
    # ── Dormant → baldur_dormant ──
    "compliance.py": PlacementEntry(
        impl_home="baldur_dormant",
        impl_path="services/compliance",
        oss_chassis=(
            "api/django/urls/compliance.py",
            "api/django/views/compliance.py",
            "api/handlers/compliance.py",
            "models/compliance.py",
        ),
    ),
    "correlation_engine.py": PlacementEntry(
        impl_home="baldur_dormant",
        impl_path="services/correlation_engine",
        oss_chassis=("metrics/recorders/correlation_engine.py",),
    ),
    "drift_threshold.py": PlacementEntry(
        impl_home=_OSS_EXEMPT,
        rationale=(
            "Diffuse Dormant slice woven through metrics/reconciler, "
            "models/drift_config, core config/safe_defaults and the api "
            "3-surface — no relocatable subtree; extraction cost > benefit "
            "(599 D15); runtime exposure neutralized by 527 defaults-OFF; "
            "extraction deferred via OOS_INDEX row #599."
        ),
    ),
    "ml_models.py": PlacementEntry(
        impl_home="baldur_dormant",
        impl_path="services/ml_models",
    ),
    "predictive_forecaster.py": PlacementEntry(
        impl_home="baldur_dormant",
        impl_path="services/predictive_forecaster",
    ),
}


def _is_live_package(base: Path) -> bool:
    """True when ``base`` is a directory containing at least one ``.py`` file.

    A directory holding only ``__pycache__`` debris (stale local bytecode left
    behind by the physical move) is NOT a violation — any real package has at
    least one ``.py`` file, and CI checkouts never carry bytecode.
    """
    return base.is_dir() and any(base.rglob("*.py"))


def relocated_set_violations(src_dir: Path) -> list[str]:
    """Check 1 (pure over ``src_dir``): relocated prefixes must not exist."""
    violations: list[str] = []
    for prefix in RELOCATED_PATH_PREFIXES:
        base = src_dir.joinpath(*prefix.split("/"))
        if _is_live_package(base):
            violations.append(f"{prefix}/ exists as a package under {src_dir}")
        if base.with_suffix(".py").is_file():
            violations.append(f"{prefix}.py exists as a module under {src_dir}")
    return violations


def load_deferred_dormant_modules() -> set[str]:
    """Distinct settings-module filenames of all Deferred/Dormant manifest rows."""
    with _MANIFEST_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return {
        entry["module"]
        for entry in data.get("entries") or []
        if entry.get("tier") in _PRIVATE_TIERS
    }


def missing_placement_entries(modules: set[str]) -> list[str]:
    """Modules that have no PLACEMENT_MAP row (check 2 completeness, pure)."""
    return sorted(modules - set(PLACEMENT_MAP))


def stem_hits(baldur_root: Path, stem: str) -> set[str]:
    """``baldur_root``-relative posix paths of feature-named ``.py`` files.

    A file hits when its basename is ``<stem>.py`` or any directory segment of
    its relative path equals ``stem``. Segment-exact matching keeps the false
    positive rate at zero (``saga_helper.py`` does not hit stem ``saga``).
    """
    hits: set[str] = set()
    for path in walk_src((baldur_root,)):
        rel = path.relative_to(baldur_root).as_posix()
        parts = rel.split("/")
        if parts[-1] == f"{stem}.py" or stem in parts[:-1]:
            hits.add(rel)
    return hits


def chassis_violations(
    baldur_root: Path, stem: str, allowlist: tuple[str, ...]
) -> list[str]:
    """Feature-named paths outside the allowlist + stale allowlist entries.

    The settings module ``settings/<stem>.py`` is implicitly allowed (D6).
    A directory allowlist entry covers every file beneath it.
    """
    hits = stem_hits(baldur_root, stem)
    covered_entries: set[str] = set()
    violations: list[str] = []
    for hit in sorted(hits):
        if hit == f"settings/{stem}.py":
            continue
        for entry in allowlist:
            if hit == entry or hit.startswith(f"{entry}/"):
                covered_entries.add(entry)
                break
        else:
            violations.append(
                f"src/baldur/{hit}: feature-named path for Deferred/Dormant "
                f"module {stem!r} outside its OSS chassis allowlist"
            )
    for entry in sorted(set(allowlist) - covered_entries):
        violations.append(
            f"stale oss_chassis entry {entry!r} for {stem!r}: covers no "
            "existing file — remove it from PLACEMENT_MAP"
        )
    return violations


_DEFERRED_DORMANT_MODULES: set[str] = (
    load_deferred_dormant_modules() if _MANIFEST_PATH.is_file() else set()
)


class TestRelocatedSetEnforcedEmpty:
    """Check 1 — the ten relocated paths may never reappear under src/baldur."""

    @pytest.mark.parametrize("prefix", RELOCATED_PATH_PREFIXES)
    def test_relocated_path_absent(self, prefix: str):
        base = _SRC.joinpath(*prefix.split("/"))
        assert not _is_live_package(base), (
            f"G39: {prefix} reappeared under src/ — Deferred/Dormant "
            f"implementations live in baldur_pro/baldur_dormant (599 D2). "
            f"[{_RULE_ANCHOR}]"
        )
        assert not base.with_suffix(".py").is_file(), (
            f"G39: {prefix} reappeared under src/ — Deferred/Dormant "
            f"implementations live in baldur_pro/baldur_dormant (599 D2). "
            f"[{_RULE_ANCHOR}]"
        )

    def test_pinned_list_shape(self):
        """Single-source sanity: the script-shared list keeps its pinned shape."""
        assert len(RELOCATED_PATH_PREFIXES) == 10
        assert len(set(RELOCATED_PATH_PREFIXES)) == 10
        for prefix in RELOCATED_PATH_PREFIXES:
            assert prefix.startswith("baldur/")
            assert not prefix.endswith("/")

    def test_detects_planted_package_and_module(self, tmp_path: Path):
        """Non-vacuity (SC4 mutation probe, permanent form): a planted package
        and a planted module are both flagged; a clean tree is not."""
        assert relocated_set_violations(tmp_path) == []
        pkg = tmp_path / "baldur" / "services" / "runbook"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        mod = tmp_path / "baldur" / "coordination" / "redis_elector.py"
        mod.parent.mkdir(parents=True)
        mod.write_text("", encoding="utf-8")
        flagged = relocated_set_violations(tmp_path)
        assert any("baldur/services/runbook/" in v for v in flagged)
        assert any("baldur/coordination/redis_elector.py" in v for v in flagged)


class TestManifestPlacementMap:
    """Check 2 — every Deferred/Dormant manifest row has a placement decision."""

    def test_manifest_loaded(self):
        """Non-vacuity: the manifest yields the known Deferred/Dormant modules."""
        assert _MANIFEST_PATH.is_file(), f"missing {_MANIFEST_PATH}"
        assert "runbook.py" in _DEFERRED_DORMANT_MODULES  # Deferred pin
        assert "compliance.py" in _DEFERRED_DORMANT_MODULES  # Dormant pin
        assert len(_DEFERRED_DORMANT_MODULES) >= 15

    @pytest.mark.parametrize("module", sorted(_DEFERRED_DORMANT_MODULES))
    def test_module_has_placement_entry(self, module: str):
        assert module in PLACEMENT_MAP, (
            f"G39: Deferred/Dormant manifest module {module!r} has no "
            f"PLACEMENT_MAP entry — decide where the implementation lives "
            f"(baldur_pro / baldur_dormant / oss-exempt + rationale) at "
            f"feature-introduction time. [{_RULE_ANCHOR}]"
        )

    def test_no_stale_placement_entries(self):
        """A graduated/removed feature's entry must be re-triaged, not linger."""
        stale = sorted(set(PLACEMENT_MAP) - _DEFERRED_DORMANT_MODULES)
        assert not stale, (
            f"G39: PLACEMENT_MAP entr(ies) {stale} no longer match any "
            f"Deferred/Dormant manifest row — the feature graduated or was "
            f"removed; re-triage its placement at the manifest-flip moment "
            f"(claim-wiring re-tier hook). [{_RULE_ANCHOR}]"
        )

    def test_entry_shapes_valid(self):
        problems: list[str] = []
        for module, entry in sorted(PLACEMENT_MAP.items()):
            if entry.impl_home in _PRIVATE_HOMES:
                if not entry.impl_path:
                    problems.append(f"{module}: private home without impl_path")
            elif entry.impl_home == _OSS_EXEMPT:
                if not (entry.rationale or "").strip():
                    problems.append(f"{module}: oss-exempt without a rationale")
            else:
                problems.append(f"{module}: invalid impl_home {entry.impl_home!r}")
        assert not problems, (
            "G39: malformed PLACEMENT_MAP entr(ies):\n  " + "\n  ".join(problems)
        )

    def test_impl_paths_exist(self):
        """Map staleness: declared private implementation subtrees exist.

        Entries whose private root is absent (public-OSS-only checkout) are
        skipped — the gate asserts about src/baldur only there.
        """
        problems: list[str] = []
        for module, entry in sorted(PLACEMENT_MAP.items()):
            if entry.impl_home not in _PRIVATE_HOMES:
                continue
            root = _SRC / entry.impl_home
            if not root.is_dir():
                continue
            target = root.joinpath(*entry.impl_path.split("/"))
            if not target.is_dir() and not target.with_suffix(".py").is_file():
                problems.append(
                    f"{module}: declared impl_path "
                    f"{entry.impl_home}/{entry.impl_path} does not exist"
                )
        assert not problems, "G39: stale PLACEMENT_MAP impl_path(s):\n  " + "\n  ".join(
            problems
        )

    def test_oss_chassis_covers_feature_named_paths(self):
        """No feature-named OSS path outside the allowlist; no stale allowlist."""
        violations: list[str] = []
        for module, entry in sorted(PLACEMENT_MAP.items()):
            if entry.impl_home not in _PRIVATE_HOMES:
                continue
            stem = module.removesuffix(".py")
            violations.extend(chassis_violations(_BALDUR_ROOT, stem, entry.oss_chassis))
        assert not violations, (
            f"G39: {len(violations)} OSS chassis violation(s) — a "
            f"Deferred/Dormant feature grew a new OSS surface (allowlist it "
            f"consciously in PLACEMENT_MAP, or relocate it) or the allowlist "
            f"went stale. [{_RULE_ANCHOR}]\n  " + "\n  ".join(violations)
        )

    def test_pinned_dirs_exact(self):
        """Split-home directories contain exactly their pinned module set."""
        problems: list[str] = []
        for module, entry in sorted(PLACEMENT_MAP.items()):
            if entry.pinned_dir is None:
                continue
            dir_rel, allowed = entry.pinned_dir
            actual = {p.name for p in (_BALDUR_ROOT / dir_rel).glob("*.py")}
            extra = sorted(actual - allowed)
            missing = sorted(allowed - actual)
            if extra:
                problems.append(
                    f"{module}: unexpected module(s) in baldur/{dir_rel}: {extra}"
                )
            if missing:
                problems.append(
                    f"{module}: pinned module(s) missing from baldur/{dir_rel}: "
                    f"{missing}"
                )
        assert not problems, (
            "G39: split-home pinned-directory drift:\n  " + "\n  ".join(problems)
        )


class TestPlacementHelpers:
    """Non-vacuity probes — each check 2 helper flags the bad shape."""

    def test_missing_entry_detected(self):
        assert missing_placement_entries({"made_up_feature.py"}) == [
            "made_up_feature.py"
        ]
        assert missing_placement_entries({"runbook.py"}) == []

    def test_stem_hits_segment_exact(self, tmp_path: Path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "widget.py").write_text("", encoding="utf-8")
        (tmp_path / "api" / "widget_helper.py").write_text("", encoding="utf-8")
        (tmp_path / "widget").mkdir()
        (tmp_path / "widget" / "inner.py").write_text("", encoding="utf-8")
        hits = stem_hits(tmp_path, "widget")
        assert hits == {"api/widget.py", "widget/inner.py"}

    def test_chassis_scan_flags_unallowlisted_and_stale(self, tmp_path: Path):
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "widget.py").write_text("", encoding="utf-8")
        (tmp_path / "settings").mkdir()
        (tmp_path / "settings" / "widget.py").write_text("", encoding="utf-8")
        # Unallowlisted hit flagged; settings module implicitly allowed.
        flagged = chassis_violations(tmp_path, "widget", ())
        assert len(flagged) == 1
        assert "api/widget.py" in flagged[0]
        # Allowlisted -> clean.
        assert chassis_violations(tmp_path, "widget", ("api/widget.py",)) == []
        # Stale allowlist entry flagged.
        stale = chassis_violations(
            tmp_path, "widget", ("api/widget.py", "models/widget.py")
        )
        assert len(stale) == 1
        assert "stale oss_chassis" in stale[0]

    def test_directory_allowlist_entry_covers_subtree(self, tmp_path: Path):
        views = tmp_path / "api" / "views" / "widget"
        views.mkdir(parents=True)
        (views / "__init__.py").write_text("", encoding="utf-8")
        assert chassis_violations(tmp_path, "widget", ("api/views/widget",)) == []
