"""G37 — FEATURE_CATALOG.md MUST NOT drift from code / settings reality.

Impl doc 589 D5. The product catalog (`docs/features/FEATURE_CATALOG.md`) used to
hand-copy enablement data the manifest owns — stale `SELFHEALING_*` env vars (a
no-op if an operator copies one; the runtime prefix is `BALDUR_*`) and stale
module paths (`resilience/bulkhead/`, which does not exist). Nothing enforced
that the catalog's citations still matched the code, so the drift accreted
silently. G37 is the inverse of the claim-wiring gates: it keeps the
catalog's four mechanically-checkable surfaces honest.

**Checks.**

* **(a) No legacy prefix** — the count of the obsolete pre-rename env-var prefix
  (`SELFHEALING_`) in the catalog is 0. Enablement data is delegated to
  `V1_LAUNCH_MANIFEST.yaml` + `docs/reference/env-vars.md`; a stale legacy var is
  a silent operator no-op.
* **(b) Cited env vars resolve** — every inline-code `BALDUR_*` token resolves via
  the #576 known-var universe (`settings.introspection.is_known_env_var` over
  `build_prefix_index()`): a field-backed Pydantic var (`BALDUR_ADMIN_BIND` →
  `AdminServerSettings.bind`) OR a catalogued direct-read var (`BALDUR_REDIS_URL`).
  A wildcard token (`BALDUR_META_WATCHDOG_*`) is split on the trailing `*` and its
  prefix must be a registered settings `env_prefix`.
* **(c) Module paths exist** — every `**Module**:` backtick path resolves under
  `src/baldur`, `src/baldur_pro`, or `src/baldur_dormant`.
* **(d) Structural completeness** — every catalog *product entry* (a `###` block
  carrying a `**Module**:` field or any axis label) exposes all three axis fields
  with a recognized value (`Product Status` ∈ {OSS, PRO, Parked}; `Code Role` ∈
  {product-feature, internal-support}; `Package` ∈ {baldur, baldur_pro,
  baldur_dormant}). A mistyped axis-field label would otherwise parse to empty and
  let the entry be silently skipped by both G36 and G37 — so a missing/mistyped
  axis field is a loud FAIL, not a silent skip.

**OSS-only-checkout robust.** A `**Module**:` path that is absent under
`src/baldur` AND whose only possible private homes (`baldur_pro` /
`baldur_dormant`) are not present in the checkout is SKIPPED rather than flagged
(G19 precedent) — the public repo collects this gate but ships no private
source tree.

The catalog parser is the SHARED `_helpers` parser (also used by G36), so the
triage predicate and the gate predicate cannot diverge.

**Baseline granularity** — ENFORCED-EMPTY (`catalog_tier_drift: []`). A drift is
FIXED (rename the var, correct the path, complete the axes), never baselined.

Rule registry: ``ARCHITECTURE.md#g37-catalog-tier-drift``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from baldur.settings.introspection import build_prefix_index, is_known_env_var
from tests.architecture._helpers import (
    CATALOG_PATH,
    CATALOG_SRC_ROOTS,
    CODE_ROLE_VALUES,
    PACKAGE_VALUES,
    PRODUCT_STATUS_VALUES,
    catalog_module_path_status,
    parse_catalog_entries,
)
from tests.architecture.conftest import collect_violations, iter_inline_code_spans

_RULE_KEY = "catalog_tier_drift"
_RULE_ANCHOR = "#g37-catalog-tier-drift"

# FEATURE_CATALOG.md is a private-repo-only artifact (not published to the public repo), so
# it is absent in the public OSS repo. G37 is fully covered by the private-repo
# run; an in-body skip (not a module-level skipif marker) keeps the skip path
# monkeypatch-testable and the synthetic anti-vacuous fixtures running in the
# public repo.
_CATALOG_ABSENT_REASON = "FEATURE_CATALOG.md is private-repo-only (not published)"


def _skip_if_catalog_absent() -> None:
    if not CATALOG_PATH.exists():
        pytest.skip(_CATALOG_ABSENT_REASON)


# The legacy (pre-rename) env-var prefix the catalog must no longer cite. Built
# from fragments so this gate file does not itself contain the literal token it
# forbids (the catalog and this test live in the same grep surface for the
# success criterion `grep -c <legacy> FEATURE_CATALOG.md == 0`).
_LEGACY_ENV_PREFIX = "SELFHEALING_"

# A `BALDUR_*` env-var token, optional trailing `*` (wildcard) and ignoring a
# trailing `=value` example. Run only inside inline-code spans.
_BALDUR_TOKEN_RE = re.compile(r"BALDUR_[A-Z0-9_]+\*?")


def cited_baldur_tokens(text: str) -> list[str]:
    """Every inline-code ``BALDUR_*`` token in the catalog (pure)."""
    tokens: list[str] = []
    for span in iter_inline_code_spans(text):
        for match in _BALDUR_TOKEN_RE.finditer(span):
            tokens.append(match.group(0))
    return tokens


def env_token_resolves(token: str, index: dict[str, set[str]]) -> bool:
    """True iff a cited ``BALDUR_*`` token is real (field-backed, direct-read, or wildcard prefix)."""
    if token.endswith("*"):
        prefix = token[:-1]
        return prefix in index
    return is_known_env_var(token, index)


class TestCatalogTierDrift:
    """G37 — the product catalog's mechanically-checkable citations stay honest."""

    def _entries(self):
        _skip_if_catalog_absent()
        text = CATALOG_PATH.read_text(encoding="utf-8")
        entries = parse_catalog_entries(text)
        # Anti-vacuous guard: the catalog always carries dozens of product
        # entries — an empty parse means extraction broke, not a clean surface.
        assert entries, "G37: FEATURE_CATALOG.md yielded no ### entries — parser broken"
        return entries

    def test_no_legacy_env_prefix(self):
        """(a) The obsolete pre-rename env-var prefix is absent from the catalog."""
        _skip_if_catalog_absent()
        text = CATALOG_PATH.read_text(encoding="utf-8")
        count = text.count(_LEGACY_ENV_PREFIX)
        assert count == 0, (
            f"G37: {count} occurrence(s) of the obsolete legacy env-var prefix in "
            f"FEATURE_CATALOG.md — the runtime prefix is BALDUR_*, so a copied "
            f"legacy var is a silent operator no-op. Delete it (delegate to "
            f"V1_LAUNCH_MANIFEST.yaml + docs/reference/env-vars.md)."
        )

    def test_cited_env_vars_resolve(self):
        """(b) Every inline-code BALDUR_* token resolves to a real settings sink."""
        _skip_if_catalog_absent()
        text = CATALOG_PATH.read_text(encoding="utf-8")
        index = build_prefix_index()
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for token in sorted(set(cited_baldur_tokens(text))):
            if env_token_resolves(token, index):
                continue
            raw.append(
                (
                    CATALOG_PATH,
                    None,
                    token,
                    f"cited env var {token} resolves to no (env_prefix, field) "
                    f"and is not a direct-read/wildcard prefix — a silent no-op",
                )
            )
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G37: {len(violations)} phantom BALDUR_* citation(s) in the catalog. "
            "Rename to the real BALDUR_<PREFIX>_<FIELD> or remove it.\n"
            + "\n".join(violations)
        )

    def test_module_paths_exist(self):
        """(c) Every **Module**: backtick path exists under a source root."""
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        checked = 0
        for entry in self._entries():
            for path in entry.modules:
                status = catalog_module_path_status(path)
                if status == "skip":
                    continue
                checked += 1
                if status == "missing":
                    raw.append(
                        (
                            CATALOG_PATH,
                            None,
                            entry.title,
                            f"Module path `{path}` does not exist under "
                            f"src/baldur[_pro|_dormant]",
                        )
                    )
        assert checked, "G37: no Module paths were verified — extraction broke"
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G37: {len(violations)} stale Module path(s) in the catalog. Fix the "
            "path to the real subtree (e.g. the bulkhead impl is at "
            "baldur_pro/services/bulkhead/).\n" + "\n".join(violations)
        )

    def test_every_product_entry_has_all_three_axes(self):
        """(d) Structural completeness — no product entry silently skips an axis."""
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        product_entries = 0
        for entry in self._entries():
            if not entry.is_product_entry:
                continue
            product_entries += 1
            missing = []
            if entry.product_status not in PRODUCT_STATUS_VALUES:
                missing.append(f"Product Status (got {entry.product_status!r})")
            if entry.code_role not in CODE_ROLE_VALUES:
                missing.append(f"Code Role (got {entry.code_role!r})")
            if entry.package not in PACKAGE_VALUES:
                missing.append(f"Package (got {entry.package!r})")
            if missing:
                raw.append(
                    (
                        CATALOG_PATH,
                        None,
                        entry.title,
                        "missing/mistyped axis field(s): " + ", ".join(missing),
                    )
                )
        # Anti-vacuous: the catalog has dozens of product entries.
        assert product_entries >= 30, (
            f"G37: only {product_entries} product entries parsed — the axis-field "
            "extraction is broken (expected ≥30)"
        )
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G37: {len(violations)} catalog product entr(ies) with a "
            "missing/mistyped axis field — every product entry MUST expose "
            "Product Status / Code Role / Package with a recognized value.\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Anti-silent-pass inline fixtures (G24/G30 precedent). Synthetic catalog
