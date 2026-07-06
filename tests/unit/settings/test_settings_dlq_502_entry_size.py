"""DLQSettings entry-size fields unit tests (#502 D1).

Test targets:
    - baldur.settings.dlq.DLQSettings new fields (#502):
        * entry_payload_compression_enabled (bool, default True)
        * request_data_max_bytes (int, default 4096, ge=256, le=1_048_576)
        * field_max_bytes (int, default 4096, ge=256, le=1_048_576)
        * truncate_blocks_replay (bool, default True)
    - BALDUR_DLQ_* env-var binding

Test Categories:
    A. Contract — defaults from impl doc 502 D1
    B. Behavior — boundary validation (ge/le constraints)
    C. Behavior — env-var binding via BALDUR_DLQ_ prefix
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.dlq import DLQSettings

# =============================================================================
# A. Contract — defaults declared in impl doc 502 D1
# =============================================================================


class TestDLQSettings502Contract:
    """Default values for #502 entry-size fields, per impl doc 502 D1."""

    def test_entry_payload_compression_enabled_default_is_true(self):
        # D6: zlib compression default-on for ~50-70% per-entry savings.
        assert DLQSettings().entry_payload_compression_enabled is True

    def test_request_data_max_bytes_default_is_4096(self):
        # D7: forensic cap for request_data field.
        assert DLQSettings().request_data_max_bytes == 4096

    def test_field_max_bytes_default_is_4096(self):
        # D7: forensic cap for snapshot/response/metadata fields.
        assert DLQSettings().field_max_bytes == 4096

    def test_truncate_blocks_replay_default_is_true(self):
        # D7: conservative default — truncated entries do not auto-replay.
        assert DLQSettings().truncate_blocks_replay is True


# =============================================================================
# B. Behavior — boundary validation (Pydantic ge/le)
# =============================================================================


class TestRequestDataMaxBytesBoundaryContract:
    """``request_data_max_bytes`` ge=256, le=1_048_576."""

    def test_minimum_accepted(self):
        DLQSettings(request_data_max_bytes=256)

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQSettings(request_data_max_bytes=255)

    def test_maximum_accepted(self):
        DLQSettings(request_data_max_bytes=1_048_576)

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQSettings(request_data_max_bytes=1_048_577)


class TestFieldMaxBytesBoundaryContract:
    """``field_max_bytes`` ge=256, le=1_048_576."""

    def test_minimum_accepted(self):
        DLQSettings(field_max_bytes=256)

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQSettings(field_max_bytes=255)

    def test_maximum_accepted(self):
        DLQSettings(field_max_bytes=1_048_576)

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQSettings(field_max_bytes=1_048_577)


# =============================================================================
# C. Behavior — env-var binding (BALDUR_DLQ_*)
# =============================================================================


class TestDLQSettings502EnvBinding:
    """Pydantic env binding uses BALDUR_DLQ_ prefix per D1."""

    def test_entry_payload_compression_enabled_binds_from_env(self, monkeypatch):
        monkeypatch.setenv("BALDUR_DLQ_ENTRY_PAYLOAD_COMPRESSION_ENABLED", "false")
        s = DLQSettings()
        assert s.entry_payload_compression_enabled is False

    def test_request_data_max_bytes_binds_from_env(self, monkeypatch):
        monkeypatch.setenv("BALDUR_DLQ_REQUEST_DATA_MAX_BYTES", "8192")
        s = DLQSettings()
        assert s.request_data_max_bytes == 8192

    def test_field_max_bytes_binds_from_env(self, monkeypatch):
        monkeypatch.setenv("BALDUR_DLQ_FIELD_MAX_BYTES", "2048")
        s = DLQSettings()
        assert s.field_max_bytes == 2048

    def test_truncate_blocks_replay_binds_from_env(self, monkeypatch):
        monkeypatch.setenv("BALDUR_DLQ_TRUNCATE_BLOCKS_REPLAY", "false")
        s = DLQSettings()
        assert s.truncate_blocks_replay is False
