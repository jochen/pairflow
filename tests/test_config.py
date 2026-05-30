"""Tests for the config loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pairflow.config import load_config, resolve_config_path


def _write_config(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip())


def test_resolve_path_explicit_wins(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PAIRFLOW_CONFIG", str(tmp_path / "env.toml"))
    resolved = resolve_config_path(tmp_path / "explicit.toml")
    assert resolved == tmp_path / "explicit.toml"


def test_resolve_path_env_used_when_no_explicit(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PAIRFLOW_CONFIG", str(tmp_path / "env.toml"))
    assert resolve_config_path() == tmp_path / "env.toml"


def test_resolve_path_falls_back_to_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("PAIRFLOW_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert resolve_config_path() == tmp_path / "pairflow" / "config.toml"


def test_load_config_minimal(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    _write_config(
        cfg_file,
        """
        [node_red]
        flows_file = "/tmp/flows.json"

        [mqtt.default]
        host = "localhost"
        """,
    )
    cfg = load_config(cfg_file)
    assert cfg.node_red.flows_file == Path("/tmp/flows.json")
    assert cfg.node_red.admin_url == "http://localhost:1880"  # default
    assert cfg.node_red.eager_reload is True  # default
    assert cfg.broker("default").host == "localhost"


def test_load_config_eager_reload_can_be_disabled(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    _write_config(
        cfg_file,
        """
        [node_red]
        flows_file = "/tmp/flows.json"
        eager_reload = false
        """,
    )
    cfg = load_config(cfg_file)
    assert cfg.node_red.eager_reload is False


def test_load_config_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_load_config_missing_node_red_section(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("[mqtt.default]\nhost = \"localhost\"\n")
    with pytest.raises(ValueError, match="node_red"):
        load_config(cfg_file)


def test_load_config_multiple_brokers(tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    _write_config(
        cfg_file,
        """
        [node_red]
        flows_file = "/tmp/flows.json"

        [mqtt.default]
        host = "localhost"

        [mqtt.remote]
        host = "broker.example.tld"
        username = "u"
        password_env = "PW_VAR"
        """,
    )
    cfg = load_config(cfg_file)
    assert set(cfg.brokers) == {"default", "remote"}
    assert cfg.broker("remote").username == "u"
    assert cfg.broker("remote").password_env == "PW_VAR"


def test_broker_password_from_env(monkeypatch, tmp_path: Path):
    cfg_file = tmp_path / "c.toml"
    _write_config(
        cfg_file,
        """
        [node_red]
        flows_file = "/tmp/flows.json"

        [mqtt.remote]
        host = "broker.example.tld"
        password_env = "MY_PW"
        """,
    )
    monkeypatch.setenv("MY_PW", "s3cret")
    cfg = load_config(cfg_file)
    assert cfg.broker("remote").password == "s3cret"
