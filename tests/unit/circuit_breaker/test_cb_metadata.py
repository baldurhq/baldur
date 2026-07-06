"""
CircuitBreakerStateData metadata field unit tests.

Targets:
- interfaces/repositories.py CircuitBreakerStateData.metadata
- services/circuit_breaker/service.py get_all_states() metadata serialization
"""

from __future__ import annotations

from dataclasses import fields

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import CircuitBreakerStateData
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService


class TestCircuitBreakerMetadataContract:
    """CircuitBreakerStateData metadata field contract."""

    def test_metadata_field_exists(self):
        field_names = [f.name for f in fields(CircuitBreakerStateData)]
        assert "metadata" in field_names

    def test_metadata_default_is_empty_dict(self):
        state = CircuitBreakerStateData(service_name="test")
        assert state.metadata == {}

    def test_metadata_default_factory_creates_independent_dicts(self):
        state1 = CircuitBreakerStateData(service_name="svc1")
        state2 = CircuitBreakerStateData(service_name="svc2")
        state1.metadata["key"] = "value"
        assert state2.metadata == {}

    def test_metadata_field_type_annotation(self):
        for f in fields(CircuitBreakerStateData):
            if f.name == "metadata":
                assert "dict" in str(f.type)
                break

    def test_get_all_states_includes_metadata_key(self):
        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("test_service")
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True),
            repository=repo,
        )
        states = service.get_all_states()
        assert len(states) == 1
        assert "metadata" in states[0]


class TestCircuitBreakerMetadataBehavior:
    """CircuitBreakerStateData metadata storage behavior."""

    def test_metadata_can_store_arbitrary_data(self):
        meta = {"region_id": "us-east-1", "tenant_id": "t-123"}
        state = CircuitBreakerStateData(service_name="svc", metadata=meta)
        assert state.metadata == meta

    def test_backward_compatibility_without_metadata(self):
        state = CircuitBreakerStateData(service_name="svc", state="open")
        assert state.service_name == "svc"
        assert state.state == "open"
        assert state.metadata == {}


class TestGetAllStatesSerializationBehavior:
    """get_all_states() metadata serialization behavior."""

    def test_empty_metadata_serialized_as_empty_dict(self):
        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("svc")

        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True),
            repository=repo,
        )
        states = service.get_all_states()
        assert states[0]["metadata"] == {}
