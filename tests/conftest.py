"""Shared fixtures.

The redaction registry is a module-level singleton by design -- it has to be
reachable from anywhere without threading it through every call site. That makes
it process-global state, so tests clear it explicitly rather than leaking
secrets between cases and producing order-dependent failures.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from insta_report.support.redaction import get_registry

# Long enough to clear MIN_SECRET_LENGTH so register() accepts it.
FAKE_SESSIONID = "fake-sessionid-value-0000000000000001"


@pytest.fixture(autouse=True)
def _clean_registry():
    get_registry().clear()
    yield
    get_registry().clear()


def _is_loopback(address: object) -> bool:
    """Whether *address* is a loopback target, in any of the shapes a socket takes.

    ``connect`` is handed an ``(host, port)`` tuple for AF_INET and a
    ``(host, port, flowinfo, scopeid)`` tuple for AF_INET6, and either may be a
    4-tuple already. Unpacking defensively is cheaper than a test that fails on
    a shape nobody predicted.
    """
    if not isinstance(address, tuple) or not address:
        return False
    try:
        host = str(address[0])
    except Exception:  # pragma: no cover - defensive
        return False
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch: pytest.MonkeyPatch, request):
    """Fail any test that opens a non-loopback socket.

    "The suite is offline" was a claim in the README with nothing behind it, and
    a claim like that is worth exactly nothing until something breaks. This is
    that something: a test that reaches the internet fails here, loudly, instead
    of passing because the network happened to be up and quietly costing CI a
    round trip to a third party.

    Loopback stays open on purpose. asyncio holds a self-pipe socket per event
    loop, and on Windows that pipe is a real AF_INET socket on 127.0.0.1, so
    blocking it breaks every async test in the file for a reason that has
    nothing to do with what they are testing.

    A test that genuinely needs a socket opts out by asking for
    ``network_access``. Nothing does today; the marker exists so that adding one
    is a deliberate, greppable act rather than a quiet deletion of this fixture.
    """
    if request.node.get_closest_marker("network_access"):
        return

    def blocked(self, address, *args, **kwargs):
        if _is_loopback(address):
            return _real_connect(self, address, *args, **kwargs)
        raise AssertionError(
            f"test {request.node.nodeid!r} tried to open a socket to {address!r}. "
            "The suite is meant to be offline: inject a transport (fetch_impl, "
            "httpx.MockTransport, or a patched client factory) instead of letting "
            "a real request escape. If this test genuinely needs the network, mark "
            "it with @pytest.mark.network_access and say why."
        )

    def blocked_ex(self, address, *args, **kwargs):
        if _is_loopback(address):
            return _real_connect_ex(self, address, *args, **kwargs)
        raise AssertionError(
            f"test {request.node.nodeid!r} tried to connect to {address!r}; "
            "the suite is meant to be offline."
        )

    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex
    monkeypatch.setattr(socket.socket, "connect", blocked, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_ex, raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A data directory guaranteed to sit outside the working tree."""
    target = tmp_path / "data"
    target.mkdir()
    return target


@pytest.fixture
def base_config_toml(data_dir: Path) -> str:
    """Minimal valid config body, with secrets supplied via the environment."""
    return f"""
data_dir = "{data_dir.as_posix()}"
anchors_path = "anchors.toml"

[accounts.alpha]
username = "reporter.one"
sessionid_env = "IG_SESSIONID_ALPHA"
daily_budget = 20

[proxies]
source = "file"
file_path = "{data_dir.as_posix()}/proxies.txt"

[browser]
max_concurrent = 1
"""


@pytest.fixture
def session_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IG_SESSIONID_ALPHA", FAKE_SESSIONID)
    return FAKE_SESSIONID


@pytest.fixture
def proxy_file(data_dir: Path) -> Path:
    path = data_dir / "proxies.txt"
    path.write_text("203.0.113.10:8080\n", encoding="utf-8")
    return path


@pytest.fixture
def write_config(tmp_path: Path):
    def _write(body: str, name: str = "config.toml") -> Path:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path

    return _write


#: Config tests rewrite a TOML body many times, and each rewrite needs its own
#: directory because one test deliberately produces two files. These are tracked
#: so the autouse cleanup below can remove them instead of littering %TEMP%.
_SCRATCH_DIRS: list[Path] = []


def scratch_config(body: str, name: str = "config.toml") -> Path:
    """Write *body* to a fresh temp file and return its path.

    A plain function rather than a fixture: fixtures must be injected as
    parameters, and these call sites are ``load_config(_write(body))`` inside
    the assertion. Threading a parameter through every one of them would be
    noise that says nothing.
    """
    directory = Path(tempfile.mkdtemp(prefix="insta-report-cfg-"))
    _SCRATCH_DIRS.append(directory)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clean_scratch():
    yield
    while _SCRATCH_DIRS:
        shutil.rmtree(_SCRATCH_DIRS.pop(), ignore_errors=True)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Strip inherited env that could satisfy a config by accident."""
    for name in list(os.environ):
        if name.startswith("IG_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch
