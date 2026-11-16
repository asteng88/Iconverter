#!/usr/bin/env python3
"""
Icon & Vector Studio — Faithful Picture to .SVG and Multi-Size .ICNS Converter.

Run directly from Python:
    python icon_converter.py
    python icon_converter.py path/to/picture.png

Headless CLI conversion:
    python icon_converter.py --cli icons/porter_source.jpg --out-dir icons --name porter

Build standalone Windows executable later with PyInstaller:
    uv run pyinstaller --noconfirm --onefile --windowed --name Iconverter ^
        --icon icons/Icon_generator.ico --add-data "icons;icons" ^
        --paths ModernTtk --collect-all modernttk --collect-all vtracer --collect-all resvg_py icon_converter.py
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
from dataclasses import dataclass, field, replace
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pickle
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse


def _read_exact_bytes(pipe: Any, n: int) -> bytes:
    """Read exactly `n` bytes from a binary pipe, or return truncated bytes on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _write_ipc_frame(pipe: Any, payload: Any) -> None:
    """Serialize `payload` with a 4-byte length prefix onto a binary pipe."""
    data = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    pipe.write(struct.pack(">I", len(data)))
    pipe.write(data)
    pipe.flush()


def _read_ipc_frame(pipe: Any) -> Any:
    """Read and deserialize one length-prefixed frame from a binary pipe."""
    hdr = _read_exact_bytes(pipe, 4)
    if not hdr:
        raise EOFError("Native worker pipe closed.")
    if len(hdr) < 4:
        raise EOFError("Truncated native worker frame header.")
    (length,) = struct.unpack(">I", hdr)
    body = _read_exact_bytes(pipe, length)
    if len(body) < length:
        raise EOFError("Truncated native worker frame body.")
    return pickle.loads(body)


def _run_native_svg_worker_loop() -> int:
    """Persistent subprocess loop executing GIL-heavy vtracer/resvg_py native calls."""
    in_pipe = sys.stdin.buffer
    out_pipe = sys.stdout.buffer
    if sys.stderr is not None:
        sys.stdout = sys.stderr

    try:
        import vtracer as _vt  # type: ignore
    except ImportError:  # pragma: no cover
        _vt = None  # type: ignore
    try:
        import resvg_py as _rv  # type: ignore
    except ImportError:  # pragma: no cover
        _rv = None  # type: ignore

    while True:
        try:
            req = _read_ipc_frame(in_pipe)
        except EOFError:
            return 0
        except Exception as exc:
            _write_ipc_frame(out_pipe, (False, f"{type(exc).__name__}: {exc}"))
            continue

        op = req[0] if req else ""
        try:
            if op == "ping":
                _write_ipc_frame(out_pipe, (True, "pong"))
            elif op == "shutdown":
                _write_ipc_frame(out_pipe, (True, None))
                return 0
            elif op == "vtracer_pixels":
                if _vt is None:
                    raise RuntimeError("vtracer is unavailable in worker.")
                _, rgba_bytes, tw, th, cfg_dict = req
                cfg = _vt.Config(**cfg_dict)
                svg_text = _vt.convert_pixels(rgba_bytes, int(tw), int(th), config=cfg)
                _write_ipc_frame(out_pipe, (True, svg_text))
            elif op == "vtracer_raw":
                if _vt is None:
                    raise RuntimeError("vtracer is unavailable in worker.")
                _, png_bytes, kw_dict = req
                svg_text = _vt.convert_raw_image_to_svg(png_bytes, **kw_dict)
                _write_ipc_frame(out_pipe, (True, svg_text))
            elif op == "resvg":
                if _rv is None:
                    raise RuntimeError("resvg_py is unavailable in worker.")
                _, svg_text, width, height = req
                kw: Dict[str, Any] = {"svg_string": svg_text}
                if width is not None:
                    kw["width"] = int(width)
                if height is not None:
                    kw["height"] = int(height)
                png_bytes = bytes(_rv.svg_to_bytes(**kw))
                _write_ipc_frame(out_pipe, (True, png_bytes))
            else:
                _write_ipc_frame(out_pipe, (False, f"Unknown worker op: {op!r}"))
        except Exception as exc:
            _write_ipc_frame(out_pipe, (False, f"{type(exc).__name__}: {exc}"))


if len(sys.argv) >= 2 and sys.argv[1] == "--native-svg-worker":
    raise SystemExit(_run_native_svg_worker_loop())


import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox

import cv2
import numpy as np
from PIL import (
    IcnsImagePlugin,
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageFont,
    ImageGrab,
    ImageOps,
    ImageTk,
)


def ensure_modernttk_on_path() -> Path:
    """Ensure `modernttk` is importable from the PyInstaller bundle, installed package, or local `./ModernTtk`."""
    meipass = getattr(sys, "_MEIPASS", None)
    here = Path(__file__).resolve().parent
    candidates: List[Path] = []
    if meipass:
        candidates.extend([Path(meipass), Path(meipass) / "ModernTtk"])
    candidates.extend([
        here / "ModernTtk",
        here,
        here.parent / "ModernTtk",
    ])
    for cand in candidates:
        if (cand / "modernttk" / "__init__.py").is_file():
            s = str(cand)
            if s not in sys.path:
                sys.path.insert(0, s)
            return cand / "modernttk"

    try:
        import modernttk  # noqa: F401
        return Path(modernttk.__file__).resolve().parent
    except ImportError:
        return Path()


ensure_modernttk_on_path()
import modernttk as mtk  # noqa: E402

try:
    import vtracer  # type: ignore
except ImportError:  # pragma: no cover - fallback path handled cleanly
    vtracer = None  # type: ignore

try:
    import resvg_py  # type: ignore
except ImportError:  # pragma: no cover - fallback path handled cleanly
    resvg_py = None  # type: ignore


class _NativeSvgWorkerClient(mtk.ModernSubprocessWorker):
    """Thread-safe client for the persistent GIL-free `vtracer`/`resvg_py` subprocess (backed by ModernTtk)."""

    def __init__(self) -> None:
        super().__init__(
            script_path=Path(__file__).resolve(),
            worker_flag="--native-svg-worker",
            auto_atexit=False,
        )


_NATIVE_SVG_WORKER = _NativeSvgWorkerClient()
atexit.register(_NATIVE_SVG_WORKER.shutdown)


def _should_offload_native_gil() -> bool:
    """Offload GIL-blocking Rust extensions when executing on a GUI background thread."""
    return mtk.ModernSubprocessWorker.should_offload()


__version__ = "1.0.3"
GITHUB_OWNER = os.environ.get("ICONVERTER_GITHUB_OWNER", "asteng88")
GITHUB_REPO = os.environ.get("ICONVERTER_GITHUB_REPO", "Iconverter")
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
GITHUB_API_LATEST_URL = os.environ.get(
    "ICONVERTER_UPDATE_URL",
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest",
)

if getattr(sys, "frozen", False):
    APP_DIR = Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))).resolve()
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
    ROOT_DIR = APP_DIR


def ensure_icons_dir(base_dir: Optional[Path | str] = None) -> Path:
    """Ensure the `icons` folder is built on first run and seeded with bundled studio icons."""
    if base_dir is not None:
        target_root = Path(base_dir).resolve()
    elif getattr(sys, "frozen", False):
        target_root = Path(sys.executable).resolve().parent
    else:
        target_root = Path(__file__).resolve().parent

    icons_dir = target_root / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    # Seed bundled brand icons into the external icons/ folder on first run when compiled with PyInstaller
    bundled_candidates = [APP_DIR / "icons", Path(__file__).resolve().parent / "icons"]
    for bundled_icons in bundled_candidates:
        if not bundled_icons.is_dir():
            continue
        try:
            if bundled_icons.resolve() == icons_dir.resolve():
                continue
        except OSError:
            continue
        for asset_name in ("Icon_generator.icns", "Icon_generator.svg", "Icon_generator.ico"):
            src_asset = bundled_icons / asset_name
            dst_asset = icons_dir / asset_name
            if src_asset.is_file() and not dst_asset.exists():
                try:
                    shutil.copy2(src_asset, dst_asset)
                except OSError:
                    pass
    return icons_dir


ICONS_DIR = ensure_icons_dir()

APP_TITLE = "Icon & Vector Studio — Picture to .SVG & .ICNS"
SUPPORTED_INPUT_EXTS = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".ico", ".svg", ".icns",
)
MAX_TRACE_DIM = 3072
MAX_SVG_ZOOM_RENDER_DIM = 4096

BG_MODE_LABELS: Dict[str, str] = {
    "faithful": "Faithful Original (Keep Canvas)",
    "transparent": "Transparent (Smart Alpha Matte)",
    "squircle": "macOS Squircle Badge",
}
LABEL_TO_BG_MODE: Dict[str, str] = {v: k for k, v in BG_MODE_LABELS.items()}

ICNS_TIERS: Tuple[int, ...] = (16, 32, 48, 64, 128, 256, 512, 1024)

# Complete 12-slot Apple Icon Image specification matching PIL.IcnsImagePlugin.IcnsFile.SIZES
ICNS_SLOT_SPECS: Tuple[Tuple[bytes, int, int, int, str, str], ...] = (
    (b"is32", 16, 16, 1, "16x16 @1x (Legacy RGB)", "RLE24 + s8mk"),
    (b"icp4", 16, 16, 1, "16x16 @1x", "PNG"),
    (b"ic11", 32, 16, 2, "16x16 @2x Retina", "PNG"),
    (b"il32", 32, 32, 1, "32x32 @1x (Legacy RGB)", "RLE24 + l8mk"),
    (b"icp5", 32, 32, 1, "32x32 @1x", "PNG"),
    (b"ih32", 48, 48, 1, "48x48 @1x (Legacy RGB)", "RLE24 + h8mk"),
    (b"ic12", 64, 32, 2, "32x32 @2x Retina", "PNG"),
    (b"icp6", 64, 64, 1, "64x64 @1x", "PNG"),
    (b"ic07", 128, 128, 1, "128x128 @1x", "PNG"),
    (b"ic13", 256, 128, 2, "128x128 @2x Retina", "PNG"),
    (b"ic08", 256, 256, 1, "256x256 @1x", "PNG"),
    (b"ic14", 512, 256, 2, "256x256 @2x Retina", "PNG"),
    (b"ic09", 512, 512, 1, "512x512 @1x", "PNG"),
    (b"ic10", 1024, 512, 2, "512x512 @2x Retina", "PNG"),
)

LEGACY_MASK_TYPES: Dict[bytes, bytes] = {
    b"is32": b"s8mk",
    b"il32": b"l8mk",
    b"ih32": b"h8mk",
}


@dataclass(frozen=True)
class TransparencyPick:
    """User-selected color/seed location to add to smart background transparency."""

    x_norm: float
    y_norm: float
    rgb: Tuple[int, int, int]
    tolerance: float = 26.0


@dataclass
class SVGOptions:
    """Parameters governing SVG vector tracing."""

    preset_key: str = "ultra"
    mode: str = "spline"  # "spline", "polygon", "pixel"
    hierarchical: str = "stacked"
    color_precision: int = 8
    layer_difference: int = 6
    filter_speckle: int = 3
    corner_threshold: int = 55
    length_threshold: float = 3.8
    max_iterations: int = 10
    splice_threshold: int = 45
    simplify: Optional[float] = 0.04
    path_precision: int = 2
    optimize: int = 1
    trace_scale: float = 2.0
    true_gradients: bool = True
    clean_aa_lines: bool = True


SVG_PRESETS: Dict[str, Tuple[str, SVGOptions]] = {
    "ultra": (
        "Ultra Faithful (Recommended)",
        SVGOptions(
            preset_key="ultra",
            mode="spline",
            color_precision=8,
            layer_difference=6,
            filter_speckle=3,
            corner_threshold=55,
            length_threshold=3.8,
            splice_threshold=45,
            simplify=0.04,
            path_precision=2,
            optimize=1,
            trace_scale=2.0,
            true_gradients=True,
            clean_aa_lines=True,
        ),
    ),
    "high": (
        "High Detail Illustration",
        SVGOptions(
            preset_key="high",
            mode="spline",
            color_precision=8,
            layer_difference=8,
            filter_speckle=3,
            corner_threshold=56,
            length_threshold=4.0,
            splice_threshold=45,
            simplify=0.05,
            path_precision=2,
            optimize=1,
            trace_scale=2.0,
            true_gradients=True,
            clean_aa_lines=True,
        ),
    ),
    "balanced": (
        "Balanced Vector",
        SVGOptions(
            preset_key="balanced",
            mode="spline",
            color_precision=7,
            layer_difference=11,
            filter_speckle=4,
            corner_threshold=58,
            length_threshold=4.2,
            splice_threshold=45,
            simplify=0.08,
            path_precision=2,
            optimize=1,
            trace_scale=2.0,
            true_gradients=True,
            clean_aa_lines=True,
        ),
    ),
    "compact": (
        "Compact / Flat Shapes",
        SVGOptions(
            preset_key="compact",
            mode="spline",
            color_precision=6,
            layer_difference=20,
            filter_speckle=6,
            corner_threshold=60,
            length_threshold=4.5,
            splice_threshold=45,
            simplify=0.15,
            path_precision=2,
            optimize=1,
            trace_scale=1.5,
            true_gradients=True,
            clean_aa_lines=True,
        ),
    ),
}

PRESET_LABEL_TO_KEY: Dict[str, str] = {label: key for key, (label, _) in SVG_PRESETS.items()}


@dataclass
class PrepareOptions:
    """Parameters governing image preprocessing and framing."""

    bg_mode: str = "faithful"  # "faithful", "transparent", "squircle"
    clean_jpeg_noise: bool = True
    auto_trim: bool = False
    padding_pct: float = 6.0
    deepen_diff: bool = True
    deepen_strength: float = 55.0
    picked_transparencies: Tuple[TransparencyPick, ...] = ()
    square_crop: Optional[Tuple[int, int, int, int]] = None
    rounded_corners: bool = False
    corner_radius_pct: float = 18.0
    rounded_border: bool = False
    border_width_pct: float = 3.0
    border_color_rgb: Tuple[int, int, int] = (76, 194, 255)


@dataclass
class PreparedMaster:
    """Preprocessed artwork plus detected border color metadata."""

    image: Image.Image  # RGBA image for SVG tracing & inspection
    square_icon: Image.Image  # Square >= 1024x1024 RGBA master for ICNS/ICO
    border_rgb: Optional[Tuple[int, int, int]]
    has_transparency: bool
    source_size: Tuple[int, int]
    crop_box: Tuple[int, int, int, int] = (0, 0, 0, 0)
    unframed_image: Optional[Image.Image] = None
    rounded_corners: bool = False
    corner_radius_pct: float = 0.0
    rounded_border: bool = False
    border_width_pct: float = 0.0
    border_color_rgb: Tuple[int, int, int] = (76, 194, 255)


@dataclass
class IcnsSlotInfo:
    """Metadata and verified decoded image for one ICNS chunk/slot."""

    ostype: str
    slot_label: str
    pixel_size: int
    scale: int
    encoding: str
    byte_length: int
    decoded_ok: bool
    decoded_size: Optional[Tuple[int, int]] = None
    image: Optional[Image.Image] = None


@dataclass
class IcnsInspection:
    """Full structural and Pillow round-trip inspection of an .icns payload."""

    total_bytes: int
    has_toc: bool
    chunk_count: int
    slots: List[IcnsSlotInfo]
    verified_sizes: List[Tuple[int, int, int]]
    images_by_size: Dict[Tuple[int, int, int], Image.Image]


