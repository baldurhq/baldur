"""
HedgingPolicy / AsyncHedgingPolicy / HedgingConfigUpdateHook 단위 테스트 (#230).

테스트 대상:
- resilience/policies/hedging.py (HedgingPolicy, AsyncHedgingPolicy, HedgingConfigUpdateHook)
- core/hedging/strategy.py (HedgingStrategy deprecated)
- core/hedging/config.py (bulkhead_name, acquire_bulkhead_per_candidate deprecated metadata)
- core/hedging/__init__.py (신규 심볼 export)
- resilience/policies/__init__.py (신규 심볼 export)

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): 하드코딩 기대값 (name, outcome, executed_policies, _LOAD_LEVEL_ORDER)
- 동작 검증(Behavior): 소스 참조 (PolicyOutcome, HedgingConfig 기본값 등)
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부 (§5.1)
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import warnings
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.resilience_policy import (
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)
from baldur.resilience.policies.hedging import (
    _LOAD_LEVEL_ORDER,
    AsyncHedgingPolicy,
    HedgingConfigUpdateHook,
    HedgingPolicy,
)
from baldur_pro.services.hedging.config import (
    HedgingCandidate,
    HedgingConfig,
    HedgingMode,
)
from baldur_pro.services.hedging.exceptions import HedgingError

# =============================================================================
# Fixtures — 1개 파일 전용이므로 파일 내부 배치 (§5.1)
# =============================================================================


@pytest.fixture
def basic_policy():
    """candidates + default_value를 가진 기본 HedgingPolicy."""
    return HedgingPolicy(
        candidates=[lambda: "candidate_1_value"],
        config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.01, timeout=2.0),
        default_value="default",
    )


@pytest.fixture
def no_default_policy():
    """default_value 없는 HedgingPolicy."""
    return HedgingPolicy(
        candidates=[lambda: "candidate_1_value"],
        config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.01, timeout=2.0),
    )


@pytest.fixture
def named_policy():
    """candidate_names가 있는 HedgingPolicy."""
    return HedgingPolicy(
        candidates=[lambda: "b", lambda: "c"],
        candidate_names=["my_primary", "region_b", "region_c"],
        config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.01, timeout=2.0),
    )


@pytest.fixture
def mock_per_candidate_policy():
    """per_candidate_policy 목 객체."""
    policy = MagicMock(spec=ResiliencePolicy)
    policy.name = "mock_per_candidate"
    policy.execute = MagicMock(
        return_value=PolicyResult(
            value="policy_wrapped_value",
            outcome=PolicyOutcome.SUCCESS,
            executed_policies=["mock_per_candidate"],
        )
    )
    return policy


@pytest.fixture
def mock_overall_policy():
    """overall_policy 목 객체."""
    policy = MagicMock(spec=ResiliencePolicy)
    policy.name = "mock_overall"

    def pass_through(fn, *args, **kwargs):
        result_value = fn()
        return PolicyResult(
            value=result_value,
            outcome=PolicyOutcome.SUCCESS,
            executed_policies=["mock_overall"],
            metadata={},
        )

    policy.execute = MagicMock(side_effect=pass_through)
    return policy


@pytest.fixture
def async_basic_policy():
    """candidates를 가진 기본 AsyncHedgingPolicy."""

    async def async_candidate():
        return "async_candidate_value"

    return AsyncHedgingPolicy(
        candidates=[async_candidate],
        config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.01, timeout=2.0),
        default_value="async_default",
    )


# =============================================================================
# 계약 검증 (Contract) — HedgingPolicy 고정 식별자 및 결과 구조
# =============================================================================


class TestHedgingPolicyContract:
    """HedgingPolicy 고정 식별자 및 결과 구조 계약 검증."""

    def test_name_is_hedging(self, basic_policy):
        """name property는 'hedging'이다."""
        assert basic_policy.name == "hedging"

    def test_success_result_has_hedging_in_executed_policies(self, basic_policy):
        """성공 결과의 executed_policies에 'hedging'이 포함된다."""
        result = basic_policy.execute(lambda: "primary_value")
        assert "hedging" in result.executed_policies

    def test_result_is_policy_result_instance(self, basic_policy):
        """반환 타입은 PolicyResult이다."""
        result = basic_policy.execute(lambda: "ok")
        assert isinstance(result, PolicyResult)

    def test_success_outcome_is_success(self, basic_policy):
        """func 성공 시 outcome은 PolicyOutcome.SUCCESS이다."""
        result = basic_policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_success_metadata_has_hedged_key(self, basic_policy):
        """성공 결과의 metadata에 'hedged' 키가 존재한다."""
        result = basic_policy.execute(lambda: "ok")
        assert "hedged" in result.metadata

    def test_success_metadata_has_winner_key(self, basic_policy):
        """성공 결과의 metadata에 'winner' 키가 존재한다."""
        result = basic_policy.execute(lambda: "ok")
        assert "winner" in result.metadata

    def test_success_metadata_has_latency_ms_key(self, basic_policy):
        """성공 결과의 metadata에 'latency_ms' 키가 존재한다."""
        result = basic_policy.execute(lambda: "ok")
        assert "latency_ms" in result.metadata

    def test_success_metadata_has_hedging_benefit_ms_key(self, basic_policy):
        """성공 결과의 metadata에 'hedging_benefit_ms' 키가 존재한다."""
        result = basic_policy.execute(lambda: "ok")
        assert "hedging_benefit_ms" in result.metadata

    def test_default_value_fallback_outcome(self, basic_policy):
        """모든 후보 실패 + default_value 시 outcome은 SUCCESS_WITH_FALLBACK이다."""

        def all_fail():
            raise RuntimeError("primary fail")

        policy = HedgingPolicy(
            candidates=[lambda: (_ for _ in ()).throw(RuntimeError("c1 fail"))],
            config=HedgingConfig(mode=HedgingMode.IMMEDIATE, delay=0.01, timeout=1.0),
            default_value="fallback_default",
        )
        result = policy.execute(all_fail)
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_default_value_metadata_hedging_all_failed(self, basic_policy):
        """모든 후보 실패 + default_value 시 metadata['hedging_all_failed']는 True이다."""

        def all_fail():
            raise RuntimeError("fail")

        policy = HedgingPolicy(
            candidates=[lambda: (_ for _ in ()).throw(RuntimeError("c1 fail"))],
            config=HedgingConfig(mode=HedgingMode.IMMEDIATE, delay=0.01, timeout=1.0),
            default_value="fallback_default",
        )
        result = policy.execute(all_fail)
        assert result.metadata.get("hedging_all_failed") is True

    def test_no_default_failure_outcome(self, no_default_policy):
        """모든 후보 실패 + default_value 없으면 outcome은 FAILURE이다."""

        def all_fail():
            raise RuntimeError("primary fail")

        policy = HedgingPolicy(
            candidates=[lambda: (_ for _ in ()).throw(RuntimeError("c1 fail"))],
            config=HedgingConfig(mode=HedgingMode.IMMEDIATE, delay=0.01, timeout=1.0),
        )
        result = policy.execute(all_fail)
        assert result.outcome == PolicyOutcome.FAILURE

    def test_single_execution_metadata_hedged_false(self):
        """후보 없이 단일 실행 시 metadata['hedged']는 False이다."""
        policy = HedgingPolicy(
            candidates=[],
            config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.01, timeout=2.0),
        )
        result = policy.execute(lambda: "single")
        assert result.metadata["hedged"] is False

    def test_hedging_policy_is_resilience_policy_instance(self, basic_policy):
        """HedgingPolicy는 ResiliencePolicy의 인스턴스이다."""
        assert isinstance(basic_policy, ResiliencePolicy)


# =============================================================================
# 계약 검증 (Contract) — _LOAD_LEVEL_ORDER 매핑 테이블
# =============================================================================


class TestLoadLevelOrderContract:
    """_LOAD_LEVEL_ORDER 매핑 테이블 계약 검증."""

    def test_none_is_0(self):
        """none → 0."""
        assert _LOAD_LEVEL_ORDER["none"] == 0

    def test_low_is_1(self):
        """low → 1."""
        assert _LOAD_LEVEL_ORDER["low"] == 1

    def test_medium_is_2(self):
        """medium → 2."""
        assert _LOAD_LEVEL_ORDER["medium"] == 2

    def test_high_is_3(self):
        """high → 3."""
        assert _LOAD_LEVEL_ORDER["high"] == 3

    def test_critical_is_4(self):
        """critical → 4."""
        assert _LOAD_LEVEL_ORDER["critical"] == 4

    def test_has_exactly_5_entries(self):
        """매핑 테이블은 정확히 5개 항목을 가진다."""
        assert len(_LOAD_LEVEL_ORDER) == 5

    def test_order_is_monotonically_increasing(self):
        """순서가 none < low < medium < high < critical이다."""
        assert (
            _LOAD_LEVEL_ORDER["none"]
            < _LOAD_LEVEL_ORDER["low"]
            < _LOAD_LEVEL_ORDER["medium"]
            < _LOAD_LEVEL_ORDER["high"]
            < _LOAD_LEVEL_ORDER["critical"]
        )


# =============================================================================
# 계약 검증 (Contract) — AsyncHedgingPolicy 고정 식별자
# =============================================================================


class TestAsyncHedgingPolicyContract:
    """AsyncHedgingPolicy 고정 식별자 및 결과 구조 계약 검증."""

    def test_name_is_hedging(self, async_basic_policy):
        """name property는 'hedging'이다."""
        assert async_basic_policy.name == "hedging"

    @pytest.mark.asyncio
    async def test_success_result_has_hedging_in_executed_policies(
        self, async_basic_policy
    ):
        """성공 결과의 executed_policies에 'hedging'이 포함된다."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert "hedging" in result.executed_policies

    @pytest.mark.asyncio
    async def test_success_outcome_is_success(self, async_basic_policy):
        """func 성공 시 outcome은 PolicyOutcome.SUCCESS이다."""

        async def ok():
            return 42

        result = await async_basic_policy.execute(ok)
        assert result.outcome == PolicyOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_result_is_policy_result_instance(self, async_basic_policy):
        """반환 타입은 PolicyResult이다."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert isinstance(result, PolicyResult)

    @pytest.mark.asyncio
    async def test_success_metadata_has_hedged_key(self, async_basic_policy):
        """성공 결과의 metadata에 'hedged' 키가 존재한다."""

        async def ok():
            return "ok"

        result = await async_basic_policy.execute(ok)
        assert "hedged" in result.metadata


# =============================================================================
# 계약 검증 (Contract) — Export 검증
# =============================================================================


class TestExportContract:
    """core/hedging/__init__.py, resilience/policies/__init__.py export 계약 검증."""

    def test_hedging_policy_exported_from_core_hedging(self):
        """HedgingPolicy는 core/hedging/__init__.py에서 export된다."""
        from baldur_pro.services.hedging import HedgingPolicy as HP

        assert HP is HedgingPolicy

    def test_async_hedging_policy_exported_from_core_hedging(self):
        """AsyncHedgingPolicy는 core/hedging/__init__.py에서 export된다."""
        from baldur_pro.services.hedging import AsyncHedgingPolicy as AHP

        assert AHP is AsyncHedgingPolicy

    def test_hedging_config_update_hook_exported_from_core_hedging(self):
        """HedgingConfigUpdateHook은 core/hedging/__init__.py에서 export된다."""
        from baldur_pro.services.hedging import HedgingConfigUpdateHook as HCUH

        assert HCUH is HedgingConfigUpdateHook

    def test_hedging_policy_exported_from_resilience_policies(self):
        """HedgingPolicy는 resilience/policies/__init__.py에서 export된다."""
        from baldur.resilience.policies import HedgingPolicy as HP

        assert HP is HedgingPolicy

    def test_async_hedging_policy_exported_from_resilience_policies(self):
        """AsyncHedgingPolicy는 resilience/policies/__init__.py에서 export된다."""
        from baldur.resilience.policies import AsyncHedgingPolicy as AHP

        assert AHP is AsyncHedgingPolicy

    def test_hedging_config_update_hook_exported_from_resilience_policies(self):
        """HedgingConfigUpdateHook은 resilience/policies/__init__.py에서 export된다."""
        from baldur.resilience.policies import HedgingConfigUpdateHook as HCUH

        assert HCUH is HedgingConfigUpdateHook

    def test_core_hedging_all_contains_hedging_policy(self):
        """core/hedging/__all__에 'HedgingPolicy'가 포함된다."""
        import baldur_pro.services.hedging as hedging_module

        assert "HedgingPolicy" in hedging_module.__all__

    def test_core_hedging_all_contains_async_hedging_policy(self):
        """core/hedging/__all__에 'AsyncHedgingPolicy'가 포함된다."""
        import baldur_pro.services.hedging as hedging_module

        assert "AsyncHedgingPolicy" in hedging_module.__all__

    def test_core_hedging_all_contains_hedging_config_update_hook(self):
        """core/hedging/__all__에 'HedgingConfigUpdateHook'이 포함된다."""
        import baldur_pro.services.hedging as hedging_module

        assert "HedgingConfigUpdateHook" in hedging_module.__all__

    @pytest.mark.parametrize(
        "name",
        ["HedgingPolicy", "AsyncHedgingPolicy", "HedgingConfigUpdateHook"],
    )
    def test_resilience_policies_soft_removed_but_resolvable(self, name):
        """Hedging names are absent from resilience/policies/__all__ (honest
        advertisement — the engine requires the licensed package at runtime)
        yet still resolvable for existing import statements."""
        import baldur.resilience.policies as policies_module

        assert name not in policies_module.__all__
        assert getattr(policies_module, name) is not None


# =============================================================================
# 계약 검증 (Contract) — HedgingStrategy deprecated 경고
# =============================================================================


class TestHedgingStrategyDeprecatedContract:
    """HedgingStrategy 생성 시 DeprecationWarning 계약 검증."""

    def test_hedging_strategy_emits_deprecation_warning(self):
        """HedgingStrategy 생성 시 DeprecationWarning이 발생한다."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from baldur_pro.services.hedging.strategy import HedgingStrategy

            HedgingStrategy(
                candidates=[lambda: "a"],
                config=HedgingConfig(mode=HedgingMode.DELAYED),
            )
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) >= 1

    def test_hedging_strategy_warning_mentions_hedging_policy(self):
        """DeprecationWarning 메시지에 'HedgingPolicy'가 포함된다."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from baldur_pro.services.hedging.strategy import HedgingStrategy

            HedgingStrategy(
                candidates=[lambda: "a"],
                config=HedgingConfig(mode=HedgingMode.DELAYED),
            )
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert any(
                "HedgingPolicy" in str(dw.message) for dw in deprecation_warnings
            )


# =============================================================================
# 동작 검증 (Behavior) — HedgingPolicy 실행 동작
# =============================================================================


class TestHedgingPolicyExecuteBehavior:
    """HedgingPolicy.execute() 동작 검증."""

    def test_primary_success_returns_primary_value(self, basic_policy):
        """Primary 성공 시 그 값을 반환한다."""
        result = basic_policy.execute(lambda: "primary_value")
        assert result.success is True
        assert result.value is not None

    def test_execute_returns_success_on_normal_func(self, basic_policy):
        """정상 함수 실행 시 PolicyResult.success가 True이다."""
        result = basic_policy.execute(lambda: 42)
        assert result.success is True

    def test_execute_passes_args_to_func(self):
        """func에 *args가 전달된다."""
        policy = HedgingPolicy(
            candidates=[],
            config=HedgingConfig(delay=0.01, timeout=2.0),
        )

        def add(a, b):
            return a + b

        result = policy.execute(add, 3, 7)
        assert result.value == 10

    def test_execute_passes_kwargs_to_func(self):
        """func에 **kwargs가 전달된다."""
        policy = HedgingPolicy(
            candidates=[],
            config=HedgingConfig(delay=0.01, timeout=2.0),
        )

        def greet(name="world"):
            return f"hello {name}"

        result = policy.execute(greet, name="test")
        assert result.value == "hello test"

    def test_all_fail_with_default_returns_default(self, basic_policy):
        """모든 후보 실패 시 default_value를 반환한다."""

        def failing():
            raise RuntimeError("fail")

        policy = HedgingPolicy(
            candidates=[lambda: (_ for _ in ()).throw(RuntimeError("c1"))],
            config=HedgingConfig(mode=HedgingMode.IMMEDIATE, delay=0.01, timeout=1.0),
            default_value="fallback_default",
        )
        result = policy.execute(failing)
        assert result.value == "fallback_default"

    def test_all_fail_no_default_has_error(self):
        """모든 후보 실패 + default_value 없으면 error가 설정된다."""

        def failing():
            raise RuntimeError("fail")

        policy = HedgingPolicy(
            candidates=[lambda: (_ for _ in ()).throw(RuntimeError("c1"))],
            config=HedgingConfig(mode=HedgingMode.IMMEDIATE, delay=0.01, timeout=1.0),
        )
        result = policy.execute(failing)
        assert result.error is not None


# =============================================================================
# 동작 검증 (Behavior) — Backpressure 동작
# =============================================================================


class TestHedgingPolicyBackpressureBehavior:
    """HedgingPolicy Backpressure 동작 검증."""

    def test_disable_hedging_on_high_load(self):
        """부하 레벨이 disable_on_load_level 이상이면 헷징이 비활성화된다."""
        config = HedgingConfig(
            mode=HedgingMode.DELAYED,
            delay=0.01,
            timeout=2.0,
            disable_on_load_level="high",
        )
        policy = HedgingPolicy(
            candidates=[lambda: "candidate"],
            config=config,
            initial_load_level="high",
        )
        result = policy.execute(lambda: "primary")
        assert result.metadata["hedged"] is False

    def test_disable_hedging_on_critical_load(self):
        """critical 부하에서도 disable_on_load_level=high이면 비활성화된다."""
        config = HedgingConfig(
            mode=HedgingMode.DELAYED,
            delay=0.01,
            timeout=2.0,
            disable_on_load_level="high",
        )
        policy = HedgingPolicy(
            candidates=[lambda: "candidate"],
            config=config,
            initial_load_level="critical",
        )
        result = policy.execute(lambda: "primary")
        assert result.metadata["hedged"] is False

    def test_hedging_active_on_low_load(self):
        """부하 레벨이 disable_on_load_level 미만이면 헷징이 활성화된다."""
        config = HedgingConfig(
            mode=HedgingMode.DELAYED,
            delay=0.01,
            timeout=2.0,
            disable_on_load_level="high",
        )
        policy = HedgingPolicy(
            candidates=[lambda: "candidate"],
            config=config,
            initial_load_level="low",
        )
        result = policy.execute(lambda: "primary")
        assert result.success is True

    def test_effective_delay_medium_multiplier(self):
        """medium 부하에서 delay에 delay_multiplier_on_medium이 적용된다."""
        config = HedgingConfig(delay=0.1, delay_multiplier_on_medium=2.0)
        policy = HedgingPolicy(config=config, initial_load_level="medium")
        effective = policy._get_effective_delay()
        assert effective == pytest.approx(
            config.delay * config.delay_multiplier_on_medium
        )

    def test_effective_delay_high_multiplier(self):
        """high 부하에서 delay에 delay_multiplier_on_high가 적용된다."""
        config = HedgingConfig(delay=0.1, delay_multiplier_on_high=5.0)
        policy = HedgingPolicy(config=config, initial_load_level="high")
        effective = policy._get_effective_delay()
        assert effective == pytest.approx(
            config.delay * config.delay_multiplier_on_high
        )

    def test_effective_delay_none_returns_base(self):
        """none 부하에서는 기본 delay를 그대로 반환한다."""
        config = HedgingConfig(delay=0.1)
        policy = HedgingPolicy(config=config, initial_load_level="none")
        effective = policy._get_effective_delay()
        assert effective == pytest.approx(config.delay)

    def test_effective_delay_low_returns_base(self):
        """low 부하에서는 기본 delay를 그대로 반환한다."""
        config = HedgingConfig(delay=0.1)
        policy = HedgingPolicy(config=config, initial_load_level="low")
        effective = policy._get_effective_delay()
        assert effective == pytest.approx(config.delay)

    def test_delay_restored_after_execute(self):
        """execute 후 config.delay가 원래 값으로 복원된다."""
        config = HedgingConfig(delay=0.1, delay_multiplier_on_medium=3.0)
        policy = HedgingPolicy(
            candidates=[lambda: "c"],
            config=config,
            initial_load_level="medium",
        )
        original_delay = config.delay
        policy.execute(lambda: "primary")
        assert config.delay == pytest.approx(original_delay)


# =============================================================================
# 동작 검증 (Behavior) — _build_candidates 후보 구성
# =============================================================================


class TestHedgingPolicyBuildCandidatesBehavior:
    """HedgingPolicy._build_candidates() 동작 검증."""

    def test_primary_is_first_candidate(self, basic_policy):
        """func이 첫 번째 후보(primary)로 추가된다."""
        candidates = basic_policy._build_candidates(lambda: "primary")
        assert candidates[0].name == "primary"

    def test_candidates_appended_after_primary(self, basic_policy):
        """생성자 candidates는 primary 뒤에 추가된다."""
        candidates = basic_policy._build_candidates(lambda: "primary")
        assert len(candidates) == 2  # primary + 1 candidate

    def test_custom_names_applied(self, named_policy):
        """candidate_names가 설정되면 해당 이름이 사용된다."""
        candidates = named_policy._build_candidates(lambda: "primary")
        assert candidates[0].name == "my_primary"
        assert candidates[1].name == "region_b"
        assert candidates[2].name == "region_c"

    def test_default_primary_name(self):
        """candidate_names가 없으면 기본 이름 'primary'가 사용된다."""
        policy = HedgingPolicy(
            candidates=[lambda: "c1"],
            config=HedgingConfig(delay=0.01, timeout=2.0),
        )
        candidates = policy._build_candidates(lambda: "p")
        assert candidates[0].name == "primary"

    def test_default_candidate_names(self):
        """candidate_names가 없으면 'candidate_N' 형식의 기본 이름이 사용된다."""
        policy = HedgingPolicy(
            candidates=[lambda: "c1", lambda: "c2"],
            config=HedgingConfig(delay=0.01, timeout=2.0),
        )
        candidates = policy._build_candidates(lambda: "p")
        assert candidates[1].name == "candidate_1"
        assert candidates[2].name == "candidate_2"

    def test_max_candidates_limit(self):
        """max_candidates 설정에 따라 후보가 제한된다."""
        config = HedgingConfig(delay=0.01, timeout=2.0, max_candidates=2)
        policy = HedgingPolicy(
            candidates=[lambda: "c1", lambda: "c2", lambda: "c3"],
            config=config,
        )
        candidates = policy._build_candidates(lambda: "p")
        assert len(candidates) == config.max_candidates

    def test_primary_fn_wraps_args(self):
        """func + args가 no-arg callable로 래핑되어 올바르게 실행된다."""
        policy = HedgingPolicy(config=HedgingConfig(delay=0.01, timeout=2.0))
        candidates = policy._build_candidates(lambda x, y: x + y, 3, 4)
        result = candidates[0].fn()
        assert result == 7


# =============================================================================
# 동작 검증 (Behavior) — _execute_single 단일 실행
# =============================================================================


class TestHedgingPolicyExecuteSingleBehavior:
    """HedgingPolicy._execute_single() 동작 검증."""

    def test_single_success(self, basic_policy):
        """단일 실행 성공 시 value를 반환한다."""
        result = basic_policy._execute_single(lambda: "single_value")
        assert result.value == "single_value"
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_single_success_hedged_false(self, basic_policy):
        """단일 실행 성공 시 metadata['hedged']는 False이다."""
        result = basic_policy._execute_single(lambda: "ok")
        assert result.metadata["hedged"] is False

    def test_single_failure_with_default(self, basic_policy):
        """단일 실행 실패 시 default_value를 반환한다."""
        result = basic_policy._execute_single(
            lambda: (_ for _ in ()).throw(RuntimeError("fail"))
        )
        assert result.value == "default"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    def test_single_failure_without_default(self, no_default_policy):
        """단일 실행 실패 + default_value 없으면 FAILURE를 반환한다."""
        result = no_default_policy._execute_single(
            lambda: (_ for _ in ()).throw(RuntimeError("fail"))
        )
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is not None

    def test_single_failure_metadata_single_failed(self, basic_policy):
        """단일 실행 실패 시 default fallback의 metadata에 'single_failed'가 True이다."""
        result = basic_policy._execute_single(
            lambda: (_ for _ in ()).throw(RuntimeError("fail"))
        )
        assert result.metadata.get("single_failed") is True


# =============================================================================
# 동작 검증 (Behavior) — on_config_updated 이벤트 핸들러
# =============================================================================


class TestHedgingPolicyOnConfigUpdatedBehavior:
    """HedgingPolicy.on_config_updated() 동작 검증."""

    def test_update_mode(self, basic_policy):
        """hedging.mode 이벤트로 모드가 변경된다."""
        basic_policy.on_config_updated({"key": "hedging.mode", "value": "immediate"})
        assert basic_policy._config.mode == HedgingMode.IMMEDIATE

    def test_update_delay(self, basic_policy):
        """hedging.delay 이벤트로 delay가 변경된다."""
        basic_policy.on_config_updated({"key": "hedging.delay", "value": 0.5})
        assert basic_policy._config.delay == pytest.approx(0.5)

    def test_update_load_level(self, basic_policy):
        """backpressure.level 이벤트로 부하 레벨이 변경된다."""
        basic_policy.on_config_updated(
            {"key": "backpressure.level", "value": "CRITICAL"}
        )
        assert basic_policy._current_load_level == "critical"

    def test_invalid_mode_does_not_crash(self, basic_policy):
        """유효하지 않은 mode 값은 무시되고 예외가 발생하지 않는다."""
        original_mode = basic_policy._config.mode
        basic_policy.on_config_updated({"key": "hedging.mode", "value": "nonexistent"})
        assert basic_policy._config.mode == original_mode

    def test_empty_key_ignored(self, basic_policy):
        """빈 key 이벤트는 무시된다."""
        original_mode = basic_policy._config.mode
        basic_policy.on_config_updated({"key": "", "value": "something"})
        assert basic_policy._config.mode == original_mode

    def test_none_value_for_delay_ignored(self, basic_policy):
        """delay의 value가 None이면 변경되지 않는다."""
        original_delay = basic_policy._config.delay
        basic_policy.on_config_updated({"key": "hedging.delay", "value": None})
        assert basic_policy._config.delay == pytest.approx(original_delay)


# =============================================================================
# 동작 검증 (Behavior) — per_candidate_policy 래핑
# =============================================================================


class TestHedgingPolicyPerCandidateBehavior:
    """HedgingPolicy per_candidate_policy 래핑 동작 검증."""

    def test_per_candidate_policy_wraps_candidates(self, mock_per_candidate_policy):
        """per_candidate_policy가 설정되면 각 후보가 래핑된다."""
        policy = HedgingPolicy(
            candidates=[lambda: "c1"],
            config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.01, timeout=2.0),
            per_candidate_policy=mock_per_candidate_policy,
        )
        candidates = policy._build_candidates(lambda: "primary")
        wrapped = policy._wrap_candidates_with_policy(candidates)
        assert len(wrapped) == len(candidates)

    def test_per_candidate_wrapped_fn_calls_policy_execute(
        self, mock_per_candidate_policy
    ):
        """래핑된 후보 실행 시 per_candidate_policy.execute()가 호출된다."""
        policy = HedgingPolicy(
            candidates=[],
            config=HedgingConfig(delay=0.01, timeout=2.0),
            per_candidate_policy=mock_per_candidate_policy,
        )
        candidate = HedgingCandidate(name="test", fn=lambda: "orig", priority=0)
        wrapped = policy._wrap_candidates_with_policy([candidate])
        wrapped[0].fn()
        mock_per_candidate_policy.execute.assert_called_once()

    def test_per_candidate_rejected_raises_runtime_error(self):
        """per_candidate_policy가 REJECTED를 반환하면 RuntimeError가 발생한다."""
        rejected_policy = MagicMock(spec=ResiliencePolicy)
        rejected_policy.execute = MagicMock(
            return_value=PolicyResult(
                value=None,
                outcome=PolicyOutcome.REJECTED,
            )
        )
        policy = HedgingPolicy(
            config=HedgingConfig(delay=0.01, timeout=2.0),
            per_candidate_policy=rejected_policy,
        )
        candidate = HedgingCandidate(name="test", fn=lambda: "orig", priority=0)
        wrapped = policy._wrap_candidates_with_policy([candidate])
        with pytest.raises(RuntimeError, match="rejected"):
            wrapped[0].fn()

    def test_per_candidate_timeout_raises_timeout_error(self):
        """per_candidate_policy가 TIMEOUT을 반환하면 TimeoutError가 발생한다."""
        timeout_policy = MagicMock(spec=ResiliencePolicy)
        timeout_policy.execute = MagicMock(
            return_value=PolicyResult(
                value=None,
                outcome=PolicyOutcome.TIMEOUT,
            )
        )
        policy = HedgingPolicy(
            config=HedgingConfig(delay=0.01, timeout=2.0),
            per_candidate_policy=timeout_policy,
        )
        candidate = HedgingCandidate(name="test", fn=lambda: "orig", priority=0)
        wrapped = policy._wrap_candidates_with_policy([candidate])
        with pytest.raises(TimeoutError, match="timed out"):
            wrapped[0].fn()

    def test_per_candidate_success_with_fallback_returns_value(self):
        """per_candidate_policy가 SUCCESS_WITH_FALLBACK을 반환하면 value를 벗긴다."""
        fallback_policy = MagicMock(spec=ResiliencePolicy)
        fallback_policy.execute = MagicMock(
            return_value=PolicyResult(
                value="fallback_value",
                outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
            )
        )
        policy = HedgingPolicy(
            config=HedgingConfig(delay=0.01, timeout=2.0),
            per_candidate_policy=fallback_policy,
        )
        candidate = HedgingCandidate(name="test", fn=lambda: "orig", priority=0)
        wrapped = policy._wrap_candidates_with_policy([candidate])
        assert wrapped[0].fn() == "fallback_value"

    def test_per_candidate_failure_raises_runtime_error(self):
        """per_candidate_policy가 FAILURE를 반환하면 RuntimeError가 발생한다."""
        fail_policy = MagicMock(spec=ResiliencePolicy)
        fail_policy.execute = MagicMock(
            return_value=PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
            )
        )
        policy = HedgingPolicy(
            config=HedgingConfig(delay=0.01, timeout=2.0),
            per_candidate_policy=fail_policy,
        )
        candidate = HedgingCandidate(name="test", fn=lambda: "orig", priority=0)
        wrapped = policy._wrap_candidates_with_policy([candidate])
        with pytest.raises(RuntimeError, match="failed"):
            wrapped[0].fn()


# =============================================================================
# 동작 검증 (Behavior) — overall_policy Double Wrapping 방지
# =============================================================================


class TestHedgingPolicyOverallPolicyBehavior:
    """HedgingPolicy overall_policy 동작 검증 (Double Wrapping 방지)."""

    def test_overall_policy_success_appends_hedging(self, mock_overall_policy):
        """overall_policy 성공 시 executed_policies에 'hedging'이 추가된다."""
        policy = HedgingPolicy(
            candidates=[lambda: "c"],
            config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.01, timeout=2.0),
            overall_policy=mock_overall_policy,
        )
        result = policy.execute(lambda: "primary")
        assert "hedging" in result.executed_policies

    def test_overall_policy_success_merges_metadata(self, mock_overall_policy):
        """overall_policy 성공 시 hedging metadata가 병합된다."""
        policy = HedgingPolicy(
            candidates=[lambda: "c"],
            config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.01, timeout=2.0),
            overall_policy=mock_overall_policy,
        )
        result = policy.execute(lambda: "primary")
        assert result.success is True

    def test_overall_policy_rejected_passes_through(self):
        """overall_policy가 REJECTED를 반환하면 그대로 전달된다."""
        rejected_policy = MagicMock(spec=ResiliencePolicy)
        rejected_policy.execute = MagicMock(
            return_value=PolicyResult(
                value=None,
                outcome=PolicyOutcome.REJECTED,
                executed_policies=["bulkhead"],
                metadata={},
            )
        )
        policy = HedgingPolicy(
            candidates=[lambda: "c"],
            config=HedgingConfig(delay=0.01, timeout=2.0),
            overall_policy=rejected_policy,
        )
        result = policy.execute(lambda: "primary")
        assert result.outcome == PolicyOutcome.REJECTED
        assert "hedging" in result.executed_policies

    def test_overall_policy_hedging_error_with_default(self):
        """overall_policy를 통과한 HedgingError + default_value → SUCCESS_WITH_FALLBACK."""

        def raise_overall(fn, *a, **kw):
            raise HedgingError("all failed via overall")

        overall = MagicMock(spec=ResiliencePolicy)
        overall.execute = MagicMock(side_effect=raise_overall)

        policy = HedgingPolicy(
            candidates=[lambda: "c"],
            config=HedgingConfig(delay=0.01, timeout=2.0),
            overall_policy=overall,
            default_value="default_fallback",
        )
        result = policy.execute(lambda: "primary")
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "default_fallback"

    def test_overall_policy_hedging_error_without_default(self):
        """overall_policy를 통과한 HedgingError + default_value 없음 → FAILURE."""

        def raise_overall(fn, *a, **kw):
            raise HedgingError("all failed via overall")

        overall = MagicMock(spec=ResiliencePolicy)
        overall.execute = MagicMock(side_effect=raise_overall)

        policy = HedgingPolicy(
            candidates=[lambda: "c"],
            config=HedgingConfig(delay=0.01, timeout=2.0),
            overall_policy=overall,
        )
        result = policy.execute(lambda: "primary")
        assert result.outcome == PolicyOutcome.FAILURE


# =============================================================================
# 동작 검증 (Behavior) — _should_disable_hedging 경계값
# =============================================================================


class TestShouldDisableHedgingBehavior:
    """HedgingPolicy._should_disable_hedging() 경계값 검증."""

    def test_exact_threshold_disables(self):
        """부하 레벨이 임계값과 동일하면 비활성화된다."""
        config = HedgingConfig(disable_on_load_level="medium")
        policy = HedgingPolicy(config=config, initial_load_level="medium")
        assert policy._should_disable_hedging() is True

    def test_below_threshold_allows(self):
        """부하 레벨이 임계값 미만이면 활성화된다."""
        config = HedgingConfig(disable_on_load_level="high")
        policy = HedgingPolicy(config=config, initial_load_level="medium")
        assert policy._should_disable_hedging() is False

    def test_above_threshold_disables(self):
        """부하 레벨이 임계값 초과이면 비활성화된다."""
        config = HedgingConfig(disable_on_load_level="medium")
        policy = HedgingPolicy(config=config, initial_load_level="high")
        assert policy._should_disable_hedging() is True

    def test_unknown_level_defaults_to_0(self):
        """알 수 없는 부하 레벨은 기본값 0으로 처리된다."""
        config = HedgingConfig(disable_on_load_level="high")
        policy = HedgingPolicy(config=config, initial_load_level="unknown_level")
        assert policy._should_disable_hedging() is False


# =============================================================================
# 동작 검증 (Behavior) — _get_name 후보 이름 결정
# =============================================================================


class TestGetNameBehavior:
    """HedgingPolicy._get_name() 동작 검증."""

    def test_returns_custom_name_when_available(self, named_policy):
        """인덱스 범위 내에 이름이 있으면 그 이름을 반환한다."""
        assert named_policy._get_name(0, "default") == "my_primary"

    def test_returns_default_when_out_of_range(self, named_policy):
        """인덱스가 candidate_names 범위를 초과하면 기본값을 반환한다."""
        assert named_policy._get_name(10, "fallback_name") == "fallback_name"

    def test_returns_default_when_no_names(self, basic_policy):
        """candidate_names가 비어있으면 기본값을 반환한다."""
        assert basic_policy._get_name(0, "primary") == "primary"


# =============================================================================
# 동작 검증 (Behavior) — AsyncHedgingPolicy 실행 동작
# =============================================================================


class TestAsyncHedgingPolicyExecuteBehavior:
    """AsyncHedgingPolicy.execute() 동작 검증."""

    @pytest.mark.asyncio
    async def test_primary_success_returns_value(self, async_basic_policy):
        """Primary 비동기 함수 성공 시 값을 반환한다."""

        async def primary():
            return "async_primary"

        result = await async_basic_policy.execute(primary)
        assert result.success is True
        assert result.value is not None

    @pytest.mark.asyncio
    async def test_all_fail_with_default_returns_default(self):
        """모든 비동기 후보 실패 시 default_value를 반환한다."""

        async def failing():
            raise RuntimeError("async fail")

        async def failing_candidate():
            raise RuntimeError("async candidate fail")

        policy = AsyncHedgingPolicy(
            candidates=[failing_candidate],
            config=HedgingConfig(mode=HedgingMode.IMMEDIATE, delay=0.01, timeout=1.0),
            default_value="async_default",
        )
        result = await policy.execute(failing)
        assert result.value == "async_default"
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK

    @pytest.mark.asyncio
    async def test_disable_hedging_on_high_load(self):
        """비동기에서도 부하 레벨에 따라 헷징이 비활성화된다."""

        async def primary():
            return "async_primary"

        policy = AsyncHedgingPolicy(
            candidates=[lambda: None],
            config=HedgingConfig(delay=0.01, timeout=2.0, disable_on_load_level="high"),
            initial_load_level="high",
        )
        result = await policy.execute(primary)
        assert result.metadata["hedged"] is False


# =============================================================================
# 동작 검증 (Behavior) — AsyncHedgingPolicy Fail-Fast 타입 검사
# =============================================================================


class TestAsyncHedgingPolicyTypeCheckBehavior:
    """AsyncHedgingPolicy 생성 시점 타입 검사 동작 검증."""

    def test_non_protocol_per_candidate_raises_type_error(self):
        """AsyncResiliencePolicy Protocol을 충족하지 않는 객체는 TypeError가 발생한다."""

        class NotAPolicy:
            pass

        with pytest.raises(TypeError, match="AsyncResiliencePolicy"):
            AsyncHedgingPolicy(
                per_candidate_policy=NotAPolicy(),
            )

    def test_non_protocol_overall_raises_type_error(self):
        """AsyncResiliencePolicy Protocol을 충족하지 않는 객체는 TypeError가 발생한다."""

        class NotAPolicy:
            pass

        with pytest.raises(TypeError, match="AsyncResiliencePolicy"):
            AsyncHedgingPolicy(
                overall_policy=NotAPolicy(),
            )

    def test_missing_name_raises_type_error(self):
        """name property가 없는 객체는 TypeError가 발생한다."""

        class NoNamePolicy:
            async def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(value=await func(), outcome=PolicyOutcome.SUCCESS)

        with pytest.raises(TypeError, match="AsyncResiliencePolicy"):
            AsyncHedgingPolicy(
                per_candidate_policy=NoNamePolicy(),
            )

    def test_missing_execute_raises_type_error(self):
        """execute 메서드가 없는 객체는 TypeError가 발생한다."""

        class NoExecutePolicy:
            @property
            def name(self) -> str:
                return "no_execute"

        with pytest.raises(TypeError, match="AsyncResiliencePolicy"):
            AsyncHedgingPolicy(
                per_candidate_policy=NoExecutePolicy(),
            )

    def test_valid_async_per_candidate_accepted(self):
        """AsyncResiliencePolicy Protocol을 충족하는 객체는 에러 없이 생성된다."""

        class AsyncPolicy:
            @property
            def name(self) -> str:
                return "async_mock"

            async def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(value=await func(), outcome=PolicyOutcome.SUCCESS)

        policy = AsyncHedgingPolicy(per_candidate_policy=AsyncPolicy())
        assert policy._per_candidate_policy is not None

    def test_structural_typing_sync_with_protocol_shape_accepted(self):
        """runtime_checkable은 구조적 타입 검사만 수행하므로 name+execute가 있으면 통과한다."""

        class StructurallyMatching:
            @property
            def name(self) -> str:
                return "structural"

            def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(value=func(), outcome=PolicyOutcome.SUCCESS)

        # runtime_checkable은 async 여부를 구분하지 않으므로 통과됨
        policy = AsyncHedgingPolicy(per_candidate_policy=StructurallyMatching())
        assert policy._per_candidate_policy is not None


# =============================================================================
# 동작 검증 (Behavior) — AsyncHedgingPolicy on_config_updated
# =============================================================================


class TestAsyncHedgingPolicyOnConfigUpdatedBehavior:
    """AsyncHedgingPolicy.on_config_updated() 동작 검증."""

    def test_update_mode(self, async_basic_policy):
        """hedging.mode 이벤트로 모드가 변경된다."""
        async_basic_policy.on_config_updated(
            {"key": "hedging.mode", "value": "immediate"}
        )
        assert async_basic_policy._config.mode == HedgingMode.IMMEDIATE

    def test_update_delay(self, async_basic_policy):
        """hedging.delay 이벤트로 delay가 변경된다."""
        async_basic_policy.on_config_updated({"key": "hedging.delay", "value": 0.75})
        assert async_basic_policy._config.delay == pytest.approx(0.75)

    def test_update_load_level(self, async_basic_policy):
        """backpressure.level 이벤트로 부하 레벨이 변경된다."""
        async_basic_policy.on_config_updated(
            {"key": "backpressure.level", "value": "HIGH"}
        )
        assert async_basic_policy._current_load_level == "high"


# =============================================================================
# 동작 검증 (Behavior) — HedgingConfigUpdateHook
# =============================================================================


class TestHedgingConfigUpdateHookBehavior:
    """HedgingConfigUpdateHook 동작 검증."""

    def test_register_adds_policy(self):
        """register()로 Policy가 등록된다."""
        hook = HedgingConfigUpdateHook()
        policy = HedgingPolicy(config=HedgingConfig(delay=0.01))
        hook.register(policy)
        assert policy in hook._policies

    def test_register_multiple_policies(self):
        """여러 Policy를 등록할 수 있다."""
        hook = HedgingConfigUpdateHook()
        p1 = HedgingPolicy(config=HedgingConfig(delay=0.01))
        p2 = HedgingPolicy(config=HedgingConfig(delay=0.02))
        hook.register(p1)
        hook.register(p2)
        assert len(hook._policies) == 2

    def test_dispatch_updates_all_policies(self):
        """_dispatch()는 등록된 모든 Policy에 이벤트를 전달한다."""
        hook = HedgingConfigUpdateHook()
        p1 = HedgingPolicy(config=HedgingConfig(delay=0.01))
        p2 = HedgingPolicy(config=HedgingConfig(delay=0.01))
        hook.register(p1)
        hook.register(p2)

        event_data = {"key": "hedging.delay", "value": 0.99}
        hook._dispatch(event_data)
        assert p1._config.delay == pytest.approx(0.99)
        assert p2._config.delay == pytest.approx(0.99)

    def test_dispatch_with_event_data_attribute(self):
        """_dispatch()는 event.data 속성을 가진 이벤트도 처리한다."""
        hook = HedgingConfigUpdateHook()
        policy = HedgingPolicy(config=HedgingConfig(delay=0.01))
        hook.register(policy)

        class MockEvent:
            data = {"key": "hedging.delay", "value": 1.5}

        hook._dispatch(MockEvent())
        assert policy._config.delay == pytest.approx(1.5)

    def test_dispatch_fail_open_on_policy_error(self):
        """Policy.on_config_updated()가 예외를 던져도 다른 Policy에 계속 전달한다."""
        hook = HedgingConfigUpdateHook()

        # 예외를 발생시키는 가짜 policy
        bad_policy = MagicMock()
        bad_policy.on_config_updated = MagicMock(
            side_effect=RuntimeError("internal error")
        )
        hook.register(bad_policy)

        good_policy = HedgingPolicy(config=HedgingConfig(delay=0.01))
        hook.register(good_policy)

        event_data = {"key": "hedging.delay", "value": 2.0}
        hook._dispatch(event_data)  # 예외 없이 완료되어야 함

        assert good_policy._config.delay == pytest.approx(2.0)

    def test_start_without_eventbus_does_not_raise(self):
        """EventBus가 없는 환경에서 start()는 예외를 발생시키지 않는다."""
        hook = HedgingConfigUpdateHook()
        with patch(
            "baldur.resilience.policies.hedging.HedgingConfigUpdateHook.start",
            wraps=hook.start,
        ):
            hook.start()  # ImportError가 발생해도 안전

    def test_register_async_policy(self):
        """AsyncHedgingPolicy도 등록할 수 있다."""
        hook = HedgingConfigUpdateHook()
        async_policy = AsyncHedgingPolicy(config=HedgingConfig(delay=0.01))
        hook.register(async_policy)
        assert async_policy in hook._policies

    def test_dispatch_updates_async_policy(self):
        """_dispatch()는 AsyncHedgingPolicy에도 이벤트를 전달한다."""
        hook = HedgingConfigUpdateHook()
        async_policy = AsyncHedgingPolicy(config=HedgingConfig(delay=0.01))
        hook.register(async_policy)

        event_data = {"key": "hedging.delay", "value": 3.0}
        hook._dispatch(event_data)
        assert async_policy._config.delay == pytest.approx(3.0)


# =============================================================================
# 동작 검증 (Behavior) — AsyncHedgingPolicy _should_disable / _get_effective_delay
# =============================================================================


class TestAsyncHedgingPolicyBackpressureBehavior:
    """AsyncHedgingPolicy Backpressure 동작 검증."""

    def test_should_disable_at_threshold(self):
        """부하 레벨이 임계값과 동일하면 비활성화된다."""
        config = HedgingConfig(disable_on_load_level="medium")
        policy = AsyncHedgingPolicy(config=config, initial_load_level="medium")
        assert policy._should_disable_hedging() is True

    def test_should_not_disable_below_threshold(self):
        """부하 레벨이 임계값 미만이면 활성화된다."""
        config = HedgingConfig(disable_on_load_level="high")
        policy = AsyncHedgingPolicy(config=config, initial_load_level="low")
        assert policy._should_disable_hedging() is False

    def test_effective_delay_medium(self):
        """medium 부하에서 delay에 delay_multiplier_on_medium이 적용된다."""
        config = HedgingConfig(delay=0.1, delay_multiplier_on_medium=2.0)
        policy = AsyncHedgingPolicy(config=config, initial_load_level="medium")
        assert policy._get_effective_delay() == pytest.approx(
            config.delay * config.delay_multiplier_on_medium
        )

    def test_effective_delay_high(self):
        """high 부하에서 delay에 delay_multiplier_on_high가 적용된다."""
        config = HedgingConfig(delay=0.1, delay_multiplier_on_high=5.0)
        policy = AsyncHedgingPolicy(config=config, initial_load_level="high")
        assert policy._get_effective_delay() == pytest.approx(
            config.delay * config.delay_multiplier_on_high
        )

    def test_effective_delay_none_returns_base(self):
        """none 부하에서는 기본 delay를 반환한다."""
        config = HedgingConfig(delay=0.1)
        policy = AsyncHedgingPolicy(config=config, initial_load_level="none")
        assert policy._get_effective_delay() == pytest.approx(config.delay)


# =============================================================================
# 동작 검증 (Behavior) — HedgingPolicy 기본 생성자 동작
# =============================================================================


class TestHedgingPolicyInitBehavior:
    """HedgingPolicy 생성자 기본값 동작 검증."""

    def test_default_candidates_empty(self):
        """candidates 미지정 시 빈 리스트이다."""
        policy = HedgingPolicy()
        assert policy._candidates == []

    def test_default_candidate_names_empty(self):
        """candidate_names 미지정 시 빈 리스트이다."""
        policy = HedgingPolicy()
        assert policy._candidate_names == []

    def test_default_config_is_hedging_config(self):
        """config 미지정 시 HedgingConfig 기본 인스턴스가 생성된다."""
        policy = HedgingPolicy()
        assert isinstance(policy._config, HedgingConfig)

    def test_default_value_is_none(self):
        """default_value 미지정 시 None이다."""
        policy = HedgingPolicy()
        assert policy._default_value is None

    def test_default_per_candidate_policy_is_none(self):
        """per_candidate_policy 미지정 시 None이다."""
        policy = HedgingPolicy()
        assert policy._per_candidate_policy is None

    def test_default_overall_policy_is_none(self):
        """overall_policy 미지정 시 None이다."""
        policy = HedgingPolicy()
        assert policy._overall_policy is None

    def test_default_initial_load_level_is_none_string(self):
        """initial_load_level 미지정 시 'none'이다."""
        policy = HedgingPolicy()
        assert policy._current_load_level == "none"

    def test_custom_initial_load_level(self):
        """initial_load_level 설정 시 해당 값으로 초기화된다."""
        policy = HedgingPolicy(initial_load_level="medium")
        assert policy._current_load_level == "medium"

    def test_executor_is_hedging_executor(self):
        """내부 executor는 HedgingExecutor 인스턴스이다."""
        from baldur_pro.services.hedging.executor import HedgingExecutor

        policy = HedgingPolicy()
        assert isinstance(policy._executor, HedgingExecutor)


# =============================================================================
# 동작 검증 (Behavior) — AsyncHedgingPolicy 기본 생성자 동작
# =============================================================================


class TestAsyncHedgingPolicyInitBehavior:
    """AsyncHedgingPolicy 생성자 기본값 동작 검증."""

    def test_default_candidates_empty(self):
        """candidates 미지정 시 빈 리스트이다."""
        policy = AsyncHedgingPolicy()
        assert policy._candidates == []

    def test_default_config_is_hedging_config(self):
        """config 미지정 시 HedgingConfig 기본 인스턴스가 생성된다."""
        policy = AsyncHedgingPolicy()
        assert isinstance(policy._config, HedgingConfig)

    def test_default_initial_load_level(self):
        """initial_load_level 미지정 시 'none'이다."""
        policy = AsyncHedgingPolicy()
        assert policy._current_load_level == "none"

    def test_executor_is_async_hedging_executor(self):
        """내부 executor는 AsyncHedgingExecutor 인스턴스이다."""
        from baldur_pro.services.hedging.async_executor import AsyncHedgingExecutor

        policy = AsyncHedgingPolicy()
        assert isinstance(policy._executor, AsyncHedgingExecutor)


# =============================================================================
# 동작 검증 (Behavior) — AsyncHedgingPolicy _wrap_candidates_with_policy
# =============================================================================


class TestAsyncHedgingPolicyWrapCandidatesBehavior:
    """AsyncHedgingPolicy._wrap_candidates_with_policy() 동작 검증."""

    @pytest.mark.asyncio
    async def test_async_per_candidate_rejected_raises_runtime_error(self):
        """비동기 per_candidate_policy REJECTED → RuntimeError."""

        class AsyncRejectPolicy:
            @property
            def name(self):
                return "reject"

            async def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(value=None, outcome=PolicyOutcome.REJECTED)

        policy = AsyncHedgingPolicy(
            config=HedgingConfig(delay=0.01, timeout=2.0),
            per_candidate_policy=AsyncRejectPolicy(),
        )
        candidate = HedgingCandidate(name="test", fn=lambda: "orig", priority=0)
        wrapped = policy._wrap_candidates_with_policy([candidate])

        with pytest.raises(RuntimeError, match="rejected"):
            await wrapped[0].fn()

    @pytest.mark.asyncio
    async def test_async_per_candidate_timeout_raises_timeout_error(self):
        """비동기 per_candidate_policy TIMEOUT → TimeoutError."""

        class AsyncTimeoutPolicy:
            @property
            def name(self):
                return "timeout"

            async def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(value=None, outcome=PolicyOutcome.TIMEOUT)

        policy = AsyncHedgingPolicy(
            config=HedgingConfig(delay=0.01, timeout=2.0),
            per_candidate_policy=AsyncTimeoutPolicy(),
        )
        candidate = HedgingCandidate(name="test", fn=lambda: "orig", priority=0)
        wrapped = policy._wrap_candidates_with_policy([candidate])

        with pytest.raises(TimeoutError, match="timed out"):
            await wrapped[0].fn()

    @pytest.mark.asyncio
    async def test_async_per_candidate_success_returns_value(self):
        """비동기 per_candidate_policy SUCCESS → value를 벗긴다."""

        class AsyncOkPolicy:
            @property
            def name(self):
                return "ok"

            async def execute(self, func, *args, context=None, **kwargs):
                return PolicyResult(
                    value="async_ok_value", outcome=PolicyOutcome.SUCCESS
                )

        policy = AsyncHedgingPolicy(
            config=HedgingConfig(delay=0.01, timeout=2.0),
            per_candidate_policy=AsyncOkPolicy(),
        )
        candidate = HedgingCandidate(name="test", fn=lambda: "orig", priority=0)
        wrapped = policy._wrap_candidates_with_policy([candidate])

        result = await wrapped[0].fn()
        assert result == "async_ok_value"


# =============================================================================
# AsyncHedgingPolicy — disabled via settings toggle (doc 426 Phase A)
# =============================================================================


class TestAsyncHedgingPolicyToggleDisabledBehavior:
    """AsyncHedgingPolicy.execute() falls back to single execution when disabled."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from baldur.settings.hedging import reset_hedging_settings
        from baldur.settings.root import reset_config

        reset_hedging_settings()
        reset_config()
        yield
        reset_hedging_settings()
        reset_config()

    @pytest.mark.asyncio
    async def test_disabled_hedging_executes_primary_only(self, monkeypatch):
        """When hedging disabled, async execute runs only the primary function."""
        monkeypatch.setenv("BALDUR_HEDGING_ENABLED", "false")
        from baldur.settings.root import reset_config

        reset_config()

        candidate_called = False

        async def primary():
            return "primary_result"

        async def candidate():
            nonlocal candidate_called
            candidate_called = True
            return "candidate_result"

        policy = AsyncHedgingPolicy(
            candidates=[candidate],
            config=HedgingConfig(mode=HedgingMode.IMMEDIATE, timeout=2.0),
        )
        result = await policy.execute(primary)

        assert result.value == "primary_result"
        assert result.outcome == PolicyOutcome.SUCCESS
        assert candidate_called is False

    @pytest.mark.asyncio
    async def test_disabled_hedging_primary_failure_uses_default(self, monkeypatch):
        """When hedging disabled and primary fails with default_value, falls back."""
        monkeypatch.setenv("BALDUR_HEDGING_ENABLED", "false")
        from baldur.settings.root import reset_config

        reset_config()

        async def failing():
            raise ValueError("boom")

        policy = AsyncHedgingPolicy(
            candidates=[],
            config=HedgingConfig(mode=HedgingMode.DELAYED, timeout=2.0),
            default_value="fallback_val",
        )
        result = await policy.execute(failing)

        # _execute_single with default_value → SUCCESS_WITH_FALLBACK
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "fallback_val"
