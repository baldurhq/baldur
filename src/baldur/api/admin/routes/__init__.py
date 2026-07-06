"""Admin route modules — one per domain.

Each module exposes a single ``_register_<domain>_routes(registry)`` function
that wires its handlers onto an :class:`AdminRegistry`. The umbrella
:func:`register_all_routes` invokes them in order; per-module try/except
inside each registrar keeps a missing optional dependency from blocking
other domains' routes.

Extraction history: 429 (initial
extraction), 432 (Phase 2b/2c
domains).
"""

from __future__ import annotations

from baldur.api.admin.registry import AdminRegistry
from baldur.api.admin.routes._import_policy import handle_route_import_failure
from baldur.api.admin.routes.analysis import _register_analysis_routes
from baldur.api.admin.routes.audit_resilience import _register_audit_resilience_routes
from baldur.api.admin.routes.chaos import _register_chaos_routes
from baldur.api.admin.routes.circuit_breaker import (
    _register_circuit_breaker_control_routes,
)
from baldur.api.admin.routes.config_data import _register_config_data_routes
from baldur.api.admin.routes.console import _register_console_routes
from baldur.api.admin.routes.continuous_audit import _register_continuous_audit_routes
from baldur.api.admin.routes.core import _register_core_routes
from baldur.api.admin.routes.daily_report import _register_daily_report_routes
from baldur.api.admin.routes.dlq import _register_dlq_routes
from baldur.api.admin.routes.emergency import _register_emergency_routes
from baldur.api.admin.routes.error_budget import _register_error_budget_routes
from baldur.api.admin.routes.governance import _register_governance_routes
from baldur.api.admin.routes.health import _register_health_routes
from baldur.api.admin.routes.l2_storage import _register_l2_storage_routes
from baldur.api.admin.routes.operations import _register_operations_routes
from baldur.api.admin.routes.recovery import _register_recovery_routes
from baldur.api.admin.routes.runtime_config import _register_config_routes
from baldur.api.admin.routes.security_review import _register_security_review_routes
from baldur.api.admin.routes.system_control import _register_system_control_routes

__all__ = [
    "handle_route_import_failure",
    "register_all_routes",
]


def register_all_routes(registry: AdminRegistry) -> None:
    """Wire every domain's admin routes onto ``registry`` in deterministic order.

    Order matters only for the duplicate-replace semantics of
    :meth:`AdminRegistry.register` — later registrations override earlier ones
    for the same ``(method, path)`` pair. Core (K8s probes / health) registers
    first so subsequent domain modules can override individual paths if
    needed without losing the base set.
    """
    _register_core_routes(registry)
    # Extended health & metrics
    _register_health_routes(registry)
    # Operator control surface
    _register_system_control_routes(registry)
    _register_daily_report_routes(registry)
    _register_circuit_breaker_control_routes(registry)
    _register_dlq_routes(registry)
    _register_emergency_routes(registry)
    _register_config_routes(registry)
    # Resilience + observability
    _register_audit_resilience_routes(registry)
    _register_operations_routes(registry)
    # Chaos + storage
    _register_chaos_routes(registry)
    _register_l2_storage_routes(registry)
    # Audit + compliance
    _register_continuous_audit_routes(registry)
    # Reliability + governance
    _register_error_budget_routes(registry)
    _register_governance_routes(registry)
    _register_recovery_routes(registry)
    _register_analysis_routes(registry)
    _register_config_data_routes(registry)
    _register_security_review_routes(registry)
    # Web console (GET /) — registered last so its root path never shadows a
    # more specific domain route.
    _register_console_routes(registry)
