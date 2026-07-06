"""532 — Service-layer cache resolver fail-loud-and-degrade.

Covers ``baldur.services.idempotency._cache_resolver`` with
``raise_on_prod_no_toggle=False`` (the service-layer caller). Companion to
``tests/unit/decorators/test_idempotent.py::TestResolveCache*`` which
covers the decorator caller (``raise_on_prod_no_toggle=True``).

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction — ``ProviderRegistry.get_cache`` resolution,
  ``IdempotencyMetricRecorder.record_fallback`` increment.
- §8.4 Side effects — WARN log emission, Prometheus counter increment.
- §8.3 Idempotency — one-shot guard fires WARN at most once per process per
  ``(layer, reason)``.
- §8.9 Concurrency — race-safe convergence under 10 threads × 100 calls.
- §6.7 Parametrize for the production × adapter × escape_hatch matrix.
"""

# NOTE: do NOT add ``from __future__ import annotations`` — the matrix
# decorators evaluate parameter defaults eagerly.

import logging
import os
import threading
from unittest.mock import patch

import pytest

from baldur.core.exceptions import AdapterNotFoundError
from baldur.services.idempotency import _cache_resolver as resolver_module
from baldur.services.idempotency._cache_resolver import (
    _reset_service_fallback_cache,
    _reset_warned_layers,
    resolve_cache_via_registry,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_resolver_state():
    """Clear the one-shot WARN guard and service fallback cache before/after."""
    _reset_warned_layers()
    _reset_service_fallback_cache()
    yield
    _reset_warned_layers()
    _reset_service_fallback_cache()


@pytest.fixture
def reset_idempotency_settings_singleton():
    from baldur.settings.idempotency import reset_idempotency_settings

    reset_idempotency_settings()
    yield
    reset_idempotency_settings()


@pytest.fixture
def reset_runtime_isolation():
    from baldur.runtime import reset_runtime

    reset_runtime()
    yield
    reset_runtime()


def _seed_env(monkeypatch, *, in_production: bool, escape_hatch: bool) -> None:
    """Set BALDUR_ENVIRONMENT + BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK."""
    if in_production:
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
    else:
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
    monkeypatch.setenv(
        "BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK",
        "true" if escape_hatch else "false",
    )


# =============================================================================
# TestServiceCacheOutcomeMatrix — service-layer matrix (raise_on_prod_no_toggle=False)
# =============================================================================


class TestServiceCacheOutcomeMatrix:
    """8-row outcome matrix for the service-layer resolver call.

    Service-layer (``raise_on_prod_no_toggle=False``) NEVER raises:
    - Adapter present → adapter (env + escape don't matter).
    - Adapter absent + prod + escape off → fallback (with WARN + counter).
    - Adapter absent + prod + escape on  → fallback (with WARN + counter).
    - Adapter absent + non-prod         → fallback (silent).
    """

    @pytest.mark.parametrize(
        ("in_production", "adapter_present", "escape_hatch", "expected_outcome"),
        [
            (True, True, False, "adapter"),
            (True, True, True, "adapter"),
            (False, True, False, "adapter"),
            (False, True, True, "adapter"),
            (False, False, False, "fallback"),
            (False, False, True, "fallback"),
            # Asymmetry with decorator: prod+no_adapter+no_escape does NOT raise.
            (True, False, False, "fallback"),
            (True, False, True, "fallback"),
        ],
        ids=[
            "prod_adapter_escape_off_returns_adapter",
            "prod_adapter_escape_on_returns_adapter",
            "dev_adapter_escape_off_returns_adapter",
            "dev_adapter_escape_on_returns_adapter",
            "dev_no_adapter_escape_off_returns_fallback",
            "dev_no_adapter_escape_on_returns_fallback",
            "prod_no_adapter_escape_off_returns_fallback",
            "prod_no_adapter_escape_on_returns_fallback",
        ],
    )
    def test_resolve_cache_outcome_matrix(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
        in_production,
        adapter_present,
        escape_hatch,
        expected_outcome,
    ):
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter

        # Given — environment + escape hatch + adapter presence.
        _seed_env(monkeypatch, in_production=in_production, escape_hatch=escape_hatch)

        registered = InMemoryCacheAdapter(key_prefix="matrix_registered:")
        fallback = resolver_module._SERVICE_FALLBACK_CACHE
        if adapter_present:
            ctx = patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                return_value=registered,
            )
        else:
            ctx = patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                side_effect=AdapterNotFoundError(adapter_type="cache"),
            )

        # When — service-layer resolver call (no raise variant).
        with ctx:
            resolved = resolve_cache_via_registry(
                layer="service",
                fallback_cache=fallback,
                raise_on_prod_no_toggle=False,
            )

        # Then — outcome identity matches expectation.
        if expected_outcome == "adapter":
            assert resolved is registered
        else:
            assert resolved is fallback


