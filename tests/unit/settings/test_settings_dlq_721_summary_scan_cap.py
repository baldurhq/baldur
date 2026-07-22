"""DLQSettings.compress_summary_scan_cap contract + boundary tests (721 D4).

The cap bounds the compressed-summary walk. When the compressed index holds
more than ``compress_summary_scan_cap`` entries the summary covers only the
newest ``cap`` of them, sets ``summary_truncated`` on the response, and logs a
WARNING; below the cap it is exact. The field is bounded (ge=100, le=100_000)
so an operator cannot set it so low the summary is useless nor so high the rail
never engages. Operators tune it via ``BALDUR_DLQ_COMPRESS_SUMMARY_SCAN_CAP``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.dlq import DLQSettings


class TestDLQCompressSummaryScanCapContract:
    """Default for the summary scan cap (721 D4)."""

    def test_default_cap_is_5000(self):
        # Per impl doc 721 D4: default 5000 -- every realistic PRO-tier
        # compressed history stays below it, so the summary is exact in the
        # common case and the rail only engages on very large histories.
        assert DLQSettings().compress_summary_scan_cap == 5000


class TestDLQCompressSummaryScanCapBoundaryContract:
    """``compress_summary_scan_cap`` ge=100, le=100_000.

    Boundary values are design specification -- hardcoded per §0.1.
    """

    def test_minimum_accepted(self):
        s = DLQSettings(compress_summary_scan_cap=100)
        assert s.compress_summary_scan_cap == 100

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQSettings(compress_summary_scan_cap=99)

    def test_maximum_accepted(self):
        s = DLQSettings(compress_summary_scan_cap=100_000)
        assert s.compress_summary_scan_cap == 100_000

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQSettings(compress_summary_scan_cap=100_001)


class TestDLQCompressSummaryScanCapEnvBindingBehavior:
    """The cap binds from ``BALDUR_DLQ_COMPRESS_SUMMARY_SCAN_CAP``.

    Env-var -> Pydantic field propagation is behavior -- verified via the
    actual binding mechanism (§0.2).
    """

    def test_cap_binds_from_env(self, monkeypatch):
        monkeypatch.setenv("BALDUR_DLQ_COMPRESS_SUMMARY_SCAN_CAP", "250")
        s = DLQSettings()
        assert s.compress_summary_scan_cap == 250
