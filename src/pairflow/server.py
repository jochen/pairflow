"""Pairflow MCP server.

Tier 1 (flow surgery — synchronous; touches the flows file on disk):
  Reads:  nr_list_tabs, nr_list_nodes, nr_get_node
  Writes: nr_add_node, nr_update_node, nr_delete_node, nr_wire, nr_unwire,
          nr_validate_function
  Run:    nr_run_function (sandboxed; node subprocess, no deploy)

Tier 2 (verification — most are async; talks to NR, MQTT, systemd):
  Deploy:   nr_deploy
  Trigger:  nr_inject
  Inspect:  nr_tail_debug, nr_journal
  Trace:    nr_trace_pipeline (inject + debug + mqtt in one atomic call)
  MQTT:     mqtt_sub_collect, mqtt_pub, mqtt_pub_and_observe

Tier 3 (git workflow inside the Node-RED project directory):
  git_status, git_diff, git_log, git_commit

Tier 4 (Home Assistant MQTT Discovery helpers):
  ha_discovery_list, ha_discovery_validate
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from . import (
    flows,
    function_runner,
    git_ops,
    ha_discovery,
    mqtt,
    nr_admin,
    service,
    tracing,
    validate,
)
from .config import Config
from .usage_log import UsageLogger


def build_server(config: Config) -> FastMCP:
    mcp = FastMCP(name="pairflow")
    flows_file = config.node_red.flows_file

    # Per-call usage logger; no-op when telemetry.usage_log is false.
    # `tool` wraps every MCP tool with the logger so new tools auto-inherit
    # the instrumentation — see CONTRIBUTING.md and src/pairflow/usage_log.py.
    usage = UsageLogger.from_config(config.telemetry)

    def tool(fn):
        return mcp.tool(usage.wrap(fn))

    # ============================================================ #
    # Tier 1 — flow surgery                                          #
    # ============================================================ #

    # ---- read ----------------------------------------------------------- #

    @tool
    def nr_list_tabs(include_info: bool = False) -> list[dict[str, Any]]:
        """List all tabs (workspaces) in the Node-RED flows file.

        `include_info=True` adds each tab's markdown info notes; off by
        default because those notes can be long.
        """
        return flows.list_tabs(flows_file, include_info=include_info)

    @tool
    def nr_list_nodes(
        tab_id: str | None = None,
        node_type: str | None = None,
        name_contains: str | None = None,
        summary: bool | None = None,
    ) -> dict[str, Any]:
        """List nodes; returns either a summary or a filtered list.

        With no filter set, the default is a summary (counts per tab and
        per node type) — calling it unfiltered on a 2000+ node flow would
        otherwise dump a huge response. Pass any of `tab_id`, `node_type`,
        `name_contains` to get the per-node list (compact: id, type, name,
        tab, x, y, disabled). Use `nr_get_node` for the full config of a
        single node.

        `summary=False` forces the full list even without filters;
        `summary=True` forces a summary even with filters.
        """
        return flows.list_nodes(
            flows_file,
            tab_id=tab_id,
            node_type=node_type,
            name_contains=name_contains,
            summary=summary,
        )

    @tool
    def nr_get_node(
        node_id: str,
        code: str = "signatures",
        include_sources: bool = False,
    ) -> dict[str, Any] | None:
        """Return a node by id, or null if not found.

        For function nodes, `code` controls how the JS body is rendered:
          * `"signatures"` (default) — replaces `func` with `func_summary`
            (line/char count, first lines, declared helpers, node.on events).
          * `"full"` — full body, useful when you need to edit it.
          * `"omit"` — drop the body entirely.

        `include_sources=True` adds a `sources` list to the result — one
        entry per upstream connection into this node (the reverse of `wires`).
        Each entry: `{source_id, source_type, source_name, source_tab,
        port_index}`. Default off; only pay for the full-flow walk when needed.
        """
        return flows.get_node(flows_file, node_id, code=code, include_sources=include_sources)

    @tool
    def nr_list_dangling(
        tab_id: str | None = None,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Find dangling link nodes — link-ins with no source or dead peers,
        link-outs with empty or all-missing peer lists.

        Useful during refactor work to answer "are there orphan link-ins (no
        source) or orphan link-outs (no target) on this tab?" without running
        the full doctor pipeline.

        `tab_id`: restrict the scan to one tab; default scans all tabs.

        `types`: which node kinds to check; default is both
        ``["link in", "link out"]``. Pass a single-element list to limit to
        one kind.

        **Dangling rules:**

        * ``link out`` — dangling if ``.links`` is absent (``no_links_field``),
          empty (``link_out_empty_links``), or every listed peer id is missing
          from the flow (``all_peers_missing``).
        * ``link in`` — dangling if every id in its own ``.links`` is missing
          (``all_peers_missing``), *or* if no ``link out`` anywhere references
          this node's id (``link_in_no_source``).  The first failing rule wins.

        Response shape::

            {
                "tab_id": str | null,
                "total_checked": int,
                "dangling_count": int,
                "dangling": [
                    {
                        "node_id": str,
                        "type": "link in" | "link out",
                        "name": str,
                        "tab": str | null,
                        "reason": "no_links_field" | "link_out_empty_links"
                                  | "all_peers_missing" | "link_in_no_source",
                        "broken_peers": [str, ...],
                    },
                    ...
                ],
            }

        Response size is bounded: only link-node entries are included (typically
        a small fraction of the total flow), and ``broken_peers`` lists only the
        missing ids, not full node objects.
        """
        return flows.list_dangling(flows_file, tab_id=tab_id, types=types)

    # ---- write ---------------------------------------------------------- #

    @tool
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

    @tool
    def nr_update_node(
        node_id: str,
        patch: dict[str, Any],
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Apply a shallow patch to a node. id/type/z are not patchable.

        ``verbose=True`` (default) returns the full post-patch node body.
        ``verbose=False`` returns only ``{ok, node_id, applied_keys}`` — useful
        in auto-refactor loops where the full body is not needed and token cost
        matters.

        When ``active`` is patched on a ``debug``-type node, the verbose
        response includes a ``hint`` key reminding you to call ``nr_deploy``
        for the change to take effect.
        """
        return flows.update_node(flows_file, node_id, patch, verbose=verbose)

    @tool
    def nr_delete_node(node_id: str, missing_ok: bool = False) -> dict[str, Any]:
        """Delete a node and clean up wire/link references in other nodes.

        With `missing_ok=True`, a non-existent id returns
        `{deleted: null, references_removed: 0, found: false}` instead of
        raising — useful for cleanup loops over stale id lists.
        """
        return flows.delete_node(flows_file, node_id, missing_ok=missing_ok)

    @tool
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

    @tool
    def nr_unwire(src_id: str, src_port: int, dst_id: str) -> dict[str, Any]:
        """Disconnect `src_id` from `dst_id`. Symmetric to `nr_wire`.

        Link pairs are removed from both nodes' `.links`; wire pairs from
        `src.wires[src_port]`. `src_port` is ignored for link pairs.
        """
        return flows.unwire(flows_file, src_id=src_id, src_port=src_port, dst_id=dst_id)

    @tool
    def nr_validate_function(code: str) -> dict[str, Any]:
        """Syntax-check a function-node body without writing it."""
        result = validate.validate_function(code)
        return {"ok": result.ok, "error": result.error}

    @tool
    async def nr_run_function(
        node_id: str,
        msg: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Execute a function-node's body sandboxed — no Node-RED restart.

        Spawns a `node` subprocess that wraps the body in the same shape NR
        does (async function with `msg, context, flow, global, node, env`),
        mocks the NR runtime surface in-memory, runs for at most `timeout`
        seconds, then returns `{outputs, errors, warns, logs, duration_ms}`.

        `outputs` includes both explicit `return msg;` values and anything
        passed to `node.send(...)` — including from `setTimeout`/`Promise`
        callbacks that fire within the timeout window. Async failures inside
        those callbacks land in `errors` (the original motivation: silent
        `Object.keys(undefined)` inside a `setTimeout`).

        Caveats — v1 limitations:
          * `context`/`flow`/`global` are fresh in-memory stores per call,
            not the real NR persistence.
          * `require()` of function-external modules is not wired up.
          * `node.status()`/`node.done()` are no-ops.
        """
        return await function_runner.run_function(
            flows_file,
            node_id=node_id,
            msg=msg,
            timeout=timeout,
        )

    # ============================================================ #
    # Tier 2 — verification                                          #
    # ============================================================ #

    @tool
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

    @tool
    def nr_inject(node_id: str) -> dict[str, Any]:
        """Trigger an inject node via the Admin API.

        Useful right after `nr_deploy` to fire any once-on-boot inject that
        the AI just added — without waiting for the next scheduled tick.
        """
        return nr_admin.inject(config.node_red.admin_url, node_id)

    @tool
    async def nr_tail_debug(
        seconds: float = 5.0,
        filter_substr: str | None = None,
        max_messages: int = 500,
        max_msg_chars: int = 2000,
    ) -> list[dict[str, Any]]:
        """Stream the Node-RED debug sidebar over WebSocket for `seconds`.

        Returns a list of debug records, one per `node.warn()` / msg.payload
        trace that appeared in the window. `filter_substr` narrows to messages
        whose rendered text contains the substring (case-insensitive).

        `max_msg_chars` caps the rendered `msg` field per record (default
        2000); truncated records carry `msg_truncated: true` and the
        original `msg_full_chars` so you can refetch with a higher cap.
        Pass 0 to disable truncation.
        """
        return await nr_admin.tail_debug(
            config.node_red.admin_url,
            seconds=seconds,
            filter_substr=filter_substr,
            max_messages=max_messages,
            max_msg_chars=max_msg_chars,
        )

    @tool
    def nr_journal(
        lines: int = 100,
        filter_regex: str | None = None,
        max_bytes: int = 8000,
    ) -> dict[str, Any]:
        """Read recent systemd journal entries for the Node-RED service.

        `filter_regex` is a Python regular expression applied per line.
        The result reports both lines_read (total) and lines_matched
        (after filter) so an over-narrow filter is obvious.

        `max_bytes` (default 8000) caps the total bytes of returned `lines`
        content. When exceeded, the oldest matched lines are dropped and
        `output_truncated: true` + `output_full_bytes` are set. Pass `0`
        to disable.
        """
        return service.journal(
            config.node_red.service,
            lines=lines,
            filter_regex=filter_regex,
            max_bytes=max_bytes,
        )

    @tool
    async def mqtt_sub_collect(
        topic: str,
        seconds: float = 5.0,
        max_messages: int = 100,
        broker: str = "default",
        max_payload_chars: int = 2000,
    ) -> list[dict[str, Any]]:
        """Subscribe to `topic`, collect messages, disconnect.

        Returns when `max_messages` are received or `seconds` elapse,
        whichever comes first. `broker` selects a named broker from the
        Pairflow config; "default" uses [mqtt.default].

        `max_payload_chars` caps decoded payloads per message (default
        2000). Truncated records carry `payload_truncated: true` and the
        original `payload_full_chars`. Pass 0 to disable truncation.
        """
        return await mqtt.sub_collect(
            config.broker(broker),
            topic=topic,
            seconds=seconds,
            max_messages=max_messages,
            max_payload_chars=max_payload_chars,
        )

    @tool
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

    @tool
    async def nr_trace_pipeline(
        trigger_inject: str,
        seconds: float = 5.0,
        expect_topic: str | None = None,
        broker: str = "default",
        filter_substr: str | None = None,
        max_debug: int = 100,
        max_mqtt: int = 100,
        max_msg_chars: int = 2000,
        max_payload_chars: int = 2000,
    ) -> dict[str, Any]:
        """Trigger an inject, watch debug + MQTT in one atomic call.

        Opens the NR debug WebSocket and (if `expect_topic` is given) an
        MQTT subscription BEFORE firing the inject — so reactions cannot
        race the subscription, which is the failure mode of running
        `nr_tail_debug` + `mqtt_sub_collect` + `nr_inject` as parallel tools.

        Returns `{fired_nodes, output_msgs, path_complete, duration_ms,
        inject}`. `fired_nodes` is the debug-sidebar event list (same shape
        as `nr_tail_debug`); `output_msgs` are messages on `expect_topic`
        (same shape as `mqtt_sub_collect`). `path_complete` is true/false
        when `expect_topic` is given (did any message arrive?), null when
        no topic was specified.

        Use this to diagnose "I poke node X, does anything reach topic Y?"
        in one round trip instead of three.
        """
        broker_cfg = config.broker(broker) if expect_topic else None
        return await tracing.trace_pipeline(
            admin_url=config.node_red.admin_url,
            trigger_inject=trigger_inject,
            seconds=seconds,
            expect_topic=expect_topic,
            broker=broker_cfg,
            filter_substr=filter_substr,
            max_debug=max_debug,
            max_mqtt=max_mqtt,
            max_msg_chars=max_msg_chars,
            max_payload_chars=max_payload_chars,
        )

    @tool
    async def mqtt_pub_and_observe(
        pub_topic: str,
        pub_payload: str,
        observe_topics: list[str],
        seconds: float = 5.0,
        max_messages: int = 100,
        pub_retain: bool = False,
        pub_qos: int = 0,
        broker: str = "default",
        max_payload_chars: int = 2000,
    ) -> dict[str, Any]:
        """Subscribe to `observe_topics`, then publish, then collect on one
        connection.

        The subscriptions are confirmed (SUBACK) before the publish is sent,
        so a response triggered by the publish cannot race the subscription —
        which is the failure mode you get from calling `mqtt_sub_collect` and
        `mqtt_pub` in parallel. Use this when diagnosing an MQTT pipeline
        where the response can arrive in milliseconds (HA→bridge→device,
        Node-RED function chain, etc.).

        Returns `{published, observed, broker}`. `published` carries
        topic/bytes/qos/retain; `observed` is the list of received messages
        (same shape as `mqtt_sub_collect`).
        """
        return await mqtt.pub_and_observe(
            config.broker(broker),
            pub_topic=pub_topic,
            pub_payload=pub_payload,
            observe_topics=observe_topics,
            seconds=seconds,
            max_messages=max_messages,
            pub_retain=pub_retain,
            pub_qos=pub_qos,
            max_payload_chars=max_payload_chars,
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

    @tool
    def git_status() -> dict[str, Any]:
        """Parsed status of the Node-RED project's git repo.

        Returns branch, ahead/behind counts, and a list of changed paths
        with their two-character porcelain status code (e.g. " M" for
        unstaged-modified, "M " for staged-modified, "??" for untracked).
        """
        return git_ops.status(_project_dir())

    @tool
    def git_diff(
        path: str | None = None,
        staged: bool = False,
        stat: bool = True,
        max_bytes: int = 16_000,
    ) -> dict[str, Any]:
        """Diff of the working tree (or the index, if `staged=True`).

        `stat=True` (default) returns only per-file numstat (added/removed
        line counts) — small even for huge flows.json edits. `stat=False`
        returns the full unified diff, capped at `max_bytes` characters
        (0 disables) with `diff_truncated` / `diff_full_bytes` set on
        oversized responses.

        `path`: restrict to a single file.
        """
        return git_ops.diff(
            _project_dir(),
            path=path,
            staged=staged,
            stat=stat,
            max_bytes=max_bytes,
        )

    @tool
    def git_log(count: int = 10) -> dict[str, Any]:
        """Recent commits in the project repo as structured records."""
        return {"commits": git_ops.log(_project_dir(), count=count)}

    @tool
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

    @tool
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

    @tool
    async def ha_discovery_validate(
        topic: str,
        seconds: float = 2.0,
        broker: str = "default",
        include_config: bool = False,
    ) -> dict[str, Any]:
        """Validate the retained Discovery config at `topic`.

        Reads the retained payload and runs a minimal shape check:
        valid JSON object, `unique_id` present, topic fields are strings,
        `device` (if present) is an object, and (for known components)
        at least one of the required topic-field groups is set.

        Returns `{valid, errors, warnings, component, entity_id}`. The
        full parsed config is only included when `include_config=True` —
        climate / light configs are large and the validation result is
        usually enough on its own.
        """
        return await ha_discovery.validate_discovery(
            config.broker(broker),
            topic=topic,
            seconds=seconds,
            include_config=include_config,
        )

    return mcp
