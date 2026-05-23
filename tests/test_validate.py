"""Tests for the function-node JS syntax checker."""

from __future__ import annotations

import shutil

import pytest

from pairflow.validate import validate_function

node_required = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node binary not on PATH",
)


@node_required
def test_valid_simple_return():
    r = validate_function("return msg;")
    assert r.ok
    assert r.error is None


@node_required
def test_valid_with_await():
    r = validate_function("const x = await Promise.resolve(1);\nmsg.payload = x;\nreturn msg;")
    assert r.ok


@node_required
def test_invalid_unterminated_brace():
    r = validate_function("if (msg.payload) {\nreturn msg;")
    assert not r.ok
    assert r.error
    # Error message should not leak the temp file path.
    assert "/tmp/" not in r.error


@node_required
def test_invalid_typo():
    r = validate_function("rturn msg;")
    assert not r.ok


@node_required
def test_truthiness_via_bool():
    assert bool(validate_function("return msg;")) is True
    assert bool(validate_function("} bad {")) is False
