"""Strict unknown-key validation across the admin config-write handlers.

Regression coverage for the silent-drop / opaque-500 config-write bug: a
PATCH/PUT with a mistyped key previously returned ``200 success`` while the
downstream ``hasattr``-filtered sink silently applied nothing (or, on the
typed-kwargs sinks, raised ``TypeError`` surfaced as an opaque 500). Every
config-write handler now pre-validates the body — any unknown key rejects the
WHOLE body with 400 (listing ``unknown_fields`` + ``allowed_fields``, nothing
applied), and an all-valid body applies fully and echoes ``updated_fields``.

The allowed set is derived at runtime from the registry-resolved sink: its
config dataclass fields, its method signature, or (for the error-budget-gate
exemplar) a curated static allowlist. The sinks here are test-local stubs, so
this file exercises the handler-side mechanism without any private-tier
dependency and runs identically with or without those sinks installed.

Test targets:
    - api.handlers._common: dataclass_field_names, reject_unknown_config_keys,
      reject_unknown_kwargs (including the **kwargs skip).
    - The 9 strict config-write endpoints: chaos_config (4), chaos_safety (3),
      error_budget_reconciliation (1), error_budget_gate (1).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from baldur.api.handlers import (
    chaos_config,
    chaos_safety,
    error_budget_gate,
    error_budget_reconciliation,
)
from baldur.api.handlers._common import (
    dataclass_field_names,
    reject_unknown_config_keys,
    reject_unknown_kwargs,
)
from baldur.factory.registry import ProviderRegistry
from baldur.interfaces.web_framework import HttpMethod, RequestContext

_UNKNOWN_KEY = "definitely_not_a_config_field"


def _make_ctx(method: str = "PATCH", json_body: dict | None = None) -> RequestContext:
    return RequestContext(
        method=HttpMethod(method),
        path="/config/",
        query_params={},
        path_params={},
        json_body=json_body,
    )


@dataclass
class _StubConfig:
    """Test-local config dataclass; handlers derive the allowlist from its fields."""

    enabled: bool = True
    max_duration_seconds: int = 300

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class _ConfigSink:
    """get_config/update_config sink (safety guard, scheduler, report, reconciliation)."""

    def __init__(self):
        self.applied: dict | None = None

    def get_config(self) -> _StubConfig:
        return _StubConfig()

    def update_config(self, **kwargs) -> _StubConfig:
        self.applied = kwargs
        return _StubConfig()


class _PolicySink:
    """get_policy/update_policy sink (blast radius)."""

    def __init__(self):
        self.applied: dict | None = None

    def get_policy(self) -> _StubConfig:
        return _StubConfig()

    def update_policy(self, **kwargs) -> _StubConfig:
        self.applied = kwargs
        return _StubConfig()


class _StubRuntimeConfigManager:
    """Typed-kwargs sink; the handler derives the allowlist from these signatures."""

    def __init__(self):
        self.applied: dict | None = None

    def update_chaos_stop_conditions_config(
        self, enabled=None, error_rate_threshold=None
    ) -> dict:
        self.applied = {
            "enabled": enabled,
            "error_rate_threshold": error_rate_threshold,
        }
        return dict(self.applied)

    def update_chaos_ttl_config(
        self, default_ttl_seconds=None, max_ttl_seconds=None
    ) -> dict:
        self.applied = {
            "default_ttl_seconds": default_ttl_seconds,
            "max_ttl_seconds": max_ttl_seconds,
        }
        return dict(self.applied)

    def update_chaos_dry_run_config(self, enabled=None, force_dry_run=None) -> dict:
        self.applied = {"enabled": enabled, "force_dry_run": force_dry_run}
        return dict(self.applied)


def _endpoint_case(case_id: str):
    """Return (patcher, sink, handler, method, valid_body) for an endpoint id."""
    if case_id == "safety_guard":
        sink = _ConfigSink()
        patcher = patch.object(
            ProviderRegistry.safety_guard, "safe_get", return_value=sink
        )
        return (
            patcher,
            sink,
            chaos_config.safety_guard_config_update,
            "PATCH",
            {"enabled": False},
        )
    if case_id == "blast_radius":
        sink = _PolicySink()
        patcher = patch.object(
            ProviderRegistry.blast_radius_manager, "safe_get", return_value=sink
        )
        return (
            patcher,
            sink,
            chaos_config.chaos_blast_radius_policy_update,
            "PATCH",
            {"max_duration_seconds": 120},
        )
    if case_id == "scheduler":
        sink = _ConfigSink()
        patcher = patch.object(
            ProviderRegistry.chaos_scheduler, "safe_get", return_value=sink
        )
        return (
            patcher,
            sink,
            chaos_config.scheduler_config_update,
            "PATCH",
            {"enabled": True},
        )
    if case_id == "report":
        sink = _ConfigSink()
        patcher = patch.object(
            ProviderRegistry.report_generator, "safe_get", return_value=sink
        )
        return (
            patcher,
            sink,
            chaos_config.report_config_update,
            "PATCH",
            {"enabled": True},
        )
    if case_id == "reconciliation":
        sink = _ConfigSink()
        patcher = patch(
            "baldur.api.handlers.error_budget_reconciliation._service",
            return_value=sink,
        )
        return (
            patcher,
            sink,
            error_budget_reconciliation.reconciliation_config_update,
            "PUT",
            {"enabled": False},
        )
    if case_id == "stop_conditions":
        sink = _StubRuntimeConfigManager()
        patcher = patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=sink
        )
        return (
            patcher,
            sink,
            chaos_safety.stop_conditions_config_update,
            "PATCH",
            {"enabled": True},
        )
    if case_id == "ttl":
        sink = _StubRuntimeConfigManager()
        patcher = patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=sink
        )
        return (
            patcher,
            sink,
            chaos_safety.ttl_config_update,
            "PATCH",
            {"default_ttl_seconds": 60},
        )
    if case_id == "dry_run":
        sink = _StubRuntimeConfigManager()
        patcher = patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=sink
        )
        return (
            patcher,
            sink,
            chaos_safety.dry_run_config_update,
            "PATCH",
            {"enabled": True},
        )
    if case_id == "error_budget_gate":
        sink = _ConfigSink()
        patcher = patch.object(
            ProviderRegistry.error_budget_gate, "safe_get", return_value=sink
        )
        return (
            patcher,
            sink,
            error_budget_gate.gate_config_update,
            "PUT",
            {"enabled": True},
        )
    raise AssertionError(f"unknown endpoint case: {case_id}")


_ENDPOINT_IDS = [
    "safety_guard",
    "blast_radius",
    "scheduler",
    "report",
    "reconciliation",
    "stop_conditions",
    "ttl",
    "dry_run",
    "error_budget_gate",
]


class TestAdminConfigStrictValidation:
    """Endpoint x body-shape matrix: unknown keys reject atomically with 400."""

    @pytest.mark.parametrize("case_id", _ENDPOINT_IDS, ids=_ENDPOINT_IDS)
    def test_only_unknown_field_returns_400_and_applies_nothing(self, case_id):
        """A body of only unknown keys is rejected outright; the sink is untouched."""
        patcher, sink, handler, method, _ = _endpoint_case(case_id)

        with patcher:
            resp = handler(_make_ctx(method, json_body={_UNKNOWN_KEY: 1}))

        assert resp.status_code == 400
        assert resp.body["unknown_fields"] == [_UNKNOWN_KEY]
        assert sink.applied is None

    @pytest.mark.parametrize("case_id", _ENDPOINT_IDS, ids=_ENDPOINT_IDS)
    def test_unknown_field_riding_valid_fields_rejects_the_whole_body(self, case_id):
        """A typo riding valid keys rejects everything — no partial application."""
        patcher, sink, handler, method, valid_body = _endpoint_case(case_id)
        mixed_body = {**valid_body, _UNKNOWN_KEY: "typo"}

        with patcher:
            resp = handler(_make_ctx(method, json_body=mixed_body))

        assert resp.status_code == 400
        assert resp.body["unknown_fields"] == [_UNKNOWN_KEY]
        assert set(valid_body) <= set(resp.body["allowed_fields"])
        assert sink.applied is None

    @pytest.mark.parametrize("case_id", _ENDPOINT_IDS, ids=_ENDPOINT_IDS)
    def test_all_valid_body_applies_fully_and_echoes_updated_fields(self, case_id):
        """A fully valid body reaches the sink and echoes the applied field names."""
        patcher, sink, handler, method, valid_body = _endpoint_case(case_id)

        with patcher:
            resp = handler(_make_ctx(method, json_body=valid_body))

        assert resp.status_code == 200
        assert resp.body["status"] == "success"
        assert resp.body["updated_fields"] == sorted(valid_body)
        assert sink.applied is not None
        for key, value in valid_body.items():
            assert sink.applied[key] == value


class TestConfigValidationHelpers:
    """_common helper behavior: allowlist derivation and rejection semantics."""

    def test_dataclass_field_names_returns_the_field_set(self):
        assert dataclass_field_names(_StubConfig()) == {
            "enabled",
            "max_duration_seconds",
        }

    def test_reject_unknown_config_keys_passes_a_fully_valid_body(self):
        result = reject_unknown_config_keys(
            {"enabled": True}, {"enabled", "other"}, config_label="test"
        )

        assert result is None

    def test_reject_unknown_config_keys_rejects_and_lists_both_field_sets(self):
        result = reject_unknown_config_keys(
            {"enabled": True, _UNKNOWN_KEY: 1},
            {"enabled"},
            config_label="test",
        )

        assert result is not None
        assert result.status_code == 400
        assert result.body["unknown_fields"] == [_UNKNOWN_KEY]
        assert result.body["allowed_fields"] == ["enabled"]

    def test_reject_unknown_kwargs_derives_the_allowlist_from_the_signature(self):
        sink = _StubRuntimeConfigManager()

        result = reject_unknown_kwargs(
            {_UNKNOWN_KEY: 1},
            sink.update_chaos_ttl_config,
            config_label="ttl",
        )

        assert result is not None
        assert result.status_code == 400
        assert result.body["allowed_fields"] == [
            "default_ttl_seconds",
            "max_ttl_seconds",
        ]

    def test_reject_unknown_kwargs_skips_validation_for_var_keyword_sinks(self):
        """A **kwargs method can accept any key, so validation is skipped."""

        def open_sink(**kwargs):
            return kwargs

        result = reject_unknown_kwargs(
            {_UNKNOWN_KEY: 1}, open_sink, config_label="open"
        )

        assert result is None
