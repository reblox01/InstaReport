"""Filesystem locations.

The data directory defaults OUTSIDE the working tree on purpose. An
authenticated Instagram screenshot is PII, and a DOM dump contains
``sessionid`` and ``csrftoken`` in plaintext. If the artifact root lives
inside the repo, one ``git add -A`` commits live credentials -- and
``.gitignore`` alone is a convention, not a guarantee.

``assert_outside_repo`` is the guarantee. ``.gitignore`` is the backstop.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PathContainmentError",
    "RunIdError",
    "check_run_id",
    "find_repo_root",
    "default_data_dir",
    "assert_outside_repo",
    "Paths",
    "resolve_paths",
]


class RunIdError(ValueError):
    """Raised when a run id is not usable as a single path segment.

    Its own type rather than a bare ``ValueError`` so a caller can turn it into
    a refusal with a message, instead of it escaping as a traceback. The CLI
    needs exactly that: a mistyped ``--run-id`` is bad usage, and bad usage is
    exit code 2 with a sentence, not a stack trace about a string.
    """


class PathContainmentError(ValueError):
    """Raised when a path that must stay outside the repo would land inside it."""


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* looking for a ``.git`` marker.

    Returns ``None`` when not inside a work tree, so callers can decide
    whether that is fatal rather than having it raise from underneath them.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def default_data_dir() -> Path:
    """Platform-appropriate writable location, outside any work tree by default."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "instaReport"


def assert_outside_repo(path: Path, *, repo_root: Path | None = None) -> Path:
    """Return *path* resolved, or raise if it sits inside the work tree.

    Uses ``is_relative_to`` rather than string comparison so a sibling
    directory sharing a name prefix (``.../instaReport-secrets`` vs
    ``.../instaReport``) is not misjudged.
    """
    resolved = Path(path).expanduser().resolve()
    root = repo_root or find_repo_root()
    if root is None:
        # Not in a work tree at all, so containment is not a concern.
        return resolved
    root = root.resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise PathContainmentError(
            f"{resolved} is inside the repository at {root}. "
            "Runtime artifacts contain live session cookies and must be written "
            "outside the work tree. Set data_dir in the config, or run from a "
            "directory that is not a checkout."
        )
    return resolved


@dataclass(frozen=True)
class Paths:
    """Resolved runtime locations. All are validated outside the repo on build."""

    data_dir: Path
    artifacts_dir: Path
    traces_dir: Path
    state_dir: Path
    logs_dir: Path

    def ensure(self) -> "Paths":
        for directory in (
            self.data_dir,
            self.artifacts_dir,
            self.traces_dir,
            self.state_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def run_dir(self, run_id: str) -> Path:
        """Per-run state directory. Separate run ids never share a checkpoint.

        Creates the directory as a side effect, so the check runs first and
        separately: a caller that wants to know whether an id is usable cannot
        get that answer from a function that has already made a directory for
        it. See :func:`check_run_id`.
        """
        check_run_id(run_id)
        path = self.state_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def check_run_id(run_id: str) -> str:
    """Refuse a run id that is not one safe path segment. Returns it unchanged.

    **Refused, never repaired.** A sanitised id points at a *different run's*
    directory, and the consequence of resuming the wrong run is double-reporting
    a target whose report already landed -- which is the one outcome this whole
    design exists to prevent. So there is no replacement character, no
    stripping, no "best effort" branch: an id that needs fixing is an id the
    operator fixes.

    The rules, and the reason each is here rather than assumed:

    * **Non-empty, and not ``.`` or ``..``** -- both are directories that
      already exist, and both would silently reuse a different run's state.
    * **No ``/`` or ``\\``** -- a separator turns one segment into a tree, and
      on Windows ``a\\b`` is the same as ``a/b`` while on POSIX it is a
      perfectly legal *filename*. Checking both keeps a config portable between
      the two, which matters because a run started on one platform and resumed
      on another would otherwise diverge.
    * **No leading or trailing space, and no trailing dot** -- Windows silently
      strips both, so ``"r1 "`` and ``"r1."`` address the same directory that
      ``"r1"`` does. Three ids, one checkpoint. The refusal is the only thing
      that stops the second and third from being read as a settled run.
    * **No control characters** -- they are invisible in a terminal, in a log
      line, and in a directory listing, so a corrupted id is undiagnosable
      exactly when it is most expensive to debug.
    * **No NUL** -- rejected by the OS on write, which is a late and confusing
      failure for something knowable here.
    """
    if not run_id:
        raise RunIdError("a run id cannot be empty")
    if run_id in {".", ".."}:
        raise RunIdError(
            f"a run id of {run_id!r} is a directory that already exists, so it "
            "would reuse another run's state. Pass an explicit id."
        )
    if "/" in run_id or "\\" in run_id:
        raise RunIdError(
            f"a run id cannot contain a path separator, got {run_id!r}. A run id "
            "is one directory name; the state directory is already chosen for you."
        )
    if run_id != run_id.strip() or run_id.endswith("."):
        raise RunIdError(
            f"a run id cannot begin or end with a space, or end with a dot: "
            f"{run_id!r}. Windows strips both, so it would address a different "
            "directory than it looks like."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in run_id):
        raise RunIdError(
            f"a run id cannot contain control characters: {run_id!r}. They are "
            "invisible in a log and in a directory listing, so a corrupted id "
            "cannot be diagnosed after the fact."
        )
    return run_id


def resolve_paths(data_dir: str | Path | None = None) -> Paths:
    """Build and validate the path set. Raises if anything lands inside the repo."""
    root = Path(data_dir).expanduser() if data_dir else default_data_dir()
    resolved_root = assert_outside_repo(root)
    return Paths(
        data_dir=resolved_root,
        artifacts_dir=assert_outside_repo(resolved_root / "artifacts"),
        traces_dir=assert_outside_repo(resolved_root / "traces"),
        state_dir=assert_outside_repo(resolved_root / "state"),
        logs_dir=assert_outside_repo(resolved_root / "logs"),
    )
