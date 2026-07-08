"""Unit tests for scripts/verify_wiring.py — Service Wiring Verification."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts/ to path so we can import verify_wiring as a module
SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verify_wiring  # noqa: E402

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def service_tree(tmp_path):
    """Create a minimal services/ directory structure for testing."""
    services = tmp_path / "services"
    services.mkdir()

    for name in ["circuit_breaker", "dlq", "audit", "saga", "orphan_svc"]:
        svc = services / name
        svc.mkdir()
        (svc / "__init__.py").write_text("", encoding="utf-8")

    # IGNORE_DIRS should be excluded
    (services / "__pycache__").mkdir()
    (services / "event_bus").mkdir()
    (services / "factory").mkdir()

    return services


@pytest.fixture
def py_file(tmp_path):
    """Helper to create a temporary Python file with given content."""

    def _create(content: str, name: str = "test_module.py") -> Path:
        f = tmp_path / name
        f.write_text(textwrap.dedent(content), encoding="utf-8")
        return f

    return _create


@pytest.fixture
def allowlist_file(tmp_path):
    """Create a temporary allowlist YAML file."""

    def _create(content: str) -> Path:
        f = tmp_path / "wiring_allowlist.yaml"
        f.write_text(textwrap.dedent(content), encoding="utf-8")
        return f

    return _create


@pytest.fixture
def settings_dir(tmp_path):
    """Create a minimal settings/ directory."""
    d = tmp_path / "settings"
    d.mkdir()
    return d


# =============================================================================
# Phase 1: discover_services
# =============================================================================


class TestDiscoverServicesBehavior:
    """Behavior tests for Phase 1 service directory scanning."""

    def test_discover_services_returns_sorted_names(self, service_tree):
        """Verify discovered service names are returned in sorted order."""
        # Given
        with patch.object(verify_wiring, "SERVICES_DIR", service_tree):
            # When
            result = verify_wiring.discover_services()

        # Then
        assert result == ["audit", "circuit_breaker", "dlq", "orphan_svc", "saga"]

    def test_discover_services_excludes_ignore_dirs(self, service_tree):
        """Verify IGNORE_DIRS entries are excluded from discovered services."""
        # Given
        with patch.object(verify_wiring, "SERVICES_DIR", service_tree):
            result = verify_wiring.discover_services()

        # Then — IGNORE_DIRS from source
        for ignored in verify_wiring.IGNORE_DIRS:
            assert ignored not in result

    def test_discover_services_empty_directory(self, tmp_path):
        """Verify empty services directory returns an empty list."""
        empty = tmp_path / "empty_services"
        empty.mkdir()

        with patch.object(verify_wiring, "SERVICES_DIR", empty):
            assert verify_wiring.discover_services() == []

    def test_discover_services_nonexistent_directory(self, tmp_path):
        """Verify nonexistent services directory returns an empty list."""
        with patch.object(verify_wiring, "SERVICES_DIR", tmp_path / "nonexistent"):
            assert verify_wiring.discover_services() == []

    def test_discover_services_skips_files(self, service_tree):
        """Verify regular files in the services directory are not listed as services."""
        # Given — a regular file among directories
        (service_tree / "not_a_dir.py").write_text("", encoding="utf-8")

        with patch.object(verify_wiring, "SERVICES_DIR", service_tree):
            result = verify_wiring.discover_services()

        assert "not_a_dir.py" not in result


# =============================================================================
# Phase 2: extract_service_refs — AST 2-pass hybrid
# =============================================================================


class TestExtractServiceRefsBehavior:
    """Behavior tests for AST-based service reference extraction."""

    def test_direct_import_from_services_submodule(self, py_file):
        """from baldur.services.circuit_breaker import X → circuit_breaker"""
        f = py_file("from baldur.services.circuit_breaker import CircuitBreakerService")
        assert "circuit_breaker" in verify_wiring.extract_service_refs(f)

    def test_import_from_services_package(self, py_file):
        """from baldur.services import dlq → dlq"""
        f = py_file("from baldur.services import dlq")
        assert "dlq" in verify_wiring.extract_service_refs(f)

    def test_multiline_parenthesized_import(self, py_file):
        """Verify parenthesized multi-line imports are correctly parsed."""
        f = py_file("""\
            from baldur.services.replay_service import (
                ReplayService,
                ReplayConfig,
            )
        """)
        assert "replay_service" in verify_wiring.extract_service_refs(f)

    def test_alias_import(self, py_file):
        """Verify aliased service imports are detected by original name."""
        f = py_file("from baldur.services import governance as gov")
        assert "governance" in verify_wiring.extract_service_refs(f)

    def test_lazy_import_inside_function(self, py_file):
        f = py_file("""\
            def _store_to_dlq():
                from baldur_pro.services.dlq import DLQService
                DLQService().store()
        """)
        assert "dlq" in verify_wiring.extract_service_refs(f)

    def test_type_checking_conditional_import(self, py_file):
        f = py_file("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from baldur.services.cell_topology import CellTopologyService
        """)
        assert "cell_topology" in verify_wiring.extract_service_refs(f)

    def test_string_literal_mining_celery_task(self, py_file):
        f = py_file("""\
            TASK_NAME = "baldur_pro.services.saga.tasks.resume"
        """)
        assert "saga" in verify_wiring.extract_service_refs(f)

    def test_string_literal_mining_importlib(self, py_file):
        f = py_file("""\
            import importlib
            mod = importlib.import_module("baldur.services.cleanup_service.runner")
        """)
        assert "cleanup_service" in verify_wiring.extract_service_refs(f)

    def test_no_false_positive_for_non_service_import(self, py_file):
        """Verify imports from non-services modules do not produce false positives."""
        f = py_file("from baldur.core.backoff import ExponentialBackoff")
        assert verify_wiring.extract_service_refs(f) == set()

    def test_syntax_error_returns_empty(self, py_file):
        """Verify files with syntax errors return an empty set."""
        f = py_file("def broken(:\n    pass")
        assert verify_wiring.extract_service_refs(f) == set()

    def test_nonexistent_file_returns_empty(self, tmp_path):
        """Verify nonexistent file path returns an empty set."""
        assert verify_wiring.extract_service_refs(tmp_path / "nope.py") == set()

    def test_empty_file_returns_empty(self, py_file):
        """Verify empty Python file returns an empty set."""
        f = py_file("")
        assert verify_wiring.extract_service_refs(f) == set()

    def test_deep_submodule_import(self, py_file):
        """from baldur_pro.services.error_budget.gate.service → error_budget"""
        f = py_file("from baldur_pro.services.error_budget.gate.service import Gate")
        assert "error_budget" in verify_wiring.extract_service_refs(f)

    def test_multiple_services_in_single_file(self, py_file):
        """Verify multiple service references in one file are all extracted."""
        f = py_file("""\
            from baldur.services.circuit_breaker import CB
            from baldur_pro.services.dlq import DLQ
            task = "baldur_pro.services.audit.flush"
        """)
        refs = verify_wiring.extract_service_refs(f)
        assert refs == {"circuit_breaker", "dlq", "audit"}


