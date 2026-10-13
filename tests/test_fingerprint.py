"""The egress country -> browser clock binding.

Two different questions, and this file keeps them apart on purpose. The first
is behaviour: a country the map knows resolves to that country's zone, and
anything else falls back instead of guessing. The second is the map's own
integrity: every key a real ISO alpha-2 code, every value a zone a tz database
can actually load.

The second question is the one a typo answers, and a typo here is silent at
runtime -- resolve_timezone falls back to the operator's machine zone and the
mismatch this module exists to prevent walks straight back in, with no error
anywhere. So the table is validated entry by entry rather than trusted, and
the validation does not skip: on a platform without a tz database (Windows
without the declared tzdata dev dependency) a failed validation is the correct
answer, not an absence of one.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from insta_report.fingerprint import COUNTRY_TIMEZONES, resolve_timezone

ISO_ALPHA2 = re.compile(r"^[A-Z]{2}$")


class TestBehaviour:
    def test_a_known_country_resolves_to_its_own_zone(self):
        assert resolve_timezone("DE", default=None) == "Europe/Berlin"
        assert resolve_timezone("US", default=None) == "America/New_York"
        assert resolve_timezone("JP", default=None) == "Asia/Tokyo"

    def test_country_codes_normalize_before_lookup(self):
        """Probes report ISO codes; what case they arrive in is not a contract.

        Padded and lower-case input resolving identically is what keeps the
        lookup from silently falling back -- a fallback looks *exactly* like
        success from the outside, until the page reads the clock.
        """
        assert resolve_timezone(" de ", default="X") == "Europe/Berlin"
        assert resolve_timezone("de", default="X") == "Europe/Berlin"
        assert resolve_timezone("dE", default="X") == "Europe/Berlin"

    def test_no_lease_falls_back_to_the_configured_default(self):
        assert resolve_timezone(None, default="Europe/Paris") == "Europe/Paris"

    def test_an_unknown_country_falls_back_rather_than_guessing(self):
        """No lease ever proved geography for "ZZ", so no clock is claimed.

        Inventing a plausible zone would be the worse failure: a confident
        wrong answer that ships to launch_persistent_context and reads to
        every script on the page as fact.
        """
        assert resolve_timezone("ZZ", default="Europe/Paris") == "Europe/Paris"

    def test_default_none_is_the_machines_own_zone(self):
        """The pre-binding behaviour, preserved as the honest floor.

        None is what the driver has always passed when nothing is known --
        Playwright then uses the machine's zone. It is a mismatch risk, not a
        lie, and it is what the operator gets for configuring neither a
        timezone nor a probed lease.
        """
        assert resolve_timezone(None, default=None) is None
        assert resolve_timezone("ZZ", default=None) is None


class TestTheMapItself:
    def test_every_mapped_zone_exists_in_the_timezone_database(self):
        """The typo check. ZoneInfo raises on an unknown name, and this loop
        is the only place that would notice -- resolve_timezone does not
        validate, it trusts, which is exactly why the trust is tested."""
        broken = {
            country: zone
            for country, zone in COUNTRY_TIMEZONES.items()
            if not _zone_loads(zone)
        }
        assert not broken, f"zone names no database will load: {broken}"

    def test_every_key_is_an_uppercase_iso_alpha2_code(self):
        bad = [key for key in COUNTRY_TIMEZONES if not ISO_ALPHA2.match(key)]
        assert not bad, f"keys that are not upper-case alpha-2: {bad}"

    def test_the_map_covers_the_proxy_market_and_then_some(self):
        """A floor, not a boast: a map too small to matter would pass every
        other test here while binding almost nobody, and the countries
        residential proxy vendors actually sell are the ones to pin."""
        for country in ("US", "DE", "NL", "GB", "FR", "CA", "RU", "BR", "IN", "JP"):
            assert country in COUNTRY_TIMEZONES, country


def _zone_loads(name: str) -> bool:
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 -- any failure to load is the finding
        return False
    return True