# =============================================================================
# TestServiceProdFailLoud — prod + no adapter + escape off → loud signal, no raise
# =============================================================================


class TestServiceProdFailLoud:
    """532 D1: service layer emits WARN + counter and returns fallback.

    The caller (audit sync_worker, cascade_auditor, correlation_engine)
    wraps every call in ``except Exception`` by design — so raising would
    be silenced. The Prometheus counter + WARN escape the caller's
    ``except`` and reach the SRE channel.
    """

    def test_prod_no_adapter_no_escape_returns_fallback_warns_and_counts(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
        caplog,
    ):
        _seed_env(monkeypatch, in_production=True, escape_hatch=False)
        fallback = resolver_module._SERVICE_FALLBACK_CACHE

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with patch.object(
                resolver_module, "_record_fallback_metric"
            ) as record_mock:
                with caplog.at_level(
                    logging.WARNING,
                    logger="baldur.services.idempotency._cache_resolver",
                ):
                    # When — must NOT raise.
                    resolved = resolve_cache_via_registry(
                        layer="service",
                        fallback_cache=fallback,
                        raise_on_prod_no_toggle=False,
                    )

        # Then — returned the module-level service fallback.
        assert resolved is fallback

        # Then — WARN log carries the documented event name + extras.
        records = [
            r
            for r in caplog.records
            if r.message == "idempotency.distributed_dedup_unavailable"
        ]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.pid == os.getpid()
        assert record.layer == "service"
        assert record.reason == "no_cache_adapter_registered"

        # Then — Prometheus counter incremented with the correct labels.
        record_mock.assert_called_once_with(
            layer="service", reason="no_cache_adapter_registered"
        )

    def test_prod_no_adapter_escape_on_returns_fallback_with_escape_hatch_event(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
        caplog,
    ):
        # 686: env drives ``is_production()``; the escape hatch resolves via the
        # cached layered seam, patched here so a present PRO RuntimeConfigManager
        # cannot pin the full-blob default over it.
        from baldur.settings.idempotency import IdempotencySettings

        _seed_env(monkeypatch, in_production=True, escape_hatch=True)
        fallback = resolver_module._SERVICE_FALLBACK_CACHE

        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(allow_inmemory_fallback=True),
            ),
            patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                side_effect=AdapterNotFoundError(adapter_type="cache"),
            ),
            patch.object(resolver_module, "_record_fallback_metric") as record_mock,
            caplog.at_level(
                logging.WARNING,
                logger="baldur.services.idempotency._cache_resolver",
            ),
        ):
            resolved = resolve_cache_via_registry(
                layer="service",
                fallback_cache=fallback,
                raise_on_prod_no_toggle=False,
            )

        assert resolved is fallback
        records = [
            r
            for r in caplog.records
            if r.message == "idempotency.inmemory_fallback_active"
        ]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.pid == os.getpid()
        assert record.layer == "service"
        assert record.reason == "escape_hatch_enabled"

        record_mock.assert_called_once_with(
            layer="service", reason="escape_hatch_enabled"
        )


# =============================================================================
# TestServiceDevSilentFallback — non-prod path is fully silent
# =============================================================================