# =============================================================================
# Phase 2: _classify_entrypoint
# =============================================================================


class TestClassifyEntrypointContract:
    """Contract tests for entrypoint classification — all 9 types."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("api/django/middleware/baldur.py", "middleware"),
            ("api/django/tiering/middleware.py", "middleware"),
            ("api/django/rate_limit.py", "middleware"),
            ("api/django/pool_circuit_breaker.py", "middleware"),
            ("api/django/audit_middleware.py", "middleware"),
            ("api/django/cell/middleware.py", "middleware"),
            ("celery_tasks/metrics_tasks.py", "celery_task"),
            ("tasks/daily_report.py", "celery_task"),
            ("adapters/django/apps.py", "appconfig"),
            ("adapters/django/signal_hooks.py", "signal"),
            ("adapters/celery/signal_hooks.py", "signal"),
            ("api/django/views/circuit_breaker.py", "view"),
            ("adapters/django/management/commands/check.py", "command"),
            ("factory.py", "factory"),
            ("myproject/celery.py", "celery_beat"),
            ("myproject/settings/base.py", "settings"),
            ("something/unknown.py", "other"),
        ],
    )
    def test_classify_entrypoint_mapping(self, path, expected):
        """Verify each file path is classified to the correct entrypoint type."""
        assert verify_wiring._classify_entrypoint(path) == expected


# =============================================================================
# Phase 2.5: _dotted_to_file_path
# =============================================================================


class TestDottedToFilePathBehavior:
    """Behavior tests for dotted middleware path resolution."""

    def test_non_baldur_prefix_returns_none(self):
        """Verify non-baldur dotted paths return None."""
        assert verify_wiring._dotted_to_file_path("django.middleware.Foo") is None

    def test_module_path_resolution(self, tmp_path):
        """baldur.api.django.middleware.BaldurMiddleware → middleware.py"""
        # Given
        module_file = tmp_path / "api" / "django" / "middleware.py"
        module_file.parent.mkdir(parents=True)
        module_file.write_text("class BaldurMiddleware: pass", encoding="utf-8")

        with patch.object(verify_wiring, "BALDUR_ROOT", tmp_path):
            result = verify_wiring._dotted_to_file_path(
                "baldur.api.django.middleware.BaldurMiddleware"
            )

        assert result == module_file

    def test_init_resolution(self, tmp_path):
        """Falls back to __init__.py if module .py doesn't exist."""
        # Given
        pkg = tmp_path / "api" / "django" / "middleware"
        pkg.mkdir(parents=True)
        init_file = pkg / "__init__.py"
        init_file.write_text("class Foo: pass", encoding="utf-8")

        with patch.object(verify_wiring, "BALDUR_ROOT", tmp_path):
            result = verify_wiring._dotted_to_file_path(
                "baldur.api.django.middleware.Foo"
            )

        assert result == init_file

    def test_returns_none_for_missing_file(self, tmp_path):
        """Verify dotted path returns None when neither .py nor __init__.py exists."""
        with patch.object(verify_wiring, "BALDUR_ROOT", tmp_path):
            result = verify_wiring._dotted_to_file_path("baldur.missing.module.Class")
        assert result is None


