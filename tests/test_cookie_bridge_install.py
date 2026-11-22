"""Cookie bridge installer: builds the extension folders and host files, and
registers the host for Firefox, Chrome and Edge (tested against a fake registry)."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "cookie_bridge" / "install.py"
_spec = importlib.util.spec_from_file_location("bridge_install", _PATH)
inst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inst)


def _build(tmp_path, accept="firefox"):
    return inst.build(tmp_path / "build", r"C:\py\python.exe", r"D:\secrets\cookies.txt", accept)


def test_extension_id_comes_from_the_manifest_key():
    manifest = json.loads((_PATH.parent / "extension" / "manifest.chromium.json").read_text(encoding="utf-8"))
    ext_id = inst.chromium_extension_id(manifest["key"])
    assert ext_id == "onncbeblcbahfoleffhnkkibffmakcjl"
    assert len(ext_id) == 32 and set(ext_id) <= set("abcdefghijklmnop")


def test_build_lays_out_both_extensions(tmp_path):
    paths = _build(tmp_path)
    ff = json.loads((paths["firefox_dir"] / "manifest.json").read_text(encoding="utf-8"))
    ch = json.loads((paths["chromium_dir"] / "manifest.json").read_text(encoding="utf-8"))
    assert ff["manifest_version"] == 2 and ff["browser_specific_settings"]["gecko"]["id"] == inst.FIREFOX_ID
    assert ch["manifest_version"] == 3 and "key" in ch
    assert "browser_action" in ff and "action" in ch     # toolbar status badge
    assert (paths["firefox_dir"] / "background.js").is_file() and (paths["chromium_dir"] / "background.js").is_file()
    with zipfile.ZipFile(paths["firefox_zip"]) as z:
        assert {"manifest.json", "background.js"} <= set(z.namelist())


def test_build_writes_config_launcher_and_host_manifests(tmp_path):
    paths = _build(tmp_path)
    config = json.loads(paths["config"].read_text(encoding="utf-8"))
    assert config["target"] == r"D:\secrets\cookies.txt" and config["accept_from"] == "firefox" and config["log"]

    bat = paths["bat"].read_text(encoding="utf-8")
    assert bat.startswith("@echo off") and "GEMINI_BRIDGE_CONFIG" in bat
    assert r"C:\py\python.exe" in bat and " -I " in bat and "native_host.py" in bat

    ff = json.loads(paths["firefox"].read_text(encoding="utf-8"))
    ch = json.loads(paths["chrome"].read_text(encoding="utf-8"))
    for m in (ff, ch):
        assert m["name"] == inst.HOST_NAME and m["type"] == "stdio" and m["path"] == str(paths["bat"])
    assert ff["allowed_extensions"] == [inst.FIREFOX_ID]
    assert ch["allowed_origins"] == ["chrome-extension://onncbeblcbahfoleffhnkkibffmakcjl/"]


def test_build_refuses_an_unknown_source(tmp_path):
    with pytest.raises(ValueError):
        _build(tmp_path, accept="safari")


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def CreateKey(self, root, path):
        self.values.setdefault(path, None)
        return path

    def SetValueEx(self, key, name, reserved, kind, value):
        assert name == "" and kind == self.REG_SZ
        self.values[key] = value

    def CloseKey(self, key):
        pass

    def DeleteKey(self, root, path):
        if path not in self.values:
            raise FileNotFoundError(path)
        del self.values[path]


def test_register_points_each_browser_at_its_manifest(tmp_path):
    paths = _build(tmp_path)
    reg = FakeWinreg()
    inst.register(paths, winreg=reg)
    assert reg.values[inst.REG_PATHS["firefox"]] == str(paths["firefox"])
    assert reg.values[inst.REG_PATHS["chrome"]] == str(paths["chrome"])
    assert reg.values[inst.REG_PATHS["edge"]] == str(paths["chrome"])
    assert all(p.startswith("Software\\") and p.endswith(inst.HOST_NAME) for p in inst.REG_PATHS.values())


def test_unregister_removes_keys_and_tolerates_missing_ones(tmp_path):
    reg = FakeWinreg()
    inst.register(_build(tmp_path), winreg=reg)
    del reg.values[inst.REG_PATHS["edge"]]
    inst.unregister(winreg=reg)
    assert reg.values == {}
