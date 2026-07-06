"""
absolutize_against_site_url helper tests (#611 D7).

Pins the conditional-absolutization contract: a relative URL is joined
against ``site_url`` only when the setting was explicitly configured —
``model_fields_set`` distinguishes operator configuration (env or
``set_config``) from the default, so unconfigured multi-host deployments
never get misleading default-base links.

Also pins the pydantic-settings env-source behavior the helper relies on:
an env-sourced value appears in ``model_fields_set``. ``BaldurSettings``
uses an empty ``env_prefix`` (root-settings permanent waiver), so the env
var for ``site_url`` is ``SITE_URL``.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

SITE = "https://ops.example.com"


@pytest.fixture(autouse=True)
def _reset_root_config():
    """Reset the BaldurSettings singleton around every test."""
    from baldur.settings import reset_config

    reset_config()
    yield
    reset_config()


class TestPassthrough:
    """Inputs that must never be modified."""

    def test_absolute_https_passthrough(self):
        from baldur.utils.url import absolutize_against_site_url

        url = "https://grafana.internal/d/cb?service=payment"
        assert absolutize_against_site_url(url) == url

    def test_absolute_http_passthrough(self):
        from baldur.utils.url import absolutize_against_site_url

        url = "http://grafana.internal/d/cb"
        assert absolutize_against_site_url(url) == url

    def test_none_passthrough(self):
        from baldur.utils.url import absolutize_against_site_url

        assert absolutize_against_site_url(None) is None

    def test_empty_string_passthrough(self):
        from baldur.utils.url import absolutize_against_site_url

        assert absolutize_against_site_url("") == ""

    def test_absolute_passthrough_even_when_site_url_set(self):
        from baldur.settings import BaldurSettings, set_config
        from baldur.utils.url import absolutize_against_site_url

        set_config(BaldurSettings(site_url=SITE))
        url = "https://docs.internal/runbooks/cb"
        assert absolutize_against_site_url(url) == url


class TestRelativeUrls:
    """Relative URLs join only against an explicitly configured site_url."""

    def test_relative_unchanged_when_site_url_unset(self):
        from baldur.utils.url import absolutize_against_site_url

        url = "/admin/baldur/circuitbreaker/?service_id=payment"
        assert absolutize_against_site_url(url) == url

    def test_relative_joined_when_set_via_set_config(self):
        from baldur.settings import BaldurSettings, set_config
        from baldur.utils.url import absolutize_against_site_url

        set_config(BaldurSettings(site_url=SITE))

        result = absolutize_against_site_url(
            "/admin/baldur/circuitbreaker/?service_id=payment"
        )

        assert result == f"{SITE}/admin/baldur/circuitbreaker/?service_id=payment"

    def test_relative_joined_when_set_via_env(self):
        from baldur.utils.url import absolutize_against_site_url

        with patch.dict(os.environ, {"SITE_URL": SITE}, clear=False):
            from baldur.settings import reset_config

            reset_config()
            result = absolutize_against_site_url("/api/baldur/chaos/")

        assert result == f"{SITE}/api/baldur/chaos/"

    def test_explicitly_set_default_value_still_joins(self):
        """Explicitly passing the default value counts as configured."""
        from baldur.settings import BaldurSettings, set_config
        from baldur.utils.url import absolutize_against_site_url

        set_config(BaldurSettings(site_url="http://localhost:8000"))

        result = absolutize_against_site_url("/admin/cb/")

        assert result == "http://localhost:8000/admin/cb/"

    def test_query_and_fragment_preserved(self):
        from baldur.settings import BaldurSettings, set_config
        from baldur.utils.url import absolutize_against_site_url

        set_config(BaldurSettings(site_url=SITE))

        assert (
            absolutize_against_site_url("/runbooks/cb#governance")
            == f"{SITE}/runbooks/cb#governance"
        )


class TestEnvSourcePin:
    """Pin: pydantic-settings counts env-sourced values as explicitly set."""

    def test_env_sourced_site_url_appears_in_model_fields_set(self):
        from baldur.settings import BaldurSettings

        with patch.dict(os.environ, {"SITE_URL": SITE}, clear=False):
            settings = BaldurSettings()

        assert "site_url" in settings.model_fields_set
        assert settings.site_url == SITE

    def test_default_site_url_not_in_model_fields_set(self):
        from baldur.settings import BaldurSettings

        env_without_site_url = {
            k: v for k, v in os.environ.items() if k.upper() != "SITE_URL"
        }
        with patch.dict(os.environ, env_without_site_url, clear=True):
            settings = BaldurSettings()

        assert "site_url" not in settings.model_fields_set
        assert settings.site_url == "http://localhost:8000"


class TestBuilderIntegration:
    """The three producers' URL builders emit absolute URLs when site_url is set."""

    def test_cb_admin_url_absolutized(self):
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )
        from baldur.settings import BaldurSettings, set_config

        set_config(BaldurSettings(site_url=SITE))

        with patch.dict(
            os.environ,
            {"CB_ADMIN_BASE_URL": "/admin/baldur/circuitbreaker/"},
            clear=False,
        ):
            reset_actionable_alert_url_builder()
            from baldur.services.circuit_breaker.actionable_alert_urls import (
                get_actionable_alert_url_builder,
            )

            urls = get_actionable_alert_url_builder().build_cb_open_urls(
                service_name="payment",
            )

        reset_actionable_alert_url_builder()
        assert urls.admin_url is not None
        assert urls.admin_url.startswith(f"{SITE}/admin/baldur/circuitbreaker/")

    def test_cb_admin_url_stays_relative_when_site_url_unset(self):
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        with patch.dict(
            os.environ,
            {"CB_ADMIN_BASE_URL": "/admin/baldur/circuitbreaker/"},
            clear=False,
        ):
            reset_actionable_alert_url_builder()
            from baldur.services.circuit_breaker.actionable_alert_urls import (
                get_actionable_alert_url_builder,
            )

            urls = get_actionable_alert_url_builder().build_cb_open_urls(
                service_name="payment",
            )

        reset_actionable_alert_url_builder()
        assert urls.admin_url is not None
        assert urls.admin_url.startswith("/admin/baldur/circuitbreaker/")
