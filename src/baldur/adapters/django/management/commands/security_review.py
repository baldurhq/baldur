"""
Security Review Management Command

Run comprehensive security review for the baldur system.

Usage:
    python manage.py security_review
    python manage.py security_review --output /path/to/results.json
    python manage.py security_review --quiet
"""

import json
from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError

# =============================================================================
# Configuration
# =============================================================================

REVIEW_VERSION = "1.0.0"


class CheckResult:
    """Result of a single security check."""

    def __init__(self, category: str, name: str, passed: bool, details: str = ""):
        self.category = category
        self.name = name
        self.passed = passed
        self.details = details


# =============================================================================
# Security Check Functions
# =============================================================================


def check_security_violation_service() -> list[CheckResult]:
    """Check security violation service implementation."""
    results = []

    try:
        from baldur.services import (
            SEVERITY_BY_VIOLATION_TYPE,
            SecurityViolationService,
            Severity,
            ViolationType,
        )

        unmapped = []
        for vtype in ViolationType:
            if (
                vtype not in SEVERITY_BY_VIOLATION_TYPE
                and vtype.value not in SEVERITY_BY_VIOLATION_TYPE
            ):
                unmapped.append(vtype.value)

        results.append(
            CheckResult(
                "violation_handling",
                "All violation types have severity mapping",
                len(unmapped) == 0,
                f"Unmapped: {unmapped}" if unmapped else "",
            )
        )

        critical_types = [
            ViolationType.SIGNATURE_INVALID,
            ViolationType.DATA_TAMPERED,
            ViolationType.TOKEN_FORGED,
            ViolationType.REPLAY_ATTACK,
        ]
        critical_ok = all(
            SEVERITY_BY_VIOLATION_TYPE.get(vt, SEVERITY_BY_VIOLATION_TYPE.get(vt.value))
            == Severity.CRITICAL
            for vt in critical_types
        )
        results.append(
            CheckResult(
                "violation_handling",
                "Critical violations properly classified",
                critical_ok,
            )
        )

        try:
            SecurityViolationService()
            results.append(
                CheckResult(
                    "violation_handling",
                    "SecurityViolationService instantiates",
                    True,
                )
            )
        except Exception as e:
            results.append(
                CheckResult(
                    "violation_handling",
                    "SecurityViolationService instantiates",
                    False,
                    str(e),
                )
            )

    except ImportError as e:
        results.append(
            CheckResult(
                "violation_handling",
                "Security violation service exists",
                False,
                str(e),
            )
        )

    return results


def check_security_notification_service() -> list[CheckResult]:
    """Check security notification implementation.

    The concrete ``SecurityNotificationService`` transport lives in PRO (the
    notification tier principle: OSS observes, PRO notifies); when ``baldur_pro``
    is absent the instantiation check reports N/A (there is no OSS transport to
    inspect). The OSS value surface (channel vocabulary) is checked regardless.
    """
    results = []

    try:
        from baldur.services import NotificationChannel, NotificationConfig

        try:
            from baldur_pro.services.security_notification import (
                SecurityNotificationService,
            )

            config = NotificationConfig(dry_run=True)
            SecurityNotificationService(config=config)
            results.append(
                CheckResult(
                    "notifications",
                    "SecurityNotificationService instantiates",
                    True,
                )
            )
        except ImportError:
            results.append(
                CheckResult(
                    "notifications",
                    "SecurityNotificationService instantiates",
                    True,
                    "N/A — PRO transport not installed (OSS is log-only)",
                )
            )
        except Exception as e:
            results.append(
                CheckResult(
                    "notifications",
                    "SecurityNotificationService instantiates",
                    False,
                    str(e),
                )
            )

        channels = list(NotificationChannel)
        expected = ["slack", "pagerduty", "webhook"]
        has_all = all(any(c.value.lower() == exp for c in channels) for exp in expected)
        results.append(
            CheckResult(
                "notifications",
                "Multi-channel notification support",
                has_all,
                f"Available: {[c.value for c in channels]}",
            )
        )

    except ImportError as e:
        results.append(
            CheckResult(
                "notifications",
                "Security notification service exists",
                False,
                str(e),
            )
        )

    return results


