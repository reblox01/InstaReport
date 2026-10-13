"""Bind the browser's clock to the address it egresses from.

``[browser] timezone`` omitted is documented (config.py, config.example.toml)
as "derive from the egress IP's geolocation". Until this module existed that
promise was empty: the driver passed ``timezone_id=None`` to Playwright, which
means *the machine's* zone -- so a Frankfurt exit browsing under an
America/New_York clock, the single loudest cross-signal a page can read,
because ``Intl.DateTimeFormat().resolvedOptions().timeZone`` is available to
any script on the page and needs no permission.

The lease already knows the country: the pool probed the exit and recorded
where it egresses from. This module turns that country into the zone the
browser should launch with.

Scope, deliberately narrow:

* **Timezone only, not locale.** The wizard's anchors are exact English text
  (``get_by_text(..., exact=True)``); flipping the locale per egress invites
  Instagram to serve the menu in another language, and a channel that cannot
  read its own selectors is worse than a language/geo mismatch nobody grades
  as hard as the clock. ``[browser] locale`` stays the operator's setting.
* **A known country wins; anything else falls back to the configured
  default.** No guessing: a made-up zone for a country the probe only
  half-identified claims precision the probe did not deliver, and the
  configured default is at least an *intentional* one.
"""

from __future__ import annotations

__all__ = ["COUNTRY_TIMEZONES", "resolve_timezone"]


def _zones(mapping: dict[str, str]) -> dict[str, str]:
    """Identity with a tripwire: every key is upper-case alpha-2 and every
    value is a zone name zoneinfo can actually load, checked at import by
    tests, so a typo fails the suite rather than silently falling back to the
    operator's machine zone at runtime.

    The check lives in tests rather than here because zoneinfo needs a tz
    database (tzdata on Windows) and a library import must not depend on one
    -- but the tests must, which is why tzdata is a declared dev dependency
    rather than an optional skip.
    """
    return mapping


#: ISO-3166 alpha-2 country -> IANA timezone. One representative zone per
#: country. Countries that span zones get their most populous/commercial one
#: (marked), which is right far more often than it is wrong and -- crucially
#: -- is *stable*: the same country always resolves to the same zone here, so
#: a sticky exit never sees its clock move between runs.
COUNTRY_TIMEZONES: dict[str, str] = _zones(
    {
        # North America
        "US": "America/New_York",  # spans zones; eastern is the most populous
        "CA": "America/Toronto",  # spans zones; eastern is the most populous
        "MX": "America/Mexico_City",
        # South America
        "BR": "America/Sao_Paulo",  # spans zones; southeastern is the most populous
        "AR": "America/Argentina/Buenos_Aires",
        "CL": "America/Santiago",
        "CO": "America/Bogota",
        "PE": "America/Lima",
        "EC": "America/Guayaquil",
        "UY": "America/Montevideo",
        "VE": "America/Caracas",
        # Western & Northern Europe
        "GB": "Europe/London",
        "IE": "Europe/Dublin",
        "FR": "Europe/Paris",
        "DE": "Europe/Berlin",
        "NL": "Europe/Amsterdam",
        "BE": "Europe/Brussels",
        "LU": "Europe/Luxembourg",
        "CH": "Europe/Zurich",
        "AT": "Europe/Vienna",
        "IT": "Europe/Rome",
        "ES": "Europe/Madrid",
        "PT": "Europe/Lisbon",
        "SE": "Europe/Stockholm",
        "NO": "Europe/Oslo",
        "DK": "Europe/Copenhagen",
        "FI": "Europe/Helsinki",
        "IS": "Atlantic/Reykjavik",
        # Central & Eastern Europe
        "PL": "Europe/Warsaw",
        "CZ": "Europe/Prague",
        "SK": "Europe/Bratislava",
        "HU": "Europe/Budapest",
        "RO": "Europe/Bucharest",
        "BG": "Europe/Sofia",
        "GR": "Europe/Athens",
        "HR": "Europe/Zagreb",
        "RS": "Europe/Belgrade",
        "SI": "Europe/Ljubljana",
        "BA": "Europe/Sarajevo",
        "MK": "Europe/Skopje",
        "ME": "Europe/Podgorica",
        "UA": "Europe/Kyiv",
        "BY": "Europe/Minsk",
        "MD": "Europe/Chisinau",
        "LT": "Europe/Vilnius",
        "LV": "Europe/Riga",
        "EE": "Europe/Tallinn",
        "RU": "Europe/Moscow",  # spans eleven zones; Moscow is the proxy-market default
        "TR": "Europe/Istanbul",
        # Middle East & Africa
        "IL": "Asia/Jerusalem",
        "AE": "Asia/Dubai",
        "SA": "Asia/Riyadh",
        "QA": "Asia/Qatar",
        "KW": "Asia/Kuwait",
        "BH": "Asia/Bahrain",
        "OM": "Asia/Muscat",
        "JO": "Asia/Amman",
        "LB": "Asia/Beirut",
        "IQ": "Asia/Baghdad",
        "IR": "Asia/Tehran",
        "EG": "Africa/Cairo",
        "ZA": "Africa/Johannesburg",
        "NG": "Africa/Lagos",
        "KE": "Africa/Nairobi",
        "GH": "Africa/Accra",
        "MA": "Africa/Casablanca",
        # South & Central Asia
        "IN": "Asia/Kolkata",
        "PK": "Asia/Karachi",
        "BD": "Asia/Dhaka",
        "LK": "Asia/Colombo",
        "NP": "Asia/Kathmandu",
        "KZ": "Asia/Almaty",  # spans zones; Almaty is the proxy-market default
        # East & Southeast Asia
        "CN": "Asia/Shanghai",  # spans zones; eastern is the most populous
        "TW": "Asia/Taipei",
        "HK": "Asia/Hong_Kong",
        "MO": "Asia/Macau",
        "JP": "Asia/Tokyo",
        "KR": "Asia/Seoul",
        "SG": "Asia/Singapore",
        "MY": "Asia/Kuala_Lumpur",
        "TH": "Asia/Bangkok",
        "VN": "Asia/Ho_Chi_Minh",
        "PH": "Asia/Manila",
        "ID": "Asia/Jakarta",  # spans zones; western is the most populous
        "MM": "Asia/Yangon",
        "KH": "Asia/Phnom_Penh",
        "LA": "Asia/Vientiane",
        "BN": "Asia/Brunei",
        # Oceania
        "AU": "Australia/Sydney",  # spans zones; southeastern is the most populous
        "NZ": "Pacific/Auckland",
    }
)


def resolve_timezone(country: str | None, *, default: str | None) -> str | None:
    """The timezone to launch the browser with for this egress.

    A country the map knows wins -- that is the whole point. Anything else
    (no lease, no probe, a country not in the map) falls back to *default*,
    the configured ``[browser] timezone`` or None for the machine's own zone.
    """
    if country:
        zone = COUNTRY_TIMEZONES.get(country.strip().upper())
        if zone:
            return zone
    return default
