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
import re
import secrets
from collections import Counter
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


def list_tabs(
    flows_file: Path,
    include_info: bool = False,
) -> list[dict[str, Any]]:
    """Return all tabs (workspaces) with id, label, and disabled flag.

    `info` (markdown notes attached to a tab) is omitted unless
    `include_info=True` — those notes can be long and inflate the response.
    """
    out: list[dict[str, Any]] = []
    for n in _read(flows_file):
        if n.get("type") != "tab":
            continue
        entry: dict[str, Any] = {
            "id": n["id"],
            "label": n.get("label", ""),
            "disabled": bool(n.get("disabled", False)),
        }
        if include_info:
            entry["info"] = n.get("info", "")
        out.append(entry)
    return out


def list_nodes(
    flows_file: Path,
    tab_id: str | None = None,
    node_type: str | None = None,
    name_contains: str | None = None,
    summary: bool | None = None,
) -> dict[str, Any]:
    """List nodes with optional filters; returns either a summary or a list.

    When `summary` is None (default), the function picks: a summary when no
    filter is set (cheap overview of a flow with hundreds or thousands of
    nodes) and a full compact list when any filter narrows the scope.

    Pass `summary=False` to force the full list even without filters; pass
    `summary=True` to force a summary even with filters (useful for "how
    many function nodes are in tab X?" style queries).

    Summary shape::

        {"summary": True, "total": N,
         "by_tab": [{"id", "label", "count", "by_type": {...}}, ...],
         "by_type": {type: count, ...},
         "hint": "..."}

    List shape::

        {"summary": False, "count": N,
         "filter": {"tab_id", "node_type", "name_contains"},
         "nodes": [{id, type, name, z, x, y, disabled}, ...]}
    """
    raw = _read(flows_file)
    needle = name_contains.lower() if name_contains else None
    has_filter = any(x is not None for x in (tab_id, node_type, name_contains))
    if summary is None:
        summary = not has_filter

    tab_labels = {n["id"]: n.get("label", "") for n in raw if n.get("type") == "tab"}

    matches: list[dict[str, Any]] = []
    for n in raw:
        t = n.get("type")
        if t == "tab":
            continue
        if tab_id is not None and n.get("z") != tab_id:
            continue
        if node_type is not None and t != node_type:
            continue
        if needle is not None and needle not in str(n.get("name", "")).lower():
            continue
        matches.append(n)

    if summary:
        by_tab: dict[str | None, Counter[str]] = {}
        for n in matches:
            z = n.get("z")
            by_tab.setdefault(z, Counter())[n.get("type", "?")] += 1
        tabs_out = [
            {
                "id": z,
                "label": tab_labels.get(z, "" if z else "(no tab)"),
                "count": sum(c.values()),
                "by_type": dict(c.most_common()),
            }
            for z, c in by_tab.items()
        ]
        tabs_out.sort(key=lambda r: (-r["count"], str(r["id"])))
        by_type_all: Counter[str] = Counter()
        for c in by_tab.values():
            by_type_all.update(c)
        return {
            "summary": True,
            "total": len(matches),
            "by_tab": tabs_out,
            "by_type": dict(by_type_all.most_common()),
            "hint": (
                "Summary view. Pass tab_id, node_type, or name_contains to "
                "see individual nodes, or summary=False to force the full list."
            ),
        }

    out_list = [{k: n[k] for k in _COMPACT_FIELDS if k in n} for n in matches]
    return {
        "summary": False,
        "count": len(out_list),
        "filter": {
            "tab_id": tab_id,
            "node_type": node_type,
            "name_contains": name_contains,
        },
        "nodes": out_list,
    }


# Modes for the `func` body returned by get_node():
#   "full"       — full original body
#   "signatures" — compact summary (line count, head, declared helpers)
#   "omit"       — drop the func field entirely
_GET_NODE_CODE_MODES = ("full", "signatures", "omit")


