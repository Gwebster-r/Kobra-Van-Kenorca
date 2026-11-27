# Changelog

## v1.3.0 — UI Polish, ACE Slot Control Upgrades & Hardware-Unlock

### Changed
- **Tab Title (`⚕ Kobra`):** Updated `PLUGIN_TAB_NAME` from `KoVaCa` / `Kobra` to `⚕ Kobra` (inline symbol instead of an external SVG tab icon) and updated sliced-ready notifications accordingly.
- **Streamlined Pre-Print Toggles:** Removed unused `Bed Leveling (G31)` and `Force Initial Purge` toggles (Vanilla Klipper handles bed mesh leveling natively inside `G9111`, and `G9111` always loads the initial tool before layer 1). Clarified the `Skip Purge` tooltip (`K3/K3M only — KS1/KS1M G9111 always uses Smart Skip`).
- **Compact Top Status Bar:**
  - Shows multi-unit ACE count directly in the connection badge (e.g. `Connected (2xACE2)`), hides the redundant `Online (printing)` value, and moves full device model + Moonraker URL into the hover tooltip.
  - Removed redundant `Active Toolhead` and `ACE Pro Dryer` header tiles.
  - Moved the **No G-code** notice (`No valid sliced G-code → Please slice the plate in Preview or`) and **Print Safety** banner into compact inline status tiles inside the top bar.

### Added
- **Slot Bay Upgrades (`Assist ON/OFF`, `Full Unload`, `SHIFTING`):**
  - Replaced `+Feed` / `-Retr` with a live **Feed Assist** toggle (`Assist ON` / `Assist OFF`, calling `ACE_ENABLE_FEED_ASSIST` / `ACE_DISABLE_FEED_ASSIST`) styled by active state per unit.
  - Added **`Full Unload`** (`ACE_FULL_UNLOAD TOOL=<n>`) with a mandatory confirmation modal warning the user to stay at the printer so the spool does not unwind and tangle.
  - Preserved and displayed live hardware states such as **`SHIFTING`**, **`connecting`**, and **`disconnected`** in the header & slot cards.
- **Klipper ACE Pro Power Toggle (`⏻`) & External Spool Mode with Spoolman Auto-Tracking:**
  - Added a direct power toggle pill next to the connection badge (`SET_PIN PIN=ACE_Pro VALUE=1|0` via Moonraker, print-safety guarded unless unlocked) with automatic follow-up live status refreshes (`0.8s` / `1.5s`).
  - Automatically replaces `Connected` with **`ACE Pro OFF (External Spool)`**, **`ACE Connecting…`**, or **`ACE Disconnected`** when ACE is disabled or offline, and grays out/locks the Slot Bay and Dryer bar.
  - Inside **`Map · Check · Send`**, External Spool mode hides ACE slot dropdowns/buttons and lets you pick either **`External Spool (No Tracking)`** (works completely without Spoolman) or any active **Spoolman** spool with real remaining-weight prediction, calling Moonraker `POST /server/spoolman/spool_id` on Upload & Start Print and injecting reversible `SET_ACTIVE_SPOOL ID=<n>` lines after `G9111` / `T<n>` so `spoolman_logic.cfg` does not overwrite your selected external spool.
- **Hardware Lock Instant Unlock (`Unlock` / `Lock`):** Changed the active-print safety tile to orange (`Active Print — Hardware locked`) with an instant **Unlock** button (turning red: `Hardware unlocked — Active printing, be careful!`) so ACE slot controls (and the ACE Power toggle) can be operated mid-print when troubleshooting. Fixed an immediate-refresh bug where `renderSlotBay()` threw a `ReferenceError` instead of calling `renderSlots()`.
- **Per-Unit `ACE Temp` & Multi-Dryer Telemetry in Dryer Bar:** Added `unit_temps` normalization (`ACE Temp: ACE 1 29°C · ACE 2 33°C`) and per-unit active dryer status (`Dryer: ACE 1 47°C (239.4 min) · ACE 2 48°C (240 min)`) when multiple ACE units run at different temperatures or durations.

