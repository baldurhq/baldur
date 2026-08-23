"""The immediate delivery legs record the event's time, not the flush time.

Both constructors on the request/async path built their ``AuditEntry`` without
a ``timestamp``, so the dataclass default stamped the row at construction —
which happens on the async logger's batched flush thread, or at response time
in the middleware, not when the audited thing happened. Combined with the
adapter change that makes a row honour its entry's timestamp, these two
call sites are what carry the event's own time all the way to the ledger.

A malformed timestamp must cost the timestamp, never the entry: an
unparseable value degrades to the default rather than dropping the row, since
a compliance ledger missing a row is strictly worse than one with an
approximate time on it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from baldur.audit.async_audit_lifecycle import (
    _event_time_kwarg,
    create_audit_flush_callback,
)
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry, ContextType

EVENT_TIME = datetime(2020, 6, 15, 12, 0, 0, tzinfo=UTC)
FLUSH_TIME = datetime(2026, 7, 1, 9, 30, 0, tzinfo=UTC)

SINGLETON = "baldur.adapters.audit.singleton.get_audit_adapter"


class RecordingAdapter:
    """An audit adapter that only remembers what it was handed.

    Deliberately exposes ``log`` and not ``log_batch``, so the callback takes
    its per-entry branch and each entry is observable on its own.
    """

    def __init__(self):
        self.entries: list[AuditEntry] = []

    def log(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


def _event(**overrides) -> dict:
    """One batched event dict, in the shape the async logger flushes."""
    event = {
        "action": "cb_force_open",
        "timestamp": EVENT_TIME.isoformat(),
        "actor_id": "alice",
        "target_type": "circuit_breaker",
        "target_id": "payment-pg",
        "details": {"reason": "operator force"},
    }
    event.update(overrides)
    return event


def _flush(events: list[dict]) -> RecordingAdapter:
    """Run the real flush callback over ``events`` and return the adapter."""
    adapter = RecordingAdapter()
    with patch(SINGLETON, return_value=adapter):
        create_audit_flush_callback()(events)
    return adapter


class TestLegTwoPreservesEventTime:
    """The async flush callback carries each event's own timestamp."""

    def test_the_events_own_time_reaches_the_entry(self):
        """The whole point: the row is stamped when the thing happened."""
        adapter = _flush([_event()])

        assert adapter.entries[0].timestamp == EVENT_TIME

    def test_the_flush_time_does_not_replace_it(self):
        """The explicit negative — the pre-fix entry carried the flush time,
        which for a batched logger can be an unbounded interval later."""
        with patch("baldur.interfaces.audit_adapter.utc_now", return_value=FLUSH_TIME):
            adapter = _flush([_event()])

        assert adapter.entries[0].timestamp != FLUSH_TIME

    def test_a_datetime_timestamp_is_used_directly(self):
        """The async logger may hand over a ``datetime`` rather than a
        string; both carriers must reach the same entry."""
        adapter = _flush([_event(timestamp=EVENT_TIME)])

        assert adapter.entries[0].timestamp == EVENT_TIME

    def test_a_naive_datetime_is_made_aware(self):
        """A naive value would otherwise compare-fail against every aware row
        in the ledger, raising ``TypeError`` at query time."""
        adapter = _flush([_event(timestamp=EVENT_TIME.replace(tzinfo=None))])

        assert adapter.entries[0].timestamp == EVENT_TIME

    def test_a_zulu_suffix_is_parsed(self):
        """The ``Z`` form is what most emitters write."""
        adapter = _flush([_event(timestamp="2020-06-15T12:00:00Z")])

        assert adapter.entries[0].timestamp == EVENT_TIME

    @pytest.mark.parametrize(
        "raw",
        ["not-a-timestamp", "", None, 1592222400.0, {"t": 1}],
        ids=["garbage", "empty", "none", "float-epoch", "mapping"],
    )
    def test_a_malformed_timestamp_still_delivers_the_entry(self, raw):
        """Degrade the field, never the row: a compliance ledger missing an
        entry is worse than one carrying an approximate time."""
        adapter = _flush([_event(timestamp=raw)])

        assert len(adapter.entries) == 1
        assert adapter.entries[0].timestamp is not None

    def test_a_malformed_timestamp_degrades_to_the_default(self):
        """And the degraded value is the construction-time default, so the
        row is still ordered somewhere sane rather than at epoch 0."""
        with patch("baldur.interfaces.audit_adapter.utc_now", return_value=FLUSH_TIME):
            adapter = _flush([_event(timestamp="not-a-timestamp")])

        assert adapter.entries[0].timestamp == FLUSH_TIME

    def test_each_event_in_a_batch_keeps_its_own_time(self):
        """One flush covers many events; a shared stamp would collapse them
        onto one instant and destroy the ordering an auditor reads."""
        second = datetime(2021, 3, 3, 9, 0, 0, tzinfo=UTC)

        adapter = _flush(
            [_event(), _event(target_id="ledger-pg", timestamp=second.isoformat())]
        )

        assert [entry.timestamp for entry in adapter.entries] == [EVENT_TIME, second]

    def test_the_rest_of_the_entry_is_still_populated(self):
        """The timestamp kwarg is spliced in ahead of the other fields — a
        splat placed wrongly would shadow them."""
        entry = _flush([_event()]).entries[0]

        assert entry.action is AuditAction.CB_FORCE_OPEN
        assert entry.actor_id == "alice"
        assert entry.target_type == "circuit_breaker"
        assert entry.target_id == "payment-pg"


