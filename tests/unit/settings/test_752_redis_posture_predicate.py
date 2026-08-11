"""752 D1 — the Redis configured-intent predicate.

``redis_explicitly_configured()`` is the shared mechanism that lets every
component tell "optional dependency never configured" (an expected posture
whose failures are quiet) apart from "configured and broken" (an operational
fault that must stay loud). ``redis_absence_is_expected()`` layers the
production carve-out on top and is the helper consumers actually call.

The channel list is derived, not authored: ``REDIS_INTENT_ENV_VARS`` extends
``REDIS_URL_ENV_VARS``, which the live client-acquisition strategy iterates.
The derivation class below is what keeps the two from drifting back apart.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from baldur.adapters.redis.connection_factory import RedisConnectionFactory
from baldur.settings.redis import (
    DEFAULT_REDIS_URL,
    REDIS_INTENT_ENV_VARS,
    REDIS_URL_ENV_VARS,
    redis_absence_is_expected,
    redis_explicitly_configured,
)

_A_REDIS_URL = "redis://configured-host:6379/3"
_DJANGO_REDIS_BACKEND = "django_redis.cache.RedisCache"
_LOCMEM_BACKEND = "django.core.cache.backends.locmem.LocMemCache"


@pytest.fixture(autouse=True)
def unconfigured_redis_env(monkeypatch):
    """Start every case from the zero-config posture.

    The suite runs with ``DJANGO_SETTINGS_MODULE`` pointing at the test app,
    which names no Redis and uses a locmem cache — so clearing the
    environment channels is enough to reach "nobody configured a Redis".
    """
    for name in REDIS_INTENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def runtime_environment(monkeypatch):
    """Rebuild the runtime so ``BALDUR_ENVIRONMENT`` is re-read.

    ``is_production`` is eager-read at ``BaldurRuntime`` construction, so a
    plain ``setenv`` is invisible until the runtime is dropped.
    """
    from baldur.runtime import reset_runtime

    def _set(environment: str) -> None:
        monkeypatch.setenv("BALDUR_ENVIRONMENT", environment)
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        reset_runtime()

    reset_runtime()
    yield _set
    reset_runtime()


class TestRedisExplicitlyConfiguredContract:
    """The documented intent channels, and only those, express intent."""

    def test_default_url_is_the_documented_localhost_address(self):
        """The one spelling of "the address nobody asked for"."""
        assert DEFAULT_REDIS_URL == "redis://localhost:6379/0"

    def test_env_channel_tuples_are_the_documented_channels(self):
        """Acquisition order first, then the feature-local override."""
        assert REDIS_URL_ENV_VARS == ("BALDUR_REDIS_URL", "REDIS_URL")
        assert REDIS_INTENT_ENV_VARS == (
            "BALDUR_REDIS_URL",
            "REDIS_URL",
            "BALDUR_RESILIENT_STORAGE_REDIS_URL",
        )

    def test_no_channel_set_reports_unconfigured(self):
        """Zero config — the framework would be dialing its own default."""
        assert redis_explicitly_configured() is False

    @pytest.mark.parametrize(
        "env_name",
        ["BALDUR_REDIS_URL", "REDIS_URL", "BALDUR_RESILIENT_STORAGE_REDIS_URL"],
        ids=["canonical", "bare_compat", "feature_local_override"],
    )
    def test_env_channel_expresses_intent(self, monkeypatch, env_name):
        """Each documented environment variable is an operator naming a Redis."""
        monkeypatch.setenv(env_name, _A_REDIS_URL)

        assert redis_explicitly_configured() is True

    @pytest.mark.parametrize(
        "value",
        ["", " ", "\t\n  "],
        ids=["empty", "single_space", "mixed_whitespace"],
    )
    def test_whitespace_only_value_is_not_intent(self, monkeypatch, value):
        """Boundary: a blank variable is unset, not "a Redis named ' '"."""
        monkeypatch.setenv("BALDUR_REDIS_URL", value)

        assert redis_explicitly_configured() is False

    def test_single_non_whitespace_character_is_intent(self, monkeypatch):
        """The other side of the boundary — ``strip()`` leaves something."""
        monkeypatch.setenv("BALDUR_REDIS_URL", " x ")

        assert redis_explicitly_configured() is True

    def test_django_settings_attribute_expresses_intent(self):
        """A Django project can name the Redis in settings instead of the env."""
        with override_settings(BALDUR_REDIS_URL=_A_REDIS_URL):
            assert redis_explicitly_configured() is True

    def test_blank_django_settings_attribute_is_not_intent(self):
        """A falsy settings attribute is the same as not having one."""
        with override_settings(BALDUR_REDIS_URL=""):
            assert redis_explicitly_configured() is False

    def test_django_redis_cache_backend_expresses_intent(self):
        """django_redis in CACHES is unmistakably an operator naming a Redis."""
        with override_settings(
            CACHES={"default": {"BACKEND": _DJANGO_REDIS_BACKEND}},
        ):
            assert redis_explicitly_configured() is True

    def test_django_redis_in_a_non_default_cache_alias_expresses_intent(self):
        """Any alias counts — the probe scans every configured cache."""
        with override_settings(
            CACHES={
                "default": {"BACKEND": _LOCMEM_BACKEND},
                "sessions": {"BACKEND": _DJANGO_REDIS_BACKEND},
            },
        ):
            assert redis_explicitly_configured() is True

    def test_non_redis_cache_backend_is_not_intent(self):
        """The negative half: a locmem cache names no Redis."""
        with override_settings(CACHES={"default": {"BACKEND": _LOCMEM_BACKEND}}):
            assert redis_explicitly_configured() is False


class TestRedisIntentDerivationContract:
    """The intent channels are derived from the acquisition channels.

    A new Redis source is added to ``REDIS_URL_ENV_VARS`` once. These cases
    fail if either consumer goes back to naming the variables itself — the
    live acquisition strategy or the posture predicate.
    """

    def test_intent_vars_start_with_the_acquisition_vars_in_order(self):
        """Composition, not a second hand-maintained list."""
        assert REDIS_INTENT_ENV_VARS[: len(REDIS_URL_ENV_VARS)] == REDIS_URL_ENV_VARS

    @pytest.mark.parametrize("env_name", REDIS_URL_ENV_VARS)
    def test_every_acquisition_env_var_is_read_as_intent(self, monkeypatch, env_name):
        """Derived from the tuple itself, so a new member is covered for free."""
        monkeypatch.setenv(env_name, _A_REDIS_URL)

        assert redis_explicitly_configured() is True

    def test_acquire_from_env_reads_a_newly_registered_env_var(self, monkeypatch):
        """``_acquire_from_env`` iterates the tuple instead of naming variables."""
        # Given a source registered only in the shared tuple
        extra = "TEST_752_EXTRA_REDIS_URL"
        monkeypatch.setattr(
            "baldur.settings.redis.REDIS_URL_ENV_VARS",
            (*REDIS_URL_ENV_VARS, extra),
        )
        monkeypatch.setenv(extra, _A_REDIS_URL)
        factory = MagicMock(spec=RedisConnectionFactory)

        # When the acquisition strategy runs
        from baldur.adapters.redis import _acquire_from_env

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            client = _acquire_from_env()

        # Then it dialed the URL that only the tuple knew about
        factory.create.assert_called_once_with(_A_REDIS_URL)
        assert client is factory.create.return_value

    def test_acquire_from_env_prefers_the_first_registered_name(self, monkeypatch):
        """Tuple order is the priority order — canonical beats bare-compat."""
        # Given both documented sources set to different addresses
        monkeypatch.setenv("BALDUR_REDIS_URL", _A_REDIS_URL)
        monkeypatch.setenv("REDIS_URL", "redis://ignored-fallback:6379/9")
        factory = MagicMock(spec=RedisConnectionFactory)

        # When
        from baldur.adapters.redis import _acquire_from_env

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
            return_value=factory,
        ):
            _acquire_from_env()

        # Then the earlier tuple member wins
        factory.create.assert_called_once_with(_A_REDIS_URL)


class TestRedisAbsenceIsExpectedBehavior:
    """The posture helper: quiet only when unconfigured AND not production."""

    def test_unconfigured_non_production_expects_absence(self, runtime_environment):
        runtime_environment("development")

        assert redis_absence_is_expected() is True

    def test_unconfigured_production_does_not_expect_absence(self, runtime_environment):
        """A production process that never configured Redis gets the loud path.

        It bypassed every fail-loud startup gate, so the degraded-mode
        announcement is the only signal it will ever receive.
        """
        runtime_environment("production")

        assert redis_absence_is_expected() is False

    def test_configured_non_production_does_not_expect_absence(
        self, monkeypatch, runtime_environment
    ):
        runtime_environment("development")
        monkeypatch.setenv("BALDUR_REDIS_URL", _A_REDIS_URL)

        assert redis_absence_is_expected() is False

    def test_configured_production_does_not_expect_absence(
        self, monkeypatch, runtime_environment
    ):
        runtime_environment("production")
        monkeypatch.setenv("BALDUR_REDIS_URL", _A_REDIS_URL)

        assert redis_absence_is_expected() is False

    def test_a_raising_intent_probe_resolves_toward_loud(self, runtime_environment):
        """Fault injection: the helper cannot cost safety, only signal."""
        runtime_environment("development")

        with patch(
            "baldur.settings.redis.redis_explicitly_configured",
            side_effect=RuntimeError("probe exploded"),
        ):
            assert redis_absence_is_expected() is False

    def test_a_raising_runtime_lookup_resolves_toward_loud(self, runtime_environment):
        """The second input fails the same direction as the first."""
        runtime_environment("development")

        with patch(
            "baldur.runtime.get_runtime",
            side_effect=RuntimeError("no runtime"),
        ):
            assert redis_absence_is_expected() is False


class TestPredicateDjangoImportSideEffectContract:
    """The predicate does not drag Django into a process that never loaded it.

    ``django_redis`` imports ``django.conf`` and ``django.core.cache`` with
    it, so the CACHES probe has to sit behind the same "is Django plausibly
    in play" gate as the settings probe. A subprocess is the only honest
    seam: the test session itself always has Django loaded.
    """

    def _probe_in_a_django_free_process(self, extra: str = "") -> str:
        script = textwrap.dedent(
            f"""
            import os, sys
            for name in (
                "DJANGO_SETTINGS_MODULE",
                "BALDUR_REDIS_URL",
                "REDIS_URL",
                "BALDUR_RESILIENT_STORAGE_REDIS_URL",
            ):
                os.environ.pop(name, None)
            {extra}
            from baldur.settings.redis import redis_explicitly_configured
            result = redis_explicitly_configured()
            print(
                "RESULT",
                result,
                "django.conf" in sys.modules,
                "django_redis" in sys.modules,
            )
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, f"stderr={completed.stderr}"
        return completed.stdout

    def test_no_settings_module_leaves_django_unimported(self):
        """Neither probe runs, and the answer is still False."""
        assert "RESULT False False False" in self._probe_in_a_django_free_process()

    def test_an_env_channel_answers_before_either_django_probe(self):
        """The cheap channels short-circuit ahead of any import."""
        stdout = self._probe_in_a_django_free_process(
            extra=f'os.environ["BALDUR_REDIS_URL"] = "{_A_REDIS_URL}"',
        )

        assert "RESULT True False False" in stdout
