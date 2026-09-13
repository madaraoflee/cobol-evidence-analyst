"""Independent source/contract precedence and lifetime regressions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework_paths import audit_framework_paths


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "framework-path-v2"


class FrameworkPathAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()

    def fixture(self):
        shutil.copytree(FIXTURE / "source", self.root, dirs_exist_ok=True)
        contract = json.loads((FIXTURE / "runtime-contract.json").read_text(encoding="utf-8"))
        scenario = json.loads((FIXTURE / "scenarios" / "base.json").read_text(encoding="utf-8"))
        return contract, scenario

    def test_case_normalized_call_cannot_replace_present_source_with_contract(self):
        contract, scenario = self.fixture()
        caller = self.root / "programs" / "BATCHRUN.cbl"
        caller.write_text(caller.read_text(encoding="utf-8").replace("CALL 'DATAACCESS'", "CALL 'dataaccess'"), encoding="utf-8")
        (self.root / "access.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DATAACCESS.\n"
            "DATA DIVISION.\nLINKAGE SECTION.\n01 AREA PIC X(46).\n"
            "PROCEDURE DIVISION USING AREA.\nCOPY ABSENTCONTROL.\nGOBACK.\n", encoding="utf-8")
        result = audit_framework_paths(self.root, "BATCHRUN", contract, scenario,
                                       initial_values={"RESTART-FLAG": 0}, output_fields=["JOB-STATUS"])
        self.assertFalse(result["scope"]["model_path_complete"])
        self.assertEqual(result["summary"]["io_calls"], 0)
        self.assertEqual(result["root_exits"], [])
        self.assertIn("callee_source_expansion_incomplete", result["summary"]["boundary_counts"])

    def initial_program(self, header):
        (self.root / "root.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. FLOWROOT.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 INPUT-FLAG PIC 9.\n01 RESULT-FLAG PIC 9.\nPROCEDURE DIVISION.\n"
            "MOVE 1 TO INPUT-FLAG.\nCALL 'CHILDSTEP' USING INPUT-FLAG RESULT-FLAG END-CALL.\n"
            "MOVE 0 TO INPUT-FLAG.\nCALL 'CHILDSTEP' USING INPUT-FLAG RESULT-FLAG END-CALL.\nGOBACK.\n", encoding="utf-8")
        (self.root / "child.cbl").write_text(
            "IDENTIFICATION DIVISION.\n" + header + "\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 SAVED-FLAG PIC 9 VALUE 0.\nLINKAGE SECTION.\n01 INPUT-FLAG PIC 9.\n01 OUTPUT-FLAG PIC 9.\n"
            "PROCEDURE DIVISION USING INPUT-FLAG OUTPUT-FLAG.\nIF INPUT-FLAG = 1\n"
            "MOVE 7 TO SAVED-FLAG\nEND-IF.\nMOVE SAVED-FLAG TO OUTPUT-FLAG.\nGOBACK.\n", encoding="utf-8")
        return audit_framework_paths(self.root, "FLOWROOT", output_fields=["RESULT-FLAG"])

    def test_initial_program_cannot_close_with_previous_call_working_storage(self):
        result = self.initial_program("PROGRAM-ID. CHILDSTEP IS INITIAL.")
        self.assertFalse(result["scope"]["model_path_complete"], result["root_exits"])
        self.assertTrue(result["boundaries"])
        self.assertEqual(result["root_exits"], [])

    def test_multiline_initial_clause_cannot_evade_lifetime_boundary(self):
        result = self.initial_program("PROGRAM-ID. CHILDSTEP\nIS INITIAL.")
        self.assertFalse(result["scope"]["model_path_complete"], result["root_exits"])
        self.assertTrue(result["boundaries"])
        self.assertEqual(result["root_exits"], [])

    def test_every_reported_original_span_and_include_chain_matches_source_bytes(self):
        contract, scenario = self.fixture()
        result = audit_framework_paths(self.root, "BATCHRUN", contract, scenario,
                                       initial_values={"RESTART-FLAG": 0}, output_fields=["JOB-STATUS"])
        self.assertTrue(result["scope"]["model_path_complete"], result["boundaries"])
        referenced = 0
        included = 0

        def inspect(value):
            nonlocal referenced, included
            if isinstance(value, dict):
                if "derived_evidence_id" in value:
                    self.assertNotIn("evidence_id", value)
                    self.assertEqual(value["source_snapshot_id"], result["source_snapshot_id"])
                    self.assertEqual(value["derived_location"]["snapshot_id"], result["derived_snapshot_id"])
                    for span in value["source_spans"]:
                        raw = (self.root / span["relative_path"]).read_bytes()
                        self.assertEqual(hashlib.sha256(raw).hexdigest(), span["source_hash"])
                        self.assertTrue(1 <= span["start_line"] <= span["end_line"] <= len(raw.splitlines()))
                        referenced += 1
                        for site in span["include_chain"]:
                            original = (self.root / site["relative_path"]).read_bytes()
                            self.assertEqual(hashlib.sha256(original).hexdigest(), site["source_hash"])
                            self.assertIn(b"COPY", original.splitlines()[site["line"] - 1].upper())
                            included += 1
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)

        inspect(result)
        self.assertGreater(referenced, 10)
        self.assertGreater(included, 3)


if __name__ == "__main__":
    unittest.main()
