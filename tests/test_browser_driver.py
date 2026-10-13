"""BrowserDriver's launch seam, with Playwright replaced by a recording fake.

Everything this layer decides happens *before* chromium would start: what goes
into ``launch_persistent_context``, and when a live context must be thrown
away and opened again because the session's egress moved underneath it. A fake
that records the launch arguments therefore tests the real decisions, not a
mock of them -- what it cannot test (whether chromium honours a proxy) is the
live bridge's job and is left to it.

The fake is installed by putting a module in ``sys.modules`` where
``playwright.async_api`` would be, because the driver imports it lazily inside
``start`` -- the one import the layering lets through, and the seam this file
stands on.
"""

from __future__ import annotations

import sys
import types

import pytest

from insta_report.browser import BrowserDriver, _RecordingDriver
from insta_report.errors import SessionExpired

ANCHORS = frozenset({"menu", "submit"})


class FakePage:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler


class FakeContext:
    def __init__(self) -> None:
        self.timeout_ms: int | None = None
        self.closed = False
        self.page = FakePage()

    def set_default_timeout(self, ms: int) -> None:
        self.timeout_ms = ms

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.closed = False

    async def new_context(self, **kwargs: object) -> FakeContext:
        self.context_options = kwargs
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, fail_on_call: int | None = None) -> None:
        #: ``(user_data_dir, kwargs)`` per persistent-context launch, in order.
        self.launches: list[tuple[str, dict]] = []
        self.contexts: list[FakeContext] = []
        #: When set, that call number (1-based) raises -- the shape a profile
        #: lock or a dead proxy takes on the way up through start().
        self.fail_on_call = fail_on_call
        self._calls = 0

    async def launch_persistent_context(
        self, user_data_dir: str, **kwargs: object
    ) -> FakeContext:
        self._calls += 1
        if self.fail_on_call == self._calls:
            raise RuntimeError("profile is locked by another browser")
        self.launches.append((user_data_dir, dict(kwargs)))
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def launch(self, **kwargs: object) -> FakeBrowser:
        raise AssertionError(
            "the driver launches a persistent context whenever it has a "
            "user_data_dir; a plain launch means the profile would be lost"
        )


class FakePlaywright:
    def __init__(self, fail_on_call: int | None = None) -> None:
        self.chromium = FakeChromium(fail_on_call=fail_on_call)
        self.browser = FakeBrowser()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _AsyncPlaywright:
    """The object ``async_playwright()`` returns: one ``start()`` away."""

    def __init__(self, instance: FakePlaywright) -> None:
        self._instance = instance

    async def start(self) -> FakePlaywright:
        return self._instance


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> FakePlaywright:
    instance = FakePlaywright()
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: _AsyncPlaywright(instance)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    return instance


@pytest.fixture
def fake_playwright_failing_on_second_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> FakePlaywright:
    instance = FakePlaywright(fail_on_call=2)
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: _AsyncPlaywright(instance)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    return instance


def a_driver(tmp_path, **kwargs) -> BrowserDriver:
    return BrowserDriver(
        user_data_dir=tmp_path / "profile", headless=True, **kwargs
    )


