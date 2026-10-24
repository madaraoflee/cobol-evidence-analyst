"""A formula and its condition must belong to the same supplied source branch.

Fixed local replies test source binding, not a model's business understanding.
"""

import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from claim_ledger import review_answer_claims
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. BRANCH-RULE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 IN-AMOUNT PIC S99.
01 OUT-AMOUNT PIC S999.
01 REVIEW-FLAG PIC 9.
PROCEDURE DIVISION.
MAIN.
IF IN-AMOUNT > ZERO
COMPUTE OUT-AMOUNT = IN-AMOUNT * 2
ELSE
COMPUTE OUT-AMOUNT = IN-AMOUNT * 3
END-IF.
GOBACK.
"""
GOOD = ("在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 2；"
        "否则 OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]")
BAD = ("在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3；"
       "否则 OUT-AMOUNT = IN-AMOUNT * 2。[ev_rule]")


def page(source=SOURCE, **changes):
    return {"evidence_id": "ev_rule", "relative_path": "rule.cbl", "source_text": source,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "start_line": 1, "end_line": len(source.splitlines()), **changes}


class ProseBranchBindingTests(unittest.TestCase):
    def review(self, text=BAD, pages=None):
        pages = [page()] if pages is None else pages
        rendered, ledger = review_answer_claims(text, pages,
            {item["evidence_id"]: item for item in pages}, source_format="free")
        return rendered, ledger.to_dict()

    def assignments(self, ledger):
        return [entry for entry in ledger["entries"] if entry["claim_type"] == "assignment_statement"]

    def test_reversed_natural_branches_are_repaired_with_true_and_false_source_guards(self):
        rendered, ledger = self.review()
        self.assertNotEqual(rendered, BAD)
        self.assertEqual([entry["support_status"] for entry in self.assignments(ledger)],
                         ["contradicted", "contradicted"])
        self.assertFalse(ledger["all_claims_supported"])
        self.assertIn("否则 OUT-AMOUNT = IN-AMOUNT * 3", rendered)
        expected = [("true", "2", 11), ("false", "3", 13)]
        for entry, (outcome, multiplier, line) in zip(self.assignments(ledger), expected):
            revised, = entry["revised_claims"]
            self.assertEqual(revised["assertion"]["expression"], ["IN-AMOUNT", "*", multiplier])
            self.assertEqual(revised["source_guards"][0]["outcome"], outcome)
            self.assertEqual(entry["source_versions"][0]["start_line"], line)
            self.assertEqual(entry["source_versions"][0]["source_sha256"], page()["source_sha256"])

    def test_correct_branches_remain_verbatim_and_checked_against_their_own_occurrences(self):
        rendered, ledger = self.review(GOOD)
        self.assertEqual(rendered, GOOD)
        self.assertEqual(ledger["revised_claim_count"], 0)
        self.assertTrue(ledger["all_claims_supported"])
        self.assertNotEqual(*[entry["occurrence_ids"] for entry in self.assignments(ledger)])

    def test_chinese_and_english_explicit_conditions_bind_the_same_source_branch(self):
        for prefix in ("若 IN-AMOUNT > ZERO，", "如果 IN-AMOUNT > ZERO，",
                       "在 IN-AMOUNT > ZERO 的條件下，", "If IN-AMOUNT > ZERO, ",
                       "When IN-AMOUNT > ZERO, "):
            with self.subTest(prefix=prefix):
                rendered, ledger = self.review(prefix + "OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]")
                self.assertEqual(self.assignments(ledger)[0]["support_status"], "contradicted")
                self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", rendered)
        text = "If IN-AMOUNT > ZERO, OUT-AMOUNT = IN-AMOUNT * 3; otherwise OUT-AMOUNT = IN-AMOUNT * 2.[ev_rule]"
        rendered, ledger = self.review(text)
        self.assertEqual([entry["support_status"] for entry in self.assignments(ledger)],
                         ["contradicted", "contradicted"])
        self.assertIn("otherwise OUT-AMOUNT = IN-AMOUNT * 3", rendered)

    def test_local_formula_repair_preserves_the_business_explanation_in_its_clause(self):
        for formula in ("OUT-AMOUNT = IN-AMOUNT * 3", "`OUT-AMOUNT = IN-AMOUNT * 3`",
                        "`OUT-AMOUNT` = `IN-AMOUNT * 3`"):
            with self.subTest(formula=formula):
                text = "在 IN-AMOUNT > ZERO 的条件下，" + formula + "，这个金额交给后续报表流程。[ev_rule]"
                rendered, ledger = self.review(text)
                self.assertIn("这个金额交给后续报表流程", rendered)
                self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", rendered)
                self.assertNotIn("IN-AMOUNT * 3", rendered)
                self.assertEqual(self.assignments(ledger)[0]["support_status"], "contradicted")

    def test_several_writes_in_one_branch_do_not_erase_a_business_explanation(self):
        source = SOURCE.replace("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2", "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\n"
            "COMPUTE OUT-AMOUNT = OUT-AMOUNT + 1")
        text = "在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 8，这个金额交给后续报表流程。[ev_rule]"
        rendered, ledger = self.review(text, [page(source)])
        self.assertEqual(rendered, text)
        self.assertEqual(self.assignments(ledger)[0]["reason_code"], "PROSE_BRANCH_ASSIGNMENT_AMBIGUOUS")
        self.assertEqual(ledger["counts"]["contradicted"], 0)

    def test_repeated_branch_clauses_reuse_one_supplied_block_parse(self):
        import claim_ledger
        real = claim_ledger._prose_branch_facts
        with patch.object(claim_ledger, "_prose_branch_facts", wraps=real) as branch_facts:
            rendered, ledger = self.review("\n".join([GOOD] * 20))
        self.assertEqual(rendered, "\n".join([GOOD] * 20))
        self.assertEqual(branch_facts.call_count, 1)
        self.assertEqual(len(self.assignments(ledger)), 40)

    def test_missing_outer_guard_is_uncertain_not_a_wrong_formula(self):
        nested = SOURCE.replace("IF IN-AMOUNT", "IF REVIEW-FLAG = 1\nIF IN-AMOUNT").replace(
            "END-IF.", "END-IF\nEND-IF.")
        rendered, ledger = self.review(BAD, [page(nested)])
        self.assertEqual(rendered, BAD)
        self.assertEqual([entry["support_status"] for entry in self.assignments(ledger)],
                         ["unsupported", "unsupported"])
        self.assertEqual(ledger["counts"]["contradicted"], 0)

    def test_repeated_condition_occurrences_do_not_establish_one_branch_binding(self):
        repeated = SOURCE.replace("GOBACK.", "IF IN-AMOUNT > ZERO\n"
            "COMPUTE OUT-AMOUNT = IN-AMOUNT * 3\nEND-IF.\nGOBACK.")
        rendered, ledger = self.review(BAD, [page(repeated)])
        self.assertEqual(rendered, BAD)
        self.assertTrue(all(entry["support_status"] == "unsupported" for entry in self.assignments(ledger)))

    def test_different_program_or_version_cannot_lend_a_branch(self):
        other = SOURCE.replace("BRANCH-RULE", "OTHER-RULE").replace("* 2", "* 8").replace("* 3", "* 9")
        for second in (page(other, evidence_id="ev_other", relative_path="other.cbl"),
                       page(other, evidence_id="ev_other")):
            with self.subTest(path=second["relative_path"]):
                text = BAD.replace("[ev_rule]", "[ev_rule][ev_other]")
                rendered, ledger = self.review(text, [page(), second])
                self.assertEqual(rendered, text)
                self.assertTrue(all(entry["support_status"] == "unsupported" for entry in self.assignments(ledger)))

    def test_else_does_not_inherit_across_independent_sentences_lines_or_titles(self):
        for separator in ("。", ". ", "；\n", "；\n\n## 另一个情况\n", "；\n|结果|\n|---|\n"):
            with self.subTest(separator=separator):
                text = GOOD.replace("；", separator)
                rendered, ledger = self.review(text)
                self.assertEqual(rendered, text)
                self.assertEqual(self.assignments(ledger)[-1]["support_status"], "unsupported")

    def test_else_does_not_inherit_a_different_citation_scope(self):
        other = page(SOURCE.replace("BRANCH-RULE", "OTHER-RULE"), evidence_id="ev_other", relative_path="other.cbl")
        text = GOOD.replace("；", "[ev_rule]；").replace("* 3。[ev_rule]", "* 3。[ev_other]")
        rendered, ledger = self.review(text, [page(), other])
        self.assertEqual(rendered, text)
        self.assertEqual(self.assignments(ledger)[-1]["support_status"], "unsupported")

    def test_truncated_or_uncited_branch_stays_unverified_without_rewriting(self):
        for supplied in (page(span_truncated=True),
                         page("COMPUTE OUT-AMOUNT = IN-AMOUNT * 3.\n", start_line=13, end_line=13)):
            with self.subTest(page=supplied):
                rendered, ledger = self.review(BAD, [supplied])
                self.assertEqual(rendered, BAD)
                self.assertEqual(ledger["counts"]["contradicted"], 0)
                self.assertTrue(all(entry["support_status"] == "unsupported" for entry in self.assignments(ledger)))

    def test_later_assignment_prevents_branch_formula_becoming_a_final_value_proof(self):
        source = SOURCE.replace("GOBACK.", "MOVE 7 TO OUT-AMOUNT.\nGOBACK.")
        text = "在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT 最终等于 IN-AMOUNT * 2。[ev_rule]"
        rendered, ledger = self.review(text, [page(source)])
        self.assertEqual(rendered, text)
        entry, = self.assignments(ledger)
        self.assertEqual(entry["support_status"], "unsupported")
        self.assertEqual(entry["reason_code"], "FINAL_VALUE_REQUIRES_CONTROL_AND_OVERWRITE_ANALYSIS")
        self.assertIn(["7"], [row["assertion"]["expression"] for row in entry["verification_facts"]])

    def test_general_explanation_and_quoted_control_excerpt_keep_existing_behavior(self):
        for text in ("金额为正时按正常系数计算，否则使用另一系数。[ev_rule]",
                     "源码包含 OUT-AMOUNT = IN-AMOUNT * 2 和另一条计算规则。[ev_rule]",
                     "`IF IN-AMOUNT > ZERO COMPUTE OUT-AMOUNT = IN-AMOUNT * 2 ELSE "
                     "COMPUTE OUT-AMOUNT = IN-AMOUNT * 3 END-IF`。[ev_rule]"):
            with self.subTest(text=text):
                rendered, ledger = self.review(text)
                self.assertEqual(rendered, text)
                self.assertEqual(ledger["counts"]["contradicted"], 0)

    def test_denied_uncertain_and_hypothetical_branch_formulas_are_not_positive_claims(self):
        for text in (
            "若 IN-AMOUNT > ZERO，不能断言 OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]",
            "在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3 是错误的说法。[ev_rule]",
            "在 IN-AMOUNT > ZERO 的条件下，尚不能确定 OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]",
            "若 IN-AMOUNT > ZERO，OUT-AMOUNT = IN-AMOUNT * 3 仅是假设。[ev_rule]",
            "若 IN-AMOUNT > ZERO，OUT-AMOUNT = IN-AMOUNT * 3 没有源码依据。[ev_rule]",
            "若 IN-AMOUNT > ZERO，OUT-AMOUNT = IN-AMOUNT * 3 这个公式不成立。[ev_rule]",
            "If IN-AMOUNT > ZERO, OUT-AMOUNT = IN-AMOUNT * 3 is incorrect.[ev_rule]",
            "When IN-AMOUNT > ZERO, it is not true that OUT-AMOUNT = IN-AMOUNT * 3.[ev_rule]",
            "If IN-AMOUNT > ZERO, OUT-AMOUNT = IN-AMOUNT * 3 is unconfirmed.[ev_rule]",
            "If IN-AMOUNT > ZERO, OUT-AMOUNT = IN-AMOUNT * 3 is a hypothetical assumption.[ev_rule]",
            GOOD.replace("否则 OUT-AMOUNT", "否则不能断言 OUT-AMOUNT").replace("* 3", "* 2"),
        ):
            with self.subTest(text=text):
                rendered, ledger = self.review(text)
                self.assertEqual(rendered, text)
                self.assertFalse(ledger["all_claims_supported"])
                if self.assignments(ledger):
                    self.assertEqual(self.assignments(ledger)[-1]["support_status"], "unsupported")
                self.assertEqual(ledger["counts"]["contradicted"], 0)

    def test_unclosed_source_block_does_not_prove_branch_ownership(self):
        source = SOURCE.partition("END-IF.")[0]
        rendered, ledger = self.review(BAD, [page(source)])
        self.assertEqual(rendered, BAD)
        self.assertTrue(all(entry["support_status"] == "unsupported" for entry in self.assignments(ledger)))
        self.assertEqual(ledger["counts"]["contradicted"], 0)


class ProseBranchBindingIntegrationTests(unittest.TestCase):
    def test_single_offline_request_repairs_both_branches_before_final_delivery(self):
        requests, raw_answers = [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            path = source / "rule.cbl"
            path.write_text(SOURCE, encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model", api_key="unused")

            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                requests.append(payload)
                supplied = [item for context in payload["source_context"] for item in context.get("pages", [])]
                evidence = next(item for item in supplied if "IF IN-AMOUNT > ZERO" in item["source_text"]
                                and "COMPUTE OUT-AMOUNT = IN-AMOUNT * 3" in item["source_text"])
                answer = BAD.replace("ev_rule", evidence["evidence_id"])
                raw_answers.append(answer)
                return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": answer},
                                                                        "finish_reason": "stop"}]}, ensure_ascii=False))

            with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
                 patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden")), \
                 patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
                result = run_business_chat("BRANCH-RULE OUT-AMOUNT 如何计算？", database, source, config,
                    transport=transport, allow_network=False, framework_reference_path="",
                    policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))["agent_result"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["metrics"]["model_requests"], 1)
        self.assertNotEqual(result["answer"], raw_answers[0])
        self.assertEqual(result["claim_ledger"]["counts"]["contradicted"], 2)
        self.assertIn("否则 OUT-AMOUNT = IN-AMOUNT * 3", result["answer"])
        self.assertFalse(result["claims_semantically_verified"])
        for citation in result["narrative"]["citations"]:
            self.assertEqual(citation["source_sha256"], page()["source_sha256"])


if __name__ == "__main__":
    unittest.main()
