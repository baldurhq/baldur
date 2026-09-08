"""Unit tests for ``baldur.api.middleware.emergency_shedding``.

Scope:
    - ``check_emergency_shedding`` exits: OPTIONS passthrough, the enable gate,
      the PRO clean no-op (empty emergency-manager slot), the rules-import
      guard, the NORMAL+NONE fast path, and the three decision exits.
    - The Most Restrictive Wins merge of the emergency and backpressure per-tier
      multiplier tables, cell by cell.
    - Fail-open: every producer that can raise leaves the request allowed and
      records exactly one WARNING, never a 500.
    - The 503 rejection wire format (body keys + ``Retry-After``), both halves
      driven by ``EmergencyModeSettings.shed_retry_after_seconds``.
    - ``_should_allow`` boundaries and its RNG source.
    - The classification posture the helper inherits from the tiering core: an
      unmapped route is ``non_essential``, a private source address is
      ``critical`` by shipped override.

The helper resolves every dependency through a module-level accessor
(``_get_shedding_settings`` / ``_emergency_manager`` / ``_level_rules`` /
``_backpressure_level`` / ``_tier_registry``), so tests patch those seams to
inject deterministic doubles without registering the PRO package or touching the
singletons the rest of the session shares — the strategy
``test_admission_helpers.py`` uses for ``check_admission``.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.api.middleware import emergency_shedding as shedding
from baldur.api.middleware.emergency_shedding import (
    _should_allow,
    check_emergency_shedding,
)
from baldur.interfaces.emergency import EmergencyManager
from baldur.interfaces.web_framework import HttpMethod, RequestContext, ResponseContext
from baldur.models.emergency import EmergencyLevel
from baldur.scaling.tiering.defaults import BACKPRESSURE_TIER_RULES
from baldur.scaling.tiering.enums import TierFallbackReason
from baldur.scaling.tiering.models import TierResult
from baldur.scaling.tiering.registry import TierRegistry
from baldur.settings.backpressure import BackpressureLevel
from baldur.settings.emergency_mode import EmergencyModeSettings

# The per-emergency-level tier multipliers the PRO enums module ships. Mirrored
# here so this OSS suite runs on a PRO-absent checkout (the public repo's CI);
# TestSheddingRuleMirrorContract asserts the mirror against the real table
# whenever the PRO package IS installed, so the copy cannot drift.
_EMERGENCY_RULES: dict[EmergencyLevel, dict[str, float]] = {
    EmergencyLevel.NORMAL: {"critical": 1.0, "standard": 1.0, "non_essential": 1.0},
    EmergencyLevel.LEVEL_1: {"critical": 1.0, "standard": 1.0, "non_essential": 0.0},
    EmergencyLevel.LEVEL_2: {"critical": 1.0, "standard": 0.1, "non_essential": 0.0},
    EmergencyLevel.LEVEL_3: {"critical": 0.5, "standard": 0.0, "non_essential": 0.0},
}

# A public (TEST-NET-3) source address. Deliberate: the shipped
# DEFAULT_TIER_OVERRIDES classify every RFC 1918 address as `critical`, which
# would mask every shed assertion below. TestSheddingClassificationPosture
# covers the private-range half on purpose.
_PUBLIC_CLIENT_IP = "203.0.113.7"
_PRIVATE_CLIENT_IP = "192.168.1.50"

# =============================================================================
# Builders
# =============================================================================


def _make_request(
    *,
    method: HttpMethod = HttpMethod.GET,
    path: str = "/api/orders/",
    client_ip: str | None = _PUBLIC_CLIENT_IP,
    user: object | None = None,
    is_authenticated: bool = False,
) -> RequestContext:
    return RequestContext(
        method=method,
        path=path,
        client_ip=client_ip,
        user=user,
        is_authenticated=is_authenticated,
    )


def _settings(*, enabled: bool = True, retry_after: int = 30) -> EmergencyModeSettings:
    """A real settings instance — the helper reads two of its fields."""
    return EmergencyModeSettings(
        shedding_enabled=enabled,
        shed_retry_after_seconds=retry_after,
    )


def _manager(*, active: bool, level: EmergencyLevel) -> MagicMock:
    manager = MagicMock(spec=EmergencyManager)
    manager.is_active.return_value = active
    manager.get_current_level.return_value = level
    return manager


def _tier_result(tier_id: str) -> TierResult:
    return TierResult(
        tier_id=tier_id,
        multiplier=1.0,
        is_fallback=False,
        fallback_reason=TierFallbackReason.NONE,
        latency_ms=0.1,
    )


def _registry_returning(tier_id: str) -> MagicMock:
    registry = MagicMock(spec=TierRegistry)
    registry.resolve_tier_with_fallback.return_value = _tier_result(tier_id)
    return registry


@contextmanager
def _shedding_active(
    *,
    tier_id: str = "standard",
    level: EmergencyLevel = EmergencyLevel.LEVEL_1,
    active: bool = True,
    bp_level: BackpressureLevel = BackpressureLevel.NONE,
    settings: EmergencyModeSettings | None = None,
    registry: MagicMock | None = None,
):
    """Patch every seam so the PRO decision path runs deterministically.

    Yields ``(manager, registry)`` so callers can assert on the injected
    doubles.
    """
    manager = _manager(active=active, level=level)
    tier_registry = registry if registry is not None else _registry_returning(tier_id)

    with (
        patch.object(
            shedding, "_get_shedding_settings", return_value=settings or _settings()
        ),
        patch.object(shedding, "_emergency_manager", return_value=manager),
        patch.object(shedding, "_level_rules", return_value=_EMERGENCY_RULES),
        patch.object(shedding, "_backpressure_level", return_value=bp_level),
        patch.object(shedding, "_tier_registry", return_value=tier_registry),
    ):
        yield manager, tier_registry


def _warnings(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e.get("event") == event]


# =============================================================================
# Gate exits (E1-E5) — Behavior
# =============================================================================


class TestEmergencySheddingGateBehavior:
    """The four early exits that allow before any classification happens."""

    def test_options_request_allows_without_reading_settings(self):
        """CORS preflight short-circuits ahead of the enable gate (E1)."""
        with patch.object(shedding, "_get_shedding_settings") as mock_settings:
            result = check_emergency_shedding(_make_request(method=HttpMethod.OPTIONS))

        assert result is None
        mock_settings.assert_not_called()

    def test_disabled_flag_allows_without_probing_the_pro_slot(self):
        """shedding_enabled=False short-circuits before the manager slot read (E2)."""
        with (
            patch.object(
                shedding,
                "_get_shedding_settings",
                return_value=_settings(enabled=False),
            ),
            patch.object(shedding, "_emergency_manager") as mock_manager,
        ):
            result = check_emergency_shedding(_make_request())

        assert result is None
        mock_manager.assert_not_called()

    def test_missing_settings_is_treated_as_disabled(self):
        """A settings layer that cannot answer allows the request (E2)."""
        with (
            patch.object(shedding, "_get_shedding_settings", return_value=None),
            patch.object(shedding, "_emergency_manager") as mock_manager,
        ):
            result = check_emergency_shedding(_make_request())

        assert result is None
        mock_manager.assert_not_called()

    def test_empty_manager_slot_allows_without_classification_or_pro_import(self):
        """OSS / unentitled: clean no-op — no tier lookup, no rules import (E3)."""
        with (
            patch.object(shedding, "_get_shedding_settings", return_value=_settings()),
            patch.object(shedding, "_emergency_manager", return_value=None),
            patch.object(shedding, "_level_rules") as mock_rules,
            patch.object(shedding, "_tier_registry") as mock_registry,
        ):
            result = check_emergency_shedding(_make_request())

        assert result is None
        mock_rules.assert_not_called()
        mock_registry.assert_not_called()

    def test_empty_manager_slot_emits_no_log_record(self):
        """The PRO-absent path is silent — it fires once per request (G7)."""
        with (
            patch.object(shedding, "_get_shedding_settings", return_value=_settings()),
            patch.object(shedding, "_emergency_manager", return_value=None),
            capture_logs() as logs,
        ):
            check_emergency_shedding(_make_request())

        assert logs == []

    def test_unavailable_level_rules_allow_without_classification(self):
        """The rules-import guard allows and never classifies (E4)."""
        with (
            patch.object(shedding, "_get_shedding_settings", return_value=_settings()),
            patch.object(
                shedding,
                "_emergency_manager",
                return_value=_manager(active=True, level=EmergencyLevel.LEVEL_3),
            ),
            patch.object(shedding, "_level_rules", return_value=None),
            patch.object(shedding, "_tier_registry") as mock_registry,
        ):
            result = check_emergency_shedding(_make_request())

        assert result is None
        mock_registry.assert_not_called()

    def test_normal_level_and_no_backpressure_allow_before_classification(self):
        """Steady state costs two level reads and no tier lookup (E5)."""
        with _shedding_active(
            active=False,
            level=EmergencyLevel.NORMAL,
            bp_level=BackpressureLevel.NONE,
        ) as (manager, registry):
            result = check_emergency_shedding(_make_request())

        assert result is None
        registry.resolve_tier_with_fallback.assert_not_called()

    def test_inactive_manager_is_not_asked_for_its_level(self):
        """is_active() gates the level read, so an inactive process reads no state."""
        with _shedding_active(
            active=False,
            level=EmergencyLevel.LEVEL_3,
            bp_level=BackpressureLevel.NONE,
        ) as (manager, _):
            check_emergency_shedding(_make_request())

        manager.get_current_level.assert_not_called()


# =============================================================================
# Decision table (E6-E8) — Behavior
# =============================================================================


def _emergency_cells() -> list[tuple[EmergencyLevel, str, float]]:
    """Every (level, tier) cell of the emergency table past the fast path."""
    return [
        (level, tier, multiplier)
        for level, rules in _EMERGENCY_RULES.items()
        if level is not EmergencyLevel.NORMAL
        for tier, multiplier in rules.items()
    ]


def _backpressure_cells() -> list[tuple[BackpressureLevel, str, float]]:
    """Every (level, tier) cell of the backpressure table past the fast path."""
    return [
        (level, tier, multiplier)
        for level, rules in BACKPRESSURE_TIER_RULES.items()
        if level is not BackpressureLevel.NONE
        for tier, multiplier in rules.items()
    ]


class TestEmergencySheddingDecisionBehavior:
    """The merged multiplier the decision is taken against, cell by cell."""

    @pytest.mark.parametrize(
        ("level", "tier", "multiplier"),
        _emergency_cells(),
        ids=[f"{lvl.value}-{tier}" for lvl, tier, _ in _emergency_cells()],
    )
    def test_emergency_table_cell_reaches_the_decision(self, level, tier, multiplier):
        """With backpressure NONE the emergency multiplier decides alone."""
        with (
            _shedding_active(tier_id=tier, level=level),
            patch.object(shedding, "_should_allow", return_value=True) as mock_allow,
        ):
            check_emergency_shedding(_make_request())

        mock_allow.assert_called_once_with(multiplier)

    @pytest.mark.parametrize(
        ("bp_level", "tier", "multiplier"),
        _backpressure_cells(),
        ids=[f"{lvl.value}-{tier}" for lvl, tier, _ in _backpressure_cells()],
    )
    def test_backpressure_table_cell_reaches_the_decision(
        self, bp_level, tier, multiplier
    ):
        """Emergency NORMAL + an active backpressure level: backpressure decides."""
        with (
            _shedding_active(
                tier_id=tier,
                active=False,
                level=EmergencyLevel.NORMAL,
                bp_level=bp_level,
            ),
            patch.object(shedding, "_should_allow", return_value=True) as mock_allow,
        ):
            check_emergency_shedding(_make_request())

        mock_allow.assert_called_once_with(multiplier)

    def test_merge_takes_the_emergency_half_when_it_is_lower(self):
        """LEVEL_2 standard (0.1) beats backpressure HIGH standard (0.5)."""
        expected = min(
            _EMERGENCY_RULES[EmergencyLevel.LEVEL_2]["standard"],
            BACKPRESSURE_TIER_RULES[BackpressureLevel.HIGH]["standard"],
        )
        with (
            _shedding_active(
                tier_id="standard",
                level=EmergencyLevel.LEVEL_2,
                bp_level=BackpressureLevel.HIGH,
            ),
            patch.object(shedding, "_should_allow", return_value=True) as mock_allow,
        ):
            check_emergency_shedding(_make_request())

        mock_allow.assert_called_once_with(expected)

    def test_merge_takes_the_backpressure_half_when_it_is_lower(self):
        """LEVEL_1 critical (1.0) yields to backpressure CRITICAL critical (0.8)."""
        expected = min(
            _EMERGENCY_RULES[EmergencyLevel.LEVEL_1]["critical"],
            BACKPRESSURE_TIER_RULES[BackpressureLevel.CRITICAL]["critical"],
        )
        with (
            _shedding_active(
                tier_id="critical",
                level=EmergencyLevel.LEVEL_1,
                bp_level=BackpressureLevel.CRITICAL,
            ),
            patch.object(shedding, "_should_allow", return_value=True) as mock_allow,
        ):
            check_emergency_shedding(_make_request())

        mock_allow.assert_called_once_with(expected)

    def test_unknown_tier_id_falls_back_to_full_traffic(self):
        """A tier the rule tables do not name resolves to 1.0 -> allow."""
        with _shedding_active(tier_id="bespoke_tier", level=EmergencyLevel.LEVEL_3):
            result = check_emergency_shedding(_make_request())

        assert result is None

    def test_zero_multiplier_rejects_deterministically(self):
        """LEVEL_1 non_essential (0.0) is shed without consulting the RNG (E7)."""
        with (
            _shedding_active(tier_id="non_essential", level=EmergencyLevel.LEVEL_1),
            patch.object(shedding.random, "random") as mock_random,
        ):
            result = check_emergency_shedding(_make_request())

        assert isinstance(result, ResponseContext)
        assert result.status_code == 503
        mock_random.assert_not_called()

    def test_full_multiplier_allows_deterministically(self):
        """LEVEL_1 critical (1.0) is allowed without consulting the RNG (E6)."""
        with (
            _shedding_active(tier_id="critical", level=EmergencyLevel.LEVEL_1),
            patch.object(shedding.random, "random") as mock_random,
        ):
            result = check_emergency_shedding(_make_request())

        assert result is None
        mock_random.assert_not_called()

    def test_fractional_multiplier_allows_when_draw_is_below_threshold(self):
        """LEVEL_3 critical is 0.5: a 0.49 draw survives (E8)."""
        with (
            _shedding_active(tier_id="critical", level=EmergencyLevel.LEVEL_3),
            patch.object(shedding.random, "random", return_value=0.49),
        ):
            result = check_emergency_shedding(_make_request())

        assert result is None

    def test_fractional_multiplier_rejects_when_draw_reaches_threshold(self):
        """LEVEL_3 critical is 0.5: a 0.50 draw is shed (E8, closed upper half)."""
        with (
            _shedding_active(tier_id="critical", level=EmergencyLevel.LEVEL_3),
            patch.object(shedding.random, "random", return_value=0.50),
        ):
            result = check_emergency_shedding(_make_request())

        assert isinstance(result, ResponseContext)
        assert result.status_code == 503

    def test_classification_receives_path_ip_and_method(self):
        """The four classification inputs are forwarded to the tier registry."""
        request = _make_request(method=HttpMethod.POST, path="/api/orders/")
        with _shedding_active(level=EmergencyLevel.LEVEL_1) as (_, registry):
            check_emergency_shedding(request)

        kwargs = registry.resolve_tier_with_fallback.call_args.kwargs
        assert kwargs["path"] == "/api/orders/"
        assert kwargs["client_ip"] == _PUBLIC_CLIENT_IP
        assert kwargs["method"] == "POST"
        assert kwargs["user_id"] is None

    def test_authenticated_user_id_is_forwarded_as_a_string(self):
        """An authenticated principal contributes its id to classification."""
        user = type("_User", (), {"id": 42})()
        request = _make_request(user=user, is_authenticated=True)
        with _shedding_active(level=EmergencyLevel.LEVEL_1) as (_, registry):
            check_emergency_shedding(request)

        assert registry.resolve_tier_with_fallback.call_args.kwargs["user_id"] == "42"

    def test_rejection_is_logged_at_info_with_both_levels(self):
        """The per-shed record carries tier, both levels and the merged multiplier."""
        with (
            _shedding_active(
                tier_id="non_essential",
                level=EmergencyLevel.LEVEL_1,
                bp_level=BackpressureLevel.MEDIUM,
            ),
            capture_logs() as logs,
        ):
            check_emergency_shedding(_make_request())

        records = _warnings(logs, "emergency_shedding.request_rejected")
        assert len(records) == 1
        assert records[0]["log_level"] == "info"
        assert records[0]["tier"] == "non_essential"
        assert records[0]["emergency_level"] == EmergencyLevel.LEVEL_1.value
        assert records[0]["backpressure_level"] == BackpressureLevel.MEDIUM.value
        assert records[0]["multiplier"] == 0.0

    def test_allowed_request_emits_no_rejection_record(self):
        """A survivor of the probabilistic gate leaves no per-request log line."""
        with (
            _shedding_active(tier_id="critical", level=EmergencyLevel.LEVEL_1),
            capture_logs() as logs,
        ):
            check_emergency_shedding(_make_request())

        assert _warnings(logs, "emergency_shedding.request_rejected") == []

    def test_shed_is_recorded_on_the_emergency_mode_counter(self):
        """The rejection is counted with the tier and both level labels (D4)."""
        with (
            _shedding_active(
                tier_id="non_essential",
                level=EmergencyLevel.LEVEL_2,
                bp_level=BackpressureLevel.LOW,
            ),
            patch(
                "baldur.metrics.recorders.emergency_mode.record_em_shed"
            ) as mock_record,
        ):
            check_emergency_shedding(_make_request())

        mock_record.assert_called_once_with(
            "non_essential", EmergencyLevel.LEVEL_2, BackpressureLevel.LOW
        )


# =============================================================================
# Fail-open (E9) — Behavior
# =============================================================================


class TestEmergencySheddingFailOpenBehavior:
    """A protection feature never 500s the request it is protecting."""

    def _assert_allowed_with_one_warning(self, logs, result):
        assert result is None
        records = _warnings(logs, "emergency_shedding.check_failed")
        assert len(records) == 1
        assert records[0]["log_level"] == "warning"

    def test_raising_slot_provider_allows_and_warns_once(self):
        """A provider that raises past safe_get() is caught by the guard."""
        with (
            patch.object(shedding, "_get_shedding_settings", return_value=_settings()),
            patch.object(
                shedding, "_emergency_manager", side_effect=RuntimeError("slot boom")
            ),
            capture_logs() as logs,
        ):
            result = check_emergency_shedding(_make_request())

        self._assert_allowed_with_one_warning(logs, result)

    def test_raising_level_read_allows_and_warns_once(self):
        """A manager whose state backend cannot be built never reaches the caller."""
        manager = MagicMock(spec=EmergencyManager)
        manager.is_active.side_effect = RuntimeError("redis unreachable")
        with (
            patch.object(shedding, "_get_shedding_settings", return_value=_settings()),
            patch.object(shedding, "_emergency_manager", return_value=manager),
            patch.object(shedding, "_level_rules", return_value=_EMERGENCY_RULES),
            capture_logs() as logs,
        ):
            result = check_emergency_shedding(_make_request())

        self._assert_allowed_with_one_warning(logs, result)

    def test_raising_backpressure_read_allows_and_warns_once(self):
        with (
            patch.object(shedding, "_get_shedding_settings", return_value=_settings()),
            patch.object(
                shedding,
                "_emergency_manager",
                return_value=_manager(active=True, level=EmergencyLevel.LEVEL_1),
            ),
            patch.object(shedding, "_level_rules", return_value=_EMERGENCY_RULES),
            patch.object(
                shedding, "_backpressure_level", side_effect=RuntimeError("controller")
            ),
            capture_logs() as logs,
        ):
            result = check_emergency_shedding(_make_request())

        self._assert_allowed_with_one_warning(logs, result)

    def test_raising_tier_registry_allows_and_warns_once(self):
        registry = MagicMock(spec=TierRegistry)
        registry.resolve_tier_with_fallback.side_effect = RuntimeError("classifier")
        with (
            _shedding_active(level=EmergencyLevel.LEVEL_1, registry=registry),
            capture_logs() as logs,
        ):
            result = check_emergency_shedding(_make_request())

        self._assert_allowed_with_one_warning(logs, result)

    def test_settings_singleton_failure_is_absorbed_by_its_own_accessor(self):
        """The settings accessor degrades to None and names its own event."""
        with (
            patch(
                "baldur.settings.emergency_mode.get_emergency_mode_settings",
                side_effect=RuntimeError("settings layer down"),
            ),
            capture_logs() as logs,
        ):
            result = check_emergency_shedding(_make_request())

        assert result is None
        records = _warnings(logs, "emergency_shedding.settings_load_failed")
        assert len(records) == 1
        assert records[0]["log_level"] == "warning"


# =============================================================================
# 503 rejection wire format — Contract
# =============================================================================


class TestEmergencySheddingResponseContract:
    """The shed response is a public wire format; Django shipped it unchanged."""

    def _reject(self, *, retry_after: int = 30) -> ResponseContext:
        with _shedding_active(
            tier_id="non_essential",
            level=EmergencyLevel.LEVEL_1,
            settings=_settings(retry_after=retry_after),
        ):
            result = check_emergency_shedding(_make_request())
        assert isinstance(result, ResponseContext)
        return result

    def test_status_code_is_503(self):
        assert self._reject().status_code == 503

    def test_body_carries_the_documented_keys(self):
        body = self._reject().body

        assert body["error"] == "Service Temporarily Unavailable"
        assert body["code"] == "LOAD_SHEDDING"
        assert body["tier"] == "non_essential"
        assert body["emergency_level"] == "level_1"
        assert body["retry_after"] == 30
        assert isinstance(body["message"], str)

    def test_emergency_level_is_exported_as_its_value_string(self):
        """A raw (str, Enum) member would render as 'EmergencyLevel.LEVEL_1'."""
        assert self._reject().body["emergency_level"] == EmergencyLevel.LEVEL_1.value

    def test_retry_after_header_is_a_string(self):
        """ResponseContext.headers is dict[str, str]; the FastAPI encoder coerces nothing."""
        headers = self._reject().headers

        assert headers["Retry-After"] == "30"
        assert isinstance(headers["Retry-After"], str)

    def test_retry_after_setting_drives_body_and_header_together(self):
        rejection = self._reject(retry_after=10)

        assert rejection.body["retry_after"] == 10
        assert rejection.headers["Retry-After"] == "10"


# =============================================================================
# _should_allow — Behavior / Contract
# =============================================================================


class TestShouldAllowBehavior:
    """The probabilistic predicate: closed at both ends, uniform in between."""

    @pytest.mark.parametrize("multiplier", [1.0, 1.5])
    def test_multiplier_at_or_above_one_always_allows(self, multiplier):
        assert _should_allow(multiplier) is True

    @pytest.mark.parametrize("multiplier", [0.0, -0.1])
    def test_multiplier_at_or_below_zero_always_rejects(self, multiplier):
        assert _should_allow(multiplier) is False

    @pytest.mark.parametrize(
        ("draw", "expected"),
        [(0.0, True), (0.24, True), (0.25, False), (0.99, False)],
        ids=["zero", "below", "at_threshold", "above"],
    )
    def test_fractional_multiplier_compares_the_draw_strictly(self, draw, expected):
        """`random() < multiplier` — the draw at the threshold is rejected."""
        with patch.object(shedding.random, "random", return_value=draw):
            assert _should_allow(0.25) is expected

    def test_deterministic_ends_never_draw(self):
        """Neither closed end consumes RNG state (the hot path stays cheap)."""
        with patch.object(shedding.random, "random") as mock_random:
            _should_allow(1.0)
            _should_allow(0.0)

        mock_random.assert_not_called()


class TestShouldAllowRngContract:
    """The RNG source is the stdlib default instance, not a private Random.

    CPython reseeds the default instance in every forked child
    (``register_at_fork(after_in_child=_inst.seed)``), so preloading gunicorn
    workers do not inherit one shed sequence. A private ``random.Random()``
    constructed at import or at middleware construction would. The guarantee
    belongs to the stdlib, so it is pinned at the source level rather than by
    forking inside a test.
    """

    def test_helper_source_constructs_no_private_random_instance(self):
        source = inspect.getsource(shedding)

        assert "random.Random(" not in source

    def test_helper_draws_from_the_module_default_instance(self):
        import random as stdlib_random

        assert shedding.random is stdlib_random


# =============================================================================
# Classification posture — Behavior (real tier registry)
# =============================================================================


class TestSheddingClassificationPostureBehavior:
    """What the shipped tiering defaults do to an application route.

    Driven through a real ``TierRegistry`` (default mappings + overrides), not a
    double, because the posture under test IS the shipped configuration.
    """

    @pytest.fixture
    def real_registry(self) -> TierRegistry:
        registry = TierRegistry.__new__(TierRegistry)
        registry._init()
        return registry

    def test_unmapped_route_from_a_public_client_is_shed_at_level_1(
        self, real_registry
    ):
        """An application route nobody mapped is `non_essential` — shed first."""
        with _shedding_active(level=EmergencyLevel.LEVEL_1, registry=real_registry):
            result = check_emergency_shedding(
                _make_request(path="/checkout/", client_ip=_PUBLIC_CLIENT_IP)
            )

        assert isinstance(result, ResponseContext)
        assert result.body["tier"] == "non_essential"

    def test_private_range_client_survives_level_1_by_shipped_override(
        self, real_registry
    ):
        """DEFAULT_TIER_OVERRIDES classify RFC 1918 sources as `critical`.

        Behind a proxy that does not set X-Forwarded-For, every request looks
        internal and LEVEL_1/LEVEL_2 shed nothing. Pre-existing on Django and
        unchanged by the extraction; asserted here so the posture stays visible
        while it is parked.
        """
        with _shedding_active(level=EmergencyLevel.LEVEL_1, registry=real_registry):
            result = check_emergency_shedding(
                _make_request(path="/checkout/", client_ip=_PRIVATE_CLIENT_IP)
            )

        assert result is None

    def test_baldur_control_path_survives_level_1(self, real_registry):
        """The shipped mappings keep the control plane reachable during an emergency."""
        with _shedding_active(level=EmergencyLevel.LEVEL_1, registry=real_registry):
            result = check_emergency_shedding(
                _make_request(path="/api/baldur/control/", client_ip=_PUBLIC_CLIENT_IP)
            )

        assert result is None


# =============================================================================
# EmergencyManager Protocol — Contract
# =============================================================================


class TestEmergencyManagerProtocolContract:
    """The Protocol declares every method OSS code calls (D8)."""

    def test_protocol_declares_is_active(self):
        assert callable(EmergencyManager.is_active)

    def test_spec_mock_accepts_the_methods_the_helper_calls(self):
        """A `spec=EmergencyManager` double must satisfy the helper's level reads."""
        manager = MagicMock(spec=EmergencyManager)

        assert manager.is_active() is not None
        assert manager.get_current_level() is not None


# =============================================================================
# Rule-table mirror — Contract (PRO only)
# =============================================================================


class TestSheddingRuleMirrorContract:
    """The locally mirrored emergency table equals the shipped PRO table.

    The mirror exists so this suite runs on a PRO-absent checkout; this case is
    the anti-drift assertion and skips where the package is absent.
    """

    def test_local_mirror_matches_the_shipped_emergency_level_rules(self):
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.emergency_mode.enums import EMERGENCY_LEVEL_RULES

        assert _EMERGENCY_RULES == EMERGENCY_LEVEL_RULES
