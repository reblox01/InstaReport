"""Proxy pool.

The rule that shapes this module is that **a proxy failure does not look like a
proxy failure**. A blocked or expired residential address typically answers
``HTTP 200`` with an HTML interstitial -- a provider's block page, a captive
portal, a Cloudflare challenge. Every status-code health check in existence
passes that. The original tool had no proxy health checking at all, and the
reference implementations checked status codes, so a pool of dead addresses
would have looked identical to a pool of working ones.

So health is decided by the **body**, not the status:

```
  status 200 + "203.0.113.7"        -> OK          the body is the answer
  status 200 + "<html>...blocked"   -> BLOCKED     the body is the problem
  status 200 + "<html>...hello"     -> INTERSTITIAL
  status 200 + ""                   -> EMPTY
  status 200 + "????"               -> UNPARSEABLE
  status 407                        -> AUTH_FAILED
  connect/read timeout              -> TIMEOUT
```

The first two rows are why the classifier exists: both are ``200``, and only the
body distinguishes an address that works from one that is a wall.

Three further properties are enforced here rather than left to the runner:

* **Egress is asserted, not assumed.** A lease records the IP the probe actually
  observed. A proxy that silently routes somewhere else is caught at bind time
  instead of after a login.
* **ASN diversity across concurrent leases.** Two leases sharing an ASN are two
  doors into the same building, and it is the single most obvious correlation
  available to a detector. If the probe cannot report an ASN this is *reported*
  rather than skipped, because silently not enforcing it reads the same as
  enforcing it.
* **Sticky TTL is bounded by the lease.** A provider rotating the IP out from
  under a live session -- mid-report, between page load and submit -- is
  indistinguishable from a hijacked session. A lease that would outlive its own
  IP binding is refused at acquire time.

Transport is injected as ``fetch(url, proxy) -> ProbeResult``. Nothing in this
module imports an HTTP client, which is why the whole of it is testable offline
with no library mocking: the tests supply a fetch function and the production
path supplies one backed by ``httpx``.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import random
import re
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence, cast
from urllib.parse import quote, urlparse

from .errors import ProxyUnavailable

__all__ = [
    "ProbeVerdict",
    "ProbeResult",
    "EgressObservation",
    "ProxyEndpoint",
    "ProxyLease",
    "ProxyHealth",
    "ProxyPool",
    "parse_proxy_file",
    "classify_body",
    "parse_ip_echo",
    "ProviderAdapter",
    "StaticProvider",
    "HttpJsonProvider",
    "build_pool",
    "make_httpx_fetch",
    "make_direct_fetch",
    "canonical_address",
]

log = logging.getLogger(__name__)


# --- first-failure backoff, matching the account pool's shape ---------------

DEFAULT_PROXY_QUARANTINE_BASE = 120.0
DEFAULT_PROXY_QUARANTINE_CAP = 1800.0

#: Never use a proxy that is this cold. A residential exit that failed once
#: thirty seconds ago is not "recovered", and retrying it is how a run ends up
#: sending three requests through the same wall.
MIN_PROXY_COOLDOWN_SECONDS = 45.0


class ProbeVerdict(Enum):
    """What a probe of one address actually proved.

    ``OK`` is the only verdict that means the address works. Everything else is
    a way of not working, and the ones above it in the source order are the ones
    a status-code check would have mistaken for success.
    """

    OK = "ok"
    #: 2xx, but the body is an HTML error page from the provider or a middlebox.
    INTERSTITIAL = "interstitial"
    #: 2xx, but the body says the request was refused.
    BLOCKED = "blocked"
    #: 2xx with nothing in it.
    EMPTY = "empty"
    #: 2xx with a body that is not an IP and not recognisably an error page.
    UNPARSEABLE = "unparseable"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    CONNECT_ERROR = "connect_error"
    STATUS_ERROR = "status_error"
    #: The address works perfectly and is the operator's own. Not a fault of the
    #: address and not transient, so it is never quarantined -- see
    #: :meth:`ProxyPool._dismiss_as_own_address`.
    SELF_EGRESS = "self_egress"

    @property
    def usable(self) -> bool:
        return self is ProbeVerdict.OK

    @property
    def looks_healthy_to_a_status_check(self) -> bool:
        """True for the verdicts a naive ``status == 200`` check would pass.

        Exposed as part of the type so the failure mode stays visible in the
        source rather than only in a comment: these are the verdicts that make
        status-only health checking wrong, and they are all 2xx.
        """
        return self in _STATUS_HEALTHY_VERDICTS


_STATUS_HEALTHY_VERDICTS = frozenset(
    {
        ProbeVerdict.INTERSTITIAL,
        ProbeVerdict.BLOCKED,
        ProbeVerdict.EMPTY,
        ProbeVerdict.UNPARSEABLE,
    }
)


#: Markers of a refusal page. A heuristic list, not an oracle -- it is here to
#: catch the common providers' wording so a block is *classified* rather than
#: filed as "unparseable" and debugged by hand. Kept lowercase and matched
#: case-insensitively; deliberately short, because a long list produces more
#: false positives than it prevents misdiagnoses.
_BLOCK_MARKERS = (
    "access denied",
    "unusual activity",
    "proxy detected",
    "your ip",
    "blocked",
    "captcha",
    "forbidden",
    "cloudflare",
    "too many requests",
    "not authorized",
    "subscription",
    "plan expired",
    "invalid credentials",
)

_HTML_HINT = re.compile(r"<\s*(?:!doctype\s+html|html|head|body)\b", re.IGNORECASE)

#: A bare IPv4 or IPv6 literal, the thing an IP-echo service returns.
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}\b", re.IGNORECASE)

#: RFC 5737 / RFC 3849 documentation ranges. A probe that returns one of these
#: has almost certainly been intercepted rather than reached the echo service.
_DOCUMENTATION_NETS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
    # RFC 2544 benchmarking. Not documentation, but the same conclusion: no
    # residential provider has an address here, so one appearing in a probe
    # means something between us and the echo service invented it. Listed here
    # rather than in a second table elsewhere, because the question "is this
    # address real and distinct" must have exactly one answer in the codebase.
    ipaddress.ip_network("198.18.0.0/15"),
)


def canonical_address(value: str) -> str | None:
    """The one form two spellings of the same address are compared in.

    Returns ``None`` for anything that is not an address. Never guesses and never
    returns a partially-parsed value, because a caller that compares a guess is
    the same failure as a caller that does not compare at all.

    Two things are collapsed here that a string comparison gets wrong:

    * **IPv4-mapped IPv6.** ``::ffff:203.0.113.7`` and ``203.0.113.7`` are one
      host, and a dual-stack egress is entitled to answer in the mapped form.
      Compared as strings they differ, so a string comparison lets the operator's
      own address through exactly when the proxy is working correctly.
    * **Presentation form.** ``2001:0DB8::1`` and ``2001:db8::1`` are one
      address; so are the many legal spellings of an IPv4 octet group. Only the
      parsed object knows that.
    """
    candidate = (value or "").strip()
    if not candidate:
        return None
    # An echo service asked for JSON sometimes answers with the address quoted or
    # wrapped in an object, and a bracketed IPv6 literal is not a bare address.
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return str(mapped)
    return str(address)


@dataclass(frozen=True)
class EgressObservation:
    """What the outside world sees when the request comes through this address."""

    ip: str
    country: str | None = None
    asn: int | None = None
    isp: str | None = None
    is_documentation_range: bool = False

    @property
    def has_asn(self) -> bool:
        return self.asn is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "country": self.country,
            "asn": self.asn,
            "isp": self.isp,
            "is_documentation_range": self.is_documentation_range,
        }


@dataclass(frozen=True)
class ProbeResult:
    """One probe's raw outcome. Transport-specific detail stays in ``detail``."""

    verdict: ProbeVerdict
    status: int | None = None
    body: str = ""
    detail: str = ""
    egress: EgressObservation | None = None
    elapsed: float = 0.0


