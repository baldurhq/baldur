"""
Tests for Environment Variable Snapshot Audit.

Tests:
- collect_env_snapshot(): environment variable collection
- log_env_snapshot_to_audit(): audit logging (primary + fallback)
- Sensitive value masking
- Hash generation for change detection
- L1 Fallback (local file)
- Prometheus metrics
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest


class TestCollectEnvSnapshot:
    """Tests for collect_env_snapshot function."""

    def test_collect_tracked_prefixes(self):
        """Only environment variables with a tracked prefix are collected."""
        from baldur.audit.env_snapshot import (
            collect_env_snapshot,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DLQ_ENABLED": "true",
                "CIRCUIT_BREAKER_THRESHOLD": "5",
                "DLQ_MAX_RETRIES": "3",
                "SLA_TIMEOUT_MS": "5000",
                "CHAOS_ENABLED": "false",
                "UNRELATED_VAR": "should_be_ignored",
                "DATABASE_URL": "should_also_be_ignored",
            },
            clear=True,
        ):
            snapshot = collect_env_snapshot()

            assert snapshot["count"] == 5
            assert "BALDUR_DLQ_ENABLED" in snapshot["variables"]
            assert "CIRCUIT_BREAKER_THRESHOLD" in snapshot["variables"]
            assert "DLQ_MAX_RETRIES" in snapshot["variables"]
            assert "SLA_TIMEOUT_MS" in snapshot["variables"]
            assert "CHAOS_ENABLED" in snapshot["variables"]
            assert "UNRELATED_VAR" not in snapshot["variables"]
            assert "DATABASE_URL" not in snapshot["variables"]

    def test_sensitive_values_are_masked(self):
        """A variable whose name contains a sensitive keyword is masked."""
        from baldur.audit.env_snapshot import collect_env_snapshot

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SECRETS_KEY": "super-secret-value",
                "BALDUR_API_KEY": "api-key-12345",
                "BALDUR_PASSWORD": "my-password",
                "BALDUR_TOKEN": "bearer-token",
                "BALDUR_PRIVATE_KEY": "private-key-data",
                "BALDUR_CREDENTIAL": "user:pass",
                "BALDUR_DLQ_ENABLED": "true",  # non-sensitive
            },
            clear=True,
        ):
            snapshot = collect_env_snapshot()

            # Sensitive variables are masked
            assert snapshot["variables"]["BALDUR_SECRETS_KEY"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_API_KEY"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_PASSWORD"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_TOKEN"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_PRIVATE_KEY"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_CREDENTIAL"] == "***MASKED***"

            # Non-sensitive variables keep their value
            assert snapshot["variables"]["BALDUR_DLQ_ENABLED"] == "true"

    @pytest.mark.parametrize(
        ("name", "value", "recorded"),
        [
            (
                "BALDUR_SQL_DSN",
                "postgresql://app:s3cretpw@db.internal:5432/app",
                "postgresql://app:***@db.internal:5432/app",
            ),
            (
                "BALDUR_REDIS_URL",
                "redis://:s3cretpw@cache.internal:6379/0",
                "redis://:***@cache.internal:6379/0",
            ),
            (
                "BALDUR_LEADER_ELECTION_REDIS_URL",
                "redis+sentinel://:s3cretpw@s1:26379,s2:26379/mymaster",
                "redis+sentinel://:***@s1:26379,s2:26379/mymaster",
            ),
            ("BALDUR_REDIS_URL", "redis://cache.internal:6379/0", None),
        ],
        ids=["sql_dsn", "redis_url", "sentinel_url", "url_without_password"],
    )
    def test_a_password_embedded_in_a_url_value_is_masked(self, name, value, recorded):
        """A connection URL carries its credential in the value, not the name.

        The password is replaced and the rest of the URL is kept, so the
        snapshot still says which host the process was pointed at; a URL
        without a password is recorded as set.
        """
        from baldur.audit.env_snapshot import collect_env_snapshot

        with mock.patch.dict(os.environ, {name: value}, clear=True):
            snapshot = collect_env_snapshot()

        assert snapshot["variables"][name] == (value if recorded is None else recorded)
        assert "s3cretpw" not in json.dumps(snapshot)

    @pytest.mark.parametrize(
        "name",
        [
            "BALDUR_CHANNEL_TARGET_SLACK_WEBHOOK_URL",
            "BALDUR_CHANNEL_TARGET_WEBHOOK_URLS",
            "BALDUR_CHANNEL_TARGET_WEBHOOK_HEADERS",
            "BALDUR_PROMETHEUS_HEADERS",
        ],
    )
    def test_webhook_urls_and_headers_are_masked_whole(self, name):
        """A webhook URL carries its secret in the path and a headers variable
        carries Authorization; neither has a sensitive word in its name."""
        from baldur.audit.env_snapshot import collect_env_snapshot

        with mock.patch.dict(
            os.environ,
            {
                name: "https://hooks.example/services/T0/B0/s3cretpath",
                "BALDUR_HTTP_CLIENT_WEBHOOK_TIMEOUT": "5",
            },
            clear=True,
        ):
            snapshot = collect_env_snapshot()

        assert snapshot["variables"][name] == "***MASKED***"
        assert snapshot["variables"]["BALDUR_HTTP_CLIENT_WEBHOOK_TIMEOUT"] == "5"

    def test_a_value_the_url_parser_rejects_is_masked_whole(self):
        """An unparsable value is withheld rather than recorded unmasked."""
        from baldur.audit.env_snapshot import collect_env_snapshot

        with mock.patch.dict(
            os.environ,
            {"BALDUR_REDIS_URL": "redis://:s3cretpw@[::1:6379/0"},
            clear=True,
        ):
            snapshot = collect_env_snapshot()

        assert snapshot["variables"]["BALDUR_REDIS_URL"] == "***MASKED***"

    def test_hash_generation(self):
        """The hash is generated in the expected format."""
        from baldur.audit.env_snapshot import collect_env_snapshot

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_A": "value_a",
                "BALDUR_B": "value_b",
            },
            clear=True,
        ):
            snapshot = collect_env_snapshot()

            assert snapshot["hash"].startswith("sha256:")
            assert len(snapshot["hash"]) == 23  # "sha256:" + 16 chars

    def test_hash_changes_with_values(self):
        """A changed value changes the hash."""
        from baldur.audit.env_snapshot import collect_env_snapshot

        with mock.patch.dict(
            os.environ,
            {"BALDUR_TEST": "value1"},
            clear=True,
        ):
            snapshot1 = collect_env_snapshot()

        with mock.patch.dict(
            os.environ,
            {"BALDUR_TEST": "value2"},
            clear=True,
        ):
            snapshot2 = collect_env_snapshot()

        assert snapshot1["hash"] != snapshot2["hash"]

    def test_hash_same_for_same_values(self):
        """The same values give the same hash."""
        from baldur.audit.env_snapshot import collect_env_snapshot

        with mock.patch.dict(
            os.environ,
            {"BALDUR_TEST": "same_value"},
            clear=True,
        ):
            snapshot1 = collect_env_snapshot()
            snapshot2 = collect_env_snapshot()

        assert snapshot1["hash"] == snapshot2["hash"]

    def test_empty_env(self):
        """No tracked environment variable gives an empty result."""
        from baldur.audit.env_snapshot import collect_env_snapshot

        with mock.patch.dict(
            os.environ,
            {"UNRELATED_VAR": "value"},
            clear=True,
        ):
            snapshot = collect_env_snapshot()

            assert snapshot["count"] == 0
            assert snapshot["variables"] == {}
            assert snapshot["hash"].startswith("sha256:")


class TestLogEnvSnapshotToAudit:
    """Tests for log_env_snapshot_to_audit function."""

    def test_logs_to_audit_service(self):
        """The environment snapshot is recorded through the audit service."""
        from baldur.audit.env_snapshot import log_env_snapshot_to_audit

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_TEST": "value",
                "CIRCUIT_BREAKER_THRESHOLD": "5",
            },
            clear=True,
        ):
            with mock.patch(
                "baldur.audit.env_snapshot._log_to_audit_service"
            ) as mock_log:
                mock_log.return_value = True

                result = log_env_snapshot_to_audit()

                assert result is True
                mock_log.assert_called_once()

    def test_skips_when_no_tracked_vars(self):
        """Nothing is logged when no tracked environment variable is set."""
        from baldur.audit.env_snapshot import log_env_snapshot_to_audit

        with mock.patch.dict(
            os.environ,
            {"UNRELATED_VAR": "value"},
            clear=True,
        ):
            with mock.patch(
                "baldur.audit.env_snapshot._log_to_audit_service"
            ) as mock_log:
                result = log_env_snapshot_to_audit()

                assert result is True
                mock_log.assert_not_called()

    def test_fallback_on_primary_failure(self):
        """A primary failure activates the L1 fallback."""
        from baldur.audit.env_snapshot import log_env_snapshot_to_audit

        with mock.patch.dict(
            os.environ,
            {"BALDUR_TEST": "value"},
            clear=True,
        ):
            with mock.patch(
                "baldur.audit.env_snapshot._log_to_audit_service",
                return_value=False,
            ):
                with mock.patch(
                    "baldur.audit.env_snapshot._log_to_fallback",
                    return_value=True,
                ) as mock_fallback:
                    result = log_env_snapshot_to_audit()

                    assert result is True
                    mock_fallback.assert_called_once()

    def test_returns_false_when_all_fail(self):
        """Returns False when both the primary and the fallback fail."""
        from baldur.audit.env_snapshot import log_env_snapshot_to_audit

        with mock.patch.dict(
            os.environ,
            {"BALDUR_TEST": "value"},
            clear=True,
        ):
            with mock.patch(
                "baldur.audit.env_snapshot._log_to_audit_service",
                return_value=False,
            ):
                with mock.patch(
                    "baldur.audit.env_snapshot._log_to_fallback",
                    return_value=False,
                ):
                    result = log_env_snapshot_to_audit()

                    assert result is False


class TestLogToAuditService:
    """Tests for _log_to_audit_service function."""

    def test_calls_audit_module(self):
        """Calls log_config_change when audit subsystem is enabled (416 D7)."""
        from baldur.audit.env_snapshot import _log_to_audit_service
        from baldur.settings.audit import override_audit_settings

        snapshot = {
            "variables": {"BALDUR_TEST": "value"},
            "hash": "sha256:abc123",
            "count": 1,
        }

        with override_audit_settings(enabled=True):
            with mock.patch("baldur.audit.log_config_change") as mock_log:
                mock_log.return_value = True

                result = _log_to_audit_service(snapshot)

                assert result is True
                mock_log.assert_called_once()

                call_kwargs = mock_log.call_args.kwargs
                assert call_kwargs["config_type"] == "environment_variables"
                assert call_kwargs["config_key"] == "startup_snapshot"
                assert call_kwargs["old_value"] is None
                assert call_kwargs["user"] == "system_startup"
                assert call_kwargs["metadata"]["hash"] == "sha256:abc123"
                assert call_kwargs["metadata"]["variable_count"] == 1

    def test_disabled_returns_true_without_calling_primary(self):
        """416 D7: when audit is disabled, return True (silenced) without calling primary."""
        from baldur.audit.env_snapshot import _log_to_audit_service
        from baldur.settings.audit import override_audit_settings

        snapshot = {
            "variables": {"BALDUR_TEST": "value"},
            "hash": "sha256:abc123",
            "count": 1,
        }
        with override_audit_settings(enabled=False):
            with mock.patch("baldur.audit.log_config_change") as mock_log:
                result = _log_to_audit_service(snapshot)
                assert result is True
                mock_log.assert_not_called()

    def test_disabled_blocks_fallback_path(self):
        """416 D7 defense-in-depth: _log_to_fallback also early-returns when disabled."""
        from baldur.audit.env_snapshot import _log_to_fallback
        from baldur.settings.audit import override_audit_settings

        snapshot = {
            "variables": {"BALDUR_TEST": "value"},
            "hash": "sha256:abc123",
            "count": 1,
        }
        with override_audit_settings(enabled=False):
            assert _log_to_fallback(snapshot) is False

    def test_handles_import_error(self):
        """Returns False on ImportError when audit is enabled."""
        from baldur.audit.env_snapshot import _log_to_audit_service
        from baldur.settings.audit import override_audit_settings

        snapshot = {
            "variables": {"BALDUR_TEST": "value"},
            "hash": "sha256:abc123",
            "count": 1,
        }

        with override_audit_settings(enabled=True):
            with mock.patch(
                "baldur.audit.log_config_change",
                side_effect=ImportError("Module not found"),
            ):
                result = _log_to_audit_service(snapshot)
                assert result is False

    def test_handles_exception(self):
        """Returns False on unexpected exception when audit is enabled."""
        from baldur.audit.env_snapshot import _log_to_audit_service
        from baldur.settings.audit import override_audit_settings

        snapshot = {
            "variables": {"BALDUR_TEST": "value"},
            "hash": "sha256:abc123",
            "count": 1,
        }

        with override_audit_settings(enabled=True):
            with mock.patch(
                "baldur.audit.log_config_change",
                side_effect=Exception("Unexpected error"),
            ):
                result = _log_to_audit_service(snapshot)
                assert result is False


class TestLogToFallback:
    """Tests for L1 Fallback (_log_to_fallback).

    416 D7: every test in this class must run under
    ``override_audit_settings(enabled=True)`` because the fallback path
    is now defense-in-depth gated by the master switch.
    """

    def test_writes_to_fallback_file(self):
        """Fallback file is written as JSON Lines (when audit enabled)."""
        from baldur.audit.env_snapshot import _log_to_fallback
        from baldur.settings.audit import override_audit_settings

        snapshot = {
            "variables": {"BALDUR_TEST": "value"},
            "hash": "sha256:abc123",
            "count": 1,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            fallback_path = Path(tmpdir) / "env_snapshot_fallback.jsonl"

            with (
                override_audit_settings(enabled=True),
                mock.patch(
                    "baldur.audit.env_snapshot.FALLBACK_LOG_PATH",
                    str(fallback_path),
                ),
            ):
                result = _log_to_fallback(snapshot)

                assert result is True
                assert fallback_path.exists()

                with open(fallback_path) as f:
                    line = f.readline()
                    data = json.loads(line)

                    assert data["event"] == "env_snapshot_fallback"
                    assert data["hash"] == "sha256:abc123"
                    assert data["variable_count"] == 1
                    assert "timestamp" in data

    def test_appends_to_existing_file(self):
        """Append to existing file."""
        from baldur.audit.env_snapshot import _log_to_fallback
        from baldur.settings.audit import override_audit_settings

        with tempfile.TemporaryDirectory() as tmpdir:
            fallback_path = Path(tmpdir) / "env_snapshot_fallback.jsonl"
            fallback_path.write_text('{"existing": "entry"}\n')

            snapshot = {
                "variables": {"BALDUR_TEST": "value"},
                "hash": "sha256:abc123",
                "count": 1,
            }

            with (
                override_audit_settings(enabled=True),
                mock.patch(
                    "baldur.audit.env_snapshot.FALLBACK_LOG_PATH",
                    str(fallback_path),
                ),
            ):
                result = _log_to_fallback(snapshot)

                assert result is True

                lines = fallback_path.read_text().strip().split("\n")
                assert len(lines) == 2

    def test_handles_write_error(self):
        """Returns False on write failure."""
        from baldur.audit.env_snapshot import _log_to_fallback
        from baldur.settings.audit import override_audit_settings

        snapshot = {
            "variables": {"BALDUR_TEST": "value"},
            "hash": "sha256:abc123",
            "count": 1,
        }

        with (
            override_audit_settings(enabled=True),
            mock.patch(
                "baldur.audit.env_snapshot.FALLBACK_LOG_PATH",
                "/nonexistent/path/that/will/fail/file.jsonl",
            ),
        ):
            with mock.patch(
                "builtins.open", side_effect=PermissionError("Access denied")
            ):
                result = _log_to_fallback(snapshot)
                assert result is False


class TestPrometheusMetrics:
    """Tests for Prometheus metrics."""

    def test_get_metrics_without_prometheus(self):
        """Works without prometheus_client."""
        from baldur.audit.env_snapshot import _get_metrics

        with mock.patch.dict("sys.modules", {"prometheus_client": None}):
            # _get_metrics must not fail
            result = _get_metrics()
            # prometheus_client may or may not be installed, so
            # the result is a tuple, possibly (None, None)
            assert result is not None

    def test_metrics_updated_on_success(self):
        """The metrics are updated on success."""
        from baldur.audit.env_snapshot import (
            log_env_snapshot_to_audit,
        )

        # Build mock gauges
        mock_recorded = mock.Mock()
        mock_count = mock.Mock()

        with mock.patch.dict(
            os.environ,
            {"BALDUR_TEST": "value"},
            clear=True,
        ):
            with mock.patch(
                "baldur.audit.env_snapshot._get_metrics",
                return_value=(mock_recorded, mock_count),
            ):
                with mock.patch(
                    "baldur.audit.env_snapshot._log_to_audit_service",
                    return_value=True,
                ):
                    log_env_snapshot_to_audit()

                    # The metrics were set
                    mock_recorded.set.assert_called_with(1)
                    mock_count.set.assert_called()


class TestEmitCriticalLog:
    """Tests for _emit_critical_log function."""

    def test_emits_fallback_status(self):
        """A successful fallback logs the FALLBACK status."""
        from baldur.audit.env_snapshot import _emit_critical_log

        snapshot = {
            "hash": "sha256:abc123",
            "count": 1,
        }

        with mock.patch("baldur.audit.env_snapshot.logger") as mock_logger:
            _emit_critical_log(snapshot, primary_success=False, fallback_success=True)

            mock_logger.critical.assert_called_once()
            log_message = mock_logger.critical.call_args[0][0]
            assert log_message == "env_audit.snapshot"
            call_kwargs = mock_logger.critical.call_args[1]
            assert call_kwargs["snapshot_status"] == "FALLBACK"
            assert call_kwargs["snapshot"] == "sha256:abc123"

    def test_emits_failed_status(self):
        """A total failure logs the FAILED status."""
        from baldur.audit.env_snapshot import _emit_critical_log

        snapshot = {
            "hash": "sha256:abc123",
            "count": 1,
        }

        with mock.patch("baldur.audit.env_snapshot.logger") as mock_logger:
            _emit_critical_log(snapshot, primary_success=False, fallback_success=False)

            mock_logger.critical.assert_called_once()
            log_message = mock_logger.critical.call_args[0][0]
            assert log_message == "env_audit.snapshot"
            call_kwargs = mock_logger.critical.call_args[1]
            assert call_kwargs["snapshot_status"] == "FAILED"


class TestGetEnvSnapshotSummary:
    """Tests for get_env_snapshot_summary function."""

    def test_returns_summary(self):
        """Returns the summary."""
        from baldur.audit.env_snapshot import (
            TRACKED_PREFIXES,
            get_env_snapshot_summary,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_A": "1",
                "BALDUR_B": "2",
                "DLQ_ENABLED": "true",
            },
            clear=True,
        ):
            summary = get_env_snapshot_summary()

            assert summary["count"] == 3
            assert summary["hash"].startswith("sha256:")
            assert summary["tracked_prefixes"] == TRACKED_PREFIXES
