"""
CB-state event matching contract for postmortem timeline utilities.

Timeline event_type values are an open vocabulary (producers range from
framework emitters to operator-supplied API bodies), so CB states are
recognized by tolerant substring markers via the ordered pattern table in
``baldur.utils.postmortem_root_cause``. These tests pin the ordering and
tolerance invariants that table centralizes.
"""

from baldur.utils.postmortem_root_cause import extract_trigger_info


class TestOpenEventMatchingContract:
    """Ordering and tolerance contract of the CB OPEN event matchers."""

    def _event(self, event_type: str, **details) -> dict:
        return {
            "timestamp": "2026-01-27T14:01:23+09:00",
            "event_type": event_type,
            "details": details,
        }

    def test_trigger_skips_leading_half_open_event(self):
        """A windowed timeline starting at HALF_OPEN must not become the trigger.

        "half_opened" contains "opened" as a substring, so the half-open
        exclusion must run before the open match.
        """
        timeline = [
            self._event("circuit_breaker_half_opened", service_name="database"),
            self._event("circuit_breaker_opened", service_name="database"),
        ]

        result = extract_trigger_info(timeline)

        assert result is not None
        assert result["event_type"] == "circuit_breaker_opened"

    def test_trigger_matches_variant_open_spelling(self):
        """Operator-supplied variants like "cb_open" still match.

        The event vocabulary is open (API bodies may carry any name), so
        the matcher accepts any event_type containing "open" — exact-name
        matching would silently drop such variants.
        """
        timeline = [self._event("cb_open", service_name="database")]

        result = extract_trigger_info(timeline)

        assert result is not None
        assert result["event_type"] == "cb_open"
