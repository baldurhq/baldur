"""Unit tests for ``baldur.bootstrap`` (#416 Part 1).

Covers:
- ``init()`` idempotency (D2): re-entry is a silent DEBUG no-op.
- Sub-step delegation: each step function is invoked exactly once.
- ``_apply_audit_default_provider()``: re-asserts ``"null"`` when
  ``AuditSettings.enabled=False`` (D11 + D15 defense-in-depth).
- ``_start_audit_pipeline_if_enabled()``: gated on
  ``AuditSettings.enabled``; ImportError → silent skip.
- ``_run_pro_extensions()``: discovers entry-points; failing hooks do
  NOT abort init().
- ``_record_env_snapshot()``: invoked from init() (D21).
- ``reset_init_state()`` flips the flag back so the next init() runs.

These tests live at ``tests/unit/test_bootstrap.py`` because
``baldur.bootstrap`` is a top-level module (no parent package).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_bootstrap_state():
    """Ensure each test starts and ends with a clean bootstrap state."""
    from baldur import bootstrap

    bootstrap.reset_init_state()
    yield
    bootstrap.reset_init_state()


class TestInitContract:
    """Hardcoded contract checks for the ``init()`` public API."""

    def test_init_module_exposes_init_and_reset_only(self):
        """Public surface is ``init`` + ``reset_init_state`` (per D2/D3)."""
        from baldur import bootstrap

        assert "init" in bootstrap.__all__
        assert "reset_init_state" in bootstrap.__all__
        assert callable(bootstrap.init)
        assert callable(bootstrap.reset_init_state)

    def test_baldur_top_level_exposes_init(self):
        """``import baldur; baldur.init()`` must be the entry point."""
        import baldur

        assert hasattr(baldur, "init")
        # Lazy import map should resolve to bootstrap.init (PEP 562).
        assert baldur.init.__module__ == "baldur.bootstrap"

    def test_init_signature_accepts_quarantine_callback(self):
        """``init(quarantine_callback=...)`` is the documented kw arg (D5)."""
        import inspect

        from baldur.bootstrap import init

        sig = inspect.signature(init)
        assert "quarantine_callback" in sig.parameters
        assert sig.parameters["quarantine_callback"].default is None


class TestInitBehavior:
    """Behavior tests using the actual sub-step functions as targets."""

    def test_init_invokes_each_step_once(self):
        """All seven sub-steps fire exactly once on a fresh init()."""
        from baldur import bootstrap

        with (
            patch.object(bootstrap, "_validate_startup_config") as m_validate,
            patch.object(bootstrap, "_register_default_event_handlers") as m_handlers,
            patch.object(bootstrap, "_register_shutdown_handlers") as m_shutdown,
            patch.object(bootstrap, "_run_pro_extensions") as m_pro,
            patch.object(bootstrap, "_apply_audit_default_provider") as m_audit_default,
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled") as m_pipeline,
            patch.object(bootstrap, "_record_env_snapshot") as m_env,
        ):
            bootstrap.init()

        m_validate.assert_called_once_with(quarantine_callback=None)
        m_handlers.assert_called_once_with()
        m_shutdown.assert_called_once_with()
        m_pro.assert_called_once_with()
        m_audit_default.assert_called_once_with()
        m_pipeline.assert_called_once_with()
        m_env.assert_called_once_with()

    def test_init_passes_quarantine_callback_through(self):
        """Quarantine callback is forwarded to ``_validate_startup_config``."""
        from baldur import bootstrap

        callback = MagicMock()
        with (
            patch.object(bootstrap, "_validate_startup_config") as m_validate,
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_run_pro_extensions"),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch.object(bootstrap, "_record_env_snapshot"),
        ):
            bootstrap.init(quarantine_callback=callback)

        m_validate.assert_called_once_with(quarantine_callback=callback)

    def test_init_is_idempotent_no_op_on_reentry(self):
        """D2: second call short-circuits before any sub-step runs."""
        from baldur import bootstrap

        with (
            patch.object(bootstrap, "_validate_startup_config") as m_validate,
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_run_pro_extensions"),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch.object(bootstrap, "_record_env_snapshot"),
        ):
            bootstrap.init()
            bootstrap.init()
            bootstrap.init()

        # Sub-step ran exactly once across three init() calls.
        assert m_validate.call_count == 1

    def test_reset_init_state_allows_subsequent_init(self):
        """``reset_init_state()`` clears ``_init_done`` so init runs again."""
        from baldur import bootstrap

        with (
            patch.object(bootstrap, "_validate_startup_config") as m_validate,
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_run_pro_extensions"),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch.object(bootstrap, "_record_env_snapshot"),
        ):
            bootstrap.init()
            bootstrap.reset_init_state()
            bootstrap.init()

        assert m_validate.call_count == 2

    def test_init_concurrent_calls_run_steps_only_once(self):
        """The threading.Lock + ``_init_done`` flag survives a thread race."""
        from baldur import bootstrap

        call_count = 0
        lock = threading.Lock()

        def _counting_validate(*_a, **_kw):
            nonlocal call_count
            with lock:
                call_count += 1

        with (
            patch.object(
                bootstrap, "_validate_startup_config", side_effect=_counting_validate
            ),
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_run_pro_extensions"),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch.object(bootstrap, "_record_env_snapshot"),
        ):
            threads = [threading.Thread(target=bootstrap.init) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

        assert call_count == 1


class TestApplyAuditDefaultProvider:
    """``_apply_audit_default_provider()`` re-asserts null when disabled."""

    def test_disabled_resets_default_to_null(self):
        """Defense-in-depth: even if a hook flipped to ``file_hashchain``,
        the disabled audit settings force the default back to ``null``."""
        from baldur import bootstrap
        from baldur.factory import ProviderRegistry
        from baldur.settings.audit import override_audit_settings

        prior = ProviderRegistry.audit.get_default_name()
        try:
            ProviderRegistry.audit.set_default("file_hashchain")
            with override_audit_settings(enabled=False):
                bootstrap._apply_audit_default_provider()
            assert ProviderRegistry.audit.get_default_name() == "null"
        finally:
            ProviderRegistry.audit.set_default(prior or "null")

    def test_enabled_leaves_existing_default_alone(self):
        """When ``enabled=True``, the function does NOT override a hook's
        choice of ``file_hashchain``."""
        from baldur import bootstrap
        from baldur.factory import ProviderRegistry
        from baldur.settings.audit import override_audit_settings

        prior = ProviderRegistry.audit.get_default_name()
        try:
            ProviderRegistry.audit.set_default("file_hashchain")
            with override_audit_settings(enabled=True):
                bootstrap._apply_audit_default_provider()
            assert ProviderRegistry.audit.get_default_name() == "file_hashchain"
        finally:
            ProviderRegistry.audit.set_default(prior or "null")

    def test_disabled_default_already_null_is_noop(self):
        """``set_default("null")`` is NOT called when default is already null."""
        from baldur import bootstrap
        from baldur.factory import ProviderRegistry
        from baldur.settings.audit import override_audit_settings

        prior = ProviderRegistry.audit.get_default_name()
        try:
            ProviderRegistry.audit.set_default("null")
            with override_audit_settings(enabled=False):
                with patch.object(ProviderRegistry.audit, "set_default") as m_set:
                    bootstrap._apply_audit_default_provider()
            # No-op: don't churn the default when it's already correct.
            m_set.assert_not_called()
        finally:
            ProviderRegistry.audit.set_default(prior or "null")


class TestStartAuditPipelineIfEnabled:
    """``_start_audit_pipeline_if_enabled()`` gating + import-safe path."""

    def test_disabled_skips_without_calling_lifecycle(self):
        """When ``enabled=False`` the lifecycle entry point is never imported."""
        from baldur import bootstrap
        from baldur.settings.audit import override_audit_settings

        with override_audit_settings(enabled=False):
            with patch(
                "baldur.audit.async_audit_lifecycle.startup_async_audit_system"
            ) as m_start:
                bootstrap._start_audit_pipeline_if_enabled()
        m_start.assert_not_called()

    def test_enabled_invokes_startup_async_audit_system(self):
        """When ``enabled=True`` the lifecycle starts."""
        from baldur import bootstrap
        from baldur.settings.audit import override_audit_settings

        with override_audit_settings(enabled=True):
            with patch(
                "baldur.audit.async_audit_lifecycle.startup_async_audit_system",
                return_value=True,
            ) as m_start:
                bootstrap._start_audit_pipeline_if_enabled()
        m_start.assert_called_once_with()

    def test_lifecycle_exception_does_not_propagate(self):
        """Exceptions from lifecycle are logged at WARNING but swallowed."""
        from baldur import bootstrap
        from baldur.settings.audit import override_audit_settings

        with override_audit_settings(enabled=True):
            with patch(
                "baldur.audit.async_audit_lifecycle.startup_async_audit_system",
                side_effect=RuntimeError("boom"),
            ):
                # Must NOT raise.
                bootstrap._start_audit_pipeline_if_enabled()


class TestRunProExtensions:
    """``_run_pro_extensions()`` entry-point discovery + failure isolation."""

    def test_no_entry_points_is_silent(self):
        """No registered hooks → function returns without error."""
        from baldur import bootstrap

        with patch("importlib.metadata.entry_points", return_value=[]):
            # Must not raise.
            bootstrap._run_pro_extensions()

    def test_hook_invocation_propagates_no_args(self):
        """Each hook is loaded and invoked with zero arguments."""
        from baldur import bootstrap

        invoked: list[bool] = []

        def fake_hook():
            invoked.append(True)

        fake_ep = SimpleNamespace(name="audit_enabled", load=lambda: fake_hook)

        with patch(
            "importlib.metadata.entry_points",
            return_value=[fake_ep],
        ):
            bootstrap._run_pro_extensions()

        assert invoked == [True]

    def test_failing_hook_does_not_abort_run(self):
        """One failing hook does not block subsequent hooks (D4)."""
        from baldur import bootstrap

        invoked: list[str] = []

        def bad_hook():
            raise RuntimeError("intentional")

        def good_hook():
            invoked.append("good")

        eps = [
            SimpleNamespace(name="bad", load=lambda: bad_hook),
            SimpleNamespace(name="good", load=lambda: good_hook),
        ]
        with patch(
            "importlib.metadata.entry_points",
            return_value=eps,
        ):
            bootstrap._run_pro_extensions()

        assert invoked == ["good"]


class TestRecordEnvSnapshot:
    """``_record_env_snapshot()`` (D21): invoked from init(), best-effort."""

    def test_init_records_env_snapshot(self):
        """D21: ``log_env_snapshot_to_audit`` is called from init()."""
        from baldur import bootstrap

        with (
            patch.object(bootstrap, "_validate_startup_config"),
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_run_pro_extensions"),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch(
                "baldur.audit.env_snapshot.log_env_snapshot_to_audit",
                return_value=True,
            ) as m_log,
        ):
            bootstrap.init()

        m_log.assert_called_once_with()

    def test_record_env_snapshot_swallows_exceptions(self):
        """An env_snapshot failure must not abort init()."""
        from baldur import bootstrap

        with patch(
            "baldur.audit.env_snapshot.log_env_snapshot_to_audit",
            side_effect=RuntimeError("disk full"),
        ):
            # Must not raise.
            bootstrap._record_env_snapshot()


# =============================================================================
# Contract — ExtensionResult (#418 P1-1)
# =============================================================================


class TestExtensionResultContract:
    """ExtensionResult dataclass contract (#418 P1-1)."""

    def test_extension_result_defaults_empty_lists(self):
        """ExtensionResult fields default to empty lists."""
        from baldur.bootstrap import ExtensionResult

        result = ExtensionResult()
        assert result.found == []
        assert result.executed == []
        assert result.failed == []


# =============================================================================
# Behavior — _run_pro_extensions returns ExtensionResult (#418 P1-1)
# =============================================================================


class TestRunProExtensionsReturnBehavior:
    """_run_pro_extensions() returns ExtensionResult (#418 P1-1)."""

    def test_returns_extension_result(self):
        """_run_pro_extensions() returns an ExtensionResult instance."""
        from baldur.bootstrap import ExtensionResult, _run_pro_extensions

        result = _run_pro_extensions()
        assert isinstance(result, ExtensionResult)

    def test_no_hooks_returns_empty_result(self):
        """When no hooks are registered, all lists are empty."""
        from baldur.bootstrap import _run_pro_extensions

        with patch(
            "importlib.metadata.entry_points",
            return_value=[],
        ):
            result = _run_pro_extensions()
        assert result.found == []
        assert result.executed == []
        assert result.failed == []

    def test_successful_hook_in_found_and_executed(self):
        """Successful hook appears in both found and executed."""
        from baldur.bootstrap import _run_pro_extensions

        mock_hook = MagicMock()
        mock_hook.name = "test_hook"
        mock_hook.load.return_value = lambda: None

        with patch(
            "importlib.metadata.entry_points",
            return_value=[mock_hook],
        ):
            result = _run_pro_extensions()
        assert "test_hook" in result.found
        assert "test_hook" in result.executed
        assert result.failed == []

    def test_failing_hook_in_found_and_failed(self):
        """Failing hook appears in found and failed, not executed."""
        from baldur.bootstrap import _run_pro_extensions

        mock_hook = MagicMock()
        mock_hook.name = "bad_hook"
        mock_hook.load.return_value = MagicMock(side_effect=RuntimeError("boom"))

        with patch(
            "importlib.metadata.entry_points",
            return_value=[mock_hook],
        ):
            result = _run_pro_extensions()
        assert "bad_hook" in result.found
        assert "bad_hook" in result.failed
        assert "bad_hook" not in result.executed


# =============================================================================
# Behavior — _build_startup_report (#418 P1-1)
# =============================================================================


class TestBuildStartupReportBehavior:
    """_build_startup_report() behavior (#418 P1-1)."""

    def test_report_contains_extensions_key(self):
        """Report dict has 'extensions' with found/executed/failed."""
        from baldur.bootstrap import ExtensionResult, _build_startup_report

        ext = ExtensionResult(found=["hook_a"], executed=["hook_a"], failed=[])
        report = _build_startup_report(ext)
        assert "extensions" in report
        assert report["extensions"]["found"] == ["hook_a"]
        assert report["extensions"]["executed"] == ["hook_a"]
        assert report["extensions"]["failed"] == []

    def test_report_contains_features_key(self):
        """Report dict has 'features' with boolean values."""
        from baldur.bootstrap import ExtensionResult, _build_startup_report

        ext = ExtensionResult()
        report = _build_startup_report(ext)
        assert "features" in report
        features = report["features"]
        # All feature scan entries should be present
        assert "audit" in features
        assert "error_budget_gate" in features
        assert "compliance" in features
        assert "governance" in features
        assert "chaos" in features
        assert "correlation_engine" in features
        # Values should be booleans
        for v in features.values():
            assert isinstance(v, bool)

    def test_report_features_false_on_import_error(self):
        """Features default to False when settings module import fails."""
        from baldur.bootstrap import ExtensionResult, _build_startup_report

        ext = ExtensionResult()
        with patch("importlib.import_module", side_effect=ImportError("missing")):
            report = _build_startup_report(ext)
        for v in report["features"].values():
            assert v is False

    def test_init_emits_startup_report_log(self):
        """init() emits baldur.startup_report INFO log (#418 P1-1)."""
        from baldur import bootstrap

        with (
            patch.object(bootstrap, "_validate_startup_config"),
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(
                bootstrap,
                "_run_pro_extensions",
                return_value=bootstrap.ExtensionResult(),
            ),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch.object(bootstrap, "_record_env_snapshot"),
            patch.object(
                bootstrap, "_build_startup_report", return_value={}
            ) as m_report,
            patch.object(bootstrap, "logger") as m_logger,
        ):
            bootstrap.init()

        m_report.assert_called_once()
        m_logger.info.assert_any_call("baldur.startup_report")


# =============================================================================
# Behavior — runtime lifecycle (#450 Phase 1, D1)
# =============================================================================


class TestBootstrapRuntimeLifecycleBehavior:
    """``init()`` populates a runtime; ``reset_init_state()`` drops it.

    Per 450 D1: ``init()`` must lazy-create a ``BaldurRuntime`` (so re-entrant
    init under the same Context observes the same identity) and
    ``reset_init_state()`` must wipe both ``_init_done`` and the active
    runtime so a subsequent init rebuilds from scratch.
    """

    def test_init_ensures_runtime_exists_after_call(self):
        """After ``init()``, ``get_runtime()`` returns a usable ``BaldurRuntime``."""
        from baldur import bootstrap
        from baldur.runtime import BaldurRuntime, get_runtime

        with (
            patch.object(bootstrap, "_validate_startup_config"),
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_run_pro_extensions"),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch.object(bootstrap, "_record_env_snapshot"),
        ):
            bootstrap.init()

        assert isinstance(get_runtime(), BaldurRuntime)

    def test_init_keeps_same_runtime_identity_across_reentry(self):
        """Re-entrant ``init()`` under the same Context preserves runtime identity."""
        from baldur import bootstrap
        from baldur.runtime import get_runtime

        with (
            patch.object(bootstrap, "_validate_startup_config"),
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_run_pro_extensions"),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch.object(bootstrap, "_record_env_snapshot"),
        ):
            bootstrap.init()
            first = get_runtime()
            bootstrap.init()  # idempotent re-entry
            second = get_runtime()

        assert first is second

    def test_reset_init_state_drops_active_runtime(self):
        """``reset_init_state()`` calls ``reset_runtime()`` so the next init rebuilds."""
        from baldur import bootstrap
        from baldur.runtime import get_runtime

        with (
            patch.object(bootstrap, "_validate_startup_config"),
            patch.object(bootstrap, "_register_default_event_handlers"),
            patch.object(bootstrap, "_register_shutdown_handlers"),
            patch.object(bootstrap, "_run_pro_extensions"),
            patch.object(bootstrap, "_apply_audit_default_provider"),
            patch.object(bootstrap, "_start_audit_pipeline_if_enabled"),
            patch.object(bootstrap, "_record_env_snapshot"),
        ):
            bootstrap.init()
            first_runtime = get_runtime()

            bootstrap.reset_init_state()

            bootstrap.init()
            second_runtime = get_runtime()

        # The runtime must have been replaced — proves reset_runtime() ran.
        assert first_runtime is not second_runtime

    def test_reset_init_state_invokes_reset_runtime(self):
        """``reset_init_state`` delegates to ``baldur.runtime.reset_runtime``."""
        from baldur import bootstrap

        with patch("baldur.runtime.reset_runtime") as m_reset_runtime:
            bootstrap.reset_init_state()

        m_reset_runtime.assert_called_once_with()


class TestValidateClusterIdentityIfNamespaced:
    """``_validate_cluster_identity_if_namespaced`` (453 D5) state matrix.

    The function is the single bootstrap-side caller that decides whether to
    invoke ``ClusterIdentity.validate`` and — on non-fatal failure — flip
    ``set_quarantine_mode(True)``. The factory body no longer performs this
    check, so this function is the only place the env-read + global mutation
    pair lives, with timing fully under bootstrap's control. Validation runs
    only when namespace isolation is enabled; the autouse fixture defaults
    these tests to namespaced mode, and the disabled-short-circuit test
    overrides it.
    """

    def _stub_runtime(self, *, is_test_mode: bool):
        rt = MagicMock()
        rt.is_test_mode = is_test_mode
        return rt

    def _ns(self, *, enabled: bool):
        ns = MagicMock()
        ns.namespace_enabled = enabled
        return ns

    @pytest.fixture(autouse=True)
    def _namespaced_by_default(self):
        """Default to ``namespace_enabled=True`` so the validation path runs;
        individual tests re-patch with ``enabled=False`` to exercise the gate."""
        with patch(
            "baldur.settings.namespace.get_namespace_settings",
            return_value=self._ns(enabled=True),
        ):
            yield

    def test_namespace_disabled_short_circuits_before_validation(self):
        """``namespace_enabled=False`` (the zero-config default) skips validate.

        Regression guard for the quickstart fix: the documented zero-infra
        path sets no BALDUR_CLUSTER_ID / BALDUR_NAMESPACE_REGION, and must not
        be fail-fast aborted because no namespace collision surface exists.
        """
        from baldur import bootstrap

        with (
            patch(
                "baldur.runtime.get_runtime",
                return_value=self._stub_runtime(is_test_mode=False),
            ),
            patch(
                "baldur.settings.namespace.get_namespace_settings",
                return_value=self._ns(enabled=False),
            ),
            patch(
                "baldur.core.cluster_identity.get_cluster_identity"
            ) as m_get_identity,
            patch(
                "baldur.core.cluster_identity.set_quarantine_mode"
            ) as m_set_quarantine,
        ):
            bootstrap._validate_cluster_identity_if_namespaced()

        m_get_identity.assert_not_called()
        m_set_quarantine.assert_not_called()

    def test_test_mode_short_circuits_before_validation(self):
        """``runtime.is_test_mode=True`` skips validate + quarantine flip entirely."""
        from baldur import bootstrap

        with (
            patch(
                "baldur.runtime.get_runtime",
                return_value=self._stub_runtime(is_test_mode=True),
            ),
            patch(
                "baldur.core.cluster_identity.get_cluster_identity"
            ) as m_get_identity,
            patch(
                "baldur.core.cluster_identity.set_quarantine_mode"
            ) as m_set_quarantine,
        ):
            bootstrap._validate_cluster_identity_if_namespaced()

        m_get_identity.assert_not_called()
        m_set_quarantine.assert_not_called()

    def test_production_valid_identity_does_not_flip_quarantine(self):
        """Valid identity → ``validate`` returns True, no quarantine flip."""
        from baldur import bootstrap

        identity = MagicMock()
        identity.validate.return_value = True

        with (
            patch(
                "baldur.runtime.get_runtime",
                return_value=self._stub_runtime(is_test_mode=False),
            ),
            patch(
                "baldur.core.cluster_identity.get_cluster_identity",
                return_value=identity,
            ),
            patch(
                "baldur.core.cluster_identity.set_quarantine_mode"
            ) as m_set_quarantine,
            patch.dict("os.environ", {"BALDUR_FAIL_FAST": "false"}, clear=False),
        ):
            bootstrap._validate_cluster_identity_if_namespaced()

        identity.validate.assert_called_once_with(fail_fast=False)
        m_set_quarantine.assert_not_called()

    def test_production_invalid_identity_with_fail_fast_false_flips_quarantine(self):
        """fail_fast=False + invalid → ``set_quarantine_mode(True)`` is invoked."""
        from baldur import bootstrap

        identity = MagicMock()
        identity.validate.return_value = False

        with (
            patch(
                "baldur.runtime.get_runtime",
                return_value=self._stub_runtime(is_test_mode=False),
            ),
            patch(
                "baldur.core.cluster_identity.get_cluster_identity",
                return_value=identity,
            ),
            patch(
                "baldur.core.cluster_identity.set_quarantine_mode"
            ) as m_set_quarantine,
            patch.dict("os.environ", {"BALDUR_FAIL_FAST": "false"}, clear=False),
        ):
            bootstrap._validate_cluster_identity_if_namespaced()

        identity.validate.assert_called_once_with(fail_fast=False)
        m_set_quarantine.assert_called_once_with(True)

    def test_production_fail_fast_true_propagates_system_exit(self):
        """fail_fast=True path: ``SystemExit`` from validate must propagate.

        Wrapping it in the broad ``except Exception`` would silently swallow
        the deliberate-abort signal — the implementation re-raises explicitly.
        """
        from baldur import bootstrap

        identity = MagicMock()
        identity.validate.side_effect = SystemExit(1)

        with (
            patch(
                "baldur.runtime.get_runtime",
                return_value=self._stub_runtime(is_test_mode=False),
            ),
            patch(
                "baldur.core.cluster_identity.get_cluster_identity",
                return_value=identity,
            ),
            patch(
                "baldur.core.cluster_identity.set_quarantine_mode"
            ) as m_set_quarantine,
            patch.dict("os.environ", {"BALDUR_FAIL_FAST": "true"}, clear=False),
        ):
            with pytest.raises(SystemExit):
                bootstrap._validate_cluster_identity_if_namespaced()

        identity.validate.assert_called_once_with(fail_fast=True)
        # SystemExit is the abort signal — quarantine flip belongs to the
        # non-fatal path only.
        m_set_quarantine.assert_not_called()

    def test_production_unexpected_exception_is_swallowed(self):
        """Non-SystemExit exception from validate is logged at WARNING, not raised."""
        from baldur import bootstrap

        identity = MagicMock()
        identity.validate.side_effect = RuntimeError("validator boom")

        with (
            patch(
                "baldur.runtime.get_runtime",
                return_value=self._stub_runtime(is_test_mode=False),
            ),
            patch(
                "baldur.core.cluster_identity.get_cluster_identity",
                return_value=identity,
            ),
            patch(
                "baldur.core.cluster_identity.set_quarantine_mode"
            ) as m_set_quarantine,
            patch.dict("os.environ", {"BALDUR_FAIL_FAST": "false"}, clear=False),
        ):
            # Must NOT raise — bootstrap continues.
            bootstrap._validate_cluster_identity_if_namespaced()

        m_set_quarantine.assert_not_called()

    @pytest.mark.parametrize(
        ("env_value", "expected_fail_fast"),
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("false", False),
            ("False", False),
            ("anything-else", False),
        ],
    )
    def test_baldur_fail_fast_env_resolves_case_insensitive_lowercase_true(
        self, env_value, expected_fail_fast
    ):
        """``BALDUR_FAIL_FAST`` only resolves to True when ``.lower() == "true"``."""
        from baldur import bootstrap

        identity = MagicMock()
        identity.validate.return_value = True

        with (
            patch(
                "baldur.runtime.get_runtime",
                return_value=self._stub_runtime(is_test_mode=False),
            ),
            patch(
                "baldur.core.cluster_identity.get_cluster_identity",
                return_value=identity,
            ),
            patch("baldur.core.cluster_identity.set_quarantine_mode"),
            patch.dict("os.environ", {"BALDUR_FAIL_FAST": env_value}, clear=False),
        ):
            bootstrap._validate_cluster_identity_if_namespaced()

        identity.validate.assert_called_once_with(fail_fast=expected_fail_fast)

    def test_baldur_fail_fast_default_is_true_when_unset(self, monkeypatch):
        """Missing BALDUR_FAIL_FAST defaults to ``fail_fast=True``."""
        from baldur import bootstrap

        monkeypatch.delenv("BALDUR_FAIL_FAST", raising=False)

        identity = MagicMock()
        identity.validate.return_value = True

        with (
            patch(
                "baldur.runtime.get_runtime",
                return_value=self._stub_runtime(is_test_mode=False),
            ),
            patch(
                "baldur.core.cluster_identity.get_cluster_identity",
                return_value=identity,
            ),
            patch("baldur.core.cluster_identity.set_quarantine_mode"),
        ):
            bootstrap._validate_cluster_identity_if_namespaced()

        identity.validate.assert_called_once_with(fail_fast=True)


class TestRegisterShutdownHandlersWiresSignals:
    """``_register_shutdown_handlers`` must end by calling
    ``coordinator.register_signals()`` so OS signals reach the coordinator.

    Per ``CLAUDE.md`` § Pattern Compliance — Startup wiring:
    ``Defined-but-uncalled setup functions ... are bugs — not future
    work.`` Prior to this wiring, ``register_signals`` had zero callers
    in the production code path.
    """

    def test_register_signals_called_after_handler_registration(self):
        from baldur import bootstrap

        coordinator = MagicMock()
        with patch(
            "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
            return_value=coordinator,
        ):
            bootstrap._register_shutdown_handlers()

        coordinator.register_signals.assert_called_once_with()

    def test_register_signals_failure_does_not_abort_init(self):
        """A signal-registration failure must be logged and swallowed —
        the rest of the bootstrap pipeline must still proceed."""
        from baldur import bootstrap

        coordinator = MagicMock()
        coordinator.register_signals.side_effect = RuntimeError("boom")
        with patch(
            "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
            return_value=coordinator,
        ):
            # No exception escapes
            bootstrap._register_shutdown_handlers()

        coordinator.register_signals.assert_called_once_with()
