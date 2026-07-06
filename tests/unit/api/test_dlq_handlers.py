"""Unit tests for framework-agnostic DLQ management handlers (429 PR3-phase2a).

Target: ``baldur.api.handlers.dlq`` — 9 handlers covering replay,
cleanup stats/archive/purge, list, detail, retry, resolve, test-create.

Verification techniques applied (§8):
  - §8.1 Boundary analysis — batch_size 1..200
  - §8.2 Exception/edge cases — pk parsing, missing confirm, 404 on missing entry
  - §8.5 Dependency interaction — service method argument forwarding
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.dlq import (
    _parse_int,
    _parse_pk,
    dlq_cleanup_archive,
    dlq_cleanup_purge,
    dlq_cleanup_stats,
    dlq_detail,
    dlq_facets,
    dlq_list,
    dlq_replay,
    dlq_resolve,
    dlq_retry,
    dlq_test_create,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.models.dlq import CleanupStats


@pytest.fixture(autouse=True, scope="module")
def _ensure_baldur_pro_dlq_loaded():
    """Force ``baldur_pro.services.dlq`` into ``sys.modules`` before the
    function-scoped ``_pro_singleton_providers_registered`` autouse fixture
    (tests/conftest.py) runs for the first test in this file.

    That conftest fixture only registers ``ProviderRegistry.dlq_service`` when
    the PRO module is already imported; otherwise the slot stays empty and a
    handler call raises ``RuntimeError: DLQ handlers require baldur_pro
    DLQService``. The bulk of this file's tests happen to import the module
    incidentally (every ``with patch("baldur_pro.services.dlq.get_dlq_service")``
    imports it), but the very first test executed under a narrowing selector
    like ``pytest -k facet`` runs before any such patch and would otherwise
    fail at handler dispatch.

    Module-scoped autouse runs before the per-test function-scoped autouse,
    and the import lives inside the fixture body (not module top) so G19
    (``tests/architecture/test_oss_tests_pro_marker.py``) does not flag
    this file as an unmarked PRO-gated import.

    PRO-absent (public mirror): the import is best-effort. The pure-OSS
    ``_parse_int`` / ``_parse_pk`` contract tests must keep running; the
    handler-dispatch tests are independently skipped via ``importorskip``.
    """
    try:
        import baldur_pro.services.dlq  # noqa: F401
    except ImportError:
        pass

    return


def _make_ctx(
    method="GET",
    path="/test/",
    query=None,
    path_params=None,
    json_body=None,
    user=None,
):
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params=path_params or {},
        json_body=json_body,
        user=user,
    )


# =============================================================================
# _parse_int / _parse_pk — Contract
# =============================================================================


class TestParseIntContract:
    def test_returns_int_when_input_is_valid(self):
        assert _parse_int("42", 10) == 42

    def test_returns_default_when_input_is_none(self):
        assert _parse_int(None, 10) == 10

    def test_returns_default_when_input_is_non_numeric(self):
        assert _parse_int("abc", 7) == 7

    def test_returns_default_when_input_is_empty_string(self):
        assert _parse_int("", 5) == 5


class TestParsePkContract:
    def test_returns_str_when_path_param_is_numeric(self):
        # 538 D1: opaque-string id — numeric pk returned verbatim, no int().
        ctx = _make_ctx(path_params={"pk": "123"})
        assert _parse_pk(ctx) == "123"

    def test_returns_none_when_path_param_is_missing(self):
        assert _parse_pk(_make_ctx()) is None

    def test_returns_composite_string_verbatim(self):
        # A composite Redis-adapter id (538 D2) is a valid opaque pk.
        ctx = _make_ctx(path_params={"pk": "pod:1:abc:0"})
        assert _parse_pk(ctx) == "pod:1:abc:0"


# =============================================================================
# dlq_replay — batch_size boundary + delegation
# =============================================================================


def _mock_replay_result(processed=10, success=8, failed=1, skipped=1):
    return SimpleNamespace(
        processed=processed, success=success, failed=failed, skipped=skipped
    )


class TestDlqReplayBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_batch_size_below_minimum_rejected_as_400(self):
        """batch_size=0 -> 400."""
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_get:
            resp = dlq_replay(_make_ctx(method="POST", json_body={"batch_size": 0}))
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_batch_size_above_maximum_rejected_as_400(self):
        """batch_size=201 -> 400."""
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_get:
            resp = dlq_replay(_make_ctx(method="POST", json_body={"batch_size": 201}))
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_batch_size_boundary_values_accepted(self):
        """batch_size=1 and 200 -> pass."""
        service = MagicMock()
        service.replay.return_value = _mock_replay_result()
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp_min = dlq_replay(_make_ctx(method="POST", json_body={"batch_size": 1}))
            resp_max = dlq_replay(
                _make_ctx(method="POST", json_body={"batch_size": 200})
            )
        assert resp_min.status_code == 200
        assert resp_max.status_code == 200

    def test_batch_size_non_int_returns_400(self):
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_get:
            resp = dlq_replay(
                _make_ctx(method="POST", json_body={"batch_size": "lots"})
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_domain_forwarded_to_service(self):
        service = MagicMock()
        service.replay.return_value = _mock_replay_result()
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_replay(
                _make_ctx(
                    method="POST",
                    json_body={"domain": "payment", "batch_size": 50},
                )
            )
        service.replay.assert_called_once_with(
            domain="payment", batch_size=50, actor_id="anonymous", reason=None
        )


# =============================================================================
# dlq_cleanup_stats / archive / purge
# =============================================================================


def _mock_cleanup_stats():
    # The real DLQService.get_cleanup_stats returns a CleanupStats value object
    # (not an ad-hoc attribute bag) whose can_archive/can_purge are derived int
    # counts. Mocking the true return type keeps this handler test from masking
    # a service/handler contract drift.
    return CleanupStats(
        total=100,
        by_status={"pending": 50, "resolved": 30, "archived": 20},
        resolved_older_than_30_days=10,
        archived_older_than_90_days=5,
    )


class TestDlqCleanupStatsBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_returns_full_stats_shape(self):
        service = MagicMock()
        service.get_cleanup_stats.return_value = _mock_cleanup_stats()
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_cleanup_stats(_make_ctx())
        assert resp.body["total"] == 100
        # can_archive/can_purge are CleanupStats-derived int counts.
        assert resp.body["recommendations"]["can_archive"] == 10
        assert resp.body["recommendations"]["can_purge"] == 5


class TestDlqCleanupArchiveBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_default_older_than_days_is_30(self):
        """Empty body -> older_than_days=30."""
        service = MagicMock()
        service.archive_old_entries.return_value = 7
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_cleanup_archive(_make_ctx(method="POST", json_body=None))
        service.archive_old_entries.assert_called_once_with(older_than_days=30)

    def test_custom_older_than_days_forwarded(self):
        service = MagicMock()
        service.archive_old_entries.return_value = 3
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_cleanup_archive(
                _make_ctx(method="POST", json_body={"older_than_days": "45"})
            )
        service.archive_old_entries.assert_called_once_with(older_than_days=45)


class TestDlqCleanupPurgeBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_missing_confirm_returns_400(self):
        """confirm=false -> 400 without invoking service."""
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_get:
            resp = dlq_cleanup_purge(
                _make_ctx(method="POST", json_body={"confirm": False})
            )
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_missing_body_returns_400(self):
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_get:
            resp = dlq_cleanup_purge(_make_ctx(method="POST", json_body=None))
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_confirm_true_invokes_service_purge(self):
        """confirm=true + ids -> service.purge_archived called."""
        service = MagicMock()
        service.purge_archived.return_value = 5
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_cleanup_purge(
                _make_ctx(
                    method="POST",
                    json_body={"confirm": True, "ids": [1, 2, 3]},
                )
            )
        assert resp.status_code == 200
        service.purge_archived.assert_called_once_with(
            ids=[1, 2, 3], older_than_days=None
        )
        assert "warning" in resp.body

    def test_comma_separated_ids_string_split_to_list(self):
        """Console sends IDs as a comma-separated string -> split to a list."""
        service = MagicMock()
        service.purge_archived.return_value = 2
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_cleanup_purge(
                _make_ctx(
                    method="POST",
                    json_body={"confirm": True, "ids": "a:1 , b:2 ,  c:3"},
                )
            )
        assert resp.status_code == 200
        service.purge_archived.assert_called_once_with(
            ids=["a:1", "b:2", "c:3"], older_than_days=None
        )

    def test_ids_take_precedence_over_older_than_days(self):
        """When both are sent (console form default), ids win and the age
        filter is dropped so the repository never sees both (it raises)."""
        service = MagicMock()
        service.purge_archived.return_value = 1
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_cleanup_purge(
                _make_ctx(
                    method="POST",
                    json_body={"confirm": True, "ids": "x:0", "older_than_days": 90},
                )
            )
        assert resp.status_code == 200
        service.purge_archived.assert_called_once_with(
            ids=["x:0"], older_than_days=None
        )


# =============================================================================
# dlq_list / dlq_detail
# =============================================================================


def _mock_list_result():
    # 541 D7: list_entries returns a dict (matching the IPC consumer and the
    # dict-reading handler), not an attribute-access object.
    return {
        "results": [{"id": 1}],
        "page": 1,
        "page_size": 20,
        "total_pages": 1,
        "total_count": 1,
        "has_next": False,
        "has_previous": False,
    }


class TestDlqListBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_filters_from_query_params(self):
        service = MagicMock()
        service.list_entries.return_value = _mock_list_result()
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_list(_make_ctx(query={"status": "pending", "domain": "payment"}))
        _, kwargs = service.list_entries.call_args
        assert kwargs["filters"] == {"status": "pending", "domain": "payment"}

    def test_pagination_defaults(self):
        service = MagicMock()
        service.list_entries.return_value = _mock_list_result()
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_list(_make_ctx())
        _, kwargs = service.list_entries.call_args
        assert kwargs["page"] == 1
        assert kwargs["page_size"] == 20

    def test_response_envelope_built_from_dict_keys(self):
        """541 D7: the handler reads the dict result by key — an attribute-only
        object would raise AttributeError, so this asserts the dict contract
        end-to-end (the response pagination envelope mirrors the dict values)."""
        service = MagicMock()
        service.list_entries.return_value = {
            "results": [{"id": 7}],
            "page": 2,
            "page_size": 25,
            "total_pages": 4,
            "total_count": 87,
            "has_next": True,
            "has_previous": True,
        }
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_list(_make_ctx())

        assert resp.status_code == 200
        assert resp.body["results"] == [{"id": 7}]
        assert resp.body["pagination"] == {
            "page": 2,
            "page_size": 25,
            "total_pages": 4,
            "total_count": 87,
            "has_next": True,
            "has_previous": True,
        }


class TestDlqDetailBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_empty_pk_returns_400(self):
        # 538 D1: only a missing/empty pk is invalid; any non-empty token is
        # a valid opaque id (numeric or composite).
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_get:
            resp = dlq_detail(_make_ctx(path_params={"pk": ""}))
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_missing_entry_returns_404(self):
        service = MagicMock()
        service.get_entry.return_value = None
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_detail(_make_ctx(path_params={"pk": "99"}))
        assert resp.status_code == 404

    def test_found_entry_returns_200_with_body(self):
        service = MagicMock()
        service.get_entry.return_value = {"id": 7, "status": "pending"}
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_detail(_make_ctx(path_params={"pk": "7"}))
        assert resp.status_code == 200
        assert resp.body == {"id": 7, "status": "pending"}


# =============================================================================
# dlq_retry / dlq_resolve
# =============================================================================


class TestDlqRetryBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_empty_pk_returns_400(self):
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_get:
            resp = dlq_retry(_make_ctx(method="POST", path_params={"pk": ""}))
        assert resp.status_code == 400
        mock_get.assert_not_called()

    def test_valid_pk_invokes_service_retry(self):
        service = MagicMock()
        # retry_entry returns a dict (service contract); the handler reads it by
        # key. A SimpleNamespace mock would mask a dict/attribute drift.
        service.retry_entry.return_value = {
            "success": True,
            "id": "7",
            "retry_count": 3,
            "previous_retry_count": 2,
            "message": "ok",
        }
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_retry(_make_ctx(method="POST", path_params={"pk": "7"}))
        service.retry_entry.assert_called_once_with("7", reason=None)
        assert resp.body["retry_count"] == 3


class TestDlqResolveBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_notes_default_uses_actor(self):
        """Empty body -> notes='Manually resolved by <actor>'."""
        service = MagicMock()
        service.resolve_entry.return_value = {
            "success": True,
            "id": 7,
            "previous_status": "pending",
            "current_status": "resolved",
            "resolved_at": "2026-01-01",
            "notes": "Manually resolved by alice",
        }
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_resolve(
                _make_ctx(
                    method="POST",
                    path_params={"pk": "7"},
                    user=SimpleNamespace(username="alice"),
                )
            )
        _, kwargs = service.resolve_entry.call_args
        assert "alice" in kwargs["notes"]

    def test_custom_notes_preserved(self):
        service = MagicMock()
        service.resolve_entry.return_value = {
            "success": True,
            "id": 7,
            "previous_status": "pending",
            "current_status": "resolved",
            "resolved_at": None,
            "notes": "root cause fixed",
        }
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_resolve(
                _make_ctx(
                    method="POST",
                    path_params={"pk": "7"},
                    json_body={"notes": "root cause fixed"},
                )
            )
        service.resolve_entry.assert_called_once_with("7", notes="root cause fixed")


# =============================================================================
# dlq_test_create
# =============================================================================


class TestDlqTestCreateBehavior:
    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_returns_201_created(self):
        """Test entry creation returns 201."""
        service = MagicMock()
        service.create_test_entry.return_value = {"dlq_id": 42}
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_test_create(
                _make_ctx(
                    method="POST",
                    json_body={"domain": "payment", "failure_type": "timeout"},
                    user=SimpleNamespace(username="tester", id=9),
                )
            )
        assert resp.status_code == 201
        assert resp.body == {"dlq_id": 42}

    def test_user_id_forwarded_when_available(self):
        service = MagicMock()
        service.create_test_entry.return_value = {"dlq_id": 42}
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_test_create(
                _make_ctx(
                    method="POST",
                    json_body={},
                    user=SimpleNamespace(username="tester", id=9),
                )
            )
        _, kwargs = service.create_test_entry.call_args
        assert kwargs["user_id"] == 9

    def test_user_id_none_when_no_user(self):
        service = MagicMock()
        service.create_test_entry.return_value = {"dlq_id": 42}
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_test_create(_make_ctx(method="POST", json_body={}))
        _, kwargs = service.create_test_entry.call_args
        assert kwargs["user_id"] is None


# =============================================================================
# dlq_facets — 542 D1
# =============================================================================


class TestDlqFacetsHandlerContract:
    """``GET /dlq/facets`` body shape + query-param forwarding to the service.

    Contract verified:
    - response body has exactly the two top-level keys ``by_status`` and
      ``by_domain`` (mirrors the repository return shape, see
      ``FailedOperationRepository.get_facet_counts`` docstring)
    - ``status`` / ``domain`` query params reach the service as keyword args
    - the empty-string query value normalises to ``None`` so a request like
      ``/dlq/facets?status=&domain=`` is equivalent to unfiltered (matches
      the `ctx.get_query(...) or None` coercion in dlq_facets)
    """

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_response_body_has_by_status_and_by_domain_keys(self):
        service = MagicMock()
        service.get_facet_counts.return_value = {
            "by_status": {"pending": 4, "resolved": 2},
            "by_domain": {"payment": 3, "inventory": 3},
        }
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_facets(_make_ctx())

        assert resp.status_code == 200
        assert set(resp.body.keys()) == {"by_status", "by_domain"}
        assert resp.body["by_status"] == {"pending": 4, "resolved": 2}
        assert resp.body["by_domain"] == {"payment": 3, "inventory": 3}

    def test_unfiltered_request_passes_none_to_service(self):
        """No query params → service called with ``status=None, domain=None``."""
        service = MagicMock()
        service.get_facet_counts.return_value = {"by_status": {}, "by_domain": {}}
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_facets(_make_ctx())
        service.get_facet_counts.assert_called_once_with(status=None, domain=None)

    def test_status_and_domain_params_forwarded_as_kwargs(self):
        """Both filters present → forwarded verbatim as keyword args (D1)."""
        service = MagicMock()
        service.get_facet_counts.return_value = {"by_status": {}, "by_domain": {}}
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_facets(_make_ctx(query={"status": "resolved", "domain": "payment"}))
        service.get_facet_counts.assert_called_once_with(
            status="resolved", domain="payment"
        )

    def test_empty_string_query_value_normalises_to_none(self):
        """``?status=&domain=`` → ``status=None, domain=None`` (D6: an empty
        URL value matches the JS sending no filter)."""
        service = MagicMock()
        service.get_facet_counts.return_value = {"by_status": {}, "by_domain": {}}
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            dlq_facets(_make_ctx(query={"status": "", "domain": ""}))
        service.get_facet_counts.assert_called_once_with(status=None, domain=None)

    def test_missing_top_level_keys_default_to_empty_dict(self):
        """Defensive: a service returning a partial dict still produces a
        well-formed response body (handler uses ``.get(..., {})``)."""
        service = MagicMock()
        service.get_facet_counts.return_value = {}  # neither key present
        with patch("baldur_pro.services.dlq.get_dlq_service", return_value=service):
            resp = dlq_facets(_make_ctx())
        assert resp.status_code == 200
        assert resp.body == {"by_status": {}, "by_domain": {}}
