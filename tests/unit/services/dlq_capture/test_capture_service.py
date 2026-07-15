"""Unit tests for the OSS DLQ capture backing — ``DLQCaptureService``.

Covers the moved capture core (store dispatch, kill-switch, terminal
fail-open local fallback, mask-before-truncate ordering) and the single
resolution chain (``resolve_dlq_backing`` / ``resolve_dlq_backing_tier``) that
lets a pure OSS install capture failures with the ``dlq_service`` slot empty.

"PRO absent" is simulated by clearing ``ProviderRegistry.dlq_service`` (D8 —
registry-first, no import games), so these tests are deterministic whether or
not ``baldur_pro`` is installed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.factory.registry import ProviderRegistry
from baldur.models.dlq import DLQConfig, DLQEntryResult
from baldur.services.dlq_capture import (
    DLQCaptureService,
    get_dlq_capture_service,
    reset_dlq_capture_service,
    resolve_dlq_backing,
    resolve_dlq_backing_tier,
)

# =============================================================================
# store_failure — capture core behavior
# =============================================================================


class TestDLQCaptureStoreBehavior:
    """``DLQCaptureService.store_failure`` on the OSS backing (real in-memory repo)."""

    @pytest.fixture
    def repo(self):
        # Real in-process double (guidelines §6.4): deterministic, no I/O,
        # implements create / count_* / evict_oldest.
        return InMemoryFailedOperationRepository()

    @pytest.fixture
    def service(self, repo):
        return DLQCaptureService(config=DLQConfig(enabled=True), repository=repo)

    def test_sync_store_captures_entry_and_returns_real_id(self, service, repo):
        """mode='sync' durably captures into the repo and returns a real dlq_id."""
        result = service.store_failure(
            domain="payment", failure_type="PG_TIMEOUT", mode="sync"
        )

        assert isinstance(result, DLQEntryResult)
        assert result.success is True
        assert result.dlq_id is not None
        assert repo.count_all() == 1

    def test_disabled_dlq_short_circuits_without_store(self, repo):
        """Kill-switch: config.enabled=False disables OSS capture entirely."""
        service = DLQCaptureService(config=DLQConfig(enabled=False), repository=repo)

        result = service.store_failure(domain="payment", failure_type="X", mode="sync")

        assert result.success is False
        assert result.error == "DLQ is disabled"
        assert repo.count_all() == 0

    def test_async_mode_with_request_raises_value_error(self, service):
        """Explicit async + HttpRequest is a fail-fast programmer error."""
        with pytest.raises(ValueError):
            service.store_failure(
                domain="payment",
                failure_type="X",
                mode="async",
                request=object(),
            )

    def test_async_mode_enqueues_into_outbox_and_returns_no_id(self, service):
        """mode='async' dispatches into the outbox (dlq_id is None by contract)."""
        from baldur.services.dlq_outbox import outbox as outbox_module
        from baldur.services.dlq_outbox.outbox import Outbox

        mock_outbox = MagicMock(spec=Outbox)
        with (
            patch.object(outbox_module, "is_worker_dead", return_value=False),
            patch.object(outbox_module, "get_outbox", return_value=mock_outbox),
        ):
            result = service.store_failure(
                domain="payment", failure_type="X", mode="async"
            )

        assert result.success is True
        assert result.dlq_id is None
        mock_outbox.put.assert_called_once()

    def test_repo_create_failure_falls_back_to_local_without_raising(
        self, service, repo
    ):
        """Terminal fail-open: a repo write failure is captured to local
        fallback and never propagates into the protected call (§9.3)."""
        with (
            patch.object(repo, "create", side_effect=RuntimeError("redis down")),
            patch.object(
                service, "_write_to_local_fallback", return_value="/tmp/dlq.jsonl"
            ) as fallback,
        ):
            result = service.store_failure(
                domain="payment", failure_type="X", mode="sync"
            )

        assert result.success is False
        assert result.is_fallback is True
        assert result.fallback_path == "/tmp/dlq.jsonl"
        fallback.assert_called_once()

    def test_secret_in_oversize_field_masked_before_truncation(self, service, repo):
        """Redaction runs BEFORE size-cap truncation, so a secret inside an
        oversize dict can never survive into the stored value / preview."""
        request_data = {"password": "SUPERSECRET_VALUE", "filler": "x" * 200_000}

        with patch.object(repo, "create", wraps=repo.create) as create_spy:
            service.store_failure(
                domain="payment",
                failure_type="X",
                request_data=request_data,
                mode="sync",
            )

        stored = create_spy.call_args.kwargs["request_data"]
        assert "SUPERSECRET_VALUE" not in json.dumps(stored, default=str)


# =============================================================================
# Backing resolution chain — resolve_dlq_backing / resolve_dlq_backing_tier
# =============================================================================


class TestBackingChainResolution:
    """PRO (ACTIVE slot) wins the chain; else the OSS capture backing."""

    def test_resolve_returns_oss_backing_when_slot_empty(self, monkeypatch):
        """Empty slot → the OSS singleton backing, tier 'oss'."""
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)

        assert resolve_dlq_backing() is get_dlq_capture_service()
        assert resolve_dlq_backing_tier() == "oss"

    def test_resolve_prefers_pro_service_when_slot_registered(self, monkeypatch):
        """Registered slot → that service wins, tier 'pro'."""
        sentinel = object()
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: sentinel)

        assert resolve_dlq_backing() is sentinel
        assert resolve_dlq_backing_tier() == "pro"

    def test_singleton_caches_until_reset(self):
        """get_dlq_capture_service caches; reset forces a fresh instance."""
        reset_dlq_capture_service()
        first = get_dlq_capture_service()
        second = get_dlq_capture_service()
        assert first is second

        reset_dlq_capture_service()
        assert get_dlq_capture_service() is not first
