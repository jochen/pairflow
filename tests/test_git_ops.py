"""Tests for the git workflow wrappers.

Uses real git repos in tmp_path rather than subprocess mocks — git's output
is fiddly to mock perfectly, and a real ~10ms `git init` per test is fast
enough not to matter.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pairflow import git_ops

git_required = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not on PATH",
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    # Pin identity so commit works in CI without a global git config.
    _git(r, "config", "user.email", "pairflow-tests@example.com")
    _git(r, "config", "user.name", "Pairflow Tests")
    (r / "README.md").write_text("hello\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-q", "-m", "Initial commit")
    return r


# --- guards ---------------------------------------------------------------- #


def test_not_a_repo_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="Not a git repository"):
        git_ops.status(tmp_path)


def test_missing_dir_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        git_ops.status(tmp_path / "nope")


# --- status ---------------------------------------------------------------- #


@git_required
def test_status_clean_repo(repo: Path):
    s = git_ops.status(repo)
    assert s["branch"] == "main"
    assert s["clean"] is True
    assert s["files"] == []
    assert s["ahead"] == 0 and s["behind"] == 0


@git_required
def test_status_unstaged_change(repo: Path):
    (repo / "README.md").write_text("hello world\n")
    s = git_ops.status(repo)
    assert s["clean"] is False
    assert any(f["path"] == "README.md" and "M" in f["status"] for f in s["files"])


@git_required
def test_status_untracked(repo: Path):
    (repo / "new.txt").write_text("x\n")
    s = git_ops.status(repo)
    paths = {(f["status"], f["path"]) for f in s["files"]}
    assert ("??", "new.txt") in paths


@git_required
def test_status_staged_and_unstaged(repo: Path):
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    (repo / "README.md").write_text("changed\n")
    s = git_ops.status(repo)
    statuses = {f["path"]: f["status"] for f in s["files"]}
    assert statuses["a.txt"] == "A "
    assert "M" in statuses["README.md"]


# --- diff ------------------------------------------------------------------ #


@git_required
def test_diff_working_tree(repo: Path):
    (repo / "README.md").write_text("hello world\n")
    d = git_ops.diff(repo)
    assert "diff --git" in d["diff"]
    assert "README.md" in d["diff"]
    assert d["staged"] is False


@git_required
def test_diff_staged_only(repo: Path):
    (repo / "README.md").write_text("hello world\n")
    _git(repo, "add", "README.md")
    # Now make a further unstaged change
    (repo / "README.md").write_text("hello world!\n")

    staged = git_ops.diff(repo, staged=True)
    working = git_ops.diff(repo, staged=False)
    assert "hello world" in staged["diff"]
    assert staged["diff"] != working["diff"]


@git_required
def test_diff_path_filter(repo: Path):
    (repo / "a.txt").write_text("alpha\n")
    (repo / "b.txt").write_text("beta\n")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-q", "-m", "Add files")
    (repo / "a.txt").write_text("ALPHA\n")
    (repo / "b.txt").write_text("BETA\n")

    d = git_ops.diff(repo, path="a.txt")
    assert "a.txt" in d["diff"]
    assert "b.txt" not in d["diff"]


# --- commit ---------------------------------------------------------------- #


@git_required
def test_commit_existing_staged(repo: Path):
    (repo / "x.txt").write_text("x\n")
    _git(repo, "add", "x.txt")
    r = git_ops.commit(repo, message="Add x")
    assert len(r["sha"]) == 40
    assert len(r["short_sha"]) == 7
    # Now repo is clean again
    assert git_ops.status(repo)["clean"] is True


@git_required
def test_commit_with_paths_arg_stages_them(repo: Path):
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    r = git_ops.commit(repo, message="Add a only", paths=["a.txt"])
    assert r["sha"]
    # b.txt should still be untracked
    s = git_ops.status(repo)
    paths = {f["path"]: f["status"] for f in s["files"]}
    assert paths.get("b.txt") == "??"


@git_required
def test_commit_empty_message_rejected(repo: Path):
    with pytest.raises(ValueError, match="message"):
        git_ops.commit(repo, message="")


@git_required
def test_commit_nothing_to_commit_raises(repo: Path):
    """No staged changes + allow_empty=False (default) → git returns non-zero."""
    with pytest.raises(RuntimeError):
        git_ops.commit(repo, message="empty")


@git_required
def test_commit_allow_empty(repo: Path):
    r = git_ops.commit(repo, message="empty marker", allow_empty=True)
    assert r["sha"]


# --- log ------------------------------------------------------------------- #


@git_required
def test_log_returns_recent_commits(repo: Path):
    for i in range(3):
        (repo / f"f{i}.txt").write_text(f"{i}\n")
        _git(repo, "add", f"f{i}.txt")
        _git(repo, "commit", "-q", "-m", f"Add f{i}")

    log = git_ops.log(repo, count=2)
    assert len(log) == 2
    assert log[0]["subject"] == "Add f2"  # most recent first
    assert log[1]["subject"] == "Add f1"
    for c in log:
        assert len(c["sha"]) == 40
        assert len(c["short_sha"]) == 7
        assert c["author"] == "Pairflow Tests"
        assert "T" in c["date"]  # ISO 8601


@git_required
def test_log_rejects_zero_count(repo: Path):
    with pytest.raises(ValueError, match="count"):
        git_ops.log(repo, count=0)
