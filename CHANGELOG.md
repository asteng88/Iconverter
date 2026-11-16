# Changelog

All notable changes to **Iconverter (Icon & Vector Studio)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] - 2026-11-16

### Added
- **Faithful Vector `.SVG` Reconstruction Engine**:
  - High-precision color spline tracing powered by `vtracer` (`2×` supersampled coordinate tracing with exact `viewBox` normalization).
  - Automatic detection and reconstruction of true SVG `<linearGradient>`, off-center focal `<radialGradient>` (`fx`/`fy`), and curved 2D smooth-shaded `<image>` overlays clipped to exact vector region contours (`<clipPath>`).
  - Subpixel anti-aliasing deblurring and morphological stroke-coherence line cleanup to eliminate outline noise and staircase banding at `200%` and `400%` zoom.
  - Native resolution SVG rasterization via `resvg_py` so zooming to `100%`, `200%`, or `400%` re-renders crisp vector paths directly from the SVG DOM.
- **Complete 12-Slot Apple `.ICNS` Encoder & Inspector**:
  - Encodes all 12 standard macOS icon slots (`is32`/`s8mk`, `icp4`, `ic11`, `il32`/`l8mk`, `icp5`, `ih32`/`h8mk`, `ic12`, `icp6`, `ic07`, `ic13`, `ic08`, `ic14`, `ic09`, `ic10`) plus a binary `TOC ` table of contents header.
  - Exact PackBits `RLE24` encoder with `3 * W * H` decoded-length verification and automatic safe uncompressed fallback so `PIL.IcnsImagePlugin` decodes every legacy slot without corruption.
  - Aspect-ratio-preserving canvas centering for non-square artwork and optional small-icon crispness boost (`<= 128 px`).
  - Live **ICNS Multi-Size Inspector** tab previewing all decoded slots across both dark macOS Dock and light Finder surfaces.
- **Interactive Desktop Studio (`ModernTtk` UI)**:
  - Side-by-side **Original Picture** (left, always preserved untouched) vs **Rendered Vector .SVG** (right, live edited preview) comparison canvases with synchronized pan and zoom.
  - **1:1 Square Crop Tool**: Interactive square crop overlay on the right preview canvas with 8 resize handles (`NW`, `N`, `NE`, `E`, `SE`, `S`, `SW`, `W`) that strictly lock equal side lengths (`1:1`) and discard everything outside the square.
  - **Smart Transparency Matte & Colour Picker**: Flood-fill border + interior pocket matte extraction, CIE Lab chroma-difference deepening (`0–100`), and interactive click-to-sample transparency colour chips.
  - **macOS Squircle Badge Framing**: Superellipse (`|x|^4.6 + |y|^4.6 <= 1`) icon framing with soft ambient drop shadow and specular rim highlight.
  - **5-Level Undo (`Ctrl+Z`)**: Instant undo stack across both header and footer action bars with cached preview restoration.
  - **10 Built-In Themes**: `fluent_dark`, `fluent_light`, `cosmo`, `darkly`, `cyborg`, `minty`, `dracula`, `rccp_blue`, `rccp_teal`, and `rccp_fuchsia`.
- **Uninterrupted 30 FPS Animated Chronograph Timer (`ModernTtk`)**:
  - 36-frame subpixel-anti-aliased frosted glassmorphic chronograph medallion + 2× supersampled live elapsed/percentage pill (`"1.2s • 65%"`) rendered exclusively over active output canvases.
  - Persistent GIL-free `--native-svg-worker` subprocess client (`ModernSubprocessWorker`) + stage-ceiling progress gliding so heavy Rust/C calls (`vtracer`, `resvg_py`) never stall the 30 FPS UI animation loop.
- **First-Run `icons/` Initialization & In-Place GitHub Auto-Updater**:
  - Automatically builds and seeds the `icons/` directory next to the executable on first run.
  - Checks GitHub Releases (`asteng88/Iconverter`) on startup and via the header **Updates** button, verifies `Content-Length` and `SHA-256` digests over HTTPS, and performs NTFS in-place `.exe` replacement (`Iconverter.exe` -> `Iconverter.exe.old`) with detached VBS cleanup.
- **Headless CLI**:
  - Full command-line interface (`--cli`, `--out-dir`, `--name`, `--bg-mode`, `--preset`, `--crop-square`, `--pick`, `--deepen-strength`, `--ico`, `--version`) for batch scripting and CI pipelines.
