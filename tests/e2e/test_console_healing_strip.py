"""The healing strip's client logic, driven in a real browser.

Four behaviours here are the reason this lane exists. None of them is a string
that a static anchor could check — each is about *when* something renders
relative to two independent fetches that never wait for each other:

- a counter that reads a process-local, since-boot registry is drawn only once
  it cannot be false, i.e. once a real replay has moved it;
- the sentence stating whose numbers those are outlives the window they sit
  beside, because a counter with no scope is worse than no counter;
- the payload's arrival repaints the strip even when it lands *after* the
  ledger chain has already finished — the failure here is silent and
  order-dependent, so it would pass a hand check about half the time;
- and a failure of the secondary fetch never blanks the primary one.

Each case drives the real admin server over a memory DLQ and manipulates the
network at the browser, which is what a devtools "block this request" does —
only repeatable.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.requires_browser

SCOPE_SENTENCE = "this process, since"
WINDOW_COUNTER = "still unhealed"


def _footer(page) -> str:
    return page.locator("#ledger-foot").inner_text()


def _caption(page) -> str:
    return page.locator("#ledger-caption").inner_text()


def _open_console(page, base_url: str, *, settle_ms: int = 2500) -> None:
    page.goto(base_url + "/", wait_until="load")
    page.wait_for_selector("#ledger-foot")
    page.wait_for_timeout(settle_ms)


class TestHealingStripRendersOnlyWhatIsTrue:
    """A process-local counter is drawn once, and only once, it is a fact."""

    def test_the_counter_appears_only_after_a_real_replay(self, console_world, page):
        """The zero-to-positive transition, end to end.

        This case owns the session's "nothing has replayed yet" state — the
        registry counter is process-global and monotone, so that state exists
        exactly once and this is the first case in the file for that reason. If
        it ever runs after something else replayed, the pre-assertion fails
        loudly rather than passing on a state it did not establish.
        """
        assert console_world.replayed_count() == 0, (
            "another case replayed first — this one owns the zero state"
        )

        _open_console(page, console_world.base_url)
        before = _footer(page)
        assert WINDOW_COUNTER in before, before
        assert "replayed" not in before, (
            "a process-local zero reached the strip: it cannot be told apart "
            "from a replay another process performed"
        )
        assert "humans paged" not in before, before

        console_world.retry_one()
        assert console_world.replayed_count() == 1

        _open_console(page, console_world.base_url)
        after = _footer(page)
        assert "replayed" in after, after
        assert WINDOW_COUNTER in after, "the window counters were lost"

    def test_the_scope_sentence_renders_whenever_the_source_answered(
        self, a_replay_has_happened, page
    ):
        """Caption with no counter means "live source, nothing to report"; no
        caption means "no source". Without it, zero-suppression is unreadable."""
        _open_console(page, a_replay_has_happened.base_url)

        assert SCOPE_SENTENCE in _caption(page), _caption(page)


class TestHealingStripSurvivesTheLedger:
    """The two fetches are independent, and the strip has to prove it."""

    def test_the_counter_outlives_a_dead_window(self, a_replay_has_happened, page):
        """An operator who has healed everything has no window to draw — which
        is the exact moment the counter is most worth showing."""
        page.route("**/dlq/list*", lambda route: route.abort())

        _open_console(page, a_replay_has_happened.base_url)

        footer, caption = _footer(page), _caption(page)
        assert "replayed" in footer, footer
        assert WINDOW_COUNTER not in footer, "a window counter drawn with no window"
        assert SCOPE_SENTENCE in caption, caption
        assert "derived from the newest" not in caption, (
            "the caption described an axis the failed window no longer has"
        )

    def test_a_late_payload_still_repaints_the_strip(self, a_replay_has_happened, page):
        """The silent, order-dependent one.

        Neither chain waits for the other and auto-refresh ships off, so a
        healing payload landing after the ledger finished would otherwise paint
        nothing until the operator reloaded by hand — and land before it often
        enough that a manual check would miss it.

        The request is HELD and released from the test body rather than delayed
        inside the route handler: a blocking sleep in a handler stalls the whole
        driver loop and serialises the very ordering under test.
        """
        held = []
        page.route("**/healing/summary", lambda route: held.append(route))

        page.goto(a_replay_has_happened.base_url + "/", wait_until="load")
        page.wait_for_selector("#ledger-foot")
        page.wait_for_timeout(2000)

        assert held, "the healing request was never intercepted"
        early_footer, early_caption = _footer(page), _caption(page)
        assert WINDOW_COUNTER in early_footer, "the ledger did not paint first"
        assert "replayed" not in early_footer, early_footer
        assert SCOPE_SENTENCE not in early_caption, (
            "a scope sentence rendered before its payload arrived"
        )

        held[0].continue_()
        page.wait_for_timeout(2500)

        late_footer, late_caption = _footer(page), _caption(page)
        assert "replayed" in late_footer, (
            "the arrival re-render never fired: the counter is frozen at "
            "whatever the first paint saw"
        )
        assert SCOPE_SENTENCE in late_caption, late_caption
        assert WINDOW_COUNTER in late_footer, "the repaint dropped the window counters"

    def test_a_failed_healing_fetch_leaves_the_ledger_alone(
        self, a_replay_has_happened, page
    ):
        """Joined into the ledger's chain, this rejection would paint "series
        unavailable" over a plot whose own fetch succeeded."""
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("**/healing/summary", lambda route: route.abort())

        _open_console(page, a_replay_has_happened.base_url)

        footer, caption = _footer(page), _caption(page)
        assert page.locator("#ledger-plot svg").count() == 1, "the plot did not draw"
        assert "series unavailable" not in page.locator("#ledger-plot").inner_text()
        assert WINDOW_COUNTER in footer, footer
        assert "replayed" not in footer, "a counter rendered with no source"
        assert SCOPE_SENTENCE not in caption, "a scope sentence with nothing to scope"
        assert not errors, f"unhandled rejection reached the page: {errors}"
