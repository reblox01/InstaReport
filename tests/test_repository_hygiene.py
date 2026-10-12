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

    * The container had never been built, and building it found three separate
      instructions claiming something the file did not do. ``COPY
      config.example.toml`` named a path ``.dockerignore`` excluded under its own
      ``config.*.toml`` rule, so the build stopped there. The comment above that
      COPY said the example points at an anchors file; the example omits
      ``anchors_path`` on purpose. And ``"playwright==${PLAYWRIGHT_VERSION}"``
      expanded to ``playwright==`` -- an ``ARG`` declared before ``FROM`` is
      substituted into the FROM line and is then out of scope for the stage, so
      pip rejected it as an invalid requirement. The test for pinned
      dependencies passed through all three, because it matches the pin's *name*
      in the text and a name is not a version. Reading a Dockerfile as prose
      answers a different question from reading it the way the builder reads it.

Everything here is offline and hermetic. The ``git add -A`` gate builds its own
scratch repository from the real ``.gitignore`` rather than staging anything in
the checkout, so running the suite can never commit, unstage, or modify the
working tree. That is the whole reason it is written this way: a hygiene test
that mutates the repository it is policing is a test that eventually commits
something.
"""

from __future__ import annotations

import json
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


# ===========================================================================
# Reading a Dockerfile the way the builder reads it
# ===========================================================================
#
# A static reading of the Dockerfile asked "is the pin written down". The
# builder asked "does the pin have a value", and the distance between those two
# questions is where a container that had never been built was living. So these
# helpers do the builder's two jobs: apply the stage's variable scope, and apply
# the context's exclusion rules. Both are deliberately small and both are
# guarded -- a gate that guesses at syntax it does not implement is worse than
# no gate, because it answers confidently.


#: The .dockerignore syntax these helpers implement: an optional leading ``!``,
#: ``*`` that does not cross a slash, ``**`` that does, ``[abc]`` classes, and a
#: trailing ``/`` meaning a directory and its contents. A rule outside this set
#: fails a test rather than being quietly mis-read.
_SUPPORTED_RULE = re.compile(r"^!?[A-Za-z0-9_.*?\-/\[\]]+$")

_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def logical_instructions(text: str) -> list[str]:
    """Dockerfile lines rejoined where they continue, comments and blanks gone."""
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        line = raw.rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        pending += line
        if pending.strip():
            logical.append(pending.strip())
        pending = ""
    if pending.strip():
        logical.append(pending.strip())
    return logical


def declared_names(instruction: str) -> set[str]:
    """The names an ``ARG`` or ``ENV`` instruction introduces.

    An ``ENV A=1 B=2`` declares both; so does a continued one, which is why the
    continuations are joined before this sees it.
    """
    head, _, body = instruction.partition(" ")
    if head.upper() not in ("ARG", "ENV"):
        return set()
    return {
        name
        for name in re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)(?==|\s|$)", body)
    }


def referenced_names(instruction: str) -> set[str]:
    """Every ``$NAME`` / ``${NAME}`` the instruction expands."""
    return {braced or bare for braced, bare in _VAR_REF.findall(instruction)}


def active_ignore_rules() -> list[str]:
    """``.dockerignore`` as rules rather than as text: a comment is not a rule."""
    return [
        line.strip()
        for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def rule_matches(rule: str, path: str) -> bool:
    """Does one active ignore rule match this context-relative path?"""
    body = rule[1:] if rule.startswith("!") else rule
    as_directory = body.endswith("/")
    body = body.strip("/")
    if as_directory and (path == body or path.startswith(body + "/")):
        return True

    pattern = ""
    index = 0
    while index < len(body):
        if body[index:index + 3] == "**/":
            pattern += "(?:.*/)?"
            index += 3
            continue
        if body[index:index + 2] == "**":
            pattern += ".*"
            index += 2
            continue
        char = body[index]
        if char == "*":
            pattern += "[^/]*"
        elif char == "?":
            pattern += "[^/]"
        elif char == "[":
            close = body.find("]", index)
            pattern += body[index:close + 1]
            index = close + 1
            continue
        else:
            pattern += re.escape(char)
        index += 1
    return re.fullmatch(pattern, path) is not None


def ignored_by_rules(path: str, rules: list[str]) -> bool:
    """Last matching rule wins, and a parent's verdict reaches its children --
    which is the mechanism behind a bare ``secrets/`` excluding what is inside
    it."""
    path = path.replace("\\", "/").strip("/")
    parts = path.split("/")
    ignored = False
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        for rule in rules:
            if rule_matches(rule, prefix):
                ignored = not rule.startswith("!")
    return ignored


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


def would_git_ignore(path: str) -> bool:
    """Would ``git add`` refuse to stage this path -- or anything inside it?

    Asked of git rather than re-derived from the ignore file, because
    ``check-ignore`` is the same code path that decides what ``git add`` stages.

    Two queries, because the operator has not created these paths yet and git
    answers a directory rule (``targets/``) only for a path it can see *is* a
    directory. The bare name ``targets`` therefore reports "not ignored" for a
    rule that does ignore everything the operator would put there -- which is
    the shape of a gate that fails for a reason that is not a defect.
    """
    for probe in (f"{path}/", path):
        done = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", probe],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        if done.returncode == 0:
            return True
    return False


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

    def test_every_build_variable_is_in_scope_in_the_stage_that_reads_it(self):
        """A pin with an empty value passes a text search and fails a build.

        ``ARG`` declared before ``FROM`` is a *global* argument: it is
        substituted into the FROM line and then goes out of scope for the stage.
        An earlier revision relied on one inside a ``RUN``, so
        ``"playwright==${PLAYWRIGHT_VERSION}"`` expanded to ``playwright==`` and
        pip rejected it as an invalid requirement -- naming neither the argument
        nor the cause. The test above passed throughout, because it matches the
        pin's *name* in the text and a name is not a version.

        So this checks scope rather than text, using the builder's rule: a
        variable is readable only if the stage declares it before the line that
        reads it.
        """
        instructions = logical_instructions(self.DOCKERFILE.read_text(encoding="utf-8"))
        first_from = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.upper().startswith("FROM ")
        )

        declared: set[str] = set()
        seen: set[str] = set()
        problems: list[str] = []
        for instruction in instructions[first_from + 1:]:
            reads = referenced_names(instruction)
            for name in sorted(reads - declared):
                problems.append(f"  {instruction[:78]}\n    reads ${name}")
            seen |= reads
            declared |= declared_names(instruction)

        assert not problems, (
            "the Dockerfile reads build variables that are not in scope:\n"
            + "\n".join(problems)
            + "\nAn ARG declared before FROM is visible to the FROM line only."
            " Re-declare it inside the stage -- a bare `ARG NAME` with no value"
            " -- or the expansion is the empty string and the failure surfaces"
            " in pip, several layers from the cause."
        )
        assert "PLAYWRIGHT_VERSION" in seen, (
            "the scope check never saw the version argument read inside the"
            " stage, so it is not checking the thing it exists to check"
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

    def test_every_tracked_file_the_dockerfile_copies_survives_the_ignore_rules(self):
        """A COPY source has to be in the build *context*, not merely on disk.

        ``.dockerignore`` filters the context before any instruction runs, so a
        COPY naming an excluded path fails the build -- which is how this was
        found. The broad ``config.*.toml`` rule excluded the committed
        ``config.example.toml`` the Dockerfile COPYs, and the build stopped
        there with a checksum error naming neither the file nor the rule.

        Checked over *tracked* files rather than over the working tree, because
        excluding build junk is the ignore file doing its job and not a defect.
        The direction that must hold is the other one: every file the repository
        says is part of the program is in the context.
        """
        rules = active_ignore_rules()

        # The matcher below implements a subset of .dockerignore syntax. A rule
        # outside that subset would be mis-read silently, and a gate that answers
        # confidently about syntax it does not implement is worse than no gate.
        for rule in rules:
            assert _SUPPORTED_RULE.match(rule), (
                f"the ignore-rule matcher in this file does not implement {rule!r}."
                " Extend it, or restrict the rule -- do not leave it guessing."
            )
        assert len(rules) > 20, (
            "the ignore rules were not read; this gate is inspecting nothing"
        )

        sources: list[str] = []
        for instruction in logical_instructions(self.DOCKERFILE.read_text(encoding="utf-8")):
            if not instruction.upper().startswith("COPY "):
                continue
            operands = [
                token for token in instruction.split()[1:] if not token.startswith("--")
            ]
            sources.extend(operands[:-1])  # the final operand is the destination

        assert sources, "no COPY source was found; this gate is inspecting nothing"

        for source in sources:
            tracked = git("ls-files", "--", source, cwd=REPO).splitlines()
            assert tracked, (
                f"COPY {source} names a path git does not track, so nothing about"
                " it was verified"
            )
            for path in tracked:
                assert not ignored_by_rules(path, rules), (
                    f"COPY {source} names a path .dockerignore excludes:\n"
                    f"  {path}\n"
                    "The context is filtered before the Dockerfile runs, so the"
                    " build stops at that COPY -- or, for a file inside a copied"
                    " directory, ships an image quietly missing it."
                )

    @pytest.mark.docker
    def test_the_image_builds_and_is_what_the_dockerfile_says_it_is(self):
        """Every other check here is reading. This one is evidence.

        A Dockerfile is prose until something runs it, and this one was wrong in
        four places that a green suite and a careful read both missed. So while a
        daemon is listening, build it and inspect the result: it builds at all,
        it carries the file it COPYs, it does not run as root, and no layer
        carries a credential.

        Skipped without a daemon -- a machine with no Docker should still be able
        to run the suite, and the reading gates above are the fast approximation
        of this one.
        """
        docker = shutil.which("docker")
        if docker is None:
            pytest.skip("docker is not on PATH")

        def docker_run(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [docker, *args],
                cwd=REPO,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )

        if docker_run("info", timeout=45).returncode != 0:
            pytest.skip("no docker daemon is listening")

        tag = "insta-report:hygiene-gate"
        build = docker_run("build", "-t", tag, ".", timeout=1800)
        assert build.returncode == 0, (
            "the image does not build:\n"
            + build.stdout[-3000:]
            + build.stderr[-2000:]
        )

        # Not root, and specifically the base image's own unprivileged user. The
        # uid is asserted rather than merely "not 0" because the compose file
        # tells an operator to `chown 1001` the artifacts directory by hand, so
        # a silent renumbering in the base image would make that advice wrong.
        who = docker_run("run", "--rm", "--entrypoint", "id", tag, "-u")
        assert who.stdout.strip() == "1001", (
            f"`docker run` reported uid {who.stdout.strip()!r}. This image must"
            " not run as root, and the uid is the base image's `pwuser` -- if the"
            " base image renumbered it, the artifacts-ownership note in"
            " docker-compose.yml is now wrong as well."
        )

        # The one file the Dockerfile COPYs for an operator to read. Reaching
        # this line already means the ignore rules did not swallow it, because
        # the build would have stopped; this makes it a fact rather than an
        # inference.
        inside = docker_run(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            tag,
            "-c",
            "test -f /app/config.example.toml && echo present",
        )
        assert "present" in inside.stdout, (
            "the image built but /app/config.example.toml is not inside it:\n"
            + inside.stdout
            + inside.stderr
        )

        env = json.loads(
            docker_run("inspect", "--format", "{{json .Config.Env}}", tag).stdout or "[]"
        )
        declared = 0
        for entry in env:
            name, _, value = entry.partition("=")
            if not any(
                word in name.upper()
                for word in ("SESSIONID", "SECRET", "TOKEN", "PASSWORD", "API_KEY")
            ):
                continue
            declared += 1
            assert value == "", (
                f"{name} carries a value in the image configuration. A secret"
                " baked into a layer is in the image, in the build cache, in"
                " `docker history`, and in every pushed copy of it."
            )
        assert declared, (
            "the image declares no credential-shaped variable at all, so the"
            " check above verified nothing"
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

    def test_the_proxy_file_the_config_names_is_mounted_where_the_container_looks(self):
        """Two files agree on a path, or the operator is told a file is missing.

        ``config.example.toml`` sets ``file_path = "~/proxies.txt"``. Inside the
        container ``~`` is the home of the user the image runs as, so the config
        looks in ``/home/pwuser``; compose mounted the file at ``/app``. An
        operator who copied the example and used the compose file verbatim got
        ``file_path does not exist: /home/pwuser/proxies.txt`` -- a paid proxy
        list correctly mounted and correctly invisible, which reads as a bad
        file rather than as two documents disagreeing about a path.

        The home directory is derived from the image's ``USER`` rather than
        written down here, so changing the user moves this check with it instead
        of leaving it asserting yesterday's answer.
        """
        dockerfile = self.DOCKERFILE.read_text(encoding="utf-8")
        users = re.findall(r"(?m)^USER\s+(\S+)\s*$", dockerfile)
        assert len(users) == 1, (
            f"expected exactly one USER instruction in the Dockerfile, found"
            f" {users}. The mount target this gate checks depends on which user"
            " the container runs as, so it has to be unambiguous."
        )
        user = users[0]

        example = (REPO / "config.example.toml").read_text(encoding="utf-8")
        named = re.findall(r'(?m)^\s*file_path\s*=\s*"(~/[^"]+)"', example)
        assert len(named) == 1, (
            f"expected one `file_path = \"~/...\"` in config.example.toml, found"
            f" {named}. This gate ties that setting to the compose mount, so it"
            " must be able to read the setting an operator would actually copy."
        )
        proxy_file = named[0][2:]  # drop the leading "~/"

        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        mounts = re.findall(r"(?m)^\s*-\s*\./(\S+?):(\S+?):ro\s*$", compose)
        assert len(mounts) >= 3, (
            "the read-only bind mounts were not read, so this gate is inspecting"
            f" nothing. Mounts found: {mounts}"
        )
        by_source = dict(mounts)
        assert proxy_file in by_source, (
            f"the example config's proxy file is `~/{proxy_file}`, so compose"
            f" must bind-mount `./{proxy_file}` read-only. Read-only mounts"
            f" found: {sorted(by_source)}"
        )

        expected = f"/home/{user}/{proxy_file}"
        assert by_source[proxy_file] == expected, (
            f"compose mounts ./{proxy_file} at {by_source[proxy_file]}, but the"
            f" example config's `~/{proxy_file}` resolves to {expected} inside"
            f" the container, because `~` is the home of `USER {user}`. An"
            " operator following both files exactly would be told the file does"
            " not exist -- which reads as a bad proxy file, not as a mismatch."
        )

    def test_every_in_repo_file_compose_mounts_is_one_git_refuses_to_commit(self):
        """compose mounts these *from the checkout*, so git has to refuse them.

        Two operator files are bind-mounted out of the repository root: the
        config and the proxy list. Both are named in ``.dockerignore`` -- the
        image was thought about -- and the proxy list was never named in
        ``.gitignore``. So the walkthrough invited an operator to drop a paid
        proxy list in the working tree, where the next ``git add -A`` stages it.
        One of the two had a rule and the other did not, which is how an omission
        survives a review: the shape looks handled.

        Asked of git rather than re-derived from the ignore file, because
        ``git check-ignore`` is the same code path that decides what ``git add``
        stages. A source already tracked is skipped -- that is the committed
        example, and committing it was a decision rather than an accident.
        """
        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        mounts = re.findall(r"(?m)^\s*-\s*\./(\S+?):(\S+?):ro\s*$", compose)
        assert len(mounts) >= 3, f"the read-only bind mounts were not read: {mounts}"

        checked = 0
        for source, _target in mounts:
            if (REPO / source).resolve().parent != REPO:
                continue  # a source below the root is outside this gate's claim
            checked += 1
            if tracked(source):
                continue  # committed on purpose, so not an accident waiting
            assert would_git_ignore(source), (
                f"docker-compose.yml bind-mounts ./{source} from inside the"
                " checkout, but git does not ignore it, so the next `git add -A`"
                " stages it -- and the walkthrough tells an operator to create it"
                " there. Add it to .gitignore, or mount it from outside the"
                " repository."
            )
        assert checked >= 3, (
            f"only {checked} in-repo mount source(s) were examined, but compose"
            " mounts the config, the proxy list and the target directory from the"
            " root -- finding fewer means this gate is looking at the wrong thing"
        )
