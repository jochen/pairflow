"""Tests for the doctor read-only check + report rendering."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pairflow import doctor


@pytest.fixture
def good_flows(tmp_path: Path) -> Path:
    data = [
        {"id": "tab1", "type": "tab", "label": "A"},
        {"id": "fn1",  "type": "function", "z": "tab1", "name": "fn",
         "func": "return msg;", "wires": [["dbg1"]]},
        {"id": "dbg1", "type": "debug",    "z": "tab1", "name": "dbg", "wires": []},
        {"id": "lo1",  "type": "link out", "z": "tab1", "links": ["li1"], "wires": []},
        {"id": "li1",  "type": "link in",  "z": "tab1", "links": ["lo1"], "wires": []},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data, indent=4))
    return p


@pytest.fixture
def broken_flows(tmp_path: Path) -> Path:
    data = [
        {"id": "tab1", "type": "tab", "label": "A"},
        # function with invalid body
        {"id": "fn1",  "type": "function", "z": "tab1", "name": "broken",
         "func": "if (true) {\nreturn", "wires": []},
        # wire to a missing node
        {"id": "fn2",  "type": "function", "z": "tab1", "name": "wired-to-missing",
         "func": "return msg;", "wires": [["does-not-exist"]]},
        # link-in with a dangling link
        {"id": "li1",  "type": "link in",  "z": "tab1", "links": ["does-not-exist"], "wires": []},
        # duplicate id
        {"id": "tab1", "type": "debug",    "z": "tab1", "name": "duplicate-id", "wires": []},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data, indent=4))
    return p


def test_check_good_flow(good_flows: Path):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    r = doctor.check(good_flows)
    assert r.total_nodes == 5
    assert r.tab_count == 1
    assert r.function_total == 1
    assert r.function_valid == 1
    assert r.function_errors == []
    assert r.duplicate_ids == []
    assert r.dangling_wire_refs == []
    assert r.dangling_link_refs == []


def test_check_flags_all_anomalies(broken_flows: Path):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    r = doctor.check(broken_flows)
    assert r.function_errors  # at least the broken fn1
    assert r.duplicate_ids == ["tab1"]
    assert ("fn2", "does-not-exist") in r.dangling_wire_refs
    assert any("does-not-exist" == tgt for _, _, tgt in r.dangling_link_refs)


def test_render_includes_key_lines(good_flows: Path):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    out = doctor.render(doctor.check(good_flows))
    assert "Pairflow Doctor" in out
    assert "Tabs:" in out
    assert "Function-node bodies" in out
    assert "Duplicate ids" in out


def test_main_exit_code_clean(good_flows: Path, capsys):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    rc = doctor.main(["--flows", str(good_flows)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Pairflow Doctor" in out


def test_main_exit_code_problems(broken_flows: Path, capsys):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    rc = doctor.main(["--flows", str(broken_flows)])
    assert rc == 1


def test_main_missing_file(tmp_path: Path, capsys):
    rc = doctor.main(["--flows", str(tmp_path / "nope.json")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_smoke_exercise_does_not_modify_original(good_flows: Path):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    before = good_flows.read_bytes()
    out = doctor.smoke_exercise(good_flows)
    assert "nr_add_node" in out
    assert "nr_delete_node" in out
    assert good_flows.read_bytes() == before
