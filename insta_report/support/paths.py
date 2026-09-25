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
    "find_repo_root",
    "default_data_dir",
    "assert_outside_repo",
    "Paths",
    "resolve_paths",
]


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
        """Per-run state directory. Separate run ids never share a checkpoint."""
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise ValueError(f"run_id must be a single safe path segment, got {run_id!r}")
        path = self.state_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path


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
