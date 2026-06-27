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


# ---- atomic_write_json ----------------------------------------------------- #


@pytest.fixture
def json_file(tmp_path: Path) -> Path:
    p = tmp_path / "store.json"
    p.write_text(json.dumps({"a": 1}))
    return p


def test_atomic_write_json_round_trips_dict(json_file: Path):
    obj = {"key": "value", "num": 42, "nested": {"x": True}}
    writer.atomic_write_json(json_file, obj)
    assert json.loads(json_file.read_text()) == obj


def test_atomic_write_json_creates_timestamped_backup(json_file: Path):
    writer.atomic_write_json(json_file, {"key": "v2"})
    backups = list(json_file.parent.glob(f"{json_file.name}.*.bak"))
    assert len(backups) == 1
    stem = backups[0].name.removeprefix(json_file.name + ".").removesuffix(".bak")
    assert len(stem) == 15 and stem[8] == "-"


def test_atomic_write_json_new_file_no_backup(tmp_path: Path):
    """Writing a new (non-existent) file creates the file but no backup."""
    new_path = tmp_path / "new.json"
    assert not new_path.exists()
    result = writer.atomic_write_json(new_path, {"created": True})
    assert result is None  # no backup for new files
    assert json.loads(new_path.read_text()) == {"created": True}
    backups = list(tmp_path.glob(f"{new_path.name}.*.bak"))
    assert len(backups) == 0


def test_atomic_write_json_respects_expected_mtime(json_file: Path):
    mtime = writer.current_mtime(json_file)
    # Simulate concurrent modification.
    time.sleep(0.05)
    json_file.write_text(json.dumps({"concurrent": True}))
    with pytest.raises(writer.ConcurrentModificationError):
        writer.atomic_write_json(json_file, {"ours": True}, expected_mtime=mtime)


def test_atomic_write_json_passes_when_mtime_unchanged(json_file: Path):
    mtime = writer.current_mtime(json_file)
    writer.atomic_write_json(json_file, {"updated": True}, expected_mtime=mtime)
    assert json.loads(json_file.read_text()) == {"updated": True}


def test_atomic_write_json_prunes_its_own_backups(tmp_path: Path):
    """atomic_write_json prunes backups keyed to ITS path, not a sibling."""
    store_file = tmp_path / "cred.json"
    other_file = tmp_path / "flows.json"
    store_file.write_text(json.dumps({"v": 0}))
    other_file.write_text(json.dumps({"v": 0}))

    # Write store_file 4 times with backup_keep=2; other_file once.
    for i in range(4):
        writer.atomic_write_json(store_file, {"v": i + 1}, backup_keep=2)
        time.sleep(1.05)  # distinct timestamp seconds

    writer.atomic_write_json(other_file, {"v": "other"})

    store_backups = list(tmp_path.glob(f"{store_file.name}.*.bak"))
    other_backups = list(tmp_path.glob(f"{other_file.name}.*.bak"))

    assert len(store_backups) == 2   # pruned to backup_keep=2
    assert len(other_backups) == 1   # untouched


def test_atomic_write_json_uses_4_space_indent(json_file: Path):
    writer.atomic_write_json(json_file, {"key": "value"})
    text = json_file.read_text()
    # Indented key should start with 4 spaces.
    assert any(line.startswith("    ") for line in text.splitlines())