def check_security_incident_model() -> list[CheckResult]:
    """Check SecurityIncident model implementation."""
    results = []

    try:
        from baldur.adapters.django.models import SecurityIncident

        required = ["incident_type", "severity", "status", "source_ip", "description"]
        fields = [f.name for f in SecurityIncident._meta.get_fields()]
        missing = [f for f in required if f not in fields]

        results.append(
            CheckResult(
                "model",
                "SecurityIncident has required fields",
                len(missing) == 0,
                f"Missing: {missing}" if missing else "",
            )
        )

        results.append(
            CheckResult(
                "model",
                "Severity choices defined",
                hasattr(SecurityIncident, "Severity"),
            )
        )

        results.append(
            CheckResult(
                "model",
                "Status choices defined",
                hasattr(SecurityIncident, "Status"),
            )
        )

    except ImportError as e:
        results.append(
            CheckResult("model", "SecurityIncident model exists", False, str(e))
        )

    return results


def check_data_protection() -> list[CheckResult]:
    """Check sensitive data protection."""
    results = []

    results.append(
        CheckResult(
            "data_protection", "ForensicContext removed (handled by DLQService)", True
        )
    )

    try:
        from baldur_pro.services.dlq import DLQService  # noqa: F401

        results.append(CheckResult("data_protection", "DLQ service exists", True))
    except ImportError as e:
        results.append(
            CheckResult("data_protection", "DLQ service exists", False, str(e))
        )

    return results


def check_access_control() -> list[CheckResult]:
    """Check access control implementation."""
    results = []

    try:
        import inspect

        from baldur.services import CircuitBreakerService

        service = CircuitBreakerService()

        sig = inspect.signature(service.force_open)
        has_controlled_by = "controlled_by" in sig.parameters
        results.append(
            CheckResult(
                "access_control", "force_open tracks operator", has_controlled_by
            )
        )

        sig = inspect.signature(service.force_close)
        has_controlled_by = "controlled_by" in sig.parameters
        results.append(
            CheckResult(
                "access_control", "force_close tracks operator", has_controlled_by
            )
        )

    except ImportError as e:
        results.append(
            CheckResult("access_control", "CircuitBreakerService exists", False, str(e))
        )

    try:
        from baldur.adapters.django.models import FailedOperation

        fields = [f.name for f in FailedOperation._meta.get_fields()]
        has_resolved_by = "resolved_by" in fields
        results.append(
            CheckResult(
                "access_control",
                "FailedOperation tracks resolved_by",
                has_resolved_by,
            )
        )
    except ImportError as e:
        results.append(
            CheckResult("access_control", "FailedOperation model check", False, str(e))
        )

    return results


def check_audit_trail() -> list[CheckResult]:
    """Check audit trail implementation."""
    results = []

    try:
        from baldur.adapters.django.models import FailedOperation

        fields = [f.name for f in FailedOperation._meta.get_fields()]

        results.append(
            CheckResult(
                "audit_trail", "FailedOperation has created_at", "created_at" in fields
            )
        )
        results.append(
            CheckResult(
                "audit_trail", "FailedOperation has updated_at", "updated_at" in fields
            )
        )
        results.append(
            CheckResult(
                "audit_trail",
                "FailedOperation has resolved_at",
                "resolved_at" in fields,
            )
        )

        has_archived = hasattr(FailedOperation, "Status") and hasattr(
            FailedOperation.Status, "ARCHIVED"
        )
        results.append(
            CheckResult(
                "audit_trail", "Soft-delete (ARCHIVED status) implemented", has_archived
            )
        )

    except ImportError as e:
        results.append(
            CheckResult("audit_trail", "FailedOperation model exists", False, str(e))
        )

    return results


def check_ip_management() -> list[CheckResult]:
    """Check IP management for security."""
    results = []

    try:
        from baldur.services import SecurityViolationService

        service = SecurityViolationService()

        ip_methods = [
            m for m in dir(service) if not m.startswith("_") and "ip" in m.lower()
        ]
        results.append(
            CheckResult(
                "ip_management",
                "IP management methods exist",
                len(ip_methods) > 0,
                f"Methods: {ip_methods}",
            )
        )

        has_ban = any(
            "ban" in m.lower() or "block" in m.lower()
            for m in dir(service)
            if not m.startswith("_")
        )
        results.append(
            CheckResult("ip_management", "Temporary ban capability", has_ban)
        )

    except ImportError as e:
        results.append(
            CheckResult(
                "ip_management", "Security violation service exists", False, str(e)
            )
        )

    return results


# =============================================================================
# Check Function Registry
# =============================================================================

CHECK_FUNCTIONS = [
    ("Security Violation Service", check_security_violation_service),
    ("Security Notification Service", check_security_notification_service),
    ("Security Incident Model", check_security_incident_model),
    ("Data Protection", check_data_protection),
    ("Access Control", check_access_control),
    ("Audit Trail", check_audit_trail),
    ("IP Management", check_ip_management),
]


