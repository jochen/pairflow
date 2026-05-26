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


def test_list_tabs_compact_by_default(tiny_flows: Path):
    tabs = flows.list_tabs(tiny_flows)
    assert [t["id"] for t in tabs] == ["tab1", "tab2"]
    assert tabs[0]["label"] == "Main"
    assert "info" not in tabs[0]  # info omitted by default for token savings
    assert tabs[1]["disabled"] is True


def test_list_tabs_with_info_opt_in(tiny_flows: Path):
    tabs = flows.list_tabs(tiny_flows, include_info=True)
    assert tabs[0]["info"] == "primary"
    assert tabs[1]["info"] == ""


def test_list_nodes_no_filter_returns_summary(tiny_flows: Path):
    result = flows.list_nodes(tiny_flows)
    assert result["summary"] is True
    assert result["total"] == 5  # inj1, fn1, dbg1, fn2, brk1
    by_type = result["by_type"]
    assert by_type["function"] == 2
    assert by_type["debug"] == 1
    # by_tab includes the broker under "(no tab)" since it has no z.
    tabs_by_id = {t["id"]: t for t in result["by_tab"]}
    assert tabs_by_id["tab1"]["count"] == 3
    assert tabs_by_id["tab2"]["count"] == 1
    assert tabs_by_id[None]["count"] == 1


def test_list_nodes_summary_false_forces_list(tiny_flows: Path):
    result = flows.list_nodes(tiny_flows, summary=False)
    assert result["summary"] is False
    ids = {n["id"] for n in result["nodes"]}
    assert ids == {"inj1", "fn1", "dbg1", "fn2", "brk1"}


def test_list_nodes_compact_view_excludes_wires(tiny_flows: Path):
    result = flows.list_nodes(tiny_flows, node_type="function")
    assert result["summary"] is False
    fn1 = next(n for n in result["nodes"] if n["id"] == "fn1")
    assert "wires" not in fn1
    assert "func" not in fn1
    assert fn1["name"] == "Format"


def test_list_nodes_filter_by_tab(tiny_flows: Path):
    result = flows.list_nodes(tiny_flows, tab_id="tab1")
    assert {n["id"] for n in result["nodes"]} == {"inj1", "fn1", "dbg1"}


def test_list_nodes_filter_by_type(tiny_flows: Path):
    result = flows.list_nodes(tiny_flows, node_type="function")
    assert {n["id"] for n in result["nodes"]} == {"fn1", "fn2"}


def test_list_nodes_filter_combined(tiny_flows: Path):
    result = flows.list_nodes(tiny_flows, tab_id="tab2", node_type="function")
    assert [n["id"] for n in result["nodes"]] == ["fn2"]


def test_list_nodes_name_contains_case_insensitive(tiny_flows: Path):
    result = flows.list_nodes(tiny_flows, name_contains="format")
    assert [n["id"] for n in result["nodes"]] == ["fn1"]


def test_list_nodes_summary_true_with_filter(tiny_flows: Path):
    result = flows.list_nodes(tiny_flows, node_type="function", summary=True)
    assert result["summary"] is True
    assert result["total"] == 2
    assert result["by_type"] == {"function": 2}


def test_get_node_function_returns_signatures_by_default(tiny_flows: Path):
    n = flows.get_node(tiny_flows, "fn1")
    assert n is not None
    assert "func" not in n
    summary = n["func_summary"]
    assert summary["lines"] == 1
    assert summary["chars"] == len("return msg;")
    assert summary["head"] == ["return msg;"]
    assert summary["signatures"] == []
    assert summary["node_events"] == []


def test_get_node_function_full_mode(tiny_flows: Path):
    n = flows.get_node(tiny_flows, "fn1", code="full")
    assert n is not None
    assert n["func"] == "return msg;"
    assert n["wires"] == [["dbg1"]]


def test_get_node_function_omit_mode(tiny_flows: Path):
    n = flows.get_node(tiny_flows, "fn1", code="omit")
    assert n is not None
    assert "func" not in n
    assert "func_summary" not in n


def test_get_node_non_function_unaffected_by_code_mode(tiny_flows: Path):
    n = flows.get_node(tiny_flows, "dbg1", code="signatures")
    assert n is not None
    assert n["type"] == "debug"
    # No func / func_summary fields injected on non-function nodes.
    assert "func_summary" not in n


def test_get_node_signatures_extracts_helpers(tmp_path: Path):
    body = """\
const fmtTemp = (c) => Math.round(c * 10) / 10;

async function fetchState(topic) {
    return await context.get(topic);
}

node.on("close", () => { /* cleanup */ });

return msg;
"""
    data = [
        {"id": "tab1", "type": "tab", "label": "T"},
        {"id": "fn", "type": "function", "z": "tab1", "name": "complex",
         "func": body, "wires": []},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data))
    n = flows.get_node(p, "fn")
    assert n is not None
    summary = n["func_summary"]
    assert any("fmtTemp" in s for s in summary["signatures"])
    assert any("fetchState" in s for s in summary["signatures"])
    assert "close" in summary["node_events"]


