"""
Feature Toggle Compliance Scanner (doc 426, Phase E).

10-dimension checklist per feature:
  1. Master toggle exists (settings field)
  2. Entry point enforcement (early return check)
  3. Cross-service callers check enabled state
  4. EventBus handlers check enabled state
  5. Fail behavior documented
  6. Fail behavior implemented (code matches doc)
  7. ImportError separated from Exception in cross-service calls (AST)
  8. Feature-disabled scenario test exists
  9. Startup path gated by settings
 10. Observability on toggle state change (startup log)

Output: per-feature gap matrix (CSV or JSON).

Usage:
    python scripts/scan_toggle_compliance.py [--json] [--csv]

Dependencies:
    - Python 3.12+ (ast module)
    - No Django/external packages required — pure static analysis
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ============================================================================
# Configuration
# ============================================================================

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "baldur"
TESTS_ROOT = Path(__file__).resolve().parent.parent / "tests"
DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"

# Feature definitions: (feature_name, settings_env_prefix, settings_module,
#                        service_dirs, toggle_field)
FEATURES: list[dict] = [
    {
        "name": "Circuit Breaker",
        "env_prefix": "BALDUR_CB_",
        "settings_module": "settings/circuit_breaker.py",
        "service_dirs": ["services/circuit_breaker"],
        "toggle_field": "enabled",
        "tier": "OSS",
    },
    {
        "name": "Retry + Backoff",
        "env_prefix": "BALDUR_RETRY_",
        "settings_module": "settings/retry.py",
        "service_dirs": ["services/retry_handler"],
        "toggle_field": "enabled",
        "tier": "OSS",
    },
    {
        "name": "Idempotency",
        "env_prefix": "BALDUR_IDEMPOTENCY_",
        "settings_module": "settings/idempotency.py",
        "service_dirs": ["resilience/policies"],
        "toggle_field": "enabled",
        "tier": "OSS",
    },
    {
        "name": "Metrics",
        "env_prefix": "BALDUR_METRICS_",
        "settings_module": "settings/metrics.py",
        "service_dirs": ["metrics"],
        "toggle_field": "enabled",
        "tier": "OSS",
    },
    {
        "name": "DLQ + Replay",
        "env_prefix": "BALDUR_DLQ_",
        "settings_module": "settings/dlq.py",
        "service_dirs": ["services/dlq"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Audit",
        "env_prefix": "BALDUR_AUDIT_",
        "settings_module": "settings/audit.py",
        "service_dirs": ["audit"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Bulkhead",
        "env_prefix": "BALDUR_ADMISSION_CONTROL_",
        "settings_module": "settings/admission_control.py",
        "service_dirs": ["api/django"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Hedging",
        "env_prefix": "BALDUR_HEDGING_",
        "settings_module": "settings/hedging.py",
        "service_dirs": ["resilience/policies", "core/hedging"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Chaos Engineering",
        "env_prefix": "BALDUR_CHAOS_",
        "settings_module": "settings/chaos.py",
        "service_dirs": ["services/chaos"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Error Budget Gate",
        "env_prefix": "BALDUR_ERROR_BUDGET_GATE_",
        "settings_module": "settings/error_budget_gate.py",
        "service_dirs": ["services/error_budget_gate"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Postmortem",
        "env_prefix": "BALDUR_POSTMORTEM_",
        "settings_module": "settings/postmortem.py",
        "service_dirs": ["services/postmortem"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Saga",
        "env_prefix": "BALDUR_SAGA_",
        "settings_module": "settings/saga.py",
        "service_dirs": ["services/saga"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Meta-Watchdog",
        "env_prefix": "BALDUR_META_WATCHDOG_",
        "settings_module": "settings/meta_watchdog.py",
        "service_dirs": ["meta"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Notification",
        "env_prefix": "BALDUR_NOTIFICATION_",
        "settings_module": "settings/notification.py",
        "service_dirs": ["services/notification"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Circuit Mesh",
        "env_prefix": "BALDUR_CIRCUIT_MESH_",
        "settings_module": "settings/circuit_mesh.py",
        "service_dirs": ["services/circuit_mesh"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Cell Topology",
        "env_prefix": "BALDUR_CELL_TOPOLOGY_",
        "settings_module": "settings/cell_topology.py",
        "service_dirs": ["multiregion"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Daily Report",
        "env_prefix": "BALDUR_DAILY_REPORT_",
        "settings_module": "settings/daily_report.py",
        "service_dirs": ["tasks", "services/daily_report"],
        "toggle_field": "enabled",
        "tier": "OSS",
    },
    {
        "name": "Pool Monitor",
        "env_prefix": "BALDUR_POOL_MONITOR_",
        "settings_module": "settings/pool_monitor.py",
        "service_dirs": ["core"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Auto-Tuning",
        "env_prefix": "BALDUR_AUTO_TUNING_",
        "settings_module": "settings/auto_tuning.py",
        "service_dirs": ["services/auto_tuning"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Scaling",
        "env_prefix": "BALDUR_SCALING_",
        "settings_module": "settings/scale.py",
        "service_dirs": ["scaling"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
    {
        "name": "Throttle",
        "env_prefix": "BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_",
        "settings_module": "settings/rate_limit_throttle_integration.py",
        "service_dirs": ["services/throttle"],
        "toggle_field": "enabled",
        "tier": "PRO",
    },
]


@dataclass
class DimensionResult:
    """Result for a single dimension check."""

    passed: bool
    details: str = ""


@dataclass
class FeatureResult:
    """10-dimension result for a single feature."""

    feature: str
    tier: str
    d1_toggle_exists: DimensionResult = field(
        default_factory=lambda: DimensionResult(False)
    )
    d2_entry_enforcement: DimensionResult = field(
        default_factory=lambda: DimensionResult(False)
    )
    d3_xs_caller_check: DimensionResult = field(
        default_factory=lambda: DimensionResult(False, "manual review needed")
    )
    d4_eb_handler_check: DimensionResult = field(
        default_factory=lambda: DimensionResult(False, "manual review needed")
    )
    d5_fail_documented: DimensionResult = field(
        default_factory=lambda: DimensionResult(False)
    )
    d6_fail_implemented: DimensionResult = field(
        default_factory=lambda: DimensionResult(False, "manual review needed")
    )
    d7_importerror_separated: DimensionResult = field(
        default_factory=lambda: DimensionResult(False)
    )
    d8_toggle_test_exists: DimensionResult = field(
        default_factory=lambda: DimensionResult(False)
    )
    d9_startup_gated: DimensionResult = field(
        default_factory=lambda: DimensionResult(False)
    )
    d10_observability: DimensionResult = field(
        default_factory=lambda: DimensionResult(False, "manual review needed")
    )

    @property
    def score(self) -> int:
        return sum(
            1
            for d in [
                self.d1_toggle_exists,
                self.d2_entry_enforcement,
                self.d3_xs_caller_check,
                self.d4_eb_handler_check,
                self.d5_fail_documented,
                self.d6_fail_implemented,
                self.d7_importerror_separated,
                self.d8_toggle_test_exists,
                self.d9_startup_gated,
                self.d10_observability,
            ]
            if d.passed
        )


# ============================================================================
# Dimension checks
# ============================================================================


def _file_contains(path: Path, pattern: str) -> bool:
    """Check if a file contains a regex pattern."""
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return bool(re.search(pattern, text))
    except Exception:
        return False


def _search_dir(base: Path, dirs: list[str], pattern: str) -> list[str]:
    """Search for pattern in multiple directories, return matching file paths."""
    matches = []
    for d in dirs:
        dirpath = base / d
        if not dirpath.exists():
            continue
        for pyfile in dirpath.rglob("*.py"):
            if _file_contains(pyfile, pattern):
                matches.append(str(pyfile.relative_to(base)))
    return matches


def check_d1_toggle_exists(feat: dict) -> DimensionResult:
    """D1: Master toggle exists in settings class."""
    settings_path = SRC_ROOT / feat["settings_module"]
    toggle = feat["toggle_field"]
    if _file_contains(settings_path, rf"{toggle}:\s*bool\s*=\s*Field\("):
        return DimensionResult(True, str(settings_path.name))
    return DimensionResult(
        False, f"No '{toggle}: bool = Field(...)' in {settings_path.name}"
    )


def check_d2_entry_enforcement(feat: dict) -> DimensionResult:
    """D2: Entry point has early return when disabled."""
    patterns = [
        r"if not .*\.enabled",
        r"if not .*_enabled",
        r"if not get_\w+_settings\(\)\.enabled",
    ]
    for pat in patterns:
        matches = _search_dir(SRC_ROOT, feat["service_dirs"], pat)
        if matches:
            return DimensionResult(True, f"Found in: {', '.join(matches[:3])}")
    return DimensionResult(False, "No entry point enforcement found")


def check_d5_fail_documented(feat: dict) -> DimensionResult:
    """D5: Fail behavior documented in CROSS_SERVICE_STANDARDS or feature doc."""
    xs_standards = DOCS_ROOT / "laws" / "CROSS_SERVICE_STANDARDS.md"
    if _file_contains(xs_standards, re.escape(feat["name"])):
        return DimensionResult(True, "Found in CROSS_SERVICE_STANDARDS.md")
    # Check feature docs
    features_dir = DOCS_ROOT / "features"
    if features_dir.exists():
        for doc in features_dir.glob("*.md"):
            if _file_contains(doc, r"fail.?(open|closed)"):
                if _file_contains(doc, re.escape(feat["name"])):
                    return DimensionResult(True, f"Found in {doc.name}")
    return DimensionResult(False, "No fail behavior documentation found")


def check_d7_importerror_ast(feat: dict) -> DimensionResult:
    """D7: ImportError separated from Exception (AST analysis)."""
    total_try = 0
    separated = 0
    unseparated_files = []

    for d in feat["service_dirs"]:
        dirpath = SRC_ROOT / d
        if not dirpath.exists():
            continue
        for pyfile in dirpath.rglob("*.py"):
            try:
                source = pyfile.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(pyfile))
            except (SyntaxError, UnicodeDecodeError):
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                # Check if body has any import statements (from X import Y)
                has_import = False
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        has_import = True
                        break
                if not has_import:
                    continue

                total_try += 1
                has_importerror = False
                has_exception = False

                for handler in node.handlers:
                    if handler.type is None:
                        has_exception = True
                        continue
                    name = ""
                    if isinstance(handler.type, ast.Name):
                        name = handler.type.id
                    elif isinstance(handler.type, ast.Tuple):
                        name = ",".join(
                            e.id for e in handler.type.elts if isinstance(e, ast.Name)
                        )
                    if "ImportError" in name:
                        has_importerror = True
                    if "Exception" in name:
                        has_exception = True

                if has_exception and not has_importerror:
                    rel = str(pyfile.relative_to(SRC_ROOT))
                    if rel not in unseparated_files:
                        unseparated_files.append(rel)
                elif has_importerror:
                    separated += 1

    if total_try == 0:
        return DimensionResult(True, "No cross-service imports in try blocks")
    if not unseparated_files:
        return DimensionResult(True, f"{separated}/{total_try} blocks separated")
    return DimensionResult(
        False,
        f"{len(unseparated_files)} unseparated: {', '.join(unseparated_files[:3])}",
    )


def check_d8_toggle_test(feat: dict) -> DimensionResult:
    """D8: Feature-disabled scenario test exists."""
    test_dir = TESTS_ROOT / "unit"
    name_lower = (
        feat["name"].lower().replace(" ", "_").replace("+", "").replace("-", "_")
    )
    # Check matrix test file
    matrix_path = test_dir / "test_feature_toggle_matrix.py"
    if matrix_path.exists():
        env_prefix = feat["env_prefix"]
        if _file_contains(matrix_path, re.escape(env_prefix)):
            return DimensionResult(True, "Covered in test_feature_toggle_matrix.py")
    # Check per-feature test files
    for pyfile in test_dir.rglob("*.py"):
        if _file_contains(pyfile, rf"(disabled|toggle|enabled).*{name_lower}"):
            return DimensionResult(True, f"Found in {pyfile.name}")
    return DimensionResult(False, "No toggle-disabled test found")


def check_d9_startup_gated(feat: dict) -> DimensionResult:
    """D9: Startup path gated by settings."""
    apps_path = SRC_ROOT / "adapters" / "django" / "apps.py"
    name_lower = feat["name"].lower().replace(" ", "_")
    patterns = [
        feat["env_prefix"].lower().replace("baldur_", "").rstrip("_"),
        rf"get_{name_lower}_settings",
    ]
    for pat in patterns:
        if _file_contains(apps_path, pat):
            return DimensionResult(True, "Found in apps.py")
    # Some features gate internally in __init__ or start()
    for d in feat["service_dirs"]:
        dirpath = SRC_ROOT / d
        if not dirpath.exists():
            continue
        for pyfile in dirpath.rglob("*.py"):
            if _file_contains(pyfile, r"def start\("):
                if _file_contains(pyfile, r"\.enabled"):
                    return DimensionResult(True, f"self-gated in {pyfile.name}")
    return DimensionResult(False, "No startup gating found")


# ============================================================================
# Main scan
# ============================================================================


def scan_all() -> list[FeatureResult]:
    """Run all dimension checks for all features."""
    results = []
    for feat in FEATURES:
        r = FeatureResult(feature=feat["name"], tier=feat["tier"])
        r.d1_toggle_exists = check_d1_toggle_exists(feat)
        r.d2_entry_enforcement = check_d2_entry_enforcement(feat)
        r.d5_fail_documented = check_d5_fail_documented(feat)
        r.d7_importerror_separated = check_d7_importerror_ast(feat)
        r.d8_toggle_test_exists = check_d8_toggle_test(feat)
        r.d9_startup_gated = check_d9_startup_gated(feat)
        results.append(r)
    return results


def print_table(results: list[FeatureResult]) -> None:
    """Print a concise compliance matrix table."""
    header = f"{'Feature':<22} {'Tier':<4} D1 D2 D3 D4 D5 D6 D7 D8 D9 D10 Score"
    print(header)
    print("-" * len(header))
    for r in results:
        dims = [
            r.d1_toggle_exists,
            r.d2_entry_enforcement,
            r.d3_xs_caller_check,
            r.d4_eb_handler_check,
            r.d5_fail_documented,
            r.d6_fail_implemented,
            r.d7_importerror_separated,
            r.d8_toggle_test_exists,
            r.d9_startup_gated,
            r.d10_observability,
        ]
        marks = " ".join(
            " Y" if d.passed else " N" if d.details != "manual review needed" else " ?"
            for d in dims
        )
        print(f"{r.feature:<22} {r.tier:<4} {marks} {r.score:>3}/10")

    total = sum(r.score for r in results)
    max_total = len(results) * 10
    print(f"\nTotal: {total}/{max_total} ({total / max_total * 100:.0f}%)")


def to_json(results: list[FeatureResult]) -> str:
    """Export results as JSON."""
    data = []
    for r in results:
        entry = {"feature": r.feature, "tier": r.tier, "score": r.score}
        for i, dim in enumerate(
            [
                r.d1_toggle_exists,
                r.d2_entry_enforcement,
                r.d3_xs_caller_check,
                r.d4_eb_handler_check,
                r.d5_fail_documented,
                r.d6_fail_implemented,
                r.d7_importerror_separated,
                r.d8_toggle_test_exists,
                r.d9_startup_gated,
                r.d10_observability,
            ],
            1,
        ):
            entry[f"d{i}"] = {"passed": dim.passed, "details": dim.details}
        data.append(entry)
    return json.dumps(data, indent=2, ensure_ascii=False)


def to_csv(results: list[FeatureResult]) -> str:
    """Export results as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Feature",
            "Tier",
            "D1_Toggle",
            "D2_Entry",
            "D3_XSCall",
            "D4_EB",
            "D5_FailDoc",
            "D6_FailImpl",
            "D7_ImportErr",
            "D8_Test",
            "D9_Startup",
            "D10_Obs",
            "Score",
        ]
    )
    for r in results:
        writer.writerow(
            [
                r.feature,
                r.tier,
                r.d1_toggle_exists.passed,
                r.d2_entry_enforcement.passed,
                r.d3_xs_caller_check.passed,
                r.d4_eb_handler_check.passed,
                r.d5_fail_documented.passed,
                r.d6_fail_implemented.passed,
                r.d7_importerror_separated.passed,
                r.d8_toggle_test_exists.passed,
                r.d9_startup_gated.passed,
                r.d10_observability.passed,
                r.score,
            ]
        )
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description="Feature Toggle Compliance Scanner")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--csv", action="store_true", help="Output as CSV")
    args = parser.parse_args()

    results = scan_all()

    if args.json:
        print(to_json(results))
    elif args.csv:
        print(to_csv(results))
    else:
        print_table(results)


if __name__ == "__main__":
    main()
