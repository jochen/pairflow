"""Git workflow operations inside the Node-RED project directory.

Pairflow patches the flows file directly on disk. To stay coherent with the
host's git-based review and rollback workflow, these helpers expose the
common git operations the AI needs as structured calls: `status` to see
what's pending, `diff` to show the change, `log` to recall recent history,
and `commit` to land changes when the human OKs it.

All operations run as ``git -C <project_dir>``; no chdir, no environment
manipulation. The configured ``project_dir`` is validated to be an actual
git repo before any call goes out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _require_repo(project_dir: Path) -> None:
    if not project_dir or not project_dir.exists():
        raise ValueError(f"project_dir does not exist: {project_dir}")
    if not (project_dir / ".git").exists():
        raise ValueError(f"Not a git repository: {project_dir}")


def _git(project_dir: Path, *args: str, timeout: float = 30.0) -> str:
    _require_repo(project_dir)
    cmd = ["git", "-C", str(project_dir), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}): {err}")
    return proc.stdout


# --- status --------------------------------------------------------------- #


def status(project_dir: Path) -> dict[str, Any]:
    """Return parsed status for the working tree.

    Result shape::

        {
            "branch": "main",
            "ahead": 0,
            "behind": 0,
            "clean": True,
            "files": [{"status": "M ", "path": "foo.txt"}, ...]
        }
    """
    raw = _git(project_dir, "status", "--porcelain=v1", "-b")
    lines = raw.splitlines()
    branch = ""
    ahead = 0
    behind = 0
    files: list[dict[str, Any]] = []

    for ln in lines:
        if ln.startswith("##"):
            # e.g. '## main...origin/main [ahead 1, behind 2]'
            body = ln[3:]
            head = body.split(" ", 1)[0]
            branch = head.split("...", 1)[0]
            if "[" in body:
                bracket = body[body.index("[") + 1 : body.rindex("]")]
                for part in bracket.split(","):
                    part = part.strip()
                    if part.startswith("ahead "):
                        ahead = int(part.removeprefix("ahead "))
                    elif part.startswith("behind "):
                        behind = int(part.removeprefix("behind "))
        elif len(ln) >= 3:
            files.append({"status": ln[:2], "path": ln[3:]})

    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "clean": not files,
        "files": files,
    }


# --- diff ----------------------------------------------------------------- #


def diff(
    project_dir: Path,
    path: str | None = None,
    staged: bool = False,
) -> dict[str, Any]:
    """Return the diff of the working tree (or the index, if ``staged``).

    ``path`` (optional) restricts the diff to a single file. With no path,
    the diff for the entire repo is returned — which can be large; callers
    that only need a summary should use ``status`` instead.
    """
    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if path:
        args.extend(["--", path])
    raw = _git(project_dir, *args)
    return {"staged": staged, "path": path, "diff": raw, "bytes": len(raw)}


# --- commit --------------------------------------------------------------- #


def commit(
    project_dir: Path,
    message: str,
    paths: list[str] | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """Stage the given ``paths`` (if any) and commit with ``message``.

    If ``paths`` is None or empty, whatever is already staged is committed —
    matching plain ``git commit -m``. ``allow_empty`` is off by default so
    a no-op commit raises rather than silently succeeding.
    """
    if not message or not message.strip():
        raise ValueError("commit message must not be empty")

    if paths:
        _git(project_dir, "add", "--", *paths)

    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    raw = _git(project_dir, *args)

    sha = _git(project_dir, "rev-parse", "HEAD").strip()
    return {"sha": sha, "short_sha": sha[:7], "message": message, "output": raw.strip()}


# --- log ------------------------------------------------------------------ #


def log(project_dir: Path, count: int = 10) -> list[dict[str, str]]:
    """Return the last ``count`` commits as structured records.

    Each record: ``{"sha", "short_sha", "date", "author", "subject"}``.
    Dates are ISO 8601 with timezone offset (``--date=iso-strict``).
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    fmt = "%H%x00%h%x00%aI%x00%an%x00%s"
    raw = _git(
        project_dir,
        "log",
        f"-n{count}",
        f"--pretty=format:{fmt}",
        "--date=iso-strict",
    )
    commits: list[dict[str, str]] = []
    for ln in raw.splitlines():
        parts = ln.split("\x00")
        if len(parts) == 5:
            commits.append({
                "sha": parts[0],
                "short_sha": parts[1],
                "date": parts[2],
                "author": parts[3],
                "subject": parts[4],
            })
    return commits
