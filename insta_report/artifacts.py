"""Failure artifacts: DOM, screenshot, and what the tool thought it saw.

Every failed report writes a bundle. This is the only forensic material the
tool produces, so the bundle is built on the assumption that it will be read by
somebody who was not here when it happened -- weeks later, by someone debugging
a run they did not attend, with no access to the page Instagram served at the
time. Everything needed to reconstruct the failure is therefore recorded, and
everything that would leak a live session is removed.

Two failure modes drove the design.

**Screenshots of a logged-in page are credential exfiltration.** A DOM snapshot
contains the session cookie in every inline script, the CSRF token, the
reporting account's own handle, and whatever the operator was looking at. A
PNG of the same page shows the handle and the private content. Neither may be
written without going through the redactor, and the redaction is applied by this
module at write time -- not left to the logging pipeline, which is not in the
path of a file write.

**A screenshot alone cannot explain a failure.** It shows what the page looked
like; it does not show which anchors matched, what was expected, or which
category the dialog was offering. So the bundle carries the anchor drift report
alongside the pixels. When Instagram changes the report dialog, the useful
question is "which of the fourteen anchors is now wrong", and that is answered
from the metadata file, not by squinting at an image.

Retention is bounded, because the failure mode of an unbounded artifact store is
a full disk, and a full disk on a machine that also holds a live browser is a
run that stops mid-report. The default keeps a per-run cap and prunes oldest
first. Playwright traces are excluded entirely: a trace contains every request
and response of the session, which makes it the most useful and most dangerous
artifact in the set. It is opt-in per failure, off by default, and refused
unless a flag is set -- so turning it on is a visible decision.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .outcomes import TerminalState
from .support.paths import Paths, assert_outside_repo
from .support.redaction import get_registry

__all__ = [
    "ArtifactStore",
    "Artifact",
    "ArtifactError",
    "FailureContext",
    "DEFAULT_KEEP_PER_RUN",
    "MAX_HTML_BYTES",
]

log = logging.getLogger(__name__)

#: Snapshots are truncated at this size. A full Instagram page is ~600KB of
#: mostly-inlined JavaScript; the first 256KB contains the dialog and every
#: anchor we care about, and a run with 400 failures would otherwise write
#: 250MB of HTML to a disk that also holds the browser profile.
MAX_HTML_BYTES = 256 * 1024

#: Bundles kept per run before the oldest is pruned. Set high enough that a
#: genuine systematic failure is fully diagnosable; set low enough that the
#: store cannot fill a disk.
DEFAULT_KEEP_PER_RUN = 60


class ArtifactError(Exception):
    """An artifact could not be written, or was refused as unsafe."""


@dataclass(frozen=True)
class FailureContext:
    """What the tool believed when it decided a report had failed.

    Recorded verbatim because this is the claim under test. If a report is
    filed successfully and the ledger says it failed, the interesting artifact
    is not the screenshot -- it is this record, which shows what was checked and
    what was found.
    """

    target_key: str
    target_display: str
    channel: str
    terminal: TerminalState
    detail: str = ""
    #: Anchor names that matched, and what was found where one did not.
    anchors_hit: tuple[str, ...] = ()
    anchors_missed: tuple[str, ...] = ()
    drift_detail: str = ""
    #: The submit request as observed, if one was captured. Redacted on write.
    submit_status: int | None = None
    submit_body_excerpt: str = ""
    submit_url: str = ""
    account_display: str = ""
    proxy_origin: str = ""
    attempt: int = 1

    def to_metadata(self) -> dict[str, Any]:
        # Every string that came off the wire is scrubbed here rather than in each
        # caller. ``submit_body_excerpt`` and ``detail`` are both built from
        # observed traffic, and the store already scrubs the DOM it writes -- so
        # scrubbing only the HTML would have left the same secret one field away,
        # in the same directory, in the same run. Centralised because "remember to
        # scrub at the call site" is not a property, it is a habit, and the
        # channel that joins later is the one that will forget.
        scrub = get_registry().scrub
        return {
            "target": self.target_display,
            "target_key": self.target_key,
            "channel": self.channel,
            "terminal": self.terminal.value,
            "detail": scrub(self.detail),
            "anchors_hit": list(self.anchors_hit),
            "anchors_missed": list(self.anchors_missed),
            "drift_detail": self.drift_detail,
            "submit_status": self.submit_status,
            "submit_body_excerpt": scrub(self.submit_body_excerpt),
            "submit_url": scrub(self.submit_url),
            "account": self.account_display,
            "proxy_origin": self.proxy_origin,
            "attempt": self.attempt,
        }


@dataclass(frozen=True)
class Artifact:
    """Where a bundle landed and what is in it."""

    directory: Path
    metadata: Path
    html: Path | None = None
    screenshot: Path | None = None
    trace: Path | None = None
    truncated: bool = False
    redacted: bool = False

    def files(self) -> tuple[Path, ...]:
        return tuple(
            p for p in (self.metadata, self.html, self.screenshot, self.trace) if p
        )

    def __str__(self) -> str:
        return f"{self.directory.name} ({len(self.files())} file(s))"


def _validate_run_id(run_id: str) -> str:
    """Accept a run id only if it is already a safe single path segment.

    Refused rather than sanitised, and the reason is a collision rather than a
    traversal. Slugging a bad id into a safe one would stop ``../..`` escaping,
    but it would also map both ``a/b`` and ``a_b`` onto the same directory --
    silently merging the artifact bundles of two different runs, which is a
    correctness failure that produces no error and no symptom other than a
    confusing directory. A run id that is not already safe is a bug upstream, so
    it surfaces here rather than being papered over.
    """
    text = str(run_id or "").strip()
    if not text or text in {".", ".."}:
        raise ArtifactError(f"run id is empty or a relative directory: {run_id!r}")
    if "/" in text or "\\" in text or ":" in text:
        raise ArtifactError(
            f"run id {run_id!r} contains a path separator. A run id must be a "
            "single safe segment, because it names the directory every artifact "
            "for that run is written into."
        )
    if _safe_slug(text) != text:
        raise ArtifactError(
            f"run id {run_id!r} would be rewritten to {_safe_slug(text)!r} on "
            "disk. Refusing, so that two different run ids cannot end up sharing "
            "one artifact directory."
        )
    return text


def _safe_slug(text: str, width: int = 48) -> str:
    """A filesystem-safe label that stays readable.

    Collapses to ``_`` rather than dropping characters: a filename that
    silently loses most of the handle makes a directory of 60 bundles
    unsearchable, and searching them is the reason they exist.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return (cleaned or "unnamed")[:width]


