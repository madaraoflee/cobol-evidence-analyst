"""Review convergence follows individual source-bound concerns within its budget."""

from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis_continuation import AnalysisContinuation
from business_synthesis import assess_business_answer, needs_synthesis_review
from draft_validation import revision_regression, validate_answer_draft
from semantic_risk_identity import risk_identities


def source_page(program, reference):
    source = f"""IDENTIFICATION DIVISION.
PROGRAM-ID. {program}.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 SQLCODE PIC S9(9).
LINKAGE SECTION.
01 SEARCH-KEY PIC 9(8).
01 FOUND-VALUE PIC 9(8).
01 RESULT-CODE PIC XX.
PROCEDURE DIVISION USING SEARCH-KEY FOUND-VALUE RESULT-CODE.
MAIN.
EXEC SQL
SELECT STORED_VALUE INTO :FOUND-VALUE
FROM LOOKUP_ROWS WHERE ROW_KEY = :SEARCH-KEY
END-EXEC.
IF SQLCODE = 0
MOVE '47' TO RESULT-CODE
ELSE MOVE '63' TO RESULT-CODE
END-IF.
GOBACK.
"""
    return {"relative_path": program.lower() + ".cbl", "evidence_id": reference,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "source_text": source,
            "start_line": 1, "end_line": len(source.splitlines()), "format_hint": "free"}


def review_with_risks(rows, *, sql=False):
    result = {"missing_aspects": ["boundary"], "status": "incomplete"}
    result.update({"sql_boundary_review": {"risks": rows}} if sql else {"semantic_risk_candidates": rows})
    return result


def concern(assertion="All exceptions return '63'.", **changes):
    return {"kind": "unconditional_exception_result", "assertion": assertion, "source_refs": [{
        "relative_path": "lookup.cbl", "source_sha256": "a" * 64,
        "start_line": 12, "end_line": 15, "supplied_reference_ids": ["ev_old"]}], **changes}


def validated_review(*, findings=(), omissions=(), **changes):
    return {"semantic_review": {"status": "changes_requested", "validation": "strict_payload_bound",
        "semantic_verification": False, "findings": list(findings), "omissions": list(omissions), **changes}}


def model_finding(**changes):
    return {"unit_id": "unit_1", "kind": "insufficient", "quote": "The request is always accepted.",
        "start_char": 0, "end_char": 31, "reason": "The supplied branch does not establish this outcome.",
        "source_refs": [{"reference_id": "ev_old", "kind": "source_page", "path": "entry.cbl",
            "sha256": "b" * 64, "start_line": 8, "end_line": 8,
            "quote": "CALL 'PROCESSQ' USING REQUEST-RECORD."}], **changes}


