"""Per-call usage logger for the MCP tool surface.

When enabled in config (`[telemetry] usage_log = true`), every MCP tool call
appends a single JSON line to a log file describing what was called, with
which arguments, how long it took, and how big the response was.

The motivation is empirical: response-size estimates in tool design are
guesses until we see real usage. The log lets us answer questions like
"which tools dominate the token budget?", "which optional knobs are users
actually turning?", and "how often does truncation fire?" from data
instead of intuition.

What the logger records (per call):
  ts             - ISO-8601 UTC timestamp
  tool           - tool function name (e.g. "nr_list_nodes")
  args           - bound argument dict, with large strings/dicts replaced
                   by {_size, _type, _head} so the log doesn't itself blow up
  duration_ms    - wall-clock duration of the tool body
  response_bytes - len(json.dumps(result)) as a token-budget proxy
  response_shape - {type, len|keys}
  truncations    - {flag_name: count} for any *_truncated:True we found
  error          - {type, msg} if the tool raised

Privacy & safety:
  - Argument values >200 chars (configurable) and dicts >800 JSON chars are
    scrubbed to {_size, _type, _head} so MQTT payloads, JS bodies, and
    other blobs don't leak verbatim.
  - Logging failures are swallowed: the tool call is never broken by the
    logger.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

DEFAULT_MAX_ARG_CHARS = 200
DEFAULT_MAX_ARG_DICT_CHARS = 800

# Argument names whose values must NEVER appear verbatim in the log.
# When a tool call carries one of these args, the value is replaced with a
# {_redacted, _keys} summary (dict) or {_redacted} (non-dict) so the log
# never leaks credential payloads.
SENSITIVE_ARG_NAMES: frozenset[str] = frozenset({"credentials"})


def _default_log_path() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "pairflow" / "usage.jsonl"


def _scrub_value(v: Any, max_chars: int, max_dict_chars: int) -> Any:
    """Replace large blob-shaped values with a {_size, _type, _head} summary."""
    if isinstance(v, str):
        if len(v) > max_chars:
            return {"_size": len(v), "_type": "str", "_head": v[:80]}
        return v
    if isinstance(v, bytes):
        if len(v) > max_chars:
            return {"_size": len(v), "_type": "bytes"}
        return {"_size": len(v), "_type": "bytes", "_head": v[:40].hex()}
    if isinstance(v, dict):
        try:
            encoded = json.dumps(v, default=str)
        except Exception:
            return {"_type": "dict", "_keys": list(v.keys())[:10]}
        if len(encoded) > max_dict_chars:
            return {"_size": len(encoded), "_type": "dict",
                    "_keys": list(v.keys())[:10]}
        return v
    if isinstance(v, list):
        if len(v) > 50:
            return {"_size": len(v), "_type": "list"}
        return [_scrub_value(item, max_chars, max_dict_chars) for item in v]
    return v


def _scrub_args(args: dict[str, Any], max_chars: int,
                max_dict_chars: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in args.items():
        if k in SENSITIVE_ARG_NAMES:
            # Never log credential values — replace with a key-only summary.
            if isinstance(v, dict):
                result[k] = {"_redacted": True, "_keys": sorted(v.keys())}
            else:
                result[k] = {"_redacted": True}
        else:
            result[k] = _scrub_value(v, max_chars, max_dict_chars)
    return result


def _find_truncations(obj: Any) -> dict[str, int]:
    """Walk the response object, return {flag_name: count} for *_truncated:True."""
    counter: Counter[str] = Counter()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k.endswith("_truncated") and v is True:
                    counter[k] += 1
                else:
                    walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(obj)
    return dict(counter)


def _response_shape(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    if isinstance(result, list):
        return {"type": "list", "len": len(result)}
    if isinstance(result, dict):
        return {"type": "dict", "keys": len(result)}
    return {"type": type(result).__name__}


def _response_bytes(result: Any) -> int:
    if result is None:
        return 0
    try:
        return len(json.dumps(result, default=str))
    except Exception:
        return -1


@dataclass(frozen=True, slots=True)
class UsageLogger:
    enabled: bool
    path: Path | None
    max_arg_chars: int = DEFAULT_MAX_ARG_CHARS
    max_arg_dict_chars: int = DEFAULT_MAX_ARG_DICT_CHARS

    @classmethod
    def disabled(cls) -> UsageLogger:
        return cls(enabled=False, path=None)

    @classmethod
    def from_config(cls, telemetry: TelemetryConfigLike) -> UsageLogger:
        if not telemetry.usage_log:
            return cls.disabled()
        path = telemetry.usage_log_path or _default_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _log.warning("usage_log disabled: cannot create %s (%s)", path.parent, exc)
            return cls.disabled()
        return cls(enabled=True, path=path)

    def wrap(self, fn: Callable) -> Callable:
        if not self.enabled:
            return fn
        if inspect.iscoroutinefunction(fn):
            return self._wrap_async(fn)
        return self._wrap_sync(fn)

    def _wrap_sync(self, fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        name = fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            err: BaseException | None = None
            result: Any = None
            try:
                result = fn(*args, **kwargs)
                return result
            except BaseException as e:
                err = e
                raise
            finally:
                duration_ms = int((time.monotonic() - t0) * 1000)
                self._record(name, sig, args, kwargs, result, duration_ms, err)

        return wrapper

    def _wrap_async(self, fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        name = fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            err: BaseException | None = None
            result: Any = None
            try:
                result = await fn(*args, **kwargs)
                return result
            except BaseException as e:
                err = e
                raise
            finally:
                duration_ms = int((time.monotonic() - t0) * 1000)
                self._record(name, sig, args, kwargs, result, duration_ms, err)

        return wrapper

    def _record(
        self,
        name: str,
        sig: inspect.Signature,
        args: tuple,
        kwargs: dict,
        result: Any,
        duration_ms: int,
        err: BaseException | None,
    ) -> None:
        try:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            arg_dict = _scrub_args(
                dict(bound.arguments), self.max_arg_chars, self.max_arg_dict_chars,
            )
        except Exception:
            arg_dict = {"_unbindable": True}

        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "tool": name,
            "args": arg_dict,
            "duration_ms": duration_ms,
            "response_bytes": _response_bytes(result),
            "response_shape": _response_shape(result),
            "truncations": _find_truncations(result),
        }
        if err is not None:
            record["error"] = {
                "type": type(err).__name__,
                "msg": str(err)[:200],
            }

        try:
            assert self.path is not None
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            _log.debug("usage_log write failed: %s", exc)


# Avoid an import cycle with config.py: typing-only protocol-style hint.
class TelemetryConfigLike:
    usage_log: bool
    usage_log_path: Path | None