# =============================================================================
# Phase 2.5: scan_middleware_wiring
# =============================================================================


class TestScanMiddlewareWiringBehavior:
    """Behavior tests for MIDDLEWARE string array 2-hop scan."""

    def test_extracts_baldur_middleware_paths(self, tmp_path):
        """Verify baldur middleware entries are extracted with their service refs."""
        # Given — settings file with MIDDLEWARE
        settings = tmp_path / "settings.py"
        settings.write_text(
            textwrap.dedent("""\
            MIDDLEWARE = [
                "django.middleware.common.CommonMiddleware",
                "baldur.api.django.middleware.BaldurMiddleware",
            ]
        """),
            encoding="utf-8",
        )

        # And — the middleware file exists with a service import
        mw_file = tmp_path / "sh" / "api" / "django" / "middleware.py"
        mw_file.parent.mkdir(parents=True)
        mw_file.write_text(
            "from baldur.services.circuit_breaker import CB",
            encoding="utf-8",
        )

        with (
            patch.object(verify_wiring, "MIDDLEWARE_SETTINGS_PATH", settings),
            patch.object(verify_wiring, "BALDUR_ROOT", tmp_path / "sh"),
        ):
            result = verify_wiring.scan_middleware_wiring()

        assert "baldur.api.django.middleware.BaldurMiddleware" in result
        assert (
            "circuit_breaker" in result["baldur.api.django.middleware.BaldurMiddleware"]
        )

    def test_missing_settings_returns_empty(self, tmp_path):
        """Verify missing settings file returns an empty dict."""
        with patch.object(
            verify_wiring, "MIDDLEWARE_SETTINGS_PATH", tmp_path / "nope.py"
        ):
            assert verify_wiring.scan_middleware_wiring() == {}


# =============================================================================
# Phase 3: scan_indirect_connections
# =============================================================================


