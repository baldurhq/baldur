"""Unit tests for the Django connection-close helpers (adapters/django/utils.py).

The two helpers look interchangeable and are not:

- ``close_django_connections()`` wraps Django's ``close_old_connections()``, a
  *conditional* close that fires only on an autocommit mismatch, after an
  error, or once ``CONN_MAX_AGE`` has elapsed. Under persistent connections it
  is a no-op.
- ``close_all_django_connections()`` means "this thread is finished, release
  everything", for a worker thread that opened connections outside the request
  cycle where ``request_finished`` never fires.

Picking the conditional one for a worker thread strands the connection on any
``CONN_MAX_AGE > 0`` deployment, which is why the delegation target of each is
pinned here rather than left to inspection.

Verification techniques:
- Dependency interaction: which Django API each helper delegates to
- Exception/edge cases: Django-absent no-op path
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from baldur.adapters.django.utils import (
    close_all_django_connections,
    close_django_connections,
)


class TestCloseAllDjangoConnectionsBehavior:
    """close_all_django_connections() releases every thread-local connection."""

    def test_delegates_to_connections_close_all(self):
        """The unconditional helper calls ``connections.close_all()``.

        Asserting the delegation target, not merely that the call returns:
        ``close_old_connections()`` would also return cleanly here while
        leaving a persistent connection open.
        """
        with patch("django.db.connections") as mock_connections:
            close_all_django_connections()

        mock_connections.close_all.assert_called_once_with()

    def test_no_ops_when_django_is_absent(self):
        """A Django-free install must not raise — the helper is import-guarded.

        "Does not raise" is the whole contract on this path (§9.3): a
        SQLAlchemy-only deployment calls this from the probe worker's finally,
        where an ImportError would replace the probe's return value.
        """
        with patch.dict(sys.modules, {"django.db": None}):
            close_all_django_connections()

    def test_propagates_nothing_but_still_calls_through_repeatedly(self):
        """Idempotent: calling it twice closes twice, with no accumulated state."""
        with patch("django.db.connections") as mock_connections:
            close_all_django_connections()
            close_all_django_connections()

        assert mock_connections.close_all.call_count == 2


class TestCloseDjangoConnectionsBehavior:
    """close_django_connections() keeps its original conditional semantics."""

    def test_delegates_to_close_old_connections(self):
        """Unchanged: the request-boundary helper still wraps the Django one.

        It has a PRO ``pre_execute_hook`` caller, so widening it to an
        unconditional close would change behavior far outside the probe path.
        """
        with patch("django.db.close_old_connections") as mock_close_old:
            close_django_connections()

        mock_close_old.assert_called_once_with()

    def test_no_ops_when_django_is_absent(self):
        """Django-free install: silently no-ops."""
        with patch.dict(sys.modules, {"django.db": None}):
            close_django_connections()


class TestConnectionCloseHelperSeparationContract:
    """The two helpers must not collapse into each other."""

    @pytest.mark.parametrize(
        ("helper", "expected_attr", "forbidden_attr"),
        [
            (close_all_django_connections, "close_all", "close_old_connections"),
            (close_django_connections, "close_old_connections", "close_all"),
        ],
    )
    def test_each_helper_calls_only_its_own_django_api(
        self, helper, expected_attr, forbidden_attr
    ):
        """Negative assertion: neither helper reaches for the other's API."""
        with (
            patch("django.db.connections") as mock_connections,
            patch("django.db.close_old_connections") as mock_close_old,
        ):
            helper()

        called = {
            "close_all": mock_connections.close_all.called,
            "close_old_connections": mock_close_old.called,
        }
        assert called[expected_attr] is True
        assert called[forbidden_attr] is False
