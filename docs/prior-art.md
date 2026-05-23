# Prior Art

Pairflow is not the first attempt to bring AI assistance to Node-RED. This document describes the categories of existing solutions, what they do well, and where the remaining gaps are. Specific projects are not named — the goal here is to characterize the landscape, not to grade individual implementations, all of which are valuable contributions to the ecosystem.

## Category 1: MCP servers for Node-RED

Several community MCP servers expose Node-RED to AI agents. The more mature ones provide on the order of twenty tools, focused on:

- Flow CRUD: retrieve flows, retrieve a single flow by id, create a new flow tab, update a flow, delete a flow, list tabs.
- Deployment state: query and modify the deployed state of flows.
- Node discovery: list node types available in the runtime, find nodes by type or by name property, search nodes by configuration.
- Operational primitives: trigger inject nodes, read runtime settings, fetch diagnostics.
- Module management (in some): list installed modules, install or remove modules, search the palette.

These servers are well-built and address the core CRUD surface effectively. They are the natural starting point for any AI agent that needs to inspect or modify a Node-RED instance.

### Where the gaps are

Across this category, several capabilities are consistently missing — not as a failing of any particular implementation, but as a pattern in what the category has chosen to scope.

**No structured access to the debug sidebar.** When a flow misbehaves, the human's first move is to look at the debug sidebar in the Node-RED editor. An AI agent operating outside the editor has no equivalent. Reading the systemd journal is a partial substitute — it catches `[error]`-level events — but it does not surface the `node.warn()` and `msg.payload` traces that fill the debug sidebar and that are the actual diagnostic signal during development.

**No MQTT integration.** Most non-trivial Node-RED setups publish or consume MQTT, often as their primary external interface. Verifying that a flow change "actually published the right thing on the right topic" therefore requires shelling out to `mosquitto_sub` or `mosquitto_pub`, which means leaving the structured-tool surface and incurring permission prompts per call. A tool that wraps subscribe-and-collect into a single MCP call closes this loop.

**No pre-write validation of function-node JavaScript.** A function node's body is JavaScript embedded in a JSON string field. When an AI agent writes one, escaping bugs and minor syntax errors are easy to introduce and only surface at runtime — meaning a full deploy/restart cycle has to complete before the error becomes visible. A `node --check`-style validation step before the write would catch this class of bug in milliseconds.

**Coarse-grained writes.** Existing MCP-NR tools typically expose `update_flow` (replace an entire flow's contents), but not finer-grained operations such as "add this node to that tab", "wire output 0 of node A to node B", or "patch this single property". The result is that the AI either has to construct an entire updated flow object — which is verbose and error-prone for a flow with hundreds of nodes — or do the surgery in scratch space and submit the result. Atomic primitives for the common surgical operations would be both safer and shorter.

## Category 2: In-editor copilot plugins

A separate line of projects brings chat-based AI assistance into the Node-RED editor itself, typically as a right-hand sidebar that the human can converse with. These tools shine in interactive, human-in-the-loop development: the human stays in the editor, asks for help, and accepts or rejects suggestions visually.

This category is complementary to, not overlapping with, what Pairflow is trying to do. An in-editor copilot serves a human at the keyboard. Pairflow serves an AI agent operating from outside — a terminal-based coding agent, a CI bot, an autonomous routine — that needs to *be* the agent rather than to advise one.

## Category 3: Generic AI tooling reused on Node-RED

A third pattern is to use general-purpose tooling (a coding agent with shell access, an editor, a browser-driving harness) directly on Node-RED, without any specialized layer. This is what gave rise to Pairflow: it works, but the friction of operating through generic primitives on a system that has good structured APIs (Admin API, MQTT, git) becomes the dominant cost as soon as the workflow is repeated more than a handful of times.

## Where Pairflow positions

Pairflow occupies a deliberately narrow band:

- It is in **Category 1** (MCP server for external AI agents), not Category 2 (in-editor copilot).
- Within Category 1, it extends the standard CRUD surface with the missing **verification** primitives — debug streaming, MQTT publish and collect, pre-write JS validation, filtered journal access.
- It also adds **finer-grained writes** — add-node, wire, patch — so that the AI does not have to think in whole-flow replacements.

The aim is for Pairflow to be the layer that closes the verification loop. Existing Category 1 servers are excellent for inspection and bulk-edit; Pairflow's contribution is the part that comes after the edit — the part where the AI has to *find out whether it worked*.

## Relationship to existing Category 1 servers

A reasonable question is whether Pairflow should be a fork or extension of an existing server rather than a fresh build. The decision to start fresh rests on three considerations:

1. **Language consistency** with the host environment (Python throughout the surrounding stack vs. TypeScript in the existing servers).
2. **Edit-path divergence**: existing servers tend to go through the Admin API for writes; Pairflow patches the file directly so that `git diff` stays the human's source of truth. Refactoring an existing server to swap its write path is not obviously cheaper than a fresh design.
3. **Tier-2 verification is a substantial addition** in its own right — debug WebSocket handling, MQTT lifecycle, journal access — and cleanly separating it lets the implementation evolve at its own pace.

None of these arguments are decisive against integration. If a future merge with another server becomes worthwhile, the architectural decisions in Pairflow are written down explicitly enough (see [architecture.md](architecture.md)) that the merge cost is bounded.
