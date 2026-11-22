"""Cookie-file sessions that are kept fresh from outside (session keeper, cookie bridge).

The rewritten file is merged into the live client in place before each call, so
open chats survive; a reload first lets the keeper rotate the cookies; and a
keeper-fed file turns the library's own rotation off. Retry-once, keep-the-old-
client and move-the-chats are pinned in test_session_refresh.py.
"""
import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from curl_cffi.requests import Cookies
from gemini_webapi.exceptions import AuthError, ImageGenerationBlocked

from gemini_webapi_mcp import server

FUTURE = time.time() + 86400


def _write(path, psid, psidts="ts"):
    path.write_text(json.dumps([
        {"domain": ".google.com", "name": "__Secure-1PSID", "value": psid, "path": "/", "expirationDate": FUTURE},
        {"domain": ".google.com", "name": "__Secure-1PSIDTS", "value": psidts, "path": "/", "expirationDate": FUTURE},
        {"domain": ".google.com", "name": "SID", "value": "sid", "path": "/"},
    ]), encoding="utf-8")


def _bump(path, psid):
    mtime = path.stat().st_mtime
    _write(path, psid)
    os.utime(path, (mtime + 10, mtime + 10))


def _fake_client():
    async def close():
        client.closed = True
    client = SimpleNamespace(cookies=Cookies(), client=SimpleNamespace(cookies=Cookies()), closed=False, close=close)
    return client


def _ctx(client, sessions=None):
    return SimpleNamespace(request_context=SimpleNamespace(
        lifespan_context={"gemini_client": client, "chat_sessions": sessions if sessions is not None else {}}))


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(server, "_refresh_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_session", {"source": "file", "psid": "", "psidts": "", "checked_at": 0.0,
                                             "file_mtime": None})


@pytest.fixture
def cookie_file(tmp_path, monkeypatch):
    path = tmp_path / "cookies.txt"
    _write(path, "v1")
    monkeypatch.setenv("GEMINI_COOKIES_FILE", str(path))
    return path


# --- in-place pickup of a rewritten file ----------------------------------

def test_a_rewritten_cookie_file_is_merged_in_place(cookie_file):
    client = _fake_client()
    ctx = _ctx(client)
    asyncio.run(server._ensure_fresh_session(ctx))
    assert client.cookies.get("__Secure-1PSID", domain=".google.com") == "v1"
    assert client.client.cookies.get("__Secure-1PSID", domain=".google.com") == "v1"

    _bump(cookie_file, "v2")
    asyncio.run(server._ensure_fresh_session(ctx))
    assert ctx.request_context.lifespan_context["gemini_client"] is client      # same client, chats survive
    assert client.cookies.get("__Secure-1PSID", domain=".google.com") == "v2"
    assert client.client.cookies.get("__Secure-1PSID", domain=".google.com") == "v2"
    assert server._session["psid"] == "v2"


def test_an_unchanged_file_is_not_reread(cookie_file, monkeypatch):
    reads = {"n": 0}
    real = server._file_cookie_jar

    def counting(path):
        reads["n"] += 1
        return real(path)

    monkeypatch.setattr(server, "_file_cookie_jar", counting)
    ctx = _ctx(_fake_client())
    asyncio.run(server._ensure_fresh_session(ctx))
    asyncio.run(server._ensure_fresh_session(ctx))
    assert reads["n"] == 1


def test_a_bad_cookie_file_leaves_the_client_alone_and_is_retried(cookie_file):
    client = _fake_client()
    ctx = _ctx(client)
    asyncio.run(server._ensure_fresh_session(ctx))
    cookie_file.write_text("not a cookie file", encoding="utf-8")
    os.utime(cookie_file, (time.time() + 20, time.time() + 20))
    asyncio.run(server._ensure_fresh_session(ctx))          # must not raise
    assert client.cookies.get("__Secure-1PSID", domain=".google.com") == "v1"
    assert server._session["file_mtime"] != cookie_file.stat().st_mtime


def test_other_sources_never_read_the_cookie_file(cookie_file, monkeypatch):
    monkeypatch.setitem(server._session, "source", "env")
    monkeypatch.setattr(server, "_file_cookie_jar", lambda p: pytest.fail("file read for an env session"))
    asyncio.run(server._ensure_fresh_session(_ctx(_fake_client())))


# --- stale-session classification -----------------------------------------

@pytest.mark.parametrize("exc, expected", [
    (AuthError("expired"), True),
    (ImageGenerationBlocked("blocked"), True),
    (Exception("Batch execution failed with status code 400"), True),
    (Exception("Are you signed in? I can search for images"), True),
    (ValueError("boom"), False),
])
def test_stale_session_classification(exc, expected):
    assert server._is_stale_session_error(exc) is expected


# --- the keeper runs before a reload --------------------------------------

def _record_reload(monkeypatch, new):
    order = []

    async def keeper():
        order.append("keeper")
        return True

    async def new_client():
        order.append("new_client")
        return new

    monkeypatch.setattr(server, "_refresh_session_profile", keeper)
    monkeypatch.setattr(server, "_new_client", new_client)
    return order


def test_a_file_session_reload_lets_the_keeper_rotate_first(monkeypatch):
    old, new = _fake_client(), _fake_client()
    order = _record_reload(monkeypatch, new)
    ctx = _ctx(old)
    assert asyncio.run(server._refresh_client(ctx, stale=old, reason="test")) is new
    assert order == ["keeper", "new_client"] and old.closed is True


def test_a_browser_session_reload_skips_the_keeper(monkeypatch):
    monkeypatch.setitem(server._session, "source", "browser")
    order = _record_reload(monkeypatch, _fake_client())
    asyncio.run(server._refresh_client(_ctx(_fake_client()), reason="test"))
    assert order == ["new_client"]


def test_no_keeper_configured_means_no_keeper_run():
    assert asyncio.run(server._refresh_session_profile()) is False


# --- library rotation off for keeper-fed files -----------------------------

class _InitRecorder:
    last = None

    def __init__(self, **kwargs):
        _InitRecorder.last = self

    async def init(self, **kwargs):
        self.init_kwargs = kwargs


@pytest.mark.parametrize("keeper, auto_refresh", [(False, True), (True, False)])
def test_library_rotation_is_off_for_a_keeper_fed_file(cookie_file, monkeypatch, keeper, auto_refresh):
    import gemini_webapi

    monkeypatch.delenv("GEMINI_PSID", raising=False)
    if keeper:
        monkeypatch.setenv("GEMINI_SESSION_PROFILE", r"D:\keeper\profile")
    monkeypatch.setattr(gemini_webapi, "GeminiClient", _InitRecorder)
    monkeypatch.setattr(server, "_patch_client", lambda client: None)
    asyncio.run(server._new_client())
    assert _InitRecorder.last.init_kwargs["auto_refresh"] is auto_refresh
    assert server._session["source"] == "file"
    assert server._session["file_mtime"] == cookie_file.stat().st_mtime
