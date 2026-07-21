"""
Namespace Settings Unit Tests.

NamespaceSettings 및 관련 함수들을 테스트합니다.

"""

import os
from unittest.mock import patch


class TestNamespaceSettings:
    """NamespaceSettings 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.settings.namespace import reset_namespace_settings

        reset_namespace_settings()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.settings.namespace import reset_namespace_settings

        reset_namespace_settings()

    def test_namespace_disabled_returns_empty(self):
        """비활성화 시 빈 문자열 반환."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(namespace_enabled=False)
        assert settings.get_effective_namespace() == ""

    def test_namespace_disabled_default(self):
        """기본값은 비활성화 상태."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings()
        assert settings.namespace_enabled is False
        assert settings.get_effective_namespace() == ""

    def test_namespace_priority_namespace_first(self):
        """우선순위 최상위: namespace."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            namespace="ns1",
            region="seoul",
            tenant="tenant123",
            env="production",
        )
        assert settings.get_effective_namespace() == "ns1"

    def test_namespace_priority_region_second(self):
        """우선순위 2순위: region."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            namespace=None,
            region="seoul",
            tenant="tenant123",
            env="production",
        )
        assert settings.get_effective_namespace() == "seoul"

    def test_namespace_priority_tenant_third(self):
        """우선순위 3순위: tenant."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            namespace=None,
            region=None,
            tenant="tenant123",
            env="production",
        )
        assert settings.get_effective_namespace() == "tenant123"

    def test_namespace_priority_env_fourth(self):
        """우선순위 4순위: env."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            namespace=None,
            region=None,
            tenant=None,
            env="staging",
        )
        assert settings.get_effective_namespace() == "staging"

    def test_namespace_fallback_to_default(self):
        """모두 없으면 default_namespace 사용."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            namespace=None,
            region=None,
            tenant=None,
            env=None,
            default_namespace="fallback",
        )
        assert settings.get_effective_namespace() == "fallback"

    def test_key_prefix_with_namespace(self):
        """네임스페이스 포함 키 프리픽스."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            region="seoul",
        )
        assert settings.get_key_prefix() == "baldur:seoul:"

    def test_key_prefix_without_namespace(self):
        """네임스페이스 없을 때 기존 형식."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(namespace_enabled=False)
        assert settings.get_key_prefix() == "baldur:"

    def test_key_prefix_custom_base(self):
        """커스텀 base_prefix 사용."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            region="tokyo",
        )
        assert settings.get_key_prefix("myapp") == "myapp:tokyo:"

    def test_singleton_returns_same_instance(self):
        """싱글톤이 같은 인스턴스 반환."""
        from baldur.settings.namespace import (
            get_namespace_settings,
            reset_namespace_settings,
        )

        reset_namespace_settings()
        s1 = get_namespace_settings()
        s2 = get_namespace_settings()
        assert s1 is s2

    def test_reset_clears_singleton(self):
        """reset 후 새 인스턴스 생성."""
        from baldur.settings.namespace import (
            get_namespace_settings,
            reset_namespace_settings,
        )

        s1 = get_namespace_settings()
        reset_namespace_settings()
        s2 = get_namespace_settings()
        assert s1 is not s2

    @patch.dict(
        os.environ,
        {
            "BALDUR_NAMESPACE_NAMESPACE_ENABLED": "true",
            "BALDUR_NAMESPACE_REGION": "osaka",
        },
        clear=False,
    )
    def test_env_var_loading(self):
        """환경변수에서 설정 로드."""
        from baldur.settings.namespace import (
            get_namespace_settings,
            reset_namespace_settings,
        )

        reset_namespace_settings()
        settings = get_namespace_settings()

        assert settings.namespace_enabled is True
        assert settings.region == "osaka"
        assert settings.get_effective_namespace() == "osaka"

    def test_get_key_prefix_function(self):
        """편의 함수 get_key_prefix 테스트."""
        from baldur.settings.namespace import (
            get_key_prefix,
            reset_namespace_settings,
        )

        reset_namespace_settings()

        # 기본값 (비활성화)
        assert get_key_prefix() == "baldur:"
        assert get_key_prefix("cb") == "cb:"


class TestNamespaceSettingsEdgeCases:
    """NamespaceSettings 엣지 케이스 테스트."""

    def setup_method(self):
        from baldur.settings.namespace import reset_namespace_settings

        reset_namespace_settings()

    def teardown_method(self):
        from baldur.settings.namespace import reset_namespace_settings

        reset_namespace_settings()

    def test_empty_string_namespace_treated_as_none(self):
        """빈 문자열은 None과 동일하게 처리 (falsy)."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            namespace="",  # 빈 문자열
            region="seoul",
        )
        # 빈 문자열은 falsy이므로 region으로 폴백
        assert settings.get_effective_namespace() == "seoul"

    def test_special_characters_in_namespace(self):
        """네임스페이스에 특수문자 포함."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            namespace="prod-kr-1",
        )
        assert settings.get_key_prefix() == "baldur:prod-kr-1:"

    def test_unicode_namespace(self):
        """유니코드 네임스페이스 (권장하지 않지만 가능)."""
        from baldur.settings.namespace import NamespaceSettings

        settings = NamespaceSettings(
            namespace_enabled=True,
            namespace="서울",
        )
        assert settings.get_key_prefix() == "baldur:서울:"
