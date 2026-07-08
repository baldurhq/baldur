"""Unit tests for bootstrap background-service helpers.

Scope of `_start_capacity_reservation_if_enabled`,
`_start_cell_topology_if_enabled`, `_start_rate_controller_if_enabled`, and
`_start_hpa_exporter_if_enabled` (the last two added by 615 D4/G6 — OSS
`baldur.scaling` loops that were Django-only `apps.py` F30 starts):

- Gunicorn master (`SERVER_SOFTWARE` contains "gunicorn", no `GUNICORN_WORKER`)
  -> skip (609 D3 fork-safety: the scheduler/anti-entropy threads die after
  fork() and init() is not re-run in workers; the per-worker post_worker_init
  hook re-runs the start after setting GUNICORN_WORKER=1).
- Settings `.enabled` flag gates the start (Dormant capacity_reservation
  defaults to disabled, matching FEATURE_CATALOG policy).
- ImportError is swallowed (DEBUG log) — extras may not be installed.
- Runtime exceptions are swallowed (WARNING log) — init() must continue.

These tests do NOT exercise the services themselves; service-side behavior
lives in their own modules' tests.

The circuit_mesh start tests moved to
tests/pro/unit/test_register_relocated_features.py (599 D12 — the start
seam relocated to baldur_pro.register_pro_services).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.bootstrap import (
    _start_capacity_reservation_if_enabled,
    _start_cell_topology_if_enabled,
    _start_hpa_exporter_if_enabled,
    _start_rate_controller_if_enabled,
)

# =============================================================================
# capacity_reservation
# =============================================================================


class TestStartCapacityReservationIfEnabled:
    def test_gunicorn_master_skips_start(self, monkeypatch):
        """609 D3: under the Gunicorn master the start is skipped (fork-safety) —
        the per-worker post_worker_init hook re-runs it after GUNICORN_WORKER=1."""
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with patch(
            "baldur.settings.capacity_reservation.get_capacity_reservation_settings",
            autospec=True,
        ) as get_settings:
            _start_capacity_reservation_if_enabled()

        # Master-skip returns before the settings lookup.
        get_settings.assert_not_called()

    def test_disabled_skips_start(self):
        mock_settings = MagicMock(enabled=False)
        with (
            patch(
                "baldur.settings.capacity_reservation.get_capacity_reservation_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.capacity_reservation.service.CapacityReservationService"
            ) as mock_cls,
        ):
            _start_capacity_reservation_if_enabled()

        mock_cls.assert_not_called()

    def test_enabled_starts_service(self):
        mock_settings = MagicMock(enabled=True)
        mock_service = MagicMock()
        mock_cls = MagicMock(return_value=mock_service)
        with (
            patch(
                "baldur.settings.capacity_reservation.get_capacity_reservation_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.capacity_reservation.service.CapacityReservationService",
                mock_cls,
            ),
        ):
            _start_capacity_reservation_if_enabled()

        # The starter owns both initialize() and start(): start() raises without
        # a prior initialize(), so they must be co-located and ordered.
        assert [c[0] for c in mock_service.method_calls] == ["initialize", "start"]
        # The safety valve is wired: initialize() receives a metrics provider and
        # a graceful-degradation handle (without them the valve is a no-op).
        _, init_kwargs = mock_service.initialize.call_args
        assert init_kwargs["metrics_provider"] is not None
        assert init_kwargs["graceful_degradation"] is not None

    def test_import_error_swallowed(self):
        with patch(
            "baldur.settings.capacity_reservation.get_capacity_reservation_settings",
            side_effect=ImportError("missing extra"),
        ):
            _start_capacity_reservation_if_enabled()

    def test_runtime_error_swallowed(self):
        mock_settings = MagicMock(enabled=True)
        mock_service = MagicMock()
        mock_service.start.side_effect = RuntimeError("crash")
        with (
            patch(
                "baldur.settings.capacity_reservation.get_capacity_reservation_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.capacity_reservation.service.CapacityReservationService",
                return_value=mock_service,
            ),
        ):
            _start_capacity_reservation_if_enabled()


# =============================================================================
# cell_topology
# =============================================================================


class TestStartCellTopologyIfEnabled:
    def test_gunicorn_master_skips_start(self, monkeypatch):
        """609 D3: under the Gunicorn master the start is skipped (fork-safety) —
        the per-worker post_worker_init hook re-runs it after GUNICORN_WORKER=1."""
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with patch(
            "baldur.settings.cell_topology.get_cell_topology_settings",
            autospec=True,
        ) as get_settings:
            _start_cell_topology_if_enabled()

        # Master-skip returns before the settings lookup.
        get_settings.assert_not_called()

    def test_disabled_skips_start(self):
        mock_settings = MagicMock(enabled=False)
        with (
            patch(
                "baldur.settings.cell_topology.get_cell_topology_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.cell_topology.service.get_cell_topology_service"
            ) as mock_get,
        ):
            _start_cell_topology_if_enabled()

        mock_get.assert_not_called()

    def test_enabled_starts_service(self):
        mock_settings = MagicMock(enabled=True)
        mock_service = MagicMock()
        with (
            patch(
                "baldur.settings.cell_topology.get_cell_topology_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.cell_topology.service.get_cell_topology_service",
                return_value=mock_service,
            ),
        ):
            _start_cell_topology_if_enabled()

        mock_service.start.assert_called_once()

    def test_import_error_swallowed(self):
        with patch(
            "baldur.settings.cell_topology.get_cell_topology_settings",
            side_effect=ImportError("missing"),
        ):
            _start_cell_topology_if_enabled()

    def test_runtime_error_swallowed(self):
        mock_settings = MagicMock(enabled=True)
        mock_service = MagicMock()
        mock_service.start.side_effect = RuntimeError("anti-entropy boom")
        with (
            patch(
                "baldur.settings.cell_topology.get_cell_topology_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.services.cell_topology.service.get_cell_topology_service",
                return_value=mock_service,
            ),
        ):
            _start_cell_topology_if_enabled()


# =============================================================================
# F30 scaling loops — rate_controller + hpa_exporter (615 D4/G6)
# =============================================================================
#
# OSS ``baldur.scaling`` loops that were started only from Django ``apps.py``;
# 615 D4 made them direct ``_BACKGROUND_WORKER_STARTERS`` members so Flask /
# FastAPI / plain-Python CLI get them too. Both default-OFF (no AUTOSTART
# hatch) and ``_running``-idempotent. Same gating contract as the workers
# above: master-skip, settings-gate, ImportError/Exception fail-soft.


class TestF30StarterGating:
    """615 D4/G6: rate_controller + hpa_exporter master-skip, self-gate, and
    fail-soft behavior."""

    # -- rate_controller ------------------------------------------------------

    def test_rate_controller_gunicorn_master_skips_start(self, monkeypatch):
        """Under the Gunicorn master the start is skipped (fork-safety) — the
        per-worker post_worker_init hook re-runs it after GUNICORN_WORKER=1."""
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with patch(
            "baldur.settings.backpressure.get_backpressure_settings",
            autospec=True,
        ) as get_settings:
            _start_rate_controller_if_enabled()

        # Master-skip returns before the settings lookup.
        get_settings.assert_not_called()

    def test_rate_controller_disabled_skips_start(self):
        mock_settings = MagicMock(backpressure_enabled=False)
        with (
            patch(
                "baldur.settings.backpressure.get_backpressure_settings",
                return_value=mock_settings,
            ),
            patch("baldur.scaling.rate_controller.get_rate_controller") as mock_get,
        ):
            _start_rate_controller_if_enabled()

        mock_get.assert_not_called()

    def test_rate_controller_enabled_starts_service(self):
        mock_settings = MagicMock(backpressure_enabled=True)
        mock_service = MagicMock()
        with (
            patch(
                "baldur.settings.backpressure.get_backpressure_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                return_value=mock_service,
            ),
        ):
            _start_rate_controller_if_enabled()

        mock_service.start.assert_called_once()

    def test_rate_controller_import_error_swallowed(self):
        with patch(
            "baldur.settings.backpressure.get_backpressure_settings",
            side_effect=ImportError("missing extra"),
        ):
            _start_rate_controller_if_enabled()

    def test_rate_controller_runtime_error_swallowed(self):
        mock_settings = MagicMock(backpressure_enabled=True)
        mock_service = MagicMock()
        mock_service.start.side_effect = RuntimeError("crash")
        with (
            patch(
                "baldur.settings.backpressure.get_backpressure_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                return_value=mock_service,
            ),
        ):
            _start_rate_controller_if_enabled()

    # -- hpa_exporter (gated by hpa_enabled AND metrics_enabled) --------------

    def test_hpa_exporter_gunicorn_master_skips_start(self, monkeypatch):
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with patch(
            "baldur.settings.backpressure.get_backpressure_settings",
            autospec=True,
        ) as get_settings:
            _start_hpa_exporter_if_enabled()

        get_settings.assert_not_called()

    def test_hpa_exporter_both_flags_off_skips_start(self):
        mock_settings = MagicMock(hpa_enabled=False, metrics_enabled=False)
        with (
            patch(
                "baldur.settings.backpressure.get_backpressure_settings",
                return_value=mock_settings,
            ),
            patch("baldur.scaling.hpa_exporter.get_hpa_metrics_exporter") as mock_get,
        ):
            _start_hpa_exporter_if_enabled()

        mock_get.assert_not_called()

    def test_hpa_exporter_only_hpa_enabled_skips_start(self):
        """The gate is ``hpa_enabled AND metrics_enabled`` — HPA alone is not
        sufficient (the exporter has no metrics backend to publish to)."""
        mock_settings = MagicMock(hpa_enabled=True, metrics_enabled=False)
        with (
            patch(
                "baldur.settings.backpressure.get_backpressure_settings",
                return_value=mock_settings,
            ),
            patch("baldur.scaling.hpa_exporter.get_hpa_metrics_exporter") as mock_get,
        ):
            _start_hpa_exporter_if_enabled()

        mock_get.assert_not_called()

    def test_hpa_exporter_both_flags_on_starts_service(self):
        mock_settings = MagicMock(hpa_enabled=True, metrics_enabled=True)
        mock_service = MagicMock()
        with (
            patch(
                "baldur.settings.backpressure.get_backpressure_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.scaling.hpa_exporter.get_hpa_metrics_exporter",
                return_value=mock_service,
            ),
        ):
            _start_hpa_exporter_if_enabled()

        mock_service.start.assert_called_once()

    def test_hpa_exporter_import_error_swallowed(self):
        with patch(
            "baldur.settings.backpressure.get_backpressure_settings",
            side_effect=ImportError("missing"),
        ):
            _start_hpa_exporter_if_enabled()

    def test_hpa_exporter_runtime_error_swallowed(self):
        mock_settings = MagicMock(hpa_enabled=True, metrics_enabled=True)
        mock_service = MagicMock()
        mock_service.start.side_effect = RuntimeError("exporter boom")
        with (
            patch(
                "baldur.settings.backpressure.get_backpressure_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.scaling.hpa_exporter.get_hpa_metrics_exporter",
                return_value=mock_service,
            ),
        ):
            _start_hpa_exporter_if_enabled()
