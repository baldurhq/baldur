"""Who may inherit the cluster read's default, and what its failure carries.

``get_cluster_states()`` answers a *fleet-wide* question, so the interface's
default -- delegate to ``get_all_states()`` -- is correct only where this
process's view already **is** the cluster view. That holds for the in-memory
adapter, where one process is the cluster. It does not hold for an adapter
that keeps a local view in front of a shared store: there the delegate would
answer from rows another worker has since changed, and a verdict computed
from those under-counts OPEN circuits by exactly the rows this worker never
refreshed.

The override obligation is checked by derivation rather than by an authored
list: the class dictionary says which adapters define the method themselves,
so a new adapter that silently inherits the fallback shows up here.

Verification techniques applied:
- Contract: the default's delegation, and the error's ``extra_context()`` keys
- Negative assertion: the adapters in front of a shared store do not inherit
"""

from __future__ import annotations

import pytest

from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateRepository,
)
from baldur.services.circuit_breaker.exceptions import (
    CircuitBreakerError,
    CircuitBreakerStateUnavailableError,
)

OPERATION = "get_cluster_states"


class TestClusterStatesDefaultContract:
    """The interface default, and who is entitled to it."""

    def test_the_interface_default_delegates_to_get_all_states(self):
        """One method body, expressed as the delegation it is."""
        rows = [CircuitBreakerStateData(service_name="payment-api", state="open")]

        class _Adapter:
            """Only the one method the default is allowed to reach."""

            def get_all_states(self):
                return rows

        assert CircuitBreakerStateRepository.get_cluster_states(_Adapter()) is rows

    def test_the_in_memory_adapter_answers_both_reads_identically(self):
        """One process is the cluster, so the local view is the shared one."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("payment-api")
        repo.get_or_create("catalog-api")

        assert [s.service_name for s in repo.get_cluster_states()] == [
            s.service_name for s in repo.get_all_states()
        ]

    def test_the_in_memory_adapter_inherits_the_default(self):
        """It is entitled to the fallback, and takes it."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        assert "get_cluster_states" not in vars(InMemoryCircuitBreakerStateRepository)

    @pytest.mark.parametrize(
        "import_path",
        [
            "baldur.adapters.redis.circuit_breaker:RedisCircuitBreakerStateRepository",
            "baldur.adapters.memory.layered_repository.repository_operations:"
            "RepositoryOperationsMixin",
        ],
        ids=["redis", "layered"],
    )
    def test_adapters_in_front_of_a_shared_store_override_the_default(
        self, import_path: str
    ):
        """Inheriting the fallback here would substitute a partial view."""
        module_name, class_name = import_path.split(":")
        module = __import__(module_name, fromlist=[class_name])

        assert "get_cluster_states" in vars(getattr(module, class_name))


class TestClusterStateErrorContract:
    """The failure a consumer picks its own safe direction from."""

    def test_error_exposes_the_operation_and_the_reason(self):
        """Both fields reach structured logging through ``extra_context()``."""
        error = CircuitBreakerStateUnavailableError(OPERATION, "l2_timeout")

        assert error.extra_context() == {
            "operation": OPERATION,
            "reason": "l2_timeout",
        }

    def test_error_builds_a_message_naming_both_when_none_is_given(self):
        """The default message is readable without the structured fields."""
        error = CircuitBreakerStateUnavailableError(OPERATION, "backend_degraded")

        assert str(error) == (
            "Cluster state read 'get_cluster_states' unavailable: backend_degraded"
        )

    def test_error_keeps_an_explicit_message(self):
        """A caller-supplied message wins over the composed one."""
        error = CircuitBreakerStateUnavailableError(
            OPERATION, "l2_absent", "no shared store is configured"
        )

        assert str(error) == "no shared store is configured"
        assert error.reason == "l2_absent"

    def test_error_is_a_circuit_breaker_error(self):
        """It is catchable by the domain's own base, not only by name."""
        assert issubclass(CircuitBreakerStateUnavailableError, CircuitBreakerError)
