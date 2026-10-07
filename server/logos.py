"""Team logos from ESPN's CDN, made legible on a 4-grey e-paper panel.

Downloads once into cache/logos/ and never touches the network from the render
path: Board.take("sports") calls prefetch(), render calls get(), and get()
returns None for anything not yet on disk (the layout then falls back to text).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

import requests
from PIL import Image, ImageChops, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "cache", "logos")
TTL = 7 * 86400
UA = "eink-quad/0.1 (personal e-paper dashboard; hobby project)"

_mem: dict = {}
_lock = threading.Lock()


def _path(url: str) -> str:
    return os.path.join(DIR, hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")


def prefetch(urls):
    os.makedirs(DIR, exist_ok=True)
    for url in {u for u in urls if u}:
        p = _path(url)
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < TTL:
            continue
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
            r.raise_for_status()
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                f.write(r.content)
            Image.open(tmp).verify()
            os.replace(tmp, p)
            with _lock:
                for k in [k for k in _mem if k[0] == url]:
                    _mem.pop(k)
        except Exception as e:
            print(f"[logos] {url}: {e}", flush=True)


def get(url: str, size: int):
    """size x size "L" image (logo dark on white) or None."""
    if not url:
        return None
    key = (url, size)
    with _lock:
        if key in _mem:
            return _mem[key]
    p = _path(url)
    if not os.path.exists(p):
        return None
    try:
        img = _convert(Image.open(p), size)
    except Exception as e:
        print(f"[logos] convert {url}: {e}", flush=True)
        img = None
    with _lock:
        _mem[key] = img
    return img


def _convert(src: Image.Image, size: int) -> Image.Image:
    src = src.convert("RGBA")
    bg = Image.new("RGBA", src.size, (255, 255, 255, 255))
    bg.alpha_composite(src)
    g = bg.convert("L")
    # Trim the empty margin so every logo fills its box the same way.
    box = ImageChops.invert(g).point(lambda v: 255 if v > 12 else 0).getbbox()
    if box:
        g = g.crop(box)
    # Team colours land in a narrow mid-grey band (Bucs red ~ Bucs pewter);
    # stretch so the darkest part is ink and the page stays paper.
    g = ImageOps.autocontrast(g, cutoff=(1, 0))
    w, h = g.size
    s = size / max(w, h)
    g = g.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    out = Image.new("L", (size, size), 255)
    out.paste(g, ((size - g.width) // 2, (size - g.height) // 2))
    return out
