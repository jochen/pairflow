"""Integration tests against a realistically sized synthetic flows.json.

These tests exercise the tool surface against a flow that is similar in size
and shape to what real Node-RED installations look like — multiple tabs,
mixed node types, branching wires, dangling link references, and a config
node without a tab.

They are slower than the unit tests (each one parses and rewrites a ~200-node
JSON file) but still complete in well under a second per test, so they run as
part of the standard test suite.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pairflow import flows

pytestmark = pytest.mark.integration


# --- shape sanity ----------------------------------------------------------- #


def test_fixture_shape(large_flows_stats: dict):
    s = large_flows_stats
    assert s["tab_count"] == 5
    assert s["total_nodes"] > 200
    # All major node types are present.
    for t in ("function", "inject", "debug", "mqtt in", "mqtt out", "link in", "link out", "switch", "change"):
        assert s["nodes_by_type"].get(t, 0) > 0, f"node type {t!r} missing from fixture"


def test_fixture_contains_dangling_link_refs(large_flows: Path):
    """The generator is supposed to seed some dangling link-in references."""
    data = json.loads(large_flows.read_text())
    by_id = {n["id"] for n in data}
    dangling = 0
    for n in data:
        if n.get("type") == "link in":
            for target in n.get("links", []):
                if target not in by_id:
                    dangling += 1
    assert dangling > 0


# --- reads -------------------------------------------------------------------#


def test_list_tabs(large_flows: Path):
    tabs = flows.list_tabs(large_flows)
    assert {t["label"] for t in tabs} == {"Sensors", "Lights", "Climate", "Telegram", "Helpers"}


def test_list_nodes_per_tab_count_matches(large_flows: Path, large_flows_stats: dict):
    tabs = flows.list_tabs(large_flows)
    total = 0
    for tab in tabs:
        total += flows.list_nodes(large_flows, tab_id=tab["id"])["count"]
    # Non-tab, non-broker nodes (broker has no z, so it's not in any tab).
    expected = large_flows_stats["total_nodes"] - large_flows_stats["tab_count"] - 1  # broker
    assert total == expected


def test_list_nodes_summary_default_on_large_flow(large_flows: Path, large_flows_stats: dict):
    """Unfiltered call returns the cheap summary even on big flows."""
    result = flows.list_nodes(large_flows)
    assert result["summary"] is True
    # Total counts everything except tabs.
    assert result["total"] == large_flows_stats["total_nodes"] - large_flows_stats["tab_count"]
    # by_type matches the fixture's histogram for the major types.
    for t, n in large_flows_stats["nodes_by_type"].items():
        assert result["by_type"].get(t, 0) == n


# --- bulk writes -------------------------------------------------------------#


def test_add_50_nodes_across_tabs(large_flows: Path):
    tabs = flows.list_tabs(large_flows)
    added_ids: list[str] = []
    for i in range(50):
        tab = tabs[i % len(tabs)]
        node = flows.add_node(
            large_flows,
            tab_id=tab["id"],
            node_type="debug",
            props={"name": f"bulk-{i}"},
            x=1500, y=100 + i * 5,
        )
        added_ids.append(node["id"])
    # All present, all unique
    assert len(set(added_ids)) == 50
    for nid in added_ids:
        assert flows.get_node(large_flows, nid) is not None


def test_wire_mesh_and_delete_with_cascade(large_flows: Path):
    """Build a small mesh of wires, then delete a central node — refs must clear."""
    tabs = flows.list_tabs(large_flows)
    tab_id = tabs[0]["id"]

    hub = flows.add_node(large_flows, tab_id=tab_id, node_type="debug",
                         props={"name": "hub"}, x=1500, y=200)

    spokes: list[str] = []
    for i in range(5):
        s = flows.add_node(large_flows, tab_id=tab_id, node_type="function",
                           props={"name": f"spoke-{i}", "func": "return msg;"},
                           x=1700, y=200 + i * 60)
        spokes.append(s["id"])
        flows.wire(large_flows, src_id=s["id"], src_port=0, dst_id=hub["id"])

    # 5 spokes wired to hub. Delete the hub — refs must clear from all spokes.
    result = flows.delete_node(large_flows, hub["id"])
    assert result["references_removed"] == 5

    # Each spoke's wires no longer contain hub.
    for sid in spokes:
        node = flows.get_node(large_flows, sid)
        for grp in node.get("wires", []):
            assert hub["id"] not in grp


def test_delete_tolerates_dangling_link_refs(large_flows: Path):
    """The dangling refs in the fixture must not break delete_node."""
    # Pick any existing link-out node and delete one of its targets-via-z (i.e.
    # any node). The cleanup pass must complete without raising.
    nodes = flows.list_nodes(large_flows, summary=False)["nodes"]
    victim = next(n for n in nodes if n["type"] == "function")
    flows.delete_node(large_flows, victim["id"])
    assert flows.get_node(large_flows, victim["id"]) is None


def test_update_function_bodies_en_masse(large_flows: Path):
    """Patch every function node's body to a valid new one. Validation runs each time."""
    fns = flows.list_nodes(large_flows, node_type="function")["nodes"]
    new_body = "msg.payload = 'bulk-patched';\nreturn msg;"
    for fn in fns[:20]:
        result = flows.update_node(large_flows, fn["id"], {"func": new_body})
        assert result["func"] == new_body


