"""Independent business oracles for source-driven, declared-contract paths."""

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

from framework_io import validate_runtime_contract
from framework_paths import audit_framework_paths


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "framework-path-v2"
SOURCE = FIXTURE / "source"
CONTRACT = json.loads((FIXTURE / "runtime-contract.json").read_text(encoding="utf-8"))
CATALOG = json.loads((FIXTURE / "cases.json").read_text(encoding="utf-8"))["cases"]
BATCH_OUTPUTS = ["JOB-STATUS", "ERROR-CAUSE", "RESULT-FLAG", "RECORD-DECISION", "REQUEST-ID"]
SCREEN_OUTPUTS = ["SCREEN-STATUS", "ERROR-CAUSE", "SCREEN-VIEW", "RESULT-FLAG", "RECORD-DECISION", "REQUEST-ID"]


def scenario(name="base"):
    return json.loads((FIXTURE / "scenarios" / (name + ".json")).read_text(encoding="utf-8"))


def io_events(result):
    return [event["io_event"] for event in result["trace"] if event.get("event") == "io_call"]


def functions(result):
    return [event["function_value"] for event in io_events(result)]


class FrameworkAcceptanceTests(unittest.TestCase):
    def audit(self, name="base", entry="BATCHRUN", initial=None, source=SOURCE, contract=None, **options):
        data = scenario(name) if isinstance(name, str) else deepcopy(name)
        return audit_framework_paths(
            source, entry, CONTRACT if contract is None else contract, data,
            initial_values=initial if initial is not None else (
                {"RESTART-FLAG": 0} if entry == "BATCHRUN" else {"REQUEST-ID": "A001"}),
            output_fields=BATCH_OUTPUTS if entry == "BATCHRUN" else SCREEN_OUTPUTS,
            **options,
        )

    def closed_exit(self, result):
        self.assertTrue(result["scope"]["model_path_complete"], result["boundaries"])
        self.assertEqual(result["boundaries"], [])
        self.assertEqual(len(result["root_exits"]), 1)
        return result["root_exits"][0]

    def assert_case(self, case):
        data = json.loads((FIXTURE / case["scenario"]).read_text(encoding="utf-8"))
        if "resume_from" in case:
            prior = next(item for item in CATALOG if item["id"] == case["resume_from"])
            prior_data = json.loads((FIXTURE / prior["scenario"]).read_text(encoding="utf-8"))
            prior_result = self.audit(prior_data, entry=prior["entry"], initial=prior["initial_values"])
            prior_state = self.closed_exit(prior_result)["io_state"]
            self.assertEqual(prior_state["restart_checkpoint"]["key"], ["A001"])
            data["records"] = deepcopy(prior_state["committed_records"])
            data["restart_checkpoint"] = deepcopy(prior_state["restart_checkpoint"])
        result = self.audit(data, entry=case["entry"], initial=case["initial_values"])
        final = self.closed_exit(result)
        for field, expected in case["expected"]["values"].items():
            self.assertEqual(final["values"][field], expected, (case["id"], field))
        self.assertEqual(final["io_state"]["committed_records"], case["expected"]["committed_records"])
        self.assertEqual(final["io_state"]["pending_writes"], case["expected"]["pending_writes"])
        self.assertFalse(final["io_state"]["runtime_verified"])

    def test_contract_and_catalog_have_no_hidden_business_expectations_in_scenarios(self):
        self.assertEqual(validate_runtime_contract(CONTRACT), CONTRACT)
        self.assertEqual(CONTRACT["dataset"]["fields"], {"ID": "string", "TYPE": "string", "STATE": "string"})
        self.assertEqual(len(CATALOG), 15)
        self.assertEqual(len({case["id"] for case in CATALOG}), len(CATALOG))
        for case in CATALOG:
            data = json.loads((FIXTURE / case["scenario"]).read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"records", "external_locks", "faults"})

    def test_first_read_is_processed_before_next_and_two_filters_remain_distinct(self):
        result = self.audit()
        self.closed_exit(result)
        events = io_events(result)
        self.assertEqual(functions(result), [
            "FIRST", "SAVE", "COMMIT", "CHECKPOINT", "NEXT", "NEXT",
            "SAVE", "COMMIT", "CHECKPOINT", "NEXT", "CLOSE",
        ])
        read_keys = [event["record_key"] for event in events
                     if event["function_value"] in {"FIRST", "NEXT"} and "record_key" in event]
        self.assertEqual(read_keys, [["A001"], ["A003"], ["A004"]])
        self.assertEqual([event["record_key"] for event in events if event["function_value"] == "SAVE"],
                         [["A001"], ["A004"]])
        self.assertTrue(all(event["semantics"] == "declared_contract" for event in events))
        self.assertTrue(all(event["runtime_verified"] is False for event in events))

    def test_business_subroutine_is_entered_three_times_and_returns_actual_decisions(self):
        result = self.audit()
        self.closed_exit(result)
        returns = [event for event in result["trace"]
                   if event.get("event") == "call_return" and event.get("callee_program") == "REQUESTCHECK"]
        self.assertEqual(len(returns), 3)
        self.assertEqual([event["writes"]["RECORD-DECISION"] for event in returns], ["WORK", "SKIP", "WORK"])
        self.assertTrue(all(event["caller_program"] == "BATCHRUN" for event in returns))

    def test_copy_control_trace_retains_original_hash_line_and_host_include(self):
        result = self.audit()
        self.closed_exit(result)
        spans = [span for event in result["trace"] for ref in event.get("evidence_refs", [])
                 for span in ref.get("source_spans", [])]
        control_spans = [span for span in spans if span["relative_path"] == "copybooks/BATCHPATH.cpy"]
        self.assertTrue(control_spans)
        raw = (SOURCE / "copybooks/BATCHPATH.cpy").read_bytes()
        host = (SOURCE / "programs/BATCHRUN.cbl").read_text(encoding="utf-8").splitlines()
        include_line = next(index for index, line in enumerate(host, 1) if line == "COPY BATCHPATH.")
        for span in control_spans:
            self.assertEqual(span["source_hash"], hashlib.sha256(raw).hexdigest())
            self.assertGreaterEqual(span["start_line"], 1)
            self.assertLessEqual(span["end_line"], len(raw.decode().splitlines()))
            self.assertEqual(span["include_chain"][0]["relative_path"], "programs/BATCHRUN.cbl")
            self.assertEqual(span["include_chain"][0]["line"], include_line)

    def test_no_file_rows_is_success_without_save_commit_or_checkpoint(self):
        result = self.audit("empty")
        final = self.closed_exit(result)
        self.assertEqual(functions(result), ["FIRST", "CLOSE"])
        self.assertEqual(io_events(result)[0]["status"], "END")
        self.assertEqual(final["io_state"]["restart_checkpoint"], None)

    def test_io_success_is_not_business_permission(self):
        result = self.audit("review-only")
        final = self.closed_exit(result)
        self.assertEqual(functions(result), ["FIRST", "NEXT", "CLOSE"])
        self.assertEqual(io_events(result)[0]["status"], "OK")
        self.assertEqual(final["values"]["RECORD-DECISION"], "SKIP")
        self.assertEqual(final["io_state"]["restart_checkpoint"], None)

    def test_external_lock_is_seen_at_save_not_invented_during_unlocked_read(self):
        result = self.audit("locked")
        self.closed_exit(result)
        self.assertEqual(functions(result), ["FIRST", "SAVE", "ROLLBACK", "CLOSE"])
        self.assertEqual([event["status"] for event in io_events(result)], ["OK", "HELD", "OK", "OK"])

    def test_failed_commit_then_failed_rollback_keeps_pending_record_and_original_cause(self):
        result = self.audit("commit-rollback-failed")
        final = self.closed_exit(result)
        self.assertEqual(functions(result), ["FIRST", "SAVE", "COMMIT", "ROLLBACK", "CLOSE"])
        self.assertEqual(final["io_state"]["pending_writes"], [{"ID": "A001", "TYPE": "SERVICE", "STATE": "DONE"}])
        self.assertEqual(final["io_state"]["committed_records"], scenario()["records"])
        self.assertEqual((final["values"]["JOB-STATUS"], final["values"]["ERROR-CAUSE"]), ("RBER", "CMER"))

    def test_failed_checkpoint_does_not_undo_already_committed_record(self):
        result = self.audit("checkpoint-failed")
        final = self.closed_exit(result)
        self.assertEqual(functions(result), ["FIRST", "SAVE", "COMMIT", "CHECKPOINT", "CLOSE"])
        self.assertEqual(final["io_state"]["committed_records"][0]["STATE"], "DONE")
        self.assertIsNone(final["io_state"]["restart_checkpoint"])

    def checkpoint_run(self):
        first = self.audit("interrupt-after-checkpoint")
        final = self.closed_exit(first)
        self.assertEqual(final["values"]["JOB-STATUS"], "IOER")
        checkpoint = final["io_state"]["restart_checkpoint"]
        self.assertEqual(checkpoint["key"], ["A001"])
        return {"records": deepcopy(final["io_state"]["committed_records"]),
                "restart_checkpoint": deepcopy(checkpoint), "external_locks": [], "faults": []}

    def test_resume_uses_real_prior_checkpoint_and_does_not_repeat_committed_record(self):
        result = self.audit(self.checkpoint_run(), initial={"RESTART-FLAG": 1})
        final = self.closed_exit(result)
        self.assertEqual(functions(result), ["RESUME", "NEXT", "SAVE", "COMMIT", "CHECKPOINT", "NEXT", "CLOSE"])
        self.assertEqual(io_events(result)[0]["record_key"], ["A003"])
        self.assertEqual([event["record_key"] for event in io_events(result) if event["function_value"] == "SAVE"], [["A004"]])
        self.assertEqual([row["ID"] for row in final["io_state"]["committed_records"] if row["STATE"] == "DONE"],
                         ["A001", "A004"])

    def test_restart_mismatches_stop_without_normal_business_return_or_save(self):
        original = self.checkpoint_run()
        for field, replacement in (("contract_hash", "0" * 64), ("dataset_hash", "1" * 64), ("access_path", "OTHER-PATH")):
            with self.subTest(field=field):
                data = deepcopy(original)
                data["restart_checkpoint"][field] = replacement
                result = self.audit(data, initial={"RESTART-FLAG": 1})
                self.assertFalse(result["scope"]["model_path_complete"])
                self.assertTrue(result["boundaries"])
                self.assertEqual(result["root_exits"], [])
                self.assertNotIn("SAVE", functions(result))

    def test_restart_requested_without_checkpoint_stops_before_record_processing(self):
        result = self.audit(initial={"RESTART-FLAG": 1})
        self.assertFalse(result["scope"]["model_path_complete"])
        self.assertEqual(result["root_exits"], [])
        self.assertTrue(any(item["reason"] == "restart_checkpoint_missing" for item in result["boundaries"]))
        self.assertNotIn("SAVE", functions(result))

    def test_online_does_not_inherit_batch_loop_or_checkpoint(self):
        result = self.audit(entry="SCREENRUN")
        final = self.closed_exit(result)
        self.assertEqual(functions(result), ["GET", "SAVE", "COMMIT", "CLOSE"])
        self.assertEqual(final["values"]["SCREEN-VIEW"], "SUMMARY")
        self.assertFalse(any(event.get("kind") == "LOOP_TEST" for event in result["trace"]))

    def test_call_exception_follows_source_handler_and_is_not_success_status(self):
        data = scenario()
        data["faults"] = [{"program": "DATAACCESS", "function": "COMMIT", "occurrence": 1, "outcome": "exception"}]
        result = self.audit(data)
        final = self.closed_exit(result)
        commit = next(event for event in result["trace"]
                      if event.get("event") == "io_call" and event["io_event"]["function_value"] == "COMMIT")
        self.assertEqual(commit["outcome"], "exception")
        self.assertNotIn("status", commit["io_event"])
        self.assertEqual(final["values"]["JOB-STATUS"], "CMER")
        self.assertEqual(final["io_state"]["committed_records"], data["records"])

    def test_step_budget_is_not_reported_as_business_completion(self):
        result = self.audit(max_steps=12)
        self.assertFalse(result["scope"]["model_path_complete"])
        self.assertTrue(result["boundaries"])
        self.assertEqual(result["root_exits"], [])

    def test_closed_synthetic_path_and_claimed_company_profile_are_not_runtime_proof(self):
        for kind in ("synthetic", "company_validated"):
            with self.subTest(kind=kind):
                contract = deepcopy(CONTRACT)
                contract["provenance"]["kind"] = kind
                result = self.audit(contract=contract)
                self.closed_exit(result)
                self.assertTrue(result["scope"]["conditional_on_contract"])
                self.assertFalse(result["scope"]["runtime_verified"])
                self.assertFalse(result["scope"]["full_business_analysis_verified"])

    def test_analysis_does_not_modify_source_contract_or_scenario(self):
        before = {str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in SOURCE.rglob("*") if path.is_file()}
        contract, data = deepcopy(CONTRACT), scenario()
        saved_contract, saved_data = deepcopy(contract), deepcopy(data)
        result = audit_framework_paths(SOURCE, "BATCHRUN", contract, data,
                                       initial_values={"RESTART-FLAG": 0}, output_fields=BATCH_OUTPUTS)
        self.closed_exit(result)
        after = {str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in SOURCE.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(contract, saved_contract)
        self.assertEqual(data, saved_data)

    def test_position_only_first_is_not_reported_as_returning_a_record(self):
        contract = deepcopy(CONTRACT)
        contract["services"][0]["operations"][0]["reads_record"] = False
        result = self.audit(contract=contract)
        final = self.closed_exit(result)
        first, following = io_events(result)[:2]
        self.assertEqual(first["function_value"], "FIRST")
        self.assertNotIn("record_key", first)
        self.assertEqual((following["function_value"], following["record_key"]), ("NEXT", ["A001"]))
        self.assertEqual([row["ID"] for row in final["io_state"]["committed_records"] if row["STATE"] == "DONE"],
                         ["A001", "A004"])

    def test_missing_control_copy_cannot_fall_through_business_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "source"
            shutil.copytree(SOURCE, copy)
            (copy / "copybooks/BATCHPATH.cpy").unlink()
            result = self.audit(source=copy)
        self.assertFalse(result["scope"]["model_path_complete"])
        self.assertTrue(result["boundaries"])
        self.assertEqual(result["root_exits"], [])
        self.assertNotIn("SAVE", functions(result))

    def test_extra_next_in_control_copy_reproduces_first_record_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "source"
            shutil.copytree(SOURCE, copy)
            control = copy / "copybooks/BATCHPATH.cpy"
            control.write_text(control.read_text(encoding="utf-8").replace(
                "PERFORM 2000-FIRST", "PERFORM 2000-FIRST\n        PERFORM 2100-NEXT"), encoding="utf-8")
            result = self.audit(source=copy)
        final = self.closed_exit(result)
        self.assertEqual(final["values"]["JOB-STATUS"], "OK")
        self.assertEqual([row["ID"] for row in final["io_state"]["committed_records"] if row["STATE"] == "DONE"], ["A004"])
        self.assertNotEqual([event["record_key"] for event in io_events(result) if event["function_value"] == "SAVE"],
                            [["A001"], ["A004"]])

    def test_error_overwrite_is_visible_and_cannot_satisfy_independent_error_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "source"
            shutil.copytree(SOURCE, copy)
            control = copy / "copybooks/BATCHPATH.cpy"
            control.write_text(control.read_text(encoding="utf-8").replace(
                "    GOBACK.", "    MOVE 'OK' TO JOB-STATUS.\n    GOBACK."), encoding="utf-8")
            result = self.audit("commit-failed", source=copy)
        final = self.closed_exit(result)
        oracle = next(case for case in CATALOG if case["id"] == "batch-commit-failed")
        self.assertEqual(final["values"]["JOB-STATUS"], "OK")
        self.assertEqual(final["values"]["ERROR-CAUSE"], "CMER")
        self.assertNotEqual(final["values"]["JOB-STATUS"], oracle["expected"]["values"]["JOB-STATUS"])

    def test_changed_callee_condition_changes_caller_processing_not_just_display(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "source"
            shutil.copytree(SOURCE, copy)
            callee = copy / "programs/REQUESTCHECK.cbl"
            callee.write_text(callee.read_text(encoding="utf-8").replace("'PENDING'", "'REVIEW'"), encoding="utf-8")
            result = self.audit(source=copy)
        final = self.closed_exit(result)
        self.assertEqual([row["ID"] for row in final["io_state"]["committed_records"] if row["STATE"] == "DONE"], ["A003"])


def _catalog_test(case):
    def test(self):
        self.assert_case(case)
    return test


for _case in CATALOG:
    setattr(FrameworkAcceptanceTests, "test_case_" + _case["id"].replace("-", "_"), _catalog_test(_case))


if __name__ == "__main__":
    unittest.main()
