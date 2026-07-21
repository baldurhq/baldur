"""
@domain_tag Decorator and DomainContext Unit Tests.

도메인 태깅 데코레이터 및 컨텍스트 매니저 테스트.
"""

import pytest

from baldur.decorators.domain_tag import (
    DomainContext,
    clear_domain_context,
    domain_tag,
    get_current_domain,
    set_domain_context,
)

# =============================================================================
# DomainContext Tests
# =============================================================================


class TestDomainContext:
    """DomainContext 컨텍스트 매니저 테스트."""

    def setup_method(self):
        """각 테스트 전 컨텍스트 초기화."""
        clear_domain_context()

    def teardown_method(self):
        """각 테스트 후 컨텍스트 정리."""
        clear_domain_context()

    def test_context_sets_domain(self):
        """with 블록 내에서 도메인 설정."""
        with DomainContext("payment"):
            assert get_current_domain() == "payment"

    def test_context_clears_after_exit(self):
        """with 블록 종료 후 도메인 해제."""
        with DomainContext("payment"):
            pass

        assert get_current_domain() is None

    def test_context_restores_previous(self):
        """중첩 컨텍스트에서 이전 도메인 복원."""
        with DomainContext("order"):
            assert get_current_domain() == "order"

            with DomainContext("payment"):
                assert get_current_domain() == "payment"

            assert get_current_domain() == "order"

        assert get_current_domain() is None

    def test_context_normalizes_to_lowercase(self):
        """도메인 이름이 소문자로 정규화됨."""
        with DomainContext("PAYMENT"):
            assert get_current_domain() == "payment"

        with DomainContext("Payment"):
            assert get_current_domain() == "payment"

    def test_context_exception_still_clears(self):
        """예외 발생 시에도 컨텍스트 정리."""
        try:
            with DomainContext("payment"):
                raise ValueError("Test error")
        except ValueError:
            pass

        assert get_current_domain() is None

    def test_context_returns_self(self):
        """with as 문법 지원."""
        with DomainContext("payment") as ctx:
            assert ctx.domain == "payment"


# =============================================================================
# domain_tag Decorator Tests
# =============================================================================


class TestDomainTagDecorator:
    """@domain_tag 데코레이터 테스트."""

    def setup_method(self):
        """각 테스트 전 컨텍스트 초기화."""
        clear_domain_context()

    def teardown_method(self):
        """각 테스트 후 컨텍스트 정리."""
        clear_domain_context()

    def test_sets_domain_context(self):
        """함수 실행 중 도메인 컨텍스트 설정."""
        captured_domain = None

        @domain_tag("payment")
        def process_payment():
            nonlocal captured_domain
            captured_domain = get_current_domain()
            return "done"

        result = process_payment()

        assert result == "done"
        assert captured_domain == "payment"

    def test_clears_after_function(self):
        """함수 종료 후 컨텍스트 해제."""

        @domain_tag("payment")
        def process_payment():
            return get_current_domain()

        process_payment()

        assert get_current_domain() is None

    def test_preserves_function_metadata(self):
        """함수 메타데이터(이름, docstring) 보존."""

        @domain_tag("payment")
        def my_function():
            """My docstring."""
            pass

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."

    def test_normalizes_to_lowercase(self):
        """도메인 이름이 소문자로 정규화됨."""

        @domain_tag("PAYMENT")
        def process_payment():
            return get_current_domain()

        assert process_payment() == "payment"

    def test_nested_decorators(self):
        """중첩된 데코레이터 함수 호출."""

        @domain_tag("order")
        def create_order():
            return process_payment()

        @domain_tag("payment")
        def process_payment():
            return get_current_domain()

        # create_order -> payment 컨텍스트로 변경
        result = create_order()

        assert result == "payment"

    def test_exception_still_clears(self):
        """예외 발생 시에도 컨텍스트 정리."""

        @domain_tag("payment")
        def failing_function():
            raise ValueError("Test error")

        try:
            failing_function()
        except ValueError:
            pass

        assert get_current_domain() is None

    def test_with_arguments(self):
        """인자가 있는 함수 지원."""

        @domain_tag("payment")
        def process_payment(amount: float, currency: str = "KRW"):
            return {
                "domain": get_current_domain(),
                "amount": amount,
                "currency": currency,
            }

        result = process_payment(1000.0, currency="USD")

        assert result["domain"] == "payment"
        assert result["amount"] == 1000.0
        assert result["currency"] == "USD"

    def test_with_return_value(self):
        """반환값이 있는 함수 지원."""

        @domain_tag("order")
        def create_order():
            return {"order_id": "123", "domain": get_current_domain()}

        result = create_order()

        assert result["order_id"] == "123"
        assert result["domain"] == "order"


# =============================================================================
# Async Support Tests
# =============================================================================


class TestDomainTagAsync:
    """비동기 함수 지원 테스트."""

    def setup_method(self):
        """각 테스트 전 컨텍스트 초기화."""
        clear_domain_context()

    def teardown_method(self):
        """각 테스트 후 컨텍스트 정리."""
        clear_domain_context()

    @pytest.mark.asyncio
    async def test_async_function_support(self):
        """async 함수 지원."""

        @domain_tag("payment")
        async def async_process():
            return get_current_domain()

        result = await async_process()

        assert result == "payment"

    @pytest.mark.asyncio
    async def test_async_clears_after_function(self):
        """async 함수 종료 후 컨텍스트 해제."""

        @domain_tag("payment")
        async def async_process():
            return "done"

        await async_process()

        assert get_current_domain() is None

    @pytest.mark.asyncio
    async def test_async_exception_still_clears(self):
        """async 예외 발생 시에도 컨텍스트 정리."""

        @domain_tag("payment")
        async def failing_async():
            raise ValueError("Test error")

        try:
            await failing_async()
        except ValueError:
            pass

        assert get_current_domain() is None


# =============================================================================
# Utility Functions Tests
# =============================================================================


class TestUtilityFunctions:
    """유틸리티 함수 테스트."""

    def setup_method(self):
        """각 테스트 전 컨텍스트 초기화."""
        clear_domain_context()

    def teardown_method(self):
        """각 테스트 후 컨텍스트 정리."""
        clear_domain_context()

    def test_get_current_domain_when_not_set(self):
        """도메인 미설정 시 None 반환."""
        assert get_current_domain() is None

    def test_clear_domain_context(self):
        """clear_domain_context 동작 확인."""
        with DomainContext("payment"):
            clear_domain_context()
            assert get_current_domain() is None

    def test_set_domain_context_returns_token(self):
        """set_domain_context가 토큰 반환."""
        token = set_domain_context("payment")

        assert token is not None
        assert get_current_domain() == "payment"

        clear_domain_context()

    def test_set_domain_context_with_none(self):
        """set_domain_context(None)으로 해제."""
        set_domain_context("payment")
        set_domain_context(None)

        assert get_current_domain() is None
