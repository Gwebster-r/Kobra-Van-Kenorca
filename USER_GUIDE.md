# Kobra-Van-Kenorca (`⚕ Kobra`) — User Guide

> **Compatibility:** Works with **Vanilla Klipper** or **Tunneled Vanilla Klipper** (via Moonraker) on **Anycubic Kobra S1, Kobra S1 Max, Kobra 3, and Kobra 3 Max** with **ACE Pro** (1–4 units, slots `T0`–`T15`). Stock Anycubic firmware / Rinkhals without Vanilla/Tunneled Klipper, as well as Kobra X and Kobra 2 Pro, are not supported.

---

## 1. Quick Start (First Setup)

1. **Install & enable** `Kobra-Van-Kenorca` in **OrcaSlicer 2.5.0+**. A new **`⚕ Kobra`** tab appears in OrcaSlicer's top navigation bar.
2. Open **`⚕ Kobra` → Settings → Connection**:
   - Enter your printer's **Moonraker URL** (for example `http://192.0.2.10:7125` or `http://printer.local`, Port 80 fallback is enabled by default).
   - *(Optional)* Enter your **Spoolman URL** (for example `http://192.0.2.10:7912`) if you want remaining-filament checks and automatic external-spool tracking.
   - Click **Connect & Load Status**.
3. **Slice & Print:**
   - Slice your plate in OrcaSlicer's **Preview** tab as usual.
   - Click **Print** — the **`Map · Check · Send`** window opens automatically so you can verify your slot mapping, material compatibility, purge volume, and filament reserves before the file is sent to Moonraker.

---

## 2. Top Status Bar & Hardware Controls

- **Connection Badge:** Shows live ACE Pro status, e.g. `Connected (2xACE2)`, `ACE Pro OFF (External Spool)`, `ACE Connecting…`, or `ACE Disconnected`.
- **ACE Power Switch (`⏻`):** Toggles Klipper's `output_pin ACE_Pro` (`SET_PIN PIN=ACE_Pro VALUE=1|0`) directly from OrcaSlicer. Green = ACE Pro ON; gray = ACE Pro OFF (External Spool).
- **Printer Badge (`Printer: <state>`):** Displays Klipper's live `print_stats.state` (`standby`, `printing`, `paused`, `complete`, `cancelled`, `error`).
- **Active Print Hardware Lock (`Unlock` / `Lock`):** While a print is running, slot and power actions are locked (orange tile) to prevent accidental mid-print moves. Click **Unlock** (turns red) only if you intentionally need to operate an ACE slot during troubleshooting.

---

## 3. ACE Pro Slot Bay & Dryer Bar

Each slot card (`T0`–`T15`) displays live RFID/material/color info, optional Spoolman remaining weight, and action buttons:
- **Load / Unload:** Runs `ACE_CHANGE_TOOL TOOL=<n>` / `ACE_SMART_UNLOAD TOOL=<n>`.
- **Full Unload:** Runs `ACE_FULL_UNLOAD TOOL=<n>` after a confirmation dialog. **Stay at the printer** to hold and wind the filament back onto the spool so it does not unwind and tangle.
- **Auto-Feed:** Toggles ACE Pro feed assist (`ACE_ENABLE_FEED_ASSIST` / `ACE_DISABLE_FEED_ASSIST`). Green = active, neutral = off.
- **Dryer Bar:** Start/stop drying per unit or across all ACE units with presets (`PLA 45 °C`, `TPU 50 °C`, `PETG 60 °C`) or custom temperatures up to your ACE's `max_dryer_temperature`, showing live per-unit `ACE Temp` and `Humidity`.

---

## 4. `Map · Check · Send` Workflow

