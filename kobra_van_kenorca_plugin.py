#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# [tool.orcaslicer.plugin]
# name = "Kobra-Van-Kenorca"
# description = "Kobra-Van-Kenorca — Vanilla Klipper & ACE Pro Bridge for OrcaSlicer"
# author = "Kobra-Van-Kenorca Contributors"
# version = "1.0.0"
# ///
"""
Kobra-Van-Kenorca — Vanilla Klipper & ACE Pro Bridge for OrcaSlicer (Anycubic Kobra S1).

Uses exclusively the Python standard library (`urllib.request`, `urllib.error`,
`json`, `re`, `math`, `os`) to avoid any external wheel or glibc conflicts inside
the Linux Flatpak embedded CPython runtime.

Features:
1. Moonraker REST communication (e.g. `http://192.0.2.10:7125` or `http://printer.local:7125`, with
   automatic port 80 fallback) & upfront permission declaration via `orca.request_permissions(...)`.
2. Live ACE Pro status querying via `/printer/objects/query` (`ace`, `ace_state`,
   `ace_instance_0`, `ace_instance_1`, `save_variables`) and `/server/ace/status` fallback,
   exposing slots T0..T3 / T0..T7, hex colors, material types, slot states
   (`ready`, `loaded`, `empty`), RFID SKU/Spoolman matching, and dryer telemetry.
3. Embedded HTML/JS UI (`orca.pages.PagesPluginCapabilityBase`) with:
   - Visual ACE Pro slot overview (Hex swatches, material, nozzle temp, status, RFID badge, Spoolman fill bar & multi-spool picker)
   - Slicer Filament -> Physical ACE Slot Color Mapping Cards (T0..T7), Auto-Match, Live Thumbnail Recoloring, and Pre-Print Filament Remaining Prediction
   - Maintenance & Hardware Actions (`/printer/gcode/script`, print-safety locked):
     * `CUT_TIP`
     * Bed Mesh Calibration (`BED_MESH_CALIBRATE PROFILE=adaptive ADAPTIVE=1`)
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
   - Pre-print checkboxes: Bed Leveling (`G31`), Skip Purge (`SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1`),
     Unload after print (`PRINT_END CUT_TIP=1` or fallback `CUT_TIP`), and Force Initial Purge.
   - Prepends a reversible Audit Header comment block at the very top of the G-code file.
5. Multipart G-Code Upload to Moonraker (`/server/files/upload`) with "Upload Only" and "Upload & Start Print".
"""

import base64
import hashlib
import io
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
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import encodings.idna  # noqa: F401 - preload IDNA codec on main thread before HTTP workers start
except Exception:
    pass

# 1. Runtime Scope & OrcaSlicer 2.5.x Embedded CPython SDK Import
_WORKER_THREAD_LOCAL = threading.local()


class _WorkerThreadScope:
    """Marks the current thread as a background worker so UI-bound host APIs are never invoked."""

    def __init__(self, allow_ui_apis: bool = False) -> None:
        self._allow_ui_apis = allow_ui_apis
        self._prev: Optional[bool] = None

    def __enter__(self) -> "_WorkerThreadScope":
        self._prev = getattr(_WORKER_THREAD_LOCAL, "active", None)
        _WORKER_THREAD_LOCAL.active = not self._allow_ui_apis
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._prev is None:
            try:
                delattr(_WORKER_THREAD_LOCAL, "active")
            except AttributeError:
                pass
        else:
            _WORKER_THREAD_LOCAL.active = self._prev


def _is_worker_thread_context() -> bool:
    """
    Return True when executing inside a plugin worker thread or any non-main thread.
    Prevents calling non-thread-safe Orca host UI APIs (`plater()`, `model()`,
    `preset_bundle()`, uncached `plugin.storage()`) from background threads.
    """
    explicit = getattr(_WORKER_THREAD_LOCAL, "active", None)
    if explicit is not None:
        return bool(explicit)
    return threading.current_thread() is not threading.main_thread()

# Embedded CPython SDK Import (with standalone/test shim)
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
            return os.path.join(os.path.expanduser("~"), ".local", "share", "Kobra-Van-Kenorca")

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


# 2. Constants & G-Code Regular Expressions
DEFAULT_MOONRAKER_URL = ""
EXAMPLE_MOONRAKER_URL = "http://192.0.2.10:7125"
FALLBACK_MOONRAKER_PORT = 80
DEFAULT_SPOOLMAN_TIMEOUT = 2.0
DEFAULT_FILAMENT_DIAMETER = 1.75
DEFAULT_PREDICTION_RESERVE_G = 15.0
DEFAULT_STARTUP_PURGE_LENGTH_MM = 200.0
DEFAULT_ROUND_DIGITS = 2
SLOTS_PER_ACE_UNIT = 4
MAX_ACE_UNITS = 4
MAX_ACE_SLOTS = MAX_ACE_UNITS * SLOTS_PER_ACE_UNIT
SUPPORTED_ACE_COUNTS = ("auto", "1", "2", "3", "4")
DEFAULT_ACE_COUNT = "auto"
DEFAULT_PRINTER_ID = "default"
DEFAULT_PRINTER_NAME = "Kobra S1"
SUPPORTED_THEMES = ("auto", "dark", "light", "kobraorca")
DEFAULT_THEME_MODE = "auto"
SUPPORTED_COLOR_SCHEMES = (
    "teal",
    "ocean",
    "amber",
    "coral",
    "forest",
    "violet",
    "cyan",
    "graphite",
)
DEFAULT_COLOR_SCHEME = "teal"
PLUGIN_SHORT_NAME = "Kobra-Van-Kenorca"
PLUGIN_TAB_NAME = "⚕ Kobra"
PLUGIN_FULL_TITLE = "Kobra-Van-Kenorca — Vanilla Klipper & ACE Pro Bridge for OrcaSlicer"
PIPELINE_CAPABILITY_NAME = "KobraVanKenorca_PostProcess"
LEGACY_PIPELINE_CAPABILITY_NAMES = (
    "KobraOrca ACE Pro Post-Processor",
    "Anycubic Kobra S1 ACE Pro Post-Processor",
)
KOBRA_ORCA_LOGO_DATA_URI = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAMAAAD04JH5AAABCGlDQ1BJQ0MgUHJvZmlsZQAAeJxjYGA8wQAELAYMDLl5JUVB7k4KEZFRCuwPGBiBEAwSk4sLGHADoKpv1yBqL+viUYcLcKakFicD6Q9ArFIEtBxopAiQLZIOYWuA2EkQtg2IXV5SUAJkB4DYRSFBzkB2CpCtkY7ETkJiJxcUgdT3ANk2uTmlyQh3M/Ck5oUGA2kOIJZhKGYIYnBncAL5H6IkfxEDg8VXBgbmCQixpJkMDNtbGRgkbiHEVBYwMPC3MDBsO48QQ4RJQWJRIliIBYiZ0tIYGD4tZ2DgjWRgEL7AwMAVDQsIHG5TALvNnSEfCNMZchhSgSKeDHkMyQx6QJYRgwGDIYMZAKbWPz9HbOBQAAADAFBMVEXn7uCSz9bp3aGflWaVn6CStsC+xqbIunzDu6LSwX0aJCocKjIkMzoUHCMTGBsMEhkFDBMECQ0hLTQmOkMlKSc4Q0kJGSNIU1hESUwbMzr8/PxETVIaOUMhIh0mQ0smSlUzPEM4SlQWfIQzOTs3NCd5g4oYSVRGWmQoWWUaQkpXY2o3WGUKKTQmU1tmc3sJIiyHk5kdIBsZWmZZanRpeoNTXGM5U1uZo6l6i5QmZGwoanQ3aXQ5c3sVgYmEiYsZaXQ4eYSosrhzfYOkp6lnqrMLMjwKOUUaUlmTmJooc3tGY2xHanVGeYSEjJPl6OkYc3tapKpHiZM3YmtISjpQWVtUi5cpeIQ5g4o5lZpEc3lJhItzvMXS19hInKRUk5xamqS5xMoKR1NHlZpIpaliZ2libHMMQE05iZRCOypZcXxVqbJrs7uLm6OyuLnW3eHd4uQbZWxWV0VTeobDzNHK0dbt8fQqgok5mqRYt7l2xM2WnqOZq7Onq7GzvMJpZUl7ytORhVbDyMr49+hJQixtgYloipdkmaVsusNzeXuD0dqZkmjVzJo3qLZkpKqFqrOF2+OVk3WousPNxZjJxqjm26cOa3YMcXsnMCswLyQ5oqlJqLNWSTBSgotpalhq5eJ0cFd1d2h+kJh4m6h609uHhmiom2eqo3iqp4q0q3e+ydANWWYehJYoi5gmkJs9RDw3s7o5wslItLVEvMdLytNH2+ZTVD1XuMVscWVnlZ16qbRztLqLinSRlIias7qmw8mqzdK6tIO1s6u23eLCuYjJxIzN2uDn7PEKeIQsTGAql6A7t8VGoZ1RRi5WZF9Yw8Rc19ZoxMdtzNFo19V2cUl/2eJy6eabpZyinHOlpp6qsai9tn22spO62d625+vM9vfZ1rje3szm4p3n5bbz6KkTLkAfPlIan7Act8gvqq4npLcivMo/Qi8xTGA+n7I1rsI/yNBBPTNLUkVNZV5GboFFsK5Bv9FOycpVztBpWDRlYDx2bkx8iH9woq6Nfk2Pg1aOkX4g5+l8AAAaO0lEQVR42r2bBXwbR/bH1d79aXZWS9pdqdosWBGjLTPbsWOKw8ycCzdMTZmv3F577b8M1/aYmZmZ+e7PzIxvZnallSwF2vvcfJzYVrR63/m9N2/eQAJCUAgGeX9DDRrXsEUi9O/G/zj3Q+CTfWaIXUEQAvUAqCFAYwKZu0Br+DH1AEEGwL9CgAtBoIsBgH0XgLRX6INXpQD1AAXgLwEA/abtE4LLAkC/WQBqFTwAAIygyoBeEUAEmhfp6OKDgPq9CkDMvyqACBmQSnbR5lvHxraMLbsEAfg5ABUCdPlRiCOctX7DHdft37K+f/l377XQRbNAvQK8fyA0lKCZbUUh6chaeseG4cTCXE4TV93G8ZcwCBkCDUKhfiBeBoCST2VThrHl3qW7x1OLFqWM3Nh1EV//a0n4OQr4AC48AijAnJRjWKlUdnlq2XP7EwtXfWhVNrGwuP06g6TmKnSzPOyaBwDyg2sfNW+VnCd7GCgC5q1sKrX/3qlt26/4bzC/cLz/3kTE8ADmjEdfCHpZgAFcmn1mXZZdBBPMRywrdd2GbUt+dsWwlVi4e3fiu/1GOuIC1CaE2hD0AwjeVHBR+zLHzMNXxATfK0ix+p7brK3/+af7LGvh7sTCTbelrbSrQF1K4muzoFZ1wcWc7+lPey7Tb8T8FyN8EKW23NsvfvBnt6asfGLhtsTEc8OJCgCqTFWN0nA1CgMXNc8CUMYYy+wTFStlGYaVXXfrHRss7Z5PzBimlQYBtk091zuRqFWgCsA3Agh6ADXW6gYPSzW0kY8D88ObxzZsuO22sc0pLfexj00rEcMYBwG27fnW6qm8UQtQIWhivw6g0Vzi9Z8AyIqSXXXmzNhwIqdpGvyTYeYU0zRyOVBg96bTbaunUmmuUQxQhGooBP0KNM81lejHblPym68bS2kC3jnY9f3J0mB8V9KJFDUuN77QgBjcl55I9fVZtfYB35+PaxIhfAXqQs0LtJqixzOfWr9hzJDw4MGDk6PlTsc0LcdxOrOpRK6YA4Bl+9KWYeazWYXzxeDcqdEvQTDg7z5I7arNnmRh574Usbac2bBcUheMxJOKoShGBHxvAkF2eh1k4fHxhb1nIiQClGzW4VBt1poLwNcCuI6uaI3lqoCMgLM2bDFErTRSNgzFsXKaWZgtFRwFQkBJ7p1ZtSyVy902lgaizk4nqyC//ToCX0kCAD5D2N/kqn3Cxpln+iSsDrYbpINYVEauIi0O0yE4IulYtyzdZBTH7ujrdBTMOWodACCEGkxLwBDw7OC6RsOB9Z80NLZI/EhH0vnjZPctDieqi6n9Lr5IP1Arao7a//4eYdUdJvlddbgLAvimhUAT+4TAC0nwDHJuE5/f+Sd/bSRL3bIgytT8VbMiDGStGDGtrGUoOP/+YXHZGfL5io5RPUCT0iBQO87qIVwJMBrrC/6flc1a8ZJ8+6QkLzh4w8aDugjJADKTYznZvkWrbj3a9/7V0uYxDSGsK6h+1dCkPglcyD5m5jmMVo1JTxSy2WxyVo1epUuCJEmiJGkCEGiCSJsWmVm15T1t4obHBYRYENStXxoSBBrrXwWA+Mdo+grtyH99MZt0VE60sSiIfGaya1KHH8iEptEvQZS07L535yJ3KDyyVa42F8nNNAhcyL7bOO7Dxt9/1JlOOpwG9R4nSAImQfi5aRFpouA1zEtS5LFT0qotAsJRFdfYbwyAKMDF7GOU3SJ9pHOmrACrwgmavuDwYgDYeH+7uLdd5137olxwBDH3NwZ3HYfUaFRXsb+SaArQ0L6iVL2AbfXoquBTnMNhBSNR6LiBDYGtHzvH4+5Cd1mkIoiChEtlTUz3Snevi+lRNRYmKiA6mKGXMtc4CgKNO10lUKJ6NDmmxcDvSBNQ4TjYPvH5w8fuv/8sr8Uz5XjUllkUCmKmlBHElNb3YdVWVYQYgi8KGhUbTQEUymBHdZCS++AiKZZR1UwXKL/40LGt9490zUYl1O5E4+2oUBBcAByPqxLMB1eoKAYASLbDOlOBmxsE7q8XAMCKqlPz4Iax5eLtI8c+97mtXd2FjB61Q5qI9Ixs3xyXo93t7kAUhUK8oGlGbgPmcZhOwnIsrIexjOpSAZvrLwag6h26ylzBKWPrNJHnmSWJZAFkK0q5VJDVeDfIDcYV8MSu+KAiFrX9ZlAOY2ZLxhShAsDVFcyBZvaJeacai/rmsVvXr5tZdFRccV8SkUxgd4wWorIe79blA7YoqrNxHRXis1FRE/abPBeOed2VaSzI/vUSMQ5OuRCA2kG6r7jByKm6rCxft37z5v3aTZ9p5wFADMWQrLeXSjoaLSVFMRMfjcfjo4OQnUABHpEorCCoUT1WV2XKbqkYYENtzhhQV4ICUdUjCOuY+NTg7rZaCQAQhD4w2l2K77LV7u5uRAFG4QsARG5DBCE77NkLgQLhqN0UQJbluQBKdKXu6B0dUTcIdJW9Bz3+uPTCRxWJRFyMVDB2+ZnSLFZ5MTMYJ20wKoj5/fDJbhSSkRANx8JhlaLMWbNBIpo7GUH8K/rKpErigMWhrdscqxF69ovv+0nGzqiieOdke6H7QNfZgpzsFhiAmplVBXHZFpgQWRRiFWTESI5SAPQIrQrcarcJQIzlABiAOglFR18JI1HViZvkyAQ29mWlN90+OjqKxODK+OTk4E4VFw7ENTEKAKN41ywWtf1rJwhADFSAEUuEUMMqEyQkVwYC8gBoxYWVSu+ZBCtVp4MGoeLoOmQj+vLQgIWGPyy+78XC7XEs/c8TkO9UfbardKBD1LidhCDeoYnWdS0tV8N0EPbMo1A4hqPesCTO5yqVOlFAbhiDqr0yqrAG+ShKHDAxMDCEE6dBgsHbIeVd/8v7R0bOb335XBxCgJejs4Px0YygiVsee7hlSSQE81HMtROLyjIAcI02LeZOx3TsOTrxggvg0ISI8dBAy4DJrR4Tr31xdNAW5u8aueHY8RtGMigZlTgcLQ0O7oLpWTnT0tJiIFuPViI/HIYosBvv2gS4RoMAR3VQwfUBSYgAg9PwwQNXY+NuM/jJ+KA2/6FdKgS4Gk2WziEJ447S4KytaeLYqYcHJqAgiELksFwgRyH6w+oFAOoQaAxCHDAJwD7AwJ+2ASBoMdG69dKbfliQ/jdw4tDWg+ePHx4ZiUuainfOTnZoEWndN9/Q0sZB5gLJgYA4wSYBqIYbL70D7rZHnQa6A5HorCTx3+EQn6hYWTJAJZCdu8XnP6VIP7jq84tPnPj8iZfPbVUl7Cgdkzs1QzTPDLQMRaAqVclIs6NhMgZVFgdNABqVxTb4nHTbAe0dNyFbLawpyt3aU4PSkdhH2dpgpBuq42hUyezUxiEJrn0YAkAOR93FHXgiptOM4A2DRgpAYqglUAkApEMSfaw0UDyAAROPFY/g4B9t7Ah/7fb4qI5VUVKStm0LEUEb2/RwS4Jo7xUiOMySIAuEJgAyHZ61AAorhkgoePPB1dQFLaa5FPHSndD3xSOlUik+UpD4pCrzWloQlu4gAUAdQDaPyVkKhiRExQ83HgYBb5qsIaAApBxx3NIEPGAqS5gLrC2auOIfNrLK8PCha2AqtpGYS2ja5k1vIAEADsARtxkcJCSalZsMA5KIWH7wA4DnIBXoapQqQF8aGsJ5Yn8Jt269OD8w4hRGNi5efEMpDkPQ0aRcQtCW7hhassRgDiBrd9IioAamc2GTYRBgGxihUI0EMIHQYlClEpBXzJaBPM1EV+eXctJDZGG6i8xUUUlCtiaOpyVu6SmQCAIATBLzadoMEJPUA6psNx4GoACkSC4U4moBojT30XxAW9vAwBKFOGFqqSW8Pc70v+rY8Q5JgGIgYUjK0j3wr208WxVFjHSCtLSZ1HEEogIyVhg3G4bu9oxvFMLblcpwoIlhCek8OGHJvuXSfTe2d8bPHwb7W8E+rAjAfnapBR5Pm539UDatX9SfSqQTE/l8IjXTSYIR5uWwD8C3MRZgk2RtJlB1kj+8jERago5AC7ft6JeevhFWRaVyJhmNQnEoiFqiKPUvVfhIfv3SpZuWDfdOrdl0+pvfOr22d6KtrW+dRXdvIQZ0u3Zd7ANA9emYOo7+5DAf0DQ8sCSyaLsoLXD133iM9r/YpkmLNkVS2/dtT+WgUGqd1xoUtFx6+PS3v36yZ31fIj2RJhKEw3Lt3oAfALn7IJXlGBAwTcwkjQU2AltWL9XmP+iMHib2TyweFZl9Yf3aoWX7hnNS65HnX3rg9dA+88k7j6BiYsf3vr4mlbcmLCOi6K4HuMYAHoK3L0FqIEaQ7YS/YCIcIBrsSAcfvCoQ1zPxrmu6Ckx/QVt2au2OPk0Mf+TF+57euXNy8uyCBYP3PfDAYGcxffrJkxMQDhFTdzMxRBzfGMBlcHcFEGhPCWRrucJho20IEL7waJ+kU/Vv6BrNcJIG/U8IwrLHdgwXJTlT0G3yCYMLzk5CLess+NTBkilMfW9twjJMXTc5rxppDuAisGUkdjoxKUSV5fkIBGFb29DQ2zaJK/RuNwJGBF4TtQlNBPEjktzRodJ9PhmWISqZzaKjC6AVtIkn16bzSfAoV+sA3q2PawEq26Rk+ursVMB0JL8cvqXboO2YEH55Y7kzfnDj4a3XIA2WYomcuGx7ROL1DhvLmM0qiCf9sKOZBV1dCw46YvrJNdPTJtvBjlwAoPJKpWJWsllCYPXlMfigrW31dnGQBH+pUE7qmqDykjEu5VOiYDuYJ8Z5ga4dBYEijF5DJFCLYs+T6zrJDirijAiqB5ADDQ4HvO/m8qwJmbi311CUxFDbDlMqufofnhU5DAEAyzAJwbqPl5G3V8UakpE6WOq6ZoGCkPCu04YJAJEJQlFDAIoHKtvYoXqOSMRavtxUlNQwRFGi7eR28akjLAefOIZEmRcnYNiTbQsIbFRjnhLISjIZL0B3tPQ3UjApRCau9gG0BilCxQVzTk3ocazZ199nmfnhXiNitN3VI/3ixg/ohdLk7C5VRAoVQBDIsYHXe6GKIcjYTiZVB3OoqP3zqTSko7ZEFcA9KQM79QCPVPtvGKn+vv7+vDV1sqd/7aZN2grIwYEflQZH47zA8WKiSDeHYDCQtSqxLkmigDj4LopBSKdJaAoooPX+W8KCUZSHnAxCV04NgaE6DD0AuWpfsfr6rJ41/b2PvvtU70JNg0mIVYEnuiQN81pCdLenWJNETd2V6ThbctoVcMHbZXV6puwoHK/lEt/oaWtp6cmnpxLUlHdk5gfwhgHMy+w43DDN1HCPZfXc9d67tmnC/P9cIX2AASw+tFLibT6XJv0me5VMc90pd5dtMhg6DtjS/DDW995CFOC18d3/enLoLVM9efPqNAoR/dn9lSBfA8BECMnMvmKaVv9wKp/fc1ebsOLO1z/4dKh1hX6cDoESrMSgBhv33E3+KPE4RqCCkIsUJbUsXas6yVufTXZynFY0dr/rrres7k3lTZoH2Fkt/MX2iusnSGIe8p9pWak1w6mePT1i6KXX//5rVyJx/h89beuZDFRBggZjPperRBwHUmNwQbrNVNesXZPStNBrlOTjVxzNKgoCD2x7x5ff8ta3gQsMCiAwBYJ+gOrIpFMQOYXI9+/p7VmTEv/jyp9kYjCTiCt+vXXjwcnJa7YehEyDxVzRNc8r7VCZiwj3TE2Z4XXbd5xG0mt2Zmd+es/RLGSf4sK2N7/7y297x6MpK+0qQC8O1ABUJwe2VncF6FndI37ngR9moJ6FWDvy63PHTpBU2CXyPLiWAXBJlRfAej6P871TnSu/tuNdaen6H2RnPvHTRWAfFXPbhlr+7p3v+VJvfsJUgMAXgi5AXXLmiH0qwFRvn/ZXd5Zv1rFGQu19D732/KFDhw9tPSvB6Oc0cIGGuU5MOm+3DV1tmr1T+j9+5Ss56fofO30fD3zoaBZzxeLCN7/1T9/7L6fe2pPPsxmBZyHoAdRMjvRwwI2A4TW9vQnxWnXvXidMCj/xkQdHRjZuPHToeJcEXUZaURAcTDTIWzBj9Pb2qJ1r1mzvEYM//qy5/BM/X7bKgt4Ux7et/ts/e+9jwz15WFlgTDKRe2xdAahmAXY8wzzQt6y/tzeHYkr5451H5ksaOCHW3nV88eLjcZukWg7RDKBBqZCCJ+S2odXDy9b2CNK1v3rRPBr49KpFMxGFy40n3rzvL/7yPasgnzEAlgsJA+s2BagK4C7PTahuh3v39BQFpDq/8yx+6nVgq/VNK2LRTDSmnrXBBxAFxH5iCDKcOdSb7z+19mSbKK347K+eMG759D19/2SSABxPbGvb94V3Pjoz02maCqt2vFzEjAb46hCQawHW7ElBycFx2XtuUa+//qag9MkHXxufLZ3f+nIGxjt5jjhGG0+kenp72hLjohS89rNXPo+V7ns+tOjoDNE/Z+xO9LW94Z2LpgHAXWPK1aqITka++1OVM1IAWL5u0fBqC9I8Dyn12VuUJ154Qbr2d7u7Xj50/PxIiaQe8jQfWjE/yLaPg/OvfemhK38xmUnu3WvB+CfZbOH4eO6DfQu/tCc5nZmuAPj2zbkagMraKAKDYKa/vyeVIPMMz2Nl5qjC2ULoge7u0vkbzneNqBKZh+EBIPjO9de/5oWXfu8Prnzoyk8t6FowMlJWHAVhDpZK4zmt89nxoXdMWMlMxjRN35lkZcfSBxCqrAwMxczCPNib52ii4RzF6VRkJN35o+7SgZGRri4YekQCRA5DEI6tWHEkvLJrAdQfC64Z6VI1TkC81vnt1UVt7zQ3/udTnTAtOkr1EIQdKLItjBoF3l7ZJAINsn3D/RFWYxFnAYAY3NleglaQMzqMRIE+pKpZS8laRrLcPho/2zVZwDzmeF7QODWnxc8VtZPbM5lkp6nUHcvK9QAI+ZanEYqQGs5rIuJFd9qFbBQKy7aNcaHrgCwJlAxWEJySVNSyYyRhBSEjWh6R4zxNjHSeM7UlazNJx/R1n22OV86EA9U7NL61GTmYN610qs+A4lMmhYab9r/fnim0P1PqLpENckoABShBJyBIIdrTYw2CoB8wOa1lLVNfaXYq2hiASKCYaWsmb2gib+uySCEkCXV13xy/OR6PH9glsTpIY+OHpyW/xvSiRWHy4xlJ6z1JjtYr9n0r0EYKuHcX5AiORDC5omBN540IzDk4o0QzSYwcR5QPdLcDQHeBKzisFAAGDcIB/viKI9ReLuhSrrdHUdSVan3/ZSxXjsYDwco9KnZVxz3yphs85rRFCnqNhLya1FGmXZVQe3u5nLG5cnd3GVV842uSBG8+1wVPJVansFrZYqjXPuIp4AOQqwDk5k5ESZpQm1kmlDUiPa5mqmNbdgrthfaSauO6chzeISdn9fYCL+ZSU/0OJH9btyNzzXuN8wG4C2T/bT2y32nqZBYla5pikR6WS3z5mW6wX8BKvL3gaKhai3NqRil1FQReEoxUOqKS521YF+ImBGQYVq7yeStD94yViqCrSGGbrqrqnTBooIacKcPL9t5yoR3rhUwZy0k948jt8WeiTgZDVEQSFn0qaiNbrz2T8hnnagCYE7ia0zVwH7NPEk6lciMIIHX55vayLqvlciEp74XIwGX4FfwkcEbaUmV6MqNGsR1l6/zq2r9ins6GNZcZ6w54uSg094gTR7F3CwleoGshqIoEJzOdxFpy2lEgKZDXiuRCIfb2ZblwWPUAKtEv+49RA/7LpEiu3cojAJWtLTWMyNVXjWx4ETXcqxMkPbiLYi2Xo6tgniNvYJ+Jw9Ew246Xa8xX+hloeJ3Te4EcSKgxcmQIv6g2eYmYx4gPhTF1BmvFYi5HglZVbZgHeDuMNDa72eRMg0Ny/SUt3z6hPwbm3HGhn6DrgAEUsTBGsXDYluk/xcIh9hD54lQmFFajUVXG8B5qOgoOsOnBETPq2pf91zwDwbkE3rVLdtuZdYRsOZMWC7GL4EEUjlUvAxEad3Mk5r6RMNOFKF0BVnZg6i/UkKK0eqvab19giwd24YoKimNq2ObdYjYoYBoTrqti7sNkpxZEIqa92tddhyN33UPt89X7fQG+9mI58t98r3yE+xOElCoHBXYZzidBLFx1GphHvOBvNQqz/vNVNwfcW21z7jsKDVtIDccQuwkNEoToYwTFS2bQfZnelGuA4O1T1t6vDHj3+mr+8wXTv1EjYSiz/x2BwjZzUIw5IwgCAV6Q5+c8HKwKUGMf+QAqFE26X7kvBAnBRnR9C4YFChIjIAJ0Xw25QdMUoHY3gK8HqF71nNOHkBdUQhA6iombwbLHwZP4COOg4H1AcJ5YhQ7Wb0G5v/LNAITKzkul//P+PVjjBzUkMNMUgwYH1UXwng/ST6gPAd9Nc1QXA1XrPvtVAp8q5EcI9hjPbFMBSGRUuu++VfQ0aACA5gAIjQQQgmIDZ9C3EJMhkgt4gKBjo6ELCUQlE1wqgC/mgn6jdY1YDYVtSIIxFnxNwshvPxRqCjDn2WBTy15Dlawbq9zSDc2r/6DKMpiYJ61C0Rzg0lormGP2fd0PeT/PrxnctO8hX/NdaLxMq62t87wWCs0D9cEPvpeCQfIN3uhTwt1/bACALh3AMxyqb7HYnJdC8+bNb/ULUDUN03G9BJcA0Dqv3vYb5zdufgYiRCumz/vMewS+S62X3XfX2Ote97qbbvrqH0L76k03wS/uy2/0AbS2PjIXoCqAe6/4EoKNITQUoGrZb96zXw3NqhNq+08BKjn+0qIvdKHmRWNra83DaG4EevNeQGiS6y7EEpzXpLU2eYT38oDvf9x4t+vrz1ouHeTyGl8p2vz/u6AOQGqMEnxFKHNFrbfN/quXt6aW6E1NqdGCWxCFV9LqehJ0axU271TeFXDviNY0sQmJ7yPr3SVepFWm0frWEOAyUC7Qaj5HdH05F0C6hFb9sMswXUMgegSvDKDKcfl9rwEQXh2AdDEdJPFCjzSO1FcEMDdKXBsXhq6r8hn0qwa4HNkajZffHkATx/0WARoT/D9PujKZMShqggAAAABJRU5ErkJggg=="
)
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
FILAMENT_DENSITY_RE = re.compile(r"^\s*;\s*filament_density\s*[:=]\s*(.*)$")
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
ACE_SET_PURGE_LINE_RE = re.compile(r"^\s*ACE_SET_PURGE_AMOUNT\b.*$", re.IGNORECASE)
CUT_TIP_LINE_RE = re.compile(r"^\s*CUT_TIP(?:\s*;.*)?\s*$", re.IGNORECASE)
SKIPPED_G31_RE = re.compile(r"^\s*;\s*G31\s*;\s*skipped by ACE Plugin\s*$", re.IGNORECASE)
REV_META_RE = re.compile(r"^\s*;\s*@ORCA_REVERSIBLE_META\s*=\s*(.+?)\s*$")
ORIG_TOOL_INLINE_RE = re.compile(r"\s*;\s*@ORCA_ORIG_TOOL\s*:\s*(\d+)\s*$")
PURGE_MARKER_TAG = "@ORCA_ACE_PURGE"


# 3. Internationalization (i18n — English & German)
SUPPORTED_LANGUAGES = ("en", "de")
DEFAULT_LANGUAGE = "en"

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        # Header & Theme / Language Controls
        "header.language.aria": "Select interface language",
        "header.language.en": "English",
        "header.language.de": "Deutsch",
        "header.theme.aria": "Select color theme",
        "header.theme.auto": "Auto",
        "header.theme.light": "Light",
        "header.theme.dark": "Dark",
        "header.theme.kobraorca": "KobraOrca",

        # Executive Status Strip
        "exec.dryer.standby": "Standby",
        "exec.dryer.active": "ACTIVE • {temp} °C",
        "exec.dryer.remain": "Remaining time: {mins} min",

        # Safety Banner

        # Section 1: Moonraker Connection
        "sec1.title": "Moonraker Connection",
        "sec1.url.placeholder": "http://192.0.2.10:7125 or http://printer.local:7125",

        # Section 2: ACE Pro Slots & Dryer
        "slot.no_filament": "No filament detected",
        "slot.sku": "SKU: {sku}",

        # Section 3: Color Mapping Workbench
        "sec3.title": "Color Mapping Workbench",

        # Section 4: Print Preparation & Hardware Macros

        # Section 5: G-code Post-Processing & Moonraker Upload

        # Section 6: Flush Volume Monitoring & Post-Processing Audit
        "sec6.title": "6. Flush Volume Monitoring & Post-Processing Audit",
        "flush.matrix.corner": "From \\ To",

        # Activity Log Footer
        "log.title": "System & Activity Log",
        "log.clear": "Clear Log",
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
        "host.reset_done": "Color mapping reset to 1:1 default.",
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
        "gate.block.no_info": "No valid post-processing result available.",
        "gate.block.multi_incomplete": "Multiple extruders used, but flush matrix/vector is incomplete.",
        "host.ready": "ACE Pro Plugin ready.",
        "host.sync_none": "No active filament presets received from orca.host.preset_bundle().",
        "host.status_updated": "Moonraker & ACE Pro status updated.",
        "host.printer_switched": "Switched active printer profile to '{name}'.",
        "host.printer_saved": "Saved printer profile '{name}'.",
        "host.printer_deleted": "Deleted printer profile '{name}'.",
        "host.printer_delete_last": "Cannot delete the last remaining printer profile.",
        "host.hw_busy": "A hardware action is already running — please wait.",
        "host.slot_active_unloaded": "Active tool unloaded (ACE_CHANGE_TOOL TOOL=-1).",
        "host.slot_full_unloaded": "Slot T{slot}: full unload completed ({cmd}).",
        "host.slot_assist_on": "Slot T{slot}: feed assist enabled ({cmd}).",
        "host.slot_assist_off": "Slot T{slot}: feed assist disabled ({cmd}).",
        "host.slot_fed": "Slot T{slot}: fed {length}mm (ACE_FEED).",
        "host.slot_retracted": "Slot T{slot}: retracted {length}mm (ACE_RETRACT).",
        "host.cut_tip_done": "Triggered CUT_TIP macro via Moonraker.",
        "host.bed_mesh_done": "Triggered BED_MESH_CALIBRATE PROFILE=adaptive ADAPTIVE=1 via Moonraker.",
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
        "host.profile_changed_abort": "Active printer profile changed during upload check — upload aborted for safety.",
        "ui.brand.title": "Kobra-Van-Kenorca",
        "ui.brand.sub": "Kobra-Van-Kenorca — Vanilla Klipper & ACE Pro Bridge for OrcaSlicer",
        "ui.conn.connected": "Connected ({model})",
        "ui.conn.unconfigured": "Not configured",
        "ui.conn.offline": "Offline / Unreachable",
        "ui.notice.unconfigured": "Not connected to your printer yet. Open Connection to configure Moonraker and ACE Pro.",
        "ui.notice.offline": "Printer offline or Moonraker unreachable ({url}). Check printer power and Connection settings.",
        "ui.notice.btn_open_conn": "Open Connection",
        "ui.notice.btn_retry": "Retry",
        "ui.exec.conn.unconfigured": "Setup required",
        "ui.exec.conn.unconfigured_sub": "Open Settings → Connection",
        "ui.printer.standby": "Printer: Standby",
        "ui.printer.state": "Printer: {state}",
        "ui.header.refresh": "Refresh",
        "ui.safety.banner_prefix": "Print active (",
        "ui.safety.banner_suffix": ") — All hardware, motion, load/unload, and direct-print commands are locked to protect the active print.",
        "ui.safety.badge": "Hardware Lock Active",
        "ui.safety.locked_text": "Active Print — Hardware locked",
        "ui.safety.unlocked_text": "Hardware unlocked — Active printing, be careful!",
        "ui.safety.btn_unlock": "Unlock",
        "ui.safety.btn_lock": "Lock",
        "ui.exec.conn.online": "Online ({state})",
        "ui.exec.conn.offline": "Offline / Ready",
        "ui.exec.tool.kicker": "Active Toolhead",
        "ui.exec.tool.none": "None (-1)",
        "ui.exec.tool.sub": "ACE Temp: {temp} · Fan: {fan}",
        "ui.exec.tool.temp_part": "ACE Temp: {temp}",
        "ui.exec.tool.hum_part": "Humidity: {hum}",
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
        "ui.sec1.spoolman_label": "Spoolman URL:",
        "ui.sec1.spoolman_placeholder": "Optional e.g. http://192.0.2.10:7912 (leave empty to disable)",
        "ui.sec1.spoolman_test_btn": "Test / Refresh",
        "ui.sec1.spoolman_off": "Spoolman: Disabled (optional)",
        "ui.sec1.spoolman_ok": "Spoolman: Connected ({spools} spools · {matched} matched)",
        "ui.sec1.spoolman_ok_conflicts": "Spoolman: Connected ({spools} spools · {matched} matched · {conflicts} conflict)",
        "ui.sec1.spoolman_err": "Spoolman: Unreachable ({error})",
        "ui.sec1.meta_initial": "ACE Status: — | Active Tool: — | Temp: —",
        "ui.sec1.meta_live": "ACE Status: {status} | Active Tool: {tool} | Temp: {temp}°C | Fan: {fan} RPM{print_label}",
        "ui.sec1.meta_print": " | Print: {state}",
        "ui.sec2.dryer_stop_badge": "Dryer: Stop",
        "ui.sec2.dryer_active_badge": "Dryer: {temp}°C ({mins} min left)",
        "ui.sec2.dryer_multi_badge": "Dryer: {units}",
        "ui.sec2.dryer_unit_item": "ACE {unit} {temp}°C ({mins} min)",
        "ui.sec2.unit_label": "Unit:",
        "ui.sec2.unit_all": "Both (All ACE)",
        "ui.sec2.unit_all_n": "All {count} ACEs",
        "ui.sec2.unit_ace1": "ACE 1 (T0–T3)",
        "ui.sec2.unit_ace2": "ACE 2 (T4–T7)",
        "ui.sec2.unit_ace3": "ACE 3 (T8–T11)",
        "ui.sec2.unit_ace4": "ACE 4 (T12–T15)",
        "ui.sec2.unit_select_aria": "Select ACE Pro dryer unit",
        "ui.sec2.preset_label": "Preset:",
        "ui.sec2.preset_aria": "Select dryer temperature preset",
        "ui.sec2.preset_custom": "Custom",
        "ui.sec2.preset_pla": "PLA (45 °C)",
        "ui.sec2.preset_petg": "PETG (60 °C)",
        "ui.sec2.preset_tpu": "TPU (50 °C)",
        "ui.sec2.temp_label": "Temp (°C):",
        "ui.sec2.dur_label": "Duration (min):",
        "ui.sec2.btn_dryer_start": "Dryer Start",
        "ui.sec2.btn_dryer_stop": "Dryer Stop",
        "ui.bar.btn_preparing": "Preparing…",
        "ui.sec3.title": "3. ACE Pro Live Slot Bay (T0–T7)",
        "ui.sec3.title_dynamic": "3. ACE Pro Live Slot Bay (T0–T{max_tool})",
        "ui.slot.badge_active": "Active",
        "ui.slot.badge_ready": "Ready",
        "ui.slot.badge_empty": "Empty",
        "ui.slot.badge_shifting": "SHIFTING",
        "ui.slot.no_filament": "No Filament",
        "ui.slot.unconfigured": "Unconfigured",
        "ui.slot.rfid_detected": "RFID detected",
        "ui.slot.remaining": "{grams} g left",
        "ui.slot.remaining_pct": "{grams} g left ({pct}%)",
        "ui.slot.weight_empty_pct": "0.0 g left (0%)",
        "ui.slot.weight_missing": "Weight missing",
        "ui.slot.spoolman_conflict": "Spoolman conflict: {count} spools match SKU '{sku}'",
        "ui.slot.spool_auto_last": "Spoolman · Spool #{id} · last used",
        "ui.slot.spool_auto_fb": "Spoolman · Spool #{id} · fallback",
        "ui.slot.spool_auto_single": "Spoolman · Spool #{id} · auto",
        "ui.slot.spool_manual": "Spoolman · Spool #{id} · manual",
        "ui.slot.spool_select_aria": "Select Spoolman spool for ACE slot T{tool}",
        "ui.slot.spool_opt_auto": "Automatic (last used / fallback)",
        "ui.slot.btn_load": "Load",
        "ui.slot.btn_unload": "Unload",
        "ui.slot.btn_full_unload": "Full Unload",
        "ui.slot.btn_assist_on": "Auto-Feed",
        "ui.slot.btn_assist_off": "Auto-Feed",
        "ui.slot.btn_feed": "+Feed",
        "ui.slot.btn_retr": "-Retr",
        "ui.slot.title_load": "Load tool T{tool}",
        "ui.slot.title_unload": "Unload slot T{tool}",
        "ui.slot.title_full_unload": "Full unload slot T{tool} (ACE_FULL_UNLOAD — requires confirmation)",
        "ui.slot.title_assist_on": "Auto-Feed is ON for T{tool} — click to disable (ACE_DISABLE_FEED_ASSIST)",
        "ui.slot.title_assist_off": "Auto-Feed is OFF for T{tool} — click to enable (ACE_ENABLE_FEED_ASSIST)",
        "ui.slot.full_unload_modal_title": "Confirm Full Unload — Slot T{tool}",
        "ui.slot.full_unload_modal_body": "ACE_FULL_UNLOAD retracts the filament completely out of the ACE Pro unit. You MUST be present at the printer to hold and wind the filament onto the spool — otherwise the spool can unroll and tangle!",
        "ui.slot.full_unload_modal_confirm": "I am at the printer — Full Unload T{tool}",
        "ui.sec4.btn_project": "Slicer Defaults",
        "ui.sec4.btn_automatch": "Auto-Match",
        "ui.sec4.btn_reset": "1:1 Default",
        "ui.sec4.summary_ready": "Ready",
        "ui.sec4.col_extruder": "Slicer Extruder",
        "ui.map.mode_auto": "Auto-Match",
        "ui.map.mode_manual": "Manual",
        "ui.map.mode_default": "1:1",
        "ui.map.not_present": "T{slot} (Not present)",
        "ui.map.badge_invalid": "Invalid",
        "ui.map.unused_badge": "Unused (0.0 g)",
        "ui.map.sum_err_badge": "{count} Mapping Error(s)",
        "ui.map.sum_warn_badge": "{count} Warning(s)",
        "ui.map.sum_ok_badge": "Mapping consistent",
        "ui.map.usage_info_aria": "Detailed filament consumption breakdown",
        "ui.map.usage_info_heading": "Filament Consumption Breakdown",
        "ui.map.usage_info_print": "Print (model)",
        "ui.map.usage_info_purge": "Purge / Flush",
        "ui.map.usage_info_total": "Total required",
        "ui.map.usage_info_ext": "Extruders",
        "ui.sec5.opt_skip_purge_sub": "Sets SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 or 0 on the G9111 startup macro",
        "ui.sec5.path_placeholder": "/path/to/file.gcode",
        "ui.sec5.btn_parse": "Read Filaments from Header",
        "ui.sec5.btn_preview": "Offline Flush Analysis (Preview)",
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
        "ui.flush.kpi_g31_cut": "SkipPurge / CUT_TIP",
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
        "ui.nav.settings": "Settings",
        "ui.nav.diagnostics": "Flush & Audit",
        "ui.settings.title": "Kobra-Van-Kenorca Settings",
        "ui.settings.tab_connection": "Connection",
        "ui.settings.tab_purge": "Purge & Reserve",
        "ui.settings.tab_appearance": "Appearance",
        "ui.settings.tab_language": "Language",
        "ui.settings.tab_paths": "Paths",
        "ui.settings.tab_diagnostics": "Diagnostics",
        "ui.settings.conn.save_only": "Save Settings",
        "ui.settings.conn.ace_count_label": "ACE Units (Offline / Pre-Connect):",
        "ui.settings.conn.ace_count_aria": "Select configured ACE Pro unit count",
        "ui.settings.conn.ace_count_auto": "Auto-Detect (from Klipper)",
        "ui.settings.conn.ace_count_1": "1 ACE (4 Slots · T0–T3)",
        "ui.settings.conn.ace_count_2": "2 ACEs (8 Slots · T0–T7)",
        "ui.settings.conn.ace_count_3": "3 ACEs (12 Slots · T0–T11)",
        "ui.settings.conn.ace_count_4": "4 ACEs (16 Slots · T0–T15)",
        "ui.header.printer_aria": "Select active printer profile",
        "ui.modal.target_printer": "Target: {name}",
        "ui.settings.conn.profile_label": "Printer Profile:",
        "ui.settings.conn.profile_name_label": "Profile Name:",
        "ui.settings.conn.profile_name_placeholder": "e.g. Kobra S1 Studio",
        "ui.settings.conn.profile_new": "+ New Printer",
        "ui.settings.conn.profile_rename": "Save Name",
        "ui.settings.conn.profile_delete": "Delete",
        "ui.settings.conn.profile_default_new": "Printer {n}",
        "ui.settings.conn.reserve_label": "Pre-Print Safety Reserve (g):",
        "ui.settings.conn.reserve_hint": "Warn as Low when remaining filament after print + purge falls below this reserve.",
        "ui.settings.conn.startup_purge_label": "Startup Flushing Volume (G9111, mm):",
        "ui.settings.conn.startup_purge_hint": "Estimated filament length (mm) purged at print start when the target slot is not yet loaded (0 = off).",
        "ui.settings.purge.llm_summary": "Calculate exact value from your Klipper & ACE configs (LLM Prompt)",
        "ui.settings.purge.llm_intro": "Copy this prompt and paste your printer.cfg / printer_KS1.cfg and ace.cfg / ace_KS1.cfg below it:",
        "ui.settings.purge.llm_prompt": "Analyze my attached Klipper and ACE Pro configs (printer.cfg / printer_KS1.cfg and ace.cfg / ace_KS1.cfg) and calculate the exact total filament length in mm purged during print startup when switching to a different tool:\n\n1. In [gcode_macro G9111] (printer_KS1.cfg / printer.cfg), sum all purge extrusion moves (e.g. G1 E50 + G1 E60 + G1 E90) in the non-skipped branch.\n2. Check if G9111 calls T{TOOL} before purging. If so, read `default_color_change_purge_length` and `purge_multiplier` from ace_KS1.cfg / ace.cfg (`default_color_change_purge_length * purge_multiplier`), and note `toolhead_full_purge_length` separately (which primes from sensor to nozzle).\n3. Give me a short breakdown and the final recommended number in mm to enter into Kobra-Van-Kenorca -> Settings -> Purge & Reserve -> Startup Flushing Volume (G9111, mm).",
        "ui.settings.purge.llm_copy_btn": "Copy LLM Prompt",
        "ui.settings.purge.llm_copied": "Copied startup flush LLM prompt to clipboard.",
        "ui.modal.job.purge_startup_added": "incl. G9111 ~{len} mm",
        "ui.modal.job.purge_startup_skipped": "G9111 initial purge skipped (tool already loaded)",
        "ui.modal.pred.avail": "Available",
        "ui.modal.pred.print": "Print",
        "ui.modal.pred.purge": "Purge / Flush",
        "ui.modal.pred.total": "Total",
        "ui.modal.pred.after": "After Print",
        "ui.modal.pred.warn_short": "Short by {deficit} g — spool may run out during print.",
        "ui.modal.pred.warn_low": "Remaining {rem} g is below {reserve} g safety reserve.",
        "ui.modal.pred.reason_no_spool": "No Spoolman spool matched to this ACE slot.",
        "ui.modal.pred.reason_no_weight": "Spoolman spool has no remaining weight recorded.",
        "ui.modal.pred.reason_slot_empty": "Mapped ACE slot is empty.",
        "ui.modal.pred.reason_slot_missing": "Mapped ACE slot is not available.",
        "ui.modal.pred.reason_conflict": "Ambiguous Spoolman match — resolve conflict first.",
        "ui.modal.pred.reason_no_usage": "G-code filament usage could not be determined.",
        "ui.modal.pred.reason_no_density": "Filament density missing — cannot convert purge volume (mm³) to grams.",
        "ui.modal.pred.reason_flush_incomplete": "Flush matrix/vector incomplete — purge usage unreliable.",
        "ui.modal.pred.reason_unreliable": "Prediction cannot be reliably determined.",
        "ui.settings.appearance.theme_label": "Theme Mode",
        "ui.settings.appearance.scheme_label": "Accent Color Scheme (8 Palettes)",
        "ui.settings.appearance.note": "Accent colors adapt buttons, highlights, and status controls across Light, Dark, Auto, and KobraOrca without altering ACE filament colors.",
        "ui.settings.scheme.teal": "Orca Teal",
        "ui.settings.scheme.ocean": "Ocean Blue",
        "ui.settings.scheme.amber": "Warm Amber",
        "ui.settings.scheme.coral": "Coral Red",
        "ui.settings.scheme.forest": "Forest Green",
        "ui.settings.scheme.violet": "Royal Violet",
        "ui.settings.scheme.cyan": "Arctic Cyan",
        "ui.settings.scheme.graphite": "Graphite",
        "ui.settings.language.label": "Interface Language",
        "ui.settings.language.hint": "Language changes apply immediately without losing unsaved connection fields.",
        "ui.settings.paths.gcode_active": "Active G-Code File",
        "ui.settings.paths.gcode_dir": "Default G-Code Folder:",
        "ui.settings.paths.gcode_dir_placeholder": "Optional e.g. /home/user/gcode",
        "ui.settings.paths.runtime_title": "Resolved Runtime Paths",
        "ui.settings.paths.cfg_file": "Plugin Config:",
        "ui.settings.paths.storage_dir": "Storage Dir:",
        "ui.settings.paths.staged_dir": "Staged G-Code:",
        "ui.settings.paths.diag_file": "Diagnostics Log:",
        "ui.settings.paths.unresolved": "(Unavailable outside OrcaSlicer runtime)",
        "ui.settings.diag.anon_title": "Anonymized Diagnostics Summary (Safe for Sharing)",
        "ui.settings.diag.btn_refresh": "Refresh Summary",
        "ui.settings.diag.btn_copy": "Copy Anonymized",
        "ui.settings.diag.copied": "Copied anonymized diagnostic summary to clipboard.",
        "ui.settings.diag.open_log": "Open Status Log",
        "ui.settings.diag.open_flush": "Open Flush & Audit",
        "ui.settings.diag.load_logging_label": "Enable detailed G-code load diagnostics log (diagnostics_gcode_load.jsonl)",
        "host.slot_spool_saved": "Slot T{tool}: Spoolman spool #{spool_id} manually assigned.",
        "host.slot_spool_cleared": "Slot T{tool}: Reverted to automatic Spoolman resolution.",
        "host.appearance_saved": "Appearance saved (theme={theme}, scheme={scheme}).",
        "host.moonraker_unconfigured": "Moonraker URL is not configured yet. Open Settings → Connection to set up your printer.",
        "ui.bar.dryer_label": "ACE Dryer:",
        "ui.bar.btn_prepare": "Prepare & Send Print",
        "ui.modal.preprint.title": "Map · Check · Send",
        "ui.modal.close": "Close",
        "ui.modal.kpi.time_layers": "Est. Time & Layers",
        "ui.modal.kpi.usage_purge": "Usage & Purge",
        "ui.modal.feedback.loading_slicer": "Checking active sliced G-code from OrcaSlicer...",
        "ui.modal.feedback.no_gcode_action": "No G-code file loaded yet. Slice a plate or click 'Browse .gcode' first.",
        "ui.modal.feedback.ready_short": "Ready for mapping",
        "ui.modal.job.no_file": "No G-code file selected",
        "ui.modal.job.time": "Est. Time: {time}",
        "ui.modal.job.weight": "Usage total: {weight} g",
        "ui.modal.job.purge_g": "Purge: {purge}",
        "ui.modal.job.layers": "Layers: {layers}",
        "ui.modal.job.toolchanges": "Toolchanges: {tc}",
        "ui.modal.cards.title": "Mapping",
        "ui.modal.cards.show_unused": "Show unused extruders",
        "ui.modal.card.slicer_ext": "Slicer T{tool}",
        "ui.modal.card.match_exact": "Material Match ({mat})",
        "ui.modal.card.match_sufficient_tip": "Material Match ({mat}) · Sufficient filament ({rem} g left)",
        "ui.modal.card.match_insufficient": "Insufficient filament (-{deficit} g)",
        "ui.modal.card.match_low": "Low reserve ({rem} g left)",
        "ui.modal.card.mismatch_badge": "Material mismatch ({slicer_mat} vs. {ace_mat})",
        "ui.modal.card.empty_badge": "Target slot T{slot} is empty",
        "ui.modal.card.shared_badge": "Shared slot: ACE T{slot}",
        "ui.modal.thumb.alt": "G-code preview thumbnail",
        "ui.modal.thumb.fallback_title": "No embedded G-code thumbnail found",
        "ui.modal.thumb.mode_mapped": "Mapped",
        "ui.modal.thumb.mode_original": "Original",
        "ui.modal.thumb.mode_title": "Toggle live preview between mapped ACE Pro colors and original Slicer colors",
        "ui.modal.thumb.zoom_title": "Click to inspect full-size preview (zoom)",
        "ui.modal.thumb.zoom_heading": "Model Color Preview (Zoom)",
        "ui.modal.thumb.zoom_close": "Close (Esc)",
        "ui.modal.thumb.zoom_dismiss_hint": "Click preview image, backdrop, or press Esc to return to Pre-Print dialog.",
        "ui.modal.mismatch_gate.title": "Material Mismatch Detected",
        "ui.modal.mismatch_gate.desc": "One or more mapped ACE Pro slots contain a different material than configured in OrcaSlicer. Please reassign the slot or explicitly confirm below to proceed.",
        "ui.modal.mismatch_gate.checkbox": "I acknowledge the material mismatch(es) and confirm printing with the selected ACE Pro slots.",
        "ui.modal.opt.g31": "Bed Leveling",
        "ui.modal.opt.g31_tip": "G31 — Auto Bed Leveling before print",
        "ui.modal.opt.skip_purge": "Skip Purge",
        "ui.modal.opt.skip_purge_tip": "SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 (K3/K3M only — KS1/KS1M G9111 always uses Smart Skip)",
        "ui.modal.opt.cut_post": "Unload after print",
        "ui.modal.opt.cut_post_tip": "PRINT_END CUT_TIP=1 — Cut tip and unload filament at print end",
        "ui.modal.opt.force_purge": "Force Initial Purge",
        "ui.modal.opt.force_purge_tip": "Force initial purge cycle even if tool is already loaded",
        "ui.modal.card.low_spool": "Low spool ({req} g needed / {rem} g left)",
        "ui.modal.card.conflict_badge": "SKU Conflict ({count} spools)",
        "val.map.low_spool": "Warning: ACE slot T{mapped} ({spool}) has {remaining_g} g remaining, but {required_g} g is required.",
        "val.map.spoolman_conflict": "Warning: ACE slot T{mapped} has multiple Spoolman matches for SKU '{sku}' ({count} spools) — no automatic assignment made.",
        "host.spoolman_disabled": "Spoolman integration is disabled (no URL configured).",
        "host.spoolman_ok": "Spoolman connected ({url}): {spools} spool(s) loaded, {matched} matched to ACE slots.",
        "host.spoolman_ok_conflicts": "Spoolman connected ({url}): {spools} spool(s) loaded, {matched} matched, {conflicts} SKU conflict(s).",
        "host.spoolman_err": "Spoolman query failed ({url}): {error}",
        "ui.modal.btn.inspect_flush": "Inspect Flush Volume & Post-Processing Preview",
        "ui.modal.btn.cancel": "Cancel",
        "ui.modal.btn.apply_local": "Apply & Patch Locally",
        "ui.modal.btn.export_gcode": "Export .gcode...",
        "ui.modal.btn.upload_only": "Upload Only",
        "ui.modal.btn.send_print": "Upload & Start Print",
        "ui.modal.btn.load_slicer": "Reload G-Code from Slicer",
        "ui.modal.btn.browse_gcode": "Browse .gcode",
        "ui.modal.btn.clear_gcode_title": "Clear loaded G-code",
        "ui.modal.stale.banner": "Loaded G-code '{file}' does not match active OrcaSlicer model '{model}'. If you only sliced in Preview, export the G-code (Ctrl+G) or click 'Browse .gcode'.",
        "ui.modal.stale.clear_btn": "Unload Stale G-code",
        "ui.modal.btn.slot_select_aria": "Select physical ACE Pro slot for extruder T{tool}",
        "ui.modal.slot.opt_empty": "Empty",
        "ui.modal.slot.opt_unconf": "Ready",
        "host.slicer_gcode_loaded": "Loaded sliced G-code '{file}' ({tc} toolchanges) — ready for mapping.",
        "host.slicer_gcode_not_found": "No staged G-code found (OrcaSlicer's orca.host API is read-only and cannot export plates on demand). Slice/export once or click 'Browse .gcode'.",
        "host.gcode_browsed_loaded": "Loaded local G-code '{file}' ({tc} toolchanges).",
        "host.gcode_cleared": "Cleared loaded G-code selection. Ready for next sliced export or local file.",
        "host.gcode_read_error": "Failed to load G-code file: {error}",
        "host.auto_opened_preprint": "Ready for mapping",
        "host.notify_sliced_ready": "G-code '{file}' ready ({tc} toolchanges) — switch to ⚕ Kobra (Kobra-Van-Kenorca) to send.",
        "host.notify_open_action": "Open Pre-Print Dialog",
        "gate.block.mat_mismatch_unconfirmed": "Material mismatch detected between Slicer extruder and ACE Pro slot — explicit confirmation required.",
        "gate.block.stale_gcode": "Upload blocked: loaded G-code '{file}' is stale or does not match active project ({reason}). Re-export or select current G-code.",
        "host.gcode_binary_rejected": "Rejected '{file}': binary or corrupt data is not valid G-code.",
        "stale.reason.model_mismatch": "project/model mismatch",
        "stale.reason.plate_mismatch": "plate index mismatch",
        "stale.reason.filament_mismatch": "filament preset mismatch",
        "stale.reason.slice_newer_than_export": "slice is newer than loaded G-code",
        "stale.reason.provenance_stale": "superseded by newer slice",
        "ui.modal.feedback.printing_busy": "Printer is currently {state} — 'Upload & Start Print' is locked. Use 'Upload Only' or 'Export .gcode...' instead.",
        "ui.modal.lock.printing": "Printer is currently {state} — 'Upload & Start Print' is locked ('Upload Only' and 'Export .gcode...' remain available).",
        "ui.modal.lock.offline": "Printer is {state} — upload is disabled until Moonraker is connected ('Apply & Patch Locally' and 'Export .gcode...' remain available).",
        "ui.modal.lock.mapping_errors": "Target slot error in Mapping above — fix empty or out-of-range slots to unlock upload and export.",
        "ui.modal.lock.mismatch": "Material mismatch detected — confirm the checkbox above or reassign the target slot to unlock upload and export.",
        "ui.ace.off_badge": "ACE Pro OFF (External Spool)",
        "ui.ace.connecting_badge": "ACE Connecting…",
        "ui.ace.disconnected_badge": "ACE Disconnected",
        "ui.ace.btn_turn_off": "Disable ACE Pro (switch to External Spool)",
        "ui.ace.btn_turn_on": "Enable ACE Pro",
        "host.ace_pro_enabled": "ACE Pro enabled in Klipper (SET_PIN PIN=ACE_Pro VALUE=1).",
        "host.ace_pro_disabled": "ACE Pro disabled in Klipper (SET_PIN PIN=ACE_Pro VALUE=0 — External Spool).",
        "ui.modal.opt.external_spool": "External Spool (Bypass ACE)",
        "ui.modal.opt.external_spool_tip": "Treat this job as an external spool print: skips ACE slot bounds, empty-slot/material checks, ACE tool remapping, and purge commands.",
        "ui.modal.opt.endless_spool": "Endless Spool",
        "ui.modal.opt.endless_spool_tip": "Automatically switch to a matching backup spool on filament runout (applied to Klipper on Upload / Print)",
        "ui.modal.opt.endless_mode_exact": "Exact (Material + Color)",
        "ui.modal.opt.endless_mode_material": "Material",
        "ui.modal.opt.endless_mode_next": "Next Ready",
        "ui.modal.card.backup_badge": "↻ Backup: {slots}",
        "ui.modal.card.backup_none": "↻ No Backup",
        "ui.modal.card.backup_tip": "Likely backup ({mode}): {slots} ({mat}{rem}) will probably take over on runout (estimated by ACEPRO rules)",
        "ui.modal.card.backup_tip_driver": "Driver-confirmed backup ({mode}): {slots} ({mat}{rem}) reported by ACEPRO ace_state.endless_spool_next_tool",
        "ui.modal.card.backup_short_tip": "Likely backup ({mode}): {slots} ({mat}{rem}) — still {still_short} g short to finish the print",
        "ui.modal.card.backup_none_tip": "Endless Spool ({mode}): No matching ready backup spool found",
        "ui.modal.pred.backup_ok": "Short by {deficit} g — likely backup {slots} ({avail} g left) is enough to finish the print.",
        "ui.modal.pred.backup_short": "Short by {deficit} g — likely backup {slots} ({avail} g left) is also not enough ({still_short} g still short).",
        "ui.modal.pred.backup_unknown": "Short by {deficit} g — likely backup {slots} (remaining weight unknown).",
        "ui.modal.pred.backup_none": "Short by {deficit} g — no matching ready backup spool found ({mode}).",
        "ui.nav.guide": "Guide",
        "ui.nav.guide_tip": "Open Quick-Start Guide & FAQ",
        "ui.settings.tab_guide": "Guide",
        "ui.guide.intro": "Quick overview of Kobra-Van-Kenorca. Full guide with details on GitHub:",
        "ui.guide.github_link": "Open full USER_GUIDE.md on GitHub",
        "ui.guide.s1_title": "1. Quick Start (3 Steps)",
        "ui.guide.s1_body": "1) Enter your printer's Moonraker URL under Settings → Connection and click Connect. 2) Slice your model in OrcaSlicer Preview. 3) Click Print — Map · Check · Send opens automatically so you can verify slots, materials, and purge before uploading.",
        "ui.guide.s2_title": "2. Slot Bay, Auto-Feed, ACE Power & Safety Lock",
        "ui.guide.s2_body": "Load/Unload/Full Unload operate ACE Pro slots (stay at the printer during Full Unload so the spool does not tangle). Auto-Feed toggles ACE feed assist (green = ON). The ⏻ button toggles Klipper's ACE_Pro pin (ACE vs. External Spool). During an active print, hardware buttons lock automatically — click Unlock in the orange status tile only if you need manual intervention.",
        "ui.guide.s3_title": "3. Map · Check · Send & Spoolman",
        "ui.guide.s3_body": "Map each Slicer extruder to any ACE slot (T0–T15), share one slot across extruders, or use Auto-Match. Material mismatches require explicit confirmation. With optional Spoolman configured, remaining filament (model + purge) is checked before printing.",
        "ui.guide.s4_title": "4. Endless Spool & External Spool",
        "ui.guide.s4_body": "Enable Endless Spool and pick Exact (Material + Color), Material, or Next Ready to preview likely backup spool chains (T2 → T3) and check if backups cover any deficit. Changes are sent to Klipper only when you click Upload / Print. With ACE Pro OFF, External Spool mode bypasses ACE checks and optionally tracks a chosen Spoolman spool.",
        "ui.guide.s5_title": "5. FAQ & Troubleshooting",
        "ui.guide.s5_body": "If Map · Check · Send does not open when clicking Print (especially after an OrcaSlicer update), reload the plugin under Plugins and verify KobraVanKenorca_PostProcess is listed once under Process → Others → Slicing Pipeline Plugin. Cancel aborts the upload safely without starting a print.",
        "ui.modal.map.ext_spool_badge": "External Spool · ACE bypassed",
        "ui.modal.map.ext_spool_klipper": "ACE Pro is disabled in Klipper (output_pin ACE_Pro = 0) — printing via external spool; ACE slot checks and purge commands are bypassed.",
        "ui.modal.map.ext_spool_manual": "External Spool mode active — ACE slot checks, tool remapping, and purge commands are bypassed (ensure ACE_Pro is OFF in Klipper if not using ACE).",
        "ui.modal.map.switch_to_ace": "Switch to ACE Pro",
        "ui.modal.lock.external_pin_warn": "External Spool active while Klipper ACE_Pro pin is still ON — set ACE_Pro = 0 in Mainsail/Fluidd if printing outside ACE.",
        "ui.modal.ext.no_tracking": "External Spool (No Tracking)",
        "ui.modal.ext.no_tracking_sub": "Manual feed · no Spoolman sync",
        "ui.modal.ext.tracking_badge": "Spoolman #{id} · Auto-Track",
        "ui.modal.ext.select_aria": "Select external spool (Spoolman)",
        "moonraker.upload_403": "Moonraker upload rejected (HTTP 403): an active print job is already running or the target file is locked ({detail}).",
        "host.export_done": "Exported patched G-code to '{path}' ({tc} toolchanges, {len} mm purge).",
        "host.export_done_fallback": "Write access denied for '{requested}' — exported patched G-code to '{path}' instead ({tc} toolchanges, {len} mm purge).",
        "host.export_done_ui": "Exported patched G-code '{file}' ({tc} toolchanges, {len} mm purge).",
        "ui.export_modal.title": "Export Patched G-code",
        "ui.export_modal.dir_label": "Target Folder",
        "ui.export_modal.btn_downloads": "Downloads",
        "ui.export_modal.btn_plugin_dir": "Plugin Folder",
        "ui.export_modal.file_label": "Filename",
        "ui.export_modal.hint": "Choose the destination folder and filename. If OrcaSlicer's sandbox blocks writing outside plugin storage, the file is saved into the plugin's exported_gcode folder and the exact path is shown.",
        "ui.export_modal.btn_save": "Save .gcode",
        "host.slice_invalidated": "Active slicer preview was removed or invalidated — reset slice state and closed Pre-Print dialog.",
        "host.slice_invalidated_presets": "Slicer presets or filaments changed — previous slice was invalidated. Please slice again in OrcaSlicer.",
        "ui.notice.no_gcode_slice_first": "No valid sliced G-code → Please slice the plate in Preview or",
        "ui.notice.slice_outdated_presets": "Slicer settings or filaments changed in OrcaSlicer — please slice again (not re-sliced since the last edit).",
        "ui.notice.slicing_in_progress": "Slicing in progress in OrcaSlicer… New G-code will load automatically when finished.",
        "ui.notice.btn_browse_gcode": "Browse .gcode...",
        "ui.notice.btn_dismiss": "Dismiss",
    },
    "de": {
        # Header & Theme / Language Controls
        "header.language.aria": "Sprache der Benutzeroberfläche wählen",
        "header.language.en": "English",
        "header.language.de": "Deutsch",
        "header.theme.aria": "Farbschema wählen",
        "header.theme.auto": "Auto",
        "header.theme.light": "Hell",
        "header.theme.dark": "Dunkel",
        "header.theme.kobraorca": "KobraOrca",

        # Executive Status Strip
        "exec.dryer.standby": "Standby",
        "exec.dryer.active": "AKTIV • {temp} °C",
        "exec.dryer.remain": "Restlaufzeit: {mins} min",

        # Safety Banner

        # Section 1: Moonraker Connection
        "sec1.title": "Moonraker-Verbindung",
        "sec1.url.placeholder": "http://192.0.2.10:7125 oder http://printer.local:7125",

        # Section 2: ACE Pro Slots & Dryer
        "slot.no_filament": "Kein Filament erkannt",
        "slot.sku": "SKU: {sku}",

        # Section 3: Color Mapping Workbench
        "sec3.title": "Color Mapping Workbench",

        # Section 4: Print Preparation & Hardware Macros

        # Section 5: G-code Post-Processing & Moonraker Upload

        # Section 6: Flush Volume Monitoring & Post-Processing Audit
        "sec6.title": "6. Flush Volume Monitoring & Post-Processing Audit",
        "flush.matrix.corner": "Von \\ Nach",

        # Activity Log Footer
        "log.title": "System- & Aktivitätsprotokoll",
        "log.clear": "Protokoll leeren",
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
        "host.reset_done": "Color Mapping auf 1:1 Standard zurückgesetzt.",
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
        "gate.block.no_info": "Kein gültiges Post-Processing-Ergebnis vorhanden.",
        "gate.block.multi_incomplete": "Mehrere Extruder verwendet, aber Flush-Matrix/Vector unvollständig.",
        "host.ready": "ACE Pro Plugin bereit.",
        "host.sync_none": "Keine aktiven Filament-Presets von orca.host.preset_bundle() erhalten.",
        "host.status_updated": "Moonraker & ACE Pro Status aktualisiert.",
        "host.printer_switched": "Aktives Drucker-Profil auf '{name}' gewechselt.",
        "host.printer_saved": "Drucker-Profil '{name}' gespeichert.",
        "host.printer_deleted": "Drucker-Profil '{name}' gelöscht.",
        "host.printer_delete_last": "Das letzte verbleibende Drucker-Profil kann nicht gelöscht werden.",
        "host.hw_busy": "Eine Hardware-Aktion läuft bereits — bitte warten.",
        "host.slot_active_unloaded": "Aktives Tool entladen (ACE_CHANGE_TOOL TOOL=-1).",
        "host.slot_full_unloaded": "Slot T{slot}: Komplett-Entladen abgeschlossen ({cmd}).",
        "host.slot_assist_on": "Slot T{slot}: Feed Assist aktiviert ({cmd}).",
        "host.slot_assist_off": "Slot T{slot}: Feed Assist deaktiviert ({cmd}).",
        "host.slot_fed": "Slot T{slot}: {length}mm vorgeschoben (ACE_FEED).",
        "host.slot_retracted": "Slot T{slot}: {length}mm zurückgezogen (ACE_RETRACT).",
        "host.cut_tip_done": "CUT_TIP-Makro über Moonraker ausgelöst.",
        "host.bed_mesh_done": "BED_MESH_CALIBRATE PROFILE=adaptive ADAPTIVE=1 über Moonraker ausgelöst.",
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
        "host.profile_changed_abort": "Aktives Drucker-Profil wurde während der Upload-Prüfung gewechselt — Upload aus Sicherheitsgründen abgebrochen.",
        "ui.brand.title": "Kobra-Van-Kenorca",
        "ui.brand.sub": "Kobra-Van-Kenorca — Vanilla Klipper & ACE Pro Bridge for OrcaSlicer",
        "ui.conn.connected": "Verbunden ({model})",
        "ui.conn.unconfigured": "Nicht eingerichtet",
        "ui.conn.offline": "Offline / Nicht erreichbar",
        "ui.notice.unconfigured": "Noch nicht mit deinem Drucker verbunden. Öffne Connection, um Moonraker und ACE Pro einzurichten.",
        "ui.notice.offline": "Drucker offline oder Moonraker nicht erreichbar ({url}). Bitte Stromversorgung und Connection-Einstellungen prüfen.",
        "ui.notice.btn_open_conn": "Connection öffnen",
        "ui.notice.btn_retry": "Erneut prüfen",
        "ui.exec.conn.unconfigured": "Einrichtung nötig",
        "ui.exec.conn.unconfigured_sub": "Einstellungen → Verbindung öffnen",
        "ui.printer.standby": "Printer: Standby",
        "ui.printer.state": "Printer: {state}",
        "ui.header.refresh": "Aktualisieren",
        "ui.safety.banner_prefix": "Druck aktiv (",
        "ui.safety.banner_suffix": ") — Alle Hardware-, Motion-, Load/Unload- und Direktdruck-Befehle sind zum Schutz des laufenden Drucks gesperrt.",
        "ui.safety.badge": "Hardware-Sperre aktiv",
        "ui.safety.locked_text": "Druck aktiv — Hardware gesperrt",
        "ui.safety.unlocked_text": "Hardware entsperrt — Druck läuft, Vorsicht!",
        "ui.safety.btn_unlock": "Entsperren",
        "ui.safety.btn_lock": "Sperren",
        "ui.exec.conn.online": "Online ({state})",
        "ui.exec.conn.offline": "Offline / Bereit",
        "ui.exec.tool.kicker": "Aktiver Toolhead",
        "ui.exec.tool.none": "Keins (-1)",
        "ui.exec.tool.sub": "ACE Temp: {temp} · Lüfter: {fan}",
        "ui.exec.tool.temp_part": "ACE Temp: {temp}",
        "ui.exec.tool.hum_part": "Luftfeuchte: {hum}",
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
        "ui.sec1.spoolman_label": "Spoolman-URL:",
        "ui.sec1.spoolman_placeholder": "Optional z. B. http://192.0.2.10:7912 (leer = aus)",
        "ui.sec1.spoolman_test_btn": "Testen / Aktualisieren",
        "ui.sec1.spoolman_off": "Spoolman: Deaktiviert (optional)",
        "ui.sec1.spoolman_ok": "Spoolman: Verbunden ({spools} Spulen · {matched} zugeordnet)",
        "ui.sec1.spoolman_ok_conflicts": "Spoolman: Verbunden ({spools} Spulen · {matched} zugeordnet · {conflicts} Konflikt)",
        "ui.sec1.spoolman_err": "Spoolman: Nicht erreichbar ({error})",
        "ui.sec1.meta_initial": "ACE Status: — | Aktives Tool: — | Temp: —",
        "ui.sec1.meta_live": "ACE Status: {status} | Aktives Tool: {tool} | Temp: {temp}°C | Lüfter: {fan} RPM{print_label}",
        "ui.sec1.meta_print": " | Druck: {state}",
        "ui.sec2.dryer_stop_badge": "Dryer: Stop",
        "ui.sec2.dryer_active_badge": "Dryer: {temp}°C ({mins} min übrig)",
        "ui.sec2.dryer_multi_badge": "Dryer: {units}",
        "ui.sec2.dryer_unit_item": "ACE {unit} {temp}°C ({mins} min)",
        "ui.sec2.unit_label": "Einheit:",
        "ui.sec2.unit_all": "Beide (Alle ACE)",
        "ui.sec2.unit_all_n": "Alle {count} ACEs",
        "ui.sec2.unit_ace1": "ACE 1 (T0–T3)",
        "ui.sec2.unit_ace2": "ACE 2 (T4–T7)",
        "ui.sec2.unit_ace3": "ACE 3 (T8–T11)",
        "ui.sec2.unit_ace4": "ACE 4 (T12–T15)",
        "ui.sec2.unit_select_aria": "ACE Pro Trockner-Einheit wählen",
        "ui.sec2.preset_label": "Preset:",
        "ui.sec2.preset_aria": "Trockner-Temperatur-Preset wählen",
        "ui.sec2.preset_custom": "Manuell",
        "ui.sec2.preset_pla": "PLA (45 °C)",
        "ui.sec2.preset_petg": "PETG (60 °C)",
        "ui.sec2.preset_tpu": "TPU (50 °C)",
        "ui.sec2.temp_label": "Temp (°C):",
        "ui.sec2.dur_label": "Dauer (Min):",
        "ui.sec2.btn_dryer_start": "Dryer Start",
        "ui.sec2.btn_dryer_stop": "Dryer Stop",
        "ui.bar.btn_preparing": "Wird vorbereitet…",
        "ui.sec3.title": "3. ACE Pro Live Slot-Bay (T0–T7)",
        "ui.sec3.title_dynamic": "3. ACE Pro Live Slot-Bay (T0–T{max_tool})",
        "ui.slot.badge_active": "Aktiv",
        "ui.slot.badge_ready": "Bereit",
        "ui.slot.badge_empty": "Leer",
        "ui.slot.badge_shifting": "SHIFTING",
        "ui.slot.no_filament": "Kein Filament",
        "ui.slot.unconfigured": "Unkonfiguriert",
        "ui.slot.rfid_detected": "RFID erkannt",
        "ui.slot.remaining": "{grams} g übrig",
        "ui.slot.remaining_pct": "{grams} g übrig ({pct}%)",
        "ui.slot.weight_empty_pct": "0.0 g übrig (0%)",
        "ui.slot.weight_missing": "Gewicht fehlt",
        "ui.slot.spoolman_conflict": "Spoolman-Konflikt: {count} Spulen für SKU '{sku}'",
        "ui.slot.spool_auto_last": "Spoolman · Spule #{id} · zuletzt verwendet",
        "ui.slot.spool_auto_fb": "Spoolman · Spule #{id} · Fallback",
        "ui.slot.spool_auto_single": "Spoolman · Spule #{id} · automatisch",
        "ui.slot.spool_manual": "Spoolman · Spule #{id} · manuell",
        "ui.slot.spool_select_aria": "Spoolman-Spule für ACE-Slot T{tool} wählen",
        "ui.slot.spool_opt_auto": "Automatisch (zuletzt verwendet / Fallback)",
        "ui.slot.btn_load": "Laden",
        "ui.slot.btn_unload": "Entladen",
        "ui.slot.btn_full_unload": "Full Unload",
        "ui.slot.btn_assist_on": "Auto-Feed",
        "ui.slot.btn_assist_off": "Auto-Feed",
        "ui.slot.btn_feed": "+Feed",
        "ui.slot.btn_retr": "-Retr",
        "ui.slot.title_load": "Tool T{tool} laden",
        "ui.slot.title_unload": "Slot T{tool} entladen",
        "ui.slot.title_full_unload": "Slot T{tool} komplett entladen (ACE_FULL_UNLOAD — mit Sicherheitsabfrage)",
        "ui.slot.title_assist_on": "Auto-Feed ist EIN für T{tool} — klicken zum Ausschalten (ACE_DISABLE_FEED_ASSIST)",
        "ui.slot.title_assist_off": "Auto-Feed ist AUS für T{tool} — klicken zum Einschalten (ACE_ENABLE_FEED_ASSIST)",
        "ui.slot.full_unload_modal_title": "Full Unload bestätigen — Slot T{tool}",
        "ui.slot.full_unload_modal_body": "ACE_FULL_UNLOAD zieht das Filament komplett aus dem ACE Pro heraus. Du MUSST am Drucker anwesend sein und das Filament auf der Spule festhalten/aufrollen — sonst kann sich die Spule abwickeln und verheddern!",
        "ui.slot.full_unload_modal_confirm": "Ich bin am Drucker — T{tool} komplett entladen",
        "ui.sec4.btn_project": "Slicer-Vorgabe",
        "ui.sec4.btn_automatch": "Auto-Match",
        "ui.sec4.btn_reset": "1:1 Standard",
        "ui.sec4.summary_ready": "Bereit",
        "ui.sec4.col_extruder": "Slicer Extruder",
        "ui.map.mode_auto": "Auto-Match",
        "ui.map.mode_manual": "Manuell",
        "ui.map.mode_default": "1:1",
        "ui.map.not_present": "T{slot} (Nicht vorhanden)",
        "ui.map.badge_invalid": "Ungültig",
        "ui.map.unused_badge": "Unbenutzt (0,0 g)",
        "ui.map.sum_err_badge": "{count} Mapping-Fehler",
        "ui.map.sum_warn_badge": "{count} Hinweise",
        "ui.map.sum_ok_badge": "Mapping konsistent",
        "ui.map.usage_info_aria": "Detaillierte Aufschlüsselung des Filamentverbrauchs",
        "ui.map.usage_info_heading": "Filamentverbrauch im Detail",
        "ui.map.usage_info_print": "Druck (Modell)",
        "ui.map.usage_info_purge": "Purge / Flush",
        "ui.map.usage_info_total": "Gesamtbedarf",
        "ui.map.usage_info_ext": "Aktive Extruder",
        "ui.sec5.opt_skip_purge_sub": "Setzt SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 oder 0 im G9111-Startmakro",
        "ui.sec5.path_placeholder": "/pfad/zur/datei.gcode",
        "ui.sec5.btn_parse": "Filamente aus Header lesen",
        "ui.sec5.btn_preview": "Offline Flush-Analyse (Preview)",
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
        "ui.flush.kpi_g31_cut": "SkipPurge / CUT_TIP",
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
        "ui.nav.settings": "Einstellungen",
        "ui.nav.diagnostics": "Flush & Audit",
        "ui.settings.title": "Kobra-Van-Kenorca Einstellungen",
        "ui.settings.tab_connection": "Verbindung",
        "ui.settings.tab_purge": "Spülung & Reserve",
        "ui.settings.tab_appearance": "Darstellung",
        "ui.settings.tab_language": "Sprache",
        "ui.settings.tab_paths": "Pfade",
        "ui.settings.tab_diagnostics": "Diagnose",
        "ui.settings.conn.save_only": "Einstellungen speichern",
        "ui.settings.conn.ace_count_label": "ACE-Einheiten (Offline / Vor Verbindung):",
        "ui.settings.conn.ace_count_aria": "Konfigurierte ACE-Pro-Anzahl wählen",
        "ui.settings.conn.ace_count_auto": "Automatisch (von Klipper)",
        "ui.settings.conn.ace_count_1": "1 ACE (4 Slots · T0–T3)",
        "ui.settings.conn.ace_count_2": "2 ACEs (8 Slots · T0–T7)",
        "ui.settings.conn.ace_count_3": "3 ACEs (12 Slots · T0–T11)",
        "ui.settings.conn.ace_count_4": "4 ACEs (16 Slots · T0–T15)",
        "ui.header.printer_aria": "Aktives Drucker-Profil wählen",
        "ui.modal.target_printer": "Ziel: {name}",
        "ui.settings.conn.profile_label": "Drucker-Profil:",
        "ui.settings.conn.profile_name_label": "Profilname:",
        "ui.settings.conn.profile_name_placeholder": "z. B. Kobra S1 Werkstatt",
        "ui.settings.conn.profile_new": "+ Neuer Drucker",
        "ui.settings.conn.profile_rename": "Name speichern",
        "ui.settings.conn.profile_delete": "Löschen",
        "ui.settings.conn.profile_default_new": "Drucker {n}",
        "ui.settings.conn.reserve_label": "Sicherheitsreserve vor Druck (g):",
        "ui.settings.conn.reserve_hint": "Warnung „Knapp“, wenn die Restmenge nach Druck + Purge unter diese Reserve fällt.",
        "ui.settings.conn.startup_purge_label": "Start-Spülmenge (G9111, mm):",
        "ui.settings.conn.startup_purge_hint": "Geschätzte Filamentlänge (mm), die beim Druckstart gespült wird, falls der Ziel-Slot noch nicht geladen ist (0 = aus).",
        "ui.settings.purge.llm_summary": "Exakten Wert aus deinen Klipper- & ACE-Configs berechnen (LLM-Prompt)",
        "ui.settings.purge.llm_intro": "Kopiere diesen Prompt und füge deine printer.cfg / printer_KS1.cfg sowie ace.cfg / ace_KS1.cfg darunter ein:",
        "ui.settings.purge.llm_prompt": "Analysiere meine angehängten Klipper- und ACE-Pro-Configs (printer.cfg / printer_KS1.cfg und ace.cfg / ace_KS1.cfg) und berechne die exakte gesamte Filamentlänge in mm, die beim Druckstart gespült wird, wenn auf ein anderes Tool gewechselt wird:\n\n1. Summiere in [gcode_macro G9111] (printer_KS1.cfg / printer.cfg) alle Purge-Extrusionen (z. B. G1 E50 + G1 E60 + G1 E90) im nicht-übersprungenen Zweig.\n2. Prüfe, ob G9111 vor dem Purge T{TOOL} aufruft. Falls ja, lies `default_color_change_purge_length` und `purge_multiplier` aus der ace_KS1.cfg / ace.cfg (`default_color_change_purge_length * purge_multiplier`) aus und weise `toolhead_full_purge_length` (Füllen ab Sensor bis Düse) getrennt aus.\n3. Gib mir eine kurze Aufschlüsselung und den finalen empfohlenen Wert in mm für Kobra-Van-Kenorca -> Einstellungen -> Spülung & Reserve -> Start-Spülmenge (G9111, mm).",
        "ui.settings.purge.llm_copy_btn": "LLM-Prompt kopieren",
        "ui.settings.purge.llm_copied": "LLM-Prompt für die Start-Spülmenge in die Zwischenablage kopiert.",
        "ui.modal.job.purge_startup_added": "inkl. G9111 ~{len} mm",
        "ui.modal.job.purge_startup_skipped": "G9111-Initial-Purge übersprungen (Tool bereits geladen)",
        "ui.modal.pred.avail": "Verfügbar",
        "ui.modal.pred.print": "Druck",
        "ui.modal.pred.purge": "Purge / Flush",
        "ui.modal.pred.total": "Gesamt",
        "ui.modal.pred.after": "Danach",
        "ui.modal.pred.warn_short": "Fehlmenge ca. {deficit} g — Spule reicht voraussichtlich nicht.",
        "ui.modal.pred.warn_low": "Restmenge {rem} g liegt unter der Sicherheitsreserve ({reserve} g).",
        "ui.modal.pred.reason_no_spool": "Diesem ACE-Slot ist keine Spoolman-Spule zugeordnet.",
        "ui.modal.pred.reason_no_weight": "Für die Spoolman-Spule ist kein Restgewicht hinterlegt.",
        "ui.modal.pred.reason_slot_empty": "Zugeordneter ACE-Slot ist leer.",
        "ui.modal.pred.reason_slot_missing": "Zugeordneter ACE-Slot ist nicht verfügbar.",
        "ui.modal.pred.reason_conflict": "Mehrdeutige Spoolman-Zuordnung — Konflikt zuerst klären.",
        "ui.modal.pred.reason_no_usage": "Filamentverbrauch im G-Code nicht bestimmbar.",
        "ui.modal.pred.reason_no_density": "Filamentdichte fehlt — Purge-Volumen (mm³) nicht in Gramm umrechenbar.",
        "ui.modal.pred.reason_flush_incomplete": "Flush-Matrix/-Vektor unvollständig — Purge-Menge unzuverlässig.",
        "ui.modal.pred.reason_unreliable": "Prognose nicht zuverlässig bestimmbar.",
        "ui.settings.appearance.theme_label": "Theme-Modus",
        "ui.settings.appearance.scheme_label": "Akzent-Farbschema (8 Paletten)",
        "ui.settings.appearance.note": "Akzentfarben passen Schaltflächen, Hervorhebungen und Status-Elemente in Hell, Dunkel, Auto und KobraOrca an, ohne ACE-Filamentfarben zu verändern.",
        "ui.settings.scheme.teal": "Orca Teal",
        "ui.settings.scheme.ocean": "Ozeanblau",
        "ui.settings.scheme.amber": "Bernstein",
        "ui.settings.scheme.coral": "Korallenrot",
        "ui.settings.scheme.forest": "Waldgrün",
        "ui.settings.scheme.violet": "Violett",
        "ui.settings.scheme.cyan": "Arktis-Cyan",
        "ui.settings.scheme.graphite": "Graphit",
        "ui.settings.language.label": "Oberflächensprache",
        "ui.settings.language.hint": "Sprachwechsel wirkt sofort, ohne ungespeicherte Verbindungsfelder zu verlieren.",
        "ui.settings.paths.gcode_active": "Aktive G-Code-Datei",
        "ui.settings.paths.gcode_dir": "Standard-G-Code-Ordner:",
        "ui.settings.paths.gcode_dir_placeholder": "Optional z. B. /home/user/gcode",
        "ui.settings.paths.runtime_title": "Ermittelte Laufzeitpfade",
        "ui.settings.paths.cfg_file": "Plugin-Konfiguration:",
        "ui.settings.paths.storage_dir": "Plugin-Speicher:",
        "ui.settings.paths.staged_dir": "Staged-G-Code-Ordner:",
        "ui.settings.paths.diag_file": "Diagnose-Log:",
        "ui.settings.paths.unresolved": "(Außerhalb der OrcaSlicer-Runtime nicht aktiv)",
        "ui.settings.diag.anon_title": "Anonymisierte Diagnose-Zusammenfassung (Weitergabe-sicher)",
        "ui.settings.diag.btn_refresh": "Aktualisieren",
        "ui.settings.diag.btn_copy": "Anonymisiert kopieren",
        "ui.settings.diag.copied": "Anonymisierte Diagnose-Zusammenfassung in die Zwischenablage kopiert.",
        "ui.settings.diag.open_log": "Status-Log öffnen",
        "ui.settings.diag.open_flush": "Flush & Audit öffnen",
        "ui.settings.diag.load_logging_label": "Detailliertes G-Code-Lade-Diagnose-Log aktivieren (diagnostics_gcode_load.jsonl)",
        "host.slot_spool_saved": "Slot T{tool}: Spoolman-Spule #{spool_id} manuell zugeordnet.",
        "host.slot_spool_cleared": "Slot T{tool}: Zurück auf automatische Spoolman-Zuordnung.",
        "host.appearance_saved": "Darstellung gespeichert (Theme={theme}, Schema={scheme}).",
        "host.moonraker_unconfigured": "Moonraker-URL ist noch nicht konfiguriert. Öffne Einstellungen → Verbindung zum Einrichten.",
        "ui.bar.dryer_label": "ACE Trockner:",
        "ui.bar.btn_prepare": "Druck vorbereiten & senden",
        "ui.modal.preprint.title": "Map · Check · Send",
        "ui.modal.close": "Schließen",
        "ui.modal.kpi.time_layers": "Druckzeit & Schichten",
        "ui.modal.kpi.usage_purge": "Verbrauch & Purge",
        "ui.modal.feedback.loading_slicer": "Prüfe aktiven geslicten G-Code aus OrcaSlicer...",
        "ui.modal.feedback.no_gcode_action": "Noch kein G-Code geladen. Bitte zuerst slicen oder '.gcode wählen' nutzen.",
        "ui.modal.feedback.ready_short": "Bereit fürs Mapping",
        "ui.modal.job.no_file": "Keine G-Code-Datei ausgewählt",
        "ui.modal.job.time": "Druckzeit: {time}",
        "ui.modal.job.weight": "Verbrauch gesamt: {weight} g",
        "ui.modal.job.purge_g": "Purge: {purge}",
        "ui.modal.job.layers": "Schichten: {layers}",
        "ui.modal.job.toolchanges": "Toolchanges: {tc}",
        "ui.modal.cards.title": "Mapping",
        "ui.modal.cards.show_unused": "Unbenutzte Extruder anzeigen",
        "ui.modal.card.slicer_ext": "Slicer T{tool}",
        "ui.modal.card.match_exact": "Material-Match ({mat})",
        "ui.modal.card.match_sufficient_tip": "Material-Match ({mat}) · Genug Filament ({rem} g übrig)",
        "ui.modal.card.match_insufficient": "Filament reicht nicht (-{deficit} g)",
        "ui.modal.card.match_low": "Reserve knapp ({rem} g übrig)",
        "ui.modal.card.mismatch_badge": "Material-Abweichung ({slicer_mat} vs. {ace_mat})",
        "ui.modal.card.empty_badge": "Ziel-Slot T{slot} leer",
        "ui.modal.card.shared_badge": "Geteilter Slot: ACE T{slot}",
        "ui.modal.thumb.alt": "G-Code-Vorschaubild",
        "ui.modal.thumb.fallback_title": "Kein eingebettetes G-Code-Vorschaubild vorhanden",
        "ui.modal.thumb.mode_mapped": "Zugewiesen",
        "ui.modal.thumb.mode_original": "Original",
        "ui.modal.thumb.mode_title": "Live-Vorschau zwischen zugewiesenen ACE-Pro-Farben und originalen Slicer-Farben umschalten",
        "ui.modal.thumb.zoom_title": "Klicken, um die Vorschau in voller Größe zu vergrößern (Zoom)",
        "ui.modal.thumb.zoom_heading": "Modell-Farbvorschau (Zoom)",
        "ui.modal.thumb.zoom_close": "Schließen (Esc)",
        "ui.modal.thumb.zoom_dismiss_hint": "Auf Vorschaubild, Hintergrund oder Esc drücken, um zum Pre-Print-Dialog zurückzukehren.",
        "ui.modal.mismatch_gate.title": "Material-Abweichung erkannt",
        "ui.modal.mismatch_gate.desc": "Ein oder mehrere zugeordnete ACE Pro Slots enthalten ein anderes Material als in OrcaSlicer konfiguriert. Bitte Slot ändern oder unten explizit bestätigen.",
        "ui.modal.mismatch_gate.checkbox": "Ich bestätige die Material-Abweichung(en) und möchte mit den gewählten ACE Pro Slots fortfahren.",
        "ui.modal.opt.g31": "Bed Leveling",
        "ui.modal.opt.g31_tip": "G31 — Auto Bed Leveling vor dem Druck",
        "ui.modal.opt.skip_purge": "Spülen überspringen",
        "ui.modal.opt.skip_purge_tip": "SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 (nur K3/K3M — KS1/KS1M G9111 nutzt immer Smart Skip)",
        "ui.modal.opt.cut_post": "Entladen nach Druck",
        "ui.modal.opt.cut_post_tip": "PRINT_END CUT_TIP=1 — Filament am Druckende schneiden und entladen",
        "ui.modal.opt.force_purge": "Initial-Purge erzwingen",
        "ui.modal.opt.force_purge_tip": "Initialen Purge-Zyklus erzwingen, selbst wenn das Tool bereits geladen ist",
        "ui.modal.card.low_spool": "Spule knapp ({req} g benötigt / {rem} g übrig)",
        "ui.modal.card.conflict_badge": "SKU-Konflikt ({count} Spulen)",
        "val.map.low_spool": "Hinweis: ACE-Slot T{mapped} ({spool}) hat noch {remaining_g} g Restgewicht, aber {required_g} g benötigt.",
        "val.map.spoolman_conflict": "Hinweis: ACE-Slot T{mapped} hat mehrere Spoolman-Treffer für SKU '{sku}' ({count} Spulen) — keine willkürliche Zuordnung durchgeführt.",
        "host.spoolman_disabled": "Spoolman-Integration deaktiviert (keine URL konfiguriert).",
        "host.spoolman_ok": "Spoolman verbunden ({url}): {spools} Spule(n) geladen, {matched} ACE-Slots zugeordnet.",
        "host.spoolman_ok_conflicts": "Spoolman verbunden ({url}): {spools} Spule(n) geladen, {matched} zugeordnet, {conflicts} SKU-Konflikt(e).",
        "host.spoolman_err": "Spoolman-Abfrage fehlgeschlagen ({url}): {error}",
        "ui.modal.btn.inspect_flush": "Flush-Volumen & Post-Processing-Vorschau prüfen",
        "ui.modal.btn.cancel": "Abbrechen",
        "ui.modal.btn.apply_local": "Übernehmen & lokal patchen",
        "ui.modal.btn.export_gcode": "Exportieren...",
        "ui.modal.btn.upload_only": "Nur hochladen",
        "ui.modal.btn.send_print": "Hochladen & Druck starten",
        "ui.modal.btn.load_slicer": "G-Code aus Slicer neu laden",
        "ui.modal.btn.browse_gcode": ".gcode wählen",
        "ui.modal.btn.clear_gcode_title": "Geladenen G-Code entfernen",
        "ui.modal.stale.banner": "Geladener G-Code '{file}' passt nicht zum aktiven OrcaSlicer-Modell '{model}'. Falls nur in der Vorschau geslict wurde, G-Code exportieren (Strg+G / Ctrl+G) oder '.gcode wählen' nutzen.",
        "ui.modal.stale.clear_btn": "Veralteten G-Code entladen",
        "ui.modal.btn.slot_select_aria": "Physischen ACE Pro Slot für Extruder T{tool} wählen",
        "ui.modal.slot.opt_empty": "Leer",
        "ui.modal.slot.opt_unconf": "Bereit",
        "host.slicer_gcode_loaded": "Geslicten G-Code '{file}' geladen ({tc} Toolchanges) — bereit für Mapping.",
        "host.slicer_gcode_not_found": "Kein bereitgestellter G-Code gefunden (die orca.host-API von OrcaSlicer ist schreibgeschützt und kann keinen Export anstoßen). Einmal slicen oder '.gcode wählen' nutzen.",
        "host.gcode_browsed_loaded": "Lokalen G-Code '{file}' geladen ({tc} Toolchanges).",
        "host.gcode_cleared": "Geladene G-Code-Auswahl geleert. Bereit für den nächsten Export oder eine lokale Datei.",
        "host.gcode_read_error": "G-Code-Datei konnte nicht geladen werden: {error}",
        "host.auto_opened_preprint": "Bereit fürs Mapping",
        "host.notify_sliced_ready": "G-Code '{file}' bereit ({tc} Toolchanges) — zum Senden zu ⚕ Kobra (Kobra-Van-Kenorca) wechseln.",
        "host.notify_open_action": "Pre-Print-Dialog öffnen",
        "gate.block.mat_mismatch_unconfirmed": "Material-Abweichung zwischen Slicer-Extruder und ACE Pro Slot erkannt — explizite Bestätigung erforderlich.",
        "gate.block.stale_gcode": "Upload blockiert: geladener G-Code '{file}' ist veraltet oder passt nicht zum aktiven Projekt ({reason}). Bitte neu exportieren oder aktuellen G-Code wählen.",
        "host.gcode_binary_rejected": "'{file}' abgewiesen: Binär- oder defekte Daten sind kein gültiger G-Code.",
        "stale.reason.model_mismatch": "Projekt-/Modell-Abweichung",
        "stale.reason.plate_mismatch": "Plattennummer weicht ab",
        "stale.reason.filament_mismatch": "Filament-Preset weicht ab",
        "stale.reason.slice_newer_than_export": "Slice ist neuer als geladener G-Code",
        "stale.reason.provenance_stale": "durch neueren Slice ersetzt",
        "ui.modal.feedback.printing_busy": "Drucker ist aktuell im Status '{state}' — 'Hochladen & Druck starten' ist gesperrt. Nutze 'Nur hochladen' oder 'Exportieren...'.",
        "ui.modal.lock.printing": "Drucker ist aktuell im Status '{state}' — „Upload & Start Print“ ist gesperrt („Nur hochladen“ und „Exportieren…“ bleiben verfügbar).",
        "ui.modal.lock.offline": "Drucker ist {state} — Upload ist gesperrt bis Moonraker verbunden ist („Übernehmen & lokal patchen“ und „Exportieren…“ bleiben verfügbar).",
        "ui.modal.lock.mapping_errors": "Zielslot-Fehler im Mapping oben — leere oder ungültige Slots korrigieren, um Upload und Export freizugeben.",
        "ui.modal.lock.mismatch": "Material-Abweichung erkannt — oben per Häkchen bestätigen oder passenden Ziel-Slot wählen, um Upload und Export freizugeben.",
        "ui.ace.off_badge": "ACE Pro AUS (Externe Spule)",
        "ui.ace.connecting_badge": "ACE Verbindet…",
        "ui.ace.disconnected_badge": "ACE Getrennt",
        "ui.ace.btn_turn_off": "ACE Pro deaktivieren (zu Externer Spule wechseln)",
        "ui.ace.btn_turn_on": "ACE Pro aktivieren",
        "host.ace_pro_enabled": "ACE Pro in Klipper aktiviert (SET_PIN PIN=ACE_Pro VALUE=1).",
        "host.ace_pro_disabled": "ACE Pro in Klipper deaktiviert (SET_PIN PIN=ACE_Pro VALUE=0 — Externe Spule).",
        "ui.modal.opt.external_spool": "Externe Spule (ohne ACE)",
        "ui.modal.opt.external_spool_tip": "Druck als externe Spule behandeln: überspringt ACE-Slotgrenzen, Leer-/Materialprüfungen, ACE-Tool-Mapping und Purge-Befehle.",
        "ui.modal.opt.endless_spool": "Endless Spool",
        "ui.modal.opt.endless_spool_tip": "Wechselt bei leerer Spule automatisch auf eine passende Backup-Spule (wird erst beim Upload / Druckstart an Klipper gesendet)",
        "ui.modal.opt.endless_mode_exact": "Exakt (Material + Farbe)",
        "ui.modal.opt.endless_mode_material": "Material",
        "ui.modal.opt.endless_mode_next": "Nächste bereite",
        "ui.modal.card.backup_badge": "↻ Backup: {slots}",
        "ui.modal.card.backup_none": "↻ Kein Backup",
        "ui.modal.card.backup_tip": "Voraussichtliches Backup ({mode}): {slots} ({mat}{rem}) übernimmt wahrscheinlich bei leerer Spule (Schätzung nach ACEPRO-Regeln)",
        "ui.modal.card.backup_tip_driver": "Vom Treiber bestätigtes Backup ({mode}): {slots} ({mat}{rem}) laut ACEPRO ace_state.endless_spool_next_tool",
        "ui.modal.card.backup_short_tip": "Voraussichtliches Backup ({mode}): {slots} ({mat}{rem}) — es fehlen trotzdem noch {still_short} g für den Druck",
        "ui.modal.card.backup_none_tip": "Endless Spool ({mode}): Keine passende bereite Backup-Spule gefunden",
        "ui.modal.pred.backup_ok": "Es fehlen {deficit} g — voraussichtliches Backup {slots} ({avail} g übrig) reicht für den Druck.",
        "ui.modal.pred.backup_short": "Es fehlen {deficit} g — voraussichtliches Backup {slots} ({avail} g übrig) reicht ebenfalls nicht ({still_short} g fehlen weiter).",
        "ui.modal.pred.backup_unknown": "Es fehlen {deficit} g — voraussichtliches Backup {slots} (Restgewicht unbekannt).",
        "ui.modal.pred.backup_none": "Es fehlen {deficit} g — keine passende bereite Backup-Spule gefunden ({mode}).",
        "ui.nav.guide": "Anleitung",
        "ui.nav.guide_tip": "Kurzanleitung & FAQ öffnen",
        "ui.settings.tab_guide": "Anleitung",
        "ui.guide.intro": "Kurzübersicht zu Kobra-Van-Kenorca. Ausführliche Anleitung auf GitHub:",
        "ui.guide.github_link": "Vollständige USER_GUIDE.md auf GitHub öffnen",
        "ui.guide.s1_title": "1. Schnellstart (3 Schritte)",
        "ui.guide.s1_body": "1) Moonraker-URL unter Einstellungen → Verbindung eintragen und verbinden. 2) Modell in der OrcaSlicer-Vorschau slicen. 3) Auf Drucken klicken — Map · Check · Send öffnet sich automatisch zum Prüfen von Slots, Material und Purge.",
        "ui.guide.s2_title": "2. Slot-Bay, Auto-Feed, ACE-Schalter & Sperre",
        "ui.guide.s2_body": "Laden/Entladen/Full Unload steuern die ACE-Slots (bei Full Unload am Drucker bleiben, sonst verheddert sich die Spule!). Auto-Feed schaltet den Vorschub-Assistenten (grün = AN). Der ⏻-Schalter steuert Klippers ACE_Pro-Pin (ACE vs. externe Spule). Bei aktivem Druck sperrt die Hardware-Sperre alle Aktionen — Entsperren im orangenen Hinweis nur für manuelle Eingriffe nutzen.",
        "ui.guide.s3_title": "3. Map · Check · Send & Spoolman",
        "ui.guide.s3_body": "Ordne jeden Slicer-Extruder einem ACE-Slot (T0–T15) zu, nutze Auto-Match oder teile einen Slot für mehrere Extruder. Material-Abweichungen müssen explizit bestätigt werden. Mit optionalem Spoolman wird geprüft, ob die Restmenge (Modell + Purge) ausreicht.",
        "ui.guide.s4_title": "4. Endless Spool & Externe Spule",
        "ui.guide.s4_body": "Aktiviere Endless Spool und wähle Exakt (Material + Farbe), Material oder Nächste bereite, um die voraussichtliche Backup-Kette (T2 → T3) inkl. Restmengen-Check zu sehen. Änderungen werden erst bei Upload / Druckstart an Klipper gesendet. Bei ACE Pro AUS umgeht der Externe-Spule-Modus alle ACE-Prüfungen und trackt optional eine Spoolman-Spule.",
        "ui.guide.s5_title": "5. FAQ & Fehlerbehebung",
        "ui.guide.s5_body": "Öffnet sich Map · Check · Send beim Drucken nicht (besonders nach einem OrcaSlicer-Update), lade das Plugin unter Plugins neu und prüfe, ob KobraVanKenorca_PostProcess einmalig unter Prozess → Sonstiges → Slicing Pipeline Plugin steht. Abbrechen stoppt den Upload sicher ohne Druckstart.",
        "ui.modal.map.ext_spool_badge": "Externe Spule · ACE umgangen",
        "ui.modal.map.ext_spool_klipper": "ACE Pro ist in Klipper deaktiviert (output_pin ACE_Pro = 0) — Druck über externe Spule; ACE-Slotprüfungen und Purge-Befehle werden übersprungen.",
        "ui.modal.map.ext_spool_manual": "Externe-Spule-Modus aktiv — ACE-Slotprüfungen, Tool-Mapping und Purge-Befehle werden übersprungen (ggf. ACE_Pro in Klipper auf 0 setzen).",
        "ui.modal.map.switch_to_ace": "Zu ACE Pro wechseln",
        "ui.modal.lock.external_pin_warn": "Externe Spule aktiv, aber Klipper-Pin ACE_Pro ist noch AN — für Druck außerhalb ACE in Mainsail/Fluidd auf 0 setzen.",
        "ui.modal.ext.no_tracking": "Externe Spule (ohne Tracking)",
        "ui.modal.ext.no_tracking_sub": "Manuell · kein Spoolman-Sync",
        "ui.modal.ext.tracking_badge": "Spoolman #{id} · Auto-Track",
        "ui.modal.ext.select_aria": "Externe Spule wählen (Spoolman)",
        "moonraker.upload_403": "Moonraker-Upload abgelehnt (HTTP 403): Ein Druckauftrag läuft bereits oder die Zieldatei ist gesperrt ({detail}).",
        "host.export_done": "Gepatchten G-Code nach '{path}' exportiert ({tc} Toolchanges, {len} mm Purge).",
        "host.export_done_fallback": "Schreibzugriff auf '{requested}' verweigert — gepatchter G-Code stattdessen nach '{path}' exportiert ({tc} Toolchanges, {len} mm Purge).",
        "host.export_done_ui": "Gepatchten G-Code '{file}' exportiert ({tc} Toolchanges, {len} mm Purge).",
        "ui.export_modal.title": "Gepatchten G-Code exportieren",
        "ui.export_modal.dir_label": "Zielordner",
        "ui.export_modal.btn_downloads": "Downloads",
        "ui.export_modal.btn_plugin_dir": "Plugin-Ordner",
        "ui.export_modal.file_label": "Dateiname",
        "ui.export_modal.hint": "Wähle Zielordner und Dateiname. Falls die OrcaSlicer-Sandbox das Schreiben außerhalb des Plugin-Speichers blockiert, wird automatisch im Plugin-Ordner exported_gcode gespeichert und der genaue Pfad angezeigt.",
        "ui.export_modal.btn_save": "Speichern",
        "host.slice_invalidated": "Aktive Slicer-Vorschau wurde entfernt oder ungültig — Slice-Status zurückgesetzt und Pre-Print-Dialog geschlossen.",
        "host.slice_invalidated_presets": "Slicer-Presets oder Filamente geändert — vorheriger Slice wurde verworfen. Bitte in OrcaSlicer neu slicen.",
        "ui.notice.no_gcode_slice_first": "Kein gültiger geslicter G-Code → Bitte Platte in der Vorschau slicen oder",
        "ui.notice.slice_outdated_presets": "Slicer-Einstellungen oder Filamente wurden in OrcaSlicer geändert — bitte neu slicen (seit der letzten Änderung noch nicht neu gesliced).",
        "ui.notice.slicing_in_progress": "Slicing läuft in OrcaSlicer… Der neue G-Code wird nach Abschluss automatisch geladen.",
        "ui.notice.btn_browse_gcode": ".gcode wählen...",
        "ui.notice.btn_dismiss": "Schließen",
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


# 4. Color Math, Flush Calculation & Header Parsing
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


_HEX_COLOR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")


def rgb_to_hex(rgb: Any) -> str:
    """Convert [R, G, B] list or hex string to normalized '#RRGGBB'."""
    if isinstance(rgb, str):
        val = rgb.strip()
        if _HEX_COLOR_RE.match(val):
            return f"#{val.lstrip('#')[:6].upper()}"
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

    has_purge_markers = any(PURGE_MARKER_TAG in ln for ln in lines)
    has_artifacts = has_header or has_purge_markers or any(
        ln.lstrip().startswith("; G31 ; skipped by ACE Plugin")
        or "@ORCA_ORIG_TOOL:" in ln
        or "@ORCA_ACE_PLUGIN_CUT_TIP" in ln
        or "@ORCA_ACE_PLUGIN_EXT_SPOOL" in ln
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
    orig_pre_purge_raw = meta.get("origPrePurgeLines")
    orig_pre_purge_map: Dict[str, str] = (
        {str(k): str(v) for k, v in orig_pre_purge_raw.items() if isinstance(v, str)}
        if isinstance(orig_pre_purge_raw, dict)
        else {}
    )
    inv_map = meta.get("_legacyInvMap", {})
    purge_tc_idx = 0

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

        if "@ORCA_ACE_PLUGIN_CUT_TIP" in ln or "@ORCA_ACE_PLUGIN_EXT_SPOOL" in ln:
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
            is_plugin_purge = (PURGE_MARKER_TAG in ln) if has_purge_markers else _purge_precedes_toolchange(lines, idx)
            if is_plugin_purge:
                restored_pre = orig_pre_purge_map.get(str(purge_tc_idx))
                purge_tc_idx += 1
                if restored_pre:
                    cleaned.append(restored_pre)
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
    densities: List[float] = []
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

        m = FILAMENT_DENSITY_RE.match(ln)
        if m:
            try:
                densities = parse_number_list(m.group(1))
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
        dens_raw = densities[idx] if idx < len(densities) else (densities[0] if densities else 0.0)
        dens = round(float(dens_raw), 4) if (dens_raw and math.isfinite(float(dens_raw)) and float(dens_raw) > 0.0) else None
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
                "density": dens,
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


def _normalize_material_family(material: str) -> str:
    """
    Normalize material strings such as `PLA+`, `Silk PLA`, `PLA Matte`, `ePLA`,
    `PETG-CF`, or `PA6-CF` onto their base polymer family so variants match cleanly
    while genuine cross-polymer swaps (e.g. `PLA` vs `PETG`) stay flagged.
    """
    raw = str(material or "").strip().upper()
    if not raw:
        return ""
    tokens = [tok for tok in re.split(r"[^A-Z0-9]+", raw) if tok]
    if "PETG" in tokens or "PCTG" in tokens or "PETG" in raw:
        return "PETG"
    if "PLA" in tokens or "EPLA" in tokens or re.search(r"(?:^|[^A-Z])PLA(?:[^A-Z]|$)", raw):
        return "PLA"
    if "ASA" in tokens:
        return "ASA"
    if "ABS" in tokens:
        return "ABS"
    if "TPU" in tokens or "TPE" in tokens:
        return "TPU"
    if "PVA" in tokens:
        return "PVA"
    if "BVOH" in tokens:
        return "BVOH"
    if "HIPS" in tokens:
        return "HIPS"
    if "PC" in tokens and "ABS" in tokens:
        return "PC-ABS"
    if "PC" in tokens or "POLYCARBONATE" in tokens:
        return "PC"
    if "NYLON" in tokens or "PA" in tokens or any(t.startswith(("PA6", "PA12")) for t in tokens):
        return "PA"
    return re.sub(r"[^A-Z0-9]+", "", raw)


def _materials_compatible(s_mat: str, p_mat: str) -> Tuple[bool, float]:
    """Return `(is_compatible, penalty)` for comparing slicer vs ACE slot material."""
    s_u = str(s_mat or "").strip().upper()
    p_u = str(p_mat or "").strip().upper()
    if s_u and p_u:
        if s_u == p_u:
            return True, 0.0
        if s_u.startswith(p_u) or p_u.startswith(s_u):
            return True, 2500.0
        s_fam = _normalize_material_family(s_u)
        p_fam = _normalize_material_family(p_u)
        if s_fam and p_fam and s_fam == p_fam:
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


class SlotBoundsError(ValueError):
    """Raised when a slicer tool maps outside known ACE slots while ACE Pro is active."""


def is_ace_pro_disabled_in_status(ace_status: Optional[Dict[str, Any]]) -> bool:
    """Return True when Klipper/Moonraker status explicitly reports `ace_pro_enabled == False`."""
    if not isinstance(ace_status, dict):
        return False
    c_state = str(ace_status.get("connection_state") or "").strip().lower()
    if c_state in ("offline", "unconfigured"):
        return False
    return ace_status.get("ace_pro_enabled") is False


def is_ace_bypass_active(
    state: Optional["PluginState"] = None,
    ace_status: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Return True when External Spool / ACE-bypass mode applies (either Klipper reports
    `ace_pro_enabled == False`, i.e. `output_pin ACE_Pro = 0`, or `external_spool_mode` is set).
    """
    st = state or globals().get("GLOBAL_STATE")
    eff_status = ace_status
    if eff_status is None and st is not None:
        with st._lock:
            eff_status = st.last_ace_status
    if is_ace_pro_disabled_in_status(eff_status):
        return True
    if st is not None and bool(getattr(st, "external_spool_mode", False)):
        return True
    return False


def find_slot_bounds_errors(
    slicer_filaments: Optional[List[Dict[str, Any]]],
    tool_map: Optional[Dict[Any, Any]],
    max_valid_slots: Optional[int],
    *,
    lang: Optional[str] = None,
    g9111_mapped_tool: Optional[int] = None,
    used_tools: Optional[List[int]] = None,
) -> Tuple[List[str], List[Dict[str, Any]], List[int]]:
    """
    Validate that every active mapped ACE slot falls within `0 .. max_valid_slots - 1`
    when `max_valid_slots` is known. Returns `(errors, error_items, out_of_range_slots)`.
    """
    if max_valid_slots is None or max_valid_slots <= 0:
        return [], [], []
    norm_map: Dict[int, int] = {}
    if tool_map:
        for k, v in tool_map.items():
            try:
                norm_map[int(k)] = int(v)
            except (ValueError, TypeError):
                pass
    errors: List[str] = []
    error_items: List[Dict[str, Any]] = []
    out_of_range_slots: List[int] = []
    seen_errs: set = set()

    def _record_oob(s_tool: int, mapped: int) -> None:
        if mapped < 0 or mapped >= max_valid_slots:
            if mapped not in out_of_range_slots:
                out_of_range_slots.append(mapped)
            sig = (s_tool, mapped)
            if sig not in seen_errs:
                seen_errs.add(sig)
                params = {"slicer_tool": s_tool, "mapped": mapped, "max_slot": max_valid_slots - 1}
                error_items.append({"key": "val.map.not_present", "params": params})
                errors.append(t("val.map.not_present", lang=lang, **params))

    checked_tools: set = set()
    for fil in slicer_filaments or []:
        if not isinstance(fil, dict) or not fil.get("used", True):
            continue
        s_tool = int(fil.get("slicer_tool", 0))
        checked_tools.add(s_tool)
        _record_oob(s_tool, norm_map.get(s_tool, s_tool))
    for s_tool in used_tools or []:
        try:
            st_i = int(s_tool)
        except (ValueError, TypeError):
            continue
        if st_i not in checked_tools:
            checked_tools.add(st_i)
            _record_oob(st_i, norm_map.get(st_i, st_i))
    if g9111_mapped_tool is not None:
        try:
            gm = int(g9111_mapped_tool)
            if gm < 0 or gm >= max_valid_slots:
                _record_oob(gm, gm)
        except (ValueError, TypeError):
            pass
    return errors, error_items, out_of_range_slots


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
    low_spools: List[Dict[str, Any]] = []
    phys_to_slicer: Dict[int, List[int]] = {}
    phys_used_g: Dict[int, float] = {}
    has_any_usage_data = any(
        float(f.get("used_g") or 0.0) > 0.0
        or float(f.get("used_mm") or 0.0) > 0.0
        or float(f.get("used_m") or 0.0) > 0.0
        or float(f.get("used_cm3") or 0.0) > 0.0
        for f in (slicer_filaments or [])
        if isinstance(f, dict)
    )

    for fil in slicer_filaments or []:
        if not isinstance(fil, dict) or fil.get("used") is False:
            continue
        try:
            fil_used_g = max(0.0, float(fil.get("used_g") or 0.0))
            fil_used_mm = max(0.0, float(fil.get("used_mm") or 0.0))
            fil_used_m = max(0.0, float(fil.get("used_m") or 0.0))
            fil_used_cm3 = max(0.0, float(fil.get("used_cm3") or 0.0))
        except (ValueError, TypeError):
            fil_used_g = fil_used_mm = fil_used_m = fil_used_cm3 = 0.0
        if (
            has_any_usage_data
            and fil.get("used_g") is not None
            and fil_used_g <= 0.0
            and fil_used_mm <= 0.0
            and fil_used_m <= 0.0
            and fil_used_cm3 <= 0.0
        ):
            continue
        s_tool = int(fil.get("slicer_tool", 0))
        mapped = norm_map.get(s_tool, s_tool)
        s_mat = str(fil.get("material", "")).strip().upper()
        phys_to_slicer.setdefault(mapped, []).append(s_tool)
        phys_used_g[mapped] = phys_used_g.get(mapped, 0.0) + fil_used_g

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
        req_g = phys_used_g.get(phys_tool, 0.0)
        slot_obj = slot_by_tool.get(phys_tool)
        if req_g > 0.0 and isinstance(slot_obj, dict):
            sp_info = slot_obj.get("spoolman")
            if isinstance(sp_info, dict) and sp_info.get("conflict"):
                params = {
                    "mapped": phys_tool,
                    "sku": str(sp_info.get("sku") or slot_obj.get("sku") or ""),
                    "count": int(sp_info.get("conflict_count") or 0),
                }
                warning_items.append({"key": "val.map.spoolman_conflict", "params": params})
                warnings.append(t("val.map.spoolman_conflict", lang=lang, **params))
            rem_raw = sp_info.get("remaining_weight") if isinstance(sp_info, dict) else None
            if rem_raw is not None and not isinstance(rem_raw, bool):
                try:
                    rem_g = float(rem_raw)
                except (ValueError, TypeError):
                    rem_g = -1.0
                if rem_g >= 0.0 and req_g > rem_g + 1e-6:
                    sp_name = str(
                        sp_info.get("display_name")
                        or sp_info.get("filament_name")
                        or f"T{phys_tool}"
                    )
                    low_spools.append(
                        {
                            "mapped_slot": phys_tool,
                            "extruders": list(s_tools),
                            "required_g": round(req_g, 1),
                            "remaining_g": round(rem_g, 1),
                            "display_name": sp_name,
                        }
                    )
                    params = {
                        "mapped": phys_tool,
                        "spool": sp_name,
                        "required_g": f"{req_g:.1f}",
                        "remaining_g": f"{rem_g:.1f}",
                    }
                    warning_items.append({"key": "val.map.low_spool", "params": params})
                    warnings.append(t("val.map.low_spool", lang=lang, **params))

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
        "low_spools": low_spools,
    }


def _resolve_filament_density_g_cm3(
    fil: Dict[str, Any],
    sp_info: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """
    Resolve filament density (g/cm³) strictly from actual known G-code or Spoolman metadata.
    Never invents or assumes a default density.
    """
    for raw in (fil.get("density"), sp_info.get("density") if isinstance(sp_info, dict) else None):
        if raw is not None and not isinstance(raw, bool):
            try:
                val = float(raw)
                if math.isfinite(val) and val > 0.0:
                    return val
            except (ValueError, TypeError):
                pass
    try:
        used_g = float(fil.get("used_g") or 0.0)
        used_cm3 = float(fil.get("used_cm3") or 0.0)
        used_mm = float(fil.get("used_mm") or 0.0)
        diam = float(fil.get("diameter") or DEFAULT_FILAMENT_DIAMETER)
    except (ValueError, TypeError):
        return None
    if math.isfinite(used_g) and used_g > 0.0:
        if math.isfinite(used_cm3) and used_cm3 > 0.0:
            return used_g / used_cm3
        if math.isfinite(used_mm) and used_mm > 0.0 and math.isfinite(diam) and diam > 0.0:
            cm3 = (used_mm * area_mm2(diam)) / 1000.0
            if cm3 > 0.0:
                return used_g / cm3
    return None


def _resolve_filament_print_usage_g(
    fil: Dict[str, Any],
    density: Optional[float],
) -> Optional[float]:
    """Resolve expected print extrusion weight (g) from G-code filament stats without inventing density."""
    try:
        used_g = float(fil.get("used_g") or 0.0)
        if math.isfinite(used_g) and used_g > 0.0:
            return used_g
        if density is not None and math.isfinite(density) and density > 0.0:
            used_cm3 = float(fil.get("used_cm3") or 0.0)
            if math.isfinite(used_cm3) and used_cm3 > 0.0:
                return used_cm3 * density
            used_mm = float(fil.get("used_mm") or 0.0)
            diam = float(fil.get("diameter") or DEFAULT_FILAMENT_DIAMETER)
            if math.isfinite(used_mm) and used_mm > 0.0 and math.isfinite(diam) and diam > 0.0:
                return ((used_mm * area_mm2(diam)) / 1000.0) * density
    except (ValueError, TypeError):
        return None
    return None


def compute_preprint_filament_prediction(
    slicer_filaments: Optional[List[Dict[str, Any]]],
    tool_map: Optional[Dict[Any, Any]],
    ace_slots: Optional[List[Dict[str, Any]]],
    postprocess_info: Optional[Dict[str, Any]] = None,
    *,
    reserve_g: float = DEFAULT_PREDICTION_RESERVE_G,
    startup_purge_length_mm: float = DEFAULT_STARTUP_PURGE_LENGTH_MM,
    skip_purge_for_already_loaded_tool: bool = True,
    ace_status: Optional[Dict[str, Any]] = None,
    has_gcode: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Compute per-slot pre-print filament remaining prediction combining:
    - Slicer print usage (`used_g` or exact unit conversion via known density/diameter)
    - Klipper `ACE_SET_PURGE_AMOUNT` purge usage from `postprocess_info['events']` (no double counting)
    - Optional G9111 startup purge estimate when the start slot is not yet loaded (or skip-purge is disabled)
    - Spoolman remaining weight on the mapped physical ACE Pro slot.
    """
    try:
        res_val = float(reserve_g if reserve_g is not None else DEFAULT_PREDICTION_RESERVE_G)
        if not math.isfinite(res_val) or res_val < 0.0:
            res_val = DEFAULT_PREDICTION_RESERVE_G
    except (ValueError, TypeError):
        res_val = DEFAULT_PREDICTION_RESERVE_G
    res_val = round(res_val, 2)

    try:
        startup_len_mm = float(
            startup_purge_length_mm
            if startup_purge_length_mm is not None
            else DEFAULT_STARTUP_PURGE_LENGTH_MM
        )
        if not math.isfinite(startup_len_mm) or startup_len_mm < 0.0:
            startup_len_mm = DEFAULT_STARTUP_PURGE_LENGTH_MM
    except (ValueError, TypeError):
        startup_len_mm = DEFAULT_STARTUP_PURGE_LENGTH_MM
    startup_len_mm = round(max(0.0, min(1000.0, startup_len_mm)), 1)

    fils = [f for f in (slicer_filaments or []) if isinstance(f, dict)]
    pp = postprocess_info if isinstance(postprocess_info, dict) else {}
    inferred_has_gcode = bool(pp) or any(
        float(f.get("used_g") or 0.0) > 0.0
        or float(f.get("used_mm") or 0.0) > 0.0
        or float(f.get("used_cm3") or 0.0) > 0.0
        for f in fils
    )
    if has_gcode is False or (has_gcode is None and not inferred_has_gcode):
        return {
            "has_gcode": False,
            "reserve_g": res_val,
            "overall_status": "empty",
            "counts": {"sufficient": 0, "low": 0, "insufficient": 0, "unknown": 0},
            "slots": [],
        }

    norm_map: Dict[int, int] = {}
    if isinstance(tool_map, dict):
        for k, v in tool_map.items():
            try:
                norm_map[int(k)] = int(v)
            except (ValueError, TypeError):
                pass

    slot_by_tool: Dict[int, Dict[str, Any]] = {}
    for idx, slot in enumerate(ace_slots or []):
        if isinstance(slot, dict):
            try:
                t_id = int(slot.get("tool", slot.get("index", idx)))
            except (ValueError, TypeError):
                t_id = idx
            slot_by_tool[t_id] = slot

    used_fils = [f for f in fils if f.get("used", True)]
    fil_by_tool: Dict[int, Dict[str, Any]] = {}
    slot_order: List[int] = []
    tools_for_slot: Dict[int, List[int]] = {}

    for f in used_fils:
        try:
            s_tool = int(f.get("slicer_tool", 0))
        except (ValueError, TypeError):
            continue
        fil_by_tool[s_tool] = f
        phys = norm_map.get(s_tool, s_tool)
        if phys not in tools_for_slot:
            tools_for_slot[phys] = []
            slot_order.append(phys)
        tools_for_slot[phys].append(s_tool)

    events = pp.get("events") if isinstance(pp.get("events"), list) else []
    flush_incomplete = (
        str(pp.get("flush_status") or "") == "FLUSH_DATA_INCOMPLETE"
        or (pp.get("flush_data_complete") is False and len(used_fils) > 1)
    )

    purge_by_slot: Dict[int, float] = {phys: 0.0 for phys in slot_order}
    purge_err_by_slot: Dict[int, str] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        try:
            s_tool = int(ev.get("new_slicer_tool", 0))
            vol_mm3 = float(ev.get("volume_mm3") or 0.0)
        except (ValueError, TypeError):
            continue
        phys = norm_map.get(s_tool, s_tool)
        if phys not in purge_by_slot:
            continue
        if not math.isfinite(vol_mm3) or vol_mm3 <= 0.0:
            continue
        fil_obj = fil_by_tool.get(s_tool, {})
        slot_obj = slot_by_tool.get(phys)
        sp_obj = slot_obj.get("spoolman") if isinstance(slot_obj, dict) and isinstance(slot_obj.get("spoolman"), dict) else None
        dens = _resolve_filament_density_g_cm3(fil_obj, sp_obj)
        if dens is None:
            purge_err_by_slot.setdefault(phys, "no_density")
            continue
        purge_by_slot[phys] += (vol_mm3 / 1000.0) * dens

    if flush_incomplete:
        for phys in slot_order:
            purge_err_by_slot[phys] = "flush_incomplete"

    startup_est: Dict[str, Any] = {
        "active": False,
        "skipped": False,
        "slot": None,
        "length_mm": 0.0,
        "configured_length_mm": startup_len_mm,
        "grams": 0.0,
        "reason": "disabled" if startup_len_mm <= 0.0 else "no_g9111",
    }
    raw_g9111_tool = pp.get("g9111StartSlicerTool")
    if raw_g9111_tool is not None and startup_len_mm > 0.0:
        try:
            g9111_s_tool = int(raw_g9111_tool)
        except (ValueError, TypeError):
            g9111_s_tool = None
        if g9111_s_tool is not None:
            start_phys = norm_map.get(g9111_s_tool, g9111_s_tool)
            loaded_slot: Optional[int] = None
            if isinstance(ace_status, dict) and ace_status:
                if ace_status.get("connected") is True:
                    try:
                        c_idx = int(ace_status.get("current_index", -1))
                        if c_idx >= 0:
                            loaded_slot = c_idx
                    except (ValueError, TypeError):
                        loaded_slot = None
            else:
                for sl_obj in ace_slots or []:
                    if not isinstance(sl_obj, dict):
                        continue
                    if sl_obj.get("is_loaded") is True or str(sl_obj.get("status") or "").strip().lower() == "loaded":
                        try:
                            loaded_slot = int(sl_obj.get("tool", sl_obj.get("index", -1)))
                        except (ValueError, TypeError):
                            loaded_slot = None
                        if loaded_slot is not None and loaded_slot >= 0:
                            break
                        loaded_slot = None

            start_slot_obj = slot_by_tool.get(start_phys)
            start_slot_loaded_ok = bool(
                start_slot_obj is not None
                and str(start_slot_obj.get("status") or "empty").strip().lower() != "empty"
            )
            if (
                bool(skip_purge_for_already_loaded_tool)
                and loaded_slot is not None
                and loaded_slot == start_phys
                and start_slot_loaded_ok
            ):
                startup_est = {
                    "active": False,
                    "skipped": True,
                    "slot": start_phys,
                    "length_mm": 0.0,
                    "configured_length_mm": startup_len_mm,
                    "grams": 0.0,
                    "reason": "same_tool_loaded",
                }
            else:
                fil_obj = fil_by_tool.get(g9111_s_tool, {})
                sp_obj = (
                    start_slot_obj.get("spoolman")
                    if isinstance(start_slot_obj, dict) and isinstance(start_slot_obj.get("spoolman"), dict)
                    else None
                )
                try:
                    diam_val = float(
                        fil_obj.get("diameter")
                        or pp.get("filament_diameter")
                        or DEFAULT_FILAMENT_DIAMETER
                    )
                    if not math.isfinite(diam_val) or diam_val <= 0.0:
                        diam_val = DEFAULT_FILAMENT_DIAMETER
                except (ValueError, TypeError):
                    diam_val = DEFAULT_FILAMENT_DIAMETER
                startup_vol_mm3 = startup_len_mm * area_mm2(diam_val)
                dens = _resolve_filament_density_g_cm3(fil_obj, sp_obj)
                startup_g: Optional[float] = None
                if dens is None:
                    if start_phys in purge_by_slot:
                        purge_err_by_slot.setdefault(start_phys, "no_density")
                else:
                    raw_startup_g = (startup_vol_mm3 / 1000.0) * dens
                    startup_g = round(raw_startup_g, 2)
                    if start_phys in purge_by_slot and not purge_err_by_slot.get(start_phys):
                        purge_by_slot[start_phys] += raw_startup_g
                startup_est = {
                    "active": True,
                    "skipped": False,
                    "slot": start_phys,
                    "length_mm": startup_len_mm,
                    "configured_length_mm": startup_len_mm,
                    "grams": startup_g,
                    "reason": (
                        "skip_disabled"
                        if not bool(skip_purge_for_already_loaded_tool)
                        else "tool_change_or_unloaded"
                    ),
                }

    counts = {"sufficient": 0, "low": 0, "insufficient": 0, "unknown": 0}
    slot_entries: List[Dict[str, Any]] = []

    for phys in slot_order:
        slot_obj = slot_by_tool.get(phys)
        sp_obj = (
            slot_obj.get("spoolman")
            if isinstance(slot_obj, dict) and isinstance(slot_obj.get("spoolman"), dict)
            else None
        )
        s_tools = tools_for_slot[phys]

        # Print usage summed across all used Slicer extruders mapped to this ACE slot
        print_total: Optional[float] = 0.0
        for s_t in s_tools:
            fil_obj = fil_by_tool.get(s_t, {})
            dens = _resolve_filament_density_g_cm3(fil_obj, sp_obj)
            p_g = _resolve_filament_print_usage_g(fil_obj, dens)
            if p_g is None:
                print_total = None
                break
            print_total += p_g

        purge_err = purge_err_by_slot.get(phys, "")
        purge_total: Optional[float] = None if purge_err else round(purge_by_slot.get(phys, 0.0), 2)
        if print_total is not None:
            print_total = round(print_total, 2)
        total_usage: Optional[float] = (
            round(print_total + purge_total, 2)
            if (print_total is not None and purge_total is not None)
            else None
        )

        # Available spool weight & ambiguity check
        avail_g: Optional[float] = None
        avail_reason = ""
        if slot_obj is None:
            avail_reason = "slot_missing"
        elif str(slot_obj.get("status") or "empty").strip().lower() == "empty":
            avail_reason = "slot_empty"
        elif sp_obj is None:
            avail_reason = "no_spool"
        elif sp_obj.get("conflict"):
            avail_reason = "conflict"
        elif sp_obj.get("weight_status") == "missing" or sp_obj.get("remaining_weight") is None or isinstance(sp_obj.get("remaining_weight"), bool):
            avail_reason = "no_weight"
        else:
            try:
                rem_val = float(sp_obj["remaining_weight"])
                if math.isfinite(rem_val) and rem_val >= 0.0:
                    avail_g = round(rem_val, 2)
                else:
                    avail_reason = "no_weight"
            except (ValueError, TypeError):
                avail_reason = "no_weight"

        remaining_after_g: Optional[float] = None
        deficit_g = 0.0
        reason = ""
        if avail_g is None:
            status = "unknown"
            reason = avail_reason or "no_weight"
        elif print_total is None:
            status = "unknown"
            reason = "no_usage"
        elif purge_total is None:
            status = "unknown"
            reason = purge_err or "no_density"
        else:
            remaining_after_g = round(avail_g - total_usage, 2)
            if remaining_after_g < -1e-6:
                status = "insufficient"
                deficit_g = round(abs(remaining_after_g), 2)
            elif remaining_after_g < res_val - 1e-6:
                status = "low"
            else:
                status = "sufficient"

        counts[status] += 1

        # Percentages relative to spool capacity (or available weight if capacity absent) for visual bar
        cap_raw = sp_obj.get("capacity_weight") if isinstance(sp_obj, dict) else None
        try:
            cap_val = float(cap_raw) if (cap_raw is not None and not isinstance(cap_raw, bool)) else 0.0
        except (ValueError, TypeError):
            cap_val = 0.0
        scale_max = cap_val if cap_val > 0.0 else (avail_g if (avail_g is not None and avail_g > 0.0) else 0.0)
        pct_before: Optional[float] = None
        pct_after: Optional[float] = None
        if scale_max > 0.0 and avail_g is not None:
            pct_before = round(max(0.0, min(100.0, (avail_g / scale_max) * 100.0)), 1)
            if remaining_after_g is not None:
                pct_after = round(max(0.0, min(100.0, (max(0.0, remaining_after_g) / scale_max) * 100.0)), 1)

        first_fil = fil_by_tool.get(s_tools[0], {}) if s_tools else {}
        mat = str((slot_obj.get("material") if isinstance(slot_obj, dict) else "") or first_fil.get("material") or "").strip()
        hex_col = rgb_to_hex(
            (sp_obj.get("color_hex") if isinstance(sp_obj, dict) and not sp_obj.get("conflict") and sp_obj.get("color_hex") else "")
            or (slot_obj.get("hex_color") if isinstance(slot_obj, dict) else "")
            or first_fil.get("color")
            or "#64748B"
        )
        spool_name = str(
            (sp_obj.get("display_name") or sp_obj.get("filament_name") or "")
            if isinstance(sp_obj, dict) and not sp_obj.get("conflict")
            else ""
        ).strip()

        slot_entries.append(
            {
                "slot_tool": phys,
                "slot_label": f"T{phys}",
                "material": mat,
                "hex_color": hex_col,
                "slicer_tools": list(s_tools),
                "spool_id": sp_obj.get("spool_id") if isinstance(sp_obj, dict) and not sp_obj.get("conflict") else None,
                "spool_name": spool_name,
                "assignment_source": str(sp_obj.get("assignment_source") or "") if isinstance(sp_obj, dict) else "",
                "multi_match": bool(sp_obj.get("multi_match")) if isinstance(sp_obj, dict) else False,
                "available_weight_g": avail_g,
                "print_usage_g": print_total,
                "purge_usage_g": purge_total,
                "total_usage_g": total_usage,
                "remaining_after_g": remaining_after_g,
                "deficit_g": deficit_g,
                "remaining_pct_before": pct_before,
                "remaining_pct_after": pct_after,
                "status": status,
                "reason": reason,
            }
        )

    if not slot_entries:
        overall = "empty"
    elif counts["insufficient"] > 0:
        overall = "insufficient"
    elif counts["unknown"] > 0:
        overall = "unknown"
    elif counts["low"] > 0:
        overall = "low"
    else:
        overall = "sufficient"

    return {
        "has_gcode": True,
        "reserve_g": res_val,
        "overall_status": overall,
        "counts": counts,
        "slots": slot_entries,
        "startup_purge_estimate": startup_est,
    }


# 5. Core G-Code Post-Processor
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


def compute_effective_toolchanges(
    info: Optional[Dict[str, Any]],
    ace_status: Optional[Dict[str, Any]] = None,
    skip_purge_for_already_loaded_tool: Optional[bool] = None,
    force_initial_purge: Optional[bool] = None,
    tool_map: Optional[Dict[Any, Any]] = None,
) -> int:
    """
    Compute the effective number of physical filament toolchanges for a sliced G-code.
    When `Skip Purge` (`SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1`) is enabled, `force_initial_purge`
    is False, and the live ACE Pro already has the mapped start slot loaded, the skipped initial
    load event is not counted as a toolchange (e.g. single-color print -> `0` toolchanges).
    """
    if not info or not isinstance(info, dict):
        return 0
    raw_tc = int(
        info.get("raw_toolchanges")
        if info.get("raw_toolchanges") is not None
        else (info.get("toolchanges") or 0)
    )
    if raw_tc <= 0:
        return 0

    skip_purge = (
        bool(info.get("skipPurgeForAlreadyLoadedTool", True))
        if skip_purge_for_already_loaded_tool is None
        else bool(skip_purge_for_already_loaded_tool)
    )
    force_init = (
        bool(info.get("forceInitialPurge", False))
        if force_initial_purge is None
        else bool(force_initial_purge)
    )
    if not skip_purge or force_init:
        return raw_tc

    if not isinstance(ace_status, dict) or not ace_status.get("connected", False):
        return raw_tc
    raw_cur = ace_status.get("current_index")
    if raw_cur is None or isinstance(raw_cur, bool):
        return raw_tc
    try:
        cur_idx = int(raw_cur)
    except (ValueError, TypeError):
        return raw_tc
    if cur_idx < 0:
        return raw_tc

    norm_map: Dict[int, int] = {}
    src_map = tool_map if tool_map is not None else (info.get("toolMap") or {})
    if isinstance(src_map, dict):
        for k, v in src_map.items():
            try:
                norm_map[int(k)] = int(v)
            except (ValueError, TypeError):
                pass

    events = info.get("events") if isinstance(info.get("events"), list) else []
    raw_g9111 = info.get("g9111StartSlicerTool")
    start_slicer: Optional[int] = None
    if raw_g9111 is not None:
        try:
            start_slicer = int(raw_g9111)
        except (ValueError, TypeError):
            start_slicer = None
    elif events and isinstance(events[0], dict) and events[0].get("new_slicer_tool") is not None:
        try:
            start_slicer = int(events[0]["new_slicer_tool"])
        except (ValueError, TypeError):
            start_slicer = None
    if start_slicer is None:
        return raw_tc

    start_phys = norm_map.get(start_slicer, start_slicer)
    if cur_idx != start_phys:
        return raw_tc

    # Verify the first event (if present) is indeed the initial start-tool load (not a switch to a different tool)
    if events and isinstance(events[0], dict):
        first_ev = events[0]
        try:
            ev_new = int(first_ev.get("new_slicer_tool", start_slicer))
        except (ValueError, TypeError):
            ev_new = start_slicer
        ev_old = first_ev.get("old_slicer_tool")
        if ev_new != start_slicer:
            return raw_tc
        if ev_old is not None:
            try:
                if int(ev_old) != start_slicer:
                    return raw_tc
            except (ValueError, TypeError):
                pass

    return max(0, raw_tc - 1)


def remap_flush_info(
    info: Dict[str, Any],
    tool_map: Dict[Any, Any],
    slicer_filaments: Optional[List[Dict[str, Any]]] = None,
    ace_slots: Optional[List[Dict[str, Any]]] = None,
    lang: Optional[str] = None,
    ace_status: Optional[Dict[str, Any]] = None,
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
    raw_tc = int(
        updated.get("raw_toolchanges")
        if updated.get("raw_toolchanges") is not None
        else (updated.get("toolchanges") or len(new_events))
    )
    updated["raw_toolchanges"] = raw_tc
    eff_tc = compute_effective_toolchanges(
        updated,
        ace_status=ace_status,
        tool_map=norm_map,
    )
    updated["effective_toolchanges"] = eff_tc
    if ace_status is not None:
        updated["toolchanges"] = eff_tc

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
    current_loaded_slot: Optional[int] = None,
    ace_bypass: bool = False,
    external_spool_id: Optional[int] = None,
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
    seen_matrix = False
    seen_vector = False
    invalid_matrix_size = False

    def add_warning(key: str, **params: Any) -> None:
        warning_items.append({"key": key, "params": params})
        warnings.append(t(key, lang=lang, **params))

    # Normalize tool_map keys/values to int (identity / empty when External Spool bypasses ACE)
    normalized_tool_map: Dict[int, int] = {}
    if tool_map and not ace_bypass:
        for k, v in tool_map.items():
            try:
                normalized_tool_map[int(k)] = int(v)
            except (ValueError, TypeError):
                pass

    def map_tool(slicer_tool: int) -> int:
        if ace_bypass:
            return slicer_tool
        return normalized_tool_map.get(slicer_tool, slicer_tool)

    # Pass 1: Parse and validate flush metadata and filament diameter from comments
    for ln in lines:
        m = MUL_RE.match(ln)
        if m:
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
    orig_pre_purge_lines: Dict[str, str] = {}
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
                if not ace_bypass:
                    rewritten_ln = G9111_TOOL_PARAM_RE.sub(
                        lambda match: f"{match.group(1)}{mapped_start_tool}",
                        rewritten_ln,
                        count=1,
                    )

            if not ace_bypass:
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
            if ace_bypass and external_spool_id and int(external_spool_id) > 0:
                if body_lines and not body_lines[-1].endswith("\n"):
                    body_lines[-1] += "\n"
                body_lines.append(f"SET_ACTIVE_SPOOL ID={int(external_spool_id)} ; @ORCA_ACE_PLUGIN_EXT_SPOOL\n")
            continue

        # 2) Standalone SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=0|1 line (as in OrcaSlicer/refs/test_sample.gcode)
        m_skip = SKIP_PURGE_LINE_RE.match(ln)
        if m_skip and not ace_bypass:
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

            # Validate flush data completeness & tool range (skipped in External Spool / ACE-bypass mode)
            if not ace_bypass:
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

            if ace_bypass:
                body_lines.append(ln)
                if external_spool_id and int(external_spool_id) > 0:
                    if body_lines and not body_lines[-1].endswith("\n"):
                        body_lines[-1] += "\n"
                    body_lines.append(f"SET_ACTIVE_SPOOL ID={int(external_spool_id)} ; @ORCA_ACE_PLUGIN_EXT_SPOOL\n")
            else:
                # If an existing ACE_SET_PURGE_AMOUNT immediately precedes this toolchange, replace it
                # reversibly so neither duplicate purge lines nor permanent user line loss can occur.
                prev_exec_idx = len(body_lines) - 1
                while prev_exec_idx >= 0 and (
                    not body_lines[prev_exec_idx].strip()
                    or body_lines[prev_exec_idx].lstrip().startswith(";")
                ):
                    prev_exec_idx -= 1
                if (
                    prev_exec_idx >= 0
                    and body_lines[prev_exec_idx].lstrip()[:20].upper().startswith("ACE_SET_PURGE_AMOUNT")
                    and ACE_SET_PURGE_LINE_RE.match(body_lines[prev_exec_idx])
                ):
                    orig_pre_purge_lines[str(toolchange_count)] = body_lines[prev_exec_idx]
                    del body_lines[prev_exec_idx]

                if body_lines and not body_lines[-1].endswith("\n"):
                    body_lines[-1] += "\n"
                body_lines.append(f"{leading_ws}ACE_SET_PURGE_AMOUNT PURGELENGTH={length_str} ; {PURGE_MARKER_TAG}\n")
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
                    "purge_length_mm": 0.0 if ace_bypass else float(length_str),
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

    if (not ace_bypass) and len(used_slicer_tools_in_exec) > 1 and (not matrix or invalid_matrix_size):
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
        "raw_toolchanges": toolchange_count,
        "effective_toolchanges": toolchange_count,
        "purge_commands_inserted": 0 if ace_bypass else toolchange_count,
        "aceBypass": bool(ace_bypass),
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
    if current_loaded_slot is not None and not isinstance(current_loaded_slot, bool):
        try:
            loaded_idx = int(current_loaded_slot)
        except (ValueError, TypeError):
            loaded_idx = -1
        if loaded_idx >= 0:
            eff_tc = compute_effective_toolchanges(
                info,
                ace_status={"connected": True, "current_index": loaded_idx},
                skip_purge_for_already_loaded_tool=bool(skip_purge_for_already_loaded_tool),
                force_initial_purge=bool(force_initial_purge),
                tool_map=normalized_tool_map,
            )
            info["effective_toolchanges"] = eff_tc
            info["toolchanges"] = eff_tc

    if include_audit_header:
        tool_map_str = json.dumps(
            {f"T{k}": f"T{v}" for k, v in sorted(normalized_tool_map.items())},
            separators=(",", ":"),
        )
        rev_meta_dict: Dict[str, Any] = {
            "g9111OrigTool": g9111_slicer_tool,
            "eventOrigTools": event_orig_tools,
            "origSkipPurgeVal": orig_skip_purge_val,
            "origSkipPurgeMode": orig_skip_purge_mode,
            "cutTipPostInjected": bool(post_cut_injected),
            "postCutBodyIdx": post_cut_body_idx,
            "origPrintEndCutTip": orig_print_end_cut_tip,
        }
        if orig_pre_purge_lines:
            rev_meta_dict["origPrePurgeLines"] = orig_pre_purge_lines
        rev_meta_str = json.dumps(rev_meta_dict, separators=(",", ":"))
        audit_header = [
            "; === Kobra S1 ACE Pro Post-Processor Audit Header ===\n",
            (
                f"; multiplier={info['flush_multiplier']} "
                f"toolMap={tool_map_str} "
                f"bedLeveling={info['bedLeveling']} "
                f"toolchanges={info['toolchanges']} "
                f"aceBypass={bool(ace_bypass)}\n"
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


# 6. Moonraker REST Client (Pure Python Standard Library)
class MoonrakerClient:
    """
    Moonraker HTTP client using exclusively `urllib.request`, `urllib.error`, and `json`.
    Supports configurable base URL (e.g. `http://192.0.2.10:7125` or `http://printer.local:7125`)
    with automatic port 80 fallback (`http://192.0.2.10`).
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

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)

    @staticmethod
    def normalize_url(url: str) -> str:
        raw = str(url if url is not None else DEFAULT_MOONRAKER_URL or "").strip()
        if not raw:
            return ""
        if not raw.startswith(("http://", "https://")):
            raw = "http://" + raw
        return raw.rstrip("/")

    def get_candidate_urls(self) -> List[str]:
        """Return primary URL and optional port 80 / 7125 fallback URL."""
        if not self.base_url:
            return []
        candidates = [self.base_url]
        if not self.enable_port80_fallback:
            return candidates

        parsed = urllib.parse.urlsplit(self.base_url)
        hostname = parsed.hostname or ""
        if not hostname:
            return candidates
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
        if not self.is_configured:
            raise ConnectionError("Moonraker URL is not configured")
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
        status structure from `refs/ACEPRO` (`ace`, `ace_state`, `ace_instance_0..3`,
        `save_variables`, `output_pin ACE_Pro`, `temperature_sensor ace_temp`,
        plus `print_stats` for print-safety interlocks).
        Also supports `/server/ace/status` if the Moonraker ACE component is installed.
        """
        inst_params = "&".join(f"ace_instance_{idx}" for idx in range(MAX_ACE_UNITS))
        query_params = (
            f"ace&ace_state&{inst_params}"
            "&save_variables&output_pin%20ACE_Pro&temperature_sensor%20ace_temp&print_stats"
        )
        raw_response = self._request_json("GET", f"/printer/objects/query?{query_params}")
        status_block = (
            raw_response.get("result", {}).get("status", {})
            if isinstance(raw_response, dict)
            else {}
        )

        # If neither ace_instance_* nor slots are populated in /printer/objects/query,
        # also attempt /server/ace/status (from refs/ACEPRO/ace_status_integration)
        ace_ext_data: Dict[str, Any] = {}
        instance_keys = tuple(f"ace_instance_{idx}" for idx in range(MAX_ACE_UNITS)) + ("ace",)
        has_instance_slots = any(
            isinstance(status_block.get(k), dict) and status_block.get(k, {}).get("slots")
            for k in instance_keys
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

        pin_val_raw = ace_pin.get("value")
        pin_enabled = True
        if pin_val_raw is not None:
            if isinstance(pin_val_raw, str):
                pin_enabled = pin_val_raw.strip().lower() not in ("0", "0.0", "false", "off", "no")
            elif isinstance(pin_val_raw, (int, float)) and not isinstance(pin_val_raw, bool):
                pin_enabled = float(pin_val_raw) > 0.5
            else:
                pin_enabled = bool(pin_val_raw)
        mgr_enabled = ace_mgr.get("ace_pro_enabled")
        ace_pro_enabled = (bool(mgr_enabled) and pin_enabled) if mgr_enabled is not None else pin_enabled

        # Determine explicitly declared ACE unit count if Klipper/ACEPRO exposes it
        declared_raw = ace_mgr.get("ace_instances", ace_mgr.get("ace_count", ace_ext_data.get("ace_instances")))
        declared_count: Optional[int] = None
        if declared_raw is not None:
            try:
                dec_val = int(declared_raw)
                if dec_val >= 1:
                    declared_count = min(MAX_ACE_UNITS, dec_val)
            except (ValueError, TypeError):
                declared_count = None

        # Collect live instances (0 -> T0..T3, 1 -> T4..T7, 2 -> T8..T11, 3 -> T12..T15)
        raw_instances: Dict[int, Dict[str, Any]] = {}
        for idx in range(MAX_ACE_UNITS):
            inst_obj = status_block.get(f"ace_instance_{idx}")
            if isinstance(inst_obj, dict) and inst_obj:
                raw_instances[idx] = inst_obj

        # Check if `ace` object itself is an instance status dict (with `slots` / `dryer`)
        ace_obj = status_block.get("ace")
        if isinstance(ace_obj, dict) and ("slots" in ace_obj or "dryer" in ace_obj or "dryer_status" in ace_obj):
            inst_idx = max(0, min(MAX_ACE_UNITS - 1, int(ace_obj.get("instance", 0))))
            raw_instances.setdefault(inst_idx, ace_obj)

        # Check `ace_ext_data` (`/server/ace/status`)
        if isinstance(ace_ext_data.get("instances"), list):
            for item in ace_ext_data["instances"]:
                if isinstance(item, dict):
                    idx = max(0, min(MAX_ACE_UNITS - 1, int(item.get("index", item.get("instance", 0)))))
                    raw_instances.setdefault(idx, item)
        elif isinstance(ace_ext_data.get("slots"), list):
            ext_slots = ace_ext_data.get("slots") or []
            if len(ext_slots) > SLOTS_PER_ACE_UNIT and "instance" not in ace_ext_data and "instance_index" not in ace_ext_data:
                chunk_cnt = min(MAX_ACE_UNITS, max(1, (len(ext_slots) + SLOTS_PER_ACE_UNIT - 1) // SLOTS_PER_ACE_UNIT))
                for c_idx in range(chunk_cnt):
                    raw_instances.setdefault(
                        c_idx,
                        {
                            "instance": c_idx,
                            "status": ace_ext_data.get("status", "ready"),
                            "connection_state": ace_ext_data.get("connection_state", "connected"),
                            "slots": ext_slots[c_idx * SLOTS_PER_ACE_UNIT : (c_idx + 1) * SLOTS_PER_ACE_UNIT],
                        },
                    )
            else:
                idx = max(0, min(MAX_ACE_UNITS - 1, int(ace_ext_data.get("instance_index", ace_ext_data.get("instance", 0)))))
                raw_instances.setdefault(idx, ace_ext_data)

        # Fallback to persistent inventory in save_variables (`ace_inventory_0..N`) ONLY up to
        # `declared_count` (never resurrect stale ace_inventory_N from an earlier lowered ace_count)
        inv_limit = declared_count if declared_count is not None else (max(raw_instances.keys()) + 1 if raw_instances else MAX_ACE_UNITS)
        if (not raw_instances) or (declared_count is not None and len(raw_instances) < declared_count):
            for idx in range(min(MAX_ACE_UNITS, inv_limit)):
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
            lane_limit = declared_count if declared_count is not None else min(MAX_ACE_UNITS, inst_count)
            for idx in range(min(MAX_ACE_UNITS, max(inst_count, lane_limit))):
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

        # Respect explicit `declared_count` when lower/higher; otherwise infer strictly from live instances
        if declared_count is not None:
            resolved_instances = max(len(raw_instances), declared_count)
        else:
            resolved_instances = len(raw_instances) if raw_instances else 1
        resolved_instances = max(1, min(MAX_ACE_UNITS, resolved_instances))

        # Pad any missing instance indices in `0 .. resolved_instances - 1`
        for idx in range(resolved_instances):
            if idx not in raw_instances:
                raw_instances[idx] = {
                    "instance": idx,
                    "status": "unknown" if idx == 0 and len(raw_instances) == 0 else "empty",
                    "connection_state": "unknown" if idx == 0 and len(raw_instances) == 0 else "disconnected",
                    "slots": [
                        {
                            "index": i,
                            "tool": idx * SLOTS_PER_ACE_UNIT + i,
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
        feed_assist_slots: Dict[str, int] = {}

        for inst_idx in sorted(raw_instances.keys()):
            inst_data = raw_instances[inst_idx]
            slots_list = inst_data.get("slots") or []
            raw_fa = inst_data.get(
                "feed_assist_slot",
                save_vars.get(
                    f"ace_feed_assist_index_{inst_idx}",
                    ace_ext_data.get("feed_assist_slot", -1) if inst_idx == 0 else -1,
                ),
            )
            try:
                inst_fa_slot = int(raw_fa) if raw_fa is not None and not isinstance(raw_fa, bool) else -1
            except (ValueError, TypeError):
                inst_fa_slot = -1
            if inst_fa_slot < 0 and int(inst_data.get("feed_assist_count") or 0) > 0:
                if current_index >= 0 and (current_index // SLOTS_PER_ACE_UNIT) == inst_idx:
                    inst_fa_slot = current_index % SLOTS_PER_ACE_UNIT
            feed_assist_slots[str(inst_idx)] = inst_fa_slot

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
                elif raw_status in (
                    "ready",
                    "loaded",
                    "empty",
                    "busy",
                    "shifting",
                    "feeding",
                    "unwinding",
                    "preload",
                    "identifying",
                    "gear_err",
                    "tmc_err",
                ):
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
                is_feed_assist = bool(inst_fa_slot == s_idx)

                normalized_slots.append(
                    {
                        "instance": inst_idx,
                        "index": s_idx,
                        "tool": tool_num,
                        "label": f"T{tool_num}",
                        "status": display_status,
                        "raw_status": raw_status,
                        "is_loaded": tool_num == current_index,
                        "feed_assist": is_feed_assist,
                        "is_shifting": raw_status == "shifting",
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

        # Normalize Dryer status & optional ACE 2 humidity telemetry across instances
        def _parse_inst_humidity(inst: Dict[str, Any], dryer_raw: Dict[str, Any]) -> Optional[float]:
            proto = str(inst.get("protocol") or "").strip().lower()
            if proto == "ace1_proto":
                return None
            raw_h = None
            for key in ("humidity", "chamber_humidity", "rel_humidity"):
                if key in inst and inst.get(key) is not None:
                    raw_h = inst.get(key)
                    break
            if raw_h is None and isinstance(dryer_raw, dict):
                for key in ("humidity", "chamber_humidity", "rel_humidity"):
                    if key in dryer_raw and dryer_raw.get(key) is not None:
                        raw_h = dryer_raw.get(key)
                        break
            if raw_h is None or isinstance(raw_h, bool):
                return None
            try:
                val = float(raw_h)
            except (ValueError, TypeError):
                return None
            if not math.isfinite(val) or val <= 0.0 or val > 100.0:
                return None
            rounded = round(val, 1)
            return int(round(rounded)) if abs(rounded - round(rounded)) < 1e-6 else rounded

        dryer_instances: Dict[int, Dict[str, Any]] = {}
        unit_humidities: Dict[str, Any] = {}
        unit_temps: Dict[str, Any] = {}
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
            inst_hum = _parse_inst_humidity(inst_obj, rd)
            raw_inst_temp = inst_obj.get("temp")
            if raw_inst_temp is None and inst_idx == 0:
                raw_inst_temp = temp_sensor.get("temperature")
            inst_temp: Optional[float] = None
            if raw_inst_temp is not None and not isinstance(raw_inst_temp, bool):
                try:
                    t_val = float(raw_inst_temp)
                    if math.isfinite(t_val) and t_val > 0.0:
                        t_rounded = round(t_val, 1)
                        inst_temp = int(round(t_rounded)) if abs(t_rounded - round(t_rounded)) < 1e-6 else t_rounded
                except (ValueError, TypeError):
                    inst_temp = None
            dryer_entry: Dict[str, Any] = {
                "instance": inst_idx,
                "status": d_stat,
                "active": d_stat == "drying",
                "target_temp": d_temp,
                "duration": d_dur,
                "remain_time_min": d_rem_min,
            }
            if inst_hum is not None:
                dryer_entry["humidity"] = inst_hum
                unit_humidities[str(inst_idx)] = inst_hum
            if inst_temp is not None:
                dryer_entry["ambient_temp"] = inst_temp
                unit_temps[str(inst_idx)] = inst_temp
            dryer_instances[inst_idx] = dryer_entry

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

        primary_humidity = unit_humidities.get("0")
        if primary_humidity is None and unit_humidities:
            primary_humidity = unit_humidities[next(iter(unit_humidities))]

        endless_en_raw = save_vars.get("ace_endless_spool_enabled", False)
        endless_spool_enabled = (
            endless_en_raw.strip().lower() in ("1", "true", "yes", "on")
            if isinstance(endless_en_raw, str)
            else bool(endless_en_raw)
        )
        endless_mode_raw = str(save_vars.get("ace_endless_spool_match_mode") or "exact").strip().lower()
        endless_spool_mode = endless_mode_raw if endless_mode_raw in ("exact", "material", "next") else "exact"
        raw_es_next = ace_mgr.get("endless_spool_next_tool", -1)
        try:
            endless_spool_next_tool = (
                int(raw_es_next) if raw_es_next is not None and not isinstance(raw_es_next, bool) else -1
            )
        except (ValueError, TypeError):
            endless_spool_next_tool = -1

        raw_hw_state = str(primary_inst.get("connection_state") or "connected").strip().lower()
        return {
            "connected": raw_hw_state not in ("disconnected", "connecting", "error"),
            "ace_hw_state": raw_hw_state,
            "device_status": str(primary_inst.get("status") or "ready"),
            "connection_state": str(primary_inst.get("connection_state") or "connected"),
            "model": str(primary_inst.get("model") or "Anycubic Color Engine Pro"),
            "firmware": str(primary_inst.get("firmware") or "Unknown"),
            "ace_pro_enabled": ace_pro_enabled,
            "endless_spool_enabled": endless_spool_enabled,
            "endless_spool_mode": endless_spool_mode,
            "endless_spool_next_tool": endless_spool_next_tool,
            "ace_instances": resolved_instances,
            "ace_count": resolved_instances,
            "current_index": current_index,
            "target_index": target_index,
            "ambient_temp": float(ambient_temp or 0.0),
            "unit_temps": unit_temps,
            "humidity": primary_humidity,
            "unit_humidities": unit_humidities,
            "fan_speed": int(primary_inst.get("fan_speed") or 0),
            "feed_assist_slot": int(feed_assist_slots.get("0", primary_inst.get("feed_assist_slot", -1))),
            "feed_assist_slots": feed_assist_slots,
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

    def start_dryer(self, temp_c: int = 45, duration_min: int = 240, instance: Optional[int] = None) -> Dict[str, Any]:
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

    def load_slot(self, tool: int, allow_while_printing: bool = False) -> Dict[str, Any]:
        """Load physical ACE slot T<tool> into toolhead (`ACE_CHANGE_TOOL TOOL=<n>`)."""
        return self.send_gcode(f"ACE_CHANGE_TOOL TOOL={int(tool)}", allow_while_printing=allow_while_printing)

    def unload_slot(
        self,
        tool: Optional[int] = None,
        full_unload: bool = False,
        allow_while_printing: bool = False,
    ) -> Dict[str, Any]:
        """
        Unload filament from toolhead or specific ACE slot:
        - `ACE_FULL_UNLOAD TOOL=<n>` if `full_unload=True`
        - `ACE_SMART_UNLOAD TOOL=<n>` if a specific `tool` is provided
        - `ACE_CHANGE_TOOL TOOL=-1` (alias `TR`) to unload active toolhead filament
        """
        if full_unload and tool is not None and tool >= 0:
            return self.send_gcode(f"ACE_FULL_UNLOAD TOOL={int(tool)}", allow_while_printing=allow_while_printing)
        if tool is not None and tool >= 0:
            return self.send_gcode(f"ACE_SMART_UNLOAD TOOL={int(tool)}", allow_while_printing=allow_while_printing)
        return self.send_gcode("ACE_CHANGE_TOOL TOOL=-1", allow_while_printing=allow_while_printing)

    def feed_slot(
        self,
        tool: int,
        length_mm: int = 50,
        speed_mms: int = 25,
        allow_while_printing: bool = False,
    ) -> Dict[str, Any]:
        """Feed filament on ACE slot (`ACE_FEED T=<tool> LENGTH=<mm> SPEED=<mm/s>`)."""
        return self.send_gcode(
            f"ACE_FEED T={int(tool)} LENGTH={max(1, int(length_mm))} SPEED={max(1, int(speed_mms))}",
            allow_while_printing=allow_while_printing,
        )

    def retract_slot(
        self,
        tool: int,
        length_mm: int = 50,
        speed_mms: int = 25,
        allow_while_printing: bool = False,
    ) -> Dict[str, Any]:
        """Retract filament on ACE slot (`ACE_RETRACT T=<tool> LENGTH=<mm> SPEED=<mm/s>`)."""
        return self.send_gcode(
            f"ACE_RETRACT T={int(tool)} LENGTH={max(1, int(length_mm))} SPEED={max(1, int(speed_mms))}",
            allow_while_printing=allow_while_printing,
        )

    def enable_feed_assist(self, tool: int, allow_while_printing: bool = False) -> Dict[str, Any]:
        """Enable feed assist on ACE slot (`ACE_ENABLE_FEED_ASSIST T=<tool>`)."""
        return self.send_gcode(f"ACE_ENABLE_FEED_ASSIST T={int(tool)}", allow_while_printing=allow_while_printing)

    def disable_feed_assist(self, tool: int, allow_while_printing: bool = False) -> Dict[str, Any]:
        """Disable feed assist on ACE slot (`ACE_DISABLE_FEED_ASSIST T=<tool>`)."""
        return self.send_gcode(f"ACE_DISABLE_FEED_ASSIST T={int(tool)}", allow_while_printing=allow_while_printing)

    def set_active_spool(self, spool_id: Optional[int]) -> Dict[str, Any]:
        """Set Moonraker's active Spoolman spool (`POST /server/spoolman/spool_id`)."""
        val = int(spool_id) if (spool_id is not None and not isinstance(spool_id, bool) and int(spool_id) > 0) else None
        return self._request_json("POST", "/server/spoolman/spool_id", payload={"spool_id": val})

    def set_ace_pro_enabled(self, enabled: bool, allow_while_printing: bool = False) -> Dict[str, Any]:
        """Toggle Klipper `[output_pin ACE_Pro]` (`SET_PIN PIN=ACE_Pro VALUE=1|0`)."""
        return self.send_gcode(
            f"SET_PIN PIN=ACE_Pro VALUE={1 if enabled else 0}",
            allow_while_printing=allow_while_printing,
        )

    def set_endless_spool(self, enabled: bool, mode: str = "exact") -> Dict[str, Any]:
        """Apply ACE Pro Endless Spool enabled flag and match mode via Klipper G-code."""
        norm_mode = str(mode or "exact").strip().lower()
        if norm_mode not in ("exact", "material", "next"):
            norm_mode = "exact"
        cmd = (
            f"ACE_SET_ENDLESS_SPOOL_MODE MODE={norm_mode}\n"
            f"{'ACE_ENABLE_ENDLESS_SPOOL' if enabled else 'ACE_DISABLE_ENDLESS_SPOOL'}"
        )
        return self.send_gcode(cmd, allow_while_printing=True)

    @staticmethod
    def find_endless_spool_backup_chain(
        slots: List[Dict[str, Any]],
        current_tool: int,
        mode: str = "exact",
    ) -> List[Dict[str, Any]]:
        """Mirror ACEPRO EndlessSpool.find_exact_match() order to return all matching backup slots."""
        if not slots or len(slots) <= 1:
            return []
        norm_mode = str(mode or "exact").strip().lower()
        if norm_mode not in ("exact", "material", "next"):
            norm_mode = "exact"
        by_tool = {int(s.get("tool", i)): s for i, s in enumerate(slots) if isinstance(s, dict)}
        total = len(slots)
        cur = by_tool.get(int(current_tool))
        if cur is None:
            return []
        cur_mat = str(cur.get("material") or "").strip().lower()
        cur_rgb = list(cur.get("color") or [0, 0, 0])[:3]
        matches: List[Dict[str, Any]] = []
        for offset in range(1, total):
            cand = by_tool.get((int(current_tool) + offset) % total)
            if not cand:
                continue
            cand_st = str(cand.get("status") or "").strip().lower()
            if cand_st not in ("ready", "loaded"):
                continue
            if norm_mode == "next":
                matches.append(cand)
                continue
            cand_mat = str(cand.get("material") or "").strip().lower()
            if not cur_mat or cur_mat == "unknown" or not cand_mat or cand_mat == "unknown":
                continue
            if cand_mat != cur_mat:
                continue
            if norm_mode == "exact" and list(cand.get("color") or [0, 0, 0])[:3] != cur_rgb:
                continue
            matches.append(cand)
        return matches

    @staticmethod
    def find_endless_spool_backup(
        slots: List[Dict[str, Any]],
        current_tool: int,
        mode: str = "exact",
    ) -> Optional[Dict[str, Any]]:
        """Return the first predicted backup slot for `current_tool` in `mode`."""
        chain = MoonrakerClient.find_endless_spool_backup_chain(slots, current_tool, mode)
        return chain[0] if chain else None

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

        safe_filename = os.path.basename(filename or "kobra_van_kenorca.gcode").replace('"', "_")
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
        if not self.is_configured:
            raise ConnectionError("Moonraker URL is not configured")
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
                if exc.code == 403:
                    raise RuntimeError(
                        t(
                            "moonraker.upload_403",
                            lang=GLOBAL_STATE.language,
                            detail=err_detail,
                        )
                    ) from exc
                raise RuntimeError(f"Moonraker upload HTTP {exc.code}: {err_detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                continue

        raise ConnectionError(
            f"Failed to upload G-code to Moonraker ({', '.join(self.get_candidate_urls())}): {last_error}"
        )


def _clean_spoolman_str(val: Any) -> str:
    """Normalize a Spoolman string or JSON-encoded `extra` string value."""
    if val is None or isinstance(val, bool):
        return ""
    raw = str(val).strip()
    if not raw or raw.lower() in ("null", "none", "{}"):
        return ""
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, str):
                raw = decoded.strip()
        except Exception:
            raw = raw[1:-1].strip()
    return raw


def _norm_sku_key(val: Any) -> str:
    """Normalize an article number / SKU by trimming quotes, removing whitespace, and lowercasing."""
    cleaned = _clean_spoolman_str(val)
    if not cleaned:
        return ""
    return re.sub(r"\s+", "", cleaned).lower()


def _parse_optional_weight(val: Any, *, allow_zero: bool = True) -> Optional[float]:
    """Parse an optional non-negative finite weight value in grams without inventing defaults."""
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(num) or num < 0.0:
        return None
    if not allow_zero and num <= 0.0:
        return None
    return round(num, 1)


def _extract_spoolman_color_text(spool: Dict[str, Any]) -> str:
    """Extract an optional explicit color name from Spoolman filament or extra metadata."""
    if not isinstance(spool, dict):
        return ""
    filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
    candidates: List[Any] = [filament.get("color_name"), filament.get("color")]
    for extra_dict in (filament.get("extra"), spool.get("extra")):
        if not isinstance(extra_dict, dict):
            continue
        lowered_map = {str(k).strip().lower(): v for k, v in extra_dict.items()}
        for key in ("color_name", "color", "farbname", "farbe"):
            if key in lowered_map:
                candidates.append(lowered_map[key])
    for cand in candidates:
        cleaned = _clean_spoolman_str(cand)
        if cleaned and not _HEX_COLOR_RE.match(cleaned):
            return cleaned
    return ""


_COLOR_NAME_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "schwarz": {"de": "Schwarz", "en": "Black"},
    "black": {"de": "Schwarz", "en": "Black"},
    "weiß": {"de": "Weiß", "en": "White"},
    "weiss": {"de": "Weiß", "en": "White"},
    "white": {"de": "Weiß", "en": "White"},
    "grau": {"de": "Grau", "en": "Grey"},
    "grey": {"de": "Grau", "en": "Grey"},
    "gray": {"de": "Grau", "en": "Gray"},
    "silber": {"de": "Silber", "en": "Silver"},
    "silver": {"de": "Silber", "en": "Silver"},
    "rot": {"de": "Rot", "en": "Red"},
    "red": {"de": "Rot", "en": "Red"},
    "orange": {"de": "Orange", "en": "Orange"},
    "gelb": {"de": "Gelb", "en": "Yellow"},
    "yellow": {"de": "Gelb", "en": "Yellow"},
    "grün": {"de": "Grün", "en": "Green"},
    "gruen": {"de": "Grün", "en": "Green"},
    "green": {"de": "Grün", "en": "Green"},
    "türkis": {"de": "Türkis", "en": "Turquoise"},
    "tuerkis": {"de": "Türkis", "en": "Turquoise"},
    "turquoise": {"de": "Türkis", "en": "Turquoise"},
    "cyan": {"de": "Türkis", "en": "Cyan"},
    "teal": {"de": "Türkis", "en": "Teal"},
    "blau": {"de": "Blau", "en": "Blue"},
    "blue": {"de": "Blau", "en": "Blue"},
    "violett": {"de": "Violett", "en": "Violet"},
    "violet": {"de": "Violett", "en": "Violet"},
    "purple": {"de": "Violett", "en": "Purple"},
    "lila": {"de": "Violett", "en": "Violet"},
    "pink": {"de": "Pink", "en": "Pink"},
    "rosa": {"de": "Rosa", "en": "Pink"},
    "braun": {"de": "Braun", "en": "Brown"},
    "brown": {"de": "Braun", "en": "Brown"},
    "gold": {"de": "Gold", "en": "Gold"},
    "beige": {"de": "Beige", "en": "Beige"},
    "transparent": {"de": "Transparent", "en": "Transparent"},
    "clear": {"de": "Transparent", "en": "Clear"},
}


def translate_color_name(name: Any, lang: Optional[str] = None) -> str:
    """
    Bidirectionally translate standard color names between German (`de`) and English (`en`),
    preserving multi-word or custom Spoolman filament/color names verbatim.
    """
    raw = _clean_spoolman_str(name)
    if not raw:
        return ""
    entry = _COLOR_NAME_TRANSLATIONS.get(raw.lower())
    if not entry:
        return raw
    target_lang = normalize_language(lang)
    return entry.get(target_lang, raw)


def classify_color_name(hex_color: Any, spool_color_text: Any = "", lang: Optional[str] = None) -> str:
    """
    Derive a localized color name (`de` or `en`) from explicit Spoolman text or `hex_color`.
    """
    raw_name = classify_german_color_name(hex_color, spool_color_text)
    if not raw_name:
        return ""
    return translate_color_name(raw_name, lang=lang)


def classify_german_color_name(hex_color: Any, spool_color_text: Any = "") -> str:
    """
    Return explicit Spoolman color text if present, otherwise derive a readable German
    color name (e.g. Schwarz, Weiß, Grau, Silber, Rot, Orange, Gelb, Grün, Türkis,
    Blau, Violett, Pink, Braun, Gold, Beige) from `hex_color`.
    """
    explicit = _clean_spoolman_str(spool_color_text)
    if explicit and not _HEX_COLOR_RE.match(explicit):
        return explicit

    raw_hex = _clean_spoolman_str(hex_color)
    if not raw_hex or not _HEX_COLOR_RE.match(raw_hex):
        return ""

    r_i, g_i, b_i = hex_to_rgb(rgb_to_hex(raw_hex))
    diff = (max(r_i, g_i, b_i) - min(r_i, g_i, b_i)) / 255.0
    h_norm, sat, lum = rgb_to_hsl(r_i, g_i, b_i)
    hue = (h_norm * 360.0) % 360.0

    # Achromatic neutrals and blue-gray slate shades
    if sat < 0.14 or diff < 0.08 or (sat < 0.22 and 185.0 <= hue <= 245.0 and 0.16 < lum < 0.68):
        if lum <= 0.18:
            return "Schwarz"
        if lum >= 0.88:
            return "Weiß"
        if lum >= 0.66:
            return "Silber"
        return "Grau"

    if lum <= 0.11:
        return "Schwarz"
    if 25.0 <= hue <= 65.0 and lum >= 0.65 and sat <= 0.68:
        return "Beige"
    if lum >= 0.94 and sat < 0.35:
        return "Weiß"

    if 10.0 <= hue < 18.0:
        return "Braun" if (lum < 0.32 or sat < 0.45) else "Rot"
    if 18.0 <= hue < 40.0:
        if lum < 0.42 or (sat < 0.55 and lum < 0.58):
            return "Braun"
        if lum >= 0.68:
            return "Beige"
        return "Orange"
    if 40.0 <= hue < 52.0:
        if (lum <= 0.525 and sat <= 0.92) or (48.0 <= hue <= 52.0 and lum <= 0.52):
            return "Gold"
        if lum < 0.34:
            return "Braun"
        if lum >= 0.72 and sat < 0.65:
            return "Beige"
        return "Gelb"
    if 52.0 <= hue < 68.0:
        if lum < 0.30:
            return "Braun"
        if lum >= 0.68 and sat <= 0.65:
            return "Beige"
        return "Gelb"
    if 68.0 <= hue < 158.0:
        return "Grün"
    if 158.0 <= hue < 196.0:
        return "Türkis"
    if 196.0 <= hue < 258.0:
        return "Blau"
    if 258.0 <= hue < 298.0:
        return "Violett"
    if 298.0 <= hue < 342.0:
        return "Pink"
    if lum >= 0.68:
        return "Pink"
    if lum < 0.24 and sat < 0.55:
        return "Braun"
    return "Rot"


def _extract_spool_sku_candidates(spool: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Extract ordered `(primary_sku_candidates, fallback_name_candidates)` from a Spoolman
    spool payload (`filament.article_number`, `extra.sku`, `lot_nr`, and `filament.name`).
    """
    primary: List[str] = []
    fallback: List[str] = []
    if not isinstance(spool, dict):
        return primary, fallback

    filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}

    def _add_unique(bucket: List[str], candidate: Any) -> None:
        cleaned = _clean_spoolman_str(candidate)
        if cleaned and cleaned not in bucket:
            bucket.append(cleaned)

    _add_unique(primary, filament.get("article_number"))
    _add_unique(primary, filament.get("sku"))
    _add_unique(primary, spool.get("article_number"))
    _add_unique(primary, spool.get("sku"))

    for extra_dict in (filament.get("extra"), spool.get("extra")):
        if not isinstance(extra_dict, dict):
            continue
        lowered_map = {str(k).strip().lower(): v for k, v in extra_dict.items()}
        for key in ("sku", "article_number", "ace_sku", "code", "barcode", "rfid"):
            if key in lowered_map:
                _add_unique(primary, lowered_map[key])

    _add_unique(primary, spool.get("lot_nr"))
    _add_unique(fallback, filament.get("name"))
    return primary, fallback


def _build_spoolman_display_name(spool: Dict[str, Any], matched_sku: str) -> str:
    """Build clean human-readable display name (`Vendor FilamentName`) for a Spoolman spool."""
    filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
    vendor_raw = filament.get("vendor")
    vendor = _clean_spoolman_str(
        vendor_raw.get("name") if isinstance(vendor_raw, dict) else vendor_raw
    )
    fil_name = _clean_spoolman_str(filament.get("name") or spool.get("name"))
    if vendor and fil_name:
        if fil_name.lower().startswith(vendor.lower()):
            return fil_name
        return f"{vendor} {fil_name}"
    return fil_name or vendor or matched_sku


def _parse_spoolman_timestamp(ts_val: Any) -> Optional[float]:
    """Parse an ISO-8601 Spoolman timestamp (`last_used` / `first_used`) into UTC epoch seconds."""
    cleaned = _clean_spoolman_str(ts_val)
    if not cleaned:
        return None
    normalized = cleaned[:-1] + "+00:00" if cleaned.endswith(("Z", "z")) else cleaned
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
            return float(dt.timestamp())
        except ValueError:
            continue
    return None


def _format_spoolman_last_used(ts_val: Any) -> str:
    """Format a Spoolman `last_used` timestamp into a compact UTC date/time string."""
    ts = _parse_spoolman_timestamp(ts_val)
    if ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


def _spool_candidate_sort_key(spool: Dict[str, Any]) -> Tuple[int, float, int, float, int, int]:
    """
    Deterministic preference order for matching active Spoolman spools:
    1. Most recent valid `last_used` timestamp.
    2. Reproducible fallback when `last_used` is absent or tied:
       a) valid `first_used` timestamp,
       b) non-empty spool (`remaining_weight > 0`),
       c) lowest numeric `spool.id`.
    """
    last_ts = _parse_spoolman_timestamp(spool.get("last_used"))
    first_ts = _parse_spoolman_timestamp(spool.get("first_used"))
    rem_w = _parse_optional_weight(spool.get("remaining_weight"), allow_zero=True)
    has_pos_rem = 1 if (rem_w is not None and rem_w > 0.0) else 0
    sid_raw = spool.get("id")
    try:
        sid_int = int(str(sid_raw).strip()) if (sid_raw is not None and not isinstance(sid_raw, bool)) else 999999999
    except (ValueError, TypeError):
        sid_int = 999999999
    return (
        1 if last_ts is not None else 0,
        last_ts if last_ts is not None else 0.0,
        1 if first_ts is not None else 0,
        first_ts if first_ts is not None else 0.0,
        has_pos_rem,
        -sid_int,
    )


def _compute_spool_weights(spool: Dict[str, Any]) -> Dict[str, Any]:
    """Extract remaining, initial, used, capacity, fill_percent, and weight_status strictly from Spoolman."""
    filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
    rem_weight = _parse_optional_weight(spool.get("remaining_weight"), allow_zero=True)
    init_weight = _parse_optional_weight(spool.get("initial_weight"), allow_zero=False)
    fil_weight = _parse_optional_weight(filament.get("weight"), allow_zero=False)
    used_weight = _parse_optional_weight(spool.get("used_weight"), allow_zero=True)
    capacity_weight = init_weight if init_weight is not None else fil_weight

    fill_percent: Optional[float] = None
    if rem_weight is None:
        weight_status = "missing"
    elif rem_weight <= 0.0:
        rem_weight = 0.0
        weight_status = "empty"
        fill_percent = 0.0
    else:
        weight_status = "ok"
        if capacity_weight is not None and capacity_weight > 0.0:
            fill_percent = round(max(0.0, min(100.0, (rem_weight / capacity_weight) * 100.0)), 1)

    return {
        "remaining_weight": rem_weight,
        "initial_weight": init_weight,
        "used_weight": used_weight,
        "capacity_weight": capacity_weight,
        "fill_percent": fill_percent,
        "weight_status": weight_status,
    }


def _build_spoolman_candidate_summary(spool: Dict[str, Any], matched_sku: str) -> Dict[str, Any]:
    """Build compact Spoolman candidate metadata for dropdown selection."""
    w = _compute_spool_weights(spool)
    return {
        "spool_id": spool.get("id"),
        "display_name": _build_spoolman_display_name(spool, matched_sku),
        "remaining_weight": w["remaining_weight"],
        "initial_weight": w["initial_weight"],
        "fill_percent": w["fill_percent"],
        "weight_status": w["weight_status"],
        "location": _clean_spoolman_str(spool.get("location")),
        "last_used": _clean_spoolman_str(spool.get("last_used")),
        "last_used_display": _format_spoolman_last_used(spool.get("last_used")),
    }


def _build_spoolman_slot_info(
    spool: Dict[str, Any],
    matched_sku: str,
    *,
    assignment_source: str = "auto",
    match_reason: str = "single",
    candidates: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build normalized compact Spoolman metadata payload attached to an ACE slot."""
    filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
    vendor_raw = filament.get("vendor")
    vendor = _clean_spoolman_str(
        vendor_raw.get("name") if isinstance(vendor_raw, dict) else vendor_raw
    )
    fil_name = _clean_spoolman_str(filament.get("name") or spool.get("name"))
    display_name = _build_spoolman_display_name(spool, matched_sku)

    material = _clean_spoolman_str(filament.get("material"))
    color_raw = _clean_spoolman_str(filament.get("color_hex"))
    if not color_raw or not _HEX_COLOR_RE.match(color_raw):
        multi_raw = _clean_spoolman_str(filament.get("multi_color_hexes"))
        first_hex = multi_raw.split(",", 1)[0].strip() if multi_raw else ""
        color_raw = first_hex if _HEX_COLOR_RE.match(first_hex) else ""
    color_hex = rgb_to_hex(color_raw) if (color_raw and _HEX_COLOR_RE.match(color_raw)) else ""
    explicit_color = _extract_spoolman_color_text(spool)
    color_name = classify_german_color_name(color_hex, explicit_color)
    w = _compute_spool_weights(spool)
    cand_list = list(candidates or [])

    fil_density_raw = filament.get("density")
    fil_density: Optional[float] = None
    if fil_density_raw is not None and not isinstance(fil_density_raw, bool):
        try:
            d_val = float(fil_density_raw)
            if math.isfinite(d_val) and d_val > 0.0:
                fil_density = round(d_val, 4)
        except (ValueError, TypeError):
            pass

    return {
        "status": "ok",
        "conflict": False,
        "spool_id": spool.get("id"),
        "sku": matched_sku,
        "sku_norm": _norm_sku_key(matched_sku),
        "vendor": vendor,
        "filament_name": fil_name,
        "display_name": display_name,
        "material": material,
        "density": fil_density,
        "color_hex": color_hex,
        "color_name": color_name,
        "color_text": explicit_color,
        "location": _clean_spoolman_str(spool.get("location")),
        "last_used": _clean_spoolman_str(spool.get("last_used")),
        "last_used_display": _format_spoolman_last_used(spool.get("last_used")),
        "assignment_source": assignment_source,
        "match_reason": match_reason,
        "multi_match": len(cand_list) > 1,
        "candidates": cand_list,
        "remaining_weight": w["remaining_weight"],
        "initial_weight": w["initial_weight"],
        "used_weight": w["used_weight"],
        "capacity_weight": w["capacity_weight"],
        "fill_percent": w["fill_percent"],
        "weight_status": w["weight_status"],
    }


def _select_spool_from_candidates(
    sorted_spools: List[Tuple[Dict[str, Any], str]],
    matched_sku: str,
    manual_entry: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str, str, bool]:
    """
    Choose active spool from ranked candidates `(spool, label)`, honoring a valid manual selection
    for the same normalized SKU and falling back cleanly to `last_used` or deterministic fallback.
    Returns `(chosen_spool, assignment_source, match_reason, manual_invalidated)`.
    """
    cur_sku_norm = _norm_sku_key(matched_sku)
    manual_invalidated = False

    if isinstance(manual_entry, dict) and manual_entry.get("spool_id") is not None:
        saved_sku = _norm_sku_key(manual_entry.get("sku_norm") or manual_entry.get("sku_raw") or "")
        try:
            saved_id = int(manual_entry["spool_id"])
        except (ValueError, TypeError):
            saved_id = -1
        if saved_id >= 0 and (not saved_sku or saved_sku == cur_sku_norm):
            for sp_obj, _ in sorted_spools:
                try:
                    sp_id = int(str(sp_obj.get("id")).strip())
                except (ValueError, TypeError):
                    sp_id = -2
                if sp_id == saved_id:
                    return sp_obj, "manual", "manual", False
        # Saved manual selection was deleted, archived, or SKU changed
        manual_invalidated = True

    top_sp = sorted_spools[0][0]
    if len(sorted_spools) == 1:
        return top_sp, "auto", "single", manual_invalidated

    top_ts = _parse_spoolman_timestamp(top_sp.get("last_used"))
    second_ts = _parse_spoolman_timestamp(sorted_spools[1][0].get("last_used"))
    if top_ts is not None and (second_ts is None or top_ts > second_ts):
        return top_sp, "auto", "last_used", manual_invalidated
    return top_sp, "auto", "fallback", manual_invalidated


def match_spoolman_to_slots(
    ace_slots: List[Dict[str, Any]],
    spools: List[Dict[str, Any]],
    manual_selections: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    invalidated_slots_out: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Match Spoolman spools to ACE Pro slots.
    - Numeric SKUs check active `spool.id` first (never `filament.id`), falling back to
      normalized article-number/SKU search when no active spool carries that ID.
    - Single SKU matches assign automatically.
    - Multiple active spools for a SKU honor valid manual per-slot assignments (re-validated
      against active non-archived spools and current slot SKU), or automatically prefer the
      most recently used spool (`last_used`) with deterministic fallback, while exposing all
      matching candidate spools for manual UI switching.
    Returns `(enriched_slot_copies, matched_count)`.
    """
    spools_by_id: Dict[int, List[Tuple[Dict[str, Any], str]]] = {}
    primary_index: Dict[str, List[Tuple[Dict[str, Any], str]]] = {}
    fallback_index: Dict[str, List[Tuple[Dict[str, Any], str]]] = {}

    for sp in spools or []:
        if not isinstance(sp, dict) or bool(sp.get("archived")):
            continue
        sid_raw = sp.get("id")
        if sid_raw is not None and not isinstance(sid_raw, bool):
            sid_str = str(sid_raw).strip()
            if re.fullmatch(r"\d+", sid_str):
                spools_by_id.setdefault(int(sid_str), []).append((sp, sid_str))
        prim_cands, fb_cands = _extract_spool_sku_candidates(sp)
        for cand in prim_cands:
            nk = _norm_sku_key(cand)
            if nk:
                primary_index.setdefault(nk, []).append((sp, cand))
        for cand in fb_cands:
            nk = _norm_sku_key(cand)
            if nk:
                fallback_index.setdefault(nk, []).append((sp, cand))

    def _dedupe_and_rank(
        items: List[Tuple[Dict[str, Any], str]],
    ) -> List[Tuple[Dict[str, Any], str]]:
        seen_ids: Dict[Any, int] = {}
        unique: List[Tuple[Dict[str, Any], str]] = []
        for sp_obj, label in items:
            sid = sp_obj.get("id")
            ident: Any = (
                ("id", int(str(sid).strip()))
                if (sid is not None and not isinstance(sid, bool) and re.fullmatch(r"-?\d+", str(sid).strip()))
                else ("obj", id(sp_obj))
            )
            if ident not in seen_ids:
                seen_ids[ident] = len(unique)
                unique.append((sp_obj, label))
        unique.sort(key=lambda pair: _spool_candidate_sort_key(pair[0]), reverse=True)
        return unique

    enriched: List[Dict[str, Any]] = []
    matched_count = 0
    manual_map = manual_selections if isinstance(manual_selections, dict) else {}

    for slot in ace_slots or []:
        if not isinstance(slot, dict):
            continue
        slot_copy = dict(slot)
        slot_copy.pop("spoolman", None)
        slot_key_str = str(slot_copy.get("tool", ""))
        manual_entry = manual_map.get(slot_key_str) if slot_key_str else None
        status_norm = str(slot_copy.get("status") or "empty").strip().lower()

        if status_norm == "empty":
            if manual_entry and invalidated_slots_out is not None:
                invalidated_slots_out.append(slot_key_str)
            enriched.append(slot_copy)
            continue

        slot_keys: List[str] = []
        seen_norm_keys: List[str] = []
        for key_name in ("sku", "article_number", "code", "name"):
            raw_k = _clean_spoolman_str(slot_copy.get(key_name))
            nk = _norm_sku_key(raw_k)
            if raw_k and nk and nk not in seen_norm_keys:
                seen_norm_keys.append(nk)
                slot_keys.append(raw_k)

        matched_this_slot = False
        for raw_k in slot_keys:
            nk = _norm_sku_key(raw_k)
            resolved: List[Tuple[Dict[str, Any], str]] = []
            via_direct_id = False
            if re.fullmatch(r"\d+", raw_k):
                id_hits = _dedupe_and_rank(spools_by_id.get(int(raw_k), []))
                if id_hits:
                    resolved = id_hits
                    via_direct_id = True
            if not resolved:
                prim_hits = _dedupe_and_rank(primary_index.get(nk, []))
                if prim_hits:
                    resolved = prim_hits
                else:
                    resolved = _dedupe_and_rank(fallback_index.get(nk, []))
            if not resolved:
                continue

            chosen_sp, assign_src, reason, was_invalidated = _select_spool_from_candidates(
                resolved,
                raw_k,
                manual_entry=manual_entry,
            )
            if via_direct_id and assign_src == "auto" and len(resolved) == 1:
                reason = "spool_id"
            if was_invalidated and invalidated_slots_out is not None and slot_key_str not in invalidated_slots_out:
                invalidated_slots_out.append(slot_key_str)

            candidate_summaries = (
                [_build_spoolman_candidate_summary(sp_obj, raw_k) for sp_obj, _ in resolved]
                if len(resolved) > 1
                else []
            )
            slot_copy["spoolman"] = _build_spoolman_slot_info(
                chosen_sp,
                raw_k,
                assignment_source=assign_src,
                match_reason=reason,
                candidates=candidate_summaries,
            )
            matched_count += 1
            matched_this_slot = True
            break

        if not matched_this_slot and manual_entry and invalidated_slots_out is not None:
            saved_sku = _norm_sku_key(manual_entry.get("sku_norm") or manual_entry.get("sku_raw") or "")
            cur_primary_sku = _norm_sku_key(slot_keys[0]) if slot_keys else ""
            if (not cur_primary_sku) or (saved_sku and saved_sku != cur_primary_sku) or bool(spools):
                if slot_key_str not in invalidated_slots_out:
                    invalidated_slots_out.append(slot_key_str)
        enriched.append(slot_copy)
    return enriched, matched_count


def _build_external_spoolman_choices(spools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build compact list of active Spoolman spools for External Spool selection in UI."""
    out: List[Dict[str, Any]] = []
    for sp in spools or []:
        if not isinstance(sp, dict) or bool(sp.get("archived")):
            continue
        sid = sp.get("id")
        if sid is None or isinstance(sid, bool):
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue
        fil = sp.get("filament") if isinstance(sp.get("filament"), dict) else {}
        vend = fil.get("vendor") if isinstance(fil.get("vendor"), dict) else {}
        vendor = _clean_spoolman_str(vend.get("name"))
        fname = _clean_spoolman_str(fil.get("name"))
        mat = _clean_spoolman_str(fil.get("material"))
        disp = (
            f"{vendor} {fname}".strip()
            if (vendor and fname and vendor.lower() not in fname.lower())
            else (fname or vendor or mat or f"Spool #{sid_int}")
        )
        w = _compute_spool_weights(sp)
        raw_col = _clean_spoolman_str(fil.get("color_hex"))
        col_hex = rgb_to_hex(raw_col) if raw_col else "#475569"
        out.append({
            "spool_id": sid_int,
            "display_name": disp,
            "material": mat,
            "color_hex": col_hex,
            "remaining_weight": w["remaining_weight"],
            "capacity_weight": w["capacity_weight"],
            "density": fil.get("density"),
            "location": _clean_spoolman_str(sp.get("location")),
        })
    return out


class SpoolmanClient:
    """
    100% optional stdlib-only Spoolman REST client (`/api/v1/spool`).
    When `base_url` is empty, performs zero network requests.
    """

    @staticmethod
    def normalize_url(url: Any) -> str:
        raw = str(url or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = f"http://{raw}"
        parsed = urllib.parse.urlsplit(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https") or not parsed.netloc:
            return ""
        clean_path = parsed.path.rstrip("/")
        return urllib.parse.urlunsplit((scheme, parsed.netloc, clean_path, "", ""))

    def __init__(self, base_url: str = "", timeout: float = DEFAULT_SPOOLMAN_TIMEOUT):
        self.base_url = self.normalize_url(base_url)
        self.timeout = max(0.5, float(timeout or DEFAULT_SPOOLMAN_TIMEOUT))

    @property
    def is_enabled(self) -> bool:
        return bool(self.base_url)

    def fetch_spools(self) -> List[Dict[str, Any]]:
        """Query Spoolman `/api/v1/spool`; returns `[]` immediately when disabled."""
        if not self.is_enabled:
            return []
        url = f"{self.base_url}/api/v1/spool"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        if not raw:
            return []
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or data.get("spools") or []
        else:
            items = []
        return [
            item
            for item in items
            if isinstance(item, dict) and not bool(item.get("archived"))
        ]


def enrich_ace_status_with_spoolman(
    ace_status: Dict[str, Any],
    state: Optional["PluginState"] = None,
    *,
    force_fetch: bool = False,
    expected_epoch: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Optionally enrich ACE Pro `slots` with Spoolman SKU matches & remaining weights.
    Performs zero HTTP calls when `state.spoolman_url` is blank, and fails gracefully
    when Spoolman is unreachable or slow. Preserves valid per-slot manual spool overrides
    and revalidates/prunes stale overrides when spools are deleted, archived, or SKUs change.
    """
    st = state or GLOBAL_STATE
    out = dict(ace_status or {})
    raw_slots = [dict(s) for s in (out.get("slots") or []) if isinstance(s, dict)]
    with st._lock:
        sp_url = SpoolmanClient.normalize_url(st.spoolman_url)
        cached_url = st._spoolman_cache_url
        cached_spools = list(st._spoolman_spools) if cached_url == sp_url else []
        cached_ts = st._spoolman_cache_ts if cached_url == sp_url else 0.0
        manual_snapshot = {
            str(k): dict(v)
            for k, v in (getattr(st, "manual_spoolman_selections", {}) or {}).items()
            if isinstance(v, dict)
        }

    if not sp_url:
        for slot in raw_slots:
            slot.pop("spoolman", None)
        out["slots"] = raw_slots
        status_info = {
            "enabled": False,
            "connected": False,
            "url": "",
            "spools_count": 0,
            "matched_count": 0,
            "conflict_count": 0,
            "error": "",
        }
        with st._lock:
            if expected_epoch is None or getattr(st, "profile_epoch", 0) == expected_epoch:
                st._spoolman_spools = []
                st._spoolman_cache_url = ""
                st._spoolman_cache_ts = 0.0
                st.last_spoolman_status = dict(status_info)
        out["spoolman_status"] = status_info
        return out

    spools = cached_spools
    connected = bool(cached_spools and cached_ts > 0.0)
    err_msg = ""
    now = time.monotonic()
    should_fetch = force_fetch or cached_url != sp_url or (now - cached_ts) > 15.0 or cached_ts <= 0.0

    if should_fetch:
        client = SpoolmanClient(sp_url, timeout=DEFAULT_SPOOLMAN_TIMEOUT)
        try:
            spools = client.fetch_spools()
            connected = True
            with st._lock:
                if expected_epoch is None or getattr(st, "profile_epoch", 0) == expected_epoch:
                    st._spoolman_spools = list(spools)
                    st._spoolman_cache_url = sp_url
                    st._spoolman_cache_ts = now
        except Exception as exc:
            connected = False
            err_msg = str(exc)

    invalidated_slots: List[str] = []
    enriched_slots, matched_count = match_spoolman_to_slots(
        raw_slots,
        spools,
        manual_selections=manual_snapshot,
        invalidated_slots_out=invalidated_slots,
    )
    if invalidated_slots and (connected or not should_fetch):
        pruned_any = False
        with st._lock:
            if expected_epoch is None or getattr(st, "profile_epoch", 0) == expected_epoch:
                cur_manual = getattr(st, "manual_spoolman_selections", None)
                if isinstance(cur_manual, dict):
                    for s_key in invalidated_slots:
                        if s_key in cur_manual:
                            cur_manual.pop(s_key, None)
                            pruned_any = True
        if pruned_any:
            st.save_config()

    conflict_count = sum(
        1
        for sl in enriched_slots
        if isinstance(sl.get("spoolman"), dict) and sl["spoolman"].get("conflict")
    )
    status_info = {
        "enabled": True,
        "connected": connected,
        "url": sp_url,
        "spools_count": len(spools),
        "matched_count": matched_count,
        "conflict_count": conflict_count,
        "error": err_msg,
    }
    with st._lock:
        if expected_epoch is None or getattr(st, "profile_epoch", 0) == expected_epoch:
            st.last_spoolman_status = dict(status_info)
    out["slots"] = enriched_slots
    out["spoolman_status"] = status_info
    return out


def _build_host_permission_urls(url_val: Any, *, include_port80: bool = False) -> List[str]:
    """Build deduplicated permission URLs starting with the base host (no path/query/port)."""
    norm = MoonrakerClient.normalize_url(str(url_val or "")) if include_port80 else SpoolmanClient.normalize_url(url_val)
    if not norm:
        return []
    parsed = urllib.parse.urlsplit(norm)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    urls: List[str] = []
    if host:
        urls.append(f"{scheme}://{host}")
        urls.append(f"{scheme}://{host}/")
    if parsed.netloc:
        urls.append(f"{scheme}://{parsed.netloc}")
    urls.append(norm)
    if include_port80 and host:
        urls.extend((f"{scheme}://{host}:7125", f"{scheme}://{host}:80"))
    return list(dict.fromkeys(u for u in urls if u))


def _request_optional_http_permission(url_val: Any, *, include_port80: bool = False) -> None:
    """If running inside OrcaSlicer and URL is non-empty, request host-based HTTP permission dynamically."""
    if not HAS_ORCA_RUNTIME:
        return
    urls = _build_host_permission_urls(url_val, include_port80=include_port80)
    if not urls:
        return
    try:
        orca.request_permissions(network_http=urls)
    except Exception:
        pass


_PRIVATE_LAN_IP_RE = re.compile(
    r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
_HOME_POSIX_RE = re.compile(r"(?<![A-Za-z0-9_])/home/[A-Za-z0-9._-]+(?=/|$)")
_HOME_MAC_RE = re.compile(r"(?<![A-Za-z0-9_])/Users/[A-Za-z0-9._-]+(?=/|$)")
_HOME_WIN_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\/:*?\"<>|\r\n]+(?=\\|$)")
_URL_CREDS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
_KEY_TOKEN_RE = re.compile(r"(?i)(\b(?:x-api-key|api_key|token|secret|password)\b\s*[:=]\s*)([^\s,;'\"]+)")


def anonymize_diagnostic_text(text: Any, extra_secrets: Optional[List[str]] = None) -> str:
    """Redact personal paths, home usernames, RFC1918 LAN IPs, URL credentials, and API keys."""
    out = str(text or "")
    if not out:
        return ""
    for sec in extra_secrets or ():
        s = str(sec or "").strip()
        if s and len(s) >= 3:
            out = out.replace(s, "[REDACTED]")
    try:
        home_dir = os.path.expanduser("~")
        if home_dir and home_dir not in ("~", "/", "\\"):
            out = out.replace(home_dir, "/home/user")
    except Exception:
        pass
    out = _URL_CREDS_RE.sub(r"\1***:***@", out)
    out = _KEY_TOKEN_RE.sub(r"\1[REDACTED]", out)
    out = _HOME_POSIX_RE.sub("/home/user", out)
    out = _HOME_MAC_RE.sub("/Users/user", out)
    out = _HOME_WIN_RE.sub(r"C:\\Users\\user", out)
    out = _PRIVATE_LAN_IP_RE.sub("192.0.2.10", out)
    return out


def build_anonymized_diagnostics(state: Optional["PluginState"] = None) -> str:
    """Build a release-safe plain-text diagnostics summary with zero personal paths, IPs, or keys."""
    st = state or globals().get("GLOBAL_STATE")
    if st is None:
        return "Kobra-Van-Kenorca Diagnostics: unavailable"
    with st._lock:
        secrets = [st.api_key] if st.api_key else []
        moon_set = bool(MoonrakerClient.normalize_url(st.moonraker_url))
        sm_set = bool(SpoolmanClient.normalize_url(st.spoolman_url))
        sm_stat = dict(st.last_spoolman_status or {})
        ace_st = dict(st.last_ace_status or {})
        theme = str(getattr(st, "theme_mode", DEFAULT_THEME_MODE) or DEFAULT_THEME_MODE)
        scheme = str(getattr(st, "color_scheme", DEFAULT_COLOR_SCHEME) or DEFAULT_COLOR_SCHEME)
        lang = str(st.language or DEFAULT_LANGUAGE)
        gcode_base = os.path.basename(st.last_gcode_path) if st.last_gcode_path else "(none)"
        manual_slots = sorted(str(k) for k in (getattr(st, "manual_spoolman_selections", {}) or {}).keys())
        paths_raw = st.get_resolved_paths_summary()

    conn_state = str(ace_st.get("connection_state") or ("offline" if moon_set else "unconfigured"))
    events = get_diagnostic_load_events(st, limit=8)
    lines = [
        f"Plugin: {PLUGIN_SHORT_NAME}",
        f"ConnectionState: {conn_state} | MoonrakerConfigured: {moon_set} | PrintState: {ace_st.get('print_state', 'unknown')}",
        f"SpoolmanConfigured: {sm_set} | Connected: {bool(sm_stat.get('connected'))} | Spools: {sm_stat.get('spools_count', 0)} | Matched: {sm_stat.get('matched_count', 0)}",
        f"ManualSlotOverrides: {', '.join(manual_slots) if manual_slots else 'none'}",
        f"Appearance: theme={theme}, scheme={scheme}, language={lang}",
        f"ActiveGcodeFile: {gcode_base}",
        f"StorageDir: {anonymize_diagnostic_text(paths_raw.get('storage_dir') or '(none)', secrets)}",
        f"ConfigFile: {anonymize_diagnostic_text(paths_raw.get('config_path') or '(none)', secrets)}",
        "RecentLoadEvents:",
    ]
    if not events:
        lines.append("  (none)")
    else:
        for ev in events:
            lines.append("  " + anonymize_diagnostic_text(_format_diag_log_line(ev), secrets))
    return anonymize_diagnostic_text("\n".join(lines), secrets)


# 7. Path, Atomic I/O, Staged Sidecar & Model-Name Utilities
_SYSTEM_TEMP_DIRS = ("/tmp", "/var/tmp", "/usr/tmp", "/private/tmp", "/private/var/tmp")
_MODEL_IGNORE_EXTENSIONS = (
    ".gcode.3mf",
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


def _norm_cmp_path(path: str) -> str:
    """Normalize path separators and case (on Windows) for reliable cross-platform comparisons."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        resolved = os.path.abspath(raw)
    return os.path.normcase(resolved)


def _is_inside_dir(path: str, directory: str) -> bool:
    if not path or not directory:
        return False
    try:
        p_norm = _norm_cmp_path(path)
        d_norm = _norm_cmp_path(directory)
        if not p_norm or not d_norm:
            return False
        if p_norm == d_norm:
            return True
        try:
            return os.path.commonpath([p_norm, d_norm]) == d_norm
        except ValueError:
            return False
    except OSError:
        return False


def _is_system_temp_path(path: str, storage_dir: str = "") -> bool:
    """
    Return True if `path` resides under a platform system/user temp directory (Linux `/tmp`,
    macOS `/var/folders` / `/private/var/folders`, Windows `%TEMP%` / `%TMP%` / `Local\\Temp`)
    and is NOT inside the plugin's own `storage_dir`. Never calls `tempfile.gettempdir()`.
    """
    raw = str(path or "").strip()
    if not raw:
        return False
    if storage_dir and _is_inside_dir(raw, storage_dir):
        return False
    norm_raw = raw.replace("\\", "/").lower()
    try:
        norm_resolved = _norm_cmp_path(raw).replace("\\", "/").lower()
    except OSError:
        norm_resolved = norm_raw

    for target in (norm_raw, norm_resolved):
        for sys_tmp in _SYSTEM_TEMP_DIRS:
            if target == sys_tmp or target.startswith(sys_tmp + "/"):
                return True
        if target.startswith("/var/folders/") or target.startswith("/private/var/folders/"):
            return True
        if "/appdata/local/temp/" in target or target.endswith("/appdata/local/temp"):
            return True

    return False


def _is_ephemeral_upload_temp_source(path: str) -> bool:
    """
    Return True for OrcaSlicer's per-upload ephemeral staging files
    (e.g. `/tmp/.OrcaSlicer.upload.<uuid>`), which OrcaSlicer deletes immediately
    after the upload/pipeline finishes even though the plate remains sliced.
    """
    base = os.path.basename(str(path or "").strip()).lower()
    if not base:
        return False
    return (
        base.startswith(".orcaslicer.upload.")
        or base.startswith("orcaslicer.upload.")
        or ".upload." in base
    )


def _is_preview_temp_source(path: str, storage_dir: str = "") -> bool:
    """Return True if `path` looks like an OrcaSlicer transient preview/slice temp G-code."""
    raw = str(path or "").strip()
    if not raw or _is_ephemeral_upload_temp_source(raw):
        return False
    low = raw.lower()
    return bool(
        _is_system_temp_path(raw, storage_dir)
        or os.path.basename(raw).startswith(".")
        or "metadata" in low
        or "orcaslicer" in low
    )


_O_NOFOLLOW_FLAG = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0


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
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW_FLAG
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


def _open_static_temp_file(
    tmp_path: str,
    *,
    mode: str = "wb",
    encoding: Optional[str] = None,
) -> Tuple[str, Any]:
    """Open a deterministic static `.tmp` path via `os.open` without random names or `tempfile`."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW_FLAG
    fd = os.open(tmp_path, flags, 0o600)
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
        raise


def _atomic_write_with(
    target_path: str,
    writer_fn: Any,
    suffix: str = ".tmp",
    mode: str = "wb",
    encoding: Optional[str] = None,
    ensure_parent: bool = False,
    static_temp: bool = False,
) -> None:
    """Atomically write `target_path` via temp file + `os.fsync` + `os.replace`."""
    target_abs = os.path.abspath(target_path)
    target_dir = os.path.dirname(target_abs) or "."
    if ensure_parent and not os.path.isdir(target_dir):
        os.makedirs(target_dir, exist_ok=True)
    tmp_path: Optional[str] = None
    try:
        if static_temp:
            tmp_path, handle = _open_static_temp_file(
                f"{target_abs}.tmp",
                mode=mode,
                encoding=encoding,
            )
        else:
            tmp_path, handle = _open_atomic_temp_file(
                target_dir,
                prefix=".ace_tmp_",
                suffix=suffix,
                mode=mode,
                encoding=encoding,
            )
        with handle:
            writer_fn(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_abs)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass


def _atomic_write_bytes(target_path: str, payload: bytes, suffix: str = ".gcode", ensure_parent: bool = False) -> None:
    _atomic_write_with(target_path, lambda f: f.write(payload), suffix=suffix, mode="wb", ensure_parent=ensure_parent)


def _atomic_write_json(
    target_path: str,
    data: Any,
    suffix: str = ".json",
    ensure_ascii: bool = True,
    ensure_parent: bool = False,
    static_temp: bool = True,
) -> None:
    _atomic_write_with(
        target_path,
        lambda f: json.dump(data, f, indent=2, ensure_ascii=ensure_ascii),
        suffix=suffix,
        mode="w",
        encoding="utf-8",
        ensure_parent=ensure_parent,
        static_temp=static_temp,
    )


def _sidecar_path_for(gcode_path: str) -> str:
    return f"{gcode_path}.meta.json"


def _read_provenance_sidecar(gcode_path: str) -> Dict[str, Any]:
    raw = str(gcode_path or "").strip()
    if not raw:
        return {}
    meta_path = _sidecar_path_for(raw)
    try:
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _write_provenance_sidecar_atomic(staged_path: str, provenance: Dict[str, Any]) -> None:
    _atomic_write_json(
        _sidecar_path_for(staged_path),
        provenance,
        suffix=".meta.json",
        ensure_ascii=False,
    )


def _get_file_mtime_ns(path: str, default: int = 0) -> int:
    """Return file mtime in nanoseconds, or `default` on `OSError`."""
    try:
        st_f = os.stat(path)
        return int(getattr(st_f, "st_mtime_ns", int(float(st_f.st_mtime) * 1_000_000_000)))
    except OSError:
        return int(default)


def _strip_model_extension(raw_name: str) -> str:
    base = os.path.basename(str(raw_name or "").strip())
    while base:
        low = base.lower()
        stripped = False
        for ext in _MODEL_IGNORE_EXTENSIONS:
            if low.endswith(ext) and len(base) > len(ext):
                base = base[: -len(ext)].strip()
                stripped = True
                break
        if not stripped:
            break
    return base.strip()


# 8. Diagnostic Load Logging & Persistent Plugin State
# --- TEMP DIAGNOSTIC LOAD LOGGING (START) ---
DIAG_LOAD_LOGGING_ENABLED: bool = False
_DIAG_LOAD_LOCK = threading.Lock()
_DIAG_MAX_EVENTS = 120
_DIAG_LOG_MAX_BYTES = 524288


def _is_diag_load_logging_enabled(state: Optional["PluginState"] = None) -> bool:
    if DIAG_LOAD_LOGGING_ENABLED:
        return True
    st = state or globals().get("GLOBAL_STATE")
    return bool(getattr(st, "diag_load_logging", False)) if st is not None else False


def _compute_file_sha256(path: str, max_bytes: int = 32 * 1024 * 1024) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        if not os.path.isfile(raw):
            return ""
        size = os.path.getsize(raw)
        if max_bytes > 0 and size > max_bytes:
            return f"size:{size}"
        h = hashlib.sha256()
        with open(raw, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _describe_sidecar_status(
    gcode_path: str,
    sidecar: Optional[Dict[str, Any]] = None,
    storage_dir: str = "",
) -> Dict[str, Any]:
    if isinstance(sidecar, dict) and sidecar:
        sc = dict(sidecar)
    elif gcode_path and not _is_system_temp_path(gcode_path, storage_dir):
        sc = _read_provenance_sidecar(gcode_path)
    else:
        sc = {}
    if not sc:
        return {"sidecar_present": False}
    return {
        "sidecar_present": True,
        "is_cleared": bool(sc.get("is_cleared")),
        "is_stale": bool(sc.get("is_stale")),
        "is_superseded": bool(sc.get("is_superseded")),
        "slicing_run_id": str(sc.get("slicing_run_id") or ""),
        "slicing_run_seq": int(sc.get("slicing_run_seq", 0) or 0),
        "project_model": str(sc.get("project_model") or ""),
        "created_ns": int(sc.get("created_ns", 0) or 0),
        "cleared_ns": int(sc.get("cleared_ns", 0) or 0),
        "cleared_seq": int(sc.get("cleared_seq", 0) or 0),
    }


def _format_diag_log_line(entry: Dict[str, Any]) -> str:
    fn = str(entry.get("filename") or "-")
    src = str(entry.get("file_source") or "-")
    trig = str(entry.get("trigger") or "-")
    dec = str(entry.get("decision") or "-")
    rsn = str(entry.get("reason") or "-")
    act = str(entry.get("active_model") or "-")
    rid = str(entry.get("slicing_run_id") or "-")
    sha_short = str(entry.get("sha256") or "")[:12] or "-"
    sc = entry.get("sidecar_status") or {}
    sc_txt = (
        f"clr={sc.get('is_cleared')} stl={sc.get('is_stale')} sup={sc.get('is_superseded')} seq={sc.get('slicing_run_seq', 0)}"
        if sc.get("sidecar_present")
        else "no_sidecar"
    )
    return (
        f"[DIAG {entry.get('timestamp_iso', '')}] trigger={trig} decision={dec} "
        f"source={src} file={fn} run={rid} sha={sha_short} active_model={act} "
        f"sidecar=({sc_txt}) reason={rsn}"
    )


def _record_diagnostic_load_event(
    state: Optional["PluginState"],
    trigger: str,
    decision: str,
    reason: str = "",
    file_source: str = "",
    file_path: str = "",
    slicing_run_id: str = "",
    sidecar: Optional[Dict[str, Any]] = None,
    compute_sha: bool = False,
    allow_temp_probe: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not _is_diag_load_logging_enabled(state):
        return {}
    try:
        st = state or globals().get("GLOBAL_STATE")
        storage_dir = ""
        active_model_name = ""
        if st is not None:
            try:
                storage_dir = st.resolve_storage_dir()
            except Exception:
                storage_dir = ""
            try:
                snap = st.get_host_ui_snapshot()
                active_model_name = str((snap.get("active_model") or {}).get("display_name") or "").strip()
            except Exception:
                active_model_name = ""
        raw_path = str(file_path or "").strip()
        is_sys_tmp = bool(raw_path and _is_system_temp_path(raw_path, storage_dir))
        sc_info = (
            _describe_sidecar_status(raw_path, sidecar=sidecar, storage_dir=storage_dir)
            if (raw_path and (not is_sys_tmp or sidecar is not None))
            else {"sidecar_present": False}
        )
        run_id_val = str(slicing_run_id or (sidecar or {}).get("slicing_run_id") or sc_info.get("slicing_run_id") or "")
        sha_val = str((sidecar or {}).get("sha256") or "")
        if not sha_val and compute_sha and raw_path and (allow_temp_probe or not is_sys_tmp):
            sha_val = _compute_file_sha256(raw_path)
        ts_ns = time.time_ns()
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ns // 1_000_000_000))
        entry: Dict[str, Any] = {
            "timestamp_ns": ts_ns,
            "timestamp_iso": ts_iso,
            "trigger": str(trigger or ""),
            "decision": str(decision or ""),
            "reason": str(reason or ""),
            "file_source": str(file_source or ""),
            "filename": os.path.basename(raw_path) if raw_path else "",
            "file_path": raw_path,
            "slicing_run_id": run_id_val,
            "sha256": sha_val,
            "active_model": active_model_name,
            "sidecar_status": sc_info,
        }
        if extra and isinstance(extra, dict):
            entry["extra"] = dict(extra)

        with _DIAG_LOAD_LOCK:
            if st is not None:
                events = getattr(st, "_diag_load_events", None)
                if not isinstance(events, list):
                    events = []
                    st._diag_load_events = events
                events.append(entry)
                if len(events) > _DIAG_MAX_EVENTS:
                    del events[: len(events) - _DIAG_MAX_EVENTS]

            if storage_dir:
                try:
                    os.makedirs(storage_dir, exist_ok=True)
                    log_path = os.path.join(storage_dir, "diagnostics_gcode_load.jsonl")
                    if os.path.isfile(log_path) and os.path.getsize(log_path) > _DIAG_LOG_MAX_BYTES:
                        with open(log_path, "r", encoding="utf-8", errors="replace") as rf:
                            tail_lines = rf.readlines()[-(_DIAG_MAX_EVENTS // 2):]
                        with open(log_path, "w", encoding="utf-8") as wf:
                            wf.writelines(tail_lines)
                    with open(log_path, "a", encoding="utf-8") as af:
                        af.write(json.dumps(entry, ensure_ascii=False) + "\n")
                except Exception:
                    pass
        return entry
    except Exception:
        return {}


def get_diagnostic_load_events(state: Optional["PluginState"] = None, limit: int = 50) -> List[Dict[str, Any]]:
    st = state or globals().get("GLOBAL_STATE")
    if st is None:
        return []
    with _DIAG_LOAD_LOCK:
        events = list(getattr(st, "_diag_load_events", []) or [])
    if limit > 0:
        return events[-int(limit):]
    return events
# --- TEMP DIAGNOSTIC LOAD LOGGING (END) ---


_MODEL_MATCH_GENERIC_WORDS = frozenset({
    "plate", "untitled", "model", "object", "part", "body", "mesh", "print",
    "3d", "orca", "orcaslicer", "project", "noname", "default", "unnamed",
    "buildplate", "gcode", "export", "sliced", "staged",
})
_MODEL_MATCH_NOISE_WORDS = _MODEL_MATCH_GENERIC_WORDS | frozenset({
    "pla", "petg", "abs", "asa", "tpu", "pc", "pa", "pa6", "pva", "hips",
    "cf", "gf", "silk", "matte", "basic", "plus", "pro", "hs", "hf",
    "mm", "copy", "final", "test", "v1", "v2", "v3",
})


def _is_generic_stem_pure(stem: str) -> bool:
    norm = re.sub(r"[^a-z0-9]+", "", str(stem or "").lower())
    if not norm:
        return True
    if norm in _MODEL_MATCH_GENERIC_WORDS:
        return True
    if re.fullmatch(r"(?:plate|part|object|body|model|mesh|print|copy|run)[_-]?\d*", norm):
        return True
    if re.fullmatch(r"\d+", norm):
        return True
    return False


def _extract_model_stem_from_filename(filename: str) -> str:
    stem = _strip_model_extension(filename)
    if not stem:
        return ""
    # Trim trailing OrcaSlicer export suffixes such as _PLA_0.2_2h13m, _plate_1, _run2
    trimmed = re.sub(
        r"(?:[_-]+(?:plate|p)[_-]?\d+|[_-]+run[_-]?\d+|[_-]+(?:pla|petg|abs|asa|tpu|pc|pa|pa6|pva|hips)(?:[_-].*)?|[_-]+\d+(?:\.\d+)?(?:mm)?(?:[_-]+\d+[hm].*)?|[_-]+\d+h\d+m.*|[_-]+\d+m)$",
        "",
        stem,
        flags=re.IGNORECASE,
    ).strip(" _-")
    return trimmed or stem


def _normalize_model_tokens(text: str) -> Tuple[str, List[str]]:
    cleaned = _extract_model_stem_from_filename(text)
    norm_key = re.sub(r"[^a-z0-9]+", "", cleaned.lower())
    raw_tokens = [tok for tok in re.split(r"[^a-z0-9]+", cleaned.lower()) if tok]
    sig_tokens = [
        tok
        for tok in raw_tokens
        if len(tok) >= 2
        and tok not in _MODEL_MATCH_NOISE_WORDS
        and not re.fullmatch(r"\d+(?:h\d+m?|m|mm)?", tok)
    ]
    return norm_key, sig_tokens


def _check_candidate_model_mismatch(
    cand_path: str,
    sidecar: Optional[Dict[str, Any]] = None,
    state: Optional["PluginState"] = None,
) -> Tuple[bool, str]:
    st = state or globals().get("GLOBAL_STATE")
    if st is None:
        return False, ""
    try:
        snap = st.get_host_ui_snapshot()
    except Exception:
        snap = {}
    act_info = snap.get("active_model") if isinstance(snap, dict) else {}
    if not isinstance(act_info, dict):
        act_info = {}
    act_raw_names: List[str] = []
    if act_info.get("display_name"):
        act_raw_names.append(str(act_info["display_name"]))
    for c_name in act_info.get("candidates") or []:
        if c_name:
            act_raw_names.append(str(c_name))

    active_pairs: List[Tuple[str, str, List[str]]] = []
    for raw_act in act_raw_names:
        stem = _strip_model_extension(raw_act)
        if not stem or _is_generic_stem_pure(stem):
            continue
        k_act, t_act = _normalize_model_tokens(stem)
        if k_act:
            active_pairs.append((raw_act, k_act, t_act))
    if not active_pairs:
        return False, ""

    storage_dir = st.resolve_storage_dir()
    if isinstance(sidecar, dict) and sidecar:
        sc = dict(sidecar)
    elif cand_path and not _is_system_temp_path(cand_path, storage_dir):
        sc = _read_provenance_sidecar(cand_path)
    else:
        sc = {}
    cand_sources: List[str] = []
    if sc.get("project_model"):
        cand_sources.append(str(sc["project_model"]))
    if sc.get("output_name"):
        cand_sources.append(str(sc["output_name"]))
    if sc.get("original_source_name"):
        cand_sources.append(str(sc["original_source_name"]))
    if cand_path:
        cand_sources.append(os.path.basename(cand_path))

    cand_pairs: List[Tuple[str, str, List[str]]] = []
    for raw_cand in cand_sources:
        extracted = _extract_model_stem_from_filename(raw_cand)
        if not extracted or _is_generic_stem_pure(extracted):
            continue
        k_cand, t_cand = _normalize_model_tokens(extracted)
        if k_cand:
            cand_pairs.append((extracted, k_cand, t_cand))
    if not cand_pairs:
        return False, ""

    for _, k_act, t_act in active_pairs:
        for _, k_cand, t_cand in cand_pairs:
            if k_act == k_cand:
                return False, ""
            if len(k_act) >= 4 and len(k_cand) >= 4 and (k_act in k_cand or k_cand in k_act):
                return False, ""
            if t_act and t_cand:
                long_a = {tok for tok in t_act if len(tok) >= 3}
                long_b = {tok for tok in t_cand if len(tok) >= 3}
                if long_a and long_b and (long_a & long_b):
                    return False, ""

    return True, (
        f"model_mismatch(active={active_pairs[0][0]!r}, candidate={cand_pairs[0][0]!r})"
    )


def _validate_restorable_gcode_state(
    state: "PluginState",
    gcode_path: str,
    provenance: Optional[Dict[str, Any]] = None,
    source_hint: str = "",
) -> Tuple[bool, str]:
    cand = str(gcode_path or "").strip()
    if not cand:
        return False, "empty"
    storage_dir = ""
    try:
        storage_dir = state.resolve_storage_dir()
    except Exception:
        storage_dir = ""
    if _is_system_temp_path(cand, storage_dir):
        return False, "rejected:sys_temp_remembered"
    if not os.path.isfile(cand):
        return False, "rejected:missing_file"

    sc = _read_provenance_sidecar(cand)
    merged: Dict[str, Any] = {}
    if isinstance(provenance, dict):
        merged.update(provenance)
    merged.update(sc)

    if bool(merged.get("is_cleared")):
        return False, "rejected:is_cleared"
    if bool(merged.get("is_superseded")):
        return False, "rejected:is_superseded"
    if bool(merged.get("is_stale")):
        return False, "rejected:is_stale"

    cleared_run_seq = int(getattr(state, "_cleared_run_seq", 0) or 0)
    cleared_before_ns = int(getattr(state, "cleared_before_mtime_ns", 0) or 0)
    cleared_at_ns = int(getattr(state, "_cleared_at_ns", 0) or 0)
    seq_val = int(merged.get("slicing_run_seq", 0) or 0)
    created_ns = int(merged.get("created_ns", 0) or 0)

    if cleared_run_seq > 0 and seq_val > 0 and seq_val <= cleared_run_seq:
        return False, "rejected:cleared_run_seq"

    try:
        st_stat = os.stat(cand)
        cand_mtime_ns = int(getattr(st_stat, "st_mtime_ns", int(float(st_stat.st_mtime) * 1_000_000_000)))
    except OSError:
        return False, "rejected:stat_failed"

    if seq_val == 0:
        if cleared_before_ns > 0 and cand_mtime_ns <= cleared_before_ns:
            return False, "rejected:cleared_before_mtime_ns"
        if cleared_at_ns > 0 and created_ns > 0 and created_ns <= cleared_at_ns:
            return False, "rejected:cleared_at_ns"
    elif cleared_at_ns > 0 and created_ns > 0 and created_ns <= cleared_at_ns and seq_val <= cleared_run_seq:
        return False, "rejected:cleared_at_ns"

    mismatch, mismatch_reason = _check_candidate_model_mismatch(cand, sidecar=merged, state=state)
    if mismatch and str(source_hint or "").strip() != "file_picker":
        return False, f"rejected:{mismatch_reason}"

    return True, "accepted:restorable"


def _fallback_config_path_for_dir(app_dir_name: str) -> str:
    """Resolve platform-native fallback config path for a given application folder name."""
    if os.name == "nt":
        base_dir = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base_dir, app_dir_name, "state.json")
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", app_dir_name, "state.json"
        )
    return os.path.join(
        os.path.expanduser("~"), ".local", "share", app_dir_name, "state.json"
    )


def _default_fallback_config_path() -> str:
    """Resolve platform-native fallback config path when running outside OrcaSlicer."""
    return _fallback_config_path_for_dir("Kobra-Van-Kenorca")


def _legacy_fallback_config_path() -> str:
    """Resolve primary legacy fallback config path for one-time migration from `KobraOrca`."""
    return _fallback_config_path_for_dir("KobraOrca")


def _legacy_fallback_config_paths() -> Tuple[str, ...]:
    """Resolve legacy fallback config paths in priority order for one-time migration."""
    return (
        _fallback_config_path_for_dir("KobraOrca"),
        _fallback_config_path_for_dir("vKobraACE"),
        _fallback_config_path_for_dir("kobra_s1_ace_plugin"),
    )


class PluginState:
    """Manages plugin settings, Moonraker URL, pre-print toggles, and active G-code state."""

    FALLBACK_CONFIG_PATH = _default_fallback_config_path()
    USER_BACKUP_DEBOUNCE_SEC = 0.75
    _DURABLE_CAP_CONFIG_KEY = "kobra_van_kenorca_settings"
    _LEGACY_DURABLE_CAP_CONFIG_KEYS = ("kobra_orca_settings", "vkobra_ace_settings")
    DURABLE_SETTINGS_KEYS = (
        "language",
        "theme_mode",
        "color_scheme",
        "moonraker_url",
        "api_key",
        "spoolman_url",
        "default_gcode_dir",
        "manual_spoolman_selections",
        "enable_port80_fallback",
        "auto_register_pipeline",
        "bed_leveling",
        "skip_purge_for_already_loaded_tool",
        "cut_tip_after_print",
        "force_initial_purge",
        "external_spool_mode",
        "external_spool_id",
        "start_print_immediately",
        "diag_load_logging",
        "dryer_unit",
        "dryer_temp",
        "dryer_duration",
        "configured_ace_count",
        "last_detected_ace_count",
        "printer_profiles",
        "active_printer_id",
        "filament_diameter",
        "prediction_reserve_g",
        "startup_purge_length_mm",
        "tool_map",
        "tool_map_sources",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hw_action_lock = threading.Lock()
        self._storage_cache_lock = threading.Lock()
        self._backup_timer_lock = threading.Lock()
        self._backup_timer: Optional[threading.Timer] = None
        self._pending_user_backup: Optional[Tuple[str, Dict[str, Any]]] = None
        self._os_backup_denied: bool = False
        self._dryer_action_seq: int = 0
        self.profile_epoch: int = 0
        self._cached_storage_dir: str = ""
        self._host_ui_snapshot: Dict[str, Any] = {
            "active_model": {"display_name": "", "candidates": [], "plate_index": None},
            "filaments": [],
            "filaments_captured": False,
            "preset_fingerprint": {"digest": "", "captured": False, "printer_name": "", "process_name": "", "filament_names": []},
            "captured_ns": 0,
        }
        self._last_invalidation_reason: str = ""
        self._auto_slice_in_progress: bool = False
        self._last_slice_completed_ns: int = 0
        self._last_slice_satisfied_run_id: str = ""
        self._last_slice_satisfied_created_ns: int = 0
        self._last_slice_plate_label: str = ""
        self._last_slice_plate_index: Optional[int] = None
        self._last_slice_model: str = ""
        self._slicing_run_counter: int = 0
        self._last_slicing_run_id: str = ""
        self._last_staged_provenance: Dict[str, Any] = {}
        self._last_preview_temp_path: str = ""
        self._last_preview_temp_existed: bool = False
        self._reslice_preview_temp_path: str = ""
        self._reslice_preview_prev_mtime_ns: int = 0
        self._slice_host_fingerprint: Dict[str, Any] = {}
        self._session_manual_tool_map: Dict[int, Dict[str, Any]] = {}
        self._pending_preview_lock = threading.Lock()
        self._pending_preview_gcode: Dict[str, Any] = {}
        self.cleared_before_mtime_ns: int = 0
        self._cleared_at_ns: int = 0
        self._cleared_run_seq: int = 0
        self._diag_load_events: List[Dict[str, Any]] = []
        self._pipeline_retry_count: int = 0
        self._pipeline_last_error: str = ""
        self.slicing_modal_mode: str = "window_modal"
        self.language: str = DEFAULT_LANGUAGE
        self.theme_mode: str = DEFAULT_THEME_MODE
        self.color_scheme: str = DEFAULT_COLOR_SCHEME
        self.moonraker_url: str = DEFAULT_MOONRAKER_URL
        self.api_key: str = ""
        self.spoolman_url: str = ""
        self.default_gcode_dir: str = ""
        self.manual_spoolman_selections: Dict[str, Dict[str, Any]] = {}
        self._spoolman_spools: List[Dict[str, Any]] = []
        self._spoolman_cache_url: str = ""
        self._spoolman_cache_ts: float = 0.0
        self.last_spoolman_status: Dict[str, Any] = {
            "enabled": False,
            "connected": False,
            "url": "",
            "spools_count": 0,
            "matched_count": 0,
            "conflict_count": 0,
            "error": "",
        }
        self.enable_port80_fallback: bool = True
        self.bed_leveling: bool = True
        self.skip_purge_for_already_loaded_tool: bool = True
        self.cut_tip_after_print: bool = False
        self.force_initial_purge: bool = False
        self.external_spool_mode: bool = False
        self.external_spool_id: Optional[int] = None
        self.start_print_immediately: bool = False
        self.dryer_unit: str = "all"
        self.dryer_temp: int = 45
        self.dryer_duration: int = 240
        self.configured_ace_count: str = DEFAULT_ACE_COUNT
        self.last_detected_ace_count: int = 0
        self.active_printer_id: str = DEFAULT_PRINTER_ID
        self.printer_profiles: List[Dict[str, Any]] = [
            {
                "id": DEFAULT_PRINTER_ID,
                "name": DEFAULT_PRINTER_NAME,
                "moonraker_url": self.moonraker_url,
                "api_key": self.api_key,
                "enable_port80_fallback": self.enable_port80_fallback,
                "spoolman_url": self.spoolman_url,
                "configured_ace_count": self.configured_ace_count,
                "last_detected_ace_count": self.last_detected_ace_count,
            }
        ]
        self.filament_diameter: float = DEFAULT_FILAMENT_DIAMETER
        self.prediction_reserve_g: float = DEFAULT_PREDICTION_RESERVE_G
        self.startup_purge_length_mm: float = DEFAULT_STARTUP_PURGE_LENGTH_MM
        self.diag_load_logging: bool = False
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

    def set_appearance(
        self,
        theme_mode: Any = None,
        color_scheme: Any = None,
        save: bool = True,
    ) -> Tuple[str, str]:
        """Update theme_mode (`auto`|`dark`|`light`|`kobraorca`) and/or color_scheme cleanly and persist."""
        with self._lock:
            if theme_mode is not None:
                tm = str(theme_mode).strip().lower()
                if tm in SUPPORTED_THEMES:
                    self.theme_mode = tm
            if color_scheme is not None:
                sc = str(color_scheme).strip().lower()
                if sc in SUPPORTED_COLOR_SCHEMES:
                    self.color_scheme = sc
            if save:
                self.save_config()
            return self.theme_mode, self.color_scheme

    def set_manual_slot_spool(
        self,
        slot_tool: int,
        spool_id: Optional[int],
        sku: str = "",
        save: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
        """Persist or clear manual Spoolman spool assignment for one physical ACE slot."""
        slot_key = str(int(slot_tool))
        sku_clean = _clean_spoolman_str(sku)
        sku_norm = _norm_sku_key(sku_clean)
        with self._lock:
            if spool_id is None:
                self.manual_spoolman_selections.pop(slot_key, None)
            else:
                self.manual_spoolman_selections[slot_key] = {
                    "spool_id": int(spool_id),
                    "sku_norm": sku_norm,
                    "sku_raw": sku_clean,
                }
            if save:
                self.save_config()
            return {k: dict(v) for k, v in self.manual_spoolman_selections.items()}

    def get_resolved_paths_summary(self) -> Dict[str, str]:
        """Return dynamically resolved config/storage/staged paths for the UI Paths section."""
        storage_dir = self.resolve_storage_dir()
        cfg_path = self.resolve_config_path() or ""
        staged_dir = os.path.join(storage_dir, "staged_gcode") if storage_dir else ""
        exported_dir = os.path.join(storage_dir, "exported_gcode") if storage_dir else ""
        diag_path = os.path.join(storage_dir, "diagnostics_gcode_load.jsonl") if storage_dir else ""
        try:
            home_dir = os.path.expanduser("~")
            downloads_dir = os.path.join(home_dir, "Downloads") if home_dir and home_dir != "~" else ""
        except Exception:
            downloads_dir = ""
        return {
            "config_path": cfg_path,
            "storage_dir": storage_dir,
            "staged_dir": staged_dir,
            "exported_dir": exported_dir,
            "downloads_dir": downloads_dir,
            "diagnostics_log_path": diag_path,
        }

    def record_session_manual_mapping(
        self,
        tool_map: Optional[Dict[int, int]] = None,
        tool_map_sources: Optional[Dict[int, str]] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """Remember manual extruder-to-slot overrides for the active session across re-slices."""
        with self._lock:
            tmap = dict(tool_map) if isinstance(tool_map, dict) else dict(self.tool_map)
            tsrc = dict(tool_map_sources) if isinstance(tool_map_sources, dict) else dict(self.tool_map_sources)
            fil_by_tool: Dict[int, Dict[str, Any]] = {}
            for fil in self.slicer_filaments:
                if isinstance(fil, dict) and fil.get("slicer_tool") is not None:
                    try:
                        fil_by_tool[int(fil["slicer_tool"])] = fil
                    except (ValueError, TypeError):
                        pass
            for k_raw, src_val in tsrc.items():
                try:
                    k_int = int(k_raw)
                except (ValueError, TypeError):
                    continue
                if str(src_val or "").strip().lower() == "manual" and k_int in tmap:
                    try:
                        slot_int = int(tmap[k_int])
                    except (ValueError, TypeError):
                        continue
                    fil_info = fil_by_tool.get(k_int) or {}
                    self._session_manual_tool_map[k_int] = {
                        "slot": slot_int,
                        "material": str(fil_info.get("material") or "").strip().upper(),
                        "color": str(fil_info.get("color") or "").strip().upper(),
                    }
                elif k_int in self._session_manual_tool_map and str(src_val or "").strip().lower() in ("auto", "default"):
                    self._session_manual_tool_map.pop(k_int, None)
            return {k: dict(v) for k, v in self._session_manual_tool_map.items()}

    def clear_session_manual_mapping(self) -> None:
        """Clear session-remembered manual extruder-to-slot overrides."""
        with self._lock:
            self._session_manual_tool_map.clear()

    def update_slicer_filaments(self, filaments: List[Dict[str, Any]]) -> bool:
        """
        Update active slicer filaments and reset stale `tool_map` to 1:1 if the
        filament configuration or order changed, while restoring matching session
        manual overrides (`_session_manual_tool_map`). Returns True if `tool_map` was reset.
        """
        with self._lock:
            new_sig = compute_filament_signature(filaments)
            reset_occurred = False
            sig_changed = new_sig != self.filament_signature
            self.mapping_warnings = []
            if self.filament_signature and sig_changed:
                old_len = len(self.slicer_filaments)
                new_len = len(filaments)
                self.tool_map = {i: i for i in range(MAX_ACE_SLOTS)}
                self.tool_map_sources = {i: "default" for i in range(MAX_ACE_SLOTS)}
                self.material_mismatch_confirmed = False
                self.mapping_warnings.append(
                    t("map.warn.order_reset", lang=self.language, old_len=old_len, new_len=new_len)
                )
                reset_occurred = True
            if sig_changed and self._session_manual_tool_map:
                max_slots = self.resolve_known_effective_slot_count() or MAX_ACE_SLOTS
                for fil in filaments:
                    if not isinstance(fil, dict) or fil.get("slicer_tool") is None:
                        continue
                    try:
                        s_tool = int(fil["slicer_tool"])
                    except (ValueError, TypeError):
                        continue
                    mem = self._session_manual_tool_map.get(s_tool)
                    if not isinstance(mem, dict):
                        continue
                    if str(self.tool_map_sources.get(s_tool, "default")).lower() != "default":
                        continue
                    try:
                        target_slot = int(mem.get("slot", -1))
                    except (ValueError, TypeError):
                        continue
                    if not (0 <= target_slot < max_slots):
                        continue
                    mem_mat = str(mem.get("material") or "").strip().upper()
                    new_mat = str(fil.get("material") or "").strip().upper()
                    if not mem_mat or not new_mat or mem_mat == new_mat:
                        self.tool_map[s_tool] = target_slot
                        self.tool_map_sources[s_tool] = "manual"
            self.slicer_filaments = list(filaments)
            self.filament_signature = new_sig
            return reset_occurred

    def cache_orca_storage_dir(self, allow_in_worker: bool = False) -> str:
        """
        Resolve and cache `orca.host.plugin.storage()` strictly in permitted plugin callbacks.
        Never invokes `plugin.storage()` from background worker threads once cached or denied.
        """
        with self._storage_cache_lock:
            if self._cached_storage_dir:
                return self._cached_storage_dir
        if not HAS_ORCA_RUNTIME:
            return ""
        if _is_worker_thread_context() and not allow_in_worker:
            return ""
        try:
            host_mod = getattr(orca, "host", None)
            plugin_host = getattr(host_mod, "plugin", None)
            storage_fn = getattr(plugin_host, "storage", None) if plugin_host is not None else None
            if callable(storage_fn):
                storage_dir = str(storage_fn() or "").strip()
                if storage_dir:
                    with self._storage_cache_lock:
                        if not self._cached_storage_dir:
                            self._cached_storage_dir = storage_dir
                        return self._cached_storage_dir
        except Exception:
            return ""
        return ""

    def next_slicing_run_id(self) -> Tuple[int, str]:
        """Increment and return monotonic `(seq, run_id)` for consecutive slicing disambiguation."""
        with self._lock:
            base_counter = max(
                int(getattr(self, "_slicing_run_counter", 0) or 0),
                int(getattr(self, "_cleared_run_seq", 0) or 0),
            )
            self._slicing_run_counter = base_counter + 1
            seq = self._slicing_run_counter
            run_id = f"run_{seq}_{time.time_ns()}"
            self._last_slicing_run_id = run_id
            return seq, run_id

    def record_slicing_completed(
        self,
        plate_label: str = "",
        plate_index: Optional[int] = None,
        ui_snapshot: Optional[Dict[str, Any]] = None,
        satisfied_run_id: str = "",
        satisfied_created_ns: int = 0,
    ) -> int:
        """
        Record that OrcaSlicer completed a slice job (e.g. `SlicingJobComplete` without file export).
        Updates timestamp and UI snapshot without ever loading old staged G-code files.
        """
        now_ns = time.time_ns()
        snap = ui_snapshot if isinstance(ui_snapshot, dict) else self.get_host_ui_snapshot()
        active_model_dict = snap.get("active_model") if isinstance(snap.get("active_model"), dict) else {}
        model_name = str(active_model_dict.get("display_name") or "")
        snap_plate = active_model_dict.get("plate_index")
        resolved_plate = plate_index if plate_index is not None else snap_plate
        with self._lock:
            self._last_slice_completed_ns = max(now_ns, self._last_slice_completed_ns + 1)
            self._last_slice_satisfied_run_id = str(satisfied_run_id or "")
            self._last_slice_satisfied_created_ns = int(satisfied_created_ns or 0)
            self._last_slice_plate_label = str(plate_label or "").strip()
            self._last_slice_plate_index = resolved_plate
            self._last_slice_model = model_name
            return self._last_slice_completed_ns

    def get_host_ui_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            am = self._host_ui_snapshot.get("active_model")
            fils = self._host_ui_snapshot.get("filaments")
            pf = self._host_ui_snapshot.get("preset_fingerprint")
            return {
                "active_model": {
                    "display_name": str((am or {}).get("display_name") or ""),
                    "candidates": list((am or {}).get("candidates") or []),
                    "plate_index": (am or {}).get("plate_index"),
                    "host_has_slice": (am or {}).get("host_has_slice"),
                    "host_gcode_path": str((am or {}).get("host_gcode_path") or ""),
                    "host_gcode_attr_present": bool((am or {}).get("host_gcode_attr_present", False)),
                },
                "filaments": [dict(f) for f in (fils or []) if isinstance(f, dict)],
                "filaments_captured": bool(self._host_ui_snapshot.get("filaments_captured", False)),
                "preset_fingerprint": {
                    "digest": str((pf or {}).get("digest") or ""),
                    "captured": bool((pf or {}).get("captured", False)),
                    "printer_name": str((pf or {}).get("printer_name") or ""),
                    "process_name": str((pf or {}).get("process_name") or ""),
                    "filament_names": list((pf or {}).get("filament_names") or []),
                },
                "captured_ns": int(self._host_ui_snapshot.get("captured_ns", 0) or 0),
            }

    def refresh_host_ui_snapshot(
        self,
        capture_model: bool = True,
        capture_filaments: bool = True,
    ) -> Dict[str, Any]:
        """
        Capture `orca.host.plater()`/`model()`/`preset_bundle()` safely on the UI/Main thread
        and store an immutable snapshot for background worker threads.
        No-op returning cached snapshot when called from a worker thread.
        """
        if _is_worker_thread_context():
            return self.get_host_ui_snapshot()
        model_snap: Optional[Dict[str, Any]] = None
        fils_snap: Optional[List[Dict[str, Any]]] = None
        preset_snap: Optional[Dict[str, Any]] = None
        if capture_model:
            try:
                model_snap = _query_orca_active_model_info_direct()
            except Exception:
                model_snap = None
        if capture_filaments:
            try:
                fils_snap = _read_host_filaments_from_preset_bundle()
            except Exception:
                fils_snap = None
        if capture_model or capture_filaments:
            try:
                preset_snap = _read_host_preset_fingerprint()
            except Exception:
                preset_snap = None
        now_ns = time.time_ns()
        with self._lock:
            if model_snap is not None and (
                model_snap.get("display_name")
                or model_snap.get("candidates")
                or model_snap.get("host_has_slice") is not None
                or model_snap.get("host_gcode_attr_present")
                or _has_live_host_model_api()
            ):
                self._host_ui_snapshot["active_model"] = {
                    "display_name": str(model_snap.get("display_name") or ""),
                    "candidates": list(model_snap.get("candidates") or []),
                    "plate_index": model_snap.get("plate_index"),
                    "host_has_slice": model_snap.get("host_has_slice"),
                    "host_gcode_path": str(model_snap.get("host_gcode_path") or ""),
                    "host_gcode_attr_present": bool(model_snap.get("host_gcode_attr_present", False)),
                }
            if fils_snap:
                self._host_ui_snapshot["filaments"] = [dict(f) for f in fils_snap]
                self._host_ui_snapshot["filaments_captured"] = True
            if isinstance(preset_snap, dict) and preset_snap.get("captured"):
                self._host_ui_snapshot["preset_fingerprint"] = dict(preset_snap)
            self._host_ui_snapshot["captured_ns"] = now_ns
        return self.get_host_ui_snapshot()

    def resolve_config_path(self) -> Optional[str]:
        """
        Resolve plugin state file using cached `orca.host.plugin.storage()` when running in
        OrcaSlicer (which lives inside `data_dir()` and avoids `~/.config` `"conf"` blocks).
        Returns `None` if called before OrcaSlicer's host is ready or from a worker without cache.
        """
        if HAS_ORCA_RUNTIME:
            with self._storage_cache_lock:
                cached_dir = self._cached_storage_dir
            if cached_dir:
                return os.path.join(cached_dir, "state.json")
            if _is_worker_thread_context():
                return None
            storage_dir = self.cache_orca_storage_dir()
            if storage_dir:
                return os.path.join(storage_dir, "state.json")
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

    def _extract_durable_settings(self) -> Dict[str, Any]:
        full = self.to_dict()
        return {k: full[k] for k in self.DURABLE_SETTINGS_KEYS if k in full}

    def _is_os_backup_denied(self) -> bool:
        with self._backup_timer_lock:
            return bool(self._os_backup_denied)

    def _mark_os_backup_denied(self) -> None:
        with self._backup_timer_lock:
            self._os_backup_denied = True
            if self._backup_timer is not None:
                try:
                    self._backup_timer.cancel()
                except Exception:
                    pass
                self._backup_timer = None
            self._pending_user_backup = None

    @staticmethod
    def _check_install_state_allows_external_backup(backup_path: str) -> Optional[bool]:
        """
        Inspect `.install_state.json` inside the plugin's own install directory if present.
        Returns `False` when OrcaSlicer installed the plugin without `full_access` or matching
        `fs_readwrite`, `True` when permitted, or `None` when unknown (e.g. standalone/tests).
        """
        try:
            mod_dir = os.path.dirname(os.path.abspath(__file__))
            candidates = (
                os.path.join(mod_dir, ".install_state.json"),
                os.path.join(os.path.dirname(mod_dir), ".install_state.json"),
            )
            install_data: Optional[Dict[str, Any]] = None
            for cand in candidates:
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        install_data = loaded
                        break
                except FileNotFoundError:
                    continue
                except Exception:
                    return None
            if install_data is None:
                return None
            perms = install_data.get("permissions")
            if not isinstance(perms, dict):
                return None
            if bool(perms.get("full_access", False)):
                return True
            target_abs = os.path.normcase(os.path.abspath(backup_path))
            target_dir = os.path.normcase(os.path.dirname(target_abs))
            for grant in perms.get("fs_readwrite") or []:
                g_str = str(grant or "").strip()
                if not g_str:
                    continue
                g_prefix = g_str[:-2] if g_str.endswith(("/*", "\\*")) else g_str.rstrip("/\\")
                g_norm = os.path.normcase(os.path.abspath(g_prefix))
                if target_dir == g_norm or target_dir.startswith(g_norm + os.sep) or target_abs == g_norm:
                    return True
            return False
        except Exception:
            return None

    def _should_use_user_backup_path(self, primary_config_path: Optional[str]) -> bool:
        if not HAS_ORCA_RUNTIME or _is_worker_thread_context() or self._is_os_backup_denied():
            return False
        backup_path = str(getattr(self, "FALLBACK_CONFIG_PATH", "") or "").strip()
        if not backup_path:
            return False
        if primary_config_path:
            try:
                if os.path.abspath(primary_config_path) == os.path.abspath(backup_path):
                    return False
                tmp_root = os.path.abspath(tempfile.gettempdir())
                prim_abs = os.path.abspath(primary_config_path)
                back_abs = os.path.abspath(backup_path)
                if (prim_abs == tmp_root or prim_abs.startswith(tmp_root + os.sep)) and not (
                    back_abs == tmp_root or back_abs.startswith(tmp_root + os.sep)
                ):
                    return False
            except Exception:
                return False
        allowed = self._check_install_state_allows_external_backup(backup_path)
        if allowed is not True:
            self._mark_os_backup_denied()
            return False
        return True

    @staticmethod
    def _read_orca_cap_settings(cap: Any) -> Dict[str, Any]:
        if cap is None:
            return {}
        get_cfg = getattr(cap, "get_config", None)
        if not callable(get_cfg):
            return {}
        try:
            raw = get_cfg()
            parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
            if isinstance(parsed, dict):
                sub = parsed.get(PluginState._DURABLE_CAP_CONFIG_KEY)
                if isinstance(sub, dict):
                    return sub
                for legacy_key in PluginState._LEGACY_DURABLE_CAP_CONFIG_KEYS:
                    legacy_sub = parsed.get(legacy_key)
                    if isinstance(legacy_sub, dict):
                        return legacy_sub
        except Exception:
            pass
        return {}

    @staticmethod
    def _write_orca_cap_settings(cap: Any, durable: Dict[str, Any]) -> None:
        if cap is None:
            return
        save_cfg = getattr(cap, "save_config", None)
        if not callable(save_cfg):
            return
        try:
            existing: Dict[str, Any] = {}
            get_cfg = getattr(cap, "get_config", None)
            if callable(get_cfg):
                try:
                    raw = get_cfg()
                    parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
                    if isinstance(parsed, dict):
                        existing = dict(parsed)
                except Exception:
                    existing = {}
            existing[PluginState._DURABLE_CAP_CONFIG_KEY] = dict(durable)
            save_cfg(json.dumps(existing))
        except Exception:
            pass

    LEGACY_FALLBACK_CONFIG_PATH = _legacy_fallback_config_path()
    LEGACY_FALLBACK_CONFIG_PATHS = _legacy_fallback_config_paths()

    def _try_load_json_dict(self, path: str, *, mark_denied_on_error: bool = False) -> Dict[str, Any]:
        if not path:
            return {}
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (PermissionError, OSError):
            if mark_denied_on_error:
                self._mark_os_backup_denied()
        except Exception:
            if mark_denied_on_error:
                self._mark_os_backup_denied()
        return {}

    def _migrate_legacy_fallback_if_needed(self, target_path: str) -> Dict[str, Any]:
        """One-time migration from legacy `vKobraACE`/`kobra_s1_ace_plugin` `state.json` into `KobraOrca/state.json`."""
        if HAS_ORCA_RUNTIME and self._is_os_backup_denied():
            return {}
        if not target_path:
            return {}
        candidates: List[str] = []
        primary_legacy = str(getattr(self, "LEGACY_FALLBACK_CONFIG_PATH", "") or "").strip()
        if primary_legacy:
            candidates.append(primary_legacy)
        for cand in getattr(self, "LEGACY_FALLBACK_CONFIG_PATHS", ()) or ():
            c_str = str(cand or "").strip()
            if c_str and c_str not in candidates:
                candidates.append(c_str)
        if not candidates:
            return {}
        try:
            target_abs = os.path.abspath(target_path)
            if os.path.isfile(target_path):
                return {}
        except (PermissionError, OSError):
            if HAS_ORCA_RUNTIME:
                self._mark_os_backup_denied()
            return {}
        except Exception:
            if HAS_ORCA_RUNTIME:
                self._mark_os_backup_denied()
            return {}
        legacy_data: Dict[str, Any] = {}
        for legacy_path in candidates:
            try:
                if os.path.abspath(legacy_path) == target_abs:
                    continue
            except (PermissionError, OSError):
                if HAS_ORCA_RUNTIME:
                    self._mark_os_backup_denied()
                return {}
            except Exception:
                continue
            loaded = self._try_load_json_dict(legacy_path, mark_denied_on_error=HAS_ORCA_RUNTIME)
            if loaded:
                legacy_data = loaded
                break
        if not legacy_data:
            return {}
        try:
            _atomic_write_json(
                target_path,
                legacy_data,
                suffix=".state.json",
                ensure_parent=True,
                static_temp=True,
            )
        except (PermissionError, OSError):
            if HAS_ORCA_RUNTIME:
                self._mark_os_backup_denied()
        except Exception:
            if HAS_ORCA_RUNTIME:
                self._mark_os_backup_denied()
        return legacy_data

    def load_config(self, cap: Any = None) -> None:
        with self._lock:
            config_path = self.resolve_config_path()
            loaded_primary = False
            if config_path:
                data = self._try_load_json_dict(config_path)
                if not data and not HAS_ORCA_RUNTIME:
                    try:
                        if os.path.abspath(config_path) == os.path.abspath(str(self.FALLBACK_CONFIG_PATH or "")):
                            data = self._migrate_legacy_fallback_if_needed(config_path)
                    except Exception:
                        pass
                if data:
                    self.apply_dict(data, save=False)
                    loaded_primary = True

            if HAS_ORCA_RUNTIME:
                need_fallback = (not loaded_primary) or (
                    not str(self.moonraker_url or "").strip()
                    and not str(self.spoolman_url or "").strip()
                    and not str(self.api_key or "").strip()
                )
                if need_fallback:
                    restored: Dict[str, Any] = {}
                    active_cap = cap if cap is not None else _ACTIVE_PAGE_CAPABILITY
                    cap_data = self._read_orca_cap_settings(active_cap)
                    if cap_data:
                        restored = cap_data
                    has_conn_in_restored = bool(
                        str(restored.get("moonraker_url") or "").strip()
                        or str(restored.get("spoolman_url") or "").strip()
                        or str(restored.get("api_key") or "").strip()
                    )
                    if (
                        not loaded_primary
                        and (not restored or not has_conn_in_restored)
                        and self._should_use_user_backup_path(config_path)
                    ):
                        backup_path = str(getattr(self, "FALLBACK_CONFIG_PATH", "") or "").strip()
                        bdata = self._try_load_json_dict(backup_path, mark_denied_on_error=True)
                        if not bdata and backup_path and not self._is_os_backup_denied():
                            bdata = self._migrate_legacy_fallback_if_needed(backup_path)
                        if bdata:
                            restored = bdata
                    if restored:
                        self.apply_dict(restored, save=False, apply_gcode_state=False)
                        if not loaded_primary and config_path:
                            try:
                                _atomic_write_json(
                                    config_path,
                                    self.to_dict(),
                                    suffix=".state.json",
                                    ensure_parent=True,
                                    static_temp=True,
                                )
                            except Exception:
                                pass
                        if not cap_data and active_cap is not None:
                            self._write_orca_cap_settings(active_cap, self._extract_durable_settings())

    def _write_user_backup_now(self, backup_path: str, durable: Dict[str, Any]) -> None:
        if not backup_path or self._is_os_backup_denied():
            return
        try:
            _atomic_write_json(
                backup_path,
                durable,
                suffix=".state.json",
                ensure_parent=True,
                static_temp=True,
            )
        except (PermissionError, OSError):
            self._mark_os_backup_denied()
        except Exception:
            self._mark_os_backup_denied()

    def flush_user_backup(self) -> None:
        """Flush any debounced OS user-backup write immediately."""
        pending: Optional[Tuple[str, Dict[str, Any]]] = None
        with self._backup_timer_lock:
            if self._backup_timer is not None:
                try:
                    self._backup_timer.cancel()
                except Exception:
                    pass
                self._backup_timer = None
            pending = self._pending_user_backup
            self._pending_user_backup = None
            denied = bool(self._os_backup_denied)
        if not denied and pending is not None:
            self._write_user_backup_now(pending[0], pending[1])

    def _schedule_user_backup(self, backup_path: str, durable: Dict[str, Any], *, flush_now: bool = False) -> None:
        if self._is_os_backup_denied():
            return
        delay = float(getattr(self, "USER_BACKUP_DEBOUNCE_SEC", 0.75) or 0.0)
        if flush_now or delay <= 0.0:
            with self._backup_timer_lock:
                if self._backup_timer is not None:
                    try:
                        self._backup_timer.cancel()
                    except Exception:
                        pass
                    self._backup_timer = None
                self._pending_user_backup = None
                if self._os_backup_denied:
                    return
            self._write_user_backup_now(backup_path, dict(durable))
            return
        with self._backup_timer_lock:
            if self._os_backup_denied:
                return
            if self._backup_timer is not None:
                try:
                    self._backup_timer.cancel()
                except Exception:
                    pass
            self._pending_user_backup = (backup_path, dict(durable))
            timer = threading.Timer(delay, self.flush_user_backup)
            timer.daemon = True
            self._backup_timer = timer
            timer.start()

    def save_config(self, cap: Any = None, *, flush_backup: bool = False) -> None:
        with self._lock:
            config_path = self.resolve_config_path()
            if config_path:
                try:
                    _atomic_write_json(
                        config_path,
                        self.to_dict(),
                        suffix=".state.json",
                        ensure_parent=True,
                        static_temp=True,
                    )
                except Exception:
                    pass
            if HAS_ORCA_RUNTIME:
                durable = self._extract_durable_settings()
                active_cap = cap if cap is not None else _ACTIVE_PAGE_CAPABILITY
                self._write_orca_cap_settings(active_cap, durable)
                if self._should_use_user_backup_path(config_path):
                    backup_path = str(getattr(self, "FALLBACK_CONFIG_PATH", "") or "").strip()
                    if backup_path:
                        self._schedule_user_backup(backup_path, durable, flush_now=flush_backup)

    @staticmethod
    def _normalize_printer_profile(raw: Any, fallback_idx: int = 1) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        pid_raw = str(raw.get("id") or "").strip()
        pid = re.sub(r"[^a-zA-Z0-9_-]+", "_", pid_raw).strip("_").lower() or f"printer_{fallback_idx}"
        name = str(raw.get("name") or "").strip() or (DEFAULT_PRINTER_NAME if fallback_idx == 1 else f"Printer {fallback_idx}")
        mr_url = MoonrakerClient.normalize_url(str(raw.get("moonraker_url") or ""))
        api_key = str(raw.get("api_key") or "")
        p80 = bool(raw.get("enable_port80_fallback", True))
        sm_url = SpoolmanClient.normalize_url(str(raw.get("spoolman_url") or ""))
        raw_cnt = str(raw.get("configured_ace_count") or DEFAULT_ACE_COUNT).strip().lower()
        cfg_cnt = raw_cnt if raw_cnt in SUPPORTED_ACE_COUNTS else DEFAULT_ACE_COUNT
        try:
            det_cnt = max(0, min(MAX_ACE_UNITS, int(raw.get("last_detected_ace_count") or 0)))
        except (ValueError, TypeError):
            det_cnt = 0
        return {
            "id": pid,
            "name": name,
            "moonraker_url": mr_url,
            "api_key": api_key,
            "enable_port80_fallback": p80,
            "spoolman_url": sm_url,
            "configured_ace_count": cfg_cnt,
            "last_detected_ace_count": det_cnt,
        }

    def _ensure_printer_profiles_unlocked(self) -> None:
        if not isinstance(getattr(self, "printer_profiles", None), list) or not self.printer_profiles:
            self.active_printer_id = DEFAULT_PRINTER_ID
            self.printer_profiles = [
                {
                    "id": DEFAULT_PRINTER_ID,
                    "name": DEFAULT_PRINTER_NAME,
                    "moonraker_url": MoonrakerClient.normalize_url(self.moonraker_url),
                    "api_key": str(self.api_key or ""),
                    "enable_port80_fallback": bool(self.enable_port80_fallback),
                    "spoolman_url": SpoolmanClient.normalize_url(self.spoolman_url),
                    "configured_ace_count": str(self.configured_ace_count or DEFAULT_ACE_COUNT),
                    "last_detected_ace_count": int(self.last_detected_ace_count or 0),
                }
            ]
            return
        ids = [p["id"] for p in self.printer_profiles if isinstance(p, dict) and p.get("id")]
        if not ids:
            self.printer_profiles = []
            self._ensure_printer_profiles_unlocked()
            return
        if self.active_printer_id not in ids:
            self.active_printer_id = ids[0]

    def _find_profile_unlocked(self, profile_id: str) -> Optional[Dict[str, Any]]:
        target = str(profile_id or "").strip().lower()
        for prof in self.printer_profiles:
            if isinstance(prof, dict) and str(prof.get("id") or "").lower() == target:
                return prof
        return None

    def _sync_active_profile_from_state_unlocked(self, *, name: Optional[str] = None) -> None:
        self._ensure_printer_profiles_unlocked()
        prof = self._find_profile_unlocked(self.active_printer_id) or self.printer_profiles[0]
        if name is not None and str(name).strip():
            prof["name"] = str(name).strip()
        prof["moonraker_url"] = MoonrakerClient.normalize_url(self.moonraker_url)
        prof["api_key"] = str(self.api_key or "")
        prof["enable_port80_fallback"] = bool(self.enable_port80_fallback)
        prof["spoolman_url"] = SpoolmanClient.normalize_url(self.spoolman_url)
        raw_cnt = str(self.configured_ace_count or DEFAULT_ACE_COUNT).strip().lower()
        prof["configured_ace_count"] = raw_cnt if raw_cnt in SUPPORTED_ACE_COUNTS else DEFAULT_ACE_COUNT
        prof["last_detected_ace_count"] = max(0, min(MAX_ACE_UNITS, int(self.last_detected_ace_count or 0)))

    def _apply_profile_to_state_unlocked(self, prof: Dict[str, Any]) -> None:
        prev_sm = str(self.spoolman_url or "")
        self.active_printer_id = str(prof.get("id") or DEFAULT_PRINTER_ID)
        self.moonraker_url = MoonrakerClient.normalize_url(str(prof.get("moonraker_url") or ""))
        self.api_key = str(prof.get("api_key") or "")
        self.enable_port80_fallback = bool(prof.get("enable_port80_fallback", True))
        new_sm = SpoolmanClient.normalize_url(str(prof.get("spoolman_url") or ""))
        if new_sm != prev_sm:
            self._spoolman_spools = []
            self._spoolman_cache_url = ""
            self._spoolman_cache_ts = 0.0
            self.last_spoolman_status = {
                "enabled": bool(new_sm),
                "connected": False,
                "url": new_sm,
                "spools_count": 0,
                "matched_count": 0,
                "conflict_count": 0,
                "error": "",
            }
        self.spoolman_url = new_sm
        raw_cnt = str(prof.get("configured_ace_count") or DEFAULT_ACE_COUNT).strip().lower()
        self.configured_ace_count = raw_cnt if raw_cnt in SUPPORTED_ACE_COUNTS else DEFAULT_ACE_COUNT
        try:
            self.last_detected_ace_count = max(0, min(MAX_ACE_UNITS, int(prof.get("last_detected_ace_count") or 0)))
        except (ValueError, TypeError):
            self.last_detected_ace_count = 0

    def switch_printer_profile(self, profile_id: str, *, save: bool = True) -> Optional[Dict[str, Any]]:
        """Save current active profile settings, activate `profile_id`, clear stale ACE cache, and persist."""
        with self._lock:
            self._sync_active_profile_from_state_unlocked()
            target = self._find_profile_unlocked(profile_id)
            if target is None:
                return None
            if target["id"] != self.active_printer_id:
                self.profile_epoch += 1
                self._dryer_action_seq += 1
                self._apply_profile_to_state_unlocked(target)
                self.last_ace_status = {}
                self.mapping_warnings = []
            snap = dict(target)
        if save:
            self.save_config()
        return snap

    def save_printer_profile(
        self,
        *,
        profile_id: Optional[str] = None,
        name: str = "",
        moonraker_url: Optional[str] = None,
        api_key: Optional[str] = None,
        enable_port80_fallback: Optional[bool] = None,
        spoolman_url: Optional[str] = None,
        configured_ace_count: Optional[str] = None,
        activate: bool = True,
        save: bool = True,
    ) -> Dict[str, Any]:
        """Create or update/rename a printer profile, optionally activating it cleanly."""
        with self._lock:
            self._sync_active_profile_from_state_unlocked()
            target = self._find_profile_unlocked(profile_id) if profile_id else None
            clean_name = str(name or "").strip()
            if target is None:
                existing_ids = {str(p.get("id") or "").lower() for p in self.printer_profiles}
                next_num = len(self.printer_profiles) + 1
                cand_id = f"printer_{next_num}"
                while cand_id in existing_ids:
                    next_num += 1
                    cand_id = f"printer_{next_num}"
                default_name = t("ui.settings.conn.profile_default_new", lang=self.language, n=len(self.printer_profiles) + 1)
                target = {
                    "id": cand_id,
                    "name": clean_name or default_name,
                    "moonraker_url": MoonrakerClient.normalize_url(str(moonraker_url if moonraker_url is not None else "")),
                    "api_key": str(api_key if api_key is not None else ""),
                    "enable_port80_fallback": bool(enable_port80_fallback if enable_port80_fallback is not None else True),
                    "spoolman_url": SpoolmanClient.normalize_url(str(spoolman_url if spoolman_url is not None else self.spoolman_url)),
                    "configured_ace_count": (
                        str(configured_ace_count).strip().lower()
                        if str(configured_ace_count or "").strip().lower() in SUPPORTED_ACE_COUNTS
                        else DEFAULT_ACE_COUNT
                    ),
                    "last_detected_ace_count": 0,
                }
                self.printer_profiles.append(target)
            else:
                if clean_name:
                    target["name"] = clean_name
                if moonraker_url is not None:
                    target["moonraker_url"] = MoonrakerClient.normalize_url(str(moonraker_url))
                if api_key is not None:
                    target["api_key"] = str(api_key)
                if enable_port80_fallback is not None:
                    target["enable_port80_fallback"] = bool(enable_port80_fallback)
                if spoolman_url is not None:
                    target["spoolman_url"] = SpoolmanClient.normalize_url(str(spoolman_url))
                if configured_ace_count is not None:
                    rc = str(configured_ace_count).strip().lower()
                    target["configured_ace_count"] = rc if rc in SUPPORTED_ACE_COUNTS else DEFAULT_ACE_COUNT
            if activate:
                changed_id = target["id"] != self.active_printer_id
                self._apply_profile_to_state_unlocked(target)
                if changed_id:
                    self.profile_epoch += 1
                    self._dryer_action_seq += 1
                    self.last_ace_status = {}
                    self.mapping_warnings = []
            snap = dict(target)
        if save:
            self.save_config()
        return snap

    def delete_printer_profile(self, profile_id: str, *, save: bool = True) -> Tuple[bool, str]:
        """Delete `profile_id` unless it is the last profile. Returns `(deleted, deleted_name_or_reason)`."""
        with self._lock:
            self._sync_active_profile_from_state_unlocked()
            if len(self.printer_profiles) <= 1:
                return False, "last_profile"
            target = self._find_profile_unlocked(profile_id)
            if target is None:
                return False, "not_found"
            del_name = str(target.get("name") or target.get("id") or "")
            self.printer_profiles = [p for p in self.printer_profiles if p.get("id") != target["id"]]
            if self.active_printer_id == target["id"]:
                self.profile_epoch += 1
                self._dryer_action_seq += 1
                self._apply_profile_to_state_unlocked(self.printer_profiles[0])
                self.last_ace_status = {}
                self.mapping_warnings = []
        if save:
            self.save_config()
        return True, del_name

    def get_postprocess_options(self) -> Tuple[float, Dict[int, int], bool, bool, bool, bool, List[Dict[str, Any]], str]:
        """Return an atomic snapshot of `(filament_diameter, tool_map, bed_leveling, skip_purge, cut_post, force_init, ace_slots, language)`."""
        with self._lock:
            return (
                self.filament_diameter,
                dict(self.tool_map),
                self.bed_leveling,
                self.skip_purge_for_already_loaded_tool,
                self.cut_tip_after_print,
                self.force_initial_purge,
                list((self.last_ace_status or {}).get("slots", [])),
                self.language,
            )

    def apply_dict(
        self,
        data: Dict[str, Any],
        save: bool = True,
        apply_gcode_state: bool = True,
    ) -> None:
        with self._lock:
            if "printer_profiles" in data and isinstance(data["printer_profiles"], list):
                cleaned_profiles: List[Dict[str, Any]] = []
                seen_ids: Set[str] = set()
                for idx, raw_p in enumerate(data["printer_profiles"], start=1):
                    np = self._normalize_printer_profile(raw_p, fallback_idx=idx)
                    if np and np["id"] not in seen_ids:
                        seen_ids.add(np["id"])
                        cleaned_profiles.append(np)
                if cleaned_profiles:
                    self.printer_profiles = cleaned_profiles
                    req_active = str(data.get("active_printer_id") or self.active_printer_id or "").strip().lower()
                    matched_active = self._find_profile_unlocked(req_active) or self.printer_profiles[0]
                    self._apply_profile_to_state_unlocked(matched_active)
            elif "active_printer_id" in data:
                req_active = str(data.get("active_printer_id") or "").strip().lower()
                matched_active = self._find_profile_unlocked(req_active)
                if matched_active is not None:
                    self._apply_profile_to_state_unlocked(matched_active)

            if "language" in data:
                self.language = normalize_language(data["language"])
            if "theme_mode" in data:
                tm = str(data.get("theme_mode") or "").strip().lower()
                if tm in SUPPORTED_THEMES:
                    self.theme_mode = tm
            if "color_scheme" in data:
                cs = str(data.get("color_scheme") or "").strip().lower()
                if cs in SUPPORTED_COLOR_SCHEMES:
                    self.color_scheme = cs
            if "default_gcode_dir" in data:
                self.default_gcode_dir = str(data.get("default_gcode_dir") or "").strip()
            if "manual_spoolman_selections" in data and isinstance(data["manual_spoolman_selections"], dict):
                cleaned_manual: Dict[str, Dict[str, Any]] = {}
                for k, v in data["manual_spoolman_selections"].items():
                    if isinstance(v, dict) and v.get("spool_id") is not None and not isinstance(v.get("spool_id"), bool):
                        try:
                            s_key = str(int(str(k).strip()))
                            s_id = int(str(v.get("spool_id")).strip())
                            cleaned_manual[s_key] = {
                                "spool_id": s_id,
                                "sku_norm": _norm_sku_key(v.get("sku_norm") or v.get("sku_raw") or ""),
                                "sku_raw": _clean_spoolman_str(v.get("sku_raw") or ""),
                            }
                        except (ValueError, TypeError):
                            pass
                self.manual_spoolman_selections = cleaned_manual
            if "moonraker_url" in data:
                self.moonraker_url = MoonrakerClient.normalize_url(str(data["moonraker_url"] or ""))
            if "api_key" in data:
                self.api_key = str(data["api_key"] or "")
            if "spoolman_url" in data:
                norm_sm = SpoolmanClient.normalize_url(data["spoolman_url"])
                if norm_sm != self.spoolman_url:
                    self._spoolman_spools = []
                    self._spoolman_cache_url = ""
                    self._spoolman_cache_ts = 0.0
                    self.last_spoolman_status = {
                        "enabled": bool(norm_sm),
                        "connected": False,
                        "url": norm_sm,
                        "spools_count": 0,
                        "matched_count": 0,
                        "conflict_count": 0,
                        "error": "",
                    }
                    if not norm_sm and isinstance(self.last_ace_status, dict):
                        cleaned_slots = []
                        for sl in self.last_ace_status.get("slots") or []:
                            if isinstance(sl, dict):
                                sc = dict(sl)
                                sc.pop("spoolman", None)
                                cleaned_slots.append(sc)
                        self.last_ace_status["slots"] = cleaned_slots
                if not norm_sm:
                    self.external_spool_id = None
                self.spoolman_url = norm_sm
            for attr in (
                "enable_port80_fallback",
                "auto_register_pipeline",
                "bed_leveling",
                "skip_purge_for_already_loaded_tool",
                "cut_tip_after_print",
                "force_initial_purge",
                "external_spool_mode",
                "start_print_immediately",
                "diag_load_logging",
            ):
                if attr in data:
                    setattr(self, attr, bool(data[attr]))
            if "configured_ace_count" in data:
                raw_cnt = str(data.get("configured_ace_count") or "auto").strip().lower()
                self.configured_ace_count = raw_cnt if raw_cnt in SUPPORTED_ACE_COUNTS else DEFAULT_ACE_COUNT
            if "external_spool_id" in data:
                raw_ext_id = data.get("external_spool_id")
                if raw_ext_id is None or raw_ext_id == "" or isinstance(raw_ext_id, bool):
                    self.external_spool_id = None
                else:
                    try:
                        eid = int(raw_ext_id)
                        self.external_spool_id = eid if eid > 0 else None
                    except (ValueError, TypeError):
                        self.external_spool_id = None
            if "last_detected_ace_count" in data:
                try:
                    det_cnt = int(data.get("last_detected_ace_count") or 0)
                    self.last_detected_ace_count = max(0, min(MAX_ACE_UNITS, det_cnt))
                except (ValueError, TypeError):
                    pass
            if "dryer_unit" in data:
                raw_u = str(data["dryer_unit"] or "all").strip().lower()
                valid_units = ("all",) + tuple(str(i) for i in range(MAX_ACE_UNITS))
                self.dryer_unit = raw_u if raw_u in valid_units else "all"
            for attr, lo, hi in (("dryer_temp", 20, 70), ("dryer_duration", 1, 1440)):
                if attr in data:
                    try:
                        setattr(self, attr, max(lo, min(hi, int(round(float(data[attr]))))))
                    except (ValueError, TypeError):
                        pass
            if "filament_diameter" in data:
                try:
                    val = float(data["filament_diameter"])
                    if val > 0:
                        self.filament_diameter = val
                except (ValueError, TypeError):
                    pass
            if "prediction_reserve_g" in data:
                try:
                    res_v = float(data["prediction_reserve_g"])
                    if math.isfinite(res_v):
                        self.prediction_reserve_g = round(max(0.0, min(500.0, res_v)), 1)
                except (ValueError, TypeError):
                    pass
            if "startup_purge_length_mm" in data:
                try:
                    sp_v = float(data["startup_purge_length_mm"])
                    if math.isfinite(sp_v):
                        self.startup_purge_length_mm = round(max(0.0, min(1000.0, sp_v)), 1)
                except (ValueError, TypeError):
                    pass
            explicit_sources = "tool_map_sources" in data and isinstance(data["tool_map_sources"], dict)
            map_changed = False
            if "tool_map" in data and isinstance(data["tool_map"], dict):
                new_map: Dict[int, int] = {}
                for k, v in data["tool_map"].items():
                    try:
                        new_map[int(k)] = int(v)
                    except (ValueError, TypeError):
                        pass
                if new_map:
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
                if apply_gcode_state:
                    self.material_mismatch_confirmed = bool(data["material_mismatch_confirmed"])
                elif bool(self.last_gcode_path):
                    self.material_mismatch_confirmed = False if map_changed else bool(data["material_mismatch_confirmed"])

            if apply_gcode_state:
                for key, attr in (
                    ("slicing_run_counter", "_slicing_run_counter"),
                    ("cleared_run_seq", "_cleared_run_seq"),
                    ("cleared_at_ns", "_cleared_at_ns"),
                    ("cleared_before_mtime_ns", "cleared_before_mtime_ns"),
                ):
                    if key in data:
                        try:
                            cur = int(getattr(self, attr, 0) or 0)
                            setattr(self, attr, max(cur, max(0, int(data[key] or 0))))
                        except (ValueError, TypeError):
                            pass

                incoming_gcode_path = str(data.get("last_gcode_path") or "").strip() if "last_gcode_path" in data else ""
                if incoming_gcode_path:
                    prov_hint = data.get("last_staged_provenance") if isinstance(data.get("last_staged_provenance"), dict) else None
                    ok_restore, restore_reason = _validate_restorable_gcode_state(
                        self,
                        incoming_gcode_path,
                        provenance=prov_hint,
                        source_hint=str(data.get("last_gcode_source") or ""),
                    )
                    if ok_restore:
                        self.last_gcode_path = incoming_gcode_path
                        if "last_gcode_source" in data and isinstance(data["last_gcode_source"], str):
                            src_clean = data["last_gcode_source"].strip()
                            if src_clean:
                                self.last_gcode_source = src_clean
                        if "last_preview_temp_path" in data:
                            self._last_preview_temp_path = str(data.get("last_preview_temp_path") or "").strip()
                        if "last_preview_temp_existed" in data:
                            self._last_preview_temp_existed = bool(data.get("last_preview_temp_existed")) and bool(
                                self._last_preview_temp_path
                            )
                    else:
                        self.last_gcode_path = ""
                        self.last_gcode_source = ""
                        self.last_job_metadata = {}
                        self.last_postprocess_info = {}
                        self._last_staged_provenance = {}
                        self._last_preview_temp_path = ""
                        self._last_preview_temp_existed = False
                        self.pending_preprint_open = False
                    _record_diagnostic_load_event(
                        self,
                        trigger="apply_dict",
                        decision="accepted" if ok_restore else "rejected",
                        reason=restore_reason,
                        file_source="config_restore",
                        file_path=incoming_gcode_path,
                        sidecar=(self._last_staged_provenance or None) if ok_restore else prov_hint,
                        compute_sha=False,
                    )
                elif "last_gcode_path" in data:
                    self.last_gcode_path = ""
                    self.last_gcode_source = ""
                    self._last_preview_temp_path = ""
                    self._last_preview_temp_existed = False

                if not incoming_gcode_path or ok_restore:
                    if isinstance(data.get("last_job_metadata"), dict):
                        self.last_job_metadata = dict(data["last_job_metadata"])
                    if isinstance(data.get("last_postprocess_info"), dict):
                        self.last_postprocess_info = dict(data["last_postprocess_info"])
                    if isinstance(data.get("last_staged_provenance"), dict):
                        prov_in = dict(data["last_staged_provenance"])
                        if incoming_gcode_path or (
                            "last_gcode_path" not in data
                            or (not prov_in.get("is_cleared") and not prov_in.get("is_stale") and not prov_in.get("is_superseded"))
                        ):
                            self._last_staged_provenance = prov_in
                        else:
                            self._last_staged_provenance = {}

            if "slicer_filaments" in data and isinstance(data["slicer_filaments"], list):
                valid_fils = [dict(f) for f in data["slicer_filaments"] if isinstance(f, dict)]
                if valid_fils:
                    self.slicer_filaments = valid_fils
            if "filament_signature" in data and isinstance(data["filament_signature"], str) and data["filament_signature"]:
                self.filament_signature = data["filament_signature"]
            elif "slicer_filaments" in data:
                self.filament_signature = compute_filament_signature(self.slicer_filaments)
            if "mapping_warnings" in data and isinstance(data["mapping_warnings"], list):
                self.mapping_warnings = [str(w) for w in data["mapping_warnings"]]
            if "slicing_modal_mode" in data:
                mode = str(data.get("slicing_modal_mode") or "").strip().lower()
                if mode in ("async_tab", "window_modal"):
                    self.slicing_modal_mode = mode

            prof_name = str(data.get("printer_profile_name") or "").strip() if "printer_profile_name" in data else None
            self._sync_active_profile_from_state_unlocked(name=prof_name)

            if save:
                self.save_config()

    def resolve_effective_ace_count(self, ace_status: Optional[Dict[str, Any]] = None) -> int:
        """
        Resolve active ACE unit count (1..MAX_ACE_UNITS):
        1. When connected live to Klipper, use reported `ace_instances` / slot count.
        2. When disconnected/offline, honor explicit `configured_ace_count` (`1`..`4`).
        3. In `auto` offline mode, use `last_detected_ace_count`, else infer from loaded slicer
           filaments (>= 1), defaulting to `1` ACE (4 slots: T0..T3).
        """
        with self._lock:
            st = ace_status if isinstance(ace_status, dict) else (self.last_ace_status or {})
            slots = st.get("slots") or []
            if bool(st.get("connected")) and (slots or st.get("ace_instances") or st.get("ace_count")):
                by_slots = (len(slots) + SLOTS_PER_ACE_UNIT - 1) // SLOTS_PER_ACE_UNIT if slots else 0
                try:
                    by_inst = int(st.get("ace_instances") or st.get("ace_count") or 0)
                except (ValueError, TypeError):
                    by_inst = 0
                live_cnt = max(by_slots, by_inst, 1)
                return max(1, min(MAX_ACE_UNITS, live_cnt))

            cfg = str(getattr(self, "configured_ace_count", DEFAULT_ACE_COUNT) or DEFAULT_ACE_COUNT).strip().lower()
            if cfg in ("1", "2", "3", "4"):
                return int(cfg)

            if slots and st.get("connected") is not False:
                by_slots = (len(slots) + SLOTS_PER_ACE_UNIT - 1) // SLOTS_PER_ACE_UNIT
                return max(1, min(MAX_ACE_UNITS, by_slots))

            last_det = int(getattr(self, "last_detected_ace_count", 0) or 0)
            if 1 <= last_det <= MAX_ACE_UNITS:
                return last_det

            max_used_tool = 0
            for f in self.slicer_filaments or []:
                if isinstance(f, dict) and f.get("used", True):
                    try:
                        max_used_tool = max(max_used_tool, int(f.get("slicer_tool", 0)))
                    except (ValueError, TypeError):
                        pass
            if max_used_tool >= SLOTS_PER_ACE_UNIT:
                return max(1, min(MAX_ACE_UNITS, (max_used_tool + SLOTS_PER_ACE_UNIT) // SLOTS_PER_ACE_UNIT))
            return 1

    def resolve_known_effective_slot_count(self, ace_status: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """
        Return the known upper slot count (`ace_count * 4`) when hardware is connected live,
        `configured_ace_count` is explicitly set (`1`..`4`), or `last_detected_ace_count` is known.
        Returns `None` in offline `auto` mode before any ACE unit count has been observed.
        """
        with self._lock:
            st = ace_status if isinstance(ace_status, dict) else (self.last_ace_status or {})
            slots = st.get("slots") or []
            if bool(st.get("connected")) and (slots or st.get("ace_instances") or st.get("ace_count")):
                return self.resolve_effective_ace_count(st) * SLOTS_PER_ACE_UNIT
            cfg = str(getattr(self, "configured_ace_count", DEFAULT_ACE_COUNT) or DEFAULT_ACE_COUNT).strip().lower()
            if cfg in ("1", "2", "3", "4"):
                return int(cfg) * SLOTS_PER_ACE_UNIT
            if slots and st.get("connected") is not False:
                by_slots = (len(slots) + SLOTS_PER_ACE_UNIT - 1) // SLOTS_PER_ACE_UNIT
                return max(1, min(MAX_ACE_UNITS, by_slots)) * SLOTS_PER_ACE_UNIT
            last_det = int(getattr(self, "last_detected_ace_count", 0) or 0)
            if 1 <= last_det <= MAX_ACE_UNITS:
                return last_det * SLOTS_PER_ACE_UNIT
            return None

    def _commit_ace_status(
        self,
        ace_status: Dict[str, Any],
        expected_epoch: int,
        *,
        detected_ace_count: Optional[int] = None,
    ) -> bool:
        """
        Atomically commit live ACE status (and optional `last_detected_ace_count`) only if
        `profile_epoch` still matches `expected_epoch`. Returns False if a profile switch superseded it.
        """
        need_save = False
        with self._lock:
            if self.profile_epoch != expected_epoch:
                return False
            self.last_ace_status = dict(ace_status)
            if detected_ace_count is not None:
                det_cnt = max(1, min(MAX_ACE_UNITS, int(detected_ace_count)))
                if self.last_detected_ace_count != det_cnt:
                    self.last_detected_ace_count = det_cnt
                    self._sync_active_profile_from_state_unlocked()
                    need_save = True
        if need_save:
            self.save_config()
        return True

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            self._sync_active_profile_from_state_unlocked()
            gcode_path = self.last_gcode_path
            job_meta = dict(self.last_job_metadata)
            ace_slots_snap = [
                dict(s)
                for s in ((self.last_ace_status or {}).get("slots") or [])
                if isinstance(s, dict)
            ]
            reserve_g_snap = float(getattr(self, "prediction_reserve_g", DEFAULT_PREDICTION_RESERVE_G) or 0.0)
            startup_purge_snap = float(getattr(self, "startup_purge_length_mm", DEFAULT_STARTUP_PURGE_LENGTH_MM))
            if not math.isfinite(startup_purge_snap) or startup_purge_snap < 0.0:
                startup_purge_snap = DEFAULT_STARTUP_PURGE_LENGTH_MM
            skip_purge_snap = bool(self.skip_purge_for_already_loaded_tool)
            ace_status_snap = dict(self.last_ace_status) if isinstance(self.last_ace_status, dict) else None
            payload = {
                "language": self.language,
                "theme_mode": self.theme_mode,
                "color_scheme": self.color_scheme,
                "moonraker_url": self.moonraker_url,
                "api_key": self.api_key,
                "spoolman_url": self.spoolman_url,
                "spoolman_status": dict(self.last_spoolman_status),
                "spoolman_spools": _build_external_spoolman_choices(self._spoolman_spools),
                "default_gcode_dir": self.default_gcode_dir,
                "manual_spoolman_selections": {
                    str(k): dict(v) for k, v in sorted(self.manual_spoolman_selections.items())
                },
                "resolved_paths": self.get_resolved_paths_summary(),
                "enable_port80_fallback": self.enable_port80_fallback,
                "auto_register_pipeline": bool(self.auto_register_pipeline),
                "bed_leveling": self.bed_leveling,
                "skip_purge_for_already_loaded_tool": self.skip_purge_for_already_loaded_tool,
                "cut_tip_after_print": self.cut_tip_after_print,
                "force_initial_purge": self.force_initial_purge,
                "external_spool_mode": bool(getattr(self, "external_spool_mode", False)),
                "external_spool_id": getattr(self, "external_spool_id", None),
                "start_print_immediately": self.start_print_immediately,
                "diag_load_logging": bool(getattr(self, "diag_load_logging", False)),
                "dryer_unit": self.dryer_unit,
                "dryer_temp": self.dryer_temp,
                "dryer_duration": self.dryer_duration,
                "configured_ace_count": str(getattr(self, "configured_ace_count", DEFAULT_ACE_COUNT) or DEFAULT_ACE_COUNT),
                "last_detected_ace_count": int(getattr(self, "last_detected_ace_count", 0) or 0),
                "effective_ace_count": self.resolve_effective_ace_count(),
                "active_printer_id": str(getattr(self, "active_printer_id", DEFAULT_PRINTER_ID) or DEFAULT_PRINTER_ID),
                "printer_profiles": [dict(p) for p in self.printer_profiles if isinstance(p, dict)],
                "filament_diameter": self.filament_diameter,
                "prediction_reserve_g": reserve_g_snap,
                "startup_purge_length_mm": startup_purge_snap,
                "tool_map": {str(k): v for k, v in sorted(self.tool_map.items())},
                "tool_map_sources": {str(k): v for k, v in sorted(self.tool_map_sources.items())},
                "material_mismatch_confirmed": self.material_mismatch_confirmed,
                "last_job_metadata": job_meta,
                "last_gcode_path": gcode_path,
                "last_gcode_source": self.last_gcode_source,
                "last_preview_temp_path": str(getattr(self, "_last_preview_temp_path", "") or ""),
                "last_preview_temp_existed": bool(getattr(self, "_last_preview_temp_existed", False)),
                "last_invalidation_reason": str(getattr(self, "_last_invalidation_reason", "") or ""),
                "auto_slice_in_progress": bool(getattr(self, "_auto_slice_in_progress", False)),
                "cleared_before_mtime_ns": int(getattr(self, "cleared_before_mtime_ns", 0) or 0),
                "cleared_at_ns": int(getattr(self, "_cleared_at_ns", 0) or 0),
                "cleared_run_seq": int(getattr(self, "_cleared_run_seq", 0) or 0),
                "slicing_run_counter": int(getattr(self, "_slicing_run_counter", 0) or 0),
                "pending_preprint_open": bool(self.pending_preprint_open),
                "last_postprocess_info": dict(self.last_postprocess_info),
                "slicer_filaments": list(self.slicer_filaments),
                "filament_signature": self.filament_signature,
                "mapping_warnings": list(self.mapping_warnings),
                "last_staged_provenance": dict(self._last_staged_provenance),
                "last_slice_completed_ns": int(self._last_slice_completed_ns or 0),
                "slicing_modal_mode": str(self.slicing_modal_mode or "window_modal"),
                "pipeline_retry_count": int(getattr(self, "_pipeline_retry_count", 0) or 0),
                "pipeline_last_error": str(getattr(self, "_pipeline_last_error", "") or ""),
            }
        payload["stale_model_info"] = check_stale_gcode_model(
            gcode_path=gcode_path,
            filename=str(job_meta.get("output_name") or job_meta.get("filename") or ""),
            state=self,
            job_meta=job_meta,
            slicer_filaments=payload["slicer_filaments"],
        )
        if (
            payload["last_postprocess_info"]
            and "toolchanges" in payload["last_postprocess_info"]
            and isinstance(ace_status_snap, dict)
            and ace_status_snap.get("connected")
        ):
            eff_tc_snap = compute_effective_toolchanges(
                payload["last_postprocess_info"],
                ace_status=ace_status_snap,
                skip_purge_for_already_loaded_tool=skip_purge_snap,
                force_initial_purge=payload.get("force_initial_purge"),
                tool_map=payload["tool_map"],
            )
            payload["last_postprocess_info"].setdefault(
                "raw_toolchanges", int(payload["last_postprocess_info"].get("toolchanges") or 0)
            )
            payload["last_postprocess_info"]["effective_toolchanges"] = eff_tc_snap
            payload["last_postprocess_info"]["toolchanges"] = eff_tc_snap
        payload["filament_prediction"] = compute_preprint_filament_prediction(
            payload["slicer_filaments"],
            payload["tool_map"],
            ace_slots_snap,
            payload["last_postprocess_info"],
            reserve_g=reserve_g_snap,
            startup_purge_length_mm=startup_purge_snap,
            skip_purge_for_already_loaded_tool=skip_purge_snap,
            ace_status=ace_status_snap,
            has_gcode=bool(gcode_path),
        )
        return payload

    def get_client(self) -> MoonrakerClient:
        with self._lock:
            return MoonrakerClient(
                base_url=self.moonraker_url,
                api_key=self.api_key,
                enable_port80_fallback=self.enable_port80_fallback,
            )

    def get_spoolman_client(self) -> SpoolmanClient:
        with self._lock:
            return SpoolmanClient(
                base_url=self.spoolman_url,
                timeout=DEFAULT_SPOOLMAN_TIMEOUT,
            )


GLOBAL_STATE = PluginState()


# 9. Print Safety Gate & Embedded Thumbnail Color Recoloring
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
    r, g, b = hex_to_rgb(rgb_to_hex(hex_color or "#334155"))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#111827" if luminance > 150.0 else "#FFFFFF"


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


_HEX_HSL_CACHE: Dict[str, Tuple[float, float, float]] = {}


def _hex_to_hsl_cached(norm_hex: str) -> Tuple[float, float, float]:
    cached = _HEX_HSL_CACHE.get(norm_hex)
    if cached is not None:
        return cached
    r_c, g_c, b_c = hex_to_rgb(norm_hex)
    hsl_val = rgb_to_hsl(r_c, g_c, b_c)
    if len(_HEX_HSL_CACHE) >= 256:
        _HEX_HSL_CACHE.clear()
    _HEX_HSL_CACHE[norm_hex] = hsl_val
    return hsl_val


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
    _, s_s, s_l = _hex_to_hsl_cached(src_norm)
    t_h, t_s, t_l = _hex_to_hsl_cached(tgt_norm)

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


# 10. 3MF Container Extraction, G-Code Staging & Slicer Acquisition
_GCODE_EXTENSIONS = (".gcode", ".gco", ".gc")
_CONTAINER_3MF_SUFFIXES = (".gcode.3mf", ".3mf")
_ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x01\x02")
_VALID_GCODE_CMD_RE = re.compile(
    r"^\s*(?:[GMT]\d+(?:\b|\s|$)|CUT_TIP\b|ACE_[A-Z0-9_]+\b|"
    r"PRINT_(?:START|END)\b|START_PRINT\b|END_PRINT\b|BED_MESH_CALIBRATE\b|"
    r"SET_[A-Z0-9_]+\b|ACTIVATE_EXTRUDER\b|EXCLUDE_OBJECT[A-Z0-9_]*\b)",
    re.IGNORECASE,
)
_BINARY_MAGIC_PREFIXES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"GCDE",
    b"\x7fELF",
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"%PDF-",
    b"\x1f\x8b",
)


def _is_zip_magic_bytes(raw: bytes) -> bool:
    return isinstance(raw, (bytes, bytearray)) and len(raw) >= 4 and bytes(raw[:4]) in _ZIP_MAGIC_PREFIXES


def _is_gcode_filename(path: str) -> bool:
    return str(path or "").strip().lower().endswith(_GCODE_EXTENSIONS)


def _is_container_3mf_filename(path: str) -> bool:
    return str(path or "").strip().lower().endswith(_CONTAINER_3MF_SUFFIXES)


def _is_gcode_or_container_filename(path: str) -> bool:
    return _is_gcode_filename(path) or _is_container_3mf_filename(path)


def is_zip_container_file(path: str) -> bool:
    """Return True if `path` is an existing readable ZIP container (e.g. `.gcode.3mf`)."""
    raw = str(path or "").strip()
    if not raw or not os.path.isfile(raw):
        return False
    try:
        with open(raw, "rb") as f:
            head = f.read(4)
        return _is_zip_magic_bytes(head) and zipfile.is_zipfile(raw)
    except (OSError, ValueError):
        return False


def is_probably_binary_bytes(raw: bytes) -> bool:
    """Detect binary/container payloads so non-G-code files are never processed as plain G-code."""
    if not raw:
        return False
    sample = raw[:16384]
    if b"\x00" in sample:
        return True
    for magic in _BINARY_MAGIC_PREFIXES:
        if sample.startswith(magic):
            return True
    ctrl_count = 0
    for byte_val in sample[:4096]:
        if byte_val < 32 and byte_val not in (9, 10, 13, 27):
            ctrl_count += 1
    if len(sample[:4096]) > 0 and (ctrl_count / min(len(sample), 4096)) > 0.02:
        return True
    return False


def validate_gcode_lines(lines: List[str], source_name: str = "") -> None:
    """
    Validate that decoded lines represent real text G-code rather than binary garbage,
    raw XML/HTML, or an empty payload. Raises `ValueError` on invalid input.
    """
    label = os.path.basename(source_name) if source_name else "G-code"
    if not lines:
        raise ValueError(f"Selected file '{label}' is empty or contains no G-code lines.")
    has_non_ws = False
    has_gcode_cmd = False
    total_chars = 0
    replacement_chars = 0
    for idx, line in enumerate(lines):
        if "\x00" in line:
            raise ValueError(f"Rejected '{label}': NUL byte detected (binary data is not valid G-code).")
        if idx == 0 and (line.startswith("PK\x03\x04") or line.startswith("GCDE")):
            raise ValueError(f"Rejected '{label}': binary/container header cannot be read as plain G-code.")
        stripped = line.strip()
        if stripped:
            has_non_ws = True
            total_chars += len(stripped)
            replacement_chars += stripped.count("\ufffd")
            if not has_gcode_cmd and not stripped.startswith(";"):
                if _VALID_GCODE_CMD_RE.match(stripped):
                    has_gcode_cmd = True
    if not has_non_ws:
        raise ValueError(f"Selected file '{label}' is empty.")
    if total_chars > 32 and (replacement_chars / max(1, total_chars)) > 0.05:
        raise ValueError(f"Rejected '{label}': binary/corrupt encoding is not valid G-code.")
    if not has_gcode_cmd:
        raise ValueError(f"Rejected '{label}': no valid G-code commands found.")


def _normalize_plate_index(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        idx = int(str(value).strip())
        return idx if idx >= 1 else None
    except (ValueError, TypeError):
        return None


def _extract_plate_index_from_name(name: str) -> Optional[int]:
    m = re.search(r"(?:plate[_\s-]*|[_-]p)(\d+)\b", str(name or ""), re.IGNORECASE)
    if not m:
        return None
    try:
        val = int(m.group(1))
        return val if val >= 1 else None
    except (ValueError, TypeError):
        return None


def _parse_slice_info_config(xml_bytes: bytes, preferred_plate: Optional[int] = None) -> Dict[str, Any]:
    """
    Parse OrcaSlicer/Bambu `Metadata/slice_info.config` XML from a `.gcode.3mf` ZIP container
    for plate index, print time prediction, total weight, and per-extruder weights/metadata.
    """
    if not xml_bytes or not xml_bytes.strip():
        return {}
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return {}

    plates: List[Dict[str, Any]] = []
    for plate_el in root.findall(".//plate"):
        meta_map: Dict[str, str] = {}
        for meta_el in plate_el.findall("metadata"):
            k = str(meta_el.attrib.get("key") or "").strip()
            v = str(meta_el.attrib.get("value") or "").strip()
            if k:
                meta_map[k] = v
        idx_val = _normalize_plate_index(meta_map.get("index"))
        pred_s = 0.0
        total_w = 0.0
        try:
            pred_s = float(meta_map.get("prediction") or 0.0)
        except (ValueError, TypeError):
            pred_s = 0.0
        try:
            total_w = float(meta_map.get("weight") or 0.0)
        except (ValueError, TypeError):
            total_w = 0.0

        weights_by_tool: Dict[int, float] = {}
        fils: List[Dict[str, Any]] = []
        for fil_el in plate_el.findall("filament"):
            fid = str(fil_el.attrib.get("id") or "").strip()
            try:
                tool_idx = int(fid) - 1
            except (ValueError, TypeError):
                continue
            if tool_idx < 0:
                continue
            used_g = 0.0
            used_m = 0.0
            try:
                used_g = float(fil_el.attrib.get("used_g") or 0.0)
            except (ValueError, TypeError):
                used_g = 0.0
            try:
                used_m = float(fil_el.attrib.get("used_m") or 0.0)
            except (ValueError, TypeError):
                used_m = 0.0
            weights_by_tool[tool_idx] = used_g
            raw_col = str(fil_el.attrib.get("color") or "").strip()
            fil_hex = rgb_to_hex(raw_col) if raw_col else "#CCCCCC"
            fils.append(
                {
                    "slicer_tool": tool_idx,
                    "material": str(fil_el.attrib.get("type") or "PLA").strip() or "PLA",
                    "color": fil_hex,
                    "rgb": list(hex_to_rgb(fil_hex)),
                    "used_g": used_g,
                    "used_m": used_m,
                    "used": used_g > 0.0 or used_m > 0.0,
                }
            )
        plates.append(
            {
                "plate_index": idx_val,
                "prediction_s": pred_s,
                "total_weight_g": total_w,
                "weights_by_tool": weights_by_tool,
                "filaments": fils,
            }
        )

    if not plates:
        return {}
    chosen = plates[0]
    if preferred_plate is not None:
        for p in plates:
            if p.get("plate_index") == preferred_plate:
                chosen = p
                break
    return {
        "plate_index": chosen.get("plate_index"),
        "prediction_s": chosen.get("prediction_s", 0.0),
        "total_weight_g": chosen.get("total_weight_g", 0.0),
        "weights_by_tool": dict(chosen.get("weights_by_tool") or {}),
        "filaments": list(chosen.get("filaments") or []),
        "plate_count": len(plates),
    }


def _merge_slice_info_into_parsed(
    filaments: List[Dict[str, Any]],
    job_meta: Dict[str, Any],
    slice_info: Optional[Dict[str, Any]],
) -> None:
    """Enrich parsed G-code filaments and job metadata using `Metadata/slice_info.config` when present."""
    if not slice_info or not isinstance(slice_info, dict):
        return
    if slice_info.get("plate_index") is not None and not job_meta.get("plate_index"):
        job_meta["plate_index"] = int(slice_info["plate_index"])
    weights_by_tool = slice_info.get("weights_by_tool") or {}
    if weights_by_tool and isinstance(weights_by_tool, dict):
        total_w = sum(float(v or 0.0) for v in weights_by_tool.values())
        if float(job_meta.get("total_filament_g", 0.0) or 0.0) <= 0.0 and total_w > 0.0:
            job_meta["total_filament_g"] = round(total_w, 2)
    if float(job_meta.get("total_filament_g", 0.0) or 0.0) <= 0.0:
        si_total = float(slice_info.get("total_weight_g", 0.0) or 0.0)
        if si_total > 0.0:
            job_meta["total_filament_g"] = round(si_total, 2)
    pred_s = float(slice_info.get("prediction_s", 0.0) or 0.0)
    cur_est = str(job_meta.get("estimated_time") or "").strip()
    if (not cur_est or cur_est in ("—", "-")) and pred_s > 0.0:
        secs = int(round(pred_s))
        hrs, rem = divmod(secs, 3600)
        mins, sec = divmod(rem, 60)
        if hrs > 0:
            job_meta["estimated_time"] = f"{hrs}h {mins}m {sec}s"
        elif mins > 0:
            job_meta["estimated_time"] = f"{mins}m {sec}s"
        else:
            job_meta["estimated_time"] = f"{sec}s"

    si_fil_by_tool = {
        int(f["slicer_tool"]): f
        for f in (slice_info.get("filaments") or [])
        if isinstance(f, dict) and "slicer_tool" in f
    }
    if not filaments and si_fil_by_tool:
        for t_idx in sorted(si_fil_by_tool):
            sf = si_fil_by_tool[t_idx]
            filaments.append(
                {
                    "slicer_tool": t_idx,
                    "label": f"Extruder {t_idx} (T{t_idx})",
                    "color": sf.get("color", "#CCCCCC"),
                    "rgb": list(sf.get("rgb") or [204, 204, 204]),
                    "material": sf.get("material", "PLA"),
                    "used_g": float(sf.get("used_g", 0.0) or 0.0),
                    "used_m": float(sf.get("used_m", 0.0) or 0.0),
                    "used": bool(sf.get("used", True)),
                }
            )
    else:
        for fil in filaments:
            t_idx = int(fil.get("slicer_tool", 0))
            sf = si_fil_by_tool.get(t_idx)
            if sf:
                if float(fil.get("used_g", 0.0) or 0.0) <= 0.0 and float(sf.get("used_g", 0.0) or 0.0) > 0.0:
                    fil["used_g"] = float(sf["used_g"])
                    fil["used"] = True
                if float(fil.get("used_m", 0.0) or 0.0) <= 0.0 and float(sf.get("used_m", 0.0) or 0.0) > 0.0:
                    fil["used_m"] = float(sf["used_m"])


def extract_gcode_from_3mf(
    source: Any,
    plate_index: Optional[int] = None,
    filename: str = "",
) -> Dict[str, Any]:
    """
    Extract genuine text G-code (`Metadata/plate_<n>.gcode`) and XML slice metadata
    (`Metadata/slice_info.config`) from an OrcaSlicer `.gcode.3mf` ZIP container.
    Validates that the extracted payload is genuine text G-code.
    """
    req_plate = _normalize_plate_index(plate_index)
    file_arg: Any = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    display_label = filename or (os.path.basename(str(source)) if isinstance(source, str) else "container.gcode.3mf")
    try:
        zf_ctx = zipfile.ZipFile(file_arg, "r")
    except Exception as exc:
        raise ValueError(f"Invalid .gcode.3mf ZIP archive '{display_label}': {exc}") from exc

    with zf_ctx as zf:
        names = zf.namelist()
        plate_entries: Dict[int, str] = {}
        plate_re = re.compile(r"^metadata/plate_(\d+)\.gcode$", re.IGNORECASE)
        for name in names:
            m = plate_re.match(name.replace("\\", "/").lstrip("./"))
            if m:
                plate_entries[int(m.group(1))] = name
        if not plate_entries:
            raise ValueError(
                f"No Metadata/plate_<n>.gcode found inside '{display_label}'."
            )

        if req_plate is not None:
            if req_plate not in plate_entries:
                raise ValueError(
                    f"Requested Metadata/plate_{req_plate}.gcode missing inside '{display_label}' "
                    f"(available plates: {sorted(plate_entries.keys())})."
                )
            chosen_plate = req_plate
        elif 1 in plate_entries:
            chosen_plate = 1
        else:
            chosen_plate = sorted(plate_entries.keys())[0]

        entry_name = plate_entries[chosen_plate]
        raw_gcode_bytes = zf.read(entry_name)
        if is_probably_binary_bytes(raw_gcode_bytes):
            raise ValueError(
                f"Extracted '{entry_name}' from '{display_label}' is binary, not valid G-code."
            )
        gcode_text = raw_gcode_bytes.decode("utf-8", errors="replace")
        lines = gcode_text.splitlines(keepends=True)
        validate_gcode_lines(lines, source_name=f"{display_label}:{entry_name}")

        slice_info: Dict[str, Any] = {}
        for name in names:
            if name.replace("\\", "/").lstrip("./").lower() == "metadata/slice_info.config":
                try:
                    slice_info = _parse_slice_info_config(zf.read(name), preferred_plate=chosen_plate)
                except Exception:
                    slice_info = {}
                break

        return {
            "gcode_text": gcode_text,
            "lines": lines,
            "plate_index": chosen_plate,
            "zip_entry": entry_name,
            "slice_info": slice_info,
            "available_plates": sorted(plate_entries.keys()),
        }


def _write_gcode_back_to_3mf(
    archive_path: str,
    plate_entry_name: str,
    new_gcode_bytes: bytes,
) -> None:
    """
    Atomically update `plate_entry_name` inside an existing `.gcode.3mf` ZIP container
    without using `tempfile.*`, leaving zero temporary files behind on error.
    """
    target_abs = os.path.abspath(archive_path)
    norm_target = plate_entry_name.replace("\\", "/").lstrip("./").lower()

    def _rewrite_zip(dst_f: Any) -> None:
        with zipfile.ZipFile(target_abs, "r") as z_in, zipfile.ZipFile(dst_f, "w") as z_out:
            for item in z_in.infolist():
                norm_item = item.filename.replace("\\", "/").lstrip("./").lower()
                data = new_gcode_bytes if norm_item == norm_target else z_in.read(item.filename)
                z_out.writestr(item, data, compress_type=item.compress_type)

    _atomic_write_with(target_abs, _rewrite_zip, suffix=".gcode.3mf", mode="wb")


def _sanitize_staged_gcode_filename(
    filename: str,
    default_name: str = "sliced_plate.gcode",
    plate_index: Optional[int] = None,
    disambiguate_plate: bool = False,
) -> str:
    """
    Normalize a job/source filename into a clean `.gcode` (or `.gco`/`.gc`) filename inside
    `staged_gcode/`. Never produces double suffixes like `.gcode.3mf.gcode` or `.3mf.gcode`.
    """
    raw_base = os.path.basename(str(filename or default_name).strip()) or default_name
    low_raw = raw_base.lower()
    chosen_ext = ".gcode"
    if not low_raw.endswith((".gcode.3mf", ".3mf")):
        for ext in (".gco", ".gc"):
            if low_raw.endswith(ext) and not low_raw.endswith(".gcode" + ext):
                chosen_ext = ext
                break
    stem_raw = re.sub(r"(\.(?:gcode|gco|gc|3mf))+$", "", raw_base, flags=re.IGNORECASE).strip()
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem_raw).strip("._")
    if not safe_stem:
        def_stem = re.sub(r"(\.(?:gcode|gco|gc|3mf))+$", "", os.path.basename(default_name), flags=re.IGNORECASE)
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", def_stem).strip("._") or "sliced_plate"
    p_idx = _normalize_plate_index(plate_index)
    if p_idx is not None and (p_idx > 1 or disambiguate_plate):
        if not re.search(rf"(?:plate[_\s-]*|[_-]p){p_idx}\b", safe_stem, re.IGNORECASE):
            safe_stem = f"{safe_stem}_plate{p_idx}"
    return f"{safe_stem}{chosen_ext}"


def _mark_superseded_staged_sidecars(
    staged_dir: str,
    active_staged_path: str,
    current_run_id: str,
    plate_index: Optional[int],
    project_model: str,
) -> None:
    """Mark older conflicting staged sidecars as stale/superseded while preserving distinct project plates."""
    if not os.path.isdir(staged_dir):
        return
    try:
        active_real = os.path.realpath(active_staged_path)
    except OSError:
        active_real = active_staged_path
    cur_proj_norm = re.sub(r"[^a-z0-9]+", "", str(project_model or "").lower())
    try:
        with os.scandir(staged_dir) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.name.startswith(".ace_tmp_") or not entry.name.lower().endswith(".meta.json"):
                        continue
                    gcode_peer = entry.path[: -len(".meta.json")]
                    try:
                        peer_real = os.path.realpath(gcode_peer)
                    except OSError:
                        peer_real = gcode_peer
                    if peer_real == active_real:
                        continue
                    old_meta = _read_provenance_sidecar(gcode_peer)
                    if not old_meta:
                        continue
                    old_proj = re.sub(r"[^a-z0-9]+", "", str(old_meta.get("project_model") or "").lower())
                    old_plate = _normalize_plate_index(old_meta.get("plate_index"))
                    same_project = bool(cur_proj_norm and old_proj and cur_proj_norm == old_proj)
                    different_plate = (
                        plate_index is not None
                        and old_plate is not None
                        and old_plate != plate_index
                    )
                    if same_project and different_plate:
                        continue
                    if not old_meta.get("is_stale") or not old_meta.get("is_superseded"):
                        old_meta["is_stale"] = True
                        old_meta["is_superseded"] = True
                        old_meta["superseded_by_run"] = current_run_id
                        _write_provenance_sidecar_atomic(gcode_peer, old_meta)
                except Exception:
                    continue
    except OSError:
        pass


def _clear_active_job_fields(state: Optional[PluginState] = None) -> str:
    """Reset all in-memory active G-code job fields without erasing clear tombstones."""
    st = state or GLOBAL_STATE
    with st._lock:
        prev_path = str(st.last_gcode_path or "").strip()
        st.last_gcode_path = ""
        st.last_gcode_source = ""
        st.last_job_metadata = {}
        st.last_postprocess_info = {}
        st.mapping_warnings = []
        st.material_mismatch_confirmed = False
        st.pending_preprint_open = False
        st._last_staged_provenance = {}
        st._last_preview_temp_path = ""
        st._last_preview_temp_existed = False
        st._reslice_preview_temp_path = ""
        st._reslice_preview_prev_mtime_ns = 0
        st._slice_host_fingerprint = {}
        st._last_slicing_run_id = ""
        st._pending_preview_gcode = {}
        st._last_slice_satisfied_run_id = ""
        st._last_slice_satisfied_created_ns = 0
    return prev_path


def _mark_cleared_staged_sidecars(
    state: PluginState,
    cleared_ns: int,
    cleared_seq: int,
    reason: str = "user_cleared",
) -> Tuple[int, int]:
    """Atomically mark every staged file & sidecar in `staged_gcode/` as cleared (`is_cleared=True`)."""
    storage_dir = state.resolve_storage_dir()
    staged_dir = os.path.join(storage_dir, "staged_gcode") if storage_dir else ""
    newest_ns = 0
    max_seq = int(cleared_seq or 0)
    if not staged_dir or not os.path.isdir(staged_dir):
        return newest_ns, max_seq

    try:
        with os.scandir(staged_dir) as it:
            entries = list(it)
    except OSError:
        return newest_ns, max_seq

    gcode_peers: Set[str] = set()
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False) or entry.name.startswith(".ace_tmp_"):
                continue
            if entry.name.lower().endswith(".meta.json"):
                gcode_peers.add(entry.path[: -len(".meta.json")])
            elif _is_gcode_filename(entry.name):
                gcode_peers.add(entry.path)
        except OSError:
            continue

    for gcode_peer in sorted(gcode_peers):
        try:
            g_ns = _get_file_mtime_ns(gcode_peer) if os.path.isfile(gcode_peer) else 0
            if g_ns:
                newest_ns = max(newest_ns, g_ns)
            base_name = os.path.basename(gcode_peer)
            meta = _read_provenance_sidecar(gcode_peer) or {
                "staged_path": gcode_peer,
                "filename": base_name,
                "output_name": base_name,
                "source_mtime_ns": g_ns,
            }
            max_seq = max(max_seq, int(meta.get("slicing_run_seq", 0) or 0))
            if not (meta.get("is_cleared") and int(meta.get("cleared_ns", 0) or 0) == int(cleared_ns)):
                meta["is_cleared"] = True
                meta["is_stale"] = True
                meta["cleared_ns"] = int(cleared_ns)
                meta["cleared_seq"] = int(max_seq)
                meta["cleared_reason"] = str(reason or "user_cleared")
                _write_provenance_sidecar_atomic(gcode_peer, meta)
        except Exception:
            continue

    return newest_ns, max_seq


def clear_loaded_gcode_state(
    state: Optional[PluginState] = None,
    reason: str = "clear_gcode",
    sync_host: bool = True,
) -> Dict[str, Any]:
    """
    Completely clear active G-code references and `_last_staged_provenance`, atomically
    tombstone all existing staged sidecars (`is_cleared=True`, `cleared_ns`), and persist state.
    """
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    with st._lock:
        cur_path = str(st.last_gcode_path or "").strip()
        cur_src_clear = str(st.last_gcode_source or "").strip()
        saved_prev_temp = str(getattr(st, "_last_preview_temp_path", "") or "").strip() if cur_src_clear == "file_picker" else ""
        saved_prev_existed = bool(getattr(st, "_last_preview_temp_existed", False)) if cur_src_clear == "file_picker" else False
        pending_cap_ns = int((getattr(st, "_pending_preview_gcode", None) or {}).get("captured_ns", 0) or 0)
        cleared_ns = max(
            time.time_ns(),
            int(getattr(st, "_cleared_at_ns", 0) or 0) + 1,
            pending_cap_ns + 1,
        )
        base_seq = max(
            int(getattr(st, "_slicing_run_counter", 0) or 0),
            int(getattr(st, "_cleared_run_seq", 0) or 0),
        )
    newest_ns, max_seq = _mark_cleared_staged_sidecars(st, cleared_ns, base_seq, reason=reason)
    if cur_path and not (HAS_ORCA_RUNTIME and _is_system_temp_path(cur_path, storage_dir)):
        try:
            if os.path.isfile(cur_path):
                st_cur = os.stat(cur_path)
                cur_ns = int(getattr(st_cur, "st_mtime_ns", int(float(st_cur.st_mtime) * 1_000_000_000)))
                newest_ns = max(newest_ns, cur_ns)
        except OSError:
            pass
    with st._lock:
        st.cleared_before_mtime_ns = max(int(getattr(st, "cleared_before_mtime_ns", 0) or 0), newest_ns)
        st._cleared_at_ns = max(int(getattr(st, "_cleared_at_ns", 0) or 0), cleared_ns)
        st._cleared_run_seq = max(int(getattr(st, "_cleared_run_seq", 0) or 0), max_seq)
    prev_path = _clear_active_job_fields(st)
    with st._lock:
        if saved_prev_temp and os.path.isfile(saved_prev_temp):
            st._last_preview_temp_path = saved_prev_temp
            st._last_preview_temp_existed = saved_prev_existed
        st._last_invalidation_reason = ""
        st._auto_slice_in_progress = False
    if sync_host:
        host_fils = sync_filaments_from_orca_host(st, preserve_gcode_usage=False)
        if not host_fils:
            with st._lock:
                st.slicer_filaments = []
                st.filament_signature = ""
    st.save_config()
    diag_entry = _record_diagnostic_load_event(
        st,
        trigger=reason,
        decision="cleared",
        reason=f"tombstoned_sidecars;cleared_seq={st._cleared_run_seq};cleared_before_mtime_ns={st.cleared_before_mtime_ns}",
        file_source="clear_gcode",
        file_path=prev_path,
        compute_sha=False,
    )
    return diag_entry


def _resolve_disambiguated_staged_name(
    staged_dir: str,
    base_name: str,
    plate_index: Optional[int],
    output_name: str = "",
) -> str:
    p_idx = _normalize_plate_index(plate_index)
    candidate_name = _sanitize_staged_gcode_filename(
        output_name or base_name,
        default_name="sliced_plate.gcode",
        plate_index=p_idx,
        disambiguate_plate=False,
    )
    existing_path = os.path.join(staged_dir, candidate_name)
    if p_idx is not None and os.path.isfile(existing_path):
        old_meta = _read_provenance_sidecar(existing_path)
        old_plate = _normalize_plate_index(old_meta.get("plate_index"))
        if old_plate is not None and old_plate != p_idx:
            candidate_name = _sanitize_staged_gcode_filename(
                output_name or base_name,
                default_name="sliced_plate.gcode",
                plate_index=p_idx,
                disambiguate_plate=True,
            )
    return candidate_name


def _read_source_file_single_pass(src_path: str) -> bytes:
    """Read a source G-code or .gcode.3mf file in a single open() call."""
    with open(src_path, "rb") as src_f:
        return src_f.read()


def _decode_gcode_or_3mf_bytes(
    raw_bytes: bytes,
    source_name: str = "",
    plate_index: Optional[int] = None,
    binary_error_message: str = "",
) -> Dict[str, Any]:
    """Decode raw bytes of a `.gcode` or `.gcode.3mf` ZIP payload and validate genuine G-code."""
    req_plate = _normalize_plate_index(plate_index)
    if req_plate is None and source_name:
        req_plate = _extract_plate_index_from_name(source_name)
    if _is_zip_magic_bytes(raw_bytes):
        extracted = extract_gcode_from_3mf(raw_bytes, plate_index=req_plate, filename=source_name)
        extracted["is_container"] = True
        extracted["payload_bytes"] = str(extracted["gcode_text"]).encode("utf-8")
        extracted["plate_index"] = _normalize_plate_index(extracted.get("plate_index")) or req_plate
        return extracted
    if is_probably_binary_bytes(raw_bytes):
        msg = binary_error_message or (
            f"Rejected '{os.path.basename(source_name or 'file')}': binary data is not valid G-code."
        )
        raise ValueError(msg)
    decoded = raw_bytes.decode("utf-8", errors="replace")
    lines = decoded.splitlines(keepends=True)
    validate_gcode_lines(lines, source_name=source_name)
    return {
        "gcode_text": decoded,
        "lines": lines,
        "payload_bytes": decoded.encode("utf-8"),
        "plate_index": req_plate,
        "zip_entry": "",
        "slice_info": {},
        "available_plates": [],
        "is_container": False,
    }


def _build_staged_provenance(
    staged_path: str,
    *,
    output_name: str,
    original_source_name: str,
    source_kind: str,
    from_container: bool,
    host_name: str,
    plate_index: Optional[int],
    project_model: str,
    slicing_run_id: str,
    slicing_run_seq: int,
    created_ns: int,
    source_mtime_ns: int,
    payload_bytes: bytes,
    filaments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build normalized provenance sidecar dictionary for a staged G-code artifact."""
    return {
        "staged_path": staged_path,
        "filename": os.path.basename(staged_path),
        "output_name": str(output_name or os.path.basename(staged_path)),
        "original_source_name": str(original_source_name or os.path.basename(staged_path)),
        "source_kind": str(source_kind or "slicing_pipeline"),
        "from_3mf_container": bool(from_container),
        "host": str(host_name or ""),
        "plate_index": plate_index,
        "project_model": str(project_model or ""),
        "slicing_run_id": str(slicing_run_id or ""),
        "slicing_run_seq": int(slicing_run_seq or 0),
        "created_ns": int(created_ns),
        "source_mtime_ns": int(source_mtime_ns),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "filament_signature": compute_filament_signature(filaments),
        "filament_count": len(filaments),
        "used_filaments": [
            {
                "slicer_tool": int(f.get("slicer_tool", 0)),
                "material": str(f.get("material") or "").upper(),
                "color": str(f.get("color") or "").upper(),
            }
            for f in filaments
            if f.get("used", True)
        ],
        "is_cleared": False,
        "cleared_ns": 0,
        "is_stale": False,
        "is_superseded": False,
    }


def _stage_gcode_file_to_storage(
    src_path: str,
    state: Optional[PluginState] = None,
    output_name: str = "",
    plate_index: Optional[int] = None,
    host_name: str = "",
    slicing_run_id: str = "",
    slicing_run_seq: int = 0,
    project_model: str = "",
    source_kind: str = "slicing_pipeline",
    pre_extracted: Optional[Dict[str, Any]] = None,
    pre_lines: Optional[List[str]] = None,
    pre_bytes: Optional[bytes] = None,
    allow_overwrite_in_staged: bool = False,
) -> Optional[str]:
    """
    Atomically stage a validated text G-code file (extracting `Metadata/plate_<n>.gcode`
    when `src_path` is a `.gcode.3mf` ZIP container) into `<storage>/staged_gcode/` and write
    a matching atomic `.meta.json` provenance sidecar. Rejects binary/corrupt inputs and
    leaves zero temporary or partial files behind on error.
    """
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    if not storage_dir:
        return None
    staged_dir = os.path.join(storage_dir, "staged_gcode")
    try:
        if not src_path or (pre_bytes is None and pre_lines is None and not os.path.isfile(src_path)):
            return None

        req_plate = _normalize_plate_index(plate_index)
        if req_plate is None:
            req_plate = _extract_plate_index_from_name(output_name or os.path.basename(src_path))

        already_staged = _is_inside_dir(src_path, staged_dir)
        slice_info: Dict[str, Any] = {}
        is_container = False

        if pre_lines is not None:
            lines = list(pre_lines)
            validate_gcode_lines(lines, source_name=output_name or src_path)
            payload_bytes = "".join(lines).encode("utf-8")
            resolved_plate = req_plate
            if isinstance(pre_extracted, dict):
                resolved_plate = _normalize_plate_index(pre_extracted.get("plate_index")) or req_plate
                slice_info = dict(pre_extracted.get("slice_info") or {})
                is_container = True
        elif pre_extracted is not None and isinstance(pre_extracted, dict) and pre_extracted.get("gcode_text"):
            is_container = True
            lines = list(pre_extracted.get("lines") or str(pre_extracted["gcode_text"]).splitlines(keepends=True))
            validate_gcode_lines(lines, source_name=output_name or src_path)
            payload_bytes = str(pre_extracted["gcode_text"]).encode("utf-8")
            resolved_plate = _normalize_plate_index(pre_extracted.get("plate_index")) or req_plate
            slice_info = dict(pre_extracted.get("slice_info") or {})
        else:
            raw_bytes = bytes(pre_bytes) if pre_bytes is not None else _read_source_file_single_pass(src_path)
            if not _is_zip_magic_bytes(raw_bytes) and is_probably_binary_bytes(raw_bytes):
                _record_diagnostic_load_event(
                    st,
                    trigger="stage_gcode",
                    decision="rejected",
                    reason="rejected:binary_payload",
                    file_source=source_kind,
                    file_path=src_path,
                    slicing_run_id=slicing_run_id,
                    compute_sha=False,
                    allow_temp_probe=True,
                )
                return None
            decoded = _decode_gcode_or_3mf_bytes(
                raw_bytes,
                source_name=output_name or src_path,
                plate_index=req_plate,
            )
            lines = list(decoded["lines"])
            payload_bytes = decoded["payload_bytes"]
            resolved_plate = decoded.get("plate_index") or req_plate
            slice_info = dict(decoded.get("slice_info") or {})
            is_container = bool(decoded.get("is_container"))

        if already_staged and not is_container and not allow_overwrite_in_staged and not output_name:
            staged_path = src_path
            created_new_file = False
        else:
            safe_base = _resolve_disambiguated_staged_name(
                staged_dir,
                os.path.basename(src_path),
                resolved_plate,
                output_name=output_name,
            )
            staged_path = os.path.join(staged_dir, safe_base)
            created_new_file = not os.path.exists(staged_path)
            _atomic_write_bytes(staged_path, payload_bytes, suffix=".gcode", ensure_parent=True)

        fils = parse_slicer_filaments(lines)
        job_meta = parse_gcode_job_metadata(lines, staged_path)
        _merge_slice_info_into_parsed(fils, job_meta, slice_info)
        with st._lock:
            cleared_at_guard = int(getattr(st, "_cleared_at_ns", 0) or 0)
            cleared_seq_guard = int(getattr(st, "_cleared_run_seq", 0) or 0)
        created_ns = max(time.time_ns(), cleared_at_guard + 1)
        src_mtime_ns = _get_file_mtime_ns(src_path, default=created_ns)

        snap_model = str(project_model or "").strip()
        if not snap_model:
            ui_snap = st.get_host_ui_snapshot()
            snap_model = str((ui_snap.get("active_model") or {}).get("display_name") or "").strip()
        if not snap_model and output_name:
            cand_stem = _strip_model_extension(output_name)
            if cand_stem and not _is_generic_model_stem(cand_stem):
                snap_model = cand_stem

        if not slicing_run_id or int(slicing_run_seq or 0) <= cleared_seq_guard:
            seq_val, run_id_val = st.next_slicing_run_id()
        else:
            seq_val, run_id_val = int(slicing_run_seq or 0), str(slicing_run_id)

        prov = _build_staged_provenance(
            staged_path,
            output_name=str(output_name or os.path.basename(staged_path)),
            original_source_name=os.path.basename(src_path),
            source_kind=str(source_kind or "slicing_pipeline"),
            from_container=is_container,
            host_name=host_name,
            plate_index=resolved_plate,
            project_model=snap_model,
            slicing_run_id=run_id_val,
            slicing_run_seq=seq_val,
            created_ns=created_ns,
            source_mtime_ns=src_mtime_ns,
            payload_bytes=payload_bytes,
            filaments=fils,
        )
        try:
            src_real_prov = os.path.realpath(src_path) if src_path else ""
            staged_real_prov = os.path.realpath(staged_path) if staged_path else ""
            if (
                str(source_kind or "").strip() not in ("file_picker", "manual")
                and str(host_name or "").strip().lower() != "file"
                and src_path
                and src_real_prov != staged_real_prov
                and _is_preview_temp_source(src_path, storage_dir)
                and os.path.isfile(src_path)
            ):
                prov["preview_temp_path"] = src_path
                prov["preview_temp_existed"] = True
            elif (
                str(source_kind or "").strip() == "slicing_pipeline"
                and str(host_name or "").strip().lower() != "file"
                and src_path
                and _is_ephemeral_upload_temp_source(src_path)
            ):
                with st._lock:
                    existing_prev = str(getattr(st, "_last_preview_temp_path", "") or "").strip()
                if (
                    existing_prev
                    and not _is_ephemeral_upload_temp_source(existing_prev)
                    and os.path.isfile(existing_prev)
                ):
                    prov["preview_temp_path"] = existing_prev
                    prov["preview_temp_existed"] = True
        except OSError:
            pass
        try:
            _write_provenance_sidecar_atomic(staged_path, prov)
            if source_kind in ("slicing_pipeline", "slicer_explicit"):
                _mark_superseded_staged_sidecars(
                    staged_dir=staged_dir,
                    active_staged_path=staged_path,
                    current_run_id=run_id_val,
                    plate_index=resolved_plate,
                    project_model=snap_model,
                )
        except Exception:
            if created_new_file and os.path.exists(staged_path):
                try:
                    os.unlink(staged_path)
                except OSError:
                    pass
            raise
        with st._lock:
            st._last_staged_provenance = dict(prov)
        _record_diagnostic_load_event(
            st,
            trigger="stage_gcode",
            decision="staged",
            reason="staged_ok",
            file_source=str(source_kind or "slicing_pipeline"),
            file_path=staged_path,
            slicing_run_id=run_id_val,
            sidecar=prov,
            compute_sha=False,
        )
        return staged_path
    except Exception as exc:
        _record_diagnostic_load_event(
            st,
            trigger="stage_gcode",
            decision="error",
            reason=f"staging_exception:{exc}",
            file_source=str(source_kind or "slicing_pipeline"),
            file_path=str(src_path or ""),
            slicing_run_id=str(slicing_run_id or ""),
            compute_sha=False,
        )
        return None


def _evaluate_staged_candidate(
    cand_path: str,
    state: Optional[PluginState] = None,
    allow_superseded: bool = False,
    allow_mismatch: bool = False,
    allow_unsliced_host: bool = False,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Strictly evaluate whether a candidate G-code file is eligible to be loaded.
    Rejects `is_cleared`, `is_superseded`, `is_stale`, pre-clear sequences/timestamps,
    missing preview temp sources / unsliced OrcaSlicer host states, and demonstrably
    mismatched models — regardless of `force_refresh`.
    """
    st = state or GLOBAL_STATE
    raw = str(cand_path or "").strip()
    if not raw or not os.path.isfile(raw):
        return False, "rejected:missing_file", {}
    if not _is_gcode_filename(raw):
        return False, "rejected:not_gcode_ext", {}

    sidecar = _read_provenance_sidecar(raw)
    if sidecar:
        if bool(sidecar.get("is_cleared")):
            return False, "rejected:is_cleared", sidecar
        if not allow_superseded and bool(sidecar.get("is_superseded")):
            return False, "rejected:is_superseded", sidecar
        if not allow_superseded and bool(sidecar.get("is_stale")):
            return False, "rejected:is_stale", sidecar

    if not allow_unsliced_host:
        sc_prev = str(sidecar.get("preview_temp_path") or "").strip() if sidecar else ""
        if sc_prev and bool(sidecar.get("preview_temp_existed")) and not os.path.isfile(sc_prev):
            return False, "rejected:missing_preview_temp_file", sidecar
        model_info = get_orca_active_model_info(state=st)
        if model_info.get("host_has_slice") is False:
            return False, "rejected:host_unsliced", sidecar
        if bool(model_info.get("host_gcode_attr_present")):
            host_gcode = str(model_info.get("host_gcode_path") or "").strip()
            if not host_gcode:
                return False, "rejected:host_missing_preview_gcode", sidecar
            if not os.path.isfile(host_gcode):
                return False, "rejected:host_missing_preview_gcode_file", sidecar

    with st._lock:
        cleared_run_seq = int(getattr(st, "_cleared_run_seq", 0) or 0)
        cleared_before_ns = int(getattr(st, "cleared_before_mtime_ns", 0) or 0)
        cleared_at_ns = int(getattr(st, "_cleared_at_ns", 0) or 0)

    seq_val = int(sidecar.get("slicing_run_seq", 0) or 0) if sidecar else 0
    created_ns = int(sidecar.get("created_ns", 0) or 0) if sidecar else 0

    if cleared_run_seq > 0 and seq_val > 0 and seq_val <= cleared_run_seq:
        return False, "rejected:cleared_run_seq", sidecar
    if cleared_at_ns > 0 and created_ns > 0 and created_ns <= cleared_at_ns:
        return False, "rejected:cleared_at_ns", sidecar

    try:
        st_cand = os.stat(raw)
        if st_cand.st_size <= 0:
            return False, "rejected:empty_file", sidecar
        mtime_ns = int(getattr(st_cand, "st_mtime_ns", int(float(st_cand.st_mtime) * 1_000_000_000)))
    except OSError:
        return False, "rejected:stat_failed", sidecar

    if seq_val == 0 and cleared_before_ns > 0 and mtime_ns <= cleared_before_ns:
        return False, "rejected:cleared_before_mtime_ns", sidecar

    if not allow_mismatch:
        mismatch, mismatch_reason = _check_candidate_model_mismatch(raw, sidecar=sidecar, state=st)
        if mismatch:
            return False, f"rejected:{mismatch_reason}", sidecar

    return True, "accepted:eligible", sidecar


def discover_sliced_gcode_candidates(
    state: Optional[PluginState] = None,
    limit: int = 12,
    include_stale: bool = True,
) -> List[str]:
    """
    Discover valid G-code candidates strictly inside the plugin's own `<storage>/staged_gcode/`
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

    found: List[Tuple[int, int, int, str]] = []
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
                    if fname.startswith(".ace_tmp_") or lower.endswith((".bak", ".meta.json")):
                        continue
                    real_f = os.path.realpath(entry.path)
                    if real_f in seen_files:
                        continue
                    st_res = entry.stat(follow_symlinks=False)
                    if st_res.st_size <= 0:
                        continue
                    try:
                        with open(entry.path, "rb") as head_f:
                            head_bytes = head_f.read(512)
                        if is_probably_binary_bytes(head_bytes):
                            continue
                    except OSError:
                        continue
                    seen_files.add(real_f)
                    sidecar: Optional[Dict[str, Any]] = None
                    if not include_stale:
                        ok_cand, _, sidecar = _evaluate_staged_candidate(entry.path, st)
                        if not ok_cand:
                            continue
                    mtime_ns = int(getattr(st_res, "st_mtime_ns", int(float(st_res.st_mtime) * 1_000_000_000)))
                    if sidecar is None:
                        sidecar = _read_provenance_sidecar(entry.path)
                    seq_val = int(sidecar.get("slicing_run_seq", 0) or 0) if sidecar else 0
                    created_ns = int(sidecar.get("created_ns", 0) or 0) if sidecar else 0
                    found.append((seq_val, created_ns, mtime_ns, entry.path))
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        return []

    found.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    return [path for _, _, _, path in found[: max(1, int(limit))]]


def acquire_gcode_from_slicer(
    state: Optional[PluginState] = None,
    explicit_path: str = "",
    include_last_gcode_path: bool = True,
    force_refresh: bool = False,
    output_name: str = "",
    plate_index: Optional[int] = None,
    explicit_flag: bool = False,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """
    Acquire a G-code file strictly from sandbox-safe sources (read-only analysis only;
    never sends Moonraker commands, starts a print, or scans `/tmp` / system folders):
    1. An `explicit_path` directly passed by OrcaSlicer (`execute(ctx)` / `on_lifecycle_event`)
       or explicit reload (`.gcode` or `.gcode.3mf` container extracted into staged storage).
    2. Eligible non-cleared, non-stale, model-compatible files staged inside `<storage>/staged_gcode/`.
    3. Eligible `state.last_gcode_path` (only when `include_last_gcode_path=True`, skipping
       transient `/tmp` / `/var/tmp` / `/usr/tmp` paths when running inside OrcaSlicer).
    4. Otherwise clears active job state and syncs active in-memory filaments.
    Returns `(source_kind, resolved_path, preview_info)`.
    """
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    staged_dir = os.path.join(storage_dir, "staged_gcode") if storage_dir else ""
    explicit_clean = str(explicit_path or "").strip()
    explicit_is_container = False
    explicit_is_sys_temp = bool(explicit_clean and _is_system_temp_path(explicit_clean, storage_dir))
    explicit_valid = False
    explicit_sidecar: Dict[str, Any] = {}
    explicit_prefetched_bytes: Optional[bytes] = None
    if explicit_clean:
        if HAS_ORCA_RUNTIME and explicit_is_sys_temp and not explicit_flag:
            _record_diagnostic_load_event(
                st,
                trigger="acquire_gcode_from_slicer",
                decision="rejected",
                reason="rejected:sys_temp_unexplicit",
                file_source="explicit_candidate",
                file_path=explicit_clean,
                compute_sha=False,
            )
            explicit_clean = ""
        elif os.path.isfile(explicit_clean):
            if _is_container_3mf_filename(explicit_clean):
                try:
                    explicit_prefetched_bytes = _read_source_file_single_pass(explicit_clean)
                    if _is_zip_magic_bytes(explicit_prefetched_bytes):
                        explicit_is_container = True
                        explicit_valid = True
                    else:
                        explicit_prefetched_bytes = None
                except OSError:
                    explicit_valid = False
            elif _is_gcode_filename(explicit_clean):
                explicit_inside_staged = bool(staged_dir and _is_inside_dir(explicit_clean, staged_dir))
                if explicit_inside_staged and not explicit_flag:
                    ok_exp, rsn_exp, sc_exp = _evaluate_staged_candidate(explicit_clean, st)
                    if not ok_exp:
                        _record_diagnostic_load_event(
                            st,
                            trigger="acquire_gcode_from_slicer",
                            decision="rejected",
                            reason=rsn_exp,
                            file_source="staged_echoed_explicit",
                            file_path=explicit_clean,
                            sidecar=sc_exp,
                            compute_sha=False,
                        )
                    else:
                        explicit_valid = True
                        explicit_sidecar = sc_exp
                else:
                    sc_exp = _read_provenance_sidecar(explicit_clean) if not explicit_is_sys_temp else {}
                    if sc_exp and bool(sc_exp.get("is_cleared")):
                        _record_diagnostic_load_event(
                            st,
                            trigger="acquire_gcode_from_slicer",
                            decision="rejected",
                            reason="rejected:is_cleared",
                            file_source="slicer_explicit",
                            file_path=explicit_clean,
                            sidecar=sc_exp,
                            compute_sha=False,
                        )
                    else:
                        try:
                            if explicit_is_sys_temp and HAS_ORCA_RUNTIME and explicit_flag:
                                explicit_prefetched_bytes = _read_source_file_single_pass(explicit_clean)
                                head_bytes = explicit_prefetched_bytes[:4096]
                            else:
                                with open(explicit_clean, "rb") as chk_f:
                                    head_bytes = chk_f.read(4096)
                            if is_probably_binary_bytes(head_bytes):
                                raise ValueError(
                                    f"Rejected '{os.path.basename(explicit_clean)}': binary data is not valid G-code."
                                )
                            explicit_valid = True
                            explicit_sidecar = sc_exp
                        except OSError:
                            explicit_valid = False

    staged_candidates = discover_sliced_gcode_candidates(state=st)
    newest_staged = ""
    newest_sidecar: Dict[str, Any] = {}
    if not explicit_valid and staged_candidates:
        for cand in staged_candidates:
            ok_cand, rsn_cand, sc_cand = _evaluate_staged_candidate(cand, st)
            if ok_cand and force_refresh and str((sc_cand or {}).get("source_kind") or "") == "file_picker":
                ok_cand = False
                rsn_cand = "rejected:file_picker_on_slicer_reload"
            if ok_cand:
                newest_staged = cand
                newest_sidecar = sc_cand
                break
            _record_diagnostic_load_event(
                st,
                trigger="acquire_gcode_from_slicer",
                decision="rejected",
                reason=rsn_cand,
                file_source="staged_gcode",
                file_path=cand,
                sidecar=sc_cand,
                compute_sha=False,
            )

    cur_valid = False
    cur_path = ""
    cur_source = ""
    cur_sidecar: Dict[str, Any] = {}
    if include_last_gcode_path:
        with st._lock:
            cur_path = str(st.last_gcode_path or "").strip()
            cur_source = str(st.last_gcode_source or "").strip()
        if cur_path and _is_gcode_filename(cur_path) and not (force_refresh and cur_source == "file_picker"):
            if HAS_ORCA_RUNTIME and _is_system_temp_path(cur_path, storage_dir):
                _record_diagnostic_load_event(
                    st,
                    trigger="acquire_gcode_from_slicer",
                    decision="rejected",
                    reason="rejected:sys_temp_remembered",
                    file_source=cur_source or "existing",
                    file_path=cur_path,
                    compute_sha=False,
                )
            else:
                try:
                    if os.path.isfile(cur_path):
                        with open(cur_path, "rb") as cf:
                            non_bin = not is_probably_binary_bytes(cf.read(4096))
                        if non_bin:
                            ok_cur, rsn_cur, cur_sidecar = _evaluate_staged_candidate(
                                cur_path,
                                st,
                                allow_superseded=(cur_source == "file_picker"),
                                allow_mismatch=(cur_source == "file_picker"),
                                allow_unsliced_host=(cur_source == "file_picker"),
                            )
                            if ok_cur:
                                cur_valid = True
                            else:
                                _record_diagnostic_load_event(
                                    st,
                                    trigger="acquire_gcode_from_slicer",
                                    decision="rejected",
                                    reason=rsn_cur,
                                    file_source=cur_source or "existing",
                                    file_path=cur_path,
                                    sidecar=cur_sidecar,
                                    compute_sha=False,
                                )
                except OSError:
                    cur_valid = False

    chosen_path = ""
    chosen_sidecar: Optional[Dict[str, Any]] = None
    source_kind = "presets_only"
    accept_reason = ""
    if explicit_valid:
        if explicit_is_container or (explicit_is_sys_temp and HAS_ORCA_RUNTIME and explicit_flag):
            staged_from_explicit = _stage_gcode_file_to_storage(
                explicit_clean,
                state=st,
                output_name=output_name or explicit_clean,
                plate_index=plate_index,
                source_kind="slicer_explicit",
                pre_bytes=explicit_prefetched_bytes,
            )
            if not staged_from_explicit:
                raise ValueError(
                    f"Could not extract valid G-code from '{os.path.basename(explicit_clean)}'."
                )
            chosen_path = staged_from_explicit
            source_kind = "slicer_explicit"
            accept_reason = "accepted:explicit_staged"
        else:
            chosen_path = explicit_clean
            chosen_sidecar = explicit_sidecar
            source_kind = "slicer_explicit"
            accept_reason = "accepted:explicit"
    elif newest_staged:
        newest_mtime_ns = _get_file_mtime_ns(newest_staged)
        newest_seq = int(newest_sidecar.get("slicing_run_seq", 0) or 0) if newest_sidecar else 0
        cur_seq = int(cur_sidecar.get("slicing_run_seq", 0) or 0) if (cur_valid and cur_sidecar) else 0
        cur_mtime_ns = _get_file_mtime_ns(cur_path) if cur_valid else 0
        same_as_current = False
        if cur_valid:
            try:
                same_as_current = os.path.realpath(cur_path) == os.path.realpath(newest_staged)
            except OSError:
                same_as_current = cur_path == newest_staged

        has_newer_slice = (newest_seq > cur_seq) if (newest_seq > 0 or cur_seq > 0) else (newest_mtime_ns > cur_mtime_ns)
        if cur_valid and not force_refresh and (
            same_as_current
            or cur_source == "file_picker"
            or not has_newer_slice
        ):
            chosen_path = cur_path
            chosen_sidecar = cur_sidecar
            source_kind = cur_source or "existing"
            accept_reason = "accepted:existing_current"
        else:
            chosen_path = newest_staged
            chosen_sidecar = newest_sidecar
            source_kind = "staged_gcode"
            accept_reason = "accepted:staged_refresh" if force_refresh else "accepted:staged_newest"
    elif cur_valid:
        chosen_path = cur_path
        chosen_sidecar = cur_sidecar
        source_kind = cur_source or "existing"
        accept_reason = "accepted:existing_fallback"

    if not chosen_path:
        _clear_active_job_fields(st)
        host_fils = sync_filaments_from_orca_host(st, preserve_gcode_usage=False)
        if not host_fils:
            with st._lock:
                st.slicer_filaments = []
                st.filament_signature = ""
        st.save_config()
        _record_diagnostic_load_event(
            st,
            trigger="acquire_gcode_from_slicer",
            decision="empty",
            reason="no_eligible_gcode",
            file_source="presets_only",
            file_path="",
            compute_sha=False,
        )
        return "presets_only", "", None

    cached_info: Optional[Dict[str, Any]] = None
    if (
        not force_refresh
        and not explicit_clean
        and accept_reason in ("accepted:existing_current", "accepted:existing_fallback")
        and os.path.isfile(chosen_path)
    ):
        try:
            cur_sig = (
                os.path.realpath(chosen_path),
                _get_file_mtime_ns(chosen_path),
                int(os.path.getsize(chosen_path)),
            )
        except OSError:
            cur_sig = None
        if cur_sig is not None:
            with st._lock:
                prev_sig = getattr(st, "_last_parsed_file_sig", None)
                prev_info = dict(st.last_postprocess_info) if st.last_postprocess_info else None
                cur_tmap = dict(st.tool_map)
                opts_match = bool(
                    prev_info
                    and prev_info.get("bedLeveling") == bool(st.bed_leveling)
                    and prev_info.get("skipPurgeForAlreadyLoadedTool") == bool(st.skip_purge_for_already_loaded_tool)
                    and prev_info.get("cutTipPost") == bool(st.cut_tip_after_print)
                    and prev_info.get("forceInitialPurge") == bool(st.force_initial_purge)
                )
            if prev_sig == cur_sig and prev_info and opts_match:
                cached_info = remap_flush_info(prev_info, cur_tmap)
                with st._lock:
                    st.last_postprocess_info = dict(cached_info)

    info = cached_info if cached_info is not None else preview_gcode_file(chosen_path, st, pre_sidecar=chosen_sidecar)
    with st._lock:
        st.last_gcode_source = source_kind
        st.cleared_before_mtime_ns = 0
        st._last_invalidation_reason = ""
        st._auto_slice_in_progress = False
    st.save_config()
    _record_diagnostic_load_event(
        st,
        trigger="acquire_gcode_from_slicer",
        decision="accepted",
        reason=accept_reason,
        file_source=source_kind,
        file_path=chosen_path,
        compute_sha=(cached_info is None),
        allow_temp_probe=(source_kind == "slicer_explicit"),
    )
    return source_kind, chosen_path, info


def load_gcode_from_content(
    filename: str,
    content: Any,
    state: Optional[PluginState] = None,
    encoding: str = "auto",
    plate_index: Optional[int] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Stage a user-selected local `.gcode` or `.gcode.3mf` container (e.g. from Browse `.gcode`)
    atomically into `<storage>/staged_gcode/`, validate it as genuine G-code, analyze strictly
    read-only, and write provenance sidecar. Leaves zero temp/partial files on error.
    """
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    if not storage_dir:
        raise RuntimeError("Plugin storage directory is unavailable.")

    raw_bytes: Optional[bytes] = None
    if isinstance(content, (bytes, bytearray)):
        raw_bytes = bytes(content)
    elif str(encoding).lower() == "base64":
        raw_str = str(content or "").strip()
        if not raw_str:
            raise ValueError("Selected G-code file is empty.")
        try:
            raw_bytes = base64.b64decode(raw_str, validate=True)
        except Exception as exc:
            raise ValueError(f"Invalid base64 payload for '{filename}': {exc}") from exc
    else:
        text_content = str(content or "")
        if not text_content or not text_content.strip():
            raise ValueError("Selected G-code file is empty.")
        raw_bytes = text_content.encode("utf-8", errors="replace")

    if not raw_bytes or not raw_bytes.strip():
        raise ValueError("Selected G-code file is empty.")

    decoded = _decode_gcode_or_3mf_bytes(
        raw_bytes,
        source_name=filename,
        plate_index=plate_index,
        binary_error_message=t(
            "host.gcode_binary_rejected", lang=st.language, file=os.path.basename(filename or "file")
        ),
    )
    lines = list(decoded["lines"])
    out_bytes = decoded["payload_bytes"]
    resolved_plate = decoded["plate_index"]
    slice_info = dict(decoded.get("slice_info") or {})
    is_container = bool(decoded.get("is_container"))

    staged_dir = os.path.join(storage_dir, "staged_gcode")
    safe_base = _sanitize_staged_gcode_filename(
        filename,
        default_name="uploaded_plate.gcode",
        plate_index=resolved_plate if is_container else None,
    )
    staged_path = os.path.join(staged_dir, safe_base)
    created_new_file = not os.path.exists(staged_path)
    _atomic_write_bytes(staged_path, out_bytes, suffix=".gcode", ensure_parent=True)

    fils = parse_slicer_filaments(lines)
    job_meta = parse_gcode_job_metadata(lines, staged_path)
    _merge_slice_info_into_parsed(fils, job_meta, slice_info)
    job_meta["output_name"] = safe_base
    if resolved_plate is not None:
        job_meta["plate_index"] = resolved_plate
    seq_val, run_id_val = st.next_slicing_run_id()
    with st._lock:
        cleared_at_guard = int(getattr(st, "_cleared_at_ns", 0) or 0)
    created_ns = max(time.time_ns(), cleared_at_guard + 1)
    ui_snap = st.get_host_ui_snapshot()
    proj_model = str((ui_snap.get("active_model") or {}).get("display_name") or "").strip()
    prov = _build_staged_provenance(
        staged_path,
        output_name=safe_base,
        original_source_name=os.path.basename(str(filename or safe_base)),
        source_kind="file_picker",
        from_container=is_container,
        host_name="FilePicker",
        plate_index=resolved_plate,
        project_model=proj_model,
        slicing_run_id=run_id_val,
        slicing_run_seq=seq_val,
        created_ns=created_ns,
        source_mtime_ns=created_ns,
        payload_bytes=out_bytes,
        filaments=fils,
    )
    try:
        _write_provenance_sidecar_atomic(staged_path, prov)
    except Exception:
        if created_new_file and os.path.exists(staged_path):
            try:
                os.unlink(staged_path)
            except OSError:
                pass
        raise
    with st._lock:
        st._last_staged_provenance = dict(prov)

    info = preview_gcode_file(
        staged_path,
        st,
        extra_slice_info=slice_info,
        plate_index=resolved_plate,
        output_name=safe_base,
        pre_lines=lines,
        pre_filaments=fils,
        pre_job_metadata=job_meta,
        pre_sidecar=prov,
    )
    with st._lock:
        st.last_gcode_source = "file_picker"
        st.cleared_before_mtime_ns = 0
    st.save_config()
    _record_diagnostic_load_event(
        st,
        trigger="load_gcode_from_content",
        decision="accepted",
        reason="accepted:file_picker",
        file_source="file_picker",
        file_path=staged_path,
        slicing_run_id=run_id_val,
        sidecar=prov,
        compute_sha=False,
    )
    return staged_path, info


def _norm_fp_name(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def _has_real_attr(target: Any, attr_name: str) -> bool:
    """Return True if `target` genuinely defines `attr_name` without fabricating mock attributes."""
    if isinstance(target, dict):
        return attr_name in target
    try:
        inst_dict = getattr(target, "__dict__", None)
        if isinstance(inst_dict, dict) and attr_name in inst_dict:
            return True
    except Exception:
        pass
    try:
        return hasattr(type(target), attr_name)
    except Exception:
        return False


def _read_preset_dirty_flag(preset_obj: Any) -> Optional[bool]:
    """Read optional dirty/modified flag from an OrcaSlicer preset object."""
    if preset_obj is None:
        return None
    for attr in ("is_dirty", "dirty", "is_modified", "modified"):
        if not _has_real_attr(preset_obj, attr):
            continue
        try:
            val = preset_obj.get(attr) if isinstance(preset_obj, dict) else getattr(preset_obj, attr)
            if callable(val):
                val = val()
            if isinstance(val, bool):
                return val
        except Exception:
            continue
    return None


def _read_host_preset_fingerprint() -> Dict[str, Any]:
    """
    Capture active printer, process (print), and filament preset names/settings from
    `orca.host.preset_bundle()` strictly on the UI/Main thread to detect slicing-relevant edits.
    """
    empty = {
        "digest": "",
        "captured": False,
        "printer_name": "",
        "process_name": "",
        "filament_names": [],
    }
    try:
        host_mod = getattr(orca, "host", None)
        preset_bundle_fn = getattr(host_mod, "preset_bundle", None)
        if not callable(preset_bundle_fn):
            return empty
        bundle = preset_bundle_fn()
        if bundle is None:
            return empty

        printer_obj = None
        if hasattr(bundle, "current_printer_preset"):
            try:
                printer_obj = bundle.current_printer_preset()
            except Exception:
                printer_obj = None
        printer_name = _clean_preset_str(getattr(printer_obj, "name", ""))
        if not printer_name and hasattr(bundle, "current_printer_preset_name"):
            try:
                printer_name = _clean_preset_str(bundle.current_printer_preset_name())
            except Exception:
                printer_name = ""

        process_obj = None
        if hasattr(bundle, "current_process_preset"):
            try:
                process_obj = bundle.current_process_preset()
            except Exception:
                process_obj = None
        process_name = _clean_preset_str(getattr(process_obj, "name", ""))
        if not process_name and hasattr(bundle, "current_process_preset_name"):
            try:
                process_name = _clean_preset_str(bundle.current_process_preset_name())
            except Exception:
                process_name = ""

        def _read_bundle_or_preset_cfg(key: str, preset: Any) -> Any:
            if hasattr(bundle, "full_config_value"):
                try:
                    val = bundle.full_config_value(key)
                    if val not in (None, "", []):
                        return val
                except Exception:
                    pass
            if preset is not None and hasattr(preset, "config_value"):
                try:
                    return preset.config_value(key)
                except Exception:
                    return None
            return None

        process_keys = (
            "layer_height",
            "initial_layer_print_height",
            "wall_loops",
            "top_shell_layers",
            "bottom_shell_layers",
            "sparse_infill_density",
            "sparse_infill_pattern",
            "enable_support",
            "support_type",
            "brim_type",
            "flush_multiplier",
            "enable_prime_tower",
            "print_sequence",
        )
        process_cfg: Dict[str, Any] = {}
        for k in process_keys:
            v = _read_bundle_or_preset_cfg(k, process_obj)
            if v not in (None, "", []):
                process_cfg[k] = str(v) if not isinstance(v, (list, tuple)) else [str(x) for x in v]

        printer_cfg: Dict[str, Any] = {}
        for k in ("printer_model", "nozzle_diameter", "nozzle_type"):
            v = _read_bundle_or_preset_cfg(k, printer_obj)
            if v not in (None, "", []):
                printer_cfg[k] = str(v) if not isinstance(v, (list, tuple)) else [str(x) for x in v]

        fil_presets = list(bundle.current_filament_presets() or []) if hasattr(bundle, "current_filament_presets") else []
        fil_names_raw = list(bundle.current_filament_preset_names() or []) if hasattr(bundle, "current_filament_preset_names") else []
        fil_dirty = [_read_preset_dirty_flag(fp) for fp in fil_presets]
        host_fils = _read_host_filaments_from_preset_bundle()
        fil_temps: Dict[str, Any] = {}
        for k in ("nozzle_temperature", "hot_plate_temp", "textured_plate_temp"):
            v = _read_bundle_or_preset_cfg(k, fil_presets[0] if fil_presets else None)
            if v not in (None, "", []):
                fil_temps[k] = str(v) if not isinstance(v, (list, tuple)) else [str(x) for x in v]

        fil_names = [
            str(fil_names_raw[i] if i < len(fil_names_raw) and fil_names_raw[i] else (f.get("profile") or f.get("material") or ""))
            for i, f in enumerate(host_fils)
        ] or [str(n) for n in fil_names_raw if n]

        has_any = bool(
            printer_name
            or process_name
            or process_cfg
            or printer_cfg
            or host_fils
            or fil_names
            or fil_temps
            or _read_preset_dirty_flag(process_obj) is not None
            or _read_preset_dirty_flag(printer_obj) is not None
        )
        if not has_any:
            return empty

        fp_payload = {
            "printer": {"name": printer_name, "dirty": _read_preset_dirty_flag(printer_obj), "cfg": printer_cfg},
            "process": {"name": process_name, "dirty": _read_preset_dirty_flag(process_obj), "cfg": process_cfg},
            "filaments": [
                {
                    "tool": f.get("slicer_tool"),
                    "material": f.get("material"),
                    "color": f.get("color"),
                    "profile": f.get("profile"),
                    "vendor": f.get("vendor"),
                    "diameter": f.get("diameter"),
                }
                for f in host_fils
            ],
            "filament_names": fil_names,
            "filament_dirty": fil_dirty,
            "temps": fil_temps,
        }
        digest = hashlib.sha256(
            json.dumps(fp_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]
        return {
            "digest": digest,
            "captured": True,
            "printer_name": printer_name,
            "process_name": process_name,
            "filament_names": fil_names,
        }
    except Exception:
        return empty


def _build_invalidation_notice_text(reason: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """Return a clear localized banner message explaining why an invalidated slice must be re-sliced."""
    if reason == "auto_reslice_started":
        return t("ui.notice.slicing_in_progress", lang=lang)
    if reason in ("host_presets_changed", "host_model_changed", "host_plate_changed"):
        return t("ui.notice.slice_outdated_presets", lang=lang)
    return t("ui.notice.no_gcode_slice_first", lang=lang)


def _try_trigger_host_slice(state: PluginState) -> Dict[str, Any]:
    """
    Safely probe OrcaSlicer host objects (`plater()`, `slicer`, `host`) strictly on UI/Main thread
    for a slice-trigger callable (`slice_plate`, `start_slice`, `reslice`, `slice`, `trigger_slice`).
    Returns `{"status": "started"|"already_sliced"|"unavailable", "method": ...}`.
    """
    if _is_worker_thread_context():
        return {"status": "unavailable", "method": ""}
    host = getattr(orca, "host", None)
    dummy_cls = globals().get("_DummyHostModule")
    if host is None or (dummy_cls is not None and isinstance(host, dummy_cls)):
        return {"status": "unavailable", "method": ""}

    candidates: List[Tuple[str, Any]] = []
    for attr_name, target_label in (("plater", "plater"), ("slicer", "slicer")):
        try:
            acc = getattr(host, attr_name, None)
            if acc is not None:
                obj = acc() if callable(acc) else acc
                if obj is not None:
                    candidates.append((target_label, obj))
        except Exception:
            continue
    candidates.append(("host", host))

    slice_fn = None
    slice_label = ""
    for scope, obj in candidates:
        for meth in ("slice_plate", "start_slice", "reslice", "slice", "trigger_slice"):
            if not _has_real_attr(obj, meth):
                continue
            try:
                fn = getattr(obj, meth, None)
                if callable(fn):
                    slice_fn = fn
                    slice_label = f"{scope}.{meth}"
                    break
            except Exception:
                continue
        if slice_fn is not None:
            break

    if slice_fn is None:
        return {"status": "unavailable", "method": ""}

    try:
        res = slice_fn()
    except Exception:
        return {"status": "unavailable", "method": slice_label}

    if isinstance(res, dict):
        st_str = str(res.get("status") or "").strip().lower()
        if res.get("already_sliced") or res.get("started") is False or st_str in (
            "already_sliced", "up_to_date", "noop", "skipped", "idle", "current"
        ):
            return {"status": "already_sliced", "method": slice_label}
        if res.get("started") is True or st_str in (
            "started", "slicing", "running", "queued", "reslicing"
        ):
            return {"status": "started", "method": slice_label}
    elif isinstance(res, bool):
        return {"status": "started" if res else "already_sliced", "method": slice_label}
    elif isinstance(res, str):
        low = res.strip().lower()
        if low in ("already_sliced", "up_to_date", "noop", "skipped", "idle", "current"):
            return {"status": "already_sliced", "method": slice_label}
        if low in ("started", "slicing", "running", "queued", "reslicing"):
            return {"status": "started", "method": slice_label}

    # Void/None return: only treat as started if host is visibly dirty/unsliced; else keep slice.
    snap = state.get_host_ui_snapshot()
    am = snap.get("active_model") or {}
    pf_cur = str(((snap.get("preset_fingerprint") or {}).get("digest")) or "")
    with state._lock:
        fp = dict(getattr(state, "_slice_host_fingerprint", {}) or {})
    dirty = (
        am.get("host_has_slice") is False
        or (fp.get("preset_digest") and pf_cur and fp["preset_digest"] != pf_cur)
        or (fp.get("model") and _norm_fp_name(am.get("display_name")) and fp["model"] != _norm_fp_name(am.get("display_name")))
        or (fp.get("plate_index") is not None and am.get("plate_index") is not None and fp["plate_index"] != am.get("plate_index"))
    )
    return {"status": "started" if dirty else "already_sliced", "method": slice_label}


def _record_slice_host_fingerprint(state: PluginState) -> None:
    """Remember active OrcaSlicer model, plate index, and preset/filament digest for the current slice."""
    try:
        if not _is_worker_thread_context():
            snap = state.refresh_host_ui_snapshot()
        else:
            snap = state.get_host_ui_snapshot()
        info = get_orca_active_model_info(state=state)
    except Exception:
        snap = {}
        info = {}
    pf = snap.get("preset_fingerprint") if isinstance(snap, dict) else {}
    fp = {
        "model": _norm_fp_name(info.get("display_name")),
        "plate_index": info.get("plate_index"),
        "preset_digest": str((pf or {}).get("digest") or ""),
    }
    with state._lock:
        state._slice_host_fingerprint = fp
        state._last_invalidation_reason = ""
        state._auto_slice_in_progress = False


def check_and_invalidate_missing_preview_slice(
    state: Optional[PluginState] = None, force_reason: str = ""
) -> bool:
    """
    Reset active slice state (and tombstone staged sidecars so the old slice cannot be
    resurrected from `staged_gcode/`) if the active G-code came from OrcaSlicer's
    slicer/preview workflow (NOT manual `Browse .gcode` / `file_picker`) and its file,
    preview temp source, or OrcaSlicer host slice status is no longer valid. Returns True if invalidated.
    """
    st = state or GLOBAL_STATE
    with st._lock:
        cur_path = str(st.last_gcode_path or "").strip()
        cur_src = str(st.last_gcode_source or "").strip()
        prev_temp = str(getattr(st, "_last_preview_temp_path", "") or "").strip()
        prev_existed = bool(getattr(st, "_last_preview_temp_existed", False))
        cur_prov = dict(st._last_staged_provenance) if isinstance(st._last_staged_provenance, dict) else None
    if not cur_path or cur_src == "file_picker":
        return False

    disk_sc = cur_prov or (_read_provenance_sidecar(cur_path) if os.path.isfile(cur_path) else {})
    if not prev_temp and isinstance(disk_sc, dict):
        prev_temp = str(disk_sc.get("preview_temp_path") or "").strip()
        prev_existed = bool(disk_sc.get("preview_temp_existed", False))

    reason = str(force_reason or "")
    if reason:
        pass
    elif not os.path.isfile(cur_path):
        reason = "missing_active_gcode"
    elif prev_temp and prev_existed and not os.path.isfile(prev_temp):
        reason = "missing_preview_temp_file"
    else:
        model_info = get_orca_active_model_info(state=st)
        snap_now = st.refresh_host_ui_snapshot() if not _is_worker_thread_context() else st.get_host_ui_snapshot()
        cur_preset_digest = str(((snap_now.get("preset_fingerprint") or {}).get("digest")) or "")
        with st._lock:
            fp = dict(getattr(st, "_slice_host_fingerprint", {}) or {})
        cur_model = _norm_fp_name(model_info.get("display_name"))
        cur_plate = model_info.get("plate_index")
        if fp.get("model") and cur_model and fp["model"] != cur_model:
            reason = "host_model_changed"
        elif fp.get("plate_index") is not None and cur_plate is not None and fp["plate_index"] != cur_plate:
            reason = "host_plate_changed"
        elif fp.get("preset_digest") and cur_preset_digest and fp["preset_digest"] != cur_preset_digest:
            reason = "host_presets_changed"
        elif model_info.get("host_has_slice") is False:
            reason = "host_unsliced"
        elif bool(model_info.get("host_gcode_attr_present")):
            host_gcode = str(model_info.get("host_gcode_path") or "").strip()
            if not host_gcode:
                reason = "host_missing_preview_gcode_path"
            elif not os.path.isfile(host_gcode):
                reason = "host_missing_preview_gcode_file"

    if not reason:
        ok_state, val_reason = _validate_restorable_gcode_state(
            st,
            cur_path,
            provenance=cur_prov,
            source_hint=cur_src,
        )
        if not ok_state:
            reason = val_reason or "invalidated_preview_slice"

    if not reason:
        return False

    storage_dir = st.resolve_storage_dir()
    staged_dir = os.path.join(storage_dir, "staged_gcode") if storage_dir else ""
    if cur_path and staged_dir and _is_inside_dir(cur_path, staged_dir) and os.path.isfile(cur_path):
        try:
            cur_sc = _read_provenance_sidecar(cur_path) or {
                "staged_path": cur_path,
                "filename": os.path.basename(cur_path),
                "output_name": os.path.basename(cur_path),
                "source_mtime_ns": _get_file_mtime_ns(cur_path),
            }
            if not cur_sc.get("is_cleared"):
                cur_sc["is_cleared"] = True
                cur_sc["is_stale"] = True
                cur_sc["cleared_ns"] = time.time_ns()
                cur_sc["cleared_reason"] = f"preview_invalidated:{reason}"
                _write_provenance_sidecar_atomic(cur_path, cur_sc)
        except Exception:
            pass

    prev_cleared = _clear_active_job_fields(st)
    with st._lock:
        st._last_invalidation_reason = str(reason or "invalidated")
        if reason != "auto_reslice_started":
            st._auto_slice_in_progress = False
        st.tool_map = {i: i for i in range(MAX_ACE_SLOTS)}
        st.tool_map_sources = {i: "default" for i in range(MAX_ACE_SLOTS)}
    host_fils = sync_filaments_from_orca_host(st, preserve_gcode_usage=False)
    if not host_fils:
        with st._lock:
            st.slicer_filaments = []
            st.filament_signature = ""
    st.save_config()
    _record_diagnostic_load_event(
        st,
        trigger="preview_invalidation",
        decision="cleared",
        reason=reason,
        file_source=cur_src or "slicer_preview",
        file_path=prev_cleared,
        compute_sha=False,
    )
    return True


def _read_host_filaments_from_preset_bundle() -> List[Dict[str, Any]]:
    """
    Read active filament presets directly from OrcaSlicer `orca.host.preset_bundle()` on the
    UI/Main thread. Never mutates `PluginState` directly.
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
        return filaments
    except Exception:
        return []


def sync_filaments_from_orca_host(
    state: Optional[PluginState] = None,
    preserve_gcode_usage: bool = False,
) -> List[Dict[str, Any]]:
    """
    Sync active filament presets into `state.slicer_filaments`.
    Reads `orca.host.preset_bundle()` ONLY on the UI/Main thread; inside background worker
    threads uses the cached UI snapshot cleanly without invoking non-thread-safe host APIs.
    """
    st = state or GLOBAL_STATE
    try:
        if _is_worker_thread_context():
            snap = st.get_host_ui_snapshot()
            filaments = [dict(f) for f in (snap.get("filaments") or [])] if snap.get("filaments_captured") else []
        else:
            filaments = _read_host_filaments_from_preset_bundle()
            if filaments:
                with st._lock:
                    st._host_ui_snapshot["filaments"] = [dict(f) for f in filaments]
                    st._host_ui_snapshot["filaments_captured"] = True
                    st._host_ui_snapshot["captured_ns"] = time.time_ns()

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


# 11. OrcaSlicer Slicing-Pipeline Registration & Stale-Model Guard
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
    preserving every existing entry in order (and migrating legacy KobraS1/vKobraACE pipeline names),
    or `None` if already present with no legacy entries remaining.
    """
    entries = parse_slicer_pipeline_entries(current_value)
    target_matches = [entry for entry in entries if _pipeline_entry_matches(entry, capability_name)]
    has_target = bool(target_matches)
    has_dup_target = len(target_matches) > 1
    legacy_names = LEGACY_PIPELINE_CAPABILITY_NAMES if capability_name == PIPELINE_CAPABILITY_NAME else ()
    has_legacy = bool(legacy_names) and any(
        any(_pipeline_entry_matches(entry, leg) for leg in legacy_names)
        for entry in entries
    )
    if has_target and not has_legacy and not has_dup_target:
        return None
    if has_legacy or has_dup_target:
        filtered: List[str] = []
        seen_target = False
        for entry in entries:
            if legacy_names and any(_pipeline_entry_matches(entry, leg) for leg in legacy_names):
                continue
            if _pipeline_entry_matches(entry, capability_name):
                if seen_target:
                    continue
                seen_target = True
            filtered.append(entry)
        if not seen_target:
            filtered.append(capability_name)
        return serialize_slicer_pipeline_entries(filtered)
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
        current_raw = _safe_preset_config_value(bundle, "full_config_value", "slicing_pipeline_plugin")
        if current_raw is None or current_raw == "":
            proc_raw = _safe_preset_config_value(process, "config_value", "slicing_pipeline_plugin")
            if proc_raw is not None and proc_raw != "":
                current_raw = proc_raw

        legacy_names = LEGACY_PIPELINE_CAPABILITY_NAMES if capability_name == PIPELINE_CAPABILITY_NAME else ()
        current_entries = parse_slicer_pipeline_entries(current_raw)
        target_count = sum(1 for entry in current_entries if _pipeline_entry_matches(entry, capability_name))
        has_legacy_entry = bool(legacy_names) and any(
            any(_pipeline_entry_matches(entry, leg) for leg in legacy_names)
            for entry in current_entries
        )

        with st._lock:
            if session_key in st._pipeline_registered_keys and not has_legacy_entry and target_count <= 1:
                return {"status": "already_checked", "printer": printer_name, "process": process_name}

        new_value = build_slicing_pipeline_value(current_raw, capability_name=capability_name)

        if new_value is None:
            with st._lock:
                st._pipeline_registered_keys.add(session_key)
                st._pipeline_last_error = ""
            _record_diagnostic_load_event(
                st,
                trigger="pipeline_registration",
                decision="already_present",
                reason="pipeline_already_registered",
                file_source="preset_tx",
                file_path=f"{printer_name}::{process_name}",
                extra={"printer": printer_name, "process": process_name},
            )
            return {"status": "already_present", "printer": printer_name, "process": process_name}

        tx = None
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
                _record_diagnostic_load_event(
                    st,
                    trigger="pipeline_registration",
                    decision="aborted",
                    reason="preset_switched_before_commit",
                    file_source="preset_tx",
                    file_path=f"{printer_name}::{process_name}",
                    extra={"printer": printer_name, "process": process_name},
                )
                return {"status": "aborted_changed", "printer": printer_name, "process": process_name}

            tx.set("slicing_pipeline_plugin", new_value)
            tx.commit()
            with st._lock:
                st._pipeline_registered_keys.add(session_key)
                st._pipeline_last_error = ""
            _record_diagnostic_load_event(
                st,
                trigger="pipeline_registration",
                decision="updated",
                reason="pipeline_tx_committed",
                file_source="preset_tx",
                file_path=f"{printer_name}::{process_name}",
                extra={"printer": printer_name, "process": process_name, "value": new_value},
            )
            return {
                "status": "updated",
                "printer": printer_name,
                "process": process_name,
                "value": new_value,
            }
        except Exception as exc:
            if tx is not None:
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
                st._pipeline_retry_count = int(getattr(st, "_pipeline_retry_count", 0) or 0) + 1
                st._pipeline_last_error = str(exc)
                retry_n = st._pipeline_retry_count
            _record_diagnostic_load_event(
                st,
                trigger="pipeline_registration",
                decision="error",
                reason=f"pipeline_tx_error:{exc}",
                file_source="preset_tx",
                file_path=f"{printer_name}::{process_name}",
                extra={"retry_count": retry_n, "printer": printer_name, "process": process_name},
            )
            return {
                "status": "error",
                "printer": printer_name,
                "process": process_name,
                "error": str(exc),
                "retry_count": retry_n,
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
        st_eff = state or GLOBAL_STATE
        if check_and_invalidate_missing_preview_slice(st_eff):
            with _ACTIVE_PAGE_LOCK:
                page = _ACTIVE_PAGE_CAPABILITY
            if page is not None:
                try:
                    inv_reason = str(getattr(st_eff, "_last_invalidation_reason", "") or "")
                    page.post_message({
                        "action": "close_preprint_modal",
                        "action_reason": "slice_invalidated",
                        "notice": {
                            "level": "warn",
                            "text": _build_invalidation_notice_text(inv_reason, st_eff.language),
                        },
                        "state": st_eff.to_dict(),
                        "log": t("host.slice_invalidated_presets", lang=st_eff.language),
                    })
                except Exception:
                    pass
        return True

    if event is not None and event in tuple(e for e in (project_evt, new_proj_evt, device_evt) if e is not None):
        if event in tuple(e for e in (project_evt, new_proj_evt) if e is not None):
            st_eff_proj = state or GLOBAL_STATE
            with st_eff_proj._lock:
                st_eff_proj._pipeline_registered_keys.clear()
        _schedule_pipeline_registration(state=state, use_thread=use_thread)
        if new_proj_evt is not None and event == new_proj_evt:
            st_eff = state or GLOBAL_STATE
            with st_eff._lock:
                had_preview = bool(st_eff.last_gcode_path and str(st_eff.last_gcode_source or "") != "file_picker")
            if had_preview:
                _clear_active_job_fields(st_eff)
                sync_filaments_from_orca_host(st_eff, preserve_gcode_usage=False)
                st_eff.save_config()
                with _ACTIVE_PAGE_LOCK:
                    page = _ACTIVE_PAGE_CAPABILITY
                if page is not None:
                    try:
                        page.post_message({
                            "action": "close_preprint_modal",
                            "action_reason": "slice_invalidated",
                            "state": st_eff.to_dict(),
                            "log": t("host.slice_invalidated", lang=st_eff.language),
                        })
                    except Exception:
                        pass
        return True

    return False


_GENERIC_MODEL_STEMS = frozenset({"", "untitled", "unnamed", "none", "default", "empty", "plate", "sliced_plate"})


_STALE_PLATE_SUFFIX_RE = re.compile(r"(?:[_\-\s]plate[_\-\s]*\d+|_p\d+)$", re.IGNORECASE)


def _normalize_model_name_for_stale_check(raw_name: str) -> str:
    """
    Normalize a model path, `.gcode`/`.3mf`/`.stl` filename (including compound suffixes),
    or plate-suffixed name (`_plate_1`, `_plate2`, `-plate-1`, `_p1`) for case-insensitive
    stale comparison.
    """
    stem = _strip_model_extension(raw_name).lower()
    if not stem:
        return ""
    cleaned = _STALE_PLATE_SUFFIX_RE.sub("", stem).strip(" _-")
    return cleaned or stem


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


def _extract_host_plate_index(obj: Any, depth: int = 0) -> Optional[int]:
    if obj is None or depth > 2:
        return None
    if isinstance(obj, dict):
        for key in ("plate_index", "active_plate_index", "current_plate_index", "plate_idx", "index"):
            idx = _normalize_plate_index(obj.get(key))
            if idx is not None:
                return idx
        p_name = obj.get("plate_name")
        if isinstance(p_name, str):
            idx = _extract_plate_index_from_name(p_name)
            if idx is not None:
                return idx
        for subkey in ("active_plate", "current_plate", "plate"):
            if obj.get(subkey) is not None:
                sub = _extract_host_plate_index(obj.get(subkey), depth + 1)
                if sub is not None:
                    return sub
        return None

    for attr in ("plate_index", "active_plate_index", "current_plate_index", "plate_idx"):
        try:
            raw = getattr(obj, attr, None)
            if callable(raw):
                raw = raw()
            idx = _normalize_plate_index(raw)
            if idx is not None:
                return idx
        except Exception:
            continue
    for subattr in ("plate_name", "active_plate", "current_plate", "plate"):
        try:
            sub_val = getattr(obj, subattr, None)
            if callable(sub_val):
                sub_val = sub_val()
            if isinstance(sub_val, str):
                idx = _extract_plate_index_from_name(sub_val)
                if idx is not None:
                    return idx
            elif sub_val is not None:
                idx = _extract_host_plate_index(sub_val, depth + 1)
                if idx is not None:
                    return idx
        except Exception:
            continue
    return None


def _extract_host_slice_status(obj: Any, depth: int = 0) -> Dict[str, Any]:
    """
    Inspect `orca.host.plater()`, `model()`, or active plate objects (dict or Python/pybind11
    instance) for slice readiness booleans and preview temp G-code paths without calling
    undocumented scanning APIs or fabricating mock attributes.
    """
    empty: Dict[str, Any] = {"has_slice": None, "gcode_path": "", "gcode_attr_present": False}
    if obj is None or depth > 2:
        return empty

    bool_keys = (
        "is_sliced",
        "has_gcode",
        "gcode_ready",
        "is_slice_valid",
        "has_sliced_gcode",
        "sliced",
        "has_valid_gcode",
        "is_gcode_ready",
        "slice_ready",
    )
    path_keys = (
        "gcode_path",
        "get_gcode_path",
        "gcode_file",
        "get_gcode_file",
        "tmp_gcode_path",
        "get_tmp_gcode_path",
        "sliced_gcode_path",
        "get_sliced_gcode_path",
        "plate_gcode",
        "preview_gcode_path",
    )

    saw_true = False
    saw_false = False
    found_gcode_path = ""
    attr_present = False

    for b_key in bool_keys:
        if not _has_real_attr(obj, b_key):
            continue
        try:
            val = obj.get(b_key) if isinstance(obj, dict) else getattr(obj, b_key)
            if callable(val):
                val = val()
            if isinstance(val, bool):
                if val:
                    saw_true = True
                else:
                    saw_false = True
        except Exception:
            continue

    for p_key in path_keys:
        if not _has_real_attr(obj, p_key):
            continue
        try:
            val = obj.get(p_key) if isinstance(obj, dict) else getattr(obj, p_key)
            if callable(val):
                val = val()
            if val is None or isinstance(val, (str, os.PathLike)):
                attr_present = True
                s_val = str(os.fspath(val) if isinstance(val, os.PathLike) else (val or "")).strip()
                if s_val and not found_gcode_path:
                    found_gcode_path = s_val
        except Exception:
            continue

    for subkey in ("active_plate", "current_plate", "plate"):
        if not _has_real_attr(obj, subkey):
            continue
        try:
            sub_val = obj.get(subkey) if isinstance(obj, dict) else getattr(obj, subkey)
            if callable(sub_val):
                sub_val = sub_val()
            if sub_val is not None:
                sub_res = _extract_host_slice_status(sub_val, depth + 1)
                if sub_res["has_slice"] is False:
                    saw_false = True
                elif sub_res["has_slice"] is True:
                    saw_true = True
                if sub_res["gcode_attr_present"]:
                    attr_present = True
                if sub_res["gcode_path"] and not found_gcode_path:
                    found_gcode_path = sub_res["gcode_path"]
        except Exception:
            continue

    has_slice: Optional[bool] = False if saw_false else (True if saw_true else None)
    return {
        "has_slice": has_slice,
        "gcode_path": found_gcode_path,
        "gcode_attr_present": attr_present,
    }


def _query_orca_active_model_info_direct() -> Dict[str, Any]:
    """Query `orca.host.plater()` and `orca.host.model()` directly on the UI/Main thread."""
    host = getattr(orca, "host", None)
    if host is None:
        return {
            "display_name": "",
            "candidates": [],
            "plate_index": None,
            "host_has_slice": None,
            "host_gcode_path": "",
            "host_gcode_attr_present": False,
        }

    raw_candidates: List[str] = []
    plate_idx: Optional[int] = None
    saw_slice_true = False
    saw_slice_false = False
    host_gcode_path = ""
    host_gcode_attr_present = False
    for method_name in ("plater", "model"):
        try:
            accessor = getattr(host, method_name, None)
            if accessor is None:
                continue
            obj = accessor() if callable(accessor) else accessor
            raw_candidates.extend(_extract_host_model_candidates(obj, depth=0))
            if plate_idx is None:
                plate_idx = _extract_host_plate_index(obj, depth=0)
            sl_res = _extract_host_slice_status(obj, depth=0)
            if sl_res["has_slice"] is False:
                saw_slice_false = True
            elif sl_res["has_slice"] is True:
                saw_slice_true = True
            if sl_res["gcode_attr_present"]:
                host_gcode_attr_present = True
            if sl_res["gcode_path"] and not host_gcode_path:
                host_gcode_path = sl_res["gcode_path"]
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
        "plate_index": plate_idx,
        "host_has_slice": False if saw_slice_false else (True if saw_slice_true else None),
        "host_gcode_path": host_gcode_path,
        "host_gcode_attr_present": host_gcode_attr_present,
    }


def _has_live_host_model_api() -> bool:
    """Return True when OrcaSlicer runtime or a mocked host plater/model provider is active."""
    if HAS_ORCA_RUNTIME:
        return True
    host_obj = getattr(orca, "host", None)
    if host_obj is None:
        return False
    dummy_cls = globals().get("_DummyHostModule")
    if dummy_cls is None or not isinstance(host_obj, dummy_cls):
        return True
    return (
        getattr(host_obj, "plater", None) is not dummy_cls.plater
        or getattr(host_obj, "model", None) is not dummy_cls.model
    )


def get_orca_active_model_info(state: Optional[PluginState] = None) -> Dict[str, Any]:
    """
    Return active model/plate stems safely: uses UI snapshot inside worker threads without
    calling `orca.host.plater()` or `orca.host.model()`, and queries directly on UI/Main thread.
    """
    st = state or GLOBAL_STATE
    if _is_worker_thread_context():
        snap = st.get_host_ui_snapshot()
        am = snap.get("active_model")
        if isinstance(am, dict):
            return {
                "display_name": str(am.get("display_name") or ""),
                "candidates": list(am.get("candidates") or []),
                "plate_index": am.get("plate_index"),
                "host_has_slice": am.get("host_has_slice"),
                "host_gcode_path": str(am.get("host_gcode_path") or ""),
                "host_gcode_attr_present": bool(am.get("host_gcode_attr_present", False)),
            }
        return {
            "display_name": "",
            "candidates": [],
            "plate_index": None,
            "host_has_slice": None,
            "host_gcode_path": "",
            "host_gcode_attr_present": False,
        }

    info = _query_orca_active_model_info_direct()
    with st._lock:
        if (
            info.get("display_name")
            or info.get("candidates")
            or info.get("host_has_slice") is not None
            or info.get("host_gcode_attr_present")
            or _has_live_host_model_api()
        ):
            st._host_ui_snapshot["active_model"] = {
                "display_name": str(info.get("display_name") or ""),
                "candidates": list(info.get("candidates") or []),
                "plate_index": info.get("plate_index"),
                "host_has_slice": info.get("host_has_slice"),
                "host_gcode_path": str(info.get("host_gcode_path") or ""),
                "host_gcode_attr_present": bool(info.get("host_gcode_attr_present", False)),
            }
            st._host_ui_snapshot["captured_ns"] = time.time_ns()
            return info
        existing_am = st._host_ui_snapshot.get("active_model")
        if isinstance(existing_am, dict) and (existing_am.get("display_name") or existing_am.get("candidates")):
            return {
                "display_name": str(existing_am.get("display_name") or ""),
                "candidates": list(existing_am.get("candidates") or []),
                "plate_index": existing_am.get("plate_index"),
                "host_has_slice": existing_am.get("host_has_slice"),
                "host_gcode_path": str(existing_am.get("host_gcode_path") or ""),
                "host_gcode_attr_present": bool(existing_am.get("host_gcode_attr_present", False)),
            }
    return info


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


def _human_stale_reason(reason_code: str, lang: str = DEFAULT_LANGUAGE) -> str:
    if not reason_code:
        return ""
    key = f"stale.reason.{reason_code}"
    if key in TRANSLATIONS.get(normalize_language(lang), {}):
        return t(key, lang=lang)
    return reason_code


def check_stale_gcode_model(
    gcode_path: str = "",
    filename: str = "",
    state: Optional[PluginState] = None,
    job_meta: Optional[Dict[str, Any]] = None,
    slicer_filaments: Optional[List[Dict[str, Any]]] = None,
    output_name: str = "",
    plate_index: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Multi-signal project/slice freshness verification combining:
    1. Active OrcaSlicer project/model & plate index vs loaded filename, `output_name`, and sidecar.
    2. Captured UI filament preset signature vs loaded G-code used filaments.
    3. Slicing completion freshness & provenance sidecar staleness markers.
    """
    st = state or GLOBAL_STATE
    storage_dir = st.resolve_storage_dir()
    sidecar: Dict[str, Any] = {}
    if gcode_path and not _is_system_temp_path(gcode_path, storage_dir):
        sidecar = _read_provenance_sidecar(gcode_path)

    loaded_file = str(
        output_name
        or filename
        or (job_meta or {}).get("output_name")
        or (sidecar.get("output_name") if sidecar else "")
        or (os.path.basename(gcode_path) if gcode_path else "")
        or ""
    ).strip()

    model_info = get_orca_active_model_info(state=st if state is not None else None)
    candidates = model_info.get("candidates") or []
    active_model = str(model_info.get("display_name") or "")
    active_plate = model_info.get("plate_index")

    ui_snap = st.get_host_ui_snapshot() if state is not None else {"filaments": [], "filaments_captured": False}
    if state is not None:
        with st._lock:
            last_slice_ns = int(getattr(st, "_last_slice_completed_ns", 0) or 0)
            satisfied_run_id = str(getattr(st, "_last_slice_satisfied_run_id", "") or "")
            satisfied_created_ns = int(getattr(st, "_last_slice_satisfied_created_ns", 0) or 0)
    else:
        last_slice_ns = 0
        satisfied_run_id = ""
        satisfied_created_ns = 0

    gcode_plate = _normalize_plate_index(
        plate_index
        if plate_index is not None
        else (sidecar.get("plate_index") if sidecar else None)
        or (job_meta or {}).get("plate_index")
        or _extract_plate_index_from_name(loaded_file)
    )

    reasons: List[str] = []
    uncertain = False

    if loaded_file:
        # Signal 3a: explicit sidecar stale/superseded mark
        if sidecar and (sidecar.get("is_stale") or sidecar.get("is_superseded")):
            reasons.append("provenance_stale")

        # Signal 3b: a newer SlicingJobComplete finished after this staged file was created
        sidecar_run_id = str(sidecar.get("slicing_run_id") or "") if sidecar else ""
        loaded_ns = int(sidecar.get("created_ns", 0) or 0) if sidecar else 0
        satisfied_by_slice = (
            (bool(satisfied_run_id) and bool(sidecar_run_id) and sidecar_run_id == satisfied_run_id)
            or (satisfied_created_ns > 0 and loaded_ns > 0 and loaded_ns == satisfied_created_ns)
        )
        if last_slice_ns > 0 and not satisfied_by_slice:
            if loaded_ns <= 0 and gcode_path:
                if _is_system_temp_path(gcode_path, storage_dir):
                    reasons.append("slice_newer_than_export")
                elif os.path.isfile(gcode_path):
                    try:
                        st_f = os.stat(gcode_path)
                        loaded_ns = int(getattr(st_f, "st_mtime_ns", int(float(st_f.st_mtime) * 1_000_000_000)))
                    except OSError:
                        loaded_ns = 0
            if 0 < loaded_ns < last_slice_ns:
                reasons.append("slice_newer_than_export")

        # Signal 1: active project/model and plate index match
        if candidates:
            sidecar_model = str(sidecar.get("project_model") or "").strip() if sidecar else ""
            gcode_stem = _normalize_model_name_for_stale_check(loaded_file)
            norm_candidates = [
                _normalize_model_name_for_stale_check(cand) or str(cand or "").strip()
                for cand in candidates
            ]
            sidecar_norm = _normalize_model_name_for_stale_check(sidecar_model)
            stem_matches = any(_model_stem_matches_gcode(cand, gcode_stem) for cand in norm_candidates)
            sidecar_model_ok = (
                bool(sidecar_norm)
                and not _is_generic_model_stem(sidecar_norm)
                and any(_model_stem_matches_gcode(cand, sidecar_norm) for cand in norm_candidates)
            )
            sidecar_model_conflict = (
                bool(sidecar_norm)
                and not _is_generic_model_stem(sidecar_norm)
                and not sidecar_model_ok
            )
            if sidecar_model_conflict:
                reasons.append("model_mismatch")
            elif _is_generic_model_stem(gcode_stem):
                if not sidecar_model_ok:
                    uncertain = True
            elif not stem_matches and not sidecar_model_ok:
                reasons.append("model_mismatch")

            if (
                active_plate is not None
                and gcode_plate is not None
                and active_plate != gcode_plate
            ):
                reasons.append("plate_mismatch")

        # Signal 2: host preset filament signature vs G-code used filaments
        if ui_snap.get("filaments_captured"):
            host_fils = [f for f in (ui_snap.get("filaments") or []) if isinstance(f, dict)]
            gcode_fils = list(slicer_filaments or (sidecar.get("used_filaments") if sidecar else []) or [])
            if host_fils and gcode_fils:
                host_by_tool = {int(f.get("slicer_tool", i)): f for i, f in enumerate(host_fils)}
                for gf in gcode_fils:
                    if not isinstance(gf, dict) or not gf.get("used", True):
                        continue
                    t_idx = int(gf.get("slicer_tool", 0))
                    g_mat = str(gf.get("material") or "").strip().upper()
                    hf = host_by_tool.get(t_idx)
                    if hf is None:
                        reasons.append("filament_mismatch")
                        break
                    h_mat = str(hf.get("material") or "").strip().upper()
                    if g_mat and h_mat and g_mat != h_mat:
                        reasons.append("filament_mismatch")
                        break

    primary_reason = reasons[0] if reasons else ""
    is_stale = bool(reasons)
    confidence = "stale" if is_stale else ("uncertain" if uncertain else ("verified" if loaded_file else "none"))

    return {
        "is_stale": is_stale,
        "uncertain": uncertain,
        "confidence": confidence,
        "active_model": active_model,
        "active_plate_index": active_plate,
        "loaded_file": loaded_file,
        "gcode_plate_index": gcode_plate,
        "reason": primary_reason,
        "reasons": reasons,
        "signals": {
            "model_candidates": list(candidates),
            "plate_match": "plate_mismatch" not in reasons,
            "filament_match": "filament_mismatch" not in reasons,
            "slice_fresh": "slice_newer_than_export" not in reasons and "provenance_stale" not in reasons,
        },
    }


# 12. Embedded HTML / CSS / JS UI (OrcaSlicer 2.5.x Page Capability)
ACE_PLUGIN_HTML_UI = """<!DOCTYPE html>
<html lang="en" data-theme="auto" data-resolved="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Kobra-Van-Kenorca</title>
<link rel="icon" type="image/png" href="__KOBRA_ORCA_LOGO_URI__" />
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
--surface-panel: #1e2120;
--bg-deep: #1e2120;
--surface: #2f3332;
--surface-2: #262928;
--surface-3: #383d3b;
--input-bg: #383d3b;
--text: var(--fg);
--badge-ok-bg: rgba(0, 150, 136, 0.16);
--badge-ok-fg: #00a696;
--badge-ok-border: rgba(0, 150, 136, 0.36);
--tooltip-bg: rgba(30, 33, 32, 0.98);
--tooltip-fg: #f3f5f4;
--tooltip-shadow: 0 8px 22px rgba(0, 0, 0, 0.58);
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
--surface-panel: #ffffff;
--bg-deep: #eef0f2;
--surface: #ffffff;
--surface-2: #f6f7f8;
--surface-3: #eef0f2;
--input-bg: #ffffff;
--text: var(--fg);
--badge-ok-bg: rgba(0, 150, 136, 0.12);
--badge-ok-fg: #00796b;
--badge-ok-border: rgba(0, 150, 136, 0.32);
--tooltip-bg: rgba(255, 255, 255, 0.98);
--tooltip-fg: #1f2423;
--tooltip-shadow: 0 8px 22px rgba(15, 23, 42, 0.22);
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
--surface-panel: #1e2120;
--bg-deep: #1e2120;
--surface: #2f3332;
--surface-2: #262928;
--surface-3: #383d3b;
--input-bg: #383d3b;
--text: var(--fg);
--badge-ok-bg: rgba(0, 150, 136, 0.16);
--badge-ok-fg: #00a696;
--badge-ok-border: rgba(0, 150, 136, 0.36);
--tooltip-bg: rgba(30, 33, 32, 0.98);
--tooltip-fg: #f3f5f4;
--tooltip-shadow: 0 8px 22px rgba(0, 0, 0, 0.58);
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
--surface-panel: #ffffff;
--bg-deep: #eef0f2;
--surface: #ffffff;
--surface-2: #f6f7f8;
--surface-3: #eef0f2;
--input-bg: #ffffff;
--text: var(--fg);
--badge-ok-bg: rgba(0, 150, 136, 0.12);
--badge-ok-fg: #00796b;
--badge-ok-border: rgba(0, 150, 136, 0.32);
--tooltip-bg: rgba(255, 255, 255, 0.98);
--tooltip-fg: #1f2423;
--tooltip-shadow: 0 8px 22px rgba(15, 23, 42, 0.22);
--log-bg: #f6f7f8;
--ok: #00796b;
--ok-bg: rgba(0, 150, 136, 0.12);
--info: #00796b;
--info-bg: rgba(0, 150, 136, 0.12);
--accent-soft: rgba(0, 150, 136, 0.12);
}
html[data-scheme="teal"] {
--accent: #009688;
--accent-hover: #00a696;
--accent-active: #00796b;
--accent-fg: #ffffff;
--accent-soft: rgba(0, 150, 136, 0.16);
--info: #4db6ac;
--info-bg: rgba(0, 150, 136, 0.16);
}
html[data-resolved="light"][data-scheme="teal"] {
--accent: #00796b;
--accent-hover: #00897b;
--accent-active: #00695c;
--accent-fg: #ffffff;
--accent-soft: rgba(0, 121, 107, 0.12);
--info: #00796b;
--info-bg: rgba(0, 121, 107, 0.12);
}
html[data-scheme="ocean"] {
--accent: #2563eb;
--accent-hover: #3b82f6;
--accent-active: #1d4ed8;
--accent-fg: #ffffff;
--accent-soft: rgba(37, 99, 235, 0.18);
--info: #60a5fa;
--info-bg: rgba(37, 99, 235, 0.18);
}
html[data-resolved="light"][data-scheme="ocean"] {
--accent: #1d4ed8;
--accent-hover: #2563eb;
--accent-active: #1e40af;
--accent-fg: #ffffff;
--accent-soft: rgba(29, 78, 216, 0.12);
--info: #1d4ed8;
--info-bg: rgba(29, 78, 216, 0.12);
}
html[data-scheme="amber"] {
--accent: #d97706;
--accent-hover: #f59e0b;
--accent-active: #b45309;
--accent-fg: #111827;
--accent-soft: rgba(217, 119, 6, 0.18);
--info: #fbbf24;
--info-bg: rgba(217, 119, 6, 0.18);
}
html[data-resolved="light"][data-scheme="amber"] {
--accent: #b45309;
--accent-hover: #d97706;
--accent-active: #92400e;
--accent-fg: #ffffff;
--accent-soft: rgba(180, 83, 9, 0.12);
--info: #b45309;
--info-bg: rgba(180, 83, 9, 0.12);
}
html[data-scheme="coral"] {
--accent: #e11d48;
--accent-hover: #f43f5e;
--accent-active: #be123c;
--accent-fg: #ffffff;
--accent-soft: rgba(225, 29, 72, 0.18);
--info: #fb7185;
--info-bg: rgba(225, 29, 72, 0.18);
}
html[data-resolved="light"][data-scheme="coral"] {
--accent: #be123c;
--accent-hover: #e11d48;
--accent-active: #9f1239;
--accent-fg: #ffffff;
--accent-soft: rgba(190, 18, 60, 0.12);
--info: #be123c;
--info-bg: rgba(190, 18, 60, 0.12);
}
html[data-scheme="forest"] {
--accent: #16a34a;
--accent-hover: #22c55e;
--accent-active: #15803d;
--accent-fg: #ffffff;
--accent-soft: rgba(22, 163, 74, 0.18);
--info: #4ade80;
--info-bg: rgba(22, 163, 74, 0.18);
}
html[data-resolved="light"][data-scheme="forest"] {
--accent: #15803d;
--accent-hover: #16a34a;
--accent-active: #166534;
--accent-fg: #ffffff;
--accent-soft: rgba(21, 128, 61, 0.12);
--info: #15803d;
--info-bg: rgba(21, 128, 61, 0.12);
}
html[data-scheme="violet"] {
--accent: #7c3aed;
--accent-hover: #8b5cf6;
--accent-active: #6d28d9;
--accent-fg: #ffffff;
--accent-soft: rgba(124, 58, 237, 0.18);
--info: #a78bfa;
--info-bg: rgba(124, 58, 237, 0.18);
}
html[data-resolved="light"][data-scheme="violet"] {
--accent: #6d28d9;
--accent-hover: #7c3aed;
--accent-active: #5b21b6;
--accent-fg: #ffffff;
--accent-soft: rgba(109, 40, 217, 0.12);
--info: #6d28d9;
--info-bg: rgba(109, 40, 217, 0.12);
}
html[data-scheme="cyan"] {
--accent: #0891b2;
--accent-hover: #06b6d4;
--accent-active: #0e7490;
--accent-fg: #ffffff;
--accent-soft: rgba(8, 145, 178, 0.18);
--info: #22d3ee;
--info-bg: rgba(8, 145, 178, 0.18);
}
html[data-resolved="light"][data-scheme="cyan"] {
--accent: #0e7490;
--accent-hover: #0891b2;
--accent-active: #155e75;
--accent-fg: #ffffff;
--accent-soft: rgba(14, 116, 144, 0.12);
--info: #0e7490;
--info-bg: rgba(14, 116, 144, 0.12);
}
html[data-scheme="graphite"] {
--accent: #64748b;
--accent-hover: #94a3b8;
--accent-active: #475569;
--accent-fg: #ffffff;
--accent-soft: rgba(148, 163, 184, 0.18);
--info: #cbd5e1;
--info-bg: rgba(148, 163, 184, 0.18);
}
html[data-resolved="light"][data-scheme="graphite"] {
--accent: #334155;
--accent-hover: #475569;
--accent-active: #1e293b;
--accent-fg: #ffffff;
--accent-soft: rgba(51, 65, 85, 0.12);
--info: #334155;
--info-bg: rgba(51, 65, 85, 0.12);
}
html[data-theme="kobraorca"],
html[data-theme="kobraorca"][data-scheme] {
--bg: #0b1116;
--bg-deep: #0e151b;
--surface: #131d24;
--surface-card: #131d24;
--surface-2: #131d24;
--surface-sub: #18242d;
--surface-3: #1e2e39;
--surface-hover: #1e2e39;
--surface-input: #0e151b;
--input-bg: #0e151b;
--surface-panel: #0e151b;
--log-bg: #0b1116;
--border: #233542;
--fg: #eef5f8;
--text: #eef5f8;
--muted: #8fa6b5;
--accent: #18b6c9;
--accent-hover: #0ea5b7;
--accent-active: #22d3ee;
--accent-fg: #061318;
--accent-soft: rgba(24, 182, 201, 0.16);
--ok: #22d3ee;
--ok-bg: rgba(24, 182, 201, 0.16);
--badge-ok-bg: rgba(24, 182, 201, 0.16);
--badge-ok-fg: #22d3ee;
--badge-ok-border: rgba(34, 211, 238, 0.42);
--info: #22d3ee;
--info-bg: rgba(24, 182, 201, 0.16);
--warn: #e5b54f;
--warn-bg: rgba(229, 181, 79, 0.16);
--warn-fg: #f0c96b;
--danger: #f87171;
--danger-bg: rgba(248, 113, 113, 0.16);
--danger-fg: #fecaca;
--tooltip-bg: rgba(11, 17, 22, 0.98);
--tooltip-fg: #eef5f8;
--tooltip-shadow: 0 10px 26px rgba(0, 0, 0, 0.72);
}
html[data-theme="kobraorca"] input[type="checkbox"]:checked::after {
border-right-color: #061318;
border-bottom-color: #061318;
}
html[data-theme="kobraorca"] .btn-cta-send .btn-spinner {
border-color: rgba(6, 19, 24, 0.28);
border-top-color: #061318;
}
html[data-theme="kobraorca"] .badge.ready,
html[data-theme="kobraorca"] .badge.ok {
background: var(--ok-bg);
color: var(--ok);
border-color: rgba(34, 211, 238, 0.36);
}
html[data-theme="kobraorca"] .badge.drying,
html[data-theme="kobraorca"] .badge.warn {
background: rgba(229, 181, 79, 0.16);
color: #f0c96b;
border: 1px solid rgba(229, 181, 79, 0.36);
}
html[data-theme="kobraorca"] .badge.info {
background: rgba(24, 182, 201, 0.16);
color: #22d3ee;
}
html[data-theme="kobraorca"] .cluster-tag {
color: #e5b54f;
}
html[data-theme="kobraorca"] .brand-mark,
html[data-theme="kobraorca"] .modal-brand-logo {
border-color: #22d3ee;
box-shadow: 0 0 12px rgba(24, 182, 201, 0.26);
}
html[data-theme="kobraorca"] .slot-card.is-active,
html[data-theme="kobraorca"] .slot-card.active {
border-color: #22d3ee;
}
html[data-theme="kobraorca"] .slot-card.slot-loaded {
border-color: #22d3ee;
box-shadow: 0 0 0 1px #22d3ee;
background: linear-gradient(180deg, var(--accent-soft) 0%, var(--surface-card) 36%);
}
html[data-theme="kobraorca"] .custom-select-trigger {
background: #18242d;
color: #eef5f8;
border-color: #233542;
}
html[data-theme="kobraorca"] .custom-select-trigger:hover,
html[data-theme="kobraorca"] .custom-select.open .custom-select-trigger {
background: #1e2e39;
color: #eef5f8;
border-color: #18b6c9;
}
html[data-theme="kobraorca"] .custom-select-caret {
color: #8fa6b5;
}
html[data-theme="kobraorca"] .custom-select-menu {
background: #131d24;
border-color: #233542;
box-shadow: 0 12px 28px rgba(0, 0, 0, 0.62);
}
html[data-theme="kobraorca"] .custom-select-option {
background: #18242d;
color: #eef5f8;
}
html[data-theme="kobraorca"] .custom-select-option:hover {
background: #1e2e39;
color: #eef5f8;
border-color: #18b6c9;
}
html[data-theme="kobraorca"] .custom-select-option.is-selected {
background: #18b6c9;
color: #061318;
border-color: #22d3ee;
}
html[data-theme="kobraorca"] .modal-thumb-box,
html[data-theme="kobraorca"] .thumb-zoom-stage {
background: #0e151b;
background-image: radial-gradient(circle at 50% 45%, rgba(24, 182, 201, 0.08), rgba(6, 19, 24, 0.36));
border-color: #233542;
}
html[data-theme="kobraorca"] .thumb-mode-badge,
html[data-theme="kobraorca"] .thumb-zoom-badge {
border-color: rgba(34, 211, 238, 0.34);
background: rgba(11, 17, 22, 0.90);
color: #eef5f8;
}
html[data-theme="kobraorca"] .thumb-mode-badge.is-original {
border-color: #22d3ee;
background: #18b6c9;
color: #061318;
}
html[data-theme="kobraorca"] .mapping-panel {
background: #0e151b;
border-color: #233542;
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
.exec-value,
.job-meta-val,
.kpi-val,
.slot-meta,
.fil-card-title,
.fil-card-sub,
.slot-color-trigger,
.slot-color-option,
.matrix-table td,
.purge-badge,
.cmd-pill,
.slot-chip {
font-variant-numeric: tabular-nums;
}
.ui-icon {
width: 13px;
height: 13px;
flex-shrink: 0;
display: inline-block;
vertical-align: -2px;
stroke: currentColor;
fill: none;
stroke-width: 1.85;
stroke-linecap: round;
stroke-linejoin: round;
}
.ui-icon-sm {
width: 11px;
height: 11px;
}
.ui-icon-lg {
width: 38px;
height: 38px;
stroke-width: 1.4;
}
input[type="checkbox"] {
appearance: none;
-webkit-appearance: none;
position: relative;
width: 15px;
height: 15px;
min-width: 15px;
margin: 0;
border-radius: 3px;
border: 1.5px solid var(--border);
background: var(--surface-input);
cursor: pointer;
flex-shrink: 0;
transition: border-color 0.14s ease, background 0.14s ease, box-shadow 0.14s ease;
}
input[type="checkbox"]:hover {
border-color: var(--accent);
}
input[type="checkbox"]:checked {
background: var(--accent);
border-color: var(--accent);
}
input[type="checkbox"]:checked::after {
content: "";
position: absolute;
left: 4px;
top: 1px;
width: 4px;
height: 8px;
border-right: 2px solid #ffffff;
border-bottom: 2px solid #ffffff;
transform: rotate(40deg);
}
button:focus-visible,
input:focus-visible,
[tabindex]:focus-visible,
summary:focus-visible {
outline: 2px solid var(--accent);
outline-offset: 1px;
}
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
width: 58px;
height: 58px;
border-radius: var(--radius);
background: #0b1116;
border: 1.5px solid var(--accent);
overflow: hidden;
padding: 0;
display: flex;
align-items: center;
justify-content: center;
flex-shrink: 0;
}
.brand-logo-img {
width: 100%;
height: 100%;
object-fit: contain;
object-position: center;
display: block;
}
.modal-brand-logo {
    width: 26px;
    height: 26px;
border-radius: var(--radius-sm);
border: 1px solid var(--accent);
background: #0b1116;
object-fit: contain;
object-position: center;
flex-shrink: 0;
display: block;
}
.brand-title { margin: 0; font-size: 15px; font-weight: 700; color: var(--fg); white-space: nowrap; }
.brand-sub { font-size: 10.5px; color: var(--muted); white-space: nowrap; }
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
border-radius: var(--radius-sm);
cursor: pointer;
}
.segmented button.active {
background: var(--accent);
color: var(--accent-fg);
}
.connection-banner {
display: none;
padding: 7px 12px;
border-radius: var(--radius-sm);
border: 1px solid var(--accent);
background: var(--accent-soft);
color: var(--fg);
font-size: 12px;
font-weight: 500;
flex-shrink: 0;
}
.connection-banner.is-visible { display: block; }
.connection-banner.is-offline {
border-color: var(--warn);
background: var(--warn-bg);
color: var(--warn-fg);
}
.connection-banner-inner {
display: flex;
align-items: center;
justify-content: space-between;
gap: 10px;
flex-wrap: wrap;
}
.safety-banner {
display: none;
align-items: center;
gap: 7px;
background: var(--warn-bg);
border: 1px solid var(--warn);
color: var(--warn-fg);
padding: 4px 9px;
border-radius: var(--radius-sm);
font-weight: 600;
font-size: 11.5px;
flex-shrink: 0;
user-select: none;
}
.safety-banner.is-unlocked {
background: var(--danger-bg);
border-color: var(--danger);
color: var(--danger-fg);
}
.safety-unlock-btn {
padding: 2px 8px;
font-size: 11px;
font-weight: 700;
border-radius: var(--radius-sm);
background: var(--surface-card);
color: var(--fg);
border: 1px solid var(--warn);
cursor: pointer;
}
.safety-banner.is-unlocked .safety-unlock-btn {
border-color: var(--danger);
color: var(--danger-fg);
background: var(--danger-bg);
}
.exec-tile.connection-banner {
display: none;
align-items: center;
gap: 7px;
padding: 4px 9px;
font-size: 11.5px;
font-weight: 600;
}
.exec-tile.connection-banner.is-visible {
display: inline-flex;
}
.exec-tile.connection-banner button {
padding: 2px 8px;
font-size: 11px;
}
.safety-banner-inner {
display: flex;
align-items: center;
justify-content: space-between;
gap: 12px;
flex-wrap: wrap;
}
.banner-msg-row {
display: inline-flex;
align-items: center;
gap: 7px;
}
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
display: inline-flex;
align-items: center;
gap: 6px;
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
display: inline-flex;
align-items: center;
justify-content: center;
gap: 5px;
background: var(--surface-sub);
color: var(--fg);
border: 1px solid var(--border);
border-radius: var(--radius-sm);
padding: 5px 10px;
cursor: pointer;
font-size: 11.5px;
font-weight: 600;
font-family: var(--font);
transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease, opacity 0.14s ease;
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
button.warn:hover { background: var(--warn-bg); border-color: var(--warn); }
button.danger { border-color: var(--danger); color: var(--danger); }
button.danger:hover { background: var(--danger-bg); border-color: var(--danger); }
button.subtle { padding: 4px 8px; font-size: 11px; color: var(--muted); }
button:disabled { opacity: 0.42; cursor: not-allowed; pointer-events: none; }
.divider { border: 0; border-top: 1px solid var(--border); margin: 8px 0; }
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
transition: border-color 0.14s ease, box-shadow 0.14s ease;
}
.slot-card:hover {
border-color: var(--muted);
}
.slot-card.has-open-menu {
position: relative;
z-index: 45;
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
.swatch-row { display: flex; align-items: center; gap: 9px; }
.slot-color-bar {
width: 100%;
height: clamp(10px, 1.15vw, 14px);
border-radius: 3px;
border: 1px solid rgba(100, 116, 139, 0.48);
background: var(--surface-sub);
overflow: hidden;
box-sizing: border-box;
flex-shrink: 0;
display: flex;
align-items: stretch;
transition: opacity 0.15s ease, border-color 0.15s ease;
}
.slot-color-bar-fill {
height: 100%;
min-width: 0;
border-radius: 2px;
transition: width 0.2s ease, background-color 0.15s ease;
}
.slot-empty .slot-color-bar {
background: repeating-linear-gradient(
-45deg,
var(--surface-sub),
var(--surface-sub) 5px,
transparent 5px,
transparent 10px
);
opacity: 0.45;
}
.slot-spool-picker {
position: relative;
width: 100%;
}
.slot-spool-picker .custom-select-trigger {
padding: 4px 8px;
font-size: 10.5px;
background: var(--surface-sub);
color: var(--fg);
border: 1px solid var(--border);
}
.slot-spool-picker .custom-select-trigger:hover,
.slot-spool-picker.open .custom-select-trigger {
background: var(--surface-hover);
color: var(--fg);
border-color: var(--accent);
}
.slot-spool-picker .custom-select-menu {
background: var(--surface-card);
border: 1px solid var(--border);
max-height: 190px;
overflow-y: auto;
}
.slot-spool-picker .custom-select-option {
background: var(--surface-sub);
color: var(--fg);
font-size: 10.5px;
padding: 5px 8px;
white-space: normal;
line-height: 1.25;
}
.slot-spool-picker .custom-select-option:hover {
background: var(--surface-hover);
color: var(--fg);
}
.slot-spool-picker .custom-select-option.is-selected {
background: var(--accent);
color: var(--accent-fg);
border-color: var(--accent);
}
.slot-spool-svg {
width: clamp(38px, 3vw, 44px);
height: clamp(38px, 3vw, 44px);
flex-shrink: 0;
display: block;
}
.spool-svg-flange {
fill: var(--surface-sub);
stroke: var(--muted);
stroke-width: 1.2;
}
.spool-svg-flange.is-empty {
fill: transparent;
stroke: var(--muted);
stroke-dasharray: 3 2;
}
.spool-svg-spoke {
stroke: var(--border);
stroke-width: 1.05;
opacity: 0.85;
}
.spool-svg-hub {
fill: var(--surface-card);
stroke: var(--muted);
stroke-width: 1.25;
}
.spool-svg-hole {
fill: var(--bg);
stroke: var(--border);
stroke-width: 1;
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
.slot-actions { display: flex; gap: 5px; flex-wrap: wrap; margin-top: auto; padding-top: 5px; border-top: 1px solid var(--border); }
.slot-actions button { flex: 1; padding: 4px 6px; font-size: 11px; min-width: 44px; }
.slot-actions button.btn-full-unload {
background: rgba(220, 38, 38, 0.15);
color: var(--danger);
border-color: rgba(220, 38, 38, 0.45);
font-weight: 600;
}
.slot-actions button.btn-full-unload:hover:not(:disabled) {
background: rgba(220, 38, 38, 0.26);
border-color: var(--danger);
}
.slot-actions button.btn-assist-on {
background: var(--ok-bg);
color: var(--ok);
border-color: var(--badge-ok-border);
font-weight: 700;
box-shadow: 0 0 8px var(--ok-bg);
}
.slot-actions button.btn-assist-on:hover:not(:disabled) {
background: var(--accent-soft);
border-color: var(--ok);
}
.badge.shifting {
background: var(--warn-bg);
color: var(--warn);
border: 1px solid rgba(245, 158, 11, 0.42);
font-weight: 700;
letter-spacing: 0.4px;
text-transform: uppercase;
}
.bottom-control-strip {
display: grid;
grid-template-columns: 1fr auto;
align-items: center;
gap: 12px;
padding: 9px 14px;
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
padding: 9px 18px;
font-size: 13px;
font-weight: 700;
letter-spacing: 0.15px;
box-shadow: 0 2px 8px rgba(0, 0, 0, 0.28), 0 0 0 1px rgba(255, 255, 255, 0.08) inset;
transition: transform 0.12s ease, box-shadow 0.15s ease, filter 0.15s ease;
}
.btn-cta-send:hover:not(:disabled) {
filter: brightness(1.08);
transform: translateY(-1px);
box-shadow: 0 4px 14px rgba(0, 0, 0, 0.36), 0 0 0 1px rgba(255, 255, 255, 0.14) inset;
}
.btn-cta-send .ui-icon {
width: 15px;
height: 15px;
stroke-width: 2;
}
.modal-backdrop {
display: none;
position: fixed;
inset: 0;
z-index: 200;
background: rgba(0, 0, 0, 0.72);
backdrop-filter: blur(3px);
align-items: center;
justify-content: center;
padding: 12px;
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
.modal-dialog.modal-preprint {
max-width: 1060px;
max-height: 94vh;
}
.modal-dialog.modal-sm { max-width: 560px; }
.modal-dialog.modal-lg { max-width: 1160px; }
.modal-header {
display: flex;
align-items: center;
justify-content: space-between;
gap: 10px;
padding: 10px 14px;
background: var(--surface-sub);
border-bottom: 1px solid var(--border);
flex-shrink: 0;
}
.modal-title {
margin: 0;
font-size: 14px;
font-weight: 700;
color: var(--fg);
display: inline-flex;
align-items: center;
gap: 7px;
}
.modal-body {
padding: 10px 14px;
overflow-y: auto;
display: flex;
flex-direction: column;
gap: 9px;
flex: 1;
}
.modal-footer {
padding: 9px 14px;
background: var(--surface-sub);
border-top: 1px solid var(--border);
display: flex;
flex-direction: column;
gap: 7px;
flex-shrink: 0;
}
.modal-job-hero {
display: flex;
align-items: stretch;
gap: 12px;
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
color: var(--muted);
opacity: 0.72;
}
.thumb-mode-badge {
position: absolute;
top: 6px;
right: 6px;
z-index: 2;
display: inline-flex;
align-items: center;
gap: 4px;
padding: 3px 8px;
font-size: 10.5px;
font-weight: 700;
line-height: 1.25;
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
height: 26px;
padding: 0;
display: inline-flex;
align-items: center;
justify-content: center;
font-size: 0;
line-height: 0;
border-radius: 50%;
border: 1px solid rgba(255, 255, 255, 0.22);
background: rgba(31, 34, 33, 0.88);
color: #ffffff;
backdrop-filter: blur(4px);
cursor: zoom-in;
box-shadow: 0 2px 6px rgba(0, 0, 0, 0.28);
transition: border-color 0.14s ease, background 0.14s ease, transform 0.14s ease;
}
.thumb-zoom-badge .ui-icon {
width: 13px;
height: 13px;
margin: 0;
vertical-align: middle;
display: block;
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
padding: 8px 14px;
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
gap: 6px;
}
@media (max-width: 780px) {
.modal-job-hero { flex-direction: column; }
.modal-thumb-box { width: 100%; min-width: 0; max-width: 340px; height: auto; aspect-ratio: 2 / 1; min-height: 150px; align-self: center; }
}
@media (max-width: 480px) {
.job-meta-grid { grid-template-columns: 1fr; }
}
.job-meta-box {
background: var(--bg);
border: 1px solid var(--border);
border-radius: var(--radius-sm);
padding: 6px 10px;
min-width: 0;
display: flex;
flex-direction: column;
justify-content: center;
gap: 3px;
}
.job-meta-span2 {
grid-column: 1 / -1;
}
.job-usage-purge-row {
display: flex;
align-items: center;
flex-wrap: wrap;
gap: 14px;
font-size: 12px;
font-weight: 700;
color: var(--fg);
}
.job-usage-purge-item {
display: inline-flex;
align-items: center;
gap: 4px;
white-space: nowrap;
}
.job-meta-val {
font-size: 12px;
font-weight: 700;
color: var(--fg);
white-space: normal;
overflow-wrap: anywhere;
line-height: 1.28;
}
.mapping-panel {
background: var(--bg-deep);
border: 1.5px solid #444c49;
border-radius: var(--radius);
padding: 9px 11px;
display: flex;
flex-direction: column;
gap: 8px;
}
html[data-resolved="light"] .mapping-panel {
background: var(--surface-sub);
border-color: #aeb8b5;
}
.mapping-toolbar {
display: flex;
align-items: center;
justify-content: space-between;
gap: 10px;
flex-wrap: wrap;
}
.mapping-toolbar-left {
display: flex;
align-items: center;
gap: 10px;
flex-wrap: wrap;
}
.mapping-title {
    font-size: 15px;
    font-weight: 700;
color: var(--fg);
letter-spacing: 0.1px;
}
.map-summary-inline {
display: inline-flex;
align-items: center;
margin: 0 auto 0 2px;
}
.mapping-cards-grid {
display: grid;
grid-template-columns: repeat(2, minmax(0, 1fr));
gap: 8px;
}
@media (max-width: 820px) {
.mapping-cards-grid { grid-template-columns: 1fr; }
}
.fil-map-card {
background: var(--bg);
border: 1px solid var(--border);
border-radius: var(--radius);
padding: 8px 10px;
display: flex;
flex-direction: column;
gap: 6px;
transition: border-color 0.14s ease, box-shadow 0.14s ease;
}
.fil-map-card:hover {
border-color: var(--accent);
box-shadow: 0 0 0 1px var(--accent-soft);
}
.fil-map-card.card-mismatch {
border-color: var(--warn);
box-shadow: inset 3px 0 0 var(--warn);
}
.fil-map-card.card-error {
border-color: var(--danger);
box-shadow: inset 3px 0 0 var(--danger);
}
.fil-map-row {
display: grid;
grid-template-columns: minmax(0, 1fr) auto minmax(0, 1.05fr);
align-items: center;
gap: 8px;
}
.fil-source-col,
.fil-target-col {
display: flex;
align-items: center;
gap: 8px;
min-width: 0;
}
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
gap: 7px;
padding: 5px 9px;
border-radius: var(--radius-sm);
border: 1px solid rgba(128, 128, 128, 0.45);
background: var(--surface-card);
color: var(--fg);
font-size: 11.5px;
font-weight: 700;
line-height: 1.2;
cursor: pointer;
text-align: left;
box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.14);
transition: filter 0.14s ease, border-color 0.14s ease;
white-space: normal;
}
.slot-color-trigger:hover,
.slot-color-dropdown.open .slot-color-trigger {
border-color: var(--accent);
filter: brightness(1.06);
}
.slot-color-trigger.is-empty {
border-style: dashed;
opacity: 0.92;
}
.slot-color-trigger.is-mismatch {
border-color: var(--warn);
}
.slot-swatch-ring {
width: 13px;
height: 13px;
border-radius: 50%;
border: 1.5px solid currentColor;
display: inline-block;
flex-shrink: 0;
opacity: 0.88;
}
.slot-swatch-ring.is-empty {
background: transparent;
border: 1.5px dashed currentColor;
box-shadow: none;
}
.slot-color-trigger-label {
flex: 1;
min-width: 0;
}
.slot-lbl-stack {
display: flex;
flex-direction: column;
align-items: flex-start;
min-width: 0;
line-height: 1.18;
gap: 1px;
}
.slot-lbl-primary {
font-size: 11.5px;
font-weight: 700;
white-space: normal;
overflow-wrap: anywhere;
}
.slot-lbl-secondary {
font-size: 10px;
font-weight: 600;
opacity: 0.85;
white-space: normal;
}
.slot-color-caret {
color: inherit;
opacity: 0.88;
display: inline-flex;
align-items: center;
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
gap: 7px;
padding: 5px 8px;
border-radius: var(--radius-sm);
border: 1px solid rgba(128, 128, 128, 0.35);
background: var(--surface-sub);
color: var(--fg);
font-size: 11.5px;
font-weight: 700;
cursor: pointer;
text-align: left;
transition: filter 0.12s ease, border-color 0.12s ease;
}
.slot-color-option:hover {
filter: brightness(1.06);
border-color: var(--accent);
}
.slot-color-option.is-selected {
border-color: var(--accent);
box-shadow: inset 0 0 0 2px var(--accent);
}
.slot-opt-main {
display: flex;
align-items: center;
gap: 7px;
min-width: 0;
flex: 1;
}
.slot-opt-name {
min-width: 0;
flex: 1;
}
.slot-opt-meta {
display: inline-flex;
align-items: center;
gap: 5px;
font-size: 10.5px;
font-weight: 700;
color: inherit;
opacity: 0.92;
flex-shrink: 0;
}
.slot-rfid-tag {
padding: 1px 5px;
border-radius: 3px;
border: 1px solid currentColor;
font-size: 9.5px;
font-weight: 700;
letter-spacing: 0.2px;
line-height: 1.2;
}
.slot-spoolman-meta {
font-size: 10.5px;
color: var(--info);
font-weight: 600;
white-space: nowrap;
overflow: hidden;
text-overflow: ellipsis;
}
.slot-spoolman-meta.is-conflict {
color: var(--warn);
}
.slot-spoolman-meta.is-empty {
color: var(--danger);
}
.slot-spoolman-meta.is-missing {
color: var(--muted);
font-weight: 500;
}
.custom-select.dryer-preset-dropdown {
min-width: 130px;
}
.custom-select {
position: relative;
display: inline-block;
min-width: 148px;
}
.custom-select.dryer-unit-dropdown {
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
background: var(--accent);
color: var(--accent-fg);
border-color: var(--accent-hover);
}
html[data-resolved="light"] .custom-select-trigger {
background: #e4e9e7;
color: #111827;
border-color: #aeb8b5;
}
html[data-resolved="light"] .custom-select-trigger:hover,
html[data-resolved="light"] .custom-select.open .custom-select-trigger {
background: #d8e0dd;
color: #000000;
border-color: var(--accent);
}
html[data-resolved="light"] .custom-select-caret {
color: #374151;
}
html[data-resolved="light"] .custom-select-menu {
background: #f3f5f4;
border-color: #aeb8b5;
box-shadow: 0 10px 24px rgba(0, 0, 0, 0.18);
}
html[data-resolved="light"] .custom-select-option {
background: #e4e9e7;
color: #111827;
}
html[data-resolved="light"] .custom-select-option:hover {
background: #d8e0dd;
color: #000000;
border-color: var(--accent);
}
html[data-resolved="light"] .custom-select-option.is-selected {
background: var(--accent);
color: var(--accent-fg);
border-color: var(--accent-hover);
}
.prepare-action-bar {
display: flex;
align-items: center;
justify-content: center;
width: 100%;
padding: 2px 0;
}
.prepare-action-bar .print-trigger-cluster {
margin-left: 0;
display: flex;
align-items: center;
justify-content: center;
}
@keyframes kobraorca-spin {
to { transform: rotate(360deg); }
}
.btn-cta-send.is-loading {
opacity: 0.92;
cursor: progress;
}
.btn-cta-send .btn-spinner {
display: none;
width: 14px;
height: 14px;
border: 2px solid rgba(255, 255, 255, 0.35);
border-top-color: #ffffff;
border-radius: 50%;
animation: kobraorca-spin 0.65s linear infinite;
flex-shrink: 0;
}
.btn-cta-send.is-loading .btn-spinner {
display: inline-block;
}
.btn-cta-send.is-loading .ui-icon {
display: none;
}
.modal-dialog.modal-settings {
width: min(860px, 100%);
}
.settings-tabs {
display: flex;
align-items: center;
gap: 4px;
padding: 8px 16px 0 16px;
background: var(--surface-2);
border-bottom: 1px solid var(--border);
flex-wrap: wrap;
}
.settings-tab-btn {
padding: 8px 14px;
border: 1px solid transparent;
border-bottom: 2px solid transparent;
border-radius: var(--radius-sm) var(--radius-sm) 0 0;
background: transparent;
color: var(--muted);
font-size: 12.5px;
font-weight: 600;
cursor: pointer;
margin-bottom: -1px;
}
.settings-tab-btn:hover {
color: var(--text);
background: var(--surface-3);
}
.settings-tab-btn.active {
color: var(--accent);
background: var(--surface);
border-color: var(--border);
border-bottom-color: var(--accent);
}
.settings-panel {
display: none;
flex-direction: column;
gap: 12px;
}
.settings-panel.active {
display: flex;
}
.settings-section-card {
background: var(--surface-2);
border: 1px solid var(--border);
border-radius: var(--radius);
padding: 12px 14px;
}
.settings-section-title {
font-size: 12px;
font-weight: 700;
color: var(--text);
margin-bottom: 8px;
display: flex;
align-items: center;
justify-content: space-between;
gap: 8px;
}
.scheme-grid {
display: flex;
gap: 8px;
flex-wrap: wrap;
}
.scheme-chip {
display: inline-flex;
align-items: center;
gap: 8px;
padding: 7px 12px;
border-radius: var(--radius-sm);
border: 1px solid var(--border);
background: var(--input-bg);
color: var(--text);
font-size: 12px;
font-weight: 600;
cursor: pointer;
}
.scheme-chip.active {
border-color: var(--accent);
box-shadow: 0 0 0 1px var(--accent);
}
.scheme-swatch {
width: 12px;
height: 12px;
border-radius: 50%;
flex-shrink: 0;
}
.paths-kv-grid {
display: grid;
grid-template-columns: 165px 1fr;
gap: 6px 10px;
font-size: 12px;
align-items: center;
}
.paths-kv-val {
font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
font-size: 11.5px;
background: var(--input-bg);
border: 1px solid var(--border);
border-radius: var(--radius-sm);
padding: 4px 8px;
color: var(--text);
word-break: break-all;
}
.match-icon-badge {
display: inline-flex;
align-items: center;
justify-content: center;
width: 20px;
height: 20px;
border-radius: 50%;
font-size: 11px;
font-weight: 700;
background: var(--badge-ok-bg);
color: var(--badge-ok-fg);
border: 1px solid var(--badge-ok-border);
flex-shrink: 0;
}
.dryer-humidity-badge {
font-variant-numeric: tabular-nums;
}
.grid-2 {
display: grid;
grid-template-columns: 1fr 1fr;
gap: 12px;
}
@media (max-width: 780px) {
.grid-2 { grid-template-columns: 1fr; }
}
.hint {
font-size: 11.5px;
color: var(--muted);
margin-top: 6px;
line-height: 1.4;
}
.fil-swatch-lg {
width: 20px;
height: 20px;
border-radius: 50%;
border: 1.5px solid rgba(128, 128, 128, 0.45);
flex-shrink: 0;
}
.fil-card-meta {
min-width: 0;
flex: 1;
display: flex;
flex-direction: column;
gap: 2px;
}
.fil-card-title {
display: flex;
align-items: center;
gap: 6px;
font-size: 12px;
font-weight: 700;
color: var(--fg);
flex-wrap: wrap;
}
.fil-arrow-col {
width: 22px;
height: 22px;
border-radius: 50%;
background: var(--surface-sub);
border: 1px solid var(--border);
color: var(--muted);
display: inline-flex;
align-items: center;
justify-content: center;
flex-shrink: 0;
}
.fil-arrow-col.is-mapped {
color: var(--accent);
border-color: var(--accent);
background: var(--accent-soft);
}
.fil-status-group {
display: flex;
align-items: center;
gap: 5px;
flex-wrap: wrap;
min-width: 0;
}
.fil-status-err-text {
font-size: 10.5px;
font-weight: 600;
color: var(--warn-fg);
}
.fil-pred-divider {
display: flex;
align-items: center;
gap: 8px;
padding-top: 2px;
}
.fil-pred-divider .prediction-bar-track {
flex: 1;
min-width: 60px;
}
.fil-pred-block {
display: flex;
flex-direction: column;
gap: 5px;
padding-top: 5px;
border-top: 1px solid var(--border);
}
.fil-info-meta-row {
display: flex;
align-items: center;
justify-content: space-between;
gap: 6px;
margin-bottom: 2px;
font-size: 11.5px;
}
.prediction-bar-track {
position: relative;
width: 100%;
height: 6px;
border-radius: 999px;
background: var(--surface-sub);
border: 1px solid var(--border);
overflow: hidden;
}
.prediction-bar-track.is-light-spool {
border-color: #94a3b8;
}
.prediction-bar-before,
.prediction-bar-after {
position: absolute;
top: 0;
bottom: 0;
left: 0;
border-radius: 999px;
transition: width 0.2s ease;
}
.prediction-bar-before {
opacity: 0.28;
}
.prediction-bar-before.is-light-spool {
opacity: 0.45;
}
.prediction-metrics {
display: grid;
grid-template-columns: repeat(3, minmax(0, 1fr));
gap: 8px;
padding-top: 2px;
}
.prediction-metric {
min-width: 0;
display: flex;
flex-direction: column;
gap: 1px;
}
.prediction-metric-lbl {
font-size: 10px;
font-weight: 600;
color: var(--muted);
white-space: nowrap;
overflow: hidden;
text-overflow: ellipsis;
}
.prediction-metric-val {
font-size: 11.5px;
font-weight: 700;
color: var(--fg);
font-variant-numeric: tabular-nums;
white-space: nowrap;
}
.prediction-metric-val.is-low { color: var(--warn); }
.prediction-metric-val.is-short { color: var(--danger); }
.prediction-note {
font-size: 10.5px;
font-weight: 600;
color: var(--muted);
padding-top: 2px;
}
.prediction-note.is-low { color: var(--warn); }
.prediction-note.is-short { color: var(--danger); }
.prediction-note.is-ok { color: var(--ok); }
.job-meta-file-box {
justify-content: center;
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
font-size: 12px;
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
.modal-feedback-banner {
display: none;
padding: 4px 8px;
border-radius: var(--radius-sm);
font-size: 11px;
font-weight: 600;
line-height: 1.3;
margin-top: 2px;
border: 1px solid var(--border);
background: var(--surface-sub);
color: var(--fg);
}
.modal-feedback-banner.visible { display: block; }
.modal-feedback-banner.ok { background: var(--ok-bg); border-color: var(--ok); color: var(--ok); }
.modal-feedback-banner.info { background: var(--info-bg); border-color: var(--info); color: var(--info); }
.modal-feedback-banner.warn { background: var(--warn-bg); border-color: var(--warn); color: var(--warn-fg); }
.modal-feedback-banner.error { background: var(--danger-bg); border-color: var(--danger); color: var(--danger-fg); }
.modal-stale-banner {
    display: none;
flex-direction: column;
gap: 4px;
padding: 7px 11px;
border-radius: var(--radius-sm);
background: var(--warn-bg);
border: 1px solid var(--warn);
color: var(--warn-fg);
font-size: 11.5px;
font-weight: 600;
}
.modal-stale-banner.visible { display: flex; }
.modal-external-banner {
display: none;
align-items: center;
justify-content: space-between;
gap: 8px;
flex-wrap: wrap;
padding: 7px 11px;
margin-bottom: 8px;
border-radius: var(--radius-sm);
background: var(--info-bg, rgba(14, 116, 144, 0.12));
border: 1px solid var(--info);
color: var(--fg);
font-size: 11.5px;
font-weight: 600;
}
.modal-external-banner.visible { display: flex; }
.slots-grid.is-ace-inactive, .bottom-control-strip.is-ace-inactive { opacity: 0.45; filter: grayscale(0.75); }
.bottom-control-strip.is-ace-inactive button, .bottom-control-strip.is-ace-inactive input { pointer-events: none; }
.ace-pin-toggle-btn {display: inline-flex;align-items: center;justify-content: center;width: 22px;height: 22px;padding: 0;gap: 0;line-height: 1;flex-shrink: 0;border-radius: 99px;border: 1px solid var(--ok);background: var(--surface-sub);color: var(--muted);cursor: pointer;}
.ace-pin-toggle-btn svg {display: block;margin: 0;}
.ace-pin-toggle-btn.is-on { background: var(--ok-bg); color: var(--ok); }
.endless-spool-pill { gap: 6px; }
.endless-mode-group { display: inline-flex; align-items: center; gap: 2px; }
.endless-mode-btn { padding: 1px 6px; font-size: 10.5px; line-height: 1.4; border-radius: 99px; border: 1px solid var(--border); background: var(--surface); color: var(--muted); cursor: pointer; }
.endless-mode-btn.active { background: var(--ok-bg); color: var(--ok); border-color: var(--badge-ok-border); font-weight: 700; }
.endless-mode-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.backup-spool-badge { position: relative; display: inline-flex; align-items: center; gap: 4px; cursor: help; }
.backup-swatch-dot { width: 8px; height: 8px; border-radius: 50%; border: 1px solid rgba(128,128,128,0.6); display: inline-block; flex-shrink: 0; }
.guide-acc { display: flex; flex-direction: column; gap: 6px; }
.guide-acc details { border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-sub); padding: 7px 10px; }
.guide-acc summary { font-weight: 700; font-size: 12px; cursor: pointer; color: var(--fg); }
.guide-acc p { margin: 6px 0 2px; font-size: 11.5px; line-height: 1.48; color: var(--muted); }
.guide-link { color: var(--accent); font-weight: 600; text-decoration: underline; }
.mismatch-gate-box {
    display: none;
flex-direction: column;
gap: 6px;
padding: 8px 11px;
border-radius: var(--radius-sm);
background: var(--warn-bg);
border: 1px solid var(--warn);
color: var(--warn-fg);
}
.mismatch-gate-box.visible { display: flex; }
.mismatch-gate-label {
display: flex;
align-items: flex-start;
gap: 7px;
font-size: 11.5px;
font-weight: 700;
cursor: pointer;
}
.modal-send-lock-notice {
    display: none;
align-items: center;
gap: 7px;
padding: 6px 10px;
border-radius: var(--radius-sm);
font-size: 11px;
font-weight: 600;
line-height: 1.3;
border: 1px solid var(--warn);
background: var(--warn-bg);
color: var(--warn-fg);
}
.modal-send-lock-notice.is-visible { display: flex; }
.modal-send-lock-notice.is-danger {
border-color: var(--danger);
background: var(--danger-bg);
color: var(--danger-fg);
}
.modal-actions-bar {
display: flex;
align-items: center;
justify-content: space-between;
gap: 8px;
flex-wrap: wrap;
width: 100%;
}
.modal-actions-right {
display: flex;
align-items: center;
justify-content: flex-start;
gap: 7px;
flex-wrap: wrap;
width: 100%;
}
#btnModalSendPrint {
margin-left: auto;
}
.usage-info-wrap {
position: relative;
display: inline-flex;
align-items: center;
}
.usage-info-badge {
width: 22px;
height: 22px;
padding: 0;
border-radius: 50%;
display: inline-flex;
align-items: center;
justify-content: center;
background: var(--ok-bg);
color: var(--ok);
border: 1px solid rgba(0, 166, 150, 0.45);
cursor: help;
line-height: 0;
}
.usage-info-badge:hover,
.usage-info-badge:focus-visible {
border-color: var(--ok);
filter: brightness(1.08);
outline: none;
}
.usage-info-badge .ui-icon {
width: 13px;
height: 13px;
margin: 0;
display: block;
}
.usage-info-popover {
display: none;
position: absolute;
top: calc(100% + 6px);
left: 0;
z-index: 260;
min-width: 290px;
max-width: 440px;
padding: 8px 10px;
border-radius: var(--radius-sm);
background: var(--tooltip-bg, #1e2120);
color: var(--tooltip-fg, #f3f5f4);
border: 1px solid var(--border);
box-shadow: var(--tooltip-shadow, 0 8px 22px rgba(0, 0, 0, 0.58));
font-size: 11px;
line-height: 1.4;
white-space: normal;
pointer-events: none;
}
.usage-info-wrap:hover .usage-info-popover,
.usage-info-wrap:focus-within .usage-info-popover {
display: block;
}
.usage-info-pop-title {
font-weight: 700;
color: var(--ok);
margin-bottom: 4px;
}
.usage-info-pop-row {
display: flex;
justify-content: space-between;
gap: 12px;
}
.usage-info-pop-sep {
border-top: 1px solid var(--border);
margin: 5px 0;
}
.usage-info-pop-ext {
display: flex;
align-items: center;
justify-content: space-between;
gap: 8px;
font-variant-numeric: tabular-nums;
}
.preprint-toggles-row {
display: flex;
flex-wrap: wrap;
align-items: center;
gap: 6px;
padding-top: 2px;
}
.toggle-pill {
position: relative;
display: inline-flex;
align-items: center;
gap: 6px;
padding: 5px 10px;
border-radius: var(--radius-sm);
background: var(--surface-sub);
border: 1px solid var(--border);
font-size: 11.5px;
font-weight: 600;
color: var(--fg);
cursor: pointer;
user-select: none;
}
.toggle-pill:hover {
border-color: var(--accent);
}
.toggle-pill[data-tooltip]:hover::after,
.toggle-pill[data-tooltip]:focus-within::after,
.match-icon-badge[data-tooltip]:hover::after,
.backup-spool-badge[data-tooltip]:hover::after {
content: attr(data-tooltip);
position: absolute;
bottom: calc(100% + 6px);
left: 50%;
transform: translateX(-50%);
background: var(--tooltip-bg, #1e2120);
color: var(--tooltip-fg, #f3f5f4);
border: 1px solid var(--border);
border-radius: var(--radius-sm);
padding: 5px 9px;
font-size: 10.5px;
font-weight: 600;
white-space: nowrap;
pointer-events: none;
opacity: 1;
z-index: 250;
box-shadow: var(--tooltip-shadow, 0 8px 22px rgba(0, 0, 0, 0.58));
}
.backup-spool-badge[data-tooltip]:hover::after {
left: 0;
transform: none;
max-width: 280px;
white-space: normal;
text-align: left;
line-height: 1.35;
}
.match-icon-badge[data-tooltip]:hover::after {
left: auto;
right: 0;
transform: none;
}
.preprint-toggles-row .toggle-pill: first-of-type[data-tooltip]:hover::after,
.preprint-toggles-row .toggle-pill: first-of-type[data-tooltip]:focus-within::after {
left: 0;
transform: none;
}
.preprint-toggles-row .toggle-pill: last-of-type[data-tooltip]:hover::after,
.preprint-toggles-row .toggle-pill: last-of-type[data-tooltip]:focus-within::after {
left: auto;
right: 0;
transform: none;
}
.kpi-grid {
display: grid;
grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
gap: 8px;
margin-bottom: 10px;
}
.kpi-box {
background: var(--bg);
border: 1px solid var(--border);
border-radius: var(--radius-sm);
padding: 8px 10px;
}
.kpi-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; font-weight: 700; }
.kpi-val {
font-size: 13.5px;
font-weight: 700;
margin-top: 3px;
color: var(--fg);
display: flex;
align-items: center;
gap: 6px;
}
.inspector-panel {
background: var(--bg);
border: 1px solid var(--border);
border-radius: var(--radius);
padding: 10px 12px;
margin-top: 10px;
}
.inspector-panel-head {
display: flex;
align-items: center;
justify-content: space-between;
gap: 8px;
margin-bottom: 6px;
}
.inspector-panel-title {
margin: 0;
font-size: 11.5px;
font-weight: 700;
text-transform: uppercase;
letter-spacing: 0.4px;
color: var(--muted);
}
.slot-chip {
display: inline-flex;
align-items: center;
gap: 5px;
padding: 2px 7px;
border-radius: 99px;
background: var(--surface-sub);
border: 1px solid var(--border);
font-size: 11px;
font-weight: 600;
color: var(--fg);
white-space: nowrap;
}
.purge-badge {
display: inline-flex;
align-items: center;
gap: 4px;
padding: 2px 7px;
border-radius: var(--radius-sm);
font-size: 11px;
font-weight: 700;
white-space: nowrap;
}
.purge-badge.tier-zero { background: var(--surface-sub); color: var(--muted); border: 1px solid var(--border); }
.purge-badge.tier-low { background: var(--ok-bg); color: var(--ok); border: 1px solid rgba(0, 150, 136, 0.32); }
.purge-badge.tier-med { background: var(--warn-bg); color: var(--warn-fg); border: 1px solid var(--warn); }
.purge-badge.tier-high { background: var(--danger-bg); color: var(--danger-fg); border: 1px solid var(--danger); }
.cmd-pill {
display: inline-block;
padding: 2px 6px;
border-radius: var(--radius-sm);
background: var(--surface-sub);
border: 1px solid var(--border);
font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
font-size: 10.5px;
color: var(--info);
white-space: nowrap;
}
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
.matrix-table th, .matrix-table td {
text-align: center;
padding: 6px 8px;
font-size: 11px;
border: 1px solid var(--border);
min-width: 66px;
}
.matrix-cell-mm { display: block; font-weight: 700; color: var(--fg); }
.matrix-cell-vol { display: block; font-size: 10px; color: var(--muted); }
.events-details {
margin-top: 10px;
background: var(--bg);
border: 1px solid var(--border);
border-radius: var(--radius);
padding: 9px 12px;
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
.events-scroll { max-height: 240px; overflow-y: auto; margin-top: 8px; }
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
body.standalone-modal {
overflow: hidden;
background: var(--surface-card);
}
body.standalone-modal > :not(#prePrintModal):not(#diagnosticsModal):not(#thumbZoomModal):not(#settingsModal):not(#logModal):not(#exportPathModal),
body.standalone-modal .app-shell {
display: none;
}
body.standalone-modal #prePrintModal,
body.standalone-modal #prePrintModal.modal-backdrop {
display: flex;
position: fixed;
inset: 0;
padding: 0;
background: var(--surface-card);
backdrop-filter: none;
}
body.standalone-modal #prePrintModal .modal-dialog.modal-preprint {
width: 100%;
max-width: none;
height: 100%;
max-height: none;
border: none;
border-radius: 0;
box-shadow: none;
}
</style>
</head>
<body>
<div class="app-shell">
<!-- Unified Compact Top Bar (Single Screen, Zero Duplicate Telemetry) -->
<header class="topbar">
<div class="brand-block">
<div class="brand-mark"><img class="brand-logo-img" src="__KOBRA_ORCA_LOGO_URI__" alt="Kobra-Van-Kenorca Logo" /></div>
<div>
<h1 class="brand-title" data-i18n="ui.brand.title">Kobra-Van-Kenorca</h1>
<div class="brand-sub" data-i18n="ui.brand.sub">Kobra-Van-Kenorca — Vanilla Klipper &amp; ACE Pro Bridge for OrcaSlicer</div>
</div>
</div>

<div class="top-telemetry">
<div class="exec-tile" id="execConnTile">
<span id="connBadge" class="badge empty" data-i18n="ui.conn.unconfigured">Not configured</span>
<button type="button" id="btnToggleAceProPin" class="ace-pin-toggle-btn" style="display:none;" onclick="toggleKlipperAceProPin()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg></button>
<span class="exec-value" id="execConnVal" data-i18n="ui.exec.conn.unconfigured">Setup required</span>
<span class="exec-sub" id="execConnSub" style="display:none;" data-i18n="ui.exec.conn.unconfigured_sub">Open Settings → Connection</span>
</div>
<div class="exec-tile">
<span id="headerPrintBadge" class="badge empty" data-i18n="ui.printer.standby">Printer: Standby</span>
</div>
<div id="printSafetyBanner" class="exec-tile safety-banner">
<span class="banner-msg-row"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><span id="printSafetyText" data-i18n="ui.safety.locked_text">Active Print — Hardware locked</span><span id="printSafetyState" style="display:none;">printing</span></span>
<button type="button" id="btnToggleHardwareLock" class="safety-unlock-btn" onclick="toggleHardwareLock()" data-i18n="ui.safety.btn_unlock">Unlock</button>
</div>
<div id="noGcodeNoticeBanner" class="exec-tile connection-banner is-offline" role="status" aria-live="polite">
<span class="banner-msg-row">
<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
<span id="noGcodeNoticeText" data-i18n="ui.notice.no_gcode_slice_first">No valid sliced G-code → Please slice the plate in Preview or</span>
</span>
<button type="button" id="btnNoGcodeBrowse" class="primary" onclick="triggerMainGcodeBrowse()" data-i18n="ui.notice.btn_browse_gcode">Browse .gcode...</button>
<button type="button" id="btnNoGcodeDismiss" onclick="hideNoGcodeNotice()" data-i18n="ui.notice.btn_dismiss">Dismiss</button>
</div>
<div class="exec-tile">
<span class="exec-kicker" data-i18n="ui.exec.gcode.kicker">Active G-code &amp; Purge</span>
<span class="exec-value" id="execGcodeVal" data-i18n="ui.exec.gcode.none">No file</span>
<span class="exec-sub" id="execGcodeSub" data-i18n="ui.exec.gcode.select_sub">Select G-code for Preview / Patch</span>
</div>
</div>

<div class="topbar-right">
<div id="headerPrinterSelect" class="custom-select" data-value="default" style="min-width:130px;max-width:210px;">
<button type="button" id="headerPrinterTrigger" class="custom-select-trigger" onclick="toggleHeaderPrinterDropdown(event)" aria-haspopup="listbox" aria-expanded="false" data-i18n-aria-label="ui.header.printer_aria">
<span id="headerPrinterLabel" class="custom-select-label">Kobra S1</span>
<span class="custom-select-caret" aria-hidden="true">▾</span>
</button>
<div id="headerPrinterMenu" class="custom-select-menu" role="listbox"></div>
</div>
<button id="btnNavGuide" onclick="openSettingsModal('guide')" data-i18n-title="ui.nav.guide_tip" title="Open Quick-Start Guide &amp; FAQ"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><span data-i18n="ui.nav.guide">Guide</span></button>
<button id="btnNavSettings" onclick="openSettingsModal('connection')"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg><span data-i18n="ui.nav.settings">Settings</span></button>
<button id="btnNavDiagnostics" onclick="openDrawer('diagnosticsModal')"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg><span data-i18n="ui.nav.diagnostics">Flush &amp; Audit</span></button>
<button id="btnNavRefresh" onclick="requestStatus()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg><span data-i18n="ui.header.refresh">Refresh</span></button>
</div>
</header>

<div id="connectionNoticeBanner" class="connection-banner" role="status" aria-live="polite">
<div class="connection-banner-inner">
<span class="banner-msg-row">
<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
<span id="connectionNoticeText" data-i18n="ui.notice.unconfigured">Not connected to your printer yet. Open Connection to configure Moonraker and ACE Pro.</span>
</span>
<div class="row" style="margin:0;gap:6px;">
<button type="button" id="btnNoticeRetry" style="display:none;" onclick="requestStatus()" data-i18n="ui.notice.btn_retry">Retry</button>
<button type="button" id="btnNoticeOpenConn" class="primary" onclick="openSettingsModal('connection')" data-i18n="ui.notice.btn_open_conn">Open Connection</button>
</div>
</div>
</div>

<!-- Primary View: ACE Pro Live Slot Bay (T0-T7, 4x2 Grid) -->
<section class="card slots-main-card">
<div class="card-head">
<h2 id="slotBayTitle" class="card-title" data-i18n="ui.sec3.title">3. ACE Pro Live Slot Bay (T0–T7)</h2>
</div>
<div id="slotsContainer" class="slots-grid"></div>
</section>

<!-- Single-Row Compact Dryer & Print Trigger Bar -->
<section class="bottom-control-strip">
<!-- Cluster 1: ACE Pro Dryer -->
<div class="strip-cluster">
<span class="cluster-tag" data-i18n="ui.bar.dryer_label">ACE Dryer:</span>
<span id="dryerBadge" class="badge empty" data-i18n="ui.sec2.dryer_stop_badge">Dryer: Stop</span>
<span id="dryerTempBadge" class="badge info dryer-temp-badge" style="display:none;"></span>
<span id="dryerHumidityBadge" class="badge info dryer-humidity-badge" style="display:none;"></span>
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
<input type="number" id="dryerTemp" value="45" min="20" max="70" oninput="onDryerTempInput()" onchange="onDryerSettingsChange()" />
<label for="dryerPresetTrigger" data-i18n="ui.sec2.preset_label">Preset:</label>
<div id="dryerPreset" class="custom-select dryer-preset-dropdown" data-value="45">
<button type="button" id="dryerPresetTrigger" class="custom-select-trigger" onclick="toggleDryerPresetDropdown(event)" aria-haspopup="listbox" aria-expanded="false" data-i18n-aria-label="ui.sec2.preset_aria">
<span id="dryerPresetLabel" class="custom-select-label" data-i18n="ui.sec2.preset_pla">PLA (45 °C)</span>
<span class="custom-select-caret" aria-hidden="true">▾</span>
</button>
<div id="dryerPresetMenu" class="custom-select-menu" role="listbox">
<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="custom" data-i18n="ui.sec2.preset_custom" onclick="selectDryerPreset('custom', event)">Custom</button>
<button type="button" role="option" aria-selected="true" class="custom-select-option is-selected" data-value="45" data-i18n="ui.sec2.preset_pla" onclick="selectDryerPreset('45', event)">PLA (45 °C)</button>
<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="60" data-i18n="ui.sec2.preset_petg" onclick="selectDryerPreset('60', event)">PETG (60 °C)</button>
<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="50" data-i18n="ui.sec2.preset_tpu" onclick="selectDryerPreset('50', event)">TPU (50 °C)</button>
</div>
</div>
<label for="dryerDuration" data-i18n="ui.sec2.dur_label">Duration (min):</label>
<input type="number" id="dryerDuration" value="240" min="1" max="1440" onchange="onDryerSettingsChange()" />
<button id="btnDryerStart" class="primary" onclick="startDryer()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2c1 4-2 6-2 10a4 4 0 0 0 8 0c0-3-2-5-2-8 2 1.5 4 4 4 8a8 8 0 1 1-16 0c0-4 3-7 8-10z"/></svg><span data-i18n="ui.sec2.btn_dryer_start">Dryer Start</span></button>
<button id="btnDryerStop" class="danger" onclick="stopDryer()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1"/></svg><span data-i18n="ui.sec2.btn_dryer_stop">Dryer Stop</span></button>
</div>
</section>

<!-- Centered Pre-Print Send Modal Trigger Bar (below ACE Dryer) -->
<section class="prepare-action-bar">
<div class="print-trigger-cluster">
<button id="btnOpenPrePrintModal" class="primary btn-cta-send" aria-busy="false" onclick="openPrePrintModal()"><span class="btn-spinner" aria-hidden="true"></span><svg class="ui-icon" viewBox="0 0 24 24" stroke-width="2" aria-hidden="true"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg><span id="btnOpenPrePrintLabel" data-i18n="ui.bar.btn_prepare">Prepare &amp; Send Print</span></button>
</div>
</section>
</div>

<!-- ===================================================================== -->
<!-- INTERACTIVE PRE-PRINT SEND MODAL (SnapSend-Inspired Single-Card View) -->
<!-- ===================================================================== -->
<div id="prePrintModal" class="modal-backdrop" role="dialog" aria-modal="true">
<div class="modal-dialog modal-preprint">
<div class="modal-header">
<div style="min-width:0;">
<h2 class="modal-title" style="display:inline-flex;align-items:center;gap:7px;"><img class="modal-brand-logo" src="__KOBRA_ORCA_LOGO_URI__" alt="KobraOrca" /><span data-i18n="ui.modal.preprint.title">Map · Check · Send</span></h2>
</div>
<div class="row" style="margin:0;">
<input type="file" id="modalGcodeFileInput" accept=".gcode,.gco,.gc,.gcode.3mf,.3mf" style="display:none;" onchange="handleModalGcodeFileSelect(event)" />
<button type="button" id="btnModalLoadSlicer" onclick="acquireSlicerGcode(true)"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg><span data-i18n="ui.modal.btn.load_slicer">Reload G-Code from Slicer</span></button>
<button type="button" id="btnModalBrowseGcode" onclick="triggerModalGcodeBrowse()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg><span data-i18n="ui.modal.btn.browse_gcode">Browse .gcode</span></button>
<span id="modalPrinterTargetBadge" class="badge info">Kobra S1</span>
<span id="modalPrinterBadge" class="badge empty" data-i18n="ui.printer.standby">Printer: Standby</span>
<span id="modalAceModeBadge" class="badge info" style="display:none;"></span>
<button type="button" onclick="closePrePrintModal()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span data-i18n="ui.modal.close">Close</span></button>
</div>
</div>

<div class="modal-body">
<!-- Top-Left Enlarged G-code Thumbnail & 2x2 Job Metadata Strip -->
<div class="modal-job-hero">
<div id="modalThumbBox" class="modal-thumb-box" onclick="onModalThumbBoxClick(event)" title="No embedded G-code thumbnail found" data-i18n-title="ui.modal.thumb.fallback_title">
<img id="modalThumbImg" class="modal-thumb-img" src="" alt="" style="display:none;" />
<canvas id="modalThumbCanvas" class="modal-thumb-canvas" style="display:none;" aria-label="G-code preview thumbnail" data-i18n-aria-label="ui.modal.thumb.alt"></canvas>
<span id="modalThumbFallback" class="modal-thumb-fallback" aria-hidden="true"><svg class="ui-icon ui-icon-lg" viewBox="0 0 24 24"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg></span>
<button type="button" id="btnThumbColorMode" class="thumb-mode-badge" style="display:none;" onclick="toggleThumbnailColorMode(event)" title="Toggle live preview between mapped ACE Pro colors and original Slicer colors" data-i18n-title="ui.modal.thumb.mode_title"><svg class="ui-icon ui-icon-sm" viewBox="0 0 24 24" aria-hidden="true"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/></svg><span class="btn-label" data-i18n="ui.modal.thumb.mode_mapped">Mapped</span></button>
<button type="button" id="btnThumbZoom" class="thumb-zoom-badge" style="display:none;" onclick="openThumbZoomModal(event)" title="Click to inspect full-size preview (zoom)" data-i18n-title="ui.modal.thumb.zoom_title" aria-label="Click to inspect full-size preview (zoom)" data-i18n-aria-label="ui.modal.thumb.zoom_title"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg></button>
</div>
<div class="job-meta-grid">
<div class="job-meta-box job-meta-file-box">
<div class="job-meta-file-row">
<div class="job-meta-val" id="modalJobFile" data-i18n="ui.modal.job.no_file">No G-code file selected</div>
<button type="button" id="btnClearModalGcode" class="btn-clear-inline" style="display:none;" onclick="clearModalGcode()" title="Clear loaded G-code" data-i18n-title="ui.modal.btn.clear_gcode_title" aria-label="Clear loaded G-code" data-i18n-aria-label="ui.modal.btn.clear_gcode_title"><svg class="ui-icon ui-icon-sm" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
</div>
<div id="modalFeedbackBanner" class="modal-feedback-banner" role="status"></div>
</div>
<div class="job-meta-box">
<div class="kpi-label" data-i18n="ui.modal.kpi.time_layers">Est. Time &amp; Layers</div>
<div class="job-meta-val" id="modalJobTimeLayers">—</div>
</div>
<div class="job-meta-box job-meta-span2">
<div class="kpi-label" data-i18n="ui.modal.kpi.usage_purge">Usage &amp; Purge</div>
<div class="job-usage-purge-row">
<span class="job-usage-purge-item" id="modalJobWeight">Usage total: 0.0 g</span>
<span class="job-usage-purge-item" id="modalJobPurgeG">Purge: 0.0 g</span>
<span class="job-usage-purge-item" id="modalJobPurge">Toolchanges: 0</span>
</div>
</div>
</div>
</div>

<div id="modalStaleModelBanner" class="modal-stale-banner" role="alert">
<div class="banner-msg-row">
<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
<div id="modalStaleModelText"></div>
</div>
<div class="row" style="margin:2px 0 0 0;gap:6px;">
<button type="button" class="subtle" onclick="triggerModalGcodeBrowse()"><svg class="ui-icon ui-icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg><span data-i18n="ui.modal.btn.browse_gcode">Browse .gcode</span></button>
<button type="button" class="subtle" onclick="clearModalGcode()"><svg class="ui-icon ui-icon-sm" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span data-i18n="ui.modal.stale.clear_btn">Unload Stale G-code</span></button>
</div>
</div>

<!-- Unified Bordered Mapping & Filament Prediction Container -->
<div id="modalMappingPanel" class="mapping-panel">
<div class="mapping-toolbar">
<div class="mapping-toolbar-left">
<strong class="mapping-title" data-i18n="ui.modal.cards.title">Mapping</strong>
<label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;font-size:11px;">
<input type="checkbox" id="chkShowUnusedExtruders" onchange="renderMappingTable()" />
<span data-i18n="ui.modal.cards.show_unused">Show unused extruders</span>
</label>
<span id="mappingUsageInfoWrap" class="usage-info-wrap" style="display:none;">
<button type="button" id="mappingUsageInfoBadge" class="usage-info-badge" aria-label="Detailed filament consumption breakdown" data-i18n-aria-label="ui.map.usage_info_aria">
<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
</button>
<div id="mappingUsagePopover" class="usage-info-popover" role="tooltip"></div>
</span>
</div>
<span id="mappingSummaryBar" class="map-summary-inline"><span class="badge empty" data-i18n="ui.sec4.summary_ready">Ready</span></span>
<div id="mappingAceActions" class="row" style="margin:0;">
<button onclick="syncOrcaFilaments()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg><span data-i18n="ui.sec4.btn_project">Slicer Defaults</span></button>
<button class="primary" onclick="autoMatchColors()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 4V2m0 14v-2M8 9h2m10 0h2m-4.2 4.2 1.4 1.4M10.8 4.8l1.4 1.4M17.8 4.8l-1.4 1.4M3 21l9-9"/></svg><span data-i18n="ui.sec4.btn_automatch">Auto-Match</span></button>
<button class="subtle" onclick="resetColorMapping()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg><span data-i18n="ui.sec4.btn_reset">1:1 Default</span></button>
</div>
</div>

<div id="modalExternalSpoolBanner" class="modal-external-banner" role="status">
<div class="banner-msg-row">
<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
<span id="modalExternalSpoolText"></span>
</div>
<button type="button" id="btnSwitchBackToAce" class="subtle" style="display:none;" onclick="setExternalSpoolMode(false)" data-i18n="ui.modal.map.switch_to_ace">Switch to ACE Pro</button>
</div>

<!-- Visual Filament-to-Slot Mapping + Integrated Remaining Prediction Cards -->
<div id="modalMappingCards" class="mapping-cards-grid"></div>
</div>

<!-- Material Mismatch Explicit Confirmation Gate -->
<div id="modalMismatchGate" class="mismatch-gate-box">
<div>
<span class="banner-msg-row"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><strong data-i18n="ui.modal.mismatch_gate.title">Material Mismatch Detected</strong></span>
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
<label class="toggle-pill" data-i18n-title="ui.modal.opt.skip_purge_tip" title="SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 (K3/K3M only — KS1/KS1M G9111 always uses Smart Skip)" data-tooltip="SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 (K3/K3M only — KS1/KS1M G9111 always uses Smart Skip)">
<input type="checkbox" id="chkSkipPurgeLoaded" checked onchange="onPrePrintPurgeOptionChange()" />
<span data-i18n="ui.modal.opt.skip_purge">Skip Purge</span>
</label>
<label class="toggle-pill" data-i18n-title="ui.modal.opt.cut_post_tip" title="PRINT_END CUT_TIP=1 — Cut tip and unload filament at print end" data-tooltip="PRINT_END CUT_TIP=1 — Cut tip and unload filament at print end">
<input type="checkbox" id="chkCutTipPost" onchange="syncSettings()" />
<span data-i18n="ui.modal.opt.cut_post">Unload after print</span>
</label>
<div class="toggle-pill endless-spool-pill" id="endlessSpoolTogglePill" data-i18n-title="ui.modal.opt.endless_spool_tip" title="Automatically switch to a matching backup spool on filament runout (applied to Klipper on Upload / Print)">
<label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;margin:0;">
<input type="checkbox" id="chkEndlessSpool" onchange="onEndlessSpoolOptionChange()" />
<span data-i18n="ui.modal.opt.endless_spool">Endless Spool</span>
</label>
<span id="endlessSpoolModeGroup" class="endless-mode-group" role="group" aria-label="Endless Spool Mode">
<button type="button" class="endless-mode-btn" data-mode="exact" onclick="setEndlessSpoolMode('exact')" data-i18n="ui.modal.opt.endless_mode_exact">Exact (Material + Color)</button>
<button type="button" class="endless-mode-btn" data-mode="material" onclick="setEndlessSpoolMode('material')" data-i18n="ui.modal.opt.endless_mode_material">Material</button>
<button type="button" class="endless-mode-btn" data-mode="next" onclick="setEndlessSpoolMode('next')" data-i18n="ui.modal.opt.endless_mode_next">Next Ready</button>
</span>
</div>
<label class="toggle-pill" id="extSpoolTogglePill" data-i18n-title="ui.modal.opt.external_spool_tip" title="Treat this job as an external spool print: skips ACE slot bounds, empty-slot/material checks, ACE tool remapping, and purge commands.">
<input type="checkbox" id="chkExternalSpool" onchange="onExternalSpoolOptionChange()" />
<span data-i18n="ui.modal.opt.external_spool">External Spool (Bypass ACE)</span>
</label>
</div>

<div id="modalSendLockNotice" class="modal-send-lock-notice" role="status" aria-live="polite">
<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
<span id="modalSendLockText"></span>
</div>

<div class="modal-actions-bar">
<button id="btnInspectFlushPreview" onclick="inspectFlushFromModal()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg><span data-i18n="ui.modal.btn.inspect_flush">Inspect Flush Volume &amp; Post-Processing Preview</span></button>
<div class="row modal-actions-right">
<button type="button" onclick="closePrePrintModal()" data-i18n="ui.modal.btn.cancel">Cancel</button>
<button id="btnModalApplyLocal" class="primary" onclick="runPostProcessAndUpload(false, false)"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg><span data-i18n="ui.modal.btn.apply_local">Apply &amp; Patch Locally</span></button>
<button id="btnModalExportGcode" onclick="exportPatchedGcode()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><span data-i18n="ui.modal.btn.export_gcode">Export .gcode...</span></button>
<button id="btnModalUploadOnly" class="primary" onclick="runPostProcessAndUpload(true, false)"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg><span data-i18n="ui.modal.btn.upload_only">Upload Only</span></button>
<button id="btnModalSendPrint" class="primary btn-cta-send" onclick="runPostProcessAndUpload(true, true)"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg><span data-i18n="ui.modal.btn.send_print">Upload &amp; Start Print</span></button>
</div>
</div>
</div>
</div>
</div>

<!-- ===================================================================== -->
<!-- SECONDARY MODAL 1: UNIFIED SETTINGS MENU                              -->
<!-- ===================================================================== -->
<div id="fullUnloadConfirmModal" class="modal-backdrop" role="dialog" aria-modal="true" onclick="closeFullUnloadModal()">
<div class="modal-dialog" style="max-width:460px;" onmousedown="event.stopPropagation()" onclick="event.stopPropagation()">
<div class="modal-header">
<h2 class="modal-title" id="fullUnloadModalTitle">Confirm Full Unload</h2>
<button type="button" onclick="closeFullUnloadModal()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span data-i18n="ui.modal.close">Close</span></button>
</div>
<div class="modal-body" style="padding:14px 16px;">
<div style="padding:10px 12px;border-radius:8px;background:var(--danger-bg);border:1px solid var(--danger);color:var(--text);font-size:12.5px;line-height:1.45;" id="fullUnloadModalBody"></div>
</div>
<div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;padding:10px 16px;">
<button type="button" onclick="closeFullUnloadModal()" data-i18n="ui.modal.btn.cancel">Cancel</button>
<button type="button" id="btnConfirmFullUnloadExec" class="primary" style="background:var(--danger);border-color:var(--danger);color:#fff;" onclick="executeConfirmedFullUnload()">Full Unload</button>
</div>
</div>
</div>

<div id="settingsModal" class="modal-backdrop" role="dialog" aria-modal="true">
<div class="modal-dialog modal-settings" onmousedown="event.stopPropagation()" onclick="event.stopPropagation()">
<div class="modal-header">
<h2 class="modal-title" data-i18n="ui.settings.title">Kobra-Van-Kenorca Settings</h2>
<div class="row" style="margin:0;">
<button type="button" id="btnSaveSettingsOnly" onclick="syncSettings()" data-i18n="ui.settings.conn.save_only">Save Settings</button>
<button type="button" onclick="closeDrawer('settingsModal')"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span data-i18n="ui.modal.close">Close</span></button>
</div>
</div>
<div class="modal-body">
<div class="settings-tabs" role="tablist">
<button type="button" role="tab" id="settingsTabBtn_connection" class="settings-tab-btn active" onclick="switchSettingsTab('connection')" data-i18n="ui.settings.tab_connection">Connection</button>
<button type="button" role="tab" id="settingsTabBtn_purge" class="settings-tab-btn" onclick="switchSettingsTab('purge')" data-i18n="ui.settings.tab_purge">Purge &amp; Reserve</button>
<button type="button" role="tab" id="settingsTabBtn_appearance" class="settings-tab-btn" onclick="switchSettingsTab('appearance')" data-i18n="ui.settings.tab_appearance">Appearance</button>
<button type="button" role="tab" id="settingsTabBtn_language" class="settings-tab-btn" onclick="switchSettingsTab('language')" data-i18n="ui.settings.tab_language">Language</button>
<button type="button" role="tab" id="settingsTabBtn_paths" class="settings-tab-btn" onclick="switchSettingsTab('paths')" data-i18n="ui.settings.tab_paths">Paths</button>
<button type="button" role="tab" id="settingsTabBtn_diagnostics" class="settings-tab-btn" onclick="switchSettingsTab('diagnostics')" data-i18n="ui.settings.tab_diagnostics">Diagnostics</button>
<button type="button" role="tab" id="settingsTabBtn_guide" class="settings-tab-btn" onclick="switchSettingsTab('guide')" data-i18n="ui.settings.tab_guide">Guide</button>
</div>

<!-- TAB 1: Connection (Moonraker & Spoolman) -->
<div id="settingsPanel_connection" class="settings-panel active" role="tabpanel">
<div class="settings-section-card">
<div><span class="badge info" data-i18n="ui.sec1.offline_badge">Offline analysis works independently</span></div>
<div class="row" style="gap:6px;flex-wrap:wrap;background:var(--surface-sub);padding:7px 9px;border-radius:var(--radius-sm);border:1px solid var(--border);">
<label for="settingsPrinterTrigger" data-i18n="ui.settings.conn.profile_label">Printer Profile:</label>
<div id="settingsPrinterSelect" class="custom-select" data-value="default" style="min-width:165px;">
<button type="button" id="settingsPrinterTrigger" class="custom-select-trigger" onclick="toggleSettingsPrinterDropdown(event)" aria-haspopup="listbox" aria-expanded="false" data-i18n-aria-label="ui.header.printer_aria">
<span id="settingsPrinterLabel" class="custom-select-label">Kobra S1</span>
<span class="custom-select-caret" aria-hidden="true">▾</span>
</button>
<div id="settingsPrinterMenu" class="custom-select-menu" role="listbox"></div>
</div>
<label for="printerProfileName" data-i18n="ui.settings.conn.profile_name_label">Profile Name:</label>
<input type="text" id="printerProfileName" value="Kobra S1" placeholder="e.g. Kobra S1 Studio" data-i18n-placeholder="ui.settings.conn.profile_name_placeholder" oninput="markSettingsDirty()" style="min-width:130px;flex:1;" />
<button type="button" id="btnPrinterProfileNew" onclick="createPrinterProfile()" data-i18n="ui.settings.conn.profile_new">+ New Printer</button>
<button type="button" id="btnPrinterProfileRename" onclick="renamePrinterProfile()" data-i18n="ui.settings.conn.profile_rename">Save Name</button>
<button type="button" id="btnPrinterProfileDelete" class="danger" onclick="deletePrinterProfile()" data-i18n="ui.settings.conn.profile_delete" disabled>Delete</button>
</div>
<div class="row">
<label for="moonrakerUrl" data-i18n="ui.sec1.url_label">URL:</label>
<input type="text" id="moonrakerUrl" value="" placeholder="http://192.0.2.10:7125 or http://printer.local:7125" data-i18n-placeholder="sec1.url.placeholder" oninput="markSettingsDirty()" style="flex:1;" />
<label class="row" style="gap:5px;margin:0;cursor:pointer;"><input type="checkbox" id="port80Fallback" checked onchange="markSettingsDirty()" /> <span data-i18n="ui.sec1.port80">Port 80 Fallback</span></label>
</div>
<div class="row">
<label for="apiKey" data-i18n="ui.sec1.apikey_label">API Key:</label>
<input type="text" id="apiKey" value="" placeholder="Optional X-Api-Key" data-i18n-placeholder="ui.sec1.apikey_placeholder" oninput="markSettingsDirty()" style="flex:1;" />
<button type="button" class="primary" onclick="saveSettingsAndRefresh(); closeDrawer('settingsModal');" data-i18n="ui.sec1.connect_btn">Connect &amp; Load Status</button>
</div>
<hr class="divider" />
<div class="row">
<label for="spoolmanUrl" data-i18n="ui.sec1.spoolman_label">Spoolman URL:</label>
<input type="text" id="spoolmanUrl" value="" placeholder="Optional e.g. http://192.0.2.10:7912 (leave empty to disable)" data-i18n-placeholder="ui.sec1.spoolman_placeholder" oninput="markSettingsDirty()" style="flex:1;" />
<button type="button" id="btnTestSpoolman" onclick="testSpoolmanConnection()" data-i18n="ui.sec1.spoolman_test_btn">Test / Refresh</button>
</div>
<div class="row" style="margin-top:2px;">
<span id="spoolmanStatusBadge" class="badge empty" data-i18n="ui.sec1.spoolman_off">Spoolman: Disabled (optional)</span>
</div>
<div class="row" style="margin-top:4px;">
<label for="configuredAceCountTrigger" data-i18n="ui.settings.conn.ace_count_label">ACE Units (Offline / Pre-Connect):</label>
<div id="configuredAceCount" class="custom-select" data-value="auto" style="min-width:220px;">
<button type="button" id="configuredAceCountTrigger" class="custom-select-trigger" onclick="toggleConfiguredAceCountDropdown(event)" aria-haspopup="listbox" aria-expanded="false" data-i18n-aria-label="ui.settings.conn.ace_count_aria">
<span id="configuredAceCountLabel" class="custom-select-label" data-i18n="ui.settings.conn.ace_count_auto">Auto-Detect (from Klipper)</span>
<span class="custom-select-caret" aria-hidden="true">▾</span>
</button>
<div id="configuredAceCountMenu" class="custom-select-menu" role="listbox">
<button type="button" role="option" aria-selected="true" class="custom-select-option is-selected" data-value="auto" data-i18n="ui.settings.conn.ace_count_auto" onclick="selectConfiguredAceCount('auto', event)">Auto-Detect (from Klipper)</button>
<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="1" data-i18n="ui.settings.conn.ace_count_1" onclick="selectConfiguredAceCount('1', event)">1 ACE (4 Slots · T0–T3)</button>
<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="2" data-i18n="ui.settings.conn.ace_count_2" onclick="selectConfiguredAceCount('2', event)">2 ACEs (8 Slots · T0–T7)</button>
<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="3" data-i18n="ui.settings.conn.ace_count_3" onclick="selectConfiguredAceCount('3', event)">3 ACEs (12 Slots · T0–T11)</button>
<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="4" data-i18n="ui.settings.conn.ace_count_4" onclick="selectConfiguredAceCount('4', event)">4 ACEs (16 Slots · T0–T15)</button>
</div>
</div>
</div>
<div id="deviceMeta" style="color: var(--muted); font-size: 11.5px;" data-i18n="ui.sec1.meta_initial">
ACE Status: — | Active Tool: — | Temp: —
</div>
</div>
</div>

<!-- TAB 2: Purge & Reserve -->
<div id="settingsPanel_purge" class="settings-panel" role="tabpanel">
<div class="settings-section-card">
<div class="row">
<label for="predictionReserveInput" data-i18n="ui.settings.conn.reserve_label">Pre-Print Safety Reserve (g):</label>
<input type="number" id="predictionReserveInput" value="15" min="0" max="500" step="1" oninput="markSettingsDirty()" style="width:84px;" />
<span style="font-size:11px;color:var(--muted);" data-i18n="ui.settings.conn.reserve_hint">Warn as Low when remaining filament after print + purge falls below this reserve.</span>
</div>
<hr class="divider" />
<div class="row">
<label for="startupPurgeLengthInput" data-i18n="ui.settings.conn.startup_purge_label">Startup Flushing Volume (G9111, mm):</label>
<input type="number" id="startupPurgeLengthInput" value="200" min="0" max="1000" step="1" oninput="markSettingsDirty()" style="width:84px;" />
<span style="font-size:11px;color:var(--muted);flex:1;line-height:1.35;" data-i18n="ui.settings.conn.startup_purge_hint">Estimated filament length (mm) purged at print start when the target slot is not yet loaded (0 = off).</span>
</div>
<details id="startupPurgePromptDetails" class="events-details" style="margin-top:6px;">
<summary data-i18n="ui.settings.purge.llm_summary">Calculate exact value from your Klipper &amp; ACE configs (LLM Prompt)</summary>
<div style="margin-top:8px;">
<div style="font-size:11px;color:var(--muted);" data-i18n="ui.settings.purge.llm_intro">Copy this prompt and paste your printer.cfg / printer_KS1.cfg and ace.cfg / ace_KS1.cfg below it:</div>
<pre id="startupPurgePromptText" class="status-log" style="max-height:200px;margin:6px 0;" data-i18n="ui.settings.purge.llm_prompt"></pre>
<div class="row" style="margin:0;justify-content:flex-end;">
<button type="button" id="btnCopyStartupPurgePrompt" onclick="copyStartupPurgePrompt()" data-i18n="ui.settings.purge.llm_copy_btn">Copy LLM Prompt</button>
</div>
</div>
</details>
</div>
</div>

<!-- TAB 3: Appearance (Theme + 8 Color Schemes) -->
<div id="settingsPanel_appearance" class="settings-panel" role="tabpanel">
<div class="settings-section-card">
<div class="row" style="justify-content:space-between;">
<span class="settings-section-title" data-i18n="ui.settings.appearance.theme_label">Theme Mode</span>
<div class="segmented" role="group" aria-label="Select color theme" data-i18n-aria-label="header.theme.aria">
<button type="button" id="themeBtnAuto" class="active" onclick="setThemeMode('auto')" data-i18n="header.theme.auto">Auto</button>
<button type="button" id="themeBtnDark" onclick="setThemeMode('dark')" data-i18n="header.theme.dark">Dark</button>
<button type="button" id="themeBtnLight" onclick="setThemeMode('light')" data-i18n="header.theme.light">Light</button>
<button type="button" id="themeBtnKobraOrca" onclick="setThemeMode('kobraorca')" data-i18n="header.theme.kobraorca">KobraOrca</button>
</div>
</div>
<div class="settings-section-title" data-i18n="ui.settings.appearance.scheme_label">Accent Color Scheme (8 Palettes)</div>
<div class="scheme-grid" role="group">
<button type="button" class="scheme-chip active" data-scheme="teal" aria-pressed="true" onclick="setColorScheme('teal', true)"><span class="scheme-swatch" style="background:#009688;"></span><span data-i18n="ui.settings.scheme.teal">Orca Teal</span></button>
<button type="button" class="scheme-chip" data-scheme="ocean" aria-pressed="false" onclick="setColorScheme('ocean', true)"><span class="scheme-swatch" style="background:#2563eb;"></span><span data-i18n="ui.settings.scheme.ocean">Ocean Blue</span></button>
<button type="button" class="scheme-chip" data-scheme="amber" aria-pressed="false" onclick="setColorScheme('amber', true)"><span class="scheme-swatch" style="background:#d97706;"></span><span data-i18n="ui.settings.scheme.amber">Warm Amber</span></button>
<button type="button" class="scheme-chip" data-scheme="coral" aria-pressed="false" onclick="setColorScheme('coral', true)"><span class="scheme-swatch" style="background:#e11d48;"></span><span data-i18n="ui.settings.scheme.coral">Coral Red</span></button>
<button type="button" class="scheme-chip" data-scheme="forest" aria-pressed="false" onclick="setColorScheme('forest', true)"><span class="scheme-swatch" style="background:#16a34a;"></span><span data-i18n="ui.settings.scheme.forest">Forest Green</span></button>
<button type="button" class="scheme-chip" data-scheme="violet" aria-pressed="false" onclick="setColorScheme('violet', true)"><span class="scheme-swatch" style="background:#7c3aed;"></span><span data-i18n="ui.settings.scheme.violet">Royal Violet</span></button>
<button type="button" class="scheme-chip" data-scheme="cyan" aria-pressed="false" onclick="setColorScheme('cyan', true)"><span class="scheme-swatch" style="background:#0891b2;"></span><span data-i18n="ui.settings.scheme.cyan">Arctic Cyan</span></button>
<button type="button" class="scheme-chip" data-scheme="graphite" aria-pressed="false" onclick="setColorScheme('graphite', true)"><span class="scheme-swatch" style="background:#64748b;"></span><span data-i18n="ui.settings.scheme.graphite">Graphite</span></button>
</div>
<div style="font-size:11px;color:var(--muted);" data-i18n="ui.settings.appearance.note">Accent colors adapt buttons, highlights, and status controls across Light, Dark, Auto, and KobraOrca without altering ACE filament colors.</div>
</div>
</div>

<!-- TAB 3: Language -->
<div id="settingsPanel_language" class="settings-panel" role="tabpanel">
<div class="settings-section-card">
<div class="row" style="justify-content:space-between;">
<span class="settings-section-title" data-i18n="ui.settings.language.label">Interface Language</span>
<div class="segmented" role="group" aria-label="Select interface language" data-i18n-aria-label="header.language.aria">
<button type="button" id="langBtnEn" class="active" onclick="setLanguage('en', true)" data-i18n="header.language.en">English</button>
<button type="button" id="langBtnDe" onclick="setLanguage('de', true)" data-i18n="header.language.de">Deutsch</button>
</div>
</div>
<div style="font-size:11px;color:var(--muted);" data-i18n="ui.settings.language.hint">Language changes apply immediately without losing unsaved connection fields.</div>
</div>
</div>

<!-- TAB 4: Paths -->
<div id="settingsPanel_paths" class="settings-panel" role="tabpanel">
<div class="settings-section-card">
<div class="settings-section-title" data-i18n="ui.settings.paths.gcode_active">Active G-Code File</div>
<div class="row">
<input type="text" id="gcodePathInput" placeholder="/path/to/file.gcode" data-i18n-placeholder="ui.sec5.path_placeholder" oninput="markSettingsDirty()" style="flex:1;" />
<button type="button" onclick="parseGcodeHeader()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><span data-i18n="ui.sec5.btn_parse">Read Filaments from Header</span></button>
</div>
<div class="row">
<label for="defaultGcodeDirInput" data-i18n="ui.settings.paths.gcode_dir">Default G-Code Folder:</label>
<input type="text" id="defaultGcodeDirInput" value="" placeholder="Optional e.g. /home/user/gcode" data-i18n-placeholder="ui.settings.paths.gcode_dir_placeholder" oninput="markSettingsDirty()" style="flex:1;" />
</div>
<hr class="divider" />
<div class="settings-section-title" data-i18n="ui.settings.paths.runtime_title">Resolved Runtime Paths</div>
<div class="paths-kv-grid">
<span style="color:var(--muted);" data-i18n="ui.settings.paths.cfg_file">Plugin Config:</span><span class="paths-kv-val" id="pathCfgVal">—</span>
<span style="color:var(--muted);" data-i18n="ui.settings.paths.storage_dir">Storage Dir:</span><span class="paths-kv-val" id="pathStorageVal">—</span>
<span style="color:var(--muted);" data-i18n="ui.settings.paths.staged_dir">Staged G-Code:</span><span class="paths-kv-val" id="pathStagedVal">—</span>
<span style="color:var(--muted);" data-i18n="ui.settings.paths.diag_file">Diagnostics Log:</span><span class="paths-kv-val" id="pathDiagVal">—</span>
</div>
</div>
</div>

<!-- TAB 5: Diagnostics -->
<div id="settingsPanel_diagnostics" class="settings-panel" role="tabpanel">
<div class="settings-section-card">
<div class="row" style="justify-content:space-between;">
<span class="settings-section-title" data-i18n="ui.settings.diag.anon_title">Anonymized Diagnostics Summary (Safe for Sharing)</span>
<div class="row" style="margin:0;gap:6px;">
<button type="button" onclick="refreshAnonymizedDiagnostics()" data-i18n="ui.settings.diag.btn_refresh">Refresh Summary</button>
<button type="button" id="btnCopyAnonDiag" class="primary" onclick="copyAnonymizedDiagnostics()" data-i18n="ui.settings.diag.btn_copy">Copy Anonymized</button>
</div>
</div>
<pre id="anonDiagPreview" class="status-log" style="max-height:190px;margin:0;"></pre>
<label class="row" style="gap:6px;margin:2px 0;cursor:pointer;font-size:11.5px;">
<input type="checkbox" id="chkDiagLoadLogging" onchange="markSettingsDirty(); syncSettings();" />
<span data-i18n="ui.settings.diag.load_logging_label">Enable detailed G-code load diagnostics log (diagnostics_gcode_load.jsonl)</span>
</label>
<div class="row" style="justify-content:flex-end;">
<button type="button" id="btnNavLog" onclick="closeDrawer('settingsModal'); openDrawer('logModal');"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg><span data-i18n="ui.settings.diag.open_log">Open Status Log</span></button>
<button type="button" onclick="closeDrawer('settingsModal'); openDrawer('diagnosticsModal');" data-i18n="ui.settings.diag.open_flush">Open Flush &amp; Audit</button>
</div>
</div>
</div>

<!-- TAB 6: Guide / Help -->
<div id="settingsPanel_guide" class="settings-panel" role="tabpanel">
<div class="settings-section-card">
<div style="font-size:12px;color:var(--muted);margin-bottom:8px;">
<span data-i18n="ui.guide.intro">Quick overview of Kobra-Van-Kenorca. Full guide with details on GitHub:</span>
<a class="guide-link" href="https://github.com/Gwebster-r/Kobra-Van-Kenorca/blob/main/USER_GUIDE.md" target="_blank" rel="noopener noreferrer" data-i18n="ui.guide.github_link">Open full USER_GUIDE.md on GitHub</a>
</div>
<div class="guide-acc">
<details open><summary data-i18n="ui.guide.s1_title">1. Quick Start (3 Steps)</summary><p data-i18n="ui.guide.s1_body"></p></details>
<details><summary data-i18n="ui.guide.s2_title">2. Slot Bay, Auto-Feed, ACE Power &amp; Safety Lock</summary><p data-i18n="ui.guide.s2_body"></p></details>
<details><summary data-i18n="ui.guide.s3_title">3. Map · Check · Send &amp; Spoolman</summary><p data-i18n="ui.guide.s3_body"></p></details>
<details><summary data-i18n="ui.guide.s4_title">4. Endless Spool &amp; External Spool</summary><p data-i18n="ui.guide.s4_body"></p></details>
<details><summary data-i18n="ui.guide.s5_title">5. FAQ &amp; Troubleshooting</summary><p data-i18n="ui.guide.s5_body"></p></details>
</div>
</div>
</div>
</div>
</div>
</div>

<!-- ===================================================================== -->
<!-- SECONDARY MODAL 2: FLUSH VOLUME MONITORING & POST-PROCESSING AUDIT    -->
<!-- ===================================================================== -->
<div id="diagnosticsModal" class="modal-backdrop" role="dialog" aria-modal="true">
<div class="modal-dialog modal-lg" onmousedown="event.stopPropagation()" onclick="event.stopPropagation()">
<div class="modal-header">
<div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
<h2 class="modal-title" data-i18n="sec6.title">6. Flush Volume Monitoring &amp; Post-Processing Audit</h2>
<span id="flushModeBadge" class="badge empty">—</span>
<span id="flushStatusBadge" class="badge empty" data-i18n="ui.sec6.no_analysis">No analysis yet</span>
</div>
<div class="row" style="margin:0;">
<button onclick="previewGcodeFlush()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg><span data-i18n="ui.sec5.btn_preview">Offline Flush Analysis (Preview)</span></button>
<button type="button" onclick="closeDrawer('diagnosticsModal')"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span data-i18n="ui.modal.close">Close</span></button>
</div>
</div>
<div class="modal-body">
<div id="flushKpiGrid" class="kpi-grid"></div>
<div id="flushAuditWarnings"></div>

<div class="grid-2" style="margin-top:2px;">
<div class="inspector-panel">
<div class="inspector-panel-head">
<h3 class="inspector-panel-title" data-i18n="ui.sec6.vec_title">Initial Flush Vector (flush_volumes_vector)</h3>
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
<div class="inspector-panel">
<div class="inspector-panel-head">
<h3 class="inspector-panel-title" data-i18n="ui.sec6.mat_title">N×N Flush Matrix Heatmap (Slicer From → To)</h3>
</div>
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
<div class="modal-dialog modal-sm" onmousedown="event.stopPropagation()" onclick="event.stopPropagation()">
<div class="modal-header">
<h2 class="modal-title" data-i18n="ui.log.title">Status &amp; Audit Log</h2>
<div class="row" style="margin:0;">
<button class="subtle" onclick="clearStatusLog()" data-i18n="ui.log.clear_btn">Clear</button>
<button type="button" onclick="closeDrawer('logModal')"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span data-i18n="ui.modal.close">Close</span></button>
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
<button type="button" id="btnZoomColorMode" onclick="toggleThumbnailColorMode(event)" title="Toggle live preview between mapped ACE Pro colors and original Slicer colors" data-i18n-title="ui.modal.thumb.mode_title"><svg class="ui-icon ui-icon-sm" viewBox="0 0 24 24" aria-hidden="true"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/></svg><span class="btn-label" data-i18n="ui.modal.thumb.mode_mapped">Mapped</span></button>
<button type="button" id="btnZoomClose" onclick="closeThumbZoomModal(event)"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span data-i18n="ui.modal.thumb.zoom_close">Close (Esc)</span></button>
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

<!-- ===================================================================== -->
<!-- SECONDARY MODAL 5: EXPORT G-CODE DESTINATION PICKER                   -->
<!-- ===================================================================== -->
<div id="exportPathModal" class="modal-backdrop modal-backdrop-zoom" role="dialog" aria-modal="true" onclick="closeExportPathModal()">
<div class="modal-dialog modal-sm" onmousedown="event.stopPropagation()" onclick="event.stopPropagation()">
<div class="modal-header">
<h2 class="modal-title" data-i18n="ui.export_modal.title">Export Patched G-code</h2>
<button type="button" onclick="closeExportPathModal()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span data-i18n="ui.modal.close">Close</span></button>
</div>
<div class="modal-body" style="display:flex;flex-direction:column;gap:10px;">
<div style="display:flex;flex-direction:column;gap:4px;">
<div class="row" style="justify-content:space-between;margin:0;">
<label for="exportTargetDirInput" style="font-size:11px;font-weight:700;color:var(--muted);" data-i18n="ui.export_modal.dir_label">Target Folder</label>
<div class="row" style="margin:0;gap:4px;">
<button type="button" class="subtle" style="padding:2px 8px;font-size:11px;" onclick="setExportDirPreset('downloads')" data-i18n="ui.export_modal.btn_downloads">Downloads</button>
<button type="button" class="subtle" style="padding:2px 8px;font-size:11px;" onclick="setExportDirPreset('storage')" data-i18n="ui.export_modal.btn_plugin_dir">Plugin Folder</button>
</div>
</div>
<input type="text" id="exportTargetDirInput" style="width:100%;" />
</div>
<div style="display:flex;flex-direction:column;gap:4px;">
<label for="exportFilenameInput" style="font-size:11px;font-weight:700;color:var(--muted);" data-i18n="ui.export_modal.file_label">Filename</label>
<input type="text" id="exportFilenameInput" style="width:100%;" />
</div>
<div style="font-size:11px;color:var(--muted);line-height:1.45;" data-i18n="ui.export_modal.hint">Choose the destination folder and filename. If OrcaSlicer's sandbox blocks writing outside plugin storage, the file is saved into the plugin's exported_gcode folder and the exact path is shown.</div>
<div class="row" style="justify-content:flex-end;margin-top:4px;">
<button type="button" onclick="closeExportPathModal()" data-i18n="ui.modal.btn.cancel">Cancel</button>
<button type="button" id="btnConfirmExportGcode" class="primary" onclick="confirmExportPatchedGcode()" data-i18n="ui.export_modal.btn_save">Save .gcode</button>
</div>
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

let lastAppliedI18nLanguage = null;

function applyTranslations() {
if (currentLanguage !== lastAppliedI18nLanguage) {
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
if (key) {
const text = t(key);
el.setAttribute("title", text);
if (el.hasAttribute("data-tooltip")) {
el.setAttribute("data-tooltip", text);
}
}
});
document.querySelectorAll("[data-i18n-aria-label]").forEach(el => {
const key = el.getAttribute("data-i18n-aria-label");
if (key) el.setAttribute("aria-label", t(key));
});
lastAppliedI18nLanguage = currentLanguage;
}
renderConnectionNotice();
renderLiveHeaderAndMeta();
renderExecutiveStrip();
renderResolvedPaths();
if (typeof renderDryerUnitDropdown === "function") {
renderDryerUnitDropdown();
} else if (typeof setDryerUnitValue === "function") {
setDryerUnitValue((typeof readDryerControls === "function") ? readDryerControls().unit : currentState.dryer_unit);
}
if (typeof setConfiguredAceCountValue === "function") {
setConfiguredAceCountValue(currentState.configured_ace_count || "auto");
}
if (typeof renderPrinterProfileControls === "function") {
renderPrinterProfileControls();
}
if (typeof syncDryerPresetFromTemp === "function") {
const tempEl = document.getElementById("dryerTemp");
const ddPreset = document.getElementById("dryerPreset");
const curPreset = ddPreset && ddPreset.dataset && ddPreset.dataset.value ? ddPreset.dataset.value : "";
if (curPreset === "custom" && tempEl && String(tempEl.value || "") === String(currentState.dryer_temp)) {
syncDryerPresetFromTemp("custom", true);
} else {
syncDryerPresetFromTemp(tempEl ? tempEl.value : currentState.dryer_temp);
}
}
if (typeof renderSpoolmanStatusBadge === "function") {
renderSpoolmanStatusBadge();
}
renderSlots();
renderMappingTable();
renderFlushDashboard();
renderModalJobSummary();
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
theme_mode: "auto",
color_scheme: "teal",
moonraker_url: "",
spoolman_url: "",
default_gcode_dir: "",
diag_load_logging: false,
resolved_paths: {},
manual_spoolman_selections: {},
anonymized_diagnostics: "",
spoolman_status: { enabled: false, connected: false, url: "", spools_count: 0, matched_count: 0, conflict_count: 0, error: "" },
enable_port80_fallback: true,
bed_leveling: true,
skip_purge_for_already_loaded_tool: true,
cut_tip_after_print: false,
force_initial_purge: false,
start_print_immediately: false,
dryer_unit: "all",
dryer_temp: 45,
dryer_duration: 240,
configured_ace_count: "auto",
last_detected_ace_count: 0,
effective_ace_count: 1,
active_printer_id: "default",
printer_profiles: [
{ id: "default", name: "Kobra S1", moonraker_url: "", api_key: "", enable_port80_fallback: true, spoolman_url: "", configured_ace_count: "auto", last_detected_ace_count: 0 }
],
prediction_reserve_g: 15,
startup_purge_length_mm: 200,
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
{tool: 3, label: "T3", status: "empty", hex_color: "#000000", material: "", temp: 0, rfid: false}
]
};

let _localMismatchAck = false;
let _localMismatchAckContextKey = "";

function computeMismatchContextKey(stateObj) {
const st = stateObj || {};
const gPath = String(st.last_gcode_path || "").trim();
const filSig = String(st.filament_signature || "");
const tmap = st.tool_map || {};
const mapStr = Object.keys(tmap).sort((a, b) => Number(a) - Number(b)).map(k => `${k}:${tmap[k]}`).join(",");
const slots = (st.ace_status && Array.isArray(st.ace_status.slots))
? st.ace_status.slots
: (Array.isArray(st.ace_slots) ? st.ace_slots : []);
const slotStr = slots.map(sl => {
if (!sl) return "";
const sp = sl.spoolman || {};
return [
sl.index !== undefined ? sl.index : (sl.tool !== undefined ? sl.tool : ""),
String(sl.status || ""),
String(sl.material || sl.type || "").toUpperCase(),
String(sl.hex_color || sl.color || "").toUpperCase(),
String(sl.sku || ""),
String(sp.id !== undefined && sp.id !== null ? sp.id : (sl.spool_id || ""))
].join(":");
}).join("|");
return `${gPath}||${filSig}||${mapStr}||${slotStr}`;
}

function getEffectiveAceUnitCount() {
const st = currentState.ace_status || {};
const liveSlots = Array.isArray(st.slots) ? st.slots : [];
if (st.connected && (liveSlots.length > 0 || Number(st.ace_instances || st.ace_count || 0) > 0)) {
const bySlots = Math.ceil(liveSlots.length / 4);
const byInst = Number(st.ace_instances || st.ace_count || 0);
return Math.max(1, Math.min(4, Math.max(bySlots, byInst, 1)));
}
const cfg = String(currentState.configured_ace_count || "auto").trim().toLowerCase();
if (["1", "2", "3", "4"].includes(cfg)) {
return Number(cfg);
}
const rawSlots = Array.isArray(currentState.ace_slots) ? currentState.ace_slots : [];
if (rawSlots.length > 0 && st.connected !== false && st.connection_state) {
return Math.max(1, Math.min(4, Math.ceil(rawSlots.length / 4)));
}
const lastDet = Number(currentState.last_detected_ace_count || 0);
if (lastDet >= 1 && lastDet <= 4) {
return lastDet;
}
const effSrv = Number(currentState.effective_ace_count || 0);
if (effSrv >= 1 && effSrv <= 4) {
return effSrv;
}
return 1;
}

function getEffectiveMaxSlots() {
return getEffectiveAceUnitCount() * 4;
}

function isKlipperAceDisabled() {
const st = currentState.ace_status || {};
const cState = getEffectiveConnectionState();
if (cState === "offline" || cState === "unconfigured") return false;
return st.ace_pro_enabled === false;
}

function getAceHardwareState() {
const st = currentState.ace_status || {};
return String(st.ace_hw_state || "").trim().toLowerCase();
}

function isAceHardwareInactive() {
const st = currentState.ace_status || {};
const cState = getEffectiveConnectionState();
if (cState !== "connected_standby" && cState !== "connected_active") return false;
const hw = getAceHardwareState();
return isKlipperAceDisabled() || st.connected === false || hw === "connecting" || hw === "disconnected" || hw === "error";
}

function isExternalSpoolBypassActive() {
return isKlipperAceDisabled() || !!currentState.external_spool_mode;
}

function isMoonrakerOnline() {
const cState = getEffectiveConnectionState();
return cState === "connected_standby" || cState === "connected_active";
}

let endlessSpoolDraft = { dirty: false, enabled: false, mode: "exact" };

function normalizeEndlessMode(raw) {
const m = String(raw || "exact").trim().toLowerCase();
return (m === "material" || m === "next") ? m : "exact";
}

function syncEndlessSpoolUi(resetDraft) {
if (resetDraft) endlessSpoolDraft.dirty = false;
const st = (currentState && currentState.ace_status) ? currentState.ace_status : {};
const online = isMoonrakerOnline();
const aceActive = online && !isExternalSpoolBypassActive() && !isKlipperAceDisabled();
if (!endlessSpoolDraft.dirty) {
endlessSpoolDraft.enabled = !!st.endless_spool_enabled;
endlessSpoolDraft.mode = normalizeEndlessMode(st.endless_spool_mode);
}
const pill = document.getElementById("endlessSpoolTogglePill");
const chk = document.getElementById("chkEndlessSpool");
const grp = document.getElementById("endlessSpoolModeGroup");
if (pill) pill.style.display = aceActive ? "inline-flex" : "none";
if (chk) {
chk.checked = !!endlessSpoolDraft.enabled;
chk.disabled = !aceActive;
}
if (grp && grp.querySelectorAll) {
grp.querySelectorAll(".endless-mode-btn").forEach(btn => {
const m = btn.dataset ? btn.dataset.mode : (btn.getAttribute ? btn.getAttribute("data-mode") : "");
const isAct = m === endlessSpoolDraft.mode;
if (btn.classList) btn.classList.toggle("active", isAct);
if (btn.setAttribute) btn.setAttribute("aria-pressed", isAct ? "true" : "false");
btn.disabled = !aceActive || !endlessSpoolDraft.enabled;
});
}
}

function onEndlessSpoolOptionChange() {
const chk = document.getElementById("chkEndlessSpool");
endlessSpoolDraft = {
dirty: true,
enabled: !!(chk && chk.checked),
mode: normalizeEndlessMode(endlessSpoolDraft.mode)
};
syncEndlessSpoolUi(false);
renderMappingTable();
}

function setEndlessSpoolMode(mode) {
endlessSpoolDraft = {
dirty: true,
enabled: true,
mode: normalizeEndlessMode(mode)
};
syncEndlessSpoolUi(false);
renderMappingTable();
}

function findEndlessSpoolBackupChain(slots, currentTool, mode) {
if (!Array.isArray(slots) || slots.length <= 1) return [];
const normMode = normalizeEndlessMode(mode);
const byTool = {};
slots.forEach((s, i) => { if (s && typeof s === "object") byTool[s.tool !== undefined ? Number(s.tool) : i] = s; });
const total = slots.length;
const cur = byTool[Number(currentTool)];
if (!cur) return [];
const curMat = String(cur.material || "").trim().toLowerCase();
const curRgb = Array.isArray(cur.color) ? cur.color.slice(0, 3).map(Number) : [0, 0, 0];
const matches = [];
for (let off = 1; off < total; off++) {
const cand = byTool[(Number(currentTool) + off) % total];
if (!cand) continue;
const st = String(cand.status || "").trim().toLowerCase();
if (st !== "ready" && st !== "loaded") continue;
if (normMode === "next") { matches.push(cand); continue; }
const candMat = String(cand.material || "").trim().toLowerCase();
if (!curMat || curMat === "unknown" || !candMat || candMat === "unknown") continue;
if (candMat !== curMat) continue;
if (normMode === "exact") {
const candRgb = Array.isArray(cand.color) ? cand.color.slice(0, 3).map(Number) : [0, 0, 0];
if (candRgb[0] !== curRgb[0] || candRgb[1] !== curRgb[1] || candRgb[2] !== curRgb[2]) continue;
}
matches.push(cand);
}
return matches;
}

function findEndlessSpoolBackup(slots, currentTool, mode) {
const chain = findEndlessSpoolBackupChain(slots, currentTool, mode);
return chain.length > 0 ? chain[0] : null;
}

function syncExternalSpoolUi() {
const klipperOff = isKlipperAceDisabled();
const online = isMoonrakerOnline();
const isPrinting = !!(currentState.ace_status && currentState.ace_status.is_printing);
const extEl = document.getElementById("chkExternalSpool");
if (extEl) {
extEl.checked = online ? klipperOff : !!currentState.external_spool_mode;
extEl.disabled = online && isPrinting && !hardwareUnlocked;
}
syncEndlessSpoolUi(false);
}

function toggleKlipperAceProPin(targetEnabled) {
if (!isMoonrakerOnline()) return;
const desired = (targetEnabled === undefined) ? isKlipperAceDisabled() : !!targetEnabled;
sendToHost({
type: "set_ace_pro_enabled",
enabled: desired,
allow_while_printing: !!hardwareUnlocked
});
}

function onExternalSpoolOptionChange() {
const extEl = document.getElementById("chkExternalSpool");
const wantExternal = !!(extEl && extEl.checked);
if (isMoonrakerOnline()) {
toggleKlipperAceProPin(!wantExternal);
return;
}
currentState.external_spool_mode = wantExternal;
syncSettings();
renderLiveHeaderAndMeta();
renderMappingTable();
if (String(currentState.last_gcode_path || "").trim()) {
previewGcodeFlush();
}
}

function setExternalSpoolMode(enabled) {
const extEl = document.getElementById("chkExternalSpool");
if (extEl && !extEl.disabled) {
extEl.checked = !!enabled;
}
if (isMoonrakerOnline()) {
toggleKlipperAceProPin(!enabled);
return;
}
currentState.external_spool_mode = !!enabled;
onExternalSpoolOptionChange();
}

function selectExternalSpool(spoolId, event) {
if (event) event.stopPropagation();
closeAllSlotDropdowns();
const parsed = (spoolId === null || spoolId === undefined || spoolId === "") ? null : Number(spoolId);
currentState.external_spool_id = (parsed !== null && Number.isFinite(parsed) && parsed > 0) ? parsed : null;
syncSettings();
renderMappingTable();
}

/* Modal & Drawer Controls */
let hasReceivedInitialState = false;
let settingsModalDirty = false;
let activeSettingsTab = "connection";

function markSettingsDirty() {
settingsModalDirty = true;
}

function isSettingsModalOpen() {
const el = document.getElementById("settingsModal");
return !!(el && el.classList.contains("open"));
}

function switchSettingsTab(tabName) {
const valid = ["connection", "purge", "appearance", "language", "paths", "diagnostics", "guide"];
const target = valid.includes(String(tabName || "")) ? String(tabName) : "connection";
activeSettingsTab = target;
valid.forEach(name => {
const btn = document.getElementById("settingsTabBtn_" + name);
const panel = document.getElementById("settingsPanel_" + name);
const isAct = name === target;
if (btn) btn.classList.toggle("active", isAct);
if (panel) panel.classList.toggle("active", isAct);
});
if (target === "paths") renderResolvedPaths();
if (target === "diagnostics") refreshAnonymizedDiagnostics();
}

function openSettingsModal(tabName) {
openDrawer("settingsModal");
if (tabName) switchSettingsTab(tabName);
}

function renderResolvedPaths() {
const paths = (currentState.resolved_paths && typeof currentState.resolved_paths === "object")
? currentState.resolved_paths
: {};
const fallbackTxt = t("ui.settings.paths.unresolved");
const pairs = [
["pathCfgVal", paths.config_path],
["pathStorageVal", paths.storage_dir],
["pathStagedVal", paths.staged_dir],
["pathDiagVal", paths.diagnostics_log_path]
];
pairs.forEach(pair => {
const el = document.getElementById(pair[0]);
if (el) el.textContent = String(pair[1] || "").trim() || fallbackTxt;
});
}

function refreshAnonymizedDiagnostics() {
const preEl = document.getElementById("anonDiagPreview");
if (preEl && currentState.anonymized_diagnostics) {
preEl.textContent = currentState.anonymized_diagnostics;
}
sendToHost({ type: "get_diagnostics_bundle" });
}

function copyAnonymizedDiagnostics() {
const preEl = document.getElementById("anonDiagPreview");
const txt = (preEl && preEl.textContent) ? preEl.textContent : (currentState.anonymized_diagnostics || "");
if (!txt) return;
if (navigator && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
navigator.clipboard.writeText(txt).catch(() => {});
}
logMsg(t("ui.settings.diag.copied"));
}

function copyStartupPurgePrompt() {
const preEl = document.getElementById("startupPurgePromptText");
const txt = (preEl && preEl.textContent) ? preEl.textContent : t("ui.settings.purge.llm_prompt");
if (!txt) return;
if (navigator && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
navigator.clipboard.writeText(txt).catch(() => {});
}
logMsg(t("ui.settings.purge.llm_copied"));
}

function getPrinterProfilesList() {
const raw = Array.isArray(currentState.printer_profiles) ? currentState.printer_profiles : [];
if (raw.length > 0) return raw;
return [{
id: currentState.active_printer_id || "default",
name: "Kobra S1",
moonraker_url: currentState.moonraker_url || "",
spoolman_url: currentState.spoolman_url || "",
configured_ace_count: currentState.configured_ace_count || "auto"
}];
}

function getActivePrinterProfile() {
const list = getPrinterProfilesList();
const activeId = String(currentState.active_printer_id || "default").toLowerCase();
return list.find(p => String(p.id || "").toLowerCase() === activeId) || list[0] || null;
}

function renderPrinterProfileControls() {
const list = getPrinterProfilesList();
const active = getActivePrinterProfile();
const activeId = active ? String(active.id || "default") : "default";
const activeName = active ? String(active.name || activeId) : "Kobra S1";

const buildOpts = (fnName) => list.map(p => {
const pid = String(p.id || "");
const pname = String(p.name || pid);
const isSel = pid === activeId;
const hostHint = String(p.moonraker_url || "").replace(/^https?:\\/\\//i, "").replace(/\\/.*$/, "");
const fullLabel = hostHint ? `${pname} (${hostHint})` : pname;
const pidArg = escapeHtml(JSON.stringify(pid));
return `<button type="button" role="option" aria-selected="${isSel ? "true" : "false"}" class="custom-select-option${isSel ? " is-selected" : ""}" data-value="${escapeHtml(pid)}" onclick="${fnName}(${pidArg}, event)">${escapeHtml(fullLabel)}</button>`;
}).join("");

const hdrMenu = document.getElementById("headerPrinterMenu");
if (hdrMenu) hdrMenu.innerHTML = buildOpts("selectHeaderPrinter");
const hdrEl = document.getElementById("headerPrinterSelect");
if (hdrEl) hdrEl.dataset.value = activeId;
const hdrLbl = document.getElementById("headerPrinterLabel");
if (hdrLbl) hdrLbl.textContent = activeName;

const setMenu = document.getElementById("settingsPrinterMenu");
if (setMenu) setMenu.innerHTML = buildOpts("selectSettingsPrinter");
const setEl = document.getElementById("settingsPrinterSelect");
if (setEl) setEl.dataset.value = activeId;
const setLbl = document.getElementById("settingsPrinterLabel");
if (setLbl) setLbl.textContent = activeName;

const delBtn = document.getElementById("btnPrinterProfileDelete");
if (delBtn) delBtn.disabled = list.length <= 1;

const targetBadge = document.getElementById("modalPrinterTargetBadge");
if (targetBadge) {
targetBadge.textContent = t("ui.modal.target_printer", { name: activeName });
const activeUrl = active && active.moonraker_url ? String(active.moonraker_url) : String(currentState.moonraker_url || "");
if (activeUrl) targetBadge.setAttribute("title", activeUrl);
}
}

function toggleHeaderPrinterDropdown(event) {
renderPrinterProfileControls();
toggleCustomSelectDropdown(
document.getElementById("headerPrinterSelect"),
document.getElementById("headerPrinterTrigger"),
document.getElementById("headerPrinterMenu"),
event
);
}

function selectHeaderPrinter(profileId, event) {
if (event) event.stopPropagation();
closeAllSlotDropdowns();
currentState.active_printer_id = String(profileId || "default");
renderPrinterProfileControls();
sendToHost({ type: "switch_printer", profile_id: currentState.active_printer_id });
}

function toggleSettingsPrinterDropdown(event) {
renderPrinterProfileControls();
toggleCustomSelectDropdown(
document.getElementById("settingsPrinterSelect"),
document.getElementById("settingsPrinterTrigger"),
document.getElementById("settingsPrinterMenu"),
event
);
}

function selectSettingsPrinter(profileId, event) {
if (event) event.stopPropagation();
closeAllSlotDropdowns();
settingsModalDirty = false;
currentState.active_printer_id = String(profileId || "default");
sendToHost({ type: "switch_printer", profile_id: currentState.active_printer_id });
}

function createPrinterProfile() {
settingsModalDirty = false;
const list = getPrinterProfilesList();
const defaultName = t("ui.settings.conn.profile_default_new", { n: list.length + 1 });
sendToHost({
type: "save_printer_profile",
profile_id: "",
name: defaultName,
moonraker_url: "",
api_key: "",
enable_port80_fallback: true,
spoolman_url: String(currentState.spoolman_url || ""),
configured_ace_count: "auto",
activate: true,
refresh_status: true
});
}

function renamePrinterProfile() {
const active = getActivePrinterProfile();
const nameEl = document.getElementById("printerProfileName");
const nextName = nameEl ? nameEl.value.trim() : "";
if (!active || !nextName) return;
settingsModalDirty = false;
const s = collectSettingsFromUI();
sendToHost({
type: "save_printer_profile",
profile_id: active.id,
name: nextName,
moonraker_url: s.moonraker_url,
api_key: s.api_key,
enable_port80_fallback: s.enable_port80_fallback,
spoolman_url: s.spoolman_url,
configured_ace_count: s.configured_ace_count,
activate: true,
refresh_status: false
});
}

function deletePrinterProfile() {
const active = getActivePrinterProfile();
if (!active || getPrinterProfilesList().length <= 1) return;
settingsModalDirty = false;
sendToHost({ type: "delete_printer_profile", profile_id: active.id, refresh_status: true });
}

function syncSettingsInputsFromState(force, skipProfileRender) {
if (!force && (isSettingsModalOpen() || settingsModalDirty)) return;
const urlEl = document.getElementById("moonrakerUrl");
if (urlEl && (force || document.activeElement !== urlEl)) {
urlEl.value = currentState.moonraker_url || "";
}
const keyEl = document.getElementById("apiKey");
if (keyEl && (force || document.activeElement !== keyEl)) {
keyEl.value = currentState.api_key || "";
}
const smEl = document.getElementById("spoolmanUrl");
if (smEl && (force || document.activeElement !== smEl)) {
smEl.value = currentState.spoolman_url || "";
}
const p80El = document.getElementById("port80Fallback");
if (p80El && (force || document.activeElement !== p80El)) {
p80El.checked = !!currentState.enable_port80_fallback;
}
const gcodeInput = document.getElementById("gcodePathInput");
if (gcodeInput && (force || document.activeElement !== gcodeInput) && currentState.last_gcode_path !== undefined) {
gcodeInput.value = currentState.last_gcode_path || "";
}
const gcodeDirInput = document.getElementById("defaultGcodeDirInput");
if (gcodeDirInput && (force || document.activeElement !== gcodeDirInput)) {
gcodeDirInput.value = currentState.default_gcode_dir || "";
}
const resEl = document.getElementById("predictionReserveInput");
if (resEl && (force || document.activeElement !== resEl) && currentState.prediction_reserve_g !== undefined) {
resEl.value = String(currentState.prediction_reserve_g);
}
const spLenEl = document.getElementById("startupPurgeLengthInput");
if (spLenEl && (force || document.activeElement !== spLenEl) && currentState.startup_purge_length_mm !== undefined) {
spLenEl.value = String(currentState.startup_purge_length_mm);
}
const diagLogChk = document.getElementById("chkDiagLoadLogging");
if (diagLogChk && (force || document.activeElement !== diagLogChk)) {
diagLogChk.checked = !!currentState.diag_load_logging;
}
const cfgAceEl = document.getElementById("configuredAceCount");
if (cfgAceEl && (force || !cfgAceEl.classList.contains("open"))) {
setConfiguredAceCountValue(currentState.configured_ace_count || "auto");
}
const pNameEl = document.getElementById("printerProfileName");
if (pNameEl && (force || document.activeElement !== pNameEl)) {
const prof = getActivePrinterProfile();
pNameEl.value = prof ? String(prof.name || "") : "Kobra S1";
}
if (!skipProfileRender && typeof renderPrinterProfileControls === "function") {
renderPrinterProfileControls();
}
}

function bindSafeBackdropClose(modalId, onCloseFn) {
const el = document.getElementById(modalId);
if (!el) return;
let downOnBackdrop = false;
el.addEventListener("mousedown", evt => {
downOnBackdrop = !!(evt && evt.target === el && evt.button === 0);
});
el.addEventListener("click", evt => {
const shouldClose = !!(downOnBackdrop && evt && evt.target === el);
downOnBackdrop = false;
if (shouldClose && typeof onCloseFn === "function") {
onCloseFn();
}
});
}

function openDrawer(modalId) {
const el = document.getElementById(modalId);
if (!el) return;
if (modalId === "settingsModal" && !el.classList.contains("open")) {
settingsModalDirty = false;
syncSettingsInputsFromState(true);
}
el.classList.add("open");
}

function closeDrawer(modalId) {
const el = document.getElementById(modalId);
if (el) el.classList.remove("open");
if (modalId === "settingsModal") {
settingsModalDirty = false;
}
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

let _openModalAfterBrowse = false;

function showNoGcodeNotice(customText, level) {
const banner = document.getElementById("noGcodeNoticeBanner");
const txtEl = document.getElementById("noGcodeNoticeText");
if (txtEl && customText) {
txtEl.textContent = String(customText);
} else if (txtEl) {
txtEl.textContent = t("ui.notice.no_gcode_slice_first");
}
if (banner) {
const isInfo = String(level || "").toLowerCase() === "info";
banner.classList.toggle("is-offline", !isInfo);
banner.classList.add("is-visible");
}
}

function hideNoGcodeNotice() {
const banner = document.getElementById("noGcodeNoticeBanner");
if (banner) {
banner.classList.remove("is-visible");
banner.classList.add("is-offline");
}
}

function triggerMainGcodeBrowse() {
_openModalAfterBrowse = true;
triggerModalGcodeBrowse();
}

function hasValidLoadedGcode() {
const gPath = String((currentState && currentState.last_gcode_path) || "").trim();
return Boolean(gPath);
}

function setPrepareBtnLoading(loading) {
const btn = document.getElementById("btnOpenPrePrintModal");
if (!btn) return;
const isBusy = Boolean(loading);
btn.classList.toggle("is-loading", isBusy);
btn.setAttribute("aria-busy", isBusy ? "true" : "false");
const lbl = document.getElementById("btnOpenPrePrintLabel") || btn.querySelector("[data-i18n]");
if (lbl) {
lbl.textContent = t(isBusy ? "ui.bar.btn_preparing" : "ui.bar.btn_prepare");
}
}

function openPrePrintModal() {
clearModalFeedback();
syncEndlessSpoolUi(true);
setPrepareBtnLoading(true);
const canInstantOpen = (
hasValidLoadedGcode()
&& !currentState.last_invalidation_reason
&& !currentState.auto_slice_in_progress
&& !(currentState.stale_model_info && currentState.stale_model_info.is_stale)
);
if (typeof orca !== "undefined" && typeof orca.postMessage === "function") {
if (canInstantOpen) {
hideNoGcodeNotice();
openDrawer("prePrintModal");
renderModalJobSummary();
renderMappingTable();
}
requestStatus();
sendToHost({
type: "acquire_slicer_gcode",
gcode_path: "",
force_refresh: false,
open_preprint_on_ready: true,
refresh_live_status: true
});
return;
}
if (!hasValidLoadedGcode()) {
setPrepareBtnLoading(false);
const noticeText = t("ui.notice.no_gcode_slice_first");
showNoGcodeNotice(noticeText);
logMsg(noticeText);
return;
}
hideNoGcodeNotice();
requestStatus();
openDrawer("prePrintModal");
    renderModalJobSummary();
    renderMappingTable();
    acquireSlicerGcode(false);
setPrepareBtnLoading(false);
}

function acquireSlicerGcode(forceRefresh) {
if (forceRefresh) {
requestStatus();
}
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

function _arrayBufferToBase64(buf) {
const bytes = new Uint8Array(buf);
let binary = "";
const chunkSize = 0x8000;
for (let i = 0; i < bytes.length; i += chunkSize) {
const sub = bytes.subarray(i, i + chunkSize);
binary += String.fromCharCode.apply(null, sub);
}
return btoa(binary);
}

function handleModalGcodeFileSelect(evt) {
const file = evt && evt.target && evt.target.files ? evt.target.files[0] : null;
if (!file) {
_openModalAfterBrowse = false;
return;
}
const reader = new FileReader();
reader.onload = function(loadEvt) {
const buf = loadEvt && loadEvt.target ? loadEvt.target.result : null;
if (!buf) return;
const b64 = _arrayBufferToBase64(buf);
sendToHost({
type: "load_gcode_content",
filename: file.name || "selected.gcode",
content_base64: b64,
encoding: "base64"
});
};
reader.readAsArrayBuffer(file);
}

let isStandaloneModal = (
(typeof document !== "undefined" && document.body && document.body.classList && document.body.classList.contains("standalone-modal"))
|| (typeof window !== "undefined" && window.location && /[?&]standalone_modal=1(?:&|$)/.test(String(window.location.search || "")))
);

function closePrePrintModal(skipHostCancel) {
setPrepareBtnLoading(false);
closeThumbZoomModal();
clearHoveredExtruder();
closeAllSlotDropdowns();
const standaloneActive = (
isStandaloneModal
|| (typeof document !== "undefined" && document.body && document.body.classList && document.body.classList.contains("standalone-modal"))
);
if (standaloneActive) {
sendToHost({ type: skipHostCancel ? "close_standalone_window" : "cancel_print_setup" });
return;
}
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
.replace(/"/g, "&quot;")
.replace(/'/g, "&#39;");
}

function safeHexColor(col, fallback) {
const raw = String(col || "").trim();
if (/^#[0-9A-Fa-f]{6}$/.test(raw)) return raw.toUpperCase();
if (/^[0-9A-Fa-f]{6}$/.test(raw)) return "#" + raw.toUpperCase();
return fallback || "#64748B";
}

function parseCssColorLuminance(cssColor) {
if (!cssColor) return null;
const s = cssColor.trim().toLowerCase();
if (s === "transparent" || s === "none") return null;
let r, g, b;
const hexM = /^#([0-9a-f]{6})([0-9a-f]{2})?$/.exec(s);
if (hexM) {
if (hexM[2] && parseInt(hexM[2], 16) === 0) return null;
r = parseInt(hexM[1].slice(0, 2), 16);
g = parseInt(hexM[1].slice(2, 4), 16);
b = parseInt(hexM[1].slice(4, 6), 16);
} else {
const rgbM = /^rgba?\\(\\s*([0-9.]+)\\s*,\\s*([0-9.]+)\\s*,\\s*([0-9.]+)(?:\\s*[,\\/]\\s*([0-9.]+))?/.exec(s);
if (!rgbM) return null;
if (rgbM[4] !== undefined && Number(rgbM[4]) <= 0.01) return null;
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
if (mode === "kobraorca") {
resolved = "dark";
} else if (mode === "light" || mode === "dark") {
resolved = mode;
} else if (
html.classList.contains("orca-dark")
|| (document.body && document.body.classList.contains("orca-dark"))
|| html.getAttribute("data-orca-theme") === "dark"
) {
resolved = "dark";
} else {
const style = getComputedStyle(html);
const orcaBg = style.getPropertyValue("--orca-bg").trim()
|| style.getPropertyValue("--oui-background").trim()
|| (document.body ? getComputedStyle(document.body).backgroundColor : "");
const lum = parseCssColorLuminance(orcaBg);
if (lum !== null) {
resolved = lum > 0.5 ? "light" : "dark";
} else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
resolved = "light";
}
}
html.setAttribute("data-resolved", resolved);
[["auto", "themeBtnAuto"], ["dark", "themeBtnDark"], ["light", "themeBtnLight"], ["kobraorca", "themeBtnKobraOrca"]].forEach(pair => {
const btn = document.getElementById(pair[1]);
if (btn) btn.classList.toggle("active", mode === pair[0]);
});
}

const COLOR_SCHEMES = ["teal", "ocean", "amber", "coral", "forest", "violet", "cyan", "graphite"];

function applyColorScheme(scheme) {
const norm = COLOR_SCHEMES.includes(String(scheme || "").toLowerCase())
? String(scheme).toLowerCase()
: "teal";
document.documentElement.setAttribute("data-scheme", norm);
document.querySelectorAll(".scheme-chip[data-scheme]").forEach(btn => {
const isSel = btn.getAttribute("data-scheme") === norm;
btn.classList.toggle("active", isSel);
btn.setAttribute("aria-pressed", isSel ? "true" : "false");
});
return norm;
}

function setColorScheme(scheme, notifyHost) {
const norm = applyColorScheme(scheme);
currentState.color_scheme = norm;
try { localStorage.setItem("kobra_orca_scheme", norm); } catch (e) {}
if (notifyHost !== false) {
sendToHost({
type: "set_appearance",
theme_mode: currentState.theme_mode || "auto",
color_scheme: norm
});
}
}

function setThemeMode(mode, notifyHost) {
const norm = ["auto", "dark", "light", "kobraorca"].includes(String(mode || "").toLowerCase())
? String(mode).toLowerCase()
: "auto";
currentState.theme_mode = norm;
try { localStorage.setItem("kobra_orca_theme", norm); } catch (e) {}
applyResolvedTheme(norm);
if (notifyHost !== false) {
sendToHost({
type: "set_appearance",
theme_mode: norm,
color_scheme: currentState.color_scheme || "teal"
});
}
}

function initTheme() {
let saved = "auto";
try { saved = localStorage.getItem("kobra_orca_theme") || localStorage.getItem("kobra_ace_theme") || "auto"; } catch (e) {}
if (!["auto", "dark", "light", "kobraorca"].includes(saved)) saved = "auto";
currentState.theme_mode = saved;
applyResolvedTheme(saved);

let savedScheme = "teal";
try { savedScheme = localStorage.getItem("kobra_orca_scheme") || localStorage.getItem("kobra_ace_scheme") || "teal"; } catch (e) {}
currentState.color_scheme = applyColorScheme(savedScheme);

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

function uiSvg(name, extraClass) {
const cls = extraClass ? `ui-icon ${extraClass}` : "ui-icon";
if (name === "arrowRight") {
return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>`;
}
if (name === "check") {
return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>`;
}
if (name === "chevronDown") {
return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>`;
}
return "";
}

function filamentColorDot(slicerTool) {
const fil = (currentState.slicer_filaments || []).find(f => Number(f.slicer_tool) === Number(slicerTool));
const hex = fil ? safeHexColor(fil.color, "#64748B") : "#64748B";
return `<span class="swatch-dot" style="background:${hex};"></span>`;
}

function aceSlotColorDot(slotTool) {
const slot = (currentState.ace_slots || []).find(s => Number(s.tool) === Number(slotTool));
const isEmpty = !slot || String(slot.status || "").toLowerCase() === "empty";
if (isEmpty) {
return '<span class="swatch-dot" style="background:transparent;border-style:dashed;border-color:var(--muted);"></span>';
}
const hex = safeHexColor(slot.hex_color, "#64748B");
return `<span class="swatch-dot" style="background:${hex};"></span>`;
}

function purgeTierClass(lenMm) {
const val = Number(lenMm || 0);
if (val <= 0.05) return "tier-zero";
if (val < 30) return "tier-low";
if (val < 80) return "tier-med";
return "tier-high";
}

function getEffectiveConnectionState() {
const st = currentState.ace_status || {};
const urlStr = String(currentState.moonraker_url || "").trim();
if (!urlStr) return "unconfigured";
const rawConn = String(st.connection_state || "").trim().toLowerCase();
if (rawConn === "connected_standby" || rawConn === "connected_active" || rawConn === "offline" || rawConn === "unconfigured") {
return rawConn;
}
if (rawConn === "connected" || st.connected) {
return st.is_printing ? "connected_active" : "connected_standby";
}
return "offline";
}

function renderConnectionNotice() {
const banner = document.getElementById("connectionNoticeBanner");
const txtEl = document.getElementById("connectionNoticeText");
const retryBtn = document.getElementById("btnNoticeRetry");
if (!banner || !txtEl) return;
const cState = getEffectiveConnectionState();
if (cState === "connected_standby" || cState === "connected_active") {
banner.className = "connection-banner";
return;
}
if (cState === "offline") {
banner.className = "connection-banner is-visible is-offline";
txtEl.textContent = t("ui.notice.offline", { url: String(currentState.moonraker_url || "").trim() });
if (retryBtn) retryBtn.style.display = "inline-flex";
return;
}
banner.className = "connection-banner is-visible";
txtEl.textContent = t("ui.notice.unconfigured");
if (retryBtn) retryBtn.style.display = "none";
}

function formatAceHumidityDisplay(st) {
if (!st || typeof st !== "object") return "";
const unitMap = (st.unit_humidities && typeof st.unit_humidities === "object") ? st.unit_humidities : {};
const validEntries = Object.keys(unitMap)
.sort((a, b) => Number(a) - Number(b))
.map(k => ({ unit: Number(k) + 1, val: Number(unitMap[k]) }))
.filter(e => Number.isFinite(e.val) && e.val > 0 && e.val <= 100);
if (validEntries.length > 1) {
return validEntries.map(e => `ACE ${e.unit} ${e.val}%`).join(" · ");
}
if (validEntries.length === 1) {
return `${validEntries[0].val}%`;
}
if (st.humidity !== undefined && st.humidity !== null && typeof st.humidity !== "boolean") {
const num = Number(st.humidity);
if (Number.isFinite(num) && num > 0 && num <= 100) {
return `${num}%`;
}
}
return "";
}

function formatAceTempDisplay(st) {
if (!st || typeof st !== "object") return "";
const unitMap = (st.unit_temps && typeof st.unit_temps === "object") ? st.unit_temps : {};
const validEntries = Object.keys(unitMap)
.sort((a, b) => Number(a) - Number(b))
.map(k => ({ unit: Number(k) + 1, val: Math.round(Number(unitMap[k]) * 10) / 10 }))
.filter(e => Number.isFinite(e.val) && e.val > 0);
if (validEntries.length > 1) {
return validEntries.map(e => `ACE ${e.unit} ${e.val}°C`).join(" · ");
}
if (validEntries.length === 1) {
return `${validEntries[0].val}°C`;
}
if (st.ambient_temp !== undefined && st.ambient_temp !== null && typeof st.ambient_temp !== "boolean") {
const num = Math.round(Number(st.ambient_temp) * 10) / 10;
if (Number.isFinite(num) && num > 0) {
return `${num}°C`;
}
}
return "";
}

function formatAceDryerDisplay(st) {
if (!st || !st.dryer || !st.dryer.active) return "";
const instMap = (st.dryer.instances && typeof st.dryer.instances === "object") ? st.dryer.instances : {};
const activeEntries = Object.keys(instMap)
.sort((a, b) => Number(a) - Number(b))
.map(k => ({ unit: Number(k) + 1, data: instMap[k] }))
.filter(e => e.data && e.data.active);
if (activeEntries.length > 1) {
const parts = activeEntries.map(e => t("ui.sec2.dryer_unit_item", {
unit: e.unit,
temp: e.data.target_temp !== undefined ? e.data.target_temp : 0,
mins: e.data.remain_time_min !== undefined ? e.data.remain_time_min : 0,
}));
return t("ui.sec2.dryer_multi_badge", { units: parts.join(" · ") });
}
const instList = Array.isArray(st.dryer.active_instances) ? st.dryer.active_instances : [];
const singleEntry = activeEntries.length === 1 ? activeEntries[0] : null;
const totalUnits = Math.max(Object.keys(instMap).length, getEffectiveAceUnitCount());
const unitNum = singleEntry ? singleEntry.unit : (instList.length === 1 ? Number(instList[0]) + 1 : 0);
const unitPrefix = (unitNum > 0 && totalUnits > 1) ? `ACE ${unitNum} · ` : "";
const tempVal = singleEntry && singleEntry.data.target_temp !== undefined ? singleEntry.data.target_temp : st.dryer.target_temp;
const minsVal = singleEntry && singleEntry.data.remain_time_min !== undefined ? singleEntry.data.remain_time_min : st.dryer.remain_time_min;
return unitPrefix + t("ui.sec2.dryer_active_badge", { temp: tempVal, mins: minsVal });
}

let hardwareUnlocked = false;

function toggleHardwareLock() {
hardwareUnlocked = !hardwareUnlocked;
renderLiveHeaderAndMeta();
renderSlots();
}

function renderLiveHeaderAndMeta() {
const st = currentState.ace_status || {};
const cState = getEffectiveConnectionState();
const connBadge = document.getElementById("connBadge");
const connTile = document.getElementById("execConnTile");
const hostUrl = String(currentState.moonraker_url || "").trim();
const modelStr = String(st.model || "ACE Pro").trim();
const klipperOff = isKlipperAceDisabled();
const aceInactive = isAceHardwareInactive();
const hwState = getAceHardwareState();
if (connBadge) {
if (cState === "connected_standby" || cState === "connected_active") {
if (klipperOff) {
connBadge.textContent = t("ui.ace.off_badge");
connBadge.className = "badge info";
} else if (aceInactive) {
connBadge.textContent = hwState === "connecting" ? t("ui.ace.connecting_badge") : t("ui.ace.disconnected_badge");
connBadge.className = "badge warn";
} else {
const shortModel = modelStr.split("(")[0].trim() || "ACE Pro";
const unitCount = getEffectiveAceUnitCount();
const modelWithCount = unitCount > 1 ? `${unitCount}x${shortModel}` : shortModel;
connBadge.textContent = t("ui.conn.connected", { model: modelWithCount });
connBadge.className = "badge ready";
}
} else if (cState === "offline") {
connBadge.textContent = t("ui.conn.offline");
connBadge.className = "badge warn";
} else {
connBadge.textContent = t("ui.conn.unconfigured");
connBadge.className = "badge empty";
}
const tipParts = [modelStr, hostUrl].filter(Boolean);
const tipStr = tipParts.join(" · ");
connBadge.title = tipStr;
if (connTile) connTile.title = tipStr;
}

const isPrinting = !!st.is_printing;
if (!isPrinting && hardwareUnlocked) {
hardwareUnlocked = false;
}
const headerPrintBadge = document.getElementById("headerPrintBadge");
const modalPrinterBadge = document.getElementById("modalPrinterBadge");
if (headerPrintBadge) {
if (cState === "connected_standby" || cState === "connected_active") {
headerPrintBadge.textContent = t("ui.printer.state", { state: st.print_state || "standby" });
headerPrintBadge.className = "badge " + (isPrinting ? "warn" : "ready");
} else {
headerPrintBadge.textContent = t("ui.printer.standby");
headerPrintBadge.className = "badge empty";
}
if (modalPrinterBadge) {
modalPrinterBadge.textContent = headerPrintBadge.textContent;
modalPrinterBadge.className = headerPrintBadge.className;
}
}
const extManual = !klipperOff && !!currentState.external_spool_mode;
const modalAceBadge = document.getElementById("modalAceModeBadge");
if (modalAceBadge) {
if (klipperOff || extManual) {
modalAceBadge.textContent = klipperOff ? t("ui.ace.off_badge") : t("ui.modal.map.ext_spool_badge");
modalAceBadge.style.display = "inline-flex";
} else {
modalAceBadge.textContent = "";
modalAceBadge.style.display = "none";
}
}
const acePinBtn = document.getElementById("btnToggleAceProPin");
if (acePinBtn) {
const onlineNow = (cState === "connected_standby" || cState === "connected_active");
const tipTxt = klipperOff ? t("ui.ace.btn_turn_on") : t("ui.ace.btn_turn_off");
acePinBtn.style.display = onlineNow ? "inline-flex" : "none";
acePinBtn.title = tipTxt;
if (acePinBtn.setAttribute) acePinBtn.setAttribute("aria-label", tipTxt);
if (acePinBtn.classList) acePinBtn.classList.toggle("is-on", !klipperOff);
acePinBtn.disabled = !onlineNow || (isPrinting && !hardwareUnlocked);
}
const dryerStripEl = document.querySelector ? document.querySelector(".bottom-control-strip") : null;
if (dryerStripEl && dryerStripEl.classList) {
dryerStripEl.classList.toggle("is-ace-inactive", aceInactive);
dryerStripEl.querySelectorAll("button, input").forEach(el => { el.disabled = aceInactive; });
}
syncExternalSpoolUi();

const banner = document.getElementById("printSafetyBanner");
const bannerState = document.getElementById("printSafetyState");
const bannerText = document.getElementById("printSafetyText");
const btnUnlock = document.getElementById("btnToggleHardwareLock");
if (banner) {
banner.style.display = isPrinting ? "inline-flex" : "none";
banner.classList.toggle("is-unlocked", !!(isPrinting && hardwareUnlocked));
const detailStr = st.print_filename ? `${st.print_state}: ${st.print_filename}` : (st.print_state || "printing");
banner.title = detailStr;
if (bannerState) {
bannerState.textContent = detailStr;
}
if (bannerText) {
bannerText.textContent = hardwareUnlocked ? t("ui.safety.unlocked_text") : t("ui.safety.locked_text");
}
if (btnUnlock) {
btnUnlock.textContent = hardwareUnlocked ? t("ui.safety.btn_lock") : t("ui.safety.btn_unlock");
}
}
const slotsElNow = document.getElementById("slotsContainer");
if (slotsElNow && slotsElNow.classList) {
slotsElNow.classList.toggle("is-ace-inactive", aceInactive);
}
document.querySelectorAll(".hw-action-btn, .slot-actions button").forEach(btn => {
btn.disabled = aceInactive || (isPrinting && !hardwareUnlocked);
});

if (!currentState.ace_status) return;
const activeToolStr = st.current_index >= 0 ? `T${st.current_index}` : t("ui.exec.tool.none");
const printLabel = st.print_state ? t("ui.sec1.meta_print", { state: st.print_state }) : "";
const humText = formatAceHumidityDisplay(st);
const metaEl = document.getElementById("deviceMeta");
if (metaEl) {
let metaStr = t("ui.sec1.meta_live", {
status: st.device_status || "—",
tool: activeToolStr,
temp: st.ambient_temp !== undefined ? st.ambient_temp : "—",
fan: st.fan_speed !== undefined ? st.fan_speed : "—",
print_label: printLabel,
});
if (humText) {
metaStr = `${metaStr} | ${t("ui.exec.tool.hum_part", { hum: humText })}`;
}
metaEl.textContent = metaStr;
}

const dryerBadge = document.getElementById("dryerBadge");
if (dryerBadge) {
const dryerDisplay = formatAceDryerDisplay(st);
if (dryerDisplay) {
dryerBadge.textContent = dryerDisplay;
dryerBadge.className = "badge drying";
} else {
dryerBadge.textContent = t("ui.sec2.dryer_stop_badge");
dryerBadge.className = "badge empty";
}
}

const dryerTempBadge = document.getElementById("dryerTempBadge");
if (dryerTempBadge) {
const tempDisplay = formatAceTempDisplay(st);
if (tempDisplay) {
dryerTempBadge.textContent = t("ui.exec.tool.temp_part", { temp: tempDisplay });
dryerTempBadge.style.display = "inline-flex";
} else {
dryerTempBadge.textContent = "";
dryerTempBadge.style.display = "none";
}
}

const dryerHumBadge = document.getElementById("dryerHumidityBadge");
if (dryerHumBadge) {
if (humText) {
dryerHumBadge.textContent = t("ui.exec.tool.hum_part", { hum: humText });
dryerHumBadge.style.display = "inline-flex";
} else {
dryerHumBadge.textContent = "";
dryerHumBadge.style.display = "none";
}
}
updateModalSendActionUi();
}

function renderExecutiveStrip() {
const st = currentState.ace_status || {};
const info = currentState.last_postprocess_info || {};
const cState = getEffectiveConnectionState();

const connVal = document.getElementById("execConnVal");
const connSub = document.getElementById("execConnSub");
if (connVal && connSub) {
if (cState === "connected_standby" || cState === "connected_active") {
connVal.textContent = "";
connVal.style.display = "none";
connSub.textContent = "";
connSub.style.display = "none";
} else if (cState === "offline") {
connVal.textContent = t("ui.exec.conn.offline");
connVal.style.display = "";
connSub.textContent = String(currentState.moonraker_url || "");
connSub.style.display = "none";
} else {
connVal.textContent = t("ui.exec.conn.unconfigured");
connVal.style.display = "";
connSub.textContent = t("ui.exec.conn.unconfigured_sub");
connSub.style.display = "";
}
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
tc: computeEffectiveToolchanges(info),
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

const _hexToHslCache = new Map();
function hexToHslCached(hexStr) {
const key = String(hexStr || "").trim().toUpperCase();
if (_hexToHslCache.has(key)) return _hexToHslCache.get(key);
const rgb = hexToRgbObj(key, "#64748B");
const hsl = rgbToHsl(rgb.r, rgb.g, rgb.b);
if (_hexToHslCache.size > 256) _hexToHslCache.clear();
_hexToHslCache.set(key, hsl);
return hsl;
}

function expectedShadedLightness(lVal) {
if (lVal < 0.15) return 0.13 + lVal * 0.5;
if (lVal > 0.85) return 0.72 + (lVal - 0.85) * 0.2;
return lVal * 0.85;
}

function hasFilamentUsageData(filaments) {
return Array.isArray(filaments) && filaments.some(f => (
Number(f.used_g || 0) > 0
|| Number(f.used_mm || 0) > 0
|| Number(f.used_m || 0) > 0
|| Number(f.used_cm3 || 0) > 0
));
}

function getActiveSlicerPalette() {
const filaments = Array.isArray(currentState.slicer_filaments) ? currentState.slicer_filaments : [];
const hasAnyUsageData = hasFilamentUsageData(filaments);
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

const applyModeBtnText = (el) => {
if (!el) return;
const lbl = el.querySelector(".btn-label");
if (lbl) {
lbl.setAttribute("data-i18n", key);
lbl.textContent = label;
} else {
el.setAttribute("data-i18n", key);
el.textContent = label;
}
el.classList.toggle("is-original", !!thumbShowOriginal);
};

if (zoomBtn) {
zoomBtn.style.display = hasThumb ? "inline-flex" : "none";
}
applyModeBtnText(modalModeBtn);
if (!btn) return;
if (!hasThumb) {
btn.style.display = "none";
closeThumbZoomModal();
return;
}
btn.style.display = "inline-flex";
applyModeBtnText(btn);
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
targetByTool[sTool] = isValidSlot ? resolveSlotColorHex(assignedSlot, entry.hex) : entry.hex;
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
if (stale && stale.is_stale && hasPath && (stale.active_model || stale.reason)) {
const meta = (currentState.last_postprocess_info && currentState.last_postprocess_info.job_metadata)
|| currentState.last_job_metadata || {};
const loadedFile = stale.loaded_file
|| meta.filename
|| String(currentState.last_gcode_path || "").split(/[\\\\/]/).pop()
|| "";
const reasonCode = String(stale.reason || "").trim();
let msg = t("ui.modal.stale.banner", {
file: loadedFile,
model: stale.active_model || ""
});
if (reasonCode && reasonCode !== "model_mismatch") {
const reasonKey = "stale.reason." + reasonCode;
const reasonText = t(reasonKey);
const label = reasonText !== reasonKey ? reasonText : reasonCode;
msg = label + (loadedFile ? " (" + loadedFile + ")" : "");
}
textEl.textContent = msg;
banner.classList.add("visible");
} else {
textEl.textContent = "";
banner.classList.remove("visible");
}
}

function resetMismatchConfirmationUi() {
_localMismatchAck = false;
_localMismatchAckContextKey = "";
currentState.material_mismatch_confirmed = false;
const gateChk = document.getElementById("chkConfirmMaterialMismatch");
if (gateChk) gateChk.checked = false;
}

function clearModalGcode() {
currentState.last_gcode_path = "";
currentState.last_gcode_source = "";
currentState.last_job_metadata = {};
currentState.last_postprocess_info = {};
currentState.mapping_warnings = [];
currentState.stale_model_info = { is_stale: false, active_model: "", loaded_file: "", reason: "" };
const gcodeInput = document.getElementById("gcodePathInput");
if (gcodeInput) gcodeInput.value = "";
resetMismatchConfirmationUi();
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
const displayBase = base ? base.replace(/\\.(?:gcode\\.3mf|gcode|gco|gc)$/i, "") : "";
if (fileEl) {
fileEl.textContent = displayBase || t("ui.modal.job.no_file");
fileEl.title = base || "";
}
const zoomFileEl = document.getElementById("zoomThumbFilename");
if (zoomFileEl) {
zoomFileEl.textContent = displayBase || base || t("ui.modal.job.no_file");
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
wgEl.textContent = t("ui.modal.job.weight", { weight: wg });
}

const pgGEl = document.getElementById("modalJobPurgeG");
if (pgGEl) {
let purgeStr = "0.0 g";
const pred = (typeof computePreprintFilamentPredictionLocal === "function")
? computePreprintFilamentPredictionLocal()
: null;
if (pred && Array.isArray(pred.slots) && pred.slots.length > 0) {
let sumPurge = 0;
let allKnown = true;
pred.slots.forEach(sl => {
if (sl && sl.purge_usage_g !== null && sl.purge_usage_g !== undefined && Number.isFinite(Number(sl.purge_usage_g))) {
sumPurge += Number(sl.purge_usage_g);
} else {
allKnown = false;
}
});
if (allKnown) {
purgeStr = sumPurge.toFixed(1) + " g";
} else {
const evs = Array.isArray(info.events) ? info.events : [];
if (evs.length === 0 && !(pred.startup_purge_estimate && pred.startup_purge_estimate.active)) purgeStr = "0.0 g";
else purgeStr = sumPurge > 0 ? ("~" + sumPurge.toFixed(1) + " g") : "—";
}
const stEst = pred.startup_purge_estimate;
if (stEst && stEst.active && Number(stEst.length_mm || 0) > 0) {
purgeStr += " (" + t("ui.modal.job.purge_startup_added", { len: Math.round(Number(stEst.length_mm)) }) + ")";
} else if (stEst && stEst.skipped) {
purgeStr += " (" + t("ui.modal.job.purge_startup_skipped") + ")";
}
} else if (Number(info.total_purge_volume_mm3 || 0) <= 0) {
purgeStr = "0.0 g";
} else {
purgeStr = "—";
}
pgGEl.textContent = t("ui.modal.job.purge_g", { purge: purgeStr });
}

const pgEl = document.getElementById("modalJobPurge");
if (pgEl) {
const tc = computeEffectiveToolchanges(info);
pgEl.textContent = t("ui.modal.job.toolchanges", { tc: tc });
}
const clearBtn = document.getElementById("btnClearModalGcode");
if (clearBtn) {
clearBtn.style.display = path ? "inline-flex" : "none";
}
renderModalStaleWarning();
}

function resolveSlotColorHex(slotObj, fallbackHex) {
const fb = fallbackHex || "#64748B";
if (!slotObj || typeof slotObj !== "object") return fb;
const sp = (slotObj.spoolman && typeof slotObj.spoolman === "object") ? slotObj.spoolman : null;
if (sp && !sp.conflict && sp.color_hex && /^#?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$/.test(String(sp.color_hex).trim())) {
return safeHexColor(sp.color_hex, fb);
}
return safeHexColor(slotObj.hex_color, fb);
}

function classifyGermanColorName(hexColor, spoolColorText) {
const explicit = String(spoolColorText || "").trim();
if (explicit && !/^#?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$/.test(explicit)) {
return explicit;
}
const norm = safeHexColor(hexColor, "");
if (!norm) return "";
const rgb = hexToRgbObj(norm);
const diff = (Math.max(rgb.r, rgb.g, rgb.b) - Math.min(rgb.r, rgb.g, rgb.b)) / 255;
const hsl = hexToHslCached(norm);
const hue = ((hsl.h * 360) % 360 + 360) % 360;
const sat = hsl.s;
const lum = hsl.l;

if (sat < 0.14 || diff < 0.08 || (sat < 0.22 && hue >= 185 && hue <= 245 && lum > 0.16 && lum < 0.68)) {
if (lum <= 0.18) return "Schwarz";
if (lum >= 0.88) return "Weiß";
if (lum >= 0.66) return "Silber";
return "Grau";
}
if (lum <= 0.11) return "Schwarz";
if (hue >= 25 && hue <= 65 && lum >= 0.65 && sat <= 0.68) return "Beige";
if (lum >= 0.94 && sat < 0.35) return "Weiß";

if (hue >= 10 && hue < 18) return (lum < 0.32 || sat < 0.45) ? "Braun" : "Rot";
if (hue >= 18 && hue < 40) {
if (lum < 0.42 || (sat < 0.55 && lum < 0.58)) return "Braun";
if (lum >= 0.68) return "Beige";
return "Orange";
}
if (hue >= 40 && hue < 52) {
if ((lum <= 0.525 && sat <= 0.92) || (hue >= 48 && hue <= 52 && lum <= 0.52)) return "Gold";
if (lum < 0.34) return "Braun";
if (lum >= 0.72 && sat < 0.65) return "Beige";
return "Gelb";
}
if (hue >= 52 && hue < 68) {
if (lum < 0.30) return "Braun";
if (lum >= 0.68 && sat <= 0.65) return "Beige";
return "Gelb";
}
if (hue >= 68 && hue < 158) return "Grün";
if (hue >= 158 && hue < 196) return "Türkis";
if (hue >= 196 && hue < 258) return "Blau";
if (hue >= 258 && hue < 298) return "Violett";
if (hue >= 298 && hue < 342) return "Pink";
if (lum >= 0.68) return "Pink";
if (lum < 0.24 && sat < 0.55) return "Braun";
return "Rot";
}

const COLOR_NAME_TRANSLATIONS = __COLOR_NAME_I18N_JSON__;

function translateColorName(nameStr, targetLang) {
const raw = String(nameStr || "").trim();
if (!raw) return "";
const lang = (targetLang || currentLanguage || "en") === "de" ? "de" : "en";
const hit = COLOR_NAME_TRANSLATIONS[raw.toLowerCase()];
return hit ? (hit[lang] || raw) : raw;
}

function getReadableColorName(hexColor, spoolColorText) {
const rawName = classifyGermanColorName(hexColor, spoolColorText);
if (!rawName) return "";
return translateColorName(rawName, currentLanguage);
}

function computeColorLuminance(hexColor) {
const norm = safeHexColor(hexColor, "#475569").slice(1);
const r = parseInt(norm.slice(0, 2), 16);
const g = parseInt(norm.slice(2, 4), 16);
const b = parseInt(norm.slice(4, 6), 16);
return 0.299 * r + 0.587 * g + 0.114 * b;
}

function computeRelativeLuminance(hexColor) {
return computeColorLuminance(hexColor) / 255.0;
}

function computeContrastBorderColor(hexColor) {
const lum = computeColorLuminance(hexColor);
if (lum > 185) return "#64748B";
if (lum < 55) return "#94A3B8";
return "rgba(100, 116, 139, 0.58)";
}

function computeSlotVisualFill(slot, spInfo, isEmpty) {
const effHex = resolveSlotColorHex(slot, "#64748B");
const wStatus = (spInfo && !spInfo.conflict) ? String(spInfo.weight_status || "") : "";
const rawPct = (spInfo && !spInfo.conflict) ? spInfo.fill_percent : null;
const hasKnownPct = (rawPct !== null && rawPct !== undefined && Number.isFinite(Number(rawPct)));
const clampedPct = hasKnownPct ? Math.max(0, Math.min(100, Number(rawPct))) : null;

if (isEmpty) {
return { mode: "slot_empty", hex: effHex, fillPct: 0, isKnown: false, weightStatus: "slot-empty" };
}
if (wStatus === "empty" || (clampedPct !== null && clampedPct <= 0)) {
return { mode: "empty_spool", hex: effHex, fillPct: 0, isKnown: true, weightStatus: "empty" };
}
if (clampedPct !== null) {
return { mode: "known", hex: effHex, fillPct: clampedPct, isKnown: true, weightStatus: wStatus || "ok" };
}
return { mode: "unknown", hex: effHex, fillPct: null, isKnown: false, weightStatus: wStatus || "ace" };
}

function renderSlotColorBar(vis, titleText) {
const hex = escapeHtml(vis.hex);
if (vis.mode === "slot_empty") {
return `<div class="slot-color-bar is-slot-empty" data-fill-mode="slot_empty" data-fill-pct="0" title="${escapeHtml(titleText || "")}"></div>`;
}
if (vis.mode === "empty_spool") {
return `<div class="slot-color-bar is-empty-spool" data-fill-mode="empty_spool" data-fill-pct="0" style="border-left:3px solid ${hex};" title="${escapeHtml(titleText || "")}"><div class="slot-color-bar-fill" style="width:0%;background:${hex};"></div></div>`;
}
const borderCol = escapeHtml(computeContrastBorderColor(vis.hex));
if (vis.mode === "known") {
const wPct = Math.max(0, Math.min(100, Number(vis.fillPct)));
return `<div class="slot-color-bar is-known" data-fill-mode="known" data-fill-pct="${Math.round(wPct)}" title="${escapeHtml(titleText || "")}"><div class="slot-color-bar-fill" style="width:${wPct.toFixed(1)}%;background:${hex};box-shadow:inset 0 0 0 1px ${borderCol};"></div></div>`;
}
return `<div class="slot-color-bar is-unknown" data-fill-mode="unknown" data-fill-pct="unknown" title="${escapeHtml(titleText || "")}"><div class="slot-color-bar-fill" style="width:100%;background:${hex};box-shadow:inset 0 0 0 1px ${borderCol};"></div></div>`;
}

function renderSlotSpoolSvg(visOrPct, hexColor, isEmptyArg, weightStatusArg) {
const vis = (visOrPct && typeof visOrPct === "object")
? visOrPct
: computeSlotVisualFill(
{ hex_color: hexColor },
{ fill_percent: visOrPct, weight_status: weightStatusArg },
!!isEmptyArg
);
const CX = 22;
const CY = 22;
const R_CORE = 6.5;
const R_MAX = 18.0;
const effHex = vis.hex;
const lum = computeColorLuminance(effHex);
const isEmpty = vis.mode === "slot_empty";
const wStatus = vis.weightStatus;
const clampedPct = (vis.mode === "empty_spool") ? 0 : (vis.mode === "known" ? vis.fillPct : null);

let drawWinding = false;
let rFilOuter = R_CORE;
let pctAttr = "unknown";
if (isEmpty || vis.mode === "empty_spool" || (clampedPct !== null && clampedPct <= 0)) {
drawWinding = false;
rFilOuter = R_CORE;
pctAttr = "0";
} else if (clampedPct !== null) {
drawWinding = true;
const frac = Math.max(0.10, clampedPct / 100);
rFilOuter = R_CORE + (R_MAX - R_CORE) * frac;
pctAttr = String(Math.round(clampedPct));
} else {
drawWinding = true;
rFilOuter = R_CORE + (R_MAX - R_CORE) * 0.72;
pctAttr = "unknown";
}

let windingSvg = "";
if (drawWinding) {
const bandWidth = Math.max(1.2, rFilOuter - R_CORE);
const midR = R_CORE + bandWidth / 2;
const edgeStroke = lum > 185 ? "#64748B" : (lum < 55 ? "#94A3B8" : "rgba(15, 23, 42, 0.45)");
const strandStroke = lum < 55 ? "rgba(255, 255, 255, 0.18)" : (lum > 185 ? "rgba(15, 23, 42, 0.14)" : "rgba(255, 255, 255, 0.22)");
windingSvg = `<circle class="spool-svg-winding" cx="${CX}" cy="${CY}" r="${midR.toFixed(2)}" fill="none" stroke="${escapeHtml(effHex)}" stroke-width="${bandWidth.toFixed(2)}" />`
+ (bandWidth >= 4.2 ? `<circle cx="${CX}" cy="${CY}" r="${midR.toFixed(2)}" fill="none" stroke="${strandStroke}" stroke-width="0.9" />` : "")
+ `<circle cx="${CX}" cy="${CY}" r="${rFilOuter.toFixed(2)}" fill="none" stroke="${edgeStroke}" stroke-width="1.1" />`;
}
const hubStrokeAttr = (drawWinding && lum < 55) ? ' stroke="#94A3B8"' : "";
const flangeCls = isEmpty ? "spool-svg-flange is-empty" : "spool-svg-flange";

return `<svg class="slot-spool-svg" viewBox="0 0 44 44" data-fill-pct="${escapeHtml(pctAttr)}" data-fill-mode="${escapeHtml(vis.mode)}" data-weight-status="${escapeHtml(wStatus)}" data-winding-radius="${rFilOuter.toFixed(2)}" data-color-hex="${escapeHtml(effHex)}" aria-hidden="true">`
+ `<circle class="${flangeCls}" cx="${CX}" cy="${CY}" r="20.5" />`
+ `<line class="spool-svg-spoke" x1="22" y1="3.5" x2="22" y2="15.5" />`
+ `<line class="spool-svg-spoke" x1="22" y1="28.5" x2="22" y2="40.5" />`
+ `<line class="spool-svg-spoke" x1="3.5" y1="22" x2="15.5" y2="22" />`
+ `<line class="spool-svg-spoke" x1="28.5" y1="22" x2="40.5" y2="22" />`
+ windingSvg
+ `<circle class="spool-svg-hub" cx="${CX}" cy="${CY}" r="${R_CORE}"${hubStrokeAttr} />`
+ `<circle class="spool-svg-hole" cx="${CX}" cy="${CY}" r="3.1" />`
+ `</svg>`;
}

function formatSpoolCandidateLabel(cand) {
if (!cand || typeof cand !== "object") return "";
const parts = [`#${cand.spool_id}`];
if (cand.remaining_weight !== null && cand.remaining_weight !== undefined && Number.isFinite(Number(cand.remaining_weight))) {
const rem = Number(cand.remaining_weight);
const remStr = (Math.abs(rem - Math.round(rem)) < 0.05) ? `${Math.round(rem)} g` : `${rem.toFixed(1)} g`;
parts.push(remStr);
} else {
parts.push(t("ui.slot.weight_missing"));
}
if (cand.location) parts.push(String(cand.location));
let txt = parts.join(" – ");
if (cand.last_used_display) txt += ` · ${cand.last_used_display}`;
return txt;
}

function formatSlotSpoolmanMeta(spInfo, fallbackSku) {
if (!spInfo || typeof spInfo !== "object") {
return { text: "", sourceLine: "", shortWeight: "", title: "", cssClass: "slot-spoolman-meta", hasSpoolman: false, isConflict: false };
}
if (spInfo.conflict || spInfo.status === "conflict") {
const cnt = Number(spInfo.conflict_count || 2);
const skuTxt = String(spInfo.sku || fallbackSku || "");
const candList = Array.isArray(spInfo.candidates)
? spInfo.candidates.map(c => (c.spool_id ? `#${c.spool_id} ` : "") + (c.display_name || "")).filter(Boolean).join(", ")
: "";
const txt = t("ui.slot.spoolman_conflict", { count: cnt, sku: skuTxt });
return {
text: txt,
sourceLine: "",
shortWeight: t("ui.modal.card.conflict_badge", { count: cnt }),
title: candList ? `${txt} (${candList})` : txt,
cssClass: "slot-spoolman-meta is-conflict",
hasSpoolman: true,
isConflict: true
};
}
const spName = String(spInfo.display_name || spInfo.filament_name || "").trim();
const wStatus = String(spInfo.weight_status || "");
const remRaw = spInfo.remaining_weight;
const remNum = (remRaw !== null && remRaw !== undefined && Number.isFinite(Number(remRaw))) ? Number(remRaw) : null;
const pctRaw = spInfo.fill_percent;
const pctNum = (pctRaw !== null && pctRaw !== undefined && Number.isFinite(Number(pctRaw))) ? Math.round(Number(pctRaw)) : null;

let weightText = "";
let shortWeight = "";
let extraCls = "";
if (wStatus === "empty" || (remNum !== null && remNum <= 0)) {
weightText = t("ui.slot.weight_empty_pct");
shortWeight = "0.0g (0%)";
extraCls = " is-empty";
} else if (wStatus === "missing" || remNum === null) {
weightText = t("ui.slot.weight_missing");
shortWeight = t("ui.slot.weight_missing");
extraCls = " is-missing";
} else if (pctNum !== null) {
const gStr = remNum.toFixed(1);
weightText = t("ui.slot.remaining_pct", { grams: gStr, pct: pctNum });
shortWeight = `${gStr}g (${pctNum}%)`;
} else {
const gStr = remNum.toFixed(1);
weightText = t("ui.slot.remaining", { grams: gStr });
shortWeight = `${gStr}g`;
}

let sourceLine = "";
if (spInfo.spool_id !== null && spInfo.spool_id !== undefined) {
const sid = String(spInfo.spool_id);
if (spInfo.assignment_source === "manual") {
sourceLine = t("ui.slot.spool_manual", { id: sid });
} else if (spInfo.match_reason === "last_used") {
sourceLine = t("ui.slot.spool_auto_last", { id: sid });
} else if (spInfo.match_reason === "fallback") {
sourceLine = t("ui.slot.spool_auto_fb", { id: sid });
} else {
sourceLine = t("ui.slot.spool_auto_single", { id: sid });
}
}

const fullText = [spName, weightText].filter(Boolean).join(" · ");
const fullTitle = [sourceLine, fullText, spInfo.location || ""].filter(Boolean).join(" · ");
return {
text: fullText,
sourceLine: sourceLine,
shortWeight: shortWeight,
title: fullTitle,
cssClass: `slot-spoolman-meta${extraCls}`,
hasSpoolman: true,
isConflict: false
};
}

function toggleCustomSelectDropdown(dd, trig, menu, event) {
if (event) event.stopPropagation();
if (!dd) return;
const willOpen = !dd.classList.contains("open");
closeAllSlotDropdowns(dd);
const card = dd.closest(".slot-card");
if (!willOpen) {
dd.classList.remove("open", "drop-up");
if (card) card.classList.remove("has-open-menu");
if (trig) trig.setAttribute("aria-expanded", "false");
return;
}
dd.classList.add("open");
dd.classList.remove("drop-up");
if (card) card.classList.add("has-open-menu");
if (trig) trig.setAttribute("aria-expanded", "true");
if (menu) {
const mRect = menu.getBoundingClientRect();
if (mRect.bottom > (window.innerHeight || document.documentElement.clientHeight) - 8) {
dd.classList.add("drop-up");
}
}
}

function applyCustomSelectValue(ddId, lblId, val, i18nKey) {
const dd = document.getElementById(ddId);
const lbl = document.getElementById(lblId);
if (dd) {
dd.dataset.value = val;
dd.value = val;
dd.querySelectorAll(".custom-select-option").forEach(opt => {
const isSel = String(opt.getAttribute("data-value") || "") === val;
opt.classList.toggle("is-selected", isSel);
opt.setAttribute("aria-selected", isSel ? "true" : "false");
});
}
if (lbl) {
lbl.setAttribute("data-i18n", i18nKey);
lbl.textContent = t(i18nKey);
}
}

function toggleSlotSpoolDropdown(trigEl, event) {
const dd = trigEl ? trigEl.closest(".slot-spool-picker") : null;
toggleCustomSelectDropdown(dd, trigEl, dd ? dd.querySelector(".custom-select-menu") : null, event);
}

function selectSlotSpool(slotTool, spoolIdOrAuto, skuStr, event) {
if (event) event.stopPropagation();
closeAllSlotDropdowns();
sendToHost({
type: "select_slot_spool",
tool: Number(slotTool),
spool_id: spoolIdOrAuto === "auto" ? null : Number(spoolIdOrAuto),
sku: String(skuStr || "")
});
}

function renderSlots() {
const container = document.getElementById("slotsContainer");
if (!container) return;
container.innerHTML = "";
const unitCount = getEffectiveAceUnitCount();
const maxSlot = unitCount * 4;
const titleEl = document.getElementById("slotBayTitle");
if (titleEl) {
titleEl.textContent = t("ui.sec3.title_dynamic", { max_tool: maxSlot - 1 });
}
const rawSlots = Array.isArray(currentState.ace_slots) ? currentState.ace_slots : [];
const slots = Array.from({ length: maxSlot }, (_, i) => {
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
const aceInactive = isAceHardwareInactive();
if (container.classList) container.classList.toggle("is-ace-inactive", aceInactive);
const disAttr = (aceInactive || (isPrinting && !hardwareUnlocked)) ? "disabled" : "";

slots.forEach(slot => {
const rawStatus = String(slot.status || "empty").toLowerCase();
const hwRawStatus = String(slot.raw_status || rawStatus).toLowerCase();
const isEmpty = rawStatus === "empty" || hwRawStatus === "empty";
const isLoaded = !!(slot.is_loaded || rawStatus === "loaded");
const isFeedAssist = !!slot.feed_assist;
const isShifting = !!(slot.is_shifting || hwRawStatus === "shifting" || rawStatus === "shifting");
const isHwDynamic = ["shifting", "feeding", "unwinding", "busy", "preload", "identifying", "gear_err", "tmc_err"].includes(hwRawStatus);
const matRaw = String(slot.material || "").trim();
const isUnconfigured = !isEmpty && !isLoaded && !matRaw;

const cardClass = isLoaded
? "slot-loaded"
: (isEmpty ? "slot-empty" : (isUnconfigured ? "slot-unconfigured" : ""));
const badgeClass = isLoaded
? "loaded"
: (isShifting ? "shifting" : (isHwDynamic ? "warn" : (rawStatus === "ready" ? "ready" : "empty")));
const badgeLabel = isLoaded
? t("ui.slot.badge_active")
: (isShifting
? t("ui.slot.badge_shifting")
: (isHwDynamic ? hwRawStatus.toUpperCase() : (rawStatus === "ready" ? t("ui.slot.badge_ready") : (isEmpty ? t("ui.slot.badge_empty") : rawStatus))));
const extraHwBadge = (isLoaded && (isShifting || isHwDynamic))
? `<span class="badge ${isShifting ? "shifting" : "warn"}">${escapeHtml(isShifting ? t("ui.slot.badge_shifting") : hwRawStatus.toUpperCase())}</span>`
: "";

const spInfo = (slot.spoolman && typeof slot.spoolman === "object") ? slot.spoolman : null;
const vis = computeSlotVisualFill(slot, spInfo, isEmpty);
const hexCol = vis.hex;
const matText = matRaw || (isEmpty ? t("ui.slot.no_filament") : t("ui.slot.unconfigured"));
const tempText = slot.temp ? `${Number(slot.temp)}°C` : "—";
const brandSku = [slot.brand, slot.sku].filter(Boolean).join(" · ");
const spMeta = formatSlotSpoolmanMeta(spInfo, slot.sku);
const colorName = !isEmpty
? getReadableColorName(hexCol, (spInfo && !spInfo.conflict) ? (spInfo.color_name || spInfo.color_text || "") : "")
: "";
const colorBarTitle = (colorName ? `${colorName} (${hexCol})` : hexCol)
+ (vis.isKnown ? ` · ${Math.round(Number(vis.fillPct))}%` : "");
const colorBarHtml = renderSlotColorBar(vis, colorBarTitle);
const colorMetaHtml = isEmpty
? escapeHtml(tempText)
: `<span title="${escapeHtml(hexCol)}">${escapeHtml(colorName || hexCol)}</span> · ${escapeHtml(tempText)}`;
const spoolSvgHtml = renderSlotSpoolSvg(vis);
const rfidBadge = slot.rfid
? `<span class="badge info" title="${escapeHtml(brandSku || t('ui.slot.rfid_detected'))}">RFID</span>`
: "";
const slotNum = Number(slot.tool);

let spoolDropdownHtml = "";
if (!isEmpty && spInfo && Array.isArray(spInfo.candidates) && spInfo.candidates.length > 1) {
const curSid = spInfo.spool_id;
const curCand = spInfo.candidates.find(c => Number(c.spool_id) === Number(curSid)) || spInfo.candidates[0];
const trigTxt = formatSpoolCandidateLabel(curCand);
const skuVal = String(spInfo.sku || slot.sku || "");
const skuArg = escapeHtml(JSON.stringify(skuVal));
let optsHtml = spInfo.candidates.map(cand => {
const isSel = Number(cand.spool_id) === Number(curSid);
const lbl = formatSpoolCandidateLabel(cand);
return `<button type="button" role="option" aria-selected="${isSel ? "true" : "false"}" class="custom-select-option${isSel ? " is-selected" : ""}" onclick="selectSlotSpool(${slotNum}, ${Number(cand.spool_id)}, ${skuArg}, event)">${escapeHtml(lbl)}</button>`;
}).join("");
if (spInfo.assignment_source === "manual") {
optsHtml += `<button type="button" role="option" aria-selected="false" class="custom-select-option" onclick="selectSlotSpool(${slotNum}, 'auto', ${skuArg}, event)">${escapeHtml(t("ui.slot.spool_opt_auto"))}</button>`;
}
spoolDropdownHtml = `
<div class="custom-select slot-spool-picker" data-slot="${slotNum}">
<button type="button" class="custom-select-trigger" onclick="toggleSlotSpoolDropdown(this, event)" aria-haspopup="listbox" aria-expanded="false" aria-label="${escapeHtml(t('ui.slot.spool_select_aria', { tool: slotNum }))}">
<span class="custom-select-label">${escapeHtml(trigTxt)}</span>
<span class="custom-select-caret" aria-hidden="true">▾</span>
</button>
<div class="custom-select-menu" role="listbox">${optsHtml}</div>
</div>
`;
}

const assistAction = isFeedAssist ? "feed_assist_off" : "feed_assist_on";
const assistBtnClass = isFeedAssist ? "btn-assist-on" : "btn-assist-off";
const assistBtnLabel = isFeedAssist ? t("ui.slot.btn_assist_on") : t("ui.slot.btn_assist_off");
const assistBtnTitle = isFeedAssist
? t("ui.slot.title_assist_on", { tool: slotNum })
: t("ui.slot.title_assist_off", { tool: slotNum });

const card = document.createElement("div");
card.className = `slot-card ${cardClass}`.trim();
card.innerHTML = `
<div class="slot-header">
<span class="slot-id">${escapeHtml(slot.label || ('T' + slotNum))} ${rfidBadge}</span>
<span style="display:inline-flex;align-items:center;gap:4px;">${extraHwBadge}<span class="badge ${badgeClass}">${escapeHtml(badgeLabel)}</span></span>
</div>
${colorBarHtml}
<div class="swatch-row">
${spoolSvgHtml}
<div style="min-width:0;flex:1;">
<div style="font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(matText)}</div>
<div class="slot-meta">${colorMetaHtml}</div>
${brandSku ? `<div class="slot-meta" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(brandSku)}">${escapeHtml(brandSku)}</div>` : ""}
${spMeta.sourceLine ? `<div class="slot-meta" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(spMeta.title)}">${escapeHtml(spMeta.sourceLine)}</div>` : ""}
${spMeta.text ? `<div class="${spMeta.cssClass}" title="${escapeHtml(spMeta.title)}">${escapeHtml(spMeta.text)}</div>` : ""}
</div>
</div>
${spoolDropdownHtml}
<div class="slot-actions">
<button ${disAttr} onclick="slotAction('load', ${slotNum})" title="${escapeHtml(t('ui.slot.title_load', { tool: slotNum }))}">${escapeHtml(t('ui.slot.btn_load'))}</button>
<button ${disAttr} onclick="slotAction('unload', ${slotNum})" title="${escapeHtml(t('ui.slot.title_unload', { tool: slotNum }))}">${escapeHtml(t('ui.slot.btn_unload'))}</button>
<button ${disAttr} class="btn-full-unload" onclick="confirmFullUnload(${slotNum})" title="${escapeHtml(t('ui.slot.title_full_unload', { tool: slotNum }))}">${escapeHtml(t('ui.slot.btn_full_unload'))}</button>
<button ${disAttr} class="${assistBtnClass}" aria-pressed="${isFeedAssist ? "true" : "false"}" onclick="slotAction('${assistAction}', ${slotNum})" title="${escapeHtml(assistBtnTitle)}">${escapeHtml(assistBtnLabel)}</button>
</div>
`;
container.appendChild(card);
});
}

function computeContrastTextColor(hexColor) {
const luminance = computeColorLuminance(hexColor);
return luminance > 150 ? "#111827" : "#FFFFFF";
}

function formatSlotDropdownLines(tIdx, slotInfo) {
if (!slotInfo) return { line1: t("ui.map.not_present", { slot: tIdx }), line2: "" };
const rawStatus = String(slotInfo.status || "").toLowerCase();
if (rawStatus === "empty") return { line1: `T${tIdx} · ${t("ui.modal.slot.opt_empty")}`, line2: "" };
const matRaw = String(slotInfo.material || "").trim();
const matLabel = matRaw || t("ui.modal.slot.opt_unconf");
const spInfo = (slotInfo.spoolman && typeof slotInfo.spoolman === "object" && !slotInfo.spoolman.conflict)
? slotInfo.spoolman
: null;
const spName = spInfo ? String(spInfo.display_name || spInfo.filament_name || "").trim() : "";
if (spName) {
return { line1: spName, line2: matLabel };
}
const explicitCol = spInfo ? String(spInfo.color_name || spInfo.color_text || "").trim() : "";
const slotHex = resolveSlotColorHex(slotInfo, "");
const colorText = getReadableColorName(slotHex, explicitCol) || slotHex;
return { line1: matLabel, line2: colorText };
}

function renderSlotDropdownLabelHtml(linesObj) {
const l1 = escapeHtml(linesObj && linesObj.line1 ? linesObj.line1 : "");
const l2 = (linesObj && linesObj.line2) ? `<span class="slot-lbl-secondary">${escapeHtml(linesObj.line2)}</span>` : "";
return `<span class="slot-lbl-stack"><span class="slot-lbl-primary">${l1}</span>${l2}</span>`;
}

function getSlotDropdownColors(slotInfo) {
const isEmpty = !slotInfo || String(slotInfo.status || "").toLowerCase() === "empty";
const bgHex = isEmpty ? "#475569" : resolveSlotColorHex(slotInfo, "#475569");
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
const card = dd.closest(".slot-card");
if (card) card.classList.remove("has-open-menu");
}
});
}

const DRYER_UNIT_I18N_MAP = {
"all": "ui.sec2.unit_all",
"0": "ui.sec2.unit_ace1",
"1": "ui.sec2.unit_ace2",
"2": "ui.sec2.unit_ace3",
"3": "ui.sec2.unit_ace4"
};

function getConfiguredAceCountValue() {
const raw = String(currentState.configured_ace_count || "auto").trim().toLowerCase();
return ["auto", "1", "2", "3", "4"].includes(raw) ? raw : "auto";
}

function setConfiguredAceCountValue(val) {
const raw = String(val === undefined || val === null ? "auto" : val).trim().toLowerCase();
const norm = ["auto", "1", "2", "3", "4"].includes(raw) ? raw : "auto";
applyCustomSelectValue(
"configuredAceCount",
"configuredAceCountLabel",
norm,
"ui.settings.conn.ace_count_" + norm
);
return norm;
}

function toggleConfiguredAceCountDropdown(event) {
toggleCustomSelectDropdown(
document.getElementById("configuredAceCount"),
document.getElementById("configuredAceCountTrigger"),
document.getElementById("configuredAceCountMenu"),
event
);
}

function selectConfiguredAceCount(val, event) {
if (event) event.stopPropagation();
const norm = setConfiguredAceCountValue(val);
currentState.configured_ace_count = norm;
markSettingsDirty();
closeAllSlotDropdowns();
renderDryerUnitDropdown();
renderSlots();
renderMappingTable();
}

function renderDryerUnitDropdown(explicitUnitCount) {
const menuEl = document.getElementById("dryerUnitMenu");
const unitCount = Math.max(1, Math.min(4, Number(explicitUnitCount || getEffectiveAceUnitCount() || 1)));
const curRaw = readDryerControls().unit;
if (menuEl && (menuEl.dataset.renderedUnitCount !== String(unitCount) || menuEl.dataset.renderedLang !== String(currentLanguage))) {
const opts = [];
if (unitCount >= 2) {
const allLabel = unitCount > 2
? t("ui.sec2.unit_all_n", { count: unitCount })
: t("ui.sec2.unit_all");
opts.push(
`<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="all" onclick="selectDryerUnit('all', event)">${escapeHtml(allLabel)}</button>`
);
}
for (let idx = 0; idx < unitCount; idx++) {
const key = DRYER_UNIT_I18N_MAP[String(idx)] || "ui.sec2.unit_ace1";
opts.push(
`<button type="button" role="option" aria-selected="false" class="custom-select-option" data-value="${idx}" onclick="selectDryerUnit('${idx}', event)">${escapeHtml(t(key))}</button>`
);
}
menuEl.innerHTML = opts.join("");
menuEl.dataset.renderedUnitCount = String(unitCount);
menuEl.dataset.renderedLang = String(currentLanguage);
}
return _applyDryerUnitValue(curRaw, unitCount);
}

function _applyDryerUnitValue(unitVal, unitCount) {
const count = Math.max(1, Math.min(4, Number(unitCount || getEffectiveAceUnitCount() || 1)));
const raw = String(unitVal === undefined || unitVal === null ? "all" : unitVal).trim().toLowerCase();
let norm = "all";
if (count <= 1) {
norm = "0";
} else if (raw === "all") {
norm = "all";
} else if (/^[0-3]$/.test(raw) && Number(raw) < count) {
norm = raw;
} else {
norm = "all";
}
const i18nKey = (norm === "all" && count > 2)
? "ui.sec2.unit_all_n"
: (DRYER_UNIT_I18N_MAP[norm] || "ui.sec2.unit_all");
applyCustomSelectValue("dryerUnit", "dryerUnitLabel", norm, i18nKey);
if (norm === "all" && count > 2) {
const lblEl = document.getElementById("dryerUnitLabel");
if (lblEl) lblEl.textContent = t("ui.sec2.unit_all_n", { count: count });
}
return norm;
}

function setDryerUnitValue(unitVal) {
return _applyDryerUnitValue(unitVal, getEffectiveAceUnitCount());
}

function toggleDryerUnitDropdown(event) {
toggleCustomSelectDropdown(
document.getElementById("dryerUnit"),
document.getElementById("dryerUnitTrigger"),
document.getElementById("dryerUnitMenu"),
event
);
}

function selectDryerUnit(unitVal, event) {
if (event) event.stopPropagation();
setDryerUnitValue(unitVal);
closeAllSlotDropdowns();
onDryerSettingsChange();
}

const DRYER_PRESETS = {
"custom": "ui.sec2.preset_custom",
"45": "ui.sec2.preset_pla",
"60": "ui.sec2.preset_petg",
"50": "ui.sec2.preset_tpu"
};

function syncDryerPresetFromTemp(tempVal, forceCustom) {
const raw = String(tempVal === undefined || tempVal === null ? "" : tempVal).trim();
const key = (!forceCustom && Object.prototype.hasOwnProperty.call(DRYER_PRESETS, raw) && raw !== "custom")
? raw
: "custom";
applyCustomSelectValue("dryerPreset", "dryerPresetLabel", key, DRYER_PRESETS[key] || "ui.sec2.preset_custom");
return key;
}

function toggleDryerPresetDropdown(event) {
toggleCustomSelectDropdown(
document.getElementById("dryerPreset"),
document.getElementById("dryerPresetTrigger"),
document.getElementById("dryerPresetMenu"),
event
);
}

function selectDryerPreset(presetVal, event) {
if (event) event.stopPropagation();
const key = String(presetVal === undefined || presetVal === null ? "custom" : presetVal).trim();
closeAllSlotDropdowns();
const tempEl = document.getElementById("dryerTemp");
if (key === "custom") {
syncDryerPresetFromTemp("custom", true);
if (tempEl && typeof tempEl.focus === "function") tempEl.focus();
return;
}
if (tempEl) tempEl.value = key;
syncDryerPresetFromTemp(key, false);
onDryerSettingsChange();
}

function onDryerTempInput() {
const tempEl = document.getElementById("dryerTemp");
syncDryerPresetFromTemp(tempEl ? tempEl.value : "", false);
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
const btnExport = document.getElementById("btnModalExportGcode");
const btnUpload = document.getElementById("btnModalUploadOnly");
const btnSend = document.getElementById("btnModalSendPrint");
const lockBox = document.getElementById("modalSendLockNotice");
const lockTxt = document.getElementById("modalSendLockText");
const st = currentState.ace_status || {};
const cState = getEffectiveConnectionState();
const isDisconnected = !st.connected && (cState === "offline" || (cState === "unconfigured" && hasReceivedInitialState));
const isPrinting = !!st.is_printing;
const blockedByMismatch = !!currentState._modalBlockedByMismatch;
const blockedByErrors = !!currentState._modalBlockedByErrors;

if (btnApply) {
btnApply.disabled = blockedByMismatch || blockedByErrors;
}
if (btnExport) {
btnExport.disabled = blockedByMismatch || blockedByErrors;
}
if (btnUpload) {
btnUpload.disabled = blockedByMismatch || blockedByErrors || isDisconnected;
}
if (btnSend) {
btnSend.disabled = blockedByMismatch || blockedByErrors || isPrinting || isDisconnected;
if (isPrinting) {
const busyTip = t("ui.modal.feedback.printing_busy", { state: st.print_state || "printing" });
btnSend.setAttribute("title", busyTip);
btnSend.setAttribute("data-tooltip", busyTip);
} else {
btnSend.removeAttribute("title");
btnSend.removeAttribute("data-tooltip");
}
}

if (lockBox && lockTxt) {
let reasonMsg = "";
let isDanger = false;
if (isPrinting) {
reasonMsg = t("ui.modal.lock.printing", { state: st.print_state || "printing" });
} else if (blockedByErrors) {
reasonMsg = t("ui.modal.lock.mapping_errors");
isDanger = true;
} else if (blockedByMismatch) {
reasonMsg = t("ui.modal.lock.mismatch");
} else if (isDisconnected) {
reasonMsg = t("ui.modal.lock.offline", { state: cState === "offline" ? "offline" : "not configured" });
} else if (currentState.external_spool_mode && !isKlipperAceDisabled() && st.connected && st.ace_pro_enabled !== false) {
reasonMsg = t("ui.modal.lock.external_pin_warn");
}
if (reasonMsg) {
lockTxt.textContent = reasonMsg;
lockBox.className = isDanger ? "modal-send-lock-notice is-visible is-danger" : "modal-send-lock-notice is-visible";
} else {
lockTxt.textContent = "";
lockBox.className = "modal-send-lock-notice";
}
}
}

function normalizeMaterialFamily(material) {
const raw = String(material || "").trim().toUpperCase();
if (!raw) return "";
const tokens = raw.split(/[^A-Z0-9]+/).filter(Boolean);
if (tokens.includes("PETG") || tokens.includes("PCTG") || raw.includes("PETG")) return "PETG";
if (tokens.includes("PLA") || tokens.includes("EPLA") || /(?:^|[^A-Z])PLA(?:[^A-Z]|$)/.test(raw)) return "PLA";
if (tokens.includes("ASA")) return "ASA";
if (tokens.includes("ABS")) return "ABS";
if (tokens.includes("TPU") || tokens.includes("TPE")) return "TPU";
if (tokens.includes("PVA")) return "PVA";
if (tokens.includes("BVOH")) return "BVOH";
if (tokens.includes("HIPS")) return "HIPS";
if (tokens.includes("PC") && tokens.includes("ABS")) return "PC-ABS";
if (tokens.includes("PC") || tokens.includes("POLYCARBONATE")) return "PC";
if (tokens.includes("NYLON") || tokens.includes("PA") || tokens.some(t => t.startsWith("PA6") || t.startsWith("PA12"))) return "PA";
return raw.replace(/[^A-Z0-9]+/g, "");
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
const maxSlot = getEffectiveMaxSlots();
const slotByTool = {};
slots.forEach(s => {
if (s && s.tool !== undefined) slotByTool[Number(s.tool)] = s;
});
const precomputedSlotOptions = [];
const checkSvgHtml = uiSvg("check", "ui-icon-sm");
for (let tIdx = 0; tIdx < maxSlot; tIdx++) {
const slotInfo = slotByTool[tIdx];
const optColors = getSlotDropdownColors(slotInfo);
const optLines = formatSlotDropdownLines(tIdx, slotInfo);
const optLabelHtml = renderSlotDropdownLabelHtml(optLines);
const optRingStyle = optColors.isEmpty ? "" : `style="background:${optColors.bgHex};"`;
const optStyleAttr = `style="background-color:${optColors.bgHex};color:${optColors.fgHex};"`;
const optRfidHtml = (slotInfo && slotInfo.rfid) ? `<span class="slot-rfid-tag">RFID</span>` : "";
precomputedSlotOptions.push({
tIdx: tIdx,
optColors: optColors,
optLabelHtml: optLabelHtml,
optRingStyle: optRingStyle,
optStyleAttr: optStyleAttr,
optRfidHtml: optRfidHtml
});
}

const predData = (typeof computePreprintFilamentPredictionLocal === "function")
? computePreprintFilamentPredictionLocal()
: null;
const predBySlot = {};
if (predData && Array.isArray(predData.slots)) {
predData.slots.forEach(item => {
if (item && item.slot_tool !== undefined) predBySlot[Number(item.slot_tool)] = item;
});
}
const fmtG = (v) => (v === null || v === undefined || !Number.isFinite(Number(v))) ? "—" : (Number(v).toFixed(1) + " g");
const reasonMap = {
no_spool: "ui.modal.pred.reason_no_spool",
no_weight: "ui.modal.pred.reason_no_weight",
slot_empty: "ui.modal.pred.reason_slot_empty",
slot_missing: "ui.modal.pred.reason_slot_missing",
conflict: "ui.modal.pred.reason_conflict",
no_usage: "ui.modal.pred.reason_no_usage",
no_density: "ui.modal.pred.reason_no_density",
flush_incomplete: "ui.modal.pred.reason_flush_incomplete"
};

const filaments = currentState.slicer_filaments || [];
const sources = currentState.tool_map_sources || {};
const showUnusedEl = document.getElementById("chkShowUnusedExtruders");
const showUnused = !!(showUnusedEl && showUnusedEl.checked);
const hasAnyUsageData = hasFilamentUsageData(filaments);

const mappedCounts = {};
const mappedUsedG = {};
filaments.forEach(fil => {
if (isFilamentUsed(fil, hasAnyUsageData)) {
const m = (currentState.tool_map && currentState.tool_map[fil.slicer_tool] !== undefined)
? Number(currentState.tool_map[fil.slicer_tool])
: Number(fil.slicer_tool);
mappedCounts[m] = (mappedCounts[m] || 0) + 1;
mappedUsedG[m] = (mappedUsedG[m] || 0) + Math.max(0, Number(fil.used_g || 0));
}
});

const klipperOff = isKlipperAceDisabled();
const extBypass = isExternalSpoolBypassActive();
const aceActionsEl = document.getElementById("mappingAceActions");
if (aceActionsEl) {
aceActionsEl.style.display = extBypass ? "none" : "flex";
}
const extBanner = document.getElementById("modalExternalSpoolBanner");
const extText = document.getElementById("modalExternalSpoolText");
const btnSwitchAce = document.getElementById("btnSwitchBackToAce");
if (extBanner && extText) {
if (extBypass) {
extText.textContent = klipperOff ? t("ui.modal.map.ext_spool_klipper") : t("ui.modal.map.ext_spool_manual");
extBanner.classList.add("visible");
if (btnSwitchAce) btnSwitchAce.style.display = "inline-flex";
} else {
extText.textContent = "";
extBanner.classList.remove("visible");
if (btnSwitchAce) btnSwitchAce.style.display = "none";
}
}

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

const assignedSlot = slotByTool[mapped];
const filUsed = isFilamentUsed(fil, hasAnyUsageData);
const predItem = (filUsed && predBySlot[mapped]) ? predBySlot[mapped] : null;
let cardStateClass = "";
let extraLeftBadgesHtml = "";
let compactStatusBadgeHtml = "";
let backupEval = null;
let cardFallbackNoteHtml = "";

if (!filUsed) {
compactStatusBadgeHtml = `<span class="badge empty">${escapeHtml(t("ui.map.unused_badge"))}</span>`;
} else if (extBypass) {
compactStatusBadgeHtml = `<span class="badge info">${escapeHtml(t("ui.modal.map.ext_spool_badge"))}</span>`;
} else if (mapped < 0 || (slots.length > 0 && mapped >= maxSlot)) {
errCount++;
cardStateClass = "card-error";
const invTip = t("ui.map.badge_invalid");
compactStatusBadgeHtml = `<span class="badge warn match-icon-badge" data-tooltip="${escapeHtml(invTip)}" aria-label="${escapeHtml(invTip)}">!</span>`;
cardFallbackNoteHtml = `<div class="prediction-note is-short">${escapeHtml(invTip)}</div>`;
} else {
const sMat = String(fil.material || "").trim().toUpperCase();
const pMat = assignedSlot ? String(assignedSlot.material || "").trim().toUpperCase() : "";
const isEmpty = assignedSlot ? (String(assignedSlot.status || "").toLowerCase() === "empty") : false;
const isShared = (mappedCounts[mapped] || 0) > 1;
const sFam = normalizeMaterialFamily(sMat);
const pFam = normalizeMaterialFamily(pMat);
const isMatCompatible = (!sMat || !pMat || sMat === pMat || sMat.startsWith(pMat) || pMat.startsWith(sMat) || (!!sFam && !!pFam && sFam === pFam));
const spInfo = (assignedSlot && assignedSlot.spoolman && typeof assignedSlot.spoolman === "object")
? assignedSlot.spoolman
: null;

let isLowSpool = !!(predItem && predItem.status === "low");
let isShortSpool = !!(predItem && predItem.status === "insufficient");
let shortTip = isShortSpool
? t("ui.modal.card.match_insufficient", { deficit: Number(predItem.deficit_g || 0).toFixed(1) })
: "";
let lowTip = isLowSpool
? t("ui.modal.card.match_low", { rem: Number(predItem.remaining_after_g || 0).toFixed(1) })
: "";
if (!predItem && spInfo && !spInfo.conflict && spInfo.remaining_weight !== null && spInfo.remaining_weight !== undefined) {
const remW = Number(spInfo.remaining_weight);
const reqW = mappedUsedG[mapped] || Math.max(0, Number(fil.used_g || 0));
if (Number.isFinite(remW) && reqW > 0 && reqW > remW + 1e-6) {
isShortSpool = true;
shortTip = t("ui.modal.card.low_spool", { req: reqW.toFixed(1), rem: remW.toFixed(1) });
}
}

if (isShared) {
extraLeftBadgesHtml += `<span class="badge info">${escapeHtml(t("ui.modal.card.shared_badge", { slot: mapped }))}</span>`;
}
if (spInfo && spInfo.conflict) {
warnCount++;
extraLeftBadgesHtml += `<span class="badge warn">${escapeHtml(t("ui.modal.card.conflict_badge", { count: Number(spInfo.conflict_count || 2) }))}</span>`;
}
let noBackupWarnText = "";
if (filUsed && !extBypass && isMoonrakerOnline() && endlessSpoolDraft.enabled && assignedSlot) {
let bkChain = findEndlessSpoolBackupChain(slots, mapped, endlessSpoolDraft.mode);
let driverConfirmed = false;
const stLiveEs = (currentState && currentState.ace_status) ? currentState.ace_status : {};
const drvNext = (stLiveEs.endless_spool_next_tool !== undefined && stLiveEs.endless_spool_next_tool !== null)
? Number(stLiveEs.endless_spool_next_tool)
: -1;
if (!endlessSpoolDraft.dirty && !!stLiveEs.endless_spool_enabled && Number(stLiveEs.current_index) === mapped && drvNext >= 0 && slotByTool[drvNext]) {
driverConfirmed = true;
bkChain = [slotByTool[drvNext]].concat(bkChain.filter(c => Number(c.tool) !== drvNext));
}
const modeLbl = endlessSpoolDraft.mode === "material"
? t("ui.modal.opt.endless_mode_material")
: (endlessSpoolDraft.mode === "next" ? t("ui.modal.opt.endless_mode_next") : t("ui.modal.opt.endless_mode_exact"));
const deficitG = (predItem && predItem.status === "insufficient")
? Math.max(0, Number(predItem.deficit_g || 0))
: 0;
if (bkChain.length > 0) {
const usedChain = [];
let sumAvail = 0;
let anyUnknown = false;
if (deficitG > 0) {
for (const cand of bkChain) {
usedChain.push(cand);
const rawW = (cand.spoolman && cand.spoolman.remaining_weight !== null && cand.spoolman.remaining_weight !== undefined && Number.isFinite(Number(cand.spoolman.remaining_weight)))
? Number(cand.spoolman.remaining_weight)
: null;
if (rawW === null) {
anyUnknown = true;
break;
}
sumAvail += rawW;
if (sumAvail >= deficitG - 1e-6) break;
}
} else {
const firstCand = bkChain[0];
usedChain.push(firstCand);
const rawW = (firstCand.spoolman && firstCand.spoolman.remaining_weight !== null && firstCand.spoolman.remaining_weight !== undefined && Number.isFinite(Number(firstCand.spoolman.remaining_weight)))
? Number(firstCand.spoolman.remaining_weight)
: null;
if (rawW === null) anyUnknown = true;
else sumAvail = rawW;
}
const slotsLabel = usedChain.map(c => `T${Number(c.tool)}`).join(" → ");
const firstHex = safeHexColor(resolveSlotColorHex(usedChain[0], "#64748B"), "#64748B");
const matLabel = usedChain[0].material || "?";
const bkAvailTotal = anyUnknown ? null : sumAvail;
const bkRem = bkAvailTotal !== null ? ` · ${bkAvailTotal.toFixed(0)} g` : "";
const stillShortG = (deficitG > 0 && bkAvailTotal !== null) ? Math.max(0, deficitG - bkAvailTotal) : 0;
const coversDeficit = deficitG > 0 && bkAvailTotal !== null && bkAvailTotal >= deficitG - 1e-6;
backupEval = { slotsLabel, availG: bkAvailTotal, coversDeficit, stillShortG, modeLbl };
const tipKey = stillShortG > 1e-6
? "ui.modal.card.backup_short_tip"
: ((driverConfirmed && usedChain.length === 1) ? "ui.modal.card.backup_tip_driver" : "ui.modal.card.backup_tip");
const bkTip = t(tipKey, { mode: modeLbl, slots: slotsLabel, mat: matLabel, rem: bkRem, still_short: stillShortG.toFixed(1) });
const bkCls = stillShortG > 1e-6 ? "warn" : "ok";
extraLeftBadgesHtml += `<span class="badge ${bkCls} backup-spool-badge" data-tooltip="${escapeHtml(bkTip)}" aria-label="${escapeHtml(bkTip)}"><span class="backup-swatch-dot" style="background:${escapeHtml(firstHex)};"></span>${escapeHtml(t("ui.modal.card.backup_badge", { slots: slotsLabel }))}</span>`;
} else if (isShortSpool || isLowSpool) {
const noBkTip = t("ui.modal.card.backup_none_tip", { mode: modeLbl });
noBackupWarnText = noBkTip;
backupEval = { slotsLabel: "", availG: null, coversDeficit: false, stillShortG: deficitG, modeLbl };
extraLeftBadgesHtml += `<span class="badge warn backup-spool-badge" data-tooltip="${escapeHtml(noBkTip)}" aria-label="${escapeHtml(noBkTip)}">${escapeHtml(t("ui.modal.card.backup_none"))}</span>`;
}
}

if (isEmpty) {
errCount++;
cardStateClass = "card-error";
const emptyTip = t("ui.modal.card.empty_badge", { slot: mapped });
compactStatusBadgeHtml = `<span class="badge warn match-icon-badge" data-tooltip="${escapeHtml(emptyTip)}" aria-label="${escapeHtml(emptyTip)}">!</span>`;
cardFallbackNoteHtml = `<div class="prediction-note is-short">${escapeHtml(emptyTip)}</div>`;
} else if (assignedSlot || predItem) {
const reasonParts = [];
if (!isMatCompatible) {
warnCount++;
cardStateClass = "card-mismatch";
const misTxt = t("ui.modal.card.mismatch_badge", { slicer_mat: sMat, ace_mat: pMat });
reasonParts.push(misTxt);
mismatchDescriptions.push(`T${sTool} (${sMat}) → T${mapped} (${pMat})`);
}
if (isShortSpool) {
warnCount++;
if (cardStateClass === "" && !(backupEval && backupEval.coversDeficit)) cardStateClass = "card-error";
if (shortTip) reasonParts.push(shortTip);
} else if (isLowSpool) {
warnCount++;
if (lowTip) reasonParts.push(lowTip);
}
if (noBackupWarnText) {
reasonParts.push(noBackupWarnText);
}
if (reasonParts.length > 0) {
const combinedErr = reasonParts.join(" · ");
compactStatusBadgeHtml = `<span class="badge warn match-icon-badge" data-tooltip="${escapeHtml(combinedErr)}" aria-label="${escapeHtml(combinedErr)}">!</span>`;
if (!isMatCompatible) {
cardFallbackNoteHtml = `<div class="fil-status-err-text">${escapeHtml(t("ui.modal.card.mismatch_badge", { slicer_mat: sMat, ace_mat: pMat }))}</div>`;
} else if (!predItem) {
cardFallbackNoteHtml = `<div class="fil-status-err-text">${escapeHtml(combinedErr)}</div>`;
}
} else {
const okTip = (predItem && predItem.status === "sufficient" && predItem.remaining_after_g !== null && predItem.remaining_after_g !== undefined)
? t("ui.modal.card.match_sufficient_tip", { mat: pMat || sMat || "OK", rem: Number(predItem.remaining_after_g).toFixed(1) })
: t("ui.modal.card.match_exact", { mat: pMat || sMat || "OK" });
compactStatusBadgeHtml = `<span class="badge ok match-icon-badge" data-tooltip="${escapeHtml(okTip)}" aria-label="${escapeHtml(okTip)}">✓</span>`;
}
}
}

const filHex = safeHexColor(fil.color, "#64748B");

if (cardsContainer && (showUnused || filUsed)) {
let trigColors;
let trigLabelHtml;
let trigRingStyle;
let trigMismatchClass = cardStateClass === "card-mismatch" ? " is-mismatch" : "";
let trigStyleAttr;
let menuItemsHtml = "";
let predDividerHtml = "";
let predMetricsHtml = "";
let cardLeftBadgeClass = srcClass;
let cardLeftBadgeLabel = srcLabel;
let ariaSelectLabel = t("ui.modal.btn.slot_select_aria", { tool: sTool });

if (extBypass && filUsed) {
ariaSelectLabel = t("ui.modal.ext.select_aria");
const allSpools = Array.isArray(currentState.spoolman_spools) ? currentState.spoolman_spools : [];
const selExtId = (currentState.external_spool_id !== null && currentState.external_spool_id !== undefined && Number(currentState.external_spool_id) > 0)
? Number(currentState.external_spool_id)
: null;
const selSpool = selExtId ? (allSpools.find(sp => Number(sp.spool_id) === selExtId) || null) : null;
const sFamExt = normalizeMaterialFamily(fil.material || "");
const sortedSpools = allSpools.slice().sort((a, b) => {
const aMatch = sFamExt && normalizeMaterialFamily(a.material || "") === sFamExt ? 0 : 1;
const bMatch = sFamExt && normalizeMaterialFamily(b.material || "") === sFamExt ? 0 : 1;
if (aMatch !== bMatch) return aMatch - bMatch;
return Number(a.spool_id || 0) - Number(b.spool_id || 0);
});
const noneSel = !selSpool;
const noneBg = "#475569";
const noneFg = "#FFFFFF";
const extMenuParts = [
`<button type="button" role="option" aria-selected="${noneSel ? "true" : "false"}"
class="slot-color-option${noneSel ? " is-selected" : ""}"
style="background-color:${noneBg};color:${noneFg};"
onclick="selectExternalSpool(null, event)">
<span class="slot-opt-main">
<span class="slot-swatch-ring is-empty"></span>
<span class="slot-opt-name">${renderSlotDropdownLabelHtml({ line1: t("ui.modal.ext.no_tracking"), line2: t("ui.modal.ext.no_tracking_sub") })}</span>
</span>
<span class="slot-opt-meta">${noneSel ? checkSvgHtml : ""}</span>
</button>`
];
sortedSpools.forEach(sp => {
const spId = Number(sp.spool_id);
const isSelSp = !!selSpool && Number(selSpool.spool_id) === spId;
const spHex = safeHexColor(sp.color_hex, "#475569");
const spFg = computeContrastTextColor(spHex);
const remTxt = (sp.remaining_weight !== null && sp.remaining_weight !== undefined && Number.isFinite(Number(sp.remaining_weight)))
? `${Number(sp.remaining_weight).toFixed(0)} g`
: "";
const subParts = [`#${spId}`, sp.material || "", remTxt].filter(Boolean);
extMenuParts.push(`
<button type="button" role="option" aria-selected="${isSelSp ? "true" : "false"}"
class="slot-color-option${isSelSp ? " is-selected" : ""}"
style="background-color:${spHex};color:${spFg};"
onclick="selectExternalSpool(${spId}, event)">
<span class="slot-opt-main">
<span class="slot-swatch-ring" style="background:${spHex};"></span>
<span class="slot-opt-name">${renderSlotDropdownLabelHtml({ line1: sp.display_name || ("#" + spId), line2: subParts.join(" · ") })}</span>
</span>
<span class="slot-opt-meta">${isSelSp ? checkSvgHtml : ""}</span>
</button>
`);
});
menuItemsHtml = extMenuParts.join("");
if (selSpool) {
const spHex = safeHexColor(selSpool.color_hex, "#475569");
const spFg = computeContrastTextColor(spHex);
const remTxt = (selSpool.remaining_weight !== null && selSpool.remaining_weight !== undefined && Number.isFinite(Number(selSpool.remaining_weight)))
? `${Number(selSpool.remaining_weight).toFixed(0)} g`
: "";
const subParts = [`#${selSpool.spool_id}`, selSpool.material || "", remTxt].filter(Boolean);
trigColors = { bgHex: spHex, fgHex: spFg, isEmpty: false };
trigLabelHtml = renderSlotDropdownLabelHtml({ line1: selSpool.display_name || ("#" + selSpool.spool_id), line2: subParts.join(" · ") });
trigRingStyle = `style="background:${spHex};"`;
trigStyleAttr = `style="background-color:${spHex};color:${spFg};"`;
cardLeftBadgeClass = "loaded";
cardLeftBadgeLabel = t("ui.modal.ext.tracking_badge", { id: selSpool.spool_id });
const availG = (selSpool.remaining_weight !== null && selSpool.remaining_weight !== undefined && Number.isFinite(Number(selSpool.remaining_weight)))
? Number(selSpool.remaining_weight)
: null;
const densExt = resolveFilamentDensityJs(fil, selSpool);
const printG = resolveFilamentPrintUsageJs(fil, densExt);
if (availG !== null) {
const remAfter = printG !== null ? (availG - printG) : null;
const resG = Number(currentState.prediction_reserve_g !== undefined ? currentState.prediction_reserve_g : 15);
const capG = (selSpool.capacity_weight && Number(selSpool.capacity_weight) > 0) ? Number(selSpool.capacity_weight) : (availG > 0 ? availG : 1000);
const beforePct = Math.max(0, Math.min(100, (availG / capG) * 100)).toFixed(1);
const afterPct = remAfter !== null ? Math.max(0, Math.min(100, (Math.max(0, remAfter) / capG) * 100)).toFixed(1) : beforePct;
const isShort = remAfter !== null && remAfter < -1e-6;
const isLow = !isShort && remAfter !== null && remAfter < resG - 1e-6;
const valCls = isShort ? "is-short" : (isLow ? "is-low" : "");
const isLightSp = computeRelativeLuminance(spHex) > 0.78;
const afterCol = isShort ? "var(--danger)" : spHex;
const beforeBg = isLightSp ? "#1e293b" : spHex;
const trackStyle = isLightSp ? ' style="background:#cbd5e1;"' : "";
predDividerHtml = `<div class="fil-pred-divider"><div class="prediction-bar-track${isLightSp ? " is-light-spool" : ""}"${trackStyle}><div class="prediction-bar-before${isLightSp ? " is-light-spool" : ""}" style="width:${beforePct}%;background:${beforeBg};${isLightSp ? "opacity:0.9;" : ""}"></div><div class="prediction-bar-after${isLightSp ? " is-light-spool" : ""}" style="width:${afterPct}%;background:${afterCol};"></div></div></div>`;
let noteHtml = "";
if (isShort) {
noteHtml = `<div class="prediction-note is-short">${escapeHtml(t("ui.modal.pred.warn_short", { deficit: Math.abs(remAfter).toFixed(1) }))}</div>`;
} else if (isLow) {
noteHtml = `<div class="prediction-note is-low">${escapeHtml(t("ui.modal.pred.warn_low", { rem: remAfter.toFixed(1), reserve: resG.toFixed(0) }))}</div>`;
}
predMetricsHtml = `
<div class="prediction-metrics">
<div class="prediction-metric"><div class="prediction-metric-lbl">${escapeHtml(t("ui.modal.pred.avail"))}</div><div class="prediction-metric-val">${fmtG(availG)}</div></div>
<div class="prediction-metric"><div class="prediction-metric-lbl">${escapeHtml(t("ui.modal.pred.print"))}</div><div class="prediction-metric-val ${valCls}">${fmtG(printG)}</div></div>
<div class="prediction-metric"><div class="prediction-metric-lbl">${escapeHtml(t("ui.modal.pred.after"))}</div><div class="prediction-metric-val ${valCls}">${fmtG(remAfter)}</div></div>
</div>
${noteHtml}
`;
}
} else {
trigColors = { bgHex: noneBg, fgHex: noneFg, isEmpty: true };
trigLabelHtml = renderSlotDropdownLabelHtml({ line1: t("ui.modal.ext.no_tracking"), line2: t("ui.modal.ext.no_tracking_sub") });
trigRingStyle = "";
trigStyleAttr = `style="background-color:${noneBg};color:${noneFg};"`;
cardLeftBadgeClass = "info";
cardLeftBadgeLabel = t("ui.modal.map.ext_spool_badge");
compactStatusBadgeHtml = "";
}
} else {
const assignedDesc = (mapped >= 0 && mapped < maxSlot) ? precomputedSlotOptions[mapped] : null;
trigColors = assignedDesc ? assignedDesc.optColors : getSlotDropdownColors(assignedSlot);
trigLabelHtml = assignedDesc ? assignedDesc.optLabelHtml : renderSlotDropdownLabelHtml(formatSlotDropdownLines(mapped, assignedSlot));
trigRingStyle = assignedDesc ? assignedDesc.optRingStyle : (trigColors.isEmpty ? "" : `style="background:${trigColors.bgHex};"`);
trigStyleAttr = assignedDesc ? assignedDesc.optStyleAttr : `style="background-color:${trigColors.bgHex};color:${trigColors.fgHex};"`;
const menuItemsParts = [];
for (let tIdx = 0; tIdx < maxSlot; tIdx++) {
const opt = precomputedSlotOptions[tIdx];
const isSel = mapped === tIdx;
const optCheckHtml = isSel ? checkSvgHtml : "";
menuItemsParts.push(`
<button type="button" role="option" aria-selected="${isSel ? "true" : "false"}"
class="slot-color-option${isSel ? " is-selected" : ""}"
${opt.optStyleAttr}
onclick="selectSlotFromDropdown(${sTool}, ${tIdx}, event)">
<span class="slot-opt-main">
<span class="slot-swatch-ring${opt.optColors.isEmpty ? " is-empty" : ""}" ${opt.optRingStyle}></span>
<span class="slot-opt-name">${opt.optLabelHtml}</span>
</span>
<span class="slot-opt-meta">${opt.optRfidHtml}${optCheckHtml}</span>
</button>
`);
}
if (mapped < 0 || mapped >= maxSlot) {
const oobStyle = `style="background-color:#475569;color:#FFFFFF;"`;
menuItemsParts.push(`
<button type="button" role="option" aria-selected="true"
class="slot-color-option is-selected"
${oobStyle}
onclick="selectSlotFromDropdown(${sTool}, ${mapped}, event)">
<span class="slot-opt-main">
<span class="slot-swatch-ring is-empty"></span>
<span class="slot-opt-name">${renderSlotDropdownLabelHtml({ line1: t("ui.map.not_present", { slot: mapped }), line2: "" })}</span>
</span>
<span class="slot-opt-meta">${checkSvgHtml}</span>
</button>
`);
}
menuItemsHtml = menuItemsParts.join("");

if (filUsed && predItem) {
const pStatus = predItem.status;
const valCls = pStatus === "insufficient" ? "is-short" : (pStatus === "low" ? "is-low" : "");
const beforePct = predItem.remaining_pct_before !== null ? predItem.remaining_pct_before.toFixed(1) : "0";
const afterPct = predItem.remaining_pct_after !== null ? predItem.remaining_pct_after.toFixed(1) : "0";
const spoolHex = escapeHtml(predItem.hex_color || (assignedSlot ? resolveSlotColorHex(assignedSlot, filHex) : filHex));
const isLightSpool = computeRelativeLuminance(spoolHex) > 0.78;
const afterCol = pStatus === "insufficient" ? "var(--danger)" : spoolHex;
const trackCls = isLightSpool ? "prediction-bar-track is-light-spool" : "prediction-bar-track";
const beforeCls = isLightSpool ? "prediction-bar-before is-light-spool" : "prediction-bar-before";
const afterCls = isLightSpool ? "prediction-bar-after is-light-spool" : "prediction-bar-after";
const beforeBg = isLightSpool ? "#1e293b" : spoolHex;
const beforeOpStyle = isLightSpool ? "opacity:0.9;" : "";
const afterBorderStyle = (isLightSpool && pStatus !== "insufficient" && Number(afterPct) > 0) ? "border:1px solid #64748b;" : "";
const trackStyle = isLightSpool ? ' style="background:#cbd5e1;"' : "";
const barHtml = predItem.remaining_pct_before !== null
? `<div class="${trackCls}"${trackStyle}><div class="${beforeCls}" style="width:${beforePct}%;background:${beforeBg};${beforeOpStyle}"></div><div class="${afterCls}" style="width:${afterPct}%;background:${afterCol};${afterBorderStyle}"></div></div>`
: `<div class="${trackCls}"${trackStyle}></div>`;
predDividerHtml = `
<div class="fil-pred-divider" data-slot="${predItem.slot_tool}" data-pred-status="${escapeHtml(pStatus)}">
${barHtml}
</div>
`;
let noteHtml = "";
if (pStatus === "insufficient") {
const defStr = Number(predItem.deficit_g || 0).toFixed(1);
if (backupEval && backupEval.slotsLabel) {
if (backupEval.coversDeficit) {
noteHtml = `<div class="prediction-note is-ok">${escapeHtml(t("ui.modal.pred.backup_ok", { deficit: defStr, slots: backupEval.slotsLabel, avail: backupEval.availG.toFixed(1) }))}</div>`;
} else if (backupEval.availG !== null) {
noteHtml = `<div class="prediction-note is-short">${escapeHtml(t("ui.modal.pred.backup_short", { deficit: defStr, slots: backupEval.slotsLabel, avail: backupEval.availG.toFixed(1), still_short: backupEval.stillShortG.toFixed(1) }))}</div>`;
} else {
noteHtml = `<div class="prediction-note is-low">${escapeHtml(t("ui.modal.pred.backup_unknown", { deficit: defStr, slots: backupEval.slotsLabel }))}</div>`;
}
} else if (backupEval && !backupEval.slotsLabel) {
noteHtml = `<div class="prediction-note is-short">${escapeHtml(t("ui.modal.pred.backup_none", { deficit: defStr, mode: backupEval.modeLbl }))}</div>`;
} else {
noteHtml = `<div class="prediction-note is-short">${escapeHtml(t("ui.modal.pred.warn_short", { deficit: defStr }))}</div>`;
}
} else if (pStatus === "low") {
noteHtml = `<div class="prediction-note is-low">${escapeHtml(t("ui.modal.pred.warn_low", { rem: Number(predItem.remaining_after_g || 0).toFixed(1), reserve: Number((predData && predData.reserve_g) || 0).toFixed(0) }))}</div>`;
} else if (pStatus === "unknown") {
const rKey = reasonMap[predItem.reason] || "ui.modal.pred.reason_unreliable";
noteHtml = `<div class="prediction-note">${escapeHtml(t(rKey))}</div>`;
}
const fullMetricsTip = [
`${t("ui.modal.pred.avail")}: ${fmtG(predItem.available_weight_g)}`,
`${t("ui.modal.pred.print")}: ${fmtG(predItem.print_usage_g)}`,
`${t("ui.modal.pred.purge")}: ${fmtG(predItem.purge_usage_g)}`,
`${t("ui.modal.pred.total")}: ${fmtG(predItem.total_usage_g)}`,
`${t("ui.modal.pred.after")}: ${fmtG(predItem.remaining_after_g)}`
].join(" | ");
predMetricsHtml = `
<div class="prediction-metrics" title="${escapeHtml(fullMetricsTip)}">
<div class="prediction-metric"><div class="prediction-metric-lbl">${escapeHtml(t("ui.modal.pred.avail"))}</div><div class="prediction-metric-val">${fmtG(predItem.available_weight_g)}</div></div>
<div class="prediction-metric"><div class="prediction-metric-lbl">${escapeHtml(t("ui.modal.pred.purge"))}</div><div class="prediction-metric-val">${fmtG(predItem.purge_usage_g)}</div></div>
<div class="prediction-metric"><div class="prediction-metric-lbl">${escapeHtml(t("ui.modal.pred.total"))}</div><div class="prediction-metric-val ${valCls}">${fmtG(predItem.total_usage_g)}</div></div>
</div>
${noteHtml}
`;
}
}

const arrowActiveClass = filUsed && cardStateClass !== "card-error" ? " is-mapped" : "";
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
</div>
</div>
<div class="fil-arrow-col${arrowActiveClass}">${uiSvg("arrowRight", "ui-icon-sm")}</div>
<div class="fil-target-col">
<div class="slot-color-dropdown" data-slicer-tool="${sTool}" data-selected-slot="${mapped}">
<button type="button"
class="slot-color-trigger${trigColors.isEmpty ? " is-empty" : ""}${trigMismatchClass}"
${trigStyleAttr}
aria-label="${escapeHtml(ariaSelectLabel)}"
onclick="toggleSlotDropdown(this, event)">
<span class="slot-swatch-ring${trigColors.isEmpty ? " is-empty" : ""}" ${trigRingStyle}></span>
<span class="slot-color-trigger-label">${trigLabelHtml}</span>
<span class="slot-color-caret">${uiSvg("chevronDown", "ui-icon-sm")}</span>
</button>
<div class="slot-color-menu" role="listbox">${menuItemsHtml}</div>
</div>
</div>
</div>
${predDividerHtml}
<div class="fil-pred-block">
<div class="fil-info-meta-row">
<div class="fil-status-group">
<span class="badge ${cardLeftBadgeClass}">${escapeHtml(cardLeftBadgeLabel)}</span>
${extraLeftBadgesHtml}
</div>
<div class="fil-status-group">${compactStatusBadgeHtml}</div>
</div>
${predMetricsHtml}
${cardFallbackNoteHtml}
</div>
`;
card.setAttribute("data-slicer-tool", String(sTool));
card.addEventListener("mouseenter", () => setHoveredExtruder(sTool));
card.addEventListener("mouseleave", () => clearHoveredExtruder(sTool));
cardsContainer.appendChild(card);
}
});

if (summaryBar) {
if (extBypass && activeFils.length > 0) {
summaryBar.innerHTML = `<span class="badge info">${escapeHtml(t("ui.modal.map.ext_spool_badge"))}</span>`;
} else if (errCount > 0) {
summaryBar.innerHTML = `<span class="badge danger">${escapeHtml(t("ui.map.sum_err_badge", { count: errCount }))}</span>`;
} else if (warnCount > 0) {
summaryBar.innerHTML = `<span class="badge warn">${escapeHtml(t("ui.map.sum_warn_badge", { count: warnCount }))}</span>`;
} else if (activeFils.length === 0) {
summaryBar.innerHTML = `<span class="badge warn">0</span>`;
} else {
summaryBar.innerHTML = `<span class="badge ok">${escapeHtml(t("ui.map.sum_ok_badge"))}</span>`;
}
}

// Green Usage Info Icon & Hover Breakdown Popover in Mapping Header
const usageWrap = document.getElementById("mappingUsageInfoWrap");
const usageBadge = document.getElementById("mappingUsageInfoBadge");
const usagePop = document.getElementById("mappingUsagePopover");
if (usageWrap && usageBadge && usagePop) {
const hasGcode = !!String(currentState.last_gcode_path || "").trim();
if (!hasGcode || activeFils.length === 0) {
usageWrap.style.display = "none";
usageBadge.setAttribute("title", "");
usagePop.innerHTML = "";
} else {
const ppInfo = (currentState.last_postprocess_info && typeof currentState.last_postprocess_info === "object")
? currentState.last_postprocess_info
: {};
const evs = Array.isArray(ppInfo.events) ? ppInfo.events : [];
const purgeVolBySlicerTool = {};
evs.forEach(ev => {
if (!ev || typeof ev !== "object") return;
const nt = Number(ev.new_slicer_tool);
const vm = Number(ev.volume_mm3 || 0);
if (Number.isFinite(nt) && Number.isFinite(vm) && vm > 0) {
purgeVolBySlicerTool[nt] = (purgeVolBySlicerTool[nt] || 0) + vm;
}
});
const stEst = (predData && predData.startup_purge_estimate && typeof predData.startup_purge_estimate === "object")
? predData.startup_purge_estimate
: null;
const g9111STool = (ppInfo.g9111StartSlicerTool !== null && ppInfo.g9111StartSlicerTool !== undefined)
? Number(ppInfo.g9111StartSlicerTool)
: null;
let totalPrintG = 0;
let printKnown = true;
let totalPurgeG = 0;
let purgeKnown = true;
const extLines = [];
const extHtmlParts = [];

activeFils.forEach(fil => {
const sTool = Number(fil.slicer_tool);
const mapped = (currentState.tool_map && currentState.tool_map[sTool] !== undefined)
? Number(currentState.tool_map[sTool])
: sTool;
const slObj = slotByTool[mapped];
const spObj = (slObj && slObj.spoolman && typeof slObj.spoolman === "object") ? slObj.spoolman : null;
const dens = resolveFilamentDensityJs(fil, spObj);
const pG = resolveFilamentPrintUsageJs(fil, dens);
if (pG === null) {
printKnown = false;
} else {
totalPrintG += pG;
}
let purG = null;
const pVol = purgeVolBySlicerTool[sTool] || 0;
if (dens !== null) {
purG = (pVol / 1000.0) * dens;
} else if (pVol === 0) {
purG = 0;
} else {
purgeKnown = false;
}
if (stEst && stEst.active && g9111STool === sTool && stEst.grams !== null && stEst.grams !== undefined) {
purG = (purG !== null ? purG : 0) + Number(stEst.grams);
} else if (stEst && stEst.active && g9111STool === sTool && (stEst.grams === null || stEst.grams === undefined)) {
purgeKnown = false;
}
if (purG !== null) {
totalPurgeG += purG;
}
const totG = (pG !== null && purG !== null) ? (pG + purG) : null;
const matTxt = String(fil.material || "—");
const pTxt = pG !== null ? (pG.toFixed(1) + " g") : "—";
const purTxt = purG !== null ? (purG.toFixed(1) + " g") : "—";
const totTxt = totG !== null ? (totG.toFixed(1) + " g") : "—";
extLines.push(`T${sTool} (${matTxt}): ${t("ui.modal.pred.print")} ${pTxt} · ${t("ui.modal.pred.purge")} ${purTxt} · ${t("ui.modal.pred.total")} ${totTxt}`);
extHtmlParts.push(
`<div class="usage-info-pop-ext"><span>${filamentColorDot(sTool)} T${sTool} (${escapeHtml(matTxt)})</span><span>${escapeHtml(pTxt)} + ${escapeHtml(purTxt)} = <strong>${escapeHtml(totTxt)}</strong></span></div>`
);
});

const printSumStr = printKnown ? (totalPrintG.toFixed(1) + " g") : "—";
const purgeSumBase = purgeKnown ? (totalPurgeG.toFixed(1) + " g") : "—";
const purgeNote = (stEst && stEst.active && Number(stEst.length_mm || 0) > 0)
? (" (" + t("ui.modal.job.purge_startup_added", { len: Math.round(Number(stEst.length_mm)) }) + ")")
: ((stEst && stEst.skipped) ? (" (" + t("ui.modal.job.purge_startup_skipped") + ")") : "");
const purgeSumStr = purgeSumBase + purgeNote;
const totalSumStr = (printKnown && purgeKnown) ? ((totalPrintG + totalPurgeG).toFixed(1) + " g") : "—";

usageBadge.removeAttribute("title");
usagePop.innerHTML = `
<div class="usage-info-pop-title">${escapeHtml(t("ui.map.usage_info_heading"))}</div>
<div class="usage-info-pop-row"><span>${escapeHtml(t("ui.map.usage_info_print"))}</span><strong>${escapeHtml(printSumStr)}</strong></div>
<div class="usage-info-pop-row"><span>${escapeHtml(t("ui.map.usage_info_purge"))}</span><strong>${escapeHtml(purgeSumStr)}</strong></div>
<div class="usage-info-pop-row"><span>${escapeHtml(t("ui.map.usage_info_total"))}</span><strong>${escapeHtml(totalSumStr)}</strong></div>
<div class="usage-info-pop-sep"></div>
<div style="font-size:10px;color:var(--muted);margin-bottom:3px;font-weight:700;">${escapeHtml(t("ui.map.usage_info_ext"))}</div>
${extHtmlParts.join("")}
`;
usageWrap.style.display = "inline-flex";
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

function resolveFilamentDensityJs(fil, spInfo) {
const candidates = [fil ? fil.density : null, spInfo ? spInfo.density : null];
for (let i = 0; i < candidates.length; i++) {
const val = Number(candidates[i]);
if (candidates[i] !== null && candidates[i] !== undefined && Number.isFinite(val) && val > 0) {
return val;
}
}
const usedG = Number(fil && fil.used_g ? fil.used_g : 0);
const usedCm3 = Number(fil && fil.used_cm3 ? fil.used_cm3 : 0);
const usedMm = Number(fil && fil.used_mm ? fil.used_mm : 0);
const diam = Number(fil && fil.diameter ? fil.diameter : 1.75);
if (Number.isFinite(usedG) && usedG > 0) {
if (Number.isFinite(usedCm3) && usedCm3 > 0) return usedG / usedCm3;
if (Number.isFinite(usedMm) && usedMm > 0 && Number.isFinite(diam) && diam > 0) {
const area = Math.PI * (diam * 0.5) * (diam * 0.5);
const cm3 = (usedMm * area) / 1000.0;
if (cm3 > 0) return usedG / cm3;
}
}
return null;
}

function resolveFilamentPrintUsageJs(fil, density) {
const usedG = Number(fil && fil.used_g ? fil.used_g : 0);
if (Number.isFinite(usedG) && usedG > 0) return usedG;
if (density !== null && Number.isFinite(density) && density > 0) {
const usedCm3 = Number(fil && fil.used_cm3 ? fil.used_cm3 : 0);
if (Number.isFinite(usedCm3) && usedCm3 > 0) return usedCm3 * density;
const usedMm = Number(fil && fil.used_mm ? fil.used_mm : 0);
const diam = Number(fil && fil.diameter ? fil.diameter : 1.75);
if (Number.isFinite(usedMm) && usedMm > 0 && Number.isFinite(diam) && diam > 0) {
const area = Math.PI * (diam * 0.5) * (diam * 0.5);
return ((usedMm * area) / 1000.0) * density;
}
}
return null;
}

function computePreprintFilamentPredictionLocal() {
const rawRes = Number(currentState.prediction_reserve_g !== undefined ? currentState.prediction_reserve_g : 15);
const reserveG = (Number.isFinite(rawRes) && rawRes >= 0) ? Math.round(rawRes * 100) / 100 : 15;
const rawStartupLen = Number(currentState.startup_purge_length_mm !== undefined ? currentState.startup_purge_length_mm : 200);
const startupLenMm = (Number.isFinite(rawStartupLen) && rawStartupLen >= 0) ? Math.round(Math.min(1000, rawStartupLen) * 10) / 10 : 200;
const skipPurgeLoaded = currentState.skip_purge_for_already_loaded_tool !== false;
const fils = Array.isArray(currentState.slicer_filaments) ? currentState.slicer_filaments : [];
const pp = (currentState.last_postprocess_info && typeof currentState.last_postprocess_info === "object")
? currentState.last_postprocess_info
: {};
const hasGcode = !!String(currentState.last_gcode_path || "").trim();
if (!hasGcode) {
return { has_gcode: false, reserve_g: reserveG, overall_status: "empty", counts: { sufficient: 0, low: 0, insufficient: 0, unknown: 0 }, slots: [] };
}

const slots = Array.isArray(currentState.ace_slots) ? currentState.ace_slots : [];
const slotByTool = {};
slots.forEach(s => { if (s && s.tool !== undefined) slotByTool[Number(s.tool)] = s; });

const hasAnyUsage = hasFilamentUsageData(fils);
const usedFils = fils.filter(f => isFilamentUsed(f, hasAnyUsage));
const filByTool = {};
const slotOrder = [];
const toolsForSlot = {};
usedFils.forEach(f => {
const sTool = Number(f.slicer_tool);
filByTool[sTool] = f;
const phys = (currentState.tool_map && currentState.tool_map[sTool] !== undefined)
? Number(currentState.tool_map[sTool])
: sTool;
if (!toolsForSlot[phys]) {
toolsForSlot[phys] = [];
slotOrder.push(phys);
}
toolsForSlot[phys].push(sTool);
});

const events = Array.isArray(pp.events) ? pp.events : [];
const flushIncomplete = (String(pp.flush_status || "") === "FLUSH_DATA_INCOMPLETE")
|| (pp.flush_data_complete === false && usedFils.length > 1);
const purgeBySlot = {};
const purgeErrBySlot = {};
slotOrder.forEach(phys => { purgeBySlot[phys] = 0; });

events.forEach(ev => {
if (!ev || typeof ev !== "object") return;
const sTool = Number(ev.new_slicer_tool);
const volMm3 = Number(ev.volume_mm3 || 0);
const phys = (currentState.tool_map && currentState.tool_map[sTool] !== undefined)
? Number(currentState.tool_map[sTool])
: sTool;
if (purgeBySlot[phys] === undefined || !Number.isFinite(volMm3) || volMm3 <= 0) return;
const filObj = filByTool[sTool] || {};
const slotObj = slotByTool[phys];
const spObj = (slotObj && slotObj.spoolman && typeof slotObj.spoolman === "object") ? slotObj.spoolman : null;
const dens = resolveFilamentDensityJs(filObj, spObj);
if (dens === null) {
if (!purgeErrBySlot[phys]) purgeErrBySlot[phys] = "no_density";
return;
}
purgeBySlot[phys] += (volMm3 / 1000.0) * dens;
});
if (flushIncomplete) {
slotOrder.forEach(phys => { purgeErrBySlot[phys] = "flush_incomplete"; });
}

let startupEst = {
active: false,
skipped: false,
slot: null,
length_mm: 0,
configured_length_mm: startupLenMm,
grams: 0,
reason: startupLenMm <= 0 ? "disabled" : "no_g9111"
};
const rawG9111Tool = pp.g9111StartSlicerTool;
if (rawG9111Tool !== null && rawG9111Tool !== undefined && startupLenMm > 0) {
const g9111STool = Number(rawG9111Tool);
if (Number.isFinite(g9111STool)) {
const startPhys = (currentState.tool_map && currentState.tool_map[g9111STool] !== undefined)
? Number(currentState.tool_map[g9111STool])
: g9111STool;
let loadedSlot = null;
const aceSt = (currentState.ace_status && typeof currentState.ace_status === "object") ? currentState.ace_status : null;
if (aceSt && aceSt.connected === true) {
const cIdx = Number(aceSt.current_index);
if (Number.isFinite(cIdx) && cIdx >= 0) loadedSlot = cIdx;
} else if (!aceSt) {
for (let i = 0; i < slots.length; i++) {
const sl = slots[i];
if (sl && (sl.is_loaded === true || String(sl.status || "").toLowerCase() === "loaded")) {
const tVal = Number(sl.tool !== undefined ? sl.tool : sl.index);
if (Number.isFinite(tVal) && tVal >= 0) { loadedSlot = tVal; break; }
}
}
}
const startSlotObj = slotByTool[startPhys];
const startSlotOk = !!(startSlotObj && String(startSlotObj.status || "empty").toLowerCase() !== "empty");
if (skipPurgeLoaded && loadedSlot !== null && loadedSlot === startPhys && startSlotOk) {
startupEst = {
active: false,
skipped: true,
slot: startPhys,
length_mm: 0,
configured_length_mm: startupLenMm,
grams: 0,
reason: "same_tool_loaded"
};
} else {
const filObj = filByTool[g9111STool] || {};
const spObj = (startSlotObj && startSlotObj.spoolman && typeof startSlotObj.spoolman === "object") ? startSlotObj.spoolman : null;
const diamRaw = Number(filObj.diameter || pp.filament_diameter || 1.75);
const diamVal = (Number.isFinite(diamRaw) && diamRaw > 0) ? diamRaw : 1.75;
const areaMm2 = Math.PI * Math.pow(diamVal / 2.0, 2);
const startupVolMm3 = startupLenMm * areaMm2;
const dens = resolveFilamentDensityJs(filObj, spObj);
let startupG = null;
if (dens === null) {
if (purgeBySlot[startPhys] !== undefined && !purgeErrBySlot[startPhys]) purgeErrBySlot[startPhys] = "no_density";
} else {
const rawStartupG = (startupVolMm3 / 1000.0) * dens;
startupG = Math.round(rawStartupG * 100) / 100;
if (purgeBySlot[startPhys] !== undefined && !purgeErrBySlot[startPhys]) purgeBySlot[startPhys] += rawStartupG;
}
startupEst = {
active: true,
skipped: false,
slot: startPhys,
length_mm: startupLenMm,
configured_length_mm: startupLenMm,
grams: startupG,
reason: !skipPurgeLoaded ? "skip_disabled" : "tool_change_or_unloaded"
};
}
}
}

const counts = { sufficient: 0, low: 0, insufficient: 0, unknown: 0 };
const entries = slotOrder.map(phys => {
const slotObj = slotByTool[phys];
const spObj = (slotObj && slotObj.spoolman && typeof slotObj.spoolman === "object") ? slotObj.spoolman : null;
const sTools = toolsForSlot[phys];

let printTotal = 0;
for (let i = 0; i < sTools.length; i++) {
const filObj = filByTool[sTools[i]] || {};
const dens = resolveFilamentDensityJs(filObj, spObj);
const pg = resolveFilamentPrintUsageJs(filObj, dens);
if (pg === null) { printTotal = null; break; }
printTotal += pg;
}
const purgeErr = purgeErrBySlot[phys] || "";
const purgeTotal = purgeErr ? null : Math.round((purgeBySlot[phys] || 0) * 100) / 100;
if (printTotal !== null) printTotal = Math.round(printTotal * 100) / 100;
const totalUsage = (printTotal !== null && purgeTotal !== null)
? Math.round((printTotal + purgeTotal) * 100) / 100
: null;

let availG = null;
let availReason = "";
if (!slotObj) availReason = "slot_missing";
else if (String(slotObj.status || "empty").toLowerCase() === "empty") availReason = "slot_empty";
else if (!spObj) availReason = "no_spool";
else if (spObj.conflict) availReason = "conflict";
else if (spObj.weight_status === "missing" || spObj.remaining_weight === null || spObj.remaining_weight === undefined) availReason = "no_weight";
else {
const rw = Number(spObj.remaining_weight);
if (Number.isFinite(rw) && rw >= 0) availG = Math.round(rw * 100) / 100;
else availReason = "no_weight";
}

let status = "unknown";
let reason = "";
let remAfter = null;
let deficitG = 0;
if (availG === null) {
reason = availReason || "no_weight";
} else if (printTotal === null) {
reason = "no_usage";
} else if (purgeTotal === null) {
reason = purgeErr || "no_density";
} else {
remAfter = Math.round((availG - totalUsage) * 100) / 100;
if (remAfter < -1e-6) {
status = "insufficient";
deficitG = Math.round(Math.abs(remAfter) * 100) / 100;
} else if (remAfter < reserveG - 1e-6) {
status = "low";
} else {
status = "sufficient";
}
}
counts[status] += 1;

const capVal = (spObj && spObj.capacity_weight) ? Number(spObj.capacity_weight) : 0;
const scaleMax = (Number.isFinite(capVal) && capVal > 0) ? capVal : ((availG !== null && availG > 0) ? availG : 0);
const pctBefore = (scaleMax > 0 && availG !== null) ? Math.max(0, Math.min(100, (availG / scaleMax) * 100)) : null;
const pctAfter = (scaleMax > 0 && remAfter !== null) ? Math.max(0, Math.min(100, (Math.max(0, remAfter) / scaleMax) * 100)) : null;

const firstFil = filByTool[sTools[0]] || {};
const hexCol = (spObj && !spObj.conflict && spObj.color_hex)
? safeHexColor(spObj.color_hex, "#64748B")
: (slotObj ? resolveSlotColorHex(slotObj, safeHexColor(firstFil.color, "#64748B")) : safeHexColor(firstFil.color, "#64748B"));
const spoolName = (spObj && !spObj.conflict) ? String(spObj.display_name || spObj.filament_name || "").trim() : "";

return {
slot_tool: phys,
slot_label: "T" + phys,
material: String((slotObj && slotObj.material) || firstFil.material || "").trim(),
hex_color: hexCol,
spool_id: (spObj && !spObj.conflict) ? spObj.spool_id : null,
spool_name: spoolName,
available_weight_g: availG,
print_usage_g: printTotal,
purge_usage_g: purgeTotal,
total_usage_g: totalUsage,
remaining_after_g: remAfter,
deficit_g: deficitG,
remaining_pct_before: pctBefore,
remaining_pct_after: pctAfter,
status: status,
reason: reason
};
});

const overall = !entries.length ? "empty"
: (counts.insufficient > 0 ? "insufficient"
: (counts.unknown > 0 ? "unknown"
: (counts.low > 0 ? "low" : "sufficient")));
return { has_gcode: true, reserve_g: reserveG, overall_status: overall, counts: counts, slots: entries, startup_purge_estimate: startupEst };
}

function renderPreprintPrediction() {
renderMappingTable();
}

function updateToolMapping(slicerTool, physicalTool) {
currentState.tool_map[slicerTool] = parseInt(physicalTool, 10);
if (!currentState.tool_map_sources) currentState.tool_map_sources = {};
currentState.tool_map_sources[slicerTool] = "manual";
resetMismatchConfirmationUi();
renderMappingTable();
sendToHost({ type: "update_mapping", settings: collectSettingsFromUI() });
}

function onMismatchConfirmChange(checked) {
const ack = !!checked;
_localMismatchAck = ack;
_localMismatchAckContextKey = ack ? computeMismatchContextKey(currentState) : "";
currentState.material_mismatch_confirmed = ack;
renderMappingTable();
syncSettings();
}

function resetColorMapping() {
resetMismatchConfirmationUi();
sendToHost({ type: "reset_mapping" });
}

function readDryerControls() {
const unitEl = document.getElementById("dryerUnit");
const tempEl = document.getElementById("dryerTemp");
const durEl = document.getElementById("dryerDuration");
const rawUnit = unitEl
? String((unitEl.dataset && unitEl.dataset.value) ? unitEl.dataset.value : (unitEl.value || "all")).trim().toLowerCase()
: String(currentState.dryer_unit || "all").trim().toLowerCase();
const validUnits = ["all", "0", "1", "2", "3"];
const unitCount = getEffectiveAceUnitCount();
const unit = unitCount <= 1 ? "0" : (validUnits.includes(rawUnit) ? rawUnit : "all");
const parsedTemp = tempEl ? parseInt(tempEl.value, 10) : 45;
const rawTemp = Number.isFinite(parsedTemp) ? parsedTemp : 45;
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
const smEl = document.getElementById("spoolmanUrl");
const gcDirEl = document.getElementById("defaultGcodeDirInput");
const diagLogEl = document.getElementById("chkDiagLoadLogging");
const pNameEl = document.getElementById("printerProfileName");
const resEl = document.getElementById("predictionReserveInput");
const parsedRes = resEl ? parseFloat(resEl.value) : Number(currentState.prediction_reserve_g);
const predReserve = (Number.isFinite(parsedRes) && parsedRes >= 0) ? Math.min(500, parsedRes) : 15;
const spLenEl = document.getElementById("startupPurgeLengthInput");
const parsedSpLen = spLenEl ? parseFloat(spLenEl.value) : Number(currentState.startup_purge_length_mm);
const startupPurgeLen = (Number.isFinite(parsedSpLen) && parsedSpLen >= 0) ? Math.min(1000, parsedSpLen) : 200;
const cfgAceEl = document.getElementById("configuredAceCount");
const cfgAceVal = (cfgAceEl && cfgAceEl.dataset && cfgAceEl.dataset.value)
? String(cfgAceEl.dataset.value).trim().toLowerCase()
: getConfiguredAceCountValue();
const dryer = readDryerControls();
const extSpoolEl = document.getElementById("chkExternalSpool");
return {
moonraker_url: document.getElementById("moonrakerUrl").value.trim(),
api_key: document.getElementById("apiKey").value.trim(),
spoolman_url: smEl ? smEl.value.trim() : "",
default_gcode_dir: gcDirEl ? gcDirEl.value.trim() : String(currentState.default_gcode_dir || ""),
printer_profile_name: pNameEl ? pNameEl.value.trim() : (getActivePrinterProfile() ? getActivePrinterProfile().name : "Kobra S1"),
diag_load_logging: diagLogEl ? !!diagLogEl.checked : !!currentState.diag_load_logging,
configured_ace_count: ["auto", "1", "2", "3", "4"].includes(cfgAceVal) ? cfgAceVal : "auto",
prediction_reserve_g: predReserve,
startup_purge_length_mm: startupPurgeLen,
theme_mode: currentState.theme_mode || "auto",
color_scheme: currentState.color_scheme || "teal",
enable_port80_fallback: document.getElementById("port80Fallback").checked,
bed_leveling: true,
skip_purge_for_already_loaded_tool: document.getElementById("chkSkipPurgeLoaded").checked,
cut_tip_after_print: document.getElementById("chkCutTipPost").checked,
force_initial_purge: false,
external_spool_mode: isKlipperAceDisabled() ? !!currentState.external_spool_mode : !!(extSpoolEl && extSpoolEl.checked),
external_spool_id: (currentState.external_spool_id !== undefined && currentState.external_spool_id !== null && Number(currentState.external_spool_id) > 0) ? Number(currentState.external_spool_id) : null,
material_mismatch_confirmed: gateChk ? !!gateChk.checked : !!currentState.material_mismatch_confirmed,
last_gcode_path: document.getElementById("gcodePathInput").value.trim(),
dryer_unit: dryer.unit,
dryer_temp: dryer.temp,
dryer_duration: dryer.duration,
tool_map: currentState.tool_map,
tool_map_sources: currentState.tool_map_sources || {}
};
}

function renderSpoolmanStatusBadge() {
const badge = document.getElementById("spoolmanStatusBadge");
if (!badge) return;
const st = currentState.spoolman_status || {};
const url = String(st.url || currentState.spoolman_url || "").trim();
if (!st.enabled && !url) {
badge.className = "badge empty";
badge.textContent = t("ui.sec1.spoolman_off");
return;
}
if (st.connected) {
const conflicts = Number(st.conflict_count || 0);
if (conflicts > 0) {
badge.className = "badge warn";
badge.textContent = t("ui.sec1.spoolman_ok_conflicts", {
spools: Number(st.spools_count || 0),
matched: Number(st.matched_count || 0),
conflicts: conflicts
});
} else {
badge.className = "badge ok";
badge.textContent = t("ui.sec1.spoolman_ok", {
spools: Number(st.spools_count || 0),
matched: Number(st.matched_count || 0)
});
}
return;
}
if (st.error) {
badge.className = "badge warn";
badge.textContent = t("ui.sec1.spoolman_err", { error: st.error });
return;
}
badge.className = "badge info";
badge.textContent = url;
}

function testSpoolmanConnection() {
const smEl = document.getElementById("spoolmanUrl");
const smUrl = smEl ? smEl.value.trim() : "";
currentState.spoolman_url = smUrl;
sendToHost({
type: "test_spoolman",
spoolman_url: smUrl,
settings: collectSettingsFromUI()
});
}

function syncSettings() {
settingsModalDirty = false;
const settings = collectSettingsFromUI();
currentState.prediction_reserve_g = settings.prediction_reserve_g;
currentState.startup_purge_length_mm = settings.startup_purge_length_mm;
renderPreprintPrediction();
sendToHost({ type: "save_settings", settings: settings });
}

function onPrePrintPurgeOptionChange() {
syncSettings();
if (String(currentState.last_gcode_path || "").trim()) {
previewGcodeFlush();
}
}

function saveSettingsAndRefresh() {
syncSettings();
requestStatus();
}

let _lastAutoSlicerSyncMs = 0;
let _lastAutoLiveStatusMs = 0;
const AUTO_SLICER_SYNC_COOLDOWN_MS = 800;
function autoSyncFromSlicerOnTabActivate() {
if (typeof document !== "undefined" && document.hidden) return false;
if ((typeof isStandaloneModal !== "undefined" && isStandaloneModal) || (typeof document !== "undefined" && document.body && document.body.classList && document.body.classList.contains("standalone-modal"))) return false;
const src = String((currentState && currentState.last_gcode_source) || "").trim();
if (src === "file_picker") return false;
const nowMs = Date.now();
if (nowMs - _lastAutoSlicerSyncMs < AUTO_SLICER_SYNC_COOLDOWN_MS) return false;
_lastAutoSlicerSyncMs = nowMs;
_lastAutoLiveStatusMs = nowMs;
sendToHost({ type: "acquire_slicer_gcode", gcode_path: "", force_refresh: false, auto_tab_sync: true, refresh_live_status: true });
return true;
}
function onTabActivatedRefresh() {
if (typeof document !== "undefined" && document.hidden) return false;
const nowMs = Date.now();
const syncedSlicer = autoSyncFromSlicerOnTabActivate();
if (syncedSlicer) {
requestStatus();
return true;
}
if (nowMs - _lastAutoLiveStatusMs < AUTO_SLICER_SYNC_COOLDOWN_MS) return false;
_lastAutoLiveStatusMs = nowMs;
requestStatus();
return true;
}
if (typeof window !== "undefined" && typeof document !== "undefined" && document.addEventListener && window.addEventListener) {
document.addEventListener("visibilitychange", function () {
if (!document.hidden) onTabActivatedRefresh();
});
window.addEventListener("focus", function () {
onTabActivatedRefresh();
});
}

function requestStatus() {
if (hasReceivedInitialState) {
sendToHost({ type: "get_status", settings: collectSettingsFromUI() });
} else {
sendToHost({ type: "get_status" });
}
}

function onDryerSettingsChange() {
const d = readDryerControls();
currentState.dryer_unit = d.unit;
currentState.dryer_temp = d.temp;
currentState.dryer_duration = d.duration;
syncDryerPresetFromTemp(d.temp, false);
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

let _pendingFullUnloadTool = null;

function confirmFullUnload(tool) {
_pendingFullUnloadTool = Number(tool);
const modal = document.getElementById("fullUnloadConfirmModal");
const titleEl = document.getElementById("fullUnloadModalTitle");
const bodyEl = document.getElementById("fullUnloadModalBody");
const btnEl = document.getElementById("btnConfirmFullUnloadExec");
if (titleEl) titleEl.textContent = t("ui.slot.full_unload_modal_title", { tool: _pendingFullUnloadTool });
if (bodyEl) bodyEl.textContent = t("ui.slot.full_unload_modal_body", { tool: _pendingFullUnloadTool });
if (btnEl) btnEl.textContent = t("ui.slot.full_unload_modal_confirm", { tool: _pendingFullUnloadTool });
if (modal) modal.classList.add("open");
}

function closeFullUnloadModal() {
_pendingFullUnloadTool = null;
const modal = document.getElementById("fullUnloadConfirmModal");
if (modal) modal.classList.remove("open");
}

function executeConfirmedFullUnload() {
if (_pendingFullUnloadTool === null || _pendingFullUnloadTool === undefined) return;
const tool = _pendingFullUnloadTool;
closeFullUnloadModal();
slotAction("full_unload", tool);
}

function slotAction(action, tool) {
const lenEl = document.getElementById("feedLength");
const len = lenEl ? (parseInt(lenEl.value, 10) || 50) : 50;
sendToHost({
type: "slot_action",
action: action,
tool: tool,
length: len,
unlock_override: !!hardwareUnlocked
});
}

function autoMatchColors() {
resetMismatchConfirmationUi();
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

let _pendingExportFileHandle = null;

function deriveExportGcodeFilename() {
const meta = (currentState && currentState.last_job_metadata) || {};
const raw = String(meta.output_name || meta.filename || currentState.last_gcode_path || "exported.gcode").trim();
let base = (raw.split(/[\\/]/).pop() || "exported.gcode").trim();
const low = base.toLowerCase();
if (low.endsWith(".gcode.3mf")) {
base = base.slice(0, -4);
} else if (low.endsWith(".3mf")) {
base = base.slice(0, -4) + ".gcode";
} else if (!low.endsWith(".gcode") && !low.endsWith(".gco") && !low.endsWith(".gc")) {
base = base + ".gcode";
}
return base || "exported.gcode";
}

let _pendingExportContext = null;

function _defaultExportDir() {
const customDir = String(currentState.default_gcode_dir || "").trim();
if (customDir) return customDir;
const rp = (currentState && currentState.resolved_paths) || {};
const dlDir = String(rp.downloads_dir || "").trim();
if (dlDir) return dlDir;
const expDir = String(rp.exported_dir || "").trim();
if (expDir) return expDir;
const storageDir = String(rp.storage_dir || "").trim();
if (storageDir) {
const sep = storageDir.includes("\\\\") ? "\\\\" : "/";
return storageDir.replace(/[\\\\/]+$/, "") + sep + "exported_gcode";
}
return "";
}

function setExportDirPreset(kind) {
const rp = (currentState && currentState.resolved_paths) || {};
const dirEl = document.getElementById("exportTargetDirInput");
if (!dirEl) return;
if (kind === "downloads") {
dirEl.value = String(rp.downloads_dir || "").trim() || _defaultExportDir();
} else if (kind === "storage") {
const expDir = String(rp.exported_dir || "").trim();
if (expDir) {
dirEl.value = expDir;
} else {
const storageDir = String(rp.storage_dir || "").trim();
const sep = storageDir.includes("\\\\") ? "\\\\" : "/";
dirEl.value = storageDir ? (storageDir.replace(/[\\\\/]+$/, "") + sep + "exported_gcode") : "";
}
}
}

function openExportPathModal(gcodePath, suggestedName, settings) {
_pendingExportContext = { gcodePath: gcodePath, settings: settings };
const dirEl = document.getElementById("exportTargetDirInput");
const fileEl = document.getElementById("exportFilenameInput");
if (dirEl) dirEl.value = _defaultExportDir();
if (fileEl) fileEl.value = suggestedName || "exported.gcode";
const modalEl = document.getElementById("exportPathModal");
if (modalEl) {
modalEl.classList.add("open");
if (fileEl) {
try { fileEl.focus(); fileEl.select(); } catch (_e) {}
}
}
}

function closeExportPathModal() {
_pendingExportContext = null;
const modalEl = document.getElementById("exportPathModal");
if (modalEl) modalEl.classList.remove("open");
}

function confirmExportPatchedGcode() {
const ctx = _pendingExportContext;
if (!ctx) return;
const dirEl = document.getElementById("exportTargetDirInput");
const fileEl = document.getElementById("exportFilenameInput");
const rawDir = String((dirEl && dirEl.value) || "").trim();
let rawFile = String((fileEl && fileEl.value) || "").trim() || deriveExportGcodeFilename();
if (!/\\.(gcode|gco|gc)$/i.test(rawFile)) rawFile += ".gcode";
const sep = rawDir.includes("\\\\") ? "\\\\" : "/";
const destPath = rawDir ? (rawDir.replace(/[\\\\/]+$/, "") + sep + rawFile.replace(/^[\\\\/]+/, "")) : "";
closeExportPathModal();
sendToHost({
type: "export_gcode",
gcode_path: ctx.gcodePath,
dest_path: destPath,
require_mismatch_gate: true,
mismatch_confirmed: ctx.settings.material_mismatch_confirmed,
settings: ctx.settings
});
}

async function exportPatchedGcode() {
const settings = collectSettingsFromUI();
const gcodePath = String(settings.last_gcode_path || currentState.last_gcode_path || "").trim();
if (!gcodePath) {
const msgText = t("ui.modal.feedback.no_gcode_action");
showModalFeedback("warn", msgText);
logMsg(msgText);
return;
}
_pendingExportFileHandle = null;
const suggestedName = deriveExportGcodeFilename();
if (typeof window !== "undefined" && typeof window.showSaveFilePicker === "function") {
try {
_pendingExportFileHandle = await window.showSaveFilePicker({
suggestedName: suggestedName,
types: [{ description: "G-code", accept: { "text/plain": [".gcode", ".gco", ".gc"] } }]
});
} catch (err) {
_pendingExportFileHandle = null;
if (err && err.name === "AbortError") {
return;
}
}
if (_pendingExportFileHandle) {
sendToHost({
type: "export_gcode",
gcode_path: gcodePath,
require_mismatch_gate: true,
mismatch_confirmed: settings.material_mismatch_confirmed,
settings: settings
});
return;
}
}
openExportPathModal(gcodePath, suggestedName, settings);
}

function triggerBlobGcodeDownload(filename, content) {
if (typeof Blob === "undefined" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
return false;
}
const blob = new Blob([String(content || "")], { type: "text/plain;charset=utf-8" });
const url = URL.createObjectURL(blob);
const a = document.createElement("a");
a.href = url;
a.download = filename || "exported.gcode";
a.style.display = "none";
document.body.appendChild(a);
a.click();
setTimeout(() => {
try { document.body.removeChild(a); } catch (_e) {}
try { URL.revokeObjectURL(url); } catch (_e) {}
}, 1000);
return true;
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
const st = currentState.ace_status || {};
if (shouldStartPrint && st.is_printing) {
const busyMsg = t("ui.modal.feedback.printing_busy", { state: st.print_state || "printing" });
showModalFeedback("warn", busyMsg);
logMsg(busyMsg);
return;
}
const payload = {
type: upload ? "postprocess_and_upload" : "postprocess_only",
gcode_path: gcodePath,
require_mismatch_gate: true,
mismatch_confirmed: settings.material_mismatch_confirmed,
start_print: shouldStartPrint,
settings: settings
};
if (upload && isMoonrakerOnline() && !isExternalSpoolBypassActive() && !isKlipperAceDisabled()) {
payload.endless_spool_enabled = !!endlessSpoolDraft.enabled;
payload.endless_spool_mode = normalizeEndlessMode(endlessSpoolDraft.mode);
}
sendToHost(payload);
}

function computeEffectiveToolchanges(info) {
if (!info || typeof info !== "object") return 0;
const rawTc = Number(info.raw_toolchanges !== undefined ? info.raw_toolchanges : (info.toolchanges || 0));
if (!Number.isFinite(rawTc) || rawTc <= 0) return 0;
const skipPurge = (currentState && currentState.skip_purge_for_already_loaded_tool !== undefined)
? Boolean(currentState.skip_purge_for_already_loaded_tool)
: (info.skipPurgeForAlreadyLoadedTool !== false);
const forceInit = (currentState && currentState.force_initial_purge !== undefined)
? Boolean(currentState.force_initial_purge)
: Boolean(info.forceInitialPurge);
if (!skipPurge || forceInit) return rawTc;
const st = (currentState && currentState.ace_status && typeof currentState.ace_status === "object")
? currentState.ace_status
: {};
if (!st.connected) return rawTc;
const curIdx = (st.current_index !== null && st.current_index !== undefined && st.current_index !== "")
? Number(st.current_index)
: -1;
if (!Number.isFinite(curIdx) || curIdx < 0) return rawTc;
const evs = Array.isArray(info.events) ? info.events : [];
let startSlicer = null;
if (info.g9111StartSlicerTool !== null && info.g9111StartSlicerTool !== undefined) {
startSlicer = Number(info.g9111StartSlicerTool);
} else if (evs.length > 0 && evs[0] && evs[0].new_slicer_tool !== undefined) {
startSlicer = Number(evs[0].new_slicer_tool);
}
if (startSlicer === null || !Number.isFinite(startSlicer)) return rawTc;
const tmap = (currentState && currentState.tool_map) || info.toolMap || {};
const mappedRaw = tmap[startSlicer] !== undefined ? tmap[startSlicer] : tmap[String(startSlicer)];
const startPhys = mappedRaw !== undefined ? Number(mappedRaw) : startSlicer;
if (curIdx !== startPhys) return rawTc;
if (evs.length > 0 && evs[0]) {
const evNew = Number(evs[0].new_slicer_tool);
const evOld = evs[0].old_slicer_tool;
if (Number.isFinite(evNew) && evNew !== startSlicer) return rawTc;
if (evOld !== null && evOld !== undefined && Number(evOld) !== startSlicer) return rawTc;
}
return Math.max(0, rawTc - 1);
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
const hasG9111 = (info.g9111StartSlicerTool !== null && info.g9111StartSlicerTool !== undefined);
const g9111Html = hasG9111
? `${filamentColorDot(info.g9111StartSlicerTool)} <span>T${info.g9111StartSlicerTool}</span> ${uiSvg("arrowRight", "ui-icon-sm")} ${aceSlotColorDot(info.g9111MappedTool)} <span>T${info.g9111MappedTool}</span>`
: "—";
const cutStr = t("ui.flush.cut_fmt", {
skip: (info.skipPurgeForAlreadyLoadedTool !== false) ? t("ui.flush.yes") : t("ui.flush.no"),
cut: info.cutTipPost ? t("ui.flush.yes") : t("ui.flush.no"),
});

kpiGrid.innerHTML = `
<div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_mul"))}</div><div class="kpi-val">${mult.toFixed(2)}</div></div>
<div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_diam"))}</div><div class="kpi-val">${diam.toFixed(2)} mm</div></div>
<div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_tc"))}</div><div class="kpi-val">${computeEffectiveToolchanges(info)}</div></div>
<div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_total"))}</div><div class="kpi-val">${totalVol.toFixed(1)} mm³ · ${totalLen.toFixed(1)} mm</div></div>
<div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_g9111"))}</div><div class="kpi-val">${g9111Html}</div></div>
<div class="kpi-box"><div class="kpi-label">${escapeHtml(t("ui.flush.kpi_g31_cut"))}</div><div class="kpi-val" style="font-size:11.5px;">${escapeHtml(cutStr)}</div></div>
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
const vecSummary = Array.isArray(info.vector_summary) ? info.vector_summary : [];
if (vecSummary.length > 0) {
const layout = vecSummary[0].layout || "N";
if (vecLayoutBadge) {
vecLayoutBadge.textContent = layout === "2xN" ? "2×N (Unload / Load)" : "N (Load)";
vecLayoutBadge.className = "badge info";
}
const vecRows = [];
vecSummary.forEach(item => {
const sIdx = Number(item.tool) || 0;
const mappedT = item.mapped_tool !== undefined
? Number(item.mapped_tool)
: (currentState.tool_map[sIdx] !== undefined ? Number(currentState.tool_map[sIdx]) : sIdx);
const unloadTxt = (item.unload_mm3 === null || item.unload_mm3 === undefined)
? "—"
: Number(item.unload_mm3).toFixed(1);
const effLen = Number(item.effective_length_mm || 0);
vecRows.push(`
<tr>
<td>${filamentColorDot(sIdx)} <strong>${escapeHtml(item.label || ('T' + sIdx))}</strong></td>
<td><span class="slot-chip">${aceSlotColorDot(mappedT)} T${mappedT}</span></td>
<td>${unloadTxt}</td>
<td>${Number(item.load_mm3 || 0).toFixed(1)}</td>
<td>${Number(item.effective_load_mm3 || 0).toFixed(1)}</td>
<td><span class="purge-badge ${purgeTierClass(effLen)}">${effLen.toFixed(2)} mm</span></td>
</tr>
`);
});
vecBody.innerHTML = vecRows.join("");
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
const vecRows = [];
for (let i = 0; i < count; i++) {
const mappedT = currentState.tool_map[i] !== undefined ? Number(currentState.tool_map[i]) : i;
const unloadV = is2n ? Number(vec[2 * i] || 0).toFixed(1) : "—";
const loadV = is2n ? Number(vec[2 * i + 1] || 0) : Number(vec[i] || 0);
const effV = loadV * mult;
const pLen = calcPurgeLenMm(effV, diam);
vecRows.push(`
<tr>
<td>${filamentColorDot(i)} <strong>T${i}</strong></td>
<td><span class="slot-chip">${aceSlotColorDot(mappedT)} T${mappedT}</span></td>
<td>${unloadV}</td>
<td>${loadV.toFixed(1)}</td>
<td>${effV.toFixed(1)}</td>
<td><span class="purge-badge ${purgeTierClass(pLen)}">${pLen.toFixed(2)} mm</span></td>
</tr>
`);
}
vecBody.innerHTML = vecRows.join("");
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
mHtml += `<td style="${bg}"><span class="matrix-cell-mm">${plen.toFixed(1)} mm</span><span class="matrix-cell-vol">${val.toFixed(0)} mm³</span></td>`;
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
if (events.length === 0) {
evBody.innerHTML = `<tr><td colspan="6" style="color:var(--muted);">${escapeHtml(t("ui.flush.no_events"))}</td></tr>`;
} else {
const evRows = [];
events.forEach(ev => {
const oldT = (ev.old_slicer_tool === null || ev.old_slicer_tool === undefined)
? t("ui.flush.init")
: `T${Number(ev.old_slicer_tool)}`;
const newSlicerT = Number(ev.new_slicer_tool);
const mappedT = currentState.tool_map[newSlicerT] !== undefined
? Number(currentState.tool_map[newSlicerT])
: Number(ev.mapped_tool);
const pLen = Number(ev.purge_length_mm || 0);
evRows.push(`
<tr>
<td>${Number(ev.index)}</td>
<td>${filamentColorDot(newSlicerT)} ${escapeHtml(oldT)} → T${newSlicerT}</td>
<td><span class="slot-chip">${aceSlotColorDot(mappedT)} T${mappedT}</span></td>
<td>${Number(ev.raw_volume_mm3 || 0).toFixed(1)}</td>
<td>${Number(ev.volume_mm3 || 0).toFixed(1)}</td>
<td><span class="purge-badge ${purgeTierClass(pLen)}">${pLen.toFixed(2)} mm</span> <span class="cmd-pill">PURGELENGTH=${pLen.toFixed(2)}</span></td>
</tr>
`);
});
evBody.innerHTML = evRows.join("");
}
}

if (typeof orca !== "undefined" && typeof orca.onMessage === "function") {
orca.onMessage(msg => {
if (!msg || typeof msg !== "object") return;
ensureLogInitialized();
if (msg.log) logMsg(msg.log);
// --- TEMP DIAGNOSTIC LOAD LOGGING (START) ---
if (msg.diag_log) logMsg(msg.diag_log);
// --- TEMP DIAGNOSTIC LOAD LOGGING (END) ---
if (msg.ace_status) {
const st = msg.ace_status;
currentState.ace_status = st;
currentState.ace_slots = st.slots || currentState.ace_slots;
if (st.spoolman_status) {
currentState.spoolman_status = st.spoolman_status;
}
}
let prevGcodePath = String(currentState.last_gcode_path || "").trim();
let prevGcodeSource = String(currentState.last_gcode_source || "").trim();
if (msg.state) {
hasReceivedInitialState = true;
const prevActivePrinter = String(currentState.active_printer_id || "");
const nextActivePrinter = String(msg.state.active_printer_id || prevActivePrinter);
const prevDryerTemp = currentState.dryer_temp;
Object.assign(currentState, msg.state);
const nowGcodeAfterAssign = String(currentState.last_gcode_path || "").trim();
const nextMismatchCtxKey = computeMismatchContextKey(currentState);
if (
!nowGcodeAfterAssign
|| (prevGcodePath && nowGcodeAfterAssign !== prevGcodePath)
|| (_localMismatchAck && _localMismatchAckContextKey && nextMismatchCtxKey !== _localMismatchAckContextKey)
) {
_localMismatchAck = false;
_localMismatchAckContextKey = "";
if (!nowGcodeAfterAssign || (prevGcodePath && nowGcodeAfterAssign !== prevGcodePath)) {
currentState.material_mismatch_confirmed = false;
}
} else if (_localMismatchAck && _localMismatchAckContextKey && nextMismatchCtxKey === _localMismatchAckContextKey) {
currentState.material_mismatch_confirmed = true;
} else if (msg.state.material_mismatch_confirmed !== undefined) {
currentState.material_mismatch_confirmed = !!msg.state.material_mismatch_confirmed;
}
if (msg.state.language) {
currentLanguage = normalizeLang(msg.state.language);
}
if (msg.state.theme_mode) {
setThemeMode(msg.state.theme_mode, false);
}
if (msg.state.color_scheme) {
setColorScheme(msg.state.color_scheme, false);
}
syncSettingsInputsFromState(prevActivePrinter !== nextActivePrinter, true);
document.getElementById("chkSkipPurgeLoaded").checked = currentState.skip_purge_for_already_loaded_tool !== false;
document.getElementById("chkCutTipPost").checked = !!currentState.cut_tip_after_print;
syncExternalSpoolUi();
const unitEl = document.getElementById("dryerUnit");
if (unitEl && !unitEl.classList.contains("open") && msg.state.dryer_unit !== undefined && unitEl.dataset) {
unitEl.dataset.value = String(currentState.dryer_unit || "all");
}
const tempEl = document.getElementById("dryerTemp");
if (tempEl && document.activeElement !== tempEl && msg.state.dryer_temp !== undefined) {
const ddPreset = document.getElementById("dryerPreset");
if (ddPreset && ddPreset.dataset && ddPreset.dataset.value === "custom" && String(msg.state.dryer_temp) !== String(prevDryerTemp)) {
ddPreset.dataset.value = "";
}
tempEl.value = String(currentState.dryer_temp);
}
const durEl = document.getElementById("dryerDuration");
if (durEl && document.activeElement !== durEl && msg.state.dryer_duration !== undefined) {
durEl.value = String(currentState.dryer_duration);
}
const gateChk = document.getElementById("chkConfirmMaterialMismatch");
if (gateChk) gateChk.checked = !!currentState.material_mismatch_confirmed;
}
const nowGcodePath = String(currentState.last_gcode_path || "").trim();
if (nowGcodePath) {
hideNoGcodeNotice();
if (_openModalAfterBrowse) {
_openModalAfterBrowse = false;
openDrawer("prePrintModal");
}
}
if (msg.anonymized_diagnostics !== undefined) {
currentState.anonymized_diagnostics = String(msg.anonymized_diagnostics || "");
const preEl = document.getElementById("anonDiagPreview");
if (preEl) preEl.textContent = currentState.anonymized_diagnostics;
}
if (msg.spoolman_status) {
currentState.spoolman_status = msg.spoolman_status;
}
applyTranslations();
const prePrintElNow = document.getElementById("prePrintModal");
if (
prePrintElNow
&& prePrintElNow.classList.contains("open")
&& msg.action !== "open_preprint_modal"
&& prevGcodePath
&& prevGcodeSource !== "file_picker"
&& !nowGcodePath
) {
closePrePrintModal();
}
if (msg.action === "open_preprint_modal") {
setPrepareBtnLoading(false);
syncEndlessSpoolUi(true);
if (msg.standalone_modal === true) {
isStandaloneModal = true;
if (typeof document !== "undefined" && document.body && document.body.classList) {
document.body.classList.add("standalone-modal");
}
}
hideNoGcodeNotice();
closeAllSlotDropdowns();
closeThumbZoomModal();
for (const secId of ["settingsModal", "diagnosticsModal", "logModal"]) {
closeDrawer(secId);
}
clearModalFeedback();
openDrawer("prePrintModal");
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
} else if (msg.action === "slicing_in_progress") {
setPrepareBtnLoading(false);
_pendingExportFileHandle = null;
closePrePrintModal();
showNoGcodeNotice((msg.notice && msg.notice.text) || t("ui.notice.slicing_in_progress"), "info");
} else if (msg.action === "close_preprint_modal") {
setPrepareBtnLoading(false);
_pendingExportFileHandle = null;
closePrePrintModal(true);
if (msg.notice && msg.notice.text) {
showNoGcodeNotice(msg.notice.text, msg.notice.level || "warn");
}
} else if (msg.action === "no_gcode_notice") {
setPrepareBtnLoading(false);
_pendingExportFileHandle = null;
closePrePrintModal();
showNoGcodeNotice((msg.notice && msg.notice.text) || "", (msg.notice && msg.notice.level) || "warn");
} else if (msg.action === "export_gcode_ready") {
const expName = String(msg.filename || "exported.gcode");
const expContent = String(msg.content || "");
const handle = _pendingExportFileHandle;
_pendingExportFileHandle = null;
if (handle && typeof handle.createWritable === "function") {
Promise.resolve(handle.createWritable())
.then(async writable => {
await writable.write(expContent);
await writable.close();
const okMsg = t("host.export_done_ui", { file: expName, tc: (currentState.last_postprocess_info && currentState.last_postprocess_info.toolchanges) || 0, len: Number((currentState.last_postprocess_info && currentState.last_postprocess_info.total_purge_length_mm) || 0).toFixed(2) });
showModalFeedback("ok", okMsg);
logMsg(okMsg);
})
.catch(err => {
const errTxt = String((err && err.message) || err || "Export failed");
showModalFeedback("error", errTxt);
logMsg(errTxt);
});
} else if (triggerBlobGcodeDownload(expName, expContent)) {
const okMsg = t("host.export_done_ui", { file: expName, tc: (currentState.last_postprocess_info && currentState.last_postprocess_info.toolchanges) || 0, len: Number((currentState.last_postprocess_info && currentState.last_postprocess_info.total_purge_length_mm) || 0).toFixed(2) });
showModalFeedback("ok", okMsg);
}
}
if (msg.modal_feedback && typeof msg.modal_feedback === "object") {
if (msg.modal_feedback.level === "error") {
_pendingExportFileHandle = null;
}
showModalFeedback(msg.modal_feedback.level || "info", msg.modal_feedback.text || "");
}
});
}

document.addEventListener("click", () => closeAllSlotDropdowns());
document.addEventListener("keydown", evt => {
if (evt.key !== "Escape") return;
const expEl = document.getElementById("exportPathModal");
if (expEl && expEl.classList.contains("open")) {
evt.preventDefault();
closeExportPathModal();
return;
}
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
if ((prePrintEl && prePrintEl.classList.contains("open")) || isStandaloneModal) {
closePrePrintModal();
}
});

bindSafeBackdropClose("settingsModal", () => closeDrawer("settingsModal"));
bindSafeBackdropClose("diagnosticsModal", () => closeDrawer("diagnosticsModal"));
bindSafeBackdropClose("logModal", () => closeDrawer("logModal"));
initTheme();
ensureLogInitialized();
  applyTranslations();
sendToHost({ type: "ready" });
</script>
</body>
</html>
""".replace("__I18N_JSON__", json.dumps(TRANSLATIONS, ensure_ascii=False)).replace(
    "__COLOR_NAME_I18N_JSON__", json.dumps(_COLOR_NAME_TRANSLATIONS, ensure_ascii=False)
).replace("__KOBRA_ORCA_LOGO_URI__", KOBRA_ORCA_LOGO_DATA_URI)


# 13. OrcaSlicer 2.5.x Capability Classes & Plugin Registration
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
                    fn(PLUGIN_TAB_NAME)
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
            def _post_state_only() -> None:
                with _WorkerThreadScope():
                    try:
                        page.post_message({"state": GLOBAL_STATE.to_dict()})
                    except Exception:
                        pass

            if use_thread:
                threading.Thread(target=_post_state_only, daemon=True).start()
            else:
                _post_state_only()
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
        with _WorkerThreadScope():
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


def _derive_lifecycle_output_name(
    raw_candidate: str,
    ctx_name: str = "",
    ctx_output_name: str = "",
    ui_snap: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Preserve explicit output names and visible G-code filenames, while translating hidden
    OrcaSlicer preview files (e.g. `.../Metadata/.<pid>.<idx>.gcode` from `GCodeExportFinished.msg`)
    to a readable `<model>.gcode` filename based on active model metadata.
    """
    explicit_out = str(ctx_output_name or "").strip()
    if explicit_out:
        return explicit_out
    raw_base = os.path.basename(str(raw_candidate or "").strip())
    if not raw_base:
        return ""
    if not raw_base.startswith("."):
        return raw_base
    low_raw = raw_base.lower()
    ext = ".gcode"
    for cand_ext in (".gcode.3mf", ".gcode", ".gco", ".gc", ".3mf"):
        if low_raw.endswith(cand_ext):
            ext = cand_ext
            break
    snap_model = str(((ui_snap or {}).get("active_model") or {}).get("display_name") or "").strip()
    ctx_clean = str(ctx_name or "").strip()
    if ctx_clean and ("/" in ctx_clean or "\\" in ctx_clean or _is_gcode_or_container_filename(ctx_clean)):
        ctx_clean = ""
    for cand_label in (snap_model, ctx_clean):
        stem = _strip_model_extension(cand_label)
        if stem and not _is_generic_model_stem(stem):
            return f"{stem}{ext}"
    return f"sliced_plate{ext}"


def _resolve_lifecycle_project_model(
    ctx_name: str = "",
    ui_snap: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve non-generic model name from UI snapshot or lifecycle `ctx.name`."""
    snap_model = str(((ui_snap or {}).get("active_model") or {}).get("display_name") or "").strip()
    if snap_model and not _is_generic_model_stem(snap_model):
        return snap_model
    ctx_clean = str(ctx_name or "").strip()
    if (
        ctx_clean
        and "/" not in ctx_clean
        and "\\" not in ctx_clean
        and not _is_gcode_or_container_filename(ctx_clean)
        and not _is_generic_model_stem(ctx_clean)
    ):
        return ctx_clean
    return snap_model


def _find_updated_preview_temp_source(
    state: PluginState,
    ui_snap: Optional[Dict[str, Any]] = None,
    allow_from_file_picker: bool = False,
) -> str:
    """
    Check strictly OrcaSlicer-known preview temp G-code paths (never scans `/tmp`) for a
    freshly updated file written by a re-slice (e.g. when OrcaSlicer overwrites the same
    preview temp file in-place after modifying a model and re-slicing).
    Returns the eligible path if newer than the staged copy / pre-reslice mtime, else `""`.
    """
    storage_dir = state.resolve_storage_dir()
    with state._lock:
        cur_path = str(state.last_gcode_path or "").strip()
        cur_src = str(state.last_gcode_source or "").strip()
        prev_temp = str(getattr(state, "_last_preview_temp_path", "") or "").strip()
        reslice_temp = str(getattr(state, "_reslice_preview_temp_path", "") or "").strip()
        reslice_prev_mtime = int(getattr(state, "_reslice_preview_prev_mtime_ns", 0) or 0)
        cur_prov = dict(state._last_staged_provenance) if isinstance(state._last_staged_provenance, dict) else {}
        last_slice_ns = int(getattr(state, "_last_slice_completed_ns", 0) or 0)

    if cur_src == "file_picker" and not allow_from_file_picker:
        return ""

    snap = ui_snap if isinstance(ui_snap, dict) else state.get_host_ui_snapshot()
    am = snap.get("active_model") if isinstance(snap, dict) else {}
    if isinstance(am, dict) and am.get("host_has_slice") is False:
        return ""
    host_gcode = str((am or {}).get("host_gcode_path") or "").strip() if isinstance(am, dict) else ""

    sidecar = cur_prov or (_read_provenance_sidecar(cur_path) if cur_path and os.path.isfile(cur_path) else {})
    sc_prev = str(sidecar.get("preview_temp_path") or "").strip() if isinstance(sidecar, dict) else ""
    sc_src_mtime_ns = int(sidecar.get("source_mtime_ns", 0) or 0) if isinstance(sidecar, dict) else 0
    sc_created_ns = int(sidecar.get("created_ns", 0) or 0) if isinstance(sidecar, dict) else 0
    sc_size = int(sidecar.get("file_size", 0) or 0) if isinstance(sidecar, dict) else 0

    candidates: List[Tuple[str, bool]] = []
    seen: Set[str] = set()
    for cand, is_reslice in (
        (host_gcode, False),
        (reslice_temp, True),
        (prev_temp, False),
        (sc_prev, False),
    ):
        if not cand or cand in seen:
            continue
        seen.add(cand)
        candidates.append((cand, is_reslice))

    for cand, is_reslice in candidates:
        if cand.lower().endswith(".tmp") or not _is_gcode_or_container_filename(cand):
            continue
        if not (_is_preview_temp_source(cand, storage_dir) or (host_gcode and cand == host_gcode)):
            continue
        try:
            if not os.path.isfile(cand):
                continue
            st_cand = os.stat(cand)
            if st_cand.st_size <= 0:
                continue
            cand_mtime_ns = int(
                getattr(st_cand, "st_mtime_ns", int(float(st_cand.st_mtime) * 1_000_000_000))
            )
        except OSError:
            continue

        if is_reslice:
            if reslice_prev_mtime > 0 and cand_mtime_ns > reslice_prev_mtime:
                return cand
            continue

        if allow_from_file_picker and (cur_src == "file_picker" or not cur_path):
            return cand

        if not cur_path or not os.path.isfile(cur_path):
            if host_gcode and cand == host_gcode:
                return cand
            continue

        if cur_src == "slicing_pipeline":
            newest_ref_ns = max(sc_src_mtime_ns, sc_created_ns)
            if newest_ref_ns > 0 and cand_mtime_ns <= newest_ref_ns:
                continue

        if (
            (sc_src_mtime_ns > 0 and cand_mtime_ns > sc_src_mtime_ns)
            or (sc_created_ns > 0 and cand_mtime_ns > sc_created_ns)
            or (sc_size > 0 and st_cand.st_size != sc_size and cand_mtime_ns >= sc_src_mtime_ns)
            or (last_slice_ns > 0 and sc_created_ns > 0 and sc_created_ns < last_slice_ns and cand_mtime_ns >= sc_src_mtime_ns)
        ):
            return cand

    return ""


def _capture_lifecycle_explicit_unlocked(
    state: PluginState,
    raw_candidate: str,
    ctx_output_name: str,
    derived_output_name: str,
    plate_idx: Optional[int],
    host_name: str,
    project_model: str,
    deferred_on_perm_error: bool = True,
) -> Tuple[str, Optional[Tuple[str, str, Optional[Dict[str, Any]]]]]:
    """
    Validate, stage, and activate an explicit sliced G-code file delivered via OrcaSlicer
    lifecycle events (including `GCodeExportFinished.ctx.msg` temporary preview G-code).
    Caller must hold `state._pending_preview_lock`.
    """
    storage_dir = state.resolve_storage_dir()
    eff_output_name = derived_output_name or ctx_output_name or os.path.basename(raw_candidate)
    expected_staged = (
        os.path.join(
            storage_dir,
            "staged_gcode",
            _sanitize_staged_gcode_filename(
                eff_output_name,
                plate_index=plate_idx,
            ),
        )
        if storage_dir
        else ""
    )
    with state._lock:
        cur_src = str(state.last_gcode_source or "")
        cur_saved = str(state.last_gcode_path or "")
        cur_prov_snap = dict(state._last_staged_provenance) if isinstance(state._last_staged_provenance, dict) else {}
        pp_recent = bool(getattr(state, "_pipeline_active_since_slice", False))
        last_pp_ns = int(getattr(state, "_last_pipeline_export_ns", 0) or 0)

    with _ACTIVE_MODAL_SESSIONS_LOCK:
        modal_session_active = bool(_ACTIVE_MODAL_SESSIONS)

    pp_active_within_window = (
        pp_recent and last_pp_ns > 0 and (time.time_ns() - last_pp_ns) < 5_000_000_000
    )
    explicit = ""
    already_staged_by_pipeline = False
    prefetched_bytes: Optional[bytes] = None
    is_hidden_preview = os.path.basename(raw_candidate).startswith(".")
    if (
        cur_src == "slicing_pipeline"
        and cur_saved
        and os.path.isfile(cur_saved)
        and (modal_session_active or is_hidden_preview)
    ):
        sc_cur = cur_prov_snap or _read_provenance_sidecar(cur_saved)
        if sc_cur and not sc_cur.get("is_cleared") and not sc_cur.get("is_stale") and not sc_cur.get("is_superseded"):
            cand_mtime_ns = _get_file_mtime_ns(raw_candidate) if os.path.isfile(raw_candidate) else 0
            cur_ref_ns = max(
                int(sc_cur.get("created_ns", 0) or 0),
                int(sc_cur.get("source_mtime_ns", 0) or 0),
            )
            if modal_session_active or (cand_mtime_ns > 0 and cur_ref_ns > 0 and cand_mtime_ns <= cur_ref_ns):
                return ("already_handled", None)
    if (
        expected_staged
        and pp_active_within_window
        and not is_hidden_preview
        and cur_src == "slicing_pipeline"
        and cur_saved
        and os.path.realpath(cur_saved) == os.path.realpath(expected_staged)
        and os.path.isfile(expected_staged)
    ):
        sc_expected = _read_provenance_sidecar(expected_staged)
        if not sc_expected.get("is_cleared"):
            explicit = expected_staged
            already_staged_by_pipeline = True

    if not explicit:
        if not os.path.isfile(raw_candidate):
            return ("missing", None)
        try:
            prefetched_bytes = _read_source_file_single_pass(raw_candidate)
            explicit = raw_candidate
        except PermissionError:
            if deferred_on_perm_error:
                # Worker threads cannot show an inline permission prompt in OrcaSlicer; defer
                # reading so `SlicingJobComplete` (wx main thread) can retry synchronously.
                _record_diagnostic_load_event(
                    state,
                    trigger="on_lifecycle_event",
                    decision="deferred",
                    reason="permission_denied_worker_deferred",
                    file_source="lifecycle_explicit",
                    file_path=raw_candidate,
                    compute_sha=False,
                )
                return ("deferred_permission", None)
            return ("missing", None)
        except OSError:
            return ("missing", None)

    if already_staged_by_pipeline:
        effective_explicit = explicit
    else:
        staged_copy = _stage_gcode_file_to_storage(
            explicit,
            state,
            output_name=eff_output_name,
            plate_index=plate_idx,
            host_name=host_name,
            project_model=project_model,
            source_kind="slicing_pipeline" if explicit == cur_saved else "slicer_explicit",
            pre_bytes=prefetched_bytes,
            allow_overwrite_in_staged=True,
        )
        if not staged_copy:
            _record_diagnostic_load_event(
                state,
                trigger="on_lifecycle_event",
                decision="error",
                reason="explicit_staging_failed",
                file_source="lifecycle_explicit",
                file_path=explicit,
                compute_sha=False,
            )
            return ("staging_failed", None)
        effective_explicit = staged_copy

    source_kind, resolved_path, info = acquire_gcode_from_slicer(
        state,
        explicit_path=effective_explicit,
        include_last_gcode_path=False,
        force_refresh=True,
        output_name=ctx_output_name or derived_output_name,
        plate_index=plate_idx,
        explicit_flag=True,
    )
    if not resolved_path or info is None:
        return ("missing", None)
    try:
        raw_real = os.path.realpath(raw_candidate) if raw_candidate else ""
        res_real = os.path.realpath(resolved_path) if resolved_path else ""
        is_preview_src = bool(
            raw_candidate
            and raw_real != res_real
            and _is_preview_temp_source(raw_candidate, storage_dir)
            and os.path.isfile(raw_candidate)
        )
    except OSError:
        is_preview_src = False
    with state._lock:
        state._auto_slice_in_progress = False
        state._last_invalidation_reason = ""
        if is_preview_src:
            state._last_preview_temp_path = raw_candidate
            state._last_preview_temp_existed = True
        else:
            state._last_preview_temp_path = ""
            state._last_preview_temp_existed = False
    if is_preview_src:
        _record_slice_host_fingerprint(state)
    return ("loaded", (source_kind, resolved_path, info))


def _record_slicing_completed_for_staged(
    state: PluginState,
    staged_path: str,
    plate_label: str = "",
    plate_index: Optional[int] = None,
    ui_snapshot: Optional[Dict[str, Any]] = None,
) -> None:
    """Record SlicingJobComplete while correlating it to the staged file just produced."""
    sc = _read_provenance_sidecar(staged_path) if staged_path else {}
    state.record_slicing_completed(
        plate_label=plate_label,
        plate_index=plate_index,
        ui_snapshot=ui_snapshot,
        satisfied_run_id=str(sc.get("slicing_run_id") or "") if sc else "",
        satisfied_created_ns=int(sc.get("created_ns", 0) or 0) if sc else 0,
    )


class KobraS1AcePageCapability(orca.pages.PagesPluginCapabilityBase):
    """Interactive HTML Control & Color Mapping UI Tab inside OrcaSlicer 2.5.x."""

    DRYER_START_REFRESH_DELAYS_SEC = (0.5,)
    DRYER_STOP_REFRESH_DELAYS_SEC = (0.5, 1.5)
    UPLOAD_REFRESH_DELAYS_SEC = (0.5, 1.5)

    def get_name(self) -> str:
        return PLUGIN_TAB_NAME

    def on_load(self) -> None:
        global _ACTIVE_PAGE_CAPABILITY
        with _ACTIVE_PAGE_LOCK:
            _ACTIVE_PAGE_CAPABILITY = self
        GLOBAL_STATE.cache_orca_storage_dir()
        GLOBAL_STATE.refresh_host_ui_snapshot()
        GLOBAL_STATE.load_config(cap=self)
        _schedule_pipeline_registration(GLOBAL_STATE, use_thread=True)

    def on_unload(self) -> None:
        global _ACTIVE_PAGE_CAPABILITY
        GLOBAL_STATE.flush_user_backup()
        with _ACTIVE_PAGE_LOCK:
            if _ACTIVE_PAGE_CAPABILITY is self:
                _ACTIVE_PAGE_CAPABILITY = None

    def _consume_pending_preview_on_complete(
        self,
        ctx_id: str,
        ctx_name: str,
        ctx_plate_idx: Optional[int],
        ui_snap: Dict[str, Any],
        resolved_project_model: str,
    ) -> Optional[Tuple[bool, Optional[Tuple[str, str, Optional[Dict[str, Any]]]]]]:
        """
        If `SlicingJobComplete` fired without a file path, consume any pending preview G-code
        captured during the preceding `GCodeExportFinished`. Returns `None` if no valid pending
        entry existed, or `(already_handled, loaded_payload)`.
        """
        with GLOBAL_STATE._pending_preview_lock:
            with GLOBAL_STATE._lock:
                pending = dict(getattr(GLOBAL_STATE, "_pending_preview_gcode", None) or {})
                cleared_at_ns = int(getattr(GLOBAL_STATE, "_cleared_at_ns", 0) or 0)
            cap_ns = int(pending.get("captured_ns", 0) or 0)
            p_id = str(pending.get("model_id") or "")
            pending_valid = (
                bool(pending.get("path"))
                and cap_ns > cleared_at_ns
                and (time.time_ns() - cap_ns) < 120_000_000_000
                and (not ctx_id or not p_id or p_id == ctx_id)
            )
            if not pending_valid:
                return None
            if pending.get("staged_path"):
                staged_pending = str(pending.get("staged_path") or "")
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE._pending_preview_gcode = {}
                _record_slicing_completed_for_staged(
                    GLOBAL_STATE,
                    staged_pending,
                    plate_label=ctx_name,
                    plate_index=ctx_plate_idx,
                    ui_snapshot=ui_snap,
                )
                return (True, None)

            status, payload = _capture_lifecycle_explicit_unlocked(
                GLOBAL_STATE,
                str(pending["path"]),
                str(pending.get("raw_output_name") or ""),
                str(pending.get("output_name") or ""),
                pending.get("plate_idx"),
                str(pending.get("host") or ""),
                str(pending.get("project_model") or resolved_project_model),
                deferred_on_perm_error=False,
            )
            with GLOBAL_STATE._lock:
                GLOBAL_STATE._pending_preview_gcode = {}
            if status == "loaded" and payload is not None:
                _record_slicing_completed_for_staged(
                    GLOBAL_STATE,
                    str(payload[1] or ""),
                    plate_label=ctx_name,
                    plate_index=ctx_plate_idx,
                    ui_snapshot=ui_snap,
                )
                return (False, payload)
        return None

    def _capture_lifecycle_worker(
        self,
        *,
        raw_candidate: str,
        capture_ns: Optional[int],
        ctx_output_name: str,
        derived_output_name: str,
        ctx_plate_idx: Optional[int],
        ctx_host_name: str,
        ctx_name: str,
        resolved_project_model: str,
        ui_snap: Dict[str, Any],
        is_slicing_complete_evt: bool,
    ) -> None:
        with _WorkerThreadScope():
            try:
                if raw_candidate:
                    loaded_payload: Optional[Tuple[str, str, Optional[Dict[str, Any]]]] = None
                    cap_status = "missing"
                    with GLOBAL_STATE._pending_preview_lock:
                        if capture_ns is not None:
                            with GLOBAL_STATE._lock:
                                cleared_ns = int(getattr(GLOBAL_STATE, "_cleared_at_ns", 0) or 0)
                                pending_cur = dict(getattr(GLOBAL_STATE, "_pending_preview_gcode", None) or {})
                            if (
                                capture_ns <= cleared_ns
                                or not pending_cur
                                or int(pending_cur.get("captured_ns", 0) or 0) != capture_ns
                                or pending_cur.get("staged_path")
                            ):
                                return
                        cap_status, loaded_payload = _capture_lifecycle_explicit_unlocked(
                            GLOBAL_STATE,
                            raw_candidate,
                            ctx_output_name,
                            derived_output_name,
                            ctx_plate_idx,
                            ctx_host_name,
                            resolved_project_model,
                            deferred_on_perm_error=not is_slicing_complete_evt,
                        )
                        if cap_status == "loaded" and loaded_payload is not None and capture_ns is not None:
                            with GLOBAL_STATE._lock:
                                pending_now = getattr(GLOBAL_STATE, "_pending_preview_gcode", None)
                                if (
                                    isinstance(pending_now, dict)
                                    and int(pending_now.get("captured_ns", 0) or 0) == capture_ns
                                ):
                                    pending_now["staged_path"] = loaded_payload[1]

                    if cap_status in ("already_handled", "deferred_permission"):
                        return
                    if cap_status == "loaded" and loaded_payload is not None:
                        if is_slicing_complete_evt:
                            _record_slicing_completed_for_staged(
                                GLOBAL_STATE,
                                str(loaded_payload[1] or ""),
                                plate_label=ctx_name,
                                plate_index=ctx_plate_idx,
                                ui_snapshot=ui_snap,
                            )
                        _trigger_auto_preprint_popup(
                            self,
                            loaded_payload[0],
                            loaded_payload[1],
                            loaded_payload[2],
                            use_thread=False,
                        )
                        return

                    _record_diagnostic_load_event(
                        GLOBAL_STATE,
                        trigger="on_lifecycle_event",
                        decision="error",
                        reason="explicit_staging_failed"
                        if cap_status == "staging_failed"
                        else "explicit_path_missing_or_unreadable",
                        file_source="lifecycle_explicit",
                        file_path=raw_candidate,
                        compute_sha=False,
                    )
                    _clear_active_job_fields(GLOBAL_STATE)
                    GLOBAL_STATE.save_config()
                    sync_filaments_from_orca_host(GLOBAL_STATE, preserve_gcode_usage=False)
                    _trigger_auto_preprint_popup(self, "presets_only", "", None, use_thread=False)
                    return

                # Non-file lifecycle notification (e.g. standalone SlicingJobComplete "Plate 1"):
                # NEVER fall back to discover_sliced_gcode_candidates() or open an old staged file.
                if is_slicing_complete_evt:
                    with GLOBAL_STATE._lock:
                        pp_recent = bool(getattr(GLOBAL_STATE, "_pipeline_active_since_slice", False))
                        last_pp_ns = int(getattr(GLOBAL_STATE, "_last_pipeline_export_ns", 0) or 0)
                        if pp_recent:
                            GLOBAL_STATE._pipeline_active_since_slice = False
                    if not (pp_recent and last_pp_ns > 0 and (time.time_ns() - last_pp_ns) < 5_000_000_000):
                        GLOBAL_STATE.record_slicing_completed(
                            plate_label=ctx_name,
                            plate_index=ctx_plate_idx,
                            ui_snapshot=ui_snap,
                        )
                with GLOBAL_STATE._lock:
                    cur_active_path = str(GLOBAL_STATE.last_gcode_path or "").strip()
                    cur_active_src = str(GLOBAL_STATE.last_gcode_source or "").strip()
                has_active = False
                if cur_active_path:
                    ok_act, rsn_act = _validate_restorable_gcode_state(
                        GLOBAL_STATE,
                        cur_active_path,
                        source_hint=cur_active_src,
                    )
                    if ok_act:
                        has_active = True
                    else:
                        _record_diagnostic_load_event(
                            GLOBAL_STATE,
                            trigger="on_lifecycle_event",
                            decision="rejected",
                            reason=rsn_act,
                            file_source=cur_active_src or "existing",
                            file_path=cur_active_path,
                            compute_sha=False,
                        )
                        _clear_active_job_fields(GLOBAL_STATE)
                        GLOBAL_STATE.save_config()
                if not has_active:
                    sync_filaments_from_orca_host(GLOBAL_STATE, preserve_gcode_usage=False)
                _trigger_auto_preprint_popup(self, "presets_only", "", None, use_thread=False)
            except Exception:
                pass

    def on_lifecycle_event(self, event: Any, ctx: Any) -> None:
        """
        Observe OrcaSlicer preset/project and slice/export lifecycle events non-blockingly.
        - Refreshes cached storage & UI snapshots strictly on the UI/Main callback thread.
        - Auto-registers Kobra S1 slicing pipeline capability via `begin_update` off UI thread.
        - Captures preview/export G-code paths delivered via `ctx.gcode_path`,
          `GCodeExportFinished.ctx.msg` (`.../Metadata/.<pid>.<idx>.gcode`), or `ctx.name`,
          without ever scanning `/tmp` or reloading previously cleared/old staged files.
        """
        GLOBAL_STATE.cache_orca_storage_dir()
        ui_snap = GLOBAL_STATE.refresh_host_ui_snapshot()
        if _handle_pipeline_lifecycle_event(event, ctx, GLOBAL_STATE, use_thread=True):
            return
        evt_enum = getattr(orca, "LifecycleEvent", None)
        err_enum = getattr(orca, "LifecycleEvtCode", None)
        if evt_enum is None:
            return
        slice_started_evt = getattr(evt_enum, "SliceStarted", None)
        invalidating_evts = tuple(
            getattr(evt_enum, nm)
            for nm in (
                "PlateChanged",
                "PlateSelected",
                "ModelModified",
                "ProjectModified",
                "ProjectLoaded",
                "ObjectsChanged",
                "SliceInvalidated",
                "SlicingInvalidated",
            )
            if getattr(evt_enum, nm, None) is not None
        )
        is_started = slice_started_evt is not None and event == slice_started_evt
        reslice_prev_path = ""
        reslice_prev_mtime = 0
        if is_started:
            with GLOBAL_STATE._lock:
                if str(GLOBAL_STATE.last_gcode_source or "") != "file_picker":
                    reslice_prev_path = str(getattr(GLOBAL_STATE, "_last_preview_temp_path", "") or "").strip()
                    if not reslice_prev_path and isinstance(GLOBAL_STATE._last_staged_provenance, dict):
                        reslice_prev_path = str(GLOBAL_STATE._last_staged_provenance.get("preview_temp_path") or "").strip()
            if reslice_prev_path and os.path.isfile(reslice_prev_path):
                reslice_prev_mtime = _get_file_mtime_ns(reslice_prev_path)
        if is_started or (invalidating_evts and event in invalidating_evts):
            # OrcaSlicer discarded/restarted the slice: the old temp .gcode may still exist under
            # the same name, so invalidate by event rather than by file presence.
            if check_and_invalidate_missing_preview_slice(
                GLOBAL_STATE,
                force_reason="slice_started" if is_started else "host_slice_invalidated_event",
            ):
                with GLOBAL_STATE._lock:
                    inv_lang = GLOBAL_STATE.language
                self.post_message(
                    {
                        "state": GLOBAL_STATE.to_dict(),
                        "action": "close_preprint_modal",
                        "action_reason": "slice_invalidated",
                        "log": t("host.slice_invalidated", lang=inv_lang),
                    }
                )
        if is_started:
            with GLOBAL_STATE._lock:
                GLOBAL_STATE._pipeline_active_since_slice = False
                GLOBAL_STATE._pending_preview_gcode = {}
                GLOBAL_STATE._last_slice_satisfied_run_id = ""
                GLOBAL_STATE._last_slice_satisfied_created_ns = 0
                GLOBAL_STATE._reslice_preview_temp_path = reslice_prev_path
                GLOBAL_STATE._reslice_preview_prev_mtime_ns = reslice_prev_mtime
            _schedule_pipeline_registration(GLOBAL_STATE, use_thread=True)
            return

        watched = tuple(
            getattr(evt_enum, name, None)
            for name in ("GCodeExportFinished", "SlicingJobComplete")
            if getattr(evt_enum, name, None) is not None
        )
        if event not in watched:
            return

        is_slicing_complete_evt = event == getattr(evt_enum, "SlicingJobComplete", None)
        ctx_code = getattr(ctx, "code", None)
        ctx_msg = str(getattr(ctx, "msg", "") or "").strip()
        err_val = getattr(err_enum, "Error", object()) if err_enum is not None else object()
        warn_val = getattr(err_enum, "Warn", object()) if err_enum is not None else object()
        if ctx_code in (err_val, warn_val) or (is_slicing_complete_evt and ctx_msg.lower() == "cancelled"):
            with GLOBAL_STATE._lock:
                GLOBAL_STATE._pending_preview_gcode = {}
            return

        ctx_id = str(getattr(ctx, "id", "") or "").strip()
        ctx_name = str(getattr(ctx, "name", "") or "").strip()
        ctx_gcode_path = str(getattr(ctx, "gcode_path", "") or "").strip()
        ctx_output_name = str(getattr(ctx, "output_name", "") or "").strip()
        ctx_plate_idx = _normalize_plate_index(getattr(ctx, "plate_index", getattr(ctx, "plate", None)))
        if ctx_plate_idx is None:
            ctx_plate_idx = _normalize_plate_index(((ui_snap.get("active_model") or {}).get("plate_index")))
        ctx_host_name = str(getattr(ctx, "host", "") or "").strip()
        resolved_project_model = _resolve_lifecycle_project_model(ctx_name, ui_snap)

        raw_candidate = (
            ctx_gcode_path
            or (ctx_msg if _is_gcode_or_container_filename(ctx_msg) else "")
            or (ctx_name if _is_gcode_or_container_filename(ctx_name) else "")
        )
        if raw_candidate and raw_candidate.lower().endswith(".tmp"):
            raw_candidate = ""

        if is_slicing_complete_evt:
            _schedule_pipeline_registration(GLOBAL_STATE, use_thread=True)
        if is_slicing_complete_evt and not raw_candidate:
            consumed = self._consume_pending_preview_on_complete(
                ctx_id, ctx_name, ctx_plate_idx, ui_snap, resolved_project_model
            )
            if consumed is not None:
                already_handled, pending_payload = consumed
                if already_handled:
                    return
                if pending_payload is not None:
                    _trigger_auto_preprint_popup(self, *pending_payload, use_thread=True)
                    return
            refreshed_prev = _find_updated_preview_temp_source(GLOBAL_STATE, ui_snap=ui_snap)
            if refreshed_prev:
                raw_candidate = refreshed_prev
            with GLOBAL_STATE._lock:
                GLOBAL_STATE._reslice_preview_temp_path = ""
                GLOBAL_STATE._reslice_preview_prev_mtime_ns = 0

        capture_ns: Optional[int] = None
        derived_output_name = ""
        if raw_candidate:
            derived_output_name = _derive_lifecycle_output_name(
                raw_candidate,
                ctx_name=ctx_name,
                ctx_output_name=ctx_output_name,
                ui_snap=ui_snap,
            )
            if not is_slicing_complete_evt:
                with GLOBAL_STATE._lock:
                    capture_ns = max(
                        time.time_ns(),
                        int(getattr(GLOBAL_STATE, "_cleared_at_ns", 0) or 0) + 1,
                    )
                    GLOBAL_STATE._pending_preview_gcode = {
                        "path": raw_candidate,
                        "model_id": ctx_id,
                        "model_name": ctx_name,
                        "raw_output_name": ctx_output_name,
                        "output_name": derived_output_name,
                        "plate_idx": ctx_plate_idx,
                        "host": ctx_host_name,
                        "project_model": resolved_project_model,
                        "captured_ns": capture_ns,
                        "staged_path": "",
                    }

        threading.Thread(
            target=self._capture_lifecycle_worker,
            kwargs={
                "raw_candidate": raw_candidate,
                "capture_ns": capture_ns,
                "ctx_output_name": ctx_output_name,
                "derived_output_name": derived_output_name,
                "ctx_plate_idx": ctx_plate_idx,
                "ctx_host_name": ctx_host_name,
                "ctx_name": ctx_name,
                "resolved_project_model": resolved_project_model,
                "ui_snap": ui_snap,
                "is_slicing_complete_evt": is_slicing_complete_evt,
            },
            daemon=True,
        ).start()

    def get_ui(self) -> str:
        return ACE_PLUGIN_HTML_UI

    def on_message(self, data: Any) -> None:
        global _ACTIVE_PAGE_CAPABILITY
        if not isinstance(data, dict):
            return
        with _ACTIVE_PAGE_LOCK:
            if _ACTIVE_PAGE_CAPABILITY is None:
                _ACTIVE_PAGE_CAPABILITY = self
        GLOBAL_STATE.cache_orca_storage_dir()
        GLOBAL_STATE.refresh_host_ui_snapshot()

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

        if msg_type == "set_appearance":
            tm, cs = GLOBAL_STATE.set_appearance(
                theme_mode=data.get("theme_mode"),
                color_scheme=data.get("color_scheme"),
                save=True,
            )
            self.post_message(
                {
                    "state": GLOBAL_STATE.to_dict(),
                    "log": t("host.appearance_saved", lang=GLOBAL_STATE.language, theme=tm, scheme=cs),
                }
            )
            return

        if "settings" in data and isinstance(data["settings"], dict):
            prev_mr = str(GLOBAL_STATE.moonraker_url or "")
            prev_sm = str(GLOBAL_STATE.spoolman_url or "")
            GLOBAL_STATE.apply_dict(data["settings"], apply_gcode_state=False)
            if msg_type == "update_mapping":
                GLOBAL_STATE.record_session_manual_mapping()
            new_mr = str(GLOBAL_STATE.moonraker_url or "")
            new_sm = str(GLOBAL_STATE.spoolman_url or "")
            if new_mr and new_mr != prev_mr:
                _request_optional_http_permission(new_mr, include_port80=True)
            if new_sm and new_sm != prev_sm:
                _request_optional_http_permission(new_sm, include_port80=False)
        elif msg_type in ("test_spoolman", "refresh_spoolman") and data.get("spoolman_url"):
            _request_optional_http_permission(data.get("spoolman_url"), include_port80=False)
        elif msg_type == "switch_printer":
            pid = str(data.get("profile_id") or "").strip()
            switched = GLOBAL_STATE.switch_printer_profile(pid, save=True)
            if switched:
                if switched.get("moonraker_url"):
                    _request_optional_http_permission(switched.get("moonraker_url"), include_port80=True)
                if switched.get("spoolman_url"):
                    _request_optional_http_permission(switched.get("spoolman_url"), include_port80=False)
        elif msg_type == "save_printer_profile":
            mr_in = data.get("moonraker_url")
            sm_in = data.get("spoolman_url")
            if mr_in:
                _request_optional_http_permission(mr_in, include_port80=True)
            if sm_in:
                _request_optional_http_permission(sm_in, include_port80=False)

        if msg_type == "save_settings":
            self.post_message(
                {
                    "state": GLOBAL_STATE.to_dict(),
                    "log": t("host.settings_saved", lang=GLOBAL_STATE.language, url=GLOBAL_STATE.moonraker_url),
                }
            )
            return

        payload_copy = dict(data)
        if msg_type == "acquire_slicer_gcode" and not _is_worker_thread_context():
            with GLOBAL_STATE._lock:
                cur_g_path = str(GLOBAL_STATE.last_gcode_path or "").strip()
                cur_g_src = str(GLOBAL_STATE.last_gcode_source or "").strip()
                auto_busy = bool(getattr(GLOBAL_STATE, "_auto_slice_in_progress", False))
            if cur_g_path and cur_g_src != "file_picker" and not auto_busy:
                trig = _try_trigger_host_slice(GLOBAL_STATE)
                trig_status = str(trig.get("status") or "unavailable")
                payload_copy["_auto_slice_status"] = trig_status
                if trig_status == "started":
                    with GLOBAL_STATE._lock:
                        GLOBAL_STATE._auto_slice_in_progress = True
                    check_and_invalidate_missing_preview_slice(
                        GLOBAL_STATE,
                        force_reason="auto_reslice_started",
                    )

        worker = threading.Thread(
            target=self._handle_async_message,
            args=(msg_type, payload_copy),
            daemon=True,
        )
        worker.start()

    def _handle_async_message(self, msg_type: str, data: Dict[str, Any]) -> None:
        with _WorkerThreadScope():
            self._run_async_message_impl(msg_type, data)

    def _run_with_hw_lock(
        self,
        fn: Any,
        *,
        busy_key: str = "host.hw_busy",
        busy_modal: bool = False,
    ) -> Tuple[bool, Any]:
        """Acquire `_hw_action_lock` non-blockingly and run `fn()`; post busy notice if locked."""
        if not GLOBAL_STATE._hw_action_lock.acquire(blocking=False):
            busy_msg = t(busy_key, lang=GLOBAL_STATE.language)
            payload: Dict[str, Any] = {"log": busy_msg}
            if busy_modal:
                payload["modal_feedback"] = {"level": "warn", "text": busy_msg}
            self.post_message(payload)
            return False, None
        try:
            return True, fn()
        finally:
            GLOBAL_STATE._hw_action_lock.release()

    def _apply_mapping_and_revalidate(
        self,
        *,
        new_map: Optional[Dict[int, int]] = None,
        source_tag: Optional[str] = None,
        reset_all: bool = False,
        ace_slots: Optional[List[Dict[str, Any]]] = None,
        reset_mismatch_confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Shared mapping update + validation + flush remap + save_config path for
        `update_mapping`, `auto_match`, and `reset_mapping`.
        """
        with GLOBAL_STATE._lock:
            if reset_all:
                GLOBAL_STATE.tool_map = {i: i for i in range(MAX_ACE_SLOTS)}
                GLOBAL_STATE.tool_map_sources = {i: "default" for i in range(MAX_ACE_SLOTS)}
            elif new_map is not None:
                GLOBAL_STATE.tool_map.update(new_map)
                if source_tag:
                    for tool_id in new_map:
                        GLOBAL_STATE.tool_map_sources[int(tool_id)] = source_tag
            if reset_mismatch_confirm:
                GLOBAL_STATE.material_mismatch_confirmed = False
            slicer_fils = list(GLOBAL_STATE.slicer_filaments)
            slots = (
                list(ace_slots)
                if ace_slots is not None
                else list((GLOBAL_STATE.last_ace_status or {}).get("slots", []))
            )
            cur_lang = GLOBAL_STATE.language
            tmap = dict(GLOBAL_STATE.tool_map)
            ace_st_snap = dict(GLOBAL_STATE.last_ace_status or {})
            if is_ace_bypass_active(GLOBAL_STATE, ace_status=ace_st_snap):
                validation = validate_color_mapping([], tmap, [], lang=cur_lang)
                GLOBAL_STATE.mapping_warnings = []
            elif slots and slicer_fils:
                validation = validate_color_mapping(slicer_fils, tmap, slots, lang=cur_lang)
                GLOBAL_STATE.mapping_warnings = list(validation["errors"] + validation["warnings"])
            else:
                validation = validate_color_mapping(slicer_fils, tmap, [], lang=cur_lang)
                GLOBAL_STATE.mapping_warnings = []
            if GLOBAL_STATE.last_postprocess_info:
                remapped = remap_flush_info(GLOBAL_STATE.last_postprocess_info, tmap)
                remapped["mapping_validation"] = validation
                GLOBAL_STATE.last_postprocess_info = remapped
        GLOBAL_STATE.save_config()
        return validation

    def _post_with_diag(self, payload: Dict[str, Any]) -> None:
        """Attach the latest diagnostic load log line (when enabled) and post to UI."""
        if _is_diag_load_logging_enabled(GLOBAL_STATE):
            diag_evts = get_diagnostic_load_events(GLOBAL_STATE, limit=1)
            if diag_evts:
                payload["diag_log"] = _format_diag_log_line(diag_evts[-1])
        self.post_message(payload)

    def _msg_ready(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        GLOBAL_STATE.load_config()
        sync_filaments_from_orca_host(GLOBAL_STATE, preserve_gcode_usage=True)
        with GLOBAL_STATE._lock:
            was_pending = bool(GLOBAL_STATE.pending_preprint_open)
            cur_gcode = str(GLOBAL_STATE.last_gcode_path or "").strip()
            cur_src = str(GLOBAL_STATE.last_gcode_source or "").strip()
            cur_prov = dict(GLOBAL_STATE._last_staged_provenance) if isinstance(GLOBAL_STATE._last_staged_provenance, dict) else {}
            has_info = bool(GLOBAL_STATE.last_postprocess_info)
        if cur_gcode:
            ok_ready, rsn_ready = _validate_restorable_gcode_state(
                GLOBAL_STATE,
                cur_gcode,
                provenance=cur_prov or None,
                source_hint=cur_src,
            )
            if not ok_ready:
                _clear_active_job_fields(GLOBAL_STATE)
                GLOBAL_STATE.save_config()
                _record_diagnostic_load_event(
                    GLOBAL_STATE,
                    trigger="ready",
                    decision="rejected",
                    reason=rsn_ready,
                    file_source=cur_src or "config_restore",
                    file_path=cur_gcode,
                    sidecar=cur_prov or None,
                    compute_sha=False,
                )
                cur_gcode = ""
                was_pending = False
                has_info = False
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

    def _msg_preprint_modal_opened(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        with GLOBAL_STATE._lock:
            GLOBAL_STATE.pending_preprint_open = False

    def _msg_acquire_slicer_gcode(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        log_msg = ""
        modal_fb: Dict[str, Any] = {"level": "warn", "text": ""}
        open_on_ready = bool(data.get("open_preprint_on_ready", False))
        extra_payload: Dict[str, Any] = {}
        try:
            auto_slice_st = str(data.get("_auto_slice_status") or "").strip().lower()
            if auto_slice_st == "started":
                info_notice = t("ui.notice.slicing_in_progress", lang=GLOBAL_STATE.language)
                log_msg = info_notice
                modal_fb = {"level": "info", "text": info_notice}
                extra_payload["action"] = "slicing_in_progress"
                extra_payload["notice"] = {"level": "info", "text": info_notice}
                return

            invalidated_now = check_and_invalidate_missing_preview_slice(GLOBAL_STATE)
            raw_path = str(data.get("gcode_path") or "").strip()
            force_refresh = bool(data.get("force_refresh", False))
            explicit_flag = bool(data.get("explicit", False))
            output_name_arg = str(data.get("output_name") or "").strip()
            plate_idx_arg = _normalize_plate_index(data.get("plate_index"))
            storage_dir = GLOBAL_STATE.resolve_storage_dir()
            with GLOBAL_STATE._lock:
                saved_path = str(GLOBAL_STATE.last_gcode_path or "").strip()

            explicit = raw_path if (raw_path and (raw_path != saved_path or explicit_flag)) else ""
            if explicit and HAS_ORCA_RUNTIME and _is_system_temp_path(explicit, storage_dir) and not explicit_flag:
                _record_diagnostic_load_event(
                    GLOBAL_STATE,
                    trigger="acquire_slicer_gcode",
                    decision="rejected",
                    reason="rejected:sys_temp_unexplicit",
                    file_source="ui_payload",
                    file_path=explicit,
                    compute_sha=False,
                )
                explicit = ""
            if explicit and saved_path and not explicit_flag:
                try:
                    if os.path.realpath(explicit) == os.path.realpath(saved_path):
                        explicit = ""
                except OSError:
                    if explicit == saved_path:
                        explicit = ""
            # Any UI-echoed path (without explicit=True) that was cleared, superseded, stale,
            # pre-clear, or mismatched must NEVER bypass acquire_gcode_from_slicer() guards.
            if explicit and not explicit_flag and os.path.isfile(explicit):
                ok_exp, rsn_exp, sc_exp = _evaluate_staged_candidate(explicit, GLOBAL_STATE)
                if not ok_exp:
                    _record_diagnostic_load_event(
                        GLOBAL_STATE,
                        trigger="acquire_slicer_gcode",
                        decision="rejected",
                        reason=rsn_exp,
                        file_source="ui_echoed",
                        file_path=explicit,
                        sidecar=sc_exp,
                        compute_sha=False,
                    )
                    explicit = ""

            _kind = "presets_only"
            resolved_path = ""
            info = None
            if not explicit and not explicit_flag:
                refreshed_prev = _find_updated_preview_temp_source(
                    GLOBAL_STATE,
                    allow_from_file_picker=force_refresh,
                )
                if refreshed_prev:
                    ui_snap_acq = GLOBAL_STATE.get_host_ui_snapshot()
                    derived_acq = _derive_lifecycle_output_name(
                        refreshed_prev,
                        ctx_name="",
                        ctx_output_name=output_name_arg,
                        ui_snap=ui_snap_acq,
                    )
                    proj_model_acq = _resolve_lifecycle_project_model("", ui_snap_acq)
                    with GLOBAL_STATE._pending_preview_lock:
                        cap_st, cap_payload = _capture_lifecycle_explicit_unlocked(
                            GLOBAL_STATE,
                            refreshed_prev,
                            output_name_arg,
                            derived_acq,
                            plate_idx_arg,
                            "",
                            proj_model_acq,
                            deferred_on_perm_error=False,
                        )
                    if cap_st == "loaded" and cap_payload is not None:
                        _kind, resolved_path, info = cap_payload
                        with GLOBAL_STATE._lock:
                            had_slice_ns = int(getattr(GLOBAL_STATE, "_last_slice_completed_ns", 0) or 0)
                        if had_slice_ns > 0:
                            _record_slicing_completed_for_staged(
                                GLOBAL_STATE,
                                resolved_path,
                                plate_index=plate_idx_arg,
                                ui_snapshot=ui_snap_acq,
                            )

            if not resolved_path or info is None:
                _kind, resolved_path, info = acquire_gcode_from_slicer(
                    GLOBAL_STATE,
                    explicit_path=explicit,
                    include_last_gcode_path=True,
                    force_refresh=force_refresh,
                    output_name=output_name_arg,
                    plate_index=plate_idx_arg,
                    explicit_flag=explicit_flag,
                )
            if resolved_path and info is not None:
                log_msg = t(
                    "host.slicer_gcode_loaded",
                    lang=GLOBAL_STATE.language,
                    file=os.path.basename(resolved_path),
                    tc=info.get("toolchanges", 0),
                )
                modal_fb = {
                    "level": "ok",
                    "text": t("ui.modal.feedback.ready_short", lang=GLOBAL_STATE.language),
                }
                if open_on_ready:
                    extra_payload["action"] = "open_preprint_modal"
                    extra_payload["source_file"] = os.path.basename(resolved_path)
            else:
                with GLOBAL_STATE._lock:
                    inv_reason = str(getattr(GLOBAL_STATE, "_last_invalidation_reason", "") or "")
                    cur_lang_acq = GLOBAL_STATE.language
                notice_txt = _build_invalidation_notice_text(inv_reason, cur_lang_acq)
                log_msg = notice_txt if (open_on_ready or invalidated_now or inv_reason) else t("host.slicer_gcode_not_found", lang=cur_lang_acq)
                modal_fb = {"level": "warn", "text": log_msg}
                if open_on_ready or invalidated_now or (bool(data.get("auto_tab_sync")) and inv_reason):
                    extra_payload["action"] = "no_gcode_notice"
                    extra_payload["notice"] = {"level": "warn", "text": notice_txt}

            if (
                bool(data.get("refresh_live_status", False))
                or open_on_ready
                or bool(data.get("auto_tab_sync", False))
            ):
                live_res = self._refresh_live_ace_state(
                    client, log_msg or t("host.status_updated", lang=GLOBAL_STATE.language)
                )
                if isinstance(live_res, dict) and live_res.get("ace_status") is not None:
                    extra_payload["ace_status"] = live_res["ace_status"]
        except Exception as exc:
            log_msg = t(
                "host.msg_error",
                lang=GLOBAL_STATE.language,
                msg_type="acquire_slicer_gcode",
                error=exc,
            )
            modal_fb = {"level": "error", "text": log_msg}
            if open_on_ready:
                extra_payload["action"] = "no_gcode_notice"
                extra_payload["notice"] = {"level": "error", "text": log_msg}
        finally:
            payload_out: Dict[str, Any] = {
                "state": GLOBAL_STATE.to_dict(),
                "log": log_msg,
                "modal_feedback": modal_fb,
            }
            payload_out.update(extra_payload)
            self._post_with_diag(payload_out)

    def _msg_load_gcode_content(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        fname = str(data.get("filename") or "selected.gcode")
        encoding = str(data.get("encoding") or "auto")
        content_payload = (
            data.get("content_base64")
            if data.get("content_base64") is not None
            else data.get("content", "")
        )
        plate_idx = _normalize_plate_index(data.get("plate_index"))
        try:
            staged_path, info = load_gcode_from_content(
                fname,
                content_payload,
                GLOBAL_STATE,
                encoding=encoding,
                plate_index=plate_idx,
            )
            log_msg = t(
                "host.gcode_browsed_loaded",
                lang=GLOBAL_STATE.language,
                file=os.path.basename(staged_path),
                tc=info.get("toolchanges", 0),
            )
            modal_fb = {
                "level": "ok",
                "text": t("ui.modal.feedback.ready_short", lang=GLOBAL_STATE.language),
            }
        except Exception as exc:
            log_msg = t("host.gcode_read_error", lang=GLOBAL_STATE.language, error=exc)
            modal_fb = {"level": "error", "text": log_msg}
        self.post_message({
            "state": GLOBAL_STATE.to_dict(),
            "log": log_msg,
            "modal_feedback": modal_fb,
        })

    def _msg_sync_orca_filaments(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        with GLOBAL_STATE._lock:
            has_active_gcode = bool(
                GLOBAL_STATE.last_gcode_path
                and os.path.isfile(GLOBAL_STATE.last_gcode_path)
                and GLOBAL_STATE.slicer_filaments
            )
        GLOBAL_STATE.clear_session_manual_mapping()
        fils = sync_filaments_from_orca_host(GLOBAL_STATE, preserve_gcode_usage=has_active_gcode)
        self._apply_mapping_and_revalidate(reset_all=True, reset_mismatch_confirm=True)
        if fils:
            GLOBAL_STATE.save_config()
            msg = t("host.sync_filaments", lang=GLOBAL_STATE.language, count=len(fils))
        else:
            msg = t("host.sync_none", lang=GLOBAL_STATE.language)
        self.post_message({"state": GLOBAL_STATE.to_dict(), "log": msg})

    def _msg_get_status(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        self._send_state_and_live_status(client, t("host.status_updated", lang=GLOBAL_STATE.language))

    def _msg_switch_printer(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        pid = str(data.get("profile_id") or "").strip()
        if pid:
            GLOBAL_STATE.switch_printer_profile(pid, save=True)
        with GLOBAL_STATE._lock:
            GLOBAL_STATE._ensure_printer_profiles_unlocked()
            prof = GLOBAL_STATE._find_profile_unlocked(GLOBAL_STATE.active_printer_id) or GLOBAL_STATE.printer_profiles[0]
            prof_name = str(prof.get("name") or GLOBAL_STATE.active_printer_id)
            cur_lang = GLOBAL_STATE.language
        new_client = GLOBAL_STATE.get_client()
        self._send_state_and_live_status(new_client, t("host.printer_switched", lang=cur_lang, name=prof_name))

    def _msg_save_printer_profile(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        prof = GLOBAL_STATE.save_printer_profile(
            profile_id=str(data.get("profile_id") or "").strip() or None,
            name=str(data.get("name") or ""),
            moonraker_url=data.get("moonraker_url"),
            api_key=data.get("api_key"),
            enable_port80_fallback=data.get("enable_port80_fallback"),
            spoolman_url=data.get("spoolman_url"),
            configured_ace_count=data.get("configured_ace_count"),
            activate=bool(data.get("activate", True)),
            save=True,
        )
        msg = t("host.printer_saved", lang=GLOBAL_STATE.language, name=prof.get("name", ""))
        if bool(data.get("refresh_status", True)):
            self._send_state_and_live_status(GLOBAL_STATE.get_client(), msg)
        else:
            self.post_message({"state": GLOBAL_STATE.to_dict(), "log": msg})

    def _msg_delete_printer_profile(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        pid = str(data.get("profile_id") or GLOBAL_STATE.active_printer_id or "").strip()
        prev_active = GLOBAL_STATE.active_printer_id
        deleted, detail = GLOBAL_STATE.delete_printer_profile(pid, save=True)
        cur_lang = GLOBAL_STATE.language
        if not deleted:
            msg = t("host.printer_delete_last", lang=cur_lang) if detail == "last_profile" else t("host.msg_error", lang=cur_lang, msg_type="delete_printer_profile", error=detail)
            self.post_message({"state": GLOBAL_STATE.to_dict(), "log": msg})
            return
        msg = t("host.printer_deleted", lang=cur_lang, name=detail)
        if prev_active == pid or bool(data.get("refresh_status", True)):
            self._send_state_and_live_status(GLOBAL_STATE.get_client(), msg)
        else:
            self.post_message({"state": GLOBAL_STATE.to_dict(), "log": msg})

    def _refresh_dryer_status_sequence(
        self,
        client: MoonrakerClient,
        initial_log_msg: str,
        delays_sec: Tuple[float, ...],
        action_seq: int,
    ) -> None:
        """Query live ACE/dryer status after configured Klipper settle delays without stale overwrites."""
        elapsed = 0.0
        with GLOBAL_STATE._lock:
            cur_lang = GLOBAL_STATE.language
            expected_epoch = GLOBAL_STATE.profile_epoch
        for idx, target_delay in enumerate(delays_sec):
            delta = max(0.0, float(target_delay) - elapsed)
            if delta > 0.0:
                time.sleep(delta)
            elapsed = float(target_delay)
            with GLOBAL_STATE._lock:
                if GLOBAL_STATE._dryer_action_seq != action_seq or GLOBAL_STATE.profile_epoch != expected_epoch:
                    return
                cur_lang = GLOBAL_STATE.language
            msg = initial_log_msg if idx == 0 else t("host.status_updated", lang=cur_lang)
            self._send_state_and_live_status(client, msg)

    def _msg_dryer(self, client: MoonrakerClient, data: Dict[str, Any], *, active: bool) -> None:
        raw_unit = str(
            data.get("unit") if "unit" in data else (GLOBAL_STATE.dryer_unit or "all")
        ).strip().lower()
        valid_unit_strs = ("all",) + tuple(str(i) for i in range(MAX_ACE_UNITS))
        unit = raw_unit if raw_unit in valid_unit_strs else "all"
        with GLOBAL_STATE._lock:
            last_ace = dict(GLOBAL_STATE.last_ace_status) if isinstance(GLOBAL_STATE.last_ace_status, dict) else {}
            cfg_count = str(getattr(GLOBAL_STATE, "configured_ace_count", DEFAULT_ACE_COUNT) or DEFAULT_ACE_COUNT).strip().lower()
            eff_count = GLOBAL_STATE.resolve_effective_ace_count(last_ace)
            GLOBAL_STATE._dryer_action_seq += 1
            action_seq = GLOBAL_STATE._dryer_action_seq

        known_unit_limit = bool(last_ace.get("connected")) or cfg_count in ("1", "2", "3", "4")
        if known_unit_limit and eff_count <= 1:
            unit = "0"
            instance: Optional[int] = None
            inst_suffix = ""
            unit_label = t("ui.sec2.unit_ace1", lang=GLOBAL_STATE.language)
        elif unit == "all":
            instance = None
            inst_suffix = ""
            if eff_count > 2:
                unit_label = t("ui.sec2.unit_all_n", lang=GLOBAL_STATE.language, count=eff_count)
            else:
                unit_label = t("ui.sec2.unit_all", lang=GLOBAL_STATE.language)
        else:
            parsed_idx = int(unit)
            if known_unit_limit and parsed_idx >= eff_count:
                parsed_idx = 0
                unit = "0"
            instance = parsed_idx
            inst_suffix = f" INSTANCE={instance}"
            unit_key = f"ui.sec2.unit_ace{instance + 1}"
            unit_label = t(unit_key, lang=GLOBAL_STATE.language)

        if active:
            try:
                raw_temp = int(float(data.get("temp") if data.get("temp") is not None else GLOBAL_STATE.dryer_temp))
            except (ValueError, TypeError):
                raw_temp = 45
            temp = max(20, min(70, raw_temp))
            try:
                raw_dur = int(float(data.get("duration") if data.get("duration") is not None else GLOBAL_STATE.dryer_duration))
            except (ValueError, TypeError):
                raw_dur = 240
            duration = max(1, min(1440, raw_dur))
            with GLOBAL_STATE._lock:
                GLOBAL_STATE.dryer_unit = unit
                GLOBAL_STATE.dryer_temp = temp
                GLOBAL_STATE.dryer_duration = duration
            GLOBAL_STATE.save_config()
            cmd_str = f"ACE_START_DRYING TEMP={temp} DURATION={duration}{inst_suffix}"
            acquired, _ = self._run_with_hw_lock(
                lambda: client.start_dryer(temp_c=temp, duration_min=duration, instance=instance)
            )
            if not acquired:
                return
            log_msg = t(
                "host.dryer_started_unit",
                lang=GLOBAL_STATE.language,
                unit=unit_label,
                temp=temp,
                duration=duration,
                cmd=cmd_str,
            )
            delays = tuple(self.DRYER_START_REFRESH_DELAYS_SEC)
        else:
            with GLOBAL_STATE._lock:
                GLOBAL_STATE.dryer_unit = unit
            GLOBAL_STATE.save_config()
            cmd_str = f"ACE_STOP_DRYING{inst_suffix}"
            acquired, _ = self._run_with_hw_lock(lambda: client.stop_dryer(instance=instance))
            if not acquired:
                return
            log_msg = t(
                "host.dryer_stopped_unit",
                lang=GLOBAL_STATE.language,
                unit=unit_label,
                cmd=cmd_str,
            )
            delays = tuple(self.DRYER_STOP_REFRESH_DELAYS_SEC)
        self._refresh_dryer_status_sequence(client, log_msg, delays, action_seq)

    def _msg_set_ace_pro_enabled(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        enabled = bool(data.get("enabled", True))
        allow_printing = bool(data.get("allow_while_printing", False))
        cur_lang = GLOBAL_STATE.language

        def _do_toggle() -> None:
            if not allow_printing:
                check_print_safety(client, "ACE Pro ON/OFF", GLOBAL_STATE)
            client.set_ace_pro_enabled(enabled, allow_while_printing=allow_printing)

        acquired, _ = self._run_with_hw_lock(_do_toggle, busy_key="host.hw_busy")
        if not acquired:
            return
        with GLOBAL_STATE._lock:
            GLOBAL_STATE.external_spool_mode = False
        GLOBAL_STATE.save_config()
        log_msg = t("host.ace_pro_enabled" if enabled else "host.ace_pro_disabled", lang=cur_lang)
        self._send_state_and_live_status(client, log_msg)
        def _follow_up() -> None:
            for d in (0.8, 1.5):
                time.sleep(d)
                self._send_state_and_live_status(client, t("host.status_updated", lang=GLOBAL_STATE.language))
        threading.Thread(target=_follow_up, daemon=True).start()

    def _msg_slot_action(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        action = str(data.get("action") or "")
        tool = int(data.get("tool", 0))
        length = int(data.get("length", 50))
        unlock_override = bool(data.get("unlock_override", False))

        def _execute_slot() -> str:
            if not unlock_override:
                check_print_safety(client, f"Slot {action} T{tool}", GLOBAL_STATE)
            cur_lang = GLOBAL_STATE.language
            if action == "load":
                client.load_slot(tool, allow_while_printing=unlock_override)
                return t("host.slot_loaded", lang=cur_lang, slot=tool, cmd=f"ACE_CHANGE_TOOL TOOL={tool}")
            if action == "unload":
                client.unload_slot(tool=tool, allow_while_printing=unlock_override)
                return t("host.slot_unloaded", lang=cur_lang, slot=tool, cmd=f"ACE_SMART_UNLOAD TOOL={tool}")
            if action == "full_unload":
                client.unload_slot(tool=tool, full_unload=True, allow_while_printing=unlock_override)
                return t("host.slot_full_unloaded", lang=cur_lang, slot=tool, cmd=f"ACE_FULL_UNLOAD TOOL={tool}")
            if action == "feed_assist_on":
                client.enable_feed_assist(tool=tool, allow_while_printing=unlock_override)
                return t("host.slot_assist_on", lang=cur_lang, slot=tool, cmd=f"ACE_ENABLE_FEED_ASSIST T={tool}")
            if action == "feed_assist_off":
                client.disable_feed_assist(tool=tool, allow_while_printing=unlock_override)
                return t("host.slot_assist_off", lang=cur_lang, slot=tool, cmd=f"ACE_DISABLE_FEED_ASSIST T={tool}")
            if action == "unload_active":
                client.unload_slot(tool=None, allow_while_printing=unlock_override)
                return t("host.slot_active_unloaded", lang=cur_lang)
            if action == "feed":
                client.feed_slot(tool=tool, length_mm=length, allow_while_printing=unlock_override)
                return t("host.slot_fed", lang=cur_lang, slot=tool, length=length)
            if action == "retract":
                client.retract_slot(tool=tool, length_mm=length, allow_while_printing=unlock_override)
                return t("host.slot_retracted", lang=cur_lang, slot=tool, length=length)
            if action == "cut_tip":
                client.trigger_cut_tip()
                return t("host.cut_tip_done", lang=cur_lang)
            if action == "bed_leveling":
                client.trigger_bed_leveling(mode="BED_MESH_CALIBRATE")
                return t("host.bed_mesh_done", lang=cur_lang)
            return t("host.slot_unknown_action", lang=cur_lang, action=action)

        acquired, log_text = self._run_with_hw_lock(_execute_slot)
        if acquired:
            self._send_state_and_live_status(client, str(log_text or ""))

    def _msg_parse_gcode(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        gcode_path = str(data.get("gcode_path") or GLOBAL_STATE.last_gcode_path).strip()
        if not gcode_path or not os.path.isfile(gcode_path):
            self.post_message({"log": t("host.file_not_found", lang=GLOBAL_STATE.language, path=gcode_path)})
            return
        parsed_read = _read_validated_gcode_file(gcode_path)
        lines = parsed_read["lines"]
        slice_info = parsed_read.get("slice_info") or {}
        with GLOBAL_STATE._lock:
            GLOBAL_STATE.last_gcode_path = gcode_path
            parsed_fils = parse_slicer_filaments(lines)
            parsed_meta = parse_gcode_job_metadata(lines, gcode_path)
            _merge_slice_info_into_parsed(parsed_fils, parsed_meta, slice_info)
            was_reset = GLOBAL_STATE.update_slicer_filaments(parsed_fils)
            GLOBAL_STATE.last_job_metadata = parsed_meta
            fil_count = len(GLOBAL_STATE.slicer_filaments)
        info = analyze_gcode_flush(
            lines,
            GLOBAL_STATE,
            filepath=gcode_path,
            extra_slice_info=slice_info,
            pre_filaments=parsed_fils,
            pre_job_metadata=parsed_meta,
        )
        info["analysis_mode"] = "preview"
        with GLOBAL_STATE._lock:
            GLOBAL_STATE.last_postprocess_info = dict(info)
        GLOBAL_STATE.save_config()
        reset_note = t("host.parse_reset_note", lang=GLOBAL_STATE.language) if was_reset else ""
        self.post_message({
            "state": GLOBAL_STATE.to_dict(),
            "log": t(
                "host.parse_ok",
                lang=GLOBAL_STATE.language,
                count=fil_count,
                file=os.path.basename(gcode_path),
                note=reset_note,
            ),
        })

    def _msg_clear_gcode(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        clear_loaded_gcode_state(GLOBAL_STATE, reason="clear_gcode", sync_host=True)
        cleared_msg = t("host.gcode_cleared", lang=GLOBAL_STATE.language)
        self._post_with_diag({
            "state": GLOBAL_STATE.to_dict(),
            "log": cleared_msg,
            "modal_feedback": {"level": "info", "text": cleared_msg},
        })

    def _msg_update_mapping(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        if isinstance(data.get("tool_map"), dict):
            GLOBAL_STATE.apply_dict(
                {
                    "tool_map": data.get("tool_map"),
                    **(
                        {"tool_map_sources": data.get("tool_map_sources")}
                        if isinstance(data.get("tool_map_sources"), dict)
                        else {}
                    ),
                },
                apply_gcode_state=False,
                save=False,
            )
        GLOBAL_STATE.record_session_manual_mapping()
        self._apply_mapping_and_revalidate()
        self.post_message({"state": GLOBAL_STATE.to_dict()})

    def _msg_auto_match(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        with GLOBAL_STATE._lock:
            expected_epoch = GLOBAL_STATE.profile_epoch
        ace_status = client.query_ace_status()
        ace_status = enrich_ace_status_with_spoolman(
            ace_status, GLOBAL_STATE, force_fetch=False, expected_epoch=expected_epoch
        )
        if not GLOBAL_STATE._commit_ace_status(ace_status, expected_epoch):
            return
        GLOBAL_STATE.clear_session_manual_mapping()
        slots = ace_status.get("slots", [])
        with GLOBAL_STATE._lock:
            slicer_fils = list(GLOBAL_STATE.slicer_filaments)
            cur_lang = GLOBAL_STATE.language
        matched = auto_match_filaments_to_slots(slicer_fils, slots)
        validation = self._apply_mapping_and_revalidate(
            new_map=matched,
            source_tag="auto",
            ace_slots=slots,
            reset_mismatch_confirm=True,
        )
        warns = validation["errors"] + validation["warnings"]
        warn_suffix = t("host.automatch_warns", lang=cur_lang, warns="; ".join(warns)) if warns else ""
        self.post_message({
            "state": GLOBAL_STATE.to_dict(),
            "ace_status": ace_status,
            "log": t(
                "host.automatch_log",
                lang=cur_lang,
                matched=json.dumps(matched),
                warns=warn_suffix,
            ),
        })

    def _msg_reset_mapping(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        GLOBAL_STATE.clear_session_manual_mapping()
        self._apply_mapping_and_revalidate(reset_all=True, reset_mismatch_confirm=True)
        self.post_message({
            "state": GLOBAL_STATE.to_dict(),
            "log": t("host.reset_done", lang=GLOBAL_STATE.language),
        })

    def _msg_preview_gcode(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        gcode_path = str(data.get("gcode_path") or GLOBAL_STATE.last_gcode_path).strip()
        if not gcode_path or not os.path.isfile(gcode_path):
            self.post_message({"log": t("host.file_not_found", lang=GLOBAL_STATE.language, path=gcode_path)})
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

    def _msg_postprocess(self, client: MoonrakerClient, data: Dict[str, Any], *, upload: bool) -> None:
        if isinstance(data.get("settings"), dict):
            GLOBAL_STATE.apply_dict(data["settings"], apply_gcode_state=False, save=False)
        gcode_path = str(data.get("gcode_path") or GLOBAL_STATE.last_gcode_path).strip()
        if not gcode_path or not os.path.isfile(gcode_path):
            no_file_msg = (
                t("host.no_gcode_loaded", lang=GLOBAL_STATE.language)
                if not gcode_path
                else t("host.file_not_found", lang=GLOBAL_STATE.language, path=gcode_path)
            )
            self.post_message({
                "state": GLOBAL_STATE.to_dict(),
                "log": no_file_msg,
                "modal_feedback": {"level": "error", "text": no_file_msg},
            })
            return

        info, out_lines, parsed = _postprocess_gcode_file_impl(gcode_path, GLOBAL_STATE)
        patched_content = str(parsed.get("gcode_text") or "".join(out_lines))
        cur_lang = GLOBAL_STATE.language
        base_name = os.path.basename(gcode_path)
        purge_len = f"{info.get('total_purge_length_mm', 0.0):.2f}"
        log_msg = t(
            "host.pp_log",
            lang=cur_lang,
            file=base_name,
            status=info["flush_status"],
            tc=info["toolchanges"],
            mul=info["flush_multiplier"],
            bed=info["bedLeveling"],
            len=purge_len,
        )
        modal_fb: Dict[str, str] = {
            "level": "ok",
            "text": t("host.pp_modal_ok", lang=cur_lang, file=base_name, tc=info["toolchanges"], len=purge_len),
        }

        extra_post: Dict[str, Any] = {}
        start_print_req = False
        upload_epoch = 0
        if upload:
            with GLOBAL_STATE._lock:
                expected_epoch = GLOBAL_STATE.profile_epoch
                upload_epoch = expected_epoch
            try:
                live_status = client.query_ace_status()
                live_status = enrich_ace_status_with_spoolman(
                    live_status, GLOBAL_STATE, force_fetch=False, expected_epoch=expected_epoch
                )
                if not GLOBAL_STATE._commit_ace_status(live_status, expected_epoch):
                    abort_msg = t("host.profile_changed_abort", lang=cur_lang)
                    self.post_message({
                        "state": GLOBAL_STATE.to_dict(),
                        "log": abort_msg,
                        "modal_feedback": {"level": "error", "text": abort_msg},
                    })
                    return
            except Exception:
                with GLOBAL_STATE._lock:
                    if GLOBAL_STATE.profile_epoch != expected_epoch:
                        abort_msg = t("host.profile_changed_abort", lang=cur_lang)
                        self.post_message({
                            "state": GLOBAL_STATE.to_dict(),
                            "log": abort_msg,
                            "modal_feedback": {"level": "error", "text": abort_msg},
                        })
                        return
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
                    lang=cur_lang,
                    base=log_msg,
                    reasons="; ".join(blocking_reasons),
                )
                self.post_message({
                    "state": GLOBAL_STATE.to_dict(),
                    "log": blocked_msg,
                    "modal_feedback": {"level": "error", "text": blocked_msg},
                })
                return

            upload_name = _sanitize_staged_gcode_filename(
                str((info.get("job_metadata") or {}).get("output_name") or base_name)
            )
            is_modal_bridge = bool(getattr(self, "_is_modal_bridge", False))
            bypass_active = is_ace_bypass_active(GLOBAL_STATE, ace_status=live_status)
            ext_spool_to_track = (
                getattr(GLOBAL_STATE, "external_spool_id", None)
                if (bypass_active and bool(str(GLOBAL_STATE.spoolman_url or "").strip()))
                else None
            )
            endless_to_apply: Optional[Tuple[bool, str]] = None
            if not bypass_active and "endless_spool_enabled" in data:
                want_es_en = bool(data.get("endless_spool_enabled"))
                want_es_mode = str(data.get("endless_spool_mode") or "exact").strip().lower()
                if want_es_mode not in ("exact", "material", "next"):
                    want_es_mode = "exact"
                cur_es_en = bool((live_status or {}).get("endless_spool_enabled", False))
                cur_es_mode = str((live_status or {}).get("endless_spool_mode") or "exact").strip().lower()
                if want_es_en != cur_es_en or (want_es_en and want_es_mode != cur_es_mode):
                    endless_to_apply = (want_es_en, want_es_mode)

            def _apply_endless_spool_if_needed() -> None:
                if endless_to_apply is None:
                    return
                try:
                    client.set_endless_spool(endless_to_apply[0], endless_to_apply[1])
                    if isinstance(live_status, dict):
                        live_status["endless_spool_enabled"] = endless_to_apply[0]
                        live_status["endless_spool_mode"] = endless_to_apply[1]
                    with GLOBAL_STATE._lock:
                        if isinstance(GLOBAL_STATE.last_ace_status, dict):
                            GLOBAL_STATE.last_ace_status["endless_spool_enabled"] = endless_to_apply[0]
                            GLOBAL_STATE.last_ace_status["endless_spool_mode"] = endless_to_apply[1]
                except Exception:
                    pass

            if start_print_req:
                def _upload_and_start() -> None:
                    check_print_safety(client, "Moonraker Upload & Start Print", GLOBAL_STATE)
                    if ext_spool_to_track:
                        try:
                            client.set_active_spool(ext_spool_to_track)
                        except Exception:
                            pass
                    _apply_endless_spool_if_needed()
                    if not is_modal_bridge:
                        client.upload_gcode(filename=upload_name, gcode_content=patched_content, start_print=True)

                acquired, _ = self._run_with_hw_lock(
                    _upload_and_start, busy_key="host.hw_busy_upload", busy_modal=True
                )
                if not acquired:
                    return
            else:
                _apply_endless_spool_if_needed()
                if not is_modal_bridge:
                    client.upload_gcode(filename=upload_name, gcode_content=patched_content, start_print=False)

            done_key = "host.upload_done_started" if start_print_req else "host.upload_done"
            log_msg = t(done_key, lang=cur_lang, base=log_msg, url=client.active_url)
            mode_label = t("host.upload_mode_print" if start_print_req else "host.upload_mode_only", lang=cur_lang)
            modal_fb = {
                "level": "ok",
                "text": t(
                    "host.upload_modal_ok",
                    lang=cur_lang,
                    file=base_name,
                    url=client.active_url,
                    mode=mode_label,
                    len=purge_len,
                ),
            }
            extra_post["action"] = "close_preprint_modal"
            extra_post["action_reason"] = "upload_started" if start_print_req else "upload_only"

        out_payload: Dict[str, Any] = {
            "state": GLOBAL_STATE.to_dict(),
            "log": log_msg,
            "modal_feedback": modal_fb,
        }
        out_payload.update(extra_post)
        self.post_message(out_payload)
        if upload and not getattr(self, "_is_modal_bridge", False):
            threading.Thread(
                target=self._refresh_upload_status_sequence,
                args=(client, self.UPLOAD_REFRESH_DELAYS_SEC, upload_epoch),
                daemon=True,
            ).start()

    def _refresh_upload_status_sequence(
        self,
        client: MoonrakerClient,
        delays_sec: Tuple[float, ...],
        expected_epoch: int,
    ) -> None:
        """Poll live printer/ACE status shortly after upload/print start without blocking response."""
        with _WorkerThreadScope():
            elapsed = 0.0
            for target_delay in delays_sec:
                delta = max(0.0, float(target_delay) - elapsed)
                if delta > 0.0:
                    time.sleep(delta)
                elapsed = float(target_delay)
                with GLOBAL_STATE._lock:
                    if GLOBAL_STATE.profile_epoch != expected_epoch:
                        return
                    cur_lang = GLOBAL_STATE.language
                try:
                    self._send_state_and_live_status(
                        client, t("host.status_updated", lang=cur_lang)
                    )
                except Exception:
                    return

    def _msg_export_gcode(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        if isinstance(data.get("settings"), dict):
            GLOBAL_STATE.apply_dict(data["settings"], apply_gcode_state=False, save=False)
        gcode_path = str(data.get("gcode_path") or GLOBAL_STATE.last_gcode_path).strip()
        cur_lang = GLOBAL_STATE.language
        if not gcode_path or not os.path.isfile(gcode_path):
            no_file_msg = (
                t("host.no_gcode_loaded", lang=cur_lang)
                if not gcode_path
                else t("host.file_not_found", lang=cur_lang, path=gcode_path)
            )
            self.post_message({
                "state": GLOBAL_STATE.to_dict(),
                "log": no_file_msg,
                "modal_feedback": {"level": "error", "text": no_file_msg},
            })
            return

        dest_path = str(data.get("dest_path") or "").strip()
        mismatch_conf: Optional[bool] = None
        if data.get("require_mismatch_gate") or "mismatch_confirmed" in data:
            mismatch_conf = bool(data.get("mismatch_confirmed", GLOBAL_STATE.material_mismatch_confirmed))

        with GLOBAL_STATE._lock:
            cached_status = dict(GLOBAL_STATE.last_ace_status) if isinstance(GLOBAL_STATE.last_ace_status, dict) else None

        used_fallback_path = False
        requested_dest = dest_path
        try:
            export_res = export_patched_gcode(
                gcode_path,
                state=GLOBAL_STATE,
                dest_path=dest_path,
                ace_status=cached_status,
                mismatch_confirmed=mismatch_conf,
            )
        except (PermissionError, OSError) as exc:
            if not dest_path:
                err_txt = str(exc)
                self.post_message({
                    "state": GLOBAL_STATE.to_dict(),
                    "log": err_txt,
                    "modal_feedback": {"level": "error", "text": err_txt},
                })
                return
            storage_dir = GLOBAL_STATE.resolve_storage_dir()
            fallback_name = _sanitize_staged_gcode_filename(os.path.basename(dest_path) or os.path.basename(gcode_path))
            fallback_dest = os.path.join(storage_dir, "exported_gcode", fallback_name)
            try:
                export_res = export_patched_gcode(
                    gcode_path,
                    state=GLOBAL_STATE,
                    dest_path=fallback_dest,
                    ace_status=cached_status,
                    mismatch_confirmed=mismatch_conf,
                )
                dest_path = export_res.get("dest_path") or fallback_dest
                used_fallback_path = True
            except Exception as fb_exc:
                err_txt = f"{exc} (fallback failed: {fb_exc})"
                self.post_message({
                    "state": GLOBAL_STATE.to_dict(),
                    "log": err_txt,
                    "modal_feedback": {"level": "error", "text": err_txt},
                })
                return
        except ValueError as exc:
            err_txt = str(exc)
            self.post_message({
                "state": GLOBAL_STATE.to_dict(),
                "log": err_txt,
                "modal_feedback": {"level": "error", "text": err_txt},
            })
            return

        info = export_res["info"]
        purge_len = f"{info.get('total_purge_length_mm', 0.0):.2f}"
        tc_count = info.get("toolchanges", 0)
        if dest_path:
            final_dest = export_res.get("dest_path") or dest_path
            if used_fallback_path:
                done_msg = t(
                    "host.export_done_fallback",
                    lang=cur_lang,
                    requested=requested_dest,
                    path=final_dest,
                    tc=tc_count,
                    len=purge_len,
                )
            else:
                final_dir = os.path.dirname(final_dest)
                if final_dir:
                    with GLOBAL_STATE._lock:
                        if GLOBAL_STATE.default_gcode_dir != final_dir:
                            GLOBAL_STATE.default_gcode_dir = final_dir
                    try:
                        GLOBAL_STATE.save_config()
                    except Exception:
                        pass
                done_msg = t("host.export_done", lang=cur_lang, path=final_dest, tc=tc_count, len=purge_len)
            self.post_message({
                "state": GLOBAL_STATE.to_dict(),
                "log": done_msg,
                "modal_feedback": {"level": "warn" if used_fallback_path else "ok", "text": done_msg},
            })
            return

        ui_msg = t("host.export_done_ui", lang=cur_lang, file=export_res["filename"], tc=tc_count, len=purge_len)
        self.post_message({
            "action": "export_gcode_ready",
            "filename": export_res["filename"],
            "content": export_res["content"],
            "state": GLOBAL_STATE.to_dict(),
            "log": ui_msg,
        })

    def _run_async_message_impl(self, msg_type: str, data: Dict[str, Any]) -> None:
        client = GLOBAL_STATE.get_client()
        handlers = {
            "ready": self._msg_ready,
            "preprint_modal_opened": self._msg_preprint_modal_opened,
            "acquire_slicer_gcode": self._msg_acquire_slicer_gcode,
            "load_gcode_content": self._msg_load_gcode_content,
            "sync_orca_filaments": self._msg_sync_orca_filaments,
            "get_status": self._msg_get_status,
            "switch_printer": self._msg_switch_printer,
            "save_printer_profile": self._msg_save_printer_profile,
            "delete_printer_profile": self._msg_delete_printer_profile,
            "test_spoolman": self._msg_test_spoolman,
            "refresh_spoolman": self._msg_test_spoolman,
            "select_slot_spool": self._msg_select_slot_spool,
            "get_diagnostics_bundle": self._msg_get_diagnostics_bundle,
            "dryer_start": lambda c, d: self._msg_dryer(c, d, active=True),
            "dryer_stop": lambda c, d: self._msg_dryer(c, d, active=False),
            "set_ace_pro_enabled": self._msg_set_ace_pro_enabled,
            "slot_action": self._msg_slot_action,
            "parse_gcode": self._msg_parse_gcode,
            "clear_gcode": self._msg_clear_gcode,
            "update_mapping": self._msg_update_mapping,
            "auto_match": self._msg_auto_match,
            "reset_mapping": self._msg_reset_mapping,
            "preview_gcode": self._msg_preview_gcode,
            "export_gcode": self._msg_export_gcode,
            "postprocess_only": lambda c, d: self._msg_postprocess(c, d, upload=False),
            "postprocess_and_upload": lambda c, d: self._msg_postprocess(c, d, upload=True),
        }
        handler = handlers.get(msg_type)
        if handler is None:
            return
        try:
            handler(client, data)
        except Exception as exc:
            err_msg = t("host.msg_error", lang=GLOBAL_STATE.language, msg_type=msg_type, error=exc)
            err_payload: Dict[str, Any] = {
                "state": GLOBAL_STATE.to_dict(),
                "log": err_msg,
            }
            if msg_type in (
                "postprocess_only",
                "postprocess_and_upload",
                "export_gcode",
                "acquire_slicer_gcode",
                "load_gcode_content",
                "preview_gcode",
            ):
                err_payload["modal_feedback"] = {"level": "error", "text": err_msg}
            self.post_message(err_payload)

    def _msg_test_spoolman(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        if "spoolman_url" in data:
            GLOBAL_STATE.apply_dict({"spoolman_url": data.get("spoolman_url")}, apply_gcode_state=False)
        with GLOBAL_STATE._lock:
            expected_epoch = GLOBAL_STATE.profile_epoch
            cur_lang = GLOBAL_STATE.language
            sp_url = str(GLOBAL_STATE.spoolman_url or "")
            base_status = dict(GLOBAL_STATE.last_ace_status) if isinstance(GLOBAL_STATE.last_ace_status, dict) else {}

        if not sp_url:
            enriched = enrich_ace_status_with_spoolman(
                base_status or {"slots": []}, GLOBAL_STATE, force_fetch=False, expected_epoch=expected_epoch
            )
            if base_status and not GLOBAL_STATE._commit_ace_status(enriched, expected_epoch):
                return
            log_msg = t("host.spoolman_disabled", lang=cur_lang)
            payload: Dict[str, Any] = {
                "state": GLOBAL_STATE.to_dict(),
                "spoolman_status": dict(GLOBAL_STATE.last_spoolman_status),
                "log": log_msg,
            }
            if base_status:
                payload["ace_status"] = enriched
            self.post_message(payload)
            return

        if not (base_status.get("slots")):
            try:
                base_status = client.query_ace_status()
            except Exception:
                base_status = {"slots": []}

        enriched = enrich_ace_status_with_spoolman(
            base_status, GLOBAL_STATE, force_fetch=True, expected_epoch=expected_epoch
        )
        if enriched.get("slots"):
            if not GLOBAL_STATE._commit_ace_status(enriched, expected_epoch):
                return
        with GLOBAL_STATE._lock:
            if GLOBAL_STATE.profile_epoch != expected_epoch:
                return
            sm_stat = dict(GLOBAL_STATE.last_spoolman_status)

        if sm_stat.get("connected"):
            conflict_cnt = int(sm_stat.get("conflict_count", 0) or 0)
            if conflict_cnt > 0:
                log_msg = t(
                    "host.spoolman_ok_conflicts",
                    lang=cur_lang,
                    spools=sm_stat.get("spools_count", 0),
                    matched=sm_stat.get("matched_count", 0),
                    conflicts=conflict_cnt,
                    url=sm_stat.get("url", sp_url),
                )
            else:
                log_msg = t(
                    "host.spoolman_ok",
                    lang=cur_lang,
                    spools=sm_stat.get("spools_count", 0),
                    matched=sm_stat.get("matched_count", 0),
                    url=sm_stat.get("url", sp_url),
                )
        else:
            log_msg = t(
                "host.spoolman_err",
                lang=cur_lang,
                url=sm_stat.get("url", sp_url),
                error=sm_stat.get("error") or "unreachable",
            )
        payload = {
            "state": GLOBAL_STATE.to_dict(),
            "spoolman_status": sm_stat,
            "log": log_msg,
        }
        if enriched.get("slots"):
            payload["ace_status"] = enriched
        self.post_message(payload)

    def _msg_select_slot_spool(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        try:
            tool = int(data.get("tool", -1))
        except (ValueError, TypeError):
            return
        if tool < 0 or tool >= MAX_ACE_SLOTS:
            return
        raw_sid = data.get("spool_id")
        if raw_sid is None or raw_sid == "" or raw_sid == "auto":
            spool_id: Optional[int] = None
        else:
            try:
                spool_id = int(raw_sid)
            except (ValueError, TypeError):
                return
        sku = str(data.get("sku") or "")
        with GLOBAL_STATE._lock:
            expected_epoch = GLOBAL_STATE.profile_epoch
            cur_lang = GLOBAL_STATE.language
            base_status = dict(GLOBAL_STATE.last_ace_status) if isinstance(GLOBAL_STATE.last_ace_status, dict) else {}
            if not sku:
                for sl in base_status.get("slots") or []:
                    if isinstance(sl, dict) and int(sl.get("tool", -1)) == tool:
                        sku = str(sl.get("sku") or sl.get("article_number") or sl.get("code") or sl.get("name") or "")
                        break
        GLOBAL_STATE.set_manual_slot_spool(tool, spool_id, sku=sku, save=True)
        enriched = enrich_ace_status_with_spoolman(
            base_status or {"slots": []}, GLOBAL_STATE, force_fetch=False, expected_epoch=expected_epoch
        )
        if enriched.get("slots") and not GLOBAL_STATE._commit_ace_status(enriched, expected_epoch):
            return
        with GLOBAL_STATE._lock:
            sm_stat = dict(GLOBAL_STATE.last_spoolman_status)
        log_msg = (
            t("host.slot_spool_saved", lang=cur_lang, tool=tool, spool_id=spool_id)
            if spool_id is not None
            else t("host.slot_spool_cleared", lang=cur_lang, tool=tool)
        )
        payload: Dict[str, Any] = {
            "state": GLOBAL_STATE.to_dict(),
            "spoolman_status": sm_stat,
            "log": log_msg,
        }
        if enriched.get("slots") or base_status:
            payload["ace_status"] = enriched
        self.post_message(payload)

    def _msg_get_diagnostics_bundle(self, client: MoonrakerClient, data: Dict[str, Any]) -> None:
        self.post_message({"anonymized_diagnostics": build_anonymized_diagnostics(GLOBAL_STATE)})

    @staticmethod
    def _build_disconnected_ace_status(
        *,
        configured: bool,
        connection_state: str,
        error: str = "",
        ace_instances: int = 1,
    ) -> Dict[str, Any]:
        inst_cnt = max(1, min(MAX_ACE_UNITS, int(ace_instances or 1)))
        return {
            "connected": False,
            "configured": configured,
            "connection_state": connection_state,
            "ace_instances": inst_cnt,
            "ace_count": inst_cnt,
            "print_state": "standby",
            "print_filename": "",
            "is_printing": False,
            "current_index": -1,
            "slots": [],
            "error": error,
        }

    def _refresh_live_ace_state(
        self,
        client: MoonrakerClient,
        log_msg: str,
    ) -> Optional[Dict[str, Any]]:
        """Query Moonraker + Spoolman live status and commit under `profile_epoch` guard."""
        with GLOBAL_STATE._lock:
            expected_epoch = GLOBAL_STATE.profile_epoch
        client_configured = bool(
            getattr(
                client,
                "is_configured",
                bool(getattr(client, "base_url", "") or getattr(client, "active_url", "")),
            )
        )
        client_key = str(getattr(client, "api_key", "") or "")
        if not client_configured:
            unconfigured_status = self._build_disconnected_ace_status(
                configured=False,
                connection_state="unconfigured",
                ace_instances=GLOBAL_STATE.resolve_effective_ace_count(),
            )
            if str(getattr(GLOBAL_STATE, "spoolman_url", "") or "").strip():
                unconfigured_status = enrich_ace_status_with_spoolman(
                    unconfigured_status, GLOBAL_STATE, force_fetch=False, expected_epoch=expected_epoch
                )
            if not GLOBAL_STATE._commit_ace_status(unconfigured_status, expected_epoch):
                return None
            with GLOBAL_STATE._lock:
                cur_lang = GLOBAL_STATE.language
            return {
                "ace_status": unconfigured_status,
                "log": f"{log_msg} ({t('host.moonraker_unconfigured', lang=cur_lang)})",
            }
        try:
            ace_status = client.query_ace_status()
            ace_status["configured"] = True
            ace_status["connection_state"] = (
                "connected_active" if ace_status.get("is_printing") else "connected_standby"
            )
            ace_status = enrich_ace_status_with_spoolman(
                ace_status, GLOBAL_STATE, force_fetch=False, expected_epoch=expected_epoch
            )
            det_cnt = int(
                ace_status.get("ace_instances")
                or ((len(ace_status.get("slots") or []) + SLOTS_PER_ACE_UNIT - 1) // SLOTS_PER_ACE_UNIT)
                or 1
            )
            if not GLOBAL_STATE._commit_ace_status(
                ace_status, expected_epoch, detected_ace_count=det_cnt
            ):
                return None
            with GLOBAL_STATE._lock:
                slicer_fils = list(GLOBAL_STATE.slicer_filaments)
                tmap = dict(GLOBAL_STATE.tool_map)
                cur_lang = GLOBAL_STATE.language
            if slicer_fils and ace_status.get("slots") and not is_ace_bypass_active(GLOBAL_STATE, ace_status=ace_status):
                validation = validate_color_mapping(slicer_fils, tmap, ace_status["slots"], lang=cur_lang)
                with GLOBAL_STATE._lock:
                    if GLOBAL_STATE.profile_epoch != expected_epoch:
                        return None
                    GLOBAL_STATE.mapping_warnings = list(validation["errors"] + validation["warnings"])
            else:
                with GLOBAL_STATE._lock:
                    if GLOBAL_STATE.profile_epoch != expected_epoch:
                        return None
                    GLOBAL_STATE.mapping_warnings = []
            return {
                "ace_status": ace_status,
                "log": f"{log_msg} (Moonraker: {client.active_url})",
            }
        except Exception as exc:
            safe_err = anonymize_diagnostic_text(str(exc), [client_key] if client_key else None)
            offline_status = self._build_disconnected_ace_status(
                configured=True,
                connection_state="offline",
                error=safe_err,
                ace_instances=GLOBAL_STATE.resolve_effective_ace_count(),
            )
            if not GLOBAL_STATE._commit_ace_status(offline_status, expected_epoch):
                return None
            with GLOBAL_STATE._lock:
                cur_lang = GLOBAL_STATE.language
            return {
                "ace_status": offline_status,
                "log": t("host.status_query_failed", lang=cur_lang, base=log_msg, error=safe_err),
            }

    def _send_state_and_live_status(
        self,
        client: MoonrakerClient,
        log_msg: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        live_res = self._refresh_live_ace_state(client, log_msg)
        if live_res is None:
            return
        payload: Dict[str, Any] = dict(live_res)
        slice_invalidated = check_and_invalidate_missing_preview_slice(GLOBAL_STATE)
        payload["state"] = GLOBAL_STATE.to_dict()
        payload["anonymized_diagnostics"] = build_anonymized_diagnostics(GLOBAL_STATE)
        if slice_invalidated and not (extra and "action" in extra):
            payload["action"] = "close_preprint_modal"
            payload["action_reason"] = "slice_invalidated"
            with GLOBAL_STATE._lock:
                inv_lang = GLOBAL_STATE.language
                inv_reason_st = str(getattr(GLOBAL_STATE, "_last_invalidation_reason", "") or "")
            payload["notice"] = {
                "level": "warn",
                "text": _build_invalidation_notice_text(inv_reason_st, inv_lang),
            }
            payload["log"] = f"{payload.get('log', log_msg)} — {t('host.slice_invalidated', lang=inv_lang)}"
        if extra:
            payload.update(extra)
        self.post_message(payload)


def _enrich_audit_info(
    info: Dict[str, Any],
    filaments: List[Dict[str, Any]],
    tool_map: Dict[int, int],
    ace_slots: List[Dict[str, Any]],
    lang: Optional[str] = None,
    ace_bypass: bool = False,
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
    info["aceBypass"] = bool(ace_bypass)
    info["mapping_validation"] = (
        validate_color_mapping([], {}, [], lang=lang)
        if ace_bypass
        else validate_color_mapping(filaments, tool_map, ace_slots, lang=lang)
    )
    return info


def _read_validated_gcode_file(
    gcode_path: str,
    plate_index: Optional[int] = None,
    output_name: str = "",
) -> Dict[str, Any]:
    """
    Safely read either a plain text `.gcode` file or an OrcaSlicer `.gcode.3mf` ZIP container
    strictly read-only in a single read pass, rejecting binary garbage or corrupt inputs cleanly.
    """
    raw_path = str(gcode_path or "").strip()
    if not raw_path or not os.path.isfile(raw_path):
        raise FileNotFoundError(f"G-code file not found: {raw_path}")
    raw_bytes = _read_source_file_single_pass(raw_path)
    return _decode_gcode_or_3mf_bytes(
        raw_bytes,
        source_name=output_name or raw_path,
        plate_index=plate_index,
        binary_error_message=f"Rejected '{os.path.basename(raw_path)}': binary or corrupt content is not valid G-code.",
    )


def analyze_gcode_flush(
    lines: List[str],
    state: Optional[PluginState] = None,
    filepath: str = "",
    extra_slice_info: Optional[Dict[str, Any]] = None,
    output_name: str = "",
    plate_index: Optional[int] = None,
    pre_filaments: Optional[List[Dict[str, Any]]] = None,
    pre_job_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Pure in-memory read-only G-code & flush-volume analysis using `state` configuration.
    Never modifies files or makes any network requests.
    """
    st = state or GLOBAL_STATE
    diam, tmap, bed_lvl, skip_purge, cut_post, force_init, ace_slots, cur_lang = st.get_postprocess_options()
    if pre_filaments is not None and pre_job_metadata is not None:
        filaments = pre_filaments
        job_metadata = pre_job_metadata
    else:
        with st._lock:
            effective_path = filepath or st.last_gcode_path
        filaments = parse_slicer_filaments(lines)
        job_metadata = parse_gcode_job_metadata(lines, effective_path)
        if output_name:
            job_metadata["output_name"] = _sanitize_staged_gcode_filename(output_name, plate_index=plate_index)
        if plate_index is not None:
            job_metadata["plate_index"] = int(plate_index)
        _merge_slice_info_into_parsed(filaments, job_metadata, extra_slice_info)
    with st._lock:
        ace_st_snap = dict(st.last_ace_status or {})
    cur_loaded_slot: Optional[int] = None
    if ace_st_snap.get("connected") and ace_st_snap.get("current_index") is not None:
        try:
            ci = int(ace_st_snap["current_index"])
            if ci >= 0:
                cur_loaded_slot = ci
        except (ValueError, TypeError):
            cur_loaded_slot = None
    ace_bypass = is_ace_bypass_active(st, ace_status=ace_st_snap)
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
        current_loaded_slot=cur_loaded_slot,
        ace_bypass=ace_bypass,
        external_spool_id=getattr(st, "external_spool_id", None) if (ace_bypass and bool(str(st.spoolman_url or "").strip())) else None,
    )
    info["job_metadata"] = job_metadata
    return _enrich_audit_info(info, filaments, tmap, ace_slots, lang=cur_lang, ace_bypass=ace_bypass)


def preview_gcode_file(
    gcode_path: str,
    state: Optional[PluginState] = None,
    extra_slice_info: Optional[Dict[str, Any]] = None,
    plate_index: Optional[int] = None,
    output_name: str = "",
    *,
    pre_lines: Optional[List[str]] = None,
    pre_filaments: Optional[List[Dict[str, Any]]] = None,
    pre_job_metadata: Optional[Dict[str, Any]] = None,
    pre_sidecar: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Read `gcode_path` (`.gcode` or `.gcode.3mf` container) strictly read-only, validate content,
    analyze flush metrics & events, and update in-memory preview state without writing to disk.
    """
    st = state or GLOBAL_STATE
    if pre_lines is not None:
        lines = pre_lines
        merged_slice_info = extra_slice_info or {}
        resolved_plate = plate_index
    else:
        parsed = _read_validated_gcode_file(gcode_path, plate_index=plate_index, output_name=output_name)
        lines = parsed["lines"]
        merged_slice_info = extra_slice_info or parsed.get("slice_info") or {}
        resolved_plate = plate_index if plate_index is not None else parsed.get("plate_index")

    filaments = list(pre_filaments) if pre_filaments is not None else parse_slicer_filaments(lines)
    job_meta = dict(pre_job_metadata) if pre_job_metadata is not None else parse_gcode_job_metadata(lines, gcode_path)
    sidecar = pre_sidecar if pre_sidecar is not None else _read_provenance_sidecar(gcode_path)
    eff_output_name = output_name or str(sidecar.get("output_name") or "")
    if eff_output_name:
        job_meta["output_name"] = eff_output_name
    if resolved_plate is None and sidecar.get("plate_index") is not None:
        resolved_plate = _normalize_plate_index(sidecar.get("plate_index"))
    if resolved_plate is not None:
        job_meta["plate_index"] = resolved_plate
    if pre_filaments is None or pre_job_metadata is None:
        _merge_slice_info_into_parsed(filaments, job_meta, merged_slice_info)

    with st._lock:
        st.last_gcode_path = gcode_path
        st.update_slicer_filaments(filaments)
        st.last_job_metadata = job_meta

    info = analyze_gcode_flush(
        lines,
        st,
        filepath=gcode_path,
        extra_slice_info=merged_slice_info,
        output_name=eff_output_name,
        plate_index=resolved_plate,
        pre_filaments=filaments,
        pre_job_metadata=job_meta,
    )
    info["analysis_mode"] = "preview"
    parsed_sig = None
    if gcode_path and os.path.isfile(gcode_path):
        try:
            parsed_sig = (
                os.path.realpath(gcode_path),
                _get_file_mtime_ns(gcode_path),
                int(os.path.getsize(gcode_path)),
            )
        except OSError:
            parsed_sig = None
    with st._lock:
        st.last_postprocess_info = dict(info)
        st._last_parsed_file_sig = parsed_sig
    return info


def can_upload_gcode(
    info: Optional[Dict[str, Any]],
    state: Optional[PluginState] = None,
    ace_status: Optional[Dict[str, Any]] = None,
    start_print: bool = False,
    mismatch_confirmed: Optional[bool] = None,
    allow_stale: bool = False,
) -> Tuple[bool, List[str], List[str]]:
    """
    Upload Safety Gate: blocks Moonraker upload when multi-tool flush data is incomplete,
    color mapping targets empty/out-of-range active slots, material mismatch is
    unconfirmed (`mismatch_confirmed=False`), project/slice provenance is stale/mismatched
    or uncertain (`allow_stale=False`), or `start_print=True` while another print job is
    already running (`printing` or `paused`).
    Returns `(allowed, blocking_reasons, warnings)`.
    """
    st = state or GLOBAL_STATE
    cur_lang = getattr(st, "language", DEFAULT_LANGUAGE)
    blocking_reasons: List[str] = []
    warnings: List[str] = list((info or {}).get("warnings", []))

    if not info or not isinstance(info, dict):
        blocking_reasons.append(t("gate.block.no_info", lang=cur_lang))
        return False, blocking_reasons, warnings

    with st._lock:
        fils = list(info.get("slicer_filaments") or st.slicer_filaments)
        tmap = dict(info.get("toolMap") if isinstance(info.get("toolMap"), dict) else st.tool_map)
        effective_status = ace_status if ace_status is not None else st.last_ace_status
        active_path = str(st.last_gcode_path or "")

    ace_bypass = is_ace_bypass_active(st, ace_status=effective_status)
    used_tools = info.get("used_slicer_tools") or []
    flush_status = str(info.get("flush_status") or "")
    flush_complete = bool(info.get("flush_data_complete", True))
    if not ace_bypass:
        if flush_status == "FLUSH_DATA_INCOMPLETE":
            blocking_reasons.append(t("gate.block.flush_incomplete", lang=cur_lang))
        elif len(used_tools) > 1 and not flush_complete:
            blocking_reasons.append(t("gate.block.multi_incomplete", lang=cur_lang))

    job_meta = info.get("job_metadata") if isinstance(info.get("job_metadata"), dict) else {}
    stale_check = check_stale_gcode_model(
        gcode_path=str(job_meta.get("filepath") or active_path or ""),
        filename=str(job_meta.get("output_name") or job_meta.get("filename") or ""),
        state=st,
        job_meta=job_meta,
        slicer_filaments=fils,
        plate_index=job_meta.get("plate_index"),
    )
    if not allow_stale and (stale_check.get("is_stale") or (stale_check.get("uncertain") and stale_check.get("active_model"))):
        reason_code = str(stale_check.get("reason") or "model_mismatch")
        reason_label = _human_stale_reason(reason_code, lang=cur_lang)
        loaded_label = str(stale_check.get("loaded_file") or os.path.basename(active_path) or "unknown")
        blocking_reasons.append(
            t("gate.block.stale_gcode", lang=cur_lang, file=loaded_label, reason=reason_label)
        )

    if not ace_bypass:
        known_slots = st.resolve_known_effective_slot_count(effective_status)
        bounds_errs, _, _ = find_slot_bounds_errors(
            fils,
            tmap,
            known_slots,
            lang=cur_lang,
            g9111_mapped_tool=info.get("g9111MappedTool"),
            used_tools=used_tools,
        )
        blocking_reasons.extend(bounds_errs)

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


def _postprocess_gcode_file_impl(
    gcode_path: str,
    state: Optional[PluginState] = None,
    output_name: str = "",
    plate_index: Optional[int] = None,
    *,
    write_back: bool = True,
    enforce_slot_bounds: bool = True,
    ace_status: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """Internal implementation returning `(info, patched_lines, parsed_read_dict)`."""
    st = state or GLOBAL_STATE
    parsed = _read_validated_gcode_file(gcode_path, plate_index=plate_index, output_name=output_name)
    lines = parsed["lines"]
    slice_info = parsed.get("slice_info") or {}
    resolved_plate = plate_index if plate_index is not None else parsed.get("plate_index")

    filaments = parse_slicer_filaments(lines)
    job_meta = parse_gcode_job_metadata(lines, gcode_path)
    if output_name:
        job_meta["output_name"] = _sanitize_staged_gcode_filename(output_name, plate_index=resolved_plate)
    if resolved_plate is not None:
        job_meta["plate_index"] = resolved_plate
    _merge_slice_info_into_parsed(filaments, job_meta, slice_info)

    if write_back:
        with st._lock:
            if not parsed.get("is_container"):
                st.last_gcode_path = gcode_path
            st.update_slicer_filaments(filaments)
            st.last_job_metadata = job_meta

    diam, tmap, bed_lvl, skip_purge, cut_post, force_init, ace_slots, cur_lang = st.get_postprocess_options()
    with st._lock:
        eff_ace_status = ace_status if ace_status is not None else (
            dict(st.last_ace_status) if isinstance(st.last_ace_status, dict) else None
        )
    ace_bypass = is_ace_bypass_active(st, ace_status=eff_ace_status)
    known_slots = st.resolve_known_effective_slot_count(eff_ace_status)
    if enforce_slot_bounds and not ace_bypass and known_slots is not None:
        exec_used_tools = [
            int(m.group(2))
            for ln in lines
            if not ln.lstrip().startswith(";")
            for m in (TOOL_RE.match(ln),)
            if m
        ]
        g9111_tools = [
            int(mp.group(2))
            for ln in lines
            if not ln.lstrip().startswith(";") and G9111_RE.match(ln)
            for mp in (G9111_TOOL_PARAM_RE.search(ln),)
            if mp
        ]
        bounds_errs, _, _ = find_slot_bounds_errors(
            filaments,
            tmap,
            known_slots,
            lang=cur_lang,
            used_tools=exec_used_tools + g9111_tools,
        )
        if bounds_errs:
            raise SlotBoundsError("; ".join(bounds_errs))

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
        ace_bypass=ace_bypass,
        external_spool_id=getattr(st, "external_spool_id", None) if (ace_bypass and bool(str(st.spoolman_url or "").strip())) else None,
    )
    info["job_metadata"] = job_meta
    _enrich_audit_info(info, filaments, tmap, ace_slots, lang=cur_lang, ace_bypass=ace_bypass)
    info["analysis_mode"] = "postprocessed"
    if resolved_plate is not None:
        info["plate_index"] = resolved_plate

    target_abs = os.path.abspath(gcode_path)
    patched_text = "".join(out_lines)
    patched_bytes = patched_text.encode("utf-8")
    parsed["gcode_text"] = patched_text
    if write_back:
        if parsed.get("is_container") and parsed.get("zip_entry"):
            _write_gcode_back_to_3mf(target_abs, str(parsed["zip_entry"]), patched_bytes)
        else:
            _atomic_write_bytes(target_abs, patched_bytes, suffix=".gcode")

        with st._lock:
            st.last_postprocess_info = info
        st.save_config()
    return info, out_lines, parsed


def postprocess_gcode_file(
    gcode_path: str,
    state: Optional[PluginState] = None,
    output_name: str = "",
    plate_index: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Read & validate `.gcode` or `.gcode.3mf`, strip prior ACE artifacts, post-process,
    and atomically write the updated G-code back using `state`.
    """
    info, _, _ = _postprocess_gcode_file_impl(
        gcode_path=gcode_path,
        state=state,
        output_name=output_name,
        plate_index=plate_index,
    )
    return info


def _derive_export_gcode_filename(gcode_path: str, job_meta: Optional[Dict[str, Any]] = None) -> str:
    raw = str(
        (job_meta or {}).get("output_name")
        or (job_meta or {}).get("filename")
        or os.path.basename(gcode_path)
        or "exported.gcode"
    ).strip()
    base = os.path.basename(raw)
    low = base.lower()
    if low.endswith(".gcode.3mf"):
        base = base[:-4]
    elif low.endswith(".3mf"):
        base = base[:-4] + ".gcode"
    elif not low.endswith((".gcode", ".gco", ".gc")):
        base = f"{base}.gcode"
    return base or "exported.gcode"


def export_patched_gcode(
    gcode_path: str,
    state: Optional[PluginState] = None,
    dest_path: str = "",
    ace_status: Optional[Dict[str, Any]] = None,
    mismatch_confirmed: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Generate fully patched G-code from `gcode_path` without modifying or moving the
    source temp/staged file. Optionally writes to `dest_path` when supplied.
    """
    st = state or GLOBAL_STATE
    info, out_lines, parsed = _postprocess_gcode_file_impl(
        gcode_path=gcode_path,
        state=st,
        write_back=False,
        ace_status=ace_status,
    )
    allowed, blocking_reasons, _ = can_upload_gcode(
        info,
        st,
        ace_status=ace_status,
        start_print=False,
        mismatch_confirmed=mismatch_confirmed,
        allow_stale=True,
    )
    if not allowed:
        raise ValueError("; ".join(blocking_reasons))

    patched_text = str(parsed.get("gcode_text") or "".join(out_lines))
    export_name = _derive_export_gcode_filename(gcode_path, info.get("job_metadata"))
    resolved_dest = ""
    if dest_path:
        resolved_dest = os.path.abspath(dest_path)
        _atomic_write_bytes(
            resolved_dest,
            patched_text.encode("utf-8"),
            suffix=".gcode",
            ensure_parent=True,
        )

    return {
        "info": info,
        "filename": export_name,
        "content": patched_text,
        "dest_path": resolved_dest,
        "source_path": gcode_path,
    }


_ACTIVE_MODAL_SESSIONS_LOCK = threading.Lock()
_ACTIVE_MODAL_SESSIONS: Set[Any] = set()


class _SlicingModalSetupSession:
    """
    Synchronous `WINDOW_MODAL` session controller (following `OpenCreatorSend`/`SnapSend`/`u1control` lifecycle):
    - Bypasses modal when `ctx.host == "File"` (file export only).
    - Uses `orca.host.ui.create_window(..., style=WINDOW_MODAL, width=980, height=740, on_message=..., on_close=...)`
      and delivers messages via `window.post(...)` or `window.post_message(...)`.
    - Delegates full UI actions (`get_status`, `update_mapping`, `auto_match`, `reset_mapping`, `save_settings`,
      `export_gcode`, `postprocess`) through a page-bridge targeting the modal WebView window.
    - On Apply/Proceed, re-patches `source_ctx_path` (`ctx.gcode_path`) so OrcaSlicer's native upload sends
      the updated mapping/purge G-code; on plugin-direct upload or cancel, aborts duplicate native upload.
    """

    def __init__(
        self,
        ctx: Any,
        state: PluginState,
        gcode_path: str,
        info: Dict[str, Any],
        ready_timeout_s: float = 10.0,
        poll_interval_s: float = 0.2,
        source_ctx_path: str = "",
    ) -> None:
        self.ctx = ctx
        self.state = state
        self.gcode_path = gcode_path
        self.source_ctx_path = source_ctx_path or gcode_path
        self.info = info
        self.ready_timeout_s = max(0.05, float(ready_timeout_s))
        self.poll_interval_s = max(0.01, float(poll_interval_s))
        self.ready_evt = threading.Event()
        self.done_evt = threading.Event()
        self.outcome: str = "pending"  # "proceed" | "uploaded_by_plugin" | "cancelled" | "unloaded" | "ready_timeout"
        self.window: Any = None
        self._closed = False
        self._work_lock = threading.Lock()
        self._post_lock = threading.Lock()
        self._bridge_page: Optional[KobraS1AcePageCapability] = None

    def _post_to_window(self, payload: Any) -> None:
        with self._post_lock:
            win = self.window
            if win is None or self._closed or self.done_evt.is_set():
                return
            for meth in ("post", "post_message"):
                fn = getattr(win, meth, None)
                if callable(fn):
                    try:
                        fn(payload)
                        return
                    except Exception:
                        continue

    def _ensure_bridge_page(self) -> KobraS1AcePageCapability:
        if self._bridge_page is None:
            bp = KobraS1AcePageCapability()
            bp._is_modal_bridge = True  # type: ignore[attr-defined]
            bp.post_message = self._post_to_window  # type: ignore[method-assign]
            self._bridge_page = bp
        return self._bridge_page

    def is_ctx_cancelled(self) -> bool:
        fn = getattr(self.ctx, "cancelled", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        return False

    def _repatch_ctx_source_if_needed(self) -> None:
        target = str(self.source_ctx_path or "").strip()
        if not target or not os.path.isfile(target):
            return
        try:
            out_name = str(getattr(self.ctx, "output_name", "") or "").strip()
            plate_idx = _normalize_plate_index(getattr(self.ctx, "plate_index", getattr(self.ctx, "plate", None)))
            _postprocess_gcode_file_impl(
                target,
                self.state,
                output_name=out_name,
                plate_index=plate_idx,
                write_back=True,
            )
        except Exception:
            pass

    def on_message(self, data: Any) -> None:
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return
        if not isinstance(data, dict):
            return
        mtype = str(data.get("type") or "")
        if mtype == "ready":
            self.ready_evt.set()
            self._post_to_window(
                {
                    "action": "open_preprint_modal",
                    "source": "window_modal",
                    "standalone_modal": True,
                    "source_file": os.path.basename(self.gcode_path),
                    "state": self.state.to_dict(),
                }
            )
            if self.state is GLOBAL_STATE:
                def _refresh_modal_live() -> None:
                    if not self._work_lock.acquire(blocking=False):
                        return
                    try:
                        with _WorkerThreadScope():
                            if self.done_evt.is_set() or self._closed:
                                return
                            try:
                                bp = self._ensure_bridge_page()
                                bp._send_state_and_live_status(
                                    self.state.get_client(),
                                    t("host.ready", lang=self.state.language),
                                )
                            except Exception:
                                pass
                    finally:
                        self._work_lock.release()
                threading.Thread(target=_refresh_modal_live, daemon=True).start()
            return
        if mtype in ("confirm_print_setup", "modal_confirm", "postprocess_and_upload_done"):
            self._repatch_ctx_source_if_needed()
            self.finish("proceed")
            return
        if mtype in ("cancel_print_setup", "modal_cancel", "close_standalone_window"):
            self.finish("cancelled")
            return

        # Delegate live interactive UI messages (mapping edits, auto-match, upload, local apply, export, etc.)
        if self.state is GLOBAL_STATE and mtype:
            def _handle_modal_action() -> None:
                with _WorkerThreadScope():
                    with self._work_lock:
                        if self.done_evt.is_set() or self._closed:
                            return
                        bp = self._ensure_bridge_page()
                        if "settings" in data and isinstance(data["settings"], dict):
                            self.state.apply_dict(data["settings"], apply_gcode_state=False)
                            if mtype == "update_mapping":
                                self.state.record_session_manual_mapping()
                        if mtype == "save_settings":
                            self._post_to_window({"state": self.state.to_dict()})
                            return
                        if mtype == "set_language":
                            self.state.set_language(data.get("language"), save=True)
                            self._post_to_window({"state": self.state.to_dict()})
                            return
                        if mtype == "set_appearance":
                            self.state.set_appearance(
                                theme_mode=data.get("theme_mode"),
                                color_scheme=data.get("color_scheme"),
                                save=True,
                            )
                            self._post_to_window({"state": self.state.to_dict()})
                            return
                        last_posted: List[Dict[str, Any]] = []
                        orig_pm = bp.post_message

                        def _capturing_post(p: Any) -> None:
                            if isinstance(p, dict):
                                last_posted.append(p)
                                fb_lvl_post = str(((p.get("modal_feedback") or {}).get("level")) or "").lower()
                                if p.get("action") == "close_preprint_modal" or (
                                    mtype in ("postprocess_only", "postprocess_and_upload") and fb_lvl_post == "ok"
                                ):
                                    # Terminal success: never post a close_preprint_modal event to the
                                    # WebView right before/during native C++ window.close().
                                    return
                            orig_pm(p)

                        bp.post_message = _capturing_post  # type: ignore[method-assign]
                        try:
                            bp._handle_async_message(mtype, dict(data))
                        finally:
                            bp.post_message = orig_pm  # type: ignore[method-assign]
                        if mtype in ("postprocess_only", "postprocess_and_upload") and last_posted:
                            fb_lvl = str(((last_posted[-1].get("modal_feedback") or {}).get("level")) or "").lower()
                            if fb_lvl == "ok":
                                self._repatch_ctx_source_if_needed()
                                self.finish("proceed")

            threading.Thread(target=_handle_modal_action, daemon=True).start()

    def finish(self, reason: str = "") -> None:
        if reason and self.outcome == "pending":
            self.outcome = reason
        self.done_evt.set()

    def close(self, reason: str = "") -> None:
        self.finish(reason)
        with self._post_lock:
            self._closed = True
            win = self.window
        if win is not None:
            is_open_fn = getattr(win, "is_open", None)
            if callable(is_open_fn):
                try:
                    if not is_open_fn():
                        return
                except Exception:
                    pass
            close_fn = getattr(win, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    def run(self) -> str:
        if str(getattr(self.ctx, "host", "") or "").strip().lower() == "file":
            return "skipped_file_host"
        ui_mod = getattr(getattr(orca, "host", None), "ui", None)
        create_fn = getattr(ui_mod, "create_window", None) if ui_mod is not None else None
        if not callable(create_fn):
            return "unsupported"

        with _ACTIVE_MODAL_SESSIONS_LOCK:
            _ACTIVE_MODAL_SESSIONS.add(self)
        try:
            style_val = getattr(ui_mod, "WINDOW_MODAL", None)
            modality_enum = getattr(ui_mod, "WindowModality", None) or getattr(orca, "WindowModality", None)
            modality = getattr(modality_enum, "WINDOW_MODAL", None) if modality_enum is not None else None
            standalone_html = (
                ACE_PLUGIN_HTML_UI
                .replace("<body>", '<body class="standalone-modal">', 1)
                .replace('<div class="app-shell">', '<div class="app-shell" hidden style="display:none;">', 1)
                .replace(
                    '<div id="prePrintModal" class="modal-backdrop"',
                    '<div id="prePrintModal" class="modal-backdrop open"',
                    1,
                )
            )
            base_kwargs: Dict[str, Any] = {
                "title": f"{PLUGIN_SHORT_NAME} — {t('ui.modal.preprint.title', lang=self.state.language)}",
                "html": standalone_html,
                "on_message": self.on_message,
            }
            full_kwargs: Dict[str, Any] = dict(base_kwargs)
            full_kwargs["width"] = 980
            full_kwargs["height"] = 740
            full_kwargs["on_close"] = lambda: self.finish("cancelled")
            if style_val is not None:
                full_kwargs["style"] = style_val
            elif modality is not None:
                full_kwargs["modality"] = modality
            try:
                self.window = create_fn(**full_kwargs)
            except TypeError:
                self.window = create_fn(**base_kwargs)

            deadline = time.monotonic() + self.ready_timeout_s
            while not self.ready_evt.is_set() and not self.done_evt.is_set():
                if self.is_ctx_cancelled():
                    self.finish("cancelled")
                    return "cancelled"
                if time.monotonic() >= deadline:
                    self.finish("ready_timeout")
                    return "ready_timeout"
                self.ready_evt.wait(timeout=min(self.poll_interval_s, max(0.01, deadline - time.monotonic())))

            while not self.done_evt.is_set():
                if self.is_ctx_cancelled():
                    self.finish("cancelled")
                    return "cancelled"
                self.done_evt.wait(timeout=self.poll_interval_s)

            return self.outcome
        finally:
            self.done_evt.set()
            with self._work_lock:
                self.close()
            with _ACTIVE_MODAL_SESSIONS_LOCK:
                _ACTIVE_MODAL_SESSIONS.discard(self)


def _abort_pipeline_result(ctx: Any, message: str) -> Any:
    """
    Abort an intercepted slicing-pipeline modal step so OrcaSlicer stops the
    pending upload/print instead of continuing with the raw or unconfirmed job.
    In OrcaSlicer 2.5+, `ExecutionResult.skipped(...)` lets PrintHost upload and
    print anyway; returning `ExecutionResult.failure(PluginResult.FatalError, ...)`
    is required to abort the send/print pipeline.
    """
    cancelled_factory = getattr(orca.ExecutionResult, "cancelled", None)
    if callable(cancelled_factory):
        try:
            return cancelled_factory(message)
        except TypeError:
            return cancelled_factory()
    cancel_fn = getattr(ctx, "cancel", None) if ctx is not None else None
    if callable(cancel_fn):
        try:
            cancel_fn()
        except Exception:
            pass
    fatal_status = getattr(getattr(orca, "PluginResult", None), "FatalError", None)
    failure_factory = getattr(orca.ExecutionResult, "failure", None)
    if fatal_status is not None and callable(failure_factory):
        return failure_factory(fatal_status, message)
    return orca.ExecutionResult.skipped(message)


def _close_all_active_modal_sessions(reason: str = "unloaded") -> None:
    with _ACTIVE_MODAL_SESSIONS_LOCK:
        sessions = list(_ACTIVE_MODAL_SESSIONS)
    for sess in sessions:
        try:
            sess.close(reason=reason)
        except Exception:
            pass


def _refresh_ace_status_on_bounds_error(state: PluginState) -> Optional[Dict[str, Any]]:
    """Probe Klipper/Moonraker once (capped timeout) when slot bounds fail during slicing/export."""
    with state._lock:
        has_url = bool(str(state.moonraker_url or "").strip())
        expected_epoch = state.profile_epoch
    if not has_url:
        return None
    client = state.get_client()
    orig_timeout = getattr(client, "timeout", 5.0)
    try:
        try:
            client.timeout = min(float(orig_timeout or 5.0), 1.5)
        except Exception:
            pass
        live_st = client.query_ace_status()
        if not isinstance(live_st, dict):
            return None
        live_st["configured"] = True
        live_st["connection_state"] = (
            "connected_active" if live_st.get("is_printing") else "connected_standby"
        )
        raw_det = live_st.get("ace_instances") or live_st.get("ace_count") or 0
        det_cnt = int(raw_det) if raw_det else None
        state._commit_ace_status(live_st, expected_epoch, detected_ace_count=det_cnt)
        return live_st
    except Exception:
        return None
    finally:
        try:
            client.timeout = orig_timeout
        except Exception:
            pass


class KobraS1AcePostProcessCapability(orca.slicing.SlicingPipelineCapabilityBase):
    """
    OrcaSlicer 2.5.x Slicing Pipeline Post-Processing Hook (`psGCodePostProcess`).
    Automatically patches the generated `.gcode` or `.gcode.3mf` file after slicing/export,
    recording provenance and handling cancellation/host modes safely.
    """

    def get_name(self) -> str:
        return PIPELINE_CAPABILITY_NAME

    def on_load(self) -> None:
        GLOBAL_STATE.cache_orca_storage_dir()
        GLOBAL_STATE.refresh_host_ui_snapshot()
        GLOBAL_STATE.load_config(cap=self)
        _schedule_pipeline_registration(GLOBAL_STATE, use_thread=True)

    def on_unload(self) -> None:
        GLOBAL_STATE.flush_user_backup()
        _close_all_active_modal_sessions(reason="unloaded")

    def on_lifecycle_event(self, event: Any, ctx: Any) -> None:
        GLOBAL_STATE.cache_orca_storage_dir()
        GLOBAL_STATE.refresh_host_ui_snapshot()
        _handle_pipeline_lifecycle_event(event, ctx, GLOBAL_STATE, use_thread=True)

    def execute(self, ctx: Any) -> Any:
        step = getattr(ctx, "step", None)
        expected_step = getattr(orca.slicing.Step, "psGCodePostProcess", "psGCodePostProcess")
        if step is not None and step != expected_step:
            return orca.ExecutionResult.success()

        cancelled_fn = getattr(ctx, "cancelled", None)
        if callable(cancelled_fn) and cancelled_fn():
            return _abort_pipeline_result(ctx, "Cancelled before post-processing")

        # Cache storage inside permitted capability callback ONLY if still empty, then prevent
        # any subsequent plater()/model()/preset_bundle() UI calls during slicing execution.
        GLOBAL_STATE.cache_orca_storage_dir(allow_in_worker=True)

        gcode_path = getattr(ctx, "gcode_path", None)
        if not gcode_path or not os.path.isfile(str(gcode_path)):
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                f"G-code file not found: {gcode_path}",
            )

        output_name = str(getattr(ctx, "output_name", "") or "").strip()
        host_name = str(getattr(ctx, "host", "") or "").strip()
        plate_idx = _normalize_plate_index(getattr(ctx, "plate_index", getattr(ctx, "plate", None)))
        project_name_ctx = str(getattr(ctx, "project_name", getattr(ctx, "model_name", "")) or "").strip()

        # Guard against duplicate `slicing_pipeline_plugin` entries in a user preset / .3mf project
        # invoking execute() twice on the exact same already-patched export file.
        try:
            src_real_chk = os.path.realpath(str(gcode_path))
            st_chk = os.stat(str(gcode_path))
            with GLOBAL_STATE._lock:
                last_exec = getattr(GLOBAL_STATE, "_last_completed_pipeline_exec", None)
            if (
                isinstance(last_exec, tuple)
                and len(last_exec) == 5
                and last_exec[0] == src_real_chk
                and last_exec[1] == host_name.lower()
                and last_exec[2] == int(st_chk.st_mtime_ns)
                and last_exec[3] == int(st_chk.st_size)
                and (time.monotonic() - float(last_exec[4])) < 15.0
            ):
                return orca.ExecutionResult.success(
                    f"{PLUGIN_SHORT_NAME} already processed this export pass."
                )
        except OSError:
            pass

        with _WorkerThreadScope():
            run_id_val = ""
            try:
                seq_val, run_id_val = GLOBAL_STATE.next_slicing_run_id()
                src_str = str(gcode_path)
                _record_diagnostic_load_event(
                    GLOBAL_STATE,
                    trigger="execute",
                    decision="started",
                    reason="postprocess_pipeline_started",
                    file_source="slicing_pipeline",
                    file_path=src_str,
                    slicing_run_id=run_id_val,
                    compute_sha=False,
                )
                try:
                    info, patched_lines, parsed = _postprocess_gcode_file_impl(
                        src_str,
                        GLOBAL_STATE,
                        output_name=output_name,
                        plate_index=plate_idx,
                    )
                except SlotBoundsError as bounds_exc:
                    live_st = _refresh_ace_status_on_bounds_error(GLOBAL_STATE)
                    resolved_after_live = False
                    active_bounds_err = bounds_exc
                    if live_st is not None:
                        try:
                            info, patched_lines, parsed = _postprocess_gcode_file_impl(
                                src_str,
                                GLOBAL_STATE,
                                output_name=output_name,
                                plate_index=plate_idx,
                                ace_status=live_st,
                            )
                            resolved_after_live = True
                        except SlotBoundsError as bounds_exc2:
                            active_bounds_err = bounds_exc2
                    if not resolved_after_live:
                        if host_name.lower() == "file":
                            raise active_bounds_err
                        parsed = _read_validated_gcode_file(
                            src_str,
                            plate_index=plate_idx,
                            output_name=output_name,
                        )
                        patched_lines = list(parsed["lines"])
                        info = preview_gcode_file(
                            src_str,
                            GLOBAL_STATE,
                            extra_slice_info=parsed.get("slice_info") or {},
                            plate_index=plate_idx if plate_idx is not None else parsed.get("plate_index"),
                            output_name=output_name,
                            pre_lines=patched_lines,
                        )
                        info["deferred_bounds_errors"] = str(active_bounds_err)
                        with GLOBAL_STATE._lock:
                            GLOBAL_STATE.last_postprocess_info = dict(info)
                if callable(cancelled_fn) and cancelled_fn():
                    _clear_active_job_fields(GLOBAL_STATE)
                    GLOBAL_STATE.save_config()
                    return _abort_pipeline_result(ctx, "Cancelled during post-processing")

                staged_path = _stage_gcode_file_to_storage(
                    src_str,
                    GLOBAL_STATE,
                    output_name=output_name or os.path.basename(src_str),
                    plate_index=plate_idx if plate_idx is not None else parsed.get("plate_index"),
                    host_name=host_name,
                    slicing_run_id=run_id_val,
                    slicing_run_seq=seq_val,
                    project_model=project_name_ctx,
                    source_kind="slicing_pipeline",
                    pre_extracted=parsed if parsed.get("is_container") else None,
                    pre_lines=patched_lines,
                    allow_overwrite_in_staged=True,
                )
                if not staged_path:
                    _clear_active_job_fields(GLOBAL_STATE)
                    GLOBAL_STATE.save_config()
                    _record_diagnostic_load_event(
                        GLOBAL_STATE,
                        trigger="execute",
                        decision="error",
                        reason="staging_failed_cleared_active_job",
                        file_source="slicing_pipeline",
                        file_path=src_str,
                        slicing_run_id=run_id_val,
                        compute_sha=False,
                    )
                    if host_name.lower() != "file":
                        with _ACTIVE_PAGE_LOCK:
                            page = _ACTIVE_PAGE_CAPABILITY
                        _trigger_auto_preprint_popup(
                            page,
                            "presets_only",
                            "",
                            None,
                            use_thread=True,
                        )
                    return orca.ExecutionResult.failure(
                        orca.PluginResult.RecoverableError,
                        f"{PLUGIN_SHORT_NAME} could not stage sliced G-code '{os.path.basename(src_str)}'.",
                    )

                effective_path = staged_path
                storage_dir_exec = GLOBAL_STATE.resolve_storage_dir()
                try:
                    src_real_exec = os.path.realpath(src_str) if src_str else ""
                    staged_real_exec = os.path.realpath(staged_path) if staged_path else ""
                    is_preview_exec = bool(
                        host_name.lower() != "file"
                        and src_str
                        and src_real_exec != staged_real_exec
                        and _is_preview_temp_source(src_str, storage_dir_exec)
                        and os.path.isfile(src_str)
                    )
                except OSError:
                    is_preview_exec = False
                with GLOBAL_STATE._lock:
                    GLOBAL_STATE.last_gcode_path = staged_path
                    GLOBAL_STATE.last_gcode_source = "slicing_pipeline"
                    GLOBAL_STATE.cleared_before_mtime_ns = 0
                    GLOBAL_STATE._last_pipeline_export_ns = time.time_ns()
                    GLOBAL_STATE._pipeline_active_since_slice = True
                    GLOBAL_STATE._pending_preview_gcode = {}
                    exec_prov = GLOBAL_STATE._last_staged_provenance if isinstance(GLOBAL_STATE._last_staged_provenance, dict) else {}
                    if exec_prov:
                        GLOBAL_STATE._last_slice_satisfied_run_id = str(exec_prov.get("slicing_run_id") or "")
                        GLOBAL_STATE._last_slice_satisfied_created_ns = int(exec_prov.get("created_ns", 0) or 0)
                    if is_preview_exec:
                        GLOBAL_STATE._last_preview_temp_path = src_str
                        GLOBAL_STATE._last_preview_temp_existed = True
                    elif _is_ephemeral_upload_temp_source(src_str):
                        existing_prev_exec = str(getattr(GLOBAL_STATE, "_last_preview_temp_path", "") or "").strip()
                        if (
                            existing_prev_exec
                            and not _is_ephemeral_upload_temp_source(existing_prev_exec)
                            and os.path.isfile(existing_prev_exec)
                        ):
                            GLOBAL_STATE._last_preview_temp_path = existing_prev_exec
                            GLOBAL_STATE._last_preview_temp_existed = True
                        else:
                            GLOBAL_STATE._last_preview_temp_path = ""
                            GLOBAL_STATE._last_preview_temp_existed = False
                    else:
                        GLOBAL_STATE._last_preview_temp_path = ""
                        GLOBAL_STATE._last_preview_temp_existed = False
                    if isinstance(GLOBAL_STATE.last_job_metadata, dict):
                        if output_name:
                            GLOBAL_STATE.last_job_metadata["output_name"] = _sanitize_staged_gcode_filename(
                                output_name,
                                plate_index=plate_idx if plate_idx is not None else parsed.get("plate_index"),
                            )
                        eff_plate = plate_idx if plate_idx is not None else parsed.get("plate_index")
                        if eff_plate is not None:
                            GLOBAL_STATE.last_job_metadata["plate_index"] = eff_plate
                GLOBAL_STATE.save_config()
                _record_slice_host_fingerprint(GLOBAL_STATE)
                _record_diagnostic_load_event(
                    GLOBAL_STATE,
                    trigger="execute",
                    decision="accepted",
                    reason="postprocess_and_staged",
                    file_source="slicing_pipeline",
                    file_path=effective_path,
                    slicing_run_id=run_id_val,
                    sidecar=GLOBAL_STATE._last_staged_provenance or None,
                    compute_sha=True,
                )

                # File export only (`ctx.host == "File"`): patch & stage cleanly without print modal.
                if host_name.lower() == "file":
                    return orca.ExecutionResult.success(
                        f"{PLUGIN_SHORT_NAME} exported {info['toolchanges']} toolchanges."
                    )

                with _ACTIVE_PAGE_LOCK:
                    page = _ACTIVE_PAGE_CAPABILITY
                modal_mode = str(getattr(GLOBAL_STATE, "slicing_modal_mode", "window_modal") or "window_modal").lower()
                if modal_mode == "window_modal":
                    session_res = _SlicingModalSetupSession(
                        ctx, GLOBAL_STATE, effective_path, info, source_ctx_path=src_str
                    ).run()
                    if session_res == "uploaded_by_plugin":
                        return _abort_pipeline_result(
                            ctx,
                            f"{PLUGIN_SHORT_NAME} uploaded and started job directly.",
                        )
                    if session_res in ("cancelled", "unloaded", "ready_timeout"):
                        abort_msg = (
                            "Print setup timed out waiting for plugin window — print was not started."
                            if session_res == "ready_timeout"
                            else (
                                "Print setup aborted because plugin was unloaded — print was not started."
                                if session_res == "unloaded"
                                else "Print setup cancelled by user — print was not started."
                            )
                        )
                        return _abort_pipeline_result(ctx, abort_msg)
                    if session_res in ("proceed", "skipped_file_host"):
                        try:
                            st_done = os.stat(src_str)
                            with GLOBAL_STATE._lock:
                                GLOBAL_STATE._last_completed_pipeline_exec = (
                                    os.path.realpath(src_str),
                                    host_name.lower(),
                                    int(st_done.st_mtime_ns),
                                    int(st_done.st_size),
                                    time.monotonic(),
                                )
                        except OSError:
                            pass
                        return orca.ExecutionResult.success(
                            f"{PLUGIN_SHORT_NAME} post-processed {info['toolchanges']} toolchanges."
                        )

                _trigger_auto_preprint_popup(
                    page,
                    "slicing_pipeline",
                    effective_path,
                    info,
                    use_thread=True,
                )
                return orca.ExecutionResult.success(
                    f"{PLUGIN_SHORT_NAME} post-processed {info['toolchanges']} toolchanges."
                )
            except Exception as exc:
                _clear_active_job_fields(GLOBAL_STATE)
                try:
                    GLOBAL_STATE.save_config()
                except Exception:
                    pass
                _record_diagnostic_load_event(
                    GLOBAL_STATE,
                    trigger="execute",
                    decision="error",
                    reason=f"execute_exception:{exc}",
                    file_source="slicing_pipeline",
                    file_path=str(gcode_path),
                    slicing_run_id=run_id_val,
                    compute_sha=False,
                )
                if host_name.lower() != "file":
                    with _ACTIVE_PAGE_LOCK:
                        page = _ACTIVE_PAGE_CAPABILITY
                    _trigger_auto_preprint_popup(
                        page,
                        "presets_only",
                        "",
                        None,
                        use_thread=True,
                    )
                return orca.ExecutionResult.failure(
                    orca.PluginResult.RecoverableError,
                    f"{PLUGIN_SHORT_NAME} post-processing failed: {exc}",
                )


def _slicer_model_temp_read_grants() -> List[str]:
    """
    Build narrow tree `fs_read` grants strictly for OrcaSlicer's model backup folder
    (`<temp>/orcaslicer_<uid>/orcaslicer_model/*` and `<temp>/orcaslicer_model/*`),
    where `PartPlate::get_tmp_gcode_path()` writes preview G-code delivered via
    `GCodeExportFinished.ctx.msg`. Never requests broad `/tmp/*`.
    """
    out: List[str] = []
    try:
        tmp_base = tempfile.gettempdir().rstrip("/\\") or os.sep
    except Exception:
        tmp_base = "C:\\Windows\\Temp" if os.name == "nt" else "/tmp"
    bases = [tmp_base]
    if os.name == "nt":
        la_tmp = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp").rstrip("/\\")
        if la_tmp and la_tmp not in bases:
            bases.append(la_tmp)
    else:
        if "/tmp" not in bases:
            bases.append("/tmp")
    uid_dir: Optional[str] = None
    if os.name != "nt" and hasattr(os, "getuid"):
        try:
            uid_dir = f"orcaslicer_{os.getuid()}"
        except Exception:
            uid_dir = None
    candidate_roots: List[str] = []
    for base in bases:
        if uid_dir and os.path.basename(base.rstrip("/\\")) != uid_dir:
            candidate_roots.append(os.path.join(base, uid_dir, "orcaslicer_model"))
        candidate_roots.append(os.path.join(base, "orcaslicer_model"))
    for root in candidate_roots:
        variants = [root]
        try:
            rp = os.path.realpath(root)
            if rp and rp not in variants:
                variants.insert(0, rp)
        except OSError:
            pass
        for form in variants:
            clean_form = form.rstrip("/\\")
            if not clean_form:
                continue
            target = f"{clean_form}{os.sep}*" if os.name == "nt" else f"{clean_form}/*"
            if target not in out:
                out.append(target)
    return out


@orca.plugin
class KobraS1AcePlugin(orca.base):
    """Main OrcaSlicer 2.5.x Plugin Package Entry Point."""

    def register_capabilities(self) -> None:
        # Cache storage folder and UI snapshot inside registration callback BEFORE
        # spawning capabilities/threads so workers never call plugin.storage() directly.
        GLOBAL_STATE.cache_orca_storage_dir()
        GLOBAL_STATE.refresh_host_ui_snapshot()
        # Load persisted config first so any custom Moonraker host/port is included
        # when requesting upfront network permissions.
        GLOBAL_STATE.load_config()
        # Request upfront host-based permissions for Moonraker/Spoolman HTTP requests,
        # narrow read-only tree access for OrcaSlicer's preview G-code folder (`orcaslicer_model/*`),
        # and background threading.
        allowed_urls: List[str] = []
        allowed_urls.extend(_build_host_permission_urls(GLOBAL_STATE.moonraker_url, include_port80=True))
        allowed_urls.extend(_build_host_permission_urls(getattr(GLOBAL_STATE, "spoolman_url", ""), include_port80=False))
        for prof in getattr(GLOBAL_STATE, "printer_profiles", None) or []:
            if isinstance(prof, dict):
                allowed_urls.extend(_build_host_permission_urls(prof.get("moonraker_url", ""), include_port80=True))
                allowed_urls.extend(_build_host_permission_urls(prof.get("spoolman_url", ""), include_port80=False))
        unique_urls = list(dict.fromkeys(allowed_urls))
        preview_read_grants = _slicer_model_temp_read_grants()

        try:
            orca.request_permissions(
                fs_read=preview_read_grants,
                network_http=unique_urls,
                threading=True,
            )
        except TypeError:
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


# Public Kobra-Van-Kenorca & KobraOrca class aliases (backwards-compatible with KobraS1Ace* capability symbols)
KobraOrcaPageCapability = KobraS1AcePageCapability
KobraOrcaPostProcessCapability = KobraS1AcePostProcessCapability
KobraOrcaPlugin = KobraS1AcePlugin
KobraVanKenorcaPageCapability = KobraS1AcePageCapability
KobraVanKenorcaPostProcessCapability = KobraS1AcePostProcessCapability
KobraVanKenorcaPlugin = KobraS1AcePlugin


# 14. Standalone CLI Support
def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or "-h" in args or "--help" in args:
        print(
            "Usage: kobra_van_kenorca_plugin.py <gcode_file> "
            "[--diameter 1.75] [--round 2] [--tool-map 0:0,1:1] "
            "[--no-bed-leveling] [--skip-purge|--no-skip-purge] [--cut-tip-post] [--force-initial-purge] "
            "[--upload [http://printer.local:7125]] [--start-print]"
        )
        return 0 if ("-h" in args or "--help" in args) else 2

    filament_diameter = float(DEFAULT_FILAMENT_DIAMETER)
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
        print(f"[Kobra-Van-Kenorca] ERROR: G-code file not found: {gcode_path}", file=sys.stderr)
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
        f"[Kobra-Van-Kenorca] OK: {os.path.basename(gcode_path)} "
        f"(toolchanges={info['toolchanges']}, multiplier={info['flush_multiplier']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
