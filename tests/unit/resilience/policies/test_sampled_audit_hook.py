"""
SampledAuditHook 단위 테스트.

테스트 대상:
- resilience/policies/hooks/sampled_audit.py (SampledAuditHook)

검증 기법:
- 경계값 분석: sample_rate 0.0/1.0/-0.1/1.1
- 결정론적 샘플링: 카운터 기반 N번째 호출 검증
- 부작용 검증: on_failure/on_reject 항상 호출
- 동시성: 멀티스레드 카운터 정합성
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.resilience_policy import PolicyOutcome, PolicyResult
from baldur.resilience.policies.hooks.audit import AuditHook
from baldur.resilience.policies.hooks.sampled_audit import SampledAuditHook

# =============================================================================
# 계약 검증 — SampledAuditHook
# =============================================================================


class TestSampledAuditHookContract:
    """SampledAuditHook 계약 검증."""

    def test_inherits_from_audit_hook(self):
        """SampledAuditHook은 AuditHook의 서브클래스이다."""
        assert issubclass(SampledAuditHook, AuditHook)

    def test_default_sample_rate_is_one(self):
        """기본 sample_rate는 1.0 (100% 감사)이다."""
        hook = SampledAuditHook()
        assert hook.sample_rate == 1.0

    def test_sample_rate_boundary_zero_accepted(self):
        """sample_rate=0.0은 허용된다."""
        hook = SampledAuditHook(sample_rate=0.0)
        assert hook.sample_rate == 0.0

    def test_sample_rate_boundary_one_accepted(self):
        """sample_rate=1.0은 허용된다."""
        hook = SampledAuditHook(sample_rate=1.0)
        assert hook.sample_rate == 1.0

    def test_sample_rate_below_zero_raises(self):
        """sample_rate < 0.0이면 ValueError가 발생한다."""
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            SampledAuditHook(sample_rate=-0.1)

    def test_sample_rate_above_one_raises(self):
        """sample_rate > 1.0이면 ValueError가 발생한다."""
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            SampledAuditHook(sample_rate=1.1)

    def test_interval_for_half_rate(self):
        """sample_rate=0.5이면 _interval은 2 (2번 중 1번)이다."""
        hook = SampledAuditHook(sample_rate=0.5)
        assert hook._interval == 2

    def test_interval_for_one_percent(self):
        """sample_rate=0.01이면 _interval은 100 (100번 중 1번)이다."""
        hook = SampledAuditHook(sample_rate=0.01)
        assert hook._interval == 100

    def test_interval_for_zero_rate(self):
        """sample_rate=0.0이면 _interval은 0이다."""
        hook = SampledAuditHook(sample_rate=0.0)
        assert hook._interval == 0


# =============================================================================
# 동작 검증 — 샘플링 로직
# =============================================================================


def _make_success_result() -> PolicyResult:
    """테스트용 성공 PolicyResult 생성."""
    return PolicyResult(
        value="ok",
        outcome=PolicyOutcome.SUCCESS,
        executed_policies=["circuit_breaker"],
        total_duration_ms=1.0,
    )


class TestSampledAuditHookSamplingBehavior:
    """SampledAuditHook 샘플링 동작 검증."""

    def test_rate_one_samples_every_call(self):
        """sample_rate=1.0이면 모든 on_success 호출이 기록된다."""
        hook = SampledAuditHook(sample_rate=1.0)
        result = _make_success_result()

        with patch.object(AuditHook, "on_success") as mock_super:
            for _ in range(5):
                hook.on_success("composer", result)
            assert mock_super.call_count == 5

    def test_rate_zero_samples_nothing(self):
        """sample_rate=0.0이면 on_success가 기록되지 않는다."""
        hook = SampledAuditHook(sample_rate=0.0)
        result = _make_success_result()

        with patch.object(AuditHook, "on_success") as mock_super:
            for _ in range(10):
                hook.on_success("composer", result)
            assert mock_super.call_count == 0

    def test_rate_half_samples_every_second_call(self):
        """sample_rate=0.5이면 2번 중 1번 기록된다."""
        hook = SampledAuditHook(sample_rate=0.5)
        result = _make_success_result()

        with patch.object(AuditHook, "on_success") as mock_super:
            for _ in range(10):
                hook.on_success("composer", result)
            assert mock_super.call_count == 5

    def test_rate_tenth_samples_every_tenth_call(self):
        """sample_rate=0.1이면 10번 중 1번 기록된다."""
        hook = SampledAuditHook(sample_rate=0.1)
        result = _make_success_result()

        with patch.object(AuditHook, "on_success") as mock_super:
            for _ in range(100):
                hook.on_success("composer", result)
            assert mock_super.call_count == 10

    def test_failure_always_recorded_regardless_of_rate(self):
        """on_failure는 sample_rate=0.0에서도 항상 기록된다."""
        hook = SampledAuditHook(sample_rate=0.0)
        error = RuntimeError("fail")

        with patch.object(AuditHook, "on_failure") as mock_super:
            for _ in range(5):
                hook.on_failure("composer", error, 1)
            assert mock_super.call_count == 5

    def test_reject_always_recorded_regardless_of_rate(self):
        """on_reject는 sample_rate=0.0에서도 항상 기록된다."""
        hook = SampledAuditHook(sample_rate=0.0)

        with patch.object(AuditHook, "on_reject") as mock_super:
            for _ in range(5):
                hook.on_reject("guard", "budget_exhausted")
            assert mock_super.call_count == 5

    def test_failure_recorded_with_correct_args(self):
        """on_failure 호출 시 인자가 정확히 전달된다."""
        hook = SampledAuditHook(sample_rate=0.0)
        error = ValueError("test")

        with patch.object(AuditHook, "on_failure") as mock_super:
            hook.on_failure("cb_policy", error, 3)
            mock_super.assert_called_once_with("cb_policy", error, 3, context=None)

    def test_reject_recorded_with_correct_args(self):
        """on_reject 호출 시 인자가 정확히 전달된다."""
        hook = SampledAuditHook(sample_rate=0.0)

        with patch.object(AuditHook, "on_reject") as mock_super:
            hook.on_reject("kill_switch", "disabled")
            mock_super.assert_called_once_with("kill_switch", "disabled", context=None)


# =============================================================================
# 동작 검증 — 스레드 안전성
# =============================================================================


class TestSampledAuditHookConcurrencyBehavior:
    """SampledAuditHook 동시성 동작 검증."""

    def test_concurrent_sampling_counter_consistency(self):
        """멀티스레드 환경에서 카운터 정합성이 유지된다."""
        hook = SampledAuditHook(sample_rate=0.1)
        result = _make_success_result()
        call_count = MagicMock()
        total_calls = 1000
        threads_count = 10
        calls_per_thread = total_calls // threads_count

        def counting_success(self_arg, policy_name, res, **kwargs):
            call_count()

        with patch.object(AuditHook, "on_success", counting_success):

            def worker():
                for _ in range(calls_per_thread):
                    hook.on_success("composer", result)

            threads = [threading.Thread(target=worker) for _ in range(threads_count)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # interval=10이므로 1000번 호출 시 약 100번 기록되어야 함
        # 스레드 경쟁으로 정확히 100은 아닐 수 있으므로 범위 검증
        sampled = call_count.call_count
        assert hook._counter == total_calls
        assert 80 <= sampled <= 120
