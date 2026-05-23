"""Syntax validation for Node-RED function-node JavaScript bodies.

A function node's `func` field is a fragment of JavaScript: bare statements
that NR wraps internally inside an async function so that `return msg;` and
`await ...` work. To validate it standalone we wrap it the same way and ask
the Node.js binary to syntax-check the result.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def validate_function(code: str, *, timeout: float = 5.0) -> ValidationResult:
    """Check that `code` is syntactically valid as a function-node body.

    Returns ValidationResult(ok=True) on success or ValidationResult(ok=False,
    error=...) with the node --check error message on failure.

    If the node binary is not on PATH, returns ok=True with a warning in
    `error` — better to admit ignorance than to refuse the write.
    """
    wrapped = (
        "module.exports = async function (msg, context, flow, global, node) {\n"
        f"{code}\n"
        "};\n"
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(wrapped)
        tf_path = tf.name

    try:
        try:
            result = subprocess.run(
                ["node", "--check", tf_path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return ValidationResult(
                ok=True,
                error="node binary not found on PATH; syntax check skipped",
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(ok=False, error="node --check timed out")

        if result.returncode == 0:
            return ValidationResult(ok=True)

        err = (result.stderr or result.stdout).strip()
        # Strip the temporary file path from error messages so they read
        # cleanly without leaking implementation detail.
        err = err.replace(tf_path, "<function>")
        return ValidationResult(ok=False, error=err)
    finally:
        try:
            os.unlink(tf_path)
        except OSError:
            pass
