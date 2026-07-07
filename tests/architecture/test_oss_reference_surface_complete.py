"""G25 — OSS reference-surface completeness.

The published API reference (``docs/reference/**``) renders each curated
``__all__`` facade. Historically every large package was documented with a
single whole-package ``::: baldur.<pkg>`` directive that auto-tracked
``__all__`` — a new symbol appeared on the page for free. Impl doc 554 split
the oversized pages into hand-authored themed per-symbol pages, which removed
that inherent drift-freeness. G25 restores it: every obligated package's
``__all__`` symbol MUST be rendered by some ``:::`` directive somewhere under
``docs/reference/**``.

Coverage model — **leaf-name** (554 D3). Three models were prototyped; only
leaf-name is green on the real tree, because directive forms are heterogeneous
and Python re-export shadowing corrupts object identity:

* A whole-package directive (``::: P`` where ``P`` imports as a package) covers
  **all** of ``P.__all__``.
* A symbol/plain-module directive covers its ``rsplit('.', 1)`` leaf, attributed
  to package ``P`` when the directive's parent is ``P`` (e.g. ``::: baldur.init``
  → ``baldur``) or a non-package module directly under ``P`` (the decorators
  ``::: baldur.decorators.dlq_protect.dlq_protect`` disambiguation form →
  ``baldur.decorators``).

A package is **complete** iff ``set(P.__all__) ⊆ covered_leaves(P)``.

Obligated-package discovery (no hardcoded list): a directive target that
resolves to a **package** obligates that package; a genuine **symbol** target
(no such module on disk) obligates its attributed package. A directive whose
target resolves to a plain **module** (``baldur.protect_facade`` / ``baldur.core.exceptions``)
contributes a covered leaf to its parent package but does NOT obligate anything
— this is why plain modules and non-curated parents (``baldur.core``,
``baldur.adapters``) stay out of the obligated set, yielding exactly the curated
reference packages.

Classification is driven by ``importlib.util.find_spec`` (which inspects the
import system WITHOUT executing a target's ``__init__``), so the rule never
imports optional-extra adapters (``django`` / ``fastapi`` / ``flask`` /
``gunicorn``) — their pages are whole-package directives and therefore complete
by construction. Only the leaf-split packages (all core, no extras) are fully
imported to read ``__all__`` for the completeness check.

Allowlist is reviewed and enforced-near-empty — it holds only the public symbols
that genuinely cannot be rendered by a per-symbol ``:::`` directive:
``baldur.__version__`` (a version string) and the two ``baldur.services``
security accessors built by a generic singleton factory via tuple-unpacking
(griffe cannot statically resolve a factory-closure target). A new uncovered
``__all__`` symbol is given a ``:::`` directive, never baselined.

Rule registry:
``ARCHITECTURE.md#g25-oss-reference-surface-complete``
"""

from __future__ import annotations

import importlib
import importlib.util
from collections import defaultdict
from pathlib import Path

from tests.architecture.conftest import REFERENCE_DIR, directive_targets

# Reviewed, enforced-near-empty allowlist of ``(package, leaf)`` public symbols
# that genuinely cannot be rendered by a per-symbol ``:::`` directive. A real
# uncovered symbol is documented (given a ``:::`` line), never added here.
_REFERENCE_SURFACE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # A version string, not a mkdocstrings-renderable object.
        ("baldur", "__version__"),
        # Built by a generic singleton factory via tuple-unpacking assignment
        # (``(get, reset, ...) = make_singleton_factory(...)``), so griffe cannot
        # statically resolve them for a per-symbol page — a ``:::`` line hard-fails
        # ``mkdocs build --strict``. Documented in prose on the security page.
        ("baldur.services", "get_security_violation_service"),
    }
)

