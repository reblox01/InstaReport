#!/usr/bin/env python3
"""Bump the version in both homes, and refuse to let them drift.

The version lives in two files, because a package needs it at import time
(``insta_report/__init__.py``) and a build needs it at build time
(``pyproject.toml``). Two homes with nothing checking the seam is a release
that ships one number while the running process prints another -- exactly the
class of drift the rest of this project writes gates against. This script is
the sanctioned way to move the number: it verifies the two homes already
agree, refuses a target that is not a strict forward ``x.y.z`` step, rewrites
both, and re-reads them before reporting success.

``tests/test_version_harmony.py`` drives this contract against copies in a
temporary directory, so the refusals below are tested rather than hoped for.
The release workflow is the only caller in production.

Usage:
    python .github/scripts/bump_version.py 0.2.0
    python .github/scripts/bump_version.py 0.2.0 --pyproject P --init I
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_DEFAULT_INIT = _REPO_ROOT / "insta_report" / "__init__.py"

_INIT_LINE = re.compile(r'^__version__ = "(?P<version>[^"]+)"$', re.MULTILINE)
_SEMVER = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


def _parts(version: str) -> tuple[int, int, int]:
    match = _SEMVER.match(version)
    if match is None:
        raise SystemExit(f"refusing {version!r}: expected a strict x.y.z version")
    return int(match["major"]), int(match["minor"]), int(match["patch"])


def read_homes(pyproject: Path, init: Path) -> tuple[str, str]:
    """Return ``(declared, imported)`` without judging whether they match."""
    with pyproject.open("rb") as handle:
        declared = tomllib.load(handle)["project"]["version"]
    match = _INIT_LINE.search(init.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f'{init}: no `__version__ = "x.y.z"` line found')
    return declared, match["version"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the new version, as x.y.z")
    parser.add_argument("--pyproject", type=Path, default=_DEFAULT_PYPROJECT)
    parser.add_argument("--init", type=Path, default=_DEFAULT_INIT)
    args = parser.parse_args(argv)

    requested = _parts(args.version)  # refuse a non-semver target up front

    declared, imported = read_homes(args.pyproject, args.init)
    if declared != imported:
        raise SystemExit(
            "the two version homes already disagree, so there is no safe "
            f"baseline to bump from:\n  {args.pyproject}: {declared}\n"
            f"  {args.init}: {imported}\n"
            "Reconcile them by hand first; a bump on top of drift hides which "
            "number was the real one."
        )

    current = declared
    if requested <= _parts(current):
        raise SystemExit(
            f"refusing {args.version!r}: not greater than the current version "
            f"{current!r}. Downstream cannot tell an unchanged or backwards "
            "version from a successful forward bump."
        )

    pyproject_text = args.pyproject.read_text(encoding="utf-8")
    updated_pyproject = pyproject_text.replace(
        f'version = "{current}"', f'version = "{args.version}"', 1
    )
    if updated_pyproject == pyproject_text:
        raise SystemExit(
            f'{args.pyproject}: could not find `version = "{current}"` to replace'
        )

    init_text = args.init.read_text(encoding="utf-8")
    updated_init, replacements = _INIT_LINE.subn(
        f'__version__ = "{args.version}"', init_text
    )
    if replacements != 1:
        raise SystemExit(
            f"{args.init}: expected exactly one __version__ line, found {replacements}"
        )

    args.pyproject.write_text(updated_pyproject, encoding="utf-8")
    args.init.write_text(updated_init, encoding="utf-8")

    declared_after, imported_after = read_homes(args.pyproject, args.init)
    if declared_after != args.version or imported_after != args.version:
        raise SystemExit(
            "wrote the version but re-reading it disagrees: "
            f"pyproject={declared_after!r} init={imported_after!r} "
            f"expected={args.version!r}"
        )

    print(args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