class ArtifactStore:
    """Writes failure bundles outside the work tree, redacted.

    ``screenshot`` is injected because Playwright must not be a dependency of
    the failure path: if the browser has crashed -- which is the most common
    reason a report failed -- then taking a screenshot of the crash is not
    possible, and a store that requires Playwright to record a Playwright
    failure is useless in exactly the case it exists for.
    """

    def __init__(
        self,
        paths: Paths,
        run_id: str,
        *,
        screenshot: Callable[[], bytes | None] | None = None,
        keep_per_run: int = DEFAULT_KEEP_PER_RUN,
        allow_trace: bool = False,
        max_html_bytes: int = MAX_HTML_BYTES,
    ) -> None:
        self._paths = paths
        self._run_id = _validate_run_id(run_id)
        self._screenshot = screenshot
        self._keep = max(0, keep_per_run)
        self._allow_trace = allow_trace
        self._max_html = max_html_bytes
        self._sequence = 0
        # Re-asserted after the run id is validated, not only in Paths: the
        # artifact root is built by joining a configured directory to a run id,
        # and the join is where a traversal would land.
        self._root = assert_outside_repo(paths.artifacts_dir / self._run_id)

    @property
    def root(self) -> Path:
        return self._root

    def prepare(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def capture(
        self,
        context: FailureContext,
        *,
        html: str | None = None,
        trace: bytes | None = None,
    ) -> Artifact:
        """Write one bundle. Returns what was actually written.

        Never raises for a missing screenshot or a missing trace. Losing the
        pixels is a real loss, but raising would lose the metadata, the anchor
        report and the DOM as well -- and those are the parts that can be read
        after the fact. A partial bundle with a note about what is missing is
        worth more than no bundle.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        stem = f"{self._sequence:04d}-{_safe_slug(context.target_key)}"
        directory = self._root / stem
        directory.mkdir(parents=True, exist_ok=True)

        notes: list[str] = []
        scrubber = get_registry().scrub

        metadata_path = directory / "failure.json"
        metadata = context.to_metadata()
        metadata["sequence"] = self._sequence
        metadata["run_id"] = self._run_id
        if notes:
            metadata["notes"] = notes
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        html_path: Path | None = None
        truncated = False
        if html is not None:
            body, truncated = _cap(html, self._max_html)
            # Redacted before the bytes touch the disk, not after. A file that
            # was briefly on disk with a live session in it is a leak that
            # "we deleted it later" does not undo.
            body = scrubber(body)
            html_path = directory / "page.html"
            html_path.write_text(body, encoding="utf-8")

        shot_path: Path | None = None
        if self._screenshot is not None:
            try:
                data = self._screenshot()
            except Exception as exc:  # noqa: BLE001 - a failed capture must not mask the failure
                data = None
                notes.append(f"screenshot capture failed: {exc}")
            if data:
                shot_path = directory / "page.png"
                shot_path.write_bytes(get_registry().scrub_bytes(data))
            else:
                notes.append("screenshot unavailable")

        trace_path: Path | None = None
        if trace is not None:
            trace_path = self._write_trace(directory, trace, notes)

        if notes:
            # Rewrite the metadata now that the capture notes exist, so the
            # bundle states its own incompleteness rather than leaving a reader
            # to assume the screenshot is absent because nothing was wrong.
            metadata["notes"] = notes
            metadata_path.write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        self._prune()
        artifact = Artifact(
            directory=directory,
            metadata=metadata_path,
            html=html_path,
            screenshot=shot_path,
            trace=trace_path,
            truncated=truncated,
            redacted=True,
        )
        log.debug("captured failure artifact %s", artifact)
        return artifact

    def _write_trace(
        self,
        directory: Path,
        trace: bytes,
        notes: list[str],
    ) -> Path | None:
        """Write a Playwright trace, or refuse to.

        Refused unless ``allow_trace``. A trace is a complete recording of the
        session: every request header, every cookie, every response body. It is
        also the single most useful artifact for a bug that only reproduces in
        the browser, which is why it is worth having and why the default is no.

        Scrubbing is not offered as an alternative. Session values are encoded
        and fragmented across a trace in ways that string replacement does not
        reliably catch, so a "redacted" trace would be a promise this module
        cannot keep. It is written only when the operator has asked for it.
        """
        if not self._allow_trace:
            notes.append(
                "playwright trace not written: it records every request header "
                "and cookie in the session. Set allow_trace to write one for a "
                "failure you are actively debugging."
            )
            return None
        path = directory / "trace.zip"
        path.write_bytes(get_registry().scrub_bytes(trace))
        notes.append(
            "playwright trace written. This file contains the live session in "
            "full. Do not attach it to a report, a ticket, or a chat."
        )
        log.warning("wrote a session-bearing trace to %s", path)
        return path

    def _prune(self) -> None:
        """Keep the newest ``keep_per_run`` bundles; delete the rest.

        Pruning rather than refusing. A run that has already found more than
        sixty failures is not going to be rescued by the sixty-first-first
        bundle, so the useful choice is which failures to keep: the recent ones,
        which are the ones being debugged.
        """
        if self._keep <= 0:
            return
        try:
            bundles = sorted(
                (p for p in self._root.iterdir() if p.is_dir()),
                key=lambda p: p.name,
            )
        except OSError:
            return
        for stale in bundles[: max(0, len(bundles) - self._keep)]:
            for child in stale.glob("*"):
                try:
                    child.unlink()
                except OSError:  # noqa: PERF203 - best effort; a locked file must not abort the run
                    log.warning("could not remove %s", child)
            try:
                stale.rmdir()
            except OSError:
                log.warning("could not remove artifact directory %s", stale)

    def list_bundles(self) -> list[Path]:
        if not self._root.is_dir():
            return []
        return sorted(p for p in self._root.iterdir() if p.is_dir())


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n\n<!-- truncated at {limit} bytes of {len(text)} -->", True
