"""Crash-consistency matrix.

Every other test in this suite passes with all three delivery channels dead,
because they exercise logic. This one exercises durability: it hard-kills a
real process at each checkpoint write point and then resumes, because the
failure being defended against is not a logic bug but bytes that never reached
the disk.

``os._exit`` is used rather than an exception or a clean return. It skips
interpreter shutdown, skips flushing, and skips every ``finally`` block.

**What this does and does not prove.** Killing a process does not empty the OS
page cache, so this matrix proves durability against a *process* dying -- it
would catch a missing ``flush()``, a lost append, a bad ordering, or state that
only ever lived in memory. It does **not** prove durability against power loss,
which needs ``fsync``. That gap is covered differently:
``test_intent_is_fsynced_before_dispatch`` asserts ``os.fsync`` is actually
invoked on every write, while this matrix asserts the process-kill half. Neither
test alone covers the other, and a real power-loss test is not something a test
process can produce on demand.

The invariant checked everywhere below:

    no target that was dispatched is ever offered for dispatch again
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from insta_report.checkpoint import CheckpointStore
from insta_report.outcomes import TerminalState

REPO = Path(__file__).resolve().parent.parent

#: The five points a run can die at. Each maps to a distinct on-disk state, and
#: each must be recoverable without ever re-offering a dispatched target.
STOP_POINTS = [
    "before_intent",
    "after_intent",
    "after_dispatch_no_outcome",
    "after_outcome",
    "torn_final_line",
]

#: What a resumed run must conclude at each stop point.
EXPECTED = {
    "before_intent": "attemptable",
    "after_intent": "dispatch_unknown",
    "after_dispatch_no_outcome": "dispatch_unknown",
    "after_outcome": "already_settled",
    "torn_final_line": "already_settled",
}

CHILD = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    from insta_report.checkpoint import CheckpointStore, Intent
    from insta_report.outcomes import Outcome, TerminalState, utc_now

    path, stop = Path(sys.argv[1]), sys.argv[2]
    run = "crash-run"

    if stop == "torn_final_line":
        # Simulate the raw bytes a power loss leaves: a valid prefix with no
        # terminating newline. Written directly, bypassing the store.
        with CheckpointStore(path, run) as store:
            store.record_intent(Intent(run_id=run, target_ref="a",
                                       account_ref="alpha", lease_id="l1",
                                       channel="browser"))
            store.record_outcome(Outcome(
                terminal=TerminalState.SUBMITTED_ACKED, target_ref="a",
                channel="browser", account_ref="alpha", lease_id="l1",
                dispatched_at=utc_now(), finished_at=utc_now()))
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{{"kind": "outcome", "terminal": "subm')
        os._exit(9)

    store = CheckpointStore(path, run).open()

    if stop == "before_intent":
        os._exit(9)

    store.record_intent(Intent(run_id=run, target_ref="a", account_ref="alpha",
                               lease_id="l1", channel="browser"))
    if stop == "after_intent":
        os._exit(9)

    # Dispatch happens here. The window between this line and the outcome write
    # is exactly the interval the boundary exists to cover.
    if stop == "after_dispatch_no_outcome":
        os._exit(9)

    store.record_outcome(Outcome(
        terminal=TerminalState.SUBMITTED_ACKED, target_ref="a",
        channel="browser", account_ref="alpha", lease_id="l1",
        dispatched_at=utc_now(), finished_at=utc_now()))
    if stop == "after_outcome":
        os._exit(9)

    os._exit(0)
    """
)


def crash_at(path: Path, stop: str) -> int:
    """Run a child that hard-kills itself at *stop*. Returns its exit code."""
    script = CHILD.format(repo=str(REPO))
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), stop],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode not in (0, 9):
        raise AssertionError(
            f"child failed unexpectedly at {stop}: {result.returncode}\n{result.stderr}"
        )
    return result.returncode


@pytest.mark.parametrize("stop", STOP_POINTS)
def test_matrix_child_survives_its_own_write_points(tmp_path, stop):
    crash_at(tmp_path / "run.jsonl", stop)


