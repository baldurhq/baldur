"""752 D1 — where the resilient backend's Redis URL comes from.

Startup wiring used to inject ``redis_url=get_redis_settings().url`` into
``ResilientStorageSettings``. That marked the field operator-chosen on every
boot, which made the posture predicate read "configured" on a zero-config
framework boot and kept the degraded-mode CRITICAL alive — the defect that
would have made the whole change miss its goal for the Django / Flask /
FastAPI quickstarts. It also clobbered the documented per-class override and
crashed non-production boots with ``BALDUR_REDIS_URL=""``.

The injection is gone; the settings validator is the only resolution path.
These cases are the regression net for all four operator postures, measured
at both altitudes: the settings class itself, and what startup wiring hands
the backend.

This module sits at the top level following the ``test_bootstrap_*``
convention — ``baldur.bootstrap`` is a top-level module with no parent
package.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.settings.redis import DEFAULT_REDIS_URL
from baldur.settings.resilient_storage import (
    ResilientStorageSettings,
    reset_resilient_storage_settings,
)

_GLOBAL_URL = "redis://global:6379/1"
_PER_CLASS_URL = "redis://perclass:6379/2"
_PER_CLASS_ENV = "BALDUR_RESILIENT_STORAGE_REDIS_URL"


@pytest.fixture(autouse=True)
def clean_redis_env(monkeypatch):
    """Every case declares its own posture — none inherits the host's."""
    monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
    monkeypatch.delenv(_PER_CLASS_ENV, raising=False)
    reset_resilient_storage_settings()
    yield
    reset_resilient_storage_settings()


