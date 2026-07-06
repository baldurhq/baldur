"""
Tests for 321 — Celery Beat Schedule Internalization.

Covers:
- kombu Queue/Exchange definitions (Q4)
- Queue namespace isolation via get_baldur_queues(prefix) (Q3)
- Task routes via get_baldur_task_routes(prefix) (Q3/Q6)
- configure_baldur_celery(app) wrapper (Q6)
- BALDUR_QUEUE_CONFIG backward-compatible dict
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kombu import Queue

from baldur.adapters.celery.beat_schedule import (
    _CRITICAL_TASK_ROUTES,
    _QUEUE_DEFINITIONS,
    BALDUR_QUEUE_CONFIG,
    _baldur_dlx,
    _baldur_exchange,
    _reset_celery_configured,
    configure_baldur_celery,
    get_baldur_beat_schedule,
    get_baldur_queues,
    get_baldur_task_routes,
)


# Reset idempotency guard before each test in this module
@pytest.fixture(autouse=True)
def _reset_configure_guard():
    _reset_celery_configured()
    yield
    _reset_celery_configured()


# =============================================================================
# Queue/Exchange Definition Contract Tests (Q4)
# =============================================================================


class TestQueueDefinitionsContract:
    """kombu Queue/Exchange definitions contract values from 321 design doc."""

    def test_baldur_exchange_name(self):
        """Main exchange is named 'baldur' with direct type."""
        assert _baldur_exchange.name == "baldur"
        assert _baldur_exchange.type == "direct"
        assert _baldur_exchange.durable is True

    def test_baldur_dlx_exchange_name(self):
        """Dead Letter Exchange is named 'baldur.dlx' with direct type."""
        assert _baldur_dlx.name == "baldur.dlx"
        assert _baldur_dlx.type == "direct"
        assert _baldur_dlx.durable is True

    def test_queue_definitions_count(self):
        """11 queues are defined per the 321 design doc."""
        assert len(_QUEUE_DEFINITIONS) == 11

    def test_all_queue_names_present(self):
        """All 11 expected queue names exist."""
        names = {q.name for q in _QUEUE_DEFINITIONS}
        expected = {
            "maintenance",
            "critical_maintenance",
            "analysis",
            "realtime",
            "compliance",
            "reports",
            "metrics",
            "audit_flush",
            "chaos",
            "chaos_monitoring",
            "baldur.critical",
        }
        assert names == expected

    def test_all_queues_have_quorum_type(self):
        """Every queue uses x-queue-type: quorum."""
        for q in _QUEUE_DEFINITIONS:
            assert q.queue_arguments.get("x-queue-type") == "quorum", (
                f"Queue '{q.name}' missing quorum type"
            )

    def test_dlx_bound_queues(self):
        """critical_maintenance, realtime, baldur.critical have DLX binding."""
        dlx_queues = {
            q.name
            for q in _QUEUE_DEFINITIONS
            if q.queue_arguments.get("x-dead-letter-exchange")
        }
        assert dlx_queues == {
            "critical_maintenance",
            "realtime",
            "baldur.critical",
        }

    def test_dlx_value_is_baldur_dlx(self):
        """DLX-bound queues reference 'baldur.dlx' exchange."""
        for q in _QUEUE_DEFINITIONS:
            dlx = q.queue_arguments.get("x-dead-letter-exchange")
            if dlx:
                assert dlx == "baldur.dlx", (
                    f"Queue '{q.name}' DLX should be 'baldur.dlx', got '{dlx}'"
                )

    def test_realtime_queue_has_ttl(self):
        """Realtime queue has x-message-ttl: 30000 (30s)."""
        realtime = next(q for q in _QUEUE_DEFINITIONS if q.name == "realtime")
        assert realtime.queue_arguments["x-message-ttl"] == 30000

    def test_critical_queue_uses_dedicated_exchange(self):
        """baldur.critical queue has its own exchange, not shared."""
        critical = next(q for q in _QUEUE_DEFINITIONS if q.name == "baldur.critical")
        assert critical.exchange.name == "baldur.critical"
        assert critical.exchange.type == "direct"
        assert critical.exchange.durable is True

    def test_chaos_monitoring_routing_key(self):
        """chaos_monitoring queue uses 'chaos.monitoring' routing key (dot notation)."""
        cm = next(q for q in _QUEUE_DEFINITIONS if q.name == "chaos_monitoring")
        assert cm.routing_key == "chaos.monitoring"

    def test_queue_priority_values(self):
        """Queue x-max-priority values match the 321 spec."""
        priorities = {
            q.name: q.queue_arguments["x-max-priority"] for q in _QUEUE_DEFINITIONS
        }
        assert priorities["maintenance"] == 3
        assert priorities["critical_maintenance"] == 10
        assert priorities["analysis"] == 5
        assert priorities["realtime"] == 10
        assert priorities["compliance"] == 7
        assert priorities["reports"] == 2
        assert priorities["metrics"] == 1
        assert priorities["audit_flush"] == 4
        assert priorities["chaos"] == 5
        assert priorities["chaos_monitoring"] == 6
        assert priorities["baldur.critical"] == 10


# =============================================================================
# BALDUR_QUEUE_CONFIG Backward Compatibility Contract
# =============================================================================


class TestQueueConfigDictContract:
    """BALDUR_QUEUE_CONFIG dict stays in sync with kombu Queue definitions."""

    def test_dict_keys_match_queue_names(self):
        """Dict keys match _QUEUE_DEFINITIONS queue names."""
        expected_keys = {q.name for q in _QUEUE_DEFINITIONS}
        assert set(BALDUR_QUEUE_CONFIG.keys()) == expected_keys

    def test_dict_contains_exchange_and_routing_key(self):
        """Each dict entry has exchange, routing_key, and queue_arguments."""
        for name, config in BALDUR_QUEUE_CONFIG.items():
            assert "exchange" in config, f"{name}: missing 'exchange'"
            assert "routing_key" in config, f"{name}: missing 'routing_key'"
            assert "queue_arguments" in config, f"{name}: missing 'queue_arguments'"

    def test_dict_values_match_kombu_objects(self):
        """Dict values match the corresponding kombu Queue attributes."""
        for q in _QUEUE_DEFINITIONS:
            config = BALDUR_QUEUE_CONFIG[q.name]
            assert config["exchange"] == q.exchange.name
            assert config["routing_key"] == q.routing_key
            assert config["queue_arguments"] == (q.queue_arguments or {})


# =============================================================================
# get_baldur_queues() Behavior Tests (Q3)
# =============================================================================


class TestGetBaldurQueuesBehavior:
    """get_baldur_queues() factory function behavior."""

    def test_no_prefix_returns_all_queues(self):
        """Without prefix, returns the same number of queues as _QUEUE_DEFINITIONS."""
        queues = get_baldur_queues()
        assert len(queues) == len(_QUEUE_DEFINITIONS)

    def test_no_prefix_returns_original_names(self):
        """Without prefix, queue names match _QUEUE_DEFINITIONS."""
        queues = get_baldur_queues()
        names = {q.name for q in queues}
        expected = {q.name for q in _QUEUE_DEFINITIONS}
        assert names == expected

    def test_no_prefix_returns_new_list_instance(self):
        """Without prefix, returns a new list (not the original)."""
        queues = get_baldur_queues()
        assert queues is not _QUEUE_DEFINITIONS

    def test_prefix_applies_to_queue_name(self):
        """With prefix, all queue names get the prefix."""
        queues = get_baldur_queues(prefix="shopping")
        for q in queues:
            assert q.name.startswith("shopping."), f"Queue '{q.name}' missing prefix"

    def test_prefix_applies_to_exchange_name(self):
        """With prefix, all exchange names get the prefix."""
        queues = get_baldur_queues(prefix="shopping")
        for q in queues:
            assert q.exchange.name.startswith("shopping."), (
                f"Exchange '{q.exchange.name}' on queue '{q.name}' missing prefix"
            )

    def test_prefix_applies_to_routing_key(self):
        """With prefix, all routing keys get the prefix."""
        queues = get_baldur_queues(prefix="shopping")
        for q in queues:
            assert q.routing_key.startswith("shopping."), (
                f"Routing key '{q.routing_key}' on queue '{q.name}' missing prefix"
            )

    def test_prefix_preserves_queue_arguments(self):
        """With prefix, queue_arguments are preserved identically."""
        original = get_baldur_queues()
        prefixed = get_baldur_queues(prefix="myapp")
        for orig, pref in zip(original, prefixed, strict=False):
            assert pref.queue_arguments == orig.queue_arguments

    def test_prefix_preserves_exchange_properties(self):
        """With prefix, exchange type and durable are preserved."""
        original = get_baldur_queues()
        prefixed = get_baldur_queues(prefix="myapp")
        for orig, pref in zip(original, prefixed, strict=False):
            assert pref.exchange.type == orig.exchange.type
            assert pref.exchange.durable == orig.exchange.durable

    def test_prefixed_critical_queue_name(self):
        """baldur.critical becomes 'shopping.baldur.critical'."""
        queues = get_baldur_queues(prefix="shopping")
        names = {q.name for q in queues}
        assert "shopping.baldur.critical" in names

    def test_empty_prefix_treated_as_no_prefix(self):
        """Empty string prefix returns unprefixed queues."""
        queues = get_baldur_queues(prefix="")
        names = {q.name for q in queues}
        expected = {q.name for q in _QUEUE_DEFINITIONS}
        assert names == expected


# =============================================================================
# get_baldur_task_routes() Behavior Tests (Q3/Q6)
# =============================================================================


class TestGetBaldurTaskRoutesBehavior:
    """get_baldur_task_routes() factory function behavior."""

    def test_returns_four_critical_task_routes(self):
        """4 critical tasks are routed per the 321 spec."""
        routes = get_baldur_task_routes()
        assert len(routes) == 4

    def test_all_critical_tasks_present(self):
        """All 4 critical task names are in the routes."""
        routes = get_baldur_task_routes()
        expected_tasks = set(_CRITICAL_TASK_ROUTES.keys())
        assert set(routes.keys()) == expected_tasks

    def test_no_prefix_routes_to_baldur_critical(self):
        """Without prefix, all routes point to 'baldur.critical'."""
        routes = get_baldur_task_routes()
        for task_name, route in routes.items():
            assert route["queue"] == "baldur.critical", (
                f"Task '{task_name}' should route to 'baldur.critical'"
            )
            assert route["routing_key"] == "baldur.critical"

    def test_prefix_applies_to_queue_in_routes(self):
        """With prefix, queue names in routes get the prefix."""
        routes = get_baldur_task_routes(prefix="shopping")
        for _task_name, route in routes.items():
            assert route["queue"] == "shopping.baldur.critical"
            assert route["routing_key"] == "shopping.baldur.critical"

    def test_each_route_has_queue_and_routing_key(self):
        """Each route entry has both 'queue' and 'routing_key' keys."""
        routes = get_baldur_task_routes()
        for task_name, route in routes.items():
            assert "queue" in route, f"Route for '{task_name}' missing 'queue'"
            assert "routing_key" in route, (
                f"Route for '{task_name}' missing 'routing_key'"
            )

    def test_empty_prefix_treated_as_no_prefix(self):
        """Empty string prefix returns unprefixed routes."""
        routes = get_baldur_task_routes(prefix="")
        for route in routes.values():
            assert route["queue"] == "baldur.critical"


# =============================================================================
# configure_baldur_celery() Behavior Tests (Q6)
# =============================================================================


class TestConfigureBaldurCeleryBehavior:
    """configure_baldur_celery() wrapper behavior."""

    @pytest.fixture
    def mock_celery_app(self):
        """Celery app mock with realistic conf structure."""
        app = MagicMock()
        app.conf.beat_schedule = {}
        app.conf.task_queues = []
        app.conf.task_routes = {}
        return app

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_merges_beat_schedule(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Beat schedule is merged into app.conf.beat_schedule."""
        mock_get_schedule.return_value = {
            "baldur-task-1": {"task": "test.task", "schedule": 60.0}
        }

        configure_baldur_celery(mock_celery_app)

        assert "baldur-task-1" in mock_celery_app.conf.beat_schedule

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_preserves_existing_beat_schedule(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Existing consumer beat schedule entries are preserved."""
        mock_celery_app.conf.beat_schedule = {
            "consumer-task": {"task": "consumer.task", "schedule": 300.0}
        }
        mock_get_schedule.return_value = {
            "baldur-task-1": {"task": "sh.task", "schedule": 60.0}
        }

        configure_baldur_celery(mock_celery_app)

        assert "consumer-task" in mock_celery_app.conf.beat_schedule
        assert "baldur-task-1" in mock_celery_app.conf.beat_schedule

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_merges_task_queues(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """kombu Queue objects are appended to app.conf.task_queues."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)

        queues = mock_celery_app.conf.task_queues
        assert len(queues) == len(_QUEUE_DEFINITIONS)
        assert all(isinstance(q, Queue) for q in queues)

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_preserves_existing_task_queues(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Existing consumer queues are preserved."""
        existing_queue = Queue("consumer-queue")
        mock_celery_app.conf.task_queues = [existing_queue]
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)

        queues = mock_celery_app.conf.task_queues
        assert queues[0] is existing_queue
        assert len(queues) == 1 + len(_QUEUE_DEFINITIONS)

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_merges_task_routes(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Critical task routes are merged into app.conf.task_routes."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)

        routes = mock_celery_app.conf.task_routes
        assert len(routes) == len(_CRITICAL_TASK_ROUTES)

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_preserves_existing_task_routes(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Existing consumer routes are preserved."""
        mock_celery_app.conf.task_routes = {
            "consumer.task.*": {"queue": "consumer-queue"}
        }
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)

        routes = mock_celery_app.conf.task_routes
        assert "consumer.task.*" in routes
        assert len(routes) == 1 + len(_CRITICAL_TASK_ROUTES)

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_calls_register_all_tasks(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """register_all_tasks_with_celery is called with the app."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)

        mock_register.assert_called_once_with(mock_celery_app)

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_queue_prefix_flows_to_queues(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """queue_prefix parameter applies prefix to all injected queues."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app, queue_prefix="shopping")

        queues = mock_celery_app.conf.task_queues
        for q in queues:
            assert q.name.startswith("shopping."), (
                f"Queue '{q.name}' should have 'shopping.' prefix"
            )

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_queue_prefix_flows_to_routes(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """queue_prefix parameter applies prefix to all injected routes."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app, queue_prefix="shopping")

        routes = mock_celery_app.conf.task_routes
        for task_name, route in routes.items():
            if task_name.startswith("baldur."):
                assert route["queue"].startswith("shopping.")

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_include_flags_passed_to_get_schedule(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """include_* flags are forwarded to get_baldur_beat_schedule."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(
            mock_celery_app,
            include_intelligence=False,
            include_saga=False,
        )

        call_kwargs = mock_get_schedule.call_args[1]
        assert call_kwargs["include_intelligence"] is False
        assert call_kwargs["include_saga"] is False
        assert call_kwargs["include_cleanup"] is True

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_handles_none_beat_schedule(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Works when app.conf.beat_schedule is None."""
        mock_celery_app.conf.beat_schedule = None
        mock_get_schedule.return_value = {"task-1": {"task": "t", "schedule": 60}}

        configure_baldur_celery(mock_celery_app)

        assert "task-1" in mock_celery_app.conf.beat_schedule

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_handles_none_task_queues(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Works when app.conf.task_queues is None."""
        mock_celery_app.conf.task_queues = None
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)

        assert len(mock_celery_app.conf.task_queues) == len(_QUEUE_DEFINITIONS)

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_handles_none_task_routes(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Works when app.conf.task_routes is None."""
        mock_celery_app.conf.task_routes = None
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)

        assert len(mock_celery_app.conf.task_routes) == len(_CRITICAL_TASK_ROUTES)


# =============================================================================
# Critical Task Routes Contract (Q6)
# =============================================================================


class TestCriticalTaskRoutesContract:
    """_CRITICAL_TASK_ROUTES contract values from 321 + consumer celery.py."""

    def test_execute_recovery_step_routed(self):
        """execute_recovery_step routes to baldur.critical."""
        assert (
            _CRITICAL_TASK_ROUTES["baldur.celery_tasks.execute_recovery_step"]
            == "baldur.critical"
        )

    def test_check_recovery_trigger_routed(self):
        """check_recovery_trigger routes to baldur.critical."""
        assert (
            _CRITICAL_TASK_ROUTES["baldur.celery_tasks.check_recovery_trigger"]
            == "baldur.critical"
        )

    def test_monitor_recovery_health_routed(self):
        """monitor_recovery_health routes to baldur.critical."""
        assert (
            _CRITICAL_TASK_ROUTES["baldur.celery_tasks.monitor_recovery_health"]
            == "baldur.critical"
        )

    def test_check_circuit_breaker_recovery_routed(self):
        """check_circuit_breaker_recovery routes to baldur.critical."""
        assert (
            _CRITICAL_TASK_ROUTES["baldur.celery_tasks.check_circuit_breaker_recovery"]
            == "baldur.critical"
        )


# =============================================================================
# Data Immutability Behavior Tests
# =============================================================================


class TestQueueDefinitionsImmutabilityBehavior:
    """Verify get_baldur_queues does not mutate _QUEUE_DEFINITIONS."""

    def test_get_queues_does_not_mutate_original(self):
        """Calling get_baldur_queues with prefix does not alter _QUEUE_DEFINITIONS."""
        original_names = [q.name for q in _QUEUE_DEFINITIONS]

        get_baldur_queues(prefix="test-prefix")

        current_names = [q.name for q in _QUEUE_DEFINITIONS]
        assert current_names == original_names

    def test_get_routes_does_not_mutate_original(self):
        """Calling get_baldur_task_routes with prefix does not alter _CRITICAL_TASK_ROUTES."""
        original_routes = dict(_CRITICAL_TASK_ROUTES)

        get_baldur_task_routes(prefix="test-prefix")

        assert _CRITICAL_TASK_ROUTES == original_routes


# =============================================================================
# get_baldur_queues() queue_type / enable_dlx Tests (Finding #2)
# =============================================================================


class TestGetBaldurQueuesSettingsWiring:
    """get_baldur_queues() queue_type and enable_dlx parameter behavior."""

    def test_default_queue_type_is_quorum(self):
        """Default queue_type='quorum' preserves original queue arguments."""
        queues = get_baldur_queues()
        for q in queues:
            assert q.queue_arguments["x-queue-type"] == "quorum"

    def test_queue_type_classic_applies_to_all(self):
        """queue_type='classic' overrides x-queue-type on all queues."""
        queues = get_baldur_queues(queue_type="classic")
        for q in queues:
            assert q.queue_arguments["x-queue-type"] == "classic", (
                f"Queue '{q.name}' should have classic type"
            )

    def test_queue_type_stream_applies_to_all(self):
        """queue_type='stream' overrides x-queue-type on all queues."""
        queues = get_baldur_queues(queue_type="stream")
        for q in queues:
            assert q.queue_arguments["x-queue-type"] == "stream"

    def test_enable_dlx_true_preserves_dlx_bindings(self):
        """enable_dlx=True (default) keeps DLX bindings on critical queues."""
        queues = get_baldur_queues(enable_dlx=True)
        dlx_names = {
            q.name for q in queues if q.queue_arguments.get("x-dead-letter-exchange")
        }
        assert dlx_names == {"critical_maintenance", "realtime", "baldur.critical"}

    def test_enable_dlx_false_removes_all_dlx_bindings(self):
        """enable_dlx=False removes x-dead-letter-exchange from all queues."""
        queues = get_baldur_queues(enable_dlx=False)
        for q in queues:
            assert "x-dead-letter-exchange" not in q.queue_arguments, (
                f"Queue '{q.name}' should not have DLX when enable_dlx=False"
            )

    def test_enable_dlx_false_preserves_other_arguments(self):
        """enable_dlx=False only removes DLX, other arguments are preserved."""
        queues = get_baldur_queues(enable_dlx=False)
        realtime = next(q for q in queues if q.name == "realtime")
        assert realtime.queue_arguments["x-max-priority"] == 10
        assert realtime.queue_arguments["x-message-ttl"] == 30000
        assert realtime.queue_arguments["x-queue-type"] == "quorum"

    def test_queue_type_and_prefix_combined(self):
        """queue_type and prefix work together."""
        queues = get_baldur_queues(prefix="shopping", queue_type="classic")
        for q in queues:
            assert q.name.startswith("shopping.")
            assert q.queue_arguments["x-queue-type"] == "classic"

    def test_queue_type_does_not_mutate_originals(self):
        """Calling with non-default queue_type does not alter _QUEUE_DEFINITIONS."""
        original_types = [q.queue_arguments["x-queue-type"] for q in _QUEUE_DEFINITIONS]

        get_baldur_queues(queue_type="classic")

        current_types = [q.queue_arguments["x-queue-type"] for q in _QUEUE_DEFINITIONS]
        assert current_types == original_types

    def test_enable_dlx_false_does_not_mutate_originals(self):
        """Calling with enable_dlx=False does not alter _QUEUE_DEFINITIONS."""
        original_dlx_count = sum(
            1
            for q in _QUEUE_DEFINITIONS
            if "x-dead-letter-exchange" in q.queue_arguments
        )

        get_baldur_queues(enable_dlx=False)

        current_dlx_count = sum(
            1
            for q in _QUEUE_DEFINITIONS
            if "x-dead-letter-exchange" in q.queue_arguments
        )
        assert current_dlx_count == original_dlx_count


# =============================================================================
# configure_baldur_celery() Beat Schedule Prefix Tests (Finding #3)
# =============================================================================


class TestConfigureBeatScheduleQueuePrefix:
    """queue_prefix is applied to beat schedule options.queue entries."""

    @pytest.fixture
    def mock_celery_app(self):
        """Celery app mock with realistic conf structure."""
        app = MagicMock()
        app.conf.beat_schedule = {}
        app.conf.task_queues = []
        app.conf.task_routes = {}
        return app

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_prefix_applies_to_beat_schedule_queue_options(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """queue_prefix is injected into beat schedule options.queue values."""
        mock_get_schedule.return_value = {
            "cleanup-task": {
                "task": "baldur.tasks.cleanup",
                "schedule": 60.0,
                "options": {"queue": "maintenance"},
            },
            "analysis-task": {
                "task": "baldur.tasks.analysis",
                "schedule": 120.0,
                "options": {"queue": "analysis"},
            },
        }

        configure_baldur_celery(mock_celery_app, queue_prefix="shopping")

        schedule = mock_celery_app.conf.beat_schedule
        assert schedule["cleanup-task"]["options"]["queue"] == "shopping.maintenance"
        assert schedule["analysis-task"]["options"]["queue"] == "shopping.analysis"

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_no_prefix_leaves_beat_schedule_queue_options_unchanged(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Without prefix, beat schedule options.queue values are unchanged."""
        mock_get_schedule.return_value = {
            "cleanup-task": {
                "task": "baldur.tasks.cleanup",
                "schedule": 60.0,
                "options": {"queue": "maintenance"},
            },
        }

        configure_baldur_celery(mock_celery_app)

        schedule = mock_celery_app.conf.beat_schedule
        assert schedule["cleanup-task"]["options"]["queue"] == "maintenance"

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_prefix_skips_entries_without_queue_option(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Entries without options.queue are not affected by prefix."""
        mock_get_schedule.return_value = {
            "no-queue-task": {
                "task": "baldur.tasks.simple",
                "schedule": 60.0,
            },
        }

        configure_baldur_celery(mock_celery_app, queue_prefix="shopping")

        schedule = mock_celery_app.conf.beat_schedule
        assert "options" not in schedule["no-queue-task"] or "queue" not in schedule[
            "no-queue-task"
        ].get("options", {})


# =============================================================================
# configure_baldur_celery() Idempotency Tests (Finding #5)
# =============================================================================


class TestConfigureIdempotencyGuard:
    """configure_baldur_celery() idempotency guard behavior."""

    @pytest.fixture
    def mock_celery_app(self):
        """Celery app mock with realistic conf structure."""
        app = MagicMock()
        app.conf.beat_schedule = {}
        app.conf.task_queues = []
        app.conf.task_routes = {}
        return app

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_second_call_is_noop(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Second call to configure_baldur_celery is a no-op."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)
        configure_baldur_celery(mock_celery_app)

        mock_register.assert_called_once()

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_second_call_does_not_duplicate_queues(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """Queues are not duplicated on second call."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)
        first_count = len(mock_celery_app.conf.task_queues)

        configure_baldur_celery(mock_celery_app)
        second_count = len(mock_celery_app.conf.task_queues)

        assert first_count == second_count

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_reset_allows_reconfiguration(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """_reset_celery_configured() allows a fresh configure call."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app)
        _reset_celery_configured()
        configure_baldur_celery(mock_celery_app)

        assert mock_register.call_count == 2


# =============================================================================
# configure_baldur_celery() queue_type/enable_dlx Forwarding (Finding #2)
# =============================================================================


class TestIncludeConfigApplyGating:
    """include_config_apply gates the config-apply lane in the schedule (665 D1).

    This is the lane that makes DELAYED/GRACEFUL config changes apply on the
    Celery-beat (multi-host) path; it is on by default and an operator can
    suppress it via the flag.
    """

    _APPLY_KEY = "apply-pending-config-changes"

    def test_config_apply_lane_present_by_default(self):
        """The config-apply entry is in the default consolidated schedule."""
        schedule = get_baldur_beat_schedule()

        assert self._APPLY_KEY in schedule
        assert (
            schedule[self._APPLY_KEY]["task"] == "baldur.apply_pending_config_changes"
        )

    def test_config_apply_lane_absent_when_disabled(self):
        """include_config_apply=False removes the lane (other lanes unaffected)."""
        schedule = get_baldur_beat_schedule(include_config_apply=False)

        assert self._APPLY_KEY not in schedule


class TestConfigureQueueSettingsForwarding:
    """configure_baldur_celery() forwards queue_type/enable_dlx to queues."""

    @pytest.fixture
    def mock_celery_app(self):
        """Celery app mock with realistic conf structure."""
        app = MagicMock()
        app.conf.beat_schedule = {}
        app.conf.task_queues = []
        app.conf.task_routes = {}
        return app

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_queue_type_classic_flows_to_queues(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """queue_type='classic' is applied to all injected queues."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app, queue_type="classic")

        queues = mock_celery_app.conf.task_queues
        for q in queues:
            assert q.queue_arguments["x-queue-type"] == "classic"

    @patch(
        "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
        autospec=True,
    )
    @patch(
        "baldur.adapters.celery.beat_schedule.get_baldur_beat_schedule",
        autospec=True,
    )
    def test_enable_dlx_false_flows_to_queues(
        self, mock_get_schedule, mock_register, mock_celery_app
    ):
        """enable_dlx=False removes DLX bindings from all injected queues."""
        mock_get_schedule.return_value = {}

        configure_baldur_celery(mock_celery_app, enable_dlx=False)

        queues = mock_celery_app.conf.task_queues
        for q in queues:
            assert "x-dead-letter-exchange" not in q.queue_arguments
