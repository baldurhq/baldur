"""IdempotencyService.check() emits metrics on each outcome (484 D4).

Three outcome paths in ``IdempotencyService.check()``:
1. Cache hit → ``record_check(result="cache_hit", domain=key.domain.value)``
2. DB hit (lookup_fn returned non-empty) → ``record_check(result="db_hit", ...)``
3. Miss (no cache + no lookup or lookup returned empty) → ``record_check(result="miss", ...)``

Domain label is ``IdempotencyDomain.value`` from the input key. The metrics
emit is best-effort: failures inside ``_record_idempotency_check`` are
swallowed so the idempotency hot path is never broken by an observability
fault.

Reference: ``484`` D4.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.idempotency.models import (
    IdempotencyDomain,
    IdempotencyKey,
)
from baldur.services.idempotency.service import IdempotencyService


def _make_key(
    domain: IdempotencyDomain = IdempotencyDomain.EXTERNAL_SERVICE,
    key_str: str = "op-001",
) -> IdempotencyKey:
    """Build an IdempotencyKey for the given domain."""
    return IdempotencyKey(
        domain=domain,
        key=key_str,
        components={"order_id": key_str},
    )


@pytest.fixture
def service_with_mock_cache():
    """IdempotencyService preloaded with a MagicMock cache."""
    cache = MagicMock()
    cache.get.return_value = None
    service = IdempotencyService()
    service._cache = cache
    return service, cache


# =============================================================================
# Behavior — one record_check call per check() invocation
# =============================================================================


class TestIdempotencyServiceMetricsEmissionBehavior:
    """484 D4: ``IdempotencyService.check()`` emits exactly one record_check call."""

    @pytest.mark.parametrize(
        "domain",
        [
            IdempotencyDomain.EXTERNAL_SERVICE,
            IdempotencyDomain.CHAOS_EXPERIMENT,
            IdempotencyDomain.WAL_RECOVERY,
        ],
        ids=["external_service", "chaos_experiment", "wal_recovery"],
    )
    def test_cache_hit_emits_cache_hit_with_domain(
        self, service_with_mock_cache, domain
    ):
        """Cache hit path → record_check('cache_hit', <domain>)."""
        service, cache = service_with_mock_cache
        cache.get.return_value = {"status": "processed"}
        key = _make_key(domain=domain)

        with patch(
            "baldur.services.idempotency.service._record_idempotency_check"
        ) as mock_record:
            result = service.check(key)

        assert result.is_duplicate is True
        mock_record.assert_called_once_with("cache_hit", domain.value)

    @pytest.mark.parametrize(
        "domain",
        [
            IdempotencyDomain.EXTERNAL_SERVICE,
            IdempotencyDomain.CHAOS_EXPERIMENT,
            IdempotencyDomain.WAL_RECOVERY,
        ],
        ids=["external_service", "chaos_experiment", "wal_recovery"],
    )
    def test_db_hit_emits_db_hit_with_domain(self, service_with_mock_cache, domain):
        """DB hit path (lookup_fn returns non-empty) → record_check('db_hit', <domain>)."""
        service, cache = service_with_mock_cache
        cache.get.return_value = None  # No cache hit
        key = _make_key(domain=domain)
        lookup_fn = MagicMock(return_value={"id": 7, "status": "found"})

        with patch(
            "baldur.services.idempotency.service._record_idempotency_check"
        ) as mock_record:
            result = service.check(key, lookup_fn=lookup_fn)

        assert result.is_duplicate is True
        mock_record.assert_called_once_with("db_hit", domain.value)

    @pytest.mark.parametrize(
        "domain",
        [
            IdempotencyDomain.EXTERNAL_SERVICE,
            IdempotencyDomain.CHAOS_EXPERIMENT,
            IdempotencyDomain.WAL_RECOVERY,
        ],
        ids=["external_service", "chaos_experiment", "wal_recovery"],
    )
    def test_miss_emits_miss_with_domain(self, service_with_mock_cache, domain):
        """Miss path (no cache + lookup returns empty) → record_check('miss', <domain>)."""
        service, cache = service_with_mock_cache
        cache.get.return_value = None
        key = _make_key(domain=domain)
        lookup_fn = MagicMock(return_value=None)

        with patch(
            "baldur.services.idempotency.service._record_idempotency_check"
        ) as mock_record:
            result = service.check(key, lookup_fn=lookup_fn)

        assert result.is_duplicate is False
        mock_record.assert_called_once_with("miss", domain.value)

    def test_miss_without_lookup_fn_emits_miss(self, service_with_mock_cache):
        """No cache hit and no lookup_fn → still emits a single 'miss' counter."""
        service, cache = service_with_mock_cache
        cache.get.return_value = None
        key = _make_key()

        with patch(
            "baldur.services.idempotency.service._record_idempotency_check"
        ) as mock_record:
            result = service.check(key)

        assert result.is_duplicate is False
        mock_record.assert_called_once_with("miss", key.domain.value)


# =============================================================================
# Behavior — _record_idempotency_check swallows metrics-side errors
# =============================================================================


class TestRecordIdempotencyCheckBestEffortBehavior:
    """484 D4: ``_record_idempotency_check`` is best-effort (swallow errors)."""

    def test_metrics_exception_does_not_break_check(self, service_with_mock_cache):
        """``IdempotencyService.check()`` must succeed even if metrics raise.

        We patch ``get_metrics`` to raise; the service path must still
        return its idempotency verdict. This protects against a broken
        Prometheus client / OTEL backend taking down request handling.
        """
        service, cache = service_with_mock_cache
        cache.get.return_value = None
        key = _make_key()

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=RuntimeError("metrics down"),
        ):
            # No raise — the helper swallows the failure.
            result = service.check(key)

        assert result.is_duplicate is False

    def test_record_helper_no_op_when_recorder_missing(self):
        """Helper exits silently when ``get_metrics()`` returns no recorder."""
        from baldur.services.idempotency.service import _record_idempotency_check

        fake_metrics = MagicMock(spec=[])  # No 'idempotency' attribute

        with patch("baldur.metrics.prometheus.get_metrics", return_value=fake_metrics):
            # No raise.
            _record_idempotency_check("miss", "external_service")