class TestResilientStorageSettingsPostureContract:
    """The four operator postures, and the empty-string edge that crashed boot.

    ``model_fields_set`` is the operator-chose-this signal the posture
    predicate reads, so each row pins the resolved URL *and* whether the
    field looks operator-chosen. The project-wide fallback is written with
    ``object.__setattr__``, which is why it resolves the URL without
    marking the field.
    """

    @pytest.mark.parametrize(
        ("global_url", "per_class_url", "expected_url", "expected_operator_chosen"),
        [
            (None, None, DEFAULT_REDIS_URL, False),
            (_GLOBAL_URL, None, _GLOBAL_URL, False),
            (None, _PER_CLASS_URL, _PER_CLASS_URL, True),
            (_GLOBAL_URL, _PER_CLASS_URL, _PER_CLASS_URL, True),
        ],
        ids=["nothing_set", "global_only", "per_class_only", "both_per_class_wins"],
    )
    def test_url_resolution_and_operator_chosen_flag_per_posture(
        self,
        monkeypatch,
        global_url,
        per_class_url,
        expected_url,
        expected_operator_chosen,
    ):
        # Given
        if global_url is not None:
            monkeypatch.setenv("BALDUR_REDIS_URL", global_url)
        if per_class_url is not None:
            monkeypatch.setenv(_PER_CLASS_ENV, per_class_url)

        # When
        settings = ResilientStorageSettings()

        # Then
        assert settings.redis_url == expected_url
        assert ("redis_url" in settings.model_fields_set) is expected_operator_chosen

    def test_explicit_kwarg_wins_and_marks_the_field_operator_chosen(self, monkeypatch):
        """A programmatic kwarg is the fifth channel — tests rely on it."""
        monkeypatch.setenv("BALDUR_REDIS_URL", _GLOBAL_URL)

        settings = ResilientStorageSettings(redis_url=_PER_CLASS_URL)

        assert settings.redis_url == _PER_CLASS_URL
        assert "redis_url" in settings.model_fields_set

    def test_empty_global_url_leaves_the_default_instead_of_failing_min_length(
        self, monkeypatch
    ):
        """``BALDUR_REDIS_URL=""`` used to raise ValidationError out of init()."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "")

        settings = ResilientStorageSettings()

        assert settings.redis_url == DEFAULT_REDIS_URL
        assert "redis_url" not in settings.model_fields_set

    def test_default_field_value_is_the_shared_constant(self):
        """One spelling of the default across every settings class."""
        assert (
            ResilientStorageSettings.model_fields["redis_url"].default
            == DEFAULT_REDIS_URL
        )


class TestInstallResilientStorageBackendUrlBehavior:
    """Startup wiring hands the backend the settings-resolved URL, unmodified.

    The wiring supplies only fields an operator actually set, so the
    validator resolves ``redis_url`` exactly as it does for a bare
    construction — which is what makes the posture predicate able to tell a
    zero-config boot from a configured one.
    """

    @staticmethod
    def _install_and_capture(runtime_is_production: bool = False):
        """Run the install step with the backend construction stubbed out.

        Returns the ``ResilientStorageSettings`` instance the wiring built —
        it becomes ``backend.config`` verbatim.
        """
        from baldur import bootstrap
        from baldur.adapters.resilient.backend import (
            ResilientStorageBackend,
            configure_storage_backend,
        )
        from baldur.runtime import BaldurRuntime

        backend_instance = MagicMock(spec=ResilientStorageBackend)
        backend_instance._wal_initialized = True
        backend_instance._wal_on_fallback_dir = False
        backend_instance._wal_honors_configured_dir = True
        backend_cls = MagicMock(
            spec=ResilientStorageBackend, return_value=backend_instance
        )
        runtime = MagicMock(spec=BaldurRuntime, is_production=runtime_is_production)

        with (
            patch(
                "baldur.adapters.resilient.backend.ResilientStorageBackend",
                backend_cls,
            ),
            patch(
                "baldur.adapters.resilient.backend.configure_storage_backend",
                MagicMock(spec=configure_storage_backend),
            ),
        ):
            bootstrap._install_resilient_storage_backend(runtime)

        return backend_cls.call_args.kwargs["settings"]

    @pytest.mark.parametrize(
        ("global_url", "per_class_url", "expected_url", "expected_operator_chosen"),
        [
            (None, None, DEFAULT_REDIS_URL, False),
            (_GLOBAL_URL, None, _GLOBAL_URL, False),
            (None, _PER_CLASS_URL, _PER_CLASS_URL, True),
            (_GLOBAL_URL, _PER_CLASS_URL, _PER_CLASS_URL, True),
        ],
        ids=["nothing_set", "global_only", "per_class_only", "both_per_class_wins"],
    )
    def test_installed_config_matches_the_settings_posture_matrix(
        self,
        monkeypatch,
        global_url,
        per_class_url,
        expected_url,
        expected_operator_chosen,
    ):
        # Given
        if global_url is not None:
            monkeypatch.setenv("BALDUR_REDIS_URL", global_url)
        if per_class_url is not None:
            monkeypatch.setenv(_PER_CLASS_ENV, per_class_url)
        reset_resilient_storage_settings()

        # When
        installed = self._install_and_capture()

        # Then
        assert installed.redis_url == expected_url
        assert ("redis_url" in installed.model_fields_set) is expected_operator_chosen

    def test_empty_global_url_no_longer_aborts_a_non_production_install(
        self, monkeypatch
    ):
        """The latent boot crash: ``min_length=1`` vs an empty injected URL."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "")
        reset_resilient_storage_settings()

        installed = self._install_and_capture()

        assert installed.redis_url == DEFAULT_REDIS_URL


class TestResilientStorageRedisUrlSourceBehavior:
    """The install log line names the channel that actually resolved the URL.

    It used to hardcode ``BALDUR_REDIS_URL``, which the injection removal
    turns into a false claim for a per-class operator and for a zero-config
    boot.
    """

    @pytest.mark.parametrize(
        ("global_url", "per_class_url", "expected_source"),
        [
            (None, None, "default"),
            (_GLOBAL_URL, None, "BALDUR_REDIS_URL"),
            (None, _PER_CLASS_URL, _PER_CLASS_ENV),
            (_GLOBAL_URL, _PER_CLASS_URL, _PER_CLASS_ENV),
        ],
        ids=["nothing_set", "global_only", "per_class_only", "both_per_class_wins"],
    )
    def test_source_names_the_winning_channel(
        self, monkeypatch, global_url, per_class_url, expected_source
    ):
        from baldur.bootstrap import _resilient_storage_redis_url_source

        # Given
        if global_url is not None:
            monkeypatch.setenv("BALDUR_REDIS_URL", global_url)
        if per_class_url is not None:
            monkeypatch.setenv(_PER_CLASS_ENV, per_class_url)

        # When
        source = _resilient_storage_redis_url_source(ResilientStorageSettings())

        # Then
        assert source == expected_source
