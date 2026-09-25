"""Shared fixtures.

The redaction registry is a module-level singleton by design -- it has to be
reachable from anywhere without threading it through every call site. That makes
it process-global state, so tests clear it explicitly rather than leaking
secrets between cases and producing order-dependent failures.
"""

from __future__ import annotations

import os
import shutil
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
