"""Unit tests for the DLQ domain-cardinality alert (544 D3).

The cardinality alert is a soft observability signal that fires when the
``dlq:domains`` registry grows past the configured threshold. Two trigger
sites are exercised:

  - ``create()`` after a new-domain ZADD lands and the post-ZADD ZCARD
    exceeds the threshold (alert is keyed on the new-domain transition,
    not on every create; the ``_known_domains`` process-local cache
    dedups the post-ZADD ZCARD to one RTT per new domain).
  - ``_warm_domains_registry_if_needed`` after the one-time
    SCAN-then-ZADD warmup completes with ``ZCARD > threshold``.

The alert does NOT fire when:
  - The domain is already in ``_known_domains`` (known-domain create).
  - The post-ZADD / warmup-complete ZCARD is at or below the threshold.

A buggy upstream caller that injects ``str(uuid4())`` as a domain is the
canonical scenario the alert protects against -- the alert surfaces the
explosion before the operator notices via the panel or memory pressure.

Test classes:
    TestCardinalityAlertCreatePath -- parametrize matrix on
        (new domain / known domain) x (post-ZADD ZCARD > threshold / <= threshold).
    TestCardinalityAlertWarmupPath -- warmup-completion ZCARD parametrize on
        (> threshold / <= threshold).
    TestCardinalityAlertContract -- emission-shape contract (event name +
        structured ``domain_count`` + ``threshold`` + ``trigger`` fields).
"""

from __future__ import annotations

import itertools
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.redis.dlq_query import RedisDLQQuery

_ALERT_EVENT = "redis_dlq.domain_cardinality_alert"
_SETTINGS_PATCH = "baldur.settings.dlq.get_dlq_settings"


def _make_repo(backend: MagicMock | None = None) -> RedisDLQRepository:
    """Construct a RedisDLQRepository without running the real __init__.

    Mirrors the helper in ``test_dlq_502_blob_codec._make_repo``.
    """
    backend = backend or MagicMock()
    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo._entry_prefix = "dlq:entry:"
    repo._by_domain_prefix = "dlq:by_domain:"
    repo._status_prefix = "dlq:status:"
    repo._status_domain_prefix = "dlq:status_domain:"
    repo._all_key = "dlq:all"
    repo._domains_key = "dlq:domains"
    repo._known_domains = set()
    repo._pod_id = "pod-a"
    repo._pid = 100
    repo._run_nonce = "nonce0"
    repo._seq_counter = itertools.count()
    repo._compression_enabled = MagicMock(return_value=False)
    repo.query = RedisDLQQuery(repo)
    return repo


def _alert_entries(captured):
    """Extract just the cardinality-alert events from a capture_logs list."""
    return [e for e in captured if e.get("event") == _ALERT_EVENT]


# =============================================================================
# Create-path alert -- parametrized matrix
# =============================================================================


class TestCardinalityAlertCreatePathBehavior:
    """create() fires the alert iff (new domain AND post-ZADD ZCARD > threshold).

    Matrix:
        (new domain / known domain) x (ZCARD > threshold / <= threshold)
    """

    @pytest.mark.parametrize(
        ("known_domain", "zcard_value", "expected_fired"),
        [
            (False, 2048, True),
            (False, 1024, False),
            (False, 500, False),
            (True, 2048, False),
            (True, 100, False),
        ],
    )
    def test_alert_fires_only_for_new_domain_above_threshold(
        self, known_domain, zcard_value, expected_fired
    ):
        backend = MagicMock()
        backend.zcard.return_value = zcard_value
        repo = _make_repo(backend)

        with patch(
            _SETTINGS_PATCH,
            return_value=MagicMock(domain_cardinality_alert_threshold=1024),
        ):
            if known_domain:
                repo._known_domains.add("payment")

            with capture_logs() as captured:
                repo.create(domain="payment", failure_type="PG_TIMEOUT")

        assert (len(_alert_entries(captured)) > 0) is expected_fired

    def test_alert_dedupes_across_consecutive_creates_for_same_domain(self):
        """Second create() for the same domain skips the post-ZADD ZCARD
        entirely -- the ``_known_domains`` cache flips on first observation
        and the cardinality-alert helper is not re-invoked."""
        backend = MagicMock()
        backend.zcard.return_value = 9999
        repo = _make_repo(backend)

        with patch(
            _SETTINGS_PATCH,
            return_value=MagicMock(domain_cardinality_alert_threshold=1024),
        ):
            with capture_logs() as captured:
                repo.create(domain="payment", failure_type="t")
                repo.create(domain="payment", failure_type="t")

        assert len(_alert_entries(captured)) == 1
        assert backend.zcard.call_count == 1

    def test_alert_fires_per_distinct_new_domain(self):
        """Each distinct new domain that crosses the threshold gets its own
        alert -- the cache dedupes per-domain, not globally."""
        backend = MagicMock()
        backend.zcard.return_value = 2048
        repo = _make_repo(backend)

        with patch(
            _SETTINGS_PATCH,
            return_value=MagicMock(domain_cardinality_alert_threshold=1024),
        ):
            with capture_logs() as captured:
                repo.create(domain="payment", failure_type="t")
                repo.create(domain="auth", failure_type="t")
                repo.create(domain="inventory", failure_type="t")

        assert len(_alert_entries(captured)) == 3


# =============================================================================
# Warmup-path alert
# =============================================================================


class TestCardinalityAlertWarmupPathBehavior:
    """``_warm_domains_registry_if_needed`` fires the alert once on warmup
    completion when ZCARD exceeds the configured threshold."""

    @pytest.mark.parametrize(
        ("zcard_value", "expected_fired"),
        [
            (2048, True),
            (1024, False),
            (500, False),
            (0, False),
        ],
    )
    def test_alert_fires_only_when_warmup_zcard_exceeds_threshold(
        self, zcard_value, expected_fired
    ):
        backend = MagicMock()
        backend.zcard.return_value = zcard_value
        backend._get_full_key.side_effect = lambda key: key
        raw_client = MagicMock()
        raw_client.scan.return_value = (
            0,
            [
                b"dlq:by_domain:payment",
                b"dlq:by_domain:auth",
                b"dlq:by_domain:inventory",
            ],
        )
        repo = _make_repo(backend)
        with patch.object(
            type(repo),
            "_raw_redis_client",
            new=property(lambda self: raw_client),
        ):
            with patch(
                _SETTINGS_PATCH,
                return_value=MagicMock(domain_cardinality_alert_threshold=1024),
            ):
                with capture_logs() as captured:
                    result = repo.query._warm_domains_registry_if_needed()

        assert result is True
        assert (len(_alert_entries(captured)) > 0) is expected_fired

    def test_warmup_alert_fires_once_per_process(self):
        """Second call to the warmup helper is a no-op -- the cardinality
        alert does not re-fire on subsequent ZRANGE-served reads."""
        backend = MagicMock()
        backend.zcard.return_value = 9999
        backend._get_full_key.side_effect = lambda key: key
        raw_client = MagicMock()
        raw_client.scan.return_value = (0, [b"dlq:by_domain:payment"])
        repo = _make_repo(backend)

        with patch.object(
            type(repo),
            "_raw_redis_client",
            new=property(lambda self: raw_client),
        ):
            with patch(
                _SETTINGS_PATCH,
                return_value=MagicMock(domain_cardinality_alert_threshold=1024),
            ):
                with capture_logs() as captured:
                    repo.query._warm_domains_registry_if_needed()
                    repo.query._warm_domains_registry_if_needed()

        assert len(_alert_entries(captured)) == 1


# =============================================================================
# Emission contract -- event name + structured fields
# =============================================================================


class TestCardinalityAlertContract:
    """The alert's event name and structured payload are stable so the
    operator's log-search query keeps working."""

    def test_alert_carries_event_name_threshold_and_domain_count(self):
        backend = MagicMock()
        backend.zcard.return_value = 1500
        repo = _make_repo(backend)

        with patch(
            _SETTINGS_PATCH,
            return_value=MagicMock(domain_cardinality_alert_threshold=1024),
        ):
            with capture_logs() as captured:
                repo.create(domain="payment", failure_type="t")

        alerts = _alert_entries(captured)
        assert len(alerts) == 1
        entry = alerts[0]
        assert entry["domain_count"] == 1500
        assert entry["threshold"] == 1024
        assert entry["trigger"] in {"create_new_domain", "warmup_complete"}
        assert entry["log_level"] == "warning"

    def test_create_path_alert_carries_create_new_domain_trigger(self):
        backend = MagicMock()
        backend.zcard.return_value = 2048
        repo = _make_repo(backend)

        with patch(
            _SETTINGS_PATCH,
            return_value=MagicMock(domain_cardinality_alert_threshold=1024),
        ):
            with capture_logs() as captured:
                repo.create(domain="payment", failure_type="t")

        alerts = _alert_entries(captured)
        assert len(alerts) == 1
        assert alerts[0]["trigger"] == "create_new_domain"

    def test_warmup_path_alert_carries_warmup_complete_trigger(self):
        backend = MagicMock()
        backend.zcard.return_value = 2048
        backend._get_full_key.side_effect = lambda key: key
        raw_client = MagicMock()
        raw_client.scan.return_value = (0, [b"dlq:by_domain:payment"])
        repo = _make_repo(backend)

        with patch.object(
            type(repo),
            "_raw_redis_client",
            new=property(lambda self: raw_client),
        ):
            with patch(
                _SETTINGS_PATCH,
                return_value=MagicMock(domain_cardinality_alert_threshold=1024),
            ):
                with capture_logs() as captured:
                    repo.query._warm_domains_registry_if_needed()

        alerts = _alert_entries(captured)
        assert len(alerts) == 1
        assert alerts[0]["trigger"] == "warmup_complete"