@dataclass
class ConversionResult:
    """Full artifact + metric bundle returned by conversion/preview."""

    source_image: Image.Image
    prepared: PreparedMaster
    svg_text: str
    svg_rendered: Image.Image
    psnr_db: float
    similarity_pct: float
    path_count: int
    svg_bytes: int
    icns_bytes: bytes
    icns_inspection: IcnsInspection
    elapsed_sec: float
    gradient_count: int = 0
    written_files: List[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Image Preprocessing & Framing
# ---------------------------------------------------------------------------


def load_source_image(path: str | Path) -> Image.Image:
    """Load a raster, .svg, or .icns image file honoring EXIF orientation and convert to RGBA."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".svg":
        svg_text = p.read_text(encoding="utf-8", errors="replace")
        return render_svg_to_pil(svg_text)
    if ext == ".icns":
        try:
            insp = inspect_icns(p)
            if insp.images_by_size:
                best_key = max(insp.images_by_size.keys(), key=lambda k: k[0] * k[1] * k[2])
                return insp.images_by_size[best_key].copy().convert("RGBA")
        except Exception:
            pass
    with Image.open(p) as im:
        oriented = ImageOps.exif_transpose(im)
        return oriented.convert("RGBA")


def _detect_uniform_border_rgb(arr: np.ndarray) -> Optional[Tuple[int, int, int]]:
    """Return uniform perimeter RGB if corners/border agree within <= 10 distance."""
    h, w = arr.shape[:2]
    if h < 4 or w < 4:
        return None

    alpha = arr[..., 3]
    # If border already uses genuine transparency, don't force an opaque border fill
    border_alpha = np.concatenate([alpha[0, :], alpha[-1, :], alpha[:, 0], alpha[:, -1]])
    if np.mean(border_alpha < 250) > 0.15:
        return None

    corners = np.array([
        arr[0, 0, :3],
        arr[0, w - 1, :3],
        arr[h - 1, 0, :3],
        arr[h - 1, w - 1, :3],
    ], dtype=np.float32)

    border_pixels = np.concatenate([
        arr[0, :, :3],
        arr[-1, :, :3],
        arr[:, 0, :3],
        arr[:, -1, :3],
    ], axis=0).astype(np.float32)

    med = np.median(border_pixels, axis=0)
    corner_dists = np.linalg.norm(corners - med, axis=1)
    if float(np.max(corner_dists)) > 10.0:
        return None

    # Snap near-white borders (e.g. JPEG 254,254,254) to clean pure white
    r, g, b = (int(round(med[0])), int(round(med[1])), int(round(med[2])))
    if r >= 248 and g >= 248 and b >= 248:
        return (255, 255, 255)
    if r <= 7 and g <= 7 and b <= 7:
        return (0, 0, 0)
    return (r, g, b)


def _edge_connected_mask(candidate_mask: np.ndarray) -> np.ndarray:
    """Return boolean mask of pixels in `candidate_mask` connected to the outer border."""
    mask_u8 = candidate_mask.astype(np.uint8)
    num_labels, labels = cv2.connectedComponents(mask_u8, connectivity=4)
    if num_labels <= 1:
        return np.zeros_like(candidate_mask, dtype=bool)

    border_labels = np.unique(
        np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])
    )
    border_labels = border_labels[border_labels > 0]
    if border_labels.size == 0:
        return np.zeros_like(candidate_mask, dtype=bool)
    return np.isin(labels, border_labels)


def _apply_flat_jpeg_denoise(rgb: np.ndarray) -> np.ndarray:
    """Smooth JPEG mosquito/block noise inside flat fills and stroke cores while preserving sharp edges."""
    _, grad_mag = _compute_lab_gradient(rgb)
    s1 = cv2.bilateralFilter(rgb, d=7, sigmaColor=14, sigmaSpace=4.5)
    s2 = cv2.bilateralFilter(s1, d=5, sigmaColor=10, sigmaSpace=3.5)
    w = np.clip((11.0 - grad_mag) / 4.5, 0.0, 1.0)[..., None]
    blended = rgb.astype(np.float32) * (1.0 - w) + s2.astype(np.float32) * w
    return np.clip(np.round(blended), 0, 255).astype(np.uint8)


def _create_superellipse_mask(size: int, box_size: int, exponent: float = 5.0, scale: int = 4) -> Image.Image:
    """Render a 4x supersampled Apple-style continuous superellipse alpha mask."""
    big = size * scale
    half = (box_size * scale) / 2.0
    cx = (big - 1) / 2.0
    cy = (big - 1) / 2.0

    yy, xx = np.ogrid[:big, :big]
    nx = np.abs(xx - cx) / max(half, 1.0)
    ny = np.abs(yy - cy) / max(half, 1.0)
    dist = np.power(nx, exponent) + np.power(ny, exponent)
    # Smooth anti-aliased edge over ~1 supersampled pixel
    edge = np.clip((1.0 - dist) * 20.0 + 0.5, 0.0, 1.0)
    mask_big = Image.fromarray((edge * 255.0).astype(np.uint8), mode="L")
    return mask_big.resize((size, size), Image.Resampling.LANCZOS)


def _apply_squircle_badge(im_rgba: Image.Image, fill_bg: Tuple[int, int, int]) -> Image.Image:
    """Place artwork cleanly inside a 1024x1024 macOS HIG superellipse badge with subtle shadow."""
    canvas_size = 1024
    badge_size = 824

    mask = _create_superellipse_mask(canvas_size, badge_size, exponent=5.0, scale=4)

    # Drop shadow derived from superellipse mask
    shadow_alpha = mask.point(lambda p: int(p * 0.28)).filter(ImageFilter.GaussianBlur(radius=18))
    shadow = Image.new("RGBA", (canvas_size, canvas_size), (10, 14, 24, 0))
    shadow.putalpha(shadow_alpha)
    shadow_shifted = ImageChops.offset(shadow, 0, 14)

    # Scale artwork proportionally inside the badge
    inner = Image.new("RGBA", (canvas_size, canvas_size), (*fill_bg, 255))
    fit = ImageOps.contain(im_rgba, (badge_size - 24, badge_size - 24), Image.Resampling.LANCZOS)
    px = (canvas_size - fit.width) // 2
    py = (canvas_size - fit.height) // 2
    inner.alpha_composite(fit, (px, py))

    # Soft inner highlight border along badge silhouette
    clipped = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    clipped.paste(inner, (0, 0), mask)

    out = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    out.alpha_composite(shadow_shifted)
    out.alpha_composite(clipped)
    return out


def _detect_fallback_border_rgb(arr: np.ndarray) -> Optional[Tuple[int, int, int]]:
    """Estimate perimeter background RGB when borders are opaque and reasonably cohesive."""
    h, w = arr.shape[:2]
    if h < 4 or w < 4:
        return None
    alpha = arr[..., 3]
    border_alpha = np.concatenate([alpha[0, :], alpha[-1, :], alpha[:, 0], alpha[:, -1]])
    if float(np.mean(border_alpha >= 245)) <= 0.85:
        return None
    border_px = np.concatenate(
        [arr[0, :, :3], arr[-1, :, :3], arr[:, 0, :3], arr[:, -1, :3]], axis=0
    ).astype(np.float32)
    q25 = np.percentile(border_px, 25, axis=0)
    q75 = np.percentile(border_px, 75, axis=0)
    if float(np.linalg.norm(q75 - q25)) > 32.0:
        return None
    med = np.median(border_px, axis=0)
    return (int(round(med[0])), int(round(med[1])), int(round(med[2])))


def _pull_push_extrapolate_rgb(
    rgb_f: np.ndarray,
    valid_mask: np.ndarray,
    kernel_sizes: Tuple[int, ...] = (5, 11, 23),
) -> np.ndarray:
    """Propagate clean foreground RGB colors outward across invalid/transparent regions."""
    cur_rgb = np.where(valid_mask[..., None], rgb_f, 0.0).astype(np.float32)
    cur_w = valid_mask.astype(np.float32)
    for ksz in kernel_sizes:
        b_rgb = cv2.GaussianBlur(cur_rgb, (ksz, ksz), 0)
        b_w = cv2.GaussianBlur(cur_w, (ksz, ksz), 0)
        fill_zone = (cur_w < 1e-4) & (b_w > 1e-4)
        if np.any(fill_zone):
            cur_rgb = np.where(fill_zone[..., None], b_rgb / np.maximum(b_w[..., None], 1e-6), cur_rgb)
            cur_w = np.where(fill_zone, 1.0, cur_w)
    return np.clip(cur_rgb, 0.0, 255.0)


def _compute_lab_gradient(rgb_u8: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Return (lab_f32, approx_per_pixel_lab_grad_mag)."""
    lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2Lab).astype(np.float32)
    gx = cv2.Sobel(lab, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(lab, cv2.CV_32F, 0, 1, ksize=3)
    grad_mag = np.sqrt(np.sum(gx * gx + gy * gy, axis=2)) / 4.0
    return lab, grad_mag


def _build_contrast_barrier(lum: np.ndarray, ref_lum: float, delta: float = 70.0) -> np.ndarray:
    """Create a 4-connected-sealed barrier mask separating foreground outlines from background."""
    if ref_lum >= 128.0:
        stroke = lum < min(ref_lum - delta, max(45.0, ref_lum - 70.0))
    else:
        stroke = lum > max(ref_lum + delta, min(210.0, ref_lum + 70.0))
    cross3 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
    return cv2.dilate(stroke.astype(np.uint8), cross3, iterations=1) > 0


def _detect_smart_bg_pockets(
    cand: np.ndarray,
    ext_mask: np.ndarray,
    dist_rgb: np.ndarray,
    cc_labels: np.ndarray,
    cc_stats: np.ndarray,
    num_cc: int,
    border_ids: set[int],
) -> np.ndarray:
    """Identify enclosed background pockets while keeping interior artwork highlights opaque."""
    h, w = cand.shape
    pocket_mask = np.zeros((h, w), dtype=bool)
    macro_min_area = max(300, int(0.0003 * h * w))
    counter_max_area = max(500, int(0.0006 * h * w))
    max_island_area = int(0.005 * h * w)

    non_ext_fg = (~ext_mask).astype(np.uint8)
    _, fg_labels, fg_stats, _ = cv2.connectedComponentsWithStats(non_ext_fg, connectivity=8)
    kern3 = np.ones((3, 3), dtype=np.uint8)

    for cid in range(1, num_cc):
        if cid in border_ids:
            continue
        area = int(cc_stats[cid, cv2.CC_STAT_AREA])
        if area < 18:
            continue
        comp = cc_labels == cid
        p15_dist = float(np.percentile(dist_rgb[comp], 15))

        # Rule A: Macro enclosed background pocket (e.g. between legs, under arms, between wheels)
        if area >= macro_min_area and p15_dist <= 5.5:
            dt = cv2.distanceTransform(comp.astype(np.uint8), cv2.DIST_L2, 3)
            if float(dt.max()) >= 4.0:
                pocket_mask |= comp
                continue

        # Rule B: Counter hole inside an isolated small foreground glyph (e.g. hollow '0')
        if area <= counter_max_area and p15_dist <= 14.0:
            dil = cv2.dilate(comp.astype(np.uint8), kern3, iterations=1) > 0
            ring_ids = set(np.unique(fg_labels[dil & (~comp)])) - {0}
            if len(ring_ids) == 1:
                fid = next(iter(ring_ids))
                if int(fg_stats[fid, cv2.CC_STAT_AREA]) <= max_island_area:
                    pocket_mask |= comp

    return pocket_mask


def _apply_smart_transparency_matte(
    arr: np.ndarray,
    border_rgb: Optional[Tuple[int, int, int]],
    opts: PrepareOptions,
) -> np.ndarray:
    """
    Remove background regions cleanly with perceptual Lab separation, enclosed-pocket
    detection, interactive user transparency picks, smooth glow alpha feathering,
    and halo-free foreground color unmixing.
    """
    h, w = arr.shape[:2]
    rgb_u8 = arr[..., :3]
    rgb_f = rgb_u8.astype(np.float32)
    alpha_f = arr[..., 3].astype(np.float32) / 255.0

    base_bg_tuple = border_rgb if border_rgb is not None else _detect_fallback_border_rgb(arr)
    has_existing_alpha = bool(np.mean(alpha_f[0, :] < 0.98) > 0.15 or np.mean(alpha_f[:, 0] < 0.98) > 0.15)

    lab, grad_mag = _compute_lab_gradient(rgb_u8)
    lum = rgb_f.mean(axis=2)
    s = (float(np.clip(opts.deepen_strength, 0.0, 100.0)) / 100.0) if opts.deepen_diff else 0.0

    if base_bg_tuple is not None:
        bg_vec = np.array(base_bg_tuple, dtype=np.float32)
    else:
        bg_vec = np.array([255.0, 255.0, 255.0], dtype=np.float32)
    ref_bg_map = np.broadcast_to(bg_vec[None, None, :], (h, w, 3)).copy()

    # Phase 1: Automatic exterior + smart enclosed background pockets
    if base_bg_tuple is not None and not has_existing_alpha:
        bg_lab = cv2.cvtColor(np.uint8([[bg_vec]]), cv2.COLOR_RGB2Lab).astype(np.float32)[0, 0]
        bg_lum = float(bg_vec.mean())
        sealed_dark = _build_contrast_barrier(lum, bg_lum, delta=70.0)
        edge_barrier = sealed_dark | (grad_mag >= 14.0)

        dist_rgb = np.linalg.norm(rgb_f - bg_vec[None, None, :], axis=2)
        d_lab = lab - bg_lab[None, None, :]
        chroma_diff = np.sqrt(d_lab[..., 1] ** 2 + d_lab[..., 2] ** 2)

        chroma_boost = np.clip((chroma_diff - 4.0) / 14.0, 0.0, 1.6)
        neutral_factor = np.where(chroma_diff < 4.5, 1.0 - 0.28 * s, 1.0)
        eff_dist = dist_rgb * (1.0 + 0.95 * s * chroma_boost) * neutral_factor

        cand = (eff_dist <= (26.0 + 4.0 * s)) & (~sealed_dark)
        num_cc, cc_labels, cc_stats, _ = cv2.connectedComponentsWithStats(
            cand.astype(np.uint8), connectivity=4
        )
        border_ids = (
            set(np.unique(np.concatenate([cc_labels[0, :], cc_labels[-1, :], cc_labels[:, 0], cc_labels[:, -1]])))
            - {0}
        )
        ext_mask = np.isin(cc_labels, list(border_ids))
        pocket_mask = _detect_smart_bg_pockets(
            cand, ext_mask, dist_rgb, cc_labels, cc_stats, num_cc, border_ids
        )
        confirmed_bg = ext_mask | pocket_mask

        hard_floor = 7.0 + 5.0 * s
        soft_ceil = 46.0 - 8.0 * s
        halo_cand = confirmed_bg | ((~edge_barrier) & (eff_dist <= soft_ceil))
        _, h_labels = cv2.connectedComponents(halo_cand.astype(np.uint8), connectivity=4)
        bg_h_ids = set(np.unique(h_labels[confirmed_bg])) - {0}
        halo_zone = np.isin(h_labels, list(bg_h_ids))
        aa_ring = (
            (cv2.dilate(halo_zone.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1) > 0)
            & (~halo_zone)
            & (~sealed_dark)
        )

        t = np.clip((eff_dist - hard_floor) / max(soft_ceil - hard_floor, 4.0), 0.0, 1.0)
        ramp_alpha = t * t * (3.0 - 2.0 * t)
        alpha_f = np.where(halo_zone, np.minimum(alpha_f, ramp_alpha), alpha_f)
        aa_t = np.clip((dist_rgb - 6.0) / 48.0, 0.0, 1.0)
        alpha_f = np.where(aa_ring, np.minimum(alpha_f, aa_t), alpha_f)

    # Phase 2: Interactive smart Transparency Picker seeds
    kern5 = np.ones((5, 5), dtype=np.uint8)
    kern3 = np.ones((3, 3), dtype=np.uint8)
    bg_lum_ref = float(bg_vec.mean())
    for pick in opts.picked_transparencies:
        px = int(np.clip(round(pick.x_norm * max(w - 1, 1)), 0, w - 1))
        py = int(np.clip(round(pick.y_norm * max(h - 1, 1)), 0, h - 1))
        p_rgb = np.array(pick.rgb, dtype=np.float32)

        if base_bg_tuple is not None and float(np.linalg.norm(p_rgb - bg_vec)) > 2.0:
            seg_v = p_rgb - bg_vec
            seg_len2 = float(np.dot(seg_v, seg_v))
            proj_u = np.clip(
                np.sum((rgb_f - bg_vec[None, None, :]) * seg_v[None, None, :], axis=2) / max(seg_len2, 1e-6),
                0.0,
                1.0,
            )
            closest = bg_vec[None, None, :] + proj_u[..., None] * seg_v[None, None, :]
            dist_seg = np.linalg.norm(rgb_f - closest, axis=2)
        else:
            closest = np.broadcast_to(p_rgb[None, None, :], (h, w, 3)).copy()
            dist_seg = np.linalg.norm(rgb_f - p_rgb[None, None, :], axis=2)

        # Boost chromatic mismatch from the picked color family when Deepen Difference is active
        closest_u8 = np.clip(np.round(closest), 0, 255).astype(np.uint8)
        closest_lab = cv2.cvtColor(closest_u8, cv2.COLOR_RGB2Lab).astype(np.float32)
        d_pick_ab = lab[..., 1:] - closest_lab[..., 1:]
        pick_chroma = np.sqrt(d_pick_ab[..., 0] ** 2 + d_pick_ab[..., 1] ** 2)
        dist_eff = dist_seg * (1.0 + 0.65 * s * np.clip((pick_chroma - 6.0) / 14.0, 0.0, 1.5))

        p_lum = float(p_rgb.mean())
        anchor_lum = min(bg_lum_ref, p_lum) if base_bg_tuple is not None else p_lum
        pick_dark_barrier = _build_contrast_barrier(lum, anchor_lum, delta=42.0)
        pick_barrier = pick_dark_barrier | (grad_mag >= 16.0)

        pick_core_tol = float(pick.tolerance) + 8.0 * s
        pick_soft_tol = pick_core_tol + 36.0 - 6.0 * s

        core_cand = (dist_eff <= pick_core_tol) & (~pick_barrier)
        core_cand[py, px] = True
        _, lbl_p = cv2.connectedComponents(core_cand.astype(np.uint8), connectivity=4)
        seed_id = int(lbl_p[py, px])
        selected_core = lbl_p == seed_id

        # If clicked region connects to existing transparent background, also include sibling
        # background-adjacent segments (e.g. floor shadow parts between wheels), while never touching enclosed objects.
        bg_dil = cv2.dilate((alpha_f < 0.25).astype(np.uint8), kern5, iterations=1) > 0
        if np.any(selected_core & bg_dil):
            touching_ids = set(np.unique(lbl_p[core_cand & bg_dil])) - {0}
            if touching_ids:
                selected_core |= np.isin(lbl_p, list(touching_ids))

        soft_cand = selected_core | ((dist_eff <= pick_soft_tol) & (~pick_barrier))
        _, slbl_p = cv2.connectedComponents(soft_cand.astype(np.uint8), connectivity=4)
        sel_sids = set(np.unique(slbl_p[selected_core])) - {0}
        pick_zone = np.isin(slbl_p, list(sel_sids))
        pick_aa = (
            (cv2.dilate(pick_zone.astype(np.uint8), kern3, iterations=1) > 0)
            & (~pick_zone)
            & (~pick_dark_barrier)
        )

        pt = np.clip(
            (dist_eff - pick_core_tol * 0.72) / max(pick_soft_tol - pick_core_tol * 0.72, 6.0),
            0.0,
            1.0,
        )
        p_alpha = pt * pt * (3.0 - 2.0 * pt)
        paa_t = np.clip((dist_eff - pick_core_tol * 0.50) / (pick_soft_tol + 12.0), 0.0, 1.0)

        drop_zone = pick_zone & (p_alpha < alpha_f)
        drop_aa = pick_aa & (paa_t < alpha_f)
        ref_bg_map[drop_zone] = closest[drop_zone]
        ref_bg_map[drop_aa] = closest[drop_aa]

        alpha_f = np.where(pick_zone, np.minimum(alpha_f, p_alpha), alpha_f)
        alpha_f = np.where(pick_aa, np.minimum(alpha_f, paa_t), alpha_f)

    # Phase 3: Foreground color unmixing + pull-push smoothing for faint gradient tails
    fringe = (alpha_f >= 0.08) & (alpha_f < 0.98)
    if np.any(fringe):
        a_exp = np.maximum(alpha_f[fringe, None], 0.14)
        unmixed = (rgb_f[fringe] - (1.0 - alpha_f[fringe, None]) * ref_bg_map[fringe]) / a_exp
        rgb_f[fringe] = np.clip(unmixed, 0.0, 255.0)

    faint = (alpha_f > 0.008) & (alpha_f < 0.12)
    solid_enough = alpha_f >= 0.12
    if np.any(faint) and np.any(solid_enough):
        extrap_rgb = _pull_push_extrapolate_rgb(rgb_f, solid_enough, kernel_sizes=(5, 11))
        rgb_f[faint] = extrap_rgb[faint]

    out = arr.copy()
    out[..., :3] = np.clip(np.round(rgb_f), 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(np.round(alpha_f * 255.0), 0, 255).astype(np.uint8)
    out[out[..., 3] == 0, :3] = 0
    return out


def _trim_margins(
    arr: np.ndarray,
    border_rgb: Optional[Tuple[int, int, int]],
    padding_pct: float,
) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    """Crop outer empty margins while preserving aspect-safe configurable padding."""
    h, w = arr.shape[:2]
    alpha = arr[..., 3]
    if np.any(alpha < 250):
        fg_mask = alpha > 8
    elif border_rgb is not None:
        bg_vec = np.array(border_rgb, dtype=np.float32)
        dist = np.linalg.norm(arr[..., :3].astype(np.float32) - bg_vec, axis=2)
        fg_mask = dist > 8.0
    else:
        return arr, (0, 0, w, h)

    ys, xs = np.where(fg_mask)
    if ys.size == 0 or xs.size == 0:
        return arr, (0, 0, w, h)

    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    pad = int(round(max(y1 - y0, x1 - x0) * (max(0.0, padding_pct) / 100.0)))

    crop_x0 = max(0, x0 - pad)
    crop_y0 = max(0, y0 - pad)
    crop_x1 = min(w, x1 + pad)
    crop_y1 = min(h, y1 + pad)
    return arr[crop_y0:crop_y1, crop_x0:crop_x1], (crop_x0, crop_y0, crop_x1, crop_y1)


def make_square_icon_master(
    im_rgba: Image.Image,
    *,
    bg_mode: str,
    border_rgb: Optional[Tuple[int, int, int]],
    min_size: int = 1024,
) -> Image.Image:
    """Center-pad onto a square >= 1024x1024 canvas without ever stretching aspect ratio."""
    w, h = im_rgba.size
    side = max(w, h)
    if w != h:
        alpha_arr = np.asarray(im_rgba.getchannel("A"))
        is_opaque_faithful = (
            bg_mode == "faithful" and border_rgb is not None and bool(np.all(alpha_arr >= 250))
        )
        fill = (*border_rgb, 255) if (is_opaque_faithful and border_rgb is not None) else (0, 0, 0, 0)
        sq = Image.new("RGBA", (side, side), fill)
        sq.paste(im_rgba, ((side - w) // 2, (side - h) // 2), im_rgba)
    else:
        sq = im_rgba

    if side < min_size:
        sq = sq.resize((min_size, min_size), Image.Resampling.LANCZOS)
    return sq


def normalize_square_crop(
    box: Tuple[int, int, int, int] | Tuple[int, int, int],
    width: int,
    height: int,
    *,
    min_side: int = 1,
) -> Tuple[int, int, int, int]:
    """Clamp and snap a `(x0, y0, x1, y1)` or `(x0, y0, side)` box to a strict 1:1 square."""
    w = max(1, int(width))
    h = max(1, int(height))
    max_side = max(1, min(w, h))
    eff_min = max(1, min(int(min_side), max_side))
    if len(box) == 3:
        raw_s = int(box[2])
    else:
        raw_s = min(int(box[2]) - int(box[0]), int(box[3]) - int(box[1]))
    side = int(np.clip(raw_s, eff_min, max_side))
    cx0 = int(np.clip(int(box[0]), 0, max(0, w - side)))
    cy0 = int(np.clip(int(box[1]), 0, max(0, h - side)))
    return (cx0, cy0, cx0 + side, cy0 + side)


def compute_square_crop_drag(
    orig_box: Tuple[int, int, int, int],
    handle: str,
    dx: float,
    dy: float,
    width: int,
    height: int,
    *,
    min_side: int = 16,
) -> Tuple[int, int, int, int]:
    """Update a 1:1 square crop box from a handle/move drag, keeping equal side lengths."""
    w = max(1, int(width))
    h = max(1, int(height))
    ox0, oy0, ox1, oy1 = normalize_square_crop(orig_box, w, h, min_side=min_side)
    oside = ox1 - ox0
    max_dim = max(1, min(w, h))
    eff_min = max(1, min(int(min_side), max_dim))

    if handle == "move":
        nx0 = int(np.clip(round(ox0 + dx), 0, max(0, w - oside)))
        ny0 = int(np.clip(round(oy0 + dy), 0, max(0, h - oside)))
        return (nx0, ny0, nx0 + oside, ny0 + oside)
    if handle == "se":
        ax, ay = ox0, oy0
        d = dx if abs(dx) >= abs(dy) else dy
        max_s = max(eff_min, min(w - ax, h - ay))
        side = int(np.clip(round(oside + d), eff_min, max_s))
        return (ax, ay, ax + side, ay + side)
    if handle == "nw":
        ax, ay = ox1, oy1
        ox_d, oy_d = -dx, -dy
        d = ox_d if abs(ox_d) >= abs(oy_d) else oy_d
        max_s = max(eff_min, min(ax, ay))
        side = int(np.clip(round(oside + d), eff_min, max_s))
        return (ax - side, ay - side, ax, ay)
    if handle == "ne":
        ax, ay = ox0, oy1
        ox_d, oy_d = dx, -dy
        d = ox_d if abs(ox_d) >= abs(oy_d) else oy_d
        max_s = max(eff_min, min(w - ax, ay))
        side = int(np.clip(round(oside + d), eff_min, max_s))
        return (ax, ay - side, ax + side, ay)
    if handle == "sw":
        ax, ay = ox1, oy0
        ox_d, oy_d = -dx, dy
        d = ox_d if abs(ox_d) >= abs(oy_d) else oy_d
        max_s = max(eff_min, min(ax, h - ay))
        side = int(np.clip(round(oside + d), eff_min, max_s))
        return (ax - side, ay, ax, ay + side)
    if handle == "e":
        max_s = max(eff_min, min(w - ox0, h))
        side = int(np.clip(round(oside + dx), eff_min, max_s))
        cy_mid = 0.5 * (oy0 + oy1)
        ny0 = int(np.clip(round(cy_mid - 0.5 * side), 0, max(0, h - side)))
        return (ox0, ny0, ox0 + side, ny0 + side)
    if handle == "w":
        max_s = max(eff_min, min(ox1, h))
        side = int(np.clip(round(oside - dx), eff_min, max_s))
        cy_mid = 0.5 * (oy0 + oy1)
        ny0 = int(np.clip(round(cy_mid - 0.5 * side), 0, max(0, h - side)))
        return (ox1 - side, ny0, ox1, ny0 + side)
    if handle == "s":
        max_s = max(eff_min, min(h - oy0, w))
        side = int(np.clip(round(oside + dy), eff_min, max_s))
        cx_mid = 0.5 * (ox0 + ox1)
        nx0 = int(np.clip(round(cx_mid - 0.5 * side), 0, max(0, w - side)))
        return (nx0, oy0, nx0 + side, oy0 + side)
    if handle == "n":
        max_s = max(eff_min, min(oy1, w))
        side = int(np.clip(round(oside - dy), eff_min, max_s))
        cx_mid = 0.5 * (ox0 + ox1)
        nx0 = int(np.clip(round(cx_mid - 0.5 * side), 0, max(0, w - side)))
        return (nx0, oy1 - side, nx0 + side, oy1)
    return (ox0, oy0, ox1, oy1)


def apply_rounded_corners_and_border(
    im: Image.Image,
    *,
    rounded_corners: bool,
    corner_radius_pct: float = 18.0,
    rounded_border: bool = False,
    border_width_pct: float = 3.0,
    border_color_rgb: Tuple[int, int, int] = (76, 194, 255),
) -> Image.Image:
    """Apply smooth 4x-supersampled rounded corners and/or a custom-colored inward border."""
    use_round = bool(rounded_corners and corner_radius_pct > 0.0)
    use_border = bool(rounded_border and border_width_pct > 0.0)
    if not use_round and not use_border:
        return im.convert("RGBA")

    rgba = im.convert("RGBA")
    w, h = rgba.size
    min_dim = float(min(w, h))
    rad_pct = float(np.clip(corner_radius_pct, 0.0, 50.0)) if use_round else 0.0
    bw_pct = float(np.clip(border_width_pct, 0.5, 25.0)) if use_border else 0.0

    ss = 4
    w4, h4 = w * ss, h * ss
    r4 = int(round(min_dim * (rad_pct / 100.0) * ss))

    outer_4x = Image.new("L", (w4, h4), 0)
    d_outer = ImageDraw.Draw(outer_4x)
    if r4 > 0:
        d_outer.rounded_rectangle([0, 0, w4 - 1, h4 - 1], radius=r4, fill=255)
    else:
        d_outer.rectangle([0, 0, w4 - 1, h4 - 1], fill=255)
    outer_mask = outer_4x.resize((w, h), Image.Resampling.LANCZOS)

    out = rgba.copy()
    if r4 > 0:
        orig_a = out.getchannel("A")
        out.putalpha(ImageChops.multiply(orig_a, outer_mask))

    if use_border:
        bw4 = max(ss, int(round(min_dim * (bw_pct / 100.0) * ss)))
        inner_4x = Image.new("L", (w4, h4), 0)
        if w4 - 2 * bw4 > 1 and h4 - 2 * bw4 > 1:
            d_inner = ImageDraw.Draw(inner_4x)
            r_in4 = max(0, r4 - bw4)
            if r_in4 > 0:
                d_inner.rounded_rectangle([bw4, bw4, w4 - 1 - bw4, h4 - 1 - bw4], radius=r_in4, fill=255)
            else:
                d_inner.rectangle([bw4, bw4, w4 - 1 - bw4, h4 - 1 - bw4], fill=255)
        ring_4x = ImageChops.subtract(outer_4x, inner_4x)
        ring_mask = ring_4x.resize((w, h), Image.Resampling.LANCZOS)
        br, bg_c, bb = (int(np.clip(c, 0, 255)) for c in border_color_rgb[:3])
        border_layer = Image.new("RGBA", (w, h), (br, bg_c, bb, 0))
        border_layer.putalpha(ring_mask)
        out = Image.alpha_composite(out, border_layer)

    return out


def prepare_master_image(source_rgba: Image.Image, opts: PrepareOptions) -> PreparedMaster:
    """Preprocess source artwork according to square crop, background mode, denoising, and trim options."""
    src = source_rgba.convert("RGBA")
    arr = np.array(src, dtype=np.uint8)
    orig_size = src.size
    orig_w, orig_h = orig_size
    crop_box: Tuple[int, int, int, int] = (0, 0, orig_w, orig_h)
    eff_opts = opts

    if opts.square_crop is not None:
        cx0, cy0, cx1, cy1 = normalize_square_crop(opts.square_crop, orig_w, orig_h, min_side=1)
        arr = arr[cy0:cy1, cx0:cx1].copy()
        crop_box = (cx0, cy0, cx1, cy1)
        cw = cx1 - cx0
        ch = cy1 - cy0
        if opts.picked_transparencies:
            remapped: List[TransparencyPick] = []
            for pick in opts.picked_transparencies:
                sx = pick.x_norm * max(orig_w - 1, 1)
                sy = pick.y_norm * max(orig_h - 1, 1)
                rx_i = int(round(sx))
                ry_i = int(round(sy))
                if cx0 <= rx_i < cx1 and cy0 <= ry_i < cy1:
                    remapped.append(
                        TransparencyPick(
                            x_norm=float(np.clip((sx - cx0) / max(cw - 1, 1), 0.0, 1.0)),
                            y_norm=float(np.clip((sy - cy0) / max(ch - 1, 1), 0.0, 1.0)),
                            rgb=pick.rgb,
                            tolerance=pick.tolerance,
                        )
                    )
            eff_opts = replace(opts, picked_transparencies=tuple(remapped))

    if eff_opts.clean_jpeg_noise:
        arr[..., :3] = _apply_flat_jpeg_denoise(arr[..., :3])

    border_rgb = _detect_uniform_border_rgb(arr)

    if eff_opts.bg_mode == "faithful":
        if border_rgb is not None:
            bg_vec = np.array(border_rgb, dtype=np.float32)
            dist = np.linalg.norm(arr[..., :3].astype(np.float32) - bg_vec, axis=2)
            ext_mask = _edge_connected_mask(dist <= 2.5)
            arr[ext_mask, 0] = border_rgb[0]
            arr[ext_mask, 1] = border_rgb[1]
            arr[ext_mask, 2] = border_rgb[2]
            arr[ext_mask, 3] = 255
    elif eff_opts.bg_mode == "transparent":
        arr = _apply_smart_transparency_matte(arr, border_rgb, eff_opts)
    elif eff_opts.bg_mode == "squircle":
        if border_rgb is not None:
            bg_vec = np.array(border_rgb, dtype=np.float32)
            dist = np.linalg.norm(arr[..., :3].astype(np.float32) - bg_vec, axis=2)
            ext_mask = _edge_connected_mask(dist <= 2.5)
            arr[ext_mask, 0] = border_rgb[0]
            arr[ext_mask, 1] = border_rgb[1]
            arr[ext_mask, 2] = border_rgb[2]

    if eff_opts.auto_trim:
        arr, rel_box = _trim_margins(arr, border_rgb, eff_opts.padding_pct)
        crop_box = (
            crop_box[0] + rel_box[0],
            crop_box[1] + rel_box[1],
            crop_box[0] + rel_box[2],
            crop_box[1] + rel_box[3],
        )

    out_im = Image.fromarray(arr, mode="RGBA")
    if eff_opts.bg_mode == "squircle":
        out_im = _apply_squircle_badge(out_im, border_rgb or (255, 255, 255))

    unframed_im = out_im
    use_round = bool(eff_opts.rounded_corners and eff_opts.corner_radius_pct > 0.0)
    use_border = bool(eff_opts.rounded_border and eff_opts.border_width_pct > 0.0)
    if use_round or use_border:
        out_im = apply_rounded_corners_and_border(
            unframed_im,
            rounded_corners=use_round,
            corner_radius_pct=eff_opts.corner_radius_pct,
            rounded_border=use_border,
            border_width_pct=eff_opts.border_width_pct,
            border_color_rgb=eff_opts.border_color_rgb,
        )

    has_transparency = bool(np.any(np.asarray(out_im.getchannel("A")) < 252))
    square_icon = make_square_icon_master(
        unframed_im if (use_round or use_border) else out_im,
        bg_mode=opts.bg_mode,
        border_rgb=border_rgb,
        min_size=1024,
    )
    if use_round or use_border:
        square_icon = apply_rounded_corners_and_border(
            square_icon,
            rounded_corners=use_round,
            corner_radius_pct=eff_opts.corner_radius_pct,
            rounded_border=use_border,
            border_width_pct=eff_opts.border_width_pct,
            border_color_rgb=eff_opts.border_color_rgb,
        )

    return PreparedMaster(
        image=out_im,
        square_icon=square_icon,
        border_rgb=border_rgb,
        has_transparency=has_transparency,
        source_size=orig_size,
        crop_box=crop_box,
        unframed_image=unframed_im,
        rounded_corners=use_round,
        corner_radius_pct=float(eff_opts.corner_radius_pct) if use_round else 0.0,
        rounded_border=use_border,
        border_width_pct=float(eff_opts.border_width_pct) if use_border else 0.0,
        border_color_rgb=tuple(int(c) for c in eff_opts.border_color_rgb[:3]),
    )


# ---------------------------------------------------------------------------
# Faithful Vector .SVG Engine
# ---------------------------------------------------------------------------


def _normalize_svg_root(
    svg_text: str,
    *,
    trace_w: int,
    trace_h: int,
    display_w: int,
    display_h: int,
    base_bg_hex: Optional[str],
) -> str:
    """Ensure explicit viewBox, display dimensions, XML namespace, and optional seamless base fill."""
    root_match = re.search(r"<svg\b[^>]*>", svg_text, flags=re.IGNORECASE | re.DOTALL)
    replacement_tag = (
        f'<svg version="1.1" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {trace_w} {trace_h}" width="{display_w}" height="{display_h}">'
    )
    if root_match:
        normalized = svg_text[: root_match.start()] + replacement_tag + svg_text[root_match.end() :]
    else:
        normalized = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f"{replacement_tag}\n{svg_text}\n</svg>\n"
        )

    if base_bg_hex:
        insert_pos = normalized.find(">", normalized.find("<svg")) + 1
        bg_rect = f'\n<rect width="{trace_w}" height="{trace_h}" fill="{base_bg_hex}"/>'
        normalized = normalized[:insert_pos] + bg_rect + normalized[insert_pos:]

    return normalized


def _fallback_opencv_svg(im_rgba: Image.Image, opts: SVGOptions) -> str:
    """Pure OpenCV + NumPy multi-layer Bezier path fallback when vtracer is unavailable."""
    arr = np.array(im_rgba)
    h, w = arr.shape[:2]
    alpha = arr[..., 3]
    opaque_mask = alpha >= 16

    num_colors = max(6, min(48, 4 + opts.color_precision * 4))
    pts = arr[opaque_mask, :3].astype(np.float32)
    if pts.shape[0] == 0:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg version="1.1" xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {w} {h}" width="{w}" height="{h}"></svg>\n'
        )

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 16, 1.0)
    k = min(num_colors, int(pts.shape[0]))
    _, labels, centers = cv2.kmeans(pts, k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    centers_u8 = np.clip(np.round(centers), 0, 255).astype(np.uint8)

    label_img = np.full((h, w), -1, dtype=np.int32)
    label_img[opaque_mask] = labels.ravel()

    # Paint largest area color layers first (painter's order)
    order = sorted(range(k), key=lambda idx: int(np.sum(label_img == idx)), reverse=True)
    paths: List[str] = []
    min_area = max(1.0, float(opts.filter_speckle) * 2.0)

    for cid in order:
        mask = (label_img == cid).astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        r, g, b = (int(centers_u8[cid, 0]), int(centers_u8[cid, 1]), int(centers_u8[cid, 2]))
        hex_col = f"#{r:02X}{g:02X}{b:02X}"
        d_parts: List[str] = []
        for cnt in contours:
            if abs(cv2.contourArea(cnt)) < min_area or len(cnt) < 3:
                continue
            eps = 0.7 if opts.mode != "pixel" else 0.1
            approx = cv2.approxPolyDP(cnt, eps, True)
            pts_c = approx.reshape(-1, 2)
            if len(pts_c) < 3:
                continue
            if opts.mode == "spline" and len(pts_c) >= 4:
                # Smooth quadratic midpoint spline through polygon vertices
                mid0 = (pts_c[-1] + pts_c[0]) / 2.0
                segs = [f"M{mid0[0]:.1f},{mid0[1]:.1f}"]
                n_pts = len(pts_c)
                for i in range(n_pts):
                    p0 = pts_c[i]
                    p1 = pts_c[(i + 1) % n_pts]
                    mid = (p0 + p1) / 2.0
                    segs.append(f"Q{p0[0]:.1f},{p0[1]:.1f} {mid[0]:.1f},{mid[1]:.1f}")
                segs.append("Z")
                d_parts.append("".join(segs))
            else:
                segs = [f"M{pts_c[0, 0]},{pts_c[0, 1]}"]
                for pt in pts_c[1:]:
                    segs.append(f"L{pt[0]},{pt[1]}")
                segs.append("Z")
                d_parts.append("".join(segs))
        if d_parts:
            paths.append(f'<path d="{"".join(d_parts)}" fill="{hex_col}"/>')

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg version="1.1" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {w} {h}" width="{w}" height="{h}">\n'
        + "\n".join(paths)
        + "\n</svg>\n"
    )


def _contours_to_svg_path(
    mask_u8: np.ndarray,
    *,
    sx: float = 1.0,
    sy: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    eps: float = 0.55,
    min_area: float = 4.0,
    spline: bool = True,
) -> str:
    """Convert a binary uint8 mask into an SVG path `d` string supporting holes via evenodd."""
    mh, mw = mask_u8.shape[:2]
    if spline and offset_x == 0.0 and offset_y == 0.0:
        up_scale = 2
        uw, uh = mw * up_scale, mh * up_scale
        m_up = cv2.resize(mask_u8, (uw, uh), interpolation=cv2.INTER_LINEAR)
        m_up = cv2.GaussianBlur(m_up, (3, 3), 0.75, borderType=cv2.BORDER_REPLICATE)
        trace_mask = ((m_up >= 128) * 255).astype(np.uint8)
        eff_sx = sx / float(up_scale)
        eff_sy = sy / float(up_scale)
        eff_eps = eps * float(up_scale)
        eff_min_area = min_area * float(up_scale * up_scale)
    else:
        up_scale = 1
        uw, uh = mw, mh
        trace_mask = mask_u8
        eff_sx, eff_sy = sx, sy
        eff_eps, eff_min_area = eps, min_area

    contours, _ = cv2.findContours(trace_mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return ""
    parts: List[str] = []
    max_r = 2.0 * max(sx, sy)
    sharp_len = 4.0 * max(sx, sy)
    for cnt in contours:
        if len(cnt) < 3 or abs(cv2.contourArea(cnt)) < eff_min_area:
            continue
        approx = cv2.approxPolyDP(cnt, eff_eps, True)
        raw = approx.reshape(-1, 2).astype(np.float32)
        if len(raw) < 3:
            continue
        if up_scale > 1:
            px = np.where(
                raw[:, 0] >= uw - 1,
                float(uw),
                np.where(raw[:, 0] <= 0, 0.0, raw[:, 0] + 0.5),
            ) * eff_sx
            py = np.where(
                raw[:, 1] >= uh - 1,
                float(uh),
                np.where(raw[:, 1] <= 0, 0.0, raw[:, 1] + 0.5),
            ) * eff_sy
        else:
            lx = np.where(
                raw[:, 0] >= mw - 1,
                float(mw),
                np.where(raw[:, 0] <= 0, 0.0, raw[:, 0] + 0.5),
            )
            ly = np.where(
                raw[:, 1] >= mh - 1,
                float(mh),
                np.where(raw[:, 1] <= 0, 0.0, raw[:, 1] + 0.5),
            )
            px = (lx + offset_x) * eff_sx
            py = (ly + offset_y) * eff_sy
        pts = np.column_stack([px, py])
        n_pts = len(pts)
        if spline and n_pts >= 4:
            a_in = np.zeros_like(pts)
            a_out = np.zeros_like(pts)
            rounded = [False] * n_pts
            for idx in range(n_pts):
                p_prev = pts[(idx - 1) % n_pts]
                p_curr = pts[idx]
                p_next = pts[(idx + 1) % n_pts]
                vin = p_curr - p_prev
                vout = p_next - p_curr
                lin = float(math.hypot(float(vin[0]), float(vin[1])))
                lout = float(math.hypot(float(vout[0]), float(vout[1])))
                if lin > 1e-4 and lout > 1e-4:
                    cos_t = float(vin[0] * vout[0] + vin[1] * vout[1]) / (lin * lout)
                    if not (cos_t <= 0.25 and max(lin, lout) > sharp_len):
                        r = min(max_r, 0.45 * lin, 0.45 * lout)
                        if r > 0.15:
                            a_in[idx] = p_curr - (vin / lin) * r
                            a_out[idx] = p_curr + (vout / lout) * r
                            rounded[idx] = True
            start_pt = a_in[0] if rounded[0] else pts[0]
            segs = [f"M{start_pt[0]:.2f},{start_pt[1]:.2f}"]
            prev_exit = start_pt
            for idx in range(n_pts):
                if idx > 0:
                    entry = a_in[idx] if rounded[idx] else pts[idx]
                    if math.hypot(float(entry[0] - prev_exit[0]), float(entry[1] - prev_exit[1])) > 0.02:
                        segs.append(f"L{entry[0]:.2f},{entry[1]:.2f}")
                if rounded[idx]:
                    segs.append(f"Q{pts[idx, 0]:.2f},{pts[idx, 1]:.2f} {a_out[idx, 0]:.2f},{a_out[idx, 1]:.2f}")
                    prev_exit = a_out[idx]
                else:
                    prev_exit = pts[idx]
            segs.append("Z")
            parts.append("".join(segs))
        else:
            segs = [f"M{pts[0, 0]:.2f},{pts[0, 1]:.2f}"]
            for pt in pts[1:]:
                segs.append(f"L{pt[0]:.2f},{pt[1]:.2f}")
            segs.append("Z")
            parts.append("".join(segs))
    return "".join(parts)


def _sample_gradient_stops(
    param: np.ndarray,
    pts_rgb: np.ndarray,
    stops_t: np.ndarray,
    band_half: float = 0.08,
) -> np.ndarray:
    """Compute robust median RGB color stops along normalized parameter `param` in [0, 1]."""
    out = np.zeros((len(stops_t), 3), dtype=np.float32)
    for si, st in enumerate(stops_t):
        band = np.abs(param - st) <= band_half
        if np.any(band):
            out[si] = np.median(pts_rgb[band], axis=0)
        else:
            out[si] = pts_rgb[int(np.argmin(np.abs(param - st)))]
    return np.clip(out, 0.0, 255.0)


def _stops_to_xml(stops_t: np.ndarray, stops_rgb: np.ndarray) -> str:
    return "".join(
        f'<stop offset="{st * 100.0:.1f}%" '
        f'stop-color="#{int(round(c[0])):02X}{int(round(c[1])):02X}{int(round(c[2])):02X}"/>'
        for st, c in zip(stops_t, stops_rgb)
    )


def _eval_focal_radial_t(
    xs_f: np.ndarray,
    ys_f: np.ndarray,
    cx: float,
    cy: float,
    r: float,
    fx: float,
    fy: float,
) -> Tuple[np.ndarray, float, float]:
    """Compute SVG `<radialGradient>` normalized parameter `t` in `[0, 1]` with focal point `(fx, fy)`."""
    odx, ody = fx - cx, fy - cy
    odist = math.hypot(odx, ody)
    if odist > 0.90 * r:
        scale_o = (0.90 * r) / max(odist, 1e-6)
        fx = cx + odx * scale_o
        fy = cy + ody * scale_o
        odx, ody = fx - cx, fy - cy
    dx = xs_f - fx
    dy = ys_f - fy
    d2 = dx * dx + dy * dy
    o_dot_d = odx * dx + ody * dy
    denom = max(r * r - (odx * odx + ody * ody), 1e-4)
    disc = np.maximum(o_dot_d * o_dot_d + denom * d2, 0.0)
    t = (o_dot_d + np.sqrt(disc)) / denom
    return np.clip(t, 0.0, 1.0), fx, fy


def _build_vector_field_overlay(
    cid: int,
    reg: np.ndarray,
    stats_row: np.ndarray,
    rgb_f: np.ndarray,
    d_reg: str,
    sx: float,
    sy: float,
) -> Tuple[List[str], str]:
    """Build a pure-vector `<clipPath>` + sRGB `<feGaussianBlur>` adaptive patch mesh for 2D freeform gradients."""
    sh, sw = rgb_f.shape[:2]
    x0 = int(stats_row[cv2.CC_STAT_LEFT])
    y0 = int(stats_row[cv2.CC_STAT_TOP])
    bw = int(stats_row[cv2.CC_STAT_WIDTH])
    bh = int(stats_row[cv2.CC_STAT_HEIGHT])

    base_step = 2 if max(bw, bh) < 42 else (3 if max(bw, bh) < 96 else 4)
    step_px = max(base_step, int(math.ceil(max(bw, bh) / 56.0)), 2)
    pad = step_px * 3

    rx0, ry0 = max(0, x0 - pad), max(0, y0 - pad)
    rx1, ry1 = min(sw, x0 + bw + pad), min(sh, y0 + bh + pad)
    roi_rgb = rgb_f[ry0:ry1, rx0:rx1]
    roi_mask = reg[ry0:ry1, rx0:rx1]
    ext_roi = _pull_push_extrapolate_rgb(roi_rgb, roi_mask, kernel_sizes=(5, 11, 23, 45))

    gh = max(1, int(math.ceil(ext_roi.shape[0] / float(step_px))))
    gw = max(1, int(math.ceil(ext_roi.shape[1] / float(step_px))))
    coarse = cv2.resize(ext_roi, (gw, gh), interpolation=cv2.INTER_AREA)
    coarse_mask = cv2.resize(roi_mask.astype(np.uint8) * 255, (gw, gh), interpolation=cv2.INTER_AREA) > 8

    # Mild unsharp pre-compensation offsets attenuation from subsequent Gaussian blurring.
    c_blur = cv2.GaussianBlur(coarse, (0, 0), 0.85)
    coarse_sharp = np.clip(coarse + 0.48 * (coarse - c_blur), 0.0, 255.0).astype(np.float32)

    active_pts = coarse_sharp[coarse_mask] if np.any(coarse_mask) else coarse_sharp.reshape(-1, 3)
    k_pal = int(np.clip(min(36, max(12, len(active_pts) // 4)), 8, 36))
    if len(active_pts) <= k_pal:
        centers_f = active_pts
    else:
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 18, 0.5)
        _, _, centers_f = cv2.kmeans(active_pts, k_pal, None, criteria, 2, cv2.KMEANS_PP_CENTERS)

    all_cells = coarse_sharp.reshape(-1, 3)
    dists = np.sum((all_cells[:, None, :] - centers_f[None, :, :]) ** 2, axis=2)
    grid_lbl = np.argmin(dists, axis=1).reshape(gh, gw)
    centers_u8 = np.clip(np.round(centers_f), 0, 255).astype(np.uint8)

    counts = np.bincount(grid_lbl.ravel(), minlength=len(centers_u8))
    base_k = int(np.argmax(counts))
    fx0 = (rx0 - pad) * sx
    fy0 = (ry0 - pad) * sy
    fw = ((rx1 - rx0) + 2 * pad) * sx
    fh = ((ry1 - ry0) + 2 * pad) * sy

    br, bg_c, bb = int(centers_u8[base_k, 0]), int(centers_u8[base_k, 1]), int(centers_u8[base_k, 2])
    elems = [
        f'<rect x="{fx0:.2f}" y="{fy0:.2f}" width="{fw:.2f}" height="{fh:.2f}" '
        f'fill="#{br:02X}{bg_c:02X}{bb:02X}"/>'
    ]

    for kid in range(len(centers_u8)):
        if kid == base_k or counts[kid] == 0:
            continue
        ds: List[str] = []
        for gy in range(gh):
            row = grid_lbl[gy]
            if not np.any(row == kid):
                continue
            y_top = ((ry0 - pad) if gy == 0 else (ry0 + gy * step_px)) * sy
            y_bot = ((ry1 + pad) if gy == gh - 1 else min(ry1, ry0 + (gy + 1) * step_px)) * sy
            gx = 0
            while gx < gw:
                if row[gx] != kid:
                    gx += 1
                    continue
                gx_end = gx + 1
                while gx_end < gw and row[gx_end] == kid:
                    gx_end += 1
                x_left = ((rx0 - pad) if gx == 0 else (rx0 + gx * step_px)) * sx
                x_right = ((rx1 + pad) if gx_end == gw else min(rx1, rx0 + gx_end * step_px)) * sx
                ds.append(f"M{x_left:.2f},{y_top:.2f}H{x_right:.2f}V{y_bot:.2f}H{x_left:.2f}Z")
                gx = gx_end
        if ds:
            kr, kg, kb = int(centers_u8[kid, 0]), int(centers_u8[kid, 1]), int(centers_u8[kid, 2])
            elems.append(f'<path d="{"".join(ds)}" fill="#{kr:02X}{kg:02X}{kb:02X}"/>')

    sd_x = step_px * 0.85 * sx
    sd_y = step_px * 0.85 * sy
    cp_id = f"cp_{cid}"
    gb_id = f"gb_{cid}"
    defs = [
        f'<clipPath id="{cp_id}"><path d="{d_reg}" clip-rule="evenodd"/></clipPath>',
        f'<filter id="{gb_id}" filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB" '
        f'x="{fx0:.2f}" y="{fy0:.2f}" width="{fw:.2f}" height="{fh:.2f}">'
        f'<feGaussianBlur stdDeviation="{sd_x:.2f} {sd_y:.2f}"/></filter>',
    ]
    overlay = f'<g clip-path="url(#{cp_id})"><g filter="url(#{gb_id})">{"".join(elems)}</g></g>'
    return defs, overlay


def _build_gradient_defs_and_overlays(
    seg_rgb_u8: np.ndarray,
    vis_mask: np.ndarray,
    grad_mag: np.ndarray,
    border_rgb: Optional[Tuple[int, int, int]],
    *,
    sx: float,
    sy: float,
    tw: int,
    th: int,
    spline: bool,
) -> Tuple[List[str], List[str], np.ndarray, np.ndarray]:
    """
    Detect smooth gradient compartments, fit SVG `<linearGradient>` / focal `<radialGradient>`
    definitions (or clipped pure-vector sRGB Gaussian meshes for 2D freeform gradients),
    and return `(defs_xml, overlay_paths, inner_flatten_mask, inner_median_rgb)`.
    """
    _ = (border_rgb, tw, th)
    sh, sw = seg_rgb_u8.shape[:2]
    rgb_f = seg_rgb_u8.astype(np.float32)

    smooth_mask = (grad_mag < 13.0) & vis_mask
    num_cc, labels, stats, _ = cv2.connectedComponentsWithStats(
        smooth_mask.astype(np.uint8), connectivity=4
    )

    kern3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    defs_xml: List[str] = []
    overlays: List[str] = []
    flatten_mask = np.zeros((sh, sw), dtype=bool)
    flatten_rgb = np.zeros((sh, sw, 3), dtype=np.uint8)
    stops_t = np.linspace(0.0, 1.0, 11)

    for cid in range(1, num_cc):
        area = int(stats[cid, cv2.CC_STAT_AREA])
        if area < 75:
            continue
        reg = labels == cid
        dt_full = cv2.distanceTransform(reg.astype(np.uint8), cv2.DIST_L2, 3)
        dt_max = float(dt_full.max())
        if dt_max < 2.5:
            continue

        ys, xs = np.where(reg)
        pts_rgb = rgb_f[ys, xs]
        span96 = float(np.linalg.norm(np.percentile(pts_rgb, 96, axis=0) - np.percentile(pts_rgb, 4, axis=0)))
        span995 = float(np.linalg.norm(np.percentile(pts_rgb, 99.5, axis=0) - np.percentile(pts_rgb, 0.5, axis=0)))
        if span96 < 16.0 and span995 < 26.0:
            continue
        flat_rmse = float(np.sqrt(np.mean((pts_rgb - pts_rgb.mean(axis=0)) ** 2)))
        if flat_rmse < 3.2 and span995 < 28.0:
            continue
        if dt_max < 4.2 and span96 < 24.0:
            continue

        reg_u8 = reg.astype(np.uint8) * 255
        d_reg = _contours_to_svg_path(reg_u8, sx=sx, sy=sy, eps=0.42, min_area=4.0, spline=spline)
        if not d_reg:
            continue

        xs_f = xs.astype(np.float32)
        ys_f = ys.astype(np.float32)

        # 1. Fit 11-stop linear gradient via SVD on spatial RGB least-squares slope
        design = np.column_stack([xs_f, ys_f, np.ones(len(xs_f), dtype=np.float32)])
        coeffs, _, _, _ = np.linalg.lstsq(design, pts_rgb, rcond=None)
        u_svd, _, _ = np.linalg.svd(coeffs[:2, :], full_matrices=False)
        dx, dy = float(u_svd[0, 0]), float(u_svd[1, 0])
        proj = xs_f * dx + ys_f * dy
        p0, p1 = float(proj.min()), float(proj.max())
        tl = (proj - p0) / max(p1 - p0, 1e-4)
        lin_stops = _sample_gradient_stops(tl, pts_rgb, stops_t, band_half=0.06)
        pred_lin = np.column_stack([np.interp(tl, stops_t, lin_stops[:, ch]) for ch in range(3)])
        lin_rmse = float(np.sqrt(np.mean((pts_rgb - pred_lin) ** 2)))

        # 2. Fit 11-stop focal radial gradient (concentric + off-center spherical extrema)
        lum = pts_rgb.mean(axis=1)
        cx_g = 0.5 * (float(xs_f.min()) + float(xs_f.max()))
        cy_g = 0.5 * (float(ys_f.min()) + float(ys_f.max()))
        r_g = max(float(np.percentile(np.sqrt((xs_f - cx_g) ** 2 + (ys_f - cy_g) ** 2), 99.5)), 2.5)
        hi_mask = lum >= np.percentile(lum, 94)
        lo_mask = lum <= np.percentile(lum, 6)
        fx_hi, fy_hi = float(xs_f[hi_mask].mean()), float(ys_f[hi_mask].mean())
        fx_lo, fy_lo = float(xs_f[lo_mask].mean()), float(ys_f[lo_mask].mean())
        r_hi = max(float(np.percentile(np.sqrt((xs_f - fx_hi) ** 2 + (ys_f - fy_hi) ** 2), 99.5)), 2.5)

        cand_centers = [
            (cx_g, cy_g, r_g, cx_g, cy_g),
            (cx_g, cy_g, r_g * 1.05, fx_hi, fy_hi),
            (cx_g, cy_g, r_g * 1.05, fx_lo, fy_lo),
            (fx_hi, fy_hi, r_hi, fx_hi, fy_hi),
        ]
        best_rad_rmse = 999.0
        best_rad_params: Optional[Tuple[float, float, float, float, float, np.ndarray]] = None
        for cxc, cyc, rc, fxc, fyc in cand_centers:
            tr, fxc_c, fyc_c = _eval_focal_radial_t(xs_f, ys_f, cxc, cyc, rc, fxc, fyc)
            rad_stops = _sample_gradient_stops(tr, pts_rgb, stops_t, band_half=0.06)
            pred_rad = np.column_stack([np.interp(tr, stops_t, rad_stops[:, ch]) for ch in range(3)])
            r_rmse = float(np.sqrt(np.mean((pts_rgb - pred_rad) ** 2)))
            if r_rmse < best_rad_rmse:
                best_rad_rmse = r_rmse
                best_rad_params = (cxc, cyc, rc, fxc_c, fyc_c, rad_stops)

        best_rmse = min(lin_rmse, best_rad_rmse)
        inner_erode = cv2.erode(reg_u8, kern3, iterations=1) > 0
        med_u8 = np.clip(np.round(np.median(pts_rgb, axis=0)), 0, 255).astype(np.uint8)

        if best_rmse <= 3.8 and best_rmse < flat_rmse * 0.65:
            if best_rad_rmse < lin_rmse and best_rad_params is not None:
                cxc, cyc, rc, fxc_c, fyc_c, rad_stops = best_rad_params
                gid = f"rg_{cid}"
                stops_xml = _stops_to_xml(stops_t, rad_stops)
                defs_xml.append(
                    f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                    f'cx="{cxc * sx:.2f}" cy="{cyc * sy:.2f}" r="{rc * max(sx, sy):.2f}" '
                    f'fx="{fxc_c * sx:.2f}" fy="{fyc_c * sy:.2f}">'
                    f"{stops_xml}</radialGradient>"
                )
            else:
                gid = f"lg_{cid}"
                mx, my = float(xs_f.mean()), float(ys_f.mean())
                pm = mx * dx + my * dy
                x1, y1 = (mx + (p0 - pm) * dx) * sx, (my + (p0 - pm) * dy) * sy
                x2, y2 = (mx + (p1 - pm) * dx) * sx, (my + (p1 - pm) * dy) * sy
                stops_xml = _stops_to_xml(stops_t, lin_stops)
                defs_xml.append(
                    f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                    f'x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}">'
                    f"{stops_xml}</linearGradient>"
                )
            overlays.append(f'<path d="{d_reg}" fill="url(#{gid})" fill-rule="evenodd"/>')
        else:
            # 3. Pure-vector clipped sRGB Gaussian color-field mesh for curved / 2D freeform gradients
            mesh_defs, mesh_overlay = _build_vector_field_overlay(
                cid, reg, stats[cid], rgb_f, d_reg, sx, sy
            )
            defs_xml.extend(mesh_defs)
            overlays.append(mesh_overlay)

        flatten_mask |= inner_erode
        flatten_rgb[inner_erode] = med_u8

    return defs_xml, overlays, flatten_mask, flatten_rgb


def _build_svg_alpha_mask_defs(
    alpha_u8: np.ndarray,
    *,
    sx: float,
    sy: float,
    tw: int,
    th: int,
    spline: bool,
) -> Optional[str]:
    """Build pure-vector `<mask>` definitions representing continuous alpha & sharp silhouettes."""
    if not np.any((alpha_u8 >= 8) & (alpha_u8 <= 248)):
        return None

    levels = (10, 28, 52, 80, 110, 140, 170, 200, 226, 246)
    soft_paths: List[str] = []
    for lv in levels:
        d = _contours_to_svg_path(
            ((alpha_u8 >= lv) * 255).astype(np.uint8),
            sx=sx,
            sy=sy,
            eps=0.70,
            min_area=3.0,
            spline=spline,
        )
        if d:
            soft_paths.append(f'<path d="{d}" fill="#{lv:02X}{lv:02X}{lv:02X}" fill-rule="evenodd"/>')

    if not soft_paths:
        return None

    agx = cv2.Sobel(alpha_u8.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3)
    agy = cv2.Sobel(alpha_u8.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3)
    agmag = np.sqrt(agx * agx + agy * agy) / 4.0
    kern3 = np.ones((3, 3), dtype=np.uint8)
    sharp_edge = cv2.dilate((agmag >= 32.0).astype(np.uint8), kern3, iterations=1) > 0

    sharp_zero = (alpha_u8 <= 4) & sharp_edge
    sharp_zero_d = _contours_to_svg_path(
        (sharp_zero.astype(np.uint8) * 255), sx=sx, sy=sy, eps=0.5, min_area=3.0, spline=spline
    )
    sharp_zero_xml = f'<path d="{sharp_zero_d}" fill="#000000" fill-rule="evenodd"/>' if sharp_zero_d else ""

    opaque_u8 = (alpha_u8 >= 248).astype(np.uint8)
    core_mask = (opaque_u8 > 0) & (sharp_edge | (cv2.erode(opaque_u8, kern3, iterations=1) > 0))
    core_d = _contours_to_svg_path(
        (core_mask.astype(np.uint8) * 255), sx=sx, sy=sy, eps=0.45, min_area=3.0, spline=spline
    )
    core_xml = f'<path d="{core_d}" fill="#FFFFFF" fill-rule="evenodd"/>' if core_d else ""

    blur_sd = 1.5 * max(sx, sy)
    return (
        f'<filter id="svg-alpha-blur" x="-5%" y="-5%" width="110%" height="110%">'
        f'<feGaussianBlur stdDeviation="{blur_sd:.2f}"/></filter>'
        f'<mask id="svg-alpha-matte" maskUnits="userSpaceOnUse" x="0" y="0" width="{tw}" height="{th}">'
        f'<rect width="{tw}" height="{th}" fill="#000000"/>'
        f'<g filter="url(#svg-alpha-blur)">{"".join(soft_paths)}</g>'
        f"{sharp_zero_xml}{core_xml}"
        f"</mask>"
    )


def _upscale_and_dealias_for_trace(
    rgb_u8: np.ndarray,
    tw: int,
    th: int,
    *,
    clean_aa_lines: bool,
) -> np.ndarray:
    """
    Smooth low-gradient stroke-interior noise while preserving edges, upscale cleanly
    with bicubic interpolation, and tighten high-contrast step-edge anti-aliasing via
    a single selective luma-guided Kramer-Bruckner shock pass.
    """
    h, w = rgb_u8.shape[:2]
    if clean_aa_lines:
        _, grad_mag = _compute_lab_gradient(rgb_u8)
        bf = cv2.bilateralFilter(rgb_u8, d=5, sigmaColor=12, sigmaSpace=3.5)
        w_s = np.clip((10.5 - grad_mag) / 4.0, 0.0, 1.0)[..., None]
        clean = np.clip(
            np.round(rgb_u8.astype(np.float32) * (1.0 - w_s) + bf.astype(np.float32) * w_s),
            0,
            255,
        ).astype(np.uint8)
    else:
        clean = rgb_u8

    if (tw, th) != (w, h):
        up_rgb = cv2.resize(clean, (tw, th), interpolation=cv2.INTER_CUBIC)
    else:
        up_rgb = clean.copy()

    scale_factor = max(tw / float(max(w, 1)), th / float(max(h, 1)))
    if clean_aa_lines and scale_factor >= 1.5:
        k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        emin = cv2.erode(up_rgb, k3)
        emax = cv2.dilate(up_rgb, k3)
        mask = np.max(cv2.subtract(emax, emin), axis=2) >= 40
        if np.any(mask):
            luma = cv2.cvtColor(up_rgb, cv2.COLOR_RGB2GRAY)
            lgx = cv2.Sobel(luma, cv2.CV_16S, 1, 0, ksize=3)[mask].astype(np.float32)
            lgy = cv2.Sobel(luma, cv2.CV_16S, 0, 1, ksize=3)[mask].astype(np.float32)
            cgx = cv2.Sobel(up_rgb, cv2.CV_16S, 1, 0, ksize=3)[mask].astype(np.float32)
            cgy = cv2.Sobel(up_rgb, cv2.CV_16S, 0, 1, ksize=3)[mask].astype(np.float32)
            same_dir = (cgx * lgx[:, None] + cgy * lgy[:, None]) >= 0.0

            em_min = emin[mask].astype(np.float32)
            em_max = emax[mask].astype(np.float32)
            c_low = np.where(same_dir, em_min, em_max)
            c_high = np.where(same_dir, em_max, em_min)
            p = up_rgb[mask].astype(np.float32)

            vec = c_high - c_low
            len2 = np.sum(vec * vec, axis=1)
            t = np.sum((p - c_low) * vec, axis=1) / np.maximum(len2, 1e-4)
            target = np.where((t <= 0.5)[:, None], c_low, c_high)
            w_edge = (np.clip((np.sqrt(len2) - 40.0) / 35.0, 0.0, 1.0) * 0.55)[:, None]
            up_rgb[mask] = np.clip(np.round(p * (1.0 - w_edge) + target * w_edge), 0, 255).astype(np.uint8)

    return up_rgb


def convert_image_to_svg(
    prepared: PreparedMaster,
    opts: SVGOptions,
    *,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> str:
    """Convert a prepared RGBA master into a pure-vector scalable SVG string."""
    use_round_vec = bool(prepared.rounded_corners or prepared.rounded_border)
    img = (
        prepared.unframed_image
        if (use_round_vec and prepared.unframed_image is not None)
        else prepared.image
    )
    orig_w, orig_h = img.size

    def _prog(pct: float, msg: str) -> None:
        if progress_cb is not None:
            progress_cb(pct, msg)

    scale = max(1.0, float(opts.trace_scale))
    if max(orig_w, orig_h) * scale > MAX_TRACE_DIM:
        scale = max(1.0, MAX_TRACE_DIM / float(max(orig_w, orig_h)))

    tw = max(1, int(round(orig_w * scale)))
    th = max(1, int(round(orig_h * scale)))

    src_arr = np.array(img, dtype=np.uint8)
    unframed_has_trans = bool(np.any(src_arr[..., 3] < 252)) if use_round_vec else prepared.has_transparency

    # Work at segmentation resolution (<= 1024) for fast, crisp gradient & mask extraction
    if max(orig_w, orig_h) > 1024:
        r = 1024.0 / float(max(orig_w, orig_h))
        seg_w = max(1, int(round(orig_w * r)))
        seg_h = max(1, int(round(orig_h * r)))
        seg_arr = cv2.resize(src_arr, (seg_w, seg_h), interpolation=cv2.INTER_AREA)
    else:
        seg_w, seg_h = orig_w, orig_h
        seg_arr = src_arr

    seg_rgb = seg_arr[..., :3]
    seg_alpha = seg_arr[..., 3]
    sx = tw / float(max(seg_w, 1))
    sy = th / float(max(seg_h, 1))

    _prog(18, "Analyzing SVG gradients & alpha masks...")

    # Extrapolate clean foreground RGB across faint/transparent rims so traces never acquire dark fringes
    if unframed_has_trans:
        vis_core = seg_alpha >= 10
        if np.any(vis_core):
            seg_rgb_clean = np.clip(
                np.round(_pull_push_extrapolate_rgb(seg_rgb.astype(np.float32), vis_core, kernel_sizes=(5, 9))),
                0,
                255,
            ).astype(np.uint8)
        else:
            seg_rgb_clean = seg_rgb
    else:
        seg_rgb_clean = seg_rgb

    _, seg_grad = _compute_lab_gradient(seg_rgb_clean)

    all_defs: List[str] = []
    grad_overlays: List[str] = []
    flatten_mask_seg = np.zeros((seg_h, seg_w), dtype=bool)
    flatten_rgb_seg = np.zeros((seg_h, seg_w, 3), dtype=np.uint8)

    alpha_mask_xml: Optional[str] = None
    if unframed_has_trans:
        alpha_mask_xml = _build_svg_alpha_mask_defs(
            seg_alpha, sx=sx, sy=sy, tw=tw, th=th, spline=(opts.mode != "pixel")
        )
        if alpha_mask_xml is not None:
            all_defs.append(alpha_mask_xml)

    use_gradients = bool(opts.true_gradients and opts.mode != "pixel")
    if use_gradients:
        if unframed_has_trans:
            vis_seg = (seg_alpha >= 12) if alpha_mask_xml is not None else (seg_alpha >= 245)
        else:
            vis_seg = np.ones((seg_h, seg_w), dtype=bool)
        g_defs, g_overlays, flatten_mask_seg, flatten_rgb_seg = _build_gradient_defs_and_overlays(
            seg_rgb_clean,
            vis_seg,
            seg_grad,
            None if unframed_has_trans else prepared.border_rgb,
            sx=sx,
            sy=sy,
            tw=tw,
            th=th,
            spline=(opts.mode == "spline"),
        )
        all_defs.extend(g_defs)
        grad_overlays.extend(g_overlays)

    _prog(30, "De-aliasing edges for vector trace...")

    # Prepare clean 1x RGB source (extrapolated across alpha rims) then upscale & de-alias for tracing
    base_rgb_1x = src_arr[..., :3]
    src_alpha_1x = src_arr[..., 3]
    if unframed_has_trans:
        vis_1x = src_alpha_1x >= 10
        if np.any(vis_1x):
            extrap_1x = _pull_push_extrapolate_rgb(base_rgb_1x.astype(np.float32), vis_1x, kernel_sizes=(5, 9))
            base_rgb_1x = np.clip(np.round(extrap_1x), 0, 255).astype(np.uint8)

    trace_rgb = _upscale_and_dealias_for_trace(
        base_rgb_1x,
        tw,
        th,
        clean_aa_lines=bool(opts.clean_aa_lines and opts.mode != "pixel"),
    )
    if (tw, th) != (orig_w, orig_h):
        trace_alpha = cv2.resize(src_alpha_1x, (tw, th), interpolation=cv2.INTER_CUBIC)
    else:
        trace_alpha = src_alpha_1x.copy()

    if unframed_has_trans:
        if alpha_mask_xml is not None:
            # Make visible rim slightly dilated & binary opaque so the pure-vector alpha mask fades cleanly
            vis_dil = cv2.dilate((trace_alpha >= 6).astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1) > 0
            trace_alpha = np.where(vis_dil, 255, 0).astype(np.uint8)
        else:
            trace_alpha[trace_alpha < 16] = 0

    trace_arr = np.dstack([trace_rgb, trace_alpha])

    if use_gradients and np.any(flatten_mask_seg):
        # Flatten interior of fitted SVG gradient regions so vtracer skips redundant contour bands
        fm_big = cv2.resize((flatten_mask_seg.astype(np.uint8) * 255), (tw, th), interpolation=cv2.INTER_LINEAR) >= 128
        frgb_big = cv2.resize(flatten_rgb_seg, (tw, th), interpolation=cv2.INTER_NEAREST)
        trace_arr[fm_big, :3] = frgb_big[fm_big]

    trace_img = Image.fromarray(trace_arr, mode="RGBA")
    eff_layer_diff = int(opts.layer_difference)
    eff_speckle = max(1, int(round(int(opts.filter_speckle) * scale))) if int(opts.filter_speckle) > 0 else 0

    _prog(38, "Tracing scalable vector .SVG paths...")

    if vtracer is not None and hasattr(vtracer, "Config") and hasattr(vtracer, "convert_pixels"):
        cfg_dict: Dict[str, Any] = {
            "clustering": "color-cluster",
            "hierarchical": opts.hierarchical,
            "mode": opts.mode,
            "filter_speckle": eff_speckle,
            "color_precision": int(opts.color_precision),
            "layer_difference": eff_layer_diff,
            "corner_threshold": int(opts.corner_threshold),
            "length_threshold": float(opts.length_threshold),
            "max_iterations": int(opts.max_iterations),
            "splice_threshold": int(opts.splice_threshold),
            "simplify": opts.simplify,
            "path_precision": int(opts.path_precision),
            "optimize": int(opts.optimize),
        }
        rgba_bytes = trace_img.tobytes()
        if _should_offload_native_gil():
            try:
                raw_svg = _NATIVE_SVG_WORKER.request(("vtracer_pixels", rgba_bytes, tw, th, cfg_dict))
            except (OSError, EOFError, BrokenPipeError):
                cfg = vtracer.Config(**cfg_dict)
                raw_svg = vtracer.convert_pixels(rgba_bytes, tw, th, config=cfg)
        else:
            cfg = vtracer.Config(**cfg_dict)
            raw_svg = vtracer.convert_pixels(rgba_bytes, tw, th, config=cfg)
    elif vtracer is not None and hasattr(vtracer, "convert_raw_image_to_svg"):
        buf = io.BytesIO()
        trace_img.save(buf, format="PNG")
        vt_mode = "none" if opts.mode == "pixel" else opts.mode
        kw_dict: Dict[str, Any] = {
            "img_format": "png",
            "colormode": "color",
            "hierarchical": opts.hierarchical,
            "mode": vt_mode,
            "filter_speckle": eff_speckle,
            "color_precision": int(opts.color_precision),
            "layer_difference": eff_layer_diff,
            "corner_threshold": int(opts.corner_threshold),
            "length_threshold": float(opts.length_threshold),
            "max_iterations": int(opts.max_iterations),
            "splice_threshold": int(opts.splice_threshold),
            "path_precision": int(opts.path_precision),
        }
        png_payload = buf.getvalue()
        if _should_offload_native_gil():
            try:
                raw_svg = _NATIVE_SVG_WORKER.request(("vtracer_raw", png_payload, kw_dict))
            except (OSError, EOFError, BrokenPipeError):
                raw_svg = vtracer.convert_raw_image_to_svg(png_payload, **kw_dict)
        else:
            raw_svg = vtracer.convert_raw_image_to_svg(png_payload, **kw_dict)
    else:
        raw_svg = _fallback_opencv_svg(trace_img, opts)

    base_bg_hex: Optional[str] = None
    if not unframed_has_trans and prepared.border_rgb is not None:
        r, g, b = prepared.border_rgb
        base_bg_hex = f"#{r:02X}{g:02X}{b:02X}"

    svg_out = _normalize_svg_root(
        raw_svg,
        trace_w=tw,
        trace_h=th,
        display_w=orig_w,
        display_h=orig_h,
        base_bg_hex=base_bg_hex,
    )

    round_border_xml = ""
    if use_round_vec:
        min_t = float(min(tw, th))
        rad_pct = float(np.clip(prepared.corner_radius_pct, 0.0, 50.0)) if prepared.rounded_corners else 0.0
        r_t = min_t * (rad_pct / 100.0)
        all_defs.append(
            f'<clipPath id="svg-rounded-clip">'
            f'<rect x="0" y="0" width="{tw}" height="{th}" rx="{r_t:.2f}" ry="{r_t:.2f}"/>'
            f'</clipPath>\n'
        )
        if prepared.rounded_border and prepared.border_width_pct > 0.0:
            bw_pct = float(np.clip(prepared.border_width_pct, 0.5, 25.0))
            bw_t = max(scale, min_t * (bw_pct / 100.0))
            br, bg_c, bb = (int(np.clip(c, 0, 255)) for c in prepared.border_color_rgb[:3])
            border_hex = f"#{br:02X}{bg_c:02X}{bb:02X}"
            round_border_xml = (
                f'\n<rect id="svg-rounded-border" x="0" y="0" width="{tw}" height="{th}" '
                f'rx="{r_t:.2f}" ry="{r_t:.2f}" fill="none" stroke="{border_hex}" '
                f'stroke-width="{2.0 * bw_t:.2f}"/>'
            )

    if grad_overlays or all_defs or use_round_vec:
        root_end = svg_out.find(">", svg_out.find("<svg")) + 1
        close_pos = svg_out.rfind("</svg>")
        inner_body = svg_out[root_end:close_pos]
        defs_block = f'\n<defs>\n{"".join(all_defs)}\n</defs>' if all_defs else ""
        content_block = inner_body + ("\n" + "\n".join(grad_overlays) if grad_overlays else "")
        if alpha_mask_xml is not None:
            content_block = f'\n<g mask="url(#svg-alpha-matte)">{content_block}\n</g>'
        if use_round_vec:
            content_block = f'\n<g clip-path="url(#svg-rounded-clip)">{content_block}{round_border_xml}\n</g>'
        svg_out = svg_out[:root_end] + defs_block + content_block + "\n</svg>\n"

    return svg_out


def render_svg_to_pil(svg_text: str, width: Optional[int] = None, height: Optional[int] = None) -> Image.Image:
    """Rasterize SVG markup to an RGBA PIL Image via `resvg_py`."""
    if resvg_py is None:
        raise RuntimeError("resvg_py is required to rasterize SVG previews.")
    if _should_offload_native_gil():
        try:
            png_bytes = _NATIVE_SVG_WORKER.request(("resvg", svg_text, width, height))
            with Image.open(io.BytesIO(png_bytes)) as im:
                return im.convert("RGBA")
        except (OSError, EOFError, BrokenPipeError):
            pass
    kwargs: Dict[str, Any] = {"svg_string": svg_text}
    if width is not None:
        kwargs["width"] = int(width)
    if height is not None:
        kwargs["height"] = int(height)
    png_bytes = resvg_py.svg_to_bytes(**kwargs)
    with Image.open(io.BytesIO(png_bytes)) as im:
        return im.convert("RGBA")


def compute_fidelity_metrics(
    reference_rgba: Image.Image,
    rendered_rgba: Image.Image,
    svg_text: str,
) -> Tuple[float, float, int, int]:
    """Return (psnr_db, similarity_pct, path_count, svg_byte_len)."""
    if rendered_rgba.size != reference_rgba.size:
        rendered_rgba = rendered_rgba.resize(reference_rgba.size, Image.Resampling.LANCZOS)

    ref_arr = np.asarray(reference_rgba, dtype=np.float64)
    ren_arr = np.asarray(rendered_rgba, dtype=np.float64)

    # Composite alpha onto neutral white for fair visual comparison when transparent
    ref_a = ref_arr[..., 3:4] / 255.0
    ren_a = ren_arr[..., 3:4] / 255.0
    ref_comp = ref_arr[..., :3] * ref_a + 255.0 * (1.0 - ref_a)
    ren_comp = ren_arr[..., :3] * ren_a + 255.0 * (1.0 - ren_a)

    # Include alpha channel difference so transparency accuracy counts too
    ref_4 = np.concatenate([ref_comp, ref_arr[..., 3:4]], axis=2)
    ren_4 = np.concatenate([ren_comp, ren_arr[..., 3:4]], axis=2)

    mse = float(np.mean((ref_4 - ren_4) ** 2))
    psnr_db = 99.9 if mse < 1e-10 else 10.0 * math.log10((255.0 * 255.0) / mse)
    mae = float(np.mean(np.abs(ref_4 - ren_4)))
    similarity_pct = max(0.0, min(100.0, 100.0 * (1.0 - mae / 255.0)))
    path_count = len(re.findall(r"<path\b", svg_text))
    svg_bytes = len(svg_text.encode("utf-8"))
    return psnr_db, similarity_pct, path_count, svg_bytes


# ---------------------------------------------------------------------------
# Complete Multi-Size Apple .ICNS Compiler & Verifier
# ---------------------------------------------------------------------------


def _sharpen_small_icon(im_rgba: Image.Image, size: int) -> Image.Image:
    """Apply subtle RGB-only unsharp mask for small icon sizes while keeping clean alpha."""
    if size > 128:
        return im_rgba
    if size <= 32:
        filt = ImageFilter.UnsharpMask(radius=0.6, percent=45, threshold=2)
    elif size <= 64:
        filt = ImageFilter.UnsharpMask(radius=0.65, percent=30, threshold=2)
    else:
        filt = ImageFilter.UnsharpMask(radius=0.7, percent=18, threshold=2)

    r, g, b, a = im_rgba.split()
    rgb_sharp = Image.merge("RGB", (r, g, b)).filter(filt)
    rs, gs, bs = rgb_sharp.split()
    return Image.merge("RGBA", (rs, gs, bs, a))


def render_icon_mipmaps(
    square_master: Image.Image,
    sizes: Iterable[int],
    *,
    small_icon_sharpen: bool = True,
) -> Dict[int, Image.Image]:
    """Generate crisp mipmaps using progressive 2x Lanczos halving from >= 1024x1024 master."""
    base = square_master.convert("RGBA")
    if base.width != base.height:
        side = max(base.width, base.height)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(base, ((side - base.width) // 2, (side - base.height) // 2), base)
        base = sq
    if base.width < 1024:
        base = base.resize((1024, 1024), Image.Resampling.LANCZOS)

    # Progressive halving pyramid: 1024 -> 512 -> 256 -> 128 -> 64 -> 32 -> 16
    pyramid: Dict[int, Image.Image] = {}
    cur = base
    if cur.width > 1024:
        cur = cur.resize((1024, 1024), Image.Resampling.LANCZOS)
    pyramid[1024] = cur
    for dim in (512, 256, 128, 64, 32, 16):
        cur = cur.resize((dim, dim), Image.Resampling.LANCZOS)
        pyramid[dim] = cur

    result: Dict[int, Image.Image] = {}
    for target in sorted({int(s) for s in sizes}):
        if target in pyramid:
            im = pyramid[target]
        else:
            # Find smallest pyramid level strictly >= target (e.g. 128 -> 48, 24 -> 64/32)
            higher = [s for s in sorted(pyramid.keys()) if s >= target]
            src_im = pyramid[higher[0]] if higher else pyramid[1024]
            im = src_im.resize((target, target), Image.Resampling.LANCZOS)

        if small_icon_sharpen and target <= 128:
            im = _sharpen_small_icon(im, target)
        result[target] = im
    return result


def _packbits_channel(channel: bytes) -> bytes:
    """Compress a single W*H planar byte channel using Apple ICNS PackBits-like RLE."""
    n = len(channel)
    out = bytearray()
    lit = bytearray()
    i = 0

    def _flush_lit() -> None:
        idx = 0
        while idx < len(lit):
            chunk = lit[idx : idx + 128]
            out.append(len(chunk) - 1)
            out.extend(chunk)
            idx += len(chunk)
        lit.clear()

    while i < n:
        val = channel[i]
        run_len = 1
        while i + run_len < n and run_len < 130 and channel[i + run_len] == val:
            run_len += 1
        if run_len >= 3:
            _flush_lit()
            out.append(run_len + 125)
            out.append(val)
            i += run_len
        else:
            lit.append(val)
            if len(lit) == 128:
                _flush_lit()
            i += 1

    _flush_lit()
    return bytes(out)


def encode_icns_rle24(im_rgba: Image.Image) -> bytes:
    """
    Encode planar R, G, B bytes with Apple ICNS PackBits RLE (`is32`, `il32`, `ih32`).

    Guarantees total output length != 3 * W * H so decoders like Pillow's `read_32`
    never misinterpret the stream as uncompressed interleaved RGB.
    """
    im_rgb = im_rgba.convert("RGB")
    w, h = im_rgb.size
    arr = np.asarray(im_rgb, dtype=np.uint8)
    r_enc = _packbits_channel(arr[..., 0].tobytes())
    g_enc = _packbits_channel(arr[..., 1].tobytes())
    b_enc = bytearray(_packbits_channel(arr[..., 2].tobytes()))

    if len(r_enc) + len(g_enc) + len(b_enc) == w * h * 3:
        # Add 1 harmless zero-run/literal split byte so total != 3*W*H.
        # Walk b_enc packets to split the last literal >1 byte, or re-encode last 2 bytes as literals.
        raw_b = arr[..., 2].tobytes()
        # Force first 2 bytes of B as separate 1-byte literals (adds +1 header byte cleanly)
        b_alt = bytearray()
        b_alt.append(0)
        b_alt.append(raw_b[0])
        b_alt.extend(_packbits_channel(raw_b[1:]))
        if len(r_enc) + len(g_enc) + len(b_alt) == w * h * 3:
            b_alt = bytearray()
            b_alt.append(0)
            b_alt.append(raw_b[0])
            b_alt.append(0)
            b_alt.append(raw_b[1])
            b_alt.extend(_packbits_channel(raw_b[2:]))
        b_enc = b_alt

    return r_enc + g_enc + bytes(b_enc)


def _encode_png_bytes(im_rgba: Image.Image) -> bytes:
    buf = io.BytesIO()
    im_rgba.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_icns_bytes(
    square_master: Image.Image,
    *,
    enabled_sizes: Optional[Iterable[int]] = None,
    small_icon_sharpen: bool = True,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> bytes:
    """
    Build a complete, spec-compliant Apple `.icns` binary with `TOC ` and all enabled slots.

    When all 8 size tiers `(16, 32, 48, 64, 128, 256, 512, 1024)` are enabled (default),
    all 12 `(width, height, scale)` entries supported by Apple ICNS and Pillow are populated,
    including legacy RLE24+8bit masks (`is32/s8mk`, `il32/l8mk`, `ih32/h8mk`) alongside modern PNG chunks.
    """
    selected = set(ICNS_TIERS if enabled_sizes is None else (int(s) for s in enabled_sizes))
    if not selected:
        raise ValueError("Select at least one ICNS icon size tier.")

    if progress_cb is not None:
        progress_cb(80.0, "Generating multi-size .ICNS mipmaps...")

    needed_pixels = {
        px
        for _ostype, px, _pt, _sc, _lbl, _enc in ICNS_SLOT_SPECS
        if px in selected
    }
    mipmaps = render_icon_mipmaps(
        square_master,
        needed_pixels,
        small_icon_sharpen=small_icon_sharpen,
    )

    active_specs = [spec for spec in ICNS_SLOT_SPECS if spec[1] in selected]
    chunks: List[Tuple[bytes, bytes]] = []
    for idx, (ostype, px, _pt, _sc, _lbl, _enc) in enumerate(active_specs):
        im = mipmaps[px]
        if ostype in LEGACY_MASK_TYPES:
            rle_payload = encode_icns_rle24(im)
            mask_type = LEGACY_MASK_TYPES[ostype]
            mask_payload = im.getchannel("A").tobytes()
            chunks.append((ostype, rle_payload))
            chunks.append((mask_type, mask_payload))
        else:
            chunks.append((ostype, _encode_png_bytes(im)))
        if progress_cb is not None:
            pct = 82.0 + 13.0 * float(idx + 1) / float(max(len(active_specs), 1))
            progress_cb(pct, f"Encoding .ICNS slot {px}x{px}...")

    # Build TOC chunk listing every data chunk (ostype + total chunk size)
    toc_payload = bytearray()
    for ostype, data in chunks:
        toc_payload.extend(ostype)
        toc_payload.extend(struct.pack(">I", 8 + len(data)))

    all_chunks = [(b"TOC ", bytes(toc_payload)), *chunks]
    total_len = 8 + sum(8 + len(data) for _, data in all_chunks)

    out = bytearray()
    out.extend(b"icns")
    out.extend(struct.pack(">I", total_len))
    for ostype, data in all_chunks:
        out.extend(ostype)
        out.extend(struct.pack(">I", 8 + len(data)))
        out.extend(data)
    return bytes(out)


def save_icns(
    square_master: Image.Image,
    out_path: str | Path,
    *,
    enabled_sizes: Optional[Iterable[int]] = None,
    small_icon_sharpen: bool = True,
) -> bytes:
    """Compile and write an `.icns` file to disk, returning the written bytes."""
    data = build_icns_bytes(
        square_master,
        enabled_sizes=enabled_sizes,
        small_icon_sharpen=small_icon_sharpen,
    )
    Path(out_path).write_bytes(data)
    return data


ICO_MIPMAP_SIZES: Tuple[int, ...] = (256, 128, 64, 48, 40, 32, 24, 20, 16)


def _encode_ico_dib_chunk(im: Image.Image) -> bytes:
    """Encode an RGBA image as a 32-bit BGRA BITMAPINFOHEADER + 1-bit AND mask DIB for Windows `.ico`."""
    rgba = np.asarray(im.convert("RGBA"), dtype=np.uint8)
    h, w, _ = rgba.shape
    flipped = rgba[::-1]
    bgra = np.ascontiguousarray(flipped[..., [2, 1, 0, 3]])
    xor_bytes = bgra.tobytes()

    row_bytes = ((w + 31) // 32) * 4
    alpha_trans = (flipped[..., 3] < 128).astype(np.uint8)
    packed = np.packbits(alpha_trans, axis=1, bitorder="big")
    if packed.shape[1] < row_bytes:
        pad = np.zeros((h, row_bytes - packed.shape[1]), dtype=np.uint8)
        packed = np.hstack([packed, pad])
    and_bytes = np.ascontiguousarray(packed[:, :row_bytes]).tobytes()
    bih = struct.pack(
        "<IIIHHIIIIII",
        40,
        w,
        h * 2,
        1,
        32,
        0,
        len(xor_bytes) + len(and_bytes),
        0,
        0,
        0,
        0,
    )
    return bih + xor_bytes + and_bytes


def build_ico_bytes(
    square_master: Image.Image,
    *,
    sizes: Sequence[int] = ICO_MIPMAP_SIZES,
    small_icon_sharpen: bool = True,
) -> bytes:
    """Build a multi-resolution Windows `.ico` with descending entries (256 first) and High-DPI mipmaps."""
    ordered = sorted({int(s) for s in sizes if 16 <= int(s) <= 256}, reverse=True)
    if not ordered:
        ordered = list(ICO_MIPMAP_SIZES)
    mips = render_icon_mipmaps(square_master, ordered, small_icon_sharpen=small_icon_sharpen)

    payloads: List[Tuple[int, bytes]] = []
    for sz in ordered:
        im_s = mips[sz]
        if sz >= 256:
            buf = io.BytesIO()
            im_s.save(buf, format="PNG", optimize=True)
            payloads.append((sz, buf.getvalue()))
        else:
            payloads.append((sz, _encode_ico_dib_chunk(im_s)))

    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = 6 + 16 * len(payloads)
    dir_entries: List[bytes] = []
    data_blobs: List[bytes] = []
    for sz, blob in payloads:
        wb = 0 if sz >= 256 else sz
        dir_entries.append(struct.pack("<BBBBHHII", wb, wb, 0, 0, 1, 32, len(blob), offset))
        data_blobs.append(blob)
        offset += len(blob)
    return header + b"".join(dir_entries) + b"".join(data_blobs)


def save_ico(square_master: Image.Image, out_path: str | Path) -> bytes:
    """Save a crisp multi-size Windows `.ico` (256..16 descending with 40x40 & 20x20 High-DPI slots)."""
    data = build_ico_bytes(square_master, sizes=ICO_MIPMAP_SIZES, small_icon_sharpen=True)
    Path(out_path).write_bytes(data)
    return data


def inspect_icns(source: bytes | str | Path) -> IcnsInspection:
    """Parse an `.icns` file/bytes and verify every slot via `PIL.IcnsImagePlugin.IcnsFile`."""
    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    if len(data) < 8 or data[:4] != b"icns":
        raise ValueError("Not a valid ICNS payload (missing 'icns' header).")
    declared_len = struct.unpack(">I", data[4:8])[0]
    if declared_len != len(data):
        raise ValueError(f"ICNS declared length {declared_len} != actual length {len(data)}.")

    raw_chunks: Dict[bytes, int] = {}
    has_toc = False
    pos = 8
    while pos + 8 <= len(data):
        ostype = data[pos : pos + 4]
        chunk_len = struct.unpack(">I", data[pos + 4 : pos + 8])[0]
        if chunk_len < 8 or pos + chunk_len > len(data):
            raise ValueError(f"Corrupt ICNS chunk {ostype!r} length {chunk_len}.")
        if ostype == b"TOC ":
            has_toc = True
        else:
            raw_chunks[ostype] = chunk_len
        pos += chunk_len

    # Verify round-trip decoding using Pillow's IcnsFile
    icns_file = IcnsImagePlugin.IcnsFile(io.BytesIO(data))
    verified_sizes = sorted(icns_file.itersizes(), key=lambda t: (t[0] * t[2], t[2]))
    images_by_size: Dict[Tuple[int, int, int], Image.Image] = {}
    for sz in verified_sizes:
        decoded = icns_file.getimage(sz).convert("RGBA")
        expected = (sz[0] * sz[2], sz[1] * sz[2])
        if decoded.size != expected:
            raise ValueError(f"ICNS slot {sz} decoded size {decoded.size} != expected {expected}.")
        images_by_size[sz] = decoded

    slot_rows: List[IcnsSlotInfo] = []
    for ostype, px, pt, sc, lbl, enc in ICNS_SLOT_SPECS:
        if ostype not in raw_chunks:
            continue
        key = (pt, pt, sc)
        decoded_img = images_by_size.get(key)
        slot_rows.append(
            IcnsSlotInfo(
                ostype=ostype.decode("ascii", errors="replace"),
                slot_label=lbl,
                pixel_size=px,
                scale=sc,
                encoding=enc,
                byte_length=raw_chunks[ostype] + (raw_chunks.get(LEGACY_MASK_TYPES.get(ostype, b""), 0)),
                decoded_ok=decoded_img is not None and decoded_img.size == (px, px),
                decoded_size=decoded_img.size if decoded_img else None,
                image=decoded_img,
            )
        )

    return IcnsInspection(
        total_bytes=len(data),
        has_toc=has_toc,
        chunk_count=len(raw_chunks),
        slots=slot_rows,
        verified_sizes=verified_sizes,
        images_by_size=images_by_size,
    )


# ---------------------------------------------------------------------------
# Unified Conversion Pipeline
# ---------------------------------------------------------------------------


def run_conversion(
    source_path: str | Path | Image.Image,
    *,
    prepare_opts: Optional[PrepareOptions] = None,
    svg_opts: Optional[SVGOptions] = None,
    icns_sizes: Optional[Iterable[int]] = None,
    small_icon_sharpen: bool = True,
    out_dir: Optional[str | Path] = None,
    stem: Optional[str] = None,
    write_svg: bool = True,
    write_icns: bool = True,
    write_ico: bool = False,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> ConversionResult:
    """Execute end-to-end preprocessing, SVG vectorization, ICNS assembly, and optional file export."""
    t0 = time.perf_counter()
    p_opts = prepare_opts or PrepareOptions()
    s_opts = svg_opts or SVG_PRESETS["ultra"][1]

    def _prog(pct: float, msg: str) -> None:
        if progress_cb is not None:
            progress_cb(pct, msg)

    _prog(8, "Loading & preparing source image...")
    if isinstance(source_path, Image.Image):
        src_im = source_path.convert("RGBA")
        default_stem = "converted_icon"
    else:
        src_path = Path(source_path)
        src_im = load_source_image(src_path)
        default_stem = src_path.stem

    prepared = prepare_master_image(src_im, p_opts)

    svg_text = convert_image_to_svg(prepared, s_opts, progress_cb=_prog)

    _prog(66, "Rasterizing .SVG & measuring fidelity...")
    svg_rendered = render_svg_to_pil(svg_text, width=prepared.image.width, height=prepared.image.height)
    psnr_db, sim_pct, path_count, svg_len = compute_fidelity_metrics(
        prepared.image, svg_rendered, svg_text
    )
    gradient_count = len(re.findall(r"<(?:linear|radial)Gradient\b", svg_text)) + len(
        re.findall(r'<filter\s+id="gb_', svg_text)
    )

    icns_bytes = build_icns_bytes(
        prepared.square_icon,
        enabled_sizes=icns_sizes,
        small_icon_sharpen=small_icon_sharpen,
        progress_cb=_prog,
    )
    _prog(97, "Verifying 12-slot .ICNS payload...")
    inspection = inspect_icns(icns_bytes)

    written: List[Path] = []
    if out_dir is not None:
        out_root = Path(out_dir)
        out_root.mkdir(parents=True, exist_ok=True)
        out_stem = (stem or default_stem).strip() or "icon"
        if write_svg:
            svg_out = out_root / f"{out_stem}.svg"
            svg_out.write_text(svg_text, encoding="utf-8")
            written.append(svg_out)
        if write_icns:
            icns_out = out_root / f"{out_stem}.icns"
            icns_out.write_bytes(icns_bytes)
            written.append(icns_out)
        if write_ico:
            ico_out = out_root / f"{out_stem}.ico"
            save_ico(prepared.square_icon, ico_out)
            written.append(ico_out)

    _prog(100, "Conversion complete.")
    return ConversionResult(
        source_image=src_im,
        prepared=prepared,
        svg_text=svg_text,
        svg_rendered=svg_rendered,
        psnr_db=psnr_db,
        similarity_pct=sim_pct,
        path_count=path_count,
        svg_bytes=svg_len,
        icns_bytes=icns_bytes,
        icns_inspection=inspection,
        elapsed_sec=time.perf_counter() - t0,
        gradient_count=gradient_count,
        written_files=written,
    )


# ---------------------------------------------------------------------------
# ModernTtk Desktop Studio UI
# ---------------------------------------------------------------------------


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def _make_checkerboard(width: int, height: int, tile: int = 12, dark: bool = True) -> Image.Image:
    """Create a subtle checkerboard PIL Image for visualizing alpha transparency."""
    c1 = (38, 42, 52) if dark else (228, 232, 238)
    c2 = (50, 55, 68) if dark else (246, 248, 251)
    yy, xx = np.ogrid[:height, :width]
    pat = (((xx // tile) + (yy // tile)) & 1) == 0
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[pat] = c1
    rgb[~pat] = c2
    return Image.fromarray(rgb, mode="RGB")


SPINNER_FRAME_COUNT = mtk.SPINNER_FRAME_COUNT
_DIAL_FRAMES_CACHE = mtk._DIAL_FRAMES_CACHE
build_timer_dial_frames = mtk.build_timer_dial_frames
render_timer_overlay_badge = mtk.render_timer_overlay_badge


def _parse_hex_rgb(
    hex_color: str,
    fallback: Tuple[int, int, int] = (76, 194, 255),
) -> Tuple[int, int, int]:
    """Parse a `#RRGGBB` or `#RGB` hex color into an `(R, G, B)` tuple."""
    s = str(hex_color or "").strip().lstrip("#")
    try:
        if len(s) == 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        if len(s) == 3:
            return (int(s[0] * 2, 16), int(s[1] * 2, 16), int(s[2] * 2, 16))
    except ValueError:
        pass
    return fallback


def _load_ui_font(size_px: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a clean TrueType sans-serif UI font with tabular-digit friendly metrics."""
    candidates = (
        ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
        if bold
        else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size_px)
        except Exception:
            continue
    return ImageFont.load_default()


_APP_BRAND_BUNDLE_CACHE: Optional[Tuple[Optional[Image.Image], Dict[int, Image.Image], Optional[Path]]] = None
_BRAND_BADGE_CACHE: Dict[int, Image.Image] = {}


def _ico_has_descending_256_first(ico_path: Path) -> bool:
    """Return True if `ico_path` has a valid ICO header with 256x256 at entry 0 and a 40x40 High-DPI slot."""
    try:
        if not ico_path.is_file():
            return False
        raw = ico_path.read_bytes()
        if len(raw) < 22:
            return False
        res, typ, count = struct.unpack("<HHH", raw[:6])
        if res != 0 or typ != 1 or count < 7 or len(raw) < 6 + 16 * count:
            return False
        w0 = raw[6]
        widths = {(256 if raw[6 + i * 16] == 0 else int(raw[6 + i * 16])) for i in range(count)}
        return w0 == 0 and 40 in widths and 256 in widths
    except OSError:
        return False


def resolve_app_brand_paths() -> Tuple[Optional[Path], Optional[Path], Path]:
    """Locate `Icon_generator.icns`, `Icon_generator.svg`, and `Icon_generator.ico` in `icons/`."""
    ext_icons = ensure_icons_dir()
    roots: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "icons")
    roots.append(ext_icons)
    here_icons = Path(__file__).resolve().parent / "icons"
    if here_icons not in roots:
        roots.append(here_icons)

    icns_path: Optional[Path] = None
    svg_path: Optional[Path] = None
    ico_found: Optional[Path] = None
    for icons_dir in roots:
        if not icons_dir.is_dir():
            continue
        if icns_path is None:
            for cand_icns in (icons_dir / "Icon_generator.icns", icons_dir / "icon_generator.icns"):
                if cand_icns.is_file():
                    icns_path = cand_icns
                    break
        if svg_path is None:
            for cand_svg in (icons_dir / "Icon_generator.svg", icons_dir / "icon_generator.svg"):
                if cand_svg.is_file():
                    svg_path = cand_svg
                    break
        if ico_found is None:
            for cand_ico in (icons_dir / "Icon_generator.ico", icons_dir / "icon_generator.ico"):
                if cand_ico.is_file():
                    ico_found = cand_ico
                    break
    target_ico = ico_found if (ico_found is not None and _ico_has_descending_256_first(ico_found)) else (ext_icons / "Icon_generator.ico")
    return icns_path, svg_path, target_ico


def load_app_brand_bundle() -> Tuple[Optional[Image.Image], Dict[int, Image.Image], Optional[Path]]:
    """Load and cache multi-resolution app icons from `Icon_generator.icns` / `Icon_generator.svg` / `Icon_generator.ico`."""
    global _APP_BRAND_BUNDLE_CACHE
    if _APP_BRAND_BUNDLE_CACHE is not None:
        master_c, by_px_c, ico_c = _APP_BRAND_BUNDLE_CACHE
        if ico_c is not None and _ico_has_descending_256_first(ico_c):
            return _APP_BRAND_BUNDLE_CACHE

    ext_icons = ensure_icons_dir()
    icns_path, svg_path, ico_path = resolve_app_brand_paths()
    master_im: Optional[Image.Image] = None
    by_px: Dict[int, Image.Image] = {}

    if icns_path is not None and icns_path.is_file():
        try:
            insp = inspect_icns(icns_path)
            for (w, h, sc), slot_im in insp.images_by_size.items():
                px = w * sc
                if px not in by_px or (sc > 1):
                    by_px[px] = slot_im.convert("RGBA")
            if by_px:
                max_px = max(by_px.keys())
                master_im = by_px[max_px]
        except Exception:
            pass

    if master_im is None and svg_path is not None and svg_path.is_file():
        try:
            svg_text = svg_path.read_text(encoding="utf-8", errors="replace")
            master_im = render_svg_to_pil(svg_text, width=1024, height=1024)
            by_px[1024] = master_im
        except Exception:
            pass

    if master_im is None and ico_path.is_file():
        try:
            with Image.open(ico_path) as im_ico:
                im_ico.load()
                master_im = im_ico.convert("RGBA")
        except Exception:
            pass

    if master_im is not None:
        if 1024 not in by_px:
            by_px[1024] = master_im if master_im.size == (1024, 1024) else master_im.resize((1024, 1024), Image.Resampling.LANCZOS)
        mips = render_icon_mipmaps(by_px[1024], (16, 20, 24, 32, 40, 48, 64, 128, 256, 512), small_icon_sharpen=True)
        for sz, mip_im in mips.items():
            if sz not in by_px:
                by_px[sz] = mip_im
        if not _ico_has_descending_256_first(ico_path):
            try:
                ico_path = ext_icons / "Icon_generator.ico"
                ico_path.parent.mkdir(parents=True, exist_ok=True)
                save_ico(by_px[1024], ico_path)
            except Exception:
                pass
        ext_icns = ext_icons / "Icon_generator.icns"
        if (icns_path is None or not icns_path.is_file()) and not ext_icns.is_file():
            try:
                save_icns(by_px[1024], ext_icns)
            except Exception:
                pass

    final_ico = ico_path if ico_path.is_file() else None
    _APP_BRAND_BUNDLE_CACHE = (by_px.get(1024, master_im), by_px, final_ico)
    return _APP_BRAND_BUNDLE_CACHE


def render_brand_squircle_badge(size_px: int) -> Optional[Image.Image]:
    """Render a smooth anti-aliased squircle badge of `Icon_generator` at `size_px`."""
    sz = max(16, int(size_px))
    cached = _BRAND_BADGE_CACHE.get(sz)
    if cached is not None:
        return cached
    master_im, by_px, _ = load_app_brand_bundle()
    if master_im is None:
        return None
    src_im = by_px.get(sz)
    if src_im is None:
        higher = sorted(k for k in by_px.keys() if k >= sz)
        base = by_px[higher[0]] if higher else master_im
        src_im = base.resize((sz, sz), Image.Resampling.LANCZOS)
    else:
        src_im = src_im.copy()

    if src_im.getpixel((0, 0))[3] == 0:
        out = src_im
    else:
        mask = _create_superellipse_mask(sz, sz, exponent=4.6, scale=4)
        out = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        out.paste(src_im, (0, 0), mask)

        # Add subtle 2x supersampled inner highlight rim
        ring_2x = _create_superellipse_mask(sz * 2, sz * 2, exponent=4.6, scale=2)
        inner_2x = _create_superellipse_mask(sz * 2, max(4, sz * 2 - 4), exponent=4.6, scale=2)
        rim_alpha = ImageChops.subtract(ring_2x, inner_2x).resize((sz, sz), Image.Resampling.LANCZOS)
        rim_alpha = rim_alpha.point(lambda p: int(p * 0.55))
        rim_layer = Image.new("RGBA", (sz, sz), (125, 211, 252, 0))
        rim_layer.putalpha(rim_alpha)
        out.alpha_composite(rim_layer)

    if len(_BRAND_BADGE_CACHE) >= 16:
        _BRAND_BADGE_CACHE.clear()
    _BRAND_BADGE_CACHE[sz] = out
    return out


# ==============================================================================
# GitHub Releases Auto-Updater & In-Place Executable Replacement
# (Same NTFS in-place replacement & VBS cleanup technique as Capacity Planner)
# ==============================================================================


def parse_version(version_str: str) -> Tuple[int, ...]:
    """Extract integer version tuple from semantic string like 'v1.0.1' -> (1, 0, 1)."""
    if not version_str:
        return (0,)
    clean = version_str.strip().lstrip("vV")
    nums = [int(n) for n in re.findall(r"\d+", clean)]
    return tuple(nums) if nums else (0,)


def is_newer_version(current: str, candidate: str) -> bool:
    """Return True if candidate is strictly newer than current version."""
    v_curr = parse_version(current)
    v_cand = parse_version(candidate)
    max_len = max(len(v_curr), len(v_cand))
    padded_curr = v_curr + (0,) * (max_len - len(v_curr))
    padded_cand = v_cand + (0,) * (max_len - len(v_cand))
    return padded_cand > padded_curr


@dataclass
class ReleaseInfo:
    tag_name: str
    version: str
    name: str
    body: str
    html_url: str
    asset_name: Optional[str]
    asset_url: Optional[str]
    asset_size: int
    is_newer: bool
    asset_api_url: Optional[str] = None
    asset_sha256: Optional[str] = None


def find_asset_sha256(assets: List[Dict[str, Any]], asset_name: Optional[str]) -> Optional[str]:
    """Return the expected SHA-256 hex digest for `asset_name` if the GitHub release provides one."""
    if not assets or not asset_name:
        return None
    for asset in assets:
        if asset.get("name") != asset_name:
            continue
        digest = str(asset.get("digest") or "").strip()
        if digest.lower().startswith("sha256:"):
            hex_digest = digest.split(":", 1)[1].strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", hex_digest):
                return hex_digest
    return None


def is_allowed_download_url(url: Optional[str]) -> bool:
    """Only allow update downloads over HTTPS."""
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def find_executable_asset(
    assets: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str], Optional[str], int]:
    """Find the best matching Windows `.exe` asset from GitHub release assets."""
    if not assets:
        return None, None, None, 0

    for asset in assets:
        name = str(asset.get("name", ""))
        low = name.lower()
        if low.endswith(".exe") and ("iconverter" in low or "icon" in low):
            return (
                name,
                asset.get("browser_download_url"),
                asset.get("url"),
                int(asset.get("size", 0) or 0),
            )

    for asset in assets:
        name = str(asset.get("name", ""))
        if name.lower().endswith(".exe"):
            return (
                name,
                asset.get("browser_download_url"),
                asset.get("url"),
                int(asset.get("size", 0) or 0),
            )

    return None, None, None, 0


def parse_release_payload(raw_text: str, source_url: str = "") -> Optional[Dict[str, Any]]:
    """Parse release metadata from a GitHub Releases JSON payload or HTML release manifest."""
    if not raw_text:
        return None

    stripped = raw_text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    manifest_match = re.search(
        r'<script[^>]+id=["\'](?:iconverter|rccp)-release-manifest["\'][^>]*>(.*?)</script>',
        raw_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if manifest_match:
        try:
            data = json.loads(manifest_match.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    tag_match = re.search(
        r'<meta[^>]+name=["\'](?:iconverter|rccp)-release-tag["\'][^>]+content=["\']([^"\']+)["\']',
        raw_text,
        flags=re.IGNORECASE,
    ) or re.search(r'data-release-tag=["\']([^"\']+)["\']', raw_text, flags=re.IGNORECASE)
    if not tag_match:
        return None

    tag_name = tag_match.group(1).strip()
    url_match = re.search(
        r'<meta[^>]+name=["\'](?:iconverter|rccp)-download-url["\'][^>]+content=["\']([^"\']+)["\']',
        raw_text,
        flags=re.IGNORECASE,
    ) or re.search(r'data-download-url=["\']([^"\']+)["\']', raw_text, flags=re.IGNORECASE)
    dl_url = url_match.group(1).strip() if url_match else "Iconverter.exe"
    asset_name = dl_url.rsplit("/", 1)[-1] or "Iconverter.exe"

    size_match = re.search(
        r'<meta[^>]+name=["\'](?:iconverter|rccp)-asset-size["\'][^>]+content=["\'](\d+)["\']',
        raw_text,
        flags=re.IGNORECASE,
    )
    asset_size = int(size_match.group(1)) if size_match else 0

    return {
        "tag_name": tag_name,
        "name": f"Iconverter {tag_name}",
        "body": "",
        "html_url": source_url or GITHUB_RELEASES_PAGE,
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": dl_url,
                "size": asset_size,
            }
        ],
    }


def check_for_updates(
    current_version: str = __version__,
    timeout: float = 8.0,
    api_url: str = GITHUB_API_LATEST_URL,
) -> Optional[ReleaseInfo]:
    """Query GitHub Releases (or configured update endpoint) and return `ReleaseInfo`."""
    headers = {
        "User-Agent": f"Iconverter/{current_version}",
        "Accept": "application/vnd.github+json, application/json, text/html;q=0.9",
    }
    gh_token = os.environ.get("ICONVERTER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if gh_token and gh_token.strip():
        headers["Authorization"] = f"Bearer {gh_token.strip()}"

    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None

    data = parse_release_payload(raw_text, source_url=api_url)
    if not isinstance(data, dict):
        return None

    tag_name = str(data.get("tag_name", "") or "")
    version_clean = tag_name.lstrip("vV")
    name = str(data.get("name") or tag_name)
    body = str(data.get("body", "") or "")
    html_url = str(data.get("html_url") or GITHUB_RELEASES_PAGE)
    if html_url and api_url and not html_url.startswith(("http://", "https://", "file://")):
        html_url = urljoin(api_url, html_url)

    assets = list(data.get("assets", []) or [])
    asset_name, asset_url, asset_api_url, asset_size = find_executable_asset(assets)
    if asset_url and api_url and not asset_url.startswith(("http://", "https://", "file://")):
        asset_url = urljoin(api_url, asset_url)
    if asset_api_url and api_url and not asset_api_url.startswith(("http://", "https://", "file://")):
        asset_api_url = urljoin(api_url, asset_api_url)
    if not asset_url and asset_api_url:
        asset_url = asset_api_url

    asset_sha256 = find_asset_sha256(assets, asset_name)
    newer = is_newer_version(current_version, version_clean)

    return ReleaseInfo(
        tag_name=tag_name,
        version=version_clean,
        name=name,
        body=body,
        html_url=html_url,
        asset_name=asset_name,
        asset_url=asset_url,
        asset_size=asset_size,
        is_newer=newer,
        asset_api_url=asset_api_url,
        asset_sha256=asset_sha256,
    )


def download_update_asset(
    asset_url: str,
    dest_path: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    chunk_size: int = 65536,
    expected_size: int = 0,
    expected_sha256: Optional[str] = None,
) -> bool:
    """Stream download release asset to `dest_path` with size and SHA-256 verification."""
    if not is_allowed_download_url(asset_url):
        return False

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": f"Iconverter/{__version__}"}
    gh_token = os.environ.get("ICONVERTER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if gh_token and gh_token.strip():
        headers["Authorization"] = f"Bearer {gh_token.strip()}"

    temp_file = dest_path.with_suffix(".tmp_download")
    req = urllib.request.Request(asset_url, headers=headers)
    verified = False
    try:
        with urllib.request.urlopen(req, timeout=30.0) as response:
            total_size = int(response.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            hasher = hashlib.sha256()

            with open(temp_file, "wb") as f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        return False
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size)

        if downloaded == 0:
            return False
        if total_size and downloaded != total_size:
            return False
        if expected_size and downloaded != expected_size:
            return False
        if expected_sha256 and hasher.hexdigest().lower() != expected_sha256.strip().lower():
            return False

        os.replace(temp_file, dest_path)
        verified = True
        return True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    finally:
        if not verified:
            temp_file.unlink(missing_ok=True)


def replace_executable_in_place(new_exe_path: Path) -> bool:
    """Replace the currently running executable with the new version in-place on disk.

    Windows NTFS allows renaming a running executable:
    1. Rename `current_exe` -> `current_exe.old`
    2. Move `new_exe` -> `current_exe`
    """
    if not getattr(sys, "frozen", False):
        return False

    current_exe = Path(sys.executable).resolve()
    new_exe = Path(new_exe_path).resolve()
    if not new_exe.exists():
        return False

    old_backup = current_exe.with_suffix(".exe.old")
    try:
        if old_backup.exists():
            old_backup.unlink(missing_ok=True)
        current_exe.rename(old_backup)
        new_exe.rename(current_exe)
        return True
    except OSError:
        if not current_exe.exists() and old_backup.exists():
            try:
                old_backup.rename(current_exe)
            except OSError:
                pass
        return False


def schedule_delete_old_backup(old_path: Optional[Path] = None) -> bool:
    """Spawn a detached, invisible background VBS cleaner to delete the `.exe.old` file after exit."""
    if not getattr(sys, "frozen", False):
        return False

    target_old = Path(old_path) if old_path is not None else Path(sys.executable).resolve().with_suffix(".exe.old")
    if not target_old.exists():
        return False

    vbs_file = target_old.parent / f"_clean_old_{os.getpid()}.vbs"
    esc_old = str(target_old).replace('"', '""')
    vbs_content = f'''Set fso = CreateObject("Scripting.FileSystemObject")
WScript.Sleep 1500
On Error Resume Next
For i = 1 To 25
    fso.DeleteFile "{esc_old}", True
    If Err.Number = 0 Then Exit For
    Err.Clear
    WScript.Sleep 500
Next
fso.DeleteFile WScript.ScriptFullName, True
'''
    try:
        vbs_file.write_text(vbs_content, encoding="utf-8")
        subprocess.Popen(
            ["wscript.exe", str(vbs_file)],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def cleanup_old_executable_backup() -> None:
    """Silently delete `.exe.old` backup from previous updates once unlocked."""
    if getattr(sys, "frozen", False):
        try:
            old_backup = Path(sys.executable).with_suffix(".exe.old")
            if old_backup.exists():
                old_backup.unlink(missing_ok=True)
        except OSError:
            pass


apply_inplace_update_and_restart = replace_executable_in_place

if getattr(sys, "frozen", False):
    cleanup_old_executable_backup()


class UpdatePromptDialog:
    """Modal dialog alerting the user to an available GitHub release with changelog and one-click update."""

    def __init__(self, parent: tk.Misc, release: ReleaseInfo) -> None:
        self.parent = parent
        self.release = release
        p = mtk.ThemeManager.instance().palette

        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Update Available — Iconverter v{release.version}")
        self.dialog.transient(parent.winfo_toplevel())
        self.dialog.grab_set()
        self.dialog.configure(bg=p.surface)
        mtk.ThemeManager._apply_windows_titlebar_mode(self.dialog, p.mode == "dark")

        w, h = 580, 480
        root = parent.winfo_toplevel()
        root.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        self.dialog.geometry(f"{w}x{h}+{max(20, x)}+{max(20, y)}")
        self.dialog.minsize(480, 400)

        self._build_ui()

    def _build_ui(self) -> None:
        container = mtk.Frame(self.dialog, surface_role="surface", padx=20, pady=18)
        container.pack(fill="both", expand=True)

        mtk.Label(
            container,
            text="🚀 New Version Available!",
            surface_role="surface",
            bold=True,
            font_size_delta=3,
        ).pack(anchor="w")

        mtk.Label(
            container,
            text=f"A new release of Iconverter is available: v{self.release.version} (currently on v{__version__}).",
            surface_role="surface",
            muted=True,
        ).pack(anchor="w", pady=(4, 10))

        badge_card = mtk.Card(container, surface_role="surface_alt", padding=10)
        badge_card.pack(fill="x", pady=(0, 12))
        mtk.Label(
            badge_card.body,
            text="🛡 Seamless In-Place Update",
            surface_role="surface_alt",
            bootstyle="success",
            bold=True,
        ).pack(anchor="w")
        mtk.Label(
            badge_card.body,
            text=(
                "Updating replaces the application executable in-place on disk while preserving "
                "your icons folder and exported vector assets."
            ),
            surface_role="surface_alt",
            muted=True,
            font_size_delta=-1,
        ).pack(anchor="w", pady=(2, 0))

        mtk.Label(
            container,
            text="What's New in this Release:",
            surface_role="surface",
            bold=True,
        ).pack(anchor="w", pady=(0, 6))

        self.notes_text = mtk.Textarea(
            container,
            readonly=False,
            monospace=False,
            height=160,
        )
        self.notes_text.pack(fill="both", expand=True, pady=(0, 14))
        body_content = self.release.body.strip() if self.release.body else "No changelog provided."
        self.notes_text.set(body_content)
        self.notes_text.text.configure(state="disabled")

        btn_frame = mtk.Frame(container, surface_role="surface")
        btn_frame.pack(fill="x")

        mtk.Button(
            btn_frame,
            text="Later",
            bootstyle="secondary-outline",
            command=self.dialog.destroy,
        ).pack(side="left")

        if self.release.asset_url or self.release.asset_api_url:
            mtk.Button(
                btn_frame,
                text=f"Download & Update to v{self.release.version}",
                icon="download",
                bootstyle="success",
                command=self._start_update,
            ).pack(side="right")

    def _start_update(self) -> None:
        self.dialog.destroy()
        UpdateProgressDialog(self.parent, self.release)


class UpdateProgressDialog:
    """Modal download progress dialog and in-place executable replacement handoff."""

    def __init__(self, parent: tk.Misc, release: ReleaseInfo) -> None:
        self.parent = parent
        self.release = release
        self.cancel_event = threading.Event()
        p = mtk.ThemeManager.instance().palette

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Downloading Update...")
        self.dialog.transient(parent.winfo_toplevel())
        self.dialog.grab_set()
        self.dialog.configure(bg=p.surface)
        self.dialog.resizable(False, False)
        mtk.ThemeManager._apply_windows_titlebar_mode(self.dialog, p.mode == "dark")

        w, h = 460, 210
        root = parent.winfo_toplevel()
        root.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        self.dialog.geometry(f"{w}x{h}+{max(20, x)}+{max(20, y)}")

        self._build_ui()
        self._start_download_thread()

    def _build_ui(self) -> None:
        container = mtk.Frame(self.dialog, surface_role="surface", padx=20, pady=16)
        container.pack(fill="both", expand=True)

        self.title_label = mtk.Label(
            container,
            text=f"Downloading Iconverter v{self.release.version}…",
            surface_role="surface",
            bold=True,
        )
        self.title_label.pack(anchor="w", pady=(0, 10))

        self.progress_bar = mtk.ProgressBar(
            container,
            value=0.0,
            maximum=100.0,
            mode="determinate",
            show_percent=True,
            bootstyle="success",
            width=420,
            height=20,
        )
        self.progress_bar.pack(fill="x", pady=(0, 8))

        self.status_label = mtk.Label(
            container,
            text="Connecting…",
            surface_role="surface",
            muted=True,
            font_size_delta=-1,
        )
        self.status_label.pack(anchor="w", pady=(0, 12))

        btn_frame = mtk.Frame(container, surface_role="surface")
        btn_frame.pack(fill="x")

        self.cancel_btn = mtk.Button(
            btn_frame,
            text="Cancel",
            bootstyle="secondary-outline",
            command=self._cancel,
        )
        self.cancel_btn.pack(side="right")

    def _cancel(self) -> None:
        self.cancel_event.set()
        if self.dialog.winfo_exists():
            self.dialog.destroy()

    def _start_download_thread(self) -> None:
        thread = threading.Thread(target=self._run_download, daemon=True)
        thread.start()

    def _run_download(self) -> None:
        target_url = self.release.asset_url or self.release.asset_api_url
        if not target_url:
            self.dialog.after_idle(lambda: self._on_error("No downloadable asset found for this release."))
            return

        is_frozen = getattr(sys, "frozen", False)
        if is_frozen:
            dest_dir = Path(sys.executable).resolve().parent
            dest_file = dest_dir / f"update_{self.release.version}.exe"
        else:
            dest_file = Path.home() / "Downloads" / (self.release.asset_name or f"Iconverter_{self.release.version}.exe")

        def on_progress(downloaded: int, total: int) -> None:
            if total > 0:
                pct = (downloaded / total) * 100.0
                mb_down = downloaded / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                msg = f"{mb_down:.1f} MB / {mb_tot:.1f} MB ({pct:.0f}%)"
            else:
                pct = 0.0
                mb_down = downloaded / (1024 * 1024)
                msg = f"{mb_down:.1f} MB downloaded"
            try:
                self.dialog.after_idle(lambda p=pct, m=msg: self._update_ui(p, m))
            except tk.TclError:
                pass

        success = download_update_asset(
            target_url,
            dest_file,
            progress_callback=on_progress,
            cancel_event=self.cancel_event,
            expected_size=self.release.asset_size or 0,
            expected_sha256=self.release.asset_sha256,
        )

        if not self.cancel_event.is_set():
            try:
                if success:
                    self.dialog.after_idle(lambda: self._on_success(dest_file, is_frozen))
                else:
                    self.dialog.after_idle(
                        lambda: self._on_error("Download failed, was incomplete, or failed verification.")
                    )
            except tk.TclError:
                pass

    def _update_ui(self, pct: float, msg: str) -> None:
        if self.dialog.winfo_exists():
            self.progress_bar.set(pct)
            self.status_label.configure(text=msg)

    def _on_error(self, message: str) -> None:
        if self.dialog.winfo_exists():
            self.dialog.destroy()
        messagebox.showerror("Update Error", f"Unable to complete update: {message}")

    def _on_success(self, dest_file: Path, is_frozen: bool) -> None:
        if not self.dialog.winfo_exists():
            return
        self.dialog.destroy()

        if is_frozen:
            success = replace_executable_in_place(dest_file)
            if success:
                old_backup = Path(sys.executable).resolve().with_suffix(".exe.old")
                if old_backup.exists():
                    schedule_delete_old_backup(old_backup)

                messagebox.showinfo(
                    "Update Complete",
                    f"Iconverter has been successfully updated to v{self.release.version}!\n\n"
                    "The application will now close.\n"
                    "Please relaunch the application to use the new version.\n\n"
                    "Your icons folder and saved assets remain untouched.",
                )
                root = self.parent.winfo_toplevel()
                root.destroy()
                os._exit(0)
            else:
                messagebox.showerror(
                    "Update Error",
                    f"Could not automatically overwrite the executable file.\n\n"
                    f"The downloaded update has been saved to:\n{dest_file}",
                )
        else:
            messagebox.showinfo(
                "Update Downloaded",
                f"You are running in development/script mode.\n\n"
                f"The compiled update has been saved to:\n{dest_file}",
            )


MAX_UNDO_LEVELS = 5


@dataclass(frozen=True)
class _EditorConfig:
    """Immutable snapshot of editable studio controls and framing state."""

    bg_label: str
    clean_jpeg: bool
    auto_trim: bool
    padding_pct: int
    deepen_diff: bool
    deepen_strength: int
    picked_transparencies: Tuple[TransparencyPick, ...]
    applied_square_crop: Optional[Tuple[int, int, int, int]]
    rounded_corners: bool
    corner_radius_pct: int
    rounded_border: bool
    border_width_pct: int
    border_color_rgb: Tuple[int, int, int]
    preset_label: str
    curve_label: str
    true_gradients: bool
    clean_lines: bool
    color_precision: int
    layer_difference: int
    filter_speckle: int
    enabled_tiers: Tuple[int, ...]
    small_sharpen: bool


@dataclass
class _UndoEntry:
    """History entry pairing an editor configuration with its optional cached preview."""

    config: _EditorConfig
    cached_result: Optional[ConversionResult] = None


class IconConverterApp:
    """ModernTtk interactive desktop studio for converting pictures to faithful .svg and .icns."""

    def __init__(
        self,
        *,
        initial_image: Optional[str | Path] = None,
        themename: str = "fluent_dark",
        auto_check_updates: bool = True,
    ) -> None:
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    f"Iconverter.VectorStudio.App.{__version__}"
                )
            except Exception:
                pass

        self._icons_dir = ensure_icons_dir()

        self.root = mtk.ModernWindow(
            title=f"{APP_TITLE} v{__version__}",
            themename=themename,
            geometry="1400x880",
        )
        self.root.minsize(1120, 740)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                pass
        self._app_icon_photos: List[ImageTk.PhotoImage] = []
        self._header_logo_photo: Optional[ImageTk.PhotoImage] = None
        self._apply_window_branding()

        self._source_path: Optional[Path] = None
        self._source_image: Optional[Image.Image] = None
        self._last_result: Optional[ConversionResult] = None
        self._worker_busy = False
        self._pending_refresh = False
        self._debounce_job: Optional[str] = None
        self._poll_job: Optional[str] = None
        self._ui_queue: queue.Queue[Tuple[Any, ...]] = queue.Queue()
        self._syncing_preset = False

        # Interactive comparison zoom, pan, square crop & transparency picker state
        self._zoom_mode: str = "Fit"
        self._pan_x: float = 0.0
        self._pan_y: float = 0.0
        self._drag_origin: Optional[Tuple[int, int]] = None
        self._canvas_img_geom: Dict[int, Tuple[int, int, float, int, int]] = {}
        self._canvas_crop_rect: Dict[int, Tuple[int, int, int, int]] = {}
        self._crop_mode_active: bool = False
        self._applied_square_crop: Optional[Tuple[int, int, int, int]] = None
        self._draft_square_crop: Optional[Tuple[int, int, int, int]] = None
        self._inflight_square_crop: Optional[Tuple[int, int, int, int]] = None
        self._last_result_square_crop: Optional[Tuple[int, int, int, int]] = None
        self._crop_drag_state: Optional[Tuple[str, int, int, float, Tuple[int, int, int, int]]] = None
        self._pick_mode_active: bool = False
        self._picked_transparencies: List[TransparencyPick] = []
        self._tk_refs: List[ImageTk.PhotoImage] = []
        self._icns_tk_refs: List[ImageTk.PhotoImage] = []
        self._svg_zoom_cache: Optional[Tuple[int, int, int, Image.Image]] = None
        self._busy_anim_job: Optional[str] = None
        self._busy_start_t: float = 0.0
        self._busy_pct: float = 0.0
        self._busy_pct_ceil: float = 16.0
        self._busy_frame_idx: int = 0
        self._busy_overlay_photo: Optional[ImageTk.PhotoImage] = None
        self._spinner_cache_key: Optional[Tuple[bool, Tuple[int, int, int], Tuple[int, int, int]]] = None
        self._spinner_frames: List[Image.Image] = []
        self._spinner_pill_cache: Dict[Tuple[Any, ...], Image.Image] = {}
        self._undo_stack: List[_UndoEntry] = []
        self._committed_entry: Optional[_UndoEntry] = None
        self._restoring_undo: bool = False
        self._job_generation: int = 0
        self._active_worker_gen: Optional[int] = None

        # Default paths — always ensure the `icons` folder is built on first run
        repo_root = Path(__file__).resolve().parent
        default_out = self._icons_dir
        self._var_out_dir = tk.StringVar(value=str(default_out))
        self._var_stem = tk.StringVar(value="icon")
        self._var_write_svg = tk.BooleanVar(value=True)
        self._var_write_icns = tk.BooleanVar(value=True)
        self._var_write_ico = tk.BooleanVar(value=True)

        self._var_clean_jpeg = tk.BooleanVar(value=True)
        self._var_auto_trim = tk.BooleanVar(value=False)
        self._var_deepen_diff = tk.BooleanVar(value=True)
        self._var_rounded_corners = tk.BooleanVar(value=False)
        self._var_rounded_border = tk.BooleanVar(value=False)
        self._border_color_rgb: Tuple[int, int, int] = (76, 194, 255)
        self._var_true_gradients = tk.BooleanVar(value=True)
        self._var_clean_lines = tk.BooleanVar(value=True)
        self._var_small_sharpen = tk.BooleanVar(value=True)

        self._tier_vars: Dict[int, tk.BooleanVar] = {
            sz: tk.BooleanVar(value=True) for sz in ICNS_TIERS
        }

        self._build_ui(themename)
        self._reset_undo_history()
        self._log(f"Icon & Vector Studio v{__version__} ready. Output folder: {self._icons_dir}", "INFO")
        self._poll_job = self.root.after(25, self._poll_ui_queue)
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self.root.bind("<Escape>", self._on_escape_key, add="+")
        self.root.bind("<Return>", self._on_return_key, add="+")
        self.root.bind("<Control-z>", self._on_undo_shortcut, add="+")
        self.root.bind("<Control-Z>", self._on_undo_shortcut, add="+")
        threading.Thread(target=_NATIVE_SVG_WORKER.prewarm, daemon=True).start()

        if auto_check_updates and not os.environ.get("ICONVERTER_DISABLE_AUTO_UPDATE_CHECK"):
            self._update_check_job: Optional[str] = self.root.after(2500, self._schedule_startup_update_check)
        else:
            self._update_check_job = None

        sample_candidate = repo_root / "icons" / "porter_source.jpg"
        target_img: Optional[Path] = None
        if initial_image and Path(initial_image).is_file():
            target_img = Path(initial_image)
        elif not getattr(sys, "frozen", False) and sample_candidate.is_file():
            target_img = sample_candidate

        if target_img is not None:
            self.load_image(target_img, auto_preview=True)

    # ------------------------------------------------------------------ UI Layout

    def _apply_window_branding(self) -> None:
        """Apply `Icon_generator.ico` natively on Windows (without `wm iconphoto` 16x16 overwrite)."""
        _master, by_px, ico_path = load_app_brand_bundle()
        photos: List[ImageTk.PhotoImage] = []
        for sz in (256, 128, 64, 48, 32, 16):
            im_sz = by_px.get(sz)
            if im_sz is not None:
                try:
                    photos.append(ImageTk.PhotoImage(im_sz, master=self.root))
                except Exception:
                    continue
        self._app_icon_photos = photos

        if sys.platform == "win32" and ico_path is not None and ico_path.is_file():
            try:
                self.root.iconbitmap(default=str(ico_path))
                self.root.tk.call("wm", "iconbitmap", self.root._w, str(ico_path))
            except Exception:
                pass
            try:
                from ctypes import wintypes

                self.root.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                LoadImageW = ctypes.windll.user32.LoadImageW
                LoadImageW.argtypes = [
                    wintypes.HINSTANCE,
                    wintypes.LPCWSTR,
                    wintypes.UINT,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ]
                LoadImageW.restype = wintypes.HANDLE
                SendMessageW = ctypes.windll.user32.SendMessageW
                SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                SetClassLongPtrW = getattr(
                    ctypes.windll.user32,
                    "SetClassLongPtrW",
                    ctypes.windll.user32.SetClassLongW,
                )
                SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_size_t]
                SetClassLongPtrW.restype = ctypes.c_size_t

                cx_big = int(ctypes.windll.user32.GetSystemMetrics(11)) or 48
                cy_big = int(ctypes.windll.user32.GetSystemMetrics(12)) or 48
                cx_sm = int(ctypes.windll.user32.GetSystemMetrics(49)) or 24
                cy_sm = int(ctypes.windll.user32.GetSystemMetrics(50)) or 24

                h_big = LoadImageW(None, str(ico_path), 1, cx_big, cy_big, 0x0010)
                h_sm = LoadImageW(None, str(ico_path), 1, cx_sm, cy_sm, 0x0010)
                self._win32_hicons = (h_big, h_sm)
                if h_big:
                    SendMessageW(hwnd, 0x0080, 1, ctypes.cast(h_big, ctypes.c_void_p).value or 0)
                    SetClassLongPtrW(hwnd, -14, ctypes.cast(h_big, ctypes.c_void_p).value or 0)
                if h_sm:
                    SendMessageW(hwnd, 0x0080, 0, ctypes.cast(h_sm, ctypes.c_void_p).value or 0)
                    SetClassLongPtrW(hwnd, -34, ctypes.cast(h_sm, ctypes.c_void_p).value or 0)
            except Exception:
                pass
            return

        if photos:
            try:
                self.root.iconphoto(True, *photos)
            except Exception:
                pass

    def _redraw_header_logo(self) -> None:
        cv = getattr(self, "canvas_app_logo", None)
        if cv is None or not cv.winfo_exists():
            return
        p = mtk.ThemeManager.instance().palette
        cv.configure(bg=p.bg)
        cv.delete("all")
        badge = render_brand_squircle_badge(42)
        if badge is not None:
            self._header_logo_photo = ImageTk.PhotoImage(badge, master=self.root)
            cv.create_image(22, 22, image=self._header_logo_photo, anchor="center", tags=("header_logo",))

    def _build_ui(self, initial_theme: str) -> None:
        # Top Header Bar
        header = mtk.Frame(self.root)
        header.pack(fill="x", side="top", padx=16, pady=(12, 8))

        self.canvas_app_logo = tk.Canvas(header, width=44, height=44, bd=0, highlightthickness=0)
        self.canvas_app_logo.pack(side="left", padx=(0, 10))
        self._redraw_header_logo()

        title_col = mtk.Frame(header)
        title_col.pack(side="left")
        title_row = mtk.Frame(title_col)
        title_row.pack(anchor="w")
        mtk.Label(
            title_row,
            text="ICON & VECTOR STUDIO",
            bold=True,
            font_size_delta=3,
        ).pack(side="left", padx=(0, 8))
        self.badge_version = mtk.Badge(title_row, text=f"v{__version__}", bootstyle="primary-subtle")
        self.badge_version.pack(side="left")
        mtk.Label(
            title_col,
            text="Faithful Picture -> Scalable Vector (.SVG), Windows (.ICO) & Complete 12-Slot Apple (.ICNS)",
            muted=True,
            font_size_delta=-1,
        ).pack(anchor="w")

        theme_box = mtk.Frame(header)
        theme_box.pack(side="right")
        self.btn_header_undo = mtk.Button(
            theme_box,
            text="Undo",
            icon="chevron_left",
            bootstyle="secondary-outline",
            padx=12,
            height=34,
            state="disabled",
            command=self.undo,
        )
        self.btn_header_undo.pack(side="left", padx=(0, 8))
        self.btn_check_updates = mtk.Button(
            theme_box,
            text="Updates",
            icon="download",
            bootstyle="info-outline",
            padx=10,
            height=34,
            command=self.check_updates_interactive,
        )
        self.btn_check_updates.pack(side="left", padx=(0, 10))
        mtk.Label(theme_box, text="Theme:", muted=True).pack(side="left", padx=(0, 6))
        self.theme_combo = mtk.Combobox(
            theme_box,
            values=list(mtk.BUILTIN_THEMES.keys()),
            default=initial_theme,
            width=150,
            command=self._on_theme_selected,
        )
        self.theme_combo.pack(side="left", padx=(0, 8))
        mtk.Button(
            theme_box,
            text="Light / Dark",
            icon="sun",
            bootstyle="secondary-outline",
            height=34,
            command=self._toggle_light_dark,
        ).pack(side="left")

        # Bottom Status Bar
        self.status_bar = mtk.StatusBar(self.root, status_text="Ready", show_stats=False)
        self.status_bar.pack(side="bottom", fill="x")

        # Main Split Area
        main = mtk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        # Left Control Column
        left_col = mtk.Frame(main, width=430)
        left_col.pack(side="left", fill="y", padx=(0, 14))
        left_col.pack_propagate(False)

        self.scroll_controls = mtk.ScrollableFrame(left_col, surface_role="bg", padding=4)
        self.scroll_controls.pack(fill="both", expand=True)

        self._build_source_card(self.scroll_controls.body)
        self._build_framing_card(self.scroll_controls.body)
        self._build_svg_card(self.scroll_controls.body)
        self._build_icns_card(self.scroll_controls.body)

        # Pinned Action Footer below scroll area
        footer_card = mtk.Card(left_col, padding=12)
        footer_card.pack(fill="x", pady=(8, 0))

        self.btn_convert = mtk.Button(
            footer_card.body,
            text="Convert & Save (svg + icons)",
            icon="download",
            bootstyle="success",
            height=40,
            command=self.export_outputs,
        )
        self.btn_convert.pack(fill="x", pady=(0, 6))

        btn_row = mtk.Frame(footer_card.body)
        btn_row.pack(fill="x", pady=(0, 8))
        self.btn_undo = mtk.Button(
            btn_row,
            text="Undo",
            icon="chevron_left",
            bootstyle="secondary-outline",
            padx=12,
            height=34,
            state="disabled",
            command=self.undo,
        )
        self.btn_undo.pack(side="left", padx=(0, 6))

        self.btn_preview = mtk.Button(
            btn_row,
            text="Update Preview",
            icon="refresh",
            bootstyle="primary-outline",
            padx=12,
            height=34,
            command=self.refresh_preview,
        )
        self.btn_preview.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_open_dir = mtk.Button(
            btn_row,
            text="Open Folder",
            icon="folder",
            bootstyle="secondary-ghost",
            padx=12,
            height=34,
            command=self._open_output_folder,
        )
        self.btn_open_dir.pack(side="left", fill="x", expand=True)

        self.progress = mtk.ProgressBar(
            footer_card.body,
            value=0,
            maximum=100,
            show_percent=True,
            bootstyle="primary",
            height=18,
        )
        self.progress.pack(fill="x")

        # Right Inspection Workspace
        right_col = mtk.Frame(main)
        right_col.pack(side="left", fill="both", expand=True)

        # KPI Badge Strip
        kpi_bar = mtk.Frame(right_col)
        kpi_bar.pack(fill="x", pady=(0, 8))
        self.badge_source = mtk.Badge(kpi_bar, text="Source: --", bootstyle="secondary")
        self.badge_source.pack(side="left", padx=(0, 6))
        self.badge_psnr = mtk.Badge(kpi_bar, text="PSNR: --", bootstyle="primary")
        self.badge_psnr.pack(side="left", padx=(0, 6))
        self.badge_sim = mtk.Badge(kpi_bar, text="Similarity: --", bootstyle="success")
        self.badge_sim.pack(side="left", padx=(0, 6))
        self.badge_paths = mtk.Badge(kpi_bar, text="SVG Paths: --", bootstyle="info")
        self.badge_paths.pack(side="left", padx=(0, 6))
        self.badge_svg_sz = mtk.Badge(kpi_bar, text="SVG: --", bootstyle="secondary")
        self.badge_svg_sz.pack(side="left", padx=(0, 6))
        self.badge_icns_sz = mtk.Badge(kpi_bar, text="ICNS: --", bootstyle="secondary")
        self.badge_icns_sz.pack(side="left", padx=(0, 6))
        self.badge_slots = mtk.Badge(kpi_bar, text="ICNS Slots: --", bootstyle="warning")
        self.badge_slots.pack(side="left")

        # Notebook Tabs
        self.notebook = mtk.Notebook(right_col, variant="pills", bootstyle="primary")
        self.notebook.pack(fill="both", expand=True)

        tab_cmp = self.notebook.add("Original vs Vector .SVG")
        tab_icns = self.notebook.add("ICNS Multi-Size Inspector")
        tab_src = self.notebook.add("SVG Source & Log")

        self._build_compare_tab(tab_cmp)
        self._build_icns_tab(tab_icns)
        self._build_source_tab(tab_src)
        self._redraw_compare_canvases()
        self._redraw_icns_strip()

    def _build_source_card(self, parent: tk.Misc) -> None:
        card = mtk.Card(parent, title="Source & Output", padding=12)
        card.pack(fill="x", pady=(0, 8))

        row1 = mtk.Frame(card.body)
        row1.pack(fill="x", pady=(0, 6))
        mtk.Button(
            row1,
            text="Browse Picture...",
            icon="upload",
            bootstyle="primary",
            height=34,
            command=self._browse_input,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        mtk.Button(
            row1,
            text="Clipboard",
            bootstyle="secondary-outline",
            height=34,
            command=self._paste_clipboard,
        ).pack(side="left")

        sample_path = Path(__file__).resolve().parent / "icons" / "porter_source.jpg"
        brand_icns, brand_svg, _ = resolve_app_brand_paths()
        brand_sample = brand_icns or brand_svg
        if sample_path.is_file():
            self.btn_load_sample = mtk.Button(
                card.body,
                text="Load Test Sample (porter_source.jpg)",
                icon="sparkles",
                bootstyle="info-subtle",
                height=30,
                command=lambda p=sample_path: self.load_image(p, auto_preview=True),
            )
            self.btn_load_sample.pack(fill="x", pady=(0, 6))
        elif brand_sample is not None and brand_sample.is_file():
            self.btn_load_sample = mtk.Button(
                card.body,
                text=f"Load Studio Icon ({brand_sample.name})",
                icon="sparkles",
                bootstyle="info-subtle",
                height=30,
                padx=10,
                command=lambda p=brand_sample: self.load_image(p, auto_preview=True),
            )
            self.btn_load_sample.pack(fill="x", pady=(0, 6))

        self.lbl_source_info = mtk.Label(card.body, text="No picture loaded.", muted=True, font_size_delta=-1)
        self.lbl_source_info.pack(anchor="w", pady=(0, 6))

        mtk.Label(card.body, text="Output Folder", font_size_delta=-1, bold=True).pack(anchor="w")
        out_row = mtk.Frame(card.body)
        out_row.pack(fill="x", pady=(2, 6))
        self.btn_browse_out_dir = mtk.Button(
            out_row,
            text="...",
            bootstyle="secondary-outline",
            padx=14,
            height=34,
            command=self._browse_out_dir,
        )
        self.btn_browse_out_dir.pack(side="right", padx=(6, 0))
        self.entry_out_dir = mtk.Entry(
            out_row,
            textvariable=self._var_out_dir,
            placeholder="Output folder...",
            width=180,
            height=34,
        )
        self.entry_out_dir.pack(side="left", fill="x", expand=True)

        mtk.Label(card.body, text="Output Base Name", font_size_delta=-1, bold=True).pack(anchor="w")
        self.entry_stem = mtk.Entry(
            card.body,
            textvariable=self._var_stem,
            placeholder="icon",
            height=34,
        )
        self.entry_stem.pack(fill="x", pady=(2, 8))

        fmt_row = mtk.Frame(card.body)
        fmt_row.pack(fill="x")
        mtk.Checkbox(fmt_row, text=".svg", variable=self._var_write_svg, bootstyle="primary").pack(side="left", padx=(0, 10))
        mtk.Checkbox(fmt_row, text=".icns", variable=self._var_write_icns, bootstyle="success").pack(side="left", padx=(0, 10))
        mtk.Checkbox(fmt_row, text=".ico", variable=self._var_write_ico, bootstyle="info").pack(side="left")

    def _build_framing_card(self, parent: tk.Misc) -> None:
        card = mtk.Card(parent, title="Framing & Background", padding=12)
        card.pack(fill="x", pady=(0, 8))

        mtk.Label(card.body, text="Canvas / Alpha Mode", font_size_delta=-1, bold=True).pack(anchor="w")
        self.combo_bg = mtk.Combobox(
            card.body,
            values=list(BG_MODE_LABELS.values()),
            default=BG_MODE_LABELS["faithful"],
            height=34,
            command=lambda _v: self._schedule_preview(),
        )
        self.combo_bg.pack(fill="x", pady=(3, 8))

        mtk.Switch(
            card.body,
            text="Deepen Colour Difference (Smart Matte)",
            variable=self._var_deepen_diff,
            bootstyle="info",
            command=self._schedule_preview,
        ).pack(anchor="w", pady=(0, 4))

        mtk.Label(card.body, text="Colour Separation Depth %", muted=True, font_size_delta=-1).pack(anchor="w")
        self.slider_deepen = mtk.Slider(
            card.body,
            from_=0,
            to=100,
            value=55,
            height=28,
            command=lambda _v: self._schedule_preview(),
        )
        self.slider_deepen.pack(fill="x", pady=(0, 8))

        mtk.Label(card.body, text="Square Crop (1:1)", font_size_delta=-1, bold=True).pack(anchor="w")
        crop_row = mtk.Frame(card.body)
        crop_row.pack(fill="x", pady=(3, 4))
        self.btn_crop_mode = mtk.Button(
            crop_row,
            text="Square Crop",
            icon="edit",
            bootstyle="primary-outline",
            height=32,
            command=self._toggle_crop_mode,
        )
        self.btn_crop_mode.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_apply_crop = mtk.Button(
            crop_row,
            text="Apply",
            icon="check",
            bootstyle="success",
            height=32,
            command=self.apply_square_crop,
        )
        self.btn_apply_crop.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_apply_crop.configure_button(state="disabled")
        self.btn_reset_crop = mtk.Button(
            crop_row,
            text="Reset",
            icon="refresh",
            bootstyle="secondary-outline",
            height=32,
            command=self.reset_square_crop,
        )
        self.btn_reset_crop.pack(side="left", fill="x", expand=True)
        self.btn_reset_crop.configure_button(state="disabled")
        self.lbl_crop_info = mtk.Label(
            card.body,
            text="Full image — click Square Crop to frame 1:1",
            muted=True,
            font_size_delta=-2,
        )
        self.lbl_crop_info.pack(anchor="w", pady=(0, 8))

        mtk.Label(card.body, text="Transparency Colour Picker", font_size_delta=-1, bold=True).pack(anchor="w")
        self.btn_pick_trans = mtk.Button(
            card.body,
            text="Pick Colour on Image",
            icon="eye",
            bootstyle="info-outline",
            height=32,
            command=self._toggle_pick_mode,
        )
        self.btn_pick_trans.pack(fill="x", pady=(3, 6))

        pick_row = mtk.Frame(card.body)
        pick_row.pack(fill="x", pady=(0, 6))
        self.btn_undo_pick = mtk.Button(
            pick_row,
            text="Undo Pick",
            icon="chevron_left",
            bootstyle="secondary-outline",
            height=30,
            command=self.undo_transparency_pick,
        )
        self.btn_undo_pick.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_clear_picks = mtk.Button(
            pick_row,
            text="Clear Picks",
            icon="trash",
            bootstyle="secondary-ghost",
            height=30,
            command=self.clear_transparency_picks,
        )
        self.btn_clear_picks.pack(side="left", fill="x", expand=True)

        self.canvas_swatches = tk.Canvas(card.body, height=28, bd=0, highlightthickness=0, cursor="hand2")
        self.canvas_swatches.pack(fill="x", pady=(0, 2))
        self.canvas_swatches.bind("<ButtonPress-1>", self._on_swatch_click)
        self.canvas_swatches.bind("<Configure>", lambda _e: self._redraw_pick_swatches())

        self.lbl_pick_info = mtk.Label(
            card.body,
            text="0 picks — click Pick Colour then click background/glow",
            muted=True,
            font_size_delta=-2,
        )
        self.lbl_pick_info.pack(anchor="w", pady=(0, 8))

        mtk.Switch(
            card.body,
            text="Clean JPEG Artifact Noise (Edge-Safe)",
            variable=self._var_clean_jpeg,
            bootstyle="primary",
            command=self._schedule_preview,
        ).pack(anchor="w", pady=(0, 4))

        mtk.Switch(
            card.body,
            text="Auto-Trim Outer Margins",
            variable=self._var_auto_trim,
            bootstyle="primary",
            command=self._schedule_preview,
        ).pack(anchor="w", pady=(0, 4))

        mtk.Label(card.body, text="Trim Padding %", muted=True, font_size_delta=-1).pack(anchor="w")
        self.slider_pad = mtk.Slider(
            card.body,
            from_=0,
            to=20,
            value=6,
            height=30,
            command=lambda _v: self._schedule_preview(),
        )
        self.slider_pad.pack(fill="x", pady=(0, 8))

        mtk.Label(card.body, text="Rounded Corners & Border", font_size_delta=-1, bold=True).pack(anchor="w")
        self.sw_rounded_corners = mtk.Switch(
            card.body,
            text="Rounded Corners (.svg & icons)",
            variable=self._var_rounded_corners,
            bootstyle="primary",
            command=self._schedule_preview,
        )
        self.sw_rounded_corners.pack(anchor="w", pady=(3, 4))

        mtk.Label(card.body, text="Corner Radius %", muted=True, font_size_delta=-1).pack(anchor="w")
        self.slider_corner_radius = mtk.Slider(
            card.body,
            from_=2,
            to=50,
            value=18,
            height=28,
            command=lambda _v: self._schedule_preview(),
        )
        self.slider_corner_radius.pack(fill="x", pady=(0, 6))

        self.sw_rounded_border = mtk.Switch(
            card.body,
            text="Add Border Around Frame",
            variable=self._var_rounded_border,
            bootstyle="info",
            command=self._schedule_preview,
        )
        self.sw_rounded_border.pack(anchor="w", pady=(0, 4))

        mtk.Label(card.body, text="Border Width %", muted=True, font_size_delta=-1).pack(anchor="w")
        self.slider_border_width = mtk.Slider(
            card.body,
            from_=1,
            to=15,
            value=3,
            height=28,
            command=lambda _v: self._schedule_preview(),
        )
        self.slider_border_width.pack(fill="x", pady=(0, 6))

        border_col_row = mtk.Frame(card.body)
        border_col_row.pack(fill="x")
        self.canvas_border_color = tk.Canvas(
            border_col_row,
            width=28,
            height=28,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.canvas_border_color.pack(side="left", padx=(0, 8))
        self.canvas_border_color.bind("<ButtonRelease-1>", lambda _e: self._choose_border_color())
        self.lbl_border_color = mtk.Label(
            border_col_row,
            text="#4CC2FF",
            muted=True,
            font_size_delta=-1,
        )
        self.lbl_border_color.pack(side="left", padx=(0, 8))
        self.btn_border_color = mtk.Button(
            border_col_row,
            text="Border Colour...",
            icon="sliders",
            bootstyle="info-outline",
            height=30,
            padx=12,
            command=self._choose_border_color,
        )
        self.btn_border_color.pack(side="right", fill="x", expand=True)
        self._redraw_border_color_swatch()

    def _build_svg_card(self, parent: tk.Misc) -> None:
        card = mtk.Card(parent, title="SVG Vectorizer", padding=12)
        card.pack(fill="x", pady=(0, 8))

        mtk.Label(card.body, text="Fidelity Preset", font_size_delta=-1, bold=True).pack(anchor="w")
        self.combo_preset = mtk.Combobox(
            card.body,
            values=[lbl for lbl, _ in SVG_PRESETS.values()],
            default=SVG_PRESETS["ultra"][0],
            height=34,
            command=self._on_preset_selected,
        )
        self.combo_preset.pack(fill="x", pady=(3, 8))

        mtk.Label(card.body, text="Curve Mode", font_size_delta=-1, bold=True).pack(anchor="w")
        self.seg_curve = mtk.SegmentedButton(
            card.body,
            values=("Spline", "Polygon", "Pixel"),
            default="Spline",
            pill=True,
            height=32,
            command=lambda _v: self._schedule_preview(),
        )
        self.seg_curve.pack(fill="x", pady=(3, 8))

        mtk.Switch(
            card.body,
            text="Faithful SVG Gradients (Linear, Radial & Freeform)",
            variable=self._var_true_gradients,
            bootstyle="success",
            command=self._schedule_preview,
        ).pack(anchor="w", pady=(0, 4))

        mtk.Switch(
            card.body,
            text="Smooth Lines & De-Alias Edges",
            variable=self._var_clean_lines,
            bootstyle="info",
            command=self._schedule_preview,
        ).pack(anchor="w", pady=(0, 8))

        mtk.Label(card.body, text="Color Precision (Bits/Channel)", muted=True, font_size_delta=-1).pack(anchor="w")
        self.slider_color_prec = mtk.Slider(
            card.body,
            from_=1,
            to=8,
            value=8,
            height=28,
            command=lambda _v: self._schedule_preview(),
        )
        self.slider_color_prec.pack(fill="x", pady=(0, 4))

        mtk.Label(card.body, text="Gradient Detail (Lower = Finer)", muted=True, font_size_delta=-1).pack(anchor="w")
        self.slider_layer_diff = mtk.Slider(
            card.body,
            from_=1,
            to=32,
            value=SVG_PRESETS["ultra"][1].layer_difference,
            height=28,
            command=lambda _v: self._schedule_preview(),
        )
        self.slider_layer_diff.pack(fill="x", pady=(0, 4))

        mtk.Label(card.body, text="Speckle Filter (px)", muted=True, font_size_delta=-1).pack(anchor="w")
        self.slider_speckle = mtk.Slider(
            card.body,
            from_=0,
            to=16,
            value=SVG_PRESETS["ultra"][1].filter_speckle,
            height=28,
            command=lambda _v: self._schedule_preview(),
        )
        self.slider_speckle.pack(fill="x")

    def _build_icns_card(self, parent: tk.Misc) -> None:
        card = mtk.Card(parent, title="ICNS Multi-Size Targets", padding=12)
        card.pack(fill="x", pady=(0, 4))

        mtk.Label(
            card.body,
            text="Covers @1x, @2x Retina, & Legacy RLE24+Mask slots:",
            muted=True,
            font_size_delta=-1,
        ).pack(anchor="w", pady=(0, 6))

        grid = mtk.Frame(card.body)
        grid.pack(fill="x", pady=(0, 6))
        for idx, sz in enumerate(ICNS_TIERS):
            r, c = divmod(idx, 2)
            cb = mtk.Checkbox(
                grid,
                text=f"{sz}x{sz} px",
                variable=self._tier_vars[sz],
                bootstyle="primary",
                command=self._schedule_preview,
            )
            cb.grid(row=r, column=c, sticky="w", padx=(0, 12), pady=2)

        mtk.Switch(
            card.body,
            text="Small-Icon Crispness Boost (<=128px)",
            variable=self._var_small_sharpen,
            bootstyle="success",
            command=self._schedule_preview,
        ).pack(anchor="w", pady=(4, 0))

    def _build_compare_tab(self, parent: tk.Misc) -> None:
        toolbar = mtk.Frame(parent)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))

        mtk.Label(toolbar, text="Zoom:", bold=True).pack(side="left", padx=(0, 8))
        self.seg_zoom = mtk.SegmentedButton(
            toolbar,
            values=("Fit", "100%", "200%", "400%"),
            default="Fit",
            pill=True,
            height=32,
            command=self._on_zoom_changed,
        )
        self.seg_zoom.pack(side="left", padx=(0, 8))

        self.btn_toolbar_picker = mtk.Button(
            toolbar,
            text="Pick Transparency Colour",
            icon="eye",
            bootstyle="info-outline",
            height=32,
            padx=12,
            command=self._toggle_pick_mode,
        )
        self.btn_toolbar_picker.pack(side="right")

        toolbar_sub = mtk.Frame(parent)
        toolbar_sub.pack(fill="x", padx=8, pady=(0, 6))

        self._crop_toolbar_box = mtk.Frame(toolbar_sub)
        self._crop_toolbar_box.pack(side="left", padx=(0, 8))
        self.btn_toolbar_crop = mtk.Button(
            self._crop_toolbar_box,
            text="Square Crop",
            icon="edit",
            bootstyle="primary-outline",
            height=32,
            padx=12,
            command=self._toggle_crop_mode,
        )
        self.btn_toolbar_crop.pack(side="left")
        self.btn_toolbar_apply_crop = mtk.Button(
            self._crop_toolbar_box,
            text="Apply Crop",
            icon="check",
            bootstyle="success",
            height=32,
            padx=12,
            command=self.apply_square_crop,
        )
        self.btn_toolbar_reset_crop = mtk.Button(
            self._crop_toolbar_box,
            text="Reset Crop",
            icon="refresh",
            bootstyle="secondary-outline",
            height=32,
            padx=12,
            command=self.reset_square_crop,
        )

        self.lbl_canvas_hint = mtk.Label(
            toolbar_sub,
            text="Drag either canvas to pan simultaneously",
            muted=True,
            font_size_delta=-1,
        )
        self.lbl_canvas_hint.pack(side="right")

        panes = mtk.Frame(parent)
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left_box = mtk.Card(panes, title="Original Picture", padding=8)
        left_box.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.canvas_orig = tk.Canvas(left_box.body, bd=0, highlightthickness=0, cursor="fleur")
        self.canvas_orig.pack(fill="both", expand=True)

        right_box = mtk.Card(panes, title="Rendered Vector .SVG", padding=8)
        right_box.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.canvas_svg = tk.Canvas(right_box.body, bd=0, highlightthickness=0, cursor="fleur")
        self.canvas_svg.pack(fill="both", expand=True)

        for cv in (self.canvas_orig, self.canvas_svg):
            cv.bind("<ButtonPress-1>", self._on_pan_start)
            cv.bind("<B1-Motion>", self._on_pan_drag)
            cv.bind("<ButtonRelease-1>", self._on_pan_end)
            cv.bind("<Motion>", self._on_canvas_hover)
            cv.bind("<Configure>", lambda _e: self._redraw_compare_canvases())

    def _build_icns_tab(self, parent: tk.Misc) -> None:
        strip_card = mtk.Card(parent, title="Decoded ICNS Preview (Dark & Light Docks + 4x Small Loupes)", padding=10)
        strip_card.pack(fill="x", padx=8, pady=8)

        self.canvas_icns = tk.Canvas(strip_card.body, height=275, bd=0, highlightthickness=0)
        self.canvas_icns.pack(fill="x")
        self.canvas_icns.bind("<Configure>", lambda _e: self._redraw_icns_strip())

        cols = (
            ("ostype", "OSType", 80),
            ("slot", "Icon Slot / Scale", 200),
            ("pixels", "Pixels", 100),
            ("encoding", "Encoding", 130),
            ("bytes", "Bytes", 100),
            ("status", "Decode Check", 130),
        )
        self.icns_table = mtk.DataTable(
            parent,
            columns=cols,
            height=9,
            striped=True,
            sortable=True,
            bootstyle="primary",
        )
        self.icns_table.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_source_tab(self, parent: tk.Misc) -> None:
        mtk.Label(parent, text="Generated SVG Markup (Preview up to 24 KB)", bold=True).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.txt_svg = mtk.Textarea(
            parent,
            line_numbers=True,
            readonly=True,
            monospace=True,
            height=280,
        )
        self.txt_svg.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        mtk.Label(parent, text="Activity Log", bold=True).pack(anchor="w", padx=8, pady=(0, 4))
        self.txt_log = mtk.Textarea(
            parent,
            line_numbers=False,
            readonly=True,
            monospace=True,
            height=170,
        )
        self.txt_log.pack(fill="x", padx=8, pady=(0, 8))

    # ------------------------------------------------------------------ Actions & State

    def _refresh_theme_surfaces(self) -> None:
        self._redraw_header_logo()
        self._redraw_compare_canvases()
        self._redraw_icns_strip()
        self._redraw_pick_swatches()
        self._redraw_border_color_swatch()
        if self._worker_busy:
            self._render_and_push_busy_frame()

    def _redraw_border_color_swatch(self) -> None:
        cv = getattr(self, "canvas_border_color", None)
        if cv is None or not cv.winfo_exists():
            return
        p = mtk.ThemeManager.instance().palette
        cv.configure(bg=p.surface)
        cv.delete("all")
        r, g, b = self._border_color_rgb
        hex_col = f"#{r:02X}{g:02X}{b:02X}"
        cv.create_rectangle(2, 2, 25, 25, fill=hex_col, outline=p.border_strong, width=1, tags=("border_swatch",))
        lbl = getattr(self, "lbl_border_color", None)
        if lbl is not None and lbl.winfo_exists():
            lbl.configure(text=hex_col)

    def set_border_color(
        self,
        color: str | Tuple[int, int, int],
        *,
        enable_border: bool = True,
        trigger_preview: bool = True,
    ) -> Tuple[int, int, int]:
        """Set the rounded/frame border colour from a `#RRGGBB` string or `(R, G, B)` tuple."""
        if isinstance(color, str):
            rgb = _parse_hex_rgb(color, self._border_color_rgb)
        else:
            rgb = (
                int(np.clip(color[0], 0, 255)),
                int(np.clip(color[1], 0, 255)),
                int(np.clip(color[2], 0, 255)),
            )
        self._border_color_rgb = rgb
        if enable_border and not self._var_rounded_border.get():
            self._var_rounded_border.set(True)
        self._redraw_border_color_swatch()
        self._commit_undo_if_changed()
        if trigger_preview and self._source_image is not None:
            self.refresh_preview()
        return rgb

    def _choose_border_color(self) -> None:
        r, g, b = self._border_color_rgb
        init_hex = f"#{r:02X}{g:02X}{b:02X}"
        chosen_rgb, chosen_hex = colorchooser.askcolor(
            color=init_hex,
            parent=self.root,
            title="Select Border Colour",
        )
        if chosen_hex:
            self.set_border_color(chosen_hex, enable_border=True, trigger_preview=True)
        elif chosen_rgb is not None:
            self.set_border_color(
                (int(round(chosen_rgb[0])), int(round(chosen_rgb[1])), int(round(chosen_rgb[2]))),
                enable_border=True,
                trigger_preview=True,
            )

    def _on_theme_selected(self, name: str) -> None:
        mtk.set_theme(name, root=self.root)
        self._refresh_theme_surfaces()

    def _toggle_light_dark(self) -> None:
        mtk.toggle_theme(self.root)
        self.theme_combo.set(mtk.get_theme())
        self._refresh_theme_surfaces()

    def _on_escape_key(self, _e: Optional[tk.Event] = None) -> None:
        if self._pick_mode_active:
            self._set_pick_mode(False)
        if self._crop_mode_active:
            self._set_crop_mode(False)

    def _on_return_key(self, e: tk.Event) -> None:
        if self._crop_mode_active and not isinstance(e.widget, tk.Entry):
            self.apply_square_crop()

    def _on_undo_shortcut(self, e: tk.Event) -> Optional[str]:
        if isinstance(e.widget, (tk.Entry, tk.Text)):
            return None
        self.undo()
        return "break"

    def _capture_editor_config(self) -> _EditorConfig:
        return _EditorConfig(
            bg_label=str(self.combo_bg.get()),
            clean_jpeg=bool(self._var_clean_jpeg.get()),
            auto_trim=bool(self._var_auto_trim.get()),
            padding_pct=int(round(self.slider_pad.get())),
            deepen_diff=bool(self._var_deepen_diff.get()),
            deepen_strength=int(round(self.slider_deepen.get())),
            picked_transparencies=tuple(self._picked_transparencies),
            applied_square_crop=self._applied_square_crop,
            rounded_corners=bool(self._var_rounded_corners.get()),
            corner_radius_pct=int(round(self.slider_corner_radius.get())),
            rounded_border=bool(self._var_rounded_border.get()),
            border_width_pct=int(round(self.slider_border_width.get())),
            border_color_rgb=tuple(self._border_color_rgb),
            preset_label=str(self.combo_preset.get()),
            curve_label=str(self.seg_curve.get()),
            true_gradients=bool(self._var_true_gradients.get()),
            clean_lines=bool(self._var_clean_lines.get()),
            color_precision=int(round(self.slider_color_prec.get())),
            layer_difference=int(round(self.slider_layer_diff.get())),
            filter_speckle=int(round(self.slider_speckle.get())),
            enabled_tiers=tuple(sz for sz in ICNS_TIERS if self._tier_vars[sz].get()),
            small_sharpen=bool(self._var_small_sharpen.get()),
        )

    def undo_depth(self) -> int:
        """Return number of currently available undo steps (capped at MAX_UNDO_LEVELS = 5)."""
        cur = self._capture_editor_config()
        has_uncommitted = self._committed_entry is not None and cur != self._committed_entry.config
        return min(MAX_UNDO_LEVELS, len(self._undo_stack) + (1 if has_uncommitted else 0))

    def can_undo(self) -> bool:
        """Return True when at least one undo step is available."""
        return self.undo_depth() > 0

    def _update_undo_ui(self) -> None:
        d = self.undo_depth()
        label = f"Undo ({d})" if d > 0 else "Undo"
        state = "normal" if d > 0 else "disabled"
        for btn_attr in ("btn_undo", "btn_header_undo"):
            btn = getattr(self, btn_attr, None)
            if btn is not None:
                try:
                    btn.configure_button(text=label, state=state)
                except tk.TclError:
                    pass

    def _reset_undo_history(self) -> None:
        self._undo_stack.clear()
        self._committed_entry = _UndoEntry(
            config=self._capture_editor_config(),
            cached_result=self._last_result,
        )
        self._update_undo_ui()

    def _commit_undo_if_changed(self) -> bool:
        if self._restoring_undo:
            return False
        cur = self._capture_editor_config()
        if self._committed_entry is None:
            self._committed_entry = _UndoEntry(config=cur, cached_result=None)
            self._update_undo_ui()
            return False
        if cur == self._committed_entry.config:
            self._update_undo_ui()
            return False
        self._undo_stack.append(self._committed_entry)
        if len(self._undo_stack) > MAX_UNDO_LEVELS:
            del self._undo_stack[:-MAX_UNDO_LEVELS]
        self._committed_entry = _UndoEntry(config=cur, cached_result=None)
        self._update_undo_ui()
        return True

    def _restore_editor_config(self, cfg: _EditorConfig) -> None:
        self._restoring_undo = True
        try:
            if self._pick_mode_active:
                self._set_pick_mode(False)
            self._crop_mode_active = False
            self._draft_square_crop = None
            self._crop_drag_state = None
            self.lbl_canvas_hint.configure(text="Drag either canvas to pan simultaneously")
            for cv in (self.canvas_orig, self.canvas_svg):
                cv.configure(cursor="fleur")

            self.combo_bg.set(cfg.bg_label, notify=False)
            self.combo_preset.set(cfg.preset_label, notify=False)
            self.seg_curve.set(cfg.curve_label, notify=False)
            self.slider_pad.set(cfg.padding_pct, notify=False)
            self.slider_deepen.set(cfg.deepen_strength, notify=False)
            self.slider_corner_radius.set(cfg.corner_radius_pct, notify=False)
            self.slider_border_width.set(cfg.border_width_pct, notify=False)
            self.slider_color_prec.set(cfg.color_precision, notify=False)
            self.slider_layer_diff.set(cfg.layer_difference, notify=False)
            self.slider_speckle.set(cfg.filter_speckle, notify=False)

            self._var_clean_jpeg.set(cfg.clean_jpeg)
            self._var_auto_trim.set(cfg.auto_trim)
            self._var_deepen_diff.set(cfg.deepen_diff)
            self._var_rounded_corners.set(cfg.rounded_corners)
            self._var_rounded_border.set(cfg.rounded_border)
            self._border_color_rgb = tuple(cfg.border_color_rgb)
            self._var_true_gradients.set(cfg.true_gradients)
            self._var_clean_lines.set(cfg.clean_lines)
            self._var_small_sharpen.set(cfg.small_sharpen)
            tier_set = set(cfg.enabled_tiers)
            for sz in ICNS_TIERS:
                self._tier_vars[sz].set(sz in tier_set)

            self._picked_transparencies = list(cfg.picked_transparencies)
            self._applied_square_crop = cfg.applied_square_crop
            self._pan_x = 0.0
            self._pan_y = 0.0
            self._svg_zoom_cache = None

            self._redraw_pick_swatches()
            self._redraw_border_color_swatch()
            self._update_crop_status_ui()
            self._redraw_compare_canvases()
        finally:
            self._restoring_undo = False

    def undo(self, *, trigger_preview: bool = True) -> bool:
        """Revert the last studio edit (up to 5 levels) and restore or refresh the preview."""
        if self._debounce_job is not None:
            try:
                self.root.after_cancel(self._debounce_job)
            except tk.TclError:
                pass
            self._debounce_job = None

        self._commit_undo_if_changed()
        if not self._undo_stack:
            if self._crop_mode_active:
                self._set_crop_mode(False)
            self._update_undo_ui()
            return False

        target_entry = self._undo_stack.pop()
        self._committed_entry = target_entry
        self._pending_refresh = False
        self._job_generation += 1
        self._restore_editor_config(target_entry.config)
        self._update_undo_ui()
        self._log(f"Undo applied ({len(self._undo_stack)}/{MAX_UNDO_LEVELS} levels remaining).", "INFO")

        if self._source_image is not None:
            if target_entry.cached_result is not None:
                self._inflight_square_crop = target_entry.config.applied_square_crop
                self._apply_result(target_entry.cached_result, wrote_files=False)
            elif trigger_preview:
                self.refresh_preview()
        return True

    def _toggle_pick_mode(self) -> None:
        self._set_pick_mode(not self._pick_mode_active)

    def _set_pick_mode(self, active: bool) -> None:
        if active and self._crop_mode_active:
            self._set_crop_mode(False)
        self._pick_mode_active = bool(active)
        if self._pick_mode_active:
            self.btn_pick_trans.configure_button(text="Picking... (Esc to Stop)", bootstyle="info")
            self.btn_toolbar_picker.configure_button(text="Picking... (Esc to Stop)", bootstyle="info")
            self.lbl_canvas_hint.configure(text="Click background/glow colour to add transparency (Esc to exit)")
            for cv in (self.canvas_orig, self.canvas_svg):
                cv.configure(cursor="crosshair")
        else:
            self.btn_pick_trans.configure_button(text="Pick Colour on Image", bootstyle="info-outline")
            self.btn_toolbar_picker.configure_button(text="Pick Transparency Colour", bootstyle="info-outline")
            if not self._crop_mode_active:
                self.lbl_canvas_hint.configure(text="Drag either canvas to pan simultaneously")
                for cv in (self.canvas_orig, self.canvas_svg):
                    cv.configure(cursor="fleur")
        self._redraw_compare_canvases()

    def _default_square_crop_for_source(self) -> Tuple[int, int, int, int]:
        assert self._source_image is not None
        src_w, src_h = self._source_image.size
        max_s = max(1, min(src_w, src_h))
        min_s = min(max_s, 16)
        side = max(min_s, int(round(max_s * 0.85)))
        x0 = (src_w - side) // 2
        y0 = (src_h - side) // 2
        return normalize_square_crop((x0, y0, x0 + side, y0 + side), src_w, src_h, min_side=min_s)

    def _update_crop_status_ui(self) -> None:
        if self._crop_mode_active:
            self.btn_crop_mode.configure_button(text="Cancel Crop", bootstyle="primary")
            self.btn_toolbar_crop.configure_button(text="Cancel Crop", bootstyle="primary")
            self.btn_apply_crop.configure_button(state="normal")
            if not self.btn_toolbar_apply_crop.winfo_manager():
                self.btn_toolbar_apply_crop.pack(side="left", padx=(6, 0))
            if self.btn_toolbar_reset_crop.winfo_manager():
                self.btn_toolbar_reset_crop.pack_forget()
        else:
            self.btn_crop_mode.configure_button(text="Square Crop", bootstyle="primary-outline")
            self.btn_toolbar_crop.configure_button(text="Square Crop", bootstyle="primary-outline")
            self.btn_apply_crop.configure_button(state="disabled")
            if self.btn_toolbar_apply_crop.winfo_manager():
                self.btn_toolbar_apply_crop.pack_forget()
            if self._applied_square_crop is not None:
                if not self.btn_toolbar_reset_crop.winfo_manager():
                    self.btn_toolbar_reset_crop.pack(side="left", padx=(6, 0))
            else:
                if self.btn_toolbar_reset_crop.winfo_manager():
                    self.btn_toolbar_reset_crop.pack_forget()

        has_applied = self._applied_square_crop is not None
        self.btn_reset_crop.configure_button(state="normal" if (has_applied or self._crop_mode_active) else "disabled")

        if self._source_image is None:
            self.lbl_crop_info.configure(text="Full image — click Square Crop to frame 1:1")
            self.badge_source.set_text("Source: --")
            return

        src_w, src_h = self._source_image.size
        if self._crop_mode_active and self._draft_square_crop is not None:
            bx0, by0, bx1, by1 = self._draft_square_crop
            side = bx1 - bx0
            self.lbl_crop_info.configure(text=f"Framing 1:1 square: {side}x{side} px at ({bx0}, {by0})")
        elif self._applied_square_crop is not None:
            bx0, by0, bx1, by1 = self._applied_square_crop
            side = bx1 - bx0
            self.lbl_crop_info.configure(text=f"Cropped 1:1 square: {side}x{side} px at ({bx0}, {by0})")
        else:
            self.lbl_crop_info.configure(text="Full image — click Square Crop to frame 1:1")

        if self._applied_square_crop is not None:
            side = self._applied_square_crop[2] - self._applied_square_crop[0]
            self.badge_source.set_text(f"Source: {src_w}x{src_h} -> {side}x{side}")
        else:
            self.badge_source.set_text(f"Source: {src_w}x{src_h}")

    def _toggle_crop_mode(self) -> None:
        self._set_crop_mode(not self._crop_mode_active)

    def set_crop_mode(self, active: bool) -> None:
        """Enable or disable interactive 1:1 square crop mode."""
        self._set_crop_mode(active)

    def _set_crop_mode(self, active: bool) -> None:
        target = bool(active)
        if target and self._source_image is None:
            mtk.Toast.show(self.root, title="No Image", message="Please choose a picture first.", bootstyle="warning")
            return
        if target and self._pick_mode_active:
            self._set_pick_mode(False)
        self._crop_mode_active = target
        self._crop_drag_state = None
        if target:
            assert self._source_image is not None
            if self._applied_square_crop is not None:
                self._draft_square_crop = normalize_square_crop(
                    self._applied_square_crop,
                    self._source_image.width,
                    self._source_image.height,
                    min_side=min(16, min(self._source_image.size)),
                )
            else:
                self._draft_square_crop = self._default_square_crop_for_source()
            self._pan_x = 0.0
            self._pan_y = 0.0
            self.lbl_canvas_hint.configure(text="1:1 Square: drag handles/box (Enter/Esc)")
        else:
            if not self._pick_mode_active:
                self.lbl_canvas_hint.configure(text="Drag either canvas to pan simultaneously")
                for cv in (self.canvas_orig, self.canvas_svg):
                    cv.configure(cursor="fleur")
        self._update_crop_status_ui()
        self._redraw_compare_canvases()

    def set_draft_square_crop(self, box: Tuple[int, int, int, int] | Tuple[int, int, int]) -> Tuple[int, int, int, int]:
        """Set and redraw a normalized 1:1 draft square crop box."""
        if self._source_image is None:
            raise RuntimeError("No source image loaded.")
        min_s = min(16, min(self._source_image.size))
        norm = normalize_square_crop(box, self._source_image.width, self._source_image.height, min_side=min_s)
        self._draft_square_crop = norm
        self._update_crop_status_ui()
        self._redraw_compare_canvases()
        return norm

    def apply_square_crop(self, *, trigger_preview: bool = True) -> Optional[Tuple[int, int, int, int]]:
        """Commit the 1:1 square crop region, discarding everything outside it."""
        if self._source_image is None:
            return None
        box = self._draft_square_crop or self._applied_square_crop or self._default_square_crop_for_source()
        norm = normalize_square_crop(box, self._source_image.width, self._source_image.height, min_side=1)
        self._applied_square_crop = norm
        self._draft_square_crop = norm
        self._crop_drag_state = None
        self._crop_mode_active = False
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._svg_zoom_cache = None
        if not self._pick_mode_active:
            self.lbl_canvas_hint.configure(text="Drag either canvas to pan simultaneously")
            for cv in (self.canvas_orig, self.canvas_svg):
                cv.configure(cursor="fleur")
        self._update_crop_status_ui()
        self._redraw_compare_canvases()
        side = norm[2] - norm[0]
        self._log(f"Applied 1:1 square crop ({norm[0]},{norm[1]})..({norm[2]},{norm[3]}) [{side}x{side} px].", "INFO")
        self._commit_undo_if_changed()
        if trigger_preview:
            self.refresh_preview()
        return norm

    def reset_square_crop(self, *, trigger_preview: bool = True) -> None:
        """Clear any 1:1 square crop and restore the full source image."""
        had_applied = self._applied_square_crop is not None
        self._applied_square_crop = None
        self._draft_square_crop = None
        self._crop_drag_state = None
        self._crop_mode_active = False
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._svg_zoom_cache = None
        if not self._pick_mode_active:
            self.lbl_canvas_hint.configure(text="Drag either canvas to pan simultaneously")
            for cv in (self.canvas_orig, self.canvas_svg):
                cv.configure(cursor="fleur")
        self._update_crop_status_ui()
        self._redraw_compare_canvases()
        if had_applied:
            self._log("Reset square crop to full source image.", "INFO")
            self._commit_undo_if_changed()
            if trigger_preview:
                self.refresh_preview()

    def _sample_source_median_rgb(self, sx: int, sy: int) -> Tuple[int, int, int]:
        assert self._source_image is not None
        arr = np.asarray(self._source_image)
        h, w = arr.shape[:2]
        patch = arr[max(0, sy - 1) : min(h, sy + 2), max(0, sx - 1) : min(w, sx + 2), :3]
        med = np.median(patch.reshape(-1, 3), axis=0)
        return (int(round(med[0])), int(round(med[1])), int(round(med[2])))

    def _map_canvas_point_to_source(self, canvas: tk.Canvas, cx: float, cy: float) -> Optional[Tuple[int, int]]:
        if self._source_image is None:
            return None
        geom = self._canvas_img_geom.get(id(canvas))
        if geom is None:
            return None
        x0, y0, scale, img_w, img_h = geom
        if scale <= 0.0:
            return None
        ix = (cx - x0) / scale
        iy = (cy - y0) / scale
        if not (0.0 <= ix < img_w and 0.0 <= iy < img_h):
            return None

        src_w, src_h = self._source_image.size
        if canvas is self.canvas_orig:
            cx0, cy0, cx1, cy1 = (0, 0, src_w, src_h)
        elif (
            self._last_result is not None
            and not self._crop_mode_active
            and self._applied_square_crop == self._last_result_square_crop
        ):
            cx0, cy0, cx1, cy1 = self._last_result.prepared.crop_box
        else:
            cx0, cy0, cx1, cy1 = self._applied_square_crop or (0, 0, src_w, src_h)
        crop_w = max(1, cx1 - cx0)
        crop_h = max(1, cy1 - cy0)
        sx = int(np.clip(round(cx0 + (ix / max(1, img_w)) * crop_w - 0.5), 0, src_w - 1))
        sy = int(np.clip(round(cy0 + (iy / max(1, img_h)) * crop_h - 0.5), 0, src_h - 1))
        return sx, sy

    def add_transparency_pick_at_source(self, sx: int, sy: int, *, trigger_preview: bool = True) -> TransparencyPick:
        if self._source_image is None:
            raise RuntimeError("No source image loaded.")
        src_w, src_h = self._source_image.size
        sx_c = int(np.clip(sx, 0, max(src_w - 1, 0)))
        sy_c = int(np.clip(sy, 0, max(src_h - 1, 0)))
        rgb = self._sample_source_median_rgb(sx_c, sy_c)
        pick = TransparencyPick(
            x_norm=sx_c / float(max(src_w - 1, 1)),
            y_norm=sy_c / float(max(src_h - 1, 1)),
            rgb=rgb,
            tolerance=26.0,
        )
        self._picked_transparencies.append(pick)
        if self.combo_bg.get() != BG_MODE_LABELS["transparent"]:
            self.combo_bg.set(BG_MODE_LABELS["transparent"], notify=False)
        self._redraw_pick_swatches()
        self._log(
            f"Added transparency pick #{len(self._picked_transparencies)} at ({sx_c},{sy_c}) "
            f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}",
            "INFO",
        )
        self._commit_undo_if_changed()
        if trigger_preview:
            self.refresh_preview()
        return pick

    def undo_transparency_pick(self) -> None:
        if not self._picked_transparencies:
            return
        self._picked_transparencies.pop()
        self._redraw_pick_swatches()
        self._commit_undo_if_changed()
        self.refresh_preview()

    def clear_transparency_picks(self) -> None:
        if not self._picked_transparencies:
            return
        self._picked_transparencies.clear()
        self._redraw_pick_swatches()
        self._commit_undo_if_changed()
        self.refresh_preview()

    def _on_swatch_click(self, e: tk.Event) -> None:
        idx = (e.x - 4) // 24
        if 0 <= idx < len(self._picked_transparencies):
            self._picked_transparencies.pop(idx)
            self._redraw_pick_swatches()
            self._commit_undo_if_changed()
            self.refresh_preview()

    def _redraw_pick_swatches(self) -> None:
        cv = self.canvas_swatches
        p = mtk.ThemeManager.instance().palette
        cv.configure(bg=p.surface)
        cv.delete("all")
        n = len(self._picked_transparencies)
        if n == 0:
            self.lbl_pick_info.configure(text="0 picks — click Pick Colour then click background/glow")
            return
        self.lbl_pick_info.configure(text=f"{n} pick(s) active — click any colour chip above to remove it")
        for idx, pick in enumerate(self._picked_transparencies):
            x0 = 4 + idx * 24
            if x0 + 20 > max(cv.winfo_width(), 220):
                break
            r, g, b = pick.rgb
            hex_col = f"#{r:02X}{g:02X}{b:02X}"
            cv.create_rectangle(x0, 4, x0 + 20, 24, fill=hex_col, outline=p.border_strong, width=1)

    def _log(self, msg: str, level: str = "INFO") -> None:
        try:
            self.txt_log.append_log(msg, level)
        except Exception:
            pass

    def _on_preset_selected(self, label: str) -> None:
        key = PRESET_LABEL_TO_KEY.get(label, "ultra")
        _, opts = SVG_PRESETS[key]
        self._syncing_preset = True
        try:
            self.slider_color_prec.set(opts.color_precision, notify=False)
            self.slider_layer_diff.set(opts.layer_difference, notify=False)
            self.slider_speckle.set(opts.filter_speckle, notify=False)
            self.seg_curve.set(opts.mode.capitalize(), notify=False)
        finally:
            self._syncing_preset = False
        self._commit_undo_if_changed()
        self._schedule_preview()

    def _schedule_preview(self) -> None:
        if self._syncing_preset or self._restoring_undo or self._source_image is None:
            return
        if self._debounce_job is not None:
            try:
                self.root.after_cancel(self._debounce_job)
            except tk.TclError:
                pass
        self._debounce_job = self.root.after(220, self.refresh_preview)
        self._update_undo_ui()

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Picture to Convert",
            filetypes=[
                ("Pictures & Icons", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.ico *.svg *.icns"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.load_image(path, auto_preview=True)

    def _browse_out_dir(self) -> None:
        path = filedialog.askdirectory(title="Select Output Folder", initialdir=self._var_out_dir.get() or None)
        if path:
            self._var_out_dir.set(path)

    def _paste_clipboard(self) -> None:
        try:
            grabbed = ImageGrab.grabclipboard()
        except Exception as exc:
            self._log(f"Clipboard unavailable: {exc}", "WARNING")
            return
        if isinstance(grabbed, Image.Image):
            self._source_path = None
            self._source_image = grabbed.convert("RGBA")
            self._last_result = None
            self._last_result_square_crop = None
            self._svg_zoom_cache = None
            self._var_stem.set("clipboard_icon")
            w, h = self._source_image.size
            self.lbl_source_info.configure(text=f"Clipboard image ({w}x{h})")
            self.badge_source.set_text(f"Source: {w}x{h}")
            self._pan_x = 0.0
            self._pan_y = 0.0
            self._applied_square_crop = None
            self._draft_square_crop = None
            self._set_crop_mode(False)
            self._picked_transparencies.clear()
            self._set_pick_mode(False)
            self._redraw_pick_swatches()
            self._update_crop_status_ui()
            self._reset_undo_history()
            self.refresh_preview()
        elif isinstance(grabbed, list) and grabbed:
            first = Path(grabbed[0])
            if first.is_file():
                self.load_image(first, auto_preview=True)
        else:
            mtk.Toast.show(self.root, title="Clipboard", message="No image found on clipboard.", bootstyle="warning")

    def load_image(self, path: str | Path, *, auto_preview: bool = True) -> None:
        p = Path(path)
        try:
            im = load_source_image(p)
        except Exception as exc:
            self._log(f"Failed to load {p}: {exc}", "ERROR")
            mtk.Toast.show(self.root, title="Load Error", message=str(exc), bootstyle="danger")
            return

        self._source_path = p
        self._source_image = im
        self._last_result = None
        self._last_result_square_crop = None
        self._svg_zoom_cache = None
        self._var_stem.set(p.stem)
        self.lbl_source_info.configure(text=f"{p.name} ({im.width}x{im.height} px)")
        self.badge_source.set_text(f"Source: {im.width}x{im.height}")
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._applied_square_crop = None
        self._draft_square_crop = None
        self._set_crop_mode(False)
        self._picked_transparencies.clear()
        self._set_pick_mode(False)
        self._redraw_pick_swatches()
        self._update_crop_status_ui()
        self._reset_undo_history()
        self._log(f"Loaded {p} ({im.width}x{im.height} RGBA).", "INFO")
        if auto_preview:
            self.refresh_preview()

    def _gather_options(self) -> Tuple[PrepareOptions, SVGOptions, List[int], bool]:
        bg_label = self.combo_bg.get()
        bg_mode = LABEL_TO_BG_MODE.get(bg_label, "faithful")
        p_opts = PrepareOptions(
            bg_mode=bg_mode,
            clean_jpeg_noise=bool(self._var_clean_jpeg.get()),
            auto_trim=bool(self._var_auto_trim.get()),
            padding_pct=float(round(self.slider_pad.get())),
            deepen_diff=bool(self._var_deepen_diff.get()),
            deepen_strength=float(round(self.slider_deepen.get())),
            picked_transparencies=tuple(self._picked_transparencies),
            square_crop=self._applied_square_crop,
            rounded_corners=bool(self._var_rounded_corners.get()),
            corner_radius_pct=float(round(self.slider_corner_radius.get())),
            rounded_border=bool(self._var_rounded_border.get()),
            border_width_pct=float(round(self.slider_border_width.get())),
            border_color_rgb=tuple(self._border_color_rgb),
        )

        preset_label = self.combo_preset.get()
        preset_key = PRESET_LABEL_TO_KEY.get(preset_label, "ultra")
        base_svg = SVG_PRESETS[preset_key][1]
        curve_val = self.seg_curve.get().lower()
        s_opts = SVGOptions(
            preset_key=preset_key,
            mode=curve_val if curve_val in ("spline", "polygon", "pixel") else "spline",
            hierarchical=base_svg.hierarchical,
            color_precision=int(round(self.slider_color_prec.get())),
            layer_difference=int(round(self.slider_layer_diff.get())),
            filter_speckle=int(round(self.slider_speckle.get())),
            corner_threshold=base_svg.corner_threshold,
            length_threshold=base_svg.length_threshold,
            max_iterations=base_svg.max_iterations,
            splice_threshold=base_svg.splice_threshold,
            simplify=base_svg.simplify,
            path_precision=base_svg.path_precision,
            optimize=base_svg.optimize,
            trace_scale=base_svg.trace_scale,
            true_gradients=bool(self._var_true_gradients.get()),
            clean_aa_lines=bool(self._var_clean_lines.get()),
        )

        tiers = [sz for sz in ICNS_TIERS if self._tier_vars[sz].get()]
        if not tiers:
            tiers = list(ICNS_TIERS)
        return p_opts, s_opts, tiers, bool(self._var_small_sharpen.get())

    def refresh_preview(self) -> None:
        if self._crop_mode_active and self._draft_square_crop is not None:
            self._applied_square_crop = self._draft_square_crop
            self._set_crop_mode(False)
            self._pan_x = 0.0
            self._pan_y = 0.0
        self._commit_undo_if_changed()
        self._start_worker(write_files=False)

    def export_outputs(self) -> None:
        if self._crop_mode_active and self._draft_square_crop is not None:
            self._applied_square_crop = self._draft_square_crop
            self._set_crop_mode(False)
            self._pan_x = 0.0
            self._pan_y = 0.0
        self._commit_undo_if_changed()
        self._start_worker(write_files=True)

    def _ensure_spinner_frames(self) -> Tuple[List[Image.Image], bool, Tuple[int, int, int]]:
        p = mtk.ThemeManager.instance().palette
        dark = p.mode == "dark"
        prim = _parse_hex_rgb(p.primary, (76, 194, 255))
        info = _parse_hex_rgb(p.info, (56, 189, 248))
        key = (dark, prim, info)
        if self._spinner_cache_key != key or not self._spinner_frames:
            self._spinner_frames = build_timer_dial_frames(
                dark=dark,
                primary_rgb=prim,
                info_rgb=info,
                num_frames=SPINNER_FRAME_COUNT,
            )
            self._spinner_cache_key = key
        return self._spinner_frames, dark, prim

    def _apply_busy_overlay_to_canvases(self, canvases: Sequence[tk.Canvas]) -> None:
        if self._busy_overlay_photo is None:
            return
        for cv in canvases:
            try:
                if not cv.winfo_exists():
                    continue
                cw = cv.winfo_width()
                ch = cv.winfo_height()
                if cw <= 10:
                    cw = max(240, cv.winfo_reqwidth())
                if ch <= 10:
                    ch = max(220, cv.winfo_reqheight())
                cx, cy = cw // 2, ch // 2
                existing = cv.find_withtag("busy_overlay")
                if existing:
                    cv.coords(existing[0], cx, cy)
                    cv.itemconfigure(existing[0], image=self._busy_overlay_photo)
                    cv.tag_raise("busy_overlay")
                else:
                    cv.create_image(
                        cx,
                        cy,
                        image=self._busy_overlay_photo,
                        anchor="center",
                        tags=("busy_overlay",),
                    )
            except tk.TclError:
                continue

    def _render_and_push_busy_frame(self) -> None:
        try:
            if not self._worker_busy or not self.root.winfo_exists():
                return
            frames, dark, prim = self._ensure_spinner_frames()
            dial = frames[self._busy_frame_idx % len(frames)]
            elapsed = max(0.0, time.perf_counter() - self._busy_start_t)
            badge_im = render_timer_overlay_badge(
                dial,
                elapsed_sec=elapsed,
                pct=self._busy_pct,
                dark=dark,
                primary_rgb=prim,
                pill_cache=self._spinner_pill_cache,
            )
            self._busy_overlay_photo = ImageTk.PhotoImage(badge_im, master=self.root)
            self._apply_busy_overlay_to_canvases((self.canvas_svg, self.canvas_icns))
        except tk.TclError:
            pass

    @staticmethod
    def _stage_pct_ceiling(pct: float) -> float:
        if pct < 18.0:
            return 17.0
        if pct < 30.0:
            return 29.0
        if pct < 38.0:
            return 37.0
        if pct < 66.0:
            return 64.0
        if pct < 80.0:
            return 78.0
        if pct < 97.0:
            return 96.0
        if pct < 100.0:
            return 99.0
        return 100.0

    def _tick_busy_overlay(self) -> None:
        self._busy_anim_job = None
        try:
            if not self._worker_busy or not self.root.winfo_exists():
                return
            self._busy_frame_idx = (self._busy_frame_idx + 1) % SPINNER_FRAME_COUNT
            if self._busy_pct < self._busy_pct_ceil:
                prev_i = int(round(self._busy_pct))
                step = max(0.22, (self._busy_pct_ceil - self._busy_pct) * 0.055)
                self._busy_pct = min(self._busy_pct_ceil, self._busy_pct + step)
                new_i = int(round(self._busy_pct))
                if new_i != prev_i:
                    self.progress.set(new_i)
            self._render_and_push_busy_frame()
            self._busy_anim_job = self.root.after(33, self._tick_busy_overlay)
        except tk.TclError:
            self._busy_anim_job = None

    def _clear_busy_overlays(self) -> None:
        if self._busy_anim_job is not None:
            try:
                self.root.after_cancel(self._busy_anim_job)
            except tk.TclError:
                pass
            self._busy_anim_job = None
        for cv in (self.canvas_orig, self.canvas_svg, self.canvas_icns):
            try:
                if cv.winfo_exists():
                    cv.delete("busy_overlay")
            except tk.TclError:
                pass
        self._busy_overlay_photo = None

    def _set_busy(self, busy: bool) -> None:
        self._worker_busy = busy
        state = "disabled" if busy else "normal"
        self.btn_convert.configure_button(state=state)
        self.btn_preview.configure_button(state=state)
        if busy:
            if self._busy_anim_job is not None:
                try:
                    self.root.after_cancel(self._busy_anim_job)
                except tk.TclError:
                    pass
                self._busy_anim_job = None
            self._busy_start_t = time.perf_counter()
            self._busy_pct = 5.0
            self._busy_pct_ceil = 16.0
            self._busy_frame_idx = 0
            self._render_and_push_busy_frame()
            self._busy_anim_job = self.root.after(33, self._tick_busy_overlay)
        else:
            self._clear_busy_overlays()

    def _start_worker(self, *, write_files: bool) -> None:
        if self._source_image is None:
            mtk.Toast.show(self.root, title="No Image", message="Please choose a picture first.", bootstyle="warning")
            return
        if self._worker_busy:
            if not write_files:
                self._pending_refresh = True
            return

        p_opts, s_opts, tiers, sharpen = self._gather_options()
        self._inflight_square_crop = p_opts.square_crop
        src_copy = self._source_image.copy()
        out_dir: Optional[Path] = None
        if write_files:
            out_dir = Path(self.entry_out_dir.get().strip() or self._var_out_dir.get())

        stem = (self.entry_stem.get().strip() or self._var_stem.get().strip() or "icon")
        w_svg = bool(self._var_write_svg.get())
        w_icns = bool(self._var_write_icns.get())
        w_ico = bool(self._var_write_ico.get())
        if write_files and not (w_svg or w_icns or w_ico):
            mtk.Toast.show(self.root, title="No Format Selected", message="Check .svg or .icns first.", bootstyle="warning")
            return

        self._job_generation += 1
        job_gen = self._job_generation
        self._active_worker_gen = job_gen
        job_cfg = self._capture_editor_config()

        self._set_busy(True)
        self.progress.set(5)
        self.status_bar.set_status("Converting...")

        def _on_progress(pct: float, msg: str) -> None:
            self._ui_queue.put(("progress", job_gen, pct, msg))

        def _worker() -> None:
            try:
                res = run_conversion(
                    src_copy,
                    prepare_opts=p_opts,
                    svg_opts=s_opts,
                    icns_sizes=tiers,
                    small_icon_sharpen=sharpen,
                    out_dir=out_dir,
                    stem=stem,
                    write_svg=w_svg,
                    write_icns=w_icns,
                    write_ico=w_ico,
                    progress_cb=_on_progress,
                )
                self._ui_queue.put(("done", job_gen, job_cfg, res, write_files))
            except Exception as exc:
                self._ui_queue.put(("error", job_gen, exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _schedule_startup_update_check(self) -> None:
        self._update_check_job = None
        if not self.root.winfo_exists():
            return

        def _bg_worker() -> None:
            release = check_for_updates(current_version=__version__)
            if release is not None and release.is_newer:
                try:
                    self.root.after_idle(
                        lambda r=release: UpdatePromptDialog(self.root, r) if self.root.winfo_exists() else None
                    )
                except tk.TclError:
                    pass

        threading.Thread(target=_bg_worker, daemon=True).start()

    def check_updates_interactive(self) -> None:
        """Manually check GitHub Releases for a newer version and open `UpdatePromptDialog` or notify the user."""
        self.status_bar.set_status("Checking GitHub for updates...")

        def _bg_worker() -> None:
            release = check_for_updates(current_version=__version__)

            def _finish() -> None:
                if not self.root.winfo_exists():
                    return
                if release is None:
                    self.status_bar.set_status("Could not reach GitHub Releases.")
                    mtk.Toast.show(
                        self.root,
                        title="Update Check",
                        message="Could not reach GitHub Releases right now.",
                        bootstyle="warning",
                    )
                elif release.is_newer:
                    self.status_bar.set_status(f"Update available: v{release.version}")
                    UpdatePromptDialog(self.root, release)
                else:
                    self.status_bar.set_status(f"Up to date (v{__version__})")
                    mtk.Toast.show(
                        self.root,
                        title="Up to Date",
                        message=f"You are running the latest version (v{__version__}).",
                        bootstyle="success",
                    )

            try:
                self.root.after_idle(_finish)
            except tk.TclError:
                pass

        threading.Thread(target=_bg_worker, daemon=True).start()

    def _on_root_destroy(self, e: tk.Event) -> None:
        if e.widget is not self.root:
            return
        for attr in ("_poll_job", "_debounce_job", "_busy_anim_job", "_update_check_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def drain_ui_queue(self) -> None:
        """Process all pending background-worker messages on the Tkinter main thread."""
        while True:
            try:
                item = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "progress":
                _, job_gen, pct, msg = item
                if job_gen != self._job_generation:
                    continue
                f_pct = float(pct)
                self._busy_pct = max(self._busy_pct, f_pct)
                self._busy_pct_ceil = max(self._busy_pct, self._stage_pct_ceiling(f_pct))
                self.progress.set(int(round(self._busy_pct)))
                self.status_bar.set_status(msg)
            elif kind == "done":
                _, job_gen, job_cfg, res, wf = item
                if self._committed_entry is not None and self._committed_entry.config == job_cfg:
                    self._committed_entry.cached_result = res
                for entry in self._undo_stack:
                    if entry.config == job_cfg and entry.cached_result is None:
                        entry.cached_result = res
                if self._active_worker_gen == job_gen:
                    self._active_worker_gen = None
                if job_gen != self._job_generation and not wf:
                    if self._active_worker_gen is None:
                        self._set_busy(False)
                        if self._pending_refresh:
                            self._pending_refresh = False
                            self.refresh_preview()
                    continue
                self._apply_result(res, wf)
            elif kind == "error":
                _, job_gen, exc = item
                if self._active_worker_gen == job_gen:
                    self._active_worker_gen = None
                if job_gen != self._job_generation:
                    if self._active_worker_gen is None:
                        self._set_busy(False)
                        if self._pending_refresh:
                            self._pending_refresh = False
                            self.refresh_preview()
                    continue
                self._handle_worker_error(exc)

    def _poll_ui_queue(self) -> None:
        try:
            if not self.root.winfo_exists():
                return
            self.drain_ui_queue()
            self._poll_job = self.root.after(25, self._poll_ui_queue)
        except tk.TclError:
            self._poll_job = None

    def _handle_worker_error(self, exc: Exception) -> None:
        self._set_busy(False)
        self.progress.set(0)
        self.status_bar.set_status(f"Error: {exc}")
        self._log(f"Conversion failed: {exc}", "ERROR")
        mtk.Toast.show(self.root, title="Conversion Error", message=str(exc), bootstyle="danger")
        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh_preview()

    def _apply_result(self, res: ConversionResult, wrote_files: bool) -> None:
        self._set_busy(False)
        self._svg_zoom_cache = None
        self._last_result = res
        self._last_result_square_crop = self._inflight_square_crop
        self.progress.set(100)

        self.badge_psnr.set_text(
            f"PSNR: {res.psnr_db:.2f} dB",
            bootstyle="success" if res.psnr_db >= 29.0 else "warning",
        )
        self.badge_sim.set_text(
            f"Similarity: {res.similarity_pct:.2f}%",
            bootstyle="success" if res.similarity_pct >= 97.5 else "warning",
        )
        if res.gradient_count > 0:
            self.badge_paths.set_text(f"SVG Paths: {res.path_count:,} + {res.gradient_count} Grads")
        else:
            self.badge_paths.set_text(f"SVG Paths: {res.path_count:,}")
        self.badge_svg_sz.set_text(f"SVG: {_format_bytes(res.svg_bytes)}")
        self.badge_icns_sz.set_text(f"ICNS: {_format_bytes(len(res.icns_bytes))}")
        self.badge_slots.set_text(
            f"ICNS Slots: {len(res.icns_inspection.verified_sizes)}/12",
            bootstyle="success" if len(res.icns_inspection.verified_sizes) == 12 else "info",
        )

        # Populate DataTable
        self.icns_table.clear()
        for slot in res.icns_inspection.slots:
            tag = "success" if slot.decoded_ok else "danger"
            self.icns_table.insert_row(
                (
                    slot.ostype,
                    slot.slot_label,
                    f"{slot.pixel_size}x{slot.pixel_size}",
                    slot.encoding,
                    _format_bytes(slot.byte_length),
                    "Verified OK" if slot.decoded_ok else "Failed",
                ),
                tag=tag,
            )

        # Populate truncated SVG markup preview so Tkinter Text never lags on multi-MB SVGs
        max_chars = 24_000
        if len(res.svg_text) > max_chars:
            snippet = (
                res.svg_text[:max_chars]
                + f"\n\n<!-- ... truncated for preview ({len(res.svg_text):,} total characters) ... -->\n"
            )
        else:
            snippet = res.svg_text
        self.txt_svg.set(snippet)

        self._redraw_compare_canvases()
        self._redraw_icns_strip()

        msg = (
            f"Done in {res.elapsed_sec:.2f}s — PSNR {res.psnr_db:.2f} dB, "
            f"Similarity {res.similarity_pct:.2f}%, {len(res.icns_inspection.verified_sizes)} verified ICNS slots"
        )
        self.status_bar.set_status(msg)
        if wrote_files and res.written_files:
            names = ", ".join(p.name for p in res.written_files)
            self._log(f"Saved {names} ({msg})", "SUCCESS")
            mtk.Toast.show(
                self.root,
                title="Saved Successfully",
                message=f"Wrote: {names}",
                bootstyle="success",
            )
        else:
            self._log(f"Preview updated ({msg})", "INFO")

        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh_preview()

    # ------------------------------------------------------------------ Interactive Canvas Rendering

    def _on_zoom_changed(self, value: str) -> None:
        self._zoom_mode = value
        if value == "Fit":
            self._pan_x = 0.0
            self._pan_y = 0.0
        self._redraw_compare_canvases()

    def _hit_test_crop_overlay(self, canvas: tk.Canvas, cx: float, cy: float) -> str:
        rect = self._canvas_crop_rect.get(id(canvas))
        if rect is None:
            return "pan"
        csx0, csy0, csx1, csy1 = rect
        cx_mid = 0.5 * (csx0 + csx1)
        cy_mid = 0.5 * (csy0 + csy1)

        corners = (
            ("nw", csx0, csy0),
            ("ne", csx1, csy0),
            ("sw", csx0, csy1),
            ("se", csx1, csy1),
        )
        best_corner: Optional[Tuple[float, str]] = None
        for name, hx, hy in corners:
            d = max(abs(cx - hx), abs(cy - hy))
            if d <= 10.0 and (best_corner is None or d < best_corner[0]):
                best_corner = (d, name)
        if best_corner is not None:
            return best_corner[1]

        sides = (
            ("n", cx_mid, csy0),
            ("e", csx1, cy_mid),
            ("s", cx_mid, csy1),
            ("w", csx0, cy_mid),
        )
        best_side: Optional[Tuple[float, str]] = None
        for name, hx, hy in sides:
            d = max(abs(cx - hx), abs(cy - hy))
            if d <= 10.0 and (best_side is None or d < best_side[0]):
                best_side = (d, name)
        if best_side is not None:
            return best_side[1]

        if csy0 - 6.0 <= cy <= csy1 + 6.0:
            if abs(cx - csx0) <= 6.0:
                return "w"
            if abs(cx - csx1) <= 6.0:
                return "e"
        if csx0 - 6.0 <= cx <= csx1 + 6.0:
            if abs(cy - csy0) <= 6.0:
                return "n"
            if abs(cy - csy1) <= 6.0:
                return "s"

        if csx0 < cx < csx1 and csy0 < cy < csy1:
            return "move"
        return "pan"

    def _on_canvas_hover(self, e: tk.Event) -> None:
        if self._pick_mode_active:
            return
        if not self._crop_mode_active or self._crop_drag_state is not None:
            return
        if e.widget is not self.canvas_svg:
            return
        mode = self._hit_test_crop_overlay(e.widget, float(e.x), float(e.y))
        cursor_map = {
            "nw": "size_nw_se",
            "se": "size_nw_se",
            "ne": "size_ne_sw",
            "sw": "size_ne_sw",
            "n": "sb_v_double_arrow",
            "s": "sb_v_double_arrow",
            "e": "sb_h_double_arrow",
            "w": "sb_h_double_arrow",
            "move": "fleur",
            "pan": "fleur",
        }
        try:
            e.widget.configure(cursor=cursor_map.get(mode, "fleur"))
        except tk.TclError:
            try:
                e.widget.configure(cursor="crosshair")
            except tk.TclError:
                pass

    def _on_pan_start(self, e: tk.Event) -> None:
        if self._pick_mode_active:
            self._drag_origin = None
            pt = self._map_canvas_point_to_source(e.widget, float(e.x), float(e.y))
            if pt is not None:
                self.add_transparency_pick_at_source(pt[0], pt[1], trigger_preview=True)
            return
        if self._crop_mode_active and self._draft_square_crop is not None:
            mode = self._hit_test_crop_overlay(e.widget, float(e.x), float(e.y))
            if mode != "pan":
                geom = self._canvas_img_geom.get(id(e.widget))
                if geom is not None and geom[2] > 0.0:
                    self._crop_drag_state = (mode, e.x, e.y, float(geom[2]), self._draft_square_crop)
                    self._drag_origin = None
                    return
        self._crop_drag_state = None
        self._drag_origin = (e.x, e.y)

    def _on_pan_drag(self, e: tk.Event) -> None:
        if self._pick_mode_active:
            return
        if self._crop_mode_active and self._crop_drag_state is not None and self._source_image is not None:
            mode, sx_start, sy_start, scale, orig_box = self._crop_drag_state
            dx = (e.x - sx_start) / max(scale, 1e-6)
            dy = (e.y - sy_start) / max(scale, 1e-6)
            min_s = min(16, min(self._source_image.size))
            self._draft_square_crop = compute_square_crop_drag(
                orig_box,
                mode,
                dx,
                dy,
                self._source_image.width,
                self._source_image.height,
                min_side=min_s,
            )
            self._update_crop_status_ui()
            self._redraw_compare_canvases()
            return
        if self._drag_origin is None:
            return
        dx = e.x - self._drag_origin[0]
        dy = e.y - self._drag_origin[1]
        self._drag_origin = (e.x, e.y)
        self._pan_x += dx
        self._pan_y += dy
        self._redraw_compare_canvases()

    def _on_pan_end(self, _e: tk.Event) -> None:
        self._crop_drag_state = None
        self._drag_origin = None

    def _get_svg_display_image(self, scale: float) -> Image.Image:
        assert self._last_result is not None
        bw, bh = self._last_result.prepared.image.size
        if scale <= 1.05:
            return self._last_result.svg_rendered
        rw = max(1, int(round(bw * scale)))
        rh = max(1, int(round(bh * scale)))
        max_dim = max(rw, rh)
        if max_dim > MAX_SVG_ZOOM_RENDER_DIM:
            ratio = MAX_SVG_ZOOM_RENDER_DIM / float(max_dim)
            rw = max(1, int(round(rw * ratio)))
            rh = max(1, int(round(rh * ratio)))
        res_id = id(self._last_result)
        if self._svg_zoom_cache is not None:
            c_id, c_w, c_h, c_im = self._svg_zoom_cache
            if c_id == res_id and c_w == rw and c_h == rh:
                return c_im
        rendered = render_svg_to_pil(self._last_result.svg_text, width=rw, height=rh)
        self._svg_zoom_cache = (res_id, rw, rh, rendered)
        return rendered

    def _render_canvas_pair(
        self,
        canvas: tk.Canvas,
        img: Image.Image,
        *,
        logical_size: Optional[Tuple[int, int]] = None,
        overlay_square_crop: Optional[Tuple[int, int, int, int]] = None,
    ) -> ImageTk.PhotoImage:
        cw = max(120, canvas.winfo_width())
        ch = max(120, canvas.winfo_height())
        p = mtk.ThemeManager.instance().palette
        canvas.configure(bg=p.surface_alt)
        canvas.delete("all")
        self._canvas_crop_rect.pop(id(canvas), None)

        lw, lh = logical_size if logical_size is not None else (img.width, img.height)
        if self._zoom_mode == "Fit":
            scale = min((cw - 16) / max(1, lw), (ch - 16) / max(1, lh))
        else:
            pct = float(self._zoom_mode.rstrip("%")) / 100.0
            scale = pct

        disp_w = max(1, int(round(lw * scale)))
        disp_h = max(1, int(round(lh * scale)))

        # Center + pan coordinates
        x0 = (cw - disp_w) // 2 + int(round(self._pan_x))
        y0 = (ch - disp_h) // 2 + int(round(self._pan_y))
        self._canvas_img_geom[id(canvas)] = (x0, y0, scale, lw, lh)

        # Crop visible sub-region for fast 400% zoom rendering without multi-megapixel PhotoImage spikes
        vx0 = max(0, x0)
        vy0 = max(0, y0)
        vx1 = min(cw, x0 + disp_w)
        vy1 = min(ch, y0 + disp_h)
        if vx1 <= vx0 or vy1 <= vy0:
            return ImageTk.PhotoImage(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))

        eff_sx = disp_w / max(1.0, float(img.width))
        eff_sy = disp_h / max(1.0, float(img.height))
        sx0 = max(0.0, (vx0 - x0) / eff_sx)
        sy0 = max(0.0, (vy0 - y0) / eff_sy)
        sx1 = min(float(img.width), (vx1 - x0) / eff_sx)
        sy1 = min(float(img.height), (vy1 - y0) / eff_sy)

        out_w = max(1, vx1 - vx0)
        out_h = max(1, vy1 - vy0)
        ix0 = int(round(sx0))
        iy0 = int(round(sy0))
        if (
            abs(sx0 - ix0) < 1e-3
            and abs(sy0 - iy0) < 1e-3
            and int(round(sx1 - sx0)) == out_w
            and int(round(sy1 - sy0)) == out_h
            and ix0 + out_w <= img.width
            and iy0 + out_h <= img.height
        ):
            resized = img.crop((ix0, iy0, ix0 + out_w, iy0 + out_h))
        else:
            crop = img.crop((sx0, sy0, sx1, sy1))
            resample = Image.Resampling.NEAREST if max(eff_sx, eff_sy) >= 3.0 else Image.Resampling.LANCZOS
            resized = crop.resize((out_w, out_h), resample)

        bg_tile = _make_checkerboard(out_w, out_h, tile=10, dark=(p.mode == "dark")).convert("RGBA")
        bg_tile.alpha_composite(resized)

        crop_screen: Optional[Tuple[int, int, int, int, int, int]] = None
        if overlay_square_crop is not None:
            bx0, by0, bx1, by1 = normalize_square_crop(overlay_square_crop, lw, lh, min_side=1)
            csx0 = x0 + int(round(bx0 * scale))
            csy0 = y0 + int(round(by0 * scale))
            cside = max(4, int(round((bx1 - bx0) * scale)))
            csx1 = csx0 + cside
            csy1 = csy0 + cside
            tx0 = max(0, min(out_w, csx0 - vx0))
            ty0 = max(0, min(out_h, csy0 - vy0))
            tx1 = max(0, min(out_w, csx1 - vx0))
            ty1 = max(0, min(out_h, csy1 - vy0))
            tile_arr = np.array(bg_tile, dtype=np.uint8)
            keep = np.zeros((out_h, out_w), dtype=bool)
            if tx1 > tx0 and ty1 > ty0:
                keep[ty0:ty1, tx0:tx1] = True
            outside = ~keep
            if np.any(outside):
                tile_arr[outside, :3] = (tile_arr[outside, :3].astype(np.float32) * 0.42).astype(np.uint8)
            bg_tile = Image.fromarray(tile_arr, mode="RGBA")
            crop_screen = (csx0, csy0, csx1, csy1, bx1 - bx0, by1 - by0)

        photo = ImageTk.PhotoImage(bg_tile)
        canvas.create_image(vx0, vy0, image=photo, anchor="nw")

        if crop_screen is not None:
            csx0, csy0, csx1, csy1, side_w, side_h = crop_screen
            cside = csx1 - csx0
            self._canvas_crop_rect[id(canvas)] = (csx0, csy0, csx1, csy1)
            canvas.create_rectangle(
                csx0, csy0, csx1, csy1,
                outline="#0B1220", width=3,
                tags=("crop_overlay", "crop_frame_shadow"),
            )
            canvas.create_rectangle(
                csx0, csy0, csx1, csy1,
                outline="#38BDF8", width=2,
                tags=("crop_overlay", "crop_frame"),
            )
            if cside >= 48:
                for frac in (1.0 / 3.0, 2.0 / 3.0):
                    gx = csx0 + int(round(cside * frac))
                    gy = csy0 + int(round(cside * frac))
                    canvas.create_line(
                        gx, csy0, gx, csy1,
                        fill="#BAE6FD", width=1, dash=(3, 3),
                        tags=("crop_overlay", "crop_grid"),
                    )
                    canvas.create_line(
                        csx0, gy, csx1, gy,
                        fill="#BAE6FD", width=1, dash=(3, 3),
                        tags=("crop_overlay", "crop_grid"),
                    )

            arm = min(18, max(8, cside // 6))
            for bx, by, dx_s, dy_s in (
                (csx0, csy0, 1, 1),
                (csx1, csy0, -1, 1),
                (csx0, csy1, 1, -1),
                (csx1, csy1, -1, -1),
            ):
                canvas.create_line(
                    bx, by, bx + dx_s * arm, by,
                    fill="#FFFFFF", width=3,
                    tags=("crop_overlay", "crop_bracket"),
                )
                canvas.create_line(
                    bx, by, bx, by + dy_s * arm,
                    fill="#FFFFFF", width=3,
                    tags=("crop_overlay", "crop_bracket"),
                )

            cx_mid = (csx0 + csx1) // 2
            cy_mid = (csy0 + csy1) // 2
            hs = 5
            for hname, hx, hy in (
                ("nw", csx0, csy0),
                ("n", cx_mid, csy0),
                ("ne", csx1, csy0),
                ("e", csx1, cy_mid),
                ("se", csx1, csy1),
                ("s", cx_mid, csy1),
                ("sw", csx0, csy1),
                ("w", csx0, cy_mid),
            ):
                canvas.create_rectangle(
                    hx - hs, hy - hs, hx + hs, hy + hs,
                    fill="#FFFFFF", outline="#0284C7", width=2,
                    tags=("crop_overlay", "crop_handle", f"crop_handle_{hname}"),
                )

            lbl_y = csy0 - 16 if csy0 >= 28 else csy0 + 18
            lbl_txt = f"{side_w}x{side_h} px"
            tid = canvas.create_text(
                cx_mid, lbl_y,
                text=lbl_txt,
                fill="#FFFFFF",
                font=("Segoe UI", 9, "bold"),
                tags=("crop_overlay", "crop_label"),
            )
            tb = canvas.bbox(tid)
            if tb is not None:
                bg_id = canvas.create_rectangle(
                    tb[0] - 6, tb[1] - 2, tb[2] + 6, tb[3] + 2,
                    fill="#0F172A", outline="#38BDF8", width=1,
                    tags=("crop_overlay", "crop_label_bg"),
                )
                canvas.tag_lower(bg_id, tid)

        return photo

    def _render_empty_welcome_canvas(self, canvas: tk.Canvas, *, title: str, subtitle: str) -> None:
        cw = max(220, canvas.winfo_width())
        ch = max(200, canvas.winfo_height())
        p = mtk.ThemeManager.instance().palette
        canvas.configure(bg=p.surface_alt)
        canvas.delete("all")
        self._canvas_crop_rect.pop(id(canvas), None)

        cx, cy = cw // 2, ch // 2
        badge = render_brand_squircle_badge(112)
        if badge is not None:
            photo = ImageTk.PhotoImage(badge, master=self.root)
            self._tk_refs.append(photo)
            canvas.create_image(cx, cy - 26, image=photo, anchor="center", tags=("brand_welcome",))
        canvas.create_text(
            cx,
            cy + 48,
            text=title,
            fill=p.fg,
            font=("Segoe UI", 11, "bold"),
            anchor="center",
            tags=("brand_welcome",),
        )
        canvas.create_text(
            cx,
            cy + 70,
            text=subtitle,
            fill=p.fg_muted,
            font=("Segoe UI", 9),
            anchor="center",
            tags=("brand_welcome",),
        )

    def _redraw_compare_canvases(self) -> None:
        if self._source_image is None:
            self._tk_refs.clear()
            self._render_empty_welcome_canvas(
                self.canvas_orig,
                title="Original Picture",
                subtitle="Browse a picture or paste from Clipboard to begin",
            )
            self._render_empty_welcome_canvas(
                self.canvas_svg,
                title="Vector .SVG & 12-Slot .ICNS",
                subtitle="1:1 Square Crop • Smart Transparency • True SVG Gradients",
            )
            return
        self._tk_refs.clear()

        # Left panel always renders the untouched original source image
        p1 = self._render_canvas_pair(self.canvas_orig, self._source_image)

        # Right panel carries all interactive edits, overlays, and vector SVG output
        if self._crop_mode_active:
            crop_box = self._draft_square_crop
            if (
                self._last_result is not None
                and self._last_result.prepared.image.size == self._source_image.size
                and self._last_result.prepared.crop_box == (0, 0, self._source_image.width, self._source_image.height)
            ):
                src_sz = self._source_image.size
                cw_svg = max(120, self.canvas_svg.winfo_width())
                ch_svg = max(120, self.canvas_svg.winfo_height())
                if self._zoom_mode == "Fit":
                    svg_scale = min((cw_svg - 16) / max(1, src_sz[0]), (ch_svg - 16) / max(1, src_sz[1]))
                else:
                    svg_scale = float(self._zoom_mode.rstrip("%")) / 100.0
                svg_img = self._get_svg_display_image(svg_scale)
                p2 = self._render_canvas_pair(
                    self.canvas_svg,
                    svg_img,
                    logical_size=src_sz,
                    overlay_square_crop=crop_box,
                )
            else:
                p2 = self._render_canvas_pair(
                    self.canvas_svg,
                    self._source_image,
                    overlay_square_crop=crop_box,
                )
        elif self._last_result is None or self._applied_square_crop != self._last_result_square_crop:
            preview_src = (
                self._source_image.crop(self._applied_square_crop)
                if self._applied_square_crop is not None
                else self._source_image
            )
            p2 = self._render_canvas_pair(self.canvas_svg, preview_src)
        else:
            edited_size = self._last_result.prepared.image.size
            cw_svg = max(120, self.canvas_svg.winfo_width())
            ch_svg = max(120, self.canvas_svg.winfo_height())
            if self._zoom_mode == "Fit":
                svg_scale = min((cw_svg - 16) / max(1, edited_size[0]), (ch_svg - 16) / max(1, edited_size[1]))
            else:
                svg_scale = float(self._zoom_mode.rstrip("%")) / 100.0
            svg_img = self._get_svg_display_image(svg_scale)
            p2 = self._render_canvas_pair(self.canvas_svg, svg_img, logical_size=edited_size)

        self._tk_refs.extend([p1, p2])
        if self._worker_busy:
            self._apply_busy_overlay_to_canvases((self.canvas_svg,))

    def _redraw_icns_strip(self) -> None:
        cv = self.canvas_icns
        p = mtk.ThemeManager.instance().palette
        cv.configure(bg=p.surface_alt)
        cv.delete("all")
        self._icns_tk_refs.clear()

        if self._last_result is None:
            badge_48 = render_brand_squircle_badge(48)
            tx = 20
            if badge_48 is not None:
                photo_b = ImageTk.PhotoImage(badge_48, master=self.root)
                self._icns_tk_refs.append(photo_b)
                cv.create_image(36, 40, image=photo_b, anchor="center", tags=("brand_welcome",))
                tx = 72
            cv.create_text(
                tx,
                40,
                text="Run preview to inspect decoded ICNS sizes (16x16 through 1024x1024 @2x Retina).",
                fill=p.fg_muted,
                anchor="w",
            )
            if self._worker_busy:
                self._apply_busy_overlay_to_canvases((cv,))
            return

        cw = max(600, cv.winfo_width())
        ch = max(250, cv.winfo_height())
        half_h = (ch - 14) // 2

        # Top band: Dark macOS Dock preview; Bottom band: Light macOS Finder preview
        cv.create_rectangle(6, 4, cw - 6, half_h, fill="#1B1F2A", outline=p.border, width=1)
        cv.create_rectangle(6, half_h + 6, cw - 6, ch - 4, fill="#F3F5F8", outline=p.border, width=1)
        cv.create_text(14, 16, text="DARK SURFACE", fill="#8F9BB3", font=("Segoe UI", 8, "bold"), anchor="w")
        cv.create_text(14, half_h + 18, text="LIGHT SURFACE", fill="#4E5969", font=("Segoe UI", 8, "bold"), anchor="w")

        imgs = self._last_result.icns_inspection.images_by_size
        preview_specs = [
            ((16, 16, 1), "16", 1),
            ((32, 32, 1), "32", 1),
            ((48, 48, 1), "48", 1),
            ((64, 64, 1), "64", 1),
            ((128, 128, 1), "128", 1),
            ((16, 16, 1), "16x4x", 4),
            ((32, 32, 1), "32x3x", 3),
        ]

        x_cursor = 20
        for key, label, zoom in preview_specs:
            im = imgs.get(key)
            if im is None:
                continue
            show_im = im if zoom == 1 else im.resize((im.width * zoom, im.height * zoom), Image.Resampling.NEAREST)
            # Keep strip height tidy (cap 96px tall on strip)
            if show_im.height > half_h - 32:
                ratio = (half_h - 32) / show_im.height
                show_im = show_im.resize(
                    (max(1, int(round(show_im.width * ratio))), max(1, int(round(show_im.height * ratio)))),
                    Image.Resampling.LANCZOS,
                )
            photo = ImageTk.PhotoImage(show_im)
            self._icns_tk_refs.append(photo)

            cy_top = 12 + (half_h - 12) // 2
            cy_bot = half_h + 12 + (ch - half_h - 16) // 2
            cx = x_cursor + show_im.width // 2

            cv.create_image(cx, cy_top - 6, image=photo, anchor="center")
            cv.create_text(cx, half_h - 10, text=label, fill="#CBD5E1", font=("Segoe UI", 8), anchor="center")

            cv.create_image(cx, cy_bot - 6, image=photo, anchor="center")
            cv.create_text(cx, ch - 14, text=label, fill="#334155", font=("Segoe UI", 8), anchor="center")

            x_cursor += show_im.width + 24

        if self._worker_busy:
            self._apply_busy_overlay_to_canvases((cv,))

    def _open_output_folder(self) -> None:
        target = Path(self.entry_out_dir.get().strip() or self._var_out_dir.get())
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(target))
        else:  # pragma: no cover
            import webbrowser
            webbrowser.open(target.as_uri())

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# CLI & Direct Entrypoint
# ---------------------------------------------------------------------------


def _parse_cli_picks(
    src_im: Image.Image,
    raw_picks: Optional[Sequence[str]],
) -> Tuple[TransparencyPick, ...]:
    if not raw_picks:
        return ()
    arr = np.asarray(src_im.convert("RGBA"))
    h, w = arr.shape[:2]
    picks: List[TransparencyPick] = []
    for item in raw_picks:
        parts = [p.strip() for p in item.split(",")]
        if len(parts) != 2:
            raise ValueError(f"Invalid --pick coordinate {item!r}; expected 'x,y'.")
        vx = float(parts[0])
        vy = float(parts[1])
        if 0.0 <= vx < 1.0 and 0.0 <= vy < 1.0 and ("." in parts[0] or "." in parts[1]):
            sx = int(np.clip(round(vx * max(w - 1, 1)), 0, max(w - 1, 0)))
            sy = int(np.clip(round(vy * max(h - 1, 1)), 0, max(h - 1, 0)))
        else:
            sx = int(np.clip(round(vx), 0, max(w - 1, 0)))
            sy = int(np.clip(round(vy), 0, max(h - 1, 0)))
        patch = arr[max(0, sy - 1) : min(h, sy + 2), max(0, sx - 1) : min(w, sx + 2), :3]
        med = np.median(patch.reshape(-1, 3), axis=0)
        picks.append(
            TransparencyPick(
                x_norm=sx / float(max(w - 1, 1)),
                y_norm=sy / float(max(h - 1, 1)),
                rgb=(int(round(med[0])), int(round(med[1])), int(round(med[2]))),
                tolerance=26.0,
            )
        )
    return tuple(picks)


def _parse_cli_square_crop(
    src_im: Image.Image,
    raw_crop: Optional[str],
) -> Optional[Tuple[int, int, int, int]]:
    if not raw_crop:
        return None
    parts = [p.strip() for p in raw_crop.split(",")]
    if len(parts) not in (3, 4):
        raise ValueError(f"Invalid --crop-square specification {raw_crop!r}; expected 'x,y,size' or 'x0,y0,x1,y1'.")
    vals = [int(round(float(p))) for p in parts]
    if len(vals) == 3:
        box: Tuple[int, int, int, int] | Tuple[int, int, int] = (vals[0], vals[1], vals[2])
    else:
        box = (vals[0], vals[1], vals[2], vals[3])
    return normalize_square_crop(box, src_im.width, src_im.height, min_side=1)


def _run_cli(args: argparse.Namespace) -> int:
    in_path = Path(args.cli)
    if not in_path.is_file():
        print(f"Error: input image not found: {in_path}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else in_path.parent
    stem = args.name or in_path.stem
    base_template = SVG_PRESETS[args.preset][1]
    svg_opts = SVGOptions(
        **{
            **base_template.__dict__,
            "true_gradients": not args.no_gradients,
            "clean_aa_lines": not args.no_line_cleanup,
        }
    )

    src_im = load_source_image(in_path)
    picks = _parse_cli_picks(src_im, args.pick)
    square_crop = _parse_cli_square_crop(src_im, args.crop_square)

    p_opts = PrepareOptions(
        bg_mode=args.bg_mode,
        clean_jpeg_noise=not args.no_denoise,
        auto_trim=bool(args.trim),
        padding_pct=float(args.padding),
        deepen_diff=not args.no_deepen_diff,
        deepen_strength=float(args.deepen_strength),
        picked_transparencies=picks,
        square_crop=square_crop,
        rounded_corners=bool(args.rounded_corners),
        corner_radius_pct=float(args.corner_radius),
        rounded_border=bool(args.border),
        border_width_pct=float(args.border_width),
        border_color_rgb=_parse_hex_rgb(args.border_color, (76, 194, 255)),
    )

    res = run_conversion(
        src_im,
        prepare_opts=p_opts,
        svg_opts=svg_opts,
        icns_sizes=ICNS_TIERS,
        small_icon_sharpen=not args.no_sharpen,
        out_dir=out_dir,
        stem=stem,
        write_svg=not args.no_svg,
        write_icns=not args.no_icns,
        write_ico=bool(args.ico),
    )

    print(f"Input:      {in_path} ({res.source_image.width}x{res.source_image.height})")
    print(f"Mode/Preset:{args.bg_mode} / {args.preset}")
    print(f"SVG PSNR:   {res.psnr_db:.2f} dB | Similarity: {res.similarity_pct:.2f}%")
    print(f"SVG Output: {res.path_count:,} paths, {res.gradient_count} SVG gradients, {_format_bytes(res.svg_bytes)}")
    print(
        f"ICNS Slots: {len(res.icns_inspection.verified_sizes)} verified sizes "
        f"({_format_bytes(len(res.icns_bytes))}, TOC={'yes' if res.icns_inspection.has_toc else 'no'})"
    )
    for wf in res.written_files:
        print(f"Wrote:      {wf}")
    print(f"Elapsed:    {res.elapsed_sec:.2f}s")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Convert any picture to faithful .svg and multi-size .icns.")
    ap.add_argument("--version", action="version", version=f"Iconverter v{__version__}")
    ap.add_argument("image", nargs="?", help="Optional initial picture path for GUI mode.")
    ap.add_argument("--cli", metavar="INPUT", help="Run headless CLI conversion on INPUT image and exit.")
    ap.add_argument("--out-dir", help="Output directory (defaults to input directory in CLI mode).")
    ap.add_argument("--name", help="Output base filename stem (defaults to input stem).")
    ap.add_argument(
        "--bg-mode",
        choices=("faithful", "transparent", "squircle"),
        default="faithful",
        help="Background mode (default: faithful).",
    )
    ap.add_argument(
        "--preset",
        choices=tuple(SVG_PRESETS.keys()),
        default="ultra",
        help="SVG fidelity preset (default: ultra).",
    )
    ap.add_argument("--trim", action="store_true", help="Auto-trim outer uniform margins.")
    ap.add_argument("--padding", type=float, default=6.0, help="Trim margin padding percent (default: 6.0).")
    ap.add_argument("--crop-square", metavar="X,Y,SIZE", help="Crop a 1:1 square region before conversion (X,Y,SIZE or X0,Y0,X1,Y1).")
    ap.add_argument("--rounded-corners", action="store_true", help="Apply rounded corners to .svg, .icns, and .ico outputs.")
    ap.add_argument("--corner-radius", type=float, default=18.0, help="Rounded corner radius percent 0..50 (default: 18.0).")
    ap.add_argument("--border", action="store_true", help="Draw a border around the rounded/frame boundary.")
    ap.add_argument("--border-width", type=float, default=3.0, help="Border thickness percent 0.5..25 (default: 3.0).")
    ap.add_argument("--border-color", default="#4CC2FF", help="Border colour hex #RRGGBB (default: #4CC2FF).")
    ap.add_argument("--no-deepen-diff", action="store_true", help="Disable Lab chroma separation deepening for transparency.")
    ap.add_argument("--deepen-strength", type=float, default=55.0, help="Color separation depth 0..100 (default: 55.0).")
    ap.add_argument("--pick", action="append", metavar="X,Y", help="Add smart transparency picker seed coordinate (repeatable).")
    ap.add_argument("--no-gradients", action="store_true", help="Disable true SVG gradient reconstruction.")
    ap.add_argument(
        "--no-line-cleanup",
        action="store_true",
        help="Disable subpixel anti-aliasing deblurring and stroke-coherent line cleanup.",
    )
    ap.add_argument("--no-denoise", action="store_true", help="Disable edge-safe JPEG noise cleanup.")
    ap.add_argument("--no-sharpen", action="store_true", help="Disable small-icon crispness boost.")
    ap.add_argument("--no-svg", action="store_true", help="Skip writing .svg file in CLI mode.")
    ap.add_argument("--no-icns", action="store_true", help="Skip writing .icns file in CLI mode.")
    ap.add_argument("--ico", action="store_true", help="Also write a Windows .ico file.")
    ap.add_argument("--theme", default="fluent_dark", help="Initial ModernTtk theme name.")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.cli:
        return _run_cli(args)
    app = IconConverterApp(initial_image=args.image, themename=args.theme)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
