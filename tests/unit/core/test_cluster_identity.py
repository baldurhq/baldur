"""
Cluster Identity Unit Tests.

ClusterIdentity 및 관련 함수들을 테스트합니다.

Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

import os
from unittest.mock import patch

import pytest


class TestClusterIdentity:
    """ClusterIdentity 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cluster_identity()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cluster_identity()

    def test_basic_creation(self):
        """기본 ClusterIdentity 생성."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="seoul-prod-01",
            region="seoul",
            environment="production",
        )

        assert identity.cluster_id == "seoul-prod-01"
        assert identity.region == "seoul"
        assert identity.environment == "production"
        assert identity.tenant is None

    def test_namespace_priority_region(self):
        """namespace 속성: region 우선."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="test",
            region="seoul",
            tenant="tenant123",
            environment="staging",
        )

        assert identity.namespace == "seoul"

    def test_namespace_priority_tenant(self):
        """namespace 속성: tenant 차선."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="test",
            region=None,
            tenant="tenant123",
            environment="staging",
        )

        assert identity.namespace == "tenant123"

    def test_namespace_priority_environment(self):
        """namespace 속성: environment 최하위."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="test",
            region=None,
            tenant=None,
            environment="staging",
        )

        assert identity.namespace == "staging"

    def test_full_prefix(self):
        """full_prefix 속성."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="test",
            region="tokyo",
        )

        assert identity.full_prefix == "baldur:tokyo:"

    def test_trace_id_prefix(self):
        """trace_id_prefix 속성."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="test",
            region="seoul",
            environment="production",
        )

        # 리전 앞 3글자 + 환경 앞 1글자
        assert identity.trace_id_prefix == "seop"

    def test_trace_id_prefix_no_region(self):
        """trace_id_prefix: 리전 없을 때."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="test",
            region=None,
            environment="development",
        )

        # "unk" + "d"
        assert identity.trace_id_prefix == "unkd"

    def test_validate_valid_cluster_id(self):
        """유효한 cluster_id 검증."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="my-cluster-01",
            region="seoul",
        )

        # fail_fast=False로 호출해서 sys.exit 방지
        assert identity.validate(fail_fast=False) is True

    def test_validate_default_cluster_id(self):
        """기본값 cluster_id는 유효하지 않음."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="default",
        )

        assert identity.validate(fail_fast=False) is False

    def test_validate_unknown_cluster_id(self):
        """unknown cluster_id는 유효하지 않음."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="unknown",
        )

        assert identity.validate(fail_fast=False) is False

    def test_validate_empty_cluster_id(self):
        """빈 cluster_id는 유효하지 않음."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="",
        )

        assert identity.validate(fail_fast=False) is False

    def test_validate_missing_region_fails(self):
        """region 누락 시 검증 실패 (Phase 1 FailFastClusterIdentity)."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="seoul-prod-01",
            region=None,  # 누락
        )

        # region 필수 - 검증 실패
        assert identity.validate(fail_fast=False) is False

    def test_validate_valid_cluster_and_region(self):
        """cluster_id와 region 모두 유효할 때 통과."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="seoul-prod-01",
            region="seoul",
        )

        assert identity.validate(fail_fast=False) is True

    def test_validate_fail_fast_exits_on_missing_region(self):
        """fail_fast=True일 때 region 누락 시 SystemExit."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="seoul-prod-01",
            region=None,
        )

        with pytest.raises(SystemExit) as exc_info:
            identity.validate(fail_fast=True)
        assert exc_info.value.code == 1

    def test_validate_fail_fast_exits_on_invalid_cluster_id(self):
        """fail_fast=True일 때 cluster_id 무효 시 SystemExit."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="default",
            region="seoul",
        )

        with pytest.raises(SystemExit) as exc_info:
            identity.validate(fail_fast=True)
        assert exc_info.value.code == 1

    def test_validate_multiple_errors_reported(self):
        """cluster_id와 region 모두 무효할 때 두 에러 모두 보고."""

        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="default",
            region=None,
        )

        # fail_fast=False로 에러 수집
        result = identity.validate(fail_fast=False)
        assert result is False

    def test_immutable(self):
        """ClusterIdentity는 불변."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(
            cluster_id="test",
            region="seoul",
        )

        with pytest.raises(Exception):  # FrozenInstanceError
            identity.cluster_id = "changed"