class TestScanIndirectConnectionsBehavior:
    """Behavior tests for 1-depth indirect connection detection."""

    def test_finds_transitive_import(self, tmp_path):
        """Verify transitive service imports are detected as indirect connections."""
        # Given — service A imports service B
        services = tmp_path / "services"
        svc_a = services / "circuit_breaker"
        svc_a.mkdir(parents=True)
        (svc_a / "service.py").write_text(
            "from baldur_pro.services.error_budget import ErrorBudget",
            encoding="utf-8",
        )

        svc_b = services / "error_budget"
        svc_b.mkdir(parents=True)
        (svc_b / "__init__.py").write_text("", encoding="utf-8")

        with patch.object(verify_wiring, "SERVICES_DIR", services):
            result = verify_wiring.scan_indirect_connections(
                already_connected={"circuit_breaker"},
                all_services=["circuit_breaker", "error_budget"],
            )

        assert "error_budget" in result
        assert "via circuit_breaker" in result["error_budget"]

    def test_does_not_include_already_connected(self, tmp_path):
        """Verify already-connected services are excluded from indirect results."""
        services = tmp_path / "services"
        svc = services / "dlq"
        svc.mkdir(parents=True)
        (svc / "service.py").write_text(
            "from baldur_pro.services.audit import AuditService",
            encoding="utf-8",
        )

        with patch.object(verify_wiring, "SERVICES_DIR", services):
            result = verify_wiring.scan_indirect_connections(
                already_connected={"dlq", "audit"},
                all_services=["dlq", "audit"],
            )

        # audit is already connected, so should NOT appear
        assert "audit" not in result


# =============================================================================
# Phase 3: EventBus subscription detection (§3.8)
# =============================================================================


class TestEventBusSubscriptionBehavior:
    """Behavior tests for EventBus.subscribe() regex detection."""

    def test_detects_subscribe_pattern(self, tmp_path):
        """Verify EventBus.subscribe with EventType argument is detected."""
        svc = tmp_path / "my_svc"
        svc.mkdir()
        (svc / "handler.py").write_text(
            "EventBus.subscribe(EventType.CIRCUIT_OPENED, self._on_open)",
            encoding="utf-8",
        )
        assert verify_wiring._has_eventbus_subscription(svc) is True

    def test_detects_multiline_subscribe(self, tmp_path):
        """Formatter may wrap subscribe across lines (§3.8)."""
        svc = tmp_path / "my_svc"
        svc.mkdir()
        (svc / "handler.py").write_text(
            textwrap.dedent("""\
            EventBus.subscribe(
                EventType.RECOVERY_STARTED,
                handler,
            )
        """),
            encoding="utf-8",
        )
        assert verify_wiring._has_eventbus_subscription(svc) is True

    def test_no_match_without_eventtype(self, tmp_path):
        """Verify subscribe calls without EventType are not matched."""
        svc = tmp_path / "my_svc"
        svc.mkdir()
        (svc / "handler.py").write_text(
            'EventBus.subscribe("some_event", handler)',
            encoding="utf-8",
        )
        assert verify_wiring._has_eventbus_subscription(svc) is False

    def test_empty_directory(self, tmp_path):
        """Verify empty directory has no EventBus subscription."""
        svc = tmp_path / "empty_svc"
        svc.mkdir()
        assert verify_wiring._has_eventbus_subscription(svc) is False


# =============================================================================
# Phase 4: load_allowlist
# =============================================================================


class TestLoadAllowlistBehavior:
    """Behavior tests for YAML allowlist loading."""

    def test_valid_allowlist(self, allowlist_file):
        """Verify valid YAML allowlist returns correct service names, on-demand tasks, and reasons."""
        f = allowlist_file("""\
            allowlist:
              - name: saga
                reason: "Host app defines"
              - name: isolation
                reason: "Multi-region only"
            on_demand_tasks:
              - name: "baldur.saga.*"
                reason: "On demand"
        """)
        with patch.object(verify_wiring, "ALLOWLIST_PATH", f):
            names, on_demand, reasons = verify_wiring.load_allowlist()

        assert names == {"saga", "isolation"}
        assert "baldur.saga.*" in on_demand
        assert reasons == {"saga": "Host app defines", "isolation": "Multi-region only"}

    def test_missing_file_returns_empty(self, tmp_path):
        """Verify missing allowlist file returns empty set, empty list, and empty dict."""
        with patch.object(
            verify_wiring, "ALLOWLIST_PATH", tmp_path / "nonexistent.yaml"
        ):
            names, on_demand, reasons = verify_wiring.load_allowlist()

        assert names == set()
        assert on_demand == []
        assert reasons == {}

    def test_empty_yaml_returns_empty(self, allowlist_file):
        """Verify empty YAML file returns empty set, empty list, and empty dict."""
        f = allowlist_file("")
        with patch.object(verify_wiring, "ALLOWLIST_PATH", f):
            names, on_demand, reasons = verify_wiring.load_allowlist()

        assert names == set()
        assert on_demand == []
        assert reasons == {}


# =============================================================================
# §6.1: Celery task verification
# =============================================================================


