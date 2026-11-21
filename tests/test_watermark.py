"""Watermark removal tests.

Goal (per spec): after removal, neither the Gemini sparkle nor any removal
artifact is visible — on any background, size or aspect ratio.

Two layers:
  * synthetic — deterministic forward/reverse blend over clean and busy bases,
    covering the canonical 64px and the new 192px anchors,
    plus clean (no-false-positive) cases.
  * real fixture — an actual busy-background corner where free NCC search failed
    to even detect the mark (regression for the wandering-argmax bug).
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gemini_webapi_mcp import server as S  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _clean_base(w, h, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 130 + 40 * np.sin(xx / 53.0) + 35 * np.cos(yy / 61.0)
    return np.clip(base[..., None] + rng.normal(0, 4, (h, w, 3)), 0, 255).astype(np.float32)


def _blend(C, margin, A=0.27):
    """Forward-blend the white 96px mark at `margin`: L = bg + A*shape*(255-bg)."""
    alpha = (S._shape_at(96) * A)[..., None]
    x0, y0 = C.shape[1] - 96 - margin, C.shape[0] - 96 - margin
    W = C.copy()
    box = C[y0:y0 + 96, x0:x0 + 96]
    W[y0:y0 + 96, x0:x0 + 96] = box * (1.0 - alpha) + alpha * 255.0
    return W, (x0, y0)


def _sparkle_ncc(gray, x0, y0, size=96):
    """Correlation of the alpha sparkle shape with a patch — high = mark present,
    near-zero = clean, negative = over-removed (dark sparkle artifact)."""
    a = S._shape_at(size)
    p = gray[y0:y0 + size, x0:x0 + size]
    t, q = a - a.mean(), p - p.mean()
    return float((t * q).sum() / (np.sqrt((t * t).sum() * (q * q).sum()) + 1e-6))


def _run_remove(arr, upscaled=True):
    """Every case in this file uses the 2x anchor convention — a 96px mark at
    margin 64 or 192 (`_ANCHOR_MARGINS` doubled). `_remove_watermark` takes
    `upscaled` explicitly rather than inferring it from size, so it must be
    passed here or the search looks at logo 48 / margins 96,32 and finds nothing.
    """
    p = tempfile.mktemp(suffix=".png")
    Image.fromarray(arr.astype(np.uint8)).save(p)
    removed = S._remove_watermark(p, upscaled=upscaled)
    out = np.asarray(Image.open(p).convert("RGB"), dtype=np.float32)
    os.remove(p)
    return removed, out


def _assert_clean(name, C, W, anchor, max_err):
    """max_err is the "no visible residual" bar. It is looser here than the 8.0
    used in test_watermark_gate.py because `_clean_base` is deliberately rougher
    (amplitudes 40/35, noise sigma 4) than a real frame: the per-image plane-fit
    opacity lands within about +-15% of the stamped A (measured 0.236-0.310 for
    A=0.27), leaving 7.5-9.0 levels. The same geometry on a smooth base leaves
    5.2. The tighter bar stays enforced on realistic content by the gate test.
    """
    removed, R = _run_remove(W)
    x0, y0 = anchor
    err = float(np.abs(R[y0:y0 + 96, x0:x0 + 96] - C[y0:y0 + 96, x0:x0 + 96]).max())
    assert removed, f"{name}: watermark not detected"
    assert err <= max_err, f"{name}: patch error {err:.1f} > {max_err} (visible residual/artifact)"


def test_easy_portrait_64():
    C = _clean_base(1792, 2400, 1)
    W, a = _blend(C, 64)
    _assert_clean("easy-portrait-64", C, W, a, 10.0)


def test_easy_landscape_64():
    C = _clean_base(2400, 1792, 2)
    W, a = _blend(C, 64)
    _assert_clean("easy-landscape-64", C, W, a, 10.0)


def test_new_margin_192():
    C = _clean_base(2816, 1536, 3)
    W, a = _blend(C, 192)
    _assert_clean("new-margin-192", C, W, a, 10.0)


def test_no_false_positive_clean():
    C = _clean_base(1792, 2400, 9)
    removed, R = _run_remove(C)
    assert not removed, "false positive on clean image"
    assert float(np.abs(R - C).max()) < 1.0, "clean image was modified"


def test_real_busy_corner_detected_and_cleaned():
    """Real busy-background frame: mark is faint over dark texture (NCC ~0.44 at
    the anchor). Free argmax search wandered off and detected nothing. After the
    fix the mark must be located and removed without leaving a sparkle artifact.
    Crop is 1280px (>=1k) so the real 96px mark sits on the canonical 64 anchor."""
    path = os.path.join(FIXTURES, "busy_corner_1280.png")
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]
    x0, y0 = w - 96 - 64, h - 96 - 64
    removed, R = _run_remove(arr)
    after = abs(_sparkle_ncc(R.mean(2), x0, y0))
    assert removed, "real busy: watermark not detected"
    assert after < 0.30, f"real busy: sparkle/artifact remains (residual NCC {after:.3f})"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
