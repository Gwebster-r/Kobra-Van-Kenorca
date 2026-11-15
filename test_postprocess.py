#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validation & Unit Test Suite for `kobra_s1_ace_plugin.py`.

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

import hashlib
import io
import math
import os
import re
import socket
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

import kobra_s1_ace_plugin as plugin


def _forbid_network(*args, **kwargs):
    raise AssertionError("Network access is strictly forbidden during offline unit tests!")


class TestKobraS1AcePlugin(unittest.TestCase):
    def setUp(self) -> None:
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
        self.assertIn("ACE_SET_PURGE_AMOUNT PURGELENGTH=0.00\nT5 ; change extruder\n", head_snippet)
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
        self.assertIn(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_0_to_2:.2f}\nT1 ; switch 0 -> 2\n", full_text)
        self.assertIn(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_2_to_3:.2f}\nT6 ; switch 2 -> 3\n", full_text)
        self.assertIn(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_3_to_1:.2f}\nT7 ; switch 3 -> 1\n", full_text)
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
        self.assertNotIn("fs_read", plugin.orca.last_requested_permissions)
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
        self.assertIn(f"ACE_SET_PURGE_AMOUNT PURGELENGTH={expected_len:.2f}\n", out_n)
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
        self.assertIn("G1 X10\nACE_SET_PURGE_AMOUNT PURGELENGTH=24.95\nT3\nCUT_TIP\n", text)

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
            plugin.GLOBAL_STATE.moonraker_url = "http://192.168.55.99:7125"
            plug = plugin.KobraS1AcePlugin()
            plug.register_capabilities()
            mock_load.assert_called_once()
            perms_kwargs = mock_perms.call_args.kwargs
            self.assertIn("http://192.168.55.99:7125", perms_kwargs["network_http"])
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
            "spool-ring",
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
            self.assertIn("G31", plugin.t("ui.modal.opt.g31", lang=lang))
            self.assertIn("PRINT_END CUT_TIP=1", plugin.t("ui.modal.opt.cut_post", lang=lang))
            self.assertIn("G9111", plugin.t("ui.sec5.opt_skip_purge_sub", lang=lang))
            self.assertIn("SKIP_PURGE_FOR_ALREADY_LOADED_TOOL", plugin.t("ui.modal.opt.skip_purge", lang=lang))
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
                    "moonraker_url": "http://192.168.8.199:7125",
                    "api_key": "secret-token-123",
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
            self.assertEqual(st1.moonraker_url, "http://192.168.8.199:7125")
            self.assertEqual(st1.api_key, "secret-token-123")
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
            self.assertEqual(st2.moonraker_url, "http://192.168.8.199:7125")
            self.assertEqual(st2.api_key, "secret-token-123")
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
        self.assertIn("T${tIdx} · ${matRaw}", html)

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
        self.assertIn("btnUpload.disabled = blockedByMismatch || blockedByErrors;", html)
        self.assertIn("btnSend.disabled = blockedByMismatch || blockedByErrors || isPrinting;", html)
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
        self.assertIn("🔍</button>", html)
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

                # Explicit Load from Slicer (force_refresh=True) recovers newest staged slice cleanly
                page._handle_async_message(
                    "acquire_slicer_gcode",
                    {"gcode_path": plate1, "force_refresh": True},
                )
                self.assertEqual(st.last_gcode_path, plate2)
                self.assertEqual(st.cleared_before_mtime_ns, 0)

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
            mock_client.active_url = "http://192.168.8.80:7125"
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

        # Verify request_permissions still does not request fs_read/fs_readwrite and preserves network_http
        pkg = plugin.KobraS1AcePlugin()
        with mock.patch.object(plugin.orca, "register_capability"):
            pkg.register_capabilities()
        self.assertTrue(plugin.orca.last_requested_permissions.get("threading"))
        self.assertIn("network_http", plugin.orca.last_requested_permissions)
        self.assertNotIn("fs_read", plugin.orca.last_requested_permissions)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