def get_node(
    flows_file: Path,
    node_id: str,
    code: str = "signatures",
    include_sources: bool = False,
) -> dict[str, Any] | None:
    """Return the full node JSON, or None if no node with that id exists.

    For function nodes, the `func` body can dominate the response (multi-KB).
    The `code` mode controls how it is rendered:

      * ``"signatures"`` (default) — replaces `func` with a `func_summary`
        object (line count, character count, the first few non-blank lines,
        and any top-level declared helpers / `node.on()` event names).
      * ``"full"`` — keep the original `func` body in the response.
      * ``"omit"`` — drop the `func` field entirely.

    Non-function nodes are unaffected by `code`.

    When `include_sources=True`, a ``sources`` key is added to the result with
    one entry per upstream connection::

        [{"source_id": str, "source_type": str, "source_name": str,
          "source_tab": str | None, "port_index": int}, ...]

    This is the reverse of ``wires`` — it answers "which nodes feed this node?"
    Default off to keep the common case cheap.
    """
    if code not in _GET_NODE_CODE_MODES:
        raise ValueError(
            f"Unknown code mode {code!r}; expected one of {_GET_NODE_CODE_MODES}"
        )
    data = _read(flows_file)
    for n in data:
        if n.get("id") == node_id:
            if n.get("type") == "function" and isinstance(n.get("func"), str) and code != "full":
                n = dict(n)
                func = n.pop("func")
                if code == "signatures":
                    n["func_summary"] = _summarize_function_body(func)
            if include_sources:
                n = dict(n)
                n["sources"] = _find_sources(data, node_id)
            return n
    return None


def _find_sources(
    data: list[dict[str, Any]],
    node_id: str,
) -> list[dict[str, Any]]:
    """Return all nodes that have an outgoing connection to `node_id`.

    Each entry describes one connection::

        {"source_id": str, "source_type": str, "source_name": str,
         "source_tab": str | None, "port_index": int}

    Rules:
    - Regular nodes: each output-port group in ``wires`` that contains
      ``node_id`` produces one entry; ``port_index`` is the group index.
    - ``link out`` nodes: if ``node_id`` is in the node's ``links`` array,
      one entry with ``port_index=0`` is produced.
    - ``link in`` nodes: their ``links`` array is UI metadata (mirrors the
      link-out side) and does NOT make them a source — skip them.
    - The target node itself is never included.
    """
    sources: list[dict[str, Any]] = []
    for n in data:
        nid = n.get("id")
        if nid == node_id:
            continue
        ntype = n.get("type", "")

        if ntype == "link in":
            # link-in.links is UI metadata only — not a routing source
            continue

        if ntype == "link out":
            links = n.get("links")
            if isinstance(links, list) and node_id in links:
                sources.append({
                    "source_id": nid,
                    "source_type": ntype,
                    "source_name": n.get("name", ""),
                    "source_tab": n.get("z") or None,
                    "port_index": 0,
                })
            continue

        # Regular node: walk each output-port group
        wires = n.get("wires")
        if not isinstance(wires, list):
            continue
        for port_idx, grp in enumerate(wires):
            if isinstance(grp, list) and node_id in grp:
                sources.append({
                    "source_id": nid,
                    "source_type": ntype,
                    "source_name": n.get("name", ""),
                    "source_tab": n.get("z") or None,
                    "port_index": port_idx,
                })

    return sources


def _summarize_function_body(code: str) -> dict[str, Any]:
    """Extract a compact summary of a Node-RED function-node body.

    Returns line/char counts, the first few non-blank lines, any top-level
    `function` / `const|let|var fn = ...` declarations, and any `node.on()`
    event names. Pattern-based, not a real parser — good enough to give the
    AI structure clues without shipping kilobytes of JS.
    """
    lines = code.splitlines()
    non_blank = [ln for ln in lines if ln.strip()]
    head = non_blank[:8]

    signatures: list[str] = []
    seen: set[str] = set()

    # function fooBar(a, b) {
    for m in re.finditer(
        r"^[ \t]*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)",
        code,
        flags=re.MULTILINE,
    ):
        sig = m.group(0).strip()
        if sig not in seen:
            seen.add(sig)
            signatures.append(sig)

    # const|let|var name = (...) => / function(...) {
    for m in re.finditer(
        r"^[ \t]*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:async\s+)?(?:function\s*\([^)]*\)|\([^)]*\)\s*=>)",
        code,
        flags=re.MULTILINE,
    ):
        sig = m.group(0).strip()
        if sig not in seen:
            seen.add(sig)
            signatures.append(sig)

    node_events = sorted({
        m.group(1)
        for m in re.finditer(r"node\.on\(\s*['\"]([^'\"]+)['\"]", code)
    })

    return {
        "lines": len(lines),
        "chars": len(code),
        "head": head,
        "signatures": signatures,
        "node_events": node_events,
    }


