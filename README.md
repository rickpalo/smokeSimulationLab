# BatchSimLab

A Blender 4.x/5.x add-on for **batch fluid-simulation parameter sweeping**.

BatchSimLab automates the tedious work of testing many smoke/fire simulation
settings. Define parameter ranges in a panel, click **Export Batch**, and a
Windows batch file runs every combination — baking the Mantaflow simulation,
rendering a playblast animation and a final still, and logging results to CSV for
side-by-side comparison.

**Roadmap:** smoke today (v0.x → v1.0.0), smoke + fire at v2.0.0, smoke + fire +
liquid at v3.0.0 — hence "BatchSim".

> The repository and source folder are `BatchSimLab` (renamed from `SmokeSimLab`
> at v0.9.3). Some lowercase runtime identifiers (`smoke_settings`, `SMOKE_*`,
> `.smokesettings`) keep the legacy prefix for backwards compatibility with
> existing `.blend` saves and keymaps.

![BatchSimLab panel](documentation/images/SmokeSimLab_Panel.png)

---

## Features

- **Batch parameter sweeping** — Resolution, Vorticity, Buoyancy (density/heat),
  Dissolve, Noise (upres/strength/scale), **gas timing** (time scale + adaptive
  timesteps), **fire** (burning rate, flame smoke/vorticity/temp/ignition), and
  **per-emitter flow settings** (temperature, density, surface/volume emission,
  initial velocity).
- **Two iteration modes** — *Limited* (vary one parameter at a time) or *All*
  (full Cartesian product), plus *Iterate Both On/Off* for Dissolve and Noise.
- **Per-job outputs** — MP4 playblast, PNG final still, and a row in
  `results.csv` (23 columns incl. bake time + addon version).
- **In-render text overlays** — burn the current parameter values into each
  render via scene FONT objects.
- **Cycles GPU** (OptiX → CUDA → HIP) headless, or **EEVEE** windowed.
- **Crash-safe launcher**, per-job logs, live **Job Log** with progress bars and
  phase-aware ETAs, **Monitor Existing Jobs**, **Auto Retry**, **Use Existing
  Cache** / **Placeholders**, and `.smokesettings` presets.

---

## Requirements

- **Blender 4.2+** (tested on 4.5.x LTS and 5.1.1)
- **Windows** (the launcher is a `.bat`)
- NVIDIA GPU with OptiX recommended (not required)

---

## Installation (extension feed)

1. **Edit → Preferences → Get Extensions → Repositories (▾) → ＋ Add Remote
   Repository** and paste:
   ```
   https://rickpalo.github.io/BatchSimLab/index.json
   ```
2. Enable it, then install **BatchSimLab** from **Get Extensions**.
3. The **BatchLab** tab appears in the 3D Viewport N-panel (press **N**).

Updates are delivered automatically through the feed.

---

## Quick start

1. Set up a Mantaflow fluid domain. 2. Open the **BatchLab** tab. 3. Set
**Domain Object** and **Output** folder. 4. Configure defaults + ranges to sweep.
5. **Save the `.blend`**. 6. Click **Export Batch**. 7. Double-click
`run_smoke_batch.bat` (or **Run Batch**) and watch the **Job Log**.

---

## Full documentation

See **[DOCUMENTATION.md](DOCUMENTATION.md)** for the complete reference: every
parameter (incl. emitters/fire/timing), iteration modes, the two-phase pipeline,
render settings, text overlays, output structure, the full `results.csv` schema,
caching/resume/placeholders, estimates, troubleshooting, and limitations.

---

## Contributing

Bug reports and pull requests are welcome on the
[GitHub repository](https://github.com/rickpalo/BatchSimLab).

## License

GPL-2.0-or-later. See [LICENSE](LICENSE) for details.
