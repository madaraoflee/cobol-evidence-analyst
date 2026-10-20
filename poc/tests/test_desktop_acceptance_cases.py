"""The release runner must reject incomplete or mismatched evidence offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.desktop_acceptance_cases import (DEFAULT_MANIFEST, _large_program, _public_case, _quality_checks,
    _scope_probe_checks,
    corpus_fingerprint, ledger_versions_match, prepare_source, ranges_supplied, replacement_supported,
    run_cases, verify_pages)


class DesktopAcceptanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "entry.cbl").write_bytes(b"MOVE 1 TO A.\nMOVE A TO B.\nGOBACK.\n")
        self.version = hashlib.sha256((self.root / "entry.cbl").read_bytes()).hexdigest()
        self.page = {"relative_path": "entry.cbl", "source_sha256": self.version,
            "evidence_id": "ev_page_example", "start_line": 1, "end_line": 2,
            "source_text": "MOVE 1 TO A.\nMOVE A TO B.", "span_truncated": False}

    def test_actual_text_hash_and_range_are_all_required(self):
        self.assertEqual(verify_pages(self.root, [self.page])[0]["source_sha256"], self.version)
        for change in ({"source_sha256": "0" * 64}, {"source_text": "MOVE 999 TO A."},
                       {"start_line": 0}, {"end_line": 99}, {"start_line": True}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                verify_pages(self.root, [{**self.page, **change}])
        with self.assertRaises(AssertionError):
            verify_pages(self.root, [])

    def test_path_hit_truncated_page_and_wrong_version_do_not_cover_gold(self):
        required = [{"path": "entry.cbl", "sha256": self.version, "start_line": 1, "end_line": 3}]
        self.assertFalse(ranges_supplied([self.page], required))
        last = {**self.page, "start_line": 3, "end_line": 3}
        self.assertTrue(ranges_supplied([self.page, last], required))
        self.assertFalse(ranges_supplied([self.page, {**last, "source_sha256": "0" * 64}], required))
        self.assertFalse(ranges_supplied([{**self.page, "end_line": 3, "span_truncated": True}], required))
        self.assertFalse(ranges_supplied([self.page], []))

    def test_missing_fixed_public_corpus_is_failure_not_skip(self):
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        case = next(row for row in manifest["cases"] if row["corpus"] == "public")
        with self.assertRaisesRegex(AssertionError, "skipping is forbidden"):
            prepare_source(case, manifest, self.root / "missing", None)

    def test_frozen_sources_are_byte_identical_and_manifest_has_required_risks(self):
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        case = next(row for row in manifest["cases"] if row["id"] == "frontend_semantics")
        source = self.root / "frozen"
        info = prepare_source(case, manifest, source, None)
        self.assertEqual(info["files"], 2)
        self.assertNotIn(b"\r\n", (source / "frontend.cbl").read_bytes())
        self.assertEqual(hashlib.sha256((source / "frontend.cbl").read_bytes()).hexdigest(),
            manifest["source_files"]["frontend.cbl"]["sha256"])
        self.assertTrue({"quality_add_giving_roles", "quality_scope_words_not_operands",
            "quality_same_line_occurrences", "quality_literal_dependencies_absent", "quality_literal_comment_marker",
            "quality_qualified_fields_distinct", "quality_section_binding", "quality_section_flow",
            "quality_semantic_ir_agrees"} <= set(case["assertions"]))
        for row in manifest["cases"]:
            self.assertGreaterEqual(row["repeats"], 3)
            self.assertEqual(len(row["assertions"]), len(set(row["assertions"])))
            for group in ("quality", "completeness", "evidence") if row["kind"] == "answer" else ("lifecycle",):
                self.assertTrue(row["assertion_groups"][group])
                self.assertTrue(set(row["assertion_groups"][group]) <= set(row["assertions"]))

    def test_large_shape_matches_previously_measured_program_bytes(self):
        text = _large_program(0, 12000, 224, 24)
        self.assertEqual(len(text.splitlines()), 224)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),
            "5e0b7fa10e8ac65df18265ccdbd318fd24e7710bebe457426dee03b1a6ef08dc")
        self.assertEqual(len(_large_program(11999, 12000, 25000, 24).splitlines()), 25000)
        self.assertEqual(corpus_fingerprint(self.root)["files"], 1)

    def test_partial_without_critical_ledger_facts_does_not_pass_quality(self):
        case = {"id": "overwrite_condition", "corpus": "frozen"}
        with mock.patch("tools.desktop_acceptance_cases.flow_checks", return_value={
                "quality_conditional_reaching_definitions": True}):
            checks = _quality_checks(case, {"status": "PARTIAL", "answer": "FLAG 覆盖条件未成立计算",
                "claim_ledger": {"entries": [], "counts": {"contradicted": 0}}}, self.root, [self.page])
        self.assertFalse(checks["quality_formula_condition_overwrite_supported"])

    def test_revision_count_and_correct_text_cannot_replace_structured_support(self):
        assertion = {"predicate": "assignment_statement", "target": "RESULT", "expression": ["INPUT", "*", "2"]}
        self.assertFalse(replacement_supported({"revised_claim_count": 1, "entries": []}, assertion))
        for result in ({"support_status": "unsupported", "assertion": assertion},
                       {"support_status": "supported", "assertion": {**assertion, "expression": ["INPUT", "*", "999"]}}):
            self.assertFalse(replacement_supported({"entries": [{"revised_claims": [result]}]}, assertion))
        self.assertTrue(replacement_supported({"entries": [{"revised_claims": [
            {"support_status": "supported", "assertion": assertion}]}]}, assertion))

    def test_supported_ledger_cannot_bind_another_source_version_or_unsupplied_id(self):
        reference = {"relative_path": "entry.cbl", "source_sha256": self.version, "start_line": 1, "end_line": 1}
        entry = {"support_status": "supported", "evidence_ids": [self.page["evidence_id"]], "source_versions": [reference]}
        self.assertTrue(ledger_versions_match({"entries": [entry]}, [self.page]))
        for change in ({"source_versions": [{**reference, "source_sha256": "0" * 64}]},
                       {"source_versions": [{**reference, "end_line": 3}]},
                       {"evidence_ids": ["ev_not_supplied"]}, {"invalid_evidence_ids": ["ev_bad"]}):
            self.assertFalse(ledger_versions_match({"entries": [{**entry, **change}]}, [self.page]))

    def test_previous_sample_directory_is_never_reused(self):
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        case = next(row for row in manifest["cases"] if row["id"] == "frontend_semantics")
        (self.root / case["id"]).mkdir()
        with self.assertRaisesRegex(AssertionError, "previous samples cannot be reused"), \
                mock.patch("tools.desktop_acceptance_cases.LoopbackProvider", side_effect=AssertionError("provider")):
            run_cases([], self.root, manifest, None, case_ids=[case["id"]])

    def test_execution_failure_keeps_every_required_sample_failed(self):
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        case = next(row for row in manifest["cases"] if row["id"] == "frontend_semantics")
        with mock.patch("tools.desktop_acceptance_cases._run_cases", side_effect=TimeoutError("deadline")):
            rows = run_cases([], self.root, manifest, None, case_ids=[case["id"]])
        self.assertEqual(len(rows), case["repeats"])
        self.assertTrue(all(row["status"] == "failed" and not any(row["checks"].values()) for row in rows))
        self.assertTrue(all(row["business_answer_correctness"] is None for row in rows))

    def test_scope_probes_remain_mandatory_without_replacing_original_samples_or_gold(self):
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["cases"]), 25)
        self.assertEqual(sum(case["repeats"] for case in manifest["cases"]), 79)
        self.assertEqual(sum(case["repeats"] * len(case.get("scope_probes", []))
                             for case in manifest["cases"]), 14)
        self.assertEqual(sum(case["repeats"] for case in manifest["cases"]
                             if case["id"] not in {"soft_target_answer", "activation_trigger",
                                 "cl_batch_context", "cl_late_context", "framework_page_chain", "business_batch_overview", "business_online_overview", "business_semantic_revision"}), 55)
        cases = {case["id"]: case for case in manifest["cases"]}
        frontend = cases["frontend_semantics"]
        self.assertEqual(frontend["reviewed_files"], ["frontend.cbl"])
        self.assertEqual({probe["id"] for probe in frontend["scope_probes"]}, {
            "bare_group_field", "qualified_output_scope", "ambiguous_group_field"})
        large = cases["large_repository"]
        self.assertEqual(large["question"], "BILL-AMOUNT 怎么计算？有哪些触发条件？")
        self.assertIn("completeness_formula_control_supplied", large["assertions"])
        self.assertIn("completeness_large_two_if_pic_supplied", large["assertions"])
        self.assertEqual(large["limits"]["question_seconds"], 60)
        self.assertEqual(large["scope_probes"][0]["expected_candidate_count"], 2)
        for identity, assertion in (("public_monthly_interest", "completeness_accumulation_covered"),
                ("public_monthly_interest_repair", "completeness_accumulation_omission_repaired")):
            self.assertIn(assertion, cases[identity]["assertions"])
            self.assertIn(assertion, cases[identity]["assertion_groups"]["completeness"])

    def test_new_batch_cases_require_actual_source_and_distinguish_late_scope(self):
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        cases = {case["id"]: case for case in manifest["cases"]}
        for identity in ("cl_batch_context", "cl_late_context"):
            case = cases[identity]
            self.assertEqual(case["repeats"], 3)
            source = self.root / identity
            prepare_source(case, manifest, source, None)
            self.assertEqual(case["policy"]["max_model_requests"], 1)
            self.assertIn("completeness_cl_single_request", case["assertions"])
        late = (self.root / "cl_late_context/late.clle").read_text(encoding="utf-8").splitlines()
        self.assertGreater(next(index for index, line in enumerate(late, 1) if line.startswith("CALL ")), 300)
        self.assertIn("quality_cl_late_scope_unknown", cases["cl_late_context"]["assertions"])

    def test_old_incomplete_repair_cannot_pass_new_source_completion_gate(self):
        case, agent, trace = self.monthly_fixture(repair=True)
        checks = _quality_checks(case, agent, self.root, [self.page], trace)
        self.assertFalse(checks["completeness_accumulation_omission_repaired"])
        self.assertFalse(checks["completeness_reviewed_answer_facts"])
        self.assertFalse(checks["completeness_local_facts_source_bound"])

    def monthly_fixture(self, *, repair=False):
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        identity = "public_monthly_interest_repair" if repair else "public_monthly_interest"
        case = next(row for row in manifest["cases"] if row["id"] == identity)
        mechanism = _public_case(case)["mechanism"]
        assertions = mechanism["source_assertions"]
        entries = [{"support_status": "supported", "claim_type": item["predicate"],
                    "target_entity": item["target"], "expression": item["expression"]} for item in assertions]
        answer = mechanism["fake_correct_answer"]
        if repair:
            answer = mechanism["fake_incorrect_answer"].replace("/ 999", "/ 1200")
            entries = [{"support_status": "contradicted", "revised_claims": [
                {"support_status": "supported", "assertion": assertions[0]}]}]
        agent = {"status": "PARTIAL" if repair else "ANALYZED", "answer": answer,
            "claim_ledger": {"entries": entries, "revised_claim_count": 1 if repair else 0}}
        trace = {"final": {"answer_completion": {"required_aspects": ["formula", "accumulation"],
            "missing_aspects": ["accumulation"] if repair else [],
            "status": "incomplete" if repair else "complete", "semantic_verification": "unverified"}}}
        return case, agent, trace

    def test_monthly_interest_needs_the_real_accumulator_and_completion_coverage(self):
        import copy
        case, agent, trace = self.monthly_fixture()
        label = "completeness_accumulation_covered"
        self.assertTrue(_quality_checks(case, agent, self.root, [], trace)[label])
        for mutation in ("no_completion", "no_required_aspect", "no_missing_list", "missing_accumulation",
                         "missing_accumulator_text", "missing_accumulator_support", "wrong_accumulator_expression"):
            with self.subTest(mutation=mutation):
                changed_agent, changed_trace = copy.deepcopy((agent, trace))
                completion = changed_trace["final"]["answer_completion"]
                if mutation == "no_completion":
                    changed_trace["final"].pop("answer_completion")
                elif mutation == "no_required_aspect":
                    completion["required_aspects"] = ["formula"]
                elif mutation == "no_missing_list":
                    completion.pop("missing_aspects")
                elif mutation == "missing_accumulation":
                    completion["missing_aspects"] = ["accumulation"]
                elif mutation == "missing_accumulator_text":
                    changed_agent["answer"] = "月息公式除以1200，然后调用1300-B-WRITE-TX。"
                elif mutation == "missing_accumulator_support":
                    changed_agent["claim_ledger"]["entries"].pop()
                else:
                    changed_agent["claim_ledger"]["entries"][-1]["expression"] = ["WS-MONTHLY-INT", "+", "999"]
                self.assertFalse(_quality_checks(case, changed_agent, self.root, [], changed_trace)[label])

    def test_monthly_repair_requires_the_exact_omission_and_preserves_the_supported_1200_formula(self):
        import copy
        case, agent, trace = self.monthly_fixture(repair=True)
        label = "completeness_accumulation_omission_reported"
        checks = _quality_checks(case, agent, self.root, [], trace)
        self.assertTrue(checks[label])
        self.assertTrue(checks["quality_frozen_source_assertions"])
        self.assertNotIn("WS-TOTAL-INT", agent["answer"])
        self.assertIn("/ 999", _public_case(case)["mechanism"]["fake_incorrect_answer"])
        for mutation in ("partial_only", "analyzed", "no_required_aspect", "wrong_missing_aspect",
                         "complete", "verified", "formula_removed", "wrong_formula", "unsupported_replacement"):
            with self.subTest(mutation=mutation):
                changed_agent, changed_trace = copy.deepcopy((agent, trace))
                completion = changed_trace["final"]["answer_completion"]
                if mutation == "partial_only":
                    changed_trace["final"].pop("answer_completion")
                elif mutation == "analyzed":
                    changed_agent["status"] = "ANALYZED"
                elif mutation == "no_required_aspect":
                    completion["required_aspects"] = ["formula"]
                elif mutation == "wrong_missing_aspect":
                    completion["missing_aspects"] = ["inputs"]
                elif mutation == "complete":
                    completion["status"] = "complete"
                elif mutation == "verified":
                    completion["semantic_verification"] = "verified"
                elif mutation == "formula_removed":
                    changed_agent["answer"] = "累加解释尚缺。"
                elif mutation == "wrong_formula":
                    changed_agent["answer"] = changed_agent["answer"].replace("/ 1200", "/ 999")
                else:
                    changed_agent["claim_ledger"]["entries"][0]["revised_claims"][0]["support_status"] = "unsupported"
                self.assertFalse(_quality_checks(case, changed_agent, self.root, [], changed_trace)[label])

    def test_noop_scope_requires_actual_complete_body_not_generic_success_status(self):
        case = {"id": "public_unimplemented_procedure", "corpus": "public",
                "public_case_id": "unimplemented-procedure", "required_answer_text": [], "unverified_prose": True}
        agent = {"status": "ANALYZED", "answer": "EXIT", "claim_ledger": {}}
        with mock.patch("tools.desktop_acceptance_cases._public_case", return_value={
                "mechanism": {"source_assertions": []}}):
            absent = _quality_checks(case, agent, self.root, [], {})
            self.assertFalse(absent["quality_noop_scope_not_monthly_interest"])
            items = [{"kind": kind, "candidate_count": count, "status": "SATISFIED"}
                     for kind, count in (("procedure_body", 1), ("formula", 0), ("conditions", 0), ("dependencies", 0))]
            items[0]["evidence_ids"] = [self.page["evidence_id"]]
            scope = {"procedure_name": "1400-COMPUTE-FEES", "relative_path": "entry.cbl",
                     "source_sha256": self.version, "start_line": 1, "end_line": 2}
            trace = {"final": {"question_investigation": {"required_items": items, "open_gaps": [],
                    "source_scope": {"ranges": [scope]}}}}
            self.assertTrue(_quality_checks(case, agent, self.root, [self.page], trace)["quality_noop_scope_not_monthly_interest"])
            items[1]["candidate_count"] = 1
            self.assertFalse(_quality_checks(case, agent, self.root, [self.page], trace)["quality_noop_scope_not_monthly_interest"])

    def test_file_mapping_requires_bound_file_and_actual_layout_and_io_evidence(self):
        import copy
        case = {"id": "public_file_record_mapping", "corpus": "public", "public_case_id": "file-record-mapping",
                "required_answer_text": [], "unverified_prose": True}
        agent = {"status": "ANALYZED", "answer": "ACCOUNT-FILE", "claim_ledger": {}}
        entity = {"entity_type": "FileRef", "file_name": "ACCOUNT-FILE", "binding_status": "bound",
            "external_identity_verified": False, "diagnostics": [], "source_versions": [{
                "relative_path": "entry.cbl", "source_sha256": self.version, "start_line": 1, "end_line": 2}]}
        trace = {"final": {"question_investigation": {"analysis_plan": {"file_entities": [entity]},
            "required_items": [{"kind": kind, "status": "SATISFIED", "evidence_ids": [self.page["evidence_id"]]}
                for kind in ("file_definitions", "file_layout", "copy_layout", "file_io")], "open_gaps": []}}}
        def checked(value, pages):
            with mock.patch("tools.desktop_acceptance_cases._public_case", return_value={
                    "mechanism": {"source_assertions": []}}):
                return _quality_checks(case, agent, self.root, pages, value)["completeness_file_entity_bound"]
        self.assertTrue(checked(trace, [self.page]))
        self.assertFalse(checked({}, [self.page]))
        self.assertFalse(checked(trace, [{**self.page, "source_sha256": "0" * 64}]))
        for change in ({"binding_status": "candidate"}, {"external_identity_verified": True},
                       {"source_versions": []}, {"diagnostics": ["file_reference_ambiguous"]}):
            changed = copy.deepcopy(trace)
            changed["final"]["question_investigation"]["analysis_plan"]["file_entities"][0].update(change)
            self.assertFalse(checked(changed, [self.page]))
        for change in ({"status": "OPEN"}, {"evidence_ids": ["not_supplied"]}):
            changed = copy.deepcopy(trace)
            changed["final"]["question_investigation"]["required_items"][-1].update(change)
            self.assertFalse(checked(changed, [self.page]))

    def test_ambiguity_requires_preserved_qualified_candidates_and_real_obligation(self):
        probe = {"id": "ambiguous_group_field", "expected_candidate_count": 1}
        investigation = {"required_items": [{"kind": "formula", "status": "SATISFIED",
            "candidate_count": 1, "evidence_ids": [self.page["evidence_id"]]}], "open_gaps": [
            {"reason": "field_reference_ambiguous", "candidates": ["AMOUNT OF INPUT-REC", "AMOUNT OF OUTPUT-REC"]}]}
        trace = {"final": {"question_investigation": investigation}}
        self.assertTrue(_scope_probe_checks(probe, trace, {"status": "PARTIAL"}, [self.page])[
            "quality_ambiguous_group_field_candidates"])
        self.assertFalse(_scope_probe_checks(probe, trace, {"status": "ANALYZED"}, [self.page])[
            "quality_ambiguous_group_field_candidates"])
        investigation["required_items"][0]["evidence_ids"] = ["not_supplied"]
        self.assertFalse(_scope_probe_checks(probe, trace, {"status": "PARTIAL"}, [self.page])[
            "quality_ambiguous_group_field_candidates"])

    def test_ambiguous_completion_cannot_demand_other_field_rounding_or_omit_the_check(self):
        probe = {"id": "ambiguous_group_field", "expected_candidate_count": 1}
        def checked(completion):
            return _scope_probe_checks(probe, {"final": {"answer_completion": completion}},
                {"status": "PARTIAL"}, [self.page])["quality_ambiguous_completion_relevant"]
        valid = {"semantic_verification": "unverified", "required_aspects": ["formula"], "missing_aspects": []}
        self.assertTrue(checked(valid))
        self.assertFalse(checked({}))
        self.assertFalse(checked({**valid, "required_aspects": ["formula", "result_adjustments"]}))
        self.assertFalse(checked({**valid, "missing_aspects": ["rounding"]}))


if __name__ == "__main__":
    unittest.main()
