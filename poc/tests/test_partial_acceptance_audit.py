"""Partial diagnostics cannot be promoted or joined across answer identities."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.partial_acceptance_audit import audit_report, classify_gap, main, trace_index
from tools.acceptance_diagnostics import capture_diagnostics


def sample(**changes):
    answer_hash = hashlib.sha256(b"OUT-AMOUNT = IN-AMOUNT * 2.").hexdigest()
    return {"id": "formula", "turn_id": "1", "sample_index": 1, "run_id": "run-current",
        "answer_sha256": answer_hash, "agent_status": "PARTIAL", "stop_reason": "answer_incomplete",
        "checks": {"quality_formula_supported": True, "completeness_source_supplied": True},
        "claim_ledger": {"answer_sha256": answer_hash, "scope": "source_statement_syntax", "entries": []},
        **changes}


def trace(row, **changes):
    return {"schema_version": "business-quality-trace/v1", "run_id": row["run_id"],
        "claim_ledger": row["claim_ledger"], "final": {"stop_reason": row.get("stop_reason"),
            "answer_completion": {"status": "incomplete", "missing_aspects": ["conditions"]},
            "question_investigation": {"required_items": [{"kind": "conditions", "status": "SATISFIED"}],
                "open_gaps": [{"kind": "inputs", "reason": "input_source_not_located"}]}}, **changes}


class PartialAcceptanceAuditTests(unittest.TestCase):
    def test_mechanism_passes_do_not_close_missing_native_diagnostics(self):
        report = audit_report({"cases": [sample()]})
        row = report["samples"][0]
        self.assertTrue(row["mechanism_assertions_passed"])
        self.assertFalse(row["root_cause_closed"])
        self.assertEqual(report["diagnostic_complete_count"], 0)
        self.assertEqual(row["recorded_stop_reason"], "answer_incomplete")
        self.assertIsNone(row["required_items"])
        self.assertIn("answer_completion", row["missing_diagnostic_fields"])

    def test_other_run_with_identical_answer_is_never_attached(self):
        row = sample()
        other = trace(row, run_id="run-old")
        result = audit_report({"cases": [row]}, traces={"run-old": {"data": other, "sha256": "a" * 64}})
        self.assertEqual(result["samples"][0]["diagnostic_origin"]["kind"], "not_archived")

    def test_same_run_wrong_answer_cannot_supply_diagnostics(self):
        row = sample()
        wrong = trace(row)
        wrong["claim_ledger"] = {"answer_sha256": "b" * 64}
        with self.assertRaisesRegex(ValueError, "TRACE_ANSWER_IDENTITY_MISMATCH"):
            audit_report({"cases": [row]}, traces={row["run_id"]: {"data": wrong, "sha256": "a" * 64}})

    def test_supplied_conditions_can_still_be_unexplained(self):
        row = sample()
        result = audit_report({"cases": [row]}, traces={row["run_id"]: {"data": trace(row), "sha256": "a" * 64}})
        observation = result["samples"][0]
        self.assertEqual(observation["answer_completion"]["missing_aspects"], ["conditions"])
        self.assertEqual(observation["required_items"][0]["status"], "SATISFIED")
        self.assertIn("supplied_aspects_not_explained_by_answer", observation["reason_categories"])
        self.assertEqual(result["diagnostic_complete_count"], 1)
        self.assertFalse(observation["root_cause_closed"])

    def captured_row(self, *, native=True):
        row = sample(answer="OUT-AMOUNT = IN-AMOUNT * 2.")
        record = trace(row)
        row["acceptance_diagnostics"] = capture_diagnostics(record, {"answer": row["answer"],
            "status": row["agent_status"], "stop_reason": row["stop_reason"],
            "claim_ledger": row["claim_ledger"]}, trace_sha256="8" * 64)
        if not native:
            row.pop("answer_sha256")
            row.pop("sample_index")
        return row

    def test_actual_executable_capture_resolves_office_report_diagnostics_without_external_trace(self):
        row = self.captured_row()
        original = audit_report({"cases": [row]})
        self.assertEqual(original["diagnostic_complete_count"], 1)
        observed = original["samples"][0]
        self.assertEqual(observed["diagnostic_origin"]["kind"], "same_report_executable_capture")
        self.assertEqual(observed["answer_completion"]["missing_aspects"], ["conditions"])
        self.assertFalse(observed["root_cause_closed"])

    def test_live_row_without_top_level_hash_is_bound_to_actual_answer(self):
        row = self.captured_row(native=False)
        observed = audit_report({"cases": [row]})["samples"][0]
        self.assertEqual(observed["answer_sha256"], hashlib.sha256(row["answer"].encode()).hexdigest())
        self.assertEqual(observed["diagnostic_origin"]["kind"], "same_report_executable_capture")

    def test_capture_different_run_answer_final_or_status_cannot_be_attached(self):
        original = self.captured_row()
        changes = (("run_id", "other"), ("answer_sha256", "0" * 64),
                   ("agent_status", "ANALYZED"), ("trace_sha256", "invalid"),
                   ("final_sha256", "0" * 64), ("stop_reason", "sufficient_material"))
        for key, value in changes:
            row = copy.deepcopy(original)
            row["acceptance_diagnostics"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "CAPTURED_DIAGNOSTIC_IDENTITY_MISMATCH"):
                audit_report({"cases": [row]})
        original["answer"] = "changed final answer"
        with self.assertRaisesRegex(ValueError, "REPORT_ANSWER_HASH_MISMATCH"):
            audit_report({"cases": [original]})

    def test_local_repair_keeps_original_contradiction_distinct_from_visible_replacement(self):
        row = sample()
        row["claim_ledger"]["entries"] = [{"claim_id": "claim-1", "support_status": "contradicted",
            "reason_code": "SOURCE_STATEMENT_MISMATCH", "text": "OUT-AMOUNT = IN-AMOUNT * 999.",
            "revised_claims": [{"support_status": "supported"}]},
            {"claim_id": "claim-2", "support_status": "unsupported", "reason_code": "PROSE_SEMANTICS_NOT_SUPPORTED"}]
        observed = audit_report({"cases": [row]})["samples"][0]["claim_observations"]
        self.assertTrue(observed[0]["supported_local_replacement_recorded"])
        self.assertFalse(observed[0]["final_visible_claim_is_unresolved"])
        self.assertTrue(observed[1]["final_visible_claim_is_unresolved"])

    def test_search_failure_and_candidate_cycle_are_not_proof_of_runtime_unknown(self):
        self.assertEqual(classify_gap({"reason": "input_source_not_located"}),
                         "input_origin_search_incomplete_or_value_unspecified")
        self.assertEqual(classify_gap({"reason": "input_dependency_cycle"}),
                         "candidate_cycle_requires_order_and_flow_review")
        self.assertEqual(classify_gap({"reason": "formula_group_budget"}), "local_candidate_frontier_incomplete")
        self.assertEqual(classify_gap({"reason": "external_implementation_unavailable"}), "external_semantics_unresolved")

    def test_missing_runtime_detail_and_all_conversation_turns_are_retained(self):
        row = sample(id="conversation", stop_reason=None, claim_ledger={}, turns=[
            sample(id="conversation", turn_id=str(index), run_id="turn-" + str(index)) for index in (1, 2, 3)])
        observed = audit_report({"cases": [row]})["samples"][0]
        self.assertEqual(len(observed["turns"]), 3)
        self.assertIn("stop_reason", observed["missing_diagnostic_fields"])
        self.assertIn("partial_reason_not_archived", observed["reason_categories"])

    def test_large_plan_keeps_count_and_hash_when_paths_are_previewed(self):
        row = sample()
        record = trace(row)
        paths = ["member-" + str(index) + ".cbl" for index in range(12000)]
        record["final"]["question_investigation"]["analysis_plan"] = {"paths": paths,
            "required_obligations": ["assignment_expression"]}
        plan = audit_report({"cases": [row]}, traces={row["run_id"]: {
            "data": record, "sha256": "a" * 64}})["samples"][0]["analysis_plan"]
        self.assertEqual(plan["path_count"], 12000)
        self.assertEqual(len(plan["paths"]), 32)
        self.assertTrue(plan["paths_truncated"])
        self.assertEqual(plan["all_paths_sha256"], hashlib.sha256(json.dumps(
            paths, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_duplicate_samples_and_embedded_identity_mismatch_are_rejected(self):
        row = sample()
        with self.assertRaisesRegex(ValueError, "REPORT_SAMPLE_DUPLICATED"):
            audit_report({"cases": [row, copy.deepcopy(row)]})
        row["partial_diagnostics"] = {"run_id": "run-other", "answer_sha256": row["answer_sha256"]}
        with self.assertRaisesRegex(ValueError, "EMBEDDED_DIAGNOSTIC_IDENTITY_MISMATCH"):
            audit_report({"cases": [row]})

    def test_cli_separates_source_replay_from_original_native_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = sample()
            original = root / "native.json"
            original.write_text(json.dumps({"cases": [row], "execution_surface": "native_windows_http"}))
            replay = root / "source.json"
            later = sample(run_id="source-run")
            replay.write_text(json.dumps({"cases": [later], "execution_surface": "source_python_http"}))
            traces = root / "quality-traces"
            traces.mkdir()
            (traces / "source-run.json").write_text(json.dumps(trace(later)))
            output = root / "audit.json"
            self.assertEqual(main(["--report", str(original), "--supplemental-report", str(replay),
                "--supplemental-trace-root", str(root), "--output", str(output)]), 0)
            audit = json.loads(output.read_text())
            self.assertEqual(audit["original"]["diagnostic_complete_count"], 0)
            self.assertEqual(audit["supplemental_observations"][0]["diagnostic_complete_count"], 1)
            self.assertFalse(audit["supplemental_observations"][0]["may_close_original_samples"])
            self.assertFalse(audit["release_qualified"])
            self.assertEqual(audit["external_model_calls"], 0)

    def test_conflicting_trace_run_ids_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "quality-traces"
            root.mkdir()
            row = sample()
            (root / "one.json").write_text(json.dumps(trace(row)))
            (root / "two.json").write_text(json.dumps(trace(row, created_at="later")))
            with self.assertRaisesRegex(ValueError, "TRACE_RUN_ID_CONFLICT"):
                trace_index(root)


if __name__ == "__main__":
    unittest.main()
