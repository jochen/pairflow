"""Pipeline tracing: fire an inject, watch debug + MQTT in one atomic call.

The wins-over-composition story:

  * Both streams are opened *before* the inject fires, so a fast
    NR→bridge→MQTT response cannot race the subscription. (Running
    `nr_tail_debug` and `mqtt_sub_collect` and `nr_inject` as three
    parallel tools is racy — observed in the eq3 thermostat session.)
  * One round trip in, one structured record out — `fired_nodes` from
    the debug sidebar, `output_msgs` from MQTT, `path_complete` as a
    cheap "did anything reach the expected topic" flag.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack
from typing import Any

import websockets

from . import mqtt, nr_admin
from .config import BrokerConfig


async def trace_pipeline(
    admin_url: str,
    trigger_inject: str,
    seconds: float,
    expect_topic: str | None = None,
    broker: BrokerConfig | None = None,
    filter_substr: str | None = None,
    max_debug: int = 100,
    max_mqtt: int = 100,
    settle_ms: int = 50,
    max_msg_chars: int = 2000,
    max_payload_chars: int = 2000,
) -> dict[str, Any]:
    """Open debug WS (+ optional MQTT sub), fire inject, collect for `seconds`.

    `expect_topic` is optional; when omitted, only the debug sidebar is
    observed and `path_complete` is `None` (nothing to compare against).
    When given, `broker` is required.

    `settle_ms` is a small delay between opening streams and firing inject
    so that NR has time to register the WS subscribe (NR /comms does not
    ACK subscribes, so we cannot wait for confirmation). 50 ms is generous
    on localhost; bump it for remote brokers / NR instances.
    """
    if expect_topic and broker is None:
        raise ValueError("expect_topic requires a broker config")

    debug_records: list[dict[str, Any]] = []
    mqtt_records: list[dict[str, Any]] = []
    needle = filter_substr.lower() if filter_substr else None
    ws_url = nr_admin._ws_url(admin_url)

    start = time.monotonic()

    async with AsyncExitStack() as stack:
        try:
            ws = await stack.enter_async_context(
                websockets.connect(ws_url, open_timeout=5)
            )
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            raise RuntimeError(
                f"Cannot connect to Node-RED comms at {ws_url}: {exc}"
            ) from exc

        await ws.send(json.dumps([{"subscribe": "debug"}]))

        mqtt_client = None
        if expect_topic:
            assert broker is not None
            mqtt_client = await stack.enter_async_context(mqtt._client(broker))
            await mqtt_client.subscribe(expect_topic)

        # Let NR settle the WS subscribe before we fire.
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000)

        # Run the synchronous inject off-thread so we don't block the event
        # loop (a slow Admin API would otherwise starve the consumers).
        inject_result = await asyncio.to_thread(
            nr_admin.inject, admin_url, trigger_inject
        )

        async def _debug_consume() -> None:
            async for raw in ws:
                try:
                    events = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(events, list):
                    events = [events]
                for evt in events:
                    if not isinstance(evt, dict):
                        continue
                    topic = evt.get("topic", "")
                    if not topic.startswith("debug"):
                        continue
                    data = evt.get("data", {}) or {}
                    rendered = str(data.get("msg", ""))
                    if needle is not None and needle not in rendered.lower():
                        continue
                    debug_records.append(
                        nr_admin._build_debug_record(data, rendered, max_msg_chars)
                    )
                    if max_debug and len(debug_records) >= max_debug:
                        return

        async def _mqtt_consume() -> None:
            assert mqtt_client is not None
            async for m in mqtt_client.messages:
                payload = (
                    mqtt._decode_payload(m.payload)
                    if isinstance(m.payload, bytes)
                    else m.payload
                )
                mqtt_records.append(
                    mqtt._build_observed_record(m.topic, payload, m.qos, m.retain, max_payload_chars)
                )
                if max_mqtt and len(mqtt_records) >= max_mqtt:
                    return

        tasks = [asyncio.create_task(_debug_consume())]
        if mqtt_client is not None:
            tasks.append(asyncio.create_task(_mqtt_consume()))

        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=seconds,
            )
        except TimeoutError:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    duration_ms = int((time.monotonic() - start) * 1000)
    path_complete = bool(mqtt_records) if expect_topic else None

    return {
        "fired_nodes": debug_records,
        "output_msgs": mqtt_records,
        "path_complete": path_complete,
        "duration_ms": duration_ms,
        "inject": inject_result,
    }
