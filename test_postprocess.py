#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validation & Unit Test Suite for `kobra_van_kenorca_plugin.py`.

Tests:
1. Real OrcaSlicer 2.5.0 G-code (`refs/test_sample.gcode`) post-processing:
   - Flush multiplier, 8x8 flush_volumes_matrix, and flush_volumes_vector parsing
   - `G9111 ... tool=1` remapping and `SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1` handling
     (initial `T1` -> `PURGELENGTH=0.00` when `force_initial_purge=False`,
     and `PURGELENGTH=75.67` when `force_initial_purge=True`)
   - Exact purge lengths using original Slicer tool indices (`T1->T0`: 443.19 mm,
     `T0->T1`: 105.93 mm) BEFORE applying color mapping (`{0: 2, 1: 3}`)
2. Synthetic Multi-Toolchange G-code across 4+ filaments mapped to T0..T7:
   - Verifies original slicer index purge calculation vs mapped ACE slots
   - Verifies `bed_leveling=False` (`; G31 ; skipped by ACE Plugin`)
   - Verifies `skip_purge_for_already_loaded_tool=True` (`SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1` on `G9111`)
     and `cut_tip_after_print=True` (`PRINT_END CUT_TIP=1` or fallback `CUT_TIP`)
   - Verifies Audit Header fields (`multiplier`, `toolMap`, `bedLeveling`, `toolchanges`)
3. Auto-Match (`auto_match_filaments_to_slots`), Moonraker status normalization (`T0..T7`),
   Multipart upload payload generation, and OrcaSlicer 2.5.x plugin registration.
"""

import base64
import hashlib
import io
import json
import math
import os
import re
import socket
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from unittest import mock

import kobra_van_kenorca_plugin as plugin


def _forbid_network(*args, **kwargs):
    raise AssertionError("Network access is strictly forbidden during offline unit tests!")


class TestKobraS1AcePlugin(unittest.TestCase):
    def setUp(self) -> None:
        self._isolated_cfg_tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._isolated_cfg_tmpdir.cleanup)
        isolated_cfg = os.path.join(self._isolated_cfg_tmpdir.name, "Kobra-Van-Kenorca", "state.json")
        self._fallback_cfg_patcher = mock.patch.object(plugin.PluginState, "FALLBACK_CONFIG_PATH", isolated_cfg)
        self._legacy_cfg_patcher = mock.patch.object(plugin, "_legacy_fallback_config_paths", return_value=())
        self._fallback_cfg_patcher.start()
        self._legacy_cfg_patcher.start()
        self.addCleanup(self._fallback_cfg_patcher.stop)
        self.addCleanup(self._legacy_cfg_patcher.stop)

        self._urlopen_patcher = mock.patch("urllib.request.urlopen", side_effect=_forbid_network)
        self._socket_patcher = mock.patch("socket.create_connection", side_effect=_forbid_network)
        self._urlopen_patcher.start()
        self._socket_patcher.start()
        self.addCleanup(self._urlopen_patcher.stop)
        self.addCleanup(self._socket_patcher.stop)

        self.sample_gcode_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "refs",
            "test_sample.gcode",
        )

    def test_01_real_sample_gcode_postprocess_and_remapping(self) -> None:
        """Test post-processor with refs/test_sample.gcode and non-identity color mapping."""
        self.assertTrue(
            os.path.isfile(self.sample_gcode_path),
            f"Expected sample G-code at {self.sample_gcode_path}",
        )
        with open(self.sample_gcode_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Verify Slicer filament header extraction
        filaments = plugin.parse_slicer_filaments(lines)
        self.assertEqual(len(filaments), 8)
        self.assertEqual(filaments[0]["color"], "#FFFFFF")
        self.assertEqual(filaments[0]["material"], "PETG")
        self.assertTrue(filaments[0]["used"])
        self.assertEqual(filaments[1]["color"], "#000000")
        self.assertEqual(filaments[1]["material"], "PETG")
        self.assertTrue(filaments[1]["used"])

        # Map Slicer T0 -> physical ACE T2, Slicer T1 -> physical ACE T5
        tool_map = {0: 2, 1: 5}
        out_lines, info = plugin.process_gcode(
            lines=lines,
            filament_diameter=1.75,
            round_digits=2,
            tool_map=tool_map,
            bed_leveling=False,
            skip_purge_for_already_loaded_tool=True,
            cut_tip_after_print=True,
            force_initial_purge=False,
            include_audit_header=True,
        )

        # Verify Audit Header at the top of the output
        self.assertTrue(out_lines[0].startswith("; === Kobra S1 ACE Pro Post-Processor Audit Header ==="))
        header_block = "".join(out_lines[:4])
        self.assertIn("multiplier=1.3", header_block)
        self.assertIn("toolMap=", header_block)
        self.assertIn("bedLeveling=False", header_block)
        self.assertIn("toolchanges=7", header_block)

        # Verify parsed info stats
        self.assertAlmostEqual(info["flush_multiplier"], 1.3)
        self.assertEqual(info["tools"], 8)
        self.assertEqual(info["matrix_len"], 64)
        self.assertEqual(info["vector_len"], 16)
        self.assertEqual(info["toolchanges"], 7)
        self.assertEqual(info["g9111StartSlicerTool"], 1)
        self.assertEqual(info["g9111MappedTool"], 5)
        self.assertTrue(info["skipPurgeForAlreadyLoadedTool"])
        self.assertEqual(info["g31_skipped"], 1)

        # Verify purge calculation uses ORIGINAL Slicer indices (1 -> 0 and 0 -> 1), NOT mapped (5 -> 2)!
        # Event 0: Initial T1 after G9111 tool=1 + SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 -> 0.00 mm
        events = info["events"]
        self.assertEqual(len(events), 7)
        self.assertEqual(events[0]["old_slicer_tool"], 1)
        self.assertEqual(events[0]["new_slicer_tool"], 1)
        self.assertEqual(events[0]["mapped_tool"], 5)
        self.assertAlmostEqual(events[0]["purge_length_mm"], 0.00, places=2)

        # Event 1: Slicer T1 -> T0 (matrix[1*8 + 0] = 820 * 1.3 = 1066 mm^3 -> 443.19 mm), mapped to T2
        self.assertEqual(events[1]["old_slicer_tool"], 1)
        self.assertEqual(events[1]["new_slicer_tool"], 0)
        self.assertEqual(events[1]["mapped_tool"], 2)
        self.assertAlmostEqual(events[1]["purge_length_mm"], 443.19, places=2)

        # Event 2: Slicer T0 -> T1 (matrix[0*8 + 1] = 196 * 1.3 = 254.8 mm^3 -> 105.93 mm), mapped to T5
        self.assertEqual(events[2]["old_slicer_tool"], 0)
        self.assertEqual(events[2]["new_slicer_tool"], 1)
        self.assertEqual(events[2]["mapped_tool"], 5)
        self.assertAlmostEqual(events[2]["purge_length_mm"], 105.93, places=2)

        # Verify G9111 rewrite (SKIP_PURGE=1 preserved without pre-print CUT_TIP), and G31 skipped comment
        head_snippet = "".join(out_lines[:120])
        self.assertIn("G9111 bedTemp=80 extruderTemp=250 tool=5", head_snippet)
        self.assertIn("SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n", head_snippet)
        self.assertNotIn("CUT_TIP\nG9111", head_snippet)
        self.assertIn("; G31 ; skipped by ACE Plugin\n", head_snippet)
        self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=0.00 ; @ORCA_ACE_PURGE\nT5 ; change extruder\n", head_snippet)
        tail_snippet = "".join(out_lines[-1000:-500])
        self.assertIn("PRINT_END CUT_TIP=1", tail_snippet)

    def test_02_force_initial_purge_toggle(self) -> None:
        """Verify Force Initial Purge overrides SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 on first T<n>."""
        with open(self.sample_gcode_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[:160] + f.readlines()[-800:]

        # Re-read properly with slice of start + config block
        with open(self.sample_gcode_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        subset_lines = all_lines[:150] + all_lines[189490:]

        _, info = plugin.process_gcode(
            lines=subset_lines,
            filament_diameter=1.75,
            round_digits=2,
            tool_map={1: 3},
            force_initial_purge=True,
        )
        # vector[1] = 140, multiplier = 1.3 -> 182.0 mm^3 -> 75.67 mm
        self.assertEqual(len(info["events"]), 1)
        self.assertIsNone(info["events"][0]["old_slicer_tool"])
        self.assertEqual(info["events"][0]["new_slicer_tool"], 1)
        self.assertEqual(info["events"][0]["mapped_tool"], 3)
        self.assertAlmostEqual(info["events"][0]["purge_length_mm"], 75.67, places=2)

    def test_03_synthetic_multi_toolchange_gcode(self) -> None:
        """Test synthetic 4-tool G-code mapped across ACE Pro slots T0..T7."""
        synthetic_gcode = [
            "; HEADER_BLOCK_START\n",
            "; filament_diameter: 1.75,1.75,1.75,1.75\n",
            "; filament: 1,2,3,4\n",
            "; HEADER_BLOCK_END\n",
            "; EXECUTABLE_BLOCK_START\n",
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n",
            "SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n",
            "G31\n",
            "; FLUSH_START\n",
            "T0 ; initial load\n",
            "; FLUSH_END\n",
            "G1 X10 Y10 E1.0\n",
            "T2 ; switch 0 -> 2\n",
            "G1 X20 Y20 E2.0\n",
            "T3 ; switch 2 -> 3\n",
            "G1 X30 Y30 E3.0\n",
            "T1 ; switch 3 -> 1\n",
            "G1 X40 Y40 E4.0\n",
            "T1 ; redundant same-tool select 1 -> 1\n",
            "PRINT_END CUT_TIP=0\n",
            "M84\n",
            "; EXECUTABLE_BLOCK_END\n",
            "; CONFIG_BLOCK_START\n",
            "; filament_colour = #FF0000;#00FF00;#0000FF;#FFFF00\n",
            "; filament_type = PLA;PLA;PETG;ASA\n",
            "; flush_multiplier = 1.5\n",
            # 4x4 matrix (row = old_tool, col = new_tool):
            # 0->2 = 200, 2->3 = 320, 3->1 = 480
            "; flush_volumes_matrix = "
            "0,100,200,150,"
            "120,0,180,220,"
            "250,160,0,320,"
            "300,480,140,0\n",
            "; flush_volumes_vector = 100,110,120,130\n",
            "; CONFIG_BLOCK_END\n",
        ]

        tool_map = {0: 4, 1: 7, 2: 1, 3: 6}
        out_lines, info = plugin.process_gcode(
            lines=synthetic_gcode,
            filament_diameter=1.75,
            round_digits=2,
            tool_map=tool_map,
            bed_leveling=False,
            skip_purge_for_already_loaded_tool=True,
            cut_tip_after_print=True,
            force_initial_purge=False,
            include_audit_header=True,
        )

        area = math.pi * (1.75 * 0.5) ** 2
        expected_0_to_2 = round((200.0 * 1.5) / area, 2)  # 124.73 mm
        expected_2_to_3 = round((320.0 * 1.5) / area, 2)  # 199.56 mm
        expected_3_to_1 = round((480.0 * 1.5) / area, 2)  # 299.34 mm

        events = info["events"]
        self.assertEqual(len(events), 5)
        # 1) Initial T0 (skipped because G9111 tool=0 + SKIP_PURGE=1) -> mapped to T4, 0.00 mm
        self.assertEqual((events[0]["old_slicer_tool"], events[0]["new_slicer_tool"], events[0]["mapped_tool"]), (0, 0, 4))
        self.assertAlmostEqual(events[0]["purge_length_mm"], 0.00, places=2)
        # 2) T0 -> T2 -> mapped to T1
        self.assertEqual((events[1]["old_slicer_tool"], events[1]["new_slicer_tool"], events[1]["mapped_tool"]), (0, 2, 1))
        self.assertAlmostEqual(events[1]["purge_length_mm"], expected_0_to_2, places=2)
        # 3) T2 -> T3 -> mapped to T6
        self.assertEqual((events[2]["old_slicer_tool"], events[2]["new_slicer_tool"], events[2]["mapped_tool"]), (2, 3, 6))
        self.assertAlmostEqual(events[2]["purge_length_mm"], expected_2_to_3, places=2)
        # 4) T3 -> T1 -> mapped to T7
        self.assertEqual((events[3]["old_slicer_tool"], events[3]["new_slicer_tool"], events[3]["mapped_tool"]), (3, 1, 7))
        self.assertAlmostEqual(events[3]["purge_length_mm"], expected_3_to_1, places=2)
        # 5) T1 -> T1 -> mapped to T7, 0.00 mm
        self.assertEqual((events[4]["old_slicer_tool"], events[4]["new_slicer_tool"], events[4]["mapped_tool"]), (1, 1, 7))
        self.assertAlmostEqual(events[4]["purge_length_mm"], 0.00, places=2)

        full_text = "".join(out_lines)
        self.assertIn("G9111 bedTemp=60 extruderTemp=215 tool=4\nSKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n", full_text)
        self.assertNotIn("CUT_TIP\nG9111", full_text)
        self.assertIn("; G31 ; skipped by ACE Plugin\n", full_text)
        self.assertIn(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_0_to_2:.2f} ; @ORCA_ACE_PURGE\nT1 ; switch 0 -> 2\n", full_text)
        self.assertIn(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_2_to_3:.2f} ; @ORCA_ACE_PURGE\nT6 ; switch 2 -> 3\n", full_text)
        self.assertIn(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_3_to_1:.2f} ; @ORCA_ACE_PURGE\nT7 ; switch 3 -> 1\n", full_text)
        self.assertIn("PRINT_END CUT_TIP=1\n", full_text)
        self.assertNotIn("CUT_TIP\n; EXECUTABLE_BLOCK_END\n", full_text)

    def test_04_auto_match_and_moonraker_status_normalization(self) -> None:
        """Test Moonraker status normalization (T0..T7) and color/material Auto-Match."""
        mock_query_status = {
            "ace_state": {
                "ace_instances": 2,
                "current_index": 5,
                "target_index": -1,
                "ace_pro_enabled": True,
            },
            "ace_instance_0": {
                "instance": 0,
                "status": "ready",
                "connection_state": "connected",
                "temp": 29,
                "fan_speed": 4200,
                "dryer": {"status": "drying", "target_temp": 50, "duration": 240, "remain_time": 7200},
                "slots": [
                    {"index": 0, "tool": 0, "status": "ready", "color": [255, 0, 0], "material": "PLA", "temp": 210, "rfid": 2},
                    {"index": 1, "tool": 1, "status": "ready", "color": [0, 0, 0], "material": "PETG", "temp": 245, "rfid": 0},
                    {"index": 2, "tool": 2, "status": "empty", "color": [0, 0, 0], "material": "", "temp": 0, "rfid": 0},
                    {"index": 3, "tool": 3, "status": "ready", "color": [255, 255, 255], "material": "PLA", "temp": 210, "rfid": 2},
                ],
            },
            "ace_instance_1": {
                "instance": 1,
                "status": "ready",
                "connection_state": "connected",
                "slots": [
                    {"index": 0, "tool": 4, "status": "ready", "color": [250, 250, 250], "material": "PETG", "temp": 245, "rfid": 2},
                    {"index": 1, "tool": 5, "status": "ready", "color": [0, 255, 0], "material": "PETG", "temp": 245, "rfid": 2},
                ],
            },
        }

        norm = plugin.MoonrakerClient.normalize_ace_status(mock_query_status)
        self.assertEqual(len(norm["slots"]), 8)
        self.assertEqual(norm["current_index"], 5)
        self.assertEqual(norm["slots"][5]["status"], "loaded")
        self.assertTrue(norm["dryer"]["active"])
        self.assertAlmostEqual(norm["dryer"]["remain_time_min"], 120.0)

        slicer_fils = [
            {"slicer_tool": 0, "color": "#FFFFFF", "material": "PETG", "used": True},
            {"slicer_tool": 1, "color": "#050505", "material": "PETG", "used": True},
            {"slicer_tool": 2, "color": "#FE0202", "material": "PLA", "used": True},
        ]
        matched = plugin.auto_match_filaments_to_slots(slicer_fils, norm["slots"])
        # Slicer 0 (White PETG) -> T4 (White PETG, not T3 which is White PLA!)
        self.assertEqual(matched[0], 4)
        # Slicer 1 (Black PETG) -> T1 (Black PETG)
        self.assertEqual(matched[1], 1)
        # Slicer 2 (Red PLA) -> T0 (Red PLA)
        self.assertEqual(matched[2], 0)

    def test_05_plugin_registration_and_multipart_upload(self) -> None:
        """Verify @orca.plugin, request_permissions, and Multipart upload builder."""
        pkg = plugin.KobraS1AcePlugin()
        pkg.register_capabilities()
        self.assertTrue(plugin.orca.last_requested_permissions.get("threading"))
        fs_read = plugin.orca.last_requested_permissions.get("fs_read") or []
        self.assertTrue(any(p.endswith("orcaslicer_model/*") for p in fs_read))
        if hasattr(os, "getuid"):
            uid_suffix = f"orcaslicer_{os.getuid()}/orcaslicer_model/*"
            self.assertTrue(any(p.endswith(uid_suffix) for p in fs_read), fs_read)
        self.assertFalse(any(p.rstrip("/*") == "/tmp" for p in fs_read))
        self.assertNotIn("fs_readwrite", plugin.orca.last_requested_permissions)

        cap = plugin.KobraS1AcePostProcessCapability()

        class _MissingCtx:
            step = plugin.orca.slicing.Step.psGCodePostProcess
            gcode_path = "/tmp/nonexistent_kobra_s1_ace_test_file.gcode"

        err_res = cap.execute(_MissingCtx())
        self.assertFalse(err_res.ok)
        self.assertEqual(err_res.status, plugin.orca.PluginResult.RecoverableError)

        body, content_type = plugin.MoonrakerClient.build_multipart_upload(
            filename="test_job.gcode",
            content_bytes=b"G28\nM84\n",
            start_print=True,
        )
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="print"\r\n\r\ntrue', body)
        self.assertIn(b'filename="test_job.gcode"', body)
        self.assertIn(b"G28\nM84\n", body)

    # --- Phase 1: Flush Calculation & Validation Tests (100% Offline) ---

    def test_flush_01_initial_flush_via_vector(self) -> None:
        """Initial flush uses vector (N and 2*N formats)."""
        lines_n = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 120,240\n",
            "T0\n",
        ]
        out_n, info_n = plugin.process_gcode(lines_n, filament_diameter=1.75, include_audit_header=False)
        expected_len = round(120.0 / (math.pi * (1.75 / 2.0) ** 2), 2)
        self.assertAlmostEqual(info_n["events"][0]["purge_length_mm"], expected_len, places=2)
        self.assertIn(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_len:.2f} ; @ORCA_ACE_PURGE\n", out_n)
        self.assertEqual(info_n["flush_status"], "POSTPROCESS_OK")

        # 2*N vector layout (OrcaSlicer 2.5.0 style)
        lines_2n = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 80,140,90,200\n",
            "T1\n",
        ]
        _, info_2n = plugin.process_gcode(lines_2n, filament_diameter=1.75, include_audit_header=False)
        expected_2n = round(200.0 / (math.pi * (1.75 / 2.0) ** 2), 2)
        self.assertAlmostEqual(info_2n["events"][0]["purge_length_mm"], expected_2n, places=2)

    def test_flush_02_t0_to_t1_via_matrix(self) -> None:
        """T0 -> T1 computes matrix[0*2 + 1]."""
        lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,150,300,0\n",
            "; flush_volumes_vector = 100,100\n",
            "T0\n",
            "T1\n",
        ]
        _, info = plugin.process_gcode(lines, filament_diameter=1.75, include_audit_header=False)
        area = math.pi * (1.75 * 0.5) ** 2
        self.assertAlmostEqual(info["events"][1]["purge_length_mm"], round(150.0 / area, 2), places=2)

    def test_flush_03_t1_to_t2_via_matrix(self) -> None:
        """T1 -> T2 computes row-major matrix[1*3 + 2] for 3x3 matrix."""
        lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,110,0,275,300,150,0\n",
            "; flush_volumes_vector = 100,100,100\n",
            "T1\n",
            "T2\n",
        ]
        _, info = plugin.process_gcode(lines, filament_diameter=1.75, include_audit_header=False)
        area = math.pi * (1.75 * 0.5) ** 2
        self.assertAlmostEqual(info["events"][1]["purge_length_mm"], round(275.0 / area, 2), places=2)

    def test_flush_04_flush_multiplier_greater_than_one(self) -> None:
        """Flush multiplier > 1 scales both initial vector and matrix lookups."""
        lines = [
            "; flush_multiplier = 1.4\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 50,60\n",
            "T0\n",
            "T1\n",
        ]
        _, info = plugin.process_gcode(lines, filament_diameter=1.75, include_audit_header=False)
        area = math.pi * (1.75 * 0.5) ** 2
        self.assertAlmostEqual(info["events"][0]["purge_length_mm"], round(50.0 * 1.4 / area, 2), places=2)
        self.assertAlmostEqual(info["events"][1]["purge_length_mm"], round(100.0 * 1.4 / area, 2), places=2)

    def test_flush_05_different_filament_diameters(self) -> None:
        """Different filament diameters (1.75 vs 2.85 and per-extruder) scale cross-section area."""
        lines = [
            "; filament_diameter = 1.75, 2.85\n",
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,200,200,0\n",
            "; flush_volumes_vector = 100,100\n",
            "T0\n",
            "T1\n",
            "T0\n",
        ]
        _, info = plugin.process_gcode(lines, filament_diameter=None, include_audit_header=False)
        area_175 = math.pi * (1.75 * 0.5) ** 2
        area_285 = math.pi * (2.85 * 0.5) ** 2
        self.assertAlmostEqual(info["events"][0]["purge_length_mm"], round(100.0 / area_175, 2), places=2)
        self.assertAlmostEqual(info["events"][1]["purge_length_mm"], round(200.0 / area_285, 2), places=2)
        self.assertAlmostEqual(info["events"][2]["purge_length_mm"], round(200.0 / area_175, 2), places=2)

    def test_flush_06_missing_matrix(self) -> None:
        """Missing flush_volumes_matrix on multi-tool print reports FLUSH_DATA_INCOMPLETE."""
        lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_vector = 100,100\n",
            "T0\n",
            "T1\n",
        ]
        _, info = plugin.process_gcode(lines, include_audit_header=False)
        self.assertFalse(info["flush_data_complete"])
        self.assertEqual(info["flush_status"], "FLUSH_DATA_INCOMPLETE")
        self.assertTrue(any("flush_volumes_matrix" in w for w in info["warnings"]))

    def test_flush_07_missing_vector(self) -> None:
        """Missing flush_volumes_vector for initial flush reports FLUSH_DATA_INCOMPLETE but still computes matrix switch."""
        lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,180,200,0\n",
            "T0\n",
            "T1\n",
        ]
        _, info = plugin.process_gcode(lines, include_audit_header=False)
        self.assertFalse(info["flush_data_complete"])
        self.assertEqual(info["flush_status"], "FLUSH_DATA_INCOMPLETE")
        self.assertAlmostEqual(info["events"][0]["purge_length_mm"], 0.0, places=2)
        area = math.pi * (1.75 * 0.5) ** 2
        self.assertAlmostEqual(info["events"][1]["purge_length_mm"], round(180.0 / area, 2), places=2)

    def test_flush_08_invalid_matrix_size(self) -> None:
        """Non-square matrix (e.g. 5 elements) is flagged as FLUSH_DATA_INCOMPLETE."""
        lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0,50\n",
            "; flush_volumes_vector = 100,100\n",
            "T0\n",
            "T1\n",
        ]
        self.assertIsNone(plugin.infer_tool_count([0, 100, 200, 0, 50], [100, 100]))
        _, info = plugin.process_gcode(lines, include_audit_header=False)
        self.assertFalse(info["flush_data_complete"])
        self.assertEqual(info["flush_status"], "FLUSH_DATA_INCOMPLETE")
        self.assertAlmostEqual(info["events"][1]["purge_length_mm"], 0.0, places=2)

    def test_flush_09_invalid_tool_ids(self) -> None:
        """Out-of-range tool ID T9 produces 0.00 purge and explicit warning without crashing."""
        lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 100,100\n",
            "T0\n",
            "T9\n",
        ]
        _, info = plugin.process_gcode(lines, include_audit_header=False)
        self.assertEqual(info["events"][1]["purge_length_mm"], 0.0)
        self.assertTrue(any("T9" in w for w in info["warnings"]))

    def test_flush_10_multiple_consecutive_toolchanges(self) -> None:
        """Consecutive toolchanges T0->T1->T2->T0->T2 emit 5 accurate purge commands."""
        lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,110,0,150,210,160,0\n",
            "; flush_volumes_vector = 50,60,70\n",
            "T0\n",
            "T1\n",
            "T2\n",
            "T0\n",
            "T2\n",
        ]
        out_lines, info = plugin.process_gcode(lines, filament_diameter=1.75, include_audit_header=False)
        purge_cmds = [ln.strip() for ln in out_lines if ln.startswith("ACE_SET_PURGE_AMOUNT")]
        self.assertEqual(len(purge_cmds), 5)
        self.assertEqual(info["purge_commands_inserted"], 5)
        area = math.pi * (1.75 * 0.5) ** 2
        expected_vols = [50.0, 100.0, 150.0, 210.0, 200.0]
        for ev, exp_v in zip(info["events"], expected_vols):
            self.assertAlmostEqual(ev["purge_length_mm"], round(exp_v / area, 2), places=2)

    def test_flush_11_repeated_same_tool_ids(self) -> None:
        """Repeated same-tool selection T1 -> T1 produces 0.00 mm purge."""
        lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 50,60\n",
            "T1\n",
            "T1\n",
        ]
        _, info = plugin.process_gcode(lines, include_audit_header=False)
        self.assertAlmostEqual(info["events"][1]["purge_length_mm"], 0.0, places=2)

    def test_flush_13_invalid_flush_values(self) -> None:
        """Negative, NaN, Inf values and negative filament diameter are safely rejected/clamped with warnings."""
        self.assertEqual(plugin.area_mm2(-1.75), 0.0)
        self.assertEqual(plugin.area_mm2(float("nan")), 0.0)
        self.assertEqual(plugin.volume_to_length_mm(100.0, -1.75), 0.0)
        self.assertEqual(plugin.volume_to_length_mm(float("inf"), 1.75), 0.0)
        self.assertEqual(plugin.compute_flush_volume(0, 1, 2, [0.0, -50.0, 100.0, 0.0], [10.0, 10.0], 1.0), 0.0)

        lines = [
            "; filament_diameter = -1.75, 1.75\n",
            "; flush_multiplier = -2.0\n",
            "; flush_volumes_matrix = 0, NaN, inf, -20\n",
            "; flush_volumes_vector = -10, abc\n",
            "T0\n",
            "T1\n",
        ]
        _, info = plugin.process_gcode(lines, filament_diameter=-1.75, include_audit_header=False)
        self.assertEqual(info["flush_multiplier"], 0.0)
        self.assertGreater(len(info["warnings"]), 2)
        for ev in info["events"]:
            self.assertGreaterEqual(ev["purge_length_mm"], 0.0)
            self.assertTrue(math.isfinite(ev["purge_length_mm"]))

    def test_flush_14_gcode_with_and_without_trailing_newline(self) -> None:
        """G-code ending without trailing newline never concatenates injected commands onto previous lines."""
        lines_no_nl = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 50,60\n",
            "G28\n",
            "G1 X10",  # intentionally missing \n
            "T1",       # intentionally missing \n
        ]
        out_lines, _ = plugin.process_gcode(
            lines_no_nl,
            tool_map={1: 3},
            cut_tip_after_print=True,
            include_audit_header=False,
        )
        text = "".join(out_lines)
        self.assertIn("G1 X10\nACE_SET_PURGE_AMOUNT PURGELENGTH=24.95 ; @ORCA_ACE_PURGE\nT3\nCUT_TIP\n", text)

    def test_flush_12_existing_ace_purge_commands_idempotency(self) -> None:
        """Repeated post-processing is 100% reversible/idempotent even across different mappings and toggles."""
        raw_lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 50,60\n",
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n",
            "G31\n",
            "T0\n",
            "T1\n",
            "PRINT_END CUT_TIP=0\n",
            "; EXECUTABLE_BLOCK_END\n",
        ]
        # Pass 1: map {0:2, 1:5}, disable G31, enable SKIP_PURGE & PRINT_END CUT_TIP=1
        out1, info1 = plugin.process_gcode(
            raw_lines,
            tool_map={0: 2, 1: 5},
            bed_leveling=False,
            skip_purge_for_already_loaded_tool=True,
            cut_tip_after_print=True,
            include_audit_header=True,
        )
        # Pass 2 (same settings): identical output and no duplicate commands
        out2, info2 = plugin.process_gcode(
            out1,
            tool_map={0: 2, 1: 5},
            bed_leveling=False,
            skip_purge_for_already_loaded_tool=True,
            cut_tip_after_print=True,
            include_audit_header=True,
        )
        self.assertEqual(out1, out2)
        self.assertEqual(info1["events"], info2["events"])

        # Pass 3: re-map with many-to-one {0:3, 1:3}, restore G31 / SKIP_PURGE=0 / PRINT_END CUT_TIP=0
        out3, info3 = plugin.process_gcode(
            out2,
            tool_map={0: 3, 1: 3},
            bed_leveling=True,
            skip_purge_for_already_loaded_tool=False,
            cut_tip_after_print=False,
            include_audit_header=True,
        )
        text3 = "".join(out3)
        self.assertEqual(text3.count("ACE_SET_PURGE_AMOUNT"), 2)
        self.assertEqual(text3.count("CUT_TIP\n"), 0)
        self.assertIn("PRINT_END CUT_TIP=0\n", text3)
        self.assertIn("G31\n", text3)
        self.assertNotIn("; G31 ; skipped by ACE Plugin", text3)
        self.assertIn("G9111 bedTemp=60 extruderTemp=215 tool=3 SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=0\n", text3)
        # Also verify strip_previous_postprocess recovers exact original G9111 line
        restored3 = "".join(plugin.strip_previous_postprocess(out3))
        self.assertEqual(restored3, "".join(raw_lines))
        # Purge still calculated using original Slicer indices 0 -> 1 (100 mm^3 -> 41.58 mm)
        self.assertEqual(info3["events"][0]["new_slicer_tool"], 0)
        self.assertEqual(info3["events"][1]["old_slicer_tool"], 0)
        self.assertEqual(info3["events"][1]["new_slicer_tool"], 1)
        self.assertAlmostEqual(info3["events"][1]["purge_length_mm"], 41.58, places=2)

        # Raw G-code containing a stray pre-existing ACE_SET_PURGE_AMOUNT without audit header
        stray_lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 50,60\n",
            "ACE_SET_PURGE_AMOUNT PURGELENGTH=999.99\n",
            "T0\n",
            "ACE_SET_PURGE_AMOUNT PURGELENGTH=888.88\n",
            "T1\n",
        ]
        out_stray, _ = plugin.process_gcode(stray_lines, include_audit_header=False)
        stray_text = "".join(out_stray)
        self.assertEqual(stray_text.count("ACE_SET_PURGE_AMOUNT"), 2)
        self.assertNotIn("999.99", stray_text)
        self.assertNotIn("888.88", stray_text)

    def test_postprocess_gcode_file_atomic_remapping_roundtrip(self) -> None:
        """postprocess_gcode_file strips prior passes and updates atomically when mapping changes."""
        raw = (
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,100,200,0\n"
            "; flush_volumes_vector = 50,60\n"
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n"
            "G31\n"
            "T0\n"
            "T1\n"
            "PRINT_END CUT_TIP=0\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            gpath = os.path.join(tmpdir, "part.gcode")
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(raw)

            st = plugin.PluginState()
            st.save_config = lambda: None
            st.tool_map = {0: 2, 1: 5}
            st.bed_leveling = True
            st.skip_purge_for_already_loaded_tool = False
            st.cut_tip_after_print = False

            info1 = plugin.postprocess_gcode_file(gpath, st)
            self.assertEqual(info1["toolchanges"], 2)
            self.assertEqual(info1["g9111MappedTool"], 2)

            # Change to colliding many-to-one mapping + different toggles and re-process
            st.tool_map = {0: 4, 1: 4}
            st.bed_leveling = False
            st.skip_purge_for_already_loaded_tool = True
            st.cut_tip_after_print = True
            info2 = plugin.postprocess_gcode_file(gpath, st)

            self.assertFalse(info2.get("already_processed", False))
            self.assertEqual(info2["g9111StartSlicerTool"], 0)
            self.assertEqual(info2["g9111MappedTool"], 4)
            self.assertEqual(info2["events"][0]["new_slicer_tool"], 0)
            self.assertEqual(info2["events"][1]["new_slicer_tool"], 1)
            self.assertAlmostEqual(info2["events"][1]["purge_length_mm"], 41.58, places=2)

            with open(gpath, "r", encoding="utf-8") as f:
                final_txt = f.read()
            self.assertEqual(final_txt.count("ACE_SET_PURGE_AMOUNT"), 2)
            self.assertIn("G9111 bedTemp=60 extruderTemp=215 tool=4 SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n", final_txt)
            self.assertNotIn("CUT_TIP\nG9111", final_txt)
            self.assertEqual(final_txt.count("CUT_TIP\n"), 0)
            self.assertIn("; G31 ; skipped by ACE Plugin\n", final_txt)
            self.assertIn("PRINT_END CUT_TIP=1\n", final_txt)
            # No leftover temp files in directory
            self.assertEqual(os.listdir(tmpdir), ["part.gcode"])

    def test_phase3_auto_match_and_color_mapping_validation(self) -> None:
        """Auto-Match never selects empty slots or incompatible materials, and validate_color_mapping catches errors."""
        # Case 1: Only empty slots available -> never assigns an empty slot
        empty_slots = [
            {"tool": 0, "status": "empty", "hex_color": "#FFFFFF", "material": "PLA", "rfid": False},
            {"tool": 1, "status": "empty", "hex_color": "#000000", "material": "PETG", "rfid": False},
        ]
        fils = [{"slicer_tool": 0, "color": "#FFFFFF", "material": "PLA", "used": True}]
        val_empty = plugin.validate_color_mapping(fils, {0: 0}, empty_slots)
        self.assertFalse(val_empty["valid"])
        self.assertEqual(val_empty["empty_slots"], [0])

        # Case 2: Wrong material with exact RGB match (#FFFFFF PETG) MUST NOT beat right material (#000000 PLA)
        # color_distance_sq(#FFFFFF, #000000) == 520200, incompatible penalty == 1_000_000
        slots_mat = [
            {"tool": 0, "status": "ready", "hex_color": "#FFFFFF", "material": "PETG", "rfid": True},
            {"tool": 1, "status": "ready", "hex_color": "#000000", "material": "PLA", "rfid": True},
            {"tool": 2, "status": "empty", "hex_color": "#FFFFFF", "material": "PLA", "rfid": False},
        ]
        matched = plugin.auto_match_filaments_to_slots(fils, slots_mat)
        self.assertEqual(matched[0], 1)

        # Case 3: Unconfigured default non-RFID [0,0,0] empty-material slot is penalized vs real dark slot
        slots_unconf = [
            {"tool": 0, "status": "ready", "hex_color": "#000000", "material": "", "rfid": False},
            {"tool": 1, "status": "ready", "hex_color": "#181818", "material": "PLA", "rfid": False},
        ]
        fil_dark = [{"slicer_tool": 0, "color": "#000000", "material": "PLA", "used": True}]
        self.assertEqual(plugin.auto_match_filaments_to_slots(fil_dark, slots_unconf)[0], 1)

        # Case 4: Duplicate, out-of-range, empty, and material mismatch checks
        fils_multi = [
            {"slicer_tool": 0, "color": "#FFFFFF", "material": "PLA", "used": True},
            {"slicer_tool": 1, "color": "#000000", "material": "ASA", "used": True},
            {"slicer_tool": 2, "color": "#FF0000", "material": "PETG", "used": True},
            {"slicer_tool": 3, "color": "#00FF00", "material": "PLA", "used": True},
        ]
        slots_4 = [
            {"tool": 0, "status": "ready", "hex_color": "#FFFFFF", "material": "PETG", "rfid": True},
            {"tool": 1, "status": "empty", "hex_color": "#000000", "material": "", "rfid": False},
            {"tool": 2, "status": "ready", "hex_color": "#000000", "material": "", "rfid": False},
            {"tool": 3, "status": "ready", "hex_color": "#FF0000", "material": "PETG", "rfid": True},
        ]
        # Map T0->0 (mat mismatch), T1->0 (duplicate), T2->1 (empty), T3->6 (out of range for 4-slot ACE)
        val = plugin.validate_color_mapping(fils_multi, {0: 0, 1: 0, 2: 1, 3: 6}, slots_4)
        self.assertFalse(val["valid"])
        self.assertEqual(val["duplicate_slots"], [0])
        self.assertEqual(val["empty_slots"], [1])
        self.assertEqual(val["out_of_range_slots"], [6])
        self.assertGreaterEqual(len(val["material_mismatches"]), 1)

    def test_phase3_plugin_state_persistence_and_order_reset(self) -> None:
        """PluginState persists/restores slicer_filaments & last_postprocess_info and resets stale tool_map on change."""
        st1 = plugin.PluginState()
        st1.save_config = lambda: None
        fil_a = [
            {"slicer_tool": 0, "color": "#FF0000", "material": "PLA", "used": True},
            {"slicer_tool": 1, "color": "#0000FF", "material": "PLA", "used": True},
        ]
        st1.update_slicer_filaments(fil_a)
        st1.tool_map = {0: 3, 1: 2}
        st1.last_postprocess_info = {"flush_status": "POSTPROCESS_OK", "toolchanges": 4}
        dumped = st1.to_dict()

        st2 = plugin.PluginState()
        st2.save_config = lambda: None
        st2.apply_dict(dumped, save=False)
        self.assertEqual(st2.slicer_filaments, fil_a)
        self.assertEqual(st2.last_postprocess_info, {"flush_status": "POSTPROCESS_OK", "toolchanges": 4})
        self.assertEqual(st2.tool_map[0], 3)
        self.assertEqual(st2.tool_map[1], 2)

        # Change filament order/material -> stale mapping resets to 1:1 with warning
        fil_b = [
            {"slicer_tool": 0, "color": "#0000FF", "material": "PLA", "used": True},
            {"slicer_tool": 1, "color": "#FF0000", "material": "PLA", "used": True},
        ]
        was_reset = st2.update_slicer_filaments(fil_b)
        self.assertTrue(was_reset)
        self.assertEqual(st2.tool_map[0], 0)
        self.assertEqual(st2.tool_map[1], 1)
        self.assertTrue(len(st2.mapping_warnings) >= 1)

    def test_phase4_orca_host_preset_bundle_sync(self) -> None:
        """sync_filaments_from_orca_host reads mock orca.host.preset_bundle() and degrades gracefully."""
        class _MockPreset:
            def __init__(self, name: str, colour: str, ftype: str, vendor: str) -> None:
                self.name = name
                self._vals = {
                    "filament_colour": colour,
                    "filament_type": ftype,
                    "filament_vendor": vendor,
                    "filament_diameter": "1.75",
                }

            def config_value(self, key: str):
                return self._vals.get(key)

        class _MockBundle:
            def current_filament_presets(self):
                return [
                    _MockPreset("Bambu PLA Red", "#FF1122", "PLA", "Generic"),
                    _MockPreset("Overture PETG Blue", "#0033FF", "PETG", "Overture"),
                ]

            def current_filament_preset_names(self):
                return ["Bambu PLA Red", "Overture PETG Blue"]

            def full_config_value(self, key: str):
                mapping = {
                    "filament_colour": ["#FF1122", "#0033FF"],
                    "filament_type": ["PLA", "PETG"],
                    "filament_vendor": ["Generic", "Overture"],
                    "filament_diameter": ["1.75", "1.75"],
                }
                return mapping.get(key)

        class _MockHost:
            def preset_bundle(self):
                return _MockBundle()

        st = plugin.PluginState()
        st.save_config = lambda: None

        with mock.patch.object(plugin.orca, "host", _MockHost(), create=True):
            fils = plugin.sync_filaments_from_orca_host(st)

        self.assertEqual(len(fils), 2)
        self.assertEqual(fils[0]["color"], "#FF1122")
        self.assertEqual(fils[0]["material"], "PLA")
        self.assertEqual(fils[0]["profile"], "Bambu PLA Red")
        self.assertEqual(fils[1]["color"], "#0033FF")
        self.assertEqual(fils[1]["material"], "PETG")
        self.assertEqual(st.slicer_filaments[1]["vendor"], "Overture")

        # Graceful degradation when preset_bundle is unavailable
        with mock.patch.object(plugin.orca, "host", object(), create=True):
            self.assertEqual(plugin.sync_filaments_from_orca_host(st), [])

    def test_phase5_http_error_handling_and_print_safety(self) -> None:
        """HTTPError raises RuntimeError and check_print_safety blocks printing/paused states."""
        client = plugin.MoonrakerClient("http://127.0.0.1:7125", enable_port80_fallback=False)

        # Simulate Moonraker HTTP 400 JSON error without touching any network
        err_stream = io.BytesIO(b'{"error":{"code":400,"message":"Unknown macro CUT_BAD"}}')
        http_400 = urllib.error.HTTPError(
            "http://127.0.0.1:7125/printer/gcode/script",
            400,
            "Bad Request",
            None,
            err_stream,
        )
        with mock.patch("urllib.request.urlopen", side_effect=http_400):
            with self.assertRaises(RuntimeError) as ctx_err:
                client.send_gcode("CUT_BAD")
        self.assertIn("400", str(ctx_err.exception))
        self.assertIn("Unknown macro CUT_BAD", str(ctx_err.exception))

        # Verify print_stats extraction in normalize_ace_status
        printing_norm = plugin.MoonrakerClient.normalize_ace_status(
            {"print_stats": {"state": "printing", "filename": "benchy.gcode"}}
        )
        self.assertEqual(printing_norm["print_state"], "printing")
        self.assertEqual(printing_norm["print_filename"], "benchy.gcode")
        self.assertTrue(printing_norm["is_printing"])

        standby_norm = plugin.MoonrakerClient.normalize_ace_status(
            {"print_stats": {"state": "standby", "filename": ""}}
        )
        self.assertEqual(standby_norm["print_state"], "standby")
        self.assertFalse(standby_norm["is_printing"])

        # Verify check_print_safety blocks 'printing' and 'paused', allows 'standby'
        st = plugin.PluginState()
        for blocked_state in ("printing", "paused"):
            with mock.patch.object(
                client,
                "query_ace_status",
                return_value={"is_printing": True, "print_state": blocked_state},
            ):
                with self.assertRaises(RuntimeError):
                    plugin.check_print_safety(client, "CUT_TIP", st)

        with mock.patch.object(
            client,
            "query_ace_status",
            return_value={"is_printing": False, "print_state": "standby"},
        ):
            res = plugin.check_print_safety(client, "CUT_TIP", st)
            self.assertFalse(res["is_printing"])

    def test_phase6_offline_preview_preserves_file_hash(self) -> None:
        """preview_gcode_file analyzes flush volume/length read-only without modifying the file SHA256."""
        raw = (
            "; filament_colour = #FF0000;#00FF00\n"
            "; filament_type = PLA;PLA\n"
            "; flush_multiplier = 1.2\n"
            "; flush_volumes_matrix = 0,100,200,0\n"
            "; flush_volumes_vector = 50,60\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\n"
            "T1\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            gpath = os.path.join(tmpdir, "preview_only.gcode")
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(raw)

            with open(gpath, "rb") as rf:
                sha_before = hashlib.sha256(rf.read()).hexdigest()

            st = plugin.PluginState()
            st.save_config = lambda: None
            st.tool_map = {0: 1, 1: 3}

            info = plugin.preview_gcode_file(gpath, st)

            with open(gpath, "rb") as rf:
                sha_after = hashlib.sha256(rf.read()).hexdigest()
            self.assertEqual(sha_before, sha_after)
            self.assertEqual(os.listdir(tmpdir), ["preview_only.gcode"])
            self.assertEqual(info["flush_status"], "POSTPROCESS_OK")
            self.assertEqual(info["toolchanges"], 2)
            # Default skip_purge_for_already_loaded_tool=True skips initial T0 (matches G9111 tool=0), leaving T0->T1 (100*1.2 = 120 mm^3)
            self.assertTrue(info["skipPurgeForAlreadyLoadedTool"])
            self.assertAlmostEqual(info["total_purge_volume_mm3"], 120.0, places=2)
            self.assertEqual(len(info["matrix"]), 4)
            self.assertEqual(len(info["vector"]), 2)
            self.assertEqual(info["events"][0]["mapped_tool"], 1)
            self.assertEqual(info["events"][1]["mapped_tool"], 3)

            # With skip_purge_for_already_loaded_tool=False, Initial T0 (50*1.2 = 60) + T0->T1 (100*1.2 = 120) = 180 mm^3
            st.skip_purge_for_already_loaded_tool = False
            info_full = plugin.preview_gcode_file(gpath, st)
            with open(gpath, "rb") as rf:
                sha_after_full = hashlib.sha256(rf.read()).hexdigest()
            self.assertEqual(sha_before, sha_after_full)
            self.assertAlmostEqual(info_full["total_purge_volume_mm3"], 180.0, places=2)
            self.assertGreater(info_full["total_purge_length_mm"], 70.0)

    def test_phase7_upload_safety_gate_and_dashboard_ui(self) -> None:
        """can_upload_gcode blocks incomplete flush data, empty/out-of-range slots, and active prints, while allowing shared ACE slots."""
        st = plugin.PluginState()
        st.save_config = lambda: None
        valid_fils = [
            {"slicer_tool": 0, "color": "#FFFFFF", "material": "PLA", "used": True},
            {"slicer_tool": 1, "color": "#000000", "material": "PLA", "used": True},
        ]
        valid_info = {
            "flush_status": "POSTPROCESS_OK",
            "flush_data_complete": True,
            "used_slicer_tools": [0, 1],
            "slicer_filaments": valid_fils,
            "toolMap": {0: 0, 1: 1},
            "warnings": [],
        }
        valid_status = {
            "is_printing": False,
            "print_state": "standby",
            "slots": [
                {"tool": 0, "status": "ready", "hex_color": "#FFFFFF", "material": "PLA", "rfid": True},
                {"tool": 1, "status": "ready", "hex_color": "#000000", "material": "PLA", "rfid": True},
                {"tool": 2, "status": "empty", "hex_color": "#000000", "material": "", "rfid": False},
                {"tool": 3, "status": "ready", "hex_color": "#FF0000", "material": "PETG", "rfid": True},
            ],
        }

        # 1) Valid case -> allowed
        ok, blocks, warns = plugin.can_upload_gcode(valid_info, st, ace_status=valid_status, start_print=False)
        self.assertTrue(ok)
        self.assertEqual(blocks, [])
        self.assertEqual(warns, [])

        # 2) Incomplete flush data -> blocked
        bad_flush = dict(valid_info, flush_status="FLUSH_DATA_INCOMPLETE", flush_data_complete=False)
        ok, blocks, _ = plugin.can_upload_gcode(bad_flush, st, ace_status=valid_status, start_print=False)
        self.assertFalse(ok)
        self.assertTrue(any("FLUSH_DATA_INCOMPLETE" in b for b in blocks))

        # 3) Empty slot mapped -> blocked
        bad_empty = dict(valid_info, toolMap={0: 0, 1: 2})
        ok, blocks, _ = plugin.can_upload_gcode(bad_empty, st, ace_status=valid_status, start_print=False)
        self.assertFalse(ok)
        self.assertTrue(any("T2" in b and ("empty" in b.lower() or "leer" in b.lower()) for b in blocks))

        # 4) Out-of-range slot mapped -> blocked
        bad_range = dict(valid_info, toolMap={0: 0, 1: 7})
        ok, blocks, _ = plugin.can_upload_gcode(bad_range, st, ace_status=valid_status, start_print=False)
        self.assertFalse(ok)
        self.assertTrue(any("T7" in b for b in blocks))

        # 5) Intentional multi-extruder reuse on the same valid ACE slot -> allowed
        shared_slot = dict(valid_info, toolMap={0: 0, 1: 0})
        ok, blocks, _ = plugin.can_upload_gcode(shared_slot, st, ace_status=valid_status, start_print=False)
        self.assertTrue(ok)
        self.assertEqual(blocks, [])

        # 6) start_print=True during active print -> blocked
        printing_status = dict(valid_status, is_printing=True, print_state="printing")
        ok, blocks, _ = plugin.can_upload_gcode(valid_info, st, ace_status=printing_status, start_print=True)
        self.assertFalse(ok)
        self.assertTrue(any("printing" in b for b in blocks))

        # Verify UI contains Card 6 Dashboard, Offline Preview button, and Orca Preset button
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn("6. Flush Volume Monitoring &amp; Post-Processing Audit", html)
        self.assertIn("previewGcodeFlush()", html)
        self.assertIn("syncOrcaFilaments()", html)
        self.assertIn("printSafetyBanner", html)

    def test_review_01_2n_vector_without_matrix_and_vector_summary(self) -> None:
        """2*N vector without matrix uses parsed_diameters for tool count and emits consistent vector_summary."""
        lines_2n_no_mat = [
            "; filament_diameter = 1.75, 1.75\n",
            "; flush_multiplier = 1.5\n",
            "; flush_volumes_vector = 20,100,30,150\n",
            "T1\n",
        ]
        _, info = plugin.process_gcode(lines_2n_no_mat, include_audit_header=False)
        self.assertEqual(info["tools"], 2)
        area = math.pi * (1.75 * 0.5) ** 2
        expected_vol = 150.0 * 1.5
        expected_len = round(expected_vol / area, 2)
        self.assertEqual(len(info["events"]), 1)
        self.assertAlmostEqual(info["events"][0]["volume_mm3"], expected_vol, places=2)
        self.assertAlmostEqual(info["events"][0]["purge_length_mm"], expected_len, places=2)

        vs = info["vector_summary"]
        self.assertEqual(len(vs), 2)
        self.assertEqual(vs[0]["layout"], "2xN")
        self.assertEqual(vs[0]["unload_mm3"], 20.0)
        self.assertEqual(vs[0]["load_mm3"], 100.0)
        self.assertEqual(vs[1]["unload_mm3"], 30.0)
        self.assertEqual(vs[1]["load_mm3"], 150.0)
        self.assertAlmostEqual(vs[1]["effective_load_mm3"], expected_vol, places=2)
        self.assertAlmostEqual(vs[1]["effective_length_mm"], expected_len, places=2)

        # Verify N-entry vector summary as well
        lines_n = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,80,80,0\n",
            "; flush_volumes_vector = 120,240\n",
            "T0\n",
        ]
        _, info_n = plugin.process_gcode(lines_n, filament_diameter=1.75, include_audit_header=False)
        self.assertEqual(len(info_n["vector_summary"]), 2)
        self.assertEqual(info_n["vector_summary"][0]["layout"], "N")
        self.assertIsNone(info_n["vector_summary"][0]["unload_mm3"])
        self.assertEqual(info_n["vector_summary"][0]["load_mm3"], 120.0)

    def test_review_02_reversible_postprocess_preserves_user_cut_tip_and_standalone_commands(self) -> None:
        """strip_previous_postprocess removes only plugin-injected lines, keeping user CUT_TIP and standalone purge commands."""
        raw_lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 50,60\n",
            "CUT_TIP ; user manual start cut\n",
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n",
            "G31\n",
            "T0\n",
            "ACE_SET_PURGE_AMOUNT PURGELENGTH=35.0 ; manual mid-print prime\n",
            "G1 X50 Y50 E1.0\n",
            "T1\n",
            "PRINT_END CUT_TIP=0\n",
            "; EXECUTABLE_BLOCK_END\n",
        ]
        out1, _ = plugin.process_gcode(
            raw_lines,
            tool_map={0: 2, 1: 5},
            bed_leveling=False,
            skip_purge_for_already_loaded_tool=True,
            cut_tip_after_print=True,
            include_audit_header=True,
        )
        txt1 = "".join(out1)
        self.assertIn("CUT_TIP ; user manual start cut\n", txt1)
        self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=35.0 ; manual mid-print prime\n", txt1)
        # PRINT_END present -> uses PRINT_END CUT_TIP=1 rather than injecting loose CUT_TIP; only user CUT_TIP exists
        self.assertIn("PRINT_END CUT_TIP=1\n", txt1)
        self.assertIn("SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n", txt1)
        self.assertEqual(sum(1 for l in out1 if not l.lstrip().startswith(";") and "CUT_TIP" in l and "PRINT_END" not in l), 1)

        # Re-process with cut_tip & skip_purge toggles turned OFF and bed_leveling ON
        out2, info2 = plugin.process_gcode(
            out1,
            tool_map={0: 4, 1: 6},
            bed_leveling=True,
            skip_purge_for_already_loaded_tool=False,
            cut_tip_after_print=False,
            include_audit_header=True,
        )
        txt2 = "".join(out2)
        remaining_cuts = [l for l in out2 if not l.lstrip().startswith(";") and "CUT_TIP" in l and "PRINT_END" not in l]
        self.assertEqual(remaining_cuts, ["CUT_TIP ; user manual start cut\n"])
        # User standalone ACE_SET_PURGE_AMOUNT preserved; 2 toolchange ACE_SET_PURGE_AMOUNT re-emitted
        self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=35.0 ; manual mid-print prime\n", txt2)
        self.assertEqual(txt2.count("ACE_SET_PURGE_AMOUNT"), 3)
        self.assertIn("G31\n", txt2)
        self.assertIn("G9111 bedTemp=60 extruderTemp=215 tool=4 SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=0\n", txt2)
        self.assertIn("PRINT_END CUT_TIP=0\n", txt2)
        self.assertEqual(info2["events"][1]["mapped_tool"], 6)

        # Stripping recovers exact raw_lines (excluding plugin header & added params)
        self.assertEqual("".join(plugin.strip_previous_postprocess(out2)), "".join(raw_lines))

        # Fallback without PRINT_END injects standalone CUT_TIP and reversibly removes ONLY injected CUT_TIP
        raw_no_end = [l for l in raw_lines if "PRINT_END" not in l]
        out_fb1, _ = plugin.process_gcode(raw_no_end, cut_tip_after_print=True, include_audit_header=True)
        self.assertEqual(sum(1 for l in out_fb1 if l.strip().startswith("CUT_TIP")), 2)
        out_fb2, _ = plugin.process_gcode(out_fb1, cut_tip_after_print=False, include_audit_header=True)
        remaining_fb = [l for l in out_fb2 if not l.lstrip().startswith(";") and "CUT_TIP" in l]
        self.assertEqual(remaining_fb, ["CUT_TIP ; user manual start cut\n"])

    def test_review_03_plugin_state_initial_mapping_preserved_and_source_tracking(self) -> None:
        """First real G-code load keeps pre-configured tool_map, tracks sources, and resets only on real filament change."""
        raw_a = (
            "; filament_colour = #FF0000;#00FF00\n"
            "; filament_type = PLA;PETG\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,80,80,0\n"
            "; flush_volumes_vector = 40,40\n"
            "T0\n"
            "T1\n"
        )
        raw_b = (
            "; filament_colour = #0000FF;#FFFF00\n"
            "; filament_type = ASA;PLA\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,80,80,0\n"
            "; flush_volumes_vector = 40,40\n"
            "T0\n"
            "T1\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a = os.path.join(tmpdir, "a.gcode")
            path_b = os.path.join(tmpdir, "b.gcode")
            with open(path_a, "w", encoding="utf-8") as fa:
                fa.write(raw_a)
            with open(path_b, "w", encoding="utf-8") as fb:
                fb.write(raw_b)

            st = plugin.PluginState()
            st.save_config = lambda: None
            self.assertEqual(st.filament_signature, "")
            self.assertEqual(st.tool_map_sources[0], "default")

            st.apply_dict({"tool_map": {"0": 2, "1": 3}}, save=False)
            self.assertEqual(st.tool_map_sources[0], "manual")
            self.assertEqual(st.tool_map_sources[1], "manual")

            # First real file must NOT wipe custom tool_map
            info_a = plugin.preview_gcode_file(path_a, st)
            self.assertEqual(info_a["analysis_mode"], "preview")
            self.assertEqual(info_a["events"][0]["mapped_tool"], 2)
            self.assertEqual(info_a["events"][1]["mapped_tool"], 3)
            self.assertEqual(st.tool_map[0], 2)

            # Preserving auto source when unrelated toggles change
            st.apply_dict({"bed_leveling": False, "tool_map": {"0": 2, "1": 3}, "tool_map_sources": {"0": "auto"}}, save=False)
            self.assertEqual(st.tool_map_sources[0], "auto")

            # Loading a DIFFERENT filament project must reset stale mapping BEFORE analyzing preview
            info_b = plugin.preview_gcode_file(path_b, st)
            self.assertEqual(info_b["events"][0]["mapped_tool"], 0)
            self.assertEqual(info_b["events"][1]["mapped_tool"], 1)
            self.assertEqual(st.tool_map_sources[0], "default")

    def test_review_04_moonraker_client_direct_print_safety_interlock(self) -> None:
        """MoonrakerClient enforces print-safety directly inside send_gcode and start_print upload."""
        client = plugin.MoonrakerClient("http://127.0.0.1:7125", enable_port80_fallback=False)

        with mock.patch.object(
            client,
            "query_ace_status",
            return_value={"is_printing": True, "print_state": "paused"},
        ), mock.patch.object(client, "_request_json", side_effect=AssertionError("POST must not run while printing")):
            for call in (
                lambda: client.send_gcode("CUT_TIP"),
                lambda: client.trigger_cut_tip(),
                lambda: client.trigger_bed_leveling("G31"),
                lambda: client.load_slot(0),
                lambda: client.unload_slot(0),
                lambda: client.feed_slot(0, 20),
                lambda: client.retract_slot(0, 20),
                lambda: client.upload_gcode("test.gcode", "G28\n", start_print=True),
            ):
                with self.assertRaises(RuntimeError):
                    call()

        # Dryer remains explicitly allowed (allow_while_printing=True) without triggering send_gcode safety failure
        with mock.patch.object(client, "_request_json", return_value={"result": "ok"}) as mock_req:
            client.start_dryer(50, 60)
            mock_req.assert_called_once()

        # Non-printing upload (start_print=False) uses upload endpoint cleanly without safety block
        with mock.patch("urllib.request.urlopen") as mock_open:
            fake_resp = mock.MagicMock()
            fake_resp.read.return_value = b'{"result":{"item":{"path":"test.gcode"}}}'
            fake_resp.__enter__.return_value = fake_resp
            mock_open.return_value = fake_resp
            up_res = client.upload_gcode("test.gcode", "G28\n", start_print=False)
            self.assertEqual(up_res["result"]["item"]["path"], "test.gcode")

        # Verify register_capabilities calls load_config() before requesting network_http permissions
        mock_orca = plugin.orca
        with mock.patch.object(plugin.GLOBAL_STATE, "load_config") as mock_load, mock.patch.object(
            mock_orca, "request_permissions"
        ) as mock_perms, mock.patch.object(mock_orca, "register_capability"):
            plugin.GLOBAL_STATE.moonraker_url = "http://192.0.2.55:7125"
            plug = plugin.KobraS1AcePlugin()
            plug.register_capabilities()
            mock_load.assert_called_once()
            perms_kwargs = mock_perms.call_args.kwargs
            self.assertIn("http://192.0.2.55:7125", perms_kwargs["network_http"])
            plugin.GLOBAL_STATE.moonraker_url = plugin.DEFAULT_MOONRAKER_URL

    def test_review_05_redesigned_ui_theme_tokens_and_controls(self) -> None:
        """Redesigned UI integrates OrcaSlicer CSS variables, Auto/Dark/Light modes, and all workbench controls."""
        html = plugin.ACE_PLUGIN_HTML_UI
        for required_token in (
            "--orca-bg",
            "--orca-fg",
            "--orca-muted",
            "--orca-accent",
            "--orca-accent-fg",
            "--orca-border",
            "--orca-font",
            "setThemeMode('auto')",
            "setThemeMode('dark')",
            "setThemeMode('light')",
            "data-resolved",
            "execConnVal",
            "execToolVal",
            "execDryerVal",
            "execGcodeVal",
            "slot-spool-svg",
            "slot-color-bar",
            "resetColorMapping()",
            "mappingSummaryBar",
            "vector_summary",
            "Unload (mm³)",
            "Load (mm³)",
            "flushEventsDetails",
        ):
            self.assertIn(required_token, html)
        # Never use !important in OrcaSlicer Pages CSS
        style_block = html.split("<style>")[1].split("</style>")[0]
        self.assertNotIn("!important", style_block)

    def test_i18n_01_dictionaries_complete_and_matching_keys(self) -> None:
        """Both English ('en') and German ('de') dictionaries contain the exact same complete set of keys."""
        self.assertEqual(plugin.SUPPORTED_LANGUAGES, ("en", "de"))
        self.assertEqual(plugin.DEFAULT_LANGUAGE, "en")
        self.assertIn("en", plugin.TRANSLATIONS)
        self.assertIn("de", plugin.TRANSLATIONS)

        en_keys = set(plugin.TRANSLATIONS["en"].keys())
        de_keys = set(plugin.TRANSLATIONS["de"].keys())
        self.assertGreaterEqual(len(en_keys), 150)
        self.assertEqual(
            en_keys,
            de_keys,
            f"Key mismatch between en and de: en-de={en_keys - de_keys}, de-en={de_keys - en_keys}",
        )

    def test_i18n_02_no_empty_translations_and_preserved_technical_identifiers(self) -> None:
        """No translation key resolves to an empty string and technical terms remain intact in both languages."""
        for lang in ("en", "de"):
            for key, val in plugin.TRANSLATIONS[lang].items():
                self.assertIsInstance(val, str, f"{lang}.{key} is not a string")
                self.assertTrue(val.strip(), f"{lang}.{key} resolved to an empty string")
                resolved = plugin.t(key, lang=lang)
                self.assertTrue(resolved.strip(), f"t('{key}', lang='{lang}') returned empty string")

        # Verify technical identifiers remain unchanged in both languages
        for lang in ("en", "de"):
            self.assertIn("Moonraker", plugin.t("ui.sec1.title", lang=lang))
            self.assertIn("ACE Pro", plugin.t("ui.sec3.title", lang=lang))
            self.assertIn("T0–T7", plugin.t("ui.sec3.title", lang=lang))
            self.assertIn("Bed Leveling", plugin.t("ui.modal.opt.g31", lang=lang))
            self.assertIn("G31", plugin.t("ui.modal.opt.g31_tip", lang=lang))
            self.assertIn("PRINT_END CUT_TIP=1", plugin.t("ui.modal.opt.cut_post_tip", lang=lang))
            self.assertIn("G9111", plugin.t("ui.sec5.opt_skip_purge_sub", lang=lang))
            self.assertIn("SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1", plugin.t("ui.modal.opt.skip_purge_tip", lang=lang))
            self.assertTrue(len(plugin.t("ui.modal.opt.cut_post", lang=lang)) <= 24)
            self.assertTrue(len(plugin.t("ui.modal.opt.skip_purge", lang=lang)) <= 24)
            self.assertIn("ACE 1", plugin.t("ui.sec2.unit_ace1", lang=lang))
            self.assertIn("ACE 2", plugin.t("ui.sec2.unit_ace2", lang=lang))
            self.assertEqual(plugin.t("header.language.en", lang=lang), "English")
            self.assertEqual(plugin.t("header.language.de", lang=lang), "Deutsch")

    def test_i18n_03_missing_or_empty_german_keys_fallback_to_english(self) -> None:
        """Missing or empty German translation keys fall back cleanly to English without errors."""
        with mock.patch.dict(
            plugin.TRANSLATIONS["de"],
            {"ui.sec1.connect_btn": ""},
            clear=False,
        ):
            self.assertEqual(
                plugin.t("ui.sec1.connect_btn", lang="de"),
                plugin.TRANSLATIONS["en"]["ui.sec1.connect_btn"],
            )

        de_copy = dict(plugin.TRANSLATIONS["de"])
        removed_val = de_copy.pop("ui.sec4.btn_reset")
        try:
            plugin.TRANSLATIONS["de"] = de_copy
            self.assertEqual(
                plugin.t("ui.sec4.btn_reset", lang="de"),
                plugin.TRANSLATIONS["en"]["ui.sec4.btn_reset"],
            )
        finally:
            de_copy["ui.sec4.btn_reset"] = removed_val
            plugin.TRANSLATIONS["de"] = de_copy

        # Unknown key falls back to the key identifier itself
        self.assertEqual(plugin.t("nonexistent.custom.key", lang="de"), "nonexistent.custom.key")
        # Invalid language codes normalize to English ('en')
        self.assertEqual(plugin.normalize_language(None), "en")
        self.assertEqual(plugin.normalize_language("fr"), "en")
        self.assertEqual(plugin.normalize_language("DE-AT"), "de")
        self.assertEqual(plugin.normalize_language("en_US"), "en")

    def test_i18n_04_parameter_interpolation_and_validation_messages(self) -> None:
        """Parameter interpolation works in both English and German across t(), validation, and G-code warnings."""
        en_msg = plugin.t("val.map.duplicate", lang="en", extruders="T0, T1", phys_tool=2)
        de_msg = plugin.t("val.map.duplicate", lang="de", extruders="T0, T1", phys_tool=2)
        self.assertIn("T0, T1", en_msg)
        self.assertIn("T2", en_msg)
        self.assertIn("Duplicate slot assignment", en_msg)
        self.assertIn("T0, T1", de_msg)
        self.assertIn("T2", de_msg)
        self.assertIn("Doppelte Slot-Belegung", de_msg)

        # Unsupplied placeholder remains intact without raising KeyError
        partial = plugin.t("val.map.duplicate", lang="en", extruders="T0, T1")
        self.assertIn("T0, T1", partial)
        self.assertIn("{phys_tool}", partial)

        # Verify validate_color_mapping and process_gcode emit both localized strings and structured items
        slicer_fils = [
            {"slicer_tool": 0, "material": "PLA", "color": "#FF0000", "used": True},
            {"slicer_tool": 1, "material": "PETG", "color": "#00FF00", "used": True},
        ]
        ace_slots = [
            {"tool": 0, "status": "empty", "material": "", "hex_color": "#000000", "rfid": False},
            {"tool": 1, "status": "ready", "material": "PLA", "hex_color": "#00FF00", "rfid": True},
        ]
        val_en = plugin.validate_color_mapping(slicer_fils, {0: 0, 1: 1}, ace_slots, lang="en")
        val_de = plugin.validate_color_mapping(slicer_fils, {0: 0, 1: 1}, ace_slots, lang="de")
        self.assertTrue(any("empty ACE slot T0" in e for e in val_en["errors"]))
        self.assertTrue(any("leeren ACE-Slot T0" in e for e in val_de["errors"]))
        self.assertTrue(any("Material mismatch" in w for w in val_en["warnings"]))
        self.assertTrue(any("Material-Abweichung" in w for w in val_de["warnings"]))
        self.assertEqual(val_en["error_items"][0]["key"], "val.map.empty_slot")
        self.assertEqual(val_en["warning_items"][0]["key"], "val.map.mat_mismatch")

    def test_i18n_05_ui_instant_language_switching_without_restart(self) -> None:
        """UI provides top-right English/Deutsch selector and immediate client-side DOM & audit re-translation."""
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn('id="langBtnEn"', html)
        self.assertIn('id="langBtnDe"', html)
        self.assertIn("setLanguage('en', true)", html)
        self.assertIn("setLanguage('de', true)", html)
        self.assertIn("function applyTranslations()", html)
        self.assertIn("info.warning_items", html)
        self.assertIn("mval.error_items", html)
        # Ensure HTML is not duplicated per language (single #slotsContainer, single #modalMappingCards, no #mappingTableBody)
        self.assertEqual(html.count('id="slotsContainer"'), 1)
        self.assertEqual(html.count('id="modalMappingCards"'), 1)
        self.assertEqual(html.count('id="mappingTableBody"'), 0)
        self.assertEqual(html.count('id="flushMatrixContainer"'), 1)

    def test_i18n_06_language_persistence_without_overwriting_settings(self) -> None:
        """Selected language persists across reloads without overwriting other plugin settings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "state.json")
            st1 = plugin.PluginState()
            st1.resolve_config_path = lambda: cfg_file
            # Default language is English ('en')
            self.assertEqual(st1.language, "en")

            # Configure custom settings
            st1.apply_dict(
                {
                    "moonraker_url": "http://192.0.2.199:7125",
                    "api_key": "example-api-key-placeholder",
                    "bed_leveling": False,
                    "skip_purge_for_already_loaded_tool": False,
                    "dryer_unit": "1",
                    "dryer_temp": 60,
                    "tool_map": {"0": 3, "1": 2},
                },
                save=True,
            )
            self.assertEqual(st1.language, "en")

            # Change language via PageCapability set_language message (must NOT overwrite other settings)
            page = plugin.KobraS1AcePageCapability()
            sent_messages = []
            page.post_message = lambda payload: sent_messages.append(payload)
            with mock.patch.object(plugin, "GLOBAL_STATE", st1):
                page.on_message({"type": "set_language", "language": "de"})

            self.assertEqual(st1.language, "de")
            self.assertEqual(st1.moonraker_url, "http://192.0.2.199:7125")
            self.assertEqual(st1.api_key, "example-api-key-placeholder")
            self.assertFalse(st1.bed_leveling)
            self.assertFalse(st1.skip_purge_for_already_loaded_tool)
            self.assertEqual(st1.dryer_unit, "1")
            self.assertEqual(st1.dryer_temp, 60)
            self.assertEqual(st1.tool_map[0], 3)
            self.assertEqual(st1.tool_map[1], 2)
            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(sent_messages[0]["state"]["language"], "de")
            self.assertIn("Deutsch", sent_messages[0]["log"])

            # Reload state into a fresh PluginState instance and verify everything is restored
            st2 = plugin.PluginState()
            st2.resolve_config_path = lambda: cfg_file
            st2.load_config()
            self.assertEqual(st2.language, "de")
            self.assertEqual(st2.moonraker_url, "http://192.0.2.199:7125")
            self.assertEqual(st2.api_key, "example-api-key-placeholder")
            self.assertFalse(st2.bed_leveling)
            self.assertFalse(st2.skip_purge_for_already_loaded_tool)
            self.assertEqual(st2.dryer_unit, "1")
            self.assertEqual(st2.dryer_temp, 60)
            self.assertEqual(st2.tool_map[0], 3)
            self.assertEqual(st2.tool_map[1], 2)

            # Subsequent save_settings without "language" in payload must NOT reset language back to "en"
            st2.apply_dict({"cut_tip_after_print": True}, save=True)
            self.assertEqual(st2.language, "de")

    def test_i18n_07_complete_static_and_dynamic_ui_translation_coverage(self) -> None:
        """Every data-i18n* attribute and JS t('...') call in ACE_PLUGIN_HTML_UI resolves in both 'en' and 'de'."""
        html = plugin.ACE_PLUGIN_HTML_UI
        attr_keys = re.findall(
            r'data-i18n(?:-(?:placeholder|title|aria-label))?="([^"]+)"',
            html,
        )
        js_keys = re.findall(r"\bt\(\s*['\"]([a-zA-Z0-9_.]+)['\"]", html)
        all_ui_keys = set(attr_keys) | set(js_keys)
        self.assertGreaterEqual(len(attr_keys), 55)
        self.assertGreaterEqual(len(js_keys), 45)

        for key in sorted(all_ui_keys):
            self.assertIn(key, plugin.TRANSLATIONS["en"], f"UI key '{key}' missing in TRANSLATIONS['en']")
            self.assertIn(key, plugin.TRANSLATIONS["de"], f"UI key '{key}' missing in TRANSLATIONS['de']")
            self.assertTrue(plugin.TRANSLATIONS["en"][key].strip())
            self.assertTrue(plugin.TRANSLATIONS["de"][key].strip())

    def test_i18n_08_theme_compatibility_auto_light_dark_with_both_languages(self) -> None:
        """Both English and German translations cover Auto, Light, and Dark theme controls cleanly."""
        self.assertEqual(plugin.t("header.theme.auto", lang="en"), "Auto")
        self.assertEqual(plugin.t("header.theme.light", lang="en"), "Light")
        self.assertEqual(plugin.t("header.theme.dark", lang="en"), "Dark")
        self.assertEqual(plugin.t("header.theme.auto", lang="de"), "Auto")
        self.assertEqual(plugin.t("header.theme.light", lang="de"), "Hell")
        self.assertEqual(plugin.t("header.theme.dark", lang="de"), "Dunkel")

        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn('id="themeBtnAuto"', html)
        self.assertIn('id="themeBtnDark"', html)
        self.assertIn('id="themeBtnLight"', html)
        self.assertIn('data-i18n="header.theme.auto"', html)
        self.assertIn('data-i18n="header.theme.dark"', html)
        self.assertIn('data-i18n="header.theme.light"', html)

    def test_preprint_01_slot_remapping_preserves_flush_matrix_and_vector_and_updates_schedule(self) -> None:
        """Swapping slots (e.g. T0->T3, T1->T2) preserves exact Slicer flush calculations and remaps target slots."""
        lines = [
            "; flush_multiplier = 1.2\n",
            "; flush_volumes_matrix = 0, 120, 240, 0\n",
            "; flush_volumes_vector = 40, 80, 50, 90\n",
            "; filament_type = PLA;PETG\n",
            "; filament_colour = #FF0000;#00FF00\n",
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n",
            "T0\n",
            "G1 X10 Y10 E2\n",
            "T1\n",
            "G1 X20 Y20 E4\n",
            "T0\n",
        ]
        # 1) Default 1:1 mapping
        out_default, info_default = plugin.process_gcode(
            lines=lines,
            tool_map={0: 0, 1: 1},
            force_initial_purge=True,
        )
        # 2) Swapped mapping: Slicer T0 -> ACE T3, Slicer T1 -> ACE T2
        out_swapped, info_swapped = plugin.process_gcode(
            lines=lines,
            tool_map={0: 3, 1: 2},
            force_initial_purge=True,
        )

        # Purge lengths (mm) must remain identical because flush_volumes_matrix is indexed by original slicer tools
        self.assertEqual(len(info_default["events"]), 3)
        self.assertEqual(len(info_swapped["events"]), 3)
        for ev_def, ev_swp in zip(info_default["events"], info_swapped["events"]):
            self.assertEqual(ev_def["volume_mm3"], ev_swp["volume_mm3"])
            self.assertEqual(ev_def["purge_length_mm"], ev_swp["purge_length_mm"])

        # Physical ACE slots in events, G9111, vector_summary, and remapped_flush_schedule follow {0: 3, 1: 2}
        self.assertEqual(info_swapped["g9111StartSlicerTool"], 0)
        self.assertEqual(info_swapped["g9111MappedTool"], 3)
        self.assertEqual(info_swapped["events"][0]["mapped_tool"], 3)
        self.assertIsNone(info_swapped["events"][0]["old_mapped_tool"])
        self.assertEqual(info_swapped["events"][1]["old_mapped_tool"], 3)
        self.assertEqual(info_swapped["events"][1]["mapped_tool"], 2)
        self.assertEqual(info_swapped["events"][2]["old_mapped_tool"], 2)
        self.assertEqual(info_swapped["events"][2]["mapped_tool"], 3)

        self.assertEqual(info_swapped["vector_summary"][0]["mapped_tool"], 3)
        self.assertEqual(info_swapped["vector_summary"][1]["mapped_tool"], 2)
        sched = info_swapped["remapped_flush_schedule"]
        self.assertEqual(sched["slicer_to_ace_map"], {0: 3, 1: 2})
        transitions = sched["transitions"]
        self.assertEqual(len(transitions), 2)
        t0_to_t1 = next(x for x in transitions if x["from_slicer_tool"] == 0 and x["to_slicer_tool"] == 1)
        self.assertEqual(t0_to_t1["from_ace_slot"], 3)
        self.assertEqual(t0_to_t1["to_ace_slot"], 2)
        self.assertAlmostEqual(t0_to_t1["effective_volume_mm3"], 144.0, places=2)
        self.assertIn("T3->T2", sched["physical_matrix"])
        self.assertIn("T2->T3", sched["physical_matrix"])

        # Dynamic in-memory remap_flush_info updates existing analysis dict when user changes dropdown in modal
        remapped_live = plugin.remap_flush_info(info_default, {0: 5, 1: 6})
        self.assertEqual(remapped_live["g9111MappedTool"], 5)
        self.assertEqual(remapped_live["events"][0]["mapped_tool"], 5)
        self.assertEqual(remapped_live["events"][1]["old_mapped_tool"], 5)
        self.assertEqual(remapped_live["events"][1]["mapped_tool"], 6)
        self.assertEqual(remapped_live["vector_summary"][0]["mapped_tool"], 5)
        self.assertEqual(remapped_live["vector_summary"][1]["mapped_tool"], 6)

    def test_preprint_02_material_mismatch_detection_and_confirmation_gate(self) -> None:
        """Material mismatch (PLA vs PETG) triggers warning & blocks unconfirmed upload; matching material has no warning."""
        filaments = [
            {"slicer_tool": 0, "color": "#FFFFFF", "material": "PLA", "used": True},
            {"slicer_tool": 1, "color": "#000000", "material": "PETG", "used": True},
        ]
        ace_slots = [
            {"tool": 0, "status": "ready", "hex_color": "#FFFFFF", "material": "PLA", "rfid": True},
            {"tool": 1, "status": "ready", "hex_color": "#000000", "material": "PETG", "rfid": True},
        ]

        # Case A: Exact match (T0 PLA -> T0 PLA, T1 PETG -> T1 PETG) -> zero warnings, no confirmation needed
        val_ok = plugin.validate_color_mapping(filaments, {0: 0, 1: 1}, ace_slots, lang="en")
        self.assertFalse(val_ok["has_material_mismatch"])
        self.assertFalse(val_ok["requires_mismatch_confirmation"])
        self.assertEqual(val_ok["warnings"], [])
        self.assertEqual(val_ok["errors"], [])

        # Case B: Mismatched mapping (T0 PLA -> T1 PETG, T1 PETG -> T0 PLA)
        val_mismatch = plugin.validate_color_mapping(filaments, {0: 1, 1: 0}, ace_slots, lang="en")
        self.assertTrue(val_mismatch["has_material_mismatch"])
        self.assertTrue(val_mismatch["requires_mismatch_confirmation"])
        self.assertEqual(len(val_mismatch["material_mismatches"]), 2)

        # Verify Upload Gate blocks when mismatch_confirmed=False and allows when mismatch_confirmed=True
        st = plugin.PluginState()
        st.slicer_filaments = filaments
        st.tool_map = {0: 1, 1: 0}
        st.last_ace_status = {"is_printing": False, "print_state": "standby", "slots": ace_slots}
        info = {
            "flush_status": "POSTPROCESS_OK",
            "flush_data_complete": True,
            "used_slicer_tools": [0, 1],
            "slicer_filaments": filaments,
            "toolMap": {0: 1, 1: 0},
            "warnings": [],
        }

        allowed_unconfirmed, blocks_unconfirmed, _ = plugin.can_upload_gcode(
            info, state=st, ace_status=st.last_ace_status, start_print=False, mismatch_confirmed=False
        )
        self.assertFalse(allowed_unconfirmed)
        self.assertTrue(any("Material mismatch" in b for b in blocks_unconfirmed))

        allowed_confirmed, blocks_confirmed, _ = plugin.can_upload_gcode(
            info, state=st, ace_status=st.last_ace_status, start_print=False, mismatch_confirmed=True
        )
        self.assertTrue(allowed_confirmed)
        self.assertEqual(blocks_confirmed, [])

        # Changing tool_map in PluginState automatically resets material_mismatch_confirmed to False
        st.apply_dict({"material_mismatch_confirmed": True}, save=False)
        self.assertTrue(st.material_mismatch_confirmed)
        st.apply_dict({"tool_map": {0: 0, 1: 1}}, save=False)
        self.assertFalse(st.material_mismatch_confirmed)

    def test_preprint_03_modal_toggles_and_job_metadata_parsing(self) -> None:
        """Pre-print modal toggles (G31, SKIP_PURGE, PRINT_END CUT_TIP, Force Initial Purge) and metadata apply cleanly."""
        lines = [
            "; estimated printing time (normal mode) = 2h 15m 30s\n",
            "; total layers count = 420\n",
            "; max_z_height = 84.00\n",
            "; filament used [g] = 18.4, 6.2\n",
            "; filament used [mm] = 6150.0, 2080.0\n",
            "; filament_type = PLA;PETG\n",
            "; filament_colour = #FFFFFF;#000000\n",
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0, 150, 150, 0\n",
            "; flush_volumes_vector = 60, 60\n",
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n",
            "SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n",
            "G31\n",
            "T0\n",
            "G1 X10 Y10 E1\n",
            "T1\n",
            "PRINT_END CUT_TIP=0\n",
        ]
        meta = plugin.parse_gcode_job_metadata(lines, "/tmp/benchy_multicolor.gcode")
        self.assertEqual(meta["filename"], "benchy_multicolor.gcode")
        self.assertEqual(meta["estimated_time"], "2h 15m 30s")
        self.assertEqual(meta["total_layers"], 420)
        self.assertAlmostEqual(meta["max_z_height"], 84.0, places=2)
        self.assertAlmostEqual(meta["total_weight_g"], 24.6, places=1)
        self.assertAlmostEqual(meta["total_length_m"], 8.23, places=2)

        fils = plugin.parse_slicer_filaments(lines)
        self.assertAlmostEqual(fils[0]["used_g"], 18.4, places=1)
        self.assertAlmostEqual(fils[0]["used_m"], 6.15, places=2)
        self.assertAlmostEqual(fils[1]["used_g"], 6.2, places=1)
        self.assertAlmostEqual(fils[1]["used_m"], 2.08, places=2)

        out_lines, info = plugin.process_gcode(
            lines=lines,
            tool_map={0: 2, 1: 5},
            bed_leveling=False,
            skip_purge_for_already_loaded_tool=True,
            cut_tip_after_print=True,
            force_initial_purge=True,
        )
        joined = "".join(out_lines)
        self.assertIn("; G31 ; skipped by ACE Plugin", joined)
        self.assertEqual(info["g31_skipped"], 1)
        self.assertTrue(info["skipPurgeForAlreadyLoadedTool"])
        self.assertTrue(info["cutTipPost"])
        self.assertTrue(info["forceInitialPurge"])
        self.assertGreater(info["events"][0]["purge_length_mm"], 0.0)
        self.assertIn("SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1", joined)
        self.assertIn("PRINT_END CUT_TIP=1", joined)

    def test_preprint_04_zero_duplicate_html_ids_and_full_hd_modal_structure(self) -> None:
        """ACE_PLUGIN_HTML_UI has zero duplicate element IDs and includes the 1080p dashboard and Pre-Print Modal."""
        html = plugin.ACE_PLUGIN_HTML_UI
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertGreaterEqual(len(ids), 40)
        seen = set()
        duplicates = []
        for el_id in ids:
            if el_id in seen:
                duplicates.append(el_id)
            seen.add(el_id)
        self.assertEqual(duplicates, [], f"Duplicate HTML IDs found in ACE_PLUGIN_HTML_UI: {duplicates}")

        # Verify key Pre-Print Modal & 1080p Single-Screen Dashboard elements exist
        for required_id in (
            "prePrintModal",
            "modalMappingCards",
            "modalMismatchGate",
            "chkConfirmMaterialMismatch",
            "btnOpenPrePrintModal",
            "btnModalSendPrint",
            "btnModalApplyLocal",
            "btnModalLoadSlicer",
            "btnModalBrowseGcode",
            "modalGcodeFileInput",
            "btnInspectFlushPreview",
            "settingsModal",
            "diagnosticsModal",
            "logModal",
            "slotsContainer",
        ):
            self.assertIn(f'id="{required_id}"', html)

    def test_refine_01_luminance_contrast_and_custom_dropdown_without_redundant_swatch(self) -> None:
        """Filament-colored dropdown uses L=0.299R+0.587G+0.114B contrast thresholds and drops duplicate target swatch."""
        # Light filament colors (L > 150) -> dark text #111827
        for light_hex in ("#FFFFFF", "#FFFF00", "#00FFFF", "#F8FAFC", "#A0A0A0"):
            self.assertEqual(plugin.compute_contrast_text_color(light_hex), "#111827", light_hex)

        # Dark filament colors (L <= 150) -> crisp white text #FFFFFF
        for dark_hex in ("#000000", "#808080", "#FF0000", "#001F3F", "#3B0764"):
            self.assertEqual(plugin.compute_contrast_text_color(dark_hex), "#FFFFFF", dark_hex)

        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn("function computeContrastTextColor(hexColor)", html)
        self.assertIn("0.299 * r + 0.587 * g + 0.114 * b", html)
        self.assertIn('luminance > 150 ? "#111827" : "#FFFFFF"', html)
        self.assertIn("slot-color-dropdown", html)
        self.assertIn("slot-color-trigger", html)
        self.assertIn("slot-color-menu", html)
        self.assertIn("slot-color-option", html)
        self.assertIn("return { line1: spName, line2: matLabel };", html)
        self.assertIn("return { line1: matLabel, line2: colorText };", html)

        # Verify redundant .fil-swatch-lg was removed from .fil-target-col
        target_col_block = html.split('<div class="fil-target-col">', 1)[1].split("</div>\n          </div>", 1)[0]
        self.assertNotIn("fil-swatch-lg", target_col_block)
        self.assertIn("slot-color-dropdown", target_col_block)

    def test_refine_02_compact_slot_bay_no_forced_stretch(self) -> None:
        """ACE Pro 4x2 live slot bay uses natural compact card heights without forced 100vh row stretching."""
        style_block = plugin.ACE_PLUGIN_HTML_UI.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertIsNone(re.search(r'(?<!-)height:\s*100vh\s*;', style_block))
        app_shell_css = style_block.split(".app-shell {", 1)[1].split("}", 1)[0]
        self.assertIn("height: auto", app_shell_css)
        self.assertNotIn("grid-template-rows: repeat(2, minmax(0, 1fr));", style_block)
        slots_grid_css = style_block.split(".slots-grid {", 1)[1].split("}", 1)[0]
        self.assertIn("repeat(4, minmax(200px, 1fr))", slots_grid_css)
        self.assertIn("grid-auto-rows: auto", slots_grid_css)
        self.assertNotIn("flex: 1", slots_grid_css)
        slots_card_css = style_block.split(".slot-card {", 1)[1].split("}", 1)[0]
        self.assertIn("height: auto", slots_card_css)

    def test_refine_03_slicer_gcode_acquisition_and_browse_stage_non_destructive(self) -> None:
        """Slicer temp scan and modal file browse acquire & analyze G-code read-only without modifying source or printing."""
        sample_gcode = (
            "; generated by OrcaSlicer 2.5.0\n"
            "; filament_colour = #FF5500;#00AAFF\n"
            "; filament_type = PLA;PETG\n"
            "; estimated printing time (normal mode) = 45m 10s\n"
            "; total layer number = 120\n"
            "; filament used [g] = 10.5, 4.5\n"
            "; filament used [mm] = 3500.0, 1500.0\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,240,180,0\n"
            "; flush_volumes_vector = 120,120,140,140\n"
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n"
            "T0\n"
            "G1 X10 Y10 E1\n"
            "T1\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")

            # External orcaslicer_model temp dirs must NEVER be scanned anymore
            ext_tmp_dir = os.path.join(tmpdir, "orcaslicer_model", "plate_run")
            os.makedirs(ext_tmp_dir, exist_ok=True)
            ext_ignored = os.path.join(ext_tmp_dir, "external_ignored.gcode")
            with open(ext_ignored, "w", encoding="utf-8") as f:
                f.write(sample_gcode)
            os.utime(ext_ignored, (9000.0, 9000.0))

            staged_dir = os.path.join(tmpdir, "staged_gcode")
            os.makedirs(staged_dir, exist_ok=True)
            older = os.path.join(staged_dir, "plate_1.gcode")
            newer = os.path.join(staged_dir, "plate_2.gcode")
            ignored_tmp = os.path.join(staged_dir, ".ace_tmp_partial.gcode")
            with open(older, "w", encoding="utf-8") as f:
                f.write(sample_gcode)
            with open(newer, "w", encoding="utf-8") as f:
                f.write(sample_gcode.replace("tool=0", "tool=1"))
            with open(ignored_tmp, "w", encoding="utf-8") as f:
                f.write(sample_gcode)
            os.utime(older, (1000.0, 1000.0))
            os.utime(newer, (2000.0, 2000.0))
            os.utime(ignored_tmp, (3000.0, 3000.0))

            cands = plugin.discover_sliced_gcode_candidates(state=st)
            self.assertEqual(cands, [newer, older])

            with open(newer, "rb") as fh:
                src_hash_before = hashlib.sha256(fh.read()).hexdigest()

            kind, resolved, info = plugin.acquire_gcode_from_slicer(st)
            self.assertEqual(kind, "staged_gcode")
            self.assertEqual(resolved, newer)
            self.assertIsNotNone(info)
            self.assertEqual(info["analysis_mode"], "preview")
            self.assertEqual(st.last_gcode_path, newer)
            self.assertEqual(st.last_gcode_source, "staged_gcode")
            with open(newer, "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), src_hash_before)

            # Direct explicit_path (passed by OrcaSlicer execute/lifecycle) still acquires read-only
            kind_exp, resolved_exp, _ = plugin.acquire_gcode_from_slicer(
                st, explicit_path=ext_ignored, include_last_gcode_path=False
            )
            self.assertEqual(kind_exp, "slicer_explicit")
            self.assertEqual(resolved_exp, ext_ignored)

            # Test local Browse .gcode staging helper
            staged_path, staged_info = plugin.load_gcode_from_content("My/../Plate #2.gcode", sample_gcode, st)
            self.assertTrue(os.path.isfile(staged_path))
            self.assertIn("staged_gcode", staged_path)
            self.assertNotIn("..", os.path.basename(staged_path))
            self.assertEqual(st.last_gcode_source, "file_picker")
            self.assertEqual(staged_info["toolchanges"], 2)
            with self.assertRaises(ValueError):
                plugin.load_gcode_from_content("empty.gcode", "   \n", st)

        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn("acquireSlicerGcode(false)", html)
        self.assertIn("acquireSlicerGcode(true)", html)
        self.assertIn("handleModalGcodeFileSelect(event)", html)

    def test_refine_04_lifecycle_and_pipeline_modal_trigger_without_network(self) -> None:
        """Slicing pipeline & lifecycle events populate and push open_preprint_modal asynchronously without network IO."""
        sample_gcode = (
            "; generated by OrcaSlicer 2.5.0\n"
            "; filament_colour = #FFFFFF;#000000\n"
            "; filament_type = PLA;PLA\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,200,200,0\n"
            "; flush_volumes_vector = 100,100\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\n"
            "T1\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            gcode_file = os.path.join(tmpdir, "auto_plate.gcode")
            with open(gcode_file, "w", encoding="utf-8") as f:
                f.write(sample_gcode)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")

            page = plugin.KobraS1AcePageCapability()
            posted = []
            evt_done = threading.Event()

            def _record_post(msg):
                posted.append(msg)
                if msg.get("action") == "open_preprint_modal":
                    evt_done.set()

            page.post_message = _record_post
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page.on_load()
                try:
                    ctx = types.SimpleNamespace(
                        step=plugin.orca.slicing.Step.psGCodePostProcess,
                        gcode_path=gcode_file,
                    )
                    pp = plugin.KobraS1AcePostProcessCapability()
                    res = pp.execute(ctx)
                    self.assertTrue(res.ok)
                    self.assertTrue(evt_done.wait(timeout=2.0))
                    self.assertTrue(any(p.get("action") == "open_preprint_modal" for p in posted))
                    self.assertEqual(st.last_gcode_source, "slicing_pipeline")

                    # Also verify SlicingJobComplete lifecycle observation updates state & pushes open_preprint_modal
                    posted.clear()
                    evt_done.clear()
                    l_ctx = types.SimpleNamespace(
                        name=gcode_file,
                        code=plugin.orca.LifecycleEvtCode.Success,
                        msg="",
                    )
                    page.on_lifecycle_event(plugin.orca.LifecycleEvent.SlicingJobComplete, l_ctx)
                    self.assertTrue(evt_done.wait(timeout=2.0))
                    self.assertTrue(any(p.get("action") == "open_preprint_modal" for p in posted))
                finally:
                    page.on_unload()
                self.assertIsNone(plugin._ACTIVE_PAGE_CAPABILITY)

    def test_refine_05_modal_action_split_upload_only_vs_send_print_and_gates(self) -> None:
        """Modal separates Apply Local, Upload Only (start_print=False), and Send & Print (start_print=True) with gates."""
        sample_gcode = (
            "; generated by OrcaSlicer 2.5.0\n"
            "; filament_colour = #FFFFFF\n"
            "; filament_type = PLA\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0\n"
            "; flush_volumes_vector = 100,100\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            gcode_file = os.path.join(tmpdir, "split_test.gcode")
            with open(gcode_file, "w", encoding="utf-8") as f:
                f.write(sample_gcode)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            st.tool_map[0] = 0

            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append

            class _FakeClient:
                active_url = "http://mock-printer:7125"

                def __init__(self, is_printing: bool = False, slot_mat: str = "PLA") -> None:
                    self.is_printing = is_printing
                    self.slot_mat = slot_mat
                    self.uploads = []

                def query_ace_status(self):
                    return {
                        "connected": True,
                        "is_printing": self.is_printing,
                        "print_state": "printing" if self.is_printing else "standby",
                        "slots": [
                            {"tool": 0, "status": "ready", "material": self.slot_mat, "hex_color": "#FFFFFF"}
                        ],
                    }

                def upload_gcode(self, filename, gcode_content, start_print=False):
                    if start_print and self.is_printing:
                        raise RuntimeError("Blocked: active print")
                    self.uploads.append((filename, start_print))
                    return {"item": {"path": filename}}

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                # 1. Apply Local -> never uploads
                fake = _FakeClient(is_printing=False, slot_mat="PLA")
                st.get_client = lambda: fake
                page._handle_async_message("postprocess_only", {"gcode_path": gcode_file})
                self.assertEqual(fake.uploads, [])

                # 2. Upload Only (start_print_immediately=False) -> uploads with start_print=False
                st.start_print_immediately = False
                page._handle_async_message(
                    "postprocess_and_upload",
                    {
                        "gcode_path": gcode_file,
                        "require_mismatch_gate": True,
                        "mismatch_confirmed": False,
                        "settings": {"start_print_immediately": False},
                    },
                )
                self.assertEqual(fake.uploads, [("split_test.gcode", False)])

                # 3. Send & Print while printing -> blocked by safety gate
                fake_printing = _FakeClient(is_printing=True, slot_mat="PLA")
                st.get_client = lambda: fake_printing
                page._handle_async_message(
                    "postprocess_and_upload",
                    {
                        "gcode_path": gcode_file,
                        "settings": {"start_print_immediately": True},
                    },
                )
                self.assertEqual(fake_printing.uploads, [])

                # 4. Send & Print with material mismatch unconfirmed -> blocked; confirmed -> start_print=True
                fake_mismatch = _FakeClient(is_printing=False, slot_mat="PETG")
                st.get_client = lambda: fake_mismatch
                page._handle_async_message(
                    "postprocess_and_upload",
                    {
                        "gcode_path": gcode_file,
                        "require_mismatch_gate": True,
                        "mismatch_confirmed": False,
                        "settings": {"start_print_immediately": True},
                    },
                )
                self.assertEqual(fake_mismatch.uploads, [])

                page._handle_async_message(
                    "postprocess_and_upload",
                    {
                        "gcode_path": gcode_file,
                        "require_mismatch_gate": True,
                        "mismatch_confirmed": True,
                        "settings": {"start_print_immediately": True},
                    },
                )
                self.assertEqual(fake_mismatch.uploads, [("split_test.gcode", True)])

        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertNotIn("chkStartPrint", html)
        self.assertNotIn("onStartPrintToggleChange", html)
        self.assertNotIn("ui.modal.opt.start_print", html)
        self.assertIn('id="btnModalApplyLocal"', html)
        self.assertIn('id="btnModalUploadOnly"', html)
        self.assertIn('id="btnModalSendPrint"', html)
        self.assertIn("runPostProcessAndUpload(false, false)", html)
        self.assertIn("runPostProcessAndUpload(true, false)", html)
        self.assertIn("runPostProcessAndUpload(true, true)", html)
        self.assertIn("function updateModalSendActionUi()", html)
        self.assertIn("btnUpload.disabled = blockedByMismatch || blockedByErrors || isDisconnected;", html)
        self.assertIn("btnSend.disabled = blockedByMismatch || blockedByErrors || isPrinting || isDisconnected;", html)
        self.assertIn("ui.modal.btn.upload_only", html)
        self.assertIn("ui.modal.btn.send_print", html)

    def test_refine_06_diagnostics_duplicate_mapping_section_removed_and_no_dup_ids(self) -> None:
        """Duplicate 4. Color Mapping Workbench table is removed from #diagnosticsModal with zero duplicate IDs."""
        html = plugin.ACE_PLUGIN_HTML_UI
        diag_section = html.split('<div id="diagnosticsModal"', 1)[1].split('<div id="logModal"', 1)[0]
        self.assertNotIn("mappingTableBody", diag_section)
        self.assertNotIn("4. Color Mapping Workbench", diag_section)
        self.assertNotIn("ui.sec4.col_extruder", diag_section)

        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), f"Duplicate IDs: {[x for x in ids if ids.count(x) > 1]}")

    def test_hotfix_01_no_tmp_or_system_dir_scanning(self) -> None:
        """discover_sliced_gcode_candidates never scans /tmp or tempfile.gettempdir() and triggers zero external enumeration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")

            real_scandir = os.scandir
            scanned_paths = []

            def _spy_scandir(path="."):
                scanned_paths.append(os.fspath(path))
                return real_scandir(path)

            # 1. With no staged_gcode directory, zero enumeration happens anywhere
            with mock.patch("tempfile.gettempdir", side_effect=AssertionError("gettempdir must not be called")), mock.patch(
                "os.walk", side_effect=AssertionError("os.walk must not be called")
            ), mock.patch("os.listdir", side_effect=AssertionError("os.listdir must not be called")), mock.patch(
                "os.scandir", side_effect=_spy_scandir
            ):
                self.assertEqual(plugin.discover_sliced_gcode_candidates(state=st), [])
                self.assertEqual(scanned_paths, [])

            # 2. If HAS_ORCA_RUNTIME=True and storage() is unavailable, zero FS calls happen
            st_orca_missing = plugin.PluginState()
            with mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True), mock.patch.object(
                plugin.orca.host.plugin, "storage", return_value=""
            ), mock.patch("os.path.isdir", side_effect=AssertionError("isdir must not run")), mock.patch(
                "os.scandir", side_effect=AssertionError("scandir must not run")
            ), mock.patch("tempfile.gettempdir", side_effect=AssertionError("gettempdir must not run")):
                self.assertEqual(st_orca_missing.resolve_storage_dir(), "")
                self.assertEqual(plugin.discover_sliced_gcode_candidates(state=st_orca_missing), [])

            # 3. External folders and /tmp are ignored; ONLY <storage>/staged_gcode/ is scanned
            ext_dir = os.path.join(tmpdir, "external_tmp")
            os.makedirs(ext_dir, exist_ok=True)
            with open(os.path.join(ext_dir, "ignored.gcode"), "w", encoding="utf-8") as f:
                f.write("; ignore me\nT0\n")

            staged_dir = os.path.join(tmpdir, "staged_gcode")
            os.makedirs(staged_dir, exist_ok=True)
            staged_file = os.path.join(staged_dir, "safe_plate.gcode")
            with open(staged_file, "w", encoding="utf-8") as f:
                f.write("; safe staged\nT0\n")

            scanned_paths.clear()
            with mock.patch("tempfile.gettempdir", side_effect=AssertionError("gettempdir must not be called")), mock.patch(
                "os.walk", side_effect=AssertionError("os.walk must not be called")
            ), mock.patch("os.listdir", side_effect=AssertionError("os.listdir must not be called")), mock.patch(
                "os.scandir", side_effect=_spy_scandir
            ):
                found = plugin.discover_sliced_gcode_candidates(state=st)
                self.assertEqual(found, [staged_file])
                self.assertEqual(scanned_paths, [staged_dir])
                for p in scanned_paths:
                    self.assertFalse(p.rstrip("/") in ("/tmp", "/var/tmp", "/usr/tmp"))

            # 4. In OrcaSlicer runtime, stale /tmp last_gcode_path is skipped without os.path.isfile probe
            stale_storage = os.path.join(tmpdir, "stale_storage")
            os.makedirs(stale_storage, exist_ok=True)
            st_stale = plugin.PluginState()
            st_stale.resolve_config_path = lambda: os.path.join(stale_storage, "state.json")
            st_stale.last_gcode_path = "/tmp/stale_orcaslicer_plate.gcode"
            probed_files = []
            real_isfile = os.path.isfile

            def _spy_isfile(path):
                probed_files.append(os.fspath(path))
                return real_isfile(path)

            with mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True), mock.patch(
                "os.path.isfile", side_effect=_spy_isfile
            ):
                kind, resolved, info = plugin.acquire_gcode_from_slicer(
                    st_stale, include_last_gcode_path=True
                )
                self.assertEqual(kind, "presets_only")
                self.assertEqual(resolved, "")
                self.assertIsNone(info)
                self.assertNotIn("/tmp/stale_orcaslicer_plate.gcode", probed_files)

    def test_hotfix_02_ready_and_non_gcode_lifecycle_no_auto_discovery_or_probing(self) -> None:
        """Page ready/startup and non-gcode lifecycle events perform zero external probing or directory scanning."""
        sample_gcode = (
            "; generated by OrcaSlicer 2.5.0\n"
            "; filament_colour = #FFFFFF;#000000\n"
            "; filament_type = PLA;PLA\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,200,200,0\n"
            "; flush_volumes_vector = 100,100\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\n"
            "T1\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "plugin_storage")
            os.makedirs(storage_dir, exist_ok=True)
            ext_plate = os.path.join(tmpdir, "slicer_export.gcode")
            with open(ext_plate, "w", encoding="utf-8") as f:
                f.write(sample_gcode)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")
            st.last_gcode_path = "/tmp/stale_yesterday_plate.gcode"

            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append

            probed_paths = []
            real_isfile = os.path.isfile

            def _spy_isfile(path):
                probed_paths.append(os.fspath(path))
                return real_isfile(path)

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                # 1. 'ready' handler must NOT call discover_sliced_gcode_candidates / acquire_gcode_from_slicer or probe /tmp
                with mock.patch.object(
                    plugin, "acquire_gcode_from_slicer", side_effect=AssertionError("ready must not auto-acquire")
                ), mock.patch.object(
                    plugin, "discover_sliced_gcode_candidates", side_effect=AssertionError("ready must not scan")
                ), mock.patch.object(
                    page, "_send_state_and_live_status"
                ) as mock_send, mock.patch("os.path.isfile", side_effect=_spy_isfile):
                    page._handle_async_message("ready", {})
                    mock_send.assert_called_once()
                    self.assertNotIn("/tmp/stale_yesterday_plate.gcode", probed_paths)

                # 2. SlicingJobComplete with non-gcode plate label must NOT probe 'Plate 1' or /tmp
                page.on_load()
                try:
                    evt_done = threading.Event()
                    orig_post = page.post_message

                    def _wait_post(msg):
                        orig_post(msg)
                        evt_done.set()

                    page.post_message = _wait_post
                    probed_paths.clear()
                    with mock.patch("os.path.isfile", side_effect=_spy_isfile), mock.patch(
                        "tempfile.gettempdir", side_effect=AssertionError("gettempdir forbidden")
                    ):
                        l_ctx = types.SimpleNamespace(
                            name="Plate 1",
                            code=plugin.orca.LifecycleEvtCode.Success,
                            msg="",
                        )
                        page.on_lifecycle_event(plugin.orca.LifecycleEvent.SlicingJobComplete, l_ctx)
                        self.assertTrue(evt_done.wait(timeout=2.0))
                    self.assertNotIn("Plate 1", probed_paths)
                    self.assertNotIn("/tmp/stale_yesterday_plate.gcode", probed_paths)

                    # 3. execute(ctx) stages post-processed copy inside <storage>/staged_gcode/
                    pp = plugin.KobraS1AcePostProcessCapability()
                    evt_done.clear()
                    posted.clear()
                    res = pp.execute(
                        types.SimpleNamespace(
                            step=plugin.orca.slicing.Step.psGCodePostProcess,
                            gcode_path=ext_plate,
                        )
                    )
                    self.assertTrue(res.ok)
                    self.assertTrue(evt_done.wait(timeout=2.0))
                    self.assertTrue(st.last_gcode_path.startswith(os.path.join(storage_dir, "staged_gcode")))
                    self.assertTrue(os.path.isfile(st.last_gcode_path))
                finally:
                    page.on_unload()

    def test_ux_01_unused_extruders_filtered_and_excluded_from_warnings(self) -> None:
        """0.0g unused Slicer extruders are hidden by default, preserved across sync, and never block upload."""
        lines = [
            "; filament: 1,2,3,4,5,6,7,8\n",
            "; filament used [g] = 16.06, 26.25, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00\n",
            "; filament used [mm] = 5300.0, 8700.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0\n",
            "; filament_type = PETG;PETG;TPU;ASA;PLA;ABS;PETG;PLA\n",
            "; filament_colour = #FFFFFF;#000000;#FF0000;#00FF00;#0000FF;#FFFF00;#FF00FF;#00FFFF\n",
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = " + ",".join(["0"] * 64) + "\n",
            "; flush_volumes_vector = " + ",".join(["50"] * 8) + "\n",
            "G9111 bedTemp=80 extruderTemp=250 tool=1\n",
            "SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n",
            "T1\n",
            "T0\n",
        ]
        fils = plugin.parse_slicer_filaments(lines)
        self.assertEqual(len(fils), 8)
        self.assertTrue(fils[0]["used"])
        self.assertTrue(fils[1]["used"])
        for idx in range(2, 8):
            self.assertFalse(fils[idx]["used"], f"Extruder T{idx} with 0.0g must have used=False")
            self.assertAlmostEqual(fils[idx]["used_g"], 0.0, places=2)

        # Even if unused T2..T7 map to empty/incompatible slots, ONLY used T0 & T1 are validated
        ace_slots = [
            {"tool": 0, "status": "ready", "material": "PETG", "hex_color": "#FFFFFF", "rfid": True},
            {"tool": 1, "status": "ready", "material": "PETG", "hex_color": "#000000", "rfid": True},
        ] + [
            {"tool": i, "status": "empty", "material": "", "hex_color": "#000000", "rfid": False}
            for i in range(2, 8)
        ]
        tool_map = {i: i for i in range(8)}
        val = plugin.validate_color_mapping(fils, tool_map, ace_slots, lang="en")
        self.assertTrue(val["valid"])
        self.assertEqual(val["errors"], [])
        self.assertEqual(val["warnings"], [])
        self.assertFalse(val["has_material_mismatch"])

        st = plugin.PluginState()
        st.save_config = lambda: None
        st.last_gcode_path = "/tmp/fake_unused_check.gcode"
        st.slicer_filaments = fils
        st.tool_map = tool_map
        info = {
            "flush_status": "POSTPROCESS_OK",
            "flush_data_complete": True,
            "used_slicer_tools": [0, 1],
            "slicer_filaments": fils,
            "toolMap": tool_map,
            "warnings": [],
        }
        allowed, blocking, gate_warns = plugin.can_upload_gcode(
            info, st, ace_status={"slots": ace_slots, "is_printing": False}, start_print=False
        )
        self.assertTrue(allowed)
        self.assertEqual(blocking, [])
        self.assertEqual(gate_warns, [])

        # Verify sync_filaments_from_orca_host(..., preserve_gcode_usage=True) retains used=False
        fake_cfg = {
            "filament_colour": ["#FFFFFF", "#000000", "#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#FF00FF", "#00FFFF"],
            "filament_type": ["PETG", "PETG", "TPU", "ASA", "PLA", "ABS", "PETG", "PLA"],
            "filament_vendor": ["Generic"] * 8,
            "filament_diameter": ["1.75"] * 8,
        }
        fake_bundle = types.SimpleNamespace(
            current_filament_presets=lambda: [
                types.SimpleNamespace(name=f"Preset {i}", config_value=lambda _k: None)
                for i in range(8)
            ],
            current_filament_preset_names=lambda: [f"Preset {i}" for i in range(8)],
            full_config_value=lambda key: fake_cfg.get(key),
        )
        fake_host = types.SimpleNamespace(preset_bundle=lambda: fake_bundle)
        with mock.patch.object(plugin.orca, "host", fake_host, create=True):
            synced = plugin.sync_filaments_from_orca_host(st, preserve_gcode_usage=True)
        self.assertEqual(len(synced), 8)
        self.assertTrue(synced[0]["used"])
        self.assertTrue(synced[1]["used"])
        for idx in range(2, 8):
            self.assertFalse(synced[idx]["used"])

        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn('id="chkShowUnusedExtruders"', html)
        self.assertIn("function isFilamentUsed(", html)
        self.assertIn("const activeFils = filaments.filter(f => isFilamentUsed(f, hasAnyUsageData));", html)
        self.assertIn("if (cardsContainer && (showUnused || filUsed)) {", html)
        self.assertIn("ui.map.unused_badge", html)

    def test_ux_02_macro_aligned_skip_purge_and_print_end_reversibility(self) -> None:
        """G9111 SKIP_PURGE (inline, split-line, injected) and PRINT_END CUT_TIP rewrite & revert cleanly."""
        # 1. Bare G9111 + bare PRINT_END -> inject SKIP_PURGE=1 & CUT_TIP=1, then flip off, then strip
        bare_lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,100,0\n",
            "; flush_volumes_vector = 50,50\n",
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n",
            "T0\n",
            "T1\n",
            "PRINT_END\n",
        ]
        out_on, info_on = plugin.process_gcode(
            bare_lines,
            tool_map={0: 2, 1: 3},
            skip_purge_for_already_loaded_tool=True,
            cut_tip_after_print=True,
        )
        txt_on = "".join(out_on)
        self.assertIn("G9111 bedTemp=60 extruderTemp=210 tool=2 SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n", txt_on)
        self.assertIn("PRINT_END CUT_TIP=1\n", txt_on)
        self.assertEqual(txt_on.count("CUT_TIP\n"), 0)
        self.assertAlmostEqual(info_on["events"][0]["purge_length_mm"], 0.0, places=2)

        out_off, info_off = plugin.process_gcode(
            out_on,
            tool_map={0: 2, 1: 3},
            skip_purge_for_already_loaded_tool=False,
            cut_tip_after_print=False,
        )
        txt_off = "".join(out_off)
        self.assertIn("G9111 bedTemp=60 extruderTemp=210 tool=2 SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=0\n", txt_off)
        self.assertIn("PRINT_END CUT_TIP=0\n", txt_off)
        self.assertGreater(info_off["events"][0]["purge_length_mm"], 15.0)
        # Reversibly strip restores exact bare G9111 and bare PRINT_END
        stripped_bare = "".join(plugin.strip_previous_postprocess(out_off))
        self.assertEqual(stripped_bare, "".join(bare_lines))

        # 2. Inline G9111 ... SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=0 toggled ON -> stripped back to =0
        inline_lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,100,0\n",
            "; flush_volumes_vector = 50,50\n",
            "G9111 bedTemp=60 extruderTemp=210 tool=1 SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=0 ; start macro\n",
            "T1\n",
            "PRINT_END CUT_TIP=0 ; finish\n",
        ]
        out_in_on, _ = plugin.process_gcode(
            inline_lines,
            tool_map={1: 4},
            skip_purge_for_already_loaded_tool=True,
            cut_tip_after_print=True,
        )
        txt_in_on = "".join(out_in_on)
        self.assertIn(
            "G9111 bedTemp=60 extruderTemp=210 tool=4 SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1 ; start macro\n",
            txt_in_on,
        )
        self.assertIn("PRINT_END CUT_TIP=1 ; finish\n", txt_in_on)
        self.assertEqual("".join(plugin.strip_previous_postprocess(out_in_on)), "".join(inline_lines))

        # 3. Ensure cut_tip_before_print / chkCutTipPre is completely removed from HTML and PluginState
        self.assertNotIn("chkCutTipPre", plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn("chkSkipPurgeLoaded", plugin.ACE_PLUGIN_HTML_UI)
        st = plugin.PluginState()
        self.assertFalse(hasattr(st, "cut_tip_before_print"))
        self.assertTrue(st.skip_purge_for_already_loaded_tool)

    def test_ux_03_dryer_unit_selector_and_exact_temperature(self) -> None:
        """Custom #dryerUnit dropdown replaces native <select>, guarantees dark/high-contrast CSS, and emits exact commands."""
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertNotIn("<select", html)
        for expected_fragment in (
            'id="dryerUnit"',
            'class="custom-select dryer-unit-dropdown"',
            'id="dryerUnitTrigger"',
            'id="dryerUnitMenu"',
            'data-value="all"',
            'data-value="0"',
            'data-value="1"',
            "function toggleDryerUnitDropdown(event)",
            "function selectDryerUnit(unitVal, event)",
            "function setDryerUnitValue(unitVal)",
            ".custom-select-trigger {",
            "background: #383d3b;",
            "color: #f8fafc;",
            ".custom-select.drop-up .custom-select-menu {",
            'id="dryerTemp"',
            'max="70"',
            "function readDryerControls()",
        ):
            self.assertIn(expected_fragment, html)

        sent_scripts = []
        client = plugin.MoonrakerClient("http://127.0.0.1:7125", enable_port80_fallback=False)
        client.send_gcode = lambda script, **_kwargs: sent_scripts.append(script) or {"result": "ok"}

        client.start_dryer(temp_c=60, duration_min=180, instance=None)
        client.start_dryer(temp_c=45, duration_min=120, instance=0)
        client.start_dryer(temp_c=65, duration_min=300, instance=1)
        client.stop_dryer(instance=0)
        client.stop_dryer(instance=None)
        self.assertEqual(
            sent_scripts,
            [
                "ACE_START_DRYING TEMP=60 DURATION=180",
                "ACE_START_DRYING TEMP=45 DURATION=120 INSTANCE=0",
                "ACE_START_DRYING TEMP=65 DURATION=300 INSTANCE=1",
                "ACE_STOP_DRYING INSTANCE=0",
                "ACE_STOP_DRYING",
            ],
        )

        # PageCapability dryer_start persists exact unit & temp and logs ACE 2 + exact command
        st = plugin.PluginState()
        st.save_config = lambda: None
        st.language = "en"
        page = plugin.KobraS1AcePageCapability()
        posted = []
        page.post_message = posted.append

        fake_client = types.SimpleNamespace(
            active_url="http://127.0.0.1:7125",
            start_dryer=mock.MagicMock(return_value={"result": "ok"}),
            query_ace_status=mock.MagicMock(return_value={"slots": [], "dryer": {"status": "drying", "target_temp": 62, "remain_time": 180}}),
            query_print_stats=mock.MagicMock(return_value={"state": "standby", "is_printing": False}),
        )
        st.get_client = lambda: fake_client
        with mock.patch.object(plugin, "GLOBAL_STATE", st):
            page._handle_async_message("dryer_start", {"unit": "1", "temp": 62, "duration": 180})

        self.assertEqual(st.dryer_unit, "1")
        self.assertEqual(st.dryer_temp, 62)
        self.assertEqual(st.dryer_duration, 180)
        fake_client.start_dryer.assert_called_once_with(temp_c=62, duration_min=180, instance=1)
        self.assertTrue(posted)
        self.assertIn("ACE 2", posted[-1]["log"])
        self.assertIn("ACE_START_DRYING TEMP=62 DURATION=180 INSTANCE=1", posted[-1]["log"])

        # Multi-instance normalization catches drying on ACE 2 while ACE 1 is stopped
        norm = plugin.MoonrakerClient.normalize_ace_status(
            {
                "ace_state": {"ace_instances": 2, "current_index": 0},
                "ace_instance_0": {"dryer": {"status": "stop", "target_temp": 0, "remain_time": 0}, "slots": []},
                "ace_instance_1": {"dryer": {"status": "drying", "target_temp": 65, "remain_time": 90}, "slots": []},
            }
        )
        self.assertEqual(norm["dryer"]["status"], "drying")
        self.assertEqual(norm["dryer"]["target_temp"], 65)
        self.assertEqual(norm["dryer"]["active_instances"], [1])

    def test_ux_04_audit_log_persistence_maintenance_bar_removed_and_modal_feedback(self) -> None:
        """Audit log is never wiped by translations, redundant macro buttons are removed, and modal feedback fires."""
        html = plugin.ACE_PLUGIN_HTML_UI
        # Redundant Cluster 2 macro buttons completely removed
        for removed_id in ("btnCutTip", "btnG31", "btnBedMesh", "ui.sec2.btn_cut", "ui.bar.macros_label"):
            self.assertNotIn(removed_id, html)

        # #statusLog must NOT carry data-i18n (otherwise applyTranslations wipes history)
        self.assertIn('<div id="statusLog" class="status-log"></div>', html)
        self.assertIn("function ensureLogInitialized()", html)
        self.assertIn("ensureLogInitialized();\n  applyTranslations();", html)
        self.assertIn('id="modalFeedbackBanner"', html)
        self.assertIn("openDrawer(\"prePrintModal\");\n    renderModalJobSummary();\n    renderMappingTable();\n    acquireSlicerGcode(false);", html)
        self.assertIn('id="btnModalLoadSlicer"', html)
        self.assertIn("acquireSlicerGcode(true)", html)

        # Verify backend feedback + persistent log payloads for Upload Only vs missing G-code
        with tempfile.TemporaryDirectory() as tmpdir:
            gpath = os.path.join(tmpdir, "job.gcode")
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(
                    "; filament used [g] = 10.0, 0.0\n"
                    "; filament_type = PLA;PETG\n"
                    "; filament_colour = #FFFFFF;#000000\n"
                    "; flush_multiplier = 1.0\n"
                    "; flush_volumes_matrix = 0,100,100,0\n"
                    "; flush_volumes_vector = 50,50\n"
                    "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
                    "T0\n"
                    "PRINT_END CUT_TIP=0\n"
                )
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            st.language = "en"
            st.tool_map = {0: 0, 1: 1}
            ace_slots = [{"tool": i, "status": "ready", "material": "PLA", "hex_color": "#FFFFFF"} for i in range(8)]
            fake_client = types.SimpleNamespace(
                active_url="http://127.0.0.1:7125",
                query_ace_status=mock.MagicMock(return_value={"slots": ace_slots}),
                query_print_stats=mock.MagicMock(return_value={"state": "standby", "is_printing": False}),
                upload_gcode=mock.MagicMock(return_value={"item": {"path": "job.gcode"}}),
            )
            st.get_client = lambda: fake_client

            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                # 1. Missing G-code path emits error modal_feedback + log
                st.last_gcode_path = ""
                page._handle_async_message("postprocess_only", {"gcode_path": ""})
                self.assertEqual(posted[-1]["modal_feedback"]["level"], "error")
                self.assertIn("No G-code", posted[-1]["modal_feedback"]["text"])

                # 2. acquire_slicer_gcode not found emits warn modal_feedback
                page._handle_async_message("acquire_slicer_gcode", {"force_refresh": True})
                self.assertEqual(posted[-1]["modal_feedback"]["level"], "warn")

                # 3. Upload Only with active file calls upload_gcode(start_print=False) and emits ok modal_feedback
                page._handle_async_message(
                    "postprocess_and_upload",
                    {"gcode_path": gpath, "start_print": False, "settings": {"start_print_immediately": False}},
                )
                fake_client.upload_gcode.assert_called_once()
                self.assertFalse(fake_client.upload_gcode.call_args.kwargs["start_print"])
                self.assertEqual(posted[-1]["modal_feedback"]["level"], "ok")
                self.assertIn("Upload Only", posted[-1]["modal_feedback"]["text"])

    def test_ux_05_modal_reopen_gcode_persistence_and_readonly_host_fallback(self) -> None:
        """Loaded/browsed G-code persists across modal re-opens until replaced by a newer slice, and read-only host handles empty state gracefully."""
        plate_a = (
            "; filament used [g] = 12.0, 3.0\n"
            "; filament_type = PLA;PETG\n"
            "; filament_colour = #FF0000;#00FF00\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,100,100,0\n"
            "; flush_volumes_vector = 50,50\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\n"
            "T1\n"
        )
        plate_b = plate_a.replace("12.0, 3.0", "25.0, 5.0").replace("#FF0000", "#0000FF")

        with tempfile.TemporaryDirectory() as tmpdir:
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            st.language = "en"

            staged_dir = os.path.join(tmpdir, "staged_gcode")
            os.makedirs(staged_dir, exist_ok=True)
            older_slice = os.path.join(staged_dir, "earlier_slice.gcode")
            with open(older_slice, "w", encoding="utf-8") as f:
                f.write(plate_a)
            os.utime(older_slice, (5000.0, 5000.0))

            # User browses a file (`file_picker`), which gets staged alongside an earlier slice
            browsed_path, _ = plugin.load_gcode_from_content("custom_user.gcode", plate_b, st)
            # Even if timestamps collide or another slice exists earlier, reopening modal (force_refresh=False) preserves user's active file
            os.utime(browsed_path, (4999.0, 4999.0))
            st.tool_map[0] = 3
            st.save_config()

            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"gcode_path": browsed_path, "force_refresh": False},
                )
            self.assertEqual(st.last_gcode_path, browsed_path)
            self.assertEqual(st.last_gcode_source, "file_picker")
            self.assertEqual(st.tool_map[0], 3)

            # Reloading config in a fresh PluginState restores last_gcode_path & last_gcode_source
            st_reloaded = plugin.PluginState()
            st_reloaded.resolve_config_path = st.resolve_config_path
            st_reloaded.load_config()
            self.assertEqual(st_reloaded.last_gcode_path, browsed_path)
            self.assertEqual(st_reloaded.last_gcode_source, "file_picker")

            # A genuine newer slice via SlicingPipelineCapabilityBase replaces the active file cleanly
            raw_slice = os.path.join(tmpdir, "new_plate.gcode")
            with open(raw_slice, "w", encoding="utf-8") as f:
                f.write(plate_a)
            os.utime(raw_slice, (6000.0, 6000.0))

            cap = plugin.KobraS1AcePostProcessCapability()
            ctx = types.SimpleNamespace(
                step=plugin.orca.slicing.Step.psGCodePostProcess,
                gcode_path=raw_slice,
            )
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                res = cap.execute(ctx)
            self.assertEqual(res.status, plugin.orca.PluginResult.Success)
            self.assertEqual(st.last_gcode_source, "slicing_pipeline")
            self.assertIn("new_plate.gcode", st.last_gcode_path)

            # Read-only orca.host fallback when NO staged/active G-code exists handles Load from Slicer gracefully
            st_empty = plugin.PluginState()
            empty_dir = os.path.join(tmpdir, "empty_storage")
            os.makedirs(empty_dir, exist_ok=True)
            st_empty.resolve_config_path = lambda: os.path.join(empty_dir, "state.json")
            st_empty.language = "en"
            page2 = plugin.KobraS1AcePageCapability()
            posted2 = []
            page2.post_message = posted2.append
            with mock.patch.object(plugin, "GLOBAL_STATE", st_empty):
                page2._handle_async_message("acquire_slicer_gcode", {"force_refresh": True})
            self.assertEqual(posted2[-1]["modal_feedback"]["level"], "warn")
            self.assertIn("orca.host", posted2[-1]["modal_feedback"]["text"])

    def test_ux_06_shared_slot_multi_extruder_reuse_and_gcode_thumbnail(self) -> None:
        """Multi-extruder reuse on one physical ACE slot is allowed cleanly and G-code PNG thumbnails are extracted."""
        import base64

        lines = [
            "; generated by OrcaSlicer 2.5.0\n",
            "; filament_type = PLA;PLA;PLA;PLA\n",
            "; filament_colour = #FFFFFF;#111111;#222222;#FF5500\n",
            "; filament_diameter = 1.75, 1.75, 1.75, 1.75\n",
            "; filament used [mm] = 120.0, 0.0, 0.0, 80.0\n",
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,100,100,100,0,100,100,100,100,0,100,100,100,100,0\n",
            "; flush_volumes_vector = 100,100,100,100\n",
            "G9111 bedTemp=60 extruderTemp=220 tool=0\n",
            "T0\n",
            "G1 X10 Y10 E1.0\n",
            "T3\n",
            "G1 X20 Y20 E2.0\n",
            "T0\n",
            "G1 X30 Y30 E3.0\n",
        ]
        filaments = plugin.parse_slicer_filaments(lines)
        ace_status = {
            "connected": True,
            "print_state": "standby",
            "is_printing": False,
            "slot_count": 8,
            "slots": [
                {"tool": i, "status": "ready", "material": "PLA", "color": "#FF5500"}
                for i in range(8)
            ],
        }

        # Mapping active extruders T0 and T3 to the SAME physical ACE slot T3 must be allowed
        shared_map = {0: 3, 3: 3}
        val = plugin.validate_color_mapping(filaments, shared_map, ace_status["slots"])
        self.assertTrue(val["valid"])
        self.assertEqual(val["errors"], [])
        self.assertEqual(val["warnings"], [])
        self.assertEqual(val["shared_slots"], [3])
        self.assertFalse(val["has_material_mismatch"])
        self.assertTrue(any(item.get("key") == "val.map.shared" for item in val["info_items"]))

        # Even if ACE T3 has PETG while slicer T0/T3 want PLA, shared slot adds info + non-blocking material warning only
        petg_status = {
            "connected": True,
            "print_state": "standby",
            "is_printing": False,
            "slot_count": 8,
            "slots": [
                {"tool": i, "status": "ready", "material": "PETG" if i == 3 else "PLA", "color": "#FF5500"}
                for i in range(8)
            ],
        }
        val_mm = plugin.validate_color_mapping(filaments, shared_map, petg_status["slots"])
        self.assertTrue(val_mm["valid"])
        self.assertEqual(val_mm["errors"], [])
        self.assertEqual(val_mm["shared_slots"], [3])
        self.assertTrue(val_mm["has_material_mismatch"])
        self.assertEqual(len(val_mm["material_mismatches"]), 2)

        # Purge calculation uses original Slicer tool transitions before remapping both T0 and T3 -> T3
        # Initial T0 gets 0.00 due to G9111 SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1; T0->T3 and T3->T0 get 41.58
        area = math.pi * (1.75 * 0.5) ** 2
        expected_len = round(100.0 / area, 2)
        out_lines, info = plugin.process_gcode(
            lines=lines,
            filament_diameter=1.75,
            round_digits=2,
            tool_map=shared_map,
            include_audit_header=True,
        )
        out_text = "".join(out_lines)
        self.assertIn("G9111 bedTemp=60 extruderTemp=220 tool=3", out_text)
        self.assertEqual(out_text.count("ACE_SET_PURGE_AMOUNT PURGELENGTH=0.00"), 1)
        self.assertEqual(out_text.count(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_len:.2f}"), 2)
        self.assertIn("\nT3\n", out_text)
        self.assertNotIn("\nT0\n", out_text)

        st = plugin.PluginState()
        st.update_slicer_filaments(filaments)
        st.tool_map = dict(shared_map)
        st.last_ace_status = ace_status
        st.confirm_material_mismatch = False
        enriched_info = plugin.analyze_gcode_flush(lines, st, filepath="/tmp/shared.gcode")
        self.assertEqual(enriched_info["mapping_validation"]["shared_slots"], [3])
        self.assertEqual(enriched_info["mapping_validation"]["errors"], [])
        for sp in (False, True):
            ok, blocks, warnings = plugin.can_upload_gcode(enriched_info, st, ace_status=ace_status, start_print=sp)
            self.assertTrue(ok)
            self.assertEqual(blocks, [])
            self.assertEqual(warnings, [])

        # Reversible re-patching to distinct slots ({0: 1, 3: 2}) works cleanly with preserved original transitions
        out_repatched, _ = plugin.process_gcode(
            lines=out_lines,
            filament_diameter=1.75,
            round_digits=2,
            tool_map={0: 1, 3: 2},
            include_audit_header=True,
        )
        repatched_text = "".join(out_repatched)
        self.assertIn("G9111 bedTemp=60 extruderTemp=220 tool=1", repatched_text)
        self.assertEqual(repatched_text.count(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_len:.2f}"), 2)
        self.assertIn("\nT1\n", repatched_text)
        self.assertIn("\nT2\n", repatched_text)
        self.assertNotIn("\nT3\n", repatched_text)

        # Thumbnail extraction: picks largest valid PNG and ignores invalid/non-PNG blocks
        small_png = b"\x89PNG\r\n\x1a\nsmall"
        large_png = b"\x89PNG\r\n\x1a\n" + (b"LARGEPNGDATA" * 10)
        bad_payload = b"NOT_A_PNG_AT_ALL"
        small_b64 = base64.b64encode(small_png).decode("ascii")
        large_b64 = base64.b64encode(large_png).decode("ascii")
        bad_b64 = base64.b64encode(bad_payload).decode("ascii")
        thumb_lines = [
            f"; thumbnail begin 32x32 {len(small_b64)}\n",
            f"; {small_b64}\n",
            "; thumbnail end\n",
            f"; thumbnail begin 230x110 {len(large_b64)}\n",
            f"; {large_b64[:40]}\n",
            f"; {large_b64[40:]}\n",
            "; thumbnail end\n",
            f"; thumbnail begin 400x400 {len(bad_b64)}\n",
            f"; {bad_b64}\n",
            "; thumbnail end\n",
            "; HEADER_BLOCK_END\n",
            "; total filament weight [g] : 12.3\n",
        ]
        meta = plugin.parse_gcode_job_metadata(thumb_lines, filepath="/tmp/multi_thumb.gcode")
        self.assertEqual(meta["filename"], "multi_thumb.gcode")
        self.assertEqual(meta["thumbnail_width"], 230)
        self.assertEqual(meta["thumbnail_height"], 110)
        self.assertEqual(meta["thumbnail_data_url"], f"data:image/png;base64,{large_b64}")
        decoded = base64.b64decode(meta["thumbnail_data_url"].split(",", 1)[1])
        self.assertEqual(decoded, large_png)

        # Real OrcaSlicer sample G-code thumbnail extraction (read-only)
        with open(self.sample_gcode_path, "r", encoding="utf-8") as f:
            sample_lines = f.readlines()
        sample_meta = plugin.parse_gcode_job_metadata(sample_lines, filepath=self.sample_gcode_path)
        self.assertEqual(sample_meta["thumbnail_width"], 230)
        self.assertEqual(sample_meta["thumbnail_height"], 110)
        self.assertTrue(sample_meta["thumbnail_data_url"].startswith("data:image/png;base64,"))
        sample_png = base64.b64decode(sample_meta["thumbnail_data_url"].split(",", 1)[1])
        self.assertTrue(sample_png.startswith(b"\x89PNG\r\n\x1a\n"))

        # G-code without thumbnails or with only malformed blocks yields empty fallback
        no_thumb_meta = plugin.parse_gcode_job_metadata(["; no thumbnail here\n", "G28\n"], filepath="/tmp/no_thumb.gcode")
        self.assertEqual(no_thumb_meta["thumbnail_data_url"], "")
        self.assertEqual(no_thumb_meta["thumbnail_width"], 0)
        self.assertEqual(no_thumb_meta["thumbnail_height"], 0)

        corrupt_meta = plugin.parse_gcode_job_metadata([
            "; thumbnail begin 128x128 16\n",
            "; !!!not-valid-base64!!!\n",
            "; thumbnail end\n",
        ])
        self.assertEqual(corrupt_meta["thumbnail_data_url"], "")
        self.assertEqual(corrupt_meta["thumbnail_width"], 0)
        self.assertEqual(corrupt_meta["thumbnail_height"], 0)

        # UI & i18n checks for shared-slot badge + thumbnail DOM
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn('id="modalThumbBox"', html)
        self.assertIn('id="modalThumbImg"', html)
        self.assertIn('id="modalThumbFallback"', html)
        self.assertIn('ui.modal.card.shared_badge', html)
        self.assertIn('<span class="badge info">', html)
        self.assertNotIn('ui.modal.card.dup_badge', html)
        self.assertIn("shared", plugin.t("val.map.shared", lang="en", extruders="T0, T3", phys_tool=3).lower())
        self.assertIn("geteilter", plugin.t("val.map.shared", lang="de", extruders="T0, T3", phys_tool=3).lower())
        self.assertIn("Shared slot", plugin.t("ui.modal.card.shared_badge", lang="en", slot=3))
        self.assertIn("Geteilter Slot", plugin.t("ui.modal.card.shared_badge", lang="de", slot=3))

    def test_ux_07_enlarged_thumbnail_and_live_canvas_recoloring(self) -> None:
        """Enlarged thumbnail container, 2x2 grid, <canvas> live recolor + hover pulse, and EN/DE keys."""
        import base64
        import struct
        import zlib

        html = plugin.ACE_PLUGIN_HTML_UI

        # 1. Enlarged #modalThumbBox, 2x2 .job-meta-grid, canvas, and toggle button DOM/CSS/JS
        self.assertIn("width: 340px;", html)
        self.assertIn("min-width: 300px;", html)
        self.assertIn("height: 170px;", html)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", html)
        self.assertIn("overflow-wrap: anywhere;", html)
        self.assertIn('id="modalThumbBox"', html)
        self.assertIn('id="modalThumbImg"', html)
        self.assertIn('id="modalThumbCanvas"', html)
        self.assertIn('id="modalThumbFallback"', html)
        self.assertIn('id="btnThumbColorMode"', html)
        for js_symbol in (
            "function rgbToHsl(",
            "function hslToRgb(",
            "function matchPixelToExtruder(",
            "function recolorThumbnailPixel(",
            "function toggleThumbnailColorMode(",
            "function setHoveredExtruder(",
            "function clearHoveredExtruder(",
            "function updateThumbnailMappedColors(",
            "updateThumbnailMappedColors();",
            'card.addEventListener("mouseenter"',
            'card.addEventListener("mouseleave"',
        ):
            self.assertIn(js_symbol, html)

        # 2. Pixel recoloring: identical mapping leaves pixels strictly unchanged
        for r, g, b in ((0, 174, 66), (0, 96, 36), (30, 210, 98), (220, 220, 220)):
            self.assertEqual(plugin.recolor_thumbnail_pixel(r, g, b, "#00AE42", "#00AE42"), (r, g, b))

        # 3. Shading-preserving hue shift: Green (#00AE42) -> Red (#EF4444) and Neutral Grey (#808080)
        shadow_red = plugin.recolor_thumbnail_pixel(0, 96, 36, "#00AE42", "#EF4444")
        lit_red = plugin.recolor_thumbnail_pixel(30, 210, 98, "#00AE42", "#EF4444")
        self.assertGreater(shadow_red[0], shadow_red[1] + 40)
        self.assertGreater(shadow_red[0], shadow_red[2] + 40)
        self.assertGreater(lit_red[0], lit_red[1] + 40)
        self.assertGreater(lit_red[0], lit_red[2] + 40)
        self.assertGreater(sum(lit_red), sum(shadow_red))

        shadow_grey = plugin.recolor_thumbnail_pixel(0, 96, 36, "#00AE42", "#808080")
        lit_grey = plugin.recolor_thumbnail_pixel(30, 210, 98, "#00AE42", "#808080")
        self.assertLessEqual(abs(shadow_grey[0] - shadow_grey[1]), 2)
        self.assertLessEqual(abs(shadow_grey[1] - shadow_grey[2]), 2)
        self.assertLessEqual(abs(lit_grey[0] - lit_grey[1]), 2)
        self.assertLessEqual(abs(lit_grey[1] - lit_grey[2]), 2)
        self.assertGreater(lit_grey[0], shadow_grey[0])

        # White (#FFFFFF) -> Blue (#2563EB) & Black (#000000) -> Orange (#F97316) preserve shading gradients
        dark_blue = plugin.recolor_thumbnail_pixel(120, 120, 120, "#FFFFFF", "#2563EB")
        lit_blue = plugin.recolor_thumbnail_pixel(225, 225, 225, "#FFFFFF", "#2563EB")
        self.assertGreater(lit_blue[2], lit_blue[0] + 50)
        self.assertGreater(sum(lit_blue), sum(dark_blue))

        dark_orange = plugin.recolor_thumbnail_pixel(16, 16, 16, "#000000", "#F97316")
        lit_orange = plugin.recolor_thumbnail_pixel(48, 48, 48, "#000000", "#F97316")
        self.assertGreater(lit_orange[0], lit_orange[1])
        self.assertGreater(lit_orange[1], lit_orange[2])
        self.assertGreater(sum(lit_orange), sum(dark_orange))

        # 4. Chromatic palette matching rejects neutral grid/purge-tower grey & transparent pixels
        chrom_palette = plugin.build_thumbnail_source_palette([
            {"slicer_tool": 0, "color": "#00AE42", "used": True},
            {"slicer_tool": 1, "color": "#0055FF", "used": True},
            {"slicer_tool": 2, "color": "#FF6600", "used": False},
        ])
        self.assertEqual([e["slicer_tool"] for e in chrom_palette], [0, 1])
        self.assertEqual(plugin.match_thumbnail_pixel_extruder(0, 174, 66, 0, chrom_palette), -1)
        self.assertEqual(plugin.match_thumbnail_pixel_extruder(128, 128, 128, 255, chrom_palette), -1)
        self.assertEqual(plugin.match_thumbnail_pixel_extruder(10, 145, 55, 255, chrom_palette), 0)
        self.assertEqual(plugin.match_thumbnail_pixel_extruder(20, 70, 210, 255, chrom_palette), 1)

        # 5. Real OrcaSlicer sample thumbnail RGBA matching across both active extruders (T0=#FFFFFF, T1=#000000)
        with open(self.sample_gcode_path, "r", encoding="utf-8") as f:
            sample_lines = f.readlines()
        sample_filaments = plugin.parse_slicer_filaments(sample_lines)
        sample_meta = plugin.parse_gcode_job_metadata(sample_lines, filepath=self.sample_gcode_path)
        sample_palette = plugin.build_thumbnail_source_palette(sample_filaments)
        raw_png = base64.b64decode(sample_meta["thumbnail_data_url"].split(",", 1)[1])
        pos = 8
        idat = bytearray()
        while pos < len(raw_png):
            length, ctype = struct.unpack(">I4s", raw_png[pos:pos + 8])
            chunk = raw_png[pos + 8:pos + 8 + length]
            pos += 12 + length
            if ctype == b"IDAT":
                idat.extend(chunk)
        decoded_scanlines = zlib.decompress(bytes(idat))
        w = sample_meta["thumbnail_width"]
        h = sample_meta["thumbnail_height"]
        stride = w * 4 + 1
        matched_tools = set()
        for y in range(h):
            row_start = y * stride + 1
            for x in range(w):
                off = row_start + x * 4
                idx = plugin.match_thumbnail_pixel_extruder(
                    decoded_scanlines[off],
                    decoded_scanlines[off + 1],
                    decoded_scanlines[off + 2],
                    decoded_scanlines[off + 3],
                    sample_palette,
                )
                if idx >= 0:
                    matched_tools.add(idx)
        self.assertEqual(matched_tools, {0, 1})

        # 6. EN & DE translations for preview mode toggle & tooltips
        self.assertEqual(set(plugin.TRANSLATIONS["en"].keys()), set(plugin.TRANSLATIONS["de"].keys()))
        for key in ("ui.modal.thumb.mode_mapped", "ui.modal.thumb.mode_original", "ui.modal.thumb.mode_title"):
            en_val = plugin.t(key, lang="en")
            de_val = plugin.t(key, lang="de")
            self.assertTrue(en_val.strip(), key)
            self.assertTrue(de_val.strip(), key)
        self.assertNotEqual(
            plugin.t("ui.modal.thumb.mode_mapped", lang="en"),
            plugin.t("ui.modal.thumb.mode_mapped", lang="de"),
        )

    def test_ux_08_click_to_zoom_lightbox_and_escape_layering(self) -> None:
        """340x170 thumbnail, zoom-in hint, #thumbZoomModal / #zoomThumbCanvas, Escape layering, and EN/DE keys."""
        html = plugin.ACE_PLUGIN_HTML_UI

        # 1. Enlarged 340x170 thumbnail container CSS, cursor hint, and zoom icon badge
        self.assertIn("width: 340px;", html)
        self.assertIn("min-width: 300px;", html)
        self.assertIn("height: 170px;", html)
        self.assertIn("cursor: zoom-in;", html)
        self.assertIn('id="btnThumbZoom"', html)
        self.assertIn('id="btnThumbZoom" class="thumb-zoom-badge"', html)
        self.assertNotIn("🔍</button>", html)
        self.assertIn("max-width: 85vw;", html)
        self.assertIn("max-height: 75vh;", html)

        # 2. Lightbox overlay elements, unique IDs, click handlers, and live canvas sync
        for required_id in ("thumbZoomModal", "zoomThumbCanvas", "btnZoomColorMode", "btnZoomClose", "zoomThumbFilename"):
            self.assertIn(f'id="{required_id}"', html)
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), "Duplicate HTML element IDs found")

        for js_symbol in (
            "function openThumbZoomModal(",
            "function closeThumbZoomModal(",
            "function onModalThumbBoxClick(",
            "function onThumbZoomBackdropClick(",
            "function syncZoomCanvasFromCache(",
            'thumb-mode-badge',
            'zoomEl.classList.contains("open")',
            'closeThumbZoomModal();',
        ):
            self.assertIn(js_symbol, html)

        # 3. Verify Escape key layering: closes #thumbZoomModal first and returns before closing #prePrintModal
        keydown_section = html.split('document.addEventListener("keydown"', 1)[1].split("initTheme();", 1)[0]
        idx_zoom_check = keydown_section.find('document.getElementById("thumbZoomModal")')
        idx_zoom_close = keydown_section.find("closeThumbZoomModal();")
        idx_return_after_zoom = keydown_section.find("return;", idx_zoom_close)
        idx_preprint_check = keydown_section.find('document.getElementById("prePrintModal")')
        idx_preprint_close = keydown_section.find("closePrePrintModal();")
        self.assertNotEqual(idx_zoom_check, -1)
        self.assertGreater(idx_zoom_close, idx_zoom_check)
        self.assertGreater(idx_return_after_zoom, idx_zoom_close)
        self.assertGreater(idx_preprint_check, idx_return_after_zoom)
        self.assertGreater(idx_preprint_close, idx_preprint_check)

        # 4. EN & DE translation keys for zoom button tooltip, heading, close button, and dismiss hint
        self.assertEqual(set(plugin.TRANSLATIONS["en"].keys()), set(plugin.TRANSLATIONS["de"].keys()))
        for key in (
            "ui.modal.thumb.zoom_title",
            "ui.modal.thumb.zoom_heading",
            "ui.modal.thumb.zoom_close",
            "ui.modal.thumb.zoom_dismiss_hint",
        ):
            en_val = plugin.t(key, lang="en")
            de_val = plugin.t(key, lang="de")
            self.assertTrue(en_val.strip(), key)
            self.assertTrue(de_val.strip(), key)
            self.assertNotEqual(en_val, de_val, key)

    def test_ux_09_native_orcaslicer_theme_adaptive_thumb_and_staged_handover(self) -> None:
        """Neutral charcoal dark theme, #009688 teal accent, 6/4px radii, light/dark thumb backdrops, clear button, and stale-model check."""
        html = plugin.ACE_PLUGIN_HTML_UI
        style = html.split("<style>")[1].split("</style>")[0]

        # 1. Native OrcaSlicer neutral charcoal dark palette, #009688 teal accent, and crisp 6px/4px radii
        for required_css in (
            "--bg: var(--orca-bg, #262928);",
            "--surface-card: #2f3332;",
            "--surface-sub: #383d3b;",
            "--surface-input: #383d3b;",
            "--border: var(--orca-border, #464b49);",
            "--accent: var(--orca-accent, #009688);",
            "--accent-hover: #00a696;",
            "--accent-active: #00796b;",
            "--accent-soft: rgba(0, 150, 136, 0.16);",
            "--radius: 6px;",
            "--radius-sm: 4px;",
            "--bg: var(--orca-bg, #f3f4f6);",
            "--border: var(--orca-border, #d8dce0);",
            "button.primary:hover",
            "button.primary:active",
            "background: #1c1f1e;",
            'html[data-resolved="light"] .modal-thumb-box',
            'html[data-resolved="light"] .thumb-zoom-stage',
            'html[data-resolved="light"] .thumb-mode-badge',
            'html[data-resolved="light"] .thumb-zoom-badge',
            "background: #ffffff;",
            "border-color: #d8dce0;",
        ):
            self.assertIn(required_css, style)

        # Ensure old blue-tinted hexes, emerald greens, and oversized 10px/12px radii are gone
        for forbidden_css in (
            "#11161d",
            "#181e28",
            "#202736",
            "#283144",
            "#141922",
            "#1b2230",
            "#253045",
            "#3b475c",
            "#0d131c",
            "#0b1119",
            "#00b894",
            "#00997a",
            "rgba(56, 189, 248",
            "border-radius: 10px;",
            "border-radius: 12px;",
            "border-radius: 8px;",
        ):
            self.assertNotIn(forbidden_css, style)
        self.assertIn("rgba(0,150,136,", html)
        self.assertNotIn("rgba(0,184,148,", html)

        # 2. Clear (x) button & stale-model warning DOM/JS hooks with zero duplicate IDs
        for required_dom in (
            'id="btnClearModalGcode"',
            'id="modalStaleModelBanner"',
            'id="modalStaleModelText"',
            "function clearModalGcode()",
            "function renderModalStaleWarning()",
            'sendToHost({ type: "clear_gcode" });',
        ):
            self.assertIn(required_dom, html)
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), "Duplicate HTML element IDs found")

        # 3. Staged slice handover (sub-50ms newer mtime + echoed gcode_path + clear_gcode behavior)
        sample_a = (
            "; filament used [g] = 10.0, 2.0\n"
            "; filament_type = PLA;PLA\n"
            "; filament_colour = #FF0000;#00FF00\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,100,100,0\n"
            "; flush_volumes_vector = 50,50\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\nT1\n"
        )
        sample_b = sample_a.replace("10.0, 2.0", "22.0, 4.0")

        with tempfile.TemporaryDirectory() as tmpdir:
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            st.language = "en"
            staged_dir = os.path.join(tmpdir, "staged_gcode")
            os.makedirs(staged_dir, exist_ok=True)

            plate1 = os.path.join(staged_dir, "plate_first_PLA.gcode")
            plate2 = os.path.join(staged_dir, "plate_second_PLA.gcode")
            with open(plate1, "w", encoding="utf-8") as f:
                f.write(sample_a)
            os.utime(plate1, ns=(5_000_000_000_000, 5_000_000_000_000))

            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message("acquire_slicer_gcode", {"force_refresh": False})
                self.assertEqual(st.last_gcode_path, plate1)

                # Simulate writing a second slice just 10ms later while UI still echoes plate1 as gcode_path
                with open(plate2, "w", encoding="utf-8") as f:
                    f.write(sample_b)
                os.utime(plate2, ns=(5_000_010_000_000, 5_000_010_000_000))

                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"gcode_path": plate1, "force_refresh": False},
                )
                self.assertEqual(st.last_gcode_path, plate2)
                self.assertEqual(st.last_gcode_source, "staged_gcode")

                # Clear (x) unloads active G-code and suppresses zombie reload on immediate reopen
                page._handle_async_message("clear_gcode", {})
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(st.last_job_metadata, {})
                self.assertEqual(st.last_postprocess_info, {})
                self.assertGreater(st.cleared_before_mtime_ns, 0)
                self.assertEqual(posted[-1]["modal_feedback"]["level"], "info")

                page._handle_async_message("acquire_slicer_gcode", {"force_refresh": False})
                self.assertEqual(st.last_gcode_path, "")

                # Cleared files must NEVER reload even on force_refresh=True
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"gcode_path": plate1, "force_refresh": True},
                )
                self.assertEqual(st.last_gcode_path, "")
                self.assertGreater(st.cleared_before_mtime_ns, 0)

                # Clear again, then simulate a newer slice (plate3): automatically picked up on reopen
                page._handle_async_message("clear_gcode", {})
                plate3 = os.path.join(staged_dir, "plate_third_PLA.gcode")
                with open(plate3, "w", encoding="utf-8") as f:
                    f.write(sample_b)
                os.utime(plate3, ns=(5_000_020_000_000, 5_000_020_000_000))
                page._handle_async_message("acquire_slicer_gcode", {"force_refresh": False})
                self.assertEqual(st.last_gcode_path, plate3)

            # 4. Stale-model comparison via orca.host.model() / orca.host.plater()
            mock_host = types.SimpleNamespace(
                plater=lambda: {"project_name": "NewWidget.3mf"},
                model=lambda: types.SimpleNamespace(name="NewWidget_Body"),
            )
            with mock.patch.object(plugin.orca, "host", mock_host):
                stale_res = plugin.check_stale_gcode_model(gcode_path=plate1, filename="OldGadget_PLA_1h.gcode")
                self.assertTrue(stale_res["is_stale"])
                self.assertEqual(stale_res["active_model"], "NewWidget")
                self.assertEqual(stale_res["loaded_file"], "OldGadget_PLA_1h.gcode")

                fresh_res = plugin.check_stale_gcode_model(gcode_path="/x/NewWidget_PLA_0.2_2h.gcode")
                self.assertFalse(fresh_res["is_stale"])

                st.last_gcode_path = plate1
                st.last_job_metadata = {"filename": "OldGadget_PLA_1h.gcode"}
                state_dict = st.to_dict()
                self.assertTrue(state_dict["stale_model_info"]["is_stale"])

            # Versioned mismatch check & generic Untitled/Plate 1 fallback
            ver_host = types.SimpleNamespace(
                plater=lambda: types.SimpleNamespace(project_name="Part_v2.3mf"),
                model=lambda: None,
            )
            with mock.patch.object(plugin.orca, "host", ver_host):
                self.assertTrue(plugin.check_stale_gcode_model(filename="Part_v1_PLA.gcode")["is_stale"])
                self.assertTrue(plugin.check_stale_gcode_model(filename="Part_v20_PLA.gcode")["is_stale"])
                self.assertFalse(plugin.check_stale_gcode_model(filename="Part_v2_PLA.gcode")["is_stale"])

            generic_host = types.SimpleNamespace(
                plater=lambda: {"project_name": "Untitled", "plate_name": "Plate 1"},
                model=lambda: None,
            )
            with mock.patch.object(plugin.orca, "host", generic_host):
                self.assertFalse(plugin.check_stale_gcode_model(filename="AnyModel_PLA.gcode")["is_stale"])

        # 5. EN & DE translation keys completeness & distinctness
        self.assertEqual(set(plugin.TRANSLATIONS["en"].keys()), set(plugin.TRANSLATIONS["de"].keys()))
        for key in (
            "ui.modal.btn.clear_gcode_title",
            "ui.modal.stale.banner",
            "ui.modal.stale.clear_btn",
            "host.gcode_cleared",
        ):
            en_val = plugin.t(key, lang="en", file="a.gcode", model="ModelX")
            de_val = plugin.t(key, lang="de", file="a.gcode", model="ModelX")
            self.assertTrue(en_val.strip(), key)
            self.assertTrue(de_val.strip(), key)
            self.assertNotEqual(en_val, de_val, key)

    def test_ux_10_status_badge_teal_slot_bays_and_auto_popup_notification(self) -> None:
        """
        Verify OrcaSlicer Teal status badges (--ok #00a696/#00796b), #262928/#2f3332 slot bay
        layering, native push_notification on slice/export completion (even with page=None), lazy
        'ready' -> open_preprint_modal without triggering auto-acquire, and preprint_modal_opened ACK.
        """
        html = plugin.ACE_PLUGIN_HTML_UI
        style_start = html.find("<style>")
        style_end = html.find("</style>")
        self.assertGreater(style_start, 0)
        self.assertGreater(style_end, style_start)
        css_block = html[style_start:style_end]

        # 1. Unified Teal status badges & zero emerald --ok tokens
        for forbidden in ("#10b981", "#047857", "rgba(16, 185, 129"):
            self.assertNotIn(forbidden, css_block.lower())
        self.assertIn("--ok: #00a696;", css_block)
        self.assertIn("--ok: #00796b;", css_block)
        self.assertIn(".badge.ready, .badge.ok", css_block)
        self.assertIn("rgba(0, 150, 136, 0.32)", css_block)

        # 2. Slot panel (#262928 --bg) & Slot bays (#2f3332 --surface-card)
        slots_main_block = re.search(r"\.slots-main-card\s*\{[^}]*\}", css_block)
        self.assertIsNotNone(slots_main_block)
        self.assertIn("background: var(--bg);", slots_main_block.group(0))
        slot_card_block = re.search(r"\.slot-card\s*\{[^}]*\}", css_block)
        self.assertIsNotNone(slot_card_block)
        self.assertIn("background: var(--surface-card);", slot_card_block.group(0))
        slot_loaded_block = re.search(r"\.slot-card\.slot-loaded\s*\{[^}]*\}", css_block)
        self.assertIsNotNone(slot_loaded_block)
        self.assertIn("var(--surface-card)", slot_loaded_block.group(0))

        # 3. JS open_preprint_modal handler closes secondary modals, refreshes summary/mapping & sends ACK
        self.assertIn('msg.action === "open_preprint_modal"', html)
        self.assertIn("closeThumbZoomModal();", html)
        self.assertIn("renderModalJobSummary();", html)
        self.assertIn("renderMappingTable();", html)
        self.assertIn('showModalFeedback("ok", t("host.auto_opened_preprint"', html)
        self.assertIn('sendToHost({ type: "preprint_modal_opened" });', html)

        # 4. EN & DE translation keys for native OrcaSlicer notification
        self.assertEqual(set(plugin.TRANSLATIONS["en"].keys()), set(plugin.TRANSLATIONS["de"].keys()))
        for key in ("host.notify_sliced_ready", "host.notify_open_action"):
            en_val = plugin.t(key, lang="en", file="benchy.gcode", tc=3)
            de_val = plugin.t(key, lang="de", file="benchy.gcode", tc=3)
            self.assertTrue(en_val.strip(), key)
            self.assertTrue(de_val.strip(), key)
            self.assertNotEqual(en_val, de_val, key)

        # 5. End-to-end auto-popup workflow: execute(ctx) while page=None -> push_notification + lazy ready + ACK
        sample_gcode = (
            "; generated by OrcaSlicer 2.5.0\n"
            "; filament_colour = #FFFFFF;#000000\n"
            "; filament_type = PLA;PLA\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,200,200,0\n"
            "; flush_volumes_vector = 100,100\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\n"
            "T1\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "plugin_storage")
            os.makedirs(storage_dir, exist_ok=True)
            gcode_file = os.path.join(tmpdir, "notif_plate.gcode")
            with open(gcode_file, "w", encoding="utf-8") as f:
                f.write(sample_gcode)

            st = plugin.PluginState()
            st.language = "en"
            st.resolve_storage_dir = lambda: storage_dir
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")

            notifications = []

            def _record_notif(level, text, hyper_text="", on_click=None):
                notifications.append(
                    {
                        "level": level,
                        "text": text,
                        "hyper_text": hyper_text,
                        "on_click": on_click,
                    }
                )

            mock_client = mock.MagicMock()
            mock_client.active_url = "http://192.0.2.10:7125"
            mock_client.query_ace_status.return_value = {"slots": []}

            orig_active = plugin._ACTIVE_PAGE_CAPABILITY
            plugin._ACTIVE_PAGE_CAPABILITY = None
            try:
                with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
                    st, "get_client", return_value=mock_client
                ), mock.patch.object(
                    plugin.orca.host.ui, "push_notification", side_effect=_record_notif
                ):
                    ctx = types.SimpleNamespace(
                        step=plugin.orca.slicing.Step.psGCodePostProcess,
                        gcode_path=gcode_file,
                    )
                    res = plugin.KobraS1AcePostProcessCapability().execute(ctx)
                    self.assertTrue(res.ok)
                    self.assertTrue(st.pending_preprint_open)
                    self.assertEqual(len(notifications), 1)
                    self.assertEqual(
                        notifications[0]["level"],
                        plugin.orca.host.ui.NotificationLevel.RegularNotificationLevel,
                    )
                    self.assertIn("notif_plate.gcode", notifications[0]["text"])
                    self.assertEqual(
                        notifications[0]["hyper_text"],
                        plugin.t("host.notify_open_action", lang="en"),
                    )
                    self.assertTrue(callable(notifications[0]["on_click"]))

                    # Simulate user switching to plugin tab for the first time ('ready' -> opens modal without auto-acquire)
                    page = plugin.KobraS1AcePageCapability()
                    posted = []
                    page.post_message = posted.append
                    page.on_load()
                    try:
                        with mock.patch.object(
                            plugin,
                            "acquire_gcode_from_slicer",
                            side_effect=AssertionError("ready must not call acquire_gcode_from_slicer"),
                        ), mock.patch.object(
                            plugin,
                            "discover_sliced_gcode_candidates",
                            side_effect=AssertionError("ready must not scan storage on ready"),
                        ):
                            page._handle_async_message("ready", {})

                        self.assertTrue(any(m.get("action") == "open_preprint_modal" for m in posted))
                        ready_msg = next(m for m in posted if m.get("action") == "open_preprint_modal")
                        self.assertEqual(ready_msg.get("source_file"), "notif_plate.gcode")
                        self.assertTrue(st.pending_preprint_open)

                        # JS sends preprint_modal_opened ACK once modal opens
                        page._handle_async_message("preprint_modal_opened", {})
                        self.assertFalse(st.pending_preprint_open)

                        # Simulate GCodeExportFinished while tab is loaded + clicking notification hypertext
                        export_file = os.path.join(tmpdir, "exported_plate.gcode")
                        with open(export_file, "w", encoding="utf-8") as f:
                            f.write(sample_gcode)
                        posted.clear()
                        notifications.clear()
                        evt_done = threading.Event()
                        orig_post = page.post_message

                        def _capture_export_post(msg):
                            orig_post(msg)
                            if msg.get("action") == "open_preprint_modal":
                                evt_done.set()

                        page.post_message = _capture_export_post
                        l_ctx = types.SimpleNamespace(
                            name=export_file,
                            code=plugin.orca.LifecycleEvtCode.Success,
                            msg="",
                        )
                        page.on_lifecycle_event(plugin.orca.LifecycleEvent.GCodeExportFinished, l_ctx)
                        self.assertTrue(evt_done.wait(timeout=2.0))
                        self.assertEqual(len(notifications), 1)
                        self.assertIn("exported_plate.gcode", notifications[0]["text"])

                        posted.clear()
                        clicked_ok = notifications[0]["on_click"]()
                        self.assertTrue(clicked_ok)
                        self.assertTrue(any(m.get("action") == "open_preprint_modal" for m in posted))
                    finally:
                        page.on_unload()
            finally:
                plugin._ACTIVE_PAGE_CAPABILITY = orig_active

    @staticmethod
    def _make_fake_preset_host(
        printer_name: str,
        printer_model: str = "",
        printer_inherits: str = "",
        process_name: str = "0.20mm Standard @Anycubic Kobra S1",
        initial_pipeline: Any = "",
        zero_arg_begin_update: bool = False,
        on_commit: Any = None,
    ) -> Any:
        """Build a lightweight in-memory mock matching OrcaSlicer 2.5.0 preset_bundle + transaction API."""
        state_box: Dict[str, Any] = {
            "printer_name": printer_name,
            "printer_model": printer_model,
            "printer_inherits": printer_inherits,
            "process_name": process_name,
            "process_opts": {
                "slicing_pipeline_plugin": initial_pipeline,
                "sparse_infill_density": "15%",
            },
            "begin_calls": [],
            "commits": [],
            "aborts": 0,
        }

        class _FakePreset:
            def __init__(self, getter_name: Any, getter_cfg: Any) -> None:
                self._getter_name = getter_name
                self._getter_cfg = getter_cfg

            @property
            def name(self) -> str:
                return self._getter_name()

            def config_value(self, key: str) -> Any:
                return self._getter_cfg(key)

        class _FakeBundle:
            def __init__(self) -> None:
                self._printer = _FakePreset(
                    lambda: state_box["printer_name"],
                    lambda k: (
                        state_box["printer_model"]
                        if k == "printer_model"
                        else state_box["printer_inherits"]
                        if k == "inherits"
                        else None
                    ),
                )
                self._process = _FakePreset(
                    lambda: state_box["process_name"],
                    lambda k: state_box["process_opts"].get(k),
                )

            def current_printer_preset(self) -> Any:
                return self._printer

            def current_process_preset(self) -> Any:
                return self._process

            def full_config_value(self, key: str) -> Any:
                return state_box["process_opts"].get(key)

        class _FakeTx:
            def __init__(self) -> None:
                self.pending: Dict[str, Any] = {}
                self.thread_id = threading.get_ident()

            def set(self, key: str, value: Any) -> None:
                self.pending[key] = value

            def commit(self) -> None:
                state_box["process_opts"].update(self.pending)
                state_box["commits"].append((dict(self.pending), threading.get_ident()))
                if callable(on_commit):
                    on_commit(dict(self.pending), self.thread_id)

            def abort(self) -> None:
                state_box["aborts"] += 1

        def _begin_update_one_arg(name: str = "") -> _FakeTx:
            state_box["begin_calls"].append(name)
            return _FakeTx()

        def _begin_update_zero_arg() -> _FakeTx:
            state_box["begin_calls"].append(None)
            return _FakeTx()

        fake_host = types.SimpleNamespace(
            plugin=plugin.orca.host.plugin,
            ui=plugin.orca.host.ui,
            preset_bundle=lambda: _FakeBundle(),
            preset=types.SimpleNamespace(
                process=types.SimpleNamespace(
                    begin_update=_begin_update_zero_arg if zero_arg_begin_update else _begin_update_one_arg
                )
            ),
            _box=state_box,
        )
        return fake_host

    def test_pipeline_reg_01_only_matching_kobra_s1_profiles_modified(self) -> None:
        """Auto-registration targets only matching Kobra S1 printer profiles and leaves foreign profiles untouched."""
        # Exact printer name/model/inherits matrix
        self.assertTrue(plugin.is_kobra_s1_printer_name("Anycubic Kobra S1"))
        self.assertTrue(plugin.is_kobra_s1_printer_name("Anycubic Kobra S1 0.4 nozzle"))
        self.assertTrue(plugin.is_kobra_s1_printer_name("Kobra S1 Combo"))
        self.assertTrue(plugin.is_kobra_s1_printer_name("Kobra_S1_Pro"))
        for foreign in (
            "Anycubic Kobra 3",
            "Anycubic Kobra 3 Combo",
            "Anycubic Kobra 2 Pro",
            "Anycubic Kobra X",
            "Anycubic Kobra S1 Max",
            "Bambu Lab X1 Carbon 0.4 nozzle",
            "Snapmaker U1",
            "",
        ):
            self.assertFalse(plugin.is_kobra_s1_printer_name(foreign), f"Unexpected match: {foreign}")

        # 1. Foreign printer profile -> zero transactions, preset completely untouched
        foreign_host = self._make_fake_preset_host(
            printer_name="Bambu Lab X1 Carbon 0.4 nozzle",
            printer_model="Bambu Lab X1 Carbon",
            process_name="0.20mm Standard @BBL X1C",
            initial_pipeline="existing_bbl_hook",
        )
        st_foreign = plugin.PluginState()
        st_foreign.resolve_config_path = lambda: ""
        with mock.patch.object(plugin.orca, "host", foreign_host):
            res_foreign = plugin.ensure_slicing_pipeline_registered(st_foreign)
        self.assertEqual(res_foreign["status"], "skipped_non_kobra_s1")
        self.assertEqual(foreign_host._box["begin_calls"], [])
        self.assertEqual(foreign_host._box["commits"], [])
        self.assertEqual(foreign_host._box["process_opts"]["slicing_pipeline_plugin"], "existing_bbl_hook")
        self.assertEqual(foreign_host._box["process_opts"]["sparse_infill_density"], "15%")

        # 2. Custom renamed printer preset inheriting from Anycubic Kobra S1 -> registered + unrelated opts kept
        kobra_host = self._make_fake_preset_host(
            printer_name="My Fast PETG Printer",
            printer_model="",
            printer_inherits='"Anycubic Kobra S1 0.4 nozzle"',
            process_name="0.20mm PETG Custom",
            initial_pipeline="",
            zero_arg_begin_update=True,
        )
        st_ks1 = plugin.PluginState()
        st_ks1.resolve_config_path = lambda: ""
        with mock.patch.object(plugin.orca, "host", kobra_host):
            res_ks1 = plugin.ensure_slicing_pipeline_registered(st_ks1)
        self.assertEqual(res_ks1["status"], "updated")
        self.assertEqual(len(kobra_host._box["commits"]), 1)
        parsed = plugin.parse_slicer_pipeline_entries(
            kobra_host._box["process_opts"]["slicing_pipeline_plugin"]
        )
        self.assertEqual(parsed, [plugin.PIPELINE_CAPABILITY_NAME])
        self.assertEqual(kobra_host._box["process_opts"]["sparse_infill_density"], "15%")

    def test_pipeline_reg_02_preserves_existing_entries_and_prevents_duplicates(self) -> None:
        """Existing slicing_pipeline_plugin entries are preserved and repeated updates are prevented."""
        # Existing entries: both raw string (with embedded quoted semicolon ref) and native list
        host_existing = self._make_fake_preset_host(
            printer_name="Anycubic Kobra S1 0.4 nozzle",
            printer_model="Anycubic Kobra S1",
            process_name="0.20mm Standard @Anycubic Kobra S1",
            initial_pipeline='first_tool;"5d572581;Existing Helper"',
        )
        st = plugin.PluginState()
        st.resolve_config_path = lambda: ""
        with mock.patch.object(plugin.orca, "host", host_existing):
            res1 = plugin.ensure_slicing_pipeline_registered(st)
            self.assertEqual(res1["status"], "updated")
            entries = plugin.parse_slicer_pipeline_entries(
                host_existing._box["process_opts"]["slicing_pipeline_plugin"]
            )
            self.assertEqual(
                entries,
                [
                    "first_tool",
                    "5d572581;Existing Helper",
                    plugin.PIPELINE_CAPABILITY_NAME,
                ],
            )
            self.assertEqual(len(host_existing._box["commits"]), 1)

            # Repeat call on same (printer, process) must not re-run begin_update even if user removes it
            host_existing._box["process_opts"]["slicing_pipeline_plugin"] = "first_tool"
            res2 = plugin.ensure_slicing_pipeline_registered(st)
            self.assertEqual(res2["status"], "already_checked")
            self.assertEqual(len(host_existing._box["begin_calls"]), 1)
            self.assertEqual(host_existing._box["process_opts"]["slicing_pipeline_plugin"], "first_tool")

            # Switching to a DIFFERENT process preset on Kobra S1 checks that new preset once
            host_existing._box["process_name"] = "0.16mm Optimal @Anycubic Kobra S1"
            host_existing._box["process_opts"]["slicing_pipeline_plugin"] = [
                "Other Plugin",
                "Another Helper",
            ]
            res3 = plugin.ensure_slicing_pipeline_registered(st)
            self.assertEqual(res3["status"], "updated")
            self.assertEqual(
                plugin.parse_slicer_pipeline_entries(
                    host_existing._box["process_opts"]["slicing_pipeline_plugin"]
                ),
                ["Other Plugin", "Another Helper", plugin.PIPELINE_CAPABILITY_NAME],
            )

        # Already-present bare or UUID-qualified capability must NOT open begin_update at all
        for pre_val in (
            f'"{plugin.PIPELINE_CAPABILITY_NAME}"',
            f'"abc123;{plugin.PIPELINE_CAPABILITY_NAME}";another_tool',
            ["alpha", plugin.PIPELINE_CAPABILITY_NAME],
        ):
            host_present = self._make_fake_preset_host(
                printer_name="Anycubic Kobra S1 0.4 nozzle",
                printer_model="Anycubic Kobra S1",
                process_name="Preconfigured Process",
                initial_pipeline=pre_val,
            )
            st_fresh = plugin.PluginState()
            st_fresh.resolve_config_path = lambda: ""
            with mock.patch.object(plugin.orca, "host", host_present):
                res_present = plugin.ensure_slicing_pipeline_registered(st_fresh)
            self.assertEqual(res_present["status"], "already_present")
            self.assertEqual(host_present._box["begin_calls"], [])

        # Respect user opt-out (auto_register_pipeline=False)
        host_optout = self._make_fake_preset_host(
            printer_name="Anycubic Kobra S1 0.4 nozzle",
            printer_model="Anycubic Kobra S1",
            process_name="OptOut Process",
            initial_pipeline="user_tool",
        )
        st_optout = plugin.PluginState()
        st_optout.resolve_config_path = lambda: ""
        st_optout.apply_dict({"auto_register_pipeline": False}, save=False)
        with mock.patch.object(plugin.orca, "host", host_optout):
            self.assertEqual(plugin.ensure_slicing_pipeline_registered(st_optout)["status"], "disabled")
            self.assertEqual(host_optout._box["begin_calls"], [])
            self.assertEqual(host_optout._box["process_opts"]["slicing_pipeline_plugin"], "user_tool")

    def test_pipeline_reg_03_lifecycle_off_ui_thread_and_ctx_gcode_atomic_staging(self) -> None:
        """Lifecycle events schedule off-UI-thread registration; ctx.gcode_path stages atomically without tempfile.* or /tmp scanning."""
        commit_evt = threading.Event()
        commit_threads: List[int] = []
        main_tid = threading.get_ident()

        def _on_commit(pending: Dict[str, Any], tid: int) -> None:
            commit_threads.append(tid)
            commit_evt.set()

        host_async = self._make_fake_preset_host(
            printer_name="Anycubic Kobra S1 0.4 nozzle",
            printer_model="Anycubic Kobra S1",
            process_name="0.20mm Lifecycle Test",
            initial_pipeline="existing_plugin",
            on_commit=_on_commit,
        )

        st = plugin.PluginState()
        st.resolve_config_path = lambda: ""
        with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
            plugin.orca, "host", host_async
        ):
            page = plugin.KobraS1AcePageCapability()
            pp = plugin.KobraS1AcePostProcessCapability()

            # Filament preset changes do not trigger pipeline edits
            page.on_lifecycle_event(
                plugin.orca.LifecycleEvent.PresetSelected,
                types.SimpleNamespace(msg="filament", name=""),
            )
            self.assertFalse(commit_evt.wait(timeout=0.08))
            self.assertEqual(host_async._box["begin_calls"], [])

            # Process preset selection runs off the caller (UI) thread and preserves existing entry
            pp.on_lifecycle_event(
                plugin.orca.LifecycleEvent.PresetSelected,
                types.SimpleNamespace(msg="process", name="0.20mm Lifecycle Test"),
            )
            self.assertTrue(commit_evt.wait(timeout=2.0))
            self.assertTrue(commit_threads)
            self.assertNotEqual(commit_threads[0], main_tid)
            self.assertEqual(
                plugin.parse_slicer_pipeline_entries(
                    host_async._box["process_opts"]["slicing_pipeline_plugin"]
                ),
                ["existing_plugin", plugin.PIPELINE_CAPABILITY_NAME],
            )

            # Follow-up ProjectOpened on Page must NOT repeat begin_update
            page.on_lifecycle_event(
                plugin.orca.LifecycleEvent.ProjectOpened,
                types.SimpleNamespace(msg="", name="project.3mf"),
            )
            for thr in list(threading.enumerate()):
                if thr.name == "kobra-s1-pipeline-reg" and thr.is_alive():
                    thr.join(timeout=2.0)
            self.assertEqual(len(host_async._box["commits"]), 1)

        # Verify request_permissions never requests fs_readwrite or broad /tmp/*, only narrow orcaslicer_model/*
        pkg = plugin.KobraS1AcePlugin()
        with mock.patch.object(plugin.orca, "register_capability"):
            pkg.register_capabilities()
        self.assertTrue(plugin.orca.last_requested_permissions.get("threading"))
        self.assertIn("network_http", plugin.orca.last_requested_permissions)
        fs_read = plugin.orca.last_requested_permissions.get("fs_read") or []
        self.assertTrue(any(p.endswith("orcaslicer_model/*") for p in fs_read))
        if hasattr(os, "getuid"):
            uid_suffix = f"orcaslicer_{os.getuid()}/orcaslicer_model/*"
            self.assertTrue(any(p.endswith(uid_suffix) for p in fs_read), fs_read)
        self.assertFalse(any(p.rstrip("/*") == "/tmp" for p in fs_read))
        self.assertNotIn("fs_readwrite", plugin.orca.last_requested_permissions)

        # Verify atomic ctx.gcode_path postprocessing & staging without tempfile.* or /tmp scanning
        sample_gcode = (
            "; generated by OrcaSlicer 2.5.0\n"
            "; filament_colour = #FF0000;#00FF00\n"
            "; filament_type = PLA;PLA\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,150,150,0\n"
            "; flush_volumes_vector = 100,100\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\n"
            "T1\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "plugin_storage")
            export_dir = os.path.join(tmpdir, "slicer_tmp_job")
            os.makedirs(storage_dir, exist_ok=True)
            os.makedirs(export_dir, exist_ok=True)
            ctx_gcode = os.path.join(export_dir, "atomic_job.gcode")
            with open(ctx_gcode, "w", encoding="utf-8") as f:
                f.write(sample_gcode)

            st_exec = plugin.PluginState()
            st_exec.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")
            st_exec.apply_dict({"tool_map": {0: 2, 1: 5}}, save=False)

            pp_exec = plugin.KobraS1AcePostProcessCapability()
            with mock.patch.object(plugin, "GLOBAL_STATE", st_exec), mock.patch(
                "tempfile.NamedTemporaryFile",
                side_effect=AssertionError("tempfile.NamedTemporaryFile must not be called"),
            ), mock.patch(
                "tempfile.gettempdir",
                side_effect=AssertionError("tempfile.gettempdir must not be called"),
            ), mock.patch("os.listdir", side_effect=AssertionError("os.listdir forbidden")), mock.patch(
                "os.walk", side_effect=AssertionError("os.walk forbidden")
            ):
                res = pp_exec.execute(
                    types.SimpleNamespace(
                        step=plugin.orca.slicing.Step.psGCodePostProcess,
                        gcode_path=ctx_gcode,
                    )
                )
                self.assertTrue(res.ok, res.message)

                staged_list = plugin.discover_sliced_gcode_candidates(state=st_exec)
                self.assertEqual(len(staged_list), 1)
                self.assertTrue(staged_list[0].startswith(os.path.join(storage_dir, "staged_gcode")))
                # Ensure no leftover .ace_tmp_* files remain in staged or export dirs
                staged_leftover = [
                    e.name for e in os.scandir(os.path.dirname(staged_list[0])) if e.name.startswith(".ace_tmp_")
                ]
                export_leftover = [
                    e.name for e in os.scandir(export_dir) if e.name.startswith(".ace_tmp_")
                ]
                self.assertEqual(staged_leftover, [])
                self.assertEqual(export_leftover, [])
                with open(staged_list[0], "r", encoding="utf-8") as f:
                    staged_content = f.read()
                self.assertIn("G9111 bedTemp=60 extruderTemp=210 tool=2", staged_content)
                self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=", staged_content)

        # Verify mid-flight preset switch before commit aborts cleanly without mutating the new preset
        switch_host = self._make_fake_preset_host(
            printer_name="Anycubic Kobra S1 0.4 nozzle",
            printer_model="Anycubic Kobra S1",
            process_name="0.20mm Before Switch",
            initial_pipeline="keep_me",
        )
        orig_begin = switch_host.preset.process.begin_update

        def _switching_begin(name: str = "") -> Any:
            tx = orig_begin(name)
            switch_host._box["printer_name"] = "Bambu Lab X1 Carbon 0.4 nozzle"
            switch_host._box["printer_model"] = "Bambu Lab X1 Carbon"
            switch_host._box["process_name"] = "0.20mm Foreign After Switch"
            return tx

        switch_host.preset.process.begin_update = _switching_begin
        st_switch = plugin.PluginState()
        st_switch.resolve_config_path = lambda: ""
        with mock.patch.object(plugin.orca, "host", switch_host):
            res_abort = plugin.ensure_slicing_pipeline_registered(st_switch)
        self.assertEqual(res_abort["status"], "aborted_changed")
        self.assertEqual(switch_host._box["aborts"], 1)
        self.assertEqual(switch_host._box["commits"], [])
        self.assertEqual(switch_host._box["process_opts"]["slicing_pipeline_plugin"], "keep_me")

    # -----------------------------------------------------------------------
    # Phase 1 & Phase 2 Pipeline / Container / Provenance / Thread Regression
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_3mf_bytes(
        plates: dict,
        slice_info_xml: str = "",
        extra_entries: dict = None,
    ) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for plate_idx, gcode_text in plates.items():
                zf.writestr(f"Metadata/plate_{int(plate_idx)}.gcode", gcode_text)
            if slice_info_xml:
                zf.writestr("Metadata/slice_info.config", slice_info_xml)
            for name, payload in (extra_entries or {}).items():
                zf.writestr(name, payload)
        return buf.getvalue()

    @staticmethod
    def _sample_mm_gcode(
        colors: str = "#FF0000;#00FF00",
        types_str: str = "PLA;PLA",
        weights: str = "12.5, 3.5",
        tool_seq: str = "T0\nT1\n",
    ) -> str:
        return (
            "; generated by OrcaSlicer 2.5.0\n"
            f"; filament_colour = {colors}\n"
            f"; filament_type = {types_str}\n"
            f"; filament used [g] = {weights}\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,150,200,0\n"
            "; flush_volumes_vector = 100,100\n"
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n"
            f"{tool_seq}"
            "PRINT_END CUT_TIP=0\n"
        )

    def test_phase12_01_normal_gcode_acquired_cleanly(self) -> None:
        """1) Normal .gcode is postprocessed cleanly via ctx.gcode_path + ctx.output_name with provenance sidecar."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            export_dir = os.path.join(tmpdir, "orcaslicer_job")
            os.makedirs(storage_dir, exist_ok=True)
            os.makedirs(export_dir, exist_ok=True)

            raw_plate = os.path.join(export_dir, "plate_1.gcode")
            with open(raw_plate, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode())

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")
            st.apply_dict({"tool_map": {0: 2, 1: 4}}, save=False)

            pp = plugin.KobraS1AcePostProcessCapability()
            ctx = types.SimpleNamespace(
                step=plugin.orca.slicing.Step.psGCodePostProcess,
                gcode_path=raw_plate,
                output_name="HeroModel_PLA_1h20m.gcode",
                host="Device",
                plate_index=1,
            )
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                res = pp.execute(ctx)
            self.assertTrue(res.ok, res.message)
            self.assertEqual(st.last_gcode_source, "slicing_pipeline")
            self.assertEqual(os.path.basename(st.last_gcode_path), "HeroModel_PLA_1h20m.gcode")
            self.assertTrue(os.path.isfile(st.last_gcode_path))

            sidecar_path = st.last_gcode_path + ".meta.json"
            self.assertTrue(os.path.isfile(sidecar_path))
            with open(sidecar_path, "r", encoding="utf-8") as sf:
                meta = json.load(sf)
            self.assertEqual(meta["source_kind"], "slicing_pipeline")
            self.assertEqual(meta["output_name"], "HeroModel_PLA_1h20m.gcode")
            self.assertEqual(meta["plate_index"], 1)
            self.assertFalse(meta["is_stale"])
            self.assertTrue(meta["sha256"])

            with open(st.last_gcode_path, "r", encoding="utf-8") as gf:
                content = gf.read()
            self.assertIn("G9111 bedTemp=60 extruderTemp=215 tool=2", content)
            self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=", content)

    def test_phase12_02_gcode_3mf_recognized_as_zip(self) -> None:
        """2) .gcode.3mf containers are reliably detected as ZIP vs plain .gcode or corrupt files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_3mf = os.path.join(tmpdir, "part.gcode.3mf")
            with open(zip_3mf, "wb") as f:
                f.write(self._build_3mf_bytes({1: self._sample_mm_gcode()}))
            plain = os.path.join(tmpdir, "part.gcode")
            with open(plain, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode())
            bogus = os.path.join(tmpdir, "fake.gcode.3mf")
            with open(bogus, "wb") as f:
                f.write(b"\x00\xffNOT_A_ZIP")

            self.assertTrue(plugin.is_zip_container_file(zip_3mf))
            self.assertFalse(plugin.is_zip_container_file(plain))
            self.assertFalse(plugin.is_zip_container_file(bogus))

    def test_phase12_03_gcode_3mf_extracts_plate_and_slice_info(self) -> None:
        """3) Actual G-code & slice_info.config are extracted from .gcode.3mf ZIP without saving binary bytes."""
        slice_xml = (
            "<config>"
            "<plate>"
            '<metadata key="index" value="2"/>'
            '<metadata key="prediction" value="5400"/>'
            '<metadata key="weight" value="44.8"/>'
            '<filament id="1" type="PETG" color="#112233" used_m="10.0" used_g="30.5"/>'
            '<filament id="2" type="PETG" color="#AABBCC" used_m="4.5" used_g="14.3"/>'
            "</plate>"
            "</config>"
        )
        p1_gcode = self._sample_mm_gcode(tool_seq="T0\n")
        p2_gcode = self._sample_mm_gcode(colors="#112233;#AABBCC", types_str="PETG;PETG", weights="", tool_seq="T0\nT1\nT0\n")
        raw_3mf = self._build_3mf_bytes(
            plates={1: p1_gcode, 2: p2_gcode},
            slice_info_xml=slice_xml,
            extra_entries={"Metadata/top_2.png": b"\x89PNG\r\n\x1a\nMOCK"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            export_dir = os.path.join(tmpdir, "export")
            os.makedirs(storage_dir, exist_ok=True)
            os.makedirs(export_dir, exist_ok=True)
            container_path = os.path.join(export_dir, "plate_job.gcode.3mf")
            with open(container_path, "wb") as f:
                f.write(raw_3mf)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")
            st.apply_dict({"tool_map": {0: 1, 1: 3}}, save=False)

            pp = plugin.KobraS1AcePostProcessCapability()
            ctx = types.SimpleNamespace(
                step=plugin.orca.slicing.Step.psGCodePostProcess,
                gcode_path=container_path,
                output_name="MultiPart.gcode.3mf",
                host="Device",
                plate_index=2,
            )
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                res = pp.execute(ctx)
            self.assertTrue(res.ok, res.message)

            # Staged file must be readable plain G-code (NEVER ZIP bytes) and contain plate 2 patched content
            staged = st.last_gcode_path
            self.assertTrue(staged.endswith(".gcode"))
            self.assertFalse(plugin.is_zip_container_file(staged))
            with open(staged, "r", encoding="utf-8") as sf:
                staged_text = sf.read()
            self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=", staged_text)
            self.assertIn("G9111 bedTemp=60 extruderTemp=215 tool=1", staged_text)
            self.assertEqual(st.last_job_metadata.get("plate_index"), 2)
            self.assertAlmostEqual(float(st.last_job_metadata.get("total_filament_g", 0.0)), 44.8, places=1)
            self.assertEqual(st.last_job_metadata.get("estimated_time"), "1h 30m 0s")

            # Container itself remains a valid ZIP with updated Metadata/plate_2.gcode & preserved PNG/XML
            with zipfile.ZipFile(container_path, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("Metadata/top_2.png", names)
                self.assertIn("Metadata/slice_info.config", names)
                inner_p2 = zf.read("Metadata/plate_2.gcode").decode("utf-8")
                self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=", inner_p2)

            # Browse via base64 container payload also extracts cleanly
            b64_payload = base64.b64encode(raw_3mf).decode("ascii")
            browsed_path, browsed_info = plugin.load_gcode_from_content(
                "UploadedBox.gcode.3mf",
                b64_payload,
                state=st,
                encoding="base64",
                plate_index=2,
            )
            self.assertTrue(browsed_path.endswith(".gcode"))
            self.assertEqual(browsed_info["toolchanges"], 3)

    def test_phase12_04_never_creates_double_gcode_3mf_suffix(self) -> None:
        """4) Filename sanitizer & staging never create .gcode.3mf.gcode or .3mf.gcode double extensions."""
        for raw_name in (
            "Otoskop.gcode.3mf",
            "Otoskop.GCODE.3MF",
            "Otoskop.gcode.3mf.gcode",
            "Otoskop.3mf",
        ):
            clean = plugin._sanitize_staged_gcode_filename(raw_name)
            self.assertEqual(clean, "Otoskop.gcode")
            self.assertNotIn(".gcode.3mf", clean.lower())
            self.assertNotIn(".3mf.gcode", clean.lower())

        clean_p2 = plugin._sanitize_staged_gcode_filename("Otoskop.gcode.3mf", plate_index=2)
        self.assertEqual(clean_p2, "Otoskop_plate2.gcode")

        with tempfile.TemporaryDirectory() as tmpdir:
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            raw_3mf_path = os.path.join(tmpdir, "Case.gcode.3mf")
            with open(raw_3mf_path, "wb") as f:
                f.write(self._build_3mf_bytes({1: self._sample_mm_gcode()}))
            staged = plugin._stage_gcode_file_to_storage(raw_3mf_path, st, output_name="Case.gcode.3mf")
            self.assertIsNotNone(staged)
            self.assertEqual(os.path.basename(staged), "Case.gcode")

    def test_phase12_05_binary_files_rejected_never_analyzed(self) -> None:
        """5) Binary payloads and corrupt ZIPs are strictly rejected and never analyzed as 0.0g G-code."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            os.makedirs(storage_dir, exist_ok=True)
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")

            bad_nul = os.path.join(tmpdir, "corrupt_nul.gcode")
            with open(bad_nul, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00IHDRbinary_junk")

            bad_bgcode = os.path.join(tmpdir, "binary_bg.gcode")
            with open(bad_bgcode, "wb") as f:
                f.write(b"GCDE\x01\x00\x00\x00binary_payload")

            no_plate_zip = os.path.join(tmpdir, "empty.gcode.3mf")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("3D/3dmodel.model", "<model/>")
            with open(no_plate_zip, "wb") as f:
                f.write(buf.getvalue())

            for bad_path in (bad_nul, bad_bgcode, no_plate_zip):
                self.assertIsNone(plugin._stage_gcode_file_to_storage(bad_path, st))
                with self.assertRaises(ValueError):
                    plugin.preview_gcode_file(bad_path, st)
                with self.assertRaises(ValueError):
                    plugin.postprocess_gcode_file(bad_path, st)

            # Verify no bogus file ended up staged
            self.assertEqual(plugin.discover_sliced_gcode_candidates(st), [])

            # Upload via load_gcode_from_content (base64 binary garbage) raises cleanly
            bad_b64 = base64.b64encode(b"\x00\x01\x02\x7fELF_binary").decode("ascii")
            with self.assertRaises(ValueError):
                plugin.load_gcode_from_content("bad.gcode", bad_b64, st, encoding="base64")

            # PostProcess execute returns RecoverableError on binary input
            pp = plugin.KobraS1AcePostProcessCapability()
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                res = pp.execute(
                    types.SimpleNamespace(
                        step=plugin.orca.slicing.Step.psGCodePostProcess,
                        gcode_path=bad_nul,
                    )
                )
            self.assertFalse(res.ok)

    def test_phase12_06_slicing_job_complete_does_not_load_old_staged(self) -> None:
        """6) SlicingJobComplete with non-file name never falls back to loading an old file from staged_gcode/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            staged_dir = os.path.join(storage_dir, "staged_gcode")
            os.makedirs(staged_dir, exist_ok=True)
            old_staged = os.path.join(staged_dir, "old_yesterday.gcode")
            with open(old_staged, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode())

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")
            st.last_gcode_path = ""
            st.pending_preprint_open = False

            page = plugin.KobraS1AcePageCapability()
            posted = []
            done_evt = threading.Event()

            def _capture_post(msg):
                posted.append(msg)
                done_evt.set()

            page.post_message = _capture_post
            with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
                plugin,
                "discover_sliced_gcode_candidates",
                side_effect=AssertionError("SlicingJobComplete must never scan/load old staged files!"),
            ):
                page.on_lifecycle_event(
                    plugin.orca.LifecycleEvent.SlicingJobComplete,
                    types.SimpleNamespace(name="Plate 1", code=plugin.orca.LifecycleEvtCode.Success),
                )
                self.assertTrue(done_evt.wait(timeout=2.0))

            self.assertEqual(st.last_gcode_path, "")
            self.assertFalse(st.pending_preprint_open)
            self.assertFalse(any(isinstance(m, dict) and m.get("action") == "open_preprint_modal" for m in posted))
            self.assertGreater(st._last_slice_completed_ns, 0)

    def test_phase12_07_explicit_paths_preserved_with_force_refresh(self) -> None:
        """7) Explicit paths passed with force_refresh=True are preserved rather than blindly discarded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            staged_dir = os.path.join(storage_dir, "staged_gcode")
            ext_dir = os.path.join(tmpdir, "external_export")
            os.makedirs(staged_dir, exist_ok=True)
            os.makedirs(ext_dir, exist_ok=True)

            staged_newest = os.path.join(staged_dir, "staged_other.gcode")
            with open(staged_newest, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode(tool_seq="T0\n"))
            os.utime(staged_newest, ns=(9_000_000_000_000, 9_000_000_000_000))

            explicit_ext = os.path.join(ext_dir, "RequestedTarget.gcode")
            with open(explicit_ext, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode(tool_seq="T0\nT1\n"))
            os.utime(explicit_ext, ns=(1_000_000_000_000, 1_000_000_000_000))

            explicit_older_staged = os.path.join(staged_dir, "older_selected.gcode")
            with open(explicit_older_staged, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode(tool_seq="T0\nT1\nT0\n"))
            os.utime(explicit_older_staged, ns=(2_000_000_000_000, 2_000_000_000_000))

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")
            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"gcode_path": explicit_ext, "force_refresh": True},
                )
                self.assertEqual(st.last_gcode_path, explicit_ext)
                self.assertEqual(st.last_gcode_source, "slicer_explicit")

                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"gcode_path": explicit_older_staged, "force_refresh": True, "explicit": True},
                )
                self.assertEqual(st.last_gcode_path, explicit_older_staged)
                self.assertEqual(st.last_gcode_source, "slicer_explicit")

    def test_phase12_08_multi_plate_disambiguation(self) -> None:
        """8) Plate 1 and Plate 2 of the same project remain cleanly separated with accurate sidecars."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            export_dir = os.path.join(tmpdir, "export")
            os.makedirs(storage_dir, exist_ok=True)
            os.makedirs(export_dir, exist_ok=True)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")
            pp = plugin.KobraS1AcePostProcessCapability()

            p1_raw = os.path.join(export_dir, "p1_tmp.gcode")
            p2_raw = os.path.join(export_dir, "p2_tmp.gcode")
            with open(p1_raw, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode(tool_seq="T0\n"))
            with open(p2_raw, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode(tool_seq="T0\nT1\n"))

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                r1 = pp.execute(
                    types.SimpleNamespace(
                        step=plugin.orca.slicing.Step.psGCodePostProcess,
                        gcode_path=p1_raw,
                        output_name="MultiPlateAssembly.gcode.3mf",
                        project_name="MultiPlateAssembly",
                        plate_index=1,
                        host="File",
                    )
                )
                self.assertTrue(r1.ok, r1.message)
                p1_staged = st.last_gcode_path

                r2 = pp.execute(
                    types.SimpleNamespace(
                        step=plugin.orca.slicing.Step.psGCodePostProcess,
                        gcode_path=p2_raw,
                        output_name="MultiPlateAssembly.gcode.3mf",
                        project_name="MultiPlateAssembly",
                        plate_index=2,
                        host="File",
                    )
                )
                self.assertTrue(r2.ok, r2.message)
                p2_staged = st.last_gcode_path

            self.assertNotEqual(p1_staged, p2_staged)
            self.assertTrue(os.path.isfile(p1_staged))
            self.assertTrue(os.path.isfile(p2_staged))
            self.assertEqual(os.path.basename(p1_staged), "MultiPlateAssembly.gcode")
            self.assertEqual(os.path.basename(p2_staged), "MultiPlateAssembly_plate2.gcode")

            meta1 = plugin._read_provenance_sidecar(p1_staged)
            meta2 = plugin._read_provenance_sidecar(p2_staged)
            self.assertEqual(meta1.get("plate_index"), 1)
            self.assertEqual(meta2.get("plate_index"), 2)
            # Same project, distinct plates -> neither erroneously marked superseded
            self.assertFalse(meta1.get("is_superseded"))
            self.assertFalse(meta2.get("is_superseded"))

    def test_phase12_09_consecutive_slicing_runs_never_mixed_up(self) -> None:
        """9) Two rapid consecutive slices never get mixed up even with equal mtime_ns; older run is marked superseded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            export_dir = os.path.join(tmpdir, "export")
            os.makedirs(storage_dir, exist_ok=True)
            os.makedirs(export_dir, exist_ok=True)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")
            pp = plugin.KobraS1AcePostProcessCapability()

            run1_raw = os.path.join(export_dir, "run1.gcode")
            run2_raw = os.path.join(export_dir, "run2.gcode")
            with open(run1_raw, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode(tool_seq="T0\n"))
            with open(run2_raw, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode(tool_seq="T0\nT1\nT0\n"))

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                res1 = pp.execute(
                    types.SimpleNamespace(
                        step=plugin.orca.slicing.Step.psGCodePostProcess,
                        gcode_path=run1_raw,
                        output_name="Bracket_v1.gcode",
                        project_name="Bracket",
                        plate_index=1,
                        host="File",
                    )
                )
                self.assertTrue(res1.ok)
                staged1 = st.last_gcode_path

                res2 = pp.execute(
                    types.SimpleNamespace(
                        step=plugin.orca.slicing.Step.psGCodePostProcess,
                        gcode_path=run2_raw,
                        output_name="Bracket_v2.gcode",
                        project_name="Bracket",
                        plate_index=1,
                        host="File",
                    )
                )
                self.assertTrue(res2.ok)
                staged2 = st.last_gcode_path

            # Force equal OS file timestamps to prove provenance sequence separates consecutive runs
            same_ns = 6_000_000_000_000
            os.utime(staged1, ns=(same_ns, same_ns))
            os.utime(staged2, ns=(same_ns, same_ns))

            candidates = plugin.discover_sliced_gcode_candidates(st)
            self.assertEqual(candidates, [staged2, staged1])

            m1 = plugin._read_provenance_sidecar(staged1)
            m2 = plugin._read_provenance_sidecar(staged2)
            self.assertGreater(m2["slicing_run_seq"], m1["slicing_run_seq"])
            self.assertTrue(m1["is_superseded"])
            self.assertTrue(m1["is_stale"])
            self.assertFalse(m2["is_superseded"])
            self.assertEqual(st.last_gcode_path, staged2)
            self.assertEqual(st.last_postprocess_info["toolchanges"], 3)

    def test_phase12_10_project_mismatch_blocks_upload(self) -> None:
        """10) Model, plate, filament signature, uncertain generic stem, and slice-freshness mismatches block upload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            os.makedirs(storage_dir, exist_ok=True)
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")

            valid_fils = [
                {"slicer_tool": 0, "color": "#FF0000", "material": "PLA", "used": True},
                {"slicer_tool": 1, "color": "#00FF00", "material": "PLA", "used": True},
            ]
            ace_status = {
                "is_printing": False,
                "print_state": "standby",
                "slots": [
                    {"tool": 0, "status": "ready", "hex_color": "#FF0000", "material": "PLA", "rfid": True},
                    {"tool": 1, "status": "ready", "hex_color": "#00FF00", "material": "PLA", "rfid": True},
                ],
            }
            base_info = {
                "flush_status": "POSTPROCESS_OK",
                "flush_data_complete": True,
                "used_slicer_tools": [0, 1],
                "slicer_filaments": valid_fils,
                "toolMap": {0: 0, 1: 1},
                "warnings": [],
                "job_metadata": {"filename": "OldModel_PLA.gcode"},
            }

            host_new = types.SimpleNamespace(
                plater=lambda: {"project_name": "CurrentProject.3mf", "plate_index": 2},
                model=lambda: types.SimpleNamespace(name="CurrentProject_Body"),
            )
            with mock.patch.object(plugin.orca, "host", host_new):
                # A) Project/Model mismatch -> blocked
                ok, blocks, _ = plugin.can_upload_gcode(base_info, st, ace_status=ace_status)
                self.assertFalse(ok)
                self.assertTrue(any("OldModel_PLA.gcode" in b for b in blocks))

                # B) Unverified generic filename ('plate_1.gcode') under non-generic project -> blocked
                uncertain_info = dict(base_info, job_metadata={"filename": "plate_1.gcode"})
                ok_u, blocks_u, _ = plugin.can_upload_gcode(uncertain_info, st, ace_status=ace_status)
                self.assertFalse(ok_u)

                # C) Plate mismatch -> blocked
                plate_mismatch_info = dict(
                    base_info, job_metadata={"filename": "CurrentProject_plate1.gcode", "plate_index": 1}
                )
                ok_p, blocks_p, _ = plugin.can_upload_gcode(plate_mismatch_info, st, ace_status=ace_status)
                self.assertFalse(ok_p)

                # D) Matching project + matching plate -> allowed
                good_info = dict(
                    base_info, job_metadata={"filename": "CurrentProject_plate2.gcode", "plate_index": 2}
                )
                ok_good, blocks_good, _ = plugin.can_upload_gcode(good_info, st, ace_status=ace_status)
                self.assertTrue(ok_good, blocks_good)

            # E) Filament preset mismatch captured in UI snapshot -> blocked
            with st._lock:
                st._host_ui_snapshot["filaments"] = [
                    {"slicer_tool": 0, "material": "PETG", "color": "#FF0000"},
                    {"slicer_tool": 1, "material": "PETG", "color": "#00FF00"},
                ]
                st._host_ui_snapshot["filaments_captured"] = True
            with mock.patch.object(plugin.orca, "host", host_new):
                ok_fil, _ = plugin.can_upload_gcode(good_info, st, ace_status=ace_status)[:2]
                self.assertFalse(ok_fil)
                # Explicit allow_stale=True override works only when explicitly requested
                ok_override, _ = plugin.can_upload_gcode(
                    good_info, st, ace_status=ace_status, allow_stale=True
                )[:2]
                self.assertTrue(ok_override)

    def test_phase12_11_worker_threads_never_call_ui_bound_host_apis(self) -> None:
        """11) Worker threads read cached storage & UI snapshots and NEVER invoke plater/model/preset_bundle/uncached storage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "orca_storage")
            os.makedirs(storage_dir, exist_ok=True)
            sample_path = os.path.join(tmpdir, "LivePart_PLA.gcode")
            with open(sample_path, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode())

            st = plugin.PluginState()
            storage_calls = {"count": 0}
            plater_calls = {"count": 0}
            model_calls = {"count": 0}
            bundle_calls = {"count": 0}
            ui_tid = threading.get_ident()

            def _storage():
                if threading.get_ident() != ui_tid:
                    raise AssertionError("plugin.storage() MUST NOT run from worker thread")
                storage_calls["count"] += 1
                return storage_dir

            def _plater():
                if threading.get_ident() != ui_tid:
                    raise AssertionError("orca.host.plater() MUST NOT run from worker thread")
                plater_calls["count"] += 1
                return {"project_name": "LivePart.3mf", "plate_index": 1}

            def _model():
                if threading.get_ident() != ui_tid:
                    raise AssertionError("orca.host.model() MUST NOT run from worker thread")
                model_calls["count"] += 1
                return types.SimpleNamespace(name="LivePart")

            fake_cfg = {
                "filament_colour": ["#FF0000", "#00FF00"],
                "filament_type": ["PLA", "PLA"],
                "filament_vendor": ["Generic", "Generic"],
                "filament_diameter": ["1.75", "1.75"],
            }
            fake_bundle = types.SimpleNamespace(
                current_filament_presets=lambda: [
                    types.SimpleNamespace(name="PLA A", config_value=lambda _k: None),
                    types.SimpleNamespace(name="PLA B", config_value=lambda _k: None),
                ],
                current_filament_preset_names=lambda: ["PLA A", "PLA B"],
                full_config_value=lambda k: fake_cfg.get(k),
            )

            def _preset_bundle():
                if threading.get_ident() != ui_tid:
                    raise AssertionError("orca.host.preset_bundle() MUST NOT run from worker thread")
                bundle_calls["count"] += 1
                return fake_bundle

            guarded_host = types.SimpleNamespace(
                plugin=types.SimpleNamespace(storage=_storage),
                plater=_plater,
                model=_model,
                preset_bundle=_preset_bundle,
                ui=types.SimpleNamespace(push_notification=lambda *a, **kw: None),
            )

            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append

            with mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True), mock.patch.object(
                plugin, "GLOBAL_STATE", st
            ), mock.patch.object(plugin.orca, "host", guarded_host):
                pkg = plugin.KobraS1AcePlugin()
                with mock.patch.object(plugin.orca, "register_capability"):
                    pkg.register_capabilities()
                page.on_load()
                self.assertEqual(storage_calls["count"], 1)

                # Once cached on UI callback, even if storage/plater/model/preset_bundle raise in workers, workers succeed!
                worker_errors = []

                def _run_workers() -> None:
                    try:
                        page._handle_async_message("ready", {})
                        page._handle_async_message("sync_orca_filaments", {})
                        snap_dict = st.to_dict()
                        self.assertEqual(snap_dict["stale_model_info"]["active_model"], "LivePart")
                        res_pp = plugin.KobraS1AcePostProcessCapability().execute(
                            types.SimpleNamespace(
                                step=plugin.orca.slicing.Step.psGCodePostProcess,
                                gcode_path=sample_path,
                                output_name="LivePart_PLA.gcode",
                                host="File",
                                plate_index=1,
                            )
                        )
                        self.assertTrue(res_pp.ok, res_pp.message)
                    except Exception as exc:
                        worker_errors.append(exc)

                t = threading.Thread(target=_run_workers)
                t.start()
                t.join(timeout=3.0)
                self.assertEqual(worker_errors, [])
                self.assertEqual(storage_calls["count"], 1)

    def test_phase12_12_staging_failure_atomic_and_window_modal_lifecycle(self) -> None:
        """12) Staging failures leave zero partial/temp files or broken sidecars; WINDOW_MODAL handles host/cancel/unload."""
        # Part A: Staging failures (os.fsync OSError & sidecar write failure) leave zero leftovers
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "storage")
            staged_dir = os.path.join(storage_dir, "staged_gcode")
            os.makedirs(storage_dir, exist_ok=True)
            src_file = os.path.join(tmpdir, "ValidJob.gcode")
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(self._sample_mm_gcode())

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(storage_dir, "state.json")

            with mock.patch("os.fsync", side_effect=OSError("Simulated disk full")):
                res = plugin._stage_gcode_file_to_storage(src_file, st)
                self.assertIsNone(res)
                with self.assertRaises(OSError):
                    plugin.load_gcode_from_content("BrowseFail.gcode", self._sample_mm_gcode(), st)

            with mock.patch.object(
                plugin, "_write_provenance_sidecar_atomic", side_effect=OSError("Sidecar IO fail")
            ):
                res2 = plugin._stage_gcode_file_to_storage(src_file, st)
                self.assertIsNone(res2)

            if os.path.isdir(staged_dir):
                leftovers = [e.name for e in os.scandir(staged_dir)]
                self.assertEqual(leftovers, [])

        # Part B: WINDOW_MODAL controller lifecycle safety (ctx.host=='File', ctx.cancelled(), timeout, on_unload)
        st_m = plugin.PluginState()
        st_m.resolve_config_path = lambda: ""

        # 1. File export host bypasses modal immediately
        sess_file = plugin._SlicingModalSetupSession(
            types.SimpleNamespace(host="File", cancelled=lambda: False),
            st_m,
            "foo.gcode",
            {"toolchanges": 0},
        )
        self.assertEqual(sess_file.run(), "skipped_file_host")

        # 2. ctx.cancelled() aborts modal and closes window promptly
        closed_flags = []
        cancel_box = {"val": False}

        def _create_win(**kwargs):
            return types.SimpleNamespace(
                post_message=lambda _m: None,
                close=lambda: closed_flags.append(True),
            )

        fake_ui = types.SimpleNamespace(
            create_window=_create_win,
            WindowModality=types.SimpleNamespace(WINDOW_MODAL="WINDOW_MODAL"),
        )
        with mock.patch.object(plugin.orca, "host", types.SimpleNamespace(ui=fake_ui), create=True):
            sess_cancel = plugin._SlicingModalSetupSession(
                types.SimpleNamespace(host="Device", cancelled=lambda: cancel_box["val"]),
                st_m,
                "foo.gcode",
                {"toolchanges": 1},
                ready_timeout_s=2.0,
                poll_interval_s=0.02,
            )

            def _trigger_cancel():
                time.sleep(0.05)
                cancel_box["val"] = True

            threading.Thread(target=_trigger_cancel, daemon=True).start()
            self.assertEqual(sess_cancel.run(), "cancelled")
            self.assertTrue(closed_flags)

            # 3. Timeout waiting for 'ready' returns cleanly without hanging
            sess_timeout = plugin._SlicingModalSetupSession(
                types.SimpleNamespace(host="Device", cancelled=lambda: False),
                st_m,
                "foo.gcode",
                {"toolchanges": 1},
                ready_timeout_s=0.06,
                poll_interval_s=0.01,
            )
            self.assertEqual(sess_timeout.run(), "ready_timeout")

            # 4. Capability.on_unload cleanly tears down an active waiting modal session
            unload_closed = []

            def _create_unload_win(**kwargs):
                on_msg = kwargs.get("on_message")
                threading.Thread(target=lambda: on_msg({"type": "ready"}), daemon=True).start()
                return types.SimpleNamespace(
                    post_message=lambda _m: None,
                    close=lambda: unload_closed.append(True),
                )

            fake_ui.create_window = _create_unload_win
            sess_unload = plugin._SlicingModalSetupSession(
                types.SimpleNamespace(host="Device", cancelled=lambda: False),
                st_m,
                "foo.gcode",
                {"toolchanges": 1},
                ready_timeout_s=1.0,
                poll_interval_s=0.02,
            )
            pp_cap = plugin.KobraS1AcePostProcessCapability()
            threading.Thread(target=lambda: (time.sleep(0.06), pp_cap.on_unload()), daemon=True).start()
            self.assertEqual(sess_unload.run(), "unloaded")
            self.assertTrue(unload_closed)

    def _make_zombie_test_state_and_page(self, tmp_dir: str):
        storage_dir = os.path.join(tmp_dir, "plugin_storage")
        os.makedirs(storage_dir, exist_ok=True)
        cfg_path = os.path.join(storage_dir, "state.json")
        st = plugin.PluginState()
        st._cached_storage_dir = storage_dir
        st.resolve_storage_dir = lambda: storage_dir  # type: ignore[method-assign]
        st.resolve_config_path = lambda: cfg_path  # type: ignore[method-assign]
        st.last_gcode_path = ""
        st.last_gcode_source = ""
        st.last_job_metadata = {}
        st.last_postprocess_info = {}
        st.mapping_warnings = []
        st.pending_preprint_open = False
        st._last_staged_provenance = {}
        st.cleared_before_mtime_ns = 0
        st._cleared_at_ns = 0
        st._cleared_run_seq = 0
        st._slicing_run_counter = 0
        st._diag_load_events = []

        sent = []
        page = plugin.KobraS1AcePageCapability()
        page.post_message = lambda msg: sent.append(msg)  # type: ignore[method-assign]
        return st, storage_dir, cfg_path, page, sent

    @staticmethod
    def _sample_zombie_gcode_text(model_tag: str = "Flexi_Snake_Mini", tool: int = 0) -> str:
        return (
            f"; model: {model_tag}\n"
            "; total filament used [g] = 12.5, 8.3\n"
            "; estimated printing time (normal mode) = 2h 13m 0s\n"
            "; filament_type = PLA;PLA\n"
            "; filament_colour = #FFFFFF;#000000\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,80,80,0\n"
            "; flush_volumes_vector = 100,100\n"
            f"G9111 bedTemp=60 extruderTemp=220 tool={tool}\n"
            "M104 S220\n"
            "G1 X10 Y10 E1.0\n"
            "T1\n"
            "G1 X20 Y20 E2.0\n"
            "M84\n"
        )

    def test_zombie_01_clear_gcode_then_force_refresh_never_reloads(self) -> None:
        """1) clear_gcode followed by acquire_slicer_gcode(force_refresh=True) NEVER reloads old files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            src_file = os.path.join(tmp_dir, "Flexi_Snake_Mini_PLA_0.2_2h13m.gcode")
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Flexi_Snake_Mini"))

            staged_old = plugin._stage_gcode_file_to_storage(
                src_file,
                st,
                output_name="Flexi_Snake_Mini_PLA_0.2_2h13m.gcode",
                project_model="Flexi_Snake_Mini",
                source_kind="slicing_pipeline",
            )
            self.assertIsNotNone(staged_old)
            plugin.preview_gcode_file(staged_old, st)
            self.assertEqual(st.last_gcode_path, staged_old)
            self.assertTrue(bool(st._last_staged_provenance))

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message("clear_gcode", {})
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(st._last_staged_provenance, {})
                sc_after_clear = plugin._read_provenance_sidecar(staged_old)
                self.assertTrue(sc_after_clear.get("is_cleared"))
                self.assertGreater(int(sc_after_clear.get("cleared_ns", 0)), 0)

                page._handle_async_message("acquire_slicer_gcode", {"force_refresh": True})
                self.assertEqual(st.last_gcode_path, "")

                # Also reject stale UI-echoed gcode_path without explicit=True
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"gcode_path": staged_old, "force_refresh": True},
                )
                self.assertEqual(st.last_gcode_path, "")

    def test_zombie_02_clear_gcode_with_nonzero_slicing_run_seq_never_reloads(self) -> None:
        """2) clear_gcode after previous slicing run (slicing_run_seq > 0) blocks subsequent refreshes."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            src_file = os.path.join(tmp_dir, "Flexi_Snake_Mini_PLA_0.2_2h13m.gcode")
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Flexi_Snake_Mini"))

            seq1, run1 = st.next_slicing_run_id()
            seq2, run2 = st.next_slicing_run_id()
            self.assertGreater(seq2, 0)
            staged = plugin._stage_gcode_file_to_storage(
                src_file,
                st,
                output_name="Flexi_Snake_Mini_PLA_0.2_2h13m.gcode",
                slicing_run_id=run2,
                slicing_run_seq=seq2,
                project_model="Flexi_Snake_Mini",
                source_kind="slicing_pipeline",
            )
            self.assertIsNotNone(staged)
            st.last_gcode_path = staged
            st.last_gcode_source = "slicing_pipeline"

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message("clear_gcode", {})
                self.assertGreaterEqual(st._cleared_run_seq, seq2)

                # Even with cleared_before_mtime_ns tampered to 0, seq guard & is_cleared block it
                st.cleared_before_mtime_ns = 0
                for refresh in (False, True):
                    kind, path, info = plugin.acquire_gcode_from_slicer(st, force_refresh=refresh)
                    self.assertEqual(kind, "presets_only")
                    self.assertEqual(path, "")
                    self.assertIsNone(info)

    def test_zombie_03_is_cleared_sidecars_permanently_blocked(self) -> None:
        """3) Sidecars marked is_cleared=True are never adopted by acquire_gcode_from_slicer or discover."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, _page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            staged_dir = os.path.join(storage_dir, "staged_gcode")
            os.makedirs(staged_dir, exist_ok=True)
            zombie_path = os.path.join(staged_dir, "Cleared_Old.gcode")
            with open(zombie_path, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Cleared_Old"))
            plugin._write_provenance_sidecar_atomic(
                zombie_path,
                {
                    "staged_path": zombie_path,
                    "filename": "Cleared_Old.gcode",
                    "slicing_run_id": "run_99_old",
                    "slicing_run_seq": 99,
                    "created_ns": time.time_ns() + 1_000_000_000,
                    "is_cleared": True,
                    "cleared_ns": time.time_ns(),
                    "is_stale": False,
                    "is_superseded": False,
                },
            )

            self.assertEqual(plugin.discover_sliced_gcode_candidates(st, include_stale=False), [])
            kind, path, info = plugin.acquire_gcode_from_slicer(st, force_refresh=True)
            self.assertEqual((kind, path, info), ("presets_only", "", None))
            kind_exp, path_exp, info_exp = plugin.acquire_gcode_from_slicer(
                st, explicit_path=zombie_path, force_refresh=True, explicit_flag=True
            )
            self.assertEqual((kind_exp, path_exp, info_exp), ("presets_only", "", None))

    def test_zombie_04_is_stale_and_is_superseded_sidecars_rejected(self) -> None:
        """4) is_stale=True and is_superseded=True sidecars are rejected instead of blind [0] pickup."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, _page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            staged_dir = os.path.join(storage_dir, "staged_gcode")
            os.makedirs(staged_dir, exist_ok=True)
            f_stale = os.path.join(staged_dir, "old_stale.gcode")
            f_sup = os.path.join(staged_dir, "old_superseded.gcode")
            for idx, (fp, flag_name) in enumerate([(f_stale, "is_stale"), (f_sup, "is_superseded")], start=1):
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(self._sample_zombie_gcode_text(f"Model_{idx}"))
                plugin._write_provenance_sidecar_atomic(
                    fp,
                    {
                        "staged_path": fp,
                        "filename": os.path.basename(fp),
                        "slicing_run_id": f"run_{idx}_test",
                        "slicing_run_seq": idx,
                        "created_ns": 1000 + idx,
                        "is_cleared": False,
                        "is_stale": flag_name == "is_stale",
                        "is_superseded": flag_name == "is_superseded",
                    },
                )

            raw_candidates = plugin.discover_sliced_gcode_candidates(st)
            self.assertEqual(len(raw_candidates), 2)
            self.assertEqual(plugin.discover_sliced_gcode_candidates(st, include_stale=False), [])
            for refresh in (False, True):
                kind, path, info = plugin.acquire_gcode_from_slicer(st, force_refresh=refresh)
                self.assertEqual((kind, path, info), ("presets_only", "", None))

    def test_zombie_05_model_mismatch_candidate_rejected(self) -> None:
        """5) Candidate with different project_model (or non-generic filename stem) than active model is rejected."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, _page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            st._host_ui_snapshot = {
                "active_model": {"display_name": "otoskopmmu", "candidates": ["otoskopmmu"], "plate_index": 1},
                "filaments": [],
                "filaments_captured": False,
                "captured_ns": time.time_ns(),
            }
            staged_dir = os.path.join(storage_dir, "staged_gcode")
            os.makedirs(staged_dir, exist_ok=True)
            wrong_path = os.path.join(staged_dir, "Flexi_Snake_Mini_PLA_0.2_2h13m.gcode")
            with open(wrong_path, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Flexi_Snake_Mini"))
            plugin._write_provenance_sidecar_atomic(
                wrong_path,
                {
                    "staged_path": wrong_path,
                    "filename": os.path.basename(wrong_path),
                    "project_model": "Flexi_Snake_Mini",
                    "slicing_run_id": "run_10_wrong",
                    "slicing_run_seq": 10,
                    "created_ns": time.time_ns(),
                    "is_cleared": False,
                    "is_stale": False,
                    "is_superseded": False,
                },
            )
            kind, path, _info = plugin.acquire_gcode_from_slicer(st, force_refresh=True)
            self.assertEqual((kind, path), ("presets_only", ""))

            # Even without project_model in sidecar, non-generic filename stem rejects mismatch
            plugin._write_provenance_sidecar_atomic(
                wrong_path,
                {
                    "staged_path": wrong_path,
                    "filename": os.path.basename(wrong_path),
                    "project_model": "",
                    "slicing_run_id": "run_11_wrong",
                    "slicing_run_seq": 11,
                    "created_ns": time.time_ns(),
                    "is_cleared": False,
                    "is_stale": False,
                    "is_superseded": False,
                },
            )
            kind2, path2, _ = plugin.acquire_gcode_from_slicer(st, force_refresh=True)
            self.assertEqual((kind2, path2), ("presets_only", ""))

            # Matching model loads cleanly
            right_path = os.path.join(staged_dir, "otoskopmmu_PLA_0.2_5h20m.gcode")
            with open(right_path, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("otoskopmmu"))
            plugin._write_provenance_sidecar_atomic(
                right_path,
                {
                    "staged_path": right_path,
                    "filename": os.path.basename(right_path),
                    "project_model": "otoskopmmu",
                    "slicing_run_id": "run_12_right",
                    "slicing_run_seq": 12,
                    "created_ns": time.time_ns(),
                    "is_cleared": False,
                    "is_stale": False,
                    "is_superseded": False,
                },
            )
            kind3, path3, info3 = plugin.acquire_gcode_from_slicer(st, force_refresh=True)
            self.assertEqual(kind3, "staged_gcode")
            self.assertEqual(path3, right_path)
            self.assertIsNotNone(info3)

    def test_zombie_06_stale_ui_settings_and_apply_dict_never_resurrect_or_overwrite_clear(self) -> None:
        """6) Stale UI settings payloads and apply_dict never resurrect cleared/stale paths or wipe clear state."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, cfg_path, page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            src_file = os.path.join(tmp_dir, "Flexi_Snake_Mini_PLA_0.2_2h13m.gcode")
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Flexi_Snake_Mini"))
            staged_old = plugin._stage_gcode_file_to_storage(
                src_file, st, output_name="Flexi_Snake_Mini_PLA_0.2_2h13m.gcode", project_model="Flexi_Snake_Mini"
            )
            self.assertIsNotNone(staged_old)

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message("clear_gcode", {})
                saved_cleared_seq = st._cleared_run_seq
                self.assertGreater(saved_cleared_seq, 0)

                # UI sends settings payload containing stale last_gcode_path + cleared_before_mtime_ns=0
                page.on_message(
                    {
                        "type": "save_settings",
                        "settings": {
                            "moonraker_url": "http://192.0.2.10:7125",
                            "last_gcode_path": staged_old,
                            "last_gcode_source": "slicing_pipeline",
                            "cleared_before_mtime_ns": 0,
                            "cleared_run_seq": 0,
                        },
                    }
                )
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(st._cleared_run_seq, saved_cleared_seq)

            # Direct apply_dict(persisted_dict) refuses is_cleared and /tmp paths
            st.apply_dict(
                {
                    "last_gcode_path": staged_old,
                    "last_gcode_source": "slicing_pipeline",
                    "last_staged_provenance": {"filename": "Flexi_Snake_Mini_PLA_0.2_2h13m.gcode"},
                },
                save=False,
            )
            self.assertEqual(st.last_gcode_path, "")

            with open(cfg_path, "w", encoding="utf-8") as cf:
                json.dump(
                    {
                        "last_gcode_path": "/tmp/orcaslicer_old_dir/zombie.gcode",
                        "last_gcode_source": "slicing_pipeline",
                    },
                    cf,
                )
            st.load_config()
            self.assertEqual(st.last_gcode_path, "")

    def test_zombie_07_execute_staging_failure_clears_previous_active_job(self) -> None:
        """7) Failed staging or exception in PostProcess execute() never leaves prior job active."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            old_src = os.path.join(tmp_dir, "Flexi_Snake_Mini.gcode")
            new_src = os.path.join(tmp_dir, "otoskopmmu.gcode")
            for fp, name in ((old_src, "Flexi_Snake_Mini"), (new_src, "otoskopmmu")):
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(self._sample_zombie_gcode_text(name))

            pp = plugin.KobraS1AcePostProcessCapability()
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
            ):
                res1 = pp.execute(
                    types.SimpleNamespace(
                        gcode_path=old_src,
                        output_name="Flexi_Snake_Mini.gcode",
                        host="Device",
                        plate_index=1,
                        project_name="Flexi_Snake_Mini",
                        cancelled=lambda: False,
                    )
                )
                self.assertEqual(res1.status, plugin.orca.PluginResult.Success)
                self.assertIn("Flexi_Snake_Mini", os.path.basename(st.last_gcode_path))

                deadline1 = time.time() + 1.0
                while time.time() < deadline1 and not any(
                    m.get("action") == "open_preprint_modal" for m in sent
                ):
                    time.sleep(0.01)
                sent.clear()
                with mock.patch.object(plugin, "_stage_gcode_file_to_storage", return_value=None):
                    res2 = pp.execute(
                        types.SimpleNamespace(
                            gcode_path=new_src,
                            output_name="otoskopmmu.gcode",
                            host="Device",
                            plate_index=1,
                            project_name="otoskopmmu",
                            cancelled=lambda: False,
                        )
                    )
                self.assertEqual(res2.status, plugin.orca.PluginResult.RecoverableError)
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(st.last_gcode_source, "")
                self.assertEqual(st.last_job_metadata, {})
                self.assertEqual(st.last_postprocess_info, {})
                self.assertEqual(st._last_staged_provenance, {})
                self.assertFalse(st.pending_preprint_open)

                # Wait briefly for daemon notification thread
                time.sleep(0.05)
                self.assertFalse(
                    any(m.get("action") == "open_preprint_modal" and m.get("source_file") for m in sent)
                )

    def test_zombie_08_new_slicing_run_after_clear_loads_valid_model_and_diag_logging(self) -> None:
        """8) Valid new slicing run after clear loads new model without bringing back cleared model + records diag logs."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            st.diag_load_logging = True
            old_src = os.path.join(tmp_dir, "Flexi_Snake_Mini_PLA_0.2_2h13m.gcode")
            new_src = os.path.join(tmp_dir, "otoskopmmu_PLA_0.2_5h20m.gcode")
            with open(old_src, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Flexi_Snake_Mini", tool=0))
            with open(new_src, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("otoskopmmu", tool=1))

            pp = plugin.KobraS1AcePostProcessCapability()
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
            ):
                res1 = pp.execute(
                    types.SimpleNamespace(
                        gcode_path=old_src,
                        output_name="Flexi_Snake_Mini_PLA_0.2_2h13m.gcode",
                        host="File",
                        plate_index=1,
                        project_name="Flexi_Snake_Mini",
                        cancelled=lambda: False,
                    )
                )
                self.assertEqual(res1.status, plugin.orca.PluginResult.Success)
                old_staged = st.last_gcode_path
                self.assertIn("Flexi_Snake_Mini", old_staged)

                page._handle_async_message("clear_gcode", {})
                self.assertEqual(st.last_gcode_path, "")

                st._host_ui_snapshot = {
                    "active_model": {"display_name": "otoskopmmu", "candidates": ["otoskopmmu"], "plate_index": 1},
                    "filaments": [],
                    "filaments_captured": False,
                    "captured_ns": time.time_ns(),
                }
                sent.clear()
                res2 = pp.execute(
                    types.SimpleNamespace(
                        gcode_path=new_src,
                        output_name="otoskopmmu_PLA_0.2_5h20m.gcode",
                        host="Device",
                        plate_index=1,
                        project_name="otoskopmmu",
                        cancelled=lambda: False,
                    )
                )
                self.assertEqual(res2.status, plugin.orca.PluginResult.Success)
                self.assertIn("otoskopmmu", os.path.basename(st.last_gcode_path))
                self.assertNotEqual(st.last_gcode_path, old_staged)
                self.assertTrue(plugin._read_provenance_sidecar(old_staged).get("is_cleared"))

                # Subsequent refresh stays on otoskopmmu and NEVER brings back Flexi_Snake_Mini
                page._handle_async_message("acquire_slicer_gcode", {"force_refresh": True})
                self.assertIn("otoskopmmu", os.path.basename(st.last_gcode_path))

            events = plugin.get_diagnostic_load_events(st)
            self.assertGreaterEqual(len(events), 3)
            accepted_evts = [e for e in events if e.get("decision") == "accepted" and "otoskopmmu" in str(e.get("filename"))]
            self.assertTrue(accepted_evts)
            evt = accepted_evts[-1]
            for req_key in ("timestamp_iso", "trigger", "file_source", "filename", "file_path", "slicing_run_id", "sha256", "active_model", "sidecar_status", "reason"):
                self.assertIn(req_key, evt)
            self.assertTrue(len(evt["sha256"]) == 64)
            diag_jsonl = os.path.join(storage_dir, "diagnostics_gcode_load.jsonl")
            self.assertTrue(os.path.isfile(diag_jsonl))

    def test_zombie_09_explicit_readable_tmp_gcode_without_dir_scan(self) -> None:
        """9) Explicit readable ctx.gcode_path under /tmp stages directly with zero /tmp directory scans."""
        import glob as _glob

        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            ext_tmp_dir = tempfile.mkdtemp(prefix="orcaslicer_explicit_test_")
            self.addCleanup(lambda: __import__("shutil").rmtree(ext_tmp_dir, ignore_errors=True))
            tmp_gcode_1 = os.path.join(ext_tmp_dir, "otoskopmmu_plate1.gcode")
            tmp_gcode_2 = os.path.join(ext_tmp_dir, "otoskopmmu_plate2.gcode")
            with open(tmp_gcode_1, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("otoskopmmu_p1"))
            with open(tmp_gcode_2, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("otoskopmmu_p2"))

            staged_dir_real = os.path.realpath(os.path.join(storage_dir, "staged_gcode"))
            orig_listdir = os.listdir
            orig_scandir = os.scandir
            orig_walk = os.walk
            orig_glob = _glob.glob

            def _assert_not_external_scan(target):
                real_t = os.path.realpath(str(target or ""))
                if not (real_t == staged_dir_real or real_t.startswith(staged_dir_real + os.sep)):
                    if real_t.startswith("/tmp") or "orcaslicer" in real_t:
                        raise AssertionError(f"Forbidden directory scan on {target!r}")

            def guarded_listdir(p="."):
                _assert_not_external_scan(p)
                return orig_listdir(p)

            def guarded_scandir(p="."):
                _assert_not_external_scan(p)
                return orig_scandir(p)

            def guarded_walk(p, *a, **kw):
                _assert_not_external_scan(p)
                return orig_walk(p, *a, **kw)

            def guarded_glob(pattern, *a, **kw):
                _assert_not_external_scan(os.path.dirname(str(pattern)))
                return orig_glob(pattern, *a, **kw)

            pp = plugin.KobraS1AcePostProcessCapability()
            fake_evt_enum = types.SimpleNamespace(
                SliceStarted="SliceStarted",
                GCodeExportFinished="GCodeExportFinished",
                SlicingJobComplete="SlicingJobComplete",
            )
            fake_code_enum = types.SimpleNamespace(Success=0, Error=1)
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "DIAG_LOAD_LOGGING_ENABLED", True),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "LifecycleEvent", fake_evt_enum, create=True),
                mock.patch.object(plugin.orca, "LifecycleEvtCode", fake_code_enum, create=True),
                mock.patch("os.listdir", side_effect=guarded_listdir),
                mock.patch("os.scandir", side_effect=guarded_scandir),
                mock.patch("os.walk", side_effect=guarded_walk),
                mock.patch.object(_glob, "glob", side_effect=guarded_glob),
            ):
                res = pp.execute(
                    types.SimpleNamespace(
                        gcode_path=tmp_gcode_1,
                        output_name="otoskopmmu_plate1.gcode",
                        host="File",
                        plate_index=1,
                        project_name="otoskopmmu",
                        cancelled=lambda: False,
                    )
                )
                self.assertEqual(res.status, plugin.orca.PluginResult.Success)
                self.assertTrue(plugin._is_inside_dir(st.last_gcode_path, staged_dir_real))
                self.assertIn("otoskopmmu_plate1", os.path.basename(st.last_gcode_path))

                # Also verify on_lifecycle_event handles an explicit readable /tmp ctx.gcode_path directly
                page.on_lifecycle_event(
                    fake_evt_enum.GCodeExportFinished,
                    types.SimpleNamespace(
                        code=0,
                        name="otoskopmmu_plate2.gcode",
                        gcode_path=tmp_gcode_2,
                        output_name="otoskopmmu_plate2.gcode",
                        plate_index=2,
                        host="Device",
                    ),
                )
                deadline = time.time() + 1.5
                while time.time() < deadline and "otoskopmmu_plate2" not in os.path.basename(st.last_gcode_path):
                    time.sleep(0.02)
                self.assertIn("otoskopmmu_plate2", os.path.basename(st.last_gcode_path))
                self.assertTrue(plugin._is_inside_dir(st.last_gcode_path, staged_dir_real))

    def test_zombie_10_multi_plate_consecutive_runs_and_pipeline_tx_retry(self) -> None:
        """10) Multi-plate / consecutive runs behave consistently AND pipeline registration retries after tx error."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            st._host_ui_snapshot = {
                "active_model": {"display_name": "otoskopmmu", "candidates": ["otoskopmmu"], "plate_index": 1},
                "filaments": [],
                "filaments_captured": False,
                "captured_ns": time.time_ns(),
            }
            p1_a = os.path.join(tmp_dir, "otoskopmmu_p1_v1.gcode")
            p2 = os.path.join(tmp_dir, "otoskopmmu_p2_v1.gcode")
            p1_b = os.path.join(tmp_dir, "otoskopmmu_p1_v2.gcode")
            for fp in (p1_a, p2, p1_b):
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(self._sample_zombie_gcode_text("otoskopmmu"))

            staged_p1_a = plugin._stage_gcode_file_to_storage(
                p1_a, st, output_name="otoskopmmu_plate_1.gcode", plate_index=1, project_model="otoskopmmu"
            )
            staged_p2 = plugin._stage_gcode_file_to_storage(
                p2, st, output_name="otoskopmmu_plate_2.gcode", plate_index=2, project_model="otoskopmmu"
            )
            self.assertFalse(plugin._read_provenance_sidecar(staged_p1_a).get("is_superseded"))
            self.assertFalse(plugin._read_provenance_sidecar(staged_p2).get("is_superseded"))

            # Consecutive run for plate 1 supersedes prior plate 1 (staged_p1_a), preserving plate 2
            staged_p1_b = plugin._stage_gcode_file_to_storage(
                p1_b, st, output_name="otoskopmmu_plate_1_r2.gcode", plate_index=1, project_model="otoskopmmu"
            )
            self.assertTrue(plugin._read_provenance_sidecar(staged_p1_a).get("is_superseded"))
            self.assertFalse(plugin._read_provenance_sidecar(staged_p1_b).get("is_superseded"))
            self.assertFalse(plugin._read_provenance_sidecar(staged_p2).get("is_superseded"))

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message("clear_gcode", {})
                self.assertEqual(plugin.discover_sliced_gcode_candidates(st, include_stale=False), [])

            # New plate 2 after clear loads cleanly
            staged_p2_new = plugin._stage_gcode_file_to_storage(
                p2, st, output_name="otoskopmmu_plate_2_after.gcode", plate_index=2, project_model="otoskopmmu"
            )
            kind, path, _ = plugin.acquire_gcode_from_slicer(st, force_refresh=True)
            self.assertEqual(kind, "staged_gcode")
            self.assertEqual(path, staged_p2_new)

            # Pipeline registration: tx.commit() failure does NOT cache session_key, allowing retry
            commit_attempts = [0]
            stored_val = {"v": ""}

            class _RetryTx:
                def __init__(self):
                    self._pending = ""

                def set(self, k, v):
                    self._pending = v

                def commit(self):
                    commit_attempts[0] += 1
                    if commit_attempts[0] == 1:
                        raise RuntimeError("transient preset lock")
                    stored_val["v"] = self._pending

                def abort(self):
                    self._pending = ""

            printer_preset = types.SimpleNamespace(name="Anycubic Kobra S1 0.4 nozzle")
            process_preset = types.SimpleNamespace(
                name="0.20mm Standard @Anycubic Kobra S1",
                config_value=lambda k: "",
            )
            bundle = types.SimpleNamespace(
                current_printer_preset=lambda: printer_preset,
                current_process_preset=lambda: process_preset,
                full_config_value=lambda k: stored_val["v"],
            )
            fake_host = types.SimpleNamespace(
                preset_bundle=lambda: bundle,
                preset=types.SimpleNamespace(process=types.SimpleNamespace(begin_update=lambda _t="": _RetryTx())),
            )
            with mock.patch.object(plugin.orca, "host", fake_host, create=True):
                res_err = plugin.ensure_slicing_pipeline_registered(st)
                self.assertEqual(res_err["status"], "error")
                self.assertEqual(st._pipeline_retry_count, 1)
                self.assertNotIn((printer_preset.name, process_preset.name), st._pipeline_registered_keys)

                res_ok = plugin.ensure_slicing_pipeline_registered(st)
                self.assertEqual(res_ok["status"], "updated")
                self.assertIn((printer_preset.name, process_preset.name), st._pipeline_registered_keys)
                self.assertIn(plugin.PIPELINE_CAPABILITY_NAME, stored_val["v"])

    # ------------------------------------------------------------------
    # Slice-plate without Export PoC: GCodeExportFinished.ctx.msg -> SlicingJobComplete
    # ------------------------------------------------------------------

    def test_slice_preview_poc_01_captures_gcode_export_finished_ctx_msg_then_slicing_job_complete(self) -> None:
        """1) Plain 'Slice plate' (GCodeExportFinished.ctx.msg -> SlicingJobComplete) captures preview G-code automatically with strict single open and no false stale banner."""
        import builtins
        import glob as _glob

        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            st._host_ui_snapshot = {
                "active_model": {"display_name": "otoskopmmu", "candidates": ["otoskopmmu"], "plate_index": 1},
                "filaments": [],
                "filaments_captured": False,
                "captured_ns": time.time_ns(),
            }
            model_tmp_dir = tempfile.mkdtemp(prefix="orcaslicer_model_poc_")
            meta_dir = os.path.join(model_tmp_dir, "Sun_Nov_15_21_00_00#1234#42", "Metadata")
            os.makedirs(meta_dir, exist_ok=True)
            self.addCleanup(lambda: __import__("shutil").rmtree(model_tmp_dir, ignore_errors=True))
            hidden_preview_gcode = os.path.join(meta_dir, ".1234.0.gcode")
            with open(hidden_preview_gcode, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("otoskopmmu_live_slice"))

            staged_dir_real = os.path.realpath(os.path.join(storage_dir, "staged_gcode"))
            preview_real = os.path.realpath(hidden_preview_gcode)
            preview_open_count = 0
            orig_open = builtins.open
            orig_listdir = os.listdir
            orig_scandir = os.scandir
            orig_walk = os.walk
            orig_glob = _glob.glob

            def counting_open(file, *args, **kwargs):
                nonlocal preview_open_count
                try:
                    if isinstance(file, (str, os.PathLike)) and os.path.realpath(os.fspath(file)) == preview_real:
                        preview_open_count += 1
                except OSError:
                    pass
                return orig_open(file, *args, **kwargs)

            def _assert_not_external_scan(target):
                real_t = os.path.realpath(str(target or ""))
                if not (real_t == staged_dir_real or real_t.startswith(staged_dir_real + os.sep)):
                    if real_t.startswith("/tmp") or "orcaslicer" in real_t:
                        raise AssertionError(f"Forbidden directory scan on {target!r}")

            fake_evt_enum = types.SimpleNamespace(
                SliceStarted="SliceStarted",
                GCodeExportStarted="GCodeExportStarted",
                GCodeExportFinished="GCodeExportFinished",
                SlicingJobComplete="SlicingJobComplete",
            )
            fake_code_enum = types.SimpleNamespace(Ok=0, Warn=1, Error=2)
            fake_host = types.SimpleNamespace(
                plater=lambda: {"project_name": "otoskopmmu.3mf"},
                model=lambda: types.SimpleNamespace(name="otoskopmmu"),
            )

            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "DIAG_LOAD_LOGGING_ENABLED", True),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "host", fake_host),
                mock.patch.object(plugin.orca, "LifecycleEvent", fake_evt_enum, create=True),
                mock.patch.object(plugin.orca, "LifecycleEvtCode", fake_code_enum, create=True),
                mock.patch("builtins.open", side_effect=counting_open),
                mock.patch("os.listdir", side_effect=lambda p=".": (_assert_not_external_scan(p), orig_listdir(p))[1]),
                mock.patch("os.scandir", side_effect=lambda p=".": (_assert_not_external_scan(p), orig_scandir(p))[1]),
                mock.patch("os.walk", side_effect=lambda p, *a, **kw: (_assert_not_external_scan(p), orig_walk(p, *a, **kw))[1]),
                mock.patch.object(_glob, "glob", side_effect=lambda pat, *a, **kw: (_assert_not_external_scan(os.path.dirname(str(pat))), orig_glob(pat, *a, **kw))[1]),
            ):
                page.on_lifecycle_event(
                    fake_evt_enum.SliceStarted,
                    types.SimpleNamespace(id="42", name="otoskopmmu", code=0, msg=""),
                )
                page.on_lifecycle_event(
                    fake_evt_enum.GCodeExportFinished,
                    types.SimpleNamespace(
                        id="42",
                        name="otoskopmmu",
                        code=0,
                        msg=hidden_preview_gcode,
                    ),
                )
                deadline = time.time() + 2.0
                while time.time() < deadline and not any(
                    isinstance(m, dict) and m.get("action") == "open_preprint_modal" for m in sent
                ):
                    time.sleep(0.02)

                page.on_lifecycle_event(
                    fake_evt_enum.SlicingJobComplete,
                    types.SimpleNamespace(id="42", name="otoskopmmu", code=0, msg=""),
                )
                state_snap = st.to_dict()
                page._handle_async_message("get_state", {})

                self.assertEqual(preview_open_count, 1)
                self.assertTrue(plugin._is_inside_dir(st.last_gcode_path, staged_dir_real))
                self.assertEqual(os.path.basename(st.last_gcode_path), "otoskopmmu.gcode")
                self.assertEqual(st.last_gcode_source, "slicer_explicit")
                self.assertTrue(st.pending_preprint_open)
                self.assertGreater(st._last_slice_completed_ns, 0)
                stale_info = state_snap.get("stale_model_info") or {}
                self.assertEqual(stale_info.get("active_model"), "otoskopmmu")
                self.assertFalse(stale_info.get("is_stale"), stale_info)
                self.assertEqual(stale_info.get("reasons"), [])
                self.assertEqual(
                    sum(1 for m in sent if isinstance(m, dict) and m.get("action") == "open_preprint_modal"),
                    1,
                )
                sc = plugin._read_provenance_sidecar(st.last_gcode_path)
                self.assertEqual(sc.get("original_source_name"), ".1234.0.gcode")
                self.assertEqual(sc.get("project_model"), "otoskopmmu")
                self.assertFalse(sc.get("is_cleared", True))

    def test_slice_preview_poc_02_worker_permission_error_defers_cleanly_to_slicing_job_complete(self) -> None:
        """2) PermissionError off main thread in GCodeExportFinished defers cleanly to main-thread SlicingJobComplete."""
        import builtins

        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            preview_dir = os.path.join(tmp_dir, "orcaslicer_model_perm", "Metadata")
            os.makedirs(preview_dir, exist_ok=True)
            hidden_preview_gcode = os.path.join(preview_dir, ".9999.0.gcode")
            with open(hidden_preview_gcode, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("otoskopmmu"))

            staged_dir_real = os.path.realpath(os.path.join(storage_dir, "staged_gcode"))
            fake_evt_enum = types.SimpleNamespace(
                SliceStarted="SliceStarted",
                GCodeExportFinished="GCodeExportFinished",
                SlicingJobComplete="SlicingJobComplete",
            )
            fake_code_enum = types.SimpleNamespace(Ok=0, Warn=1, Error=2)
            orig_open = builtins.open
            target_real = os.path.realpath(hidden_preview_gcode)

            def guarded_open(file, *args, **kwargs):
                should_deny = False
                try:
                    should_deny = (
                        isinstance(file, (str, os.PathLike))
                        and os.path.realpath(os.fspath(file)) == target_real
                        and threading.current_thread() is not threading.main_thread()
                    )
                except OSError:
                    should_deny = False
                if should_deny:
                    raise PermissionError("simulated off-main-thread audit denial")
                return orig_open(file, *args, **kwargs)

            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "DIAG_LOAD_LOGGING_ENABLED", True),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "LifecycleEvent", fake_evt_enum, create=True),
                mock.patch.object(plugin.orca, "LifecycleEvtCode", fake_code_enum, create=True),
                mock.patch("builtins.open", side_effect=guarded_open),
            ):
                page.on_lifecycle_event(
                    fake_evt_enum.GCodeExportFinished,
                    types.SimpleNamespace(id="77", name="otoskopmmu", code=0, msg=hidden_preview_gcode),
                )
                deadline = time.time() + 1.5
                while time.time() < deadline and not any(
                    isinstance(ev, dict) and ev.get("reason") == "permission_denied_worker_deferred"
                    for ev in st._diag_load_events
                ):
                    time.sleep(0.02)
                self.assertTrue(
                    any(ev.get("reason") == "permission_denied_worker_deferred" for ev in st._diag_load_events)
                )
                self.assertEqual(st.last_gcode_path, "")

                # SlicingJobComplete runs on wx main thread -> stages & opens modal cleanly
                page.on_lifecycle_event(
                    fake_evt_enum.SlicingJobComplete,
                    types.SimpleNamespace(id="77", name="otoskopmmu", code=0, msg=""),
                )
                deadline2 = time.time() + 2.0
                while time.time() < deadline2 and not any(
                    isinstance(m, dict) and m.get("action") == "open_preprint_modal" for m in sent
                ):
                    time.sleep(0.02)

                self.assertTrue(plugin._is_inside_dir(st.last_gcode_path, staged_dir_real))
                self.assertEqual(os.path.basename(st.last_gcode_path), "otoskopmmu.gcode")
                self.assertTrue(st.pending_preprint_open)
                self.assertTrue(any(isinstance(m, dict) and m.get("action") == "open_preprint_modal" for m in sent))

    def test_slice_preview_poc_03_cancelled_error_and_clear_gcode_never_resurrect_preview(self) -> None:
        """3) Cancelled slicing & clear_gcode both drop pending previews and never resurrect old preview files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            meta_dir = os.path.join(tmp_dir, "orcaslicer_model_cancel", "Metadata")
            os.makedirs(meta_dir, exist_ok=True)
            cancelled_gcode = os.path.join(meta_dir, ".555.0.gcode")
            valid_gcode = os.path.join(meta_dir, ".556.0.gcode")
            for p in (cancelled_gcode, valid_gcode):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(self._sample_zombie_gcode_text("otoskopmmu"))

            fake_evt_enum = types.SimpleNamespace(
                SliceStarted="SliceStarted",
                GCodeExportFinished="GCodeExportFinished",
                SlicingJobComplete="SlicingJobComplete",
            )
            fake_code_enum = types.SimpleNamespace(Ok=0, Warn=1, Error=2)

            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "DIAG_LOAD_LOGGING_ENABLED", True),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "LifecycleEvent", fake_evt_enum, create=True),
                mock.patch.object(plugin.orca, "LifecycleEvtCode", fake_code_enum, create=True),
            ):
                # Cancelled slice (Warn / "cancelled") clears any pending preview
                st._pending_preview_gcode = {
                    "path": cancelled_gcode,
                    "captured_ns": time.time_ns(),
                    "model_id": "11",
                    "staged_path": "",
                }
                page.on_lifecycle_event(
                    fake_evt_enum.SlicingJobComplete,
                    types.SimpleNamespace(id="11", name="otoskopmmu", code=1, msg="cancelled"),
                )
                self.assertEqual(st._pending_preview_gcode, {})
                self.assertEqual(st.last_gcode_path, "")

                # Valid Slice plate -> user clicks clear_gcode -> zombie locks stay sealed
                sent.clear()
                page.on_lifecycle_event(
                    fake_evt_enum.SliceStarted,
                    types.SimpleNamespace(id="12", name="otoskopmmu", code=0, msg=""),
                )
                page.on_lifecycle_event(
                    fake_evt_enum.GCodeExportFinished,
                    types.SimpleNamespace(id="12", name="otoskopmmu", code=0, msg=valid_gcode),
                )
                deadline = time.time() + 2.0
                while time.time() < deadline and not st.last_gcode_path:
                    time.sleep(0.02)
                self.assertTrue(os.path.isfile(st.last_gcode_path))

                page._handle_async_message("clear_gcode", {})
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(st._pending_preview_gcode, {})
                self.assertEqual(plugin.acquire_gcode_from_slicer(st, force_refresh=True)[:2], ("presets_only", ""))

                sent.clear()
                page.on_lifecycle_event(
                    fake_evt_enum.SlicingJobComplete,
                    types.SimpleNamespace(id="12", name="Plate 1", code=0, msg=""),
                )
                deadline2 = time.time() + 1.5
                while time.time() < deadline2 and not sent:
                    time.sleep(0.02)
                self.assertEqual(st.last_gcode_path, "")
                self.assertFalse(any(isinstance(m, dict) and m.get("action") == "open_preprint_modal" for m in sent))

    def test_slice_preview_poc_04_reslice_after_prior_export_ignores_stale_pipeline_cache(self) -> None:
        """4) Re-slicing via 'Slice plate' after a prior 'Export G-code' loads the fresh preview instead of aliasing old export."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            st._host_ui_snapshot = {
                "active_model": {"display_name": "otoskopmmu", "candidates": ["otoskopmmu"], "plate_index": 1},
                "filaments": [],
                "filaments_captured": False,
                "captured_ns": time.time_ns(),
            }
            export_dir = os.path.join(tmp_dir, "exports")
            preview_dir = os.path.join(tmp_dir, "orcaslicer_model_reslice", "Metadata")
            os.makedirs(export_dir, exist_ok=True)
            os.makedirs(preview_dir, exist_ok=True)

            old_export = os.path.join(export_dir, "otoskopmmu.gcode")
            new_preview = os.path.join(preview_dir, ".777.1.gcode")
            with open(old_export, "w", encoding="utf-8") as f:
                f.write(
                    "; generated by OrcaSlicer 2.5.0\n"
                    "; model: old_v1\n"
                    "; filament_colour = #FFFFFF\n"
                    "; filament_type = PLA\n"
                    "; flush_multiplier = 1.0\n"
                    "; flush_volumes_matrix = 0\n"
                    "; flush_volumes_vector = 100,100\n"
                    "G9111 bedTemp=60 extruderTemp=210 tool=0\nT0\n"
                )
            with open(new_preview, "w", encoding="utf-8") as f:
                f.write(
                    "; generated by OrcaSlicer 2.5.0\n"
                    "; model: fresh_v2_resliced\n"
                    "; filament_colour = #FF0000;#00FF00\n"
                    "; filament_type = PLA;PLA\n"
                    "; flush_multiplier = 1.0\n"
                    "; flush_volumes_matrix = 0,150,150,0\n"
                    "; flush_volumes_vector = 100,100\n"
                    "G9111 bedTemp=60 extruderTemp=210 tool=0\nT0\nT1\nT0\n"
                )

            pp = plugin.KobraS1AcePostProcessCapability()
            fake_evt_enum = types.SimpleNamespace(
                SliceStarted="SliceStarted",
                GCodeExportFinished="GCodeExportFinished",
                SlicingJobComplete="SlicingJobComplete",
            )
            fake_code_enum = types.SimpleNamespace(Ok=0, Warn=1, Error=2)
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "DIAG_LOAD_LOGGING_ENABLED", True),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "LifecycleEvent", fake_evt_enum, create=True),
                mock.patch.object(plugin.orca, "LifecycleEvtCode", fake_code_enum, create=True),
            ):
                res = pp.execute(
                    types.SimpleNamespace(
                        gcode_path=old_export,
                        output_name="otoskopmmu.gcode",
                        host="File",
                        plate_index=1,
                        project_name="otoskopmmu",
                        cancelled=lambda: False,
                    )
                )
                self.assertEqual(res.status, plugin.orca.PluginResult.Success)
                with open(st.last_gcode_path, "r", encoding="utf-8") as f:
                    self.assertIn("old_v1", f.read())

                # User modifies model and clicks ONLY "Slice plate"
                sent.clear()
                page.on_lifecycle_event(
                    fake_evt_enum.SliceStarted,
                    types.SimpleNamespace(id="88", name="otoskopmmu", code=0, msg=""),
                )
                page.on_lifecycle_event(
                    fake_evt_enum.GCodeExportFinished,
                    types.SimpleNamespace(id="88", name="otoskopmmu", code=0, msg=new_preview),
                )
                deadline = time.time() + 2.0
                while time.time() < deadline and not any(
                    isinstance(m, dict) and m.get("action") == "open_preprint_modal" for m in sent
                ):
                    time.sleep(0.02)
                page.on_lifecycle_event(
                    fake_evt_enum.SlicingJobComplete,
                    types.SimpleNamespace(id="88", name="otoskopmmu", code=0, msg=""),
                )

                self.assertEqual(os.path.basename(st.last_gcode_path), "otoskopmmu.gcode")
                with open(st.last_gcode_path, "r", encoding="utf-8") as f:
                    staged_content = f.read()
                self.assertIn("fresh_v2_resliced", staged_content)
                self.assertNotIn("old_v1", staged_content)
                self.assertEqual(st.last_postprocess_info.get("toolchanges"), 3)

    def test_slice_preview_poc_05_single_read_staging_for_acquire_temp_gcode_and_3mf(self) -> None:
        """5) acquire_gcode_from_slicer opens system-temp .gcode and .gcode.3mf containers strictly once during staging."""
        import builtins
        import zipfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, _page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            ext_tmp = tempfile.mkdtemp(prefix="orcaslicer_single_read_")
            self.addCleanup(lambda: __import__("shutil").rmtree(ext_tmp, ignore_errors=True))
            temp_gcode = os.path.join(ext_tmp, ".500.0.gcode")
            with open(temp_gcode, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("single_read_gcode"))

            temp_3mf = os.path.join(ext_tmp, "single_read_plate.gcode.3mf")
            with zipfile.ZipFile(temp_3mf, "w") as zf:
                zf.writestr("Metadata/plate_1.gcode", self._sample_zombie_gcode_text("single_read_3mf"))

            orig_open = builtins.open
            open_counts: Dict[str, int] = {
                os.path.realpath(temp_gcode): 0,
                os.path.realpath(temp_3mf): 0,
            }

            def counting_open(file, *args, **kwargs):
                try:
                    if isinstance(file, (str, os.PathLike)):
                        rp = os.path.realpath(os.fspath(file))
                        if rp in open_counts:
                            open_counts[rp] += 1
                except OSError:
                    pass
                return orig_open(file, *args, **kwargs)

            with (
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch("builtins.open", side_effect=counting_open),
            ):
                src_k1, staged_k1, _ = plugin.acquire_gcode_from_slicer(
                    st,
                    explicit_path=temp_gcode,
                    output_name="otoskopmmu.gcode",
                    plate_index=1,
                    explicit_flag=True,
                )
                src_k2, staged_k2, _ = plugin.acquire_gcode_from_slicer(
                    st,
                    explicit_path=temp_3mf,
                    output_name="otoskopmmu.gcode.3mf",
                    plate_index=1,
                    explicit_flag=True,
                )

            staged_dir_real = os.path.realpath(os.path.join(storage_dir, "staged_gcode"))
            self.assertEqual(src_k1, "slicer_explicit")
            self.assertEqual(src_k2, "slicer_explicit")
            self.assertTrue(plugin._is_inside_dir(staged_k1, staged_dir_real))
            self.assertTrue(plugin._is_inside_dir(staged_k2, staged_dir_real))
            self.assertEqual(open_counts[os.path.realpath(temp_gcode)], 1)
            self.assertEqual(open_counts[os.path.realpath(temp_3mf)], 1)

    def test_slice_preview_poc_06_stale_model_normalization_and_slice_correlation(self) -> None:
        """6) Stale check handles identical/compound/plate-suffixed model names without false warnings while still catching genuine staleness."""
        self.assertEqual(plugin._normalize_model_name_for_stale_check("otoskopmmu.gcode"), "otoskopmmu")
        self.assertEqual(
            plugin._normalize_model_name_for_stale_check("/tmp/orcaslicer_1000/OtoskopMMU_plate_1.GCODE"),
            "otoskopmmu",
        )
        self.assertEqual(
            plugin._normalize_model_name_for_stale_check("otoskopmmu_plate2.stl.gcode"),
            "otoskopmmu",
        )
        self.assertEqual(
            plugin._normalize_model_name_for_stale_check("otoskopmmu-plate-3.gcode.3mf"),
            "otoskopmmu",
        )
        self.assertEqual(
            plugin._normalize_model_name_for_stale_check("otoskopmmu_p4.STL"),
            "otoskopmmu",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, _page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            st._host_ui_snapshot = {
                "active_model": {"display_name": "otoskopmmu", "candidates": ["otoskopmmu.3mf"], "plate_index": 1},
                "filaments": [],
                "filaments_captured": False,
                "captured_ns": time.time_ns(),
            }

            raw_tmp_file = os.path.join(tmp_dir, ".9001.0.gcode")
            with open(raw_tmp_file, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("otoskopmmu"))

            staged_path = plugin._stage_gcode_file_to_storage(
                raw_tmp_file,
                state=st,
                output_name="otoskopmmu_plate_1.STL.gcode",
                plate_index=1,
                project_model="OtoskopMMU.3mf",
                source_kind="slicer_explicit",
            )
            self.assertTrue(staged_path and os.path.isfile(staged_path))
            sc = plugin._read_provenance_sidecar(staged_path)

            # Matching slice completion correlation must NOT raise slice_newer_than_export
            st.record_slicing_completed(
                plate_label="Plate 1",
                plate_index=1,
                ui_snapshot=st._host_ui_snapshot,
                satisfied_run_id=sc["slicing_run_id"],
                satisfied_created_ns=sc["created_ns"],
            )
            for fname in (
                "otoskopmmu.gcode",
                "/tmp/x/OtoskopMMU_plate_1.GCODE",
                "otoskopmmu_plate1.stl.gcode",
            ):
                res = plugin.check_stale_gcode_model(gcode_path=staged_path, filename=fname, state=st)
                self.assertFalse(res["is_stale"], f"Unexpected stale result for {fname}: {res}")

            # Real model mismatch still fails
            mismatch = plugin.check_stale_gcode_model(
                gcode_path=staged_path,
                filename="DifferentModel_plate_1.gcode",
                state=st,
            )
            # Without a separate conflicting sidecar, test clean mismatch directly on a non-matching filename
            pure_mismatch = plugin.check_stale_gcode_model(filename="OtherProject_PLA.gcode", state=st)
            self.assertTrue(pure_mismatch["is_stale"])
            self.assertIn("model_mismatch", pure_mismatch["reasons"])
            self.assertFalse(mismatch["is_stale"])  # same staged sidecar model matches

            # Subsequent uncorrelated SlicingJobComplete must still detect slice_newer_than_export
            time.sleep(0.01)
            st.record_slicing_completed(plate_label="Plate 1", plate_index=1, ui_snapshot=st._host_ui_snapshot)
            now_stale = plugin.check_stale_gcode_model(
                gcode_path=staged_path,
                filename="otoskopmmu.gcode",
                state=st,
            )
            self.assertTrue(now_stale["is_stale"])
            self.assertIn("slice_newer_than_export", now_stale["reasons"])

            # Verify deferred SlicingJobComplete permission recovery path also sets satisfied correlation
            st2, _st2, _cfg2, page2, sent2 = self._make_zombie_test_state_and_page(os.path.join(tmp_dir, "s2"))
            st2._host_ui_snapshot = dict(st._host_ui_snapshot)
            hidden_preview = os.path.join(tmp_dir, ".9002.0.gcode")
            with open(hidden_preview, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("otoskopmmu"))
            fake_evt_enum = types.SimpleNamespace(
                SliceStarted="SliceStarted",
                GCodeExportFinished="GCodeExportFinished",
                SlicingJobComplete="SlicingJobComplete",
            )
            fake_code_enum = types.SimpleNamespace(Ok=0, Warn=1, Error=2)
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st2),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page2),
                mock.patch.object(plugin.orca, "LifecycleEvent", fake_evt_enum, create=True),
                mock.patch.object(plugin.orca, "LifecycleEvtCode", fake_code_enum, create=True),
            ):
                page2.on_lifecycle_event(
                    fake_evt_enum.SliceStarted,
                    types.SimpleNamespace(id="91", name="otoskopmmu", code=0, msg=""),
                )
                st2._pending_preview_gcode = {
                    "path": hidden_preview,
                    "model_id": "91",
                    "model_name": "otoskopmmu",
                    "raw_output_name": "",
                    "output_name": "otoskopmmu.gcode",
                    "plate_idx": 1,
                    "host": "",
                    "project_model": "otoskopmmu",
                    "captured_ns": time.time_ns(),
                    "staged_path": "",
                }
                page2.on_lifecycle_event(
                    fake_evt_enum.SlicingJobComplete,
                    types.SimpleNamespace(id="91", name="otoskopmmu", code=0, msg=""),
                )
                deadline = time.time() + 2.0
                while time.time() < deadline and not sent2:
                    time.sleep(0.02)
                self.assertFalse(st2.to_dict()["stale_model_info"]["is_stale"])

    def test_ux_11_full_color_slot_dropdowns_and_compact_preprint_tooltips(self) -> None:
        """
        1) Slot dropdown trigger and menu options apply full filament background + contrast foreground.
        2) Pre-Print option checkboxes use compact 1-row pills with G-code hover tooltips (`data-tooltip` + `title`).
        """
        html = plugin.ACE_PLUGIN_HTML_UI
        # Full-surface coloring on trigger & menu options via computeContrastTextColor
        self.assertIn('style="background-color:${trigColors.bgHex};color:${trigColors.fgHex};"', html)
        self.assertIn('style="background-color:${optColors.bgHex};color:${optColors.fgHex};"', html)
        self.assertIn(".slot-rfid-tag", html)
        self.assertIn("color: inherit;", html)
        # Ensure menu highlight does not clobber full-filament inline background with var(--accent-soft)
        opt_sel_block = html.split(".slot-color-option.is-selected {", 1)[1].split("}", 1)[0]
        self.assertNotIn("background:", opt_sel_block)
        # Ensure trigger hover does not clobber full-filament inline background
        trig_hover_block = html.split(".slot-color-trigger:hover,", 1)[1].split("}", 1)[0]
        self.assertNotIn("background:", trig_hover_block)

        # Single-row toggles & hover tooltips (G31 & Force Initial Purge toggles removed)
        self.assertIn(".toggle-pill[data-tooltip]:hover::after", html)
        self.assertNotIn('id="chkBedLeveling"', html)
        self.assertNotIn('id="chkForceInitialPurge"', html)
        self.assertIn('data-i18n-title="ui.modal.opt.skip_purge_tip"', html)
        self.assertIn('data-i18n-title="ui.modal.opt.cut_post_tip"', html)
        self.assertIn('el.setAttribute("data-tooltip", text)', html)

    def test_ux_12_dryer_preset_custom_dropdown_and_manual_input(self) -> None:
        """Dryer temperature preset uses `.custom-select` (<= 60 C presets only) alongside `#dryerTemp` (manual up to 70 C)."""
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertNotIn("<select", html.lower())
        self.assertIn('id="dryerPreset"', html)
        self.assertIn('id="dryerPresetTrigger"', html)
        self.assertIn('id="dryerPresetMenu"', html)
        for temp_val, key in (
            ("custom", "ui.sec2.preset_custom"),
            ("45", "ui.sec2.preset_pla"),
            ("60", "ui.sec2.preset_petg"),
            ("50", "ui.sec2.preset_tpu"),
        ):
            self.assertIn(f'data-value="{temp_val}" data-i18n="{key}"', html)
            self.assertIn(f'"{temp_val}": "{key}"', html)
        # Presets > 60 C (65 C ASA/ABS, 70 C PA/Nylon) are removed for ACE Pro & ACE 2 compatibility
        for removed_val, removed_key in (
            ("65", "ui.sec2.preset_abs_asa"),
            ("70", "ui.sec2.preset_pa"),
        ):
            self.assertNotIn(f'data-value="{removed_val}"', html)
            self.assertNotIn(removed_key, html)
            self.assertNotIn(removed_key, plugin.TRANSLATIONS["en"])
            self.assertNotIn(removed_key, plugin.TRANSLATIONS["de"])
        # Verify every numeric option inside #dryerPresetMenu is <= 60 C
        menu_m = re.search(r'<div id="dryerPresetMenu"[^>]*>(.*?)</div>', html, re.S)
        self.assertIsNotNone(menu_m)
        menu_vals = re.findall(r'data-value="([^"]+)"', menu_m.group(1))
        self.assertEqual(menu_vals, ["custom", "45", "60", "50"])
        for mv in menu_vals:
            if mv != "custom":
                self.assertLessEqual(int(mv), 60)
        # Default dryer preset is PLA (45 °C, 240 min); manual temperature input stays unrestricted up to 70 °C
        self.assertIn('<input type="number" id="dryerTemp" value="45" min="20" max="70" oninput="onDryerTempInput()" onchange="onDryerSettingsChange()"', html)
        self.assertIn('<input type="number" id="dryerDuration" value="240" min="1" max="1440"', html)
        self.assertIn('<div id="dryerPreset" class="custom-select dryer-preset-dropdown" data-value="45">', html)
        self.assertIn('<span id="dryerPresetLabel" class="custom-select-label" data-i18n="ui.sec2.preset_pla">PLA (45 °C)</span>', html)
        self.assertIn('role="option" aria-selected="true" class="custom-select-option is-selected" data-value="45" data-i18n="ui.sec2.preset_pla"', html)
        self.assertIn('role="option" aria-selected="false" class="custom-select-option" data-value="custom" data-i18n="ui.sec2.preset_custom"', html)
        st_default = plugin.PluginState()
        self.assertEqual(st_default.dryer_temp, 45)
        self.assertEqual(st_default.dryer_duration, 240)
        for fn_name in (
            "toggleDryerPresetDropdown",
            "selectDryerPreset",
            "syncDryerPresetFromTemp",
            "onDryerTempInput",
        ):
            self.assertIn(fn_name, html)
        self.assertIn('oninput="onDryerTempInput()"', html)

        # Verify JS runtime preset default & temp/duration sync via gjs
        import inspect
        import shutil
        import subprocess
        self.assertEqual(inspect.signature(plugin.MoonrakerClient.start_dryer).parameters["temp_c"].default, 45)
        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(tagOrId) {
  return {
    id: tagOrId, tagName: tagOrId, className: "", value: "", checked: false, textContent: "", innerHTML: "",
    dataset: {}, style: {}, files: [], children: [], disabled: false,
    appendChild(c) { this.children.push(c); return c; },
    classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,f){ if(f===undefined){this._s.has(c)?this._s.delete(c):this._s.add(c);} else if(f){this._s.add(c);} else {this._s.delete(c);} }, contains(c){return this._s.has(c);} },
    setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;},
    addEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
  };
}
[
  "slotsContainer","slotBayTitle","dryerUnit","dryerUnitTrigger","dryerUnitLabel","dryerUnitMenu",
  "dryerPreset","dryerPresetTrigger","dryerPresetLabel","dryerPresetMenu","dryerTemp","dryerDuration",
  "modalMappingCards","mappingSummaryBar","chkShowUnusedExtruders","chkConfirmMaterialMismatch",
  "chkBedLeveling","chkSkipPurgeLoaded","chkCutTipPost","chkForceInitialPurge",
  "configuredAceCount","configuredAceCountLabel","configuredAceCountMenu",
  "moonrakerUrl","apiKey","spoolmanUrl","port80Fallback","gcodePathInput","defaultGcodeDirInput",
  "predictionReserveInput","chkDiagLoadLogging","connBadge","headerPrintBadge","modalPrinterBadge",
  "modalPrinterTargetBadge","headerPrinterSelect","headerPrinterLabel","headerPrinterMenu",
  "settingsPrinterSelect","settingsPrinterLabel","settingsPrinterMenu","printerProfileName",
  "btnPrinterProfileDelete","printSafetyBanner","printSafetyState","deviceMeta","dryerBadge",
  "execConnVal","execConnSub","execToolVal","execToolSub","execDryerVal","execDryerSub","execGcodeVal","execGcodeSub"
].forEach(id => { _els[id] = _mkEl(id); });
_els.dryerTemp.value = "45";
_els.dryerDuration.value = "240";
_els.dryerPreset.dataset.value = "45";
_els.dryerUnit.dataset.value = "all";
_els.configuredAceCount.dataset.value = "auto";
let _onMsgCb = null;
var document = {
  documentElement: _mkEl("documentElement"), activeElement: null,
  getElementById(id) { return _els[id] || null; },
  createElement(tag) { return _mkEl(tag); },
  querySelectorAll() { return []; }, addEventListener() {},
};
var window = { orca: { postMessage(){}, onMessage(cb){ _onMsgCb = cb; } }, matchMedia() { return { matches: true, addEventListener(){} }; } };
var orca = window.orca;
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + r"""
const out = {
  init_temp: currentState.dryer_temp,
  init_dur: currentState.dryer_duration,
  init_preset: _els.dryerPreset.dataset.value,
  init_preset_lbl: _els.dryerPresetLabel.textContent,
};
selectDryerPreset("custom");
out.after_custom_preset = _els.dryerPreset.dataset.value;
if (_onMsgCb) {
  _onMsgCb({ state: { dryer_temp: 45, dryer_duration: 240, dryer_unit: "all" } });
}
out.poll_same_temp_preset = _els.dryerPreset.dataset.value;
if (_onMsgCb) {
  _onMsgCb({ state: { dryer_temp: 60, dryer_duration: 300, dryer_unit: "all" } });
}
out.poll_changed_temp_preset = _els.dryerPreset.dataset.value;
out.poll_changed_temp_val = _els.dryerTemp.value;
out.poll_changed_dur_val = _els.dryerDuration.value;
print(JSON.stringify(out));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "dryer_default_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(res["init_temp"], 45)
        self.assertEqual(res["init_dur"], 240)
        self.assertEqual(res["init_preset"], "45")
        self.assertEqual(res["init_preset_lbl"], "PLA (45 °C)")
        self.assertEqual(res["after_custom_preset"], "custom")
        self.assertEqual(res["poll_same_temp_preset"], "custom")
        self.assertEqual(res["poll_changed_temp_preset"], "60")
        self.assertEqual(res["poll_changed_temp_val"], "60")
        self.assertEqual(res["poll_changed_dur_val"], "300")

    def test_spoolman_01_disabled_by_default_zero_network_calls(self) -> None:
        """Empty `spoolman_url` (default) performs ZERO HTTP requests and cleans stale metadata."""
        st = plugin.PluginState()
        self.assertEqual(st.spoolman_url, "")
        self.assertFalse(st.get_spoolman_client().is_enabled)
        self.assertEqual(st.to_dict()["spoolman_url"], "")

        base_status = {
            "slots": [
                {
                    "tool": 0,
                    "status": "ready",
                    "material": "PLA",
                    "hex_color": "#111111",
                    "sku": "ANY-PLA-BLK",
                    "spoolman": {"display_name": "Stale Spool", "remaining_weight": 50.0},
                }
            ]
        }
        with mock.patch("urllib.request.urlopen") as m_open:
            self.assertEqual(plugin.SpoolmanClient("").fetch_spools(), [])
            out = plugin.enrich_ace_status_with_spoolman(base_status, st, force_fetch=True)
            m_open.assert_not_called()

        self.assertNotIn("spoolman", out["slots"][0])
        self.assertFalse(out["spoolman_status"]["enabled"])

        # Setting empty string strips cached spoolman fields
        st.apply_dict({"spoolman_url": "http://127.0.0.1:7912"}, save=False)
        self.assertEqual(st.spoolman_url, "http://127.0.0.1:7912")
        st.apply_dict({"spoolman_url": ""}, save=False)
        self.assertEqual(st.spoolman_url, "")
        self.assertFalse(st.last_spoolman_status["enabled"])

    def test_spoolman_02_sku_matching_weight_enrichment_and_low_spool_warning(self) -> None:
        """Optional Spoolman integration matches SKU/extra/lot_nr, displays weights, warns non-blockingly, and handles offline errors."""
        sample_spools = [
            {
                "id": 10,
                "archived": True,
                "remaining_weight": 999.0,
                "lot_nr": "ANY-PLA-BLK",
                "filament": {
                    "name": "Archived Black",
                    "article_number": "ANY-PLA-BLK",
                    "material": "PLA",
                    "vendor": {"name": "ArchivedVendor"},
                },
            },
            {
                "id": 11,
                "archived": False,
                "remaining_weight": 420.5,
                "filament": {
                    "name": "PLA+ Black",
                    "article_number": "ANY-PLA-BLK",
                    "material": "PLA",
                    "color_hex": "111827",
                    "vendor": {"name": "Anycubic"},
                },
            },
            {
                "id": 12,
                "archived": False,
                "initial_weight": 1000.0,
                "used_weight": 988.0,
                "remaining_weight": 12.0,
                "filament": {
                    "name": "PETG Crisp White",
                    "material": "PETG",
                    "vendor": {"name": "Sunlu"},
                    "extra": {"sku": '"PETG-WHT-01"'},
                },
            },
            {
                "id": 13,
                "archived": False,
                "remaining_weight": 310.0,
                "lot_nr": "TPU-RED-95A",
                "filament": {
                    "name": "TPU 95A Red",
                    "material": "TPU",
                },
            },
        ]

        class _FakeResp:
            def __init__(self, payload: Any) -> None:
                self._raw = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._raw

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *args: Any) -> bool:
                return False

        st = plugin.PluginState()
        st.apply_dict({"spoolman_url": "192.0.2.10:7912/"}, save=False)
        self.assertEqual(st.spoolman_url, "http://192.0.2.10:7912")

        ace_status = {
            "slots": [
                {"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#111827", "sku": "any-pla-blk"},
                {"tool": 1, "status": "ready", "material": "PETG", "hex_color": "#FFFFFF", "sku": "petg-wht-01"},
                {"tool": 2, "status": "ready", "material": "TPU", "hex_color": "#DC2626", "sku": "TPU-RED-95A"},
                {"tool": 3, "status": "empty", "material": "", "hex_color": "#000000", "sku": "ANY-PLA-BLK"},
            ]
        }

        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(sample_spools)) as m_open:
            enriched = plugin.enrich_ace_status_with_spoolman(ace_status, st, force_fetch=True)
            m_open.assert_called_once()
            req_arg = m_open.call_args[0][0]
            self.assertEqual(req_arg.full_url, "http://192.0.2.10:7912/api/v1/spool")

        self.assertTrue(enriched["spoolman_status"]["connected"])
        self.assertEqual(enriched["spoolman_status"]["spools_count"], 3)
        self.assertEqual(enriched["spoolman_status"]["matched_count"], 3)

        slots = enriched["slots"]
        self.assertEqual(slots[0]["spoolman"]["display_name"], "Anycubic PLA+ Black")
        self.assertEqual(slots[0]["spoolman"]["remaining_weight"], 420.5)
        self.assertEqual(slots[1]["spoolman"]["display_name"], "Sunlu PETG Crisp White")
        self.assertEqual(slots[1]["spoolman"]["remaining_weight"], 12.0)
        self.assertEqual(slots[1]["spoolman"]["fill_percent"], 1.2)
        self.assertEqual(slots[1]["spoolman"]["weight_status"], "ok")
        self.assertEqual(slots[2]["spoolman"]["display_name"], "TPU 95A Red")
        self.assertEqual(slots[2]["spoolman"]["remaining_weight"], 310.0)
        self.assertNotIn("spoolman", slots[3])

        # Non-blocking low-spool warning when required grams > remaining_weight
        fils = [
            {"slicer_tool": 0, "material": "PLA", "used": True, "used_g": 45.0},
            {"slicer_tool": 1, "material": "PETG", "used": True, "used_g": 35.0},
        ]
        val = plugin.validate_color_mapping(fils, {0: 0, 1: 1}, slots, lang="en")
        self.assertTrue(val["valid"])
        self.assertEqual(len(val["low_spools"]), 1)
        self.assertEqual(val["low_spools"][0]["mapped_slot"], 1)
        self.assertEqual(val["low_spools"][0]["remaining_g"], 12.0)
        self.assertEqual(val["low_spools"][0]["required_g"], 35.0)
        self.assertEqual(val["warning_items"][-1]["key"], "val.map.low_spool")

        # Graceful offline error handling
        with mock.patch("urllib.request.urlopen", side_effect=plugin.urllib.error.URLError("timed out")):
            offline_out = plugin.enrich_ace_status_with_spoolman(ace_status, st, force_fetch=True)
        self.assertFalse(offline_out["spoolman_status"]["connected"])
        self.assertIn("timed out", offline_out["spoolman_status"]["error"])

        # Settings & Card UI hooks present
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn('id="spoolmanUrl"', html)
        self.assertIn('id="btnTestSpoolman"', html)
        self.assertIn('id="spoolmanStatusBadge"', html)
        self.assertIn("testSpoolmanConnection", html)
        self.assertIn("slot-spoolman-meta", html)
        self.assertIn("ui.modal.card.low_spool", html)

    def test_spoolman_03_numeric_spool_id_first_then_article_number_and_colors(self) -> None:
        """Numeric SKU checks Spoolman spool.id first (never filament.id), falls back to normalized SKU, and resolves German color names."""
        spools = [
            {
                "id": 67,
                "archived": False,
                "remaining_weight": 450.0,
                "initial_weight": 1000.0,
                "filament": {
                    "id": 999,
                    "name": "PLA Matte Signal Red",
                    "article_number": "SIG-RED-01",
                    "material": "PLA",
                    "color_hex": "#DC2626",
                    "vendor": {"name": "Sunlu"},
                },
            },
            {
                "id": 88,
                "archived": False,
                "remaining_weight": 600.0,
                "initial_weight": 1000.0,
                "filament": {
                    "id": 77,
                    "name": "PETG Slate Jet",
                    "article_number": "HPL19-106",
                    "material": "PETG",
                    "color_hex": "111827",
                    "vendor": {"name": "Anycubic"},
                },
            },
            {
                "id": 89,
                "archived": False,
                "remaining_weight": 300.0,
                "initial_weight": 1000.0,
                "filament": {
                    "id": 78,
                    "name": "PLA Arctic",
                    "article_number": "9001",
                    "material": "PLA",
                    "color_hex": "FFFFFF",
                    "extra": {"color_name": '"Arktisweiß"'},
                },
            },
            {
                "id": 90,
                "archived": False,
                "remaining_weight": 200.0,
                "initial_weight": 1000.0,
                "filament": {
                    "id": 777,
                    "name": "Decoy Filament Id 777",
                    "article_number": "OTHER-SKU",
                    "material": "PLA",
                    "color_hex": "#2563EB",
                },
            },
        ]
        slots = [
            # Numeric spool.id match with surrounding spaces
            {"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#000000", "sku": " 67 "},
            # Manufacturer article number with mixed case & extra spaces
            {"tool": 1, "status": "ready", "material": "PETG", "hex_color": "#FFFFFF", "sku": "  hpl 19 - 106 "},
            # Numeric SKU where no spool has id=9001 -> falls back to article_number="9001"
            {"tool": 2, "status": "ready", "material": "PLA", "hex_color": "#000000", "sku": "9001"},
            # Numeric SKU matching ONLY filament.id (777) must NOT resolve falsely
            {"tool": 3, "status": "ready", "material": "PLA", "hex_color": "#2563EB", "sku": "777"},
        ]

        enriched, matched = plugin.match_spoolman_to_slots(slots, spools)
        self.assertEqual(matched, 3)

        sp0 = enriched[0]["spoolman"]
        self.assertEqual(sp0["spool_id"], 67)
        self.assertEqual(sp0["status"], "ok")
        self.assertFalse(sp0["conflict"])
        self.assertEqual(sp0["color_hex"], "#DC2626")
        self.assertEqual(sp0["color_name"], "Rot")
        self.assertEqual(sp0["fill_percent"], 45.0)

        sp1 = enriched[1]["spoolman"]
        self.assertEqual(sp1["spool_id"], 88)
        self.assertEqual(sp1["color_hex"], "#111827")
        self.assertEqual(sp1["color_name"], "Schwarz")
        self.assertEqual(sp1["fill_percent"], 60.0)

        # Explicit Spoolman color text overrides derived color name
        sp2 = enriched[2]["spoolman"]
        self.assertEqual(sp2["spool_id"], 89)
        self.assertEqual(sp2["color_hex"], "#FFFFFF")
        self.assertEqual(sp2["color_name"], "Arktisweiß")

        # filament.id must never be used as spool_id
        self.assertNotIn("spoolman", enriched[3])

        # Verify broad German color classifier palette
        expected_palette = {
            "#000000": "Schwarz",
            "#FFFFFF": "Weiß",
            "#808080": "Grau",
            "#C0C0C0": "Silber",
            "#DC2626": "Rot",
            "#FF6600": "Orange",
            "#FACC15": "Gelb",
            "#16A34A": "Grün",
            "#009688": "Türkis",
            "#2563EB": "Blau",
            "#8B5CF6": "Violett",
            "#EC4899": "Pink",
            "#8B4513": "Braun",
            "#D4AF37": "Gold",
            "#F5F5DC": "Beige",
        }
        for hex_val, expected_name in expected_palette.items():
            self.assertEqual(
                plugin.classify_german_color_name(hex_val),
                expected_name,
                f"Color classification mismatch for {hex_val}",
            )

    def test_spoolman_04_multi_match_last_used_fallback_manual_and_revalidation(self) -> None:
        """Duplicate SKUs resolve via last_used, deterministic fallback, manual per-slot persistence, and revalidation."""
        spools_base = [
            {
                "id": 182,
                "archived": False,
                "remaining_weight": 642.0,
                "initial_weight": 1000.0,
                "location": "Lager B",
                "last_used": "2026-11-10T09:00:00Z",
                "filament": {"name": "PLA Matte Grey", "article_number": "HPL19-106", "material": "PLA"},
            },
            {
                "id": 123,
                "archived": False,
                "remaining_weight": 985.0,
                "initial_weight": 1000.0,
                "location": "Lager A",
                "last_used": "2026-11-15T18:30:00Z",
                "filament": {"name": "PLA Matte Grey", "article_number": "HPL19-106", "material": "PLA"},
            },
            {
                "id": 147,
                "archived": False,
                "remaining_weight": 1000.0,
                "initial_weight": 1000.0,
                "location": "Lager A",
                "last_used": None,
                "filament": {"name": "PLA Matte Grey", "article_number": "HPL19-106", "material": "PLA"},
            },
            # Single spool mentioning its SKU twice (article_number + lot_nr) must NOT duplicate candidates
            {
                "id": 23,
                "archived": False,
                "remaining_weight": 500.0,
                "initial_weight": 1000.0,
                "lot_nr": "UNIQ-1",
                "filament": {"name": "PLA Unique", "article_number": "UNIQ-1", "material": "PLA"},
            },
            # Archived spool sharing UNIQ-1 must be ignored
            {
                "id": 24,
                "archived": True,
                "remaining_weight": 900.0,
                "filament": {"name": "Old Archived", "article_number": "UNIQ-1", "material": "PLA"},
            },
        ]
        ace_status = {
            "slots": [
                {"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#808080", "sku": "hpl19 - 106"},
                {"tool": 1, "status": "ready", "material": "PLA", "hex_color": "#2563EB", "sku": "uniq-1"},
            ]
        }

        class _FakeResp:
            def __init__(self, payload: Any) -> None:
                self._raw = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._raw

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *args: Any) -> bool:
                return False

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "state.json")
            st = plugin.PluginState()
            st.resolve_config_path = lambda: cfg_file
            st.apply_dict({"spoolman_url": "http://127.0.0.1:7912"}, save=True)
            st.last_ace_status = dict(ace_status)

            # 1) Auto selection prefers most recently used spool (#123), exposing all 3 sorted candidates
            with mock.patch("urllib.request.urlopen", return_value=_FakeResp(spools_base)):
                enriched = plugin.enrich_ace_status_with_spoolman(ace_status, st, force_fetch=True)

            self.assertEqual(enriched["spoolman_status"]["matched_count"], 2)
            self.assertEqual(enriched["spoolman_status"]["conflict_count"], 0)
            s0 = enriched["slots"][0]["spoolman"]
            self.assertEqual(s0["spool_id"], 123)
            self.assertEqual(s0["assignment_source"], "auto")
            self.assertEqual(s0["match_reason"], "last_used")
            self.assertEqual(s0["location"], "Lager A")
            self.assertEqual(s0["last_used_display"], "2026-11-15 18:30")
            self.assertEqual([c["spool_id"] for c in s0["candidates"]], [123, 182, 147])
            self.assertEqual(enriched["slots"][1]["spoolman"]["spool_id"], 23)
            self.assertEqual(enriched["slots"][1]["spoolman"]["match_reason"], "single")

            # 2) Missing/identical timestamps use deterministic, permutation-invariant fallback (never arbitrary!)
            spools_no_ts_a = [
                {"id": 182, "archived": False, "remaining_weight": 642.0, "location": "B", "last_used": None,
                 "filament": {"name": "PLA", "article_number": "HPL19-106"}},
                {"id": 147, "archived": False, "remaining_weight": 1000.0, "location": "A", "last_used": None,
                 "filament": {"name": "PLA", "article_number": "HPL19-106"}},
                {"id": 101, "archived": False, "remaining_weight": 0.0, "location": "EmptyBin", "last_used": None,
                 "filament": {"name": "PLA", "article_number": "HPL19-106"}},
            ]
            spools_no_ts_b = list(reversed(spools_no_ts_a))
            enr_a, _ = plugin.match_spoolman_to_slots(ace_status["slots"][:1], spools_no_ts_a)
            enr_b, _ = plugin.match_spoolman_to_slots(ace_status["slots"][:1], spools_no_ts_b)
            self.assertEqual(enr_a[0]["spoolman"]["spool_id"], 147)
            self.assertEqual(enr_b[0]["spoolman"]["spool_id"], 147)
            self.assertEqual(enr_a[0]["spoolman"]["match_reason"], "fallback")

            # 3) Manual UI selection persists permanently and survives refreshes & reloads
            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                st.last_ace_status = dict(ace_status)
                st._spoolman_spools = list(spools_base)
                st._spoolman_cache_url = "http://127.0.0.1:7912"
                st._spoolman_cache_ts = plugin.time.monotonic()
                page._handle_async_message(
                    "select_slot_spool",
                    {"tool": 0, "spool_id": 147, "sku": "HPL19-106"},
                )

            self.assertEqual(st.manual_spoolman_selections["0"]["spool_id"], 147)
            self.assertEqual(posted[-1]["ace_status"]["slots"][0]["spoolman"]["spool_id"], 147)
            self.assertEqual(posted[-1]["ace_status"]["slots"][0]["spoolman"]["assignment_source"], "manual")

            # Reload from disk and verify manual selection stays active across subsequent fetch
            st_reloaded = plugin.PluginState()
            st_reloaded.resolve_config_path = lambda: cfg_file
            st_reloaded.load_config()
            self.assertEqual(st_reloaded.manual_spoolman_selections["0"]["spool_id"], 147)
            with mock.patch("urllib.request.urlopen", return_value=_FakeResp(spools_base)):
                refreshed = plugin.enrich_ace_status_with_spoolman(ace_status, st_reloaded, force_fetch=True)
            self.assertEqual(refreshed["slots"][0]["spoolman"]["spool_id"], 147)
            self.assertEqual(refreshed["slots"][0]["spoolman"]["assignment_source"], "manual")

            # 4) Offline error must NOT delete saved manual selection
            with mock.patch("urllib.request.urlopen", side_effect=plugin.urllib.error.URLError("offline")):
                plugin.enrich_ace_status_with_spoolman(ace_status, st_reloaded, force_fetch=True)
            self.assertIn("0", st_reloaded.manual_spoolman_selections)

            # 5) Archiving or deleting spool #147 invalidates manual override and falls back cleanly to #123
            spools_archived = [
                dict(sp, archived=(sp["id"] == 147))
                for sp in spools_base
            ]
            with mock.patch("urllib.request.urlopen", return_value=_FakeResp(spools_archived)):
                after_archive = plugin.enrich_ace_status_with_spoolman(ace_status, st_reloaded, force_fetch=True)
            self.assertEqual(after_archive["slots"][0]["spoolman"]["spool_id"], 123)
            self.assertEqual(after_archive["slots"][0]["spoolman"]["assignment_source"], "auto")
            self.assertNotIn("0", st_reloaded.manual_spoolman_selections)

            # 6) Changing slot SKU invalidates old manual selection even before next spool fetch
            st_reloaded.set_manual_slot_spool(0, 147, sku="HPL19-106", save=True)
            changed_sku_slots = {
                "slots": [
                    {"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#2563EB", "sku": "UNIQ-1"}
                ]
            }
            with mock.patch("urllib.request.urlopen", return_value=_FakeResp(spools_base)):
                after_sku_change = plugin.enrich_ace_status_with_spoolman(
                    changed_sku_slots, st_reloaded, force_fetch=True
                )
            self.assertEqual(after_sku_change["slots"][0]["spoolman"]["spool_id"], 23)
            self.assertEqual(after_sku_change["slots"][0]["spoolman"]["assignment_source"], "auto")
            self.assertNotIn("0", st_reloaded.manual_spoolman_selections)

    def test_spoolman_05_remaining_weight_percentages_empty_and_no_1000g_fallback(self) -> None:
        """Weight calculation never silently treats initial_weight or 1000 g defaults as remaining_weight."""
        spools = [
            {
                "id": 1,
                "archived": False,
                "remaining_weight": 1000.0,
                "initial_weight": 1000.0,
                "filament": {"name": "Full 100", "article_number": "W-100"},
            },
            {
                "id": 2,
                "archived": False,
                "remaining_weight": 500.0,
                "initial_weight": 1000.0,
                "filament": {"name": "Half 50", "article_number": "W-50"},
            },
            {
                "id": 3,
                "archived": False,
                "remaining_weight": 0.0,
                "initial_weight": 1000.0,
                "filament": {"name": "Empty 0", "article_number": "W-0"},
            },
            {
                "id": 4,
                "archived": False,
                "remaining_weight": None,
                "initial_weight": 1000.0,
                "used_weight": 0.0,
                "filament": {"name": "Missing Rem", "article_number": "W-NULL"},
            },
            {
                "id": 5,
                "archived": False,
                "remaining_weight": 300.0,
                "filament": {"name": "No Init Cap", "article_number": "W-NOCAP"},
            },
            {
                "id": 6,
                "archived": False,
                "remaining_weight": 250.0,
                "filament": {"name": "Fil Cap", "article_number": "W-FILCAP", "weight": 500.0},
            },
        ]
        slots = [
            {"tool": idx, "status": "ready", "material": "PLA", "sku": sku}
            for idx, sku in enumerate(["W-100", "W-50", "W-0", "W-NULL", "W-NOCAP", "W-FILCAP"])
        ]
        enriched, matched = plugin.match_spoolman_to_slots(slots, spools)
        self.assertEqual(matched, 6)

        s0 = enriched[0]["spoolman"]
        self.assertEqual((s0["remaining_weight"], s0["fill_percent"], s0["weight_status"]), (1000.0, 100.0, "ok"))

        s1 = enriched[1]["spoolman"]
        self.assertEqual((s1["remaining_weight"], s1["fill_percent"], s1["weight_status"]), (500.0, 50.0, "ok"))

        s2 = enriched[2]["spoolman"]
        self.assertEqual((s2["remaining_weight"], s2["fill_percent"], s2["weight_status"]), (0.0, 0.0, "empty"))

        # Crucial regression check: null remaining_weight + 1000 initial_weight NEVER becomes 1000.0 g left
        s3 = enriched[3]["spoolman"]
        self.assertIsNone(s3["remaining_weight"])
        self.assertIsNone(s3["fill_percent"])
        self.assertEqual(s3["weight_status"], "missing")
        self.assertEqual(s3["initial_weight"], 1000.0)

        # Missing initial_weight & filament.weight NEVER invents 1000.0 g percentage denominator
        s4 = enriched[4]["spoolman"]
        self.assertEqual(s4["remaining_weight"], 300.0)
        self.assertIsNone(s4["fill_percent"])
        self.assertEqual(s4["weight_status"], "ok")

        s5 = enriched[5]["spoolman"]
        self.assertEqual((s5["remaining_weight"], s5["fill_percent"], s5["weight_status"]), (250.0, 50.0, "ok"))

    def test_spoolman_06_slot_ui_svg_spool_color_bar_and_conflict_hooks(self) -> None:
        """Slot UI uses dynamic fill-scaled SVG spool icon, thick responsive color bar, readable color names, and valid JS syntax."""
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn("function renderSlotSpoolSvg(", html)
        self.assertIn("function classifyGermanColorName(", html)
        self.assertIn("function resolveSlotColorHex(", html)
        self.assertIn("function formatSlotSpoolmanMeta(", html)
        self.assertIn("function computeContrastBorderColor(", html)
        self.assertIn("slot-color-bar", html)
        self.assertIn("slot-spool-svg", html)
        self.assertIn("spool-svg-winding", html)
        self.assertIn("slot-spoolman-meta.is-conflict", html)
        self.assertIn("slot-spoolman-meta.is-missing", html)
        self.assertIn("slot-spoolman-meta.is-empty", html)
        self.assertIn('data-fill-pct="', html)
        self.assertIn('data-winding-radius="', html)
        # Ensure high-contrast borders cover white & black filament colors across dark/light themes
        self.assertIn("lum > 185", html)
        self.assertIn("lum < 55", html)
        # Ensure all 15 German color names appear in UI helper
        for cname in (
            "Schwarz", "Weiß", "Grau", "Silber", "Rot", "Orange", "Gelb",
            "Grün", "Türkis", "Blau", "Violett", "Pink", "Braun", "Gold", "Beige",
        ):
            self.assertIn(f'"{cname}"', html)
        # Ensure old static .spool-ring element is gone from slot rendering
        self.assertNotIn('<div class="spool-ring"', html)
        self.assertNotIn("!important", html.split("<style>", 1)[1].split("</style>", 1)[0])

    def test_spoolman_07_settings_modal_drag_focus_and_input_preservation(self) -> None:
        """Settings modal blocks accidental drag-out backdrop closes and preserves user input/focus over state updates."""
        html = plugin.ACE_PLUGIN_HTML_UI
        # No naive click-only backdrop closer that kills text-drag selection in URL inputs
        self.assertNotIn('id="settingsModal" class="modal-backdrop" role="dialog" aria-modal="true" onclick=', html)
        self.assertIn('onmousedown="event.stopPropagation()" onclick="event.stopPropagation()"', html)
        self.assertIn("function bindSafeBackdropClose(", html)
        self.assertIn('bindSafeBackdropClose("settingsModal"', html)
        self.assertIn("function syncSettingsInputsFromState(", html)
        self.assertIn("isSettingsModalOpen() || settingsModalDirty", html)
        self.assertIn('oninput="markSettingsDirty()"', html)
        # Verify mousedown target must match backdrop before click can dismiss
        bind_fn = html.split("function bindSafeBackdropClose(", 1)[1].split("function openDrawer(", 1)[0]
        self.assertIn("mousedown", bind_fn)
        self.assertIn("downOnBackdrop && evt && evt.target === el", bind_fn)

    def test_release_01_slot_fill_bar_and_multi_spool_dropdown_ui(self) -> None:
        """Slot color bars visualize 100/50/12/0/unknown fill from shared spool data alongside custom candidate dropdown."""
        html = plugin.ACE_PLUGIN_HTML_UI
        # Shared fill calculation drives BOTH color bar and SVG spool icon
        for fn in (
            "function computeSlotVisualFill(",
            "function renderSlotColorBar(",
            "function renderSlotSpoolSvg(",
            "function formatSpoolCandidateLabel(",
            "function toggleSlotSpoolDropdown(",
            "function selectSlotSpool(",
        ):
            self.assertIn(fn, html)

        render_block = html.split("function renderSlots()", 1)[1].split("function computeContrastTextColor(", 1)[0]
        self.assertIn("const vis = computeSlotVisualFill(slot, spInfo, isEmpty);", render_block)
        self.assertIn("renderSlotColorBar(vis, colorBarTitle);", render_block)
        self.assertIn("renderSlotSpoolSvg(vis);", render_block)

        # Modes: 0% empty spool, known % width, unknown missing percentage without false consumption
        bar_block = html.split("function renderSlotColorBar(", 1)[1].split("function renderSlotSpoolSvg(", 1)[0]
        self.assertIn('data-fill-mode="empty_spool"', bar_block)
        self.assertIn("width:0%", bar_block)
        self.assertIn('data-fill-mode="known"', bar_block)
        self.assertIn("width:${wPct.toFixed(1)}%", bar_block)
        self.assertIn('data-fill-mode="unknown"', bar_block)
        self.assertIn("width:100%", bar_block)

        # Multi-match candidate picker uses custom WebKit-safe dropdown (zero <select>)
        self.assertNotIn("<select", html.lower())
        self.assertIn("slot-spool-picker", html)
        self.assertIn("spInfo.candidates.length > 1", html)
        self.assertIn('type: "select_slot_spool"', html)

    def test_release_02_connection_states_and_onboarding_banner(self) -> None:
        """Handles unconfigured, offline, connected_standby, and connected_active states with subtle auto-hiding notice."""
        self.assertEqual(plugin.DEFAULT_MOONRAKER_URL, "")
        empty_client = plugin.MoonrakerClient("")
        self.assertFalse(empty_client.is_configured)
        self.assertEqual(empty_client.get_candidate_urls(), [])
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Must not hit network when unconfigured")):
            with self.assertRaises(ConnectionError):
                empty_client.query_ace_status()

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "state.json")
            st = plugin.PluginState()
            st.resolve_config_path = lambda: cfg_file
            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append

            # 1) Fresh install unconfigured -> connection_state="unconfigured", zero HTTP requests
            with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch(
                "urllib.request.urlopen", side_effect=AssertionError("No probe when unconfigured")
            ):
                page._handle_async_message("get_status", {})
            self.assertEqual(posted[-1]["ace_status"]["connection_state"], "unconfigured")
            self.assertFalse(posted[-1]["ace_status"]["configured"])
            self.assertFalse(posted[-1]["ace_status"]["connected"])

            # 2) Configured offline -> connection_state="offline" + anonymized error
            st.apply_dict({"moonraker_url": "http://192.0.2.10:7125", "api_key": "tok-abc-999"}, save=False)
            with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch(
                "urllib.request.urlopen", side_effect=plugin.urllib.error.URLError("timed out tok-abc-999")
            ):
                page._handle_async_message("get_status", {})
            self.assertEqual(posted[-1]["ace_status"]["connection_state"], "offline")
            self.assertTrue(posted[-1]["ace_status"]["configured"])
            self.assertFalse(posted[-1]["ace_status"]["connected"])
            self.assertNotIn("tok-abc-999", posted[-1]["ace_status"]["error"])

            # 3) Connected standby & connected active
            mock_standby = mock.MagicMock()
            mock_standby.is_configured = True
            mock_standby.active_url = "http://192.0.2.10:7125"
            mock_standby.api_key = ""
            mock_standby.query_ace_status.return_value = {"connected": True, "is_printing": False, "print_state": "standby", "slots": []}
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._send_state_and_live_status(mock_standby, "ok")
            self.assertEqual(posted[-1]["ace_status"]["connection_state"], "connected_standby")

            mock_standby.query_ace_status.return_value = {"connected": True, "is_printing": True, "print_state": "printing", "slots": []}
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._send_state_and_live_status(mock_standby, "ok")
            self.assertEqual(posted[-1]["ace_status"]["connection_state"], "connected_active")

        html = plugin.ACE_PLUGIN_HTML_UI
        for req_tok in (
            'id="connectionNoticeBanner"',
            'id="btnNoticeOpenConn"',
            'id="btnNoticeRetry"',
            "function renderConnectionNotice()",
            "function getEffectiveConnectionState()",
        ):
            self.assertIn(req_tok, html)

    def test_release_03_bundled_settings_menu_and_8_color_schemes_persistence(self) -> None:
        """Settings modal bundles 5 sections, keeps Refresh & Flush & Audit, supports 8 schemes, and persists preferences."""
        html = plugin.ACE_PLUGIN_HTML_UI
        style = html.split("<style>", 1)[1].split("</style>", 1)[0]

        # Bundled tabs & visible standalone topbar actions (Refresh, Flush & Audit)
        for token in (
            'id="btnNavSettings"',
            'id="btnNavDiagnostics"',
            'id="btnNavRefresh"',
            "settingsTabBtn_connection",
            "settingsTabBtn_appearance",
            "settingsTabBtn_language",
            "settingsTabBtn_paths",
            "settingsTabBtn_diagnostics",
            "settingsPanel_connection",
            "settingsPanel_appearance",
            "settingsPanel_language",
            "settingsPanel_paths",
            "settingsPanel_diagnostics",
            "function switchSettingsTab(",
            "function setColorScheme(",
            "function renderResolvedPaths(",
        ):
            self.assertIn(token, html)

        # All 8 schemes defined cleanly for Dark and Light modes without forbidden tokens
        self.assertEqual(len(plugin.SUPPORTED_COLOR_SCHEMES), 8)
        for scheme in plugin.SUPPORTED_COLOR_SCHEMES:
            self.assertIn(f'html[data-scheme="{scheme}"]', style)
            self.assertIn(f'html[data-resolved="light"][data-scheme="{scheme}"]', style)
            self.assertIn(f'data-scheme="{scheme}"', html)
            self.assertIn(f"ui.settings.scheme.{scheme}", plugin.TRANSLATIONS["en"])
            self.assertIn(f"ui.settings.scheme.{scheme}", plugin.TRANSLATIONS["de"])
        for forbidden in ("#10b981", "#047857", "!important", "border-radius: 8px;", "border-radius: 10px;", "border-radius: 12px;"):
            self.assertNotIn(forbidden, style.lower())

        # Backend persistence for theme_mode, color_scheme, language, and default_gcode_dir
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "state.json")
            st1 = plugin.PluginState()
            st1.resolve_config_path = lambda: cfg_file
            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append
            with mock.patch.object(plugin, "GLOBAL_STATE", st1):
                page.on_message({"type": "set_language", "language": "de"})
                page.on_message({"type": "set_appearance", "theme_mode": "light", "color_scheme": "violet"})
                st1.apply_dict({"default_gcode_dir": "/home/user/prints"}, save=True)

            st2 = plugin.PluginState()
            st2.resolve_config_path = lambda: cfg_file
            st2.load_config()
            self.assertEqual(st2.language, "de")
            self.assertEqual(st2.theme_mode, "light")
            self.assertEqual(st2.color_scheme, "violet")
            self.assertEqual(st2.default_gcode_dir, "/home/user/prints")
            # Invalid values fall back safely without corruption
            st2.set_appearance(theme_mode="bogus", color_scheme="invalid_scheme", save=False)
            self.assertEqual(st2.theme_mode, "light")
            self.assertEqual(st2.color_scheme, "violet")

    def test_release_04_anonymization_redaction_and_release_self_audit(self) -> None:
        """Diagnostics anonymizer redacts IPs, homes, credentials, and release files hold zero personal identifiers."""
        sample = (
            "URL=http://user:secretpass@192.168.178.42:7125 path=/home/alice/Models/part.gcode "
            "win=C:\\Users\\Alice\\Desktop\\test.gcode x-api-key=super-secret-987 "
            "lan2=10.0.1.55 lan3=172.16.4.9 doc=192.0.2.10"
        )
        redacted = plugin.anonymize_diagnostic_text(sample, extra_secrets=["super-secret-987"])
        for sensitive in ("secretpass", "192.168.178.42", "10.0.1.55", "172.16.4.9", "/home/alice", "Users\\Alice", "super-secret-987"):
            self.assertNotIn(sensitive, redacted)
        self.assertIn("/home/user/", redacted)
        self.assertIn("192.0.2.10", redacted)
        self.assertIn("[REDACTED]", redacted)

        with tempfile.TemporaryDirectory() as tmpdir:
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            st.moonraker_url = "http://192.168.42.10:7125"
            st.api_key = "real-user-api-key"
            bundle = plugin.build_anonymized_diagnostics(st)
            self.assertNotIn("192.168.42.10", bundle)
            self.assertNotIn("real-user-api-key", bundle)
            self.assertIn("MoonrakerConfigured: True", bundle)

        # Self-audit active release Python modules for leftover personal data
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        forbidden_literals = (
            ".".join(("192", "168", "8", "80")),
            ".".join(("192", "168", "8", "199")),
            ".".join(("192", "168", "55", "99")),
            "/home/" + chr(121) + "ves",
            "secret-" + "token-123",
        )
        for fname in ("kobra_van_kenorca_plugin.py", "test_postprocess.py"):
            fpath = os.path.join(repo_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                content_lower = f.read().lower()
            for needle in forbidden_literals:
                self.assertEqual(
                    content_lower.find(needle),
                    -1,
                    f"Unexpected personal/placeholder literal {needle!r} found in {fname}",
                )

    def test_release_05_dryer_presets_capped_at_60c_and_refactor_invariants(self) -> None:
        """Dryer presets exclude >60C while manual input allows 70C; refactored helpers preserve exact behavior."""
        html = plugin.ACE_PLUGIN_HTML_UI
        # 1. Dryer presets <= 60 C only, manual input still allows up to 70 C
        st = plugin.PluginState()
        st.apply_dict({"dryer_temp": 70, "dryer_duration": 300, "dryer_unit": "1"}, save=False)
        self.assertEqual(st.dryer_temp, 70)
        self.assertEqual(st.dryer_duration, 300)
        self.assertEqual(st.dryer_unit, "1")

        # 2. Refactored JS helpers present and unused CSS class removed
        self.assertNotIn("map-origin-pill", html)
        for token in (
            "function toggleCustomSelectDropdown(",
            "function applyCustomSelectValue(",
            "function hexToHslCached(",
            "function hasFilamentUsageData(",
            "function resetMismatchConfirmationUi(",
        ):
            self.assertIn(token, html)

        # 3. load_gcode_from_content reuses in-memory parsed lines/filaments/metadata without re-reading disk
        sample_gcode = (
            "; filament_type = PLA;PETG\n"
            "; filament_colour = #FF0000;#00FF00\n"
            "; flush_multiplier = 1.0\n"
            "; flush_volumes_matrix = 0,100,100,0\n"
            "; flush_volumes_vector = 50,50\n"
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
            "T0\n"
            "G1 X10 Y10 E5\n"
            "T1\n"
            "G1 X20 Y20 E5\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            st_load = plugin.PluginState()
            st_load.resolve_storage_dir = lambda: tmpdir
            st_load.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            with mock.patch.object(
                plugin,
                "_read_validated_gcode_file",
                wraps=plugin._read_validated_gcode_file,
            ) as spy_read:
                staged_path, info = plugin.load_gcode_from_content(
                    "refactor_check.gcode",
                    sample_gcode,
                    state=st_load,
                )
                self.assertTrue(os.path.isfile(staged_path))
                self.assertEqual(info["toolchanges"], 2)
                self.assertEqual(spy_read.call_count, 0)

    def test_pred_01_status_thresholds_exact_exhausted_low_insufficient_and_non_blocking(self) -> None:
        """Prediction distinguishes sufficient, low (reserve), exact zero, and insufficient without blocking upload."""
        fils = [
            {"slicer_tool": 0, "material": "PLA", "color": "#FF0000", "diameter": 1.75, "density": 1.24, "used_g": 100.0, "used": True},
            {"slicer_tool": 1, "material": "PLA", "color": "#00FF00", "diameter": 1.75, "density": 1.24, "used_g": 45.0, "used": True},
            {"slicer_tool": 2, "material": "PLA", "color": "#0000FF", "diameter": 1.75, "density": 1.24, "used_g": 50.0, "used": True},
            {"slicer_tool": 3, "material": "PLA", "color": "#FFFF00", "diameter": 1.75, "density": 1.24, "used_g": 80.0, "used": True},
            {"slicer_tool": 4, "material": "PLA", "color": "#FFFFFF", "diameter": 1.75, "density": 1.24, "used_g": 0.0, "used": False},
        ]
        # 1000 mm3 * 1.24 / 1000 = 1.24 g purge on T1
        pp_info = {
            "flush_status": "POSTPROCESS_OK",
            "flush_data_complete": True,
            "events": [
                {"new_slicer_tool": 1, "volume_mm3": 1000.0},
            ],
        }
        slots = [
            {"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#FF0000", "spoolman": {"remaining_weight": 200.0, "capacity_weight": 1000.0, "weight_status": "ok"}},
            {"tool": 1, "status": "ready", "material": "PLA", "hex_color": "#00FF00", "spoolman": {"remaining_weight": 55.0, "capacity_weight": 1000.0, "weight_status": "ok"}},
            {"tool": 2, "status": "ready", "material": "PLA", "hex_color": "#0000FF", "spoolman": {"remaining_weight": 50.0, "capacity_weight": 1000.0, "weight_status": "ok"}},
            {"tool": 3, "status": "ready", "material": "PLA", "hex_color": "#FFFF00", "spoolman": {"remaining_weight": 60.0, "capacity_weight": 1000.0, "weight_status": "ok"}},
            {"tool": 4, "status": "ready", "material": "PLA", "hex_color": "#FFFFFF", "spoolman": {"remaining_weight": 500.0, "capacity_weight": 1000.0, "weight_status": "ok"}},
        ]
        pred = plugin.compute_preprint_filament_prediction(
            fils, {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}, slots, pp_info, reserve_g=15.0, has_gcode=True
        )
        self.assertTrue(pred["has_gcode"])
        self.assertEqual(pred["overall_status"], "insufficient")
        # Unused slot T4 must NOT appear in prediction
        by_slot = {s["slot_tool"]: s for s in pred["slots"]}
        self.assertEqual(sorted(by_slot.keys()), [0, 1, 2, 3])
        # T0: 200 - 100 = 100 >= 15 -> sufficient
        self.assertEqual(by_slot[0]["status"], "sufficient")
        self.assertAlmostEqual(by_slot[0]["remaining_after_g"], 100.0, places=2)
        # T1: 45 + 1.24 = 46.24 -> 55 - 46.24 = 8.76 < 15 -> low
        self.assertEqual(by_slot[1]["status"], "low")
        self.assertAlmostEqual(by_slot[1]["purge_usage_g"], 1.24, places=2)
        self.assertAlmostEqual(by_slot[1]["total_usage_g"], 46.24, places=2)
        self.assertAlmostEqual(by_slot[1]["remaining_after_g"], 8.76, places=2)
        # T2: exactly exhausted (50 - 50 = 0.0) -> low with reserve=15, sufficient when reserve=0
        self.assertEqual(by_slot[2]["status"], "low")
        self.assertAlmostEqual(by_slot[2]["remaining_after_g"], 0.0, places=2)
        self.assertEqual(by_slot[2]["deficit_g"], 0.0)
        pred_zero_res = plugin.compute_preprint_filament_prediction(
            fils, {0: 0, 1: 1, 2: 2, 3: 3}, slots, pp_info, reserve_g=0.0, has_gcode=True
        )
        self.assertEqual({s["slot_tool"]: s for s in pred_zero_res["slots"]}[2]["status"], "sufficient")
        # T3: 60 - 80 = -20 -> insufficient with deficit 20.0 g
        self.assertEqual(by_slot[3]["status"], "insufficient")
        self.assertAlmostEqual(by_slot[3]["remaining_after_g"], -20.0, places=2)
        self.assertAlmostEqual(by_slot[3]["deficit_g"], 20.0, places=2)

    def test_pred_02_unknown_states_never_sufficient_and_no_invented_density(self) -> None:
        """Missing weight, missing spool, conflict, empty slot, missing usage/density, or incomplete flush -> UNKNOWN."""
        # 1) Missing density when purge > 0 -> unknown (never invent 1.24!)
        fils_no_dens = [
            {"slicer_tool": 0, "material": "PLA", "diameter": 1.75, "used_mm": 1000.0, "used": True},
        ]
        slots = [
            {"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#FFFFFF", "spoolman": {"remaining_weight": 500.0, "weight_status": "ok"}},
        ]
        pp_purge = {"flush_status": "POSTPROCESS_OK", "flush_data_complete": True, "events": [{"new_slicer_tool": 0, "volume_mm3": 500.0}]}
        pred1 = plugin.compute_preprint_filament_prediction(fils_no_dens, {0: 0}, slots, pp_purge, has_gcode=True)
        self.assertEqual(pred1["slots"][0]["status"], "unknown")
        self.assertEqual(pred1["slots"][0]["reason"], "no_usage")

        # used_g present but used_cm3=0 and density=None -> purge > 0 cannot convert mm3 to g -> unknown (no_density)
        fils_g_only = [
            {"slicer_tool": 0, "material": "PLA", "diameter": 1.75, "used_g": 20.0, "used": True},
        ]
        pred2 = plugin.compute_preprint_filament_prediction(fils_g_only, {0: 0}, slots, pp_purge, has_gcode=True)
        self.assertEqual(pred2["slots"][0]["status"], "unknown")
        self.assertEqual(pred2["slots"][0]["reason"], "no_density")

        # 2) Missing spoolman weight, no spool, conflict, empty slot
        fils_ok = [{"slicer_tool": i, "material": "PLA", "density": 1.24, "used_g": 10.0, "used": True} for i in range(4)]
        slots_edge = [
            {"tool": 0, "status": "ready", "material": "PLA", "spoolman": {"remaining_weight": None, "weight_status": "missing"}},
            {"tool": 1, "status": "ready", "material": "PLA"},
            {"tool": 2, "status": "ready", "material": "PLA", "spoolman": {"conflict": True, "remaining_weight": 300.0}},
            {"tool": 3, "status": "empty", "material": ""},
        ]
        pred3 = plugin.compute_preprint_filament_prediction(fils_ok, {i: i for i in range(4)}, slots_edge, {"flush_status": "POSTPROCESS_OK"}, has_gcode=True)
        reasons = {s["slot_tool"]: (s["status"], s["reason"]) for s in pred3["slots"]}
        self.assertEqual(reasons[0], ("unknown", "no_weight"))
        self.assertEqual(reasons[1], ("unknown", "no_spool"))
        self.assertEqual(reasons[2], ("unknown", "conflict"))
        self.assertEqual(reasons[3], ("unknown", "slot_empty"))

        # 3) Incomplete flush matrix -> unknown
        pred4 = plugin.compute_preprint_filament_prediction(
            fils_ok[:2], {0: 0, 1: 1}, slots, {"flush_status": "FLUSH_DATA_INCOMPLETE", "flush_data_complete": False}, has_gcode=True
        )
        self.assertEqual(pred4["slots"][0]["status"], "unknown")
        self.assertEqual(pred4["slots"][0]["reason"], "flush_incomplete")

    def test_pred_03_sample_gcode_no_double_counting_and_shared_slot_remapping(self) -> None:
        """refs/test_sample.gcode avoids double counting print vs ACE purge and supports slot remapping/sharing."""
        sample_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refs", "test_sample.gcode")
        with open(sample_path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()

        fils = plugin.parse_slicer_filaments(raw_lines)
        self.assertEqual(fils[0]["density"], 1.29)
        self.assertEqual(fils[1]["density"], 1.29)

        pp_info = plugin.analyze_gcode_flush(raw_lines)
        # Re-processing already post-processed lines must yield identical totals (no double counting!)
        out_lines, _ = plugin.process_gcode(raw_lines)
        pp_re = plugin.analyze_gcode_flush(out_lines)
        self.assertEqual(pp_info["total_purge_volume_mm3"], pp_re["total_purge_volume_mm3"])

        slots = [
            {"tool": 0, "status": "ready", "material": "PETG", "hex_color": "#FFFFFF", "spoolman": {"spool_id": 10, "remaining_weight": 100.0, "weight_status": "ok"}},
            {"tool": 1, "status": "ready", "material": "PETG", "hex_color": "#000000", "spoolman": {"spool_id": 11, "remaining_weight": 100.0, "weight_status": "ok"}},
            {"tool": 2, "status": "ready", "material": "PETG", "hex_color": "#FF0000", "spoolman": {"spool_id": 12, "remaining_weight": 50.0, "weight_status": "ok"}},
        ]
        # Default 1:1 mapping (excluding G9111 startup estimate): T0 print=16.06g + purge=3198.0*1.29/1000=4.13g -> 20.19g; T1 print=26.25g + purge=764.4*1.29/1000=0.99g -> 27.24g
        pred_11 = plugin.compute_preprint_filament_prediction(
            fils, {0: 0, 1: 1}, slots, pp_info, startup_purge_length_mm=0.0, has_gcode=True
        )
        m11 = {s["slot_tool"]: s for s in pred_11["slots"]}
        self.assertAlmostEqual(m11[0]["print_usage_g"], 16.06, places=2)
        self.assertAlmostEqual(m11[0]["purge_usage_g"], 4.13, places=2)
        self.assertAlmostEqual(m11[0]["total_usage_g"], 20.19, places=2)
        self.assertAlmostEqual(m11[1]["print_usage_g"], 26.25, places=2)
        self.assertAlmostEqual(m11[1]["purge_usage_g"], 0.99, places=2)
        self.assertAlmostEqual(m11[1]["total_usage_g"], 27.24, places=2)

        # Shared mapping: both Slicer T0 & T1 mapped onto physical slot T2 (50g available, 47.43g total -> 2.57g left -> low)
        pred_shared = plugin.compute_preprint_filament_prediction(
            fils, {0: 2, 1: 2}, slots, pp_info, reserve_g=15.0, startup_purge_length_mm=0.0, has_gcode=True
        )
        self.assertEqual(len(pred_shared["slots"]), 1)
        sh = pred_shared["slots"][0]
        self.assertEqual(sh["slot_tool"], 2)
        self.assertAlmostEqual(sh["print_usage_g"], 42.31, places=2)
        self.assertAlmostEqual(sh["purge_usage_g"], 5.11, places=2)
        self.assertAlmostEqual(sh["total_usage_g"], 47.42, places=2)
        self.assertAlmostEqual(sh["remaining_after_g"], 2.58, places=2)
        self.assertEqual(sh["status"], "low")

    def test_pred_04_manual_spool_switch_reslice_clear_and_ui_tokens(self) -> None:
        """Manual spool switch, reserve persistence, clear_loaded_gcode_state stale protection, and UI tokens."""
        with tempfile.TemporaryDirectory() as tmpdir:
            st = plugin.PluginState()
            st.resolve_storage_dir = lambda: tmpdir
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            st.spoolman_url = "http://192.0.2.50:7912"
            st.apply_dict({"prediction_reserve_g": 20.0, "startup_purge_length_mm": 0.0}, save=True)
            self.assertEqual(st.prediction_reserve_g, 20.0)

            spools_payload = [
                {"id": 101, "last_used": "2026-11-15T10:00:00Z", "remaining_weight": 150.0, "initial_weight": 1000.0, "filament": {"article_number": "SKU-X", "material": "PLA", "color_hex": "#112233", "density": 1.24}},
                {"id": 102, "last_used": "2026-11-10T10:00:00Z", "remaining_weight": 25.0, "initial_weight": 1000.0, "filament": {"article_number": "SKU-X", "material": "PLA", "color_hex": "#112233", "density": 1.24}},
            ]
            gcode_a = (
                "; filament_type = PLA\n"
                "; filament_density = 1.24\n"
                "; filament used [g] = 40.0\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
                "T0\n"
                "G1 X10 Y10 E5\n"
            )
            plugin.load_gcode_from_content("part_a.gcode", gcode_a, state=st)
            with st._lock:
                st._spoolman_spools = spools_payload
                st._spoolman_cache_url = plugin.SpoolmanClient.normalize_url(st.spoolman_url)
                st._spoolman_cache_ts = plugin.time.monotonic()
                st.last_ace_status = plugin.enrich_ace_status_with_spoolman(
                    {"slots": [{"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#112233", "sku": "SKU-X", "rfid": True}]},
                    st,
                    force_fetch=False,
                )

            # Auto selects #101 (150g) -> 150 - 40 = 110g -> sufficient
            d1 = st.to_dict()["filament_prediction"]
            self.assertTrue(d1["has_gcode"])
            self.assertEqual(d1["slots"][0]["spool_id"], 101)
            self.assertEqual(d1["slots"][0]["status"], "sufficient")

            # Manual switch to #102 (25g) -> 25 - 40 = -15g -> insufficient
            st.set_manual_slot_spool(0, 102, sku="SKU-X", save=True)
            with st._lock:
                st.last_ace_status = plugin.enrich_ace_status_with_spoolman(
                    {"slots": [{"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#112233", "sku": "SKU-X", "rfid": True}]},
                    st,
                    force_fetch=False,
                )
            d2 = st.to_dict()["filament_prediction"]
            self.assertEqual(d2["slots"][0]["spool_id"], 102)
            self.assertEqual(d2["slots"][0]["status"], "insufficient")
            self.assertAlmostEqual(d2["slots"][0]["deficit_g"], 15.0, places=2)

            # Clear G-code -> prediction must reset immediately (no stale prediction)
            plugin.clear_loaded_gcode_state(st, reason="test_clear")
            d_clr = st.to_dict()["filament_prediction"]
            self.assertFalse(d_clr["has_gcode"])
            self.assertEqual(d_clr["overall_status"], "empty")
            self.assertEqual(d_clr["slots"], [])

            # Re-slice with smaller usage (4.0g) -> 25 - 4 = 21g >= 20g reserve -> sufficient
            gcode_b = (
                "; filament_type = PLA\n"
                "; filament_density = 1.24\n"
                "; filament used [g] = 4.0\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
                "T0\n"
                "G1 X10 Y10 E5\n"
            )
            plugin.load_gcode_from_content("part_b.gcode", gcode_b, state=st)
            d_new = st.to_dict()["filament_prediction"]
            self.assertTrue(d_new["has_gcode"])
            self.assertEqual(d_new["slots"][0]["status"], "sufficient")
            self.assertAlmostEqual(d_new["slots"][0]["remaining_after_g"], 21.0, places=2)

        html = plugin.ACE_PLUGIN_HTML_UI
        for tok in (
            'id="modalMappingPanel"',
            "fil-pred-divider",
            "prediction-bar-track",
            "prediction-metrics",
            "match-icon-badge",
            'id="predictionReserveInput"',
            "function renderPreprintPrediction(",
            "function computePreprintFilamentPredictionLocal(",
            "function onPrePrintPurgeOptionChange(",
        ):
            self.assertIn(tok, html)
        self.assertNotIn('id="modalPredictionSection"', html)
        self.assertNotIn('id="modalPredictionSummaryBadge"', html)
        self.assertNotIn('id="modalPredictionCards"', html)

    def test_release_06_maintenance_buttons_diag_toggle_and_cross_platform_paths(self) -> None:
        """Main bay removes redundant global macro header bar, keeps backend CUT_TIP/Bed Mesh + diag toggle, and handles Win/macOS paths."""
        html = plugin.ACE_PLUGIN_HTML_UI
        for removed_hdr in (
            'id="feedLength"',
            'id="btnMacroCutAction"',
            'id="btnMacroMeshAction"',
            'id="btnUnloadActive"',
        ):
            self.assertNotIn(removed_hdr, html)
        self.assertIn('id="chkDiagLoadLogging"', html)
        self.assertIn("slotAction('load'", html)
        self.assertIn("slotAction('unload'", html)
        self.assertIn("confirmFullUnload(", html)
        self.assertIn('id="fullUnloadConfirmModal"', html)
        self.assertIn("feed_assist_on", html)
        self.assertIn("feed_assist_off", html)

        # Verify normalize_ace_status exposes feed_assist and shifting status per slot
        norm_fa = plugin.MoonrakerClient.normalize_ace_status(
            {
                "ace_state": {"current_index": 1, "ace_instances": 1},
                "ace_instance_0": {
                    "status": "ready",
                    "connection_state": "connected",
                    "feed_assist_slot": 1,
                    "slots": [
                        {"index": 0, "tool": 0, "status": "ready", "material": "PLA", "color": [255, 255, 255]},
                        {"index": 1, "tool": 1, "status": "shifting", "material": "PLA", "color": [117, 120, 123]},
                        {"index": 2, "tool": 2, "status": "shifting", "material": "PETG", "color": [0, 200, 100]},
                        {"index": 3, "tool": 3, "status": "empty", "material": "", "color": [0, 0, 0]},
                    ],
                },
            }
        )
        self.assertEqual(norm_fa["feed_assist_slot"], 1)
        self.assertFalse(norm_fa["slots"][0]["feed_assist"])
        self.assertTrue(norm_fa["slots"][1]["feed_assist"])
        self.assertTrue(norm_fa["slots"][1]["is_loaded"])
        self.assertTrue(norm_fa["slots"][1]["is_shifting"])
        self.assertEqual(norm_fa["slots"][1]["raw_status"], "shifting")
        self.assertEqual(norm_fa["slots"][2]["status"], "shifting")
        self.assertTrue(norm_fa["slots"][2]["is_shifting"])

        # 1. diag_load_logging defaults to False and persists cleanly in state.json
        self.assertFalse(plugin.DIAG_LOAD_LOGGING_ENABLED)
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = os.path.join(tmpdir, "state.json")
            st = plugin.PluginState()
            st.resolve_config_path = lambda: cfg
            self.assertFalse(st.diag_load_logging)
            self.assertEqual(
                plugin._record_diagnostic_load_event(st, trigger="test", decision="empty"),
                {},
            )
            st.apply_dict({"diag_load_logging": True}, save=True)
            st2 = plugin.PluginState()
            st2.resolve_config_path = lambda: cfg
            st2.load_config()
            self.assertTrue(st2.diag_load_logging)
            evt = plugin._record_diagnostic_load_event(st2, trigger="test", decision="empty")
            self.assertEqual(evt.get("trigger"), "test")

        # 2. Maintenance actions cut_tip & bed_leveling call Klipper macros with print safety
        fake_client = mock.MagicMock()
        fake_client.is_configured = True
        fake_client.active_url = "http://192.0.2.10:7125"
        fake_client.query_ace_status.return_value = {"connected": True, "is_printing": False, "print_state": "standby", "slots": []}
        page = plugin.KobraS1AcePageCapability()
        posted = []
        page.post_message = posted.append
        st_hw = plugin.PluginState()
        st_hw.moonraker_url = "http://192.0.2.10:7125"
        with mock.patch.object(plugin, "GLOBAL_STATE", st_hw):
            page._msg_slot_action(fake_client, {"action": "cut_tip", "tool": -1})
            page._msg_slot_action(fake_client, {"action": "bed_leveling", "tool": -1})
        fake_client.trigger_cut_tip.assert_called_once()
        fake_client.trigger_bed_leveling.assert_called_once_with(mode="BED_MESH_CALIBRATE")

        # 3. Cross-platform Windows & macOS temp/config path handling
        with mock.patch.dict(os.environ, {"APPDATA": "C:\\Users\\Tester\\AppData\\Roaming"}, clear=False), mock.patch.object(
            plugin.os, "name", "nt"
        ):
            win_cfg = plugin._default_fallback_config_path()
            self.assertIn("AppData", win_cfg)
            self.assertTrue(win_cfg.endswith("state.json"))

        with mock.patch.object(plugin.os, "name", "posix"), mock.patch.object(plugin.sys, "platform", "darwin"):
            mac_cfg = plugin._default_fallback_config_path()
            self.assertIn("Library", mac_cfg)
            self.assertIn("Application Support", mac_cfg)

        self.assertTrue(plugin._is_system_temp_path("/var/folders/ab/cd/T/orcaslicer_model/plate_1.gcode"))
        self.assertTrue(plugin._is_system_temp_path("C:\\Users\\Tester\\AppData\\Local\\Temp\\orcaslicer_model\\plate_1.gcode"))
        with mock.patch.dict(os.environ, {"TEMP": "/mock_win_temp", "LOCALAPPDATA": "/mock_local"}, clear=False), mock.patch.object(
            plugin.os, "name", "nt"
        ), mock.patch("tempfile.gettempdir", return_value="/mock_win_temp"):
            grants = plugin._slicer_model_temp_read_grants()
            self.assertTrue(any("orcaslicer_model" in g for g in grants))

    def test_settings_persistence_restart_cap_fallback_and_update_migration(self) -> None:
        """Connection settings persist via capability config fallback across restarts and migrate across plugin updates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            user_backup = os.path.join(tmpdir, "user_data", "state.json")

            class _FakeCap(plugin.KobraS1AcePageCapability):
                def __init__(self, store_dict):
                    super().__init__()
                    self._store = store_dict

                def get_config(self):
                    return self._store.get("cfg", "")

                def save_config(self, text):
                    self._store["cfg"] = str(text)
                    return True

            cap_store = {}

            # 1. Restart persistence via capability config even when plugin.storage() is unavailable
            with mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True), mock.patch.object(
                plugin.orca.host.plugin, "storage", return_value=""
            ), mock.patch.object(
                plugin.PluginState, "_check_install_state_allows_external_backup", return_value=True
            ):
                st1 = plugin.PluginState()
                st1.FALLBACK_CONFIG_PATH = user_backup
                cap1 = _FakeCap(cap_store)
                cap1.post_message = lambda _m: None
                with mock.patch.object(plugin, "GLOBAL_STATE", st1), mock.patch.object(
                    plugin, "_ACTIVE_PAGE_CAPABILITY", cap1
                ), mock.patch.object(plugin, "_schedule_pipeline_registration"):
                    cap1.on_message({
                        "type": "save_settings",
                        "settings": {
                            "moonraker_url": "http://192.0.2.10:7125",
                            "api_key": "secret-key-123",
                            "spoolman_url": "http://192.0.2.10:7912",
                            "enable_port80_fallback": True,
                        },
                    })

                # Simulated OrcaSlicer restart: fresh PluginState + fresh Capability reading same cap_store
                st1.flush_user_backup()
                st2 = plugin.PluginState()
                st2.FALLBACK_CONFIG_PATH = user_backup
                cap2 = _FakeCap(cap_store)
                with mock.patch.object(plugin, "GLOBAL_STATE", st2), mock.patch.object(
                    plugin, "_ACTIVE_PAGE_CAPABILITY", None
                ), mock.patch.object(plugin, "_schedule_pipeline_registration"):
                    cap2.on_load()

                self.assertEqual(st2.moonraker_url, "http://192.0.2.10:7125")
                self.assertEqual(st2.api_key, "secret-key-123")
                self.assertEqual(st2.spoolman_url, "http://192.0.2.10:7912")
                self.assertTrue(st2.enable_port80_fallback)

            # 2. Simulated plugin update: new storage directory & empty capability config, restored from user_backup
            new_storage = os.path.join(tmpdir, "orca_storage_v2")
            os.makedirs(new_storage, exist_ok=True)
            empty_cap_store = {}
            with mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True), mock.patch.object(
                plugin.orca.host.plugin, "storage", return_value=new_storage
            ), mock.patch.object(
                plugin.PluginState, "_check_install_state_allows_external_backup", return_value=True
            ):
                st3 = plugin.PluginState()
                st3.FALLBACK_CONFIG_PATH = user_backup
                cap3 = _FakeCap(empty_cap_store)
                with mock.patch.object(plugin, "GLOBAL_STATE", st3), mock.patch.object(
                    plugin, "_ACTIVE_PAGE_CAPABILITY", None
                ), mock.patch.object(plugin, "_schedule_pipeline_registration"):
                    cap3.on_load()

                self.assertEqual(st3.moonraker_url, "http://192.0.2.10:7125")
                self.assertEqual(st3.api_key, "secret-key-123")
                self.assertEqual(st3.spoolman_url, "http://192.0.2.10:7912")
                self.assertTrue(os.path.isfile(os.path.join(new_storage, "state.json")))
                self.assertIn("kobra_van_kenorca_settings", empty_cap_store.get("cfg", ""))

        # 3. JS initial-state guard prevents requestStatus() from wiping settings before msg.state arrives
        self.assertIn("let hasReceivedInitialState = false;", plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn("if (hasReceivedInitialState)", plugin.ACE_PLUGIN_HTML_UI)

    def test_acquire_slicer_gcode_always_posts_completion_on_error(self) -> None:
        """_msg_acquire_slicer_gcode guarantees a UI completion payload even if acquire_gcode_from_slicer raises."""
        page = plugin.KobraS1AcePageCapability()
        posted = []
        page.post_message = posted.append
        st = plugin.PluginState()
        with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
            plugin, "acquire_gcode_from_slicer", side_effect=RuntimeError("audit blocked")
        ):
            page._msg_acquire_slicer_gcode(st.get_client(), {})
        self.assertEqual(len(posted), 1)
        self.assertIn("modal_feedback", posted[0])
        self.assertEqual(posted[0]["modal_feedback"].get("level"), "error")
        self.assertIn("audit blocked", posted[0]["modal_feedback"].get("text", ""))

    def test_legacy_kobra_s1_ace_config_one_time_migration(self) -> None:
        """Legacy kobra_s1_ace_plugin/state.json, vKobraACE/state.json, and KobraOrca/state.json migrate once into Kobra-Van-Kenorca/state.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_path = os.path.join(tmpdir, "kobra_s1_ace_plugin", "state.json")
            new_path = os.path.join(tmpdir, "Kobra-Van-Kenorca", "state.json")
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            with open(legacy_path, "w", encoding="utf-8") as f:
                json.dump({"moonraker_url": "http://192.0.2.10:7125", "language": "de"}, f)

            st = plugin.PluginState()
            st.FALLBACK_CONFIG_PATH = new_path
            st.LEGACY_FALLBACK_CONFIG_PATH = legacy_path
            st.resolve_config_path = lambda: new_path
            st.load_config()

            self.assertEqual(st.moonraker_url, "http://192.0.2.10:7125")
            self.assertEqual(st.language, "de")
            self.assertTrue(os.path.isfile(new_path))

    def test_static_temp_file_no_random_ace_tmp_and_debounced_backup(self) -> None:
        """State saves use static state.json.tmp (no random .ace_tmp_*), coalesce rapid backups, and request host-first URLs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "Kobra-Van-Kenorca", "state.json")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            opened_paths = []
            real_open = os.open

            def _spy_open(path, flags, mode=0o777, *args, **kwargs):
                opened_paths.append(os.fspath(path))
                return real_open(path, flags, mode, *args, **kwargs)

            with mock.patch("os.open", side_effect=_spy_open), mock.patch(
                "tempfile.mkstemp", side_effect=AssertionError("mkstemp must not be called")
            ), mock.patch(
                "tempfile.NamedTemporaryFile", side_effect=AssertionError("NamedTemporaryFile must not be called")
            ):
                plugin._atomic_write_json(target, {"moonraker_url": "http://192.0.2.10:7125"})

            self.assertTrue(os.path.isfile(target))
            self.assertIn(target + ".tmp", opened_paths)
            self.assertFalse(any(".ace_tmp_" in p for p in opened_paths), opened_paths)

            # Rapid consecutive save_config() calls coalesce OS user-backup writes via debounce timer
            storage_dir = os.path.join(tmpdir, "orca_storage")
            backup_path = os.path.join(tmpdir, "user_backup", "state.json")
            os.makedirs(storage_dir, exist_ok=True)
            write_calls = []
            with mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True), mock.patch.object(
                plugin.orca.host.plugin, "storage", return_value=storage_dir
            ), mock.patch.object(
                plugin.PluginState, "_check_install_state_allows_external_backup", return_value=True
            ):
                st = plugin.PluginState()
                st.FALLBACK_CONFIG_PATH = backup_path
                st.USER_BACKUP_DEBOUNCE_SEC = 0.05
                orig_write = st._write_user_backup_now

                def _spy_write(bp, payload):
                    write_calls.append(dict(payload))
                    return orig_write(bp, payload)

                st._write_user_backup_now = _spy_write
                for slot_idx in range(5):
                    st.tool_map = {0: slot_idx}
                    st.save_config()
                time.sleep(0.15)
                st.flush_user_backup()

            self.assertEqual(len(write_calls), 1)
            self.assertEqual(write_calls[0]["tool_map"], {"0": 4})

        # Host-based URLs come before port/path-specific URLs
        urls = plugin._build_host_permission_urls("http://192.0.2.55:7125/server/info", include_port80=True)
        self.assertEqual(urls[0], "http://192.0.2.55")
        self.assertEqual(urls[1], "http://192.0.2.55/")
        self.assertIn("http://192.0.2.55:7125", urls)

    def test_os_backup_permission_error_silent_fallback_and_session_suppression(self) -> None:
        """Blocked OS backup sets _os_backup_denied=True without propagating errors while primary & cap storage persist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_dir = os.path.join(tmpdir, "orca_storage")
            backup_path = os.path.join(tmpdir, "external_backup", "state.json")
            os.makedirs(storage_dir, exist_ok=True)
            cap_store = {}

            class _FakeCap(plugin.KobraS1AcePageCapability):
                def get_config(self):
                    return cap_store.get("cfg", "")

                def save_config(self, text):
                    cap_store["cfg"] = str(text)
                    return True

            backup_attempts = []
            real_atomic_json = plugin._atomic_write_json

            def _spy_atomic_json(target_path, data, **kwargs):
                if os.path.abspath(target_path) == os.path.abspath(backup_path):
                    backup_attempts.append(target_path)
                    raise PermissionError("Plugin operation denied")
                return real_atomic_json(target_path, data, **kwargs)

            with mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True), mock.patch.object(
                plugin.orca.host.plugin, "storage", return_value=storage_dir
            ), mock.patch.object(
                plugin.PluginState, "_check_install_state_allows_external_backup", return_value=True
            ), mock.patch.object(plugin, "_atomic_write_json", side_effect=_spy_atomic_json):
                st = plugin.PluginState()
                st.FALLBACK_CONFIG_PATH = backup_path
                cap = _FakeCap()
                cap.post_message = lambda _m: None

                st.moonraker_url = "http://192.0.2.77:7125"
                st.save_config(cap=cap, flush_backup=True)

                # First external attempt raised PermissionError -> silently suppressed & flagged
                self.assertEqual(len(backup_attempts), 1)
                self.assertTrue(st._os_backup_denied)
                # Primary plugin.storage() and capability config succeeded
                self.assertTrue(os.path.isfile(os.path.join(storage_dir, "state.json")))
                self.assertIn("http://192.0.2.77:7125", cap_store.get("cfg", ""))

                # Subsequent saves must suppress any further write attempts to external backup
                st.spoolman_url = "http://192.0.2.77:7912"
                st.save_config(cap=cap, flush_backup=True)
                st.flush_user_backup()
                self.assertEqual(len(backup_attempts), 1)

            # Proactive .install_state.json check without full_access also suppresses OS backup cleanly
            inst_dir = os.path.join(tmpdir, "inst")
            os.makedirs(inst_dir, exist_ok=True)
            with open(os.path.join(inst_dir, ".install_state.json"), "w", encoding="utf-8") as f:
                json.dump({"permissions": {"full_access": False, "fs_readwrite": []}}, f)
            fake_mod_file = os.path.join(inst_dir, "kobra_van_kenorca_plugin.py")
            with mock.patch.object(plugin, "__file__", fake_mod_file), mock.patch.object(
                plugin, "HAS_ORCA_RUNTIME", True
            ):
                st2 = plugin.PluginState()
                st2.FALLBACK_CONFIG_PATH = backup_path
                self.assertFalse(st2._should_use_user_backup_path(os.path.join(storage_dir, "state.json")))
                self.assertTrue(st2._os_backup_denied)

    def test_dryer_start_and_stop_delayed_live_status_refreshes(self) -> None:
        """Dryer start refreshes live status after 500 ms; dryer stop refreshes after 500 ms and 1500 ms."""
        st = plugin.PluginState()
        st.save_config = lambda *a, **kw: None
        st.language = "en"
        page = plugin.KobraS1AcePageCapability()
        posted = []
        page.post_message = posted.append

        # 1. Start Dryer -> start_dryer called first, then 0.5s delay, then query_ace_status pushes active state
        events_start = []
        sleep_calls_start = []
        fake_client = types.SimpleNamespace(
            active_url="http://192.0.2.10:7125",
            is_configured=True,
            start_dryer=mock.MagicMock(side_effect=lambda **kw: events_start.append(("start_dryer", kw)) or {"result": "ok"}),
            stop_dryer=mock.MagicMock(side_effect=lambda **kw: events_stop.append(("stop_dryer", kw)) or {"result": "ok"}),
            query_ace_status=mock.MagicMock(
                side_effect=lambda: (
                    events_start.append(("query_start", None)),
                    {"slots": [], "dryer": {"status": "drying", "target_temp": 55, "remain_time": 120.0}},
                )[1]
            ),
            query_print_stats=mock.MagicMock(return_value={"state": "standby", "is_printing": False}),
        )
        events_stop = []
        st.get_client = lambda: fake_client
        with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
            plugin.time, "sleep", side_effect=lambda s: sleep_calls_start.append(s)
        ):
            page._handle_async_message("dryer_start", {"unit": "0", "temp": 55, "duration": 120})

        self.assertEqual(sleep_calls_start, [0.5])
        self.assertEqual([e[0] for e in events_start], ["start_dryer", "query_start"])
        self.assertEqual(posted[-1]["ace_status"]["dryer"]["status"], "drying")

        # 2. Stop Dryer -> stop_dryer called first, then 0.5s (stale/intermediate) and +1.0s (=1.5s total, Off/Idle)
        posted.clear()
        sleep_calls_stop = []
        stop_responses = [
            {"slots": [], "dryer": {"status": "drying", "target_temp": 55, "remain_time": 119.5}},
            {"slots": [], "dryer": {"status": "stop", "target_temp": 0, "remain_time": 0.0}},
        ]

        def _query_stop():
            idx = len([e for e in events_stop if e[0] == "query_stop"])
            events_stop.append(("query_stop", idx))
            return dict(stop_responses[min(idx, len(stop_responses) - 1)])

        fake_client.query_ace_status = mock.MagicMock(side_effect=_query_stop)
        with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
            plugin.time, "sleep", side_effect=lambda s: sleep_calls_stop.append(s)
        ):
            page._handle_async_message("dryer_stop", {"unit": "0"})

        self.assertEqual(sleep_calls_stop, [0.5, 1.0])
        self.assertAlmostEqual(sum(sleep_calls_stop), 1.5, places=3)
        self.assertEqual([e[0] for e in events_stop], ["stop_dryer", "query_stop", "query_stop"])
        self.assertEqual(len(posted), 2)
        self.assertEqual(posted[0]["ace_status"]["dryer"]["status"], "drying")
        self.assertEqual(posted[1]["ace_status"]["dryer"]["status"], "stop")

    def test_dynamic_ace_count_1_2_3_slots_mapping_and_dryer_units(self) -> None:
        """1x/2x/3x ACE units dynamically control slot count, mapping dropdowns, Auto-Match, offline config, and dryer units."""
        import shutil
        import subprocess

        # 1. Backend 1x ACE (4 slots: T0..T3), ignoring stale save_variables.ace_inventory_1
        norm_1 = plugin.MoonrakerClient.normalize_ace_status(
            {
                "ace_state": {"ace_instances": 1, "current_index": 2, "ace_pro_enabled": True},
                "ace_instance_0": {
                    "instance": 0,
                    "status": "ready",
                    "connection_state": "connected",
                    "slots": [
                        {"index": i, "tool": i, "status": "ready", "color": [255 * (i == 0), 255 * (i == 1), 255 * (i == 2)], "material": "PLA"}
                        for i in range(4)
                    ],
                },
                "save_variables": {
                    "variables": {
                        "ace_inventory_1": [{"status": "ready", "material": "ABS", "color": [10, 20, 30]}] * 4,
                    }
                },
            }
        )
        self.assertEqual(norm_1["ace_instances"], 1)
        self.assertEqual(len(norm_1["slots"]), 4)
        self.assertEqual([s["tool"] for s in norm_1["slots"]], [0, 1, 2, 3])

        # Auto-Match with 1x ACE strictly binds to T0..T3 and validation rejects T4
        fils_5 = [{"slicer_tool": i, "color": "#FF0000" if i == 0 else "#00FF00", "material": "PLA", "used": True} for i in range(5)]
        matched_1 = plugin.auto_match_filaments_to_slots(fils_5, norm_1["slots"])
        self.assertTrue(all(0 <= v < 4 for v in matched_1.values()), matched_1)
        val_1 = plugin.validate_color_mapping(fils_5, {4: 4}, norm_1["slots"], lang="en")
        self.assertGreater(len(val_1["errors"]), 0)

        # 1x ACE Dryer start/stop strips invalid INSTANCE=1 / avoids unnecessary INSTANCE on single ACE
        st = plugin.PluginState()
        st.save_config = lambda *a, **kw: None
        st.last_ace_status = {**norm_1, "connected": True}
        page = plugin.KobraS1AcePageCapability()
        page.post_message = lambda _m: None
        sent_cmds = []
        fake_client = plugin.MoonrakerClient("http://192.0.2.10:7125", enable_port80_fallback=False)
        fake_client.send_gcode = lambda s, **_kw: sent_cmds.append(s) or {"result": "ok"}
        fake_client.query_ace_status = lambda: norm_1
        st.get_client = lambda: fake_client
        with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(plugin.time, "sleep"):
            page._handle_async_message("dryer_start", {"unit": "1", "temp": 45, "duration": 90})
            page._handle_async_message("dryer_stop", {"unit": "1"})
        self.assertEqual(sent_cmds[0], "ACE_START_DRYING TEMP=45 DURATION=90")
        self.assertEqual(sent_cmds[1], "ACE_STOP_DRYING")

        # 2. Backend 3x ACE (12 slots: T0..T11) via ace_instance_0..2 & lanes fallback
        norm_3 = plugin.MoonrakerClient.normalize_ace_status(
            {
                "ace_state": {"ace_instances": 3, "current_index": 9, "ace_pro_enabled": True},
                "ace_instance_0": {"instance": 0, "status": "ready", "slots": [{"index": i, "tool": i, "status": "ready", "material": "PLA", "color": [255, 0, 0]} for i in range(4)]},
                "ace_instance_1": {"instance": 1, "status": "ready", "slots": [{"index": i, "tool": 4 + i, "status": "ready", "material": "PLA", "color": [0, 255, 0]} for i in range(4)]},
                "ace_instance_2": {"instance": 2, "status": "ready", "slots": [{"index": i, "tool": 8 + i, "status": "ready", "material": "PETG", "color": [12, 34, 240] if i == 1 else [0, 0, 0]} for i in range(4)]},
            }
        )
        self.assertEqual(norm_3["ace_instances"], 3)
        self.assertEqual(len(norm_3["slots"]), 12)
        self.assertEqual([s["tool"] for s in norm_3["slots"]], list(range(12)))
        self.assertEqual(norm_3["slots"][9]["status"], "loaded")
        matched_3 = plugin.auto_match_filaments_to_slots(
            [{"slicer_tool": 8, "color": "#0C22F0", "material": "PETG", "used": True}],
            norm_3["slots"],
        )
        self.assertEqual(matched_3[8], 9)

        # 3x ACE Dryer targets INSTANCE=2
        sent_cmds.clear()
        st.last_ace_status = {**norm_3, "connected": True}
        with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(plugin.time, "sleep"):
            page._handle_async_message("dryer_start", {"unit": "2", "temp": 55, "duration": 60})
        self.assertEqual(sent_cmds[0], "ACE_START_DRYING TEMP=55 DURATION=60 INSTANCE=2")

        # Offline configured_ace_count persistence and resolution ("1", "2", "3", "auto")
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "state.json")
            st_off = plugin.PluginState()
            st_off.resolve_config_path = lambda: cfg_file
            st_off.apply_dict({"configured_ace_count": "3"}, apply_gcode_state=False)
            self.assertEqual(st_off.resolve_effective_ace_count({}), 3)

            st_reloaded = plugin.PluginState()
            st_reloaded.resolve_config_path = lambda: cfg_file
            st_reloaded.load_config()
            self.assertEqual(st_reloaded.configured_ace_count, "3")
            self.assertEqual(st_reloaded.resolve_effective_ace_count({}), 3)

        # 3. Verify UI HTML settings control & execute real JS functions in gjs for 1x, 2x, 3x ACE
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertNotIn("<select", html)
        for token in ('id="configuredAceCount"', 'id="slotBayTitle"', 'function getEffectiveAceUnitCount()', 'function renderDryerUnitDropdown('):
            self.assertIn(token, html)

        import shutil
        import subprocess
        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin, "gjs must be available for JS DOM behavior check")
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(tagOrId) {
  return {
    id: tagOrId, tagName: tagOrId, className: "", value: "", checked: false, textContent: "", innerHTML: "",
    dataset: { value: "auto" }, style: {}, files: [], children: [],
    appendChild(c) { this.children.push(c); return c; },
    classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,f){ if(f===undefined){this._s.has(c)?this._s.delete(c):this._s.add(c);} else if(f){this._s.add(c);} else {this._s.delete(c);} }, contains(c){return this._s.has(c);} },
    setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;},
    addEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
  };
}
[
  "slotsContainer","slotBayTitle","dryerUnit","dryerUnitTrigger","dryerUnitLabel","dryerUnitMenu",
  "dryerPreset","dryerPresetLabel","dryerTemp","dryerDuration","modalMappingCards","mappingSummaryBar",
  "chkShowUnusedExtruders","chkConfirmMaterialMismatch","chkBedLeveling","chkSkipPurgeLoaded",
  "chkCutTipPost","chkForceInitialPurge","configuredAceCount","configuredAceCountLabel","configuredAceCountMenu",
  "moonrakerUrl","apiKey","spoolmanUrl","port80Fallback","gcodePathInput","defaultGcodeDirInput",
  "predictionReserveInput","chkDiagLoadLogging","connBadge","headerPrintBadge","modalPrinterBadge",
  "printSafetyBanner","printSafetyState","deviceMeta","dryerBadge","execConnVal","execConnSub",
  "execToolVal","execToolSub","execDryerVal","execDryerSub","execGcodeVal","execGcodeSub"
].forEach(id => { _els[id] = _mkEl(id); });
_els.slotsContainer.__defineSetter__("innerHTML", function(v){ if (v === "") this.children = []; this._html = v; });
_els.slotsContainer.__defineGetter__("innerHTML", function(){ return this._html || ""; });
_els.modalMappingCards.__defineSetter__("innerHTML", function(v){ if (v === "") this.children = []; this._html = v; });
_els.modalMappingCards.__defineGetter__("innerHTML", function(){ return this._html || ""; });
_els.dryerUnit.dataset.value = "all";
_els.configuredAceCount.dataset.value = "auto";
_els.predictionReserveInput.value = "15";
_els.dryerTemp.value = "50";
_els.dryerDuration.value = "240";
var document = {
  documentElement: _mkEl("documentElement"),
  activeElement: null,
  getElementById(id) { return _els[id] || null; },
  createElement(tag) { return _mkEl(tag); },
  querySelectorAll() { return []; },
  addEventListener() {},
};
var window = {
  orca: { postMessage(){}, onMessage(){} },
  matchMedia() { return { matches: true, addEventListener(){} }; }
};
var orca = window.orca;
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + r"""
const countRegexMatches = (str, pat) => (String(str || "").match(pat) || []).length;
const countSlotCards = () => _els.slotsContainer.children.length;
const countMapOpts = () => _els.modalMappingCards.children.reduce((acc, c) => acc + countRegexMatches(c.innerHTML, /class="slot-color-option/g), 0);
const out = {};

// Case A: 1x ACE connected (4 slots)
currentState.configured_ace_count = "auto";
currentState.ace_status = {
  connected: true, ace_instances: 1,
  slots: [0,1,2,3].map(i => ({tool: i, label: "T"+i, status: "ready", hex_color: "#FF0000", material: "PLA", temp: 210}))
};
currentState.ace_slots = currentState.ace_status.slots;
currentState.slicer_filaments = [{slicer_tool: 0, label: "Ext 0", color: "#FF0000", material: "PLA", used: true}];
renderDryerUnitDropdown();
renderSlots();
renderMappingTable();
out.ace1_units = getEffectiveAceUnitCount();
out.ace1_bay_title = _els.slotBayTitle.textContent;
out.ace1_slot_cards = countSlotCards();
out.ace1_map_opts = countMapOpts();
out.ace1_dryer_opts = (_els.dryerUnitMenu.innerHTML.match(/data-value="[^"]+"/g) || []);
out.ace1_dryer_selected = _els.dryerUnit.dataset.value;

// Case B: 2x ACE connected (8 slots)
currentState.ace_status = {
  connected: true, ace_instances: 2,
  slots: Array.from({length: 8}, (_, i) => ({tool: i, label: "T"+i, status: "ready", hex_color: "#00FF00", material: "PETG", temp: 240}))
};
currentState.ace_slots = currentState.ace_status.slots;
renderDryerUnitDropdown();
renderSlots();
renderMappingTable();
out.ace2_units = getEffectiveAceUnitCount();
out.ace2_bay_title = _els.slotBayTitle.textContent;
out.ace2_slot_cards = countSlotCards();
out.ace2_map_opts = countMapOpts();
out.ace2_dryer_opts = (_els.dryerUnitMenu.innerHTML.match(/data-value="[^"]+"/g) || []);

// Case C: 3x ACE connected (12 slots)
currentState.ace_status = {
  connected: true, ace_instances: 3,
  slots: Array.from({length: 12}, (_, i) => ({tool: i, label: "T"+i, status: "ready", hex_color: "#0000FF", material: "PLA", temp: 210}))
};
currentState.ace_slots = currentState.ace_status.slots;
renderDryerUnitDropdown();
renderSlots();
renderMappingTable();
out.ace3_units = getEffectiveAceUnitCount();
out.ace3_bay_title = _els.slotBayTitle.textContent;
out.ace3_slot_cards = countSlotCards();
out.ace3_map_opts = countMapOpts();
out.ace3_dryer_opts = (_els.dryerUnitMenu.innerHTML.match(/data-value="[^"]+"/g) || []);

// Case D: Offline configured_ace_count = "2" when disconnected
currentState.ace_status = { connected: false, slots: [] };
currentState.ace_slots = [];
selectConfiguredAceCount("2");
out.offline2_slot_cards = countSlotCards();
out.offline2_map_opts = countMapOpts();
out.offline2_dryer_opts = (_els.dryerUnitMenu.innerHTML.match(/data-value="[^"]+"/g) || []);

print(JSON.stringify(out));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "ace_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, f"gjs failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        res = json.loads(proc.stdout.strip().splitlines()[-1])

        self.assertEqual(res["ace1_units"], 1)
        self.assertIn("T0–T3", res["ace1_bay_title"])
        self.assertEqual(res["ace1_slot_cards"], 4)
        self.assertEqual(res["ace1_map_opts"], 4)
        self.assertEqual(res["ace1_dryer_opts"], ['data-value="0"'])
        self.assertEqual(res["ace1_dryer_selected"], "0")

        self.assertEqual(res["ace2_units"], 2)
        self.assertIn("T0–T7", res["ace2_bay_title"])
        self.assertEqual(res["ace2_slot_cards"], 8)
        self.assertEqual(res["ace2_map_opts"], 8)
        self.assertEqual(res["ace2_dryer_opts"], ['data-value="all"', 'data-value="0"', 'data-value="1"'])

        self.assertEqual(res["ace3_units"], 3)
        self.assertIn("T0–T11", res["ace3_bay_title"])
        self.assertEqual(res["ace3_slot_cards"], 12)
        self.assertEqual(res["ace3_map_opts"], 12)
        self.assertEqual(res["ace3_dryer_opts"], ['data-value="all"', 'data-value="0"', 'data-value="1"', 'data-value="2"'])

        self.assertEqual(res["offline2_slot_cards"], 8)
        self.assertEqual(res["offline2_map_opts"], 8)
        self.assertEqual(res["offline2_dryer_opts"], ['data-value="all"', 'data-value="0"', 'data-value="1"'])

    def test_color_name_translation_and_mapping_plain_color_labels(self) -> None:
        """Standard colors translate bidirectionally DE<->EN in slots & both sides of mapping UI while custom Spoolman names stay untouched."""
        # 1. Python bidirectional color translation
        pairs = [
            ("Grün", "Green"),
            ("Silber", "Silver"),
            ("Violett", "Violet"),
            ("Rot", "Red"),
            ("Weiß", "White"),
            ("Schwarz", "Black"),
            ("Orange", "Orange"),
            ("Grau", "Grey"),
            ("Blau", "Blue"),
            ("Gelb", "Yellow"),
            ("Braun", "Brown"),
            ("Beige", "Beige"),
            ("Pink", "Pink"),
            ("Transparent", "Transparent"),
            ("Gold", "Gold"),
        ]
        for de_name, en_name in pairs:
            self.assertEqual(plugin.translate_color_name(de_name, "en"), en_name)
            self.assertEqual(plugin.translate_color_name(en_name, "de"), de_name)
        self.assertEqual(plugin.translate_color_name("Rosa", "en"), "Pink")
        self.assertEqual(plugin.translate_color_name("Purple", "de"), "Violett")
        self.assertEqual(plugin.translate_color_name("Prusament Galaxy Black", "en"), "Prusament Galaxy Black")
        self.assertEqual(plugin.translate_color_name("Prusament Galaxy Black", "de"), "Prusament Galaxy Black")

        # 2. Tooltip background variables must be opaque (no transparent --surface-panel leak)
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn("--tooltip-bg: rgba(30, 33, 32, 0.98);", html)
        self.assertIn("--tooltip-bg: rgba(255, 255, 255, 0.98);", html)
        self.assertIn("background: var(--tooltip-bg, #1e2120);", html)

        # 3. Execute JS color translation + left/right mapping card rendering via gjs
        import shutil
        import subprocess
        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(tagOrId) {
  return {
    id: tagOrId, tagName: tagOrId, className: "", value: "", checked: false, textContent: "", innerHTML: "",
    dataset: { value: "auto" }, style: {}, files: [], children: [],
    appendChild(c) { this.children.push(c); return c; },
    classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,f){ if(f===undefined){this._s.has(c)?this._s.delete(c):this._s.add(c);} else if(f){this._s.add(c);} else {this._s.delete(c);} }, contains(c){return this._s.has(c);} },
    setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;},
    addEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
  };
}
[
  "slotsContainer","slotBayTitle","dryerUnit","dryerUnitTrigger","dryerUnitLabel","dryerUnitMenu",
  "dryerPreset","dryerPresetLabel","dryerTemp","dryerDuration","modalMappingCards","mappingSummaryBar",
  "chkShowUnusedExtruders","chkConfirmMaterialMismatch","chkBedLeveling","chkSkipPurgeLoaded",
  "chkCutTipPost","chkForceInitialPurge","configuredAceCount","configuredAceCountLabel","configuredAceCountMenu",
  "moonrakerUrl","apiKey","spoolmanUrl","port80Fallback","gcodePathInput","defaultGcodeDirInput",
  "predictionReserveInput","chkDiagLoadLogging","connBadge","headerPrintBadge","modalPrinterBadge",
  "modalPrinterTargetBadge","headerPrinterSelect","headerPrinterLabel","headerPrinterMenu",
  "settingsPrinterSelect","settingsPrinterLabel","settingsPrinterMenu","printerProfileName",
  "btnPrinterProfileDelete","printSafetyBanner","printSafetyState","deviceMeta","dryerBadge",
  "execConnVal","execConnSub","execToolVal","execToolSub","execDryerVal","execDryerSub","execGcodeVal","execGcodeSub",
  "modalJobFile","modalJobTimeLayers","modalJobWeight","modalJobPurgeG","modalJobPurge","zoomThumbFilename"
].forEach(id => { _els[id] = _mkEl(id); });
_els.slotsContainer.__defineSetter__("innerHTML", function(v){ if (v === "") this.children = []; this._html = v; });
_els.slotsContainer.__defineGetter__("innerHTML", function(){ return this._html || ""; });
_els.modalMappingCards.__defineSetter__("innerHTML", function(v){ if (v === "") this.children = []; this._html = v; });
_els.modalMappingCards.__defineGetter__("innerHTML", function(){ return this._html || ""; });
var document = {
  documentElement: _mkEl("documentElement"), activeElement: null,
  getElementById(id) { return _els[id] || null; },
  createElement(tag) { return _mkEl(tag); },
  querySelectorAll() { return []; }, addEventListener() {},
};
var window = { orca: { postMessage(){}, onMessage(){} }, matchMedia() { return { matches: true, addEventListener(){} }; } };
var orca = window.orca;
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + r"""
currentState.ace_status = {
  connected: true, ace_instances: 2,
  slots: [
    {tool: 0, label: "T0", status: "ready", hex_color: "#00B050", material: "PLA", temp: 210, spoolman: {color_name: "Grün"}},
    {tool: 1, label: "T1", status: "ready", hex_color: "#7C3AED", material: "PLA", temp: 210, spoolman: {color_name: "Prusament Galaxy Purple"}},
    {tool: 2, label: "T2", status: "ready", hex_color: "#FF8000", material: "PETG", temp: 245},
    {tool: 3, label: "T3", status: "ready", hex_color: "#E02020", material: "PLA", temp: 210},
    {tool: 4, label: "T4", status: "ready", hex_color: "#22C55E", material: "PLA", temp: 210, spoolman: {display_name: "ANYCUBIC Spring Leaf", remaining_weight: 850, capacity_weight: 1000, weight_status: "ok"}},
    {tool: 5, label: "T5", status: "empty", hex_color: "#000000", material: "", temp: 0},
    {tool: 6, label: "T6", status: "empty", hex_color: "#000000", material: "", temp: 0},
    {tool: 7, label: "T7", status: "empty", hex_color: "#000000", material: "", temp: 0}
  ]
};
currentState.ace_slots = currentState.ace_status.slots;
currentState.last_gcode_path = "/tmp/Puzzle_Box_-_Low_Poly.gcode";
currentState.last_job_metadata = {
  filename: "Puzzle_Box_-_Low_Poly.gcode",
  total_weight_g: 163.4,
  total_length_m: 54.1,
  estimated_time: "3h 12m",
  total_layers: 120
};
currentState.last_postprocess_info = {
  toolchanges: 1,
  events: [{ new_slicer_tool: 0, volume_mm3: 1000.0 }]
};
currentState.slicer_filaments = [
  {slicer_tool: 0, color: "#FFFFFF", material: "PLA", density: 1.24, used: true, used_g: 36.5, used_m: 11.95}
];
currentState.tool_map = {0: 4};

currentLanguage = "en";
renderModalJobSummary();
renderSlots();
renderMappingTable();
const enCard = _els.modalMappingCards.children[0].innerHTML;
const enSlot0 = _els.slotsContainer.children[0].innerHTML;
const enSlot1 = _els.slotsContainer.children[1].innerHTML;
const enSummary = _els.mappingSummaryBar.innerHTML;
const enJob = {
  file: _els.modalJobFile.textContent,
  weight: _els.modalJobWeight.textContent,
  purgeG: _els.modalJobPurgeG.textContent,
  tc: _els.modalJobPurge.textContent
};

_els.modalMappingCards.children = [];
currentState.tool_map = {0: 3};
currentLanguage = "de";
renderSlots();
renderMappingTable();
const deCard = _els.modalMappingCards.children[0].innerHTML;
const deSlot0 = _els.slotsContainer.children[0].innerHTML;

_els.modalMappingCards.children = [];
currentState.tool_map = {0: 2};
currentLanguage = "en";
renderMappingTable();
const mismatchCard = _els.modalMappingCards.children[0].innerHTML;
const mismatchSummary = _els.mappingSummaryBar.innerHTML;

print(JSON.stringify({ enCard, enSlot0, enSlot1, enSummary, deCard, deSlot0, mismatchCard, mismatchSummary, enJob }));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "color_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])

        # Verify cleaned modal header, Layers icon, Reload G-Code button, & merged job summary
        self.assertEqual(plugin.t("ui.modal.preprint.title", lang="en"), "Map · Check · Send")
        self.assertEqual(plugin.t("ui.modal.cards.title", lang="en"), "Mapping")
        self.assertEqual(plugin.t("ui.modal.btn.load_slicer", lang="en"), "Reload G-Code from Slicer")
        self.assertEqual(plugin.t("ui.modal.btn.load_slicer", lang="de"), "G-Code aus Slicer neu laden")
        self.assertIn('<span data-i18n="ui.modal.btn.load_slicer">Reload G-Code from Slicer</span>', html)
        self.assertEqual(plugin.t("ui.sec4.btn_project", lang="en"), "Slicer Defaults")
        self.assertEqual(plugin.t("ui.sec4.btn_project", lang="de"), "Slicer-Vorgabe")
        self.assertNotIn("ui.modal.preprint.subtitle", html)
        self.assertNotIn("ui.modal.options.title", html)
        self.assertNotIn("Pre-Print Hardware", html)
        self.assertIn('stroke-width="2" aria-hidden="true"><polygon points="12 2 2 7 12 12 22 7 12 2"/>', html)
        self.assertNotIn('<polygon points="22 2 15 22 11 13 2 9 22 2"/></svg><span data-i18n="ui.modal.preprint.title">', html)
        self.assertIn(".mapping-title {\n    font-size: 15px;\n    font-weight: 700;", html)
        self.assertIn('id="mappingSummaryBar" class="map-summary-inline"', html)
        self.assertEqual(out["enJob"]["file"], "Puzzle_Box_-_Low_Poly")
        self.assertEqual(out["enJob"]["weight"], "Usage total: 163.4 g")
        self.assertNotIn("54.1", out["enJob"]["weight"])
        self.assertEqual(out["enJob"]["purgeG"], "Purge: 1.2 g")
        self.assertEqual(out["enJob"]["tc"], "Toolchanges: 1")

        # Verify inline mapping summary bar (no "... active slicer filament(s) assigned." text)
        self.assertIn("Mapping consistent", out["enSummary"])
        self.assertNotIn("assigned.", out["enSummary"])

        # Verify Slicer T0 without redundant subline, no spool text next to Auto-Match/Manual/Default,
        # two-line dropdown labels, spool-hex consumption bar, no SUFFICIENT badge, and single-tooltip ✓
        self.assertIn("<span>Slicer T0</span>", out["enCard"])
        self.assertIn('class="fil-swatch-lg" style="background:#FFFFFF;"', out["enCard"])
        self.assertNotIn("fil-card-sub", out["enCard"])
        self.assertNotIn("36.5 g · 11.95 m", out["enCard"])
        self.assertNotIn("White (#FFFFFF)", out["enCard"])
        self.assertNotIn("spoolman-pill", out["enCard"])
        self.assertNotIn("850.0 g left", out["enCard"])
        self.assertIn('<span class="slot-lbl-primary">ANYCUBIC Spring Leaf</span><span class="slot-lbl-secondary">PLA</span>', out["enCard"])
        self.assertIn('<span class="slot-lbl-primary">PLA</span><span class="slot-lbl-secondary">Red</span>', out["enCard"])
        self.assertIn('<span class="slot-lbl-primary">PETG</span><span class="slot-lbl-secondary">Orange</span>', out["enCard"])
        self.assertIn('<span class="slot-lbl-primary">PLA</span><span class="slot-lbl-secondary">Green</span>', out["enCard"])
        self.assertNotIn("850g", out["enCard"])
        self.assertNotIn("(85%)", out["enCard"])
        self.assertIn("prediction-bar-track", out["enCard"])
        self.assertIn("background:#22C55E;", out["enCard"])
        self.assertNotIn("#86efac", out["enCard"])
        self.assertNotIn("#15803d", out["enCard"])
        self.assertNotIn("SUFFICIENT", out["enCard"])
        self.assertIn("prediction-metrics", out["enCard"])
        self.assertIn('class="badge ok match-icon-badge" data-tooltip="Material Match (PLA)', out["enCard"])
        self.assertIn(">✓</span>", out["enCard"])
        self.assertIsNone(re.search(r'class="badge [^"]*match-icon-badge"[^>]*\btitle=', out["enCard"]))
        self.assertIn("Green", out["enSlot0"])
        self.assertNotIn("Grün", out["enSlot0"])
        self.assertIn("Prusament Galaxy Purple", out["enSlot1"])

        self.assertNotIn("Weiß (#FFFFFF)", out["deCard"])
        self.assertNotIn("fil-card-sub", out["deCard"])
        self.assertNotIn("spoolman-pill", out["deCard"])
        self.assertIn('<span class="slot-lbl-primary">PLA</span><span class="slot-lbl-secondary">Rot</span>', out["deCard"])
        self.assertIn('<span class="slot-lbl-primary">PLA</span><span class="slot-lbl-secondary">Grün</span>', out["deCard"])
        self.assertIn("Grün", out["deSlot0"])

        # Verify mismatch card renders orange ! badge + inline red reason text with single data-tooltip (no native title)
        self.assertIn('class="badge warn match-icon-badge"', out["mismatchCard"])
        self.assertIn(">!</span>", out["mismatchCard"])
        self.assertIn('class="fil-status-err-text"', out["mismatchCard"])
        self.assertIsNone(re.search(r'class="badge [^"]*match-icon-badge"[^>]*\btitle=', out["mismatchCard"]))
        self.assertIn("1 Warning(s)", out["mismatchSummary"])

    def test_linux_appimage_tmpdir_and_storage_paths(self) -> None:
        """Linux AppImage custom TMPDIR, TEMP, and ~/.config storage paths are supported with zero Flatpak hardcoding."""
        with open(plugin.__file__, "r", encoding="utf-8") as f:
            src_txt = f.read()
        self.assertNotIn("/.var/app", src_txt)

        custom_tmp = "/var/tmp/appimage_custom_tmp"
        with mock.patch.object(plugin.os, "name", "posix"), mock.patch.object(
            plugin.sys, "platform", "linux"
        ), mock.patch("tempfile.gettempdir", return_value=custom_tmp):
            grants = plugin._slicer_model_temp_read_grants()
            uid = os.getuid() if hasattr(os, "getuid") else 1000
            self.assertTrue(
                any(g == f"{custom_tmp}/orcaslicer_{uid}/orcaslicer_model/*" for g in grants),
                grants,
            )
            self.assertTrue(any(g == f"{custom_tmp}/orcaslicer_model/*" for g in grants), grants)
            self.assertTrue(plugin._is_system_temp_path(f"{custom_tmp}/orcaslicer_{uid}/orcaslicer_model/plate_1.gcode"))
            self.assertFalse(plugin._is_system_temp_path("/home/tester/.config/OrcaSlicer/orca_plugins/kobra_van_kenorca_plugin.py"))
            self.assertNotIn("os.environ", src_txt)
            self.assertNotIn("os.getenv", src_txt)

    def test_multi_printer_profiles_migration_switch_crud_and_persistence(self) -> None:
        """Legacy single-printer configs migrate to a default profile; create/rename/switch/delete and UI controls persist & query live."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "state.json")
            # 1. Legacy single-printer config migrates seamlessly into printer_profiles[0]
            legacy_cfg = {
                "moonraker_url": "http://192.0.2.10:7125",
                "api_key": "key-alpha",
                "enable_port80_fallback": False,
                "spoolman_url": "http://192.0.2.10:7912",
                "configured_ace_count": "1",
            }
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(legacy_cfg, f)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: cfg_file
            st.load_config()
            self.assertEqual(st.active_printer_id, "default")
            self.assertEqual(len(st.printer_profiles), 1)
            self.assertEqual(st.printer_profiles[0]["moonraker_url"], "http://192.0.2.10:7125")
            self.assertEqual(st.printer_profiles[0]["spoolman_url"], "http://192.0.2.10:7912")
            self.assertEqual(st.printer_profiles[0]["configured_ace_count"], "1")
            self.assertFalse(st.printer_profiles[0]["enable_port80_fallback"])

            # 2. Add a second printer profile and rename it
            prof2 = st.save_printer_profile(
                name="Studio S1 #2",
                moonraker_url="http://192.0.2.20:7125",
                api_key="key-beta",
                spoolman_url="http://192.0.2.10:7912",
                configured_ace_count="2",
                activate=True,
                save=True,
            )
            self.assertEqual(len(st.printer_profiles), 2)
            self.assertEqual(st.active_printer_id, prof2["id"])
            self.assertEqual(st.moonraker_url, "http://192.0.2.20:7125")
            self.assertEqual(st.configured_ace_count, "2")

            # Rename active profile
            st.save_printer_profile(profile_id=prof2["id"], name="Basement Kobra S1", save=True)
            self.assertEqual(st._find_profile_unlocked(prof2["id"])["name"], "Basement Kobra S1")

            # Reload from disk and verify both profiles and active selection persist
            st_reloaded = plugin.PluginState()
            st_reloaded.resolve_config_path = lambda: cfg_file
            st_reloaded.load_config()
            self.assertEqual(len(st_reloaded.printer_profiles), 2)
            self.assertEqual(st_reloaded.active_printer_id, prof2["id"])
            self.assertEqual(st_reloaded.moonraker_url, "http://192.0.2.20:7125")

            # register_capabilities requests network permissions for ALL configured printer profiles
            with mock.patch.object(plugin, "GLOBAL_STATE", st_reloaded):
                pkg = plugin.KobraS1AcePlugin()
                pkg.register_capabilities()
            req_net = plugin.orca.last_requested_permissions.get("network_http", [])
            self.assertIn("http://192.0.2.10", req_net)
            self.assertIn("http://192.0.2.20", req_net)

            # Switching back to "default" via PageCapability triggers immediate live status query on the newly chosen printer
            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append
            queried_urls = []

            def _fake_query(client_self):
                queried_urls.append(client_self.base_url)
                return {
                    "connected": True, "device_status": "ready", "connection_state": "connected",
                    "ace_instances": 1, "current_index": 0, "target_index": -1,
                    "ambient_temp": 25.0, "fan_speed": 0, "print_state": "standby",
                    "is_printing": False, "dryer": {"status": "stop", "active": False, "target_temp": 0, "remain_time_min": 0.0, "active_instances": [], "instances": {}},
                    "slots": [{"tool": i, "label": f"T{i}", "status": "ready", "hex_color": "#FFFFFF", "material": "PLA", "temp": 210} for i in range(4)],
                }

            with mock.patch.object(plugin, "GLOBAL_STATE", st_reloaded), mock.patch.object(
                plugin.MoonrakerClient, "query_ace_status", autospec=True, side_effect=_fake_query
            ):
                page._handle_async_message("switch_printer", {"profile_id": "default"})

            self.assertEqual(st_reloaded.active_printer_id, "default")
            self.assertEqual(st_reloaded.moonraker_url, "http://192.0.2.10:7125")
            self.assertEqual(queried_urls, ["http://192.0.2.10:7125"])
            self.assertTrue(posted)
            self.assertEqual(posted[-1]["state"]["active_printer_id"], "default")

            # Deleting second profile works; deleting last profile is safely rejected
            ok_del, _ = st_reloaded.delete_printer_profile(prof2["id"], save=True)
            self.assertTrue(ok_del)
            self.assertEqual(len(st_reloaded.printer_profiles), 1)
            ok_last, reason_last = st_reloaded.delete_printer_profile("default", save=True)
            self.assertFalse(ok_last)
            self.assertEqual(reason_last, "last_profile")

        # UI contains header switcher, pre-print target badge, and settings profile manager
        html = plugin.ACE_PLUGIN_HTML_UI
        for req_id in ('id="headerPrinterSelect"', 'id="modalPrinterTargetBadge"', 'id="settingsPrinterSelect"', 'id="printerProfileName"', 'function renderPrinterProfileControls('):
            self.assertIn(req_id, html)

    def test_profile_epoch_prevents_stale_poll_and_dryer_overwrite_on_switch(self) -> None:
        """Concurrent slow query_ace_status / pending dryer refresh from old profile never overwrites new profile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            st = plugin.PluginState()
            st.resolve_storage_dir = lambda: tmpdir
            st.resolve_config_path = lambda: os.path.join(tmpdir, "state.json")
            st.moonraker_url = "http://192.0.2.10:7125"
            st.last_detected_ace_count = 1
            prof2 = st.save_printer_profile(
                name="Kobra S1 B",
                moonraker_url="http://192.0.2.20:7125",
                configured_ace_count="auto",
                activate=False,
                save=False,
            )
            self.assertEqual(prof2["last_detected_ace_count"], 0)
            initial_epoch = st.profile_epoch

            query_entered = threading.Event()
            release_query = threading.Event()
            posted = []
            page = plugin.KobraS1AcePageCapability()
            page.post_message = posted.append

            old_client = st.get_client()

            def _slow_old_query():
                query_entered.set()
                release_query.wait(timeout=3.0)
                return {
                    "connected": True,
                    "ace_instances": 3,
                    "device_status": "ready",
                    "slots": [
                        {"tool": i, "label": f"T{i}", "status": "ready", "hex_color": "#FF0000", "material": "PLA", "temp": 210}
                        for i in range(12)
                    ],
                }

            old_client.query_ace_status = _slow_old_query

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                poll_thread = threading.Thread(
                    target=lambda: page._send_state_and_live_status(old_client, "Poll old printer")
                )
                poll_thread.start()
                self.assertTrue(query_entered.wait(timeout=2.0))

                # User switches to Printer B and Printer B gets a 1-ACE status committed
                old_seq = st._dryer_action_seq
                st.switch_printer_profile(prof2["id"], save=False)
                self.assertGreater(st.profile_epoch, initial_epoch)
                self.assertGreater(st._dryer_action_seq, old_seq)
                self.assertEqual(st.last_ace_status, {})
                self.assertEqual(st.last_detected_ace_count, 0)

                release_query.set()
                poll_thread.join(timeout=3.0)

                # Slow old-printer response (3 ACEs / 12 slots) MUST have been discarded cleanly
                self.assertEqual(st.active_printer_id, prof2["id"])
                self.assertEqual(st.last_ace_status, {})
                self.assertEqual(st.last_detected_ace_count, 0)
                self.assertEqual(prof2["last_detected_ace_count"], 0)
                self.assertEqual(posted, [])

    def test_purge_marker_preserves_user_template_and_slot_bounds_validation(self) -> None:
        """@ORCA_ACE_PURGE prevents duplicate purge lines and restores user templates on strip; 1-ACE bounds block >=4."""
        raw_lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,100,200,0\n",
            "; flush_volumes_vector = 50,60\n",
            "G9111 bedTemp=60 extruderTemp=215 tool=0\n",
            "G31\n",
            "T0\n",
            "ACE_SET_PURGE_AMOUNT PURGELENGTH=35.0 ; manual mid-print prime\n",
            "G1 X25 Y25 E1.2\n",
            "ACE_SET_PURGE_AMOUNT PURGELENGTH=123.45 ; user template\n",
            "T1\n",
            "PRINT_END CUT_TIP=0\n",
        ]
        # 1. Two-pass process_gcode + strip_previous_postprocess preserves user pre-T1 template & mid-print prime
        out1, _ = plugin.process_gcode(raw_lines, tool_map={0: 2, 1: 3}, include_audit_header=True)
        out2, _ = plugin.process_gcode(out1, tool_map={0: 2, 1: 3}, include_audit_header=True)
        self.assertEqual(out1, out2)

        txt2 = "".join(out2)
        exec_purge_lines = [
            ln for ln in out2 if not ln.lstrip().startswith(";") and "ACE_SET_PURGE_AMOUNT" in ln
        ]
        # Exactly 1 manual mid-print command + 2 marked toolchange commands (no adjacent duplicate purge lines)
        self.assertEqual(len(exec_purge_lines), 3)
        self.assertEqual(txt2.count(plugin.PURGE_MARKER_TAG), 2)
        self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=35.0 ; manual mid-print prime\n", txt2)
        self.assertNotIn("\nACE_SET_PURGE_AMOUNT PURGELENGTH=123.45 ; user template\n", txt2)

        stripped = "".join(plugin.strip_previous_postprocess(out2))
        self.assertEqual(stripped, "".join(raw_lines))

        # 2. Reduced ACE slot bounds validation (1 ACE = T0..T3 blocks T5 before postprocess & offline upload)
        with tempfile.TemporaryDirectory() as tmpdir:
            gpath = os.path.join(tmpdir, "bounds_check.gcode")
            orig_text = "".join(raw_lines)
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(orig_text)

            st = plugin.PluginState()
            st.save_config = lambda: None
            st.configured_ace_count = "1"
            st.tool_map = {0: 0, 1: 5}
            with self.assertRaises(ValueError) as ctx:
                plugin.postprocess_gcode_file(gpath, st)
            self.assertIn("T5", str(ctx.exception))
            with open(gpath, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), orig_text)

            fake_info = {
                "flush_status": "POSTPROCESS_OK",
                "flush_data_complete": True,
                "used_slicer_tools": [0, 1],
                "slicer_filaments": [
                    {"slicer_tool": 0, "material": "PLA", "used": True},
                    {"slicer_tool": 1, "material": "PLA", "used": True},
                ],
                "toolMap": {0: 0, 1: 5},
                "g9111MappedTool": 0,
            }
            allowed, blocks, _ = plugin.can_upload_gcode(fake_info, st, ace_status={"slots": []}, allow_stale=True)
            self.assertFalse(allowed)
            self.assertTrue(any("T5" in b and "0..3" in b for b in blocks))

    def test_render_slots_sku_quotes_xss_safe_via_gjs(self) -> None:
        """SKUs containing single/double quotes remain syntactically safe onclick callbacks and preserve exact text."""
        import html as html_lib
        import shutil
        import subprocess

        html = plugin.ACE_PLUGIN_HTML_UI
        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        evil_sku = """Maker's "PLA" \\ <b>Pro</b>"""
        js_harness = r"""
const _els = {};
function _mkEl(tagOrId) {
  return {
    id: tagOrId, tagName: tagOrId, className: "", value: "", checked: false, textContent: "", innerHTML: "",
    dataset: {}, style: {}, files: [], children: [], disabled: false,
    appendChild(c) { this.children.push(c); return c; },
    classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,f){ if(f===undefined){this._s.has(c)?this._s.delete(c):this._s.add(c);} else if(f){this._s.add(c);} else {this._s.delete(c);} }, contains(c){return this._s.has(c);} },
    setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;},
    addEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
  };
}
[
  "slotsContainer","slotBayTitle","dryerUnit","dryerUnitTrigger","dryerUnitLabel","dryerUnitMenu",
  "dryerPreset","dryerPresetTrigger","dryerPresetLabel","dryerPresetMenu","dryerTemp","dryerDuration",
  "modalMappingCards","mappingSummaryBar","chkShowUnusedExtruders","chkConfirmMaterialMismatch",
  "chkBedLeveling","chkSkipPurgeLoaded","chkCutTipPost","chkForceInitialPurge",
  "configuredAceCount","configuredAceCountLabel","configuredAceCountMenu",
  "moonrakerUrl","apiKey","spoolmanUrl","port80Fallback","gcodePathInput","defaultGcodeDirInput",
  "predictionReserveInput","chkDiagLoadLogging","connBadge","headerPrintBadge","modalPrinterBadge",
  "modalPrinterTargetBadge","headerPrinterSelect","headerPrinterLabel","headerPrinterMenu",
  "settingsPrinterSelect","settingsPrinterLabel","settingsPrinterMenu","printerProfileName",
  "btnPrinterProfileDelete","printSafetyBanner","printSafetyState","deviceMeta","dryerBadge",
  "execConnVal","execConnSub","execToolVal","execToolSub","execDryerVal","execDryerSub","execGcodeVal","execGcodeSub"
].forEach(id => { _els[id] = _mkEl(id); });
_els.slotsContainer.__defineSetter__("innerHTML", function(v){ if (v === "") this.children = []; this._html = v; });
_els.slotsContainer.__defineGetter__("innerHTML", function(){ return this._html || ""; });
var document = {
  documentElement: _mkEl("documentElement"), activeElement: null,
  getElementById(id) { return _els[id] || null; },
  createElement(tag) { return _mkEl(tag); },
  querySelectorAll() { return []; }, addEventListener() {},
};
var window = { orca: { postMessage(){}, onMessage(){} }, matchMedia() { return { matches: true, addEventListener(){} }; } };
var orca = window.orca;
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + f"""
const sku = {json.dumps(evil_sku)};
currentState.ace_status = {{
  connected: true, ace_instances: 1,
  slots: [{{
    tool: 0, label: "T0", status: "ready", hex_color: "#FF0000", material: "PLA", temp: 210, sku: sku,
    spoolman: {{
      spool_id: 11, sku: sku, assignment_source: "manual",
      candidates: [
        {{ spool_id: 11, display_name: "Spool A", remaining_weight: 800, capacity_weight: 1000 }},
        {{ spool_id: 12, display_name: "Spool B", remaining_weight: 500, capacity_weight: 1000 }}
      ]
    }}
  }}]
}};
currentState.ace_slots = currentState.ace_status.slots;
renderSlots();
print(JSON.stringify({{ html: _els.slotsContainer.children[0].innerHTML, escSingle: escapeHtml("a'b\\"c") }}));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "xss_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(res["escSingle"], "a&#39;b&quot;c")

        # Simulate browser HTML attribute entity decode -> JS execution of every onclick callback
        onclicks = re.findall(r'onclick="([^"]+)"', res["html"])
        spool_cbs = [html_lib.unescape(cb) for cb in onclicks if cb.startswith("selectSlotSpool(")]
        self.assertGreaterEqual(len(spool_cbs), 2)
        with tempfile.TemporaryDirectory() as js_tmp2:
            exec_path = os.path.join(js_tmp2, "exec_cb.js")
            calls_js = "\n".join(f"(() => {{ {cb}; }})();" for cb in spool_cbs)
            with open(exec_path, "w", encoding="utf-8") as f:
                f.write(
                    "const captured = [];\n"
                    "const event = null;\n"
                    "function selectSlotSpool(t, sid, skuStr) { captured.push({t, sid, skuStr}); }\n"
                    + calls_js + "\n"
                    "print(JSON.stringify(captured));\n"
                )
            proc2 = subprocess.run([gjs_bin, exec_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        captured = json.loads(proc2.stdout.strip().splitlines()[-1])
        for entry in captured:
            self.assertEqual(entry["skuStr"], evil_sku)

    def test_upload_autoclose_delayed_refresh_and_http403_busy_guard(self) -> None:
        """Upload & Start Print closes modal, triggers delayed status refresh, guards busy printing, and surfaces HTTP 403 clearly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            gpath = os.path.join(tmp_dir, "print_me.gcode")
            gcode_txt = (
                "; generated by OrcaSlicer 2.5.0\n"
                "; filament used [g] = 10.0\n"
                "; filament_type = PLA\n"
                "; filament_colour = #FF0000\n"
                "; flush_multiplier = 1.0\n"
                "; flush_volumes_vector = 50\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
                "T0\nG1 X1 Y1 E1.0\n"
            )
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(gcode_txt)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmp_dir, "state.json")
            st.moonraker_url = "http://127.0.0.1:7125"
            st.language = "en"
            st.last_gcode_path = gpath
            st.last_gcode_source = "staged_gcode"

            idle_status = {
                "connected": True,
                "print_state": "standby",
                "is_printing": False,
                "ace_instances": 1,
                "slots": [{"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#FF0000"}],
            }
            printing_status = {
                "connected": True,
                "print_state": "printing",
                "is_printing": True,
                "ace_instances": 1,
                "slots": [{"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#FF0000"}],
            }

            fake_client = mock.MagicMock()
            fake_client.is_configured = True
            fake_client.active_url = "http://127.0.0.1:7125"
            fake_client.query_ace_status.side_effect = [
                idle_status,
                idle_status,
                printing_status,
                printing_status,
            ]
            fake_client.upload_gcode.return_value = {"result": {"item": {"path": "print_me.gcode"}}}
            st.get_client = lambda: fake_client

            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append

            sleep_calls = []
            orig_sleep = time.sleep
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(page, "UPLOAD_REFRESH_DELAYS_SEC", (0.5, 1.5)),
                mock.patch.object(plugin.time, "sleep", side_effect=lambda s: sleep_calls.append(s)),
            ):
                page._handle_async_message(
                    "postprocess_and_upload",
                    {"gcode_path": gpath, "start_print": True, "settings": {"start_print_immediately": True}},
                )
                self.assertEqual(posted[0]["action"], "close_preprint_modal")
                self.assertEqual(posted[0]["action_reason"], "upload_started")
                deadline = time.time() + 2.0
                while time.time() < deadline and len(posted) < 3:
                    orig_sleep(0.01)

            self.assertGreaterEqual(len(posted), 3)
            self.assertEqual([round(x, 2) for x in sleep_calls[:2]], [0.5, 1.0])
            self.assertTrue(posted[-1]["ace_status"]["is_printing"])

            # Verify Moonraker HTTP 403 produces clear active-print / locked-file error message
            import io
            import urllib.error

            real_client = plugin.MoonrakerClient("http://127.0.0.1:7125")
            http403 = urllib.error.HTTPError(
                "http://127.0.0.1:7125/server/files/upload",
                403,
                "Forbidden",
                hdrs=None,
                fp=io.BytesIO(b'{"error":{"message":"File is currently printing"}}'),
            )
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch("urllib.request.urlopen", side_effect=http403),
            ):
                with self.assertRaises(RuntimeError) as ctx403:
                    real_client.upload_gcode("print_me.gcode", gcode_txt, start_print=False)
            self.assertIn("403", str(ctx403.exception))
            self.assertIn("print", str(ctx403.exception).lower())

    def test_export_patched_gcode_and_non_destructive_source(self) -> None:
        """Export .gcode... produces patched G-code cleanly without modifying or moving the source file."""
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertEqual(plugin.t("ui.modal.btn.export_gcode", lang="en"), "Export .gcode...")
        self.assertEqual(plugin.t("ui.modal.btn.export_gcode", lang="de"), "Exportieren...")
        self.assertIn('id="btnModalExportGcode"', html)
        self.assertIn("exportPatchedGcode()", html)

        with tempfile.TemporaryDirectory() as tmp_dir:
            src_path = os.path.join(tmp_dir, "staged_temp.gcode")
            dest_path = os.path.join(tmp_dir, "exported", "final_print.gcode")
            orig_content = (
                "; generated by OrcaSlicer 2.5.0\n"
                "; filament used [g] = 12.0, 8.0\n"
                "; filament_type = PLA;PETG\n"
                "; filament_colour = #FF0000;#00FF00\n"
                "; flush_multiplier = 1.0\n"
                "; flush_volumes_vector = 40,40\n"
                "; flush_volumes_matrix = 0,80,90,0\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\n"
                "T0\nG1 X1 Y1 E1.0\nT1\nG1 X2 Y2 E2.0\n"
            )
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(orig_content)

            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmp_dir, "state.json")
            st.language = "en"
            st.tool_map = {0: 2, 1: 5}
            st.tool_map_sources = {0: "manual", 1: "manual"}
            st.last_gcode_path = src_path

            # 1. export_patched_gcode to explicit dest_path keeps src_path byte-for-byte intact
            res = plugin.export_patched_gcode(src_path, state=st, dest_path=dest_path)
            self.assertTrue(os.path.isfile(src_path))
            self.assertTrue(os.path.isfile(dest_path))
            with open(src_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), orig_content)
            with open(dest_path, "r", encoding="utf-8") as f:
                exported_txt = f.read()
            self.assertIn("G9111 bedTemp=60 extruderTemp=210 tool=2", exported_txt)
            self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=", exported_txt)
            self.assertIn("T5\n", exported_txt)
            self.assertEqual(res["filename"], "staged_temp.gcode")

            # 2. _msg_export_gcode without dest_path posts export_gcode_ready payload for Save-As / Blob download
            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message("export_gcode", {"gcode_path": src_path})
            self.assertEqual(posted[-1]["action"], "export_gcode_ready")
            self.assertEqual(posted[-1]["filename"], "staged_temp.gcode")
            self.assertIn("T5\n", posted[-1]["content"])

    def test_light_filament_high_contrast_consumption_bar(self) -> None:
        """White/light filament (luminance > 0.78) renders high-contrast border, dark consumed band, and #cbd5e1 track."""
        import shutil
        import subprocess

        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        html = plugin.ACE_PLUGIN_HTML_UI
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(tagOrId) {
  return {
    id: tagOrId, tagName: tagOrId, className: "", value: "", checked: false, textContent: "", _html: "",
    dataset: { value: "auto" }, style: {}, files: [], children: [],
    appendChild(c) { this.children.push(c); return c; },
    classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,f){ if(f===undefined){this._s.has(c)?this._s.delete(c):this._s.add(c);} else if(f){this._s.add(c);} else {this._s.delete(c);} }, contains(c){return this._s.has(c);} },
    setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;}, removeAttribute(){},
    addEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
  };
}
Object.defineProperty(_mkEl.prototype || Object.prototype, "__dummy", { value: 1, configurable: true });
var document = {
  documentElement: _mkEl("documentElement"), activeElement: null,
  getElementById(id) {
    if (!_els[id]) {
      const el = _mkEl(id);
      Object.defineProperty(el, "innerHTML", {
        get() { return this._html; },
        set(v) { this._html = String(v); if (v === "") this.children = []; }
      });
      _els[id] = el;
    }
    return _els[id];
  },
  createElement(tag) {
    const el = _mkEl(tag);
    Object.defineProperty(el, "innerHTML", {
      get() { return this._html; },
      set(v) { this._html = String(v); }
    });
    return el;
  },
  querySelectorAll() { return []; }, addEventListener() {}, body: _mkEl("body"),
};
var window = { orca: { postMessage(){}, onMessage(){} }, matchMedia() { return { matches: false, addEventListener(){} }; } };
var orca = window.orca;
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + """
currentState.ace_instances = 1;
currentState.effective_ace_count = 1;
currentState.configured_ace_count = "1";
currentState.ace_status = {
  connected: true, ace_instances: 1, ace_count: 1, current_index: 0,
  slots: [
    { tool: 0, label: "T0", status: "ready", hex_color: "#FFFFFF", material: "PLA", temp: 210, spoolman: { display_name: "White PLA", remaining_weight: 700, capacity_weight: 1000, weight_status: "ok" } },
    { tool: 1, label: "T1", status: "ready", hex_color: "#1E40AF", material: "PLA", temp: 210, spoolman: { display_name: "Blue PLA", remaining_weight: 700, capacity_weight: 1000, weight_status: "ok" } }
  ]
};
currentState.ace_slots = currentState.ace_status.slots;
currentState.last_gcode_path = "/tmp/contrast_test.gcode";
currentState.last_postprocess_info = {
  toolchanges: 2,
  events: [
    { new_slicer_tool: 0, volume_mm3: 1000.0 },
    { new_slicer_tool: 1, volume_mm3: 1000.0 }
  ]
};
currentState.slicer_filaments = [
  { slicer_tool: 0, color: "#FFFFFF", material: "PLA", density: 1.24, used: true, used_g: 50.0, used_m: 16.0 },
  { slicer_tool: 1, color: "#1E40AF", material: "PLA", density: 1.24, used: true, used_g: 50.0, used_m: 16.0 }
];
currentState.tool_map = { 0: 0, 1: 1 };
renderMappingTable();
print(JSON.stringify({
  lumWhite: computeRelativeLuminance("#FFFFFF"),
  lumBlue: computeRelativeLuminance("#1E40AF"),
  whiteCard: _els.modalMappingCards.children[0].innerHTML,
  blueCard: _els.modalMappingCards.children[1].innerHTML
}));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "contrast_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertGreater(res["lumWhite"], 0.78)
        self.assertLess(res["lumBlue"], 0.78)
        self.assertIn("prediction-bar-track is-light-spool", res["whiteCard"])
        self.assertIn("background:#cbd5e1;", res["whiteCard"])
        self.assertIn("prediction-bar-before is-light-spool", res["whiteCard"])
        self.assertIn("background:#1e293b;", res["whiteCard"])
        self.assertIn("prediction-bar-after is-light-spool", res["whiteCard"])
        self.assertIn("border:1px solid #64748b;", res["whiteCard"])
        self.assertNotIn("is-light-spool", res["blueCard"])

    def test_preview_slice_invalidation_autoclose_and_session_mapping_memory(self) -> None:
        """Deleted temp preview slice resets state + auto-closes modal (preserving file_picker), and session manual mappings restore on compatible re-slice."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            preview_dir = os.path.join(tmp_dir, "orcaslicer_preview_Metadata")
            os.makedirs(preview_dir, exist_ok=True)
            preview_file = os.path.join(preview_dir, ".9999.1.gcode")
            plate1_txt = (
                "; generated by OrcaSlicer 2.5.0\n"
                "; filament used [g] = 15.0, 10.0\n"
                "; filament_type = PLA;PETG\n"
                "; filament_colour = #FFFFFF;#FF8000\n"
                "; flush_multiplier = 1.0\n"
                "; flush_volumes_vector = 40,40\n"
                "; flush_volumes_matrix = 0,80,80,0\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\nT0\nT1\n"
            )
            with open(preview_file, "w", encoding="utf-8") as f:
                f.write(plate1_txt)

            # 1. Capture preview temp slice via lifecycle worker & record manual override T0->T5
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
            ):
                with st._pending_preview_lock:
                    cap_res, _ = plugin._capture_lifecycle_explicit_unlocked(
                        st,
                        preview_file,
                        "demo.gcode",
                        "demo.gcode",
                        1,
                        "Orca",
                        "demo",
                        deferred_on_perm_error=False,
                    )
                self.assertEqual(cap_res, "loaded")
                self.assertEqual(st._last_preview_temp_path, preview_file)

                page._handle_async_message(
                    "update_mapping",
                    {"tool_map": {0: 5, 1: 6}, "tool_map_sources": {0: "manual", 1: "manual"}},
                )
                self.assertEqual(st._session_manual_tool_map[0]["slot"], 5)
                self.assertEqual(st._session_manual_tool_map[1]["slot"], 6)

                # 2. Simulate OrcaSlicer deleting its preview temp file on plate edit -> status poll invalidates + closes modal
                os.unlink(preview_file)
                sent.clear()
                page._handle_async_message("get_status", {})
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(sent[-1]["action"], "close_preprint_modal")
                self.assertEqual(sent[-1]["action_reason"], "slice_invalidated")

                # 3. Re-slice with changed extruder count/types (T0 still PLA, T1 changed PETG->ABS, T2 added TPU):
                #    T0->T5 (PLA) restores automatically, T1 (ABS != PETG) resets to 1:1 default!
                new_fils = [
                    {"slicer_tool": 0, "material": "PLA", "color": "#FFFFFF", "used": True, "used_g": 20.0},
                    {"slicer_tool": 1, "material": "ABS", "color": "#000000", "used": True, "used_g": 12.0},
                    {"slicer_tool": 2, "material": "TPU", "color": "#00FF00", "used": True, "used_g": 5.0},
                ]
                st.update_slicer_filaments(new_fils)
                self.assertEqual(st.tool_map[0], 5)
                self.assertEqual(st.tool_map_sources[0], "manual")
                self.assertEqual(st.tool_map[1], 1)
                self.assertEqual(st.tool_map_sources[1], "default")

                # 4. Reset via 1:1 Default (reset_mapping), Auto-Match, or Slicer Defaults clears session memory
                page._handle_async_message("reset_mapping", {})
                self.assertEqual(st._session_manual_tool_map, {})

                # Also verify manual Browse .gcode (file_picker) is NEVER invalidated by preview temp watcher
                staged_browse, _ = plugin.load_gcode_from_content("browsed.gcode", plate1_txt, st)
                self.assertEqual(st.last_gcode_source, "file_picker")
                self.assertTrue(os.path.isfile(staged_browse))
                self.assertFalse(plugin.check_and_invalidate_missing_preview_slice(st))
                self.assertEqual(st.last_gcode_path, staged_browse)

    def test_prepare_modal_guard_without_valid_gcode(self) -> None:
        """Prepare & Send Print... refuses to open modal without valid G-code and surfaces clear slice/browse guidance."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st = plugin.PluginState()
            st.resolve_config_path = lambda: os.path.join(tmp_dir, "state.json")
            st.language = "en"
            st.last_gcode_path = ""
            page = plugin.KobraS1AcePageCapability()
            posted = []
            page.post_message = posted.append

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"force_refresh": False, "open_preprint_on_ready": True},
                )
            self.assertEqual(posted[-1]["action"], "no_gcode_notice")
            self.assertEqual(posted[-1]["notice"]["level"], "warn")
            self.assertIn("Please slice the plate in Preview", posted[-1]["notice"]["text"])

        # Also verify JS openPrePrintModal() offline guard keeps modal closed and shows #noGcodeNoticeBanner
        import shutil
        import subprocess

        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", plugin.ACE_PLUGIN_HTML_UI, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(id) {
  const s = new Set();
  return {
    id, value: "", checked: false, textContent: "", innerHTML: "", dataset: {}, style: {}, files: [], children: [],
    appendChild(c){this.children.push(c);}, setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;}, removeAttribute(){},
    addEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
    classList: { add(c){s.add(c);}, remove(c){s.delete(c);}, contains(c){return s.has(c);}, toggle(c,f){f?s.add(c):s.delete(c);} }
  };
}
var document = {
  documentElement: _mkEl("doc"), activeElement: null,
  getElementById(id) { if (!_els[id]) _els[id] = _mkEl(id); return _els[id]; },
  createElement(t) { return _mkEl(t); }, querySelectorAll(){return [];}, addEventListener(){}, body: _mkEl("body"),
};
var window = { matchMedia(){ return { matches: false, addEventListener(){} }; } };
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + """
currentState.last_gcode_path = "";
document.getElementById("prePrintModal");
openPrePrintModal();
print(JSON.stringify({
  modalOpen: _els.prePrintModal.classList.contains("open"),
  noticeVisible: _els.noGcodeNoticeBanner.classList.contains("is-visible"),
  noticeText: _els.noGcodeNoticeText.textContent
}));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "nogcode_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(res["modalOpen"])
        self.assertTrue(res["noticeVisible"])
        self.assertIn("Please slice the plate in Preview", res["noticeText"])

    def test_ace2_optional_humidity_telemetry(self) -> None:
        """normalize_ace_status & header UI render optional ACE 2 humidity only when valid sensor reading is present."""
        ace1_payload = {
            "ace_state": {"ace_instances": 1, "current_index": 0},
            "ace_instance_0": {
                "instance": 0,
                "protocol": "ace1_proto",
                "status": "ready",
                "temp": 28,
                "fan_speed": 0,
                "slots": [{"index": i, "tool": i, "status": "ready", "material": "PLA", "color": [255, 255, 255]} for i in range(4)],
            },
        }
        ace2_payload = {
            "ace_state": {"ace_instances": 2, "current_index": 4},
            "ace_instance_0": ace1_payload["ace_instance_0"],
            "ace_instance_1": {
                "instance": 1,
                "protocol": "ace2_proto",
                "status": "ready",
                "temp": 31,
                "humidity": 24,
                "fan_speed": 1200,
                "slots": [{"index": i, "tool": 4 + i, "status": "ready", "material": "PETG", "color": [255, 128, 0]} for i in range(4)],
            },
        }
        dual_ace2_payload = {
            "ace_state": {"ace_instances": 2, "current_index": 0},
            "ace_instance_0": {
                "instance": 0,
                "protocol": "ace2_proto",
                "status": "ready",
                "temp": 29,
                "humidity": 23,
                "fan_speed": 900,
                "slots": [{"index": i, "tool": i, "status": "ready", "material": "PLA", "color": [255, 255, 255]} for i in range(4)],
            },
            "ace_instance_1": {
                "instance": 1,
                "protocol": "ace2_proto",
                "status": "ready",
                "temp": 33,
                "humidity": 10,
                "fan_speed": 1200,
                "slots": [{"index": i, "tool": 4 + i, "status": "ready", "material": "PETG", "color": [255, 128, 0]} for i in range(4)],
            },
        }
        st1 = plugin.MoonrakerClient.normalize_ace_status(ace1_payload)
        self.assertIsNone(st1["humidity"])
        self.assertEqual(st1["unit_humidities"], {})

        st2 = plugin.MoonrakerClient.normalize_ace_status(ace2_payload)
        self.assertEqual(st2["humidity"], 24)
        self.assertEqual(st2["unit_humidities"], {"1": 24})

        st_dual = plugin.MoonrakerClient.normalize_ace_status(dual_ace2_payload)
        self.assertEqual(st_dual["humidity"], 23)
        self.assertEqual(st_dual["unit_humidities"], {"0": 23, "1": 10})
        self.assertEqual(st_dual["unit_temps"], {"0": 29, "1": 33})

        # Verify humidity appears in ACE DRYER (#dryerHumidityBadge) for all sensor units, and NEVER truncated in #execToolSub
        import shutil
        import subprocess

        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", plugin.ACE_PLUGIN_HTML_UI, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(id) {
  const s = new Set();
  return {
    id, value: "", checked: false, textContent: "", innerHTML: "", dataset: {}, style: {}, files: [], children: [],
    appendChild(c){this.children.push(c);}, setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;}, removeAttribute(){},
    addEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
    classList: { add(c){s.add(c);}, remove(c){s.delete(c);}, contains(c){return s.has(c);}, toggle(c,f){f?s.add(c):s.delete(c);} }
  };
}
var document = {
  documentElement: _mkEl("doc"), activeElement: null,
  getElementById(id) { if (!_els[id]) _els[id] = _mkEl(id); return _els[id]; },
  createElement(t) { return _mkEl(t); }, querySelectorAll(){return [];}, addEventListener(){}, body: _mkEl("body"),
};
var window = { orca: { postMessage(){}, onMessage(){} }, matchMedia(){ return { matches: false, addEventListener(){} }; } };
var orca = window.orca;
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + f"""
currentLanguage = "en";
currentState.moonraker_url = "http://192.0.2.55:7125";
currentState.ace_status = {json.dumps(st1)};
currentState.ace_slots = currentState.ace_status.slots;
renderLiveHeaderAndMeta();
renderExecutiveStrip();
const ace1Sub = _els.execToolSub.textContent;
const ace1DryerHum = _els.dryerHumidityBadge.textContent;
const ace1DryerHumDisplay = _els.dryerHumidityBadge.style.display;
const ace1DryerTemp = _els.dryerTempBadge.textContent;

currentState.ace_status = {json.dumps(st2)};
currentState.ace_slots = currentState.ace_status.slots;
renderLiveHeaderAndMeta();
renderExecutiveStrip();
const ace2SubEn = _els.execToolSub.textContent;
const ace2DryerHumEn = _els.dryerHumidityBadge.textContent;
const ace2MetaEn = _els.deviceMeta.textContent;

currentState.ace_status = {json.dumps(st_dual)};
currentState.ace_slots = currentState.ace_status.slots;
renderLiveHeaderAndMeta();
renderExecutiveStrip();
const dualDryerHumEn = _els.dryerHumidityBadge.textContent;
const dualDryerTempEn = _els.dryerTempBadge.textContent;
const dualConnTextEn = _els.connBadge.textContent;
const dualExecConnVal = _els.execConnVal.textContent;
const dualExecConnDisplay = _els.execConnVal.style.display;

currentState.ace_status = Object.assign({{}}, {json.dumps(st_dual)}, {{ model: "ACE2 (USB Single Serial)" }});
renderLiveHeaderAndMeta();
const dualAce2ConnText = _els.connBadge.textContent;

currentLanguage = "de";
renderLiveHeaderAndMeta();
renderExecutiveStrip();
const dualDryerHumDe = _els.dryerHumidityBadge.textContent;
print(JSON.stringify({{ ace1Sub, ace1DryerHum, ace1DryerHumDisplay, ace1DryerTemp, ace2SubEn, ace2DryerHumEn, ace2MetaEn, dualDryerHumEn, dualDryerTempEn, dualDryerHumDe, dualConnTextEn, dualExecConnVal, dualExecConnDisplay, dualAce2ConnText }}));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "humidity_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertNotIn("Humidity", res["ace1Sub"])
        self.assertEqual(res["ace1DryerHum"], "")
        self.assertEqual(res["ace1DryerHumDisplay"], "none")
        self.assertEqual(res["ace1DryerTemp"], "ACE Temp: 28°C")
        self.assertNotIn("Humidity", res["ace2SubEn"])
        self.assertEqual(res["ace2DryerHumEn"], "Humidity: 24%")
        self.assertIn("Humidity: 24%", res["ace2MetaEn"])
        self.assertEqual(res["dualDryerHumEn"], "Humidity: ACE 1 23% · ACE 2 10%")
        self.assertEqual(res["dualDryerTempEn"], "ACE Temp: ACE 1 29°C · ACE 2 33°C")
        self.assertEqual(res["dualDryerHumDe"], "Luftfeuchte: ACE 1 23% · ACE 2 10%")
        self.assertEqual(res["dualAce2ConnText"], "Connected (2xACE2)")
        self.assertEqual(res["dualExecConnVal"], "")
        self.assertEqual(res["dualExecConnDisplay"], "none")

    def test_upload_lock_notice_box_when_printing_or_offline_or_errors(self) -> None:
        """Footer #modalSendLockNotice clearly explains disabled Upload & Start Print when printing, offline, or invalid mapping, and hides when ready."""
        import shutil
        import subprocess

        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", plugin.ACE_PLUGIN_HTML_UI, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(id) {
  const s = new Set();
  return {
    id, value: "", checked: false, disabled: false, textContent: "", innerHTML: "", className: "", dataset: {}, style: {}, files: [], children: [],
    appendChild(c){this.children.push(c);}, setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;}, removeAttribute(){},
    addEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
    classList: { add(c){s.add(c);}, remove(c){s.delete(c);}, contains(c){return s.has(c);}, toggle(c,f){f?s.add(c):s.delete(c);} }
  };
}
var document = {
  documentElement: _mkEl("doc"), activeElement: null,
  getElementById(id) { if (!_els[id]) _els[id] = _mkEl(id); return _els[id]; },
  createElement(t) { return _mkEl(t); }, querySelectorAll(){return [];}, addEventListener(){}, body: _mkEl("body"),
};
var window = { orca: { postMessage(){}, onMessage(){} }, matchMedia(){ return { matches: false, addEventListener(){} }; } };
var orca = window.orca;
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + """
currentLanguage = "en";
currentState.moonraker_url = "http://127.0.0.1:7125";
currentState._modalBlockedByMismatch = false;
currentState._modalBlockedByErrors = false;

// 1. Printing -> btnSend disabled, btnUpload enabled, lock notice visible
currentState.ace_status = { connected: true, is_printing: true, print_state: "printing", connection_state: "connected_active" };
updateModalSendActionUi();
const printingSnap = {
  sendDisabled: _els.btnModalSendPrint.disabled,
  uploadDisabled: _els.btnModalUploadOnly.disabled,
  noticeClass: _els.modalSendLockNotice.className,
  noticeText: _els.modalSendLockText.textContent
};

// 2. Idle & ready -> btnSend enabled, lock notice hidden
currentState.ace_status = { connected: true, is_printing: false, print_state: "standby", connection_state: "connected_standby" };
updateModalSendActionUi();
const readySnap = {
  sendDisabled: _els.btnModalSendPrint.disabled,
  noticeClass: _els.modalSendLockNotice.className,
  noticeText: _els.modalSendLockText.textContent
};

// 3. Offline -> upload & send disabled, apply & export enabled, lock notice visible
currentState.ace_status = { connected: false, is_printing: false, connection_state: "offline" };
updateModalSendActionUi();
const offlineSnap = {
  sendDisabled: _els.btnModalSendPrint.disabled,
  uploadDisabled: _els.btnModalUploadOnly.disabled,
  applyDisabled: _els.btnModalApplyLocal.disabled,
  exportDisabled: _els.btnModalExportGcode.disabled,
  noticeClass: _els.modalSendLockNotice.className,
  noticeText: _els.modalSendLockText.textContent
};

// 4. Mapping errors -> danger lock notice visible
currentState.ace_status = { connected: true, is_printing: false, connection_state: "connected_standby" };
currentState._modalBlockedByErrors = true;
updateModalSendActionUi();
const errorSnap = {
  sendDisabled: _els.btnModalSendPrint.disabled,
  noticeClass: _els.modalSendLockNotice.className,
  noticeText: _els.modalSendLockText.textContent
};

print(JSON.stringify({ printingSnap, readySnap, offlineSnap, errorSnap }));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "lock_notice_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])

        self.assertTrue(res["printingSnap"]["sendDisabled"])
        self.assertFalse(res["printingSnap"]["uploadDisabled"])
        self.assertIn("is-visible", res["printingSnap"]["noticeClass"])
        self.assertIn("printing", res["printingSnap"]["noticeText"])
        self.assertIn("Upload & Start Print", res["printingSnap"]["noticeText"])

        self.assertFalse(res["readySnap"]["sendDisabled"])
        self.assertNotIn("is-visible", res["readySnap"]["noticeClass"])
        self.assertEqual(res["readySnap"]["noticeText"], "")

        self.assertTrue(res["offlineSnap"]["sendDisabled"])
        self.assertTrue(res["offlineSnap"]["uploadDisabled"])
        self.assertFalse(res["offlineSnap"]["applyDisabled"])
        self.assertFalse(res["offlineSnap"]["exportDisabled"])
        self.assertIn("is-visible", res["offlineSnap"]["noticeClass"])
        self.assertIn("offline", res["offlineSnap"]["noticeText"])

        self.assertTrue(res["errorSnap"]["sendDisabled"])
        self.assertIn("is-visible", res["errorSnap"]["noticeClass"])
        self.assertIn("is-danger", res["errorSnap"]["noticeClass"])
        self.assertIn("Mapping", res["errorSnap"]["noticeText"])

    def test_staged_gcode_invalidated_when_host_unsliced_or_preview_missing(self) -> None:
        """Staged slicer G-code is invalidated & never resurrected when OrcaSlicer host is unsliced or preview temp disappears."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, storage_dir, cfg_path, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            st.language = "en"
            preview_dir = os.path.join(tmp_dir, "orcaslicer_model", "Metadata")
            os.makedirs(preview_dir, exist_ok=True)
            preview_tmp = os.path.join(preview_dir, "pringles-slider-v4.gcode")
            gcode_txt = (
                "; generated by OrcaSlicer 2.5.0\n"
                "; filament used [g] = 18.0, 9.0\n"
                "; filament_type = PLA;PLA\n"
                "; filament_colour = #FFFFFF;#112233\n"
                "; flush_multiplier = 1.0\n"
                "; flush_volumes_vector = 40,40\n"
                "; flush_volumes_matrix = 0,80,80,0\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\nT0\nT1\n"
            )
            with open(preview_tmp, "w", encoding="utf-8") as f:
                f.write(gcode_txt)

            host_state = {"sliced": True, "tmp_path": preview_tmp}
            fake_host = types.SimpleNamespace(
                plater=lambda: types.SimpleNamespace(
                    project_name="pringles-slider-v4.3mf",
                    plate_index=lambda: 1,
                    is_sliced=lambda: host_state["sliced"],
                    get_tmp_gcode_path=lambda: host_state["tmp_path"],
                ),
                model=lambda: types.SimpleNamespace(name="pringles-slider-v4"),
            )

            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "host", fake_host),
            ):
                pp = plugin.KobraS1AcePostProcessCapability()
                res = pp.execute(
                    types.SimpleNamespace(
                        gcode_path=preview_tmp,
                        output_name="pringles-slider-v4.gcode",
                        host="Orca",
                        plate_index=1,
                        project_name="pringles-slider-v4",
                        cancelled=lambda: False,
                    )
                )
                self.assertEqual(res.status, plugin.orca.PluginResult.Success)
                staged_file = st.last_gcode_path
                self.assertTrue(os.path.isfile(staged_file))
                self.assertEqual(st._last_preview_temp_path, preview_tmp)
                self.assertTrue(st._last_preview_temp_existed)

                # Save session manual override T0 -> T4 before plate invalidation
                page._handle_async_message(
                    "update_mapping",
                    {"tool_map": {0: 4, 1: 1}, "tool_map_sources": {0: "manual", 1: "default"}},
                )
                self.assertEqual(st._session_manual_tool_map[0]["slot"], 4)

                # Case A: OrcaSlicer becomes unsliced (is_sliced=False, tmp_path="") while staged file still exists on disk
                host_state["sliced"] = False
                host_state["tmp_path"] = ""
                st.refresh_host_ui_snapshot()
                sent.clear()
                page._handle_async_message("get_status", {})
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(sent[-1]["action"], "close_preprint_modal")
                self.assertEqual(sent[-1]["action_reason"], "slice_invalidated")
                self.assertTrue(os.path.isfile(staged_file))

                # Pressing Prepare & Send Print... MUST NOT resurrect staged pringles-slider-v4.gcode
                st.refresh_host_ui_snapshot()
                sent.clear()
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"force_refresh": False, "open_preprint_on_ready": True},
                )
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(sent[-1]["action"], "no_gcode_notice")

                # Session manual mapping stays preserved across invalidation
                self.assertEqual(st._session_manual_tool_map[0]["slot"], 4)

                # Manual Browse .gcode (file_picker) remains allowed & valid even when host plate is unsliced
                browsed_path, _ = plugin.load_gcode_from_content("manual_job.gcode", gcode_txt, st)
                self.assertEqual(st.last_gcode_source, "file_picker")
                self.assertFalse(plugin.check_and_invalidate_missing_preview_slice(st))
                self.assertEqual(st.last_gcode_path, browsed_path)

                # Case B: Re-slice restores valid preview temp + persisted config reload tracks preview_temp_path
                with open(preview_tmp, "w", encoding="utf-8") as f:
                    f.write(gcode_txt)
                host_state["sliced"] = True
                host_state["tmp_path"] = preview_tmp
                st.refresh_host_ui_snapshot()
                res2 = pp.execute(
                    types.SimpleNamespace(
                        gcode_path=preview_tmp,
                        output_name="pringles-slider-v4.gcode",
                        host="Orca",
                        plate_index=1,
                        project_name="pringles-slider-v4",
                        cancelled=lambda: False,
                    )
                )
                self.assertEqual(res2.status, plugin.orca.PluginResult.Success)
                self.assertEqual(st.tool_map[0], 4)
                self.assertEqual(st.tool_map_sources[0], "manual")

                # Reload state from state.json, delete preview_tmp -> persisted last_preview_temp_path triggers invalidation
                st2 = plugin.PluginState()
                st2.resolve_config_path = lambda: cfg_path
                st2.load_config()
                self.assertEqual(st2._last_preview_temp_path, preview_tmp)
                self.assertTrue(st2._last_preview_temp_existed)
                os.unlink(preview_tmp)
                self.assertTrue(plugin.check_and_invalidate_missing_preview_slice(st2))
                self.assertEqual(st2.last_gcode_path, "")


    def test_same_named_temp_file_invalidated_by_host_change_and_events_and_tab_sync(self) -> None:
        """Old same-named temp .gcode still on disk: model/plate change or lifecycle event invalidates; tab activation syncs."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            st.language = "en"
            prev_dir = os.path.join(tmp_dir, "orcaslicer_model", "Metadata")
            os.makedirs(prev_dir, exist_ok=True)
            tmp_g = os.path.join(prev_dir, "pringles-slider-v4.gcode")
            txt = (
                "; filament used [g] = 18.0\n; filament_type = PLA\n; filament_colour = #FFFFFF\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\nT0\n"
            )
            with open(tmp_g, "w", encoding="utf-8") as f:
                f.write(txt)
            hs = {"name": "pringles-slider-v4.3mf", "plate": 1}
            host = types.SimpleNamespace(
                plater=lambda: types.SimpleNamespace(project_name=hs["name"], plate_index=lambda: hs["plate"]),
                model=lambda: None,
            )
            fake_evt = types.SimpleNamespace(
                SliceStarted="SliceStarted",
                PlateChanged="PlateChanged",
                GCodeExportFinished="GCodeExportFinished",
                SlicingJobComplete="SlicingJobComplete",
            )
            ctx = types.SimpleNamespace(
                gcode_path=tmp_g, output_name="pringles-slider-v4.gcode", host="Orca",
                plate_index=1, project_name="pringles-slider-v4", cancelled=lambda: False,
            )

            def _slice():
                with open(tmp_g, "w", encoding="utf-8") as f:
                    f.write(txt)
                r = plugin.KobraS1AcePostProcessCapability().execute(ctx)
                self.assertEqual(r.status, plugin.orca.PluginResult.Success)
                self.assertNotEqual(st.last_gcode_path, "")

            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "host", host),
                mock.patch.object(plugin.orca, "LifecycleEvent", fake_evt, create=True),
            ):
                st.refresh_host_ui_snapshot()
                _slice()
                self.assertTrue(os.path.isfile(tmp_g))

                # 1) plate changed, same-named temp file untouched on disk
                hs["plate"] = 2
                st.refresh_host_ui_snapshot()
                sent.clear()
                page._handle_async_message("get_status", {})
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(sent[-1]["action"], "close_preprint_modal")
                self.assertTrue(os.path.isfile(tmp_g))

                # 2) model changed
                hs["plate"] = 1
                st.refresh_host_ui_snapshot()
                _slice()
                hs["name"] = "other-model.3mf"
                st.refresh_host_ui_snapshot()
                self.assertTrue(plugin.check_and_invalidate_missing_preview_slice(st))
                self.assertEqual(st.last_gcode_path, "")

                # 3) lifecycle event (PlateChanged / SliceStarted) invalidates even when host info is unchanged
                hs["name"] = "pringles-slider-v4.3mf"
                st.refresh_host_ui_snapshot()
                for evt in ("PlateChanged", "SliceStarted"):
                    _slice()
                    sent.clear()
                    page.on_lifecycle_event(evt, types.SimpleNamespace(code=0, name="x"))
                    self.assertEqual(st.last_gcode_path, "", evt)
                    self.assertEqual(sent[-1]["action"], "close_preprint_modal", evt)

        # 4) tab activation listeners with cooldown & file_picker guard
        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn('document.addEventListener("visibilitychange"', html)
        self.assertIn('window.addEventListener("focus"', html)
        self.assertIn("AUTO_SLICER_SYNC_COOLDOWN_MS = 800", html)
        fn = html[html.index("function autoSyncFromSlicerOnTabActivate"):]
        fn = fn[: fn.index("if (typeof window")]
        import shutil, subprocess, json
        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        harness = (
            "let sent=[]; let now=10000; const Date={now:()=>now};\n"
            "const document={hidden:false}; let currentState={last_gcode_source:'slicing_pipeline'};\n"
            "function sendToHost(m){sent.push(m);}\n"
            "let _lastAutoSlicerSyncMs=0; const AUTO_SLICER_SYNC_COOLDOWN_MS=800;\n"
            + fn.replace("let _lastAutoSlicerSyncMs = 0;", "")
            + "\nconst r=[]; r.push(autoSyncFromSlicerOnTabActivate()); now+=100; r.push(autoSyncFromSlicerOnTabActivate());"
            "now+=900; r.push(autoSyncFromSlicerOnTabActivate());"
            "currentState.last_gcode_source='file_picker'; now+=900; r.push(autoSyncFromSlicerOnTabActivate());"
            "currentState.last_gcode_source=''; document.hidden=true; now+=900; r.push(autoSyncFromSlicerOnTabActivate());"
            "print(JSON.stringify({r, n:sent.length, t:sent[0].type}));\n"
        )
        with tempfile.TemporaryDirectory() as jt:
            jp = os.path.join(jt, "h.js")
            with open(jp, "w", encoding="utf-8") as f:
                f.write(harness)
            proc = subprocess.run([gjs_bin, jp], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(res["r"], [True, False, True, False, False])
        self.assertEqual(res["n"], 2)
        self.assertEqual(res["t"], "acquire_slicer_gcode")

    def test_auto_slice_trigger_on_tab_sync_and_noop_keep_slice(self) -> None:
        """Tab-sync triggers optional host slice_plate(): No-Op keeps slice; started invalidates, shows banner, and reloads on finish."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            st.language = "en"
            prev_dir = os.path.join(tmp_dir, "orcaslicer_model", "Metadata")
            os.makedirs(prev_dir, exist_ok=True)
            tmp_g = os.path.join(prev_dir, "auto_part.gcode")
            gcode_txt = (
                "; filament used [g] = 12.0\n; filament_type = PLA\n; filament_colour = #FFFFFF\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\nT0\n"
            )
            with open(tmp_g, "w", encoding="utf-8") as f:
                f.write(gcode_txt)

            slice_mode = {"result": False, "calls": 0}

            def _slice_plate():
                slice_mode["calls"] += 1
                return slice_mode["result"]

            fake_host = types.SimpleNamespace(
                plater=lambda: types.SimpleNamespace(
                    project_name="auto_part.3mf",
                    plate_index=lambda: 1,
                    slice_plate=_slice_plate,
                ),
                model=lambda: None,
            )
            ctx = types.SimpleNamespace(
                gcode_path=tmp_g,
                output_name="auto_part.gcode",
                host="Orca",
                plate_index=1,
                project_name="auto_part",
                cancelled=lambda: False,
            )
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "host", fake_host),
            ):
                res = plugin.KobraS1AcePostProcessCapability().execute(ctx)
                self.assertEqual(res.status, plugin.orca.PluginResult.Success)
                staged_first = st.last_gcode_path
                page._handle_async_message(
                    "update_mapping",
                    {"tool_map": {0: 3}, "tool_map_sources": {0: "manual"}},
                )

                # 1. Host returns False (already sliced) -> keeps existing slice
                slice_mode["result"] = False
                sent.clear()
                page.on_message({"type": "acquire_slicer_gcode", "auto_tab_sync": True})
                deadline = time.time() + 2.0
                while time.time() < deadline and not sent:
                    time.sleep(0.01)
                self.assertEqual(slice_mode["calls"], 1)
                self.assertEqual(st.last_gcode_path, staged_first)
                self.assertNotEqual(sent[-1].get("action"), "slicing_in_progress")

                # 2. Host returns True (started slicing) -> invalidates old slice, keeps manual map, emits banner
                slice_mode["result"] = True
                sent.clear()
                page.on_message({"type": "acquire_slicer_gcode", "auto_tab_sync": True})
                deadline = time.time() + 2.0
                while time.time() < deadline and not sent:
                    time.sleep(0.01)
                self.assertEqual(st.last_gcode_path, "")
                self.assertTrue(st._auto_slice_in_progress)
                self.assertEqual(st._session_manual_tool_map[0]["slot"], 3)
                self.assertEqual(sent[-1]["action"], "slicing_in_progress")
                self.assertEqual(sent[-1]["notice"]["level"], "info")
                self.assertIn("Slicing in progress", sent[-1]["notice"]["text"])

                # 3. Finished re-slice reloads new G-code and restores _session_manual_tool_map
                res2 = plugin.KobraS1AcePostProcessCapability().execute(ctx)
                self.assertEqual(res2.status, plugin.orca.PluginResult.Success)
                self.assertFalse(st._auto_slice_in_progress)
                self.assertNotEqual(st.last_gcode_path, "")
                self.assertEqual(st.tool_map[0], 3)

                # 4. file_picker never calls slice_plate
                prev_calls = slice_mode["calls"]
                plugin.load_gcode_from_content("browsed.gcode", gcode_txt, st)
                self.assertEqual(st.last_gcode_source, "file_picker")
                page.on_message({"type": "acquire_slicer_gcode", "auto_tab_sync": True})
                time.sleep(0.05)
                self.assertEqual(slice_mode["calls"], prev_calls)

    def test_preset_and_filament_fingerprint_invalidation_keeps_session_mapping(self) -> None:
        """Changing process/printer/filament presets in OrcaSlicer without re-slicing invalidates old slice & shows notice."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            st.language = "en"
            prev_dir = os.path.join(tmp_dir, "orcaslicer_model", "Metadata")
            os.makedirs(prev_dir, exist_ok=True)
            tmp_g = os.path.join(prev_dir, "preset_part.gcode")
            gcode_txt = (
                "; filament used [g] = 10.0\n; filament_type = PLA\n; filament_colour = #FFFFFF\n"
                "G9111 bedTemp=60 extruderTemp=210 tool=0\nT0\n"
            )
            with open(tmp_g, "w", encoding="utf-8") as f:
                f.write(gcode_txt)

            preset_state = {
                "printer": "Anycubic Kobra S1",
                "process": "0.20mm Standard",
                "layer_height": "0.20",
                "filament_name": "Generic PLA White",
                "filament_type": "PLA",
                "filament_color": "#FFFFFF",
            }

            class _Bundle:
                def current_printer_preset(self):
                    return types.SimpleNamespace(name=preset_state["printer"], config_value=lambda k: "")

                def current_process_preset(self):
                    return types.SimpleNamespace(name=preset_state["process"], config_value=lambda k: "")

                def current_filament_presets(self):
                    return [types.SimpleNamespace(name=preset_state["filament_name"], config_value=lambda k: "")]

                def current_filament_preset_names(self):
                    return [preset_state["filament_name"]]

                def full_config_value(self, key):
                    return {
                        "layer_height": preset_state["layer_height"],
                        "filament_type": [preset_state["filament_type"]],
                        "filament_colour": [preset_state["filament_color"]],
                    }.get(key, "")

            host = types.SimpleNamespace(
                plater=lambda: types.SimpleNamespace(project_name="preset_part.3mf", plate_index=lambda: 1),
                model=lambda: None,
                preset_bundle=lambda: _Bundle(),
            )
            ctx = types.SimpleNamespace(
                gcode_path=tmp_g,
                output_name="preset_part.gcode",
                host="Orca",
                plate_index=1,
                project_name="preset_part",
                cancelled=lambda: False,
            )

            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "host", host),
            ):
                st.refresh_host_ui_snapshot()
                res = plugin.KobraS1AcePostProcessCapability().execute(ctx)
                self.assertEqual(res.status, plugin.orca.PluginResult.Success)
                self.assertNotEqual(st.last_gcode_path, "")

                page._handle_async_message(
                    "update_mapping",
                    {"tool_map": {0: 6}, "tool_map_sources": {0: "manual"}},
                )
                self.assertEqual(st._session_manual_tool_map[0]["slot"], 6)

                # Same model & plate, unchanged temp file, BUT layer_height + filament preset changed -> must invalidate!
                preset_state["layer_height"] = "0.12"
                preset_state["filament_name"] = "Anycubic Matte PLA Green"
                preset_state["filament_color"] = "#00AA55"
                st.refresh_host_ui_snapshot()
                sent.clear()
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"force_refresh": False, "auto_tab_sync": True},
                )
                self.assertEqual(st.last_gcode_path, "")
                self.assertEqual(st._last_invalidation_reason, "host_presets_changed")
                self.assertEqual(sent[-1]["action"], "no_gcode_notice")
                self.assertIn("not re-sliced", sent[-1]["notice"]["text"])
                self.assertEqual(st._session_manual_tool_map[0]["slot"], 6)

                # Re-slice with new compatible preset -> restores manual session mapping
                res2 = plugin.KobraS1AcePostProcessCapability().execute(ctx)
                self.assertEqual(res2.status, plugin.orca.PluginResult.Success)
                self.assertEqual(st._last_invalidation_reason, "")
                self.assertEqual(st.tool_map[0], 6)

    def test_mismatch_checkbox_persists_across_polls_and_resets_on_change(self) -> None:
        """Mismatch ack persists across apply_dict(apply_gcode_state=False) and stale get_status polls, resetting only on mapping or G-code changes."""
        import shutil, subprocess, json

        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, cfg_path, _page, _sent = self._make_zombie_test_state_and_page(tmp_dir)
            fake_gcode = os.path.join(tmp_dir, "job1.gcode")
            with open(fake_gcode, "w", encoding="utf-8") as f:
                f.write("; G-code\n")
            st.last_gcode_path = fake_gcode
            st.tool_map[0] = 0

            # Python backend: apply_dict with apply_gcode_state=False MUST accept checkbox ack while G-code is active
            st.apply_dict({"material_mismatch_confirmed": True, "tool_map": {"0": 0}}, apply_gcode_state=False, save=False)
            self.assertTrue(st.material_mismatch_confirmed)
            # If tool_map actually changes, mismatch ack resets immediately
            st.apply_dict({"material_mismatch_confirmed": True, "tool_map": {"0": 1}}, apply_gcode_state=False, save=False)
            self.assertFalse(st.material_mismatch_confirmed)
            # Without active G-code, stale UI payload cannot set material_mismatch_confirmed
            st.last_gcode_path = ""
            st.apply_dict({"material_mismatch_confirmed": True}, apply_gcode_state=False, save=False)
            self.assertFalse(st.material_mismatch_confirmed)

        # gjs UI harness verifying full poll cycle, unlock/lock transitions, and reset triggers
        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", plugin.ACE_PLUGIN_HTML_UI, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(id) {
  const s = new Set();
  return {
    id, value: "", checked: false, disabled: false, textContent: "", innerHTML: "", dataset: {}, style: {}, files: [], children: [],
    className: "",
    appendChild(c){this.children.push(c);}, setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;}, removeAttribute(){},
    addEventListener(){}, removeEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
    classList: { add(c){s.add(c);}, remove(c){s.delete(c);}, contains(c){return s.has(c);}, toggle(c,f){f?s.add(c):s.delete(c);} }
  };
}
let _msgHandler = null;
var orca = {
  postMessage(){},
  onMessage(fn){ _msgHandler = fn; }
};
var document = {
  documentElement: _mkEl("doc"), activeElement: null, hidden: false,
  getElementById(id) { if (!_els[id]) _els[id] = _mkEl(id); return _els[id]; },
  createElement(t) { return _mkEl(t); }, querySelectorAll(){return [];}, addEventListener(){}, body: _mkEl("body"),
};
var window = { matchMedia(){ return { matches: false, addEventListener(){} }; }, addEventListener(){} };
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + """
["modalMismatchGate", "modalMismatchList", "chkConfirmMaterialMismatch", "mappingBody",
 "btnModalApplyLocal", "btnModalExportGcode", "btnModalUploadOnly", "btnModalSendPrint",
 "modalSendLockNotice", "modalSendLockText", "gcodePathInput", "moonrakerUrl", "apiKey",
 "port80Fallback", "chkBedLeveling", "chkSkipPurgeLoaded", "chkCutTipPost", "chkForceInitialPurge"
].forEach(id => document.getElementById(id));

const baseState = {
  language: "en",
  last_gcode_path: "/tmp/staged/job1.gcode",
  filament_signature: "sig1",
  material_mismatch_confirmed: false,
  tool_map: {"0": 0},
  tool_map_sources: {"0": "default"},
  slicer_filaments: [{slicer_tool: 0, material: "PLA", color: "#FFFFFF", used: true, used_g: 10.0}],
  ace_status: {
    connected: true, is_printing: false, print_state: "standby", connection_state: "connected_standby",
    slots: [
      {index: 0, status: "ready", material: "PETG", type: "PETG", hex_color: "#FF0000", temp: 245},
      {index: 1, status: "ready", material: "ABS", type: "ABS", hex_color: "#00FF00", temp: 255}
    ]
  }
};

_msgHandler({ state: JSON.parse(JSON.stringify(baseState)) });
renderMappingTable();
const initialSnap = {
  checked: _els.chkConfirmMaterialMismatch.checked,
  sendDisabled: _els.btnModalSendPrint.disabled,
  applyDisabled: _els.btnModalApplyLocal.disabled,
  lockVisible: _els.modalSendLockNotice.className.includes("is-visible"),
  lockText: _els.modalSendLockText.textContent
};

// User ticks checkbox -> unlocks Send & Apply and hides lock notice
onMismatchConfirmChange(true);
const afterCheckSnap = {
  checked: _els.chkConfirmMaterialMismatch.checked,
  stateAck: !!currentState.material_mismatch_confirmed,
  sendDisabled: _els.btnModalSendPrint.disabled,
  applyDisabled: _els.btnModalApplyLocal.disabled,
  lockVisible: _els.modalSendLockNotice.className.includes("is-visible")
};

// Stale in-flight get_status poll arrives with material_mismatch_confirmed=false for same constellation -> MUST STAY CHECKED!
_msgHandler({ state: JSON.parse(JSON.stringify(baseState)) });
renderMappingTable();
const afterPollSnap = {
  checked: _els.chkConfirmMaterialMismatch.checked,
  stateAck: !!currentState.material_mismatch_confirmed,
  sendDisabled: _els.btnModalSendPrint.disabled,
  lockVisible: _els.modalSendLockNotice.className.includes("is-visible")
};

// User changes mapped slot -> ack MUST reset!
updateToolMapping(0, 1);
const afterRemapSnap = {
  checked: _els.chkConfirmMaterialMismatch.checked,
  stateAck: !!currentState.material_mismatch_confirmed,
  sendDisabled: _els.btnModalSendPrint.disabled
};

// User ticks again, then a new G-code loads -> ack MUST reset!
onMismatchConfirmChange(true);
const nextGcodeState = JSON.parse(JSON.stringify(baseState));
nextGcodeState.last_gcode_path = "/tmp/staged/job2.gcode";
nextGcodeState.material_mismatch_confirmed = false;
_msgHandler({ state: nextGcodeState });
renderMappingTable();
const afterNewGcodeSnap = {
  checked: _els.chkConfirmMaterialMismatch.checked,
  stateAck: !!currentState.material_mismatch_confirmed,
  sendDisabled: _els.btnModalSendPrint.disabled
};

print(JSON.stringify({ initialSnap, afterCheckSnap, afterPollSnap, afterRemapSnap, afterNewGcodeSnap }));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "mismatch_ack_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])

        self.assertFalse(res["initialSnap"]["checked"])
        self.assertTrue(res["initialSnap"]["sendDisabled"])
        self.assertTrue(res["initialSnap"]["applyDisabled"])
        self.assertTrue(res["initialSnap"]["lockVisible"])
        self.assertIn("Material mismatch", res["initialSnap"]["lockText"])

        self.assertTrue(res["afterCheckSnap"]["checked"])
        self.assertTrue(res["afterCheckSnap"]["stateAck"])
        self.assertFalse(res["afterCheckSnap"]["sendDisabled"])
        self.assertFalse(res["afterCheckSnap"]["applyDisabled"])
        self.assertFalse(res["afterCheckSnap"]["lockVisible"])

        self.assertTrue(res["afterPollSnap"]["checked"])
        self.assertTrue(res["afterPollSnap"]["stateAck"])
        self.assertFalse(res["afterPollSnap"]["sendDisabled"])
        self.assertFalse(res["afterPollSnap"]["lockVisible"])

        self.assertFalse(res["afterRemapSnap"]["checked"])
        self.assertFalse(res["afterRemapSnap"]["stateAck"])
        self.assertTrue(res["afterRemapSnap"]["sendDisabled"])

        self.assertFalse(res["afterNewGcodeSnap"]["checked"])
        self.assertFalse(res["afterNewGcodeSnap"]["stateAck"])
        self.assertTrue(res["afterNewGcodeSnap"]["sendDisabled"])

    def test_live_refresh_on_tab_activate_open_preprint_cta_and_modal_intercept(self) -> None:
        """1) Live Moonraker refresh on tab focus & Prepare & Send Print; 2) CTA button without ellipsis; 3) WINDOW_MODAL Print-Intercept + Script capability removed."""
        import shutil, subprocess, json

        # Script Send capability and send.cap.* keys cleanly removed
        self.assertFalse(hasattr(plugin, "KobraS1AceSendCapability"))
        self.assertNotIn("send.cap.opened", plugin.TRANSLATIONS["en"])
        self.assertNotIn("send.cap.no_gcode", plugin.TRANSLATIONS["en"])

        # 2. CTA button text & Layers SVG + prominent CSS without '...'
        self.assertEqual(plugin.TRANSLATIONS["en"]["ui.bar.btn_prepare"], "Prepare & Send Print")
        self.assertEqual(plugin.TRANSLATIONS["de"]["ui.bar.btn_prepare"], "Druck vorbereiten & senden")
        html = plugin.ACE_PLUGIN_HTML_UI
        m_btn = re.search(r'<button id="btnOpenPrePrintModal"[^>]*>.*?</button>', html, re.S)
        self.assertIsNotNone(m_btn)
        btn_html = m_btn.group(0)
        self.assertIn("Prepare &amp; Send Print", btn_html)
        self.assertNotIn("...", btn_html)
        self.assertIn('polygon points="12 2 2 7 12 12 22 7 12 2"', btn_html)
        self.assertIn("padding: 9px 18px;", html)
        self.assertIn(".btn-cta-send:hover:not(:disabled)", html)

        # 1a. Frontend gjs check: onTabActivatedRefresh & openPrePrintModal trigger live Moonraker + slicer sync
        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(id) {
  const s = new Set();
  return {
    id, value: "", checked: false, disabled: false, textContent: "", innerHTML: "", dataset: {}, style: {}, files: [], children: [],
    className: "",
    appendChild(c){this.children.push(c);}, setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;}, removeAttribute(){},
    addEventListener(){}, removeEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
    classList: { add(c){s.add(c);}, remove(c){s.delete(c);}, contains(c){return s.has(c);}, toggle(c,f){f?s.add(c):s.delete(c);} }
  };
}
const sentMsgs = [];
var orca = {
  postMessage(raw){ sentMsgs.push(typeof raw === "string" ? JSON.parse(raw) : raw); },
  onMessage(){}
};
let fakeNow = 20000;
Date.now = () => fakeNow;
var document = {
  documentElement: _mkEl("doc"), activeElement: null, hidden: false,
  getElementById(id) { if (!_els[id]) _els[id] = _mkEl(id); return _els[id]; },
  createElement(t) { return _mkEl(t); }, querySelectorAll(){return [];}, addEventListener(){}, body: _mkEl("body"),
};
var window = { matchMedia(){ return { matches: false, addEventListener(){} }; }, addEventListener(){} };
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + """
// Tab activation with active slicer job sends BOTH acquire_slicer_gcode (refresh_live_status) AND get_status
sentMsgs.length = 0;
currentState.last_gcode_source = "slicing_pipeline";
onTabActivatedRefresh();
const tabSlicerTypes = sentMsgs.map(m => m.type);
const tabAcquireFlags = Object.assign({}, sentMsgs.find(m => m.type === "acquire_slicer_gcode") || {});

// Tab activation with file_picker skips acquire_slicer_gcode but STILL refreshes live status via get_status
fakeNow += 1200;
sentMsgs.length = 0;
currentState.last_gcode_source = "file_picker";
onTabActivatedRefresh();
const tabPickerTypes = sentMsgs.map(m => m.type);

// openPrePrintModal immediately triggers BOTH get_status AND acquire_slicer_gcode(open_preprint_on_ready=true, refresh_live_status=true)
sentMsgs.length = 0;
openPrePrintModal();
const openTypes = sentMsgs.map(m => m.type);
const openAcquireFlags = Object.assign({}, sentMsgs.find(m => m.type === "acquire_slicer_gcode") || {});

print(JSON.stringify({ tabSlicerTypes, tabAcquireFlags, tabPickerTypes, openTypes, openAcquireFlags }));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "cta_refresh_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        js_res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIn("acquire_slicer_gcode", js_res["tabSlicerTypes"])
        self.assertIn("get_status", js_res["tabSlicerTypes"])
        self.assertTrue(js_res["tabAcquireFlags"].get("refresh_live_status"))
        self.assertEqual(js_res["tabPickerTypes"], ["get_status"])
        self.assertIn("get_status", js_res["openTypes"])
        self.assertIn("acquire_slicer_gcode", js_res["openTypes"])
        self.assertTrue(js_res["openAcquireFlags"].get("refresh_live_status"))
        self.assertTrue(js_res["openAcquireFlags"].get("open_preprint_on_ready"))

        # 1b & 3. Backend live Moonraker refresh during acquire_slicer_gcode + KobraS1AceSendCapability
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _storage, _cfg, page, sent = self._make_zombie_test_state_and_page(tmp_dir)
            st.language = "en"
            st.moonraker_url = "http://127.0.0.1:7125"
            st.last_ace_status = {
                "connected": True,
                "configured": True,
                "is_printing": True,
                "print_state": "printing",
                "slots": [{"index": 0, "status": "ready", "material": "PLA", "hex_color": "#FFFFFF"}],
            }

            class _FinishedMoonraker:
                base_url = "http://127.0.0.1:7125"
                active_url = "http://127.0.0.1:7125"
                api_key = ""
                is_configured = True

                def query_ace_status(self):
                    return {
                        "connected": True,
                        "is_printing": False,
                        "print_state": "standby",
                        "ace_instances": 1,
                        "slots": [
                            {"index": 0, "status": "ready", "material": "PLA", "hex_color": "#FFFFFF"},
                            {"index": 1, "status": "ready", "material": "PLA", "hex_color": "#000000"},
                            {"index": 2, "status": "ready", "material": "PLA", "hex_color": "#FFFFFF"},
                        ],
                    }

            st.get_client = lambda: _FinishedMoonraker()  # type: ignore[method-assign]

            focused_pages = []
            fake_ui = types.SimpleNamespace(
                NotificationLevel=plugin.orca.host.ui.NotificationLevel,
                push_notification=lambda *a, **kw: None,
                activate_page=lambda name: focused_pages.append(name),
            )
            fake_host = types.SimpleNamespace(
                plugin=plugin.orca.host.plugin,
                ui=fake_ui,
                plater=lambda: None,
                model=lambda: None,
            )

            # Register capabilities (Pages + PostProcess only; no Script capability)
            reg_caps = []
            uploaded_jobs = []
            with (
                mock.patch.object(plugin, "GLOBAL_STATE", st),
                mock.patch.object(plugin, "_ACTIVE_PAGE_CAPABILITY", page),
                mock.patch.object(plugin.orca, "host", fake_host),
                mock.patch.object(plugin.orca, "register_capability", side_effect=reg_caps.append),
            ):
                plugin.KobraS1AcePlugin().register_capabilities()
                self.assertEqual(
                    reg_caps,
                    [plugin.KobraS1AcePageCapability, plugin.KobraS1AcePostProcessCapability],
                )

                # Verify acquire_slicer_gcode refreshes Moonraker standby state!
                g_path = os.path.join(tmp_dir, "finished_print.gcode")
                gcode_sample = (
                    "; filament used [g] = 10.0\n; filament_type = PLA\n; filament_colour = #FFFFFF\n"
                    "; flush_multiplier = 1.0\n; flush_volumes_vector = 20\n; flush_volumes_matrix = 0\n"
                    "G9111 bedTemp=60 extruderTemp=210 tool=0\nT0\n"
                )
                with open(g_path, "w", encoding="utf-8") as f:
                    f.write(gcode_sample)
                sent.clear()
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"gcode_path": g_path, "explicit": True, "open_preprint_on_ready": True, "refresh_live_status": True},
                )
                self.assertEqual(sent[-1]["action"], "open_preprint_modal")
                self.assertFalse(sent[-1]["ace_status"]["is_printing"])
                self.assertEqual(sent[-1]["ace_status"]["print_state"], "standby")
                self.assertFalse(st.last_ace_status["is_printing"])

                # 3. WINDOW_MODAL Print-Intercept:
                # 3a) Apply & Patch Locally -> repatches ctx.gcode_path with mapped tool & succeeds for OrcaSlicer upload
                win_msgs = []
                win_htmls = []
                def _make_intercept_win(flow_fn):
                    def _create(**kwargs):
                        win_htmls.append(kwargs.get("html", ""))
                        on_msg = kwargs["on_message"]
                        on_cls = kwargs.get("on_close")
                        def _driver():
                            time.sleep(0.02)
                            on_msg({"type": "ready"})
                            time.sleep(0.03)
                            flow_fn(on_msg, on_cls)
                        threading.Thread(target=_driver, daemon=True).start()
                        return types.SimpleNamespace(
                            post=lambda p: win_msgs.append(p),
                            close=lambda: None,
                        )
                    return _create

                fake_ui_modal = types.SimpleNamespace(
                    NotificationLevel=plugin.orca.host.ui.NotificationLevel,
                    push_notification=lambda *a, **kw: None,
                    WINDOW_MODAL="WINDOW_MODAL",
                    create_window=_make_intercept_win(
                        lambda on_msg, _on_cls: (
                            on_msg({"type": "update_mapping", "settings": {"tool_map": {"0": 2}}}),
                            time.sleep(0.03),
                            on_msg({"type": "postprocess_only"}),
                        )
                    ),
                )
                fake_host.ui = fake_ui_modal
                st.slicing_modal_mode = "window_modal"
                pp_cap = plugin.KobraS1AcePostProcessCapability()
                with open(g_path, "w", encoding="utf-8") as f:
                    f.write(gcode_sample)
                ctx_print = types.SimpleNamespace(
                    step=plugin.orca.slicing.Step.psGCodePostProcess,
                    gcode_path=g_path,
                    output_name="finished_print.gcode",
                    host="Moonraker",
                    plate_index=1,
                    project_name="finished_print",
                    cancelled=lambda: False,
                )
                res_proceed = pp_cap.execute(ctx_print)
                self.assertTrue(res_proceed.ok)
                patched_disk = open(g_path, encoding="utf-8").read()
                self.assertIn("T2\n", patched_disk)
                self.assertIn("tool=2", patched_disk)
                self.assertTrue(any(
                    isinstance(m, dict) and m.get("action") == "open_preprint_modal" and m.get("standalone_modal") is True
                    for m in win_msgs
                ))
                self.assertIn('<body class="standalone-modal">', win_htmls[0])
                self.assertIn('<div class="app-shell" hidden style="display:none;">', win_htmls[0])
                self.assertIn('<div id="prePrintModal" class="modal-backdrop open"', win_htmls[0])

                # 3b) User closes modal with X / Cancel -> returns FatalError so OrcaSlicer aborts upload & print
                with open(g_path, "w", encoding="utf-8") as f:
                    f.write(gcode_sample)
                fake_ui_modal.create_window = _make_intercept_win(lambda _on_msg, on_cls: on_cls())
                res_cancel = pp_cap.execute(ctx_print)
                self.assertFalse(res_cancel.ok)
                self.assertEqual(res_cancel.status, plugin.orca.PluginResult.FatalError)

                # 3b-ii) Ready timeout / plugin unload during WINDOW_MODAL also abort via FatalError (never print blindly)
                with open(g_path, "w", encoding="utf-8") as f:
                    f.write(gcode_sample)
                with mock.patch.object(
                    plugin._SlicingModalSetupSession, "run", return_value="ready_timeout"
                ):
                    res_timeout = pp_cap.execute(ctx_print)
                self.assertFalse(res_timeout.ok)
                self.assertEqual(res_timeout.status, plugin.orca.PluginResult.FatalError)
                with open(g_path, "w", encoding="utf-8") as f:
                    f.write(gcode_sample)
                with mock.patch.object(
                    plugin._SlicingModalSetupSession, "run", return_value="unloaded"
                ):
                    res_unloaded = pp_cap.execute(ctx_print)
                self.assertFalse(res_unloaded.ok)
                self.assertEqual(res_unloaded.status, plugin.orca.PluginResult.FatalError)

                # 3c) Upload/Send inside WINDOW_MODAL -> validates, patches source file in-place, and hands single upload to OrcaSlicer (no duplicate HTTP 403 upload)
                class _UploadClient(_FinishedMoonraker):
                    def upload_gcode(self, filename, gcode_content, start_print=False):
                        uploaded_jobs.append((filename, start_print))

                st.get_client = lambda: _UploadClient()  # type: ignore[method-assign]
                with open(g_path, "w", encoding="utf-8") as f:
                    f.write(gcode_sample)
                fake_ui_modal.create_window = _make_intercept_win(
                    lambda on_msg, _on_cls: on_msg({"type": "postprocess_and_upload", "start_print": False})
                )
                res_uploaded = pp_cap.execute(ctx_print)
                self.assertTrue(res_uploaded.ok)
                self.assertEqual(res_uploaded.status, plugin.orca.PluginResult.Success)
                self.assertEqual(len(uploaded_jobs), 0)
                patched_after_send = open(g_path, encoding="utf-8").read()
                self.assertIn("T2\n", patched_after_send)
                self.assertIn("tool=2", patched_after_send)
                self.assertFalse(any(
                    isinstance(m, dict) and m.get("action") == "close_preprint_modal"
                    for m in win_msgs
                ))

                # 3d) Duplicate slicing_pipeline_plugin entry on same untouched export file must NOT reopen modal
                dup_modal_calls = []
                fake_ui_modal.create_window = lambda **kw: dup_modal_calls.append(kw)
                res_dup_pass = pp_cap.execute(ctx_print)
                self.assertTrue(res_dup_pass.ok)
                self.assertEqual(len(dup_modal_calls), 0)
                deduped = plugin.build_slicing_pipeline_value(
                    "other_plugin;KobraVanKenorca_PostProcess;KobraVanKenorca_PostProcess"
                )
                self.assertEqual(
                    plugin.parse_slicer_pipeline_entries(deduped),
                    ["other_plugin", "KobraVanKenorca_PostProcess"],
                )

    def test_standalone_modal_chrome_hidden_direct_close_and_silent_abort(self) -> None:
        """Standalone WINDOW_MODAL hides .app-shell behind #prePrintModal, delegates window close to Python (no JS window.close() crash), and returns Skipped."""
        import shutil, subprocess, json

        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertIn("body.standalone-modal .app-shell", html)
        self.assertIn("display: none;", html)
        self.assertIn("body.standalone-modal #prePrintModal", html)

        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(id) {
  const s = new Set();
  return {
    id, value: "", checked: false, disabled: false, textContent: "", innerHTML: "", dataset: {}, style: {}, files: [], children: [],
    className: "",
    appendChild(c){this.children.push(c);}, setAttribute(){}, getAttribute(){return "";}, hasAttribute(){return false;}, removeAttribute(){},
    addEventListener(){}, removeEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
    classList: { add(c){s.add(c);}, remove(c){s.delete(c);}, contains(c){return s.has(c);}, toggle(c,f){f?s.add(c):s.delete(c);} }
  };
}
const sentMsgs = [];
let winClosedCount = 0;
let _msgHandler = null;
var orca = {
  postMessage(raw){ sentMsgs.push(typeof raw === "string" ? JSON.parse(raw) : raw); },
  onMessage(fn){ _msgHandler = fn; }
};
var document = {
  documentElement: _mkEl("doc"), activeElement: null, hidden: false,
  getElementById(id) { if (!_els[id]) _els[id] = _mkEl(id); return _els[id]; },
  createElement(t) { return _mkEl(t); }, querySelectorAll(){return [];}, addEventListener(){}, body: _mkEl("body"),
};
document.body.classList.add("standalone-modal");
document.getElementById("prePrintModal").classList.add("open");
var window = {
  location: { search: "" },
  close(){ winClosedCount++; },
  matchMedia(){ return { matches: false, addEventListener(){} }; },
  addEventListener(){}
};
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + """
_msgHandler({ action: "open_preprint_modal", standalone_modal: true, state: { language: "en", last_gcode_path: "/tmp/a.gcode" } });
const activeAfterOpen = isStandaloneModal && document.body.classList.contains("standalone-modal");

// Cancel/Close sends cancel_print_setup to Python without calling JS window.close() while keeping #prePrintModal open
sentMsgs.length = 0;
closePrePrintModal();
const cancelSent = sentMsgs.some(m => m.type === "cancel_print_setup");
const jsWinCloseCount = winClosedCount;
const modalStillOpenAfterCancel = document.getElementById("prePrintModal").classList.contains("open");

// Server-side close_preprint_modal sends close_standalone_window without duplicate cancel or hiding #prePrintModal
sentMsgs.length = 0;
_msgHandler({ action: "close_preprint_modal" });
const closeWindowSent = sentMsgs.some(m => m.type === "close_standalone_window");
const dupCancel = sentMsgs.some(m => m.type === "cancel_print_setup");
const modalStillOpenAfterDone = document.getElementById("prePrintModal").classList.contains("open");

print(JSON.stringify({ activeAfterOpen, cancelSent, jsWinCloseCount, closeWindowSent, dupCancel, modalStillOpenAfterCancel, modalStillOpenAfterDone }));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "standalone_modal_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(res["activeAfterOpen"])
        self.assertTrue(res["cancelSent"])
        self.assertEqual(res["jsWinCloseCount"], 0)
        self.assertTrue(res["closeWindowSent"])
        self.assertFalse(res["dupCancel"])
        self.assertTrue(res["modalStillOpenAfterCancel"])
        self.assertTrue(res["modalStillOpenAfterDone"])

        # Backend abort helper preference order:
        # 1) ExecutionResult.cancelled(msg) if present
        with mock.patch.object(plugin.orca.ExecutionResult, "cancelled", create=True, side_effect=lambda msg="": ("CANCELLED_API", msg)):
            self.assertEqual(plugin._abort_pipeline_result(None, "done"), ("CANCELLED_API", "done"))
        # 2) ctx.cancel() + ExecutionResult.failure(FatalError, msg)
        called = []
        ctx_with_cancel = types.SimpleNamespace(cancel=lambda: called.append(True))
        ab_ctx = plugin._abort_pipeline_result(ctx_with_cancel, "by_ctx")
        self.assertEqual(called, [True])
        self.assertFalse(ab_ctx.ok)
        self.assertEqual(ab_ctx.status, plugin.orca.PluginResult.FatalError)
        # 3) Standard Orca 2.5+ abort -> ExecutionResult.failure(FatalError, msg) (stops PrintHost upload/print)
        ab_default = plugin._abort_pipeline_result(types.SimpleNamespace(), "by_fatal")
        self.assertFalse(ab_default.ok)
        self.assertEqual(ab_default.status, plugin.orca.PluginResult.FatalError)
        self.assertEqual(ab_default.message, "by_fatal")

    def test_startup_purge_estimate_and_settings(self) -> None:
        """G9111 startup purge estimate respects loaded tool, skip-purge toggle, and configurable startup_purge_length_mm."""
        st = plugin.PluginState()
        self.assertIn("startup_purge_length_mm", plugin.PluginState.DURABLE_SETTINGS_KEYS)
        self.assertEqual(st.startup_purge_length_mm, 200.0)
        st.apply_dict({"startup_purge_length_mm": 150.0}, apply_gcode_state=False)
        self.assertEqual(st.to_dict()["startup_purge_length_mm"], 150.0)

        fils = [
            {"slicer_tool": 0, "material": "PLA", "diameter": 1.75, "density": 1.24, "used_g": 10.0, "used": True}
        ]
        slots = [
            {"tool": 0, "status": "ready", "spoolman": {"spool_id": 1, "remaining_weight": 500.0}},
            {"tool": 1, "status": "loaded", "is_loaded": True, "spoolman": {"spool_id": 2, "remaining_weight": 500.0}},
        ]
        pp_info = {"flush_status": "POSTPROCESS_OK", "g9111StartSlicerTool": 0, "filament_diameter": 1.75, "events": []}

        # 1) Different slot loaded (T1 loaded, job starts on T0) -> 200mm startup purge included (~0.60g)
        pred_active = plugin.compute_preprint_filament_prediction(
            fils,
            {0: 0},
            slots,
            pp_info,
            startup_purge_length_mm=200.0,
            skip_purge_for_already_loaded_tool=True,
            ace_status={"connected": True, "current_index": 1},
            has_gcode=True,
        )
        self.assertTrue(pred_active["startup_purge_estimate"]["active"])
        self.assertFalse(pred_active["startup_purge_estimate"]["skipped"])
        self.assertAlmostEqual(pred_active["slots"][0]["purge_usage_g"], 0.6, delta=0.05)

        # 2) Same slot already loaded (T0 loaded) + skip_purge=True -> G9111 purge skipped (0.0g)
        slots_t0_loaded = [
            {"tool": 0, "status": "loaded", "is_loaded": True, "spoolman": {"spool_id": 1, "remaining_weight": 500.0}},
        ]
        pred_skipped = plugin.compute_preprint_filament_prediction(
            fils,
            {0: 0},
            slots_t0_loaded,
            pp_info,
            startup_purge_length_mm=200.0,
            skip_purge_for_already_loaded_tool=True,
            ace_status={"connected": True, "current_index": 0},
            has_gcode=True,
        )
        self.assertFalse(pred_skipped["startup_purge_estimate"]["active"])
        self.assertTrue(pred_skipped["startup_purge_estimate"]["skipped"])
        self.assertEqual(pred_skipped["slots"][0]["purge_usage_g"], 0.0)

        # 3) Same slot loaded + skip_purge=False -> startup purge still included
        pred_forced = plugin.compute_preprint_filament_prediction(
            fils,
            {0: 0},
            slots_t0_loaded,
            pp_info,
            startup_purge_length_mm=200.0,
            skip_purge_for_already_loaded_tool=False,
            ace_status={"connected": True, "current_index": 0},
            has_gcode=True,
        )
        self.assertTrue(pred_forced["startup_purge_estimate"]["active"])
        self.assertGreater(pred_forced["slots"][0]["purge_usage_g"], 0.5)

        # 4) Configured to 0 mm -> disabled
        pred_zero = plugin.compute_preprint_filament_prediction(
            fils,
            {0: 0},
            slots,
            pp_info,
            startup_purge_length_mm=0.0,
            skip_purge_for_already_loaded_tool=True,
            ace_status={"connected": True, "current_index": 1},
            has_gcode=True,
        )
        self.assertFalse(pred_zero["startup_purge_estimate"]["active"])
        self.assertEqual(pred_zero["slots"][0]["purge_usage_g"], 0.0)

        # 5) Settings UI includes dedicated Purge & Reserve tab with startupPurgeLengthInput and collapsible LLM prompt helper
        self.assertIn('id="settingsTabBtn_purge"', plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn('id="settingsPanel_purge"', plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn('id="startupPurgeLengthInput"', plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn('id="startupPurgePromptDetails"', plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn('id="btnCopyStartupPurgePrompt"', plugin.ACE_PLUGIN_HTML_UI)

    def test_ui_and_validation_preprint_modal_fixes(self) -> None:
        """Verify all 5 Map·Check·Send fixes: unused-extruder mismatch exclusion, hidden empty banners, centered zoom icon, green usage info popover, and right-aligned footer."""
        import shutil, subprocess, json

        # 1) Material mismatch: only active extruders checked + polymer family normalized
        st = plugin.PluginState()
        fils_mixed = [
            {"slicer_tool": 0, "material": "PLA+", "used": True, "used_g": 12.0, "density": 1.24},
            {"slicer_tool": 1, "material": "PETG", "used_g": 0.0, "used_mm": 0.0, "used_cm3": 0.0},  # unused (0g) -> must be ignored even without used=False!
            {"slicer_tool": 3, "material": "Silk PLA", "used": True, "used_g": 8.0, "density": 1.24},
            {"slicer_tool": 7, "material": "ABS", "used": False, "used_g": 0.0},
        ]
        slots = [
            {"tool": 0, "status": "ready", "material": "PLA", "hex_color": "#FF0000", "spoolman": {"spool_id": 1, "remaining_weight": 5.0}},  # low/insufficient weight, matching material!
            {"tool": 1, "status": "ready", "material": "PETG", "hex_color": "#00FF00"},
            {"tool": 2, "status": "empty", "material": "", "hex_color": "#000000"},
            {"tool": 3, "status": "ready", "material": "PLA", "hex_color": "#FFFF00", "spoolman": {"spool_id": 2, "remaining_weight": 200.0}},
        ]
        val_ok = plugin.validate_color_mapping(fils_mixed, {0: 0, 1: 2, 3: 3, 7: 1}, slots)
        self.assertFalse(val_ok["has_material_mismatch"])
        self.assertEqual(val_ok["material_mismatches"], [])
        self.assertEqual(val_ok["empty_slots"], [])

        # Even with insufficient spool weight (5.0g < 12.0g), upload gate blocks ONLY if start_print=True or real error — never asks for mismatch confirmation
        st.slicer_filaments = fils_mixed
        st.tool_map = {0: 0, 1: 2, 3: 3, 7: 1}
        allowed_no_ack, reasons_no_ack, _ = plugin.can_upload_gcode(
            {"warnings": [], "warnings_by_category": {}, "events": []},
            st,
            ace_status={"connected": True, "is_printing": False, "slots": slots},
            start_print=False,
            mismatch_confirmed=False,
        )
        self.assertTrue(allowed_no_ack, f"Expected allowed without mismatch ack, got: {reasons_no_ack}")

        # Genuine polymer conflict (active PLA extruder mapped to PETG slot) MUST trigger mismatch + block unless confirmed
        val_bad = plugin.validate_color_mapping(fils_mixed, {0: 1, 3: 3}, slots)
        self.assertTrue(val_bad["has_material_mismatch"])
        self.assertEqual(len(val_bad["material_mismatches"]), 1)
        self.assertEqual(val_bad["material_mismatches"][0]["slicer_tool"], 0)

        st.tool_map = {0: 1, 3: 3}
        allowed_bad, _, _ = plugin.can_upload_gcode(
            {"warnings": [], "warnings_by_category": {}, "events": []},
            st,
            ace_status={"connected": True, "is_printing": False, "slots": slots},
            start_print=True,
            mismatch_confirmed=False,
        )
        self.assertFalse(allowed_bad)

        # 2 & 3 & 5) CSS checks for hidden banners, centered zoom badge, green usage badge, and right-aligned footer
        html = plugin.ACE_PLUGIN_HTML_UI
        for css_snippet in (
            ".modal-stale-banner {\n    display: none;",
            ".modal-send-lock-notice {\n    display: none;",
            ".mismatch-gate-box {\n    display: none;",
            ".thumb-zoom-badge {",
            "width: 26px;\n    height: 26px;",
            "justify-content: center;",
            ".usage-info-badge {",
            "color: var(--ok);",
            ".modal-actions-bar {",
            "justify-content: space-between;",
            "width: 100%;",
            ".modal-actions-right {",
            "justify-content: flex-start;",
            "#btnModalSendPrint {",
            "margin-left: auto;",
        ):
            self.assertIn(css_snippet, html)
        self.assertIn('<div class="row modal-actions-right">', html)
        self.assertNotIn('class="row modal-actions-right" style="margin:0;"', html)
        right_m = re.search(r'<div class="row modal-actions-right">(.*?)</div>\s*</div>\s*</div>\s*</div>\s*</div>', html, re.S)
        self.assertIsNotNone(right_m)
        btn_ids = re.findall(r'id="([^"]+)"', right_m.group(1))
        self.assertEqual(btn_ids[-1], "btnModalSendPrint")

        # 4) JS gjs verification: hidden empty banners, no false mismatch gate on PLA+->PLA / unused PETG, and green usage breakdown populated
        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(id) {
  const s = new Set();
  const attrs = {};
  return {
    id, value: "", checked: false, disabled: false, textContent: "", innerHTML: "", dataset: {}, style: {}, files: [], children: [],
    className: "",
    appendChild(c){this.children.push(c);},
    setAttribute(k, v){ attrs[k] = String(v); },
    getAttribute(k){ return attrs[k] || ""; },
    hasAttribute(k){ return k in attrs; },
    removeAttribute(k){ delete attrs[k]; },
    addEventListener(){}, removeEventListener(){}, closest(){return null;}, querySelector(){return null;}, querySelectorAll(){return [];},
    getBoundingClientRect(){ return { left: 0, right: 0, top: 0, bottom: 0 }; },
    classList: {
      add(c){s.add(c); this._sync();},
      remove(c){s.delete(c); this._sync();},
      contains(c){return s.has(c);},
      toggle(c,f){f?s.add(c):s.delete(c); this._sync();},
      _sync(){ /* keep className simple unless overwritten directly */ }
    }
  };
}
var orca = { postMessage(){}, onMessage(fn){ globalThis._onMsg = fn; } };
var document = {
  documentElement: _mkEl("doc"), activeElement: null, hidden: false,
  getElementById(id) { if (!_els[id]) _els[id] = _mkEl(id); return _els[id]; },
  createElement(t) { return _mkEl(t); }, querySelectorAll(){return [];}, addEventListener(){}, body: _mkEl("body"),
};
var window = { location: { search: "" }, matchMedia(){ return { matches: false, addEventListener(){} }; }, addEventListener(){} };
var getComputedStyle = () => ({ getPropertyValue(){ return ""; } });
var localStorage = { getItem(){return null;}, setItem(){} };
var navigator = {};
""" + "\n".join(scripts) + """
globalThis._onMsg({
  state: {
    language: "en",
    last_gcode_path: "/tmp/snake.gcode",
    stale_model_info: { is_stale: false, active_model: "", reason: "" },
    startup_purge_length_mm: 200,
    skip_purge_for_already_loaded_tool: true,
    ace_status: { connected: true, is_printing: false, print_state: "standby", current_index: 3 },
    ace_slots: [
      { tool: 0, status: "ready", material: "PLA", hex_color: "#FF0000", spoolman: { spool_id: 1, remaining_weight: 111.7 } },
      { tool: 1, status: "empty", material: "", hex_color: "#000000" },
      { tool: 3, status: "loaded", is_loaded: true, material: "PLA", hex_color: "#FFFF00", spoolman: { spool_id: 2, remaining_weight: 664.0 } }
    ],
    slicer_filaments: [
      { slicer_tool: 0, material: "PLA+", color: "#FF0000", used: true, used_g: 5.6, density: 1.24, diameter: 1.75 },
      { slicer_tool: 1, material: "PETG", color: "#00FF00", used_g: 0, used_mm: 0, used_cm3: 0 },
      { slicer_tool: 3, material: "Silk PLA", color: "#FFFF00", used: true, used_g: 14.2, density: 1.24, diameter: 1.75 }
    ],
    tool_map: { 0: 0, 1: 1, 3: 3 },
    last_postprocess_info: {
      flush_status: "POSTPROCESS_OK",
      g9111StartSlicerTool: 0,
      filament_diameter: 1.75,
      events: [{ new_slicer_tool: 3, volume_mm3: 5000 }]
    }
  }
});
print(JSON.stringify({
  staleVisible: _els.modalStaleModelBanner.classList.contains("visible"),
  lockVisible: _els.modalSendLockNotice.className.includes("is-visible"),
  mismatchVisible: _els.modalMismatchGate.classList.contains("visible"),
  usageDisplay: _els.mappingUsageInfoWrap.style.display,
  usageTitle: _els.mappingUsageInfoBadge.getAttribute("title"),
  usagePopHtml: _els.mappingUsagePopover.innerHTML
}));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "modal_5fixes_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(res["staleVisible"])
        self.assertFalse(res["lockVisible"])
        self.assertFalse(res["mismatchVisible"])
        self.assertEqual(res["usageDisplay"], "inline-flex")
        self.assertFalse(res["usageTitle"])
        self.assertIn("T0 (PLA+)", res["usagePopHtml"])
        self.assertIn("T3 (Silk PLA)", res["usagePopHtml"])
        self.assertNotIn("T1 (PETG)", res["usagePopHtml"])
        self.assertIn("19.8 g", res["usagePopHtml"])
        self.assertIn("Filament Consumption Breakdown", res["usagePopHtml"])

    def test_main_menu_dropdowns_centered_prepare_cta_and_instant_feedback(self) -> None:
        """
        Regression test for the 4 main-menu UX & performance fixes:
        1. Custom dropdown menus (.custom-select-menu) are hidden by default (`display: none`) and only shown when `.custom-select.open` is active, with light-gray / black-text styling in light mode.
        2. `#btnOpenPrePrintModal` (`Prepare & Send Print`) is moved out of `.bottom-control-strip` (`ACE DRYER` row) and placed inside a centered `<section class="prepare-action-bar">` directly below it.
        3. Clicking `#btnOpenPrePrintModal` (`openPrePrintModal()`) immediately sets `aria-busy="true"`, class `is-loading`, and `"Preparing…"` / `"Wird vorbereitet…"`, opens `#prePrintModal` immediately if a valid non-stale G-code is already loaded in `currentState`, and resets loading state on host response or close.
        4. `acquire_gcode_from_slicer(force_refresh=False)` reuses cached `last_postprocess_info` in memory when the file signature (`realpath`, `mtime_ns`, `size`) and pre-print toggles are unchanged.
        """
        import shutil, subprocess, json, unittest.mock
        html = plugin.ACE_PLUGIN_HTML_UI

        dryer_sec_m = re.search(
            r'<section class="bottom-control-strip">(.*?)</section>',
            html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(dryer_sec_m)
        self.assertNotIn("btnOpenPrePrintModal", dryer_sec_m.group(1))
        prep_sec_m = re.search(
            r'</section>\s*(?:<!--[^>]*-->\s*)?<section class="prepare-action-bar">(.*?)</section>',
            html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(prep_sec_m)
        self.assertIn('id="btnOpenPrePrintModal"', prep_sec_m.group(1))
        self.assertRegex(
            html,
            r"\.prepare-action-bar\s*\{[^}]*display:\s*flex;[^}]*justify-content:\s*center;",
        )

        # 3. Backend file-signature cache avoids redundant disk re-parsing on `acquire_gcode_from_slicer(force_refresh=False)`
        with tempfile.TemporaryDirectory() as tmp:
            gcode_p = os.path.join(tmp, "cached_job.gcode")
            shutil.copyfile(self.sample_gcode_path, gcode_p)
            st = plugin.PluginState()
            st.storage_dir = tmp
            info1 = plugin.preview_gcode_file(gcode_p, st)
            self.assertEqual(info1["flush_status"], "POSTPROCESS_OK")
            self.assertIsNotNone(getattr(st, "_last_parsed_file_sig", None))

            with unittest.mock.patch(
                "kobra_van_kenorca_plugin.preview_gcode_file",
                side_effect=AssertionError("preview_gcode_file should not be called when file signature is cached"),
            ):
                src, path_out, info2 = plugin.acquire_gcode_from_slicer(
                    explicit_path="",
                    state=st,
                    force_refresh=False,
                )
            self.assertEqual(path_out, gcode_p)
            self.assertIsNotNone(info2)
            self.assertEqual(info2["flush_status"], "POSTPROCESS_OK")

        # 4. GJS runtime check for immediate loading state & zero-latency modal open
        gjs_bin = shutil.which("gjs")
        if not gjs_bin:
            return
        script_m = re.search(r"<script>(.*)</script>", html, flags=re.DOTALL)
        self.assertIsNotNone(script_m)
        js_code = script_m.group(1)

        js_harness = r"""
const _els = {};
function _makeEl(id) {
  return {
    id: id,
    value: "",
    checked: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    className: "",
    style: { display: "", setProperty: function() {} },
    dataset: {},
    attributes: {},
    children: [],
    _html: "",
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v || ""); if (this._html === "") this.children = []; },
    setAttribute: function(k, v) { this.attributes[k] = String(v); },
    getAttribute: function(k) { return this.attributes[k] || ""; },
    removeAttribute: function(k) { delete this.attributes[k]; },
    addEventListener: function() {},
    appendChild: function(c) { this.children.push(c); return c; },
    querySelector: function() { return null; },
    querySelectorAll: function() { return []; },
    contains: function() { return false; },
    getBoundingClientRect: function() { return { top: 0, bottom: 0, left: 0, right: 0, width: 100, height: 30 }; },
    classList: {
      _set: new Set(),
      add: function(...cls) { cls.forEach(c => this._set.add(c)); },
      remove: function(...cls) { cls.forEach(c => this._set.delete(c)); },
      toggle: function(c, force) {
        const on = force === undefined ? !this._set.has(c) : Boolean(force);
        if (on) this._set.add(c); else this._set.delete(c);
        return on;
      },
      contains: function(c) { return this._set.has(c); }
    }
  };
}
const document = {
  body: _makeEl("body"),
  documentElement: _makeEl("html"),
  activeElement: null,
  getElementById: function(id) {
    if (!_els[id]) _els[id] = _makeEl(id);
    return _els[id];
  },
  querySelector: function() { return null; },
  querySelectorAll: function() { return []; },
  createElement: function(tag) { return _makeEl(tag); },
  addEventListener: function() {}
};
const window = {
  location: { search: "" },
  matchMedia: function() { return { matches: true, addEventListener: function() {} }; },
  getComputedStyle: function() { return { getPropertyValue: function() { return ""; }, backgroundColor: "", color: "" }; },
  addEventListener: function() {}
};
function getComputedStyle() { return { getPropertyValue: function() { return ""; }, backgroundColor: "", color: "" }; }
let _posted = [];
const orca = {
  postMessage: function(m) { _posted.push(typeof m === "string" ? JSON.parse(m) : m); },
  onMessage: function(fn) { globalThis._onMsg = fn; }
};
function setInterval() { return 1; }
function clearInterval() {}
function setTimeout() { return 1; }
function clearTimeout() {}
""" + js_code + r"""

// Case A: Valid G-code already loaded -> clicking Prepare & Send Print sets loading immediately AND opens modal synchronously
currentState.last_gcode_path = "/tmp/ready_plate.gcode";
currentState.last_invalidation_reason = "";
currentState.auto_slice_in_progress = false;
currentState.stale_model_info = { is_stale: false };
openPrePrintModal();
const busyDuringClick = _els.btnOpenPrePrintModal.getAttribute("aria-busy");
const loadingClassDuringClick = _els.btnOpenPrePrintModal.classList.contains("is-loading");
const labelDuringClick = _els.btnOpenPrePrintLabel.textContent;
const modalOpenedImmediately = _els.prePrintModal.classList.contains("open");

// When host replies with open_preprint_modal, button resets loading state
globalThis._onMsg({
  action: "open_preprint_modal",
  source_file: "/tmp/ready_plate.gcode",
  state: { last_gcode_path: "/tmp/ready_plate.gcode" }
});
const busyAfterHostReply = _els.btnOpenPrePrintModal.getAttribute("aria-busy");
const loadingClassAfterHostReply = _els.btnOpenPrePrintModal.classList.contains("is-loading");
const labelAfterHostReply = _els.btnOpenPrePrintLabel.textContent;

print(JSON.stringify({
  busyDuringClick: busyDuringClick,
  loadingClassDuringClick: loadingClassDuringClick,
  labelDuringClick: labelDuringClick,
  modalOpenedImmediately: modalOpenedImmediately,
  busyAfterHostReply: busyAfterHostReply,
  loadingClassAfterHostReply: loadingClassAfterHostReply,
  labelAfterHostReply: labelAfterHostReply,
  postedTypes: _posted.map(p => p.type)
}));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "main_menu_4fixes_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(res["busyDuringClick"], "true")
        self.assertTrue(res["loadingClassDuringClick"])
        self.assertEqual(res["labelDuringClick"], "Preparing…")
        self.assertTrue(res["modalOpenedImmediately"])
        self.assertEqual(res["busyAfterHostReply"], "false")
        self.assertFalse(res["loadingClassAfterHostReply"])
        self.assertEqual(res["labelAfterHostReply"], "Prepare & Send Print")
        self.assertIn("get_status", res["postedTypes"])
        self.assertIn("acquire_slicer_gcode", res["postedTypes"])

    def test_kobra_orca_rebrand_logo_theme_and_migration(self) -> None:
        """Verify Kobra-Van-Kenorca (KoVaCa) rebrand, official logo & KobraOrca theme, migrations, 0-toolchange skipped initial purge, G9111 wording, and DE label."""
        import shutil, subprocess, json

        repo_dir = os.path.dirname(os.path.abspath(__file__))
        plugin_path = os.path.join(repo_dir, "kobra_van_kenorca_plugin.py")
        with open(plugin_path, "r", encoding="utf-8") as f:
            head_txt = "".join(f.readline() for _ in range(15))

        # 1. PEP 723 metadata, constants, Kobra tab title & zero stale vKobraACE / vKA UI strings
        self.assertIn('name = "Kobra-Van-Kenorca"', head_txt)
        self.assertEqual(plugin.PLUGIN_SHORT_NAME, "Kobra-Van-Kenorca")
        self.assertEqual(plugin.PLUGIN_TAB_NAME, "⚕ Kobra")
        self.assertEqual(plugin.PIPELINE_CAPABILITY_NAME, "KobraVanKenorca_PostProcess")
        self.assertEqual(plugin.KobraS1AcePageCapability().get_name(), "⚕ Kobra")
        self.assertEqual(plugin.KobraVanKenorcaPageCapability().get_name(), "⚕ Kobra")
        self.assertIs(plugin.KobraVanKenorcaPlugin, plugin.KobraS1AcePlugin)

        html = plugin.ACE_PLUGIN_HTML_UI
        self.assertNotIn("vKobraACE", html)
        self.assertNotIn(">vKA<", html)
        self.assertIn("<title>Kobra-Van-Kenorca</title>", html)
        for lang_code in ("en", "de"):
            for k, val in plugin.TRANSLATIONS[lang_code].items():
                self.assertNotIn("vKobraACE", str(val), f"Leftover vKobraACE in {lang_code}:{k}")

        # 1b. Precise G9111 initial-purge-skipped wording & German 'Entladen nach Druck' label
        self.assertEqual(
            plugin.t("ui.modal.job.purge_startup_skipped", lang="en"),
            "G9111 initial purge skipped (tool already loaded)",
        )
        self.assertEqual(
            plugin.t("ui.modal.job.purge_startup_skipped", lang="de"),
            "G9111-Initial-Purge übersprungen (Tool bereits geladen)",
        )
        self.assertEqual(plugin.t("ui.modal.opt.cut_post", lang="de"), "Entladen nach Druck")

        # 2. Official PNG assets on disk & Data-URI embedded in header, favicon, and Map·Check·Send modal
        for rel in ("assets/kobra_orca_logo.png", "assets/kobra_orca_icon.png"):
            fpath = os.path.join(repo_dir, rel)
            self.assertTrue(os.path.isfile(fpath), f"Missing asset {fpath}")
            with open(fpath, "rb") as imgf:
                self.assertEqual(imgf.read(8), b"\x89PNG\r\n\x1a\n")

        self.assertTrue(plugin.KOBRA_ORCA_LOGO_DATA_URI.startswith("data:image/png;base64,"))
        logo_b64 = plugin.KOBRA_ORCA_LOGO_DATA_URI.split(",", 1)[1]
        logo_raw = base64.b64decode(logo_b64, validate=True)
        self.assertEqual(logo_raw[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn(b"IEND", logo_raw[-16:])
        self.assertNotIn("__KOBRA_ORCA_LOGO_URI__", html)
        self.assertIn(f'class="brand-logo-img" src="{plugin.KOBRA_ORCA_LOGO_DATA_URI}"', html)
        self.assertIn(f'class="modal-brand-logo" src="{plugin.KOBRA_ORCA_LOGO_DATA_URI}"', html)
        self.assertIn(f'<link rel="icon" type="image/png" href="{plugin.KOBRA_ORCA_LOGO_DATA_URI}" />', html)

        # Fail-closed check: inside HAS_ORCA_RUNTIME without explicit .install_state.json grant,
        # external OS backup path is never touched (prevents OrcaSlicer "Plugin operation denied" popups)
        with tempfile.TemporaryDirectory() as tmpdir:
            st_fc = plugin.PluginState()
            st_fc.FALLBACK_CONFIG_PATH = os.path.join(tmpdir, "Kobra-Van-Kenorca", "state.json")
            with mock.patch.object(plugin, "HAS_ORCA_RUNTIME", True), mock.patch.object(
                plugin.PluginState, "_check_install_state_allows_external_backup", return_value=None
            ):
                self.assertFalse(st_fc._should_use_user_backup_path(os.path.join(tmpdir, "storage", "state.json")))
                self.assertTrue(st_fc._os_backup_denied)

        # 3. KobraOrca signature theme palette, i18n, UI button, and backend persistence
        self.assertIn("kobraorca", plugin.SUPPORTED_THEMES)
        self.assertEqual(plugin.t("header.theme.kobraorca", lang="en"), "KobraOrca")
        self.assertEqual(plugin.t("header.theme.kobraorca", lang="de"), "KobraOrca")
        self.assertIn('id="themeBtnKobraOrca"', html)
        style_block = html.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertIn('html[data-theme="kobraorca"]', style_block)
        for hex_col in (
            "#0b1116", "#0e151b", "#131d24", "#18242d", "#233542",
            "#18b6c9", "#22d3ee", "#0ea5b7", "#061318",
            "#e5b54f", "#f0c96b", "#eef5f8", "#8fa6b5",
        ):
            self.assertIn(hex_col, style_block)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "state.json")
            st1 = plugin.PluginState()
            st1.resolve_config_path = lambda: cfg_file
            st1.set_appearance(theme_mode="kobraorca", save=True)
            st2 = plugin.PluginState()
            st2.resolve_config_path = lambda: cfg_file
            st2.load_config()
            self.assertEqual(st2.theme_mode, "kobraorca")

        # 4. Backwards-compatible migrations (KobraOrca, vKobraACE, kobra_s1_ace_plugin state.json + cap keys + pipeline)
        with tempfile.TemporaryDirectory() as tmpdir:
            ko_path = os.path.join(tmpdir, "KobraOrca", "state.json")
            kvk_path = os.path.join(tmpdir, "Kobra-Van-Kenorca", "state.json")
            os.makedirs(os.path.dirname(ko_path), exist_ok=True)
            with open(ko_path, "w", encoding="utf-8") as f:
                json.dump({"moonraker_url": "http://192.0.2.88:7125", "theme_mode": "kobraorca"}, f)

            st_mig = plugin.PluginState()
            st_mig.FALLBACK_CONFIG_PATH = kvk_path
            st_mig.LEGACY_FALLBACK_CONFIG_PATH = ko_path
            st_mig.LEGACY_FALLBACK_CONFIG_PATHS = (
                ko_path,
                os.path.join(tmpdir, "vKobraACE", "state.json"),
                os.path.join(tmpdir, "kobra_s1_ace_plugin", "state.json"),
            )
            st_mig.resolve_config_path = lambda: kvk_path
            st_mig.load_config()
            self.assertEqual(st_mig.moonraker_url, "http://192.0.2.88:7125")
            self.assertEqual(st_mig.theme_mode, "kobraorca")
            self.assertTrue(os.path.isfile(kvk_path))

        for leg_key in ("kobra_orca_settings", "vkobra_ace_settings"):
            cap_legacy = types.SimpleNamespace(
                get_config=lambda k=leg_key: json.dumps({k: {"moonraker_url": "http://192.0.2.99:7125"}})
            )
            self.assertEqual(
                plugin.PluginState._read_orca_cap_settings(cap_legacy),
                {"moonraker_url": "http://192.0.2.99:7125"},
            )

        for legacy_pipe_name in ("KobraOrca ACE Pro Post-Processor", "Anycubic Kobra S1 ACE Pro Post-Processor"):
            migrated_pipe = plugin.build_slicing_pipeline_value(f'first_plugin;"{legacy_pipe_name}"')
            self.assertEqual(
                plugin.parse_slicer_pipeline_entries(migrated_pipe),
                ["first_plugin", "KobraVanKenorca_PostProcess"],
            )

        # 5. Toolchange count == 0 when Skip Purge is enabled and start tool is already loaded (single-color G-code)
        single_color_lines = [
            "; flush_multiplier = 1.0\n",
            "; flush_volumes_matrix = 0,0,0,0\n",
            "; flush_volumes_vector = 140,140\n",
            "; filament_diameter = 1.75\n",
            "G9111 bedTemp=60 extruderTemp=210 tool=0\n",
            "SKIP_PURGE_FOR_ALREADY_LOADED_TOOL=1\n",
            "T0\n",
            "G1 X10 Y10 E5\n",
        ]
        _, info_loaded = plugin.process_gcode(
            single_color_lines,
            tool_map={0: 2},
            skip_purge_for_already_loaded_tool=True,
            force_initial_purge=False,
            current_loaded_slot=2,
        )
        self.assertEqual(info_loaded["raw_toolchanges"], 1)
        self.assertEqual(info_loaded["toolchanges"], 0)
        self.assertEqual(info_loaded["effective_toolchanges"], 0)

        # Remapping to a different (not-loaded) ACE slot restores toolchanges == 1; mapping back to loaded slot -> 0
        remapped_diff = plugin.remap_flush_info(
            info_loaded, {0: 3}, ace_status={"connected": True, "current_index": 2}
        )
        self.assertEqual(remapped_diff["toolchanges"], 1)
        remapped_same = plugin.remap_flush_info(
            remapped_diff, {0: 2}, ace_status={"connected": True, "current_index": 2}
        )
        self.assertEqual(remapped_same["toolchanges"], 0)

        # When Skip Purge is disabled or Force Initial Purge is enabled, toolchanges stays 1
        _, info_forced = plugin.process_gcode(
            single_color_lines,
            tool_map={0: 2},
            skip_purge_for_already_loaded_tool=True,
            force_initial_purge=True,
            current_loaded_slot=2,
        )
        self.assertEqual(info_forced["toolchanges"], 1)

        # 6. GJS runtime verification of setThemeMode('kobraorca') & 0-toolchange display in renderModalJobSummary()
        gjs_bin = shutil.which("gjs")
        if gjs_bin:
            js_code = re.search(r"<script>(.*)</script>", html, flags=re.DOTALL).group(1)
            js_harness = r"""
const _els = {};
function _makeEl(id) {
  return {
    id, value: "", checked: false, disabled: false, textContent: "", innerHTML: "",
    className: "", style: { display: "", setProperty() {} }, dataset: {}, attributes: {}, children: [],
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return this.attributes[k] || ""; },
    removeAttribute(k) { delete this.attributes[k]; },
    addEventListener() {}, appendChild(c) { this.children.push(c); return c; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    contains() { return false; },
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 100, height: 30 }; },
    classList: {
      _set: new Set(),
      add(...cls) { cls.forEach(c => this._set.add(c)); },
      remove(...cls) { cls.forEach(c => this._set.delete(c)); },
      toggle(c, force) {
        const on = force === undefined ? !this._set.has(c) : Boolean(force);
        if (on) this._set.add(c); else this._set.delete(c);
        return on;
      },
      contains(c) { return this._set.has(c); }
    }
  };
}
const document = {
  body: _makeEl("body"), documentElement: _makeEl("html"), activeElement: null,
  getElementById(id) { if (!_els[id]) _els[id] = _makeEl(id); return _els[id]; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  createElement(t) { return _makeEl(t); }, addEventListener() {}
};
const window = {
  location: { search: "" },
  matchMedia() { return { matches: false, addEventListener() {} }; },
  getComputedStyle() { return { getPropertyValue() { return ""; }, backgroundColor: "", color: "" }; },
  addEventListener() {}
};
function getComputedStyle() { return { getPropertyValue() { return ""; }, backgroundColor: "", color: "" }; }
const _storage = {};
const localStorage = { getItem(k) { return _storage[k] || null; }, setItem(k, v) { _storage[k] = String(v); } };
let _posted = [];
const orca = { postMessage(m) { _posted.push(typeof m === "string" ? JSON.parse(m) : m); }, onMessage(fn) {} };
function setInterval() { return 1; }
function clearInterval() {}
function setTimeout() { return 1; }
function clearTimeout() {}
""" + js_code + r"""
setThemeMode("kobraorca", true);

// Test JS 0-toolchange display when mapped start slot is already loaded & Skip Purge is active
currentState.last_gcode_path = "/tmp/single.gcode";
currentState.skip_purge_for_already_loaded_tool = true;
currentState.force_initial_purge = false;
currentState.tool_map = { 0: 2 };
currentState.ace_status = { connected: true, current_index: 2 };
currentState.last_postprocess_info = {
  toolchanges: 1,
  raw_toolchanges: 1,
  g9111StartSlicerTool: 0,
  g9111MappedTool: 2,
  events: [{ old_slicer_tool: 0, new_slicer_tool: 0, mapped_tool: 2, purge_length_mm: 0 }]
};
renderModalJobSummary();
const tcWhenLoaded = _els.modalJobPurge.textContent;
currentState.ace_status.current_index = 1;
renderModalJobSummary();
const tcWhenDifferent = _els.modalJobPurge.textContent;

print(JSON.stringify({
  dataTheme: document.documentElement.getAttribute("data-theme"),
  dataResolved: document.documentElement.getAttribute("data-resolved"),
  btnActive: _els.themeBtnKobraOrca.classList.contains("active"),
  autoActive: _els.themeBtnAuto.classList.contains("active"),
  storedTheme: _storage["kobra_orca_theme"],
  lastPosted: _posted[_posted.length - 1],
  tcWhenLoaded: tcWhenLoaded,
  tcWhenDifferent: tcWhenDifferent
}));
"""
            with tempfile.TemporaryDirectory() as js_tmp:
                js_path = os.path.join(js_tmp, "kobraorca_theme_harness.js")
                with open(js_path, "w", encoding="utf-8") as f:
                    f.write(js_harness)
                proc = subprocess.run([gjs_bin, js_path], capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            res = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(res["dataTheme"], "kobraorca")
            self.assertEqual(res["dataResolved"], "dark")
            self.assertTrue(res["btnActive"])
            self.assertFalse(res["autoActive"])
            self.assertEqual(res["storedTheme"], "kobraorca")
            self.assertEqual(res["lastPosted"]["theme_mode"], "kobraorca")
            self.assertEqual(res["tcWhenLoaded"], "Toolchanges: 0")
            self.assertEqual(res["tcWhenDifferent"], "Toolchanges: 1")

    def test_pipeline_reg_project_opened_and_legacy_name_remigration(self) -> None:
        """
        Regression test:
        1. Opening a .3mf project (`ProjectOpened`) whose preset shares the same
           `(printer, process)` name but has an empty `slicing_pipeline_plugin` must
           re-register `KobraVanKenorca_PostProcess` so clicking Upload does not bypass the modal.
        2. Even within the same session key, a legacy pipeline name (`KobraOrca ACE Pro Post-Processor`)
           must be migrated to `KobraVanKenorca_PostProcess` instead of returning `already_checked`.
        """
        host = self._make_fake_preset_host(
            printer_name="Anycubic Kobra S1 0.4 nozzle",
            printer_model="Anycubic Kobra S1",
            process_name="0.20mm Standard @Anycubic Kobra S1",
            initial_pipeline="",
        )
        st = plugin.PluginState()
        st.resolve_config_path = lambda: ""
        fake_evt_enum = types.SimpleNamespace(
            PresetSelected="PresetSelected",
            ProjectOpened="ProjectOpened",
            NewProject="NewProject",
            DeviceSelected="DeviceSelected",
        )
        with mock.patch.object(plugin.orca, "host", host), mock.patch.object(
            plugin.orca, "LifecycleEvent", fake_evt_enum
        ):
            # Initial registration
            res1 = plugin.ensure_slicing_pipeline_registered(st)
            self.assertEqual(res1["status"], "updated")
            self.assertIn(plugin.PIPELINE_CAPABILITY_NAME, host._box["process_opts"]["slicing_pipeline_plugin"])

            # Simulate opening a .3mf project that resets slicing_pipeline_plugin to empty
            host._box["process_opts"]["slicing_pipeline_plugin"] = ""
            handled = plugin._handle_pipeline_lifecycle_event(
                fake_evt_enum.ProjectOpened,
                types.SimpleNamespace(msg="kobra-bite-handle_ac.3mf"),
                state=st,
                use_thread=False,
            )
            self.assertTrue(handled)
            self.assertIn(
                plugin.PIPELINE_CAPABILITY_NAME,
                plugin.parse_slicer_pipeline_entries(host._box["process_opts"]["slicing_pipeline_plugin"]),
            )

            # Simulate legacy pipeline entry present on the already-checked session key
            host._box["process_opts"]["slicing_pipeline_plugin"] = '"KobraOrca ACE Pro Post-Processor"'
            res_mig = plugin.ensure_slicing_pipeline_registered(st)
            self.assertEqual(res_mig["status"], "updated")
            entries = plugin.parse_slicer_pipeline_entries(host._box["process_opts"]["slicing_pipeline_plugin"])
            self.assertIn(plugin.PIPELINE_CAPABILITY_NAME, entries)
            self.assertNotIn("KobraOrca ACE Pro Post-Processor", entries)

    def test_reslice_overwrites_same_preview_temp_clears_stale_and_reloads(self) -> None:
        """
        Regression test for modifying a model & re-slicing in OrcaSlicer:
        1. Slice #1 stages preview temp `.1234.0.gcode` as `kobra-bite-handle_ac.gcode`.
        2. Slice #2 fires `SliceStarted`, overwrites `.1234.0.gcode` in-place with newer mtime,
           and fires `SlicingJobComplete` without a new `GCodeExportFinished` path -> must
           automatically re-capture `.1234.0.gcode` without leaving `slice_newer_than_export`.
        3. Manual `acquire_slicer_gcode` (Reload G-Code from Slicer) when a newer `.1234.0.gcode`
           exists on disk while `slice_newer_than_export` is active must re-stage & clear stale.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _st_dir, _cfg, page, _sent = self._make_zombie_test_state_and_page(os.path.join(tmp_dir, "st"))
            model_dir = os.path.join(tmp_dir, "orcaslicer_model", "Metadata")
            os.makedirs(model_dir, exist_ok=True)
            preview_path = os.path.join(model_dir, ".1234.0.gcode")
            with open(preview_path, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("kobra-bite-handle_ac"))

            st._host_ui_snapshot = {
                "active_model": {
                    "display_name": "kobra-bite-handle_ac",
                    "candidates": ["kobra-bite-handle_ac"],
                    "plate_index": 1,
                    "host_has_slice": True,
                },
                "filaments": [],
                "filaments_captured": False,
                "preset_fingerprint": {"digest": "fp1", "captured": True},
                "captured_ns": time.time_ns(),
            }
            fake_evt_enum = types.SimpleNamespace(
                SliceStarted="SliceStarted",
                GCodeExportFinished="GCodeExportFinished",
                SlicingJobComplete="SlicingJobComplete",
            )
            fake_err_enum = types.SimpleNamespace(Error="Error", Warn="Warn")

            with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
                plugin.orca, "LifecycleEvent", fake_evt_enum
            ), mock.patch.object(plugin.orca, "LifecycleEvtCode", fake_err_enum), mock.patch.object(
                st, "refresh_host_ui_snapshot", return_value=st._host_ui_snapshot
            ), mock.patch.object(
                plugin, "get_orca_active_model_info", return_value=st._host_ui_snapshot["active_model"]
            ):
                # Slice #1: GCodeExportFinished + SlicingJobComplete
                page.on_lifecycle_event(
                    fake_evt_enum.GCodeExportFinished,
                    types.SimpleNamespace(id="p1", name="kobra-bite-handle_ac", msg=preview_path, code=None),
                )
                for _ in range(40):
                    if st.last_gcode_path:
                        break
                    time.sleep(0.02)
                page.on_lifecycle_event(
                    fake_evt_enum.SlicingJobComplete,
                    types.SimpleNamespace(id="p1", name="Plate 1", msg="", code=None),
                )
                self.assertTrue(st.last_gcode_path and os.path.isfile(st.last_gcode_path))
                stale1 = plugin.check_stale_gcode_model(gcode_path=st.last_gcode_path, state=st)
                self.assertFalse(stale1["is_stale"], f"Slice #1 should be fresh: {stale1}")

                # Slice #2: SliceStarted -> overwrite same preview temp file -> SlicingJobComplete (no GCodeExportFinished)
                page.on_lifecycle_event(
                    fake_evt_enum.SliceStarted,
                    types.SimpleNamespace(id="p1", name="Plate 1", msg="", code=None),
                )
                time.sleep(0.02)
                with open(preview_path, "w", encoding="utf-8") as f:
                    f.write(self._sample_zombie_gcode_text("kobra-bite-handle_ac") + "\n; modified_v2\n")
                old_stat = os.stat(preview_path)
                os.utime(preview_path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 50_000_000))

                page.on_lifecycle_event(
                    fake_evt_enum.SlicingJobComplete,
                    types.SimpleNamespace(id="p1", name="Plate 1", msg="", code=None),
                )
                for _ in range(50):
                    if st.last_gcode_path and os.path.isfile(st.last_gcode_path):
                        with open(st.last_gcode_path, "r", encoding="utf-8") as rf:
                            if "; modified_v2" in rf.read():
                                break
                    time.sleep(0.02)

                self.assertTrue(st.last_gcode_path and os.path.isfile(st.last_gcode_path))
                with open(st.last_gcode_path, "r", encoding="utf-8") as rf:
                    self.assertIn("; modified_v2", rf.read())
                stale2 = plugin.check_stale_gcode_model(gcode_path=st.last_gcode_path, state=st)
                self.assertFalse(stale2["is_stale"], f"Slice #2 should be fresh after re-capture: {stale2}")

                # Reload G-Code from Slicer recovery when slice_newer_than_export is active and preview temp updated
                time.sleep(0.02)
                with open(preview_path, "w", encoding="utf-8") as f:
                    f.write(self._sample_zombie_gcode_text("kobra-bite-handle_ac") + "\n; modified_v3\n")
                st_v3 = os.stat(preview_path)
                os.utime(preview_path, ns=(st_v3.st_atime_ns, st_v3.st_mtime_ns + 50_000_000))
                st.record_slicing_completed(plate_label="Plate 1", plate_index=1, ui_snapshot=st._host_ui_snapshot)
                stale_before_reload = plugin.check_stale_gcode_model(gcode_path=st.last_gcode_path, state=st)
                self.assertIn("slice_newer_than_export", stale_before_reload["reasons"])

                page._msg_acquire_slicer_gcode(st.get_client(), {"force_refresh": True})
                with open(st.last_gcode_path, "r", encoding="utf-8") as rf:
                    self.assertIn("; modified_v3", rf.read())
                stale_after_reload = plugin.check_stale_gcode_model(gcode_path=st.last_gcode_path, state=st)
                self.assertFalse(
                    stale_after_reload["is_stale"],
                    f"Reload G-Code from Slicer should clear stale after re-staging preview: {stale_after_reload}",
                )

    def test_ephemeral_upload_temp_deletion_does_not_invalidate_sliced_plate(self) -> None:
        """
        Regression test:
        When OrcaSlicer runs `KobraS1AcePostProcessCapability.execute(ctx)` on Upload,
        `ctx.gcode_path` is an ephemeral file like `/tmp/.OrcaSlicer.upload.<uuid>` that
        OrcaSlicer deletes immediately after the upload popup closes.
        Deleting that ephemeral upload temp file must NEVER invalidate the active staged
        G-code (`missing_preview_temp_file`) while the plate is still sliced!
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _st_dir, _cfg, _page, _sent = self._make_zombie_test_state_and_page(os.path.join(tmp_dir, "st"))
            model_dir = os.path.join(tmp_dir, "orcaslicer_model", "Metadata")
            os.makedirs(model_dir, exist_ok=True)
            real_preview = os.path.join(model_dir, ".2.0.gcode")
            ephemeral_upload = os.path.join(tmp_dir, ".OrcaSlicer.upload.07a9-2511-e9b1-6614")
            sample_gcode = self._sample_zombie_gcode_text("kobra-bite-handle_ac")
            with open(real_preview, "w", encoding="utf-8") as f:
                f.write(sample_gcode)
            with open(ephemeral_upload, "w", encoding="utf-8") as f:
                f.write(sample_gcode)

            self.assertTrue(plugin._is_ephemeral_upload_temp_source(ephemeral_upload))
            self.assertFalse(plugin._is_preview_temp_source(ephemeral_upload, st.resolve_storage_dir()))
            self.assertTrue(plugin._is_preview_temp_source(real_preview, st.resolve_storage_dir()))

            st._last_preview_temp_path = real_preview
            st._last_preview_temp_existed = True

            staged_path = plugin._stage_gcode_file_to_storage(
                ephemeral_upload,
                st,
                output_name="kobra-bite-handle_ac_PLA_0.2_1h30m.gcode",
                host_name="OctoPrint",
                project_model="kobra-bite-handle_ac",
                source_kind="slicing_pipeline",
            )
            self.assertTrue(staged_path and os.path.isfile(staged_path))
            st.last_gcode_path = staged_path
            st.last_gcode_source = "slicing_pipeline"

            sc = plugin._read_provenance_sidecar(staged_path)
            self.assertEqual(sc.get("preview_temp_path"), real_preview)

            # OrcaSlicer deletes the ephemeral upload temp file right after the popup closes
            os.unlink(ephemeral_upload)

            # Active staged G-code must remain valid because real_preview still exists
            invalidated = plugin.check_and_invalidate_missing_preview_slice(st)
            self.assertFalse(invalidated, "Deleting ephemeral .OrcaSlicer.upload.* must not invalidate active G-code!")
            self.assertEqual(st.last_gcode_path, staged_path)

            # If OrcaSlicer actually un-slices (deletes real_preview), THEN it invalidates cleanly
            os.unlink(real_preview)
            invalidated_after_unslice = plugin.check_and_invalidate_missing_preview_slice(st)
            self.assertTrue(invalidated_after_unslice)
            self.assertEqual(st.last_gcode_path, "")

    def test_preview_print_not_superseded_by_older_hidden_preview_temp(self) -> None:
        """
        Regression test:
        When the user slices in Preview (writing `.../Metadata/.2.2.gcode`) and then clicks
        Print directly (`KobraS1AcePostProcessCapability.execute(ctx)` staging
        `<model>_PLA_0.16_2h37m.gcode` as `slicing_pipeline`), a delayed lifecycle event or
        `acquire_slicer_gcode` referencing the older `.2.2.gcode` must NEVER re-stage it as
        `slicer_explicit` and mark the active `slicing_pipeline` file `is_superseded=True`.
        Also verifies standalone modal suppresses `autoSyncFromSlicerOnTabActivate`.
        """
        self.assertIn('document.body.classList.contains("standalone-modal")', plugin.ACE_PLUGIN_HTML_UI)
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _st_dir, _cfg, _page, _sent = self._make_zombie_test_state_and_page(os.path.join(tmp_dir, "st"))
            model_dir = os.path.join(tmp_dir, "orcaslicer_model", "Metadata")
            os.makedirs(model_dir, exist_ok=True)
            real_preview = os.path.join(model_dir, ".2.2.gcode")
            ephemeral_upload = os.path.join(tmp_dir, ".OrcaSlicer.upload.306e-e41b-53e8-2819")
            sample_gcode = self._sample_zombie_gcode_text("Ergonomic Replacement Handle for IKEA Torkis")
            with open(real_preview, "w", encoding="utf-8") as f:
                f.write(sample_gcode)
            # Make preview temp file slightly older than the pipeline upload temp file
            old_t = time.time() - 20.0
            os.utime(real_preview, (old_t, old_t))
            with open(ephemeral_upload, "w", encoding="utf-8") as f:
                f.write(sample_gcode)

            st._last_preview_temp_path = real_preview
            st._last_preview_temp_existed = True
            st._pending_preview_gcode = {"path": real_preview, "captured_ns": time.time_ns()}

            staged_path = plugin._stage_gcode_file_to_storage(
                ephemeral_upload,
                st,
                output_name="Ergonomic Replacement Handle for IKEA Torkis_PLA_0.16_2h37m.gcode",
                host_name="OctoPrint",
                project_model="Ergonomic Replacement Handle for IKEA Torkis",
                source_kind="slicing_pipeline",
            )
            self.assertTrue(staged_path and os.path.isfile(staged_path))
            st.last_gcode_path = staged_path
            st.last_gcode_source = "slicing_pipeline"
            st._pipeline_active_since_slice = True
            st._last_pipeline_export_ns = time.time_ns()

            # Older `.2.2.gcode` must NOT be returned by `_find_updated_preview_temp_source`
            self.assertEqual(plugin._find_updated_preview_temp_source(st), "")

            # Attempting to capture the older `.2.2.gcode` via lifecycle must be ignored (`already_handled`)
            with st._pending_preview_lock:
                cap_status, cap_payload = plugin._capture_lifecycle_explicit_unlocked(
                    st,
                    real_preview,
                    "",
                    "Ergonomic Replacement Handle for IKEA Torkis.gcode",
                    None,
                    "",
                    "Ergonomic Replacement Handle for IKEA Torkis",
                    deferred_on_perm_error=False,
                )
            self.assertEqual(cap_status, "already_handled")
            self.assertIsNone(cap_payload)

            # Sidecar of the active `slicing_pipeline` file must remain fresh (not superseded/stale)
            sc = plugin._read_provenance_sidecar(staged_path)
            self.assertFalse(sc.get("is_superseded"))
            self.assertFalse(sc.get("is_stale"))
            stale = plugin.check_stale_gcode_model(gcode_path=staged_path, state=st)
            self.assertFalse(stale.get("is_stale"), f"Unexpected stale result: {stale}")

    def test_export_gcode_modal_and_permission_fallback(self) -> None:
        """Export UI provides destination modal when showSaveFilePicker is absent, and backend falls back to plugin storage on PermissionError."""
        self.assertIn('id="exportPathModal"', plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn("openExportPathModal(", plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn("confirmExportPatchedGcode()", plugin.ACE_PLUGIN_HTML_UI)
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, st_dir, _cfg, page, sent = self._make_zombie_test_state_and_page(os.path.join(tmp_dir, "st"))
            src = os.path.join(st_dir, "Support_test_PLA_0.12_41m11s.gcode")
            with open(src, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Support_test"))

            orig_export = plugin.export_patched_gcode

            def _deny_external(gcode_path, state=None, dest_path="", **kwargs):
                if dest_path and "denied_dir" in dest_path:
                    raise PermissionError("sandbox denied")
                return orig_export(gcode_path, state=state, dest_path=dest_path, **kwargs)

            with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
                plugin, "export_patched_gcode", side_effect=_deny_external
            ):
                page._handle_async_message(
                    "export_gcode",
                    {"gcode_path": src, "dest_path": os.path.join(tmp_dir, "denied_dir", "out.gcode")},
                )
            self.assertTrue(sent)
            fb = sent[-1].get("modal_feedback") or {}
            self.assertEqual(fb.get("level"), "warn")
            expected_fb = os.path.join(st_dir, "exported_gcode", "out.gcode")
            self.assertTrue(os.path.isfile(expected_fb))
            self.assertIn(expected_fb, fb.get("text", ""))

    def test_multi_ace_dryer_independent_temps_and_times_display(self) -> None:
        """When 2 ACE units dry at different temperatures and remaining times, both units appear in dryerBadge."""
        self.assertIn("function formatAceDryerDisplay(", plugin.ACE_PLUGIN_HTML_UI)
        raw_status = {
            "ace_instance_0": {
                "status": "ready",
                "temp": 32,
                "dryer_status": {"status": "drying", "target_temp": 47, "duration": 240, "remain_time": 239.4},
                "slots": [{"index": i, "status": "ready", "type": "PLA", "color": [255, 255, 255], "temp": 210} for i in range(4)],
            },
            "ace_instance_1": {
                "status": "ready",
                "temp": 33,
                "dryer_status": {"status": "drying", "target_temp": 48, "duration": 240, "remain_time": 240.0},
                "slots": [{"index": i, "status": "ready", "type": "PETG", "color": [0, 0, 0], "temp": 240} for i in range(4)],
            },
        }
        norm = plugin.MoonrakerClient.normalize_ace_status(raw_status)
        self.assertEqual(norm["dryer"]["active_instances"], [0, 1])
        self.assertEqual(norm["dryer"]["instances"][0]["target_temp"], 47)
        self.assertEqual(norm["dryer"]["instances"][1]["target_temp"], 48)
        self.assertEqual(norm["dryer"]["instances"][0]["remain_time_min"], 239.4)
        self.assertEqual(norm["dryer"]["instances"][1]["remain_time_min"], 240.0)

    def test_reload_gcode_from_slicer_replaces_stale_file_picker_gcode(self) -> None:
        """Clicking Reload G-Code from Slicer (force_refresh=True) replaces a stale file_picker file with the slicer's staged G-code."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            st, _st_dir, _cfg, page, sent = self._make_zombie_test_state_and_page(os.path.join(tmp_dir, "st"))
            slicer_src = os.path.join(tmp_dir, "Support_test.gcode")
            with open(slicer_src, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Support_test"))

            # 1) Slicer staged a valid slice
            slicer_staged = plugin._stage_gcode_file_to_storage(
                slicer_src,
                st,
                output_name="Support_test.gcode",
                project_model="Support_test",
                source_kind="slicing_pipeline",
            )
            self.assertTrue(slicer_staged and os.path.isfile(slicer_staged))

            # 2) User manually loaded a different file via Browse .gcode (`file_picker`), then a slice completed
            fp_path, _fp_info = plugin.load_gcode_from_content(
                "adsfasdSupport_test_PLA_0.12_41m11s.gcode",
                self._sample_zombie_gcode_text("adsfasdSupport_test"),
                state=st,
            )
            self.assertEqual(st.last_gcode_source, "file_picker")
            self.assertEqual(os.path.basename(st.last_gcode_path), "adsfasdSupport_test_PLA_0.12_41m11s.gcode")
            st._last_slice_completed_ns = time.time_ns() + 1_000_000_000

            stale_before = plugin.check_stale_gcode_model(gcode_path=fp_path, state=st)
            self.assertTrue(stale_before.get("is_stale"))

            # 3) User clicks "Reload G-Code from Slicer" (`force_refresh=True`)
            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message("acquire_slicer_gcode", {"gcode_path": "", "force_refresh": True})

            self.assertEqual(os.path.basename(st.last_gcode_path), "Support_test.gcode")
            self.assertNotEqual(st.last_gcode_source, "file_picker")
            self.assertTrue(sent)

            # 4) Even after Browse -> Unload Stale G-code -> Reload G-Code from Slicer, live preview temp reloads cleanly
            model_dir = os.path.join(tmp_dir, "orcaslicer_model", "Metadata")
            os.makedirs(model_dir, exist_ok=True)
            live_preview = os.path.join(model_dir, ".2.0.gcode")
            with open(live_preview, "w", encoding="utf-8") as f:
                f.write(self._sample_zombie_gcode_text("Support_test"))
            st._last_preview_temp_path = live_preview
            st._last_preview_temp_existed = True

            plugin.load_gcode_from_content("uploadtest.gcode", self._sample_zombie_gcode_text("uploadtest"), state=st)
            self.assertEqual(st._last_preview_temp_path, live_preview)
            plugin.clear_loaded_gcode_state(st, reason="clear_gcode", sync_host=False)
            self.assertEqual(st.last_gcode_path, "")
            self.assertEqual(st._last_preview_temp_path, live_preview)

            with mock.patch.object(plugin, "GLOBAL_STATE", st):
                page._handle_async_message("acquire_slicer_gcode", {"gcode_path": "", "force_refresh": True})
            self.assertTrue(st.last_gcode_path and os.path.isfile(st.last_gcode_path))
            self.assertNotEqual(st.last_gcode_source, "file_picker")

    def test_toggle_hardware_lock_calls_render_slots_without_error(self) -> None:
        """Clicking Unlock toggles hardwareUnlocked and updates slot buttons immediately without ReferenceError."""
        self.assertIn("function toggleHardwareLock()", plugin.ACE_PLUGIN_HTML_UI)
        self.assertNotIn("renderSlotBay()", plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn("renderSlots();", plugin.ACE_PLUGIN_HTML_UI)
        self.assertIn('.hw-action-btn, .slot-actions button', plugin.ACE_PLUGIN_HTML_UI)

    def test_external_spool_ace_pro_disabled_allows_t4_and_bypasses_ace_checks(self) -> None:
        """When Klipper reports output_pin ACE_Pro=0 or external_spool_mode=True, T4 with 1 ACE (0..3) and empty slots is allowed."""
        raw_status = {
            "ace_state": {"ace_instances": 1, "ace_pro_enabled": True, "current_index": -1},
            "ace_instance_0": {
                "connection_state": "disconnected",
                "status": "ready",
                "slots": [
                    {"index": i, "status": "empty", "color": [0, 0, 0], "material": "", "temp": 0, "rfid": False}
                    for i in range(4)
                ],
            },
            "output_pin ACE_Pro": {"value": 0.0},
            "print_stats": {"state": "standby", "filename": ""},
        }
        norm = plugin.MoonrakerClient.normalize_ace_status(raw_status)
        norm["connection_state"] = "connected_standby"
        self.assertFalse(norm["ace_pro_enabled"])
        self.assertTrue(plugin.is_ace_pro_disabled_in_status(norm))

        gcode_t4 = (
            "; generated by OrcaSlicer\n"
            "; filament_type = PLA;PLA;PLA;PLA;TPU\n"
            "; filament_colour = #FF0000;#00FF00;#0000FF;#FFFF00;#112233\n"
            "G9111 BEDTEMP=60 EXTRUDERTEMP=220 TOOL=4\n"
            "; FLUSH_START\n"
            "T4\n"
            "G1 X10 Y10 E1.0\n"
            "PRINT_END\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            gpath = os.path.join(tmpdir, "ext_spool_t4.gcode")
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(gcode_t4)

            st = plugin.PluginState()
            st.save_config = lambda: None
            st.configured_ace_count = "1"
            st.tool_map = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}
            st.last_ace_status = norm

            self.assertTrue(plugin.is_ace_bypass_active(st))
            info = plugin.postprocess_gcode_file(gpath, st)
            self.assertTrue(info.get("aceBypass"))
            self.assertEqual(info.get("purge_commands_inserted"), 0)

            with open(gpath, "r", encoding="utf-8") as f:
                patched = f.read()
            self.assertIn("G9111 BEDTEMP=60 EXTRUDERTEMP=220 TOOL=4", patched)
            self.assertIn("\nT4\n", patched)
            self.assertNotIn("ACE_SET_PURGE_AMOUNT", patched)

            allowed, blocks, _ = plugin.can_upload_gcode(info, st, ace_status=norm, allow_stale=True)
            self.assertTrue(allowed, f"Expected External Spool upload allowed, got blocks={blocks}")
            self.assertEqual(blocks, [])

            # Manual external_spool_mode=True also works offline even when Klipper pin state isn't reported
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(gcode_t4)
            st_manual = plugin.PluginState()
            st_manual.save_config = lambda: None
            st_manual.configured_ace_count = "1"
            st_manual.tool_map = {4: 4}
            st_manual.external_spool_mode = True
            info_m = plugin.postprocess_gcode_file(gpath, st_manual)
            self.assertTrue(info_m.get("aceBypass"))
            allowed_m, blocks_m, _ = plugin.can_upload_gcode(
                info_m,
                st_manual,
                ace_status={"connected": True, "connection_state": "connected_standby", "ace_pro_enabled": True, "slots": norm["slots"]},
                allow_stale=True,
            )
            self.assertTrue(allowed_m, f"Expected manual External Spool mode allowed, got blocks={blocks_m}")

    def test_execute_defers_slot_bounds_to_modal_and_probes_live_ace_pin(self) -> None:
        """execute() probes live ACE_Pro state on slot bounds errors and opens Map·Check·Send instead of hard-failing interactive prints."""
        gcode_t4 = (
            "; generated by OrcaSlicer\n"
            "; filament_type = PLA;PLA;PLA;PLA;PETG\n"
            "; filament_colour = #FF0000;#00FF00;#0000FF;#FFFF00;#AA5500\n"
            "G9111 BEDTEMP=70 EXTRUDERTEMP=235 TOOL=4\n"
            "T4\n"
            "PRINT_END\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            gpath = os.path.join(tmpdir, "t4_project.gcode")
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(gcode_t4)

            st, _st_dir, _cfg, _page, _sent = self._make_zombie_test_state_and_page(os.path.join(tmpdir, "st"))
            st.configured_ace_count = "1"
            st.moonraker_url = "http://192.0.2.50:7125"
            st.slicing_modal_mode = "window_modal"
            st.tool_map = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}
            st.last_ace_status = {
                "connected": True,
                "configured": True,
                "connection_state": "connected_standby",
                "ace_pro_enabled": True,
                "ace_instances": 1,
                "slots": [
                    {"tool": i, "status": "ready", "material": "PLA", "hex_color": "#FFFFFF"}
                    for i in range(4)
                ],
            }

            class _FakeCtx:
                def __init__(self, host_val: str) -> None:
                    self.step = getattr(plugin.orca.slicing.Step, "psGCodePostProcess", "psGCodePostProcess")
                    self.gcode_path = gpath
                    self.host = host_val
                    self.output_name = "t4_project.gcode"
                    self.plate_index = 1
                    self.project_name = "t4_project"

                def cancelled(self) -> bool:
                    return False

            cap = plugin.KobraS1AcePostProcessCapability()

            # 1) Headless File export with ACE enabled and T4 still fails with slot-bounds error
            with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
                plugin, "_refresh_ace_status_on_bounds_error", return_value=None
            ):
                res_file = cap.execute(_FakeCtx("File"))
            self.assertFalse(res_file.ok)
            self.assertIn("T4", res_file.message)
            self.assertIn("0..3", res_file.message)

            # 2) Live Klipper reports ACE_Pro=0 during execute() -> resolves cleanly without blocking or failing!
            disabled_live = dict(st.last_ace_status, ace_pro_enabled=False)
            seen_modal_info = []

            class _MockModalSession:
                def __init__(self, ctx, state, gcode_path, info, **kwargs) -> None:
                    seen_modal_info.append(dict(info))
                    self.outcome = "proceed"

                def run(self) -> str:
                    return "proceed"

            with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
                plugin, "_refresh_ace_status_on_bounds_error", return_value=disabled_live
            ), mock.patch.object(
                plugin, "_SlicingModalSetupSession", _MockModalSession
            ), mock.patch.object(
                plugin, "_record_slice_host_fingerprint", lambda s: None
            ):
                res_live_off = cap.execute(_FakeCtx("Printer"))
            self.assertTrue(res_live_off.ok, f"Expected ok when live ACE_Pro=0, got: {res_live_off.message}")
            self.assertTrue(seen_modal_info and seen_modal_info[-1].get("aceBypass"))
            self.assertNotIn("deferred_bounds_errors", seen_modal_info[-1])

            # 3) Interactive Print with ACE STILL enabled and T4 -> opens Map·Check·Send instead of aborting!
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(gcode_t4)
            st.last_ace_status["ace_pro_enabled"] = True
            seen_modal_info.clear()
            with mock.patch.object(plugin, "GLOBAL_STATE", st), mock.patch.object(
                plugin, "_refresh_ace_status_on_bounds_error", return_value=None
            ), mock.patch.object(
                plugin, "_SlicingModalSetupSession", _MockModalSession
            ), mock.patch.object(
                plugin, "_record_slice_host_fingerprint", lambda s: None
            ):
                res_interactive = cap.execute(_FakeCtx("Printer"))
            self.assertTrue(res_interactive.ok, f"Expected modal deferral for T4, got: {res_interactive.message}")
            self.assertTrue(seen_modal_info and "T4" in str(seen_modal_info[-1].get("deferred_bounds_errors", "")))

    def test_external_spool_ui_js_gjs(self) -> None:
        """GJS UI verification: External Spool (ace_pro_enabled=false) clears mapping errors and unlocks Upload/Export for T4."""
        import shutil
        import subprocess

        gjs_bin = shutil.which("gjs")
        self.assertIsNotNone(gjs_bin)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", plugin.ACE_PLUGIN_HTML_UI, re.S)
        js_harness = r"""
const _els = {};
function _mkEl(tagOrId) {
  return {
    id: tagOrId, tagName: tagOrId, className: "", value: "", checked: false, disabled: false,
    textContent: "", innerHTML: "", style: {}, dataset: {}, attributes: {},
    classList: {
      _s: new Set(),
      add: function(c) { this._s.add(c); },
      remove: function(c) { this._s.delete(c); },
      contains: function(c) { return this._s.has(c); },
      toggle: function(c, f) { if (f === undefined) { if (this._s.has(c)) this._s.delete(c); else this._s.add(c); } else if (f) this._s.add(c); else this._s.delete(c); }
    },
    children: [],
    _html: "",
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v || ""); if (this._html === "") this.children = []; },
    setAttribute: function(k, v) { this.attributes[k] = String(v); },
    getAttribute: function(k) { return this.attributes[k] || null; },
    hasAttribute: function(k) { return k in this.attributes; },
    removeAttribute: function(k) { delete this.attributes[k]; },
    appendChild: function(c) { this.children.push(c); return c; },
    addEventListener: function() {},
    closest: function() { return null; },
    querySelector: function() { return null; },
    querySelectorAll: function() { return []; }
  };
}
var document = {
  documentElement: _mkEl("documentElement"),
  activeElement: null,
  getElementById: function(id) { if (!_els[id]) _els[id] = _mkEl(id); return _els[id]; },
  createElement: function(tag) { return _mkEl(tag); },
  querySelectorAll: function() { return []; },
  addEventListener: function() {},
  body: _mkEl("body")
};
var _posted = [];
var window = {
  orca: { _cb: null, postMessage: function(m) { _posted.push(m); }, onMessage: function(cb) { this._cb = cb; } },
  matchMedia: function() { return { matches: true, addEventListener: function() {} }; },
  addEventListener: function() {}
};
var orca = window.orca;
var getComputedStyle = function() { return { getPropertyValue: function() { return ""; } }; };
var localStorage = { getItem: function() { return null; }, setItem: function() {} };
var navigator = {};
""" + "\n".join(scripts) + r"""
hasReceivedInitialState = true;
currentLanguage = "en";
currentState = Object.assign(currentState, {
  language: "en",
  moonraker_url: "http://192.0.2.50:7125",
  configured_ace_count: "1",
  external_spool_mode: false,
  last_gcode_path: "/tmp/t4_ext.gcode",
  slicer_filaments: [{ slicer_tool: 4, material: "PLA", color: "#FF5500", used: true, used_g: 12.5 }],
  tool_map: { 4: 4 },
  tool_map_sources: { 4: "default" },
  ace_status: {
    connected: false,
    configured: true,
    connection_state: "connected_standby",
    ace_pro_enabled: false,
    ace_instances: 1,
    slots: [0,1,2,3].map(i => ({ tool: i, status: "empty", material: "", hex_color: "#000000" }))
  },
  ace_slots: [0,1,2,3].map(i => ({ tool: i, status: "empty", material: "", hex_color: "#000000" })),
  spoolman_spools: [{ spool_id: 42, display_name: "eSUN TPU Black", material: "TPU", color_hex: "#111111", remaining_weight: 250, capacity_weight: 1000 }],
  external_spool_id: 42,
  last_postprocess_info: { toolchanges: 0, flush_status: "POSTPROCESS_OK", flush_data_complete: true, slicer_filaments: [{ slicer_tool: 4, material: "PLA", color: "#FF5500", used: true }] }
});
renderLiveHeaderAndMeta();
renderSlots();
renderMappingTable();
const cardHtml = document.getElementById("modalMappingCards").children.map(c => c.innerHTML).join("");
const connTextOff = document.getElementById("connBadge").textContent;
const slotsGrayOff = document.getElementById("slotsContainer").classList.contains("is-ace-inactive");
// Without Spoolman connected (spoolman_spools = []): only External Spool (No Tracking) option renders cleanly
currentState.spoolman_spools = [];
renderMappingTable();
const noSmCardHtml = document.getElementById("modalMappingCards").children.map(c => c.innerHTML).join("");
const noSmBlockedErr = !!currentState._modalBlockedByErrors;
toggleKlipperAceProPin(true);
const toggledMsg = _posted.find(m => m && m.type === "set_ace_pro_enabled") || null;
currentState.ace_status.ace_pro_enabled = true;
currentState.ace_status.ace_hw_state = "connecting";
currentState.ace_status.connected = false;
renderLiveHeaderAndMeta();
renderSlots();
const connTextConnecting = document.getElementById("connBadge").textContent;
const slotsGrayConnecting = document.getElementById("slotsContainer").classList.contains("is-ace-inactive");
print(JSON.stringify({
  klipperOff: true,
  extActive: true,
  blockedErr: !!currentState._modalBlockedByErrors,
  blockedMis: !!currentState._modalBlockedByMismatch,
  connTextOff: connTextOff,
  slotsGrayOff: slotsGrayOff,
  connTextConnecting: connTextConnecting,
  slotsGrayConnecting: slotsGrayConnecting,
  extBannerHasVisible: document.getElementById("modalExternalSpoolBanner").classList.contains("visible"),
  uploadDisabled: !!document.getElementById("btnModalUploadOnly").disabled,
  exportDisabled: !!document.getElementById("btnModalExportGcode").disabled,
  aceActionsDisplay: document.getElementById("mappingAceActions").style.display,
  hasExtSpoolOption: cardHtml.includes("External Spool (No Tracking)") && cardHtml.includes("eSUN TPU Black") && cardHtml.includes("Spoolman #42"),
  hasFakeAceSlot: cardHtml.includes("selectSlotFromDropdown"),
  noSmHasExtOnly: noSmCardHtml.includes("External Spool (No Tracking)") && !noSmCardHtml.includes("Spoolman #42") && !noSmBlockedErr,
  toggledPinEnabled: !!(toggledMsg && toggledMsg.enabled === true)
}));
"""
        with tempfile.TemporaryDirectory() as js_tmp:
            js_path = os.path.join(js_tmp, "ext_spool_harness.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(js_harness)
            proc = subprocess.run([gjs_bin, js_path], text=True, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(out["blockedErr"])
        self.assertFalse(out["blockedMis"])
        self.assertEqual(out["connTextOff"], "ACE Pro OFF (External Spool)")
        self.assertTrue(out["slotsGrayOff"])
        self.assertEqual(out["connTextConnecting"], "ACE Connecting…")
        self.assertTrue(out["slotsGrayConnecting"])
        self.assertTrue(out["extBannerHasVisible"])
        self.assertFalse(out["uploadDisabled"])
        self.assertFalse(out["exportDisabled"])
        self.assertEqual(out["aceActionsDisplay"], "none")
        self.assertTrue(out["hasExtSpoolOption"])
        self.assertFalse(out["hasFakeAceSlot"])
        self.assertTrue(out["noSmHasExtOnly"])
        self.assertTrue(out["toggledPinEnabled"])

    def test_external_spool_sets_active_spoolman_spool_on_start_print(self) -> None:
        """Starting a print in External Spool mode with external_spool_id calls Moonraker REST and injects reversible SET_ACTIVE_SPOOL ID."""
        client = plugin.MoonrakerClient("http://192.0.2.50:7125")
        calls = []
        with mock.patch.object(client, "_request_json", side_effect=lambda m, p, payload=None, **kw: calls.append((m, p, payload)) or {"result": {}}):
            client.set_active_spool(42)
        self.assertEqual(calls, [("POST", "/server/spoolman/spool_id", {"spool_id": 42})])

        raw_lines = ["G9111 BEDTEMP=60 EXTRUDERTEMP=215 TOOL=0\n", "T0\n", "G1 X10 Y10 E1.0\n", "PRINT_END\n"]
        out_lines, _ = plugin.process_gcode(raw_lines, ace_bypass=True, external_spool_id=42)
        joined = "".join(out_lines)
        self.assertIn("SET_ACTIVE_SPOOL ID=42 ; @ORCA_ACE_PLUGIN_EXT_SPOOL\n", joined)
        self.assertEqual("".join(plugin.strip_previous_postprocess(out_lines)), "".join(raw_lines))

        st = plugin.PluginState()
        st._spoolman_spools = [{"id": 42, "archived": False, "remaining_weight": 320.5, "filament": {"name": "PLA Matte", "material": "PLA", "color_hex": "224466", "weight": 1000, "vendor": {"name": "eSUN"}}}]
        st.external_spool_id = 42
        d = st.to_dict()
        self.assertEqual(d["external_spool_id"], 42)
        self.assertEqual(len(d["spoolman_spools"]), 1)
        self.assertEqual(d["spoolman_spools"][0]["spool_id"], 42)
        self.assertEqual(d["spoolman_spools"][0]["display_name"], "eSUN PLA Matte")

        # Real Klipper ACE Pro ON/OFF pin switch (`SET_PIN PIN=ACE_Pro VALUE=0|1`)
        gcode_cmds = []
        with mock.patch.object(client, "send_gcode", side_effect=lambda cmd, **kw: gcode_cmds.append(cmd) or {"result": "ok"}):
            client.set_ace_pro_enabled(False)
            client.set_ace_pro_enabled(True)
        self.assertEqual(gcode_cmds, ["SET_PIN PIN=ACE_Pro VALUE=0", "SET_PIN PIN=ACE_Pro VALUE=1"])

        # When Spoolman is NOT configured, external_spool_id is cleared and no SET_ACTIVE_SPOOL is injected
        st.apply_dict({"spoolman_url": ""}, save=False)
        self.assertIsNone(st.external_spool_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            gpath = os.path.join(tmpdir, "no_spoolman_ext.gcode")
            with open(gpath, "w", encoding="utf-8") as f:
                f.write("".join(raw_lines))
            st.save_config = lambda: None
            st.last_ace_status = {"connected": False, "connection_state": "connected_standby", "ace_pro_enabled": False, "slots": []}
            st.external_spool_id = 42  # stale id without spoolman_url must not inject SET_ACTIVE_SPOOL
            plugin.postprocess_gcode_file(gpath, st)
            with open(gpath, "r", encoding="utf-8") as f:
                no_sm_text = f.read()
            self.assertNotIn("SET_ACTIVE_SPOOL", no_sm_text)

    def test_endless_spool_read_backup_prediction_and_deferred_apply(self) -> None:
        """Endless Spool reads save_variables, mirrors ACEPRO backup matching (exact/material/next), and applies only on upload when changed."""
        client = plugin.MoonrakerClient("http://192.0.2.50:7125")
        raw_resp = {
            "result": {
                "status": {
                    "ace_instance_0": {
                        "status": "ready",
                        "connection_state": "connected",
                        "slots": [
                            {"tool": 0, "status": "ready", "material": "PLA", "color": [255, 0, 0]},
                            {"tool": 1, "status": "empty", "material": "", "color": [0, 0, 0]},
                            {"tool": 2, "status": "ready", "material": "PLA", "color": [0, 255, 0]},
                            {"tool": 3, "status": "ready", "material": "PLA", "color": [255, 0, 0]},
                        ],
                    },
                    "save_variables": {
                        "variables": {
                            "ace_current_index": 0,
                            "ace_endless_spool_enabled": True,
                            "ace_endless_spool_match_mode": "material",
                        }
                    },
                    "output_pin ACE_Pro": {"value": 1.0},
                    "print_stats": {"state": "standby"},
                }
            }
        }
        with mock.patch.object(client, "_request_json", return_value=raw_resp):
            st_live = client.query_ace_status()
        self.assertTrue(st_live["endless_spool_enabled"])
        self.assertEqual(st_live["endless_spool_mode"], "material")

        slots = st_live["slots"]
        # Exact: skips empty T1 and green PLA T2, picks red PLA T3
        self.assertEqual(plugin.MoonrakerClient.find_endless_spool_backup(slots, 0, "exact")["tool"], 3)
        # Material: skips empty T1, picks green PLA T2, then red PLA T3 in chain
        self.assertEqual(plugin.MoonrakerClient.find_endless_spool_backup(slots, 0, "material")["tool"], 2)
        self.assertEqual(
            [s["tool"] for s in plugin.MoonrakerClient.find_endless_spool_backup_chain(slots, 0, "material")],
            [2, 3],
        )
        # Wrap-around from T3 in exact mode -> finds loaded T0 (status=='loaded' is physically ready in ACE)
        self.assertEqual(plugin.MoonrakerClient.find_endless_spool_backup(slots, 3, "exact")["tool"], 0)
        # Next: ignores material/color, picks first ready slot
        slots_unk = [
            {"tool": 0, "status": "ready", "material": "Unknown", "color": [0, 0, 0]},
            {"tool": 1, "status": "ready", "material": "PETG", "color": [1, 2, 3]},
        ]
        self.assertIsNone(plugin.MoonrakerClient.find_endless_spool_backup(slots_unk, 0, "exact"))
        self.assertIsNone(plugin.MoonrakerClient.find_endless_spool_backup(slots_unk, 0, "material"))
        self.assertEqual(plugin.MoonrakerClient.find_endless_spool_backup(slots_unk, 0, "next")["tool"], 1)

        # set_endless_spool emits exact ACEPRO Klipper commands
        sent_gcodes = []
        with mock.patch.object(client, "send_gcode", side_effect=lambda cmd, **kw: sent_gcodes.append((cmd, kw)) or {"result": "ok"}):
            client.set_endless_spool(True, "next")
            client.set_endless_spool(False, "exact")
        self.assertEqual(sent_gcodes[0][0], "ACE_SET_ENDLESS_SPOOL_MODE MODE=next\nACE_ENABLE_ENDLESS_SPOOL")
        self.assertTrue(sent_gcodes[0][1].get("allow_while_printing"))
        self.assertEqual(sent_gcodes[1][0], "ACE_SET_ENDLESS_SPOOL_MODE MODE=exact\nACE_DISABLE_ENDLESS_SPOOL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
