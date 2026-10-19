"""Targeted repairs preserve useful prose and genuine source/runtime boundaries."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source_availability import repair_source_availability_assertions, source_availability_contradictions


class SourceAvailabilityRepairTests(unittest.TestCase):
    def setUp(self):
        body = "MAIN.\nMOVE 1 TO AMOUNT.\nGOBACK."
        self.page = {"relative_path": "programs/rule.cbl", "source_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "source_text": body, "start_line": 1, "end_line": 3, "evidence_id": "ev_rule", "span_truncated": False}
        self.working_set = {"root_paths": ["programs/rule.cbl"], "source_manifest": [{
            "relative_path": "programs/rule.cbl", "sha256": self.page["source_sha256"], "line_count": 3}]}

    def repair(self, answer, pages=None, **options):
        return repair_source_availability_assertions(answer, pages or [self.page], self.working_set, **options)

    def test_processing_source_is_not_confused_with_runtime_parameters(self):
        for denied in ("rule.cbl 的字段处理源码未提供", "rule.cbl 的参数处理源码未提供",
                       "当前程序的字段处理源码未提供", "本轮未提供参数处理源码",
                       "rule.cbl 中 OUT-AMOUNT 字段处理源码未提供",
                       "The parameter processing source for rule.cbl was not supplied"):
            with self.subTest(denied=denied):
                original = f"金额按本金乘二计算。[ev_rule] {denied}，无法完整解答。运行参数尚未提供。"
                answer, records = self.repair(original, answer_language="zh-CN")
                self.assertIn("金额按本金乘二计算。[ev_rule]", answer)
                self.assertIn("运行参数尚未提供。", answer)
                self.assertIn("完整源码已提供", answer)
                self.assertNotIn("无法完整解答", answer)
                self.assertNotIn(denied, answer)
                self.assertFalse(source_availability_contradictions(answer, [self.page], self.working_set))
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["source_sha256"], self.page["source_sha256"])
                self.assertEqual(records[0]["supplied_ranges"], [{"start_line": 1, "end_line": 3}])

    def test_protected_history_and_real_unknowns_remain_byte_identical(self):
        texts = ("rule.cbl 的运行参数未提供。", "rule.cbl 源码的字段运行值未提供。",
                 "如果 rule.cbl 源码未提供，就不能核对。", "rule.cbl 源码未提供时需要补读。",
                 "历史回答称“rule.cbl 源码未提供”。", "`rule.cbl 源码未提供` 是历史错误。",
                 "> rule.cbl 源码未提供。\n当前金额由本金计算。",
                 "```text\nrule.cbl 源码未提供。\n```", "external.cbl 的字段处理源码未提供。",
                 "rule.cbl 的旧版本源码未提供。", "rule.cbl 的被调用程序源码未提供。",
                 "rule.cbl 的参数处理源码所需的运行参数未提供。")
        for original in texts:
            with self.subTest(original=original):
                answer, records = self.repair(original)
                self.assertEqual(answer, original)
                self.assertEqual(records, [])

    def test_actual_line_ranges_control_specific_range_denials(self):
        partial = {**self.page, "source_text": "MOVE 1 TO AMOUNT.", "start_line": 2, "end_line": 2}
        for original in ("rule.cbl 第1行源码未提供。", "rule.cbl 第2-3行源码未提供。",
                         "rule.cbl lines 9-10 were not supplied."):
            answer, records = self.repair(original, pages=[partial])
            self.assertEqual(answer, original)
            self.assertEqual(records, [])
        for original in ("rule.cbl 的字段处理源码未提供。", "rule.cbl 的参数处理源码未提供。"):
            answer, records = self.repair(original, pages=[partial])
            self.assertEqual(answer, original)
            self.assertEqual(records, [])
        answer, records = self.repair("rule.cbl 第2行源码未提供。", pages=[partial])
        self.assertIn("相关源码已提供", answer)
        self.assertNotIn("完整源码已提供", answer)
        self.assertEqual(records[0]["requested_range"], {"start_line": 2, "end_line": 2})
        self.assertEqual(records[0]["supplied_versions"][0]["source_sha256"], self.page["source_sha256"])

    def test_partial_wrong_version_and_truncated_source_do_not_prove_completeness(self):
        for page in ({**self.page, "source_sha256": "another-version"},
                     {**self.page, "span_truncated": True},
                     {**self.page, "source_text": "MAIN.", "end_line": 1}):
            original = "rule.cbl 的完整源码未提供。"
            answer, records = self.repair(original, pages=[page])
            self.assertEqual(answer, original)
            self.assertEqual(records, [])

    def test_repair_keeps_independent_prose_in_same_clause_and_quotes_unchanged(self):
        original = "历史回答称“rule.cbl源码未提供”。`rule.cbl` 源码未提供但金额按本金乘二。"
        answer, records = self.repair(original)
        self.assertTrue(answer.startswith("历史回答称“rule.cbl源码未提供”。`programs/rule.cbl`"))
        self.assertTrue(answer.endswith("但金额按本金乘二。"))
        self.assertEqual(len(records), 1)
        self.assertNotIn("``", answer)

    def test_multiple_repairs_preserve_offsets_and_valid_citations(self):
        original = "金额按本金乘二。[ev_rule] rule.cbl 源码未提供。输入另查。当前程序源码未提供。"
        answer, records = self.repair(original)
        self.assertEqual(len(records), 2)
        self.assertIn("金额按本金乘二。[ev_rule]", answer)
        self.assertIn("输入另查。", answer)
        self.assertEqual(answer.count("完整源码已提供"), 2)
        repeated, again = self.repair(answer)
        self.assertEqual(repeated, answer)
        self.assertEqual(again, [])

    def test_multiple_source_subjects_cannot_assert_an_unknown_file_was_supplied(self):
        for original in ("external.cbl 和 rule.cbl 的源码未提供。",
                         "rule.cbl 和 external.cbl 的源码未提供。",
                         "rule.cbl 已提供但 external.cbl 的源码未提供。",
                         "external.cbl 的源码未提供但 rule.cbl 已提供。",
                         "| rule.cbl | 源码未提供 | 金额按本金乘二 |"):
            with self.subTest(original=original):
                answer, records = self.repair(original)
                self.assertEqual(answer, original)
                self.assertEqual(records, [])

    def test_embedded_conditionals_are_not_rewritten_as_current_facts(self):
        for original in ("假设 rule.cbl 源码未提供，需要先读取。",
                         "rule.cbl 如果未提供源码就不能核对。",
                         "rule.cbl 源码未提供则不能核对。",
                         "rule.cbl source, if not supplied, should be read first."):
            answer, records = self.repair(original)
            self.assertEqual(answer, original)
            self.assertEqual(records, [])

    def test_dotted_path_quotes_and_runtime_field_values_keep_exact_boundaries(self):
        page = {**self.page, "relative_path": "modules/rules.v2/rule.cbl"}
        working_set = {"source_manifest": [{"relative_path": page["relative_path"],
                                           "sha256": page["source_sha256"], "line_count": 3}]}
        original = ('“modules/rules.v2/rule.cbl 未提供”是之前的错误。'
                    '`modules/rules.v2/rule.cbl` 的字段处理源码未提供。'
                    '字段的运行值没有提供。')
        answer, records = repair_source_availability_assertions(original, [page], working_set)
        self.assertTrue(answer.startswith('“modules/rules.v2/rule.cbl 未提供”是之前的错误。'))
        self.assertTrue(answer.endswith('字段的运行值没有提供。'))
        self.assertIn('`modules/rules.v2/rule.cbl` 的完整源码已提供[ev_rule]。', answer)
        self.assertEqual(len(records), 1)

    def test_english_repair_keeps_sentence_end_and_independent_sentence(self):
        answer, records = self.repair("The source for rule.cbl was not supplied. Runtime input remains unknown.",
                                     answer_language="en")
        self.assertIn("was supplied[ev_rule]. Runtime input remains unknown.", answer)
        self.assertEqual(len(records), 1)
        self.assertNotRegex(answer, r"[\u3400-\u9fff]")


if __name__ == "__main__":
    unittest.main()
