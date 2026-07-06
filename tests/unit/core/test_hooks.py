"""
Test cases for Hook Registry (BypassRegistry).

Tests cover:
1. BypassRegistry singleton and thread-safety
2. Priority-based hook execution
3. Audit logging integration
4. Hook registration/unregistration
5. Statistics tracking
"""

import threading

import pytest

from baldur.core.hooks import (
    BypassRegistry,
    BypassResult,
    HookInfo,
    register_bypass_hook,
)

# =============================================================================
# Fixtures
# =============================================================================


class MockRequest:
    """Mock Django HttpRequest for testing."""

    def __init__(self, headers=None, path="/test", method="GET"):
        self._headers = headers or {}
        self.path = path
        self.method = method
        self.META = {}

        # Convert headers to META format
        for key, value in self._headers.items():
            meta_key = f"HTTP_{key.upper().replace('-', '_')}"
            self.META[meta_key] = value

    @property
    def headers(self):
        return self._headers


@pytest.fixture
def mock_request():
    """Create a mock request factory."""

    def _create(headers=None, path="/test", method="GET"):
        return MockRequest(headers=headers, path=path, method=method)

    return _create


@pytest.fixture(autouse=True)
def cleanup_registry():
    """Clear registry before and after each test."""
    BypassRegistry.clear_all()
    yield
    BypassRegistry.clear_all()


# =============================================================================
# BypassRegistry Tests
# =============================================================================


class TestBypassRegistryBasic:
    """Basic functionality tests for BypassRegistry."""

    def test_register_hook(self):
        """Test hook registration."""

        def test_hook(request):
            return True

        BypassRegistry.register(test_hook, priority=100, name="test_hook")

        hooks = BypassRegistry.get_registered_hooks()
        assert len(hooks) == 1
        assert hooks[0]["name"] == "test_hook"
        assert hooks[0]["priority"] == 100

    def test_register_duplicate_hook_skips(self):
        """Test that duplicate hook names are skipped."""

        def test_hook(request):
            return True

        BypassRegistry.register(test_hook, priority=100, name="dup_hook")
        BypassRegistry.register(test_hook, priority=200, name="dup_hook")  # Duplicate

        hooks = BypassRegistry.get_registered_hooks()
        assert len(hooks) == 1
        assert hooks[0]["priority"] == 100  # Original retained

    def test_unregister_hook(self):
        """Test hook unregistration."""

        def test_hook(request):
            return True

        BypassRegistry.register(test_hook, priority=100, name="remove_me")
        assert len(BypassRegistry.get_registered_hooks()) == 1

        removed = BypassRegistry.unregister("remove_me")
        assert removed is True
        assert len(BypassRegistry.get_registered_hooks()) == 0

    def test_unregister_nonexistent_hook(self):
        """Test unregistering a hook that doesn't exist."""
        removed = BypassRegistry.unregister("nonexistent")
        assert removed is False

    def test_clear_all(self):
        """Test clearing all hooks."""
        for i in range(5):
            BypassRegistry.register(lambda r: False, priority=i, name=f"hook_{i}")

        assert len(BypassRegistry.get_registered_hooks()) == 5

        count = BypassRegistry.clear_all()
        assert count == 5
        assert len(BypassRegistry.get_registered_hooks()) == 0


