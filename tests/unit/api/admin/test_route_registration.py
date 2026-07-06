"""Smoke test for the admin-route umbrella registrar (526 D7).

D7 strengthens the broken-import policy: production logs a WARNING and
fail-opens, but ``BALDUR_ENV=dev`` re-raises so renames surface during
local development.

This module verifies both branches of that contract end-to-end:

- ``register_all_routes`` on a clean registry must not emit any
  ``admin.X_routes_unavailable`` warning (production fail-open path with
  no broken imports — guards against regressing the rename work).
- A simulated handler-module ImportError under ``BALDUR_ENV=dev`` must
  re-raise so CI / local servers fail fast.
- The same simulated ImportError without ``BALDUR_ENV=dev`` set must log
  a WARNING and skip the affected route group without raising.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from baldur.api.admin.registry import AdminRegistry, reset_admin_registry
from baldur.api.admin.routes import register_all_routes
from baldur.api.admin.routes._import_policy import handle_route_import_failure


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:
    reset_admin_registry()
    yield
    reset_admin_registry()


class TestRegisterAllRoutesBehavior:
    def test_no_broken_route_groups_emit_warning(self) -> None:
        """Clean registration path: every admin route group's handler module
        imports cleanly, so no ``admin.X_routes_unavailable`` warning is
        logged. Regression guard for the 5 D7-fixed route groups (recovery,
        governance, config_data, analysis, error_budget reconciliation)."""
        registry = AdminRegistry()

        with capture_logs() as captured:
            register_all_routes(registry)

        unavailable_events = [
            entry["event"]
            for entry in captured
            if entry.get("event", "").startswith("admin.")
            and entry["event"].endswith("_routes_unavailable")
            and entry.get("log_level") == "warning"
        ]
        assert unavailable_events == [], (
            "D7 strengthening expects every admin route group to import cleanly. "
            f"Unavailable groups: {unavailable_events}"
        )

    def test_registers_a_nontrivial_number_of_routes(self) -> None:
        """register_all_routes is the singleton factory's payload — must
        register the full admin surface, not bail out early."""
        registry = AdminRegistry()
        register_all_routes(registry)
        assert len(registry.all_routes()) >= 200


class TestImportPolicyBehavior:
    def test_dev_env_reraises_to_fail_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BALDUR_ENV=dev: a handler-module ImportError must re-raise so
        rename drift surfaces immediately at server startup."""
        monkeypatch.setenv("BALDUR_ENV", "dev")

        with pytest.raises(ImportError, match="simulated rename drift"):
            try:
                raise ImportError("simulated rename drift")
            except ImportError as exc:
                handle_route_import_failure("admin.test_group_unavailable", exc)

    def test_production_logs_warning_and_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without BALDUR_ENV=dev: a handler-module ImportError must be
        absorbed with a WARNING-level log so SRE has visibility while the
        rest of the admin routes still register."""
        monkeypatch.delenv("BALDUR_ENV", raising=False)

        with capture_logs() as captured:
            try:
                raise ImportError("simulated production failure")
            except ImportError as exc:
                handle_route_import_failure("admin.test_group_unavailable", exc)

        assert any(
            entry.get("event") == "admin.test_group_unavailable"
            and entry.get("log_level") == "warning"
            for entry in captured
        ), f"expected WARNING for admin.test_group_unavailable, got: {captured}"

    def test_dev_env_blank_value_falls_through_to_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty BALDUR_ENV value should not trigger fail-fast — only the
        explicit string ``dev``. Guards against accidental fail-fast in
        environments that set the variable to ``""`` to "unset" it."""
        monkeypatch.setenv("BALDUR_ENV", "")

        with capture_logs() as captured:
            try:
                raise ImportError("simulated")
            except ImportError as exc:
                handle_route_import_failure("admin.test_group_unavailable", exc)

        # No exception raised; production warning still emitted.
        assert any(
            entry.get("event") == "admin.test_group_unavailable"
            and entry.get("log_level") == "warning"
            for entry in captured
        )


class TestSimulatedRenameDriftBehavior:
    def test_dev_env_register_all_routes_raises_on_broken_handler_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end fail-fast: when BALDUR_ENV=dev and a handler module is
        broken, register_all_routes propagates the ImportError instead of
        silently skipping the route group."""
        monkeypatch.setenv("BALDUR_ENV", "dev")

        # Force a broken import: replace the recovery handler module with
        # something that lacks the expected names.
        broken_module = type(sys)("baldur.api.handlers.recovery")
        monkeypatch.setitem(sys.modules, "baldur.api.handlers.recovery", broken_module)

        registry = AdminRegistry()
        with pytest.raises(ImportError):
            register_all_routes(registry)
