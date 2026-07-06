"""DLQSettings.domain_cardinality_alert_threshold contract + boundary tests (#544 D3).

The threshold gates the ``redis_dlq.domain_cardinality_alert`` WARNING --
operators can configure it via the ``BALDUR_DLQ_DOMAIN_CARDINALITY_ALERT_THRESHOLD``
env var. The field is bounded (ge=10, le=100_000) so the alert cannot be
disabled by a too-low number (which would fire on every create) nor by a
too-high number (which would never fire in practice).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.dlq import DLQSettings


class TestDLQDomainCardinalityAlertThresholdContract:
    """Default + field constants for the new threshold field (#544 D3)."""

    def test_default_threshold_is_1024(self):
        # Per impl doc 544 D3: default is 1024 -- generous headroom over
        # typical @domain_tag cardinality (developer-assigned, low double
        # digits) while still flagging a buggy str(uuid4()) explosion early.
        assert DLQSettings().domain_cardinality_alert_threshold == 1024


class TestDLQDomainCardinalityAlertThresholdBoundaryContract:
    """``domain_cardinality_alert_threshold`` ge=10, le=100_000.

    Boundary values are design specification — hardcoded per §0.1.
    """

    def test_minimum_accepted(self):
        s = DLQSettings(domain_cardinality_alert_threshold=10)
        assert s.domain_cardinality_alert_threshold == 10

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQSettings(domain_cardinality_alert_threshold=9)

    def test_maximum_accepted(self):
        s = DLQSettings(domain_cardinality_alert_threshold=100_000)
        assert s.domain_cardinality_alert_threshold == 100_000

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQSettings(domain_cardinality_alert_threshold=100_001)


class TestDLQDomainCardinalityAlertThresholdEnvBindingBehavior:
    """The threshold binds from ``BALDUR_DLQ_DOMAIN_CARDINALITY_ALERT_THRESHOLD``.

    Env-var → Pydantic field propagation is behavior — verified via the
    actual binding mechanism (§0.2).
    """

    def test_threshold_binds_from_env(self, monkeypatch):
        monkeypatch.setenv("BALDUR_DLQ_DOMAIN_CARDINALITY_ALERT_THRESHOLD", "2048")
        s = DLQSettings()
        assert s.domain_cardinality_alert_threshold == 2048
