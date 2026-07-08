#!/usr/bin/env python3
"""
Clean-venv smoke installer for Baldur (OSS) packaging (Wave 6B-1).

Builds the OSS ``baldur`` wheel via ``python -m build`` (or reuses a
pre-built one via ``--wheel-path``), then for each cell (baseline / django /
fastapi / flask / postgres / redis / celery / prometheus / openapi) creates
an isolated tmp venv, installs the wheel with that cell's extras, and asserts:

  * entry-point imports succeed
  * each cell's `must_import` set imports cleanly (extras-dep regression gate)
  * each cell's `must_not_import` set raises ModuleNotFoundError
    (sibling-framework leak gate)
  * each cell's `call_assertions` execute the expected runtime shape

Writes a machine-readable JSON report to dist/smoke_install_report.json.

Pass criteria: exit 0 + 9 cells "pass" in dist/smoke_install_report.json.
No ImportError / ModuleNotFoundError.

Usage:
    python scripts/smoke_install.py
    python scripts/smoke_install.py --fail-fast
    python scripts/smoke_install.py --report-path custom/report.json
    python scripts/smoke_install.py --wheel-path dist/baldur-0.1.0-py3-none-any.whl
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
import tomllib
import venv
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# Per-cell declarative spec (D1/D2/D8/D14, 516 D5).
#   extras           : extras spec for `pip install pkg[extras]` ("" = no extras)
#   entry_points     : modules/names imported as a smoke-check (must succeed)
#   must_import      : positive-test set — extras-dep regression gate (D14)
#   must_not_import  : negative-test set — sibling-framework leak gate (D8)
#   call_assertions  : 516 D5 — call-path assertions executed in a single
#                      subprocess per cell. Each is a (call_expr, expected) pair
#                      where expected is one of:
#                        "ok"               — call must complete without raising
#                        "silent_noop"      — call must return without raising
#                                             AND produce no observable effect
#                        "NotImplementedError" — call must raise
#                                                NotImplementedError
CELLS: dict[str, dict[str, Any]] = {
    "baseline": {
        "extras": "",
        "entry_points": [
            "import baldur",
            "from baldur import protected",
            "from baldur import get_circuit_breaker_service",
        ],
        "must_import": [],
        "must_not_import": ["django", "fastapi", "starlette", "flask"],
        "call_assertions": [
            # 516 D2 — NoOp governance default must answer fail-open without PRO.
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "g = ProviderRegistry.governance.get(); "
                "assert g.is_system_enabled() is True; "
                "assert g.check_all_governance().allowed is True",
                "ok",
            ),
            # 516 D2 — NoOp pool_monitor default returns empty stats.
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "p = ProviderRegistry.pool_monitor.get(); "
                "s = p.get_stats(); "
                "assert s.max_connections == 0",
                "ok",
            ),
            # 516 D2 — OSS in-memory pool stats provider constructs without PRO.
            (
                "from baldur.adapters.pool.memory_stats import "
                "InMemoryPoolStatsProvider; "
                "p = InMemoryPoolStatsProvider(); "
                "assert p.get_stats().pool_name == 'test_pool'",
                "ok",
            ),
            # 516 D3 — ThrottleGovernanceGuard.check() returns allowed via NoOp.
            (
                "from baldur.resilience.policies.guards.governance import "
                "ThrottleGovernanceGuard; "
                "result = ThrottleGovernanceGuard().check(); "
                "assert result.allowed is True",
                "ok",
            ),
            # 518 D14 batch (a) — audit helper silent no-op on clean OSS install.
            (
                "from baldur.audit.helpers import log_dlq_replay_audit; "
                "log_dlq_replay_audit(dlq_id=1, domain='x', success=True)",
                "silent_noop",
            ),
            # 518 D14 batch (a) — notification helper silent no-op on clean OSS install.
            (
                "from baldur.notification.helpers import notify; "
                "notify(title='t', message='m')",
                "silent_noop",
            ),
            # 518 D14 batch (a) — dlq helper silent no-op on clean OSS install.
            (
                "from baldur.dlq.helpers import store_to_dlq; "
                "store_to_dlq(domain='x', failure_type='y')",
                "silent_noop",
            ),
            # 518 D14 batch (b) — extended GovernanceChecker methods are NoOp on OSS.
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "g = ProviderRegistry.governance.get(); "
                "g.invalidate_governance_cache(); "
                "g.reset_governance_pipeline_cache()",
                "ok",
            ),
            # 519 G1 (d-track) — StepResult relocated to baldur.models.saga.
            # Runtime-reachable on OSS via runbook action handlers
            # (services/runbook/primitives.py); previously raised
            # AttributeError on `None.succeeded(...)` when baldur_pro was absent.
            (
                "from baldur.models.saga import StepResult; "
                "r = StepResult.succeeded({'a': 1}); "
                "assert r.success is True and r.data == {'a': 1}; "
                "f = StepResult.failed('boom', 'E1'); "
                "assert f.success is False and f.error_code == 'E1'",
                "ok",
            ),
            # 599 D2 — the runbook implementation (incl. primitives) moved to
            # baldur_pro; an OSS-only install must NOT be able to import it.
            (
                "import importlib.util; "
                "assert importlib.util.find_spec('baldur.services.runbook') is None",
                "ok",
            ),
            # 599 D2 batch C — the Dormant cluster moved to baldur_dormant;
            # an OSS-only install must NOT be able to import any of it.
            (
                "import importlib.util; "
                "assert importlib.util.find_spec('baldur.services.ml_models') is None; "
                "assert importlib.util.find_spec("
                "'baldur.services.predictive_forecaster') is None; "
                "assert importlib.util.find_spec('baldur.services.learning') is None; "
                "assert importlib.util.find_spec("
                "'baldur.services.correlation_engine') is None; "
                "assert importlib.util.find_spec('baldur.services.compliance') is None",
                "ok",
            ),
            # 599 D5 batch D — multiregion moved to baldur_dormant; an
            # OSS-only install must NOT be able to import it, and the
            # quorum_witness slot has no OSS default anymore (the dormant
            # hook registers the in-memory witness).
            (
                "import importlib.util; "
                "assert importlib.util.find_spec('baldur.multiregion') is None",
                "ok",
            ),
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "assert ProviderRegistry.quorum_witness.safe_get() is None",
                "ok",
            ),
            # 519 PR 2 (c) track — all 14 OSS->PRO singleton slots return None
            # when baldur_pro is not installed (safe_get() raises no error,
            # callers branch on the None result instead of crashing). Matches
            # the doc's Implementation Deviations PR 2 statement: "all 14 (c)
            # slots return None via safe_get() on a clean OSS install".
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "assert ProviderRegistry.emergency_manager.safe_get() is None; "
                "assert ProviderRegistry.adaptive_throttle.safe_get() is None; "
                "assert ProviderRegistry.bulkhead_registry.safe_get() is None; "
                "assert ProviderRegistry.runtime_config_manager.safe_get() is None; "
                "assert ProviderRegistry.chaos_scheduler.safe_get() is None; "
                "assert ProviderRegistry.report_generator.safe_get() is None; "
                "assert ProviderRegistry.safety_guard.safe_get() is None; "
                "assert ProviderRegistry.dlq_service.safe_get() is None; "
                "assert ProviderRegistry.dlq_repository.safe_get() is None; "
                "assert ProviderRegistry.selfhealer_watchdog.safe_get() is None; "
                "assert ProviderRegistry.error_budget_service.safe_get() is None; "
                "assert ProviderRegistry.error_budget_gate.safe_get() is None; "
                "assert ProviderRegistry.canary_rollout_service.safe_get() is None; "
                "assert ProviderRegistry.blast_radius_manager.safe_get() is None",
                "ok",
            ),
            # 599 D7 — relocated-feature slots are empty on a clean OSS install.
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "assert ProviderRegistry.finops_service.safe_get() is None; "
                "assert ProviderRegistry.learning_service.safe_get() is None; "
                "assert ProviderRegistry.compliance_engine.safe_get() is None; "
                "assert ProviderRegistry.predictive_forecaster_service.safe_get() is None; "
                "assert not ProviderRegistry.worker_background_starts.has_any_providers()",
                "ok",
            ),
            # 519 PR 2 (c) track — safe_get is the documented OSS->PRO boundary
            # access shape; .get() on an empty slot still raises by design.
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "from baldur.core.exceptions import AdapterNotFoundError; "
                "raised = False;\n"
                "try:\n"
                "    ProviderRegistry.emergency_manager.get()\n"
                "except AdapterNotFoundError:\n"
                "    raised = True\n"
                "assert raised",
                "ok",
            ),
            # 519 PR 3 (d) track — G3: PoolHealthStatus enum reachable on OSS
            # via baldur.interfaces.pool_monitor (previously a None-fallback
            # that crashed PoolWatchdog._handle_* method bodies on OSS install).
            (
                "from baldur.interfaces.pool_monitor import PoolHealthStatus; "
                "assert PoolHealthStatus.HEALTHY.value == 'healthy'; "
                "from baldur.core.pool_watchdog import PoolHealthStatus as PW; "
                "assert PW is PoolHealthStatus",
                "ok",
            ),
            # 519 PR 3 (d) track — G4: PassCriteria DTO constructs cleanly on
            # OSS install (previously a None-fallback that crashed
            # LiveCanaryEvaluator.__init__ when pass_criteria was None).
            (
                "from baldur.models.canary import PassCriteria, CanaryState; "
                "pc = PassCriteria(); "
                "assert 0 < pc.error_rate_absolute_max < 1; "
                "assert CanaryState.CREATED.value == 'created'",
                "ok",
            ),
        ],
    },
    "django": {
        "extras": "django",
        "entry_points": [
            "import baldur",
            "from baldur import protected",
            "from baldur import get_circuit_breaker_service",
            "import django",
            "from baldur.adapters.django import apps",
        ],
        "must_import": ["django"],
        "must_not_import": ["fastapi", "starlette", "flask"],
        "call_assertions": [
            # Same governance assertion under Django extras path.
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "g = ProviderRegistry.governance.get(); "
                "assert g.is_system_enabled() is True",
                "ok",
            ),
            # 516 D2 — Config apply service responds fail-open without PRO.
            (
                "from baldur.services.execution_services.config_apply_service "
                "import ConfigApplyService; "
                "ConfigApplyService.reset_instance(); "
                "result = ConfigApplyService().apply_pending_changes(); "
                "assert result.get('status') in ('blocked', 'success')",
                "ok",
            ),
        ],
    },
    "fastapi": {
        "extras": "fastapi",
        "entry_points": [
            "import baldur",
            "from baldur import protected",
            "from baldur import get_circuit_breaker_service",
            "from baldur.adapters.fastapi.middleware import BaldurMiddleware",
        ],
        "must_import": ["fastapi", "starlette"],
        "must_not_import": ["django", "flask"],
        "call_assertions": [
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "assert ProviderRegistry.governance.get() is not None",
                "ok",
            ),
        ],
    },
    "flask": {
        "extras": "flask",
        "entry_points": [
            "import baldur",
            "from baldur import protected",
            "from baldur import get_circuit_breaker_service",
            "from baldur.adapters.flask import init_flask",
        ],
        "must_import": ["flask"],
        "must_not_import": ["django", "fastapi", "starlette"],
        "call_assertions": [
            (
                "from baldur.factory.registry import ProviderRegistry; "
                "assert ProviderRegistry.shutdown_integrations is not None",
                "ok",
            ),
        ],
    },
    "postgres": {
        "extras": "postgres",
        "entry_points": [
            "import baldur",
            "from baldur import protected",
            "from baldur import get_circuit_breaker_service",
            "import baldur.adapters.postgres",
            "from baldur.adapters.postgres import PgAdmin",
        ],
        "must_import": ["psycopg2", "django"],
        "must_not_import": ["fastapi", "starlette", "flask"],
        "call_assertions": [
            (
                "from baldur.adapters.pool.sqlalchemy_stats import "
                "SQLAlchemyPoolStatsProvider; "
                "p = SQLAlchemyPoolStatsProvider(); "
                "assert p.get_stats().pool_name == 'default'",
                "ok",
            ),
        ],
    },
    # release-checklist 6B — closes the [redis] / [celery] / [prometheus]
    # extras coverage gap. Each cell verifies the extras-declared dep is
    # installed AND a representative baldur adapter module loads cleanly,
    # so a typo in pyproject.toml [project.optional-dependencies] surfaces
    # here rather than on the user's first import.
    "redis": {
        "extras": "redis",
        "entry_points": [
            "import baldur",
            "from baldur.adapters.redis.connection_factory import "
            "RedisConnectionFactory",
        ],
        "must_import": ["redis"],
        "must_not_import": ["django", "fastapi", "starlette", "flask", "celery"],
        "call_assertions": [
            (
                "from baldur.adapters.redis.connection_factory import "
                "RedisConnectionFactory; "
                "f = RedisConnectionFactory(); "
                "assert hasattr(f, 'create')",
                "ok",
            ),
        ],
    },
    # celery extra transitively pulls redis (pyproject.toml [celery] =
    # ["celery>=5.3", "redis>=4.0"]), so must_import asserts both.
    "celery": {
        "extras": "celery",
        "entry_points": [
            "import baldur",
            "from baldur.adapters.celery.baldur_task import baldur_task",
        ],
        "must_import": ["celery", "redis"],
        "must_not_import": ["django", "fastapi", "starlette", "flask"],
        "call_assertions": [
            (
                "from baldur.adapters.celery.baldur_task import baldur_task\n"
                "def _stub():\n"
                "    return 1\n"
                "wrapped = baldur_task(domain='test')(_stub)\n"
                "assert callable(wrapped)",
                "ok",
            ),
        ],
    },
    # PROMETHEUS_AVAILABLE flips to True only when the prometheus_client
    # dep is resolved by the extras install — the assertion is the gate.
    "prometheus": {
        "extras": "prometheus",
        "entry_points": [
            "import baldur",
            "from baldur.metrics.prometheus import BaldurMetrics",
        ],
        "must_import": ["prometheus_client"],
        "must_not_import": ["django", "fastapi", "starlette", "flask", "celery"],
        "call_assertions": [
            (
                "from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE; "
                "assert PROMETHEUS_AVAILABLE is True",
                "ok",
            ),
        ],
    },
    # 530 Wave 6F — drf-spectacular extras is the gate for the /schema/
    # /docs/ /redoc/ surface. Cell verifies (i) the dep itself constructs,
    # (ii) the framework-agnostic features handler imports without
    # baldur_pro present, (iii) the wheel-bundled V1_LAUNCH_MANIFEST.yaml
    # is reachable via importlib.resources (force-include gate), and
    # (iv) get_entitlement_status() short-circuits to MISSING on a clean
    # OSS install — guards against ImportError leakage from the
    # entitlement-overlay path inside the /features/ handler.
    "openapi": {
        "extras": "openapi",
        "entry_points": [
            "import baldur",
            "from baldur.api.handlers.features import features_summary",
            "from baldur.settings.openapi import get_openapi_settings",
        ],
        # drf-spectacular is a Django REST Framework extension: Django + DRF are
        # hard dependencies of the [openapi] extra (verified via its metadata),
        # not sibling-framework leaks. They belong in must_import, not
        # must_not_import — the sibling gate covers only the OTHER web stacks.
        "must_import": ["drf_spectacular", "django", "rest_framework"],
        "must_not_import": ["fastapi", "starlette", "flask", "celery"],
        "call_assertions": [
            # NOTE: drf-spectacular's generators / SchemaGenerator touch Django's
            # REST_FRAMEWORK setting at import time, which a clean packaging venv
            # (no DJANGO_SETTINGS_MODULE) cannot provide. The dep's presence is
            # gated by `must_import: drf_spectacular` above; the assertions here
            # stay settings-free and exercise baldur's own integration surface.
            (
                "from baldur.api.handlers.features import features_summary; "
                "assert callable(features_summary)",
                "ok",
            ),
            (
                "from baldur.core.entitlement import get_entitlement_status; "
                "assert get_entitlement_status().status.value == 'missing'",
                "ok",
            ),
            (
                "from importlib.resources import files; "
                "p = files('baldur._data').joinpath('V1_LAUNCH_MANIFEST.yaml'); "
                "assert p.is_file()",
                "ok",
            ),
        ],
    },
}


# Per-operation subprocess timeouts (R5). Tuned in one place so future CI
# slowdowns can be absorbed without touching call sites.
TIMEOUTS: dict[str, int] = {
    "wheel_build": 300,
    "pip_install": 180,
    "import_check": 30,
}


@dataclass
class CellResult:
    name: str
    status: str  # "pass" | "fail" | "skipped"
    duration_s: float = 0.0
    stderr_tail: str | None = None


@dataclass
class SmokeReport:
    meta: dict[str, str]
    cells: list[CellResult] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


def load_pyproject(project_root: Path) -> dict[str, Any]:
    """Parse pyproject.toml from project_root (D11)."""
    return tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))


def get_distribution_name(project_root: Path) -> str:
    """Resolve the distribution name at runtime (D11 — PEP 541 contingency)."""
    return load_pyproject(project_root)["project"]["name"]


def _create_venv(venv_path: Path) -> Path:
    """Create a clean venv at venv_path; return the path to the venv python.

    Spawns `python -m venv` as a subprocess (rather than calling
    `venv.EnvBuilder.create()` in-process) because the in-process path
    silently produces an empty Scripts/ directory on Windows when the
    parent interpreter is itself inside a venv. `python -m venv` is the
    canonical Windows-correct invocation.

    Uses `venv.EnvBuilder.ensure_directories` solely for cross-platform
    path resolution — it returns the `Scripts/` (Windows) vs `bin/` (POSIX)
    layout without manual concatenation (D6).
    """
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_path)],
        capture_output=True,
        text=True,
        timeout=TIMEOUTS["pip_install"],
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`python -m venv` failed (rc={result.returncode}): "
            f"{result.stderr or result.stdout}"
        )
    context = venv.EnvBuilder().ensure_directories(str(venv_path))
    python_exe = Path(context.env_exe)
    if not python_exe.exists():
        raise RuntimeError(f"venv python not found at expected path: {python_exe}")
    return python_exe


def _tail(text: str, n_chars: int = 2000) -> str:
    if len(text) <= n_chars:
        return text
    return "...[truncated]...\n" + text[-n_chars:]


def _run_subprocess(
    cmd: list[str],
    *,
    timeout: int,
    operation: str,
) -> tuple[int, str, str, str | None]:
    """Run a subprocess with capture + timeout. Return (rc, stdout, stderr, timeout_msg)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr, None
    except subprocess.TimeoutExpired as exc:
        return (
            124,
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or ""),
            f"timeout after {timeout}s during {operation}",
        )


