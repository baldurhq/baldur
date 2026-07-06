"""CLI subcommand unit tests (429 Part 7 PR5).

Uses ``typer.testing.CliRunner`` to exercise the fully wired ``baldur``
app through its public surface. Backing services/handlers are mocked so
the CLI→handler wiring is the only thing under test; the handlers
themselves are covered by their own unit suites.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from baldur.cli.app import app
from baldur.interfaces.web_framework import ResponseContext

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def runner():
    """Isolated CliRunner - typer's default stdout/stderr merge is kept."""
    return CliRunner()


@pytest.fixture
def no_init(monkeypatch):
    """Stub ``ensure_init`` so tests don't run baldur.init() on every command.

    baldur.init() is idempotent but still walks registries; bypassing it keeps
    subcommand tests focused on CLI→handler wiring. Each subcommand module
    imports ``ensure_init`` via ``from baldur.cli._bootstrap import ensure_init``,
    so we patch each bound name at its import site.
    """
    from baldur.cli.commands import admin, cb, dlq, escalation, report, scheduler

    for mod in (admin, cb, dlq, escalation, report, scheduler):
        if hasattr(mod, "ensure_init"):
            monkeypatch.setattr(mod, "ensure_init", lambda ctx: None)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip BALDUR_* env vars before and after each test.

    Commands that exercise ``--config`` call ``apply_config_to_env`` which
    writes directly to ``os.environ`` (bypassing monkeypatch). The teardown
    _strip prevents env leakage from poisoning sibling tests under xdist,
    particularly admin-server tests that instantiate AdminServerSettings.
    """
    import os

    def _strip() -> None:
        for key in list(os.environ.keys()):
            if key.startswith("BALDUR_") or key == "XDG_CONFIG_HOME":
                os.environ.pop(key, None)

    _strip()
    yield monkeypatch
    _strip()


# =============================================================================
# Root command / --config plumbing
# =============================================================================


class TestRootAppBehavior:
    """The root typer app enforces no-args-is-help and registers all subcommands."""

    def test_no_args_prints_help(self, runner, clean_env):
        result = runner.invoke(app, [])
        assert result.exit_code != 0  # typer's help-exit code
        assert "check-config" in result.stdout
        assert "admin" in result.stdout
        assert "dlq" in result.stdout
        assert "cb" in result.stdout
        assert "scheduler" in result.stdout
        assert "report" in result.stdout

    def test_config_flag_missing_file_raises(self, runner, clean_env, tmp_path):
        """--config pointing to a non-existent file must surface, not silently fall through."""
        missing = tmp_path / "nope.toml"
        result = runner.invoke(app, ["--config", str(missing), "scheduler", "list"])

        # FileNotFoundError bubbles up through typer; non-zero exit + error visible.
        assert result.exit_code != 0

    def test_config_flag_valid_toml_exports_env(
        self, runner, clean_env, tmp_path, no_init
    ):
        """--config file's [baldur.<section>] values project onto BALDUR_<SECTION>_* env vars."""
        import os

        cfg = tmp_path / "baldur.toml"
        cfg.write_text('[baldur.admin]\nbind = "203.0.113.7"\n')

        # try/finally guarantees env cleanup even if an assertion fails midway.
        try:
            with patch(
                "baldur.coordination.scheduler.get_leader_scheduler",
                return_value=_fake_scheduler(),
            ):
                result = runner.invoke(
                    app, ["--config", str(cfg), "scheduler", "list", "--json"]
                )

            assert result.exit_code == 0
            assert os.environ.get("BALDUR_ADMIN_BIND") == "203.0.113.7"
        finally:
            os.environ.pop("BALDUR_ADMIN_BIND", None)


# =============================================================================
# check-config command
# =============================================================================


def _fake_preflight(*, fatal=None, warnings=None, is_valid=True):
    """Construct a preflight-result double with the attributes check_config reads."""
    result = MagicMock()
    result.fatal_violations = fatal or {}
    result.non_fatal_warnings = warnings or {}
    result.is_valid = is_valid
    result.has_fatal_violations = bool(fatal)
    return result