class TestServiceDevSilentFallback:
    """Non-production with no adapter must NOT emit WARN or counter.

    The fallback would happen either way in dev/test, so the loud signal
    would be noise — operators only care when distributed dedup is
    silently broken in production.
    """

    def test_dev_no_adapter_returns_fallback_no_warn_no_counter(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
        caplog,
    ):
        _seed_env(monkeypatch, in_production=False, escape_hatch=False)
        fallback = resolver_module._SERVICE_FALLBACK_CACHE

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with patch.object(
                resolver_module, "_record_fallback_metric"
            ) as record_mock:
                with caplog.at_level(
                    logging.WARNING,
                    logger="baldur.services.idempotency._cache_resolver",
                ):
                    resolved = resolve_cache_via_registry(
                        layer="service",
                        fallback_cache=fallback,
                        raise_on_prod_no_toggle=False,
                    )

        assert resolved is fallback
        assert not [
            r
            for r in caplog.records
            if r.message
            in (
                "idempotency.distributed_dedup_unavailable",
                "idempotency.inmemory_fallback_active",
            )
        ]
        record_mock.assert_not_called()

    def test_dev_no_adapter_escape_on_returns_fallback_no_warn_no_counter(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
        caplog,
    ):
        # Escape hatch is moot in dev — fallback would happen anyway.
        _seed_env(monkeypatch, in_production=False, escape_hatch=True)
        fallback = resolver_module._SERVICE_FALLBACK_CACHE

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with patch.object(
                resolver_module, "_record_fallback_metric"
            ) as record_mock:
                with caplog.at_level(
                    logging.WARNING,
                    logger="baldur.services.idempotency._cache_resolver",
                ):
                    resolved = resolve_cache_via_registry(
                        layer="service",
                        fallback_cache=fallback,
                        raise_on_prod_no_toggle=False,
                    )

        assert resolved is fallback
        assert not [
            r
            for r in caplog.records
            if r.message
            in (
                "idempotency.distributed_dedup_unavailable",
                "idempotency.inmemory_fallback_active",
            )
        ]
        record_mock.assert_not_called()


# =============================================================================
# TestOneShotWarnGuard — _fallback_warned_layers semantics
# =============================================================================


