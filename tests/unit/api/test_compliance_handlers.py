"""Compliance handlers — unit tests (523 Step 9).

Target: ``baldur.api.handlers.compliance`` — framework-agnostic Compliance
HTTP endpoints (standards / checks / run / reports / report_detail /
pending_evidence / evidence_review) plus the private ``_engine`` lazy
provider lookup.

Verification techniques applied (§8):
  - §8.2 Exception/edge cases — engine missing (503), unknown standard
    (400), invalid pagination (400), report-not-found (404), missing
    required body fields (400), ValueError-from-engine wrapping (400/404).
  - §8.4 Side effects — engine method invocation verified per route.
  - §8.5 Dependency interaction — ``get_compliance_engine`` and
    ``get_continuous_audit_recorder`` are surface-mocked via
    ``unittest.mock.patch``.

All "engine" objects in tests are ``MagicMock``; the OSS handlers under test
are pure response-formatting plus dispatch glue.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.compliance import (
    _engine,
    compliance_checks,
    compliance_evidence_review,
    compliance_pending_evidence,
    compliance_report_detail,
    compliance_reports,
    compliance_run,
    compliance_standards,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext

# =============================================================================
# Fixtures
# =============================================================================


def _make_ctx(
    method: str = "GET",
    path: str = "/compliance/",
    query: dict | None = None,
    path_params: dict | None = None,
    json_body: dict | None = None,
) -> RequestContext:
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params=path_params or {},
        json_body=json_body,
    )


def _standard(value: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(value=value, name=name)


def _check_dict(check_id: str = "C1") -> SimpleNamespace:
    """Stand-in for CheckDefinition exposing to_dict()."""
    m = MagicMock()
    m.to_dict.return_value = {"check_id": check_id, "name": f"check_{check_id}"}
    return m


@pytest.fixture
def mock_engine():
    """Mock compliance engine wired into the lazy lookup."""
    engine = MagicMock(name="ComplianceEngine")
    engine.list_standards.return_value = [
        _standard("DORA_2025", "DORA_2025"),
        _standard("SOC2", "SOC2"),
    ]
    engine.list_checks.return_value = [_check_dict("C1"), _check_dict("C2")]
    return engine


@pytest.fixture
def wired_engine(monkeypatch, mock_engine):
    """Wire the mock engine into the ProviderRegistry slot (599 D7)."""
    from baldur.factory.registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry.compliance_engine,
        "safe_get",
        lambda *a, **kw: mock_engine,
    )
    return mock_engine


@pytest.fixture
def no_engine(monkeypatch):
    """ProviderRegistry slot returns None — OSS-without-private branch."""
    from baldur.factory.registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry.compliance_engine, "safe_get", lambda *a, **kw: None
    )


# =============================================================================
# _engine
# =============================================================================


class TestEngineResolution:
    def test_returns_engine_when_slot_registered(self, wired_engine):
        assert _engine() is wired_engine

    def test_returns_none_when_slot_empty(self):
        # 599 D7 — clean OSS install leaves the slot empty; safe_get -> None.
        from baldur.factory.registry import ProviderRegistry

        slot = ProviderRegistry.compliance_engine
        with slot.snapshot():
            slot.reset()  # deterministic empty slot regardless of test order
            assert _engine() is None


# =============================================================================
# compliance_standards
# =============================================================================


class TestComplianceStandards:
    def test_503_when_engine_missing(self, no_engine):
        resp = compliance_standards(_make_ctx())
        assert resp.status_code == 503

    def test_happy_path(self, wired_engine):
        resp = compliance_standards(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["standards"] == [
            {"value": "DORA_2025", "name": "DORA_2025"},
            {"value": "SOC2", "name": "SOC2"},
        ]


# =============================================================================
# compliance_checks
# =============================================================================


class TestComplianceChecks:
    def test_503_when_engine_missing(self, no_engine):
        resp = compliance_checks(_make_ctx())
        assert resp.status_code == 503

    def test_no_standard_returns_all_checks(self, wired_engine):
        resp = compliance_checks(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["total_count"] == 2
        wired_engine.list_checks.assert_called_once_with()

    def test_unknown_standard_returns_400(self, wired_engine):
        resp = compliance_checks(_make_ctx(path_params={"standard": "MADE_UP"}))
        assert resp.status_code == 400
        assert "Unknown standard" in resp.body["error"]

    def test_valid_standard_filters_checks(self, wired_engine):
        resp = compliance_checks(_make_ctx(path_params={"standard": "SOC2"}))
        assert resp.status_code == 200
        # The handler converts the string to ComplianceStandard enum
        from baldur.models.compliance import ComplianceStandard

        wired_engine.list_checks.assert_called_once_with(ComplianceStandard.SOC2)


# =============================================================================
# compliance_run
# =============================================================================


class TestComplianceRun:
    def test_503_when_engine_missing(self, no_engine):
        resp = compliance_run(_make_ctx(method="POST"))
        assert resp.status_code == 503

    def test_run_all_without_standard(self, wired_engine):
        report = MagicMock()
        report.to_dict.return_value = {"report_id": "rp1", "summary": "ok"}
        wired_engine.run_configured.return_value = report

        resp = compliance_run(_make_ctx(method="POST", json_body={"domain": "d"}))
        assert resp.status_code == 200
        assert resp.body["report_id"] == "rp1"

        # ComplianceContext receives triggered_by="api_manual"
        ctx_arg = wired_engine.run_configured.call_args.args[0]
        assert ctx_arg.triggered_by == "api_manual"
        assert ctx_arg.domain == "d"

    def test_unknown_standard_returns_400(self, wired_engine):
        resp = compliance_run(
            _make_ctx(method="POST", path_params={"standard": "NOPE"})
        )
        assert resp.status_code == 400
        assert "Unknown standard" in resp.body["error"]

    def test_valid_standard_runs_specific(self, wired_engine):
        report = MagicMock()
        report.to_dict.return_value = {"report_id": "rp2"}
        wired_engine.run_standard.return_value = report

        resp = compliance_run(
            _make_ctx(method="POST", path_params={"standard": "GDPR"})
        )
        assert resp.status_code == 200
        from baldur.models.compliance import ComplianceStandard

        args, _kwargs = wired_engine.run_standard.call_args
        assert args[0] == ComplianceStandard.GDPR
        assert args[1].triggered_by == "api_manual"

    def test_run_standard_value_error_returns_404(self, wired_engine):
        wired_engine.run_standard.side_effect = ValueError("standard not enabled")
        resp = compliance_run(
            _make_ctx(method="POST", path_params={"standard": "HIPAA"})
        )
        assert resp.status_code == 404
        assert "not enabled" in resp.body["error"]

    def test_json_body_none_treated_as_empty(self, wired_engine):
        report = MagicMock()
        report.to_dict.return_value = {"report_id": "rpN"}
        wired_engine.run_configured.return_value = report

        resp = compliance_run(_make_ctx(method="POST", json_body=None))
        assert resp.status_code == 200
        ctx_arg = wired_engine.run_configured.call_args.args[0]
        assert ctx_arg.domain is None


# =============================================================================
# compliance_reports  &  compliance_report_detail
# -----------------------------------------------------------------------------
# Backed by ``get_continuous_audit_recorder()`` and ``recorder.query(
# action=AuditAction.COMPLIANCE_CHECK, ...)``. Feature #34 (Compliance) is
# Dormant per FEATURE_CATALOG — code-maintenance fix only, no SemVer surface
# guarantee.
# =============================================================================


@pytest.fixture
def mock_recorder():
    """Mock recorder wired into the lazy lookup used by both endpoints."""
    recorder = MagicMock(name="ContinuousAuditRecorder")
    recorder.query.return_value = []
    with patch(
        "baldur.audit.continuous_audit.get_continuous_audit_recorder",
        return_value=recorder,
    ):
        yield recorder


class TestComplianceReports:
    def test_invalid_page_returns_400(self):
        resp = compliance_reports(_make_ctx(query={"page": "abc"}))
        assert resp.status_code == 400

    def test_invalid_page_size_returns_400(self):
        resp = compliance_reports(_make_ctx(query={"page_size": "x"}))
        assert resp.status_code == 400

    def test_page_size_clamped(self, mock_recorder):
        resp = compliance_reports(_make_ctx(query={"page_size": "9999"}))
        assert resp.status_code == 200
        assert resp.body["page_size"] == 100

    def test_page_floored_to_1(self, mock_recorder):
        resp = compliance_reports(_make_ctx(query={"page": "0"}))
        assert resp.status_code == 200
        assert resp.body["page"] == 1

    def test_default_pagination_empty_result(self, mock_recorder):
        resp = compliance_reports(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["page"] == 1
        assert resp.body["page_size"] == 20
        assert resp.body["reports"] == []
        assert resp.body["total_count"] == 0

    def test_happy_path_returns_records(self, mock_recorder):
        mock_recorder.query.return_value = [
            {"details": {"standards_checked": ["SOC2"], "report_id": "r1"}},
            {"details": {"standards_checked": ["GDPR"], "report_id": "r2"}},
        ]
        resp = compliance_reports(_make_ctx())
        assert resp.status_code == 200
        assert resp.body["total_count"] == 2
        assert len(resp.body["reports"]) == 2

    def test_standard_filter_applied(self, mock_recorder):
        mock_recorder.query.return_value = [
            {"details": {"standards_checked": ["SOC2"]}},
            {"details": {"standards_checked": ["GDPR"]}},
            {"details": {"standards_checked": ["SOC2", "PCI_DSS"]}},
        ]
        resp = compliance_reports(_make_ctx(query={"standard": "SOC2"}))
        assert resp.status_code == 200
        assert resp.body["total_count"] == 2

    def test_date_range_passed_to_recorder(self, mock_recorder):
        compliance_reports(
            _make_ctx(query={"date_from": "2026-01-01", "date_to": "2026-02-01"})
        )
        _args, kwargs = mock_recorder.query.call_args
        assert kwargs["start_time"] == datetime(2026, 1, 1)
        assert kwargs["end_time"] == datetime(2026, 2, 1)

    def test_invalid_date_strings_silently_ignored(self, mock_recorder):
        # Dormant scope — bad ISO strings parse to None rather than 400.
        compliance_reports(
            _make_ctx(query={"date_from": "not-a-date", "date_to": "also-bad"})
        )
        _args, kwargs = mock_recorder.query.call_args
        assert kwargs["start_time"] is None
        assert kwargs["end_time"] is None

    def test_pagination_slices_results(self, mock_recorder):
        mock_recorder.query.return_value = [
            {"details": {"report_id": f"r{i}"}} for i in range(25)
        ]
        resp = compliance_reports(_make_ctx(query={"page": "2", "page_size": "10"}))
        assert resp.status_code == 200
        assert resp.body["total_count"] == 25
        assert len(resp.body["reports"]) == 10
        assert resp.body["reports"][0]["details"]["report_id"] == "r10"

    def test_recorder_exception_returns_degraded_response(self, mock_recorder):
        mock_recorder.query.side_effect = RuntimeError("backend exploded")
        resp = compliance_reports(_make_ctx(query={"page": "2", "page_size": "5"}))
        assert resp.status_code == 200
        assert resp.body["reports"] == []
        assert resp.body["total_count"] == 0
        assert resp.body["page"] == 2
        assert resp.body["page_size"] == 5


class TestComplianceReportDetail:
    def test_returns_404_when_no_records(self, mock_recorder):
        resp = compliance_report_detail(_make_ctx(path_params={"report_id": "r1"}))
        assert resp.status_code == 404
        assert resp.body["error"] == "Report not found"

    def test_returns_404_when_no_matching_report_id(self, mock_recorder):
        mock_recorder.query.return_value = [
            {"details": {"report_id": "other"}},
        ]
        resp = compliance_report_detail(_make_ctx(path_params={"report_id": "r1"}))
        assert resp.status_code == 404

    def test_returns_matched_records(self, mock_recorder):
        mock_recorder.query.return_value = [
            {"details": {"report_id": "r1", "overall_status": "compliant"}},
            {"details": {"report_id": "other"}},
            {"details": {"report_id": "r1", "overall_status": "compliant"}},
        ]
        resp = compliance_report_detail(_make_ctx(path_params={"report_id": "r1"}))
        assert resp.status_code == 200
        assert resp.body["report_id"] == "r1"
        assert resp.body["total_count"] == 2
        assert all(r["details"]["report_id"] == "r1" for r in resp.body["records"])

    def test_recorder_exception_returns_404(self, mock_recorder):
        mock_recorder.query.side_effect = RuntimeError("backend exploded")
        resp = compliance_report_detail(_make_ctx(path_params={"report_id": "r1"}))
        assert resp.status_code == 404
        assert resp.body["error"] == "Report not found"


# =============================================================================
# compliance_pending_evidence
# =============================================================================


class TestCompliancePendingEvidence:
    def test_503_when_engine_missing(self, no_engine):
        resp = compliance_pending_evidence(_make_ctx(path_params={"report_id": "r1"}))
        assert resp.status_code == 503

    def test_happy_path(self, wired_engine):
        wired_engine.get_pending_evidence.return_value = [
            {"check": "x"},
            {"check": "y"},
        ]
        resp = compliance_pending_evidence(_make_ctx(path_params={"report_id": "r1"}))
        assert resp.status_code == 200
        assert resp.body["report_id"] == "r1"
        assert resp.body["total_count"] == 2
        wired_engine.get_pending_evidence.assert_called_once_with("r1")


# =============================================================================
# compliance_evidence_review
# =============================================================================


class TestComplianceEvidenceReview:
    def test_503_when_engine_missing(self, no_engine):
        resp = compliance_evidence_review(
            _make_ctx(
                method="PATCH",
                path_params={"report_id": "r1", "check_id": "c1"},
            )
        )
        assert resp.status_code == 503

    def test_missing_approved_returns_400(self, wired_engine):
        resp = compliance_evidence_review(
            _make_ctx(
                method="PATCH",
                path_params={"report_id": "r1", "check_id": "c1"},
                json_body={"reviewer": "alice"},
            )
        )
        assert resp.status_code == 400
        assert "approved" in resp.body["error"]

    def test_missing_reviewer_returns_400(self, wired_engine):
        resp = compliance_evidence_review(
            _make_ctx(
                method="PATCH",
                path_params={"report_id": "r1", "check_id": "c1"},
                json_body={"approved": True},
            )
        )
        assert resp.status_code == 400
        assert "reviewer" in resp.body["error"]

    def test_json_body_none_returns_400(self, wired_engine):
        resp = compliance_evidence_review(
            _make_ctx(
                method="PATCH",
                path_params={"report_id": "r1", "check_id": "c1"},
                json_body=None,
            )
        )
        assert resp.status_code == 400

    def test_happy_path(self, wired_engine):
        result = MagicMock()
        result.to_dict.return_value = {"status": "approved"}
        wired_engine.review_evidence.return_value = result

        resp = compliance_evidence_review(
            _make_ctx(
                method="PATCH",
                path_params={"report_id": "r1", "check_id": "c1"},
                json_body={
                    "approved": True,
                    "reviewer": "alice",
                    "comment": "looks good",
                },
            )
        )
        assert resp.status_code == 200
        assert resp.body["status"] == "approved"
        wired_engine.review_evidence.assert_called_once_with(
            check_id="c1",
            report_id="r1",
            approved=True,
            reviewer="alice",
            comment="looks good",
        )

    def test_value_error_from_engine_returns_400(self, wired_engine):
        wired_engine.review_evidence.side_effect = ValueError("bad state")
        resp = compliance_evidence_review(
            _make_ctx(
                method="PATCH",
                path_params={"report_id": "r1", "check_id": "c1"},
                json_body={"approved": True, "reviewer": "alice"},
            )
        )
        assert resp.status_code == 400
        assert "bad state" in resp.body["error"]

    def test_approved_falsy_value_still_passes_validation(self, wired_engine):
        # `approved=False` is valid (rejection); only `None` is missing.
        result = MagicMock()
        result.to_dict.return_value = {"status": "rejected"}
        wired_engine.review_evidence.return_value = result

        resp = compliance_evidence_review(
            _make_ctx(
                method="PATCH",
                path_params={"report_id": "r1", "check_id": "c1"},
                json_body={"approved": False, "reviewer": "alice"},
            )
        )
        assert resp.status_code == 200
        wired_engine.review_evidence.assert_called_once()
        assert wired_engine.review_evidence.call_args.kwargs["approved"] is False
