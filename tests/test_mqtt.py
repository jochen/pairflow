"""Tests for the MQTT helpers (aiomqtt mocked).

Real-broker integration is covered by manual smoke testing against a
localhost broker — these unit tests verify behavior of the wrapper logic
(timeout, max_messages cap, payload decoding) with the client mocked out.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from pairflow import mqtt
from pairflow.config import BrokerConfig


@pytest.fixture
def broker() -> BrokerConfig:
    return BrokerConfig(host="localhost", port=1883)


class _FakeMessage:
    def __init__(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False):
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain


class _FakeClient:
    """Minimal aiomqtt.Client stand-in."""

    def __init__(self, messages: list[_FakeMessage]):
        self._messages = messages
        self.subscribed: list[str] = []
        self.published: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def subscribe(self, topic: str):
        self.subscribed.append(topic)

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    @property
    def messages(self):
        async def _gen():
            for m in self._messages:
                yield m
        return _gen()


# --- sub_collect ------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_sub_collect_all_messages(broker: BrokerConfig):
    msgs = [
        _FakeMessage("a/b", b"hello", qos=1, retain=True),
        _FakeMessage("a/c", b"world", qos=0, retain=False),
    ]
    fake = _FakeClient(msgs)
    with patch("pairflow.mqtt._client", return_value=fake):
        out = await mqtt.sub_collect(broker, "a/#", seconds=1.0, max_messages=10)
    assert len(out) == 2
    assert out[0] == {"topic": "a/b", "payload": "hello", "qos": 1, "retain": True}
    assert fake.subscribed == ["a/#"]


@pytest.mark.asyncio
async def test_sub_collect_max_messages_cap(broker: BrokerConfig):
    msgs = [_FakeMessage(f"t/{i}", f"p{i}".encode()) for i in range(10)]
    fake = _FakeClient(msgs)
    with patch("pairflow.mqtt._client", return_value=fake):
        out = await mqtt.sub_collect(broker, "t/#", seconds=2.0, max_messages=3)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_sub_collect_timeout(broker: BrokerConfig):
    """If no messages arrive, returns empty list without raising."""

    class _Empty(_FakeClient):
        @property
        def messages(self):
            async def _gen():
                await asyncio.sleep(10)  # never yields within the test window
                yield None
            return _gen()

    fake = _Empty([])
    with patch("pairflow.mqtt._client", return_value=fake):
        out = await mqtt.sub_collect(broker, "t/#", seconds=0.05, max_messages=10)
    assert out == []


@pytest.mark.asyncio
async def test_sub_collect_decodes_invalid_utf8_as_hex(broker: BrokerConfig):
    msgs = [_FakeMessage("bin", b"\xff\xfe\x00")]
    fake = _FakeClient(msgs)
    with patch("pairflow.mqtt._client", return_value=fake):
        out = await mqtt.sub_collect(broker, "bin", seconds=0.5, max_messages=1)
    assert out[0]["payload"] == "fffe00"


# --- publish ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_publish_string_payload(broker: BrokerConfig):
    fake = _FakeClient([])
    with patch("pairflow.mqtt._client", return_value=fake):
        r = await mqtt.publish(broker, "t/x", "hello", retain=True, qos=1)
    assert r["topic"] == "t/x"
    assert r["bytes"] == 5
    assert r["retain"] is True
    assert fake.published == [("t/x", b"hello", 1, True)]


@pytest.mark.asyncio
async def test_publish_bytes_payload(broker: BrokerConfig):
    fake = _FakeClient([])
    with patch("pairflow.mqtt._client", return_value=fake):
        r = await mqtt.publish(broker, "t/x", b"\x00\x01\x02")
    assert r["bytes"] == 3
    assert fake.published[0][1] == b"\x00\x01\x02"
