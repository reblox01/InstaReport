"""The 60-second demo. Offline, synthetic, and honest about both.

A hackathon judge has three minutes and no proxy, no session, and no reason to
trust a tool that has never filed a report. This command is what runs in that
window:

```
    insta-report demo                 # offline half, no browser
    insta-report demo --with-browser  # plus real Chromium, local fixture only
```

What it proves, in order, cheapest first:

1. The setup checks pass with no network (the ``doctor --no-live`` half).
2. A real Chromium opens the committed golden report dialog and the wizard
   reads and classifies it -- through the production ``observe()`` and
   ``ReportWizard``, with no clicks issued. The browser moves, the dialog is
   real pixels, and nothing is submitted because the demo never calls
   ``click_submit``.
3. The dispatch boundary works: an intent is fsynced, an outcome is appended,
   and a fresh read-back finds both. The records are synthetic and labelled so
   in every field that names them -- a demo that filed a fake ACKED would be
   the original ``igban.py`` bug with better lighting.

What it never does: open a socket past loopback, open a session, lease
an exit, or touch instagram.com. The session values it asks for are checked
for shape only and never used; the offline socket guard in
``tests/conftest.py`` covers this the same way it covers everything else.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .anchors import load_anchors
from .browser import BrowserDriver, ReportWizard, observe
from .checkpoint import CheckpointStore, Intent
from .config import Config
from .doctor import run_doctor
from .outcomes import Outcome, TerminalState
from .support.logging import setup_logging
from .support.paths import check_run_id

__all__ = [
    "DEMO_TARGET_KEY",
    "DEMO_DETAIL",
    "DemoStep",
    "DemoResult",
    "run_demo",
    "render_evidence_html",
]

#: The ledger key the demo writes under. Namespaced so it can never collide
#: with a real target handle, and so a ``status`` listing shows it for what it
#: is rather than as a report that went out.
DEMO_TARGET_KEY = "demo/synthetic-target"

#: The detail on the demo's outcome record. Stated on the record itself,
#: because a synthetic row that relies on surrounding context to be recognised
#: as synthetic is one ``grep`` away from being quoted as a real filing.
DEMO_DETAIL = (
    "synthetic demo record: the dispatch boundary exercised with no network, "
    "no submit click, nothing sent to Instagram"
)

#: The golden dialog the fixture rehearsal opens. Committed, versioned, and
#: the same file the offline drift suite reads -- so a demo that passes while
#: the anchors drift is impossible.
FIXTURE_NAME = "report_dialog.html"


@dataclass(frozen=True)
class DemoStep:
    """One demo act, and how it ended. ``skip`` is a result, not a failure."""

    name: str
    status: str  # "pass" | "skip" | "fail"
    detail: str = ""


@dataclass
class DemoResult:
    """Everything the demo did, for the terminal and the evidence bundle."""

    run_id: str
    steps: list[DemoStep] = field(default_factory=list)
    wizard_path: tuple[str, ...] = ()
    anchors_hit: tuple[str, ...] = ()
    checkpoint: str = ""
    evidence: str = ""
    browser_used: bool = False

    def failed(self) -> bool:
        return any(step.status == "fail" for step in self.steps)

    def render(self) -> str:
        lines = [
            f"demo {self.run_id}  (offline -- nothing was sent anywhere)",
            "",
            "  steps:",
        ]
        for step in self.steps:
            marker = {"pass": "ok  ", "skip": "skip", "fail": "FAIL"}.get(
                step.status, "????"
            )
            lines.append(f"    {marker}  {step.name}")
            if step.detail:
                lines.append(f"          {step.detail}")
        if self.wizard_path:
            lines.append(f"  wizard: {' -> '.join(self.wizard_path)}")
        if self.checkpoint:
            lines.append(f"  ledger: {self.checkpoint}")
        if self.evidence:
            lines.append(f"  evidence: {self.evidence}")
        lines.append(
            "  every record above names itself synthetic; "
            "quote it as a filing and you are misquoting it on purpose."
        )
        return "\n".join(lines)


def _fixture_path() -> Path | None:
    """The committed golden dialog, or ``None`` outside a checkout."""
    candidate = Path(__file__).resolve().parent.parent / "tests" / "golden" / FIXTURE_NAME
    return candidate if candidate.is_file() else None


async def _rehearse_fixture(
    config: Config, result: DemoResult
) -> None:
    """Open the golden dialog in real Chromium and read it. No clicks.

    Uses the production ``BrowserDriver`` and ``observe()`` -- the same
    objects a run uses -- minus everything that moves the flow forward. The
    driver is started, the page is read once, the wizard classifies the
    reading, and the driver is closed. There is no ``click_submit`` in this
    function, which is checkable by reading it.
    """
    fixture = _fixture_path()
    if fixture is None:
        result.steps.append(
            DemoStep(
                "fixture rehearsal",
                "skip",
                "tests/golden/report_dialog.html is not present; "
                "this copy is installed, not checked out",
            )
        )
        return

    try:
        anchors = load_anchors(config.anchors.path)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        result.steps.append(
            DemoStep("fixture rehearsal", "fail", f"anchors would not load: {exc}")
        )
        return

    driver = BrowserDriver(user_data_dir=None, headless=True)
    try:
        await driver.start(anchors)
    except Exception as exc:  # noqa: BLE001 - Chromium missing is a skip
        result.steps.append(
            DemoStep(
                "fixture rehearsal",
                "skip",
                f"chromium did not launch ({type(exc).__name__}): "
                "run `python -m playwright install chromium` for the Browser act",
            )
        )
        return

    try:
        page = driver._page
        if page is None:  # pragma: no cover - start() always sets it
            raise RuntimeError("driver started without a page")
        await page.goto(fixture.as_uri())

        async def probe(
            name: str, *, require_enabled: bool, timeout_ms: int | None
        ) -> str | None:
            return await driver.read(name, require_enabled=require_enabled)

        observation = await observe(anchors, probe, at=0.0)
        wizard = ReportWizard()
        state = wizard.observe(observation)
        action = wizard.action()
        result.wizard_path = tuple(wizard.path())
        result.anchors_hit = tuple(sorted(observation.anchors_hit))
        result.browser_used = True
        result.steps.append(
            DemoStep(
                "fixture rehearsal",
                "pass",
                f"chromium read {fixture.name}: state={state.value}, "
                f"next={action.value}, {len(result.anchors_hit)} anchor(s) hit; "
                "no click issued",
            )
        )
    except Exception as exc:  # noqa: BLE001 - a failed read is a finding
        result.steps.append(
            DemoStep(
                "fixture rehearsal", "fail", f"{type(exc).__name__}: {exc}"
            )
        )
    finally:
        await driver.close()


def _ledger_round_trip(config: Config, run_id: str, result: DemoResult) -> None:
    """Write an intent and an outcome, then prove a fresh read finds both.

    Synthetic throughout: the target key, the account ref, the lease id, the
    channel, and the detail all say so. The point being proved is mechanical --
    fsync-per-record, intent-before-outcome, read-back agreement -- not that a
    report exists, because none does.
    """
    run_dir = config.paths.run_dir(run_id)
    path = run_dir / "checkpoint.jsonl"
    store = CheckpointStore(path, run_id)
    try:
        with store:
            store.record_intent(
                Intent(
                    run_id=run_id,
                    target_ref=DEMO_TARGET_KEY,
                    account_ref="demo/synthetic-account",
                    lease_id="demo",
                    channel="demo",
                    attempt=1,
                )
            )
            store.record_outcome(
                Outcome(
                    terminal=TerminalState.CHANNEL_FAILED,
                    target_ref=DEMO_TARGET_KEY,
                    channel="demo",
                    account_ref="demo/synthetic-account",
                    lease_id="demo",
                    detail=DEMO_DETAIL,
                )
            )
    except Exception as exc:  # noqa: BLE001
        result.steps.append(
            DemoStep("ledger round-trip", "fail", f"{type(exc).__name__}: {exc}")
        )
        return

    reread = CheckpointStore(path, run_id)
    outcome = reread.state.outcome_for(DEMO_TARGET_KEY)
    if outcome is None or outcome.detail != DEMO_DETAIL:
        result.steps.append(
            DemoStep(
                "ledger round-trip",
                "fail",
                "a fresh read did not find the synthetic outcome; "
                "the boundary did not hold",
            )
        )
        return
    result.checkpoint = str(path)
    result.steps.append(
        DemoStep(
            "ledger round-trip",
            "pass",
            "intent fsynced, outcome appended, fresh read agrees; "
            "target key and detail name it synthetic",
        )
    )


async def run_demo(
    config: Config,
    *,
    run_id: str,
    with_browser: bool,
    stream: TextIO,
    verbose: bool = False,
) -> DemoResult:
    """The demo, cheapest act first. Never opens a non-loopback socket."""
    setup_logging(verbose=verbose, stream=stream)
    try:
        check_run_id(run_id)
    except Exception as exc:  # noqa: BLE001
        result = DemoResult(run_id=run_id)
        result.steps.append(DemoStep("run id", "fail", str(exc)))
        return result

    result = DemoResult(run_id=run_id)

    report = await run_doctor(
        config,
        targets=None,
        pool=None,
        channels=(),
        probe_target=None,
        submit=False,
        live=False,
    )
    failures = [c for c in report.checks if c.status.value == "fail"]
    if failures:
        result.steps.append(
            DemoStep(
                "setup checks",
                "fail",
                "; ".join(f"{c.name}: {c.detail}" for c in failures),
            )
        )
    else:
        passed = sum(1 for c in report.checks if c.status.value == "pass")
        result.steps.append(
            DemoStep(
                "setup checks",
                "pass",
                f"{passed} offline check(s) passed, no network touched; "
                "no channel rehearsed, and the report says so",
            )
        )

    if with_browser:
        await _rehearse_fixture(config, result)
    else:
        result.steps.append(
            DemoStep(
                "fixture rehearsal",
                "skip",
                "pass --with-browser to open the golden dialog in Chromium",
            )
        )

    _ledger_round_trip(config, run_id, result)

    evidence = render_evidence_html(result, config.paths.run_dir(run_id))
    result.evidence = str(evidence)
    return result


def render_evidence_html(result: DemoResult, run_dir: Any) -> Path:
    """One self-contained HTML bundle a judge can open with a double-click.

    No external assets, no JavaScript, no network references. A bundle that
    phones home for a font is a bundle that fails the offline claim it exists
    to demonstrate.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "demo-evidence.html"

    rows = "\n".join(
        "      <tr>"
        f"<td>{html.escape(step.name)}</td>"
        f"<td class='{html.escape(step.status)}'>{html.escape(step.status)}</td>"
        f"<td>{html.escape(step.detail)}</td></tr>"
        for step in result.steps
    )
    wizard = (
        html.escape(" -&gt; ".join(result.wizard_path))
        if result.wizard_path
        else "(browser act skipped)"
    )
    anchors = (
        html.escape(", ".join(result.anchors_hit)) if result.anchors_hit else "(none)"
    )

    path.write_text(
        "<!DOCTYPE html>\n"
        "<html lang='en'><head><meta charset='utf-8'>\n"
        f"<title>demo {html.escape(result.run_id)} -- synthetic</title>\n"
        "<style>"
        "body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;"
        "padding:0 1rem;color:#1a1a1a}"
        ".banner{background:#fff3cd;border:1px solid #9a7b00;padding:1rem;"
        "margin-bottom:1.5rem}"
        "table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ccc;padding:.4rem .6rem;text-align:left;"
        "vertical-align:top}"
        ".pass{color:#0a6b2d;font-weight:bold}"
        ".fail{color:#a00;font-weight:bold}"
        ".skip{color:#666}"
        "code{background:#f4f4f4;padding:.1rem .3rem}"
        "</style></head><body>\n"
        "<div class='banner'><strong>SYNTHETIC DEMO RECORD.</strong> "
        "Nothing on this page was sent to Instagram. The ledger rows name "
        "themselves synthetic; quote them as a filing and you are misquoting "
        "them on purpose.</div>\n"
        f"<h1>demo <code>{html.escape(result.run_id)}</code></h1>\n"
        "<h2>steps</h2>\n"
        "<table><tr><th>act</th><th>result</th><th>detail</th></tr>\n"
        f"{rows}\n</table>\n"
        f"<h2>wizard path</h2><p><code>{wizard}</code></p>\n"
        f"<h2>anchors hit</h2><p><code>{anchors}</code></p>\n"
        "<h2>what this proves</h2>\n"
        "<ul><li>setup checks run with no network</li>"
        "<li>real Chromium reads the golden dialog (browser act)</li>"
        "<li>intent-before-outcome holds on a fresh read</li></ul>\n"
        "<h2>what this does not prove</h2>\n"
        "<ul><li>that a report can be filed</li>"
        "<li>that Instagram would act on one</li>"
        "<li>anything about any real account</li></ul>\n"
        "</body></html>\n",
        encoding="utf-8",
    )
    return path
