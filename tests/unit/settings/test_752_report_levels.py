"""752 D10/D11 — settings reports that stop alarming about their own defaults.

Two independent sites announced the framework's shipped defaults at alarm
level on every healthy boot:

- every secret field defaults to an empty ``SecretStr``, so a zero-config dev
  process logged a security ERROR per CRITICAL secret and a WARNING per
  IMPORTANT one — and a security ERROR that fires on every dev machine
  teaches operators to ignore security ERRORs;
- the leader-election renew interval is *derived* from the lease TTL, and
  the shipped numbers land outside the recommended band by arithmetic, so
  the range check warned about numbers nobody chose.

Both keep their full loudness where the finding is real: production for the
secrets, an operator-chosen interval for the range check. Neither hard gate
moves — production still aborts on a missing CRITICAL secret, and an unsafe
renew cadence still raises.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from baldur.core.exceptions import ConfigurationError
from baldur.settings.leader_election import LeaderElectionSettings
from baldur.settings.secrets import SecretsSettings, validate_required_secrets

_CRITICAL_EVENT = "security.critical_secret_set_system"
_IMPORTANT_EVENT = "security.important_secret_set_some"
_OPTIONAL_EVENT = "security.optional_secret_set"
_RANGE_EVENT = "leader_election.renew_interval_outside_range"


def _levels(logs: list[dict], name: str) -> list[str]:
    return [e["log_level"] for e in logs if e.get("event") == name]


@pytest.fixture
def runtime_environment(monkeypatch):
    """Rebuild the runtime so ``is_production`` re-reads the environment."""
    from baldur.runtime import reset_runtime

    def _set(environment: str) -> None:
        monkeypatch.setenv("BALDUR_ENVIRONMENT", environment)
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        reset_runtime()

    reset_runtime()
    yield _set
    reset_runtime()


@pytest.fixture
def empty_secrets():
    """The zero-config state: every field at its empty default."""
    return SecretsSettings(
        encryption_key=SecretStr(""),
        audit_signing_key=SecretStr(""),
        database_password=SecretStr(""),
        redis_password=SecretStr(""),
        toss_secret_key=SecretStr(""),
        slack_webhook_token=SecretStr(""),
        slack_bot_token=SecretStr(""),
        pagerduty_api_key=SecretStr(""),
        aws_access_key_id=SecretStr(""),
        aws_secret_access_key=SecretStr(""),
    )


class TestSecretReportLevelBehavior:
    """Level by environment; the returned classification never moves."""

    @pytest.mark.parametrize(
        ("environment", "critical_level", "important_level"),
        [
            ("production", "error", "warning"),
            ("development", "info", "debug"),
        ],
        ids=["production_loud", "non_production_quiet"],
    )
    def test_unset_secret_report_levels_split_on_environment(
        self,
        runtime_environment,
        empty_secrets,
        environment,
        critical_level,
        important_level,
    ):
        runtime_environment(environment)

        with capture_logs() as logs:
            try:
                validate_required_secrets(empty_secrets)
            except ConfigurationError:
                pass  # production aborts — the per-secret lines are already out

        assert _levels(logs, _CRITICAL_EVENT) == [critical_level] * 2
        assert _levels(logs, _IMPORTANT_EVENT) == [important_level] * 2

    def test_optional_secrets_report_at_info_in_every_environment(
        self, runtime_environment, empty_secrets
    ):
        """Only the two loud tiers were the problem."""
        runtime_environment("development")

        with capture_logs() as logs:
            validate_required_secrets(empty_secrets)

        assert set(_levels(logs, _OPTIONAL_EVENT)) == {"info"}

    def test_the_returned_classification_is_unchanged_by_the_demotion(
        self, runtime_environment, empty_secrets
    ):
        """Callers branch on the result, not on the level."""
        runtime_environment("development")

        result = validate_required_secrets(empty_secrets)

        assert result["critical"] == ["encryption_key", "audit_signing_key"]
        assert result["warning"] == ["database_password", "redis_password"]
        assert len(result["info"]) == 6

    def test_production_still_aborts_on_a_missing_critical_secret(
        self, runtime_environment, empty_secrets
    ):
        """The hard gate the demotion must not touch."""
        runtime_environment("production")

        with pytest.raises(ConfigurationError, match="encryption_key"):
            validate_required_secrets(empty_secrets)

    def test_a_configured_secret_is_not_reported_at_all(self, runtime_environment):
        runtime_environment("development")
        secrets = SecretsSettings(
            encryption_key=SecretStr("set"),
            audit_signing_key=SecretStr("set"),
        )

        with capture_logs() as logs:
            result = validate_required_secrets(secrets)

        assert result["critical"] == []
        assert _levels(logs, _CRITICAL_EVENT) == []


class TestBootstrapSecretAggregateLevelBehavior:
    """The boot-time aggregate follows the same environment split."""

    @pytest.mark.parametrize(
        ("environment", "expected_level"),
        [("production", "error"), ("development", "info")],
        ids=["production_loud", "non_production_quiet"],
    )
    def test_the_critical_aggregate_level_splits_on_environment(
        self, runtime_environment, environment, expected_level
    ):
        from unittest.mock import patch

        from baldur.bootstrap import _validate_critical_secrets

        runtime_environment(environment)

        with (
            patch(
                "baldur.settings.secrets.validate_required_secrets",
                return_value={"critical": ["encryption_key"], "warning": []},
            ),
            capture_logs() as logs,
        ):
            _validate_critical_secrets()

        assert _levels(logs, "baldur.critical_secrets_configured_check") == [
            expected_level
        ]

    @pytest.mark.parametrize(
        ("environment", "expected_level"),
        [("production", "warning"), ("development", "info")],
        ids=["production_loud", "non_production_quiet"],
    )
    def test_the_important_aggregate_level_splits_on_environment(
        self, runtime_environment, environment, expected_level
    ):
        from unittest.mock import patch

        from baldur.bootstrap import _validate_critical_secrets

        runtime_environment(environment)

        with (
            patch(
                "baldur.settings.secrets.validate_required_secrets",
                return_value={"critical": [], "warning": ["redis_password"]},
            ),
            capture_logs() as logs,
        ):
            _validate_critical_secrets()

        assert _levels(logs, "baldur.important_secrets_configured_check") == [
            expected_level
        ]


class TestLeaderElectionRangeLevelBehavior:
    """The recommended-range check announces at the level its cause deserves.

    The band is ``lease_ttl/4 .. lease_ttl/3``; the derived default lands
    outside it, which is the framework talking to itself.
    """

    def test_the_shipped_defaults_land_outside_the_recommended_band(self):
        """Guards the DEBUG case below from passing because nothing fired.

        ``renew_interval_seconds`` defaults to ``None`` and is derived from
        the lease TTL, which is precisely why nobody chose the number the
        range check used to warn about.
        """
        settings = LeaderElectionSettings()

        assert settings.renew_interval_seconds is None
        interval = settings.get_effective_renew_interval()
        assert not (
            settings.lease_ttl_seconds / 4 <= interval <= settings.lease_ttl_seconds / 3
        )

    def test_a_derived_interval_outside_the_band_reports_at_debug(self):
        with capture_logs() as logs:
            LeaderElectionSettings()

        records = [e for e in logs if e.get("event") == _RANGE_EVENT]
        assert len(records) == 1
        assert records[0]["log_level"] == "debug"
        assert records[0]["operator_set"] is False

    def test_an_operator_chosen_interval_outside_the_band_warns(self):
        """Somebody chose this number, so somebody needs to hear about it."""
        with capture_logs() as logs:
            LeaderElectionSettings(lease_ttl_seconds=60, renew_interval_seconds=5)

        records = [e for e in logs if e.get("event") == _RANGE_EVENT]
        assert len(records) == 1
        assert records[0]["log_level"] == "warning"
        assert records[0]["operator_set"] is True
        assert records[0]["effective_interval"] == 5

    def test_an_operator_chosen_interval_inside_the_band_says_nothing(self):
        with capture_logs() as logs:
            LeaderElectionSettings(lease_ttl_seconds=60, renew_interval_seconds=20)

        assert [e for e in logs if e.get("event") == _RANGE_EVENT] == []

    def test_an_unsafe_interval_still_raises_regardless_of_level(self):
        """The hard constraint (``>= lease_ttl/2``) is untouched."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="lease_ttl/2"):
            LeaderElectionSettings(lease_ttl_seconds=60, renew_interval_seconds=45)

    def test_the_renamed_event_follows_the_component_entity_action_shape(self):
        """The old name (``outside.recommended_range``) named no component."""
        with capture_logs() as logs:
            LeaderElectionSettings(lease_ttl_seconds=60, renew_interval_seconds=5)

        events = {e.get("event") for e in logs}
        assert _RANGE_EVENT in events
        assert "outside.recommended_range" not in events
