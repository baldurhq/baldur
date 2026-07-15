"""Unit test for the ``dlq_capture_backing`` startup-report field (D7).

``baldur.init()``'s ``_build_startup_report`` reports the resolved DLQ capture
tier so an operator can see whether ``dlq=True`` durably captures on this
install. No ``setup_*`` function is introduced (the backing resolves lazily).
"""

from __future__ import annotations

from baldur.bootstrap import ExtensionResult, _build_startup_report
from baldur.factory.registry import ProviderRegistry


class TestStartupReportContract:
    """The report tier field mirrors the resolved backing (pro / oss)."""

    def test_report_tier_oss_when_slot_empty(self, monkeypatch):
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)

        report = _build_startup_report(ExtensionResult())

        assert report["dlq_capture_backing"] == "oss"

    def test_report_tier_pro_when_slot_registered(self, monkeypatch):
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: object())

        report = _build_startup_report(ExtensionResult())

        assert report["dlq_capture_backing"] == "pro"
