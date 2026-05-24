"""MQTT publish + subscribe-and-collect helpers.

Used by the `mqtt_pub` and `mqtt_sub_collect` MCP tools. Brokers are looked
up by name from the Pairflow config; credentials come from the broker entry
(with passwords resolved through env vars).
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiomqtt

from .config import BrokerConfig


def _client(broker: BrokerConfig) -> aiomqtt.Client:
    return aiomqtt.Client(
        hostname=broker.host,
        port=broker.port,
        username=broker.username,
        password=broker.password,
    )


def _decode_payload(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.hex()


async def sub_collect(
    broker: BrokerConfig,
    topic: str,
    seconds: float,
    max_messages: int,
) -> list[dict[str, Any]]:
    """Subscribe to `topic`, collect messages for `seconds` (or until
    `max_messages` are received, whichever comes first), then disconnect.

    Returns a list of `{topic, payload, qos, retain}` dicts. Payloads are
    decoded as UTF-8 where possible, otherwise returned as a hex string.
    """
    messages: list[dict[str, Any]] = []

    async with _client(broker) as client:
        await client.subscribe(topic)

        async def _consume() -> None:
            async for m in client.messages:
                messages.append({
                    "topic": str(m.topic),
                    "payload": _decode_payload(m.payload) if isinstance(m.payload, bytes) else m.payload,
                    "qos": int(m.qos),
                    "retain": bool(m.retain),
                })
                if max_messages and len(messages) >= max_messages:
                    return

        try:
            await asyncio.wait_for(_consume(), timeout=seconds)
        except TimeoutError:
            pass

    return messages


async def publish(
    broker: BrokerConfig,
    topic: str,
    payload: str | bytes,
    retain: bool = False,
    qos: int = 0,
) -> dict[str, Any]:
    """Publish a single message and disconnect.

    `payload` may be a string (UTF-8 encoded for transmission) or raw bytes.
    """
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    async with _client(broker) as client:
        await client.publish(topic, payload=body, qos=qos, retain=retain)
    return {
        "topic": topic,
        "bytes": len(body),
        "qos": qos,
        "retain": retain,
        "broker": f"{broker.host}:{broker.port}",
    }


async def pub_and_observe(
    broker: BrokerConfig,
    pub_topic: str,
    pub_payload: str | bytes,
    observe_topics: list[str],
    seconds: float,
    max_messages: int = 100,
    pub_retain: bool = False,
    pub_qos: int = 0,
) -> dict[str, Any]:
    """Subscribe to `observe_topics`, then publish, then collect responses
    on a single connection.

    The subscribe call(s) complete (SUBACK received) before the publish is
    sent, so reaction messages that the publish triggers cannot race the
    subscription. After publish, messages are collected until `seconds`
    elapse or `max_messages` are received, whichever comes first.

    Returns `{published, observed, broker}` where `published` mirrors the
    `mqtt.publish` shape (topic/bytes/qos/retain) and `observed` is a list
    of `{topic, payload, qos, retain}` dicts (same shape as `sub_collect`).
    """
    if not observe_topics:
        raise ValueError(
            "observe_topics must contain at least one topic; "
            "use mqtt.publish when no observation is needed."
        )

    body = pub_payload.encode("utf-8") if isinstance(pub_payload, str) else pub_payload
    messages: list[dict[str, Any]] = []

    async with _client(broker) as client:
        for t in observe_topics:
            await client.subscribe(t)

        await client.publish(pub_topic, payload=body, qos=pub_qos, retain=pub_retain)

        async def _consume() -> None:
            async for m in client.messages:
                messages.append({
                    "topic": str(m.topic),
                    "payload": _decode_payload(m.payload) if isinstance(m.payload, bytes) else m.payload,
                    "qos": int(m.qos),
                    "retain": bool(m.retain),
                })
                if max_messages and len(messages) >= max_messages:
                    return

        try:
            await asyncio.wait_for(_consume(), timeout=seconds)
        except TimeoutError:
            pass

    return {
        "published": {
            "topic": pub_topic,
            "bytes": len(body),
            "qos": pub_qos,
            "retain": pub_retain,
        },
        "observed": messages,
        "broker": f"{broker.host}:{broker.port}",
    }
