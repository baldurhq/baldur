"""G21 — no tests/ TEST file may classify as pure_pro / support_only.

CLAUDE.md § Test Location Rules + impl doc 533 D12. The 533 ``git mv`` is
one-time: G19 guards only marker presence and G20 only the private-import axis.
Neither stops a *new* ``pure_pro`` / ``support_only`` test — one whose
production-import graph shows its system-under-test is PRO — from being added
to ``tests/`` and shipping at the next mirror build (the path-level
allowlist publishes ``tests/`` wholesale, so G2 recurs). G21 closes that
axis permanently.

Single source of truth (533 D12): G21 reuses the rewritten classifier's
per-file verdict function ``scripts.classify_pro_importing_tests.classify_file``
— there is no second copy to drift. The function is pure AST/string analysis
that never imports ``baldur_pro``, so it runs in the public mirror where
``scripts/`` is published wholesale and the PRO package source is absent.

Scope (matches the move plan): TEST files only (``test_*.py``). conftest.py /
__init__.py are directory infrastructure — a PRO-importing conftest that
legitimately serves staying boundary tests is acceptable public-API surface
(533 R2) and is checked for private leaks by G20, not relocated by G21.

G21 keys off ``pure_pro`` + ``support_only`` only, NOT ``pro_dominant``: the 8
named ``pro_dominant`` STAY boundary tests (533 D3) are never re-flagged, which
is why D3 needs no machine-readable allowlist file. A future edit that strips a
STAY file's OSS imports down to ``pure_pro`` SHOULD trip G21 — that is correct
behavior, not a false positive: the file would then genuinely belong in
``tests/pro/``.

ENFORCED-EMPTY baseline (533 D12, same rationale as G20): a baseline entry
would whitelist the leak it documents, so the second test method meta-asserts
the key stays empty.

Architectural fitness function rule registry:
``ARCHITECTURE.md#g21-oss-test-pro-classification``
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.classify_pro_importing_tests import (
    PURE_PRO,
    SUPPORT_ONLY,
    classify_file,
)
from tests.architecture import _helpers as arch_helpers
from tests.architecture.conftest import (
    OSS_TESTS_ROOT,
    collect_violations,
    load_baseline,
)

_RULE_KEY = "oss_test_pro_classification"
_RULE_ANCHOR = "#g21-oss-test-pro-classification"
_LEAKING_VERDICTS = (PURE_PRO, SUPPORT_ONLY)


def _walk_oss_test_files() -> Iterator[Path]:
    """Walk the OSS test root's ``test_*.py`` files, skipping ``__pycache__``."""
    root = OSS_TESTS_ROOT
    if not root.exists():
        return
    for path in root.rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


class TestOssTestProClassification:
    """G21 — no tests/ test file classifies as pure_pro / support_only."""

    def test_no_pure_pro_or_support_only_oss_tests(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _walk_oss_test_files():
            verdict = classify_file(path)
            if verdict is None:
                continue
            if verdict.verdict in _LEAKING_VERDICTS:
                raw.append(
                    (
                        path,
                        None,
                        None,
                        f"classifies as '{verdict.verdict}' "
                        f"(pro_imports={verdict.pro_count}, oss_imports={verdict.oss_count}) "
                        "— SUT is PRO, leaks at the mirror build",
                    )
                )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G21: tests/ contains {len(violations)} test file(s) whose "
            "import-graph SUT is PRO (pure_pro / support_only). These ship at "
            "the next mirror build. Move them to tests/pro/.\n" + "\n".join(violations)
        )

    def test_baseline_is_enforced_empty(self):
        # 533 D12: a G21 baseline entry would whitelist a pure_pro/support_only
        # test that stays under tests/ and ships at the mirror build — the
        # exact leak G21 prevents. Remediation is move, never baseline.
        assert load_baseline(_RULE_KEY) == {}, (
            f"G21: baseline key '{_RULE_KEY}' must stay empty (533 D12). A "
            "pure_pro/support_only test is moved to tests/pro/, never baselined."
        )


class TestG21ClassificationGate:
    """G21 keys off the classifier verdict: pure_pro/support_only fail, others pass (533 D12)."""

    @pytest.mark.parametrize(
        ("source", "leaks"),
        [
            pytest.param("from baldur_pro.x import Foo\n", True, id="pure_pro-fails"),
            pytest.param(
                "from baldur_pro.x import Foo\nfrom baldur.settings.base import S\n",
                True,
                id="support_only-fails",
            ),
            pytest.param(
                "from baldur_pro.a import A\nfrom baldur_pro.b import B\n"
                "from baldur.services.cb import CB\n",
                False,
                id="pro_dominant-passes",
            ),
            pytest.param(
                "from baldur_pro.a import A\nfrom baldur.services.cb import C\n"
                "from baldur.services.dlq import D\n",
                False,
                id="true_boundary-passes",
            ),
            pytest.param(
                "from baldur.services.cb import CB\n", False, id="oss-only-passes"
            ),
        ],
    )
    def test_gate_flags_only_pro_sut_verdicts(
        self, tmp_path: Path, source: str, leaks: bool
    ):
        # The gate's decision predicate is `verdict in _LEAKING_VERDICTS` — the
        # single source of truth reused from the classifier (533 D12).
        path = tmp_path / "test_x.py"
        path.write_text(source, encoding="utf-8")
        verdict = classify_file(path)
        assert verdict is not None
        assert (verdict.verdict in _LEAKING_VERDICTS) is leaks


class TestG21BaselineEnforcedEmpty:
    """The G21 meta-assertion goes red if the enforced-empty baseline key gains an entry (533 D12)."""

    def test_meta_assertion_fails_when_baseline_key_nonempty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: a baseline document with a non-empty G21 key
        path = tmp_path / "baseline.yaml"
        path.write_text(
            f"{_RULE_KEY}:\n"
            '  - {file: "tests/unit/leaky.py", reason: "x", ticket: "533"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(arch_helpers, "BASELINE_PATH", path)
        arch_helpers._load_baseline_document.cache_clear()
        try:
            # When/Then: an entry whitelists the leak G21 prevents (533 D12) → guard fails
            with pytest.raises(AssertionError):
                TestOssTestProClassification().test_baseline_is_enforced_empty()
        finally:
            arch_helpers._load_baseline_document.cache_clear()

    def test_meta_assertion_passes_when_baseline_key_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "baseline.yaml"
        path.write_text(f"{_RULE_KEY}: []\n", encoding="utf-8")
        monkeypatch.setattr(arch_helpers, "BASELINE_PATH", path)
        arch_helpers._load_baseline_document.cache_clear()
        try:
            TestOssTestProClassification().test_baseline_is_enforced_empty()
        finally:
            arch_helpers._load_baseline_document.cache_clear()
