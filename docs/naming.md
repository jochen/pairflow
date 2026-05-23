# Naming

This document records why the project is called **Pairflow**, what alternatives were considered, and why they were not chosen. It is short on purpose — but it exists because future contributors will reasonably ask the question, and because the naming decision happens to encode something real about the project's intent.

## The name

**Pairflow** reads two ways simultaneously, and both are intended.

- **Pair** as in pair-programming: a human and an AI working together on the same artifact, in real time, with shared goals and shared feedback.
- **Flow** as in Node-RED flow: the unit of work being created and modified.

The full reading is therefore "pair-programming on flows" — which describes the relationship between the user, the AI, and the artifact more precisely than any name purely about Node-RED or MCP.

## What was rejected

Three alternatives reached the final round:

### Loom

A weaving metaphor: Node-RED's visual structure looks like a loom, with nodes as warp threads and wires as weft. The AI, when restructuring flows, is doing something close to weaving — drawing threads through, crossing them, forming patterns.

Loom is short, evocative, and slightly poetic. The argument against it was that it describes the *action of the AI* without describing the *relationship between the AI and the human*. The project is about the latter. There is also a popular screen-recording brand using the name; the search collision is survivable but not free.

### Nodewright

In the tradition of *playwright*, *shipwright*, *wheelwright* — the craftsperson who builds a particular kind of thing. A nodewright builds Node-RED nodes.

Nodewright is dignified and conveys mastery. It was set aside because it locates the project's identity in the AI alone, casting the human as a client of the craft rather than as a partner. The pair-programming intent is the more accurate framing.

### Patchbay

From audio and broadcast engineering: a patchbay is a physical panel of jacks used to route signals between pieces of equipment. The AI, when reconfiguring flows, is in some sense operating a patchbay.

Patchbay is concrete and resonates with engineers who know the term. It was set aside for the same reason as Nodewright: it describes the medium (the wiring) rather than the relationship (the pairing).

## Why the relationship matters

The choice to name the *relationship* rather than the *medium* is the load-bearing decision. It commits the project to a particular shape: tools that close the loop between AI action and human-readable feedback (the `git diff` after every change, the MQTT verification primitives, the debug streaming) rather than tools that simply give the AI more power over the flows file.

A project named Loom or Patchbay could justifiably ignore Tier 2 (verification) — it would still serve its name. A project named Pairflow cannot. The name commits to closing the loop.

## On searchability

A practical aside: "Pairflow" is well-positioned for discovery. A developer searching for "AI pair programming Node-RED", "pair coding flow editor", or similar phrases will land on this repository quickly. The competing names either share a namespace with much larger brands (Loom) or are obscure enough that no one would search for them (Nodewright, Patchbay).

This is a minor consideration, not the primary one. But it lines up with the substantive argument, which is reassuring.
