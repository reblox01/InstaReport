"""Tests for the offline demo.

The demo is the hackathon's sixty seconds, so its contract is strict:

* **It never touches the network.** Asserted by the conftest socket guard
  that covers the whole suite -- but also asserted positively here, by
  handing the demo a config whose transports would raise if built. The demo
  builds no pool and no channel, so there is nothing to inject into; the
  assertion is that ``demo`` has no parameter for them at all.
* **Its ledger rows name themselves synthetic**, in the key, the refs, and
  the detail -- so no screenshot of a demo ledger can be quoted as a filing.
* **It never claims a filing.** The outcome is ``channel_failed`` with no
  dispatch timestamp, which is the vocabulary's way of saying nothing left.
* **The evidence bundle is self-contained**: no external assets, no
  JavaScript, no network references. A bundle that needs the network to be
  read fails the claim it exists to demonstrate.
* **A bad run id is refused**, through the same rule the ledger uses.

The browser act (``--with-browser``) is environment-dependent -- it needs a
Chromium build -- so it is tested structurally: the rehearsal records a step
and never raises, whether the step is a pass or a skip. The fixture-present
path is exercised by the operator running the demo, which is its purpose.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import insta_report.cli as cli
import insta_report.demo as demo
from insta_report.demo import DEMO_DETAIL, DEMO_TARGET_KEY

from .conftest import FAKE_SESSIONID
from .test_cli import CONFIG_TEMPLATE, PROXY_LINES


@pytest.fixture
def denv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proxies = data_dir / "proxies.txt"
    proxies.write_text(PROXY_LINES, encoding="utf-8")
    config = tmp_path / "insta-report.toml"
    config.write_text(
        CONFIG_TEMPLATE.format(
            data_dir=data_dir.as_posix(), proxies=proxies.as_posix()
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("IG_SESSIONID_ALPHA", FAKE_SESSIONID)

    class Env:
        pass

    env = Env()
    env.data_dir = data_dir  # type: ignore[attr-defined]
    env.config = config  # type: ignore[attr-defined]
    return env


def ledger_records(data_dir: Path, run_id: str) -> list[dict]:
    path = data_dir / "state" / run_id / "checkpoint.jsonl"
    assert path.is_file(), f"the demo wrote no ledger at {path}"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestTheOfflineDemo:
    def test_demo_runs_offline_and_says_so(self, denv):
        out = io.StringIO()
        code = cli.main(
            ["--config", str(denv.config), "demo", "--run-id", "demo-1"],
            stream=out,
        )
        output = out.getvalue()
        assert code == 0, output
        assert "offline" in output
        assert "nothing was sent" in output

    def test_demo_writes_a_synthetic_ledger_that_reads_back(self, denv):
        out = io.StringIO()
        code = cli.main(
            ["--config", str(denv.config), "demo", "--run-id", "demo-2"],
            stream=out,
        )
        assert code == 0, out.getvalue()

        records = ledger_records(denv.data_dir, "demo-2")
        kinds = [record["kind"] for record in records]
        assert kinds[0] == "intent"
        assert "outcome" in kinds

        outcomes = [r for r in records if r["kind"] == "outcome"]
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome["target_ref"] == DEMO_TARGET_KEY
        assert "demo/" in outcome["target_ref"]
        assert outcome["terminal"] == "channel_failed"
        assert outcome["detail"] == DEMO_DETAIL
        assert "synthetic" in outcome["detail"]
        assert outcome["dispatched_at"] is None, (
            "a demo outcome with a dispatch timestamp claims something left; "
            "nothing did"
        )

    def test_demo_rows_cannot_be_mistaken_for_filings(self, denv):
        """Every identifying field says demo, not just the detail."""
        out = io.StringIO()
        cli.main(
            ["--config", str(denv.config), "demo", "--run-id", "demo-3"],
            stream=out,
        )
        records = ledger_records(denv.data_dir, "demo-3")
        for record in records:
            for field in ("account_ref", "lease_id", "channel"):
                assert "demo" in str(record.get(field, "")), (field, record)

    def test_demo_renders_a_self_contained_bundle(self, denv):
        out = io.StringIO()
        code = cli.main(
            ["--config", str(denv.config), "demo", "--run-id", "demo-4"],
            stream=out,
        )
        assert code == 0, out.getvalue()

        bundle = denv.data_dir / "state" / "demo-4" / "demo-evidence.html"
        assert bundle.is_file()
        page = bundle.read_text(encoding="utf-8")
        assert "SYNTHETIC DEMO RECORD" in page
        assert "instagram.com" not in page, (
            "the bundle references the live site; a judge opening it offline "
            "would reach the network the demo claims it never touches"
        )
        assert "<script" not in page.lower(), "the bundle needs no scripting"
        assert "what this does not prove" in page

    def test_demo_has_no_transport_to_inject(self, denv):
        """Structural: the demo builds no pool and no channel.

        Asserted on the signature rather than on behaviour, because a demo
        that merely *happens* not to call the network today is one edit away
        from calling it tomorrow. ``_cmd_demo`` accepts the dispatcher's
        fetch args and ignores them; ``run_demo`` does not declare them at
        all.
        """
        import asyncio
        import inspect

        from insta_report.config import load_config

        assert "fetch_impl" not in inspect.signature(demo.run_demo).parameters
        assert "direct_fetch_impl" not in inspect.signature(
            demo.run_demo
        ).parameters

        # And it runs with the network transports absent entirely.
        config = load_config(denv.config)
        result = asyncio.run(
            demo.run_demo(
                config,
                run_id="demo-sig",
                with_browser=False,
                stream=io.StringIO(),
            )
        )
        assert not result.failed()

    def test_demo_does_not_need_a_proxy_file(self, denv):
        """The seam: the demo builds no pool, so a missing list is nothing.

        A run must still refuse without it -- pinned by
        ``test_missing_proxy_file_is_reported_at_load`` on the default path --
        because a run that cannot lease an exit must fail at load naming the
        file, not later as a misgraded channel failure.
        """
        (denv.data_dir / "proxies.txt").unlink()
        out = io.StringIO()
        code = cli.main(
            ["--config", str(denv.config), "demo", "--run-id", "demo-noproxy"],
            stream=out,
        )
        assert code == 0, out.getvalue()
        assert (denv.data_dir / "state" / "demo-noproxy" / "demo-evidence.html").is_file()

    def test_demo_refuses_a_bad_run_id(self, denv):
        out = io.StringIO()
        code = cli.main(
            ["--config", str(denv.config), "demo", "--run-id", "a/b"],
            stream=out,
        )
        assert code == 2
        assert "run id" in out.getvalue().lower()

    def test_demo_status_reads_the_demo_ledger(self, denv):
        """The demo writes a real ledger, so ``status`` reads it back.

        A demo whose output only exists on the terminal is a performance;
        one whose ledger survives it is a record.
        """
        controls = io.StringIO()
        cli.main(
            ["--config", str(denv.config), "demo", "--run-id", "demo-5"],
            stream=controls,
        )
        out = io.StringIO()
        code = cli.main(
            ["--config", str(denv.config), "status", "--run", "demo-5"],
            stream=out,
        )
        assert code == 0, out.getvalue()
        assert "settled     1" in out.getvalue()
        assert "channel_failed" in out.getvalue()

    def test_the_browser_act_records_a_step_and_never_raises(self, denv):
        """Pass or skip depending on the machine; fail on neither count.

        Chromium may or may not be installed where this runs. What is pinned
        is that the rehearsal path executes end to end -- driver start,
        fixture read, wizard classify, driver close -- and records exactly
        one step for it, whatever the machine allows.
        """
        import asyncio

        from insta_report.config import load_config

        config = load_config(denv.config)
        result = asyncio.run(
            demo.run_demo(
                config,
                run_id="demo-b",
                with_browser=True,
                stream=io.StringIO(),
            )
        )
        rehearsals = [s for s in result.steps if s.name == "fixture rehearsal"]
        assert len(rehearsals) == 1
        assert rehearsals[0].status in ("pass", "skip"), rehearsals[0].detail
        assert not result.failed()
        if rehearsals[0].status == "pass":
            assert result.wizard_path, "a pass with no wizard path proves nothing"
            assert result.browser_used
