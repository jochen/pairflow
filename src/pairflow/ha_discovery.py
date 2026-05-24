"""Home Assistant MQTT Discovery helpers.

Two operations:

  * ``list_discoveries(broker, component=None)`` collects retained configs
    on ``homeassistant/<component>/+/config`` (or ``+/+/config`` for all
    components) and returns a summarized list. Empty retained payloads
    (Discovery "deletes") are filtered out.

  * ``validate_discovery(broker, topic)`` reads the retained config at a
    specific Discovery topic and runs a minimal shape check: JSON object,
    ``unique_id`` present, known topic/template fields are strings, and
    (for the handful of components Pairflow knows about) at least one of
    the required topic-field groups is present.

The validator is intentionally shallow. HA's full schema spans hundreds
of fields per platform and changes between releases; trying to be
authoritative would be an unwinnable battle. The goal here is to catch
the obvious mistakes — missing unique_id, command_topic typo'd as
"command_topi", payload field accidentally a dict — before an AI's write
ships a broken Discovery.
"""

from __future__ import annotations

import json
from typing import Any

from . import mqtt
from .config import BrokerConfig

# --- list ----------------------------------------------------------------- #


def _parse_discovery_topic(topic: str) -> tuple[str, str] | None:
    """Extract (component, entity_id) from a homeassistant Discovery topic.

    Two layouts are valid per HA spec:
      homeassistant/<component>/<object_id>/config
      homeassistant/<component>/<node_id>/<object_id>/config
    """
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != "homeassistant" or parts[-1] != "config":
        return None
    component = parts[1]
    if len(parts) == 4:
        return component, parts[2]
    if len(parts) == 5:
        return component, f"{parts[2]}/{parts[3]}"
    return None


async def list_discoveries(
    broker: BrokerConfig,
    component: str | None = None,
    seconds: float = 2.0,
    max_messages: int = 500,
) -> list[dict[str, Any]]:
    """Return a summary of retained Discovery configs on `broker`."""
    topic = f"homeassistant/{component or '+'}/+/config"
    messages = await mqtt.sub_collect(broker, topic, seconds, max_messages)
    # Also collect the longer topic layout (4-segment after homeassistant/)
    long_topic = f"homeassistant/{component or '+'}/+/+/config"
    messages.extend(await mqtt.sub_collect(broker, long_topic, seconds, max_messages))

    results: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    for m in messages:
        if m["topic"] in seen_topics:
            continue
        seen_topics.add(m["topic"])
        if not m.get("retain"):
            continue  # Discovery configs are always retained
        if not m["payload"]:
            continue  # empty retained payload == HA Discovery "delete"
        parsed = _parse_discovery_topic(m["topic"])
        if not parsed:
            continue
        comp, entity_id = parsed
        try:
            cfg = json.loads(m["payload"])
        except json.JSONDecodeError:
            continue
        if not isinstance(cfg, dict):
            continue
        results.append({
            "topic": m["topic"],
            "component": comp,
            "entity_id": entity_id,
            "unique_id": cfg.get("unique_id") or cfg.get("uniq_id"),
            "name": cfg.get("name"),
            "device_class": cfg.get("device_class") or cfg.get("dev_cla"),
            "device_identifiers": (cfg.get("device") or {}).get("identifiers"),
        })
    results.sort(key=lambda r: (r["component"], r["entity_id"]))
    return results


# --- validate ------------------------------------------------------------- #


# Minimal per-component shape: which field groups are "the entity needs at least
# one of these" to be functional. Pairflow's validator does not try to be a
# full HA schema — just enough to catch the obvious mistakes.
_COMPONENT_ANY_OF: dict[str, list[list[str]]] = {
    # Cover needs either command_topic (open/close/stop strings) or set_position_topic
    "cover":         [["command_topic"], ["set_position_topic"]],
    "sensor":        [["state_topic"]],
    "binary_sensor": [["state_topic"]],
    "switch":        [["command_topic"]],
    "light":         [["command_topic"]],
    "climate":       [["mode_command_topic"], ["temperature_command_topic"]],
    "fan":           [["command_topic"]],
    "lock":          [["command_topic"]],
    "number":        [["command_topic"]],
    "select":        [["command_topic"]],
    "button":        [["command_topic"]],
    "text":          [["command_topic"]],
    "device_tracker": [["state_topic"], ["json_attributes_topic"]],
}


