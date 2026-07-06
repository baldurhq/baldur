"""Stage DNA URL patterns (FinOps + Self-Learning + Rollback + Blast Radius).

Conditionally loaded — DNA view modules live in optional packages and may be
absent in OSS-only installs. Imports are wrapped in try/except so a missing
DNA package degrades to an empty pattern list instead of breaking URL loading.
"""

from __future__ import annotations

from django.urls import path

try:
    from baldur.api.django.views.blast_radius import (
        BlastRadiusAssessmentView,
        BlastRadiusDependencyView,
        BlastRadiusDNAPolicyView,
        BlastRadiusGraphView,
        BlastRadiusIsolationView,
    )
    from baldur.api.django.views.finops import (
        FinOpsAlertsView,
        FinOpsBudgetView,
        FinOpsCostView,
        FinOpsReportView,
    )
    from baldur.api.django.views.learning import (
        LearningInsightsView,
        LearningMetricView,
        LearningPatternView,
        LearningSessionView,
        LearningSuggestionView,
    )

    urlpatterns = [
        # FinOps DNA — cost management
        path(
            "dna/finops/budget/",
            FinOpsBudgetView.as_view(),
            name="dna-finops-budget-list",
        ),
        path(
            "dna/finops/budget/<str:service_name>/",
            FinOpsBudgetView.as_view(),
            name="dna-finops-budget",
        ),
        path("dna/finops/cost/", FinOpsCostView.as_view(), name="dna-finops-cost"),
        path(
            "dna/finops/report/", FinOpsReportView.as_view(), name="dna-finops-report"
        ),
        path(
            "dna/finops/alerts/", FinOpsAlertsView.as_view(), name="dna-finops-alerts"
        ),
        path(
            "dna/finops/alerts/<int:alert_index>/acknowledge/",
            FinOpsAlertsView.as_view(),
            name="dna-finops-alert-ack",
        ),
        # Self-Learning DNA — pattern learning + suggestions
        path(
            "dna/learning/session/<str:action>/",
            LearningSessionView.as_view(),
            name="dna-learning-session",
        ),
        path(
            "dna/learning/patterns/",
            LearningPatternView.as_view(),
            name="dna-learning-patterns",
        ),
        path(
            "dna/learning/suggestions/",
            LearningSuggestionView.as_view(),
            name="dna-learning-suggestions",
        ),
        path(
            "dna/learning/suggestions/<str:suggestion_id>/apply/",
            LearningSuggestionView.as_view(),
            name="dna-learning-suggestion-apply",
        ),
        path(
            "dna/learning/metrics/",
            LearningMetricView.as_view(),
            name="dna-learning-metrics",
        ),
        path(
            "dna/learning/insights/",
            LearningInsightsView.as_view(),
            name="dna-learning-insights",
        ),
        # Blast Radius DNA — failure impact scope
        path(
            "dna/blast-radius/policy/<str:service_name>/",
            BlastRadiusDNAPolicyView.as_view(),
            name="dna-blast-radius-policy",
        ),
        path(
            "dna/blast-radius/dependency/",
            BlastRadiusDependencyView.as_view(),
            name="dna-blast-radius-dependency-add",
        ),
        path(
            "dna/blast-radius/dependency/<str:service_name>/",
            BlastRadiusDependencyView.as_view(),
            name="dna-blast-radius-dependency",
        ),
        path(
            "dna/blast-radius/assessment/",
            BlastRadiusAssessmentView.as_view(),
            name="dna-blast-radius-assessment",
        ),
        path(
            "dna/blast-radius/isolation/",
            BlastRadiusIsolationView.as_view(),
            name="dna-blast-radius-isolation-list",
        ),
        path(
            "dna/blast-radius/isolation/<str:service_name>/",
            BlastRadiusIsolationView.as_view(),
            name="dna-blast-radius-isolation",
        ),
        path(
            "dna/blast-radius/graph/",
            BlastRadiusGraphView.as_view(),
            name="dna-blast-radius-graph",
        ),
    ]
except ImportError:
    # DNA service not installed — skip
    urlpatterns = []
