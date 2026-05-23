"""Pairflow MCP server.

Read tools (Tier 1, read-only):
  - nr_list_tabs
  - nr_list_nodes
  - nr_get_node

Write tools (Tier 1, mutate flows.json):
  - nr_add_node
  - nr_update_node
  - nr_delete_node
  - nr_wire
  - nr_unwire
  - nr_validate_function

Every write goes through atomic write + automatic timestamped backup +
optimistic mtime locking; for function nodes, the JS body is syntax-checked
before the write. Restarting Node-RED so the change becomes live is a
separate concern handled by Tier 2 (planned).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from . import flows, validate
from .config import Config


def build_server(config: Config) -> FastMCP:
    mcp = FastMCP(name="pairflow")
    flows_file = config.node_red.flows_file

    # ---- read ------------------------------------------------------------- #

    @mcp.tool
    def nr_list_tabs() -> list[dict[str, Any]]:
        """List all tabs (workspaces) in the Node-RED flows file.

        Each entry includes the tab id, its label as shown in the editor,
        whether the tab is disabled, and the free-text info field.
        """
        return flows.list_tabs(flows_file)

    @mcp.tool
    def nr_list_nodes(
        tab_id: str | None = None,
        node_type: str | None = None,
        name_contains: str | None = None,
    ) -> list[dict[str, Any]]:
        """List nodes with optional filters.

        - tab_id: restrict to a single tab (workspace). Pass the tab id, not its label.
        - node_type: e.g. "function", "mqtt out", "alexa-smart-home-v3".
        - name_contains: case-insensitive substring match on the node name.

        Returns a compact view (id, type, name, tab, x, y, disabled). Use
        nr_get_node for the full configuration of a specific node.
        """
        return flows.list_nodes(
            flows_file,
            tab_id=tab_id,
            node_type=node_type,
            name_contains=name_contains,
        )

    @mcp.tool
    def nr_get_node(node_id: str) -> dict[str, Any] | None:
        """Return the full JSON of a single node by id, or null if not found."""
        return flows.get_node(flows_file, node_id)

    # ---- write ------------------------------------------------------------ #

    @mcp.tool
    def nr_add_node(
        tab_id: str,
        node_type: str,
        props: dict[str, Any] | None = None,
        x: int | None = None,
        y: int | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        """Add a new node to a tab.

        - tab_id: target workspace.
        - node_type: e.g. "function", "inject", "mqtt out".
        - props: additional fields merged into the node (e.g. name, topic,
          broker, func body). Required structural fields (id, type, z) are
          enforced after merge.
        - x, y: canvas coordinates (optional).
        - node_id: explicit id; if omitted a 16-hex-char id is generated.

        If node_type is "function" and props.func is provided, the JS body
        is syntax-checked before write. Returns the new node JSON.
        """
        return flows.add_node(
            flows_file,
            tab_id=tab_id,
            node_type=node_type,
            props=props,
            x=x,
            y=y,
            node_id=node_id,
        )

    @mcp.tool
    def nr_update_node(node_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a shallow patch to a node.

        The fields `id`, `type`, and `z` are not patchable through this path
        — use add+delete if you need to change them. For function nodes,
        if `patch.func` is supplied, the new body is syntax-checked before
        the write. Returns the updated node JSON.
        """
        return flows.update_node(flows_file, node_id, patch)

    @mcp.tool
    def nr_delete_node(node_id: str) -> dict[str, Any]:
        """Delete a node and clean up wire/link references in other nodes.

        Returns `{"deleted": <node>, "references_removed": <count>}`.
        """
        return flows.delete_node(flows_file, node_id)

    @mcp.tool
    def nr_wire(src_id: str, src_port: int, dst_id: str) -> dict[str, Any]:
        """Add a wire from output `src_port` of `src_id` to `dst_id`.

        No-op (with `added: false`) if the wire already exists.
        """
        return flows.wire(flows_file, src_id=src_id, src_port=src_port, dst_id=dst_id)

    @mcp.tool
    def nr_unwire(src_id: str, src_port: int, dst_id: str) -> dict[str, Any]:
        """Remove the wire from output `src_port` of `src_id` to `dst_id`."""
        return flows.unwire(flows_file, src_id=src_id, src_port=src_port, dst_id=dst_id)

    @mcp.tool
    def nr_validate_function(code: str) -> dict[str, Any]:
        """Syntax-check a function-node body without writing it.

        Returns `{"ok": true}` if the JS parses, or
        `{"ok": false, "error": "..."}` with the node --check error.
        """
        result = validate.validate_function(code)
        return {"ok": result.ok, "error": result.error}

    return mcp
