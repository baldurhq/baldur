"""``init()`` Step 5 reports whether the audit trail actually reaches a backend.

Source: ``src/baldur/bootstrap.py`` — ``_apply_audit_default_provider`` and
``_set_audit_backend_wired_gauge``.

Audit enabled while the resolved default provider is still the no-op adapter
is the one combination that silently voids the trail: records are written,
accepted, and reach nothing. Step 5 reports it twice — a WARNING naming the
condition, and the ``audit_backend_wired`` gauge, which is the channel an
alert can watch. A boot log line is the weakest possible signal for the state
that voids the compliance product.

Companion files: ``tests/unit/test_bootstrap.py::TestApplyAuditDefaultProvider``
covers the disabled-path re-assert this function has always performed;
``tests/unit/metrics/test_audit_backend_metrics.py`` covers the gauge module
itself (series name, both verdicts, the no-prometheus fallback).

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.4 Side effects (WARNING emission, gauge writes) over a truth table of
  ``enabled`` x resolved default name.
- §8.2 Exception/edge cases (the gauge write is fail-open).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur import bootstrap
from baldur.factory import ProviderRegistry
from baldur.settings.audit import override_audit_settings

# The event name an alert or log filter matches on.
_UNWIRED_EVENT = "audit.backend_unwired"


@pytest.fixture
def audit_default():
    """Set the audit registry default per test and restore it afterwards."""
    prior = ProviderRegistry.audit.get_default_name()

    def _set(name: str) -> None:
        ProviderRegistry.audit.set_default(name)

    yield _set
    ProviderRegistry.audit.set_default(prior or "null")


def _unwired_warnings(mock_logger) -> list:
    return [
        call
        for call in mock_logger.warning.call_args_list
        if call.args and call.args[0] == _UNWIRED_EVENT
    ]


class TestAuditBackendUnwiredSignalBehavior:
    """The WARNING fires on exactly one cell of the truth table."""

    def test_enabled_with_null_default_warns_naming_the_condition(self, audit_default):
        # Given: the master switch on, the no-op adapter resolved
        audit_default("null")

        # When
        with (
            override_audit_settings(enabled=True),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._apply_audit_default_provider()

        # Then
        warnings = _unwired_warnings(mock_logger)
        assert len(warnings) == 1
        assert warnings[0].kwargs["provider"] == "null"
        assert (
            warnings[0].kwargs["reason"] == "audit_enabled_but_default_provider_is_noop"
        )

    def test_enabled_with_real_default_does_not_warn(self, audit_default):
        """The control arm — a wired process must stay quiet, or the WARNING
        is noise and gets filtered out before it ever matters."""
        audit_default("file_hashchain")

        with (
            override_audit_settings(enabled=True),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._apply_audit_default_provider()

        assert _unwired_warnings(mock_logger) == []

    def test_disabled_with_null_default_does_not_warn(self, audit_default):
        """Audit off is a deliberate operator choice, not a defect."""
        audit_default("null")

        with (
            override_audit_settings(enabled=False),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._apply_audit_default_provider()

        assert _unwired_warnings(mock_logger) == []

    def test_disabled_revokes_a_promotion_without_warning(self, audit_default):
        """The revoked default is ``"null"``, which is the WARNING's own
        trigger value — the ``enabled`` guard is what keeps it quiet."""
        audit_default("file_hashchain")

        with (
            override_audit_settings(enabled=False),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._apply_audit_default_provider()

        assert ProviderRegistry.audit.get_default_name() == "null"
        assert _unwired_warnings(mock_logger) == []


class TestAuditBackendWiredGaugeBehavior:
    """The gauge carries both verdicts, and never breaks ``init()``."""

    def test_enabled_with_null_default_publishes_zero(self, audit_default):
        audit_default("null")

        with (
            override_audit_settings(enabled=True),
            patch.object(bootstrap, "_set_audit_backend_wired_gauge") as mock_gauge,
        ):
            bootstrap._apply_audit_default_provider()

        mock_gauge.assert_called_once_with(False)

    def test_enabled_with_real_default_publishes_one(self, audit_default):
        """A wired process publishes 1 rather than leaving the series absent —
        an alert on ``== 0`` cannot distinguish "healthy" from "never booted"
        when the healthy case emits nothing."""
        audit_default("file_hashchain")

        with (
            override_audit_settings(enabled=True),
            patch.object(bootstrap, "_set_audit_backend_wired_gauge") as mock_gauge,
        ):
            bootstrap._apply_audit_default_provider()

        mock_gauge.assert_called_once_with(True)

    def test_disabled_publishes_nothing(self, audit_default):
        """With audit off there is no verdict to publish; a 0 would read as a
        misconfiguration alert on every OSS boot."""
        audit_default("null")

        with (
            override_audit_settings(enabled=False),
            patch.object(bootstrap, "_set_audit_backend_wired_gauge") as mock_gauge,
        ):
            bootstrap._apply_audit_default_provider()

        mock_gauge.assert_not_called()

    def test_gauge_reaches_the_metric_helper_with_the_verdict(self, audit_default):
        """The unpatched path: Step 5's verdict lands on the real setter."""
        audit_default("null")

        with (
            override_audit_settings(enabled=True),
            patch(
                "baldur.metrics.audit_backend_metrics.set_audit_backend_wired"
            ) as mock_set,
        ):
            bootstrap._apply_audit_default_provider()

        mock_set.assert_called_once_with(False)

    def test_gauge_failure_does_not_abort_step_five(self, audit_default):
        """Fail-open: an observability fault must not take down ``init()``,
        and must not swallow the WARNING that shares the branch."""
        audit_default("null")

        with (
            override_audit_settings(enabled=True),
            patch(
                "baldur.metrics.audit_backend_metrics.set_audit_backend_wired",
                side_effect=RuntimeError("prometheus registry exploded"),
            ),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._apply_audit_default_provider()

        assert len(_unwired_warnings(mock_logger)) == 1
        assert any(
            call.args and call.args[0] == "audit.default_provider_set"
            for call in mock_logger.debug.call_args_list
        )

    def test_helper_swallows_an_import_failure(self):
        """The metrics module may be absent entirely."""
        with (
            patch.dict("sys.modules", {"baldur.metrics.audit_backend_metrics": None}),
            patch.object(bootstrap, "logger") as mock_logger,
        ):
            bootstrap._set_audit_backend_wired_gauge(True)

        assert any(
            call.args and call.args[0] == "audit.backend_wired_gauge_skipped"
            for call in mock_logger.debug.call_args_list
        )