class TestVerifyCeleryTasksBehavior:
    """Behavior tests for periodic vs on-demand task classification."""

    def test_periodic_task_classified_correctly(self):
        """Verify tasks found in celery beat schedule are classified as periodic."""
        with (
            patch.object(
                verify_wiring,
                "scan_celery_beat_tasks",
                autospec=True,
                return_value={"baldur.celery_tasks.collect_metrics"},
            ),
            patch.object(
                verify_wiring,
                "scan_shared_tasks",
                autospec=True,
                return_value={
                    "baldur.celery_tasks.collect_metrics": "celery_tasks/metrics.py"
                },
            ),
        ):
            result = verify_wiring.verify_celery_tasks([])

        assert len(result["periodic"]) == 1
        assert result["periodic"][0]["name"] == "baldur.celery_tasks.collect_metrics"

    def test_on_demand_with_glob_pattern(self):
        """Verify on-demand tasks matching an allowlist glob are marked as allowlisted."""
        with (
            patch.object(
                verify_wiring,
                "scan_celery_beat_tasks",
                autospec=True,
                return_value=set(),
            ),
            patch.object(
                verify_wiring,
                "scan_shared_tasks",
                autospec=True,
                return_value={"baldur.saga.step_execute": "services/saga/tasks.py"},
            ),
        ):
            result = verify_wiring.verify_celery_tasks(["baldur.saga.*"])

        assert len(result["on_demand"]) == 1
        assert result["on_demand"][0]["allowlisted"] is True

    def test_on_demand_without_pattern_not_allowlisted(self):
        """Verify on-demand tasks not matching any glob are marked as not allowlisted."""
        with (
            patch.object(
                verify_wiring,
                "scan_celery_beat_tasks",
                autospec=True,
                return_value=set(),
            ),
            patch.object(
                verify_wiring,
                "scan_shared_tasks",
                autospec=True,
                return_value={"baldur.unknown.task": "services/x/tasks.py"},
            ),
        ):
            result = verify_wiring.verify_celery_tasks([])

        assert len(result["on_demand"]) == 1
        assert result["on_demand"][0]["allowlisted"] is False


# =============================================================================
# §6.2: verify_dependency_graph
# =============================================================================


class TestVerifyDependencyGraphBehavior:
    """Behavior tests for ServiceDependencyGraph consistency check."""

    def test_orphan_not_in_graph_produces_warning(self, py_file):
        """Verify orphan service not registered in the graph produces a warning."""
        apps = py_file(
            """\
            graph = ServiceDependencyGraph()
            graph.register_service("event_journal")
            init_order = graph.topological_sort_subset(
                services=["event_journal"],
                direction="leaves_first",
            )
        """,
            name="apps.py",
        )

        with patch.object(verify_wiring, "BALDUR_ROOT", apps.parent):
            # Pretend apps.py lives at BALDUR_ROOT/adapters/django/apps.py
            adapters = apps.parent / "adapters" / "django"
            adapters.mkdir(parents=True)
            real_apps = adapters / "apps.py"
            real_apps.write_text(apps.read_text(encoding="utf-8"), encoding="utf-8")

            warnings = verify_wiring.verify_dependency_graph(["new_orphan_svc"])

        assert len(warnings) == 1
        assert "new_orphan_svc" in warnings[0]

    def test_no_warning_when_orphan_is_registered(self, tmp_path):
        """Verify no warning when orphan service is registered in the dependency graph."""
        adapters = tmp_path / "adapters" / "django"
        adapters.mkdir(parents=True)
        apps = adapters / "apps.py"
        apps.write_text(
            textwrap.dedent("""\
            graph.register_service("my_svc")
            graph.topological_sort_subset(services=["my_svc"], direction="leaves_first")
        """),
            encoding="utf-8",
        )

        with patch.object(verify_wiring, "BALDUR_ROOT", tmp_path):
            warnings = verify_wiring.verify_dependency_graph(["my_svc"])

        assert warnings == []

    def test_missing_apps_file(self, tmp_path):
        """Verify missing apps.py file produces a 'not found' warning."""
        with patch.object(verify_wiring, "BALDUR_ROOT", tmp_path):
            warnings = verify_wiring.verify_dependency_graph(["any_svc"])
        assert len(warnings) == 1
        assert "not found" in warnings[0]


