"""Unit tests for the config-history rollback apply rewire (662 D2a).

Target: ``baldur.api.handlers.config_history._apply_config_values`` — the
rewired rollback apply path that delegates the full real-field snapshot to the
manager's generic ``apply_config_values`` (replacing the drifted typed-method
dispatch map whose signatures raised ``TypeError`` on every Pydantic-class
domain).

Verification techniques (§8):
  - §8.5 Dependency interaction — delegation target + actor/values passthrough
  - §8.2 Exception/edge — None-manager (OSS, no PRO backend) raises RuntimeError

The PRO manager is supplied via the real ``ProviderRegistry`` slot (register a
fake "pro" provider) rather than importing baldur_pro — ``safe_get()`` resolves
it, matching the handler's actual lookup, with no PRO dependency.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.api.handlers.config_history import _apply_config_values
from baldur.factory import ProviderRegistry


class TestConfigRollbackApply:
    """``_apply_config_values`` delegates to ``manager.apply_config_values``."""

    def test_delegates_full_snapshot_to_manager(self):
        manager = MagicMock()
        snapshot = {"max_attempts": 5, "backoff_strategy": "exponential"}

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.register("pro", lambda: manager)
            _apply_config_values("retry", snapshot, changed_by="alice")

        manager.apply_config_values.assert_called_once()
        args, kwargs = manager.apply_config_values.call_args
        assert args[0] == "retry"
        assert args[1] == snapshot
        assert kwargs["changed_by"] == "alice"
        assert "retry" in kwargs["reason"]

    def test_default_changed_by_is_system(self):
        manager = MagicMock()

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.register("pro", lambda: manager)
            _apply_config_values("dlq", {"max_replay_attempts": 3})

        _, kwargs = manager.apply_config_values.call_args
        assert kwargs["changed_by"] == "system"

    def test_missing_manager_raises_runtime_error(self):
        """OSS (no PRO RuntimeConfigManager registered) → RuntimeError, not a
        silent no-op rollback."""
        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.reset()
            with pytest.raises(RuntimeError, match="baldur_pro"):
                _apply_config_values("retry", {"max_attempts": 5})


class TestConfigRollbackApplySemanticsBehavior:
    """744 D11/G24 — the rollback response states whether the values it just
    restored reach running processes.

    Rollback is the other incident-path write: an operator reaches it while
    undoing a bad change, and a bare success there reads as "the old value is
    back in effect". The block degrades — to the framework-derived
    ``runtime_apply`` alone — but never to nothing.
    """

    class _StubManager:
        """Only the method ``_default_strategy`` calls."""

        def __init__(self, strategy=None, error=None):
            self._strategy = strategy
            self._error = error

        def get_default_strategy(self, config_type):
            if self._error is not None:
                raise self._error
            return dict(self._strategy or {}, resolved_for=config_type)

    def test_manager_strategy_is_returned_when_the_manager_resolves(self):
        from baldur.api.handlers.config_history import _default_strategy

        manager = self._StubManager(
            {"strategy": "immediate", "runtime_apply": {"mode": "unverified"}}
        )

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.register("pro", lambda: manager)
            block = _default_strategy("circuit_breaker")

        assert block["strategy"] == "immediate"
        assert block["resolved_for"] == "circuit_breaker"

    def test_unregistered_manager_degrades_to_the_framework_block(self):
        """OSS with no PRO manager still owes the statement — the values were
        written either way."""
        from baldur.api.handlers.config_history import _default_strategy

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.reset()
            block = _default_strategy("circuit_breaker")

        assert set(block) == {"runtime_apply"}
        assert block["runtime_apply"]["mode"] == "unverified"

    def test_failing_manager_degrades_to_the_framework_block(self):
        """A registered-but-broken provider must not turn the response into a
        bare success."""
        from baldur.api.handlers.config_history import _default_strategy

        manager = self._StubManager(error=RuntimeError("manager down"))

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.register("pro", lambda: manager)
            block = _default_strategy("circuit_breaker")

        assert set(block) == {"runtime_apply"}
        assert "mode" in block["runtime_apply"]

    @pytest.mark.parametrize(
        "config_type", ["circuit_breaker", "retry", "dlq", "rate_limit"]
    )
    def test_every_domain_gets_a_statement_in_the_degraded_path(self, config_type):
        """Negative assertion across the domains an operator can roll back: none
        of them reports a bare success with no apply-semantics statement."""
        from baldur.api.handlers.config_history import _default_strategy

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.reset()
            block = _default_strategy(config_type)

        assert block["runtime_apply"]["detail"]

    def test_rollback_response_carries_the_block(self):
        """The handler-level assertion: the statement reaches the body, not just
        the helper."""
        from unittest.mock import patch

        from baldur.api.handlers.config_history import config_rollback
        from baldur.interfaces.web_framework import HttpMethod, RequestContext

        class _StubVersion:
            values = {"failure_threshold": 5}
            version = 3

        class _StubHistoryService:
            def is_valid_config_type(self, config_type):
                return True

            def get_version(self, config_type, version):
                return _StubVersion()

            def rollback(self, config_type, target_version, rolled_back_by):
                return _StubVersion()

        ctx = RequestContext(
            method=HttpMethod("POST"),
            path="/config/circuit_breaker/rollback",
            query_params={},
            path_params={"config_type": "circuit_breaker"},
            json_body={"version": 3},
            user=None,
        )

        with (
            patch(
                "baldur.services.config_history.get_config_history_service",
                return_value=_StubHistoryService(),
            ),
            patch(
                "baldur.api.handlers.config_history._apply_config_values",
                return_value=None,
            ),
            ProviderRegistry.runtime_config_manager.snapshot(),
        ):
            ProviderRegistry.runtime_config_manager.reset()
            resp = config_rollback(ctx)

        assert resp.status_code == 200
        assert "runtime_apply" in resp.body["default_strategy"]
