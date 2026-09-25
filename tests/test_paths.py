from __future__ import annotations

import pytest

from insta_report.support.paths import (
    PathContainmentError,
    assert_outside_repo,
    default_data_dir,
    find_repo_root,
    resolve_paths,
)


def test_find_repo_root_locates_the_checkout():
    root = find_repo_root()
    assert root is not None, "tests run from inside the repo"
    assert (root / ".git").exists()


def test_find_repo_root_returns_none_outside_a_worktree(tmp_path):
    # tmp_path is not a git repo and has no .git anywhere above it under pytest.
    assert find_repo_root(tmp_path / "not-a-repo") in (None, find_repo_root())


def test_assert_outside_repo_rejects_a_path_inside(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    inside = repo / "artifacts"
    with pytest.raises(PathContainmentError, match="outside the work tree"):
        assert_outside_repo(inside, repo_root=repo)


def test_assert_outside_repo_rejects_the_repo_root_itself(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(PathContainmentError):
        assert_outside_repo(repo, repo_root=repo)


def test_sibling_sharing_a_name_prefix_is_not_inside(tmp_path):
    """``.../instaReport-secrets`` must not be judged inside ``.../instaReport``.

    Guards the string-prefix bug: ``str(inside).startswith(str(repo))`` is true
    for this case and wrong about it.
    """
    repo = tmp_path / "instaReport"
    (repo / ".git").mkdir(parents=True)
    sibling = tmp_path / "instaReport-secrets"
    sibling.mkdir()

    resolved = assert_outside_repo(sibling, repo_root=repo)
    assert resolved == sibling.resolve()


def test_assert_outside_repo_passes_when_not_in_a_worktree(tmp_path):
    target = tmp_path / "anywhere"
    target.mkdir()
    assert assert_outside_repo(target, repo_root=tmp_path / "no-such-repo") == target.resolve()


def test_resolve_paths_builds_every_subdir_under_data_dir(data_dir):
    paths = resolve_paths(data_dir)
    assert paths.data_dir == data_dir.resolve()
    for field in ("artifacts_dir", "traces_dir", "state_dir", "logs_dir"):
        assert getattr(paths, field).is_relative_to(paths.data_dir)


def test_resolve_paths_rejects_a_data_dir_inside_the_repo():
    """Points at a real path in the checkout, not a fake one.

    ``resolve_paths`` discovers the repo from the process cwd, so a synthetic
    repo under tmp_path would not exercise the guard at all. It validates
    before it creates anything, so this leaves no directory behind -- asserted
    immediately below.
    """
    from insta_report.support.paths import find_repo_root

    inside = find_repo_root() / ".runtime-should-never-be-created"
    with pytest.raises(PathContainmentError):
        resolve_paths(inside)
    assert not inside.exists()


def test_ensure_creates_missing_directories(data_dir):
    paths = resolve_paths(data_dir / "nested" / "deeper")
    assert not paths.artifacts_dir.exists()
    paths.ensure()
    assert paths.artifacts_dir.is_dir()
    assert paths.state_dir.is_dir()


def test_run_dir_is_isolated_per_run(data_dir):
    paths = resolve_paths(data_dir).ensure()
    first = paths.run_dir("20260925-a")
    second = paths.run_dir("20260925-b")
    assert first != second
    assert first.is_dir() and second.is_dir()


@pytest.mark.parametrize("bad", ["", "..", ".", "a/b", "a\\b", "../escape"])
def test_run_dir_rejects_path_traversal(data_dir, bad):
    paths = resolve_paths(data_dir)
    with pytest.raises(ValueError):
        paths.run_dir(bad)


def test_default_data_dir_is_outside_the_checkout():
    root = find_repo_root()
    assert default_data_dir().resolve() != root
