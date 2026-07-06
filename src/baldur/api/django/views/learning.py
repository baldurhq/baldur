"""
Self-Learning DNA API Views.

Thin HandlerAPIView wrappers. Business logic extracted to
api/handlers/learning.py (Phase 2b -- 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.learning import (
    learning_insights,
    learning_metric_record,
    learning_pattern_create,
    learning_pattern_list,
    learning_session_action,
    learning_suggestion_apply,
    learning_suggestion_list,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "LearningSessionView",
    "LearningPatternView",
    "LearningSuggestionView",
    "LearningMetricView",
    "LearningInsightsView",
]


class LearningSessionView(HandlerAPIView):
    """Learning session start/end endpoint."""

    permission_level = PermissionLevel.OPERATOR
    handler = learning_session_action


class LearningPatternView(HandlerAPIView):
    """Learning pattern query and creation endpoint."""

    handler_map = {
        HttpMethod.GET: learning_pattern_list,
        HttpMethod.POST: learning_pattern_create,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.OPERATOR,
    }


class LearningSuggestionView(HandlerAPIView):
    """Optimization suggestion query and apply endpoint."""

    handler_map = {
        HttpMethod.GET: learning_suggestion_list,
        HttpMethod.POST: learning_suggestion_apply,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.POST: PermissionLevel.OPERATOR,
    }


class LearningMetricView(HandlerAPIView):
    """Performance metric recording endpoint."""

    permission_level = PermissionLevel.OPERATOR
    handler = learning_metric_record


class LearningInsightsView(HandlerAPIView):
    """Cross-stage insights endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = learning_insights