def classify_body(
    body: str,
    status: int,
    *,
    content_type: str | None = None,
) -> tuple[ProbeVerdict, str]:
    """Decide whether a 2xx body means the address works.

    Split out from the transport so the classifier can be tested against every
    awkward response shape without a socket, and so the decision is auditable
    in one place.
    """
    if status == 407:
        return ProbeVerdict.AUTH_FAILED, "proxy rejected the supplied credentials"
    if not 200 <= status < 300:
        # Gate first, and before anything looks at the body. A 404 page from a
        # captive portal or a provider's error page routinely contains an IP
        # literal in its diagnostic text, and an address literal is this
        # function's definition of a healthy answer -- so without this gate a
        # failed request with a hostname in its body is graded as a working
        # exit. Checked before the body is even stripped, because the question
        # "did this succeed" is not the body's to answer.
        return ProbeVerdict.STATUS_ERROR, f"the probe answered with HTTP {status}"

    stripped = body.strip()
    if not stripped:
        return ProbeVerdict.EMPTY, "2xx with an empty body"

    lowered = stripped.casefold()

    if any(marker in lowered for marker in _BLOCK_MARKERS):
        # A block page is the case this function exists for. It arrives as 200.
        return ProbeVerdict.BLOCKED, "body carries a refusal marker"

    if _IPV4.search(stripped) or _looks_like_ipv6(stripped):
        return ProbeVerdict.OK, "body contains an address literal"

    if _HTML_HINT.search(stripped):
        if content_type and "text/html" in content_type.casefold():
            return ProbeVerdict.INTERSTITIAL, "2xx HTML page where an address was expected"
        return ProbeVerdict.UNPARSEABLE, "2xx HTML page, no address and no refusal marker"

    if content_type and "json" in content_type.casefold():
        return ProbeVerdict.UNPARSEABLE, "2xx JSON with no address field"

    return ProbeVerdict.UNPARSEABLE, "2xx body is neither an address nor a known page"


def _looks_like_ipv6(text: str) -> bool:
    for candidate in _IPV6.findall(text):
        try:
            ipaddress.IPv6Address(candidate)
            return True
        except ValueError:
            continue
    return False


def parse_ip_echo(payload: str) -> EgressObservation | None:
    """Extract the observed egress from an IP-echo response.

    Tries JSON first, because that is the only form that can carry an ASN or a
    country, then falls back to a bare literal. Returning ``None`` for a 2xx
    body with no address in it is the important case: it is how a block page
    arriving with ``HTTP 200`` stops being counted as a working address.
    """
    text = payload.strip()
    if not text:
        return None

    # JSON first: an ASN is worth far more than an address, and only JSON has it.
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            observation = _observation_from_mapping(data)
            if observation is not None:
                return observation

    literal = _first_ip_literal(text)
    if literal is None:
        return None
    return _build_observation(literal)


def _first_ip_literal(text: str) -> str | None:
    for match in _IPV4.findall(text):
        try:
            ipaddress.IPv4Address(match)
            return match
        except ValueError:
            continue
    for match in _IPV6.findall(text):
        try:
            return str(ipaddress.IPv6Address(match))
        except ValueError:
            continue
    return None


#: Echo services spell the ASN field differently. Listed rather than guessed,
#: because a silently missing key means ASN diversity quietly stops being
#: enforced while the operator believes it still is.
_ASN_KEYS = ("asn", "as", "autonomous_system", "asn_org", "asnumber")
_COUNTRY_KEYS = ("country", "country_code", "countryCode", "region")
_ISP_KEYS = ("isp", "org", "organization", "asn_org", "asn_owner", "company")


