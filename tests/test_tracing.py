"""Tests for trace_pipeline. NR /comms WS, MQTT client, and the inject
HTTP call are all mocked — the test asserts ordering (streams open BEFORE
inject) and the combined return shape."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from pairflow import tracing
from pairflow.config import BrokerConfig


@pytest.fixture
def broker() -> BrokerConfig:
    return BrokerConfig(host="localhost", port=1883)


# --- WS stand-in (matches the one in test_nr_admin.py) --------------------- #


class _FakeWS:
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


# --- MQTT stand-in --------------------------------------------------------- #


class _FakeMessage:
    def __init__(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False):
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain


class _FakeMqttClient:
    def __init__(self, messages: list[_FakeMessage]):
        self._messages = messages
        self.subscribed: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def subscribe(self, topic: str):
        self.subscribed.append(topic)

    @property
    def messages(self):
        async def _gen():
            for m in self._messages:
                yield m
        return _gen()


# --- tests ----------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_trace_pipeline_collects_debug_and_mqtt(broker: BrokerConfig):
    debug_events = [
        {"topic": "debug", "data": {
            "id": "fn1", "z": "tab", "name": "bridge", "topic": "cmd",
            "msg": "got setpoint 18.5", "timestamp": 1,
        }},
        {"topic": "debug", "data": {
            "id": "fn2", "z": "tab", "name": "sink", "topic": "out",
            "msg": "{'topic':'eq3/x/command','payload':18.5}", "timestamp": 2,
        }},
    ]
    mqtt_msgs = [_FakeMessage("eq3/x/command", b"18.5", qos=0, retain=False)]

    fake_ws = _FakeWS(debug_events)
    fake_mqtt = _FakeMqttClient(mqtt_msgs)

    with patch("pairflow.tracing.websockets.connect", return_value=fake_ws), \
         patch("pairflow.tracing.mqtt._client", return_value=fake_mqtt), \
         patch("pairflow.tracing.nr_admin.inject",
               return_value={"node_id": "inject1", "status_code": 200, "triggered": True}):
        r = await tracing.trace_pipeline(
            admin_url="http://localhost:1880",
            trigger_inject="inject1",
            seconds=0.5,
            expect_topic="eq3/x/command",
            broker=broker,
            settle_ms=0,
        )

    assert len(r["fired_nodes"]) == 2
    assert r["fired_nodes"][0]["id"] == "fn1"
    assert r["output_msgs"] == [
        {"topic": "eq3/x/command", "payload": "18.5", "qos": 0, "retain": False}
    ]
    assert r["path_complete"] is True
    assert r["inject"]["triggered"] is True
    assert r["duration_ms"] >= 0
    assert fake_mqtt.subscribed == ["eq3/x/command"]
    # WS subscribe frame is the first thing we send.
    assert len(fake_ws.sent) == 1
    assert "subscribe" in fake_ws.sent[0] and "debug" in fake_ws.sent[0]


@pytest.mark.asyncio
async def test_trace_pipeline_subscribe_before_inject(broker: BrokerConfig):
    """The whole reason this tool exists: subscribes must happen before
    the inject fires."""
    order: list[str] = []

    class _OrderingWS(_FakeWS):
        async def send(self, frame: str):
            order.append("ws-subscribe")
            await super().send(frame)

    class _OrderingMqtt(_FakeMqttClient):
        async def subscribe(self, topic: str):
            order.append(f"mqtt-subscribe:{topic}")
            await super().subscribe(topic)

    def _spy_inject(_url, _node):
        order.append("inject")
        return {"node_id": _node, "status_code": 200, "triggered": True}

    with patch("pairflow.tracing.websockets.connect", return_value=_OrderingWS([])), \
         patch("pairflow.tracing.mqtt._client",
               return_value=_OrderingMqtt([])), \
         patch("pairflow.tracing.nr_admin.inject", side_effect=_spy_inject):
        await tracing.trace_pipeline(
            admin_url="http://localhost:1880",
            trigger_inject="inject1",
            seconds=0.05,
            expect_topic="eq3/x/command",
            broker=broker,
            settle_ms=0,
        )

    assert order == ["ws-subscribe", "mqtt-subscribe:eq3/x/command", "inject"]


@pytest.mark.asyncio
async def test_trace_pipeline_without_expect_topic_returns_path_complete_none():
    """No MQTT topic given → no MQTT connection, path_complete is None."""
    with patch("pairflow.tracing.websockets.connect", return_value=_FakeWS([])), \
         patch("pairflow.tracing.mqtt._client") as mqtt_client_factory, \
         patch("pairflow.tracing.nr_admin.inject",
               return_value={"node_id": "n", "status_code": 200, "triggered": True}):
        r = await tracing.trace_pipeline(
            admin_url="http://localhost:1880",
            trigger_inject="n",
            seconds=0.05,
            settle_ms=0,
        )
    assert r["path_complete"] is None
    assert r["output_msgs"] == []
    mqtt_client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_trace_pipeline_path_incomplete_when_no_mqtt_arrives(broker: BrokerConfig):
    """expect_topic set but no MQTT message → path_complete is False."""

    class _SlowMqtt(_FakeMqttClient):
        @property
        def messages(self):
            async def _gen():
                await asyncio.sleep(10)
                yield None
            return _gen()

    with patch("pairflow.tracing.websockets.connect", return_value=_FakeWS([])), \
         patch("pairflow.tracing.mqtt._client", return_value=_SlowMqtt([])), \
         patch("pairflow.tracing.nr_admin.inject",
               return_value={"node_id": "n", "status_code": 200, "triggered": True}):
        r = await tracing.trace_pipeline(
            admin_url="http://localhost:1880",
            trigger_inject="n",
            seconds=0.05,
            expect_topic="eq3/x/command",
            broker=broker,
            settle_ms=0,
        )
    assert r["path_complete"] is False


@pytest.mark.asyncio
async def test_trace_pipeline_expect_topic_requires_broker():
    with pytest.raises(ValueError, match="broker"):
        await tracing.trace_pipeline(
            admin_url="http://localhost:1880",
            trigger_inject="n",
            seconds=0.05,
            expect_topic="some/topic",
            broker=None,
        )


@pytest.mark.asyncio
async def test_trace_pipeline_filter_substr_narrows_debug(broker: BrokerConfig):
    debug_events = [
        {"topic": "debug", "data": {"id": "n1", "msg": "Bridge fired"}},
        {"topic": "debug", "data": {"id": "n2", "msg": "Unrelated traffic"}},
        {"topic": "debug", "data": {"id": "n3", "msg": "bridge again"}},
    ]
    with patch("pairflow.tracing.websockets.connect",
               return_value=_FakeWS(debug_events)), \
         patch("pairflow.tracing.nr_admin.inject",
               return_value={"node_id": "n", "status_code": 200, "triggered": True}):
        r = await tracing.trace_pipeline(
            admin_url="http://localhost:1880",
            trigger_inject="n",
            seconds=0.5,
            filter_substr="bridge",
            settle_ms=0,
        )
    assert {x["id"] for x in r["fired_nodes"]} == {"n1", "n3"}


@pytest.mark.asyncio
async def test_trace_pipeline_inject_failure_propagates():
    fake_ws = _FakeWS([])
    with patch("pairflow.tracing.websockets.connect", return_value=fake_ws), \
         patch("pairflow.tracing.nr_admin.inject",
               side_effect=ValueError("404 for inject")):
        with pytest.raises(ValueError, match="404"):
            await tracing.trace_pipeline(
                admin_url="http://localhost:1880",
                trigger_inject="bad-id",
                seconds=0.5,
                settle_ms=0,
            )


@pytest.mark.asyncio
async def test_trace_pipeline_ws_connect_error_is_runtime_error():
    with patch("pairflow.tracing.websockets.connect",
               side_effect=OSError("connection refused")):
        with pytest.raises(RuntimeError, match="Cannot connect"):
            await tracing.trace_pipeline(
                admin_url="http://localhost:1880",
                trigger_inject="n",
                seconds=0.5,
                settle_ms=0,
            )


@pytest.mark.asyncio
async def test_trace_pipeline_max_debug_cap(broker: BrokerConfig):
    debug_events = [
        {"topic": "debug", "data": {"id": str(i), "msg": "x"}}
        for i in range(10)
    ]
    with patch("pairflow.tracing.websockets.connect",
               return_value=_FakeWS(debug_events)), \
         patch("pairflow.tracing.nr_admin.inject",
               return_value={"node_id": "n", "status_code": 200, "triggered": True}):
        r = await tracing.trace_pipeline(
            admin_url="http://localhost:1880",
            trigger_inject="n",
            seconds=2.0,
            max_debug=3,
            settle_ms=0,
        )
    assert len(r["fired_nodes"]) == 3