class ProductionRiskProgressTests(unittest.TestCase):
    def setUp(self):
        self.pages = [source_page("LOOKUPQ", "ev_lookup"), source_page("OTHERQ", "ev_other")]
        self.question = "返回状态？"
        self.both = "LOOKUPQ 任何异常都返回 '63'。[ev_lookup]\nOTHERQ 任何异常都返回 '63'。[ev_other]"
        self.first_fixed = ("LOOKUPQ 只有正常返回并到达 IF 时才按 SQLCODE 分支设置响应码。[ev_lookup]\n"
                            "OTHERQ 任何异常都返回 '63'。[ev_other]")

    def assessment(self, answer):
        return assess_business_answer(self.question, answer, {}, self.pages, answer_detail="brief")

    def draft(self, answer):
        return validate_answer_draft(self.question, answer, {}, self.pages, answer_detail="brief")

    def test_resolving_one_of_two_same_kind_concerns_allows_remaining_review(self):
        initial, reduced = self.assessment(self.both), self.assessment(self.first_fixed)
        self.assertEqual(len(initial["semantic_risk_candidates"]), 2)
        self.assertEqual(len(reduced["semantic_risk_candidates"]), 1)
        self.assertEqual(initial["missing_aspects"], reduced["missing_aspects"])
        continuation = AnalysisContinuation(self.question)
        self.assertTrue(continuation.needs_followup(self.both, initial, self.pages))
        self.assertTrue(continuation.needs_followup(self.first_fixed, reduced, self.pages))
        self.assertEqual(continuation.revision_requests, 2)

    def test_same_kind_concern_moving_to_another_program_is_a_regression(self):
        first_only = ("LOOKUPQ 任何异常都返回 '63'。[ev_lookup]\n"
                      "OTHERQ 正常返回并到达 IF 时按 SQLCODE 判断。[ev_other]")
        previous, candidate = self.draft(first_only), self.draft(self.first_fixed)
        self.assertEqual(len(previous["assessment"]["semantic_risk_candidates"]), 1)
        self.assertEqual(len(candidate["assessment"]["semantic_risk_candidates"]), 1)
        self.assertEqual(previous["assessment"]["missing_aspects"], candidate["assessment"]["missing_aspects"])
        self.assertEqual(revision_regression(previous, candidate), "ANSWER_BOUNDARIES_REGRESSED")

    def test_resolving_concerns_is_not_a_regression(self):
        previous, candidate = self.draft(self.both), self.draft(self.first_fixed)
        self.assertIsNone(revision_regression(previous, candidate))
        safe = ("LOOKUPQ 正常返回并到达 IF 时按 SQLCODE 判断。[ev_lookup]\n"
                "OTHERQ 正常返回并到达 IF 时按 SQLCODE 判断。[ev_other]")
        self.assertIsNone(revision_regression(candidate, self.draft(safe)))

    def test_clean_prose_without_nominated_risks_does_not_need_review(self):
        assessment = self.assessment("LOOKUPQ 正常返回并到达 IF 时按 SQLCODE 判断。[ev_lookup]")
        self.assertEqual(risk_identities(assessment), frozenset())
        self.assertFalse(needs_synthesis_review(self.question, "", {}, source_pages=self.pages,
            source_available=True, answer_detail="brief", assessment=assessment))


class RiskIdentityTests(unittest.TestCase):
    def test_order_duplicates_generated_ids_and_answer_offsets_do_not_reset_review(self):
        first, second = concern(), concern("Invalid input returns '63'.", kind="unvalidated_input_outcome")
        old = review_with_risks([first, second])
        moved = deepcopy(first)
        moved["assertion"] = "All   exceptions return '63'. [ev_regenerated]"
        moved["source_refs"][0]["supplied_reference_ids"] = ["ev_regenerated"]
        moved["span"] = {"start": 123, "end": 170}
        new = review_with_risks([second, moved, second])
        self.assertEqual(risk_identities(old), risk_identities(new))
        continuation = AnalysisContinuation("Explain the result")
        self.assertTrue(continuation.needs_followup("old", old, []))
        self.assertFalse(continuation.needs_followup("reordered", new, []))
        self.assertEqual(continuation.stop_reason, "answer_review_no_progress")

    def test_source_version_location_and_include_instance_remain_distinct(self):
        original = concern()
        for field, value in (("relative_path", "other.cbl"), ("source_sha256", "c" * 64),
                             ("start_line", 22), ("end_line", 25), ("include_chain_sha256", "d" * 64),
                             ("copy_instance", "copy-2"),
                             ("replacement_trace", [{"from": "INPUT", "to": "SECOND-INPUT"}]),
                             ("source_binding_sha256", "e" * 64)):
            with self.subTest(field=field):
                changed = deepcopy(original)
                changed["source_refs"][0][field] = value
                self.assertNotEqual(risk_identities(review_with_risks([original])),
                                    risk_identities(review_with_risks([changed])))

    def test_whitespace_in_literal_values_is_not_discarded(self):
        first = review_with_risks([concern("The output is 'A  B'.")])
        changed = review_with_risks([concern("The output is 'A B'.")])
        formatted = review_with_risks([concern("The  output\n is 'A  B'.")])
        self.assertNotEqual(risk_identities(first), risk_identities(changed))
        self.assertEqual(risk_identities(first), risk_identities(formatted))

    def test_sql_span_offsets_and_evidence_ids_are_presentation_but_hash_preserves_long_claims(self):
        original = {"kind": "SQL_RUNTIME_EFFECT_UNSUPPORTED", "answer_span": {
            "start": 0, "end": 1000, "text": "This SELECT does not lock " * 10, "sha256": "a" * 64},
            "source_refs": concern()["source_refs"]}
        shifted = deepcopy(original)
        shifted["answer_span"].update(start=30, end=1030)
        shifted["source_refs"][0]["supplied_reference_ids"] = ["ev_new"]
        self.assertEqual(risk_identities(review_with_risks([original], sql=True)),
                         risk_identities(review_with_risks([shifted], sql=True)))
        shifted["answer_span"]["sha256"] = "b" * 64
        self.assertNotEqual(risk_identities(review_with_risks([original], sql=True)),
                            risk_identities(review_with_risks([shifted], sql=True)))

    def test_existing_sql_and_source_semantic_domains_are_both_retained(self):
        row = concern()
        semantic = review_with_risks([row])
        sql = review_with_risks([row], sql=True)
        self.assertEqual(len(risk_identities({**semantic, **sql})), 2)

    def test_rewording_does_not_escape_budget_even_after_checkpoint_restore(self):
        continuation = AnalysisContinuation("Explain outcomes")
        for phrase in ("Every exception returns '63'.", "All failures return '63'."):
            self.assertTrue(continuation.needs_followup(phrase, review_with_risks([concern(phrase)]), []))
            continuation = AnalysisContinuation("Explain outcomes", continuation.checkpoint())
        self.assertFalse(continuation.needs_followup("third wording",
            review_with_risks([concern("Every failure yields '63'.")]), []))
        self.assertEqual(continuation.stop_reason, "answer_revision_budget")
        self.assertEqual(continuation.revision_requests, 2)


