# ModernTtk

**ModernTtk** is a hybrid Python Tkinter & `ttk` UI library that blends the clean **Windows 11 Fluent Design** surfaces of `sv_ttk` with the **semantic color roles and rich component suite** of `ttkbootstrap`—powered by 4× supersampled Pillow (`PIL`) rendering for crisp, anti-aliased rounded corners, procedural vector icons, and smooth animations.

## Highlights

- **10 Built-In Themes + OS Auto-Detection**: `fluent_dark`, `fluent_light`, `cosmo`, `darkly`, `cyborg`, `minty`, `dracula`, `rccp_blue`, `rccp_teal`, `rccp_fuchsia`, plus `mtk.set_theme("system")` and custom JSON/dict palettes.
- **Zero-Refactor Legacy App Theming**: Call `mtk.auto_theme_tree(root)` to modernize an existing `tk`/`ttk` widget tree automatically.
- **28 Procedural Vector Icons**: Render theme-colored vector icons in memory (`mtk.icon("search")`, `icon="download"`, etc.) with zero external image files.
- **High-DPI Animated Chronograph Timer & Uninterrupted Execution Engine**:
  - `ActivityTimer` (`ModernActivityTimer`) and `BusyOverlay` (`ModernBusyOverlay`) render a 36-frame subpixel-anti-aliased frosted glassmorphic chronograph medallion with wake-illuminated dial ticks, a tapered conical comet sweep, a rotating stopwatch hand, and a 2× supersampled live elapsed/percentage pill (`"1.2s • 65%"`) at an uninterrupted 30 FPS (`33 ms`).
  - `SubprocessWorker` (`ModernSubprocessWorker`) and `TaskRunner` (`ModernTaskRunner`) offload GIL-holding native C/Rust extensions to a persistent child process over binary stdio IPC while gliding progress smoothly toward each stage ceiling—preventing mid-task UI/timer freezes (such as the 35% GIL stall).
- **30+ Supersampled Custom Widgets**:
  - **Navigation & Overlays**: `Sidebar` (collapsible rail), `Notebook` (`underline`, `pills`, `cards`), `SegmentedButton`, `CommandPalette` (`Ctrl+K`), `ContextMenu` / `Menu`
  - **Inputs & Validation**: `Entry`, `SearchBar`, `PasswordEntry`, `Combobox`, `Spinbox`, `TagInput` / `Chips`, `Textarea` / `Text` (with line numbers & colored logs), and `.set_validation()` states
  - **Gauges, Timers, Data & Enterprise**: `ActivityTimer` / `BusyOverlay`, `Meter` (radial arc gauge), `Calendar` & `DatePicker`, `DataTable` / `Treeview`, `Accordion` / `Expander`, `MetricCard`, `StatusBar`, `ScrollableFrame`, `ProgressDialog`, `Splash`, `Toast`, `Dialog`, `Badge`, `Tooltip`

## Quick Start

```python
import modernttk as mtk

app = mtk.ModernWindow(title="My Modern App", themename="system", geometry="720x480")

card = mtk.Card(app, title="Quick Settings", subtitle="Configure workspace preferences")
card.pack(fill="both", expand=True, padx=24, pady=24)

mtk.Switch(card.body, text="Enable Dark Mode", bootstyle="primary", command=mtk.toggle_theme).pack(anchor="w", pady=6)
mtk.TagInput(card.body, tags=["production", "us-east-1"], bootstyle="primary").pack(anchor="w", pady=8)

mtk.Button(
    card.body,
    text="Save Configuration",
    icon="download",
    bootstyle="primary-pill",
    command=lambda: mtk.Toast.show(app, "Saved!", "Your preferences were updated.", bootstyle="success"),
).pack(anchor="w", pady=12)

app.mainloop()
```

## Running the Interactive 5-Page Showcase

```bash
python demo.py
```

## Full Documentation

See **[`instructions.md`](instructions.md)** for the complete developer guide, widget API reference, uninterrupted animated timer & subprocess worker guide, enterprise/analytics adapters (`Matplotlib`, `tksheet`), migration cheatsheets from `sv_ttk` and `ttkbootstrap`, and GitHub/PyPI release instructions.
