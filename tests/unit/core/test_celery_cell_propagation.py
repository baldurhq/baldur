"""
Celery Cell ID 전파 테스트.

before_task_publish 하이브리드 3단계, task_prerun/postrun ContextVar 설정/정리.

참조 소스:
- context/celery_cell_propagation.py (add_cell_id_to_task, extract_cell_id_on_prerun,
  clear_cell_id_on_postrun, CELERY_ROUTING_KEYS, _extract_routing_key)
- context/cell_context.py (_current_cell_id, get_current_cell_id)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.context.celery_cell_propagation import (
    CELERY_ROUTING_KEYS,
    _extract_routing_key,
    add_cell_id_to_task,
    clear_cell_id_on_postrun,
    extract_cell_id_on_prerun,
)
from baldur.context.cell_context import _current_cell_id, get_current_cell_id


class TestExtractRoutingKeyContract:
    """라우팅 키 추출 계약 검증."""

    def test_routing_keys_order(self):
        """라우팅 키 우선순위: service_name > namespace > domain > user_id."""
        assert CELERY_ROUTING_KEYS == [
            "service_name",
            "namespace",
            "domain",
            "user_id",
        ]


class TestExtractRoutingKeyBehavior:
    """_extract_routing_key 동작 검증."""

    def test_extracts_service_name(self):
        """service_name이 있으면 반환해야 한다."""
        result = _extract_routing_key(
            {"service_name": "toss_api", "namespace": "global"}
        )
        assert result == ("service_name", "toss_api")

    def test_extracts_namespace_when_no_service_name(self):
        """service_name이 없으면 namespace를 반환해야 한다."""
        result = _extract_routing_key({"namespace": "payment"})
        assert result == ("namespace", "payment")

    def test_extracts_domain(self):
        """domain만 있으면 반환해야 한다."""
        result = _extract_routing_key({"domain": "order"})
        assert result == ("domain", "order")

    def test_extracts_user_id(self):
        """user_id만 있으면 반환해야 한다."""
        result = _extract_routing_key({"user_id": "uid-42"})
        assert result == ("user_id", "uid-42")

    def test_returns_none_for_empty_kwargs(self):
        """빈 kwargs면 None을 반환해야 한다."""
        assert _extract_routing_key({}) is None

    def test_converts_value_to_str(self):
        """정수 값도 문자열로 변환해야 한다."""
        result = _extract_routing_key({"user_id": 12345})
        assert result == ("user_id", "12345")

    def test_none_value_is_skipped(self):
        """None 값은 건너뛰어야 한다."""
        result = _extract_routing_key({"service_name": None, "namespace": "ns1"})
        assert result == ("namespace", "ns1")


class TestAddCellIdToTaskBehavior:
    """add_cell_id_to_task 동작 검증."""

    def test_skip_when_headers_none(self):
        """headers가 None이면 아무 동작도 하지 않아야 한다."""
        # 예외 없이 정상 종료
        add_cell_id_to_task(sender="test.task", headers=None, body=None)

    def test_skip_when_cell_id_already_set(self):
        """headers에 cell_id가 이미 있으면 덮어쓰지 않아야 한다."""
        headers = {"cell_id": "cell-existing"}
        add_cell_id_to_task(sender="test.task", headers=headers, body=None)
        assert headers["cell_id"] == "cell-existing"

    @patch("baldur.settings.cell_topology.get_cell_topology_settings")
    def test_skip_when_disabled(self, mock_settings):
        """비활성 시 cell_id를 삽입하지 않아야 한다."""
        mock_settings.return_value = MagicMock(enabled=False, tagging_enabled=False)
        headers = {}
        add_cell_id_to_task(sender="test.task", headers=headers, body=None)
        assert "cell_id" not in headers

    @patch("baldur.settings.cell_topology.get_cell_topology_settings")
    def test_inherits_contextvar_cell_id(self, mock_settings):
        """ContextVar에 cell_id가 있으면 1순위로 상속해야 한다."""
        mock_settings.return_value = MagicMock(enabled=True, tagging_enabled=True)
        _current_cell_id.set("cell-3")

        headers = {}
        add_cell_id_to_task(sender="test.task", headers=headers, body=None)
        assert headers["cell_id"] == "cell-3"

    @patch("baldur.services.cell_topology.get_cell_registry")
    @patch("baldur.settings.cell_topology.get_cell_topology_settings")
    def test_extracts_from_kwargs(self, mock_settings, mock_get_registry):
        """ContextVar 없고 kwargs에 라우팅 키가 있으면 2순위로 해시해야 한다."""
        mock_settings.return_value = MagicMock(enabled=True, tagging_enabled=True)
        mock_registry = MagicMock()
        mock_registry.get_cell_for_key.return_value = "cell-5"
        mock_get_registry.return_value = mock_registry

        headers = {"task": "force_open_circuit_breaker"}
        body = [[], {"service_name": "toss_api"}, {}]

        add_cell_id_to_task(sender="force_open_cb", headers=headers, body=body)
        assert headers["cell_id"] == "cell-5"
        mock_registry.get_cell_for_key.assert_called_once_with("service_name:toss_api")

    @patch("baldur.services.cell_topology.get_cell_registry")
    @patch("baldur.settings.cell_topology.get_cell_topology_settings")
    def test_falls_back_to_task_name(self, mock_settings, mock_get_registry):
        """ContextVar 없고 kwargs에 라우팅 키도 없으면 3순위 task_name 해시해야 한다."""
        mock_settings.return_value = MagicMock(enabled=True, tagging_enabled=True)
        mock_registry = MagicMock()
        mock_registry.get_cell_for_key.return_value = "cell-1"
        mock_get_registry.return_value = mock_registry

        headers = {"task": "collect_baldur_metrics"}
        body = [[], {}, {}]

        add_cell_id_to_task(sender="metrics", headers=headers, body=body)
        assert headers["cell_id"] == "cell-1"
        mock_registry.get_cell_for_key.assert_called_once_with(
            "task:collect_baldur_metrics"
        )

    @patch("baldur.settings.cell_topology.get_cell_topology_settings")
    def test_exception_is_silenced(self, mock_settings):
        """태깅 실패 시 예외가 무시되어야 한다 (Fail-Open)."""
        mock_settings.side_effect = RuntimeError("boom")
        headers = {}
        # 예외 없이 정상 종료
        add_cell_id_to_task(sender="test.task", headers=headers, body=None)
        assert "cell_id" not in headers


class TestExtractCellIdOnPrerunBehavior:
    """extract_cell_id_on_prerun 동작 검증."""

    def test_sets_contextvar_from_task_request(self):
        """태스크 헤더에서 cell_id를 ContextVar에 설정해야 한다."""
        task = MagicMock()
        task.request.get.return_value = "cell-6"

        extract_cell_id_on_prerun(task=task)

        assert get_current_cell_id() == "cell-6"
        assert hasattr(task, "_cell_id_token")

    def test_no_cell_id_in_headers(self):
        """cell_id가 없으면 ContextVar를 변경하지 않아야 한다."""
        task = MagicMock()
        task.request.get.return_value = None

        extract_cell_id_on_prerun(task=task)
        assert get_current_cell_id() is None

    def test_none_task_is_safe(self):
        """task가 None이면 예외 없이 무시해야 한다."""
        extract_cell_id_on_prerun(task=None)
        assert get_current_cell_id() is None


class TestClearCellIdOnPostrunBehavior:
    """clear_cell_id_on_postrun 동작 검증."""

    def test_clears_contextvar(self):
        """태스크 종료 시 ContextVar를 복원해야 한다."""
        token = _current_cell_id.set("cell-9")
        task = MagicMock()
        task._cell_id_token = token

        clear_cell_id_on_postrun(task=task)
        assert get_current_cell_id() is None

    def test_no_token_attribute_is_safe(self):
        """_cell_id_token 속성이 없으면 예외 없이 무시해야 한다."""
        task = MagicMock(spec=[])  # no attributes
        clear_cell_id_on_postrun(task=task)

    def test_none_task_is_safe(self):
        """task가 None이면 예외 없이 무시해야 한다."""
        clear_cell_id_on_postrun(task=None)
