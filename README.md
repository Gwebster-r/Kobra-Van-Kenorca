<p align="center">
  <img src="assets/kobra_orca_logo.png" alt="Kobra-Van-Kenorca Logo" width="420" />
</p>

# Kobra-Van-Kenorca (`⚕ Kobra`)

**Vanilla / Tunneled Klipper & ACE Pro Bridge for OrcaSlicer**

> **Compatibility Notice:** This plugin **only works with Vanilla Klipper or Tunneled Vanilla Klipper** (via Moonraker) and is **not compatible with stock Anycubic firmware / Rinkhals** unless running a Vanilla/Tunneled Klipper setup. Designed for **Kobra S1, Kobra S1 Max, Kobra 3, and Kobra 3 Max** ACE Pro configs (Kobra X / Kobra 2 Pro are not supported). So far, it has **only been tested on an Anycubic Kobra S1**.

Kobra-Van-Kenorca (OrcaSlicer tab: **`⚕ Kobra`**) integrates supported Anycubic Kobra printers running **Vanilla Klipper** (or **Tunneled Vanilla Klipper**) and **ACE Pro** (1–4 units, slots `T0`–`T15`) into **OrcaSlicer 2.5.0+**. Built exclusively on the Python standard library, it runs inside OrcaSlicer's embedded CPython runtime with zero external dependencies.

---

## Features

- **Hybrid Print-Intercept & Auto-Sync Workflow:** Works both via the blocking **`WINDOW_MODAL` Print-Intercept** when clicking **Print** in OrcaSlicer's Preview screen (enabled via **Process → Others → Slicing Pipeline Plugin** as `KobraVanKenorca_PostProcess`, supporting normal Klipper/Moonraker printer profiles) and directly via the **`⚕ Kobra` tab** with automatic post-slice capture (`.gcode` / `.gcode.3mf`), live Moonraker status refresh on tab activation, and automatic invalidation when models, plates, presets, or filaments change.
- **Map · Check · Send Pre-Print Dialog:** Map Slicer extruders to physical ACE Pro slots (`T0`–`T15`, 1–4 ACE units), persist material-mismatch acknowledgments across live polls, preserve compatible manual slot overrides across re-slices, reuse a single slot across multiple extruders, preview mapped colors live on the embedded thumbnail, or export patched files directly via `Export .gcode...`.
- **Multi-Printer Support:** Manage multiple printer profiles (Moonraker URL, API key, optional Spoolman URL, ACE unit count) with a quick switcher in the header and target indicator in Pre-Print.
- **Reversible G-Code Post-Processing:**
  - Computes `ACE_SET_PURGE_AMOUNT PURGELENGTH=<mm>` from `; flush_multiplier`, `; flush_volumes_matrix`, `; flush_volumes_vector`, and `; filament_diameter` using the original Slicer tool indices before remapping `T<n>` and `G9111 tool=<n>`.
  - Pre-print toggles for **Skip Purge** (`SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1`, K3/K3M `G9111`; KS1/KS1M `G9111` handles smart skip natively) and **Unload after print** (`PRINT_END CUT_TIP=1` or fallback `CUT_TIP`), plus an explicit Send-Lock notice whenever `Upload & Start Print` is blocked (e.g., while printing, offline, or on mapping errors).
- **Flush & Audit Dashboard:** Inspect the flush vector, N×N flush heatmap, and every toolchange event.
- **Pre-Print Filament Remaining Prediction:** Estimates print + purge usage per mapped slot against Spoolman remaining weight and warns on low safety reserve or insufficient filament (informational, non-blocking).
- **Spoolman Integration:** Resolves numeric spool IDs and manufacturer SKUs/article numbers, handles multiple spools per SKU via `last_used` ranking and manual per-slot dropdown selection, and displays spool fill levels.
- **ACE Pro Live Control & Multi-Unit Dryer:** Per-slot **Load**, **Unload**, **Full Unload** (with mandatory presence/tangle-warning confirmation), and **Assist ON / Assist OFF** (`ACE_ENABLE_FEED_ASSIST` / `ACE_DISABLE_FEED_ASSIST`) toggle, live **`SHIFTING`** / hardware-state badges, and ACE Dryer start/stop (presets up to `60 °C`, manual `20–70 °C` within your ACE `max_dryer_temperature`) with per-unit `ACE Temp` and `Humidity` telemetry across all connected ACE units (1–4 units).
- **Safety & Customization:** Locks hardware actions during active prints (with an instant **Unlock / Lock** override in the top status bar for emergency ACE intervention), supports English & German, **Auto / Dark / Light / KobraOrca** signature theme modes, 8 accent color schemes, automatic one-time migration from `KobraOrca` / `vKobraACE` / `kobra_s1_ace_plugin` configs, and anonymized diagnostics export.

