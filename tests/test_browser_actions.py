"""The browser driver's ACTIONS, against a real browser.

``test_browser_channel.py`` has a real-browser class, but it drives ``read``.
That is half the driver. The half that files a report is the actions --
``open_menu``, ``pick_menu_item``, ``choose_category``, ``click_submit`` -- and
not one of them had ever been run against a real DOM. ``tests/golden/`` cannot
supply them: a golden page is static by design, and an action needs the page to
react to a click.

So the fixture in ``tests/interactive/`` is a different kind of object. It is not
a captured DOM, it is a *clickable* one, and it is kept out of the golden corpus
on purpose so the drift gates never see it. Every affordance is an
``<a href="#...">``, so a click shows up as a fragment change in the page URL --
which is what lets a test assert *which* element was clicked, not merely that a
locator matched. Clicking the wrong element out of several that match is the
failure the fake driver structurally cannot represent, because a fake is handed
the answer.

The first test in this file is the one that was red when the file was written:
``pick_menu_item`` clicked ``report_menu_item.selectors``, and that anchor is a
text anchor with no selectors, so the click raised ``SelectorDrift: nothing
matched any of []`` on every real run. The browser channel could not get past its
second step, and the suite was green throughout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from insta_report.anchors import load_anchors
from insta_report.errors import ChannelFailError, SelectorDrift

ANCHORS = load_anchors()

_HERE = Path(__file__).parent


def _uri(*parts: str) -> str:
    return _HERE.joinpath(*parts).as_uri()


def interactive(name: str) -> str:
    """A ``file://`` URL for a clickable fixture (reacts to a click)."""
    return _uri("interactive", name)


def golden(name: str) -> str:
    """A ``file://`` URL for a static golden fixture (does not react)."""
    return _uri("golden", name)


async def clicked_to(driver, suffix: str, timeout: float = 5.0) -> bool:
    """Whether the page URL has come to end with ``suffix``.

    A hash-only change is not a navigation Playwright waits on, so asserting the
    URL immediately after ``click`` is a race. Poll, and answer honestly if it
    never arrives -- the assertion that reads this reports the real URL.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if driver._page.url.endswith(suffix):
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.fixture
async def started(tmp_path):
    from insta_report.browser import BrowserDriver

    driver = BrowserDriver(user_data_dir=tmp_path / "profile", headless=True)
    try:
        await driver.start(ANCHORS)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"chromium is unavailable here: {exc}")
    try:
        yield driver
    finally:
        await driver.close()


@pytest.mark.browser
class TestTheDriverCanActOnARealPage:
    """Every action method, against a real DOM, one click per page load."""

    async def test_open_menu_clicks_the_trigger(self, started):
        await started._page.goto(interactive("wizard.html"))
        await started.open_menu()
        assert await clicked_to(started, "#menu-open"), started._page.url

    async def test_pick_menu_item_clicks_the_item_labelled_report(self, started):
        # The ordering trap this pins: `report_dialog.menu_item` is a text anchor
        # ("Report"), so `pick_menu_item` must match by *exact* label. It clicked
        # selectors, of which the anchor has none, so it raised on every run.
        await started._page.goto(interactive("wizard.html"))
        await started.pick_menu_item()
        assert await clicked_to(started, "#report-item"), started._page.url

    async def test_the_menu_click_is_exact_and_never_matches_a_paragraph(
        self, started
    ):
        # The read of this anchor is deliberately loose: "Report" is a substring
        # of "Why are you reporting this account?" and of the confirmation text,
        # which is documented and pinned in test_anchor_drift.py. A *click* must
        # not be loose -- matching that paragraph would click the wrong node. So
        # on a page where the anchor reads as a hit but no element carries the
        # exact label, the click must refuse rather than fire.
        await started._page.goto(golden("report_dialog.html"))
        with pytest.raises(SelectorDrift):
            await started.pick_menu_item()

    async def test_choose_category_clicks_the_row_whose_text_matches(self, started):
        # The oracle, not a smoke test: three menuitem rows are present, and the
        # target is the *last* one. Clicking by index (0 or 1) lands on a
        # different fragment and fails -- which is exactly the "filed under the
        # wrong category" failure choose_category exists to prevent.
        await started._page.goto(interactive("wizard.html"))
        await started.choose_category("Impersonation")
        assert await clicked_to(started, "#cat-impersonation"), started._page.url

    async def test_choose_category_for_an_absent_row_raises_rather_than_guessing(
        self, started
    ):
        await started._page.goto(interactive("wizard.html"))
        with pytest.raises(SelectorDrift):
            await started.choose_category("No Such Category")

    async def test_click_submit_clicks_the_submit_affordance(self, started):
        await started._page.goto(interactive("wizard.html"))
        await started.click_submit()
        assert await clicked_to(started, "#submitted"), started._page.url

    async def test_offered_categories_are_read_from_the_page(self, started):
        await started._page.goto(interactive("wizard.html"))
        assert await started.offered_categories() == (
            "Report",
            "Spam",
            "Impersonation",
        )

    async def test_selected_category_is_none_when_nothing_is_marked(self, started):
        await started._page.goto(interactive("wizard.html"))
        assert await started.selected_category() is None


@pytest.mark.browser
class TestAnActionOnAPageWithoutTheAffordanceRefuses:
    """A click that finds nothing must name the drift, never click 'something'."""

    async def test_absent_trigger_and_submit_raise_selector_drift(self, started):
        await started._page.goto(golden("report_confirmation.html"))
        with pytest.raises(SelectorDrift):
            await started.open_menu()
        with pytest.raises(SelectorDrift):
            await started.click_submit()


@pytest.mark.browser
class TestTheDriverNavigatesWhenToldTo:
    async def test_goto_profile_reaches_the_configured_url(self, started):
        # ``profile_url`` is the fixture seam: without it the driver navigates to
        # the live profile, which an offline test must never do. Setting it makes
        # the navigation observable without leaving the machine.
        started.profile_url = interactive("wizard.html")
        await started.goto_profile("some.one")
        assert started._page.url.endswith("wizard.html")


@pytest.mark.browser
class TestAnUnstartedDriverRefusesTheActionsToo:
    """The action methods, like the readers, must not raise AttributeError.

    ``test_browser_channel.py`` covers the readers against an unstarted driver.
    The actions are the other half and were reached by nothing; a bare
    ``AttributeError`` from any of them is graded CHANNEL_FAILED, which reads to
    an operator as "Instagram rejected the session".
    """

    @pytest.fixture
    def unstarted(self, tmp_path):
        from insta_report.browser import BrowserDriver

        return BrowserDriver(user_data_dir=tmp_path / "profile", headless=True)

    @pytest.mark.parametrize("method", ["open_menu", "pick_menu_item", "click_submit"])
    async def test_the_unstarted_action_raises_the_typed_error(self, unstarted, method):
        with pytest.raises(ChannelFailError):
            await getattr(unstarted, method)()

    async def test_the_unstarted_action_never_touches_a_page(self, unstarted):
        assert unstarted._page is None
        with pytest.raises(ChannelFailError):
            await unstarted.pick_menu_item()