# Reviewed OSS<->private boundary symbols DE-PUBLISHED from the rendered
# reference: they stay in ``__all__`` as stable import seams that the private
# packages type-hint against and implement, but naming them on the public site
# would disclose either a private-tier adapter no public user can enable or a
# post-launch feature not in the v1.0 shipped set. Removal from ``__all__`` was
# rejected — it demotes the seam to unguaranteed-internal under the private
# tier's pinned-floor build. Reversible: when a feature ships, delete its entry
# here and restore its ``:::`` directive (the graduation reversal is a
# maintainer checklist).
_UNRENDERED_BOUNDARY_SYMBOLS: frozenset[tuple[str, str]] = frozenset(
    {
        # Private-tier boundary — implementations ship only in the private,
        # non-installable distribution.
        ("baldur.interfaces", "KafkaEventBusProtocol"),
        ("baldur.interfaces", "ConsumedEventProtocol"),
        ("baldur.interfaces", "NoOpKafkaEventBus"),
        ("baldur.interfaces", "NoOpKafkaAuditAdapter"),
        ("baldur.interfaces", "NoOpWormAdapter"),
        ("baldur.interfaces", "QuorumLease"),
        ("baldur.interfaces", "QuorumWitnessProtocol"),
        ("baldur.interfaces", "RoutingChange"),
        ("baldur.interfaces", "TrafficRoutingAdapter"),
        ("baldur.interfaces", "LearningServiceProtocol"),
        # Post-launch feature boundary — registered under an active license but
        # not in the v1.0 launch set / not SemVer-marketed.
        ("baldur.interfaces", "RuntimeConfigManager"),
        ("baldur.interfaces", "ConnectionPoolMonitor"),
        ("baldur.interfaces", "PoolInfoProvider"),
        ("baldur.interfaces", "ConnectionInfo"),
        ("baldur.interfaces", "LeakReport"),
        ("baldur.interfaces", "NoOpPoolStatsProvider"),
        ("baldur.interfaces", "PoolHealthStatus"),
        ("baldur.interfaces", "PoolStats"),
        ("baldur.interfaces", "PoolStatsProvider"),
        ("baldur.interfaces", "OptimizationStrategy"),
        ("baldur.interfaces", "ChaosExperimentStore"),
        ("baldur.interfaces", "ChaosScheduler"),
        ("baldur.interfaces", "BlastRadiusManager"),
        ("baldur.interfaces", "ReportGenerator"),
        ("baldur.interfaces", "SafetyGuard"),
        ("baldur.interfaces", "ErrorBudgetGate"),
        ("baldur.interfaces", "ErrorBudgetService"),
        ("baldur.interfaces", "PostmortemRepository"),
        ("baldur.interfaces", "PostmortemData"),
        # In-memory adapters for the de-published state stores.
        ("baldur.adapters.memory", "InMemoryPostmortemRepository"),
        ("baldur.adapters.memory", "InMemoryChaosExperimentStore"),
    }
)

# Permanent large OSS packages that must ALWAYS be in the obligated set. These
# named anchors close the D3 "delete every page of a package → it silently drops
# from discovery → vacuous pass" hole for the two surfaces a reader is most
# likely to hit, without hardcoding the full package list.
_SANITY_ANCHOR_PACKAGES: tuple[str, ...] = (
    "baldur.interfaces",
    "baldur.services",
)


def _spec_kind(name: str) -> str:
    """Classify a dotted name via ``find_spec`` WITHOUT importing it.

    Returns ``"package"`` (has a submodule search path), ``"module"`` (a plain
    module file), ``"absent"`` (the import system finds no such module — a
    genuine symbol such as a class/function), or ``"error"`` (a parent in the
    path is not a package, e.g. the ``pkg.module.symbol`` disambiguation form).

    ``find_spec`` imports a target's *parent* packages to locate it but never
    executes the target's own module body, so this stays cheap and free of
    optional-extra side effects.
    """
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        return "error"
    if spec is None:
        return "absent"
    return "package" if spec.submodule_search_locations is not None else "module"


def _attributed_package(parent: str) -> str | None:
    """Return the package a leaf directive is attributed to, or None.

    The leaf is attributed to ``parent`` when ``parent`` is itself a package, or
    to the grandparent when ``parent`` is a non-package module directly under a
    package (the ``::: pkg.module.symbol`` decorators form). Returns None when
    neither resolves to a package.
    """
    kind = _spec_kind(parent)
    if kind == "package":
        return parent
    if kind == "module" and "." in parent:
        grandparent = parent.rsplit(".", 1)[0]
        if _spec_kind(grandparent) == "package":
            return grandparent
    return None


