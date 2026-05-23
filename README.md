# Pairflow

**An MCP server for AI-assisted Node-RED engineering.**

Pairflow is a Model Context Protocol server that lets an AI coding agent (Claude Code, Cursor, or any MCP-compatible client) work fluently on a Node-RED instance — editing flows, validating function-node code, triggering inject nodes, streaming debug output, and verifying behavior through MQTT — without forcing the human to approve every individual shell command.

The name reads two ways: **pair**-programming between a human and an AI, and **flow** as in Node-RED flow. The goal is to make working *with* an AI on Node-RED feel like collaborating with a competent pair partner rather than supervising a tool that needs permission for every move.

> **Status:** Concept / design phase. Architecture is sketched; implementation has not started. See [STORY.md](STORY.md) for why this exists, [docs/architecture.md](docs/architecture.md) for the design, [docs/prior-art.md](docs/prior-art.md) for how Pairflow positions against existing solutions, and [docs/naming.md](docs/naming.md) for the naming rationale.

## Planned scope

Four tiers of MCP tools:

1. **Flow surgery** — atomic operations on `flows.json` (add node, wire, patch property, validate function-node JS, deploy/restart) with automatic backup and JSON-syntax safety.
2. **Verification** — trigger injects, tail the debug sidebar over WebSocket, collect MQTT messages on a topic for N seconds, publish MQTT messages, read filtered journal output.
3. **Workflow** — git diff / status / commit inside the Node-RED project directory.
4. **HA-Discovery helpers** — list and validate retained Home Assistant MQTT Discovery configs.

## License

MIT — see [LICENSE](LICENSE).