class TestClusterIdentitySingleton:
    """ClusterIdentity 싱글톤 테스트."""

    def setup_method(self):
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cluster_identity()

    def teardown_method(self):
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cluster_identity()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 같은 인스턴스 반환."""
        from baldur.core.cluster_identity import (
            get_cluster_identity,
            reset_cluster_identity,
        )

        reset_cluster_identity()
        i1 = get_cluster_identity()
        i2 = get_cluster_identity()
        assert i1 is i2

    def test_reset_clears_singleton(self):
        """reset 후 새 인스턴스 생성."""
        from baldur.core.cluster_identity import (
            get_cluster_identity,
            reset_cluster_identity,
        )

        i1 = get_cluster_identity()
        reset_cluster_identity()
        i2 = get_cluster_identity()
        assert i1 is not i2

    @patch.dict(
        os.environ,
        {
            "BALDUR_CLUSTER_ID": "env-cluster",
            "BALDUR_NAMESPACE_REGION": "busan",
            "BALDUR_NAMESPACE_ENV": "staging",
        },
        clear=False,
    )
    def test_env_var_loading(self):
        """환경변수에서 설정 로드."""
        from baldur.core.cluster_identity import (
            get_cluster_identity,
            reset_cluster_identity,
        )

        reset_cluster_identity()
        identity = get_cluster_identity()

        assert identity.cluster_id == "env-cluster"
        assert identity.region == "busan"
        assert identity.environment == "staging"

    def test_skip_validation_emits_deprecation_warning(self):
        """skip_validation=True emits DeprecationWarning."""
        import warnings

        from baldur.core.cluster_identity import get_cluster_identity

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            get_cluster_identity(skip_validation=True)

        deprecations = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert "skip_validation" in str(deprecations[0].message)

    def test_default_call_no_deprecation_warning(self):
        """get_cluster_identity() without skip_validation emits no warning."""
        import warnings

        from baldur.core.cluster_identity import get_cluster_identity

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            get_cluster_identity()

        deprecations = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecations) == 0


class TestClusterIdentityPodId:
    """ClusterIdentity pod_id 테스트."""

    def setup_method(self):
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cluster_identity()

    def teardown_method(self):
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cluster_identity()

    @patch.dict(os.environ, {"HOSTNAME": "pod-abc123"}, clear=False)
    def test_pod_id_from_hostname(self):
        """pod_id가 HOSTNAME 환경변수에서 로드됨."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(cluster_id="test")
        assert identity.pod_id == "pod-abc123"


class TestClusterIdentityValidateContract:
    """``validate(fail_fast)`` post-453 D3: explicit-only signature, no None sentinel.

    The sentinel ``fail_fast=None`` previously inlined a ``BALDUR_FAIL_FAST``
    env-read at the call site. Decoupling the env-read from validate() means
    callers (today: only ``bootstrap._validate_cluster_identity_if_namespaced``)
    pass the policy explicitly. The contract here pins both the resolved
    behavior and the signature change.
    """

    @pytest.mark.parametrize(
        ("fail_fast", "cluster_id", "region", "expected"),
        [
            (False, "seoul-prod-01", "seoul", True),
            (False, "default", "seoul", False),
            (False, "seoul-prod-01", None, False),
            (False, "default", None, False),
            (True, "seoul-prod-01", "seoul", True),
        ],
    )
    def test_validate_returns_expected_for_non_fatal_combinations(
        self, fail_fast, cluster_id, region, expected
    ):
        """Non-fatal matrix returns the expected boolean — no SystemExit raised."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(cluster_id=cluster_id, region=region)
        assert identity.validate(fail_fast=fail_fast) is expected

    @pytest.mark.parametrize(
        ("cluster_id", "region"),
        [
            ("default", "seoul"),
            ("unknown", "seoul"),
            ("seoul-prod-01", None),
            ("default", None),
        ],
    )
    def test_validate_fail_fast_true_exits_on_invalid_input(self, cluster_id, region):
        """``fail_fast=True`` raises ``SystemExit(1)`` on any validation error."""
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(cluster_id=cluster_id, region=region)
        with pytest.raises(SystemExit) as exc_info:
            identity.validate(fail_fast=True)
        assert exc_info.value.code == 1

    def test_validate_signature_default_is_true_no_sentinel(self):
        """453 D3: ``fail_fast`` default is ``True`` — no ``None`` sentinel."""
        import inspect
        import typing

        from baldur.core.cluster_identity import ClusterIdentity

        sig = inspect.signature(ClusterIdentity.validate)
        param = sig.parameters["fail_fast"]
        assert param.default is True

        # ``from __future__ import annotations`` stringifies annotations —
        # resolve via get_type_hints to compare the actual type object.
        hints = typing.get_type_hints(ClusterIdentity.validate)
        assert hints["fail_fast"] is bool

    def test_validate_does_not_read_baldur_fail_fast_env(self, monkeypatch):
        """453 D3: BALDUR_FAIL_FAST env is no longer consulted inside validate().

        Setting BALDUR_FAIL_FAST=true with explicit ``fail_fast=False`` must
        return False (env value is ignored). Conversely, BALDUR_FAIL_FAST=false
        with explicit ``fail_fast=True`` must still raise SystemExit.
        """
        from baldur.core.cluster_identity import ClusterIdentity

        identity = ClusterIdentity(cluster_id="default", region=None)

        # Env says fail-fast, caller says don't — caller wins.
        monkeypatch.setenv("BALDUR_FAIL_FAST", "true")
        assert identity.validate(fail_fast=False) is False

        # Env says don't fail-fast, caller says do — caller wins.
        monkeypatch.setenv("BALDUR_FAIL_FAST", "false")
        with pytest.raises(SystemExit):
            identity.validate(fail_fast=True)
