"""ServiceDependencyGraph internal-synchronization tests.

Verification techniques:
- Thread safety: register-vs-traversal race, lost-update check (§8.7)
- State transition: set_criticality hit/miss returns
- Exception/edge: cycle present during a locked traversal
"""

from __future__ import annotations

import threading

from baldur.core.dependency_graph import ServiceDependencyGraph


class TestDependencyGraphConcurrencyBehavior:
    """Registrations racing traversals neither raise nor lose updates."""

    def test_register_racing_traversal_completes_consistently(self):
        """Threads register while a traversal loop runs: no exception,
        and every registration lands (no lost multi-step update)."""
        graph = ServiceDependencyGraph()
        graph.register_service("root", depends_on=[])

        n_registrars = 4
        per_thread = 50
        errors: list[Exception] = []
        errors_lock = threading.Lock()
        stop = threading.Event()
        barrier = threading.Barrier(n_registrars + 2)  # +1 traverser, +1 main

        def registrar(worker_id: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                for n in range(per_thread):
                    graph.register_service(
                        f"svc-{worker_id}-{n}",
                        depends_on=["root"],
                        criticality="low",
                    )
            except Exception as e:  # pragma: no cover - failure diagnostics
                with errors_lock:
                    errors.append(e)

        def traverser() -> None:
            try:
                barrier.wait(timeout=5.0)
                while not stop.is_set():
                    graph.get_cascading_affected("root")
                    graph.get_dependents_recursive("root", max_depth=3)
                    graph.topological_sort_subset(["root"])
                    graph.get_critical_dependents("root")
            except Exception as e:  # pragma: no cover - failure diagnostics
                with errors_lock:
                    errors.append(e)

        registrars = [
            threading.Thread(target=registrar, args=(i,)) for i in range(n_registrars)
        ]
        traversal_thread = threading.Thread(target=traverser)
        for t in registrars:
            t.start()
        traversal_thread.start()
        barrier.wait(timeout=5.0)
        for t in registrars:
            t.join(timeout=10.0)
        stop.set()
        traversal_thread.join(timeout=10.0)

        assert errors == []
        expected = {
            f"svc-{i}-{n}" for i in range(n_registrars) for n in range(per_thread)
        }
        assert set(graph.get_dependents("root")) == expected

    def test_traversal_with_cycle_completes_and_returns_finite_results(self):
        """A cycle in the graph terminates the locked traversal (the
        circular-dependency signal is emitted after lock release)."""
        graph = ServiceDependencyGraph()
        graph.register_service("service-a", depends_on=[])
        graph.register_service("service-b", depends_on=["service-a"])
        # Manually create the cycle (abnormal input)
        graph._dependencies["service-a"].dependents.append("service-b")
        graph._dependencies["service-b"].dependents.append("service-a")

        results = graph.get_dependents_recursive("service-a", max_depth=5)

        assert ("service-b", 1) in results
        assert len(results) < 5  # finite, cycle not re-walked


class TestSetCriticalityBehavior:
    """set_criticality hit/miss state transitions."""

    def test_updates_registered_service_and_returns_true(self):
        """A registered service's criticality changes and is visible to
        criticality-filtered traversals."""
        graph = ServiceDependencyGraph()
        graph.register_service("db", depends_on=[])
        graph.register_service("api", depends_on=["db"], criticality="low")

        assert graph.set_criticality("api", "critical") is True
        assert graph.get_critical_dependents("db") == ["api"]

    def test_unknown_service_returns_false_and_creates_no_node(self):
        """An unregistered service is rejected without creating a node."""
        graph = ServiceDependencyGraph()

        assert graph.set_criticality("ghost", "critical") is False
        assert graph.get_dependents("ghost") == []
        assert graph.get_dependencies("ghost") == []
