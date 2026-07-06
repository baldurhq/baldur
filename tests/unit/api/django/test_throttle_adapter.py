"""Throttle adapter — unit tests (523 Step 6).

Covers ``baldur.api.django.throttle_adapter`` end-to-end:
- ``AdaptiveDRFThrottle`` lazy throttle resolution + RuntimeError when the
  PRO provider is absent, config loading from Django settings + ImportError
  fallback, ``allow_request`` allowed/throttled branches, ``get_ident``
  precedence (XFF / X-Real-IP / REMOTE_ADDR / unknown), ``wait`` reset-at
  arithmetic + default, and ``finalize_response`` RTT recording.
- ``CorruptionShieldDRFValidator`` lazy shield resolution + ImportError
  fallback, ``has_permission`` valid/blocked branches with violation
  capture, ``validate_data`` field grouping + non_field fallback,
  ``_extract_data`` body+query_params merge, ``_build_context`` user +
  view-hook composition.
- ``record_response_time`` no-attr/no-provider/recording branches.
- ``mark_request_start`` attribute injection.
"""

from __future__ import annotations

import builtins
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory

from baldur.api.django.throttle_adapter import (
    AdaptiveDRFThrottle,
    CorruptionShieldDRFValidator,
    ThrottleConfig,
    mark_request_start,
    record_response_time,
)
from baldur.factory import ProviderRegistry

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def rf() -> RequestFactory:
    return RequestFactory()


@pytest.fixture
def fake_throttle() -> MagicMock:
    """MagicMock standing in for the PRO AdaptiveThrottle singleton."""
    throttle = MagicMock(name="AdaptiveThrottle")
    throttle.allow_request.return_value = SimpleNamespace(
        allowed=True,
        limit=100,
        current_count=1,
    )
    throttle.get_status.return_value = SimpleNamespace(reset_at=None)
    throttle.record_response.return_value = None
    return throttle


@pytest.fixture
def registered_throttle(monkeypatch, fake_throttle) -> MagicMock:
    """Wire fake_throttle into the ProviderRegistry slot via monkeypatch."""
    monkeypatch.setattr(
        ProviderRegistry.adaptive_throttle, "safe_get", lambda *a, **kw: fake_throttle
    )
    return fake_throttle


@pytest.fixture
def no_throttle(monkeypatch) -> None:
    """Make safe_get return None so the OSS fallback branch is exercised."""
    monkeypatch.setattr(
        ProviderRegistry.adaptive_throttle, "safe_get", lambda *a, **kw: None
    )


# =============================================================================
# AdaptiveDRFThrottle — throttle property (lazy resolution)
# =============================================================================


class TestAdaptiveDRFThrottleResolution:
    def test_lazy_resolves_from_provider_registry(self, registered_throttle):
        adapter = AdaptiveDRFThrottle()
        resolved = adapter.throttle
        assert resolved is registered_throttle

    def test_resolution_caches_instance(self, registered_throttle):
        adapter = AdaptiveDRFThrottle()
        first = adapter.throttle
        second = adapter.throttle
        assert first is second

    def test_runtime_error_when_no_pro_provider_registered(self, no_throttle):
        adapter = AdaptiveDRFThrottle()
        with pytest.raises(RuntimeError, match="baldur_pro"):
            _ = adapter.throttle

    def test_scope_attribute_is_adaptive(self):
        assert AdaptiveDRFThrottle.scope == "adaptive"


# =============================================================================
# AdaptiveDRFThrottle — _get_config
# =============================================================================


