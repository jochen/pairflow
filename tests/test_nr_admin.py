"""Tests for the Node-RED Admin API client (HTTP + WebSocket mocked)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pairflow import nr_admin

# --- inject ----------------------------------------------------------------- #


def test_inject_success():
    fake_resp = MagicMock(status_code=200, text="")
    with patch("pairflow.nr_admin.httpx.post", return_value=fake_resp) as m:
        r = nr_admin.inject("http://localhost:1880", "nodeX")
    assert r == {"node_id": "nodeX", "status_code": 200, "triggered": True}
    m.assert_called_once()
    assert m.call_args.args[0] == "http://localhost:1880/inject/nodeX"


def test_inject_404_is_value_error():
    fake_resp = MagicMock(status_code=404, text="not found")
    with patch("pairflow.nr_admin.httpx.post", return_value=fake_resp):
        with pytest.raises(ValueError, match="404"):
            nr_admin.inject("http://localhost:1880", "missing")


def test_inject_5xx_is_runtime_error():
    fake_resp = MagicMock(status_code=500, text="boom")
    with patch("pairflow.nr_admin.httpx.post", return_value=fake_resp):
        with pytest.raises(RuntimeError, match="500"):
            nr_admin.inject("http://localhost:1880", "nodeX")


def test_inject_connection_failure_is_runtime_error():
    with patch("pairflow.nr_admin.httpx.post",
               side_effect=httpx.ConnectError("nope")):
        with pytest.raises(RuntimeError, match="Cannot reach"):
            nr_admin.inject("http://localhost:1880", "nodeX")


# --- ws url derivation ------------------------------------------------------ #


def test_ws_url_from_http():
    assert nr_admin._ws_url("http://localhost:1880") == "ws://localhost:1880/comms"


def test_ws_url_from_https():
    assert nr_admin._ws_url("https://nr.example/") == "wss://nr.example/comms"


def test_ws_url_preserves_path_prefix():
    assert nr_admin._ws_url("http://h/nodered") == "ws://h/nodered/comms"


# --- tail_debug ------------------------------------------------------------- #


class _FakeWS:
    """Minimal async-iterable websocket stand-in."""

    def __init__(self, events: list[dict]):
        self._events = events
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def send(self, frame: str):
        self.sent.append(frame)

    def __aiter__(self):
        async def _gen():
            for e in self._events:
                yield json.dumps(e)
        return _gen()


@pytest.mark.asyncio
async def test_tail_debug_filters_to_debug_topic():
    events = [
        {"topic": "notification/runtime-deploy", "data": {"revision": 1}},
        {"topic": "debug", "data": {
            "id": "n1", "z": "tab1", "name": "fn", "topic": "msg.payload",
            "msg": "hello", "format": "string[5]", "timestamp": 12345,
        }},
        {"topic": "status/n2", "data": {"text": "ignore me"}},
        {"topic": "debug", "data": {
            "id": "n3", "z": "tab1", "name": "other", "topic": "",
            "msg": "world", "format": "string[5]", "timestamp": 12346,
        }},
    ]
    with patch("pairflow.nr_admin.websockets.connect",
               return_value=_FakeWS(events)):
        out = await nr_admin.tail_debug("http://localhost:1880", seconds=0.5)
    assert len(out) == 2
    assert out[0]["id"] == "n1"
    assert out[0]["msg"] == "hello"
    assert out[1]["id"] == "n3"


@pytest.mark.asyncio
async def test_tail_debug_substring_filter():
    events = [
        {"topic": "debug", "data": {"id": "n1", "msg": "Hello world", "timestamp": 1}},
        {"topic": "debug", "data": {"id": "n2", "msg": "Goodbye", "timestamp": 2}},
        {"topic": "debug", "data": {"id": "n3", "msg": "well HELLO again", "timestamp": 3}},
    ]
    with patch("pairflow.nr_admin.websockets.connect",
               return_value=_FakeWS(events)):
        out = await nr_admin.tail_debug(
            "http://localhost:1880", seconds=0.5, filter_substr="hello",
        )
    assert {m["id"] for m in out} == {"n1", "n3"}


@pytest.mark.asyncio
async def test_tail_debug_tolerates_non_json_frames():
    """Bad frames must be skipped, not crash the collector."""
    class _Mixed:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def send(self, _frame): pass
        def __aiter__(self):
            async def _gen():
                yield "not json"
                yield json.dumps({"topic": "debug", "data": {"id": "ok", "msg": "good"}})
            return _gen()

    with patch("pairflow.nr_admin.websockets.connect", return_value=_Mixed()):
        out = await nr_admin.tail_debug("http://localhost:1880", seconds=0.5)
    assert len(out) == 1
    assert out[0]["id"] == "ok"


@pytest.mark.asyncio
async def test_tail_debug_max_messages():
    events = [
        {"topic": "debug", "data": {"id": str(i), "msg": "x", "timestamp": i}}
        for i in range(10)
    ]
    with patch("pairflow.nr_admin.websockets.connect",
               return_value=_FakeWS(events)):
        out = await nr_admin.tail_debug(
            "http://localhost:1880", seconds=2.0, max_messages=3,
        )
    assert len(out) == 3


@pytest.mark.asyncio
async def test_tail_debug_sends_subscribe_frame():
    """NR /comms requires an explicit subscribe before it emits events."""
    fake = _FakeWS([])
    with patch("pairflow.nr_admin.websockets.connect", return_value=fake):
        await nr_admin.tail_debug("http://localhost:1880", seconds=0.05)
    assert len(fake.sent) == 1
    assert "subscribe" in fake.sent[0] and "debug" in fake.sent[0]


@pytest.mark.asyncio
async def test_tail_debug_truncates_large_msg():
    big = "x" * 5000
    events = [
        {"topic": "debug", "data": {"id": "big", "msg": big, "timestamp": 1}},
        {"topic": "debug", "data": {"id": "small", "msg": "ok", "timestamp": 2}},
    ]
    with patch("pairflow.nr_admin.websockets.connect",
               return_value=_FakeWS(events)):
        out = await nr_admin.tail_debug(
            "http://localhost:1880", seconds=0.5, max_msg_chars=100,
        )
    big_rec = next(r for r in out if r["id"] == "big")
    assert big_rec["msg"] == "x" * 100
    assert big_rec["msg_truncated"] is True
    assert big_rec["msg_full_chars"] == 5000
    small_rec = next(r for r in out if r["id"] == "small")
    assert small_rec["msg"] == "ok"
    assert "msg_truncated" not in small_rec


@pytest.mark.asyncio
async def test_tail_debug_truncation_disabled_with_zero():
    big = "x" * 5000
    events = [{"topic": "debug", "data": {"id": "big", "msg": big, "timestamp": 1}}]
    with patch("pairflow.nr_admin.websockets.connect",
               return_value=_FakeWS(events)):
        out = await nr_admin.tail_debug(
            "http://localhost:1880", seconds=0.5, max_msg_chars=0,
        )
    assert out[0]["msg"] == big
    assert "msg_truncated" not in out[0]