class TestBypassRegistryPriority:
    """Priority-based execution tests."""

    def test_hooks_sorted_by_priority(self):
        """Test hooks are sorted by priority (highest first)."""
        BypassRegistry.register(lambda r: False, priority=100, name="low")
        BypassRegistry.register(lambda r: False, priority=1000, name="high")
        BypassRegistry.register(lambda r: False, priority=500, name="medium")

        hooks = BypassRegistry.get_registered_hooks()
        priorities = [h["priority"] for h in hooks]

        assert priorities == [1000, 500, 100]

    def test_higher_priority_hook_runs_first(self, mock_request):
        """Test that higher priority hook triggers first."""
        call_order = []

        def low_hook(request):
            call_order.append("low")
            return False

        def high_hook(request):
            call_order.append("high")
            return True  # This should bypass

        BypassRegistry.register(low_hook, priority=100, name="low")
        BypassRegistry.register(high_hook, priority=1000, name="high")

        result = BypassRegistry.should_bypass(mock_request())

        assert result.bypassed is True
        assert result.hook_name == "high"
        assert call_order == ["high"]  # Low should not be called

    def test_first_bypassing_hook_wins(self, mock_request):
        """Test that first hook returning True wins."""

        def no_bypass(request):
            return False

        def yes_bypass(request):
            return True

        BypassRegistry.register(no_bypass, priority=1000, name="no")
        BypassRegistry.register(yes_bypass, priority=500, name="yes")

        result = BypassRegistry.should_bypass(mock_request())

        assert result.bypassed is True
        assert result.hook_name == "yes"


class TestBypassRegistryShouldBypass:
    """Tests for should_bypass method."""

    def test_no_hooks_returns_no_bypass(self, mock_request):
        """Test that no registered hooks = no bypass."""
        result = BypassRegistry.should_bypass(mock_request())

        assert result.bypassed is False
        assert result.hook_name == ""
        assert result.reason == "No bypass hook triggered"

    def test_bypass_result_contains_audit_info(self, mock_request):
        """Test BypassResult contains full audit information."""
        BypassRegistry.register(
            lambda r: True,
            priority=100,
            name="audit_test",
            description="Test audit hook",
        )

        request = mock_request(path="/api/test", method="POST")
        result = BypassRegistry.should_bypass(request)

        assert result.bypassed is True
        assert result.hook_name == "audit_test"
        assert result.reason == "Test audit hook"
        assert result.request_path == "/api/test"
        assert result.request_method == "POST"
        assert result.priority == 100
        assert result.timestamp is not None

    def test_hook_exception_continues_to_next(self, mock_request):
        """Test that hook exceptions don't block other hooks."""

        def failing_hook(request):
            raise RuntimeError("Hook error")

        def working_hook(request):
            return True

        BypassRegistry.register(failing_hook, priority=1000, name="failing")
        BypassRegistry.register(working_hook, priority=500, name="working")

        result = BypassRegistry.should_bypass(mock_request())

        assert result.bypassed is True
        assert result.hook_name == "working"

    def test_to_audit_dict(self, mock_request):
        """Test BypassResult.to_audit_dict() format."""
        BypassRegistry.register(
            lambda r: True,
            priority=500,
            name="audit_format",
            description="Test description",
        )

        result = BypassRegistry.should_bypass(mock_request(path="/test"))
        audit_dict = result.to_audit_dict()

        assert audit_dict["event_type"] == "bypass_decision"
        assert audit_dict["bypassed"] is True
        assert audit_dict["hook_name"] == "audit_format"
        assert audit_dict["priority"] == 500
        assert audit_dict["request_path"] == "/test"


class TestBypassRegistryStatistics:
    """Statistics tracking tests."""

    def test_invocation_count_tracks(self, mock_request):
        """Test that invocation count is tracked."""
        BypassRegistry.register(lambda r: False, priority=100, name="counter")

        for _ in range(5):
            BypassRegistry.should_bypass(mock_request())

        stats = BypassRegistry.get_statistics()
        assert stats["total_invocations"] == 5
        assert stats["total_bypasses"] == 0

    def test_bypass_count_tracks(self, mock_request):
        """Test that bypass count is tracked."""
        BypassRegistry.register(lambda r: True, priority=100, name="bypasser")

        for _ in range(3):
            BypassRegistry.should_bypass(mock_request())

        stats = BypassRegistry.get_statistics()
        assert stats["total_invocations"] == 3
        assert stats["total_bypasses"] == 3
        assert stats["bypass_rate"] == 1.0

    def test_bypass_rate_calculation(self, mock_request):
        """Test bypass rate calculation."""
        call_count = [0]

        def half_bypass(request):
            call_count[0] += 1
            return call_count[0] % 2 == 0  # Bypass every other call

        BypassRegistry.register(half_bypass, priority=100, name="half")

        for _ in range(10):
            BypassRegistry.should_bypass(mock_request())

        stats = BypassRegistry.get_statistics()
        assert stats["bypass_rate"] == 0.5


