"""Reputation across restarts: the budget is real, and the streak never resets.

The claims under test, in the order the module makes them:

* a missing file is a first run, not an error;
* everything a run learns -- budget, streak, deadline, history -- survives
  save-then-load into a *fresh* pool, with the deadline crossing the process
  boundary as wall-clock on the way out and the pool's monotonic on the way in;
* an exhausted budget stays exhausted across a restart (the Ctrl-C hole);
* a budget from a previous day does not charge today;
* a quarantine that lapsed while the tool was off is gone, but the streak
  that caused it still backs the next failure off to where it stopped;
* a file that cannot be trusted is refused, loudly, naming itself;
* the file never contains a credential.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from insta_report.accounts import (
    DEFAULT_QUARANTINE_BASE_SECONDS,
    Account,
    AccountPool,
)
from insta_report.config import ConfigError
from insta_report.reputation import (
    FORMAT_VERSION,
    AccountReputation,
    ReputationError,
    ReputationStore,
)
from .test_probe import FAKE_SESSION

#: The bare sessionid *value* -- the shape a redaction registry is handed, and
#: so the shape this file must be proved not to carry. Shared with test_probe
#: rather than re-spelled: the credential gate's allowlist is meant to hold one
#: literal, not one near-identical entry per test, because an allowlist that
#: grows that way is one a real cookie can walk into by changing a character.
SESSION = FAKE_SESSION


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_pool(*, mono: float = 1000.0) -> tuple[AccountPool, Clock]:
    clock = Clock(mono)
    pool = AccountPool(
        [
            Account(
                ref="alpha",
                username="reporter.one",
                sessionid=SESSION,
                daily_budget=20,
            )
        ],
        monotonic=clock,
    )
    return pool, clock


def entry(**overrides: object) -> dict:
    """One valid on-disk account entry, with room for one deliberate break."""
    fields: dict[str, object] = {
        "consecutive_failures": 0,
        "total_reports": 0,
        "total_acked": 0,
        "used_today": 0,
        "day": date.today().isoformat(),
    }
    fields.update(overrides)
    return fields


def write_file(path: Path, accounts: dict[str, object], version: int = FORMAT_VERSION) -> None:
    path.write_text(
        json.dumps({"version": version, "saved_at": 0, "accounts": accounts}),
        encoding="utf-8",
    )


# -- loading ----------------------------------------------------------------


def test_a_missing_file_is_a_first_run_not_an_error(tmp_path):
    pool, _ = make_pool()
    store = ReputationStore(tmp_path / "reputation.json")
    assert store.load_into(pool) == []
    assert pool.get("alpha").used_today == 0
    assert pool.get("alpha").consecutive_failures == 0


def test_a_path_that_exists_but_is_not_a_file_is_refused(tmp_path):
    """A directory at the file's name would otherwise read as "no file",
    and "no file" means a fresh budget -- silently, through a typo."""
    path = tmp_path / "reputation.json"
    path.mkdir()
    with pytest.raises(ReputationError, match="not a regular file"):
        ReputationStore(path).load_into(make_pool()[0])


def test_everything_a_run_learned_survives_save_then_a_fresh_load(tmp_path):
    path = tmp_path / "reputation.json"
    pool, _ = make_pool(mono=1000.0)
    alpha = pool.get("alpha")
    alpha.used_today = 7
    alpha.consecutive_failures = 3
    alpha.total_reports = 12
    alpha.total_acked = 9
    alpha.sidelined_for = "challenge"
    alpha.quarantined_until = 1600.0  # 600s from the pool's 1000.0

    ReputationStore(path, wall_clock=lambda: 1_000_000.0).save(pool)

    fresh, _ = make_pool(mono=50.0)
    applied = ReputationStore(path, wall_clock=lambda: 1_000_100.0).load_into(fresh)

    assert applied and isinstance(applied[0], AccountReputation)
    restored = fresh.get("alpha")
    assert restored.used_today == 7
    assert restored.consecutive_failures == 3
    assert restored.total_reports == 12
    assert restored.total_acked == 9
    assert restored.sidelined_for == "challenge"
    # Saved at epoch 1_000_600; loaded 100s later, so 500s remain, re-anchored
    # on THIS process's clock: 50 + 500. The arithmetic is the bridge.
    assert restored.quarantined_until == 550.0


def test_the_pool_exposes_the_clock_a_deadline_is_judged_in():
    pool, clock = make_pool(mono=42.0)
    assert pool.monotonic() == 42.0
    clock.advance(3.0)
    assert pool.monotonic() == 45.0


# -- the budget, the hole it closes ----------------------------------------


def test_an_exhausted_budget_stays_exhausted_across_a_restart(tmp_path):
    """The hole: before this file, every restart reset used_today to zero and
    Ctrl-C re-approved a budget the previous run had already spent."""
    path = tmp_path / "reputation.json"
    pool, _ = make_pool()
    alpha = pool.get("alpha")
    alpha.used_today = 20
    ReputationStore(path, wall_clock=lambda: 1_000_000.0).save(pool)

    fresh, clock = make_pool()
    ReputationStore(path).load_into(fresh)

    restored = fresh.get("alpha")
    assert restored.budget_spent is True
    assert restored.remaining == 0
    assert restored.eligible(clock()) is False


def test_a_budget_saved_yesterday_does_not_charge_today(tmp_path, caplog):
    """The other half of the same claim: the reset on a *new* day still
    happens, so persistence must not pin the budget to last Tuesday."""
    path = tmp_path / "reputation.json"
    pool, _ = make_pool()
    alpha = pool.get("alpha")
    alpha.used_today = 20
    alpha.day = date.today() - timedelta(days=1)
    ReputationStore(path, wall_clock=lambda: 1_000_000.0).save(pool)

    fresh, _ = make_pool()
    with caplog.at_level(logging.INFO, logger="insta_report.reputation"):
        ReputationStore(path).load_into(fresh)

    restored = fresh.get("alpha")
    assert restored.used_today == 0
    assert restored.remaining == 20
    assert any(
        "daily budget starts fresh" in record.getMessage()
        for record in caplog.records
    )


# -- quarantine across the process boundary ---------------------------------


def test_a_lapsed_quarantine_clears_but_the_streak_keeps_backing_off(tmp_path):
    """Reputation is cumulative -- there is no reset.

    The deadline decays while the tool is off; the streak does not. So the
    first failure after the restart backs off from where the last run
    stopped, not from a fresh five minutes.
    """
    path = tmp_path / "reputation.json"
    pool, _ = make_pool()
    alpha = pool.get("alpha")
    alpha.consecutive_failures = 4
    alpha.quarantined_until = 1000.0 + 600
    ReputationStore(path, wall_clock=lambda: 1_000_000.0).save(pool)

    # Loaded 700s after the save: the deadline (epoch 1_000_600) has passed.
    fresh, clock = make_pool(mono=42.0)
    ReputationStore(path, wall_clock=lambda: 1_000_700.0).load_into(fresh)

    restored = fresh.get("alpha")
    assert restored.quarantined_until is None
    assert restored.consecutive_failures == 4
    # Streak 4 restored, so the next failure is the fifth: base * 2^4 -- the
    # backoff resumes where the last run stopped, not from a fresh five minutes.
    wait = restored.record_failure("flaky", clock())
    assert wait == DEFAULT_QUARANTINE_BASE_SECONDS * (2**4)


def test_a_live_quarantine_is_written_as_an_epoch_and_read_back(tmp_path):
    path = tmp_path / "reputation.json"
    pool, _ = make_pool(mono=100.0)
    alpha = pool.get("alpha")
    alpha.consecutive_failures = 1
    alpha.quarantined_until = 400.0  # 300s remaining
    ReputationStore(path, wall_clock=lambda: 1_000_000.0).save(pool)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["accounts"]["alpha"]["quarantined_until_epoch"] == 1_000_300.0

    fresh, _ = make_pool(mono=0.0)
    ReputationStore(path, wall_clock=lambda: 1_000_050.0).load_into(fresh)
    # 250s still to run, from the fresh pool's own zero.
    assert fresh.get("alpha").quarantined_until == 250.0


# -- refusals ----------------------------------------------------------------


def test_a_corrupt_file_is_refused_and_names_itself(tmp_path):
    path = tmp_path / "reputation.json"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ReputationError, match="reputation.json") as excinfo:
        ReputationStore(path).load_into(make_pool()[0])
    assert isinstance(excinfo.value, ConfigError)


def test_a_version_from_the_future_is_refused_not_guessed_at(tmp_path):
    path = tmp_path / "reputation.json"
    write_file(path, {}, version=99)
    with pytest.raises(ReputationError, match="version"):
        ReputationStore(path).load_into(make_pool()[0])


def test_a_top_level_that_is_not_an_object_is_refused(tmp_path):
    path = tmp_path / "reputation.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ReputationError, match="expected a JSON object"):
        ReputationStore(path).load_into(make_pool()[0])


@pytest.mark.parametrize(
    "key",
    ["consecutive_failures", "total_reports", "total_acked", "used_today", "day"],
)
def test_a_field_missing_from_an_entry_is_refused_naming_it(tmp_path, key):
    fields = entry()
    del fields[key]
    path = tmp_path / "reputation.json"
    write_file(path, {"alpha": fields})
    with pytest.raises(ReputationError, match=key):
        ReputationStore(path).load_into(make_pool()[0])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("used_today", "many"),
        ("used_today", True),  # bool is not a count, however much Python disagrees
        ("consecutive_failures", -1),
        ("total_acked", 3.5),
        ("day", "13-10-2026"),
        ("quarantined_until_epoch", "soon"),
        ("sidelined_for", 7),
    ],
)
def test_a_field_of_the_wrong_shape_is_refused(tmp_path, key, value):
    path = tmp_path / "reputation.json"
    write_file(path, {"alpha": entry(**{key: value})})
    with pytest.raises(ReputationError) as excinfo:
        ReputationStore(path).load_into(make_pool()[0])
    message = str(excinfo.value)
    assert "reputation.json" in message
    assert "alpha" in message
    assert key in message


def test_reputation_for_an_account_that_left_the_config_is_ignored(tmp_path):
    path = tmp_path / "reputation.json"
    write_file(path, {"alpha": entry(), "ghost": entry(total_reports=9)})

    fresh, _ = make_pool()
    applied = ReputationStore(path).load_into(fresh)

    assert [record.ref for record in applied] == ["alpha"]
    assert fresh.get("alpha").total_reports == 0


# -- hygiene -----------------------------------------------------------------


def test_the_file_never_carries_a_credential(tmp_path):
    path = tmp_path / "reputation.json"
    pool, _ = make_pool()
    pool.get("alpha").used_today = 3
    ReputationStore(path).save(pool)

    text = path.read_text(encoding="utf-8")
    assert SESSION not in text
    assert "sessionid" not in text
    payload = json.loads(text)
    # Keyed by ref on the outside -- that is the only name in the file, and it
    # is not a credential.
    assert payload["accounts"]["alpha"]["used_today"] == 3
    assert "alpha" not in payload["accounts"]["alpha"]


def test_save_creates_the_directory_and_leaves_only_the_final_file(tmp_path):
    path = tmp_path / "state" / "nested" / "reputation.json"
    pool, _ = make_pool()
    store = ReputationStore(path)
    store.save(pool)

    assert path.is_file()
    assert [p.name for p in path.parent.iterdir()] == ["reputation.json"]

    pool.get("alpha").used_today = 5
    store.save(pool)
    assert json.loads(path.read_text(encoding="utf-8"))["accounts"]["alpha"][
        "used_today"
    ] == 5
    assert [p.name for p in path.parent.iterdir()] == ["reputation.json"]
