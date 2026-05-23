"""Pairflow CLI entry point.

Usage:
    pairflow [--config PATH]

The MCP server speaks over stdio by default — connect it to an MCP client
(Claude Code, Cursor, etc.) via the client's MCP server configuration.
"""

from __future__ import annotations

import argparse
import sys

from .config import load_config
from .server import build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pairflow",
        description="MCP server for AI-assisted Node-RED engineering.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to pairflow config (default: $PAIRFLOW_CONFIG or "
             "$XDG_CONFIG_HOME/pairflow/config.toml).",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"pairflow: {exc}", file=sys.stderr)
        return 2

    server = build_server(config)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
