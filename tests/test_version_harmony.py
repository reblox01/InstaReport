"""The version invariant, and the tool that is allowed to change it.

One number lives in two files: ``pyproject.toml``, read at build time, and
``insta_report/__init__.py``, read at import time. Nothing made the two agree
until this file -- which means a release could ship one number while the
running process printed another, the precise drift this project writes gates
for everywhere else. This is that gate, and it also drives the bump script
that is the sanctioned way to move the number, so the script's refusals are
exercised rather than assumed.

The bump script is driven as a subprocess against copies in a temporary
directory: it is release tooling, deliberately not part of the shipped wheel,
and the command line it is given here is the command line the release
workflow gives it.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import insta_report

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_BUMP_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "bump_version.py"
_INIT_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


def _declared_version(pyproject: Path) -> str:
    with pyproject.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _imported_version(init: Path) -> str:
    match = _INIT_LINE.search(init.read_text(encoding="utf-8"))
    assert match is not None, f"{init} has no __version__ line"
    return match.group(1)


class TestTheTwoHomesAgree:
    def test_the_imported_version_equals_the_declared_version(self):
        """The invariant the bump script exists to preserve.

        When this fails the fix is
        ``python .github/scripts/bump_version.py <version>`` -- not editing one
        file and hoping the other follows.
        """
        assert insta_report.__version__ == _declared_version(_PYPROJECT)


class TestTheCliReportsTheVersion:
    def test_dash_dash_version_prints_the_imported_version(self):
        """A released tool answers ``--version``.

        Asserted as a subprocess, through the module entry point, because that
        is how an operator reaches it and because the argparse ``version``
        action exits rather than returns -- invisible to a test that calls
        ``main`` in-process.
        """
        result = subprocess.run(
            [sys.executable, "-m", "insta_report.cli", "--version"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"insta-report {insta_report.__version__}"


class TestTheBumpScriptIsTheOnlySanctionedWay:
    def _fixture(
        self, directory: Path, *, declared: str = "1.2.3", imported: str = "1.2.3"
    ) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        pyproject = directory / "pyproject.toml"
        pyproject.write_text(
            f'[project]\nname = "insta-report"\nversion = "{declared}"\n',
            encoding="utf-8",
        )
        init = directory / "__init__.py"
        init.write_text(
            f'"""a stand-in package docstring."""\n\n__version__ = "{imported}"\n',
            encoding="utf-8",
        )
        return pyproject, init

    def _run(
        self, version: str, *, pyproject: Path, init: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(_BUMP_SCRIPT),
                version,
                "--pyproject",
                str(pyproject),
                "--init",
                str(init),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(_REPO_ROOT),
        )

    def test_a_forward_bump_rewrites_both_homes_and_prints_the_version(
        self, tmp_path
    ):
        pyproject, init = self._fixture(tmp_path)
        result = self._run("1.2.4", pyproject=pyproject, init=init)
        assert result.returncode == 0, result.stderr
        assert _declared_version(pyproject) == "1.2.4"
        assert _imported_version(init) == "1.2.4"
        assert result.stdout.strip() == "1.2.4"

    def test_a_backwards_or_unchanged_version_is_refused_before_writing(
        self, tmp_path
    ):
        for index, version in enumerate(("1.2.3", "1.2.2", "1.1.9")):
            pyproject, init = self._fixture(tmp_path / str(index))
            result = self._run(version, pyproject=pyproject, init=init)
            assert result.returncode != 0, version
            assert "not greater" in result.stderr, version
            assert _declared_version(pyproject) == "1.2.3", version
            assert _imported_version(init) == "1.2.3", version

    def test_a_non_semver_target_is_refused(self, tmp_path):
        pyproject, init = self._fixture(tmp_path)
        result = self._run("1.3", pyproject=pyproject, init=init)
        assert result.returncode != 0
        assert "x.y.z" in result.stderr
        assert _declared_version(pyproject) == "1.2.3"

    def test_drift_between_the_homes_is_refused_before_anything_is_written(
        self, tmp_path
    ):
        pyproject, init = self._fixture(tmp_path, declared="1.2.3", imported="1.2.2")
        result = self._run("1.3.0", pyproject=pyproject, init=init)
        assert result.returncode != 0
        assert "disagree" in result.stderr
        # Nothing was touched -- a refused bump leaves both homes as it found them.
        assert _declared_version(pyproject) == "1.2.3"
        assert _imported_version(init) == "1.2.2"
