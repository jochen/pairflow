"""systemd service control + journal access for the Node-RED runtime.

`restart` is the way file-based flow changes become live: Pairflow patches
`flows.json` on disk, then `nr_deploy` calls `restart` to make NR pick the
changes up. After issuing the restart we poll the Admin API and return only
when NR is ready (or the timeout fires).

`journal` is the after-the-fact diagnostic: read the last N lines from
`journalctl -u <service>`, optionally filtered by a regular expression.
Together with `nr_admin.tail_debug` this closes the verification loop —
runtime errors that don't surface as debug messages still land in the
journal.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any

import httpx


def restart(
    service: str,
    admin_url: str | None = None,
    wait_timeout: float = 30.0,
    use_sudo: bool = True,
) -> dict[str, Any]:
    """Restart a systemd service and (optionally) wait for the Admin API to come back.

    `use_sudo=True` (default) prefixes the command with `sudo -n` so it
    fails immediately with a clear error if the invoking user doesn't have
    a passwordless sudoers entry for systemctl.

    If `admin_url` is given, polls it after the restart and returns
    `ready=True` once it responds, or `ready=False` with a warning if the
    poll timeout elapses first.
    """
    cmd = (["sudo", "-n"] if use_sudo else []) + ["systemctl", "restart", service]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        hint = ""
        if "password is required" in stderr or "a password is required" in stderr.lower():
            hint = (
                "  Hint: configure a NOPASSWD sudoers entry, e.g.\n"
                "    %sudo ALL=(root) NOPASSWD: /bin/systemctl restart " + service
            )
        raise RuntimeError(
            f"systemctl restart {service} failed (exit {proc.returncode}): {stderr}\n{hint}"
        )

    result: dict[str, Any] = {
        "service": service,
        "restarted": True,
        "restart_seconds": round(time.time() - started, 2),
    }

    if admin_url:
        result.update(_wait_for_admin(admin_url, wait_timeout))

    return result


def _wait_for_admin(admin_url: str, timeout: float) -> dict[str, Any]:
    """Poll admin_url/settings until it responds 200, or until `timeout` elapses."""
    url = admin_url.rstrip("/") + "/settings"
    deadline = time.time() + timeout
    last_err: str | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return {"ready": True, "ready_after_seconds": round(timeout - (deadline - time.time()), 2)}
            last_err = f"HTTP {r.status_code}"
        except httpx.RequestError as exc:
            last_err = str(exc)
        time.sleep(0.5)
    return {"ready": False, "ready_timeout": timeout, "last_error": last_err}


def journal(
    service: str,
    lines: int = 100,
    filter_regex: str | None = None,
) -> dict[str, Any]:
    """Return recent journalctl output for `service`.

    `filter_regex`: Python `re` pattern; only lines matching are returned.
    The full unfiltered line count is also reported so callers can tell
    whether their filter is overly narrow.
    """
    cmd = [
        "journalctl",
        "-u", service,
        "-n", str(lines),
        "--no-pager",
        "-o", "short",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(
            f"journalctl -u {service} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )

    all_lines = proc.stdout.splitlines()
    if filter_regex:
        try:
            pattern = re.compile(filter_regex)
        except re.error as exc:
            raise ValueError(f"Invalid filter regex {filter_regex!r}: {exc}") from exc
        matched = [ln for ln in all_lines if pattern.search(ln)]
    else:
        matched = all_lines

    return {
        "service": service,
        "lines_read": len(all_lines),
        "lines_matched": len(matched),
        "lines": matched,
    }
