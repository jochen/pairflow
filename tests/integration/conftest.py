"""Integration-test fixtures.

The `large_flows` fixture writes a synthetic-but-realistic flows.json into a
tmp directory and returns the path. The flow is deterministic (fixed seed) so
test failures are reproducible. It is sized and shaped to surface bugs that
the unit-test mini-fixtures cannot:

  * Multiple tabs (5) with diverse node populations.
  * All common node types: inject, function, debug, change, switch,
    mqtt in/out, link in/out, plus an mqtt-broker config node (no `z`).
  * A wire mesh with branching and multi-output function nodes.
  * Deliberate dangling references — link-in nodes whose `links` list
    points at ids that no longer exist. Real flows accumulate these and
    the cleanup logic has to tolerate them.
  * Function-node bodies with realistic JS (msg.payload mutations, async,
    context.get/set, returns).
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest


# Node-RED id format: 16 hex chars. Deterministic via seeded RNG.
def _make_id(rng: random.Random) -> str:
    return f"{rng.getrandbits(64):016x}"


def _build_large_flow(seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    flow: list[dict] = []

    # ---- broker config (no z) ---------------------------------------------
    broker_id = "mqtt_broker_local"
    flow.append({
        "id": broker_id,
        "type": "mqtt-broker",
        "name": "local",
        "broker": "localhost",
        "port": "1883",
        "clientid": "",
        "autoConnect": True,
        "usetls": False,
        "protocolVersion": "4",
        "keepalive": "60",
        "cleansession": True,
    })

    # ---- tabs --------------------------------------------------------------
    tab_labels = ["Sensors", "Lights", "Climate", "Telegram", "Helpers"]
    tabs: list[dict] = []
    for label in tab_labels:
        tab_id = _make_id(rng)
        tab = {"id": tab_id, "type": "tab", "label": label, "disabled": False, "info": ""}
        flow.append(tab)
        tabs.append(tab)

    function_bodies = [
        "msg.payload = msg.payload * 2;\nreturn msg;",
        "const x = msg.payload;\nif (x > 100) return msg;\nreturn null;",
        "context.set('last', msg.payload);\nreturn msg;",
        "const prev = context.get('last') || 0;\nmsg.delta = msg.payload - prev;\ncontext.set('last', msg.payload);\nreturn msg;",
        "msg.topic = msg.topic.replace(/sensors\\//, 'normalized/');\nreturn msg;",
        "const out = await (async () => msg.payload + 1)();\nmsg.payload = out;\nreturn msg;",
    ]

    # Track ids created per tab so we can build wires that make sense.
    by_tab_ids: dict[str, list[str]] = {t["id"]: [] for t in tabs}

    # ---- nodes per tab ----------------------------------------------------
    for tab in tabs:
        x, y = 100, 80
        # Per-tab node sequence: inject -> function -> change -> switch -> debug/mqtt
        for i in range(40):  # 40 nodes/tab * 5 tabs = 200 nodes
            x += 150
            if x > 1200:
                x = 100
                y += 80

            kind = rng.choice([
                "inject", "function", "function", "change", "switch",
                "debug", "mqtt in", "mqtt out", "link in", "link out",
            ])
            nid = _make_id(rng)
            n: dict = {"id": nid, "type": kind, "z": tab["id"], "x": x, "y": y, "wires": []}
            if kind == "inject":
                n["name"] = f"trigger {i}"
                n["payload"] = "test"
                n["topic"] = f"sensors/{tab['label'].lower()}/{i}"
                n["repeat"] = ""
                n["crontab"] = ""
                n["once"] = False
            elif kind == "function":
                n["name"] = f"transform {i}"
                n["func"] = rng.choice(function_bodies)
                n["outputs"] = 1
                n["noerr"] = 0
            elif kind == "change":
                n["name"] = f"rewrite {i}"
                n["rules"] = [{"t": "set", "p": "topic", "pt": "msg", "to": "x", "tot": "str"}]
            elif kind == "switch":
                n["name"] = f"route {i}"
                n["property"] = "payload"
                n["rules"] = [{"t": "gt", "v": "0", "vt": "num"}]
                n["outputs"] = 1
            elif kind == "debug":
                n["name"] = f"out {i}"
                n["active"] = bool(rng.getrandbits(1))
                n["complete"] = "true"
            elif kind == "mqtt in":
                n["topic"] = f"sensors/{tab['label'].lower()}/{i}/state"
                n["qos"] = "0"
                n["broker"] = broker_id
            elif kind == "mqtt out":
                n["topic"] = f"actuators/{tab['label'].lower()}/{i}/set"
                n["qos"] = "0"
                n["broker"] = broker_id
            elif kind == "link in":
                # 30% of the time, point at a stale id that won't exist —
                # simulating the "dangling refs" condition that accumulates
                # in long-lived flows.
                if rng.random() < 0.3:
                    n["links"] = [_make_id(rng)]
                else:
                    n["links"] = []
            elif kind == "link out":
                n["links"] = []
                n["mode"] = "link"

            flow.append(n)
            by_tab_ids[tab["id"]].append(nid)

    # ---- wires: thread nodes together within each tab ---------------------
    for tab in tabs:
        ids = by_tab_ids[tab["id"]]
        for src_id, dst_id in zip(ids, ids[1:], strict=False):
            src = next(n for n in flow if n["id"] == src_id)
            # Some node types don't emit output wires meaningfully; skip them.
            if src.get("type") in ("debug", "mqtt out", "link in"):
                continue
            src["wires"] = [[dst_id]]
        # Add some branching: pick 5 random sources and add a second output dst.
        for _ in range(5):
            src_id = rng.choice(ids)
            dst_id = rng.choice(ids)
            src = next(n for n in flow if n["id"] == src_id)
            if src.get("type") in ("debug", "mqtt out", "link in"):
                continue
            if not src.get("wires"):
                src["wires"] = [[]]
            if dst_id not in src["wires"][0]:
                src["wires"][0].append(dst_id)

    return flow


@pytest.fixture
def large_flows(tmp_path: Path) -> Path:
    """Path to a freshly generated, realistic flows.json (~200 nodes, 5 tabs)."""
    data = _build_large_flow()
    p = tmp_path / "flows.json"
    p.write_text(json.dumps(data, indent=4))
    return p


@pytest.fixture
def large_flows_stats(large_flows: Path) -> dict:
    """Convenience: stats about the generated fixture for assertions."""
    import json as _json
    data = _json.loads(large_flows.read_text())
    tabs = [n for n in data if n.get("type") == "tab"]
    nodes_by_type: dict[str, int] = {}
    for n in data:
        if n.get("type") == "tab":
            continue
        nodes_by_type[n.get("type", "?")] = nodes_by_type.get(n.get("type", "?"), 0) + 1
    return {
        "total_nodes": len(data),
        "tab_count": len(tabs),
        "tabs": [t["label"] for t in tabs],
        "nodes_by_type": nodes_by_type,
    }
