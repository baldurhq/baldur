"""
DLQ backend-selection settings unit tests (778 D6).

Test target:
    - ``baldur.settings.dlq.DLQSettings.backend`` + its ``_normalize_backend``
      validator.

The field exists so ``BALDUR_DLQ_BACKEND`` is a real settings field rather
than a bare direct environment read: only a field resolves for the env-var
reference page and the startup unknown-var scan.

Its validator normalizes but deliberately does NOT raise on an unknown
value. Every dead-letter consumer builds its config through
``get_dlq_settings()``, so a raising membership check would turn an operator
typo into "no dead-letter queue at all" instead of "the backend name was
ignored". Membership is enforced where the value is consumed — the registry
wiring warns and falls through to its probe chain.

Test categories:
    A. Contract: the shipped default and the normalization rules.
    B. Contract: an unknown value is carried, not rejected.
"""

from __future__ import annotations

import pytest

from baldur.settings.dlq import DLQSettings, get_dlq_settings, reset_dlq_settings


@pytest.fixture(autouse=True)
def _clean_backend_env(monkeypatch):
    """Keep the knob out of the ambient environment for every case."""
    monkeypatch.delenv("BALDUR_DLQ_BACKEND", raising=False)
    reset_dlq_settings()
    yield
    reset_dlq_settings()


class TestDLQSettingsBackendContract:
    """The ``backend`` field's shipped contract."""

    def test_backend_default_is_empty_string(self):
        """Unset means "let the startup probe chain decide"."""
        assert DLQSettings().backend == ""

    @pytest.mark.parametrize("value", ["memory", "redis", "sql"])
    def test_documented_backend_names_pass_through_unchanged(self, value):
        """The three names the description advertises survive validation."""
        assert DLQSettings(backend=value).backend == value

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("SQL", "sql"),
            ("Redis", "redis"),
            ("  sql  ", "sql"),
            ("\tMEMORY\n", "memory"),
            ("   ", ""),
        ],
    )
    def test_backend_is_stripped_and_lowercased(self, raw, expected):
        """Case and surrounding whitespace are operator noise, not intent.

        A whitespace-only value normalizes to the empty string, which is the
        same as unset — the chain decides.
        """
        assert DLQSettings(backend=raw).backend == expected

    def test_backend_reads_the_env_var(self, monkeypatch):
        """``BALDUR_DLQ_BACKEND`` is what the field is for."""
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "SQL")

        assert DLQSettings().backend == "sql"

    def test_unknown_backend_value_is_carried_not_rejected(self):
        """An unrecognized name must not raise.

        This is the deliberate deviation from the raising-validator
        convention used elsewhere in this settings class: rejecting here
        would make the settings object unconstructable, and every dead-letter
        consumer resolves its config through it.
        """
        settings = DLQSettings(backend="postgres")

        assert settings.backend == "postgres"

    def test_typo_backend_value_keeps_dlq_settings_constructable(self, monkeypatch):
        """The failure mode the non-raising validator exists to prevent.

        With a raising validator, ``get_dlq_settings()`` would raise for
        every caller — killing dead-letter capture entirely over a
        misspelled backend name.
        """
        monkeypatch.setenv("BALDUR_DLQ_BACKEND", "postgres")
        reset_dlq_settings()

        settings = get_dlq_settings()

        assert settings.backend == "postgres"
        assert settings.enabled is True
