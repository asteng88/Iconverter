from __future__ import annotations

import io
from pathlib import Path
import struct
import subprocess
import sys
import time

import numpy as np
from PIL import IcnsImagePlugin, Image, ImageDraw
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import icon_converter as ic  # noqa: E402


PORTER_SOURCE = REPO_ROOT / "icons" / "porter_source.jpg"


def _make_synthetic_picture(size: tuple[int, int] = (240, 160), bg: tuple[int, int, int, int] = (255, 255, 255, 255)) -> Image.Image:
    im = Image.new("RGBA", size, bg)
    draw = ImageDraw.Draw(im)
    w, h = size
    draw.rounded_rectangle([w // 6, h // 6, w * 5 // 6, h * 5 // 6], radius=18, fill=(24, 118, 210, 255), outline=(12, 24, 44, 255), width=4)
    draw.ellipse([w // 3, h // 3, w * 2 // 3, h * 2 // 3], fill=(255, 160, 40, 255))
    return im


@pytest.mark.skipif(not PORTER_SOURCE.is_file(), reason="Optional icons/porter_source.jpg fixture not present")
def test_porter_source_faithful_svg_and_icns(tmp_path: Path) -> None:
    assert PORTER_SOURCE.is_file(), "Test fixture icons/porter_source.jpg must exist"

    res = ic.run_conversion(
        PORTER_SOURCE,
        prepare_opts=ic.PrepareOptions(bg_mode="faithful", clean_jpeg_noise=True, auto_trim=False),
        svg_opts=ic.SVG_PRESETS["ultra"][1],
        icns_sizes=ic.ICNS_TIERS,
        small_icon_sharpen=True,
        out_dir=tmp_path,
        stem="porter_test",
        write_svg=True,
        write_icns=True,
    )

    svg_file = tmp_path / "porter_test.svg"
    icns_file = tmp_path / "porter_test.icns"
    assert svg_file.is_file()
    assert icns_file.is_file()

    svg_text = svg_file.read_text(encoding="utf-8")
    assert "<svg" in svg_text
    assert 'viewBox="0 0 ' in svg_text
    assert "<path" in svg_text
    assert "<image" not in svg_text.lower()
    assert res.path_count > 100
    assert res.gradient_count > 0
    assert "<lineargradient" in svg_text.lower() or "<radialgradient" in svg_text.lower()
    assert res.psnr_db >= 31.0
    assert res.similarity_pct >= 98.5

    icns_bytes = icns_file.read_bytes()
    assert icns_bytes[:4] == b"icns"
    assert struct.unpack(">I", icns_bytes[4:8])[0] == len(icns_bytes)

    insp = ic.inspect_icns(icns_bytes)
    assert insp.has_toc is True
    expected_12 = set(IcnsImagePlugin.IcnsFile.SIZES.keys())
    assert set(insp.verified_sizes) == expected_12
    for key, decoded_im in insp.images_by_size.items():
        expected_px = (key[0] * key[2], key[1] * key[2])
        assert decoded_im.size == expected_px
        # Ensure icon content is non-empty and not flat single-color
        arr = np.asarray(decoded_im)
        assert arr[..., :3].std() > 5.0


def test_transparent_and_squircle_modes() -> None:
    src = _make_synthetic_picture((256, 256))

    prep_trans = ic.prepare_master_image(src, ic.PrepareOptions(bg_mode="transparent", clean_jpeg_noise=False))
    assert prep_trans.has_transparency is True
    assert prep_trans.image.getpixel((0, 0))[3] == 0
    assert prep_trans.image.getpixel((128, 128))[3] == 255
    svg_trans = ic.convert_image_to_svg(prep_trans, ic.SVG_PRESETS["balanced"][1])
    rendered_trans = ic.render_svg_to_pil(svg_trans, width=256, height=256)
    assert rendered_trans.getpixel((0, 0))[3] == 0
    assert rendered_trans.getpixel((128, 128))[3] > 200

    prep_squircle = ic.prepare_master_image(src, ic.PrepareOptions(bg_mode="squircle", clean_jpeg_noise=False))
    assert prep_squircle.image.size == (1024, 1024)
    assert prep_squircle.image.getpixel((0, 0))[3] == 0
    assert prep_squircle.image.getpixel((512, 512))[3] == 255


def test_nonsquare_aspect_ratio_preserved() -> None:
    rect = _make_synthetic_picture((320, 160), bg=(255, 255, 255, 255))
    prep = ic.prepare_master_image(rect, ic.PrepareOptions(bg_mode="faithful", clean_jpeg_noise=False))
    assert prep.image.size == (320, 160)
    assert prep.square_icon.size == (1024, 1024)

    # Top and bottom padding bars should remain pure white, while center has colored rectangle
    assert prep.square_icon.getpixel((512, 40))[:3] == (255, 255, 255)
    assert prep.square_icon.getpixel((512, 512))[:3] != (255, 255, 255)


def test_rle24_exact_3wh_length_guard_roundtrip() -> None:
    # Construct a 16x16 image whose raw alternating pattern would compress near 3*W*H bytes
    # and verify encode_icns_rle24 never outputs exactly 3*W*H bytes and decodes identically via Pillow.
    w, h = 16, 16
    im = Image.new("RGBA", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            # Alternating checkerboard prevents 3+ runs; literal 128-byte blocks add +1 byte/128,
            # while testing various patterns ensures len != 3*w*h and Pillow read_32 parses cleanly.
            v = ((x + y) * 37) % 255
            px[x, y] = (v, (v * 3) % 255, (v * 7) % 255, 255)

    rle = ic.encode_icns_rle24(im)
    assert len(rle) != w * h * 3

    decoded_dict = IcnsImagePlugin.read_32(io.BytesIO(rle), (0, len(rle)), (16, 16, 1))
    assert decoded_dict["RGB"].tobytes() == im.convert("RGB").tobytes()


def test_cli_entrypoint(tmp_path: Path) -> None:
    src_path = tmp_path / "sample_rect.png"
    _make_synthetic_picture((180, 120)).save(src_path)

    rc = ic.main([
        "--cli",
        str(src_path),
        "--out-dir",
        str(tmp_path / "out"),
        "--name",
        "cli_icon",
        "--bg-mode",
        "transparent",
        "--preset",
        "balanced",
        "--ico",
    ])
    assert rc == 0
    assert (tmp_path / "out" / "cli_icon.svg").is_file()
    assert (tmp_path / "out" / "cli_icon.icns").is_file()
    assert (tmp_path / "out" / "cli_icon.ico").is_file()


def test_modernttk_gui_startup_and_export(tmp_path: Path) -> None:
    src_path = tmp_path / "gui_sample.png"
    _make_synthetic_picture((128, 128)).save(src_path)

    app = ic.IconConverterApp(initial_image=src_path, themename="fluent_dark")
    try:
        app.root.update()
        # Wait briefly for background initial preview worker to finish
        for _ in range(200):
            app.drain_ui_queue()
            app.root.update()
            if app._last_result is not None and not app._worker_busy:
                break
            time.sleep(0.03)

        assert app._last_result is not None
        assert len(app._last_result.icns_inspection.verified_sizes) == 12
        assert app.btn_browse_out_dir.winfo_ismapped() == 1
        assert app.btn_browse_out_dir.winfo_width() >= app.btn_browse_out_dir.winfo_reqwidth() - 2
        assert (
            app.btn_browse_out_dir.winfo_x() + app.btn_browse_out_dir.winfo_width()
            <= app.btn_browse_out_dir.master.winfo_width() + 2
        )

        # Toggle theme and verify widgets survive cleanly
        app._toggle_light_dark()
        app.root.update()

        # Trigger export into tmp_path
        app._var_out_dir.set(str(tmp_path / "gui_out"))
        app._var_stem.set("gui_exported")
        app.export_outputs()
        for _ in range(200):
            app.drain_ui_queue()
            app.root.update()
            if not app._worker_busy and (tmp_path / "gui_out" / "gui_exported.icns").is_file():
                break
            time.sleep(0.03)

        assert (tmp_path / "gui_out" / "gui_exported.svg").is_file()
        assert (tmp_path / "gui_out" / "gui_exported.icns").is_file()
    finally:
        app.root.destroy()


def test_faithful_svg_gradients_linear_and_radial() -> None:
    w, h = 256, 256
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    rgb = np.stack(
        [
            np.clip(255.0 - xx * 0.7, 0, 255),
            np.clip(60.0 + yy * 0.6, 0, 255),
            np.clip(120.0 + xx * 0.5, 0, 255),
        ],
        axis=2,
    ).astype(np.uint8)
    dist_c = np.sqrt((xx - 128.0) ** 2 + (yy - 128.0) ** 2)
    orb = dist_c <= 65.0
    t_orb = np.clip(dist_c / 65.0, 0.0, 1.0)
    rgb[orb, 0] = np.clip(255.0 * (1.0 - t_orb[orb] * 0.6), 0, 255).astype(np.uint8)
    rgb[orb, 1] = np.clip(230.0 * (1.0 - t_orb[orb] * 0.8), 0, 255).astype(np.uint8)
    rgb[orb, 2] = np.clip(80.0 + 170.0 * t_orb[orb], 0, 255).astype(np.uint8)
    im = Image.fromarray(rgb, mode="RGB").convert("RGBA")
    ImageDraw.Draw(im).ellipse([63, 63, 193, 193], outline=(20, 24, 45, 255), width=4)

    res = ic.run_conversion(
        im,
        prepare_opts=ic.PrepareOptions(bg_mode="faithful", clean_jpeg_noise=False),
        svg_opts=ic.SVGOptions(preset_key="ultra", true_gradients=True, trace_scale=1.0),
    )
    assert res.gradient_count >= 1
    assert "<radialgradient" in res.svg_text.lower() or "<lineargradient" in res.svg_text.lower()
    assert res.psnr_db >= 27.0


@pytest.mark.skipif(not PORTER_SOURCE.is_file(), reason="Optional icons/porter_source.jpg fixture not present")
def test_smart_transparency_pockets_deepen_and_picker() -> None:
    assert PORTER_SOURCE.is_file()
    src = ic.load_source_image(PORTER_SOURCE)
    src_arr = np.asarray(src)

    # 1) Default transparent with deepen_diff: clears enclosed white pockets, protects artwork
    prep = ic.prepare_master_image(
        src,
        ic.PrepareOptions(bg_mode="transparent", deepen_diff=True, deepen_strength=55.0),
    )
    pa = np.asarray(prep.image)[..., 3]
    # Enclosed pockets should become transparent
    assert pa[791, 281] <= 10  # between legs
    assert pa[638, 412] <= 10  # between body and cart
    assert pa[821, 654] <= 10  # between wheels
    assert pa[158, 637] <= 10  # inside floating binary '0'
    # Protected interior artwork stays opaque
    assert pa[245, 330] >= 250  # face
    assert pa[525, 575] >= 250  # floppy rainbow label
    assert pa[560, 570] >= 250  # floppy grey body
    assert pa[699, 548] >= 250  # white server highlight

    # 2) Interactive picker clicks on floor shadow + cyan glow clear shadow & glow while keeping grey floppy body opaque
    picks = (
        ic.TransparencyPick(
            x_norm=300 / 1023.0,
            y_norm=852 / 1023.0,
            rgb=tuple(int(v) for v in src_arr[852, 300, :3]),
        ),
        ic.TransparencyPick(
            x_norm=620 / 1023.0,
            y_norm=280 / 1023.0,
            rgb=tuple(int(v) for v in src_arr[280, 620, :3]),
        ),
    )
    res_p = ic.run_conversion(
        src,
        prepare_opts=ic.PrepareOptions(
            bg_mode="transparent",
            deepen_diff=True,
            deepen_strength=60.0,
            picked_transparencies=picks,
        ),
        svg_opts=ic.SVG_PRESETS["balanced"][1],
    )
    pa2 = np.asarray(res_p.prepared.image)[..., 3]
    rpa2 = np.asarray(res_p.svg_rendered)[..., 3]
    assert pa2[852, 300] <= 5 and rpa2[852, 300] <= 20  # left shadow removed
    assert pa2[852, 650] <= 5 and rpa2[852, 650] <= 20  # right shadow sibling segment removed
    assert pa2[280, 620] <= 5  # picked glow cleared
    assert pa2[560, 570] >= 250 and rpa2[560, 570] >= 245  # enclosed grey floppy disk still 100% protected!
    assert pa2[699, 548] >= 250 and rpa2[699, 548] >= 245  # enclosed pure-white server highlight still 100% protected!
    assert pa2[245, 330] >= 250 and rpa2[245, 330] >= 245  # face still 100% protected!
    assert "<mask" in res_p.svg_text.lower()
    assert res_p.psnr_db >= 27.5


def test_gui_transparency_picker_workflow(tmp_path: Path) -> None:
    src_path = tmp_path / "picker_sample.png"
    _make_synthetic_picture((160, 160), bg=(235, 240, 245, 255)).save(src_path)

    script = f"""
import time
from pathlib import Path
import numpy as np
import icon_converter as ic

app = ic.IconConverterApp(initial_image=Path({str(src_path)!r}), themename="fluent_dark")
try:
    for _ in range(150):
        app.drain_ui_queue()
        app.root.update()
        if app._last_result is not None and not app._worker_busy:
            break
        time.sleep(0.02)

    app._toggle_pick_mode()
    assert app._pick_mode_active is True
    pick = app.add_transparency_pick_at_source(10, 10, trigger_preview=True)
    assert len(app._picked_transparencies) == 1
    assert pick.rgb == (235, 240, 245)
    assert app.combo_bg.get() == ic.BG_MODE_LABELS["transparent"]

    for _ in range(150):
        app.drain_ui_queue()
        app.root.update()
        if not app._worker_busy:
            break
        time.sleep(0.02)

    assert np.asarray(app._last_result.prepared.image)[10, 10, 3] == 0
    app.undo_transparency_pick()
    assert len(app._picked_transparencies) == 0
finally:
    app.root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_line_cleanup_eliminates_outline_noise_and_thin_gradient_staircase() -> None:
    import cv2

    w, h = 200, 200
    yy4, xx4 = np.mgrid[: h * 4, : w * 4].astype(np.float32)
    t_bg = np.clip((xx4 - 360.0) / 440.0, 0.0, 1.0)
    bg4 = np.stack(
        [
            255.0 - 70.0 * t_bg,
            255.0 - 15.0 * t_bg,
            np.full_like(t_bg, 255.0),
        ],
        axis=2,
    ).astype(np.uint8)
    im4 = Image.fromarray(bg4, mode="RGB").convert("RGBA")
    d4 = ImageDraw.Draw(im4)

    # Curved dark-navy tube crossing into the gradient sky on the right + thin 3px-at-1x cyan highlight
    bbox_outer = [160, 180, 920, 940]
    d4.arc(bbox_outer, start=185, end=295, fill=(10, 24, 68, 255), width=88)
    d4.arc(bbox_outer, start=187, end=293, fill=(42, 106, 176, 255), width=48)
    # 12px at 4x -> 3px wide curved highlight with smooth longitudinal shade
    pts_hi = []
    for deg in np.linspace(192.0, 288.0, 40):
        rad = np.deg2rad(deg)
        cx, cy, rx, ry = 540.0, 560.0, 342.0, 342.0
        pts_hi.append((cx + rx * np.cos(rad), cy + ry * np.sin(rad)))
    for i in range(len(pts_hi) - 1):
        frac = i / max(1, len(pts_hi) - 2)
        c_hi = (
            int(round(60 + 40 * frac)),
            int(round(227 + 20 * frac)),
            int(round(248 - 12 * frac)),
            255,
        )
        d4.line([pts_hi[i], pts_hi[i + 1]], fill=c_hi, width=12)

    clean_1x = im4.resize((w, h), Image.Resampling.LANCZOS)
    arr = np.array(clean_1x, dtype=np.int16)

    # Deterministic +-7 block/mosquito noise injected across fills AND inside dark outlines
    rng = np.random.default_rng(42)
    block_noise = rng.integers(-4, 5, size=(h // 8 + 1, w // 8 + 1, 3))
    block_up = np.repeat(np.repeat(block_noise, 8, axis=0), 8, axis=1)[:h, :w]
    px_noise = rng.integers(-3, 4, size=(h, w, 3))
    arr[..., :3] = np.clip(arr[..., :3] + block_up + px_noise, 0, 255)
    noisy_im = Image.fromarray(arr.astype(np.uint8), mode="RGBA")

    # Mask of the dark-navy outline interior core (1px inside outline boundary)
    clean_arr = np.asarray(clean_1x)[..., :3]
    dark_mask = (clean_arr[..., 0] < 30) & (clean_arr[..., 1] < 45) & (clean_arr[..., 2] < 90)
    dark_core = cv2.erode(dark_mask.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1) > 0
    assert int(dark_core.sum()) >= 200

    res_clean = ic.run_conversion(
        noisy_im,
        prepare_opts=ic.PrepareOptions(bg_mode="faithful", clean_jpeg_noise=True),
        svg_opts=ic.SVG_PRESETS["ultra"][1],
    )
    res_raw = ic.run_conversion(
        noisy_im,
        prepare_opts=ic.PrepareOptions(bg_mode="faithful", clean_jpeg_noise=False),
        svg_opts=ic.SVGOptions(preset_key="high", trace_scale=1.0, clean_aa_lines=False, true_gradients=True),
    )

    ren_clean = np.asarray(res_clean.svg_rendered)[..., :3]
    noisy_std = float(np.mean(arr[..., :3][dark_core].std(axis=0)))
    clean_std = float(np.mean(ren_clean[dark_core].std(axis=0)))

    assert clean_std < 2.5
    assert clean_std < noisy_std
    assert res_clean.psnr_db >= 27.0
    # Dark outline adjacent to right-side sky gradient must remain dark (no +0.5px sky bleed)
    right_dark = dark_core & (np.arange(w)[None, :] >= 90)
    assert int(right_dark.sum()) >= 40
    assert float(ren_clean[right_dark].mean()) < 60.0
    # Thin 3px curved cyan highlight should NOT become a staircase gradient overlay;
    # only the large smooth background gradient (at most 1-2 wide regions) is eligible
    assert res_clean.gradient_count <= 2
    assert res_clean.path_count <= res_raw.path_count or clean_std < float(
        np.mean(np.asarray(res_raw.svg_rendered)[..., :3][dark_core].std(axis=0))
    )


def test_gui_400_percent_renders_native_vector_resolution(tmp_path: Path) -> None:
    src_path = tmp_path / "zoom400_sample.png"
    _make_synthetic_picture((128, 128)).save(src_path)

    script = f"""
import time
from pathlib import Path
import icon_converter as ic

app = ic.IconConverterApp(initial_image=Path({str(src_path)!r}), themename="fluent_dark")
try:
    app.root.update()
    for _ in range(200):
        app.drain_ui_queue()
        app.root.update()
        if app._last_result is not None and not app._worker_busy:
            break
        time.sleep(0.02)

    assert app._last_result is not None
    app._on_zoom_changed("400%")
    app.root.update()
    assert app._svg_zoom_cache is not None
    _rid, rw, rh, zoom_im = app._svg_zoom_cache
    assert (rw, rh) == (512, 512)
    assert zoom_im.size == (512, 512)
finally:
    app.root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_gui_preview_busy_rotating_timer_overlay(tmp_path: Path) -> None:
    ic._DIAL_FRAMES_CACHE.clear()
    t0 = time.perf_counter()
    frames_dark = ic.build_timer_dial_frames(dark=True)
    cold_sec = time.perf_counter() - t0
    assert cold_sec < 0.75, f"Cold dial frame generation too slow: {cold_sec:.3f}s"
    t1 = time.perf_counter()
    _ = ic.build_timer_dial_frames(dark=True)
    assert (time.perf_counter() - t1) < 0.005
    assert len(frames_dark) == ic.SPINNER_FRAME_COUNT == 36
    assert frames_dark[0].mode == "RGBA"
    assert frames_dark[0].size == (168, 146)

    frames_light = ic.build_timer_dial_frames(dark=False)
    assert not np.array_equal(np.asarray(frames_dark[0]), np.asarray(frames_dark[9]))
    assert not np.array_equal(np.asarray(frames_dark[0]), np.asarray(frames_light[0]))

    badge = ic.render_timer_overlay_badge(frames_dark[0], elapsed_sec=1.2, pct=65.0, dark=True)
    assert badge.mode == "RGBA"
    assert badge.size == (168, 188)
    alpha = np.asarray(badge)[..., 3]
    smooth_aa_pixels = int(((alpha > 0) & (alpha < 255)).sum())
    assert smooth_aa_pixels >= 1500

    src_path = tmp_path / "busy_timer_sample.png"
    _make_synthetic_picture((128, 128)).save(src_path)

    script = f"""
import threading
import time
from pathlib import Path
import icon_converter as ic

app = ic.IconConverterApp(initial_image=Path({str(src_path)!r}), themename="fluent_dark")
try:
    app.root.update()
    for _ in range(200):
        app.drain_ui_queue()
        app.root.update()
        if app._last_result is not None and not app._worker_busy:
            break
        time.sleep(0.02)

    assert app._last_result is not None

    entered = threading.Event()
    gate = threading.Event()
    orig_run = ic.run_conversion

    def _gated_run(*args, **kwargs):
        entered.set()
        gate.wait(timeout=5.0)
        return orig_run(*args, **kwargs)

    ic.run_conversion = _gated_run
    try:
        app.refresh_preview()
        assert entered.wait(timeout=2.0)
        assert app._worker_busy is True

        assert app.canvas_orig.find_withtag("busy_overlay") == ()
        for cv in (app.canvas_svg, app.canvas_icns):
            assert len(cv.find_withtag("busy_overlay")) == 1

        start_idx = app._busy_frame_idx
        for _ in range(4):
            time.sleep(0.036)
            app.root.update()
        assert app._busy_frame_idx != start_idx

        app._toggle_light_dark()
        app.root.update()
        assert app.canvas_orig.find_withtag("busy_overlay") == ()
        assert len(app.canvas_svg.find_withtag("busy_overlay")) == 1
    finally:
        gate.set()
        ic.run_conversion = orig_run

    for _ in range(200):
        app.drain_ui_queue()
        app.root.update()
        if not app._worker_busy:
            break
        time.sleep(0.02)

    assert app._worker_busy is False
    for cv in (app.canvas_orig, app.canvas_svg, app.canvas_icns):
        assert cv.find_withtag("busy_overlay") == ()
finally:
    app.root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_complex_curved_2d_gradients_focal_buttons_and_outer_glow() -> None:
    """Verify curved 2D gradients, off-center spherical buttons, intact dark rims, and smooth outer glow."""
    import cv2

    w, h = 260, 320
    scale = 2
    sw, sh = w * scale, h * scale
    yy, xx = np.mgrid[:sh, :sw].astype(np.float32)

    # Start on pure white background with soft cyan outer glow extending onto the right side
    canvas = np.full((sh, sw, 3), 255.0, dtype=np.float32)

    # Rotated elliptical grip + upper shoulder body union via smooth min
    ang = -0.34
    rx = (xx - 320.0) * np.cos(ang) + (yy - 390.0) * np.sin(ang)
    ry = -(xx - 320.0) * np.sin(ang) + (yy - 390.0) * np.cos(ang)
    d_grip = np.sqrt((rx / 122.0) ** 2 + (ry / 190.0) ** 2) - 1.0
    d_top = np.sqrt(((xx - 155.0) / 235.0) ** 2 + ((yy - 195.0) / 145.0) ** 2) - 1.0
    # Smooth union (smin)
    k_s = 0.25
    h_u = np.clip(0.5 + 0.5 * (d_grip - d_top) / k_s, 0.0, 1.0)
    sdf = d_top * h_u + d_grip * (1.0 - h_u) - k_s * h_u * (1.0 - h_u)

    # Outer soft cyan halo fading into pure white outside sdf > 0.13
    glow_t = np.clip(1.0 - np.maximum(sdf - 0.11, 0.0) / 0.28, 0.0, 1.0)
    glow_w = (glow_t ** 2.1) * 0.42
    cyan_glow = np.array([65.0, 240.0, 255.0], dtype=np.float32)
    canvas = canvas * (1.0 - glow_w[..., None]) + cyan_glow[None, None, :] * glow_w[..., None]

    # Outer cyan aura ring -> dark navy outline -> bevel rim -> 2D shaded body
    in_aura = sdf <= 0.13
    canvas[in_aura] = np.array([25.0, 238.0, 255.0], dtype=np.float32)

    in_dark = sdf <= 0.05
    canvas[in_dark] = np.array([10.0, 20.0, 56.0], dtype=np.float32)

    # U-shaped curved bevel rim with cyan -> lavender -> bright pink -> violet gradient
    in_rim = sdf <= -0.05
    u_rim = np.clip(yy / float(sh), 0.0, 1.0)
    x_rim = np.clip(xx / float(sw), 0.0, 1.0)
    rim_r = np.clip(70.0 + 175.0 * (u_rim ** 1.1) * (0.4 + 0.6 * x_rim), 0.0, 255.0)
    rim_g = np.clip(225.0 - 115.0 * u_rim + 45.0 * np.sin(u_rim * np.pi), 0.0, 255.0)
    rim_b = np.clip(255.0 - 15.0 * (u_rim ** 2), 0.0, 255.0)
    canvas[in_rim] = np.stack([rim_r, rim_g, rim_b], axis=2)[in_rim]

    # Non-convex controller face with rich 2D cyan-blue -> royal-blue -> indigo -> purple gradient
    in_face = sdf <= -0.175
    tx = np.clip(xx / float(sw), 0.0, 1.0)
    ty = np.clip(yy / float(sh), 0.0, 1.0)
    face_r = np.clip(18.0 + 108.0 * (ty ** 1.35) * (0.55 + 0.45 * tx), 0.0, 255.0)
    face_g = np.clip(165.0 - 125.0 * (ty ** 0.95) - 30.0 * tx, 0.0, 255.0)
    face_b = np.clip(242.0 - 45.0 * (ty * tx), 0.0, 255.0)
    canvas[in_face] = np.stack([face_r, face_g, face_b], axis=2)[in_face]

    # Three off-center 3D spherical face buttons surrounded by dark bezels
    buttons = [
        (180.0, 142.0, 33.0, (255.0, 232.0, 110.0), (242.0, 120.0, 32.0)),  # amber/orange button
        (258.0, 202.0, 33.0, (255.0, 196.0, 180.0), (235.0, 58.0, 82.0)),   # coral/red button
        (112.0, 202.0, 33.0, (165.0, 250.0, 255.0), (14.0, 126.0, 230.0)),  # cyan/blue button
    ]
    for cx, cy, rad, hi_c, lo_c in buttons:
        dist_b = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        bezel = dist_b <= (rad + 11.0)
        canvas[bezel] = np.array([10.0, 22.0, 58.0], dtype=np.float32)
        sphere = dist_b <= rad
        fx, fy = cx - 0.34 * rad, cy - 0.36 * rad
        dist_f = np.sqrt((xx - fx) ** 2 + (yy - fy) ** 2) / (1.35 * rad)
        t_s = np.clip(dist_f, 0.0, 1.0)
        for ch in range(3):
            col_s = hi_c[ch] * (1.0 - t_s) + lo_c[ch] * t_s
            canvas[..., ch] = np.where(sphere, col_s, canvas[..., ch])

    # Natural raster anti-aliasing at 2x then downsample to 1x artwork
    canvas_u8 = np.clip(np.round(canvas), 0, 255).astype(np.uint8)
    canvas_aa = cv2.GaussianBlur(canvas_u8, (3, 3), 0.75)
    im_2x = Image.fromarray(canvas_aa, mode="RGB").convert("RGBA")
    im_1x = im_2x.resize((w, h), Image.Resampling.BILINEAR)

    res = ic.run_conversion(
        im_1x,
        prepare_opts=ic.PrepareOptions(bg_mode="faithful", clean_jpeg_noise=True),
        svg_opts=ic.SVG_PRESETS["ultra"][1],
        write_svg=False,
        write_icns=False,
    )

    assert "<image" not in res.svg_text.lower()
    assert res.gradient_count >= 5
    assert res.psnr_db >= 30.5
    assert res.similarity_pct >= 98.5

    ren_1x = np.asarray(res.svg_rendered)[..., :3].astype(np.float32)
    src_1x = np.asarray(im_1x)[..., :3].astype(np.float32)

    # Dark outline adjacent to right bevel rim must remain unbroken and dark
    sdf_1x = cv2.resize(sdf, (w, h), interpolation=cv2.INTER_AREA)
    dark_ring_1x = (sdf_1x >= -0.03) & (sdf_1x <= 0.03) & (np.arange(w)[None, :] >= 90)
    assert int(dark_ring_1x.sum()) >= 50
    assert float(ren_1x[dark_ring_1x].mean()) < 65.0

    # Smooth outer glow fading into white must be preserved smoothly without spiky comb artifacts
    glow_zone_1x = (sdf_1x > 0.16) & (sdf_1x < 0.36) & (np.arange(w)[None, :] >= 150)
    assert int(glow_zone_1x.sum()) >= 80
    glow_rmse = float(np.sqrt(np.mean((ren_1x[glow_zone_1x] - src_1x[glow_zone_1x]) ** 2)))
    assert glow_rmse < 8.5

    # Native 200% vector render must stay crisp and accurate
    ren_200 = ic.render_svg_to_pil(res.svg_text, width=w * 2, height=h * 2)
    assert ren_200.size == (w * 2, h * 2)
    psnr_200, sim_200, _, _ = ic.compute_fidelity_metrics(im_2x, ren_200, res.svg_text)
    assert psnr_200 >= 28.5
    assert sim_200 >= 98.0


def test_square_crop_handles_keep_equal_sides_and_disregards_outside(tmp_path: Path) -> None:
    """Verify all 8 square crop handles lock 1:1 side lengths and pipeline discards everything outside."""
    # 1. Pure geometry invariance check across landscape, portrait, and square canvases
    for dims in ((320, 200), (190, 310), (240, 240)):
        w, h = dims
        box = ic.normalize_square_crop((20, 15, 190, 120), w, h, min_side=16)
        assert box[2] - box[0] == box[3] - box[1]
        for handle in ("nw", "n", "ne", "e", "se", "s", "sw", "w", "move"):
            for dx, dy in ((-140, 25), (150, -30), (18, -95), (-22, 110), (-500, -500), (500, 500)):
                nb = ic.compute_square_crop_drag(box, handle, dx, dy, w, h, min_side=16)
                nx0, ny0, nx1, ny1 = nb
                nw_s = nx1 - nx0
                nh_s = ny1 - ny0
                assert nw_s == nh_s, f"Non-square result {nb} for handle={handle} on {dims}"
                assert nw_s >= 16
                assert 0 <= nx0 < nx1 <= w
                assert 0 <= ny0 < ny1 <= h

    # 2. End-to-end conversion must keep only inside the square and disregard everything outside
    src = Image.new("RGBA", (260, 180), (255, 0, 220, 255))  # bright magenta outside crop
    draw = ImageDraw.Draw(src)
    # Draw clean interior background & green/gold emblem inside (70, 30, 170, 130)
    draw.rectangle([70, 30, 169, 129], fill=(242, 246, 252, 255))
    draw.rounded_rectangle([86, 46, 154, 114], radius=16, fill=(24, 155, 92, 255))
    draw.ellipse([104, 64, 136, 96], fill=(250, 204, 21, 255))

    opts = ic.PrepareOptions(
        bg_mode="transparent",
        clean_jpeg_noise=True,
        auto_trim=False,
        square_crop=(70, 30, 170, 130),
        picked_transparencies=(
            # Inside pick (green-tinted edge pixel? No, white corner of cropped square)
            ic.TransparencyPick(x_norm=75 / 259.0, y_norm=35 / 179.0, rgb=(242, 246, 252), tolerance=24.0),
            # Outside pick pointing at excluded magenta border must be ignored
            ic.TransparencyPick(x_norm=10 / 259.0, y_norm=10 / 179.0, rgb=(255, 0, 220), tolerance=24.0),
        ),
    )
    res = ic.run_conversion(src, prepare_opts=opts, svg_opts=ic.SVG_PRESETS["balanced"][1], write_svg=False, write_icns=False)
    assert res.prepared.image.size == (100, 100)
    assert res.prepared.crop_box == (70, 30, 170, 130)
    assert res.svg_rendered.size == (100, 100)
    assert "<image" not in res.svg_text.lower()

    prep_arr = np.asarray(res.prepared.image)
    ren_arr = np.asarray(res.svg_rendered)
    # Inside pick cleared corner inside crop to transparent; zero magenta leaked into prepared, SVG, or ICNS
    assert prep_arr[5, 5, 3] == 0
    for arr_chk in (prep_arr, ren_arr, np.asarray(res.icns_inspection.images_by_size[(256, 256, 1)])):
        vis = arr_chk[arr_chk[..., 3] > 20][..., :3]
        assert vis.size > 0
        magenta_dist = np.linalg.norm(vis.astype(np.float32) - np.array([255.0, 0.0, 220.0]), axis=1)
        assert float(magenta_dist.min()) > 80.0

    # 3. Verify CLI --crop-square support
    cli_src = tmp_path / "cli_crop_src.png"
    src.save(cli_src)
    cli_out = tmp_path / "cli_out"
    rc = ic.main(
        [
            "--cli",
            str(cli_src),
            "--out-dir",
            str(cli_out),
            "--name",
            "cropped",
            "--crop-square",
            "70,30,100",
            "--preset",
            "balanced",
            "--no-icns",
        ]
    )
    assert rc == 0
    cli_svg = (cli_out / "cropped.svg").read_text(encoding="utf-8")
    cli_ren = np.asarray(ic.render_svg_to_pil(cli_svg, width=100, height=100))
    cli_vis = cli_ren[cli_ren[..., 3] > 20][..., :3]
    assert cli_vis.size > 0
    cli_magenta_dist = np.linalg.norm(cli_vis.astype(np.float32) - np.array([255.0, 0.0, 220.0]), axis=1)
    assert float(cli_magenta_dist.min()) > 80.0


def test_gui_square_crop_interactive_handles_and_apply_reset(tmp_path: Path) -> None:
    """Verify GUI square crop handles render 1:1 on canvas, drag locking 1:1, Apply crops, and Reset restores."""
    src_path = tmp_path / "gui_crop_sample.png"
    src = Image.new("RGBA", (280, 190), (255, 0, 220, 255))
    draw = ImageDraw.Draw(src)
    draw.rectangle([50, 12, 229, 177], fill=(240, 248, 255, 255))
    draw.ellipse([95, 45, 185, 135], fill=(37, 99, 235, 255))
    draw.ellipse([120, 70, 160, 110], fill=(250, 204, 21, 255))
    src.save(src_path)

    script = f"""
import time
from pathlib import Path
import numpy as np
import icon_converter as ic

app = ic.IconConverterApp(initial_image=Path({str(src_path)!r}), themename="fluent_dark")
try:
    app.root.geometry("1120x740")
    app.root.update()
    for _ in range(200):
        app.drain_ui_queue()
        app.root.update()
        if app._last_result is not None and not app._worker_busy:
            break
        time.sleep(0.02)

    assert app._last_result is not None
    assert app._last_result.prepared.image.size == (280, 190)

    app.set_crop_mode(True)
    app.root.update()
    assert app._crop_mode_active is True

    # Toolbar & framing controls must remain visible without clipping at minimum window size 1120x740
    for w in (app.seg_zoom, app.btn_toolbar_picker, app.btn_toolbar_crop, app.btn_toolbar_apply_crop, app.lbl_canvas_hint):
        assert w.winfo_ismapped() == 1
        assert w.winfo_width() >= w.winfo_reqwidth() - 2

    def _assert_canvas_square_geometry():
        assert app.canvas_orig.find_withtag("crop_overlay") == ()
        fb = app.canvas_svg.coords("crop_frame")
        assert len(fb) == 4
        fw = round(fb[2] - fb[0])
        fh = round(fb[3] - fb[1])
        assert abs(fw - fh) <= 1, f"Canvas crop_frame not square: {{fb}}"
        for hname in ("nw", "n", "ne", "e", "se", "s", "sw", "w"):
            items = app.canvas_svg.find_withtag(f"crop_handle_{{hname}}")
            assert len(items) == 1
            hb = app.canvas_svg.coords(items[0])
            assert round(hb[2] - hb[0]) == round(hb[3] - hb[1]), f"Handle {{hname}} not square: {{hb}}"

    _assert_canvas_square_geometry()

    def _drag_handle(hname: str, dx_px: int, dy_px: int):
        hb = app.canvas_svg.coords(f"crop_handle_{{hname}}")
        hx = int(round(0.5 * (hb[0] + hb[2])))
        hy = int(round(0.5 * (hb[1] + hb[3])))
        app.canvas_svg.event_generate("<ButtonPress-1>", x=hx, y=hy)
        app.canvas_svg.event_generate("<B1-Motion>", x=hx + dx_px, y=hy + dy_px)
        app.canvas_svg.event_generate("<ButtonRelease-1>", x=hx + dx_px, y=hy + dy_px)
        app.root.update()
        box = app._draft_square_crop
        assert box is not None
        assert (box[2] - box[0]) == (box[3] - box[1]), f"Draft box non-square after {{hname}}: {{box}}"
        _assert_canvas_square_geometry()

    # Asymmetric drags across corner, edge, and interior move handles must always lock 1:1 square
    _drag_handle("se", -35, -8)
    _drag_handle("nw", 12, 30)
    _drag_handle("e", -14, 42)
    _drag_handle("n", 36, 10)
    # Move center handle slightly and verify final box stays inside non-magenta interior
    fb0 = app.canvas_svg.coords("crop_frame")
    mx = int(round(0.5 * (fb0[0] + fb0[2])))
    my = int(round(0.5 * (fb0[1] + fb0[3])))
    app.canvas_svg.event_generate("<ButtonPress-1>", x=mx, y=my)
    app.canvas_svg.event_generate("<B1-Motion>", x=mx + 10, y=my - 6)
    app.canvas_svg.event_generate("<ButtonRelease-1>", x=mx + 10, y=my - 6)
    app.root.update()
    box = app._draft_square_crop
    assert box is not None and (box[2] - box[0]) == (box[3] - box[1])

    # Explicitly verify cropped square strictly excludes outside magenta zone
    chosen = app.set_draft_square_crop((65, 22, 195, 152))
    assert (chosen[2] - chosen[0]) == (chosen[3] - chosen[1]) == 130
    _assert_canvas_square_geometry()

    applied = app.apply_square_crop()
    assert applied == (65, 22, 195, 152)
    for _ in range(200):
        app.drain_ui_queue()
        app.root.update()
        if not app._worker_busy and app._last_result is not None and app._last_result.prepared.image.size == (130, 130):
            break
        time.sleep(0.02)

    assert app._last_result is not None
    assert app._last_result.prepared.image.size == (130, 130)
    assert app._last_result.svg_rendered.size == (130, 130)
    assert app._canvas_img_geom[id(app.canvas_orig)][3:5] == (280, 190)
    assert app._canvas_img_geom[id(app.canvas_svg)][3:5] == (130, 130)
    for chk_im in (app._last_result.prepared.image, app._last_result.svg_rendered):
        arr = np.asarray(chk_im)
        vis = arr[arr[..., 3] > 20][..., :3]
        mag_dist = np.linalg.norm(vis.astype(np.float32) - np.array([255.0, 0.0, 220.0]), axis=1)
        assert float(mag_dist.min()) > 80.0

    # Reset restores the original full non-square dimensions
    app.reset_square_crop()
    for _ in range(200):
        app.drain_ui_queue()
        app.root.update()
        if not app._worker_busy and app._last_result is not None and app._last_result.prepared.image.size == (280, 190):
            break
        time.sleep(0.02)

    assert app._applied_square_crop is None
    assert app._last_result is not None
    assert app._last_result.prepared.image.size == (280, 190)
finally:
    app.root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_busy_timer_spins_continuously_without_35pct_gil_stall(tmp_path: Path) -> None:
    """Verify native worker offloading prevents 35% GIL freezes and keeps 30 FPS clock & progress moving."""
    import threading

    assert ic._NATIVE_SVG_WORKER.prewarm() is True
    try:
        # Detailed 360x360 gradient + multi-element illustration so vtracer exercises real trace time
        w, h = 360, 360
        yy, xx = np.mgrid[:h, :w].astype(np.float32)
        bg = np.stack(
            [
                np.clip(28.0 + 180.0 * (yy / float(h)), 0, 255),
                np.clip(190.0 - 110.0 * (xx / float(w)), 0, 255),
                np.clip(245.0 - 60.0 * ((xx + yy) / float(w + h)), 0, 255),
            ],
            axis=2,
        ).astype(np.uint8)
        im = Image.fromarray(bg, mode="RGB").convert("RGBA")
        draw = ImageDraw.Draw(im)
        for idx in range(18):
            ang = idx * (2.0 * np.pi / 18.0)
            cx = int(round(180 + 110 * np.cos(ang)))
            cy = int(round(180 + 110 * np.sin(ang)))
            r = 20 + (idx % 3) * 6
            col = ((35 * idx) % 255, (200 - 11 * idx) % 255, (80 + 19 * idx) % 255, 255)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col, outline=(12, 20, 48, 255), width=3)
        for ring in range(4):
            pad = 85 + ring * 18
            draw.rounded_rectangle([pad, pad, w - pad, h - pad], radius=18, outline=(255, 230, 120, 255), width=3)

        events: list[tuple[float, str]] = []
        done_flag = threading.Event()
        err_holder: list[BaseException] = []
        res_holder: list[ic.ConversionResult] = []

        def _worker() -> None:
            try:
                res = ic.run_conversion(
                    im,
                    svg_opts=ic.SVG_PRESETS["ultra"][1],
                    write_svg=False,
                    write_icns=False,
                    progress_cb=lambda pct, msg: events.append((float(pct), msg)),
                )
                res_holder.append(res)
            except BaseException as exc:  # pragma: no cover
                err_holder.append(exc)
            finally:
                done_flag.set()

        th = threading.Thread(target=_worker, daemon=True)
        last_t = time.perf_counter()
        max_gap = 0.0
        th.start()
        while not done_flag.is_set():
            time.sleep(0.01)
            now = time.perf_counter()
            max_gap = max(max_gap, now - last_t)
            last_t = now
        th.join(timeout=15.0)
        assert not err_holder
        assert res_holder and "<image" not in res_holder[0].svg_text.lower()
        assert max_gap < 0.15, f"Main-thread GIL stall detected: {max_gap:.3f}s"
        assert len(events) >= 6
        pcts = [p for p, _m in events]
        assert pcts == sorted(pcts)
        assert pcts[-1] == 100.0
    finally:
        ic._NATIVE_SVG_WORKER.shutdown()

    src_path = tmp_path / "gil_free_sample.png"
    im.save(src_path)

    script = f"""
import time
from pathlib import Path
import icon_converter as ic

app = ic.IconConverterApp(initial_image=Path({str(src_path)!r}), themename="fluent_dark")
try:
    for _ in range(300):
        app.drain_ui_queue()
        app.root.update()
        if app._last_result is not None and not app._worker_busy:
            break
        time.sleep(0.02)
    assert app._last_result is not None

    samples = []
    app.refresh_preview()
    for _ in range(400):
        app.drain_ui_queue()
        app.root.update()
        samples.append((time.perf_counter(), app._busy_frame_idx, app._busy_pct, app._worker_busy))
        if not app._worker_busy:
            break
        time.sleep(0.02)

    assert app._worker_busy is False
    busy_samples = [s for s in samples if s[3]]
    assert len(busy_samples) >= 8, f"Expected >= 8 busy animation samples, got {{len(busy_samples)}}"
    mid_frames = [s[1] for s in busy_samples if 30.0 <= s[2] <= 66.0]
    mid_pcts = [s[2] for s in busy_samples if 30.0 <= s[2] <= 66.0]
    assert len(set(mid_frames)) >= 3, f"Clock froze in 30..66% window: {{mid_frames}}"
    assert max(mid_pcts) > min(mid_pcts) + 1.0, f"Progress froze in 30..66% window: {{mid_pcts}}"
finally:
    app.root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_five_level_undo_button_and_shortcut(tmp_path: Path) -> None:
    """Verify 5-level Undo buttons, Ctrl+Z shortcut, coalesced slider edits, and cached restore."""
    src_path = tmp_path / "undo_sample.png"
    src = Image.new("RGBA", (260, 190), (242, 246, 252, 255))
    draw = ImageDraw.Draw(src)
    draw.rounded_rectangle([40, 20, 159, 139], radius=18, fill=(28, 128, 220, 255))
    draw.ellipse([70, 50, 130, 110], fill=(250, 204, 21, 255))
    src.save(src_path)

    script = f"""
import time
from pathlib import Path
import icon_converter as ic

app = ic.IconConverterApp(initial_image=Path({str(src_path)!r}), themename="fluent_dark")
try:
    app.root.geometry("1120x740")
    app.root.update()

    def _wait_idle():
        for _ in range(250):
            app.drain_ui_queue()
            app.root.update()
            if app._last_result is not None and not app._worker_busy:
                return
            time.sleep(0.02)
        raise AssertionError("Timed out waiting for background worker.")

    _wait_idle()
    assert app._last_result is not None
    assert app.undo_depth() == 0
    assert app.can_undo() is False
    assert app.btn_undo._state == "disabled"
    assert app.btn_header_undo._state == "disabled"
    assert app.btn_undo._text == "Undo"
    assert app.btn_header_undo._text == "Undo"

    for btn in (app.btn_undo, app.btn_header_undo):
        assert btn.winfo_ismapped() == 1
        assert btn.winfo_width() >= btn.winfo_reqwidth() - 2
        assert btn.winfo_x() + btn.winfo_width() <= btn.master.winfo_width() + 2

    # S1: Apply 1:1 square crop (40, 20, 160, 140)
    app.set_crop_mode(True)
    app.set_draft_square_crop((40, 20, 160, 140))
    app.apply_square_crop()
    _wait_idle()
    assert app.undo_depth() == 1
    assert app._applied_square_crop == (40, 20, 160, 140)
    assert app._last_result.prepared.image.size == (120, 120)

    # S2: Add a transparency pick inside the cropped square
    app.add_transparency_pick_at_source(42, 22, trigger_preview=True)
    _wait_idle()
    assert app.undo_depth() == 2
    assert len(app._picked_transparencies) == 1

    # S3: Switch SVG preset to Compact
    compact_lbl = ic.SVG_PRESETS["compact"][0]
    app.combo_preset.set(compact_lbl, notify=True)
    assert app.undo_depth() == 3
    app.refresh_preview()
    _wait_idle()
    assert app.combo_preset.get() == compact_lbl

    # S4: Enable Auto-Trim Outer Margins
    app._var_auto_trim.set(True)
    app.refresh_preview()
    _wait_idle()
    assert app.undo_depth() == 4
    assert bool(app._var_auto_trim.get()) is True

    # S5: Rapid burst of slider moves must coalesce into a single undo level (depth -> 5)
    for val in (12, 16, 20, 22):
        app.slider_layer_diff.set(val, notify=False)
        app._schedule_preview()
    assert app.undo_depth() == 5
    app.refresh_preview()
    _wait_idle()
    assert app.undo_depth() == 5
    assert int(round(app.slider_layer_diff.get())) == 22

    # S6: 6th edit caps stack at MAX_UNDO_LEVELS = 5 (evicts oldest S0)
    app.seg_curve.set("Polygon", notify=False)
    app.refresh_preview()
    _wait_idle()
    assert app.undo_depth() == 5 == ic.MAX_UNDO_LEVELS
    assert app.seg_curve.get() == "Polygon"
    assert app.btn_undo._text == "Undo (5)"
    assert app.btn_header_undo._text == "Undo (5)"
    app.root.update()
    for btn in (app.btn_undo, app.btn_header_undo):
        assert btn.winfo_width() >= btn.winfo_reqwidth() - 2
        assert btn.winfo_x() + btn.winfo_width() <= btn.master.winfo_width() + 2

    # Undo 1 -> S5 (curve back to compact/spline or S5 curve, layer_diff=22)
    assert app.undo() is True
    _wait_idle()
    assert int(round(app.slider_layer_diff.get())) == 22
    assert bool(app._var_auto_trim.get()) is True
    assert app.undo_depth() == 4

    # Undo 2 via Ctrl+Z shortcut -> S4 (auto_trim=True, layer_diff back to Compact default)
    app.btn_undo.focus_set()
    app.root.update()
    app.btn_undo.event_generate("<Control-z>")
    _wait_idle()
    compact_ld = ic.SVG_PRESETS["compact"][1].layer_difference
    assert int(round(app.slider_layer_diff.get())) == compact_ld
    assert bool(app._var_auto_trim.get()) is True
    assert app.undo_depth() == 3

    # Undo 3 -> S3 (auto_trim=False, Compact preset still active)
    assert app.undo() is True
    _wait_idle()
    assert bool(app._var_auto_trim.get()) is False
    assert app.combo_preset.get() == compact_lbl
    assert app.undo_depth() == 2

    # Undo 4 -> S2 (preset back to Ultra, 1 transparency pick still active)
    assert app.undo() is True
    _wait_idle()
    assert app.combo_preset.get() == ic.SVG_PRESETS["ultra"][0]
    assert len(app._picked_transparencies) == 1
    assert app._applied_square_crop == (40, 20, 160, 140)
    assert app.undo_depth() == 1

    # Undo 5 -> S1 (0 transparency picks, square crop (40, 20, 160, 140) preserved, cached instant restore)
    assert app.undo() is True
    assert len(app._picked_transparencies) == 0
    assert app._applied_square_crop == (40, 20, 160, 140)
    assert app._last_result.prepared.image.size == (120, 120)
    assert app.undo_depth() == 0
    assert app.can_undo() is False
    assert app.btn_undo._state == "disabled"
    assert app.btn_header_undo._state == "disabled"

    # Undo 6 -> False (5-level cap; S0 was evicted so S1 stays intact)
    assert app.undo() is False
    assert app._applied_square_crop == (40, 20, 160, 140)
    assert app._last_result.prepared.image.size == (120, 120)
finally:
    app.root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_left_panel_stays_original_across_all_edits(tmp_path: Path) -> None:
    """Verify the left Original Picture panel never changes or gets overlays/timer across all edits."""
    src_path = tmp_path / "orig_immutable_sample.png"
    src = Image.new("RGBA", (240, 180), (244, 248, 255, 255))
    draw = ImageDraw.Draw(src)
    draw.rounded_rectangle([36, 24, 156, 144], radius=20, fill=(24, 132, 224, 255))
    draw.ellipse([66, 54, 126, 114], fill=(250, 204, 21, 255))
    src.save(src_path)

    script = f"""
import time
from pathlib import Path
from PIL import ImageTk
import icon_converter as ic

app = ic.IconConverterApp(initial_image=Path({str(src_path)!r}), themename="fluent_dark")
try:
    app.root.update()

    def _wait_idle_checking_left():
        for _ in range(250):
            app.drain_ui_queue()
            app.root.update()
            assert app.canvas_orig.find_withtag("busy_overlay") == ()
            if not app._worker_busy and app._last_result is not None:
                return
            time.sleep(0.02)
        raise AssertionError("Timed out waiting for background worker.")

    def _sample_photo_rgb(photo):
        im_p = ImageTk.getimage(photo)
        pw, ph = im_p.size
        pts = [(2, 2), (pw - 3, 2), (2, ph - 3), (pw - 3, ph - 3), (pw // 2, ph // 2)]
        return pw, ph, [im_p.getpixel(pt) for pt in pts]

    def _assert_left_unchanged(expected_sig):
        assert app.canvas_orig.find_withtag("busy_overlay") == ()
        assert app.canvas_orig.find_withtag("crop_overlay") == ()
        assert app._canvas_img_geom[id(app.canvas_orig)][3:5] == (240, 180)
        assert _sample_photo_rgb(app._tk_refs[0]) == expected_sig

    _wait_idle_checking_left()
    orig_sig = _sample_photo_rgb(app._tk_refs[0])
    assert app._canvas_img_geom[id(app.canvas_orig)][3:5] == (240, 180)

    # (a) Transparency pick (right panel updates background to checkerboard, left stays white original)
    app.add_transparency_pick_at_source(4, 4, trigger_preview=True)
    _wait_idle_checking_left()
    _assert_left_unchanged(orig_sig)
    assert _sample_photo_rgb(app._tk_refs[1]) != orig_sig

    # (b) Squircle badge mode (right panel renders 1024x1024 badge, left stays 240x180 original)
    app.combo_bg.set(ic.BG_MODE_LABELS["squircle"], notify=False)
    app.refresh_preview()
    _wait_idle_checking_left()
    _assert_left_unchanged(orig_sig)
    assert app._canvas_img_geom[id(app.canvas_svg)][3:5] == (1024, 1024)

    # (c) Auto-trim outer margins in faithful mode (right panel tightens, left keeps 240x180)
    app.combo_bg.set(ic.BG_MODE_LABELS["faithful"], notify=False)
    app._var_auto_trim.set(True)
    app.refresh_preview()
    _wait_idle_checking_left()
    _assert_left_unchanged(orig_sig)
    assert app._canvas_img_geom[id(app.canvas_svg)][3:5] != (240, 180)

    # (d) 1:1 Square crop (overlays and cropped size happen on right panel only)
    app._var_auto_trim.set(False)
    app.set_crop_mode(True)
    app.set_draft_square_crop((30, 20, 150, 140))
    _assert_left_unchanged(orig_sig)
    assert len(app.canvas_svg.find_withtag("crop_frame")) == 1
    app.apply_square_crop()
    _wait_idle_checking_left()
    _assert_left_unchanged(orig_sig)
    assert app._canvas_img_geom[id(app.canvas_svg)][3:5] == (120, 120)
finally:
    app.root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_app_brand_icons_and_svg_loaded() -> None:
    """Verify `icons/Icon_generator.icns` and `.svg` power window icons, header logo, and welcome badges."""
    icns_p, svg_p, ico_p = ic.resolve_app_brand_paths()
    assert icns_p is not None and icns_p.is_file()
    assert svg_p is not None and svg_p.is_file()

    master_im, by_px, resolved_ico = ic.load_app_brand_bundle()
    assert master_im is not None
    assert master_im.size == (1024, 1024)
    for sz in (16, 32, 48, 64, 128, 256, 512, 1024):
        assert sz in by_px
        assert by_px[sz].size == (sz, sz)
    assert resolved_ico is not None and resolved_ico.is_file()
    assert resolved_ico == ico_p
    assert ic._ico_has_descending_256_first(resolved_ico) is True

    b42 = ic.render_brand_squircle_badge(42)
    b112 = ic.render_brand_squircle_badge(112)
    assert b42 is not None and b42.size == (42, 42)
    assert b112 is not None and b112.size == (112, 112)

    loaded_from_icns = ic.load_source_image(icns_p)
    assert loaded_from_icns.size == (1024, 1024)

    script = """
import icon_converter as ic

app = ic.IconConverterApp(themename="fluent_dark")
try:
    app.root.update()
    assert app.root.state() == "zoomed"
    assert app._var_write_svg.get() is True
    assert app._var_write_icns.get() is True
    assert app._var_write_ico.get() is True
    app.root.state("normal")
    app.root.geometry("1120x740")
    app.root.update()
    assert len(app._app_icon_photos) == 6
    assert hasattr(app, "_win32_hicons") and all(app._win32_hicons)
    assert app._header_logo_photo is not None
    assert len(app.canvas_app_logo.find_withtag("header_logo")) == 1
    assert len(app.canvas_orig.find_withtag("brand_welcome")) >= 2
    assert len(app.canvas_svg.find_withtag("brand_welcome")) >= 2
    assert len(app.canvas_icns.find_withtag("brand_welcome")) >= 1
    assert hasattr(app, "btn_load_sample")
    assert app.btn_load_sample.winfo_ismapped() == 1
    assert app.btn_load_sample.winfo_width() >= app.btn_load_sample.winfo_reqwidth() - 2
finally:
    app.root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_modernttk_activity_timer_and_uninterrupted_busy_overlay() -> None:
    """Verify ModernTtk exports ActivityTimer, BusyOverlay, SubprocessWorker, and TaskRunner for uninterrupted 30 FPS timers."""
    import modernttk as mtk

    assert mtk.SPINNER_FRAME_COUNT == 36
    assert mtk.ActivityTimer is mtk.ModernActivityTimer
    assert mtk.BusyOverlay is mtk.ModernBusyOverlay
    assert mtk.SubprocessWorker is mtk.ModernSubprocessWorker
    assert mtk.TaskRunner is mtk.ModernTaskRunner

    frames = mtk.build_timer_dial_frames(dark=True)
    assert len(frames) == 36
    assert frames[0].size == (168, 146)
    badge = mtk.render_timer_overlay_badge(frames[0], elapsed_sec=1.5, pct=42.0, dark=True)
    assert badge.size == (168, 188)

    script = """
import time
import tkinter as tk
import modernttk as mtk

root = mtk.ModernWindow(title="Timer Test", themename="fluent_dark", geometry="640x420")
try:
    timer = mtk.ActivityTimer(root, bootstyle="primary", autostart=True)
    timer.pack(side="left", padx=8, pady=8)

    cv_orig = tk.Canvas(root, width=200, height=200)
    cv_orig.pack(side="left")
    cv_edit = tk.Canvas(root, width=200, height=200)
    cv_edit.pack(side="left")
    pb = mtk.ProgressBar(root, width=180)
    pb.pack(side="bottom")

    overlay = mtk.BusyOverlay(root, canvases=(cv_edit,), progress_bar=pb, bootstyle="primary")
    runner = mtk.TaskRunner(root, overlay=overlay, progress_bar=pb)

    done_val = []
    runner.start(
        lambda progress_cb: (
            progress_cb(30.0, "Stage 1"),
            time.sleep(0.14),
            progress_cb(75.0, "Stage 2"),
            time.sleep(0.10),
            "ok",
        )[-1],
        on_done=lambda res: done_val.append(res),
    )

    seen_frames = set()
    seen_pcts = []
    for _ in range(40):
        root.update()
        if overlay.is_active:
            seen_frames.add(overlay.frame_index)
            seen_pcts.append(overlay.pct)
            assert cv_orig.find_withtag("busy_overlay") == ()
            assert len(cv_edit.find_withtag("busy_overlay")) == 1
        if done_val:
            break
        time.sleep(0.015)

    assert done_val == ["ok"]
    assert len(seen_frames) >= 4
    assert seen_pcts == sorted(seen_pcts)
    assert cv_edit.find_withtag("busy_overlay") == ()
    assert timer.is_running is True
    f0 = timer.frame_index
    timer.step_frame()
    assert timer.frame_index == (f0 + 1) % mtk.SPINNER_FRAME_COUNT
    timer.stop()
    assert timer.is_running is False
finally:
    root.destroy()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_version_1_0_0_ensure_icons_dir_and_github_inplace_updater(tmp_path: Path) -> None:
    """Verify v1.0.0 versioning, first-run `icons` folder creation, GitHub release check, and Capacity-style in-place install."""
    import hashlib
    import json
    from unittest.mock import MagicMock, patch

    # 1. Version 1.0.1 & --version CLI flag
    assert ic.__version__ == "1.0.1"
    ver_proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "icon_converter.py"), "--version"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert ver_proc.returncode == 0
    assert "Iconverter v1.0.1" in ver_proc.stdout

    # 2. First-run `icons` folder creation & brand asset seeding
    fresh_install_dir = tmp_path / "fresh_app_dir"
    assert not (fresh_install_dir / "icons").exists()
    created_icons = ic.ensure_icons_dir(fresh_install_dir)
    assert created_icons == (fresh_install_dir / "icons").resolve()
    assert created_icons.is_dir()
    assert (created_icons / "Icon_generator.icns").is_file()
    assert (created_icons / "Icon_generator.svg").is_file()

    # 3. Semantic version parsing & comparison
    assert ic.parse_version("1.0.0") == (1, 0, 0)
    assert ic.parse_version("v1.2.1") == (1, 2, 1)
    assert ic.is_newer_version("1.0.0", "1.0.1") is True
    assert ic.is_newer_version("1.0.0", "v1.1.0") is True
    assert ic.is_newer_version("1.0.0", "1.0.0") is False
    assert ic.is_newer_version("1.2.0", "1.0.9") is False

    # 4. Asset prioritization & SHA-256 extraction
    fake_payload = b"ICONVERTER_RELEASE_BINARY_V110"
    expected_sha = hashlib.sha256(fake_payload).hexdigest()
    assets = [
        {"name": "notes.txt", "browser_download_url": "https://github.com/x/y/notes.txt", "size": 40},
        {
            "name": "Iconverter_1.1.0.exe",
            "browser_download_url": "https://github.com/asteng88/Iconverter/releases/download/v1.1.0/Iconverter_1.1.0.exe",
            "url": "https://api.github.com/repos/asteng88/Iconverter/releases/assets/123",
            "size": len(fake_payload),
            "digest": f"sha256:{expected_sha}",
        },
    ]
    a_name, a_url, a_api_url, a_size = ic.find_executable_asset(assets)
    assert a_name == "Iconverter_1.1.0.exe"
    assert a_size == len(fake_payload)
    assert ic.find_asset_sha256(assets, a_name) == expected_sha

    # 5. check_for_updates with mock GitHub Release JSON
    mock_release_json = {
        "tag_name": "v1.1.0",
        "name": "Iconverter v1.1.0",
        "body": "### What's New\n- Improved vector gradients\n- Faster ICNS export",
        "html_url": "https://github.com/asteng88/Iconverter/releases/tag/v1.1.0",
        "assets": assets,
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_release_json).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        rel_info = ic.check_for_updates(current_version="1.0.0")
    assert rel_info is not None
    assert rel_info.version == "1.1.0"
    assert rel_info.is_newer is True
    assert rel_info.asset_name == "Iconverter_1.1.0.exe"
    assert rel_info.asset_sha256 == expected_sha

    # 6. Stream download + SHA-256 verification
    dl_dest = tmp_path / "update_1.1.0.exe"

    class _FakeDownloadStream:
        def __init__(self, data: bytes) -> None:
            self._bio = io.BytesIO(data)
            self.headers = {"Content-Length": str(len(data))}

        def read(self, n: int = -1) -> bytes:
            return self._bio.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    prog_steps: list[tuple[int, int]] = []
    with patch("urllib.request.urlopen", return_value=_FakeDownloadStream(fake_payload)):
        ok = ic.download_update_asset(
            rel_info.asset_url,
            dl_dest,
            progress_callback=lambda d, t: prog_steps.append((d, t)),
            expected_size=len(fake_payload),
            expected_sha256=expected_sha,
        )
    assert ok is True
    assert dl_dest.is_file()
    assert dl_dest.read_bytes() == fake_payload
    assert prog_steps[-1] == (len(fake_payload), len(fake_payload))

    # 7. Capacity Planner in-place executable replacement + VBS old-backup cleanup
    assert ic.replace_executable_in_place(dl_dest) is False  # Guard in non-frozen dev mode
    orig_exe = tmp_path / "Iconverter.exe"
    orig_exe.write_bytes(b"OLD_V100_BINARY")
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(orig_exe)):
        replaced = ic.replace_executable_in_place(dl_dest)
        assert replaced is True
        assert orig_exe.read_bytes() == fake_payload
        assert not dl_dest.exists()
        old_bak = orig_exe.with_suffix(".exe.old")
        assert old_bak.is_file()
        assert old_bak.read_bytes() == b"OLD_V100_BINARY"

        with patch("subprocess.Popen") as mock_popen:
            sched = ic.schedule_delete_old_backup(old_bak)
            assert sched is True
            mock_popen.assert_called_once()
            cmd = mock_popen.call_args[0][0]
            assert cmd[0] == "wscript.exe"
            vbs_path = Path(cmd[1])
            assert vbs_path.is_file()
            vbs_path.unlink(missing_ok=True)

        ic.cleanup_old_executable_backup()
        assert not old_bak.exists()

