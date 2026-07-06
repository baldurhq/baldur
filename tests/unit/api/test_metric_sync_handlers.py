"""Handler -> real-service regression for the 625 metric-sync kwarg fix.

Target: ``baldur.api.handlers.metric_sync.metric_sync`` driven against a
**real** in-process ``MetricSyncService`` (OSS), with only its data-source
dependencies (reconciler + adapter) inert. This is the regression surface for
the 625 ``actor_id=`` -> ``actor=`` fix (G4): the handler previously passed
``actor_id=`` to ``sync_metrics(domains, dry_run, actor, reason)`` (no
``**kwargs``), raising ``TypeError`` on every ``POST /metrics/sync`` request.

Why handler -> real service (no service mock): the service-level tests pass
``actor=`` directly and never traverse the handler's wrong kwarg, so the bug
was invisible to them. Driving the handler against a real service — with the
reconciler mocked and a ``NullMetricSourceAdapter`` so the run is in-memory and
deterministic — exercises the handler->service call contract itself
(§8.2 exception-absence).

No ``baldur_pro`` dependency: ``MetricSyncService`` is OSS, so this file needs
no ``requires_pro`` marker.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.metrics.base import NullMetricSourceAdapter
from baldur.api.handlers.metric_sync import metric_sync
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.metrics.reconciler import MetricReconciler
from baldur.services.metric_sync_service import (
    MetricSyncService,
    configure_metric_sync_service,
    reset_metric_sync_service,
)

_AUDIT_LOGGER = "baldur.audit.logger.AuditLogger.get_instance"


@pytest.fixture(autouse=True)
def _reset_metric_sync_service():
    reset_metric_sync_service()
    yield
    reset_metric_sync_service()


def _make_ctx(json_body=None, user=None):
    return RequestContext(
        method=HttpMethod("POST"),
        path="/metrics/sync",
        json_body=json_body,
        user=user,
    )


def _real_service():
    """A real MetricSyncService whose data source is inert.

    The reconciler is mocked (its sync calls are no-ops whose return value
    ``sync_metrics`` ignores) and the adapter is the in-memory
    ``NullMetricSourceAdapter`` (returns 0 / 0.0). The real ``sync_metrics``
    method runs end-to-end — only its I/O is stubbed — so the handler->service
    ``actor=`` kwarg is genuinely exercised.
    """
    return MetricSyncService(
        reconciler=MagicMock(spec=MetricReconciler),
        adapter=NullMetricSourceAdapter(),
    )


class TestMetricSyncHandlerBehavior:
    """``POST /metrics/sync`` — 625 G4 (actor= no TypeError)."""

    def test_dry_run_actor_path_returns_without_type_error(self):
        """A dry-run sync drives the real ``sync_metrics`` via actor= and 200s.

        Regression: the old ``actor_id=`` call site raised ``TypeError`` on the
        real service signature.
        """
        # Given the handler wired to a real (inert-source) MetricSyncService
        configure_metric_sync_service(_real_service())

        # When a dry-run sync is requested
        resp = metric_sync(
            _make_ctx(json_body={"domains": ["payment"], "dry_run": True})
        )

        # Then the actor= call site completes without a TypeError
        assert resp.status_code == 200
        assert resp.body["status"] == "dry_run"
        assert resp.body["actor"] == "anonymous"

    def test_real_sync_actor_path_returns_without_type_error(self):
        """The default (non-dry-run) path runs reconciler + audit for real
        (audit patched to a silent sink to avoid a filesystem write); the
        ``actor=`` kwarg still completes without a ``TypeError``."""
        # Given the handler wired to a real service + a silent audit logger
        configure_metric_sync_service(_real_service())
        audit = SimpleNamespace(log=lambda event: None)

        # When a real (non-dry-run) sync is requested by an authenticated actor
        with patch(_AUDIT_LOGGER, return_value=audit):
            resp = metric_sync(
                _make_ctx(
                    json_body={"domains": ["payment"]},
                    user=SimpleNamespace(username="alice"),
                )
            )

        # Then the sync completes and the actor threads through
        assert resp.status_code == 200
        assert resp.body["status"] == "completed"
        assert resp.body["actor"] == "alice"
