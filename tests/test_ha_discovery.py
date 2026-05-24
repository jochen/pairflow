"""Tests for the HA MQTT Discovery helpers.

`list_discoveries` and `validate_discovery` both call `mqtt.sub_collect`
underneath, so we patch that at the seam — letting us drive arbitrary
retained payloads into the validator without standing up an MQTT broker.
The shape validator is also covered directly (it's pure).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from pairflow import ha_discovery
from pairflow.config import BrokerConfig


@pytest.fixture
def broker() -> BrokerConfig:
    return BrokerConfig(host="localhost", port=1883)


def _msg(topic: str, payload: dict | str, retain: bool = True) -> dict:
    body = json.dumps(payload) if isinstance(payload, dict) else payload
    return {"topic": topic, "payload": body, "qos": 0, "retain": retain}


# --- topic parsing -------------------------------------------------------- #


def test_parse_topic_short_form():
    assert ha_discovery._parse_discovery_topic(
        "homeassistant/cover/rollo_buero/config"
    ) == ("cover", "rollo_buero")


def test_parse_topic_long_form():
    assert ha_discovery._parse_discovery_topic(
        "homeassistant/sensor/node1/temp/config"
    ) == ("sensor", "node1/temp")


def test_parse_topic_rejects_non_ha():
    assert ha_discovery._parse_discovery_topic("foo/bar/baz/config") is None
    assert ha_discovery._parse_discovery_topic("homeassistant/cover/x") is None
    assert ha_discovery._parse_discovery_topic("homeassistant/a/b/c/d/e/config") is None


# --- list_discoveries ----------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_discoveries_basic(broker: BrokerConfig):
    msgs_short = [
        _msg("homeassistant/cover/rollo_a/config",
             {"unique_id": "rollo_a", "name": "Rollo A", "device_class": "shutter",
              "device": {"identifiers": ["rollo_a"]}}),
        _msg("homeassistant/sensor/temp_a/config",
             {"unique_id": "temp_a", "name": "Temp"}),
    ]
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(side_effect=[msgs_short, []])):
        out = await ha_discovery.list_discoveries(broker)
    by_id = {r["entity_id"]: r for r in out}
    assert by_id["rollo_a"]["component"] == "cover"
    assert by_id["rollo_a"]["unique_id"] == "rollo_a"
    assert by_id["rollo_a"]["device_class"] == "shutter"
    assert by_id["rollo_a"]["device_identifiers"] == ["rollo_a"]
    assert by_id["temp_a"]["component"] == "sensor"


@pytest.mark.asyncio
async def test_list_discoveries_filters_empty_retained(broker: BrokerConfig):
    msgs = [
        _msg("homeassistant/cover/keep/config", {"unique_id": "keep"}),
        _msg("homeassistant/cover/deleted/config", "", retain=True),
    ]
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(side_effect=[msgs, []])):
        out = await ha_discovery.list_discoveries(broker)
    assert [r["entity_id"] for r in out] == ["keep"]


@pytest.mark.asyncio
async def test_list_discoveries_skips_non_retained(broker: BrokerConfig):
    msgs = [
        _msg("homeassistant/cover/keep/config", {"unique_id": "keep"}, retain=True),
        _msg("homeassistant/cover/transient/config", {"unique_id": "x"}, retain=False),
    ]
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(side_effect=[msgs, []])):
        out = await ha_discovery.list_discoveries(broker)
    assert [r["entity_id"] for r in out] == ["keep"]


@pytest.mark.asyncio
async def test_list_discoveries_skips_invalid_json(broker: BrokerConfig):
    msgs = [
        _msg("homeassistant/cover/ok/config", {"unique_id": "ok"}),
        _msg("homeassistant/cover/broken/config", "{not json", retain=True),
    ]
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(side_effect=[msgs, []])):
        out = await ha_discovery.list_discoveries(broker)
    assert [r["entity_id"] for r in out] == ["ok"]


@pytest.mark.asyncio
async def test_list_discoveries_dedups_topics_seen_twice(broker: BrokerConfig):
    """Short and long topic searches may return the same topic — dedup expected."""
    msg = _msg("homeassistant/cover/x/config", {"unique_id": "x"})
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(side_effect=[[msg], [msg]])):
        out = await ha_discovery.list_discoveries(broker)
    assert len(out) == 1


# --- validate_discovery (shape) ------------------------------------------- #


def test_validate_shape_cover_minimal_ok():
    cfg = {
        "unique_id": "rollo_x",
        "command_topic": "z2m/x/set",
        "device": {"identifiers": ["rollo_x"]},
    }
    r = ha_discovery._validate_shape("cover", cfg)
    assert r["errors"] == []


def test_validate_shape_missing_unique_id():
    cfg = {"command_topic": "x/set", "device": {"identifiers": ["x"]}}
    r = ha_discovery._validate_shape("cover", cfg)
    assert any("unique_id" in e for e in r["errors"])


def test_validate_shape_cover_missing_command_and_position():
    cfg = {"unique_id": "x", "name": "X"}
    r = ha_discovery._validate_shape("cover", cfg)
    assert any("command_topic" in e or "set_position_topic" in e for e in r["errors"])


def test_validate_shape_topic_field_wrong_type():
    cfg = {
        "unique_id": "x",
        "command_topic": ["x/set"],  # list instead of string
    }
    r = ha_discovery._validate_shape("cover", cfg)
    assert any("command_topic" in e and "must be a string" in e for e in r["errors"])


def test_validate_shape_device_not_object():
    cfg = {"unique_id": "x", "command_topic": "x/set", "device": "rollo"}
    r = ha_discovery._validate_shape("cover", cfg)
    assert any("device" in e and "must be an object" in e for e in r["errors"])


def test_validate_shape_device_without_identifiers_warns():
    cfg = {"unique_id": "x", "command_topic": "x/set", "device": {"name": "X"}}
    r = ha_discovery._validate_shape("cover", cfg)
    assert r["errors"] == []
    assert any("identifiers" in w for w in r["warnings"])


def test_validate_shape_unknown_component_warns_only():
    cfg = {"unique_id": "x", "command_topic": "y/set"}
    r = ha_discovery._validate_shape("vacuum-bot-9000", cfg)
    assert r["errors"] == []
    assert any("known list" in w for w in r["warnings"])


def test_validate_shape_set_position_satisfies_any_of():
    cfg = {"unique_id": "x", "set_position_topic": "x/pos"}
    r = ha_discovery._validate_shape("cover", cfg)
    assert r["errors"] == []


# --- validate_discovery (full path with mocked mqtt) ---------------------- #


@pytest.mark.asyncio
async def test_validate_discovery_happy_path(broker: BrokerConfig):
    cfg = {"unique_id": "x", "command_topic": "x/set", "device": {"identifiers": ["x"]}}
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(return_value=[_msg("homeassistant/cover/x/config", cfg)])):
        r = await ha_discovery.validate_discovery(broker, "homeassistant/cover/x/config")
    assert r["valid"] is True
    assert r["component"] == "cover"
    assert r["entity_id"] == "x"
    assert r["errors"] == []
    # config is opt-in now to save tokens
    assert "config" not in r


@pytest.mark.asyncio
async def test_validate_discovery_include_config(broker: BrokerConfig):
    cfg = {"unique_id": "x", "command_topic": "x/set", "device": {"identifiers": ["x"]}}
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(return_value=[_msg("homeassistant/cover/x/config", cfg)])):
        r = await ha_discovery.validate_discovery(
            broker, "homeassistant/cover/x/config", include_config=True
        )
    assert r["config"] == cfg


@pytest.mark.asyncio
async def test_validate_discovery_bad_topic_pattern(broker: BrokerConfig):
    r = await ha_discovery.validate_discovery(broker, "not/a/discovery/topic")
    assert r["valid"] is False
    assert any("Discovery config topic" in e for e in r["errors"])


@pytest.mark.asyncio
async def test_validate_discovery_no_retained_message(broker: BrokerConfig):
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(return_value=[])):
        r = await ha_discovery.validate_discovery(broker, "homeassistant/cover/x/config")
    assert r["valid"] is False
    assert any("No retained message" in e for e in r["errors"])


@pytest.mark.asyncio
async def test_validate_discovery_empty_payload_is_deletion(broker: BrokerConfig):
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(return_value=[_msg("homeassistant/cover/x/config", "", retain=True)])):
        r = await ha_discovery.validate_discovery(broker, "homeassistant/cover/x/config")
    assert r["valid"] is False
    assert any("Empty" in e or "empty" in e for e in r["errors"])


@pytest.mark.asyncio
async def test_validate_discovery_invalid_json(broker: BrokerConfig):
    with patch("pairflow.ha_discovery.mqtt.sub_collect",
               AsyncMock(return_value=[_msg("homeassistant/cover/x/config", "{nope", retain=True)])):
        r = await ha_discovery.validate_discovery(broker, "homeassistant/cover/x/config")
    assert r["valid"] is False
    assert any("Invalid JSON" in e for e in r["errors"])
