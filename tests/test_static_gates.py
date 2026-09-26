"""T13: static analysis, run as tests rather than as a step someone remembers.

The argument for this file is narrow and worth stating, because "just run mypy"
is a reasonable position and this is not a refutation of it -- it is a
refutation of running it *only* by hand.

A check that lives in a README is a check that is skipped on the day somebody is
in a hurry, on a machine where it was not installed, or in a change made from a
laptop with a stale checkout. Every defect this repository has actually shipped
was invisible to a check that nobody had run yet. A check that runs as part of
the suite cannot be skipped without also skipping the tests, and the two are
skipped together on purpose.

Two properties keep this from being theatre:

  * Each gate is verified to have teeth here, not assumed to. Both a genuine
    failure and a deliberately introduced one are exercised, because a lint
    gate that has only ever seen a clean tree is indistinguishable from a lint
    gate that always passes.

  * A missing tool is a *skip with a reason*, and the reason is asserted to be
    in the dev dependencies. So on a machine without mypy the execution is
    skipped honestly rather than silently, and a change to pyproject that drops
    the dependency still fails -- unconditionally, on every machine, with no
    tools required. That split is deliberate: the declaration is gated hard, the
    execution is gated softly.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "insta_report"

pytestmark = pytest.mark.static


def dev_dependencies() -> set[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return {d.split(">=")[0].split("==")[0].strip().lower() for d in
            data["project"]["optional-dependencies"]["dev"]}


def run_tool(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def require(module: str) -> None:
    """Skip unless *module* is installed, naming where to get it.

    ``importlib.util.find_spec`` rather than ``import``. An import probe is
    itself an import, and this file is linted by the gate it guards -- so
    ``try: import pyflakes`` registers as a used import while ``# noqa: F401``
    on the line above does nothing, because pyflakes does not read noqa. The
    gate failed on its own source, which is at least a demonstration that it
    works.

    find_spec also has the right semantics for the question being asked. We do
    not want to *import* mypy to check that it is installed; importing a type
    checker is slow, has side effects on its own caches, and is not the same
    question as "will ``python -m mypy`` run".
    """
    if importlib.util.find_spec(module) is None:
        pytest.skip(
            f"{module} is not installed; it is declared in"
            " [project.optional-dependencies].dev in pyproject.toml"
        )


# ===========================================================================
# The declarations, which need no tools and therefore always run
# ===========================================================================


class TestTheToolsAreDeclared:
    """Independent of what is installed, so it cannot be skipped.

    Everything else in this file might be marked skipped on a machine with no
    mypy. This cannot, which is the point: the failure being guarded against is
    "someone removed the dependency and the check quietly stopped existing", and
    that is exactly the case where the check itself would not be running.
    """

    def test_mypy_is_a_declared_dev_dependency(self):
        assert "mypy" in dev_dependencies(), (
            "mypy is not in [project.optional-dependencies].dev. Every gate in"
            " this file skips without it, so removing the declaration silently"
            " removes the type check on every machine that installs from"
            " pyproject rather than from a hand-built environment."
        )

    def test_pyflakes_is_a_declared_dev_dependency(self):
        assert "pyflakes" in dev_dependencies(), (
            "pyflakes is not in [project.optional-dependencies].dev. It is the"
            " check that found two duplicate test functions shadowing each"
            " other, one of which had therefore never run."
        )

    def test_mypy_is_configured_in_pyproject_not_only_on_the_command_line(self):
        """Configuration that lives only in a shell command is not configuration.

        A developer who runs ``mypy insta_report`` gets different results from
        CI, and the difference is invisible until CI is the one that is wrong.
        """
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        assert "mypy" in data.get("tool", {}), (
            "no [tool.mypy] section. The settings have to be in pyproject so"
            " that every invocation -- local, CI, editor -- checks the same"
            " things."
        )

    @pytest.mark.parametrize(
        "setting",
        [
            "disallow_untyped_defs",
            "disallow_incomplete_defs",
            "no_implicit_optional",
            "warn_unused_ignores",
            "warn_redundant_casts",
        ],
    )
    def test_the_settings_that_found_defects_stay_on(self, setting):
        """Tripwires on the strictness, because strictness gets turned off.

        Every one of these earned its place by catching something, and the
        natural response to a noisy checker is to switch it off. That response
        is correct in general and wrong here, so the settings are asserted
        rather than trusted.
        """
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["tool"]["mypy"].get(setting) is True, (
            f"mypy.{setting} is not enabled. Each of these found a real defect:"
            " unannotated **kwargs hid an unchecked constructor signature, a"
            " stale type: ignore was suppressing nothing, and a cast that does"
            " nothing is dead code or an un-reverted fix."
        )


# ===========================================================================
# The gates
# ===========================================================================


class TestPyflakesIsClean:
    def test_the_package_and_tests_have_no_unused_or_undefined_names(self):
        require("pyflakes")

        done = run_tool(["pyflakes", PACKAGE, "tests"])
        assert done.returncode == 0, (
            "pyflakes reported:\n"
            + (done.stdout + done.stderr).strip()
            + "\n\nThe class it exists for is code that is written, imported and"
            " never reached. It found two duplicate test functions whose names"
            " shadowed each other, so one had never run in the life of the"
            " suite -- a test that has never executed reports the truth about"
            " nothing."
        )

    def test_the_gate_would_notice_a_real_violation(self):
        """A gate that has only ever seen a clean tree is a gate that always passes.

        Runs pyflakes over a file that genuinely has an unused import, in a
        throwaway directory, and requires it to be reported. Costs a
        millisecond and converts "the gate works" from an assumption into a
        fact.
        """
        require("pyflakes")

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.py"
            bad.write_text("import os\n\n\ndef f() -> int:\n    return 1\n",
                           encoding="utf-8")
            done = run_tool(["pyflakes", str(bad)])
            assert done.returncode != 0, (
                "pyflakes exited 0 on a file with an unused import, so a clean"
                " run of this gate means nothing."
            )
            assert "os" in done.stdout + done.stderr, (
                "pyflakes failed but did not name the unused import:\n"
                + done.stdout + done.stderr
            )


class TestMypyIsClean:
    def test_the_package_type_checks(self):
        require("mypy")

        done = run_tool(["mypy"])
        assert done.returncode == 0, (
            "mypy reported:\n"
            + (done.stdout + done.stderr).strip()
            + "\n\nOne of the first things it found was a declaration that lied:"
            " BrowserDriver typed `_anchors: AnchorSet` while defaulting it to"
            " None. Following that lie turned up five methods reading it with no"
            " guard, any of which raised an AttributeError that the runner"
            " grades as CHANNEL_FAILED -- which an operator reads as Instagram"
            " having rejected the session, when nothing had been sent at all."
        )

    def test_the_gate_would_notice_a_real_violation(self):
        require("mypy")

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.py"
            # An unannotated function: exactly what disallow_untyped_defs is for,
            # and the shape of the real defect (an unchecked **kwargs made a
            # whole constructor signature unverifiable).
            bad.write_text("def f(x):\n    return x\n", encoding="utf-8")
            done = run_tool(
                [
                    "mypy",
                    "--disallow-untyped-defs",
                    "--no-incremental",
                    "--cache-dir",
                    str(Path(tmp) / "cache"),
                    str(bad),
                ]
            )
            assert done.returncode != 0, (
                "mypy accepted an unannotated function, so a clean run of this"
                " gate means nothing."
            )
            assert "no-untyped-def" in done.stdout + done.stderr, (
                "mypy failed but not for the reason the gate is checking:\n"
                + done.stdout + done.stderr
            )