_TOPIC_FIELDS = {
    "command_topic", "state_topic", "position_topic", "set_position_topic",
    "tilt_command_topic", "tilt_status_topic", "availability_topic",
    "json_attributes_topic", "mode_command_topic", "mode_state_topic",
    "temperature_command_topic", "temperature_state_topic",
    "current_temperature_topic", "action_topic", "preset_mode_command_topic",
    "preset_mode_state_topic", "swing_mode_command_topic",
    "swing_mode_state_topic", "fan_mode_command_topic",
    "fan_mode_state_topic", "brightness_command_topic", "brightness_state_topic",
    "rgb_command_topic", "rgb_state_topic", "color_temp_command_topic",
    "color_temp_state_topic",
}


def _validate_shape(component: str, cfg: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if not (cfg.get("unique_id") or cfg.get("uniq_id")):
        errors.append(
            "Missing 'unique_id' — HA cannot persist this entity across restarts "
            "without one, and duplicate-detection won't work."
        )

    for field in _TOPIC_FIELDS:
        if field in cfg and not isinstance(cfg[field], str):
            errors.append(f"Field {field!r} must be a string, got {type(cfg[field]).__name__}")

    device = cfg.get("device")
    if device is not None:
        if not isinstance(device, dict):
            errors.append(f"Field 'device' must be an object, got {type(device).__name__}")
        else:
            if device.get("identifiers") is None and device.get("connections") is None:
                warnings.append(
                    "'device' has neither 'identifiers' nor 'connections' — HA may "
                    "not group entities under one device card."
                )

    any_of_groups = _COMPONENT_ANY_OF.get(component)
    if any_of_groups is None:
        warnings.append(
            f"Component {component!r} is not in Pairflow's known list; "
            "performed structural check only."
        )
    else:
        ok = any(all(f in cfg for f in group) for group in any_of_groups)
        if not ok:
            options = " or ".join(" + ".join(g) for g in any_of_groups)
            errors.append(f"Need at least one of: {options}")

    return {"errors": errors, "warnings": warnings}


async def validate_discovery(
    broker: BrokerConfig,
    topic: str,
    seconds: float = 2.0,
    include_config: bool = False,
) -> dict[str, Any]:
    """Fetch the retained config at `topic` and run a shape validation.

    Returns ``{topic, valid, component, entity_id, errors, warnings}``.
    `valid` is True only if `errors` is empty. `warnings` is informational.

    `include_config=True` adds the full parsed config under the `config`
    key. Off by default — climate/light/etc. configs are large and rarely
    needed to act on validation results.
    """
    parsed = _parse_discovery_topic(topic)
    if not parsed:
        return {
            "topic": topic, "valid": False, "errors": [
                f"Topic {topic!r} is not a Discovery config topic "
                "(expected homeassistant/<component>/.../config)"
            ], "warnings": [], "component": None,
        }
    component, entity_id = parsed

    # Validation needs the full payload regardless of caller's include_config
    # preference, so disable payload truncation on this internal sub.
    messages = await mqtt.sub_collect(
        broker, topic, seconds=seconds, max_messages=1, max_payload_chars=0
    )
    if not messages:
        return {
            "topic": topic, "valid": False, "errors": [
                f"No retained message found on {topic} (waited {seconds}s)"
            ], "warnings": [], "component": component,
        }
    payload = messages[0]["payload"]
    if not payload:
        return {
            "topic": topic, "valid": False, "errors": [
                "Retained payload is empty (this is the HA convention for 'delete this entity')"
            ], "warnings": [], "component": component,
        }
    try:
        cfg = json.loads(payload)
    except json.JSONDecodeError as exc:
        return {
            "topic": topic, "valid": False, "errors": [f"Invalid JSON: {exc}"],
            "warnings": [], "component": component,
        }
    if not isinstance(cfg, dict):
        result: dict[str, Any] = {
            "topic": topic, "valid": False, "errors": [
                f"Config must be a JSON object, got {type(cfg).__name__}"
            ], "warnings": [], "component": component,
        }
        if include_config:
            result["config"] = cfg
        return result

    shape = _validate_shape(component, cfg)
    result = {
        "topic": topic,
        "valid": not shape["errors"],
        "component": component,
        "entity_id": entity_id,
        "errors": shape["errors"],
        "warnings": shape["warnings"],
    }
    if include_config:
        result["config"] = cfg
    return result
