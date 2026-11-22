"""Session keeper: keep the Gemini session fresh from a dedicated, headless Chrome.

A browser extension only works while its browser is open, and a cookie file
exported once goes stale within minutes (the short-lived rotation token expires,
then Gemini refuses images while chat still works). This keeper owns its own
browser profile, separate from your everyday browsers, signed in once to the
Google account the server should use. On each run it starts that profile
headless, opens Gemini so Google rotates the session cookies, reads them through
Playwright's automation pipe (no debugging port is opened), and writes
GEMINI_COOKIES_FILE through the same checks as the native host (Gemini domains
only, signed-in sessions only, atomic, unchanged sets skipped). Run on a schedule,
it keeps the server signed in with every browser closed.

    python cookie_bridge/session_keeper.py login           one-time: opens a normal window; you sign in
    python cookie_bridge/session_keeper.py refresh         what the schedule runs (headless)
    python cookie_bridge/session_keeper.py install-task    every 20 min, windowless, no admin
    python cookie_bridge/session_keeper.py uninstall-task

Settings come from arguments, then build/keeper_config.json (written by
install-task), then GEMINI_SESSION_PROFILE / GEMINI_COOKIES_FILE /
GEMINI_SESSION_BROWSER. No cookie value is ever printed or logged.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import native_host  # noqa: E402  (same folder)

TASK_NAME = "Gemini Session Keeper"
CONFIG_PATH = HERE / "build" / "keeper_config.json"
LOG_PATH = HERE / "build" / "keeper.log"
GEMINI_URL = "https://gemini.google.com/app"
LOGIN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp"
STALE_LOCK_SECONDS = 600
DEFAULT_PROFILE = str(Path.home() / ".gemini-session" / "chrome-profile")

_PF = os.environ.get("ProgramFiles", r"C:\Program Files")
_PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
_LAD = os.environ.get("LOCALAPPDATA", "")
EXECUTABLES = {
    "chrome": [rf"{_PF}\Google\Chrome\Application\chrome.exe",
               rf"{_PF86}\Google\Chrome\Application\chrome.exe",
               rf"{_LAD}\Google\Chrome\Application\chrome.exe"],
    "chrome-dev": [rf"{_PF}\Google\Chrome Dev\Application\chrome.exe",
                   rf"{_PF86}\Google\Chrome Dev\Application\chrome.exe",
                   rf"{_LAD}\Google\Chrome Dev\Application\chrome.exe"],
    "msedge": [rf"{_PF86}\Microsoft\Edge\Application\msedge.exe",
               rf"{_PF}\Microsoft\Edge\Application\msedge.exe"],
}


# --- pure helpers ---------------------------------------------------------

def browser_executable(channel: str) -> str:
    if channel not in EXECUTABLES:
        raise ValueError(f"unknown browser {channel!r}; use one of {sorted(EXECUTABLES)}")
    for path in EXECUTABLES[channel]:
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(f"{channel} is not installed in the usual places")


def to_rows(cookies) -> list:
    """Map Playwright's context.cookies() to the bridge's cookie rows (filtered)."""
    rows = []
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        row = {"domain": c.get("domain"), "name": c.get("name"), "value": c.get("value"),
               "path": c.get("path") or "/"}
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:          # -1 = session cookie
            row["expirationDate"] = exp
        rows.append(row)
    return native_host.filter_cookies(rows)


def write_rows(target: str, rows: list) -> dict:
    """Write through the native host's checks; the reply never carries values."""
    return native_host.handle({"type": "cookies", "source": "keeper", "cookies": rows}, target, "any")


def task_command(pythonw: str, every: int) -> list:
    if not 5 <= int(every) <= 720:
        raise ValueError("interval must be 5–720 minutes")
    tr = f'"{pythonw}" "{Path(__file__).resolve()}" refresh'
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", str(int(every)), "/TR", tr, "/F"]


def save_config(path, profile: str, target: str, channel: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"profile": profile, "target": target, "channel": channel}, indent=2),
                          encoding="utf-8")