@pytest.mark.parametrize("stop", STOP_POINTS)
def test_resume_never_re_offers_a_dispatched_target(tmp_path, stop):
    """The invariant, at every point a run can die."""
    path = tmp_path / "run.jsonl"
    crash_at(path, stop)

    state = CheckpointStore(path, "crash-run").state
    reason = state.skip_reason("a")
    expected = EXPECTED[stop]

    if expected == "attemptable":
        assert reason is None, f"{stop}: nothing was dispatched, must be retryable"
    else:
        assert reason is not None, (
            f"{stop}: target was dispatched but resume would offer it again -- "
            "this is the double-send bug"
        )
        assert reason.value == expected


@pytest.mark.parametrize("stop", STOP_POINTS)
def test_a_fresh_target_is_unaffected_by_the_crash(tmp_path, stop):
    """A crash on one target must not block the rest of the run."""
    path = tmp_path / "run.jsonl"
    crash_at(path, stop)
    state = CheckpointStore(path, "crash-run").state
    assert state.skip_reason("never_touched") is None


def test_reconcile_after_crash_marks_dispatched_targets_unknown(tmp_path):
    """A crash between dispatch and outcome is UNKNOWN, never a retry.

    This is the one that needs a human: Instagram gives a reporter no way to
    confirm a report was recorded, so the honest state is uncertainty.
    """
    path = tmp_path / "run.jsonl"
    crash_at(path, "after_dispatch_no_outcome")

    with CheckpointStore(path, "crash-run") as resumed:
        assert resumed.state.pending == {"a"}
        reconciled = resumed.reconcile_pending()

    assert len(reconciled) == 1
    assert reconciled[0].terminal is TerminalState.UNKNOWN
    assert reconciled[0].needs_human_review is True
    assert reconciled[0].was_dispatched is True
    assert "may have landed" in reconciled[0].detail or "may have" in reconciled[0].detail


def test_reconcile_survives_a_torn_line(tmp_path):
    """Recovery has to work on the state a real crash leaves behind."""
    path = tmp_path / "run.jsonl"
    crash_at(path, "torn_final_line")

    with CheckpointStore(path, "crash-run") as resumed:
        assert resumed.state.settled == {"a"}
        assert resumed.reconcile_pending() == []
        assert resumed.state.counts()["submitted_acked"] == 1


def test_multi_target_crash_mid_run(tmp_path):
    """Several targets dispatched, crash, resume: the earlier ones stay settled.

    The realistic shape of an hours-long run dying at 2am.
    """
    path = tmp_path / "run.jsonl"
    script = textwrap.dedent(
        f"""
        import os, sys
        from pathlib import Path
        sys.path.insert(0, {str(REPO)!r})
        from insta_report.checkpoint import CheckpointStore, Intent
        from insta_report.outcomes import Outcome, TerminalState, utc_now

        store = CheckpointStore(Path(sys.argv[1]), "crash-run").open()
        for name, done in (("a", True), ("b", True), ("c", False), ("d", False)):
            store.record_intent(Intent(run_id="crash-run", target_ref=name,
                                       account_ref="alpha", lease_id="l1",
                                       channel="browser"))
            if done:
                store.record_outcome(Outcome(
                    terminal=TerminalState.SUBMITTED_ACKED, target_ref=name,
                    channel="browser", account_ref="alpha", lease_id="l1",
                    dispatched_at=utc_now(), finished_at=utc_now()))
        os._exit(9)
        """
    )
    subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True, text=True, timeout=120, check=False,
    )

    with CheckpointStore(path, "crash-run") as resumed:
        # a and b settled. c and d dispatched with no outcome.
        assert resumed.state.settled == {"a", "b"}
        assert resumed.state.pending == {"c", "d"}

        reconciled = resumed.reconcile_pending()
        assert {o.target_ref for o in reconciled} == {"c", "d"}
        assert all(o.terminal is TerminalState.UNKNOWN for o in reconciled)

        # Nothing is offerable, and the two never-touched targets still are.
        for name in ("a", "b", "c", "d"):
            assert resumed.state.skip_reason(name) is not None, name
        assert resumed.state.skip_reason("e") is None
        assert resumed.state.skip_reason("f") is None
