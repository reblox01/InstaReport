"""The CI claims in the README, checked against the files they describe.

The README's development section now states what CI does: which interpreters the
matrix runs, that the container gate has its own job, that one job drives the
*installed* console script, and that the release is cut by hand with the
sanctioned version script. Those are claims about files under ``.github/``, and
this file is what keeps the claims and the files from drifting apart.

Deliberately dependency-free. PyYAML is not a dev dependency, and a gate that
needs one is a gate that silently skips on the machine without it -- the exact
failure mode this project writes gates to avoid. The checks are structural
(is this version named, does this job exist, is the number moved by the one
script allowed to move it) rather than a full schema, because a schema for
someone else's file format is upkeep this repository does not need. What it
needs is that the claims stay true.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CI = _REPO / ".github" / "workflows" / "ci.yml"
_RELEASE = _REPO / ".github" / "workflows" / "release.yml"
_BUMP = _REPO / ".github" / "scripts" / "bump_version.py"
_README = _REPO / "README.md"

#: The interpreters the README says the matrix runs. Kept here rather than read
#: out of the workflow on purpose: these are the versions claimed *supported*,
#: and the assertion is that the workflow honours that claim, not that the
#: workflow agrees with itself.
_SUPPORTED = ("3.11", "3.12", "3.13")


class TestTheReadmeNamesTheWorkflowsItDescribes:
    def test_every_file_the_readme_points_at_exists(self):
        assert _CI.is_file(), "ci.yml is missing"
        assert _RELEASE.is_file(), "release.yml is missing"
        assert _BUMP.is_file(), "the version bump script is missing"

        readme = _README.read_text(encoding="utf-8")
        for path in (
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/scripts/bump_version.py",
        ):
            assert path in readme, f"the README no longer mentions {path}"


class TestTheMatrixRunsTheClaimedInterpreters:
    def test_ci_names_every_version_the_readme_claims(self):
        ci = _CI.read_text(encoding="utf-8")
        readme = _README.read_text(encoding="utf-8")
        for version in _SUPPORTED:
            assert f'"{version}"' in ci, (
                f"the README claims CI runs python {version}, but ci.yml does "
                "not name it -- widen the matrix or narrow the claim"
            )
            assert version in readme, (
                f"ci.yml runs python {version}, but the README's CI paragraph no "
                "longer names it -- the prose and the matrix have drifted"
            )

    def test_the_container_gate_and_the_cli_smoke_test_are_separate_jobs(self):
        ci = _CI.read_text(encoding="utf-8")
        # The README promises the image build does not delay the fast suite, and
        # that a distinct job is what touches the installed artifact.
        assert re.search(r"^\s{2}container:\s*$", ci, re.MULTILINE), (
            "ci.yml no longer defines the container gate as its own job"
        )
        assert re.search(r"^\s{2}cli:\s*$", ci, re.MULTILINE), (
            "ci.yml no longer defines the installed-artifact job as its own job"
        )
        assert '-m docker' in ci or '-m "docker"' in ci

    def test_the_installed_console_script_is_driven_directly(self):
        ci = _CI.read_text(encoding="utf-8")
        # The claim is specifically that the *installed* script answers these,
        # which is why the invocation is by name and not `python -m`.
        assert "insta-report --help" in ci
        assert "insta-report --version" in ci


class TestTheReleaseIsCutTheWayTheReadmeSays:
    def test_it_is_dispatched_by_hand_and_not_on_push(self):
        release = _RELEASE.read_text(encoding="utf-8")
        assert "workflow_dispatch:" in release, (
            "the README says a release is a decision someone makes"
        )
        assert not re.search(r"^\s{2}push:\s*$", release, re.MULTILINE), (
            "a release triggered on push is the side effect the README says it "
            "is not"
        )

    def test_the_number_moves_only_through_the_bump_script(self):
        release = _RELEASE.read_text(encoding="utf-8")
        assert "bump_version.py" in release, (
            "the release no longer moves the version with the sanctioned script"
        )

    def test_the_irreversible_step_can_be_rehearsed(self):
        release = _RELEASE.read_text(encoding="utf-8")
        assert "dry_run" in release, (
            "the release lost its rehearsal flag, so the only way to test the "
            "tag-and-publish path is to do it"
        )
