"""DLQSettings ``replay_request_data_max_bytes`` — the argument cap of a replayable job.

Test target: ``baldur.settings.dlq.DLQSettings.replay_request_data_max_bytes``,
bound to ``BALDUR_DLQ_REPLAY_REQUEST_DATA_MAX_BYTES``. A domain with a registered
replay handler keeps its parked arguments up to this many bytes, because a job
re-run from cut-short arguments would be lost.

UNIT_TEST_GUIDELINES.md: the default and the ``ge`` / ``le`` bounds are design
values, hardcoded (§0.1); each bound is tested at, just below and just above it
(§8.1).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.dlq import DLQSettings


class TestReplayRequestDataCapSettingsContract:
    """256 KiB by default, between 4 KiB and 1 MiB, from its own env var."""

    def test_default_is_256_kib(self):
        """A prompt, a document or a RAG context fits; 4 KiB would not."""
        assert DLQSettings().replay_request_data_max_bytes == 262_144

    @pytest.mark.parametrize(
        ("value", "accepted"),
        [
            (4095, False),
            (4096, True),
            (1_048_576, True),
            (1_048_577, False),
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_bounds(self, value, accepted):
        """``ge=4096`` (never below the ordinary cap's default), ``le=1_048_576``."""
        if accepted:
            assert (
                DLQSettings(
                    replay_request_data_max_bytes=value
                ).replay_request_data_max_bytes
                == value
            )
        else:
            with pytest.raises(ValidationError):
                DLQSettings(replay_request_data_max_bytes=value)

    def test_env_var_binds_the_field(self, monkeypatch):
        """``BALDUR_DLQ_REPLAY_REQUEST_DATA_MAX_BYTES`` sets it."""
        monkeypatch.setenv("BALDUR_DLQ_REPLAY_REQUEST_DATA_MAX_BYTES", "65536")

        assert DLQSettings().replay_request_data_max_bytes == 65_536

    def test_ordinary_cap_is_unchanged(self):
        """A domain no handler replays keeps the 4 KiB forensic cap."""
        assert DLQSettings().request_data_max_bytes == 4096
