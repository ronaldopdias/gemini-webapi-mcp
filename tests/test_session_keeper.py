"""Session keeper: a dedicated, headless Chrome profile keeps the Gemini session
fresh even when every everyday browser is closed. Browser launches are not
exercised here; the pure parts (cookie mapping, executable lookup, task command,
run lock, writing through the host's checks) are."""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

from gemini_webapi_mcp import server

_DIR = Path(__file__).resolve().parents[1] / "cookie_bridge"
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location("session_keeper", _DIR / "session_keeper.py")
keeper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keeper)

FUTURE = time.time() + 86400
PAST = time.time() - 86400


def _pw(name, value, domain=".google.com", expires=FUTURE):
    """A cookie as Playwright's context.cookies() returns it."""
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires,
            "httpOnly": True, "secure": True, "sameSite": "None"}


def test_playwright_cookies_map_to_bridge_rows():
    rows = keeper.to_rows([
        _pw("__Secure-1PSID", "psid-secret"),
        _pw("SID", "sid-secret", expires=-1),                    # session cookie
        _pw("OSID", "osid-secret", domain="gemini.google.com"),
        _pw("LSID", "x", domain="accounts.google.com"),
        _pw("OLD", "y", expires=PAST),
    ])
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"__Secure-1PSID", "SID", "OSID"}
    assert by_name["__Secure-1PSID"]["expirationDate"] == pytest.approx(FUTURE)
    assert "expirationDate" not in by_name["SID"]


def test_writing_goes_through_the_hosts_checks(tmp_path):
    target = tmp_path / "cookies.txt"
    reply = keeper.write_rows(str(target), keeper.to_rows([_pw("__Secure-1PSID", "psid-secret"), _pw("SID", "s")]))
    assert reply["ok"] is True and reply["count"] == 2
    assert server._file_cookie_jar(str(target)).get("__Secure-1PSID", domain=".google.com") == "psid-secret"
    assert keeper.write_rows(str(target), keeper.to_rows([_pw("__Secure-1PSID", "psid-secret"), _pw("SID", "s")]))["unchanged"]


def test_a_profile_that_is_not_signed_in_writes_nothing(tmp_path):
    target = tmp_path / "cookies.txt"
    reply = keeper.write_rows(str(target), keeper.to_rows([_pw("NID", "n")]))
    assert reply["ok"] is False and not target.exists()


def test_browser_executable_picks_the_first_installed(monkeypatch):
    seen = []

    def fake_isfile(p):
        seen.append(p)
        return "Chrome Dev" in p

    monkeypatch.setattr(keeper.os.path, "isfile", fake_isfile)
    assert "Chrome Dev" in keeper.browser_executable("chrome-dev")
    monkeypatch.setattr(keeper.os.path, "isfile", lambda p: False)
    with pytest.raises(FileNotFoundError):
        keeper.browser_executable("chrome")
    with pytest.raises(ValueError):
        keeper.browser_executable("netscape")


def test_task_command_runs_windowless_and_fits_the_schtasks_limit():
    cmd = keeper.task_command(r"D:\Projects\gemini-webapi-mcp\.venv\Scripts\pythonw.exe", 20)
    assert cmd[:2] == ["schtasks", "/Create"] and "/F" in cmd
    assert cmd[cmd.index("/TN") + 1] == keeper.TASK_NAME
    assert cmd[cmd.index("/SC") + 1] == "MINUTE" and cmd[cmd.index("/MO") + 1] == "20"
    tr = cmd[cmd.index("/TR") + 1]
    assert tr.startswith('"D:\\Projects\\gemini-webapi-mcp\\.venv\\Scripts\\pythonw.exe" ')
    assert "session_keeper.py" in tr and tr.endswith(" refresh") and len(tr) < 261


def test_task_interval_is_bounded():
    with pytest.raises(ValueError):
        keeper.task_command("pythonw", 0)


def test_settings_precedence_is_argument_then_config_then_environment(tmp_path):
    cfg = tmp_path / "keeper_config.json"
    keeper.save_config(cfg, r"D:\cfg\profile", r"D:\cfg\cookies.txt", "msedge")
    config = keeper.load_config(cfg)
    env = {"GEMINI_SESSION_PROFILE": r"D:\env\profile", "GEMINI_COOKIES_FILE": r"D:\env\cookies.txt"}
    assert keeper.resolve(None, None, None, config, env) == (r"D:\cfg\profile", r"D:\cfg\cookies.txt", "msedge")
    assert keeper.resolve(r"D:\arg\p", None, "chrome", config, env) == (r"D:\arg\p", r"D:\cfg\cookies.txt", "chrome")
    assert keeper.resolve(None, None, None, {}, env) == (r"D:\env\profile", r"D:\env\cookies.txt", "chrome")
    assert keeper.load_config(tmp_path / "missing.json") == {}


def test_run_lock_is_exclusive_and_recovers_from_a_stale_one(tmp_path):
    lock = tmp_path / "keeper.lock"
    assert keeper.acquire_lock(lock) is True
    assert keeper.acquire_lock(lock) is False          # another run holds it
    keeper.release_lock(lock)
    assert keeper.acquire_lock(lock) is True
    old = time.time() - keeper.STALE_LOCK_SECONDS - 5
    os.utime(lock, (old, old))                         # a crashed run left it behind
    assert keeper.acquire_lock(lock) is True
    keeper.release_lock(lock)
    assert not lock.exists()


def test_summaries_never_carry_values(tmp_path):
    reply = keeper.write_rows(str(tmp_path / "c.txt"), keeper.to_rows([_pw("__Secure-1PSID", "psid-secret")]))
    assert "secret" not in json.dumps(reply)