class TestEventTimeKwarg:
    """``_event_time_kwarg`` — the carrier-shape rules, read directly."""

    def test_a_parseable_value_yields_the_kwarg(self):
        assert _event_time_kwarg(EVENT_TIME.isoformat()) == {"timestamp": EVENT_TIME}

    @pytest.mark.parametrize(
        "raw",
        [None, "", "garbage", 1592222400.0, [], {}],
        ids=["none", "empty", "garbage", "float", "list", "dict"],
    )
    def test_an_unusable_value_yields_no_kwarg(self, raw):
        """No kwarg means the dataclass default applies — which is what keeps
        a malformed carrier from costing the whole entry."""
        assert _event_time_kwarg(raw) == {}


class TestRequestPathPreservesEventTime:
    """The middleware's per-event constructor carries the same time.

    Recording happens at response time, so the default would stamp the row
    when the request finished rather than when the audited thing happened.
    """

    @staticmethod
    def _record(event) -> AuditEntry:
        """Drive ``_record_single_event`` with a minimal recorder stand-in.

        The method is called unbound against a stub holding just the one
        attribute it reads, so the assertion is about the constructor rather
        than about Django middleware wiring.
        """
        from baldur.api.django.audit_middleware import AuditMiddleware

        adapter = RecordingAdapter()

        class StubRecorder:
            audit_adapter = adapter

        class StubMiddleware:
            _recorder = StubRecorder()

        AuditMiddleware._record_single_event(StubMiddleware(), event, {})
        assert adapter.entries, "the stub adapter received no entry"
        return adapter.entries[0]

    def _audit_event(self, **overrides):
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        kwargs = {
            "event_type": AuditEventType.CB_STATE_CHANGE,
            "timestamp": EVENT_TIME,
            "source": "CircuitBreaker",
            "details": {"cb_name": "payment-pg"},
        }
        kwargs.update(overrides)
        return AuditEvent(**kwargs)

    def test_the_events_own_time_reaches_the_entry(self):
        entry = self._record(self._audit_event())

        assert entry.timestamp == EVENT_TIME

    def test_the_response_time_does_not_replace_it(self):
        with patch("baldur.interfaces.audit_adapter.utc_now", return_value=FLUSH_TIME):
            entry = self._record(self._audit_event())

        assert entry.timestamp != FLUSH_TIME

    def test_an_absent_event_time_falls_back_to_the_default(self):
        """The kwarg is spliced only when the event carries a time, so an
        event without one still produces a row."""
        with patch("baldur.interfaces.audit_adapter.utc_now", return_value=FLUSH_TIME):
            entry = self._record(self._audit_event(timestamp=None))

        assert entry.timestamp == FLUSH_TIME

    def test_the_middleware_context_is_still_recorded(self):
        """The spliced kwarg must not displace the fields around it."""
        entry = self._record(self._audit_event())

        assert entry.context_type is ContextType.REQUEST
        assert entry.target_type == "CircuitBreaker"
