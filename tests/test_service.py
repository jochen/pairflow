"""Tests for the systemctl + journalctl wrappers (subprocess mocked)."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from pairflow import service


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- restart ---------------------------------------------------------------- #


def test_restart_success_without_admin_url():
    with patch("pairflow.service.subprocess.run", return_value=_proc(0)) as m:
        r = service.restart("nodered")
    assert r["restarted"] is True
    assert r["service"] == "nodered"
    assert "ready" not in r  # no admin_url, no poll
    args = m.call_args.args[0]
    assert args[:2] == ["sudo", "-n"]
    assert args[-3:] == ["systemctl", "restart", "nodered"]


def test_restart_uses_no_sudo_when_requested():
    with patch("pairflow.service.subprocess.run", return_value=_proc(0)) as m:
        service.restart("nodered", use_sudo=False)
    args = m.call_args.args[0]
    assert args[0] == "systemctl"


def test_restart_nopasswd_hint_in_error():
    with patch(
        "pairflow.service.subprocess.run",
        return_value=_proc(1, stderr="sudo: a password is required"),
    ):
        with pytest.raises(RuntimeError, match="NOPASSWD"):
            service.restart("nodered")


def test_restart_polls_admin_when_url_given():
    """admin_url polling: first attempts fail, then succeed."""
    proc_ok = _proc(0)

    class FakeResp:
        def __init__(self, status: int):
            self.status_code = status

    call_count = {"n": 0}

    def fake_get(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            import httpx
            raise httpx.ConnectError("not yet")
        return FakeResp(200)

    with patch("pairflow.service.subprocess.run", return_value=proc_ok), \
         patch("pairflow.service.httpx.get", side_effect=fake_get), \
         patch("pairflow.service.time.sleep"):
        r = service.restart("nodered", admin_url="http://localhost:1880")
    assert r["ready"] is True


def test_restart_admin_poll_timeout():
    import httpx
    proc_ok = _proc(0)
    with patch("pairflow.service.subprocess.run", return_value=proc_ok), \
         patch("pairflow.service.httpx.get", side_effect=httpx.ConnectError("nope")), \
         patch("pairflow.service.time.sleep"):
        r = service.restart("nodered", admin_url="http://localhost:1880", wait_timeout=0.01)
    assert r["ready"] is False
    assert "last_error" in r


# --- journal ---------------------------------------------------------------- #


def test_journal_basic():
    sample = "May 23 12:00:00 host node-red[1]: line A\nMay 23 12:00:01 host node-red[1]: line B"
    with patch("pairflow.service.subprocess.run", return_value=_proc(0, stdout=sample)):
        r = service.journal("nodered", lines=10)
    assert r["lines_read"] == 2
    assert r["lines_matched"] == 2
    assert r["lines"][0].endswith("line A")


def test_journal_filter_matches_subset():
    sample = "info: ok\nerror: bad\ninfo: also ok"
    with patch("pairflow.service.subprocess.run", return_value=_proc(0, stdout=sample)):
        r = service.journal("nodered", lines=10, filter_regex=r"error")
    assert r["lines_read"] == 3
    assert r["lines_matched"] == 1
    assert "bad" in r["lines"][0]


def test_journal_filter_regex_must_be_valid():
    with patch("pairflow.service.subprocess.run", return_value=_proc(0, stdout="a")):
        with pytest.raises(ValueError, match="Invalid filter regex"):
            service.journal("nodered", filter_regex="[unclosed")


def test_journal_command_failure():
    with patch(
        "pairflow.service.subprocess.run",
        return_value=_proc(1, stderr="No such service"),
    ):
        with pytest.raises(RuntimeError, match="journalctl"):
            service.journal("does-not-exist")


def test_journal_truncates_over_max_bytes_keeps_newest():
    """Over the cap, oldest lines drop, newest are retained (= the
    'what just happened' end of the journal is what callers want)."""
    sample = "\n".join(f"line {i:03d} " + "x" * 50 for i in range(40))
    with patch("pairflow.service.subprocess.run", return_value=_proc(0, stdout=sample)):
        r = service.journal("nodered", lines=40, max_bytes=500)
    assert r["lines_read"] == 40
    assert r["lines_matched"] == 40
    assert r["output_truncated"] is True
    assert r["output_full_bytes"] > 500
    joined = "\n".join(r["lines"])
    assert len(joined) <= 500
    # Newest line preserved, oldest dropped
    assert "line 039" in r["lines"][-1]
    assert "line 000" not in joined


def test_journal_no_truncation_when_under_cap():
    sample = "short line A\nshort line B"
    with patch("pairflow.service.subprocess.run", return_value=_proc(0, stdout=sample)):
        r = service.journal("nodered", lines=10, max_bytes=8000)
    assert "output_truncated" not in r
    assert "output_full_bytes" not in r
    assert len(r["lines"]) == 2


def test_journal_max_bytes_zero_disables_cap():
    sample = "\n".join(f"line {i:03d} " + "x" * 50 for i in range(40))
    with patch("pairflow.service.subprocess.run", return_value=_proc(0, stdout=sample)):
        r = service.journal("nodered", lines=40, max_bytes=0)
    assert "output_truncated" not in r
    assert len(r["lines"]) == 40