class TestWhatGetsLaunched:
    async def test_the_launch_carries_the_proxy_and_the_egress_clock(
        self, tmp_path, fake_playwright
    ):
        driver = a_driver(tmp_path)
        result = await driver.start(
            ANCHORS,
            proxy="http://user:pw@203.0.113.9:8080",
            egress_country="DE",
        )
        assert result is driver
        directory, options = fake_playwright.chromium.launches[0]
        assert directory == str(tmp_path / "profile")
        assert options["proxy"] == {
            "server": "http://203.0.113.9:8080",
            "username": "user",
            "password": "pw",
        }
        assert options["timezone_id"] == "Europe/Berlin"
        # The clock binds; the language does not. The wizard's anchors are
        # exact English text, and a menu Instagram decides to localize is a
        # channel that cannot read itself -- see fingerprint.py.
        assert options["locale"] == "en-US"
        assert options["viewport"] == {"width": 1280, "height": 900}

    async def test_a_direct_session_launches_with_no_proxy_key(
        self, tmp_path, fake_playwright
    ):
        """Absent, not None: with nothing leased there is no egress to claim,
        and the option that would claim one is simply not offered."""
        driver = a_driver(tmp_path)
        await driver.start(ANCHORS)
        _, options = fake_playwright.chromium.launches[0]
        assert "proxy" not in options
        # No lease, no country, no configured zone: the machine's own, which
        # is what the driver has always done and what [browser] timezone
        # exists to override.
        assert options["timezone_id"] is None

    async def test_an_unknown_egress_country_keeps_the_configured_zone(
        self, tmp_path, fake_playwright
    ):
        driver = a_driver(tmp_path, timezone_id="Europe/Paris")
        await driver.start(ANCHORS, proxy="http://203.0.113.9:8080", egress_country="ZZ")
        _, options = fake_playwright.chromium.launches[0]
        assert options["timezone_id"] == "Europe/Paris"

    async def test_a_known_country_wins_over_the_configured_zone(
        self, tmp_path, fake_playwright
    ):
        """The documented rule: omitted timezone means derived, so the
        operator's leftover zone does not survive a known egress."""
        driver = a_driver(tmp_path, timezone_id="America/New_York")
        await driver.start(ANCHORS, egress_country="DE")
        _, options = fake_playwright.chromium.launches[0]
        assert options["timezone_id"] == "Europe/Berlin"


class TestWhenItRelaunches:
    async def test_the_same_session_starts_once_and_reuses(
        self, tmp_path, fake_playwright
    ):
        """Idempotence for a *bound* session. The doctor and every report
        after the first arrive here, and a relaunch per call would trade the
        profile lock for a lock error -- the exact failure the old guard
        existed to prevent."""
        driver = a_driver(tmp_path)
        session = dict(proxy="http://203.0.113.9:8080", egress_country="DE")
        await driver.start(ANCHORS, **session)
        await driver.start(ANCHORS, **session)
        await driver.start(ANCHORS, **session)
        assert len(fake_playwright.chromium.launches) == 1

    async def test_a_changed_egress_relaunches_on_the_same_profile(
        self, tmp_path, fake_playwright
    ):
        """The point of the whole module: the lease moved, so the context
        must move with it -- same profile directory, new proxy, new clock.

        A persistent context fixes proxy and timezone at launch; there is no
        reconfigure. So the old context is closed (releasing the directory's
        lock) and a new one opens the same directory, which is what keeps the
        *session* continuous while the *egress* changes. The first context
        being closed is asserted rather than assumed: a leaked context holds
        the lock the relaunch needs, and the next launch fails with an error
        that reads like a corrupt profile.
        """
        driver = a_driver(tmp_path)
        await driver.start(
            ANCHORS, proxy="http://203.0.113.9:8080", egress_country="DE"
        )
        await driver.start(
            ANCHORS, proxy="http://198.51.100.7:9000", egress_country="FR"
        )
        launches = fake_playwright.chromium.launches
        assert len(launches) == 2
        first_dir, first = launches[0]
        second_dir, second = launches[1]
        assert first_dir == second_dir == str(tmp_path / "profile")
        assert first["timezone_id"] == "Europe/Berlin"
        assert second["timezone_id"] == "Europe/Paris"
        assert second["proxy"]["server"] == "http://198.51.100.7:9000"
        assert fake_playwright.chromium.contexts[0].closed, (
            "the old context must be closed before the relaunch, or it "
            "keeps holding the profile lock the new one needs"
        )
        assert not fake_playwright.chromium.contexts[1].closed

    async def test_a_country_change_alone_relaunches(self, tmp_path, fake_playwright):
        """Same proxy, different observed country -- still a new session.

        The session key is the whole triple, because the country is what the
        clock is derived from: two launches sharing a URL but disagreeing
        about where that URL egresses to are two different fingerprints, and
        the second one would otherwise inherit the first one's clock."""
        driver = a_driver(tmp_path)
        await driver.start(ANCHORS, proxy="http://203.0.113.9:8080", egress_country="DE")
        await driver.start(ANCHORS, proxy="http://203.0.113.9:8080", egress_country="NL")
        assert len(fake_playwright.chromium.launches) == 2
        assert fake_playwright.chromium.launches[1][1]["timezone_id"] == (
            "Europe/Amsterdam"
        )

    async def test_a_failed_relaunch_leaves_no_stale_session_claim(
        self, tmp_path, fake_playwright_failing_on_second_launch
    ):
        """A launch that raised must not describe a context that is not there.

        The guard for reuse is the live context, not the remembered key --
        so after the second launch fails the third call launches again
        instead of "reusing" a session whose context never opened. The
        alternative is a driver that believes it is running while every page
        call hits None, which the runner grades as a failure against
        Instagram rather than against the launch.
        """
        fake = fake_playwright_failing_on_second_launch
        driver = a_driver(tmp_path)
        await driver.start(ANCHORS, proxy="http://203.0.113.9:8080", egress_country="DE")
        with pytest.raises(SessionExpired, match="could not be opened"):
            await driver.start(
                ANCHORS, proxy="http://198.51.100.7:9000", egress_country="FR"
            )
        assert driver._context is None
        # And the way out is open: a third attempt is a launch, not a reuse.
        await driver.start(
            ANCHORS, proxy="http://198.51.100.7:9000", egress_country="FR"
        )
        assert len(fake.chromium.launches) == 2  # first succeeded, second failed
        assert driver._session_key == (
            "http://198.51.100.7:9000",
            "en-US",
            "Europe/Paris",
        )

    async def test_close_then_start_is_a_fresh_launch(self, tmp_path, fake_playwright):
        driver = a_driver(tmp_path)
        await driver.start(ANCHORS, egress_country="DE")
        await driver.close()
        await driver.start(ANCHORS, egress_country="DE")
        assert len(fake_playwright.chromium.launches) == 2
        assert fake_playwright.stopped


