"""Proxy pool tests.

The centre of gravity here is F9: a blocked residential address answers
``HTTP 200`` with an HTML interstitial, so every status-code health check passes
it. ``test_a_block_page_arriving_as_200_is_not_healthy`` is the test that earns
this file -- if the classifier ever goes back to reading status codes, it is
the one that fails.

The second load-bearing group is the lease invariants: sticky TTL, egress
assertion, and ASN diversity. Each of those exists because the alternative is a
report going out under a binding nobody intended.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from urllib.parse import urlparse

import pytest

from insta_report.errors import ProxyUnavailable
from insta_report.proxies import (
    EgressObservation,
    HttpJsonProvider,
    ProbeResult,
    ProbeVerdict,
    ProxyEndpoint,
    ProxyLease,
    ProxyPool,
    StaticProvider,
    classify_body,
    extract_common_shapes,
    parse_ip_echo,
    parse_proxy_file,
)

# --- helpers ---------------------------------------------------------------


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def ok(ip: str = "203.0.113.7", asn: int = 64500, country: str = "US") -> ProbeResult:
    return ProbeResult(
        verdict=ProbeVerdict.OK,
        status=200,
        body=json.dumps({"ip": ip, "asn": asn, "country": country}),
        egress=EgressObservation(ip=ip, asn=asn, country=country),
    )


def blocked_200(detail: str = "blocked") -> ProbeResult:
    """The F9 shape: HTTP 200, HTML body, an explicit refusal."""
    return ProbeResult(
        verdict=ProbeVerdict.BLOCKED,
        status=200,
        body="<html><body>Access Denied - your IP has been blocked</body></html>",
        detail=detail,
    )


def host_of(proxy_url: str) -> str:
    return urlparse(proxy_url).hostname or "?"


def make_pool(
    endpoints=None,
    fetch=None,
    *,
    clock=None,
    min_cooldown=0.0,
    **kwargs,
) -> ProxyPool:
    clock = clock or Clock()
    endpoints = endpoints if endpoints is not None else [
        ProxyEndpoint(f"http://10.0.0.{i}:8080", label=f"p{i}") for i in (1, 2, 3)
    ]
    if fetch is None:
        fetch = lambda url, proxy: ok()
    kwargs.setdefault("monotonic", clock)
    kwargs.setdefault("probe_url", "https://echo.invalid/ip")
    kwargs.setdefault("rng", random.Random(5))
    kwargs["min_cooldown"] = min_cooldown
    return ProxyPool(endpoints, fetch=fetch, **kwargs)


# --- F9: the body is the verdict -------------------------------------------


def test_a_block_page_arriving_as_200_is_not_healthy():
    """The single most important assertion in this file.

    A 200 with a refusal in the body is a dead address. Any health check that
    reads only the status code calls this address healthy and spends a session
    discovering otherwise.
    """
    verdict, reason = classify_body(
        "<html><body>Access Denied - your IP has been blocked</body></html>", 200
    )
    assert verdict is ProbeVerdict.BLOCKED
    assert verdict.usable is False
    assert verdict.looks_healthy_to_a_status_check is True, (
        "this is exactly the verdict a status-only check would pass"
    )
    assert "refusal" in reason


def test_an_html_interstitial_is_classified_not_guessed_at():
    verdict, _ = classify_body(
        "<!DOCTYPE html><html><body>hello</body></html>",
        200,
        content_type="text/html; charset=utf-8",
    )
    assert verdict is ProbeVerdict.INTERSTITIAL
    assert verdict.usable is False


def test_html_without_a_content_type_is_unparseable_not_assumed_interstitial():
    """Without the header we know the shape but not the intent.

    Both verdicts are unusable, so this cannot read as healthy either way -- but
    naming it ``UNPARSEABLE`` keeps the log honest: "it looked like HTML" and "it
    was a provider page" are different findings.
    """
    verdict, _ = classify_body("<!DOCTYPE html><html><body>hello</body></html>", 200)
    assert verdict is ProbeVerdict.UNPARSEABLE
    assert verdict.usable is False


def test_an_empty_200_is_not_healthy():
    verdict, _ = classify_body("", 200)
    assert verdict is ProbeVerdict.EMPTY
    assert verdict.usable is False


def test_a_2xx_with_no_address_is_unparseable_not_assumed_ok():
    """A 2xx that says nothing is not a success. It is an absence of evidence."""
    verdict, _ = classify_body("???", 200)
    assert verdict is ProbeVerdict.UNPARSEABLE
    assert verdict.usable is False


def test_a_2xx_json_with_no_address_is_unparseable():
    verdict, _ = classify_body('{"status":"ok"}', 200, content_type="application/json")
    assert verdict is ProbeVerdict.UNPARSEABLE


def test_407_is_an_auth_failure():
    verdict, reason = classify_body("", 407)
    assert verdict is ProbeVerdict.AUTH_FAILED
    assert "credential" in reason


def test_a_bare_address_is_healthy():
    verdict, _ = classify_body("203.0.113.7", 200)
    assert verdict is ProbeVerdict.OK
    assert verdict.usable is True


def test_ipv6_is_recognised():
    verdict, _ = classify_body("2001:db8::1", 200)
    assert verdict is ProbeVerdict.OK


def test_json_with_an_address_is_healthy():
    verdict, _ = classify_body('{"ip":"198.51.100.9"}', 200, content_type="application/json")
    assert verdict is ProbeVerdict.OK


def test_a_block_marker_inside_a_json_body_still_wins():
    """A JSON envelope around a refusal must not read as healthy."""
    verdict, _ = classify_body('{"error":"proxy detected","blocked":true}', 200)
    assert verdict.usable is False


def test_every_non_ok_2xx_verdict_looks_healthy_to_a_naive_check():
    """Makes the trap explicit: these are the verdicts status-only checking misses."""
    misleading = {
        v for v in ProbeVerdict if v.looks_healthy_to_a_status_check
    }
    assert misleading == {
        ProbeVerdict.INTERSTITIAL,
        ProbeVerdict.BLOCKED,
        ProbeVerdict.EMPTY,
        ProbeVerdict.UNPARSEABLE,
    }
    assert all(v.usable is False for v in misleading)


# --- egress parsing --------------------------------------------------------


def test_ip_echo_json_yields_ip_country_and_asn():
    observed = parse_ip_echo(json.dumps({"ip": "203.0.113.7", "country": "DE", "asn": 64501}))
    assert observed is not None
    assert observed.ip == "203.0.113.7"
    assert observed.country == "DE"
    assert observed.asn == 64501
    assert observed.has_asn


def test_ip_echo_accepts_the_alternative_field_names():
    """Providers disagree on spelling; a missing key would silently disable the
    ASN check, so the alternatives are covered explicitly."""
    observed = parse_ip_echo(json.dumps({"query": "1.2.3.4", "countryCode": "FR", "as": "AS15169"}))
    assert observed is not None
    assert observed.ip == "1.2.3.4"
    assert observed.country == "FR"
    assert observed.asn == 15169


def test_ip_echo_falls_back_to_a_bare_address():
    observed = parse_ip_echo("203.0.113.7\n")
    assert observed is not None
    assert observed.ip == "203.0.113.7"
    assert observed.asn is None


def test_ip_echo_returns_nothing_for_a_block_page():
    """This is what stops an HTML 200 from counting as a working address."""
    assert parse_ip_echo("<html>Access denied</html>") is None
    assert parse_ip_echo("") is None
    assert parse_ip_echo("{}") is None


def test_documentation_ranges_are_flagged():
    """A probe returning 203.0.113.x was intercepted, not reached."""
    observed = parse_ip_echo('{"ip":"203.0.113.7"}')
    assert observed is not None
    assert observed.is_documentation_range is True


def test_a_real_address_is_not_flagged_as_documentation():
    observed = parse_ip_echo('{"ip":"8.8.8.8"}')
    assert observed is not None
    assert observed.is_documentation_range is False


def test_malformed_json_falls_through_to_literal_scanning():
    assert parse_ip_echo('{"ip": 203.0.113.7') is not None


# --- file parsing ----------------------------------------------------------


def test_proxy_file_parses_the_four_common_shapes(tmp_path: Path):
    path = tmp_path / "proxies.txt"
    path.write_text(
        "\n".join(
            [
                "# a comment",
                "10.0.0.1:8080",
                "10.0.0.2:8080",
                "",
                "user:pass@10.0.0.3:8080",
                "socks5://10.0.0.4:1080",
                "// another comment style",
            ]
        ),
        encoding="utf-8",
    )
    parsed = parse_proxy_file(path)
    assert len(parsed) == 4
    assert parsed[0].url == "http://10.0.0.1:8080"
    assert parsed[2].has_credentials
    assert parsed[3].url.startswith("socks5://")


def test_proxy_file_deduplicates_while_preserving_order(tmp_path: Path):
    path = tmp_path / "proxies.txt"
    path.write_text(
        "10.0.0.1:8080\n10.0.0.2:8080\n10.0.0.1:8080\n", encoding="utf-8"
    )
    parsed = parse_proxy_file(path)
    assert [e.host for e in parsed] == ["10.0.0.1", "10.0.0.2"]


def test_proxy_file_skips_unparseable_lines_without_dying(tmp_path: Path):
    """A strict parser would reject a working list over a stray blank token."""
    path = tmp_path / "proxies.txt"
    path.write_text("10.0.0.1:8080\nnot-a-proxy\n10.0.0.2:8080\n", encoding="utf-8")
    parsed = parse_proxy_file(path)
    assert [e.host for e in parsed] == ["10.0.0.1", "10.0.0.2"]


def test_proxy_file_never_exposes_credentials_in_its_own_repr(tmp_path: Path):
    path = tmp_path / "proxies.txt"
    path.write_text("user:supersecret@10.0.0.1:8080\n", encoding="utf-8")
    endpoint = parse_proxy_file(path)[0]
    assert "supersecret" not in repr(endpoint)
    assert "supersecret" not in endpoint.redacted()
    assert endpoint.origin == "10.0.0.1:8080"


def test_proxy_file_handles_crlf(tmp_path: Path):
    path = tmp_path / "proxies.txt"
    path.write_bytes(b"10.0.0.1:8080\r\n10.0.0.2:8080\r\n")
    assert len(parse_proxy_file(path)) == 2


def test_a_username_containing_an_at_sign_is_encoded(tmp_path: Path):
    path = tmp_path / "proxies.txt"
    path.write_text("us:er@name:pw@10.0.0.1:8080\n", encoding="utf-8")
    endpoint = parse_proxy_file(path)[0]
    assert endpoint.host == "10.0.0.1", "the last @ must be the host separator"
    assert endpoint.has_credentials


def test_an_endpoint_without_a_scheme_is_rejected():
    with pytest.raises(ValueError, match="no scheme"):
        ProxyEndpoint("10.0.0.1:8080")


def test_an_empty_endpoint_is_rejected():
    with pytest.raises(ValueError, match="may not be empty"):
        ProxyEndpoint("")


# --- pool behaviour --------------------------------------------------------


def test_acquire_probes_before_binding():
    """A lease is proof the address works, not a hopeful assignment.

    Binding without probing is how a run discovers a dead exit after the
    session is already established on it.
    """
    probed: list[str | None] = []

    def fetch(url, proxy):
        probed.append(proxy)
        return ok()

    pool = make_pool(fetch=fetch)
    pool.acquire()
    assert len(probed) == 1
    assert probed[0].startswith("http://10.0.0.")


def test_acquire_records_the_observed_egress():
    pool = make_pool(fetch=lambda u, p: ok(ip="198.51.100.5", asn=64510))
    lease = pool.acquire()
    assert lease.egress is not None
    assert lease.egress.ip == "198.51.100.5"
    assert lease.egress.asn == 64510


def test_a_blocked_address_is_never_bound():
    pool = make_pool(fetch=lambda u, p: blocked_200())
    with pytest.raises(ProxyUnavailable):
        pool.acquire()


def test_a_blocked_address_cooling_down_can_be_reported_before_giving_up():
    pool = make_pool(fetch=lambda u, p: blocked_200())
    with pytest.raises(ProxyUnavailable) as excinfo:
        pool.acquire()
    assert "cooling down" in str(excinfo.value)


def test_a_working_address_is_used_even_when_others_are_broken():
    """One good address among three bad ones is still a working pool."""

    def fetch(url, proxy):
        return ok() if proxy and "10.0.0.2" in proxy else blocked_200()

    pool = make_pool(fetch=fetch)
    lease = pool.acquire()
    assert lease.endpoint.host == "10.0.0.2", "the only working address must be found"


def test_a_transport_that_raises_is_recorded_as_a_verdict_not_a_crash():
    """'Could not tell' is an answer, and it is recorded as one.

    Every address here fails, so all of them get probed and every one has a
    verdict -- which is what makes this test independent of probe order.
    """

    def fetch(url, proxy):
        if "10.0.0.1" in proxy:
            raise OSError("connection refused")
        return blocked_200()

    pool = make_pool(fetch=fetch)
    with pytest.raises(ProxyUnavailable):
        pool.acquire()

    by_origin = {row["origin"]: row for row in pool.status()}
    assert by_origin["10.0.0.1:8080"]["last_verdict"] == "connect_error"
    assert "connection refused" in by_origin["10.0.0.1:8080"]["last_detail"]


def test_a_raising_address_does_not_stop_a_working_one_being_found():
    def fetch(url, proxy):
        if "10.0.0.1" in proxy:
            raise OSError("connection refused")
        return ok()

    pool = make_pool(fetch=fetch)
    assert pool.acquire().endpoint.host != "10.0.0.1"


def test_an_exhausted_pool_explains_itself():
    """Every address named, with why it is out."""
    pool = make_pool(fetch=lambda u, p: blocked_200(), min_cooldown=0.0)
    for _ in range(2):
        with pytest.raises(ProxyUnavailable):
            pool.acquire()

    with pytest.raises(ProxyUnavailable) as excinfo:
        pool.acquire()
    message = str(excinfo.value)
    assert "no proxy address is currently available" in message
    for octet in (1, 2, 3):
        assert f"10.0.0.{octet}:8080" in message
    assert "blocked" in message


def test_a_blocked_address_cooling_down_can_be_reported_before_giving_up():
    """Both passes are named, and the second says it was a fallback.

    The pool retries sidelined addresses rather than refusing outright -- a
    one-address configuration has to keep working -- but when that retry also
    finds them blocked, the message has to make clear these addresses were
    already sidelined. "3 were considered" alone reads like a fresh attempt and
    hides that the pool is genuinely out of options.
    """
    pool = make_pool(fetch=lambda u, p: blocked_200())

    with pytest.raises(ProxyUnavailable) as first:
        pool.acquire()
    assert "probing 3 address(es)" in str(first.value)
    assert "fallback pass" not in str(first.value)

    with pytest.raises(ProxyUnavailable) as second:
        pool.acquire()
    message = str(second.value)
    assert "fallback pass" in message, "the retry pass must identify itself"
    assert "cooling down" in message
    assert "blocked" in message


def test_a_healthy_address_is_preferred_over_an_untried_one():
    """A proven address outranks an untried one, so sessions are reused."""

    def fetch(url, proxy):
        return ok() if proxy and "10.0.0.2" in proxy else blocked_200()

    pool = make_pool(fetch=fetch)
    first = pool.acquire()
    assert first.endpoint.host == "10.0.0.2"

    pool.release(first)
    calls: list[str] = []
    pool._fetch = lambda url, proxy: (calls.append(proxy), ok())[1]  # noqa: SLF001
    pool.acquire()
    assert calls == ["http://10.0.0.2:8080"], "the proven address should be retried first"


def test_releasing_a_lease_frees_the_address():
    pool = make_pool()
    lease = pool.acquire()
    assert pool.active_leases() == (lease,)
    pool.release(lease)
    assert pool.active_leases() == ()


# --- F10: sticky TTL -------------------------------------------------------


def test_a_hold_longer_than_the_sticky_ttl_is_refused():
    """A lease outliving its own IP binding submits from the wrong address.

    A report that starts on one IP and submits from another is the shape of a
    hijacked session, so this is refused at acquire time rather than discovered
    mid-report.
    """
    pool = make_pool(sticky_ttl=600.0)
    with pytest.raises(ProxyUnavailable, match="sticky TTL"):
        pool.acquire(hold_for=1200.0)


def test_a_hold_within_the_ttl_is_accepted():
    pool = make_pool(sticky_ttl=3600.0)
    assert pool.acquire(hold_for=600.0) is not None


def test_a_lease_expires_with_its_sticky_window():
    clock = Clock()
    pool = make_pool(clock=clock, sticky_ttl=300.0)
    lease = pool.acquire()
    assert lease.expired(clock.now) is False
    clock.advance(299.0)
    assert lease.expired(clock.now) is False
    clock.advance(2.0)
    assert lease.expired(clock.now) is True


def test_asserting_a_fresh_lease_on_an_expired_one_explains_the_consequence():
    clock = Clock()
    pool = make_pool(clock=clock, sticky_ttl=60.0)
    lease = pool.acquire()
    clock.advance(120.0)
    with pytest.raises(ProxyUnavailable) as excinfo:
        pool.assert_lease_fresh(lease)
    assert "different IP" in str(excinfo.value)


def test_a_superseded_lease_is_refused_rather_than_used():
    """The same ordering rule as the account pool, in the same direction."""
    pool = asn_pool({"10.0.0.1": 64500, "10.0.0.2": 64501, "10.0.0.3": 64502})
    first = pool.acquire()
    second = pool.rebind(first)

    with pytest.raises(ProxyUnavailable, match="superseded"):
        pool.assert_lease_fresh(first)
    pool.assert_lease_fresh(second)


def test_rebind_retires_the_old_lease_only_after_the_new_one_exists():
    """A failed rebind must leave the old binding intact.

    Retiring first would leave the caller holding an unbound account, and the
    next report would go out direct.
    """
    pool = make_pool(fetch=lambda u, p: ok(), sticky_ttl=3600.0)
    original = pool.acquire()

    def failing_acquire(**kwargs):
        raise ProxyUnavailable("no address available")

    pool.acquire = failing_acquire  # type: ignore[method-assign]
    with pytest.raises(ProxyUnavailable):
        pool.rebind(original)

    assert original.lease_id in {lease.lease_id for lease in pool.active_leases()}
    pool.assert_lease_fresh(original)


# --- ASN diversity ---------------------------------------------------------


def asn_pool(asn_by_host: dict[str, int], **kwargs) -> ProxyPool:
    """A pool whose addresses each have a fixed, known ASN.

    Deterministic per address rather than a shared counter, because the ASN
    tests are about *which* address gets bound and a counter makes the answer
    depend on probe order -- which is exactly the thing under test.
    """

    def fetch(url, proxy):
        host = host_of(proxy)
        return ok(ip=f"198.51.100.{int(host.split('.')[-1])}", asn=asn_by_host[host])

    return make_pool(
        endpoints=[ProxyEndpoint(f"http://{host}:8080") for host in asn_by_host],
        fetch=fetch,
        **kwargs,
    )


def test_two_leases_cannot_share_an_asn():
    """Two leases on one ASN are two doors into the same building."""
    pool = asn_pool({"10.0.0.1": 64500, "10.0.0.2": 64500, "10.0.0.3": 64501})
    first = pool.acquire()
    second = pool.acquire()
    assert first.egress.asn != second.egress.asn


def test_an_address_repeating_a_leased_asn_is_not_bound():
    """The duplicate ASN is skipped; a distinct one is found instead.

    Asserts the invariant rather than which address wins, because the pool
    rotates genuine ties and pinning a specific address here would make the test
    a statement about the rotation seed rather than about diversity.
    """
    pool = asn_pool({"10.0.0.1": 64500, "10.0.0.2": 64500, "10.0.0.3": 64501})
    first, second = pool.acquire(), pool.acquire()
    assert first.egress.asn != second.egress.asn
    assert first.endpoint.origin != second.endpoint.origin


def test_a_skipped_duplicate_asn_does_not_damage_the_address_health():
    """Skipping an address for ASN reuse is a scheduling decision, not a fault.

    Recording it as a failure would quarantine a working address and demote it
    permanently behind addresses that have never been proven -- the opposite of
    what "prefer what works" means.
    """
    pool = asn_pool({"10.0.0.1": 64500, "10.0.0.2": 64500, "10.0.0.3": 64501})
    pool.acquire()
    pool.acquire()

    for health in pool:
        assert health.failures == 0, f"{health.endpoint.origin} was penalised for a skip"
        assert health.quarantined_until is None


def test_the_second_lease_finds_a_third_asn_when_a_pool_has_one():
    pool = asn_pool({"10.0.0.1": 64500, "10.0.0.2": 64501, "10.0.0.3": 64502})
    seen = {pool.acquire().egress.asn for _ in range(3)}
    assert seen == {64500, 64501, 64502}


def test_asn_diversity_can_be_switched_off_explicitly():
    """With enforcement off, a second lease on the same ASN is allowed."""
    pool = asn_pool({"10.0.0.1": 64500, "10.0.0.2": 64500, "10.0.0.3": 64500})
    first = pool.acquire()
    second = pool.acquire(require_distinct_asn=False)
    assert first.egress.asn == second.egress.asn == 64500


def test_diversity_is_reported_even_when_it_cannot_be_enforced():
    """An echo response with no ASN must read as unverified, not as enforced."""
    pool = asn_pool({"10.0.0.1": 64500, "10.0.0.2": 64501})
    pool.acquire()
    pool.release(pool.active_leases()[0])
    pool.acquire()
    assert all(row["known"] is True for row in pool.asn_diversity_report())

    blind = make_pool(fetch=lambda u, p: ok(asn=None))
    blind.acquire()
    blind.acquire()
    assert any(row["known"] is False for row in blind.asn_diversity_report())


# --- reporting -------------------------------------------------------------


def test_status_never_includes_credentials():
    pool = make_pool(
        endpoints=[ProxyEndpoint("http://user:supersecret@10.0.0.1:8080")],
        fetch=lambda u, p: ok(),
    )
    pool.acquire()
    assert "supersecret" not in json.dumps(pool.status())


def test_a_lease_renders_without_credentials():
    endpoint = ProxyEndpoint("http://user:supersecret@10.0.0.1:8080")
    lease = ProxyLease(
        lease_id="px-1",
        endpoint=endpoint,
        sticky_expires_at=100.0,
        acquired_at=0.0,
    )
    rendered = json.dumps(lease.redacted())
    assert "supersecret" not in rendered
    assert "10.0.0.1:8080" in rendered


def test_summary_lists_every_address_with_a_state():
    pool = make_pool()
    pool.acquire()
    text = pool.summary()
    for octet in (1, 2, 3):
        assert f"10.0.0.{octet}:8080" in text
    assert "lease(s) active" in text


def test_quarantine_backoff_doubles_and_caps():
    clock = Clock()
    health = make_pool(clock=clock, quarantine_base=10.0, quarantine_cap=40.0)[0]
    waits = [
        health.record_failure(ProbeVerdict.BLOCKED, "x", clock.now, base=10.0, cap=40.0)
        for _ in range(5)
    ]
    assert waits == [10.0, 20.0, 40.0, 40.0, 40.0]


def test_a_success_clears_the_failure_streak():
    clock = Clock()
    pool = make_pool(clock=clock, quarantine_base=10.0, quarantine_cap=40.0)
    health = pool[0]
    health.record_failure(ProbeVerdict.BLOCKED, "x", clock.now, base=10.0, cap=40.0)
    health.record_failure(ProbeVerdict.BLOCKED, "x", clock.now, base=10.0, cap=40.0)
    health.record_ok(clock.now, EgressObservation(ip="8.8.8.8"))
    assert health.consecutive_failures == 0
    assert health.quarantined_until is None


def test_a_proven_address_outranks_an_untried_one_after_a_blip():
    """One transient failure must not re-rank a proven address to the back.

    The quarantine window is what handles transience -- here it is set to zero so
    the address is immediately eligible again and this test measures the
    *ranking* rather than the quarantine. If a single blip also dropped the
    address behind untried ones, every momentary hiccup would discard a session
    that had already been validated.
    """
    clock = Clock()
    pool = make_pool(clock=clock)
    proven = pool[0]
    for _ in range(3):
        proven.record_ok(clock.now, EgressObservation(ip="8.8.8.8"))
    proven.record_failure(ProbeVerdict.TIMEOUT, "blip", clock.now, base=0.0, cap=0.0)

    assert pool._ranked(clock.now)[0] is proven  # noqa: SLF001
    others = [h for h in pool if h is not proven]
    assert pool._ranked(clock.now)[1:] == others  # noqa: SLF001


def test_an_untried_address_never_outranks_a_proven_one_even_when_proven_is_negative():
    """The primary key is 'has it ever worked', not the score.

    A freshly probed address with score 0 must not displace one with a history,
    which is why the sort leads with ``successes == 0`` rather than ordering by
    health_score alone.
    """
    clock = Clock()
    pool = make_pool(clock=clock)
    proven = pool[0]
    proven.record_ok(clock.now, EgressObservation(ip="8.8.8.8"))
    proven.record_failure(ProbeVerdict.TIMEOUT, "blip", clock.now, base=0.0, cap=0.0)

    assert proven.health_score < 0, "one failure against one success is negative"
    assert pool._ranked(clock.now)[0] is proven  # noqa: SLF001


def test_a_quarantined_address_is_excluded_until_its_window_closes():
    """The quarantine is what actually handles transience."""
    clock = Clock()
    pool = make_pool(clock=clock)
    proven = pool[0]
    for _ in range(3):
        proven.record_ok(clock.now, EgressObservation(ip="8.8.8.8"))
    proven.record_failure(ProbeVerdict.TIMEOUT, "blip", clock.now, base=30.0, cap=30.0)

    assert pool._ranked(clock.now)[0] is not proven, "still sidelined"
    clock.advance(31.0)
    assert pool._ranked(clock.now)[0] is proven, "and it leads again once the window closes"


def test_a_recently_used_address_is_not_immediately_reused():
    """Spinning the same address per report means a session check per report.

    With only one address available the pool must still hand it back rather than
    refuse -- refusing would deadlock a single-exit configuration -- but the
    cooldown is what stops a bigger pool from round-robining.
    """
    clock = Clock()
    pool = asn_pool({"10.0.0.1": 64500, "10.0.0.2": 64501, "10.0.0.3": 64502},
                    min_cooldown=45.0, clock=clock)
    lease = pool.acquire()
    pool.release(lease)

    available = {h.endpoint.origin for h in pool.available(clock.now)}
    assert lease.endpoint.origin not in available, "the just-used address is on cooldown"
    assert len(available) == 2

    clock.advance(46.0)
    assert len(pool.available(clock.now)) == 3


def test_a_single_address_pool_still_works_despite_the_cooldown():
    """A one-exit configuration is a legitimate setup, not an error.

    Cooldown governs *preference* among available addresses. If it also governed
    availability, this configuration would simply stop working -- and refusing
    would be a worse failure than the session re-validation it prevents.
    """
    clock = Clock()
    pool = asn_pool({"10.0.0.1": 64500}, min_cooldown=45.0, clock=clock)
    first = pool.acquire()
    pool.release(first)
    assert pool.acquire().endpoint.origin == "10.0.0.1:8080"


# --- providers -------------------------------------------------------------


def test_a_static_provider_supplies_the_initial_batch():
    endpoints = [ProxyEndpoint(f"http://10.0.0.{i}:8080") for i in range(1, 4)]
    pool = make_pool(endpoints=StaticProvider(endpoints), fetch=lambda u, p: ok())
    assert len(pool) == 3


def test_extracting_addresses_from_a_bare_list():
    assert extract_common_shapes(["1.2.3.4:8080", "5.6.7.8:8080"]) == [
        "1.2.3.4:8080",
        "5.6.7.8:8080",
    ]


def test_extracting_addresses_from_a_vendor_envelope():
    payload = {"proxies": [{"host": "1.2.3.4", "port": 8080}]}
    assert extract_common_shapes(payload) == ["http://1.2.3.4:8080"]


def test_extracting_addresses_with_credentials():
    payload = {"data": [{"ip": "1.2.3.4", "port": 9000, "username": "u", "password": "p"}]}
    assert extract_common_shapes(payload) == ["http://u:p@1.2.3.4:9000"]


def test_an_unrecognised_provider_shape_yields_nothing_rather_than_guessing():
    assert extract_common_shapes({"unexpected": "shape"}) == []


def test_the_http_provider_refuses_to_guess_without_a_transport():
    provider = HttpJsonProvider("https://vendor.invalid/proxies")
    with pytest.raises(ProxyUnavailable, match="without a transport"):
        provider.fetch(5)


def test_the_http_provider_uses_the_injected_transport():
    captured: dict = {}

    def transport(url, headers=None, params=None, timeout=None):
        captured.update(url=url, headers=headers, params=params)
        return {"proxies": ["1.2.3.4:8080", "5.6.7.8:8080"]}

    provider = HttpJsonProvider(
        "https://vendor.invalid/proxies",
        headers={"Authorization": "Bearer k"},
        query={"n": "2"},
        transport=transport,
    )
    endpoints = provider.fetch(2)
    assert captured["headers"] == {"Authorization": "Bearer k"}
    assert [e.url for e in endpoints] == [
        "http://1.2.3.4:8080",
        "http://5.6.7.8:8080",
    ]


# --- config wiring ---------------------------------------------------------


def test_a_file_backed_config_builds_a_pool(tmp_path: Path):
    from types import SimpleNamespace

    from insta_report.proxies import build_pool

    listing = tmp_path / "proxies.txt"
    listing.write_text("10.0.0.1:8080\n10.0.0.2:8080\n", encoding="utf-8")
    config = SimpleNamespace(source="file", file_path=listing, sticky_ttl_minutes=90)

    pool = build_pool(config, fetch=lambda u, p: ok())
    assert len(pool) == 2
    assert pool._sticky_ttl == 90 * 60.0  # noqa: SLF001


def test_a_configured_sticky_ttl_is_carried_into_the_lease(tmp_path: Path):
    from types import SimpleNamespace

    from insta_report.proxies import build_pool

    listing = tmp_path / "proxies.txt"
    listing.write_text("10.0.0.1:8080\n", encoding="utf-8")
    config = SimpleNamespace(source="file", file_path=listing, sticky_ttl_minutes=30)

    clock = Clock()
    pool = build_pool(config, fetch=lambda u, p: ok(), monotonic=clock)
    lease = pool.acquire()
    assert lease.sticky_expires_at == 30 * 60.0


def test_a_config_with_no_file_path_yields_an_empty_pool_rather_than_direct(caplog):
    """Failing closed: no configured exit must mean no exit, not a direct connection.

    A run that quietly bypasses its configured isolation is worse than a run
    that refuses, because nothing in the output reveals the isolation was never
    applied.
    """
    from types import SimpleNamespace

    from insta_report.proxies import build_pool

    config = SimpleNamespace(source="file", file_path=None, sticky_ttl_minutes=240)
    with caplog.at_level("ERROR"):
        pool = build_pool(config, fetch=lambda u, p: ok())

    assert len(pool) == 0
    assert any("no file_path" in r.message for r in caplog.records)
    assert any("rather than connecting directly" in r.message for r in caplog.records)
    with pytest.raises(ProxyUnavailable):
        pool.acquire()


def test_a_provider_source_with_no_key_refuses_rather_than_using_the_key_env_directly(caplog):
    from types import SimpleNamespace

    from insta_report.proxies import build_pool

    config = SimpleNamespace(
        source="provider",
        provider="somevendor",
        provider_key_env="NOT_SET_ANYWHERE",
        sticky_ttl_minutes=240,
        file_path=None,
    )
    with caplog.at_level("ERROR"):
        pool = build_pool(config, fetch=lambda u, p: ok())

    assert len(pool) == 0
    assert any("no key is available" in r.message for r in caplog.records)


def test_an_unknown_source_is_reported_by_name(caplog):
    from types import SimpleNamespace

    from insta_report.proxies import build_pool

    config = SimpleNamespace(source="harvest", file_path=None, sticky_ttl_minutes=240)
    with caplog.at_level("ERROR"):
        build_pool(config, fetch=lambda u, p: ok())
    assert any("unknown source 'harvest'" in r.message for r in caplog.records)


# --- http transport (built here, not imported by the pool) -----------------


def test_the_httpx_transport_classifies_a_200_block_page_as_blocked():
    """The real transport, exercised end to end without a network.

    Monkeypatching ``httpx.Client.send`` is the seam, not the pool: the pool has
    no HTTP dependency of its own, which is what keeps it testable offline.
    """
    import httpx

    from insta_report.proxies import make_httpx_fetch

    class FakeResponse:
        def __init__(self, status_code, text, headers=None):
            self.status_code = status_code
            self.text = text
            self.headers = headers or {}

    def fake_send(self, request, **kwargs):
        return FakeResponse(
            200,
            "<html><body>Access Denied - your IP has been blocked</body></html>",
            {"content-type": "text/html"},
        )

    original = httpx.Client.send
    httpx.Client.send = fake_send  # type: ignore[method-assign]
    try:
        fetch = make_httpx_fetch(timeout=5.0)
        result = fetch("https://echo.invalid/ip", "http://10.0.0.1:8080")
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]

    assert result.verdict is ProbeVerdict.BLOCKED
    assert result.status == 200
    assert result.verdict.usable is False


def test_the_httpx_transport_extracts_egress_on_success():
    import httpx

    from insta_report.proxies import make_httpx_fetch

    class FakeResponse:
        def __init__(self, status_code, text, headers=None):
            self.status_code = status_code
            self.text = text
            self.headers = headers or {}

    def fake_send(self, request, **kwargs):
        return FakeResponse(200, json.dumps({"ip": "198.51.100.7", "asn": 64511}))

    original = httpx.Client.send
    httpx.Client.send = fake_send  # type: ignore[method-assign]
    try:
        result = make_httpx_fetch(timeout=5.0)("https://echo.invalid/ip", None)
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]

    assert result.verdict is ProbeVerdict.OK
    assert result.egress is not None
    assert result.egress.ip == "198.51.100.7"
    assert result.egress.asn == 64511


def test_the_httpx_transport_reports_a_timeout_rather_than_raising():
    import httpx

    from insta_report.proxies import make_httpx_fetch

    def fake_send(self, request, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    original = httpx.Client.send
    httpx.Client.send = fake_send  # type: ignore[method-assign]
    try:
        result = make_httpx_fetch(timeout=5.0)("https://echo.invalid/ip", "http://10.0.0.1:8080")
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]

    assert result.verdict is ProbeVerdict.TIMEOUT
    assert "timed out" in result.detail


def test_the_httpx_transport_passes_the_proxy_through():
    import httpx

    from insta_report.proxies import make_httpx_fetch

    seen: dict = {}

    class FakeResponse:
        status_code = 200
        text = "198.51.100.7"
        headers = {"content-type": "text/plain"}

    def fake_send(self, request, **kwargs):
        seen.update(proxy=self._transport)
        return FakeResponse()

    original = httpx.Client.send
    httpx.Client.send = fake_send  # type: ignore[method-assign]
    try:
        make_httpx_fetch(timeout=5.0)("https://echo.invalid/ip", "http://10.0.0.1:8080")
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]

    assert seen["proxy"] is not None