def _scan(reference_dir: Path) -> tuple[set[str], dict[str, set[str]], set[str]]:
    """Walk every reference ``:::`` directive once.

    Returns ``(whole_package_packages, leaf_covered, obligated)``:

    * ``whole_package_packages`` — packages rendered by a whole-package ``:::``
      (complete by construction; never need importing for the check);
    * ``leaf_covered`` — ``{package: {leaf, ...}}`` accumulated from symbol and
      plain-module directives;
    * ``obligated`` — every package whose ``__all__`` the reference must cover.
    """
    whole_package: set[str] = set()
    leaf_covered: dict[str, set[str]] = defaultdict(set)
    obligated: set[str] = set()

    for target in directive_targets(reference_dir):
        kind = _spec_kind(target)
        if kind == "package":
            whole_package.add(target)
            obligated.add(target)
            continue
        if "." not in target:
            continue
        parent, leaf = target.rsplit(".", 1)
        attributed = _attributed_package(parent)
        if attributed is None:
            continue
        leaf_covered[attributed].add(leaf)
        # A plain-module target (``baldur.protect_facade`` / ``baldur.core.exceptions``)
        # contributes a covered leaf but does NOT obligate its parent — only a
        # genuine symbol (``absent`` / ``error``) obligates.
        if kind != "module":
            obligated.add(attributed)

    return whole_package, dict(leaf_covered), obligated


def _obligated_packages(reference_dir: Path = REFERENCE_DIR) -> set[str]:
    """Return the set of packages whose ``__all__`` the reference must cover."""
    _whole, _covered, obligated = _scan(reference_dir)
    return obligated


def _covered_leaves(package: str, reference_dir: Path = REFERENCE_DIR) -> set[str]:
    """Return the leaf names the reference covers for ``package``.

    A whole-package directive covers all of ``package.__all__``; otherwise the
    accumulated per-leaf coverage is returned.
    """
    whole, covered, _obligated = _scan(reference_dir)
    if package in whole:
        module = _safe_import(package)
        return set(getattr(module, "__all__", ())) if module is not None else set()
    return set(covered.get(package, set()))


