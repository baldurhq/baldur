"""
CellTagger 동작 검증 테스트.

태깅 키 우선순위, Fallback 분산, 비활성화 시 기본 Cell 할당 테스트.

참조 소스:
- services/cell_topology/tagger.py (CellTagger, TAG_KEY_PRIORITY)
- services/cell_topology/registry.py (CellRegistry, get_cell_for_key)
- settings/cell_topology.py (CellTopologySettings)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.services.cell_topology.registry import CellRegistry
from baldur.services.cell_topology.tagger import CellTagger
from baldur.settings.cell_topology import (
    CellTopologySettings,
    reset_cell_topology_settings,
)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """테스트 간 싱글톤 리셋."""
    reset_cell_topology_settings()
    yield
    reset_cell_topology_settings()


@pytest.fixture
def enabled_settings() -> CellTopologySettings:
    """enabled + tagging_enabled인 설정."""
    return CellTopologySettings(
        enabled=True,
        tagging_enabled=True,
        bulkhead_isolation_enabled=False,
        cell_count=8,
        cell_prefix="cell",
    )


@pytest.fixture
def disabled_settings() -> CellTopologySettings:
    """전체 비활성 설정."""
    return CellTopologySettings(
        enabled=False,
        tagging_enabled=False,
        cell_prefix="cell",
    )


@pytest.fixture
def tagging_disabled_settings() -> CellTopologySettings:
    """enabled=True이지만 tagging_enabled=False."""
    return CellTopologySettings(
        enabled=True,
        tagging_enabled=False,
        cell_prefix="cell",
    )


@pytest.fixture
def registry(enabled_settings: CellTopologySettings) -> CellRegistry:
    """활성화된 CellRegistry."""
    return CellRegistry(settings=enabled_settings)


@pytest.fixture
def tagger(registry: CellRegistry) -> CellTagger:
    """CellRegistry가 주입된 CellTagger."""
    t = CellTagger()
    t._cell_registry = registry
    return t


@pytest.fixture
def disabled_tagger(disabled_settings: CellTopologySettings) -> CellTagger:
    """비활성 CellRegistry가 주입된 CellTagger."""
    reg = CellRegistry(settings=disabled_settings)
    t = CellTagger()
    t._cell_registry = reg
    return t


@pytest.fixture
def tagging_disabled_tagger(
    tagging_disabled_settings: CellTopologySettings,
) -> CellTagger:
    """tagging_enabled=False인 CellTagger."""
    reg = CellRegistry(settings=tagging_disabled_settings)
    t = CellTagger()
    t._cell_registry = reg
    return t


class TestCellTaggerContract:
    """CellTagger 계약 검증."""

    def test_tag_key_priority_order(self):
        """태깅 키 우선순위: tenant_id > user_id > session_id > client_ip."""
        assert CellTagger.TAG_KEY_PRIORITY == [
            "tenant_id",
            "user_id",
            "session_id",
            "client_ip",
        ]

    def test_tag_key_priority_count(self):
        """태깅 키는 4개여야 한다."""
        assert len(CellTagger.TAG_KEY_PRIORITY) == 4


class TestCellTaggerResolveBehavior:
    """CellTagger.resolve_cell_id 동작 검증."""

    def test_disabled_returns_default_cell(self, disabled_tagger: CellTagger):
        """비활성 시 '{prefix}-0'을 반환해야 한다."""
        result = disabled_tagger.resolve_cell_id({"user_id": "user-1"})
        settings = disabled_tagger._get_registry()._settings
        assert result == f"{settings.cell_prefix}-0"

    def test_tagging_disabled_returns_default_cell(
        self, tagging_disabled_tagger: CellTagger
    ):
        """tagging_enabled=False 시 '{prefix}-0'을 반환해야 한다."""
        result = tagging_disabled_tagger.resolve_cell_id({"user_id": "user-1"})
        settings = tagging_disabled_tagger._get_registry()._settings
        assert result == f"{settings.cell_prefix}-0"

    def test_tenant_id_has_highest_priority(self, tagger: CellTagger):
        """tenant_id가 있으면 다른 키보다 우선 사용해야 한다."""
        context = {
            "tenant_id": "tenant-abc",
            "user_id": "user-123",
            "session_id": "sess-xyz",
            "client_ip": "1.2.3.4",
        }
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id(context)
        expected = registry.get_cell_for_key("tenant_id:tenant-abc")
        assert result == expected

    def test_user_id_used_when_no_tenant(self, tagger: CellTagger):
        """tenant_id가 없으면 user_id를 사용해야 한다."""
        context = {"user_id": "user-42", "client_ip": "10.0.0.1"}
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id(context)
        expected = registry.get_cell_for_key("user_id:user-42")
        assert result == expected

    def test_session_id_used_when_no_user(self, tagger: CellTagger):
        """user_id가 없으면 session_id를 사용해야 한다."""
        context = {"session_id": "sess-abc", "client_ip": "10.0.0.1"}
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id(context)
        expected = registry.get_cell_for_key("session_id:sess-abc")
        assert result == expected

    def test_client_ip_used_when_no_session(self, tagger: CellTagger):
        """session_id가 없으면 client_ip를 사용해야 한다."""
        context = {"client_ip": "192.168.1.100"}
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id(context)
        expected = registry.get_cell_for_key("client_ip:192.168.1.100")
        assert result == expected

    def test_fallback_with_trace_id(self, tagger: CellTagger):
        """모든 키 누락 + trace_id 있으면 fallback:trace_id로 해시해야 한다."""
        context = {"trace_id": "abc-123-trace"}
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id(context)
        expected = registry.get_cell_for_key("fallback:abc-123-trace")
        assert result == expected

    def test_fallback_without_trace_id_distributes(self, tagger: CellTagger):
        """모든 키와 trace_id 누락 시에도 유효한 cell_id를 반환해야 한다."""
        result = tagger.resolve_cell_id({})
        assert result.startswith("cell-")

    def test_same_context_returns_same_cell(self, tagger: CellTagger):
        """동일한 컨텍스트는 동일한 Cell을 반환해야 한다 (결정적)."""
        context = {"user_id": "consistent-user"}
        cell1 = tagger.resolve_cell_id(context)
        cell2 = tagger.resolve_cell_id(context)
        assert cell1 == cell2

    def test_empty_string_values_are_skipped(self, tagger: CellTagger):
        """빈 문자열 값은 건너뛰어야 한다."""
        context = {"tenant_id": "", "user_id": "user-real"}
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id(context)
        expected = registry.get_cell_for_key("user_id:user-real")
        assert result == expected


class TestCellTaggerResolveFromRequestBehavior:
    """CellTagger.resolve_cell_id_from_request 동작 검증."""

    def _make_mock_request(
        self,
        user_pk=None,
        session_key=None,
        tenant_id=None,
        trace_id=None,
        ip_addr="127.0.0.1",
        x_forwarded_for=None,
    ) -> MagicMock:
        """테스트용 Django HttpRequest Mock 생성."""
        request = MagicMock()

        # META
        meta = {"REMOTE_ADDR": ip_addr}
        if x_forwarded_for:
            meta["HTTP_X_FORWARDED_FOR"] = x_forwarded_for
        request.META = meta

        # user
        if user_pk is not None:
            request.user = MagicMock()
            request.user.pk = user_pk
        else:
            request.user = MagicMock()
            request.user.pk = None

        # session
        if session_key is not None:
            request.session = MagicMock()
            request.session.session_key = session_key
        else:
            request.session = MagicMock()
            request.session.session_key = None

        # tenant_id
        if tenant_id is not None:
            request.tenant_id = tenant_id
        else:
            del request.tenant_id

        # trace_id
        if trace_id is not None:
            request.trace_id = trace_id
        else:
            del request.trace_id

        return request

    def test_extracts_user_id_from_request(self, tagger: CellTagger):
        """request.user.pk에서 user_id를 추출해야 한다."""
        request = self._make_mock_request(user_pk=42)
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id_from_request(request)
        expected = registry.get_cell_for_key("user_id:42")
        assert result == expected

    def test_extracts_session_id_from_request(self, tagger: CellTagger):
        """request.session.session_key에서 session_id를 추출해야 한다."""
        request = self._make_mock_request(session_key="sess-abc123")
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id_from_request(request)
        expected = registry.get_cell_for_key("session_id:sess-abc123")
        assert result == expected

    def test_extracts_tenant_id_from_request(self, tagger: CellTagger):
        """request.tenant_id에서 tenant_id를 추출해야 한다."""
        request = self._make_mock_request(tenant_id="tenant-xyz", user_pk=42)
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id_from_request(request)
        expected = registry.get_cell_for_key("tenant_id:tenant-xyz")
        assert result == expected

    def test_falls_back_to_client_ip(self, tagger: CellTagger):
        """user/session/tenant 누락 시 client_ip를 사용해야 한다."""
        request = self._make_mock_request(ip_addr="10.0.0.55")
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id_from_request(request)
        expected = registry.get_cell_for_key("client_ip:10.0.0.55")
        assert result == expected

    def test_x_forwarded_for_takes_first_ip(self, tagger: CellTagger):
        """X-Forwarded-For 헤더에서 첫 번째 IP를 추출해야 한다."""
        request = self._make_mock_request(
            ip_addr="10.0.0.1",
            x_forwarded_for="203.0.113.50, 70.41.3.18, 150.172.238.178",
        )
        registry = tagger._get_registry()

        result = tagger.resolve_cell_id_from_request(request)
        expected = registry.get_cell_for_key("client_ip:203.0.113.50")
        assert result == expected

    def test_extracts_trace_id_for_fallback(self, tagger: CellTagger):
        """trace_id가 있으면 Fallback 해시 키로 사용해야 한다."""
        request = self._make_mock_request(
            ip_addr="unknown",
            trace_id="trace-abc-123",
        )
        # client_ip가 "unknown"이므로 client_ip 키가 사용될 것
        # (unknown은 falsy가 아니므로 client_ip로 해시됨)
        registry = tagger._get_registry()
        result = tagger.resolve_cell_id_from_request(request)
        expected = registry.get_cell_for_key("client_ip:unknown")
        assert result == expected
