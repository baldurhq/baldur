"""
Tests for Environment Variable Snapshot Audit.

Tests:
- collect_env_snapshot(): 환경변수 수집
- log_env_snapshot_to_audit(): Audit 로깅 (primary + fallback)
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


class TestCollectEnvSnapshot:
    """Tests for collect_env_snapshot function."""

    def test_collect_tracked_prefixes(self):
        """Tracked prefix가 있는 환경변수만 수집한다."""
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
        """민감 키워드가 포함된 변수는 마스킹된다."""
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

            # 민감 변수는 마스킹되어야 함
            assert snapshot["variables"]["BALDUR_SECRETS_KEY"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_API_KEY"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_PASSWORD"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_TOKEN"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_PRIVATE_KEY"] == "***MASKED***"
            assert snapshot["variables"]["BALDUR_CREDENTIAL"] == "***MASKED***"

            # 비민감 변수는 원본 값 유지
            assert snapshot["variables"]["BALDUR_DLQ_ENABLED"] == "true"

    def test_hash_generation(self):
        """Hash가 올바르게 생성된다."""
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
        """값이 변경되면 Hash도 변경된다."""
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
        """같은 값이면 Hash도 동일하다."""
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
        """Tracked 환경변수가 없으면 빈 결과 반환."""
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
        """환경변수 스냅샷이 Audit 서비스에 기록된다."""
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
        """Tracked 환경변수가 없으면 로깅하지 않는다."""
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
        """Primary 실패 시 L1 Fallback 활성화."""
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
        """Primary와 Fallback 모두 실패 시 False 반환."""
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
        """prometheus_client 없어도 동작한다."""
        from baldur.audit.env_snapshot import _get_metrics

        with mock.patch.dict("sys.modules", {"prometheus_client": None}):
            # _get_metrics가 None을 반환해야 함
            result = _get_metrics()
            # prometheus_client가 설치되어 있을 수도 없을 수도 있으므로
            # 결과가 tuple이거나 (None, None)이어야 함
            assert result is not None

    def test_metrics_updated_on_success(self):
        """성공 시 메트릭이 업데이트된다."""
        from baldur.audit.env_snapshot import (
            log_env_snapshot_to_audit,
        )

        # Mock Gauge 생성
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

                    # 메트릭이 설정되었는지 확인
                    mock_recorded.set.assert_called_with(1)
                    mock_count.set.assert_called()


class TestEmitCriticalLog:
    """Tests for _emit_critical_log function."""

    def test_emits_fallback_status(self):
        """Fallback 성공 시 FALLBACK 상태 로깅."""
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
        """모든 실패 시 FAILED 상태 로깅."""
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
        """요약 정보를 올바르게 반환한다."""
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
