"""Pairflow MCP server.

Tier 1 (flow surgery — synchronous; touches the flows file on disk):
  Reads:  nr_list_tabs, nr_list_nodes, nr_get_node
  Writes: nr_add_node, nr_update_node, nr_delete_node, nr_wire, nr_unwire,
          nr_validate_function

Tier 2 (verification — most are async; talks to NR, MQTT, systemd):
  Deploy:   nr_deploy
  Trigger:  nr_inject
  Inspect:  nr_tail_debug, nr_journal
  MQTT:     mqtt_sub_collect, mqtt_pub

Tier 3 (git workflow inside the Node-RED project directory):
  git_status, git_diff, git_log, git_commit

Tier 4 (Home Assistant MQTT Discovery helpers):
  ha_discovery_list, ha_discovery_validate
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from . import flows, git_ops, ha_discovery, mqtt, nr_admin, service, validate
from .config import Config


def build_server(config: Config) -> FastMCP:
    mcp = FastMCP(name="pairflow")
    flows_file = config.node_red.flows_file

    # ============================================================ #
    # Tier 1 — flow surgery                                          #
    # ============================================================ #

    # ---- read ----------------------------------------------------------- #

    @mcp.tool
    def nr_list_tabs() -> list[dict[str, Any]]:
        """List all tabs (workspaces) in the Node-RED flows file."""
        return flows.list_tabs(flows_file)

    @mcp.tool
    def nr_list_nodes(
        tab_id: str | None = None,
        node_type: str | None = None,
        name_contains: str | None = None,
    ) -> list[dict[str, Any]]:
        """List nodes with optional filters (tab_id, node_type, name_contains).

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

    # ---- write ---------------------------------------------------------- #

    @mcp.tool
    def nr_add_node(
        tab_id: str,
        node_type: str,
        props: dict[str, Any] | None = None,
        x: int | None = None,
        y: int | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        """Add a new node to a tab. For function nodes, validates JS first."""
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
        """Apply a shallow patch to a node. id/type/z are not patchable."""
        return flows.update_node(flows_file, node_id, patch)

    @mcp.tool
    def nr_delete_node(node_id: str) -> dict[str, Any]:
        """Delete a node and clean up wire/link references in other nodes."""
        return flows.delete_node(flows_file, node_id)

    @mcp.tool
    def nr_wire(src_id: str, src_port: int, dst_id: str) -> dict[str, Any]:
        """Connect `src_id` to `dst_id`. Idempotent.

        For `link out` → `link in` pairs, the `.links` array on *both* nodes
        is updated (NR's editor expects this; setting only the link-out side
        works at runtime but the editor shows "no input connected" at the
        link-in). `src_port` is ignored in that case.

        For all other node types, this writes the regular `src.wires[src_port]`
        connection. Mixing link and non-link nodes raises `ValueError`.

        Returns `{src, dst, added, kind}` where `kind` is `"link"` or `"wire"`.
        """
        return flows.wire(flows_file, src_id=src_id, src_port=src_port, dst_id=dst_id)

    @mcp.tool
    def nr_unwire(src_id: str, src_port: int, dst_id: str) -> dict[str, Any]:
        """Disconnect `src_id` from `dst_id`. Symmetric to `nr_wire`.

        Link pairs are removed from both nodes' `.links`; wire pairs from
        `src.wires[src_port]`. `src_port` is ignored for link pairs.
        """
        return flows.unwire(flows_file, src_id=src_id, src_port=src_port, dst_id=dst_id)

    @mcp.tool
    def nr_validate_function(code: str) -> dict[str, Any]:
        """Syntax-check a function-node body without writing it."""
        result = validate.validate_function(code)
        return {"ok": result.ok, "error": result.error}

    # ============================================================ #
    # Tier 2 — verification                                          #
    # ============================================================ #

    @mcp.tool
    def nr_deploy(wait_timeout: float = 30.0) -> dict[str, Any]:
        """Restart the Node-RED systemd service so disk-based flow changes go live.

        Returns when NR's Admin API responds again (or `wait_timeout` elapses,
        in which case `ready` is false in the result). Requires a passwordless
        sudoers entry for `systemctl restart <service>`; see the error message
        for the exact line if the call fails on permissions.
        """
        return service.restart(
            config.node_red.service,
            admin_url=config.node_red.admin_url,
            wait_timeout=wait_timeout,
        )

    @mcp.tool
    def nr_inject(node_id: str) -> dict[str, Any]:
        """Trigger an inject node via the Admin API.

        Useful right after `nr_deploy` to fire any once-on-boot inject that
        the AI just added — without waiting for the next scheduled tick.
        """
        return nr_admin.inject(config.node_red.admin_url, node_id)

    @mcp.tool
    async def nr_tail_debug(
        seconds: float = 5.0,
        filter_substr: str | None = None,
        max_messages: int = 500,
    ) -> list[dict[str, Any]]:
        """Stream the Node-RED debug sidebar over WebSocket for `seconds`.

        Returns a list of debug records, one per `node.warn()` / msg.payload
        trace that appeared in the window. `filter_substr` narrows to messages
        whose rendered text contains the substring (case-insensitive).
        """
        return await nr_admin.tail_debug(
            config.node_red.admin_url,
            seconds=seconds,
            filter_substr=filter_substr,
            max_messages=max_messages,
        )

    @mcp.tool
    def nr_journal(
        lines: int = 100,
        filter_regex: str | None = None,
    ) -> dict[str, Any]:
        """Read recent systemd journal entries for the Node-RED service.

        `filter_regex` is a Python regular expression applied per line.
        The result reports both lines_read (total) and lines_matched
        (after filter) so an over-narrow filter is obvious.
        """
        return service.journal(
            config.node_red.service,
            lines=lines,
            filter_regex=filter_regex,
        )

    @mcp.tool
    async def mqtt_sub_collect(
        topic: str,
        seconds: float = 5.0,
        max_messages: int = 100,
        broker: str = "default",
    ) -> list[dict[str, Any]]:
        """Subscribe to `topic`, collect messages, disconnect.

        Returns when `max_messages` are received or `seconds` elapse,
        whichever comes first. `broker` selects a named broker from the
        Pairflow config; "default" uses [mqtt.default].
        """
        return await mqtt.sub_collect(
            config.broker(broker),
            topic=topic,
            seconds=seconds,
            max_messages=max_messages,
        )

    @mcp.tool
    async def mqtt_pub(
        topic: str,
        payload: str,
        retain: bool = False,
        qos: int = 0,
        broker: str = "default",
    ) -> dict[str, Any]:
        """Publish one message and disconnect.

        Useful for test-publishing into a flow ("does this MQTT in trigger
        what I expect?") and for retained-config writes (publish an empty
        retained payload to delete an HA Discovery entry, for example).
        """
        return await mqtt.publish(
            config.broker(broker),
            topic=topic,
            payload=payload,
            retain=retain,
            qos=qos,
        )

    # ============================================================ #
    # Tier 3 — git workflow                                          #
    # ============================================================ #

    def _project_dir():
        pd = config.node_red.project_dir
        if pd is None:
            raise ValueError(
                "No project_dir configured. Add [node_red].project_dir = \"...\" "
                "to your pairflow config to enable git_* tools."
            )
        return pd

    @mcp.tool
    def git_status() -> dict[str, Any]:
        """Parsed status of the Node-RED project's git repo.

        Returns branch, ahead/behind counts, and a list of changed paths
        with their two-character porcelain status code (e.g. " M" for
        unstaged-modified, "M " for staged-modified, "??" for untracked).
        """
        return git_ops.status(_project_dir())

    @mcp.tool
    def git_diff(path: str | None = None, staged: bool = False) -> dict[str, Any]:
        """Diff of the working tree (or the index, if `staged=True`).

        `path`: restrict to a single file. Without a path, the full diff is
        returned, which can be large for a multi-megabyte flows.json.
        """
        return git_ops.diff(_project_dir(), path=path, staged=staged)

    @mcp.tool
    def git_log(count: int = 10) -> dict[str, Any]:
        """Recent commits in the project repo as structured records."""
        return {"commits": git_ops.log(_project_dir(), count=count)}

    @mcp.tool
    def git_commit(
        message: str,
        paths: list[str] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """Stage `paths` (if any) and commit them with `message`.

        If `paths` is omitted, commits whatever is already staged. An empty
        message is rejected. With `allow_empty=False` (default), a no-op
        commit raises rather than silently succeeding.
        """
        return git_ops.commit(
            _project_dir(),
            message=message,
            paths=paths,
            allow_empty=allow_empty,
        )

    # ============================================================ #
    # Tier 4 — Home Assistant MQTT Discovery helpers                 #
    # ============================================================ #

    @mcp.tool
    async def ha_discovery_list(
        component: str | None = None,
        seconds: float = 2.0,
        max_messages: int = 500,
        broker: str = "default",
    ) -> list[dict[str, Any]]:
        """List retained HA MQTT Discovery configs.

        Connects to `broker` (named broker from the Pairflow config),
        subscribes to `homeassistant/<component>/+/config` (and the
        longer `homeassistant/<component>/+/+/config` variant), waits
        `seconds` for the retained flood to arrive, then disconnects.

        Returns one entry per discovered entity with topic, component,
        unique_id, name, device_class, and device identifiers — enough
        to navigate; use ha_discovery_validate for a deeper look at a
        specific entry.
        """
        return await ha_discovery.list_discoveries(
            config.broker(broker),
            component=component,
            seconds=seconds,
            max_messages=max_messages,
        )

    @mcp.tool
    async def ha_discovery_validate(
        topic: str,
        seconds: float = 2.0,
        broker: str = "default",
    ) -> dict[str, Any]:
        """Validate the retained Discovery config at `topic`.

        Reads the retained payload and runs a minimal shape check:
        valid JSON object, `unique_id` present, topic fields are strings,
        `device` (if present) is an object, and (for known components)
        at least one of the required topic-field groups is set.

        Returns `{valid, errors, warnings, component, entity_id, config}`.
        `errors` block validity; `warnings` are informational (e.g.,
        unknown component, device without identifiers).
        """
        return await ha_discovery.validate_discovery(
            config.broker(broker),
            topic=topic,
            seconds=seconds,
        )

    return mcp