When you click **Print** in Preview (or **Prepare & Send** in the `⚕ Kobra` tab):
- **Slot Mapping:** Assign each active Slicer extruder to any physical ACE slot (`T0`–`T15`). You can reuse one physical slot for multiple extruders, use **Auto-Match** (by material & color), or reset to **1:1 Default**. The embedded 3D thumbnail updates live to preview your mapped colors.
- **Material Mismatch Guard:** If a mapped ACE slot contains a different material than sliced (e.g. PLA sliced vs. PETG loaded), the card highlights the mismatch and requires explicit checkbox confirmation before sending.
- **Spoolman Remaining Filament Prediction:** Combines model filament usage + calculated `ACE_SET_PURGE_AMOUNT` flush/purge volume + startup purge estimate against each mapped spool's remaining weight and warns you if a spool will run out or drop below your safety reserve.
- **Pre-Print Toggles:**
  - **Skip Purge:** Sets `SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1` on `G9111` (for K3/K3M; KS1/KS1M `G9111` always uses Smart Skip natively).
  - **Unload after print:** Sets `PRINT_END CUT_TIP=1` so filament is cut and unloaded automatically when the print finishes.
  - **Cancel:** Safely aborts the OrcaSlicer upload without starting a print.

---

## 5. Endless Spool & Backup Prediction

- **Live Sync:** When `Map · Check · Send` opens, the **Endless Spool** checkbox and mode buttons (**Exact (Material + Color)**, **Material**, **Next Ready**) are pre-filled from Klipper's `save_variables` (`ace_endless_spool_enabled`, `ace_endless_spool_match_mode`).
- **Safe Testing Before Sending:** Toggling Endless Spool or switching modes inside `Map · Check · Send` updates the backup badges live **without changing Klipper yet**. Settings are sent to Klipper (`ACE_SET_ENDLESS_SPOOL_MODE` + `ACE_ENABLE_ENDLESS_SPOOL` / `ACE_DISABLE_ENDLESS_SPOOL`) only when you click **Upload Only** or **Upload & Start Print**.
- **Backup Badges & Multi-Spool Chains:**
  - Whenever a matching backup exists, a green **`↻ Backup: T<n>`** badge appears on the mapping card (using `ace_state.endless_spool_next_tool` from ACEPRO when available for the currently loaded live tool, and mirroring ACEPRO's `EndlessSpool` rules for all other slots and modes).
  - If a spool does not have enough filament to finish the print, the plugin checks whether the backup chain (`T2 → T3`) covers the deficit and tells you clearly if it is enough, still short, or if no matching backup (`↻ No Backup`) is ready.

---

## 6. External Spool Mode (ACE Pro OFF)

- Turn ACE Pro off using the **`⏻`** button in the header (or in Mainsail/Fluidd via `output_pin ACE_Pro = 0`).
- The plugin automatically switches to **External Spool** mode:
  - ACE slot bounds (e.g. Slicer `T4` on a 1-ACE setup), empty-slot checks, material checks, ACE tool remapping, and `ACE_SET_PURGE_AMOUNT` are bypassed.
  - Inside `Map · Check · Send`, you can choose **External Spool (No Tracking)** (works completely without Spoolman) or select any active **Spoolman** spool — on Upload & Start Print, the plugin activates that spool in Moonraker (`POST /server/spoolman/spool_id`) and injects reversible `SET_ACTIVE_SPOOL ID=<n>` commands so `spoolman_logic.cfg` tracks your external spool automatically.

---

## 7. FAQ & Troubleshooting

- **`Map · Check · Send` does not open when clicking Print (especially after an OrcaSlicer update):**
  1. Open OrcaSlicer **Plugins** and **reload / re-enable** `Kobra-Van-Kenorca`.
  2. Check **Process → Others → Slicing Pipeline Plugin** and make sure `KobraVanKenorca_PostProcess` is listed **once** (duplicate entries are automatically cleaned up by the plugin).
- **Offline `.gcode` Export:** Use **Export .gcode...** inside `Map · Check · Send` to save a fully post-processed `.gcode` file to your Downloads or chosen folder (with automatic sandbox fallback to the plugin's `exported_gcode/` directory on Linux/macOS).
- **Diagnostics & Bug Reports:** Open **Settings → Diagnostics** to copy an anonymized diagnostics summary (IPs, API keys, and local paths are scrubbed) or inspect **Flush & Audit** for the full N×N purge matrix and per-toolchange log.
