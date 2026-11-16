# Contributing to Iconverter

Thank you for your interest in contributing to **Iconverter (Icon & Vector Studio)**!

---

## Development Setup

### Prerequisites
- **Python**: `3.11+` (`3.14` recommended)
- **[uv](https://docs.astral.sh/uv/)**: Fast Python package and environment manager

### Clone & Install Dependencies

```powershell
git clone https://github.com/asteng88/Iconverter.git
cd Iconverter
uv sync
```

This installs all runtime and development dependencies (`numpy`, `opencv-python-headless`, `pillow`, `vtracer`, `resvg-py`, `pytest`, `pyinstaller`, and the local editable `ModernTtk` UI toolkit in `./ModernTtk`).

---

## Running the Application

```powershell
# Launch the interactive GUI studio
uv run python icon_converter.py

# Run headless CLI conversion
uv run python icon_converter.py --cli path/to/image.png --out-dir icons --name sample_icon
```

---

## Running the Test Suite

All changes must pass the automated `pytest` suite before submitting a pull request:

```powershell
uv run pytest -v
```

### What the Test Suite Verifies
1. **Vector Fidelity & Gradients**: Linear, off-center focal radial, and curved 2D mesh gradient reconstruction (`PSNR >= 30.5 dB` and `>= 36 dB` on smooth gradients).
2. **Line Cleanup & 400% Native Vector Zoom**: Subpixel anti-aliasing deblurring, stroke-coherent outline cleanup, and native-resolution `resvg_py` re-rendering at `400%` zoom.
3. **12-Slot Apple `.ICNS` Round-Trip**: Exact `3 * W * H` PackBits `RLE24` length guard, `s8mk`/`l8mk`/`h8mk` 8-bit masks, `TOC ` table of contents, and verification of all 12 decoded icon slots.
4. **Interactive Studio Controls**:
   - 1:1 Square Crop handle locking (`NW`, `N`, `NE`, `E`, `SE`, `S`, `SW`, `W`) and strict exclusion of pixels outside the square.
   - Left `"Original Picture"` panel immutability across all right-panel edits.
   - 5-level Undo stack (`btn_undo`, `btn_header_undo`, `Ctrl+Z`).
   - Smart Transparency Colour Picker and CIE Lab chroma deepening.
5. **Uninterrupted 30 FPS Chronograph Timer**: Verifies that `ModernSubprocessWorker` (`--native-svg-worker`) keeps the main Tkinter thread GIL-free so the 36-frame chronograph timer spins continuously (`max_dt < 0.25s`, zero 35% GIL stalls).
6. **First-Run `icons/` Initialization & In-Place Updater**: Verifies `ensure_icons_dir()`, semantic version comparison, SHA-256 verification, and NTFS in-place `.exe` replacement.

---

## Building the Standalone Executable

```powershell
uv run pyinstaller --clean --noconfirm --onefile --windowed --name Iconverter ^
    --icon icons/Icon_generator.ico --add-data "icons;icons" ^
    --paths ModernTtk --collect-all modernttk --collect-all vtracer --collect-all resvg_py icon_converter.py
```

The compiled executable is written to `dist/Iconverter.exe`.

---

## Pull Request Guidelines

1. Keep the left **Original Picture** canvas strictly read-only—all visual edits, overlays, and busy timers belong exclusively on the right **Rendered Vector .SVG** and **ICNS Multi-Size Inspector** canvases.
2. Any long-running native C/Rust call (`vtracer`, `resvg_py`) invoked during GUI preview/export must be routed through `_NATIVE_SVG_WORKER` (`ModernSubprocessWorker`) so the Tkinter `after()` loop never stalls.
3. Add or update unit/GUI tests in `tests/test_icon_converter.py` for any new feature or bug fix.
