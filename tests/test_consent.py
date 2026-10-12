"""Tests for the live-dispatch consent gates.

A run that cannot name the account its operator owns does not start. That is
the project's anti-brigading rule, and it lives in front of the doctor gate
rather than behind it: a refusal that arrives after a thirty-second rehearsal
trains the operator to route around it.

What is pinned:

* **A live run without ``--consent-owned`` is refused (exit 2)**, before any
  channel is built, with nothing dispatched and nothing written.
* **A consent naming a different account than the rehearsal target is
  refused.** Consent for an account the run will not open is consent for
  nothing.
* **Matching is case- and @-insensitive**, because a gate that rejects
  ``@Spammer_One`` for ``spammer_one`` is a nuisance the operator routes
  around -- and a routed-around gate is not a gate.
* **Dry runs need no consent.** They dispatch nothing.
* **``--no-doctor`` does not excuse consent.** Skipping the health check is
  not skipping ownership.
* **``--allow-home-egress`` files without a proxy exit**, loudly. Without it,
  a run with no usable exit refuses to bind rather than going direct quietly.

These tests drive ``cli.main`` directly with their own fixture rather than the
suites' ``run_cli`` fixtures, because those fixtures now supply consent as
part of a valid invocation -- which is exactly what would make a gate test
pass without testing the gate.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import insta_report.cli as cli
from insta_report.pacing import Pacer
from insta_report.runner import ChannelSpec

from .conftest import FAKE_SESSIONID
from .test_cli import CONFIG_TEMPLATE, PROXY_LINES, _no_wait, _ok_direct, _ok_probe
from .test_runner import FakeChannel


@pytest.fixture
def bare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A working setup with an invoke that adds nothing on its own.

    Deliberately *not* the suites' ``run_cli``: that fixture supplies consent,
    so a gate test using it would pass with the gate removed.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proxies = data_dir / "proxies.txt"
    proxies.write_text(PROXY_LINES, encoding="utf-8")
    targets = data_dir / "targets.txt"
    targets.write_text("spammer_one\nspammer_two\n", encoding="utf-8")
    config = tmp_path / "insta-report.toml"
    config.write_text(
        CONFIG_TEMPLATE.format(
            data_dir=data_dir.as_posix(), proxies=proxies.as_posix()
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("IG_SESSIONID_ALPHA", FAKE_SESSIONID)
    channel = FakeChannel(name="browser")

    def build_channels(*args, **kwargs):
        return [ChannelSpec("browser", channel, 1)]

    monkeypatch.setattr(cli, "_build_channels", build_channels)
    monkeypatch.setattr(Pacer, "async_wait", _no_wait)

    def invoke(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(
            ["--config", str(config), *argv],
            stream=out,
            fetch_impl=_ok_probe,
            direct_fetch_impl=_ok_direct,
        )
        return code, out.getvalue()

    invoke.config = config  # type: ignore[attr-defined]
    invoke.data_dir = data_dir  # type: ignore[attr-defined]
    invoke.targets = targets  # type: ignore[attr-defined]
    invoke.channel = channel  # type: ignore[attr-defined]
    return invoke


def ledger(data_dir: Path, run_id: str) -> list[str]:
    path = data_dir / "state" / run_id / "checkpoint.jsonl"
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestConsentOwned:
    def test_a_live_run_without_consent_is_refused(self, bare):
        code, output = bare("run", "--run-id", "c1")
        assert code == 2, output
        assert "--consent-owned" in output
        assert bare.channel.calls == []
        assert ledger(bare.data_dir, "c1") == []

    def test_consent_for_another_account_is_refused(self, bare):
        code, output = bare(
            "run", "--run-id", "c2", "--consent-owned", "someone_else"
        )
        assert code == 2, output
        assert "does not match" in output
        assert "spammer_one" in output
        assert bare.channel.calls == []
        assert ledger(bare.data_dir, "c2") == []

    def test_matching_consent_lets_the_run_proceed(self, bare):
        code, output = bare(
            "run", "--run-id", "c3", "--consent-owned", "spammer_one"
        )
        assert code == 0, output
        assert [call[0] for call in bare.channel.calls] == [
            "spammer_one",
            "spammer_two",
        ]

    def test_matching_ignores_case_and_a_leading_at(self, bare):
        """A gate that rejects ``@Spammer_One`` is a gate operators evade.

        Evaded gates protect nothing, so the normalisation is the property,
        not a convenience.
        """
        code, output = bare(
            "run", "--run-id", "c4", "--consent-owned", "@Spammer_One"
        )
        assert code == 0, output
        assert bare.channel.calls, "the run did not dispatch"

    def test_consent_may_name_an_explicit_probe_target(self, bare):
        code, output = bare(
            "run",
            "--run-id",
            "c5",
            "--probe-target",
            "spammer_two",
            "--consent-owned",
            "spammer_two",
        )
        assert code == 0, output
        assert bare.channel.calls, "the run did not dispatch"

    def test_no_doctor_does_not_excuse_consent(self, bare):
        """Skipping the health check is not skipping ownership."""
        code, output = bare("run", "--run-id", "c6", "--no-doctor")
        assert code == 2, output
        assert "--consent-owned" in output
        assert bare.channel.calls == []

    def test_consent_with_no_doctor_still_runs(self, bare):
        code, output = bare(
            "run",
            "--run-id",
            "c7",
            "--no-doctor",
            "--consent-owned",
            "spammer_one",
        )
        assert code == 0, output
        assert bare.channel.calls, "the run did not dispatch"

    def test_a_dry_run_needs_no_consent(self, bare):
        """It dispatches nothing, so there is nothing to own."""
        code, output = bare("run", "--run-id", "c8", "--dry-run")
        assert code == 0, output
        assert "what would be sent" in output
        assert bare.channel.calls == []


class TestAllowHomeEgress:
    def test_direct_runs_say_so_loudly(self, bare):
        """The flag is a conscious decision, and the output shows it was."""
        code, output = bare(
            "run",
            "--run-id",
            "e1",
            "--consent-owned",
            "spammer_one",
            "--allow-home-egress",
        )
        assert code == 0, output
        assert "--allow-home-egress" in output
        assert "own address" in output
        assert bare.channel.calls, "the run did not dispatch"

    def test_doctor_without_an_exit_does_not_build_a_pool(self, bare):
        """Proved by handing it transports that explode on any use.

        A test asserting "no pool was built" by reading the output would pass
        if the pool were built and silently unused. A transport that raises
        on any call fails the test if anything touches the network path.
        """
        import asyncio

        from insta_report.cli import _doctor
        from insta_report.config import load_config

        config = load_config(bare.config)

        def explosive(*args, **kwargs):
            raise AssertionError("the network path was touched")

        report = asyncio.run(
            _doctor(
                config,
                targets=None,
                probe_target=None,
                submit=False,
                live=True,
                fetch_impl=explosive,
                direct_fetch_impl=explosive,
                allow_home_egress=True,
            )
        )
        assert report is not None
