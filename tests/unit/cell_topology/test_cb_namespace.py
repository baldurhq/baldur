"""
cb_namespace 모듈 단위 테스트.

테스트 대상: core/cb_namespace.py
- make_cell_scoped_cb_name(): Composite Key 생성
- parse_composite_cb_name(): Composite Key 파싱 + 레거시 호환
- COMPOSITE_KEY_SEPARATOR: 구분자 상수
"""

from __future__ import annotations

import pytest

from baldur.core.cb_namespace import (
    COMPOSITE_KEY_SEPARATOR,
    make_cell_scoped_cb_name,
    parse_composite_cb_name,
)

# =============================================================================
# 계약 검증 (Contract)
# =============================================================================


class TestCompositeKeyContract:
    """Composite Key 설계 계약값 검증."""

    def test_separator_is_double_colon(self):
        """COMPOSITE_KEY_SEPARATOR는 '::'이다."""
        assert COMPOSITE_KEY_SEPARATOR == "::"

    def test_make_produces_expected_format(self):
        """make_cell_scoped_cb_name은 'service::cell_id' 형식을 생성한다."""
        result = make_cell_scoped_cb_name("payment_api", "cell-3")
        assert result == "payment_api::cell-3"

    def test_parse_returns_two_element_tuple(self):
        """parse_composite_cb_name은 항상 2-tuple을 반환한다."""
        result = parse_composite_cb_name("payment_api::cell-3")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_parse_legacy_key_returns_empty_cell_id(self):
        """레거시 키(구분자 없음)는 cell_id=''로 반환된다."""
        base, cell_id = parse_composite_cb_name("payment_api")
        assert base == "payment_api"
        assert cell_id == ""


# =============================================================================
# 동작 검증 (Behavior)
# =============================================================================


class TestCompositeKeyBehavior:
    """Composite Key 생성/파싱 동작 검증."""

    def test_make_parse_roundtrip(self):
        """make로 생성한 키를 parse로 분리하면 원래 값이 복원된다."""
        service = "order_service"
        cell = "cell-7"
        composite = make_cell_scoped_cb_name(service, cell)
        parsed_service, parsed_cell = parse_composite_cb_name(composite)
        assert parsed_service == service
        assert parsed_cell == cell

    def test_make_contains_separator(self):
        """make로 생성한 키에 COMPOSITE_KEY_SEPARATOR가 포함된다."""
        result = make_cell_scoped_cb_name("svc", "cell-1")
        assert COMPOSITE_KEY_SEPARATOR in result

    def test_parse_splits_on_first_separator_only(self):
        """구분자가 여러 개면 첫 번째에서만 분리한다 (maxsplit=1)."""
        composite = f"svc{COMPOSITE_KEY_SEPARATOR}cell{COMPOSITE_KEY_SEPARATOR}extra"
        base, cell_id = parse_composite_cb_name(composite)
        assert base == "svc"
        assert cell_id == f"cell{COMPOSITE_KEY_SEPARATOR}extra"

    def test_parse_empty_string(self):
        """빈 문자열은 레거시 키로 처리된다."""
        base, cell_id = parse_composite_cb_name("")
        assert base == ""
        assert cell_id == ""

    @pytest.mark.parametrize(
        ("service_name", "cell_id"),
        [
            ("payment-api", "cell-1"),
            ("user_service", "cell-99"),
            ("api.gateway.v2", "region-us-east-1"),
        ],
    )
    def test_various_service_cell_combinations(self, service_name, cell_id):
        """다양한 서비스/셀 조합에서 라운드트립이 성공한다."""
        composite = make_cell_scoped_cb_name(service_name, cell_id)
        parsed_svc, parsed_cell = parse_composite_cb_name(composite)
        assert parsed_svc == service_name
        assert parsed_cell == cell_id
