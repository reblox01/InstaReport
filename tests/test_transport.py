"""Tests for the live transport.

Two things are being pinned here, and only these two:

1. **What counts as a working exit.** ``classify_body`` has its own oracle in
   ``test_proxies.py``; this module only checks that :func:`fetch` wires the
   response into it correctly. The interesting assertion is the one that
   overwrites a ``200 OK`` with a non-OK verdict, because a proxy that answers
   ``200`` while saying nothing useful is the exact shape a status-code check
   mistakes for success.

2. **Egress identification.** An address that cannot be identified, or that
   turns out to be a documentation range, is not a working exit. Both of those
   downgrade an otherwise-passing response.

Everything here runs offline. The transport is the *only* place in the project
allowed to need a socket, and it is the one place with nothing to test without
one -- so the client is injected and no request is ever made.
"""

from __future__ import annotations

import json

import httpx
import pytest

from insta_report.proxies import ProbeVerdict
from insta_report.transport import fetch, parse_egress


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_handler(payload, *, status: int = 200, content_type: str = "application/json"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=json.dumps(payload).encode("utf-8"),
            headers={"content-type": content_type},
            request=request,
        )

    return handler


# --- egress parsing ---------------------------------------------------------


class TestParseEgress:
    @pytest.mark.parametrize("field", ["ip", "query", "origin", "ipAddress", "client_ip"])
    def test_the_common_field_names_are_all_accepted(self, field):
        observed = parse_egress(json.dumps({field: "8.8.8.8"}))
        assert observed is not None, field
        assert observed.ip == "8.8.8.8"

    def test_a_nested_asn_object_is_understood(self):
        observed = parse_egress(json.dumps({"ip": "8.8.8.8", "asn": {"asn": 15169}}))
        assert observed is not None
        assert observed.asn == 15169

    @pytest.mark.parametrize(
        "asn,expected",
        [
            ("AS15169", 15169),
            ("as15169", 15169),
            ("15169", 15169),
            (15169, 15169),
            ("not a number", None),
            ({"number": "15169"}, 15169),
            (None, None),
        ],
    )
    def test_provider_disagreement_about_asn_shape_is_tolerated(self, asn, expected):
        """Every public egress service spells ASN differently.

        A wrong ASN is worse than a missing one -- it defeats the diversity
        check that is the point of collecting it -- so an unrecognised shape
        becomes ``None`` rather than a guess.
        """
        observed = parse_egress(json.dumps({"ip": "8.8.8.8", "asn": asn}))
        assert observed is not None
        assert observed.asn == expected

    def test_a_body_that_is_not_an_address_object_identifies_nothing(self):
        for body in ("", "not json", "[1, 2, 3]", "null", '"a string"'):
            assert parse_egress(body) is None, body

    def test_a_whitespace_padded_address_is_trimmed(self):
        observed = parse_egress(json.dumps({"ip": "  8.8.8.8\n"}))
        assert observed is not None
        assert observed.ip == "8.8.8.8"

    def test_a_bare_literal_is_understood_without_json(self):
        """Most echo services answer with the address and nothing else."""
        observed = parse_egress("203.0.113.9\n")
        assert observed is not None
        assert observed.ip == "203.0.113.9"

    def test_the_whole_function_is_shared_rather_than_reimplemented(self):
        """``parse_egress`` is an alias, and that is the property.

        The transport used to carry its own copy of this logic with its own
        reserved-range table, and the two drifted. An alias cannot drift; a
        re-export that recomputes cannot. Asserting identity of the function
        object is the only way to say "this is the shared one" in a test.
        """
        from insta_report.proxies import parse_ip_echo

        assert parse_egress is parse_ip_echo


class TestReservedRanges:
    """The range list is ``proxies.py``'s, and that is the point.

    There was a second copy here once, and the two drifted: this one covered
    RFC 2544's benchmarking range and the shared table did not, so
    ``transport.fetch`` and ``ProxyPool`` disagreed about whether an address
    was real. The oracle for the list itself lives in ``test_proxies.py``; all
    this pins is that the transport reads the same answer, because "is this
    address real" having two answers is the bug.
    """

    @pytest.mark.parametrize(
        "ip",
        [
            "192.0.2.7",  # TEST-NET-1
            "198.51.100.7",  # TEST-NET-2
            "203.0.113.7",  # TEST-NET-3
            "198.18.0.1",  # benchmarking, low
            "198.19.255.255",  # benchmarking, high
            "2001:db8::1",
            "2001:DB8::1",  # casefolded before comparison
        ],
    )
    def test_reserved_and_benchmarking_ranges_are_not_real_exits(self, ip):
        observed = parse_egress(json.dumps({"ip": ip}))
        assert observed is not None
        assert observed.is_documentation_range is True

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",
            "1.1.1.1",
            "198.20.0.1",  # just past the benchmarking /15
            "2606:4700::1111",  # a real global v6 address
            "198.51.101.7",  # just past TEST-NET-2
        ],
    )
    def test_ordinary_addresses_are_real_exits(self, ip):
        observed = parse_egress(json.dumps({"ip": ip}))
        assert observed is not None
        assert observed.is_documentation_range is False

    def test_transport_and_pool_agree_about_a_reserved_address(self):
        """The one assertion that would have caught the drift.

        Reads the answer twice, through both paths, and requires the same
        verdict. A future change to either list alone fails here instead of
        producing a pool that accepts exits the transport called fake.
        """
        from insta_report.proxies import _build_observation

        for ip in ("198.51.100.7", "198.18.4.4", "8.8.8.8", "2001:db8::1"):
            via_transport = parse_egress(json.dumps({"ip": ip}))
            via_pool = _build_observation(ip)
            assert via_transport is not None
            assert via_transport.is_documentation_range == via_pool.is_documentation_range, ip


