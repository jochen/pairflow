"""Pairflow MCP server.

Exposes a tier of read-only flow inspection tools for the initial release.
Write-path tools (add_node, wire, update_node, deploy) will follow.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from . import flows
from .config import Config


def build_server(config: Config) -> FastMCP:
    mcp = FastMCP(name="pairflow")
    flows_file = config.node_red.flows_file

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
        """List nodes in the flows file with optional filters.

        - tab_id: restrict to a single tab (workspace). Pass the tab id, not its label.
        - node_type: restrict to a node type, e.g. "function", "mqtt out", "alexa-smart-home-v3".
        - name_contains: case-insensitive substring match on the node's name field.

        Returns a compact view (id, type, name, tab, x, y, disabled). Use nr_get_node
        for the full configuration of a specific node.
        """
        return flows.list_nodes(
            flows_file,
            tab_id=tab_id,
            node_type=node_type,
            name_contains=name_contains,
        )

    @mcp.tool
    def nr_get_node(node_id: str) -> dict[str, Any] | None:
        """Return the full JSON of a single node by id, or null if not found.

        Includes wires, full configuration, and (for function nodes) the source
        code of the function body.
        """
        return flows.get_node(flows_file, node_id)

    return mcp