# fragments prove each check flags the bad shape and clears the good shape, so a
# helper bug cannot let a real drift pass while the live catalog happens to be
# clean. Type: Behavior — they exercise the parser/resolver branches directly.
# ---------------------------------------------------------------------------
_GOOD_ENTRY = (
    "### Widget\n\n"
    "- **Product Status**: PRO | **Code Role**: product-feature | "
    "**Package**: baldur_pro | **Category**: Resilience | **Module**: `services/dlq/`\n"
    "- **What**: A widget.\n"
)


class TestCatalogParser:
    """Shared `parse_catalog_entries` — extraction is exact (also exercised by G36)."""

    def test_parses_axis_fields_and_module(self):
        entry = parse_catalog_entries(_GOOD_ENTRY)[0]
        assert entry.title == "Widget"
        assert entry.product_status == "PRO"
        assert entry.code_role == "product-feature"
        assert entry.package == "baldur_pro"
        assert entry.modules == ("services/dlq/",)
        assert entry.is_product_entry
        assert entry.is_sold_product_feature

    def test_multi_module_paths_split(self):
        body = (
            "### X\n\n- **Module**: `services/postmortem/`, "
            "`services/correlation_engine/`\n"
        )
        entry = parse_catalog_entries(body)[0]
        assert entry.modules == ("services/postmortem/", "services/correlation_engine/")

    def test_section_heading_ends_entry(self):
        text = _GOOD_ENTRY + "\n## Next Section\n\n- **Module**: `nope/`\n"
        entries = parse_catalog_entries(text)
        # The `## Next Section` content is NOT folded into the Widget entry.
        assert len(entries) == 1
        assert entries[0].modules == ("services/dlq/",)

    def test_non_product_entry_without_axes_or_module(self):
        entry = parse_catalog_entries("### Prose\n\nJust narrative, no fields.\n")[0]
        assert not entry.is_product_entry

    def test_parked_internal_support_not_sold(self):
        body = (
            "### Lib\n\n- **Product Status**: Parked | **Code Role**: "
            "internal-support | **Package**: baldur | **Module**: `services/ml_models/`\n"
        )
        entry = parse_catalog_entries(body)[0]
        assert entry.is_product_entry
        assert not entry.is_sold_product_feature


