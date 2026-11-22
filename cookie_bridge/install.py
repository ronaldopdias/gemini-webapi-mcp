"""Install the Gemini cookie bridge for Firefox, Chrome and Edge.

    uv run python cookie_bridge/install.py --accept-from firefox
    uv run python cookie_bridge/install.py --uninstall

Builds cookie_bridge/build/ with:
  firefox/, chromium/        the extension to load in the browser
  firefox.zip                the Firefox extension, ready for Mozilla signing
  native_host.bat            launcher the browser starts (stdlib-only Python host)
  host_config.json           target cookie file, accepted browser, log file
  host.*.json                native-messaging host manifests
then registers the host under HKCU (no admin needed). The target cookie file
defaults to GEMINI_COOKIES_FILE. --accept-from names the one browser whose cookies
are used, so two browsers signed into different accounts can't overwrite each other.
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

HOST_NAME = "com.gemini_webapi_mcp.cookie_bridge"
FIREFOX_ID = "gemini-cookie-bridge@gemini-webapi-mcp"
SOURCES = ("firefox", "chrome", "edge", "any")
HERE = Path(__file__).resolve().parent
REG_PATHS = {
    "firefox": "Software\\Mozilla\\NativeMessagingHosts\\" + HOST_NAME,
    "chrome": "Software\\Google\\Chrome\\NativeMessagingHosts\\" + HOST_NAME,
    "edge": "Software\\Microsoft\\Edge\\NativeMessagingHosts\\" + HOST_NAME,
}


def chromium_extension_id(key_b64: str) -> str:
    """Chromium derives an extension's ID from its public key."""
    der = base64.b64decode(key_b64)
    return hashlib.sha256(der).hexdigest()[:32].translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def build(out_dir, python_exe, target, accept_from, log=None) -> dict:
    if accept_from not in SOURCES:
        raise ValueError(f"accept_from must be one of {SOURCES}")
    out = Path(out_dir)
    ext = HERE / "extension"
    for folder, manifest in (("firefox", "manifest.firefox.json"), ("chromium", "manifest.chromium.json")):
        d = out / folder
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ext / "background.js", d / "background.js")
        shutil.copyfile(ext / manifest, d / "manifest.json")
    firefox_zip = Path(shutil.make_archive(str(out / "firefox"), "zip", root_dir=out / "firefox"))

    key = json.loads((ext / "manifest.chromium.json").read_text(encoding="utf-8"))["key"]
    ext_id = chromium_extension_id(key)

    config = out / "host_config.json"
    config.write_text(json.dumps({
        "target": str(target),
        "accept_from": accept_from,
        "log": str(log or out / "bridge.log"),
    }, indent=2), encoding="utf-8")

    bat = out / "native_host.bat"
    bat.write_text(
        "@echo off\r\n"
        f'set "GEMINI_BRIDGE_CONFIG={config}"\r\n'
        f'"{python_exe}" -I "{HERE / "native_host.py"}" %*\r\n',
        encoding="utf-8",
    )

    common = {"name": HOST_NAME, "description": "Gemini cookie bridge", "path": str(bat), "type": "stdio"}
    host_ff = out / "host.firefox.json"
    host_ff.write_text(json.dumps({**common, "allowed_extensions": [FIREFOX_ID]}, indent=2), encoding="utf-8")
    host_ch = out / "host.chromium.json"
    host_ch.write_text(json.dumps({**common, "allowed_origins": [f"chrome-extension://{ext_id}/"]}, indent=2),
                       encoding="utf-8")

    return {
        "firefox": host_ff, "chrome": host_ch, "edge": host_ch,
        "extension_id": ext_id, "config": config, "bat": bat,
        "firefox_dir": out / "firefox", "chromium_dir": out / "chromium", "firefox_zip": firefox_zip,
    }


def register(paths, winreg=None):
    """Point each browser's native-messaging registry key at its host manifest."""
    if winreg is None:
        import winreg  # noqa: PLC0415 (Windows only)
    for browser, reg_path in REG_PATHS.items():
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path)
        try:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(paths[browser]))
        finally:
            winreg.CloseKey(key)


def unregister(winreg=None):
    if winreg is None:
        import winreg  # noqa: PLC0415
    for reg_path in REG_PATHS.values():
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_path)
        except FileNotFoundError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install the Gemini cookie bridge native host.")
    parser.add_argument("--accept-from", choices=SOURCES,
                        help="the one browser whose Google cookies the server should use")
    parser.add_argument("--target", default=os.environ.get("GEMINI_COOKIES_FILE"),
                        help="cookie file to keep updated (default: GEMINI_COOKIES_FILE)")
    parser.add_argument("--uninstall", action="store_true", help="remove the registry entries")
    args = parser.parse_args(argv)

    if args.uninstall:
        unregister()
        print("Cookie bridge host unregistered for Firefox, Chrome and Edge.")
        return 0
    if not args.accept_from:
        parser.error("--accept-from is required (firefox, chrome, edge or any)")
    if not args.target:
        parser.error("no --target given and GEMINI_COOKIES_FILE is not set")

    paths = build(HERE / "build", sys.executable, args.target, args.accept_from)
    register(paths)
    print("Cookie bridge installed.")
    print(f"  cookie file   : {args.target}")
    print(f"  accepts from  : {args.accept_from}")
    print(f"  Firefox folder: {paths['firefox_dir']}  (zip for signing: {paths['firefox_zip']})")
    print(f"  Chromium folder (Chrome/Edge, load unpacked): {paths['chromium_dir']}")
    print(f"  Chromium extension ID: {paths['extension_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