class TestBypassRegistryThreadSafety:
    """Thread-safety tests."""

    def test_concurrent_registration(self):
        """Test concurrent hook registration is thread-safe."""

        def register_hook(idx):
            BypassRegistry.register(lambda r: False, priority=idx, name=f"hook_{idx}")

        threads = [threading.Thread(target=register_hook, args=(i,)) for i in range(20)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        hooks = BypassRegistry.get_registered_hooks()
        assert len(hooks) == 20

    def test_concurrent_should_bypass(self, mock_request):
        """Test concurrent should_bypass calls are thread-safe."""
        BypassRegistry.register(lambda r: True, priority=100, name="concurrent")

        results = []

        def check_bypass():
            result = BypassRegistry.should_bypass(mock_request())
            results.append(result.bypassed)

        threads = [threading.Thread(target=check_bypass) for _ in range(50)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 50
        assert all(r is True for r in results)


class TestRegisterBypassHookDecorator:
    """Tests for @register_bypass_hook decorator."""

    def test_decorator_registers_hook(self, mock_request):
        """Test decorator registers the hook properly."""

        @register_bypass_hook(
            priority=777, name="decorated", description="Decorated hook"
        )
        def my_hook(request):
            return True

        hooks = BypassRegistry.get_registered_hooks()
        hook_names = [h["name"] for h in hooks]

        assert "decorated" in hook_names

        result = BypassRegistry.should_bypass(mock_request())
        assert result.bypassed is True
        assert result.hook_name == "decorated"

    def test_decorator_without_name_uses_func_name(self):
        """Test decorator uses function name if name not provided."""

        @register_bypass_hook(priority=100)
        def auto_named_hook(request):
            return False

        hooks = BypassRegistry.get_registered_hooks()
        hook_names = [h["name"] for h in hooks]

        assert "auto_named_hook" in hook_names


class TestHookInfo:
    """Tests for HookInfo dataclass."""

    def test_hook_info_call_tracks_stats(self):
        """Test calling HookInfo tracks invocation stats."""
        hook = HookInfo(
            func=lambda r: True, priority=100, name="test", description="Test hook"
        )

        request = MockRequest()

        result1 = hook(request)
        assert result1 is True
        assert hook.invocation_count == 1
        assert hook.bypass_count == 1

        # Change func to return False
        hook.func = lambda r: False
        result2 = hook(request)
        assert result2 is False
        assert hook.invocation_count == 2
        assert hook.bypass_count == 1  # Still 1


class TestBypassResult:
    """Tests for BypassResult dataclass."""

    def test_bypass_result_defaults(self):
        """Test BypassResult default values."""
        result = BypassResult(
            bypassed=True, reason="Test", hook_name="test_hook", priority=100
        )

        assert result.bypassed is True
        assert result.reason == "Test"
        assert result.request_path == ""
        assert result.request_method == ""
        assert result.timestamp is not None

    def test_bypass_result_to_audit_dict_complete(self):
        """Test to_audit_dict returns complete audit info."""
        result = BypassResult(
            bypassed=True,
            reason="Full bypass test",
            hook_name="audit_hook",
            priority=1000,
            request_path="/api/test",
            request_method="POST",
        )

        audit = result.to_audit_dict()

        assert audit["event_type"] == "bypass_decision"
        assert audit["bypassed"] is True
        assert audit["reason"] == "Full bypass test"
        assert audit["hook_name"] == "audit_hook"
        assert audit["priority"] == 1000
        assert audit["request_path"] == "/api/test"
        assert audit["request_method"] == "POST"
        assert "timestamp" in audit