# --- fetch ------------------------------------------------------------------


class TestFetch:
    def test_a_clean_json_probe_is_ok_and_identifies_the_exit(self):
        client = _client(_json_handler({"ip": "203.0.113.99", "asn": "AS64500"}))
        result = fetch("https://example.invalid/probe", client=client)
        assert result.verdict is ProbeVerdict.OK
        assert result.status == 200
        assert result.egress is not None
        assert result.egress.ip == "203.0.113.99"

    def test_a_documentation_address_is_observed_and_flagged_not_hidden(self):
        """The body parsed and the status is 200, but the "exit" is a fixture.

        The transport reports what it saw and lets the pool refuse it. Hiding
        the observation here would make the refusal unexplainable in a log --
        and a fixture address passed to a report means reporting from a fake
        address while the pool's diversity accounting believes it found a
        distinct one.
        """
        client = _client(_json_handler({"ip": "198.51.100.7"}))
        result = fetch("https://example.invalid/probe", client=client)
        assert result.egress is not None
        assert result.egress.is_documentation_range is True

    def test_a_200_that_identifies_nothing_is_not_ok(self):
        """The hole the egress requirement exists to close.

        ``{"status": "ok"}`` from a proxy provider is a 200 with no address in
        it. A status-code check calls that a working exit.
        """
        client = _client(_json_handler({"status": "ok"}))
        result = fetch("https://example.invalid/probe", client=client)
        assert result.verdict is ProbeVerdict.UNPARSEABLE
        assert result.egress is None

    def test_an_out_of_range_address_in_prose_is_not_an_identified_exit(self):
        """Why the transport's own downgrade is not dead code.

        ``classify_body`` finds an address by regex, and
        ``\\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b`` is satisfied by ``999.1.1.1``.
        ``ipaddress`` rejects it. So this body is graded OK by the classifier
        and yields no observation from the parser, and the gap between those
        two is exactly what this re-check exists to close.
        """
        client = _client(
            lambda request: httpx.Response(
                200,
                content=b"upstream 999.1.1.1 unreachable",
                headers={"content-type": "text/plain"},
                request=request,
            )
        )
        result = fetch("https://example.invalid/probe", client=client)
        assert result.verdict is ProbeVerdict.UNPARSEABLE
        assert result.egress is None
        assert "did not identify the exit" in result.detail

    def test_an_html_interstitial_is_not_a_success_despite_a_200(self):
        """F9: proxy failure is a 200 with an HTML body."""
        client = _client(
            lambda request: httpx.Response(
                200,
                content=b"<html><body>Access denied</body></html>",
                headers={"content-type": "text/html"},
                request=request,
            )
        )
        result = fetch("https://example.invalid/probe", client=client)
        assert result.verdict is not ProbeVerdict.OK

    @pytest.mark.parametrize("status", [429, 403, 500, 302])
    def test_an_error_status_is_graded_not_raised(self, status):
        client = _client(
            lambda request: httpx.Response(status, content=b"", request=request)
        )
        result = fetch("https://example.invalid/probe", client=client)
        assert result.verdict is not ProbeVerdict.OK
        assert result.status == status

    def test_a_non_2xx_carrying_an_address_is_still_not_ok(self):
        """The order of the two checks, which is the whole point.

        ``classify_body`` defines a healthy answer as "an address literal
        appears in the body". A provider's 404 page and a captive portal's
        error page both routinely contain one. Without the status gate first,
        a failed request is graded as a working exit and the lease is bound.
        """
        client = _client(
            lambda request: httpx.Response(
                404,
                content=b"no route to 203.0.113.5",
                headers={"content-type": "text/plain"},
                request=request,
            )
        )
        result = fetch("https://example.invalid/probe", client=client)
        assert result.verdict is ProbeVerdict.STATUS_ERROR
        assert result.egress is None

    def test_a_timeout_is_a_verdict_not_an_exception(self):
        """A pool that raised out of fetch would have to decide at every call
        site which failures mean "try the next address"."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow", request=request)

        result = fetch("https://example.invalid/probe", client=_client(handler))
        assert result.verdict is ProbeVerdict.TIMEOUT
        assert "did not answer within" in result.detail

    def test_a_refused_proxy_is_reported_as_an_auth_problem(self):
        """Distinct from a connect error.

        The proxy refusing is the provider's answer about credentials or quota,
        not the destination being unreachable -- and an operator debugging a
        provider needs the difference.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ProxyError("407", request=request)

        result = fetch("https://example.invalid/probe", client=_client(handler))
        assert result.verdict is ProbeVerdict.AUTH_FAILED
        assert "refused" in result.detail

    def test_a_connection_failure_is_graded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        result = fetch("https://example.invalid/probe", client=_client(handler))
        assert result.verdict is ProbeVerdict.CONNECT_ERROR

    def test_an_oversized_body_is_truncated_not_returned_whole(self):
        """A DNS-hijacking provider can answer with megabytes of anything, and
        the result is retained on the ProbeResult for the log."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"x" * (200 * 1024),
                headers={"content-type": "application/json"},
                request=request,
            )

        result = fetch("https://example.invalid/probe", client=client_of(handler))
        assert len(result.body) <= 64 * 1024

    def test_the_user_agent_is_the_probe_not_a_browser(self):
        """A probe that identifies itself as Chrome is asking to be treated as
        the traffic it is measuring, and a provider that blocks scrapers will
        answer the probe and not the reports."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("user-agent", ""))
            return httpx.Response(
                200,
                content=json.dumps({"ip": "8.8.8.8"}).encode(),
                headers={"content-type": "application/json"},
                request=request,
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch("https://example.invalid/probe", user_agent="insta-report/test", client=client)
        assert seen == ["insta-report/test"]


def client_of(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))
