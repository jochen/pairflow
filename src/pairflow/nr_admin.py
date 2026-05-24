"""Node-RED Admin API client.

Two operations live here:

  * ``inject(admin_url, node_id)`` — fire an inject node via the HTTP
    Admin API. Synchronous because it's a single short request.
  * ``tail_debug(admin_url, seconds, filter_substr)`` — connect to the
    Node-RED ``/comms`` WebSocket, collect debug-channel events for the
    requested window, return them as structured records. Asynchronous
    because it streams.

Both assume the Admin API is reachable without authentication, which is
the default for many development setups. Auth-protected setups will need
a follow-up to thread a token through here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets

# --- inject ---------------------------------------------------------------- #


def inject(admin_url: str, node_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """Trigger an inject node by id. Returns a small status dict."""
    url = admin_url.rstrip("/") + f"/inject/{node_id}"
    try:
        r = httpx.post(url, timeout=timeout)
    except httpx.RequestError as exc:
        raise RuntimeError(f"Cannot reach Node-RED Admin API at {admin_url}: {exc}") from exc

    if r.status_code == 404:
        raise ValueError(
            f"Inject endpoint returned 404 for node {node_id!r}. "
            "Either the node id is wrong, the node is not an inject node, "
            "or its tab is disabled."
        )
    if r.status_code >= 400:
        raise RuntimeError(
            f"Admin API returned {r.status_code} for {url}: {r.text[:200]}"
        )

    return {"node_id": node_id, "status_code": r.status_code, "triggered": True}


# --- debug stream ---------------------------------------------------------- #


def _ws_url(admin_url: str) -> str:
    parts = urlsplit(admin_url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, "ws")
    path = (parts.path.rstrip("/") + "/comms") if parts.path else "/comms"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


async def tail_debug(
    admin_url: str,
    seconds: float,
    filter_substr: str | None = None,
    max_messages: int = 500,
    max_msg_chars: int = 2000,
) -> list[dict[str, Any]]:
    """Collect debug-channel messages from Node-RED's /comms WebSocket.

    Returns one record per debug event. Each record contains the source node
    id, its tab id, name, topic, the rendered message string, and the
    server-side timestamp. Other event topics (notifications, status, etc.)
    are filtered out.

    `filter_substr`: case-insensitive substring filter applied to the
    rendered message string. Use it to narrow to a specific topic/payload.

    `max_msg_chars`: per-message cap on the rendered `msg` field. When a
    message would exceed it, the string is truncated and `msg_truncated`
    is set on the record together with `msg_full_chars` (original length).
    Pass 0 to disable truncation.
    """
    url = _ws_url(admin_url)
    messages: list[dict[str, Any]] = []
    needle = filter_substr.lower() if filter_substr else None

    async def _consume(ws) -> None:
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
                record = _build_debug_record(data, rendered, max_msg_chars)
                messages.append(record)
                if max_messages and len(messages) >= max_messages:
                    return

    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            # Node-RED /comms requires an explicit subscribe before it sends
            # events. The format is a JSON array of one or more {subscribe}
            # objects; "debug" requests the debug channel.
            await ws.send(json.dumps([{"subscribe": "debug"}]))
            try:
                await asyncio.wait_for(_consume(ws), timeout=seconds)
            except TimeoutError:
                pass
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        raise RuntimeError(f"Cannot connect to Node-RED comms at {url}: {exc}") from exc

    return messages


def _build_debug_record(
    data: dict[str, Any],
    rendered: str,
    max_msg_chars: int,
) -> dict[str, Any]:
    """Shape a single debug-stream event into the record returned to callers.

    Truncates the rendered `msg` if it exceeds `max_msg_chars` (0 disables),
    tagging the record with the original length so the caller can decide
    whether to fetch more.
    """
    record: dict[str, Any] = {
        "id": data.get("id"),
        "z": data.get("z"),
        "name": data.get("name"),
        "msg_topic": data.get("topic"),
        "msg": rendered,
        "format": data.get("format"),
        "timestamp": data.get("timestamp"),
    }
    if max_msg_chars and len(rendered) > max_msg_chars:
        record["msg"] = rendered[:max_msg_chars]
        record["msg_truncated"] = True
        record["msg_full_chars"] = len(rendered)
    return record
