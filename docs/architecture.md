# Architecture

This document describes the design of Pairflow at the level a contributor needs to start working on it. For *why* these choices were made, see [STORY.md](../STORY.md).

## Overview

Pairflow is a single Python process that speaks MCP to one or more clients (an AI coding agent, a CI bot, an autonomous runner). It exposes a set of structured tools that operate on a Node-RED instance and its surrounding artifacts (the flows file, the project's git repo, the MQTT broker, the systemd journal).

```
┌──────────────┐   MCP    ┌─────────────────────┐
│  AI client   │ ────────▶│      Pairflow       │
│ (Claude etc.)│          │   (Python+FastMCP)  │
└──────────────┘          └──────┬──────┬───────┘
                                 │      │
                ┌────────────────┼──────┼────────────────────────┐
                │                │      │                        │
                ▼                ▼      ▼                        ▼
        ┌──────────────┐  ┌──────────┐ ┌──────────┐    ┌─────────────────┐
        │  flows.json  │  │ NR Admin │ │   MQTT   │    │ systemd / git / │
        │  (direct)    │  │   API    │ │  broker  │    │   filesystem    │
        └──────────────┘  └──────────┘ └──────────┘    └─────────────────┘
```

## Implementation language: Python + FastMCP

Pairflow is implemented in Python using [FastMCP](https://github.com/jlowin/fastmcp). Rationale: the surrounding ecosystem of the original host already runs Python for scripts, daemons, and ad-hoc work, and FastMCP is currently the most ergonomic way to author an MCP server. The trade-off — less alignment with the existing TypeScript Node-RED-MCP ecosystem — is accepted in exchange for staying within one language for the whole stack.

## Edit strategy: direct file patching, not Admin API

All structural changes to flows are made by reading `flows.json`, mutating the in-memory representation, and writing the file back atomically. The Node-RED service is then restarted (or the user is prompted to reload) so the change becomes live.

The Admin API is consumed for read-only and trigger-style operations where it adds value (inject triggering, runtime diagnostics, settings access), but is deliberately *not* used as the path for writes.

The reason is epistemic, not technical: when the file on disk is the source of truth, `git diff` is the AI's primary feedback loop with the human. A workflow that pushes through the Admin API and then has to round-trip back to disk to update git introduces a step where formatting, ordering, and credential references can drift silently. Direct patching keeps the diff honest.

The cost is a service restart per write cycle. On a development host this is acceptable; for production hosts the same Pairflow process can be pointed at a staging instance instead.

### Safety properties of the write path

- **Atomic writes.** Pairflow writes to a temporary file in the same directory and renames into place; readers never see a partial file.
- **Pre-write validation.** JSON schema check on the whole document. JavaScript syntax check (`node --check`) on any function-node body that is being written.
- **Auto-backup.** Before any write, Pairflow keeps the previous file under a timestamped name in the same directory. Configurable retention.
- **Format preservation.** The same JSON indentation (4 spaces in the reference setup) is preserved across writes.

## Tool tiers

Tools are grouped by purpose. The grouping is descriptive, not technical — all tools live in one MCP server.

### Tier 1 — Flow surgery

Atomic, structured operations on the flows file. All write operations include backup and validation.

- `nr_list_tabs()` → list of tabs with ids and labels
- `nr_list_nodes(tab=, type=)` → filtered list of nodes
- `nr_get_node(id)` → full node JSON
- `nr_add_node(tab, type, props, x, y)` → returns new node id
- `nr_update_node(id, patch)` → partial update (jsonpatch-style)
- `nr_delete_node(id)` → with cascading cleanup of dangling link references
- `nr_wire(src_id, src_port, dst_id)`
- `nr_unwire(src_id, src_port, dst_id)`
- `nr_validate_function(code)` → syntax-only JS validation
- `nr_deploy(mode="restart" | "reload")`

### Tier 2 — Verification

The primitives that close the "did it work?" loop.

- `nr_inject(node_id)` — trigger an inject node via Admin API
- `nr_tail_debug(seconds, filter=)` — open a WebSocket to the NR debug stream, collect entries for the given window, return as structured data
- `nr_journal(lines, filter=)` — read recent systemd journal entries, optionally filtered for errors
- `mqtt_sub_collect(topic, seconds, max_messages=, broker=)` — subscribe, collect, return
- `mqtt_pub(topic, payload, retain=, broker=)`

### Tier 3 — Workflow

Git operations inside the Node-RED project directory.

- `git_diff(path=)`
- `git_status()`
- `git_commit(message, paths=)`
- `git_log(count=)`

### Tier 4 — HA-Discovery helpers

Convenience tooling for Home Assistant MQTT Discovery, which is a common destination for Node-RED-driven setups.

- `ha_discovery_list(component=)` — list all retained `homeassistant/<component>/+/config` topics
- `ha_discovery_validate(unique_id)` — validate a retained config against the relevant HA schema

## Configuration

Pairflow reads its configuration from a single TOML file (default `~/.config/pairflow/config.toml`, overridable via `PAIRFLOW_CONFIG`).

Required keys:

```toml
[node_red]
flows_file = "/path/to/flows.json"
admin_url  = "http://localhost:1880"
service    = "nodered"            # systemd unit name
project_dir = "/path/to/flows-project"

[mqtt.default]
host = "localhost"
port = 1883

# Optional additional brokers, named:
[mqtt.remote_main]
host = "broker.example.tld"
port = 1883
username = "..."
password_env = "BROKER_PW"        # never inline secrets
```

All MQTT tools accept an optional `broker=` argument; if omitted, the `default` broker is used.

## Out of scope

- Running inside Node-RED as a plugin. Pairflow is an external process.
- Driving the Node-RED browser UI (sidebar chat, palette manipulation through the editor). The companion category of in-editor copilots already covers that ground.
- A general-purpose Node-RED-management tool. Pairflow's shape is opinionated toward AI agents — for instance, function-node JS validation before write is essential for AI but redundant for a human in the editor.

## Open design questions

These will be resolved as implementation progresses; recording them here so they aren't forgotten.

- **Concurrency with the editor.** If the human is actively editing in the Node-RED UI when Pairflow writes the file, the next UI-side deploy will overwrite Pairflow's change. Mitigation options: detect via mtime and refuse to write; or write through Admin API as a fallback when the editor is connected; or simply document the constraint and rely on the human not to interleave.
- **Credentials.** `flows_cred.json` lives next to `flows.json` and is encrypted. Pairflow should never need to read it, but should be aware of it when copying or backing up. To verify and document.
- **Multi-instance support.** Whether one Pairflow process should be able to point at multiple Node-RED instances, or whether multiple Pairflow processes should run in parallel. Probably the latter — simpler, fewer footguns.
