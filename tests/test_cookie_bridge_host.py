"""Cookie bridge native-messaging host: framing, filtering, and safe writes.

The browser extension sends the browser's current Google cookies; the host writes
them to the cookie file the MCP server reads. It must keep only Gemini-session
domains, refuse sessions that are not signed in or come from the wrong browser,
replace the file atomically, and never echo a cookie value.
"""
import importlib.util
import io
import json
import os
import struct
import time
from pathlib import Path

import pytest

from gemini_webapi_mcp import server

_HOST_PATH = Path(__file__).resolve().parents[1] / "cookie_bridge" / "native_host.py"
_spec = importlib.util.spec_from_file_location("native_host", _HOST_PATH)
host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host)

FUTURE = time.time() + 86400
PAST = time.time() - 86400


def _session(extra=()):
    return [
        {"domain": ".google.com", "name": "__Secure-1PSID", "value": "psid-secret", "path": "/", "expirationDate": FUTURE},
        {"domain": ".google.com", "name": "SID", "value": "sid-secret", "path": "/"},
        {"domain": "gemini.google.com", "name": "OSID", "value": "osid-secret", "path": "/"},
        *extra,
    ]


def _frame(obj):
    data = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(data)) + data


# --- framing --------------------------------------------------------------

def test_message_framing_round_trips():
    buf = io.BytesIO()
    host.write_message(buf, {"ok": True, "count": 3})
    buf.seek(0)
    assert host.read_message(buf) == {"ok": True, "count": 3}


def test_read_message_on_empty_stream_is_none():
    assert host.read_message(io.BytesIO(b"")) is None


def test_read_message_rejects_oversized_and_truncated():
    with pytest.raises(ValueError):
        host.read_message(io.BytesIO(struct.pack("<I", host.MAX_MESSAGE + 1)))
    with pytest.raises(ValueError):
        host.read_message(io.BytesIO(struct.pack("<I", 50) + b"{}"))


# --- filtering ------------------------------------------------------------

def test_filter_keeps_only_gemini_session_domains():
    cookies = _session([
        {"domain": "accounts.google.com", "name": "LSID", "value": "x"},
        {"domain": ".youtube.com", "name": "VISITOR", "value": "y"},
        {"domain": ".google.com", "name": "EMPTY", "value": ""},
        {"domain": ".google.com", "name": "OLD", "value": "z", "expirationDate": PAST},
        "not-a-dict",
    ])
    kept = host.filter_cookies(cookies)
    assert {(c["domain"], c["name"]) for c in kept} == {
        (".google.com", "__Secure-1PSID"), (".google.com", "SID"), ("gemini.google.com", "OSID"),
    }


@pytest.mark.parametrize("junk", [None, {}, "x", 42])
def test_filter_survives_junk_input(junk):
    assert host.filter_cookies(junk) == []


# --- handling -------------------------------------------------------------

def test_handle_writes_a_file_the_server_can_load(tmp_path):
    target = tmp_path / "cookies.txt"
    reply = host.handle({"type": "cookies", "source": "firefox", "cookies": _session()}, str(target), "any")
    assert reply["ok"] is True and reply["count"] == 3
    jar = server._file_cookie_jar(str(target))
    assert jar.get("__Secure-1PSID", domain=".google.com") == "psid-secret"
    assert jar.get("OSID", domain="gemini.google.com") == "osid-secret"


def test_handle_refuses_a_session_that_is_not_signed_in(tmp_path):
    target = tmp_path / "cookies.txt"
    reply = host.handle({"type": "cookies", "source": "firefox",
                         "cookies": [{"domain": ".google.com", "name": "NID", "value": "n"}]}, str(target), "any")
    assert reply["ok"] is False
    assert not target.exists()


def test_handle_refuses_a_browser_it_was_not_set_to_accept(tmp_path):
    target = tmp_path / "cookies.txt"
    reply = host.handle({"type": "cookies", "source": "chrome", "cookies": _session()}, str(target), "firefox")
    assert reply["ok"] is False and "chrome" in reply["error"]
    assert not target.exists()


def test_handle_rejects_unknown_messages(tmp_path):
    reply = host.handle({"type": "something-else"}, str(tmp_path / "c.txt"), "any")
    assert reply["ok"] is False


def test_unchanged_cookies_do_not_rewrite_the_file(tmp_path):
    target = tmp_path / "cookies.txt"
    host.handle({"type": "cookies", "source": "firefox", "cookies": _session()}, str(target), "any")
    old = time.time() - 100
    os.utime(target, (old, old))
    reply = host.handle({"type": "cookies", "source": "firefox", "cookies": _session()}, str(target), "any")
    assert reply["ok"] is True and reply.get("unchanged") is True
    assert target.stat().st_mtime == pytest.approx(old)


def test_replace_retries_when_the_reader_briefly_holds_the_file(tmp_path, monkeypatch):
    target = tmp_path / "cookies.txt"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(host.os, "replace", flaky)
    monkeypatch.setattr(host.time, "sleep", lambda s: None)
    host.write_cookie_file(str(target), _session())
    assert calls["n"] == 3 and json.loads(target.read_text(encoding="utf-8"))[0]["name"] == "__Secure-1PSID"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


# --- the whole exchange ---------------------------------------------------

def test_run_answers_over_the_protocol_and_logs_no_values(tmp_path):
    target = tmp_path / "cookies.txt"
    log = tmp_path / "bridge.log"
    out = io.BytesIO()
    reply = host.run(io.BytesIO(_frame({"type": "cookies", "source": "firefox", "cookies": _session()})), out,
                     {"target": str(target), "accept_from": "firefox", "log": str(log)})
    assert reply["ok"] is True
    out.seek(0)
    assert host.read_message(out) == reply
    text = log.read_text(encoding="utf-8")
    assert "firefox" in text and "secret" not in text


def test_errors_never_carry_cookie_values(tmp_path):
    out = io.BytesIO()
    reply = host.run(io.BytesIO(_frame({"type": "cookies", "source": "edge", "cookies": _session()})), out,
                     {"target": str(tmp_path / "c.txt"), "accept_from": "firefox", "log": str(tmp_path / "l.log")})
    assert reply["ok"] is False and "secret" not in json.dumps(reply)


def test_store_diagnostics_are_logged_as_counts_only(tmp_path):
    log = tmp_path / "bridge.log"
    diag = [
        {"id": "firefox-default", "n": 3, "psid": False},
        {"id": "firefox-container-2", "n": 21, "psid": True},
        {"id": "evil\nid psid-secret <script>", "n": "x", "psid": "yes"},
        "junk",
    ]
    host.run(io.BytesIO(_frame({"type": "cookies", "source": "firefox", "cookies": [], "diag": diag})), io.BytesIO(),
             {"target": str(tmp_path / "c.txt"), "accept_from": "firefox", "log": str(log)})
    text = log.read_text(encoding="utf-8")
    assert "firefox-default:3:no-psid" in text and "firefox-container-2:21:psid" in text
    assert "secret" not in text and "<script>" not in text and len(text.splitlines()) == 1


def test_run_without_a_config_target_fails_cleanly(tmp_path):
    out = io.BytesIO()
    reply = host.run(io.BytesIO(_frame({"type": "cookies", "source": "firefox", "cookies": _session()})), out, {})
    assert reply["ok"] is False
