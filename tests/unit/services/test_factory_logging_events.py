"""
Factory.py 로깅 이벤트명 수정 (312) 단위 테스트.

검증 대상:
- factory.py의 모든 로그 이벤트명이 올바른 컨벤션을 따르는지
- 'cell_registry' 이벤트명이 제거되었는지
- AdapterNotFoundError가 올바르게 발생하는지

기법 분류:
- 계약 검증: 이벤트명 컨벤션, deprecated 이벤트명 제거
- 동작 검증: AdapterNotFoundError 발생 패턴
"""

from __future__ import annotations

import re

import pytest

from baldur.core.exceptions import AdapterNotFoundError, BaldurError
from baldur.factory import ProviderRegistry
from tests.factories import src_root

# =============================================================================
# 계약 검증 — 이벤트명 컨벤션
# =============================================================================


class TestFactoryLoggingEventNamesContract:
    """factory.py의 로그 이벤트명이 312 설계 계약대로 수정되었는지 검증."""

    @pytest.fixture(scope="class")
    def factory_source(self):
        """factory/registry.py 소스 코드를 읽어온다."""
        factory_path = src_root() / "baldur" / "factory" / "registry.py"
        return factory_path.read_text(encoding="utf-8")

    def test_no_cell_registry_event_names_remain(self, factory_source):
        """factory.py에서 deprecated 'cell_registry' 이벤트명이 완전히 제거되어야 한다."""
        assert "cell_registry" not in factory_source, (
            "factory.py still uses deprecated 'cell_registry' event names"
        )

    def test_all_log_events_follow_convention(self, factory_source):
        """모든 로그 이벤트가 'module.entity_action' 패턴을 따라야 한다."""
        events = re.findall(r'logger\.\w+\("([^"]+)"', factory_source)
        pattern = re.compile(r"^[a-z_]+\.[a-z_]+$")

        for event in events:
            assert pattern.match(event), (
                f"Event name '{event}' doesn't follow 'module.entity_action' convention"
            )

    def test_no_bare_registry_log_calls(self, factory_source):
        """'logger.debug(\"registry\")' 같은 불완전한 로그 호출이 없어야 한다."""
        bare_pattern = re.compile(r'logger\.\w+\("registry"\s*[,)]')
        matches = bare_pattern.findall(factory_source)
        assert len(matches) == 0, f"Found bare 'registry' log calls: {matches}"


# =============================================================================
# 동작 검증 — AdapterNotFoundError
# =============================================================================


class TestFactoryAdapterNotFoundBehavior:
    """ProviderRegistry의 get_* 메서드가 AdapterNotFoundError를 발생시키는지 검증."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self):
        """각 테스트 전후로 레지스트리를 초기화한다."""
        ProviderRegistry.reset()
        yield
        ProviderRegistry.reset()

    def test_get_cache_unknown_raises_adapter_not_found(self):
        """미등록 캐시 어댑터 조회 시 AdapterNotFoundError가 발생해야 한다."""
        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_cache("nonexistent")

    def test_get_queue_unknown_raises_adapter_not_found(self):
        """미등록 큐 어댑터 조회 시 AdapterNotFoundError가 발생해야 한다."""
        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_queue("nonexistent")

    def test_adapter_not_found_is_baldur_error(self):
        """AdapterNotFoundError는 BaldurError 계열이어야 한다."""
        with pytest.raises(BaldurError):
            ProviderRegistry.get_cache("nonexistent")

    def test_adapter_not_found_is_not_value_error(self):
        """AdapterNotFoundError는 ValueError가 아니어야 한다."""
        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_cache("nonexistent")

        # ValueError로는 catch되지 않아야 함
        try:
            ProviderRegistry.get_cache("nonexistent")
        except ValueError:
            pytest.fail(
                "AdapterNotFoundError should not be caught by except ValueError"
            )
        except AdapterNotFoundError:
            pass