def list_dangling(
    flows_file: Path,
    tab_id: str | None = None,
    types: list[str] | None = None,
) -> dict[str, Any]:
    """Find dangling link nodes (link in / link out with broken or missing peers).

    Scans nodes matching `types` (default: both ``"link in"`` and ``"link out"``),
    optionally restricted to `tab_id`, and reports nodes that are dangling.

    Semantics:

    * **link out** — dangling if ``.links`` is absent/empty, or all listed
      peer ids point at nodes that don't exist (``all_peers_missing``).
    * **link in** — dangling if its own ``.links`` lists ids that don't exist
      (``all_peers_missing``), *or* if no ``link out`` node's ``.links``
      references this node's id (``link_in_no_source``).  The first failing
      rule wins; only one ``reason`` is reported per node.

    Return shape::

        {
            "tab_id": tab_id,            # None when not filtered
            "total_checked": int,        # nodes matching types (in scope)
            "dangling_count": int,
            "dangling": [
                {
                    "node_id": str,
                    "type": str,         # "link in" or "link out"
                    "name": str,
                    "tab": str | None,   # z field
                    "reason": str,       # "no_links_field" | "all_peers_missing"
                                         # | "link_in_no_source" | "link_out_empty_links"
                    "broken_peers": list[str],  # ids in .links that don't exist
                },
                ...
            ],
        }
    """
    if types is None:
        types = ["link in", "link out"]
    types_set = set(types)

    raw = _read(flows_file)
    all_ids = {n["id"] for n in raw if "id" in n}

    # Build the set of link-in ids that are referenced by at least one link-out.
    link_in_ids_with_source: set[str] = set()
    for n in raw:
        if n.get("type") == "link out":
            for peer in n.get("links") or []:
                link_in_ids_with_source.add(peer)

    candidates = [
        n for n in raw
        if n.get("type") in types_set
        and (tab_id is None or n.get("z") == tab_id)
    ]

    dangling: list[dict[str, Any]] = []
    for n in candidates:
        nid = n.get("id", "")
        ntype = n.get("type", "")
        name = n.get("name", "")
        tab = n.get("z")
        links = n.get("links")

        reason: str | None = None
        broken_peers: list[str] = []

        if ntype == "link out":
            if not isinstance(links, list):
                reason = "no_links_field"
            elif len(links) == 0:
                reason = "link_out_empty_links"
            else:
                broken = [p for p in links if p not in all_ids]
                if len(broken) == len(links):
                    reason = "all_peers_missing"
                    broken_peers = broken

        elif ntype == "link in":
            if isinstance(links, list) and links:
                broken = [p for p in links if p not in all_ids]
                if broken and len(broken) == len(links):
                    reason = "all_peers_missing"
                    broken_peers = broken
            if reason is None and nid not in link_in_ids_with_source:
                reason = "link_in_no_source"

        if reason is not None:
            dangling.append({
                "node_id": nid,
                "type": ntype,
                "name": name,
                "tab": tab,
                "reason": reason,
                "broken_peers": broken_peers,
            })

    return {
        "tab_id": tab_id,
        "total_checked": len(candidates),
        "dangling_count": len(dangling),
        "dangling": dangling,
    }


# --------------------------------------------------------------------------- #
# Search                                                                       #
# --------------------------------------------------------------------------- #


