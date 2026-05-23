# The Pairflow Story

This document captures *why* Pairflow exists, *what it is reacting to*, and *how the design and the name came to be*. It is intentionally long — the goal is that anyone discovering this repo later (including future contributors, and the original authors revisiting it a year from now) can reconstruct the reasoning without having to ask.

## The trigger

The project was born during a Node-RED session in May 2026. The task was straightforward: migrate two roller-shutter devices into Home Assistant via MQTT Discovery, while keeping the existing Alexa control path inside Node-RED untouched. One shutter was Zigbee, controlled through zigbee2mqtt; the other was a self-built WiFi controller speaking a small custom protocol on its own MQTT topics.

The work itself was well-scoped. The friction came from the tooling between the AI and Node-RED.

To accomplish what was, by any reasonable measure, a single coherent change, the following sequence of operations was required:

- Read the 1.3 MB `flows.json` file, identify the relevant tab, find the existing nodes and broker configuration.
- Write a Python script that loaded the JSON, mutated the relevant objects in place, validated the result, and wrote it back atomically. (Reason: incremental string-replace edits on a 1.3 MB file with hundreds of structurally similar entries are fragile.)
- Manually back up the file before writing.
- Restart the Node-RED systemd service so it would re-read the file.
- Wait for the service to come up — long enough that the once-only inject node had fired.
- Use `mosquitto_sub` to subscribe to the discovery topic and verify the retained config payload was correct.
- Tail the systemd journal to look for any function-node runtime errors.

Each of those steps was a separate Bash invocation, and on the host's permission settings every one prompted the human user to approve it. The human said yes a dozen times in a row, mechanically. It was the kind of friction that erodes trust in a workflow more than any single failure would.

When a bug slipped through — Python f-string escapes (`{{` and `}}`) left as literal braces in JavaScript function-node code, breaking it silently — the cost of catching it was another round of the same dance: edit, restart, wait, sub, journal. The bug itself was trivial. The verification loop was anything but.

This was the trigger. Not the bug. The loop.

## The recognition

The friction was not Node-RED's fault, and it was not the AI's fault. It was a missing layer. The AI was operating with general-purpose primitives (a shell, a file editor) on a system that has perfectly good structured APIs — the Node-RED Admin API, MQTT, the project's git repo. There was simply nothing in between that knew about both sides of the boundary.

Several existing community projects address parts of the problem. Some MCP servers for Node-RED expose flow CRUD operations and inject triggering, which is exactly what they should do. In-editor copilot plugins bring chat-based AI assistance into the Node-RED editor itself. Both categories are valuable, both are well-built, and neither was sufficient for the workflow described above.

What was missing — what would have collapsed that dozen-step verification loop into a single round trip — were the *verification primitives*:

- A way to read the Node-RED debug sidebar without opening a browser.
- A way to subscribe to MQTT, collect messages for a bounded window, and return them as structured data.
- A way to publish MQTT test messages.
- A way to validate a function-node's JavaScript syntax *before* writing it into `flows.json`.
- A way to read the recent systemd journal, filtered for errors related to the deploy that just happened.

Together, these would turn "did the change work?" from a multi-tool, multi-prompt shell session into a single structured query.

Pairflow is the missing layer.

## The architecture choice

Two crossroads shaped the design before any code was written.

**File patching vs. Admin API for edits.** The Node-RED Admin API offers a clean `POST /flows` endpoint to deploy changes live, without restarting the runtime. It is faster and more elegant in isolation. It was rejected, with reluctance, in favor of directly patching `flows.json` on disk and restarting the service.

The deciding argument was epistemic: when changes go through the Admin API, the response tells you the deploy succeeded, but it does not tell you what the resulting file looks like. Git is the source of truth in the host workflow; `git diff` is how the human reviews what the AI did. An Admin-API-driven workflow would either bypass git entirely or require an after-the-fact serialization step to put changes back into the repo — and that serialization is exactly where formatting drifts and silent reorderings sneak in. Direct file patching keeps `git diff` honest. The cost is a service restart per change cycle; on a development host that is cheap.

**Implementation language: Python with FastMCP.** Existing MCP servers for Node-RED tend to be written in TypeScript. There is nothing wrong with TypeScript, but the host environment for this project already runs Python everywhere — for scripts, for daemons, for ad-hoc verification — and FastMCP is the most ergonomic way to author MCP servers right now. The choice trades giving up easy contribution from the existing TypeScript MCP-NR community against staying inside one language for the whole stack.

**Remote-capable broker configuration.** MQTT verification needs to work against any broker, not just `localhost`. This matters because real Node-RED deployments often span multiple hosts and brokers, and a tool that only worked against the local broker would be useless for half of its intended audience. This is also the smallest commitment toward making the tool publishable rather than personal.

## The naming

Existing tools in this space — both the helpful MCP servers and the in-editor copilots — tend to be named after their technical facts: `node-red-mcp`, `node-red-mcp-server`, `node-red-dev-copilot`. These names are accurate. They are also forgettable, and they describe the *medium* rather than the *purpose*.

Four candidates were considered, with these characters:

- **Loom** — a weaving metaphor, in keeping with Node-RED's visual structure of threads (wires) and nodes (warp). Short, evocative, slightly poetic.
- **Pairflow** — pair-programming + flow. Direct and pragmatic; it announces the goal in the name.
- **Nodewright** — the craftsperson of nodes, in the tradition of playwright, shipwright, wheelwright. Dignified, mastery-oriented.
- **Patchbay** — the audio-engineering term for a rack that routes signals between equipment. Concrete and technical.

The choice came down to a quiet preference for naming the *goal* rather than the *tool*. "Loom" describes what the AI is doing; "Patchbay" describes what the AI is using; "Nodewright" describes the AI's role. "Pairflow" describes the relationship between the human and the AI — which is what this project is actually about. The human said it plainly: *pair = ich und ai, flow = teil von Node-RED*. The name has the additional virtue of being searchable: a developer looking for "pair programming with AI on Node-RED" will find it on the first page of results, where a developer searching for the same thing under "loom" would have to dig past a screen-recording brand.

## What this project is *not* trying to be

A few non-goals, stated explicitly so they don't drift in later:

- **Not** an in-editor sidebar chat. There are already projects that do this well. Pairflow is for AI agents operating from outside the Node-RED UI — terminal-based coding agents, CI bots, autonomous runs.
- **Not** a replacement for the Node-RED Admin API. Pairflow consumes the Admin API where appropriate and complements it where the Admin API has gaps (verification, debug streaming, MQTT).
- **Not** a Node-RED plugin or palette node. Pairflow runs alongside Node-RED, not inside it.
- **Not** a generic Node-RED management tool. It is specifically shaped for AI agents — for example, by validating function-node JavaScript *before* writing it, which is wasted effort for a human in the editor but essential for an AI that cannot see its own typos until runtime.

## What success looks like

Pairflow is successful when the verification loop described at the top of this document collapses from a dozen Bash prompts into something like: "add the Bürorollo discovery, deploy, verify it published with the right payload" — a single structured request, executed by the AI, with the human reading the resulting `git diff` afterward.

When that loop closes cleanly, the project is done. Everything else is polish.