class ValidatedSemanticReviewIdentityTests(unittest.TestCase):
    def test_raw_invalid_or_successful_model_report_is_not_a_review_concern(self):
        finding = model_finding()
        self.assertEqual(len(risk_identities(validated_review(findings=[finding]))), 1)
        for changes in ({"validation": "rejected"}, {"validation": None}, {"status": "invalid"},
                        {"status": "model_reviewed"}):
            with self.subTest(changes=changes):
                self.assertEqual(risk_identities(validated_review(findings=[finding], **changes)), frozenset())

    def test_model_unit_ids_offsets_reason_and_reference_order_are_not_progress(self):
        first = model_finding()
        moved = deepcopy(first)
        moved.update(unit_id="unit_9", start_char=100, end_char=131, reason="Same concern explained differently.")
        moved["source_refs"][0]["reference_id"] = "ev_new"
        moved["source_refs"] *= 2
        self.assertEqual(risk_identities(validated_review(findings=[first])),
                         risk_identities(validated_review(findings=[moved])))

    def test_model_concern_migration_and_evidence_excerpt_changes_are_distinct(self):
        first = model_finding()
        for field, value in (("path", "other.cbl"), ("sha256", "c" * 64),
                             ("quote", "CALL 'OTHERQ' USING REQUEST-RECORD."),
                             ("source_context", {"include_chain": ["entry.cbl", "layout.cpy"]})):
            with self.subTest(field=field):
                changed = deepcopy(first)
                changed["source_refs"][0][field] = value
                previous = {"ledger": {"entries": []}, "assessment": {
                    "missing_aspects": ["semantic_review"], **validated_review(findings=[first])}}
                candidate = {"ledger": {"entries": []}, "assessment": {
                    "missing_aspects": ["semantic_review"], **validated_review(findings=[changed])}}
                self.assertEqual(revision_regression(previous, candidate), "ANSWER_BOUNDARIES_REGRESSED")

    def test_omissions_have_identity_and_do_not_disappear_when_findings_are_empty(self):
        omission = {"reason": "The normal-return precondition is missing.",
                    "source_refs": model_finding()["source_refs"]}
        first = validated_review(omissions=[omission])
        self.assertEqual(len(risk_identities(first)), 1)
        self.assertEqual(risk_identities(first), risk_identities(validated_review(omissions=[omission, omission])))
        changed = deepcopy(omission)
        changed["source_refs"][0]["path"] = "other.cbl"
        self.assertNotEqual(risk_identities(first), risk_identities(validated_review(omissions=[changed])))


if __name__ == "__main__":
    unittest.main()
