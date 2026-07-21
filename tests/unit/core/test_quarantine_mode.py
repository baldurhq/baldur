"""
Quarantine Mode 테스트.

"""


class TestQuarantineMode:
    """Quarantine Mode 기능 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.core.cluster_identity import (
            reset_cluster_identity,
            reset_quarantine_state,
        )

        reset_cluster_identity()
        reset_quarantine_state()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.core.cluster_identity import (
            reset_cluster_identity,
            reset_quarantine_state,
        )

        reset_cluster_identity()
        reset_quarantine_state()

    def test_is_quarantine_mode_initially_false(self):
        """초기 Quarantine Mode는 False."""
        from baldur.core.cluster_identity import is_quarantine_mode

        assert is_quarantine_mode() is False

    def test_set_quarantine_mode_manually(self):
        """수동으로 Quarantine Mode 설정."""
        from baldur.core.cluster_identity import (
            is_quarantine_mode,
            set_quarantine_mode,
        )

        set_quarantine_mode(True)
        assert is_quarantine_mode() is True

        set_quarantine_mode(False)
        assert is_quarantine_mode() is False

    def test_validate_returns_false_on_invalid_cluster_id(self):
        """잘못된 cluster_id일 때 validate()가 False를 반환.

        453 D5: factory body에서 quarantine flip을 제거했으므로,
        invalid identity가 quarantine을 자동 설정하지 않음. 검증 실패 신호는
        ``validate(fail_fast=False) → False``를 통해 호출자에게 전달되고,
        실제 quarantine flip은 bootstrap에서 책임진다.
        """
        from baldur.core.cluster_identity import (
            ClusterIdentity,
            is_quarantine_mode,
        )

        identity = ClusterIdentity(cluster_id="default", region=None)
        assert identity.validate(fail_fast=False) is False
        # No automatic quarantine flip — validate() is pure inspection.
        assert is_quarantine_mode() is False

    def test_validate_returns_true_on_valid_cluster_id(self):
        """유효한 cluster_id/region일 때 validate()가 True를 반환."""
        from baldur.core.cluster_identity import (
            ClusterIdentity,
            is_quarantine_mode,
        )

        identity = ClusterIdentity(cluster_id="seoul-prod-01", region="seoul")
        assert identity.validate(fail_fast=False) is True
        assert is_quarantine_mode() is False

    def test_reset_quarantine_state_clears_flag(self):
        """``reset_quarantine_state()``가 flag를 초기화 (453 D2).

        cluster_identity 싱글톤 생성 여부와 무관하게 ``set_quarantine_mode(True)``
        를 호출한 후의 leak를 닫는 path. autodiscovery가 conftest의
        module-scope reset에서 이 함수를 호출하여 xdist worker 간 leak를 차단.
        """
        from baldur.core.cluster_identity import (
            is_quarantine_mode,
            reset_quarantine_state,
            set_quarantine_mode,
        )

        # No get_cluster_identity() call — verify the flag clears even when
        # the cluster_identity singleton was never created (the original
        # cleanup_fn-based path could not handle this case).
        set_quarantine_mode(True)
        assert is_quarantine_mode() is True

        reset_quarantine_state()
        assert is_quarantine_mode() is False


class TestResetQuarantineStateBehavior:
    """``reset_quarantine_state`` (453 D2) — keyed singleton with autodiscovered reset.

    Promoting ``_quarantine_state`` to its own ``make_singleton_factory`` triple
    closes the G6 leak vector: a test that calls ``set_quarantine_mode(True)``
    without ever instantiating ``cluster_identity`` previously left the flag
    stuck across module boundaries because the cleanup_fn only fired for
    cluster_identity itself. The reset is now autodiscovered by
    ``tests/conftest.py``'s module-scope reset phase.
    """

    def setup_method(self):
        from baldur.core.cluster_identity import (
            reset_cluster_identity,
            reset_quarantine_state,
        )

        reset_cluster_identity()
        reset_quarantine_state()

    def teardown_method(self):
        from baldur.core.cluster_identity import (
            reset_cluster_identity,
            reset_quarantine_state,
        )

        reset_cluster_identity()
        reset_quarantine_state()

    def test_reset_is_idempotent(self):
        """Calling ``reset_quarantine_state`` twice in a row does not raise.

        Idempotency matters for the conftest autodiscovery loop — every reset
        function on the registered list runs unconditionally between modules,
        even when the singleton was never created.
        """
        from baldur.core.cluster_identity import (
            is_quarantine_mode,
            reset_quarantine_state,
        )

        reset_quarantine_state()
        reset_quarantine_state()
        assert is_quarantine_mode() is False

    def test_reset_independent_of_cluster_identity_singleton(self):
        """G6 leak repro: flag clears even when cluster_identity was never created.

        The original ``cleanup_fn`` path in cluster_identity's factory only ran
        when the singleton existed; ``set_quarantine_mode(True)`` followed by
        ``reset_cluster_identity()`` left the flag stuck. The new
        ``reset_quarantine_state`` is its own autodiscovered triple, so the
        flag clears regardless of cluster_identity ownership.
        """
        from baldur.core.cluster_identity import (
            is_quarantine_mode,
            reset_cluster_identity,
            reset_quarantine_state,
            set_quarantine_mode,
        )

        # Given — quarantine flipped without ever instantiating cluster_identity.
        set_quarantine_mode(True)
        assert is_quarantine_mode() is True

        # When — only reset cluster_identity (the OLD path).
        reset_cluster_identity()
        # The flag would still be stuck under the old cleanup_fn-only design.
        assert is_quarantine_mode() is True

        # Then — explicit quarantine reset clears it.
        reset_quarantine_state()
        assert is_quarantine_mode() is False

    def test_reset_quarantine_state_is_registered_in_singleton_registry(self):
        """453 D2: ``quarantine_state`` is its own keyed singleton triple.

        Verifies the keyed name is registered with ``make_singleton_factory``,
        which is the contract conftest autodiscovery scans.
        """
        from baldur.utils.singleton import _REGISTRY

        assert "quarantine_state" in _REGISTRY

    def test_reset_quarantine_state_does_not_disturb_cluster_identity(self):
        """Resetting quarantine must not drop the cluster_identity singleton.

        Independence of the two factories is the whole point of D2 — they share
        no state, so resetting one cannot reach into the other.
        """
        from baldur.core.cluster_identity import (
            get_cluster_identity,
            reset_quarantine_state,
        )

        first = get_cluster_identity()
        reset_quarantine_state()
        second = get_cluster_identity()

        assert first is second


class TestPropagatorQuarantineMode:
    """GlobalConfigPropagator의 Quarantine Mode 동작 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.core.cluster_identity import (
            reset_cluster_identity,
            reset_quarantine_state,
        )

        reset_cluster_identity()
        reset_quarantine_state()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.core.cluster_identity import (
            reset_cluster_identity,
            reset_quarantine_state,
        )

        reset_cluster_identity()
        reset_quarantine_state()

    def test_propagate_blocked_in_quarantine_mode(self):
        """Quarantine Mode에서는 전파가 차단됨."""
        from baldur.core.cluster_identity import set_quarantine_mode
        from baldur.services.config.propagator import (
            ConfigScope,
            GlobalConfigChange,
            GlobalConfigPropagator,
            PropagationTier,
        )

        set_quarantine_mode(True)

        propagator = GlobalConfigPropagator(redis_client=None)

        change = GlobalConfigChange(
            config_type="test",
            config_key="key1",
            new_value="value1",
            previous_value=None,
            scope=ConfigScope.GLOBAL,
            tier=PropagationTier.TIER_1_IMMEDIATE,
            source_cluster="test-cluster",
        )

        result = propagator.propagate(change)
        assert result is False  # Quarantine Mode에서는 전파 실패