def search_flows(
    flows_file: "Path",
    query: str,
    fields: "list[str] | None" = None,
    regex: bool = False,
    case_sensitive: bool = False,
    max_matches: int = 100,
    max_snippet_chars: int = 120,
) -> "dict[str, Any]":
    """Global string search over the flows.json document.

    Walks every node in the document and recursively visits all string values,
    matching against `query`.  Substring match by default; set `regex=True` to
    use a Python `re.search` pattern.  Matching is case-insensitive by default;
    set `case_sensitive=True` to override.

    `fields` restricts matching to string values whose parent key name (the
    final segment of the field path, or the parent key for list elements) is in
    the set.  E.g. ``fields=["func"]`` searches only function-node bodies at
    *any* depth.  Pass ``None`` (default) to search every string value.

    Returns::

        {
            "query": str,
            "regex": bool,
            "total_matches": int,     # actual count (may exceed max_matches)
            "returned": int,          # number of matches in the payload
            "truncated": bool,        # True iff total_matches > max_matches
            "matches": [
                {
                    "node_id": str | None,
                    "type": str | None,
                    "name": str | None,
                    "tab": str | None,
                    "field_path": str,        # e.g. "func", "rules[3].t"
                    "snippet": str,
                    "snippet_truncated": bool,
                    "snippet_full_chars": int,  # only when snippet_truncated
                },
                ...
            ],
        }
    """
    if not query:
        raise ValueError("query must not be empty")

    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex {query!r}: {exc}") from exc

        def _matches(s: str) -> bool:
            return bool(pattern.search(s))
    else:
        needle = query if case_sensitive else query.lower()

        def _matches(s: str) -> bool:
            hay = s if case_sensitive else s.lower()
            return needle in hay

    field_set: set[str] | None = set(fields) if fields is not None else None

    data = _read(flows_file)

    total = 0
    matches: list[dict[str, Any]] = []

    def _collect(node: dict[str, Any], value: Any, path: str, parent_key: str | None) -> None:
        nonlocal total
        if isinstance(value, str):
            if field_set is not None and parent_key not in field_set:
                return
            if not _matches(value):
                return
            total += 1
            if len(matches) < max_matches:
                rec: dict[str, Any] = {
                    "node_id": node.get("id"),
                    "type": node.get("type"),
                    "name": node.get("name"),
                    "tab": node.get("z"),
                    "field_path": path,
                    "snippet": value,
                    "snippet_truncated": False,
                }
                full_len = len(value)
                if max_snippet_chars and full_len > max_snippet_chars:
                    rec["snippet"] = value[:max_snippet_chars]
                    rec["snippet_truncated"] = True
                    rec["snippet_full_chars"] = full_len
                matches.append(rec)
        elif isinstance(value, dict):
            for k, v in value.items():
                child_path = f"{path}.{k}" if path else k
                _collect(node, v, child_path, k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                child_path = f"{path}[{i}]"
                # For list elements we inherit the parent key so that
                # fields=["links"] matches items inside a links array.
                _collect(node, v, child_path, parent_key)

    for node in data:
        for key, val in node.items():
            _collect(node, val, key, key)

    return {
        "query": query,
        "regex": regex,
        "total_matches": total,
        "returned": len(matches),
        "truncated": total > max_matches,
        "matches": matches,
    }


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
    verbose: bool = True,
) -> dict[str, Any]:
    """Apply a shallow patch to the node with `node_id`.

    For function nodes, if `func` is in the patch, the new body is
    syntax-checked before the write.

    When ``verbose=False``, returns a compact summary
    ``{ok, node_id, applied_keys}`` instead of the full post-patch node.
    Default is ``True`` (full node) for backward compatibility.

    When the patch toggles ``active`` on a ``debug``-type node, the result
    includes a ``hint`` reminding the caller that the change only takes effect
    after ``nr_deploy``.
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

    if not verbose:
        return {"ok": True, "node_id": node_id, "applied_keys": list(patch.keys())}

    result: dict[str, Any] = dict(candidate)
    if candidate.get("type", "").startswith("debug") and "active" in patch:
        result["hint"] = "Toggling debug 'active' takes effect only after nr_deploy."
    return result


def delete_node(
    flows_file: Path,
    node_id: str,
    missing_ok: bool = False,
) -> dict[str, Any]:
    """Delete the node with `node_id` and clean up references in other nodes.

    Returns a summary: the deleted node, plus the count of wire/link references
    that were removed elsewhere.

    With `missing_ok=True`, a non-existent id returns
    `{deleted: None, references_removed: 0, found: False}` instead of raising
    `KeyError` — useful for cleanup loops over stale id lists.
    """
    mtime = current_mtime(flows_file)
    data = _read(flows_file)
    try:
        idx = _find_index(data, node_id)
    except KeyError:
        if missing_ok:
            return {"deleted": None, "references_removed": 0, "found": False}
        raise
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
    """Connect `src_id` to `dst_id`. Idempotent.

    Two shapes, dispatched by node type:

    * **Wire pair** — `src.wires[src_port]` gets `dst_id` appended.
    * **Link pair** (`src` is `link out`, `dst` is `link in`) — the `.links`
      array on *both* nodes is updated, because Node-RED's editor and Pairflow's
      direct-patch model treat the link-in's `.links` as UI metadata that must
      mirror the link-out's `.links` (the real routing source). `src_port` is
      ignored for link pairs.

    Mixing a link node with a non-link node on either side raises `ValueError`
    rather than silently writing the wrong field.
    """
    mtime = current_mtime(flows_file)
    data = _read(flows_file)

    src_idx = _find_index(data, src_id)
    dst_idx = _find_index(data, dst_id)
    src = data[src_idx]
    dst = data[dst_idx]

    if _is_link_node(src) or _is_link_node(dst):
        _require_link_pair(src, dst)
        added = _add_link_ref(src, dst_id) | _add_link_ref(dst, src_id)
        if added:
            atomic_write(flows_file, data, expected_mtime=mtime)
        return {"src": src_id, "dst": dst_id, "added": added, "kind": "link"}

    wires = src.setdefault("wires", [])
    while len(wires) <= src_port:
        wires.append([])

    added = False
    if dst_id not in wires[src_port]:
        wires[src_port].append(dst_id)
        added = True

    if added:
        atomic_write(flows_file, data, expected_mtime=mtime)
    return {
        "src": src_id,
        "src_port": src_port,
        "dst": dst_id,
        "added": added,
        "kind": "wire",
    }


def unwire(
    flows_file: Path,
    src_id: str,
    src_port: int,
    dst_id: str,
) -> dict[str, Any]:
    """Disconnect `src_id` from `dst_id`. No-op if not connected.

    Symmetric to `wire`: link pairs are unlinked from both nodes' `.links`,
    wire pairs from `src.wires[src_port]`. `src_port` is ignored for link
    pairs.
    """
    mtime = current_mtime(flows_file)
    data = _read(flows_file)

    src_idx = _find_index(data, src_id)
    dst_idx = _find_index(data, dst_id)
    src = data[src_idx]
    dst = data[dst_idx]

    if _is_link_node(src) or _is_link_node(dst):
        _require_link_pair(src, dst)
        removed = _remove_link_ref(src, dst_id) | _remove_link_ref(dst, src_id)
        if removed:
            atomic_write(flows_file, data, expected_mtime=mtime)
        return {"src": src_id, "dst": dst_id, "removed": removed, "kind": "link"}

    wires = src.get("wires") or []
    removed = False
    if src_port < len(wires) and dst_id in wires[src_port]:
        wires[src_port] = [d for d in wires[src_port] if d != dst_id]
        removed = True

    if removed:
        atomic_write(flows_file, data, expected_mtime=mtime)
    return {
        "src": src_id,
        "src_port": src_port,
        "dst": dst_id,
        "removed": removed,
        "kind": "wire",
    }


def _is_link_node(node: dict[str, Any]) -> bool:
    return node.get("type") in ("link in", "link out")


def _require_link_pair(src: dict[str, Any], dst: dict[str, Any]) -> None:
    if src.get("type") != "link out" or dst.get("type") != "link in":
        raise ValueError(
            f"Link wiring requires src='link out' and dst='link in'; "
            f"got src={src.get('type')!r} ({src.get('id')!r}), "
            f"dst={dst.get('type')!r} ({dst.get('id')!r})"
        )


def _add_link_ref(node: dict[str, Any], peer_id: str) -> bool:
    links = node.setdefault("links", [])
    if peer_id in links:
        return False
    links.append(peer_id)
    return True


def _remove_link_ref(node: dict[str, Any], peer_id: str) -> bool:
    links = node.get("links")
    if not isinstance(links, list) or peer_id not in links:
        return False
    node["links"] = [d for d in links if d != peer_id]
    return True


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