class TestAdaptiveDRFThrottleGetConfig:
    def test_returns_config_from_django_settings(self, settings):
        settings.BALDUR_THROTTLE = {"initial_limit": 250, "min_limit": 25}
        adapter = AdaptiveDRFThrottle()
        config = adapter._get_config()
        assert isinstance(config, ThrottleConfig)
        assert config.initial_limit == 250
        assert config.min_limit == 25

    def test_returns_default_config_when_setting_missing(self, settings):
        if hasattr(settings, "BALDUR_THROTTLE"):
            del settings.BALDUR_THROTTLE
        adapter = AdaptiveDRFThrottle()
        config = adapter._get_config()
        assert isinstance(config, ThrottleConfig)
        # Default ThrottleConfig (no overrides applied).
        assert config.initial_limit == ThrottleConfig().initial_limit

    def test_falls_back_to_default_when_django_unavailable(self, monkeypatch):
        adapter = AdaptiveDRFThrottle()
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "django.conf":
                raise ImportError("simulated missing django")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        config = adapter._get_config()
        assert isinstance(config, ThrottleConfig)


# =============================================================================
# AdaptiveDRFThrottle — allow_request
# =============================================================================


class TestAdaptiveDRFThrottleAllowRequest:
    def test_allowed_request_returns_true(self, rf, registered_throttle):
        adapter = AdaptiveDRFThrottle()
        request = rf.get("/x", HTTP_X_REAL_IP="10.0.0.1")
        assert adapter.allow_request(request, view=None) is True
        registered_throttle.allow_request.assert_called_once_with("10.0.0.1")
        assert adapter._request_start_time is not None

    def test_throttled_request_returns_false_and_logs(self, rf, registered_throttle):
        registered_throttle.allow_request.return_value = SimpleNamespace(
            allowed=False, limit=10, current_count=11
        )
        adapter = AdaptiveDRFThrottle()
        request = rf.get("/x", HTTP_X_REAL_IP="10.0.0.2")
        assert adapter.allow_request(request, view=None) is False


# =============================================================================
# AdaptiveDRFThrottle — get_ident
# =============================================================================


class TestAdaptiveDRFThrottleGetIdent:
    def test_prefers_first_x_forwarded_for_address(self, rf):
        adapter = AdaptiveDRFThrottle()
        request = rf.get(
            "/x", HTTP_X_FORWARDED_FOR="203.0.113.10, 10.0.0.1, 192.168.0.1"
        )
        assert adapter.get_ident(request) == "203.0.113.10"

    def test_strips_whitespace_from_x_forwarded_for_first(self, rf):
        adapter = AdaptiveDRFThrottle()
        request = rf.get("/x", HTTP_X_FORWARDED_FOR="   203.0.113.99 ")
        assert adapter.get_ident(request) == "203.0.113.99"

    def test_falls_back_to_x_real_ip(self, rf):
        adapter = AdaptiveDRFThrottle()
        request = rf.get("/x", HTTP_X_REAL_IP="198.51.100.7")
        assert adapter.get_ident(request) == "198.51.100.7"

    def test_falls_back_to_remote_addr(self, rf):
        adapter = AdaptiveDRFThrottle()
        request = rf.get("/x", REMOTE_ADDR="127.0.0.5")
        assert adapter.get_ident(request) == "127.0.0.5"

    def test_returns_unknown_when_all_identifiers_missing(self):
        adapter = AdaptiveDRFThrottle()
        request = SimpleNamespace(META={})  # no addrs at all
        assert adapter.get_ident(request) == "unknown"


# =============================================================================
# AdaptiveDRFThrottle — wait
# =============================================================================


class TestAdaptiveDRFThrottleWait:
    def test_returns_default_one_second_when_no_reset_at(self, registered_throttle):
        registered_throttle.get_status.return_value = SimpleNamespace(reset_at=None)
        adapter = AdaptiveDRFThrottle()
        assert adapter.wait() == pytest.approx(1.0)

    def test_returns_positive_wait_when_reset_in_future(self, registered_throttle):
        registered_throttle.get_status.return_value = SimpleNamespace(
            reset_at=time.time() + 10
        )
        adapter = AdaptiveDRFThrottle()
        wait = adapter.wait()
        assert wait is not None
        assert 0 < wait <= 10

    def test_clamps_negative_wait_to_zero(self, registered_throttle):
        registered_throttle.get_status.return_value = SimpleNamespace(
            reset_at=time.time() - 5
        )
        adapter = AdaptiveDRFThrottle()
        assert adapter.wait() == 0.0