class TestOneShotWarnGuard:
    """D4: WARN fires at most once per (layer, reason) per process.

    Subsequent resolutions still bump the Prometheus counter — that's the
    cumulative SRE signal. The WARN is the human-readable bookmark.
    """

    def test_warn_emitted_once_across_repeated_resolutions(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
        caplog,
    ):
        _seed_env(monkeypatch, in_production=True, escape_hatch=False)
        fallback = resolver_module._SERVICE_FALLBACK_CACHE

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with patch.object(resolver_module, "_record_fallback_metric"):
                with caplog.at_level(
                    logging.WARNING,
                    logger="baldur.services.idempotency._cache_resolver",
                ):
                    for _ in range(5):
                        resolve_cache_via_registry(
                            layer="service",
                            fallback_cache=fallback,
                            raise_on_prod_no_toggle=False,
                        )

        warn_records = [
            r
            for r in caplog.records
            if r.message == "idempotency.distributed_dedup_unavailable"
        ]
        assert len(warn_records) == 1, (
            f"WARN must fire exactly once, got {len(warn_records)}"
        )
        # Guard must be populated for the (layer, reason) pair.
        assert ("service", "no_cache_adapter_registered") in (
            resolver_module._fallback_warned_layers
        )

    def test_counter_increments_every_call_even_when_warn_throttled(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
    ):
        """D5: Prometheus counter is the cumulative signal — not throttled."""
        _seed_env(monkeypatch, in_production=True, escape_hatch=False)
        fallback = resolver_module._SERVICE_FALLBACK_CACHE

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with patch.object(
                resolver_module, "_record_fallback_metric"
            ) as record_mock:
                for _ in range(5):
                    resolve_cache_via_registry(
                        layer="service",
                        fallback_cache=fallback,
                        raise_on_prod_no_toggle=False,
                    )

        assert record_mock.call_count == 5
        for call in record_mock.call_args_list:
            assert call.kwargs == {
                "layer": "service",
                "reason": "no_cache_adapter_registered",
            }

    def test_distinct_layer_reason_pairs_each_emit_once(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
        caplog,
    ):
        """Service + escape_hatch_enabled is a different key than service +
        no_cache_adapter_registered — both get one WARN."""
        # 686: env drives ``is_production()``; the escape hatch resolves via the
        # cached layered seam, patched per-segment so a present PRO
        # RuntimeConfigManager cannot pin the full-blob default over it.
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import IdempotencySettings

        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        reset_runtime()
        fallback = resolver_module._SERVICE_FALLBACK_CACHE

        with (
            patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                side_effect=AdapterNotFoundError(adapter_type="cache"),
            ),
            patch.object(resolver_module, "_record_fallback_metric"),
            caplog.at_level(
                logging.WARNING,
                logger="baldur.services.idempotency._cache_resolver",
            ),
        ):
            # First — prod + escape off → distributed_dedup_unavailable
            with patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(allow_inmemory_fallback=False),
            ):
                resolve_cache_via_registry(
                    layer="service",
                    fallback_cache=fallback,
                    raise_on_prod_no_toggle=False,
                )
            # Second — prod + escape on → inmemory_fallback_active
            with patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=IdempotencySettings(allow_inmemory_fallback=True),
            ):
                resolve_cache_via_registry(
                    layer="service",
                    fallback_cache=fallback,
                    raise_on_prod_no_toggle=False,
                )

        a = [
            r
            for r in caplog.records
            if r.message == "idempotency.distributed_dedup_unavailable"
        ]
        b = [
            r
            for r in caplog.records
            if r.message == "idempotency.inmemory_fallback_active"
        ]
        assert len(a) == 1
        assert len(b) == 1

    def test_race_safe_under_concurrent_first_resolution(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
        caplog,
    ):
        """10 threads × 100 calls — WARN bounded by thread count, counter
        accurate. Lock-free guard is by design (D4); first-race double-WARN
        is harmless because the cumulative SRE signal is the counter."""
        _seed_env(monkeypatch, in_production=True, escape_hatch=False)
        fallback = resolver_module._SERVICE_FALLBACK_CACHE

        thread_count = 10
        calls_per_thread = 100
        barrier = threading.Barrier(thread_count)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait(timeout=5.0)
                for _ in range(calls_per_thread):
                    resolve_cache_via_registry(
                        layer="service",
                        fallback_cache=fallback,
                        raise_on_prod_no_toggle=False,
                    )
            except Exception as exc:
                errors.append(exc)

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with patch.object(
                resolver_module, "_record_fallback_metric"
            ) as record_mock:
                with caplog.at_level(
                    logging.WARNING,
                    logger="baldur.services.idempotency._cache_resolver",
                ):
                    threads = [
                        threading.Thread(target=worker) for _ in range(thread_count)
                    ]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join(timeout=10.0)

        assert not errors, f"worker threads raised: {errors}"

        # WARN is bounded by thread count (typically converges to 1).
        warn_records = [
            r
            for r in caplog.records
            if r.message == "idempotency.distributed_dedup_unavailable"
        ]
        assert 1 <= len(warn_records) <= thread_count, (
            f"WARN count {len(warn_records)} must be in [1, {thread_count}]"
        )

        # Counter is cumulative — every call must have been counted.
        assert record_mock.call_count == thread_count * calls_per_thread


# =============================================================================
# TestFallbackMetricExposed — Prometheus exposition with layer + reason labels
# =============================================================================


