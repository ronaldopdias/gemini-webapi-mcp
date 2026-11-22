"""Server side of the cookie bridge: pick up a rewritten cookie file before each
call (in place, so chat sessions survive) and self-heal once on auth failures."""
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


def _write(path, psid):
    path.write_text(json.dumps([
        {"domain": ".google.com", "name": "__Secure-1PSID", "value": psid, "path": "/", "expirationDate": FUTURE},
        {"domain": ".google.com", "name": "SID", "value": "sid", "path": "/"},
    ]), encoding="utf-8")


def _fake_client():
    async def close():
        client.closed = True
    client = SimpleNamespace(cookies=Cookies(), client=SimpleNamespace(cookies=Cookies()), closed=False, close=close)
    return client


def _ctx(client, sessions=None):
    return SimpleNamespace(request_context=SimpleNamespace(
        lifespan_context={"gemini_client": client, "chat_sessions": sessions if sessions is not None else {}}))


@pytest.fixture
def cookie_file(tmp_path, monkeypatch):
    path = tmp_path / "cookies.txt"
    _write(path, "v1")
    monkeypatch.setenv("GEMINI_COOKIES_FILE", str(path))
    monkeypatch.setattr(server, "_cookie_file_mtime_applied", None)
    return path


def _bump(path, psid):
    mtime = path.stat().st_mtime
    _write(path, psid)
    os.utime(path, (mtime + 10, mtime + 10))


# --- in-place refresh -----------------------------------------------------

def test_current_client_applies_a_changed_cookie_file_in_place(cookie_file):
    client = _fake_client()
    ctx = _ctx(client)
    assert asyncio.run(server._current_client(ctx)) is client
    assert client.cookies.get("__Secure-1PSID", domain=".google.com") == "v1"
    assert client.client.cookies.get("__Secure-1PSID", domain=".google.com") == "v1"

    _bump(cookie_file, "v2")
    asyncio.run(server._current_client(ctx))
    assert client.cookies.get("__Secure-1PSID", domain=".google.com") == "v2"
    assert client.client.cookies.get("__Secure-1PSID", domain=".google.com") == "v2"


def test_unchanged_file_is_not_reread(cookie_file, monkeypatch):
    reads = {"n": 0}
    real = server._file_cookie_jar

    def counting(path):
        reads["n"] += 1
        return real(path)

    monkeypatch.setattr(server, "_file_cookie_jar", counting)
    ctx = _ctx(_fake_client())
    asyncio.run(server._current_client(ctx))
    asyncio.run(server._current_client(ctx))
    assert reads["n"] == 1


def test_a_bad_cookie_file_leaves_the_client_alone_and_is_retried(cookie_file):
    client = _fake_client()
    ctx = _ctx(client)
    asyncio.run(server._current_client(ctx))
    cookie_file.write_text("not a cookie file", encoding="utf-8")
    os.utime(cookie_file, (time.time() + 20, time.time() + 20))
    asyncio.run(server._current_client(ctx))          # must not raise
    assert client.cookies.get("__Secure-1PSID", domain=".google.com") == "v1"
    assert server._cookie_file_mtime_applied != cookie_file.stat().st_mtime


def test_no_cookie_file_configured_means_no_refresh(monkeypatch):
    monkeypatch.delenv("GEMINI_COOKIES_FILE", raising=False)
    client = _fake_client()
    assert asyncio.run(server._current_client(_ctx(client))) is client
    assert not list(client.cookies.jar)


# --- self-heal ------------------------------------------------------------

@pytest.mark.parametrize("exc, expected", [
    (AuthError("expired"), True),
    (ImageGenerationBlocked("Are you signed in?"), True),
    (Exception("Batch execution failed with status code 400"), True),
    (ValueError("boom"), False),
])
def test_auth_failure_classification(exc, expected):
    assert server._is_auth_failure(exc) is expected


def _counting_rebuild(monkeypatch):
    calls = {"n": 0}

    async def fake_rebuild(ctx):
        calls["n"] += 1

    monkeypatch.setattr(server, "_rebuild_client", fake_rebuild)
    return calls


def test_auth_failure_rebuilds_once_and_retries(monkeypatch):
    calls = _counting_rebuild(monkeypatch)
    attempts = {"n": 0}

    async def call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ImageGenerationBlocked("Are you signed in?")
        return "ok"

    assert asyncio.run(server._with_auth_retry(_ctx(_fake_client()), call)) == "ok"
    assert calls["n"] == 1 and attempts["n"] == 2


def test_other_errors_are_reported_without_rebuilding(monkeypatch):
    calls = _counting_rebuild(monkeypatch)

    async def call():
        raise ValueError("boom")

    result = asyncio.run(server._with_auth_retry(_ctx(_fake_client()), call))
    assert isinstance(result, str) and calls["n"] == 0


def test_a_second_auth_failure_is_reported_not_looped(monkeypatch):
    calls = _counting_rebuild(monkeypatch)

    async def call():
        raise AuthError("still expired")

    result = asyncio.run(server._with_auth_retry(_ctx(_fake_client()), call))
    assert isinstance(result, str) and calls["n"] == 1


def test_rebuild_swaps_the_client_and_rebinds_open_chats(monkeypatch):
    old, new = _fake_client(), _fake_client()

    async def fake_new_client():
        return new

    monkeypatch.setattr(server, "_new_client", fake_new_client)
    chat = SimpleNamespace(geminiclient=old)
    ctx = _ctx(old, {"abc": chat})
    asyncio.run(server._rebuild_client(ctx))
    assert ctx.request_context.lifespan_context["gemini_client"] is new
    assert chat.geminiclient is new and old.closed is True


def test_a_failed_rebuild_keeps_the_old_client(monkeypatch):
    old = _fake_client()

    async def failing():
        raise AuthError("no cookies")

    monkeypatch.setattr(server, "_new_client", failing)
    ctx = _ctx(old)
    with pytest.raises(AuthError):
        asyncio.run(server._rebuild_client(ctx))
    assert ctx.request_context.lifespan_context["gemini_client"] is old and old.closed is False
