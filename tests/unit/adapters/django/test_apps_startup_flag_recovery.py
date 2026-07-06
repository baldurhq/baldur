"""
Unit tests for apps.py startup duplicate-start guards.

Tests:
D. _start_correlation_engine_loop resets its flag when initialization fails
   (provider absent or Exception) — fix(356) contract, body resolves via
   ProviderRegistry.worker_background_starts since 599 D12.

Note: the OSS-5 init()-started workers (meta_watchdog, precomputed_cache,
system_metrics_cache, capacity_reservation, cell_topology) are started by
baldur.bootstrap.start_background_workers() (framework-agnostic init() + the
gunicorn post_worker_init hook) and carry their own service-level idempotency
guard, so they no longer keep a Django-side duplicate-start flag here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.adapters.django.apps import BaldurConfig


class TestCorrelationLoopFlagRecoveryBehavior:
    """Correlation engine loop startup flag must be reset on failure.

    599 D12 — the start body resolves a callable from
    ``ProviderRegistry.worker_background_starts`` (the engine itself lives
    in the private distribution); the flag-recovery contract is unchanged.
    """

    def setup_method(self) -> None:
        BaldurConfig._correlation_loop_started = False

    def teardown_method(self) -> None:
        BaldurConfig._correlation_loop_started = False

    def test_correlation_loop_flag_reset_when_provider_absent(self) -> None:
        """Empty slot (OSS-only install) resets the flag and no-ops."""
        from baldur.factory.registry import ProviderRegistry

        config = BaldurConfig.__new__(BaldurConfig)

        mock_settings = MagicMock()
        mock_settings.enabled = True

        slot = ProviderRegistry.worker_background_starts
        with (
            patch(
                "baldur.settings.correlation_engine.get_correlation_engine_settings",
                return_value=mock_settings,
            ),
            slot.snapshot(),
        ):
            slot.reset()  # deterministic empty slot regardless of test order
            config._start_correlation_engine_loop()

        assert BaldurConfig._correlation_loop_started is False

    def test_correlation_loop_flag_reset_on_runtime_error(self) -> None:
        """Runtime exception during the start callable resets flag to False."""
        from baldur.factory.registry import ProviderRegistry

        config = BaldurConfig.__new__(BaldurConfig)

        mock_settings = MagicMock()
        mock_settings.enabled = True

        def _crashing_start() -> None:
            raise RuntimeError("engine crash")

        slot = ProviderRegistry.worker_background_starts
        with (
            patch(
                "baldur.settings.correlation_engine.get_correlation_engine_settings",
                return_value=mock_settings,
            ),
            slot.snapshot(),
        ):
            slot.reset()
            slot.register("correlation_engine_loop", _crashing_start)
            config._start_correlation_engine_loop()

        assert BaldurConfig._correlation_loop_started is False
