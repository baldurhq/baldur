"""
Service Wiring Verification Script.

Statically verifies that every baldur service directory is reachable
from at least one entrypoint (middleware, celery task, appconfig, signal,
API view, management command) or is explicitly allowlisted.

Usage:
    python scripts/verify_wiring.py [--verbose] [--json] [--fix-suggestions]

Exit Codes:
    0 — All services wired or allowlisted
    1 — Orphan services detected

Dependencies:
    - Python 3.12+ (ast module)
    - pyyaml (allowlist parsing)
    - Django import unnecessary — pure static analysis
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BALDUR_ROOT = PROJECT_ROOT / "src" / "baldur"
SERVICES_DIR = BALDUR_ROOT / "services"

IGNORE_DIRS = {"event_bus", "factory", "__pycache__"}

ENTRY_POINT_PATHS: list[str] = [
    # Middleware
    "api/django/middleware/",
    "api/django/tiering/",
    "api/django/rate_limit.py",
    "api/django/pool_circuit_breaker.py",
    "api/django/audit_middleware.py",
    "api/django/cell/middleware.py",
    # Celery Tasks
    "celery_tasks/",
    "tasks/",
    "adapters/celery/tasks/",
    # AppConfig / Bootstrap
    "adapters/django/apps.py",
    # Signals
    "adapters/django/signal_hooks.py",
    "adapters/celery/signal_hooks.py",
    # API Views
    "api/django/views/",
    # API Handlers (framework-agnostic, invoked by views after 432 refactor)
    "api/handlers/",
    # Management Commands
    "adapters/django/management/commands/",
    # Factory
    "factory.py",
]

HOST_ENTRY_POINT_PATHS: list[str] = [
    "myproject/celery.py",
    "myproject/settings/",
]

MIDDLEWARE_SETTINGS_PATH = PROJECT_ROOT / "myproject" / "settings" / "base.py"
ALLOWLIST_PATH = PROJECT_ROOT / "scripts" / "wiring_allowlist.yaml"

SUBSCRIBE_PATTERN = re.compile(r"\.subscribe\(\s*EventType\.")


# ---------------------------------------------------------------------------
# Phase 1: Service directory scan
# ---------------------------------------------------------------------------


def discover_services() -> list[str]:
    """Return sorted list of service directory names under services/."""
    if not SERVICES_DIR.is_dir():
        return []
    return sorted(
        d.name
        for d in SERVICES_DIR.iterdir()
        if d.is_dir() and d.name not in IGNORE_DIRS
    )


# ---------------------------------------------------------------------------
# Phase 2: AST-based entrypoint import scan (§3.6)
# ---------------------------------------------------------------------------


def extract_service_refs(file_path: Path) -> set[str]:
    """Extract service references from a Python file via AST 2-pass hybrid."""
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return set()

    refs: set[str] = set()

    _SERVICE_PREFIXES = ("baldur.services.", "baldur_pro.services.")

    for node in ast.walk(tree):
        # Pass 1: direct imports (multiline, alias, conditional, lazy — all caught)
        if isinstance(node, ast.ImportFrom) and node.module:
            for prefix in _SERVICE_PREFIXES:
                if node.module.startswith(prefix):
                    parts = node.module[len(prefix) :].split(".")
                    if parts[0]:
                        refs.add(parts[0])
                    break
            if node.module in ("baldur.services", "baldur_pro.services") and node.names:
                for alias in node.names:
                    refs.add(alias.name)

        # Pass 2: string literal mining (Celery task names, importlib paths)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            for prefix in _SERVICE_PREFIXES:
                if prefix in value:
                    tail = value.split(prefix)[1].split(".")
                    if tail[0]:
                        refs.add(tail[0])
                    break

    return refs


def _collect_python_files(path: Path) -> list[Path]:
    """Collect .py files from a path (file or directory)."""
    if path.is_file() and path.suffix == ".py":
        return [path]
    if path.is_dir():
        return list(path.rglob("*.py"))
    return []


def scan_entrypoints() -> dict[str, set[str]]:
    """Scan all entrypoint paths and return {service_name: set_of_entrypoint_types}."""
    result: dict[str, set[str]] = {}

    for rel_path in ENTRY_POINT_PATHS:
        full_path = BALDUR_ROOT / rel_path
        ep_type = _classify_entrypoint(rel_path)
        for py_file in _collect_python_files(full_path):
            for svc in extract_service_refs(py_file):
                result.setdefault(svc, set()).add(ep_type)

    for rel_path in HOST_ENTRY_POINT_PATHS:
        full_path = PROJECT_ROOT / rel_path
        ep_type = _classify_entrypoint(rel_path)
        for py_file in _collect_python_files(full_path):
            for svc in extract_service_refs(py_file):
                result.setdefault(svc, set()).add(ep_type)

    return result


def _classify_entrypoint(rel_path: str) -> str:
    """Classify an entrypoint path into a human-readable type."""
    if (
        "middleware" in rel_path
        or "tiering" in rel_path
        or "rate_limit" in rel_path
        or "pool_circuit_breaker" in rel_path
        or "audit_middleware" in rel_path
        or "cell/" in rel_path
    ):
        return "middleware"
    if "celery_tasks" in rel_path or "tasks/" in rel_path:
        return "celery_task"
    if "apps.py" in rel_path:
        return "appconfig"
    if "signal_hooks" in rel_path:
        return "signal"
    if "views" in rel_path:
        return "view"
    if "api/handlers" in rel_path:
        return "handler"
    if "commands" in rel_path:
        return "command"
    if "factory" in rel_path:
        return "factory"
    if "celery.py" in rel_path:
        return "celery_beat"
    if "settings" in rel_path:
        return "settings"
    return "other"


# ---------------------------------------------------------------------------
# Phase 2.5: Django MIDDLEWARE string array scan (§3.7)
# ---------------------------------------------------------------------------


def _dotted_to_file_path(dotted_path: str) -> Path | None:
    """Convert a dotted middleware path to a filesystem path.

    e.g. 'baldur.api.django.middleware.BaldurMiddleware'
    → packages/baldur-python/src/baldur/api/django/middleware/baldur.py
    (via directory search, not name assumption)
    """
    if not dotted_path.startswith("baldur."):
        return None

    parts = dotted_path.split(".")
    # The last part is the class name — try module path first
    module_parts = parts[:-1]  # drop class name
    rel = "/".join(module_parts[1:])  # drop 'baldur' prefix

    candidate = BALDUR_ROOT / f"{rel}.py"
    if candidate.exists():
        return candidate

    # Maybe the class IS the module (e.g. trace_id_middleware is a function)
    rel_full = "/".join(parts[1:])
    candidate_full = BALDUR_ROOT / f"{rel_full}.py"
    if candidate_full.exists():
        return candidate_full

    # Try as directory __init__
    candidate_dir = BALDUR_ROOT / rel / "__init__.py"
    if candidate_dir.exists():
        return candidate_dir

    return None


def scan_middleware_wiring() -> dict[str, set[str]]:
    """Extract indirect service refs from MIDDLEWARE string array (2-hop)."""
    if not MIDDLEWARE_SETTINGS_PATH.exists():
        return {}

    try:
        tree = ast.parse(MIDDLEWARE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return {}

    middleware_paths: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", "") == "MIDDLEWARE":
                    if isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(
                                elt.value, str
                            ):
                                if "baldur." in elt.value:
                                    middleware_paths.append(elt.value)

    result: dict[str, set[str]] = {}
    for dotted_path in middleware_paths:
        file_path = _dotted_to_file_path(dotted_path)
        if file_path and file_path.exists():
            refs = extract_service_refs(file_path)
            if refs:
                result[dotted_path] = refs

    return result


# ---------------------------------------------------------------------------
# Phase 3: Indirect connection scan
# ---------------------------------------------------------------------------


def scan_indirect_connections(
    already_connected: set[str],
    all_services: list[str],
) -> dict[str, set[str]]:
    """Find services indirectly connected (imported by already-connected services)."""
    result: dict[str, set[str]] = {}

    for connected_svc in already_connected:
        svc_dir = SERVICES_DIR / connected_svc
        if not svc_dir.is_dir():
            continue
        for py_file in svc_dir.rglob("*.py"):
            refs = extract_service_refs(py_file)
            for ref in refs:
                if ref in all_services and ref not in already_connected:
                    result.setdefault(ref, set()).add(f"via {connected_svc}")

    return result


def scan_eventbus_subscriptions(all_services: list[str]) -> set[str]:
    """Detect services with EventBus.subscribe() calls (§3.8)."""
    result: set[str] = set()
    for svc_name in all_services:
        svc_dir = SERVICES_DIR / svc_name
        if not svc_dir.is_dir():
            continue
        if _has_eventbus_subscription(svc_dir):
            result.add(svc_name)
    return result


def _has_eventbus_subscription(service_dir: Path) -> bool:
    """Detect EventBus subscribe calls in service directory."""
    for py_file in service_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if SUBSCRIBE_PATTERN.search(content):
            return True
    return False


# ---------------------------------------------------------------------------
# Phase 4: Allowlist
# ---------------------------------------------------------------------------


def load_allowlist() -> tuple[set[str], list[str], dict[str, str]]:
    """Load wiring_allowlist.yaml.

    Returns (allowlisted_names, on_demand_task_patterns, reasons).
    """
    if not ALLOWLIST_PATH.exists():
        return set(), [], {}
    if yaml is None:
        print("WARNING: pyyaml not installed, skipping allowlist", file=sys.stderr)
        return set(), [], {}

    with open(ALLOWLIST_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    names: set[str] = set()
    reasons: dict[str, str] = {}
    for entry in data.get("allowlist", []):
        if isinstance(entry, dict) and "name" in entry:
            names.add(entry["name"])
            reasons[entry["name"]] = entry.get("reason", "")

    on_demand: list[str] = []
    for entry in data.get("on_demand_tasks", []):
        if isinstance(entry, dict) and "name" in entry:
            on_demand.append(entry["name"])

    return names, on_demand, reasons


# ---------------------------------------------------------------------------
# §6.1: Celery Task registration verification
# ---------------------------------------------------------------------------


def scan_celery_beat_tasks() -> set[str]:
    """Extract task names from CELERY_BEAT_SCHEDULE in myproject/celery.py."""
    celery_path = PROJECT_ROOT / "myproject" / "celery.py"
    if not celery_path.exists():
        return set()

    try:
        tree = ast.parse(celery_path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()

    tasks: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            if val.startswith("baldur."):
                tasks.add(val)

    # Also mine beat_schedule.py _SCHEDULE_MODULES string literals
    beat_path = BALDUR_ROOT / "adapters" / "celery" / "beat_schedule.py"
    if beat_path.exists():
        try:
            tree2 = ast.parse(beat_path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            return tasks
        for node in ast.walk(tree2):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                if val.startswith("baldur."):
                    tasks.add(val)

    return tasks


def scan_shared_tasks() -> dict[str, str]:
    """Find all @shared_task definitions and return {task_name: file_path}."""
    result: dict[str, str] = {}
    search_dirs = [
        BALDUR_ROOT / "celery_tasks",
        BALDUR_ROOT / "tasks",
        BALDUR_ROOT / "adapters" / "celery" / "tasks",
    ]
    # Also scan services/*/tasks.py
    if SERVICES_DIR.is_dir():
        for svc_dir in SERVICES_DIR.iterdir():
            if svc_dir.is_dir():
                tasks_file = svc_dir / "tasks.py"
                if tasks_file.exists():
                    search_dirs.append(tasks_file)

    for search_path in search_dirs:
        for py_file in _collect_python_files(search_path):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    # Look for @shared_task(name="...") or @app.task(name="...")
                    for kw in getattr(node, "keywords", []):
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                            result[kw.value.value] = str(
                                py_file.relative_to(PROJECT_ROOT)
                            )

    return result


def verify_celery_tasks(on_demand_patterns: list[str]) -> dict[str, list[dict]]:
    """Cross-verify shared_task definitions against Beat schedule."""
    beat_tasks = scan_celery_beat_tasks()
    shared = scan_shared_tasks()

    periodic: list[dict] = []
    on_demand: list[dict] = []

    for task_name, file_path in sorted(shared.items()):
        if not task_name.startswith("baldur."):
            continue
        is_beat = task_name in beat_tasks
        is_od_allow = any(
            task_name == pat or (pat.endswith(".*") and task_name.startswith(pat[:-1]))
            for pat in on_demand_patterns
        )
        if is_beat:
            periodic.append({"name": task_name, "file": file_path})
        elif is_od_allow:
            on_demand.append(
                {"name": task_name, "file": file_path, "allowlisted": True}
            )
        else:
            on_demand.append(
                {"name": task_name, "file": file_path, "allowlisted": False}
            )

    return {"periodic": periodic, "on_demand": on_demand}


# ---------------------------------------------------------------------------
# §6.2: ServiceDependencyGraph init order verification
# ---------------------------------------------------------------------------


def verify_dependency_graph(orphan_services: list[str]) -> list[str]:
    """Verify that orphan services are registered in the dependency graph."""
    apps_path = BALDUR_ROOT / "adapters" / "django" / "apps.py"
    if not apps_path.exists():
        return ["apps.py not found — cannot verify dependency graph"]

    try:
        tree = ast.parse(apps_path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return ["apps.py parse error"]

    # Extract register_service() calls
    registered: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "register_service":
                if node.args and isinstance(node.args[0], ast.Constant):
                    registered.add(node.args[0].value)

    # Extract topological_sort_subset services list
    subset_services: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "topological_sort_subset"
            ):
                for kw in node.keywords:
                    if kw.arg == "services" and isinstance(kw.value, ast.List):
                        for elt in kw.value.elts:
                            if isinstance(elt, ast.Constant):
                                subset_services.add(elt.value)

    warnings: list[str] = []
    for svc in orphan_services:
        if svc not in registered and svc not in subset_services:
            warnings.append(
                f"Orphan '{svc}' not in ServiceDependencyGraph — "
                f"consider adding to _initialize_orphan_services()"
            )

    return warnings


# ---------------------------------------------------------------------------
# §6.3: Feature Flag existence verification
# ---------------------------------------------------------------------------


def verify_feature_flags(service_names: list[str]) -> list[str]:
    """Verify each service has a corresponding settings file with env_prefix."""
    settings_dir = BALDUR_ROOT / "settings"
    if not settings_dir.is_dir():
        return service_names

    missing: list[str] = []
    for name in service_names:
        expected_prefix = f"BALDUR_{name.upper()}_"
        found = False
        for py_file in settings_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if f'env_prefix="{expected_prefix}"' in content:
                found = True
                break
            if f"env_prefix='{expected_prefix}'" in content:
                found = True
                break
        if not found:
            missing.append(name)

    return missing


# ---------------------------------------------------------------------------
# Phase 5: Report generation
# ---------------------------------------------------------------------------


class WiringReport:
    """Aggregated wiring verification results."""

    def __init__(self):
        self.connected: dict[str, set[str]] = {}
        self.indirect: dict[str, set[str]] = {}
        self.allowlisted: dict[str, str] = {}  # name → reason
        self.orphans: list[str] = []
        self.eventbus_subscribers: set[str] = set()
        self.middleware_wiring: dict[str, set[str]] = {}
        self.celery_tasks: dict[str, list[dict]] = {}
        self.dep_graph_warnings: list[str] = []
        self.missing_feature_flags: list[str] = []
        self.total_services: int = 0

    def print_verbose(self) -> None:
        print("\n=== Service Wiring Verification ===\n")

        print(f"CONNECTED ({len(self.connected)}):")
        for svc in sorted(self.connected):
            eps = ", ".join(sorted(self.connected[svc]))
            print(f"  \u2713 {svc:<30} \u2192 {eps}")

        print(f"\nINDIRECTLY CONNECTED ({len(self.indirect)}):")
        for svc in sorted(self.indirect):
            sources = ", ".join(sorted(self.indirect[svc]))
            print(f"  ~ {svc:<30} \u2192 {sources}")

        print(f"\nEVENTBUS SUBSCRIBERS ({len(self.eventbus_subscribers)}):")
        for svc in sorted(self.eventbus_subscribers):
            print(f"  \u266b {svc:<30} \u2192 EventBus.subscribe()")

        print(f"\nALLOWLISTED ({len(self.allowlisted)}):")
        for svc, reason in sorted(self.allowlisted.items()):
            print(f'  \u25cb {svc:<30} \u2192 "{reason}"')

        if self.orphans:
            print(f"\nORPHAN ({len(self.orphans)}):")
            for svc in sorted(self.orphans):
                print(f"  \u2717 {svc:<30} \u2192 NO ENTRY POINT FOUND")
        else:
            print("\nORPHAN (0): None")

        if self.middleware_wiring:
            print("\nMIDDLEWARE 2-HOP WIRING:")
            for mw, svcs in sorted(self.middleware_wiring.items()):
                print(f"  {mw} \u2192 {', '.join(sorted(svcs))}")

        if self.dep_graph_warnings:
            print("\nDEPENDENCY GRAPH WARNINGS:")
            for w in self.dep_graph_warnings:
                print(f"  ! {w}")

        if self.missing_feature_flags:
            print(f"\nMISSING FEATURE FLAGS ({len(self.missing_feature_flags)}):")
            for svc in sorted(self.missing_feature_flags):
                print(f"  ? {svc} \u2192 BALDUR_{svc.upper()}_* not found in settings/")

        total_wired = (
            len(self.connected)
            + len(self.indirect)
            + len(self.allowlisted)
            + len(self.eventbus_subscribers)
        )
        status = "PASS" if not self.orphans else f"FAIL ({len(self.orphans)} orphans)"
        print(
            f"\n=== RESULT: {status} === ({total_wired}/{self.total_services} wired)\n"
        )

    def print_fix_suggestions(self) -> None:
        if not self.orphans:
            return
        print("\nFIX SUGGESTIONS:")
        for svc in sorted(self.orphans):
            print(f"\n  ORPHAN: {svc}")
            print("    \u251c\u2500\u2500 Not imported in any middleware")
            print("    \u251c\u2500\u2500 Not imported in any celery task")
            print("    \u251c\u2500\u2500 Not imported in AppConfig.ready()")
            print("    \u251c\u2500\u2500 Not imported in any signal hook")
            print("    \u251c\u2500\u2500 Not imported in any API view")
            print("    \u2514\u2500\u2500 Not in wiring_allowlist.yaml")
            print("    SUGGESTION: Add to one of:")
            print("      1. adapters/django/apps.py \u2192 ready() initialization")
            print("      2. celery_tasks/ \u2192 periodic task")
            print(
                "      3. scripts/wiring_allowlist.yaml \u2192 if intentionally unwired"
            )

    def to_json(self) -> dict:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_services": self.total_services,
            "connected": len(self.connected),
            "indirect": len(self.indirect),
            "eventbus_subscribers": len(self.eventbus_subscribers),
            "allowlisted": len(self.allowlisted),
            "orphan": len(self.orphans),
            "orphans": [
                {
                    "name": svc,
                    "path": f"services/{svc}/",
                    "suggested_entry_points": ["appconfig", "celery_beat"],
                }
                for svc in sorted(self.orphans)
            ],
            "middleware_wiring": {
                k: sorted(v) for k, v in self.middleware_wiring.items()
            },
            "dep_graph_warnings": self.dep_graph_warnings,
            "missing_feature_flags": self.missing_feature_flags,
            "pass": len(self.orphans) == 0,
        }


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def run_verification(
    verbose: bool = False, output_json: bool = False, fix_suggestions: bool = False
) -> int:
    report = WiringReport()

    # Phase 1: discover services
    all_services = discover_services()
    report.total_services = len(all_services)

    if verbose:
        print(f"Discovered {len(all_services)} service directories")

    # Phase 2: AST-based entrypoint scan
    ep_map = scan_entrypoints()
    report.connected = {svc: eps for svc, eps in ep_map.items() if svc in all_services}

    # Phase 2.5: MIDDLEWARE string array 2-hop
    mw_wiring = scan_middleware_wiring()
    report.middleware_wiring = mw_wiring
    for _mw_path, svc_refs in mw_wiring.items():
        for svc in svc_refs:
            if svc in all_services and svc not in report.connected:
                report.connected.setdefault(svc, set()).add("middleware (2-hop)")

    connected_names = set(report.connected.keys())

    # Phase 3: indirect connections
    indirect = scan_indirect_connections(connected_names, all_services)
    report.indirect = indirect

    # Phase 3 cont.: EventBus subscriptions
    eventbus_subs = scan_eventbus_subscriptions(all_services)
    report.eventbus_subscribers = eventbus_subs

    all_wired = connected_names | set(indirect.keys()) | eventbus_subs

    # Phase 4: allowlist
    allowlisted_names, on_demand_patterns, allowlist_reasons = load_allowlist()

    for svc in all_services:
        if svc in allowlisted_names and svc not in all_wired:
            report.allowlisted[svc] = allowlist_reasons.get(svc, "")

    all_accounted = all_wired | allowlisted_names

    # Determine orphans
    report.orphans = [svc for svc in all_services if svc not in all_accounted]

    # §6.1: Celery task verification
    report.celery_tasks = verify_celery_tasks(on_demand_patterns)

    # §6.2: Dependency graph verification
    report.dep_graph_warnings = verify_dependency_graph(report.orphans)

    # §6.3: Feature flag verification
    report.missing_feature_flags = verify_feature_flags(all_services)

    # Phase 5: output
    if output_json:
        report_data = report.to_json()
        print(json.dumps(report_data, indent=2, ensure_ascii=False))
        # Also write to file for CI artifact
        report_path = PROJECT_ROOT / "wiring_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)
    else:
        report.print_verbose()

    if fix_suggestions:
        report.print_fix_suggestions()

    return 1 if report.orphans else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Service Wiring Verification")
    parser.add_argument(
        "--verbose", action="store_true", help="Verbose terminal output"
    )
    parser.add_argument(
        "--json", action="store_true", dest="output_json", help="JSON output"
    )
    parser.add_argument(
        "--fix-suggestions",
        action="store_true",
        help="Show fix suggestions for orphans",
    )
    args = parser.parse_args()

    exit_code = run_verification(
        verbose=args.verbose or not args.output_json,
        output_json=args.output_json,
        fix_suggestions=args.fix_suggestions,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
