# Iconverter (Icon & Vector Studio)

Standalone desktop & CLI application that converts pictures into faithful scalable `.svg` files (with true `<linearGradient>` / `<radialGradient>` / multi-axis gradient reconstruction and smart transparency controls) and complete 12-slot Apple `.icns` icon bundles built with [ModernTtk](./ModernTtk).

## Setup

Create and populate the isolated virtual environment using `uv`:

```powershell
uv sync
```

## Run Directly from `.py`

Launch the `ModernTtk` GUI studio:

```powershell
.\.venv\Scripts\python.exe icon_converter.py
```

Or via `uv` / the launcher batch script:

```powershell
uv run python icon_converter.py
# or:
Iconverter.bat
```

## CLI Usage

Convert an image cleanly from the command line:

```powershell
uv run python icon_converter.py --cli icons\porter_source.jpg --out-dir icons --name porter
```

Transparent background with color-difference deepening, interactive transparency seed picks, and `.ico` export:

```powershell
uv run python icon_converter.py --cli icons\porter_source.jpg --out-dir icons --name porter --bg-mode transparent --deepen-strength 60 --pick 300,852 --ico
```

## Run Tests

```powershell
uv run pytest -v
```

## Build Standalone Windows `.exe` with PyInstaller

```powershell
uv run pyinstaller --noconfirm --onefile --windowed --name Iconverter ^
    --icon icons/Icon_generator.ico --add-data "icons;icons" ^
    --paths ModernTtk --collect-all vtracer --collect-all resvg_py icon_converter.py
```