---

## Requirements

- **OrcaSlicer:** `2.5.0` or newer (Python plugin API enabled).
- **Printer & Firmware:** Anycubic Kobra S1 / KS1M / K3 / K3M with Vanilla Klipper (or Tunneled Vanilla Klipper), Moonraker, and ACE Pro driver/macros.
- **Required Klipper Macros & Moonraker Objects:**
  - Moonraker objects: `ace` / `ace_state` / `ace_instance_0..3` / `save_variables`, `print_stats`.
  - Klipper macros: `ACE_SET_PURGE_AMOUNT`, `ACE_CHANGE_TOOL`, `ACE_SMART_UNLOAD`, `ACE_FULL_UNLOAD`, `ACE_ENABLE_FEED_ASSIST`, `ACE_DISABLE_FEED_ASSIST`, `ACE_START_DRYING`, `ACE_STOP_DRYING`, `G9111`, `PRINT_END` (supporting `CUT_TIP`), `CUT_TIP`.
- **Optional:** Spoolman server (`/api/v1/spool` v1 REST API).

---

## Installation

Copy `kobra_van_kenorca_plugin.py` into your OrcaSlicer plugins directory, then restart OrcaSlicer:

- **Linux (Flatpak):**
  `~/.var/app/io.github.softfever3d.OrcaSlicer/config/OrcaSlicer/orca_plugins/`
- **Linux (AppImage & Native):**
  `~/.config/OrcaSlicer/orca_plugins/`
  *(Supports custom `TMPDIR`, `TMP`, and `TEMP` paths for AppImage preview G-code capture and uses `orca.host.plugin.storage()` without hardcoded Flatpak assumptions.)*
- **Windows:**
  `%APPDATA%\OrcaSlicer\orca_plugins\`
- **macOS:**
  `~/Library/Application Support/OrcaSlicer/orca_plugins/`

*(Upgrading from `KobraOrca`, `vKobraACE`, or `kobra_s1_ace_plugin`? Remove the old `.py` file if present; your saved settings, printer profiles, and slicing pipeline entry migrate automatically into `Kobra-Van-Kenorca`.)*

### Uninstallation

Remove `kobra_van_kenorca_plugin.py` (and optionally its plugin storage folder inside OrcaSlicer's `orca_plugins` data directory) and restart OrcaSlicer.

---

## Permissions & Privacy

On startup (and when configuring URLs in **Settings → Connection**), OrcaSlicer prompts for:

- **`fs_read`:** Narrow read-only access to OrcaSlicer's temporary model directory (`orcaslicer_model/*`) for automatic post-slice G-code capture.
- **`network_http`:** Access strictly to your configured Moonraker and (optional) Spoolman URLs.
- **`threading`:** Background worker threads so HTTP requests and G-code analysis never freeze the UI.

Diagnostic summaries copied from **Settings → Diagnostics** automatically redact private LAN IPv4 addresses, local home directory usernames, URL credentials, and API keys.

---

## Offline Usage

G-code header/flush analysis, local `.gcode` / `.gcode.3mf` loading (*Browse .gcode*), color mapping, and local patching (*Apply & Patch Locally*) work completely offline without Moonraker or Spoolman.

Standalone CLI usage is also supported:

```bash
python3 kobra_van_kenorca_plugin.py <gcode_file> --tool-map 0:0,1:1
```

## OrcaSlicer 2.5.0 Permissions

- **Filesystem (`fs_read` / `fs_write`):** On first use, OrcaSlicer may ask once to allow read-only access (`fs_read`) to OrcaSlicer's temporary `orcaslicer_model/*` preview G-code folder and static state-file writes (`state.json` / `state.json.tmp` inside plugin storage or the `Kobra-Van-Kenorca` user backup folder).
- **Network (`network_http`):** When configuring Moonraker or Spoolman, approve the base host entry (e.g. `http://<printer-host>`) once so all Moonraker/Spoolman endpoints work without per-endpoint prompts.
