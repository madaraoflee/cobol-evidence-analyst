"""Narrow SQL answer review without model calls or database assumptions."""

import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from answer_sql_boundaries import assess_sql_answer_boundaries


def page(body=None, *, name="INQUIRY", identifier="ev_query"):
    text = body or (
        f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
        "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        "01 SQLCODE PIC S9(9).\n01 RESULT-AMOUNT PIC 9(8).\n"
        "01 RESULT-STATUS PIC XX.\nPROCEDURE DIVISION.\nMAIN.\n"
        "EXEC SQL SELECT AMOUNT INTO :RESULT-AMOUNT FROM RECORDS END-EXEC.\n"
        "IF SQLCODE = 0\nMOVE 'AA' TO RESULT-STATUS\n"
        "ELSE MOVE 'BB' TO RESULT-STATUS\nEND-IF.\nGOBACK.\n")
    return {"evidence_id": identifier, "relative_path": name + ".cbl", "source_text": text,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "start_line": 1,
            "end_line": len(text.splitlines()), "include_chain": []}


class SQLBoundaryReviewTests(unittest.TestCase):
    def review(self, text, pages=None):
        return assess_sql_answer_boundaries(text, [page()] if pages is None else pages)

    def test_read_only_does_not_prove_no_locks(self):
        text = "程序执行 SELECT。这是只读查询——不改表、不锁定、不写回。[ev_query]"
        result = self.review(text)
        self.assertEqual([r["kind"] for r in result["risks"]], ["SQL_RUNTIME_EFFECT_UNSUPPORTED"])
        risk = result["risks"][0]
        span = risk["answer_span"]
        self.assertEqual(text[span["start"]:span["end"]], span["text"])
        self.assertEqual(risk["source_refs"][0]["supplied_reference_ids"], ["ev_query"])
        self.assertEqual(risk["source_refs"][0]["start_line"], 10)
        self.assertFalse(result["semantic_verification"])

    def test_multiple_rows_conflicting_paths_are_both_nominated(self):
        text = ("SQLCODE只判断是否为零。查不到账号、查出多行、数据库报错，全部走同一个ELSE，"
                "一律返回'BB'。[ev_query]\n"
                "查询没有证明记录唯一。若真出现多行，行为将落到非零分支之外，"
                "只能以数据库实际语义为准。[ev_query]")
        risks = self.review(text)["risks"]
        self.assertEqual([r["kind"] for r in risks],
                         ["SQL_EVENT_BRANCH_UNSUPPORTED", "SQL_EVENT_BRANCH_UNSUPPORTED"])

    def test_sql_boundary_references_keep_copy_instance_and_replacement_identity(self):
        from semantic_risk_identity import risk_identities
        original = page("EXEC SQL SELECT AMOUNT INTO :RESULT-AMOUNT FROM RECORDS FOR UPDATE END-EXEC.")
        first = {**original, "evidence_id": "ev_first", "copy_instance": "copy-1",
                 "replacement_trace": [{"from": "INPUT", "to": "FIRST-INPUT"}]}
        for change in ({"copy_instance": "copy-2"},
                       {"replacement_trace": [{"from": "INPUT", "to": "SECOND-INPUT"}]}):
            second = {**first, **change, "evidence_id": "ev_second"}
            with self.subTest(change=change):
                both = self.review("This SELECT does not lock rows.", [first, second])
                refs = both["risks"][0]["source_refs"]
                self.assertEqual(len(refs), 2)
                self.assertNotEqual(refs[0]["source_binding_sha256"], refs[1]["source_binding_sha256"])
                old = self.review("This SELECT does not lock rows. [ev_first]", [first, second])
                new = self.review("This SELECT does not lock rows. [ev_second]", [first, second])
                self.assertNotEqual(risk_identities({"sql_boundary_review": old}),
                                    risk_identities({"sql_boundary_review": new}))

    def test_source_names_and_return_values_are_not_gold_constants(self):
        source = page(name="LOOKUP-TOTAL")
        source["source_text"] = source["source_text"].replace("'AA'", "'17'").replace("'BB'", "'42'")
        source["source_sha256"] = hashlib.sha256(source["source_text"].encode()).hexdigest()
        result = self.review("The query returns code '42' for multiple rows; multiple rows go to the ELSE branch.", [source])
        self.assertTrue(result["review_required"])

    def test_ordinary_source_bound_explanation_needs_no_review(self):
        for answer in (
            "SELECT把查询结果放入RESULT-AMOUNT。SQLCODE=0时返回'AA'，否则返回'BB'。",
            "本段查询没有UPDATE语句，但不能据此确认不加锁。",
            "查询不能确认不加锁，也不能断定失败时宿主变量保持原值。",
            "SELECT本身不能证明锁行为。多行对应哪个SQLCODE取决于未提供的数据库语义。",
            "The query reads AMOUNT. If SQLCODE is nonzero, the ELSE branch sets code 'BB'.",
            "SELECT does not prove that the query does not lock rows.",
            "The query is not guaranteed to be lock-free.",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.review(answer)["review_required"])

    def test_multiple_negation_questions_and_literal_quotes_are_not_assertions(self):
        for answer in (
            "不能说SELECT不会加锁。", "SELECT并非不锁定。", "不能确认查询不加锁。",
            "查询不会加锁吗？", "Does this query never lock rows?",
            "查询的“不锁定”说法没有得到证明。", 'The query description says \"does not lock\", without proof.',
            "> SELECT不锁定。\n", "```text\nSELECT不锁定。\n```\n",
            "SELECT读取`NO-LOCK-FLAG`字段，LOCK字段没有更新。",
            "查询中的不锁定字段是业务标志，不描述数据库锁。",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.review(answer)["review_required"])

    def test_conditional_status_mapping_is_not_database_event_mapping(self):
        for answer in (
            "如果多行查询时SQLCODE非零，则走ELSE分支。",
            "If multiple rows result in SQLCODE <> 0, the query takes the ELSE branch.",
            "若运行配置保证只读查询无锁，SELECT就不会加锁。",
            "If the isolation contract guarantees it, this query will not lock rows.",
            "SQL查询多行可能进入ELSE分支，当前不能确认。",
            "SQL查出多行并不代表一定走ELSE分支。",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.review(answer)["review_required"])

    def test_unrelated_qualification_does_not_hide_later_assertion(self):
        result = self.review("无法确定查询的部署环境，但SELECT不会阻塞其他事务。")
        self.assertTrue(result["review_required"])

    def test_indirect_route_questions_and_absent_guarantees_need_no_review(self):
        for answer in (
            "对于多行查询，是否走ELSE分支需要数据库约定，本段源码没有提供。",
            "对于数据库查询，查不到时走哪条分支取决于数据库返回的状态。",
            "这段SELECT未保证不会加锁。",
            "For multiple rows in this query, whether it takes the ELSE branch depends on the database contract.",
            "For this query, which branch is taken when no rows are found depends on the returned status.",
            "This SELECT does not guarantee that the query will not acquire locks.",
            "There is no guarantee that this SELECT is non-blocking.",
            "查询不锁定仍未得到证明。",
            "The claim that this SELECT does not lock remains unproven.",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.review(answer)["review_required"])

    def test_condition_in_previous_semicolon_clause_does_not_excuse_new_assertion(self):
        answer = "如果SQLCODE=0就返回AA；查询多行一定走ELSE分支。"
        self.assertEqual([r["kind"] for r in self.review(answer)["risks"]],
                         ["SQL_EVENT_BRANCH_UNSUPPORTED"])

    def test_sentence_citation_does_not_authorize_unrelated_later_claim(self):
        other = page("PROCEDURE DIVISION.\nMAIN.\nCONTINUE.\n", identifier="ev_other", name="OTHER")
        answer = "SELECT查询返回金额。[ev_query] 外部组件不会加锁。[ev_other]"
        self.assertFalse(self.review(answer, [page(), other])["review_required"])

    def test_report_size_and_source_refs_are_bounded_without_losing_identity(self):
        source = page("PROCEDURE DIVISION.\nMAIN.\n" +
            "EXEC SQL SELECT AMOUNT INTO :RESULT-AMOUNT FROM RECORDS END-EXEC.\n" * 200)
        answer = ("这些SQL查询不会加锁。" + "业务说明" * 200 + "[ev_query]\n") * 12
        result = self.review(answer, [source])
        self.assertTrue(result["review_required"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 8000)
        self.assertTrue(result["risks"])
        for risk in result["risks"]:
            self.assertLessEqual(len(risk["source_refs"]), 3)
            self.assertEqual(risk["omitted_source_refs"], 197)
            span = risk["answer_span"]
            self.assertEqual(span["sha256"], hashlib.sha256(answer[span["start"]:span["end"]].encode()).hexdigest())
            self.assertLessEqual(len(span["text"]), 512)

    def test_english_lock_assertions(self):
        for answer in (
            "SELECT不锁定。",
            "SELECT does not lock rows.", "The query will not acquire any locks.",
            "This SELECT is non-blocking.", "The query always locks the table.",
        ):
            with self.subTest(answer=answer):
                self.assertTrue(self.review(answer)["review_required"])

    def test_for_update_syntax_and_requested_intent_are_not_runtime_guarantees(self):
        source = page()
        source["source_text"] = source["source_text"].replace("FROM RECORDS END-EXEC", "FROM RECORDS FOR UPDATE END-EXEC")
        source["source_sha256"] = hashlib.sha256(source["source_text"].encode()).hexdigest()
        for answer in ("SQL显式包含FOR UPDATE，声明更新意图。", "The query requests update access with FOR UPDATE."):
            self.assertFalse(self.review(answer, [source])["review_required"])
        self.assertTrue(self.review("SELECT FOR UPDATE一定锁定整张表。", [source])["review_required"])

    def test_only_actual_complete_identified_sql_pages_nominate_review(self):
        source = page()
        for pages in (
            [], [{**source, "source_sha256": ""}],
            [{**source, "selection_reasons": ["repository_orientation"]}],
            [page("*> EXEC SQL SELECT A INTO :B END-EXEC.\n")],
            [page("EXEC SQL SELECT A INTO :B\n")],
            [page("DISPLAY 'EXEC SQL SELECT A INTO :B END-EXEC'.\n")],
        ):
            with self.subTest(pages=pages):
                self.assertFalse(self.review("SELECT不锁定。", pages)["review_required"])

    def test_other_cited_source_and_explicit_other_subject_are_not_bound_to_sql(self):
        source = page("PROCEDURE DIVISION.\nMAIN.\nCONTINUE.\n", identifier="ev_other", name="OTHER")
        self.assertFalse(self.review("查询不锁定。[ev_other]", [page(), source])["review_required"])
        self.assertFalse(self.review("Another query does not lock rows.")["review_required"])


if __name__ == "__main__":
    unittest.main()
