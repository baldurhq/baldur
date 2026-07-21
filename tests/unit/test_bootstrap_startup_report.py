"""Startup-report visibility for durability directory resolutions.

An operator could not previously distinguish "checkpointing works" from
"checkpointing is silently dead" without reading debug logs. The report now
carries every resolution, and boot warns when the two audit surfaces disagree
about durability — that split resets the audit resume point to 0 and re-reads
the surviving WAL files as duplicate events.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from baldur.bootstrap import (
    _AUDIT_CHECKPOINT_ENV_VAR,
    _AUDIT_CHECKPOINT_PURPOSE,
    _AUDIT_WAL_ENV_VAR,
    _AUDIT_WAL_PURPOSE,
    ExtensionResult,
    _build_startup_report,
    _find_resolution,
    _warn_on_audit_durability_split,
)
from baldur.utils.fs import resolve_writable_dir
from tests.factories.writable_dir import log_events

SPLIT_EVENT = "storage.audit_dir_durability_split"


def _entry(status: str, path: str) -> dict[str, str]:
    """Build one registry entry as the primitive records it."""
    return {"status": status, "path": path, "preferred": "/var/log/audit"}


def _registry(checkpoint_status: str, wal_status: str) -> dict[str, dict[str, str]]:
    """Build a registry holding both audit surfaces at chosen statuses."""
    return {
        f"{_AUDIT_CHECKPOINT_PURPOSE}-aaaaaaaa": _entry(
            checkpoint_status, "/tmp/checkpoint"
        ),
        f"{_AUDIT_WAL_PURPOSE}-bbbbbbbb": _entry(wal_status, "/var/log/audit/wal"),
    }


class TestAuditDurabilitySplitWarningBehavior:
    """Detection of a checkpoint/WAL durability mismatch at boot."""

    def test_differing_statuses_warn_naming_both_paths_and_env_vars(self):
        """The operator's remedy is to set both audit variables or neither."""
        storage_dirs = _registry("fallback", "ok")

        with capture_logs() as logs:
            _warn_on_audit_durability_split(storage_dirs)

        records = log_events(logs, SPLIT_EVENT)
        assert len(records) == 1
        assert records[0]["checkpoint_status"] == "fallback"
        assert records[0]["wal_status"] == "ok"
        assert records[0]["checkpoint_path"] == "/tmp/checkpoint"
        assert records[0]["wal_path"] == "/var/log/audit/wal"
        assert records[0]["checkpoint_env"] == _AUDIT_CHECKPOINT_ENV_VAR
        assert records[0]["wal_env"] == _AUDIT_WAL_ENV_VAR
        assert records[0]["log_level"] == "warning"

    @pytest.mark.parametrize(
        "status", ["ok", "fallback", "unwritable"], ids=["ok", "fallback", "unwritable"]
    )
    def test_matching_statuses_stay_silent(self, status):
        """Negative: agreement is the healthy case, whatever the shared status."""
        with capture_logs() as logs:
            _warn_on_audit_durability_split(_registry(status, status))

        assert log_events(logs, SPLIT_EVENT) == []

    def test_missing_checkpoint_resolution_stays_silent(self):
        """Negative: an audit-disabled boot resolves neither, so there is no split."""
        storage_dirs = _registry("fallback", "ok")
        del storage_dirs[f"{_AUDIT_CHECKPOINT_PURPOSE}-aaaaaaaa"]

        with capture_logs() as logs:
            _warn_on_audit_durability_split(storage_dirs)

        assert log_events(logs, SPLIT_EVENT) == []

    def test_missing_wal_resolution_stays_silent(self):
        """Negative: one surface alone cannot disagree with anything."""
        storage_dirs = _registry("fallback", "ok")
        del storage_dirs[f"{_AUDIT_WAL_PURPOSE}-bbbbbbbb"]

        with capture_logs() as logs:
            _warn_on_audit_durability_split(storage_dirs)

        assert log_events(logs, SPLIT_EVENT) == []

    def test_empty_registry_stays_silent(self):
        """Negative: a boot that resolved nothing must not warn."""
        with capture_logs() as logs:
            _warn_on_audit_durability_split({})

        assert log_events(logs, SPLIT_EVENT) == []


class TestResolutionLookupBehavior:
    """Recovering a surface from a hashed registry key."""

    def test_entry_is_found_by_its_purpose_prefix(self):
        """Keys carry a digest suffix, so lookup is by prefix."""
        storage_dirs = _registry("ok", "ok")

        found = _find_resolution(storage_dirs, _AUDIT_CHECKPOINT_PURPOSE)

        assert found is not None
        assert found["path"] == "/tmp/checkpoint"

    def test_unknown_purpose_returns_none(self):
        """A surface that never resolved has no entry."""
        assert _find_resolution(_registry("ok", "ok"), "disk_buffer") is None

    def test_lookup_does_not_match_a_purpose_that_merely_shares_a_stem(self):
        """Negative: ``checkpoint`` must not pick up ``checkpoint_kafka_backup``."""
        storage_dirs = {"checkpoint_kafka_backup-cccccccc": _entry("ok", "/tmp/backup")}

        assert _find_resolution(storage_dirs, _AUDIT_CHECKPOINT_PURPOSE) is None


class TestStartupReportStorageDirsBehavior:
    """The report field that carries resolutions to the operator."""

    def test_report_carries_every_recorded_resolution(
        self, writable_dir_chain, deny_dir, tmp_path
    ):
        """Boot-time counterpart to the primitive's one-time warning."""
        # Given — one healthy surface and one that fell back
        resolve_writable_dir(
            tmp_path / "configured", purpose="wal_resilient_storage", operator_set=False
        )
        preferred = tmp_path / "unwritable"
        deny_dir(preferred)
        resolve_writable_dir(preferred, purpose="checkpoint", operator_set=False)

        # When
        report = _build_startup_report(ExtensionResult())

        # Then
        statuses = {entry["status"] for entry in report["storage_dirs"].values()}
        assert statuses == {"ok", "fallback"}

    def test_report_storage_dirs_is_a_detached_copy(self, writable_dir_chain, tmp_path):
        """Immutability: mutating the report must not corrupt the registry."""
        resolve_writable_dir(
            tmp_path / "configured", purpose="checkpoint", operator_set=False
        )

        report = _build_startup_report(ExtensionResult())
        key = next(iter(report["storage_dirs"]))
        report["storage_dirs"][key]["status"] = "tampered"

        assert (
            _build_startup_report(ExtensionResult())["storage_dirs"][key]["status"]
            == "ok"
        )

    def test_report_stays_buildable_when_resolution_lookup_fails(
        self, writable_dir_chain, monkeypatch
    ):
        """Fail-open: a broken registry must not take the whole boot report down."""
        monkeypatch.setattr(
            "baldur.utils.fs.get_writable_dir_resolutions",
            lambda: (_ for _ in ()).throw(RuntimeError("registry broken")),
        )

        report = _build_startup_report(ExtensionResult())

        assert report["storage_dirs"] == {}