class TestAxisCompleteness:
    """(d) — a mistyped axis label/value FAILs rather than parse-to-empty-and-skip."""

    def test_complete_entry_passes(self):
        entry = parse_catalog_entries(_GOOD_ENTRY)[0]
        assert entry.product_status in PRODUCT_STATUS_VALUES
        assert entry.code_role in CODE_ROLE_VALUES
        assert entry.package in PACKAGE_VALUES

    def test_mistyped_label_is_caught(self):
        # `**Prodct Status**` (typo) → the real label is absent → parses to None,
        # but `is_product_entry` is still True (Module present), so the
        # completeness check fires.
        bad = _GOOD_ENTRY.replace("**Product Status**", "**Prodct Status**")
        entry = parse_catalog_entries(bad)[0]
        assert entry.is_product_entry
        assert entry.product_status is None  # would be silently skipped if not for (d)
        assert entry.product_status not in PRODUCT_STATUS_VALUES

    def test_mistyped_value_is_caught(self):
        bad = _GOOD_ENTRY.replace("Package**: baldur_pro", "Package**: baldur_prooo")
        entry = parse_catalog_entries(bad)[0]
        assert entry.package == "baldur_prooo"
        assert entry.package not in PACKAGE_VALUES

    def test_missing_one_axis_is_caught(self):
        # Drop the Code Role field entirely but keep Module → product entry,
        # incomplete.
        bad = (
            "### Widget\n\n"
            "- **Product Status**: OSS | **Package**: baldur | "
            "**Module**: `services/dlq/`\n"
        )
        entry = parse_catalog_entries(bad)[0]
        assert entry.is_product_entry
        assert entry.code_role is None


