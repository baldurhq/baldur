"""
SafeFormatDict 유틸리티의 계약값 및 동작 검증.

테스트 대상: baldur.utils.template.SafeFormatDict
"""

from __future__ import annotations

from baldur.utils.template import SafeFormatDict

# =============================================================================
# Contract Tests
# =============================================================================


class TestSafeFormatDictContract:
    """SafeFormatDict가 dict 서브클래스인지 계약 검증."""

    def test_is_dict_subclass(self):
        """SafeFormatDict는 dict의 서브클래스이다."""
        assert issubclass(SafeFormatDict, dict)

    def test_missing_key_returns_empty_string(self):
        """누락 키는 빈 문자열을 반환한다."""
        d = SafeFormatDict({"key": "value"})
        assert d["missing"] == ""


# =============================================================================
# Behavior Tests
# =============================================================================


class TestSafeFormatDictBehavior:
    """SafeFormatDict.format_map() 동작 검증."""

    def test_format_map_with_all_keys_present(self):
        """모든 키가 있으면 정상 치환된다."""
        template = "Service {service_name} in {region}"
        context = SafeFormatDict(
            {"service_name": "payment_api", "region": "ap-northeast-2"}
        )
        result = template.format_map(context)
        assert result == "Service payment_api in ap-northeast-2"

    def test_format_map_with_missing_key(self):
        """누락 키가 있으면 빈 문자열로 대체된다."""
        template = "Service {service_name} in {region}"
        context = SafeFormatDict({"service_name": "payment_api"})
        result = template.format_map(context)
        assert result == "Service payment_api in "

    def test_format_map_with_all_keys_missing(self):
        """모든 키가 없으면 빈 문자열로 대체된다."""
        template = "{a} {b} {c}"
        result = template.format_map(SafeFormatDict({}))
        assert result == "  "

    def test_format_map_preserves_non_template_text(self):
        """템플릿이 아닌 부분은 그대로 보존된다."""
        template = "No variables here"
        result = template.format_map(SafeFormatDict({}))
        assert result == "No variables here"

    def test_existing_key_access_works_normally(self):
        """존재하는 키 접근은 정상 동작한다."""
        d = SafeFormatDict({"service_name": "payment_api"})
        assert d["service_name"] == "payment_api"

    def test_format_map_with_nested_braces(self):
        """중첩되지 않은 중괄호 변수가 올바르게 치환된다."""
        template = "CB OPEN — {service_name} 트래픽 차단"
        context = SafeFormatDict({"service_name": "payment"})
        result = template.format_map(context)
        assert result == "CB OPEN — payment 트래픽 차단"


class TestSafeFormatDictImmutabilityBehavior:
    """SafeFormatDict 원본 데이터 불변성 검증."""

    def test_original_dict_not_mutated_by_missing_key(self):
        """누락 키 접근이 원본 dict에 영향을 주지 않는다."""
        original = {"key": "value"}
        d = SafeFormatDict(original)
        _ = d["missing"]
        assert "missing" not in d
        assert "missing" not in original
