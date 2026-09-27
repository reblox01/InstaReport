"""T13: the gates that keep the repository honest.

Three of these exist because the thing they check was already broken. That is
worth stating up front, because a gate written to match current behaviour is
worthless, and a gate written after a real failure tends to be written narrowly
-- to catch that one failure rather than the class.

    * The golden DOM corpus was invisible to git. ``.gitignore`` ended its
      runtime-state section with ``*.html``, and an unscoped extension rule
      matches every file of that name everywhere. All seven golden fixtures and
      one corpus fixture were therefore untracked, and not even reported as
      untracked. The drift suite passed on the machine that wrote them and broke
      on a fresh clone: 27 of 36 tests. A module docstring claimed "a fixture is
      committed to git" and the repository disagreed.

    * ``config.toml`` was not ignored. The tool refuses a data directory inside
      the repo -- ``assert_outside_repo`` raises -- which is the right guard and
      made the artifact rules look unnecessary. But an operator's *config* is
      meant to live in the repo, next to the example it was copied from, and it
      holds a live ``sessionid``. So the one credential location an operator is
      actively invited to create was the one place ``git add -A`` would happily
      stage. The paths that hold secrets are not the paths the tool writes to.

    * Line endings were normalised in the repository but not in the working
      tree, so every ``git add`` on Windows emitted a warning. A warning on all
      traffic is a warning nobody reads.

Everything here is offline and hermetic. The ``git add -A`` gate builds its own
scratch repository from the real ``.gitignore`` rather than staging anything in
the checkout, so running the suite can never commit, unstage, or modify the
working tree. That is the whole reason it is written this way: a hygiene test
that mutates the repository it is policing is a test that eventually commits
something.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GITIGNORE = REPO / ".gitignore"
GITATTRIBUTES = REPO / ".gitattributes"
GOLDEN = Path("tests") / "golden"
CORPUS = Path("tests") / "corpus"


# ===========================================================================
# Talking to git
# ===========================================================================


def git(*args: str, cwd: Path) -> str:
    """Run git, returning stdout. Fails loudly -- a silent git is a fake gate."""
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if done.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({done.returncode}):\n"
            f"{done.stdout}\n{done.stderr}"
        )
    return done.stdout


def is_checkout() -> bool:
    return (REPO / ".git").exists()


needs_git = pytest.mark.skipif(
    not shutil.which("git"), reason="git is not on PATH"
)
# A source distribution has no .git, and every check here is about repository
# *state* rather than about the code. Skipping is correct there. Skipping is NOT
# correct in a checkout -- which is why this is a skip and not a silent pass.
needs_checkout = pytest.mark.skipif(
    not is_checkout(), reason="not a git checkout; repository state is unavailable"
)


def tracked(*paths: str) -> set[str]:
    out = git("ls-files", "-z", "--", *paths, cwd=REPO)
    return {p for p in out.split("\0") if p}


def untracked_and_visible(*paths: str) -> set[str]:
    """Files git would stage but has never been told about.

    ``--others --exclude-standard`` deliberately omits ignored files: the whole
    failure was files invisible in *both* senses, so a check that looks only at
    this list would not have seen them. The other half is below.
    """
    out = git(
        "ls-files", "-z", "--others", "--exclude-standard", "--", *paths, cwd=REPO
    )
    return {p for p in out.split("\0") if p}


def ignored(*paths: str) -> set[str]:
    out = git("ls-files", "-z", "--others", "--ignored", "--exclude-standard",
              "--", *paths, cwd=REPO)
    return {p for p in out.split("\0") if p}


# ===========================================================================
# 1. The fixtures must be in the repository
# ===========================================================================


class TestTheFixtureCorpusIsActuallyInTheRepository:
    """The gate for the failure that started this.

    A test suite that reads a committed fixture is making a claim about the
    repository: that the file it is about to open exists in the thing other
    people clone. The claim was false, and every offline test that read a
    golden page was quietly testing the author's working copy instead.
    """

    @needs_git
    @needs_checkout
    def test_every_golden_fixture_is_tracked(self):
        on_disk = {
            p.relative_to(REPO).as_posix() for p in (REPO / GOLDEN).glob("*.html")
        }
        assert on_disk, "no golden fixtures on disk at all -- has the dir moved?"
        missing = on_disk - tracked(GOLDEN.as_posix())
        assert not missing, (
            "golden fixtures exist on this machine but are not in git:\n  "
            + "\n  ".join(sorted(missing))
            + "\n\nThey pass here and fail on every fresh clone. Either add them"
            " or delete them; an untracked fixture is worse than none, because"
            " the suite claims to have tested it."
        )

    @needs_git
    @needs_checkout
    def test_no_fixture_is_silently_ignored(self):
        """The specific shape of the bug: invisible to *both* senses.

        Tracked-but-modified and untracked-but-listed would both have shown up
        in ``git status``. These showed up nowhere, which is why eleven
        commits went by without anyone noticing a suite that could not run
        anywhere but here.
        """
        on_disk = {
            p.relative_to(REPO).as_posix()
            for p in (REPO / GOLDEN).glob("*")
        } | {p.relative_to(REPO).as_posix() for p in (REPO / CORPUS).glob("*")}
        hidden = on_disk & ignored(GOLDEN.as_posix(), CORPUS.as_posix())
        assert not hidden, (
            "fixture files are being ignored:\n  "
            + "\n  ".join(sorted(hidden))
            + "\n\nAn unscoped rule like `*.html` matches committed fixtures too."
            " Scope artifact rules by path -- the tool refuses a data_dir inside"
            " the repo anyway, so they are not the guard."
        )

    @needs_git
    @needs_checkout
    def test_nothing_under_tests_is_left_unstaged(self):
        """A source file that exists only locally is a test that only runs here."""
        stray = untracked_and_visible("tests")
        assert not stray, (
            "untracked files under tests/:\n  " + "\n  ".join(sorted(stray))
        )

    def test_every_file_the_corpus_manifest_names_exists(self):
        """Independent of git: the manifest must not name a file that is absent.

        This is the check that would have caught the missing corpus fixture
        without consulting git at all, and it is worth having *because* it does
        not depend on git -- it holds in a sdist, in a zip, anywhere.
        """
        import tomllib

        manifest = REPO / CORPUS / "manifest.toml"
        cases = tomllib.loads(manifest.read_text(encoding="utf-8"))["case"]
        assert len(cases) >= 10, f"corpus shrank to {len(cases)} cases"
        for case in cases:
            named = case["file"]
            path = manifest.parent / named
            assert path.is_file(), (
                f"corpus case {case['name']!r} names {named!r}, which does not "
                f"exist. A case that cannot be loaded is not a case."
            )


# ===========================================================================
# 2. Credentials
# ===========================================================================

#: Values that look like credentials and provably are not.
#:
#: Each is a redaction test's own fixture, and a redaction test *needs* a value
#: shaped like a real one -- that is the entire point. So the shape stays and the
#: value stays fake. They are listed here individually rather than matched by a
#: loose pattern, because an allowlist that grows by pattern is an allowlist
#: that eventually matches a real cookie.
SAFE_LITERALS = frozenset(
    {
        "sessionid%3AAbCdEf-1234567890abcdef",  # tests/test_logging_pipeline.py
        "sessionid=abcdef1234567890XYZ",  # tests/test_redaction.py
        "sessionid%3Anever-log-this-value",  # tests/test_accounts.py
        # tests/test_probe.py. A whole sessionid *value* with no cookie name in
        # front of it -- which is what register_secret() is handed. One entry
        # rather than four: the tests share a constant precisely so this
        # allowlist cannot grow one near-identical entry per test, since an
        # allowlist that grows that way is an allowlist a real cookie can walk
        # into by changing one character.
        "10000000001%3A3Qxe4Kp3ze0djU%3A0%3AAYkQ5dJ9fake",
    }
)

#: Shapes that indicate a live credential.
#:
#: Narrow on purpose, and the narrowness is calibrated against the repository's
#: own contents rather than against a theory of what credentials look like. The
#: first cut of this scanned for ``sessionid=`` followed by eight word characters
#: and immediately flagged ``sessionid=sessionid`` in config.py and
#: ``sessionid=entry.sessionid`` in cli.py -- both keyword arguments naming a
#: local variable, neither a cookie. A gate that fires on the project's own
#: source is a gate that gets switched off, and a switched-off credential gate is
#: worse than none because it is still there looking like a control.
#:
#: So each pattern requires the thing that distinguishes a generated token from
#: an identifier: length, and character-class mixing that a hand-written
#: placeholder does not have. This is a backstop against the common accident,
#: not proof of absence -- a short real password pasted into a docstring would
#: pass. It cannot be otherwise without a scanner that understands which
#: strings are load-bearing, and that is a different tool with its own false
#: positives.
CREDENTIAL_PATTERNS = (
    # Instagram session cookie. A real value is a long mixed-case token, so the
    # lookaheads demand length, a digit, and case mixing. A dotted attribute path
    # or a bare identifier is excluded outright.
    re.compile(
        r"sessionid(?:%3A|=)(?![A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b)"
        r"(?=[A-Za-z0-9%_\-.]{24,})(?=[^\s]*[0-9])(?=[^\s]*[a-z])(?=[^\s]*[A-Z])"
        r"[A-Za-z0-9%_\-]+"
    ),
    # Proxy URL with an inline password. Twelve characters minimum: every
    # word-like placeholder in the repository ("pass", "p", "supersecret") is
    # shorter, and every generated password is longer.
    re.compile(
        r"(?:https?|socks5?)://[^\s/:@]{1,64}:[^\s/@]{12,}@"
    ),
    # Provider API keys, as they actually look.
    re.compile(
        r"\b(?:api[_-]?key|apikey|access[_-]?token|secret)\s*[=:]\s*"
        r"[\"'][A-Za-z0-9_\-]{16,}[\"']",
        re.IGNORECASE,
    ),
    # An Instagram sessionid VALUE with no cookie name in front of it.
    #
    # Added after the repository shipped one. Every other pattern needs a name
    # to anchor on -- ``sessionid=``, ``apikey:``, ``scheme://user:pass@`` -- and
    # a test fixture does not have a name: it holds the bare value, because that
    # is what register_secret() takes. So the shape that actually occurs in
    # practice was the one shape nothing matched, and a real cookie pasted into a
    # fixture would have passed this whole gate silently.
    #
    # The shape is structural rather than guessed: a leading ``ds_user_id``
    # digit run, then %3A-separated segments, then the length and character-class
    # mixing that separates a generated token from ``id:part:part``. A config
    # value like ``user_id = "61214264580"`` is a bare digit run with no %3A, so
    # it does not match -- which is correct, since a user id is not a secret.
    re.compile(
        r"\b\d{4,}%3A(?=[A-Za-z0-9%]{16,})(?=[^\s]*[0-9])(?=[^\s]*[a-z])"
        r"(?=[^\s]*[A-Z])[A-Za-z0-9%]+\b"
    ),
)


def credential_hits(text: str) -> list[tuple[str, str]]:
    """Every ``(pattern-name, matched-literal)`` in *text*, minus known fakes."""
    hits: list[tuple[str, str]] = []
    for pattern in CREDENTIAL_PATTERNS:
        for match in pattern.finditer(text):
            literal = match.group(0)
            if literal in SAFE_LITERALS:
                continue
            hits.append((pattern.pattern[:40], literal))
    return hits


class TestNoCredentialIsCommitted:
    """``.gitignore`` prevents the *tool* from leaking. It cannot prevent a
    developer pasting a real cookie into a test fixture, because a test fixture
    is a source file and source files are supposed to be committed.

    So this is a content scan of everything git tracks, not a check of the ignore
    rules. The two cover different leaks and neither substitutes for the other.
    """

    @needs_git
    @needs_checkout
    def test_no_tracked_file_contains_something_credential_shaped(self):
        offenders: list[str] = []
        for name in sorted(tracked()):
            path = REPO / name
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # Binary, or unreadable. Nothing to scan; the .gitattributes
                # binary rules are what keep that from being a surprise.
                continue
            for _, literal in credential_hits(text):
                line = next(
                    (
                        i
                        for i, row in enumerate(text.splitlines(), start=1)
                        if literal in row
                    ),
                    0,
                )
                offenders.append(f"{name}:{line}: {literal[:60]}")
        assert not offenders, (
            "committed files contain credential-shaped values:\n  "
            + "\n  ".join(offenders)
            + "\n\nIf these are real, revoke them -- they are in the history, so"
            " deleting the line is not enough. If they are fixtures, add the"
            " literal to SAFE_LITERALS in this file with a comment saying which"
            " test needs it, so the next reader knows it was a decision."
        )

    def test_a_bare_sessionid_value_is_caught_though_it_has_no_cookie_name(self):
        """The shape that occurs in practice, and used to occur nowhere.

        Every other pattern anchors on a *name*. A fixture does not have one:
        it holds the value, because that is what ``register_secret`` takes. So
        this repository shipped a real account id paired with a fake secret for
        the whole life of the API work, and the gate was green throughout --
        there was no secret in it, only an identifier nobody meant to publish.
        """
        # A real sessionid, stripped of its ``sessionid=`` prefix -- which is
        # exactly what it looks like by the time it is a fixture.
        #
        # Assembled from fragments on purpose. This file is tracked, so the
        # scanner scans this line too, and a contiguous literal here would be
        # its own false positive. Which is the correct behaviour: the gate
        # flagged the test that documents the gate, and the fix is to not commit
        # the thing being hunted rather than to allowlist the hunter.
        bare = "61214264580%3A" "3Qxe4Kp3ze0djU%3A0%3A" "AYkQ5dJ9fakeXk2"
        assert credential_hits(f'cookie = "{bare}"'), (
            "a bare sessionid value is not matched by any pattern"
        )
        # With the cookie name in front of it, the older pattern still fires --
        # the new one is additive, not a replacement.
        assert credential_hits(f'"Cookie": "sessionid={bare}"'), (
            "the named form regressed"
        )

    def test_the_new_pattern_does_not_fire_on_a_bare_user_id(self):
        """A ``user_id`` is public: it is in every profile URL.

        Firing on it would train the reader to ignore this gate, and a gate that
        cries wolf over the project's own source is a gate that gets switched
        off. The separator is what tells the two apart, and the test pins that
        rather than assuming it.
        """
        assert not credential_hits('user_id = "61214264580"')
        assert not credential_hits('user_id = "61214264580:placeholder"')
        # ...and the keyword-argument shapes the first cut of this scanner
        # wrongly flagged, still not flagged.
        assert not credential_hits("cookie = sessionid")
        assert not credential_hits("headers['cookie'] = sessionid")

    def test_the_allowlist_has_not_grown_to_a_pattern(self):
        """A cheap tripwire on this file's own escape hatch.

        SAFE_LITERALS is the one place a real credential could hide, so it is
        worth a test that says what it is for. Without a bound, "just add it to
        the allowlist" is the path of least resistance and the gate decays into
        documentation.
        """
        assert len(SAFE_LITERALS) <= 8, (
            f"SAFE_LITERALS has grown to {len(SAFE_LITERALS)} entries. Each one"
            " is a decision that a credential-shaped string in the repository is"
            " not a credential. Past a handful, the gate is decoration."
        )
        # And every allowlisted literal must actually occur, so a stale entry
        # cannot quietly widen the exemption.
        for literal in SAFE_LITERALS:
            found = any(
                literal in (REPO / name).read_text(encoding="utf-8", errors="replace")
                for name in tracked()
                if (REPO / name).is_file()
            ) if is_checkout() else True
            assert found, f"SAFE_LITERALS names {literal!r}, which is not used"


class TestGitAddDashAStagesNoSecret:
    """The gate T13 was written for, and the one that cannot be faked.

    It builds a scratch repository from the *real* ``.gitignore`` and the *real*
    ``.gitattributes``, plants a credential at every location a plausible
    operator would put one, runs ``git add -A``, and reads back what was staged.

    Using the real ignore file is the point. A test that reimplements the rules
    in Python is testing its own reimplementation, and the day the rules change
    the test keeps passing against a version that no longer exists.

    And it is a scratch repository, always. A version of this test that staged
    files in the checkout would be one bad day away from committing a real
    cookie to prove it could not.
    """

    #: Obviously fake, obviously a session cookie, so a match can only mean a
    #: real rule failure rather than a lucky one.
    SECRET = "sessionid%3Aplanted-by-the-git-add-test-0123456789abcdef"

    # (relative path, template). Every one of these is a place a real operator
    # would genuinely put a credential -- not a set invented to be convenient.
    PLANTED = (
        # The operator's own config, copied from the example next to it. This is
        # the most likely leak in the entire project, because the config is
        # *meant* to sit in the repo and this is the one place holding a live
        # session that the tool does not control the location of.
        ("config.toml", '[accounts.alpha]\nusername = "alpha"\nsessionid = "{s}"\n'),
        ("config.local.toml", '[accounts.alpha]\nsessionid = "{s}"\n'),
        # Secrets by the names tools and humans actually use.
        (".env", f"SESSIONID={SECRET}\n"),
        (".env.production", f"SESSIONID={SECRET}\n"),
        ("secrets/accounts.toml", f'sessionid = "{SECRET}"\n'),
        ("credentials/alpha.json", f'{{"sessionid": "{SECRET}"}}\n'),
        # Runtime output. Reachable only by pointing data_dir at the repo, which
        # assert_outside_repo refuses -- but the refusal is in the tool, and this
        # is the layer below it.
        ("artifacts/run-1/page.html", f"<html><!-- {SECRET} --></html>\n"),
        ("artifacts/run-1/shot.png", f"\x89PNG\r\n\x1a\n{SECRET}\n"),
        ("traces/run-1/trace.zip", f"PK\x03\x04{SECRET}\n"),
        ("state/run-1/checkpoint.jsonl", f'{{"sessionid": "{SECRET}"}}\n'),
        ("runs/run-1/checkpoint.jsonl", f'{{"sessionid": "{SECRET}"}}\n'),
        ("checkpoints/run-1.jsonl", f'{{"sessionid": "{SECRET}"}}\n'),
    )

    @pytest.fixture
    def scratch(self, tmp_path):
        """A throwaway repository that is not this one."""
        repo = tmp_path / "scratch"
        (repo / ".git").mkdir(parents=True)
        git("init", "-q", cwd=repo)
        git("config", "user.email", "gate@example.invalid", cwd=repo)
        git("config", "user.name", "gate", cwd=repo)
        # The real files, byte for byte. Copied rather than referenced by path
        # so the test cannot be affected by --git-dir or a worktree layout.
        shutil.copy2(GITIGNORE, repo / ".gitignore")
        shutil.copy2(GITATTRIBUTES, repo / ".gitattributes")
        # One tracked file, so `git add -A` has a baseline and the staged set is
        # not trivially empty. Also proves source files are still stageable --
        # a gate that staged nothing would pass for the wrong reason.
        (repo / "README.md").write_text("# scratch\n", encoding="utf-8")
        git("add", "-A", cwd=repo)
        return repo

    @pytest.fixture
    def staged(self, scratch):
        """Plant everything, ``git add -A``, and return the staged contents."""
        for name, template in self.PLANTED:
            path = scratch / name
            path.parent.mkdir(parents=True, exist_ok=True)
            body = (
                template.format(s=self.SECRET) if "{s}" in template else template
            )
            path.write_text(body, encoding="utf-8", newline="")
        git("add", "-A", cwd=scratch)
        # Read every staged blob back out of the index, so the assertion is on
        # the bytes git actually holds rather than on what we think we wrote.
        names = git("diff", "--cached", "--name-only", cwd=scratch).splitlines()
        blobs = [(name, git("show", f":{name}", cwd=scratch)) for name in names]
        return names, blobs

    def test_no_planted_credential_is_staged(self, staged):
        names, blobs = staged
        leaked = [name for name, body in blobs if self.SECRET in body]
        assert not leaked, (
            "these files would be committed with a live credential in them:\n  "
            + "\n  ".join(sorted(leaked))
            + "\n\nEverything else passing is not a pass: the operator's own"
            " config.toml is not something a rule scoped to artifacts/ will"
            " ever match."
        )

    def test_the_scratch_repository_did_stage_something(self, staged):
        """Guards the test above against passing for the wrong reason.

        If the ignore rules were so broad that nothing at all was stageable, the
        leak assertion would hold trivially -- an ignore-everything policy leaks
        nothing and is useless. So the gate also asserts that ordinary source
        files still reach the index.
        """
        names, _ = staged
        assert names, "nothing was staged at all; the scratch repo is broken"
        for source_file in ("README.md", ".gitignore", ".gitattributes"):
            assert source_file in names, (
                f"{source_file} did not stage. The ignore rules are too broad:"
                " a policy that blocks everything also blocks every credential"
                " and is not a policy, it is an accident."
            )

    def test_a_source_file_still_stages(self, scratch):
        """A fixture with an .html extension must be committable.

        Written as its own test because it is the exact regression: the
        artifact rules used to end in ``*.html``, which made every committed
        HTML fixture unstageable. The rule that keeps a screenshot out of the
        repository must not also keep a test fixture out of it.
        """
        fixture = scratch / GOLDEN / "report_dialog.html"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("<html><body>Report</body></html>\n", encoding="utf-8")
        git("add", "-A", cwd=scratch)
        names = git("diff", "--cached", "--name-only", cwd=scratch).splitlines()
        assert "tests/golden/report_dialog.html" in names, (
            "a committed HTML fixture cannot be staged. Something in .gitignore"
            " is matching by extension rather than by path -- that is how the"
            " golden corpus went missing."
        )

    @needs_git
    @needs_checkout
    def test_the_real_repository_would_not_stage_a_config_toml(self, tmp_path):
        """The same question, asked of this repository rather than a copy.

        Cheap, and it fails at the moment someone adds a rule that reopens this
        rather than waiting for the scratch gate to be re-run and misread.
        """
        verdict = subprocess.run(
            ["git", "check-ignore", "-q", "config.toml"],
            cwd=REPO,
            capture_output=True,
        )
        # check-ignore exits 0 when the path *is* ignored, 1 when it is not, and
        # 128 on error. The error case must not read as a pass.
        assert verdict.returncode == 0, (
            f"config.toml is not ignored (git check-ignore exited "
            f"{verdict.returncode}). It is the file an operator copies from"
            " config.example.toml, it lives in the repo by design, and it holds"
            " a live sessionid -- so `git add -A` would stage the credential."
        )


# ===========================================================================
# 3. Line endings
# ===========================================================================


class TestLineEndingsAreNormalised:
    """Checked against the *index*, not the working tree.

    That distinction is the whole test. A working-tree check would fail on
    every Windows checkout by construction and pass on every Linux one, so it
    would be measuring the platform rather than the repository. The index is
    where the committed bytes live and it is the same everywhere.
    """

    def test_gitattributes_exists(self):
        assert GITATTRIBUTES.is_file(), (
            "without .gitattributes, git guesses per file whether it is text and"
            " normalises inconsistently -- which is how a file ends up with LF"
            " in the repository and CRLF in the working tree, and a drift test"
            " that compares committed bytes starts failing on one platform only"
        )

    def test_it_normalises_text_and_declares_binaries(self):
        text = GITATTRIBUTES.read_text(encoding="utf-8")
        assert "text=auto" in text, "no general normalisation rule"
        assert "eol=lf" in text, (
            "no working-tree rule, so a Windows checkout gets CRLF while the"
            " repository has LF"
        )
        for binary in ("*.png", "*.zip"):
            assert re.search(rf"^{re.escape(binary)}\s+binary", text, re.M), (
                f"{binary} is not declared binary. Line-ending translation turns"
                " a trace zip into a corrupt trace zip, and 'corrupt' sends"
                " someone hunting for a bug in the capture code."
            )

    @needs_git
    @needs_checkout
    def test_no_tracked_file_has_crlf_in_the_index(self):
        """``i/lf`` is index-with-LF, ``i/crlf`` is the failure.

        Asked of git rather than recomputed, because the question is what git
        will hand someone on clone -- not what this working copy happens to
        contain.
        """
        report = git("ls-files", "--eol", cwd=REPO)
        offenders = [
            line
            for line in report.splitlines()
            if re.search(r"\bi/(crlf|mixed)\b", line)
        ]
        assert not offenders, (
            "these tracked files carry CRLF in the index:\n  "
            + "\n  ".join(offenders[:20])
            + "\n\nFix with `git add --renormalize .` and commit."
        )


# ===========================================================================
# 4. The container
# ===========================================================================


class TestTheDockerfileCannotDrift:
    """A Dockerfile is a second manifest of the dependencies, and nothing makes
    the two agree.

    The usual shape of the drift: a dependency is added to ``pyproject.toml``
    because the code needs it, the image is not rebuilt, the image is not
    rebuilt *loudly* either, and the failure appears later as an ``ImportError``
    in a container rather than as a failing test on a machine. The
    ``--no-deps`` install below is what makes the gap possible at all, so this
    is checked rather than trusted.
    """

    DOCKERFILE = REPO / "Dockerfile"

    def test_it_exists_and_ships_no_secret(self):
        text = self.DOCKERFILE.read_text(encoding="utf-8")

        # An ARG or ENV carrying a credential is permanent. It is in the image,
        # in the build cache, in `docker history`, and in anything the image is
        # pushed to -- and this repository is public, so a pushed image is a
        # published secret. Declared-and-empty is the correct shape, and the
        # assignment-with-no-value below is what makes that explicit.
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("ARG ", "ENV ")):
                continue
            for name in ("SESSIONID", "PASSWORD", "SECRET", "TOKEN", "API_KEY"):
                if name in stripped.upper():
                    assert "=" not in stripped or stripped.endswith("="), (
                        f"Dockerfile assigns a {name} into a build layer:\n"
                        f"  {stripped}\n"
                        "A credential passed as a build argument or as ENV is"
                        " permanent, inspectable and pushed with the image."
                        " Supply it at run time via env_file instead."
                    )

    def test_its_pinned_dependencies_are_exactly_the_runtime_ones(self):
        import tomllib

        with (REPO / "pyproject.toml").open("rb") as handle:
            declared = {
                spec.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
                for spec in tomllib.load(handle)["project"]["dependencies"]
            }

        text = self.DOCKERFILE.read_text(encoding="utf-8")
        pinned = set(re.findall(r'^\s*"([A-Za-z0-9_.-]+)[=<>]', text, re.M))
        pinned = {name.lower() for name in pinned}

        assert declared == pinned, (
            "the Dockerfile installs a different set of runtime dependencies"
            " than pyproject declares.\n"
            f"  only in pyproject: {sorted(declared - pinned)}\n"
            f"  only in Dockerfile: {sorted(pinned - declared)}\n"
            "Add it to both, or the image and the metadata have stopped"
            " describing the same program."
        )

    def test_it_installs_the_package_without_deps_against_that_pinned_set(self):
        text = self.DOCKERFILE.read_text(encoding="utf-8")
        assert "--no-deps" in text, (
            "the image installs the package with --no-deps so the pins above"
            " hold. Without it, pip re-resolves the dependency tree and quietly"
            " replaces every pin with whatever it picks."
        )

    def test_the_image_never_bakes_the_operators_config_or_proxy_list(self):
        """The context is sent to the daemon whole, before any COPY runs.

        So this is about ``.dockerignore`` as much as about the Dockerfile: a
        file excluded from every ``COPY`` still arrives in the build context and
        lands in a layer the daemon holds. The distinction between "not in the
        final image" and "never transmitted" is the whole point of the file.

        Rules are read as *active rules*, not as substrings. Asserting the
        pattern appears in the text is satisfied by commenting the line out,
        which is the shape this very file's own ``.gitignore`` warns about:
        a rule that looks present and excludes nothing.
        """
        active = {
            line.strip()
            for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for pattern, why in (
            ("config.toml", "names real accounts and the env var holding the sessionid"),
            ("proxies.txt", "a paid resource, and the addresses are the point of the tool"),
            (".git/", "five commits in the history carry the operator's real account id"),
        ):
            assert pattern in active, (
                f".dockerignore has no active rule for {pattern} -- {why}\n"
                "Active rules are: " + ", ".join(sorted(active)[:12])
            )

    def test_nothing_the_tool_writes_to_is_in_the_build_context(self):
        """A ledger inside a layer is a ledger nobody can rotate.

        Listed by what the code *writes*, not by what a reader guesses might
        matter. The first draft of this file used a count threshold instead --
        "at least N active rules" -- and it was decoration: the file has 42, so
        deleting 30 of them still passed. A count is not a property of anything;
        this list is, and adding a place the tool writes to means adding it
        here too.
        """
        active = {
            line.strip()
            for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for written, what in (
            ("artifacts/", "redacted screenshots, DOM diffs, traces"),
            ("traces/", "Playwright trace archives"),
            ("checkpoints/", "the resume ledger"),
            ("runs/", "per-run directories"),
            ("state/", "account and target state"),
            ("data/", "the data directory, under whatever name it resolves to"),
        ):
            assert written in active, (
                f".dockerignore does not exclude {written} -- {what}. A ledger"
                " baked into an image outlives the run that produced it and is"
                " not covered by the credential scan, which reads the work tree."
            )

    def test_the_compose_file_reads_its_secret_from_outside_the_repository(self):
        text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")

        block = re.search(r"env_file:\s*\n((?:\s+-\s*\S.*\n?)+)", text)
        assert block, (
            "the sessionid has to reach the container from somewhere. A file"
            " outside the repository is the only route that keeps it out of git,"
            " out of the image, and out of the build cache -- and env_file is"
            " the only such route compose offers."
        )

        items = re.findall(r"-\s*(\S+)", block.group(1))
        assert items, "env_file is present but empty."

        for item in items:
            # ${VAR:-fallback} -> fallback. Read the fallback, because that is
            # the path an operator gets who has not set the variable -- and the
            # fallback is the one that silently lives inside the checkout.
            fallback = item.split(":-", 1)[-1].rstrip("}")
            # resolved, not string-tested: `REPO / "a/../b"` is not a normalised
            # Path, so is_relative_to answers about the un-normalised form and
            # a `..` that leaves the tree can look like it stays.
            assert not (REPO / fallback).resolve().is_relative_to(REPO), (
                f"the default env_file path {fallback!r} resolves to inside the"
                " checkout"
            )
            assert fallback.startswith(("../", "/", "~")), (
                f"the default env_file path is {fallback!r}. It should be one"
                " level up from the repository, or an absolute path outside it."
                " A secrets file under the checkout is a secrets file that the"
                " next `git add -A` commits."
            )

    def test_the_documented_vps_override_form_is_the_one_the_test_accepts(self):
        """The README's escape hatch and the gate that reads it must agree.

        The check above allows an absolute path, because a VPS has no parent
        directory to be one level up from and the README tells operators to
        supply one. That makes the absolute form a feature, and a feature with
        no positive test is a feature that quietly stops working while the
        refusal still passes. So the documented string is asserted to be the
        documented string.
        """
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")

        assert "INSTA_REPORT_ENV_FILE=" in readme, (
            "the README no longer documents how to point the container at a"
            " secrets file on a second host, which is the only host where the"
            " container is worth using"
        )
        assert "/run/secrets" in readme, (
            "the documented override is not an absolute path, so it would be"
            " refused by the same rule that keeps the secrets file out of the"
            " checkout"
        )
        # The variable must be the one compose reads, or the override is a
        # sentence in a README that does nothing.
        assert "INSTA_REPORT_ENV_FILE" in compose, (
            "the README documents INSTA_REPORT_ENV_FILE but compose reads a"
            " different name, so the documented override is silently ignored"
        )

    def test_the_compose_file_never_defines_the_sessionid_inline(self):
        """The failure mode is an ``environment:`` entry, not a stray mention.

        A comment saying "the sessionid comes from env_file" is the intent. An
        ``IG_SESSIONID_ALPHA: <value>`` line is a cookie in a tracked file, and
        it is one keystroke from being written. So the test matches a *definition*
        -- a name followed by a YAML ``:`` or an ``=`` -- and ignores prose.
        """
        text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        defines = re.compile(r"^\s*(?:-\s*)?\w*sessionid\w*\s*[:=]", re.IGNORECASE)

        offenders = [
            line
            for line in text.splitlines()
            if not line.strip().startswith("#") and defines.match(line)
        ]
        assert not offenders, (
            "docker-compose.yml defines the sessionid inline:\n  "
            + "\n  ".join(offenders)
            + "\n\nThis file is tracked. Use env_file."
        )
