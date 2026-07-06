"""PRO durable wrapper unit tests (impl doc 486 D1, G8).

Test targets:
    - baldur_pro.services.dlq_outbox.durable.make_durable_sync_writer
    - baldur_pro.services.dlq_outbox.durable.setup_durable_outbox_if_enabled

Covers Test Assessment rows:
- ``TestDurableSyncWriterBehavior`` — persists to disk before downstream dispatch
- ``TestDurableSetupContract`` — monkeypatches ``_default_sync_writer`` only when
  ``durable=true``
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

from baldur_pro.services.dlq_outbox.durable import (
    make_durable_sync_writer,
    setup_durable_outbox_if_enabled,
)

# =============================================================================
# Behavior — make_durable_sync_writer
# =============================================================================


class TestDurableSyncWriterBehavior:
    """Wrapped writer persists to disk before invoking the original."""

    def test_durable_writer_persists_then_dispatches(self):
        """Order: ``DiskBufferAdapter.add(...)`` then ``original_sync_writer(kwargs)``."""
        # Given
        sequence: list[str] = []

        def original_writer(kwargs):
            sequence.append("original")
            return "original-result"

        mock_buffer = MagicMock()

        def fake_add(entry):
            sequence.append("disk_add")
            assert entry["category"] == "dlq_outbox_durable"
            assert entry["kwargs"] == {"domain": "payment", "failure_type": "PG"}

        mock_buffer.add = MagicMock(side_effect=fake_add)

        durable_writer = make_durable_sync_writer(original_writer)

        # When
        with patch(
            "baldur.audit.persistence.disk_buffer_adapter.DiskBufferAdapter.get_instance",
            return_value=mock_buffer,
        ):
            result = durable_writer({"domain": "payment", "failure_type": "PG"})

        # Then
        assert result == "original-result"
        assert sequence == ["disk_add", "original"]

    def test_durable_writer_dispatches_even_when_disk_persist_fails(self):
        """Disk persistence failure must not block the downstream DLQ DB write."""
        # Given
        original_calls: list[dict] = []

        def original_writer(kwargs):
            original_calls.append(kwargs)
            return "ok"

        mock_buffer = MagicMock()
        mock_buffer.add = MagicMock(side_effect=RuntimeError("LMDB unavailable"))

        durable_writer = make_durable_sync_writer(original_writer)

        # When
        with patch(
            "baldur.audit.persistence.disk_buffer_adapter.DiskBufferAdapter.get_instance",
            return_value=mock_buffer,
        ):
            result = durable_writer({"domain": "payment", "failure_type": "PG"})

        # Then — original called regardless of disk error
        assert result == "ok"
        assert len(original_calls) == 1

    def test_durable_writer_carries_iso_timestamp_in_entry(self):
        """Entry includes ISO-formatted timestamp for forensic ordering."""
        # Given
        captured: list[dict] = []

        mock_buffer = MagicMock()
        mock_buffer.add = MagicMock(side_effect=captured.append)

        durable_writer = make_durable_sync_writer(lambda k: None)

        # When
        with patch(
            "baldur.audit.persistence.disk_buffer_adapter.DiskBufferAdapter.get_instance",
            return_value=mock_buffer,
        ):
            durable_writer({"domain": "x", "failure_type": "y"})

        # Then
        assert len(captured) == 1
        entry = captured[0]
        assert "timestamp" in entry
        # ISO-8601 timestamp format check
        assert "T" in entry["timestamp"]


# =============================================================================
# Contract — setup_durable_outbox_if_enabled gates on settings
# =============================================================================


class TestDurableSetupContract:
    """``setup_durable_outbox_if_enabled`` only installs wrapper when durable=True."""

    def test_setup_returns_false_when_durable_disabled(self):
        # Given
        from baldur.settings.dlq_outbox import DLQOutboxSettings

        with patch(
            "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
            return_value=DLQOutboxSettings(durable=False),
        ):
            # When
            installed = setup_durable_outbox_if_enabled()

        # Then
        assert installed is False

    def test_setup_returns_false_when_settings_unavailable(self):
        """Settings load failure must NOT install the wrapper (safe default)."""
        # When
        with patch(
            "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
            side_effect=RuntimeError("settings down"),
        ):
            installed = setup_durable_outbox_if_enabled()

        # Then
        assert installed is False

    def test_setup_replaces_default_sync_writer_when_enabled(self):
        """When durable=True, ``outbox_module._default_sync_writer`` is replaced."""
        # Given
        from baldur.services.dlq_outbox import outbox as outbox_module
        from baldur.settings.dlq_outbox import DLQOutboxSettings

        original = outbox_module._default_sync_writer

        try:
            # When
            with patch(
                "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
                return_value=DLQOutboxSettings(durable=True),
            ):
                installed = setup_durable_outbox_if_enabled()

            # Then
            assert installed is True
            assert outbox_module._default_sync_writer is not original
        finally:
            outbox_module._default_sync_writer = original
