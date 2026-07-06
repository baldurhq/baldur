"""
Loki Labels 단위 테스트.

테스트 대상:
- get_standard_labels
- get_throttle_labels
- merge_labels
- validate_labels
- sanitize_label_value
"""

from __future__ import annotations


class TestGetStandardLabels:
    """get_standard_labels 함수 테스트."""

    def test_returns_expected_keys(self):
        """표준 라벨 키들이 반환되는지 확인."""
        from baldur.audit.loki_labels import get_standard_labels

        audit_data = {
            "action": "throttle_limit_adjusted",
            "severity": "info",
            "cluster": {
                "cluster_id": "seoul-prod-01",
                "region": "seoul",
                "environment": "production",
            },
        }

        labels = get_standard_labels(audit_data)

        assert "job" in labels
        assert "component" in labels
        assert "env" in labels
        assert "region" in labels
        assert "cluster" in labels
        assert "audit_action" in labels
        assert "severity" in labels
        assert "is_cascade" in labels

    def test_extracts_cluster_info(self):
        """클러스터 정보가 추출되는지 확인."""
        from baldur.audit.loki_labels import get_standard_labels

        audit_data = {
            "action": "throttle_limit_adjusted",
            "cluster": {
                "cluster_id": "seoul-prod-01",
                "region": "seoul",
                "environment": "staging",
            },
        }

        labels = get_standard_labels(audit_data)

        assert labels["region"] == "seoul"
        assert labels["cluster"] == "seoul-prod-01"
        assert labels["env"] == "staging"

    def test_marks_cascade_events_correctly(self):
        """CASCADE_EVENT 여부가 올바르게 표시되는지 확인."""
        from baldur.audit.loki_labels import get_standard_labels

        # CASCADE_EVENT
        cascade_data = {"action": "throttle_full_stop_activated"}
        cascade_labels = get_standard_labels(cascade_data)
        assert cascade_labels["is_cascade"] == "true"

        # 비 CASCADE_EVENT
        non_cascade_data = {"action": "throttle_limit_adjusted"}
        non_cascade_labels = get_standard_labels(non_cascade_data)
        assert non_cascade_labels["is_cascade"] == "false"

    def test_handles_missing_cluster_info(self):
        """클러스터 정보가 없을 때 기본값 사용 확인."""
        from baldur.audit.loki_labels import get_standard_labels

        audit_data = {"action": "throttle_limit_adjusted"}

        labels = get_standard_labels(audit_data)

        assert labels["region"] == "unknown"
        assert labels["cluster"] == "unknown"
        assert labels["env"] == "production"  # 기본값


class TestGetThrottleLabels:
    """get_throttle_labels 함수 테스트."""

    def test_creates_throttle_specific_labels(self):
        """Throttle 전용 라벨이 생성되는지 확인."""
        from baldur.audit.loki_labels import get_throttle_labels

        labels = get_throttle_labels(
            action="throttle_limit_adjusted",
            severity="info",
            region="seoul",
            cluster_id="seoul-prod-01",
            environment="production",
        )

        assert labels["job"] == "baldur-audit"
        assert labels["component"] == "throttle"
        assert labels["audit_action"] == "throttle_limit_adjusted"
        assert labels["severity"] == "info"
        assert labels["region"] == "seoul"
        assert labels["cluster"] == "seoul-prod-01"

    def test_cascade_event_marked(self):
        """CASCADE_EVENT 마킹 확인."""
        from baldur.audit.loki_labels import get_throttle_labels

        labels = get_throttle_labels(action="throttle_full_stop_activated")
        assert labels["is_cascade"] == "true"


class TestMergeLabels:
    """merge_labels 함수 테스트."""

    def test_merges_labels_correctly(self):
        """라벨 병합이 올바르게 동작하는지 확인."""
        from baldur.audit.loki_labels import merge_labels

        base = {"job": "audit", "env": "production"}
        custom = {"app": "throttle", "team": "platform"}

        result = merge_labels(base, custom)

        assert result["job"] == "audit"
        assert result["env"] == "production"
        assert result["app"] == "throttle"
        assert result["team"] == "platform"

    def test_custom_overrides_base(self):
        """커스텀 라벨이 기본 라벨을 오버라이드하는지 확인."""
        from baldur.audit.loki_labels import merge_labels

        base = {"job": "audit", "env": "production"}
        custom = {"env": "staging"}  # 오버라이드

        result = merge_labels(base, custom)

        assert result["env"] == "staging"

    def test_handles_none_custom(self):
        """custom이 None일 때 기본 라벨 복사 확인."""
        from baldur.audit.loki_labels import merge_labels

        base = {"job": "audit", "env": "production"}

        result = merge_labels(base, None)

        assert result == base
        # 원본과 다른 객체인지 확인
        assert result is not base


class TestValidateLabels:
    """validate_labels 함수 테스트."""

    def test_valid_labels_pass(self):
        """유효한 라벨이 통과하는지 확인."""
        from baldur.audit.loki_labels import validate_labels

        labels = {
            "job": "audit",
            "env": "production",
            "region": "seoul",
        }

        valid, errors = validate_labels(labels)

        assert valid is True
        assert len(errors) == 0

    def test_invalid_label_name_fails(self):
        """유효하지 않은 라벨 이름이 실패하는지 확인."""
        from baldur.audit.loki_labels import validate_labels

        labels = {
            "my-label": "value",  # 하이픈 허용 안됨
            "123start": "value",  # 숫자로 시작 불가
        }

        valid, errors = validate_labels(labels)

        assert valid is False
        assert len(errors) == 2

    def test_empty_value_fails(self):
        """빈 라벨 값이 실패하는지 확인."""
        from baldur.audit.loki_labels import validate_labels

        labels = {
            "job": "",  # 빈 값
            "env": "production",
        }

        valid, errors = validate_labels(labels)

        assert valid is False
        assert any("job" in e for e in errors)


class TestSanitizeLabelValue:
    """sanitize_label_value 함수 테스트."""

    def test_replaces_special_characters(self):
        """특수문자가 underscore로 대체되는지 확인.

        Note: 하이픈(-)은 유효한 문자이므로 대체되지 않음.
        """
        from baldur.audit.loki_labels import sanitize_label_value

        # 하이픈은 유효한 문자, 마침표는 underscore로 대체
        assert sanitize_label_value("my-service.v2") == "my-service_v2"
        assert sanitize_label_value("payment/gateway") == "payment_gateway"
        assert sanitize_label_value("api@v1") == "api_v1"

    def test_truncates_long_values(self):
        """긴 값이 잘리는지 확인."""
        from baldur.audit.loki_labels import sanitize_label_value

        long_value = "a" * 200
        result = sanitize_label_value(long_value, max_length=128)

        assert len(result) == 128

    def test_returns_unknown_for_empty(self):
        """빈 값에 대해 'unknown' 반환 확인."""
        from baldur.audit.loki_labels import sanitize_label_value

        assert sanitize_label_value("") == "unknown"
        assert sanitize_label_value(None) == "unknown"

    def test_preserves_valid_characters(self):
        """유효한 문자가 보존되는지 확인."""
        from baldur.audit.loki_labels import sanitize_label_value

        assert sanitize_label_value("my_service_v2") == "my_service_v2"
        assert sanitize_label_value("API123") == "API123"
