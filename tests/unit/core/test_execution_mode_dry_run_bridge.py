"""Unit tests for the dry-run → execution-mode bridge (impl doc 603).

Covers the D1 bridge and the D2 guard predicate added to
``baldur.core.execution_mode``:

- ``_resolve_mode()`` / ``get_execution_mode()`` precedence
  (override > runtime dry-run toggle > ``BALDUR_EXECUTION_MODE`` env),
  monotonicity toward observe-only, and the ``mode_source`` resolution.
- ``_is_runtime_dry_run()`` fail-safe behaviour (any error → not dry-run, so
  the toggle never disables healing on error).
- ``intervention_suppressed()`` predicate + its both-halves observability
  contract (fixed-field decision record AND the per-site would-have log).

The behaviours are computed against source constants (``ExecutionMode.*``,
``ReasonCode.POLICY_CONSTRAINT_ACTIVE``), so these are Behavior-class tests.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import structlog

from baldur.core.execution_mode import (
    ExecutionMode,
    _get_mode_from_env,
    _is_runtime_dry_run,
    _resolve_mode,
    clear_execution_mode_override,
    get_execution_mode,
    intervention_suppressed,
    set_execution_mode,
)
from tests.factories import dry_run_active


@contextmanager
def _env_mode(value: str | None):
    """Set ``BALDUR_EXECUTION_MODE`` for the block, clearing the lru_cache.

    ``value=None`` removes the var so the env resolves to the ``active`` default.
    """
    env = {} if value is None else {"BALDUR_EXECUTION_MODE": value}
    with patch.dict("os.environ", env, clear=False):
        if value is None:
            # patch.dict won't remove an inherited var; force the active default.
            import os

            os.environ.pop("BALDUR_EXECUTION_MODE", None)
        _get_mode_from_env.cache_clear()
        try:
            yield
        finally:
            _get_mode_from_env.cache_clear()


@contextmanager
def _toggle(on: bool):
    """Force the runtime dry-run toggle on/off by patching the reader."""
    with patch("baldur.core.execution_mode._is_runtime_dry_run", return_value=on):
        yield


class TestExecutionModeBridge:
    """D1 — single observe-only resolver precedence and monotonicity."""

    def teardown_method(self):
        clear_execution_mode_override()
        _get_mode_from_env.cache_clear()

    # --- Precedence: env × toggle (no override) ---------------------------

    def test_env_active_toggle_off_resolves_to_active_via_env(self):
        # Given an executing env posture and the toggle off
        # When the mode is resolved
        with _env_mode(None), _toggle(False):
            mode, source = _resolve_mode()
        # Then it stays active and is attributed to the env rung
        assert mode.should_execute is True
        assert mode.is_active is True
        assert source == "env"

    def test_env_active_toggle_on_forces_observe_only_via_runtime_toggle(self):
        # Given an executing env posture and the toggle on
        with _env_mode(None), _toggle(True):
            mode, source = _resolve_mode()
        # Then the toggle forces shadow and is attributed to the toggle rung
        assert mode.should_execute is False
        assert mode.is_shadow is True
        assert source == "runtime_toggle"

    def test_env_shadow_toggle_off_stays_shadow_via_env(self):
        with _env_mode("shadow"), _toggle(False):
            mode, source = _resolve_mode()
        assert mode.is_shadow is True
        assert mode.should_execute is False
        assert source == "env"

    def test_env_shadow_toggle_on_is_not_downgraded_and_stays_env(self):
        # Monotonicity: an already-observe-only env is kept as-is; the toggle
        # only forces observe-only over an executing posture, never the reverse.
        with _env_mode("shadow"), _toggle(True):
            mode, source = _resolve_mode()
        assert mode.is_shadow is True
        assert source == "env"

    def test_env_evaluation_toggle_on_preserves_validate_only(self):
        # Monotonicity nuance: evaluation already does not execute, so the toggle
        # keeps it (NOT downgraded to shadow) and validate_only survives.
        with _env_mode("evaluation"), _toggle(True):
            mode, source = _resolve_mode()
        assert mode.is_evaluation is True
        assert mode.validate_only is True
        assert mode.should_execute is False
        assert source == "env"

    # --- Precedence: override wins absolutely -----------------------------

    def test_override_active_beats_toggle_on(self):
        # The programmatic override wins absolutely — it can force-execute even
        # while the runtime toggle is on.
        set_execution_mode(ExecutionMode.active())
        with _env_mode(None), _toggle(True):
            mode, source = _resolve_mode()
        assert mode.should_execute is True
        assert source == "override"

    def test_override_shadow_is_observe_only_via_override_source(self):
        set_execution_mode(ExecutionMode.shadow())
        mode, source = _resolve_mode()
        assert mode.should_execute is False
        assert source == "override"

    def test_get_execution_mode_returns_resolved_mode_only(self):
        # get_execution_mode() is the public face — returns the mode, drops source.
        with _env_mode(None), _toggle(True):
            assert get_execution_mode().is_shadow is True

    # --- Real bridge end-to-end (runtime toggle, not patched) -------------

    def test_real_runtime_toggle_drives_observe_only(self):
        # Drive the genuine D1 bridge through System Control's enable_dry_run().
        with dry_run_active():
            mode, source = _resolve_mode()
        assert mode.should_execute is False
        assert source == "runtime_toggle"
        # Teardown restored an executing posture.
        assert get_execution_mode().should_execute is True


class TestRuntimeDryRunFailSafe:
    """D1 — ``_is_runtime_dry_run()`` is fail-safe (error → not dry-run)."""

    def teardown_method(self):
        clear_execution_mode_override()
        _get_mode_from_env.cache_clear()

    def test_toggle_read_error_falls_back_to_not_dry_run(self):
        # Given the System Control reader raises (cycle / early-init / backend)
        with patch(
            "baldur.services.system_control.is_dry_run",
            side_effect=RuntimeError("backend down"),
        ):
            # Then the fail-safe swallows it and reports "not dry-run"
            assert _is_runtime_dry_run() is False

    def test_toggle_read_error_preserves_healing_in_resolved_mode(self):
        # The error must NOT disable healing — env=active stays executing.
        with (
            _env_mode(None),
            patch(
                "baldur.services.system_control.is_dry_run",
                side_effect=RuntimeError("backend down"),
            ),
        ):
            mode, source = _resolve_mode()
        assert mode.should_execute is True
        assert source == "env"


def _decision_records(logs: list[dict]) -> list[dict]:
    """Parse the fixed-field JSON decision records out of captured logs."""
    out = []
    for entry in logs:
        event = entry.get("event")
        if isinstance(event, str) and event.startswith("{"):
            try:
                out.append(json.loads(event))
            except ValueError:
                continue
    return out


def _would_have_logs(logs: list[dict]) -> list[dict]:
    """Filter the per-site would-have structured logs."""
    return [
        e for e in logs if e.get("event") == "execution_mode.intervention_suppressed"
    ]


class TestInterventionSuppressed:
    """D2 — the shared guard predicate + its observability contract."""

    def teardown_method(self):
        clear_execution_mode_override()
        _get_mode_from_env.cache_clear()

    def test_returns_false_and_silent_when_executing(self):
        # Given an executing mode
        set_execution_mode(ExecutionMode.active())
        # When the predicate is consulted
        with structlog.testing.capture_logs() as logs:
            suppressed = intervention_suppressed("svc", "retry")
        # Then it proceeds (False) and emits nothing
        assert suppressed is False
        assert _decision_records(logs) == []
        assert _would_have_logs(logs) == []

    def test_returns_true_when_observe_only(self):
        set_execution_mode(ExecutionMode.shadow())
        assert intervention_suppressed("svc", "retry") is True

    def test_emits_fixed_field_decision_record_when_suppressed(self):
        # Given observe-only
        set_execution_mode(ExecutionMode.shadow())
        # When suppressed
        with structlog.testing.capture_logs() as logs:
            intervention_suppressed("payment-api", "dlq_store")
        # Then the fixed-field decision record mirrors the action-executor path
        from baldur.core.decision_logger import (
            DecisionBoundaryEventType,
            ReasonCode,
        )

        records = _decision_records(logs)
        evaluated = [
            r
            for r in records
            if r.get("event") == DecisionBoundaryEventType.INTERVENTION_EVALUATED.value
        ]
        assert len(evaluated) == 1
        rec = evaluated[0]
        assert rec["allowed"] is False
        assert rec["reason"] == ReasonCode.POLICY_CONSTRAINT_ACTIVE.value
        assert rec["service_name"] == "payment-api"

    def test_emits_would_have_log_with_action_mode_and_source(self):
        # The half the fixed-field record cannot express: action + mode_source +
        # site-specific would_have context.
        set_execution_mode(ExecutionMode.shadow())
        with structlog.testing.capture_logs() as logs:
            intervention_suppressed(
                "orders", "circuit_breaker_reject", would_reject=True
            )
        would_have = _would_have_logs(logs)
        assert len(would_have) == 1
        entry = would_have[0]
        assert entry["service_name"] == "orders"
        assert entry["action"] == "circuit_breaker_reject"
        assert entry["mode"] == ExecutionMode.shadow().mode.value
        assert entry["mode_source"] == "override"
        assert entry["would_reject"] is True

    def test_mode_source_is_runtime_toggle_under_real_toggle(self):
        # Under the genuine runtime toggle (not an override), mode_source tells
        # the operator a console toggle drove it — not a deployment env var.
        with dry_run_active():
            with structlog.testing.capture_logs() as logs:
                suppressed = intervention_suppressed("svc", "retry")
        assert suppressed is True
        would_have = _would_have_logs(logs)
        assert len(would_have) == 1
        assert would_have[0]["mode_source"] == "runtime_toggle"

    def test_idempotent_predicate_no_side_effect_beyond_logging(self):
        # Calling N times yields the identical verdict; the only effect is the
        # paired log lines (predicate, not a control-flow router / mutator).
        set_execution_mode(ExecutionMode.shadow())
        with structlog.testing.capture_logs() as logs:
            verdicts = [intervention_suppressed("svc", "retry") for _ in range(3)]
        assert verdicts == [True, True, True]
        # Three calls → three would-have logs, three decision records.
        assert len(_would_have_logs(logs)) == 3
        evaluated = [
            r
            for r in _decision_records(logs)
            if r.get("event") == "INTERVENTION_EVALUATED"
        ]
        assert len(evaluated) == 3
