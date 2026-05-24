"""Tests for the function-node sandbox runner.

These tests spawn a real `node` subprocess (the wrapper script's whole
point is to exercise actual JS semantics). They are skipped if `node` is
not on PATH so the suite still runs in a node-less environment.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pairflow import function_runner

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node binary not on PATH; cannot exercise the function-node sandbox",
)


def _write_flow(tmp_path: Path, func_body: str, node_id: str = "fn1") -> Path:
    flows_file = tmp_path / "flows.json"
    flows_file.write_text(json.dumps([
        {"id": "tab1", "type": "tab", "label": "Tab"},
        {"id": node_id, "type": "function", "z": "tab1",
         "name": "test", "func": func_body, "outputs": 1, "wires": [[]]},
    ]))
    return flows_file


@pytest.mark.asyncio
async def test_run_function_sync_return_msg(tmp_path: Path):
    flows_file = _write_flow(tmp_path, "msg.payload = msg.payload * 2; return msg;")
    r = await function_runner.run_function(
        flows_file, "fn1", msg={"payload": 21}, timeout=0.5,
    )
    assert r["outputs"] == [{"payload": 42}]
    assert r["errors"] == []
    assert r["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_run_function_node_send(tmp_path: Path):
    flows_file = _write_flow(
        tmp_path,
        "node.send({payload: 'a'}); node.send({payload: 'b'}); return null;",
    )
    r = await function_runner.run_function(flows_file, "fn1", timeout=0.5)
    assert r["outputs"] == [{"payload": "a"}, {"payload": "b"}]


@pytest.mark.asyncio
async def test_run_function_node_warn_and_error(tmp_path: Path):
    flows_file = _write_flow(
        tmp_path,
        "node.warn('careful'); node.error('boom'); return msg;",
    )
    r = await function_runner.run_function(
        flows_file, "fn1", msg={"x": 1}, timeout=0.5,
    )
    assert r["warns"] == ["careful"]
    assert r["errors"] == ["boom"]
    assert r["outputs"] == [{"x": 1}]


@pytest.mark.asyncio
async def test_run_function_settimeout_callback_fires_within_timeout(tmp_path: Path):
    flows_file = _write_flow(
        tmp_path,
        "setTimeout(() => node.send({payload: 'late'}), 100); return null;",
    )
    r = await function_runner.run_function(flows_file, "fn1", timeout=0.5)
    assert r["outputs"] == [{"payload": "late"}]


@pytest.mark.asyncio
async def test_run_function_settimeout_error_is_captured(tmp_path: Path):
    """The original motivating bug: silent failure inside setTimeout
    (Object.keys(undefined) → TypeError) must show up in errors."""
    flows_file = _write_flow(
        tmp_path,
        "setTimeout(() => { Object.keys(undefined); }, 50); return null;",
    )
    r = await function_runner.run_function(flows_file, "fn1", timeout=0.5)
    assert r["outputs"] == []
    assert len(r["errors"]) == 1
    assert "TypeError" in r["errors"][0] or "undefined" in r["errors"][0]


@pytest.mark.asyncio
async def test_run_function_sync_throw_is_captured(tmp_path: Path):
    flows_file = _write_flow(tmp_path, "throw new Error('nope');")
    r = await function_runner.run_function(flows_file, "fn1", timeout=0.5)
    assert any("nope" in e for e in r["errors"])
    assert r["outputs"] == []


@pytest.mark.asyncio
async def test_run_function_context_isolated_per_run(tmp_path: Path):
    """context is in-memory; a new run starts with an empty store."""
    flows_file = _write_flow(
        tmp_path,
        "const v = context.get('n') || 0; context.set('n', v + 1); "
        "msg.payload = v + 1; return msg;",
    )
    r1 = await function_runner.run_function(flows_file, "fn1", msg={}, timeout=0.5)
    r2 = await function_runner.run_function(flows_file, "fn1", msg={}, timeout=0.5)
    assert r1["outputs"] == [{"payload": 1}]
    assert r2["outputs"] == [{"payload": 1}]


@pytest.mark.asyncio
async def test_run_function_missing_node_raises(tmp_path: Path):
    flows_file = _write_flow(tmp_path, "return msg;")
    with pytest.raises(ValueError, match="No node with id"):
        await function_runner.run_function(flows_file, "nonexistent", timeout=0.5)


@pytest.mark.asyncio
async def test_run_function_wrong_node_type_raises(tmp_path: Path):
    flows_file = tmp_path / "flows.json"
    flows_file.write_text(json.dumps([
        {"id": "tab1", "type": "tab", "label": "Tab"},
        {"id": "n1", "type": "inject", "z": "tab1"},
    ]))
    with pytest.raises(ValueError, match="not 'function'"):
        await function_runner.run_function(flows_file, "n1", timeout=0.5)


@pytest.mark.asyncio
async def test_run_function_multi_output_via_array(tmp_path: Path):
    """node.send([msg1, msg2]) emits to outputs 0 and 1 — we preserve the array."""
    flows_file = _write_flow(
        tmp_path,
        "node.send([{payload: 'left'}, {payload: 'right'}]); return null;",
    )
    r = await function_runner.run_function(flows_file, "fn1", timeout=0.5)
    assert r["outputs"] == [[{"payload": "left"}, {"payload": "right"}]]


@pytest.mark.asyncio
async def test_run_function_empty_body_returns_nothing(tmp_path: Path):
    flows_file = _write_flow(tmp_path, "")
    r = await function_runner.run_function(flows_file, "fn1", timeout=0.2)
    assert r["outputs"] == []
    assert r["errors"] == []


@pytest.mark.asyncio
async def test_run_function_user_console_log_does_not_break_envelope(tmp_path: Path):
    """User-level console.log is ignored; only the sentinel envelope is parsed."""
    flows_file = _write_flow(
        tmp_path,
        "console.log('user noise'); console.log('more noise'); "
        "node.send({payload: 'ok'}); return null;",
    )
    r = await function_runner.run_function(flows_file, "fn1", timeout=0.5)
    assert r["outputs"] == [{"payload": "ok"}]
