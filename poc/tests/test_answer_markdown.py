from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_markdown import normalize_answer_markdown, strip_incomplete_source_citation
from business_analysis import _extract_text
from business_chat import _actions


class AnswerMarkdownTests(unittest.TestCase):
    def extract(self, content):
        return _extract_text({"choices": [{"message": {"role": "assistant", "content": content},
                                          "finish_reason": "stop"}]})

    def test_business_markdown_preserves_tables_lists_formulas_and_citations(self):
        markdown = ("**金额按基础值乘以比例计算。** [ev:rule-42]\n\n"
                    "## 条件与结果\n\n| 条件 | 结果 |\n| --- | --- |\n| 超过上限 | 复核 |\n\n"
                    "- 先检查 `REQUEST-STATE`。\n- 再计算 `TOTAL = BASE * RATE`。")
        self.assertEqual(self.extract(markdown).text, markdown)
        for content in (f"```markdown\n{markdown}\n```", f"~~~MD\n{markdown}\n~~~",
                        json.dumps({"answer": markdown}, ensure_ascii=False),
                        json.dumps({"content": f"```md\n{markdown}\n```"}, ensure_ascii=False)):
            with self.subTest(content=content):
                result = self.extract(content)
                self.assertEqual(result.text, markdown)
                self.assertFalse(result.structured)

    def test_local_code_blocks_survive_markdown_envelope(self):
        markdown = "计算代码如下：\n\n```cobol\nCOMPUTE TOTAL = BASE * RATE.\n```\n\n金额按比例计算。"
        self.assertEqual(self.extract(f"````markdown\n{markdown}\n````").text, markdown)
        self.assertEqual(self.extract(markdown).text, markdown)

    def test_same_length_outer_markdown_and_inner_labelled_code_are_unwrapped(self):
        for inner in ("```cobol\nCOMPUTE TOTAL = BASE * RATE.\n```",
                      "~~~python\ntotal = base * rate\n~~~",
                      "```markdown\n**literal example**\n```",
                      "```cobol\nMOVE BASE TO TOTAL.\n```\n\n```cobol\nADD FEE TO TOTAL.\n```"):
            with self.subTest(inner=inner):
                markdown = f"**计算规则如下。**\n\n{inner}\n\n以上计算包含基础金额。"
                wrapped = f"```markdown\n{markdown}\n```"
                self.assertEqual(self.extract(wrapped).text, markdown)
                self.assertEqual(self.extract(json.dumps({"answer": wrapped})).text, markdown)

    def test_unfinished_inner_code_does_not_consume_a_missing_outer_close(self):
        text = "```markdown\n计算规则如下：\n\n```cobol\nCOMPUTE TOTAL = BASE * RATE.\n```"
        self.assertEqual(normalize_answer_markdown(text), text)

    def test_code_examples_and_ambiguous_fences_are_not_removed(self):
        examples = ("```cobol\nCOMPUTE TOTAL = BASE * RATE.\n```",
                    "```\n**literal example**\n```",
                    "```text\n**literal example**\n```",
                    "```md\n**first example**\n```\n\n```md\n**second example**\n```",
                    "```markdown\n**unfinished example**")
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(normalize_answer_markdown(text), text)

    def test_optional_investigation_protocol_still_parses(self):
        command = '{"search":["RULE-SET-42"],"read":[{"relative_path":"rule.cbl","start_line":10,"end_line":20}]}'
        for content in (command, f"```json\n{command}\n```"):
            with self.subTest(content=content):
                self.assertEqual(_actions(self.extract(content).text), json.loads(command))

    def test_opaque_structured_business_data_is_preserved(self):
        content = '{"rules":[{"condition":"over limit","outcome":"review"}]}'
        result = self.extract(content)
        self.assertEqual(result.text, content)
        self.assertTrue(result.structured)

    def test_only_supplied_unclosed_source_citation_at_truncated_prose_tail_is_removed(self):
        identifier = "ev_page_0123456789abcdef01234567"
        prefix = "已完成的业务正文和完整引用保留。[" + identifier + "]\n\n其后调用："
        for suffix in ("[" + identifier[:18], "[" + identifier, "[" + identifier[:18] + "\n"):
            with self.subTest(suffix=suffix):
                self.assertEqual(strip_incomplete_source_citation(prefix + suffix, truncated=True,
                                 evidence_ids=[identifier]), prefix)
                self.assertEqual(strip_incomplete_source_citation(prefix + suffix, truncated=False,
                                 evidence_ids=[identifier]), prefix + suffix)

    def test_ordinary_brackets_complete_unknown_and_nonterminal_markers_are_unchanged(self):
        identifier = "ev_page_0123456789abcdef01234567"
        samples = ("普通条件 [尚未写完", "普通圆括号（尚未写完", "完整 [" + identifier + "]",
                   "未知 [ev_page_ffffffff", "片段 [ev_page_", "内部[ev_page_012345 后有正文",
                   "转义 \\[ev_page_012345", "多于哈希长度 [" + identifier + "a")
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(strip_incomplete_source_citation(sample, truncated=True,
                                 evidence_ids=[identifier]), sample)

    def test_incomplete_internal_marker_inside_code_is_not_display_repaired(self):
        identifier = "ev_page_0123456789abcdef01234567"
        samples = ("代码 `[ev_page_012345", "代码 ``literal [ev_page_012345",
                   "```text\n[ev_page_012345", "~~~text\n[ev_page_012345",
                   "    [ev_page_012345", "\t[ev_page_012345", r"\\`[ev_page_012345",
                   "> ~~~text\n> [ev_page_012345")
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(strip_incomplete_source_citation(sample, truncated=True,
                                 evidence_ids=[identifier]), sample)
        closed = "```text\n完整代码\n```\n正文：[ev_page_012345"
        self.assertEqual(strip_incomplete_source_citation(closed, truncated=True, evidence_ids=[identifier]),
                         "```text\n完整代码\n```\n正文：")


if __name__ == "__main__":
    unittest.main()