### Fixed
- **External Spool Printing (`output_pin ACE_Pro = 0` / Out-of-Range Slicer Tools such as `T4`):**
  - Respected Klipper's `ace_pro_enabled` / `output_pin ACE_Pro` state across post-processing, `can_upload_gcode()`, and the UI (`Map · Check · Send` & top status bar) so printing from an external spool with ACE Pro disabled no longer fails on `Slicer extruder T4` slot bounds, empty ACE slots, material mismatch checks, or missing multi-color flush vectors, and bypasses ACE tool remapping / `ACE_SET_PURGE_AMOUNT` injection.
  - Added a live `output_pin ACE_Pro` probe on `SlotBoundsError` during slicing/export and deferred unresolved interactive slot-bounds errors (`host != "File"`) directly into the `Map · Check · Send` modal instead of aborting OrcaSlicer with an error dialog, alongside a manual **External Spool (Bypass ACE)** toggle.
- **Direct Preview-to-Print False `superseded by newer slice` Block:** Cleared `_pending_preview_gcode` and marked the slice run as satisfied immediately when `KobraS1AcePostProcessCapability.execute()` stages a `slicing_pipeline` job, prevented delayed lifecycle/tab-focus syncs inside the standalone `WINDOW_MODAL` from re-staging an older hidden preview temp file (`.2.x.gcode`) as `slicer_explicit`, and stopped older preview files from falsely superseding the active print job at the Upload Safety Gate.
- **`Reload G-Code from Slicer` After Manual `Browse .gcode` / `Unload Stale G-code`:** Preserved `_last_preview_temp_path` when loading or unloading a manually browsed `file_picker` file and skipped `file_picker` candidates during explicit `force_refresh` reloads so clicking **Reload G-Code from Slicer** reliably restores the active OrcaSlicer Preview slice.
- **Export `.gcode` Destination Picker & Sandbox Fallback:** Added an interactive Export destination dialog (`#exportPathModal`, defaulting to remembered `Default G-Code Folder` or `~/Downloads` with one-click `Downloads` / `Plugin Folder` presets) when `window.showSaveFilePicker` is unavailable (Linux WebKitGTK / macOS WKWebView), with automatic fallback to `<storage>/exported_gcode/` on `PermissionError` and full saved file path reporting.
- **Filament Usage Info `(i)` Tooltip Overlap:** Removed the duplicate native browser `title` attribute from `#mappingUsageInfoBadge` so only the styled **Filament Consumption Breakdown** popover renders on hover/focus.

## v1.2.0 — `Kobra-Van-Kenorca` (`KoVaCa`) Rebrand, Toolchange Count & G9111 Wording Fixes

### Changed
- **Rebrand to `Kobra-Van-Kenorca` (Tab: `KoVaCa`):** Renamed main plugin file from `kobra_orca_plugin.py` to `kobra_van_kenorca_plugin.py`, updated PEP 723 metadata (`name = "Kobra-Van-Kenorca"`), OrcaSlicer main tab title to `KoVaCa`, Slicing Pipeline capability name to `KobraVanKenorca_PostProcess`, and extended automatic settings/pipeline migration to cover `KobraOrca`, `vKobraACE`, and `kobra_s1_ace_plugin` while preserving the official embedded logo and `KobraOrca` theme.
- **Accurate Toolchange Counter (`0` on Skipped Initial Load):** When `Skip Purge` (`SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1`) is active and the mapped start tool is already loaded in ACE Pro, the skipped initial load is no longer counted as a toolchange (single-color prints now show `0 Toolchanges`).
- **Clearer `G9111` Initial-Purge Wording:** Updated EN/DE status text from *"G9111 skipped"* / *"G9111 übersprungen"* to *"G9111 initial purge skipped (tool already loaded)"* / *"G9111-Initial-Purge übersprungen (Tool bereits geladen)"*.
- **German UI Polish:** Renamed `"Unload nach Druck"` to `"Entladen nach Druck"`.

## v1.1.0 — KobraOrca Rebrand, Official Logo & Signature Theme

