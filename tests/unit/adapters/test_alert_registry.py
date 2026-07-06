"""
Alert Adapter 레지스트리 통합 단위 테스트.

테스트 대상:
- factory.py: ProviderRegistry.register_alert(), get_alert(),
  _auto_register_alert_adapters(), reset(), list_providers()
- adapters/alert/__init__.py: get_alert_adapter()
"""

from __future__ import annotations

import pytest

from baldur.adapters.alert import get_alert_adapter
from baldur.adapters.alert.null_adapter import NullAlertAdapter
from baldur.adapters.alert.stdout_adapter import StdoutAlertAdapter
from baldur.factory import ProviderRegistry
from baldur.interfaces.alert_adapter import AlertAdapter


@pytest.fixture(autouse=True)
def _reset_registry():
    """각 테스트 전후 ProviderRegistry 초기화."""
    ProviderRegistry.reset()
    yield
    ProviderRegistry.reset()


# =============================================================================
# ProviderRegistry.register_alert / get_alert — 동작 검증
# =============================================================================


class TestAlertRegistryBehavior:
    """ProviderRegistry alert 어댑터 등록/조회 동작 검증."""

    def test_register_and_get_custom_alert_adapter(self):
        """커스텀 어댑터 등록 후 조회 가능하다."""

        class CustomAdapter(AlertAdapter):
            def send(self, alert):
                pass

            def resolve(self, alert_key):
                pass

        ProviderRegistry.register_alert("custom", CustomAdapter)
        adapter = ProviderRegistry.get_alert("custom")

        assert isinstance(adapter, CustomAdapter)

    def test_get_alert_returns_singleton(self):
        """동일 이름으로 조회 시 같은 인스턴스를 반환한다."""
        adapter1 = ProviderRegistry.get_alert("stdout")
        adapter2 = ProviderRegistry.get_alert("stdout")

        assert adapter1 is adapter2

    def test_default_alert_is_stdout(self):
        """기본 alert 어댑터는 stdout이다."""
        adapter = ProviderRegistry.get_alert()

        assert isinstance(adapter, StdoutAlertAdapter)

    def test_get_null_adapter(self):
        """null 어댑터 조회 가능하다."""
        adapter = ProviderRegistry.get_alert("null")

        assert isinstance(adapter, NullAlertAdapter)

    def test_auto_register_populates_stdout_and_null(self):
        """자동 등록 시 stdout, null 어댑터가 등록된다."""
        from baldur.factory.adapters import discover_alert_adapters

        discover_alert_adapters()
        providers = ProviderRegistry.list_providers()

        assert "stdout" in providers["alert"]
        assert "null" in providers["alert"]

    def test_list_providers_includes_alert_key(self):
        """list_providers 결과에 alert 키가 포함된다."""
        providers = ProviderRegistry.list_providers()

        assert "alert" in providers

    def test_reset_clears_alert_registrations(self):
        """reset() 호출 시 alert 등록이 초기화된다."""
        ProviderRegistry.register_alert("test", StdoutAlertAdapter)
        ProviderRegistry.get_alert("test")  # populate instance cache

        ProviderRegistry.reset()

        providers = ProviderRegistry.list_providers()
        assert "test" not in providers["alert"]

    def test_clear_instances_clears_alert_cache(self):
        """clear_instances() 호출 시 alert 인스턴스 캐시가 초기화된다."""
        adapter1 = ProviderRegistry.get_alert("stdout")
        ProviderRegistry.clear_instances()
        adapter2 = ProviderRegistry.get_alert("stdout")

        assert adapter1 is not adapter2


# =============================================================================
# get_alert_adapter() 편의 함수 — 동작 검증
# =============================================================================


class TestGetAlertAdapterBehavior:
    """adapters/alert/__init__.py get_alert_adapter() 동작 검증."""

    def test_returns_default_adapter(self):
        """인자 없이 호출 시 기본 어댑터를 반환한다."""
        adapter = get_alert_adapter()

        assert isinstance(adapter, StdoutAlertAdapter)

    def test_returns_named_adapter(self):
        """이름 지정 시 해당 어댑터를 반환한다."""
        adapter = get_alert_adapter("null")

        assert isinstance(adapter, NullAlertAdapter)

    def test_delegates_to_provider_registry(self):
        """ProviderRegistry.get_alert에 위임한다."""
        # get_alert_adapter 결과와 ProviderRegistry.get_alert 결과 비교
        adapter1 = get_alert_adapter("stdout")
        adapter2 = ProviderRegistry.get_alert("stdout")

        assert adapter1 is adapter2


# =============================================================================
# ProviderRegistry._default_alert — 계약 검증
# =============================================================================


class TestAlertDefaultContract:
    """ProviderRegistry alert 기본값 계약 검증."""

    def test_default_alert_name_is_stdout(self):
        """alert sub-registry default is 'stdout' after discovery."""
        from baldur.factory.adapters import discover_alert_adapters

        discover_alert_adapters()
        # After auto-discover, first registered becomes default = "stdout"
        assert ProviderRegistry.alert.get_default_name() == "stdout"

    def test_reset_clears_default_alert(self):
        """reset() clears alert default to None."""
        ProviderRegistry.alert.set_default("null")
        ProviderRegistry.reset()
        assert ProviderRegistry.alert.get_default_name() is None
