"""Tests for the mutating flow helpers (add/update/delete/wire/unwire)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pairflow import flows


@pytest.fixture
def fw(tmp_path: Path) -> Path:
    data = [
        {"id": "tab1", "type": "tab", "label": "Main"},
        {"id": "inj",  "type": "inject",   "name": "Boot",    "z": "tab1",
         "x": 100, "y": 80, "wires": [["fn"]]},
        {"id": "fn",   "type": "function", "name": "Format",  "z": "tab1",
         "x": 250, "y": 80, "func": "return msg;", "wires": [["dbg"]]},
        {"id": "dbg",  "type": "debug",    "name": "out",     "z": "tab1",
         "x": 400, "y": 80, "wires": []},
        {"id": "lo",   "type": "link out", "z": "tab1", "links": ["li"], "wires": []},
        {"id": "li",   "type": "link in",  "z": "tab1", "links": ["lo"], "wires": []},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data, indent=4))
    return p


# ---- add_node -------------------------------------------------------------- #


def test_add_node_basic(fw: Path):
    node = flows.add_node(fw, tab_id="tab1", node_type="debug", x=500, y=80)
    assert node["type"] == "debug"
    assert node["z"] == "tab1"
    assert node["x"] == 500 and node["y"] == 80
    assert len(node["id"]) == 16

    # Persisted to disk:
    assert flows.get_node(fw, node["id"]) is not None


def test_add_node_unknown_tab_rejected(fw: Path):
    with pytest.raises(ValueError, match="No tab with id"):
        flows.add_node(fw, tab_id="nope", node_type="debug")


def test_add_node_with_explicit_id_and_props(fw: Path):
    node = flows.add_node(
        fw,
        tab_id="tab1",
        node_type="mqtt out",
        node_id="my_mqtt_out",
        props={"topic": "test/topic", "qos": "1", "retain": False},
    )
    assert node["id"] == "my_mqtt_out"
    assert node["topic"] == "test/topic"


def test_add_node_rejects_duplicate_id(fw: Path):
    with pytest.raises(ValueError, match="already exists"):
        flows.add_node(fw, tab_id="tab1", node_type="debug", node_id="dbg")


def test_add_node_function_with_valid_code(fw: Path):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    node = flows.add_node(
        fw,
        tab_id="tab1",
        node_type="function",
        props={"name": "x", "func": "return msg;"},
    )
    assert node["func"] == "return msg;"


def test_add_node_function_with_invalid_code_rejected(fw: Path):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    with pytest.raises(ValueError, match="Invalid function body"):
        flows.add_node(
            fw,
            tab_id="tab1",
            node_type="function",
            props={"name": "x", "func": "if (true) {\nreturn"},
        )


# ---- update_node ----------------------------------------------------------- #


def test_update_node_basic(fw: Path):
    node = flows.update_node(fw, "dbg", {"name": "renamed", "active": False})
    assert node["name"] == "renamed"
    assert node["active"] is False
    assert flows.get_node(fw, "dbg")["name"] == "renamed"


def test_update_node_cannot_change_id_type_z(fw: Path):
    node = flows.update_node(
        fw, "dbg",
        {"id": "evil", "type": "evil", "z": "evil", "name": "renamed"},
    )
    assert node["id"] == "dbg"
    assert node["type"] == "debug"
    assert node["z"] == "tab1"
    assert node["name"] == "renamed"


def test_update_node_function_with_invalid_code(fw: Path):
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    with pytest.raises(ValueError, match="Invalid function body"):
        flows.update_node(fw, "fn", {"func": "if (true) {\nbreak"})


def test_update_node_missing(fw: Path):
    with pytest.raises(KeyError):
        flows.update_node(fw, "nope", {"name": "x"})


# ---- delete_node ----------------------------------------------------------- #


def test_delete_node_removes_and_cleans_wires(fw: Path):
    result = flows.delete_node(fw, "fn")
    assert result["deleted"]["id"] == "fn"
    # The "inj" node's wires pointed to "fn"; that reference should be gone.
    inj = flows.get_node(fw, "inj")
    assert "fn" not in inj["wires"][0]
    # references_removed should count: inj -> fn (1)
    assert result["references_removed"] == 1


def test_delete_node_cleans_link_references(fw: Path):
    flows.delete_node(fw, "li")
    lo = flows.get_node(fw, "lo")
    assert "li" not in lo["links"]


def test_delete_node_missing(fw: Path):
    with pytest.raises(KeyError):
        flows.delete_node(fw, "nope")


# ---- wire / unwire --------------------------------------------------------- #


def test_wire_adds_to_existing_port(fw: Path):
    # inj already wires to fn; add a wire to dbg too.
    result = flows.wire(fw, "inj", 0, "dbg")
    assert result["added"] is True
    inj = flows.get_node(fw, "inj")
    assert inj["wires"][0] == ["fn", "dbg"]


def test_wire_idempotent(fw: Path):
    flows.wire(fw, "inj", 0, "dbg")
    result = flows.wire(fw, "inj", 0, "dbg")
    assert result["added"] is False


def test_wire_creates_new_output_port(fw: Path):
    result = flows.wire(fw, "dbg", 2, "fn")
    assert result["added"] is True
    dbg = flows.get_node(fw, "dbg")
    # Output port 2 created, intermediate ports filled with empty lists.
    assert dbg["wires"] == [[], [], ["fn"]]


def test_wire_rejects_unknown_src(fw: Path):
    with pytest.raises(KeyError):
        flows.wire(fw, "nope", 0, "dbg")


def test_wire_rejects_unknown_dst(fw: Path):
    with pytest.raises(KeyError):
        flows.wire(fw, "inj", 0, "nope")


def test_unwire_removes(fw: Path):
    result = flows.unwire(fw, "inj", 0, "fn")
    assert result["removed"] is True
    inj = flows.get_node(fw, "inj")
    assert inj["wires"][0] == []


def test_unwire_noop_if_not_present(fw: Path):
    result = flows.unwire(fw, "inj", 0, "dbg")
    assert result["removed"] is False
