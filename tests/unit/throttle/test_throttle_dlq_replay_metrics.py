"""
Throttle DLQ Replay Integration Prometheus 메트릭 등록 단위 테스트.

테스트 대상: baldur.services.metrics.definitions
  - 9개 DLQ Replay 관련 메트릭 등록 및 라벨 검증

메트릭 목록:
1. throttle_rejection_dlq_stored_total (Counter, labels: reason, domain)
2. throttle_recovery_replay_total (Counter, labels: domain, result)
3. throttle_replay_delay_seconds (Histogram, labels: domain)
4. throttle_rejection_sampled_out_total (Counter, labels: tier_id, reason)
5. throttle_rejection_hedged_skipped_total (Counter, labels: domain)
6. throttle_replay_ttl_expired_total (Counter, labels: domain)
7. throttle_replay_permanently_failed_total (Counter, labels: domain)
8. throttle_replay_adaptive_interval_ms (Gauge, labels: service)
9. throttle_dlq_fallback_total (Counter, labels: channel)
"""

import pytest

from baldur.services.metrics import definitions


class TestThrottleRejectionDlqStoredTotal:
    """throttle_rejection_dlq_stored_total 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_rejection_dlq_stored_total이 존재한다."""
        assert hasattr(definitions, "throttle_rejection_dlq_stored_total")
        assert definitions.throttle_rejection_dlq_stored_total is not None

    def test_labels_reason_and_domain(self):
        """reason, domain 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_rejection_dlq_stored_total
        labeled = metric.labels(reason="capacity_exceeded", domain="payment")
        assert labeled is not None


class TestThrottleRecoveryReplayTotal:
    """throttle_recovery_replay_total 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_recovery_replay_total이 존재한다."""
        assert hasattr(definitions, "throttle_recovery_replay_total")
        assert definitions.throttle_recovery_replay_total is not None

    def test_labels_domain_and_result(self):
        """domain, result 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_recovery_replay_total
        labeled = metric.labels(domain="throttle_rejection", result="succeeded")
        assert labeled is not None


class TestThrottleReplayDelaySeconds:
    """throttle_replay_delay_seconds 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_replay_delay_seconds가 존재한다."""
        assert hasattr(definitions, "throttle_replay_delay_seconds")
        assert definitions.throttle_replay_delay_seconds is not None

    def test_label_domain(self):
        """domain 라벨로 observe 호출이 가능하다."""
        metric = definitions.throttle_replay_delay_seconds
        labeled = metric.labels(domain="payment")
        labeled.observe(30.0)


class TestThrottleRejectionSampledOutTotal:
    """throttle_rejection_sampled_out_total 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_rejection_sampled_out_total이 존재한다."""
        assert hasattr(definitions, "throttle_rejection_sampled_out_total")
        assert definitions.throttle_rejection_sampled_out_total is not None

    def test_labels_tier_id_and_reason(self):
        """tier_id, reason 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_rejection_sampled_out_total
        labeled = metric.labels(tier_id="non_essential", reason="non_essential")
        assert labeled is not None


class TestThrottleRejectionHedgedSkippedTotal:
    """throttle_rejection_hedged_skipped_total 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_rejection_hedged_skipped_total이 존재한다."""
        assert hasattr(definitions, "throttle_rejection_hedged_skipped_total")
        assert definitions.throttle_rejection_hedged_skipped_total is not None

    def test_label_domain(self):
        """domain 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_rejection_hedged_skipped_total
        labeled = metric.labels(domain="test")
        assert labeled is not None


class TestThrottleReplayTtlExpiredTotal:
    """throttle_replay_ttl_expired_total 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_replay_ttl_expired_total이 존재한다."""
        assert hasattr(definitions, "throttle_replay_ttl_expired_total")
        assert definitions.throttle_replay_ttl_expired_total is not None

    def test_label_domain(self):
        """domain 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_replay_ttl_expired_total
        labeled = metric.labels(domain="throttle_rejection")
        assert labeled is not None


class TestThrottleReplayPermanentlyFailedTotal:
    """throttle_replay_permanently_failed_total 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_replay_permanently_failed_total이 존재한다."""
        assert hasattr(definitions, "throttle_replay_permanently_failed_total")
        assert definitions.throttle_replay_permanently_failed_total is not None

    def test_label_domain(self):
        """domain 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_replay_permanently_failed_total
        labeled = metric.labels(domain="throttle_rejection")
        assert labeled is not None


class TestThrottleReplayAdaptiveIntervalMs:
    """throttle_replay_adaptive_interval_ms 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_replay_adaptive_interval_ms가 존재한다."""
        assert hasattr(definitions, "throttle_replay_adaptive_interval_ms")
        assert definitions.throttle_replay_adaptive_interval_ms is not None

    def test_label_service_and_set_value(self):
        """service 라벨로 Gauge set 호출이 가능하다."""
        metric = definitions.throttle_replay_adaptive_interval_ms
        labeled = metric.labels(service="test-service")
        labeled.set(200.0)


class TestThrottleDlqFallbackTotal:
    """throttle_dlq_fallback_total 메트릭 등록 테스트."""

    def test_metric_exists_in_definitions(self):
        """definitions 모듈에 throttle_dlq_fallback_total이 존재한다."""
        assert hasattr(definitions, "throttle_dlq_fallback_total")
        assert definitions.throttle_dlq_fallback_total is not None

    def test_label_channel_disk_persistent_buffer(self):
        """channel=disk_persistent_buffer 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_dlq_fallback_total
        labeled = metric.labels(channel="disk_persistent_buffer")
        assert labeled is not None

    def test_label_channel_jsonl(self):
        """channel=jsonl 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_dlq_fallback_total
        labeled = metric.labels(channel="jsonl")
        assert labeled is not None

    def test_label_channel_stderr(self):
        """channel=stderr 라벨로 메트릭 사용 가능하다."""
        metric = definitions.throttle_dlq_fallback_total
        labeled = metric.labels(channel="stderr")
        assert labeled is not None


class TestAllNineMetricsRegistered:
    """9개 DLQ Replay 메트릭 전체 등록 확인 테스트."""

    EXPECTED_METRIC_NAMES = [
        "throttle_rejection_dlq_stored_total",
        "throttle_recovery_replay_total",
        "throttle_replay_delay_seconds",
        "throttle_rejection_sampled_out_total",
        "throttle_rejection_hedged_skipped_total",
        "throttle_replay_ttl_expired_total",
        "throttle_replay_permanently_failed_total",
        "throttle_replay_adaptive_interval_ms",
        "throttle_dlq_fallback_total",
    ]

    @pytest.mark.parametrize("metric_name", EXPECTED_METRIC_NAMES)
    def test_metric_registered_in_definitions(self, metric_name):
        """각 메트릭이 definitions 모듈에 등록되어 있다."""
        assert hasattr(definitions, metric_name), (
            f"definitions 모듈에 {metric_name} 메트릭이 없습니다"
        )
        metric = getattr(definitions, metric_name)
        assert metric is not None, f"{metric_name} 메트릭이 None입니다"
