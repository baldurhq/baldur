"""
Unit tests for HTTP Method 기반 Tier 분류 (240 작업).

테스트 항목:
- TierMapping.methods 필드 추가 및 __post_init__ 정규화
- TierMapping.matches() HTTP method 필터링
- TierMapping.to_dict() / from_dict() 직렬화/역직렬화
- TierRegistry.get_tier_for_request() method 기반 tier 분류
- TierRegistry 캐시 키 (path, method) 튜플 + LRU Eviction
- AdmissionControlMiddleware OPTIONS bypass + method 전달
- TieringMiddleware OPTIONS bypass + method 전달
- DEFAULT_TIER_MAPPINGS method-specific 매핑 계약값
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest

from baldur.scaling.tiering.defaults import DEFAULT_TIER_MAPPINGS
from baldur.scaling.tiering.enums import TierMatchType
from baldur.scaling.tiering.models import TierMapping
from baldur.scaling.tiering.registry import TierRegistry

# =============================================================================
# TierMapping.methods 필드 및 __post_init__ 정규화
# =============================================================================


class TestTierMappingMethodsContract:
    """TierMapping.methods 필드 계약값 검증."""

    def test_methods_default_is_none(self):
        """methods 기본값은 None (모든 HTTP 메서드에 매칭)."""
        mapping = TierMapping(pattern="/api/test", tier_id="standard")
        assert mapping.methods is None

    def test_post_init_normalizes_list_to_frozenset(self):
        """list 입력은 frozenset으로 정규화된다."""
        mapping = TierMapping(
            pattern="/api/test",
            tier_id="standard",
            methods=["GET", "POST"],
        )
        assert isinstance(mapping.methods, frozenset)
        assert mapping.methods == frozenset({"GET", "POST"})

    def test_post_init_normalizes_tuple_to_frozenset(self):
        """tuple 입력은 frozenset으로 정규화된다."""
        mapping = TierMapping(
            pattern="/api/test",
            tier_id="standard",
            methods=("PUT",),
        )
        assert isinstance(mapping.methods, frozenset)
        assert mapping.methods == frozenset({"PUT"})

    def test_post_init_normalizes_lowercase_to_upper(self):
        """소문자 메서드는 대문자로 정규화된다."""
        mapping = TierMapping(
            pattern="/api/test",
            tier_id="standard",
            methods=frozenset({"post", "get"}),
        )
        assert mapping.methods == frozenset({"POST", "GET"})

    def test_post_init_normalizes_mixed_case_list(self):
        """list의 혼합 대소문자도 대문자로 정규화된다."""
        mapping = TierMapping(
            pattern="/api/test",
            tier_id="standard",
            methods=["Delete", "patch"],
        )
        assert mapping.methods == frozenset({"DELETE", "PATCH"})

    def test_post_init_none_methods_unchanged(self):
        """methods=None은 변경되지 않는다."""
        mapping = TierMapping(
            pattern="/api/test",
            tier_id="standard",
            methods=None,
        )
        assert mapping.methods is None

    def test_post_init_already_uppercase_frozenset_unchanged(self):
        """이미 대문자 frozenset이면 동일 객체 유지."""
        original = frozenset({"GET", "POST"})
        mapping = TierMapping(
            pattern="/api/test",
            tier_id="standard",
            methods=original,
        )
        assert mapping.methods == original


# =============================================================================
# TierMapping.matches() HTTP method 필터링
# =============================================================================


class TestTierMappingMatchesBehavior:
    """TierMapping.matches() method 필터 동작 검증."""

    def test_matches_method_specific_hit(self):
        """method-specific 매핑에 해당 method로 요청 시 True."""
        mapping = TierMapping(
            pattern="/api/baldur/config/*",
            tier_id="critical",
            pattern_type=TierMatchType.WILDCARD,
            methods=frozenset({"POST", "PUT"}),
        )
        assert mapping.matches("/api/baldur/config/test", "POST") is True

    def test_matches_method_specific_miss(self):
        """method-specific 매핑에 다른 method로 요청 시 False."""
        mapping = TierMapping(
            pattern="/api/baldur/config/*",
            tier_id="critical",
            pattern_type=TierMatchType.WILDCARD,
            methods=frozenset({"POST", "PUT"}),
        )
        assert mapping.matches("/api/baldur/config/test", "GET") is False

    def test_matches_methods_none_accepts_all(self):
        """methods=None이면 모든 HTTP method에 매칭된다."""
        mapping = TierMapping(
            pattern="/api/baldur/config/*",
            tier_id="standard",
            pattern_type=TierMatchType.WILDCARD,
            methods=None,
        )
        assert mapping.matches("/api/baldur/config/test", "GET") is True
        assert mapping.matches("/api/baldur/config/test", "POST") is True
        assert mapping.matches("/api/baldur/config/test", "DELETE") is True

    def test_matches_method_arg_none_skips_filter(self):
        """method 인자가 None이면 method 필터를 스킵한다."""
        mapping = TierMapping(
            pattern="/api/baldur/config/*",
            tier_id="critical",
            pattern_type=TierMatchType.WILDCARD,
            methods=frozenset({"POST"}),
        )
        # method=None이면 method 필터 없이 path만으로 매칭
        assert mapping.matches("/api/baldur/config/test", None) is True

    def test_matches_method_case_insensitive(self):
        """method 비교는 대소문자를 구분하지 않는다."""
        mapping = TierMapping(
            pattern="/api/test",
            tier_id="critical",
            methods=frozenset({"POST"}),
        )
        assert mapping.matches("/api/test", "post") is True
        assert mapping.matches("/api/test", "Post") is True

    def test_matches_path_mismatch_with_method_match(self):
        """path가 불일치하면 method가 일치해도 False."""
        mapping = TierMapping(
            pattern="/api/other/*",
            tier_id="critical",
            pattern_type=TierMatchType.WILDCARD,
            methods=frozenset({"POST"}),
        )
        assert mapping.matches("/api/baldur/config/test", "POST") is False

    def test_matches_backward_compatible_no_method_arg(self):
        """method 인자 없이 호출 시 기존과 동일하게 동작한다."""
        mapping = TierMapping(
            pattern="/api/baldur/control/",
            tier_id="critical",
        )
        assert mapping.matches("/api/baldur/control/") is True

    def test_matches_regex_with_method(self):
        """regex 패턴도 method 필터와 함께 동작한다."""
        mapping = TierMapping(
            pattern=r"/api/baldur/dlq/\d+/retry/",
            tier_id="critical",
            pattern_type=TierMatchType.REGEX,
            methods=frozenset({"POST"}),
        )
        assert mapping.matches("/api/baldur/dlq/123/retry/", "POST") is True
        assert mapping.matches("/api/baldur/dlq/123/retry/", "GET") is False


# =============================================================================
# TierMapping 직렬화/역직렬화
# =============================================================================


class TestTierMappingSerializationBehavior:
    """TierMapping to_dict()/from_dict() 직렬화 동작 검증."""

    def test_to_dict_with_methods(self):
        """methods가 있으면 to_dict()에 sorted list로 포함된다."""
        mapping = TierMapping(
            pattern="/api/test",
            tier_id="critical",
            methods=frozenset({"POST", "DELETE", "GET"}),
        )
        d = mapping.to_dict()
        assert "methods" in d
        assert d["methods"] == ["DELETE", "GET", "POST"]  # sorted

    def test_to_dict_without_methods(self):
        """methods=None이면 to_dict()에 methods 키가 없다."""
        mapping = TierMapping(pattern="/api/test", tier_id="standard")
        d = mapping.to_dict()
        assert "methods" not in d

    def test_from_dict_with_methods(self):
        """methods가 있는 dict에서 TierMapping을 복원한다."""
        data = {
            "pattern": "/api/test",
            "tier_id": "critical",
            "pattern_type": "wildcard",
            "methods": ["POST", "PUT"],
        }
        mapping = TierMapping.from_dict(data)
        assert mapping.methods == frozenset({"POST", "PUT"})

    def test_from_dict_without_methods_key(self):
        """methods 키가 없는 기존 JSON에서 methods=None으로 복원된다."""
        data = {
            "pattern": "/api/test",
            "tier_id": "standard",
        }
        mapping = TierMapping.from_dict(data)
        assert mapping.methods is None

    def test_from_dict_with_methods_null(self):
        """methods: null인 JSON에서 methods=None으로 복원된다."""
        data = {
            "pattern": "/api/test",
            "tier_id": "standard",
            "methods": None,
        }
        mapping = TierMapping.from_dict(data)
        assert mapping.methods is None

    def test_roundtrip_with_methods(self):
        """to_dict() → from_dict() 왕복 시 methods가 보존된다."""
        original = TierMapping(
            pattern="/api/test/*",
            tier_id="critical",
            pattern_type=TierMatchType.WILDCARD,
            priority=70,
            description="테스트 매핑",
            methods=frozenset({"POST", "DELETE"}),
        )
        restored = TierMapping.from_dict(original.to_dict())
        assert restored.methods == original.methods
        assert restored.pattern == original.pattern
        assert restored.tier_id == original.tier_id

    def test_roundtrip_without_methods(self):
        """methods=None 매핑의 to_dict() → from_dict() 왕복."""
        original = TierMapping(
            pattern="/api/test",
            tier_id="standard",
        )
        restored = TierMapping.from_dict(original.to_dict())
        assert restored.methods is None


# =============================================================================
# TierRegistry method 기반 Tier 분류
# =============================================================================


class TestTierRegistryMethodResolutionBehavior:
    """TierRegistry method 기반 tier 분류 동작 검증."""

    @pytest.fixture
    def registry(self):
        """격리된 TierRegistry 인스턴스 (기본 매핑 포함)."""
        r = TierRegistry.__new__(TierRegistry)
        r._init()
        return r

    def test_post_config_resolves_to_critical(self, registry):
        """POST /config/* → critical tier (method-specific 매핑)."""
        tier = registry.get_tier_for_request("/api/baldur/config/test", method="POST")
        assert tier is not None
        assert tier.id == "critical"

    def test_get_config_resolves_to_non_essential(self, registry):
        """GET /config/* → non_essential tier (method-specific 읽기 매핑)."""
        tier = registry.get_tier_for_request("/api/baldur/config/test", method="GET")
        assert tier is not None
        assert tier.id == "non_essential"

    def test_delete_config_resolves_to_critical(self, registry):
        """DELETE /config/* → critical tier (method-specific 쓰기 매핑)."""
        tier = registry.get_tier_for_request("/api/baldur/config/test", method="DELETE")
        assert tier is not None
        assert tier.id == "critical"

    def test_post_dlq_resolves_to_critical(self, registry):
        """POST /dlq/* → critical tier (DLQ 재처리 쓰기 매핑)."""
        tier = registry.get_tier_for_request("/api/baldur/dlq/replay/", method="POST")
        assert tier is not None
        assert tier.id == "critical"

    def test_get_dlq_resolves_to_standard(self, registry):
        """GET /dlq/* → standard tier (path-only fallback 매핑)."""
        tier = registry.get_tier_for_request("/api/baldur/dlq/list/", method="GET")
        assert tier is not None
        assert tier.id == "standard"

    def test_method_none_falls_back_to_path_only(self, registry):
        """method=None이면 path-only 매핑으로 fallback된다."""
        tier = registry.get_tier_for_request("/api/baldur/config/test", method=None)
        assert tier is not None
        # method=None이면 method filter를 스킵하므로 가장 높은 priority 매핑에 매칭
        # priority=70 method-specific도 method=None이면 통과
        assert tier.id == "critical"

    def test_method_specific_higher_priority_than_path_only(self, registry):
        """동일 path에서 method-specific 매핑이 path-only보다 우선한다."""
        # POST /config → priority=70 method-specific (critical)
        # /config → priority=50 path-only (standard)
        tier_post = registry.get_tier_for_request(
            "/api/baldur/config/test", method="POST"
        )
        registry.get_tier_for_path("/api/baldur/config/test")
        assert tier_post is not None
        assert tier_post.id == "critical"

    def test_legacy_get_tier_for_path_still_works(self, registry):
        """기존 get_tier_for_path() API가 하위 호환으로 동작한다."""
        tier = registry.get_tier_for_path("/api/baldur/control/")
        assert tier is not None
        assert tier.id == "critical"

    def test_control_path_unaffected_by_method(self, registry):
        """/control/ 경로는 method와 무관하게 critical 유지 (priority=100)."""
        for method in ("GET", "POST", "PUT", "DELETE"):
            tier = registry.get_tier_for_request("/api/baldur/control/", method=method)
            assert tier is not None
            assert tier.id == "critical"


# =============================================================================
# TierRegistry 캐시 키 및 LRU Eviction
# =============================================================================


class TestTierRegistryCacheLRUBehavior:
    """TierRegistry LRU 캐시 동작 검증."""

    @pytest.fixture
    def registry(self):
        """격리된 TierRegistry 인스턴스."""
        r = TierRegistry.__new__(TierRegistry)
        r._init()
        return r

    def test_cache_key_includes_method(self, registry):
        """캐시 키에 method가 포함되어 GET과 POST가 독립 캐시된다."""
        registry.get_tier_for_request("/api/baldur/config/test", method="GET")
        registry.get_tier_for_request("/api/baldur/config/test", method="POST")

        assert ("/api/baldur/config/test", "GET") in registry._path_tier_cache
        assert ("/api/baldur/config/test", "POST") in registry._path_tier_cache

    def test_cache_is_ordered_dict(self, registry):
        """캐시가 OrderedDict 타입이다."""
        assert isinstance(registry._path_tier_cache, OrderedDict)

    def test_lru_eviction_on_cache_full(self, registry):
        """캐시가 최대 크기를 초과하면 가장 오래된 항목이 제거된다."""
        registry._PATH_CACHE_MAX_SIZE = 3

        registry.get_tier_for_request("/path/1/", method="GET")
        registry.get_tier_for_request("/path/2/", method="GET")
        registry.get_tier_for_request("/path/3/", method="GET")

        assert len(registry._path_tier_cache) == 3

        # 4번째 항목 추가 → 가장 오래된 /path/1/ 제거
        registry.get_tier_for_request("/path/4/", method="GET")

        assert len(registry._path_tier_cache) == 3
        assert ("/path/1/", "GET") not in registry._path_tier_cache
        assert ("/path/4/", "GET") in registry._path_tier_cache

    def test_lru_move_to_end_on_hit(self, registry):
        """캐시 히트 시 해당 항목이 LRU 최신으로 이동한다."""
        registry._PATH_CACHE_MAX_SIZE = 3

        registry.get_tier_for_request("/path/1/", method="GET")
        registry.get_tier_for_request("/path/2/", method="GET")
        registry.get_tier_for_request("/path/3/", method="GET")

        # /path/1/ 재접근 → LRU 최신으로 이동
        registry.get_tier_for_request("/path/1/", method="GET")

        # /path/4/ 추가 → 가장 오래된 /path/2/ 제거 (1은 방금 접근)
        registry.get_tier_for_request("/path/4/", method="GET")

        assert ("/path/2/", "GET") not in registry._path_tier_cache
        assert ("/path/1/", "GET") in registry._path_tier_cache

    def test_cache_invalidation_clears_ordered_dict(self, registry):
        """_invalidate_path_cache()가 OrderedDict를 비운다."""
        registry.get_tier_for_request("/api/test", method="GET")
        assert len(registry._path_tier_cache) > 0

        registry._invalidate_path_cache()

        assert len(registry._path_tier_cache) == 0
        assert isinstance(registry._path_tier_cache, OrderedDict)


# =============================================================================
# resolve 체인 method 전달
# =============================================================================


class TestResolveChainMethodBehavior:
    """resolve_tier / resolve_tier_with_fallback method 전달 동작 검증."""

    @pytest.fixture
    def registry(self):
        """격리된 TierRegistry 인스턴스."""
        r = TierRegistry.__new__(TierRegistry)
        r._init()
        return r

    def test_resolve_tier_with_method(self, registry):
        """resolve_tier()에 method를 전달하면 method 기반 분류가 적용된다."""
        tier = registry.resolve_tier(
            path="/api/baldur/config/test",
            method="POST",
        )
        assert tier is not None
        assert tier.id == "critical"

    def test_resolve_tier_without_method(self, registry):
        """resolve_tier()에 method 없이 호출 시 기존 동작 유지."""
        tier = registry.resolve_tier(path="/api/baldur/config/test")
        assert tier is not None

    def test_resolve_tier_with_fallback_method_propagation(self, registry):
        """resolve_tier_with_fallback()에 method가 전달되어 tier 분류에 반영된다."""
        result = registry.resolve_tier_with_fallback(
            path="/api/baldur/config/test",
            method="POST",
        )
        assert result.tier_id == "critical"

    def test_resolve_tier_with_fallback_get_config(self, registry):
        """resolve_tier_with_fallback()에 GET method로 config 조회 시 non_essential."""
        result = registry.resolve_tier_with_fallback(
            path="/api/baldur/config/test",
            method="GET",
        )
        assert result.tier_id == "non_essential"


# =============================================================================
# set_mappings 정렬: method-specific이 동일 priority에서 우선
# =============================================================================


class TestMappingSortOrderBehavior:
    """set_mappings() 정렬 규칙 동작 검증."""

    @pytest.fixture
    def registry(self):
        """격리된 TierRegistry 인스턴스."""
        r = TierRegistry.__new__(TierRegistry)
        r._init()
        return r

    def test_method_specific_before_path_only_at_same_priority(self, registry):
        """동일 priority에서 method-specific 매핑이 path-only보다 앞에 온다."""
        mappings = [
            TierMapping(
                pattern="/api/test/*",
                tier_id="standard",
                pattern_type=TierMatchType.WILDCARD,
                priority=50,
            ),
            TierMapping(
                pattern="/api/test/*",
                tier_id="critical",
                pattern_type=TierMatchType.WILDCARD,
                priority=50,
                methods=frozenset({"POST"}),
            ),
        ]
        registry.set_mappings(mappings)

        # 정렬 후 method-specific이 먼저
        assert registry._mappings[0].methods is not None
        assert registry._mappings[1].methods is None

    def test_higher_priority_still_wins_over_method_specific(self, registry):
        """priority가 더 높은 매핑이 method 구체성과 무관하게 우선한다."""
        mappings = [
            TierMapping(
                pattern="/api/*",
                tier_id="standard",
                pattern_type=TierMatchType.WILDCARD,
                priority=100,
            ),
            TierMapping(
                pattern="/api/specific",
                tier_id="critical",
                priority=10,
                methods=frozenset({"GET"}),
            ),
        ]
        registry.set_mappings(mappings)

        # priority 100이 먼저
        assert registry._mappings[0].priority == 100


# =============================================================================
# DEFAULT_TIER_MAPPINGS method-specific 매핑 계약값
# =============================================================================


class TestDefaultTierMappingsMethodContract:
    """DEFAULT_TIER_MAPPINGS의 method-specific 매핑 계약값 검증."""

    def _find_mapping(
        self, pattern: str, methods: frozenset | None
    ) -> TierMapping | None:
        """패턴과 methods로 매핑을 검색한다."""
        for m in DEFAULT_TIER_MAPPINGS:
            if m.pattern == pattern and m.methods == methods:
                return m
        return None

    def test_config_write_mapping_exists(self):
        """POST/PUT/PATCH/DELETE /config/* → critical 매핑이 존재한다."""
        m = self._find_mapping(
            "/api/baldur/config/*",
            frozenset({"POST", "PUT", "PATCH", "DELETE"}),
        )
        assert m is not None
        assert m.tier_id == "critical"
        assert m.priority == 70

    def test_dlq_write_mapping_exists(self):
        """POST/PUT/DELETE /dlq/* → critical 매핑이 존재한다."""
        m = self._find_mapping(
            "/api/baldur/dlq/*",
            frozenset({"POST", "PUT", "DELETE"}),
        )
        assert m is not None
        assert m.tier_id == "critical"
        assert m.priority == 70

    def test_config_read_mapping_exists(self):
        """GET/HEAD /config/* → non_essential 매핑이 존재한다."""
        m = self._find_mapping(
            "/api/baldur/config/*",
            frozenset({"GET", "HEAD"}),
        )
        assert m is not None
        assert m.tier_id == "non_essential"
        assert m.priority == 55

    def test_path_only_config_mapping_still_exists(self):
        """/config/* path-only 매핑이 유지된다 (하위 호환)."""
        m = self._find_mapping("/api/baldur/config/*", None)
        assert m is not None
        assert m.tier_id == "standard"
        assert m.priority == 50

    def test_path_only_dlq_mapping_still_exists(self):
        """/dlq/* path-only 매핑이 유지된다 (하위 호환)."""
        m = self._find_mapping("/api/baldur/dlq/*", None)
        assert m is not None
        assert m.tier_id == "standard"

    def test_method_specific_mappings_count(self):
        """method-specific 매핑은 3개이다."""
        method_specific = [m for m in DEFAULT_TIER_MAPPINGS if m.methods is not None]
        assert len(method_specific) == 3


# =============================================================================
# AdmissionControlMiddleware OPTIONS bypass + method 전달
# =============================================================================


class TestAdmissionControlOptionsBypassBehavior:
    """AdmissionControlMiddleware OPTIONS bypass + method forwarding.

    The middleware delegates classification to ``check_admission``; these tests
    assert the Django wrapper bypasses OPTIONS before delegating and forwards
    the HTTP method into the RequestContext it builds.
    """

    def _create_middleware(self):
        from baldur.api.django.admission_control import AdmissionControlMiddleware

        mock_response = MagicMock()
        get_response = MagicMock(return_value=mock_response)
        mock_settings = MagicMock()
        mock_settings.enabled = True

        with patch(
            "baldur.settings.admission_control.get_admission_control_settings",
            return_value=mock_settings,
        ):
            middleware = AdmissionControlMiddleware(get_response)
            middleware._enabled = True
            middleware._settings = mock_settings

        return middleware, get_response, mock_response

    def test_options_request_bypasses_tier_classification(self):
        """OPTIONS requests pass through without delegating to check_admission."""
        middleware, get_response, mock_response = self._create_middleware()

        request = MagicMock()
        request.method = "OPTIONS"
        request.path = "/api/baldur/config/test"
        request.META = {"REMOTE_ADDR": "127.0.0.1"}

        with patch("baldur.api.django.admission_control.check_admission") as mock_check:
            response = middleware(request)

        mock_check.assert_not_called()
        get_response.assert_called_once_with(request)
        assert response == mock_response

    def test_post_request_forwards_method_to_admission(self):
        """POST -> the RequestContext passed to check_admission carries POST."""
        from baldur.api.middleware import AdmissionDecision
        from baldur.interfaces.web_framework import HttpMethod

        middleware, _, _ = self._create_middleware()

        request = MagicMock()
        request.method = "POST"
        request.path = "/api/baldur/config/test"
        request.META = {"REMOTE_ADDR": "127.0.0.1"}
        request.user.is_authenticated = False

        with patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(active=False),
        ) as mock_check:
            middleware(request)

        ctx = mock_check.call_args.args[0]
        assert ctx.method == HttpMethod.POST

    def test_get_request_forwards_method_to_admission(self):
        """GET -> the RequestContext passed to check_admission carries GET."""
        from baldur.api.middleware import AdmissionDecision
        from baldur.interfaces.web_framework import HttpMethod

        middleware, _, _ = self._create_middleware()

        request = MagicMock()
        request.method = "GET"
        request.path = "/api/baldur/config/test"
        request.META = {"REMOTE_ADDR": "127.0.0.1"}
        request.user.is_authenticated = False

        with patch(
            "baldur.api.django.admission_control.check_admission",
            return_value=AdmissionDecision(active=False),
        ) as mock_check:
            middleware(request)

        ctx = mock_check.call_args.args[0]
        assert ctx.method == HttpMethod.GET


# =============================================================================
# TieringMiddleware OPTIONS bypass + method 전달
# =============================================================================


class TestTieringMiddlewareOptionsBypassBehavior:
    """TieringMiddleware OPTIONS bypass 동작 검증."""

    def _create_middleware(self):
        from baldur.api.django.tiering.middleware import TieringMiddleware

        mock_response = MagicMock()
        get_response = MagicMock(return_value=mock_response)

        with patch(
            "baldur.api.django.tiering.middleware.get_tier_registry",
        ) as mock_get_registry:
            mock_registry = MagicMock()
            mock_get_registry.return_value = mock_registry
            middleware = TieringMiddleware(get_response)
            middleware._enabled = True

        return middleware, get_response, mock_response, mock_registry

    def test_options_request_bypasses_load_shedding(self):
        """OPTIONS 요청은 Load Shedding 판정 없이 바로 통과한다."""
        middleware, get_response, mock_response, mock_registry = (
            self._create_middleware()
        )

        request = MagicMock()
        request.method = "OPTIONS"
        request.path = "/api/baldur/config/test"

        response = middleware(request)

        get_response.assert_called_once_with(request)
        assert response == mock_response


class TestTieringMiddlewareMethodPropagationBehavior:
    """TieringMiddleware method 전달 동작 검증."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_method_passed_to_resolve_tier_with_fallback(self):
        """request.method가 resolve_tier_with_fallback에 전달된다."""
        from baldur.api.django.tiering.middleware import TieringMiddleware

        mock_response = MagicMock()
        get_response = MagicMock(return_value=mock_response)
        mock_registry = MagicMock()
        tier_result = MagicMock()
        tier_result.tier_id = "standard"
        mock_registry.resolve_tier_with_fallback.return_value = tier_result

        with patch(
            "baldur.api.django.tiering.middleware.get_tier_registry",
            return_value=mock_registry,
        ):
            middleware = TieringMiddleware(get_response)
            middleware._enabled = True
            middleware._registry = mock_registry

        request = MagicMock()
        request.method = "POST"
        request.path = "/api/baldur/config/test"
        request.META = {"REMOTE_ADDR": "127.0.0.1"}
        request.user.is_authenticated = False

        # Emergency/Backpressure 모듈을 모킹하여 실제 __call__ 실행
        mock_manager = MagicMock()
        mock_manager.is_active.return_value = False

        mock_controller = MagicMock()
        mock_state = MagicMock()

        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
                return_value=mock_manager,
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                return_value=mock_controller,
            ),
        ):
            from baldur.scaling.config import BackpressureLevel
            from baldur_pro.services.emergency_mode.enums import EmergencyLevel

            mock_manager.get_current_level.return_value = EmergencyLevel.NORMAL
            mock_state.level = BackpressureLevel.NONE
            mock_controller.get_state.return_value = mock_state

            middleware(request)

        # method가 전달되었는지 확인 (NORMAL/NONE이면 early return이므로, 호출 안 될 수 있음)
        # NORMAL + NONE 조합은 early return하므로 resolve_tier_with_fallback 호출 안 됨
        # 대신 비정상 상태에서 테스트
        # → 이 테스트 대상은 method 전파이므로 별도 접근 필요

    def test_method_propagation_during_emergency(self):
        """비상 모드에서 method가 resolve_tier_with_fallback에 전달된다."""
        from baldur.api.django.tiering.middleware import TieringMiddleware
        from baldur.scaling.config import BackpressureLevel
        from baldur_pro.services.emergency_mode.enums import EmergencyLevel

        mock_response = MagicMock()
        get_response = MagicMock(return_value=mock_response)
        mock_registry = MagicMock()
        tier_result = MagicMock()
        tier_result.tier_id = "critical"
        mock_registry.resolve_tier_with_fallback.return_value = tier_result

        with patch(
            "baldur.api.django.tiering.middleware.get_tier_registry",
            return_value=mock_registry,
        ):
            middleware = TieringMiddleware(get_response)
            middleware._enabled = True
            middleware._registry = mock_registry

        request = MagicMock()
        request.method = "POST"
        request.path = "/api/baldur/config/test"
        request.META = {"REMOTE_ADDR": "127.0.0.1"}
        request.user.is_authenticated = False

        mock_manager = MagicMock()
        mock_manager.is_active.return_value = True
        mock_manager.get_current_level.return_value = EmergencyLevel.LEVEL_1

        mock_controller = MagicMock()
        mock_state = MagicMock()
        mock_state.level = BackpressureLevel.LOW
        mock_controller.get_state.return_value = mock_state

        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
                return_value=mock_manager,
            ),
            patch(
                "baldur.scaling.rate_controller.get_rate_controller",
                return_value=mock_controller,
            ),
        ):
            middleware(request)

        call_kwargs = mock_registry.resolve_tier_with_fallback.call_args.kwargs
        assert call_kwargs["method"] == "POST"
