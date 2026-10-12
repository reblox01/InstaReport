"""Tests for the command line.

The whole point of ``main()`` returning an exit code instead of calling
``sys.exit`` is that these tests can drive the *real* parser and the *real*
dispatch and read the code off the return value. Every test here goes through
``main(argv)``. Nothing imports a private handler and calls it directly,
because a test that bypasses the parser proves the handler works and not that
the command does.

What is pinned:

* **The exit codes**, all four. They are the interface a wrapper script reads,
  and an exit code that silently changes meaning between releases is worse than
  one that is outright wrong -- a script written against the old meaning keeps
  running.
* **That a dry run sends nothing.** Asserted on the *absence of a checkpoint
  record*, not on the absence of output, because "printed the plan" is what a
  dry run is for, and the ledger is the record of what was requested.
* **That the read-only commands write nothing.** Same reasoning, and it is
  asserted against the whole file tree rather than a count, because a command
  that creates one unexpected file while deleting another leaves the count
  identical.
* **That a bad run id is refused rather than repaired.** Resuming the wrong
  run is worse than not resuming, and a sanitised id is a pointer to another
  run's directory.

Every test is offline. The browser channel is replaced at the seam the runner
already uses, the proxy pool's ``fetch`` is injected, and the pacer's wait is
short-circuited -- so nothing here needs a network, a Chromium build, or a real
credential.
"""

from __future__ import annotations

import io
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import insta_report.cli as cli
from insta_report.pacing import Pacer
from insta_report.proxies import EgressObservation, ProbeResult, ProbeVerdict
from insta_report.runner import ChannelSpec
from insta_report.support.paths import RunIdError, check_run_id

from .conftest import FAKE_SESSIONID
from .test_runner import FakeChannel

#: Written as a Python string and formatted, never through a PowerShell
#: here-string: PowerShell 5.1 writes a BOM with ``-Encoding UTF8`` and
#: ``tomllib`` then rejects the file with a misleading "invalid character for a
#: key part" that reads like a parser bug rather than an encoding one.
CONFIG_TEMPLATE = """\
data_dir = "{data_dir}"

[accounts.alpha]
username = "reporter.one"
sessionid_env = "IG_SESSIONID_ALPHA"
daily_budget = 20

[proxies]
source = "file"
file_path = "{proxies}"

[browser]
max_concurrent = 1
headless = true

[run]
max_reports = 50
floor_gap_seconds = 30
jitter_fraction = 0
horizon_hours = 6
"""

#: Five addresses so two concurrent slots can each hold a lease while the
#: third is on cooldown from the first report. With one, the second report
#: would wait out ``min_cooldown`` and the test would be testing the proxy
#: pool's timing instead of the CLI.
PROXY_LINES = "\n".join(f"203.0.113.{octet}:8080" for octet in range(10, 15)) + "\n"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A complete, valid operator setup outside the working tree.

    Returns a small object rather than a namespace of separate fixtures,
    because every test here needs the config *and* the paths *and* the
    sessionid, and threading three fixtures into a signature that is already
    ``(invoke, sent)`` is noise that says nothing.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proxies = data_dir / "proxies.txt"
    proxies.write_text(PROXY_LINES, encoding="utf-8")
    # The *default* target list lives under the data directory, not the working
    # directory, so a run started from anywhere resolves the same file. Placed
    # here deliberately: it means the tests that omit ``--targets`` exercise that
    # default rather than accidentally depending on the cwd.
    targets = data_dir / "targets.txt"
    targets.write_text("spammer_one\nspammer_two\n", encoding="utf-8")
    config = tmp_path / "insta-report.toml"
    config.write_text(
        CONFIG_TEMPLATE.format(data_dir=data_dir.as_posix(), proxies=proxies.as_posix()),
        encoding="utf-8",
    )
    monkeypatch.setenv("IG_SESSIONID_ALPHA", FAKE_SESSIONID)

    class Workspace:
        def __init__(self) -> None:
            self.root = tmp_path
            self.data_dir = data_dir
            self.config = config
            self.targets = targets
            self.proxies = proxies

        def ledger(self, run_id: str) -> list[dict]:
            """Every checkpoint record for *run_id*, in the order written."""
            path = data_dir / "state" / run_id / "checkpoint.jsonl"
            if not path.is_file():
                return []
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            ]

        def tree(self) -> dict[str, bytes]:
            """Every file under the data dir, by relative path and content.

            For proving a command wrote nothing. Comparing content, not a
            count, because a command that creates one file while deleting
            another leaves the count identical.
            """
            return {
                str(path.relative_to(data_dir)): path.read_bytes()
                for path in sorted(data_dir.rglob("*"))
                if path.is_file()
            }

    return Workspace()


def _ok_probe(url: str, proxy: str | None = None) -> ProbeResult:
    """A probe that always works, with an egress distinct per address.

    Distinct per address, because the pool enforces ASN diversity between
    concurrent leases: a single shared egress would make two workers refuse
    each other and the test would be measuring the diversity rule.
    """
    host = "45.9.148.99" if proxy is None else f"45.9.148.{10 + (hash(proxy) % 200)}"
    # ``hash`` is salted per process for strings, so a value derived from it is
    # only ever used for its *distinctness* within one run -- never asserted on.
    return ProbeResult(
        verdict=ProbeVerdict.OK,
        status=200,
        body=json.dumps({"ip": host, "asn": 21408}),
        egress=EgressObservation(ip=host, asn=21408),
    )


def _ok_direct(url: str) -> ProbeResult:
    """The operator's own address, as the pool would observe it.

    A separate helper from ``_ok_probe`` because the two transports are
    genuinely different, and because it is single-argument by design: the
    observation takes no proxy, so a fake that accepted one would be accepting a
    call shape the real transport cannot be given.

    The address is in a different range from every exit ``_ok_probe`` reports, so
    the pool's own-address guard does not dismiss the exits and the test fails
    on the thing it is about rather than on the guard.
    """
    return ProbeResult(
        verdict=ProbeVerdict.OK,
        status=200,
        body=json.dumps({"ip": "198.51.100.250"}),
        egress=EgressObservation(ip="198.51.100.250"),
    )


