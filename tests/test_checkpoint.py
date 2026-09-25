"""Checkpoint durability and the dispatch boundary.

The tests here are the ones that earn their keep. Everything else in this suite
can pass while all three delivery channels are dead; these cannot. Each one
kills a run at a specific write point and asserts the thing that must remain
true: no target is ever resubmitted unattended.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from insta_report.checkpoint import (
    CheckpointError,
    CheckpointStore,
    DoubleDispatch,
    Intent,
    OrphanOutcome,
    SkipReason,
)
from insta_report.errors import CheckpointCorrupt
from insta_report.outcomes import Outcome, TerminalState, utc_now

RUN = "test-run"


def make_intent(target: str, *, channel: str = "browser", attempt: int = 1) -> Intent:
    return Intent(
        run_id=RUN,
        target_ref=target,
        account_ref="alpha",
        lease_id="lease-1",
        channel=channel,
        attempt=attempt,
    )


def settled(target: str, terminal: TerminalState) -> Outcome:
    return Outcome(
        terminal=terminal,
        target_ref=target,
        channel="browser",
        account_ref="alpha",
        lease_id="lease-1",
        dispatched_at=utc_now(),
        finished_at=utc_now(),
    )


@pytest.fixture
def store(tmp_path: Path) -> CheckpointStore:
    with CheckpointStore(tmp_path / "run.jsonl", RUN) as handle:
        yield handle


# --- basic durability -------------------------------------------------------


def test_intent_and_outcome_round_trip(tmp_path):
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN) as store:
        store.record_intent(make_intent("victim_a"))
        store.record_outcome(settled("victim_a", TerminalState.SUBMITTED_ACKED))

    with CheckpointStore(path, RUN) as reopened:
        assert reopened.state.outcome_for("victim_a").terminal is TerminalState.SUBMITTED_ACKED
        assert reopened.state.settled == {"victim_a"}


def test_records_are_one_json_object_per_line(tmp_path):
    """A half-written line must be recoverable, so lines must not be nested."""
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN) as store:
        store.record_intent(make_intent("a"))
        store.record_outcome(settled("a", TerminalState.SUBMITTED_ACKED))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["kind"] in {"intent", "outcome"}


def test_intent_is_fsynced_before_dispatch(tmp_path, monkeypatch):
    """The ordering guarantee, asserted rather than assumed.

    If fsync silently stopped being called, a power loss could lose the intent
    and the next run would resend a report that already went out.
    """
    path = tmp_path / "run.jsonl"
    synced: list[int] = []
    real_fsync = os.fsync

    def counting_fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)

    with CheckpointStore(path, RUN) as store:
        before = len(synced)
        store.record_intent(make_intent("a"))
        assert len(synced) > before, "record_intent did not fsync"
        assert path.exists()
        # Durable before this returns, so the caller may now dispatch.
        assert "a" in [json.loads(l)["target_ref"] for l in path.read_text().splitlines()]


# --- the dispatch boundary --------------------------------------------------


def test_settled_target_is_skipped(store):
    store.record_intent(make_intent("a"))
    store.record_outcome(settled("a", TerminalState.SUBMITTED_ACKED))
    assert store.state.skip_reason("a") is SkipReason.ALREADY_SETTLED


def test_dispatched_without_outcome_is_never_retried(store):
    """The middle case: may have landed, so it must not be attempted again."""
    store.record_intent(make_intent("a"))
    assert store.state.skip_reason("a") is SkipReason.DISPATCH_UNKNOWN


def test_untouched_target_is_attemptable(store):
    assert store.state.skip_reason("fresh") is None


def test_second_outcome_is_refused(store):
    """Writing twice means the boundary failed upstream. Stop, do not append."""
    store.record_intent(make_intent("a"))
    store.record_outcome(settled("a", TerminalState.SUBMITTED_ACKED))
    with pytest.raises(DoubleDispatch):
        store.record_outcome(settled("a", TerminalState.SUBMITTED_UNCONFIRMED))


def test_second_intent_for_a_settled_target_is_refused(store):
    store.record_intent(make_intent("a"))
    store.record_outcome(settled("a", TerminalState.SUBMITTED_ACKED))
    with pytest.raises(DoubleDispatch):
        store.record_intent(make_intent("a", attempt=2))


def test_outcome_without_intent_is_refused(store):
    """Outcomes are only legal after an intent; the reverse is corruption."""
    with pytest.raises(OrphanOutcome):
        store.record_outcome(settled("ghost", TerminalState.SUBMITTED_ACKED))


def test_failed_write_leaves_no_phantom_state(store, monkeypatch):
    """If it is not on disk, it did not happen.

    Memory and disk must not diverge, or a later resume would believe a report
    was attempted when the record was never written.
    """
    store.record_intent(make_intent("a"))
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        store.record_intent(make_intent("b"))
    assert store.state.has_intent("b") is False
    assert "b" not in store.state.dispatched


# --- resume -----------------------------------------------------------------


def test_reconcile_marks_pending_as_unknown_not_retryable(tmp_path):
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN) as store:
        store.record_intent(make_intent("a"))
        store.record_intent(make_intent("b"))
        store.record_outcome(settled("b", TerminalState.SUBMITTED_ACKED))

    with CheckpointStore(path, RUN) as resumed:
        pending_before = resumed.state.pending
        assert pending_before == {"a"}

        reconciled = resumed.reconcile_pending()
        assert len(reconciled) == 1
        assert reconciled[0].terminal is TerminalState.UNKNOWN
        assert reconciled[0].needs_human_review is True
        assert resumed.state.pending == set()
        assert resumed.state.skip_reason("a") is SkipReason.ALREADY_SETTLED


def test_reconcile_is_idempotent(tmp_path):
    """Running it twice must not double-count or raise."""
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN) as store:
        store.record_intent(make_intent("a"))
        store.record_intent(make_intent("b"))
        store.record_outcome(settled("b", TerminalState.SUBMITTED_ACKED))

    with CheckpointStore(path, RUN) as first:
        first.reconcile_pending()
    with CheckpointStore(path, RUN) as second:
        assert second.reconcile_pending() == [], "second pass had nothing to do"
        # Two outcomes: b's ack, plus the UNKNOWN a was promoted to on the
        # first pass. The second pass must not add a third.
        assert len(second.state) == 2
        assert second.state.counts()["unknown"] == 1


def test_resume_never_offers_a_dispatched_target_again(tmp_path):
    """The single invariant the whole store exists to hold."""
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN) as store:
        for name in ("a", "b", "c"):
            store.record_intent(make_intent(name))
        store.record_outcome(settled("b", TerminalState.SUBMITTED_ACKED))

    with CheckpointStore(path, RUN) as resumed:
        resumed.reconcile_pending()
        for name in ("a", "b", "c"):
            assert resumed.state.skip_reason(name) is not None, name


def test_counts_tally_every_terminal_state(tmp_path):
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN) as store:
        for name in ("a", "b", "c"):
            store.record_intent(make_intent(name))
        store.record_outcome(settled("a", TerminalState.SUBMITTED_ACKED))
        store.record_outcome(settled("b", TerminalState.UNKNOWN))
        store.record_outcome(settled("c", TerminalState.NOT_REPORTABLE))

    tally = CheckpointStore(path, RUN).state.counts()
    assert tally["submitted_acked"] == 1
    assert tally["unknown"] == 1
    assert tally["not_reportable"] == 1
    assert tally["channel_failed"] == 0


# --- corruption -------------------------------------------------------------


def test_torn_final_line_is_tolerated_and_reported(tmp_path, caplog):
    """A crash mid-write is expected. Refusing to start would resend everything."""
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN) as store:
        store.record_intent(make_intent("a"))
        store.record_outcome(settled("a", TerminalState.SUBMITTED_ACKED))

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "outcome", "terminal": "subm')  # torn

    with CheckpointStore(path, RUN) as resumed:
        assert resumed.state.settled == {"a"}
        assert any("torn write" in r.message for r in caplog.records)


def test_corruption_in_the_middle_is_fatal(tmp_path):
    """A hole mid-file means lost state, and continuing risks double-sends."""
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN) as store:
        store.record_intent(make_intent("a"))

    with path.open("a", encoding="utf-8") as handle:
        handle.write("garbage that is not json\n")
        handle.write(json.dumps({"kind": "intent", "run_id": RUN}) + "\n")

    with pytest.raises((CheckpointCorrupt, CheckpointError, KeyError)):
        CheckpointStore(path, RUN).open()


def test_unknown_record_kind_is_rejected(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(json.dumps({"kind": "telepathy"}) + "\n", encoding="utf-8")
    with pytest.raises(CheckpointCorrupt):
        CheckpointStore(path, RUN).open()


def test_run_id_mismatch_is_refused(tmp_path):
    with CheckpointStore(tmp_path / "run.jsonl", RUN) as store:
        wrong = Intent(
            run_id="other-run",
            target_ref="a",
            account_ref="alpha",
            lease_id="lease-1",
            channel="browser",
        )
        with pytest.raises(CheckpointError, match="does not match"):
            store.record_intent(wrong)


def test_writing_while_closed_is_refused(tmp_path):
    store = CheckpointStore(tmp_path / "run.jsonl", RUN)
    with pytest.raises(CheckpointError, match="not open"):
        store.record_intent(make_intent("a"))


# --- single-writer lock -----------------------------------------------------


def test_second_process_is_refused_the_same_checkpoint(tmp_path):
    """Two runners on one checkpoint is a double-send waiting to happen."""
    path = tmp_path / "run.jsonl"
    with CheckpointStore(path, RUN).exclusive():
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys;"
                    f"sys.path.insert(0, {str(Path.cwd())!r});"
                    "from insta_report.checkpoint import CheckpointStore;"
                    "from insta_report.errors import RunAborted;\n"
                    "try:\n"
                    f"    CheckpointStore({str(path)!r}, 'test-run').exclusive().__enter__()\n"
                    "    print('ACQUIRED')\n"
                    "except RunAborted:\n"
                    "    print('REFUSED')"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "REFUSED" in result.stdout, result.stdout + result.stderr


def test_lock_is_released_when_the_holder_exits(tmp_path):
    """The OS frees the lock on process death, so there is no stale lock."""
    path = tmp_path / "run.jsonl"
    store = CheckpointStore(path, RUN)
    with store.exclusive():
        pass
    with store.exclusive():
        pass  # second acquisition must succeed


def test_run_id_cannot_escape_its_directory(tmp_path):
    """run_id becomes a path segment; traversal would clobber other state."""
    from insta_report.support.paths import resolve_paths

    paths = resolve_paths(tmp_path / "data")
    for bad in ("../escape", "a/b", "..", ""):
        with pytest.raises(ValueError):
            paths.run_dir(bad)
