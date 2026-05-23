# Pairflow

**An MCP server for AI-assisted Node-RED engineering.**

Pairflow is a Model Context Protocol server that lets an AI coding agent (Claude Code, Cursor, or any MCP-compatible client) work fluently on a Node-RED instance — editing flows, validating function-node code, triggering inject nodes, streaming debug output, and verifying behavior through MQTT — without forcing the human to approve every individual shell command.

The name reads two ways: **pair**-programming between a human and an AI, and **flow** as in Node-RED flow. The goal is to make working *with* an AI on Node-RED feel like collaborating with a competent pair partner rather than supervising a tool that needs permission for every move.

> **Status:** Tier 1 implemented and tested. Nine MCP tools live: three read-only (`nr_list_tabs`, `nr_list_nodes`, `nr_get_node`) and six writes (`nr_add_node`, `nr_update_node`, `nr_delete_node`, `nr_wire`, `nr_unwire`, `nr_validate_function`) with atomic writes, automatic timestamped backups, optimistic mtime locking, and pre-write JS syntax validation for function nodes. Tier 2 (verification: deploy, debug-stream, MQTT, journal) is next. See [STORY.md](STORY.md) for the why, [docs/architecture.md](docs/architecture.md) for the design, [docs/prior-art.md](docs/prior-art.md) for positioning, and [docs/naming.md](docs/naming.md) for the name.

## Planned scope

Four tiers of MCP tools:

1. **Flow surgery** — atomic operations on `flows.json` (add node, wire, patch property, validate function-node JS, deploy/restart) with automatic backup and JSON-syntax safety.
2. **Verification** — trigger injects, tail the debug sidebar over WebSocket, collect MQTT messages on a topic for N seconds, publish MQTT messages, read filtered journal output.
3. **Workflow** — git diff / status / commit inside the Node-RED project directory.
4. **HA-Discovery helpers** — list and validate retained Home Assistant MQTT Discovery configs.

## Getting started

```bash
pip install -e .

# Configure: point Pairflow at your Node-RED flows file.
cp examples/config.example.toml ~/.config/pairflow/config.toml
$EDITOR ~/.config/pairflow/config.toml

# Sanity-check your flows file before connecting the MCP server.
pairflow-doctor --flows /path/to/flows.json

# Run as an MCP server (stdio). Register this command in your MCP client
# (Claude Code, Cursor, etc.).
pairflow
```

## Contributing

Pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for how to run the test suite, what CI checks PRs against, and the design conventions to follow.

## License

MIT — see [LICENSE](LICENSE).
