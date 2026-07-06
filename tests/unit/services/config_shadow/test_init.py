"""
Unit tests for Config Shadow __init__.py singleton lifecycle.

Verified behaviors:
- get_shadow_evaluator_service: singleton caching behavior
- get_shadow_evaluator_service: new instance created after reset

Test target: baldur.services.config_shadow.__init__
"""

import threading
from unittest.mock import patch

from baldur.services.config_shadow import (
    get_shadow_evaluator_service,
    reset_shadow_evaluator_service,
)
from baldur.services.config_shadow.service import ShadowEvaluatorService


class TestGetShadowEvaluatorServiceBehavior:
    """get_shadow_evaluator_service singleton behavior verification."""

    def setup_method(self):
        """Reset singleton cache before each test."""
        reset_shadow_evaluator_service(cleanup=False)

    def teardown_method(self):
        """Reset singleton cache after each test."""
        reset_shadow_evaluator_service(cleanup=False)

    @patch(
        "baldur.services.config_shadow.service.ShadowEvaluatorService.__init__",
        return_value=None,
    )
    def test_returns_shadow_evaluator_service_instance(self, mock_init):
        """Returns a ShadowEvaluatorService instance."""
        result = get_shadow_evaluator_service()
        assert isinstance(result, ShadowEvaluatorService)

    @patch(
        "baldur.services.config_shadow.service.ShadowEvaluatorService.__init__",
        return_value=None,
    )
    def test_returns_same_instance_on_repeated_calls(self, mock_init):
        """Returns the same instance on repeated calls (singleton)."""
        first = get_shadow_evaluator_service()
        second = get_shadow_evaluator_service()
        assert first is second
        assert mock_init.call_count == 1

    @patch(
        "baldur.services.config_shadow.service.ShadowEvaluatorService.__init__",
        return_value=None,
    )
    def test_reset_creates_new_instance(self, mock_init):
        """Resetting the singleton creates a new instance."""
        first = get_shadow_evaluator_service()
        reset_shadow_evaluator_service(cleanup=False)
        second = get_shadow_evaluator_service()
        assert first is not second
        assert mock_init.call_count == 2

    @patch(
        "baldur.services.config_shadow.service.ShadowEvaluatorService.__init__",
        return_value=None,
    )
    def test_concurrent_init_creates_single_instance(self, mock_init):
        """Concurrent initialization creates only a single instance (lock protection)."""
        results = []

        def call_service():
            results.append(get_shadow_evaluator_service())

        threads = [threading.Thread(target=call_service) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(r is results[0] for r in results)
        assert mock_init.call_count == 1
