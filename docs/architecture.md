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

## Response-shape principle: cheap by default

The AI client pays for every byte of tool output in context tokens. A `flows.json` with 2000+ nodes, a Discovery scan returning hundreds of retained configs, or a `git diff` on a multi-MB JSON file would all overwhelm that budget if returned verbatim.

Pairflow's reads therefore default to summaries; the full payload is opt-in:

- `nr_list_nodes` returns counts per tab and per type unless a filter (`tab_id`, `node_type`, `name_contains`) is set, or `summary=False` is passed.
- `nr_get_node` on a function node returns a `func_summary` (line count, head, declared helpers) instead of the full body, unless `code="full"` is passed.
- `nr_list_tabs` omits each tab's `info` notes unless `include_info=True`.
- `git_diff` returns numstat (per-file added/removed counts) unless `stat=False`; full diffs above `max_bytes` are truncated with a `diff_truncated` flag.
- `nr_tail_debug` and `mqtt_sub_collect` truncate per-record payloads at `max_msg_chars` / `max_payload_chars` and tag truncated records with the original length so a follow-up call can refetch with a higher cap.
- `nr_journal` caps the total bytes of returned lines at `max_bytes` (default 8000), dropping the oldest matched lines first; truncated responses carry `output_truncated: true` and `output_full_bytes`.
- `ha_discovery_validate` returns only the validation verdict unless `include_config=True`.

This is a load-bearing convention, not a style preference — see CONTRIBUTING.md for the rule new tools must follow.

### Empirical instrumentation: per-call usage log

The response-shape design above is built on estimates. To validate it on real workloads, Pairflow ships an opt-in per-call usage logger (`src/pairflow/usage_log.py`). When `[telemetry] usage_log = true` is set in the config, every MCP tool call appends one JSON line to `~/.local/state/pairflow/usage.jsonl` (overridable):

```json
{"ts": "...", "tool": "nr_list_nodes", "args": {"tab_id": "abc", "summary": null},
 "duration_ms": 4, "response_bytes": 873, "response_shape": {"type": "dict", "keys": 5},
 "truncations": {}, "error": null}
```

The logger is wired in centrally via the `tool` decorator inside `build_server` — every tool inherits instrumentation automatically with no per-tool bookkeeping. Arguments larger than 200 chars (or dicts >800 JSON chars) are scrubbed to `{_size, _type, _head}` so MQTT payloads, JS bodies, and other blobs never leak verbatim. Logger failures are swallowed — instrumentation never breaks the tool call.

Use the log to answer questions like *"which tool dominates the token budget?"*, *"which opt-in flags actually get used?"*, *"how often does truncation fire — is the cap right?"*. The default is off; turn it on for a sprint, harvest data, decide.

## Tool tiers

Tools are grouped by purpose. The grouping is descriptive, not technical — all tools live in one MCP server.

### Tier 1 — Flow surgery + read/query

Atomic, structured operations on the flows file. All write operations include backup and validation.

- `nr_list_tabs(include_info=)` → list of tabs with ids and labels
- `nr_list_nodes(tab_id=, node_type=, name_contains=, summary=)` → summary by default, full list when filtered
- `nr_get_node(id, code=, include_sources=)` → node JSON; `include_sources=True` adds reverse-wires lookup
- `nr_search_flows(query, fields=, regex=, …)` → global string/regex search across every string value in the document
- `nr_list_dangling(tab_id=, types=)` → enumerate orphan link-in / link-out nodes with reason
- `nr_add_node(tab, type, props, x, y)` → returns new node id
- `nr_update_node(id, patch, verbose=)` → partial update; `verbose=False` returns compact summary
- `nr_delete_node(id, missing_ok=)` → with cascading cleanup; `missing_ok=True` for idempotent cleanup loops
- `nr_wire(src_id, src_port, dst_id)`
- `nr_unwire(src_id, src_port, dst_id)`
- `nr_validate_function(code)` → syntax-only JS validation
- `nr_run_function(node_id, msg, timeout=)` → sandboxed execution of a function body via node subprocess; no deploy needed

### Tier 2 — Verification

The primitives that close the "did it work?" loop.

- `nr_deploy(wait_timeout=)` — systemctl restart + Admin-API readiness poll
- `nr_inject(node_id)` — trigger an inject node via Admin API
- `nr_tail_debug(seconds, filter_substr=, max_msg_chars=)` — open a WebSocket to the NR debug stream, collect entries for the given window, return as structured data
- `nr_journal(lines=, filter_regex=, max_bytes=)` — read recent systemd journal entries; oldest lines dropped if output exceeds `max_bytes`
- `nr_trace_pipeline(trigger_inject, expect_topic=, seconds=, …)` — one atomic call: open WS + MQTT *then* fire the inject so reactions can't race the subscriptions
- `mqtt_sub_collect(topic, seconds, max_messages=, broker=, max_payload_chars=)` — subscribe, collect, return
- `mqtt_pub(topic, payload, retain=, broker=)`
- `mqtt_pub_and_observe(pub_topic, pub_payload, observe_topics, …)` — race-free atomic subscribe-then-publish-then-collect on one connection

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
