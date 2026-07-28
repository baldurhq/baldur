"""Contract tests for the outbound liveness-beacon settings fields (731).

The three fields ship in OSS while the only consumer is PRO, so this file pins
what the OSS half promises: the defaults that keep the beacon off, the socket
budget's declared range, and the ``BALDUR_META_WATCHDOG_BEACON_*`` env-var
names an operator configures it with.

Design contract (hardcoded per the Contract-test rule):

- ``beacon_url`` / ``beacon_fail_url`` default to ``None`` — set-to-enable, no
  separate ``*_enabled`` flag, and no egress out of the box.
- ``beacon_timeout_seconds`` defaults to 5.0 and is bounded ge=1.0 / le=10.0:
  long enough for a TLS handshake to a distant provider, short enough that a
  wedged endpoint cannot hold the sender across many passes.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from pydantic import ValidationError

from baldur.settings.meta_watchdog import MetaWatchdogSettings

BEACON_URL_ENV = "BALDUR_META_WATCHDOG_BEACON_URL"
BEACON_FAIL_URL_ENV = "BALDUR_META_WATCHDOG_BEACON_FAIL_URL"
BEACON_TIMEOUT_ENV = "BALDUR_META_WATCHDOG_BEACON_TIMEOUT_SECONDS"


class TestMetaWatchdogBeaconSettingsContract:
    """MetaWatchdogSettings beacon fields — defaults, bounds, env names."""

    def test_beacon_url_defaults_to_none(self):
        """No beacon URL out of the box: the beacon is off unless set."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert MetaWatchdogSettings().beacon_url is None

    def test_beacon_fail_url_defaults_to_none(self):
        """An UNHEALTHY pass falls back to the liveness URL unless this is set."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert MetaWatchdogSettings().beacon_fail_url is None

    def test_beacon_timeout_seconds_defaults_to_5(self):
        """beacon_timeout_seconds default: 5.0s."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert MetaWatchdogSettings().beacon_timeout_seconds == 5.0

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (0.9, False),
            (1.0, True),
            (10.0, True),
            (10.1, False),
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_beacon_timeout_seconds_boundary(self, value, should_pass):
        """beacon_timeout_seconds accepts [1.0, 10.0] and rejects outside it."""
        with mock.patch.dict(os.environ, {}, clear=True):
            if should_pass:
                assert (
                    MetaWatchdogSettings(
                        beacon_timeout_seconds=value
                    ).beacon_timeout_seconds
                    == value
                )
            else:
                with pytest.raises(ValidationError):
                    MetaWatchdogSettings(beacon_timeout_seconds=value)

    def test_beacon_fields_are_configured_by_their_env_vars(self):
        """The three BALDUR_META_WATCHDOG_BEACON_* names reach their fields.

        The env-var name is the operator's whole interface to this feature — a
        renamed prefix would leave a documented variable that configures nothing.
        """
        env = {
            BEACON_URL_ENV: "https://hc.example/ping/abc",
            BEACON_FAIL_URL_ENV: "https://hc.example/ping/abc/fail",
            BEACON_TIMEOUT_ENV: "2.5",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = MetaWatchdogSettings()

        assert settings.beacon_url == "https://hc.example/ping/abc"
        assert settings.beacon_fail_url == "https://hc.example/ping/abc/fail"
        assert settings.beacon_timeout_seconds == 2.5

    def test_beacon_url_fields_accept_none_after_being_declared_optional(self):
        """Both URL fields stay explicitly nullable — None is the off switch."""
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = MetaWatchdogSettings(beacon_url=None, beacon_fail_url=None)

        assert settings.beacon_url is None
        assert settings.beacon_fail_url is None