def _safe_import(name: str):
    """Import ``name`` returning the module, or None on any failure."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _uncovered_symbols(
    reference_dir: Path = REFERENCE_DIR,
    allowlist: frozenset[tuple[str, str]] = _REFERENCE_SURFACE_ALLOWLIST,
) -> dict[str, set[str]]:
    """Return ``{package: {uncovered __all__ symbol, ...}}`` (allowlist applied).

    Whole-package-obligated packages are complete by construction and are never
    imported. Symbol-obligated packages (all core, no optional extras) are
    imported to read ``__all__`` and compared against their covered leaves.
    """
    whole, covered, obligated = _scan(reference_dir)
    gaps: dict[str, set[str]] = {}
    for package in obligated:
        if package in whole:
            continue  # whole-package directive renders every __all__ member
        module = _safe_import(package)
        if module is None:
            continue  # obligated via a symbol but the package itself won't import
        names = set(getattr(module, "__all__", ()))
        allowed = {leaf for (pkg, leaf) in allowlist if pkg == package}
        allowed |= {
            leaf for (pkg, leaf) in _UNRENDERED_BOUNDARY_SYMBOLS if pkg == package
        }
        missing = names - covered.get(package, set()) - allowed
        if missing:
            gaps[package] = missing
    return gaps


class TestOssReferenceSurfaceComplete:
    """G25 — every obligated package's ``__all__`` is fully rendered."""

    def test_obligated_and_covered_sets_are_nonempty(self):
        """Anti-vacuous-pass guard: a broken resolver would pass vacuously."""
        whole, covered, obligated = _scan(REFERENCE_DIR)
        assert obligated, (
            "G25: zero obligated packages discovered — the directive resolver is "
            "broken, so the completeness check would pass vacuously."
        )
        assert any(covered.values()) or whole, (
            "G25: zero covered leaves discovered — the coverage resolver is "
            "broken, so the completeness check would pass vacuously."
        )

    def test_named_sanity_anchor_packages_are_obligated(self):
        """The two permanent large OSS packages must always be discovered.

        Closes the 'delete every page → silent drop → vacuous pass' hole for
        ``baldur.interfaces`` and ``baldur.services`` without hardcoding the
        full obligated-package list.
        """
        obligated = _obligated_packages()
        for package in _SANITY_ANCHOR_PACKAGES:
            assert package in obligated, (
                f"G25: {package} is not in the obligated set — its reference "
                "pages were deleted or its ::: directives stopped resolving. "
                "The completeness check no longer guards this package."
            )

    def test_every_obligated_package_all_symbol_is_covered(self):
        """The load-bearing gate: no obligated ``__all__`` symbol is unrendered."""
        gaps = _uncovered_symbols()
        offenders = [
            f"  {package}: {sorted(missing)}"
            for package, missing in sorted(gaps.items())
        ]
        assert not offenders, (
            "G25: public __all__ symbols with no ::: directive on the reference "
            f"surface ({sum(len(m) for m in gaps.values())} symbol(s) across "
            f"{len(gaps)} package(s)). Add a `::: baldur.<pkg>.<Symbol>` line to "
            "the themed page, or — for a genuinely non-renderable symbol — add a "
            "reviewed (package, leaf) entry to _REFERENCE_SURFACE_ALLOWLIST.\n"
            + "\n".join(offenders)
        )

    def test_allowlist_is_enforced_near_empty(self):
        """The allowlist holds only the documented non-renderable entry, and

        every entry names a real ``__all__`` symbol (guards against a typo
        silently suppressing a genuine gap).
        """
        assert _REFERENCE_SURFACE_ALLOWLIST == frozenset(
            {
                ("baldur", "__version__"),
                ("baldur.services", "get_security_violation_service"),
            }
        )
        for package, leaf in _REFERENCE_SURFACE_ALLOWLIST:
            module = _safe_import(package)
            assert module is not None, f"allowlist package {package!r} must import"
            assert leaf in set(getattr(module, "__all__", ())), (
                f"allowlist entry ({package!r}, {leaf!r}) is not in "
                f"{package}.__all__ — a stale or mistyped allowlist entry would "
                "silently mask a real coverage gap."
            )

    def test_unrendered_boundary_symbols_are_valid(self):
        """Every de-published boundary entry names a real ``__all__`` symbol.

        The set is not pinned to an exact membership (it shrinks as features
        graduate), but each entry MUST still name a live ``__all__`` symbol: a
        stale entry (leaf renamed
        or dropped from ``__all__``) would silently suppress a real coverage
        gap, and an entry whose symbol was never in ``__all__`` is a typo.
        """
        assert _UNRENDERED_BOUNDARY_SYMBOLS, (
            "G25: _UNRENDERED_BOUNDARY_SYMBOLS is empty — the de-publish "
            "allowlist vanished, so every boundary symbol's ::: removal would "
            "regress the completeness gate."
        )
        for package, leaf in _UNRENDERED_BOUNDARY_SYMBOLS:
            module = _safe_import(package)
            assert module is not None, f"boundary package {package!r} must import"
            assert leaf in set(getattr(module, "__all__", ())), (
                f"boundary entry ({package!r}, {leaf!r}) is not in "
                f"{package}.__all__ — a stale/mistyped entry masks a real gap."
            )


# --------------------------------------------------------------------------
# Model unit tests — fixture reference dirs built from REAL public symbols, so
# the find_spec-driven classifier exercises true import-system behaviour. Each
# locks one load-bearing property of the leaf-name model (554 Testability).
# --------------------------------------------------------------------------


def _write_reference(tmp_path: Path, *directives: str) -> Path:
    """Write a one-page fixture reference dir carrying ``directives``."""
    reference = tmp_path / "reference"
    reference.mkdir()
    body = "\n\n".join(f"::: {target}" for target in directives) + "\n"
    (reference / "page.md").write_text("# Fixture\n\n" + body, encoding="utf-8")
    return reference