### Changed
- **Plugin Rebrand (`vKobraACE` → `KobraOrca`):** Renamed the main plugin module from `vkobra_ace_plugin.py` to `kobra_orca_plugin.py`, updated PEP 723 metadata (`name = "KobraOrca"`), OrcaSlicer tab title (`KobraOrca`), Settings title, notifications, CLI usage/log prefixes, and Slicing Pipeline capability name (`KobraOrca ACE Pro Post-Processor`).
- **Backwards-Compatible Migration:** Automatically migrates existing `vKobraACE` and `kobra_s1_ace_plugin` `state.json` configs into `KobraOrca/state.json`, reads legacy `vkobra_ace_settings` capability configs into `kobra_orca_settings`, and upgrades `Anycubic Kobra S1 ACE Pro Post-Processor` pipeline entries to `KobraOrca ACE Pro Post-Processor`.

### Added
- **Official KobraOrca Logo Assets & Embedded Data-URI:** Added high-resolution repository logo assets (`assets/kobra_orca_logo.png`, `assets/kobra_orca_icon.png`) and embedded `KOBRA_ORCA_LOGO_DATA_URI` directly into the WebView header (replacing the old `vKA` badge), browser favicon, and `Map · Check · Send` Pre-Print modal with zero external dependencies.
- **Signature `KobraOrca` Theme Mode:** Added `KobraOrca` alongside `Auto`, `Dark`, and `Light` in **Settings → Appearance**, styled after the official logo's midnight-blue (`#0b1116` / `#0e151b`), slate-petrol (`#131d24` / `#18242d`, border `#233542`), Ice-Cyan (`#18b6c9` / `#22d3ee`), and Cobra-Gold (`#e5b54f` / `#f0c96b`) palette.

## v1.0.0 — Initial Public Test Release

### Added
- **OrcaSlicer 2.5.0 Integration:** Single-file stdlib-only plugin (originally `vKobraACE`, now `KobraOrca`) registering Page UI (`KobraS1AcePageCapability`) and Slicing Pipeline (`KobraS1AcePostProcessCapability` under **Process → Others → Slicing Pipeline Plugin**).
- **Hybrid `WINDOW_MODAL` Print-Intercept & Live Tab Sync:** Intercepts Preview **Print** sends via a blocking `WINDOW_MODAL` Pre-Print dialog (re-patching `ctx.gcode_path` on local apply or aborting duplicate native send when uploaded/cancelled inside the plugin), alongside automatic post-slice `.gcode` / `.gcode.3mf` capture, tab-activation auto-sync + Moonraker live refresh, host-fingerprint invalidation across model/plate/preset/filament edits, and persistent material-mismatch acknowledgment.
- **G-Code Post-Processing:** Computes `ACE_SET_PURGE_AMOUNT PURGELENGTH=<mm>` from OrcaSlicer flush comments before remapping `T<n>` and `G9111 tool=<n>`, with reversible options for `G31`, `SKIP_PURGE_FOR_ALREADY_LOADED_TOOL`, `PRINT_END CUT_TIP=1`, and initial purge.
- **Map · Check · Send Modal & Live Thumbnail Recoloring:** Visual Slicer-to-ACE slot mapping cards, shared-slot support, live `<canvas>` thumbnail recoloring + zoom lightbox, explicit Send-Lock notices, standalone `Export .gcode...`, and Flush & Audit heatmap/timeline modal.
- **Pre-Print Filament Remaining Prediction:** Per-slot remaining filament forecast with filament-colored consumption bars (high-contrast border for light spools) combining Slicer print usage, `ACE_SET_PURGE_AMOUNT` purge usage (without double counting), and Spoolman remaining weight with configurable safety reserve.
- **ACE Pro (1–4 Units), Multi-Printer & Spoolman Support:** Dynamic slot bay (`T0`–`T15`), multi-printer profile switching, RFID SKU/ID matching, multi-spool `last_used` resolution + manual per-slot picker, per-slot Load/Unload/Feed/Retract, and Dryer controls (presets up to `60 °C`, manual `20–70 °C`) with humidity telemetry for all connected ACE units.
- **Settings, Themes & Diagnostics:** Unified 5-tab Settings modal, English/German UI, Auto/Dark/Light modes, 8 accent color schemes, anonymized diagnostics summary, and configurable `diag_load_logging` toggle (default `False`).
- **Cross-Platform Path Support:** Linux/Flatpak, Windows (`%APPDATA%` / `%TEMP%`), and macOS (`~/Library/Application Support` / `/var/folders`).
