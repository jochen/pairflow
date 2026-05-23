# Contributing to Pairflow

Thanks for being here. Pairflow is early — the surface is intentionally narrow and the design choices are explicit (see [`STORY.md`](STORY.md) and [`docs/architecture.md`](docs/architecture.md)) so that the project doesn't drift before it has shape.

## Quick start

```bash
git clone https://github.com/jochen/pairflow.git
cd pairflow
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest ruff
```

Run the full test suite:

```bash
pytest -q
```

Run only the fast unit tests (skip the realistic-fixture integration ones):

```bash
pytest -q -m "not integration"
```

Lint:

```bash
ruff check src tests
```

## What changes need before a PR

When you open a pull request, GitHub Actions will run the full test suite on Python 3.11, 3.12, and 3.13. The same checks should pass locally before you push:

1. **`ruff check src tests`** — lint clean.
2. **`pytest -q`** — all tests green, including the integration tests that exercise the tools against a 200-node realistic synthetic flow.
3. **For changes that touch the tool surface**: an integration test added to `tests/integration/test_large_flow.py` that exercises the new behavior against the realistic fixture. Unit tests with mini-fixtures are also welcome, but the integration test is the load-bearing one — it's what catches surprises on real-shaped data.
4. **For changes that touch function-node JS validation, write-path safety, or backup semantics**: a unit test in the relevant `tests/test_*.py` file with a focused assertion.

If you're not sure whether a test belongs in unit or integration, default to unit. Integration tests are for "this should work on a real-world-shaped flow", not for everything.

## Doctor: a smoke check for your own setups

If you maintain a Node-RED instance and want to verify Pairflow works on *your* flows file before pointing the MCP server at it:

```bash
pairflow-doctor --flows /path/to/your/flows.json
```

The doctor is read-only by default. It reports the file's shape (tab count, node-type histogram), validates every function-node body's JS syntax, and flags structural anomalies (duplicate ids, dangling link references, wires to missing nodes).

To additionally run a write-path exercise — add, update, wire, unwire, delete, with backups — pass `--smoke`. The smoke run operates on a temporary copy; the original file is never modified.

```bash
pairflow-doctor --flows /path/to/your/flows.json --smoke
```

## Design conventions

A few conventions the codebase follows; please continue them in PRs:

- **Every write goes through `writer.atomic_write`**, which handles the backup + mtime-lock + atomic rename together. Don't bypass it for "simple" cases.
- **Function-node JS is validated before write, not after.** If you add a new write-path tool that can introduce a function body, route it through `_maybe_validate_function` or call `validate.validate_function` directly before the write commits.
- **Tool docstrings are read by the AI client as part of the tool's contract.** Keep them precise about argument meaning, return shape, and side effects. Examples in docstrings are welcome.
- **Filesystem operations live in `writer.py`.** Pure data mutations live in `flows.py`. JS validation lives in `validate.py`. The MCP wiring lives in `server.py`. New code should pick the smallest of those that fits.

## Things that are out of scope for Pairflow itself

These are deliberate non-goals, mentioned here so contributors don't spend time on them and then have a PR closed:

- An in-editor Node-RED sidebar/chat. There are existing projects in that category.
- Driving the Node-RED browser UI.
- Becoming a generic Node-RED management tool.
- Replacing the Node-RED Admin API. Pairflow consumes it where useful and complements it where it falls short.

If your idea sounds adjacent to one of these, open an issue first to discuss whether it fits.