class TestFallbackMetricExposed:
    """D5: ``baldur_idempotency_cache_unavailable_fallback_total`` is
    registered with ``layer`` and ``reason`` labels under the canonical name.
    """

    def test_fallback_counter_registered_under_canonical_name(self):
        try:
            from prometheus_client import REGISTRY
        except ImportError:
            pytest.skip("prometheus_client not installed")

        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

        # Force construction in case the test ordering reset the registry.
        IdempotencyMetricRecorder()

        assert (
            "baldur_idempotency_cache_unavailable_fallback_total"
            in REGISTRY._names_to_collectors
        ), (
            "IdempotencyMetricRecorder must register "
            "baldur_idempotency_cache_unavailable_fallback_total"
        )

    @pytest.mark.parametrize(
        ("layer", "reason"),
        [
            ("service", "no_cache_adapter_registered"),
            ("service", "escape_hatch_enabled"),
            ("decorator", "no_cache_adapter_registered"),
            ("decorator", "escape_hatch_enabled"),
        ],
        ids=[
            "service__no_cache_adapter_registered",
            "service__escape_hatch_enabled",
            "decorator__no_cache_adapter_registered",
            "decorator__escape_hatch_enabled",
        ],
    )
    def test_record_fallback_increments_labeled_counter(self, layer, reason):
        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

        recorder = IdempotencyMetricRecorder()

        with patch.object(recorder._fallback_total, "labels") as mock_labels:
            recorder.record_fallback(layer=layer, reason=reason)

            mock_labels.assert_called_once_with(layer=layer, reason=reason)
            mock_labels.return_value.inc.assert_called_once_with()

    def test_record_fallback_real_counter_increments_by_one(self):
        try:
            from prometheus_client import REGISTRY  # noqa: F401
        except ImportError:
            pytest.skip("prometheus_client not installed")

        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

        recorder = IdempotencyMetricRecorder()
        labeled = recorder._fallback_total.labels(
            layer="service", reason="no_cache_adapter_registered"
        )
        before = labeled._value.get()
        recorder.record_fallback(layer="service", reason="no_cache_adapter_registered")
        after = labeled._value.get()

        assert after - before == 1

    def test_record_fallback_swallows_label_exceptions(self):
        """Best-effort: a broken Prometheus backend must not break the resolver."""
        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

        recorder = IdempotencyMetricRecorder()
        with patch.object(
            recorder._fallback_total,
            "labels",
            side_effect=RuntimeError("metrics down"),
        ):
            # No raise — recorder swallows and the resolver hot path continues.
            recorder.record_fallback(
                layer="service", reason="no_cache_adapter_registered"
            )


# =============================================================================
# TestServiceCacheResolverIntegration — IdempotencyService._get_cache delegation
# =============================================================================


class TestServiceCacheResolverIntegration:
    """``IdempotencyService._get_cache()`` delegates to the shared resolver.

    Verifies the integration boundary so a future refactor that changes the
    resolver signature also forces this test to be updated.
    """

    def test_get_cache_returns_registered_adapter_when_present(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
    ):
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.services.idempotency.service import IdempotencyService

        _seed_env(monkeypatch, in_production=True, escape_hatch=False)
        registered = InMemoryCacheAdapter(key_prefix="registered:")

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            return_value=registered,
        ):
            service = IdempotencyService()
            cache = service._get_cache()

        assert cache is registered

    def test_get_cache_returns_service_fallback_on_prod_no_adapter(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
    ):
        from baldur.services.idempotency.service import IdempotencyService

        _seed_env(monkeypatch, in_production=True, escape_hatch=False)

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with patch.object(resolver_module, "_record_fallback_metric"):
                service = IdempotencyService()
                cache = service._get_cache()

        # Identity check against the module-level service fallback.
        assert cache is resolver_module._SERVICE_FALLBACK_CACHE

    def test_get_cache_is_lazy_and_memoized(
        self,
        monkeypatch,
        reset_idempotency_settings_singleton,
        reset_runtime_isolation,
    ):
        """First call resolves; subsequent calls return the cached reference."""
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.services.idempotency.service import IdempotencyService

        _seed_env(monkeypatch, in_production=False, escape_hatch=False)
        registered = InMemoryCacheAdapter(key_prefix="lazy_check:")

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            return_value=registered,
        ) as get_cache_mock:
            service = IdempotencyService()
            assert service._cache is None
            first = service._get_cache()
            second = service._get_cache()

        assert first is registered
        assert second is first
        # ProviderRegistry.get_cache invoked exactly once.
        assert get_cache_mock.call_count == 1

    def test_noop_cache_class_removed_from_service_module(self):
        """532 — ``_NoopCache`` / ``_NoopLock`` are deleted (unreachable
        after D1). Guard against accidental re-introduction."""
        from baldur.services.idempotency import service as service_mod

        assert not hasattr(service_mod, "_NoopCache")
        assert not hasattr(service_mod, "_NoopLock")