# =============================================================================
# AdaptiveDRFThrottle — finalize_response
# =============================================================================


class TestAdaptiveDRFThrottleFinalizeResponse:
    def test_no_op_when_request_was_never_marked(self, rf, registered_throttle):
        adapter = AdaptiveDRFThrottle()
        adapter.finalize_response(rf.get("/x"), response=None)
        registered_throttle.record_response.assert_not_called()

    def test_records_rtt_and_resets_marker(self, rf, registered_throttle):
        adapter = AdaptiveDRFThrottle()
        adapter._request_start_time = time.time() - 0.05  # 50 ms ago
        adapter.finalize_response(rf.get("/x"), response=None)
        registered_throttle.record_response.assert_called_once()
        (rtt_ms,), _ = registered_throttle.record_response.call_args
        # Windows time.time() granularity can underflow the target by ~15us;
        # bracket loosely instead of asserting an exact lower bound.
        assert 40.0 <= rtt_ms < 1000.0
        assert adapter._request_start_time is None


# =============================================================================
# AdaptiveThrottle Protocol — PRO impl conformance (526 P7 sub-PR 1 / D5)
# =============================================================================


class TestAdaptiveThrottleProtocolConformance:
    """The DRF bridge depends on ``AdaptiveThrottle.allow_request`` /
    ``get_status`` / ``record_response`` existing on the resolved PRO
    singleton. Pre-526 these methods were absent and OSS callers would have
    raised ``AttributeError`` at runtime — this guard locks the contract.
    """

    def test_oss_protocol_declares_drf_bridge_methods(self):
        from baldur.interfaces.throttle import AdaptiveThrottle as Protocol

        assert hasattr(Protocol, "allow_request")
        assert hasattr(Protocol, "get_status")
        assert hasattr(Protocol, "record_response")

    def test_pro_adaptive_throttle_instance_satisfies_protocol(self):
        pytest.importorskip("baldur_pro")
        from baldur.interfaces.throttle import AdaptiveThrottle as Protocol
        from baldur_pro.services.throttle.adaptive.throttle import (
            AdaptiveThrottle as ProImpl,
        )
        from baldur_pro.services.throttle.config import ThrottleConfig

        instance = ProImpl(ThrottleConfig())
        try:
            assert isinstance(instance, Protocol)
        finally:
            instance.close()

    def test_pro_allow_request_returns_throttle_result(self):
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.throttle.adaptive.throttle import (
            AdaptiveThrottle as ProImpl,
        )
        from baldur_pro.services.throttle.config import ThrottleConfig, ThrottleResult

        instance = ProImpl(ThrottleConfig())
        try:
            result = instance.allow_request("test-ident-allow")
            assert isinstance(result, ThrottleResult)
            assert result.allowed is True
            assert result.current_count == 1
            assert result.limit == instance.config.initial_limit
        finally:
            instance.close()

    def test_pro_get_status_reads_without_mutating(self):
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.throttle.adaptive.throttle import (
            AdaptiveThrottle as ProImpl,
        )
        from baldur_pro.services.throttle.config import ThrottleConfig, ThrottleResult

        instance = ProImpl(ThrottleConfig())
        try:
            instance.allow_request("test-ident-status")
            first = instance.get_status("test-ident-status")
            second = instance.get_status("test-ident-status")
            assert isinstance(first, ThrottleResult)
            # Status is read-only — repeated calls must not advance the count.
            assert first.current_count == second.current_count == 1
            assert first.reset_at > 0
        finally:
            instance.close()


# =============================================================================
# CorruptionShieldDRFValidator — shield property (lazy + ImportError fallback)
# =============================================================================


