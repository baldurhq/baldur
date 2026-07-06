"""
Group Postmortem recommendations English message contract tests (doc 361 §2.9).

Tests _generate_group_recommendations() returns English messages
for all cascading pattern branches and conditional recommendations.

Verification techniques:
- Contract: hardcoded English phrases from doc 361 §2.9
- Behavior: conditional branches (incident_count, affected_services count)
"""

import re

from baldur.adapters.celery.tasks.postmortem import (
    _generate_group_recommendations,
)


class TestGroupRecommendationsEnglishContract:
    """_generate_group_recommendations() English message contract."""

    def test_simultaneous_pattern_returns_english(self):
        """Simultaneous pattern produces English recommendations."""
        result = _generate_group_recommendations(
            cascading_pattern="simultaneous",
            affected_services=["svc-a"],
            incident_count=2,
        )

        assert any("Simultaneous failures" in r for r in result)
        assert any("common cause analysis required" in r for r in result)
        assert any("infrastructure/network level" in r for r in result)

    def test_cascading_pattern_returns_english(self):
        """Cascading pattern produces English recommendations."""
        result = _generate_group_recommendations(
            cascading_pattern="cascading",
            affected_services=["svc-a"],
            incident_count=2,
        )

        assert any("Cascading failure pattern detected" in r for r in result)
        assert any("dependency chain analysis" in r for r in result)
        assert any("Circuit Breaker configuration review" in r for r in result)
        assert any("Fast Fail optimization" in r for r in result)

    def test_independent_pattern_returns_english(self):
        """Independent (default) pattern produces English recommendations."""
        result = _generate_group_recommendations(
            cascading_pattern="independent",
            affected_services=["svc-a"],
            incident_count=2,
        )

        assert any("Independent failures" in r for r in result)
        assert any("analyze each individually" in r for r in result)

    def test_high_incident_count_returns_english(self):
        """incident_count >= 5 adds English system-wide review recommendation."""
        result = _generate_group_recommendations(
            cascading_pattern="simultaneous",
            affected_services=["svc-a"],
            incident_count=7,
        )

        assert any("Multiple services affected (7)" in r for r in result)
        assert any("system-wide stability review" in r for r in result)

    def test_many_affected_services_returns_english(self):
        """More than 3 affected services adds English service list."""
        services = ["svc-a", "svc-b", "svc-c", "svc-d"]
        result = _generate_group_recommendations(
            cascading_pattern="simultaneous",
            affected_services=services,
            incident_count=2,
        )

        assert any("Affected services:" in r for r in result)

    def test_always_includes_process_improvement_english(self):
        """Every result includes English process improvement recommendation."""
        result = _generate_group_recommendations(
            cascading_pattern="simultaneous",
            affected_services=["svc-a"],
            incident_count=1,
        )

        assert "Review incident response process improvements" in result

    def test_no_korean_in_any_recommendation(self):
        """No Korean (Hangul) characters in any recommendation string."""
        hangul = re.compile(r"[가-힣]")

        for pattern in ["simultaneous", "cascading", "independent"]:
            result = _generate_group_recommendations(
                cascading_pattern=pattern,
                affected_services=["svc-a", "svc-b", "svc-c", "svc-d"],
                incident_count=10,
            )
            for rec in result:
                assert not hangul.search(rec), (
                    f"Korean found in {pattern} pattern: {rec}"
                )


class TestGroupRecommendationsBehavior:
    """_generate_group_recommendations() conditional behavior."""

    def test_low_incident_count_omits_system_wide_review(self):
        """incident_count < 5 does not include system-wide review."""
        result = _generate_group_recommendations(
            cascading_pattern="simultaneous",
            affected_services=["svc-a"],
            incident_count=4,
        )

        assert not any("Multiple services affected" in r for r in result)

    def test_few_affected_services_omits_service_list(self):
        """3 or fewer affected services does not include service list."""
        result = _generate_group_recommendations(
            cascading_pattern="simultaneous",
            affected_services=["svc-a", "svc-b", "svc-c"],
            incident_count=2,
        )

        assert not any("Affected services:" in r for r in result)

    def test_affected_services_list_truncated_to_five(self):
        """Service list shows at most 5 services."""
        services = [f"svc-{i}" for i in range(10)]
        result = _generate_group_recommendations(
            cascading_pattern="simultaneous",
            affected_services=services,
            incident_count=2,
        )

        svc_line = next(r for r in result if "Affected services:" in r)
        # Only first 5 should appear
        assert "svc-0" in svc_line
        assert "svc-4" in svc_line
        assert "svc-5" not in svc_line
