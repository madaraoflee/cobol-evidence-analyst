from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework_paths import audit_framework_paths
from framework_projection import projected_index


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "framework-path-v2"


def program(name, data, body, *, signature="", linkage=""):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\n"
            + (f"LINKAGE SECTION.\n{linkage}\n" if linkage else "")
            + f"PROCEDURE DIVISION{signature}.\n{body}\n")


class FrameworkPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def run_source(self, body, *, data="01 RESULT-FLAG PIC 9.\n01 INPUT-FLAG PIC X(4).", **options):
        self.write("root.cbl", program("FLOWROOT", data, body))
        return audit_framework_paths(self.root, "FLOWROOT", output_fields=["RESULT-FLAG"], **options)

    def fixture(self):
        shutil.copytree(FIXTURE / "source", self.root, dirs_exist_ok=True)
        self.contract = json.loads((FIXTURE / "runtime-contract.json").read_text(encoding="utf-8"))
        self.scenario = json.loads((FIXTURE / "scenarios" / "base.json").read_text(encoding="utf-8"))

    def run_batch(self, **options):
        return audit_framework_paths(self.root, "BATCHRUN", self.contract, self.scenario,
                                     initial_values={"RESTART-FLAG": 0}, **options)

    def assert_boundary(self, result, reason):
        self.assertIn(reason, result["summary"]["boundary_counts"], result["boundaries"])
        self.assertFalse(result["scope"]["model_path_complete"])
        self.assertEqual(result["root_exits"], [])

    def test_copy_control_drives_real_subroutine_return_and_dual_provenance(self):
        self.fixture()
        result = self.run_batch(output_fields=["JOB-STATUS", "RESULT-FLAG"])
        self.assertTrue(result["scope"]["model_path_complete"], result["boundaries"])
        self.assertEqual(result["root_exits"][0]["values"], {"JOB-STATUS": "OK", "RESULT-FLAG": 1})
        returned = [event for event in result["trace"] if event["event"] == "call_return"]
        self.assertEqual([event["writes"]["RECORD-DECISION"] for event in returned], ["WORK", "SKIP", "WORK"])
        self.assertTrue(all(event["callee_program"] == "REQUESTCHECK" for event in returned))
        performed = next(event for event in result["trace"] if event.get("role") == "perform_entry")
        ref = performed["evidence_refs"][0]
        self.assertNotIn("evidence_id", ref)
        self.assertEqual(ref["source_spans"][0]["relative_path"], "copybooks/BATCHPATH.cpy")
        self.assertEqual(ref["source_spans"][0]["include_chain"][0]["relative_path"], "programs/BATCHRUN.cbl")
        self.assertEqual(ref["derived_location"]["snapshot_id"], result["derived_snapshot_id"])
        self.assertFalse(result["scope"]["all_paths_explored"])
        self.assertFalse(result["scope"]["runtime_verified"])

    def test_text_comparison_right_pads_and_space_is_not_business_success(self):
        result = self.run_source("MOVE SPACE TO INPUT-FLAG.\nIF INPUT-FLAG = 'GOOD'\n"
                                 "MOVE 9 TO RESULT-FLAG\nELSE\nMOVE 1 TO RESULT-FLAG\nEND-IF.\nGOBACK.")
        self.assertEqual(result["root_exits"][0]["values"]["RESULT-FLAG"], 1)
        result = self.run_source("IF INPUT-FLAG = 'A'\nMOVE 1 TO RESULT-FLAG\n"
                                 "ELSE\nMOVE 9 TO RESULT-FLAG\nEND-IF.\nGOBACK.", initial_values={"INPUT-FLAG": "A"})
        self.assertEqual(result["root_exits"][0]["values"]["RESULT-FLAG"], 1)

    def test_scalar_ordinary_reference_and_content_returns_are_distinct(self):
        self.write("child.cbl", program("CHILDSTEP", "", "MOVE 7 TO OUTPUT-FLAG.\nGOBACK.",
                                        signature=" USING OUTPUT-FLAG", linkage="01 OUTPUT-FLAG PIC 9."))
        for mode, expected in (("REFERENCE", 7), ("CONTENT", 1)):
            with self.subTest(mode=mode):
                result = self.run_source(f"MOVE 1 TO RESULT-FLAG.\nCALL 'CHILDSTEP' USING BY {mode} RESULT-FLAG\n"
                                         "ON EXCEPTION MOVE 9 TO RESULT-FLAG END-CALL.\nGOBACK.")
                self.assertTrue(result["scope"]["model_path_complete"], result["boundaries"])
                self.assertEqual(result["root_exits"][0]["values"]["RESULT-FLAG"], expected)
                returned = next(event for event in result["trace"] if event["event"] == "call_return")
                self.assertEqual(returned["writes"], {"RESULT-FLAG": 7} if mode == "REFERENCE" else {})

    def test_by_value_binary_scalar_is_copyin_only(self):
        self.write("child.cbl", program("CHILDSTEP", "", "MOVE 7 TO OUTPUT-FLAG.\nGOBACK.",
                                        signature=" USING BY VALUE OUTPUT-FLAG", linkage="01 OUTPUT-FLAG PIC 9(4) COMP-5."))
        result = self.run_source("MOVE 1 TO RESULT-FLAG.\nCALL 'CHILDSTEP' USING BY VALUE RESULT-FLAG\n"
                                 "ON EXCEPTION MOVE 9 TO RESULT-FLAG END-CALL.\nGOBACK.",
                                 data="01 RESULT-FLAG PIC 9(4) COMP-5.")
        self.assertEqual(result["root_exits"][0]["values"]["RESULT-FLAG"], 1)

    def test_group_parameters_bind_by_layout_and_copy_back_only_the_group(self):
        self.write("child.cbl", program("CHILDSTEP", "", "MOVE 'WORK' TO CHILD-STATE.\nMOVE 7 TO CHILD-FLAG.\nGOBACK.",
                                        signature=" USING CHILD-AREA", linkage="01 CHILD-AREA.\n05 CHILD-STATE PIC X(4).\n05 CHILD-FLAG PIC 9."))
        result = self.run_source("MOVE 'SKIP' TO INPUT-FLAG.\nMOVE 1 TO RESULT-FLAG.\n"
                                 "CALL 'CHILDSTEP' USING ROOT-AREA ON EXCEPTION CONTINUE END-CALL.\nGOBACK.",
                                 data="01 ROOT-AREA.\n05 INPUT-FLAG PIC X(4).\n05 RESULT-FLAG PIC 9.\n01 UNRELATED-FLAG PIC 9.")
        self.assertEqual(result["root_exits"][0]["values"]["RESULT-FLAG"], 7)
        call = next(event for event in result["trace"] if event["event"] == "call_enter")
        self.assertEqual(call["passed_values"], {"INPUT-FLAG": "SKIP", "RESULT-FLAG": 1})
        self.assertTrue(all(binding["evidence_refs"] for binding in call["bindings"]))
        returned = next(event for event in result["trace"] if event["event"] == "call_return")
        self.assertEqual(returned["writes"], {"INPUT-FLAG": "WORK", "RESULT-FLAG": 7})

    def test_ordinary_callee_working_storage_persists_between_nonrecursive_calls(self):
        self.write("child.cbl", program("CHILDSTEP", "01 SAVED-FLAG PIC 9.",
                                        "IF PHASE-FLAG = 1\nMOVE 7 TO SAVED-FLAG\nEND-IF.\nMOVE SAVED-FLAG TO OUTPUT-FLAG.\nGOBACK.",
                                        signature=" USING PHASE-FLAG OUTPUT-FLAG",
                                        linkage="01 PHASE-FLAG PIC 9.\n01 OUTPUT-FLAG PIC 9."))
        result = self.run_source("MOVE 1 TO INPUT-NUMBER.\nMOVE 0 TO RESULT-FLAG.\n"
                                 "CALL 'CHILDSTEP' USING INPUT-NUMBER RESULT-FLAG ON EXCEPTION CONTINUE END-CALL.\n"
                                 "MOVE 0 TO INPUT-NUMBER RESULT-FLAG.\n"
                                 "CALL 'CHILDSTEP' USING INPUT-NUMBER RESULT-FLAG ON EXCEPTION CONTINUE END-CALL.\nGOBACK.",
                                 data="01 INPUT-NUMBER PIC 9.\n01 RESULT-FLAG PIC 9.")
        self.assertEqual(result["root_exits"][0]["values"]["RESULT-FLAG"], 7)
        self.assertEqual(result["summary"]["source_calls"], 2)

    def test_alias_signature_layout_and_unbound_linkage_are_boundaries(self):
        self.write("child.cbl", program("CHILDSTEP", "", "MOVE 7 TO OUTPUT-FLAG.\nGOBACK.",
                                        signature=" USING INPUT-FLAG OUTPUT-FLAG",
                                        linkage="01 INPUT-FLAG PIC 9.\n01 OUTPUT-FLAG PIC 9."))
        result = self.run_source("MOVE 1 TO RESULT-FLAG.\nCALL 'CHILDSTEP' USING RESULT-FLAG RESULT-FLAG\n"
                                 "ON EXCEPTION CONTINUE END-CALL.\nGOBACK.")
        self.assert_boundary(result, "overlapping_argument_storage_not_supported")
        self.write("child.cbl", program("CHILDSTEP", "", "MOVE 7 TO EXTRA-FLAG.\nGOBACK.",
                                        signature=" USING OUTPUT-FLAG", linkage="01 OUTPUT-FLAG PIC 9.\n01 EXTRA-FLAG PIC 9."))
        result = self.run_source("MOVE 1 TO RESULT-FLAG.\nCALL 'CHILDSTEP' USING RESULT-FLAG\n"
                                 "ON EXCEPTION CONTINUE END-CALL.\nGOBACK.")
        self.assert_boundary(result, "unbound_linkage_storage_not_supported")

    def test_unknown_condition_and_uninitialized_value_stop_instead_of_guessing(self):
        result = self.run_source("IF INPUT-FLAG = 'A'\nMOVE 1 TO RESULT-FLAG\nEND-IF.\nGOBACK.")
        self.assert_boundary(result, "condition_value_not_resolved")
        result = self.run_source("IF INPUT-FLAG = 'A'\nMOVE 1 TO RESULT-FLAG\nEND-IF.\nGOBACK.",
                                 data="01 RESULT-FLAG PIC 9.\n01 INPUT-FLAG PIC X(4) VALUE 'A'.")
        self.assert_boundary(result, "condition_value_not_resolved")

    def test_loop_test_executes_and_step_budget_stops_infinite_loop(self):
        result = self.run_source("MOVE 0 TO RESULT-FLAG.\nPERFORM UNTIL RESULT-FLAG = 1\n"
                                 "MOVE 1 TO RESULT-FLAG\nEND-PERFORM.\nGOBACK.")
        self.assertEqual(result["root_exits"][0]["values"]["RESULT-FLAG"], 1)
        self.assertEqual(result["summary"]["loop_tests"], 2)
        result = self.run_source("MOVE 0 TO RESULT-FLAG.\nPERFORM UNTIL RESULT-FLAG = 1\n"
                                 "CONTINUE\nEND-PERFORM.\nGOBACK.", max_steps=12)
        self.assert_boundary(result, "framework_step_budget_exhausted")
        self.assertTrue(result["summary"]["truncated"])

    def test_unsupported_source_stops_but_unreached_unsupported_paragraph_does_not(self):
        result = self.run_source("MOVE 0 TO RESULT-FLAG.\nADD 1 TO RESULT-FLAG.\nGOBACK.")
        self.assert_boundary(result, "statement_form_not_supported")
        result = self.run_source("MOVE 1 TO RESULT-FLAG.\nGOBACK.\nUNREACHED.\nADD 1 TO RESULT-FLAG.")
        self.assertTrue(result["scope"]["model_path_complete"])

    def test_numeric_overflow_text_truncation_and_collation_stop(self):
        result = self.run_source("MOVE 10 TO RESULT-FLAG.\nGOBACK.")
        self.assert_boundary(result, "numeric_move_not_exactly_representable")
        result = self.run_source("MOVE 'ABCDE' TO INPUT-FLAG.\nGOBACK.")
        self.assert_boundary(result, "text_move_truncation_not_modeled")
        result = self.run_source("MOVE 'A' TO INPUT-FLAG.\nIF INPUT-FLAG > 'B'\n"
                                 "MOVE 1 TO RESULT-FLAG\nEND-IF.\nGOBACK.")
        self.assert_boundary(result, "text_collating_order_not_modeled")

    def test_dynamic_recursive_and_missing_source_calls_remain_boundaries(self):
        for body, reason in (("CALL INPUT-FLAG ON EXCEPTION CONTINUE END-CALL.", "dynamic_call_target_not_modeled"),
                             ("CALL 'FLOWROOT' ON EXCEPTION CONTINUE END-CALL.", "recursive_source_call_not_modeled"),
                             ("CALL 'ABSENTSTEP' ON EXCEPTION CONTINUE END-CALL.", "uncontracted_external_call")):
            with self.subTest(reason=reason):
                self.assert_boundary(self.run_source(body + "\nGOBACK."), reason)

    def test_missing_root_and_callee_copy_do_not_run_remaining_code(self):
        self.assert_boundary(self.run_source("COPY ABSENTCONTROL.\nMOVE 1 TO RESULT-FLAG.\nGOBACK."),
                             "root_source_expansion_incomplete")
        self.write("child.cbl", program("CHILDSTEP", "", "COPY ABSENTCONTROL.\nGOBACK."))
        result = self.run_source("CALL 'CHILDSTEP' ON EXCEPTION CONTINUE END-CALL.\nMOVE 1 TO RESULT-FLAG.\nGOBACK.")
        self.assert_boundary(result, "callee_source_expansion_incomplete")
        self.assertFalse(any(event.get("writes", {}).get("RESULT-FLAG") == 1 for event in result["trace"]))

    def test_stop_run_is_not_misreported_as_ordinary_return(self):
        self.assert_boundary(self.run_source("MOVE 1 TO RESULT-FLAG.\nSTOP RUN."), "stop_run_scope_not_modeled")

    def test_external_service_cannot_override_existing_source_program(self):
        self.fixture()
        self.write("access.cbl", program("DATAACCESS", "", "COPY ABSENTCONTROL.\nGOBACK.",
                                         signature=" USING AREA", linkage="01 AREA PIC X(46)."))
        result = self.run_batch()
        self.assert_boundary(result, "callee_source_expansion_incomplete")
        self.assertEqual(result["summary"]["io_calls"], 0)

    def test_external_call_requires_reference_group_and_in_group_fields(self):
        self.fixture()
        source = self.root / "programs" / "BATCHRUN.cbl"
        original = source.read_text(encoding="utf-8")
        source.write_text(original.replace("USING ACCESS-AREA", "USING BY CONTENT ACCESS-AREA"), encoding="utf-8")
        self.assert_boundary(self.run_batch(), "io_call_argument_contract_mismatch")
        source.write_text(original, encoding="utf-8")
        self.contract["services"][0]["status_field"] = "JOB-STATUS"
        self.assert_boundary(self.run_batch(), "io_contract_fields_outside_argument_group")

    def test_contract_function_lengths_and_return_record_lengths_are_checked(self):
        self.fixture()
        self.contract["services"][0]["operations"][0]["value"] = "OVERLONGFUNCTION"
        self.assert_boundary(self.run_batch(), "io_function_layout_contract_mismatch")
        self.fixture()
        self.scenario["records"][0]["STATE"] = "STATE-TOO-LONG"
        self.assert_boundary(self.run_batch(), "text_move_truncation_not_modeled")

    def test_contract_padding_collisions_and_noncanonical_records_stop(self):
        self.fixture()
        self.contract["statuses"]["eof"] = "OK "
        self.assert_boundary(self.run_batch(), "io_status_values_collide_after_storage_padding")
        self.fixture()
        self.contract["services"][0]["operations"][0]["value"] = "FIRST "
        self.assert_boundary(self.run_batch(), "io_function_padding_or_encoding_not_modeled")
        self.fixture()
        self.scenario["records"][0]["STATE"] = "PENDING "
        self.assert_boundary(self.run_batch(), "io_record_padding_or_encoding_not_modeled")

    def test_non_ascii_storage_and_conversions_do_not_guess_byte_layout(self):
        result = self.run_source("MOVE '文' TO INPUT-FLAG.\nGOBACK.")
        self.assert_boundary(result, "text_encoding_storage_not_modeled")
        result = self.run_source("MOVE 1 TO INPUT-FLAG.\nGOBACK.")
        self.assert_boundary(result, "scalar_category_conversion_not_supported")

    def test_scenario_invocation_exception_does_not_apply_io_return_values(self):
        self.fixture()
        self.scenario["faults"] = [{"program": "DATAACCESS", "function": "FIRST", "occurrence": 1, "outcome": "exception"}]
        result = self.run_batch()
        first = next(event for event in result["trace"] if event["event"] == "io_call")
        self.assertEqual(first["outcome"], "exception")
        self.assertEqual(first["writes"], {})
        self.assertEqual(result["root_exits"][0]["values"]["JOB-STATUS"], "IOER")

    def test_repeated_results_are_stable_and_sources_are_unchanged(self):
        self.fixture()
        before = {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*") if path.is_file()}
        first, second = self.run_batch(), self.run_batch()
        after = {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(first, second)
        self.assertEqual(before, after)

    def test_projection_detects_original_change_before_result_delivery(self):
        self.write("root.cbl", program("FLOWROOT", "01 RESULT-FLAG PIC 9.", "GOBACK."))
        with self.assertRaisesRegex(ValueError, "Source changed"):
            with projected_index(self.root, "FLOWROOT"):
                self.write("root.cbl", program("FLOWROOT", "01 RESULT-FLAG PIC 9.", "MOVE 1 TO RESULT-FLAG.\nGOBACK."))

    def test_argument_validation_is_bounded_and_preserves_caller_objects(self):
        self.fixture()
        before_contract, before_scenario = deepcopy(self.contract), deepcopy(self.scenario)
        self.run_batch()
        self.assertEqual(self.contract, before_contract)
        self.assertEqual(self.scenario, before_scenario)
        for options in ({"max_steps": True}, {"max_steps": 0}, {"max_call_depth": 99},
                        {"initial_values": {"RESTART-FLAG": True}}, {"output_fields": [["JOB-STATUS"]]},
                        {"initial_values": {"RESTART-FLAG": 10**18}}, {"initial_values": {"INPUT-FLAG": "a" * 2049}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                audit_framework_paths(self.root, "BATCHRUN", **options)


if __name__ == "__main__":
    unittest.main()
