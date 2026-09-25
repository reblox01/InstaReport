"""The one place httpx exists.

``proxies.py`` deliberately has no HTTP dependency: its transport is an
injected ``fetch(url, proxy) -> ProbeResult``, so every awkward response shape
can be tested without a socket. This module is the other half of that
seam -- the real implementation, built here so the dependency is stated once
instead of being re-introduced at each call site that needs a live answer.

The same discipline as the browser channel, which is the only module that
imports Playwright. If a second module grows an ``import httpx``, the rule has
been broken and the fix is to move the call behind :func:`fetch` rather than to
add an exception.

```
   proxies.ProxyPool                 transport.py                  the network
   -------------                     -----------                  -----------
   acquire()
     └─ fetch(url, proxy) ───────────►  httpx.Client
           ◄── ProbeResult            ◄── status, body, headers
              verdict = classify_body(...)
              egress  = parse_egress(...)
```

**Why the verdict comes from the body.** A residential proxy that has been
flagged, or that has hit a provider quota, answers ``200`` with an HTML
interstitial. A status-code check calls that a success, hands the address to a
report, and the report is sent into a black hole. ``classify_body`` is the
decision, and it is the only thing that may set a verdict other than a
transport error.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .proxies import (
    ProbeResult,
    ProbeVerdict,
    classify_body,
    parse_ip_echo,
)

__all__ = ["fetch", "parse_egress", "DEFAULT_TIMEOUT", "DEFAULT_USER_AGENT"]

log = logging.getLogger(__name__)

#: Short on purpose. This runs once per address per acquire, in the path of a
#: report that is waiting to start, and an operator watching a run would rather
#: be told "the address is slow" quickly than wait out a generous timeout for
#: every dead exit in the file.
DEFAULT_TIMEOUT = 15.0

DEFAULT_USER_AGENT = "insta-report/2 (+proxy probe)"


#: Re-exported rather than reimplemented. There was a second copy of this
#: logic here once, hand-rolling its own reserved-range table, and it drifted:
#: it covered the RFC 2544 benchmarking range and the shared table did not, so
#: ``fetch`` and ``ProxyPool`` disagreed about whether an address was real. The
#: pool's copy is the one with tests, the one the health accounting uses, and
#: the one that must win. One answer to "is this address real", in one place.
parse_egress = parse_ip_echo


def fetch(
    url: str,
    proxy: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    client: Any = None,
) -> ProbeResult:
    """Probe one address, through one proxy, and grade what came back.

    ``client`` exists for tests and for a caller that wants connection reuse;
    when omitted a client is built per call and closed, because a probe is a
    one-shot and a pooled client would outlive the run id it was opened under.

    Every transport-level failure is returned as a verdict rather than raised.
    A pool that raised out of ``fetch`` would have to decide at the call site
    which failures mean "try the next address" and which mean "stop", and that
    decision is the pool's, made in one place, over a full set of verdicts.
    """
    owns_client = client is None
    http = client or httpx.Client(
        proxy=proxy,
        timeout=timeout,
        follow_redirects=True,
    )
    started = time.monotonic()
    try:
        # Set per request, not only on a client we built. An earlier version set
        # it as a client header, which meant ``user_agent`` was silently ignored
        # whenever a caller injected a client -- so the parameter promised
        # something it did not do, and the only callers that injected one were
        # the tests, which is how it survived.
        response = http.get(url, headers={"User-Agent": user_agent})
    except httpx.TimeoutException as exc:
        return ProbeResult(
            verdict=ProbeVerdict.TIMEOUT,
            detail=f"the address did not answer within {timeout:.0f}s ({type(exc).__name__})",
            elapsed=time.monotonic() - started,
        )
    except httpx.ProxyError as exc:
        # Distinct from a connect error: the *proxy* refused, which is the
        # provider's answer about credentials or quota rather than the
        # destination being unreachable. Same remedy, different explanation,
        # and an operator debugging a provider needs the difference.
        return ProbeResult(
            verdict=ProbeVerdict.AUTH_FAILED,
            detail=f"the proxy refused the request ({type(exc).__name__})",
            elapsed=time.monotonic() - started,
        )
    except httpx.ConnectError as exc:
        return ProbeResult(
            verdict=ProbeVerdict.CONNECT_ERROR,
            detail=f"the address could not be reached ({type(exc).__name__})",
            elapsed=time.monotonic() - started,
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            verdict=ProbeVerdict.CONNECT_ERROR,
            detail=f"transport failure: {type(exc).__name__}: {exc}",
            elapsed=time.monotonic() - started,
        )
    finally:
        if owns_client:
            http.close()

    body = response.text[:64 * 1024]
    verdict, detail = classify_body(
        body, response.status_code, content_type=response.headers.get("content-type")
    )
    egress = parse_egress(body) if verdict is ProbeVerdict.OK else None
    if verdict is ProbeVerdict.OK and egress is None:
        # The body looked fine but said nothing about the exit. Downgraded
        # rather than passed through: an unidentified address is not a working
        # one, and letting OK through is precisely the hole the egress
        # requirement was cut to close.
        #
        # Reachable, and not defensive: ``classify_body`` finds an address by
        # matching ``\b(?:\d{1,3}\.){3}\d{1,3}\b`` against the raw text, which
        # ``999.1.1.1`` satisfies; ``parse_egress`` has to turn that text into
        # an ``ipaddress`` object, which rejects it. Anything that matches the
        # pattern but is not an address is an OK the pool must not bind to.
        verdict = ProbeVerdict.UNPARSEABLE
        detail = (
            "the response did not identify the exit address, so this address "
            "cannot be shown to be a distinct one"
        )
    return ProbeResult(
        verdict=verdict,
        status=response.status_code,
        body=body,
        detail=detail,
        egress=egress,
        elapsed=time.monotonic() - started,
    )
