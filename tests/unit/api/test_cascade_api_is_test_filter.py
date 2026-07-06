"""
CascadeEvent API is_test filter and response field unit tests.

Test targets:
- cascade_event_list handler response includes is_test field
- ?is_test=true filter returns only test events
- ?is_test=false filter returns only production events
- Omitting is_test returns all events
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.cascade_event import (
    CascadeEffect,
    CascadeEvent,
    CascadeTrigger,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def production_cascade_event():
    """Production CascadeEvent (is_test=False)."""
    return CascadeEvent(
        id="cascade-prod-001",
        trigger=CascadeTrigger(
            trigger_type="EMERGENCY_LEVEL_CHANGED",
            event_id="evt-prod-001",
            details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
            triggered_by="system",
        ),
        effects=[
            CascadeEffect(
                action_type="governance_strict",
                event_id="effect-prod-001",
                success=True,
                caused_by="evt-prod-001",
                details={},
                executed_at=datetime.now(UTC).isoformat(),
            )
        ],
        namespace="seoul",
        timestamp=datetime.now(UTC).isoformat(),
        is_test=False,
    )


@pytest.fixture
def test_cascade_event():
    """Test CascadeEvent (is_test=True)."""
    return CascadeEvent(
        id="cascade-test-001",
        trigger=CascadeTrigger(
            trigger_type="EMERGENCY_LEVEL_CHANGED",
            event_id="evt-test-001",
            details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
            triggered_by="x-test-mode",
        ),
        effects=[
            CascadeEffect(
                action_type="governance_strict",
                event_id="effect-test-001",
                success=True,
                caused_by="evt-test-001",
                details={},
                executed_at=datetime.now(UTC).isoformat(),
            )
        ],
        namespace="seoul",
        timestamp=datetime.now(UTC).isoformat(),
        is_test=True,
    )


def _make_ctx(query_params: dict) -> RequestContext:
    """Create a RequestContext for cascade_event_list handler."""
    return RequestContext(
        method=HttpMethod.GET,
        path="/cascade/events/",
        query_params=query_params,
    )


# =============================================================================
# cascade_event_list is_test response field tests
# =============================================================================


class TestCascadeEventListViewIsTestResponseField:
    """cascade_event_list handler response includes is_test field."""

    def test_response_includes_is_test_field_for_production_event(
        self, production_cascade_event
    ):
        """Production event response includes is_test=false."""
        from baldur.api.handlers.cascade import cascade_event_list

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [production_cascade_event]
        mock_auditor.get_event_count.return_value = 1

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx({"namespace": "seoul"})
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            assert "is_test" in response.body["events"][0]
            assert response.body["events"][0]["is_test"] is False

    def test_response_includes_is_test_field_for_test_event(self, test_cascade_event):
        """Test event response includes is_test=true."""
        from baldur.api.handlers.cascade import cascade_event_list

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [test_cascade_event]
        mock_auditor.get_event_count.return_value = 1

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx({"namespace": "seoul"})
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            assert "is_test" in response.body["events"][0]
            assert response.body["events"][0]["is_test"] is True


# =============================================================================
# cascade_event_list is_test filter tests
# =============================================================================


class TestCascadeEventListViewIsTestFilter:
    """cascade_event_list is_test query parameter filter tests."""

    def test_filter_is_test_false_returns_production_only(
        self, production_cascade_event, test_cascade_event
    ):
        """?is_test=false returns only production events."""
        from baldur.api.handlers.cascade import cascade_event_list

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [
            production_cascade_event,
            test_cascade_event,
        ]
        mock_auditor.get_event_count.return_value = 2

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx({"namespace": "seoul", "is_test": "false"})
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            assert len(response.body["events"]) == 1
            assert response.body["events"][0]["id"] == "cascade-prod-001"
            assert response.body["events"][0]["is_test"] is False

    def test_filter_is_test_true_returns_test_only(
        self, production_cascade_event, test_cascade_event
    ):
        """?is_test=true returns only test events."""
        from baldur.api.handlers.cascade import cascade_event_list

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [
            production_cascade_event,
            test_cascade_event,
        ]
        mock_auditor.get_event_count.return_value = 2

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx({"namespace": "seoul", "is_test": "true"})
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            assert len(response.body["events"]) == 1
            assert response.body["events"][0]["id"] == "cascade-test-001"
            assert response.body["events"][0]["is_test"] is True

    def test_no_is_test_filter_returns_all_events(
        self, production_cascade_event, test_cascade_event
    ):
        """Omitting is_test returns all events."""
        from baldur.api.handlers.cascade import cascade_event_list

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [
            production_cascade_event,
            test_cascade_event,
        ]
        mock_auditor.get_event_count.return_value = 2

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx({"namespace": "seoul"})
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            assert len(response.body["events"]) == 2

    def test_is_test_filter_case_insensitive(
        self, production_cascade_event, test_cascade_event
    ):
        """is_test filter is case insensitive."""
        from baldur.api.handlers.cascade import cascade_event_list

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [
            production_cascade_event,
            test_cascade_event,
        ]
        mock_auditor.get_event_count.return_value = 2

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx({"namespace": "seoul", "is_test": "TRUE"})
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            assert len(response.body["events"]) == 1
            assert response.body["events"][0]["is_test"] is True

    def test_is_test_filter_with_trigger_type_filter(
        self, production_cascade_event, test_cascade_event
    ):
        """Combining is_test and trigger_type filters."""
        from baldur.api.handlers.cascade import cascade_event_list

        # Add a test event with different trigger_type
        manual_test_event = CascadeEvent(
            id="cascade-manual-test",
            trigger=CascadeTrigger(
                trigger_type="MANUAL_ACTIVATION",
                event_id="evt-manual",
                details={},
            ),
            effects=[],
            namespace="seoul",
            timestamp=datetime.now(UTC).isoformat(),
            is_test=True,
        )

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [
            production_cascade_event,
            test_cascade_event,
            manual_test_event,
        ]
        mock_auditor.get_event_count.return_value = 3

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx(
                {
                    "namespace": "seoul",
                    "is_test": "true",
                    "trigger_type": "EMERGENCY_LEVEL_CHANGED",
                }
            )
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            # is_test=true AND trigger_type=EMERGENCY_LEVEL_CHANGED
            assert len(response.body["events"]) == 1
            assert response.body["events"][0]["id"] == "cascade-test-001"


# =============================================================================
# Edge Cases
# =============================================================================


class TestCascadeEventListViewIsTestEdgeCases:
    """is_test filter edge case tests."""

    def test_empty_result_when_no_matching_events(self, production_cascade_event):
        """Empty list when no events match filter."""
        from baldur.api.handlers.cascade import cascade_event_list

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [production_cascade_event]
        mock_auditor.get_event_count.return_value = 1

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx({"namespace": "seoul", "is_test": "true"})
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            assert len(response.body["events"]) == 0

    def test_invalid_is_test_value_treated_as_false(self, test_cascade_event):
        """Invalid is_test value treated as false."""
        from baldur.api.handlers.cascade import cascade_event_list

        mock_auditor = MagicMock()
        mock_auditor.get_recent_events.return_value = [test_cascade_event]
        mock_auditor.get_event_count.return_value = 1

        with patch(
            "baldur.api.handlers.cascade._get_cascade_auditor",
            return_value=mock_auditor,
        ):
            ctx = _make_ctx({"namespace": "seoul", "is_test": "invalid"})
            response = cascade_event_list(ctx)

            assert response.status_code == 200
            # "invalid".lower() == "true" -> False, so filters as is_test=False
            assert len(response.body["events"]) == 0