def _install_cell_wheel(
    python_exe: Path,
    oss_wheel: Path,
    extras: str,
    spec: str,
) -> tuple[int, str, str, str | None] | None:
    """Install the cell's OSS wheel[extras]; return the failed subprocess tuple, or None on success."""
    install_arg = f"{oss_wheel}[{extras}]" if extras else str(oss_wheel)
    rc, stdout, stderr, timeout_msg = _run_subprocess(
        [str(python_exe), "-m", "pip", "install", install_arg],
        timeout=TIMEOUTS["pip_install"],
        operation=f"pip install {spec}",
    )
    if timeout_msg is not None or rc != 0:
        return rc, stdout, stderr, timeout_msg
    return None


def run_cell(  # noqa: C901
    cell_name: str,
    cfg: dict[str, Any],
    oss_wheel: Path,
    dist_name: str,
    tmp_root: Path,
) -> CellResult:
    """Run a single cell: create venv, install baldur[extras], assert imports."""
    started = time.monotonic()
    venv_path = tmp_root / f"venv-{cell_name}"
    try:
        python_exe = _create_venv(venv_path)
    except Exception as exc:
        return CellResult(
            name=cell_name,
            status="fail",
            duration_s=time.monotonic() - started,
            stderr_tail=f"venv creation failed: {exc!r}",
        )

    extras = cfg["extras"]
    spec = f"{dist_name}[{extras}]" if extras else dist_name

    install_failure = _install_cell_wheel(python_exe, oss_wheel, extras, spec)
    if install_failure is not None:
        rc, stdout, stderr, timeout_msg = install_failure
        return CellResult(
            name=cell_name,
            status="fail",
            duration_s=time.monotonic() - started,
            stderr_tail=timeout_msg
            or _tail(f"pip install rc={rc}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"),
        )

    # Entry-point imports — must succeed.
    entry_script = "\n".join(cfg["entry_points"])
    rc, stdout, stderr, timeout_msg = _run_subprocess(
        [str(python_exe), "-c", entry_script],
        timeout=TIMEOUTS["import_check"],
        operation="entry_point import",
    )
    if timeout_msg is not None or rc != 0:
        return CellResult(
            name=cell_name,
            status="fail",
            duration_s=time.monotonic() - started,
            stderr_tail=timeout_msg
            or _tail(
                f"entry_point import rc={rc}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            ),
        )

    # Positive-test set (D14) — extras-dep regression gate. Each must import.
    for module in cfg["must_import"]:
        rc, stdout, stderr, timeout_msg = _run_subprocess(
            [str(python_exe), "-c", f"import {module}"],
            timeout=TIMEOUTS["import_check"],
            operation=f"must_import {module}",
        )
        if timeout_msg is not None or rc != 0:
            return CellResult(
                name=cell_name,
                status="fail",
                duration_s=time.monotonic() - started,
                stderr_tail=timeout_msg
                or _tail(
                    f"must_import={module} rc={rc} (expected 0)\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                ),
            )

    # Negative-test set (D8) — sibling-framework leak gate. Each must NOT import.
    for module in cfg["must_not_import"]:
        rc, stdout, stderr, timeout_msg = _run_subprocess(
            [str(python_exe), "-c", f"import {module}"],
            timeout=TIMEOUTS["import_check"],
            operation=f"must_not_import {module}",
        )
        if timeout_msg is not None:
            return CellResult(
                name=cell_name,
                status="fail",
                duration_s=time.monotonic() - started,
                stderr_tail=timeout_msg,
            )
        if rc == 0:
            return CellResult(
                name=cell_name,
                status="fail",
                duration_s=time.monotonic() - started,
                stderr_tail=(
                    f"must_not_import={module} unexpectedly succeeded "
                    f"(sibling-framework leak in baldur-framework[{extras or 'baseline'}])"
                ),
            )

    # 516 D5 — call-path assertions. All assertions run in a single subprocess
    # per cell to keep total CI cost <5s. Each assertion's `expected` field
    # is one of: "ok" (call completes without raising) | "NotImplementedError"
    # (call must raise NotImplementedError) | "silent_noop" (call completes
    # without raising and produces no observable effect — verified by the
    # assertion expression itself, since the subprocess cannot introspect
    # side-effects post-hoc).
    call_assertions = cfg.get("call_assertions", [])
    if call_assertions:
        assertion_script = _build_call_assertion_script(call_assertions)
        rc, stdout, stderr, timeout_msg = _run_subprocess(
            [str(python_exe), "-c", assertion_script],
            timeout=TIMEOUTS["import_check"],
            operation="call_assertions",
        )
        if timeout_msg is not None or rc != 0:
            return CellResult(
                name=cell_name,
                status="fail",
                duration_s=time.monotonic() - started,
                stderr_tail=timeout_msg
                or _tail(
                    f"call_assertions rc={rc}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                ),
            )

    return CellResult(
        name=cell_name,
        status="pass",
        duration_s=time.monotonic() - started,
        stderr_tail=None,
    )


def _build_call_assertion_script(
    call_assertions: list[tuple[str, str]],
) -> str:
    """Compose a single-subprocess script that runs every (call, expected) pair.

    Each assertion is wrapped according to its expected shape:

    * "ok"               — execute as-is; subprocess exits non-zero on raise
    * "silent_noop"      — same as "ok" (the call expression itself must
                           encode the noop check via inner ``assert`` clauses)
    * "NotImplementedError" — wrap with try/except that fails if the call
                              does NOT raise NotImplementedError
    """
    parts: list[str] = []
    for idx, (call_expr, expected) in enumerate(call_assertions):
        label = f"call_assertion_{idx}"
        if expected in ("ok", "silent_noop"):
            parts.append(
                f"# {label}\n"
                f"try:\n"
                f"    exec({call_expr!r})\n"
                f"except Exception as exc:\n"
                f"    raise SystemExit(f'{label} ({expected}) failed: '\n"
                f"                     f'{{type(exc).__name__}}: {{exc}}')"
            )
        elif expected == "NotImplementedError":
            parts.append(
                f"# {label}\n"
                f"_raised = None\n"
                f"try:\n"
                f"    exec({call_expr!r})\n"
                f"except NotImplementedError:\n"
                f"    _raised = 'NotImplementedError'\n"
                f"except Exception as exc:\n"
                f"    raise SystemExit(f'{label} expected NotImplementedError, '\n"
                f"                     f'got {{type(exc).__name__}}: {{exc}}')\n"
                f"if _raised is None:\n"
                f"    raise SystemExit(f'{label} expected NotImplementedError, '\n"
                f"                     'but call completed without raising')"
            )
        else:
            parts.append(
                f"raise SystemExit('{label}: unknown expected shape {expected!r}')"
            )
    return "\n".join(parts)


def write_report(report: SmokeReport, report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": report.meta,
        "cells": [asdict(c) for c in report.cells],
        "summary": report.summary,
    }
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean-venv packaging smoke installer (Wave 6B-1)."
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first failing cell; remaining cells are reported as 'skipped'.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="CELL",
        help=(
            "Run only the named cell(s) for fast local iteration (repeatable, "
            "e.g. --only openapi --only django). Unknown names error out. "
            "Default: run all cells."
        ),
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=PROJECT_ROOT / "dist" / "smoke_install_report.json",
        help="Where to write the JSON report (default: dist/smoke_install_report.json).",
    )
    parser.add_argument(
        "--wheel-path",
        type=Path,
        default=None,
        help="Reuse a pre-built OSS baldur wheel instead of building one (D15; DevEx).",
    )
    return parser.parse_args(argv)


def _validate_wheel_arg(wheel_path: Path | None, flag: str) -> int | None:
    """Return non-None exit code if the user-provided wheel path is invalid."""
    if wheel_path is None:
        return None
    if not wheel_path.is_file() or wheel_path.suffix != ".whl":
        print(
            f"ERROR: {flag} must point to an existing *.whl file: {wheel_path}",
            file=sys.stderr,
        )
        return 1
    return None


def _build_oss_wheel(dist_dir: Path) -> Path:
    """Build the OSS ``baldur`` wheel from the project root via ``python -m build``.

    The public repo's ``pyproject.toml`` is the default build front-end input,
    so no working-tree overlay is needed (``src/baldur`` is in-tree). The
    ``glob("*.whl")`` diff absorbs PEP 427 name normalization.
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in dist_dir.glob("*.whl")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(dist_dir),
            str(PROJECT_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUTS["wheel_build"],
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`python -m build` failed (rc={result.returncode}):\n"
            f"{result.stderr or result.stdout}"
        )
    after = sorted(p for p in dist_dir.glob("*.whl") if p.name not in before)
    if not after:
        raise RuntimeError(f"No wheel produced in {dist_dir}")
    if len(after) > 1:
        raise RuntimeError(
            f"`python -m build` produced multiple wheels: {[p.name for p in after]}"
        )
    return after[0]


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0912, PLR0915
    args = _parse_args(argv)

    rc = _validate_wheel_arg(args.wheel_path, "--wheel-path")
    if rc is not None:
        return rc

    dist_name = get_distribution_name(PROJECT_ROOT)
    print(f"[smoke] OSS distribution name: {dist_name}")

    # --only <cell> selects a subset for fast local iteration (DevEx), e.g.
    # `--only openapi` verifies in seconds against a reused OSS wheel.
    cells_to_run = CELLS
    if args.only:
        unknown = [c for c in args.only if c not in CELLS]
        if unknown:
            print(
                f"ERROR: --only names not in CELLS: {unknown}; "
                f"valid cells: {list(CELLS)}",
                file=sys.stderr,
            )
            return 1
        cells_to_run = {k: v for k, v in CELLS.items() if k in args.only}

    with tempfile.TemporaryDirectory(
        prefix="baldur-smoke-",
        ignore_cleanup_errors=True,  # R3: Windows handle-hold tolerance
    ) as tmp_str:
        tmp_root = Path(tmp_str)

        if args.wheel_path is not None:
            oss_wheel = args.wheel_path
            print(f"[smoke] reusing OSS wheel: {oss_wheel}")
        else:
            print("[smoke] building OSS wheel via `python -m build`...")
            oss_wheel = _build_oss_wheel(tmp_root / "wheels")
            print(f"[smoke] built OSS wheel: {oss_wheel.name}")

        meta = {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "oss_wheel_name": oss_wheel.name,
        }
        report = SmokeReport(meta=meta)

        stop_after_failure = False
        for cell_name, cfg in cells_to_run.items():
            if stop_after_failure:
                print(f"[smoke] {cell_name}: SKIPPED (--fail-fast)")
                report.cells.append(CellResult(name=cell_name, status="skipped"))
                continue

            print(f"[smoke] {cell_name}: running...")
            result = run_cell(cell_name, cfg, oss_wheel, dist_name, tmp_root)
            report.cells.append(result)
            verdict = result.status.upper()
            print(f"[smoke] {cell_name}: {verdict} ({result.duration_s:.1f}s)")
            if result.status == "fail":
                if result.stderr_tail:
                    print(f"[smoke] {cell_name} stderr_tail:\n{result.stderr_tail}")
                if args.fail_fast:
                    stop_after_failure = True

        report.summary = {
            "total": len(report.cells),
            "passed": sum(1 for c in report.cells if c.status == "pass"),
            "failed": sum(1 for c in report.cells if c.status == "fail"),
            "skipped": sum(1 for c in report.cells if c.status == "skipped"),
        }

    write_report(report, args.report_path)
    print(f"[smoke] report: {args.report_path}")
    print(
        f"[smoke] summary: total={report.summary['total']} "
        f"passed={report.summary['passed']} "
        f"failed={report.summary['failed']} "
        f"skipped={report.summary['skipped']}"
    )

    return 0 if report.summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