class TestEnvTokenResolution:
    """(b) — cited-token resolution, including the wildcard-prefix arm."""

    def test_extracts_tokens_from_inline_code_only(self):
        text = "Set `BALDUR_REDIS_URL` and see BALDUR_NOT_IN_CODE in prose.\n"
        assert cited_baldur_tokens(text) == ["BALDUR_REDIS_URL"]

    def test_strips_trailing_value_example(self):
        assert cited_baldur_tokens("`BALDUR_ADMIN_UNLOCK=1`") == ["BALDUR_ADMIN_UNLOCK"]

    def test_real_field_var_resolves(self):
        index = build_prefix_index()
        assert env_token_resolves("BALDUR_ADMIN_BIND", index) is True

    def test_real_direct_read_var_resolves(self):
        index = build_prefix_index()
        assert env_token_resolves("BALDUR_REDIS_URL", index) is True

    def test_wildcard_prefix_resolves(self):
        index = build_prefix_index()
        assert env_token_resolves("BALDUR_META_WATCHDOG_*", index) is True

    def test_phantom_var_does_not_resolve(self):
        index = build_prefix_index()
        assert env_token_resolves("BALDUR_TOTALLY_MADE_UP_FIELD", index) is False

    def test_phantom_wildcard_prefix_does_not_resolve(self):
        index = build_prefix_index()
        assert env_token_resolves("BALDUR_NOPE_PREFIX_*", index) is False


class TestModulePathStatus:
    """(c) — path existence with OSS-only-checkout skip semantics."""

    def test_existing_baldur_path_ok(self):
        assert catalog_module_path_status("services/circuit_breaker/") == "ok"

    def test_existing_pro_path_ok(self):
        # baldur_pro is installed in the private-repo checkout → "ok". In the public
        # OSS repo baldur_pro is physically absent, so the helper conservatively
        # returns "skip" (a private-tier path is unverifiable there). The test
        # tracks the helper's documented OSS-only-checkout semantics on both
        # layouts rather than vacuous-passing or hard-failing in the public repo.
        pro_present = CATALOG_SRC_ROOTS["baldur_pro"].exists()
        assert catalog_module_path_status("services/dlq/") == (
            "ok" if pro_present else "skip"
        )

    def test_missing_path_is_missing(self):
        # The stale path 589 fixes — absent everywhere. "missing" is only
        # determinable when a private root is present (so absence-everywhere can be
        # confirmed); in the public OSS repo both private roots are absent and the
        # helper returns "skip" instead.
        any_private_present = any(
            CATALOG_SRC_ROOTS[k].exists() for k in ("baldur_pro", "baldur_dormant")
        )
        assert catalog_module_path_status("resilience/bulkhead/") == (
            "missing" if any_private_present else "skip"
        )


class TestG37CatalogAbsentSkip:
    """663 D4 — every live catalog read site skips when FEATURE_CATALOG.md is absent.

    The catalog is a private-repo-only artifact (not published to the public repo); G37 is fully
    covered by the private-repo run. The in-body skip (not a module-level ``skipif``)
    is what keeps this monkeypatch-testable and the synthetic anti-vacuous fixtures
    running in the public repo. ``_entries()`` is the chokepoint for two methods; the
    other two read the catalog directly, so all three guard sites are checked.
    """

    _MOD = "tests.architecture.test_catalog_tier_drift"

    def _absent_catalog(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{self._MOD}.CATALOG_PATH", tmp_path / "no_catalog.md")

    def test_entries_skips_when_catalog_absent(self, monkeypatch, tmp_path):
        self._absent_catalog(monkeypatch, tmp_path)
        with pytest.raises(pytest.skip.Exception):
            TestCatalogTierDrift()._entries()

    def test_no_legacy_env_prefix_skips_when_catalog_absent(
        self, monkeypatch, tmp_path
    ):
        self._absent_catalog(monkeypatch, tmp_path)
        with pytest.raises(pytest.skip.Exception):
            TestCatalogTierDrift().test_no_legacy_env_prefix()

    def test_cited_env_vars_resolve_skips_when_catalog_absent(
        self, monkeypatch, tmp_path
    ):
        self._absent_catalog(monkeypatch, tmp_path)
        with pytest.raises(pytest.skip.Exception):
            TestCatalogTierDrift().test_cited_env_vars_resolve()
