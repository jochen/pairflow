"""Tests for the per-call usage logger."""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from pairflow.config import TelemetryConfig, load_config
from pairflow.usage_log import (
    UsageLogger,
    _find_truncations,
    _response_bytes,
    _response_shape,
    _scrub_value,
)

# --- config ---------------------------------------------------------------- #


def test_config_defaults_have_telemetry_disabled(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(textwrap.dedent("""
        [node_red]
        flows_file = "/tmp/flows.json"

        [mqtt.default]
        host = "localhost"
    """).lstrip())
    cfg = load_config(cfg_file)
    assert cfg.telemetry.usage_log is False
    assert cfg.telemetry.usage_log_path is None


def test_config_loads_telemetry_block(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    log_path = tmp_path / "u.jsonl"
    cfg_file.write_text(textwrap.dedent(f"""
        [node_red]
        flows_file = "/tmp/flows.json"

        [mqtt.default]
        host = "localhost"

        [telemetry]
        usage_log      = true
        usage_log_path = "{log_path}"
    """).lstrip())
    cfg = load_config(cfg_file)
    assert cfg.telemetry.usage_log is True
    assert cfg.telemetry.usage_log_path == log_path


# --- scrub_value ----------------------------------------------------------- #


def test_scrub_short_string_passthrough():
    assert _scrub_value("hello", 200, 800) == "hello"


def test_scrub_long_string_redacted():
    s = "x" * 500
    out = _scrub_value(s, 200, 800)
    assert out == {"_size": 500, "_type": "str", "_head": "x" * 80}


def test_scrub_bytes_redacted_by_size():
    out = _scrub_value(b"\x00\x01\x02" * 200, 200, 800)
    assert out["_type"] == "bytes"
    assert out["_size"] == 600


def test_scrub_bytes_short_keeps_hex_head():
    out = _scrub_value(b"\x00\x01\x02", 200, 800)
    assert out == {"_size": 3, "_type": "bytes", "_head": "000102"}


def test_scrub_dict_short_passthrough():
    d = {"a": 1, "b": 2}
    assert _scrub_value(d, 200, 800) == d


def test_scrub_dict_large_redacted():
    d = {f"key_{i}": "v" * 30 for i in range(40)}
    out = _scrub_value(d, 200, 800)
    assert out["_type"] == "dict"
    assert out["_size"] > 800
    assert len(out["_keys"]) == 10  # capped


def test_scrub_list_short_recurses():
    out = _scrub_value(["a", "x" * 500, 42], 200, 800)
    assert out[0] == "a"
    assert out[1]["_size"] == 500
    assert out[2] == 42


def test_scrub_list_very_long_summarized():
    out = _scrub_value(list(range(100)), 200, 800)
    assert out == {"_size": 100, "_type": "list"}


# --- find_truncations ------------------------------------------------------ #


def test_find_truncations_flat():
    obj = [
        {"id": "a", "msg_truncated": True},
        {"id": "b"},
        {"id": "c", "msg_truncated": True},
    ]
    assert _find_truncations(obj) == {"msg_truncated": 2}


def test_find_truncations_nested():
    obj = {"observed": [{"payload_truncated": True}], "extra": {"msg_truncated": True}}
    assert _find_truncations(obj) == {"payload_truncated": 1, "msg_truncated": 1}


def test_find_truncations_ignores_false():
    obj = [{"msg_truncated": False}, {"msg_truncated": True}]
    assert _find_truncations(obj) == {"msg_truncated": 1}


def test_find_truncations_empty():
    assert _find_truncations(None) == {}
    assert _find_truncations({"a": 1}) == {}


# --- response shape/bytes -------------------------------------------------- #


def test_response_shape_list():
    assert _response_shape([1, 2, 3]) == {"type": "list", "len": 3}


def test_response_shape_dict():
    assert _response_shape({"a": 1, "b": 2}) == {"type": "dict", "keys": 2}


def test_response_bytes_none_is_zero():
    assert _response_bytes(None) == 0


def test_response_bytes_handles_non_serializable():
    class _Weird:
        pass
    # falls back to default=str, so we get a number, not -1
    assert _response_bytes({"x": _Weird()}) > 0


# --- wrap (sync) ----------------------------------------------------------- #


def test_disabled_logger_passes_through(tmp_path: Path):
    logger = UsageLogger.disabled()

    def myfn(a: int, b: int = 2) -> int:
        return a + b

    wrapped = logger.wrap(myfn)
    assert wrapped is myfn  # not wrapped at all


def test_enabled_sync_writes_one_record(tmp_path: Path):
    log_path = tmp_path / "u.jsonl"
    logger = UsageLogger(enabled=True, path=log_path)

    def add(a: int, b: int = 2) -> int:
        return a + b

    wrapped = logger.wrap(add)
    assert wrapped(3) == 5

    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["tool"] == "add"
    assert rec["args"] == {"a": 3, "b": 2}
    assert rec["response_bytes"] > 0
    assert rec["response_shape"] == {"type": "int"}
    assert rec["truncations"] == {}
    assert "error" not in rec


def test_enabled_sync_logs_error_path(tmp_path: Path):
    log_path = tmp_path / "u.jsonl"
    logger = UsageLogger(enabled=True, path=log_path)

    def boom():
        raise ValueError("nope")

    wrapped = logger.wrap(boom)
    with pytest.raises(ValueError, match="nope"):
        wrapped()

    rec = json.loads(log_path.read_text().splitlines()[0])
    assert rec["error"] == {"type": "ValueError", "msg": "nope"}
    assert rec["response_bytes"] == 0


def test_sync_wrap_scrubs_large_string_arg(tmp_path: Path):
    log_path = tmp_path / "u.jsonl"
    logger = UsageLogger(enabled=True, path=log_path)

    def take_code(code: str) -> dict:
        return {"len": len(code)}

    wrapped = logger.wrap(take_code)
    wrapped("x" * 1000)

    rec = json.loads(log_path.read_text().splitlines()[0])
    assert rec["args"]["code"]["_size"] == 1000
    assert rec["args"]["code"]["_type"] == "str"
    assert rec["args"]["code"]["_head"] == "x" * 80


def test_sync_wrap_records_truncation_flags(tmp_path: Path):
    log_path = tmp_path / "u.jsonl"
    logger = UsageLogger(enabled=True, path=log_path)

    def listy() -> list:
        return [
            {"id": "1", "msg_truncated": True},
            {"id": "2"},
            {"id": "3", "msg_truncated": True},
        ]

    wrapped = logger.wrap(listy)
    wrapped()

    rec = json.loads(log_path.read_text().splitlines()[0])
    assert rec["truncations"] == {"msg_truncated": 2}
    assert rec["response_shape"]["len"] == 3


def test_sync_wrap_appends_across_calls(tmp_path: Path):
    log_path = tmp_path / "u.jsonl"
    logger = UsageLogger(enabled=True, path=log_path)

    def f(a: int) -> int:
        return a

    wrapped = logger.wrap(f)
    wrapped(1)
    wrapped(2)
    wrapped(3)

    lines = log_path.read_text().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["args"]["a"] for line in lines] == [1, 2, 3]


# --- wrap (async) ---------------------------------------------------------- #


def test_async_wrap_writes_record(tmp_path: Path):
    log_path = tmp_path / "u.jsonl"
    logger = UsageLogger(enabled=True, path=log_path)

    async def aadd(a: int, b: int) -> int:
        await asyncio.sleep(0)
        return a + b

    wrapped = logger.wrap(aadd)
    result = asyncio.run(wrapped(2, 3))
    assert result == 5

    rec = json.loads(log_path.read_text().splitlines()[0])
    assert rec["tool"] == "aadd"
    assert rec["args"] == {"a": 2, "b": 3}


def test_async_wrap_logs_error(tmp_path: Path):
    log_path = tmp_path / "u.jsonl"
    logger = UsageLogger(enabled=True, path=log_path)

    async def aboom():
        raise RuntimeError("async boom")

    wrapped = logger.wrap(aboom)
    with pytest.raises(RuntimeError, match="async boom"):
        asyncio.run(wrapped())

    rec = json.loads(log_path.read_text().splitlines()[0])
    assert rec["error"]["type"] == "RuntimeError"


# --- from_config ----------------------------------------------------------- #


def test_from_config_disabled():
    tel = TelemetryConfig(usage_log=False)
    logger = UsageLogger.from_config(tel)
    assert logger.enabled is False


def test_from_config_enabled_creates_parent(tmp_path: Path):
    log_path = tmp_path / "nested" / "deep" / "u.jsonl"
    tel = TelemetryConfig(usage_log=True, usage_log_path=log_path)
    logger = UsageLogger.from_config(tel)
    assert logger.enabled is True
    assert logger.path == log_path
    assert log_path.parent.is_dir()


# --- safety: logger never breaks tool calls -------------------------------- #


# --- integration: wrap must not break FastMCP introspection --------------- #


def test_build_server_tools_retain_parameter_schema(tmp_path: Path):
    """The wrap decorator must preserve signatures so FastMCP introspects
    real parameter schemas — not the wrapper's (*args, **kwargs) blob."""
    import asyncio

    from pairflow.config import BrokerConfig, Config, NodeRedConfig
    from pairflow.server import build_server

    cfg = Config(
        node_red=NodeRedConfig(flows_file=tmp_path / "flows.json"),
        brokers={"default": BrokerConfig(host="localhost")},
        telemetry=TelemetryConfig(usage_log=True, usage_log_path=tmp_path / "u.jsonl"),
    )
    server = build_server(cfg)
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}

    # All 25 tools registered.
    assert len(tools) == 25

    # A canonical sync tool: nr_list_tabs(include_info: bool = False)
    p = by_name["nr_list_tabs"].parameters
    assert "include_info" in p["properties"]
    assert p["properties"]["include_info"]["default"] is False

    # A canonical async tool: mqtt_sub_collect carries the truncation cap
    p = by_name["mqtt_sub_collect"].parameters
    assert "max_payload_chars" in p["properties"]
    assert p["properties"]["max_payload_chars"]["default"] == 2000


def test_logger_write_failure_does_not_break_call(tmp_path: Path, monkeypatch):
    # Path that we'll immediately make un-writeable by pointing at a directory
    bad_path = tmp_path / "asdir"
    bad_path.mkdir()
    logger = UsageLogger(enabled=True, path=bad_path)  # path is a dir, can't open as file

    def f(x: int) -> int:
        return x * 2

    wrapped = logger.wrap(f)
    assert wrapped(7) == 14  # must not raise