def _coerce_asn(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        # "AS15169", "15169", "15169 Google LLC"
        match = re.search(r"\d+", value)
        if match:
            return int(match.group())
    if isinstance(value, dict):
        # The nested shape: ipinfo and several others answer
        # ``{"asn": {"asn": "AS15169", "name": "Google LLC", ...}}``. Handled
        # here rather than in a caller because the field is called "asn" in
        # both the flat and the nested form, and a caller that reaches for
        # ``value["asn"]`` on a flat int is a TypeError at the worst moment.
        for key in ("asn", "as", "number", "autonomous_system_number"):
            if key in value:
                return _coerce_asn(value[key])
    return None


def _first_key(data: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _grade_egress(result: ProbeResult) -> ProbeResult:
    """Downgrade an ``OK`` probe whose address we cannot report from.

    Returns a new result; the input is untouched, because the verdict a
    transport produced is a fact about the transport and this is a fact about
    the pool's willingness to use the answer.

    The verdict answers "did the body read like a working answer?". Three
    cases say the body read fine and the answer is still unusable, and all
    three are why this is not simply ``verdict.usable`` at the call site:

    * **No address in it.** A pool that returned this would hand out an exit it
      has never identified, which is how two sessions silently share one IP.
    * **A reserved range.** ``198.51.100.x``, ``2001:db8::x``,
      ``198.18.0.0/15``. Proof the probe was intercepted rather than reaching
      the echo service -- a transparent proxy answering for the provider, a DNS
      hijack, a captive portal. Binding it means a report leaving from a
      fabricated address while the diversity accounting records a distinct ASN
      it never saw.
    * **Not an address at all.** ``"unknown"``, ``""``, ``"0.0.0.0"``. No
      reserved range to catch these, and every other field of the observation
      looks perfectly usable.

    Graded here rather than in :func:`~insta_report.transport.fetch` because the
    transport is injected: a pool that trusted whatever verdict it was handed
    had no defence of its own, and a second transport would reintroduce the
    hole. Downgrading to ``UNPARSEABLE`` rather than a new verdict keeps the
    enum closed -- the caller already treats it as "do not use this", and a
    distinct member would invite a fourth implementation to handle it.
    """
    if result.verdict is not ProbeVerdict.OK or result.egress is None:
        if result.verdict is ProbeVerdict.OK and result.egress is None:
            return replace(
                result,
                verdict=ProbeVerdict.UNPARSEABLE,
                detail="the probe did not identify the exit address",
            )
        return result

    ip = result.egress.ip
    if result.egress.is_documentation_range:
        return replace(
            result,
            verdict=ProbeVerdict.UNPARSEABLE,
            detail=(
                f"egress {ip} is a reserved range: the probe was intercepted "
                "rather than reaching the echo service"
            ),
        )
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return replace(
            result,
            verdict=ProbeVerdict.UNPARSEABLE,
            detail=f"egress {ip!r} is not an address at all",
        )
    if address.is_unspecified or address.is_loopback or address.is_link_local:
        # ``0.0.0.0``, ``127.0.0.1``, ``::1``, ``fe80::``. Valid addresses,
        # so ``ip_address`` accepts them, and ``is_documentation_range`` is
        # False for all of them -- but no residential provider has an exit on
        # the loopback interface or at the unspecified address. A provider
        # answering with one is telling us it never actually connected.
        #
        # Deliberately the narrow list. ``is_private`` is *not* refused:
        # RFC 6598 carrier-grade NAT (100.64.0.0/10) is ``is_private`` in
        # Python and is exactly what a residential mobile exit looks like, so
        # refusing it would reject the addresses this tool exists to use.
        return replace(
            result,
            verdict=ProbeVerdict.UNPARSEABLE,
            detail=f"egress {ip} is not a routable address for a remote peer",
        )
    return result


def _observation_from_mapping(data: dict[str, Any]) -> EgressObservation | None:
    ip_value = _first_key(data, ("ip", "query", "ipAddress", "ip_address", "origin", "client_ip"))
    if ip_value is None:
        return None
    literal = str(ip_value).split(",")[0].strip()
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return None

    country = _first_key(data, _COUNTRY_KEYS)
    asn = _coerce_asn(_first_key(data, _ASN_KEYS))
    isp = _first_key(data, _ISP_KEYS)
    return _build_observation(
        literal,
        country=str(country) if country is not None else None,
        asn=asn,
        isp=str(isp) if isp is not None else None,
    )


def _build_observation(
    ip: str,
    *,
    country: str | None = None,
    asn: int | None = None,
    isp: str | None = None,
) -> EgressObservation:
    try:
        address = ipaddress.ip_address(ip)
        documented = any(address in net for net in _DOCUMENTATION_NETS)
    except ValueError:
        documented = False
    return EgressObservation(
        ip=ip, country=country, asn=asn, isp=isp, is_documentation_range=documented
    )


# --- endpoints --------------------------------------------------------------


@dataclass(frozen=True)
class ProxyEndpoint:
    """One address the pool may bind to.

    Frozen for the same reason :class:`~insta_report.accounts.Lease` is: a
    mutable endpoint would let a rebound lease mutate the record of the address
    the previous reports actually went out on.
    """

    url: str
    source: str = "file"
    label: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("proxy url may not be empty")
        if "://" not in self.url:
            raise ValueError(
                f"proxy url {self.url!r} has no scheme; "
                "write http://user:pass@host:port, not host:port"
            )

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or "?"

    @property
    def port(self) -> int | None:
        return urlparse(self.url).port

    @property
    def has_credentials(self) -> bool:
        return bool(urlparse(self.url).username)

    @property
    def origin(self) -> str:
        """Host:port with credentials removed. Safe to log."""
        parsed = urlparse(self.url)
        return f"{parsed.hostname}:{parsed.port}"

    def redacted(self) -> str:
        """Loggable form. Credentials never survive this."""
        return f"{self.origin} ({self.label or self.source})"

    def __repr__(self) -> str:
        return f"ProxyEndpoint({self.origin!r}, source={self.source!r})"


#: A comment marker, but only at the *start* of a line. Anchoring matters: an
#: unanchored ``//`` also matches the separator in ``socks5://host:port``, which
#: silently reduced every scheme-qualified address to the empty string. That is
#: the sort of bug that looks like "the file format is odd" rather than a parser
#: fault, so it is worth the explicit pattern.
_COMMENT = re.compile(r"^\s*(?:#|//).*$")


def parse_proxy_file(
    path: Path,
    *,
    scheme: str = "http",
    label: str = "",
    problems: list[str] | None = None,
) -> list[ProxyEndpoint]:
    """Read an operator proxy list, tolerantly and without ever logging secrets.

    Accepts the four shapes these lists are written in -- ``host:port``,
    ``user:pass@host:port``, ``socks5://...``, and fully-formed URLs -- plus
    ``#`` and ``//`` comments, blank lines and Windows line endings. Shape
    variety is the norm; a strict parser would reject an operator's working
    list for a reason that has nothing to do with whether the addresses work.

    Duplicates are removed, preserving order, because a list with a repeated
    address would make the pool's diversity accounting think it has more
    capacity than it does.

    ``problems`` collects one message per line that had to be skipped. Passed
    in rather than returned, so this stays a plain list: the callers that care
    about skips already have a list to append to, and the ones that do not
    should not have to unpack a tuple to get the endpoints. A skipped line is
    not a small thing to lose -- an operator who wrote twenty addresses and
    gets fifteen back has fifteen exits where they believed they had twenty,
    and the only way to find out is to be told.
    """
    text = path.read_text(encoding="utf-8", errors="replace")

    endpoints: list[ProxyEndpoint] = []
    seen: set[str] = set()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _COMMENT.sub("", raw).strip()
        if not line:
            continue
        url = _normalise_line(line, scheme=scheme)
        if url is None:
            message = f"{path}:{lineno}: skipped, not a proxy address"
            if problems is not None:
                problems.append(message)
            log.warning("%s", message)
            continue
        key = _dedupe_key(url)
        if key in seen:
            continue
        seen.add(key)
        endpoints.append(ProxyEndpoint(url=url, source="file", label=label or path.stem))

    return endpoints


def _normalise_line(line: str, *, scheme: str) -> str | None:
    """One proxy line to a URL, or ``None`` if it cannot be one.

    Nothing here raises. Every rejection is a line in an operator's file, and
    a list with one bad line must not take the other nineteen down with it.
    """
    if "://" in line:
        candidate = line
    elif "@" in line:
        # user:pass@host:port -- credentials must be percent-encoded or the
        # '@' in a password would make urlparse read the wrong host.
        creds, _, hostpart = line.rpartition("@")
        user, _, password = creds.partition(":")
        candidate = f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{hostpart}"
    elif ":" in line:
        candidate = f"{scheme}://{line}"
    else:
        return None

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    # ``.port`` is a property that *raises* on a non-numeric port rather than
    # returning None, so it cannot be read inside the try above -- and a
    # malformed port is the most likely way an operator's line is wrong.
    # Guarded on its own, because without it a line like ``http://host:9:1``
    # takes the whole file down with a traceback from urllib that names no
    # line and no file.
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        return None
    return candidate


def _dedupe_key(url: str) -> str:
    """Dedupe on origin + credentials, ignoring incidental spelling.

    Total by construction -- it is only called on URLs
    :func:`_normalise_line` already accepted -- and defensive anyway, because
    a crash inside the function that exists to stop duplicates turning into
    capacity is a bad trade.
    """
    parsed = urlparse(url)
    creds = ""
    if parsed.username:
        creds = f"{parsed.username}:{parsed.password or ''}"
    try:
        port = parsed.port
    except ValueError:
        return url
    return f"{parsed.scheme}://{creds}@{parsed.hostname}:{port}"


# --- providers --------------------------------------------------------------


class ProviderAdapter(Protocol):
    """Where addresses come from.

    A protocol rather than an abstract base class so an operator can pass a
    lambda in a test or a script without declaring a subclass. Implementations
    must return a *fresh* batch: the pool assumes a provider can hand out
    distinct sticky addresses on request, and an adapter that returns the same
    list twice breaks ASN diversity silently.
    """

    def fetch(self, count: int) -> Sequence[ProxyEndpoint]:
        """Return up to *count* endpoints, each with its own sticky binding."""
        ...


class StaticProvider:
    """A provider adapter over a list the operator already has.

    Also the escape hatch for any real provider: fetch the addresses however the
    vendor documents it -- their CLI, their SDK, curl -- write them to a file,
    and point ``source = "file"`` at it. That keeps vendor API churn out of the
    codebase entirely, which matters because those APIs change on the vendor's
    schedule rather than ours.
    """

    def __init__(self, endpoints: Iterable[ProxyEndpoint]) -> None:
        self._endpoints = tuple(endpoints)

    def fetch(self, count: int) -> Sequence[ProxyEndpoint]:
        return self._endpoints[:count]

    def __len__(self) -> int:
        return len(self._endpoints)


class HttpJsonProvider:
    """A provider that answers a GET with a JSON list of addresses.

    The response shape is operator-configured rather than assumed, because no
    two residential vendors agree on one and guessing would bake a single
    vendor's field names into the tool. ``extract`` receives the parsed JSON and
    returns the address strings; the two functions below cover the common
    shapes and can be replaced.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        extract: Callable[[Any], Sequence[str]] | None = None,
        timeout: float = 20.0,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.query = dict(query or {})
        self.timeout = timeout
        self._extract = extract or extract_common_shapes
        self._transport = transport

    def fetch(self, count: int) -> Sequence[ProxyEndpoint]:
        if self._transport is None:
            raise ProxyUnavailable(
                "HttpJsonProvider was constructed without a transport; "
                "supply one or use StaticProvider with a pre-fetched list"
            )
        payload = self._transport(
            self.url, headers=self.headers, params=self.query, timeout=self.timeout
        )
        addresses = self._extract(payload)
        return [
            ProxyEndpoint(url=a if "://" in a else f"http://{a}", source="provider", label="provider")
            for a in addresses[:count]
        ]


def extract_common_shapes(payload: Any) -> list[str]:
    """Pull address strings out of the two response shapes providers use."""
    if isinstance(payload, list):
        return [str(item) for item in payload if isinstance(item, (str, int))]
    if isinstance(payload, dict):
        for key in ("proxies", "data", "results", "items", "endpoints"):
            value = payload.get(key)
            if isinstance(value, list):
                return _addresses_from_records(value)
    return []


def _addresses_from_records(records: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for record in records:
        if isinstance(record, str):
            out.append(record)
            continue
        if not isinstance(record, dict):
            continue
        host = _first_key(record, ("host", "ip", "ip_address", "server"))
        port = _first_key(record, ("port", "proxy_port"))
        if host is None or port is None:
            continue
        user = _first_key(record, ("username", "user", "login"))
        password = _first_key(record, ("password", "pass"))
        auth = f"{quote(str(user), safe='')}:{quote(str(password), safe='')}@" if user else ""
        out.append(f"http://{auth}{host}:{port}")
    return out


# --- health -----------------------------------------------------------------


@dataclass
class ProxyHealth:
    """Per-address history. What decides the order addresses are tried in."""

    endpoint: ProxyEndpoint
    consecutive_failures: int = 0
    successes: int = 0
    failures: int = 0
    last_verdict: ProbeVerdict | None = None
    last_detail: str = ""
    last_used_monotonic: float | None = None
    quarantined_until: float | None = None
    egress: EgressObservation | None = None
    ever_bound: bool = False

    def available(self, now: float, *, min_cooldown: float = MIN_PROXY_COOLDOWN_SECONDS) -> bool:
        if self.quarantined_until is not None and self.quarantined_until > now:
            return False
        if self.last_used_monotonic is not None:
            if now - self.last_used_monotonic < min_cooldown:
                return False
        return True

    def record_ok(self, now: float, egress: EgressObservation | None) -> None:
        self.consecutive_failures = 0
        self.successes += 1
        self.last_verdict = ProbeVerdict.OK
        self.last_detail = ""
        self.quarantined_until = None
        self.last_used_monotonic = now
        if egress is not None:
            self.egress = egress
        self.ever_bound = True

    def record_failure(
        self,
        verdict: ProbeVerdict,
        detail: str,
        now: float,
        *,
        base: float = DEFAULT_PROXY_QUARANTINE_BASE,
        cap: float = DEFAULT_PROXY_QUARANTINE_CAP,
    ) -> float:
        self.consecutive_failures += 1
        self.failures += 1
        self.last_verdict = verdict
        self.last_detail = detail
        self.last_used_monotonic = now
        wait = min(cap, base * (2 ** (self.consecutive_failures - 1)))
        self.quarantined_until = now + wait
        log.warning(
            "proxy %s failed (%s: %s); cooling down %.0fs",
            self.endpoint.redacted(),
            verdict.value,
            detail,
            wait,
        )
        return wait

    @property
    def health_score(self) -> float:
        """Higher is better. A clean address outranks a merely-less-bad one.

        Successes contribute more than failures cost, so an address with a
        long history of success is not discarded by one transient blip -- the
        quarantine window is what handles transience, and a single failure
        should not re-rank a proven address behind an untried one.
        """
        return self.successes - (self.consecutive_failures * 1.5)

    def status(self, now: float) -> dict[str, Any]:
        return {
            "origin": self.endpoint.origin,
            "label": self.endpoint.label,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_verdict": self.last_verdict.value if self.last_verdict else None,
            "last_detail": self.last_detail,
            "cooldown_remaining": round(
                max(0.0, (self.quarantined_until or now) - now), 1
            ),
            "available": self.available(now),
            "egress": self.egress.as_dict() if self.egress else None,
        }


# --- leases -----------------------------------------------------------------


@dataclass(frozen=True)
class ProxyLease:
    """An address bound to an account, valid for a bounded time.

    ``sticky_expires_at`` is the reason this is a lease and not a string. A
    provider whose sticky TTL is shorter than the session rotates the exit
    address partway through a report, and a report that starts on one IP and
    submits from another is the shape of a stolen session. Carrying the
    deadline on the lease makes that checkable by whoever holds it.
    """

    lease_id: str
    endpoint: ProxyEndpoint
    sticky_expires_at: float
    acquired_at: float
    egress: EgressObservation | None = None

    def expired(self, now: float) -> bool:
        return now >= self.sticky_expires_at

    def remaining(self, now: float) -> float:
        return max(0.0, self.sticky_expires_at - now)

    def assert_fresh(self, now: float) -> None:
        """Raise rather than let a report go out on a rotated address."""
        if self.expired(now):
            raise ProxyUnavailable(
                f"proxy lease {self.lease_id} on {self.endpoint.origin} expired "
                f"{now - self.sticky_expires_at:.0f}s ago. The provider has "
                "almost certainly changed the exit address, so submitting now "
                "would send the report from a different IP than the session "
                "was bound to. Rebind instead."
            )

    def redacted(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "proxy": self.endpoint.origin,
            "label": self.endpoint.label,
            "sticky_expires_at": round(self.sticky_expires_at, 1),
            "egress": self.egress.as_dict() if self.egress else None,
        }


#: Transport: given a URL and an optional proxy url, probe it.
Fetch = Callable[[str, str | None], ProbeResult]


class ProxyPool:
    """Orders, binds and retires addresses.

    Ordering is health-then-rotation, so a healthy address is reused until it
    fails or its cooldown expires rather than round-robining across the whole
    list. Rotating needlessly means a fresh session validation and a fresh login
    risk per report, which is the cost the lease design exists to avoid.
    """

    def __init__(
        self,
        endpoints: Iterable[ProxyEndpoint] | ProviderAdapter,
        *,
        fetch: Fetch,
        monotonic: Callable[[], float] = time.monotonic,
        sticky_ttl: float = 4 * 3600.0,
        probe_url: str = "https://api.ipify.org?format=json",
        min_cooldown: float = MIN_PROXY_COOLDOWN_SECONDS,
        quarantine_base: float = DEFAULT_PROXY_QUARANTINE_BASE,
        quarantine_cap: float = DEFAULT_PROXY_QUARANTINE_CAP,
        rng: random.Random | None = None,
        enforce_asn_diversity: bool = True,
        own_ip: str | None = None,
    ) -> None:
        self._fetch = fetch
        self._monotonic = monotonic
        self._sticky_ttl = sticky_ttl
        self._probe_url = probe_url
        self._min_cooldown = min_cooldown
        self._quarantine_base = quarantine_base
        self._quarantine_cap = quarantine_cap
        self._rng = rng or random.Random()
        self._enforce_asn_diversity = enforce_asn_diversity
        self._sequence = 0
        #: The operator's own address, exactly as supplied, kept so the two ways
        #: of not having one -- absent, and present but unparseable -- can be
        #: reported differently. They are different mistakes: the first is an
        #: unfinished install, the second is a typo that looks finished.
        self._own_raw = own_ip
        #: The comparable form, or ``None`` when absent or unparseable.
        self._own = canonical_address(own_ip) if own_ip else None
        #: Origins proven to egress from the operator's own address, mapped to
        #: the address they egressed from. Dismissed rather than quarantined;
        #: see :meth:`_dismiss_as_own_address`. The address is kept because the
        #: report has to be able to say *which* one it was.
        self._self_egress: dict[str, str] = {}
        #: Stable per-address tiebreak, drawn once. See ``_order_key``.
        self._tiebreak: dict[str, float] = {}

        self._health: dict[str, ProxyHealth] = {}
        self._by_origin: dict[str, str] = {}
        self._leases: dict[str, ProxyLease] = {}
        #: Declared rather than inferred. The first assignment is inside a
        #: branch, so mypy infers the type from that branch alone and then
        #: rejects the ``None`` below -- which is the inference being wrong
        #: about a value that is genuinely None for every operator-proxy pool.
        self._provider: ProviderAdapter | None

        # The two shapes a caller can pass -- a live provider, or a plain list
        # of addresses -- are told apart structurally, because both are
        # legitimate and there is no base class to inherit. ``hasattr`` is a
        # real runtime narrowing that a type checker cannot follow, so the
        # narrowing is restated here as two casts rather than scattered as three
        # ``type: ignore`` comments that suppress whatever the line happens to
        # be doing that week.
        # The two shapes a caller can pass -- a live provider, or a plain list
        # of addresses -- are told apart structurally, because both are
        # legitimate and there is no base class to inherit.
        #
        # The one cast below is load-bearing and the two ignores it replaces
        # were not. ``hasattr`` narrows the expression for member access -- so
        # ``provider.fetch(64)`` and ``for endpoint in endpoints`` both check
        # without help -- but it does not narrow an *assignment* to a variable
        # that already has a declared type, which is why storing it needs
        # saying out loud. Under ``warn_unused_ignores`` the two ignores this
        # replaced were reported as suppressing nothing, which is the check
        # earning its place in the config: a suppression that no longer
        # suppresses anything is either dead code or a leftover from a fix
        # nobody reverted.
        if hasattr(endpoints, "fetch"):
            provider = cast("ProviderAdapter", endpoints)
            self._provider = provider
            for endpoint in provider.fetch(64):
                self._register(endpoint)
        else:
            self._provider = None
            for endpoint in endpoints:
                self._register(endpoint)

        if not self._health:
            log.warning("proxy pool constructed with no addresses")

    def _register(self, endpoint: ProxyEndpoint) -> None:
        key = self._key(endpoint)
        if key in self._health:
            return
        self._health[key] = ProxyHealth(endpoint=endpoint)
        self._by_origin[key] = endpoint.origin
        self._tiebreak[endpoint.origin] = self._rng.random()

    @staticmethod
    def _key(endpoint: ProxyEndpoint) -> str:
        return _dedupe_key(endpoint.url)

    # -- inspection -----------------------------------------------------

    def __len__(self) -> int:
        return len(self._health)

    @property
    def endpoints(self) -> tuple[ProxyEndpoint, ...]:
        return tuple(h.endpoint for h in self._health.values())

    def available(self, now: float | None = None) -> list[ProxyHealth]:
        """Addresses that could be leased right now.

        Dismissed self-egress origins are excluded here rather than at each use
        site, so every consumer -- ranking, the fallback pass, and the operator
        reading ``status`` -- gets the same answer. Filtering only inside
        ``acquire`` would leave this public method reporting an address as
        available that the pool has already decided is not a proxy, which is the
        same false reading as the misleading refusal message, in a different
        place.
        """
        now = self._monotonic() if now is None else now
        return [
            h
            for h in self._health.values()
            if h.endpoint.origin not in self._self_egress
            and h.available(now, min_cooldown=self._min_cooldown)
        ]

    def status(self) -> list[dict[str, Any]]:
        now = self._monotonic()
        return [h.status(now) for h in self._health.values()]

    def active_leases(self) -> tuple[ProxyLease, ...]:
        return tuple(self._leases.values())

    def asn_diversity_report(self) -> list[dict[str, Any]]:
        """One row per ASN currently leased, so a collision is visible.

        Reported even when enforcement is off. A run with three leases on one
        ASN is not a run that has decided diversity is unimportant.
        """
        by_asn: dict[int | None, list[str]] = {}
        for lease in self._leases.values():
            asn = lease.egress.asn if lease.egress else None
            by_asn.setdefault(asn, []).append(lease.endpoint.origin)
        return [
            {
                "asn": asn,
                "origins": origins,
                "count": len(origins),
                "known": asn is not None,
            }
            for asn, origins in sorted(by_asn.items(), key=lambda kv: (kv[0] is None, kv[0]))
        ]

    # -- binding --------------------------------------------------------

    def acquire(
        self,
        *,
        hold_for: float | None = None,
        require_distinct_asn: bool | None = None,
    ) -> ProxyLease:
        """Bind the best available address and probe it before returning.

        The probe is not optional. Binding an address without proving it works
        is how a run discovers a dead exit mid-report, after the session has
        already been established on it.

        ``hold_for`` is how long the caller intends to keep the binding. If it
        exceeds the provider's sticky TTL the request is refused, because the
        address will be rotated out from under a live session.
        """
        self._require_own_address()

        if hold_for is not None and hold_for > self._sticky_ttl:
            raise ProxyUnavailable(
                f"requested hold of {hold_for / 60:.0f}min exceeds the provider's "
                f"{self._sticky_ttl / 60:.0f}min sticky TTL. The exit address "
                "would be rotated mid-session. Shorten the lease, or use a "
                "provider with a longer sticky window."
            )

        now = self._monotonic()
        candidates = self._ranked(now)
        degraded = False
        if not candidates:
            # Every address is sidelined. The cooldown after a *success* is a
            # preference, not a safety property -- its job is to avoid
            # re-validating a session per report. If it also gated availability,
            # a one-address configuration would simply never run again, and
            # refusing is a worse failure than the session re-validation it
            # prevents. So fall back to the least-bad address and say so.
            #
            # The quarantine after a *failure* is different and still respected
            # for as long as an alternative exists; it is only bypassed here,
            # with the elapsed sidelining spelled out, because "no address at
            # all" helps nobody.
            candidates = self._fallback_ranked(now)
            if not candidates:
                raise ProxyUnavailable(self._exhausted_report(now))
            degraded = True
            log.warning(
                "no proxy address is off cooldown; falling back to %s, sidelined "
                "for %.0fs after %s. Configure more addresses if this repeats.",
                candidates[0].endpoint.origin,
                max(0.0, (candidates[0].quarantined_until or now) - now),
                candidates[0].last_verdict.value if candidates[0].last_verdict else "use",
            )

        want_distinct = (
            self._enforce_asn_diversity
            if require_distinct_asn is None
            else require_distinct_asn
        )
        leased_asns = {
            lease.egress.asn for lease in self._leases.values() if lease.egress and lease.egress.asn is not None
        }

        tried = 0
        for health in candidates:
            if want_distinct and len(leased_asns) >= self._asn_capacity():
                # Every known ASN is already leased. This is a real constraint,
                # not a preference, so it is reported rather than bypassed.
                if not self._relaxable(leased_asns):
                    break
            tried += 1
            result = self._probe(health.endpoint)
            if result.verdict.usable and result.egress is not None:
                if self._is_own_address(result.egress.ip):
                    # Dismissed, not failed. This address is not broken and will
                    # not recover: it answers every probe perfectly, from the
                    # one place the operator must never be seen reporting from.
                    # Recording a health failure would quarantine it on a backoff
                    # and then let `_fallback_ranked` re-select it the moment the
                    # pool is busy, so the guard would hold exactly when it
                    # mattered least. The sibling addresses in the same file are
                    # unaffected -- one bad entry costs one entry.
                    self._dismiss_as_own_address(health.endpoint, result.egress)
                    continue
                if want_distinct and result.egress.asn is not None:
                    if result.egress.asn in leased_asns:
                        # Skip WITHOUT recording a failure. The address just
                        # answered correctly; it is merely unavailable while a
                        # sibling lease holds its ASN. Counting this against its
                        # health would quarantine a working address for a
                        # scheduling decision and, worse, permanently demote it
                        # behind addresses that have never been proven at all.
                        log.debug(
                            "skipping %s: ASN %s already leased",
                            health.endpoint.origin,
                            result.egress.asn,
                        )
                        continue
                    leased_asns.add(result.egress.asn)
                return self._bind(health, result)
            health.record_failure(
                result.verdict,
                result.detail,
                self._monotonic(),
                base=self._quarantine_base,
                cap=self._quarantine_cap,
            )

        if self._self_egress:
            # Its own headline, because the generic one is actively misleading
            # here. "no usable proxy after probing 1 address(es)" and "untried"
            # both read as a broken address, and the remedy that follows from
            # that -- buy more addresses -- does not work, because these answers
            # perfectly. They are worse than useless: they are the only
            # addresses in the pool that do work.
            raise ProxyUnavailable(
                f"refusing to lease: {len(self._self_egress)} configured "
                f"address(es) work correctly and egress from this machine's own "
                f"address, so they are not proxies. Adding more addresses will "
                f"not help. Remove them from the proxy file, or point them at a "
                f"real upstream proxy.\n"
                + self._dismissed_report()
                + "\n"
                + self._exhausted_report(self._monotonic())
            )

        raise ProxyUnavailable(
            f"no usable proxy after probing {tried} address(es); "
            f"{len(candidates)} were considered"
            + (
                " (all were on cooldown, so this was a fallback pass)"
                if degraded
                else ""
            )
            + "\n"
            + self._exhausted_report(self._monotonic())
        )

    def _dismissed_report(self) -> str:
        """Name the dismissed origins, for the refusal and for ``summary``.

        Kept separate from :meth:`_exhausted_report` because these addresses are
        not in that report at all -- they are not cooling down, not failed, not
        untried. They are a different kind of unavailable, and folding them into
        the cooldown table would restate the false reading that produced this
        fix.
        """
        lines = ["dismissed (working, but egress from this machine's own address):"]
        for origin in sorted(self._self_egress):
            lines.append(f"  {origin}: {self._self_egress[origin]}")
        return "\n".join(lines)

    def _require_own_address(self) -> None:
        """Refuse to lease anything until the operator's own address is known.

        The whole point of the guard is that a report never leaves from the
        address the operator browses from. That cannot be checked without knowing
        the operator's address, and "we could not find out" is not the same as
        "there is nothing to compare" -- reading it that way produces a guard
        that reports healthy while comparing nothing, which is the failure mode
        this project has now paid for twice.

        So this fails closed, in two distinguishable messages, because the two
        ways of arriving here have different fixes.
        """
        if self._own is not None:
            return
        if self._own_raw is None:
            raise ProxyUnavailable(
                "refusing to lease a proxy: the operator's own IP address was "
                "never established, so no lease can be checked against it. This "
                "is the one correlation the tool exists to avoid -- a report sent "
                "from the operator's own address identifies the reporting "
                "account. Either set own_ip in [proxies], or let the pool observe "
                "it at startup (build_pool does this with a direct, non-proxied "
                "request)."
            )
        raise ProxyUnavailable(
            f"refusing to lease a proxy: own_ip={self._own_raw!r} is not a valid "
            "IP address, so it cannot be compared against anything. A typo here "
            "disables the check that keeps reports off the operator's own "
            "address, while still looking configured. Fix it, or remove the key "
            "to have the pool observe the address itself."
        )

    def _is_own_address(self, observed: str) -> bool:
        """Whether *observed* egress is the operator's own address.

        Compares canonical forms, so the mapped and presentation variants of one
        address are one address. Both sides are canonicalised rather than only
        one: the baseline may have arrived in either form, and a guard that
        canonicalises only the candidate is a guard with a bypass.
        """
        if self._own is None:
            # Unreachable via acquire, which requires it first. Answering False
            # rather than raising keeps this usable from a status path that has
            # no lease to give, and cannot be reached with a pool that leases.
            return False
        candidate = canonical_address(observed)
        return candidate is not None and candidate == self._own

    def _dismiss_as_own_address(
        self, endpoint: ProxyEndpoint, egress: EgressObservation
    ) -> None:
        """Retire an origin that egresses from the operator's own address."""
        self._self_egress[endpoint.origin] = egress.ip
        # ERROR, not WARNING. The operator's next move after "no usable proxy"
        # is to add more addresses, which would not fix this and would spend
        # money. The line names the origin and the address so the entry in
        # proxies.txt can be found and read.
        log.error(
            "[proxies] %s egresses from %s, which is this machine's own address. "
            "It is not a proxy. Reports sent through it would leave from the "
            "address Instagram already associates with the operator, so it is "
            "dismissed from the pool rather than retried. Remove it from the "
            "proxy file.",
            endpoint.origin,
            egress.ip,
        )

    def _fallback_ranked(self, now: float) -> list[ProxyHealth]:
        """Every address, least-sideline-first, ignoring cooldown entirely.

        Only reached when nothing is available. Ordered by how long each has
        been sidelined so the pool still tries its best remaining option first,
        and untried addresses (no sideline at all) lead.
        """

        def key(health: ProxyHealth) -> tuple[float, bool, float, float]:
            return (
                max(0.0, (health.quarantined_until or now) - now),
                health.last_verdict is not None,
                -health.health_score,
                self._tiebreak[health.endpoint.origin],
            )

        # A dismissed origin is not on cooldown, it is not a candidate. This is
        # a cost guard, not a safety guard: the per-candidate check in `acquire`
        # would refuse the address again anyway, so removing this filter leaks
        # nothing -- it re-probes a known-bad address on every fallback pass and
        # repeats the ERROR line with it. Kept because the fallback pass is the
        # one that runs under load, and a pool that re-asks a question it has a
        # permanent answer to is a pool whose logs stop being readable.
        return sorted(
            (
                health
                for health in self._health.values()
                if health.endpoint.origin not in self._self_egress
            ),
            key=key,
        )

    def _asn_capacity(self) -> int:
        known = {h.egress.asn for h in self._health.values() if h.egress and h.egress.asn is not None}
        return len(known)

    def _relaxable(self, leased_asns: set[int]) -> bool:
        """True when some address's ASN is not yet known, so a fresh one may exist.

        A provider whose echo response carries no ASN cannot be diversity-checked
        at all. That is reported by :meth:`asn_diversity_report` rather than
        silently accepted, because "enforced" and "not applicable" must not read
        the same way in a log.
        """
        unknown = any(
            h.egress is None or h.egress.asn is None for h in self._health.values()
        )
        return unknown

    def _ranked(self, now: float) -> list[ProxyHealth]:
        """Best first: proven, then health score, then a fixed random tiebreak."""
        candidates = self.available(now)
        if not candidates:
            return []
        return sorted(candidates, key=self._order_key)

    def _order_key(self, health: ProxyHealth) -> tuple[bool, float, float]:
        """Sort key: has it ever worked, then score, then a stable random tie.

        The third component is drawn once at registration and never changes, so
        the order is a total order that is stable across calls -- which is what
        makes the ranking reproducible and what stops a tie from always resolving
        to whichever address happens to sort first by name.

        An earlier version rotated the *whole* ranked list by a random offset to
        achieve the same spreading, which quietly defeated both earlier keys: the
        proven address could end up anywhere in the list, so a blip on a
        validated session could cost more than the spreading ever bought.
        Tie-breaking inside the key cannot do that.
        """
        return (
            health.successes == 0,
            -health.health_score,
            self._tiebreak[health.endpoint.origin],
        )

    def _bind(self, health: ProxyHealth, result: ProbeResult) -> ProxyLease:
        now = self._monotonic()
        health.record_ok(now, result.egress)
        self._sequence += 1
        lease = ProxyLease(
            lease_id=f"px-{self._sequence:05d}",
            endpoint=health.endpoint,
            sticky_expires_at=now + self._sticky_ttl,
            acquired_at=now,
            egress=result.egress,
        )
        self._leases[lease.lease_id] = lease
        log.info(
            "bound %s (egress %s%s) for %.0fmin",
            health.endpoint.origin,
            result.egress.ip if result.egress else "?",
            f", AS{result.egress.asn}" if result.egress and result.egress.asn else "",
            self._sticky_ttl / 60,
        )
        return lease

    def _probe(self, endpoint: ProxyEndpoint) -> ProbeResult:
        """Ask *endpoint* what it is, and grade the answer.

        Every result passes through :func:`_grade_egress` on the way out, so
        the check that a lease is never bound to an unidentifiable or reserved
        address is in one place rather than at each call site that might forget
        it. Grading here rather than in ``acquire`` also means the downgrade
        reaches ``record_failure`` and lands in the health record, so a pool
        that refuses an address can say why in its exhaustion report.
        """
        try:
            return _grade_egress(self._fetch(self._probe_url, endpoint.url))
        except ProxyUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors are data here
            # A transport that raises is a verdict, not a crash. The whole point
            # of classifying by outcome is that "could not tell" is an answer.
            log.debug("probe transport raised for %s: %s", endpoint.origin, exc)
            return ProbeResult(
                verdict=ProbeVerdict.CONNECT_ERROR,
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )

    def _exhausted_report(self, now: float) -> str:
        """Why every address is unavailable, one line each.

        Carries the last *detail*, not just the verdict name, and that is the
        difference between a report an operator can act on and a shrug. Ten
        addresses all reading ``unparseable`` is a provider problem; one
        reading ``unparseable`` and the rest reading ``egress 198.51.100.7 is
        a reserved range`` is a provider serving fixtures, and the two need
        completely different responses. A bare verdict enum cannot tell them
        apart, because the detail is where the difference lives.
        """
        lines = ["no proxy address is currently available:"]
        if not self._health:
            # The loop below produces nothing at all when no address was ever
            # configured, leaving a header and a dangling colon. That reads the
            # same as "everything I configured is exhausted", and the two have
            # opposite fixes: fill in the file, versus buy better addresses.
            # A blank report is the one thing an operator cannot act on.
            lines.append(
                "  (none configured -- the pool holds zero addresses, so there "
                "is nothing to report on)"
            )
            lines.append(
                "  check [proxies] file_path: it resolved, and it is empty or "
                "contained only comments"
            )
        for health in self._health.values():
            remaining = max(0.0, (health.quarantined_until or now) - now)
            verdict = health.last_verdict.value if health.last_verdict else "untried"
            if remaining > 0:
                state = f"cooling down {remaining:.0f}s (last: {verdict})"
            elif (
                health.last_used_monotonic is not None
                and now - health.last_used_monotonic < self._min_cooldown
            ):
                state = f"used {(now - health.last_used_monotonic):.0f}s ago"
            else:
                state = verdict
            detail = health.last_detail
            if detail and detail != verdict:
                state = f"{state} -- {detail}"
            lines.append(f"  {health.endpoint.origin}: {state}")
        return "\n".join(lines)

    # -- release and accounting -----------------------------------------

    def release(self, lease: ProxyLease) -> None:
        self._leases.pop(lease.lease_id, None)

    def rebind(self, lease: ProxyLease, **kwargs: Any) -> ProxyLease:
        """Replace a lease, retiring the old one only after the new is bound.

        Same ordering rule as the account pool: the old binding stays in force
        until a working replacement exists, so a failure to rebind leaves the
        caller holding a lease it can still inspect rather than an unbound
        account.
        """
        replacement = self.acquire(**kwargs)
        self.release(lease)
        return replacement

    def assert_lease_fresh(self, lease: ProxyLease) -> None:
        now = self._monotonic()
        lease.assert_fresh(now)
        if lease.lease_id not in self._leases:
            raise ProxyUnavailable(
                f"proxy lease {lease.lease_id} is no longer bound; it was "
                "superseded. The account's current address may differ from the "
                "one this report started on."
            )

    def note_failure(self, lease: ProxyLease, verdict: ProbeVerdict, detail: str) -> float:
        health = self._health.get(self._key(lease.endpoint))
        self.release(lease)
        if health is None:
            return 0.0
        return health.record_failure(
            verdict,
            detail,
            self._monotonic(),
            base=self._quarantine_base,
            cap=self._quarantine_cap,
        )

    def summary(self) -> str:
        now = self._monotonic()
        lines = [f"{'origin':<28} {'ok':>4} {'fail':>5} {'streak':>7} {'state':<26}"]
        lines.append("-" * 76)
        for health in sorted(self._health.values(), key=lambda h: -h.health_score):
            remaining = max(0.0, (health.quarantined_until or now) - now)
            state = f"cooldown {remaining:.0f}s" if remaining > 0 else (
                "available" if health.available(now) else "reused recently"
            )
            lines.append(
                f"{health.endpoint.origin:<28} {health.successes:>4} {health.failures:>5} "
                f"{health.consecutive_failures:>7} {state:<26}"
            )
        if self._leases:
            lines.append("")
            lines.append(f"{len(self._leases)} lease(s) active:")
            for row in self.asn_diversity_report():
                asn = f"AS{row['asn']}" if row["known"] else "AS? (not reported)"
                lines.append(f"  {asn}: {', '.join(row['origins'])}")
        if self._self_egress:
            # Not a footnote. An operator reading `status` has to see that these
            # entries are the reason the pool is short, because the alternative
            # reading -- "the pool is exhausted" -- has a remedy that costs
            # money and changes nothing.
            lines.append("")
            lines.append(self._dismissed_report())
        return "\n".join(lines)

    def __iter__(self) -> Iterator[ProxyHealth]:
        return iter(self._health.values())

    def __getitem__(self, key: int | str) -> ProxyHealth:
        """Address by index, or by origin string.

        By origin because that is how a caller refers to an address in a log or
        a failure report; by index because iteration order is insertion order
        and the tests need a stable handle on a specific address.
        """
        if isinstance(key, int):
            return list(self._health.values())[key]
        for health in self._health.values():
            if health.endpoint.origin == key or self._key(health.endpoint) == key:
                return health
        raise KeyError(key)


# --- the httpx transport ---------------------------------------------------
#
# Defined here, not in the pool, so the pool has no HTTP dependency at all and
# stays testable with a plain function. Everything above this line runs offline;
# only this factory, and the tests that monkeypatch ``httpx.Client.send``, need
# the library present.


def make_httpx_fetch(
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    trust_env: bool = True,
) -> Fetch:
    """Build a ``fetch`` backed by ``httpx``.

    Every failure mode becomes a :class:`ProbeResult` rather than an exception.
    A transport that raises is not a broken design here -- the pool handles that
    too -- but there is no reason to convert a known timeout into an exception
    only for the caller to convert it straight back into a verdict.

    ``trust_env`` is passed through to httpx and left on by default, because for
    an address under test the operator's ``HTTPS_PROXY`` is often exactly how
    that address is reached. :func:`make_direct_fetch` is the case that turns it
    off, and the reason is in its docstring.
    """
    import httpx

    default_headers = {"Accept": "application/json, text/plain, */*"}
    default_headers.update(headers or {})

    def fetch(url: str, proxy: str | None) -> ProbeResult:
        started = time.monotonic()
        try:
            with httpx.Client(
                proxy=proxy,
                timeout=timeout,
                follow_redirects=True,
                headers=default_headers,
                trust_env=trust_env,
            ) as client:
                response = client.get(url)
        except httpx.TimeoutException as exc:
            return ProbeResult(
                verdict=ProbeVerdict.TIMEOUT,
                detail=f"{type(exc).__name__}: {exc}"[:200],
                elapsed=time.monotonic() - started,
            )
        except httpx.HTTPError as exc:
            return ProbeResult(
                verdict=ProbeVerdict.CONNECT_ERROR,
                detail=f"{type(exc).__name__}: {exc}"[:200],
                elapsed=time.monotonic() - started,
            )

        elapsed = time.monotonic() - started
        body = response.text
        content_type = response.headers.get("content-type")

        if response.status_code >= 400:
            # Classify the body even on an error status: a 403 from a provider is
            # usually an HTML page naming the reason, and that reason is more
            # useful than the code.
            verdict, detail = classify_body(body, response.status_code, content_type=content_type)
            if verdict is ProbeVerdict.UNPARSEABLE:
                verdict = ProbeVerdict.STATUS_ERROR
                detail = f"HTTP {response.status_code}"
            return ProbeResult(
                verdict=verdict,
                status=response.status_code,
                body=body[:2000],
                detail=detail,
                elapsed=elapsed,
            )

        verdict, detail = classify_body(body, response.status_code, content_type=content_type)
        egress = parse_ip_echo(body) if verdict is ProbeVerdict.OK else None
        if verdict is ProbeVerdict.OK and egress is None:
            # Defensive: classify_body said there is an address literal, but the
            # parser could not find one. Never let the two disagree in the
            # direction of "healthy".
            verdict = ProbeVerdict.UNPARSEABLE
            detail = "body looked healthy but yielded no address"

        return ProbeResult(
            verdict=verdict,
            status=response.status_code,
            body=body[:2000],
            detail=detail,
            egress=egress,
            elapsed=elapsed,
        )

    return fetch


def make_direct_fetch(
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> Callable[[str], ProbeResult]:
    """A ``fetch`` that can only observe *this* machine's own address.

    Takes a bare URL rather than ``(url, proxy)`` so it cannot be handed a
    proxy by accident. The signature is the guard: every call site that could
    pass one has to reach for a different function, rather than passing ``None``
    and hoping.

    ``trust_env`` is off, and that is the part that matters. With httpx's default
    the request would egress through ``HTTPS_PROXY`` if the operator has one
    set -- a corporate VPN, a debugging tool -- and the "own address" recorded
    would be that proxy's address. The comparison would then be real-looking and
    wrong: it would hold against a pool entry pointing at the VPN, and pass for
    a pool entry pointing at the actual home connection, which is the case the
    guard exists to catch.

    An operator genuinely behind something that rewrites their egress should
    declare ``own_ip`` explicitly instead. Silently inferring it is how a
    security check becomes a no-op with a log line saying it ran.
    """
    fetch = make_httpx_fetch(timeout=timeout, headers=headers, trust_env=False)

    def direct(url: str) -> ProbeResult:
        return fetch(url, None)

    return direct


# --- wiring -----------------------------------------------------------------


def build_pool(
    config: Any,
    *,
    fetch: Fetch,
    monotonic: Callable[[], float] = time.monotonic,
    probe_url: str = "https://api.ipify.org?format=json",
    min_cooldown: float = MIN_PROXY_COOLDOWN_SECONDS,
    rng: random.Random | None = None,
    enforce_asn_diversity: bool = True,
    own_ip: str | None = None,
    direct_fetch: Callable[[str], ProbeResult] | None = None,
) -> ProxyPool:
    """Build a pool from an :class:`~insta_report.config.ProxyConfig`.

    Typed as ``Any`` on purpose. ``config`` deliberately does not import this
    module, so a real import here would be circular; the alternative -- moving
    the config class in here -- would put a file-path field and an env lookup in
    a module that is otherwise about health and leases.

    This is also where the operator's own address is established, and it is here
    rather than inside ``ProxyPool`` for two reasons. The pool stays free of
    network I/O at construction, which is what keeps it testable with a plain
    function and no socket; and an address observed once at startup is one
    answer, where an address observed per lease is a fresh HTTP request per
    report against a third party.

    Declared ``own_ip`` wins. Otherwise the address is observed once, directly.
    If neither yields an address the pool is still built, and the refusal
    happens at the first ``acquire`` with a message naming both ways out -- not
    here, because a pool that cannot be constructed cannot be inspected, and an
    operator whose only problem is an unreachable IP-echo service should be told
    that in one line rather than by a stack trace out of the constructor.

    An operator with no proxy configuration gets an empty pool and a loud
    warning rather than a silent direct connection. Failing closed is the point:
    a run that quietly bypasses the exit it was configured to use is worse than
    a run that refuses to start, because the operator has no way to tell from
    the output that the isolation they paid for was not applied.
    """
    source = getattr(config, "source", "file")
    label = "operator file"
    skipped: list[str] = []

    endpoints: list[ProxyEndpoint]
    if source == "file":
        file_path = getattr(config, "file_path", None)
        if file_path is None:
            log.error("[proxies] source = 'file' but no file_path is configured")
            endpoints = []
        else:
            endpoints = parse_proxy_file(file_path, label=label, problems=skipped)
    elif source == "provider":
        key = config.resolved_key() if hasattr(config, "resolved_key") else None
        if not key:
            log.error(
                "[proxies] source = 'provider' but no key is available in the "
                "environment (%s). Not falling back to a direct connection.",
                getattr(config, "provider_key_env", None) or "no key env var named",
            )
            endpoints = []
        else:
            # The provider API shape is vendor-specific and changes on the
            # vendor's schedule, so this does not guess one. Fetch the addresses
            # however the provider documents it and point source = "file" at the
            # result -- see StaticProvider's docstring.
            log.error(
                "[proxies] no adapter is registered for provider %r. Fetch the "
                "addresses with the provider's own tooling, write them to a file, "
                "and set source = 'file'.",
                getattr(config, "provider", None),
            )
            endpoints = []
    else:
        log.error("[proxies] unknown source %r; expected 'file' or 'provider'", source)
        endpoints = []

    if skipped:
        # An operator who wrote a list of exits and got a shorter one back has
        # fewer exits than they believe, which is the kind of thing that only
        # surfaces later as an unexplained "no addresses available". Counted
        # loudly, and named, rather than left to a per-line warning nobody
        # reads.
        log.error(
            "[proxies] %d line(s) in the proxy file were skipped as unparseable, "
            "leaving %d address(es). Fix the lines or the pool will run out of "
            "exits mid-run. First problem: %s",
            len(skipped),
            len(endpoints),
            skipped[0],
        )

    if not endpoints:
        log.error(
            "[proxies] the pool is empty. This run will refuse to bind an exit "
            "rather than connecting directly -- a silent direct connection would "
            "mean the configured isolation was never applied."
        )

    return ProxyPool(
        endpoints,
        fetch=fetch,
        monotonic=monotonic,
        sticky_ttl=float(getattr(config, "sticky_ttl_minutes", 240)) * 60.0,
        probe_url=probe_url,
        min_cooldown=min_cooldown,
        rng=rng,
        enforce_asn_diversity=enforce_asn_diversity,
        own_ip=own_ip if own_ip is not None else _observe_own_address(
            probe_url, direct_fetch
        ),
    )


def _observe_own_address(
    probe_url: str,
    direct_fetch: Callable[[str], ProbeResult] | None,
) -> str | None:
    """This machine's own address, observed once. ``None`` if it cannot be had.

    A ``None`` here is not an error to raise: it becomes the first
    ``acquire``'s refusal, which names both remedies. Raising here instead
    would mean a pool that could not be built could not be inspected, and the
    most common cause -- an IP-echo service briefly unreachable -- deserves one
    line, not a stack trace out of a constructor.

    A declared ``own_ip`` never reaches this, which is the point of it: an
    operator behind a NAT or a corporate egress can state the address rather
    than have it guessed from whichever path the packet took.
    """
    if direct_fetch is None:
        # Injected as None by tests and by any caller that has deliberately
        # opted out of network I/O at construction. Reported as "not
        # established" so the refusal says so rather than implying an attempt.
        return None
    result = direct_fetch(probe_url)
    if result.egress is None:
        log.error(
            "[proxies] could not establish this machine's own IP address (%s). "
            "Leases will be refused rather than issued unchecked -- a lease that "
            "cannot be compared against the operator's own address is a lease "
            "that might be the operator's own address. Set own_ip in [proxies] "
            "to supply it directly.",
            result.detail or result.verdict.value,
        )
        return None
    log.info(
        "[proxies] operator's own address is %s; leases egressing from it are "
        "dismissed.",
        result.egress.ip,
    )
    return result.egress.ip
