"""Protocol and provenance tests; model judgment accuracy is not certified here."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from semantic_answer_review import build_semantic_review, validate_semantic_review


SOURCE = "IF REQUEST-STATUS = ZERO\n    MOVE 'OK' TO RESPONSE-CODE\nEND-IF.\n"
MANUAL = "CHECK-REQUEST requests validation; it does not guarantee acceptance."


def source_page(**changes):
    return {"evidence_id": "ev_page_request", "relative_path": "request.cbl",
            "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
            "source_text": SOURCE, "start_line": 10, "end_line": 12,
            "include_chain": [], **changes}


def framework_reference(**changes):
    return {"reference_id": "fw:reference:3-3", "document_name": "guide.md",
            "document_sha256": hashlib.sha256(MANUAL.encode()).hexdigest(),
            "text": MANUAL, "start_line": 3, "end_line": 3, **changes}


def payload(answer="状态为零时，程序将返回码设为 OK。", *, pages=None, references=None):
    return build_semantic_review("这段处理在什么条件下返回成功？", answer,
        [source_page()] if pages is None else pages,
        [framework_reference()] if references is None else references)


def review_response(review_payload, *, verdict="supported"):
    return {"answer_sha256": review_payload["answer_sha256"],
            "evidence_sha256": review_payload["evidence_sha256"],
            "reviews": [{"unit_id": unit["unit_id"], "verdict": verdict,
                         "reason": "该判断符合本次可见条件及赋值。",
                         "evidence": ([{"reference_id": "ev_page_request", "quote": SOURCE}]
                                      if verdict in {"supported", "contradicted"} else [])}
                        for unit in review_payload["units"]], "omissions": []}


class SemanticReviewPayloadTests(unittest.TestCase):
    def test_complete_answer_has_exact_unicode_and_whitespace_spans(self):
        answer = "  # 结果\r\n\r\n状态为零时返回 OK。🙂\n\n| 条件 | 结果 |\n| 零 | OK |\n\n  "
        result = payload(answer)
        self.assertEqual(result["status"], "reviewable")
        self.assertEqual("".join(unit["text"] for unit in result["units"]), answer)
        self.assertNotIn("draft_answer", result)
        self.assertEqual(result["answer_sha256"], hashlib.sha256(answer.encode()).hexdigest())
        self.assertEqual(result["answer_characters"], len(answer))
        previous = 0
        for unit in result["units"]:
            self.assertEqual(unit["start_char"], previous)
            self.assertEqual(answer[unit["start_char"]:unit["end_char"]], unit["text"])
            self.assertLessEqual(len(unit["text"]), 1000)
            previous = unit["end_char"]
        self.assertEqual(previous, len(answer))
        self.assertFalse(result["semantic_verification"])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_full_length_limit_and_long_line_split_are_explicit(self):
        answer = "条" * 16000
        result = payload(answer)
        self.assertEqual(result["status"], "reviewable")
        self.assertEqual(len(result["units"]), 16)
        self.assertEqual("".join(row["text"] for row in result["units"]), answer)
        self.assertEqual(payload(answer + "件")["reason"], "ANSWER_LIMIT")
        self.assertEqual(payload("段落\n\n" * 33)["reason"], "ANSWER_UNIT_LIMIT")
        self.assertEqual(payload("段落\n\n" * 32)["status"], "reviewable")

    def test_all_splitline_separators_roundtrip(self):
        for separator in ("\r", "\r\n", "\n", "\v", "\f", "\x1c", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                answer = separator + "条件" + separator * 2 + "结果" + separator
                self.assertEqual("".join(row["text"] for row in payload(answer)["units"]), answer)

    def test_empty_or_non_text_answers_and_questions_are_not_reviewable(self):
        for answer in ("", " \n ", None, 42):
            self.assertEqual(payload(answer)["status"], "not_reviewable")
        for question in ("", None, "问" * 8001):
            self.assertEqual(build_semantic_review(question, "结果", [source_page()])["reason"], "QUESTION_LIMIT")

    def test_actual_originals_preserve_source_and_manual_identity(self):
        result = payload()
        rows = {row["kind"]: row for row in result["evidence"]}
        self.assertEqual(rows["source_page"]["text"], SOURCE)
        self.assertEqual(rows["source_page"]["path"], "request.cbl")
        self.assertEqual(rows["source_page"]["start_line"], 10)
        self.assertEqual(rows["framework_reference"]["text"], MANUAL)
        self.assertEqual(rows["framework_reference"]["sha256"], framework_reference()["document_sha256"])
        self.assertEqual(rows["source_page"]["text_sha256"], hashlib.sha256(SOURCE.encode()).hexdigest())

    def test_full_document_hash_need_not_equal_excerpt_hash(self):
        page = source_page(source_sha256="a" * 64)
        result = payload(pages=[page])
        self.assertEqual(result["status"], "reviewable")
        row = next(row for row in result["evidence"] if row["kind"] == "source_page")
        self.assertNotEqual(row["sha256"], row["text_sha256"])

    def test_binding_changes_for_each_material_or_instance_difference(self):
        original = payload()["evidence_sha256"]
        differences = (
            {"source_text": SOURCE + "CONTINUE.\n"}, {"relative_path": "other.cbl"},
            {"source_sha256": "a" * 64}, {"start_line": 9}, {"end_line": 13},
            {"span_truncated": True}, {"text_offset_chars": 8},
            {"include_chain": [{"relative_path": "caller.cbl", "line": 20}]},
            {"replacement_trace": [{"from": "OLD-VALUE", "to": "NEW-VALUE"}]},
            {"copy_instance": "copy-instance-two"},
        )
        for change in differences:
            with self.subTest(change=change):
                result = payload(pages=[source_page(**change)])
                self.assertEqual(result["status"], "reviewable")
                self.assertNotEqual(result["evidence_sha256"], original)
        self.assertNotEqual(payload(references=[framework_reference(document_sha256="b" * 64)])["evidence_sha256"], original)

    def test_context_is_copied_and_bounded(self):
        chain = [{"relative_path": "caller.cbl", "line": 4}]
        result = payload(pages=[source_page(include_chain=chain)])
        source = next(row for row in result["evidence"] if row["kind"] == "source_page")
        chain[0]["line"] = 5
        self.assertEqual(source["source_context"]["include_chain"][0]["line"], 4)
        for context in ([object()], [{"x": "a" * 2049}], list(range(65)), {1: "wrong"}, [[[[[[["deep"]]]]]]],
                        [{"x": ["x"] * 64}] * 8, [float("nan")]):
            with self.subTest(context_type=type(context).__name__):
                self.assertEqual(payload(pages=[source_page(include_chain=context)])["reason"], "INVALID_EVIDENCE_CONTEXT")

    def test_navigation_summaries_and_missing_identity_are_rejected(self):
        for changes in ({"source_text": "", "summary": "A business summary"},
                        {"source_text": None}, {"content_kind": "model_candidate"},
                        {"relative_path": None}, {"source_sha256": "not-a-hash"},
                        {"start_line": 0}, {"start_line": True}, {"end_line": 9},
                        {"span_truncated": "false"}, {"text_offset_chars": -1},
                        {"evidence_id": "internal-rule-001"}):
            with self.subTest(changes=changes):
                self.assertEqual(payload(pages=[source_page(**changes)])["status"], "not_reviewable")
        self.assertEqual(payload(references=[framework_reference(text="", summary=MANUAL)])["reason"], "RAW_EVIDENCE_REQUIRED")
        self.assertEqual(payload(pages=[], references=[])["reason"], "NO_VISIBLE_EVIDENCE")

    def test_duplicate_references_and_collection_limits_are_not_silently_dropped(self):
        self.assertEqual(payload(pages=[source_page(), source_page()])["reason"], "DUPLICATE_REFERENCE_ID")
        self.assertEqual(payload(pages=[source_page()] * 129)["reason"], "EVIDENCE_ITEM_LIMIT")
        self.assertEqual(payload(pages=[source_page(source_text="x" * 80000)])["reason"], "EVIDENCE_CHARACTER_LIMIT")
        self.assertEqual(payload(pages=[source_page(source_text="字" * 75000)], references=[])["reason"], "REVIEW_PAYLOAD_LIMIT")
        self.assertEqual(build_semantic_review("问", "答", {"page": source_page()})["reason"], "INVALID_EVIDENCE_COLLECTION")

    def test_binding_is_order_independent_without_mutating_inputs(self):
        pages = [source_page(), source_page(evidence_id="ev_page_other", relative_path="other.cbl")]
        before = copy.deepcopy(pages)
        self.assertEqual(payload(pages=pages)["evidence_sha256"], payload(pages=list(reversed(pages)))["evidence_sha256"])
        self.assertEqual(pages, before)

    def test_real_framework_context_references_are_accepted(self):
        from framework_knowledge import build_framework_context
        with tempfile.TemporaryDirectory() as temporary:
            reference_path = Path(temporary) / "guide.md"
            reference_path.write_text("# Request handling\n\n## CHECK-REQUEST\n" + MANUAL + "\n", encoding="utf-8")
            context = build_framework_context(reference_path=reference_path, question="CHECK-REQUEST", source_pages=[])
            self.assertTrue(context["references"])
            result = payload(references=context["references"])
            self.assertEqual(result["status"], "reviewable")
            actual = {row["reference_id"]: row for row in context["references"]}
            for row in result["evidence"]:
                if row["kind"] == "framework_reference":
                    self.assertEqual(row["text"], actual[row["reference_id"]]["text"])
                    self.assertEqual(row["sha256"], actual[row["reference_id"]]["document_sha256"])


class SemanticReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.payload = payload("# 处理结果\n\n状态为零时返回 OK。\n\n仍需确认调用者怎样使用结果。")
        self.response = review_response(self.payload)

    def validate(self, response=None, review_payload=None):
        return validate_semantic_review(json.dumps(self.response if response is None else response, ensure_ascii=False),
                                        self.payload if review_payload is None else review_payload)

    def assertInvalid(self, response=None, *, reason=None, review_payload=None):
        result = self.validate(response, review_payload)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["validation"], "rejected")
        self.assertFalse(result["semantic_verification"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["omissions"], [])
        self.assertFalse(result["coverage"]["complete"])
        if reason is not None:
            self.assertEqual(result["reason"], reason)
        return result

    def test_complete_review_allows_non_claim_headings_but_never_proves_semantics(self):
        self.response["reviews"][0].update(verdict="non_claim", evidence=[], reason="这是段落标题。")
        result = self.validate()
        self.assertEqual(result["status"], "model_reviewed")
        self.assertEqual(result["validation"], "strict_payload_bound")
        self.assertFalse(result["semantic_verification"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["coverage"]["reviewed_units"], len(self.payload["units"]))
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["verdict_counts"]["non_claim"], 1)

    def test_exact_quote_does_not_let_local_validator_prove_model_judgment(self):
        review_payload = payload("任何状态下都必定返回 OK。")
        response = review_response(review_payload)
        response["reviews"][0]["reason"] = "模型错误地声称无条件支持。"
        result = self.validate(response, review_payload)
        self.assertEqual(result["status"], "model_reviewed")
        self.assertFalse(result["semantic_verification"])
        self.assertNotIn("verified", result)

    def test_non_claim_for_every_unit_is_not_a_completed_review(self):
        for row in self.response["reviews"]:
            row.update(verdict="non_claim", evidence=[])
        self.assertInvalid(reason="NO_SUBSTANTIVE_REVIEW")

    def test_changes_retain_exact_answer_span_and_original_provenance(self):
        self.response["reviews"][1].update(verdict="contradicted", reason="返回码赋值有明确前提。")
        self.response["reviews"][2].update(verdict="insufficient", evidence=[], reason="没有调用者原文。")
        result = self.validate()
        self.assertEqual(result["status"], "changes_requested")
        self.assertEqual([row["kind"] for row in result["findings"]], ["contradicted", "insufficient"])
        answer = "".join(row["text"] for row in self.payload["units"])
        for row in result["findings"]:
            self.assertEqual(row["quote"], answer[row["start_char"]:row["end_char"]])
        ref = result["findings"][0]["source_refs"][0]
        self.assertEqual(ref["path"], "request.cbl")
        self.assertEqual(ref["sha256"], source_page()["source_sha256"])
        self.assertEqual((ref["start_line"], ref["end_line"]), (10, 12))
        self.assertEqual(ref["quote"], SOURCE)
        self.assertEqual(result["findings"][1]["source_refs"], [])

    def test_omissions_request_revision_only_with_visible_support(self):
        self.response["omissions"] = [{"reason": "需解释状态判断与返回结果的连接。",
            "evidence": [{"reference_id": "ev_page_request", "quote": SOURCE}]}]
        result = self.validate()
        self.assertEqual(result["status"], "changes_requested")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["omissions"][0]["source_refs"][0]["path"], "request.cbl")
        self.response["omissions"][0]["evidence"] = []
        self.assertInvalid(reason="INVALID_CITATION_COUNT")

    def test_omission_count_duplicates_reason_and_structure_are_strict(self):
        omission = {"reason": "缺少关键条件说明。", "evidence": [{"reference_id": "ev_page_request", "quote": SOURCE}]}
        for omissions, reason in (([omission] * 9, "OMISSION_LIMIT"),
                                  ([omission] * 2, "DUPLICATE_OMISSION"),
                                  ([{**omission, "tool": "lookup"}], "INVALID_OMISSION"),
                                  ([{**omission, "reason": " "}], "INVALID_REASON")):
            with self.subTest(reason=reason):
                self.response["omissions"] = omissions
                self.assertInvalid(reason=reason)

    def test_missing_extra_unknown_and_duplicate_units_are_rejected(self):
        for change, reason in ((lambda rows: rows.pop(), "INCOMPLETE_UNIT_COVERAGE"),
                               (lambda rows: rows.append(copy.deepcopy(rows[0])), "INCOMPLETE_UNIT_COVERAGE"),
                               (lambda rows: rows[0].update(unit_id="unit-missing"), "UNKNOWN_OR_DUPLICATE_UNIT"),
                               (lambda rows: rows[1].update(unit_id=rows[0]["unit_id"]), "UNKNOWN_OR_DUPLICATE_UNIT")):
            with self.subTest(reason=reason):
                response = copy.deepcopy(self.response)
                change(response["reviews"])
                self.assertInvalid(response, reason=reason)

    def test_response_order_does_not_change_required_coverage(self):
        self.response["reviews"].reverse()
        self.assertEqual(self.validate()["status"], "model_reviewed")

    def test_answer_and_evidence_binding_must_both_match(self):
        for field in ("answer_sha256", "evidence_sha256"):
            response = copy.deepcopy(self.response)
            response[field] = "0" * 64
            self.assertInvalid(response, reason="REVIEW_BINDING_MISMATCH")

    def test_wrong_verdict_reason_or_extra_unit_keys_are_rejected(self):
        for change, reason in (({"verdict": "verified"}, "INVALID_VERDICT"),
                               ({"verdict": []}, "INVALID_VERDICT"),
                               ({"reason": ""}, "INVALID_REASON"),
                               ({"reason": "x" * 601}, "INVALID_REASON"),
                               ({"reason": 1}, "INVALID_REASON"),
                               ({"action": {"tool": "source_lookup"}}, "INVALID_UNIT_REVIEW")):
            with self.subTest(reason=reason):
                response = copy.deepcopy(self.response)
                response["reviews"][0].update(change)
                self.assertInvalid(response, reason=reason)

    def test_supported_or_contradicted_cannot_have_empty_evidence(self):
        for verdict in ("supported", "contradicted"):
            self.response["reviews"][0].update(verdict=verdict, evidence=[])
            self.assertInvalid(reason="INVALID_CITATION_COUNT")

    def test_reference_ids_and_quotes_must_match_visible_originals(self):
        for citation, reason in (({"reference_id": "ev_not_supplied", "quote": SOURCE}, "UNKNOWN_REFERENCE"),
                                 ({"reference_id": "internal_rule", "quote": SOURCE}, "UNKNOWN_REFERENCE"),
                                 ({"reference_id": "ev_page_request", "quote": "ELSE RETURN OK"}, "QUOTE_NOT_VISIBLE"),
                                 ({"reference_id": "ev_page_request", "quote": "状态为零时返回 OK。"}, "QUOTE_NOT_VISIBLE"),
                                 ({"reference_id": "ev_page_request", "quote": " "}, "INVALID_QUOTE"),
                                 ({"reference_id": "ev_page_request", "quote": "x" * 1001}, "INVALID_QUOTE"),
                                 ({"reference_id": "ev_page_request", "quote": SOURCE, "tool": "read"}, "INVALID_CITATION")):
            with self.subTest(reason=reason):
                self.response["reviews"][0]["evidence"] = [citation]
                self.assertInvalid(reason=reason)

    def test_manual_quotes_cannot_be_borrowed_by_source_id(self):
        self.response["reviews"][0]["evidence"] = [{"reference_id": "ev_page_request", "quote": MANUAL}]
        self.assertInvalid(reason="QUOTE_NOT_VISIBLE")
        self.response["reviews"][0]["evidence"][0]["reference_id"] = "fw:reference:3-3"
        self.assertEqual(self.validate()["status"], "model_reviewed")

    def test_non_claim_cannot_supply_citations_or_duplicate_them(self):
        self.response["reviews"][0]["verdict"] = "non_claim"
        self.assertInvalid(reason="NON_CLAIM_HAS_EVIDENCE")
        self.response["reviews"][0]["verdict"] = "supported"
        citation = self.response["reviews"][0]["evidence"][0]
        self.response["reviews"][0]["evidence"] = [citation] * 2
        self.assertInvalid(reason="DUPLICATE_CITATION")
        self.response["reviews"][0]["evidence"] = [citation] * 5
        self.assertInvalid(reason="INVALID_CITATION_COUNT")

    def test_json_fences_trailing_actions_and_duplicate_keys_are_rejected(self):
        encoded = json.dumps(self.response)
        for text in ("```json\n" + encoded + "\n```", encoded + "\nTool: search",
                     encoded + encoded, encoded[:-1] + ',"omissions":[]}',
                     encoded.replace('"reason":', '"reason": NaN, "reason":', 1),
                     '[]', 'null', '[[' * 1000 + '0' + ']]' * 1000):
            with self.subTest(text=text[:30]):
                result = validate_semantic_review(text, self.payload)
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["findings"], [])
        self.response["tool_calls"] = [{"name": "read_source"}]
        self.assertInvalid(reason="INVALID_REVIEW_OBJECT")

    def test_non_text_or_oversized_response_is_rejected(self):
        for text in (self.response, None, " " * 64001, "字" * 22000):
            result = validate_semantic_review(text, self.payload)
            self.assertEqual(result["status"], "invalid")
            self.assertEqual(result["reason"], "RESPONSE_SIZE_LIMIT")

    def test_payload_tampering_is_rejected_before_accepting_any_judgment(self):
        def alter_text(result):
            result["units"][0]["text"] += "新增断言"
        changes = (lambda result: result["units"][0].update(start_char=1), alter_text,
                   lambda result: result.update(answer_sha256="0" * 64),
                   lambda result: result["evidence"][0].update(text=SOURCE + "CONTINUE."),
                   lambda result: result["evidence"][0].update(sha256="0" * 64),
                   lambda result: result.update(evidence_sha256="0" * 64),
                   lambda result: result["evidence"][0].update(source_context={"include_chain": ["other"]}))
        for change in changes:
            altered = copy.deepcopy(self.payload)
            change(altered)
            self.assertInvalid(review_payload=altered)
        self.assertInvalid(review_payload={"status": "not_reviewable"}, reason="PAYLOAD_NOT_REVIEWABLE")

    def test_late_invalid_item_never_returns_partial_findings(self):
        self.response["reviews"][0].update(verdict="contradicted")
        self.response["reviews"][-1]["evidence"][0]["quote"] = "not visible"
        result = self.assertInvalid(reason="QUOTE_NOT_VISIBLE")
        self.assertEqual(result["coverage"]["reviewed_units"], 0)

    def test_metadata_actions_and_unbounded_trees_cannot_enter_payload(self):
        altered = copy.deepcopy(self.payload)
        altered["evidence"][0]["source_context"] = {"tool_call": "read_file"}
        self.assertInvalid(review_payload=altered, reason="INVALID_EVIDENCE_CONTEXT")


if __name__ == "__main__":
    unittest.main()
