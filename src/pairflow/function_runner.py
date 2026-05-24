"""Sandboxed execution of Node-RED function-node bodies.

Runs a function's `func` body in a Node.js subprocess with a minimal mock
of the NR runtime surface — `msg`, `node.{send,warn,error,log}`,
`context`/`flow`/`global` as in-memory key-value stores, `env.get`. The
script collects outputs and errors and prints a single JSON envelope
prefixed with a sentinel, which the Python side parses.

Why subprocess instead of in-process JS engine: we already require `node`
on PATH for `validate.validate_function`, and a real Node.js gives us the
correct semantics for `setTimeout`/Promise — the original motivation for
this tool was a bug *inside* a `setTimeout` callback that the function's
synchronous return value never reflected.

Out of scope (v1): persistent context across runs, `require()` of
function-external modules, status updates.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from . import flows

_SENTINEL = "___PAIRFLOW_RESULT___"


_WRAPPER_TEMPLATE = r"""
'use strict';

const _outputs = [];
const _errors  = [];
const _warns   = [];
const _logs    = [];

function _normalize(v) {
  if (v === undefined) return null;
  if (v === null || typeof v !== 'object') return v;
  try { return JSON.parse(JSON.stringify(v)); }
  catch (e) { return String(v); }
}

const node = {
  send:   (m) => { _outputs.push(_normalize(m)); },
  warn:   (m) => { _warns.push(_normalize(m)); },
  error:  (m) => { _errors.push(_normalize(m)); },
  log:    (m) => { _logs.push(_normalize(m)); },
  status: ()  => {},
  done:   ()  => {},
};

function _kv(prefix) {
  const store = {};
  return {
    get:  (k) => store[k],
    set:  (k, v) => { store[k] = v; },
    keys: ()    => Object.keys(store),
  };
}
const _ctx    = _kv('ctx');
const _flow   = _kv('flow');
const _global = _kv('global');
const _env    = { get: () => undefined };

process.on('uncaughtException',  (e) => { _errors.push(String(e && e.stack ? e.stack : e)); });
process.on('unhandledRejection', (e) => { _errors.push('UnhandledRejection: ' + String(e && e.stack ? e.stack : e)); });

const _msg = __INPUT_MSG__;

const _userFn = async function (msg, context, flow, global, node, env) {
__USER_BODY__
};

const _start = Date.now();

Promise.resolve()
  .then(() => _userFn(_msg, _ctx, _flow, _global, node, _env))
  .then((result) => {
    if (result !== undefined && result !== null) {
      _outputs.push(_normalize(result));
    }
  })
  .catch((e) => {
    _errors.push(String(e && e.stack ? e.stack : e));
  });

setTimeout(() => {
  process.stdout.write('__SENTINEL__' + JSON.stringify({
    outputs:     _outputs,
    errors:      _errors,
    warns:       _warns,
    logs:        _logs,
    duration_ms: Date.now() - _start,
  }) + '\n');
  process.exit(0);
}, __TIMEOUT_MS__);
"""


def _build_wrapper(body: str, msg: dict, timeout_ms: int) -> str:
    return (
        _WRAPPER_TEMPLATE
        .replace("__USER_BODY__", body)
        .replace("__INPUT_MSG__", json.dumps(msg))
        .replace("__TIMEOUT_MS__", str(timeout_ms))
        .replace("__SENTINEL__", _SENTINEL)
    )


async def run_function(
    flows_file: Path,
    node_id: str,
    msg: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Execute the named function node's body sandboxed and return its
    captured outputs.

    Returns `{outputs, errors, warns, logs, duration_ms}` where `outputs`
    is the list of values passed to `node.send(...)` (plus the function's
    explicit `return msg;` if non-null) in call order. `errors` includes
    sync throws, rejected promises, *and* asynchronous failures inside
    `setTimeout`/`setInterval` callbacks (the original motivating bug).

    `timeout` is the hard wall-clock budget given to the function — set it
    long enough to cover any timers you expect to fire. The wrapper exits
    after the timeout regardless of whether work is still pending.
    """
    node = flows.get_node(flows_file, node_id)
    if node is None:
        raise ValueError(f"No node with id {node_id!r} in flows file")
    if node.get("type") != "function":
        raise ValueError(
            f"Node {node_id!r} is type {node.get('type')!r}, not 'function'"
        )

    body = node.get("func", "") or ""
    timeout_ms = max(1, int(timeout * 1000))
    script = _build_wrapper(body, msg or {}, timeout_ms)

    try:
        proc = await asyncio.create_subprocess_exec(
            "node", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "node binary not found on PATH; cannot run function bodies"
        ) from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout + 5.0
        )
    except TimeoutError as exc:
        proc.kill()
        raise RuntimeError(
            f"Function run exceeded grace period ({timeout + 5.0}s); killed"
        ) from exc

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    idx = stdout.rfind(_SENTINEL)
    if idx == -1:
        raise RuntimeError(
            "Function wrapper did not emit a result envelope. "
            f"node exit code: {proc.returncode}. stderr: {stderr[:500]!r}"
        )

    envelope_line = stdout[idx + len(_SENTINEL):].splitlines()[0]
    try:
        result: dict[str, Any] = json.loads(envelope_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Could not parse result envelope: {exc}; raw: {envelope_line[:200]!r}"
        ) from exc

    return result