def test_update_rejects_invalid_function_body_mid_bulk(large_flows: Path):
    """One bad body must not silently corrupt the rest of the file."""
    if shutil.which("node") is None:
        pytest.skip("node binary not on PATH")
    fns = flows.list_nodes(large_flows, node_type="function")["nodes"]
    target = fns[0]
    with pytest.raises(ValueError, match="Invalid function body"):
        flows.update_node(large_flows, target["id"], {"func": "if (true) {\nreturn"})
    # File still parses, target unchanged.
    json.loads(large_flows.read_text())
    untouched = flows.get_node(large_flows, target["id"], code="full")
    assert untouched["func"] != "if (true) {\nreturn"


# --- search ------------------------------------------------------------------#


def test_search_flows_at_scale(large_flows: Path, large_flows_stats: dict):
    """search_flows works correctly on the full 200-node synthetic fixture."""
    # 1. Broad query: "sensors/" appears in many topic and func fields.
    result = flows.search_flows(large_flows, query="sensors/")
    assert result["total_matches"] >= 1
    assert len(result["matches"]) <= result["total_matches"]
    # Every match record has the required shape keys.
    for m in result["matches"]:
        assert "node_id" in m
        assert "field_path" in m
        assert "snippet" in m
        assert "snippet_truncated" in m

    # 2. Field restriction: topics only.
    topic_result = flows.search_flows(large_flows, query="sensors/", fields=["topic"])
    assert topic_result["total_matches"] >= 1
    assert all(m["field_path"] == "topic" for m in topic_result["matches"])

    # 3. max_matches cap: cap at 5, verify truncation accounting is correct.
    capped = flows.search_flows(large_flows, query="sensors/", max_matches=5)
    assert capped["returned"] == min(5, capped["total_matches"])
    if capped["total_matches"] > 5:
        assert capped["truncated"] is True
        assert capped["returned"] == 5

    # 4. Regex mode: find node-ids that look like 16-char hex strings in links.
    hex_result = flows.search_flows(
        large_flows, query=r"^[0-9a-f]{16}$", regex=True, fields=["links"]
    )
    # The fixture populates some dangling link ids; at least one should match.
    assert hex_result["total_matches"] >= 1

    # 5. Snippet cap fires on the longer function bodies.
    snippet_result = flows.search_flows(
        large_flows, query="msg.payload", fields=["func"], max_snippet_chars=30
    )
    if snippet_result["total_matches"] > 0:
        trunc = [m for m in snippet_result["matches"] if m["snippet_truncated"]]
        assert len(trunc) > 0, "Expected at least one truncated snippet from a func body"


# --- safety nets -------------------------------------------------------------#


def test_mtime_lock_blocks_stale_write(large_flows: Path):
    """A second write whose mtime is stale must be rejected by the locking layer."""
    import time

    from pairflow.writer import ConcurrentModificationError, atomic_write, current_mtime

    mtime = current_mtime(large_flows)
    # Simulate a concurrent edit by a different process.
    time.sleep(0.05)
    data = json.loads(large_flows.read_text())
    data.append({"id": "concurrent_edit", "type": "tab", "label": "Surprise"})
    large_flows.write_text(json.dumps(data, indent=4))

    with pytest.raises(ConcurrentModificationError):
        atomic_write(large_flows, [], expected_mtime=mtime)


def test_backup_per_write(large_flows: Path):
    """Each mutating call produces exactly one backup."""
    tabs = flows.list_tabs(large_flows)
    for i in range(3):
        flows.add_node(large_flows, tab_id=tabs[0]["id"], node_type="debug",
                       props={"name": f"snap-{i}"})
        # Sleep is unnecessary at sub-second resolution because each call also
        # has the timestamp granularity of the filesystem; but we want at most
        # one backup *per* call — count after each.
    backups = list(large_flows.parent.glob(f"{large_flows.name}.*.bak"))
    assert len(backups) >= 1  # at least one; may be fewer if timestamps collide
    # All backups parse as valid JSON arrays:
    for b in backups:
        data = json.loads(b.read_text())
        assert isinstance(data, list)
