"""
CeleryTaskAdapter default app binding unit tests.

Test target:
- adapters/queues/celery_adapter.py — CeleryTaskAdapter.__init__()

Regression: the app=None path previously imported a demo project module
(`myproject.celery`), which crashed with ModuleNotFoundError in any real
deployment. It must bind to Celery's current application instead.
"""

from __future__ import annotations

import pytest

celery = pytest.importorskip("celery")


class TestCeleryTaskAdapterDefaultApp:
    """CeleryTaskAdapter.__init__() resolves a usable Celery app."""

    def test_default_binds_to_current_celery_app(self):
        """app=None binds to the current Celery application (no demo-module import)."""
        from baldur.adapters.queues.celery_adapter import CeleryTaskAdapter

        adapter = CeleryTaskAdapter()

        assert isinstance(adapter._app, celery.Celery)

    def test_explicit_app_is_used_as_is(self):
        """An explicitly passed app instance is bound without substitution."""
        from baldur.adapters.queues.celery_adapter import CeleryTaskAdapter

        app = celery.Celery("explicit-test-app")
        adapter = CeleryTaskAdapter(app=app)

        assert adapter._app is app
