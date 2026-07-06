"""
Trace ID Unit Tests for Multi-Cluster Support.

Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

import os
from unittest.mock import patch


class TestGenerateTraceId:
    """generate_trace_id 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋 및 cluster prefix 활성화."""
        from baldur.audit.trace import set_cluster_prefix_enabled
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cluster_identity()
        set_cluster_prefix_enabled(True)

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.audit.trace import set_cluster_prefix_enabled
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cluster_identity()
        set_cluster_prefix_enabled(True)

    def test_basic_format_with_cluster_prefix(self):
        """클러스터 접두사가 포함된 기본 형식."""
        from baldur.audit.trace import generate_trace_id

        with patch.dict(
            os.environ,
            {
                "BALDUR_CLUSTER_ID": "seoul-prod-01",
                "BALDUR_NAMESPACE_REGION": "seoul",
                "BALDUR_NAMESPACE_ENV": "production",
                "BALDUR_FAIL_FAST": "false",
            },
        ):
            from baldur.core.cluster_identity import reset_cluster_identity

            reset_cluster_identity()

            trace_id = generate_trace_id()

            # req-seop-xxxxxxxx 형식
            assert trace_id.startswith("req-seo")
            parts = trace_id.split("-")
            assert len(parts) == 3
            assert parts[0] == "req"
            assert parts[1] == "seop"  # seoul + production
            assert len(parts[2]) == 8

    def test_format_without_cluster_prefix(self):
        """클러스터 접두사 없는 형식."""
        from baldur.audit.trace import generate_trace_id

        trace_id = generate_trace_id(include_cluster_prefix=False)

        # req-xxxxxxxx 형식
        assert trace_id.startswith("req-")
        parts = trace_id.split("-")
        assert len(parts) == 2
        assert parts[0] == "req"
        assert len(parts[1]) == 8

    def test_cluster_prefix_disabled_globally(self):
        """전역적으로 클러스터 접두사 비활성화."""
        from baldur.audit.trace import (
            generate_trace_id,
            set_cluster_prefix_enabled,
        )

        set_cluster_prefix_enabled(False)

        trace_id = generate_trace_id()

        # req-xxxxxxxx 형식 (클러스터 접두사 없음)
        parts = trace_id.split("-")
        assert len(parts) == 2

    def test_trace_id_prefix_format(self):
        """trace_id_prefix 형식 테스트."""
        from baldur.core.cluster_identity import ClusterIdentity

        # Seoul + Production -> seop
        identity1 = ClusterIdentity(
            cluster_id="test",
            region="seoul",
            environment="production",
        )
        assert identity1.trace_id_prefix == "seop"

        # Tokyo + Staging -> toks
        identity2 = ClusterIdentity(
            cluster_id="test",
            region="tokyo",
            environment="staging",
        )
        assert identity2.trace_id_prefix == "toks"

        # No region -> unkp
        identity3 = ClusterIdentity(
            cluster_id="test",
            region=None,
            environment="production",
        )
        assert identity3.trace_id_prefix == "unkp"

    def test_trace_id_uniqueness(self):
        """트레이스 ID 고유성 테스트."""
        from baldur.audit.trace import generate_trace_id

        ids = set()
        for _ in range(1000):
            trace_id = generate_trace_id(include_cluster_prefix=False)
            assert trace_id not in ids
            ids.add(trace_id)

    def test_fallback_when_identity_unavailable(self):
        """ClusterIdentity 사용 불가 시 폴백."""
        from baldur.audit.trace import (
            generate_trace_id,
            set_cluster_prefix_enabled,
        )

        set_cluster_prefix_enabled(True)

        # ClusterIdentity import 실패 시뮬레이션
        with patch(
            "baldur.core.cluster_identity.get_cluster_identity",
            side_effect=Exception("Test error"),
        ):
            # 재임포트 필요 없음 - 함수 내에서 import 시도
            trace_id = generate_trace_id()

            # 폴백: req-xxxxxxxx 형식
            parts = trace_id.split("-")
            assert len(parts) == 2