class TestCorruptionShieldResolution:
    def test_returns_none_when_pro_module_missing(self, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "baldur_pro.services.corruption_shield.shield":
                raise ImportError("pro not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        validator = CorruptionShieldDRFValidator()
        assert validator.shield is None

    def test_resolves_and_caches_shield_from_pro_module(self, monkeypatch):
        fake_shield = MagicMock(name="CorruptionShield")
        fake_module = SimpleNamespace(get_corruption_shield=lambda: fake_shield)
        import sys

        monkeypatch.setitem(
            sys.modules,
            "baldur_pro.services.corruption_shield.shield",
            fake_module,
        )
        validator = CorruptionShieldDRFValidator()
        assert validator.shield is fake_shield
        # Second access does not re-import (cached).
        assert validator.shield is fake_shield


# =============================================================================
# CorruptionShieldDRFValidator — has_permission
# =============================================================================


def _wire_shield(validator, *, is_valid=True, blocked=False, violations=()):
    """Inject a fake shield directly into the cached slot."""
    validator._shield = MagicMock(name="Shield")
    validator._shield.validate.return_value = SimpleNamespace(
        is_valid=is_valid, blocked=blocked, violations=list(violations)
    )
    return validator._shield


class TestCorruptionShieldHasPermission:
    def test_permits_request_when_validation_passes(self, rf):
        validator = CorruptionShieldDRFValidator()
        _wire_shield(validator, is_valid=True, blocked=False)
        request = rf.post("/x", data={"amount": "10"})
        assert validator.has_permission(request, view=None) is True

    def test_blocks_request_and_attaches_violations(self, rf):
        validator = CorruptionShieldDRFValidator()
        violations = [
            SimpleNamespace(field="amount", layer="L1", message="too large"),
        ]
        _wire_shield(validator, is_valid=False, blocked=True, violations=violations)
        request = rf.post("/x", data={"amount": "999999"})
        assert validator.has_permission(request, view=None) is False
        assert request._corruption_violations == violations

    def test_records_violations_even_when_not_blocked(self, rf):
        validator = CorruptionShieldDRFValidator()
        violations = [
            SimpleNamespace(field="status", layer="L2", message="warn"),
        ]
        _wire_shield(validator, is_valid=False, blocked=False, violations=violations)
        request = rf.post("/x", data={"status": "weird"})
        assert validator.has_permission(request, view=None) is True
        assert request._corruption_violations == violations


# =============================================================================
# CorruptionShieldDRFValidator — validate_data
# =============================================================================


class TestCorruptionShieldValidateData:
    def test_does_not_raise_when_not_blocked(self):
        validator = CorruptionShieldDRFValidator()
        _wire_shield(validator, blocked=False)
        # Must not raise.
        validator.validate_data({"amount": 10}, context={"u": 1})

    def test_raises_validation_error_with_field_grouping(self):
        from rest_framework import serializers

        validator = CorruptionShieldDRFValidator()
        violations = [
            SimpleNamespace(field="amount", layer="L1", message="too big"),
            SimpleNamespace(field="amount", layer="L2", message="suspicious"),
            SimpleNamespace(field=None, layer="L3", message="cross-field"),
        ]
        _wire_shield(validator, blocked=True, violations=violations)
        with pytest.raises(serializers.ValidationError) as exc_info:
            validator.validate_data({"amount": 1})
        detail = exc_info.value.detail
        assert "amount" in detail
        assert "non_field_errors" in detail
        assert len(detail["amount"]) == 2
        assert "[L1]" in detail["amount"][0]


# =============================================================================
# CorruptionShieldDRFValidator — _extract_data / _build_context
# =============================================================================


class TestCorruptionShieldExtractData:
    def test_returns_empty_dict_when_request_has_no_data_or_params(self):
        validator = CorruptionShieldDRFValidator()
        request = SimpleNamespace()
        assert validator._extract_data(request) == {}

    def test_pulls_body_data_when_request_data_is_dict(self):
        validator = CorruptionShieldDRFValidator()
        request = SimpleNamespace(data={"foo": "bar", "amount": "10"})
        assert validator._extract_data(request) == {"foo": "bar", "amount": "10"}

    def test_ignores_non_dict_body_data(self):
        validator = CorruptionShieldDRFValidator()
        request = SimpleNamespace(data=["a", "b"])  # list, not dict
        assert validator._extract_data(request) == {}

    def test_pulls_whitelisted_query_params_only(self):
        validator = CorruptionShieldDRFValidator()
        request = SimpleNamespace(
            query_params={
                "amount": "5",
                "order_id": "abc",
                "status": "ok",
                "ignored": "x",
            }
        )
        extracted = validator._extract_data(request)
        assert extracted == {"amount": "5", "order_id": "abc", "status": "ok"}

    def test_merges_body_and_query_params(self):
        validator = CorruptionShieldDRFValidator()
        request = SimpleNamespace(
            data={"foo": 1},
            query_params={"amount": "12"},
        )
        assert validator._extract_data(request) == {"foo": 1, "amount": "12"}


class TestCorruptionShieldBuildContext:
    def test_empty_when_no_user_and_no_view_hook(self):
        validator = CorruptionShieldDRFValidator()
        assert validator._build_context(SimpleNamespace(), view=None) == {}

    def test_includes_user_id_when_authenticated(self):
        validator = CorruptionShieldDRFValidator()
        user = SimpleNamespace(is_authenticated=True, id=99)
        request = SimpleNamespace(user=user)
        assert validator._build_context(request, view=None) == {"user_id": 99}

    def test_skips_user_id_when_not_authenticated(self):
        validator = CorruptionShieldDRFValidator()
        user = SimpleNamespace(is_authenticated=False, id=99)
        request = SimpleNamespace(user=user)
        assert validator._build_context(request, view=None) == {}

    def test_merges_view_hook_context(self):
        validator = CorruptionShieldDRFValidator()
        view = SimpleNamespace(
            get_corruption_shield_context=lambda req: {"endpoint": "checkout"}
        )
        ctx = validator._build_context(SimpleNamespace(), view=view)
        assert ctx == {"endpoint": "checkout"}

    def test_user_and_view_hook_coexist(self):
        validator = CorruptionShieldDRFValidator()
        user = SimpleNamespace(is_authenticated=True, id=7)
        view = SimpleNamespace(
            get_corruption_shield_context=lambda req: {"endpoint": "pay"}
        )
        ctx = validator._build_context(SimpleNamespace(user=user), view=view)
        assert ctx == {"user_id": 7, "endpoint": "pay"}


# =============================================================================
# Module-level helpers — record_response_time / mark_request_start
# =============================================================================


class TestModuleHelpers:
    def test_record_response_time_no_op_when_marker_absent(self, registered_throttle):
        request = SimpleNamespace()
        record_response_time(request)
        registered_throttle.record_response.assert_not_called()

    def test_record_response_time_records_when_provider_available(
        self, registered_throttle
    ):
        request = SimpleNamespace(_throttle_start_time=time.time() - 0.02)
        record_response_time(request)
        registered_throttle.record_response.assert_called_once()
        (rtt_ms,), _ = registered_throttle.record_response.call_args
        # Windows time.time() granularity can underflow the target by ~15us.
        assert 15.0 <= rtt_ms < 1000.0

    def test_record_response_time_no_op_when_provider_missing(self, no_throttle):
        # Marker is set but PRO singleton is absent — must not raise.
        request = SimpleNamespace(_throttle_start_time=time.time())
        record_response_time(request)

    def test_mark_request_start_sets_marker(self):
        request = SimpleNamespace()
        before = time.time()
        mark_request_start(request)
        after = time.time()
        assert before <= request._throttle_start_time <= after
