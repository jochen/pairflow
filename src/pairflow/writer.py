"""Safe writes for flows.json.

Every write goes through `atomic_write`, which:

  * Captures the current mtime before writing and refuses if it has changed
    since the read that produced the new content (optimistic locking — protects
    against the Node-RED editor or another tool overwriting our change).
  * Copies the existing file to a timestamped backup in the same directory.
  * Writes the new content to a temporary file in the same directory and
    renames it into place. The rename is atomic on POSIX, so readers never
    see a partial file.
  * Prunes older backups beyond a configurable retention count.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_BACKUP_KEEP = 20


class ConcurrentModificationError(RuntimeError):
    """Raised when the flows file changed between read and write."""


def current_mtime(flows_file: Path) -> float:
    return os.stat(flows_file).st_mtime


def atomic_write(
    flows_file: Path,
    data: list[dict[str, Any]],
    *,
    expected_mtime: float | None = None,
    backup_keep: int = DEFAULT_BACKUP_KEEP,
) -> Path:
    """Write `data` to `flows_file` atomically, with backup.

    Returns the path of the backup that was made.
    """
    flows_file = Path(flows_file)

    if expected_mtime is not None:
        actual = current_mtime(flows_file)
        # Compare with a small epsilon to tolerate filesystem-rounded mtimes.
        if abs(actual - expected_mtime) > 1e-6:
            raise ConcurrentModificationError(
                f"{flows_file} changed on disk since it was read "
                f"(expected mtime {expected_mtime}, got {actual}). "
                "Re-read and retry."
            )

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = flows_file.with_name(f"{flows_file.name}.{ts}.bak")
    shutil.copy2(flows_file, backup)

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=flows_file.parent,
        prefix=f".{flows_file.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.write("\n")
        # Preserve permissions of the original file on the new one
        shutil.copymode(flows_file, tmp_path)
        os.replace(tmp_path, flows_file)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    _prune_backups(flows_file, backup_keep)
    return backup


def _prune_backups(flows_file: Path, keep: int) -> None:
    """Keep the `keep` most recent timestamped backups, delete the rest."""
    if keep <= 0:
        return
    pattern = f"{flows_file.name}.*.bak"
    backups = sorted(
        (p for p in flows_file.parent.glob(pattern) if _is_timestamped_backup(p)),
    )
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def _is_timestamped_backup(path: Path) -> bool:
    # Filename: <flows_name>.YYYYMMDD-HHMMSS.bak
    parts = path.name.rsplit(".", 2)
    if len(parts) != 3 or parts[2] != "bak":
        return False
    ts = parts[1]
    if len(ts) != 15 or ts[8] != "-":
        return False
    return ts[:8].isdigit() and ts[9:].isdigit()