def test_get_node_rejects_unknown_code_mode(tiny_flows: Path):
    with pytest.raises(ValueError, match="Unknown code mode"):
        flows.get_node(tiny_flows, "fn1", code="bogus")


def test_get_node_missing(tiny_flows: Path):
    assert flows.get_node(tiny_flows, "does-not-exist") is None


# --------------------------------------------------------------------------- #
# list_dangling tests                                                           #
# --------------------------------------------------------------------------- #


def _make_link_flows(tmp_path: Path, nodes: list[dict]) -> Path:
    """Write a minimal flows.json with one tab and the given extra nodes."""
    data: list[dict] = [{"id": "tab1", "type": "tab", "label": "T"}] + nodes
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data))
    return p


def test_list_dangling_clean_repo_returns_empty(tmp_path: Path):
    """A properly wired link pair should not appear in the dangling report."""
    # link-out references link-in and vice-versa (both .links populated).
    nodes = [
        {"id": "lo1", "type": "link out", "z": "tab1", "name": "out", "links": ["li1"], "wires": []},
        {"id": "li1", "type": "link in",  "z": "tab1", "name": "in",  "links": ["lo1"], "wires": []},
    ]
    p = _make_link_flows(tmp_path, nodes)
    result = flows.list_dangling(p)
    assert result["dangling_count"] == 0
    assert result["dangling"] == []
    assert result["total_checked"] == 2


def test_list_dangling_link_out_with_empty_links(tmp_path: Path):
    """link-out with an empty .links list is dangling."""
    nodes = [
        {"id": "lo1", "type": "link out", "z": "tab1", "name": "orphan-out", "links": [], "wires": []},
    ]
    p = _make_link_flows(tmp_path, nodes)
    result = flows.list_dangling(p)
    assert result["dangling_count"] == 1
    entry = result["dangling"][0]
    assert entry["node_id"] == "lo1"
    assert entry["reason"] == "link_out_empty_links"
    assert entry["broken_peers"] == []


def test_list_dangling_link_out_with_dead_peer(tmp_path: Path):
    """link-out whose .links all point at non-existent ids → all_peers_missing."""
    nodes = [
        {"id": "lo1", "type": "link out", "z": "tab1", "name": "dead-out",
         "links": ["ghost1", "ghost2"], "wires": []},
    ]
    p = _make_link_flows(tmp_path, nodes)
    result = flows.list_dangling(p)
    assert result["dangling_count"] == 1
    entry = result["dangling"][0]
    assert entry["node_id"] == "lo1"
    assert entry["reason"] == "all_peers_missing"
    assert set(entry["broken_peers"]) == {"ghost1", "ghost2"}


def test_list_dangling_link_in_with_no_source(tmp_path: Path):
    """link-in that no link-out references → link_in_no_source."""
    nodes = [
        {"id": "li1", "type": "link in", "z": "tab1", "name": "lonely-in",
         "links": [], "wires": []},
    ]
    p = _make_link_flows(tmp_path, nodes)
    result = flows.list_dangling(p)
    assert result["dangling_count"] == 1
    entry = result["dangling"][0]
    assert entry["node_id"] == "li1"
    assert entry["reason"] == "link_in_no_source"
    assert entry["broken_peers"] == []


def test_list_dangling_link_in_with_dead_peer_in_links(tmp_path: Path):
    """link-in whose own .links all point at ghosts → all_peers_missing (takes priority)."""
    nodes = [
        # lo1 points at li1 so it wouldn't be link_in_no_source — but li1's
        # own .links contains a dead id, which should be caught first.
        {"id": "lo1", "type": "link out", "z": "tab1", "name": "out",
         "links": ["li1"], "wires": []},
        {"id": "li1", "type": "link in",  "z": "tab1", "name": "broken-in",
         "links": ["ghost99"], "wires": []},
    ]
    p = _make_link_flows(tmp_path, nodes)
    result = flows.list_dangling(p)
    # lo1 is clean (lo1.links = [li1] which exists); li1.links = [ghost99] → dangling
    dangling_ids = {e["node_id"] for e in result["dangling"]}
    assert "li1" in dangling_ids
    li1_entry = next(e for e in result["dangling"] if e["node_id"] == "li1")
    assert li1_entry["reason"] == "all_peers_missing"
    assert li1_entry["broken_peers"] == ["ghost99"]


def test_list_dangling_tab_filter(tmp_path: Path):
    """Dangling nodes on a different tab are ignored when tab_id is given."""
    data = [
        {"id": "tab1", "type": "tab", "label": "A"},
        {"id": "tab2", "type": "tab", "label": "B"},
        # Dangling link-out on tab1
        {"id": "lo_t1", "type": "link out", "z": "tab1", "name": "dangling-on-t1",
         "links": [], "wires": []},
        # Dangling link-out on tab2
        {"id": "lo_t2", "type": "link out", "z": "tab2", "name": "dangling-on-t2",
         "links": [], "wires": []},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data))

    result = flows.list_dangling(p, tab_id="tab1")
    assert result["tab_id"] == "tab1"
    assert result["total_checked"] == 1
    assert result["dangling_count"] == 1
    assert result["dangling"][0]["node_id"] == "lo_t1"
