"""Unit tests for security_review_run handler (api/handlers/security_review.py).

Verification techniques:
- Behavior: status codes, pass/fail rate calculation, response structure
- Dependency interaction: CHECK_FUNCTIONS call delegation
- Edge cases: all-pass, all-fail, empty checks, check function exceptions
- Side effects: JSON export on output query param
"""

from __future__ import annotations

from unittest.mock import patch

from baldur.interfaces.web_framework import (
    HttpMethod,
    RequestContext,
)


def _make_ctx(query=None):
    """Create a RequestContext for handler testing."""
    return RequestContext(
        method=HttpMethod.GET,
        path="/security-review",
        query_params=query or {},
    )


class _FakeCheckResult:
    """Minimal stub matching CheckResult interface."""

    def __init__(self, category, name, passed, details=""):
        self.category = category
        self.name = name
        self.passed = passed
        self.details = details


# =============================================================================
# Contract — response structure and version
# =============================================================================


class TestSecurityReviewResponseContract:
    """Security review response structure contract."""

    def test_response_version_is_1_0_0(self):
        """Response includes version '1.0.0'."""
        from baldur.api.handlers.security_review import REVIEW_VERSION

        assert REVIEW_VERSION == "1.0.0"

    def test_response_contains_required_keys(self):
        """Response body has all required top-level keys."""
        checks = [_FakeCheckResult("cat", "check1", True)]
        with (
            patch(
                "baldur.api.handlers.security_review.CHECK_FUNCTIONS",
                [("Section", lambda: checks)],
                create=True,
            ),
            patch(
                "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
                [("Section", lambda: checks)],
            ),
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        required_keys = {
            "version",
            "timestamp",
            "passed",
            "failed",
            "total",
            "pass_rate",
            "status",
            "results",
        }
        assert required_keys.issubset(set(resp.body.keys()))

    def test_status_code_200_when_pass_rate_above_70(self):
        """Pass rate >= 70% returns 200."""
        checks = [_FakeCheckResult("cat", f"check{i}", True) for i in range(8)] + [
            _FakeCheckResult("cat", f"fail{i}", False) for i in range(2)
        ]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Section", lambda: checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.status_code == 200

    def test_status_code_422_when_pass_rate_below_70(self):
        """Pass rate < 70% returns 422."""
        checks = [
            _FakeCheckResult("cat", "pass1", True),
            _FakeCheckResult("cat", "fail1", False),
            _FakeCheckResult("cat", "fail2", False),
            _FakeCheckResult("cat", "fail3", False),
            _FakeCheckResult("cat", "fail4", False),
        ]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Section", lambda: checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.status_code == 422


# =============================================================================
# Behavior — pass rate and status calculation
# =============================================================================


class TestSecurityReviewPassRateBehavior:
    """Pass rate and status string calculation."""

    def test_all_checks_pass_yields_100_percent(self):
        """All checks passing yields pass_rate=100.0 and status=PASSED."""
        checks = [_FakeCheckResult("cat", f"ok{i}", True) for i in range(5)]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Section", lambda: checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.body["pass_rate"] == 100.0
        assert resp.body["status"] == "PASSED"
        assert resp.body["passed"] == 5
        assert resp.body["failed"] == 0

    def test_all_checks_fail_yields_0_percent(self):
        """All checks failing yields pass_rate=0 and status=FAILED."""
        checks = [_FakeCheckResult("cat", f"fail{i}", False) for i in range(3)]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Section", lambda: checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.body["pass_rate"] == 0.0
        assert resp.body["status"] == "FAILED"
        assert resp.body["failed"] == 3

    def test_needs_attention_when_between_70_and_90(self):
        """Pass rate 70-89% yields NEEDS_ATTENTION status."""
        checks = [_FakeCheckResult("cat", f"ok{i}", True) for i in range(8)] + [
            _FakeCheckResult("cat", f"fail{i}", False) for i in range(2)
        ]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Section", lambda: checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.body["pass_rate"] == 80.0
        assert resp.body["status"] == "NEEDS_ATTENTION"

    def test_passed_at_exactly_90_percent(self):
        """Pass rate exactly 90% yields PASSED status."""
        checks = [_FakeCheckResult("cat", f"ok{i}", True) for i in range(9)] + [
            _FakeCheckResult("cat", "fail0", False)
        ]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Section", lambda: checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.body["pass_rate"] == 90.0
        assert resp.body["status"] == "PASSED"

    def test_total_equals_passed_plus_failed(self):
        """total field always equals passed + failed."""
        checks = [
            _FakeCheckResult("cat", "ok", True),
            _FakeCheckResult("cat", "fail", False),
        ]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Section", lambda: checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.body["total"] == resp.body["passed"] + resp.body["failed"]


# =============================================================================
# Behavior — check function exception handling
# =============================================================================


class TestSecurityReviewExceptionHandlingBehavior:
    """Exception in a check function produces error result instead of crash."""

    def test_check_function_exception_captured_as_error_result(self):
        """Exception in check_func yields error entry with passed=False."""

        def failing_check():
            raise RuntimeError("db connection failed")

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("FailingSection", failing_check)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.body["failed"] == 1
        error_result = resp.body["results"][0]
        assert error_result["section"] == "FailingSection"
        assert error_result["category"] == "error"
        assert error_result["passed"] is False
        assert "db connection failed" in error_result["details"]

    def test_exception_does_not_prevent_other_sections(self):
        """One section's failure doesn't block subsequent sections."""

        def failing():
            raise ValueError("boom")

        ok_checks = [_FakeCheckResult("cat", "ok1", True)]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Bad", failing), ("Good", lambda: ok_checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.body["total"] == 2
        assert resp.body["passed"] == 1
        assert resp.body["failed"] == 1


# =============================================================================
# Behavior — result item structure
# =============================================================================


class TestSecurityReviewResultStructureBehavior:
    """Each result item has the expected fields from CheckResult."""

    def test_result_item_has_required_fields(self):
        """Each result entry has section, category, name, passed, details."""
        checks = [_FakeCheckResult("access_control", "api_auth", True, "All OK")]

        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [("Security", lambda: checks)],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        item = resp.body["results"][0]
        assert item["section"] == "Security"
        assert item["category"] == "access_control"
        assert item["name"] == "api_auth"
        assert item["passed"] is True
        assert item["details"] == "All OK"


# =============================================================================
# Behavior — JSON export
# =============================================================================


class TestSecurityReviewExportBehavior:
    """Output query param triggers JSON file export."""

    def test_export_writes_json_when_output_specified(self):
        """output query param triggers _export_json call."""
        checks = [_FakeCheckResult("cat", "ok", True)]

        with (
            patch(
                "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
                [("Section", lambda: checks)],
            ),
            patch("baldur.api.handlers.security_review._export_json") as mock_export,
        ):
            from baldur.api.handlers.security_review import security_review_run

            ctx = _make_ctx(query={"output": "/tmp/report.json"})
            security_review_run(ctx)

        mock_export.assert_called_once()
        args = mock_export.call_args[0]
        assert args[0] == "/tmp/report.json"

    def test_no_export_without_output_param(self):
        """Without output query param, no export occurs."""
        checks = [_FakeCheckResult("cat", "ok", True)]

        with (
            patch(
                "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
                [("Section", lambda: checks)],
            ),
            patch("baldur.api.handlers.security_review._export_json") as mock_export,
        ):
            from baldur.api.handlers.security_review import security_review_run

            security_review_run(_make_ctx())

        mock_export.assert_not_called()


# =============================================================================
# Behavior — empty checks
# =============================================================================


class TestSecurityReviewEmptyChecksBehavior:
    """Handler gracefully handles zero CHECK_FUNCTIONS."""

    def test_empty_check_functions_returns_zero_total(self):
        """No check functions yields total=0 with 200 status."""
        with patch(
            "baldur.adapters.django.management.commands.security_review.CHECK_FUNCTIONS",
            [],
        ):
            from baldur.api.handlers.security_review import security_review_run

            resp = security_review_run(_make_ctx())

        assert resp.body["total"] == 0
        assert resp.body["pass_rate"] == 0
        assert resp.status_code == 422
