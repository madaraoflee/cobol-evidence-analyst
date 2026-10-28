from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conversation_store import ConversationStore, projected_conversation_title
from conversation_titles import MAX_TITLE_CHARACTERS, summarize_question


class ConversationTitleTests(unittest.TestCase):
    def test_titles_name_the_topic_and_inquiry_in_the_question_language(self):
        cases = {
            "请帮我分析 CLAIM01 在退款时为什么会跳过金额检查？": "CLAIM01 在退款时跳过金额检查 · 原因",
            "請幫我分析 CLAIM01 在退款時為什麼會跳過金額檢查？": "CLAIM01 在退款時跳過金額檢查 · 原因",
            "请帮我说明 ORDER_MAIN 的业务逻辑是什么？": "ORDER_MAIN 的业务逻辑",
            "请解释一下 TOTAL-AMOUNT 是什么意思？": "TOTAL-AMOUNT · 含义",
            "请分析 ORDER01 的折扣金额从哪里来？": "ORDER01 的折扣金额 · 来源",
            "请比较 ORDER01 和 ORDER02 的处理差异。": "ORDER01 和 ORDER02 的处理 · 差异",
            "Please explain why ORDER01 skips validation when status is pending.": "Cause: ORDER01 skips validation when status is pending",
            "What is the difference between ORDER01 and ORDER02?": "Comparison: ORDER01 and ORDER02",
            "PAY001 是干什么的？": "PAY001 · 用途",
            "PAY001 做什么用的": "PAY001 · 用途",
            "PAY001 的作用是什么？": "PAY001 · 用途",
            "123456 的作用是什么？": "123456 · 用途",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(summarize_question(question), expected)

    def test_pasted_code_and_format_instructions_do_not_become_the_title(self):
        question = (
            "请按以下要求回答：\n使用 Markdown 表格，答案控制在500字以内。\n"
            "```cobol\nPROGRAM-ID. ORDER01.\nMOVE A TO B.\n```\n"
            "实际问题：ORDER01 的折扣金额从哪里来？")
        self.assertEqual(summarize_question(question), "ORDER01 的折扣金额 · 来源")
        long_code = "```cobol\nPROGRAM-ID. ORDER01.\n" + "MOVE A TO B.\n" * 2000 + "```\n问题：ORDER01 的作用是什么？"
        self.assertEqual(summarize_question(long_code), "ORDER01 · 用途")

    def test_newcomer_preface_and_same_line_answer_format_do_not_become_the_topic(self):
        self.assertEqual(summarize_question(
            "我是新人，请解释这份源码实现的业务流程，先讲业务目的、规则和结果，再按 Batch job、Online function / Programs、Tables、Files 说明各自职责和关系。"),
            "源码业务流程")
        title = summarize_question(
            "我是新人，请解释 BALANCEQ 这个在线查询功能的业务用途、输入输出和 tables，哪些条件会返回失败？")
        self.assertTrue(title.startswith("BALANCEQ 在线查询功能"), title)
        self.assertNotIn("我是新人", title)
        self.assertNotIn("请解释", title)
        self.assertLessEqual(len(title), 40)

    def test_markdown_does_not_change_identifiers_or_drop_conditional_questions(self):
        self.assertEqual(summarize_question("请解释 **WS_TOTAL** 是什么意思？"), "WS_TOTAL · 含义")
        title = summarize_question("If WS_TOTAL exceeds the limit, why is validation skipped?")
        self.assertIn("WS_TOTAL", title)
        self.assertIn("validation", title)
        self.assertNotEqual(title, "Source analysis")

    def test_fitting_multiple_identifiers_never_invents_a_truncated_program_name(self):
        title = summarize_question(
            "请分析退款流程经过 ORDER_MAIN_202601 然后交给 REPORT_STAGE_04 处理时为什么发生变化？")
        self.assertLessEqual(len(title), 40)
        self.assertIn("REPORT_STAGE_04", title)
        self.assertNotIn("ORDER_MAIN_202…", title)
        self.assertNotIn("ORDER_MAIN…", title)
        self.assertTrue("ORDER_MAIN_202601" in title or "ORDER_MAIN" not in title)

    def test_generic_followups_wait_for_an_actual_topic_and_long_titles_are_bounded(self):
        for text in ("为什么？", "继续说明。", "Thanks!", "Why?", "", "Explain"):
            self.assertIsNone(summarize_question(text), text)
        title = summarize_question("Please explain why the processing of " + "pending requests " * 20 + "fails in ORDER_MAIN_02?")
        self.assertLessEqual(len(title), MAX_TITLE_CHARACTERS)
        self.assertIn("ORDER_MAIN_02", title)
        self.assertNotIn("Please", title)


class ConversationTitleStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ConversationStore(self.root / "output", self.root / "source")
        self.identifier = self.store.create()["id"]

    def test_new_title_is_saved_and_generic_followups_and_answers_cannot_replace_it(self):
        question = "请帮我说明 ORDER_MAIN 的业务逻辑是什么？"
        self.store.append(self.identifier, "user", question)
        expected = "ORDER_MAIN 的业务逻辑"
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT title FROM conversations").fetchone()[0], expected)
        self.store.append(self.identifier, "assistant", "Provider failed; no answer.", status="failed")
        self.store.append(self.identifier, "user", "为什么？")
        reopened = ConversationStore(self.root / "output", self.root / "source")
        for row in (reopened.get(self.identifier), reopened.get_metadata(self.identifier), reopened.list()[0]):
            self.assertEqual(row["title"], expected)
        self.assertEqual(reopened.get(self.identifier)["messages"][0]["content"], question)

    def test_greeting_does_not_hide_the_first_substantive_topic(self):
        self.store.append(self.identifier, "user", "你好")
        self.store.append(self.identifier, "assistant", "请提出问题。")
        self.store.append(self.identifier, "user", "PAY001 的作用是什么？")
        self.assertEqual(self.store.get_metadata(self.identifier)["title"], "PAY001 · 用途")

    def test_a_later_substantive_question_can_name_a_long_greeting_only_conversation(self):
        for _ in range(10):
            self.store.append(self.identifier, "user", "你好")
            self.store.append(self.identifier, "assistant", "请提出问题。")
        self.store.append(self.identifier, "user", "PAY001 的作用是什么？")
        self.assertEqual(self.store.get_metadata(self.identifier)["title"], "PAY001 · 用途")
        self.store.append(self.identifier, "user", "为什么？")
        self.assertEqual(self.store.list()[0]["title"], "PAY001 · 用途")

    def test_legacy_automatic_title_is_projected_readonly_without_mutating_history_or_times(self):
        question = "请帮我分析 CLAIM01 在退款时为什么会跳过金额检查？"
        self.store.append(self.identifier, "user", question)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE conversations SET title=?", (question[:80],))
            before = db.execute("SELECT * FROM conversations").fetchone()
            transcript = db.execute("SELECT * FROM messages").fetchall()
        with closing(sqlite3.connect(f"{self.store.path.as_uri()}?mode=ro", uri=True)) as db:
            self.assertEqual(projected_conversation_title(db, self.identifier, question[:80]),
                             "CLAIM01 在退款时跳过金额检查 · 原因")
        for row in (self.store.get(self.identifier), self.store.get_metadata(self.identifier), self.store.list()[0]):
            self.assertEqual(row["title"], "CLAIM01 在退款时跳过金额检查 · 原因")
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT * FROM conversations").fetchone(), before)
            self.assertEqual(db.execute("SELECT * FROM messages").fetchall(), transcript)

    def test_custom_title_is_preserved(self):
        self.store.append(self.identifier, "user", "PAY001 的作用是什么？")
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE conversations SET title='Release review notes'")
        self.store.append(self.identifier, "user", "请再解释 ORDER02。")
        self.assertEqual(self.store.get_metadata(self.identifier)["title"], "Release review notes")

    def test_title_projection_never_reads_model_details_or_unbounded_user_bodies(self):
        question = "请按以下要求回答：\n```cobol\n" + "MOVE A TO B.\n" * 10000 + "```\n问题：PAY001 的作用是什么？"
        self.store.append(self.identifier, "user", question)
        self.store.append(self.identifier, "assistant", "Large answer", details={"trace": "x" * 100000})
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE conversations SET title=?", (question[:80],))
        with mock.patch("conversation_store.json.loads", side_effect=AssertionError("diagnostic details decoded")):
            self.assertEqual(self.store.list()[0]["title"], "PAY001 · 用途")
            self.assertEqual(self.store.get_metadata(self.identifier)["title"], "PAY001 · 用途")
        self.assertEqual(self.store.get(self.identifier)["messages"][0]["content"], question)


if __name__ == "__main__":
    unittest.main()