# =============================================================================
# §6.3: verify_feature_flags
# =============================================================================


class TestVerifyFeatureFlagsBehavior:
    """Behavior tests for env_prefix static text search."""

    def test_found_double_quote_prefix(self, settings_dir):
        """Verify double-quoted env_prefix is detected for a service."""
        (settings_dir / "circuit_breaker.py").write_text(
            'model_config = SettingsConfigDict(env_prefix="BALDUR_CIRCUIT_BREAKER_")',
            encoding="utf-8",
        )
        with patch.object(verify_wiring, "BALDUR_ROOT", settings_dir.parent):
            result = verify_wiring.verify_feature_flags(["circuit_breaker"])

        assert result == []

    def test_found_single_quote_prefix(self, settings_dir):
        """Verify single-quoted env_prefix is detected for a service."""
        (settings_dir / "dlq.py").write_text(
            "model_config = SettingsConfigDict(env_prefix='BALDUR_DLQ_')",
            encoding="utf-8",
        )
        with patch.object(verify_wiring, "BALDUR_ROOT", settings_dir.parent):
            result = verify_wiring.verify_feature_flags(["dlq"])

        assert result == []

    def test_missing_prefix_reported(self, settings_dir):
        """Verify service without env_prefix is reported as missing."""
        (settings_dir / "empty.py").write_text("", encoding="utf-8")

        with patch.object(verify_wiring, "BALDUR_ROOT", settings_dir.parent):
            result = verify_wiring.verify_feature_flags(["missing_service"])

        assert "missing_service" in result

    def test_nonexistent_settings_dir(self, tmp_path):
        """Verify nonexistent settings directory reports all services as missing."""
        with patch.object(verify_wiring, "BALDUR_ROOT", tmp_path):
            result = verify_wiring.verify_feature_flags(["any"])
        assert result == ["any"]


# =============================================================================
# Phase 5: WiringReport.to_json
# =============================================================================


class TestWiringReportBehavior:
    """Behavior tests for report generation."""

    def test_to_json_structure_pass(self):
        """Verify to_json returns pass=True with correct counts when no orphans."""
        report = verify_wiring.WiringReport()
        report.total_services = 5
        report.connected = {"svc_a": {"middleware"}}
        report.indirect = {}
        report.allowlisted = {}
        report.orphans = []
        report.eventbus_subscribers = set()
        report.middleware_wiring = {}
        report.dep_graph_warnings = []
        report.missing_feature_flags = []

        data = report.to_json()

        assert data["pass"] is True
        assert data["total_services"] == 5
        assert data["connected"] == 1
        assert data["orphan"] == 0
        assert data["orphans"] == []
        assert "timestamp" in data

    def test_to_json_structure_fail(self):
        """Verify to_json returns pass=False with orphan details when orphans exist."""
        report = verify_wiring.WiringReport()
        report.total_services = 3
        report.connected = {}
        report.indirect = {}
        report.allowlisted = {}
        report.orphans = ["orphan_a", "orphan_b"]
        report.eventbus_subscribers = set()
        report.middleware_wiring = {}
        report.dep_graph_warnings = ["warn1"]
        report.missing_feature_flags = ["orphan_a"]

        data = report.to_json()

        assert data["pass"] is False
        assert data["orphan"] == 2
        assert len(data["orphans"]) == 2
        assert data["orphans"][0]["name"] == "orphan_a"
        assert data["dep_graph_warnings"] == ["warn1"]
        assert data["missing_feature_flags"] == ["orphan_a"]

    def test_to_json_idempotent(self):
        """Calling to_json() multiple times returns same structure."""
        report = verify_wiring.WiringReport()
        report.total_services = 1
        report.connected = {"x": {"view"}}
        report.orphans = []
        report.eventbus_subscribers = set()
        report.middleware_wiring = {}

        first = report.to_json()
        second = report.to_json()

        # Compare everything except timestamp
        for key in first:
            if key != "timestamp":
                assert first[key] == second[key]


# =============================================================================
# Integration: _collect_python_files
# =============================================================================