class TestCheckConfigBehavior:
    """check-config is the CI/CD preflight - strict mode decides exit code."""

    def test_valid_config_exits_zero(self, runner, clean_env):
        fake_config = MagicMock()
        fake_config.to_full_dict.return_value = {}
        with (
            patch("baldur.settings.get_config", return_value=fake_config),
            patch(
                "baldur.core.safe_defaults.validate_config_preflight",
                return_value=_fake_preflight(is_valid=True),
            ),
        ):
            result = runner.invoke(app, ["check-config"])

        assert result.exit_code == 0
        assert "valid" in result.stdout.lower()

    def test_json_output_emits_parsable_payload(self, runner, clean_env):
        with (
            patch("baldur.settings.get_config", return_value=MagicMock()),
            patch(
                "baldur.core.safe_defaults.validate_config_preflight",
                return_value=_fake_preflight(is_valid=True),
            ),
        ):
            result = runner.invoke(app, ["check-config", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "valid"
        assert payload["fatal_violation_count"] == 0
        assert payload["warning_count"] == 0

    def test_strict_with_fatal_exits_one(self, runner, clean_env):
        fatal = {"circuit_breaker": {"threshold": "must be positive"}}
        with (
            patch("baldur.settings.get_config", return_value=MagicMock()),
            patch(
                "baldur.core.safe_defaults.validate_config_preflight",
                return_value=_fake_preflight(fatal=fatal, is_valid=False),
            ),
        ):
            result = runner.invoke(app, ["check-config", "--strict"])

        assert result.exit_code == 1
        assert "FATAL" in result.stdout or "FAILED" in result.stdout

    def test_fatal_without_strict_still_exits_zero(self, runner, clean_env):
        """Non-strict mode surfaces violations but lets CI decide via --strict."""
        fatal = {"cb": {"x": "bad"}}
        with (
            patch("baldur.settings.get_config", return_value=MagicMock()),
            patch(
                "baldur.core.safe_defaults.validate_config_preflight",
                return_value=_fake_preflight(fatal=fatal, is_valid=False),
            ),
        ):
            result = runner.invoke(app, ["check-config"])

        assert result.exit_code == 0

    def test_import_failure_exits_two(self, runner, clean_env):
        """If baldur modules cannot import, exit 2 (internal error)."""
        with patch.dict(
            "sys.modules",
            {"baldur.core.safe_defaults": None, "baldur.settings": None},
        ):
            result = runner.invoke(app, ["check-config"])

        assert result.exit_code == 2

    def test_warning_count_reported(self, runner, clean_env):
        warnings_payload = {"section": {"k1": "w1", "k2": "w2"}}
        with (
            patch("baldur.settings.get_config", return_value=MagicMock()),
            patch(
                "baldur.core.safe_defaults.validate_config_preflight",
                return_value=_fake_preflight(warnings=warnings_payload),
            ),
        ):
            result = runner.invoke(app, ["check-config", "--json"])

        payload = json.loads(result.stdout)
        assert payload["warning_count"] == 2

    def test_inspect_dumps_config_tree(self, runner, clean_env):
        fake_config = MagicMock()
        fake_config.to_full_dict.return_value = {"service_name": "api"}
        with (
            patch("baldur.settings.get_config", return_value=fake_config),
            patch(
                "baldur.core.safe_defaults.validate_config_preflight",
                return_value=_fake_preflight(),
            ),
        ):
            result = runner.invoke(app, ["check-config", "--inspect", "--json"])

        payload = json.loads(result.stdout)
        assert payload["config"] == {"service_name": "api"}

    def test_config_type_filters_inspect(self, runner, clean_env):
        fake_config = MagicMock()
        fake_section = MagicMock()
        fake_section.model_dump.return_value = {"threshold": 10}
        # Only the filtered attribute should be accessed.
        fake_config.circuit_breaker = fake_section
        del fake_section.to_full_dict
        with (
            patch("baldur.settings.get_config", return_value=fake_config),
            patch(
                "baldur.core.safe_defaults.validate_config_preflight",
                return_value=_fake_preflight(),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "check-config",
                    "--inspect",
                    "--json",
                    "--config-type",
                    "circuit_breaker",
                ],
            )

        payload = json.loads(result.stdout)
        assert payload["config"] == {"circuit_breaker": {"threshold": 10}}


# =============================================================================
# dlq commands
# =============================================================================


class TestDlqListBehavior:
    """``baldur dlq list`` passes pagination + filters as query string."""

    def test_list_default_succeeds(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["query"] = dict(ctx.query_params)
            return ResponseContext.json({"results": [], "total": 0})

        with patch("baldur.cli.commands.dlq.dlq_list", side_effect=fake_handler):
            result = runner.invoke(app, ["dlq", "list"])

        assert result.exit_code == 0
        assert captured["query"]["page"] == "1"
        assert captured["query"]["page_size"] == "20"
        assert "status" not in captured["query"]
        assert "domain" not in captured["query"]

    def test_pending_shortcut_overrides_status(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["query"] = dict(ctx.query_params)
            return ResponseContext.json({"results": []})

        with patch("baldur.cli.commands.dlq.dlq_list", side_effect=fake_handler):
            result = runner.invoke(
                app, ["dlq", "list", "--status", "resolved", "--pending"]
            )

        assert result.exit_code == 0
        assert captured["query"]["status"] == "pending"

    def test_domain_filter_propagated(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["query"] = dict(ctx.query_params)
            return ResponseContext.json({"results": []})

        with patch("baldur.cli.commands.dlq.dlq_list", side_effect=fake_handler):
            result = runner.invoke(app, ["dlq", "list", "--domain", "payment"])

        assert result.exit_code == 0
        assert captured["query"]["domain"] == "payment"

    def test_handler_exception_exits_one(self, runner, clean_env, no_init):
        """run_handler wraps unexpected errors as 500 - maps to exit 1."""

        def boom(_ctx):
            raise RuntimeError("db gone")

        with patch("baldur.cli.commands.dlq.dlq_list", side_effect=boom):
            result = runner.invoke(app, ["dlq", "list"])

        assert result.exit_code == 1

    def test_page_size_validated_by_typer(self, runner, clean_env, no_init):
        """--page-size max=200 is enforced by typer, not the handler."""
        with patch("baldur.cli.commands.dlq.dlq_list") as mock_handler:
            result = runner.invoke(app, ["dlq", "list", "--page-size", "500"])

        assert result.exit_code != 0
        mock_handler.assert_not_called()


class TestDlqReplayBehavior:
    def test_replay_sends_post_with_batch_size(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["method"] = ctx.method.value
            captured["body"] = ctx.json_body
            return ResponseContext.json({"replayed": 0}, status_code=200)

        with patch("baldur.cli.commands.dlq.dlq_replay", side_effect=fake_handler):
            result = runner.invoke(app, ["dlq", "replay", "--batch-size", "25"])

        assert result.exit_code == 0
        assert captured["method"] == "POST"
        assert captured["body"] == {"batch_size": 25}

    def test_replay_with_domain_includes_key(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["body"] = ctx.json_body
            return ResponseContext.json({"replayed": 0})

        with patch("baldur.cli.commands.dlq.dlq_replay", side_effect=fake_handler):
            result = runner.invoke(app, ["dlq", "replay", "--domain", "cart"])

        assert result.exit_code == 0
        assert captured["body"]["domain"] == "cart"
        assert captured["body"]["batch_size"] == 50  # default

    def test_client_error_exits_two(self, runner, clean_env, no_init):
        def fake_handler(_ctx):
            return ResponseContext.json({"error": "bad"}, status_code=422)

        with patch("baldur.cli.commands.dlq.dlq_replay", side_effect=fake_handler):
            result = runner.invoke(app, ["dlq", "replay"])

        assert result.exit_code == 2


# =============================================================================
# cb commands
# =============================================================================


class TestCbListBehavior:
    def test_list_defaults_to_ops_environment(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["env"] = ctx.query_params.get("environment")
            return ResponseContext.json({"services": []})

        with patch("baldur.cli.commands.cb.control_status", side_effect=fake_handler):
            result = runner.invoke(app, ["cb", "list"])

        assert result.exit_code == 0
        assert captured["env"] == "ops"

    def test_list_with_environment_override(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["env"] = ctx.query_params.get("environment")
            return ResponseContext.json({"services": []})

        with patch("baldur.cli.commands.cb.control_status", side_effect=fake_handler):
            runner.invoke(app, ["cb", "list", "--environment", "chaos"])

        assert captured["env"] == "chaos"


class TestCbMutationsBehavior:
    """reset / force-open / force-close all POST through _run_quick."""

    def test_reset_propagates_service_name_and_reason(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["path_params"] = ctx.path_params
            captured["body"] = ctx.json_body
            captured["method"] = ctx.method.value
            return ResponseContext.json({"status": "ok"})

        with patch("baldur.cli.commands.cb.quick_reset", side_effect=fake_handler):
            result = runner.invoke(app, ["cb", "reset", "api", "--reason", "manual"])

        assert result.exit_code == 0
        assert captured["path_params"]["service_name"] == "api"
        assert captured["body"]["reason"] == "manual"
        assert captured["body"]["environment"] == "ops"
        assert captured["method"] == "POST"

    def test_force_open_includes_ttl_when_provided(self, runner, clean_env, no_init):
        captured = {}

        def fake_handler(ctx):
            captured["body"] = ctx.json_body
            return ResponseContext.json({"status": "ok"})

        with patch("baldur.cli.commands.cb.quick_block", side_effect=fake_handler):
            result = runner.invoke(app, ["cb", "force-open", "payments", "--ttl", "30"])

        assert result.exit_code == 0
        assert captured["body"]["ttl_minutes"] == 30

    def test_force_open_omits_ttl_when_not_provided(self, runner, clean_env, no_init):
        """Default TTL is handler-provided, so the body must not carry a key."""
        captured = {}

        def fake_handler(ctx):
            captured["body"] = ctx.json_body
            return ResponseContext.json({"status": "ok"})

        with patch("baldur.cli.commands.cb.quick_block", side_effect=fake_handler):
            result = runner.invoke(app, ["cb", "force-open", "payments"])

        assert result.exit_code == 0
        assert "ttl_minutes" not in captured["body"]

    def test_force_close_calls_quick_allow(self, runner, clean_env, no_init):
        with patch(
            "baldur.cli.commands.cb.quick_allow",
            return_value=ResponseContext.json({"status": "ok"}),
        ) as mock_allow:
            result = runner.invoke(app, ["cb", "force-close", "api"])

        assert result.exit_code == 0
        mock_allow.assert_called_once()

    def test_missing_service_name_argument_fails(self, runner, clean_env, no_init):
        """Typer enforces the positional argument - no handler call expected."""
        with patch("baldur.cli.commands.cb.quick_reset") as mock_handler:
            result = runner.invoke(app, ["cb", "reset"])

        assert result.exit_code != 0
        mock_handler.assert_not_called()


# =============================================================================
# escalation command (impl 569)
# =============================================================================


class TestEscalationCommand:
    """``baldur escalation test`` POSTs to the shared handler and maps the
    response status to an exit code (CLI→handler wiring is the SUT)."""

    def test_test_invokes_handler_with_post_to_escalation_path(
        self, runner, clean_env, no_init
    ):
        captured = {}

        def fake_handler(ctx):
            captured["method"] = ctx.method.value
            captured["path"] = ctx.path
            return ResponseContext.json(
                {
                    "success": True,
                    "channels_sent": ["slack"],
                    "channels_failed": [],
                    "error_message": None,
                }
            )

        with patch(
            "baldur.cli.commands.escalation.meta_watchdog_send_test",
            side_effect=fake_handler,
        ):
            result = runner.invoke(app, ["escalation", "test"])

        assert result.exit_code == 0
        assert captured["method"] == "POST"
        assert captured["path"] == "/meta-watchdog/escalation-test"

    def test_none_configured_maps_to_exit_two(self, runner, clean_env, no_init):
        """400 (no channel configured) is a user error -> exit 2."""

        def fake_handler(_ctx):
            return ResponseContext.json(
                {
                    "success": False,
                    "channels_sent": [],
                    "channels_failed": [],
                    "error_message": "No escalation channel configured",
                },
                status_code=400,
            )

        with patch(
            "baldur.cli.commands.escalation.meta_watchdog_send_test",
            side_effect=fake_handler,
        ):
            result = runner.invoke(app, ["escalation", "test"])

        assert result.exit_code == 2

    def test_channel_failure_maps_to_exit_one(self, runner, clean_env, no_init):
        """502 (a configured channel failed to deliver) -> exit 1."""

        def fake_handler(_ctx):
            return ResponseContext.json(
                {
                    "success": False,
                    "channels_sent": [],
                    "channels_failed": ["slack"],
                    "error_message": "slack: HTTP 403",
                },
                status_code=502,
            )

        with patch(
            "baldur.cli.commands.escalation.meta_watchdog_send_test",
            side_effect=fake_handler,
        ):
            result = runner.invoke(app, ["escalation", "test"])

        assert result.exit_code == 1

    def test_json_output_emits_parsable_payload(self, runner, clean_env, no_init):
        def fake_handler(_ctx):
            return ResponseContext.json(
                {
                    "success": True,
                    "channels_sent": ["slack"],
                    "channels_failed": [],
                    "error_message": None,
                }
            )

        with patch(
            "baldur.cli.commands.escalation.meta_watchdog_send_test",
            side_effect=fake_handler,
        ):
            result = runner.invoke(app, ["escalation", "test", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        assert payload["channels_sent"] == ["slack"]


# =============================================================================
# report command
# =============================================================================


class TestReportBehavior:
    def test_report_without_date_lists_recent(self, runner, clean_env, no_init):
        captured = {}

        def fake_list(ctx):
            captured["path"] = ctx.path
            captured["days"] = ctx.query_params.get("days")
            return ResponseContext.json({"reports": []})

        with patch(
            "baldur.cli.commands.report.daily_report_list", side_effect=fake_list
        ):
            result = runner.invoke(app, ["report", "--days", "14"])

        assert result.exit_code == 0
        assert captured["path"] == "/reports/daily/"
        assert captured["days"] == "14"

    def test_report_with_date_calls_detail_handler(self, runner, clean_env, no_init):
        captured = {}

        def fake_detail(ctx):
            captured["path"] = ctx.path
            captured["path_params"] = ctx.path_params
            return ResponseContext.json({"date": "2026-04-15"})

        with patch(
            "baldur.cli.commands.report.daily_report_detail",
            side_effect=fake_detail,
        ):
            result = runner.invoke(app, ["report", "--date", "2026-04-15"])

        assert result.exit_code == 0
        assert "2026-04-15" in captured["path"]
        assert captured["path_params"]["date"] == "2026-04-15"

    def test_report_today_resolves_via_utc_now(self, runner, clean_env, no_init):
        captured = {}
        fixed_now = datetime(2026, 4, 16, 12, 0, 0)

        def fake_detail(ctx):
            captured["path_params"] = ctx.path_params
            return ResponseContext.json({"date": "2026-04-16"})

        with (
            patch("baldur.cli.commands.report.utc_now", return_value=fixed_now),
            patch(
                "baldur.cli.commands.report.daily_report_detail",
                side_effect=fake_detail,
            ),
        ):
            result = runner.invoke(app, ["report", "--date", "today"])

        assert result.exit_code == 0
        assert captured["path_params"]["date"] == "2026-04-16"

    def test_report_404_maps_to_exit_two(self, runner, clean_env, no_init):
        """Detail 404 is a user error (date missing) - exit 2, not 1."""

        def fake_detail(_ctx):
            return ResponseContext.json({"error": "not found"}, status_code=404)

        with patch(
            "baldur.cli.commands.report.daily_report_detail",
            side_effect=fake_detail,
        ):
            result = runner.invoke(app, ["report", "--date", "2026-04-01"])

        assert result.exit_code == 2

    def test_report_server_error_exits_one(self, runner, clean_env, no_init):
        def fake_list(_ctx):
            return ResponseContext.json({"error": "boom"}, status_code=503)

        with patch(
            "baldur.cli.commands.report.daily_report_list", side_effect=fake_list
        ):
            result = runner.invoke(app, ["report"])

        assert result.exit_code == 1


# =============================================================================
# scheduler command
# =============================================================================


def _fake_job(
    name: str,
    *,
    interval: float = 60.0,
    runs: int = 0,
    errors: int = 0,
    last_run: datetime | None = None,
    enabled: bool = True,
):
    job = MagicMock()
    job.interval_seconds = interval
    job.enabled = enabled
    job.run_count = runs
    job.error_count = errors
    job.last_run = last_run
    return job


def _fake_scheduler(*, is_leader: bool = True, jobs: dict | None = None):
    sched = MagicMock()
    sched.is_leader = is_leader
    sched.jobs = jobs or {}
    return sched


class TestSchedulerListBehavior:
    def test_empty_scheduler_reports_zero_jobs(self, runner, clean_env, no_init):
        with patch(
            "baldur.coordination.scheduler.get_leader_scheduler",
            return_value=_fake_scheduler(jobs={}),
        ):
            result = runner.invoke(app, ["scheduler", "list", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["job_count"] == 0
        assert payload["jobs"] == []

    def test_jobs_rendered_with_metadata(self, runner, clean_env, no_init):
        jobs = {
            "dlq_replay": _fake_job(
                "dlq_replay",
                interval=30.0,
                runs=5,
                errors=1,
                last_run=datetime(2026, 4, 16, 10, 0, 0),
            ),
        }
        with patch(
            "baldur.coordination.scheduler.get_leader_scheduler",
            return_value=_fake_scheduler(is_leader=False, jobs=jobs),
        ):
            result = runner.invoke(app, ["scheduler", "list", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["is_leader"] is False
        assert payload["job_count"] == 1
        assert payload["jobs"][0]["name"] == "dlq_replay"
        assert payload["jobs"][0]["run_count"] == 5
        assert payload["jobs"][0]["error_count"] == 1
        assert payload["jobs"][0]["last_run"] == "2026-04-16T10:00:00"

    def test_last_run_none_serialized_as_null(self, runner, clean_env, no_init):
        jobs = {"x": _fake_job("x", last_run=None)}
        with patch(
            "baldur.coordination.scheduler.get_leader_scheduler",
            return_value=_fake_scheduler(jobs=jobs),
        ):
            result = runner.invoke(app, ["scheduler", "list", "--json"])

        payload = json.loads(result.stdout)
        assert payload["jobs"][0]["last_run"] is None

    def test_text_output_includes_leader_line(self, runner, clean_env, no_init):
        jobs = {"dlq_replay": _fake_job("dlq_replay")}
        with patch(
            "baldur.coordination.scheduler.get_leader_scheduler",
            return_value=_fake_scheduler(is_leader=True, jobs=jobs),
        ):
            result = runner.invoke(app, ["scheduler", "list"])

        assert result.exit_code == 0
        assert "Leader: True" in result.stdout
        assert "dlq_replay" in result.stdout

    def test_text_output_error_marker_when_errors(self, runner, clean_env, no_init):
        jobs = {
            "bad_job": _fake_job("bad_job", errors=3),
            "ok_job": _fake_job("ok_job"),
        }
        with patch(
            "baldur.coordination.scheduler.get_leader_scheduler",
            return_value=_fake_scheduler(jobs=jobs),
        ):
            result = runner.invoke(app, ["scheduler", "list"])

        assert "errors" in result.stdout  # from the 'errors' symbol for bad_job
        assert "[ok]" in result.stdout  # symbol for ok_job


# =============================================================================
# admin command - startup/shutdown path with mocked server
# =============================================================================


class TestAdminCommandBehavior:
    """The admin command blocks on a threading.Event; we fire it immediately
    via a signal handler substitute so the test returns without real binding."""

    def test_admin_starts_and_stops_server(self, runner, clean_env, no_init):
        server = MagicMock()
        server.settings.bind = "127.0.0.1"
        server.settings.port = 9999

        with (
            patch(
                "baldur.api.admin.start_admin_server", return_value=server
            ) as mock_start,
            patch("baldur.api.admin.stop_admin_server") as mock_stop,
            patch("baldur.cli.commands.admin.threading.Event") as mock_event_cls,
        ):
            fake_event = MagicMock()
            fake_event.is_set.return_value = True  # skip the wait loop
            mock_event_cls.return_value = fake_event

            result = runner.invoke(app, ["admin", "--port", "9999"])

        assert result.exit_code == 0
        mock_start.assert_called_once()
        mock_stop.assert_called_once()
        assert "127.0.0.1:9999" in result.stdout

    def test_admin_forwards_bind_and_port_overrides(self, runner, clean_env, no_init):
        server = MagicMock()
        server.settings.bind = "0.0.0.0"
        server.settings.port = 8081

        with (
            patch(
                "baldur.api.admin.start_admin_server", return_value=server
            ) as mock_start,
            patch("baldur.api.admin.stop_admin_server"),
            patch("baldur.cli.commands.admin.threading.Event") as mock_event_cls,
        ):
            fake_event = MagicMock()
            fake_event.is_set.return_value = True
            mock_event_cls.return_value = fake_event

            runner.invoke(app, ["admin", "--port", "8081", "--bind", "0.0.0.0"])

        mock_start.assert_called_once_with(port=8081, bind="0.0.0.0")


# =============================================================================
# Django shim delegation
# =============================================================================


class TestDjangoShimDelegation:
    """Both management shims forward argv to the CLI app; verify the argv map."""

    def test_check_baldur_config_shim_passes_strict(self, clean_env):
        from baldur.adapters.django.management.commands import check_baldur_config

        with patch("baldur.cli.app") as mock_app:
            check_baldur_config._run_cli(["check-config", "--strict"])

        mock_app.assert_called_once_with(
            args=["check-config", "--strict"], standalone_mode=False
        )

    def test_baldur_config_shim_appends_inspect_by_default(self, clean_env):
        """Legacy `baldur_config` command defaults to --inspect when --validate not set."""
        from baldur.adapters.django.management.commands.baldur_config import Command

        cmd = Command()
        with (
            patch(
                "baldur.adapters.django.management.commands.baldur_config._run_cli",
                return_value=0,
            ) as mock_run,
            patch("sys.exit") as mock_exit,
        ):
            cmd.handle(
                inspect=False,
                validate=False,
                format="text",
                config_type=None,
            )

        argv = mock_run.call_args[0][0]
        assert "check-config" in argv
        assert "--inspect" in argv
        mock_exit.assert_called_once_with(0)

    def test_baldur_config_shim_validate_only_skips_inspect(self, clean_env):
        from baldur.adapters.django.management.commands.baldur_config import Command

        cmd = Command()
        with (
            patch(
                "baldur.adapters.django.management.commands.baldur_config._run_cli",
                return_value=0,
            ) as mock_run,
            patch("sys.exit"),
        ):
            cmd.handle(
                inspect=False,
                validate=True,
                format="json",
                config_type=None,
            )

        argv = mock_run.call_args[0][0]
        assert "--inspect" not in argv
        assert "--json" in argv

    def test_baldur_config_shim_config_type_forwarded(self, clean_env):
        from baldur.adapters.django.management.commands.baldur_config import Command

        cmd = Command()
        with (
            patch(
                "baldur.adapters.django.management.commands.baldur_config._run_cli",
                return_value=0,
            ) as mock_run,
            patch("sys.exit"),
        ):
            cmd.handle(
                inspect=True,
                validate=False,
                format="text",
                config_type="circuit_breaker",
            )

        argv = mock_run.call_args[0][0]
        assert "--config-type" in argv
        assert "circuit_breaker" in argv

    def test_run_cli_returns_zero_on_success(self, clean_env):
        from baldur.adapters.django.management.commands import check_baldur_config

        with patch("baldur.cli.app", return_value=None):
            exit_code = check_baldur_config._run_cli(["check-config"])
        assert exit_code == 0

    def test_run_cli_extracts_system_exit_code(self, clean_env):
        from baldur.adapters.django.management.commands import check_baldur_config

        def raising_app(args, standalone_mode):  # noqa: ARG001
            raise SystemExit(2)

        with patch("baldur.cli.app", side_effect=raising_app):
            exit_code = check_baldur_config._run_cli(["check-config"])
        assert exit_code == 2
