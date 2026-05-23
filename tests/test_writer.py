"""Tests for the atomic-write + backup + mtime-lock infrastructure."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pairflow import writer


@pytest.fixture
def flows_file(tmp_path: Path) -> Path:
    p = tmp_path / "flows.json"
    p.write_text(json.dumps([{"id": "a", "type": "tab", "label": "A"}], indent=4))
    return p


def test_atomic_write_replaces_content(flows_file: Path):
    new_data = [{"id": "b", "type": "tab", "label": "B"}]
    writer.atomic_write(flows_file, new_data)
    assert json.loads(flows_file.read_text()) == new_data


def test_atomic_write_creates_timestamped_backup(flows_file: Path):
    writer.atomic_write(flows_file, [{"id": "x", "type": "tab"}])
    backups = list(flows_file.parent.glob(f"{flows_file.name}.*.bak"))
    assert len(backups) == 1
    # Filename matches <name>.YYYYMMDD-HHMMSS.bak
    stem = backups[0].name.removeprefix(flows_file.name + ".").removesuffix(".bak")
    assert len(stem) == 15 and stem[8] == "-"


def test_atomic_write_backup_preserves_old_content(flows_file: Path):
    old = json.loads(flows_file.read_text())
    writer.atomic_write(flows_file, [{"id": "new", "type": "tab"}])
    backup = next(flows_file.parent.glob(f"{flows_file.name}.*.bak"))
    assert json.loads(backup.read_text()) == old


def test_mtime_lock_refuses_concurrent_change(flows_file: Path):
    mtime = writer.current_mtime(flows_file)
    # Simulate a concurrent edit by another process.
    time.sleep(0.05)
    flows_file.write_text(json.dumps([{"id": "other", "type": "tab"}], indent=4))
    with pytest.raises(writer.ConcurrentModificationError):
        writer.atomic_write(flows_file, [{"id": "ours", "type": "tab"}], expected_mtime=mtime)


def test_mtime_lock_passes_when_unchanged(flows_file: Path):
    mtime = writer.current_mtime(flows_file)
    writer.atomic_write(flows_file, [{"id": "new", "type": "tab"}], expected_mtime=mtime)
    assert json.loads(flows_file.read_text()) == [{"id": "new", "type": "tab"}]


def test_atomic_write_prunes_old_backups(flows_file: Path):
    # Write 5 times with backup_keep=3 — expect at most 3 backups left.
    for i in range(5):
        writer.atomic_write(
            flows_file,
            [{"id": f"v{i}", "type": "tab"}],
            backup_keep=3,
        )
        time.sleep(1.05)  # ensure distinct timestamp seconds
    backups = list(flows_file.parent.glob(f"{flows_file.name}.*.bak"))
    assert len(backups) == 3


def test_atomic_write_preserves_4_space_indent(flows_file: Path):
    writer.atomic_write(flows_file, [{"id": "x", "type": "tab"}])
    text = flows_file.read_text()
    # Second line should start with 4 spaces (start of indented object).
    assert text.splitlines()[1].startswith("    ")