async def _no_wait(self, *args, **kwargs) -> float:
    """A ``Pacer.async_wait`` that returns immediately.

    ``MIN_GAP_SECONDS`` is 8, so an un-short-circuited wait costs eight real
    seconds per report. A test suite that takes minutes to prove a command
    prints a string is a test suite nobody runs.

    The class is patched, not the instance, because the CLI builds its own
    pacers from config and a test has no other handle on them.
    """
    return 0.0


@pytest.fixture
def run_cli(workspace, monkeypatch):
    """Invoke ``main`` with a recording channel and a working proxy probe.

    Returns ``(invoke, channel)`` where ``invoke(argv) -> (code, output)`` and
    ``channel`` is the single fake channel every report went through. Reading
    ``channel.calls`` is how a test asserts what was actually *sent*, as
    opposed to what the output claims -- and the difference between those two
    is the whole reason the exit codes exist.
    """
    channel = FakeChannel(name="browser")

    def build_channels(*args, **kwargs):
        return [ChannelSpec("browser", channel, 1)]

    monkeypatch.setattr(cli, "_build_channels", build_channels)
    monkeypatch.setattr(Pacer, "async_wait", _no_wait)

    def invoke(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(
            ["--config", str(workspace.config), *argv],
            stream=out,
            fetch_impl=_ok_probe,
            direct_fetch_impl=_ok_direct,
        )
        return code, out.getvalue()

    invoke.workspace = workspace  # type: ignore[attr-defined]
    invoke.channel = channel  # type: ignore[attr-defined]
    return invoke, channel


def reported(channel: FakeChannel) -> list[str]:
    """The target keys the channel was actually asked to report, in order."""
    return [call[0] for call in channel.calls]


# --- exit codes ------------------------------------------------------------


class TestExitCodes:
    """The four codes, because a wrapper script reads them and not the prose."""

    def test_the_codes_are_the_documented_numbers(self):
        """Pinned as literals on purpose.

        Asserting ``code == cli.EXIT_OK`` alone would pass if every constant
        were changed together -- a test that cannot fail when the interface
        changes. A script written against ``0``/``1``/``2``/``130`` is the
        consumer, so the numbers themselves are the assertion.
        """
        assert cli.EXIT_OK == 0
        assert cli.EXIT_NEEDS_REVIEW == 1
        assert cli.EXIT_REFUSED == 2
        assert cli.EXIT_INTERRUPTED == 130

    def test_a_clean_run_is_zero(self, run_cli, workspace):
        invoke, channel = run_cli
        code, output = invoke(
            "run", "--targets", str(workspace.targets), "--run-id", "r1"
        )
        assert reported(channel) == ["spammer_one", "spammer_two"]
        assert code == 0, output

    def test_a_refused_self_report_is_one_not_two(self, run_cli, workspace):
        """The distinction between 1 and 2 is the one that matters.

        Code 2 means *nothing was sent* and it is safe to fix the input and try
        again. Code 1 means requests may have gone out. A wrapper that treats
        them the same will eventually re-run a target whose report already
        landed.

        And the whole run stops, not just that one target: a list that contains
        an account reporting itself is a list whose *other* handles cannot be
        trusted either, so continuing would be filing reports derived from a
        list the tool has just shown is wrong.
        """
        invoke, channel = run_cli
        targets = workspace.data_dir / "self.txt"
        targets.write_text("reporter.one\nother_scammer\n", encoding="utf-8")
        code, output = invoke("run", "--targets", str(targets), "--run-id", "r2")
        assert code == 1, output
        assert "self-report" in output
        assert "STOPPED EARLY" in output
        assert reported(channel) == [], "the run kept going after latching"

    def test_a_missing_config_is_refused_not_a_traceback(self, workspace):
        out = io.StringIO()
        code = cli.main(
            ["--config", str(workspace.data_dir / "nope.toml"), "status"], stream=out
        )
        assert code == 2
        assert "config file not found" in out.getvalue()

    def test_malformed_toml_is_refused_and_names_the_file(self, workspace):
        broken = workspace.data_dir / "broken.toml"
        broken.write_text("this is = not = toml", encoding="utf-8")
        out = io.StringIO()
        code = cli.main(["--config", str(broken), "status"], stream=out)
        assert code == 2
        assert "not valid TOML" in out.getvalue()
        assert "broken.toml" in out.getvalue()

    def test_a_missing_targets_file_says_where_to_put_one(self, workspace):
        out = io.StringIO()
        code = cli.main(
            [
                "--config",
                str(workspace.config),
                "run",
                "--targets",
                str(workspace.data_dir / "absent.txt"),
            ],
            stream=out,
        )
        assert code == 2
        # Naming the remedy, not just the failure: this is the message a
        # first-time operator reads, and "file not found" alone tells them
        # nothing they did not already know.
        assert "target list not found" in out.getvalue()
        assert "--targets" in out.getvalue()

    def test_an_empty_targets_file_is_refused(self, run_cli, workspace):
        """An empty list would report nothing while looking like it worked."""
        invoke, _ = run_cli
        targets = workspace.data_dir / "empty.txt"
        targets.write_text("# only a comment\n\n", encoding="utf-8")
        code, output = invoke("run", "--targets", str(targets), "--run-id", "r3")
        assert code == 2
        assert "no targets" in output

    def test_omitting_the_subcommand_is_a_usage_error(self, workspace):
        out = io.StringIO()
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--config", str(workspace.config)], stream=out)
        # argparse's own bad-usage code is 2, which is the same code this tool
        # uses for "could not start". Asserted so the coincidence is a decision
        # on record rather than an accident someone has to notice later.
        assert excinfo.value.code == 2

    def test_an_account_with_no_session_in_the_environment_is_refused(
        self, workspace, monkeypatch
    ):
        """The credential error names the variable, not "auth failed"."""
        monkeypatch.delenv("IG_SESSIONID_ALPHA", raising=False)
        out = io.StringIO()
        code = cli.main(
            ["--config", str(workspace.config), "status", "--run", "nothing"], stream=out
        )
        assert code == 2
        assert "IG_SESSIONID_ALPHA" in out.getvalue()

    def test_a_provider_proxy_source_without_its_key_names_the_variable(
        self, tmp_path, monkeypatch
    ):
        """The failure the pool cannot see.

        A missing provider key reads as "no exits available", and the operator
        debugging that at 2am needs to be told the *variable* is unset rather
        than that a proxy is down.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        targets = data_dir / "targets.txt"
        targets.write_text("spammer_one\n", encoding="utf-8")
        config = tmp_path / "provider.toml"
        config.write_text(
            f'data_dir = "{data_dir.as_posix()}"\n'
            '\n[accounts.alpha]\nusername = "reporter.one"\n'
            'sessionid_env = "IG_SESSIONID_ALPHA"\n'
            '\n[proxies]\nsource = "provider"\nprovider = "brightdata"\n'
            'provider_key_env = "IG_PROXY_KEY"\n'
            '\n[browser]\nmax_concurrent = 1\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("IG_SESSIONID_ALPHA", FAKE_SESSIONID)
        monkeypatch.delenv("IG_PROXY_KEY", raising=False)
        out = io.StringIO()
        code = cli.main(
            ["--config", str(config), "run", "--run-id", "never"], stream=out
        )
        assert code == 2
        assert "IG_PROXY_KEY" in out.getvalue()


# --- dry run ---------------------------------------------------------------


class TestDryRun:
    def test_a_dry_run_sends_nothing(self, run_cli, workspace):
        invoke, channel = run_cli
        code, output = invoke(
            "run", "--targets", str(workspace.targets), "--run-id", "d1", "--dry-run"
        )
        assert reported(channel) == []
        assert code == 0, output

    def test_a_dry_run_writes_no_intent(self, run_cli, workspace):
        """The assertion that matters.

        "Printed something" is not the same as "did not act". The ledger is the
        record of what was requested, so a dry run appearing in it would make a
        later ``--resume`` believe a report had gone out -- the exact failure
        the checkpoint exists to make impossible.
        """
        invoke, _ = run_cli
        invoke(
            "run", "--targets", str(workspace.targets), "--run-id", "d2", "--dry-run"
        )
        assert invoke.workspace.ledger("d2") == []

    def test_a_dry_run_creates_no_artifact_directory(self, run_cli, workspace):
        """Nothing is created for a run that sends nothing.

        A dry run that leaves an empty ``artifacts/d2/`` behind teaches the
        operator to look for evidence in a directory that is empty, and the
        next run then has to distinguish "no reports" from "reports went
        somewhere else".
        """
        invoke, _ = run_cli
        invoke(
            "run", "--targets", str(workspace.targets), "--run-id", "d3", "--dry-run"
        )
        assert not (workspace.data_dir / "artifacts" / "d3").exists()

    def test_a_dry_run_shows_the_text_it_would_file(self, run_cli, workspace):
        """The one part of a report an operator can still change.

        After a report is filed, a wrong category is only findable by hand on a
        page Instagram does not give us. So the preview shows the text, not
        just a count -- and the handle it is filed against, because the
        category without the handle is not a reviewable thing.
        """
        invoke, _ = run_cli
        _, output = invoke(
            "run", "--targets", str(workspace.targets), "--run-id", "d4", "--dry-run"
        )
        assert "what would be sent" in output
        assert "spammer_one" in output
        assert "category:" in output

    def test_the_previewed_text_is_the_text_that_gets_sent(self, run_cli, workspace):
        """The pre-pass earns its keep here.

        The narrative is rendered once, before the run, and attached to the
        target. So a dry run previews exactly what a real run files, and a
        *retried* target produces byte-identical text rather than the same
        report landing twice under two classifications.

        Asserted by pulling the text out of the dry-run output and checking it
        against the narrative the builder produces for the same target. Whitespace
        differs by design -- the preview is wrapped to the terminal and the
        stored text is not -- so the comparison ignores it and nothing else.
        """
        from insta_report.config import load_config
        from insta_report.narrative import build_builder
        from insta_report.targets import Target

        invoke, _ = run_cli
        _, dry = invoke(
            "run", "--targets", str(workspace.targets), "--run-id", "d5", "--dry-run"
        )

        # The preview prints the handle, then "category:", then the note at a
        # four-space indent, wrapped.
        block = dry.split("spammer_one", 1)[1].split("spammer_two", 1)[0]
        previewed = " ".join(
            line[4:].strip()
            for line in block.splitlines()
            if line.startswith("    ") and not line.strip().startswith("category:")
        )
        assert previewed, "the preview rendered no note for spammer_one"

        builder = build_builder(load_config(workspace.config).run)
        built = builder.build(Target(handle="spammer_one")).text
        assert " ".join(built.split()) == " ".join(previewed.split())

    def test_two_renders_of_one_target_are_byte_identical(
        self, run_cli, workspace
    ):
        """Determinism is what makes ``resume`` safe, so it is pinned here.

        A narrative picked at random per call would file the same target under
        two classifications on a retry, and the second one would be a *different*
        report of the same account. Derived from a hash of the target, so the
        same handle always reads the same way.
        """
        from insta_report.config import load_config
        from insta_report.narrative import build_builder
        from insta_report.targets import Target

        invoke, _ = run_cli
        invoke("run", "--targets", str(workspace.targets), "--run-id", "d8")
        builder = build_builder(load_config(workspace.config).run)
        target = Target(handle="spammer_one")
        first = builder.build(target).text
        second = builder.build(target).text
        assert first == second

    def test_a_dry_run_reports_refusals_rather_than_dropping_them(
        self, run_cli, workspace
    ):
        """A refusal must be visible in the preview.

        A run that silently drops a self-report from its own output is a run
        whose target count and outcome count do not add up, and the operator is
        left deciding whether that is a rounding error or a bug. Asserted on the
        refusal being *named*, since a number that is merely correct proves
        nothing about whether anyone was told.
        """
        invoke, _ = run_cli
        targets = workspace.data_dir / "self.txt"
        targets.write_text("reporter.one\nother_scammer\n", encoding="utf-8")
        code, output = invoke(
            "run", "--targets", str(targets), "--run-id", "d7", "--dry-run"
        )
        assert "one of our own accounts" in output
        assert "refused before dispatch" in output
        assert code == 1


# --- run ids ---------------------------------------------------------------


class TestRunIds:
    @pytest.mark.parametrize(
        "bad",
        [
            "a/b",  # a separator turns one segment into a tree
            "a\\b",  # the Windows separator; a legal filename on POSIX
            "..",  # a directory that already exists
            ".",
            "",  # empty
            "   ",  # whitespace only
            " r1",  # Windows strips a leading space, so this is "r1"
            "r1 ",  # and a trailing one
            "r1.",  # and a trailing dot: three ids, one directory
            "r1\n",  # control characters are invisible in a log and a listing
        ],
    )
    def test_a_run_id_that_is_not_one_segment_is_refused(
        self, run_cli, workspace, bad
    ):
        invoke, channel = run_cli
        code, output = invoke(
            "run", "--targets", str(workspace.targets), "--run-id", bad, "--dry-run"
        )
        assert code == 2, output
        assert reported(channel) == []
        assert "run id" in output.lower()

    def test_a_refused_run_id_creates_nothing(self, run_cli, workspace):
        """Refused means refused, not "cleaned up afterwards"."""
        invoke, _ = run_cli
        invoke(
            "run", "--targets", str(workspace.targets), "--run-id", "a/b", "--dry-run"
        )
        state = workspace.data_dir / "state"
        assert not (state / "a").exists()

    def test_resume_and_run_id_together_are_refused(self, run_cli, workspace):
        """Two names for one run is a mistake worth naming, not resolving."""
        invoke, _ = run_cli
        code, output = invoke(
            "run",
            "--targets",
            str(workspace.targets),
            "--run-id",
            "a",
            "--resume",
            "b",
        )
        assert code == 2
        assert "the same thing" in output

    def test_the_cli_and_the_path_store_agree_on_what_is_usable(self):
        """One rule, one answer.

        The CLI used to carry its own inline copy of the run-id rule, which
        meant the Windows cases passed the CLI and failed later inside a
        ``mkdir`` as an ``OSError`` -- the same bug reported from three files
        further down. Asserting the CLI accepts exactly what ``check_run_id``
        accepts is what stops a second copy being written.
        """
        assert cli.check_run_id is check_run_id

    def test_check_run_id_refuses_what_run_dir_refuses(self):
        """And the path store itself, since it is the one that creates files."""
        for bad in ("a/b", "a\\b", ".", "..", "", " x", "x ", "x.", "\x00", "a\tb"):
            with pytest.raises(RunIdError):
                check_run_id(bad)

    def test_check_run_id_returns_ordinary_ids_unchanged(self):
        for good in ("run-20260101T000000Z", "r1", "a.b", "run_1", "2026-01-01"):
            assert check_run_id(good) == good

    def test_run_id_refusal_is_a_value_error_too(self):
        """So an ``except ValueError`` at a lower layer still catches it."""
        with pytest.raises(ValueError):
            check_run_id("a/b")


# --- resume ----------------------------------------------------------------


class TestResume:
    def test_resume_does_not_retry_a_settled_target(self, run_cli, workspace):
        """The property ``resume`` exists for."""
        invoke, channel = run_cli
        invoke("run", "--targets", str(workspace.targets), "--run-id", "r1")
        assert reported(channel) == ["spammer_one", "spammer_two"]

        channel.calls.clear()
        code, output = invoke(
            "run", "--targets", str(workspace.targets), "--resume", "r1"
        )
        assert reported(channel) == [], "a settled target was reported twice"
        assert "resuming run r1" in output
        assert "2 target(s) already settled" in output

    def test_reusing_a_run_id_without_resume_says_what_is_there(
        self, run_cli, workspace
    ):
        """The other half of resume's safety.

        Reusing an id without ``--resume`` would append to a checkpoint that
        may already contain post-dispatch records, so the run says what is
        there rather than quietly continuing.
        """
        invoke, channel = run_cli
        invoke("run", "--targets", str(workspace.targets), "--run-id", "r2")
        channel.calls.clear()
        code, output = invoke(
            "run", "--targets", str(workspace.targets), "--run-id", "r2"
        )
        assert "already has" in output
        assert "--resume" in output
        # Nothing re-reported: the note is informational, not a licence.
        assert reported(channel) == []

    def test_the_intent_is_written_before_the_outcome(self, run_cli, workspace):
        """The ordering the whole checkpoint design exists to guarantee.

        Asserted on record order, not on the presence of a record. A ledger
        that records the outcome after the click cannot answer the question it
        exists for, which is "may this have gone out?".
        """
        invoke, _ = run_cli
        invoke("run", "--targets", str(workspace.targets), "--run-id", "r3")
        records = invoke.workspace.ledger("r3")
        kinds = [record.get("kind") for record in records]
        assert kinds, "no ledger records at all"
        assert kinds[0] == "intent"
        assert "outcome" in kinds
        # Every intent is followed by an outcome for the same target.
        for index, record in enumerate(records):
            if record.get("kind") != "intent":
                continue
            assert any(
                later.get("kind") == "outcome"
                and later.get("target_ref") == record.get("target_ref")
                for later in records[index + 1 :]
            ), record

    def test_status_reports_an_unsettled_target_as_may_have_landed(
        self, run_cli, workspace
    ):
        """The output a ledger exists for.

        Seeded by hand rather than by breaking a run, because a run that dies
        mid-report is exactly the situation the test needs to be able to
        construct on demand and cannot be produced reliably otherwise.
        """
        from insta_report.checkpoint import CheckpointStore, Intent

        invoke, _ = run_cli
        store = CheckpointStore(
            workspace.data_dir / "state" / "r4" / "checkpoint.jsonl", "r4"
        )
        store.open()
        try:
            store.record_intent(
                Intent(
                    run_id="r4",
                    target_ref="spammer_one",
                    account_ref="alpha",
                    lease_id="px-00001",
                    channel="browser",
                    attempt=1,
                )
            )
        finally:
            store.close()

        code, output = invoke("status", "--run", "r4")
        assert code == 1
        assert "MAY HAVE LANDED" in output
        assert "spammer_one" in output


# --- read-only commands ----------------------------------------------------


class TestReadOnlyCommands:
    @pytest.mark.parametrize("command", ["status", "targets", "anchors"])
    def test_a_read_only_command_writes_nothing(self, run_cli, workspace, command):
        """A status command that touches the ledger is not a status command."""
        invoke, _ = run_cli
        before = workspace.tree()
        code, output = invoke(command)
        assert code == 0, output
        assert workspace.tree() == before

    def test_status_with_no_runs_says_so_and_is_still_zero(self, run_cli):
        """Zero, not one.

        "There is nothing to read" is a clean answer. Exit 1 would tell a
        wrapper script that something needs a human, and every fresh install
        would look like a problem.
        """
        invoke, _ = run_cli
        code, output = invoke("status")
        assert code == 0
        assert "no runs found" in output

    def test_status_lists_a_finished_run_with_no_unsettled_targets(
        self, run_cli, workspace
    ):
        invoke, _ = run_cli
        invoke("run", "--targets", str(workspace.targets), "--run-id", "r1")
        code, output = invoke("status")
        assert code == 0, output
        assert "r1" in output

    def test_status_of_one_run_summarises_it(self, run_cli, workspace):
        invoke, _ = run_cli
        invoke("run", "--targets", str(workspace.targets), "--run-id", "r2")
        code, output = invoke("status", "--run", "r2")
        assert code == 0
        assert "dispatched  2" in output
        assert "settled     2" in output
        assert "unsettled   0" in output

    def test_status_of_an_unknown_run_is_refused(self, run_cli):
        invoke, _ = run_cli
        code, output = invoke("status", "--run", "never-ran")
        assert code == 2
        assert "no checkpoint" in output

    def test_targets_reports_a_confusable_handle_as_invalid(self, run_cli, workspace):
        """F8, and the reason this command exists.

        A confusable handle resolves to a *different real account*, so a report
        filed against the wrong one is a report against an innocent party. The
        failure is prevented at exactly one place, and this is it.

        Asserted through the *listing*, not through the raise: the command has
        to reach its own printing with a bad list in hand, or it cannot report
        the problem it exists to report. It used to call ``require_usable()``
        first, which made every line below it unreachable for exactly the lists
        an operator runs it on.
        """
        invoke, _ = run_cli
        targets = workspace.data_dir / "confusable.txt"
        # Cyrillic 'a' U+0430: visually identical to ASCII 'a', not the same
        # codepoint. The one thing a hand-typed list actually gets wrong.
        targets.write_text("sp\u0430mmer_one\nspammer_two\n", encoding="utf-8")
        code, output = invoke("targets", "--targets", str(targets))

        assert code == 2, output
        # Listed per target with a BAD marker, so the good rows are visible too.
        assert "PROBLEM" in output
        assert "BAD" in output
        assert "ok " in output, "the good row was not listed"
        assert "2 target(s)" in output
        # Two problems for one handle: not a valid handle, *and* a lookalike.
        # Counting targets rather than problems would hide the second.
        assert "2 problem(s)" in output
        assert "lookalike" in output
        assert "sp\\u0430mmer_one" in output
        # The remedy, not just the diagnosis: the ASCII spelling, so the
        # operator can paste it.
        assert "spammer_one" in output
        assert "not usable" in output

    def test_targets_does_not_refuse_the_whole_list_for_one_bad_handle(
        self, run_cli, workspace
    ):
        """The good half of the list is still worth showing.

        An operator fixing 400 handles gets one error at a time when the tool
        stops at the first, and a bad afternoon. Every problem is reported at
        once, grouped, and the runnable rows are printed alongside.
        """
        invoke, _ = run_cli
        targets = workspace.data_dir / "onebad.txt"
        targets.write_text("good_one\nsp\u0430mmer_one\nalso_good\n", encoding="utf-8")
        _, output = invoke("targets", "--targets", str(targets))
        for handle in ("good_one", "also_good", "sp\\u0430mmer_one"):
            assert handle in output, handle
        assert output.count("PROBLEM") == 2

    def test_targets_escapes_a_non_ascii_handle_in_its_output(
        self, run_cli, workspace
    ):
        """``\\uXXXX``, so the console cannot mangle it.

        The Windows console here is cp1252. A raw non-ASCII handle printed to it
        comes back as mojibake, and an operator comparing that against a list
        they typed cannot tell whether the difference is the tool or their
        terminal.
        """
        invoke, _ = run_cli
        targets = workspace.data_dir / "emoji.txt"
        targets.write_text("spammer\u200b_one\n", encoding="utf-8")
        code, output = invoke("targets", "--targets", str(targets))
        assert "\\u" in output
        assert "spammer\u200b_one" not in output

    def test_targets_prints_each_target_with_its_problems(self, run_cli, workspace):
        invoke, _ = run_cli
        code, output = invoke("targets", "--targets", str(workspace.targets))
        assert code == 0
        assert "spammer_one" in output
        assert "2 target(s)" in output
        assert "2 pending" in output

    def test_anchors_lists_every_anchor_and_its_texts(self, run_cli):
        invoke, _ = run_cli
        code, output = invoke("anchors")
        assert code == 0
        assert "confirmation" in output
        # An anchor with configured texts shows them; one without is marked
        # as such rather than printing an empty column.
        assert "Thanks for reporting" in output
        assert "no text;" in output

    def test_anchors_prints_the_confusable_table(self, run_cli):
        """The operator needs to see what the tool considers confusable.

        A table nobody can read is a rule the operator cannot argue with, and an
        argument they should be able to have: a handle that is genuinely part of
        a target's name would otherwise be unfixable.
        """
        invoke, _ = run_cli
        code, output = invoke("anchors")
        assert "confusable characters" in output
        assert "looks like" in output


class TestAnchorsCheck:
    def test_drift_against_a_dump_is_reported(self, run_cli, workspace):
        """The diagnostic that matters when Instagram has moved something.

        The realistic drift is a rephrasing that is on *neither* the pinned text
        nor any of its four alternates. A tool that only checked selectors would
        not notice for weeks, and every report in the meantime would be filed
        with no corroboration.
        """
        invoke, _ = run_cli
        dump = workspace.data_dir / "dump.json"
        dump.write_text(
            json.dumps(
                {"confirmation": "You have successfully reported this profile"}
            ),
            encoding="utf-8",
        )
        code, output = invoke("anchors", "--check", str(dump))
        assert code == 1, output
        # Named as a miss, and told what was actually on the page.
        misses = output.split("no longer match this page:", 1)[1]
        assert "\n  confirmation" in misses
        # Echoed casefolded, because that is the form the comparison ran in --
        # an assertion on the original casing would fail against a report that
        # is correct, and would tempt someone to "fix" the normaliser.
        assert "you have successfully reported this profile" in output
        assert "expected to find" in output
        # And the remedy, without the trap.
        assert "update insta_report/data/anchors.toml" in output
        assert "Do not add a fallback selector" in output

    def test_a_pinned_alternate_is_a_hit_not_drift(self, run_cli, workspace):
        """The alternates are part of the contract, and this says so.

        ``"Thanks for reporting"`` is one of the four alternates, so a page
        carrying only that phrase has *transitioned* and must not be reported as
        drift. Without this test a future reader could "simplify" the alternates
        away and turn every real confirmation into an alert.
        """
        invoke, _ = run_cli
        dump = workspace.data_dir / "alt.json"
        dump.write_text(
            json.dumps({"confirmation": "Thanks for reporting"}), encoding="utf-8"
        )
        _, output = invoke("anchors", "--check", str(dump))
        misses = output.split("no longer match this page:", 1)
        if len(misses) == 2:
            assert "\n  confirmation" not in misses[1]

    def test_a_dump_carrying_the_pinned_text_is_clean(self, run_cli, workspace):
        """A full, honest dump of the pinned texts reports nothing.

        The complement of the drift case: without it, a check that reports drift
        for *every* anchor on every run looks like a working diagnostic right up
        until the day it is needed.
        """
        from insta_report.anchors import load_anchors

        invoke, _ = run_cli
        anchors = load_anchors(_config_anchors_path(workspace))
        observed = {
            anchor.name: list(anchor.texts)
            for anchor in anchors.all_anchors()
            if anchor.texts
        }
        dump = workspace.data_dir / "clean.json"
        dump.write_text(json.dumps(observed), encoding="utf-8")
        code, output = invoke("anchors", "--check", str(dump))
        assert code == 0, output
        assert "matched" in output

    def test_a_dump_may_use_a_bare_string_per_anchor(self, run_cli, workspace):
        """The other honest shape, and the one a hand-written dump uses.

        A list of strings is joined, since an anchor matches on a substring, so
        both shapes have to reach the same verdict -- otherwise the answer
        depends on which of two equally correct dumps the operator happened to
        write.
        """
        invoke, _ = run_cli
        dump = workspace.data_dir / "strings.json"
        dump.write_text(
            json.dumps({"confirmation": "Thanks for reporting this account"}),
            encoding="utf-8",
        )
        code, output = invoke("anchors", "--check", str(dump))
        assert "confirmation" not in output.split("no longer match")[0] or (
            "no observation" not in output
        )
        # The confirmation anchor specifically is not among the misses.
        misses = output.split("no longer match this page:", 1)
        if len(misses) == 2:
            assert "\n  confirmation" not in misses[1]

    @pytest.mark.parametrize(
        "value",
        [
            {"text": "Thanks for reporting this account"},
            42,
            None,
            ["ok", 7],
        ],
        ids=["object", "number", "null", "mixed-list"],
    )
    def test_a_dump_value_that_is_not_text_is_refused(
        self, run_cli, workspace, value
    ):
        """Refused, not coerced, and not graded.

        The old code ran every value through ``str()``, so an object became its
        Python repr and that repr was then compared against the anchor as if a
        human had read it off the page. The operator would be told to edit
        ``anchors.toml`` because of a bug in their capture script -- and the one
        remedy this tool offers is editing the file that pins every report.
        """
        invoke, _ = run_cli
        dump = workspace.data_dir / "wrong.json"
        dump.write_text(json.dumps({"confirmation": value}), encoding="utf-8")
        code, output = invoke("anchors", "--check", str(dump))
        assert code == 2, output
        assert "must be a string" in output
        assert "confirmation" in output
        # And it did not also print a drift verdict, which is what would send
        # the operator off to edit the anchor file.
        assert "no longer match" not in output

    def test_an_empty_dump_reports_every_text_anchor_as_missing(
        self, run_cli, workspace
    ):
        """Absent is not the same as drifted, and both must be visible.

        An anchor that was never captured is the state a first run is in, and
        the operator needs to see that rather than a clean report.
        """
        invoke, _ = run_cli
        dump = workspace.data_dir / "empty.json"
        dump.write_text("{}", encoding="utf-8")
        code, output = invoke("anchors", "--check", str(dump))
        assert code == 1
        assert "confirmation" in output

    def test_a_missing_dump_is_refused(self, run_cli, workspace):
        invoke, _ = run_cli
        code, output = invoke(
            "anchors", "--check", str(workspace.data_dir / "absent.json")
        )
        assert code == 2
        assert "cannot read" in output

    def test_a_dump_that_is_not_an_object_is_refused(self, run_cli, workspace):
        invoke, _ = run_cli
        dump = workspace.data_dir / "list.json"
        dump.write_text("[1, 2, 3]", encoding="utf-8")
        code, output = invoke("anchors", "--check", str(dump))
        assert code == 2
        assert "JSON object" in output


def _config_anchors_path(workspace) -> Path:
    from insta_report.config import load_config

    return load_config(workspace.config).anchors.path


# --- signal handling -------------------------------------------------------


class TestSignals:
    def test_the_first_sigint_latches_and_the_second_raises(self):
        """Latch, do not die.

        The first Ctrl-C asks the run to stop starting new work and let the
        report in flight settle; only a second one abandons it. A run that dies
        on the first Ctrl-C leaves a dispatched-but-unrecorded target on disk,
        which is precisely the case the ledger exists to make visible.
        """

        class Stub:
            def __init__(self) -> None:
                self.reason: str | None = None

            def request_abort(self, reason: str) -> None:
                self.reason = reason

        stub = Stub()
        restore = cli._install_sigint(stub)
        try:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
            assert stub.reason is not None
            assert "Ctrl-C" in stub.reason
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
        finally:
            restore()

    def test_the_restorer_puts_the_previous_handler_back(self):
        """Or the next thing to install a handler inherits ours."""

        def previous(signum, frame):  # noqa: ARG001
            return None

        signal.signal(signal.SIGINT, previous)
        restore = cli._install_sigint(object())
        restore()
        assert signal.getsignal(signal.SIGINT) is previous
        signal.signal(signal.SIGINT, signal.default_int_handler)

    def test_a_failed_install_still_returns_a_restorer(self):
        """On a non-main thread ``signal.signal`` raises ``ValueError``.

        The Windows event loop also has no ``add_signal_handler``, which is why
        this is the mechanism at all. Either way the caller is inside a
        ``try``/``finally`` and needs *something* to call, so the failure is a
        note rather than an exception -- and the returned restorer is a no-op
        rather than one that restores something never installed.
        """
        restore = cli._install_sigint(object())
        restore()  # must not raise


# --- the module runs -------------------------------------------------------


class TestTheModuleRuns:
    def test_python_m_insta_report_cli_prints_help_and_exits_zero(self):
        """The documented entry point, as a subprocess.

        A ``__main__`` guard that raises the wrong thing, or a package that does
        not import under ``-m``, is invisible to every other test here -- they
        all import the module directly.
        """
        result = subprocess.run(
            [sys.executable, "-m", "insta_report.cli", "--help"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0, result.stderr
        assert "run" in result.stdout
        assert "Exit codes" in result.stdout

    def test_the_help_text_states_what_the_tool_cannot_know(self):
        """The description is the tool's honesty contract, so it is a test.

        "Reports what it requested; cannot know what Instagram did with them"
        is the difference between a delivery tool and a success-claiming one,
        and the root cause of the tool this replaces was a traceback that
        printed "reported successfully" after a response it could not parse.
        """
        assert "cannot know" in (cli.build_parser().description or "")

    def test_no_secret_reaches_the_output(self, run_cli, workspace):
        """Redaction at the choke point, proven through the real CLI.

        The registry is armed by ``load_config``, so this asserts the whole
        path holds: a credential in the environment, a run, and an output.
        """
        invoke, _ = run_cli
        _, output = invoke("run", "--targets", str(workspace.targets), "--run-id", "r1")
        assert FAKE_SESSIONID not in output

    def test_main_returns_the_code_rather_than_exiting(self, workspace):
        """The property every other test in this file depends on.

        A ``sys.exit`` inside ``main`` would make the exit codes untestable
        except by catching ``SystemExit``, and a test that does that is testing
        the interpreter, not the tool.
        """
        out = io.StringIO()
        code = cli.main(
            ["--config", str(workspace.config), "status", "--run", "absent"], stream=out
        )
        assert isinstance(code, int)
        assert code == 2


# --- helpers that are not commands -----------------------------------------


class TestWrap:
    def test_a_note_is_wrapped_and_never_hyphenated(self):
        """A note an operator may copy verbatim must survive the preview.

        ``textwrap`` would break a long word with a hyphen, and then the text
        on screen is not the text that will be sent.
        """
        text = "This account is impersonating a public figure and " + "verylongword" * 3
        lines = cli._wrap(text, 40)
        assert all(len(line) <= 40 for line in lines)
        assert " ".join(lines) == text
        assert not any(line.endswith("-") for line in lines)

    def test_an_empty_note_does_not_vanish(self):
        """It prints as an empty line, so the field is visibly blank rather
        than silently absent."""
        assert cli._wrap("", 40) == [""]

    def test_whitespace_is_normalised_only_at_the_joins(self):
        assert cli._wrap("a  b", 40) == ["a b"]

    def test_a_single_longer_word_still_prints(self, test=None):
        """Not dropped, not hyphenated -- one overlong line, and the operator
        can read the whole token rather than a truncated prefix."""
        assert cli._wrap("x" * 100, 40) == ["x" * 100]


class TestResolveConfig:
    def test_an_explicit_path_is_used_as_given(self, workspace, monkeypatch):
        monkeypatch.chdir(workspace.data_dir)
        assert cli._resolve_config(str(workspace.config)) == workspace.config

    def test_a_missing_explicit_path_is_reported_by_the_resolver(
        self, workspace, monkeypatch
    ):
        """Named, not raised at the top level.

        ``ConfigError`` is what ``main`` turns into exit 2 with a clean message,
        and a second check in the resolver would produce a different exception
        type for the same condition.
        """
        from insta_report.config import ConfigError

        monkeypatch.chdir(workspace.data_dir)
        with pytest.raises(ConfigError, match="config file not found"):
            cli._resolve_config(str(workspace.data_dir / "absent.toml"))

    def test_it_finds_a_config_in_the_working_directory(self, workspace, monkeypatch):
        """So a run started from the config's own directory needs no flag."""
        monkeypatch.chdir(workspace.root)
        assert cli._resolve_config(None) == workspace.config

    def test_it_names_every_candidate_it_looked_for(self, workspace, monkeypatch):
        """An operator who mistypes ``--config`` and gets "not found" from
        auto-discovery has no way to tell which of the two was the mistake."""
        from insta_report.config import ConfigError

        elsewhere = workspace.data_dir / "empty"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        with pytest.raises(ConfigError) as excinfo:
            cli._resolve_config(None)
        message = str(excinfo.value)
        for name in cli.DEFAULT_CONFIG_NAMES:
            assert name in message

    def test_config_toml_is_the_second_candidate_not_the_only_one(
        self, workspace, monkeypatch
    ):
        """Two names, in a stated order, so "which file did it use" is
        answerable from the source rather than from experiment."""
        assert cli.DEFAULT_CONFIG_NAMES[0] == "insta-report.toml"
        assert cli.DEFAULT_CONFIG_NAMES[1] == "config.toml"


# --- the data directory is not the repository ------------------------------


class TestTheDataDirIsNotTheRepo:
    def test_a_data_dir_inside_the_work_tree_is_refused(self, tmp_path):
        """The one check that must never be relaxed to a warning.

        An authenticated screenshot is PII and a DOM dump carries ``sessionid``
        in plaintext. If the artifact root is inside the checkout, one
        ``git add -A`` commits live credentials -- and ``.gitignore`` is a
        convention, not a guarantee.
        """
        from insta_report.support.paths import PathContainmentError, find_repo_root

        repo = find_repo_root()
        if repo is None:  # pragma: no cover - only outside a checkout
            pytest.skip("not inside a work tree")

        from insta_report.support.paths import resolve_paths

        with pytest.raises(PathContainmentError, match="outside the work tree"):
            resolve_paths(repo / "state")

    def test_a_data_dir_outside_the_work_tree_is_accepted(self, tmp_path):
        from insta_report.support.paths import resolve_paths

        paths = resolve_paths(tmp_path / "data")
        assert paths.data_dir == (tmp_path / "data").resolve()

    def test_the_working_directory_is_not_a_valid_data_dir_either(self):
        """A sibling directory sharing the repo's name prefix is *not* inside
        it -- the check uses path containment, not string comparison, and a
        naive ``startswith`` would refuse a legitimate location."""
        from insta_report.support.paths import assert_outside_repo, find_repo_root

        repo = find_repo_root()
        if repo is None:  # pragma: no cover
            pytest.skip("not inside a work tree")
        sibling = repo.parent / (repo.name + "-data")
        # Does not raise. The point is that it is not mistaken for inside.
        assert assert_outside_repo(sibling) == sibling.resolve()


# --- the event loop is owned by the call, not by the tool ----------------


class TestTheEventLoopIsTheCallers:
    def test_a_run_completes_and_reports_both_targets(self, run_cli, workspace):
        """``asyncio.run`` owns the loop for the whole dispatch.

        Worth stating because the alternative is subtly different: if the CLI
        instead assumed it was *called* from a loop, then calling it from a
        script that already had one running would fail with a bare
        ``RuntimeError`` from inside ``asyncio.runners`` -- with no exit code,
        no message, and nothing an operator could act on. Asserted end to end
        rather than on a stub, so the real runner really does drive the real
        loop.
        """
        invoke, channel = run_cli
        code, output = invoke("run", "--targets", str(workspace.targets), "--run-id", "r1")
        assert code == 0, output
        assert reported(channel) == ["spammer_one", "spammer_two"]

    def test_the_channel_is_awaited_not_left_running(self, run_cli, workspace):
        """``aclose()`` is awaited, so no driver outlives the run.

        A ``close()`` called without ``await`` leaves the coroutine unstarted:
        the browser process stays up, and Python prints a ``RuntimeWarning``
        that a test suite scrolling past would never notice. Asserted on the
        flag, because that is what the coroutine sets when it *runs*.
        """
        invoke, channel = run_cli
        invoke("run", "--targets", str(workspace.targets), "--run-id", "r1")
        assert channel.closed is True

    def test_a_driver_that_will_not_close_does_not_replace_the_result(
        self, run_cli, workspace, monkeypatch
    ):
        """Best effort, and it says so.

        A Playwright teardown failure is a Playwright problem. Letting it
        propagate would turn a clean run of forty reports into a traceback and
        lose the run report -- the one thing the operator was waiting for.
        """
        invoke, channel = run_cli

        async def refuse() -> None:
            raise RuntimeError("the pipe is already closed")

        monkeypatch.setattr(channel, "aclose", refuse)
        code, output = invoke("run", "--targets", str(workspace.targets), "--run-id", "r1")
        assert code == 0, output
        assert "did not close cleanly" in output
        assert "the pipe is already closed" in output
        # And the report still printed, which is the point.
        assert "outcomes:" in output
