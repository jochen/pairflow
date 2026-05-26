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
# include_sources tests                                                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sources_flows(tmp_path: Path) -> Path:
    """A minimal flow with a couple of connected nodes for source-lookup tests."""
    data = [
        {"id": "tab1", "type": "tab", "label": "T"},
        # A → port 0 → B, A → port 1 → C
        {"id": "nodeA", "type": "inject", "name": "Trigger", "z": "tab1",
         "wires": [["nodeB"], ["nodeC"]]},
        {"id": "nodeB", "type": "function", "name": "Process", "z": "tab1",
         "func": "return msg;", "wires": []},
        {"id": "nodeC", "type": "debug", "name": "Out", "z": "tab1", "wires": []},
        # link-out → link-in pair
        {"id": "lout1", "type": "link out", "name": "Sender", "z": "tab1",
         "links": ["lin1"]},
        {"id": "lin1", "type": "link in", "name": "Receiver", "z": "tab1",
         "links": ["lout1"]},
        # orphan node with no incoming wires
        {"id": "orphan", "type": "debug", "name": "Alone", "z": "tab1", "wires": []},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data))
    return p


def test_get_node_include_sources_basic(sources_flows: Path):
    """nodeB has nodeA on port 0 as its only upstream."""
    n = flows.get_node(sources_flows, "nodeB", include_sources=True)
    assert n is not None
    sources = n["sources"]
    assert len(sources) == 1
    s = sources[0]
    assert s["source_id"] == "nodeA"
    assert s["source_type"] == "inject"
    assert s["source_name"] == "Trigger"
    assert s["source_tab"] == "tab1"
    assert s["port_index"] == 0


def test_get_node_include_sources_multiple_ports(sources_flows: Path):
    """A node with two sources from different ports produces two entries."""
    # Add a second source that also wires to nodeB from a different node
    import json
    data = json.loads(sources_flows.read_text())
    data.append({
        "id": "nodeD", "type": "function", "name": "Alt", "z": "tab1",
        "func": "return msg;", "wires": [["nodeB"]],
    })
    sources_flows.write_text(json.dumps(data))

    n = flows.get_node(sources_flows, "nodeB", include_sources=True)
    assert n is not None
    sources = n["sources"]
    assert len(sources) == 2
    ids = {s["source_id"] for s in sources}
    assert ids == {"nodeA", "nodeD"}


def test_get_node_include_sources_same_source_two_ports(tmp_path: Path):
    """A single source with two output ports both wired to target → two entries."""
    data = [
        {"id": "tab1", "type": "tab", "label": "T"},
        {"id": "src", "type": "function", "name": "Multi", "z": "tab1",
         "func": "return [msg, msg];", "wires": [["tgt"], ["tgt"]]},
        {"id": "tgt", "type": "debug", "name": "Sink", "z": "tab1", "wires": []},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data))

    n = flows.get_node(p, "tgt", include_sources=True)
    assert n is not None
    sources = n["sources"]
    assert len(sources) == 2
    assert all(s["source_id"] == "src" for s in sources)
    port_indices = {s["port_index"] for s in sources}
    assert port_indices == {0, 1}


def test_get_node_include_sources_link_out(sources_flows: Path):
    """link-out with target in its .links appears as a source for the link-in."""
    n = flows.get_node(sources_flows, "lin1", include_sources=True)
    assert n is not None
    sources = n["sources"]
    assert len(sources) == 1
    s = sources[0]
    assert s["source_id"] == "lout1"
    assert s["source_type"] == "link out"
    assert s["port_index"] == 0


def test_get_node_include_sources_link_in_not_treated_as_source(sources_flows: Path):
    """link-in.links is UI metadata — link-in must NOT appear as a source."""
    # lin1 has lout1 in its .links (mirroring) — but lin1 is not a source for lout1
    n = flows.get_node(sources_flows, "lout1", include_sources=True)
    assert n is not None
    # lout1's sources should be empty (nothing wires INTO a link-out via wires)
    source_ids = {s["source_id"] for s in n["sources"]}
    assert "lin1" not in source_ids


def test_get_node_include_sources_default_off(sources_flows: Path):
    """Without include_sources, the result has no 'sources' key."""
    n = flows.get_node(sources_flows, "nodeB")
    assert n is not None
    assert "sources" not in n


def test_get_node_include_sources_no_upstreams(sources_flows: Path):
    """An orphan node with no incoming wires returns an empty sources list."""
    n = flows.get_node(sources_flows, "orphan", include_sources=True)
    assert n is not None
    assert n["sources"] == []