class TestCollectPythonFilesBehavior:
    """Behavior tests for Python file collection utility."""

    def test_single_py_file(self, tmp_path):
        """Verify a single .py file is returned as a one-element list."""
        f = tmp_path / "module.py"
        f.write_text("", encoding="utf-8")
        assert verify_wiring._collect_python_files(f) == [f]

    def test_non_py_file_ignored(self, tmp_path):
        """Verify non-.py files are excluded from collection."""
        f = tmp_path / "readme.md"
        f.write_text("", encoding="utf-8")
        assert verify_wiring._collect_python_files(f) == []

    def test_directory_recursive(self, tmp_path):
        """Verify .py files are collected recursively from subdirectories."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        (sub / "b.py").write_text("", encoding="utf-8")

        files = verify_wiring._collect_python_files(tmp_path)
        names = {f.name for f in files}
        assert "a.py" in names
        assert "b.py" in names

    def test_nonexistent_returns_empty(self, tmp_path):
        """Verify nonexistent path returns an empty list."""
        assert verify_wiring._collect_python_files(tmp_path / "nope") == []


# =============================================================================
# Integration: run_verification exit codes
# =============================================================================


class TestRunVerificationContract:
    """Contract tests for end-to-end orchestration exit codes."""

    def test_returns_zero_when_no_orphans(self, tmp_path):
        """Verify exit code 0 when all services are connected."""
        # Given — all services are connected
        services = tmp_path / "services"
        svc = services / "my_svc"
        svc.mkdir(parents=True)
        (svc / "__init__.py").write_text("", encoding="utf-8")

        entrypoints = tmp_path / "api" / "django" / "views"
        entrypoints.mkdir(parents=True)
        (entrypoints / "my_view.py").write_text(
            "from baldur.services.my_svc import Service",
            encoding="utf-8",
        )

        settings = tmp_path / "settings"
        settings.mkdir()
        (settings / "my_svc.py").write_text(
            'model_config = SettingsConfigDict(env_prefix="BALDUR_MY_SVC_")',
            encoding="utf-8",
        )

        with (
            patch.object(verify_wiring, "SERVICES_DIR", services),
            patch.object(verify_wiring, "BALDUR_ROOT", tmp_path),
            patch.object(verify_wiring, "PROJECT_ROOT", tmp_path),
            patch.object(verify_wiring, "MIDDLEWARE_SETTINGS_PATH", tmp_path / "nope"),
            patch.object(verify_wiring, "ALLOWLIST_PATH", tmp_path / "nope.yaml"),
        ):
            exit_code = verify_wiring.run_verification(verbose=False, output_json=False)

        assert exit_code == 0

    def test_returns_one_when_orphan_exists(self, tmp_path):
        """Verify exit code 1 when an orphan service exists."""
        # Given — service exists but no entrypoint references it
        services = tmp_path / "services"
        svc = services / "orphan"
        svc.mkdir(parents=True)
        (svc / "__init__.py").write_text("", encoding="utf-8")

        with (
            patch.object(verify_wiring, "SERVICES_DIR", services),
            patch.object(verify_wiring, "BALDUR_ROOT", tmp_path),
            patch.object(verify_wiring, "PROJECT_ROOT", tmp_path),
            patch.object(verify_wiring, "MIDDLEWARE_SETTINGS_PATH", tmp_path / "nope"),
            patch.object(verify_wiring, "ALLOWLIST_PATH", tmp_path / "nope.yaml"),
        ):
            exit_code = verify_wiring.run_verification(verbose=False, output_json=False)

        assert exit_code == 1


# =============================================================================
# Constants contract tests (§3.2, §3.5)
# =============================================================================


class TestConstantsContract:
    """Contract tests verifying script constants match document spec."""

    def test_ignore_dirs_match_spec(self):
        """§3.5: IGNORE_DIRS must contain event_bus, factory, __pycache__."""
        assert verify_wiring.IGNORE_DIRS == {"event_bus", "factory", "__pycache__"}

    def test_subscribe_pattern_matches_eventtype(self):
        """§3.8: SUBSCRIBE_PATTERN must match .subscribe(EventType. with optional whitespace."""
        assert verify_wiring.SUBSCRIBE_PATTERN.search(".subscribe(EventType.X")
        assert verify_wiring.SUBSCRIBE_PATTERN.search(".subscribe(\n    EventType.X")
        assert verify_wiring.SUBSCRIBE_PATTERN.search(".subscribe(  EventType.X")
        assert not verify_wiring.SUBSCRIBE_PATTERN.search('.subscribe("some_event"')
