#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# [tool.orcaslicer.plugin]
# name = "Anycubic Kobra S1 ACE Pro"
# description = "Vanilla Klipper + ACE Pro Multi-Color & Moonraker Integration for Anycubic Kobra S1"
# author = "Yves"
# version = "1.0.0"
# ///
"""
OrcaSlicer 2.5.x Plugin for Anycubic Kobra S1 (Vanilla Klipper + ACE Pro).

Uses exclusively the Python standard library (`urllib.request`, `urllib.error`,
`json`, `re`, `math`, `os`) to avoid any external wheel or glibc conflicts inside
the Linux Flatpak embedded CPython runtime.

Features:
1. Moonraker REST communication (`http://192.168.8.80:7125` default, with automatic
   port 80 fallback) & upfront permission declaration via `orca.request_permissions(...)`.
2. Live ACE Pro status querying via `/printer/objects/query` (`ace`, `ace_state`,
   `ace_instance_0`, `ace_instance_1`, `save_variables`) and `/server/ace/status` fallback,
   exposing slots T0..T3 / T0..T7, hex colors, material types, slot states
   (`ready`, `loaded`, `empty`), and dryer telemetry.
3. Embedded HTML/JS UI (`orca.pages.PagesPluginCapabilityBase`) with:
   - Visual ACE Pro slot overview (Hex swatches, material, nozzle temp, status, RFID badge)
   - Slicer Filament -> Physical ACE Slot Color Mapping Matrix (T0..T7) + Auto-Match
   - Immediate G-code trigger buttons (`/printer/gcode/script`):
     * `CUT_TIP`
     * Bed Leveling (`G31` / `BED_MESH_CALIBRATE PROFILE=adaptive ADAPTIVE=1`)
     * ACE Dryer Start (`ACE_START_DRYING TEMP=<temp> DURATION=<min>`) & Stop (`ACE_STOP_DRYING`)
     * Per-slot Load (`ACE_CHANGE_TOOL TOOL=<n>`) / Unload (`ACE_CHANGE_TOOL TOOL=-1` / `ACE_SMART_UNLOAD TOOL=<n>`)
       and Feed (`ACE_FEED`) / Retract (`ACE_RETRACT`)
4. Pre-Print Toggles & Integrated G-Code Post-Processor (`orca.slicing.SlicingPipelineCapabilityBase`):
   - Computes `PURGELENGTH` (mm) from `; flush_multiplier`, `; flush_volumes_matrix`,
     `; flush_volumes_vector`, and `; filament_diameter` strictly using the ORIGINAL
     Slicer tool indices (`old_slicer_tool -> new_slicer_tool`) BEFORE remapping tools.
   - Rewrites `tool=<n>` in `G9111` to `tool=<mapped>` and handles
     `SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1` so the initial `T<n>` inside `; FLUSH_START`
     emits `PURGELENGTH=0.00` (unless `force_initial_purge` is enabled).
   - Rewrites every `T<n>` to `T<mapped>` and inserts
     `ACE_SET_PURGE_AMOUNT PURGELENGTH=<len_mm>` immediately above it.
   - Pre-print checkboxes: Bed Leveling (`G31`), Cut Tip before print (`CUT_TIP` before `G9111`),
     Cut Tip after print (`CUT_TIP` at end of G-code), and Force Initial Purge.
   - Prepends an Audit Header comment block at the very top of the G-code file.
5. Multipart G-Code Upload to Moonraker (`/server/files/upload`) with "Start Print immediately".
"""

import base64
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# OrcaSlicer 2.5.x Embedded CPython SDK Import (with standalone/test shim)
# ---------------------------------------------------------------------------
try:
    import orca as _imported_orca  # type: ignore

    if hasattr(_imported_orca, "plugin") and hasattr(_imported_orca, "register_capability"):
        orca = _imported_orca
        HAS_ORCA_RUNTIME = True
    else:
        raise ImportError("System 'orca' package is not the OrcaSlicer embedded SDK")
except ImportError:
    HAS_ORCA_RUNTIME = False

    class _DummyPluginResult:
        Success = "Success"
        Skipped = "Skipped"
        RecoverableError = "RecoverableError"
        FatalError = "FatalError"

    class _DummyExecutionResult:
        def __init__(self, ok: bool = True, message: str = "", status: Any = None, data: str = "") -> None:
            self.ok = ok
            self.message = message
            self.status = status or (_DummyPluginResult.Success if ok else _DummyPluginResult.RecoverableError)
            self.data = data

        @classmethod
        def success(cls, message: str = "", data: str = "") -> "_DummyExecutionResult":
            return cls(True, message, _DummyPluginResult.Success, data)

        @classmethod
        def skipped(cls, message: str = "") -> "_DummyExecutionResult":
            return cls(True, message, _DummyPluginResult.Skipped, "")

        @classmethod
        def failure(cls, status: Any, message: str = "", data: str = "") -> "_DummyExecutionResult":
            return cls(False, message, status, data)

    class _DummyStep:
        psGCodePostProcess = "psGCodePostProcess"

    class _DummySlicingModule:
        Step = _DummyStep

        class SlicingPipelineCapabilityBase:
            def get_name(self) -> str:
                return ""

            def execute(self, ctx: Any) -> Any:
                return _DummyExecutionResult.success()

    class _DummyPagesModule:
        class PagesPluginCapabilityBase:
            def get_name(self) -> str:
                return ""

            def get_ui(self) -> str:
                return ""

            def on_message(self, data: Any) -> None:
                pass

            def post_message(self, data: Any) -> None:
                pass

    class _DummyScriptModule:
        class ScriptPluginCapabilityBase:
            def get_name(self) -> str:
                return ""

            def execute(self) -> Any:
                return _DummyExecutionResult.success()

    class _DummyHostPlugin:
        @staticmethod
        def storage() -> str:
            return os.path.join(os.path.expanduser("~"), ".local", "share", "kobra_s1_ace_plugin")

    class _DummyNotificationLevel:
        RegularNotificationLevel = "RegularNotificationLevel"
        ImportantNotificationLevel = "ImportantNotificationLevel"
        HintNotificationLevel = "HintNotificationLevel"
        WarningNotificationLevel = "WarningNotificationLevel"
        ErrorNotificationLevel = "ErrorNotificationLevel"

    class _DummyHostUi:
        NotificationLevel = _DummyNotificationLevel

        @staticmethod
        def push_notification(level: Any, text: str, hyper_text: str = "", on_click: Any = None) -> None:
            pass

    class _DummyHostModule:
        plugin = _DummyHostPlugin()
        ui = _DummyHostUi()

        @staticmethod
        def model() -> Any:
            return None

        @staticmethod
        def plater() -> Any:
            return None

    class _DummyLifecycleEvent:
        SliceStarted = "SliceStarted"
        SliceGeometryFinished = "SliceGeometryFinished"
        GCodeExportStarted = "GCodeExportStarted"
        GCodeExportFinished = "GCodeExportFinished"
        SlicingJobComplete = "SlicingJobComplete"
        PrintJobStarted = "PrintJobStarted"
        PrintJobFinished = "PrintJobFinished"
        SendJobStarted = "SendJobStarted"
        SendJobFinished = "SendJobFinished"
        UploadStarted = "UploadStarted"
        UploadFinished = "UploadFinished"
        PresetSelected = "PresetSelected"
        ProjectOpened = "ProjectOpened"
        NewProject = "NewProject"
        DeviceSelected = "DeviceSelected"

    class _DummyLifecycleEvtCode:
        Success = "Success"
        Error = "Error"

    class _DummyOrcaModule:
        base = object
        PluginResult = _DummyPluginResult
        ExecutionResult = _DummyExecutionResult
        LifecycleEvent = _DummyLifecycleEvent
        LifecycleEvtCode = _DummyLifecycleEvtCode
        host = _DummyHostModule()
        slicing = _DummySlicingModule()
        pages = _DummyPagesModule()
        script = _DummyScriptModule()
        last_requested_permissions: Dict[str, Any] = {}
        registered_capabilities: List[Any] = []

        @staticmethod
        def plugin(cls: Any) -> Any:
            cls._is_orca_plugin = True
            return cls

        @classmethod
        def request_permissions(cls, **kwargs: Any) -> None:
            cls.last_requested_permissions.update(kwargs)

        @classmethod
        def register_capability(cls, cap_cls: Any) -> None:
            cls.registered_capabilities.append(cap_cls)

    orca = _DummyOrcaModule()  # type: ignore


# ---------------------------------------------------------------------------
# Constants & Regular Expressions
# ---------------------------------------------------------------------------
DEFAULT_MOONRAKER_URL = "http://192.168.8.80:7125"
FALLBACK_MOONRAKER_PORT = 80
DEFAULT_FILAMENT_DIAMETER = 1.75
DEFAULT_ROUND_DIGITS = 2
SLOTS_PER_ACE_UNIT = 4
MAX_ACE_SLOTS = 8
PIPELINE_CAPABILITY_NAME = "Anycubic Kobra S1 ACE Pro Post-Processor"
_PIPELINE_UPDATE_LOCK = threading.Lock()
_KOBRA_S1_PRINTER_RE = re.compile(
    r"(?:^|[^a-z0-9])kobra[ _-]*s1(?:$|[^a-z0-9])(?![ _-]*max(?:$|[^a-z0-9]))",
    re.IGNORECASE,
)

# Exact regexes from refs/orca_flush_to_purgelength.py + Slicer header comments
MUL_RE = re.compile(r"^\s*;\s*flush_multiplier\s*[:=]\s*(.*)$")
MAT_RE = re.compile(r"^\s*;\s*flush_volumes_matrix\s*[:=]\s*(.*)$")
VEC_RE = re.compile(r"^\s*;\s*flush_volumes_vector\s*[:=]\s*(.*)$")
DIAM_RE = re.compile(r"^\s*;\s*filament_diameter\s*[:=]\s*(.*)$")
FILAMENT_USED_HEADER_RE = re.compile(r"^\s*;\s*filament\s*:\s*([0-9,\s]+)$")
FILAMENT_COLOUR_RE = re.compile(r"^\s*;\s*filament_colour\s*[:=]\s*(.+)$")
FILAMENT_MULTI_COLOUR_RE = re.compile(r"^\s*;\s*filament_multi_colour\s*[:=]\s*(.+)$")
FILAMENT_TYPE_RE = re.compile(r"^\s*;\s*filament_type\s*[:=]\s*(.+)$")
FILAMENT_VENDOR_RE = re.compile(r"^\s*;\s*filament_vendor\s*[:=]\s*(.+)$")
FILAMENT_SETTINGS_ID_RE = re.compile(r"^\s*;\s*filament_settings_id\s*[:=]\s*(.+)$")
FILAMENT_USED_G_RE = re.compile(r"^\s*;\s*filament used \[g\]\s*=\s*([0-9.,\s]+)$")
FILAMENT_USED_MM_RE = re.compile(r"^\s*;\s*filament used \[mm\]\s*=\s*([0-9.,\s]+)$")
FILAMENT_USED_CM3_RE = re.compile(r"^\s*;\s*filament used \[cm3\]\s*=\s*([0-9.,\s]+)$")
TOTAL_FILAMENT_G_RE = re.compile(r"^\s*;\s*total filament used \[g\]\s*=\s*([0-9.]+)\s*$")
TOTAL_LAYERS_RE = re.compile(r"^\s*;\s*total layers?(?: count| number)\s*[:=]\s*(\d+)\s*$", re.IGNORECASE)
ESTIMATED_TIME_RE = re.compile(r"^\s*;\s*estimated printing time.*=\s*(.+?)\s*$", re.IGNORECASE)
MAX_Z_HEIGHT_RE = re.compile(r"^\s*;\s*max_z_height\s*[:=]\s*([0-9.]+)\s*$", re.IGNORECASE)
THUMBNAIL_BEGIN_RE = re.compile(
    r"^\s*;\s*thumbnail(?:_PNG)?\s+begin\s+(\d+)\s*x\s*(\d+)\s+(\d+)\b",
    re.IGNORECASE,
)
THUMBNAIL_END_RE = re.compile(
    r"^\s*;\s*thumbnail(?:_PNG)?\s+end\b",
    re.IGNORECASE,
)

# Executable G-code line matchers (never match pure comment lines starting with ';')
TOOL_RE = re.compile(r"^(\s*)T(\d+)(\s*(?:;.*)?)$")
G9111_RE = re.compile(r"^(\s*G9111\b.*)$", re.IGNORECASE)
G9111_TOOL_PARAM_RE = re.compile(r"(\btool\s*=\s*)(\d+)", re.IGNORECASE)
SKIP_PURGE_LINE_RE = re.compile(
    r"^\s*SKIP_PURGE_FOR_ALREADY_LOADED_TOOL\s*=\s*(\d+)\s*(?:;.*)?$",
    re.IGNORECASE,
)
SKIP_PURGE_INLINE_RE = re.compile(
    r"\bSKIP_PURGE_FOR_ALREADY_LOADED_TOOL\s*=\s*(\d+)",
    re.IGNORECASE,
)
G31_LINE_RE = re.compile(r"^(\s*)G31(\b[^;\r\n]*)(;.*)?$", re.IGNORECASE)
PRINT_END_LINE_RE = re.compile(r"^(\s*PRINT_END\b.*)$", re.IGNORECASE)
PRINT_END_CUT_TIP_PARAM_RE = re.compile(r"(\bCUT_TIP\s*=\s*)([01]|true|false)", re.IGNORECASE)
EXEC_BLOCK_END_RE = re.compile(r"^\s*;\s*EXECUTABLE_BLOCK_END\b", re.IGNORECASE)
CONFIG_BLOCK_START_RE = re.compile(r"^\s*;\s*CONFIG_BLOCK_START\b", re.IGNORECASE)
LAYER_CHANGE_RE = re.compile(r"^\s*;\s*LAYER_CHANGE\b", re.IGNORECASE)
ACE_SET_PURGE_LINE_RE = re.compile(r"^\s*ACE_SET_PURGE_AMOUNT\b.*$", re.IGNORECASE)
CUT_TIP_LINE_RE = re.compile(r"^\s*CUT_TIP(?:\s*;.*)?\s*$", re.IGNORECASE)
SKIPPED_G31_RE = re.compile(r"^\s*;\s*G31\s*;\s*skipped by ACE Plugin\s*$", re.IGNORECASE)
REV_META_RE = re.compile(r"^\s*;\s*@ORCA_REVERSIBLE_META\s*=\s*(.+?)\s*$")
ORIG_TOOL_INLINE_RE = re.compile(r"\s*;\s*@ORCA_ORIG_TOOL\s*:\s*(\d+)\s*$")


# ---------------------------------------------------------------------------
# Internationalization (i18n) System (English & German)
# ---------------------------------------------------------------------------
SUPPORTED_LANGUAGES = ("en", "de")
DEFAULT_LANGUAGE = "en"

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        # Header & Theme / Language Controls
        "app.title": "Anycubic Kobra S1 — ACE Pro Control",
        "app.subtitle": "Moonraker REST • 8-Slot Multi-Color Hub • Flush Volume & Purge Engine • G-code Post-Processor",
        "header.language.aria": "Select interface language",
        "header.language.en": "English",
        "header.language.de": "Deutsch",
        "header.theme.aria": "Select color theme",
        "header.theme.auto": "Auto",
        "header.theme.light": "Light",
        "header.theme.dark": "Dark",
        "header.conn.offline": "Offline",
        "header.conn.online": "Moonraker Online",
        "header.conn.offline_ace": "Moonraker Offline",
        "header.conn.unreachable": "Moonraker Unreachable",

        # Executive Status Strip
        "exec.printer.label": "Printer & ACE Status",
        "exec.printer.not_queried": "Not queried yet",
        "exec.printer.printing_badge": "PRINTING ({state})",
        "exec.printer.state_badge": "State: {state}",
        "exec.printer.ace_ready": "ACE Ready",
        "exec.printer.ace_prefix": "ACE: {status}",
        "exec.printer.slots_ready": "{ready}/{total} Slots ready",
        "exec.printer.temp": "Nozzle {extruder}°C • Bed {bed}°C",
        "exec.dryer.label": "Active Dryer",
        "exec.dryer.standby": "Standby",
        "exec.dryer.off_sub": "Dryer disabled • 0 °C target",
        "exec.dryer.active": "ACTIVE • {temp} °C",
        "exec.dryer.remain": "Remaining time: {mins} min",
        "exec.mapping.label": "Mapping Readiness",
        "exec.mapping.single": "Single / Standard",
        "exec.mapping.no_project": "No slicer project loaded",
        "exec.mapping.conflicts": "{count} Conflicts",
        "exec.mapping.conflicts_sub": "Empty or unconfigured target slots",
        "exec.mapping.warnings": "{count} Warnings",
        "exec.mapping.warnings_sub": "Duplicate slot or material mismatch",
        "exec.mapping.ready": "{count} Extruders Ready",
        "exec.mapping.ready_sub": "All active extruders validly mapped",
        "exec.flush.label": "Flush & Audit Status",
        "exec.flush.awaiting": "Awaiting Analysis",
        "exec.flush.awaiting_sub": "Run Flush Matrix / Preview",
        "exec.flush.incomplete": "Incomplete",
        "exec.flush.warnings": "Warnings ({count})",
        "exec.flush.ok": "Post-Process OK",
        "exec.flush.sub": "{tc} Toolchanges • Mul ×{mul}",
        "exec.safety.label": "Safety Interlock",
        "exec.safety.armed": "Armed (Idle)",
        "exec.safety.armed_sub": "Hardware actions enabled",
        "exec.safety.locked": "PRINT LOCK ACTIVE",
        "exec.safety.locked_sub": "Hardware & active print protected",

        # Safety Banner
        "safety.banner.title": "Print Safety Interlock Active:",
        "safety.banner.body": "A print job is currently active on the Kobra S1 ({state}). All hardware commands (Load, Unload, CUT_TIP, Bed Leveling, Dryer) and “Upload & Start Print” are locked to protect the running print.",

        # Section 1: Moonraker Connection
        "sec1.title": "Moonraker Connection",
        "sec1.subtitle": "REST Endpoint & Status Polling",
        "sec1.badge": "Moonraker API",
        "sec1.url.label": "Moonraker URL (Kobra S1 IP:Port)",
        "sec1.url.placeholder": "http://192.168.8.80:7125",
        "sec1.btn.test": "Test Connection",
        "sec1.btn.refresh": "Refresh ACE Status",
        "sec1.btn.save": "Save Settings",

        # Section 2: ACE Pro Slots & Dryer
        "sec2.title": "ACE Pro Live Slots (T0–T7) & Dryer",
        "sec2.subtitle": "Hardware Spool Status, RFID & Filament Dryer",
        "sec2.badge": "8 Slots",
        "sec2.dryer.title": "ACE Pro Filament Dryer",
        "sec2.dryer.temp": "Target Temp (°C)",
        "sec2.dryer.duration": "Duration (min)",
        "sec2.dryer.start": "Start Dryer",
        "sec2.dryer.stop": "Stop",
        "slot.status.ready": "READY",
        "slot.status.empty": "EMPTY",
        "slot.badge.rfid": "RFID",
        "slot.badge.manual": "Manual",
        "slot.empty_spool": "Empty Spool",
        "slot.unknown": "Unknown",
        "slot.no_filament": "No filament detected",
        "slot.sku": "SKU: {sku}",
        "slot.temp_range": "{min}–{max}°C",
        "slot.manual_assign": "Manual Assignment",
        "slot.btn.load": "Load (T{slot})",
        "slot.btn.unload": "Unload",
        "slot.btn.load.locked_title": "Locked while a print is active",
        "slot.btn.load.empty_title": "Slot is empty",
        "slot.btn.load.title": "Load filament from T{slot}",
        "slot.btn.unload.title": "Unload filament from T{slot}",

        # Section 3: Color Mapping Workbench
        "sec3.title": "Color Mapping Workbench",
        "sec3.subtitle": "OrcaSlicer Extruder → Physical ACE Pro Slot",
        "sec3.btn.sync": "Sync Slicer Filaments",
        "sec3.btn.automatch": "Auto-Match (ΔE + Material)",
        "sec3.btn.reset": "Reset 1:1",
        "map.col.extruder": "Slicer Extruder",
        "map.col.target": "ACE Target Slot",
        "map.col.physical": "Physical ACE Spool",
        "map.col.status": "Validation & ΔE",
        "map.extruder.Active": "Active",
        "map.extruder.Standby": "Standby",
        "map.extruder.fallback_name": "Extruder {index}",
        "map.option.empty": "EMPTY",
        "map.phys.unconfigured": "Unconfigured",
        "map.phys.empty_slot": "Empty Slot",
        "map.phys.not_present": "Slot T{slot} not present",
        "map.status.not_present": "Slot missing",
        "map.status.empty_error": "ERROR: Slot Empty",
        "map.status.dup_warn": "Duplicate T{slot}",
        "map.status.mat_warn": "Mat. {slicer}≠{ace}",
        "map.status.unconf_warn": "Unconfigured",
        "map.status.ok": "OK",
        "map.summary.conflicts": "{count} mapping conflict(s) detected — upload blocked until resolved.",
        "map.summary.warnings": "{count} warning(s) — please review mapping before printing.",
        "map.summary.valid": "All active extruders are validly assigned to ready ACE slots.",

        # Section 4: Print Preparation & Hardware Macros
        "sec4.title": "Print Preparation & Hardware Macros",
        "sec4.subtitle": "G31 Bed Leveling, CUT_TIP & Initial Purge Control",
        "sec4.badge": "Klipper Macros",
        "sec4.bed_leveling.title": "Perform Bed Leveling (G31) before print",
        "sec4.bed_leveling.sub": "Unchecked comments out G31 inside the G9111 start block",
        "sec4.skip_purge.title": "Skip purge for already loaded tool",
        "sec4.skip_purge.sub": "Sets SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 or 0 on G9111",
        "sec4.cut_post.title": "Cut Tip after Print (PRINT_END CUT_TIP)",
        "sec4.cut_post.sub": "Sets PRINT_END CUT_TIP=1 or 0 at print end",
        "sec4.force_purge.title": "Force initial purge on start slot",
        "sec4.force_purge.sub": "Ignores SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 on first T<n>",
        "sec4.direct.label": "Direct Klipper Macro Execution (Disabled during print):",
        "sec4.btn.cut_now": "Execute CUT_TIP Now",
        "sec4.btn.g31_now": "Bed Leveling (G31)",
        "sec4.btn.mesh_now": "BED_MESH_CALIBRATE",

        # Section 5: G-code Post-Processing & Moonraker Upload
        "sec5.title": "G-code Post-Processing & Moonraker Upload",
        "sec5.subtitle": "Reversible Purge Injection, Audit Header & Print Start Gate",
        "sec5.badge": "Pipeline",
        "sec5.path.label": "Local G-code File Path (for manual testing / direct upload)",
        "sec5.path.placeholder": "/path/to/plate_1.gcode",
        "sec5.btn.analyze": "Flush Matrix & Analysis",
        "sec5.btn.preview": "G-code Preview (Diff)",
        "sec5.btn.postprocess": "Post-Process G-code",
        "sec5.btn.upload_only": "Upload Only",
        "sec5.btn.upload_start": "Upload & Start Print",
        "sec5.preview.title": "G-code Transformation Preview (Original vs. Post-Processed)",
        "sec5.preview.close": "Close",

        # Section 6: Flush Volume Monitoring & Post-Processing Audit
        "sec6.title": "6. Flush Volume Monitoring & Post-Processing Audit",
        "sec6.subtitle": "OrcaSlicer Flush Matrix (Row-Major), Vector Summary & Toolchange Timeline",
        "flush.status.not_analyzed": "Not analyzed yet",
        "flush.kpi.multiplier": "Flush Multiplier",
        "flush.kpi.diameter": "Filament Ø / Cross-Section",
        "flush.kpi.toolchanges": "Toolchanges / Purge Cmds",
        "flush.kpi.start_tool": "G9111 Start Tool → ACE Slot",
        "flush.kpi.bed_cut": "G31 / G9111 / PRINT_END",
        "flush.kpi.bed_active": "G31 Active",
        "flush.kpi.bed_skipped": "G31 Off ({count}x)",
        "flush.kpi.skip_purge": "SkipPurge",
        "flush.kpi.cut_post": "EndCut",
        "flush.kpi.cut_none": "No Cut",
        "flush.gate.ready": "Upload Ready ({status})",
        "flush.gate.blocked": "Upload Blocked: {reasons}",
        "flush.matrix.title": "Flush Volume Matrix (From → To in mm³ & Purge Length in mm)",
        "flush.matrix.empty": "Load a G-code file and click “Flush Matrix & Analysis” to inspect the N×N matrix.",
        "flush.matrix.no_data": "No flush_volumes_matrix present in G-code (single extruder or missing comments).",
        "flush.matrix.corner": "From \\ To",
        "flush.vector.title": "Initial Flush Vector Summary (flush_volumes_vector)",
        "flush.vector.empty": "Shows initial purge volumes (N or 2×N Unload/Load) per extruder.",
        "flush.vector.no_data": "No flush_volumes_vector data present.",
        "flush.vector.col.extruder": "Extruder",
        "flush.vector.col.unload": "Unload (mm³)",
        "flush.vector.col.load": "Load (mm³)",
        "flush.vector.col.raw_sum": "Raw Sum (mm³)",
        "flush.vector.col.effective": "Effective ×{mul} (mm³)",
        "flush.vector.col.purge": "Initial Purge Length (mm)",
        "flush.timeline.title": "Toolchange & Purge Timeline",
        "flush.timeline.empty": "No toolchanges analyzed yet.",
        "flush.timeline.no_events": "No T<n> toolchange commands found in executable G-code.",
        "flush.timeline.col.idx": "#",
        "flush.timeline.col.slicer": "Slicer Tool (Old → New)",
        "flush.timeline.col.ace": "ACE Target Slot",
        "flush.timeline.col.raw": "Raw Volume (mm³)",
        "flush.timeline.col.eff": "Effective (mm³)",
        "flush.timeline.col.diam": "Ø (mm)",
        "flush.timeline.col.cmd": "Generated Klipper Command",
        "flush.timeline.initial": "Initial → T{tool}",

        # Activity Log Footer
        "log.title": "System & Activity Log",
        "log.clear": "Clear Log",
        "log.ready": "[Ready] ACE Pro Plugin UI initialized.",
        "log.cleared": "[Ready] Log cleared.",
        "log.no_host": "[Simulation / Preview] OrcaSlicer host bridge not active — sent message: {msg}",

        # Validation, Safety & Backend Messages
        "val.num.invalid": "Invalid numeric value '{token}' in {field}; replaced with {default}.",
        "val.num.non_finite": "Non-finite value '{token}' in {field}; replaced with {default}.",
        "val.num.below_min": "Invalid value {val} (< {min}) in {field}; clamped to {default}.",
        "val.map.not_present": "Slicer extruder T{slicer_tool} is mapped to non-existent ACE slot T{mapped} (available: 0..{max_slot}).",
        "val.map.empty_slot": "Slicer extruder T{slicer_tool} is mapped to empty ACE slot T{mapped}.",
        "val.map.mat_mismatch": "Material mismatch on extruder T{slicer_tool} ({slicer_mat}) -> ACE slot T{mapped} ({ace_mat}).",
        "val.map.unconfigured": "ACE slot T{mapped} (for extruder T{slicer_tool}) is unconfigured (no RFID, empty material, #000000).",
        "val.map.duplicate": "Duplicate slot assignment: Active extruders {extruders} both map to ACE slot T{phys_tool}.",
        "val.map.shared": "Shared ACE slot: Active extruders {extruders} both reuse ACE slot T{phys_tool}.",
        "val.gcode.mul_non_finite": "Non-finite flush_multiplier '{raw}'; falling back to 1.0.",
        "val.gcode.mul_negative": "Negative flush_multiplier ({val}); clamped to 0.0.",
        "val.gcode.mul_invalid": "Invalid flush_multiplier '{raw}'; falling back to 1.0.",
        "val.gcode.diam_invalid": "Invalid filament diameter ({diam}); falling back to default ({default} mm).",
        "val.gcode.matrix_empty": "flush_volumes_matrix is empty.",
        "val.gcode.matrix_not_square": "Invalid flush_volumes_matrix size ({count} elements; not an N×N square matrix).",
        "val.gcode.vector_mismatch": "flush_volumes_vector size ({vec_len}) does not match {n_tools} extruders (expected {n_tools} or {double_tools}).",
        "val.gcode.tool_exceeds": "Tool ID T{tool} exceeds detected extruder count ({max_tools}).",
        "val.gcode.vector_missing_init": "flush_volumes_vector is missing for initial flush to T{tool}.",
        "val.gcode.matrix_missing_tc": "flush_volumes_matrix is missing or invalid for toolchange T{old_tool} -> T{new_tool}.",
        "val.gcode.matrix_missing_multi": "Multiple extruders detected, but flush_volumes_matrix is missing or invalid.",
        "map.warn.order_reset": "Extruder count/order changed ({old_len} -> {new_len}). Color mapping was reset to 1:1.",
        "safety.lock.error": "Safety Lock: Action '{action}' blocked because a print is currently active (state: {state}).",
        "gate.block.flush_incomplete": "Flush calculation incomplete (FLUSH_DATA_INCOMPLETE). Please check flush_volumes_matrix / flush_volumes_vector.",
        "gate.block.printing_start": "Cannot start print: Printer is currently busy ({state}).",

        # Host Log Messages
        "host.settings_saved": "Settings saved ({url})",
        "host.lang_saved": "Language changed to {lang_name}.",
        "host.sync_filaments": "{count} slicer filaments synchronized.",
        "host.automatch_done": "Auto-Match completed (ΔE color distance + material priority).",
        "host.reset_done": "Color mapping reset to 1:1 default.",
        "host.conn_ok": "Moonraker connected! Server: {version}",
        "host.conn_fail": "Moonraker not reachable at {url}",
        "host.gcode_sent": "G-code executed: {cmd}",
        "host.slot_loaded": "Slot T{slot} loaded ({cmd})",
        "host.slot_unloaded": "Slot T{slot} unloaded ({cmd})",
        "host.dryer_started": "Dryer started: {temp}°C for {duration} min",
        "host.dryer_started_unit": "Dryer started ({unit}): {temp}°C for {duration} min ({cmd})",
        "host.dryer_stopped": "Dryer stopped",
        "host.dryer_stopped_unit": "Dryer stopped ({unit}): {cmd}",
        "host.no_gcode_loaded": "No G-code file loaded. Slice a plate in OrcaSlicer or click 'Browse .gcode' first.",
        "host.pp_modal_ok": "Post-processed '{file}' ({tc} toolchanges, {len} mm purge) — saved locally.",
        "host.upload_modal_ok": "Uploaded '{file}' to {url} ({mode}, {len} mm purge).",
        "host.upload_mode_print": "Print Started",
        "host.upload_mode_only": "Upload Only",
        "host.dryer_invalid_temp": "Invalid dryer temperature ({temp}°C); clamped to 20–70°C.",
        "host.err_no_gcode_analyze": "Please specify a valid G-code file path to analyze.",
        "host.analyze_ok": "Flush analysis finished ({status}): {tc} toolchanges, matrix={mat}, vector={vec}.",
        "host.err_no_gcode_preview": "Please specify a valid G-code file path for preview.",
        "host.preview_ok": "Preview generated: {tc} toolchanges, status={status}",
        "host.err_no_gcode_pp": "Please specify a valid G-code file path.",
        "host.pp_ok": "Post-processing finished ({status}): {tc} toolchanges, G9111 tool={g9111}",
        "host.err_no_gcode_upload": "Please specify a valid G-code file path for upload.",
        "host.upload_blocked": "Upload blocked: {reasons}",
        "host.upload_ok": "Post-processing ({tc} toolchanges) & upload successful: {res}",
        "host.error_prefix": "Error: {error}",
        "gate.block.no_info": "No valid post-processing result available.",
        "gate.block.multi_incomplete": "Multiple extruders used, but flush matrix/vector is incomplete.",
        "host.ready": "ACE Pro Plugin ready.",
        "host.sync_none": "No active filament presets received from orca.host.preset_bundle().",
        "host.status_updated": "Moonraker & ACE Pro status updated.",
        "host.hw_busy": "A hardware action is already running — please wait.",
        "host.macro_sent": "Macro '{macro}' sent to Moonraker.",
        "host.slot_active_unloaded": "Active tool unloaded (ACE_CHANGE_TOOL TOOL=-1).",
        "host.slot_fed": "Slot T{slot}: fed {length}mm (ACE_FEED).",
        "host.slot_retracted": "Slot T{slot}: retracted {length}mm (ACE_RETRACT).",
        "host.slot_unknown_action": "Unknown slot action: {action}",
        "host.file_not_found": "G-code file not found: {path}",
        "host.parse_reset_note": " (Color mapping reset due to changed filament order)",
        "host.parse_ok": "{count} slicer filaments loaded from '{file}'.{note}",
        "host.automatch_warns": " | Warnings: {warns}",
        "host.automatch_log": "Auto-Match completed: {matched}{warns}",
        "host.preview_log": "Offline Preview ({file}): Status={status}, {tc} Toolchanges, Total Purge={len} mm ({vol} mm³) [File unchanged]",
        "host.pp_log": "Post-Processing completed ({file}): Status={status}, {tc} Toolchanges, multiplier={mul}, bedLeveling={bed}, Total Purge={len} mm",
        "host.upload_gate_blocked": "{base} | Upload blocked (Safety Gate): {reasons}",
        "host.hw_busy_upload": "A hardware action is already running — Upload & Start Print aborted.",
        "host.upload_done": "{base} | Upload to {url} successful.",
        "host.upload_done_started": "{base} | Upload to {url} successful (Print started).",
        "host.msg_error": "Error in '{msg_type}': {error}",
        "host.status_query_failed": "{base} | Moonraker status query failed: {error}",
        "ui.brand.title": "Anycubic Kobra S1 — Vanilla Klipper + ACE Pro",
        "ui.brand.sub": "OrcaSlicer 2.5.x Color Mapping, Flush2Len & Moonraker Workbench",
        "ui.conn.connecting": "Moonraker: Connecting...",
        "ui.conn.connected": "Connected ({model})",
        "ui.conn.disconnected": "Disconnected",
        "ui.printer.standby": "Printer: Standby",
        "ui.printer.state": "Printer: {state}",
        "ui.header.refresh": "🔄 Refresh",
        "ui.safety.banner_prefix": "⚠️ Print active (",
        "ui.safety.banner_suffix": ") — All hardware, motion, load/unload, and direct-print commands are locked to protect the active print.",
        "ui.safety.badge": "Hardware Lock Active",
        "ui.exec.conn.kicker": "Moonraker & Printer",
        "ui.exec.conn.online": "Online ({state})",
        "ui.exec.conn.offline": "Offline / Ready",
        "ui.exec.tool.kicker": "Active Toolhead",
        "ui.exec.tool.none": "None (-1)",
        "ui.exec.tool.sub": "ACE Temp: {temp} · Fan: {fan}",
        "ui.exec.dryer.kicker": "ACE Pro Dryer",
        "ui.exec.dryer.standby": "Standby",
        "ui.exec.dryer.active": "Active ({temp}°C)",
        "ui.exec.dryer.remain": "Remaining: {mins} min",
        "ui.exec.dryer.inactive": "No active drying",
        "ui.exec.gcode.kicker": "Active G-code & Purge",
        "ui.exec.gcode.none": "No file",
        "ui.exec.gcode.ready_sub": "Ready for Offline Preview / Patch",
        "ui.exec.gcode.select_sub": "Select G-code for Preview / Patch",
        "ui.exec.gcode.stats": "{tc} Changes · {len} mm ({vol} mm³)",
        "ui.sec1.title": "1. Moonraker Connection",
        "ui.sec1.offline_badge": "Offline analysis works independently",
        "ui.sec1.url_label": "URL:",
        "ui.sec1.port80": "Port 80 Fallback",
        "ui.sec1.apikey_label": "API Key:",
        "ui.sec1.apikey_placeholder": "Optional X-Api-Key",
        "ui.sec1.connect_btn": "Connect & Load Status",
        "ui.sec1.meta_initial": "ACE Status: — | Active Tool: — | Temp: —",
        "ui.sec1.meta_live": "ACE Status: {status} | Active Tool: {tool} | Temp: {temp}°C | Fan: {fan} RPM{print_label}",
        "ui.sec1.meta_print": " | Print: {state}",
        "ui.sec2.title": "2. ACE Pro Dryer Control",
        "ui.sec2.dryer_stop_badge": "Dryer: Stop",
        "ui.sec2.dryer_active_badge": "Dryer: {temp}°C ({mins} min left)",
        "ui.sec2.unit_label": "Unit:",
        "ui.sec2.unit_all": "Both (All ACE)",
        "ui.sec2.unit_ace1": "ACE 1 (T0–T3)",
        "ui.sec2.unit_ace2": "ACE 2 (T4–T7)",
        "ui.sec2.unit_select_aria": "Select ACE Pro dryer unit",
        "ui.sec2.btn_unload": "⏏️ Unload (T-1)",
        "ui.sec2.temp_label": "Temp (°C):",
        "ui.sec2.dur_label": "Duration (min):",
        "ui.sec2.btn_dryer_start": "🔥 Dryer Start",
        "ui.sec2.btn_dryer_stop": "⏹️ Dryer Stop",
        "ui.sec3.title": "3. ACE Pro Live Slot Bay (T0–T7)",
        "ui.sec3.feed_label": "Feed/Retract (mm):",
        "ui.sec3.btn_live": "🔄 Live Status",
        "ui.slot.badge_active": "Active",
        "ui.slot.badge_ready": "Ready",
        "ui.slot.badge_empty": "Empty",
        "ui.slot.no_filament": "No Filament",
        "ui.slot.unconfigured": "Unconfigured",
        "ui.slot.rfid_detected": "RFID detected",
        "ui.slot.btn_load": "Load",
        "ui.slot.btn_unload": "Unload",
        "ui.slot.btn_feed": "+Feed",
        "ui.slot.btn_retr": "-Retr",
        "ui.slot.title_load": "Load tool T{tool}",
        "ui.slot.title_unload": "Unload slot T{tool}",
        "ui.sec4.title": "4. Color Mapping Workbench (Slicer → ACE)",
        "ui.sec4.btn_project": "🔄 OrcaSlicer Project",
        "ui.sec4.btn_automatch": "✨ Auto-Match",
        "ui.sec4.btn_reset": "↺ 1:1 Default",
        "ui.sec4.summary_ready": "Ready for filament assignment.",
        "ui.sec4.col_extruder": "Slicer Extruder",
        "ui.sec4.col_mat_color": "Material & Color",
        "ui.sec4.col_mode": "Mode",
        "ui.sec4.col_slot": "Physical ACE Slot",
        "ui.map.mode_auto": "Auto-Match",
        "ui.map.mode_manual": "Manual",
        "ui.map.mode_default": "1:1",
        "ui.map.not_present": "⚠️ T{slot} (Not present)",
        "ui.map.badge_active": "Active",
        "ui.map.badge_inactive": "Inactive",
        "ui.map.badge_invalid": "Invalid",
        "ui.map.badge_empty": "Slot empty",
        "ui.map.badge_mat": "Material: {mat}",
        "ui.map.badge_dup": "Duplicate T{slot}",
        "ui.map.unused_badge": "Unused (0.0 g)",
        "ui.map.sum_err_badge": "{count} Mapping Error(s)",
        "ui.map.sum_err_text": "Please check empty or invalid target slots before printing.",
        "ui.map.sum_warn_badge": "{count} Warning(s)",
        "ui.map.sum_warn_text": "Review material warnings before printing.",
        "ui.map.sum_ok_badge": "Mapping consistent",
        "ui.map.sum_ok_text": "{count} active slicer filament(s) assigned.",
        "ui.map.sum_empty_active": "No active filament usage found in G-code — enable 'Show unused extruders' or load a sliced G-code.",
        "ui.sec5.title": "5. Pre-Print Options & G-Code Workflow",
        "ui.sec5.opt_g31_title": "Keep Bed Leveling (G31) enabled",
        "ui.sec5.opt_g31_sub": "When unchecked, reversibly replaces G31 with ; G31 ; skipped by ACE Plugin",
        "ui.sec5.opt_skip_purge_title": "Skip Purge for Already Loaded Tool (SKIP_PURGE_FOR_ALREADY_LOADED_TOOL)",
        "ui.sec5.opt_skip_purge_sub": "Sets SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 or 0 on the G9111 startup macro",
        "ui.sec5.opt_cutpost_title": "Cut Tip after Print (PRINT_END CUT_TIP)",
        "ui.sec5.opt_cutpost_sub": "Sets PRINT_END CUT_TIP=1 or 0 at the end of print",
        "ui.sec5.opt_force_title": "Force Initial Purge",
        "ui.sec5.opt_force_sub": "Forces flush_volumes_vector purge on the first T<n>",
        "ui.sec5.opt_start_title": "Start print immediately after upload",
        "ui.sec5.opt_start_sub": "Active only when printer is in standby and Safety Gate is OK",
        "ui.sec5.path_label": "G-code Path:",
        "ui.sec5.path_placeholder": "/path/to/file.gcode",
        "ui.sec5.sim_title": "Read-Only Simulation (File unchanged · 0 Hardware commands)",
        "ui.sec5.btn_parse": "📄 Read Filaments from Header",
        "ui.sec5.btn_preview": "🔎 Offline Flush Analysis (Preview)",
        "ui.sec5.write_title": "Write Post-Processing & Moonraker Upload (incl. Safety Gate)",
        "ui.sec5.btn_patch": "⚙️ Patch Locally (Post-Process)",
        "ui.sec5.btn_upload": "🚀 Patch & Moonraker Upload",
        "ui.sec6.no_analysis": "No analysis yet",
        "ui.sec6.mode_preview": "Read-Only Preview",
        "ui.sec6.mode_pp": "Post-Processed",
        "ui.sec6.vec_title": "Initial Flush Vector (flush_volumes_vector)",
        "ui.sec6.col_extruder": "Extruder",
        "ui.sec6.col_unload": "Unload (mm³)",
        "ui.sec6.col_load": "Load (mm³)",
        "ui.sec6.col_eff": "Effective (mm³)",
        "ui.sec6.col_purge": "Purge (mm)",
        "ui.sec6.mat_title": "N×N Flush Matrix Heatmap (Slicer From → To)",
        "ui.sec6.events_summary": "Toolchange Sequence & Injected ACE_SET_PURGE_AMOUNT Commands",
        "ui.sec6.events_count": "{count} Events",
        "ui.sec6.ev_col_tc": "Slicer Toolchange",
        "ui.sec6.ev_col_slot": "ACE Slot",
        "ui.sec6.ev_col_raw": "Raw (mm³)",
        "ui.sec6.ev_col_eff": "Effective (mm³)",
        "ui.sec6.ev_col_len": "Purge Length (mm)",
        "ui.flush.empty_vec": "Select a G-code file and start “Offline Flush Analysis (Preview)” or “Patch Locally”.",
        "ui.flush.empty_mat": "No matrix loaded.",
        "ui.flush.no_vec": "No flush_volumes_vector",
        "ui.flush.no_square_mat": "No valid square matrix",
        "ui.flush.no_events": "No toolchange events found in G-code.",
        "ui.flush.kpi_mul": "Multiplier",
        "ui.flush.kpi_diam": "Diameter",
        "ui.flush.kpi_tc": "Toolchanges",
        "ui.flush.kpi_total": "Total Purge",
        "ui.flush.kpi_g9111": "G9111 Start Tool",
        "ui.flush.kpi_g31_cut": "G31 / CUT_TIP",
        "ui.flush.g31_active": "Active",
        "ui.flush.g31_skipped": "Skipped ({count})",
        "ui.flush.cut_fmt": "SkipPurge:{skip} / EndCut:{cut}",
        "ui.flush.yes": "Yes",
        "ui.flush.no": "No",
        "ui.flush.init": "Init",
        "ui.log.title": "Status & Audit Log",
        "ui.log.clear_btn": "Clear",
        "ui.log.initial": "Ready.",
        "ui.log.cleared": "Log cleared.",
        "ui.nav.settings": "⚙️ Connection",
        "ui.nav.diagnostics": "📊 Flush & Audit",
        "ui.nav.log": "📋 Log",
        "ui.bar.dryer_label": "ACE Dryer:",
        "ui.bar.job_title": "Print Job & Pre-Print Send",
        "ui.bar.btn_prepare": "🚀 Prepare & Send Print...",
        "ui.bar.btn_open_modal": "🎨 Filament Mapping & Send",
        "ui.modal.preprint.title": "Send Print Job — Filament Mapping & Pre-Print Check",
        "ui.modal.preprint.subtitle": "Assign Slicer extruders to physical ACE Pro slots (T0–T7) and verify material compatibility before sending.",
        "ui.modal.close": "✕ Close",
        "ui.modal.kpi.time_layers": "Est. Time & Layers",
        "ui.modal.kpi.filament_mat": "Filament Usage & Weight",
        "ui.modal.feedback.loading_slicer": "Checking active sliced G-code from OrcaSlicer...",
        "ui.modal.feedback.no_gcode_action": "No G-code file loaded yet. Slice a plate or click 'Browse .gcode' first.",
        "ui.modal.job.no_file": "No G-code file selected",
        "ui.modal.job.time": "Est. Time: {time}",
        "ui.modal.job.weight": "Filament: {weight} g ({length} m)",
        "ui.modal.job.layers": "Layers: {layers}",
        "ui.modal.job.toolchanges": "Toolchanges: {tc} ({purge} mm purge)",
        "ui.modal.cards.title": "Filament-to-Slot Mapping (Slicer Extruder → Physical ACE Slot)",
        "ui.modal.cards.show_unused": "Show unused extruders",
        "ui.modal.card.slicer_ext": "Slicer Extruder T{tool}",
        "ui.modal.card.amount": "{grams} g · {meters} m",
        "ui.modal.card.unused_amount": "Unused in G-code (0.0 g)",
        "ui.modal.card.target_label": "Target ACE Slot",
        "ui.modal.card.match_exact": "✓ Material Match ({mat})",
        "ui.modal.card.mismatch_badge": "⚠️ Material mismatch: Slicer {slicer_mat} vs. ACE {ace_mat}",
        "ui.modal.card.empty_badge": "⛔ Target ACE slot T{slot} is empty",
        "ui.modal.card.shared_badge": "ℹ️ Shared slot: ACE T{slot}",
        "ui.modal.thumb.alt": "G-code preview thumbnail",
        "ui.modal.thumb.fallback_title": "No embedded G-code thumbnail found",
        "ui.modal.thumb.mode_mapped": "🎨 Mapped",
        "ui.modal.thumb.mode_original": "📐 Original",
        "ui.modal.thumb.mode_title": "Toggle live preview between mapped ACE Pro colors and original Slicer colors",
        "ui.modal.thumb.zoom_title": "Click to inspect full-size preview (zoom)",
        "ui.modal.thumb.zoom_heading": "Model Color Preview (Zoom)",
        "ui.modal.thumb.zoom_close": "✕ Close (Esc)",
        "ui.modal.thumb.zoom_dismiss_hint": "Click preview image, backdrop, or press Esc to return to Pre-Print dialog.",
        "ui.modal.mismatch_gate.title": "⚠️ Material Mismatch Detected",
        "ui.modal.mismatch_gate.desc": "One or more mapped ACE Pro slots contain a different material than configured in OrcaSlicer. Please reassign the slot or explicitly confirm below to proceed.",
        "ui.modal.mismatch_gate.checkbox": "I acknowledge the material mismatch(es) and confirm printing with the selected ACE Pro slots.",
        "ui.modal.options.title": "Pre-Print Hardware & Purge Options",
        "ui.modal.opt.g31": "Bed Leveling (G31)",
        "ui.modal.opt.skip_purge": "Skip Purge for Already Loaded Tool (SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1)",
        "ui.modal.opt.cut_post": "Cut Tip after Print (PRINT_END CUT_TIP=1)",
        "ui.modal.opt.force_purge": "Force Initial Purge",
        "ui.modal.btn.inspect_flush": "🔎 Inspect Flush Volume & Post-Processing Preview",
        "ui.modal.btn.cancel": "Cancel",
        "ui.modal.btn.apply_local": "⚙️ Apply & Patch Locally",
        "ui.modal.btn.upload_only": "⬆️ Upload Only",
        "ui.modal.btn.send_print": "🚀 Upload & Start Print",
        "ui.modal.btn.load_slicer": "🔄 Load from Slicer",
        "ui.modal.btn.browse_gcode": "📁 Browse .gcode",
        "ui.modal.btn.clear_gcode_title": "Clear loaded G-code",
        "ui.modal.stale.banner": "⚠️ Loaded G-code '{file}' does not match active OrcaSlicer model '{model}'. If you only sliced in Preview, export the G-code (Ctrl+G) or click 'Browse .gcode'.",
        "ui.modal.stale.clear_btn": "× Unload Stale G-code",
        "ui.modal.btn.slot_select_aria": "Select physical ACE Pro slot for extruder T{tool}",
        "ui.modal.slot.opt_empty": "Empty",
        "ui.modal.slot.opt_unconf": "Ready",
        "host.slicer_gcode_loaded": "Loaded sliced G-code '{file}' ({tc} toolchanges) — ready for mapping.",
        "host.slicer_gcode_not_found": "No staged G-code found (OrcaSlicer's orca.host API is read-only and cannot export plates on demand). Slice/export once or click 'Browse .gcode'.",
        "host.gcode_browsed_loaded": "Loaded local G-code '{file}' ({tc} toolchanges).",
        "host.gcode_cleared": "Cleared loaded G-code selection. Ready for next sliced export or local file.",
        "host.gcode_read_error": "Failed to load G-code file: {error}",
        "host.auto_opened_preprint": "New sliced plate detected — Pre-Print Send dialog updated with '{file}'.",
        "host.notify_sliced_ready": "G-code '{file}' ready ({tc} toolchanges) — switch to Kobra S1 ACE Pro to send.",
        "host.notify_open_action": "Open Pre-Print Dialog",
        "gate.block.mat_mismatch_unconfirmed": "Material mismatch detected between Slicer extruder and ACE Pro slot — explicit confirmation required.",
    },
    "de": {
        # Header & Theme / Language Controls
        "app.title": "Anycubic Kobra S1 — ACE Pro Steuerung",
        "app.subtitle": "Moonraker REST • 8-Slot Multi-Color Hub • Flush-Volumen & Purge-Engine • G-Code Post-Processor",
        "header.language.aria": "Sprache der Benutzeroberfläche wählen",
        "header.language.en": "English",
        "header.language.de": "Deutsch",
        "header.theme.aria": "Farbschema wählen",
        "header.theme.auto": "Auto",
        "header.theme.light": "Hell",
        "header.theme.dark": "Dunkel",
        "header.conn.offline": "Offline",
        "header.conn.online": "Moonraker Online",
        "header.conn.offline_ace": "Moonraker Offline",
        "header.conn.unreachable": "Moonraker Nicht erreichbar",

        # Executive Status Strip
        "exec.printer.label": "Drucker & ACE Status",
        "exec.printer.not_queried": "Noch nicht abgefragt",
        "exec.printer.printing_badge": "DRUCKT ({state})",
        "exec.printer.state_badge": "Status: {state}",
        "exec.printer.ace_ready": "ACE Bereit",
        "exec.printer.ace_prefix": "ACE: {status}",
        "exec.printer.slots_ready": "{ready}/{total} Slots bereit",
        "exec.printer.temp": "Düse {extruder}°C • Bett {bed}°C",
        "exec.dryer.label": "Aktiver Trockner",
        "exec.dryer.standby": "Standby",
        "exec.dryer.off_sub": "Trockner deaktiviert • 0 °C Ziel",
        "exec.dryer.active": "AKTIV • {temp} °C",
        "exec.dryer.remain": "Restlaufzeit: {mins} min",
        "exec.mapping.label": "Mapping-Bereitschaft",
        "exec.mapping.single": "Single / Standard",
        "exec.mapping.no_project": "Kein Slicer-Projekt geladen",
        "exec.mapping.conflicts": "{count} Konflikte",
        "exec.mapping.conflicts_sub": "Leere oder unkonfigurierte Ziel-Slots",
        "exec.mapping.warnings": "{count} Hinweise",
        "exec.mapping.warnings_sub": "Doppelbelegung oder Material-Abweichung",
        "exec.mapping.ready": "{count} Extruder bereit",
        "exec.mapping.ready_sub": "Alle aktiven Extruder gültig zugeordnet",
        "exec.flush.label": "Flush & Audit Status",
        "exec.flush.awaiting": "Wartet auf Analyse",
        "exec.flush.awaiting_sub": "Flush-Matrix / Vorschau starten",
        "exec.flush.incomplete": "Unvollständig",
        "exec.flush.warnings": "Hinweise ({count})",
        "exec.flush.ok": "Post-Process OK",
        "exec.flush.sub": "{tc} Toolchanges • Mul ×{mul}",
        "exec.safety.label": "Sicherheits-Sperre",
        "exec.safety.armed": "Bereit (Idle)",
        "exec.safety.armed_sub": "Hardware-Aktionen freigegeben",
        "exec.safety.locked": "DRUCK-SPERRE AKTIV",
        "exec.safety.locked_sub": "Hardware & Druckstart geschützt",

        # Safety Banner
        "safety.banner.title": "Druck-Sicherheitssperre aktiv:",
        "safety.banner.body": "Der Kobra S1 führt gerade einen Druckauftrag aus ({state}). Alle Hardware-Befehle (Laden, Entladen, CUT_TIP, Bed Leveling, Trockner) sowie „Upload & Druck starten“ sind gesperrt, um den laufenden Druck zu schützen.",

        # Section 1: Moonraker Connection
        "sec1.title": "Moonraker-Verbindung",
        "sec1.subtitle": "REST-Endpunkt & Status-Abfrage",
        "sec1.badge": "Moonraker API",
        "sec1.url.label": "Moonraker URL (Kobra S1 IP:Port)",
        "sec1.url.placeholder": "http://192.168.8.80:7125",
        "sec1.btn.test": "Verbindung testen",
        "sec1.btn.refresh": "ACE Status abrufen",
        "sec1.btn.save": "Einstellungen speichern",

        # Section 2: ACE Pro Slots & Dryer
        "sec2.title": "ACE Pro Live Slots (T0–T7) & Trockner",
        "sec2.subtitle": "Hardware-Spulenstatus, RFID & Filament-Trockner",
        "sec2.badge": "8 Slots",
        "sec2.dryer.title": "ACE Pro Filament-Trockner",
        "sec2.dryer.temp": "Ziel-Temp (°C)",
        "sec2.dryer.duration": "Dauer (min)",
        "sec2.dryer.start": "Trockner starten",
        "sec2.dryer.stop": "Stopp",
        "slot.status.ready": "BEREIT",
        "slot.status.empty": "LEER",
        "slot.badge.rfid": "RFID",
        "slot.badge.manual": "Manuell",
        "slot.empty_spool": "Leere Spule",
        "slot.unknown": "Unbekannt",
        "slot.no_filament": "Kein Filament erkannt",
        "slot.sku": "SKU: {sku}",
        "slot.temp_range": "{min}–{max}°C",
        "slot.manual_assign": "Manuelle Zuweisung",
        "slot.btn.load": "Laden (T{slot})",
        "slot.btn.unload": "Entladen",
        "slot.btn.load.locked_title": "Während eines aktiven Drucks gesperrt",
        "slot.btn.load.empty_title": "Slot ist leer",
        "slot.btn.load.title": "Filament aus T{slot} laden",
        "slot.btn.unload.title": "Filament aus T{slot} entladen",

        # Section 3: Color Mapping Workbench
        "sec3.title": "Color Mapping Workbench",
        "sec3.subtitle": "OrcaSlicer Extruder → Physischer ACE Pro Slot",
        "sec3.btn.sync": "Slicer-Filamente synchronisieren",
        "sec3.btn.automatch": "Auto-Match (ΔE + Material)",
        "sec3.btn.reset": "1:1 Reset",
        "map.col.extruder": "Slicer-Extruder",
        "map.col.target": "ACE Ziel-Slot",
        "map.col.physical": "Physische ACE-Spule",
        "map.col.status": "Validierung & ΔE",
        "map.extruder.Active": "Aktiv",
        "map.extruder.Standby": "Standby",
        "map.extruder.fallback_name": "Extruder {index}",
        "map.option.empty": "LEER",
        "map.phys.unconfigured": "Unkonfiguriert",
        "map.phys.empty_slot": "Leerer Slot",
        "map.phys.not_present": "Slot T{slot} nicht vorhanden",
        "map.status.not_present": "Slot fehlt",
        "map.status.empty_error": "FEHLER: Slot leer",
        "map.status.dup_warn": "Doppelt T{slot}",
        "map.status.mat_warn": "Mat. {slicer}≠{ace}",
        "map.status.unconf_warn": "Unkonfiguriert",
        "map.status.ok": "OK",
        "map.summary.conflicts": "{count} Mapping-Konflikt(e) erkannt — Upload wird blockiert, bis diese behoben sind.",
        "map.summary.warnings": "{count} Hinweis(e) — bitte Mapping vor dem Druck prüfen.",
        "map.summary.valid": "Alle aktiven Extruder sind gültig auf bereite ACE-Slots zugeordnet.",

        # Section 4: Print Preparation & Hardware Macros
        "sec4.title": "Druckvorbereitung & Hardware-Makros",
        "sec4.subtitle": "G31 Bed Leveling, CUT_TIP & Initial-Purge-Steuerung",
        "sec4.badge": "Klipper-Makros",
        "sec4.bed_leveling.title": "Bed Leveling (G31) vor dem Druck ausführen",
        "sec4.bed_leveling.sub": "Deaktiviert kommentiert G31 im G9111-Startblock aus",
        "sec4.skip_purge.title": "Spülen bei bereits geladenem Tool überspringen",
        "sec4.skip_purge.sub": "Setzt SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 oder 0 bei G9111",
        "sec4.cut_post.title": "Cut Tip nach dem Druck (PRINT_END CUT_TIP)",
        "sec4.cut_post.sub": "Setzt PRINT_END CUT_TIP=1 oder 0 am Druckende",
        "sec4.force_purge.title": "Initial-Purge beim Start-Slot erzwingen",
        "sec4.force_purge.sub": "Ignoriert SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 beim ersten T<n>",
        "sec4.direct.label": "Direkte Klipper-Makro-Ausführung (während des Drucks gesperrt):",
        "sec4.btn.cut_now": "CUT_TIP jetzt ausführen",
        "sec4.btn.g31_now": "Bed Leveling (G31)",
        "sec4.btn.mesh_now": "BED_MESH_CALIBRATE",

        # Section 5: G-code Post-Processing & Moonraker Upload
        "sec5.title": "G-Code Post-Processing & Moonraker Upload",
        "sec5.subtitle": "Reversible Purge-Injektion, Audit-Header & Druckstart-Gate",
        "sec5.badge": "Pipeline",
        "sec5.path.label": "Lokaler G-Code-Dateipfad (für manuellen Test / Direkt-Upload)",
        "sec5.path.placeholder": "/pfad/zu/plate_1.gcode",
        "sec5.btn.analyze": "Flush-Matrix & Analyse",
        "sec5.btn.preview": "G-Code Vorschau (Diff)",
        "sec5.btn.postprocess": "G-Code nachbearbeiten",
        "sec5.btn.upload_only": "Nur Upload",
        "sec5.btn.upload_start": "Upload & Druck starten",
        "sec5.preview.title": "G-Code Transformations-Vorschau (Original vs. Post-Processed)",
        "sec5.preview.close": "Schließen",

        # Section 6: Flush Volume Monitoring & Post-Processing Audit
        "sec6.title": "6. Flush Volume Monitoring & Post-Processing Audit",
        "sec6.subtitle": "OrcaSlicer Flush-Matrix (Row-Major), Vector-Übersicht & Toolchange-Timeline",
        "flush.status.not_analyzed": "Noch nicht analysiert",
        "flush.kpi.multiplier": "Flush-Multiplier",
        "flush.kpi.diameter": "Filament-Ø / Querschnitt",
        "flush.kpi.toolchanges": "Toolchanges / Purge-Befehle",
        "flush.kpi.start_tool": "G9111 Start-Tool → ACE-Slot",
        "flush.kpi.bed_cut": "G31 / G9111 / PRINT_END",
        "flush.kpi.bed_active": "G31 Aktiv",
        "flush.kpi.bed_skipped": "G31 Aus ({count}x)",
        "flush.kpi.skip_purge": "SkipPurge",
        "flush.kpi.cut_post": "EndCut",
        "flush.kpi.cut_none": "Kein Cut",
        "flush.gate.ready": "Upload bereit ({status})",
        "flush.gate.blocked": "Upload gesperrt: {reasons}",
        "flush.matrix.title": "Flush-Volumen-Matrix (Von → Nach in mm³ & Purge-Länge in mm)",
        "flush.matrix.empty": "G-Code-Datei laden und „Flush-Matrix & Analyse“ klicken, um die N×N Matrix darzustellen.",
        "flush.matrix.no_data": "Keine flush_volumes_matrix im G-Code vorhanden (Single-Extruder oder fehlende Kommentare).",
        "flush.matrix.corner": "Von \\ Nach",
        "flush.vector.title": "Initial-Flush-Vektor Übersicht (flush_volumes_vector)",
        "flush.vector.empty": "Zeigt die Initial-Purge-Volumina (N oder 2×N Unload/Load) je Extruder.",
        "flush.vector.no_data": "Keine flush_volumes_vector-Daten vorhanden.",
        "flush.vector.col.extruder": "Extruder",
        "flush.vector.col.unload": "Unload (mm³)",
        "flush.vector.col.load": "Load (mm³)",
        "flush.vector.col.raw_sum": "Roh-Summe (mm³)",
        "flush.vector.col.effective": "Effektiv ×{mul} (mm³)",
        "flush.vector.col.purge": "Initial-Purge-Länge (mm)",
        "flush.timeline.title": "Toolchange- & Purge-Timeline",
        "flush.timeline.empty": "Noch keine Toolchanges ausgewertet.",
        "flush.timeline.no_events": "Keine T<n> Toolchange-Befehle im ausführbaren G-Code gefunden.",
        "flush.timeline.col.idx": "#",
        "flush.timeline.col.slicer": "Slicer-Tool (Alt → Neu)",
        "flush.timeline.col.ace": "ACE Ziel-Slot",
        "flush.timeline.col.raw": "Roh-Volumen (mm³)",
        "flush.timeline.col.eff": "Effektiv (mm³)",
        "flush.timeline.col.diam": "Ø (mm)",
        "flush.timeline.col.cmd": "Generierter Klipper-Befehl",
        "flush.timeline.initial": "Initial → T{tool}",

        # Activity Log Footer
        "log.title": "System- & Aktivitätsprotokoll",
        "log.clear": "Protokoll leeren",
        "log.ready": "[Bereit] ACE Pro Plugin UI initialisiert.",
        "log.cleared": "[Bereit] Protokoll geleert.",
        "log.no_host": "[Simulation / Vorschau] OrcaSlicer Host-Bridge nicht aktiv — gesendete Nachricht: {msg}",

        # Validation, Safety & Backend Messages
        "val.num.invalid": "Ungültiger Zahlenwert '{token}' in {field}; ersetzt durch {default}.",
        "val.num.non_finite": "Nicht-endlicher Wert '{token}' in {field}; ersetzt durch {default}.",
        "val.num.below_min": "Ungültiger Wert {val} (< {min}) in {field}; auf {default} begrenzt.",
        "val.map.not_present": "Slicer-Extruder T{slicer_tool} ist auf nicht vorhandenen ACE-Slot T{mapped} (verfügbar: 0..{max_slot}) gemappt.",
        "val.map.empty_slot": "Slicer-Extruder T{slicer_tool} ist auf den leeren ACE-Slot T{mapped} gemappt.",
        "val.map.mat_mismatch": "Material-Abweichung bei Extruder T{slicer_tool} ({slicer_mat}) -> ACE-Slot T{mapped} ({ace_mat}).",
        "val.map.unconfigured": "ACE-Slot T{mapped} (für Extruder T{slicer_tool}) ist unkonfiguriert (kein RFID, Material leer, #000000).",
        "val.map.duplicate": "Doppelte Slot-Belegung: Aktive Extruder {extruders} zeigen beide auf ACE-Slot T{phys_tool}.",
        "val.map.shared": "Geteilter ACE-Slot: Aktive Extruder {extruders} nutzen gemeinsam ACE-Slot T{phys_tool}.",
        "val.gcode.mul_non_finite": "Nicht-endlicher flush_multiplier '{raw}'; Fallback auf 1.0.",
        "val.gcode.mul_negative": "Negativer flush_multiplier ({val}); auf 0.0 begrenzt.",
        "val.gcode.mul_invalid": "Ungültiger flush_multiplier '{raw}'; Fallback auf 1.0.",
        "val.gcode.diam_invalid": "Ungültiger Filamentdurchmesser ({diam}); Fallback auf Standard ({default} mm).",
        "val.gcode.matrix_empty": "flush_volumes_matrix ist leer.",
        "val.gcode.matrix_not_square": "Ungültige flush_volumes_matrix-Größe ({count} Elemente; keine N×N Quadratmatrix).",
        "val.gcode.vector_mismatch": "flush_volumes_vector-Größe ({vec_len}) stimmt nicht mit {n_tools} Extrudern überein (erwartet {n_tools} oder {double_tools}).",
        "val.gcode.tool_exceeds": "Tool-ID T{tool} überschreitet die erkannte Extruderanzahl ({max_tools}).",
        "val.gcode.vector_missing_init": "flush_volumes_vector fehlt für Initial-Flush nach T{tool}.",
        "val.gcode.matrix_missing_tc": "flush_volumes_matrix fehlt oder ist ungültig für Toolchange T{old_tool} -> T{new_tool}.",
        "val.gcode.matrix_missing_multi": "Mehrere Extruder erkannt, aber flush_volumes_matrix fehlt oder ist ungültig.",
        "map.warn.order_reset": "Extruder-Anzahl/Reihenfolge geändert ({old_len} -> {new_len}). Color Mapping wurde auf 1:1 zurückgesetzt.",
        "safety.lock.error": "Sicherheits-Sperre: Aktion '{action}' blockiert, da ein Druck aktiv ist (Status: {state}).",
        "gate.block.flush_incomplete": "Flush-Berechnung unvollständig (FLUSH_DATA_INCOMPLETE). Bitte flush_volumes_matrix / flush_volumes_vector prüfen.",
        "gate.block.printing_start": "Druckstart nicht möglich: Drucker ist derzeit aktiv ({state}).",

        # Host Log Messages
        "host.settings_saved": "Einstellungen gespeichert ({url})",
        "host.lang_saved": "Sprache auf {lang_name} umgestellt.",
        "host.sync_filaments": "{count} Slicer-Filamente synchronisiert.",
        "host.automatch_done": "Auto-Match abgeschlossen (ΔE Farbdistanz + Material-Priorität).",
        "host.reset_done": "Color Mapping auf 1:1 Standard zurückgesetzt.",
        "host.conn_ok": "Moonraker verbunden! Server: {version}",
        "host.conn_fail": "Moonraker nicht erreichbar unter {url}",
        "host.gcode_sent": "G-Code ausgeführt: {cmd}",
        "host.slot_loaded": "Slot T{slot} geladen ({cmd})",
        "host.slot_unloaded": "Slot T{slot} entladen ({cmd})",
        "host.dryer_started": "Dryer gestartet: {temp}°C für {duration} min",
        "host.dryer_started_unit": "Dryer gestartet ({unit}): {temp}°C für {duration} min ({cmd})",
        "host.dryer_stopped": "Dryer gestoppt",
        "host.dryer_stopped_unit": "Dryer gestoppt ({unit}): {cmd}",
        "host.no_gcode_loaded": "Keine G-Code-Datei geladen. Bitte zuerst in OrcaSlicer slicen oder '.gcode wählen' nutzen.",
        "host.pp_modal_ok": "Post-Processing für '{file}' abgeschlossen ({tc} Toolchanges, {len} mm Purge) — lokal gespeichert.",
        "host.upload_modal_ok": "'{file}' zu {url} hochgeladen ({mode}, {len} mm Purge).",
        "host.upload_mode_print": "Druck gestartet",
        "host.upload_mode_only": "Nur Upload",
        "host.dryer_invalid_temp": "Ungültige Dryer-Temperatur ({temp}°C); auf 20–70°C begrenzt.",
        "host.err_no_gcode_analyze": "Bitte einen gültigen G-Code-Dateipfad zur Analyse angeben.",
        "host.analyze_ok": "Flush-Analyse abgeschlossen ({status}): {tc} Toolchanges, Matrix={mat}, Vector={vec}.",
        "host.err_no_gcode_preview": "Bitte einen gültigen G-Code-Dateipfad für die Vorschau angeben.",
        "host.preview_ok": "Vorschau generiert: {tc} Toolchanges, Status={status}",
        "host.err_no_gcode_pp": "Bitte einen gültigen G-Code-Dateipfad angeben.",
        "host.pp_ok": "Post-Processing abgeschlossen ({status}): {tc} Toolchanges, G9111 tool={g9111}",
        "host.err_no_gcode_upload": "Bitte einen gültigen G-Code-Dateipfad für den Upload angeben.",
        "host.upload_blocked": "Upload blockiert: {reasons}",
        "host.upload_ok": "Post-Processing ({tc} Toolchanges) & Upload erfolgreich: {res}",
        "host.error_prefix": "Fehler: {error}",
        "gate.block.no_info": "Kein gültiges Post-Processing-Ergebnis vorhanden.",
        "gate.block.multi_incomplete": "Mehrere Extruder verwendet, aber Flush-Matrix/Vector unvollständig.",
        "host.ready": "ACE Pro Plugin bereit.",
        "host.sync_none": "Keine aktiven Filament-Presets von orca.host.preset_bundle() erhalten.",
        "host.status_updated": "Moonraker & ACE Pro Status aktualisiert.",
        "host.hw_busy": "Eine Hardware-Aktion läuft bereits — bitte warten.",
        "host.macro_sent": "Makro '{macro}' an Moonraker gesendet.",
        "host.slot_active_unloaded": "Aktives Tool entladen (ACE_CHANGE_TOOL TOOL=-1).",
        "host.slot_fed": "Slot T{slot}: {length}mm vorgeschoben (ACE_FEED).",
        "host.slot_retracted": "Slot T{slot}: {length}mm zurückgezogen (ACE_RETRACT).",
        "host.slot_unknown_action": "Unbekannte Slot-Aktion: {action}",
        "host.file_not_found": "G-Code Datei nicht gefunden: {path}",
        "host.parse_reset_note": " (Color Mapping wegen geänderter Filament-Reihenfolge zurückgesetzt)",
        "host.parse_ok": "{count} Slicer-Filamente aus '{file}' geladen.{note}",
        "host.automatch_warns": " | Hinweise: {warns}",
        "host.automatch_log": "Auto-Match abgeschlossen: {matched}{warns}",
        "host.preview_log": "Offline Preview ({file}): Status={status}, {tc} Toolchanges, Gesamt-Purge={len} mm ({vol} mm³) [Datei unverändert]",
        "host.pp_log": "Post-Processing abgeschlossen ({file}): Status={status}, {tc} Toolchanges, multiplier={mul}, bedLeveling={bed}, Gesamt-Purge={len} mm",
        "host.upload_gate_blocked": "{base} | Upload blockiert (Safety Gate): {reasons}",
        "host.hw_busy_upload": "Eine Hardware-Aktion läuft bereits — Upload & Druckstart abgebrochen.",
        "host.upload_done": "{base} | Upload zu {url} erfolgreich.",
        "host.upload_done_started": "{base} | Upload zu {url} erfolgreich (Druck gestartet).",
        "host.msg_error": "Fehler bei '{msg_type}': {error}",
        "host.status_query_failed": "{base} | Moonraker Status-Abfrage fehlgeschlagen: {error}",
        "ui.brand.title": "Anycubic Kobra S1 — Vanilla Klipper + ACE Pro",
        "ui.brand.sub": "OrcaSlicer 2.5.x Color Mapping, Flush2Len & Moonraker Workbench",
        "ui.conn.connecting": "Moonraker: Verbinde...",
        "ui.conn.connected": "Verbunden ({model})",
        "ui.conn.disconnected": "Nicht verbunden",
        "ui.printer.standby": "Drucker: Standby",
        "ui.printer.state": "Drucker: {state}",
        "ui.header.refresh": "🔄 Aktualisieren",
        "ui.safety.banner_prefix": "⚠️ Druck aktiv (",
        "ui.safety.banner_suffix": ") — Alle Hardware-, Motion-, Load/Unload- und Direktdruck-Befehle sind zum Schutz des laufenden Drucks gesperrt.",
        "ui.safety.badge": "Hardware-Sperre aktiv",
        "ui.exec.conn.kicker": "Moonraker & Drucker",
        "ui.exec.conn.online": "Online ({state})",
        "ui.exec.conn.offline": "Offline / Bereit",
        "ui.exec.tool.kicker": "Aktiver Toolhead",
        "ui.exec.tool.none": "Keins (-1)",
        "ui.exec.tool.sub": "ACE Temp: {temp} · Lüfter: {fan}",
        "ui.exec.dryer.kicker": "ACE Pro Dryer",
        "ui.exec.dryer.standby": "Standby",
        "ui.exec.dryer.active": "Aktiv ({temp}°C)",
        "ui.exec.dryer.remain": "Restzeit: {mins} min",
        "ui.exec.dryer.inactive": "Keine aktive Trocknung",
        "ui.exec.gcode.kicker": "Aktiver G-Code & Purge",
        "ui.exec.gcode.none": "Keine Datei",
        "ui.exec.gcode.ready_sub": "Bereit für Offline Preview / Patch",
        "ui.exec.gcode.select_sub": "Wähle G-Code für Preview / Patch",
        "ui.exec.gcode.stats": "{tc} Wechsel · {len} mm ({vol} mm³)",
        "ui.sec1.title": "1. Moonraker Verbindung",
        "ui.sec1.offline_badge": "Offline-Analyse unabhängig möglich",
        "ui.sec1.url_label": "URL:",
        "ui.sec1.port80": "Port 80 Fallback",
        "ui.sec1.apikey_label": "API Key:",
        "ui.sec1.apikey_placeholder": "Optional X-Api-Key",
        "ui.sec1.connect_btn": "Verbinden & Status laden",
        "ui.sec1.meta_initial": "ACE Status: — | Aktives Tool: — | Temp: —",
        "ui.sec1.meta_live": "ACE Status: {status} | Aktives Tool: {tool} | Temp: {temp}°C | Lüfter: {fan} RPM{print_label}",
        "ui.sec1.meta_print": " | Druck: {state}",
        "ui.sec2.title": "2. ACE Pro Trockner-Steuerung",
        "ui.sec2.dryer_stop_badge": "Dryer: Stop",
        "ui.sec2.dryer_active_badge": "Dryer: {temp}°C ({mins} min übrig)",
        "ui.sec2.unit_label": "Einheit:",
        "ui.sec2.unit_all": "Beide (Alle ACE)",
        "ui.sec2.unit_ace1": "ACE 1 (T0–T3)",
        "ui.sec2.unit_ace2": "ACE 2 (T4–T7)",
        "ui.sec2.unit_select_aria": "ACE Pro Trockner-Einheit wählen",
        "ui.sec2.btn_unload": "⏏️ Unload (T-1)",
        "ui.sec2.temp_label": "Temp (°C):",
        "ui.sec2.dur_label": "Dauer (Min):",
        "ui.sec2.btn_dryer_start": "🔥 Dryer Start",
        "ui.sec2.btn_dryer_stop": "⏹️ Dryer Stop",
        "ui.sec3.title": "3. ACE Pro Live Slot-Bay (T0–T7)",
        "ui.sec3.feed_label": "Feed/Retract (mm):",
        "ui.sec3.btn_live": "🔄 Live-Status",
        "ui.slot.badge_active": "Aktiv",
        "ui.slot.badge_ready": "Bereit",
        "ui.slot.badge_empty": "Leer",
        "ui.slot.no_filament": "Kein Filament",
        "ui.slot.unconfigured": "Unkonfiguriert",
        "ui.slot.rfid_detected": "RFID erkannt",
        "ui.slot.btn_load": "Laden",
        "ui.slot.btn_unload": "Entladen",
        "ui.slot.btn_feed": "+Feed",
        "ui.slot.btn_retr": "-Retr",
        "ui.slot.title_load": "Tool T{tool} laden",
        "ui.slot.title_unload": "Slot T{tool} entladen",
        "ui.sec4.title": "4. Color Mapping Workbench (Slicer → ACE)",
        "ui.sec4.btn_project": "🔄 OrcaSlicer-Projekt",
        "ui.sec4.btn_automatch": "✨ Auto-Match",
        "ui.sec4.btn_reset": "↺ 1:1 Standard",
        "ui.sec4.summary_ready": "Bereit für Filament-Zuordnung.",
        "ui.sec4.col_extruder": "Slicer Extruder",
        "ui.sec4.col_mat_color": "Material & Farbe",
        "ui.sec4.col_mode": "Modus",
        "ui.sec4.col_slot": "Physischer ACE-Slot",
        "ui.map.mode_auto": "Auto-Match",
        "ui.map.mode_manual": "Manuell",
        "ui.map.mode_default": "1:1",
        "ui.map.not_present": "⚠️ T{slot} (Nicht vorhanden)",
        "ui.map.badge_active": "Aktiv",
        "ui.map.badge_inactive": "Inaktiv",
        "ui.map.badge_invalid": "Ungültig",
        "ui.map.badge_empty": "Slot leer",
        "ui.map.badge_mat": "Material: {mat}",
        "ui.map.badge_dup": "Duplikat T{slot}",
        "ui.map.unused_badge": "Unbenutzt (0,0 g)",
        "ui.map.sum_err_badge": "{count} Mapping-Fehler",
        "ui.map.sum_err_text": "Bitte leere oder ungültige Ziel-Slots vor dem Druck prüfen.",
        "ui.map.sum_warn_badge": "{count} Hinweise",
        "ui.map.sum_warn_text": "Material-Hinweise vor dem Druck prüfen.",
        "ui.map.sum_ok_badge": "Mapping konsistent",
        "ui.map.sum_ok_text": "{count} aktive(s) Slicer-Filament(e) zugeordnet.",
        "ui.map.sum_empty_active": "Keine aktive Filament-Nutzung im G-Code gefunden — 'Unbenutzte Extruder anzeigen' aktivieren oder G-Code laden.",
        "ui.sec5.title": "5. Pre-Print-Optionen & G-Code Workflow",
        "ui.sec5.opt_g31_title": "Bed Leveling (G31) aktiv lassen",
        "ui.sec5.opt_g31_sub": "Deaktiviert ersetzt G31 reversibel durch ; G31 ; skipped by ACE Plugin",
        "ui.sec5.opt_skip_purge_title": "Spülen bei bereits geladenem Tool überspringen (SKIP_PURGE_FOR_ALREADY_LOADED_TOOL)",
        "ui.sec5.opt_skip_purge_sub": "Setzt SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 oder 0 im G9111-Startmakro",
        "ui.sec5.opt_cutpost_title": "Cut Tip nach dem Druck (PRINT_END CUT_TIP)",
        "ui.sec5.opt_cutpost_sub": "Setzt PRINT_END CUT_TIP=1 oder 0 am Druckende",
        "ui.sec5.opt_force_title": "Force Initial Purge",
        "ui.sec5.opt_force_sub": "Erzwingt flush_volumes_vector-Spülung beim ersten T<n>",
        "ui.sec5.opt_start_title": "Start Print immediately nach Upload",
        "ui.sec5.opt_start_sub": "Nur aktiv wenn Drucker in Standby und Safety Gate OK ist",
        "ui.sec5.path_label": "G-Code Pfad:",
        "ui.sec5.path_placeholder": "/pfad/zur/datei.gcode",
        "ui.sec5.sim_title": "Lesende Simulation (Datei unverändert · 0 Hardware-Befehle)",
        "ui.sec5.btn_parse": "📄 Filamente aus Header lesen",
        "ui.sec5.btn_preview": "🔎 Offline Flush-Analyse (Preview)",
        "ui.sec5.write_title": "Schreibendes Post-Processing & Moonraker Upload (inkl. Safety Gate)",
        "ui.sec5.btn_patch": "⚙️ Lokal patchen (Post-Process)",
        "ui.sec5.btn_upload": "🚀 Patchen & Moonraker Upload",
        "ui.sec6.no_analysis": "Noch keine Analyse",
        "ui.sec6.mode_preview": "Read-Only Preview",
        "ui.sec6.mode_pp": "Post-Processed",
        "ui.sec6.vec_title": "Initial Flush Vector (flush_volumes_vector)",
        "ui.sec6.col_extruder": "Extruder",
        "ui.sec6.col_unload": "Unload (mm³)",
        "ui.sec6.col_load": "Load (mm³)",
        "ui.sec6.col_eff": "Effektiv (mm³)",
        "ui.sec6.col_purge": "Purge (mm)",
        "ui.sec6.mat_title": "N×N Flush-Matrix Heatmap (Slicer From → To)",
        "ui.sec6.events_summary": "Toolchange-Sequenz & injizierte ACE_SET_PURGE_AMOUNT Befehle",
        "ui.sec6.events_count": "{count} Events",
        "ui.sec6.ev_col_tc": "Slicer Toolchange",
        "ui.sec6.ev_col_slot": "ACE Slot",
        "ui.sec6.ev_col_raw": "Raw (mm³)",
        "ui.sec6.ev_col_eff": "Effektiv (mm³)",
        "ui.sec6.ev_col_len": "Purge-Länge (mm)",
        "ui.flush.empty_vec": "G-Code wählen und „Offline Flush-Analyse (Preview)“ oder „Lokal patchen“ starten.",
        "ui.flush.empty_mat": "Keine Matrix geladen.",
        "ui.flush.no_vec": "Kein flush_volumes_vector",
        "ui.flush.no_square_mat": "Keine gültige quadratische Matrix",
        "ui.flush.no_events": "Keine Toolchange-Events im G-Code gefunden.",
        "ui.flush.kpi_mul": "Multiplier",
        "ui.flush.kpi_diam": "Durchmesser",
        "ui.flush.kpi_tc": "Toolchanges",
        "ui.flush.kpi_total": "Gesamt-Spülung",
        "ui.flush.kpi_g9111": "G9111 Start-Tool",
        "ui.flush.kpi_g31_cut": "G31 / CUT_TIP",
        "ui.flush.g31_active": "Aktiv",
        "ui.flush.g31_skipped": "Übersprungen ({count})",
        "ui.flush.cut_fmt": "SkipPurge:{skip} / EndCut:{cut}",
        "ui.flush.yes": "Ja",
        "ui.flush.no": "Nein",
        "ui.flush.init": "Init",
        "ui.log.title": "Status- & Audit-Log",
        "ui.log.clear_btn": "Leeren",
        "ui.log.initial": "Bereit.",
        "ui.log.cleared": "Log geleert.",
        "ui.nav.settings": "⚙️ Verbindung",
        "ui.nav.diagnostics": "📊 Flush & Audit",
        "ui.nav.log": "📋 Protokoll",
        "ui.bar.dryer_label": "ACE Trockner:",
        "ui.bar.job_title": "Druckauftrag & Pre-Print Send",
        "ui.bar.btn_prepare": "🚀 Druck vorbereiten & senden...",
        "ui.bar.btn_open_modal": "🎨 Filament-Mapping & Senden",
        "ui.modal.preprint.title": "Druckauftrag senden — Filament-Mapping & Pre-Print-Prüfung",
        "ui.modal.preprint.subtitle": "Slicer-Extruder den physischen ACE Pro Slots (T0–T7) zuordnen und Materialkompatibilität vor dem Senden prüfen.",
        "ui.modal.close": "✕ Schließen",
        "ui.modal.kpi.time_layers": "Druckzeit & Schichten",
        "ui.modal.kpi.filament_mat": "Filament-Verbrauch & Gewicht",
        "ui.modal.feedback.loading_slicer": "Prüfe aktiven geslicten G-Code aus OrcaSlicer...",
        "ui.modal.feedback.no_gcode_action": "Noch kein G-Code geladen. Bitte zuerst slicen oder '.gcode wählen' nutzen.",
        "ui.modal.job.no_file": "Keine G-Code-Datei ausgewählt",
        "ui.modal.job.time": "Druckzeit: {time}",
        "ui.modal.job.weight": "Filament: {weight} g ({length} m)",
        "ui.modal.job.layers": "Schichten: {layers}",
        "ui.modal.job.toolchanges": "Toolchanges: {tc} ({purge} mm Purge)",
        "ui.modal.cards.title": "Filament-zu-Slot-Zuordnung (Slicer-Extruder → Physischer ACE-Slot)",
        "ui.modal.cards.show_unused": "Unbenutzte Extruder anzeigen",
        "ui.modal.card.slicer_ext": "Slicer-Extruder T{tool}",
        "ui.modal.card.amount": "{grams} g · {meters} m",
        "ui.modal.card.unused_amount": "Im G-Code unbenutzt (0.0 g)",
        "ui.modal.card.target_label": "Ziel-ACE-Slot",
        "ui.modal.card.match_exact": "✓ Material-Match ({mat})",
        "ui.modal.card.mismatch_badge": "⚠️ Material-Abweichung: Slicer {slicer_mat} vs. ACE {ace_mat}",
        "ui.modal.card.empty_badge": "⛔ Ziel-ACE-Slot T{slot} ist leer",
        "ui.modal.card.shared_badge": "ℹ️ Geteilter Slot: ACE T{slot}",
        "ui.modal.thumb.alt": "G-Code-Vorschaubild",
        "ui.modal.thumb.fallback_title": "Kein eingebettetes G-Code-Vorschaubild vorhanden",
        "ui.modal.thumb.mode_mapped": "🎨 Zugewiesen",
        "ui.modal.thumb.mode_original": "📐 Original",
        "ui.modal.thumb.mode_title": "Live-Vorschau zwischen zugewiesenen ACE-Pro-Farben und originalen Slicer-Farben umschalten",
        "ui.modal.thumb.zoom_title": "Klicken, um die Vorschau in voller Größe zu vergrößern (Zoom)",
        "ui.modal.thumb.zoom_heading": "Modell-Farbvorschau (Zoom)",
        "ui.modal.thumb.zoom_close": "✕ Schließen (Esc)",
        "ui.modal.thumb.zoom_dismiss_hint": "Auf Vorschaubild, Hintergrund oder Esc drücken, um zum Pre-Print-Dialog zurückzukehren.",
        "ui.modal.mismatch_gate.title": "⚠️ Material-Abweichung erkannt",
        "ui.modal.mismatch_gate.desc": "Ein oder mehrere zugeordnete ACE Pro Slots enthalten ein anderes Material als in OrcaSlicer konfiguriert. Bitte Slot ändern oder unten explizit bestätigen.",
        "ui.modal.mismatch_gate.checkbox": "Ich bestätige die Material-Abweichung(en) und möchte mit den gewählten ACE Pro Slots fortfahren.",
        "ui.modal.options.title": "Pre-Print Hardware- & Purge-Optionen",
        "ui.modal.opt.g31": "Bed Leveling (G31)",
        "ui.modal.opt.skip_purge": "Spülen bei geladenem Tool überspringen (SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1)",
        "ui.modal.opt.cut_post": "Cut Tip nach dem Druck (PRINT_END CUT_TIP=1)",
        "ui.modal.opt.force_purge": "Force Initial Purge",
        "ui.modal.btn.inspect_flush": "🔎 Flush-Volumen & Post-Processing-Vorschau prüfen",
        "ui.modal.btn.cancel": "Abbrechen",
        "ui.modal.btn.apply_local": "⚙️ Übernehmen & lokal patchen",
        "ui.modal.btn.upload_only": "⬆️ Nur hochladen",
        "ui.modal.btn.send_print": "🚀 Hochladen & Druck starten",
        "ui.modal.btn.load_slicer": "🔄 Aus Slicer laden",
        "ui.modal.btn.browse_gcode": "📁 .gcode wählen",
        "ui.modal.btn.clear_gcode_title": "Geladenen G-Code entfernen",
        "ui.modal.stale.banner": "⚠️ Geladener G-Code '{file}' passt nicht zum aktiven OrcaSlicer-Modell '{model}'. Falls nur in der Vorschau geslict wurde, G-Code exportieren (Strg+G / Ctrl+G) oder '.gcode wählen' nutzen.",
        "ui.modal.stale.clear_btn": "× Veralteten G-Code entladen",
        "ui.modal.btn.slot_select_aria": "Physischen ACE Pro Slot für Extruder T{tool} wählen",
        "ui.modal.slot.opt_empty": "Leer",
        "ui.modal.slot.opt_unconf": "Bereit",
        "host.slicer_gcode_loaded": "Geslicten G-Code '{file}' geladen ({tc} Toolchanges) — bereit für Mapping.",
        "host.slicer_gcode_not_found": "Kein bereitgestellter G-Code gefunden (die orca.host-API von OrcaSlicer ist schreibgeschützt und kann keinen Export anstoßen). Einmal slicen oder '.gcode wählen' nutzen.",
        "host.gcode_browsed_loaded": "Lokalen G-Code '{file}' geladen ({tc} Toolchanges).",
        "host.gcode_cleared": "Geladene G-Code-Auswahl geleert. Bereit für den nächsten Export oder eine lokale Datei.",
        "host.gcode_read_error": "G-Code-Datei konnte nicht geladen werden: {error}",
        "host.auto_opened_preprint": "Neue geslicte Platte erkannt — Pre-Print-Dialog mit '{file}' aktualisiert.",
        "host.notify_sliced_ready": "G-Code '{file}' bereit ({tc} Toolchanges) — zum Senden zu Kobra S1 ACE Pro wechseln.",
        "host.notify_open_action": "Pre-Print-Dialog öffnen",
        "gate.block.mat_mismatch_unconfirmed": "Material-Abweichung zwischen Slicer-Extruder und ACE Pro Slot erkannt — explizite Bestätigung erforderlich.",
    },
}


def normalize_language(lang: Any) -> str:
    """Normalize a language code to a supported language ('en' or 'de'), defaulting to 'en'."""
    if isinstance(lang, str):
        cleaned = lang.strip().lower()
        if cleaned in SUPPORTED_LANGUAGES:
            return cleaned
        if cleaned.startswith("de"):
            return "de"
        if cleaned.startswith("en"):
            return "en"
    return DEFAULT_LANGUAGE


def t(key: str, lang: Optional[str] = None, **params: Any) -> str:
    """
    Translate `key` into `lang` (or global plugin language when omitted).
    Falls back to English (`en`) if a key is missing or empty in the selected language,
    and interpolates `{param}` placeholders safely.
    """
    if lang is None:
        st = globals().get("GLOBAL_STATE")
        resolved_lang = normalize_language(getattr(st, "language", DEFAULT_LANGUAGE)) if st is not None else DEFAULT_LANGUAGE
    else:
        resolved_lang = normalize_language(lang)

    lang_dict = TRANSLATIONS.get(resolved_lang, TRANSLATIONS[DEFAULT_LANGUAGE])
    en_dict = TRANSLATIONS[DEFAULT_LANGUAGE]

    template = lang_dict.get(key)
    if not template:
        template = en_dict.get(key, key)

    if params:
        def _replace(match: re.Match) -> str:
            p_name = match.group(1)
            return str(params[p_name]) if p_name in params else match.group(0)

        return re.sub(r"\{([a-zA-Z0-9_]+)\}", _replace, template)
    return template


# ---------------------------------------------------------------------------
# Color & Parsing Helpers (1:1 compatible with orca_flush_to_purgelength.py)
# ---------------------------------------------------------------------------
def parse_number_list(s: str) -> List[float]:
    """Parse comma/semicolon/whitespace separated floats."""
    return [float(x) for x in re.split(r"[,;\s]+", s.strip()) if x.strip()]


def parse_number_list_validated(
    s: str,
    field_name: str,
    min_value: float = 0.0,
    default_on_invalid: float = 0.0,
    lang: Optional[str] = None,
    warning_items_out: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[float], List[str]]:
    """
    Parse comma/semicolon/whitespace separated numbers with strict validation.
    Replaces non-numeric, non-finite (NaN/Inf), or below-min values with
    `default_on_invalid` and returns explicit warning messages.
    """
    values: List[float] = []
    warnings: List[str] = []
    for raw_tok in re.split(r"[,;\s]+", s.strip()):
        tok = raw_tok.strip().strip('"').strip("'")
        if not tok:
            continue
        try:
            val = float(tok)
        except ValueError:
            params = {"token": tok, "field": field_name, "default": default_on_invalid}
            if warning_items_out is not None:
                warning_items_out.append({"key": "val.num.invalid", "params": params})
            warnings.append(t("val.num.invalid", lang=lang, **params))
            values.append(default_on_invalid)
            continue
        if not math.isfinite(val):
            params = {"token": tok, "field": field_name, "default": default_on_invalid}
            if warning_items_out is not None:
                warning_items_out.append({"key": "val.num.non_finite", "params": params})
            warnings.append(t("val.num.non_finite", lang=lang, **params))
            values.append(default_on_invalid)
            continue
        if val < min_value:
            params = {"val": val, "min": min_value, "field": field_name, "default": default_on_invalid}
            if warning_items_out is not None:
                warning_items_out.append({"key": "val.num.below_min", "params": params})
            warnings.append(t("val.num.below_min", lang=lang, **params))
            values.append(default_on_invalid)
            continue
        values.append(val)
    return values, warnings


def infer_tool_count(matrix: List[float], vector: List[float]) -> Optional[int]:
    """
    Infer slicer extruder count N from an N*N flush_volumes_matrix or vector.
    Returns None if a non-empty matrix is not square (does not silently mask invalid sizes).
    """
    if matrix:
        n = int(round(math.sqrt(len(matrix))))
        if n > 0 and n * n == len(matrix):
            return n
        return None
    if vector:
        return len(vector)
    return None


def area_mm2(diameter: float) -> float:
    """Cross-sectional area of filament in mm^2. Rejects <= 0 and non-finite inputs."""
    if isinstance(diameter, bool) or not isinstance(diameter, (int, float)):
        return 0.0
    diam = float(diameter)
    if not math.isfinite(diam) or diam <= 0.0:
        return 0.0
    return math.pi * (diam * 0.5) ** 2


def volume_to_length_mm(volume_mm3: float, filament_diam_mm: float) -> float:
    """Convert flush volume (mm^3) to purge filament length (mm)."""
    if isinstance(volume_mm3, bool) or not isinstance(volume_mm3, (int, float)):
        return 0.0
    vol = float(volume_mm3)
    if not math.isfinite(vol) or vol <= 0.0:
        return 0.0
    a = area_mm2(filament_diam_mm)
    if a <= 0.0:
        return 0.0
    return vol / a


def compute_flush_volume(
    old_tool: Optional[int],
    new_tool: int,
    n_tools: int,
    matrix: List[float],
    vector: List[float],
    multiplier: float,
) -> float:
    """
    Compute flush volume in mm^3 using original Slicer tool indices.
    Supports both N-length and 2*N-length OrcaSlicer `flush_volumes_vector` layouts.
    """
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
        return 0.0
    mul = float(multiplier)
    if not math.isfinite(mul) or mul <= 0.0 or new_tool < 0:
        return 0.0

    if old_tool is None:
        base = 0.0
        if vector:
            if n_tools > 0 and len(vector) == 2 * n_tools and 0 <= new_tool < n_tools:
                # OrcaSlicer 2.5.0 emits 2 values per tool [unload_t, load_t]; index 2*t + 1 is load volume
                base = vector[2 * new_tool + 1]
            elif 0 <= new_tool < len(vector):
                base = vector[new_tool]
        if isinstance(base, bool) or not isinstance(base, (int, float)) or not math.isfinite(base) or base < 0.0:
            base = 0.0
        return float(base) * mul

    if old_tool == new_tool:
        return 0.0
    if not matrix or n_tools <= 0:
        return 0.0
    if old_tool < 0 or old_tool >= n_tools or new_tool < 0 or new_tool >= n_tools:
        return 0.0
    idx = old_tool * n_tools + new_tool
    if 0 <= idx < len(matrix):
        val = matrix[idx]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0.0:
            return 0.0
        return float(val) * mul
    return 0.0


def rgb_to_hex(rgb: Any) -> str:
    """Convert [R, G, B] list or hex string to normalized '#RRGGBB'."""
    if isinstance(rgb, str):
        val = rgb.strip()
        if re.match(r"^#[0-9A-Fa-f]{6}$", val):
            return val.upper()
        if re.match(r"^[0-9A-Fa-f]{6}$", val):
            return f"#{val.upper()}"
        if re.match(r"^#[0-9A-Fa-f]{8}$", val):
            return val[:7].upper()
    if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
        try:
            r = max(0, min(255, int(rgb[0])))
            g = max(0, min(255, int(rgb[1])))
            b = max(0, min(255, int(rgb[2])))
            return f"#{r:02X}{g:02X}{b:02X}"
        except (ValueError, TypeError):
            pass
    return "#000000"


def hex_to_rgb(hex_str: str) -> Tuple[int, int, int]:
    """Convert '#RRGGBB' to (R, G, B) tuple."""
    if not isinstance(hex_str, str):
        return (0, 0, 0)
    clean = hex_str.strip().lstrip("#")
    if len(clean) >= 6:
        try:
            return (
                int(clean[0:2], 16),
                int(clean[2:4], 16),
                int(clean[4:6], 16),
            )
        except ValueError:
            pass
    return (0, 0, 0)


def color_distance_sq(hex1: str, hex2: str) -> float:
    """Weighted perceptual RGB distance squared (redmean approximation)."""
    r1, g1, b1 = hex_to_rgb(hex1)
    r2, g2, b2 = hex_to_rgb(hex2)
    rmean = 0.5 * (r1 + r2)
    dr = r1 - r2
    dg = g1 - g2
    db = b1 - b2
    return (2.0 + rmean / 256.0) * (dr * dr) + 4.0 * (dg * dg) + (2.0 + (255.0 - rmean) / 256.0) * (db * db)


def _split_slicer_list(raw: str) -> List[str]:
    """Split semicolon- or comma-separated OrcaSlicer config values."""
    raw = raw.strip()
    if ";" in raw:
        parts = raw.split(";")
    else:
        parts = raw.split(",")
    return [p.strip().strip('"') for p in parts]


def strip_previous_postprocess(lines: List[str]) -> List[str]:
    """
    Restore an already post-processed G-code back to its clean original Slicer state
    so repeated post-processing or UI preview with changed mappings is 100% reversible
    and never introduces duplicate `ACE_SET_PURGE_AMOUNT` / `CUT_TIP` commands or uses
    remapped tool indices for purge calculations.
    """
    if not lines:
        return lines

    # Fast check: if there is no audit header and no ACE_SET_PURGE_AMOUNT / skipped G31 / inline tag,
    # avoid allocating a copy unnecessarily.
    has_header = False
    for ln in lines[:12]:
        s = ln.lstrip()
        if s.startswith(("; === Kobra S1 ACE Pro Post-Processor", "; === flush2len postprocess begin ===")):
            has_header = True
            break

    has_artifacts = has_header or any(
        ln.lstrip().startswith(("ACE_SET_PURGE_AMOUNT", "; G31 ; skipped by ACE Plugin"))
        or "@ORCA_ORIG_TOOL:" in ln
        or "@ORCA_ACE_PLUGIN_CUT_TIP" in ln
        for ln in lines
    )
    if not has_artifacts:
        return lines

    meta: Dict[str, Any] = {}
    body_start_idx = 0

    # Parse and strip any top Audit Header block
    if has_header:
        in_hdr = False
        for i, ln in enumerate(lines[:30]):
            s = ln.lstrip()
            if s.startswith(("; === Kobra S1 ACE Pro Post-Processor", "; === flush2len postprocess begin ===")):
                in_hdr = True
                continue
            if in_hdr:
                m_meta = REV_META_RE.match(ln)
                if m_meta:
                    try:
                        parsed = json.loads(m_meta.group(1))
                        if isinstance(parsed, dict):
                            meta = parsed
                    except Exception:
                        pass
                else:
                    # Legacy header fallback: parse toolMap / cutTipPre / cutTipPost if no @ORCA_REVERSIBLE_META
                    m_tm = re.search(r"toolMap=(\{[^}]*\})", ln)
                    if m_tm:
                        try:
                            raw_tm = json.loads(m_tm.group(1))
                            inv_map: Dict[int, int] = {}
                            ambiguous: set = set()
                            for k_str, v_str in raw_tm.items():
                                k_i = int(str(k_str).lstrip("Tt"))
                                v_i = int(str(v_str).lstrip("Tt"))
                                if v_i in inv_map and inv_map[v_i] != k_i:
                                    ambiguous.add(v_i)
                                else:
                                    inv_map[v_i] = k_i
                            for a in ambiguous:
                                inv_map.pop(a, None)
                            meta.setdefault("_legacyInvMap", inv_map)
                        except Exception:
                            pass
                    if "cutTipPre=True" in ln:
                        meta.setdefault("cutTipPreInjected", True)
                    if "cutTipPost=True" in ln:
                        meta.setdefault("cutTipPostInjected", True)
                if s.startswith("; === flush2len / ACE postprocess end ===") or s.startswith("; === flush2len postprocess end ==="):
                    body_start_idx = i + 1
                    break

    event_orig_queue: List[int] = [
        int(x) for x in meta.get("eventOrigTools", []) if isinstance(x, int) or str(x).isdigit()
    ]
    g9111_orig_tool = meta.get("g9111OrigTool")
    orig_skip_purge_val = meta.get("origSkipPurgeVal")
    orig_skip_purge_mode = meta.get("origSkipPurgeMode")
    cut_pre_injected = bool(meta.get("cutTipPreInjected", False))
    cut_post_injected = bool(meta.get("cutTipPostInjected", False))
    orig_print_end_cut_tip = meta.get("origPrintEndCutTip")
    inv_map = meta.get("_legacyInvMap", {})

    def _purge_precedes_toolchange(src_lines: List[str], purge_idx: int) -> bool:
        """Return True when ACE_SET_PURGE_AMOUNT precedes a toolchange T<n> (ignores standalone user commands)."""
        for j in range(purge_idx + 1, len(src_lines)):
            nxt = src_lines[j].lstrip()
            if not nxt or nxt.startswith(";"):
                continue
            if nxt[:20].upper().startswith("ACE_SET_PURGE_AMOUNT") and ACE_SET_PURGE_LINE_RE.match(src_lines[j]):
                continue
            return bool(TOOL_RE.match(src_lines[j]))
        return False

    has_exact_pre_idx = "preCutBodyIdx" in meta
    has_exact_post_idx = "postCutBodyIdx" in meta

    pre_cut_target_idx: Optional[int] = None
    if has_exact_pre_idx and meta.get("preCutBodyIdx") is not None:
        try:
            cand = body_start_idx + int(meta["preCutBodyIdx"])
            if body_start_idx <= cand < len(lines) and not lines[cand].lstrip().startswith(";") and CUT_TIP_LINE_RE.match(lines[cand]):
                pre_cut_target_idx = cand
        except (TypeError, ValueError):
            pre_cut_target_idx = None

    post_cut_target_idx: Optional[int] = None
    if has_exact_post_idx:
        if meta.get("postCutBodyIdx") is not None:
            try:
                cand = body_start_idx + int(meta["postCutBodyIdx"])
                if body_start_idx <= cand < len(lines) and not lines[cand].lstrip().startswith(";") and CUT_TIP_LINE_RE.match(lines[cand]):
                    post_cut_target_idx = cand
            except (TypeError, ValueError):
                post_cut_target_idx = None
    elif cut_post_injected:
        # Legacy header fallback when postCutBodyIdx was not recorded
        for idx in range(len(lines) - 1, body_start_idx - 1, -1):
            ln_cur = lines[idx]
            if not ln_cur.lstrip().startswith(";") and CUT_TIP_LINE_RE.match(ln_cur):
                post_cut_target_idx = idx
                break

    pre_cut_removed = False
    g9111_restored = False
    skip_split_restored = False
    print_end_restored = False
    cleaned: List[str] = []

    for idx in range(body_start_idx, len(lines)):
        ln = lines[idx]
        s = ln.lstrip()

        if "@ORCA_ACE_PLUGIN_CUT_TIP" in ln:
            continue

        if pre_cut_target_idx is not None and idx == pre_cut_target_idx:
            continue

        if post_cut_target_idx is not None and idx == post_cut_target_idx:
            continue

        if SKIPPED_G31_RE.match(ln):
            cleaned.append("G31\n")
            continue

        if s.startswith(";"):
            cleaned.append(ln)
            continue

        if s[:20].upper().startswith("ACE_SET_PURGE_AMOUNT") and ACE_SET_PURGE_LINE_RE.match(ln):
            if _purge_precedes_toolchange(lines, idx):
                continue
            cleaned.append(ln)
            continue

        if (not has_exact_pre_idx) and cut_pre_injected and not pre_cut_removed and CUT_TIP_LINE_RE.match(ln):
            pre_cut_removed = True
            continue

        # Restore G9111 tool=<n> & SKIP_PURGE_FOR_ALREADY_LOADED_TOOL
        if not g9111_restored and s[:5].upper() == "G9111" and G9111_RE.match(ln):
            restored = ln
            inline_m = ORIG_TOOL_INLINE_RE.search(restored)
            if inline_m:
                orig_t = int(inline_m.group(1))
                restored = ORIG_TOOL_INLINE_RE.sub("", restored)
                if ln.endswith("\n") and not restored.endswith("\n"):
                    restored += "\n"
                restored = G9111_TOOL_PARAM_RE.sub(lambda m: f"{m.group(1)}{orig_t}", restored, count=1)
            elif g9111_orig_tool is not None:
                restored = G9111_TOOL_PARAM_RE.sub(
                    lambda m: f"{m.group(1)}{int(g9111_orig_tool)}", restored, count=1
                )
            elif inv_map:
                pm = G9111_TOOL_PARAM_RE.search(restored)
                if pm and int(pm.group(2)) in inv_map:
                    orig_t = inv_map[int(pm.group(2))]
                    restored = G9111_TOOL_PARAM_RE.sub(lambda m: f"{m.group(1)}{orig_t}", restored, count=1)
            if orig_skip_purge_mode == "injected":
                restored = re.sub(
                    r"\s*\bSKIP_PURGE_FOR_ALREADY_LOADED_TOOL\s*=\s*\d+",
                    "",
                    restored,
                    flags=re.IGNORECASE,
                )
            elif orig_skip_purge_mode == "inline" and orig_skip_purge_val is not None:
                restored = SKIP_PURGE_INLINE_RE.sub(
                    f"SKIP_PURGE_FOR_ALREADY_LOADED_TOOL={orig_skip_purge_val}",
                    restored,
                    count=1,
                )
            g9111_restored = True
            cleaned.append(restored)
            continue

        if (
            not skip_split_restored
            and orig_skip_purge_mode == "split_line"
            and orig_skip_purge_val is not None
            and s[:10].upper() == "SKIP_PURGE"
            and SKIP_PURGE_LINE_RE.match(ln)
        ):
            nl = "\n" if ln.endswith("\n") else ""
            cleaned.append(f"SKIP_PURGE_FOR_ALREADY_LOADED_TOOL={orig_skip_purge_val}{nl}")
            skip_split_restored = True
            continue

        # Restore PRINT_END CUT_TIP=<orig>
        if not print_end_restored and orig_print_end_cut_tip is not None and s[:9].upper() == "PRINT_END" and PRINT_END_LINE_RE.match(ln):
            if orig_print_end_cut_tip == "__NONE__":
                restored_end = re.sub(
                    r"\s*\bCUT_TIP\s*=\s*(?:[01]|true|false)",
                    "",
                    ln,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                restored_end = PRINT_END_CUT_TIP_PARAM_RE.sub(
                    rf"\g<1>{orig_print_end_cut_tip}", ln, count=1
                )
            print_end_restored = True
            cleaned.append(restored_end)
            continue

        # Restore T<mapped> -> T<orig>
        if s.startswith("T") or s.startswith("t"):
            tm = TOOL_RE.match(ln)
            if tm:
                leading_ws = tm.group(1)
                mapped_t = int(tm.group(2))
                trailing_rest = (tm.group(3) or "").rstrip("\r\n")
                inline_m = ORIG_TOOL_INLINE_RE.search(trailing_rest)
                orig_t: Optional[int] = None
                if inline_m:
                    orig_t = int(inline_m.group(1))
                    trailing_rest = ORIG_TOOL_INLINE_RE.sub("", trailing_rest)
                elif event_orig_queue:
                    orig_t = event_orig_queue.pop(0)
                elif mapped_t in inv_map:
                    orig_t = inv_map[mapped_t]
                if orig_t is not None:
                    nl = "\n" if ln.endswith("\n") else ""
                    cleaned.append(f"{leading_ws}T{orig_t}{trailing_rest}{nl}")
                    continue

        cleaned.append(ln)

    return cleaned


def parse_slicer_filaments(lines: List[str]) -> List[Dict[str, Any]]:
    """
    Extract Slicer filament definitions (Extruder 0..N) from G-Code comments
    (`; filament:`, `; filament_colour:`, `; filament used [g]/[mm]/[cm3]`, etc.)
    and executable `T<n>` / `G9111 tool=<n>` lines.
    Extruders with `0.0 g` / `0.0 mm` / `0.0 cm3` that never execute in G-code get `used=False`.
    """
    lines = strip_previous_postprocess(lines)
    header_tools: set = set()
    exec_tools: set = set()
    usage_tools: set = set()
    colours: List[str] = []
    multi_colours: List[str] = []
    types: List[str] = []
    vendors: List[str] = []
    settings_ids: List[str] = []
    diameters: List[float] = []
    used_grams: List[float] = []
    used_mms: List[float] = []
    used_cm3s: List[float] = []
    max_seen_tool = -1

    for ln in lines:
        m = FILAMENT_USED_HEADER_RE.match(ln)
        if m:
            for tok in re.split(r"[,\s]+", m.group(1).strip()):
                if tok.isdigit():
                    val = int(tok)
                    # OrcaSlicer's `; filament: 1,2` header is 1-indexed (1 -> T0, 2 -> T1)
                    idx = val - 1 if val >= 1 else 0
                    header_tools.add(idx)
                    if idx > max_seen_tool:
                        max_seen_tool = idx
            continue

        m = FILAMENT_COLOUR_RE.match(ln)
        if m:
            colours = [rgb_to_hex(c) if c.strip() else "" for c in _split_slicer_list(m.group(1))]
            continue

        m = FILAMENT_MULTI_COLOUR_RE.match(ln)
        if m:
            multi_colours = [rgb_to_hex(c) if c.strip() else "" for c in _split_slicer_list(m.group(1))]
            continue

        m = FILAMENT_TYPE_RE.match(ln)
        if m:
            types = _split_slicer_list(m.group(1))
            continue

        m = FILAMENT_VENDOR_RE.match(ln)
        if m:
            vendors = _split_slicer_list(m.group(1))
            continue

        m = FILAMENT_SETTINGS_ID_RE.match(ln)
        if m:
            settings_ids = _split_slicer_list(m.group(1))
            continue

        m = DIAM_RE.match(ln)
        if m:
            try:
                diameters = parse_number_list(m.group(1))
            except Exception:
                pass
            continue

        m = FILAMENT_USED_G_RE.match(ln)
        if m:
            try:
                used_grams = parse_number_list(m.group(1))
            except Exception:
                pass
            continue

        m = FILAMENT_USED_MM_RE.match(ln)
        if m:
            try:
                used_mms = parse_number_list(m.group(1))
            except Exception:
                pass
            continue

        m = FILAMENT_USED_CM3_RE.match(ln)
        if m:
            try:
                used_cm3s = parse_number_list(m.group(1))
            except Exception:
                pass
            continue

        if not ln.lstrip().startswith(";"):
            tm = TOOL_RE.match(ln)
            if tm:
                t_idx = int(tm.group(2))
                exec_tools.add(t_idx)
                if t_idx > max_seen_tool:
                    max_seen_tool = t_idx
                continue
            gm = G9111_RE.match(ln)
            if gm:
                pm = G9111_TOOL_PARAM_RE.search(ln)
                if pm:
                    t_idx = int(pm.group(2))
                    exec_tools.add(t_idx)
                    if t_idx > max_seen_tool:
                        max_seen_tool = t_idx

    for i, g in enumerate(used_grams):
        if g > 0:
            usage_tools.add(i)
            if i > max_seen_tool:
                max_seen_tool = i
    for i, mm in enumerate(used_mms):
        if mm > 0:
            usage_tools.add(i)
            if i > max_seen_tool:
                max_seen_tool = i
    for i, cm3 in enumerate(used_cm3s):
        if cm3 > 0:
            usage_tools.add(i)
            if i > max_seen_tool:
                max_seen_tool = i

    used_tools = (usage_tools | exec_tools) if (usage_tools or exec_tools) else header_tools

    total_defined = max(
        len(colours),
        len(multi_colours),
        len(types),
        max_seen_tool + 1,
        1,
    )

    filaments: List[Dict[str, Any]] = []
    for idx in range(total_defined):
        col = ""
        if idx < len(multi_colours) and multi_colours[idx]:
            col = multi_colours[idx]
        elif idx < len(colours) and colours[idx]:
            col = colours[idx]
        else:
            col = "#CCCCCC"

        mat = types[idx] if idx < len(types) and types[idx] else "PLA"
        vendor = vendors[idx] if idx < len(vendors) else ""
        profile = settings_ids[idx] if idx < len(settings_ids) else ""
        diam = diameters[idx] if idx < len(diameters) else (diameters[0] if diameters else DEFAULT_FILAMENT_DIAMETER)
        grams = used_grams[idx] if idx < len(used_grams) else 0.0
        mm_val = used_mms[idx] if idx < len(used_mms) else 0.0
        cm3_val = used_cm3s[idx] if idx < len(used_cm3s) else 0.0
        is_used = (idx in used_tools) if used_tools else (idx == 0)

        filaments.append(
            {
                "slicer_tool": idx,
                "label": f"Extruder {idx} (T{idx})",
                "color": col,
                "rgb": list(hex_to_rgb(col)),
                "material": mat,
                "vendor": vendor,
                "profile": profile,
                "diameter": diam,
                "used_g": round(float(grams), 2),
                "used_mm": round(float(mm_val), 2),
                "used_m": round(float(mm_val) / 1000.0, 2),
                "used_cm3": round(float(cm3_val), 2),
                "used": is_used,
            }
        )

    return filaments


def parse_gcode_job_metadata(lines: List[str], filepath: str = "") -> Dict[str, Any]:
    """
    Parse print job summary metadata from G-code header/footer comments
    (filename, estimated print time, total filament weight/length, total layers,
    max Z, and largest embedded PNG thumbnail).
    """
    estimated_time = ""
    total_layers = 0
    max_z_height = 0.0
    total_weight_g = 0.0
    used_grams_list: List[float] = []
    used_mms_list: List[float] = []

    in_thumb = False
    cur_thumb_w = 0
    cur_thumb_h = 0
    cur_thumb_chunks: List[str] = []
    best_thumb_score: Tuple[int, int] = (0, 0)
    best_thumb_w = 0
    best_thumb_h = 0
    best_thumb_data_url = ""

    for ln in lines:
        stripped = ln.lstrip()
        if not stripped.startswith(";"):
            if in_thumb:
                in_thumb = False
                cur_thumb_chunks = []
            continue

        if in_thumb:
            if THUMBNAIL_END_RE.match(ln):
                in_thumb = False
                b64_payload = "".join(cur_thumb_chunks)
                cur_thumb_chunks = []
                if b64_payload:
                    try:
                        raw_png = base64.b64decode(b64_payload, validate=True)
                        if raw_png.startswith(b"\x89PNG\r\n\x1a\n"):
                            score = (cur_thumb_w * cur_thumb_h, len(b64_payload))
                            if score > best_thumb_score:
                                best_thumb_score = score
                                best_thumb_w = cur_thumb_w
                                best_thumb_h = cur_thumb_h
                                best_thumb_data_url = f"data:image/png;base64,{b64_payload}"
                    except Exception:
                        pass
                continue
            chunk = stripped[1:].strip()
            if chunk:
                cur_thumb_chunks.append("".join(chunk.split()))
            continue

        m_thumb = THUMBNAIL_BEGIN_RE.match(ln)
        if m_thumb:
            in_thumb = True
            try:
                cur_thumb_w = int(m_thumb.group(1))
                cur_thumb_h = int(m_thumb.group(2))
            except ValueError:
                cur_thumb_w = 0
                cur_thumb_h = 0
            cur_thumb_chunks = []
            continue

        m = ESTIMATED_TIME_RE.match(ln)
        if m and not estimated_time:
            estimated_time = m.group(1).strip()
            continue
        m = TOTAL_LAYERS_RE.match(ln)
        if m:
            try:
                total_layers = int(m.group(1))
            except ValueError:
                pass
            continue
        m = MAX_Z_HEIGHT_RE.match(ln)
        if m:
            try:
                max_z_height = float(m.group(1))
            except ValueError:
                pass
            continue
        m = TOTAL_FILAMENT_G_RE.match(ln)
        if m:
            try:
                total_weight_g = float(m.group(1))
            except ValueError:
                pass
            continue
        m = FILAMENT_USED_G_RE.match(ln)
        if m:
            try:
                used_grams_list = parse_number_list(m.group(1))
            except Exception:
                pass
            continue
        m = FILAMENT_USED_MM_RE.match(ln)
        if m:
            try:
                used_mms_list = parse_number_list(m.group(1))
            except Exception:
                pass
            continue

    if total_weight_g <= 0.0 and used_grams_list:
        total_weight_g = sum(max(0.0, float(x)) for x in used_grams_list)
    total_length_m = sum(max(0.0, float(x)) for x in used_mms_list) / 1000.0 if used_mms_list else 0.0

    return {
        "filename": os.path.basename(filepath) if filepath else "",
        "estimated_time": estimated_time or "—",
        "total_layers": total_layers,
        "max_z_height": round(max_z_height, 2),
        "total_weight_g": round(total_weight_g, 2),
        "total_length_m": round(total_length_m, 2),
        "thumbnail_data_url": best_thumb_data_url,
        "thumbnail_width": best_thumb_w,
        "thumbnail_height": best_thumb_h,
    }


def compute_filament_signature(slicer_filaments: List[Dict[str, Any]]) -> str:
    """Compute a stable signature of ordered Slicer filaments to detect order/material changes."""
    items = [
        [
            int(f.get("slicer_tool", idx)),
            str(f.get("material", "")).strip().upper(),
            rgb_to_hex(f.get("color", "#CCCCCC")),
        ]
        for idx, f in enumerate(slicer_filaments or [])
        if isinstance(f, dict)
    ]
    return json.dumps(items, separators=(",", ":"))


def _materials_compatible(s_mat: str, p_mat: str) -> Tuple[bool, float]:
    """Return `(is_compatible, penalty)` for comparing slicer vs ACE slot material."""
    s_u = s_mat.strip().upper()
    p_u = p_mat.strip().upper()
    if s_u and p_u:
        if s_u == p_u:
            return True, 0.0
        if s_u.startswith(p_u) or p_u.startswith(s_u):
            return True, 2500.0
        return False, 1_000_000.0
    return False, 10_000.0


def auto_match_filaments_to_slots(
    slicer_filaments: List[Dict[str, Any]],
    ace_slots: List[Dict[str, Any]],
) -> Dict[int, int]:
    """
    Automatically match Slicer filaments (0..N) to physical ACE Pro slots (T0..T7)
    based on material compatibility and closest perceptual Hex/RGB color.
    Never matches `empty` slots and never lets incompatible material beat color proximity.
    """
    available_slots = [
        s
        for s in ace_slots
        if str(s.get("status", "")).lower() in ("ready", "loaded", "active")
    ]
    if not available_slots:
        return {
            int(f.get("slicer_tool", idx)): int(f.get("slicer_tool", idx))
            for idx, f in enumerate(slicer_filaments)
        }

    mapping: Dict[int, int] = {}
    used_physical_tools: set = set()

    # Prioritize actively used slicer filaments first
    ordered_filaments = sorted(
        slicer_filaments,
        key=lambda f: (0 if f.get("used", True) else 1, int(f.get("slicer_tool", 0))),
    )

    for fil in ordered_filaments:
        s_tool = int(fil.get("slicer_tool", 0))
        s_mat = str(fil.get("material", "")).strip().upper()
        s_hex = rgb_to_hex(fil.get("color", "#FFFFFF"))

        best_tool: Optional[int] = None
        best_score = float("inf")

        for candidate_pass in (0, 1):
            # In pass 0, if any unassigned slot has compatible material, restrict to compatible slots
            unassigned_compatible_exists = False
            if candidate_pass == 0:
                for slot in available_slots:
                    p_tool = int(slot.get("tool", slot.get("index", 0)))
                    if p_tool in used_physical_tools:
                        continue
                    p_mat = str(slot.get("material") or slot.get("type") or "").strip().upper()
                    compat, _ = _materials_compatible(s_mat, p_mat)
                    if compat:
                        unassigned_compatible_exists = True
                        break

            # Pass 0: prefer unassigned slots; Pass 1: allow sharing a slot if more filaments than slots
            for slot in available_slots:
                p_tool = int(slot.get("tool", slot.get("index", 0)))
                if candidate_pass == 0 and p_tool in used_physical_tools:
                    continue

                p_mat = str(slot.get("material") or slot.get("type") or "").strip().upper()
                p_hex = rgb_to_hex(slot.get("hex_color") or slot.get("color") or "#000000")
                compat, mat_penalty = _materials_compatible(s_mat, p_mat)

                if candidate_pass == 0 and unassigned_compatible_exists and not compat:
                    continue

                # Penalize unconfigured non-RFID default [0,0,0] slots with empty material
                unconfigured_penalty = 0.0
                if not bool(slot.get("rfid")) and not p_mat and p_hex == "#000000":
                    unconfigured_penalty = 700_000.0

                dist = color_distance_sq(s_hex, p_hex)
                score = mat_penalty + unconfigured_penalty + dist
                if score < best_score:
                    best_score = score
                    best_tool = p_tool

            if best_tool is not None:
                break

        if best_tool is None:
            best_tool = int(available_slots[0].get("tool", available_slots[0].get("index", 0)))

        mapping[s_tool] = best_tool
        used_physical_tools.add(best_tool)

    return mapping


def validate_color_mapping(
    slicer_filaments: List[Dict[str, Any]],
    tool_map: Optional[Dict[Any, Any]],
    ace_slots: List[Dict[str, Any]],
    lang: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validate Slicer -> physical ACE Pro color mapping against live ACE slot state.
    Detects blocking empty/out-of-range slots plus non-blocking material mismatches,
    unconfigured non-RFID slots, and shared ACE slot assignments (intentional multi-extruder reuse).
    """
    norm_map: Dict[int, int] = {}
    if tool_map:
        for k, v in tool_map.items():
            try:
                norm_map[int(k)] = int(v)
            except (ValueError, TypeError):
                pass

    slot_by_tool: Dict[int, Dict[str, Any]] = {}
    for idx, slot in enumerate(ace_slots or []):
        if isinstance(slot, dict):
            t_id = int(slot.get("tool", slot.get("index", idx)))
            slot_by_tool[t_id] = slot

    errors: List[str] = []
    warnings: List[str] = []
    infos: List[str] = []
    error_items: List[Dict[str, Any]] = []
    warning_items: List[Dict[str, Any]] = []
    info_items: List[Dict[str, Any]] = []
    empty_slots: List[int] = []
    out_of_range_slots: List[int] = []
    shared_slots: List[int] = []
    material_mismatches: List[Dict[str, Any]] = []
    phys_to_slicer: Dict[int, List[int]] = {}

    for fil in slicer_filaments or []:
        if not fil.get("used", True):
            continue
        s_tool = int(fil.get("slicer_tool", 0))
        mapped = norm_map.get(s_tool, s_tool)
        s_mat = str(fil.get("material", "")).strip().upper()
        phys_to_slicer.setdefault(mapped, []).append(s_tool)

        if slot_by_tool:
            if mapped < 0 or mapped >= len(ace_slots) or mapped not in slot_by_tool:
                if mapped not in out_of_range_slots:
                    out_of_range_slots.append(mapped)
                params = {"slicer_tool": s_tool, "mapped": mapped, "max_slot": len(ace_slots) - 1}
                error_items.append({"key": "val.map.not_present", "params": params})
                errors.append(t("val.map.not_present", lang=lang, **params))
                continue

            slot = slot_by_tool[mapped]
            slot_status = str(slot.get("status", "empty")).strip().lower()
            p_mat = str(slot.get("material") or slot.get("type") or "").strip().upper()
            p_hex = rgb_to_hex(slot.get("hex_color") or slot.get("color") or "#000000")

            if slot_status == "empty":
                if mapped not in empty_slots:
                    empty_slots.append(mapped)
                params = {"slicer_tool": s_tool, "mapped": mapped}
                error_items.append({"key": "val.map.empty_slot", "params": params})
                errors.append(t("val.map.empty_slot", lang=lang, **params))
            else:
                if s_mat and p_mat:
                    compat, _ = _materials_compatible(s_mat, p_mat)
                    if not compat:
                        material_mismatches.append(
                            {
                                "slicer_tool": s_tool,
                                "mapped_slot": mapped,
                                "slicer_material": s_mat,
                                "slot_material": p_mat,
                            }
                        )
                        params = {
                            "slicer_tool": s_tool,
                            "slicer_mat": s_mat,
                            "mapped": mapped,
                            "ace_mat": p_mat,
                        }
                        warning_items.append({"key": "val.map.mat_mismatch", "params": params})
                        warnings.append(t("val.map.mat_mismatch", lang=lang, **params))
                elif not bool(slot.get("rfid")) and not p_mat and p_hex == "#000000":
                    params = {"mapped": mapped, "slicer_tool": s_tool}
                    warning_items.append({"key": "val.map.unconfigured", "params": params})
                    warnings.append(t("val.map.unconfigured", lang=lang, **params))

    for phys_tool, s_tools in sorted(phys_to_slicer.items()):
        if len(s_tools) > 1:
            shared_slots.append(phys_tool)
            s_desc = ", ".join(f"T{t_idx}" for t_idx in s_tools)
            params = {"extruders": s_desc, "phys_tool": phys_tool}
            info_items.append({"key": "val.map.shared", "params": params})
            infos.append(t("val.map.shared", lang=lang, **params))

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "info": infos,
        "error_items": error_items,
        "warning_items": warning_items,
        "info_items": info_items,
        "shared_slots": shared_slots,
        "duplicate_slots": list(shared_slots),
        "empty_slots": empty_slots,
        "out_of_range_slots": out_of_range_slots,
        "material_mismatches": material_mismatches,
        "has_material_mismatch": len(material_mismatches) > 0,
        "requires_mismatch_confirmation": len(material_mismatches) > 0,
    }


# ---------------------------------------------------------------------------
# Core G-Code Post-Processor
# ---------------------------------------------------------------------------
def build_vector_summary(
    n_tools: int,
    vector: List[float],
    multiplier: float,
    default_diameter: float,
    parsed_diameters: Optional[List[float]] = None,
    tool_map: Optional[Dict[int, int]] = None,
) -> List[Dict[str, Any]]:
    """
    Build structured per-tool flush vector summary supporting both N-entry and
    2*N-entry (`[unload_0, load_0, ...]`) OrcaSlicer `flush_volumes_vector` layouts,
    including dynamic mapping to physical ACE Pro slots (`mapped_tool`).
    """
    summary: List[Dict[str, Any]] = []
    if not vector:
        return summary
    mul = float(multiplier) if math.isfinite(multiplier) and multiplier > 0 else 0.0
    diams = parsed_diameters or []
    tmap = tool_map or {}
    is_2n = n_tools > 0 and len(vector) == 2 * n_tools
    count = n_tools if is_2n else len(vector)
    for t_idx in range(count):
        tool_diam = diams[t_idx] if 0 <= t_idx < len(diams) and diams[t_idx] > 0 else default_diameter
        if is_2n:
            unload_v = round(max(0.0, float(vector[2 * t_idx])), 2)
            load_v = round(max(0.0, float(vector[2 * t_idx + 1])), 2)
            layout = "2xN"
        else:
            unload_v = None
            load_v = round(max(0.0, float(vector[t_idx])), 2)
            layout = "N"
        eff_vol = round(load_v * mul, 2)
        eff_len = round(volume_to_length_mm(eff_vol, tool_diam), 2)
        mapped_t = int(tmap.get(t_idx, t_idx))
        summary.append(
            {
                "tool": t_idx,
                "label": f"T{t_idx}",
                "mapped_tool": mapped_t,
                "mapped_label": f"T{mapped_t}",
                "layout": layout,
                "unload_mm3": unload_v,
                "load_mm3": load_v,
                "effective_load_mm3": eff_vol,
                "effective_length_mm": eff_len,
                "diameter_mm": round(float(tool_diam), 3),
            }
        )
    return summary


def build_remapped_flush_schedule(
    n_tools: int,
    matrix: List[float],
    vector: List[float],
    multiplier: float,
    default_diameter: float,
    parsed_diameters: Optional[List[float]] = None,
    tool_map: Optional[Dict[int, int]] = None,
    round_digits: int = DEFAULT_ROUND_DIGITS,
) -> Dict[str, Any]:
    """
    Build a dynamic flush volume & purge length schedule mapped to physical ACE Pro slots (T0..T7).
    Ensures every Slicer tool transition `(s_from -> s_to)` is associated with its target
    physical ACE transition `(T<mapped_from> -> T<mapped_to>)` and exact purge length (mm).
    """
    tmap = {int(k): int(v) for k, v in (tool_map or {}).items()}
    diams = parsed_diameters or []
    mul = float(multiplier) if math.isfinite(multiplier) and multiplier > 0 else 0.0

    slicer_to_ace: Dict[int, int] = {
        s_idx: tmap.get(s_idx, s_idx) for s_idx in range(max(n_tools, 0))
    }
    transitions: List[Dict[str, Any]] = []
    physical_matrix: Dict[str, Dict[str, Any]] = {}

    if matrix and n_tools > 0 and len(matrix) == n_tools * n_tools:
        for s_from in range(n_tools):
            for s_to in range(n_tools):
                if s_from == s_to:
                    continue
                raw_v = max(0.0, float(matrix[s_from * n_tools + s_to]))
                eff_v = raw_v * mul
                t_diam = diams[s_to] if 0 <= s_to < len(diams) and diams[s_to] > 0 else default_diameter
                p_len = round(volume_to_length_mm(eff_v, t_diam), round_digits)
                ace_from = slicer_to_ace.get(s_from, s_from)
                ace_to = slicer_to_ace.get(s_to, s_to)
                entry = {
                    "from_slicer_tool": s_from,
                    "to_slicer_tool": s_to,
                    "from_ace_slot": ace_from,
                    "to_ace_slot": ace_to,
                    "raw_volume_mm3": round(raw_v, 2),
                    "effective_volume_mm3": round(eff_v, 2),
                    "diameter_mm": round(float(t_diam), 3),
                    "purge_length_mm": p_len,
                }
                transitions.append(entry)
                physical_matrix[f"T{ace_from}->T{ace_to}"] = entry

    return {
        "slicer_to_ace_map": slicer_to_ace,
        "transitions": transitions,
        "physical_matrix": physical_matrix,
    }


def remap_flush_info(
    info: Dict[str, Any],
    tool_map: Dict[Any, Any],
    slicer_filaments: Optional[List[Dict[str, Any]]] = None,
    ace_slots: Optional[List[Dict[str, Any]]] = None,
    lang: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Dynamically update an existing `process_gcode` / `analyze_gcode_flush` result dictionary
    when the user changes Slicer-to-ACE slot mappings (manually or via Auto-Match).
    Updates `toolMap`, `g9111MappedTool`, `vector_summary`, `remapped_flush_schedule`,
    all toolchange events' `old_mapped_tool` / `mapped_tool`, and `mapping_validation`.
    """
    if not info or not isinstance(info, dict):
        return {}
    updated = dict(info)
    norm_map: Dict[int, int] = {}
    for k, v in (tool_map or {}).items():
        try:
            norm_map[int(k)] = int(v)
        except (ValueError, TypeError):
            pass
    updated["toolMap"] = norm_map

    def _m(s_tool: Optional[int]) -> Optional[int]:
        if s_tool is None:
            return None
        return norm_map.get(int(s_tool), int(s_tool))

    if updated.get("g9111StartSlicerTool") is not None:
        updated["g9111MappedTool"] = _m(int(updated["g9111StartSlicerTool"]))

    n_tools = int(updated.get("tools") or 0)
    vector = list(updated.get("vector") or [])
    matrix = list(updated.get("matrix") or [])
    mul = float(updated.get("flush_multiplier") if updated.get("flush_multiplier") is not None else 1.0)
    eff_diam = float(updated.get("filament_diameter") or DEFAULT_FILAMENT_DIAMETER)
    diams = list(updated.get("parsed_diameters") or [])

    updated["vector_summary"] = build_vector_summary(
        n_tools=n_tools,
        vector=vector,
        multiplier=mul,
        default_diameter=eff_diam,
        parsed_diameters=diams,
        tool_map=norm_map,
    )
    updated["remapped_flush_schedule"] = build_remapped_flush_schedule(
        n_tools=n_tools,
        matrix=matrix,
        vector=vector,
        multiplier=mul,
        default_diameter=eff_diam,
        parsed_diameters=diams,
        tool_map=norm_map,
    )

    new_events: List[Dict[str, Any]] = []
    for ev in updated.get("events") or []:
        ev_copy = dict(ev)
        old_s = ev_copy.get("old_slicer_tool")
        new_s = int(ev_copy.get("new_slicer_tool", 0))
        ev_copy["old_mapped_tool"] = _m(old_s) if old_s is not None else None
        ev_copy["mapped_tool"] = _m(new_s)
        new_events.append(ev_copy)
    updated["events"] = new_events

    fils = slicer_filaments if slicer_filaments is not None else list(updated.get("slicer_filaments") or [])
    if fils:
        updated["slicer_filaments"] = fils
    if ace_slots is not None or "mapping_validation" in updated:
        updated["mapping_validation"] = validate_color_mapping(
            fils, norm_map, ace_slots or [], lang=lang
        )
    return updated


def process_gcode(
    lines: List[str],
    filament_diameter: Optional[float] = None,
    round_digits: int = DEFAULT_ROUND_DIGITS,
    tool_map: Optional[Dict[int, int]] = None,
    bed_leveling: bool = True,
    skip_purge_for_already_loaded_tool: bool = True,
    cut_tip_after_print: bool = False,
    force_initial_purge: bool = False,
    include_audit_header: bool = True,
    lang: Optional[str] = None,
) -> Tuple[List[str], Dict[str, Any]]:
    """
    Post-process OrcaSlicer G-code for Anycubic Kobra S1 with Vanilla Klipper + ACE Pro.

    1. Parses `; flush_multiplier`, `; flush_volumes_matrix`, `; flush_volumes_vector`,
       and `; filament_diameter`.
    2. Computes `PURGELENGTH` (mm) strictly first using the ORIGINAL Slicer tool indices
       (`old_slicer_tool -> new_slicer_tool`).
    3. Rewrites `G9111 ... tool=<n>` to `tool=<mapped>` and sets
       `SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1` or `=0` (handling both inline and
       split-line `SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=...`) so the immediately following
       `T<n>` inside `; FLUSH_START` gets `PURGELENGTH=0.00` when skipped (unless
       `force_initial_purge=True`).
    4. Rewrites every `T<n>` to `T<mapped>` and inserts
       `ACE_SET_PURGE_AMOUNT PURGELENGTH=<len_mm>` directly before it.
    5. Applies macro-aligned pre-/post-print toggles:
       - `bed_leveling=False` -> comments out `G31` (`; G31 ; skipped by ACE Plugin`)
       - `skip_purge_for_already_loaded_tool=True/False` -> sets `SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1/0`
       - `cut_tip_after_print=True/False` -> sets `PRINT_END CUT_TIP=1` or `0`
         (falling back to standalone `CUT_TIP` only if no `PRINT_END` is present)
    6. Prepends an Audit Header comment block at the start of the returned lines.
    """
    lines = strip_previous_postprocess(lines)

    multiplier = 1.0
    matrix: List[float] = []
    vector: List[float] = []
    parsed_diameters: List[float] = []
    warnings: List[str] = []
    warning_items: List[Dict[str, Any]] = []
    seen_mul = False
    seen_matrix = False
    seen_vector = False
    invalid_matrix_size = False

    def add_warning(key: str, **params: Any) -> None:
        warning_items.append({"key": key, "params": params})
        warnings.append(t(key, lang=lang, **params))

    # Normalize tool_map keys/values to int
    normalized_tool_map: Dict[int, int] = {}
    if tool_map:
        for k, v in tool_map.items():
            try:
                normalized_tool_map[int(k)] = int(v)
            except (ValueError, TypeError):
                pass

    def map_tool(slicer_tool: int) -> int:
        return normalized_tool_map.get(slicer_tool, slicer_tool)

    # Pass 1: Parse and validate flush metadata and filament diameter from comments
    for ln in lines:
        m = MUL_RE.match(ln)
        if m:
            seen_mul = True
            raw_mul = m.group(1).strip()
            try:
                val = float(raw_mul)
                if not math.isfinite(val):
                    add_warning("val.gcode.mul_non_finite", raw=raw_mul)
                    multiplier = 1.0
                elif val < 0.0:
                    add_warning("val.gcode.mul_negative", val=val)
                    multiplier = 0.0
                else:
                    multiplier = val
            except ValueError:
                add_warning("val.gcode.mul_invalid", raw=raw_mul)
                multiplier = 1.0
            continue
        m = MAT_RE.match(ln)
        if m:
            seen_matrix = True
            matrix, mat_warns = parse_number_list_validated(
                m.group(1),
                "flush_volumes_matrix",
                min_value=0.0,
                default_on_invalid=0.0,
                lang=lang,
                warning_items_out=warning_items,
            )
            warnings.extend(mat_warns)
            continue
        m = VEC_RE.match(ln)
        if m:
            seen_vector = True
            vector, vec_warns = parse_number_list_validated(
                m.group(1),
                "flush_volumes_vector",
                min_value=0.0,
                default_on_invalid=0.0,
                lang=lang,
                warning_items_out=warning_items,
            )
            warnings.extend(vec_warns)
            continue
        m = DIAM_RE.match(ln)
        if m and not parsed_diameters:
            parsed_diameters, diam_warns = parse_number_list_validated(
                m.group(1),
                "filament_diameter",
                min_value=0.01,
                default_on_invalid=DEFAULT_FILAMENT_DIAMETER,
                lang=lang,
                warning_items_out=warning_items,
            )
            warnings.extend(diam_warns)
            continue

    if filament_diameter is not None:
        if isinstance(filament_diameter, bool) or not isinstance(filament_diameter, (int, float)) or not math.isfinite(filament_diameter) or filament_diameter <= 0:
            add_warning("val.gcode.diam_invalid", diam=filament_diameter, default=DEFAULT_FILAMENT_DIAMETER)
            if parsed_diameters and parsed_diameters[0] > 0:
                effective_diameter = float(parsed_diameters[0])
            else:
                effective_diameter = DEFAULT_FILAMENT_DIAMETER
        else:
            effective_diameter = float(filament_diameter)
    else:
        if parsed_diameters and parsed_diameters[0] > 0:
            effective_diameter = float(parsed_diameters[0])
        else:
            effective_diameter = DEFAULT_FILAMENT_DIAMETER

    if seen_matrix:
        if not matrix:
            invalid_matrix_size = True
            add_warning("val.gcode.matrix_empty")
        else:
            mat_n = int(round(math.sqrt(len(matrix))))
            if mat_n <= 0 or mat_n * mat_n != len(matrix):
                invalid_matrix_size = True
                add_warning("val.gcode.matrix_not_square", count=len(matrix))

    n_tools = infer_tool_count(matrix, vector) or 0
    if not matrix and seen_vector and parsed_diameters and len(vector) == 2 * len(parsed_diameters):
        n_tools = len(parsed_diameters)
    if n_tools > 0 and seen_vector and len(vector) not in (n_tools, 2 * n_tools):
        add_warning(
            "val.gcode.vector_mismatch",
            vec_len=len(vector),
            n_tools=n_tools,
            double_tools=2 * n_tools,
        )

    # Pass 2: Walk G-code lines, handle G9111, SKIP_PURGE_FOR_ALREADY_LOADED_TOOL, G31, PRINT_END, T<n>
    current_slicer_tool: Optional[int] = None
    g9111_slicer_tool: Optional[int] = None
    g9111_mapped_tool: Optional[int] = None
    skip_purge_active = False
    first_toolchange_seen = False
    post_cut_injected = False
    post_cut_body_idx: Optional[int] = None
    orig_skip_purge_val: Optional[str] = None
    orig_skip_purge_mode: Optional[str] = None
    orig_print_end_cut_tip: Optional[str] = None
    print_end_seen = False
    in_end_gcode = False
    g31_skipped_count = 0
    toolchange_count = 0
    toolchange_events: List[Dict[str, Any]] = []
    event_orig_tools: List[int] = []
    used_slicer_tools_in_exec: set = set()
    has_incomplete_flush_data = False
    warned_missing_vector = False
    warned_missing_matrix = False
    body_lines: List[str] = []

    has_split_skip_purge_in_file = any(
        (not l.lstrip().startswith(";")) and bool(SKIP_PURGE_LINE_RE.match(l)) for l in lines
    )

    for ln in lines:
        stripped = ln.lstrip()
        is_comment = stripped.startswith(";")

        # Detect start of end-gcode or config block (fallback CUT_TIP only if file has no PRINT_END)
        if is_comment:
            if EXEC_BLOCK_END_RE.match(ln) or CONFIG_BLOCK_START_RE.match(ln):
                if cut_tip_after_print and not post_cut_injected and not print_end_seen:
                    if body_lines and not body_lines[-1].endswith("\n"):
                        body_lines[-1] += "\n"
                    post_cut_body_idx = len(body_lines)
                    body_lines.append("CUT_TIP\n")
                    post_cut_injected = True
            body_lines.append(ln)
            continue

        # 1) G9111 Start Macro handling & SKIP_PURGE_FOR_ALREADY_LOADED_TOOL
        m_g9111 = G9111_RE.match(ln)
        if m_g9111:
            m_tool_param = G9111_TOOL_PARAM_RE.search(ln)
            rewritten_ln = ln
            if m_tool_param:
                orig_start_tool = int(m_tool_param.group(2))
                used_slicer_tools_in_exec.add(orig_start_tool)
                mapped_start_tool = map_tool(orig_start_tool)
                g9111_slicer_tool = orig_start_tool
                g9111_mapped_tool = mapped_start_tool
                rewritten_ln = G9111_TOOL_PARAM_RE.sub(
                    lambda match: f"{match.group(1)}{mapped_start_tool}",
                    rewritten_ln,
                    count=1,
                )

            desired_skip_val = "1" if skip_purge_for_already_loaded_tool else "0"
            m_skip_inline = SKIP_PURGE_INLINE_RE.search(rewritten_ln)
            if m_skip_inline:
                if orig_skip_purge_val is None:
                    orig_skip_purge_val = m_skip_inline.group(1)
                    orig_skip_purge_mode = "inline"
                rewritten_ln = SKIP_PURGE_INLINE_RE.sub(
                    f"SKIP_PURGE_FOR_ALREADY_LOADED_TOOL={desired_skip_val}",
                    rewritten_ln,
                    count=1,
                )
            elif not has_split_skip_purge_in_file:
                if orig_skip_purge_mode is None:
                    orig_skip_purge_mode = "injected"
                nl = "\n" if rewritten_ln.endswith("\n") else ""
                core = rewritten_ln.rstrip("\r\n").rstrip()
                if ";" in core:
                    cmd_part, cmt_part = core.split(";", 1)
                    rewritten_ln = (
                        f"{cmd_part.rstrip()} SKIP_PURGE_FOR_ALREADY_LOADED_TOOL={desired_skip_val} ;{cmt_part}{nl}"
                    )
                else:
                    rewritten_ln = f"{core} SKIP_PURGE_FOR_ALREADY_LOADED_TOOL={desired_skip_val}{nl}"

            skip_purge_active = bool(skip_purge_for_already_loaded_tool)
            if skip_purge_active and g9111_slicer_tool is not None and not force_initial_purge:
                current_slicer_tool = g9111_slicer_tool

            body_lines.append(rewritten_ln)
            continue

        # 2) Standalone SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=0|1 line (as in OrcaSlicer/refs/test_sample.gcode)
        m_skip = SKIP_PURGE_LINE_RE.match(ln)
        if m_skip:
            if orig_skip_purge_val is None:
                orig_skip_purge_val = m_skip.group(1)
                orig_skip_purge_mode = "split_line"
            desired_skip_val = "1" if skip_purge_for_already_loaded_tool else "0"
            nl = "\n" if ln.endswith("\n") else ""
            if skip_purge_for_already_loaded_tool:
                skip_purge_active = True
                if g9111_slicer_tool is not None and not force_initial_purge and not first_toolchange_seen:
                    current_slicer_tool = g9111_slicer_tool
            else:
                skip_purge_active = False
                if not first_toolchange_seen and not force_initial_purge:
                    current_slicer_tool = None
            body_lines.append(f"SKIP_PURGE_FOR_ALREADY_LOADED_TOOL={desired_skip_val}{nl}")
            continue

        # 3) Bed Leveling G31 toggle
        m_g31 = G31_LINE_RE.match(ln)
        if m_g31 and not in_end_gcode:
            if not bed_leveling:
                body_lines.append("; G31 ; skipped by ACE Plugin\n")
                g31_skipped_count += 1
                continue
            body_lines.append(ln)
            continue

        # 4) PRINT_END handling (sets PRINT_END CUT_TIP=1 or 0 reversibly)
        m_print_end = PRINT_END_LINE_RE.match(ln)
        if m_print_end:
            in_end_gcode = True
            print_end_seen = True
            desired_ct = "1" if cut_tip_after_print else "0"
            m_pe_ct = PRINT_END_CUT_TIP_PARAM_RE.search(ln)
            if m_pe_ct:
                if orig_print_end_cut_tip is None:
                    orig_print_end_cut_tip = m_pe_ct.group(2)
                rewritten_end = PRINT_END_CUT_TIP_PARAM_RE.sub(
                    rf"\g<1>{desired_ct}", ln, count=1
                )
            else:
                if orig_print_end_cut_tip is None:
                    orig_print_end_cut_tip = "__NONE__"
                nl = "\n" if ln.endswith("\n") else ""
                core = ln.rstrip("\r\n").rstrip()
                if ";" in core:
                    cmd_part, cmt_part = core.split(";", 1)
                    rewritten_end = f"{cmd_part.rstrip()} CUT_TIP={desired_ct} ;{cmt_part}{nl}"
                else:
                    rewritten_end = f"{core} CUT_TIP={desired_ct}{nl}"
            body_lines.append(rewritten_end)
            continue

        # 6) Toolchange T<n> rewrite + ACE_SET_PURGE_AMOUNT injection
        m_tool = TOOL_RE.match(ln)
        if m_tool:
            leading_ws = m_tool.group(1)
            new_slicer_tool = int(m_tool.group(2))
            trailing_rest = (m_tool.group(3) or "").rstrip("\r\n")
            used_slicer_tools_in_exec.add(new_slicer_tool)

            # Determine effective old_slicer_tool for flush calculation
            if not first_toolchange_seen and force_initial_purge:
                old_slicer_tool: Optional[int] = None
            else:
                old_slicer_tool = current_slicer_tool

            # Validate flush data completeness & tool range
            max_known_tools = n_tools if n_tools > 0 else len(vector)
            if max_known_tools > 0 and new_slicer_tool >= max_known_tools:
                add_warning("val.gcode.tool_exceeds", tool=new_slicer_tool, max_tools=max_known_tools)
            if old_slicer_tool is None:
                if not vector:
                    has_incomplete_flush_data = True
                    if not warned_missing_vector:
                        add_warning("val.gcode.vector_missing_init", tool=new_slicer_tool)
                        warned_missing_vector = True
            elif old_slicer_tool != new_slicer_tool:
                if not matrix or invalid_matrix_size:
                    has_incomplete_flush_data = True
                    if not warned_missing_matrix:
                        add_warning(
                            "val.gcode.matrix_missing_tc",
                            old_tool=old_slicer_tool,
                            new_tool=new_slicer_tool,
                        )
                        warned_missing_matrix = True

            # Determine diameter for this slicer tool (fallback to effective_diameter)
            tool_diam = (
                parsed_diameters[new_slicer_tool]
                if (0 <= new_slicer_tool < len(parsed_diameters) and parsed_diameters[new_slicer_tool] > 0)
                else effective_diameter
            )

            # Calculate flush volume & purge length using ORIGINAL slicer indices
            vol_mm3 = compute_flush_volume(
                old_slicer_tool,
                new_slicer_tool,
                n_tools,
                matrix,
                vector,
                multiplier,
            )
            length_mm = volume_to_length_mm(vol_mm3, tool_diam)
            length_rounded = round(length_mm, round_digits)
            length_str = f"{length_rounded:.{round_digits}f}"

            # Map slicer tool to physical ACE slot AFTER purge calculation
            mapped_tool = map_tool(new_slicer_tool)
            event_orig_tools.append(new_slicer_tool)

            if body_lines and not body_lines[-1].endswith("\n"):
                body_lines[-1] += "\n"
            body_lines.append(f"{leading_ws}ACE_SET_PURGE_AMOUNT PURGELENGTH={length_str}\n")
            newline_char = "\n" if ln.endswith("\n") else ""
            body_lines.append(f"{leading_ws}T{mapped_tool}{trailing_rest}{newline_char}")

            toolchange_events.append(
                {
                    "index": toolchange_count,
                    "old_slicer_tool": old_slicer_tool,
                    "old_mapped_tool": map_tool(old_slicer_tool) if old_slicer_tool is not None else None,
                    "new_slicer_tool": new_slicer_tool,
                    "mapped_tool": mapped_tool,
                    "raw_volume_mm3": round(vol_mm3 / multiplier, 2) if multiplier > 0 else 0.0,
                    "volume_mm3": round(vol_mm3, 2),
                    "diameter_mm": round(tool_diam, 3),
                    "purge_length_mm": float(length_str),
                }
            )
            toolchange_count += 1
            current_slicer_tool = new_slicer_tool
            first_toolchange_seen = True
            continue

        body_lines.append(ln)

    # If cut_tip_after_print was requested and neither PRINT_END nor EXECUTABLE_BLOCK_END/CONFIG_BLOCK_START was seen
    if cut_tip_after_print and not post_cut_injected and not print_end_seen:
        if body_lines and not body_lines[-1].endswith("\n"):
            body_lines[-1] += "\n"
        post_cut_body_idx = len(body_lines)
        body_lines.append("CUT_TIP\n")
        post_cut_injected = True

    if len(used_slicer_tools_in_exec) > 1 and (not matrix or invalid_matrix_size):
        has_incomplete_flush_data = True
        if not warned_missing_matrix:
            add_warning("val.gcode.matrix_missing_multi")

    dedup_warnings = list(dict.fromkeys(warnings))
    dedup_warning_items: List[Dict[str, Any]] = []
    seen_item_sigs = set()
    for w_item in warning_items:
        sig = json.dumps(w_item, sort_keys=True)
        if sig not in seen_item_sigs:
            seen_item_sigs.add(sig)
            dedup_warning_items.append(w_item)

    flush_data_complete = (not invalid_matrix_size) and (not has_incomplete_flush_data)
    if not flush_data_complete:
        flush_status = "FLUSH_DATA_INCOMPLETE"
    elif dedup_warnings:
        flush_status = "POSTPROCESS_WARNING"
    else:
        flush_status = "POSTPROCESS_OK"

    info: Dict[str, Any] = {
        "flush_multiplier": multiplier,
        "tools": n_tools,
        "matrix_len": len(matrix),
        "vector_len": len(vector),
        "matrix": matrix,
        "vector": vector,
        "vector_summary": build_vector_summary(
            n_tools=n_tools,
            vector=vector,
            multiplier=multiplier,
            default_diameter=effective_diameter,
            parsed_diameters=parsed_diameters,
            tool_map=normalized_tool_map,
        ),
        "remapped_flush_schedule": build_remapped_flush_schedule(
            n_tools=n_tools,
            matrix=matrix,
            vector=vector,
            multiplier=multiplier,
            default_diameter=effective_diameter,
            parsed_diameters=parsed_diameters,
            tool_map=normalized_tool_map,
        ),
        "toolchanges": toolchange_count,
        "purge_commands_inserted": toolchange_count,
        "filament_diameter": effective_diameter,
        "parsed_diameters": parsed_diameters,
        "toolMap": normalized_tool_map,
        "bedLeveling": bool(bed_leveling),
        "g31_skipped": g31_skipped_count,
        "cutTipPost": bool(cut_tip_after_print),
        "forceInitialPurge": bool(force_initial_purge),
        "skipPurgeForAlreadyLoadedTool": bool(skip_purge_for_already_loaded_tool),
        "g9111StartSlicerTool": g9111_slicer_tool,
        "g9111MappedTool": g9111_mapped_tool,
        "used_slicer_tools": sorted(used_slicer_tools_in_exec),
        "flush_data_complete": flush_data_complete,
        "flush_status": flush_status,
        "warnings": dedup_warnings,
        "warning_items": dedup_warning_items,
        "events": toolchange_events,
    }

    if include_audit_header:
        tool_map_str = json.dumps(
            {f"T{k}": f"T{v}" for k, v in sorted(normalized_tool_map.items())},
            separators=(",", ":"),
        )
        rev_meta_str = json.dumps(
            {
                "g9111OrigTool": g9111_slicer_tool,
                "eventOrigTools": event_orig_tools,
                "origSkipPurgeVal": orig_skip_purge_val,
                "origSkipPurgeMode": orig_skip_purge_mode,
                "cutTipPostInjected": bool(post_cut_injected),
                "postCutBodyIdx": post_cut_body_idx,
                "origPrintEndCutTip": orig_print_end_cut_tip,
            },
            separators=(",", ":"),
        )
        audit_header = [
            "; === Kobra S1 ACE Pro Post-Processor Audit Header ===\n",
            (
                f"; multiplier={info['flush_multiplier']} "
                f"toolMap={tool_map_str} "
                f"bedLeveling={info['bedLeveling']} "
                f"toolchanges={info['toolchanges']}\n"
            ),
            (
                f"; tools={info['tools']} matrix={info['matrix_len']} "
                f"vector={info['vector_len']} diameter={info['filament_diameter']} "
                f"skipPurge={info['skipPurgeForAlreadyLoadedTool']} cutTipPost={info['cutTipPost']} "
                f"forceInitialPurge={info['forceInitialPurge']}\n"
            ),
            f"; @ORCA_REVERSIBLE_META={rev_meta_str}\n",
            "; === flush2len / ACE postprocess end ===\n",
        ]
        out_lines = audit_header + body_lines
    else:
        out_lines = body_lines

    return out_lines, info


# ---------------------------------------------------------------------------
# Moonraker REST Client (Pure Python Standard Library)
# ---------------------------------------------------------------------------
class MoonrakerClient:
    """
    Moonraker HTTP client using exclusively `urllib.request`, `urllib.error`, and `json`.
    Supports configurable base URL (default `http://192.168.8.80:7125`) with automatic
    port 80 fallback (`http://192.168.8.80`).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_MOONRAKER_URL,
        api_key: str = "",
        timeout: float = 5.0,
        enable_port80_fallback: bool = True,
    ) -> None:
        self.base_url = self.normalize_url(base_url)
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.enable_port80_fallback = enable_port80_fallback
        self.active_url = self.base_url

    @staticmethod
    def normalize_url(url: str) -> str:
        raw = (url or DEFAULT_MOONRAKER_URL).strip()
        if not raw.startswith(("http://", "https://")):
            raw = "http://" + raw
        return raw.rstrip("/")

    def get_candidate_urls(self) -> List[str]:
        """Return primary URL and optional port 80 / 7125 fallback URL."""
        candidates = [self.base_url]
        if not self.enable_port80_fallback:
            return candidates

        parsed = urllib.parse.urlsplit(self.base_url)
        hostname = parsed.hostname or "192.168.8.80"
        scheme = parsed.scheme or "http"
        port = parsed.port

        if port == 7125:
            fallback_80 = f"{scheme}://{hostname}"
            fallback_80_explicit = f"{scheme}://{hostname}:{FALLBACK_MOONRAKER_PORT}"
            for u in (fallback_80, fallback_80_explicit):
                if u not in candidates:
                    candidates.append(u)
        elif port in (None, 80):
            fallback_7125 = f"{scheme}://{hostname}:7125"
            if fallback_7125 not in candidates:
                candidates.append(fallback_7125)
        return candidates

    def _headers(self, content_type: Optional[str] = "application/json") -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    @staticmethod
    def _extract_http_error_detail(exc: urllib.error.HTTPError) -> str:
        err_detail = str(getattr(exc, "reason", None) or getattr(exc, "code", "HTTP Error"))
        try:
            err_body = exc.read().decode("utf-8", errors="replace").strip()
            if err_body:
                try:
                    parsed_err = json.loads(err_body)
                    if isinstance(parsed_err, dict):
                        err_obj = parsed_err.get("error")
                        if isinstance(err_obj, dict) and err_obj.get("message"):
                            return str(err_obj["message"])
                        if parsed_err.get("message"):
                            return str(parsed_err["message"])
                    return err_body
                except Exception:
                    return err_body
        except Exception:
            pass
        return err_detail

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        req_timeout = timeout or self.timeout
        body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
        last_error: Optional[Exception] = None

        for base in self.get_candidate_urls():
            url = f"{base}{path}"
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers=self._headers("application/json" if body_bytes is not None else None),
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                    self.active_url = base
                    raw = resp.read()
                    if not raw:
                        return {}
                    return json.loads(raw.decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as exc:
                self.active_url = base
                err_detail = self._extract_http_error_detail(exc)
                raise RuntimeError(f"Moonraker HTTP {exc.code} on {path}: {err_detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                continue

        raise ConnectionError(
            f"Moonraker unreachable at {', '.join(self.get_candidate_urls())}: {last_error}"
        )

    def query_ace_status(self) -> Dict[str, Any]:
        """
        Query live ACE Pro status via Moonraker `/printer/objects/query` using the exact
        status structure from `refs/ACEPRO` (`ace`, `ace_state`, `ace_instance_0`,
        `ace_instance_1`, `save_variables`, `output_pin ACE_Pro`, `temperature_sensor ace_temp`,
        plus `print_stats` for print-safety interlocks).
        Also supports `/server/ace/status` if the Moonraker ACE component is installed.
        """
        query_params = (
            "ace&ace_state&ace_instance_0&ace_instance_1"
            "&save_variables&output_pin%20ACE_Pro&temperature_sensor%20ace_temp&print_stats"
        )
        raw_response = self._request_json("GET", f"/printer/objects/query?{query_params}")
        status_block = (
            raw_response.get("result", {}).get("status", {})
            if isinstance(raw_response, dict)
            else {}
        )

        # If neither ace_instance_0 nor slots are populated in /printer/objects/query,
        # also attempt /server/ace/status (from refs/ACEPRO/ace_status_integration)
        ace_ext_data: Dict[str, Any] = {}
        has_instance_slots = any(
            isinstance(status_block.get(k), dict) and status_block.get(k, {}).get("slots")
            for k in ("ace_instance_0", "ace_instance_1", "ace")
        )
        if not has_instance_slots:
            try:
                ext_resp = self._request_json("GET", "/server/ace/status")
                ace_ext_data = ext_resp.get("result", ext_resp) if isinstance(ext_resp, dict) else {}
            except Exception:
                ace_ext_data = {}

        return self.normalize_ace_status(status_block, ace_ext_data)

    @classmethod
    def normalize_ace_status(
        cls,
        status_block: Dict[str, Any],
        ace_ext_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Normalize Moonraker `/printer/objects/query` and `/server/ace/status` payloads
        into a unified structure for the UI and Auto-Match engine.
        """
        ace_ext_data = ace_ext_data or {}
        ace_mgr = status_block.get("ace_state") or status_block.get("ace") or ace_ext_data.get("ace_manager") or {}
        save_vars = (status_block.get("save_variables") or {}).get("variables") or {}
        ace_pin = status_block.get("output_pin ACE_Pro") or {}
        temp_sensor = status_block.get("temperature_sensor ace_temp") or {}

        # Current & target global tool index
        current_index = ace_mgr.get(
            "current_index",
            ace_ext_data.get("current_index", save_vars.get("ace_current_index", -1)),
        )
        try:
            current_index = int(current_index)
        except (ValueError, TypeError):
            current_index = -1

        target_index = ace_mgr.get(
            "target_index",
            ace_ext_data.get("target_index", save_vars.get("ace_target_index", -1)),
        )
        try:
            target_index = int(target_index)
        except (ValueError, TypeError):
            target_index = -1

        ace_pro_enabled = bool(
            ace_mgr.get("ace_pro_enabled", ace_pin.get("value", 1))
        )

        # Collect instances (0 -> T0..T3, 1 -> T4..T7)
        raw_instances: Dict[int, Dict[str, Any]] = {}
        for idx in (0, 1):
            inst_obj = status_block.get(f"ace_instance_{idx}")
            if isinstance(inst_obj, dict) and inst_obj:
                raw_instances[idx] = inst_obj

        # Check if `ace` object itself is an instance status dict (with `slots` / `dryer`)
        ace_obj = status_block.get("ace")
        if isinstance(ace_obj, dict) and ("slots" in ace_obj or "dryer" in ace_obj or "dryer_status" in ace_obj):
            inst_idx = int(ace_obj.get("instance", 0))
            raw_instances.setdefault(inst_idx, ace_obj)

        # Check `ace_ext_data` (`/server/ace/status`)
        if isinstance(ace_ext_data.get("instances"), list):
            for item in ace_ext_data["instances"]:
                if isinstance(item, dict):
                    idx = int(item.get("index", item.get("instance", 0)))
                    raw_instances.setdefault(idx, item)
        elif isinstance(ace_ext_data.get("slots"), list):
            idx = int(ace_ext_data.get("instance_index", ace_ext_data.get("instance", 0)))
            raw_instances.setdefault(idx, ace_ext_data)

        # Fallback to persistent inventory in save_variables (`ace_inventory_0`, `ace_inventory_1`)
        for idx in (0, 1):
            if idx not in raw_instances:
                saved_inv = save_vars.get(f"ace_inventory_{idx}")
                if isinstance(saved_inv, list) and saved_inv:
                    raw_instances[idx] = {
                        "instance": idx,
                        "status": "ready",
                        "connection_state": "connected",
                        "slots": [
                            {
                                "index": s_idx,
                                "tool": idx * SLOTS_PER_ACE_UNIT + s_idx,
                                "status": slot.get("status", "empty"),
                                "color": slot.get("color", [0, 0, 0]),
                                "material": slot.get("material", ""),
                                "temp": slot.get("temp", 0),
                                "rfid": slot.get("rfid", False),
                                "sku": slot.get("sku", ""),
                                "brand": slot.get("brand", ""),
                            }
                            for s_idx, slot in enumerate(saved_inv[:SLOTS_PER_ACE_UNIT])
                            if isinstance(slot, dict)
                        ],
                    }

        # Fallback to `lanes` array from `ace_state` (`MoonrakerLaneSyncAdapter`)
        lanes = ace_mgr.get("lanes")
        if not raw_instances and isinstance(lanes, list) and lanes:
            inst_count = max(1, (len(lanes) + SLOTS_PER_ACE_UNIT - 1) // SLOTS_PER_ACE_UNIT)
            for idx in range(min(2, inst_count)):
                chunk = lanes[idx * SLOTS_PER_ACE_UNIT : (idx + 1) * SLOTS_PER_ACE_UNIT]
                raw_instances[idx] = {
                    "instance": idx,
                    "status": "ready",
                    "connection_state": "connected",
                    "slots": [
                        {
                            "index": s_idx,
                            "tool": idx * SLOTS_PER_ACE_UNIT + s_idx,
                            "status": "ready" if lane.get("material") else "empty",
                            "color": list(hex_to_rgb(lane.get("color", "#000000"))),
                            "material": lane.get("material", ""),
                            "temp": lane.get("nozzle_temp", 0),
                            "sku": lane.get("sku", ""),
                            "brand": lane.get("vendor", ""),
                        }
                        for s_idx, lane in enumerate(chunk)
                        if isinstance(lane, dict)
                    ],
                }

        # Ensure at least instance 0 is represented
        if not raw_instances:
            raw_instances[0] = {
                "instance": 0,
                "status": "unknown",
                "connection_state": "unknown",
                "slots": [
                    {
                        "index": i,
                        "tool": i,
                        "status": "empty",
                        "color": [0, 0, 0],
                        "material": "",
                        "temp": 0,
                        "rfid": False,
                    }
                    for i in range(SLOTS_PER_ACE_UNIT)
                ],
            }

        normalized_slots: List[Dict[str, Any]] = []
        primary_inst = raw_instances.get(0) or list(raw_instances.values())[0]

        for inst_idx in sorted(raw_instances.keys()):
            inst_data = raw_instances[inst_idx]
            slots_list = inst_data.get("slots") or []
            for s_idx in range(SLOTS_PER_ACE_UNIT):
                raw_slot = (
                    slots_list[s_idx]
                    if s_idx < len(slots_list) and isinstance(slots_list[s_idx], dict)
                    else {}
                )
                tool_num = raw_slot.get("tool")
                if not isinstance(tool_num, int):
                    tool_num = inst_idx * SLOTS_PER_ACE_UNIT + s_idx

                raw_status = str(raw_slot.get("status") or "empty").strip().lower()
                if tool_num == current_index and raw_status not in ("empty",):
                    display_status = "loaded"
                elif raw_status in ("ready", "loaded", "empty", "busy"):
                    display_status = raw_status
                else:
                    display_status = "empty" if not raw_slot.get("material") and not raw_slot.get("type") else "ready"

                color_val = raw_slot.get("color", [0, 0, 0])
                hex_col = rgb_to_hex(color_val)
                rgb_col = list(hex_to_rgb(hex_col))
                material = str(raw_slot.get("material") or raw_slot.get("type") or "").strip()
                temp = int(raw_slot.get("temp") or 0)
                rfid_raw = raw_slot.get("rfid", False)
                rfid_identified = bool(rfid_raw == 2 or rfid_raw is True)

                normalized_slots.append(
                    {
                        "instance": inst_idx,
                        "index": s_idx,
                        "tool": tool_num,
                        "label": f"T{tool_num}",
                        "status": display_status,
                        "raw_status": raw_status,
                        "is_loaded": tool_num == current_index,
                        "hex_color": hex_col,
                        "color": rgb_col,
                        "material": material,
                        "temp": temp,
                        "rfid": rfid_identified,
                        "sku": str(raw_slot.get("sku") or ""),
                        "brand": str(raw_slot.get("brand") or ""),
                        "park_state": str(raw_slot.get("park_state") or "unknown"),
                    }
                )

        # Normalize Dryer status across instances (0 = ACE 1, 1 = ACE 2)
        dryer_instances: Dict[int, Dict[str, Any]] = {}
        for inst_idx in sorted(raw_instances.keys()):
            inst_obj = raw_instances[inst_idx]
            rd = inst_obj.get("dryer") or inst_obj.get("dryer_status") or {}
            if not isinstance(rd, dict):
                rd = {}
            d_stat = str(rd.get("status") or "stop").strip().lower()
            d_temp = int(rd.get("target_temp") or 0)
            d_dur = int(float(rd.get("duration") or 0))
            d_rem_raw = float(rd.get("remain_time") or 0.0)
            if d_rem_raw > 1440 or (d_dur > 0 and d_rem_raw > d_dur * 1.5 and d_rem_raw > 60):
                d_rem_min = round(d_rem_raw / 60.0, 1)
            else:
                d_rem_min = round(d_rem_raw, 1)
            dryer_instances[inst_idx] = {
                "instance": inst_idx,
                "status": d_stat,
                "active": d_stat == "drying",
                "target_temp": d_temp,
                "duration": d_dur,
                "remain_time_min": d_rem_min,
            }

        active_dryers = [d for d in dryer_instances.values() if d["active"]]
        summary_dryer = (
            active_dryers[0]
            if active_dryers
            else (
                dryer_instances.get(0)
                or (list(dryer_instances.values())[0] if dryer_instances else {
                    "status": "stop",
                    "active": False,
                    "target_temp": 0,
                    "duration": 0,
                    "remain_time_min": 0.0,
                })
            )
        )
        dryer_status_str = summary_dryer["status"]
        dryer_target_temp = summary_dryer["target_temp"]
        dryer_duration = summary_dryer["duration"]
        remain_minutes = summary_dryer["remain_time_min"]

        ambient_temp = primary_inst.get("temp")
        if ambient_temp is None:
            ambient_temp = temp_sensor.get("temperature", 0)

        print_stats = status_block.get("print_stats") or {}
        if not isinstance(print_stats, dict):
            print_stats = {}
        print_state = str(print_stats.get("state") or "standby").strip().lower()
        print_filename = str(print_stats.get("filename") or "")
        is_printing = print_state in ("printing", "paused")

        return {
            "connected": str(primary_inst.get("connection_state", "connected")).lower() != "disconnected",
            "device_status": str(primary_inst.get("status") or "ready"),
            "connection_state": str(primary_inst.get("connection_state") or "connected"),
            "model": str(primary_inst.get("model") or "Anycubic Color Engine Pro"),
            "firmware": str(primary_inst.get("firmware") or "Unknown"),
            "ace_pro_enabled": ace_pro_enabled,
            "ace_instances": max(len(raw_instances), int(ace_mgr.get("ace_instances", 1) or 1)),
            "current_index": current_index,
            "target_index": target_index,
            "ambient_temp": float(ambient_temp or 0.0),
            "humidity": primary_inst.get("humidity"),
            "fan_speed": int(primary_inst.get("fan_speed") or 0),
            "feed_assist_slot": int(primary_inst.get("feed_assist_slot", -1)),
            "max_dryer_temperature": float(primary_inst.get("max_dryer_temperature") or 60.0),
            "print_state": print_state,
            "print_filename": print_filename,
            "is_printing": is_printing,
            "dryer": {
                "status": dryer_status_str,
                "active": bool(active_dryers),
                "target_temp": dryer_target_temp,
                "duration": dryer_duration,
                "remain_time_min": remain_minutes,
                "active_instances": [d["instance"] for d in active_dryers],
                "instances": dryer_instances,
            },
            "slots": normalized_slots,
        }

    @staticmethod
    def _assert_print_safe_status(ace_status: Dict[str, Any], action_name: str, lang: Optional[str] = None) -> None:
        if ace_status.get("is_printing"):
            p_state = ace_status.get("print_state", "printing")
            raise RuntimeError(
                t("safety.lock.error", lang=lang, action=action_name, state=p_state)
            )

    def ensure_print_safe(self, action_name: str) -> Dict[str, Any]:
        """Verify live Moonraker `print_stats` is not printing/paused before mutating hardware."""
        ace_status = self.query_ace_status()
        self._assert_print_safe_status(ace_status, action_name)
        return ace_status

    # --- Moonraker G-Code Execution (`/printer/gcode/script`) ---
    def send_gcode(self, script: str, allow_while_printing: bool = False) -> Dict[str, Any]:
        """Execute a Klipper G-code script or macro via `/printer/gcode/script`."""
        clean_script = script.strip()
        if not clean_script:
            raise ValueError("G-code script must not be empty")
        if not allow_while_printing:
            self.ensure_print_safe(f"G-Code '{clean_script}'")
        return self._request_json(
            "POST",
            "/printer/gcode/script",
            payload={"script": clean_script},
            timeout=max(self.timeout, 15.0),
        )

    def trigger_cut_tip(self) -> Dict[str, Any]:
        """Trigger `CUT_TIP` macro defined in `printer_KS1.cfg`."""
        return self.send_gcode("CUT_TIP")

    def trigger_bed_leveling(self, mode: str = "G31") -> Dict[str, Any]:
        """
        Trigger Bed Leveling via Moonraker:
        - `G31` (or `BED_MESH_CALIBRATE PROFILE=adaptive ADAPTIVE=1` from `printer_KS1.cfg`).
        """
        cmd = "BED_MESH_CALIBRATE PROFILE=adaptive ADAPTIVE=1" if mode.upper() == "BED_MESH_CALIBRATE" else "G31"
        return self.send_gcode(cmd)

    def start_dryer(self, temp_c: int = 50, duration_min: int = 240, instance: Optional[int] = None) -> Dict[str, Any]:
        """Start ACE Pro filament dryer (`ACE_START_DRYING TEMP=<temp> DURATION=<min> [INSTANCE=<n>]`)."""
        temp_clamped = max(20, min(70, int(round(float(temp_c)))))
        dur_clamped = max(1, min(1440, int(round(float(duration_min)))))
        cmd = f"ACE_START_DRYING TEMP={temp_clamped} DURATION={dur_clamped}"
        if instance is not None:
            cmd += f" INSTANCE={int(instance)}"
        return self.send_gcode(cmd, allow_while_printing=True)

    def stop_dryer(self, instance: Optional[int] = None) -> Dict[str, Any]:
        """Stop ACE Pro filament dryer (`ACE_STOP_DRYING [INSTANCE=<n>]`)."""
        cmd = "ACE_STOP_DRYING"
        if instance is not None:
            cmd += f" INSTANCE={int(instance)}"
        return self.send_gcode(cmd, allow_while_printing=True)

    def load_slot(self, tool: int) -> Dict[str, Any]:
        """Load physical ACE slot T<tool> into toolhead (`ACE_CHANGE_TOOL TOOL=<n>`)."""
        return self.send_gcode(f"ACE_CHANGE_TOOL TOOL={int(tool)}")

    def unload_slot(self, tool: Optional[int] = None, full_unload: bool = False) -> Dict[str, Any]:
        """
        Unload filament from toolhead or specific ACE slot:
        - `ACE_FULL_UNLOAD TOOL=<n>` if `full_unload=True`
        - `ACE_SMART_UNLOAD TOOL=<n>` if a specific `tool` is provided
        - `ACE_CHANGE_TOOL TOOL=-1` (alias `TR`) to unload active toolhead filament
        """
        if full_unload and tool is not None and tool >= 0:
            return self.send_gcode(f"ACE_FULL_UNLOAD TOOL={int(tool)}")
        if tool is not None and tool >= 0:
            return self.send_gcode(f"ACE_SMART_UNLOAD TOOL={int(tool)}")
        return self.send_gcode("ACE_CHANGE_TOOL TOOL=-1")

    def feed_slot(self, tool: int, length_mm: int = 50, speed_mms: int = 25) -> Dict[str, Any]:
        """Feed filament on ACE slot (`ACE_FEED T=<tool> LENGTH=<mm> SPEED=<mm/s>`)."""
        return self.send_gcode(
            f"ACE_FEED T={int(tool)} LENGTH={max(1, int(length_mm))} SPEED={max(1, int(speed_mms))}"
        )

    def retract_slot(self, tool: int, length_mm: int = 50, speed_mms: int = 25) -> Dict[str, Any]:
        """Retract filament on ACE slot (`ACE_RETRACT T=<tool> LENGTH=<mm> SPEED=<mm/s>`)."""
        return self.send_gcode(
            f"ACE_RETRACT T={int(tool)} LENGTH={max(1, int(length_mm))} SPEED={max(1, int(speed_mms))}"
        )

    # --- Multipart G-Code Upload (`/server/files/upload`) ---
    @staticmethod
    def build_multipart_upload(
        filename: str,
        content_bytes: bytes,
        start_print: bool = False,
        root: str = "gcodes",
    ) -> Tuple[bytes, str]:
        """
        Construct an RFC 7578 `multipart/form-data` body using only the Python stdlib.
        Returns `(body_bytes, content_type_header)`.
        """
        boundary = f"----OrcaKobraS1ACEBoundary{uuid.uuid4().hex}"
        crlf = b"\r\n"
        parts: List[bytes] = []

        def add_field(name: str, value: str) -> None:
            parts.append(f"--{boundary}".encode("utf-8"))
            parts.append(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"))
            parts.append(b"")
            parts.append(value.encode("utf-8"))

        add_field("root", root)
        add_field("print", "true" if start_print else "false")

        safe_filename = os.path.basename(filename or "orca_kobra_s1_ace.gcode").replace('"', "_")
        parts.append(f"--{boundary}".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"'.encode("utf-8")
        )
        parts.append(b"Content-Type: application/octet-stream")
        parts.append(b"")
        parts.append(content_bytes)
        parts.append(f"--{boundary}--".encode("utf-8"))
        parts.append(b"")

        body = crlf.join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"
        return body, content_type

    def upload_gcode(
        self,
        filename: str,
        gcode_content: str,
        start_print: bool = False,
    ) -> Dict[str, Any]:
        """Upload patched G-code to Moonraker `/server/files/upload` via Multipart-POST."""
        if start_print:
            self.ensure_print_safe("Moonraker Upload & Druckstart")
        content_bytes = gcode_content.encode("utf-8")
        body, content_type = self.build_multipart_upload(
            filename=filename,
            content_bytes=content_bytes,
            start_print=start_print,
        )
        last_error: Optional[Exception] = None

        for base in self.get_candidate_urls():
            url = f"{base}/server/files/upload"
            headers = self._headers(content_type=content_type)
            headers["Content-Length"] = str(len(body))
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=max(self.timeout, 30.0)) as resp:
                    self.active_url = base
                    raw = resp.read()
                    if not raw:
                        return {"result": {"item": {"path": filename}, "print_started": start_print}}
                    return json.loads(raw.decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as exc:
                self.active_url = base
                err_detail = self._extract_http_error_detail(exc)
                raise RuntimeError(f"Moonraker upload HTTP {exc.code}: {err_detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                continue

        raise ConnectionError(
            f"Failed to upload G-code to Moonraker ({', '.join(self.get_candidate_urls())}): {last_error}"
        )


# ---------------------------------------------------------------------------
# Persistent Plugin Configuration & Shared Runtime State
# ---------------------------------------------------------------------------
class PluginState:
    """Manages plugin settings, Moonraker URL, pre-print toggles, and active G-code state."""

    FALLBACK_CONFIG_PATH = os.path.join(
        os.path.expanduser("~"), ".local", "share", "kobra_s1_ace_plugin", "state.json"
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hw_action_lock = threading.Lock()
        self.language: str = DEFAULT_LANGUAGE
        self.moonraker_url: str = DEFAULT_MOONRAKER_URL
        self.api_key: str = ""
        self.enable_port80_fallback: bool = True
        self.bed_leveling: bool = True
        self.skip_purge_for_already_loaded_tool: bool = True
        self.cut_tip_after_print: bool = False
        self.force_initial_purge: bool = False
        self.start_print_immediately: bool = False
        self.dryer_unit: str = "all"
        self.dryer_temp: int = 50
        self.dryer_duration: int = 240
        self.filament_diameter: float = DEFAULT_FILAMENT_DIAMETER
        self.tool_map: Dict[int, int] = {i: i for i in range(MAX_ACE_SLOTS)}
        self.tool_map_sources: Dict[int, str] = {i: "default" for i in range(MAX_ACE_SLOTS)}
        self.auto_register_pipeline: bool = True
        self._pipeline_registered_keys: Set[Tuple[str, str]] = set()
        self.last_gcode_path: str = ""
        self.last_gcode_source: str = ""
        self.pending_preprint_open: bool = False
        self.last_postprocess_info: Dict[str, Any] = {}
        self.slicer_filaments: List[Dict[str, Any]] = [
            {
                "slicer_tool": 0,
                "label": "Extruder 0 (T0)",
                "color": "#FFFFFF",
                "rgb": [255, 255, 255],
                "material": "PETG",
                "used": True,
            },
            {
                "slicer_tool": 1,
                "label": "Extruder 1 (T1)",
                "color": "#000000",
                "rgb": [0, 0, 0],
                "material": "PETG",
                "used": True,
            },
        ]
        self.filament_signature: str = ""
        self.mapping_warnings: List[str] = []
        self.material_mismatch_confirmed: bool = False
        self.last_job_metadata: Dict[str, Any] = {}
        self.last_ace_status: Dict[str, Any] = {}
        if not HAS_ORCA_RUNTIME:
            self.load_config()

    def set_language(self, lang: Any, save: bool = True) -> str:
        """
        Update ONLY the selected UI language (`en` or `de`) and persist state without
        modifying or overwriting any other plugin setting.
        """
        with self._lock:
            self.language = normalize_language(lang)
            if save:
                self.save_config()
            return self.language

    def update_slicer_filaments(self, filaments: List[Dict[str, Any]]) -> bool:
        """
        Update active slicer filaments and reset stale `tool_map` to 1:1 if the
        filament configuration or order changed. Returns True if `tool_map` was reset.
        """
        with self._lock:
            new_sig = compute_filament_signature(filaments)
            reset_occurred = False
            self.mapping_warnings = []
            if self.filament_signature and new_sig != self.filament_signature:
                old_len = len(self.slicer_filaments)
                new_len = len(filaments)
                self.tool_map = {i: i for i in range(MAX_ACE_SLOTS)}
                self.tool_map_sources = {i: "default" for i in range(MAX_ACE_SLOTS)}
                self.material_mismatch_confirmed = False
                self.mapping_warnings.append(
                    t("map.warn.order_reset", lang=self.language, old_len=old_len, new_len=new_len)
                )
                reset_occurred = True
            self.slicer_filaments = list(filaments)
            self.filament_signature = new_sig
            return reset_occurred

    def resolve_config_path(self) -> Optional[str]:
        """
        Resolve plugin state file inside `orca.host.plugin.storage()` when running in
        OrcaSlicer (which lives inside `data_dir()` and avoids `~/.config` `"conf"` blocks).
        Returns `None` if called at import time before OrcaSlicer's host is ready.
        """
        if HAS_ORCA_RUNTIME:
            try:
                host_mod = getattr(orca, "host", None)
                plugin_host = getattr(host_mod, "plugin", None)
                if plugin_host is not None and hasattr(plugin_host, "storage"):
                    storage_dir = plugin_host.storage()
                    if storage_dir:
                        return os.path.join(storage_dir, "state.json")
            except Exception:
                return None
            return None
        return self.FALLBACK_CONFIG_PATH

    def resolve_storage_dir(self) -> str:
        """
        Resolve writable storage folder inside `orca.host.plugin.storage()` (or fallback dir
        outside OrcaSlicer / inside test stubs). Returns `""` if Orca runtime storage is
        unavailable so callers never probe external home or system directories.
        """
        cfg_path = self.resolve_config_path()
        if cfg_path:
            return os.path.dirname(cfg_path)
        if HAS_ORCA_RUNTIME:
            return ""
        return os.path.dirname(self.FALLBACK_CONFIG_PATH)

    def load_config(self) -> None:
        with self._lock:
            config_path = self.resolve_config_path()
            if not config_path:
                return
            try:
                if os.path.isfile(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        self.apply_dict(data, save=False)
            except Exception:
                pass

    def save_config(self) -> None:
        with self._lock:
            config_path = self.resolve_config_path()
            if not config_path:
                return
            try:
                parent = os.path.dirname(config_path)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(self.to_dict(), f, indent=2)
            except Exception:
                pass

    def apply_dict(self, data: Dict[str, Any], save: bool = True) -> None:
        with self._lock:
            if "language" in data:
                self.language = normalize_language(data["language"])
            if "moonraker_url" in data and data["moonraker_url"]:
                self.moonraker_url = MoonrakerClient.normalize_url(str(data["moonraker_url"]))
            if "api_key" in data:
                self.api_key = str(data["api_key"] or "")
            if "enable_port80_fallback" in data:
                self.enable_port80_fallback = bool(data["enable_port80_fallback"])
            if "auto_register_pipeline" in data:
                self.auto_register_pipeline = bool(data["auto_register_pipeline"])
            if "bed_leveling" in data:
                self.bed_leveling = bool(data["bed_leveling"])
            if "skip_purge_for_already_loaded_tool" in data:
                self.skip_purge_for_already_loaded_tool = bool(data["skip_purge_for_already_loaded_tool"])
            if "cut_tip_after_print" in data:
                self.cut_tip_after_print = bool(data["cut_tip_after_print"])
            if "force_initial_purge" in data:
                self.force_initial_purge = bool(data["force_initial_purge"])
            if "start_print_immediately" in data:
                self.start_print_immediately = bool(data["start_print_immediately"])
            if "dryer_unit" in data:
                raw_u = str(data["dryer_unit"] or "all").strip().lower()
                self.dryer_unit = raw_u if raw_u in ("all", "0", "1") else "all"
            if "dryer_temp" in data:
                try:
                    t_val = int(round(float(data["dryer_temp"])))
                    self.dryer_temp = max(20, min(70, t_val))
                except (ValueError, TypeError):
                    pass
            if "dryer_duration" in data:
                try:
                    d_val = int(round(float(data["dryer_duration"])))
                    self.dryer_duration = max(1, min(1440, d_val))
                except (ValueError, TypeError):
                    pass
            if "filament_diameter" in data:
                try:
                    val = float(data["filament_diameter"])
                    if val > 0:
                        self.filament_diameter = val
                except (ValueError, TypeError):
                    pass
            explicit_sources = "tool_map_sources" in data and isinstance(data["tool_map_sources"], dict)
            if "tool_map" in data and isinstance(data["tool_map"], dict):
                new_map: Dict[int, int] = {}
                for k, v in data["tool_map"].items():
                    try:
                        new_map[int(k)] = int(v)
                    except (ValueError, TypeError):
                        pass
                if new_map:
                    map_changed = False
                    for k, v in new_map.items():
                        if self.tool_map.get(k) != v:
                            map_changed = True
                            if not explicit_sources:
                                self.tool_map_sources[k] = "manual"
                    self.tool_map.update(new_map)
                    if map_changed and "material_mismatch_confirmed" not in data:
                        self.material_mismatch_confirmed = False
            if explicit_sources:
                for k, v in data["tool_map_sources"].items():
                    try:
                        src = str(v).strip().lower()
                        if src in ("default", "auto", "manual"):
                            self.tool_map_sources[int(k)] = src
                    except (ValueError, TypeError):
                        pass
            if "material_mismatch_confirmed" in data:
                self.material_mismatch_confirmed = bool(data["material_mismatch_confirmed"])
            if "last_job_metadata" in data and isinstance(data["last_job_metadata"], dict):
                self.last_job_metadata = dict(data["last_job_metadata"])
            if "last_gcode_path" in data and data["last_gcode_path"]:
                self.last_gcode_path = str(data["last_gcode_path"])
            if "last_gcode_source" in data and isinstance(data["last_gcode_source"], str):
                src_clean = data["last_gcode_source"].strip()
                if src_clean:
                    self.last_gcode_source = src_clean
            if "cleared_before_mtime_ns" in data:
                try:
                    self.cleared_before_mtime_ns = max(0, int(data["cleared_before_mtime_ns"] or 0))
                except (ValueError, TypeError):
                    pass
            if "slicer_filaments" in data and isinstance(data["slicer_filaments"], list):
                valid_fils = [dict(f) for f in data["slicer_filaments"] if isinstance(f, dict)]
                if valid_fils:
                    self.slicer_filaments = valid_fils
            if "last_postprocess_info" in data and isinstance(data["last_postprocess_info"], dict):
                self.last_postprocess_info = dict(data["last_postprocess_info"])
            if "filament_signature" in data and isinstance(data["filament_signature"], str) and data["filament_signature"]:
                self.filament_signature = data["filament_signature"]
            elif "slicer_filaments" in data:
                self.filament_signature = compute_filament_signature(self.slicer_filaments)
            if "mapping_warnings" in data and isinstance(data["mapping_warnings"], list):
                self.mapping_warnings = [str(w) for w in data["mapping_warnings"]]

            if save:
                self.save_config()

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            gcode_path = self.last_gcode_path
            job_meta = dict(self.last_job_metadata)
            payload = {
                "language": self.language,
                "moonraker_url": self.moonraker_url,
                "api_key": self.api_key,
                "enable_port80_fallback": self.enable_port80_fallback,
                "auto_register_pipeline": bool(self.auto_register_pipeline),
                "bed_leveling": self.bed_leveling,
                "skip_purge_for_already_loaded_tool": self.skip_purge_for_already_loaded_tool,
                "cut_tip_after_print": self.cut_tip_after_print,
                "force_initial_purge": self.force_initial_purge,
                "start_print_immediately": self.start_print_immediately,
                "dryer_unit": self.dryer_unit,
                "dryer_temp": self.dryer_temp,
                "dryer_duration": self.dryer_duration,
                "filament_diameter": self.filament_diameter,
                "tool_map": {str(k): v for k, v in sorted(self.tool_map.items())},
                "tool_map_sources": {str(k): v for k, v in sorted(self.tool_map_sources.items())},
                "material_mismatch_confirmed": self.material_mismatch_confirmed,
                "last_job_metadata": job_meta,
                "last_gcode_path": gcode_path,
                "last_gcode_source": self.last_gcode_source,
                "cleared_before_mtime_ns": int(getattr(self, "cleared_before_mtime_ns", 0) or 0),
                "pending_preprint_open": bool(self.pending_preprint_open),
                "last_postprocess_info": dict(self.last_postprocess_info),
                "slicer_filaments": list(self.slicer_filaments),
                "filament_signature": self.filament_signature,
                "mapping_warnings": list(self.mapping_warnings),
            }
        payload["stale_model_info"] = check_stale_gcode_model(
            gcode_path=gcode_path,
            filename=str(job_meta.get("filename") or ""),
        )
        return payload

    def get_client(self) -> MoonrakerClient:
        with self._lock:
            return MoonrakerClient(
                base_url=self.moonraker_url,
                api_key=self.api_key,
                enable_port80_fallback=self.enable_port80_fallback,
            )


GLOBAL_STATE = PluginState()


def check_print_safety(
    client: MoonrakerClient,
    action_name: str,
    state: Optional[PluginState] = None,
) -> Dict[str, Any]:
    """
    Verify via live Moonraker `print_stats` that no print is currently `printing` or `paused`
    before allowing hardware/filament/print-start actions.
    Raises `RuntimeError` if blocked.
    """
    ace_status = client.query_ace_status()
    st = state or GLOBAL_STATE
    with st._lock:
        st.last_ace_status = dict(ace_status)
    MoonrakerClient._assert_print_safe_status(ace_status, action_name)
    return ace_status


_ACTIVE_PAGE_LOCK = threading.Lock()
_ACTIVE_PAGE_CAPABILITY: Any = None


def compute_contrast_text_color(hex_color: str) -> str:
    """
    Compute accessible foreground text color from filament hex using perceived
    luminance `L = 0.299*R + 0.587*G + 0.114*B`:
    returns `#111827` for light backgrounds (`L > 150`) and `#FFFFFF` for dark (`L <= 150`).
    """
    cleaned = rgb_to_hex(hex_color or "#334155").lstrip("#")
    if len(cleaned) != 6:
        return "#FFFFFF"
    try:
        r = int(cleaned[0:2], 16)
        g = int(cleaned[2:4], 16)
        b = int(cleaned[4:6], 16)
    except ValueError:
        return "#FFFFFF"
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#111827" if luminance > 150.0 else "#FFFFFF"


def rgb_to_hsl(r: int, g: int, b: int) -> Tuple[float, float, float]:
    """Convert 8-bit RGB channels `(r, g, b)` to normalized `(h, s, l)` in `[0.0, 1.0]`."""
    rn = max(0.0, min(1.0, float(r) / 255.0))
    gn = max(0.0, min(1.0, float(g) / 255.0))
    bn = max(0.0, min(1.0, float(b) / 255.0))
    max_c = max(rn, gn, bn)
    min_c = min(rn, gn, bn)
    l_val = 0.5 * (max_c + min_c)
    diff = max_c - min_c
    if diff <= 1e-9:
        return (0.0, 0.0, l_val)
    denom = (2.0 - max_c - min_c) if l_val > 0.5 else (max_c + min_c)
    s_val = diff / denom if denom > 1e-9 else 0.0
    if max_c == rn:
        h_val = (gn - bn) / diff + (6.0 if gn < bn else 0.0)
    elif max_c == gn:
        h_val = (bn - rn) / diff + 2.0
    else:
        h_val = (rn - gn) / diff + 4.0
    return ((h_val / 6.0) % 1.0, s_val, l_val)


def _hue_to_rgb_channel(p: float, q: float, t_val: float) -> float:
    t_mod = t_val % 1.0
    if t_mod < (1.0 / 6.0):
        return p + (q - p) * 6.0 * t_mod
    if t_mod < 0.5:
        return q
    if t_mod < (2.0 / 3.0):
        return p + (q - p) * (2.0 / 3.0 - t_mod) * 6.0
    return p


def hsl_to_rgb(h: float, s: float, l: float) -> Tuple[int, int, int]:
    """Convert normalized `(h, s, l)` in `[0.0, 1.0]` to clamped 8-bit `(r, g, b)`."""
    h_n = h % 1.0
    s_n = max(0.0, min(1.0, float(s)))
    l_n = max(0.0, min(1.0, float(l)))
    if s_n <= 1e-9:
        v = max(0, min(255, int(round(l_n * 255.0))))
        return (v, v, v)
    q = (l_n * (1.0 + s_n)) if l_n < 0.5 else (l_n + s_n - l_n * s_n)
    p = 2.0 * l_n - q
    r = int(round(_hue_to_rgb_channel(p, q, h_n + 1.0 / 3.0) * 255.0))
    g = int(round(_hue_to_rgb_channel(p, q, h_n) * 255.0))
    b = int(round(_hue_to_rgb_channel(p, q, h_n - 1.0 / 3.0) * 255.0))
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def _expected_shaded_lightness(l_val: float) -> float:
    """Estimate typical Lambertian 3D thumbnail mid-tone lightness for filament base lightness."""
    if l_val < 0.15:
        return 0.13 + l_val * 0.5
    if l_val > 0.85:
        return 0.72 + (l_val - 0.85) * 0.2
    return l_val * 0.85


def build_thumbnail_source_palette(slicer_filaments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build normalized palette entries for active Slicer filaments (or all if usage flags absent)."""
    fils = [f for f in (slicer_filaments or []) if isinstance(f, dict)]
    active = [f for f in fils if f.get("used", True)]
    chosen = active if active else fils
    palette: List[Dict[str, Any]] = []
    for idx, f in enumerate(chosen):
        hex_col = rgb_to_hex(f.get("color") or "#64748B")
        r, g, b = hex_to_rgb(hex_col)
        h_v, s_v, l_v = rgb_to_hsl(r, g, b)
        is_chromatic = (s_v >= 0.14) and (0.05 < l_v < 0.96)
        palette.append(
            {
                "slicer_tool": int(f.get("slicer_tool", idx)),
                "hex": hex_col,
                "rgb": (r, g, b),
                "hsl": (h_v, s_v, l_v),
                "is_chromatic": is_chromatic,
                "exp_l": _expected_shaded_lightness(l_v),
            }
        )
    return palette


def match_thumbnail_pixel_extruder(
    r: int,
    g: int,
    b: int,
    a: int,
    palette: List[Dict[str, Any]],
) -> int:
    """
    Match an RGBA thumbnail pixel against the Slicer filament palette.
    Returns matched `slicer_tool` (`0..7`), or `-1` for transparent / neutral background pixels.
    """
    if a < 24 or not palette:
        return -1
    p_h, p_s, p_l = rgb_to_hsl(r, g, b)
    p_chromatic = (p_s >= 0.14) and (0.05 < p_l < 0.96)

    best_tool = -1
    best_score = float("inf")

    if p_chromatic:
        for entry in palette:
            if not entry.get("is_chromatic"):
                continue
            f_h, f_s, _ = entry["hsl"]
            dh = abs(p_h - f_h)
            if dh > 0.5:
                dh = 1.0 - dh
            dh_deg = dh * 360.0
            if dh_deg > 48.0:
                continue
            ds = abs(p_s - f_s)
            dl = abs(p_l - float(entry["exp_l"]))
            score = ((dh_deg / 45.0) ** 2) * 1.8 + ((ds / 0.65) ** 2) * 0.45 + ((dl / 0.75) ** 2) * 0.35
            if score < best_score and score <= 1.35:
                best_score = score
                best_tool = int(entry["slicer_tool"])
        if best_tool >= 0:
            return best_tool
        if p_s >= 0.20:
            return -1

    # Neutral pixel or cool/warm low-saturation shadow fallback (only if palette defines neutral filament)
    for entry in palette:
        if entry.get("is_chromatic"):
            continue
        dl_exp = abs(p_l - float(entry["exp_l"]))
        if dl_exp > 0.48:
            continue
        score = dl_exp + p_s * 0.8
        if score < best_score:
            best_score = score
            best_tool = int(entry["slicer_tool"])
    return best_tool


def recolor_thumbnail_pixel(
    r: int,
    g: int,
    b: int,
    source_hex: str,
    target_hex: str,
) -> Tuple[int, int, int]:
    """
    Recolor a single shaded thumbnail pixel from `source_hex` to `target_hex`,
    shifting hue/saturation while preserving relative 3D shading luminance.
    Returns `(r, g, b)` unchanged when `source_hex == target_hex`.
    """
    src_norm = rgb_to_hex(source_hex or "#64748B")
    tgt_norm = rgb_to_hex(target_hex or src_norm)
    if src_norm == tgt_norm:
        return (int(r), int(g), int(b))

    _, p_s, p_l = rgb_to_hsl(r, g, b)
    sr, sg, sb = hex_to_rgb(src_norm)
    _, s_s, s_l = rgb_to_hsl(sr, sg, sb)
    tr, tg, tb = hex_to_rgb(tgt_norm)
    t_h, t_s, t_l = rgb_to_hsl(tr, tg, tb)

    if s_l < 0.16:
        k = max(0.28, min(1.15, (p_l + 0.05) / max(0.22, s_l + 0.14)))
    else:
        k = max(0.25, min(1.45, p_l / max(0.16, s_l)))

    if t_l < 0.14:
        eff_tl = 0.13 + t_l * 0.35
    elif t_l > 0.90:
        eff_tl = 0.86
    else:
        eff_tl = t_l

    new_l = max(0.03, min(0.96, eff_tl * k))
    if t_s <= 0.08:
        new_s = t_s
    elif s_s >= 0.15:
        sat_ratio = max(0.72, min(1.08, p_s / s_s))
        new_s = max(0.0, min(1.0, t_s * sat_ratio))
    else:
        new_s = t_s

    return hsl_to_rgb(t_h, new_s, new_l)


_SYSTEM_TEMP_DIRS = ("/tmp", "/var/tmp", "/usr/tmp")
_GCODE_EXTENSIONS = (".gcode", ".gco", ".gc")


def _is_gcode_filename(path: str) -> bool:
    return str(path or "").strip().lower().endswith(_GCODE_EXTENSIONS)


def _is_inside_dir(path: str, directory: str) -> bool:
    if not path or not directory:
        return False
    try:
        p_real = os.path.realpath(path)
        d_real = os.path.realpath(directory)
        return p_real == d_real or p_real.startswith(d_real.rstrip(os.sep) + os.sep)
    except OSError:
        return False


def _is_system_temp_path(path: str, storage_dir: str = "") -> bool:
    """
    Return True if `path` resides under `/tmp`, `/var/tmp`, or `/usr/tmp` (and is NOT inside
    the plugin's own `storage_dir`). Avoids querying `tempfile.gettempdir()`.
    """
    raw = str(path or "").strip()
    if not raw:
        return False
    if storage_dir and _is_inside_dir(raw, storage_dir):
        return False
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        resolved = os.path.abspath(raw)
    for target in (raw, resolved):
        for sys_tmp in _SYSTEM_TEMP_DIRS:
            if target == sys_tmp or target.startswith(sys_tmp + "/"):
                return True
    return False


def _sanitize_staged_gcode_filename(filename: str, default_name: str = "sliced_plate.gcode") -> str:
    raw_base = os.path.basename(str(filename or default_name).strip()) or default_name
    safe_base = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_base).strip("._") or default_name
    if not safe_base.lower().endswith(_GCODE_EXTENSIONS):
        safe_base += ".gcode"
    return safe_base


def _open_atomic_temp_file(
    target_dir: str,
    prefix: str = ".ace_tmp_",
    suffix: str = ".gcode",
    mode: str = "wb",
    encoding: Optional[str] = None,
) -> Tuple[str, Any]:
    """
    Create an exclusive hidden temp file inside `target_dir` without calling `tempfile.*`
    (OrcaSlicer's audit hook blocks `tempfile.*` creators off the main thread).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    last_err: Optional[OSError] = None
    for _ in range(16):
        token = f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:10]}"
        tmp_path = os.path.join(target_dir, f"{prefix}{token}{suffix}")
        try:
            fd = os.open(tmp_path, flags, 0o600)
        except FileExistsError as exc:
            last_err = exc
            continue
        try:
            kwargs: Dict[str, Any] = {}
            if "b" not in mode and encoding is not None:
                kwargs["encoding"] = encoding
            handle = os.fdopen(fd, mode, **kwargs)
            return tmp_path, handle
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    raise last_err or OSError("Could not create exclusive temp file")


def _stage_gcode_file_to_storage(
    src_path: str,
    state: Optional[PluginState] = None,
) -> Optional[str]:
    """
    Atomically copy an OrcaSlicer-approved `.gcode` file (e.g. from `execute(ctx)`) into the
    plugin's permanent `<storage>/staged_gcode/` folder (inside `data_dir()`) so later UI reads
    never touch `/tmp`. Returns the staged file path, or `None` if storage is unavailable or copy fails.
    """
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    if not storage_dir:
        return None
    staged_dir = os.path.join(storage_dir, "staged_gcode")
    tmp_path: Optional[str] = None
    try:
        if _is_inside_dir(src_path, staged_dir):
            return src_path
        safe_base = _sanitize_staged_gcode_filename(src_path, default_name="sliced_plate.gcode")
        os.makedirs(staged_dir, exist_ok=True)
        staged_path = os.path.join(staged_dir, safe_base)
        tmp_path, dst_f = _open_atomic_temp_file(
            staged_dir,
            prefix=".ace_tmp_",
            suffix=".gcode",
            mode="wb",
        )
        with open(src_path, "rb") as src_f, dst_f:
            while True:
                chunk = src_f.read(65536)
                if not chunk:
                    break
                dst_f.write(chunk)
            dst_f.flush()
            os.fsync(dst_f.fileno())
        os.replace(tmp_path, staged_path)
        tmp_path = None
        return staged_path
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def discover_sliced_gcode_candidates(
    state: Optional[PluginState] = None,
    limit: int = 12,
) -> List[str]:
    """
    Discover G-code candidates strictly inside the plugin's own `<storage>/staged_gcode/`
    directory (sandbox-safe inside `data_dir()`).
    NEVER scans `/tmp`, `/var/tmp`, `/usr/tmp`, `tempfile.gettempdir()`, `XDG_RUNTIME_DIR`,
    or any external system directory.
    """
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    if not storage_dir:
        return []
    norm_storage = os.path.abspath(storage_dir)
    if norm_storage in ("/", "/tmp", "/var/tmp", "/usr/tmp"):
        return []

    staged_dir = os.path.join(storage_dir, "staged_gcode")
    if not os.path.isdir(staged_dir):
        return []

    found: List[Tuple[float, str]] = []
    seen_files = set()
    try:
        with os.scandir(staged_dir) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    fname = entry.name
                    lower = fname.lower()
                    if not lower.endswith(_GCODE_EXTENSIONS):
                        continue
                    if fname.startswith(".ace_tmp_") or lower.endswith(".bak"):
                        continue
                    real_f = os.path.realpath(entry.path)
                    if real_f in seen_files:
                        continue
                    st_res = entry.stat(follow_symlinks=False)
                    if st_res.st_size <= 0:
                        continue
                    seen_files.add(real_f)
                    mtime_ns = int(getattr(st_res, "st_mtime_ns", int(float(st_res.st_mtime) * 1_000_000_000)))
                    found.append((mtime_ns, entry.path))
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        return []

    found.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in found[: max(1, int(limit))]]


def acquire_gcode_from_slicer(
    state: Optional[PluginState] = None,
    explicit_path: str = "",
    include_last_gcode_path: bool = True,
    force_refresh: bool = False,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """
    Acquire a G-code file strictly from sandbox-safe sources (read-only analysis only;
    never sends Moonraker commands, starts a print, or scans `/tmp` / system folders):
    1. An `explicit_path` directly passed by OrcaSlicer (`execute(ctx)` / `on_lifecycle_event`).
    2. Files already staged inside the plugin's own `<storage>/staged_gcode/` directory.
    3. Already user-approved `state.last_gcode_path` (only when `include_last_gcode_path=True`,
       skipping transient `/tmp` / `/var/tmp` / `/usr/tmp` paths when running inside OrcaSlicer).
    4. Otherwise syncs active in-memory filaments via `orca.host.preset_bundle()`.
    Returns `(source_kind, resolved_path, preview_info)`.
    """
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    explicit_clean = str(explicit_path or "").strip()
    explicit_valid = bool(
        explicit_clean and _is_gcode_filename(explicit_clean) and os.path.isfile(explicit_clean)
    )

    staged_candidates = discover_sliced_gcode_candidates(state=st)

    cur_valid = False
    cur_path = ""
    if include_last_gcode_path:
        with st._lock:
            cur_path = str(st.last_gcode_path or "").strip()
        if cur_path and _is_gcode_filename(cur_path):
            if not (HAS_ORCA_RUNTIME and _is_system_temp_path(cur_path, storage_dir)):
                try:
                    cur_valid = os.path.isfile(cur_path)
                except OSError:
                    cur_valid = False

    cur_mtime = 0.0
    cur_source = ""
    if cur_valid:
        with st._lock:
            cur_source = str(st.last_gcode_source or "").strip()
        try:
            cur_mtime = float(os.stat(cur_path).st_mtime)
        except OSError:
            cur_mtime = 0.0

    chosen_path = ""
    source_kind = "presets_only"
    if explicit_valid:
        chosen_path = explicit_clean
        source_kind = "slicer_explicit"
    elif staged_candidates:
        newest_staged = staged_candidates[0]
        newest_mtime_ns = 0
        try:
            st_new = os.stat(newest_staged)
            newest_mtime_ns = int(getattr(st_new, "st_mtime_ns", int(float(st_new.st_mtime) * 1_000_000_000)))
        except OSError:
            newest_mtime_ns = 0
        cur_mtime_ns = 0
        if cur_valid:
            try:
                st_cur = os.stat(cur_path)
                cur_mtime_ns = int(getattr(st_cur, "st_mtime_ns", int(float(st_cur.st_mtime) * 1_000_000_000)))
            except OSError:
                cur_mtime_ns = int(cur_mtime * 1_000_000_000)
        cleared_ns = int(getattr(st, "cleared_before_mtime_ns", 0) or 0)
        same_as_current = False
        if cur_valid:
            try:
                same_as_current = os.path.realpath(cur_path) == os.path.realpath(newest_staged)
            except OSError:
                same_as_current = cur_path == newest_staged

        if not cur_valid and not force_refresh and cleared_ns > 0 and newest_mtime_ns <= cleared_ns:
            chosen_path = ""
            source_kind = "presets_only"
        elif cur_valid and not force_refresh and (
            same_as_current
            or cur_source == "file_picker"
            or newest_mtime_ns <= cur_mtime_ns
        ):
            chosen_path = cur_path
            source_kind = cur_source or "existing"
        else:
            chosen_path = newest_staged
            source_kind = "staged_gcode"
    elif cur_valid:
        chosen_path = cur_path
        source_kind = cur_source or "existing"

    if chosen_path and int(getattr(st, "cleared_before_mtime_ns", 0) or 0) > 0:
        with st._lock:
            st.cleared_before_mtime_ns = 0

    if not chosen_path:
        sync_filaments_from_orca_host(st)
        return "presets_only", "", None

    info = preview_gcode_file(chosen_path, st)
    with st._lock:
        st.last_gcode_source = source_kind
    st.save_config()
    return source_kind, chosen_path, info


def load_gcode_from_content(
    filename: str,
    content: str,
    state: Optional[PluginState] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Stage a user-selected local `.gcode` (e.g. from the modal's Browse `.gcode` picker)
    into the plugin's writable storage folder, analyze it strictly read-only, and update state.
    Never modifies the original user file or triggers any print.
    """
    if not content or not content.strip():
        raise ValueError("Selected G-code file is empty.")
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    if not storage_dir:
        raise RuntimeError("Plugin storage directory is unavailable.")
    safe_base = _sanitize_staged_gcode_filename(filename, default_name="uploaded_plate.gcode")

    staged_dir = os.path.join(storage_dir, "staged_gcode")
    os.makedirs(staged_dir, exist_ok=True)
    staged_path = os.path.join(staged_dir, safe_base)
    with open(staged_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(content)

    info = preview_gcode_file(staged_path, st)
    with st._lock:
        st.last_gcode_source = "file_picker"
    st.save_config()
    return staged_path, info


def sync_filaments_from_orca_host(
    state: Optional[PluginState] = None,
    preserve_gcode_usage: bool = False,
) -> List[Dict[str, Any]]:
    """
    Read active filament presets directly from OrcaSlicer 2.5.0+ `orca.host.preset_bundle()`
    (`current_filament_presets`, `current_filament_preset_names`, `full_config_value`,
    and per-preset `config_value`). Updates `state.slicer_filaments` when available.
    Returns [] safely if running outside OrcaSlicer or if `preset_bundle()` is unavailable.
    """
    try:
        host_mod = getattr(orca, "host", None)
        preset_bundle_fn = getattr(host_mod, "preset_bundle", None)
        if not callable(preset_bundle_fn):
            return []
        bundle = preset_bundle_fn()
        if bundle is None:
            return []

        presets = list(bundle.current_filament_presets() or []) if hasattr(bundle, "current_filament_presets") else []
        names = list(bundle.current_filament_preset_names() or []) if hasattr(bundle, "current_filament_preset_names") else []

        def _norm_vals(raw: Any) -> List[str]:
            if raw is None:
                return []
            if isinstance(raw, (list, tuple)):
                return [str(x).strip() for x in raw]
            text = str(raw).strip()
            if not text:
                return []
            return _split_slicer_list(text)

        def _cfg_list(key: str) -> List[str]:
            vals: List[str] = []
            if hasattr(bundle, "full_config_value"):
                try:
                    vals = _norm_vals(bundle.full_config_value(key))
                except Exception:
                    vals = []
            if presets:
                while len(vals) < len(presets):
                    vals.append("")
                for idx, preset in enumerate(presets):
                    if preset is not None and hasattr(preset, "config_value") and not vals[idx]:
                        try:
                            p_vals = _norm_vals(preset.config_value(key))
                            if p_vals:
                                vals[idx] = p_vals[0]
                        except Exception:
                            pass
            return vals

        colours = _cfg_list("filament_colour")
        default_colours = _cfg_list("default_filament_colour")
        types = _cfg_list("filament_type")
        vendors = _cfg_list("filament_vendor")
        diameters = _cfg_list("filament_diameter")

        count = max(len(presets), len(names), len(colours), len(types))
        if count <= 0:
            return []

        filaments: List[Dict[str, Any]] = []
        for idx in range(count):
            raw_col = ""
            if idx < len(colours) and colours[idx]:
                raw_col = colours[idx]
            elif idx < len(default_colours) and default_colours[idx]:
                raw_col = default_colours[idx]
            col = rgb_to_hex(raw_col) if raw_col else "#CCCCCC"

            mat = types[idx] if idx < len(types) and types[idx] else "PLA"
            vendor = vendors[idx] if idx < len(vendors) else ""
            profile = str(names[idx]) if idx < len(names) and names[idx] else ""
            if not profile and idx < len(presets) and presets[idx] is not None:
                profile = str(getattr(presets[idx], "name", "") or "")

            diam = DEFAULT_FILAMENT_DIAMETER
            if idx < len(diameters) and diameters[idx]:
                try:
                    d_val = float(diameters[idx])
                    if math.isfinite(d_val) and d_val > 0:
                        diam = d_val
                except (ValueError, TypeError):
                    pass

            filaments.append(
                {
                    "slicer_tool": idx,
                    "label": f"Extruder {idx} (T{idx})",
                    "color": col,
                    "rgb": list(hex_to_rgb(col)),
                    "material": mat,
                    "vendor": vendor,
                    "profile": profile,
                    "diameter": diam,
                    "used_g": 0.0,
                    "used": True,
                }
            )

        st = state or GLOBAL_STATE
        if filaments and preserve_gcode_usage:
            with st._lock:
                existing_by_tool = {
                    int(f["slicer_tool"]): f
                    for f in st.slicer_filaments
                    if isinstance(f, dict) and "slicer_tool" in f
                }
                has_gcode_usage = bool(st.last_gcode_path) and any(
                    ("used_mm" in f or "used_cm3" in f or float(f.get("used_g", 0) or 0) > 0)
                    for f in existing_by_tool.values()
                )
            if has_gcode_usage:
                for fil in filaments:
                    prev = existing_by_tool.get(int(fil["slicer_tool"]))
                    if prev:
                        fil["used"] = bool(prev.get("used", False))
                        fil["used_g"] = float(prev.get("used_g", 0.0) or 0.0)
                        if "used_mm" in prev:
                            fil["used_mm"] = prev["used_mm"]
                        if "used_m" in prev:
                            fil["used_m"] = prev["used_m"]
                        if "used_cm3" in prev:
                            fil["used_cm3"] = prev["used_cm3"]
                    else:
                        fil["used"] = False
        if filaments:
            st.update_slicer_filaments(filaments)
        return filaments
    except Exception:
        return []


def _clean_preset_str(value: Any) -> str:
    """Normalize OrcaSlicer preset metadata strings without altering inner characters."""
    if not isinstance(value, str):
        return ""
    return value.strip().strip('"').strip()


def _safe_preset_config_value(obj: Any, method_name: str, key: str) -> Any:
    """Safely read a config option from an OrcaSlicer preset or bundle."""
    fn = getattr(obj, method_name, None)
    if not callable(fn):
        return None
    try:
        return fn(key)
    except Exception:
        return None


def parse_slicer_pipeline_entries(value: Any) -> List[str]:
    """
    Parse OrcaSlicer's `;`-separated `slicing_pipeline_plugin` string list (with backslash
    escapes and quoted items) or an already-unpacked list/tuple of entries.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value)
    if not text.strip():
        return []
    out: List[str] = []
    cur: List[str] = []
    quoted = False
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if quoted and ch == "\\" and idx + 1 < len(text):
            idx += 1
            cur.append({"n": "\n", "r": "\r"}.get(text[idx], text[idx]))
        elif ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            entry = "".join(cur).strip()
            if entry:
                out.append(entry)
            cur = []
        else:
            cur.append(ch)
        idx += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def serialize_slicer_pipeline_entries(entries: List[str]) -> str:
    """
    Serialize string entries in OrcaSlicer's `;`-separated config format (quoting any entry
    containing spaces, semicolons, quotes, tabs, newlines, or backslashes).
    """
    out: List[str] = []
    for raw in entries:
        item = str(raw).strip()
        if not item:
            continue
        if any(c in item for c in ' ;\t\\"\r\n'):
            escaped = (
                item.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", "\\r")
                .replace("\n", "\\n")
            )
            item = f'"{escaped}"'
        out.append(item)
    return ";".join(out)


def _pipeline_entry_matches(entry: str, capability_name: str) -> bool:
    """Return True if an existing pipeline entry already selects `capability_name`."""
    cleaned = str(entry or "").strip()
    if not cleaned:
        return False
    return cleaned == capability_name or cleaned.rsplit(";", 1)[-1].strip() == capability_name


def build_slicing_pipeline_value(
    current_value: Any,
    capability_name: str = PIPELINE_CAPABILITY_NAME,
) -> Optional[str]:
    """
    Return the updated `slicing_pipeline_plugin` string with `capability_name` appended while
    preserving every existing entry in order, or `None` if already present.
    """
    entries = parse_slicer_pipeline_entries(current_value)
    if any(_pipeline_entry_matches(entry, capability_name) for entry in entries):
        return None
    return serialize_slicer_pipeline_entries(entries + [capability_name])


def is_kobra_s1_printer_name(*values: Any) -> bool:
    """Return True strictly for Anycubic Kobra S1 printer names/models/parent presets."""
    for raw in values:
        cleaned = _clean_preset_str(raw)
        if cleaned and _KOBRA_S1_PRINTER_RE.search(cleaned):
            return True
    return False


def is_kobra_s1_printer_preset(printer_preset: Any) -> bool:
    """Inspect a host printer preset object and verify whether it targets Anycubic Kobra S1."""
    if printer_preset is None:
        return False
    return is_kobra_s1_printer_name(
        _safe_preset_config_value(printer_preset, "config_value", "printer_model"),
        getattr(printer_preset, "name", ""),
        _safe_preset_config_value(printer_preset, "config_value", "inherits"),
    )


def _host_pipeline_handles() -> Optional[Tuple[Any, Any]]:
    """Return `(preset_bundle_fn, begin_update_fn)` if OrcaSlicer's preset transaction API exists."""
    host_mod = getattr(orca, "host", None)
    preset_bundle_fn = getattr(host_mod, "preset_bundle", None)
    preset_api = getattr(host_mod, "preset", None)
    process_api = getattr(preset_api, "process", None)
    begin_update_fn = getattr(process_api, "begin_update", None)
    if not callable(preset_bundle_fn) or not callable(begin_update_fn):
        return None
    return preset_bundle_fn, begin_update_fn


def ensure_slicing_pipeline_registered(
    state: Optional[PluginState] = None,
    capability_name: str = PIPELINE_CAPABILITY_NAME,
) -> Dict[str, Any]:
    """
    Automatically ensure `KobraS1AcePostProcessCapability` is included in the active Kobra S1
    process preset's `slicing_pipeline_plugin` option via `orca.host.preset.process.begin_update`.
    Runs at most once per `(printer_name, process_name)` per session, preserves all existing
    entries in `slicing_pipeline_plugin`, and never touches non-Kobra-S1 profiles.
    Must run off the UI thread inside OrcaSlicer (so `begin_update` cannot deadlock).
    """
    st = state or GLOBAL_STATE
    with st._lock:
        if not getattr(st, "auto_register_pipeline", True):
            return {"status": "disabled"}

    handles = _host_pipeline_handles()
    if handles is None:
        return {"status": "unavailable"}
    preset_bundle_fn, begin_update_fn = handles

    with _PIPELINE_UPDATE_LOCK:
        try:
            bundle = preset_bundle_fn()
            printer = bundle.current_printer_preset() if bundle is not None else None
            process = bundle.current_process_preset() if bundle is not None else None
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        if printer is None or process is None:
            return {"status": "unavailable"}

        printer_name = _clean_preset_str(getattr(printer, "name", "") or "")
        if not is_kobra_s1_printer_preset(printer):
            return {"status": "skipped_non_kobra_s1", "printer": printer_name}

        process_name = _clean_preset_str(getattr(process, "name", "") or "")
        if not process_name:
            return {"status": "unavailable"}

        session_key = (printer_name, process_name)
        with st._lock:
            if session_key in st._pipeline_registered_keys:
                return {"status": "already_checked", "printer": printer_name, "process": process_name}

        current_raw = _safe_preset_config_value(bundle, "full_config_value", "slicing_pipeline_plugin")
        if current_raw is None or current_raw == "":
            proc_raw = _safe_preset_config_value(process, "config_value", "slicing_pipeline_plugin")
            if proc_raw is not None and proc_raw != "":
                current_raw = proc_raw

        new_value = build_slicing_pipeline_value(current_raw, capability_name=capability_name)
        with st._lock:
            st._pipeline_registered_keys.add(session_key)

        if new_value is None:
            return {"status": "already_present", "printer": printer_name, "process": process_name}

        try:
            try:
                tx = begin_update_fn("")
            except TypeError:
                tx = begin_update_fn()

            # Re-verify active preset didn't switch before committing
            try:
                cur_bundle = preset_bundle_fn()
                cur_printer = cur_bundle.current_printer_preset() if cur_bundle is not None else None
                cur_process = cur_bundle.current_process_preset() if cur_bundle is not None else None
                cur_key = (
                    _clean_preset_str(getattr(cur_printer, "name", "") or ""),
                    _clean_preset_str(getattr(cur_process, "name", "") or ""),
                )
            except Exception:
                cur_printer = None
                cur_key = ("", "")

            if not is_kobra_s1_printer_preset(cur_printer) or cur_key != session_key:
                for cancel_attr in ("abort", "cancel"):
                    cancel_fn = getattr(tx, cancel_attr, None)
                    if callable(cancel_fn):
                        try:
                            cancel_fn()
                        except Exception:
                            pass
                        break
                with st._lock:
                    st._pipeline_registered_keys.discard(session_key)
                return {"status": "aborted_changed", "printer": printer_name, "process": process_name}

            tx.set("slicing_pipeline_plugin", new_value)
            tx.commit()
            return {
                "status": "updated",
                "printer": printer_name,
                "process": process_name,
                "value": new_value,
            }
        except Exception as exc:
            return {
                "status": "error",
                "printer": printer_name,
                "process": process_name,
                "error": str(exc),
            }


def _schedule_pipeline_registration(
    state: Optional[PluginState] = None,
    use_thread: bool = True,
) -> bool:
    """Schedule Kobra S1 slicing-pipeline registration off OrcaSlicer's UI thread."""
    st = state or GLOBAL_STATE
    with st._lock:
        if not getattr(st, "auto_register_pipeline", True):
            return False
    if _host_pipeline_handles() is None:
        return False
    if use_thread:
        threading.Thread(
            target=ensure_slicing_pipeline_registered,
            args=(st,),
            name="kobra-s1-pipeline-reg",
            daemon=True,
        ).start()
    else:
        ensure_slicing_pipeline_registered(st)
    return True


def _handle_pipeline_lifecycle_event(
    event: Any,
    ctx: Any,
    state: Optional[PluginState] = None,
    use_thread: bool = True,
) -> bool:
    """
    Check if `event` is a preset/project selection event relevant for slicing-pipeline setup.
    Returns True if handled (regardless of whether an update was needed), False otherwise.
    """
    evt_enum = getattr(orca, "LifecycleEvent", None)
    if evt_enum is None:
        return False
    preset_evt = getattr(evt_enum, "PresetSelected", None)
    project_evt = getattr(evt_enum, "ProjectOpened", None)
    new_proj_evt = getattr(evt_enum, "NewProject", None)
    device_evt = getattr(evt_enum, "DeviceSelected", None)

    if event is not None and event == preset_evt:
        kind = str(getattr(ctx, "msg", "") or "").strip().lower()
        if kind in ("", "print", "process", "printer"):
            _schedule_pipeline_registration(state=state, use_thread=use_thread)
        return True

    if event is not None and event in tuple(e for e in (project_evt, new_proj_evt, device_evt) if e is not None):
        _schedule_pipeline_registration(state=state, use_thread=use_thread)
        return True

    return False


_MODEL_IGNORE_EXTENSIONS = (
    ".bgcode",
    ".gcode",
    ".gco",
    ".gc",
    ".3mf",
    ".stl",
    ".step",
    ".stp",
    ".obj",
    ".amf",
)
_GENERIC_MODEL_STEMS = frozenset({"", "untitled", "unnamed", "none", "default", "empty", "plate"})


def _strip_model_extension(raw_name: str) -> str:
    base = os.path.basename(str(raw_name or "").strip())
    low = base.lower()
    for ext in _MODEL_IGNORE_EXTENSIONS:
        if low.endswith(ext) and len(base) > len(ext):
            base = base[: -len(ext)].strip()
            break
    return base


def _is_generic_model_stem(stem: str) -> bool:
    low = str(stem or "").strip().lower()
    if low in _GENERIC_MODEL_STEMS:
        return True
    return bool(re.fullmatch(r"(plate|platte)[\s_-]*\d*", low))


def _extract_host_model_candidates(obj: Any, depth: int = 0) -> List[str]:
    """Safely extract candidate model/project/plate names from an OrcaSlicer host object."""
    if obj is None or depth > 2:
        return []
    out: List[str] = []
    if isinstance(obj, str):
        val = obj.strip()
        if val:
            out.append(val)
        return out
    if isinstance(obj, (list, tuple)):
        for item in obj[:8]:
            out.extend(_extract_host_model_candidates(item, depth + 1))
        return out
    if isinstance(obj, dict):
        for key in (
            "project_name",
            "project_path",
            "filepath",
            "file_path",
            "filename",
            "file",
            "model_name",
            "plate_name",
            "name",
            "title",
        ):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
        for subkey in ("active_plate", "current_plate", "plate", "model", "objects"):
            if subkey in obj and obj.get(subkey) is not None:
                out.extend(_extract_host_model_candidates(obj.get(subkey), depth + 1))
        return out

    for attr in (
        "project_name",
        "project_path",
        "filepath",
        "file_path",
        "filename",
        "model_name",
        "plate_name",
        "name",
        "title",
        "get_project_name",
        "get_name",
    ):
        try:
            raw_attr = getattr(obj, attr, None)
            if callable(raw_attr):
                raw_attr = raw_attr()
            if isinstance(raw_attr, str) and raw_attr.strip():
                out.append(raw_attr.strip())
        except Exception:
            continue
    for subattr in ("active_plate", "current_plate", "plate", "model", "objects"):
        try:
            sub_val = getattr(obj, subattr, None)
            if callable(sub_val):
                sub_val = sub_val()
            if sub_val is not None:
                out.extend(_extract_host_model_candidates(sub_val, depth + 1))
        except Exception:
            continue
    return out


def get_orca_active_model_info() -> Dict[str, Any]:
    """Query `orca.host.plater()` and `orca.host.model()` read-only for active model/plate stems."""
    host = getattr(orca, "host", None)
    if host is None:
        return {"display_name": "", "candidates": []}

    raw_candidates: List[str] = []
    for method_name in ("plater", "model"):
        try:
            accessor = getattr(host, method_name, None)
            if accessor is None:
                continue
            obj = accessor() if callable(accessor) else accessor
            raw_candidates.extend(_extract_host_model_candidates(obj, depth=0))
        except Exception:
            continue

    stems: List[str] = []
    seen = set()
    for raw in raw_candidates:
        stem = _strip_model_extension(raw)
        if not stem or _is_generic_model_stem(stem):
            continue
        norm_key = stem.lower()
        if norm_key not in seen:
            seen.add(norm_key)
            stems.append(stem)

    return {
        "display_name": stems[0] if stems else "",
        "candidates": stems,
    }


def _model_stem_matches_gcode(model_stem: str, gcode_stem: str) -> bool:
    """Return True when active OrcaSlicer `model_stem` plausibly matches exported `gcode_stem`."""
    cand_compact = re.sub(r"[^a-z0-9]+", "", str(model_stem or "").lower())
    gcode_compact = re.sub(r"[^a-z0-9]+", "", str(gcode_stem or "").lower())
    if not cand_compact or not gcode_compact:
        return True
    if cand_compact == gcode_compact:
        return True

    cand_tokens = [tok for tok in re.split(r"[^a-z0-9]+", str(model_stem or "").lower()) if tok]
    gcode_tokens = [tok for tok in re.split(r"[^a-z0-9]+", str(gcode_stem or "").lower()) if tok]
    if cand_tokens and gcode_tokens:
        if len(cand_tokens) <= len(gcode_tokens) and all(
            any(gt == ct or (len(ct) >= 3 and gt.startswith(ct) and not ct[-1].isdigit()) for gt in gcode_tokens)
            for ct in cand_tokens
        ):
            return True

    if len(cand_compact) >= 3:
        idx = gcode_compact.find(cand_compact)
        if idx >= 0:
            end_idx = idx + len(cand_compact)
            if not (cand_compact[-1].isdigit() and end_idx < len(gcode_compact) and gcode_compact[end_idx].isdigit()):
                return True
    return False


def check_stale_gcode_model(gcode_path: str = "", filename: str = "") -> Dict[str, Any]:
    """
    Compare the currently open OrcaSlicer model/plate name (`orca.host.model()` / `plater()`)
    against the loaded G-code filename so stale previously exported slices trigger a warning.
    """
    loaded_file = str(filename or (os.path.basename(gcode_path) if gcode_path else "") or "").strip()
    model_info = get_orca_active_model_info()
    candidates = model_info.get("candidates") or []
    active_model = str(model_info.get("display_name") or "")

    if not loaded_file or not candidates:
        return {
            "is_stale": False,
            "active_model": active_model,
            "loaded_file": loaded_file,
            "reason": "",
        }

    gcode_stem = _strip_model_extension(loaded_file)
    if any(_model_stem_matches_gcode(cand, gcode_stem) for cand in candidates):
        return {
            "is_stale": False,
            "active_model": active_model,
            "loaded_file": loaded_file,
            "reason": "",
        }

    return {
        "is_stale": True,
        "active_model": active_model,
        "loaded_file": loaded_file,
        "reason": "model_mismatch",
    }


# ---------------------------------------------------------------------------
# Embedded HTML/CSS/JS UI for OrcaSlicer 2.5.x Page Capability
# ---------------------------------------------------------------------------
ACE_PLUGIN_HTML_UI = """<!DOCTYPE html>
<html lang="en" data-theme="auto" data-resolved="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Anycubic Kobra S1 — ACE Pro Workbench</title>
<style>
  :root {
    --bg: var(--orca-bg, #262928);
    --fg: var(--orca-fg, #ebeeed);
    --muted: var(--orca-muted, #9da6a2);
    --accent: var(--orca-accent, #009688);
    --accent-hover: #00a696;
    --accent-active: #00796b;
    --accent-fg: var(--orca-accent-fg, #ffffff);
    --border: var(--orca-border, #464b49);
    --font: var(--orca-font, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif);

    --surface-card: #2f3332;
    --surface-sub: #383d3b;
    --surface-input: #383d3b;
    --surface-hover: #424846;
    --log-bg: #1c1f1e;

    --ok: #00a696;
    --ok-bg: rgba(0, 150, 136, 0.16);
    --warn: #f59e0b;
    --warn-bg: rgba(245, 158, 11, 0.15);
    --warn-fg: #fde68a;
    --danger: #ef4444;
    --danger-bg: rgba(239, 68, 68, 0.16);
    --danger-fg: #fecaca;
    --info: #4db6ac;
    --info-bg: rgba(0, 150, 136, 0.16);
    --accent-soft: rgba(0, 150, 136, 0.16);
    --radius: 6px;
    --radius-sm: 4px;
  }

  html[data-resolved="light"] {
    --bg: var(--orca-bg, #f3f4f6);
    --fg: var(--orca-fg, #1f2423);
    --muted: var(--orca-muted, #5c6563);
    --accent: var(--orca-accent, #009688);
    --accent-hover: #00a696;
    --accent-active: #00796b;
    --accent-fg: var(--orca-accent-fg, #ffffff);
    --border: var(--orca-border, #d8dce0);

    --surface-card: #ffffff;
    --surface-sub: #eef0f2;
    --surface-input: #ffffff;
    --surface-hover: #e4e7ea;
    --log-bg: #f6f7f8;

    --ok: #00796b;
    --ok-bg: rgba(0, 150, 136, 0.12);
    --warn: #b45309;
    --warn-bg: rgba(245, 158, 11, 0.14);
    --warn-fg: #92400e;
    --danger: #b91c1c;
    --danger-bg: rgba(220, 38, 38, 0.11);
    --danger-fg: #991b1b;
    --info: #00796b;
    --info-bg: rgba(0, 150, 136, 0.12);
    --accent-soft: rgba(0, 150, 136, 0.12);
  }

  html[data-theme="dark"] {
    --bg: #262928;
    --fg: #ebeeed;
    --muted: #9da6a2;
    --accent: #009688;
    --accent-hover: #00a696;
    --accent-active: #00796b;
    --accent-fg: #ffffff;
    --border: #464b49;
    --surface-card: #2f3332;
    --surface-sub: #383d3b;
    --surface-input: #383d3b;
    --surface-hover: #424846;
    --log-bg: #1c1f1e;
    --ok: #00a696;
    --ok-bg: rgba(0, 150, 136, 0.16);
    --info: #4db6ac;
    --info-bg: rgba(0, 150, 136, 0.16);
    --accent-soft: rgba(0, 150, 136, 0.16);
  }

  html[data-theme="light"] {
    --bg: #f3f4f6;
    --fg: #1f2423;
    --muted: #5c6563;
    --accent: #009688;
    --accent-hover: #00a696;
    --accent-active: #00796b;
    --accent-fg: #ffffff;
    --border: #d8dce0;
    --surface-card: #ffffff;
    --surface-sub: #eef0f2;
    --surface-input: #ffffff;
    --surface-hover: #e4e7ea;
    --log-bg: #f6f7f8;
    --ok: #00796b;
    --ok-bg: rgba(0, 150, 136, 0.12);
    --info: #00796b;
    --info-bg: rgba(0, 150, 136, 0.12);
    --accent-soft: rgba(0, 150, 136, 0.12);
  }

  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    min-height: 100vh;
    height: auto;
    overflow-x: hidden;
    overflow-y: auto;
    font-family: var(--font);
    background: var(--bg);
    color: var(--fg);
    font-size: 13px;
    line-height: 1.4;
  }

  /* 1080p Single-Screen App Shell */
  .app-shell {
    max-width: 1920px;
    width: 100%;
    height: auto;
    margin: 0 auto;
    padding: 10px 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  /* Unified Compact Top Bar */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    padding: 8px 14px;
    background: var(--surface-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    flex-shrink: 0;
  }
  .brand-block { display: flex; align-items: center; gap: 10px; }
  .brand-mark {
    width: 32px;
    height: 32px;
    border-radius: var(--radius);
    background: var(--accent-soft);
    border: 1px solid var(--accent);
    color: var(--accent);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 13px;
    letter-spacing: 0.5px;
    flex-shrink: 0;
  }
  .brand-title { margin: 0; font-size: 15px; font-weight: 700; color: var(--fg); white-space: nowrap; }
  .brand-sub { font-size: 10.5px; color: var(--muted); white-space: nowrap; }

  /* Inline Telemetry Strip inside Unified Top Bar (Single Instance of Each Metric) */
  .top-telemetry {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    flex: 1;
    justify-content: center;
  }
  .exec-tile {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 4px 10px;
    background: var(--surface-sub);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    min-width: 0;
  }
  .exec-kicker {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: var(--muted);
    white-space: nowrap;
  }
  .exec-value {
    font-size: 12px;
    font-weight: 700;
    color: var(--fg);
    white-space: nowrap;
  }
  .exec-sub {
    font-size: 10.5px;
    color: var(--muted);
    white-space: nowrap;
    max-width: 155px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .topbar-right { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }

  .segmented {
    display: inline-flex;
    background: var(--surface-sub);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 2px;
    gap: 2px;
  }
  .segmented button {
    border: 0;
    background: transparent;
    color: var(--muted);
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
    border-radius: 4px;
    cursor: pointer;
  }
  .segmented button.active {
    background: var(--accent);
    color: var(--accent-fg);
  }

  /* Safety Banner */
  .safety-banner {
    display: none;
    background: var(--danger-bg);
    border: 1px solid var(--danger);
    color: var(--danger-fg);
    padding: 8px 14px;
    border-radius: var(--radius-sm);
    font-weight: 600;
    font-size: 12px;
    flex-shrink: 0;
  }
  .safety-banner-inner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  /* Cards & Primary 1080p Slot Bay */
  .card {
    background: var(--surface-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 14px;
  }
  .slots-main-card {
    background: var(--bg);
    height: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
  }
  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .card-title {
    margin: 0;
    font-size: 13px;
    font-weight: 700;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  h3 { margin: 0 0 8px 0; font-size: 12px; font-weight: 600; color: var(--muted); }

  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  label { color: var(--muted); font-size: 11.5px; font-weight: 600; }

  input[type="text"], input[type="number"], select {
    background: var(--surface-input);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 5px 8px;
    font-size: 12px;
    font-family: var(--font);
  }
  input[type="text"]:focus, input[type="number"]:focus, select:focus {
    outline: none;
    border-color: var(--accent);
  }
  input[type="number"] { width: 68px; }

  button {
    background: var(--surface-sub);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 5px 10px;
    cursor: pointer;
    font-size: 11.5px;
    font-weight: 600;
    font-family: var(--font);
    transition: background 0.14s, border-color 0.14s, opacity 0.14s;
    white-space: nowrap;
  }
  button:hover { background: var(--surface-hover); border-color: var(--accent); }
  button.primary {
    background: var(--accent);
    color: var(--accent-fg);
    border-color: var(--accent);
  }
  button.primary:hover {
    background: var(--accent-hover, #00a696);
    border-color: var(--accent-hover, #00a696);
  }
  button.primary:active {
    background: var(--accent-active, #00796b);
    border-color: var(--accent-active, #00796b);
  }
  button.is-original {
    border-color: var(--accent);
    background: var(--accent-soft);
    color: var(--fg);
  }
  button.warn { border-color: var(--warn); color: var(--warn); }
  button.danger { border-color: var(--danger); color: var(--danger); }
  button.subtle { padding: 4px 8px; font-size: 11px; color: var(--muted); }
  button:disabled { opacity: 0.42; cursor: not-allowed; pointer-events: none; }

  .divider { border: 0; border-top: 1px solid var(--border); margin: 10px 0; }

  /* Badges */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 99px;
    font-size: 11px;
    font-weight: 600;
    white-space: nowrap;
  }
  .badge.ready, .badge.ok { background: var(--ok-bg); color: var(--ok); border: 1px solid rgba(0, 150, 136, 0.32); }
  .badge.loaded { background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent); }
  .badge.empty { background: var(--surface-sub); color: var(--muted); border: 1px solid var(--border); }
  .badge.drying, .badge.warn { background: var(--warn-bg); color: var(--warn); }
  .badge.danger { background: var(--danger-bg); color: var(--danger); }
  .badge.info { background: var(--info-bg); color: var(--info); }

  /* ACE Pro Spool Bay Grid (Compact 4x2 Natural Card Height Layout) */
  .slots-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(200px, 1fr));
    grid-auto-rows: auto;
    align-items: start;
    gap: 8px;
  }
  @media (max-width: 1080px) { .slots-grid { grid-template-columns: repeat(2, minmax(190px, 1fr)); } }
  @media (max-width: 580px) { .slots-grid { grid-template-columns: 1fr; } }

  .slot-card {
    background: var(--surface-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    height: auto;
    transition: border-color 0.15s;
  }
  .slot-card.slot-loaded {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent);
    background: linear-gradient(180deg, var(--accent-soft) 0%, var(--surface-card) 36%);
  }
  .slot-card.slot-empty {
    opacity: 0.78;
    border-style: dashed;
  }
  .slot-card.slot-unconfigured {
    border-style: dashed;
    border-color: var(--warn);
  }
  .slot-header { display: flex; justify-content: space-between; align-items: center; gap: 6px; font-weight: 700; }
  .slot-id { display: flex; align-items: center; gap: 6px; font-size: 13px; }
  .swatch-row { display: flex; align-items: center; gap: 8px; }
  .spool-ring {
    width: 30px;
    height: 30px;
    border-radius: 50%;
    border: 6px solid #64748b;
    background: var(--surface-card);
    box-shadow: inset 0 0 0 2px rgba(0, 0, 0, 0.3), 0 0 0 1px rgba(128, 128, 128, 0.5);
    flex-shrink: 0;
  }
  .slot-empty .spool-ring {
    border: 2px dashed var(--muted);
    background: transparent;
    box-shadow: none;
  }
  .swatch-dot {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 1px solid rgba(128, 128, 128, 0.55);
    display: inline-block;
    flex-shrink: 0;
  }
  .slot-meta { font-size: 11px; color: var(--muted); }
  .slot-actions { display: flex; gap: 5px; flex-wrap: wrap; margin-top: auto; padding-top: 4px; border-top: 1px solid var(--border); }
  .slot-actions button { flex: 1; padding: 4px 6px; font-size: 11px; min-width: 44px; }

  /* Single-Row Compact Hardware, Dryer & Print Trigger Bar */
  .bottom-control-strip {
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    background: var(--surface-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    flex-shrink: 0;
  }
  @media (max-width: 1360px) {
    .bottom-control-strip { grid-template-columns: 1fr; }
  }
  .strip-cluster {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .strip-divider {
    width: 1px;
    height: 24px;
    background: var(--border);
    margin: 0 2px;
  }
  .cluster-tag {
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: var(--muted);
    white-space: nowrap;
  }
  .print-trigger-cluster {
    display: flex;
    align-items: center;
    gap: 8px;
    justify-content: flex-end;
    flex-wrap: wrap;
  }
  .btn-cta-send {
    padding: 7px 14px;
    font-size: 12.5px;
    font-weight: 700;
  }

  /* Modals & Drawers */
  .modal-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    z-index: 200;
    background: rgba(0, 0, 0, 0.72);
    backdrop-filter: blur(3px);
    align-items: center;
    justify-content: center;
    padding: 16px;
  }
  .modal-backdrop.open {
    display: flex;
  }
  .modal-dialog {
    width: 100%;
    max-width: 980px;
    max-height: 92vh;
    background: var(--surface-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .modal-dialog.modal-sm { max-width: 560px; }
  .modal-dialog.modal-lg { max-width: 1160px; }
  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 16px;
    background: var(--surface-sub);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .modal-title {
    margin: 0;
    font-size: 14.5px;
    font-weight: 700;
    color: var(--fg);
  }
  .modal-body {
    padding: 14px 16px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 12px;
    flex: 1;
  }
  .modal-footer {
    padding: 12px 16px;
    background: var(--surface-sub);
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 10px;
    flex-shrink: 0;
  }

  /* Pre-Print Modal Top-Left Enlarged Thumbnail & 2x2 Job Summary Strip */
  .modal-job-hero {
    display: flex;
    align-items: stretch;
    gap: 14px;
  }
  .modal-thumb-box {
    position: relative;
    width: 340px;
    min-width: 300px;
    height: 170px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: #1c1f1e;
    background-image: radial-gradient(circle at 50% 45%, rgba(255, 255, 255, 0.04), rgba(0, 0, 0, 0.24));
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    flex-shrink: 0;
    transition: border-color 0.14s ease, box-shadow 0.14s ease;
  }
  html[data-resolved="light"] .modal-thumb-box {
    background: #ffffff;
    background-image: none;
    border-color: #d8dce0;
  }
  .modal-thumb-box.is-zoomable {
    cursor: zoom-in;
  }
  .modal-thumb-box.is-zoomable:hover {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent-soft);
  }
  .modal-thumb-img,
  .modal-thumb-canvas {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
    background: transparent;
  }
  .modal-thumb-fallback {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 40px;
    line-height: 1;
    opacity: 0.72;
  }
  .thumb-mode-badge {
    position: absolute;
    top: 6px;
    right: 6px;
    z-index: 2;
    padding: 2px 8px;
    font-size: 10.5px;
    font-weight: 700;
    line-height: 1.3;
    border-radius: 999px;
    border: 1px solid rgba(255, 255, 255, 0.22);
    background: rgba(31, 34, 33, 0.88);
    color: #ffffff;
    backdrop-filter: blur(4px);
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.28);
    transition: border-color 0.14s ease, background 0.14s ease, color 0.14s ease;
  }
  .thumb-mode-badge:hover {
    border-color: var(--accent);
    background: rgba(31, 34, 33, 0.98);
  }
  .thumb-mode-badge.is-original {
    border-color: var(--accent);
    background: rgba(0, 121, 107, 0.92);
    color: #ffffff;
  }
  html[data-resolved="light"] .thumb-mode-badge {
    border-color: rgba(38, 41, 40, 0.28);
    background: rgba(38, 41, 40, 0.86);
    color: #ffffff;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.16);
  }
  html[data-resolved="light"] .thumb-mode-badge.is-original {
    border-color: #00796b;
    background: rgba(0, 121, 107, 0.94);
    color: #ffffff;
  }
  .thumb-zoom-badge {
    position: absolute;
    bottom: 6px;
    right: 6px;
    z-index: 2;
    width: 26px;
    height: 24px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    line-height: 1;
    border-radius: 999px;
    border: 1px solid rgba(255, 255, 255, 0.22);
    background: rgba(31, 34, 33, 0.88);
    color: #ffffff;
    backdrop-filter: blur(4px);
    cursor: zoom-in;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.28);
    transition: border-color 0.14s ease, background 0.14s ease, transform 0.14s ease;
  }
  html[data-resolved="light"] .thumb-zoom-badge {
    border-color: rgba(38, 41, 40, 0.28);
    background: rgba(38, 41, 40, 0.86);
    color: #ffffff;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.16);
  }
  .modal-thumb-box.is-zoomable:hover .thumb-zoom-badge,
  .thumb-zoom-badge:hover {
    border-color: var(--accent);
    background: rgba(31, 34, 33, 0.98);
    transform: scale(1.06);
  }

  /* Click-to-Zoom Detail Lightbox (#thumbZoomModal) */
  .modal-backdrop-zoom {
    z-index: 260;
    background: rgba(0, 0, 0, 0.82);
  }
  .thumb-zoom-dialog {
    width: min(85vw, 1280px);
    max-width: 85vw;
    max-height: 75vh;
    background: var(--surface-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.65);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .thumb-zoom-stage {
    position: relative;
    flex: 1;
    min-height: 220px;
    padding: 12px;
    background: #1c1f1e;
    background-image: radial-gradient(circle at 50% 48%, rgba(255, 255, 255, 0.04), rgba(0, 0, 0, 0.24));
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    cursor: zoom-out;
  }
  html[data-resolved="light"] .thumb-zoom-stage {
    background: #ffffff;
    background-image: none;
  }
  .thumb-zoom-canvas {
    max-width: 100%;
    max-height: calc(75vh - 102px);
    width: min(100%, calc((75vh - 102px) * var(--thumb-aspect, 2.09)));
    height: auto;
    aspect-ratio: var(--thumb-aspect, 230 / 110);
    object-fit: contain;
    display: block;
    cursor: zoom-out;
  }
  .thumb-zoom-footer {
    padding: 8px 16px;
    background: var(--surface-sub);
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-shrink: 0;
  }

  .job-meta-grid {
    flex: 1;
    min-width: 0;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    align-content: stretch;
    gap: 8px;
  }
  @media (max-width: 780px) {
    .modal-job-hero { flex-direction: column; }
    .modal-thumb-box { width: 100%; min-width: 0; max-width: 340px; height: auto; aspect-ratio: 2 / 1; min-height: 150px; align-self: center; }
  }
  @media (max-width: 480px) {
    .job-meta-grid { grid-template-columns: 1fr; }
  }
  .job-meta-box {
    background: var(--surface-sub);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 8px 11px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }
  .job-meta-val {
    font-size: 12.5px;
    font-weight: 700;
    color: var(--fg);
    white-space: normal;
    overflow-wrap: anywhere;
    line-height: 1.3;
  }

  /* SnapSend-Style Visual Filament Mapping Cards */
  .mapping-cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
    gap: 10px;
  }
  @media (max-width: 680px) {
    .mapping-cards-grid { grid-template-columns: 1fr; }
  }
  .fil-map-card {
    background: var(--surface-sub);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    transition: border-color 0.14s ease, box-shadow 0.14s ease;
  }
  .fil-map-card:hover {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent-soft);
  }
  .fil-map-card.card-mismatch {
    border-color: var(--warn);
    background: var(--warn-bg);
  }
  .fil-map-card.card-error {
    border-color: var(--danger);
    background: var(--danger-bg);
  }
  .fil-map-row {
    display: grid;
    grid-template-columns: 1.05fr auto 0.95fr;
    align-items: center;
    gap: 10px;
  }
  .fil-source-col,
  .fil-target-col {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  /* Custom Filament-Colored Slot Selector Dropdown (WebKit/GTK-safe) */
  .slot-color-dropdown {
    position: relative;
    width: 100%;
    min-width: 0;
  }
  .slot-color-trigger {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 7px 11px;
    border-radius: var(--radius-sm);
    border: 1px solid rgba(128, 128, 128, 0.45);
    font-size: 12.5px;
    font-weight: 700;
    line-height: 1.25;
    cursor: pointer;
    text-align: left;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.14), 0 1px 2px rgba(0, 0, 0, 0.16);
    white-space: nowrap;
    overflow: hidden;
  }
  .slot-color-trigger:hover {
    border-color: var(--accent);
    filter: brightness(1.03);
  }
  .slot-color-trigger.is-empty {
    border-style: dashed;
  }
  .slot-color-trigger-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .slot-color-caret {
    font-size: 10px;
    opacity: 0.85;
    flex-shrink: 0;
  }
  .slot-color-menu {
    display: none;
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    z-index: 80;
    background: var(--surface-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 4px;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.45);
    max-height: 220px;
    overflow-y: auto;
    flex-direction: column;
    gap: 3px;
  }
  .slot-color-dropdown.open .slot-color-menu {
    display: flex;
  }
  .slot-color-dropdown.drop-up .slot-color-menu {
    top: auto;
    bottom: calc(100% + 4px);
  }
  .slot-color-option {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 10px;
    border-radius: var(--radius-sm);
    border: 1px solid rgba(128, 128, 128, 0.35);
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    text-align: left;
  }
  .slot-color-option:hover {
    outline: 2px solid var(--accent);
    outline-offset: -1px;
    filter: brightness(1.04);
  }
  .slot-color-option.is-selected {
    box-shadow: 0 0 0 2px var(--accent);
  }
  /* Custom Dark-Surface Select Dropdown (WebKit/GTK-safe across Auto, Dark & Light themes) */
  .custom-select {
    position: relative;
    display: inline-block;
    min-width: 148px;
  }
  .custom-select-trigger {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 5px 10px;
    border-radius: var(--radius-sm);
    border: 1px solid #464b49;
    background: #383d3b;
    color: #f8fafc;
    font-size: 12px;
    font-weight: 600;
    line-height: 1.3;
    cursor: pointer;
    text-align: left;
    white-space: nowrap;
  }
  .custom-select-trigger:hover,
  .custom-select.open .custom-select-trigger {
    background: #424846;
    color: #ffffff;
    border-color: var(--accent);
  }
  .custom-select-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .custom-select-caret {
    font-size: 10px;
    color: #cbd2d0;
    flex-shrink: 0;
  }
  .custom-select-menu {
    display: none;
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    min-width: 100%;
    z-index: 95;
    background: #2f3332;
    border: 1px solid #464b49;
    border-radius: var(--radius);
    padding: 4px;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.48);
    flex-direction: column;
    gap: 3px;
  }
  .custom-select.open .custom-select-menu {
    display: flex;
  }
  .custom-select.drop-up .custom-select-menu {
    top: auto;
    bottom: calc(100% + 4px);
  }
  .custom-select-option {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    padding: 6px 10px;
    border-radius: var(--radius-sm);
    border: 1px solid transparent;
    background: #383d3b;
    color: #f8fafc;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    text-align: left;
    white-space: nowrap;
  }
  .custom-select-option:hover {
    background: #424846;
    color: #ffffff;
    border-color: var(--accent);
  }
  .custom-select-option.is-selected {
    background: #009688;
    color: #ffffff;
    border-color: #00a696;
  }
  .fil-swatch-lg {
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    flex-shrink: 0;
    box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.3);
  }
  .fil-card-meta { min-width: 0; flex: 1; }
  .fil-card-title {
    font-weight: 700;
    font-size: 12.5px;
    display: flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .fil-card-sub {
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .fil-arrow-col {
    color: var(--muted);
    font-weight: 700;
    font-size: 14px;
  }
  .fil-card-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding-top: 6px;
    border-top: 1px solid var(--border);
    font-size: 11px;
  }

  /* Material Mismatch Confirmation Gate Banner */
  .mismatch-gate-box {
    display: none;
    flex-direction: column;
    gap: 6px;
    padding: 10px 12px;
    border-radius: var(--radius-sm);
    background: var(--warn-bg);
    border: 1px solid var(--warn);
    color: var(--warn-fg);
    font-size: 12px;
  }
  .mismatch-gate-box.visible {
    display: flex;
  }
  .mismatch-gate-label {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--fg);
    font-weight: 700;
    font-size: 12px;
    cursor: pointer;
  }
  .modal-feedback-banner {
    display: none;
    padding: 8px 12px;
    border-radius: var(--radius-sm);
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--border);
    background: var(--surface-sub);
    color: var(--fg);
  }
  .modal-feedback-banner.visible { display: block; }
  .modal-feedback-banner.ok {
    background: var(--ok-bg);
    border-color: var(--ok);
    color: var(--ok);
  }
  .modal-feedback-banner.info {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--fg);
  }
  .modal-feedback-banner.warn {
    background: var(--warn-bg);
    border-color: var(--warn);
    color: var(--warn-fg);
  }
  .modal-feedback-banner.error {
    background: var(--danger-bg);
    border-color: var(--danger);
    color: var(--danger-fg);
  }
  .job-meta-file-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    min-width: 0;
  }
  .job-meta-file-row .job-meta-val {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .btn-clear-inline {
    width: 20px;
    height: 20px;
    min-width: 20px;
    padding: 0;
    border-radius: var(--radius-sm);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    font-weight: 700;
    line-height: 1;
    color: var(--muted);
    background: var(--surface-sub);
    border: 1px solid var(--border);
    flex-shrink: 0;
    cursor: pointer;
  }
  .btn-clear-inline:hover {
    color: var(--danger);
    border-color: var(--danger);
    background: var(--danger-bg);
  }
  .modal-stale-banner {
    display: none;
    flex-direction: column;
    gap: 4px;
    padding: 8px 12px;
    border-radius: var(--radius-sm);
    background: var(--warn-bg);
    border: 1px solid var(--warn);
    color: var(--warn-fg);
    font-size: 12px;
    font-weight: 600;
  }
  .modal-stale-banner.visible {
    display: flex;
  }

  /* Pre-Print Options Footer Row */
  .preprint-toggles-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .toggle-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    background: var(--surface-card);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: 11.5px;
    font-weight: 600;
    color: var(--fg);
    cursor: pointer;
  }
  .toggle-pill.highlight {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .modal-actions-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }

  /* Mapping & Data Tables */
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media (max-width: 960px) { .grid-2 { grid-template-columns: 1fr; } }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--border); font-size: 12px; }
  th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.35px; font-weight: 600; }
  .map-summary {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    padding: 8px 10px;
    background: var(--surface-sub);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    margin-bottom: 10px;
    font-size: 12px;
  }
  .slot-target-preview {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin-top: 4px;
    font-size: 11px;
    color: var(--muted);
  }

  /* Flush Dashboard */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(165px, 1fr));
    gap: 9px;
    margin-bottom: 10px;
  }
  .kpi-box {
    background: var(--surface-sub);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 8px 10px;
  }
  .kpi-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; font-weight: 600; }
  .kpi-val { font-size: 14px; font-weight: 700; margin-top: 2px; color: var(--fg); }

  .warn-box {
    background: var(--warn-bg);
    border: 1px solid var(--warn);
    color: var(--warn-fg);
    padding: 8px 11px;
    border-radius: var(--radius-sm);
    margin: 6px 0;
    font-size: 12px;
  }
  .warn-box.error {
    background: var(--danger-bg);
    border-color: var(--danger);
    color: var(--danger-fg);
  }

  .matrix-scroll { overflow-x: auto; max-width: 100%; }
  .matrix-table th, .matrix-table td { text-align: center; padding: 6px 7px; font-size: 11px; border: 1px solid var(--border); }
  .events-details {
    margin-top: 10px;
    background: var(--surface-sub);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 8px 10px;
  }
  .events-details summary {
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    color: var(--fg);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .events-scroll { max-height: 220px; overflow-y: auto; margin-top: 8px; }

  .status-log {
    background: var(--log-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 9px 11px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11px;
    color: var(--muted);
    max-height: 300px;
    overflow-y: auto;
    white-space: pre-wrap;
  }
</style>
</head>
<body>
<div class="app-shell">
  <!-- Unified Compact Top Bar (Single Screen, Zero Duplicate Telemetry) -->
  <header class="topbar">
    <div class="brand-block">
      <div class="brand-mark">ACE</div>
      <div>
        <h1 class="brand-title" data-i18n="ui.brand.title">Anycubic Kobra S1 — Vanilla Klipper + ACE Pro</h1>
        <div class="brand-sub" data-i18n="ui.brand.sub">OrcaSlicer 2.5.x Color Mapping, Flush2Len &amp; Moonraker Workbench</div>
      </div>
    </div>

    <div class="top-telemetry">
      <div class="exec-tile">
        <span id="connBadge" class="badge empty" data-i18n="ui.conn.connecting">Moonraker: Connecting...</span>
        <span class="exec-value" id="execConnVal" data-i18n="ui.conn.disconnected">Disconnected</span>
        <span class="exec-sub" id="execConnSub">http://192.168.8.80:7125</span>
      </div>
      <div class="exec-tile">
        <span id="headerPrintBadge" class="badge empty" data-i18n="ui.printer.standby">Printer: Standby</span>
      </div>
      <div class="exec-tile">
        <span class="exec-kicker" data-i18n="ui.exec.tool.kicker">Active Toolhead</span>
        <span class="exec-value" id="execToolVal" data-i18n="ui.exec.tool.none">None (-1)</span>
        <span class="exec-sub" id="execToolSub">ACE Temp: — · Fan: —</span>
      </div>
      <div class="exec-tile">
        <span class="exec-kicker" data-i18n="ui.exec.dryer.kicker">ACE Pro Dryer</span>
        <span class="exec-value" id="execDryerVal" data-i18n="ui.exec.dryer.standby">Standby</span>
        <span class="exec-sub" id="execDryerSub" data-i18n="ui.exec.dryer.inactive">No active drying</span>
      </div>
      <div class="exec-tile">
        <span class="exec-kicker" data-i18n="ui.exec.gcode.kicker">Active G-code &amp; Purge</span>
        <span class="exec-value" id="execGcodeVal" data-i18n="ui.exec.gcode.none">No file</span>
        <span class="exec-sub" id="execGcodeSub" data-i18n="ui.exec.gcode.select_sub">Select G-code for Preview / Patch</span>
      </div>
    </div>

    <div class="topbar-right">
      <button id="btnNavSettings" onclick="openDrawer('settingsModal')" data-i18n="ui.nav.settings">⚙️ Connection</button>
      <button id="btnNavDiagnostics" onclick="openDrawer('diagnosticsModal')" data-i18n="ui.nav.diagnostics">📊 Flush &amp; Audit</button>
      <button id="btnNavLog" onclick="openDrawer('logModal')" data-i18n="ui.nav.log">📋 Log</button>
      <div class="segmented" role="group" aria-label="Select interface language" data-i18n-aria-label="header.language.aria">
        <button id="langBtnEn" class="active" onclick="setLanguage('en', true)" data-i18n="header.language.en">English</button>
        <button id="langBtnDe" onclick="setLanguage('de', true)" data-i18n="header.language.de">Deutsch</button>
      </div>
      <div class="segmented" role="group" aria-label="Select color theme" data-i18n-aria-label="header.theme.aria">
        <button id="themeBtnAuto" class="active" onclick="setThemeMode('auto')" data-i18n="header.theme.auto">Auto</button>
        <button id="themeBtnDark" onclick="setThemeMode('dark')" data-i18n="header.theme.dark">Dark</button>
        <button id="themeBtnLight" onclick="setThemeMode('light')" data-i18n="header.theme.light">Light</button>
      </div>
      <button onclick="requestStatus()" data-i18n="ui.header.refresh">🔄 Refresh</button>
    </div>
  </header>

  <div id="printSafetyBanner" class="safety-banner">
    <div class="safety-banner-inner">
      <span><span data-i18n="ui.safety.banner_prefix">⚠️ Print active (</span><span id="printSafetyState">printing</span><span data-i18n="ui.safety.banner_suffix">) — All hardware, motion, load/unload, and direct-print commands are locked to protect the active print.</span></span>
      <span class="badge danger" data-i18n="ui.safety.badge">Hardware Lock Active</span>
    </div>
  </div>

  <!-- Primary View: ACE Pro Live Slot Bay (T0-T7, 4x2 Grid) -->
  <section class="card slots-main-card">
    <div class="card-head">
      <h2 class="card-title" data-i18n="ui.sec3.title">3. ACE Pro Live Slot Bay (T0–T7)</h2>
      <div class="row" style="margin:0;">
        <label for="feedLength" data-i18n="ui.sec3.feed_label">Feed/Retract (mm):</label>
        <input type="number" id="feedLength" value="50" min="1" max="2000" style="width:68px;" />
        <button id="btnUnloadActive" class="hw-action-btn" onclick="unloadActiveTool()" data-i18n="ui.sec2.btn_unload">⏏️ Unload (T-1)</button>
        <button onclick="requestStatus()" data-i18n="ui.sec3.btn_live">🔄 Live Status</button>
      </div>
    </div>
    <div id="slotsContainer" class="slots-grid"></div>
  </section>

  <!-- Single-Row Compact Dryer & Print Trigger Bar -->
  <section class="bottom-control-strip">
    <!-- Cluster 1: ACE Pro Dryer -->
    <div class="strip-cluster">
      <span class="cluster-tag" data-i18n="ui.bar.dryer_label">ACE Dryer:</span>
      <span id="dryerBadge" class="badge empty" data-i18n="ui.sec2.dryer_stop_badge">Dryer: Stop</span>
      <label for="dryerUnitTrigger" data-i18n="ui.sec2.unit_label">Unit:</label>
      <div id="dryerUnit" class="custom-select dryer-unit-dropdown" data-value="all">
        <button type="button" id="dryerUnitTrigger" class="custom-select-trigger" onclick="toggleDryerUnitDropdown(event)" aria-haspopup="listbox" aria-expanded="false" data-i18n-aria-label="ui.sec2.unit_select_aria">
          <span id="dryerUnitLabel" class="custom-select-label" data-i18n="ui.sec2.unit_all">Both (All ACE)</span>
          <span class="custom-select-caret" aria-hidden="true">▾</span>
        </button>
        <div id="dryerUnitMenu" class="custom-select-menu" role="listbox">
          <button type="button" role="option" aria-selected="true" class="custom-select-option is-selected" data-value="all" data-i18n="ui.sec2.unit_all" onclick="selectDryerUnit('all', event)">Both (All ACE)</button>
          <button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="0" data-i18n="ui.sec2.unit_ace1" onclick="selectDryerUnit('0', event)">ACE 1 (T0–T3)</button>
          <button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="1" data-i18n="ui.sec2.unit_ace2" onclick="selectDryerUnit('1', event)">ACE 2 (T4–T7)</button>
        </div>
      </div>
      <label for="dryerTemp" data-i18n="ui.sec2.temp_label">Temp (°C):</label>
      <input type="number" id="dryerTemp" value="50" min="20" max="70" onchange="onDryerSettingsChange()" />
      <label for="dryerDuration" data-i18n="ui.sec2.dur_label">Duration (min):</label>
      <input type="number" id="dryerDuration" value="240" min="1" max="1440" onchange="onDryerSettingsChange()" />
      <button id="btnDryerStart" class="primary" onclick="startDryer()" data-i18n="ui.sec2.btn_dryer_start">🔥 Dryer Start</button>
      <button id="btnDryerStop" class="danger" onclick="stopDryer()" data-i18n="ui.sec2.btn_dryer_stop">⏹️ Dryer Stop</button>
    </div>

    <!-- Cluster 2: G-code Path & Pre-Print Send Modal Trigger -->
    <div class="print-trigger-cluster">
      <input type="text" id="gcodePathInput" placeholder="/path/to/file.gcode" data-i18n-placeholder="ui.sec5.path_placeholder" style="width:240px;" />
      <button onclick="parseGcodeHeader()" data-i18n="ui.sec5.btn_parse">📄 Read Filaments from Header</button>
      <button id="btnOpenPrePrintModal" class="primary btn-cta-send" onclick="openPrePrintModal()" data-i18n="ui.bar.btn_prepare">🚀 Prepare &amp; Send Print...</button>
    </div>
  </section>
</div>

<!-- ===================================================================== -->
<!-- INTERACTIVE PRE-PRINT SEND MODAL (SnapSend-Inspired Single-Card View) -->
<!-- ===================================================================== -->
<div id="prePrintModal" class="modal-backdrop" role="dialog" aria-modal="true">
  <div class="modal-dialog">
    <div class="modal-header">
      <div>
        <h2 class="modal-title" data-i18n="ui.modal.preprint.title">Send Print Job — Filament Mapping &amp; Pre-Print Check</h2>
        <div style="font-size:11px;color:var(--muted);" data-i18n="ui.modal.preprint.subtitle">Assign Slicer extruders to physical ACE Pro slots (T0–T7) and verify material compatibility before sending.</div>
      </div>
      <div class="row" style="margin:0;">
        <input type="file" id="modalGcodeFileInput" accept=".gcode,.gco,.gc" style="display:none;" onchange="handleModalGcodeFileSelect(event)" />
        <button type="button" id="btnModalLoadSlicer" onclick="acquireSlicerGcode(true)" data-i18n="ui.modal.btn.load_slicer">🔄 Load from Slicer</button>
        <button type="button" id="btnModalBrowseGcode" onclick="triggerModalGcodeBrowse()" data-i18n="ui.modal.btn.browse_gcode">📁 Browse .gcode</button>
        <span id="modalPrinterBadge" class="badge empty" data-i18n="ui.printer.standby">Printer: Standby</span>
        <button type="button" onclick="closePrePrintModal()" data-i18n="ui.modal.close">✕ Close</button>
      </div>
    </div>

    <div class="modal-body">
      <!-- Top-Left Enlarged G-code Thumbnail & 2x2 Job Metadata Strip -->
      <div class="modal-job-hero">
        <div id="modalThumbBox" class="modal-thumb-box" onclick="onModalThumbBoxClick(event)" title="No embedded G-code thumbnail found" data-i18n-title="ui.modal.thumb.fallback_title">
          <img id="modalThumbImg" class="modal-thumb-img" src="" alt="" style="display:none;" />
          <canvas id="modalThumbCanvas" class="modal-thumb-canvas" style="display:none;" aria-label="G-code preview thumbnail" data-i18n-aria-label="ui.modal.thumb.alt"></canvas>
          <span id="modalThumbFallback" class="modal-thumb-fallback" aria-hidden="true">🧊</span>
          <button type="button" id="btnThumbColorMode" class="thumb-mode-badge" style="display:none;" onclick="toggleThumbnailColorMode(event)" title="Toggle live preview between mapped ACE Pro colors and original Slicer colors" data-i18n-title="ui.modal.thumb.mode_title" data-i18n="ui.modal.thumb.mode_mapped">🎨 Mapped</button>
          <button type="button" id="btnThumbZoom" class="thumb-zoom-badge" style="display:none;" onclick="openThumbZoomModal(event)" title="Click to inspect full-size preview (zoom)" data-i18n-title="ui.modal.thumb.zoom_title" aria-label="Click to inspect full-size preview (zoom)" data-i18n-aria-label="ui.modal.thumb.zoom_title">🔍</button>
        </div>
        <div class="job-meta-grid">
          <div class="job-meta-box">
            <div class="kpi-label" data-i18n="ui.exec.gcode.kicker">Active G-code &amp; Purge</div>
            <div class="job-meta-file-row">
              <div class="job-meta-val" id="modalJobFile" data-i18n="ui.modal.job.no_file">No G-code file selected</div>
              <button type="button" id="btnClearModalGcode" class="btn-clear-inline" style="display:none;" onclick="clearModalGcode()" title="Clear loaded G-code" data-i18n-title="ui.modal.btn.clear_gcode_title" aria-label="Clear loaded G-code" data-i18n-aria-label="ui.modal.btn.clear_gcode_title">×</button>
            </div>
          </div>
          <div class="job-meta-box">
            <div class="kpi-label" data-i18n="ui.modal.kpi.time_layers">Est. Time &amp; Layers</div>
            <div class="job-meta-val" id="modalJobTimeLayers">—</div>
          </div>
          <div class="job-meta-box">
            <div class="kpi-label" data-i18n="ui.modal.kpi.filament_mat">Filament Usage &amp; Weight</div>
            <div class="job-meta-val" id="modalJobWeight">0.0 g (0.00 m)</div>
          </div>
          <div class="job-meta-box">
            <div class="kpi-label" data-i18n="ui.flush.kpi_total">Total Purge</div>
            <div class="job-meta-val" id="modalJobPurge">0 · 0.0 mm</div>
          </div>
        </div>
      </div>

      <div id="modalStaleModelBanner" class="modal-stale-banner" role="alert">
        <div id="modalStaleModelText"></div>
        <div class="row" style="margin:2px 0 0 0;gap:6px;">
          <button type="button" class="subtle" onclick="triggerModalGcodeBrowse()" data-i18n="ui.modal.btn.browse_gcode">📁 Browse .gcode</button>
          <button type="button" class="subtle" onclick="clearModalGcode()" data-i18n="ui.modal.stale.clear_btn">× Unload Stale G-code</button>
        </div>
      </div>

      <div id="modalFeedbackBanner" class="modal-feedback-banner" role="status"></div>

      <!-- Filament Mapping Toolbar -->
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;padding-top:4px;border-top:1px solid var(--border);">
        <div class="row" style="margin:0;">
          <strong style="font-size:12.5px;" data-i18n="ui.modal.cards.title">Filament-to-Slot Mapping (Slicer Extruder → Physical ACE Slot)</strong>
          <label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;font-size:11px;">
            <input type="checkbox" id="chkShowUnusedExtruders" onchange="renderMappingTable()" />
            <span data-i18n="ui.modal.cards.show_unused">Show unused extruders</span>
          </label>
        </div>
        <div class="row" style="margin:0;">
          <button onclick="refreshPrePrintModal()" data-i18n="ui.header.refresh">🔄 Refresh</button>
          <button onclick="syncOrcaFilaments()" data-i18n="ui.sec4.btn_project">🔄 OrcaSlicer Project</button>
          <button class="primary" onclick="autoMatchColors()" data-i18n="ui.sec4.btn_automatch">✨ Auto-Match</button>
          <button class="subtle" onclick="resetColorMapping()" data-i18n="ui.sec4.btn_reset">↺ 1:1 Default</button>
        </div>
      </div>

      <div id="mappingSummaryBar" class="map-summary" data-i18n="ui.sec4.summary_ready">Ready for filament assignment.</div>

      <!-- Visual Filament-to-Slot Mapping Cards -->
      <div id="modalMappingCards" class="mapping-cards-grid"></div>

      <!-- Material Mismatch Explicit Confirmation Gate -->
      <div id="modalMismatchGate" class="mismatch-gate-box">
        <div>
          <strong data-i18n="ui.modal.mismatch_gate.title">⚠️ Material Mismatch Detected</strong>
          <div style="font-size:11px;margin-top:2px;" data-i18n="ui.modal.mismatch_gate.desc">One or more mapped ACE Pro slots contain a different material than configured in OrcaSlicer. Please reassign the slot or explicitly confirm below to proceed.</div>
          <div id="modalMismatchList" style="font-size:11px;margin-top:4px;font-weight:700;"></div>
        </div>
        <label class="mismatch-gate-label">
          <input type="checkbox" id="chkConfirmMaterialMismatch" onchange="onMismatchConfirmChange(this.checked)" />
          <span data-i18n="ui.modal.mismatch_gate.checkbox">I acknowledge the material mismatch(es) and confirm printing with the selected ACE Pro slots.</span>
        </label>
      </div>
    </div>

    <!-- Compact Pre-Print Options & Primary Action Footer -->
    <div class="modal-footer">
      <div class="preprint-toggles-row">
        <span style="font-size:11px;font-weight:700;text-transform:uppercase;color:var(--muted);" data-i18n="ui.modal.options.title">Pre-Print Hardware &amp; Purge Options</span>
        <label class="toggle-pill">
          <input type="checkbox" id="chkBedLeveling" checked onchange="syncSettings()" />
          <span data-i18n="ui.modal.opt.g31">Bed Leveling (G31)</span>
        </label>
        <label class="toggle-pill">
          <input type="checkbox" id="chkSkipPurgeLoaded" checked onchange="syncSettings()" />
          <span data-i18n="ui.modal.opt.skip_purge">Skip Purge for Already Loaded Tool (SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1)</span>
        </label>
        <label class="toggle-pill">
          <input type="checkbox" id="chkCutTipPost" onchange="syncSettings()" />
          <span data-i18n="ui.modal.opt.cut_post">Cut Tip after Print (PRINT_END CUT_TIP=1)</span>
        </label>
        <label class="toggle-pill">
          <input type="checkbox" id="chkForceInitialPurge" onchange="syncSettings()" />
          <span data-i18n="ui.modal.opt.force_purge">Force Initial Purge</span>
        </label>
      </div>

      <div class="modal-actions-bar">
        <button id="btnInspectFlushPreview" onclick="inspectFlushFromModal()" data-i18n="ui.modal.btn.inspect_flush">🔎 Inspect Flush Volume &amp; Post-Processing Preview</button>
        <div class="row" style="margin:0;">
          <button type="button" onclick="closePrePrintModal()" data-i18n="ui.modal.btn.cancel">Cancel</button>
          <button id="btnModalApplyLocal" class="primary" onclick="runPostProcessAndUpload(false, false)" data-i18n="ui.modal.btn.apply_local">⚙️ Apply &amp; Patch Locally</button>
          <button id="btnModalUploadOnly" class="primary" onclick="runPostProcessAndUpload(true, false)" data-i18n="ui.modal.btn.upload_only">⬆️ Upload Only</button>
          <button id="btnModalSendPrint" class="primary btn-cta-send" onclick="runPostProcessAndUpload(true, true)" data-i18n="ui.modal.btn.send_print">🚀 Upload &amp; Start Print</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ===================================================================== -->
<!-- SECONDARY MODAL 1: MOONRAKER CONNECTION SETTINGS                      -->
<!-- ===================================================================== -->
<div id="settingsModal" class="modal-backdrop" role="dialog" aria-modal="true">
  <div class="modal-dialog modal-sm">
    <div class="modal-header">
      <h2 class="modal-title" data-i18n="ui.sec1.title">1. Moonraker Connection</h2>
      <button type="button" onclick="closeDrawer('settingsModal')" data-i18n="ui.modal.close">✕ Close</button>
    </div>
    <div class="modal-body">
      <div><span class="badge info" data-i18n="ui.sec1.offline_badge">Offline analysis works independently</span></div>
      <div class="row">
        <label for="moonrakerUrl" data-i18n="ui.sec1.url_label">URL:</label>
        <input type="text" id="moonrakerUrl" value="http://192.168.8.80:7125" placeholder="http://192.168.8.80:7125" data-i18n-placeholder="sec1.url.placeholder" style="flex:1;" />
        <label><input type="checkbox" id="port80Fallback" checked /> <span data-i18n="ui.sec1.port80">Port 80 Fallback</span></label>
      </div>
      <div class="row">
        <label for="apiKey" data-i18n="ui.sec1.apikey_label">API Key:</label>
        <input type="text" id="apiKey" value="" placeholder="Optional X-Api-Key" data-i18n-placeholder="ui.sec1.apikey_placeholder" style="flex:1;" />
        <button class="primary" onclick="saveSettingsAndRefresh(); closeDrawer('settingsModal');" data-i18n="ui.sec1.connect_btn">Connect &amp; Load Status</button>
      </div>
      <hr class="divider" />
      <div id="deviceMeta" style="color: var(--muted); font-size: 12px;" data-i18n="ui.sec1.meta_initial">
        ACE Status: — | Active Tool: — | Temp: —
      </div>
    </div>
  </div>
</div>

<!-- ===================================================================== -->
<!-- SECONDARY MODAL 2: FLUSH VOLUME MONITORING & POST-PROCESSING AUDIT    -->
<!-- ===================================================================== -->
<div id="diagnosticsModal" class="modal-backdrop" role="dialog" aria-modal="true">
  <div class="modal-dialog modal-lg">
    <div class="modal-header">
      <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
        <h2 class="modal-title" data-i18n="sec6.title">6. Flush Volume Monitoring &amp; Post-Processing Audit</h2>
        <span id="flushModeBadge" class="badge empty">—</span>
        <span id="flushStatusBadge" class="badge empty" data-i18n="ui.sec6.no_analysis">No analysis yet</span>
      </div>
      <div class="row" style="margin:0;">
        <button onclick="previewGcodeFlush()" data-i18n="ui.sec5.btn_preview">🔎 Offline Flush Analysis (Preview)</button>
        <button type="button" onclick="closeDrawer('diagnosticsModal')" data-i18n="ui.modal.close">✕ Close</button>
      </div>
    </div>
    <div class="modal-body">
      <div id="flushKpiGrid" class="kpi-grid"></div>
      <div id="flushAuditWarnings"></div>

      <div class="grid-2" style="margin-top:6px;">
        <div>
          <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
            <h3 style="margin:0;" data-i18n="ui.sec6.vec_title">Initial Flush Vector (flush_volumes_vector)</h3>
            <span id="vectorLayoutBadge" class="badge empty">—</span>
          </div>
          <div class="matrix-scroll">
            <table>
              <thead>
                <tr>
                  <th data-i18n="ui.sec6.col_extruder">Extruder</th>
                  <th data-i18n="ui.sec6.ev_col_slot">ACE Slot</th>
                  <th data-i18n="ui.sec6.col_unload">Unload (mm³)</th>
                  <th data-i18n="ui.sec6.col_load">Load (mm³)</th>
                  <th data-i18n="ui.sec6.col_eff">Effective (mm³)</th>
                  <th data-i18n="ui.sec6.col_purge">Purge (mm)</th>
                </tr>
              </thead>
              <tbody id="flushVectorBody"></tbody>
            </table>
          </div>
        </div>
        <div>
          <h3 data-i18n="ui.sec6.mat_title">N×N Flush Matrix Heatmap (Slicer From → To)</h3>
          <div id="flushMatrixContainer" class="matrix-scroll"></div>
        </div>
      </div>

      <details id="flushEventsDetails" class="events-details" open>
        <summary>
          <span data-i18n="ui.sec6.events_summary">Toolchange Sequence &amp; Injected ACE_SET_PURGE_AMOUNT Commands</span>
          <span id="eventsCountBadge" class="badge empty">0 Events</span>
        </summary>
        <div class="events-scroll">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th data-i18n="ui.sec6.ev_col_tc">Slicer Toolchange</th>
                <th data-i18n="ui.sec6.ev_col_slot">ACE Slot</th>
                <th data-i18n="ui.sec6.ev_col_raw">Raw (mm³)</th>
                <th data-i18n="ui.sec6.ev_col_eff">Effective (mm³)</th>
                <th data-i18n="ui.sec6.ev_col_len">Purge Length (mm)</th>
              </tr>
            </thead>
            <tbody id="flushEventsBody"></tbody>
          </table>
        </div>
      </details>
    </div>
  </div>
</div>

<!-- ===================================================================== -->
<!-- SECONDARY MODAL 3: STATUS & AUDIT LOG                                 -->
<!-- ===================================================================== -->
<div id="logModal" class="modal-backdrop" role="dialog" aria-modal="true">
  <div class="modal-dialog modal-sm">
    <div class="modal-header">
      <h2 class="modal-title" data-i18n="ui.log.title">Status &amp; Audit Log</h2>
      <div class="row" style="margin:0;">
        <button class="subtle" onclick="clearStatusLog()" data-i18n="ui.log.clear_btn">Clear</button>
        <button type="button" onclick="closeDrawer('logModal')" data-i18n="ui.modal.close">✕ Close</button>
      </div>
    </div>
    <div class="modal-body">
      <div id="statusLog" class="status-log"></div>
    </div>
  </div>
</div>

<!-- ===================================================================== -->
<!-- SECONDARY MODAL 4: THUMBNAIL DETAIL ZOOM LIGHTBOX                     -->
<!-- ===================================================================== -->
<div id="thumbZoomModal" class="modal-backdrop modal-backdrop-zoom" role="dialog" aria-modal="true" onclick="onThumbZoomBackdropClick(event)">
  <div class="thumb-zoom-dialog" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div style="min-width:0;">
        <h2 class="modal-title" data-i18n="ui.modal.thumb.zoom_heading">Model Color Preview (Zoom)</h2>
        <div id="zoomThumbFilename" style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"></div>
      </div>
      <div class="row" style="margin:0;">
        <button type="button" id="btnZoomColorMode" onclick="toggleThumbnailColorMode(event)" title="Toggle live preview between mapped ACE Pro colors and original Slicer colors" data-i18n-title="ui.modal.thumb.mode_title" data-i18n="ui.modal.thumb.mode_mapped">🎨 Mapped</button>
        <button type="button" id="btnZoomClose" onclick="closeThumbZoomModal(event)" data-i18n="ui.modal.thumb.zoom_close">✕ Close (Esc)</button>
      </div>
    </div>
    <div class="thumb-zoom-stage" onclick="closeThumbZoomModal(event)">
      <canvas id="zoomThumbCanvas" class="thumb-zoom-canvas" onclick="closeThumbZoomModal(event)" aria-label="G-code preview thumbnail" data-i18n-aria-label="ui.modal.thumb.alt"></canvas>
    </div>
    <div class="thumb-zoom-footer">
      <span class="hint" data-i18n="ui.modal.thumb.zoom_dismiss_hint">Click preview image, backdrop, or press Esc to return to Pre-Print dialog.</span>
    </div>
  </div>
</div>

<script>
  const translations = __I18N_JSON__;
  let currentLanguage = "en";

  function normalizeLang(lang) {
    if (typeof lang === "string") {
      const c = lang.trim().toLowerCase();
      if (c === "de" || c.startsWith("de")) return "de";
      if (c === "en" || c.startsWith("en")) return "en";
    }
    return "en";
  }

  function t(key, params, langOverride) {
    const lang = normalizeLang(langOverride || currentLanguage);
    const dict = translations[lang] || translations.en || {};
    const enDict = translations.en || {};
    let tmpl = dict[key];
    if (!tmpl) tmpl = enDict[key] || key;
    if (params && typeof params === "object") {
      return String(tmpl).replace(/\\{([a-zA-Z0-9_]+)\\}/g, (m, k) => (
        params[k] !== undefined && params[k] !== null ? String(params[k]) : m
      ));
    }
    return String(tmpl);
  }

  function applyTranslations() {
    document.documentElement.setAttribute("lang", currentLanguage);
    [["en", "langBtnEn"], ["de", "langBtnDe"]].forEach(pair => {
      const btn = document.getElementById(pair[1]);
      if (btn) btn.classList.toggle("active", currentLanguage === pair[0]);
    });
    document.querySelectorAll("[data-i18n]").forEach(el => {
      const key = el.getAttribute("data-i18n");
      if (key) el.textContent = t(key);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
      const key = el.getAttribute("data-i18n-placeholder");
      if (key) el.setAttribute("placeholder", t(key));
    });
    document.querySelectorAll("[data-i18n-title]").forEach(el => {
      const key = el.getAttribute("data-i18n-title");
      if (key) el.setAttribute("title", t(key));
    });
    document.querySelectorAll("[data-i18n-aria-label]").forEach(el => {
      const key = el.getAttribute("data-i18n-aria-label");
      if (key) el.setAttribute("aria-label", t(key));
    });
    renderLiveHeaderAndMeta();
    renderExecutiveStrip();
    if (typeof setDryerUnitValue === "function") {
      setDryerUnitValue((typeof readDryerControls === "function") ? readDryerControls().unit : currentState.dryer_unit);
    }
    renderSlots();
    renderMappingTable();
    renderFlushDashboard();
    renderModalJobSummary();
    updateModalSendActionUi();
  }

  function setLanguage(lang, notifyHost) {
    const resolved = normalizeLang(lang);
    currentLanguage = resolved;
    currentState.language = resolved;
    applyTranslations();
    if (notifyHost) {
      sendToHost({ type: "set_language", language: resolved });
    }
  }

  let currentState = {
    language: "en",
    moonraker_url: "http://192.168.8.80:7125",
    enable_port80_fallback: true,
    bed_leveling: true,
    skip_purge_for_already_loaded_tool: true,
    cut_tip_after_print: false,
    force_initial_purge: false,
    start_print_immediately: false,
    dryer_unit: "all",
    dryer_temp: 50,
    dryer_duration: 240,
    material_mismatch_confirmed: false,
    last_job_metadata: {},
    tool_map: {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7},
    tool_map_sources: {0: "default", 1: "default", 2: "default", 3: "default", 4: "default", 5: "default", 6: "default", 7: "default"},
    slicer_filaments: [
      {slicer_tool: 0, label: "Extruder 0 (T0)", color: "#FFFFFF", material: "PETG", used: true, used_g: 0, used_m: 0},
      {slicer_tool: 1, label: "Extruder 1 (T1)", color: "#000000", material: "PETG", used: true, used_g: 0, used_m: 0}
    ],
    ace_slots: [
      {tool: 0, label: "T0", status: "ready", hex_color: "#FFFFFF", material: "PETG", temp: 245, rfid: false},
      {tool: 1, label: "T1", status: "ready", hex_color: "#000000", material: "PETG", temp: 245, rfid: false},
      {tool: 2, label: "T2", status: "empty", hex_color: "#000000", material: "", temp: 0, rfid: false},
      {tool: 3, label: "T3", status: "empty", hex_color: "#000000", material: "", temp: 0, rfid: false},
      {tool: 4, label: "T4", status: "empty", hex_color: "#000000", material: "", temp: 0, rfid: false},
      {tool: 5, label: "T5", status: "empty", hex_color: "#000000", material: "", temp: 0, rfid: false},
      {tool: 6, label: "T6", status: "empty", hex_color: "#000000", material: "", temp: 0, rfid: false},
      {tool: 7, label: "T7", status: "empty", hex_color: "#000000", material: "", temp: 0, rfid: false}
    ]
  };

  /* Modal & Drawer Controls */
  function openDrawer(modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.add("open");
  }

  function closeDrawer(modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.remove("open");
  }

  function showModalFeedback(level, text) {
    const banner = document.getElementById("modalFeedbackBanner");
    if (!banner) return;
    if (!text) {
      banner.className = "modal-feedback-banner";
      banner.textContent = "";
      return;
    }
    const safeLevel = ["ok", "info", "warn", "error"].includes(level) ? level : "info";
    banner.className = `modal-feedback-banner visible ${safeLevel}`;
    banner.textContent = String(text);
  }

  function clearModalFeedback() {
    showModalFeedback("", "");
  }

  function openPrePrintModal() {
    clearModalFeedback();
    openDrawer("prePrintModal");
    renderModalJobSummary();
    renderMappingTable();
    acquireSlicerGcode(false);
  }

  function refreshPrePrintModal() {
    requestStatus();
    acquireSlicerGcode(true);
  }

  function acquireSlicerGcode(forceRefresh) {
    const gcodeEl = document.getElementById("gcodePathInput");
    const rawPath = gcodeEl ? gcodeEl.value.trim() : "";
    const curPath = String(currentState.last_gcode_path || "").trim();
    const gcodePath = (!forceRefresh && rawPath && rawPath !== curPath) ? rawPath : "";
    if (forceRefresh) {
      showModalFeedback("info", t("ui.modal.feedback.loading_slicer"));
    }
    sendToHost({
      type: "acquire_slicer_gcode",
      gcode_path: gcodePath,
      force_refresh: !!forceRefresh
    });
  }

  function triggerModalGcodeBrowse() {
    const inp = document.getElementById("modalGcodeFileInput");
    if (inp) {
      inp.value = "";
      inp.click();
    }
  }

  function handleModalGcodeFileSelect(evt) {
    const file = evt && evt.target && evt.target.files ? evt.target.files[0] : null;
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function(loadEvt) {
      const content = String((loadEvt && loadEvt.target && loadEvt.target.result) || "");
      sendToHost({
        type: "load_gcode_content",
        filename: file.name || "selected.gcode",
        content: content
      });
    };
    reader.readAsText(file);
  }

  function closePrePrintModal() {
    closeThumbZoomModal();
    clearHoveredExtruder();
    closeAllSlotDropdowns();
    closeDrawer("prePrintModal");
  }

  function inspectFlushFromModal() {
    const gcodePath = document.getElementById("gcodePathInput").value.trim();
    if (gcodePath) {
      previewGcodeFlush();
    }
    openDrawer("diagnosticsModal");
  }

  function escapeHtml(val) {
    return String(val === undefined || val === null ? "" : val)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function safeHexColor(col, fallback) {
    const raw = String(col || "").trim();
    if (/^#[0-9A-Fa-f]{6}$/.test(raw)) return raw.toUpperCase();
    if (/^[0-9A-Fa-f]{6}$/.test(raw)) return "#" + raw.toUpperCase();
    return fallback || "#64748B";
  }

  function parseCssColorLuminance(cssColor) {
    if (!cssColor) return null;
    const s = cssColor.trim();
    let r, g, b;
    const hexM = /^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$/.exec(s);
    if (hexM) {
      r = parseInt(hexM[1].slice(0, 2), 16);
      g = parseInt(hexM[1].slice(2, 4), 16);
      b = parseInt(hexM[1].slice(4, 6), 16);
    } else {
      const rgbM = /^rgba?\\(\\s*([0-9.]+)\\s*,\\s*([0-9.]+)\\s*,\\s*([0-9.]+)/.exec(s);
      if (!rgbM) return null;
      r = Number(rgbM[1]);
      g = Number(rgbM[2]);
      b = Number(rgbM[3]);
    }
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0;
  }

  function applyResolvedTheme(mode) {
    const html = document.documentElement;
    html.setAttribute("data-theme", mode);
    let resolved = "dark";
    if (mode === "light" || mode === "dark") {
      resolved = mode;
    } else {
      const orcaBg = getComputedStyle(html).getPropertyValue("--orca-bg").trim();
      const lum = parseCssColorLuminance(orcaBg);
      if (lum !== null) {
        resolved = lum > 0.5 ? "light" : "dark";
      } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
        resolved = "light";
      }
    }
    html.setAttribute("data-resolved", resolved);
    [["auto", "themeBtnAuto"], ["dark", "themeBtnDark"], ["light", "themeBtnLight"]].forEach(pair => {
      const btn = document.getElementById(pair[1]);
      if (btn) btn.classList.toggle("active", mode === pair[0]);
    });
  }

  function setThemeMode(mode) {
    try { localStorage.setItem("kobra_ace_theme", mode); } catch (e) {}
    applyResolvedTheme(mode);
  }

  function initTheme() {
    let saved = "auto";
    try { saved = localStorage.getItem("kobra_ace_theme") || "auto"; } catch (e) {}
    if (!["auto", "dark", "light"].includes(saved)) saved = "auto";
    applyResolvedTheme(saved);
    if (window.matchMedia) {
      const mq = window.matchMedia("(prefers-color-scheme: light)");
      if (typeof mq.addEventListener === "function") {
        mq.addEventListener("change", () => {
          if (document.documentElement.getAttribute("data-theme") === "auto") {
            applyResolvedTheme("auto");
          }
        });
      }
    }
  }

  function ensureLogInitialized() {
    const el = document.getElementById("statusLog");
    if (el && !el.textContent.trim()) {
      const ts = new Date().toLocaleTimeString();
      el.textContent = `[${ts}] ${t("ui.log.initial")}\\n`;
    }
  }

  function logMsg(msg) {
    const el = document.getElementById("statusLog");
    if (!el || !msg) return;
    const ts = new Date().toLocaleTimeString();
    const cur = el.textContent || "";
    el.textContent = `[${ts}] ${msg}\\n` + cur;
  }

  function clearStatusLog() {
    const el = document.getElementById("statusLog");
    if (el) {
      const ts = new Date().toLocaleTimeString();
      el.textContent = `[${ts}] ${t("ui.log.cleared")}\\n`;
    }
  }

  function sendToHost(payload) {
    if (typeof orca !== "undefined" && typeof orca.postMessage === "function") {
      orca.postMessage(payload);
    } else {
      logMsg(t("log.no_host", { msg: JSON.stringify(payload) }));
    }
  }

  function filamentColorDot(slicerTool) {
    const fil = (currentState.slicer_filaments || []).find(f => Number(f.slicer_tool) === Number(slicerTool));
    const hex = fil ? safeHexColor(fil.color, "#64748B") : "#64748B";
    return `<span class="swatch-dot" style="background:${hex};"></span>`;
  }

  function renderLiveHeaderAndMeta() {
    const st = currentState.ace_status;
    if (!st) return;
    const connBadge = document.getElementById("connBadge");
    if (connBadge) {
      connBadge.textContent = st.connected
        ? t("ui.conn.connected", { model: st.model || "ACE Pro" })
        : t("ui.conn.disconnected");
      connBadge.className = "badge " + (st.connected ? "ready" : "empty");
    }

    const isPrinting = !!st.is_printing;
    const headerPrintBadge = document.getElementById("headerPrintBadge");
    const modalPrinterBadge = document.getElementById("modalPrinterBadge");
    if (headerPrintBadge) {
      headerPrintBadge.textContent = t("ui.printer.state", { state: st.print_state || "standby" });
      headerPrintBadge.className = "badge " + (isPrinting ? "danger" : "ready");
      if (modalPrinterBadge) {
        modalPrinterBadge.textContent = headerPrintBadge.textContent;
        modalPrinterBadge.className = headerPrintBadge.className;
      }
    }

    const banner = document.getElementById("printSafetyBanner");
    const bannerState = document.getElementById("printSafetyState");
    if (banner) {
      banner.style.display = isPrinting ? "block" : "none";
      if (bannerState) {
        bannerState.textContent = st.print_filename ? `${st.print_state}: ${st.print_filename}` : (st.print_state || "printing");
      }
    }
    document.querySelectorAll(".hw-action-btn").forEach(btn => {
      btn.disabled = isPrinting;
    });

    const activeToolStr = st.current_index >= 0 ? `T${st.current_index}` : t("ui.exec.tool.none");
    const printLabel = st.print_state ? t("ui.sec1.meta_print", { state: st.print_state }) : "";
    const metaEl = document.getElementById("deviceMeta");
    if (metaEl) {
      metaEl.textContent = t("ui.sec1.meta_live", {
        status: st.device_status || "—",
        tool: activeToolStr,
        temp: st.ambient_temp !== undefined ? st.ambient_temp : "—",
        fan: st.fan_speed !== undefined ? st.fan_speed : "—",
        print_label: printLabel,
      });
    }

    const dryerBadge = document.getElementById("dryerBadge");
    if (dryerBadge) {
      if (st.dryer && st.dryer.active) {
        const instList = Array.isArray(st.dryer.active_instances) ? st.dryer.active_instances : [];
        const unitPrefix = instList.length === 1 ? `ACE ${Number(instList[0]) + 1} · ` : "";
        dryerBadge.textContent = unitPrefix + t("ui.sec2.dryer_active_badge", {
          temp: st.dryer.target_temp,
          mins: st.dryer.remain_time_min,
        });
        dryerBadge.className = "badge drying";
      } else {
        dryerBadge.textContent = t("ui.sec2.dryer_stop_badge");
        dryerBadge.className = "badge empty";
      }
    }
  }

  function renderExecutiveStrip() {
    const st = currentState.ace_status || {};
    const info = currentState.last_postprocess_info || {};

    const connVal = document.getElementById("execConnVal");
    const connSub = document.getElementById("execConnSub");
    if (connVal && connSub) {
      connVal.textContent = st.connected
        ? t("ui.exec.conn.online", { state: st.print_state || "standby" })
        : t("ui.exec.conn.offline");
      connSub.textContent = `${currentState.moonraker_url || "http://192.168.8.80:7125"}`;
    }

    const toolVal = document.getElementById("execToolVal");
    const toolSub = document.getElementById("execToolSub");
    if (toolVal && toolSub) {
      const idx = (st.current_index !== undefined && st.current_index !== null) ? Number(st.current_index) : -1;
      if (idx >= 0) {
        const activeSlot = (currentState.ace_slots || []).find(s => Number(s.tool) === idx);
        const col = activeSlot ? safeHexColor(activeSlot.hex_color, "#64748B") : "#64748B";
        const mat = activeSlot && activeSlot.material ? ` (${escapeHtml(activeSlot.material)})` : "";
        toolVal.innerHTML = `<span class="swatch-dot" style="background:${col};vertical-align:middle;margin-right:6px;"></span>T${idx}${mat}`;
      } else {
        toolVal.textContent = t("ui.exec.tool.none");
      }
      const tempTxt = st.ambient_temp !== undefined ? `${st.ambient_temp}°C` : "—";
      const fanTxt = st.fan_speed !== undefined ? `${st.fan_speed} RPM` : "—";
      toolSub.textContent = t("ui.exec.tool.sub", { temp: tempTxt, fan: fanTxt });
    }

    const dryVal = document.getElementById("execDryerVal");
    const drySub = document.getElementById("execDryerSub");
    if (dryVal && drySub) {
      if (st.dryer && st.dryer.active) {
        dryVal.textContent = t("ui.exec.dryer.active", { temp: st.dryer.target_temp });
        drySub.textContent = t("ui.exec.dryer.remain", { mins: st.dryer.remain_time_min });
      } else {
        dryVal.textContent = t("ui.exec.dryer.standby");
        drySub.textContent = t("ui.exec.dryer.inactive");
      }
    }

    const gcVal = document.getElementById("execGcodeVal");
    const gcSub = document.getElementById("execGcodeSub");
    if (gcVal && gcSub) {
      const path = currentState.last_gcode_path || "";
      const base = path ? path.split(/[\\\\/]/).pop() : t("ui.exec.gcode.none");
      gcVal.textContent = base;
      if (info && info.toolchanges !== undefined) {
        gcSub.textContent = t("ui.exec.gcode.stats", {
          tc: info.toolchanges,
          len: Number(info.total_purge_length_mm || 0).toFixed(1),
          vol: Number(info.total_purge_volume_mm3 || 0).toFixed(0),
        });
      } else {
        gcSub.textContent = path ? t("ui.exec.gcode.ready_sub") : t("ui.exec.gcode.select_sub");
      }
    }
  }

  let thumbShowOriginal = false;
  let thumbPixelCache = null;
  let thumbOffscreenCanvas = null;
  let hoveredSlicerTool = null;
  let thumbPulseRafId = 0;

  function clamp01(val) {
    return val < 0 ? 0 : (val > 1 ? 1 : val);
  }

  function clamp255(val) {
    const rounded = Math.round(val);
    return rounded < 0 ? 0 : (rounded > 255 ? 255 : rounded);
  }

  function hexToRgbObj(hexCol, fallback) {
    const clean = safeHexColor(hexCol, fallback || "#64748B").slice(1);
    return {
      r: parseInt(clean.slice(0, 2), 16),
      g: parseInt(clean.slice(2, 4), 16),
      b: parseInt(clean.slice(4, 6), 16)
    };
  }

  function rgbToHsl(r, g, b) {
    const rn = clamp01(r / 255);
    const gn = clamp01(g / 255);
    const bn = clamp01(b / 255);
    const maxC = Math.max(rn, gn, bn);
    const minC = Math.min(rn, gn, bn);
    const l = 0.5 * (maxC + minC);
    const diff = maxC - minC;
    if (diff <= 1e-9) {
      return { h: 0, s: 0, l: l };
    }
    const denom = l > 0.5 ? (2 - maxC - minC) : (maxC + minC);
    const s = denom > 1e-9 ? (diff / denom) : 0;
    let h = 0;
    if (maxC === rn) {
      h = (gn - bn) / diff + (gn < bn ? 6 : 0);
    } else if (maxC === gn) {
      h = (bn - rn) / diff + 2;
    } else {
      h = (rn - gn) / diff + 4;
    }
    return { h: ((h / 6) % 1 + 1) % 1, s: s, l: l };
  }

  function hueToRgbChannel(p, q, tVal) {
    let t = tVal;
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 0.5) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  }

  function hslToRgb(h, s, l) {
    const hN = ((h % 1) + 1) % 1;
    const sN = clamp01(s);
    const lN = clamp01(l);
    if (sN <= 1e-9) {
      const v = clamp255(lN * 255);
      return [v, v, v];
    }
    const q = lN < 0.5 ? (lN * (1 + sN)) : (lN + sN - lN * sN);
    const p = 2 * lN - q;
    return [
      clamp255(hueToRgbChannel(p, q, hN + 1 / 3) * 255),
      clamp255(hueToRgbChannel(p, q, hN) * 255),
      clamp255(hueToRgbChannel(p, q, hN - 1 / 3) * 255)
    ];
  }

  function expectedShadedLightness(lVal) {
    if (lVal < 0.15) return 0.13 + lVal * 0.5;
    if (lVal > 0.85) return 0.72 + (lVal - 0.85) * 0.2;
    return lVal * 0.85;
  }

  function getActiveSlicerPalette() {
    const filaments = Array.isArray(currentState.slicer_filaments) ? currentState.slicer_filaments : [];
    const hasAnyUsageData = filaments.some(f => (
      Number(f.used_g || 0) > 0
      || Number(f.used_mm || 0) > 0
      || Number(f.used_m || 0) > 0
      || Number(f.used_cm3 || 0) > 0
    ));
    const active = filaments.filter(f => isFilamentUsed(f, hasAnyUsageData));
    const chosen = active.length > 0 ? active : filaments;
    return chosen.map((f, idx) => {
      const hex = safeHexColor(f.color, "#64748B");
      const rgb = hexToRgbObj(hex, "#64748B");
      const hsl = rgbToHsl(rgb.r, rgb.g, rgb.b);
      const isChromatic = hsl.s >= 0.14 && hsl.l > 0.05 && hsl.l < 0.96;
      const sTool = (f.slicer_tool !== undefined && f.slicer_tool !== null) ? Number(f.slicer_tool) : idx;
      return {
        slicer_tool: sTool,
        hex: hex,
        rgb: rgb,
        hsl: hsl,
        isChromatic: isChromatic,
        expL: expectedShadedLightness(hsl.l)
      };
    });
  }

  function matchPixelToExtruder(r, g, b, a, palette) {
    if (a < 24 || !palette || palette.length === 0) return -1;
    const px = rgbToHsl(r, g, b);
    const pChromatic = px.s >= 0.14 && px.l > 0.05 && px.l < 0.96;
    let bestTool = -1;
    let bestScore = Infinity;

    if (pChromatic) {
      for (let i = 0; i < palette.length; i++) {
        const entry = palette[i];
        if (!entry.isChromatic) continue;
        let dh = Math.abs(px.h - entry.hsl.h);
        if (dh > 0.5) dh = 1 - dh;
        const dhDeg = dh * 360;
        if (dhDeg > 48) continue;
        const ds = Math.abs(px.s - entry.hsl.s);
        const dl = Math.abs(px.l - entry.expL);
        const score = (dhDeg / 45) * (dhDeg / 45) * 1.8
          + (ds / 0.65) * (ds / 0.65) * 0.45
          + (dl / 0.75) * (dl / 0.75) * 0.35;
        if (score < bestScore && score <= 1.35) {
          bestScore = score;
          bestTool = entry.slicer_tool;
        }
      }
      if (bestTool >= 0) return bestTool;
      if (px.s >= 0.20) return -1;
    }

    for (let i = 0; i < palette.length; i++) {
      const entry = palette[i];
      if (entry.isChromatic) continue;
      const dlExp = Math.abs(px.l - entry.expL);
      if (dlExp > 0.48) continue;
      const score = dlExp + px.s * 0.8;
      if (score < bestScore) {
        bestScore = score;
        bestTool = entry.slicer_tool;
      }
    }
    return bestTool;
  }

  function recolorThumbnailPixel(r, g, b, sourceHex, targetHex) {
    const srcNorm = safeHexColor(sourceHex, "#64748B");
    const tgtNorm = safeHexColor(targetHex, srcNorm);
    if (srcNorm === tgtNorm) {
      return [r, g, b];
    }
    const px = rgbToHsl(r, g, b);
    const srcRgb = hexToRgbObj(srcNorm, "#64748B");
    const srcHsl = rgbToHsl(srcRgb.r, srcRgb.g, srcRgb.b);
    const tgtRgb = hexToRgbObj(tgtNorm, srcNorm);
    const tgtHsl = rgbToHsl(tgtRgb.r, tgtRgb.g, tgtRgb.b);

    let k;
    if (srcHsl.l < 0.16) {
      k = Math.max(0.28, Math.min(1.15, (px.l + 0.05) / Math.max(0.22, srcHsl.l + 0.14)));
    } else {
      k = Math.max(0.25, Math.min(1.45, px.l / Math.max(0.16, srcHsl.l)));
    }

    let effTL = tgtHsl.l;
    if (tgtHsl.l < 0.14) {
      effTL = 0.13 + tgtHsl.l * 0.35;
    } else if (tgtHsl.l > 0.90) {
      effTL = 0.86;
    }

    const newL = Math.max(0.03, Math.min(0.96, effTL * k));
    let newS;
    if (tgtHsl.s <= 0.08) {
      newS = tgtHsl.s;
    } else if (srcHsl.s >= 0.15) {
      const satRatio = Math.max(0.72, Math.min(1.08, px.s / srcHsl.s));
      newS = clamp01(tgtHsl.s * satRatio);
    } else {
      newS = tgtHsl.s;
    }
    return hslToRgb(tgtHsl.h, newS, newL);
  }

  function buildThumbnailPixelCache(imgEl, palette, cacheKey) {
    const w = imgEl.naturalWidth;
    const h = imgEl.naturalHeight;
    if (!w || !h) return null;
    if (!thumbOffscreenCanvas) {
      thumbOffscreenCanvas = document.createElement("canvas");
    }
    thumbOffscreenCanvas.width = w;
    thumbOffscreenCanvas.height = h;
    const offCtx = thumbOffscreenCanvas.getContext("2d");
    if (!offCtx) return null;
    offCtx.clearRect(0, 0, w, h);
    offCtx.drawImage(imgEl, 0, 0, w, h);
    const imgData = offCtx.getImageData(0, 0, w, h);
    const origRgba = new Uint8ClampedArray(imgData.data);
    const pixelCount = w * h;
    const matchIdx = new Int8Array(pixelCount);
    for (let p = 0; p < pixelCount; p++) {
      const off = p * 4;
      matchIdx[p] = matchPixelToExtruder(
        origRgba[off],
        origRgba[off + 1],
        origRgba[off + 2],
        origRgba[off + 3],
        palette
      );
    }
    return {
      key: cacheKey,
      width: w,
      height: h,
      palette: palette,
      origRgba: origRgba,
      outImageData: offCtx.createImageData(w, h),
      matchIdx: matchIdx
    };
  }

  function renderThumbnailModeButton(hasThumb) {
    const thumbBox = document.getElementById("modalThumbBox");
    if (thumbBox) thumbBox.classList.toggle("is-zoomable", !!hasThumb);

    const btn = document.getElementById("btnThumbColorMode");
    const zoomBtn = document.getElementById("btnThumbZoom");
    const modalModeBtn = document.getElementById("btnZoomColorMode");
    const key = thumbShowOriginal ? "ui.modal.thumb.mode_original" : "ui.modal.thumb.mode_mapped";
    const label = t(key);

    if (zoomBtn) {
      zoomBtn.style.display = hasThumb ? "inline-flex" : "none";
    }
    if (modalModeBtn) {
      modalModeBtn.setAttribute("data-i18n", key);
      modalModeBtn.textContent = label;
      modalModeBtn.classList.toggle("is-original", !!thumbShowOriginal);
    }
    if (!btn) return;
    if (!hasThumb) {
      btn.style.display = "none";
      closeThumbZoomModal();
      return;
    }
    btn.style.display = "inline-flex";
    btn.setAttribute("data-i18n", key);
    btn.textContent = label;
    btn.classList.toggle("is-original", !!thumbShowOriginal);
  }

  function syncZoomCanvasFromCache() {
    const zoomCanvas = document.getElementById("zoomThumbCanvas");
    if (!zoomCanvas || !thumbPixelCache) return;
    const w = thumbPixelCache.width;
    const h = thumbPixelCache.height;
    if (!w || !h) return;
    if (zoomCanvas.width !== w || zoomCanvas.height !== h) {
      zoomCanvas.width = w;
      zoomCanvas.height = h;
    }
    zoomCanvas.style.setProperty("--thumb-aspect", String(w / h));
    const zCtx = zoomCanvas.getContext("2d");
    if (zCtx) {
      zCtx.putImageData(thumbPixelCache.outImageData, 0, 0);
    }
  }

  function closeThumbZoomModal(event) {
    if (event) event.stopPropagation();
    closeDrawer("thumbZoomModal");
  }

  function onThumbZoomBackdropClick(event) {
    if (!event || event.target === document.getElementById("thumbZoomModal")) {
      closeThumbZoomModal(event);
    }
  }

  function openThumbZoomModal(event) {
    if (event) event.stopPropagation();
    if (!thumbPixelCache) {
      updateThumbnailMappedColors();
    }
    if (!thumbPixelCache) return;
    clearHoveredExtruder();
    const fnEl = document.getElementById("zoomThumbFilename");
    const jobFileEl = document.getElementById("modalJobFile");
    if (fnEl && jobFileEl) {
      fnEl.textContent = jobFileEl.textContent || "";
    }
    renderThumbnailModeButton(true);
    drawThumbnailFrame(0);
    syncZoomCanvasFromCache();
    openDrawer("thumbZoomModal");
  }

  function onModalThumbBoxClick(event) {
    if (event && event.target && event.target.closest && event.target.closest(".thumb-mode-badge")) {
      return;
    }
    openThumbZoomModal(event);
  }

  function getThumbnailTargetColors(palette) {
    const slots = currentState.ace_slots || [];
    const targetByTool = {};
    const sourceByTool = {};
    for (let i = 0; i < palette.length; i++) {
      const entry = palette[i];
      const sTool = entry.slicer_tool;
      sourceByTool[sTool] = entry.hex;
      const mapped = (currentState.tool_map && currentState.tool_map[sTool] !== undefined)
        ? Number(currentState.tool_map[sTool])
        : sTool;
      const assignedSlot = slots.find(s => Number(s.tool) === mapped);
      const isValidSlot = assignedSlot && String(assignedSlot.status || "").toLowerCase() !== "empty";
      targetByTool[sTool] = isValidSlot ? safeHexColor(assignedSlot.hex_color, entry.hex) : entry.hex;
    }
    return { sourceByTool, targetByTool };
  }

  function drawThumbnailFrame(nowMs) {
    const thumbCanvas = document.getElementById("modalThumbCanvas");
    if (!thumbCanvas || !thumbPixelCache) return;
    const ctx = thumbCanvas.getContext("2d");
    if (!ctx) return;

    const cache = thumbPixelCache;
    const orig = cache.origRgba;
    const outData = cache.outImageData;
    const out = outData.data;
    const matchIdx = cache.matchIdx;
    const pixelCount = cache.width * cache.height;
    const colorMaps = getThumbnailTargetColors(cache.palette);
    const srcMap = colorMaps.sourceByTool;
    const tgtMap = colorMaps.targetByTool;
    const hoverTool = hoveredSlicerTool;
    const hasHover = hoverTool !== null && hoverTool >= 0;
    const pulse = hasHover ? (0.5 + 0.5 * Math.sin((nowMs || 0) * 0.006)) : 0;
    const lift = 0.08 + 0.10 * pulse;

    for (let p = 0; p < pixelCount; p++) {
      const off = p * 4;
      const a0 = orig[off + 3];
      if (a0 === 0) {
        out[off] = orig[off];
        out[off + 1] = orig[off + 1];
        out[off + 2] = orig[off + 2];
        out[off + 3] = 0;
        continue;
      }

      const toolIdx = matchIdx[p];
      let r = orig[off];
      let g = orig[off + 1];
      let b = orig[off + 2];

      if (!thumbShowOriginal && toolIdx >= 0) {
        const srcHex = srcMap[toolIdx];
        const tgtHex = tgtMap[toolIdx];
        if (srcHex && tgtHex && srcHex !== tgtHex) {
          const rgb = recolorThumbnailPixel(r, g, b, srcHex, tgtHex);
          r = rgb[0];
          g = rgb[1];
          b = rgb[2];
        }
      }

      if (hasHover) {
        if (toolIdx === hoverTool) {
          r = clamp255(r + (255 - r) * lift);
          g = clamp255(g + (255 - g) * lift);
          b = clamp255(b + (255 - b) * lift);
        } else {
          r = clamp255(r * 0.52);
          g = clamp255(g * 0.52);
          b = clamp255(b * 0.52);
        }
      }

      out[off] = r;
      out[off + 1] = g;
      out[off + 2] = b;
      out[off + 3] = a0;
    }

    ctx.putImageData(outData, 0, 0);
    const zoomModal = document.getElementById("thumbZoomModal");
    if (zoomModal && zoomModal.classList.contains("open")) {
      syncZoomCanvasFromCache();
    }
  }

  function stopThumbnailHoverLoop() {
    if (thumbPulseRafId && typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(thumbPulseRafId);
    }
    thumbPulseRafId = 0;
  }

  function startThumbnailHoverLoop() {
    if (thumbPulseRafId || typeof requestAnimationFrame !== "function") return;
    function step(ts) {
      const modal = document.getElementById("prePrintModal");
      if (hoveredSlicerTool === null || !thumbPixelCache || !(modal && modal.classList.contains("open"))) {
        thumbPulseRafId = 0;
        return;
      }
      drawThumbnailFrame(ts);
      thumbPulseRafId = requestAnimationFrame(step);
    }
    thumbPulseRafId = requestAnimationFrame(step);
  }

  function setHoveredExtruder(slicerTool) {
    const nextTool = (slicerTool === undefined || slicerTool === null) ? null : Number(slicerTool);
    if (hoveredSlicerTool === nextTool) return;
    hoveredSlicerTool = nextTool;
    if (thumbPixelCache) {
      drawThumbnailFrame(typeof performance !== "undefined" ? performance.now() : 0);
      if (hoveredSlicerTool !== null) {
        startThumbnailHoverLoop();
      } else {
        stopThumbnailHoverLoop();
      }
    }
  }

  function clearHoveredExtruder(slicerTool) {
    if (
      slicerTool !== undefined
      && slicerTool !== null
      && hoveredSlicerTool !== Number(slicerTool)
    ) {
      return;
    }
    hoveredSlicerTool = null;
    stopThumbnailHoverLoop();
    if (thumbPixelCache) {
      drawThumbnailFrame(0);
    }
  }

  function toggleThumbnailColorMode(event) {
    if (event) event.stopPropagation();
    thumbShowOriginal = !thumbShowOriginal;
    renderThumbnailModeButton(!!thumbPixelCache);
    updateThumbnailMappedColors();
  }

  function updateThumbnailMappedColors() {
    const thumbImg = document.getElementById("modalThumbImg");
    const thumbCanvas = document.getElementById("modalThumbCanvas");
    const thumbFallback = document.getElementById("modalThumbFallback");
    if (!thumbImg || !thumbFallback) return;

    const imgSrc = thumbImg.getAttribute("src") || "";
    if (!imgSrc || !imgSrc.startsWith("data:image/png;base64,")) {
      stopThumbnailHoverLoop();
      thumbPixelCache = null;
      if (thumbCanvas) thumbCanvas.style.display = "none";
      thumbImg.style.display = "none";
      thumbFallback.style.display = "inline-flex";
      renderThumbnailModeButton(false);
      return;
    }

    if (!thumbImg.complete || thumbImg.naturalWidth <= 0) {
      return;
    }

    const ctx = thumbCanvas && thumbCanvas.getContext ? thumbCanvas.getContext("2d") : null;
    if (!ctx) {
      thumbImg.style.display = "block";
      if (thumbCanvas) thumbCanvas.style.display = "none";
      thumbFallback.style.display = "none";
      renderThumbnailModeButton(false);
      return;
    }

    const palette = getActiveSlicerPalette();
    const paletteKey = palette.map(e => `${e.slicer_tool}:${e.hex}`).join("|");
    const cacheKey = `${imgSrc}::${paletteKey}`;
    if (!thumbPixelCache || thumbPixelCache.key !== cacheKey) {
      thumbPixelCache = buildThumbnailPixelCache(thumbImg, palette, cacheKey);
    }

    if (!thumbPixelCache) {
      thumbImg.style.display = "block";
      thumbCanvas.style.display = "none";
      thumbFallback.style.display = "none";
      renderThumbnailModeButton(false);
      return;
    }

    if (thumbCanvas.width !== thumbPixelCache.width || thumbCanvas.height !== thumbPixelCache.height) {
      thumbCanvas.width = thumbPixelCache.width;
      thumbCanvas.height = thumbPixelCache.height;
    }
    thumbImg.style.display = "none";
    thumbCanvas.style.display = "block";
    thumbFallback.style.display = "none";
    renderThumbnailModeButton(true);
    drawThumbnailFrame(typeof performance !== "undefined" ? performance.now() : 0);
  }

  function renderModalStaleWarning() {
    const banner = document.getElementById("modalStaleModelBanner");
    const textEl = document.getElementById("modalStaleModelText");
    if (!banner || !textEl) return;
    const stale = currentState.stale_model_info;
    const hasPath = !!String(currentState.last_gcode_path || "").trim();
    if (stale && stale.is_stale && hasPath && stale.active_model) {
      const meta = (currentState.last_postprocess_info && currentState.last_postprocess_info.job_metadata)
        || currentState.last_job_metadata || {};
      const loadedFile = stale.loaded_file
        || meta.filename
        || String(currentState.last_gcode_path || "").split(/[\\\\/]/).pop()
        || "";
      textEl.textContent = t("ui.modal.stale.banner", {
        file: loadedFile,
        model: stale.active_model
      });
      banner.classList.add("visible");
    } else {
      textEl.textContent = "";
      banner.classList.remove("visible");
    }
  }

  function clearModalGcode() {
    currentState.last_gcode_path = "";
    currentState.last_gcode_source = "";
    currentState.last_job_metadata = {};
    currentState.last_postprocess_info = {};
    currentState.mapping_warnings = [];
    currentState.material_mismatch_confirmed = false;
    currentState.stale_model_info = { is_stale: false, active_model: "", loaded_file: "", reason: "" };
    const gcodeInput = document.getElementById("gcodePathInput");
    if (gcodeInput) gcodeInput.value = "";
    const gateChk = document.getElementById("chkConfirmMaterialMismatch");
    if (gateChk) gateChk.checked = false;
    stopThumbnailHoverLoop();
    thumbPixelCache = null;
    closeThumbZoomModal();
    renderExecutiveStrip();
    renderModalJobSummary();
    renderMappingTable();
    renderFlushDashboard();
    sendToHost({ type: "clear_gcode" });
  }

  function renderModalJobSummary() {
    const info = currentState.last_postprocess_info || {};
    const meta = info.job_metadata || currentState.last_job_metadata || {};
    const path = currentState.last_gcode_path || "";
    const base = meta.filename || (path ? path.split(/[\\\\/]/).pop() : "");

    const thumbBox = document.getElementById("modalThumbBox");
    const thumbImg = document.getElementById("modalThumbImg");
    const thumbCanvas = document.getElementById("modalThumbCanvas");
    const thumbFallback = document.getElementById("modalThumbFallback");
    const thumbDataUrl = typeof meta.thumbnail_data_url === "string" ? meta.thumbnail_data_url : "";
    if (thumbImg && thumbFallback) {
      if (thumbDataUrl && thumbDataUrl.startsWith("data:image/png;base64,")) {
        thumbImg.alt = t("ui.modal.thumb.alt");
        thumbImg.onload = () => updateThumbnailMappedColors();
        thumbImg.onerror = () => {
          stopThumbnailHoverLoop();
          thumbPixelCache = null;
          if (thumbCanvas) thumbCanvas.style.display = "none";
          thumbImg.style.display = "none";
          thumbFallback.style.display = "inline-flex";
          renderThumbnailModeButton(false);
        };
        if (thumbImg.getAttribute("src") !== thumbDataUrl) {
          thumbPixelCache = null;
          thumbImg.src = thumbDataUrl;
        }
        thumbImg.style.display = "none";
        thumbFallback.style.display = "none";
        if (thumbBox) thumbBox.title = base || t("ui.modal.thumb.alt");
        updateThumbnailMappedColors();
      } else {
        stopThumbnailHoverLoop();
        thumbPixelCache = null;
        thumbImg.onload = null;
        thumbImg.removeAttribute("src");
        thumbImg.alt = "";
        thumbImg.style.display = "none";
        if (thumbCanvas) thumbCanvas.style.display = "none";
        thumbFallback.style.display = "inline-flex";
        renderThumbnailModeButton(false);
        if (thumbBox) thumbBox.title = t("ui.modal.thumb.fallback_title");
      }
    }

    const fileEl = document.getElementById("modalJobFile");
    if (fileEl) {
      fileEl.textContent = base || t("ui.modal.job.no_file");
      fileEl.title = base || "";
    }
    const zoomFileEl = document.getElementById("zoomThumbFilename");
    if (zoomFileEl) {
      zoomFileEl.textContent = base || t("ui.modal.job.no_file");
    }

    const tlEl = document.getElementById("modalJobTimeLayers");
    if (tlEl) {
      const timeStr = meta.estimated_time
        ? t("ui.modal.job.time", { time: meta.estimated_time })
        : "—";
      const lyrStr = meta.total_layers > 0
        ? " · " + t("ui.modal.job.layers", { layers: meta.total_layers })
        : "";
      tlEl.textContent = timeStr + lyrStr;
    }

    const wgEl = document.getElementById("modalJobWeight");
    if (wgEl) {
      const wg = Number(meta.total_weight_g || 0).toFixed(1);
      const lm = Number(meta.total_length_m || 0).toFixed(2);
      wgEl.textContent = t("ui.modal.job.weight", { weight: wg, length: lm });
    }

    const pgEl = document.getElementById("modalJobPurge");
    if (pgEl) {
      const tc = Number(info.toolchanges || 0);
      const plen = Number(info.total_purge_length_mm || 0).toFixed(1);
      pgEl.textContent = t("ui.modal.job.toolchanges", { tc: tc, purge: plen });
    }
    const clearBtn = document.getElementById("btnClearModalGcode");
    if (clearBtn) {
      clearBtn.style.display = path ? "inline-flex" : "none";
    }
    renderModalStaleWarning();
  }

  function renderSlots() {
    const container = document.getElementById("slotsContainer");
    if (!container) return;
    container.innerHTML = "";
    const rawSlots = currentState.ace_slots || [];
    const slots = rawSlots.length >= 8
      ? rawSlots
      : [0, 1, 2, 3, 4, 5, 6, 7].map(i => {
          const found = rawSlots.find(s => Number(s.tool) === i);
          return found || {
            tool: i,
            label: `T${i}`,
            status: "empty",
            hex_color: "#000000",
            material: "",
            temp: 0,
            rfid: false
          };
        });
    const isPrinting = !!(currentState.ace_status && currentState.ace_status.is_printing);
    const disAttr = isPrinting ? "disabled" : "";

    slots.forEach(slot => {
      const rawStatus = String(slot.status || "empty").toLowerCase();
      const isEmpty = rawStatus === "empty";
      const isLoaded = !!(slot.is_loaded || rawStatus === "loaded");
      const matRaw = String(slot.material || "").trim();
      const isUnconfigured = !isEmpty && !isLoaded && !matRaw;

      const cardClass = isLoaded
        ? "slot-loaded"
        : (isEmpty ? "slot-empty" : (isUnconfigured ? "slot-unconfigured" : ""));
      const badgeClass = isLoaded ? "loaded" : (rawStatus === "ready" ? "ready" : "empty");
      const badgeLabel = isLoaded
        ? t("ui.slot.badge_active")
        : (rawStatus === "ready" ? t("ui.slot.badge_ready") : (isEmpty ? t("ui.slot.badge_empty") : rawStatus));

      const hexCol = safeHexColor(slot.hex_color, "#64748B");
      const matText = matRaw || (isEmpty ? t("ui.slot.no_filament") : t("ui.slot.unconfigured"));
      const tempText = slot.temp ? `${Number(slot.temp)}°C` : "—";
      const brandSku = [slot.brand, slot.sku].filter(Boolean).join(" · ");
      const rfidBadge = slot.rfid
        ? `<span class="badge info" title="${escapeHtml(brandSku || t('ui.slot.rfid_detected'))}">RFID</span>`
        : "";
      const slotNum = Number(slot.tool);

      const card = document.createElement("div");
      card.className = `slot-card ${cardClass}`.trim();
      card.innerHTML = `
        <div class="slot-header">
          <span class="slot-id">${escapeHtml(slot.label || ('T' + slotNum))} ${rfidBadge}</span>
          <span class="badge ${badgeClass}">${escapeHtml(badgeLabel)}</span>
        </div>
        <div class="swatch-row">
          <div class="spool-ring" style="border-color:${isEmpty ? 'var(--muted)' : hexCol};"></div>
          <div style="min-width:0;">
            <div style="font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(matText)}</div>
            <div class="slot-meta">${escapeHtml(hexCol)} · ${escapeHtml(tempText)}</div>
            ${brandSku ? `<div class="slot-meta" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(brandSku)}</div>` : ""}
          </div>
        </div>
        <div class="slot-actions">
          <button ${disAttr} onclick="slotAction('load', ${slotNum})" title="${escapeHtml(t('ui.slot.title_load', { tool: slotNum }))}">${escapeHtml(t('ui.slot.btn_load'))}</button>
          <button ${disAttr} onclick="slotAction('unload', ${slotNum})" title="${escapeHtml(t('ui.slot.title_unload', { tool: slotNum }))}">${escapeHtml(t('ui.slot.btn_unload'))}</button>
          <button ${disAttr} onclick="slotAction('feed', ${slotNum})" title="ACE_FEED">${escapeHtml(t('ui.slot.btn_feed'))}</button>
          <button ${disAttr} onclick="slotAction('retract', ${slotNum})" title="ACE_RETRACT">${escapeHtml(t('ui.slot.btn_retr'))}</button>
        </div>
      `;
      container.appendChild(card);
    });
  }

  function computeContrastTextColor(hexColor) {
    const norm = safeHexColor(hexColor, "#475569").slice(1);
    const r = parseInt(norm.slice(0, 2), 16);
    const g = parseInt(norm.slice(2, 4), 16);
    const b = parseInt(norm.slice(4, 6), 16);
    const luminance = 0.299 * r + 0.587 * g + 0.114 * b;
    return luminance > 150 ? "#111827" : "#FFFFFF";
  }

  function formatSlotDropdownLabel(tIdx, slotInfo) {
    if (!slotInfo) return t("ui.map.not_present", { slot: tIdx });
    const rawStatus = String(slotInfo.status || "").toLowerCase();
    if (rawStatus === "empty") return `T${tIdx} · ${t("ui.modal.slot.opt_empty")}`;
    const matRaw = String(slotInfo.material || "").trim();
    return matRaw ? `T${tIdx} · ${matRaw}` : `T${tIdx} · ${t("ui.modal.slot.opt_unconf")}`;
  }

  function getSlotDropdownColors(slotInfo) {
    const isEmpty = !slotInfo || String(slotInfo.status || "").toLowerCase() === "empty";
    const bgHex = isEmpty ? "#475569" : safeHexColor(slotInfo.hex_color, "#475569");
    const fgHex = computeContrastTextColor(bgHex);
    return { bgHex, fgHex, isEmpty };
  }

  function closeAllSlotDropdowns(exceptEl) {
    document.querySelectorAll(".slot-color-dropdown.open, .custom-select.open").forEach(dd => {
      if (dd !== exceptEl) {
        dd.classList.remove("open");
        dd.classList.remove("drop-up");
        const trig = dd.querySelector("[aria-expanded]");
        if (trig) trig.setAttribute("aria-expanded", "false");
      }
    });
  }

  const DRYER_UNIT_I18N_MAP = {
    "all": "ui.sec2.unit_all",
    "0": "ui.sec2.unit_ace1",
    "1": "ui.sec2.unit_ace2"
  };

  function setDryerUnitValue(unitVal) {
    const raw = String(unitVal === undefined || unitVal === null ? "all" : unitVal).trim().toLowerCase();
    const norm = ["all", "0", "1"].includes(raw) ? raw : "all";
    const dd = document.getElementById("dryerUnit");
    const lbl = document.getElementById("dryerUnitLabel");
    if (dd) {
      dd.dataset.value = norm;
      dd.value = norm;
      dd.querySelectorAll(".custom-select-option").forEach(opt => {
        const isSel = String(opt.getAttribute("data-value") || "") === norm;
        opt.classList.toggle("is-selected", isSel);
        opt.setAttribute("aria-selected", isSel ? "true" : "false");
      });
    }
    if (lbl) {
      const i18nKey = DRYER_UNIT_I18N_MAP[norm] || "ui.sec2.unit_all";
      lbl.setAttribute("data-i18n", i18nKey);
      lbl.textContent = t(i18nKey);
    }
    return norm;
  }

  function toggleDryerUnitDropdown(event) {
    if (event) event.stopPropagation();
    const dd = document.getElementById("dryerUnit");
    if (!dd) return;
    const willOpen = !dd.classList.contains("open");
    closeAllSlotDropdowns(dd);
    const trig = document.getElementById("dryerUnitTrigger");
    if (!willOpen) {
      dd.classList.remove("open");
      dd.classList.remove("drop-up");
      if (trig) trig.setAttribute("aria-expanded", "false");
      return;
    }
    dd.classList.add("open");
    dd.classList.remove("drop-up");
    if (trig) trig.setAttribute("aria-expanded", "true");
    const menu = document.getElementById("dryerUnitMenu");
    if (menu) {
      const mRect = menu.getBoundingClientRect();
      if (mRect.bottom > (window.innerHeight || document.documentElement.clientHeight) - 8) {
        dd.classList.add("drop-up");
      }
    }
  }

  function selectDryerUnit(unitVal, event) {
    if (event) event.stopPropagation();
    setDryerUnitValue(unitVal);
    closeAllSlotDropdowns();
    onDryerSettingsChange();
  }

  function toggleSlotDropdown(triggerBtn, event) {
    if (event) event.stopPropagation();
    const dd = triggerBtn ? triggerBtn.closest(".slot-color-dropdown") : null;
    if (!dd) return;
    const willOpen = !dd.classList.contains("open");
    closeAllSlotDropdowns(dd);
    if (!willOpen) {
      dd.classList.remove("open");
      dd.classList.remove("drop-up");
      return;
    }
    dd.classList.add("open");
    dd.classList.remove("drop-up");
    const menu = dd.querySelector(".slot-color-menu");
    const modalBody = dd.closest(".modal-body");
    if (menu && modalBody) {
      const mRect = menu.getBoundingClientRect();
      const bRect = modalBody.getBoundingClientRect();
      if (mRect.bottom > bRect.bottom - 8) {
        dd.classList.add("drop-up");
      }
    }
  }

  function selectSlotFromDropdown(slicerTool, physicalSlot, event) {
    if (event) event.stopPropagation();
    closeAllSlotDropdowns();
    updateToolMapping(slicerTool, physicalSlot);
  }

  function updateModalSendActionUi() {
    const btnApply = document.getElementById("btnModalApplyLocal");
    const btnUpload = document.getElementById("btnModalUploadOnly");
    const btnSend = document.getElementById("btnModalSendPrint");
    const isPrinting = !!(currentState.ace_status && currentState.ace_status.is_printing);
    const blockedByMismatch = !!currentState._modalBlockedByMismatch;
    const blockedByErrors = !!currentState._modalBlockedByErrors;

    if (btnApply) {
      btnApply.disabled = blockedByMismatch || blockedByErrors;
    }
    if (btnUpload) {
      btnUpload.disabled = blockedByMismatch || blockedByErrors;
    }
    if (btnSend) {
      btnSend.disabled = blockedByMismatch || blockedByErrors || isPrinting;
    }
  }

  function isFilamentUsed(fil, hasAnyUsageData) {
    if (!fil || fil.used === false) return false;
    if (
      hasAnyUsageData
      && fil.used_g !== undefined
      && Number(fil.used_g || 0) <= 0
      && Number(fil.used_mm || 0) <= 0
      && Number(fil.used_m || 0) <= 0
      && Number(fil.used_cm3 || 0) <= 0
    ) {
      return false;
    }
    return true;
  }

  function renderMappingTable() {
    const cardsContainer = document.getElementById("modalMappingCards");
    const summaryBar = document.getElementById("mappingSummaryBar");
    if (cardsContainer) {
      cardsContainer.innerHTML = "";
      if (!cardsContainer.dataset.thumbHoverBound) {
        cardsContainer.dataset.thumbHoverBound = "1";
        cardsContainer.addEventListener("mouseleave", () => clearHoveredExtruder());
      }
    }

    const slots = currentState.ace_slots || [];
    const maxSlot = Math.max(8, slots.length);
    const filaments = currentState.slicer_filaments || [];
    const sources = currentState.tool_map_sources || {};
    const showUnusedEl = document.getElementById("chkShowUnusedExtruders");
    const showUnused = !!(showUnusedEl && showUnusedEl.checked);
    const hasAnyUsageData = filaments.some(f => (
      Number(f.used_g || 0) > 0
      || Number(f.used_mm || 0) > 0
      || Number(f.used_m || 0) > 0
      || Number(f.used_cm3 || 0) > 0
    ));

    const mappedCounts = {};
    filaments.forEach(fil => {
      if (isFilamentUsed(fil, hasAnyUsageData)) {
        const m = (currentState.tool_map && currentState.tool_map[fil.slicer_tool] !== undefined)
          ? Number(currentState.tool_map[fil.slicer_tool])
          : Number(fil.slicer_tool);
        mappedCounts[m] = (mappedCounts[m] || 0) + 1;
      }
    });

    let errCount = 0;
    let warnCount = 0;
    const mismatchDescriptions = [];
    const activeFils = filaments.filter(f => isFilamentUsed(f, hasAnyUsageData));

    filaments.forEach(fil => {
      const sTool = Number(fil.slicer_tool);
      const mapped = (currentState.tool_map && currentState.tool_map[sTool] !== undefined)
        ? Number(currentState.tool_map[sTool])
        : sTool;
      const srcMode = String(sources[sTool] || sources[String(sTool)] || "default").toLowerCase();
      const srcLabel = srcMode === "auto"
        ? t("ui.map.mode_auto")
        : (srcMode === "manual" ? t("ui.map.mode_manual") : t("ui.map.mode_default"));
      const srcClass = srcMode === "auto" ? "loaded" : (srcMode === "manual" ? "info" : "empty");

      const assignedSlot = slots.find(s => Number(s.tool) === mapped);
      const filUsed = isFilamentUsed(fil, hasAnyUsageData);
      let cardStatusHtml = "";
      let cardStateClass = "";

      if (!filUsed) {
        cardStatusHtml = `<span class="badge empty">${escapeHtml(t("ui.map.unused_badge"))}</span>`;
      } else if (mapped < 0 || (slots.length > 0 && mapped >= maxSlot)) {
        cardStatusHtml = `<span class="badge danger">${escapeHtml(t("ui.map.badge_invalid"))}</span>`;
        errCount++;
        cardStateClass = "card-error";
      } else if (assignedSlot) {
        const sMat = String(fil.material || "").trim().toUpperCase();
        const pMat = String(assignedSlot.material || "").trim().toUpperCase();
        const isEmpty = String(assignedSlot.status || "").toLowerCase() === "empty";
        const isShared = (mappedCounts[mapped] || 0) > 1;
        const isMatCompatible = (!sMat || !pMat || sMat === pMat || sMat.startsWith(pMat) || pMat.startsWith(sMat));

        const statusBadges = [];
        if (isEmpty) {
          errCount++;
          cardStateClass = "card-error";
          statusBadges.push(`<span class="badge danger">${escapeHtml(t("ui.modal.card.empty_badge", { slot: mapped }))}</span>`);
        } else {
          if (!isMatCompatible) {
            warnCount++;
            cardStateClass = "card-mismatch";
            const mismatchBadgeText = t("ui.modal.card.mismatch_badge", { slicer_mat: sMat, ace_mat: pMat });
            statusBadges.push(`<span class="badge warn">${escapeHtml(mismatchBadgeText)}</span>`);
            mismatchDescriptions.push(`T${sTool} (${sMat}) → T${mapped} (${pMat})`);
          } else {
            statusBadges.push(`<span class="badge ok">${escapeHtml(t("ui.modal.card.match_exact", { mat: pMat || sMat || "OK" }))}</span>`);
          }
          if (isShared) {
            statusBadges.push(`<span class="badge info">${escapeHtml(t("ui.modal.card.shared_badge", { slot: mapped }))}</span>`);
          }
        }
        cardStatusHtml = `<div class="row" style="gap:4px;margin:0;justify-content:flex-end;">${statusBadges.join("")}</div>`;
      }

      const filHex = safeHexColor(fil.color, "#64748B");

      if (cardsContainer && (showUnused || filUsed)) {
        const usedG = Number(fil.used_g || 0).toFixed(1);
        const usedM = Number(fil.used_m || 0).toFixed(2);
        const amountStr = filUsed
          ? t("ui.modal.card.amount", { grams: usedG, meters: usedM })
          : t("ui.modal.card.unused_amount");

        const trigColors = getSlotDropdownColors(assignedSlot);
        const trigLabel = formatSlotDropdownLabel(mapped, assignedSlot);
        let menuItemsHtml = "";
        for (let tIdx = 0; tIdx < maxSlot; tIdx++) {
          const slotInfo = slots.find(s => Number(s.tool) === tIdx);
          const optColors = getSlotDropdownColors(slotInfo);
          const optLabel = formatSlotDropdownLabel(tIdx, slotInfo);
          const isSel = mapped === tIdx;
          const borderStyle = optColors.isEmpty ? "border-style:dashed;" : "";
          menuItemsHtml += `
            <button type="button" role="option" aria-selected="${isSel ? "true" : "false"}"
              class="slot-color-option${isSel ? " is-selected" : ""}"
              style="background-color:${optColors.bgHex};color:${optColors.fgHex};${borderStyle}"
              onclick="selectSlotFromDropdown(${sTool}, ${tIdx}, event)">
              <span>${escapeHtml(optLabel)}</span>
              <span>${isSel ? "✓" : ""}</span>
            </button>
          `;
        }
        if (mapped < 0 || mapped >= maxSlot) {
          menuItemsHtml += `
            <button type="button" role="option" aria-selected="true"
              class="slot-color-option is-selected"
              style="background-color:#475569;color:#FFFFFF;"
              onclick="selectSlotFromDropdown(${sTool}, ${mapped}, event)">
              <span>${escapeHtml(t("ui.map.not_present", { slot: mapped }))}</span>
              <span>✓</span>
            </button>
          `;
        }

        const card = document.createElement("div");
        card.className = `fil-map-card ${cardStateClass}`.trim();
        if (!filUsed) card.style.opacity = "0.62";
        card.innerHTML = `
          <div class="fil-map-row">
            <div class="fil-source-col">
              <div class="fil-swatch-lg" style="background:${filHex};"></div>
              <div class="fil-card-meta">
                <div class="fil-card-title">
                  <span>${escapeHtml(t("ui.modal.card.slicer_ext", { tool: sTool }))}</span>
                  <span class="badge empty">${escapeHtml(fil.material || "—")}</span>
                </div>
                <div class="fil-card-sub">${escapeHtml(amountStr)} · ${escapeHtml(filHex)}</div>
              </div>
            </div>
            <div class="fil-arrow-col">→</div>
            <div class="fil-target-col">
              <div class="slot-color-dropdown" data-slicer-tool="${sTool}" data-selected-slot="${mapped}">
                <button type="button"
                  class="slot-color-trigger${trigColors.isEmpty ? " is-empty" : ""}"
                  style="background-color:${trigColors.bgHex};color:${trigColors.fgHex};"
                  aria-label="${escapeHtml(t("ui.modal.btn.slot_select_aria", { tool: sTool }))}"
                  onclick="toggleSlotDropdown(this, event)">
                  <span class="slot-color-trigger-label">${escapeHtml(trigLabel)}</span>
                  <span class="slot-color-caret">▾</span>
                </button>
                <div class="slot-color-menu" role="listbox">${menuItemsHtml}</div>
              </div>
            </div>
          </div>
          <div class="fil-card-footer">
            <span class="badge ${srcClass}">${escapeHtml(srcLabel)}</span>
            <div>${cardStatusHtml}</div>
          </div>
        `;
        card.setAttribute("data-slicer-tool", String(sTool));
        card.addEventListener("mouseenter", () => setHoveredExtruder(sTool));
        card.addEventListener("mouseleave", () => clearHoveredExtruder(sTool));
        cardsContainer.appendChild(card);
      }
    });

    if (summaryBar) {
      if (errCount > 0) {
        summaryBar.innerHTML = `<span class="badge danger">${escapeHtml(t("ui.map.sum_err_badge", { count: errCount }))}</span> <span>${escapeHtml(t("ui.map.sum_err_text"))}</span>`;
      } else if (warnCount > 0) {
        summaryBar.innerHTML = `<span class="badge warn">${escapeHtml(t("ui.map.sum_warn_badge", { count: warnCount }))}</span> <span>${escapeHtml(t("ui.map.sum_warn_text"))}</span>`;
      } else if (activeFils.length === 0) {
        summaryBar.innerHTML = `<span class="badge warn">0</span> <span>${escapeHtml(t("ui.map.sum_empty_active"))}</span>`;
      } else {
        summaryBar.innerHTML = `<span class="badge ok">${escapeHtml(t("ui.map.sum_ok_badge"))}</span> <span>${escapeHtml(t("ui.map.sum_ok_text", { count: activeFils.length }))}</span>`;
      }
    }

    // Material Mismatch Confirmation Gate
    const hasMismatch = mismatchDescriptions.length > 0;
    const gateBox = document.getElementById("modalMismatchGate");
    const gateList = document.getElementById("modalMismatchList");
    const gateChk = document.getElementById("chkConfirmMaterialMismatch");

    if (gateBox) {
      if (hasMismatch) {
        gateBox.classList.add("visible");
        if (gateList) gateList.textContent = mismatchDescriptions.join(" · ");
        if (gateChk) gateChk.checked = !!currentState.material_mismatch_confirmed;
      } else {
        gateBox.classList.remove("visible");
      }
    }

    currentState._modalBlockedByMismatch = hasMismatch && !(gateChk && gateChk.checked);
    currentState._modalBlockedByErrors = errCount > 0;
    updateModalSendActionUi();
    updateThumbnailMappedColors();
  }

  function updateToolMapping(slicerTool, physicalTool) {
    currentState.tool_map[slicerTool] = parseInt(physicalTool, 10);
    if (!currentState.tool_map_sources) currentState.tool_map_sources = {};
    currentState.tool_map_sources[slicerTool] = "manual";
    currentState.material_mismatch_confirmed = false;
    const gateChk = document.getElementById("chkConfirmMaterialMismatch");
    if (gateChk) gateChk.checked = false;
    renderMappingTable();
    sendToHost({ type: "update_mapping", settings: collectSettingsFromUI() });
  }

  function onMismatchConfirmChange(checked) {
    currentState.material_mismatch_confirmed = !!checked;
    renderMappingTable();
    syncSettings();
  }

  function resetColorMapping() {
    currentState.material_mismatch_confirmed = false;
    const gateChk = document.getElementById("chkConfirmMaterialMismatch");
    if (gateChk) gateChk.checked = false;
    sendToHost({ type: "reset_mapping" });
  }

  function readDryerControls() {
    const unitEl = document.getElementById("dryerUnit");
    const tempEl = document.getElementById("dryerTemp");
    const durEl = document.getElementById("dryerDuration");
    const rawUnit = unitEl
      ? String((unitEl.dataset && unitEl.dataset.value) ? unitEl.dataset.value : (unitEl.value || "all")).trim().toLowerCase()
      : String(currentState.dryer_unit || "all").trim().toLowerCase();
    const unit = ["all", "0", "1"].includes(rawUnit) ? rawUnit : "all";
    const parsedTemp = tempEl ? parseInt(tempEl.value, 10) : 50;
    const rawTemp = Number.isFinite(parsedTemp) ? parsedTemp : 50;
    const clampedTemp = Math.min(70, Math.max(20, rawTemp));
    const parsedDur = durEl ? parseInt(durEl.value, 10) : 240;
    const rawDur = Number.isFinite(parsedDur) ? parsedDur : 240;
    const clampedDur = Math.min(1440, Math.max(1, rawDur));
    return {
      unit: unit,
      temp: clampedTemp,
      raw_temp: rawTemp,
      temp_clamped: rawTemp !== clampedTemp,
      duration: clampedDur
    };
  }

  function collectSettingsFromUI() {
    const gateChk = document.getElementById("chkConfirmMaterialMismatch");
    const dryer = readDryerControls();
    return {
      moonraker_url: document.getElementById("moonrakerUrl").value.trim(),
      api_key: document.getElementById("apiKey").value.trim(),
      enable_port80_fallback: document.getElementById("port80Fallback").checked,
      bed_leveling: document.getElementById("chkBedLeveling").checked,
      skip_purge_for_already_loaded_tool: document.getElementById("chkSkipPurgeLoaded").checked,
      cut_tip_after_print: document.getElementById("chkCutTipPost").checked,
      force_initial_purge: document.getElementById("chkForceInitialPurge").checked,
      material_mismatch_confirmed: gateChk ? !!gateChk.checked : !!currentState.material_mismatch_confirmed,
      last_gcode_path: document.getElementById("gcodePathInput").value.trim(),
      dryer_unit: dryer.unit,
      dryer_temp: dryer.temp,
      dryer_duration: dryer.duration,
      tool_map: currentState.tool_map,
      tool_map_sources: currentState.tool_map_sources || {}
    };
  }

  function syncSettings() {
    sendToHost({ type: "save_settings", settings: collectSettingsFromUI() });
  }

  function saveSettingsAndRefresh() {
    syncSettings();
    requestStatus();
  }

  function requestStatus() {
    sendToHost({ type: "get_status", settings: collectSettingsFromUI() });
  }

  function triggerMacro(macroName) {
    sendToHost({ type: "trigger_macro", macro: macroName });
  }

  function unloadActiveTool() {
    sendToHost({ type: "slot_action", action: "unload_active", tool: -1 });
  }

  function onDryerSettingsChange() {
    const d = readDryerControls();
    currentState.dryer_unit = d.unit;
    currentState.dryer_temp = d.temp;
    currentState.dryer_duration = d.duration;
    if (d.temp_clamped) {
      logMsg(t("host.dryer_invalid_temp", { temp: d.raw_temp }));
    }
    syncSettings();
  }

  function startDryer() {
    const d = readDryerControls();
    currentState.dryer_unit = d.unit;
    currentState.dryer_temp = d.temp;
    currentState.dryer_duration = d.duration;
    const tempEl = document.getElementById("dryerTemp");
    const durEl = document.getElementById("dryerDuration");
    if (tempEl && String(tempEl.value).trim() !== String(d.temp)) tempEl.value = String(d.temp);
    if (durEl && String(durEl.value).trim() !== String(d.duration)) durEl.value = String(d.duration);
    if (d.temp_clamped) {
      logMsg(t("host.dryer_invalid_temp", { temp: d.raw_temp }));
    }
    sendToHost({
      type: "dryer_start",
      unit: d.unit,
      temp: d.temp,
      duration: d.duration,
      settings: collectSettingsFromUI()
    });
  }

  function stopDryer() {
    const d = readDryerControls();
    currentState.dryer_unit = d.unit;
    sendToHost({
      type: "dryer_stop",
      unit: d.unit,
      settings: collectSettingsFromUI()
    });
  }

  function slotAction(action, tool) {
    const len = parseInt(document.getElementById("feedLength").value, 10) || 50;
    sendToHost({ type: "slot_action", action: action, tool: tool, length: len });
  }

  function autoMatchColors() {
    currentState.material_mismatch_confirmed = false;
    const gateChk = document.getElementById("chkConfirmMaterialMismatch");
    if (gateChk) gateChk.checked = false;
    sendToHost({ type: "auto_match" });
  }

  function syncOrcaFilaments() {
    sendToHost({ type: "sync_orca_filaments" });
  }

  function parseGcodeHeader() {
    const path = document.getElementById("gcodePathInput").value.trim();
    sendToHost({ type: "parse_gcode", gcode_path: path });
  }

  function previewGcodeFlush() {
    const settings = collectSettingsFromUI();
    sendToHost({
      type: "preview_gcode",
      gcode_path: settings.last_gcode_path,
      settings: settings
    });
  }

  function runPostProcessAndUpload(upload, startPrint) {
    const settings = collectSettingsFromUI();
    const gcodePath = String(settings.last_gcode_path || currentState.last_gcode_path || "").trim();
    if (!gcodePath) {
      const msgText = t("ui.modal.feedback.no_gcode_action");
      showModalFeedback("warn", msgText);
      logMsg(msgText);
      return;
    }
    const shouldStartPrint = Boolean(upload && startPrint);
    sendToHost({
      type: upload ? "postprocess_and_upload" : "postprocess_only",
      gcode_path: gcodePath,
      require_mismatch_gate: true,
      mismatch_confirmed: settings.material_mismatch_confirmed,
      start_print: shouldStartPrint,
      settings: settings
    });
  }

  function calcPurgeLenMm(volMm3, diamMm) {
    const d = Number(diamMm) > 0 ? Number(diamMm) : 1.75;
    const area = Math.PI * (d * 0.5) * (d * 0.5);
    return area > 0 ? (Number(volMm3) / area) : 0;
  }

  function renderFlushDashboard() {
    const info = currentState.last_postprocess_info || {};
    const statusBadge = document.getElementById("flushStatusBadge");
    const modeBadge = document.getElementById("flushModeBadge");
    const vecLayoutBadge = document.getElementById("vectorLayoutBadge");
    const eventsCountBadge = document.getElementById("eventsCountBadge");
    const kpiGrid = document.getElementById("flushKpiGrid");
    const warnBox = document.getElementById("flushAuditWarnings");
    const vecBody = document.getElementById("flushVectorBody");
    const matBox = document.getElementById("flushMatrixContainer");
    const evBody = document.getElementById("flushEventsBody");
    if (!statusBadge || !kpiGrid || !vecBody || !matBox || !evBody) return;

    const fStatus = info.flush_status || "";
    if (!fStatus && info.toolchanges === undefined) {
      statusBadge.textContent = t("ui.sec6.no_analysis");
      statusBadge.className = "badge empty";
      if (modeBadge) { modeBadge.textContent = "—"; modeBadge.className = "badge empty"; }
      if (vecLayoutBadge) { vecLayoutBadge.textContent = "—"; vecLayoutBadge.className = "badge empty"; }
      if (eventsCountBadge) { eventsCountBadge.textContent = t("ui.sec6.events_count", { count: 0 }); }
      kpiGrid.innerHTML = "";
      warnBox.innerHTML = "";
      vecBody.innerHTML = `<tr><td colspan="6" style="color:var(--muted);">${escapeHtml(t("ui.flush.empty_vec"))}</td></tr>`;
      matBox.innerHTML = `<div style="color:var(--muted); font-size:12px;">${escapeHtml(t("ui.flush.empty_mat"))}</div>`;
      evBody.innerHTML = "";
      return;
    }

    if (modeBadge) {
      if (info.analysis_mode === "preview") {
        modeBadge.textContent = t("ui.sec6.mode_preview");
        modeBadge.className = "badge info";
      } else {
        modeBadge.textContent = t("ui.sec6.mode_pp");
        modeBadge.className = "badge ok";
      }
    }

    if (fStatus === "POSTPROCESS_OK") {
      statusBadge.textContent = "POSTPROCESS_OK";
      statusBadge.className = "badge ok";
    } else if (fStatus === "FLUSH_DATA_INCOMPLETE") {
      statusBadge.textContent = "FLUSH_DATA_INCOMPLETE";
      statusBadge.className = "badge danger";
    } else {
      statusBadge.textContent = fStatus || "POSTPROCESS_WARNING";
      statusBadge.className = "badge warn";
    }

    const mult = Number(info.flush_multiplier !== undefined ? info.flush_multiplier : 1.0);
    const diam = Number(info.filament_diameter || 1.75);
    const totalVol = Number(info.total_purge_volume_mm3 || 0);
    const totalLen = Number(info.total_purge_length_mm || 0);
    const g9111Str = (info.g9111StartSlicerTool !== null && info.g9111StartSlicerTool !== undefined)
      ? `T${info.g9111StartSlicerTool} → T${info.g9111MappedTool}`
      : "—";
    const g31Str = info.bedLeveling
      ? t("ui.flush.g31_active")
      : t("ui.flush.g31_skipped", { count: info.g31_skipped || 0 });
    const cutStr = t("ui.flush.cut_fmt", {
      skip: (info.skipPurgeForAlreadyLoadedTool !== false) ? t("ui.flush.yes") : t("ui.flush.no"),
      cut: info.cutTipPost ? t("ui.flush.yes") : t("ui.flush.no"),
    });

    kpiGrid.innerHTML = `
      <div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_mul"))}</div><div class="kpi-val">${mult.toFixed(2)}</div></div>
      <div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_diam"))}</div><div class="kpi-val">${diam.toFixed(2)} mm</div></div>
      <div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_tc"))}</div><div class="kpi-val">${Number(info.toolchanges || 0)}</div></div>
      <div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_total"))}</div><div class="kpi-val">${totalVol.toFixed(1)} mm³ / ${totalLen.toFixed(1)} mm</div></div>
      <div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_g9111"))}</div><div class="kpi-val">${escapeHtml(g9111Str)}</div></div>
      <div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_g31_cut"))}</div><div class="kpi-val" style="font-size:12px;">${escapeHtml(g31Str)} · ${escapeHtml(cutStr)}</div></div>
    `;

    const allWarns = [];
    if (Array.isArray(info.warning_items) && info.warning_items.length > 0) {
      info.warning_items.forEach(item => {
        allWarns.push({
          level: fStatus === "FLUSH_DATA_INCOMPLETE" ? "error" : "warn",
          text: t(item.key, item.params || {}),
        });
      });
    } else {
      (info.warnings || []).forEach(w => allWarns.push({ level: fStatus === "FLUSH_DATA_INCOMPLETE" ? "error" : "warn", text: w }));
    }
    const mval = info.mapping_validation || {};
    if (Array.isArray(mval.error_items) && mval.error_items.length > 0) {
      mval.error_items.forEach(item => allWarns.push({ level: "error", text: t(item.key, item.params || {}) }));
    } else {
      (mval.errors || []).forEach(e => allWarns.push({ level: "error", text: e }));
    }
    if (Array.isArray(mval.warning_items) && mval.warning_items.length > 0) {
      mval.warning_items.forEach(item => allWarns.push({ level: "warn", text: t(item.key, item.params || {}) }));
    } else {
      (mval.warnings || []).forEach(w => allWarns.push({ level: "warn", text: w }));
    }
    warnBox.innerHTML = allWarns.map(item =>
      `<div class="warn-box ${item.level === "error" ? "error" : ""}">${escapeHtml(item.text)}</div>`
    ).join("");

    // Render Flush Vector via info.vector_summary (authoritative backend N vs 2*N decoding + remapped slot)
    vecBody.innerHTML = "";
    const vecSummary = Array.isArray(info.vector_summary) ? info.vector_summary : [];
    if (vecSummary.length > 0) {
      const layout = vecSummary[0].layout || "N";
      if (vecLayoutBadge) {
        vecLayoutBadge.textContent = layout === "2xN" ? "2×N (Unload / Load)" : "N (Load)";
        vecLayoutBadge.className = "badge info";
      }
      vecSummary.forEach(item => {
        const sIdx = Number(item.tool) || 0;
        const mappedT = item.mapped_tool !== undefined
          ? Number(item.mapped_tool)
          : (currentState.tool_map[sIdx] !== undefined ? Number(currentState.tool_map[sIdx]) : sIdx);
        const unloadTxt = (item.unload_mm3 === null || item.unload_mm3 === undefined)
          ? "—"
          : Number(item.unload_mm3).toFixed(1);
        vecBody.innerHTML += `
          <tr>
            <td>${filamentColorDot(sIdx)} <strong>${escapeHtml(item.label || ('T' + sIdx))}</strong></td>
            <td><span class="badge info">T${mappedT}</span></td>
            <td>${unloadTxt}</td>
            <td>${Number(item.load_mm3 || 0).toFixed(1)}</td>
            <td>${Number(item.effective_load_mm3 || 0).toFixed(1)}</td>
            <td><strong>${Number(item.effective_length_mm || 0).toFixed(2)} mm</strong></td>
          </tr>
        `;
      });
    } else {
      const vec = Array.isArray(info.vector) ? info.vector : [];
      const nTools = Number(info.tools || 0) || (Array.isArray(info.slicer_filaments) ? info.slicer_filaments.length : 0);
      if (vec.length === 0) {
        if (vecLayoutBadge) { vecLayoutBadge.textContent = "—"; vecLayoutBadge.className = "badge empty"; }
        vecBody.innerHTML = `<tr><td colspan="6" style="color:var(--muted);">${escapeHtml(t("ui.flush.no_vec"))}</td></tr>`;
      } else {
        const is2n = nTools > 0 && vec.length === 2 * nTools;
        const count = is2n ? nTools : vec.length;
        if (vecLayoutBadge) {
          vecLayoutBadge.textContent = is2n ? "2×N (Unload / Load)" : "N (Load)";
          vecLayoutBadge.className = "badge info";
        }
        for (let i = 0; i < count; i++) {
          const mappedT = currentState.tool_map[i] !== undefined ? Number(currentState.tool_map[i]) : i;
          const unloadV = is2n ? Number(vec[2 * i] || 0).toFixed(1) : "—";
          const loadV = is2n ? Number(vec[2 * i + 1] || 0) : Number(vec[i] || 0);
          const effV = loadV * mult;
          const pLen = calcPurgeLenMm(effV, diam);
          vecBody.innerHTML += `
            <tr>
              <td>${filamentColorDot(i)} <strong>T${i}</strong></td>
              <td><span class="badge info">T${mappedT}</span></td>
              <td>${unloadV}</td>
              <td>${loadV.toFixed(1)}</td>
              <td>${effV.toFixed(1)}</td>
              <td><strong>${pLen.toFixed(2)} mm</strong></td>
            </tr>
          `;
        }
      }
    }

    // Render N x N Matrix Heatmap (with remapped ACE slot header annotations)
    const mat = Array.isArray(info.matrix) ? info.matrix : [];
    const side = Math.round(Math.sqrt(mat.length));
    if (mat.length === 0 || side * side !== mat.length || side === 0) {
      matBox.innerHTML = `<div style="color:var(--muted); font-size:12px;">${escapeHtml(t("ui.flush.no_square_mat"))}</div>`;
    } else {
      const maxMat = Math.max(1, ...mat.map(v => Number(v) * mult));
      let mHtml = `<table class="matrix-table"><thead><tr><th>${escapeHtml(t("flush.matrix.corner"))}</th>`;
      for (let c = 0; c < side; c++) {
        const mappedC = currentState.tool_map[c] !== undefined ? Number(currentState.tool_map[c]) : c;
        mHtml += `<th>${filamentColorDot(c)} T${c}→T${mappedC}</th>`;
      }
      mHtml += '</tr></thead><tbody>';
      for (let r = 0; r < side; r++) {
        const mappedR = currentState.tool_map[r] !== undefined ? Number(currentState.tool_map[r]) : r;
        mHtml += `<tr><th>${filamentColorDot(r)} T${r}→T${mappedR}</th>`;
        for (let c = 0; c < side; c++) {
          if (r === c) {
            mHtml += '<td style="color:var(--muted);">—</td>';
            continue;
          }
          const val = Number(mat[r * side + c] || 0) * mult;
          const plen = calcPurgeLenMm(val, diam);
          const ratio = val / maxMat;
          const alpha = Math.min(0.42, ratio * 0.42);
          const bg = alpha > 0 ? `background:rgba(0,150,136,${alpha.toFixed(2)});` : '';
          mHtml += `<td style="${bg}">${val.toFixed(0)} mm³<br/><span style="color:var(--muted);font-size:10px;">${plen.toFixed(1)} mm</span></td>`;
        }
        mHtml += '</tr>';
      }
      mHtml += '</tbody></table>';
      matBox.innerHTML = mHtml;
    }

    // Render Toolchange Events
    const events = Array.isArray(info.events) ? info.events : [];
    if (eventsCountBadge) {
      eventsCountBadge.textContent = t("ui.sec6.events_count", { count: events.length });
      eventsCountBadge.className = events.length > 0 ? "badge info" : "badge empty";
    }
    evBody.innerHTML = "";
    if (events.length === 0) {
      evBody.innerHTML = `<tr><td colspan="6" style="color:var(--muted);">${escapeHtml(t("ui.flush.no_events"))}</td></tr>`;
    } else {
      events.forEach(ev => {
        const oldT = (ev.old_slicer_tool === null || ev.old_slicer_tool === undefined)
          ? t("ui.flush.init")
          : `T${Number(ev.old_slicer_tool)}`;
        const newSlicerT = Number(ev.new_slicer_tool);
        const mappedT = currentState.tool_map[newSlicerT] !== undefined
          ? Number(currentState.tool_map[newSlicerT])
          : Number(ev.mapped_tool);
        evBody.innerHTML += `
          <tr>
            <td>${Number(ev.index)}</td>
            <td>${filamentColorDot(newSlicerT)} ${escapeHtml(oldT)} → T${newSlicerT}</td>
            <td><strong>T${mappedT}</strong></td>
            <td>${Number(ev.raw_volume_mm3 || 0).toFixed(1)}</td>
            <td>${Number(ev.volume_mm3 || 0).toFixed(1)}</td>
            <td><strong>${Number(ev.purge_length_mm || 0).toFixed(2)} mm</strong></td>
          </tr>
        `;
      });
    }
  }

  if (typeof orca !== "undefined" && typeof orca.onMessage === "function") {
    orca.onMessage(msg => {
      if (!msg || typeof msg !== "object") return;
      ensureLogInitialized();
      if (msg.log) logMsg(msg.log);
      if (msg.state) {
        Object.assign(currentState, msg.state);
        if (msg.state.language) {
          currentLanguage = normalizeLang(msg.state.language);
        }
        document.getElementById("moonrakerUrl").value = currentState.moonraker_url || "http://192.168.8.80:7125";
        document.getElementById("apiKey").value = currentState.api_key || "";
        document.getElementById("port80Fallback").checked = !!currentState.enable_port80_fallback;
        document.getElementById("chkBedLeveling").checked = !!currentState.bed_leveling;
        document.getElementById("chkSkipPurgeLoaded").checked = currentState.skip_purge_for_already_loaded_tool !== false;
        document.getElementById("chkCutTipPost").checked = !!currentState.cut_tip_after_print;
        document.getElementById("chkForceInitialPurge").checked = !!currentState.force_initial_purge;
        const unitEl = document.getElementById("dryerUnit");
        if (unitEl && !unitEl.classList.contains("open") && msg.state.dryer_unit !== undefined) {
          setDryerUnitValue(currentState.dryer_unit);
        }
        const tempEl = document.getElementById("dryerTemp");
        if (tempEl && document.activeElement !== tempEl && msg.state.dryer_temp !== undefined) {
          tempEl.value = String(currentState.dryer_temp);
        }
        const durEl = document.getElementById("dryerDuration");
        if (durEl && document.activeElement !== durEl && msg.state.dryer_duration !== undefined) {
          durEl.value = String(currentState.dryer_duration);
        }
        const gateChk = document.getElementById("chkConfirmMaterialMismatch");
        if (gateChk) gateChk.checked = !!currentState.material_mismatch_confirmed;
        const gcodeInput = document.getElementById("gcodePathInput");
        if (gcodeInput && currentState.last_gcode_path !== undefined) {
          gcodeInput.value = currentState.last_gcode_path || "";
        }
      }
      if (msg.ace_status) {
        const st = msg.ace_status;
        currentState.ace_status = st;
        currentState.ace_slots = st.slots || currentState.ace_slots;
      }
      applyTranslations();
      if (msg.action === "open_preprint_modal") {
        closeAllSlotDropdowns();
        closeThumbZoomModal();
        for (const secId of ["settingsModal", "diagnosticsModal", "logModal"]) {
          closeDrawer(secId);
        }
        clearModalFeedback();
        openDrawer("prePrintModal");
        renderModalJobSummary();
        renderMappingTable();
        const activeFile = String(
          msg.source_file
          || (currentState.last_job_metadata && currentState.last_job_metadata.filename)
          || currentState.last_gcode_path
          || ""
        );
        const baseFile = activeFile ? activeFile.split(/[\\/]/).pop() : "";
        if (baseFile) {
          showModalFeedback("ok", t("host.auto_opened_preprint", { file: baseFile }));
        }
        sendToHost({ type: "preprint_modal_opened" });
      }
      if (msg.modal_feedback && typeof msg.modal_feedback === "object") {
        showModalFeedback(msg.modal_feedback.level || "info", msg.modal_feedback.text || "");
      }
    });
  }

  document.addEventListener("click", () => closeAllSlotDropdowns());
  document.addEventListener("keydown", evt => {
    if (evt.key !== "Escape") return;
    const zoomEl = document.getElementById("thumbZoomModal");
    if (zoomEl && zoomEl.classList.contains("open")) {
      evt.preventDefault();
      closeThumbZoomModal();
      return;
    }
    const openDropdowns = document.querySelectorAll(".slot-color-dropdown.open, .custom-select.open");
    if (openDropdowns.length > 0) {
      closeAllSlotDropdowns();
      return;
    }
    for (const secId of ["settingsModal", "diagnosticsModal", "logModal"]) {
      const secEl = document.getElementById(secId);
      if (secEl && secEl.classList.contains("open")) {
        closeDrawer(secId);
        return;
      }
    }
    const prePrintEl = document.getElementById("prePrintModal");
    if (prePrintEl && prePrintEl.classList.contains("open")) {
      closePrePrintModal();
    }
  });

  initTheme();
  ensureLogInitialized();
  applyTranslations();
  sendToHost({ type: "ready" });
</script>
</body>
</html>
""".replace("__I18N_JSON__", json.dumps(TRANSLATIONS, ensure_ascii=False))


# ---------------------------------------------------------------------------
# OrcaSlicer 2.5.x Capability Classes
# ---------------------------------------------------------------------------
def _activate_plugin_page_if_available() -> None:
    """
    When running inside OrcaSlicer's embedded CPython runtime, safely try to focus
    the plugin's tab if a host page-selection API exists. Never probes mocks when
    `HAS_ORCA_RUNTIME` is False.
    """
    if not HAS_ORCA_RUNTIME:
        return
    try:
        ui_mod = getattr(getattr(orca, "host", None), "ui", None)
        pages_mod = getattr(orca, "pages", None)
        for mod in (ui_mod, pages_mod):
            if mod is None:
                continue
            for fn_name in ("select_page", "activate_page", "focus_page", "show_page"):
                fn = getattr(mod, fn_name, None)
                if callable(fn):
                    fn("Kobra S1 ACE Pro")
                    return
    except Exception:
        pass


def _trigger_auto_preprint_popup(
    page: Any,
    source_kind: str,
    gcode_path: str,
    info: Optional[Dict[str, Any]],
    use_thread: bool = True,
) -> None:
    """
    Trigger the automatic Pre-Print confirmation modal & OrcaSlicer notification when
    a new slice/export G-code becomes ready:
    - Marks `GLOBAL_STATE.pending_preprint_open = True` so lazy WebView tabs open on `ready`.
    - Posts `{"action": "open_preprint_modal", ...}` to any live page capability.
    - Pushes a clickable native notification via `orca.host.ui.push_notification(...)`.
    """
    clean_path = str(gcode_path or "").strip()
    if not clean_path or info is None:
        with GLOBAL_STATE._lock:
            GLOBAL_STATE.pending_preprint_open = False
        if page is not None:
            try:
                page.post_message({"state": GLOBAL_STATE.to_dict()})
            except Exception:
                pass
        return

    file_name = os.path.basename(clean_path)
    with GLOBAL_STATE._lock:
        GLOBAL_STATE.pending_preprint_open = True
        GLOBAL_STATE.cleared_before_mtime_ns = 0
        cur_lang = GLOBAL_STATE.language
    try:
        GLOBAL_STATE.save_config()
    except Exception:
        pass

    def _dispatch_page() -> None:
        try:
            target = page
            if target is None:
                with _ACTIVE_PAGE_LOCK:
                    target = _ACTIVE_PAGE_CAPABILITY
            _activate_plugin_page_if_available()
            if target is not None:
                target.post_message(
                    {
                        "action": "open_preprint_modal",
                        "source": source_kind,
                        "source_file": file_name,
                        "state": GLOBAL_STATE.to_dict(),
                        "log": t(
                            "host.auto_opened_preprint",
                            lang=GLOBAL_STATE.language,
                            file=file_name,
                        ),
                    }
                )
        except Exception:
            pass

    ui_mod = getattr(getattr(orca, "host", None), "ui", None)
    push_fn = getattr(ui_mod, "push_notification", None) if ui_mod is not None else None
    if callable(push_fn):
        try:
            notif_enum = getattr(ui_mod, "NotificationLevel", None) or getattr(orca, "NotificationLevel", None)
            level = (
                getattr(notif_enum, "RegularNotificationLevel", None)
                or getattr(notif_enum, "ImportantNotificationLevel", None)
                if notif_enum is not None
                else None
            )
            if level is None:
                level = "info"
            tc_count = int((info or {}).get("toolchanges", 0) or 0)
            notif_text = t("host.notify_sliced_ready", lang=cur_lang, file=file_name, tc=tc_count)
            hyper_text = t("host.notify_open_action", lang=cur_lang)

            def _on_notification_click() -> bool:
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.pending_preprint_open = True
                    GLOBAL_STATE.cleared_before_mtime_ns = 0
                _dispatch_page()
                return True

            push_fn(level, notif_text, hyper_text=hyper_text, on_click=_on_notification_click)
        except Exception:
            pass

    if use_thread:
        threading.Thread(target=_dispatch_page, daemon=True).start()
    else:
        _dispatch_page()


class KobraS1AcePageCapability(orca.pages.PagesPluginCapabilityBase):
    """Interactive HTML Control & Color Mapping UI Tab inside OrcaSlicer 2.5.x."""

    def get_name(self) -> str:
        return "Kobra S1 ACE Pro"

    def on_load(self) -> None:
        global _ACTIVE_PAGE_CAPABILITY
        GLOBAL_STATE.load_config()
        with _ACTIVE_PAGE_LOCK:
            _ACTIVE_PAGE_CAPABILITY = self
        _schedule_pipeline_registration(GLOBAL_STATE, use_thread=True)

    def on_unload(self) -> None:
        global _ACTIVE_PAGE_CAPABILITY
        with _ACTIVE_PAGE_LOCK:
            if _ACTIVE_PAGE_CAPABILITY is self:
                _ACTIVE_PAGE_CAPABILITY = None

    def on_lifecycle_event(self, event: Any, ctx: Any) -> None:
        """
        Observe OrcaSlicer preset/project and slice/export lifecycle events non-blockingly.
        - Auto-registers Kobra S1 slicing pipeline capability via `begin_update` off UI thread.
        - Acquires only an explicit G-code path passed in `ctx` or an already staged file inside
          `<storage>/staged_gcode/`. Never scans `/tmp` or external system directories and never
          reads `state.last_gcode_path`.
        """
        if _handle_pipeline_lifecycle_event(event, ctx, GLOBAL_STATE, use_thread=True):
            return
        evt_enum = getattr(orca, "LifecycleEvent", None)
        err_enum = getattr(orca, "LifecycleEvtCode", None)
        if evt_enum is None:
            return
        watched = tuple(
            getattr(evt_enum, name, None)
            for name in ("GCodeExportFinished", "SlicingJobComplete")
            if getattr(evt_enum, name, None) is not None
        )
        if event not in watched:
            return
        if err_enum is not None and getattr(ctx, "code", None) == getattr(err_enum, "Error", object()):
            return

        ctx_name = str(getattr(ctx, "name", "") or "").strip()

        def _capture_and_notify() -> None:
            try:
                explicit = ""
                if ctx_name and _is_gcode_filename(ctx_name):
                    storage_dir = GLOBAL_STATE.resolve_storage_dir()
                    expected_staged = (
                        os.path.join(storage_dir, "staged_gcode", _sanitize_staged_gcode_filename(ctx_name))
                        if storage_dir
                        else ""
                    )
                    with GLOBAL_STATE._lock:
                        cur_src = GLOBAL_STATE.last_gcode_source
                        cur_saved = str(GLOBAL_STATE.last_gcode_path or "")
                    if (
                        expected_staged
                        and cur_src == "slicing_pipeline"
                        and cur_saved
                        and os.path.realpath(cur_saved) == os.path.realpath(expected_staged)
                        and os.path.isfile(expected_staged)
                    ):
                        explicit = expected_staged
                    elif os.path.isfile(ctx_name):
                        explicit = ctx_name

                if explicit:
                    staged_copy = _stage_gcode_file_to_storage(explicit, GLOBAL_STATE)
                    effective_explicit = staged_copy or explicit
                    source_kind, resolved_path, info = acquire_gcode_from_slicer(
                        GLOBAL_STATE,
                        explicit_path=effective_explicit,
                        include_last_gcode_path=False,
                        force_refresh=True,
                    )
                else:
                    staged = discover_sliced_gcode_candidates(GLOBAL_STATE, limit=1)
                    if staged:
                        source_kind, resolved_path, info = acquire_gcode_from_slicer(
                            GLOBAL_STATE,
                            explicit_path=staged[0],
                            include_last_gcode_path=False,
                            force_refresh=True,
                        )
                    else:
                        sync_filaments_from_orca_host(GLOBAL_STATE)
                        source_kind, resolved_path, info = "presets_only", "", None

                _trigger_auto_preprint_popup(
                    self,
                    source_kind,
                    resolved_path,
                    info,
                    use_thread=False,
                )
            except Exception:
                pass

        threading.Thread(target=_capture_and_notify, daemon=True).start()

    def get_ui(self) -> str:
        return ACE_PLUGIN_HTML_UI

    def on_message(self, data: Any) -> None:
        if not isinstance(data, dict):
            return

        msg_type = str(data.get("type") or "")
        if msg_type == "set_language":
            new_lang = GLOBAL_STATE.set_language(data.get("language"), save=True)
            lang_name = t(f"header.language.{new_lang}", lang=new_lang)
            self.post_message(
                {
                    "state": GLOBAL_STATE.to_dict(),
                    "log": t("host.lang_saved", lang=new_lang, lang_name=lang_name),
                }
            )
            return

        if "settings" in data and isinstance(data["settings"], dict):
            GLOBAL_STATE.apply_dict(data["settings"])

        if msg_type == "save_settings":
            self.post_message(
                {
                    "state": GLOBAL_STATE.to_dict(),
                    "log": t("host.settings_saved", lang=GLOBAL_STATE.language, url=GLOBAL_STATE.moonraker_url),
                }
            )
            return

        payload_copy = dict(data)
        worker = threading.Thread(
            target=self._handle_async_message,
            args=(msg_type, payload_copy),
            daemon=True,
        )
        worker.start()

    def _handle_async_message(self, msg_type: str, data: Dict[str, Any]) -> None:
        client = GLOBAL_STATE.get_client()
        try:
            if msg_type == "ready":
                GLOBAL_STATE.load_config()
                sync_filaments_from_orca_host(GLOBAL_STATE, preserve_gcode_usage=True)
                with GLOBAL_STATE._lock:
                    was_pending = bool(GLOBAL_STATE.pending_preprint_open)
                    cur_gcode = str(GLOBAL_STATE.last_gcode_path or "").strip()
                    has_info = bool(GLOBAL_STATE.last_postprocess_info)
                if (
                    was_pending
                    and cur_gcode
                    and not has_info
                    and not _is_system_temp_path(cur_gcode, GLOBAL_STATE.resolve_storage_dir())
                    and os.path.isfile(cur_gcode)
                ):
                    try:
                        preview_info = preview_gcode_file(cur_gcode, GLOBAL_STATE)
                        preview_info["analysis_mode"] = "preview"
                        with GLOBAL_STATE._lock:
                            GLOBAL_STATE.last_postprocess_info = dict(preview_info)
                    except Exception:
                        pass
                client = GLOBAL_STATE.get_client()
                extra = (
                    {
                        "action": "open_preprint_modal",
                        "source_file": os.path.basename(cur_gcode) if cur_gcode else "",
                    }
                    if was_pending
                    else None
                )
                self._send_state_and_live_status(
                    client, t("host.ready", lang=GLOBAL_STATE.language), extra=extra
                )
                return

            if msg_type == "preprint_modal_opened":
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.pending_preprint_open = False
                return

            if msg_type == "acquire_slicer_gcode":
                raw_path = str(data.get("gcode_path") or "").strip()
                force_refresh = bool(data.get("force_refresh", False))
                with GLOBAL_STATE._lock:
                    saved_path = str(GLOBAL_STATE.last_gcode_path or "").strip()
                explicit = raw_path if (raw_path and raw_path != saved_path) else ""
                if explicit and HAS_ORCA_RUNTIME and _is_system_temp_path(
                    explicit, GLOBAL_STATE.resolve_storage_dir()
                ):
                    explicit = ""
                if force_refresh:
                    explicit = ""
                elif explicit and GLOBAL_STATE.last_gcode_path:
                    try:
                        if os.path.realpath(explicit) == os.path.realpath(GLOBAL_STATE.last_gcode_path):
                            explicit = ""
                    except OSError:
                        if explicit == GLOBAL_STATE.last_gcode_path:
                            explicit = ""
                _kind, resolved_path, info = acquire_gcode_from_slicer(
                    GLOBAL_STATE,
                    explicit_path=explicit,
                    include_last_gcode_path=True,
                    force_refresh=force_refresh,
                )
                if resolved_path and info is not None:
                    log_msg = t(
                        "host.slicer_gcode_loaded",
                        lang=GLOBAL_STATE.language,
                        file=os.path.basename(resolved_path),
                        tc=info.get("toolchanges", 0),
                    )
                    modal_fb = {"level": "ok", "text": log_msg}
                else:
                    log_msg = t("host.slicer_gcode_not_found", lang=GLOBAL_STATE.language)
                    modal_fb = {"level": "warn", "text": log_msg}
                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "log": log_msg,
                        "modal_feedback": modal_fb,
                    }
                )
                return

            if msg_type == "load_gcode_content":
                fname = str(data.get("filename") or "selected.gcode")
                content = str(data.get("content") or "")
                try:
                    staged_path, info = load_gcode_from_content(fname, content, GLOBAL_STATE)
                    log_msg = t(
                        "host.gcode_browsed_loaded",
                        lang=GLOBAL_STATE.language,
                        file=os.path.basename(staged_path),
                        tc=info.get("toolchanges", 0),
                    )
                    modal_fb = {"level": "ok", "text": log_msg}
                except Exception as exc:
                    log_msg = t("host.gcode_read_error", lang=GLOBAL_STATE.language, error=exc)
                    modal_fb = {"level": "error", "text": log_msg}
                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "log": log_msg,
                        "modal_feedback": modal_fb,
                    }
                )
                return

            if msg_type == "sync_orca_filaments":
                with GLOBAL_STATE._lock:
                    has_active_gcode = bool(
                        GLOBAL_STATE.last_gcode_path
                        and os.path.isfile(GLOBAL_STATE.last_gcode_path)
                        and GLOBAL_STATE.slicer_filaments
                    )
                fils = sync_filaments_from_orca_host(
                    GLOBAL_STATE, preserve_gcode_usage=has_active_gcode
                )
                if fils:
                    GLOBAL_STATE.save_config()
                    msg = t("host.sync_filaments", lang=GLOBAL_STATE.language, count=len(fils))
                else:
                    msg = t("host.sync_none", lang=GLOBAL_STATE.language)
                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "log": msg,
                    }
                )
                return

            if msg_type == "get_status":
                self._send_state_and_live_status(client, t("host.status_updated", lang=GLOBAL_STATE.language))
                return

            if msg_type == "trigger_macro":
                macro = str(data.get("macro") or "CUT_TIP").strip().upper()
                if not GLOBAL_STATE._hw_action_lock.acquire(blocking=False):
                    self.post_message({"log": t("host.hw_busy", lang=GLOBAL_STATE.language)})
                    return
                try:
                    check_print_safety(client, f"Macro {macro}", GLOBAL_STATE)
                    if macro == "CUT_TIP":
                        client.trigger_cut_tip()
                    elif macro in ("G31", "BED_MESH_CALIBRATE"):
                        client.trigger_bed_leveling(macro)
                    else:
                        client.send_gcode(macro)
                finally:
                    GLOBAL_STATE._hw_action_lock.release()
                self._send_state_and_live_status(
                    client, t("host.macro_sent", lang=GLOBAL_STATE.language, macro=macro)
                )
                return

            if msg_type == "dryer_start":
                raw_unit = str(
                    data.get("unit") if "unit" in data else (GLOBAL_STATE.dryer_unit or "all")
                ).strip().lower()
                unit = raw_unit if raw_unit in ("all", "0", "1") else "all"
                try:
                    raw_temp = int(float(data.get("temp") if data.get("temp") is not None else GLOBAL_STATE.dryer_temp))
                except (ValueError, TypeError):
                    raw_temp = 50
                temp = max(20, min(70, raw_temp))
                try:
                    raw_duration = int(float(data.get("duration") if data.get("duration") is not None else GLOBAL_STATE.dryer_duration))
                except (ValueError, TypeError):
                    raw_duration = 240
                duration = max(1, min(1440, raw_duration))
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.dryer_unit = unit
                    GLOBAL_STATE.dryer_temp = temp
                    GLOBAL_STATE.dryer_duration = duration
                GLOBAL_STATE.save_config()

                instance = 0 if unit == "0" else (1 if unit == "1" else None)
                cmd_str = f"ACE_START_DRYING TEMP={temp} DURATION={duration}" + (
                    f" INSTANCE={instance}" if instance is not None else ""
                )
                unit_key = (
                    "ui.sec2.unit_ace1"
                    if unit == "0"
                    else ("ui.sec2.unit_ace2" if unit == "1" else "ui.sec2.unit_all")
                )
                unit_label = t(unit_key, lang=GLOBAL_STATE.language)

                if not GLOBAL_STATE._hw_action_lock.acquire(blocking=False):
                    self.post_message({"log": t("host.hw_busy", lang=GLOBAL_STATE.language)})
                    return
                try:
                    client.start_dryer(temp_c=temp, duration_min=duration, instance=instance)
                finally:
                    GLOBAL_STATE._hw_action_lock.release()
                self._send_state_and_live_status(
                    client,
                    t(
                        "host.dryer_started_unit",
                        lang=GLOBAL_STATE.language,
                        unit=unit_label,
                        temp=temp,
                        duration=duration,
                        cmd=cmd_str,
                    ),
                )
                return

            if msg_type == "dryer_stop":
                raw_unit = str(
                    data.get("unit") if "unit" in data else (GLOBAL_STATE.dryer_unit or "all")
                ).strip().lower()
                unit = raw_unit if raw_unit in ("all", "0", "1") else "all"
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.dryer_unit = unit
                GLOBAL_STATE.save_config()

                instance = 0 if unit == "0" else (1 if unit == "1" else None)
                cmd_str = "ACE_STOP_DRYING" + (
                    f" INSTANCE={instance}" if instance is not None else ""
                )
                unit_key = (
                    "ui.sec2.unit_ace1"
                    if unit == "0"
                    else ("ui.sec2.unit_ace2" if unit == "1" else "ui.sec2.unit_all")
                )
                unit_label = t(unit_key, lang=GLOBAL_STATE.language)

                if not GLOBAL_STATE._hw_action_lock.acquire(blocking=False):
                    self.post_message({"log": t("host.hw_busy", lang=GLOBAL_STATE.language)})
                    return
                try:
                    client.stop_dryer(instance=instance)
                finally:
                    GLOBAL_STATE._hw_action_lock.release()
                self._send_state_and_live_status(
                    client,
                    t(
                        "host.dryer_stopped_unit",
                        lang=GLOBAL_STATE.language,
                        unit=unit_label,
                        cmd=cmd_str,
                    ),
                )
                return

            if msg_type == "slot_action":
                action = str(data.get("action") or "")
                tool = int(data.get("tool", 0))
                length = int(data.get("length", 50))
                if not GLOBAL_STATE._hw_action_lock.acquire(blocking=False):
                    self.post_message({"log": t("host.hw_busy", lang=GLOBAL_STATE.language)})
                    return
                try:
                    check_print_safety(client, f"Slot {action} T{tool}", GLOBAL_STATE)
                    if action == "load":
                        client.load_slot(tool)
                        log_text = t(
                            "host.slot_loaded",
                            lang=GLOBAL_STATE.language,
                            slot=tool,
                            cmd=f"ACE_CHANGE_TOOL TOOL={tool}",
                        )
                    elif action == "unload":
                        client.unload_slot(tool=tool)
                        log_text = t(
                            "host.slot_unloaded",
                            lang=GLOBAL_STATE.language,
                            slot=tool,
                            cmd=f"ACE_SMART_UNLOAD TOOL={tool}",
                        )
                    elif action == "unload_active":
                        client.unload_slot(tool=None)
                        log_text = t("host.slot_active_unloaded", lang=GLOBAL_STATE.language)
                    elif action == "feed":
                        client.feed_slot(tool=tool, length_mm=length)
                        log_text = t("host.slot_fed", lang=GLOBAL_STATE.language, slot=tool, length=length)
                    elif action == "retract":
                        client.retract_slot(tool=tool, length_mm=length)
                        log_text = t("host.slot_retracted", lang=GLOBAL_STATE.language, slot=tool, length=length)
                    else:
                        log_text = t("host.slot_unknown_action", lang=GLOBAL_STATE.language, action=action)
                finally:
                    GLOBAL_STATE._hw_action_lock.release()
                self._send_state_and_live_status(client, log_text)
                return

            if msg_type == "parse_gcode":
                gcode_path = str(data.get("gcode_path") or GLOBAL_STATE.last_gcode_path).strip()
                if not gcode_path or not os.path.isfile(gcode_path):
                    self.post_message(
                        {"log": t("host.file_not_found", lang=GLOBAL_STATE.language, path=gcode_path)}
                    )
                    return
                with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.last_gcode_path = gcode_path
                    was_reset = GLOBAL_STATE.update_slicer_filaments(parse_slicer_filaments(lines))
                    GLOBAL_STATE.last_job_metadata = parse_gcode_job_metadata(lines, gcode_path)
                    fil_count = len(GLOBAL_STATE.slicer_filaments)
                info = analyze_gcode_flush(lines, GLOBAL_STATE)
                info["analysis_mode"] = "preview"
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.last_postprocess_info = dict(info)
                GLOBAL_STATE.save_config()
                reset_note = t("host.parse_reset_note", lang=GLOBAL_STATE.language) if was_reset else ""
                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "log": t(
                            "host.parse_ok",
                            lang=GLOBAL_STATE.language,
                            count=fil_count,
                            file=os.path.basename(gcode_path),
                            note=reset_note,
                        ),
                    }
                )
                return

            if msg_type == "clear_gcode":
                newest_ns = 0
                for cand in discover_sliced_gcode_candidates(GLOBAL_STATE):
                    try:
                        st_cand = os.stat(cand)
                        cand_ns = int(getattr(st_cand, "st_mtime_ns", int(float(st_cand.st_mtime) * 1_000_000_000)))
                        newest_ns = max(newest_ns, cand_ns)
                    except OSError:
                        pass
                with GLOBAL_STATE._lock:
                    if GLOBAL_STATE.last_gcode_path and os.path.isfile(GLOBAL_STATE.last_gcode_path):
                        try:
                            st_cur = os.stat(GLOBAL_STATE.last_gcode_path)
                            cur_ns = int(getattr(st_cur, "st_mtime_ns", int(float(st_cur.st_mtime) * 1_000_000_000)))
                            newest_ns = max(newest_ns, cur_ns)
                        except OSError:
                            pass
                    GLOBAL_STATE.cleared_before_mtime_ns = newest_ns
                    GLOBAL_STATE.last_gcode_path = ""
                    GLOBAL_STATE.last_gcode_source = ""
                    GLOBAL_STATE.last_job_metadata = {}
                    GLOBAL_STATE.last_postprocess_info = {}
                    GLOBAL_STATE.mapping_warnings = []
                    GLOBAL_STATE.material_mismatch_confirmed = False
                    GLOBAL_STATE.pending_preprint_open = False
                host_fils = sync_filaments_from_orca_host(GLOBAL_STATE, preserve_gcode_usage=False)
                if not host_fils:
                    with GLOBAL_STATE._lock:
                        GLOBAL_STATE.slicer_filaments = []
                        GLOBAL_STATE.filament_signature = ""
                GLOBAL_STATE.save_config()
                cleared_msg = t("host.gcode_cleared", lang=GLOBAL_STATE.language)
                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "log": cleared_msg,
                        "modal_feedback": {
                            "level": "info",
                            "text": cleared_msg,
                        },
                    }
                )
                return

            if msg_type == "update_mapping":
                with GLOBAL_STATE._lock:
                    slicer_fils = list(GLOBAL_STATE.slicer_filaments)
                    ace_slots = list((GLOBAL_STATE.last_ace_status or {}).get("slots", []))
                    cur_lang = GLOBAL_STATE.language
                    tmap = dict(GLOBAL_STATE.tool_map)
                    if ace_slots and slicer_fils:
                        validation = validate_color_mapping(slicer_fils, tmap, ace_slots, lang=cur_lang)
                        GLOBAL_STATE.mapping_warnings = list(validation["errors"] + validation["warnings"])
                    else:
                        validation = validate_color_mapping(slicer_fils, tmap, [], lang=cur_lang)
                        GLOBAL_STATE.mapping_warnings = []
                    if GLOBAL_STATE.last_postprocess_info:
                        remapped = remap_flush_info(GLOBAL_STATE.last_postprocess_info, tmap)
                        remapped["mapping_validation"] = validation
                        GLOBAL_STATE.last_postprocess_info = remapped
                GLOBAL_STATE.save_config()
                self.post_message({"state": GLOBAL_STATE.to_dict()})
                return

            if msg_type == "auto_match":
                ace_status = client.query_ace_status()
                with GLOBAL_STATE._lock:
                    slicer_fils = list(GLOBAL_STATE.slicer_filaments)
                    cur_lang = GLOBAL_STATE.language
                matched = auto_match_filaments_to_slots(
                    slicer_fils,
                    ace_status.get("slots", []),
                )
                validation = validate_color_mapping(
                    slicer_fils, matched, ace_status.get("slots", []), lang=cur_lang
                )
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.tool_map.update(matched)
                    for matched_tool in matched:
                        GLOBAL_STATE.tool_map_sources[int(matched_tool)] = "auto"
                    GLOBAL_STATE.material_mismatch_confirmed = False
                    GLOBAL_STATE.mapping_warnings = list(validation["errors"] + validation["warnings"])
                    if GLOBAL_STATE.last_postprocess_info:
                        remapped = remap_flush_info(GLOBAL_STATE.last_postprocess_info, GLOBAL_STATE.tool_map)
                        remapped["mapping_validation"] = validation
                        GLOBAL_STATE.last_postprocess_info = remapped
                GLOBAL_STATE.save_config()
                warn_suffix = (
                    t(
                        "host.automatch_warns",
                        lang=cur_lang,
                        warns="; ".join(validation["errors"] + validation["warnings"]),
                    )
                    if (validation["errors"] or validation["warnings"])
                    else ""
                )
                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "ace_status": ace_status,
                        "log": t(
                            "host.automatch_log",
                            lang=cur_lang,
                            matched=json.dumps(matched),
                            warns=warn_suffix,
                        ),
                    }
                )
                return

            if msg_type == "reset_mapping":
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.tool_map = {i: i for i in range(MAX_ACE_SLOTS)}
                    GLOBAL_STATE.tool_map_sources = {i: "default" for i in range(MAX_ACE_SLOTS)}
                    GLOBAL_STATE.material_mismatch_confirmed = False
                    slicer_fils = list(GLOBAL_STATE.slicer_filaments)
                    ace_slots = list((GLOBAL_STATE.last_ace_status or {}).get("slots", []))
                    cur_lang = GLOBAL_STATE.language
                    if ace_slots and slicer_fils:
                        validation = validate_color_mapping(
                            slicer_fils, GLOBAL_STATE.tool_map, ace_slots, lang=cur_lang
                        )
                        GLOBAL_STATE.mapping_warnings = list(validation["errors"] + validation["warnings"])
                    else:
                        validation = validate_color_mapping(
                            slicer_fils, GLOBAL_STATE.tool_map, [], lang=cur_lang
                        )
                        GLOBAL_STATE.mapping_warnings = []
                    if GLOBAL_STATE.last_postprocess_info:
                        remapped = remap_flush_info(GLOBAL_STATE.last_postprocess_info, GLOBAL_STATE.tool_map)
                        remapped["mapping_validation"] = validation
                        GLOBAL_STATE.last_postprocess_info = remapped
                GLOBAL_STATE.save_config()
                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "log": t("host.reset_done", lang=cur_lang),
                    }
                )
                return

            if msg_type == "preview_gcode":
                gcode_path = str(data.get("gcode_path") or GLOBAL_STATE.last_gcode_path).strip()
                if not gcode_path or not os.path.isfile(gcode_path):
                    self.post_message(
                        {"log": t("host.file_not_found", lang=GLOBAL_STATE.language, path=gcode_path)}
                    )
                    return
                info = preview_gcode_file(gcode_path, GLOBAL_STATE)
                log_msg = t(
                    "host.preview_log",
                    lang=GLOBAL_STATE.language,
                    file=os.path.basename(gcode_path),
                    status=info["flush_status"],
                    tc=info["toolchanges"],
                    len=f"{info['total_purge_length_mm']:.2f}",
                    vol=f"{info['total_purge_volume_mm3']:.1f}",
                )
                self.post_message({"state": GLOBAL_STATE.to_dict(), "log": log_msg})
                return

            if msg_type in ("postprocess_only", "postprocess_and_upload"):
                gcode_path = str(data.get("gcode_path") or GLOBAL_STATE.last_gcode_path).strip()
                if not gcode_path or not os.path.isfile(gcode_path):
                    no_file_msg = (
                        t("host.no_gcode_loaded", lang=GLOBAL_STATE.language)
                        if not gcode_path
                        else t("host.file_not_found", lang=GLOBAL_STATE.language, path=gcode_path)
                    )
                    self.post_message(
                        {
                            "state": GLOBAL_STATE.to_dict(),
                            "log": no_file_msg,
                            "modal_feedback": {"level": "error", "text": no_file_msg},
                        }
                    )
                    return

                info = postprocess_gcode_file(gcode_path, GLOBAL_STATE)
                log_msg = t(
                    "host.pp_log",
                    lang=GLOBAL_STATE.language,
                    file=os.path.basename(gcode_path),
                    status=info["flush_status"],
                    tc=info["toolchanges"],
                    mul=info["flush_multiplier"],
                    bed=info["bedLeveling"],
                    len=f"{info.get('total_purge_length_mm', 0.0):.2f}",
                )
                modal_fb: Dict[str, str] = {
                    "level": "ok",
                    "text": t(
                        "host.pp_modal_ok",
                        lang=GLOBAL_STATE.language,
                        file=os.path.basename(gcode_path),
                        tc=info["toolchanges"],
                        len=f"{info.get('total_purge_length_mm', 0.0):.2f}",
                    ),
                }

                if msg_type == "postprocess_and_upload":
                    try:
                        live_status = client.query_ace_status()
                        with GLOBAL_STATE._lock:
                            GLOBAL_STATE.last_ace_status = dict(live_status)
                    except Exception:
                        with GLOBAL_STATE._lock:
                            live_status = dict(GLOBAL_STATE.last_ace_status) if GLOBAL_STATE.last_ace_status else None

                    mismatch_conf: Optional[bool] = None
                    if data.get("require_mismatch_gate") or "mismatch_confirmed" in data:
                        mismatch_conf = bool(data.get("mismatch_confirmed", GLOBAL_STATE.material_mismatch_confirmed))

                    settings_payload = data.get("settings") if isinstance(data.get("settings"), dict) else {}
                    if "start_print" in data:
                        start_print_req = bool(data["start_print"])
                    elif "start_print_immediately" in settings_payload:
                        start_print_req = bool(settings_payload["start_print_immediately"])
                    else:
                        start_print_req = bool(GLOBAL_STATE.start_print_immediately)

                    allowed, blocking_reasons, _gate_warnings = can_upload_gcode(
                        info,
                        GLOBAL_STATE,
                        ace_status=live_status,
                        start_print=start_print_req,
                        mismatch_confirmed=mismatch_conf,
                    )
                    if not allowed:
                        blocked_msg = t(
                            "host.upload_gate_blocked",
                            lang=GLOBAL_STATE.language,
                            base=log_msg,
                            reasons="; ".join(blocking_reasons),
                        )
                        self.post_message(
                            {
                                "state": GLOBAL_STATE.to_dict(),
                                "log": blocked_msg,
                                "modal_feedback": {"level": "error", "text": blocked_msg},
                            }
                        )
                        return

                    if start_print_req:
                        if not GLOBAL_STATE._hw_action_lock.acquire(blocking=False):
                            busy_msg = t("host.hw_busy_upload", lang=GLOBAL_STATE.language)
                            self.post_message(
                                {
                                    "log": busy_msg,
                                    "modal_feedback": {"level": "warn", "text": busy_msg},
                                }
                            )
                            return
                        try:
                            check_print_safety(client, "Moonraker Upload & Start Print", GLOBAL_STATE)
                            with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
                                patched_content = f.read()
                            client.upload_gcode(
                                filename=os.path.basename(gcode_path),
                                gcode_content=patched_content,
                                start_print=True,
                            )
                        finally:
                            GLOBAL_STATE._hw_action_lock.release()
                    else:
                        with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
                            patched_content = f.read()
                        client.upload_gcode(
                            filename=os.path.basename(gcode_path),
                            gcode_content=patched_content,
                            start_print=False,
                        )
                    done_key = (
                        "host.upload_done_started"
                        if start_print_req
                        else "host.upload_done"
                    )
                    log_msg = t(
                        done_key,
                        lang=GLOBAL_STATE.language,
                        base=log_msg,
                        url=client.active_url,
                    )
                    mode_label = t(
                        "host.upload_mode_print" if start_print_req else "host.upload_mode_only",
                        lang=GLOBAL_STATE.language,
                    )
                    modal_fb = {
                        "level": "ok",
                        "text": t(
                            "host.upload_modal_ok",
                            lang=GLOBAL_STATE.language,
                            file=os.path.basename(gcode_path),
                            url=client.active_url,
                            mode=mode_label,
                            len=f"{info.get('total_purge_length_mm', 0.0):.2f}",
                        ),
                    }

                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "log": log_msg,
                        "modal_feedback": modal_fb,
                    }
                )
                return

        except Exception as exc:
            err_msg = t("host.msg_error", lang=GLOBAL_STATE.language, msg_type=msg_type, error=exc)
            err_payload: Dict[str, Any] = {
                "state": GLOBAL_STATE.to_dict(),
                "log": err_msg,
            }
            if msg_type in (
                "postprocess_only",
                "postprocess_and_upload",
                "acquire_slicer_gcode",
                "load_gcode_content",
                "preview_gcode",
            ):
                err_payload["modal_feedback"] = {"level": "error", "text": err_msg}
            self.post_message(err_payload)

    def _send_state_and_live_status(
        self,
        client: MoonrakerClient,
        log_msg: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            ace_status = client.query_ace_status()
            with GLOBAL_STATE._lock:
                GLOBAL_STATE.last_ace_status = dict(ace_status)
            payload: Dict[str, Any] = {
                "state": GLOBAL_STATE.to_dict(),
                "ace_status": ace_status,
                "log": f"{log_msg} (Moonraker: {client.active_url})",
            }
            if extra:
                payload.update(extra)
            self.post_message(payload)
        except Exception as exc:
            payload = {
                "state": GLOBAL_STATE.to_dict(),
                "log": t("host.status_query_failed", lang=GLOBAL_STATE.language, base=log_msg, error=exc),
            }
            if extra:
                payload.update(extra)
            self.post_message(payload)


def _enrich_audit_info(
    info: Dict[str, Any],
    filaments: List[Dict[str, Any]],
    tool_map: Dict[int, int],
    ace_slots: List[Dict[str, Any]],
    lang: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach aggregate flush metrics and color mapping validation to a process_gcode result dict."""
    events = info.get("events") or []
    info["total_purge_volume_mm3"] = round(
        sum(float(ev.get("volume_mm3", 0.0)) for ev in events), 2
    )
    info["total_purge_length_mm"] = round(
        sum(float(ev.get("purge_length_mm", 0.0)) for ev in events), 2
    )
    info["max_purge_length_mm"] = round(
        max((float(ev.get("purge_length_mm", 0.0)) for ev in events), default=0.0), 2
    )
    info["slicer_filaments"] = list(filaments)
    info["mapping_validation"] = validate_color_mapping(filaments, tool_map, ace_slots, lang=lang)
    return info


def analyze_gcode_flush(
    lines: List[str],
    state: Optional[PluginState] = None,
    filepath: str = "",
) -> Dict[str, Any]:
    """
    Pure in-memory read-only G-code & flush-volume analysis using `state` configuration.
    Never modifies files or makes any network requests.
    """
    st = state or GLOBAL_STATE
    with st._lock:
        diam = st.filament_diameter
        tmap = dict(st.tool_map)
        bed_lvl = st.bed_leveling
        skip_purge = st.skip_purge_for_already_loaded_tool
        cut_post = st.cut_tip_after_print
        force_init = st.force_initial_purge
        ace_slots = list((st.last_ace_status or {}).get("slots", []))
        cur_lang = st.language
        effective_path = filepath or st.last_gcode_path

    filaments = parse_slicer_filaments(lines)
    job_metadata = parse_gcode_job_metadata(lines, effective_path)
    _, info = process_gcode(
        lines=lines,
        filament_diameter=diam,
        round_digits=DEFAULT_ROUND_DIGITS,
        tool_map=tmap,
        bed_leveling=bed_lvl,
        skip_purge_for_already_loaded_tool=skip_purge,
        cut_tip_after_print=cut_post,
        force_initial_purge=force_init,
        include_audit_header=True,
        lang=cur_lang,
    )
    info["job_metadata"] = job_metadata
    return _enrich_audit_info(info, filaments, tmap, ace_slots, lang=cur_lang)


def preview_gcode_file(gcode_path: str, state: Optional[PluginState] = None) -> Dict[str, Any]:
    """
    Read `gcode_path` strictly read-only (`"r"` mode only), analyze flush metrics & events,
    and update in-memory preview state without writing to disk or calling Moonraker.
    """
    st = state or GLOBAL_STATE
    with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    filaments = parse_slicer_filaments(lines)
    job_meta = parse_gcode_job_metadata(lines, gcode_path)
    with st._lock:
        st.last_gcode_path = gcode_path
        st.update_slicer_filaments(filaments)
        st.last_job_metadata = job_meta

    info = analyze_gcode_flush(lines, st, filepath=gcode_path)
    info["analysis_mode"] = "preview"
    with st._lock:
        st.last_postprocess_info = dict(info)
    return info


def can_upload_gcode(
    info: Optional[Dict[str, Any]],
    state: Optional[PluginState] = None,
    ace_status: Optional[Dict[str, Any]] = None,
    start_print: bool = False,
    mismatch_confirmed: Optional[bool] = None,
) -> Tuple[bool, List[str], List[str]]:
    """
    Upload Safety Gate: blocks Moonraker upload when multi-tool flush data is incomplete,
    color mapping targets empty/out-of-range active slots, material mismatch is
    unconfirmed (`mismatch_confirmed=False`), or `start_print=True` while another print job
    is already running (`printing` or `paused`). Shared ACE slots (intentional multi-extruder
    reuse) are allowed and non-blocking.
    Returns `(allowed, blocking_reasons, warnings)`.
    """
    st = state or GLOBAL_STATE
    cur_lang = getattr(st, "language", DEFAULT_LANGUAGE)
    blocking_reasons: List[str] = []
    warnings: List[str] = list((info or {}).get("warnings", []))

    if not info or not isinstance(info, dict):
        blocking_reasons.append(t("gate.block.no_info", lang=cur_lang))
        return False, blocking_reasons, warnings

    used_tools = info.get("used_slicer_tools") or []
    flush_status = str(info.get("flush_status") or "")
    flush_complete = bool(info.get("flush_data_complete", True))
    if flush_status == "FLUSH_DATA_INCOMPLETE":
        blocking_reasons.append(t("gate.block.flush_incomplete", lang=cur_lang))
    elif len(used_tools) > 1 and not flush_complete:
        blocking_reasons.append(t("gate.block.multi_incomplete", lang=cur_lang))

    with st._lock:
        fils = list(info.get("slicer_filaments") or st.slicer_filaments)
        tmap = dict(info.get("toolMap") if isinstance(info.get("toolMap"), dict) else st.tool_map)
        effective_status = ace_status if ace_status is not None else st.last_ace_status

    slots = list((effective_status or {}).get("slots", []))
    if slots:
        val = validate_color_mapping(fils, tmap, slots, lang=cur_lang)
        blocking_reasons.extend(val.get("errors", []))
        if val.get("has_material_mismatch") and mismatch_confirmed is False:
            blocking_reasons.append(t("gate.block.mat_mismatch_unconfirmed", lang=cur_lang))
        warnings.extend(val.get("warnings", []))

    if start_print and isinstance(effective_status, dict) and effective_status.get("is_printing"):
        p_state = effective_status.get("print_state", "printing")
        blocking_reasons.append(t("gate.block.printing_start", lang=cur_lang, state=p_state))

    dedup_blocks = list(dict.fromkeys(blocking_reasons))
    dedup_warns = list(dict.fromkeys(warnings))
    return (len(dedup_blocks) == 0), dedup_blocks, dedup_warns


def postprocess_gcode_file(gcode_path: str, state: Optional[PluginState] = None) -> Dict[str, Any]:
    """Read, strip prior ACE artifacts, post-process, and atomically write a G-code file using `state`."""
    st = state or GLOBAL_STATE
    with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    with st._lock:
        st.last_gcode_path = gcode_path
        filaments = parse_slicer_filaments(lines)
        job_meta = parse_gcode_job_metadata(lines, gcode_path)
        st.slicer_filaments = filaments
        st.last_job_metadata = job_meta
        st.filament_signature = compute_filament_signature(filaments)
        diam = st.filament_diameter
        tmap = dict(st.tool_map)
        bed_lvl = st.bed_leveling
        skip_purge = st.skip_purge_for_already_loaded_tool
        cut_post = st.cut_tip_after_print
        force_init = st.force_initial_purge
        ace_slots = list((st.last_ace_status or {}).get("slots", []))
        cur_lang = st.language

    out_lines, info = process_gcode(
        lines=lines,
        filament_diameter=diam,
        round_digits=DEFAULT_ROUND_DIGITS,
        tool_map=tmap,
        bed_leveling=bed_lvl,
        skip_purge_for_already_loaded_tool=skip_purge,
        cut_tip_after_print=cut_post,
        force_initial_purge=force_init,
        include_audit_header=True,
        lang=cur_lang,
    )
    info["job_metadata"] = job_meta
    _enrich_audit_info(info, filaments, tmap, ace_slots, lang=cur_lang)
    info["analysis_mode"] = "postprocessed"

    target_abs = os.path.abspath(gcode_path)
    target_dir = os.path.dirname(target_abs) or "."
    tmp_path: Optional[str] = None
    try:
        tmp_path, f = _open_atomic_temp_file(
            target_dir,
            prefix=".ace_tmp_",
            suffix=".gcode",
            mode="w",
            encoding="utf-8",
        )
        with f:
            f.writelines(out_lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_abs)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    with st._lock:
        st.last_postprocess_info = info
    st.save_config()
    return info


class KobraS1AcePostProcessCapability(orca.slicing.SlicingPipelineCapabilityBase):
    """
    OrcaSlicer 2.5.x Slicing Pipeline Post-Processing Hook (`psGCodePostProcess`).
    Automatically patches the generated G-code file after slicing/export.
    """

    def get_name(self) -> str:
        return PIPELINE_CAPABILITY_NAME

    def on_load(self) -> None:
        GLOBAL_STATE.load_config()
        _schedule_pipeline_registration(GLOBAL_STATE, use_thread=True)

    def on_lifecycle_event(self, event: Any, ctx: Any) -> None:
        _handle_pipeline_lifecycle_event(event, ctx, GLOBAL_STATE, use_thread=True)

    def execute(self, ctx: Any) -> Any:
        step = getattr(ctx, "step", None)
        expected_step = getattr(orca.slicing.Step, "psGCodePostProcess", "psGCodePostProcess")
        if step is not None and step != expected_step:
            return orca.ExecutionResult.success()

        gcode_path = getattr(ctx, "gcode_path", None)
        if not gcode_path or not os.path.isfile(gcode_path):
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                f"G-code file not found: {gcode_path}",
            )

        try:
            info = postprocess_gcode_file(str(gcode_path), GLOBAL_STATE)
            staged_path = _stage_gcode_file_to_storage(str(gcode_path), GLOBAL_STATE)
            with GLOBAL_STATE._lock:
                if staged_path:
                    GLOBAL_STATE.last_gcode_path = staged_path
                GLOBAL_STATE.last_gcode_source = "slicing_pipeline"
                GLOBAL_STATE.cleared_before_mtime_ns = 0
            GLOBAL_STATE.save_config()
            with _ACTIVE_PAGE_LOCK:
                page = _ACTIVE_PAGE_CAPABILITY
            _trigger_auto_preprint_popup(
                page,
                "slicing_pipeline",
                staged_path or str(gcode_path),
                info,
                use_thread=True,
            )
            return orca.ExecutionResult.success(
                f"Kobra S1 ACE Pro post-processed {info['toolchanges']} toolchanges."
            )
        except Exception as exc:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                f"Kobra S1 ACE Pro post-processing failed: {exc}",
            )


@orca.plugin
class KobraS1AcePlugin(orca.base):
    """Main OrcaSlicer 2.5.x Plugin Package Entry Point."""

    def register_capabilities(self) -> None:
        # Load persisted config first so any custom Moonraker host/port is included
        # when requesting upfront network permissions.
        GLOBAL_STATE.load_config()
        # Request upfront permissions for Moonraker HTTP requests (port 7125 + port 80 fallback)
        # and background threading. `data_dir()` (including `orca.host.plugin.storage()`) and
        # `ctx.gcode_path`'s parent directory are already allowed roots.
        configured_url = GLOBAL_STATE.moonraker_url
        parsed = urllib.parse.urlsplit(configured_url)
        host = parsed.hostname or "192.168.8.80"
        allowed_urls = [
            DEFAULT_MOONRAKER_URL,
            "http://192.168.8.80",
            "http://192.168.8.80:80",
            configured_url,
            f"http://{host}:7125",
            f"http://{host}",
            f"http://{host}:80",
        ]
        unique_urls = list(dict.fromkeys(allowed_urls))

        try:
            orca.request_permissions(
                network_http=unique_urls,
                threading=True,
            )
        except TypeError:
            try:
                orca.request_permissions(network_http=unique_urls)
            except Exception:
                pass

        orca.register_capability(KobraS1AcePageCapability)
        orca.register_capability(KobraS1AcePostProcessCapability)


# ---------------------------------------------------------------------------
# Standalone CLI Support (compatible with orca_flush_to_purgelength.py)
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or "-h" in args or "--help" in args:
        print(
            "Usage: kobra_s1_ace_plugin.py <gcode_file> "
            "[--diameter 1.75] [--round 2] [--tool-map 0:0,1:1] "
            "[--no-bed-leveling] [--skip-purge|--no-skip-purge] [--cut-tip-post] [--force-initial-purge] "
            "[--upload [http://192.168.8.80:7125]] [--start-print]"
        )
        return 0 if ("-h" in args or "--help" in args) else 2

    filament_diameter = float(os.getenv("FLUSH_FILAMENT_DIAMETER", DEFAULT_FILAMENT_DIAMETER))
    round_digits = DEFAULT_ROUND_DIGITS
    tool_map: Dict[int, int] = dict(GLOBAL_STATE.tool_map)
    bed_leveling = GLOBAL_STATE.bed_leveling
    skip_purge = GLOBAL_STATE.skip_purge_for_already_loaded_tool
    cut_tip_post = GLOBAL_STATE.cut_tip_after_print
    force_initial_purge = GLOBAL_STATE.force_initial_purge
    do_upload = False
    moonraker_url = GLOBAL_STATE.moonraker_url
    start_print = GLOBAL_STATE.start_print_immediately
    gcode_path = ""

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--diameter" and i + 1 < len(args):
            filament_diameter = float(args[i + 1])
            i += 2
        elif arg == "--round" and i + 1 < len(args):
            round_digits = max(0, int(args[i + 1]))
            i += 2
        elif arg == "--tool-map" and i + 1 < len(args):
            for pair in args[i + 1].split(","):
                if ":" in pair:
                    k_str, v_str = pair.split(":", 1)
                    tool_map[int(k_str.strip())] = int(v_str.strip())
            i += 2
        elif arg == "--no-bed-leveling":
            bed_leveling = False
            i += 1
        elif arg == "--bed-leveling":
            bed_leveling = True
            i += 1
        elif arg == "--skip-purge":
            skip_purge = True
            i += 1
        elif arg == "--no-skip-purge":
            skip_purge = False
            i += 1
        elif arg == "--cut-tip-post":
            cut_tip_post = True
            i += 1
        elif arg == "--force-initial-purge":
            force_initial_purge = True
            i += 1
        elif arg == "--upload":
            do_upload = True
            if i + 1 < len(args) and args[i + 1].startswith("http"):
                moonraker_url = args[i + 1]
                i += 2
            else:
                i += 1
        elif arg == "--start-print":
            start_print = True
            i += 1
        else:
            gcode_path = arg
            i += 1

    if not gcode_path or not os.path.isfile(gcode_path):
        print(f"[kobra_s1_ace_plugin] ERROR: G-code file not found: {gcode_path}", file=sys.stderr)
        return 1

    with open(gcode_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    new_lines, info = process_gcode(
        lines=lines,
        filament_diameter=filament_diameter,
        round_digits=round_digits,
        tool_map=tool_map,
        bed_leveling=bed_leveling,
        skip_purge_for_already_loaded_tool=skip_purge,
        cut_tip_after_print=cut_tip_post,
        force_initial_purge=force_initial_purge,
        include_audit_header=True,
    )

    with open(gcode_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    if do_upload:
        client = MoonrakerClient(base_url=moonraker_url)
        client.upload_gcode(
            filename=os.path.basename(gcode_path),
            gcode_content="".join(new_lines),
            start_print=start_print,
        )

    print(
        f"[kobra_s1_ace_plugin] OK: {os.path.basename(gcode_path)} "
        f"(toolchanges={info['toolchanges']}, multiplier={info['flush_multiplier']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
