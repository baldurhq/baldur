"""Unit tests for ``baldur.bootstrap._warn_unknown_env_vars`` (576 D2/D3/D5).

The startup scan compares the ``BALDUR_*`` keys present in ``os.environ``
against the union of every Pydantic ``(env_prefix, field)`` and the catalogued
direct-read registry. A key matching neither is a likely typo or removed var,
surfaced as exactly one ``baldur.unknown_env_var_detected`` WARNING carrying the
var **name** (and a difflib near-miss hint) but **never the value**.

Covers the Success Criteria of impl doc 576:

- unknown var warns (naming the var)
- legitimate direct-read var (``BALDUR_LOG_LEVEL`` / ``BALDUR_ENVIRONMENT``) silent
- Pydantic-resolvable var (``BALDUR_DLQ_MAX_SIZE``) silent; typo
  (``BALDUR_DLQ_MAX_SIE``) warns naming ``BALDUR_DLQ_MAX_SIZE`` as nearest
- ``BALDUR_SUPPRESS_UNKNOWN_ENV_WARNING`` silences the scan wholesale
- mixed-case but consumable var (``BALDUR_Dlq_Max_Size``) silent
- the event carries the var name but no value (secret-leak guard)
- a var resembling nothing still warns with no hint and never raises
- a lazily-loaded-module var (``BALDUR_DLQ_OUTBOX_ENABLED``) silent (force-load)
- a degraded-mode knob (``BALDUR_DEFAULT_TIMEOUT_MS``) silent (Channel-2)
- internal failure degrades to DEBUG and never fails boot

Verification techniques (per UNIT_TEST_GUIDELINES §8):

- §8.4 Side effects (WARNING capture via ``structlog.testing.capture_logs``).
- §8.5 Dependency interaction (``monkeypatch.setenv`` + real settings reflection).

Lives at ``tests/unit/`` (not a subpackage) because ``baldur.bootstrap`` is
a top-level module — same convention as the sibling ``test_bootstrap_*.py``.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

_EVENT = "baldur.unknown_env_var_detected"
_SCAN_FAILED_EVENT = "baldur.unknown_env_var_scan_failed"


# =============================================================================
# Helpers
# =============================================================================


def _unknown_events(captured: list[dict]) -> list[dict]:
    """All unknown-env-var WARNING events in a structlog capture."""
    return [entry for entry in captured if entry.get("event") == _EVENT]


def _events_for(captured: list[dict], var: str) -> list[dict]:
    """Unknown-env-var events naming a specific var."""
    return [entry for entry in _unknown_events(captured) if entry.get("env_var") == var]


@pytest.fixture(autouse=True)
def _ensure_channel2_registered():
    # The degraded-mode knobs register via Channel-2 at handler import; ensure
    # that has happened before any scan so ``BALDUR_DEFAULT_TIMEOUT_MS`` etc.
    # are classified as known.
    import baldur.core.degraded_mode_handler  # noqa: F401


# =============================================================================
# _warn_unknown_env_vars — Behavior
# =============================================================================


class TestWarnUnknownEnvVarsBehavior:
    """One WARNING per unknown BALDUR_* var; known vars stay silent."""

    def test_unknown_var_emits_exactly_one_warning_naming_the_var(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: an env var that maps to no settings field and no direct read
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv("BALDUR_FOO_BAR", "1")

        # When
        with capture_logs() as captured:
            _warn_unknown_env_vars()

        # Then: exactly one WARNING naming BALDUR_FOO_BAR
        events = _events_for(captured, "BALDUR_FOO_BAR")
        assert len(events) == 1
        assert events[0]["log_level"] == "warning"

    @pytest.mark.parametrize("var", ["BALDUR_LOG_LEVEL", "BALDUR_ENVIRONMENT"])
    def test_direct_read_var_emits_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, var: str
    ):
        # A catalogued direct-read var has no Pydantic field but is legitimate —
        # it must not be flagged.
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv(var, "info")

        with capture_logs() as captured:
            _warn_unknown_env_vars()

        assert _events_for(captured, var) == []

    def test_pydantic_resolvable_var_emits_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv("BALDUR_DLQ_MAX_SIZE", "5000")

        with capture_logs() as captured:
            _warn_unknown_env_vars()

        assert _events_for(captured, "BALDUR_DLQ_MAX_SIZE") == []

    def test_typo_var_warns_with_real_var_as_nearest(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: the motivating typo of a real field
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv("BALDUR_DLQ_MAX_SIE", "5000")

        # When
        with capture_logs() as captured:
            _warn_unknown_env_vars()

        # Then: warned, and difflib points at the real var it most resembles
        events = _events_for(captured, "BALDUR_DLQ_MAX_SIE")
        assert len(events) == 1
        assert events[0]["nearest"] == "BALDUR_DLQ_MAX_SIZE"

    @pytest.mark.parametrize(
        "suppress_value", ["true", "True", "TRUE", "1", "yes", "on"]
    )
    def test_suppress_flag_silences_scan_wholesale(
        self, monkeypatch: pytest.MonkeyPatch, suppress_value: str
    ):
        # Given: an unknown var that WOULD warn + the wholesale suppress flag
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv("BALDUR_FOO_BAR", "1")
        monkeypatch.setenv("BALDUR_SUPPRESS_UNKNOWN_ENV_WARNING", suppress_value)

        # When
        with capture_logs() as captured:
            _warn_unknown_env_vars()

        # Then: zero unknown-var events at all (early return before any emission)
        assert _unknown_events(captured) == []

    def test_mixed_case_consumable_var_emits_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Pydantic consumes BALDUR_Dlq_Max_Size identically (case_sensitive=False);
        # the scan upper-case-normalizes to match, so no false positive.
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv("BALDUR_Dlq_Max_Size", "5000")

        with capture_logs() as captured:
            _warn_unknown_env_vars()

        assert _events_for(captured, "BALDUR_Dlq_Max_Size") == []

    def test_lazily_loaded_module_var_emits_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # BALDUR_DLQ_OUTBOX_ENABLED is backed by dlq_outbox.py, which is NOT
        # eager-loaded. Silence proves build_prefix_index() force-loads first.
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "true")

        with capture_logs() as captured:
            _warn_unknown_env_vars()

        assert _events_for(captured, "BALDUR_DLQ_OUTBOX_ENABLED") == []

    def test_degraded_mode_knob_emits_no_warning(self, monkeypatch: pytest.MonkeyPatch):
        # An advertised degraded-mode knob that does not resolve field-level is
        # covered by the Channel-2 registration, so it stays silent.
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv("BALDUR_DEFAULT_TIMEOUT_MS", "5000")

        with capture_logs() as captured:
            _warn_unknown_env_vars()

        assert _events_for(captured, "BALDUR_DEFAULT_TIMEOUT_MS") == []

    def test_event_carries_no_value_secret_leak_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: a typo'd secret-bearing var whose VALUE must never reach the log
        from baldur.bootstrap import _warn_unknown_env_vars

        sentinel = "s3cr3t-sentinel-PLAINTEXT-do-not-leak"
        monkeypatch.setenv("BALDUR_SECRET_KYE", sentinel)

        # When
        with capture_logs() as captured:
            _warn_unknown_env_vars()

        # Then: the var name is warned, but the value appears in NO field of ANY
        # captured event.
        assert len(_events_for(captured, "BALDUR_SECRET_KYE")) == 1
        for entry in captured:
            assert sentinel not in repr(entry)

    def test_special_character_value_still_warns_without_leaking(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Resolution is key-only — a value with whitespace/special chars does not
        # change classification, and is never logged.
        from baldur.bootstrap import _warn_unknown_env_vars

        weird = "  multi line\n\t$pecial=;chars  "
        monkeypatch.setenv("BALDUR_WEIRD_TYPO", weird)

        with capture_logs() as captured:
            _warn_unknown_env_vars()

        assert len(_events_for(captured, "BALDUR_WEIRD_TYPO")) == 1
        for entry in captured:
            assert weird not in repr(entry)

    def test_distant_var_warns_with_no_hint_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A var resembling no known name (the Goal's "removed var" case): still
        # warned, but difflib returns nothing → nearest is None, no exception.
        from baldur.bootstrap import _warn_unknown_env_vars

        monkeypatch.setenv("BALDUR_ZZZ_QQQ_WWW", "1")

        with capture_logs() as captured:
            _warn_unknown_env_vars()

        events = _events_for(captured, "BALDUR_ZZZ_QQQ_WWW")
        assert len(events) == 1
        assert events[0]["nearest"] is None

    def test_internal_failure_degrades_to_debug_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Given: build_prefix_index blows up mid-scan — the diagnostic must never
        # fail boot. An unknown var is also set to prove the error swallowed the
        # would-be warning rather than the var being absent.
        from baldur.bootstrap import _warn_unknown_env_vars

        def _boom() -> dict:
            raise RuntimeError("reflection exploded")

        monkeypatch.setattr("baldur.settings.introspection.build_prefix_index", _boom)
        monkeypatch.setenv("BALDUR_FOO_BAR", "1")

        # When / Then: no exception propagates
        with capture_logs() as captured:
            _warn_unknown_env_vars()

        # No WARNING leaked, and a DEBUG breadcrumb recorded the degrade.
        assert _unknown_events(captured) == []
        scan_failed = [e for e in captured if e.get("event") == _SCAN_FAILED_EVENT]
        assert len(scan_failed) == 1
        assert scan_failed[0]["log_level"] == "debug"
