"""Safe writes for flows.json and other JSON files.

Every write goes through `atomic_write` (flows lists) or `atomic_write_json`
(any JSON-serializable object), which:

  * Captures the current mtime before writing and refuses if it has changed
    since the read that produced the new content (optimistic locking — protects
    against the Node-RED editor or another tool overwriting our change).
  * Copies the existing file to a timestamped backup in the same directory.
    When writing a *new* file (no pre-existing target) the backup step is
    skipped — there is nothing to back up.
  * Writes the new content to a temporary file in the same directory and
    renames it into place. The rename is atomic on POSIX, so readers never
    see a partial file.
  * Prunes older backups beyond a configurable retention count. Each file's
    backups are pruned independently (the glob is anchored to the target
    path's name, so e.g. cred-file backups never touch flows-file backups).
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


def atomic_write_json(
    path: Path,
    obj: Any,
    *,
    expected_mtime: float | None = None,
    backup_keep: int = DEFAULT_BACKUP_KEEP,
) -> Path | None:
    """Write `obj` to `path` as JSON atomically, with optional backup.

    Any JSON-serializable object is accepted (dict, list, …).  The write is
    identical for both flows files (lists) and credential-store files (dicts).

    When the target file already exists:
      - The mtime check fires if `expected_mtime` is supplied.
      - A timestamped backup is created before the write.
      - Old backups beyond `backup_keep` are pruned.

    When the target file does *not* yet exist (first write):
      - The mtime check is skipped.
      - No backup is created (there is nothing to back up).

    Returns the backup path, or ``None`` if no backup was made.
    """
    path = Path(path)
    file_existed = path.exists()

    if file_existed and expected_mtime is not None:
        actual = current_mtime(path)
        # Compare with a small epsilon to tolerate filesystem-rounded mtimes.
        if abs(actual - expected_mtime) > 1e-6:
            raise ConcurrentModificationError(
                f"{path} changed on disk since it was read "
                f"(expected mtime {expected_mtime}, got {actual}). "
                "Re-read and retry."
            )

    backup: Path | None = None
    if file_existed:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.{ts}.bak")
        shutil.copy2(path, backup)

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=4, ensure_ascii=False)
        if file_existed:
            # Preserve permissions of the original file on the new one.
            shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    if file_existed and backup is not None:
        _prune_backups(path, backup_keep)

    return backup


def atomic_write(
    flows_file: Path,
    data: list[dict[str, Any]],
    *,
    expected_mtime: float | None = None,
    backup_keep: int = DEFAULT_BACKUP_KEEP,
) -> Path:
    """Write `data` to `flows_file` atomically, with backup.

    Delegates to `atomic_write_json`.  The flows file always pre-exists, so
    a backup is always created and the return value is always a valid Path.
    """
    backup = atomic_write_json(
        flows_file, data, expected_mtime=expected_mtime, backup_keep=backup_keep,
    )
    # flows_file always pre-exists — backup is never None here.
    assert backup is not None, "flows file must pre-exist before atomic_write is called"
    return backup


def _prune_backups(file_path: Path, keep: int) -> None:
    """Keep the `keep` most recent timestamped backups, delete the rest."""
    if keep <= 0:
        return
    pattern = f"{file_path.name}.*.bak"
    backups = sorted(
        (p for p in file_path.parent.glob(pattern) if _is_timestamped_backup(p)),
    )
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def _is_timestamped_backup(path: Path) -> bool:
    # Filename: <name>.YYYYMMDD-HHMMSS.bak
    parts = path.name.rsplit(".", 2)
    if len(parts) != 3 or parts[2] != "bak":
        return False
    ts = parts[1]
    if len(ts) != 15 or ts[8] != "-":
        return False
    return ts[:8].isdigit() and ts[9:].isdigit()
