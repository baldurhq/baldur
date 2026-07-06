"""
Cross-service ImportError separation tests (doc 426, Phase B-MEDIUM).

Two verification strategies:
1. Runtime tests: For sites where the function can be easily called in isolation,
   mock sys.modules to simulate ImportError and verify graceful fallback.
2. AST structural tests: For sites where instantiation is complex, verify the
   code structure has separate ImportError/Exception handlers.

Pattern E from CROSS_SERVICE_STANDARDS:
  - ImportError → DEBUG log + graceful fallback
  - Exception → WARNING log + graceful fallback
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path("src/baldur")
PRO_SRC_ROOT = Path("src/baldur_pro")
DORMANT_SRC_ROOT = Path("src/baldur_dormant")


# ============================================================================
# AST Helper: verify ImportError separation in try blocks
# ============================================================================


def _find_importerror_separated_try_blocks(filepath: Path, import_target: str):
    """Find try blocks importing `import_target` and check ImportError separation.

    Returns:
        (total_matching_try_blocks, separated_count)
    """
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))

    total = 0
    separated = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue

        # Check if this try block imports from the target module
        has_target_import = False
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and child.module:
                if import_target in child.module:
                    has_target_import = True
                    break

        if not has_target_import:
            continue

        total += 1

        # Check handlers
        handler_types = set()
        for handler in node.handlers:
            if handler.type is None:
                handler_types.add("bare")
            elif isinstance(handler.type, ast.Name):
                handler_types.add(handler.type.id)
            elif isinstance(handler.type, ast.Tuple):
                for elt in handler.type.elts:
                    if isinstance(elt, ast.Name):
                        handler_types.add(elt.id)

        if "ImportError" in handler_types:
            separated += 1

    return total, separated


# ============================================================================
# chaos/base/experiment.py — D7 log message fix (AST contract test)
# ============================================================================


class TestChaosExperimentLearningLogMessageContract:
    """D7: chaos experiment learning ImportError log uses 'unavailable'."""

    def test_learning_importerror_log_says_unavailable(self):
        """'chaos.learningservice_unavailable' present, old 'available' removed."""
        src = PRO_SRC_ROOT / "services" / "chaos" / "base" / "experiment.py"
        if not src.exists():
            pytest.skip(
                "chaos/base/experiment.py is a baldur_pro module, absent on the "
                "OSS-only checkout (the mirror)"
            )
        tree = ast.parse(src.read_text(encoding="utf-8"))

        found_correct = False
        found_wrong = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value == "chaos.learningservice_unavailable":
                    found_correct = True
                if node.value == "chaos.learningservice_available":
                    found_wrong = True

        assert found_correct, (
            "Expected 'chaos.learningservice_unavailable' in experiment.py"
        )
        assert not found_wrong, "Old 'chaos.learningservice_available' still present"


# ============================================================================
# AST structural verification — ImportError separated from Exception
# ============================================================================


_IMPORTERROR_SEPARATION_SITES = [
    # (file_path, import_target, description)
    # compliance _base / DORA → chaos scheduler: removed by 519 PR 2 (c).
    # Both callsites now resolve the scheduler via
    # ``ProviderRegistry.chaos_scheduler.safe_get()``; the try/except is
    # gone and the original ImportError-separation contract no longer
    # applies. Mirrors the finops → audit deletion documented below.
    # compliance + correlation_engine relocated to baldur_dormant (599 D2);
    # the intra-cluster learning import is now dormant-qualified.
    (
        "services/compliance/standards/dora.py",
        "baldur_pro.services.postmortem",
        "DORA → postmortem",
    ),
    (
        "services/emergency_mode/manager.py",
        "baldur.services.config_history",
        "emergency mode → config_history",
    ),
    (
        "services/correlation_engine/service.py",
        "baldur_dormant.services.learning",
        "correlation engine → learning",
    ),
    (
        "services/chaos/base/experiment.py",
        "baldur.services.circuit_breaker",
        "chaos experiment → circuit breaker",
    ),
    # finops → audit: removed by 518 batch (a). finops/service.py no longer
    # carries a try/except around the audit import — it uses unconditional
    # `from baldur.audit.helpers import log_finops_audit`. The helper itself
    # owns the fail-open contract (PRO absence → silent no-op), so the
    # callsite no longer needs an ImportError handler. This deletion is
    # case-scoped; the other 7 sites still verify the 426 separation
    # contract.
    (
        "services/governance/api_service.py",
        "baldur.services.metric_sync_service",
        "governance API → metric_sync",
    ),
]


class TestImportErrorSeparationStructureContract:
    """Verify all 426-fixed sites have ImportError handler in try blocks."""

    @pytest.mark.parametrize(
        ("rel_path", "import_target", "desc"),
        _IMPORTERROR_SEPARATION_SITES,
        ids=[s[2] for s in _IMPORTERROR_SEPARATION_SITES],
    )
    def test_importerror_handler_exists(self, rel_path, import_target, desc):
        """Try block importing {import_target} has an ImportError handler."""
        filepath = SRC_ROOT / rel_path
        if not filepath.exists():
            filepath = PRO_SRC_ROOT / rel_path
        if not filepath.exists():
            filepath = DORMANT_SRC_ROOT / rel_path
        if not filepath.exists():
            # The site resolves to no present source root — its subject is a
            # PRO/Dormant module absent on the OSS-only checkout (the mirror).
            # OSS-resolvable sites still verify the 426 contract here.
            pytest.skip(
                f"{rel_path} resolves to no present source root (private tier absent)"
            )
        total, separated = _find_importerror_separated_try_blocks(
            filepath, import_target
        )
        assert total > 0, f"No try block importing {import_target} found in {rel_path}"
        assert separated == total, (
            f"{desc}: {total - separated}/{total} try blocks missing ImportError handler"
        )


class TestG4AbsentTierSkip:
    """663 D6 — a site resolving to no present source root is skipped, not failed.

    On the OSS-only checkout (the mirror) a PRO/Dormant param's source is absent;
    the case skips rather than raising ``FileNotFoundError`` at ``read_text()``.
    OSS-resolvable sites (``src/baldur/…``) still verify the 426 contract.
    """

    _MOD = "tests.unit.test_cross_service_importerror_426"

    def test_chaos_log_test_skips_when_pro_absent(self, monkeypatch, tmp_path):
        # experiment.py is a baldur_pro module; with the PRO root absent the
        # contract test skips instead of failing to read the file.
        monkeypatch.setattr(f"{self._MOD}.PRO_SRC_ROOT", tmp_path / "absent_pro")
        with pytest.raises(pytest.skip.Exception):
            TestChaosExperimentLearningLogMessageContract().test_learning_importerror_log_says_unavailable()

    def test_separation_skips_when_all_roots_absent(self, monkeypatch, tmp_path):
        absent = tmp_path / "absent"
        for name in ("SRC_ROOT", "PRO_SRC_ROOT", "DORMANT_SRC_ROOT"):
            monkeypatch.setattr(f"{self._MOD}.{name}", absent)
        with pytest.raises(pytest.skip.Exception):
            TestImportErrorSeparationStructureContract().test_importerror_handler_exists(
                "services/chaos/base/experiment.py",
                "baldur.services.circuit_breaker",
                "chaos experiment -> circuit breaker",
            )

    def test_oss_resolvable_site_verifies_contract(self, monkeypatch, tmp_path):
        # A synthetic OSS-resolvable file with a 426-compliant try block (every
        # real param resolves to a PRO/Dormant file, so a synthetic file is used
        # to exercise the "resolves -> verifies, no skip" branch). It resolves
        # under SRC_ROOT, so it is NOT skipped and the ImportError-separation
        # contract holds.
        pkg = tmp_path / "services" / "demo"
        pkg.mkdir(parents=True)
        (pkg / "mod.py").write_text(
            "try:\n"
            "    from baldur.services.target import X\n"
            "except ImportError:\n"
            "    X = None\n"
            "except Exception:\n"
            "    X = None\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(f"{self._MOD}.SRC_ROOT", tmp_path)
        TestImportErrorSeparationStructureContract().test_importerror_handler_exists(
            "services/demo/mod.py", "baldur.services.target", "demo synthetic"
        )  # must not raise: resolvable + ImportError separated
