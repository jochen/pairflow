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
# search_flows                                                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture
def search_flows_file(tmp_path: Path) -> Path:
    """A small fixture purpose-built for search tests."""
    data = [
        {"id": "tab1", "type": "tab", "label": "Main"},
        {
            "id": "fn1", "type": "function", "name": "Compute Temp",
            "z": "tab1",
            "func": "msg.payload = msg.payload * 1.8 + 32;\nreturn msg;",
            "wires": [["dbg1"]],
        },
        {
            "id": "mqin1", "type": "mqtt in", "name": "Climate sensor",
            "z": "tab1",
            "topic": "sensors/climate/temperature",
            "broker": "brk1",
            "wires": [["fn1"]],
        },
        {
            "id": "sw1", "type": "switch", "name": "Route",
            "z": "tab1",
            "rules": [
                {"t": "gt", "v": "0", "vt": "num"},
                {"t": "eq", "v": "reset", "vt": "str"},
            ],
            "wires": [["dbg1"], ["dbg1"]],
        },
        {"id": "dbg1", "type": "debug", "name": "out", "z": "tab1"},
        {
            "id": "lnkout1", "type": "link out", "name": "forward",
            "z": "tab1",
            "links": ["fn1", "deadbeef12345678"],
        },
        # Config node with no z (like an mqtt-broker)
        {"id": "brk1", "type": "mqtt-broker", "name": "local", "broker": "localhost"},
    ]
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data))
    return p


def test_search_basic_name_match(search_flows_file: Path):
    result = flows.search_flows(search_flows_file, query="Climate")
    assert result["truncated"] is False
    ids = {m["node_id"] for m in result["matches"]}
    # "Climate sensor" name matches mqin1; "Climate" also in topic string.
    assert "mqin1" in ids


def test_search_func_body(search_flows_file: Path):
    result = flows.search_flows(search_flows_file, query="1.8 + 32")
    assert result["total_matches"] >= 1
    fn_match = next(m for m in result["matches"] if m["node_id"] == "fn1")
    assert fn_match["field_path"] == "func"
    assert "1.8 + 32" in fn_match["snippet"]


def test_search_fields_restricts_to_func(search_flows_file: Path):
    # "temperature" appears in the topic field of mqin1, but NOT in any func body.
    result = flows.search_flows(search_flows_file, query="temperature", fields=["func"])
    assert result["total_matches"] == 0

    # "1.8" only lives in func; unrestricted should also find it there.
    result2 = flows.search_flows(search_flows_file, query="1.8", fields=["func"])
    assert result2["total_matches"] >= 1
    assert all(m["field_path"] == "func" for m in result2["matches"])


def test_search_fields_topic(search_flows_file: Path):
    result = flows.search_flows(search_flows_file, query="sensors/climate", fields=["topic"])
    assert result["total_matches"] == 1
    assert result["matches"][0]["node_id"] == "mqin1"


def test_search_fields_links_array(search_flows_file: Path):
    # "fn1" appears inside lnkout1's links list.
    result = flows.search_flows(search_flows_file, query="fn1", fields=["links"])
    assert result["total_matches"] >= 1
    assert any(m["node_id"] == "lnkout1" for m in result["matches"])


def test_search_regex_mode(search_flows_file: Path):
    # Match anything that looks like a decimal number in func bodies.
    result = flows.search_flows(search_flows_file, query=r"\d+\.\d+", regex=True, fields=["func"])
    assert result["total_matches"] >= 1


def test_search_invalid_regex_raises(search_flows_file: Path):
    with pytest.raises(ValueError, match="Invalid regex"):
        flows.search_flows(search_flows_file, query="[unclosed", regex=True)


def test_search_case_sensitive(search_flows_file: Path):
    # "compute" (lowercase) should not match "Compute Temp" when case_sensitive=True.
    result_cs = flows.search_flows(search_flows_file, query="compute", case_sensitive=True)
    ids_cs = {m["node_id"] for m in result_cs["matches"]}
    assert "fn1" not in ids_cs or all(
        m["field_path"] != "name" for m in result_cs["matches"] if m["node_id"] == "fn1"
    )

    # Case-insensitive (default) should match.
    result_ci = flows.search_flows(search_flows_file, query="compute")
    ids_ci = {m["node_id"] for m in result_ci["matches"]}
    assert "fn1" in ids_ci


def test_search_empty_query_raises(search_flows_file: Path):
    with pytest.raises(ValueError, match="query must not be empty"):
        flows.search_flows(search_flows_file, query="")


def test_search_max_matches_cap_triggers_truncated(search_flows_file: Path):
    # "msg" appears in func bodies and possibly names; use cap=1 to force truncation.
    result = flows.search_flows(search_flows_file, query="msg", max_matches=1)
    assert result["returned"] == 1
    if result["total_matches"] > 1:
        assert result["truncated"] is True
    assert len(result["matches"]) == result["returned"]


def test_search_snippet_truncation(search_flows_file: Path):
    # The func body is longer than 20 chars; cap at 20 to force truncation.
    result = flows.search_flows(
        search_flows_file, query="payload", fields=["func"], max_snippet_chars=20
    )
    assert result["total_matches"] >= 1
    trunc_match = next(
        (m for m in result["matches"] if m.get("snippet_truncated")), None
    )
    assert trunc_match is not None
    assert len(trunc_match["snippet"]) == 20
    assert trunc_match["snippet_full_chars"] > 20


def test_search_switch_rules_nested(search_flows_file: Path):
    # "reset" appears inside sw1's rules[1].v
    result = flows.search_flows(search_flows_file, query="reset")
    assert result["total_matches"] >= 1
    sw_match = next((m for m in result["matches"] if m["node_id"] == "sw1"), None)
    assert sw_match is not None
    assert "rules" in sw_match["field_path"]