class TestReferenceCompletenessModel:
    """The leaf-name coverage model behaves as specified (anti-silent-pass)."""

    def test_uncovered_symbol_is_flagged(self, tmp_path):
        """A page omitting one package symbol must surface a gap."""
        reference = _write_reference(
            tmp_path,
            "baldur.adapters.cache.RedisCacheAdapter",
            "baldur.adapters.cache.InMemoryCacheAdapter",
            "baldur.adapters.cache.AsyncRedisCacheAdapter",
            "baldur.adapters.cache.AsyncInMemoryCacheAdapter",
            # MetricsAwareCacheAdapter deliberately omitted.
        )
        gaps = _uncovered_symbols(reference, frozenset())
        assert gaps.get("baldur.adapters.cache") == {"MetricsAwareCacheAdapter"}

    def test_complete_fixture_is_not_flagged(self, tmp_path):
        """A page covering every package symbol must report no gap."""
        reference = _write_reference(
            tmp_path,
            "baldur.adapters.cache.RedisCacheAdapter",
            "baldur.adapters.cache.InMemoryCacheAdapter",
            "baldur.adapters.cache.MetricsAwareCacheAdapter",
            "baldur.adapters.cache.AsyncRedisCacheAdapter",
            "baldur.adapters.cache.AsyncInMemoryCacheAdapter",
        )
        gaps = _uncovered_symbols(reference, frozenset())
        assert "baldur.adapters.cache" not in gaps

    def test_whole_package_directive_covers_all_all(self, tmp_path):
        """``::: P`` covers every member of ``P.__all__``."""
        reference = _write_reference(tmp_path, "baldur.adapters.cache")
        import baldur.adapters.cache as cache

        assert _covered_leaves("baldur.adapters.cache", reference) == set(cache.__all__)
        assert _uncovered_symbols(reference, frozenset()) == {}

    def test_pkg_module_symbol_form_attributes_to_package(self, tmp_path):
        """The decorators disambiguation form attributes the leaf to the package.

        ``::: baldur.decorators.dlq_protect.dlq_protect`` must satisfy
        ``baldur.decorators.__all__``'s ``dlq_protect`` — attributed to the
        package, NOT the intermediate module ``baldur.decorators.dlq_protect``.
        """
        reference = _write_reference(
            tmp_path, "baldur.decorators.dlq_protect.dlq_protect"
        )
        assert "baldur.decorators" in _obligated_packages(reference)
        assert "dlq_protect" in _covered_leaves("baldur.decorators", reference)
        # The intermediate module must NOT become an obligated package.
        assert "baldur.decorators.dlq_protect" not in _obligated_packages(reference)

    def test_cross_package_same_leaf_requires_own_directive(self, tmp_path):
        """A shared leaf name covered for one package never satisfies another.

        ``NotificationChannel`` is in both ``baldur.interfaces.__all__`` and
        ``baldur.services.__all__``. A directive under ``interfaces`` must not
        count toward ``services``' coverage — the model is per-package.
        """
        reference = _write_reference(
            tmp_path,
            "baldur.interfaces.NotificationChannel",
            "baldur.services.get_circuit_breaker_service",
        )
        assert "NotificationChannel" in _covered_leaves("baldur.interfaces", reference)
        assert "NotificationChannel" not in _covered_leaves(
            "baldur.services", reference
        )

    def test_plain_module_directive_does_not_obligate_parent(self, tmp_path):
        """A plain-module directive contributes a leaf but obligates nothing.

        ``::: baldur.core.exceptions`` (a module) and ``::: baldur.protect_facade`` (a
        module) must keep ``baldur.core`` / ``baldur.adapters`` out of the
        obligated set — only their covered leaf is recorded.
        """
        reference = _write_reference(
            tmp_path, "baldur.core.exceptions", "baldur.protect_facade"
        )
        obligated = _obligated_packages(reference)
        assert "baldur.core" not in obligated
        assert "baldur.protect_facade" not in obligated
        # The leaf IS attributed to the parent package, just not obligating it.
        assert "protect_facade" in _covered_leaves("baldur", reference)
