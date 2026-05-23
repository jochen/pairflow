"""Tests for the read-only flows helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pairflow import flows


@pytest.fixture
def tiny_flows(tmp_path: Path) -> Path:
    data = [
        {"id": "tab1", "type": "tab", "label": "Main",   "info": "primary"},
        {"id": "tab2", "type": "tab", "label": "Helpers", "disabled": True},
        {"id": "inj1", "type": "inject",   "name": "Boot",    "z": "tab1", "x": 100, "y": 80},
        {"id": "fn1",  "type": "function", "name": "Format",  "z": "tab1", "x": 250, "y": 80,
         "func": "return msg;", "wires": [["dbg1"]]},
        {"id": "dbg1", "type": "debug",    "name": "out",     "z": "tab1", "x": 400, "y": 80},
        {"id": "fn2",  "type": "function", "name": "Other",   "z": "tab2", "x": 100, "y": 80,
         "func": "return msg;"},
        {"id": "brk1", "type": "mqtt-broker", "name": "local", "broker": "localhost"},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data))
    return p


def test_list_tabs(tiny_flows: Path):
    tabs = flows.list_tabs(tiny_flows)
    assert [t["id"] for t in tabs] == ["tab1", "tab2"]
    assert tabs[0]["label"] == "Main"
    assert tabs[0]["info"] == "primary"
    assert tabs[1]["disabled"] is True


def test_list_nodes_no_filter(tiny_flows: Path):
    nodes = flows.list_nodes(tiny_flows)
    ids = {n["id"] for n in nodes}
    assert ids == {"inj1", "fn1", "dbg1", "fn2", "brk1"}
    # Tabs themselves are not in the node list.
    assert "tab1" not in ids


def test_list_nodes_compact_view_excludes_wires(tiny_flows: Path):
    nodes = flows.list_nodes(tiny_flows, node_type="function")
    fn1 = next(n for n in nodes if n["id"] == "fn1")
    assert "wires" not in fn1
    assert "func" not in fn1
    assert fn1["name"] == "Format"


def test_list_nodes_filter_by_tab(tiny_flows: Path):
    nodes = flows.list_nodes(tiny_flows, tab_id="tab1")
    assert {n["id"] for n in nodes} == {"inj1", "fn1", "dbg1"}


def test_list_nodes_filter_by_type(tiny_flows: Path):
    nodes = flows.list_nodes(tiny_flows, node_type="function")
    assert {n["id"] for n in nodes} == {"fn1", "fn2"}


def test_list_nodes_filter_combined(tiny_flows: Path):
    nodes = flows.list_nodes(tiny_flows, tab_id="tab2", node_type="function")
    assert [n["id"] for n in nodes] == ["fn2"]


def test_list_nodes_name_contains_case_insensitive(tiny_flows: Path):
    nodes = flows.list_nodes(tiny_flows, name_contains="format")
    assert [n["id"] for n in nodes] == ["fn1"]


def test_get_node_full_payload(tiny_flows: Path):
    n = flows.get_node(tiny_flows, "fn1")
    assert n is not None
    assert n["func"] == "return msg;"
    assert n["wires"] == [["dbg1"]]


def test_get_node_missing(tiny_flows: Path):
    assert flows.get_node(tiny_flows, "does-not-exist") is None
