"""ContinuousAuditRecorder WAL configuration handling.

``wal_config`` is consumed only inside the ``wal_enabled`` branch, so supplying
one without enabling the WAL silently discards it — and with it
``wal_dir_operator_set``, which is the flag that makes an unwritable
operator-chosen directory fail loud instead of relocating audit data. The
discard is now announced.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from baldur.audit.continuous_audit import ContinuousAuditRecorder
from baldur.audit.wal import WALConfig
from baldur.interfaces.audit_adapter import AuditLogAdapter
from tests.factories.writable_dir import log_events

IGNORED_EVENT = "continuous_audit.wal_config_ignored"


@pytest.fixture
def audit_adapter():
    """Adapter double — these tests never record an entry through it."""
    return MagicMock(spec=AuditLogAdapter)


class TestContinuousAuditWALConfigIgnoredBehavior:
    """The silent-discard warning for a WAL config with the WAL disabled."""

    def test_wal_config_without_wal_enabled_warns(self, audit_adapter, tmp_path):
        """A discarded config takes the loud-failure promise down with it."""
        wal_config = WALConfig(
            wal_dir=str(tmp_path / "chosen-wal"), wal_dir_operator_set=True
        )

        with capture_logs() as logs:
            ContinuousAuditRecorder(
                audit_adapter=audit_adapter,
                wal_enabled=False,
                wal_config=wal_config,
                state_file=tmp_path / "chain.json",
            )

        records = log_events(logs, IGNORED_EVENT)
        assert len(records) == 1
        assert records[0]["wal_dir"] == wal_config.wal_dir
        assert records[0]["log_level"] == "warning"

    def test_wal_config_with_wal_enabled_stays_silent(
        self, writable_dir_chain, audit_adapter, tmp_path
    ):
        """Negative: the config is honored here, so there is nothing to announce."""
        wal_config = WALConfig(wal_dir=str(tmp_path / "wal"))

        with capture_logs() as logs:
            ContinuousAuditRecorder(
                audit_adapter=audit_adapter,
                wal_enabled=True,
                wal_config=wal_config,
                state_file=tmp_path / "chain.json",
            )

        assert log_events(logs, IGNORED_EVENT) == []

    def test_no_wal_config_stays_silent(self, audit_adapter, tmp_path):
        """Negative: nothing was supplied, so nothing was discarded."""
        with capture_logs() as logs:
            ContinuousAuditRecorder(
                audit_adapter=audit_adapter,
                wal_enabled=False,
                state_file=tmp_path / "chain.json",
            )

        assert log_events(logs, IGNORED_EVENT) == []
