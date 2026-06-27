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
    # After the first flow-mutating write since the last deploy, fire one
    # Admin-API `reload` so the runtime revision advances past what any open
    # editor holds. That arms the editor's "flows changed in the background"
    # warning and the 409 version-mismatch guard early — protecting in-progress
    # disk edits from being overwritten by a human deploy if this session ends
    # before nr_deploy. See nr_admin.reload. Set false to keep the old
    # behaviour (warning only appears after nr_deploy / restart).
    eager_reload: bool = True
    # Node-RED user directory (where settings.js and .config*.json live).
    # When None, callers default to ~/.node-red.
    user_dir: Path | None = None
    # Explicit path to the credential store file.  When None, it is derived
    # the way Node-RED itself derives it (see effective_credentials_file).
    # Set this explicitly when Node-RED projects mode is active — then the
    # cred file lives inside the active project directory, not the user dir.
    credentials_file: Path | None = None

    @property
    def effective_user_dir(self) -> Path:
        """Node-RED user directory, defaulting to ~/.node-red."""
        return self.user_dir or Path.home() / ".node-red"

    @property
    def effective_credentials_file(self) -> Path:
        """Credential-store path, derived the way Node-RED derives it.

        Node-RED (``storage/localfilesystem/projects/index.js``) computes the
        cred file as ``userDir / (basename(flowFile, ext) + "_cred" + ext)`` —
        i.e. in the **user directory**, using only the flows file's *basename*,
        NOT in the flows file's own directory.  This matters when ``flows_file``
        points into a project folder (e.g.
        ``~/.node-red/projects/foo/flows_foo.json``): the cred file is still
        ``~/.node-red/flows_foo_cred.json``.

        With Node-RED *projects mode* active the cred file lives inside the
        project dir instead; set ``credentials_file`` explicitly for that case.
        """
        if self.credentials_file is not None:
            return self.credentials_file
        ff = self.flows_file
        return self.effective_user_dir / (ff.stem + "_cred" + ff.suffix)


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
class TelemetryConfig:
    """Optional per-call usage logging — disabled by default.

    When `usage_log` is true, every MCP tool call appends one JSON line to
    `usage_log_path` (default: $XDG_STATE_HOME/pairflow/usage.jsonl, i.e.
    ~/.local/state/pairflow/usage.jsonl). See `pairflow.usage_log` for the
    record shape.
    """
    usage_log: bool = False
    usage_log_path: Path | None = None


@dataclass(frozen=True, slots=True)
class Config:
    node_red: NodeRedConfig
    brokers: dict[str, BrokerConfig] = field(default_factory=dict)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

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
        eager_reload=bool(nr_raw.get("eager_reload", True)),
        user_dir=(
            Path(nr_raw["user_dir"]).expanduser()
            if nr_raw.get("user_dir")
            else None
        ),
        credentials_file=(
            Path(nr_raw["credentials_file"]).expanduser()
            if nr_raw.get("credentials_file")
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

    tel_raw = raw.get("telemetry") or {}
    telemetry = TelemetryConfig(
        usage_log=bool(tel_raw.get("usage_log", False)),
        usage_log_path=(
            Path(tel_raw["usage_log_path"]).expanduser()
            if tel_raw.get("usage_log_path")
            else None
        ),
    )

    return Config(node_red=nr, brokers=brokers, telemetry=telemetry)
