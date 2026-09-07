from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from business_acceptance import (  # noqa: E402
    CALC01_PROFILE,
    CoverageObligation,
    CoverageProfile,
    evaluate_source_coverage,
)
from structural_index import build_structural_index  # noqa: E402


class BusinessSourceCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        shutil.copytree(POC_ROOT / "fixtures" / "synthetic-insurance-v1", self.source)
        self.database = self.root / "index.sqlite"

    def evaluate(self, profile: CoverageProfile = CALC01_PROFILE) -> dict:
        build_structural_index(self.source, self.database, quiet=True)
        return evaluate_source_coverage(self.database, profile)

    def mutate(self, program: str, old: str, new: str) -> None:
        path = self.source / "programs" / f"{program}.cbl"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    @staticmethod
    def obligation(report: dict, obligation_id: str) -> dict:
        return next(item for item in report["obligations"] if item["obligation_id"] == obligation_id)

    def assert_missing(self, report: dict, obligation_id: str) -> None:
        obligation = self.obligation(report, obligation_id)
        self.assertEqual(obligation["status"], "missing")
        self.assertTrue(obligation["missing_requirement_numbers"])
        self.assertFalse(report["full_business_analysis_verified"])

    def test_real_fixture_has_source_chain_but_known_business_gaps(self) -> None:
        report = self.evaluate()
        self.assertEqual(report["summary"], {"covered": 16, "missing": 3, "boundary": 4})
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["evaluation_scope"], "indexed_source_fact_coverage")
        for flag in (
            "full_business_analysis_verified", "question_understanding_tested",
            "agent_retrieval_tested", "answer_completeness_tested", "runtime_execution_tested",
        ):
            self.assertFalse(report[flag])
        self.assertEqual(
            {item["obligation_id"] for item in report["obligations"] if item["status"] == "missing"},
            {"all_active_riders", "every_adjustment_failure", "effective_base_rate"},
        )

    def test_evidence_has_source_spans_hashes_and_scoped_copy_origin(self) -> None:
        report = self.evaluate()
        for item in report["obligations"]:
            if item["status"] == "covered":
                self.assertTrue(item["evidence_refs"], item["obligation_id"])
            for ref in item["evidence_refs"]:
                raw = (self.source / ref["relative_path"]).read_bytes()
                lines = raw.decode("utf-8").splitlines()
                span = "\n".join(lines[ref["start_line"] - 1:ref["end_line"]])
                self.assertEqual(ref["source_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(ref["span_sha256"], hashlib.sha256(span.encode("utf-8")).hexdigest())
        refs = self.obligation(report, "scoped_parameter_definition")["evidence_refs"]
        self.assertTrue(any(ref["relative_path"] == "copybooks/SYNPRM.cpy" for ref in refs))
        condition = self.obligation(report, "policy_status_condition")
        self.assertTrue(any(ref["start_line"] == 38 and ref["end_line"] == 39 for ref in condition["evidence_refs"]))

    def test_removing_condition_continuation_fails_status_obligation(self) -> None:
        self.mutate("SYNP000", "003900        AND IN-POLICY-STATUS NOT = 'I'\n", "")
        self.assert_missing(self.evaluate(), "policy_status_condition")

    def test_reversing_condition_outcome_fails_error_clearing(self) -> None:
        self.mutate("SYNP090", "IF RETURN-CODE NOT = ZERO", "IF RETURN-CODE = ZERO")
        self.assert_missing(self.evaluate(), "error_output_clearing")

    def test_removing_factor_sql_write_is_not_satisfied_by_other_statements(self) -> None:
        self.mutate("SYNP040", "INTO :WS-MODE-FACTOR", "INTO :WS-POLICY-FEE")
        self.mutate("SYNP040", "001700 LOAD-ADJUSTMENT-FACTORS.",
            "001650     MOVE 1 TO WS-MODE-FACTOR.\n001700 LOAD-ADJUSTMENT-FACTORS.")
        self.assert_missing(self.evaluate(), "mode_factor_sql")

    def test_removing_factor_sql_input_is_not_satisfied_by_field_definition(self) -> None:
        self.mutate("SYNP040", "PAYMENT_MODE = :IN-PAYMENT-MODE", "PAYMENT_MODE = 'A'")
        self.assert_missing(self.evaluate(), "mode_factor_sql")

    def test_nonzero_error_amount_fails_even_when_normal_formula_exists(self) -> None:
        self.mutate("SYNP090", "MOVE ZERO TO OUT-INSTALMENT-PREMIUM", "MOVE 1 TO OUT-INSTALMENT-PREMIUM")
        report = self.evaluate()
        self.assert_missing(report, "error_output_clearing")
        self.assertEqual(self.obligation(report, "instalment_formula")["status"], "covered")

    def test_missing_annual_component_and_rounding_are_detected(self) -> None:
        self.mutate("SYNP040", "+ WS-POLICY-FEE.", "+ 0.")
        self.mutate("SYNP040", "OUT-INSTALMENT-PREMIUM ROUNDED =", "OUT-INSTALMENT-PREMIUM =")
        report = self.evaluate()
        self.assert_missing(report, "annual_components")
        self.assert_missing(report, "instalment_formula")

    def test_changed_rider_status_cannot_pass_on_sql_success_condition_alone(self) -> None:
        self.mutate("SYNP030", "IF IN-RIDER-STATUS = 'A'", "IF IN-RIDER-STATUS = 'D'")
        self.assert_missing(self.evaluate(), "single_active_rider")

    def test_literal_target_does_not_satisfy_dynamic_call_boundary(self) -> None:
        self.mutate("SYNP000", "CALL LK-CALCULATOR-PROGRAM", "CALL 'SYNP100'")
        self.assert_missing(self.evaluate(), "dynamic_call_boundary")

    def test_fabricated_literal_edge_on_dynamic_statement_fails_boundary(self) -> None:
        self.evaluate()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """INSERT INTO relations
                   SELECT 'invented-literal-edge', relative_path, from_entity_id,
                          'CALLS', 'SYNP100', NULL, NULL, 'confirmed', evidence_id, '{}'
                     FROM relations WHERE relation_type = 'CALL_TARGET_FROM'"""
            )
            connection.commit()
        finally:
            connection.close()
        self.assert_missing(evaluate_source_coverage(self.database), "dynamic_call_boundary")

    def test_read_only_report_does_not_change_snapshot(self) -> None:
        self.evaluate()
        before = self.database.read_bytes()
        first = evaluate_source_coverage(self.database)
        second = evaluate_source_coverage(self.database)
        self.assertEqual(first, second)
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(json.loads(json.dumps(first, ensure_ascii=False)), first)

    def test_a_small_fully_covered_profile_is_still_not_complete_business_analysis(self) -> None:
        profile = CoverageProfile("single-source-fact", (CALC01_PROFILE.source_obligations[1],))
        report = self.evaluate(profile)
        self.assertEqual(report["summary"], {"covered": 1, "missing": 0, "boundary": 0})
        self.assertEqual(report["status"], "SOURCE_FACTS_COVERED_WITH_BOUNDARIES")
        self.assertFalse(report["full_business_analysis_verified"])
        self.assertFalse(report["answer_completeness_tested"])

    def test_profile_is_explicit_and_cannot_match_a_different_program_scope(self) -> None:
        original = CALC01_PROFILE.source_obligations[1]
        wrong_scope = replace(original.requirements[0], program_name="SYNP090")
        profile = CoverageProfile("wrong-scope", (replace(original, requirements=(wrong_scope,)),))
        self.assert_missing(self.evaluate(profile), "instalment_formula")

    def test_empty_or_duplicate_obligations_are_rejected(self) -> None:
        self.evaluate()
        profiles = (
            CoverageProfile("empty", ()),
            CoverageProfile("empty-requirements", (CoverageObligation("empty", "empty", ()),)),
            CoverageProfile("duplicate", (CALC01_PROFILE.source_obligations[0],) * 2),
        )
        for profile in profiles:
            with self.subTest(profile=profile.profile_id), self.assertRaises(ValueError):
                evaluate_source_coverage(self.database, profile)

    def test_a_missing_index_is_not_created(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            evaluate_source_coverage(self.database)
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
