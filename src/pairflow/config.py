"""Configuration loading for Pairflow.

The config file is TOML. Resolution order:

  1. The path passed via --config / load_config(path=...).
  2. $PAIRFLOW_CONFIG.
  3. $XDG_CONFIG_HOME/pairflow/config.toml (or ~/.config/pairflow/config.toml).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class NodeRedConfig:
    flows_file: Path
    admin_url: str = "http://localhost:1880"
    service: str = "nodered"
    project_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password_env: str | None = None  # name of env var holding the password

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_env) if self.password_env else None


@dataclass(frozen=True, slots=True)
class Config:
    node_red: NodeRedConfig
    brokers: dict[str, BrokerConfig] = field(default_factory=dict)

    def broker(self, name: str = "default") -> BrokerConfig:
        try:
            return self.brokers[name]
        except KeyError as exc:
            raise KeyError(
                f"No broker named {name!r} in config; available: {sorted(self.brokers)}"
            ) from exc


def _default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "pairflow" / "config.toml"


def resolve_config_path(explicit: str | os.PathLike | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get("PAIRFLOW_CONFIG")
    if env:
        return Path(env).expanduser()
    return _default_config_path()


def load_config(path: str | os.PathLike | None = None) -> Config:
    config_path = resolve_config_path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Pairflow config not found at {config_path}. "
            "Create it (see examples/config.example.toml) or pass --config."
        )

    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    nr_raw = raw.get("node_red")
    if not nr_raw or "flows_file" not in nr_raw:
        raise ValueError(
            f"Config {config_path} is missing required [node_red] table with flows_file."
        )

    nr = NodeRedConfig(
        flows_file=Path(nr_raw["flows_file"]).expanduser(),
        admin_url=nr_raw.get("admin_url", "http://localhost:1880"),
        service=nr_raw.get("service", "nodered"),
        project_dir=(
            Path(nr_raw["project_dir"]).expanduser()
            if nr_raw.get("project_dir")
            else None
        ),
    )

    brokers: dict[str, BrokerConfig] = {}
    for name, b in (raw.get("mqtt") or {}).items():
        brokers[name] = BrokerConfig(
            host=b["host"],
            port=int(b.get("port", 1883)),
            username=b.get("username"),
            password_env=b.get("password_env"),
        )

    return Config(node_red=nr, brokers=brokers)