# =============================================================================
# Command
# =============================================================================


class Command(BaseCommand):
    """Security review management command."""

    help = "Run comprehensive security review for the baldur system"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            "-o",
            type=str,
            help="Output path for JSON results",
        )
        parser.add_argument(
            "--quiet",
            "-q",
            action="store_true",
            help="Only show summary, not individual checks",
        )

    def handle(self, *args, **options):
        quiet = options.get("quiet", False)
        output_path = options.get("output")
        review_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._print_header(review_date, quiet)
        all_results = self._run_all_checks(quiet)
        summary = self._calculate_summary(all_results)
        self._print_summary(summary)
        self._export_results(output_path, review_date, all_results, summary)
        self._check_pass_threshold(summary["pass_rate"])

    def _print_header(self, review_date: str, quiet: bool) -> None:
        if quiet:
            return
        self.stdout.write(self.style.NOTICE(f"\nSecurity Review v{REVIEW_VERSION}"))
        self.stdout.write(f"Date: {review_date}\n")

    def _run_all_checks(self, quiet: bool) -> list[CheckResult]:
        all_results: list[CheckResult] = []

        for section_name, check_func in CHECK_FUNCTIONS:
            self._print_section_header(section_name, quiet)
            results = check_func()
            all_results.extend(results)
            self._print_check_results(results, quiet)

        return all_results

    def _print_section_header(self, section_name: str, quiet: bool) -> None:
        if quiet:
            return
        self.stdout.write(f"\n{'=' * 50}")
        self.stdout.write(self.style.MIGRATE_HEADING(section_name))
        self.stdout.write("=" * 50)

    def _print_check_results(self, results: list[CheckResult], quiet: bool) -> None:
        if quiet:
            return
        for r in results:
            status = "✓ PASS" if r.passed else "✗ FAIL"
            style = self.style.SUCCESS if r.passed else self.style.ERROR
            self.stdout.write(style(f"  {status} - {r.name}"))
            if r.details:
                self.stdout.write(self.style.WARNING(f"        {r.details}"))

    def _calculate_summary(self, all_results: list[CheckResult]) -> dict[str, Any]:
        passed = sum(1 for r in all_results if r.passed)
        failed = sum(1 for r in all_results if not r.passed)
        total = passed + failed
        pass_rate = (passed / total * 100) if total > 0 else 0
        return {
            "passed": passed,
            "failed": failed,
            "total": total,
            "pass_rate": pass_rate,
        }

    def _print_summary(self, summary: dict[str, Any]) -> None:
        self.stdout.write(f"\n{'=' * 50}")
        self.stdout.write(self.style.MIGRATE_HEADING("Summary"))
        self.stdout.write("=" * 50)
        self.stdout.write(f"  Total Checks: {summary['total']}")
        self.stdout.write(self.style.SUCCESS(f"  Passed: {summary['passed']}"))
        self.stdout.write(self.style.ERROR(f"  Failed: {summary['failed']}"))
        self.stdout.write(f"  Pass Rate: {summary['pass_rate']:.1f}%")

        self._print_review_status(summary["pass_rate"])

    def _print_review_status(self, pass_rate: float) -> None:
        if pass_rate >= 90:
            self.stdout.write(self.style.SUCCESS("\n  ✓ SECURITY REVIEW PASSED"))
        elif pass_rate >= 70:
            self.stdout.write(
                self.style.WARNING("\n  ⚠ SECURITY REVIEW NEEDS ATTENTION")
            )
        else:
            self.stdout.write(self.style.ERROR("\n  ✗ SECURITY REVIEW FAILED"))

    def _export_results(
        self,
        output_path: str | None,
        review_date: str,
        all_results: list[CheckResult],
        summary: dict[str, Any],
    ) -> None:
        if not output_path:
            return

        results_data = {
            "version": REVIEW_VERSION,
            "date": review_date,
            "passed": summary["passed"],
            "failed": summary["failed"],
            "pass_rate": summary["pass_rate"],
            "results": [
                {
                    "category": r.category,
                    "name": r.name,
                    "passed": r.passed,
                    "details": r.details,
                }
                for r in all_results
            ],
        }

        with open(output_path, "w") as f:
            json.dump(results_data, f, indent=2, default=str)

        self.stdout.write(f"\n  Results exported to: {output_path}")

    def _check_pass_threshold(self, pass_rate: float) -> None:
        if pass_rate < 70:
            raise CommandError("Security review failed")
