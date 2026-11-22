"""Native-messaging host for the Gemini cookie bridge.

The browser extension (cookie_bridge/extension) sends the browser's current
Google session cookies whenever they change. This host writes them, atomically,
to the cookie file the MCP server reads (GEMINI_COOKIES_FILE), so the server
always uses the same session as the browser.

Standard library only. It never logs, echoes or returns a cookie value, keeps
only Gemini-session domains, refuses a session that is not signed in, and accepts
only the browser it was configured for (so two browsers signed into different
accounts cannot overwrite each other).

Configuration comes from the JSON file named by GEMINI_BRIDGE_CONFIG, written by
install.py: {"target": <cookie file>, "accept_from": "firefox"|"chrome"|"edge"|"any",
"log": <log file>}.
"""
import json
import os
import struct
import sys
import tempfile
import time

ALLOWED_DOMAINS = frozenset({".google.com", "gemini.google.com", ".gemini.google.com"})
MAX_MESSAGE = 4 * 1024 * 1024
MAX_COOKIES = 500
_REPLACE_TRIES = 20


def read_message(stream):
    """Read one length-prefixed JSON message; None at end of stream."""
    raw_len = stream.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    if length > MAX_MESSAGE:
        raise ValueError("message too large")
    data = stream.read(length)
    if len(data) < length:
        raise ValueError("truncated message")
    return json.loads(data.decode("utf-8"))


def write_message(stream, obj):
    data = json.dumps(obj).encode("utf-8")
    stream.write(struct.pack("<I", len(data)))
    stream.write(data)
    stream.flush()


def filter_cookies(cookies, now=None):
    """Keep live Gemini-session cookies, each on its own domain."""
    now = time.time() if now is None else now
    kept = []
    if not isinstance(cookies, list):
        return kept
    for c in cookies[:MAX_COOKIES]:
        if not isinstance(c, dict):
            continue
        domain, name, value = c.get("domain"), c.get("name"), c.get("value")
        if domain not in ALLOWED_DOMAINS or not isinstance(name, str) or not name:
            continue
        if not isinstance(value, str) or not value:
            continue
        exp = c.get("expirationDate")
        if isinstance(exp, (int, float)) and 0 < exp < now:
            continue
        row = {"domain": domain, "name": name, "value": value, "path": c.get("path") or "/"}
        if isinstance(exp, (int, float)):
            row["expirationDate"] = exp
        kept.append(row)
    return kept


def write_cookie_file(path, cookies):
    """Atomically replace `path` with a JSON array of cookies.

    The MCP server may be reading the file at that moment; on Windows a replace
    then fails briefly with PermissionError, so retry for up to ~1 s.
    """
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".gemini-cookies-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cookies, f)
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _same_as_file(path, cookies):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) == cookies
    except (OSError, ValueError):
        return False


def handle(msg, target, accept_from="any"):
    """Process one message from the extension; return the reply (never values)."""
    if not isinstance(msg, dict) or msg.get("type") != "cookies":
        return {"ok": False, "error": "unknown message"}
    source = msg.get("source") if isinstance(msg.get("source"), str) else "unknown"
    if accept_from not in ("any", source):
        return {"ok": False, "error": f"ignored: this host accepts {accept_from}, not {source}"}
    cookies = filter_cookies(msg.get("cookies"))
    if not any(c["name"] == "__Secure-1PSID" and c["domain"] == ".google.com" for c in cookies):
        return {"ok": False, "error": "no signed-in Google session in this browser"}
    if _same_as_file(target, cookies):
        return {"ok": True, "count": len(cookies), "unchanged": True}
    write_cookie_file(target, cookies)
    return {"ok": True, "count": len(cookies)}


def _log(config, line):
    path = config.get("log")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def run(stdin, stdout, config):
    """Handle one exchange: read the extension's message, write the reply."""
    target = config.get("target")
    try:
        msg = read_message(stdin)
        if not target:
            reply = {"ok": False, "error": "host not configured (no target file)"}
        elif msg is None:
            reply = {"ok": False, "error": "no message"}
        else:
            reply = handle(msg, target, config.get("accept_from", "any"))
            source = msg.get("source") if isinstance(msg, dict) else None
            _log(config, f"source={source} ok={reply['ok']} "
                         f"{'count=' + str(reply['count']) if reply['ok'] else 'error=' + reply['error']}"
                         f"{' unchanged' if reply.get('unchanged') else ''}")
    except Exception as exc:  # never let a value reach the reply or the log
        reply = {"ok": False, "error": f"host error: {type(exc).__name__}"}
        _log(config, reply["error"])
    write_message(stdout, reply)
    return reply


def load_config():
    path = os.environ.get("GEMINI_BRIDGE_CONFIG")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


if __name__ == "__main__":
    run(sys.stdin.buffer, sys.stdout.buffer, load_config())
