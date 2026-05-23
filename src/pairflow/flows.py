"""Read access to a Node-RED flows.json file.

Read-only for now. Each call reads the file fresh; caching by mtime is a
later optimization.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Node fields that are useful in compact list responses. Anything else
# (wires, configuration dicts, large embedded code) is omitted from list
# views to keep results small; use get_node() for the full payload.
_COMPACT_FIELDS = ("id", "type", "name", "z", "x", "y", "disabled")


def _read(flows_file: Path) -> list[dict[str, Any]]:
    with flows_file.open("rb") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{flows_file} does not contain a JSON array at the top level.")
    return data


def list_tabs(flows_file: Path) -> list[dict[str, Any]]:
    """Return all tabs (workspaces) with id, label, disabled flag, and info text."""
    nodes = _read(flows_file)
    return [
        {
            "id": n["id"],
            "label": n.get("label", ""),
            "disabled": bool(n.get("disabled", False)),
            "info": n.get("info", ""),
        }
        for n in nodes
        if n.get("type") == "tab"
    ]


def list_nodes(
    flows_file: Path,
    tab_id: str | None = None,
    node_type: str | None = None,
    name_contains: str | None = None,
) -> list[dict[str, Any]]:
    """Return a compact view of nodes, optionally filtered by tab, type, or name substring.

    Config nodes (no 'z' field) are included by default; pass tab_id to scope to a tab.
    """
    nodes = _read(flows_file)
    out: list[dict[str, Any]] = []
    needle = name_contains.lower() if name_contains else None

    for n in nodes:
        t = n.get("type")
        if t == "tab":
            continue
        if tab_id is not None and n.get("z") != tab_id:
            continue
        if node_type is not None and t != node_type:
            continue
        if needle is not None and needle not in str(n.get("name", "")).lower():
            continue
        out.append({k: n[k] for k in _COMPACT_FIELDS if k in n})

    return out


def get_node(flows_file: Path, node_id: str) -> dict[str, Any] | None:
    """Return the full node JSON, or None if no node with that id exists."""
    for n in _read(flows_file):
        if n.get("id") == node_id:
            return n
    return None