def load_config(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve(profile, target, channel, config: dict, env) -> tuple:
    """Argument, then saved config, then environment, then default."""
    return (
        profile or config.get("profile") or env.get("GEMINI_SESSION_PROFILE") or DEFAULT_PROFILE,
        target or config.get("target") or env.get("GEMINI_COOKIES_FILE"),
        channel or config.get("channel") or env.get("GEMINI_SESSION_BROWSER") or "chrome",
    )


def acquire_lock(path) -> bool:
    """One keeper run at a time; a lock older than STALE_LOCK_SECONDS is taken over."""
    path = Path(path)
    try:
        if path.exists() and time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS:
            path.unlink()
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release_lock(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


# --- browser work ---------------------------------------------------------

async def refresh(profile: str, target: str, channel: str = "chrome", headless: bool = True,
                  settle_ms: int = 5000, timeout_ms: int = 45000) -> dict:
    """Open Gemini in the keeper profile, then write its Google cookies to `target`."""
    from playwright.async_api import async_playwright

    exe = browser_executable(channel)
    Path(profile).mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            profile,
            executable_path=exe,
            headless=headless,
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check", "--disk-cache-size=10485760"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if headless:  # present as ordinary Chrome, not "HeadlessChrome"
                ua = await page.evaluate("navigator.userAgent")
                if "HeadlessChrome" in ua:
                    cdp = await ctx.new_cdp_session(page)
                    await cdp.send("Network.setUserAgentOverride", {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
            await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(settle_ms)  # let Google rotate the session cookies
            cookies = await ctx.cookies()
        finally:
            await ctx.close()
    reply = write_rows(target, to_rows(cookies))
    if not reply["ok"] and "signed-in" in reply.get("error", ""):
        reply["error"] = "the keeper profile is not signed in to Google; run: session_keeper.py login"
    return reply


def login(profile: str, channel: str) -> None:
    """Open the keeper profile in a normal (not automated) window to sign in once."""
    exe = browser_executable(channel)
    Path(profile).mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([exe, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", LOGIN_URL],
                     close_fds=True, creationflags=flags)


# --- command line ---------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Keep the Gemini session fresh from a dedicated headless browser.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("login", "refresh", "install-task"):
        sp = sub.add_parser(name)
        sp.add_argument("--profile")
        sp.add_argument("--target")
        sp.add_argument("--channel", choices=sorted(EXECUTABLES))
        if name == "refresh":
            sp.add_argument("--headed", action="store_true", help="show the window (debugging)")
        if name == "install-task":
            sp.add_argument("--every", type=int, default=20, help="minutes between runs (default 20)")
    sub.add_parser("uninstall-task")
    args = parser.parse_args(argv)

    if args.cmd == "uninstall-task":
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
        print("removed" if r.returncode == 0 else f"not removed: {r.stderr.strip()}")
        return r.returncode

    profile, target, channel = resolve(args.profile, args.target, args.channel, load_config(CONFIG_PATH), os.environ)

    if args.cmd == "login":
        login(profile, channel)
        print(f"Opened {channel} with the keeper profile ({profile}). Sign in, then close that window.")
        return 0

    if not target:
        parser.error("no --target, saved config or GEMINI_COOKIES_FILE")

    if args.cmd == "install-task":
        save_config(CONFIG_PATH, profile, target, channel)
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        r = subprocess.run(task_command(str(pythonw if pythonw.is_file() else sys.executable), args.every),
                           capture_output=True, text=True)
        print(f"scheduled every {args.every} min" if r.returncode == 0 else f"failed: {r.stderr.strip()}")
        return r.returncode

    lock = Path(profile).parent / "keeper.lock"
    if not acquire_lock(lock):
        _log("skipped: another keeper run is in progress")
        print(json.dumps({"ok": False, "error": "another keeper run is in progress"}))
        return 1
    try:
        reply = asyncio.run(refresh(profile, target, channel, headless=not args.headed))
    except Exception as exc:
        reply = {"ok": False, "error": f"could not run the keeper browser ({type(exc).__name__}); "
                                       "if the sign-in window is still open, close it"}
    finally:
        release_lock(lock)
    _log(f"ok={reply['ok']} " + (f"count={reply['count']}{' unchanged' if reply.get('unchanged') else ''}"
                                 if reply["ok"] else f"error={reply['error']}"))
    print(json.dumps(reply))
    return 0 if reply["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
