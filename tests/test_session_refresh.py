"""Keeping a long-running server's Gemini session usable.

Regression (2026-11-22): gemini_webapi's background RotateCookies loop failed every
interval for a live-browser session ("AuthError: Failed to refresh cookies"), the
client's __Secure-1PSIDTS aged while the browser rotated its own, and after a few
hours Gemini answered image requests with "Are you signed in? ... not available
in your location" (ImageGenerationBlocked). gemini_reset fixed it every time,
because it re-reads the browser. These tests pin the automatic version of that.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gemini_webapi_mcp import server as S  # noqa: E402
from gemini_webapi import APIError, AuthError, ImageGenerationBlocked  # noqa: E402


class FakeClient:
    def __init__(self, name, fail_with=None):
        self.name = name
        self.fail_with = fail_with
        self.calls = 0
        self.closed = False

    async def generate_content(self, prompt, **kwargs):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        return f"{self.name}: {prompt}"

    async def close(self):
        self.closed = True


def _ctx(client, sessions=None):
    state = {"gemini_client": client, "chat_sessions": sessions if sessions is not None else {}}
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    # One event loop per test: give each its own lock and a clean session record.
    monkeypatch.setattr(S, "_refresh_lock", asyncio.Lock())
    monkeypatch.setattr(S, "_session", {"source": "browser", "psid": "p", "psidts": "t1", "checked_at": 0.0})
    yield


def _stub_new_client(monkeypatch, *clients):
    made = []
    queue = list(clients)

    async def fake_new_client():
        made.append(queue[0] if len(queue) == 1 else queue.pop(0))
        return made[-1]

    monkeypatch.setattr(S, "_new_client", fake_new_client)
    return made


def test_stale_session_error_reloads_and_retries_once(monkeypatch):
    old = FakeClient("old", fail_with=ImageGenerationBlocked("Are you signed in?"))
    new = FakeClient("new")
    made = _stub_new_client(monkeypatch, new)
    chat = SimpleNamespace(geminiclient=old)
    ctx = _ctx(old, {"abc": chat})
    monkeypatch.setattr(S, "SESSION_CHECK_INTERVAL", 0)  # no browser check here

    result = asyncio.run(S._with_session_retry(ctx, lambda c: c.generate_content("hi")))

    assert result == "new: hi"
    assert made == [new]
    assert ctx.request_context.lifespan_context["gemini_client"] is new
    assert old.closed and not new.closed
    assert chat.geminiclient is new, "open chat sessions move to the reloaded client"


def test_auth_error_counts_as_stale(monkeypatch):
    old = FakeClient("old", fail_with=AuthError("expired"))
    _stub_new_client(monkeypatch, FakeClient("new"))
    monkeypatch.setattr(S, "SESSION_CHECK_INTERVAL", 0)

    assert asyncio.run(S._with_session_retry(_ctx(old), lambda c: c.generate_content("x"))) == "new: x"


def test_other_errors_are_not_retried(monkeypatch):
    old = FakeClient("old", fail_with=APIError("boom"))
    made = _stub_new_client(monkeypatch, FakeClient("new"))
    monkeypatch.setattr(S, "SESSION_CHECK_INTERVAL", 0)

    with pytest.raises(APIError):
        asyncio.run(S._with_session_retry(_ctx(old), lambda c: c.generate_content("x")))
    assert made == []


def test_retry_happens_only_once(monkeypatch):
    blocked = ImageGenerationBlocked("not available in your location")
    old = FakeClient("old", fail_with=blocked)
    new = FakeClient("new", fail_with=blocked)
    made = _stub_new_client(monkeypatch, new)
    monkeypatch.setattr(S, "SESSION_CHECK_INTERVAL", 0)

    with pytest.raises(ImageGenerationBlocked):
        asyncio.run(S._with_session_retry(_ctx(old), lambda c: c.generate_content("x")))
    assert made == [new] and new.calls == 1


def test_concurrent_stale_calls_reload_once(monkeypatch):
    old = FakeClient("old", fail_with=ImageGenerationBlocked("Are you signed in?"))
    new = FakeClient("new")
    made = _stub_new_client(monkeypatch, new)
    monkeypatch.setattr(S, "SESSION_CHECK_INTERVAL", 0)
    ctx = _ctx(old)

    async def both():
        return await asyncio.gather(
            S._with_session_retry(ctx, lambda c: c.generate_content("a")),
            S._with_session_retry(ctx, lambda c: c.generate_content("b")),
        )

    assert asyncio.run(both()) == ["new: a", "new: b"]
    assert made == [new], "the second caller reuses the client the first one reloaded"


def test_failed_reload_keeps_the_working_client(monkeypatch):
    old = FakeClient("old")

    async def broken_new_client():
        raise RuntimeError("browser unreadable")

    monkeypatch.setattr(S, "_new_client", broken_new_client)
    ctx = _ctx(old)

    with pytest.raises(RuntimeError):
        asyncio.run(S._refresh_client(ctx, reason="test"))
    assert ctx.request_context.lifespan_context["gemini_client"] is old and not old.closed


def test_newer_browser_token_reloads_session(monkeypatch):
    old, new = FakeClient("old"), FakeClient("new")
    made = _stub_new_client(monkeypatch, new)
    reads = []
    monkeypatch.setattr(S, "_browser_session_cookies",
                        lambda quiet=False: reads.append(quiet) or ("chrome", "p", "t2"))
    ctx = _ctx(old)

    asyncio.run(S._ensure_fresh_session(ctx))

    assert reads == [True], "the periodic check reads the browser quietly"
    assert made == [new]
    assert ctx.request_context.lifespan_context["gemini_client"] is new


def test_same_browser_token_keeps_session(monkeypatch):
    made = _stub_new_client(monkeypatch, FakeClient("new"))
    monkeypatch.setattr(S, "_browser_session_cookies", lambda quiet=False: ("chrome", "p", "t1"))

    asyncio.run(S._ensure_fresh_session(_ctx(FakeClient("old"))))

    assert made == []


def test_browser_check_is_throttled(monkeypatch):
    reads = []
    monkeypatch.setattr(S, "_browser_session_cookies",
                        lambda quiet=False: reads.append(1) or ("chrome", "p", "t1"))
    monkeypatch.setattr(S, "SESSION_CHECK_INTERVAL", 60)
    ctx = _ctx(FakeClient("old"))

    asyncio.run(S._ensure_fresh_session(ctx))
    asyncio.run(S._ensure_fresh_session(ctx))

    assert reads == [1], "a second call within the interval doesn't read the browser again"


@pytest.mark.parametrize("source", ["file", "env"])
def test_frozen_sessions_skip_the_browser_check(monkeypatch, source):
    S._session["source"] = source
    reads = []
    monkeypatch.setattr(S, "_browser_session_cookies", lambda quiet=False: reads.append(1))

    asyncio.run(S._ensure_fresh_session(_ctx(FakeClient("old"))))

    assert reads == []


class _InitRecorder:
    last = None

    def __init__(self, **kwargs):
        self.cookies = None
        _InitRecorder.last = self

    async def init(self, **kwargs):
        self.init_kwargs = kwargs


@pytest.mark.parametrize("env,auto_refresh", [({}, False), ({"GEMINI_PSID": "x"}, True)])
def test_library_rotation_only_for_frozen_sessions(monkeypatch, env, auto_refresh):
    import gemini_webapi

    for var in ("GEMINI_COOKIES_FILE", "GEMINI_PSID"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(gemini_webapi, "GeminiClient", _InitRecorder)
    monkeypatch.setattr(S, "_browser_cookie_jar", lambda: None)
    monkeypatch.setattr(S, "_resolve_cookies", lambda: ("p", "t9"))
    monkeypatch.setattr(S, "_patch_client", lambda client: None)

    asyncio.run(S._new_client())

    assert _InitRecorder.last.init_kwargs["auto_refresh"] is auto_refresh
    assert S._session["psidts"] == "t9"
    assert S._session["source"] == ("env" if env else "browser")


def test_chat_model_default_and_override(monkeypatch):
    monkeypatch.delenv("GEMINI_CHAT_MODEL", raising=False)
    assert S._chat_model_from_env() == "gemini-3.0-flash-thinking"
    monkeypatch.setenv("GEMINI_CHAT_MODEL", " gemini-3.0-pro ")
    assert S._chat_model_from_env() == "gemini-3.0-pro"


def test_image_parse_patch_does_not_stack():
    """Every reload re-runs _patch_client; the image-parse hook must keep wrapping
    the library's original, not the previous hook (one extra layer per reload
    would end in RecursionError on a long-running server)."""
    import gemini_webapi.client as gwc

    async def request(method, url, **kwargs):
        return None

    S._patch_client(SimpleNamespace(client=SimpleNamespace(request=request)))
    original = gwc._mcp_original_parse_generated_images
    first = gwc._parse_generated_images
    S._patch_client(SimpleNamespace(client=SimpleNamespace(request=request)))

    assert gwc._mcp_original_parse_generated_images is original
    assert gwc._parse_generated_images is not first
    assert gwc._parse_generated_images.__closure__ is not None
    wrapped = [c.cell_contents for c in gwc._parse_generated_images.__closure__]
    assert original in wrapped and first not in wrapped