class TestTheProxySettingsShape:
    """The URL parser, which is where a real credentials string breaks."""

    async def test_credentials_split_out_of_the_server(self):
        from insta_report.browser import playwright_proxy

        assert playwright_proxy("http://alice:s3cret@198.51.100.4:8080") == {
            "server": "http://198.51.100.4:8080",
            "username": "alice",
            "password": "s3cret",
        }

    async def test_a_user_with_no_password_yields_username_alone(self):
        from insta_report.browser import playwright_proxy

        assert playwright_proxy("http://alice@198.51.100.4:8080") == {
            "server": "http://198.51.100.4:8080",
            "username": "alice",
        }

    async def test_percent_encoded_credentials_are_decoded(self):
        """urlsplit hands back what was written; Chromium wants what was
        meant -- a password containing '@' is written encoded, and passing
        the encoding through would authenticate as the wrong user."""
        from insta_report.browser import playwright_proxy

        settings = playwright_proxy("http://alice:p%40ss@198.51.100.4:8080")
        assert settings["password"] == "p@ss"

    async def test_a_bare_address_passes_without_a_userinfo_field(self):
        from insta_report.browser import playwright_proxy

        assert playwright_proxy("http://198.51.100.4:3128") == {
            "server": "http://198.51.100.4:3128",
        }

    async def test_socks_keeps_its_scheme(self):
        from insta_report.browser import playwright_proxy

        assert playwright_proxy("socks5://198.51.100.4:1080") == {
            "server": "socks5://198.51.100.4:1080",
        }

    async def test_a_url_with_no_host_is_refused_by_name(self):
        """Not silently proxied nowhere: an unparseable lease URL is an
        error that quotes the value, before anything leaves the machine."""
        from insta_report.browser import playwright_proxy

        with pytest.raises(ValueError, match="has no host"):
            playwright_proxy("http://:8080")

    async def test_none_means_direct(self):
        from insta_report.browser import playwright_proxy

        assert playwright_proxy(None) is None


class TestTheRecordingWrapper:
    async def test_it_forwards_the_egress_to_what_it_wraps(self):
        """The rehearsal wraps the driver in _RecordingDriver, so a wrapper
        that dropped the egress kwargs would rehearse direct while the run
        goes out proxied -- a green rehearsal for a session shape the run
        never uses."""
        seen: dict[str, object] = {}

        class Inner:
            async def start(self, anchors, *, proxy=None, egress_country=None):
                seen["proxy"] = proxy
                seen["egress_country"] = egress_country
                return self

        recorder = _RecordingDriver(Inner())
        result = await recorder.start(
            ANCHORS, proxy="http://203.0.113.9:8080", egress_country="DE"
        )
        assert result is recorder.inner
        assert seen == {"proxy": "http://203.0.113.9:8080", "egress_country": "DE"}
