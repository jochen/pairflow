"""Read and write access to a Node-RED flows.json file.

The read helpers (`list_tabs`, `list_nodes`, `get_node`) load the file fresh
on every call. The mutating helpers all follow the same pattern:

  1. Capture the file's current mtime.
  2. Read and mutate the in-memory structure.
  3. Validate (for function nodes: JS syntax).
  4. `atomic_write` the result, refusing if the on-disk mtime has changed
     since step 1 (optimistic locking).
  5. Return useful information about what changed.

Restarting Node-RED so the change becomes live is a separate concern
(see the planned `nr_deploy` tool).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from .validate import ValidationResult, validate_function
from .writer import atomic_write, current_mtime

# Fields included in compact node listings. The full node JSON (with
# wires, configuration, embedded code) is available via get_node().
_COMPACT_FIELDS = ("id", "type", "name", "z", "x", "y", "disabled")


# --------------------------------------------------------------------------- #
# Read                                                                         #
# --------------------------------------------------------------------------- #


def _read(flows_file: Path) -> list[dict[str, Any]]:
    with flows_file.open("rb") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{flows_file} does not contain a JSON array at the top level.")
    return data


def list_tabs(flows_file: Path) -> list[dict[str, Any]]:
    """Return all tabs (workspaces) with id, label, disabled flag, and info text."""
    return [
        {
            "id": n["id"],
            "label": n.get("label", ""),
            "disabled": bool(n.get("disabled", False)),
            "info": n.get("info", ""),
        }
        for n in _read(flows_file)
        if n.get("type") == "tab"
    ]


def list_nodes(
    flows_file: Path,
    tab_id: str | None = None,
    node_type: str | None = None,
    name_contains: str | None = None,
) -> list[dict[str, Any]]:
    """Return a compact view of nodes, optionally filtered."""
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


# --------------------------------------------------------------------------- #
# Write                                                                        #
# --------------------------------------------------------------------------- #


def _new_id() -> str:
    """16-char hex id, matching modern Node-RED's convention."""
    return secrets.token_hex(8)


def _find_index(data: list[dict[str, Any]], node_id: str) -> int:
    for i, n in enumerate(data):
        if n.get("id") == node_id:
            return i
    raise KeyError(f"No node with id {node_id!r}")


def add_node(
    flows_file: Path,
    tab_id: str,
    node_type: str,
    props: dict[str, Any] | None = None,
    x: int | None = None,
    y: int | None = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    """Add a new node to `tab_id`.

    `props` is merged into the node after the required fields are set, so it
    can override defaults if you really mean to.

    For function nodes, the `func` body (if supplied in `props`) is
    JS-syntax-checked before the write.
    """
    mtime = current_mtime(flows_file)
    data = _read(flows_file)

    if not any(n.get("id") == tab_id and n.get("type") == "tab" for n in data):
        raise ValueError(f"No tab with id {tab_id!r}")

    node_id = node_id or _new_id()
    if any(n.get("id") == node_id for n in data):
        raise ValueError(f"Node id {node_id!r} already exists")

    new_node: dict[str, Any] = {
        "id": node_id,
        "type": node_type,
        "z": tab_id,
        "wires": [],
    }
    if props:
        new_node.update(props)
    # Ensure required positional/structural fields are present regardless of
    # what props supplied:
    new_node["id"] = node_id
    new_node["type"] = node_type
    new_node["z"] = tab_id
    if x is not None:
        new_node["x"] = x
    if y is not None:
        new_node["y"] = y
    new_node.setdefault("wires", [])

    _maybe_validate_function(new_node)

    data.append(new_node)
    atomic_write(flows_file, data, expected_mtime=mtime)
    return new_node


def update_node(
    flows_file: Path,
    node_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Apply a shallow patch to the node with `node_id`.

    For function nodes, if `func` is in the patch, the new body is
    syntax-checked before the write.
    """
    mtime = current_mtime(flows_file)
    data = _read(flows_file)
    idx = _find_index(data, node_id)

    candidate = dict(data[idx])
    candidate.update(patch)
    # id / type / z are not patchable through this path — preserve them.
    candidate["id"] = data[idx]["id"]
    candidate["type"] = data[idx]["type"]
    if "z" in data[idx]:
        candidate["z"] = data[idx]["z"]

    _maybe_validate_function(candidate)

    data[idx] = candidate
    atomic_write(flows_file, data, expected_mtime=mtime)
    return candidate


def delete_node(flows_file: Path, node_id: str) -> dict[str, Any]:
    """Delete the node with `node_id` and clean up references in other nodes.

    Returns a summary: the deleted node, plus the count of wire/link references
    that were removed elsewhere.
    """
    mtime = current_mtime(flows_file)
    data = _read(flows_file)
    idx = _find_index(data, node_id)
    deleted = data.pop(idx)

    removed_refs = 0
    for n in data:
        wires = n.get("wires")
        if isinstance(wires, list):
            for grp in wires:
                if isinstance(grp, list) and node_id in grp:
                    grp[:] = [d for d in grp if d != node_id]
                    removed_refs += 1
        # link in / link out nodes reference peers via .links
        if n.get("type") in ("link in", "link out"):
            links = n.get("links")
            if isinstance(links, list) and node_id in links:
                n["links"] = [d for d in links if d != node_id]
                removed_refs += 1

    atomic_write(flows_file, data, expected_mtime=mtime)
    return {"deleted": deleted, "references_removed": removed_refs}


def wire(
    flows_file: Path,
    src_id: str,
    src_port: int,
    dst_id: str,
) -> dict[str, Any]:
    """Add a wire from `src_id[src_port]` to `dst_id`. No-op if already wired."""
    mtime = current_mtime(flows_file)
    data = _read(flows_file)

    src_idx = _find_index(data, src_id)
    # Confirm destination exists (will raise KeyError if not):
    _find_index(data, dst_id)

    src = data[src_idx]
    wires = src.setdefault("wires", [])
    while len(wires) <= src_port:
        wires.append([])

    added = False
    if dst_id not in wires[src_port]:
        wires[src_port].append(dst_id)
        added = True

    if added:
        atomic_write(flows_file, data, expected_mtime=mtime)
    return {"src": src_id, "src_port": src_port, "dst": dst_id, "added": added}


def unwire(
    flows_file: Path,
    src_id: str,
    src_port: int,
    dst_id: str,
) -> dict[str, Any]:
    """Remove the wire from `src_id[src_port]` to `dst_id`, if present."""
    mtime = current_mtime(flows_file)
    data = _read(flows_file)

    src_idx = _find_index(data, src_id)
    src = data[src_idx]
    wires = src.get("wires") or []

    removed = False
    if src_port < len(wires) and dst_id in wires[src_port]:
        wires[src_port] = [d for d in wires[src_port] if d != dst_id]
        removed = True

    if removed:
        atomic_write(flows_file, data, expected_mtime=mtime)
    return {"src": src_id, "src_port": src_port, "dst": dst_id, "removed": removed}


# --------------------------------------------------------------------------- #
# Internals                                                                    #
# --------------------------------------------------------------------------- #


def _maybe_validate_function(node: dict[str, Any]) -> None:
    """Raise ValueError if the node is a function-node with invalid JS body."""
    if node.get("type") != "function":
        return
    func = node.get("func")
    if not isinstance(func, str):
        return
    result: ValidationResult = validate_function(func)
    if not result.ok:
        raise ValueError(f"Invalid function body: {result.error}")
